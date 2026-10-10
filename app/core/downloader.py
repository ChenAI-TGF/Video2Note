"""视频下载。

两条路径
-------
1. **原生路径（默认，抖音链接走这里）**：读取移动端分享页
   ``https://www.iesdouyin.com/share/video/{id}/`` 内嵌的 ``_ROUTER_DATA``，
   拿到作品信息与播放地址后直接下载，并把 ``playwm``（带水印）替换成 ``play``（无水印）。
   yt-dlp 的抖音提取器依赖需要 ``a_bogus`` 签名的 ``aweme/detail`` 接口，现已经常失效，
   因此不再作为抖音的首选路径。
2. **yt-dlp 兜底**：非抖音链接（如**哔哩哔哩**、YouTube 等）交给 yt-dlp 处理。

关键点
-----
* 抖音需要登录 Cookie 才能稳定获取数据；Cookie 失效会直接给出中文提示。
* 支持 ``v.douyin.com`` 短链（自动跟随跳转解析出作品 ID）。
* 其它站点（B 站等）默认不带 Cookie（公开视频无需登录）；若需要登录态，
  请在「设置 → Cookie 文件」指定一个 Netscape 格式的 cookies.txt。
* 图文类作品没有音频流，无法做 ASR，会明确报错。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shutil
import string
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional
from urllib.parse import urlparse

from ..ui import i18n

VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".m4v", ".ts"}

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


class DownloadError(RuntimeError):
    """下载/解析阶段的可读错误。"""


@dataclass
class VideoInfo:
    video_id: str = ""
    title: str = ""
    author: str = ""
    description: str = ""
    duration: float = 0.0
    webpage_url: str = ""
    thumbnail: str = ""
    upload_date: str = ""
    video_path: Optional[Path] = None
    transcript: str = ""            # 站点自带字幕（目前仅 TED 使用）；非空则跳过本地 ASR


# ----------------------------------------------------------------------------
# ffmpeg 定位：PATH 里可能是一个「精简版」ffmpeg（只有视频编解码，没有 aac/pcm），
# 直接用会导致抽音频静默失败。这里按优先级探测并验证，挑出真正可用的那个。
# ----------------------------------------------------------------------------
_FFMPEG_CACHE: dict = {"searched": False, "path": None}

_COMMON_FFMPEG = [
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
    os.path.expandvars(r"%USERPROFILE%\scoop\shims\ffmpeg.exe"),
]


def _is_usable_ffmpeg(path: str) -> bool:
    """完整版 ffmpeg 才带 aac 与 pcm_s16le 解码器。"""
    try:
        proc = subprocess.run(
            [path, "-hide_banner", "-decoders"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=20,
        )
    except Exception:  # noqa: BLE001
        return False
    out = proc.stdout or ""
    return ("aac" in out) and ("pcm_s16le" in out)


def _iter_ffmpeg_candidates(cfg=None) -> Iterator[str]:
    """按优先级产出候选 ffmpeg 路径（生成器，命中即停，避免无谓扫描）。"""
    if cfg is not None and getattr(cfg, "ffmpeg_path", ""):
        p = Path(str(cfg.ffmpeg_path)).expanduser()
        yield str(p)
        # 用户可能填的是目录
        if p.is_dir():
            yield str(p / "ffmpeg.exe")

    which = shutil.which("ffmpeg")
    if which:
        yield which

    for p in _COMMON_FFMPEG:
        if p:
            yield p

    # 各盘符根目录下常见的「解压版」目录，例如 F:\ffmpeg-8.0-essentials_build\...\bin\ffmpeg.exe
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.exists(drive):
            continue
        for pattern in ("ffmpeg*\\bin\\ffmpeg.exe", "ffmpeg*\\*\\bin\\ffmpeg.exe"):
            try:
                for found in Path(drive).glob(pattern):
                    yield str(found)
            except Exception:  # noqa: BLE001
                pass


def resolve_ffmpeg(cfg=None, force: bool = False) -> Optional[str]:
    """返回一个可用的 ffmpeg 可执行文件路径；找不到返回 None。结果会缓存。"""
    if not force and _FFMPEG_CACHE["searched"]:
        return _FFMPEG_CACHE["path"]
    chosen = None
    for cand in _iter_ffmpeg_candidates(cfg):
        try:
            if Path(cand).is_file() and _is_usable_ffmpeg(cand):
                chosen = cand
                break
        except Exception:  # noqa: BLE001
            continue
    _FFMPEG_CACHE["searched"] = True
    _FFMPEG_CACHE["path"] = chosen
    return chosen


def write_netscape_cookies(cookie_str: str, path: Path, domain: str = ".douyin.com") -> Path:
    """把浏览器复制出来的 ``k=v; k=v`` Cookie 串转成 Netscape 格式 cookies.txt。

    yt-dlp 只认 Netscape 格式，所以需要转换。过期时间设为远期，避免被当成
    会话 Cookie 而丢弃。
    """
    far_future = "4102444800"  # 2100-01-01
    lines = [
        "# Netscape HTTP Cookie File",
        i18n.trs("dl_cookies_header",
                 "# 由「抖音短视频转Markdown」自动生成，请勿外传（等同账号密码）"),
        "",
    ]
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip().replace("\t", "")
        value = value.strip().replace("\t", "").replace("\r", "").replace("\n", "")
        if not name:
            continue
        # 字段：domain  include_subdomains  path  secure  expires  name  value
        lines.append("\t".join([domain, "TRUE", "/", "TRUE", far_future, name, value]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _apply_cookies(opts: dict, cfg, work_dir: Path,
                   allow_raw: bool = True, allow_browser: bool = True) -> None:
    """按配置注入 Cookie。

    优先级：直接粘贴的 Cookie 串 > cookies.txt 文件 > 浏览器登录态。

    对非抖音站点会传入 ``allow_raw=False, allow_browser=False``：
    因为「直接粘贴的 Cookie 串 / 浏览器登录态」通常都是抖音的，对其它站点无意义，
    而且读取浏览器 Cookie 常因数据库被占用而失败；此时只使用用户显式指定的 cookies.txt。
    """
    raw = (getattr(cfg, "cookies_raw", "") or "").strip()
    if raw and allow_raw:
        cookie_file = Path(work_dir) / "cookies.txt"
        write_netscape_cookies(raw, cookie_file)
        opts["cookiefile"] = str(cookie_file)
        return

    if getattr(cfg, "cookies_file", ""):
        path = Path(cfg.cookies_file).expanduser()
        if path.exists():
            opts["cookiefile"] = str(path)
            return

    browser = (getattr(cfg, "cookies_browser", "") or "").strip().lower()
    if browser and browser != "none" and allow_browser:
        # (浏览器名, 配置文件, 密钥环, 容器)
        opts["cookiesfrombrowser"] = (browser, None, None, None)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"


def _friendly_error(msg: str) -> str:
    low = msg.lower()
    if "could not copy" in low and "cookie" in low:
        return i18n.trs(
            "de_cookie_busy",
            "读取浏览器 Cookie 失败：浏览器正在运行，Cookie 数据库被占用。\n"
            "可关闭该浏览器后重试，或改用「Cookie 字符串 / cookies.txt」方式。\n\n"
            "原始错误：{msg}", msg=msg)
    if "cookies" in low or "sign in" in low or "login" in low:
        return i18n.trs(
            "de_need_login",
            "该站点要求登录校验（Cookie 失效或未提供）。\n"
            "解决办法：在「设置」里粘贴有效的 Cookie 字符串，或指定 cookies.txt；"
            "也可以在浏览器登录该站点后再试。\n\n"
            "原始错误：{msg}", msg=msg)
    if "unsupported url" in low:
        return i18n.trs(
            "de_bad_url",
            "无法识别的链接。支持抖音、哔哩哔哩等常见站点的作品/视频链接"
            "（抖音如 https://v.douyin.com/xxxx/，B 站如 https://www.bilibili.com/video/BVxxxxxx/）。\n\n"
            "原始错误：{msg}", msg=msg)
    if "unable to download webpage" in low or "timed out" in low:
        return i18n.trs("de_network", "网络请求失败，请检查网络/代理后重试。\n\n原始错误：{msg}", msg=msg)
    return msg


def _locate_file(work_dir: Path, video_id: str, on_log=None) -> Optional[Path]:
    """在临时目录中按 id 找到下载完成的视频文件。"""
    if not video_id:
        candidates = [p for p in work_dir.iterdir() if p.suffix.lower() in VIDEO_EXTS]
    else:
        candidates = [
            p for p in work_dir.glob(f"{video_id}*") if p.suffix.lower() in VIDEO_EXTS
        ]
    if not candidates:
        return None
    # 优先 mp4，其次体积最大的
    candidates.sort(key=lambda p: (p.suffix.lower() != ".mp4", -p.stat().st_size))
    return candidates[0]


# ----------------------------------------------------------------------------
# 原生抖音下载
# ----------------------------------------------------------------------------
def _is_douyin_url(url: str) -> bool:
    return bool(re.search(r"https?://[^/\s]*(?:douyin\.com|iesdouyin\.com)", url or ""))


# 各站点：域名 -> 视频源 id（显示名走 i18n 的 badge_* 文案）
SOURCE_IDS = {
    "douyin.com": "douyin",
    "iesdouyin.com": "douyin",
    "bilibili.com": "bilibili",
    "b23.tv": "bilibili",
    "kuaishou.com": "kuaishou",
    "xiaohongshu.com": "xiaohongshu",
    "xhslink.com": "xiaohongshu",
    "weibo.com": "weibo",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "ted.com": "ted",
    "tiktok.com": "tiktok",
}


def detect_source(url: str) -> str:
    """根据链接判断来源站点名（未知站点返回「网页视频」）。"""
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return i18n.trs("src_web", "网页视频")
    for key, sid in SOURCE_IDS.items():
        if host == key or host.endswith("." + key):
            return i18n.tr(f"badge_{sid}")
    return i18n.trs("src_web", "网页视频")


# 分享口令里链接后面常紧跟中文标点或空格，这里把它们全部排除
_SHARE_URL_RE = re.compile(r"https?://[^\s，。、；：！？（）()【】\[\]\"'<>]+")
_BARE_DOMAIN_RE = re.compile(
    r"(?:v\.douyin\.com|www\.douyin\.com|www\.iesdouyin\.com|"
    r"(?:www\.|m\.)?bilibili\.com|b23\.tv|(?:www\.)?ted\.com|"
    r"(?:www\.|vm\.|vt\.)?tiktok\.com)"
    r"/[^\s，。、；：！？（）()【】\[\]\"'<>]+"
)


def extract_url(text: str) -> str:
    """从任意文本里抽出第一个可用的链接。

    抖音的分享口令形如「0.79 复制打开抖音，看看【xx的作品】… https://v.douyin.com/xxxx/ 复制此链接…」，
    这里直接把它还原成 `https://v.douyin.com/xxxx/`；若输入本身已是纯链接则原样返回；
    都取不到时返回空串。
    """
    if not text:
        return ""
    text = text.strip()
    # 整串就是一个链接 → 直接返回
    if " " not in text and re.match(r"https?://", text):
        return text.rstrip(".,;")
    urls = _SHARE_URL_RE.findall(text)
    # 优先取已知视频站点的链接（分享文案里可能还夹着别的链接）
    for u in urls:
        if detect_source(u) != i18n.trs("src_web", "网页视频"):
            return u.rstrip(".,;")
    if urls:
        return urls[0].rstrip(".,;")
    # 兜底：可能是不带协议的域名
    m = _BARE_DOMAIN_RE.search(text)
    if m:
        return "https://" + m.group(0)
    return ""


def _http_text(
    url: str,
    cookie: str = "",
    ua: str = DESKTOP_UA,
    referer: str = "https://www.douyin.com/",
    timeout: int = 30,
):
    """GET 一个页面，返回 (网页文本, 最终URL)。最终URL 用于解析短链。"""
    headers = {
        "User-Agent": ua,
        "Referer": referer,
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore"), resp.geturl()


def _extract_aweme_id(url: str, cookie: str, on_log=None) -> str:
    for pattern in (r"/video/(\d{6,})", r"/share/video/(\d{6,})", r"modal_id=(\d{6,})", r"(\d{15,})"):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    # 短链（v.douyin.com/xxx）：跟随跳转拿最终地址
    if on_log:
        on_log(i18n.trs("dl_resolving_shortlink", "正在解析短链…"))
    try:
        _, final_url = _http_text(url, cookie=cookie, ua=MOBILE_UA)
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(i18n.trs("de_shortlink", "短链解析失败：{e}", e=exc)) from exc
    for pattern in (r"/video/(\d{6,})", r"/share/video/(\d{6,})", r"(\d{15,})"):
        m = re.search(pattern, final_url)
        if m:
            return m.group(1)
    raise DownloadError(i18n.trs("de_no_aweme", "无法从链接中解析出作品 ID，请使用作品分享链接。"))


def _find_item_list(obj):
    """在嵌套结构里找到第一个非空 item_list。"""
    if isinstance(obj, dict):
        items = obj.get("item_list")
        if isinstance(items, list) and items:
            return items
        for value in obj.values():
            found = _find_item_list(value)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_item_list(value)
            if found:
                return found
    return None


def _extract_first_item(html: str) -> Optional[dict]:
    """从分享页 HTML 中取出第一个作品的信息字典。"""
    decoder = json.JSONDecoder()
    # 首选：解析 _ROUTER_DATA
    m = re.search(r"_ROUTER_DATA\s*=\s*", html)
    if m:
        start = html.find("{", m.end())
        if start >= 0:
            try:
                data, _ = decoder.raw_decode(html[start:])
                items = _find_item_list(data)
                if items:
                    return items[0]
            except Exception:  # noqa: BLE001
                pass
    # 兜底：直接在 HTML 中定位 item_list 之后的第一个对象
    idx = html.find('"item_list"')
    if idx >= 0:
        bracket = html.find("[", idx)
        brace = html.find("{", bracket if bracket >= 0 else idx)
        if brace >= 0:
            try:
                item, _ = decoder.raw_decode(html[brace:])
                if isinstance(item, dict):
                    return item
            except Exception:  # noqa: BLE001
                pass
    return None


def _first_url(container) -> str:
    if isinstance(container, dict):
        for u in container.get("url_list") or []:
            if u:
                return u
    return ""


def _no_watermark_variants(url: str):
    """优先返回无水印地址：把 playwm 换成 play，再回退到原地址。"""
    if "/playwm/" in url:
        yield url.replace("/playwm/", "/play/")
    yield url


def _stream_download(
    url: str,
    dest: Path,
    cookie: str = "",
    on_progress: Optional[Callable[[float, str], None]] = None,
) -> Path:
    headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.douyin.com/"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    total = 0
    got = 0
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if on_progress and total:
                    on_progress(got / total * 100.0, i18n.tr("ps_downloading", pct=f"{got / total * 100:.0f}"))
    # 服务端声明了长度却没传完 → 判定为不完整，交给上层换备用地址重试
    if total and got < total:
        raise DownloadError(i18n.trs("de_incomplete", "下载不完整（已收 {got} / 应为 {total}）",
                                     got=_fmt_size(got), total=_fmt_size(total)))
    if got < 4096:
        raise DownloadError(i18n.trs("de_too_small", "下载得到的文件过小，可能被服务端拒绝。"))
    return dest


def _download_via_douyin(
    url: str,
    cfg,
    work_dir: Path,
    on_progress: Optional[Callable[[float, str], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> VideoInfo:
    cookie = (getattr(cfg, "cookies_raw", "") or "").strip()
    aweme_id = _extract_aweme_id(url, cookie, on_log)
    if on_log:
        on_log(i18n.trs("dl_aweme_id", "已解析作品 ID：{id}", id=aweme_id))

    share_url = f"https://www.iesdouyin.com/share/video/{aweme_id}/"
    if on_log:
        on_log(i18n.trs("dl_reading_item", "读取作品信息：{url}", url=share_url))
    try:
        html, _ = _http_text(share_url, cookie=cookie, ua=MOBILE_UA)
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(i18n.trs("de_item_page", "获取作品页失败：{e}", e=exc)) from exc

    item = _extract_first_item(html)
    if not item:
        raise DownloadError(i18n.trs(
            "de_no_item",
            "未能从作品页解析出数据。可能原因：\n"
            "1) Cookie 已失效（请在设置里更新 Cookie 字符串）；\n"
            "2) 链接不是作品详情页；\n"
            "3) 抖音页面结构有变。"))

    video = item.get("video") or {}
    images = item.get("images") or []
    if images and not (video.get("play_addr") or video.get("bit_rate")):
        raise DownloadError(i18n.trs("de_image_post", "该作品是图文（没有音频），无法进行语音识别。"))

    # 收集候选播放地址（含更高清的 bit_rate）
    candidates = []
    for key in ("play_addr", "play_addr_h264", "play_addr_256", "download_addr"):
        u = _first_url(video.get(key))
        if u:
            candidates.append(u)
    for br in video.get("bit_rate") or []:
        u = _first_url(br.get("play_addr"))
        if u:
            candidates.append(u)
    candidates = list(dict.fromkeys(candidates))
    if not candidates:
        raise DownloadError(i18n.trs("de_no_playurl", "未找到可用的视频播放地址。"))

    dest = work_dir / f"{aweme_id}.mp4"
    ok = False
    errors = []
    for raw in candidates[:8]:
        for candidate in _no_watermark_variants(raw):
            try:
                _stream_download(candidate, dest, cookie, on_progress)
                ok = True
                break
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")
        if ok:
            break
    if not ok:
        raise DownloadError(i18n.trs("de_video_failed", "视频文件下载失败：\n{e}", e="\n".join(errors[-3:])))

    if on_log:
        on_log(i18n.trs("dl_video_done", "视频下载完成：{name}（{size}）",
                        name=dest.name, size=_fmt_size(dest.stat().st_size)))

    # 时长（分享页里通常是毫秒）
    raw_duration = video.get("duration") or item.get("duration") or 0
    try:
        duration = float(raw_duration)
        if duration > 1000:
            duration /= 1000.0
    except (TypeError, ValueError):
        duration = 0.0

    create_time = item.get("create_time")
    try:
        upload_date = _dt.date.fromtimestamp(int(create_time)).strftime("%Y%m%d") if create_time else ""
    except Exception:  # noqa: BLE001
        upload_date = ""

    desc = (item.get("desc") or "").strip()
    return VideoInfo(
        video_id=aweme_id,
        title=desc,
        author=((item.get("author") or {}).get("nickname") or "").strip(),
        description=desc,
        duration=duration,
        webpage_url=f"https://www.douyin.com/video/{aweme_id}",
        thumbnail=_first_url(video.get("cover") or video.get("origin_cover")),
        upload_date=upload_date,
        video_path=dest,
    )


# ----------------------------------------------------------------------------
# 原生 TED 下载
# ----------------------------------------------------------------------------
def _is_ted_url(url: str) -> bool:
    return bool(re.search(r"https?://[^/\s]*ted\.com", url or ""))


def _parse_next_data(html: str) -> Optional[dict]:
    """取出 Next.js 页面里的 __NEXT_DATA__ JSON。"""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:  # noqa: BLE001
        return None


def _ted_find_paragraphs(obj, depth: int = 0):
    """在 transcriptData 里递归寻找 paragraphs 列表（结构各演讲略有差异）。"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        paras = obj.get("paragraphs")
        if isinstance(paras, list) and paras:
            return paras
        for v in obj.values():
            found = _ted_find_paragraphs(v, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _ted_find_paragraphs(v, depth + 1)
            if found:
                return found
    return None


def _ted_transcript_to_text(page_props: dict) -> str:
    """把 TED 官方字幕（cues）拼成纯文本；拿不到返回空串。"""
    td = page_props.get("transcriptData")
    if not isinstance(td, dict):
        return ""
    paras = _ted_find_paragraphs(td)
    if not paras:
        return ""
    lines = []
    for para in paras:
        if not isinstance(para, dict):
            continue
        chunks = []
        for cue in para.get("cues") or []:
            t = (cue.get("text") if isinstance(cue, dict) else str(cue)) or ""
            t = re.sub(r"\s+", " ", t).strip()
            if t:
                chunks.append(t)
        if chunks:
            lines.append(" ".join(chunks))
    text = "\n\n".join(lines)
    return re.sub(r"[ \t]+", " ", text).strip()


def _ted_thumbnail(vd: dict) -> str:
    vpd = vd.get("videoPlayerData") or {}
    if vpd.get("thumb"):
        return vpd["thumb"]
    for item in vd.get("primaryImageSet") or []:
        if isinstance(item, dict) and item.get("url"):
            return item["url"]
    return ""


def _download_via_ted(
    url: str,
    cfg,
    work_dir: Path,
    on_progress: Optional[Callable[[float, str], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> VideoInfo:
    """TED 演讲：解析页面元数据 + 官方字幕，再用 yt-dlp 下载 HLS 流。

    TED 现用 Next.js 渲染，yt-dlp 自带的 TED 提取器已失效（依赖的
    ``playerData`` 字段被移除）。这里改为自己解析 ``__NEXT_DATA__``，
    拿到 ``videoData.hlsUrl`` 后交给 yt-dlp 下载（已验证可用），
    并顺带抓取 TED 官方英文字幕，省去本地 ASR。
    """
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise DownloadError(i18n.trs("de_no_ytdlp", "未安装 yt-dlp，请先运行：pip install yt-dlp")) from exc

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    if on_log:
        on_log(i18n.trs("dl_ted_reading", "TED：正在读取演讲页面…"))
    try:
        html, final_url = _http_text(url, ua=DESKTOP_UA, referer="https://www.ted.com/")
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(i18n.trs("de_ted_page", "获取 TED 页面失败：{e}", e=exc)) from exc

    data = _parse_next_data(html)
    page_props = ((data or {}).get("props") or {}).get("pageProps") or {}
    vd = page_props.get("videoData")
    if not isinstance(vd, dict):
        raise DownloadError(i18n.trs(
            "de_ted_no_video",
            "未能从 TED 页面解析出视频信息。\n"
            "请确认链接形如 https://www.ted.com/talks/<slug> 的演讲页。"))

    vpd = vd.get("videoPlayerData") or {}
    hls = (vpd.get("resources") or {}).get("hls") or {}
    stream_url = vd.get("hlsUrl") or hls.get("stream") or ""
    if not stream_url:
        raise DownloadError(i18n.trs(
            "de_ted_no_stream",
            "TED 页面里没有找到视频流地址，可能该演讲仅提供 YouTube 播放。"))
    # 去掉 &preview，优先取完整版流
    stream_url = stream_url.split("&preview")[0]

    ted_id = str(vd.get("id") or "").strip() or "ted"
    safe_stem = re.sub(r"[^\w\-]+", "_", str(vd.get("slug") or ted_id)) or ted_id

    if on_log:
        title = str(vd.get("title") or "").strip()
        if title:
            on_log(i18n.trs("dl_ted_title", "TED：{title}", title=title))
        on_log(i18n.trs("dl_ted_downloading", "正在下载视频流…"))

    def hook(d: dict) -> None:
        if not on_progress:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            pct = (got / total * 100.0) if total else 0.0
            speed = d.get("speed")
            tail = f" {speed / 1024 / 1024:.1f} MB/s" if speed else ""
            on_progress(pct, i18n.tr("ps_downloading", pct=f"{pct:.0f}") + tail)
        elif status == "finished":
            on_progress(100.0, i18n.tr("ps_merging"))

    opts = {
        "outtmpl": str(work_dir / f"{safe_stem}.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "socket_timeout": 30,
        "http_headers": {"Referer": "https://www.ted.com/", "User-Agent": DESKTOP_UA},
        "progress_hooks": [hook],
    }
    ffmpeg = resolve_ffmpeg(cfg)
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(stream_url, download=True)
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(_friendly_error(i18n.trs("de_ted_download", "TED 视频下载失败：{e}", e=exc))) from exc

    video_path = _locate_file(work_dir, safe_stem, on_log)
    if video_path is None:
        raise DownloadError(i18n.trs("de_ted_no_file", "TED 视频下载完成但未找到文件，请重试。"))
    if on_log:
        on_log(i18n.trs("dl_video_done", "视频下载完成：{name}（{size}）",
                        name=video_path.name, size=_fmt_size(video_path.stat().st_size)))

    transcript = _ted_transcript_to_text(page_props)
    if transcript and on_log:
        on_log(i18n.trs("dl_ted_subs", "已获取 TED 官方字幕（{n} 字），将跳过本地语音识别。",
                        n=len(transcript)))

    recorded = str(vd.get("recordedOn") or vd.get("publishedAt") or "").strip()
    upload_date = re.sub(r"[^0-9]", "", recorded)[:8]

    return VideoInfo(
        video_id=ted_id,
        title=str(vd.get("title") or "").strip(),
        author=str(vd.get("presenterDisplayName") or vpd.get("speaker") or "").strip(),
        description=str(vd.get("description") or "").strip(),
        duration=float(vd.get("duration") or 0.0),
        webpage_url=vd.get("canonicalUrl") or final_url or url,
        thumbnail=_ted_thumbnail(vd),
        upload_date=upload_date,
        video_path=video_path,
        transcript=transcript,
    )


# ----------------------------------------------------------------------------
def download_video(
    url: str,
    cfg,
    work_dir: Path,
    on_progress: Optional[Callable[[float, str], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> VideoInfo:
    """下载单个视频，返回视频信息（含本地文件路径与元数据）。"""
    url = (url or "").strip()
    if not url:
        raise DownloadError(i18n.trs("de_need_url", "请输入视频链接。"))

    # 视频源白名单（由 setup 勾选；白名单为空时不做限制）
    allowed_fn = getattr(cfg, "is_source_allowed", None)
    if callable(allowed_fn) and not allowed_fn(url):
        from .config import VIDEO_SOURCE_BY_ID

        allowed = cfg.allowed_source_ids() if hasattr(cfg, "allowed_source_ids") else []
        names = i18n.trs("pl_list_sep", "、").join(
            i18n.tr(f"badge_{s}") for s in allowed if s in VIDEO_SOURCE_BY_ID)
        extra = (
            i18n.trs("de_whitelist_names", "（当前允许：{names}）", names=names)
            if names else i18n.trs("de_whitelist_empty", "（白名单为空）")
        )
        raise DownloadError(i18n.trs(
            "de_whitelist",
            "该链接的来源不在已勾选的视频源白名单中{extra}。请在 setup 的「视频源」步骤勾选对应来源后重试。",
            extra=extra))

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    if _is_ted_url(url):
        return _download_via_ted(url, cfg, work_dir, on_progress, on_log)

    if _is_douyin_url(url):
        return _download_via_douyin(url, cfg, work_dir, on_progress, on_log)

    return _download_via_ytdlp(url, cfg, work_dir, on_progress, on_log)


def _download_via_ytdlp(
    url: str,
    cfg,
    work_dir: Path,
    on_progress: Optional[Callable[[float, str], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> VideoInfo:
    """非抖音链接的兜底路径（同样适用于 B 站等其它站点）。"""
    try:
        import yt_dlp
    except ImportError as exc:  # pragma: no cover
        raise DownloadError(i18n.trs("de_no_ytdlp", "未安装 yt-dlp，请先运行：pip install yt-dlp")) from exc

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    def hook(d: dict) -> None:
        if not on_progress:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            pct = (got / total * 100.0) if total else 0.0
            speed = d.get("speed")
            tail = f" {speed / 1024 / 1024:.1f} MB/s" if speed else ""
            on_progress(pct, i18n.tr("ps_downloading", pct=f"{pct:.0f}") + tail)
        elif status == "finished":
            on_progress(100.0, i18n.tr("ps_merging"))

    base_opts = {
        "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "socket_timeout": 30,
        "progress_hooks": [hook],
    }
    ffmpeg = resolve_ffmpeg(cfg)
    if ffmpeg:
        # 让 yt-dlp 用同一个可用的 ffmpeg 做音视频合并
        base_opts["ffmpeg_location"] = ffmpeg

    is_douyin = _is_douyin_url(url)

    def _run(use_cookies: bool):
        opts = dict(base_opts)
        if use_cookies:
            # 直接粘贴的 Cookie / 浏览器登录态通常都是抖音的，只给抖音用；
            # 其它站点若需要登录，请用「设置 → Cookie 文件」指定 cookies.txt。
            _apply_cookies(opts, cfg, work_dir,
                           allow_raw=is_douyin, allow_browser=is_douyin)
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    if on_log:
        on_log(i18n.trs("dl_parsing", "   正在解析链接…"))
    try:
        info = _run(True)
    except Exception as exc:  # noqa: BLE001
        low = str(exc).lower()
        if any(k in low for k in ("cookie", "cookies")):
            # 常见于浏览器 Cookie 数据库被占用（yt-dlp issue #7271）：退化为不带 Cookie 再试一次
            if on_log:
                on_log(i18n.trs("dl_cookie_retry", "   ⚠ 读取 Cookie 失败，改用「不带 Cookie」重试…"))
            try:
                info = _run(False)
            except Exception as exc2:  # noqa: BLE001
                raise DownloadError(_friendly_error(str(exc2))) from exc2
        else:
            raise DownloadError(_friendly_error(str(exc))) from exc

    # 兼容播放列表结构
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise DownloadError(i18n.trs("de_no_video_parsed", "未解析到视频，请确认链接是作品详情页而不是主页。"))
        info = entries[0]

    video_id = str(info.get("id") or "")
    video_path = _locate_file(work_dir, video_id, on_log)
    if video_path is None:
        raise DownloadError(i18n.trs("de_download_no_file", "下载已完成但未找到视频文件，请重试或检查网络。"))

    return VideoInfo(
        video_id=video_id,
        title=(info.get("title") or "").strip(),
        author=(info.get("uploader") or info.get("creator") or "").strip(),
        description=(info.get("description") or "").strip(),
        duration=float(info.get("duration") or 0.0),
        webpage_url=info.get("webpage_url") or url,
        thumbnail=info.get("thumbnail") or "",
        upload_date=str(info.get("upload_date") or ""),
        video_path=video_path,
    )


def extract_audio(video_path: Path, wav_path: Path, cfg=None) -> Path:
    """用 ffmpeg 抽取 16kHz 单声道 wav（ASR 标准输入）。"""
    ffmpeg = resolve_ffmpeg(cfg)
    if not ffmpeg:
        raise DownloadError(i18n.trs(
            "de_ffmpeg_missing",
            "未找到可用的 ffmpeg。\n"
            "注意：PATH 里的 ffmpeg 可能是精简版（缺少音频解码器），"
            "请安装完整版 ffmpeg，或在「设置 → ffmpeg 路径」中手动指定 ffmpeg.exe 的完整路径。"
        ))
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        str(wav_path),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore"
    )
    if proc.returncode != 0 or not Path(wav_path).exists():
        err = (proc.stderr or "").strip()
        if "does not contain any stream" in err or "Output file does not contain" in err:
            raise DownloadError(i18n.trs("de_no_audio", "该作品没有音频轨道（可能是图文/纯图片作品），无法进行语音识别。"))
        raise DownloadError(i18n.trs("de_audio_extract", "音频抽取失败：\n{e}",
                                     e=(err[-800:] or i18n.trs("de_unknown_err", "未知错误"))))
    return Path(wav_path)
