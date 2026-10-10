"""全局界面样式（浅色、卡片式、圆角）。"""
from __future__ import annotations

STYLESHEET = """
* {
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
}
QMainWindow, QDialog { background: #f4f5f7; }
QWidget#Card {
    background: #ffffff;
    border: 1px solid #e6e8ec;
    border-radius: 12px;
}
QLabel#AppTitle { font-size: 19px; font-weight: 600; color: #1b1f27; }
QLabel#AppSub { font-size: 12px; color: #8b929e; }
QLabel#Summary { font-size: 12px; color: #5b6472; }
QLabel#SummaryWarn { font-size: 12px; color: #d97706; }
QLabel#Stage { font-size: 13px; color: #2b3038; font-weight: 600; }
QLabel#Percent { font-size: 12px; color: #6b7280; }
QLabel#SectionTitle {
    font-size: 12px; font-weight: 600; color: #8b929e;
    padding-top: 10px; padding-bottom: 2px;
}

QLineEdit, QPlainTextEdit, QTextBrowser, QComboBox {
    background: #ffffff;
    border: 1px solid #dcdfe6;
    border-radius: 8px;
    padding: 6px 8px;
    color: #1f2329;
    selection-background-color: #4b6ef5;
    selection-color: #ffffff;
}
QLineEdit { padding: 9px 12px; font-size: 14px; }
QLineEdit:focus, QPlainTextEdit:focus, QTextBrowser:focus, QComboBox:focus {
    border: 1px solid #4b6ef5;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #ffffff; border: 1px solid #dcdfe6;
    selection-background-color: #eef2ff; selection-color: #1f2329; outline: none;
}

QPushButton {
    background: #ffffff;
    border: 1px solid #dcdfe6;
    border-radius: 8px;
    padding: 8px 14px;
    color: #2b3038;
}
QPushButton:hover { background: #f1f3f7; border-color: #c9cfda; }
QPushButton:pressed { background: #e8ebf1; }
QPushButton:disabled { color: #b0b6c0; background: #f5f6f8; border-color: #e6e8ec; }
QPushButton#Primary {
    background: #4b6ef5; border: none; color: #ffffff;
    font-weight: 600; font-size: 14px; padding: 10px 22px;
}
QPushButton#Primary:hover { background: #3f61e6; }
QPushButton#Primary:pressed { background: #3554d4; }
QPushButton#Primary:disabled { background: #b8c3ea; color: #ffffff; }
QPushButton#Ghost { background: transparent; border: none; color: #5b6472; padding: 6px 10px; }
QPushButton#Ghost:hover { background: #eef0f4; border-radius: 6px; }

/* 分段切换按钮（博客模式 / 笔记模式） */
QPushButton#Seg {
    background: #ffffff; border: 1px solid #dcdfe6; color: #2b3038;
    padding: 6px 16px; border-radius: 8px;
}
QPushButton#Seg:hover { background: #f1f3f7; border-color: #c9cfda; }
QPushButton#Seg:checked {
    background: #4b6ef5; border-color: #4b6ef5; color: #ffffff; font-weight: 600;
}
QPushButton#Seg:checked:hover { background: #3f61e6; }
QPushButton#Seg:disabled { color: #b0b6c0; }

/* 篇幅滑块（五档） */
QSlider#LengthSlider { min-height: 22px; }
QSlider#LengthSlider::groove:horizontal {
    height: 6px; background: #e9ecf2; border-radius: 3px;
}
QSlider#LengthSlider::sub-page:horizontal {
    background: #4b6ef5; border-radius: 3px;
}
QSlider#LengthSlider::handle:horizontal {
    background: #ffffff; border: 2px solid #4b6ef5;
    width: 14px; height: 14px; margin: -6px 0; border-radius: 9px;
}
QSlider#LengthSlider::handle:horizontal:hover { background: #eef2ff; }
QSlider#LengthSlider::handle:horizontal:pressed { background: #dfe6ff; }
QSlider#LengthSlider:disabled::sub-page:horizontal { background: #c4cad4; }
QSlider#LengthSlider:disabled::handle:horizontal { border-color: #c4cad4; }

QProgressBar {
    background: #e9ecf2; border: none; border-radius: 5px;
    height: 10px; text-align: center;
    color: #5b6472; font-size: 11px;
}
QProgressBar::chunk { background: #4b6ef5; border-radius: 5px; }

/* 上进度条：六个步骤的总进度 */
QProgressBar#StepBar { height: 18px; border-radius: 6px; }
QProgressBar#StepBar::chunk { background: #4b6ef5; border-radius: 6px; }

/* 下进度条：当前步骤内部的百分进度 */
QProgressBar#SubBar { height: 8px; border-radius: 4px; }
QProgressBar#SubBar::chunk { background: #2bb6a3; border-radius: 4px; }

QTabWidget::pane {
    border: 1px solid #e6e8ec; border-radius: 10px;
    background: #ffffff; top: -1px;
}
QTabBar::tab {
    background: transparent; border: none;
    padding: 8px 16px; margin-right: 4px;
    color: #6b7280; font-size: 13px;
}
QTabBar::tab:selected { color: #4b6ef5; font-weight: 600; }
QTabBar::tab:hover { color: #3f61e6; }

QCheckBox { color: #2b3038; spacing: 7px; }
QCheckBox::indicator { width: 16px; height: 16px; }
QCheckBox::indicator:unchecked {
    border: 1px solid #c4cad4; border-radius: 4px; background: #ffffff;
}
QCheckBox::indicator:checked {
    border: 1px solid #4b6ef5; border-radius: 4px; background: #4b6ef5;
}

QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #cfd4dd; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #b6bcc8; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 2px; }
QScrollBar::handle:horizontal { background: #cfd4dd; border-radius: 5px; min-width: 30px; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QStatusBar { color: #6b7280; }
QToolTip {
    background: #2b3038; color: #ffffff;
    border: none; padding: 5px 8px; border-radius: 4px;
}
"""
