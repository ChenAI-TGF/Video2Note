"""应用配置：读写根目录下的 config.json，提供默认值。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

# app/core/config.py -> app/core -> app -> 项目根
ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT_DIR / "config.json"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "output"

# 输出语言：(代码, 显示名)。提示词与界面共用，避免两处不一致。
OUTPUT_LANGUAGES = [
    ("zh", "中文"),
    ("en", "English"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("es", "Español"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("ru", "Русский"),
]
OUTPUT_LANGUAGE_NAMES = {code: name for code, name in OUTPUT_LANGUAGES}
DEFAULT_OUTPUT_LANGUAGE = "en"


def output_language_name(code: str) -> str:
    """把语言代码转成给模型的自然语言名；未知代码回退到默认语言。"""
    return OUTPUT_LANGUAGE_NAMES.get((code or "").strip().lower(),
                                     OUTPUT_LANGUAGE_NAMES[DEFAULT_OUTPUT_LANGUAGE])


# 篇幅等级：(等级, 显示名)，1 最短 → 5 最长。默认「中等偏多」= 4（与旧行为一致）。
ARTICLE_LENGTHS = [
    (1, "极简"),
    (2, "精简"),
    (3, "中等"),
    (4, "中等偏多"),
    (5, "详尽"),
]
ARTICLE_LENGTH_MIN = ARTICLE_LENGTHS[0][0]
ARTICLE_LENGTH_MAX = ARTICLE_LENGTHS[-1][0]
DEFAULT_ARTICLE_LENGTH = 4
ARTICLE_LENGTH_NAMES = {level: name for level, name in ARTICLE_LENGTHS}


def article_length_name(level) -> str:
    """把篇幅等级转成显示名；非法值回退到默认等级。"""
    try:
        n = int(level)
    except (TypeError, ValueError):
        n = DEFAULT_ARTICLE_LENGTH
    return ARTICLE_LENGTH_NAMES.get(n, ARTICLE_LENGTH_NAMES[DEFAULT_ARTICLE_LENGTH])


# ----------------------------------------------------------------------
# 视频源清单（供 setup 勾选 + 主程序白名单校验）
#   group = "required"：必须配置 Cookie 才能稳定获取
#   group = "optional"：可以不配（公开可下 / 有官方字幕等兜底）
#   domains：命中其中任一域名即视为该来源
# ----------------------------------------------------------------------
VIDEO_SOURCES = [
    {"id": "douyin", "name": "抖音", "group": "required",
     "domains": ["douyin.com", "iesdouyin.com"],
     "note": "必须登录 Cookie，否则无法稳定获取"},
    {"id": "tiktok", "name": "TikTok", "group": "required",
     "domains": ["tiktok.com"],
     "note": "多数内容需要登录 Cookie"},
    {"id": "bilibili", "name": "哔哩哔哩", "group": "optional",
     "domains": ["bilibili.com", "b23.tv"],
     "note": "公开视频无需 Cookie"},
    {"id": "kuaishou", "name": "快手", "group": "optional",
     "domains": ["kuaishou.com"],
     "note": "公开视频无需 Cookie"},
    {"id": "xiaohongshu", "name": "小红书", "group": "optional",
     "domains": ["xiaohongshu.com", "xhslink.com"],
     "note": "部分内容需要登录"},
    {"id": "weibo", "name": "微博", "group": "optional",
     "domains": ["weibo.com"],
     "note": "公开视频无需 Cookie"},
    {"id": "youtube", "name": "YouTube", "group": "optional",
     "domains": ["youtube.com", "youtu.be"],
     "note": "公开视频无需 Cookie"},
    {"id": "ted", "name": "TED", "group": "optional",
     "domains": ["ted.com"],
     "note": "带官方字幕兜底，无需 Cookie"},
]
VIDEO_SOURCE_BY_ID = {s["id"]: s for s in VIDEO_SOURCES}
VIDEO_SOURCE_IDS = [s["id"] for s in VIDEO_SOURCES]


def source_id_for_host(host: str) -> str:
    """把域名映射到视频源 id；未收录的返回空串。"""
    host = (host or "").lower()
    if not host:
        return ""
    for src in VIDEO_SOURCES:
        for d in src["domains"]:
            if host == d or host.endswith("." + d):
                return src["id"]
    return ""


def source_ids_for_url(url: str) -> List[str]:
    """从链接中解析出视频源 id（可能为多个，取域名匹配）。"""
    host = (urlparse(url or "").hostname or "").lower()
    sid = source_id_for_host(host)
    return [sid] if sid else []


# ----------------------------------------------------------------------
# API 供应商目录（OpenAI 兼容协议；供 setup 选择，可配置多个做兜底）
#   base_url：OpenAI 兼容接口根地址（openai 库的 base_url）
#   vision=True：该厂商有「识图 / 图像理解」模型，可用于核对配图
#   纯出图（文生图）的接口不收录
# ----------------------------------------------------------------------
API_PROVIDER_CATALOG = [
    {"id": "deepseek", "name": "DeepSeek", "recommended": True,
     "base_url": "https://api.deepseek.com",
     "models": ["deepseek-chat", "deepseek-reasoner"],
     "vision": True, "vision_models": ["deepseek-flash", "deepseek-v4-flash-vision-exp"],
     "signup": "https://platform.deepseek.com"},
    {"id": "openai", "name": "OpenAI",
     "base_url": "https://api.openai.com/v1",
     "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1-mini"],
     "vision": True, "vision_models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"],
     "signup": "https://platform.openai.com"},
    {"id": "kimi", "name": "Kimi / Moonshot",
     "base_url": "https://api.moonshot.cn/v1",
     "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k", "kimi-latest"],
     "vision": True,
     "vision_models": ["moonshot-v1-8k-vision-preview", "moonshot-v1-32k-vision-preview", "kimi-latest"],
     "signup": "https://platform.moonshot.cn"},
    {"id": "zhipu", "name": "智谱 AI (GLM)",
     "base_url": "https://open.bigmodel.cn/api/paas/v4",
     "models": ["glm-4-plus", "glm-4-air", "glm-4-flash"],
     "vision": True, "vision_models": ["glm-4v-plus", "glm-4v-flash"],
     "signup": "https://open.bigmodel.cn"},
    {"id": "dashscope", "name": "通义千问 DashScope",
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
     "models": ["qwen-max", "qwen-plus", "qwen-turbo"],
     "vision": True, "vision_models": ["qwen-vl-max", "qwen-vl-plus"],
     "signup": "https://bailian.console.aliyun.com"},
    {"id": "volcengine", "name": "火山引擎 方舟（豆包）",
     "base_url": "https://ark.cn-beijing.volces.com/api/v3",
     "models": ["doubao-pro-32k", "doubao-pro-128k"],
     "vision": True, "vision_models": ["doubao-vision-pro-32k"],
     "signup": "https://console.volcengine.com/ark"},
    {"id": "siliconflow", "name": "硅基流动 SiliconFlow",
     "base_url": "https://api.siliconflow.cn/v1",
     "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
     "vision": True, "vision_models": ["Qwen/Qwen2.5-VL-72B-Instruct"],
     "signup": "https://cloud.siliconflow.cn"},
    {"id": "openrouter", "name": "OpenRouter",
     "base_url": "https://openrouter.ai/api/v1",
     "models": ["deepseek/deepseek-chat", "openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet"],
     "vision": True,
     "vision_models": ["openai/gpt-4o", "anthropic/claude-3.5-sonnet", "google/gemini-2.0-flash-001"],
     "signup": "https://openrouter.ai"},
    {"id": "groq", "name": "Groq",
     "base_url": "https://api.groq.com/openai/v1",
     "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
     "vision": True, "vision_models": ["llama-3.2-11b-vision-preview"],
     "signup": "https://console.groq.com"},
    {"id": "baidu", "name": "百度智能云 千帆",
     "base_url": "https://qianfan.baidubce.com/v2",
     "models": ["ernie-4.0-turbo-8k", "ernie-3.5-8k", "ernie-speed-128k"],
     "vision": True, "vision_models": ["ernie-4.0-turbo-8k-vision-preview"],
     "signup": "https://console.bce.baidu.com/qianfan"},
]
API_PROVIDER_BY_ID = {p["id"]: p for p in API_PROVIDER_CATALOG}


def new_provider_entry(provider_id: str) -> Dict[str, Any]:
    """按目录生成一条默认的供应商配置。"""
    meta = API_PROVIDER_BY_ID.get(provider_id) or {}
    models = list(meta.get("models") or [])
    vision_models = list(meta.get("vision_models") or [])
    return {
        "id": provider_id,
        "name": meta.get("name") or provider_id,
        "base_url": meta.get("base_url") or "",
        "api_key": "",
        "model": models[0] if models else "",
        "vision": bool(meta.get("vision")),
        "vision_model": vision_models[0] if vision_models else "",
        "enabled": True,
    }


@dataclass
class AppConfig:
    # ---- 界面 ----
    ui_language: str = "en"        # 界面语言（见 app/ui/i18n.py 的 UI_LANGUAGES），默认英文

    # ---- DeepSeek ----
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_vision_model: str = "deepseek-flash"   # 支持识图的模型，用于核对配图
    use_llm: bool = True
    summary_mode: str = "note"     # 固定为「笔记模式」（保留字段以兼容旧配置）

    # ---- 配图 ----
    use_images: bool = True        # 是否从视频里自动截取配图
    use_vision: bool = True        # 是否用视觉模型核对画面并生成图注
    max_images: int = 4            # 每篇博客最多几张配图
    download_cover: bool = True    # 是否下载抖音封面图作为博客封面

    # ---- 输出 ----
    output_dir: str = ""          # 为空则用 项目根/output
    output_language: str = "en"   # 成稿语言（见 OUTPUT_LANGUAGES），默认英文
    article_length: int = 4       # 成稿篇幅等级 1-5（见 ARTICLE_LENGTHS），默认 4 = 中等偏多
    keep_video: bool = False      # 是否同时保留下载的视频
    include_transcript: bool = False  # 是否在 md 末尾附上原始转录

    # ---- 下载 ----
    cookies_browser: str = "edge"  # edge / chrome / firefox / none
    cookies_raw: str = ""          # 直接粘贴的 Cookie 串（k=v; k=v），优先级最高
    cookies_file: str = ""         # Netscape 格式 cookies.txt
    ffmpeg_path: str = ""          # 可选：手动指定 ffmpeg.exe（PATH 里的可能是精简版）

    # ---- 语音识别 ----
    asr_model: str = "iic/SenseVoiceSmall"
    asr_device: str = "cpu"        # cpu / cuda:0
    asr_language: str = "auto"

    # ---- 视频源白名单（由 setup 勾选）----
    # 存放 VIDEO_SOURCES 里的 id；为空 = 全部允许（向后兼容）
    enabled_sources: List[str] = field(default_factory=list)

    # ---- 多 API 供应商（按列表顺序作为兜底优先级）----
    # 每项：{id,name,base_url,api_key,model,vision,vision_model,enabled}
    api_providers: List[Dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    @property
    def resolved_output_dir(self) -> Path:
        """返回有效的输出目录（自动创建）。"""
        path = Path(self.output_dir).expanduser() if self.output_dir else DEFAULT_OUTPUT_DIR
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ---- 供应商 / 白名单辅助 ----
    def effective_providers(self) -> List[Dict[str, Any]]:
        """返回「已启用且有 Key」的供应商（按优先级）；列表为空时回退到旧的 DeepSeek 字段。"""
        out: List[Dict[str, Any]] = []
        for p in (self.api_providers or []):
            if not isinstance(p, dict):
                continue
            if not p.get("enabled", True):
                continue
            key = str(p.get("api_key") or "").strip()
            if not key:
                continue
            pid = str(p.get("id") or "openai")
            meta = API_PROVIDER_BY_ID.get(pid) or {}
            out.append({
                "id": pid,
                "name": str(p.get("name") or meta.get("name") or pid),
                "base_url": str(p.get("base_url") or meta.get("base_url") or ""),
                "api_key": key,
                "model": str(p.get("model") or ""),
                "vision": bool(p.get("vision", meta.get("vision", False))),
                "vision_model": str(p.get("vision_model") or ""),
            })
        if not out and (self.deepseek_api_key or "").strip():
            # 向后兼容：旧的单供应商配置
            out.append({
                "id": "deepseek",
                "name": "DeepSeek",
                "base_url": self.deepseek_base_url or "https://api.deepseek.com",
                "api_key": self.deepseek_api_key.strip(),
                "model": self.deepseek_model or "deepseek-chat",
                "vision": True,
                "vision_model": self.deepseek_vision_model or "deepseek-flash",
            })
        return out

    def api_ready(self) -> bool:
        """是否至少有一个可用的大模型供应商。"""
        return bool(self.effective_providers())

    def vision_ready(self) -> bool:
        """是否有支持识图的供应商可用。"""
        return any(p.get("vision") for p in self.effective_providers())

    def primary_vision_model(self) -> str:
        for p in self.effective_providers():
            if p.get("vision") and p.get("vision_model"):
                return p["vision_model"]
        return ""

    def allowed_source_ids(self) -> List[str]:
        """返回白名单里的视频源 id；为空表示不限制。"""
        return [s for s in (self.enabled_sources or []) if s in VIDEO_SOURCE_BY_ID]

    def is_source_allowed(self, url: str) -> bool:
        """判断链接来源是否在白名单内；白名单为空时一律允许。"""
        allowed = self.allowed_source_ids()
        if not allowed:
            return True
        host = (urlparse(url or "").hostname or "").lower()
        sid = source_id_for_host(host)
        if not sid:
            # 未收录的来源：白名单模式下默认不允许
            return False
        return sid in allowed

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls) -> "AppConfig":
        data: Dict[str, Any] = {}
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known and v is not None}
        return cls(**clean)
