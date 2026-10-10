"""处理流水线：下载 → 抽音频 → 语音识别 → 截取配图 → DeepSeek 整理 → 导出 Markdown。

日志设计：每一步都有明确的开始/结束标记与耗时，中间尽量输出可核对的数量指标
（文件大小、音频时长、字数、token 用量等），便于排查问题。
"""
from __future__ import annotations

import datetime as _dt
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import quote, urlparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import cache, downloader, frames, llm
from ..ui import i18n
from .asr import SenseVoiceASR
from .config import (
    DEFAULT_ARTICLE_LENGTH,
    AppConfig,
    VIDEO_SOURCE_BY_ID,
    output_language_name,
)
from .downloader import VideoInfo


class PipelineError(RuntimeError):
    """流水线可读错误。"""


@dataclass
class PipelineResult:
    md_path: Path
    title: str
    transcript: str
    markdown: str
    info: VideoInfo
    used_llm: bool
    elapsed: float = 0.0
    images: List = field(default_factory=list)   # List[frames.FrameCandidate]
    assets_dir: Optional[Path] = None
    cover_path: Optional[Path] = None


_BAD_NAME_RE = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_MARKER_RE = re.compile(
    r"\{\{\s*IMG[_\- ]?(\d+)\s*\}\}|\[\[\s*IMG[_\- ]?(\d+)\s*\]\]",
    re.IGNORECASE,
)
_LEFTOVER_RE = re.compile(r"\{\{\s*IMG[^}]*\}\}", re.IGNORECASE)


# ----------------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------------
def _safe_name(name: str, fallback: str = "douyin") -> str:
    name = _BAD_NAME_RE.sub(" ", name or "")
    name = re.sub(r"\s+", " ", name).strip()
    return name[:60] or fallback


def _fmt_sec(seconds) -> str:
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return i18n.trs("t_unknown", "未知")
    if total <= 0:
        return i18n.trs("t_unknown", "未知")
    m, s = divmod(total, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return i18n.trs("t_hms", "{h} 小时 {m} 分 {s} 秒", h=h, m=m, s=s)
    return i18n.trs("t_ms", "{m} 分 {s} 秒", m=m, s=s)


def _fmt_size(path) -> str:
    try:
        n = Path(path).stat().st_size
    except Exception:  # noqa: BLE001
        return i18n.trs("pl_size_unknown", "未知大小")
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"


def _ffprobe_path(ffmpeg_path: str = "") -> Optional[str]:
    """根据已解析到的 ffmpeg 位置，猜出同目录下的 ffprobe；找不到就查 PATH。"""
    if ffmpeg_path:
        p = Path(ffmpeg_path)
        for name in ("ffprobe.exe", "ffprobe"):
            cand = p.parent / name
            if cand.exists():
                return str(cand)
    return shutil.which("ffprobe")


def _probe_duration(path, ffmpeg_path: str = "") -> float:
    """探测本地视频时长（秒）：优先 ffprobe，失败再用 OpenCV，都拿不到返回 0。"""
    path = Path(path)
    probe = _ffprobe_path(ffmpeg_path)
    if probe:
        try:
            out = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            val = float((out.stdout or "").strip())
            if val > 0:
                return val
        except Exception:  # noqa: BLE001
            pass
    try:
        import cv2

        cap = cv2.VideoCapture(str(path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        cap.release()
        if fps > 0 and frames > 0:
            return float(frames) / float(fps)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _extract_title(markdown: str, default: str) -> str:
    m = re.search(r"^\s*#\s+(.+?)\s*$", markdown, re.MULTILINE)
    return m.group(1).strip() if m else (default or i18n.trs("pl_untitled", "抖音短视频笔记"))


def _build_frontmatter(info: VideoInfo, title: str, cover_rel: str = "",
                       source: str = "网页视频") -> str:
    def esc(s: str) -> str:
        return (s or "").replace('"', "'").replace("\n", " ").strip()

    lines = ["---"]
    lines.append(f'title: "{esc(title)}"')
    lines.append(f'source: "{esc(source)}"')
    if info.author:
        lines.append(f'author: "{esc(info.author)}"')
    if info.webpage_url:
        lines.append(f'url: "{esc(info.webpage_url)}"')
    if info.video_id:
        lines.append(f'video_id: "{esc(info.video_id)}"')
    if info.duration:
        lines.append(f"duration_seconds: {round(info.duration, 1)}")
    if cover_rel:
        lines.append(f'cover: "{cover_rel}"')
    lines.append(f"created: {_dt.date.today().isoformat()}")
    lines.append("---")
    return "\n".join(lines)


class _Step:
    """上下文管理器：自动打印「步骤 x/y 开始」与「完成（耗时）」，并驱动进度回调。

    进入步骤时把「步骤内部进度」置为不定态（-1），有细分进度的步骤再自行上报百分比；
    正常结束时置为 100%。
    """

    def __init__(
        self,
        log: Callable[[str], None],
        index: int,
        total: int,
        title: str,
        on_update: Optional[Callable[[int, str], None]] = None,
    ) -> None:
        self._log = log
        self._index = index
        self._total = total
        self._title = title
        self._on_update = on_update

    def __enter__(self) -> "_Step":
        self._log("")
        self._log(i18n.tr("ps_hdr", i=self._index, t=self._total, title=self._title))
        self._t0 = time.time()
        if self._on_update:
            self._on_update(-1, self._title)   # -1 = 本步骤进行中（不定态）
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        cost = time.time() - self._t0
        if exc_type is None:
            self._log(i18n.tr("ps_done", title=self._title, cost=cost))
            if self._on_update:
                self._on_update(100, self._title)
        else:
            self._log(i18n.tr("ps_fail", title=self._title, cost=cost))
        return False


def _fetch_bytes(url: str) -> bytes:
    """下载图片字节。按站点设置合适的 Referer，避免图床的防盗链拦截。"""
    try:
        u = url
        # B 站图床有时给的是 http，且需要 referer
        if u.startswith("http://") and "hdslb.com" in u:
            u = "https://" + u[len("http://"):]
        host = urlparse(u).netloc.lower()
        if "hdslb.com" in host:
            referer = "https://www.bilibili.com/"
        elif "douyin" in host or "bytedance" in host:
            referer = "https://www.douyin.com/"
        elif host:
            referer = f"https://{host}/"
        else:
            referer = "https://www.douyin.com/"
        req = urllib.request.Request(
            u,
            headers={
                "User-Agent": downloader.DESKTOP_UA,
                "Referer": referer,
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except Exception:  # noqa: BLE001
        return b""


def _to_jpeg(data: bytes) -> bytes:
    """把任意图片（抖音封面常是 webp）转成真正的 JPEG；失败则原样返回。"""
    try:
        import cv2
        import numpy as np

        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return data
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        return buf.tobytes() if ok else data
    except Exception:  # noqa: BLE001
        return data


def _download_cover(url: str, dest: Path) -> Optional[Path]:
    """下载抖音封面图（先尝试更高分辨率，失败则用原图；统一转成 JPEG）。"""
    if not url:
        return None
    urls = []
    bigger = re.sub(r"(aq:)\d+", r"\g<1>1080", url)
    if bigger != url:
        urls.append(bigger)
    urls.append(url)
    for u in urls:
        data = _fetch_bytes(u)
        if not data or len(data) < 1024:
            continue
        dest.write_bytes(_to_jpeg(data))
        return dest
    return None


def _grab_cover(info: VideoInfo, tmp_dir: Path, log: Callable[[str], None]) -> Optional[Path]:
    """把视频封面下载到临时目录并返回路径（独立于配图张数）。"""
    if not info.thumbnail:
        log(i18n.trs("pl_no_cover", "   （提示）该来源没有可用的封面地址"))
        return None
    cover_tmp = Path(tmp_dir) / "cover.jpg"
    got = _download_cover(info.thumbnail, cover_tmp)
    if got:
        log(i18n.trs("pl_cover_saved", "   封面已下载：{size}", size=_fmt_size(got)))
        return got
    log(i18n.trs("pl_cover_failed", "   （提示）封面下载失败，已忽略"))
    return None


def _pick_evenly(items: List, count: int) -> List:
    """从列表里等间隔取 count 个（保持原有顺序）。"""
    if count <= 0:
        return []
    if len(items) <= count:
        return list(items)
    if count == 1:
        return [items[len(items) // 2]]
    return [items[round(i * (len(items) - 1) / (count - 1))] for i in range(count)]


def _md_url(rel: str) -> str:
    """把相对图片路径转成合法的 Markdown 链接。

    文件名里可能带空格、em dash、中文等，直接写进 ``![](...)`` 会被 Markdown
    解析器当成普通文本，图片显示不出来；这里统一做百分号转义。
    """
    return quote((rel or "").replace("\\", "/"), safe="/:")


def _resolve_markers(article: str, path_by_index: dict, log: Callable[[str], None]):
    """把文章里的 {{IMG_n}} 占位符替换成真实图片路径。"""
    used = set()

    def repl(m: re.Match) -> str:
        num = int(m.group(1) or m.group(2))
        if num in path_by_index:
            used.add(num)
            return _md_url(path_by_index[num])
        return ""

    article = _MARKER_RE.sub(repl, article)
    leftovers = _LEFTOVER_RE.findall(article)
    if leftovers:
        article = _LEFTOVER_RE.sub("", article)
        log(i18n.trs("pl_markers_cleaned", "   清理了 {n} 处未匹配的图片标记", n=len(leftovers)))
    unused = [n for n in path_by_index if n not in used]
    return article, sorted(used), unused


def _insert_cover(article: str, cover_rel: str) -> str:
    """把封面图插到一级标题之后。"""
    lines = article.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("# "):
            lines.insert(i + 1, f"\n![cover]({_md_url(cover_rel)})\n")
            return "\n".join(lines)
    return f"![cover]({_md_url(cover_rel)})\n\n{article}"


def _make_progress(
    on_progress: Optional[Callable[[int, int, str, int, int], None]],
    total_steps: int,
    log: Callable[[str], None],
):
    """构造「总进度 + 步骤内进度」上报器，返回 (emit, step)。

    把这段逻辑独立出来，让「完整流程」与「复用缓存重生成」两条路径共用。
    """
    counter = {"n": 0}
    high = {"overall": 0}   # 已上报过的最大总进度（保证单调不减）

    def emit(step_pct: int, msg: str = "") -> None:
        """上报进度：step_pct<0 表示本步骤不定态。"""
        if not on_progress:
            return
        idx = max(1, min(total_steps, counter["n"]))
        if step_pct is None or step_pct < 0:
            sub, frac = -1, 0.0
        else:
            sub = int(max(0, min(100, step_pct)))
            frac = sub / 100.0
        overall = int(round(((idx - 1) + frac) / total_steps * 100.0))
        overall = max(0, min(100, overall))
        # 不定态（sub=-1）会把 frac 归零，容易让总进度回退；这里钳制成单调不减
        overall = max(overall, high["overall"])
        high["overall"] = overall
        on_progress(overall, sub, msg, idx, total_steps)

    def step(title: str) -> _Step:
        counter["n"] += 1
        return _Step(log, counter["n"], total_steps, title, on_update=emit)

    return emit, step


def _organize_and_export(
    *,
    cfg: AppConfig,
    info: VideoInfo,
    transcript: str,
    images: List[frames.FrameCandidate],
    cover_candidate_path: Optional[Path],
    source_name: str,
    log: Callable[[str], None],
    emit: Callable[..., None],
    step: Callable[[str], _Step],
    llm_ready: bool,
    mode: str,
    out_language: str,
    out_language_name: str,
    out_length: int,
    out_length_name: str,
    copy_video: bool = False,
) -> PipelineResult:
    """「整理文本 → 导出 Markdown」两步。

    由 ``run_pipeline``（完整流程）与 ``regenerate_from_cache``（复用缓存）共用；
    返回的 ``PipelineResult`` 其 ``elapsed`` 由调用方补齐。
    """
    # ---------------- 整理文本 ----------------
    organize_title = (
        i18n.tr("ps_note" if mode == "note" else "ps_blog")
        if llm_ready else i18n.tr("ps_format")
    )
    used_llm = False
    with step(organize_title):
        if llm_ready:
            client = llm.build_client(cfg)
            if client is None:
                raise PipelineError(i18n.trs("pe_no_api", "未配置可用的大模型 API Key，请在 setup 中填写。"))
            emit(-1, i18n.tr("ps_proofread"))
            log(i18n.trs("pl_polish_in", "第 1 步：校对与断句（输入 {n} 字）", n=len(transcript)))
            polished = client.polish_long(transcript, on_log=log)
            log(i18n.trs("pl_polish_out", "         校对后 {n} 字", n=len(polished)))
            emit(-1, i18n.tr("ps_writing"))
            kind = i18n.tr("mode_note") if mode == "note" else i18n.tr("mode_blog")
            log(i18n.trs("pl_write_in",
                         "第 2 步：撰写{kind}（配图 {imgs} 张，语言 {lang}，篇幅 {length}）",
                         kind=kind, imgs=len(images), lang=out_language_name, length=out_length_name))
            article = client.write_article(
                polished, info, images=images, mode=mode,
                language=out_language, length=out_length, on_log=log,
            ).strip()
            if not article:
                raise PipelineError(i18n.trs("pe_empty_article", "大模型返回了空内容，请稍后重试。"))
            sections = len(re.findall(r"^##\s+", article, re.MULTILINE))
            ratio = len(article) / max(1, len(transcript)) * 100
            log(i18n.trs("pl_article_stats",
                         "         生成正文 {n} 字，共 {sections} 个小节（约为文字稿的 {ratio:.0f}%）",
                         n=len(article), sections=sections, ratio=ratio))
            min_ratio = {1: 20, 2: 35, 3: 50, 4: 60, 5: 75}.get(out_length, 60)
            if ratio < min_ratio:
                log(i18n.trs("pl_article_short",
                             "         ⚠ 正文相对文字稿偏短（{length}档目标约 {minr}% 以上），可能仍有内容被压缩；可重试或调高篇幅档",
                             length=out_length_name, minr=min_ratio))
            used_llm = True
        else:
            log(i18n.trs("pl_llm_skipped", "已跳过（未启用或未配置大模型 API Key），直接使用原始转录"))
            article = f"# {info.title or i18n.trs('pl_untitled', '抖音短视频笔记')}\n\n{transcript}"
            used_llm = False

    # ---------------- 导出 ----------------
    with step(i18n.tr("ps_export")):
        out_dir = cfg.resolved_output_dir
        title = _extract_title(article, info.title)
        stamp = _dt.datetime.now().strftime("%Y%m%d")
        suffix = f"_{info.video_id}" if info.video_id else ""
        md_name = f"{stamp}_{_safe_name(title, 'douyin')}{suffix}.md"
        md_path = out_dir / md_name

        # 保存配图到 <md文件名>.assets/
        assets_dir: Optional[Path] = None
        cover_rel = ""
        path_by_index: dict = {}
        if images or cover_candidate_path:
            assets_dir = out_dir / f"{md_path.stem}.assets"
            assets_dir.mkdir(parents=True, exist_ok=True)
            if cover_candidate_path and Path(cover_candidate_path).exists():
                cover_dest = assets_dir / "cover.jpg"
                shutil.copy2(cover_candidate_path, cover_dest)
                cover_rel = f"{assets_dir.name}/cover.jpg"
                log(i18n.trs("pl_cover_file", "封面文件 ：{rel}", rel=cover_rel))
            for c in images:
                name = f"{c.index:02d}.jpg"
                (assets_dir / name).write_bytes(c.jpeg)
                path_by_index[c.index] = f"{assets_dir.name}/{name}"
                log(i18n.trs("pl_img_item", "配图 {idx}   ：{name}（{w}x{h}，视频 {ts} 处）",
                             idx=c.index, name=name, w=c.width, h=c.height,
                             ts=frames.fmt_ts(c.timestamp)))

        # 替换配图占位符
        unused: List[int] = []
        if path_by_index:
            article, used_idx, unused = _resolve_markers(article, path_by_index, log)
            log(i18n.trs("pl_img_used", "   文章内已插入配图编号：{ids}",
                         ids=used_idx or i18n.trs("pl_none", "无")))
            if unused:
                log(i18n.trs("pl_img_unused",
                             "   未被引用的配图：{ids}，将在文末「配图」小节补充", ids=unused))
                zh = str(out_language).lower().startswith("zh")
                img_heading = "配图" if zh else "Images"
                extra = ["", "---", "", f"## {img_heading}", ""]
                for n in unused:
                    cand = next((c for c in images if c.index == n), None)
                    alt = ""
                    if cand:
                        alt = cand.caption or cand.context or (
                            f"视频 {frames.fmt_ts(cand.timestamp)} 处画面" if zh
                            else f"Frame at {frames.fmt_ts(cand.timestamp)}"
                        )
                    extra.append(f"![{alt}]({_md_url(path_by_index[n])})")
                    extra.append("")
                article = article.rstrip() + "\n" + "\n".join(extra)

        if cover_rel:
            article = _insert_cover(article, cover_rel)

        parts = [_build_frontmatter(info, title, cover_rel, source=source_name), "", article.strip(), ""]
        if cfg.include_transcript and used_llm:
            zh_out = str(out_language).lower().startswith("zh")
            sum_label = "原始语音转录" if zh_out else "Original transcript"
            parts += [
                "", "---", "", "<details>", f"<summary>{sum_label}</summary>", "",
                transcript, "", "</details>", "",
            ]
            log(i18n.trs("pl_transcript_appended", "已附加「原始语音转录」折叠块"))
        markdown = "\n".join(parts)
        md_path.write_text(markdown, encoding="utf-8")

        log(i18n.trs("pl_title_line", "文章标题 ：{t}", t=title))
        log(i18n.trs("pl_filename_line", "文件名   ：{t}", t=md_name))
        log(i18n.trs("pl_path_line", "保存位置 ：{t}", t=md_path))
        log(i18n.trs("pl_size_line", "文件大小 ：{t}", t=_fmt_size(md_path)))
        if assets_dir:
            log(i18n.trs("pl_assets_line", "配图目录 ：{t}（{n} 个文件）",
                         t=assets_dir, n=len(list(assets_dir.glob('*.jpg')))))

        if copy_video and cfg.keep_video and info.video_path and Path(info.video_path).exists():
            try:
                shutil.copy2(info.video_path, out_dir / Path(info.video_path).name)
                log(i18n.trs("pl_video_kept", "已保留视频：{t}", t=Path(info.video_path).name))
            except Exception as exc:  # noqa: BLE001
                log(i18n.trs("pl_video_keep_failed", "（提示）视频保留失败，已忽略：{e}", e=exc))

        return PipelineResult(
            md_path=md_path,
            title=title,
            transcript=transcript,
            markdown=markdown,
            info=info,
            used_llm=used_llm,
            elapsed=0.0,
            images=images,
            assets_dir=assets_dir,
            cover_path=(assets_dir / "cover.jpg") if (assets_dir and cover_rel) else None,
        )


# ----------------------------------------------------------------------------
def run_pipeline(
    url: str,
    cfg: AppConfig,
    on_progress: Optional[Callable[[int, int, str, int, int], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    local_video: Optional[Path] = None,
) -> PipelineResult:
    """执行完整流程，返回结果（含生成的 md 路径）。

    * 提供 ``local_video`` 时跳过下载，直接对本地已下载的视频做后续处理
      （抽音频 → 语音识别 → 截取配图 → 整理 → 导出）。
    * 否则按 ``url`` 下载后再走后续步骤。

    ``on_progress(overall_pct, step_pct, msg, step_index, step_total)``：
    * ``overall_pct``：跨步骤的总进度 0-100（上进度条）；
    * ``step_pct``：当前步骤内部进度 0-100，``-1`` 表示不定态/进行中（下进度条）；
    * ``msg``：当前动作说明；
    * ``step_index`` / ``step_total``：当前是第几个步骤 / 共几个步骤。
    """

    def log(msg: str = "") -> None:
        if on_log:
            on_log(msg)

    t_all = time.time()
    url = (url or "").strip()
    local_video = Path(local_video) if local_video else None
    if local_video is None and not url:
        raise PipelineError(i18n.trs("pe_no_input", "请输入视频链接（抖音 / B 站等），或选择本地视频文件。"))
    if local_video is None and not cfg.is_source_allowed(url):
        allowed = cfg.allowed_source_ids()
        names = i18n.trs("pl_list_sep", "、").join(
            i18n.tr(f"badge_{s}") for s in allowed if s in VIDEO_SOURCE_BY_ID)
        if names:
            raise PipelineError(i18n.trs(
                "pe_source_blocked",
                "该链接的来源不在已勾选的视频源白名单中（当前允许：{names}）。请在 setup 的「视频源」步骤勾选对应来源后重试。",
                names=names))
        raise PipelineError(i18n.trs(
            "pe_source_blocked_empty",
            "该链接的来源不在已勾选的视频源白名单中（白名单为空）。请在 setup 的「视频源」步骤勾选对应来源后重试。"))
    source_name = i18n.trs("src_local", "本地视频") if local_video is not None else downloader.detect_source(url)

    out_dir = cfg.resolved_output_dir
    tmp_dir = Path(tempfile.mkdtemp(prefix="dy2md_"))

    images_on = bool(cfg.use_images)
    vision_on = bool(images_on and cfg.use_vision and cfg.vision_ready())
    llm_ready = bool(cfg.use_llm and cfg.api_ready())
    mode = (getattr(cfg, "summary_mode", "blog") or "blog").lower()
    out_language = (getattr(cfg, "output_language", "en") or "en").lower()
    out_language_name = output_language_name(out_language)
    out_length = int(getattr(cfg, "article_length", DEFAULT_ARTICLE_LENGTH) or DEFAULT_ARTICLE_LENGTH)
    out_length_name = i18n.length_name(out_length)
    ted_site_transcript = (local_video is None and downloader._is_ted_url(url))

    first_title = i18n.tr("ps_read_local") if local_video is not None else i18n.tr("ps_download")
    asr_title = i18n.tr("ps_ted_subs") if ted_site_transcript else i18n.tr("ps_asr")
    cover_wanted = bool(cfg.download_cover)
    step_titles = [first_title, i18n.tr("ps_extract_audio"), asr_title]
    if images_on:
        step_titles.append(i18n.tr("ps_frames"))
    elif cover_wanted:
        step_titles.append(i18n.tr("ps_cover"))
    if llm_ready:
        step_titles.append(i18n.tr("ps_note" if mode == "note" else "ps_blog"))
    else:
        step_titles.append(i18n.tr("ps_format"))
    step_titles.append(i18n.tr("ps_export"))
    total_steps = len(step_titles)
    emit, step = _make_progress(on_progress, total_steps, log)

    # ---------------- 任务概览 ----------------
    log("=" * 62)
    log(i18n.trs("pl_task_start", "任务开始"))
    log("=" * 62)
    if local_video is not None:
        log(i18n.trs("pl_src_local", "来源       : 本地视频 {p}", p=local_video))
    else:
        log(i18n.trs("pl_input_url", "输入链接   : {url}", url=url))
        log(i18n.trs("pl_src_site", "来源站点   : {s}", s=source_name))
    log(i18n.trs("pl_outdir", "输出目录   : {d}", d=out_dir))
    log(i18n.trs("pl_asr_cfg", "ASR       : {m}  设备={dev}  语言={lang}",
                 m=cfg.asr_model, dev=cfg.asr_device, lang=cfg.asr_language))
    log(i18n.trs("pl_out_lang", "成稿语言   : {n}（{c}）", n=out_language_name, c=out_language))
    log(i18n.trs("pl_out_length", "成稿篇幅   : {n}（{c}/5）", n=out_length_name, c=out_length))
    provider_names = " / ".join(
        f"{p['name']}({p.get('model') or i18n.trs('pl_default_model', '默认')})"
        for p in cfg.effective_providers()
    )
    if llm_ready:
        mode_word = i18n.tr("mode_note") if mode == "note" else i18n.tr("mode_blog")
        log(i18n.trs("pl_text_ai", "文本整理   : {providers} · {mode}",
                     providers=provider_names or i18n.trs("pl_ai_generic", "大模型"), mode=mode_word))
    else:
        log(i18n.trs("pl_text_plain", "文本整理   : 未启用（仅输出原始转录）"))
    if images_on:
        suffix = (
            i18n.trs("pl_vision_on", "，视觉核对={m}",
                     m=cfg.primary_vision_model() or i18n.trs("pl_default_model", "默认"))
            if vision_on else i18n.trs("pl_vision_off", "，启发式筛选")
        )
        log(i18n.trs("pl_images_on", "配图       : 开启（最多 {n} 张{suffix}）",
                     n=cfg.max_images, suffix=suffix))
    else:
        log(i18n.trs("pl_images_off", "配图       : 关闭"))
    cover_state = (i18n.trs("pl_cover_state_on", "保存封面图（不计入配图张数）")
                   if cover_wanted else i18n.trs("pl_cover_state_off", "不保存"))
    log(i18n.trs("pl_cover_line", "封面       : {state}", state=cover_state))
    if local_video is None:
        if cfg.cookies_raw:
            log(i18n.trs("pl_cookie_raw", "Cookie    : 已提供（{n} 字符）", n=len(cfg.cookies_raw)))
        elif cfg.cookies_file:
            log(i18n.trs("pl_cookie_file", "Cookie    : 使用文件 {p}", p=cfg.cookies_file))
        elif (cfg.cookies_browser or "none") != "none":
            log(i18n.trs("pl_cookie_browser", "Cookie    : 读取浏览器 {b}", b=cfg.cookies_browser))
        else:
            log(i18n.trs("pl_cookie_none", "Cookie    : 未配置（抖音可能无法下载）"))
    ffmpeg = downloader.resolve_ffmpeg(cfg)
    log(i18n.trs("pl_ffmpeg", "ffmpeg    : {p}",
                 p=ffmpeg or i18n.trs("pl_ffmpeg_missing", "未找到可用版本！")))

    try:
        # ---------------- ① 获取视频（下载 或 读取本地） ----------------
        if local_video is not None:
            with step(i18n.tr("ps_read_local")):
                lp = Path(local_video)
                if not lp.is_file():
                    raise PipelineError(i18n.trs("pe_local_missing", "本地视频不存在：{p}", p=lp))
                log(i18n.trs("pl_local_file", "本地文件 ：{p}", p=lp))
                log(i18n.trs("pl_local_size", "文件大小 ：{s}", s=_fmt_size(lp)))
                dur = _probe_duration(lp, ffmpeg)
                if dur > 0:
                    log(i18n.trs("pl_duration", "时长     ：{v} 秒（{human}）",
                                 v=f"{dur:.1f}", human=_fmt_sec(dur)))
                else:
                    log(i18n.trs("pl_duration_unknown", "时长     ：未知（后续以实测音频时长为准）"))
                info = VideoInfo(
                    video_id="",
                    title=lp.stem,
                    author="",
                    description="",
                    duration=float(dur or 0.0),
                    webpage_url="",
                    thumbnail="",
                    upload_date="",
                    video_path=lp,
                )
                log(i18n.trs("pl_title", "标题     ：{t}", t=info.title))
        else:
            with step(i18n.tr("ps_download")):
                if downloader._is_ted_url(url):
                    engine = i18n.trs("eng_ted", "TED 页面解析 + HLS")
                elif downloader._is_douyin_url(url):
                    engine = i18n.trs("eng_douyin", "原生解析")
                else:
                    engine = "yt-dlp"
                log(i18n.trs("pl_link_type", "链接类型：{s}（{engine}）", s=source_name, engine=engine))
                last_bucket = {"v": 0}

                def dl_progress(p: float, s: str) -> None:
                    bucket = int(p // 10) * 10          # 每 10% 记一条日志
                    if bucket > last_bucket["v"]:
                        last_bucket["v"] = bucket
                        log(i18n.trs("pl_dl_bucket", "  下载进度：{b}%", b=bucket))
                    emit(p, s)

                info = downloader.download_video(url, cfg, tmp_dir, on_progress=dl_progress, on_log=log)

                log(i18n.trs("pl_video_id", "作品 ID  ：{v}", v=info.video_id))
                log(i18n.trs("pl_title", "标题     ：{t}",
                             t=info.title or i18n.trs("pl_none_val", "(无)")))
                log(i18n.trs("pl_author", "作者     ：{a}",
                             a=info.author or i18n.trs("pl_unknown_val", "(未知)")))
                log(i18n.trs("pl_duration", "时长     ：{v} 秒（{human}）",
                             v=f"{info.duration:.1f}", human=_fmt_sec(info.duration)))
                if info.upload_date:
                    log(i18n.trs("pl_upload_date", "发布日期 ：{d}", d=info.upload_date))
                if info.description:
                    desc = info.description.replace("\n", " ")
                    log(i18n.trs("pl_desc", "作品文案 ：{t}",
                                 t=f"{desc[:120]}{'…' if len(desc) > 120 else ''}"))
                log(i18n.trs("pl_video_file", "视频文件 ：{f}",
                             f=f"{info.video_path.name}（{_fmt_size(info.video_path)}）"))
                if info.thumbnail:
                    log(i18n.trs("pl_cover_url", "封面地址 ：{u}", u=info.thumbnail))

        # ---------------- ② 抽音频 ----------------
        with step(i18n.tr("ps_extract_audio")):
            log(i18n.trs("pl_ffmpeg2", "ffmpeg   ：{f}", f=ffmpeg))
            log(i18n.trs("pl_audio_params", "参数     ：单声道 / 16kHz / PCM 16bit"))
            wav_path = tmp_dir / "audio.wav"
            downloader.extract_audio(info.video_path, wav_path, cfg)
            measured = 0.0
            try:
                measured = wav_path.stat().st_size / 32000.0
            except Exception:  # noqa: BLE001
                pass
            log(i18n.trs("pl_audio_file", "音频文件 ：audio.wav（{s}，实测时长约 {human}）",
                         s=_fmt_size(wav_path), human=_fmt_sec(measured)))
            if info.duration and measured and abs(measured - info.duration) > max(5.0, info.duration * 0.15):
                log(i18n.trs("pl_audio_mismatch", "⚠ 实测音频时长（{m}）与页面元数据（{d}）差异较大，",
                             m=_fmt_sec(measured), d=_fmt_sec(info.duration)))
                log(i18n.trs("pl_audio_mismatch2", "  可能元数据不准或下载不完整；后续统一以实测时长为准。"))
                info.duration = measured

        # ---------------- ③ 语音识别 / 站点字幕 ----------------
        with step(asr_title):
            site_transcript = (info.transcript or "").strip()
            if site_transcript:
                transcript = site_transcript
                log(i18n.trs("pl_subs_source", "来源     ：站点自带官方字幕（无需本地识别）"))
                log(i18n.trs("pl_subs_chars", "字幕字数 ：{n} 字", n=len(transcript)))
                preview = transcript[:100].replace("\n", " ")
                log(i18n.trs("pl_subs_preview", "字幕预览 ：{t}",
                             t=f"{preview}{'…' if len(transcript) > 100 else ''}"))
            else:
                log(i18n.trs("pl_asr_model", "模型     ：{m}", m=cfg.asr_model))
                log(i18n.trs("pl_asr_note", "说明     ：首次使用会自动下载模型（约 1GB），之后离线可用"))
                asr = SenseVoiceASR(cfg.asr_model, cfg.asr_device, cfg.asr_language)
                t_asr = time.time()
                transcript = asr.transcribe(wav_path, on_log=log)
                asr_cost = time.time() - t_asr
                if not transcript.strip():
                    raise PipelineError(i18n.trs("pe_no_speech", "未识别到语音内容，该作品可能是纯音乐/无口播。"))
                log(i18n.trs("pl_asr_chars", "识别字数 ：{n} 字", n=len(transcript)))
                if info.duration:
                    log(i18n.trs("pl_asr_time", "识别耗时 ：{c:.1f}s（音频 {d:.0f}s，实时率约 {r:.1f}x）",
                                 c=asr_cost, d=info.duration,
                                 r=info.duration / max(asr_cost, 0.01)))
                preview = transcript[:100].replace("\n", " ")
                log(i18n.trs("pl_asr_preview", "转录预览 ：{t}",
                             t=f"{preview}{'…' if len(transcript) > 100 else ''}"))

        # ---------------- ④ 截取配图 ----------------
        images: List[frames.FrameCandidate] = []
        cover_candidate_path: Optional[Path] = None
        if images_on:
            with step(i18n.tr("ps_frames")):
                want = min(8, max(cfg.max_images + 2, cfg.max_images))
                log(i18n.trs("pl_frames_target", "目标配图 ：{n} 张（先取 {w} 张候选再筛）",
                             n=cfg.max_images, w=want))
                try:
                    candidates = frames.extract_candidates(
                        info.video_path,
                        transcript=transcript,
                        duration=info.duration,
                        want=want,
                        on_log=log,
                        on_progress=lambda p: emit(p, i18n.tr("ps_frames")),
                    )
                except frames.FrameError as exc:
                    candidates = []
                    log(i18n.trs("pl_frames_fail", "⚠ 抽帧失败，跳过配图：{e}", e=exc))

                if candidates and vision_on:
                    emit(-1, i18n.tr("ps_review_images"))
                    try:
                        client = llm.build_client(cfg)
                        if client is None:
                            raise llm.LLMError(i18n.trs("pe_no_api_short", "未配置可用的大模型 API Key"))
                        client.judge_images(candidates, on_log=log)
                        for c in candidates:
                            flag = (i18n.trs("pl_usable", "（可用）") if c.useful
                                    else i18n.trs("pl_not_recommended", "（不推荐）"))
                            log(i18n.trs("pl_vision_item", "   #{i} {ts} 评分 {score:.0f}{flag}：{cap}",
                                         i=c.index, ts=frames.fmt_ts(c.timestamp),
                                         score=c.vision_score, flag=flag,
                                         cap=c.caption or i18n.trs("pl_no_caption", "(无描述)")))
                    except llm.LLMError as exc:
                        log(i18n.trs("pl_vision_fail", "⚠ 视觉核对失败，改用启发式筛选：{e}", e=exc))

                if candidates:
                    good = [c for c in candidates if c.useful and c.vision_score >= 5]
                    if len(good) >= 2:
                        good.sort(key=lambda c: -c.vision_score)
                        chosen = good[: cfg.max_images]
                        log(i18n.trs("pl_vision_pick", "   视觉筛选：{n} 张合格，取前 {k} 张",
                                     n=len(good), k=len(chosen)))
                    else:
                        chosen = _pick_evenly(candidates, cfg.max_images)
                        log(i18n.trs("pl_vision_fallback", "   视觉筛选可用画面不足，改用启发式结果，取 {k} 张",
                                     k=len(chosen)))
                    chosen.sort(key=lambda c: c.timestamp)
                    for i, c in enumerate(chosen, start=1):
                        c.index = i
                    images = chosen
                    log(i18n.trs("pl_frames_final", "   最终采用 {n} 张：{list}",
                                 n=len(images),
                                 list=i18n.trs("pl_list_sep", "、").join(
                                     frames.fmt_ts(c.timestamp) for c in images)))

                if cover_wanted:
                    cover_candidate_path = _grab_cover(info, tmp_dir, log)
        elif cover_wanted:
            with step(i18n.tr("ps_cover")):
                cover_candidate_path = _grab_cover(info, tmp_dir, log)

        # ---------------- ⑤⑥ 整理文本 + 导出 Markdown ----------------
        result = _organize_and_export(
            cfg=cfg,
            info=info,
            transcript=transcript,
            images=images,
            cover_candidate_path=cover_candidate_path,
            source_name=source_name,
            log=log,
            emit=emit,
            step=step,
            llm_ready=llm_ready,
            mode=mode,
            out_language=out_language,
            out_language_name=out_language_name,
            out_length=out_length,
            out_length_name=out_length_name,
            copy_video=(local_video is None),
        )

        # ---------------- 写入缓存（供「用上次结果重生成」复用） ----------------
        try:
            cache.save_cache(
                cfg,
                info=info,
                transcript=transcript,
                images=images,
                cover_path=cover_candidate_path,
                source_name=source_name,
                created=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            log(i18n.trs("pl_cache_saved",
                         "已缓存本次中间结果（转录 + 配图），下次可直接跳过下载/识别重生成"))
        except Exception as exc:  # noqa: BLE001
            log(i18n.trs("pl_cache_failed", "（提示）中间结果缓存失败，已忽略：{e}", e=exc))

        # ---------------- 汇总 ----------------
        elapsed = time.time() - t_all
        emit(100, i18n.tr("ps_finished"))
        log("")
        log("=" * 62)
        log(i18n.trs("pl_all_done", "全部完成 🎉  总耗时 {t:.1f}s（{human}）",
                     t=elapsed, human=_fmt_sec(elapsed)))
        log(i18n.trs("pl_used_llm", "是否使用大模型：{yn}",
                     yn=i18n.trs("pl_yes", "是") if result.used_llm else i18n.trs("pl_no", "否")))
        log(i18n.trs("pl_total_chars", "转录总字数：{n} 字", n=len(transcript)))
        log(i18n.trs("pl_img_count", "配图数量：{n} 张{suffix}", n=len(images),
                     suffix=i18n.trs("pl_cover_suffix", "（含封面）") if cover_candidate_path else ""))
        log(i18n.trs("pl_output", "产物：{p}", p=result.md_path))
        log("=" * 62)

        result.elapsed = elapsed
        return result
    except Exception:
        log("")
        log(i18n.trs("pl_aborted", "✘ 任务中断，已耗时 {t:.1f}s", t=time.time() - t_all))
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ----------------------------------------------------------------------------
def regenerate_from_cache(
    cfg: AppConfig,
    on_progress: Optional[Callable[[int, int, str, int, int], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> PipelineResult:
    """用上次运行缓存里的转录稿与配图，直接重跑「整理 + 导出」两步。

    跳过下载 / 抽音频 / 语音识别 / 截取配图等耗时环节，按当前设置
    （语言、篇幅、笔记/博客模式等）重新生成成稿。
    """
    def log(msg: str = "") -> None:
        if on_log:
            on_log(msg)

    t_all = time.time()
    loaded = cache.load_cache(cfg)     # 无缓存/损坏时抛异常，由调用方处理
    info = loaded.info
    transcript = loaded.transcript
    images = loaded.images
    cover_candidate_path = loaded.cover_path
    source_name = loaded.source_name or i18n.trs("src_web", "网页视频")

    # 配图按「当前设置」决定：本次关掉配图则丢弃缓存里的图；本次想开但缓存没有则如实提示
    if not cfg.use_images:
        images = []
    elif not images:
        log(i18n.trs("pl_regen_no_images", "（提示）上次运行未保存配图，本次无法补图；如需配图请完整重新运行一次"))
    # 封面是独立选项：关掉就不带，开着则复用缓存里的封面
    if not cfg.download_cover:
        cover_candidate_path = None

    llm_ready = bool(cfg.use_llm and cfg.api_ready())
    mode = (getattr(cfg, "summary_mode", "blog") or "blog").lower()
    out_language = (getattr(cfg, "output_language", "en") or "en").lower()
    out_language_name = output_language_name(out_language)
    out_length = int(getattr(cfg, "article_length", DEFAULT_ARTICLE_LENGTH) or DEFAULT_ARTICLE_LENGTH)
    out_length_name = i18n.length_name(out_length)

    step_titles = [i18n.tr("ps_read_cache")]
    step_titles.append(
        i18n.tr("ps_note" if mode == "note" else "ps_blog")
        if llm_ready else i18n.tr("ps_format")
    )
    step_titles.append(i18n.tr("ps_export"))
    total_steps = len(step_titles)
    emit, step = _make_progress(on_progress, total_steps, log)

    log("=" * 62)
    log(i18n.trs("pl_regen_title", "复用上次结果重生成"))
    log("=" * 62)
    log(i18n.trs("pl_regen_note", "说明     ：跳过下载 / 语音识别 / 抽帧，直接用缓存里的转录稿与配图重新成稿"))
    log(i18n.trs("pl_regen_lang", "成稿语言 ：{n}（{c}）", n=out_language_name, c=out_language))
    log(i18n.trs("pl_regen_length", "成稿篇幅 ：{n}（{c}/5）", n=out_length_name, c=out_length))
    provider_names = " / ".join(
        f"{p['name']}({p.get('model') or i18n.trs('pl_default_model', '默认')})"
        for p in cfg.effective_providers()
    )
    if llm_ready:
        mode_word = i18n.tr("mode_note") if mode == "note" else i18n.tr("mode_blog")
        log(i18n.trs("pl_regen_ai", "文本整理 ：{providers} · {mode}",
                     providers=provider_names or i18n.trs("pl_ai_generic", "大模型"), mode=mode_word))
    else:
        log(i18n.trs("pl_regen_plain", "文本整理 ：未启用（仅输出原始转录）"))

    with step(i18n.tr("ps_read_cache")):
        log(i18n.trs("pl_cache_dir", "缓存目录 ：{d}", d=loaded.dir))
        if loaded.created:
            log(i18n.trs("pl_cache_time", "缓存时间 ：{t}", t=loaded.created))
        log(i18n.trs("pl_title", "标题     ：{t}", t=info.title or i18n.trs("pl_none_val", "(无)")))
        if info.author:
            log(i18n.trs("pl_author", "作者     ：{a}", a=info.author))
        log(i18n.trs("pl_regen_chars", "转录字数 ：{n} 字", n=len(transcript)))
        if not transcript.strip():
            raise PipelineError(i18n.trs("pe_empty_cache", "缓存里的转录稿为空，无法重生成。"))
        log(i18n.trs("pl_img_count", "配图数量：{n} 张{suffix}", n=len(images),
                     suffix=i18n.trs("pl_cover_suffix", "（含封面）") if cover_candidate_path else ""))

    try:
        result = _organize_and_export(
            cfg=cfg,
            info=info,
            transcript=transcript,
            images=images,
            cover_candidate_path=cover_candidate_path,
            source_name=source_name,
            log=log,
            emit=emit,
            step=step,
            llm_ready=llm_ready,
            mode=mode,
            out_language=out_language,
            out_language_name=out_language_name,
            out_length=out_length,
            out_length_name=out_length_name,
            copy_video=False,
        )
    except Exception:
        log("")
        log(i18n.trs("pl_regen_aborted", "✘ 重生成中断，已耗时 {t:.1f}s", t=time.time() - t_all))
        raise

    elapsed = time.time() - t_all
    emit(100, i18n.tr("ps_finished"))
    log("")
    log("=" * 62)
    log(i18n.trs("pl_regen_done", "重生成完成 🎉  总耗时 {t:.1f}s（{human}）",
                 t=elapsed, human=_fmt_sec(elapsed)))
    log(i18n.trs("pl_used_llm", "是否使用大模型：{yn}",
                 yn=i18n.trs("pl_yes", "是") if result.used_llm else i18n.trs("pl_no", "否")))
    log(i18n.trs("pl_output", "产物：{p}", p=result.md_path))
    log("=" * 62)

    result.elapsed = elapsed
    return result
