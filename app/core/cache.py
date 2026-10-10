"""上次运行结果的本地缓存。

用途：视频下载 / 语音识别（ASR） / 截取配图这几步既耗时又依赖网络与算力。
当用户只是对「成稿」不满意（比如觉得内容太少、想换语言或篇幅），
可以用缓存里已保存的转录稿与配图直接重跑最后的「整理 + 导出」两步，
跳过前面所有耗时环节。

缓存目录固定在输出目录下的 ``.last_run_cache/``，内容：

    .last_run_cache/
        meta.json        元信息 + 配图清单（不含图片二进制）
        transcript.txt   转录稿全文
        cover.jpg        封面图（可选）
        images/01.jpg    配图（可选，编号与 meta 中对应）

一次完整运行结束后写入；新运行会整体覆盖旧缓存。
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import frames
from .config import AppConfig
from .downloader import VideoInfo
from ..ui import i18n

CACHE_DIR_NAME = ".last_run_cache"
_CACHE_VERSION = 1


@dataclass
class RunCache:
    """从磁盘读回的上次运行中间结果。"""

    info: VideoInfo
    transcript: str
    images: List[frames.FrameCandidate] = field(default_factory=list)
    cover_path: Optional[Path] = None       # 缓存目录里的封面文件（若存在）
    source_name: str = "网页视频"
    created: str = ""
    dir: Optional[Path] = None


def cache_dir(cfg: AppConfig) -> Path:
    """返回缓存目录（不自动创建）。"""
    return cfg.resolved_output_dir / CACHE_DIR_NAME


def has_cache(cfg: AppConfig) -> bool:
    """判断是否存在可用的上次运行缓存。"""
    d = cache_dir(cfg)
    return (d / "meta.json").is_file() and (d / "transcript.txt").is_file()


def clear_cache(cfg: AppConfig) -> None:
    """删除缓存目录（若存在）。"""
    shutil.rmtree(cache_dir(cfg), ignore_errors=True)


def _info_to_dict(info: VideoInfo) -> dict:
    return {
        "video_id": info.video_id or "",
        "title": info.title or "",
        "author": info.author or "",
        "description": info.description or "",
        "duration": float(info.duration or 0.0),
        "webpage_url": info.webpage_url or "",
        "thumbnail": info.thumbnail or "",
        "upload_date": info.upload_date or "",
    }


def save_cache(
    cfg: AppConfig,
    *,
    info: VideoInfo,
    transcript: str,
    images: List[frames.FrameCandidate],
    cover_path: Optional[Path],
    source_name: str = "网页视频",
    created: str = "",
) -> None:
    """把上次运行的中间结果整体写入缓存（覆盖旧缓存）。

    任何一步失败都不应影响主流程，因此调用方应当自行 try/except。
    """
    d = cache_dir(cfg)
    # 先清掉旧内容，避免残留上次的图片编号
    shutil.rmtree(d, ignore_errors=True)
    (d / "images").mkdir(parents=True, exist_ok=True)

    (d / "transcript.txt").write_text(transcript or "", encoding="utf-8")

    img_meta = []
    for c in images or []:
        name = f"{int(c.index):02d}.jpg"
        try:
            (d / "images" / name).write_bytes(c.jpeg or b"")
        except Exception:  # noqa: BLE001
            continue
        img_meta.append({
            "file": f"images/{name}",
            "index": int(c.index),
            "timestamp": float(c.timestamp or 0.0),
            "width": int(c.width or 0),
            "height": int(c.height or 0),
            "score": float(c.score or 0.0),
            "context": c.context or "",
            "caption": c.caption or "",
            "useful": bool(c.useful),
            "vision_score": float(c.vision_score or 0.0),
        })

    has_cover = False
    if cover_path and Path(cover_path).exists():
        try:
            shutil.copy2(cover_path, d / "cover.jpg")
            has_cover = True
        except Exception:  # noqa: BLE001
            has_cover = False

    meta = {
        "version": _CACHE_VERSION,
        "created": created,
        "source": source_name or i18n.trs("src_web", "网页视频"),
        "info": _info_to_dict(info),
        "transcript_chars": len(transcript or ""),
        "has_cover": has_cover,
        "images": img_meta,
    }
    (d / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_cache(cfg: AppConfig) -> RunCache:
    """读回上次运行缓存；文件缺失或损坏时抛出 ``FileNotFoundError``/``ValueError``。"""
    d = cache_dir(cfg)
    meta_path = d / "meta.json"
    tx_path = d / "transcript.txt"
    if not meta_path.is_file() or not tx_path.is_file():
        raise FileNotFoundError(i18n.trs("ca_no_cache", "没有可用的上次运行缓存，请先完整运行一次。"))

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(i18n.trs("ca_meta_broken", "缓存元信息损坏：{e}", e=exc)) from exc

    transcript = tx_path.read_text(encoding="utf-8")

    raw_info = meta.get("info") or {}
    info = VideoInfo(
        video_id=str(raw_info.get("video_id") or ""),
        title=str(raw_info.get("title") or ""),
        author=str(raw_info.get("author") or ""),
        description=str(raw_info.get("description") or ""),
        duration=float(raw_info.get("duration") or 0.0),
        webpage_url=str(raw_info.get("webpage_url") or ""),
        thumbnail=str(raw_info.get("thumbnail") or ""),
        upload_date=str(raw_info.get("upload_date") or ""),
        video_path=None,
        transcript="",
    )

    images: List[frames.FrameCandidate] = []
    for item in meta.get("images") or []:
        rel = str(item.get("file") or "")
        fpath = d / rel
        jpeg = b""
        try:
            if fpath.is_file():
                jpeg = fpath.read_bytes()
        except Exception:  # noqa: BLE001
            jpeg = b""
        images.append(frames.FrameCandidate(
            index=int(item.get("index") or 0),
            timestamp=float(item.get("timestamp") or 0.0),
            jpeg=jpeg,
            width=int(item.get("width") or 0),
            height=int(item.get("height") or 0),
            score=float(item.get("score") or 0.0),
            context=str(item.get("context") or ""),
            caption=str(item.get("caption") or ""),
            useful=bool(item.get("useful", True)),
            vision_score=float(item.get("vision_score") or 0.0),
        ))
    images.sort(key=lambda c: c.index)

    cover_path: Optional[Path] = None
    if meta.get("has_cover"):
        cand = d / "cover.jpg"
        if cand.is_file():
            cover_path = cand

    return RunCache(
        info=info,
        transcript=transcript,
        images=images,
        cover_path=cover_path,
        source_name=str(meta.get("source") or i18n.trs("src_web", "网页视频")),
        created=str(meta.get("created") or ""),
        dir=d,
    )
