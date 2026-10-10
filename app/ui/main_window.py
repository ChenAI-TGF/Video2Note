"""主窗口：极简界面。配置项集中在「设置」弹窗里，需要时才打开。"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core.config import (
    ARTICLE_LENGTH_MAX,
    ARTICLE_LENGTH_MIN,
    DEFAULT_ARTICLE_LENGTH,
    DEFAULT_OUTPUT_LANGUAGE,
    OUTPUT_LANGUAGES,
    AppConfig,
    output_language_name,
)
from ..core import cache
from ..core.downloader import extract_url
from ..core.pipeline import PipelineResult, regenerate_from_cache, run_pipeline
from . import i18n
from .preview_window import PreviewWindow

# 支持的视频源小图标：(i18n key, 底色, 文字色)
SOURCE_BADGES = (
    ("douyin", "#161823", "#25F4EE"),
    ("bilibili", "#FB7299", "#ffffff"),
    ("ted", "#E62B1E", "#ffffff"),
    ("tiktok", "#010101", "#25F4EE"),
    ("kuaishou", "#FF6A00", "#ffffff"),
    ("xiaohongshu", "#FF2442", "#ffffff"),
    ("weibo", "#E6162D", "#ffffff"),
    ("youtube", "#FF0000", "#ffffff"),
)
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".m4v", ".ts", ".avi", ".wmv"}


def detect_local_video(text: str) -> Path | None:
    """若文本指向一个存在的本地视频文件，返回其路径，否则返回 None。"""
    t = (text or "").strip().strip('"').strip("'")
    if not t:
        return None
    p = Path(t)
    if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
        return p
    return None


def looks_like_path(text: str) -> bool:
    t = (text or "").strip().strip('"').strip("'")
    if not t:
        return False
    if t.lower().endswith(tuple(VIDEO_EXTS)):
        return True
    return bool(re.match(r"^[A-Za-z]:[\\/]", t) or t.startswith("\\\\") or t.startswith("/"))


class Worker(QThread):
    """在后台线程执行流水线，避免界面卡死。"""

    progress = Signal(int, int, str, int, int)   # 总进度, 步骤内进度(-1=不定态), 说明, 第几步, 共几步
    log = Signal(str)
    ok = Signal(object)
    fail = Signal(str)

    def __init__(
        self,
        url: str,
        cfg: AppConfig,
        local_video: Path | None = None,
        reuse: bool = False,
    ) -> None:
        super().__init__()
        self._url = url
        self._cfg = cfg
        self._local_video = local_video
        self._reuse = reuse   # True 时复用上次结果，仅重跑「整理 + 导出」

    def run(self) -> None:  # noqa: D102
        try:
            if self._reuse:
                result = regenerate_from_cache(
                    self._cfg,
                    on_progress=lambda o, s, m, i, t: self.progress.emit(o, s, m, i, t),
                    on_log=lambda m: self.log.emit(m),
                )
            else:
                result = run_pipeline(
                    self._url,
                    self._cfg,
                    on_progress=lambda o, s, m, i, t: self.progress.emit(o, s, m, i, t),
                    on_log=lambda m: self.log.emit(m),
                    local_video=self._local_video,
                )
            self.ok.emit(result)
        except Exception as exc:  # noqa: BLE001
            self.fail.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.resize(980, 780)
        self.setAcceptDrops(True)   # 支持把本地视频拖进来

        self.cfg = AppConfig.load()
        i18n.set_language(self.cfg.ui_language)   # 按配置恢复界面语言
        # 本版本固定使用「笔记模式」（不再提供博客 / 笔记切换）
        self.cfg.summary_mode = "note"

        self.worker: Worker | None = None
        self.last_result: PipelineResult | None = None
        self.preview_window: PreviewWindow | None = None
        self._is_regen = False   # 当前/上次任务是否为「复用上次结果重生成」

        self._build_ui()
        self._retranslate()          # 统一填充/刷新所有文案
        self._sync_ui_language_combo()
        self._sync_language_combo()
        self._sync_image_combo()
        self._sync_cover()
        self._sync_length_slider()
        self._refresh_summary()
        self._refresh_buttons()
        self.statusBar().showMessage(i18n.tr("status_ready"))

    # ==================================================================
    # 界面
    # ==================================================================
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 18, 20, 14)
        root.setSpacing(14)

        root.addLayout(self._build_header())
        root.addWidget(self._build_input_card())
        root.addLayout(self._build_progress())
        root.addWidget(self._build_log_view(), 1)
        root.addLayout(self._build_bottom_bar())

    # ---- 顶部标题栏 ----
    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        col = QVBoxLayout()
        col.setSpacing(2)
        self.title_label = QLabel()
        self.title_label.setObjectName("AppTitle")
        self.sub_label = QLabel()
        self.sub_label.setObjectName("AppSub")
        col.addWidget(self.title_label)
        col.addWidget(self.sub_label)

        # 支持的视频源小图标（抖音 / bilibili / TED / TikTok / 快手 / 小红书 / 微博 / YouTube）
        src_row = QHBoxLayout()
        src_row.setContentsMargins(0, 6, 0, 0)
        src_row.setSpacing(6)
        self.src_lbl = QLabel()
        self.src_lbl.setObjectName("AppSub")
        src_row.addWidget(self.src_lbl)
        self._badges: dict[str, QLabel] = {}
        for key, bg, fg in SOURCE_BADGES:
            label = QLabel()
            label.setStyleSheet(
                f"QLabel {{ background: {bg}; color: {fg}; border-radius: 8px;"
                f" padding: 1px 9px; font-size: 11px; font-weight: 600; }}"
            )
            self._badges[key] = label
            src_row.addWidget(label)
        src_row.addStretch(1)
        col.addLayout(src_row)

        row.addLayout(col)
        row.addStretch(1)

        # 界面语言选择器（配置修改统一在 setup 里，主界面不再放「设置」按钮）
        self.ui_lang_combo = QComboBox()
        self.ui_lang_combo.setMinimumHeight(30)
        self.ui_lang_combo.setMinimumWidth(128)
        for code, name in i18n.UI_LANGUAGES:
            self.ui_lang_combo.addItem(name, code)
        self.ui_lang_combo.currentIndexChanged.connect(self._on_ui_language_changed)
        row.addWidget(self.ui_lang_combo, 0, Qt.AlignTop)
        return row

    # ---- 输入卡片 ----
    def _build_input_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.url_edit = QLineEdit()
        self.url_edit.setObjectName("UrlInput")
        self.url_edit.setClearButtonEnabled(True)
        self.url_edit.setMinimumHeight(42)
        self.url_edit.returnPressed.connect(self._on_start)
        row.addWidget(self.url_edit, 1)

        self.paste_btn = QPushButton()
        self.paste_btn.setMinimumHeight(42)
        self.paste_btn.clicked.connect(self._on_paste)
        row.addWidget(self.paste_btn)

        self.choose_btn = QPushButton()
        self.choose_btn.setMinimumHeight(42)
        self.choose_btn.clicked.connect(self._on_choose_video)
        row.addWidget(self.choose_btn)

        self.start_btn = QPushButton()
        self.start_btn.setObjectName("Primary")
        self.start_btn.setMinimumHeight(42)
        self.start_btn.setMinimumWidth(120)
        self.start_btn.clicked.connect(self._on_start)
        row.addWidget(self.start_btn)
        layout.addLayout(row)

        # 行内选项：成稿语言 / 配图 / 保存封面（输出固定为「笔记模式」，不再提供博客/笔记切换）
        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.lang_lbl = QLabel()
        self.lang_lbl.setObjectName("Percent")
        mode_row.addWidget(self.lang_lbl)
        self.lang_combo = QComboBox()
        self.lang_combo.setMinimumHeight(30)
        self.lang_combo.setMinimumWidth(96)
        for code, name in OUTPUT_LANGUAGES:
            self.lang_combo.addItem(name, code)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        mode_row.addWidget(self.lang_combo)

        # 配图：不带配图 / 1~8 张，直接在主界面选
        mode_row.addSpacing(16)
        self.img_lbl = QLabel()
        self.img_lbl.setObjectName("Percent")
        mode_row.addWidget(self.img_lbl)
        self.image_combo = QComboBox()
        self.image_combo.setMinimumHeight(30)
        self.image_combo.setMinimumWidth(96)
        self.image_combo.currentIndexChanged.connect(self._on_images_changed)
        mode_row.addWidget(self.image_combo)
        # 保存封面：独立选项，不计入配图张数（即使配图选「不带配图」也可单独保存）
        mode_row.addSpacing(16)
        self.cover_check = QCheckBox()
        self.cover_check.setMinimumHeight(30)
        self.cover_check.toggled.connect(self._on_cover_toggled)
        mode_row.addWidget(self.cover_check, 0, Qt.AlignVCenter)

        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        # 篇幅：五档滑块（左短右长，默认「中等偏多」）
        len_row = QHBoxLayout()
        len_row.setSpacing(8)
        self.len_lbl = QLabel()
        self.len_lbl.setObjectName("Percent")
        len_row.addWidget(self.len_lbl)
        self.length_slider = QSlider(Qt.Horizontal)
        self.length_slider.setObjectName("LengthSlider")
        self.length_slider.setMinimum(ARTICLE_LENGTH_MIN)
        self.length_slider.setMaximum(ARTICLE_LENGTH_MAX)
        self.length_slider.setSingleStep(1)
        self.length_slider.setPageStep(1)
        self.length_slider.setTickPosition(QSlider.TicksBelow)
        self.length_slider.setTickInterval(1)
        self.length_slider.setFixedWidth(190)
        self.length_slider.valueChanged.connect(self._on_length_changed)
        len_row.addWidget(self.length_slider)
        self.length_val_label = QLabel()
        self.length_val_label.setObjectName("Percent")
        self.length_val_label.setMinimumWidth(60)
        len_row.addWidget(self.length_val_label)
        len_row.addStretch(1)
        layout.addLayout(len_row)

        # 配置摘要（不用打开设置也能知道当前状态）
        self.summary_label = QLabel()
        self.summary_label.setObjectName("Summary")
        self.summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.summary_label)
        return card

    # ---- 进度区（双进度条）----
    def _build_progress(self) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(5)

        # —— 上层：六个步骤的总进度 ——
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self.stage_label = QLabel()
        self.stage_label.setObjectName("Stage")
        row1.addWidget(self.stage_label, 1)
        self.step_label = QLabel()
        self.step_label.setObjectName("Percent")
        row1.addWidget(self.step_label)
        box.addLayout(row1)

        self.step_bar = QProgressBar()
        self.step_bar.setObjectName("StepBar")
        self.step_bar.setRange(0, 100)
        self.step_bar.setValue(0)
        self.step_bar.setTextVisible(True)
        self.step_bar.setMinimumHeight(18)
        box.addWidget(self.step_bar)

        # —— 下层：当前步骤内部的百分进度 ——
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.sub_label = QLabel()
        self.sub_label.setObjectName("Percent")
        row2.addWidget(self.sub_label, 1)
        self.sub_pct_label = QLabel("")
        self.sub_pct_label.setObjectName("Percent")
        row2.addWidget(self.sub_pct_label)
        box.addLayout(row2)

        self.sub_bar = QProgressBar()
        self.sub_bar.setObjectName("SubBar")
        self.sub_bar.setRange(0, 100)
        self.sub_bar.setValue(0)
        self.sub_bar.setTextVisible(False)
        self.sub_bar.setMinimumHeight(8)
        box.addWidget(self.sub_bar)
        return box

    # ---- 运行日志 ----
    def _build_log_view(self) -> QPlainTextEdit:
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(8000)
        log_font = QFont("Cascadia Mono", 9)
        log_font.setStyleHint(QFont.Monospace)
        self.log_view.setFont(log_font)
        return self.log_view

    # ---- 底部按钮栏 ----
    def _build_bottom_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        self.clear_btn = QPushButton()
        self.clear_btn.setObjectName("Ghost")
        self.clear_btn.clicked.connect(lambda: self.log_view.clear())
        bar.addWidget(self.clear_btn)
        bar.addStretch(1)

        self.regen_btn = QPushButton()
        self.regen_btn.setObjectName("Ghost")
        self.regen_btn.setMinimumHeight(36)
        self.regen_btn.clicked.connect(self._on_regen)
        bar.addWidget(self.regen_btn)

        self.preview_btn = QPushButton()
        self.preview_btn.setObjectName("Primary")
        self.preview_btn.setMinimumHeight(36)
        self.preview_btn.clicked.connect(self._on_open_preview)
        bar.addWidget(self.preview_btn)

        self.copy_btn = QPushButton()
        self.copy_btn.clicked.connect(self._on_copy)
        bar.addWidget(self.copy_btn)

        self.open_dir_btn = QPushButton()
        self.open_dir_btn.clicked.connect(self._on_open_dir)
        bar.addWidget(self.open_dir_btn)
        return bar

    # ==================================================================
    # 多语言：统一填充 / 刷新文案
    # ==================================================================
    def _retranslate(self) -> None:
        self.setWindowTitle(i18n.tr("app_title"))
        self.title_label.setText(i18n.tr("app_title"))
        self.sub_label.setText(i18n.tr("app_subtitle"))
        self.src_lbl.setText(i18n.tr("sources_label"))
        for key, label in self._badges.items():
            label.setText(i18n.tr(f"badge_{key}"))
            label.setToolTip(i18n.tr(f"badge_{key}_tip"))

        self.ui_lang_combo.setToolTip(i18n.tr("ui_lang_tooltip"))

        self.url_edit.setPlaceholderText(i18n.tr("placeholder"))
        self.paste_btn.setText(i18n.tr("btn_paste"))
        self.choose_btn.setText(i18n.tr("btn_choose"))
        self.choose_btn.setToolTip(i18n.tr("btn_choose_tooltip"))
        self.start_btn.setText(i18n.tr("btn_start"))

        self.lang_lbl.setText(i18n.tr("label_output_lang"))
        self.lang_combo.setToolTip(i18n.tr("output_lang_tooltip"))
        self.img_lbl.setText(i18n.tr("label_images"))
        self._fill_image_combo()
        self.image_combo.setToolTip(i18n.tr("images_tooltip"))
        self.cover_check.setText(i18n.tr("chk_cover_short"))
        self.cover_check.setToolTip(i18n.tr("chk_cover_tooltip"))
        self.len_lbl.setText(i18n.tr("label_length"))
        self.length_slider.setToolTip(i18n.tr("length_tooltip"))
        self.length_val_label.setText(i18n.length_name(self.length_slider.value()))

        self.step_bar.setFormat(i18n.tr("overall_progress_format"))
        self.step_bar.setToolTip(i18n.tr("tip_overall_bar"))
        self.sub_bar.setToolTip(i18n.tr("tip_sub_bar"))
        self.log_view.setPlaceholderText(i18n.tr("log_placeholder"))

        self.clear_btn.setText(i18n.tr("btn_clear_log"))
        self.regen_btn.setText(i18n.tr("btn_regen"))
        self.regen_btn.setToolTip(i18n.tr("btn_regen_tooltip"))
        self.preview_btn.setText(i18n.tr("btn_preview"))
        self.preview_btn.setToolTip(i18n.tr("btn_preview_tooltip"))
        self.copy_btn.setText(i18n.tr("btn_copy"))
        self.open_dir_btn.setText(i18n.tr("btn_open_dir"))

        # 进度/状态区：仅在空闲时重置为基准文案，避免打断正在运行的任务
        self._refresh_buttons()
        running = self.worker is not None and self.worker.isRunning()
        if not running:
            if self.last_result is not None:
                self.stage_label.setText(i18n.tr("done_stage"))
                self.sub_pct_label.setText("100%")
            else:
                self.stage_label.setText(i18n.tr("stage_ready"))
                self.step_label.setText(i18n.tr("step_fmt", i=0, t=0))
                self.sub_label.setText(i18n.tr("current_step"))
                self.sub_pct_label.setText("")

        self._refresh_summary()
        if self.preview_window is not None:
            self.preview_window.retranslate()

    def _fill_image_combo(self) -> None:
        cur = self.image_combo.currentData()
        self.image_combo.blockSignals(True)
        self.image_combo.clear()
        self.image_combo.addItem(i18n.tr("images_none"), 0)
        for n in range(1, 9):
            self.image_combo.addItem(i18n.tr("images_count", n=n), n)
        idx = self.image_combo.findData(0 if cur is None else cur)
        self.image_combo.setCurrentIndex(max(0, idx))
        self.image_combo.blockSignals(False)

    # ==================================================================
    # 状态刷新
    # ==================================================================
    def _refresh_summary(self) -> None:
        c = self.cfg
        providers = c.effective_providers() if hasattr(c, "effective_providers") else []
        vision_ready = c.vision_ready() if hasattr(c, "vision_ready") else False
        parts = [i18n.tr("summary_asr_fmt", model=self._short_model(c.asr_model), device=c.asr_device)]
        parts.append(i18n.tr("summary_output_fmt", dir=(c.output_dir or "output/")))
        if c.use_llm and providers:
            first = providers[0]
            parts.append(i18n.tr(
                "summary_llm_fmt",
                name=first.get("name") or "LLM",
                model=first.get("model") or i18n.tr("label_model"),
            ))
            parts.append(i18n.tr("mode_note"))
            parts.append(i18n.tr("word_language") + " " + output_language_name(c.output_language))
            parts.append(i18n.tr("word_length") + " " + i18n.length_name(c.article_length))
            self.summary_label.setObjectName("Summary")
        else:
            parts.append(i18n.tr("summary_llm_off"))
            self.summary_label.setObjectName("SummaryWarn")
        if c.use_images:
            suffix = i18n.tr("summary_vision_suffix") if (c.use_vision and vision_ready) else ""
            parts.append(i18n.tr("summary_images_fmt", n=int(c.max_images)) + suffix)
        else:
            parts.append(i18n.tr("summary_images_off"))
        if not c.cookies_raw and not c.cookies_file and (c.cookies_browser or "none") == "none":
            parts.append(i18n.tr("summary_no_cookie"))
            self.summary_label.setObjectName("SummaryWarn")
        self.summary_label.setText(i18n.tr("summary_sep").join(parts))
        # 让样式表重新生效（objectName 改变后需要刷新）
        self.summary_label.style().unpolish(self.summary_label)
        self.summary_label.style().polish(self.summary_label)

    @staticmethod
    def _short_model(name: str) -> str:
        if "SenseVoice" in name:
            return "SenseVoice"
        if "paraformer" in name.lower():
            return "Paraformer"
        return name

    def _sync_ui_language_combo(self) -> None:
        idx = self.ui_lang_combo.findData(i18n.current_language())
        self.ui_lang_combo.blockSignals(True)
        self.ui_lang_combo.setCurrentIndex(max(0, idx))
        self.ui_lang_combo.blockSignals(False)

    def _sync_language_combo(self) -> None:
        code = (self.cfg.output_language or DEFAULT_OUTPUT_LANGUAGE).lower()
        idx = self.lang_combo.findData(code)
        if idx < 0:
            idx = self.lang_combo.findData(DEFAULT_OUTPUT_LANGUAGE)
        self.lang_combo.blockSignals(True)
        self.lang_combo.setCurrentIndex(max(0, idx))
        self.lang_combo.blockSignals(False)

    def _sync_image_combo(self) -> None:
        n = int(self.cfg.max_images or 0) if self.cfg.use_images else 0
        idx = self.image_combo.findData(max(0, min(8, n)))
        if idx < 0:
            idx = self.image_combo.findData(0)
        self.image_combo.blockSignals(True)
        self.image_combo.setCurrentIndex(max(0, idx))
        self.image_combo.blockSignals(False)

    def _sync_cover(self) -> None:
        self.cover_check.blockSignals(True)
        self.cover_check.setChecked(bool(self.cfg.download_cover))
        self.cover_check.blockSignals(False)

    def _sync_length_slider(self) -> None:
        try:
            level = int(self.cfg.article_length)
        except (TypeError, ValueError):
            level = DEFAULT_ARTICLE_LENGTH
        level = max(ARTICLE_LENGTH_MIN, min(ARTICLE_LENGTH_MAX, level))
        self.length_slider.blockSignals(True)
        self.length_slider.setValue(level)
        self.length_slider.blockSignals(False)
        self.length_val_label.setText(i18n.length_name(level))

    def _refresh_buttons(self) -> None:
        running = self.worker is not None and self.worker.isRunning()
        self.start_btn.setEnabled(not running)
        self.paste_btn.setEnabled(not running)
        self.choose_btn.setEnabled(not running)
        self.ui_lang_combo.setEnabled(not running)
        self.lang_combo.setEnabled(not running)
        self.image_combo.setEnabled(not running)
        self.cover_check.setEnabled(not running)
        self.length_slider.setEnabled(not running)
        self.start_btn.setText(i18n.tr("btn_start_running") if running else i18n.tr("btn_start"))
        self.preview_btn.setEnabled((self.last_result is not None) and not running)
        try:
            has_prev = cache.has_cache(self.cfg)
        except Exception:  # noqa: BLE001
            has_prev = False
        self.regen_btn.setEnabled(has_prev and not running)

    # ==================================================================
    # 事件
    # ==================================================================
    def _on_ui_language_changed(self) -> None:
        code = self.ui_lang_combo.currentData()
        if not code or code == self.cfg.ui_language:
            return
        self.cfg.ui_language = code
        self.cfg.save()
        i18n.set_language(code)
        self._retranslate()
        self.statusBar().showMessage(
            i18n.tr("msg_ui_lang_switched_fmt", name=i18n.ui_language_name(code))
        )

    def _on_language_changed(self) -> None:
        code = self.lang_combo.currentData()
        if not code or code == self.cfg.output_language:
            return
        self.cfg.output_language = code
        self.cfg.save()
        self._refresh_summary()
        self.statusBar().showMessage(
            i18n.tr("msg_output_lang_switched_fmt", name=output_language_name(code))
        )

    def _on_images_changed(self) -> None:
        n = int(self.image_combo.currentData() or 0)
        if n <= 0:
            if not self.cfg.use_images:
                return
            self.cfg.use_images = False
            self._refresh_summary()
            self.statusBar().showMessage(i18n.tr("msg_images_off"))
        else:
            if self.cfg.use_images and int(self.cfg.max_images) == n:
                return
            self.cfg.use_images = True
            self.cfg.max_images = n
            self._refresh_summary()
            self.statusBar().showMessage(i18n.tr("msg_images_set_fmt", n=n))
        self.cfg.save()

    def _on_cover_toggled(self, checked: bool) -> None:
        if bool(self.cfg.download_cover) == bool(checked):
            return
        self.cfg.download_cover = bool(checked)
        self.cfg.save()
        self._refresh_summary()
        self.statusBar().showMessage(
            i18n.tr("msg_cover_on" if checked else "msg_cover_off")
        )

    def _on_length_changed(self, value: int) -> None:
        level = max(ARTICLE_LENGTH_MIN, min(ARTICLE_LENGTH_MAX, int(value)))
        self.length_val_label.setText(i18n.length_name(level))
        if int(self.cfg.article_length or 0) == level:
            return
        self.cfg.article_length = level
        self.cfg.save()
        self._refresh_summary()
        self.statusBar().showMessage(i18n.tr("msg_length_switched_fmt", name=i18n.length_name(level)))

    def _on_paste(self) -> None:
        text = (QGuiApplication.clipboard().text() or "").strip()
        if not text:
            return
        # 剪贴板里可能是「0.79 复制打开抖音… https://v.douyin.com/xxx/…」这类分享口令，
        # 也可能是一段本地视频路径
        local = detect_local_video(text)
        if local is not None:
            self.url_edit.setText(str(local))
            self.statusBar().showMessage(i18n.tr("msg_local_detected"))
            return
        url = extract_url(text)
        if url:
            self.url_edit.setText(url)
            self.statusBar().showMessage(i18n.tr("msg_extracted_clipboard"))
        else:
            self.url_edit.setText(text)
            self.statusBar().showMessage(i18n.tr("msg_no_link_pasted"))

    def _on_start(self) -> None:
        raw = self.url_edit.text().strip()
        if not raw:
            QMessageBox.warning(self, i18n.tr("dlg_missing_input_title"), i18n.tr("dlg_missing_input_msg"))
            return

        # 输入可能是三种：本地视频路径 / 纯链接 / 整段分享口令
        local = detect_local_video(raw)
        url = ""
        if local is not None:
            self.statusBar().showMessage(i18n.tr("msg_local_selected"))
        else:
            url = extract_url(raw)
            if not url:
                if looks_like_path(raw):
                    QMessageBox.warning(
                        self, i18n.tr("dlg_local_missing_title"),
                        i18n.tr("dlg_local_missing_msg", path=raw),
                    )
                else:
                    QMessageBox.warning(
                        self,
                        i18n.tr("dlg_no_input_title"),
                        i18n.tr("dlg_no_input_msg"),
                    )
                return
            if url != raw:
                self.url_edit.setText(url)   # 把这一串直接变成网址显示出来
                self.statusBar().showMessage(i18n.tr("msg_extracted_share"))

        if self.cfg.use_llm and not self.cfg.api_ready():
            ret = QMessageBox.question(
                self,
                i18n.tr("dlg_no_key_title"),
                i18n.tr("dlg_no_key_msg"),
            )
            if ret != QMessageBox.Yes:
                return
            self.cfg.use_llm = False
            self.cfg.save()
            self._refresh_summary()

        self.last_result = None
        self._is_regen = False
        self._reset_progress()
        self.stage_label.setText(i18n.tr("preparing"))
        self.log_view.clear()
        self._refresh_buttons()

        self.worker = Worker(url, self.cfg, local_video=local)
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._append_log)
        self.worker.ok.connect(self._on_ok)
        self.worker.fail.connect(self._on_fail)
        self.worker.finished.connect(self._refresh_buttons)
        self.worker.start()
        self._refresh_buttons()

    def _on_regen(self) -> None:
        """用上次运行缓存里的转录稿与配图，直接重跑「整理 + 导出」。"""
        if not cache.has_cache(self.cfg):
            QMessageBox.information(self, i18n.tr("app_title"), i18n.tr("msg_regen_no_cache"))
            return
        self.last_result = None
        self._is_regen = True
        self._reset_progress()
        self.stage_label.setText(i18n.tr("preparing"))
        self.log_view.clear()
        self._refresh_buttons()

        self.worker = Worker("", self.cfg, reuse=True)
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._append_log)
        self.worker.ok.connect(self._on_ok)
        self.worker.fail.connect(self._on_fail)
        self.worker.finished.connect(self._refresh_buttons)
        self.worker.start()
        self._refresh_buttons()
        self.statusBar().showMessage(i18n.tr("msg_regen_start"))

    def _on_choose_video(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            i18n.tr("dlg_choose_title"),
            "",
            i18n.tr("filter_video"),
        )
        if not path:
            return
        self.url_edit.setText(path)
        self.url_edit.setToolTip(path)
        self.statusBar().showMessage(i18n.tr("msg_choose_hint"))

    # ---- 拖拽本地视频 ----
    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._first_video_path(event) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        path = self._first_video_path(event)
        if path is not None:
            self.url_edit.setText(str(path))
            self.url_edit.setToolTip(str(path))
            self.statusBar().showMessage(i18n.tr("msg_drop_hint"))
            event.acceptProposedAction()
        else:
            event.ignore()

    @staticmethod
    def _first_video_path(event) -> Path | None:
        """从拖拽事件里取第一个本地视频文件路径。"""
        mime = event.mimeData()
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            p = Path(url.toLocalFile())
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
                return p
        return None

    def _reset_progress(self) -> None:
        self.step_bar.setRange(0, 100)
        self.step_bar.setValue(0)
        self.step_label.setText(i18n.tr("step_fmt", i=0, t=0))
        self.sub_bar.setRange(0, 100)
        self.sub_bar.setValue(0)
        self.sub_label.setText(i18n.tr("current_step"))
        self.sub_pct_label.setText("")

    def _on_progress(self, overall: int, sub: int, msg: str, index: int, total: int) -> None:
        # 上层：总进度（第几步 + 跨步骤百分比）
        self.step_bar.setValue(overall)
        if total:
            self.step_label.setText(i18n.tr("step_fmt", i=index, t=total))
        if msg:
            self.stage_label.setText(msg)
            self.statusBar().showMessage(
                i18n.tr("msg_progress_bar_fmt", i=index, t=total, msg=msg, p=overall)
            )

        # 下层：步骤内部进度（-1 = 不定态，用忙碌动画表示“进行中”）
        if sub is None or sub < 0:
            if self.sub_bar.maximum() != 0:
                self.sub_bar.setRange(0, 0)   # 切换到忙碌态
            self.sub_pct_label.setText(i18n.tr("in_progress"))
        else:
            if self.sub_bar.maximum() == 0:
                self.sub_bar.setRange(0, 100)  # 从忙碌态切回确定态
            self.sub_bar.setValue(sub)
            self.sub_pct_label.setText(f"{sub}%")

    def _append_log(self, msg: str) -> None:
        stamp = _dt.datetime.now().strftime("%H:%M:%S")
        for line in str(msg).splitlines():
            self.log_view.appendPlainText(f"[{stamp}] {line}" if line.strip() else "")
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_ok(self, result: PipelineResult) -> None:
        self.last_result = result
        self.stage_label.setText(i18n.tr("done_stage"))
        self.step_bar.setRange(0, 100)
        self.step_bar.setValue(100)
        self.sub_bar.setRange(0, 100)
        self.sub_bar.setValue(100)
        self.sub_pct_label.setText("100%")
        self.statusBar().showMessage(
            i18n.tr("msg_regen_done_fmt" if self._is_regen else "msg_completed_fmt",
                    name=result.md_path.name)
        )
        # 预览窗口若已打开，顺手把内容刷新掉
        if self.preview_window is not None and self.preview_window.isVisible():
            self.preview_window.set_content(result.markdown, result.md_path, result.title)
        self._refresh_buttons()

    def _on_open_preview(self) -> None:
        if not self.last_result:
            QMessageBox.information(self, i18n.tr("dlg_no_content_title"), i18n.tr("dlg_no_content_msg"))
            return
        if self.preview_window is None:
            self.preview_window = PreviewWindow(self)
        self.preview_window.set_content(
            self.last_result.markdown, self.last_result.md_path, self.last_result.title
        )
        self.preview_window.show_and_raise()

    def _on_fail(self, message: str) -> None:
        self.stage_label.setText(i18n.tr("fail_stage"))
        if self.sub_bar.maximum() == 0:
            self.sub_bar.setRange(0, 100)   # 结束忙碌动画
        self.sub_pct_label.setText(i18n.tr("aborted"))
        self.statusBar().showMessage(i18n.tr("msg_failed"))
        self._append_log("")
        self._append_log("========== " + i18n.tr("fail_stage") + " ==========")
        self._append_log(message)
        QMessageBox.critical(self, i18n.tr("dlg_fail_title"), message)

    def _on_copy(self) -> None:
        if not self.last_result:
            QMessageBox.information(self, i18n.tr("dlg_no_content_title"), i18n.tr("dlg_no_content_msg"))
            return
        QGuiApplication.clipboard().setText(self.last_result.markdown)
        self.statusBar().showMessage(i18n.tr("msg_copied"))

    def _on_open_dir(self) -> None:
        target = self.last_result.md_path.parent if self.last_result else self.cfg.resolved_output_dir
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    # ==================================================================
    def closeEvent(self, event) -> None:  # noqa: N802
        if self.worker is not None and self.worker.isRunning():
            ret = QMessageBox.question(self, i18n.tr("dlg_running_title"), i18n.tr("dlg_running_msg"))
            if ret != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.terminate()
            self.worker.wait(2000)
        event.accept()
