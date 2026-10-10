"""大模型整理：统一走 OpenAI 兼容协议，支持多家供应商 + 兜底。

* 主推 DeepSeek（``https://api.deepseek.com``，模型 ``deepseek-chat``）；
* 也支持 OpenAI / Kimi / 智谱 / 通义 / 火山 / 硅基流动 / OpenRouter / Groq / 百度等
  （只要提供 OpenAI 兼容的 ``base_url`` + ``api_key`` + ``model``）。
* :class:`LLMClient` 会按配置顺序依次尝试，前一个失败自动切到下一个（兜底）；
  识图只用标注了 ``vision`` 能力的供应商。纯出图（文生图）接口不在此列。
"""
from __future__ import annotations

import json
import re
from typing import Callable, Dict, List, Optional

from .. import prompts
from ..ui import i18n
from .config import API_PROVIDER_BY_ID


def _vision_judge_prompt(n: int) -> str:
    """构造「核对候选画面」的提示词（文案随界面语言）。"""
    return i18n.trs(
        "ll_vision_prompt",
        "下面是同一个视频里按时间顺序截取的 {n} 张画面（编号 1 到 {n}，按输入顺序对应）。\n"
        "判断每张是否适合作为博客配图并打分：\n"
        "- 7-10 分：信息量高，如聊天记录、文档、图表、数据、界面截图、实物特写、关键画面\n"
        "- 4-6 分：一般，如人物大头像、普通风景、以字幕为主的画面\n"
        "- 0-3 分：不适合，如模糊、纯色/黑屏、与其它画面几乎重复\n"
        "只输出 JSON 数组，不要解释、不要代码块围栏：\n"
        '[{{"id":1,"score":8,"useful":true,"caption":"画面内容，用中文，含关键文字"}}]',
        n=n)


class LLMError(RuntimeError):
    """大模型调用阶段的可读错误。"""


class ProviderClient:
    """单个 OpenAI 兼容协议的供应商客户端（DeepSeek / OpenAI / Kimi / 智谱 …）。"""

    def __init__(
        self,
        *,
        provider_id: str = "deepseek",
        name: str = "DeepSeek",
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        vision: bool = False,
        vision_model: str = "",
        timeout: int = 180,
    ) -> None:
        if not api_key:
            raise LLMError(i18n.trs("ll_no_key", "{name} 未配置 API Key，请在 setup 中填写。",
                                    name=name or provider_id))
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise LLMError(i18n.trs("ll_no_openai", "未安装 openai 库，请运行：pip install openai")) from exc
        self.provider_id = provider_id or "deepseek"
        self.name = name or self.provider_id
        self.model = model or "deepseek-chat"
        self.vision = bool(vision)
        self.vision_model = vision_model or self.model
        meta = API_PROVIDER_BY_ID.get(self.provider_id) or {}
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url or meta.get("base_url") or "https://api.deepseek.com",
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    def _chat(
        self,
        system: str,
        user: str,
        on_log: Optional[Callable[[str], None]] = None,
        temperature: float = 0.4,
        max_tokens: int = 4000,
    ) -> str:
        if on_log:
            on_log(i18n.trs("ll_llm_call", "调用 {name}（模型 {model}，输入 {n} 字）…",
                            name=self.name, model=self.model, n=len(user)))
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
            )
        except Exception as exc:  # noqa: BLE001
            raise LLMError(_friendly_error(exc, self.name)) from exc

        content = (resp.choices[0].message.content or "").strip()
        if on_log:
            if resp.choices[0].finish_reason == "length":
                on_log(i18n.trs("ll_len_limit", "   ⚠ 输出达到长度上限，内容可能被截断"))
            usage = getattr(resp, "usage", None)
            if usage is not None:
                pt = getattr(usage, "prompt_tokens", 0) or 0
                ct = getattr(usage, "completion_tokens", 0) or 0
                tt = getattr(usage, "total_tokens", 0) or (pt + ct)
                hit = getattr(usage, "prompt_cache_hit_tokens", None)
                extra = i18n.trs("ll_cache_hit", "，缓存命中 {hit}", hit=hit) if hit else ""
                on_log(i18n.trs("ll_llm_return",
                                "[{name}] 返回 {n} 字｜tokens 输入 {pt} / 输出 {ct} / 合计 {tt}{extra}",
                                name=self.name, n=len(content), pt=pt, ct=ct, tt=tt, extra=extra))
        return content

    def judge_images(self, candidates: List, on_log=None, batch_size: int = 2) -> None:
        """用视觉模型核对候选画面是否适合做配图，并把描述写回 candidate。

        注意：deepseek-flash 是推理型模型，会先消耗大量 reasoning token；
        一次塞太多图片会把 token 预算吃光导致正文为空，所以分批（默认每批 2 张）
        核对，失败的批次再逐张重试。
        """
        if not candidates:
            return
        if on_log:
            on_log(i18n.trs("ll_vision_start", "   用视觉模型（{m}）核对 {n} 张候选画面…",
                            m=self.vision_model, n=len(candidates)))

        batches = [candidates[i:i + batch_size] for i in range(0, len(candidates), batch_size)]
        got = 0
        for bi, batch in enumerate(batches, start=1):
            if on_log:
                on_log(i18n.trs("ll_vision_batch", "   第 {i}/{t} 批：{n} 张",
                                i=bi, t=len(batches), n=len(batch)))
            found = self._judge_batch(batch, on_log=on_log)
            if not found:
                # 整批失败 → 逐张重试
                for cand in batch:
                    found.update(self._judge_batch([cand], on_log=on_log))
            got += len(found)
            by_id = {c.index: c for c in candidates}
            for idx, item in found.items():
                cand = by_id.get(idx)
                if cand is None:
                    continue
                cand.caption = str(item.get("caption") or "").strip()
                cand.useful = bool(item.get("useful", True))
                try:
                    cand.vision_score = float(item.get("score", 0))
                except (TypeError, ValueError):
                    cand.vision_score = 0.0

        if on_log:
            if got:
                on_log(i18n.trs("ll_vision_done", "   视觉核对完成，{got}/{total} 张拿到评分",
                                got=got, total=len(candidates)))
            else:
                on_log(i18n.trs("ll_vision_none", "   （视觉模型没有返回可用结果，沿用启发式筛选）"))

    def _judge_batch(self, batch: List, on_log=None) -> dict:
        """核对一小批图片，返回 {candidate.index: item}。"""
        from .frames import to_vision_data_url

        content: List[dict] = [
            {"type": "text", "text": _vision_judge_prompt(len(batch))}
        ]
        for cand in batch:
            content.append({"type": "image_url", "image_url": {"url": to_vision_data_url(cand)}})

        try:
            resp = self._client.chat.completions.create(
                model=self.vision_model,
                messages=[{"role": "user", "content": content}],
                temperature=0.1,
                max_tokens=3500,
            )
        except Exception as exc:  # noqa: BLE001
            if on_log:
                on_log(i18n.trs("ll_vision_batch_fail", "      （本批调用失败：{e}）", e=exc))
            return {}

        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
        usage = getattr(resp, "usage", None)
        if on_log and usage is not None:
            on_log(i18n.trs("ll_vision_tokens", "      tokens {n}，完成原因 {r}",
                            n=getattr(usage, 'total_tokens', '?'), r=choice.finish_reason))
        if not text:
            if on_log:
                on_log(i18n.trs("ll_vision_empty", "      （本批无正文返回，完成原因 {r}）",
                                r=choice.finish_reason))
            return {}

        out: dict = {}
        for item in _parse_json_array(text):
            try:
                local = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if 1 <= local <= len(batch):
                out[batch[local - 1].index] = item
        return out

    # ------------------------------------------------------------------
    def polish(self, transcript: str, on_log=None) -> str:
        """第一步：校对 + 断句 + 去口水词（按输入长度给足输出预算）。"""
        budget = int(min(8000, max(1500, len(transcript) * 1.2)))
        return self._chat(
            prompts.POLISH_SYSTEM,
            prompts.polish_user(transcript),
            on_log=on_log,
            temperature=0.2,
            max_tokens=budget,
        )

    def polish_long(self, transcript: str, on_log=None, chunk_chars: int = 2400) -> str:
        """长文本分段校对，避免一次输出被截断导致丢内容。"""
        if len(transcript) <= int(chunk_chars * 1.6):
            return self.polish(transcript, on_log=on_log)
        chunks = _split_chunks(transcript, chunk_chars)
        if on_log:
            on_log(i18n.trs("ll_long_split", "   文字稿较长（{n} 字），分 {k} 段分别校对",
                            n=len(transcript), k=len(chunks)))
        parts = []
        for i, chunk in enumerate(chunks, start=1):
            if on_log:
                on_log(i18n.trs("ll_long_chunk", "   校对第 {i}/{t} 段（{n} 字）",
                                i=i, t=len(chunks), n=len(chunk)))
            parts.append(self.polish(chunk, on_log=on_log))
        return "".join(parts)

    def write_article(
        self,
        transcript: str,
        info,
        images=None,
        mode: str = "blog",
        language: str = "en",
        length=None,
        on_log=None,
    ) -> str:
        """第二步：整理成 Markdown（mode：'blog' 博客模式 / 'note' 笔记模式）。

        language 为成稿语言代码（见 config.OUTPUT_LANGUAGES）。
        length 为成稿篇幅等级 1-5（见 config.ARTICLE_LENGTHS），None 时用默认。
        """
        from .config import DEFAULT_ARTICLE_LENGTH, output_language_name

        lang_name = output_language_name(language)
        level = DEFAULT_ARTICLE_LENGTH if length is None else length
        return self._chat(
            prompts.article_system(mode, lang_name, level),
            prompts.article_user(
                transcript, info, images=images, language_name=lang_name, length_level=level
            ),
            on_log=on_log,
            temperature=0.6,
            max_tokens=8000,
        )


# ----------------------------------------------------------------------------
class LLMClient:
    """多供应商客户端：文本调用按顺序兜底，识图只用支持 vision 的供应商。"""

    def __init__(self, providers: List[Dict]) -> None:
        clients: List[ProviderClient] = []
        for p in (providers or []):
            if not isinstance(p, dict):
                continue
            try:
                clients.append(ProviderClient(
                    provider_id=str(p.get("id") or "deepseek"),
                    name=str(p.get("name") or ""),
                    api_key=str(p.get("api_key") or ""),
                    base_url=str(p.get("base_url") or ""),
                    model=str(p.get("model") or ""),
                    vision=bool(p.get("vision")),
                    vision_model=str(p.get("vision_model") or ""),
                ))
            except LLMError:
                continue
        if not clients:
            raise LLMError(i18n.trs("ll_no_keys", "未配置任何可用的大模型 API Key，请在 setup 中填写。"))
        self.clients = clients

    # ---- 供日志/概览展示 ----
    @property
    def name(self) -> str:
        return self.clients[0].name

    @property
    def model(self) -> str:
        return self.clients[0].model

    @property
    def vision_model(self) -> str:
        for c in self.clients:
            if c.vision:
                return c.vision_model
        return self.clients[0].vision_model

    def _text(self, call: Callable[[ProviderClient], str], on_log=None) -> str:
        last: Optional[Exception] = None
        for i, c in enumerate(self.clients):
            try:
                if on_log and i > 0:
                    on_log(i18n.trs("ll_fallback", "   ↻ 改用兜底供应商 {name}（{model}）…",
                                    name=c.name, model=c.model))
                return call(c)
            except LLMError as exc:
                last = exc
                if on_log:
                    on_log(i18n.trs("ll_call_fail", "   ⚠ {name} 调用失败：{e}", name=c.name, e=exc))
        raise last or LLMError(i18n.trs("ll_all_fail", "所有大模型供应商均调用失败，请检查 API 配置。"))

    # ---- 与单供应商一致的接口，便于 pipeline 直接调用 ----
    def polish(self, transcript: str, on_log=None) -> str:
        return self._text(lambda c: c.polish(transcript, on_log=on_log), on_log)

    def polish_long(self, transcript: str, on_log=None, chunk_chars: int = 2400) -> str:
        return self._text(
            lambda c: c.polish_long(transcript, on_log=on_log, chunk_chars=chunk_chars), on_log
        )

    def write_article(self, *args, **kwargs) -> str:
        return self._text(lambda c: c.write_article(*args, **kwargs), kwargs.get("on_log"))

    def judge_images(self, candidates: List, on_log=None, batch_size: int = 2) -> None:
        vis = [c for c in self.clients if c.vision]
        if not vis:
            if on_log:
                on_log(i18n.trs("ll_no_vision", "   （没有支持识图的供应商，跳过视觉核对，改用启发式筛选）"))
            return
        for c in vis:
            try:
                c.judge_images(candidates, on_log=on_log, batch_size=batch_size)
                return
            except LLMError as exc:
                if on_log:
                    on_log(i18n.trs("ll_vision_provider_fail", "   ⚠ 识图供应商 {name} 失败：{e}",
                                    name=c.name, e=exc))


def build_client(cfg) -> Optional[LLMClient]:
    """按配置构造多供应商客户端；无可用供应商时返回 ``None``。"""
    providers = cfg.effective_providers() if hasattr(cfg, "effective_providers") else []
    if not providers:
        return None
    return LLMClient(providers)


class DeepSeekClient(ProviderClient):
    """向后兼容：旧的单 DeepSeek 调用方式（位置参数）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        vision_model: str = "deepseek-flash",
    ) -> None:
        super().__init__(
            provider_id="deepseek",
            name="DeepSeek",
            api_key=api_key,
            base_url=base_url,
            model=model,
            vision=True,
            vision_model=vision_model,
        )


def _split_chunks(text: str, size: int) -> List[str]:
    """按句子边界把长文本切成若干段（每段约 size 字）。"""
    chunks: List[str] = []
    rest = text or ""
    while len(rest) > size * 1.4:
        window = rest[: int(size * 1.3)]
        cut = max(window.rfind(p) for p in ("。", "！", "？", "\n"))
        cut = cut + 1 if cut >= size * 0.5 else size
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


def _parse_json_array(text: str) -> List[dict]:
    """从模型输出里稳健地抽出 JSON 数组（可能带代码块围栏或前后解释）。"""
    if not text:
        return []
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    decoder = json.JSONDecoder()
    idx = cleaned.find("[")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(cleaned[idx:])
        except Exception:  # noqa: BLE001
            obj = None
        if isinstance(obj, list):
            return [x for x in obj if isinstance(x, dict)]
        idx = cleaned.find("[", idx + 1)
    return []


def _friendly_error(exc: Exception, name: str = "") -> str:
    who = name or i18n.trs("ll_err_generic_who", "大模型")
    msg = str(exc)
    low = msg.lower()
    if "401" in low or "authentication" in low or "invalid api key" in low or "api key" in low:
        return i18n.trs("ll_err_auth", "{who} 鉴权失败：API Key 无效或已过期，请在 setup 中检查。\n\n原始错误：{msg}",
                        who=who, msg=msg)
    if "402" in low or "insufficient" in low or "balance" in low:
        return i18n.trs("ll_err_balance", "{who} 账户余额不足，请充值后重试。\n\n原始错误：{msg}", who=who, msg=msg)
    if "429" in low or "rate limit" in low:
        return i18n.trs("ll_err_rate", "{who} 请求过于频繁（限流），请稍后重试。\n\n原始错误：{msg}", who=who, msg=msg)
    if "timeout" in low or "timed out" in low:
        return i18n.trs("ll_err_timeout", "{who} 请求超时，请检查网络或稍后重试。\n\n原始错误：{msg}", who=who, msg=msg)
    if "connect" in low or "network" in low:
        return i18n.trs("ll_err_network", "无法连接 {who}，请检查网络/代理设置。\n\n原始错误：{msg}", who=who, msg=msg)
    if "model" in low and ("not exist" in low or "not found" in low or "not supported" in low):
        return i18n.trs("ll_err_model",
                        "模型不可用：请检查 setup 里的模型名是否正确，或该账号是否有权使用该模型。\n\n原始错误：{msg}",
                        msg=msg)
    if "404" in low:
        return i18n.trs("ll_err_404", "接口返回 404：模型名或接口地址可能不对，请在 setup 里检查。\n\n原始错误：{msg}",
                        msg=msg)
    return i18n.trs("ll_err_generic", "调用 {who} 失败：{msg}", who=who, msg=msg)
