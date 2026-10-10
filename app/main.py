"""程序入口。

运行方式：
    python -m app.main
或双击 run.bat
"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许 `python app/main.py` 直接运行
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QFont  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.main_window import MainWindow  # noqa: E402
from app.ui.style import STYLESHEET  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Video2Note")
    app.setStyle("Fusion")
    # 保证中文清晰显示
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLESHEET)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
