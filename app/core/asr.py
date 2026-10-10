"""语音识别：FunASR SenseVoice（本地、中文优化、CPU 可跑）。

推荐配置
-------
* ``iic/SenseVoiceSmall``：中文准确率最高（CER≈7.8%），CPU 上约 17 倍实时，
  模型仅约 234MB，首次使用会从 ModelScope 自动下载。
* 若需要字级时间戳做字幕，可把模型换成 ``iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch``。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable, Optional

from ..ui import i18n

# SenseVoice 输出里带 <|zh|><|HAPPY|><|Speech|> 之类的富文本标签，需要清掉
_TAG_RE = re.compile(r"<\|[^|]*\|>")
# rich_transcription_postprocess 会把情绪/事件标签转成这些 emoji，写博客时属于噪音
_EMOJI_RE = re.compile(r"[😊😔😡😰🤢😮🎼👏😀😭🤧]")


class ASRError(RuntimeError):
    """语音识别阶段的可读错误。"""


def clean_text(text: str) -> str:
    text = _TAG_RE.sub("", text or "")
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


class SenseVoiceASR:
    """懒加载封装：同一个实例只加载一次模型。"""

    def __init__(
        self,
        model_name: str = "iic/SenseVoiceSmall",
        device: str = "cpu",
        language: str = "auto",
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.language = language
        self._model = None
        self._postprocess = None

    # ------------------------------------------------------------------
    def load(self, on_log: Optional[Callable[[str], None]] = None) -> None:
        if self._model is not None:
            return
        try:
            from funasr import AutoModel
            from funasr.utils.postprocess_utils import rich_transcription_postprocess
        except ImportError as exc:  # pragma: no cover
            raise ASRError(i18n.trs(
                "asr_no_deps",
                "未安装语音识别依赖（funasr / modelscope / kaldi-native-fbank）。\n"
                "解决办法：在项目目录双击 setup.bat；或直接双击 run.bat，它会自动检测并补装。\n"
                "当前使用的 Python：{py}\n"
                "原始错误：{e}",
                py=sys.executable, e=exc)) from exc

        if on_log:
            on_log(i18n.trs("asr_loading",
                            "加载 ASR 模型：{m}（设备 {d}；首次会从 ModelScope 下载，约 1GB）",
                            m=self.model_name, d=self.device))
        try:
            self._model = AutoModel(
                model=self.model_name,
                vad_model="fsmn-vad",
                vad_kwargs={"max_single_segment_time": 30000},
                device=self.device,
                disable_update=True,
            )
        except TypeError:
            # 老版本 funasr 不接受 disable_update
            self._model = AutoModel(
                model=self.model_name,
                vad_model="fsmn-vad",
                vad_kwargs={"max_single_segment_time": 30000},
                device=self.device,
            )
        except Exception as exc:  # noqa: BLE001
            raise ASRError(i18n.trs("asr_load_fail", "ASR 模型加载失败：{e}", e=exc)) from exc

        self._postprocess = rich_transcription_postprocess

    # ------------------------------------------------------------------
    def transcribe(
        self,
        audio_path: Path,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> str:
        self.load(on_log=on_log)
        if on_log:
            on_log(i18n.trs("asr_ready", "模型就绪，开始识别音频（VAD 分段 + SenseVoice 解码）…"))
        try:
            res = self._model.generate(
                input=str(audio_path),
                cache={},
                language=self.language,
                use_itn=True,
                batch_size_s=60,
                merge_vad=True,
                merge_length_s=15,
            )
        except Exception as exc:  # noqa: BLE001
            raise ASRError(i18n.trs("asr_fail", "语音识别失败：{e}", e=exc)) from exc

        if not res:
            return ""
        raw = res[0].get("text", "") if isinstance(res[0], dict) else str(res[0])
        text = self._postprocess(raw) if self._postprocess else raw
        return clean_text(text)
