"""Markdown 预览窗口：独立窗口，占据屏幕右半侧、高度占满，可显示本地配图。"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import (
    QDesktopServices,
    QFont,
    QGuiApplication,
    QImage,
    QKeySequence,
    QShortcut,
    QTextBlockFormat,
    QTextCursor,
    QTextImageFormat,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from . import i18n

_IMG_RE = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)(\))")
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)

# 预览排版：让长文更易读、不那么「生硬」
_PREVIEW_CSS = """
body {
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 15px;
    color: #1f2329;
    line-height: 172%;
}
h1 { font-size: 26px; font-weight: 700; margin-top: 10px; margin-bottom: 14px; }
h2 { font-size: 21px; font-weight: 700; margin-top: 24px; margin-bottom: 10px; }
h3 { font-size: 18px; font-weight: 600; margin-top: 18px; margin-bottom: 8px; }
h4 { font-size: 16px; font-weight: 600; margin-top: 14px; margin-bottom: 6px; }
p  { margin-top: 6px; margin-bottom: 10px; }
ul, ol { margin-top: 6px; margin-bottom: 10px; }
li { margin-bottom: 4px; }
blockquote {
    color: #5b6472; margin-left: 14px; margin-right: 14px;
    padding-left: 12px; border-left: 3px solid #d5d9e0;
}
code { background-color: #f1f3f7; font-family: Consolas, "Cascadia Mono", monospace; font-size: 13px; }
pre  { background-color: #f6f7f9; padding: 12px; margin: 10px 0; }
a    { color: #4b6ef5; }
hr   { border: none; border-top: 1px solid #e6e8ec; margin: 20px 0; }
table { border-collapse: collapse; }
th, td { border: 1px solid #e6e8ec; padding: 4px 8px; }
"""


def _strip_frontmatter(markdown: str) -> str:
    """去掉开头的 YAML frontmatter（--- ... ---），预览时更好看。"""
    if not markdown or not markdown.lstrip().startswith("---"):
        return markdown
    m = _FRONTMATTER_RE.match(markdown.lstrip())
    if m:
        return markdown.lstrip()[m.end():]
    return markdown


def _absolutize(markdown: str, base_dir: Path) -> str:
    """把 markdown 里的相对图片路径换成绝对 file:// URL，确保本地配图能显示。"""
    def repl(m: re.Match) -> str:
        src = m.group(2)
        if re.match(r"^(?:https?:|data:|file:)", src, re.IGNORECASE) or src.startswith("#"):
            return m.group(0)
        # markdown 链接里的路径可能是百分号转义的（含空格/中文等），先还原再找文件
        try:
            rel = unquote(src)
            path = (base_dir / rel.replace("/", os.sep)).resolve()
        except Exception:  # noqa: BLE001
            return m.group(0)
        if not path.exists():
            return m.group(0)
        return f"{m.group(1)}{path.as_uri()}{m.group(3)}"

    return _IMG_RE.sub(repl, markdown)


class PreviewWindow(QMainWindow):
    """独立预览窗口：不开在主界面里，右半屏 + 满高。"""

    # 配图最大显示宽度（超过就等比缩小；够窄的窗口再按比例收）
    MAX_IMAGE_WIDTH = 620

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._markdown = ""
        self._md_path: Optional[Path] = None
        self._title = ""

        self._img_timer = QTimer(self)
        self._img_timer.setSingleShot(True)
        self._img_timer.setInterval(120)
        self._img_timer.timeout.connect(self._apply_image_sizes)

        self._build_ui()
        self.retranslate()
        self._place_right_half()
        # Esc 关闭窗口
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.close)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("PreviewRoot")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        bar = QHBoxLayout()
        self.title_label = QLabel()
        self.title_label.setObjectName("Stage")
        bar.addWidget(self.title_label, 1)

        self.copy_btn = QPushButton()
        self.copy_btn.clicked.connect(self._on_copy)
        bar.addWidget(self.copy_btn)

        self.dir_btn = QPushButton()
        self.dir_btn.clicked.connect(self._on_open_dir)
        bar.addWidget(self.dir_btn)

        self.close_btn = QPushButton()
        self.close_btn.setObjectName("Ghost")
        self.close_btn.clicked.connect(self.close)
        bar.addWidget(self.close_btn)
        root.addLayout(bar)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(True)
        self.view.setFrameShape(QTextBrowser.NoFrame)
        self.view.setStyleSheet(
            "QTextBrowser { background: #ffffff; border: 1px solid #e6e8ec;"
            " border-radius: 10px; padding: 8px 14px; }"
        )
        doc = self.view.document()
        doc.setDocumentMargin(18)
        doc.setDefaultFont(QFont("Microsoft YaHei UI", 11))
        doc.setDefaultStyleSheet(_PREVIEW_CSS)
        root.addWidget(self.view, 1)

    # ------------------------------------------------------------------
    def retranslate(self) -> None:
        """按当前界面语言刷新所有文案（切换语言时会被调用）。"""
        self.copy_btn.setText(i18n.tr("btn_copy"))
        self.dir_btn.setText(i18n.tr("btn_open_dir"))
        self.close_btn.setText(i18n.tr("btn_close"))
        self.close_btn.setToolTip(i18n.tr("close_tooltip"))
        self.view.setPlaceholderText(i18n.tr("preview_placeholder"))
        self._apply_titles()

    def _apply_titles(self) -> None:
        if self._md_path is not None:
            self.setWindowTitle(i18n.tr("preview_win_title_fmt", name=self._md_path.name))
            self.title_label.setText(self._title or self._md_path.stem)
        else:
            self.setWindowTitle(i18n.tr("preview_title"))
            self.title_label.setText(i18n.tr("preview_title"))

    def _place_right_half(self) -> None:
        """高度占满可用屏幕，宽度取屏幕的一半，靠右摆放。"""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        width = max(520, int(geo.width() * 0.5))
        self.setGeometry(geo.x() + geo.width() - width, geo.y(), width, geo.height())

    # ------------------------------------------------------------------
    def set_content(self, markdown: str, md_path: Path, title: str = "") -> None:
        self._markdown = markdown or ""
        self._md_path = Path(md_path)
        self._title = title or ""
        base_dir = self._md_path.parent

        self._apply_titles()

        # 让相对图片路径能解析到输出目录
        self.view.setSearchPaths([str(base_dir)])
        base_url = QUrl.fromLocalFile(str(base_dir) + os.sep)
        self.view.document().setBaseUrl(base_url)
        body = _absolutize(_strip_frontmatter(self._markdown), base_dir)
        self.view.setMarkdown(body)
        self.view.document().setBaseUrl(base_url)
        self.view.verticalScrollBar().setValue(0)
        # 布局完成后再按窗口宽度限制配图大小
        self._img_timer.start(0)

    def show_and_raise(self) -> None:
        self._place_right_half()
        self.show()
        self.raise_()
        self.activateWindow()
        self._img_timer.start(0)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._img_timer.start()

    # ------------------------------------------------------------------
    def _max_image_width(self) -> int:
        w = self.view.viewport().width()
        if w <= 0:
            return self.MAX_IMAGE_WIDTH
        return max(280, min(int(w * 0.86), self.MAX_IMAGE_WIDTH))

    @staticmethod
    def _natural_size(src: str):
        """读取图片原始尺寸；网络图/取不到时返回 None。"""
        if not src or src.startswith(("http://", "https://", "data:")):
            return None
        path = QUrl(src).toLocalFile() if src.startswith("file:") else src
        img = QImage(path)
        if img.isNull():
            return None
        return img.width(), img.height()

    def _apply_image_sizes(self) -> None:
        """把所有配图等比缩到合适宽度，并把「只有图片」的段落居中。"""
        doc = self.view.document()
        if doc.isEmpty():
            return
        maxw = self._max_image_width()
        changes = []          # (pos, length, QTextImageFormat)
        center_blocks = []    # 需要居中的段落号

        block = doc.begin()
        while block.isValid():
            has_image = False
            only_image = True
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid() and frag.charFormat().isImageFormat():
                    has_image = True
                    fmt = frag.charFormat().toImageFormat()
                    nat = self._natural_size(fmt.name())
                    if nat:
                        nw, nh = nat
                        if nw > maxw:
                            nf = QTextImageFormat(fmt)
                            nf.setWidth(maxw)
                            nf.setHeight(int(round(nh * maxw / nw)))
                            changes.append((frag.position(), frag.length(), nf))
                elif frag.isValid() and frag.text().strip():
                    only_image = False
                it += 1
            if has_image and only_image:
                center_blocks.append(block.blockNumber())
            block = block.next()

        for pos, length, nf in changes:
            cur = QTextCursor(doc)
            cur.setPosition(pos)
            cur.setPosition(pos + length, QTextCursor.KeepAnchor)
            cur.setCharFormat(nf)

        for bn in center_blocks:
            blk = doc.findBlockByNumber(bn)
            if not blk.isValid():
                continue
            cur = QTextCursor(blk)
            bf = QTextBlockFormat()
            bf.setAlignment(Qt.AlignHCenter)
            bf.setTopMargin(8)
            bf.setBottomMargin(8)
            cur.setBlockFormat(bf)

    # ------------------------------------------------------------------
    def _on_copy(self) -> None:
        if self._markdown:
            QGuiApplication.clipboard().setText(self._markdown)

    def _on_open_dir(self) -> None:
        if self._md_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._md_path.parent)))
