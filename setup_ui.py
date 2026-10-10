"""配置向导（原生 tkinter 界面）。

为什么用 tkinter：PySide6 本身也可能还没装，而 tkinter 是 Python 自带的。
所以在「连界面库都还没配好」的情况下，也能双击 setup.bat / 运行 `python setup_ui.py`
打开这个界面，分三步把环境配好。

三个步骤
--------
1. 系统配置：检查 requirements.txt 里的包，缺的自动 pip 安装；附带输出/运行选项。
2. 视频源与 Cookie：勾选需要的视频源（分「必须 Cookie」「可不配」两组），配置 Cookie。
3. API 配置：参考 ApiCheck 的供应商目录，可配置多个大模型做兜底；DeepSeek 置顶推荐，
   带「识图」标记的供应商可用于配图核对。

其它
----
* 右上角可切换界面语言（复用 app/ui/i18n.py）；视频源名称会随语言本地化
  （英文/日文等语言下显示 Douyin / Xiaohongshu 这类拉丁名）。
* 左侧步骤可点击切换；已完成的步骤显示为绿色，未完成显示为灰色。
* 可以反复打开修改（配置持久化在项目根目录的 config.json）。
* 只依赖标准库 + app/core/*（纯标准库），因此不导入 PySide6。
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tkinter as tk  # noqa: E402
from tkinter import filedialog, font as tkfont, messagebox, ttk  # noqa: E402

from app.core import deps as deps_mod  # noqa: E402
from app.core.config import (  # noqa: E402
    API_PROVIDER_CATALOG,
    AppConfig,
    VIDEO_SOURCES,
    new_provider_entry,
)
from app.ui import i18n  # noqa: E402

# ----------------------------------------------------------------------
# 配色 / 字体
# ----------------------------------------------------------------------
FONT = "Microsoft YaHei UI"
COL_BG = "#f4f5f7"
COL_CARD = "#ffffff"
COL_BORDER = "#e6e8ec"
COL_TEXT = "#1f2329"
COL_MUTED = "#6b7280"
COL_PRIMARY = "#4b6ef5"
COL_GREEN = "#2bb6a3"
COL_GREEN_BG = "#e3f6f1"
COL_GREEN_TEXT = "#0f766e"
COL_GRAY_BG = "#eef0f4"
COL_GRAY_TEXT = "#5b6472"
COL_WARN = "#d97706"

# 字号（比早期版本整体放大，便于阅读）
FS_TITLE = 19
FS_SUBTITLE = 11
FS_NAV = 13
FS_NAV_STATUS = 10
FS_SECTION = 14
FS_BODY = 11
FS_BTN = 12
FS_SMALL = 10

ASR_MODELS = [
    "iic/SenseVoiceSmall",
    "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
]
BROWSERS = ["edge", "chrome", "firefox", "none"]


def setup_default_fonts() -> None:
    """把 ttk 默认字体调大，避免 Entry / Combobox / Treeview 里的字很小。"""
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(name).configure(family=FONT, size=FS_BODY)
        except Exception:  # noqa: BLE001
            pass


class ScrollFrame(ttk.Frame):
    """一个可纵向滚动的容器；把子控件都塞进 ``.inner``。"""

    def __init__(self, master, bg: str = COL_CARD) -> None:
        super().__init__(master)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_inner(self, _e=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, e) -> None:
        self.canvas.itemconfigure(self._win, width=e.width)

    def _on_wheel(self, e) -> None:
        self.canvas.yview_scroll(int(-e.delta / 120), "units")


class SetupApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.cfg = AppConfig.load()
        i18n.set_language(getattr(self.cfg, "ui_language", "") or "en")

        self.current = 1
        self.done = {1: False, 2: False, 3: False}
        self.installing = False
        self._install_queue = None
        self._prov_state = []          # 当前编辑中的供应商（含 tk 变量）
        self._cards = []               # 供应商卡片所在的行容器

        self._build()
        self.refresh_steps()
        self.show_step(1)

    # ==================================================================
    # 界面骨架
    # ==================================================================
    def _build(self) -> None:
        r = self.root
        r.title(i18n.tr("setup_title"))
        r.configure(bg=COL_BG)
        r.geometry("1060x820")
        r.minsize(940, 700)

        header = tk.Frame(r, bg=COL_BG)
        header.pack(fill="x", padx=20, pady=(16, 6))

        left = tk.Frame(header, bg=COL_BG)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=i18n.tr("setup_title"), font=(FONT, FS_TITLE, "bold"),
                 bg=COL_BG, fg=COL_TEXT, anchor="w").pack(anchor="w")
        tk.Label(left, text=i18n.tr("setup_subtitle"), font=(FONT, FS_SUBTITLE),
                 bg=COL_BG, fg=COL_MUTED, anchor="w", justify="left",
                 wraplength=700).pack(anchor="w", pady=(4, 0))

        right = tk.Frame(header, bg=COL_BG)
        right.pack(side="right", anchor="ne")
        tk.Label(right, text=i18n.tr("setup_ui_lang"), font=(FONT, FS_BODY),
                 bg=COL_BG, fg=COL_MUTED).pack(side="left", padx=(0, 8))
        self.lang_combo = ttk.Combobox(
            right, state="readonly", width=13,
            values=[name for _c, name in i18n.UI_LANGUAGES],
        )
        self.lang_combo.set(i18n.ui_language_name(i18n.current_language()))
        self.lang_combo.bind("<<ComboboxSelected>>", self._on_language)
        self.lang_combo.pack(side="left")

        tk.Label(r, text=i18n.tr("setup_nav_hint"), font=(FONT, FS_SUBTITLE),
                 bg=COL_BG, fg=COL_MUTED, anchor="w").pack(fill="x", padx=20)

        body = tk.Frame(r, bg=COL_BG)
        body.pack(fill="both", expand=True, padx=20, pady=12)

        # ---- 左侧步骤导航 ----
        nav = tk.Frame(body, bg=COL_BG, width=260)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)

        self.nav_btn = {}
        self.nav_status = {}
        for i in (1, 2, 3):
            box = tk.Frame(nav, bg=COL_BG)
            box.pack(fill="x", pady=5)
            btn = tk.Button(
                box, text=i18n.tr(f"step{i}_name"), font=(FONT, FS_NAV, "bold"),
                anchor="w", relief="flat", bd=0, padx=16, pady=12,
                cursor="hand2", command=lambda n=i: self.show_step(n),
            )
            btn.pack(fill="x")
            status = tk.Label(box, text="", font=(FONT, FS_NAV_STATUS), bg=COL_BG,
                              fg=COL_MUTED, anchor="w", padx=18)
            status.pack(fill="x")
            self.nav_btn[i] = btn
            self.nav_status[i] = status

        # ---- 右侧内容区 ----
        self.content = tk.Frame(body, bg=COL_CARD, highlightbackground=COL_BORDER,
                                highlightthickness=1)
        self.content.pack(side="left", fill="both", expand=True, padx=(16, 0))

        self.frames = {}
        self.frames[1] = self._build_step1(self.content)
        self.frames[2] = self._build_step2(self.content)
        self.frames[3] = self._build_step3(self.content)

        # ---- 底部 ----
        foot = tk.Frame(r, bg=COL_BG)
        foot.pack(fill="x", padx=20, pady=(4, 14))
        tk.Label(foot, text=f"config.json · {ROOT}", font=(FONT, FS_SMALL),
                 bg=COL_BG, fg=COL_MUTED).pack(side="left")
        self.status_label = tk.Label(foot, text="", font=(FONT, FS_BODY), bg=COL_BG,
                                     fg=COL_GREEN)
        self.status_label.pack(side="left", padx=16)
        tk.Button(foot, text=i18n.tr("setup_btn_close"), font=(FONT, FS_BTN),
                  relief="flat", bd=0, padx=20, pady=8, bg=COL_PRIMARY,
                  fg="white", activebackground="#3f61e6", activeforeground="white",
                  cursor="hand2", command=self.root.destroy).pack(side="right")

    # ---- 小工具 ----
    def _section(self, parent, text: str) -> tk.Label:
        lbl = tk.Label(parent, text=text, font=(FONT, FS_SECTION, "bold"),
                       bg=COL_CARD, fg=COL_TEXT, anchor="w")
        lbl.pack(fill="x", pady=(12, 6))
        return lbl

    def _hint(self, parent, text: str, fg: str = COL_MUTED) -> tk.Label:
        return tk.Label(parent, text=text, font=(FONT, FS_BODY), bg=COL_CARD,
                        fg=fg, anchor="w", justify="left", wraplength=720)

    def _primary_btn(self, parent, text: str, command) -> tk.Button:
        return tk.Button(parent, text=text, command=command, font=(FONT, FS_BTN),
                         relief="flat", bd=0, padx=18, pady=8, bg=COL_PRIMARY,
                         fg="white", activebackground="#3f61e6",
                         activeforeground="white", cursor="hand2")

    def _ghost_btn(self, parent, text: str, command) -> tk.Button:
        return tk.Button(parent, text=text, command=command, font=(FONT, FS_BTN),
                         relief="solid", bd=1, padx=16, pady=7, bg=COL_CARD,
                         fg=COL_TEXT, activebackground=COL_GRAY_BG,
                         highlightbackground=COL_BORDER, cursor="hand2")

    # ---- 视频源本地化名称 / 备注 ----
    @staticmethod
    def _src_name(src: dict) -> str:
        key = f'badge_{src["id"]}'
        val = i18n.tr(key)
        return val if val != key else src["name"]

    @staticmethod
    def _src_note(src: dict) -> str:
        key = f'srcnote_{src["id"]}'
        val = i18n.tr(key)
        return val if val != key else src.get("note", "")

    # ==================================================================
    # 第 1 步：系统配置（依赖 + 输出/运行选项）
    # ==================================================================
    def _build_step1(self, parent) -> tk.Frame:
        frame = tk.Frame(parent, bg=COL_CARD)

        wrap = ScrollFrame(frame, COL_CARD)
        wrap.pack(fill="both", expand=True, padx=18, pady=16)
        body = wrap.inner

        self._section(body, i18n.tr("s1_deps_title"))
        self._hint(body, i18n.tr("s1_deps_hint")).pack(fill="x")

        cols = ("pkg", "use", "status")
        style = ttk.Style()
        try:
            style.configure("Setup.Treeview", rowheight=30, font=(FONT, FS_BODY))
            style.configure("Setup.Treeview.Heading", font=(FONT, FS_BODY, "bold"))
        except Exception:  # noqa: BLE001
            pass
        tree = ttk.Treeview(body, columns=cols, show="headings", height=9,
                            style="Setup.Treeview")
        tree.heading("pkg", text=i18n.tr("s1_col_pkg"))
        tree.heading("use", text=i18n.tr("s1_col_use"))
        tree.heading("status", text=i18n.tr("s1_col_status"))
        tree.column("pkg", width=220, anchor="w")
        tree.column("use", width=280, anchor="w")
        tree.column("status", width=130, anchor="center")
        tree.tag_configure("ok", foreground=COL_GREEN)
        tree.tag_configure("miss", foreground=COL_WARN)
        tree.pack(fill="x", pady=(8, 8))
        self.dep_tree = tree

        self.dep_summary = tk.Label(body, text="", font=(FONT, FS_BODY), bg=COL_CARD,
                                    fg=COL_MUTED, anchor="w")
        self.dep_summary.pack(fill="x")

        btns = tk.Frame(body, bg=COL_CARD)
        btns.pack(fill="x", pady=(10, 8))
        self.btn_recheck = self._ghost_btn(btns, i18n.tr("s1_btn_recheck"), self._populate_deps)
        self.btn_recheck.pack(side="left")
        self.btn_install = self._primary_btn(btns, i18n.tr("s1_btn_install"), self._install_missing)
        self.btn_install.pack(side="left", padx=8)

        self.log_text = tk.Text(body, height=9, font=("Consolas", FS_SMALL), bg="#1e222a",
                                fg="#d6dae2", insertbackground="#d6dae2", bd=0,
                                highlightthickness=0, wrap="none")
        self.log_text.pack(fill="both", expand=True, pady=(4, 12))
        self.log_text.configure(state="disabled")
        self._set_log(i18n.tr("s1_deps_hint"))

        # ---- 输出与运行设置 ----
        self._section(body, i18n.tr("s1_opts_title"))
        opts = tk.Frame(body, bg=COL_CARD)
        opts.pack(fill="x")
        opts.columnconfigure(1, weight=1)

        self.v_outdir = tk.StringVar()
        self.v_ffmpeg = tk.StringVar()
        self.v_asr_model = tk.StringVar()
        self.v_asr_device = tk.StringVar()

        r = 0
        self._opt_row(opts, r, i18n.tr("s1_outdir"), self.v_outdir, browse="dir"); r += 1
        self._opt_row(opts, r, i18n.tr("s1_ffmpeg"), self.v_ffmpeg, browse="file"); r += 1
        self._opt_row(opts, r, i18n.tr("s1_asr_model"), self.v_asr_model, values=ASR_MODELS); r += 1
        self._opt_row(opts, r, i18n.tr("s1_asr_device"), self.v_asr_device,
                      values=["cpu", "cuda:0"]); r += 1

        self.v_keep = tk.BooleanVar()
        self.v_transcript = tk.BooleanVar()
        self.v_cover = tk.BooleanVar()
        self.v_vision = tk.BooleanVar()
        self.v_images = tk.BooleanVar()
        checks = tk.Frame(body, bg=COL_CARD)
        checks.pack(fill="x", pady=(10, 10))
        for var, key in (
            (self.v_images, "s1_use_images"),
            (self.v_vision, "s1_use_vision"),
            (self.v_cover, "s1_download_cover"),
            (self.v_keep, "s1_keep_video"),
            (self.v_transcript, "s1_include_transcript"),
        ):
            tk.Checkbutton(checks, text=i18n.tr(key), variable=var, bg=COL_CARD,
                           fg=COL_TEXT, font=(FONT, FS_BODY), anchor="w",
                           activebackground=COL_CARD, selectcolor=COL_CARD,
                           cursor="hand2").pack(side="left", padx=(0, 18))

        self._primary_btn(body, i18n.tr("setup_btn_save"), self._save_step1).pack(
            anchor="e", pady=(6, 14))

        self._load_step1()
        self._populate_deps()
        return frame

    def _opt_row(self, parent, row: int, label: str, var, browse=None, values=None) -> None:
        tk.Label(parent, text=label, font=(FONT, FS_BODY), bg=COL_CARD, fg=COL_TEXT,
                 anchor="w").grid(row=row, column=0, sticky="w", pady=4)
        if values is not None:
            cb = ttk.Combobox(parent, textvariable=var, values=values, width=52)
            cb.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=4)
            return
        ent = ttk.Entry(parent, textvariable=var)
        ent.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=4)
        if browse in ("dir", "file"):
            cmd = (lambda: self._browse_dir(var)) if browse == "dir" else (lambda: self._browse_file(var))
            tk.Button(parent, text=i18n.tr("s1_browse"), command=cmd,
                      relief="solid", bd=1, padx=12, pady=4, bg=COL_CARD,
                      font=(FONT, FS_BODY), cursor="hand2",
                      highlightbackground=COL_BORDER).grid(row=row, column=2, padx=(8, 0))

    def _browse_dir(self, var) -> None:
        path = filedialog.askdirectory(parent=self.root, initialdir=var.get() or str(ROOT))
        if path:
            var.set(path)

    def _browse_file(self, var) -> None:
        path = filedialog.askopenfilename(parent=self.root, initialdir=str(ROOT))
        if path:
            var.set(path)

    def _load_step1(self) -> None:
        c = self.cfg
        self.v_outdir.set(c.output_dir)
        self.v_ffmpeg.set(c.ffmpeg_path)
        self.v_asr_model.set(c.asr_model or ASR_MODELS[0])
        self.v_asr_device.set(c.asr_device or "cpu")
        self.v_keep.set(bool(c.keep_video))
        self.v_transcript.set(bool(c.include_transcript))
        self.v_cover.set(bool(c.download_cover))
        self.v_vision.set(bool(c.use_vision))
        self.v_images.set(bool(c.use_images))

    def _save_step1(self) -> None:
        c = self.cfg
        c.output_dir = self.v_outdir.get().strip()
        c.ffmpeg_path = self.v_ffmpeg.get().strip()
        c.asr_model = self.v_asr_model.get().strip() or ASR_MODELS[0]
        c.asr_device = self.v_asr_device.get().strip() or "cpu"
        c.keep_video = bool(self.v_keep.get())
        c.include_transcript = bool(self.v_transcript.get())
        c.download_cover = bool(self.v_cover.get())
        c.use_vision = bool(self.v_vision.get())
        c.use_images = bool(self.v_images.get())
        c.save()
        self.refresh_steps()
        self._status(i18n.tr("setup_saved"))

    # ---- 依赖相关 ----
    def _set_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", text)
        self.log_text.configure(state="disabled")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _populate_deps(self) -> None:
        for row in self.dep_tree.get_children():
            self.dep_tree.delete(row)
        for d in deps_mod.dep_status():
            ok = bool(d["installed"])
            self.dep_tree.insert(
                "", "end",
                values=(d["pip"], i18n.tr(d["purpose"]),
                        i18n.tr("s1_st_ok" if ok else "s1_st_missing")),
                tags=("ok" if ok else "miss",),
            )
        missing = deps_mod.missing_deps()
        if missing:
            self.dep_summary.configure(
                text=i18n.tr("s1_missing_fmt", list="、".join(missing)), fg=COL_WARN)
        else:
            self.dep_summary.configure(text=i18n.tr("s1_all_ok"), fg=COL_GREEN)
        self.refresh_steps()

    def _install_missing(self) -> None:
        missing = deps_mod.missing_deps()
        if not missing:
            messagebox.showinfo(i18n.tr("setup_title"), i18n.tr("s1_all_ok"), parent=self.root)
            return
        self.installing = True
        self.btn_install.configure(state="disabled")
        self.btn_recheck.configure(state="disabled")
        self.lang_combo.configure(state="disabled")
        self._set_log(i18n.tr("s1_installing") + "\n" + ", ".join(missing) + "\n")
        self._status(i18n.tr("s1_installing"))

        q: queue.Queue = queue.Queue()
        self._install_queue = q
        threading.Thread(target=self._install_worker, args=(missing, q), daemon=True).start()
        self.root.after(150, self._poll_install)

    def _pip_run(self, cmd, q: queue.Queue, env) -> int:
        q.put("$ " + " ".join(cmd))
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", env=env, bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            q.put(i18n.tr("s1_pip_fail", e=exc))
            return 1
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line.rstrip("\n"))
        proc.wait()
        return proc.returncode

    def _install_worker(self, missing, q: queue.Queue) -> None:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        code = self._pip_run([sys.executable, "-m", "pip", "install", *missing], q, env)
        if code != 0:
            q.put("")
            q.put(i18n.tr("s1_retry_isolation"))
            code = self._pip_run(
                [sys.executable, "-m", "pip", "install", "--no-build-isolation", *missing],
                q, env)
        q.put(("__DONE__", code))

    def _poll_install(self) -> None:
        q = self._install_queue
        if q is None:
            return
        try:
            while True:
                item = q.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__DONE__":
                    self._finish_install(int(item[1]))
                    return
                self._append_log(str(item))
        except queue.Empty:
            pass
        self.root.after(150, self._poll_install)

    def _finish_install(self, code: int) -> None:
        self._install_queue = None
        self.installing = False
        self.btn_install.configure(state="normal")
        self.btn_recheck.configure(state="normal")
        self.lang_combo.configure(state="readonly")
        self._populate_deps()
        if code == 0:
            self._append_log("")
            self._append_log("✔ " + i18n.tr("s1_install_done"))
            self._status(i18n.tr("s1_install_done"))
            messagebox.showinfo(i18n.tr("setup_title"), i18n.tr("s1_install_done"),
                                parent=self.root)
        else:
            self._append_log("")
            self._append_log("✘ " + i18n.tr("s1_install_fail"))
            self._status(i18n.tr("s1_install_fail"))
            messagebox.showwarning(i18n.tr("setup_title"), i18n.tr("s1_install_fail"),
                                   parent=self.root)

    # ==================================================================
    # 第 2 步：视频源与 Cookie
    # ==================================================================
    def _build_step2(self, parent) -> tk.Frame:
        frame = tk.Frame(parent, bg=COL_CARD)
        wrap = ScrollFrame(frame, COL_CARD)
        wrap.pack(fill="both", expand=True, padx=18, pady=16)
        body = wrap.inner

        self._section(body, i18n.tr("s2_sources_title"))
        self._hint(body, i18n.tr("s2_sources_hint")).pack(fill="x")

        self.src_vars = {}
        for group, key in (("required", "s2_group_required"),
                           ("optional", "s2_group_optional")):
            tk.Label(body, text=i18n.tr(key), font=(FONT, FS_BTN, "bold"), bg=COL_CARD,
                     fg=COL_PRIMARY if group == "required" else COL_MUTED,
                     anchor="w").pack(fill="x", pady=(12, 4))
            holder = tk.Frame(body, bg=COL_CARD)
            holder.pack(fill="x")
            for src in VIDEO_SOURCES:
                if src["group"] != group:
                    continue
                var = tk.BooleanVar(value=False)
                self.src_vars[src["id"]] = var
                name = self._src_name(src)
                cb = tk.Checkbutton(
                    holder, text=name, variable=var, bg=COL_CARD,
                    fg=COL_TEXT, font=(FONT, FS_BODY), anchor="w", cursor="hand2",
                    activebackground=COL_CARD, selectcolor=COL_CARD,
                    command=lambda s=src, v=var: self._on_source_toggle(s, v),
                )
                cb.pack(side="left", padx=(0, 16))
                cb.bind("<Enter>", lambda _e, s=src, w=cb: w.configure(
                    text=f"{self._src_name(s)} · {self._src_note(s)}"))
                cb.bind("<Leave>", lambda _e, s=src, w=cb, n=name: w.configure(text=n))

        self._section(body, i18n.tr("s2_cookie_title"))
        self._hint(body, i18n.tr("s2_cookie_hint")).pack(fill="x")

        tk.Label(body, text=i18n.tr("label_cookie_raw"), font=(FONT, FS_BODY),
                 bg=COL_CARD, fg=COL_TEXT, anchor="w").pack(fill="x", pady=(8, 2))
        self.cookie_raw = tk.Text(body, height=5, font=("Consolas", FS_SMALL), bg="#fbfbfd",
                                  fg=COL_TEXT, bd=1, relief="solid", wrap="word")
        self.cookie_raw.pack(fill="x")

        row = tk.Frame(body, bg=COL_CARD)
        row.pack(fill="x", pady=(10, 4))
        tk.Label(row, text=i18n.tr("label_cookie_browser"), font=(FONT, FS_BODY),
                 bg=COL_CARD, fg=COL_TEXT).pack(side="left")
        self.v_browser = tk.StringVar(value="edge")
        ttk.Combobox(row, textvariable=self.v_browser, values=BROWSERS,
                     state="readonly", width=14).pack(side="left", padx=(12, 0))

        row2 = tk.Frame(body, bg=COL_CARD)
        row2.pack(fill="x", pady=(6, 4))
        row2.columnconfigure(1, weight=1)
        tk.Label(row2, text=i18n.tr("label_cookie_file"), font=(FONT, FS_BODY),
                 bg=COL_CARD, fg=COL_TEXT).grid(row=0, column=0, sticky="w")
        self.v_cookiefile = tk.StringVar()
        ttk.Entry(row2, textvariable=self.v_cookiefile).grid(
            row=0, column=1, sticky="ew", padx=(12, 8))
        tk.Button(row2, text=i18n.tr("s1_browse"), command=self._browse_cookie,
                  relief="solid", bd=1, padx=12, pady=4, bg=COL_CARD, font=(FONT, FS_BODY),
                  cursor="hand2", highlightbackground=COL_BORDER).grid(row=0, column=2)

        self.s2_hint = tk.Label(body, text="", font=(FONT, FS_BODY), bg=COL_CARD,
                                fg=COL_WARN, anchor="w")
        self.s2_hint.pack(fill="x", pady=(8, 0))

        self._primary_btn(body, i18n.tr("setup_btn_save"), self._save_step2).pack(
            anchor="e", pady=(12, 14))

        self._load_step2()
        return frame

    def _browse_cookie(self) -> None:
        path = filedialog.askopenfilename(parent=self.root, initialdir=str(ROOT),
                                          filetypes=[("cookies.txt", "*.txt"), ("All", "*.*")])
        if path:
            self.v_cookiefile.set(path)

    def _cookie_present(self) -> bool:
        raw = self.cookie_raw.get("1.0", "end").strip()
        return bool(raw or self.v_cookiefile.get().strip())

    def _on_source_toggle(self, src, var) -> None:
        if not var.get():
            self._update_s2_hint()
            return
        # 勾选「必须 Cookie」来源时，如果已有 Cookie 配置，询问是否替换
        if src["group"] == "required" and self._cookie_present():
            if messagebox.askyesno(
                i18n.tr("s2_replace_title"),
                i18n.tr("s2_replace_msg", name=self._src_name(src)),
                parent=self.root,
            ):
                self.cookie_raw.delete("1.0", "end")
                self.v_cookiefile.set("")
        self._update_s2_hint()

    def _update_s2_hint(self) -> None:
        chosen = [s for s in VIDEO_SOURCES if self.src_vars.get(s["id"]) and self.src_vars[s["id"]].get()]
        if not chosen:
            self.s2_hint.configure(text=i18n.tr("s2_no_source_hint"), fg=COL_MUTED)
        else:
            names = "、".join(self._src_name(s) for s in chosen)
            self.s2_hint.configure(text=names, fg=COL_MUTED)

    def _load_step2(self) -> None:
        c = self.cfg
        enabled = set(c.enabled_sources or [])
        for sid, var in self.src_vars.items():
            var.set(sid in enabled)
        self.cookie_raw.delete("1.0", "end")
        self.cookie_raw.insert("1.0", c.cookies_raw or "")
        self.v_browser.set(c.cookies_browser or "edge")
        self.v_cookiefile.set(c.cookies_file or "")
        self._update_s2_hint()

    def _save_step2(self) -> None:
        c = self.cfg
        c.enabled_sources = [sid for sid, var in self.src_vars.items() if var.get()]
        c.cookies_raw = self.cookie_raw.get("1.0", "end").strip()
        c.cookies_browser = self.v_browser.get().strip() or "none"
        c.cookies_file = self.v_cookiefile.get().strip()
        c.save()
        self.refresh_steps()
        self._status(i18n.tr("setup_saved"))

    # ==================================================================
    # 第 3 步：API 配置
    # ==================================================================
    def _build_step3(self, parent) -> tk.Frame:
        frame = tk.Frame(parent, bg=COL_CARD)
        wrap = ScrollFrame(frame, COL_CARD)
        wrap.pack(fill="both", expand=True, padx=18, pady=16)
        body = wrap.inner

        self._section(body, i18n.tr("s3_title"))
        self._hint(body, i18n.tr("s3_hint")).pack(fill="x")

        self.v_use_llm = tk.BooleanVar(value=bool(self.cfg.use_llm))
        tk.Checkbutton(body, text=i18n.tr("s3_use_llm"), variable=self.v_use_llm,
                       bg=COL_CARD, fg=COL_TEXT, font=(FONT, FS_BODY), anchor="w",
                       activebackground=COL_CARD, selectcolor=COL_CARD,
                       cursor="hand2").pack(fill="x", pady=(8, 6))

        addrow = tk.Frame(body, bg=COL_CARD)
        addrow.pack(fill="x", pady=(2, 8))
        self.add_combo = ttk.Combobox(addrow, state="readonly", width=34)
        self.add_combo.pack(side="left")
        self._primary_btn(addrow, i18n.tr("s3_add"), self._add_catalog).pack(side="left", padx=8)
        self._ghost_btn(addrow, i18n.tr("s3_add_custom"), self._add_custom).pack(side="left")

        self.cards_holder = tk.Frame(body, bg=COL_CARD)
        self.cards_holder.pack(fill="x")
        self.s3_empty = tk.Label(body, text=i18n.tr("s3_empty"), font=(FONT, FS_BODY),
                                 bg=COL_CARD, fg=COL_MUTED, anchor="w")

        self._primary_btn(body, i18n.tr("setup_btn_save"), self._save_step3).pack(
            anchor="e", pady=(12, 14))

        self._load_step3()
        return frame

    def _load_step3(self) -> None:
        c = self.cfg
        providers = list(getattr(c, "api_providers", None) or [])
        if not providers:
            # 向后兼容旧的单 DeepSeek 字段
            entry = new_provider_entry("deepseek")
            entry["api_key"] = c.deepseek_api_key
            entry["base_url"] = c.deepseek_base_url or entry["base_url"]
            entry["model"] = c.deepseek_model or entry["model"]
            entry["vision_model"] = c.deepseek_vision_model or entry["vision_model"]
            providers = [entry]
        self._prov_state = [self._state_from_entry(e) for e in providers]
        self._render_cards()
        self._refresh_add_combo()

    def _state_from_entry(self, e: dict) -> dict:
        pid = str(e.get("id") or "custom")
        meta = next((p for p in API_PROVIDER_CATALOG if p["id"] == pid), {})
        return {
            "id": pid,
            "name": str(e.get("name") or meta.get("name") or pid),
            "recommended": bool(e.get("recommended", meta.get("recommended"))),
            "vision": bool(e.get("vision", meta.get("vision"))),
            "models": list(e.get("models") or meta.get("models") or []),
            "vision_models": list(e.get("vision_models") or meta.get("vision_models") or []),
            "v_enabled": tk.BooleanVar(value=bool(e.get("enabled", True))),
            "v_key": tk.StringVar(value=str(e.get("api_key") or "")),
            "v_base": tk.StringVar(value=str(e.get("base_url") or "")),
            "v_model": tk.StringVar(value=str(e.get("model") or "")),
            "v_vmodel": tk.StringVar(value=str(e.get("vision_model") or "")),
        }

    def _refresh_add_combo(self) -> None:
        used = {st["id"] for st in self._prov_state}
        names = [p["name"] for p in API_PROVIDER_CATALOG if p["id"] not in used]
        self.add_combo.configure(values=names)
        self.add_combo.set(names[0] if names else "")

    def _add_catalog(self) -> None:
        name = self.add_combo.get()
        meta = next((p for p in API_PROVIDER_CATALOG if p["name"] == name), None)
        if not meta:
            return
        self._prov_state.append(self._state_from_entry(new_provider_entry(meta["id"])))
        self._render_cards()
        self._refresh_add_combo()

    def _add_custom(self) -> None:
        dlg = _CustomProviderDialog(self.root)
        self.root.wait_window(dlg.top)
        if not dlg.result:
            return
        entry = {
            "id": "custom",
            "name": dlg.result["name"] or i18n.tr("s3_custom_name"),
            "base_url": dlg.result["base_url"],
            "api_key": "",
            "model": dlg.result["model"],
            "vision": dlg.result["vision"],
            "models": [m for m in [dlg.result["model"]] if m],
            "vision_models": [],
            "enabled": True,
        }
        self._prov_state.append(self._state_from_entry(entry))
        self._render_cards()
        self._refresh_add_combo()

    def _render_cards(self) -> None:
        for w in self.cards_holder.winfo_children():
            w.destroy()
        if not self._prov_state:
            self.s3_empty.pack(fill="x", pady=6)
            return
        self.s3_empty.pack_forget()
        for idx, st in enumerate(self._prov_state):
            self._build_card(idx, st)

    def _build_card(self, idx: int, st: dict) -> None:
        card = tk.Frame(self.cards_holder, bg=COL_CARD,
                        highlightbackground=COL_BORDER, highlightthickness=1)
        card.pack(fill="x", pady=7)

        top = tk.Frame(card, bg=COL_CARD)
        top.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(top, text=f'{idx + 1}. {st["name"]}', font=(FONT, FS_BTN, "bold"),
                 bg=COL_CARD, fg=COL_TEXT).pack(side="left")
        if st["recommended"]:
            tk.Label(top, text=i18n.tr("s3_tag_recommended"), font=(FONT, FS_SMALL, "bold"),
                     bg=COL_PRIMARY, fg="white", padx=8, pady=1).pack(side="left", padx=(10, 0))
        if st["vision"]:
            tk.Label(top, text=i18n.tr("s3_tag_vision"), font=(FONT, FS_SMALL, "bold"),
                     bg=COL_GREEN_BG, fg=COL_GREEN_TEXT, padx=8, pady=1).pack(side="left", padx=(6, 0))

        tk.Checkbutton(top, text=i18n.tr("s3_enable"), variable=st["v_enabled"],
                       bg=COL_CARD, fg=COL_TEXT, font=(FONT, FS_BODY), cursor="hand2",
                       activebackground=COL_CARD, selectcolor=COL_CARD).pack(side="right")
        tk.Button(top, text=i18n.tr("s3_remove"), command=lambda i=idx: self._remove_provider(i),
                  relief="flat", bd=0, padx=8, bg=COL_CARD, fg="#d9534f",
                  font=(FONT, FS_BODY), activebackground=COL_CARD,
                  cursor="hand2").pack(side="right")
        tk.Button(top, text=i18n.tr("s3_down"), command=lambda i=idx: self._move_provider(i, 1),
                  relief="flat", bd=0, padx=6, bg=COL_CARD, fg=COL_MUTED,
                  font=(FONT, FS_BODY), cursor="hand2").pack(side="right")
        tk.Button(top, text=i18n.tr("s3_up"), command=lambda i=idx: self._move_provider(i, -1),
                  relief="flat", bd=0, padx=6, bg=COL_CARD, fg=COL_MUTED,
                  font=(FONT, FS_BODY), cursor="hand2").pack(side="right")

        grid = tk.Frame(card, bg=COL_CARD)
        grid.pack(fill="x", padx=12, pady=(0, 12))
        grid.columnconfigure(1, weight=1)

        def add_row(row: int, label: str, widget) -> None:
            tk.Label(grid, text=label, font=(FONT, FS_BODY), bg=COL_CARD, fg=COL_TEXT,
                     anchor="w").grid(row=row, column=0, sticky="w", pady=3)
            widget.grid(row=row, column=1, sticky="ew", padx=(12, 0), pady=3)

        r = 0
        add_row(r, i18n.tr("s3_key"), ttk.Entry(grid, textvariable=st["v_key"], show="*")); r += 1
        add_row(r, i18n.tr("s3_base"), ttk.Entry(grid, textvariable=st["v_base"])); r += 1
        add_row(r, i18n.tr("s3_model"),
                ttk.Combobox(grid, textvariable=st["v_model"], values=st["models"] or [])); r += 1
        if st["vision"]:
            add_row(r, i18n.tr("s3_vision_model"),
                    ttk.Combobox(grid, textvariable=st["v_vmodel"], values=st["vision_models"] or [])); r += 1

    def _remove_provider(self, idx: int) -> None:
        if 0 <= idx < len(self._prov_state):
            del self._prov_state[idx]
            self._render_cards()
            self._refresh_add_combo()

    def _move_provider(self, idx: int, delta: int) -> None:
        j = idx + delta
        if 0 <= idx < len(self._prov_state) and 0 <= j < len(self._prov_state):
            self._prov_state[idx], self._prov_state[j] = (
                self._prov_state[j], self._prov_state[idx])
            self._render_cards()

    def _collect_providers(self) -> list:
        out = []
        for st in self._prov_state:
            vision = bool(st["vision"])
            out.append({
                "id": st["id"],
                "name": st["name"],
                "base_url": st["v_base"].get().strip(),
                "api_key": st["v_key"].get().strip(),
                "model": st["v_model"].get().strip(),
                "vision": vision,
                "vision_model": st["v_vmodel"].get().strip() if vision else "",
                "enabled": bool(st["v_enabled"].get()),
            })
        return out

    def _save_step3(self) -> None:
        c = self.cfg
        c.api_providers = self._collect_providers()
        c.use_llm = bool(self.v_use_llm.get())
        # 向后兼容：把第一个 DeepSeek 供应商镜像回旧的单字段
        for p in c.api_providers:
            if p["id"] == "deepseek" and p["api_key"]:
                c.deepseek_api_key = p["api_key"]
                c.deepseek_base_url = p["base_url"] or c.deepseek_base_url
                c.deepseek_model = p["model"] or c.deepseek_model
                c.deepseek_vision_model = p["vision_model"] or c.deepseek_vision_model
                break
        c.save()
        self.refresh_steps()
        self._status(i18n.tr("setup_saved"))

    # ==================================================================
    # 步骤状态 / 切换 / 语言
    # ==================================================================
    def _compute_done(self) -> dict:
        return {
            1: deps_mod.all_installed(),
            2: bool(self.cfg.enabled_sources or self.cfg.cookies_raw or self.cfg.cookies_file),
            3: bool(self.cfg.effective_providers()),
        }

    def refresh_steps(self) -> None:
        self.done = self._compute_done()
        for i in (1, 2, 3):
            done = self.done[i]
            self.nav_btn[i].configure(
                bg=COL_GREEN_BG if done else COL_GRAY_BG,
                fg=COL_GREEN_TEXT if done else COL_GRAY_TEXT,
            )
            self.nav_status[i].configure(
                text=i18n.tr("setup_status_done" if done else "setup_status_todo"),
                fg=COL_GREEN if done else COL_MUTED,
            )

    def show_step(self, n: int) -> None:
        self.current = n
        for i, f in self.frames.items():
            if i == n:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()
        for i in (1, 2, 3):
            self.nav_btn[i].configure(
                highlightbackground=COL_PRIMARY if i == n else COL_BG,
                highlightthickness=2 if i == n else 0,
            )

    def _status(self, text: str) -> None:
        """在底部短暂显示一条提示。"""
        try:
            self.status_label.configure(text=text)
            self.root.after(3000, lambda: self.status_label.configure(text=""))
        except Exception:  # noqa: BLE001
            pass

    def _capture_state(self) -> None:
        c = self.cfg
        try:
            c.output_dir = self.v_outdir.get().strip()
            c.ffmpeg_path = self.v_ffmpeg.get().strip()
            c.asr_model = self.v_asr_model.get().strip() or ASR_MODELS[0]
            c.asr_device = self.v_asr_device.get().strip() or "cpu"
            c.keep_video = bool(self.v_keep.get())
            c.include_transcript = bool(self.v_transcript.get())
            c.download_cover = bool(self.v_cover.get())
            c.use_vision = bool(self.v_vision.get())
            c.use_images = bool(self.v_images.get())
        except Exception:  # noqa: BLE001
            pass
        try:
            c.enabled_sources = [sid for sid, var in self.src_vars.items() if var.get()]
            c.cookies_raw = self.cookie_raw.get("1.0", "end").strip()
            c.cookies_browser = self.v_browser.get().strip() or "none"
            c.cookies_file = self.v_cookiefile.get().strip()
        except Exception:  # noqa: BLE001
            pass
        try:
            c.api_providers = self._collect_providers()
            c.use_llm = bool(self.v_use_llm.get())
        except Exception:  # noqa: BLE001
            pass

    def _on_language(self, _event=None) -> None:
        if self.installing:
            # 安装进行中不重建界面，避免打断轮询
            self.lang_combo.set(i18n.ui_language_name(i18n.current_language()))
            return
        name = self.lang_combo.get()
        code = next((c for c, n in i18n.UI_LANGUAGES if n == name), "en")
        if code == i18n.current_language():
            return
        self._capture_state()
        i18n.set_language(code)
        self.cfg.ui_language = code
        try:
            self.cfg.save()
        except Exception:  # noqa: BLE001
            pass
        for w in self.root.winfo_children():
            w.destroy()
        self._build()
        self.refresh_steps()
        self.show_step(self.current)


class _CustomProviderDialog:
    """自定义供应商的小弹窗。"""

    def __init__(self, parent) -> None:
        self.result = None
        self.top = tk.Toplevel(parent)
        self.top.title(i18n.tr("s3_dlg_custom_title"))
        self.top.configure(bg=COL_CARD)
        self.top.transient(parent)
        self.top.resizable(False, False)

        frm = tk.Frame(self.top, bg=COL_CARD)
        frm.pack(fill="both", expand=True, padx=20, pady=16)
        frm.columnconfigure(1, weight=1)

        self.v_name = tk.StringVar(value=i18n.tr("s3_custom_name"))
        self.v_base = tk.StringVar()
        self.v_model = tk.StringVar()
        self.v_vision = tk.BooleanVar(value=True)

        rows = (
            (i18n.tr("s3_dlg_custom_name"), self.v_name),
            (i18n.tr("s3_dlg_custom_base"), self.v_base),
            (i18n.tr("s3_dlg_custom_model"), self.v_model),
        )
        for r, (label, var) in enumerate(rows):
            tk.Label(frm, text=label, font=(FONT, FS_BODY), bg=COL_CARD, fg=COL_TEXT,
                     anchor="w").grid(row=r, column=0, sticky="w", pady=4)
            ttk.Entry(frm, textvariable=var, width=44).grid(
                row=r, column=1, sticky="ew", padx=(12, 0), pady=4)
        tk.Checkbutton(frm, text=i18n.tr("s3_dlg_custom_vision"), variable=self.v_vision,
                       bg=COL_CARD, fg=COL_TEXT, font=(FONT, FS_BODY), cursor="hand2",
                       activebackground=COL_CARD, selectcolor=COL_CARD).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        bar = tk.Frame(self.top, bg=COL_CARD)
        bar.pack(fill="x", padx=20, pady=(0, 16))
        tk.Button(bar, text=i18n.tr("setup_btn_save"), command=self._ok,
                  relief="flat", bd=0, padx=18, pady=7, bg=COL_PRIMARY, fg="white",
                  font=(FONT, FS_BTN), activebackground="#3f61e6",
                  activeforeground="white", cursor="hand2").pack(side="right")
        tk.Button(bar, text=i18n.tr("btn_cancel"), command=self.top.destroy,
                  relief="solid", bd=1, padx=16, pady=6, bg=COL_CARD, font=(FONT, FS_BODY),
                  highlightbackground=COL_BORDER, cursor="hand2").pack(side="right", padx=8)

        self.top.update_idletasks()
        try:
            px = parent.winfo_rootx() + (parent.winfo_width() - self.top.winfo_width()) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - self.top.winfo_height()) // 3
            self.top.geometry(f"+{max(0, px)}+{max(0, py)}")
        except Exception:  # noqa: BLE001
            pass

    def _ok(self) -> None:
        self.result = {
            "name": self.v_name.get().strip(),
            "base_url": self.v_base.get().strip(),
            "model": self.v_model.get().strip(),
            "vision": bool(self.v_vision.get()),
        }
        self.top.destroy()


def main() -> int:
    # 尽量让界面在高分屏下不糊
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass

    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.2)
    except Exception:  # noqa: BLE001
        pass
    setup_default_fonts()
    root.protocol("WM_DELETE_WINDOW", root.destroy)

    SetupApp(root)

    if "--selftest" in sys.argv:
        root.update_idletasks()
        root.update()
        root.destroy()
        print("SELFTEST_OK")
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
