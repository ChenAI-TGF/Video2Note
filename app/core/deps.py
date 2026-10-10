"""运行依赖清单与检测。

集中维护 requirements.txt 里各项依赖的「pip 包名 / 导入名 / 用途」，
供 check_deps.py（命令行自检）和 setup_ui.py（配置向导界面）共用。

本模块只依赖标准库，因此即使 PySide6 等尚未安装也能正常导入。
"""
from __future__ import annotations

import importlib.util
from typing import Dict, List

# (pip 包名, 导入名, 用途文案 key)
DEPS: List[Dict[str, str]] = [
    {"pip": "PySide6",           "module": "PySide6",            "purpose": "purpose_ui"},
    {"pip": "yt-dlp",            "module": "yt_dlp",             "purpose": "purpose_download"},
    {"pip": "funasr",            "module": "funasr",             "purpose": "purpose_asr"},
    {"pip": "modelscope",        "module": "modelscope",         "purpose": "purpose_asr"},
    {"pip": "kaldi-native-fbank", "module": "kaldi_native_fbank", "purpose": "purpose_asr"},
    {"pip": "soundfile",         "module": "soundfile",          "purpose": "purpose_asr"},
    {"pip": "openai",            "module": "openai",             "purpose": "purpose_llm"},
    {"pip": "opencv-python",     "module": "cv2",                "purpose": "purpose_frames"},
    {"pip": "PyYAML",            "module": "yaml",               "purpose": "purpose_config"},
]


def is_installed(module: str) -> bool:
    """判断某个导入名对应的包是否已经安装（不真正导入，速度快）。"""
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:  # noqa: BLE001
        return False


def missing_deps() -> List[str]:
    """返回缺失依赖的 pip 包名列表。"""
    return [d["pip"] for d in DEPS if not is_installed(d["module"])]


def dep_status() -> List[Dict[str, object]]:
    """返回每项依赖的状态（含 installed 布尔值），供界面展示。"""
    return [{**d, "installed": is_installed(d["module"])} for d in DEPS]


def all_installed() -> bool:
    """所有依赖是否都已安装。"""
    return not missing_deps()
