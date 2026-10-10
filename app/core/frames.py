"""智能抽帧：从视频里挑选适合做博客配图的画面。

筛选思路（全自动，不需要人工干预）
--------------------------------
1. 按时间均匀采样若干候选帧（跳过片头片尾，长视频自动降低采样密度）；
2. 逐帧计算三个指标：**清晰度**（拉普拉斯方差）、**信息量**（边缘密度）、
   **场景变化强度**（与上一个采样帧的画面差异）；
3. 先剔除模糊帧，再按「信息量 + 场景变化」加权打分；
4. 把视频按时间切成若干区，每区取打分最高、且与已选帧画面差异足够大的那一帧，
   这样配图会均匀分布在全片，不会扎堆在同一分钟。

只依赖 OpenCV，不需要 ffmpeg。
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import numpy as np

from ..ui import i18n

ANALYSIS_W, ANALYSIS_H = 320, 180
MAX_SAMPLES = 300   # 采样上限，避免长视频耗时过久
MIN_SAMPLES = 24
FULL_W = 1280       # 导出配图的最大宽度


class FrameError(RuntimeError):
    """抽帧阶段的可读错误。"""


@dataclass
class FrameCandidate:
    index: int = 0            # 从 1 开始的编号
    timestamp: float = 0.0    # 视频内时间（秒）
    jpeg: bytes = b""         # 全分辨率 JPEG 数据
    width: int = 0
    height: int = 0
    score: float = 0.0        # 启发式得分
    context: str = ""         # 该时间点大概在讲什么（由转录稿按时间比例估算）
    caption: str = ""         # 视觉模型给出的画面描述
    useful: bool = True       # 视觉模型判定是否适合做配图
    vision_score: float = 0.0


def fmt_ts(seconds: float) -> str:
    total = int(round(max(0.0, seconds)))
    m, s = divmod(total, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return i18n.trs("fr_hms", "{h}时{m}分{s:02d}秒", h=h, m=m, s=s)
    if m:
        return i18n.trs("fr_ms", "{m}分{s:02d}秒", m=m, s=s)
    return i18n.trs("fr_s", "{s}秒", s=s)


def _norm(arr: np.ndarray) -> np.ndarray:
    lo, hi = float(arr.min()), float(arr.max())
    if hi - lo < 1e-9:
        return np.zeros_like(arr, dtype=float)
    return (arr - lo) / (hi - lo)


def _context_at(transcript: str, t: float, duration: float, max_len: int = 80) -> str:
    """按时间比例估算该时刻在讲什么（无时间戳 ASR 的近似方案）。"""
    if not transcript or duration <= 0:
        return ""
    ratio = max(0.0, min(1.0, t / duration))
    pos = int(len(transcript) * ratio)
    left = transcript.rfind("。", max(0, pos - 60))
    start = left + 1 if left >= 0 else max(0, pos - 30)
    right = transcript.find("。", pos)
    end = right + 1 if right >= 0 else min(len(transcript), pos + 30)
    snippet = transcript[start:end].strip()
    if len(snippet) > max_len:
        snippet = snippet[:max_len] + "…"
    return snippet


def to_vision_data_url(cand: FrameCandidate, max_w: int = 512, quality: int = 70) -> str:
    """把候选帧压小后转成 data URL，用于交给视觉模型（省 token）。"""
    try:
        import cv2

        img = cv2.imdecode(np.frombuffer(cand.jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            h, w = img.shape[:2]
            if w > max_w:
                img = cv2.resize(img, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if ok:
                return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
    except Exception:  # noqa: BLE001
        pass
    return "data:image/jpeg;base64," + base64.b64encode(cand.jpeg).decode("ascii")


def extract_candidates(
    video_path: Path,
    transcript: str = "",
    duration: float = 0.0,
    want: int = 6,
    on_log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
) -> List[FrameCandidate]:
    """从视频里挑出候选帧（数量约等于 want），按时间顺序返回。"""
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover
        raise FrameError(i18n.trs("fr_no_opencv", "未安装 opencv-python，请运行：pip install opencv-python")) from exc

    def log(msg: str) -> None:
        if on_log:
            on_log(msg)

    def prog(pct: int) -> None:
        if on_progress:
            on_progress(pct)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise FrameError(i18n.trs("fr_open_fail", "无法打开视频文件：{video_path}", video_path=video_path))

    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if fps <= 1e-3 and total_frames and duration:
            fps = total_frames / duration
        if fps <= 1e-3:
            fps = 25.0
        if duration <= 0 and total_frames:
            duration = total_frames / fps
        if total_frames <= 0:
            total_frames = int(duration * fps)
        if duration <= 0:
            raise FrameError(i18n.trs("fr_no_duration", "无法获取视频时长，跳过抽帧。"))

        # 跳过片头（通常是标题卡/品牌片头）与片尾
        if duration > 60:
            start = 10.0
        elif duration > 30:
            start = 2.0
        else:
            start = 0.5
        end = max(start + 1.0, duration - 1.0)
        span = end - start
        n = int(max(MIN_SAMPLES, min(MAX_SAMPLES, round(span))))
        times = [start + span * (i + 0.5) / n for i in range(n)]
        log(i18n.trs("fr_start", "   视频 {d} @ {fps:.1f}fps，采样 {n} 帧分析",
                     d=fmt_ts(duration), fps=fps, n=n))

        grays: List[np.ndarray] = []
        kept: List[float] = []
        for i, t in enumerate(times):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            small = cv2.resize(frame, (ANALYSIS_W, ANALYSIS_H), interpolation=cv2.INTER_AREA)
            grays.append(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
            kept.append(t)
            if i % 15 == 0:
                prog(int(i / max(1, n) * 100))
        if len(grays) < 4:
            raise FrameError(i18n.trs("fr_too_few", "采样到的有效帧太少，无法挑选配图。"))
        log(i18n.trs("fr_sampled", "   有效采样帧 {n} 张，计算清晰度/信息量/场景变化…", n=len(grays)))

        sharpness = np.array([float(cv2.Laplacian(g, cv2.CV_64F).var()) for g in grays])
        edges = np.array([float(np.mean(cv2.Canny(g, 60, 160) > 0)) for g in grays])
        diffs = np.zeros(len(grays))
        for i in range(1, len(grays)):
            d = np.abs(grays[i].astype(np.int16) - grays[i - 1].astype(np.int16))
            diffs[i] = float(np.mean(d)) / 255.0

        scores = 0.55 * _norm(diffs) + 0.45 * _norm(edges)
        floor = max(12.0, 0.25 * float(np.median(sharpness)))
        pool = [(i, float(scores[i])) for i in range(len(grays)) if sharpness[i] >= floor]
        if not pool:
            pool = [(i, float(scores[i])) for i in range(len(grays))]
        log(i18n.trs("fr_pool", "   剔除模糊帧后剩 {n} 张候选（清晰度阈值 {floor:.0f}）",
                     n=len(pool), floor=floor))

        zones = max(1, min(want, len(pool)))

        def _select(distinct: float, gap_factor: float) -> List[int]:
            """每区取一张；distinct 为与已选帧的最小画面差异，gap_factor 控制最小间隔。"""
            bounds = np.linspace(start, end, zones + 1)
            gap = max(2.0, span / (zones * gap_factor))
            out: List[int] = []
            for z in range(zones):
                lo, hi = float(bounds[z]), float(bounds[z + 1])
                zone_pool = sorted(
                    [(i, s) for (i, s) in pool if lo <= kept[i] < hi],
                    key=lambda x: -x[1],
                )
                for i, _s in zone_pool:
                    t = kept[i]
                    if any(abs(t - kept[j]) < gap for j in out):
                        continue
                    if distinct > 0 and any(
                        float(np.mean(np.abs(grays[i].astype(np.int16) - grays[j].astype(np.int16))))
                        < distinct
                        for j in out
                    ):
                        continue
                    out.append(i)
                    break
            return out

        # 画面布局固定的视频（游戏录制、字幕流）容易把所有帧都判成"重复"，
        # 因此逐步放宽去重强度，尽量凑齐需要的张数。
        picked: List[int] = []
        used_rule = ""
        for distinct, gap_factor, label in (
            (12.0, 3, i18n.trs("fr_rule_strict", "严格")),
            (8.0, 3, i18n.trs("fr_rule_tight", "较严")),
            (5.0, 4, i18n.trs("fr_rule_medium", "中等")),
            (3.0, 5, i18n.trs("fr_rule_loose", "较松")),
            (1.5, 6, i18n.trs("fr_rule_wide", "宽松")),
            (0.0, 8, i18n.trs("fr_rule_none", "不限")),
        ):
            trial = _select(distinct, gap_factor)
            if len(trial) > len(picked):
                picked = trial
                used_rule = label
            if len(picked) >= zones:
                break
        if used_rule:
            log(i18n.trs("fr_used_rule", "   去重强度：{label}", label=used_rule))
        picked.sort(key=lambda i: kept[i])
        if not picked:
            raise FrameError(i18n.trs("fr_nothing", "未能筛选出合适的画面。"))

        # 取出全分辨率的选中帧
        out: List[FrameCandidate] = []
        for idx in picked:
            t = kept[idx]
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(t * fps))
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            h, w = frame.shape[:2]
            if w > FULL_W:
                frame = cv2.resize(frame, (FULL_W, int(h * FULL_W / w)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            if not ok:
                continue
            out.append(
                FrameCandidate(
                    timestamp=t,
                    jpeg=buf.tobytes(),
                    width=int(frame.shape[1]),
                    height=int(frame.shape[0]),
                    score=float(scores[idx]),
                    context=_context_at(transcript, t, duration),
                )
            )
        for n_i, cand in enumerate(out, start=1):
            cand.index = n_i
        log(i18n.trs("fr_picked", "   选出 {n} 张候选画面：{list}",
                     n=len(out),
                     list=i18n.trs("pl_list_sep", "、").join(fmt_ts(c.timestamp) for c in out)))
        return out
    finally:
        cap.release()
