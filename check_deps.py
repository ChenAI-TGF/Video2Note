"""检查运行本程序所需的依赖是否齐全（供 run.bat 调用）。

缺少依赖时打印缺失清单并以退出码 1 结束。
依赖清单一并维护在 app/core/deps.py，界面向导（setup_ui.py）也复用它。
提示文案跟随 config.json 里的界面语言。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.deps import missing_deps  # noqa: E402
from app.ui import i18n  # noqa: E402

# 读取界面语言（读不到就用默认英文）
try:
    _cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    i18n.set_language(str(_cfg.get("ui_language") or "en"))
except Exception:  # noqa: BLE001
    i18n.set_language("en")

missing = missing_deps()

if missing:
    names = i18n.trs("pl_list_sep", "、").join(missing)
    print(i18n.tr("cd_missing_fmt", list=names))
    print(i18n.tr("cd_python_fmt", py=sys.executable))
    sys.exit(1)

sys.exit(0)
