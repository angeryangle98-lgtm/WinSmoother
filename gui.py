from __future__ import annotations

import base64
import json
import math
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk

from apps import (
    AppEntry,
    extract_desktop_icons,
    list_installed_apps,
    measure_app_size,
    store_logo_path,
    uninstall_app,
)
from cleaner import run_cleaner
from repair import PLAN, run_repair
from utils import (
    APP_NAME,
    APP_TAGLINE,
    APP_VERSION,
    IS_WINDOWS,
    Canceller,
    Logger,
    human_bytes,
    log_dir,
    prune_logs,
    resource_path,
    system_drive,
    system_drive_usage,
)

# ---------------------------------------------------------------- palette
BG = "#0b0d12"
SIDEBAR = "#10131a"
PANEL = "#151922"
PANEL_ALT = "#11151d"
TEXT = "#f5f7fb"
MUTED = "#8c96a8"
DIM = "#657186"
BORDER = "#242b38"
GREEN = "#55d68b"
GREEN_DARK = "#2f9b67"
PURPLE = "#a982ff"
RED = "#ff7373"
AMBER = "#e6bc64"
BLUE = "#5aa9ff"
FONT = "Segoe UI"

STEP_BY_NAME = {step["name"]: step for step in PLAN}
NAV_ITEMS = ("Overview", "Cleanup", "Repair", "Apps", "Report", "About")
PHASES = ("cleanup", "repair", "optimize")
PHASE_OF = {
    "cleanup": "cleanup",
    "DISM": "repair",
    "SFC": "repair",
    "DISM Cleanup": "optimize",
    "CHKDSK": "optimize",
}
PHASE_LABEL = {
    "idle": "IDLE", "cleanup": "CLEANUP", "DISM": "DISM", "SFC": "SFC",
    "DISM Cleanup": "COMPONENT CLEANUP", "CHKDSK": "CHKDSK",
    "done": "COMPLETE", "stopped": "STOPPED", "error": "ATTENTION",
}

CLEANUP_TARGETS = (
    ("Temporary files", "User and Windows TEMP folders", "SAFE"),
    ("Browser caches", "Chrome, Edge, Brave and Firefox - all profiles", "SAFE"),
    ("GPU shader caches", "DirectX, NVIDIA and AMD", "SAFE"),
    ("Crash dumps & error reports", "Minidumps and Windows Error Reporting queues", "SAFE"),
    ("Thumbnail & internet cache", "Explorer thumbnails and INetCache", "SAFE"),
    ("Windows Update leftovers", "Downloaded update payloads and Delivery Optimization cache", "SAFE"),
    ("Microsoft Teams cache", "Cache folders only - chats and sign-in are kept", "SAFE"),
    ("Recycle Bin", "Emptied permanently - you can turn this off on the Overview page", "OPTIONAL"),
)

ABOUT_DESCRIPTION = (
    "WinSmoother is a free, open-source maintenance utility for Windows 10 and 11. "
    "It clears out temporary files and caches, then repairs the Windows system with "
    "Microsoft's own built-in tools - all from one window, with no command prompt "
    "and no third-party downloads."
)
ABOUT_CAPABILITIES = (
    "Cleans temp folders, browser caches, GPU shader caches, crash dumps, "
    "thumbnails, Windows Update leftovers and Teams cache",
    "Optionally empties the Recycle Bin",
    "Lists every installed program with its real size and force-uninstalls the ones you \
tick - even while they are running",
    "Repairs the Windows component store (DISM) and protected system files (SFC)",
    "Removes superseded update components and scans the C: file system (CHKDSK)",
    "Shows each tool's real progress and a clear report at the end",
    "Keeps private logs of every run and lets you stop at any time",
)
ABOUT_SAFETY = (
    "Cleanup only touches known temp and cache folders - documents, downloads, "
    "passwords and settings are never deleted. Programs are removed only when you tick "
    "them on the Apps page",
    "Uses only DISM, SFC and CHKDSK from Windows; it never downloads or replaces "
    "system files",
    "Needs administrator rights. Repair can take 30-60+ minutes and DISM may need "
    "an internet connection",
    "Not an antivirus and cannot fix hardware faults or faulty drivers",
    "Logs are stored locally in %LOCALAPPDATA%\\WinSmootherBot\\Logs",
)


class Check(tk.Frame):
    """Dark-theme checkbox (native Windows check marks are unreadable on dark)."""

    def __init__(self, parent, text, variable, bg=PANEL, scale=1.0):
        super().__init__(parent, bg=bg, cursor="hand2")
        self.var = variable
        self.enabled = True
        self.size = int(18 * scale)
        self.cv = tk.Canvas(self, width=self.size, height=self.size, bg=bg, highlightthickness=0)
        self.cv.pack(side="left")
        self.lbl = tk.Label(self, text=text, font=(FONT, 9), fg=TEXT, bg=bg, anchor="w")
        self.lbl.pack(side="left", padx=(8, 0))
        for w in (self, self.cv, self.lbl):
            w.bind("<Button-1>", self._toggle)
        self.var.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _draw(self):
        s = self.size
        self.cv.delete("all")
        on = bool(self.var.get())
        edge = (GREEN if on else DIM) if self.enabled else BORDER
        self.cv.create_rectangle(2, 2, s - 2, s - 2, outline=edge, width=2,
                                 fill=(GREEN if on and self.enabled else PANEL_ALT))
        if on:
            self.cv.create_line(s * .27, s * .53, s * .43, s * .70, s * .74, s * .30,
                                fill="#07110b" if self.enabled else DIM, width=2)

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        self.lbl.configure(fg=TEXT if enabled else DIM)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def _toggle(self, _event=None):
        if self.enabled:
            self.var.set(not self.var.get())


def _short(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class MiniCheck(tk.Canvas):
    """Small red checkbox used in the Apps list (ticked = will be deleted)."""

    def __init__(self, parent, variable, size, bg=PANEL_ALT, enabled=True):
        super().__init__(parent, width=size, height=size, bg=bg, highlightthickness=0,
                         cursor="hand2" if enabled else "arrow")
        self.var, self.size, self.enabled = variable, size, enabled
        self.var.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _draw(self):
        try:
            s = self.size
            self.delete("all")
            on = bool(self.var.get())
            edge = BORDER if not self.enabled else (RED if on else DIM)
            self.create_rectangle(2, 2, s - 2, s - 2, outline=edge, width=2,
                                  fill=RED if on else PANEL_ALT)
            if on:
                self.create_line(s * .27, s * .53, s * .43, s * .70, s * .74, s * .30,
                                 fill="#1a0708", width=2)
        except tk.TclError:
            pass


class WinSmootherApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)

        w, h = self.px(1100), self.px(730)
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        self.root.minsize(self.px(1000), self.px(670))
        self.root.configure(bg=BG)

        self.events: queue.Queue = queue.Queue()
        self.running = False
        self.cancel: Canceller | None = None
        self.started_at: float | None = None
        self.duration = 0
        self.outcome = ""  # "done" | "stopped" | "error"
        self.log_path: Path | None = None
        self.cleanup_result: dict = {}
        self.repair_result: dict = {}
        self.skipped_phases: set[str] = set()
        self.last_options = {"repair": True, "bin": True}
        self.stage_name = ""
        self.stage_started = 0.0
        self.stage_real = False   # True once the tool itself reported a percentage
        self.pct_shown = 0.0

        self.opt_repair = tk.BooleanVar(value=True)
        self.opt_bin = tk.BooleanVar(value=True)

        self.sys_drive = system_drive()
        self.disk_before = None
        self.disk_after = None
        self.space_recovered = 0
        self.disk_before_apps = None
        self._start_text = "START MAINTENANCE  →"
        self.current_view = "Overview"
        self.apps: list[AppEntry] = []
        self.app_rows: dict[str, dict] = {}
        self.apps_loaded_once = False
        self.apps_loading = False
        self.apps_gen = 0
        self.uninstalling = False
        self.uninstall_cancel: Canceller | None = None
        self.app_sort = ("size", True)
        self.icon_px = self.px(24)

        self._load_assets()
        self._configure_style()
        self._build_shell()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind_all("<MouseWheel>", self._on_wheel)
        self._refresh_free_space()
        self.root.after(80, self._poll_events)
        self.root.after(1000, self._tick)

    # ------------------------------------------------------------ helpers
    def px(self, n: float) -> int:
        return int(n * self.scale)

    def _pick(self, sizes):
        want = sizes[0] * self.scale
        return min(sizes, key=lambda s: abs(s - want))

    def _load_assets(self):
        self.logo_small = self.logo_large = None
        try:
            small = self._pick((44, 66, 88))
            large = self._pick((128, 192, 256))
            self.logo_small = tk.PhotoImage(file=str(resource_path(f"assets/logo_{small}.png")))
            self.logo_large = tk.PhotoImage(file=str(resource_path(f"assets/logo_{large}.png")))
        except Exception:
            pass
        try:
            if IS_WINDOWS:
                self.root.iconbitmap(str(resource_path("assets/icon.ico")))
            elif self.logo_large:
                self.root.iconphoto(True, self.logo_large)
        except Exception:
            pass

    def _configure_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor="#202532", background=GREEN, bordercolor="#202532",
            lightcolor=GREEN, darkcolor=GREEN_DARK, thickness=self.px(10),
        )

    def _panel(self, parent, bg=PANEL):
        return tk.Frame(parent, bg=bg, highlightthickness=1, highlightbackground=BORDER)

    def _label(self, parent, text, size=10, bold=False, fg=TEXT, bg=PANEL, **kw):
        return tk.Label(parent, text=text, font=(FONT, size, "bold" if bold else "normal"),
                        fg=fg, bg=bg, **kw)

    def _button(self, parent, text, command, kind="ghost", **kw):
        colors = {
            "primary": (GREEN, "#73e6a6", "#07110b"),
            "danger": ("#3a1f27", "#54262f", RED),
            "ghost": (PANEL, BORDER, TEXT),
        }
        bg, hover, fg = colors[kind]
        btn = tk.Button(
            parent, text=text, command=command, font=(FONT, 10 if kind == "primary" else 9, "bold"),
            fg=fg, bg=bg, activebackground=hover, activeforeground=fg,
            disabledforeground=DIM, relief="flat", bd=0, cursor="hand2",
            padx=kw.pop("padx", 18), pady=kw.pop("pady", 11),
        )
        off_bg = "#1d2430"
        btn._enabled = True

        def set_state(enabled: bool, text: str | None = None):
            btn._enabled = enabled
            btn.configure(state="normal" if enabled else "disabled", bg=bg if enabled else off_bg)
            if text:
                btn.configure(text=text)

        btn.set_state = set_state
        btn.bind("<Enter>", lambda _e: btn._enabled and btn.configure(bg=hover))
        btn.bind("<Leave>", lambda _e: btn.configure(bg=bg if btn._enabled else off_bg))
        return btn

    def _card(self, parent, column, title, value, accent):
        card = self._panel(parent)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 10, 0))
        parent.grid_columnconfigure(column, weight=1, uniform="cards")
        self._label(card, title.upper(), 8, True, MUTED).pack(anchor="w", padx=15, pady=(13, 2))
        value_label = self._label(card, value, 18, True)
        value_label.pack(anchor="w", padx=15, pady=(0, 10))
        tk.Frame(card, bg=accent, height=2).pack(fill="x", side="bottom")
        return value_label

    # -------------------------------------------------------------- shell
    def _build_shell(self):
        frame = tk.Frame(self.root, bg=BG)
        frame.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(frame, width=self.px(216), bg=SIDEBAR,
                                highlightthickness=1, highlightbackground=BORDER)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand = tk.Frame(self.sidebar, bg=SIDEBAR)
        brand.pack(fill="x", padx=16, pady=(24, 26))
        if self.logo_small:
            tk.Label(brand, image=self.logo_small, bg=SIDEBAR).pack(side="left")
        text = tk.Frame(brand, bg=SIDEBAR)
        text.pack(side="left", padx=(8, 0))
        self._label(text, APP_NAME, 12, True, bg=SIDEBAR).pack(anchor="w")
        self._label(text, f"v{APP_VERSION}", 8, False, MUTED, SIDEBAR).pack(anchor="w")

        self.nav = {}
        for name in NAV_ITEMS:
            item = tk.Frame(self.sidebar, bg=SIDEBAR)
            item.pack(fill="x", pady=1)
            bar = tk.Frame(item, bg=SIDEBAR, width=3)
            bar.pack(side="left", fill="y")
            btn = tk.Button(
                item, text=name, command=lambda n=name: self._nav(n), anchor="w",
                font=(FONT, 10, "bold"), fg=MUTED, bg=SIDEBAR, activebackground=PANEL,
                activeforeground=TEXT, disabledforeground="#3f495b", relief="flat", bd=0,
                cursor="hand2", padx=18, pady=10,
                state="disabled" if name == "Report" else "normal",
            )
            btn.pack(side="left", fill="x", expand=True, padx=(8, 10))
            self.nav[name] = (bar, btn)

        tk.Frame(self.sidebar, bg=SIDEBAR).pack(fill="both", expand=True)
        self._label(self.sidebar, APP_TAGLINE, 8, False, DIM, SIDEBAR,
                    wraplength=self.px(170), justify="left").pack(anchor="w", padx=18, pady=(0, 18))

        self.main = tk.Frame(frame, bg=BG)
        self.main.pack(side="left", fill="both", expand=True)
        self._build_topbar()

        self.content = tk.Frame(self.main, bg=BG)
        self.content.pack(fill="both", expand=True, padx=24, pady=(0, 20))

        self.views = {
            "Overview": self._build_overview(),
            "Cleanup": self._build_cleanup_view(),
            "Repair": self._build_repair_view(),
            "Apps": self._build_apps_view(),
            "Report": tk.Frame(self.content, bg=BG),
            "About": self._build_about_view(),
        }
        self._show_view("Overview")

    def _build_topbar(self):
        top = tk.Frame(self.main, bg=BG, height=self.px(66))
        top.pack(fill="x", padx=24, pady=(16, 6))
        top.pack_propagate(False)
        self.page_title = self._label(top, "Overview", 21, True, bg=BG)
        self.page_title.pack(side="left")
        self.report_button = self._button(
            top, "VIEW REPORT", lambda: self._nav("Report"), padx=16, pady=8)
        self.report_button.set_state(False)
        self.report_button.pack(side="right", pady=6)

    # ----------------------------------------------------------- overview
    def _build_overview(self):
        view = tk.Frame(self.content, bg=BG)

        hero = self._panel(view)
        hero.pack(fill="x")

        head = tk.Frame(hero, bg=PANEL)
        head.pack(fill="x", padx=26, pady=(22, 0))
        left = tk.Frame(head, bg=PANEL)
        left.pack(side="left", fill="x", expand=True)
        self._label(left, "System health workflow", 18, True).pack(anchor="w")
        self.status_var = tk.StringVar(value="Ready. Start a full cleanup and repair pass.")
        tk.Label(left, textvariable=self.status_var, font=(FONT, 10), fg=MUTED, bg=PANEL,
                 wraplength=self.px(600), justify="left", anchor="w").pack(anchor="w", pady=(6, 0))

        right = tk.Frame(head, bg=PANEL)
        right.pack(side="right", padx=(20, 0))
        self.percent_var = tk.StringVar(value="0%")
        tk.Label(right, textvariable=self.percent_var, font=(FONT, 34, "bold"),
                 fg=GREEN, bg=PANEL).pack(anchor="e")
        self.phase_var = tk.StringVar(value=PHASE_LABEL["idle"])
        tk.Label(right, textvariable=self.phase_var, font=(FONT, 8, "bold"),
                 fg=MUTED, bg=PANEL).pack(anchor="e")

        self.progress = ttk.Progressbar(hero, style="Accent.Horizontal.TProgressbar",
                                        maximum=100, mode="determinate")
        self.progress.pack(fill="x", padx=26, pady=(16, 0))

        actions = tk.Frame(hero, bg=PANEL)
        actions.pack(fill="x", padx=26, pady=(18, 22))
        self.start_btn = self._button(actions, "START MAINTENANCE  →", self.start, "primary")
        self.start_btn.pack(side="left")
        self.stop_btn = self._button(actions, "STOP", self.stop, "danger", padx=16)
        self.stop_btn.set_state(False)
        self.stop_btn.pack(side="left", padx=(10, 0))

        opts = tk.Frame(actions, bg=PANEL)
        opts.pack(side="right")
        self.chk_repair = Check(opts, "Run Windows repair (DISM, SFC, CHKDSK) - 30-60+ min",
                                self.opt_repair, scale=self.scale)
        self.chk_repair.pack(anchor="w")
        self.chk_bin = Check(opts, "Empty the Recycle Bin (cannot be undone)",
                             self.opt_bin, scale=self.scale)
        self.chk_bin.pack(anchor="w", pady=(6, 0))

        cards = tk.Frame(view, bg=BG)
        cards.pack(fill="x", pady=14)
        self.items_card = self._card(cards, 0, "Files cleaned", "—", GREEN)
        self.space_card = self._card(cards, 1, "Space recovered", "—", PURPLE)
        self.repair_card = self._card(cards, 2, "Repair stages", "—", AMBER)
        self.skip_card = self._card(cards, 3, "Skipped / in use", "—", RED)
        self.free_card = self._card(cards, 4, f"Free on {self.sys_drive}", "—", BLUE)

        pipeline = self._panel(view)
        pipeline.pack(fill="both", expand=True)
        self._label(pipeline, "Maintenance pipeline", 13, True).pack(anchor="w", padx=22, pady=(16, 8))

        self.phase_rows = {}
        info = (
            ("cleanup", "Cleanup", "Temporary files, caches and recyclable data"),
            ("repair", "Repair", "DISM component store and SFC system-file verification"),
            ("optimize", "Optimize", "Component cleanup and CHKDSK file-system scan"),
        )
        for key, title, desc in info:
            row = tk.Frame(pipeline, bg=PANEL_ALT)
            row.pack(fill="x", padx=20, pady=4)
            dot = self._label(row, "●", 13, True, "#3d485b", PANEL_ALT)
            dot.pack(side="left", padx=14, pady=8)
            body = tk.Frame(row, bg=PANEL_ALT)
            body.pack(side="left", fill="x", expand=True, pady=6)
            self._label(body, title, 10, True, bg=PANEL_ALT).pack(anchor="w")
            self._label(body, desc, 8, False, MUTED, PANEL_ALT).pack(anchor="w")
            state = self._label(row, "WAITING", 8, True, MUTED, PANEL_ALT)
            state.pack(side="right", padx=14)
            self.phase_rows[key] = (dot, state)
        return view

    # ------------------------------------------------- cleanup / repair views
    def _list_view(self, title, subtitle, rows):
        view = tk.Frame(self.content, bg=BG)
        self._label(view, title, 18, True, bg=BG).pack(anchor="w", pady=(4, 6))
        self._label(view, subtitle, 9, False, MUTED, BG, wraplength=self.px(760),
                    justify="left").pack(anchor="w", pady=(0, 14))
        box = self._panel(view)
        box.pack(fill="both", expand=True)
        for name, desc, tag, tag_color in rows:
            row = tk.Frame(box, bg=PANEL_ALT)
            row.pack(fill="x", padx=18, pady=4)
            self._label(row, name, 10, True, bg=PANEL_ALT, width=26, anchor="w").pack(
                side="left", padx=(14, 0), pady=10)
            self._label(row, desc, 9, False, MUTED, PANEL_ALT).pack(side="left", pady=10)
            self._label(row, tag, 8, True, tag_color, PANEL_ALT).pack(side="right", padx=14)
        return view

    def _build_cleanup_view(self):
        rows = [(n, d, t, GREEN if t == "SAFE" else AMBER) for n, d, t in CLEANUP_TARGETS]
        return self._list_view(
            "Cleanup targets",
            "Only known temporary and cache locations are cleaned. Files in use are skipped, "
            "and the running application is protected.",
            rows,
        )

    def _build_repair_view(self):
        rows = [(s["name"], s["desc"], f"{s['start']}-{s['end']}%", PURPLE) for s in PLAN]
        return self._list_view(
            "Windows repair",
            "Runs the maintenance tools already supplied by Microsoft. The percentage shown "
            "comes from each tool's own progress output.",
            rows,
        )

    # -------------------------------------------------------------- about
    def _bullets(self, parent, items, color):
        for text in items:
            row = tk.Frame(parent, bg=PANEL)
            row.pack(fill="x", padx=20, pady=3)
            self._label(row, "●", 6, True, color).pack(side="left", anchor="n", pady=(5, 0))
            tk.Label(row, text=text, font=(FONT, 9), fg=TEXT, bg=PANEL, justify="left",
                     anchor="w", wraplength=self.px(400)).pack(side="left", padx=(10, 0), fill="x")

    def _build_about_view(self):
        view = tk.Frame(self.content, bg=BG)
        view.grid_columnconfigure(0, weight=2, uniform="about")
        view.grid_columnconfigure(1, weight=3, uniform="about")
        view.grid_rowconfigure(0, weight=1)

        left = self._panel(view)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        if self.logo_large:
            tk.Label(left, image=self.logo_large, bg=PANEL).pack(pady=(28, 12))
        self._label(left, APP_NAME, 20, True).pack()
        self._label(left, f"Version {APP_VERSION}", 9, False, MUTED).pack(pady=(2, 14))
        about_text = tk.Label(left, text=ABOUT_DESCRIPTION, font=(FONT, 9), fg=TEXT, bg=PANEL,
                              wraplength=self.px(240), justify="center")
        about_text.pack(padx=22)
        # keep the wrapped text inside the panel whatever the window size
        left.bind("<Configure>", lambda e: about_text.configure(wraplength=max(120, e.width - 60)))
        tk.Frame(left, bg=BORDER, height=1).pack(fill="x", padx=22, pady=18)
        self._label(left, "Free and open source · MIT License", 8, False, MUTED).pack()
        self._label(left, "Windows 10 / 11 · Administrator required", 8, False, MUTED).pack(pady=(4, 0))

        right = tk.Frame(view, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        for title, items, color in (
            ("What it can do", ABOUT_CAPABILITIES, GREEN),
            ("Safety & limits", ABOUT_SAFETY, PURPLE),
        ):
            box = self._panel(right)
            box.pack(fill="x", pady=(0, 12))
            self._label(box, title, 12, True).pack(anchor="w", padx=20, pady=(16, 8))
            self._bullets(box, items, color)
            tk.Frame(box, height=10, bg=PANEL).pack()
        return view

    # --------------------------------------------------------------- apps
    def _app_grid(self, frame):
        frame.grid_columnconfigure(0, minsize=self.px(42))
        frame.grid_columnconfigure(1, minsize=self.px(40))     # icon
        frame.grid_columnconfigure(2, weight=1)                # name
        frame.grid_columnconfigure(3, minsize=self.px(170))    # publisher
        frame.grid_columnconfigure(4, minsize=self.px(96))     # type
        frame.grid_columnconfigure(5, minsize=self.px(100))    # size

    def _build_apps_view(self):
        view = tk.Frame(self.content, bg=BG)
        self._label(view, "Installed programs", 18, True, bg=BG).pack(anchor="w", pady=(4, 4))
        self._label(
            view,
            "Tick the programs you want gone, then press the red button. Removal is forced: "
            "the program is closed even if it is running, its uninstaller runs silently, and "
            "every leftover file, service and registry entry is deleted. This cannot be undone.",
            9, False, MUTED, BG, wraplength=self.px(760), justify="left",
        ).pack(anchor="w", pady=(0, 10))

        bar = tk.Frame(view, bg=BG)
        bar.pack(fill="x", pady=(0, 8))
        self._label(bar, "SEARCH", 8, True, MUTED, BG).pack(side="left", padx=(0, 8))
        self.app_search = tk.StringVar()
        entry = tk.Entry(bar, textvariable=self.app_search, font=(FONT, 10), bg=PANEL, fg=TEXT,
                         insertbackground=TEXT, relief="flat", highlightthickness=1,
                         highlightbackground=BORDER, highlightcolor=GREEN)
        entry.pack(side="left", fill="x", expand=True, ipady=6)
        self.app_search.trace_add("write", lambda *_: self._render_apps())
        self._button(bar, "REFRESH", self._load_apps, padx=14, pady=6).pack(side="left", padx=(10, 0))
        self._button(bar, "CLEAR SELECTION", self._clear_app_selection,
                     padx=14, pady=6).pack(side="left", padx=(8, 0))

        # Bottom widgets are packed first so they stay visible when the window is small.
        foot = tk.Frame(view, bg=BG)
        foot.pack(side="bottom", fill="x", pady=(10, 0))
        self.app_progress = ttk.Progressbar(view, style="Accent.Horizontal.TProgressbar",
                                            maximum=100, mode="determinate")
        self.app_progress.pack(side="bottom", fill="x", pady=(10, 0))

        text = tk.Frame(foot, bg=BG)
        text.pack(side="left", fill="x", expand=True)
        self.apps_summary = tk.StringVar(value="")
        self.apps_status = tk.StringVar(value="Open this page to scan the installed programs.")
        tk.Label(text, textvariable=self.apps_summary, font=(FONT, 9, "bold"), fg=TEXT, bg=BG,
                 anchor="w").pack(anchor="w")
        tk.Label(text, textvariable=self.apps_status, font=(FONT, 8), fg=MUTED, bg=BG,
                 anchor="w", justify="left", wraplength=self.px(560)).pack(anchor="w")
        self.uninstall_btn = self._button(foot, "UNINSTALL SELECTED", self.start_uninstall,
                                          "danger", padx=20)
        self.uninstall_btn.set_state(False)
        self.uninstall_btn.pack(side="right")
        self.app_stop_btn = self._button(foot, "STOP", self.stop_uninstall, "ghost", padx=16)
        self.app_stop_btn.set_state(False)
        self.app_stop_btn.pack(side="right", padx=(0, 10))

        box = self._panel(view)
        box.pack(fill="both", expand=True)
        head = tk.Frame(box, bg=PANEL)
        head.pack(fill="x")
        self._app_grid(head)
        self.app_heads = {}
        for col, key, label, anchor in ((1, None, "ICON", "w"), (2, "name", "NAME", "w"),
                                        (3, None, "PUBLISHER", "w"), (4, None, "TYPE", "w"),
                                        (5, "size", "SIZE", "e")):
            lbl = tk.Label(head, text=label, font=(FONT, 8, "bold"), fg=MUTED, bg=PANEL,
                           anchor=anchor, cursor="hand2" if key else "arrow")
            lbl.grid(row=0, column=col, sticky="ew", pady=10, padx=(0, 16) if col == 5 else 0)
            if key:
                lbl.bind("<Button-1>", lambda _e, k=key: self._sort_apps(k))
                self.app_heads[key] = (lbl, label)
        tk.Frame(box, bg=BORDER, height=1).pack(fill="x")

        wrap = tk.Frame(box, bg=PANEL)
        wrap.pack(fill="both", expand=True)
        self.app_canvas = tk.Canvas(wrap, bg=PANEL, highlightthickness=0)
        scroll = tk.Scrollbar(wrap, orient="vertical", command=self.app_canvas.yview)
        self.app_canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.app_canvas.pack(side="left", fill="both", expand=True)
        self.app_list = tk.Frame(self.app_canvas, bg=PANEL)
        window = self.app_canvas.create_window((0, 0), window=self.app_list, anchor="nw")
        self.app_list.bind(
            "<Configure>", lambda _e: self.app_canvas.configure(scrollregion=self.app_canvas.bbox("all")))
        self.app_canvas.bind(
            "<Configure>", lambda e: self.app_canvas.itemconfigure(window, width=e.width))
        return view

    def _on_wheel(self, event):
        if self.current_view == "Apps":
            self.app_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _size_text(self, app: AppEntry) -> str:
        if not app.size_done:
            return "…"
        return human_bytes(app.size) if app.size else "—"

    # -- loading -------------------------------------------------------
    def _load_apps(self):
        if self.uninstalling or self.apps_loading:
            return
        self.apps_loaded_once = True
        self.apps_loading = True
        self.apps_gen += 1
        self.apps_status.set("Scanning installed programs...")
        self.apps_summary.set("")
        self.uninstall_btn.set_state(False, "UNINSTALL SELECTED")
        threading.Thread(target=self._apps_worker, args=(self.apps_gen,), daemon=True).start()

    def _apps_worker(self, gen: int):
        try:
            apps = list_installed_apps()
        except Exception as exc:
            self.events.put(("apps_error", f"{type(exc).__name__}: {exc}"))
            return
        self.events.put(("apps_loaded", gen, apps))
        stale = lambda: gen != self.apps_gen  # noqa: E731
        threading.Thread(target=self._icons_worker, args=(gen, apps), daemon=True).start()
        for app in apps:
            if stale():
                return
            try:
                size = measure_app_size(app, stale)
            except Exception:
                size = app.est_size or None
            self.events.put(("app_size", gen, app.key, size))
        self.events.put(("apps_sized", gen))

    def _on_apps_loaded(self, gen: int, apps):
        if gen != self.apps_gen:
            return
        self.apps_loading = False
        self.apps = apps
        for row in self.app_rows.values():
            row["row"].destroy()
        self.app_rows = {}
        for app in apps:
            app.size = app.est_size or None      # provisional, only used for the first sort
            self._make_app_row(app)
        self.apps_status.set(f"Found {len(apps)} programs - measuring their real sizes...")
        self._render_apps()
        self._update_uninstall_button()

    def _icons_worker(self, gen: int, apps):
        """Loads every program's icon in the background; rows show a letter until theirs arrives."""
        stale = lambda: gen != self.apps_gen  # noqa: E731
        size = self.icon_px
        desktop: list[tuple[int, str, int]] = []
        for number, app in enumerate(apps):
            if stale():
                return
            if app.kind == "store":
                try:
                    path = store_logo_path(app, size)
                    if path:
                        with open(path, "rb") as fh:
                            data = base64.b64encode(fh.read()).decode("ascii")
                        self.events.put(("app_icon", gen, app.key, data))
                except Exception:
                    pass
            elif app.icon_file:
                desktop.append((number, app.icon_file, app.icon_index))
        for start in range(0, len(desktop), 40):
            if stale():
                return
            try:
                found = extract_desktop_icons(desktop[start:start + 40], size)
            except Exception:
                continue
            for number, data in found.items():
                self.events.put(("app_icon", gen, apps[number].key, data))

    def _on_app_icon(self, gen: int, key: str, data: str):
        if gen != self.apps_gen:
            return
        row = self.app_rows.get(key)
        if not row:
            return
        try:
            img = tk.PhotoImage(data=data)
            factor = max(1, round(max(img.width(), img.height()) / self.icon_px))
            if factor > 1:
                img = img.subsample(factor, factor)
        except tk.TclError:
            return  # keep the letter placeholder
        canvas = row["icon"]
        canvas.delete("all")
        canvas.create_image(self.icon_px // 2, self.icon_px // 2, image=img)
        row["img"] = img  # Tk drops images that are not referenced

    def _on_app_size(self, gen: int, key: str, size):
        if gen != self.apps_gen:
            return
        row = self.app_rows.get(key)
        if not row:
            return
        app = row["app"]
        app.size, app.size_done = size, True
        row["size"].configure(text=self._size_text(app), fg=TEXT if size else DIM)
        if row["var"].get():
            self._update_apps_summary()

    def _on_apps_sized(self, gen: int):
        if gen != self.apps_gen:
            return
        self.apps_status.set("Ready. Tick programs to remove them - click SIZE or NAME to sort.")
        self._render_apps(reset=False)

    # -- list ----------------------------------------------------------
    def _make_app_row(self, app: AppEntry):
        bg = PANEL_ALT
        row = tk.Frame(self.app_list, bg=bg, cursor="arrow" if app.locked else "hand2")
        self._app_grid(row)
        var = tk.BooleanVar(value=False)
        chk = MiniCheck(row, var, self.px(18), bg, enabled=not app.locked)
        chk.grid(row=0, column=0, padx=(14, 8), pady=8)

        ip = self.icon_px
        icon = tk.Canvas(row, width=ip, height=ip, bg=bg, highlightthickness=0, bd=0)
        icon.grid(row=0, column=1, padx=(0, 10))
        pad = max(1, ip // 12)
        icon.create_rectangle(pad, pad, ip - pad, ip - pad, fill=BORDER, outline="")
        icon.create_text(ip // 2, ip // 2, text=(app.name[:1] or "?").upper(),
                         fill=MUTED, font=(FONT, max(8, ip // 3), "bold"))

        cell = tk.Frame(row, bg=bg)
        cell.grid(row=0, column=2, sticky="w")
        name_lbl = tk.Label(cell, text=_short(app.name, 46), font=(FONT, 9, "bold"),
                            fg=TEXT, bg=bg, anchor="w")
        name_lbl.pack(side="left")
        ver_lbl = tk.Label(cell, text=_short(app.version, 16), font=(FONT, 8), fg=DIM, bg=bg)
        ver_lbl.pack(side="left", padx=(8, 0))
        pub_lbl = tk.Label(row, text=_short(app.publisher, 24), font=(FONT, 8), fg=MUTED,
                           bg=bg, anchor="w")
        pub_lbl.grid(row=0, column=3, sticky="w")

        if app.locked:
            tag, color = "PROTECTED", DIM
        elif app.risky:
            tag, color = "⚠ SYSTEM", AMBER
        elif app.kind == "store":
            tag, color = "STORE", PURPLE
        else:
            tag, color = "DESKTOP", MUTED
        tag_lbl = tk.Label(row, text=tag, font=(FONT, 8, "bold"), fg=color, bg=bg, anchor="w")
        tag_lbl.grid(row=0, column=4, sticky="w")
        size_lbl = tk.Label(row, text=self._size_text(app), font=(FONT, 9, "bold"), fg=MUTED,
                            bg=bg, anchor="e")
        size_lbl.grid(row=0, column=5, sticky="e", padx=(0, 16))

        def toggle(_event=None, a=app, v=var):
            if a.locked or self.uninstalling:
                return
            v.set(not v.get())

        for widget in (row, icon, cell, name_lbl, ver_lbl, pub_lbl, tag_lbl, size_lbl, chk):
            widget.bind("<Button-1>", toggle)
        var.trace_add("write", lambda *_a, k=app.key: self._on_app_selection(k))
        self.app_rows[app.key] = {"app": app, "row": row, "var": var,
                                  "name": name_lbl, "size": size_lbl, "icon": icon, "img": None}

    def _sort_apps(self, key: str):
        current, desc = self.app_sort
        self.app_sort = (key, not desc) if key == current else (key, key == "size")
        self._render_apps()

    def _render_apps(self, reset: bool = True):
        query = self.app_search.get().strip().lower()
        key, desc = self.app_sort
        rows = list(self.app_rows.values())
        if key == "size":
            rows.sort(key=lambda r: (r["app"].size or 0, r["app"].name.lower()), reverse=desc)
        else:
            rows.sort(key=lambda r: r["app"].name.lower(), reverse=desc)
        for row in self.app_rows.values():
            row["row"].pack_forget()
        visible = 0
        for row in rows:
            app = row["app"]
            if query and query not in app.name.lower() and query not in app.publisher.lower():
                continue
            row["row"].pack(fill="x", padx=10, pady=2)
            visible += 1
        for k, (lbl, label) in self.app_heads.items():
            arrow = (" ▼" if desc else " ▲") if k == key else ""
            lbl.configure(text=label + arrow, fg=TEXT if k == key else MUTED)
        if reset:
            self.app_canvas.yview_moveto(0)
        if query and not visible and self.apps:
            self.apps_status.set("No program matches your search.")
        self._update_apps_summary()

    # -- selection -----------------------------------------------------
    def _selected_apps(self) -> list[AppEntry]:
        return [r["app"] for r in self.app_rows.values() if r["var"].get()]

    def _on_app_selection(self, key: str):
        row = self.app_rows.get(key)
        if row:
            row["name"].configure(fg="#ff9a9a" if row["var"].get() else TEXT)
        self._update_apps_summary()
        self._update_uninstall_button()

    def _clear_app_selection(self):
        if self.uninstalling:
            return
        for row in self.app_rows.values():
            row["var"].set(False)

    def _update_apps_summary(self):
        selected = self._selected_apps()
        shown = sum(1 for r in self.app_rows.values() if r["row"].winfo_manager())
        text = f"{shown} of {len(self.apps)} programs"
        if selected:
            text += f"   ·   {len(selected)} selected   ·   {human_bytes(sum(a.size or 0 for a in selected))}"
        self.apps_summary.set(text)

    def _update_uninstall_button(self):
        count = len(self._selected_apps())
        can = count > 0 and not self.running and not self.uninstalling and not self.apps_loading
        label = f"UNINSTALL SELECTED ({count})" if count else "UNINSTALL SELECTED"
        self.uninstall_btn.set_state(can, label)

    # -- uninstall -----------------------------------------------------
    def start_uninstall(self):
        if self.uninstalling:
            return
        if self.running:
            messagebox.showinfo(APP_NAME, "Maintenance is running.\n\n"
                                "Wait for it to finish (or stop it) before removing programs.")
            return
        chosen = self._selected_apps()
        if not chosen:
            return

        lines = [f"Force-uninstall {len(chosen)} program(s)?", ""]
        for app in chosen[:12]:
            size = self._size_text(app)
            lines.append(f"•  {_short(app.name, 52)}" + (f"   ({size})" if size not in ("…", "—") else ""))
        if len(chosen) > 12:
            lines.append(f"•  ... and {len(chosen) - 12} more")
        risky = [a.name for a in chosen if a.risky]
        if risky:
            lines += ["", "WARNING - these look like system runtimes or drivers that other "
                      "programs may depend on:",
                      ", ".join(_short(n, 34) for n in risky[:6]) + (" ..." if len(risky) > 6 else "")]
        lines += [
            "", "For each one WinSmoother will:",
            "•  Close it and stop its services, even if it is open",
            "•  Run its uninstaller silently",
            "•  Delete leftover files, services and its registry entry",
            "", "This cannot be undone and unsaved work in those programs is lost.",
            "", "Continue?",
        ]
        if not messagebox.askyesno(APP_NAME, "\n".join(lines), icon="warning"):
            return

        self.uninstalling = True
        self.uninstall_cancel = Canceller()
        self.disk_before_apps = system_drive_usage()
        self._start_text = self.start_btn.cget("text")
        self.start_btn.set_state(False, "REMOVING PROGRAMS...")
        self.uninstall_btn.set_state(False, "UNINSTALLING...")
        self.app_stop_btn.set_state(True, "STOP")
        self.app_progress["value"] = 0
        self.apps_status.set("Starting...")
        prune_logs()
        threading.Thread(target=self._uninstall_worker,
                         args=(chosen, self.uninstall_cancel), daemon=True).start()

    def stop_uninstall(self):
        if not (self.uninstalling and self.uninstall_cancel) or self.uninstall_cancel.cancelled:
            return
        if not messagebox.askyesno(
            APP_NAME,
            "Stop removing programs?\n\nThe program being removed right now is interrupted "
            "and may be left half-removed. Programs already removed stay removed.",
            icon="warning",
        ):
            return
        self.app_stop_btn.set_state(False, "STOPPING...")
        self.apps_status.set("Stopping - please wait...")
        threading.Thread(target=self.uninstall_cancel.cancel, daemon=True).start()

    def _uninstall_worker(self, chosen, cancel: Canceller):
        logger = Logger(log_dir() / f"run_{datetime.now():%Y%m%d_%H%M%S}_uninstall.log")
        results = []
        total = len(chosen)
        try:
            for i, app in enumerate(chosen):
                if cancel.cancelled:
                    break

                def step(text, frac=0.0, _i=i):
                    self.events.put(("apps_prog", int((_i + frac) / total * 100),
                                     f"[{_i + 1}/{total}] {text}"))

                step(f"Removing {app.name}...", 0.02)
                result = uninstall_app(app, logger, cancel, step)
                results.append(result)
                self.events.put(("app_result", result))
        except Exception as exc:
            logger.write(f"FATAL UNINSTALL ERROR: {type(exc).__name__}: {exc}")
        finally:
            logger.close()
            self.events.put(("apps_done", results))

    def _on_app_result(self, result: dict):
        row = self.app_rows.get(result["key"])
        if not row:
            return
        look = {"removed": ("REMOVED", GREEN), "reboot": ("RESTART", AMBER),
                "failed": ("FAILED", RED), "stopped": ("STOPPED", AMBER)}
        text, color = look.get(result["status"], ("?", MUTED))
        row["size"].configure(text=text, fg=color)

    def _on_apps_done(self, results):
        self.uninstalling = False
        self.start_btn.set_state(not self.running, self._start_text)
        self.app_stop_btn.set_state(False, "STOP")
        self.app_progress["value"] = 100 if results else 0

        after = system_drive_usage()
        before = self.disk_before_apps
        freed = max(0, after[1] - before[1]) if before and after else 0
        self._refresh_free_space()

        done = [r for r in results if r["status"] in ("removed", "reboot")]
        failed = [r for r in results if r["status"] == "failed"]
        stopped = any(r["status"] == "stopped" for r in results) or (
            self.uninstall_cancel is not None and self.uninstall_cancel.cancelled)
        reboot = any(r["status"] == "reboot" for r in results)

        lines = [f"Removed {len(done)} of {len(results)} program(s)."]
        if freed:
            lines.append(f"Free space on {self.sys_drive} increased by {human_bytes(freed)}.")
        if stopped:
            lines.append("The run was stopped before it finished.")
        if reboot:
            lines.append("Restart Windows to finish deleting files that were locked.")
        if failed:
            lines += ["", "Could not remove:"]
            lines += [f"•  {_short(r['name'], 40)} - {_short(r['message'], 90)}" for r in failed[:10]]
        lines += ["", "Details are in the logs folder (Report page → Open logs folder)."]
        self.apps_status.set(lines[0] + (f" {lines[1]}" if freed else ""))
        (messagebox.showwarning if failed else messagebox.showinfo)(APP_NAME, "\n".join(lines))

        self._load_apps()
        self._update_uninstall_button()

    # --------------------------------------------------------- navigation
    def _nav(self, name: str):
        if name == "Report" and not (self.cleanup_result or self.repair_result):
            return
        self._show_view(name)

    def _show_view(self, name: str):
        self.current_view = name
        for view in self.views.values():
            view.pack_forget()
        self.views[name].pack(fill="both", expand=True)
        self.page_title.configure(text=name)
        for label, (bar, btn) in self.nav.items():
            active = label == name
            bar.configure(bg=GREEN if active else SIDEBAR)
            btn.configure(bg=PANEL if active else SIDEBAR, fg=TEXT if active else MUTED)
        if name == "Report":
            self._refresh_report_view()
        if name == "Apps" and not self.apps_loaded_once:
            self._load_apps()

    def _set_report_enabled(self, enabled: bool):
        self.report_button.set_state(enabled)
        self.nav["Report"][1].configure(state="normal" if enabled else "disabled")

    # ----------------------------------------------------------- progress
    def _mark_phase(self, phase: str, state: str):
        dot, label = self.phase_rows[phase]
        if phase in self.skipped_phases:
            dot.configure(fg="#3d485b")
            label.configure(text="SKIPPED", fg=DIM)
            return
        look = {
            "RUNNING": (PURPLE, "RUNNING", PURPLE),
            "DONE": (GREEN, "DONE", GREEN),
            "ERROR": (RED, "CHECK REPORT", RED),
            "STOPPED": (AMBER, "STOPPED", AMBER),
        }.get(state, ("#3d485b", "WAITING", MUTED))
        dot.configure(fg=look[0])
        label.configure(text=look[1], fg=look[2])

    def _update_progress(self, pct: int, stage: str, phase: str):
        pct = max(0, min(100, int(pct)))
        if phase != self.stage_name:
            self.stage_name, self.stage_started, self.stage_real = phase, time.monotonic(), False
        if "%)" in stage:
            self.stage_real = True
        self.pct_shown = pct
        self.progress["value"] = pct
        self.percent_var.set(f"{pct}%")
        self.status_var.set(stage)
        self.phase_var.set(PHASE_LABEL.get(phase, phase.upper()))

        if phase == "done":
            for key in PHASES:
                self._mark_phase(key, "DONE")
            return
        current = PHASE_OF.get(phase)
        if current:
            idx = PHASES.index(current)
            for i, key in enumerate(PHASES):
                self._mark_phase(key, "DONE" if i < idx else "RUNNING" if i == idx else "WAITING")

    def _tick(self):
        """Heartbeat: DISM prints no live percentage when its output is piped, so show
        elapsed time and let the bar creep slowly (never past 90 % of the stage)."""
        try:
            step = STEP_BY_NAME.get(self.stage_name)
            if self.running and step and not (self.cancel and self.cancel.cancelled):
                elapsed = int(time.monotonic() - self.stage_started)
                note = ""
                if not self.stage_real:
                    frac = 0.9 * (1 - math.exp(-elapsed / (step["timeout"] / 6)))
                    pct = step["start"] + (step["end"] - step["start"]) * frac
                    if pct > self.pct_shown:
                        self.pct_shown = pct
                        self.progress["value"] = pct
                        self.percent_var.set(f"{int(pct)}%")
                    note = " - Windows shows no live percentage for this step; staying at the same point for several minutes is normal."
                m, sec = divmod(elapsed, 60)
                self.status_var.set(f"{step['stage']} - {m:02d}:{sec:02d} elapsed.{note}")
        finally:
            self.root.after(1000, self._tick)

    def _set_initial_state(self):
        self.skipped_phases = set() if self.last_options["repair"] else {"repair", "optimize"}
        for card in (self.items_card, self.space_card, self.repair_card, self.skip_card):
            card.configure(text="—")
        for key in PHASES:
            self._mark_phase(key, "WAITING")
        self._update_progress(0, "Preparing maintenance...", "cleanup")

    # ------------------------------------------------------------ actions
    def _confirm_start(self, opts) -> bool:
        lines = ["WinSmoother will:", "", "•  Delete temporary files and caches"]
        if opts["bin"]:
            lines.append("•  Empty the Recycle Bin - this cannot be undone")
        if opts["repair"]:
            lines.append("•  Run Windows repair (DISM, SFC, CHKDSK) - can take 30-60+ minutes")
        lines += ["", "Documents, downloads and settings are not touched.", "", "Continue?"]
        return messagebox.askyesno(APP_NAME, "\n".join(lines), icon="question")

    def start(self):
        if self.running:
            return
        if self.uninstalling:
            messagebox.showinfo(APP_NAME, "Programs are being removed right now.\n\n"
                                "Please wait for that to finish first.")
            return
        opts = {"repair": bool(self.opt_repair.get()), "bin": bool(self.opt_bin.get())}
        if not self._confirm_start(opts):
            return

        self.last_options = opts
        self.disk_before = system_drive_usage()
        self.disk_after = None
        self.running = True
        self.outcome = ""
        self.started_at = time.perf_counter()
        self.cleanup_result, self.repair_result = {}, {}
        self.cancel = Canceller()
        self.stage_name, self.pct_shown = "", 0.0

        self._set_report_enabled(False)
        self.start_btn.set_state(False, "MAINTENANCE RUNNING...")
        self.stop_btn.set_state(True, "STOP")
        self.chk_repair.set_enabled(False)
        self.chk_bin.set_enabled(False)
        self._update_uninstall_button()
        self._set_initial_state()

        prune_logs()
        self.log_path = log_dir() / f"run_{datetime.now():%Y%m%d_%H%M%S}.log"
        threading.Thread(target=self._worker, args=(opts, self.cancel), daemon=True).start()

    def stop(self):
        if not (self.running and self.cancel) or self.cancel.cancelled:
            return
        extra = ""
        if self.stage_name in STEP_BY_NAME:
            extra = ("\n\nWindows repair can take 30-60+ minutes and may look idle "
                     "while it works. Stopping now discards this stage's progress.")
        if not messagebox.askyesno(
            APP_NAME,
            "Stop the maintenance run?\n\nThe stage in progress will be interrupted. "
            "Files already cleaned stay cleaned." + extra,
            icon="warning",
        ):
            return
        self.stop_btn.set_state(False, "STOPPING...")
        self.status_var.set("Stopping - please wait...")
        threading.Thread(target=self.cancel.cancel, daemon=True).start()

    def _on_close(self):
        if self.uninstalling:
            if not messagebox.askyesno(
                APP_NAME, "Programs are still being removed.\n\nStop and exit?", icon="warning"
            ):
                return
            if self.uninstall_cancel:
                self.uninstall_cancel.cancel()
        if self.running:
            if not messagebox.askyesno(
                APP_NAME, "Maintenance is still running.\n\nStop it and exit?", icon="warning"
            ):
                return
            if self.cancel:
                self.cancel.cancel()
        self.root.destroy()

    # ------------------------------------------------------------- worker
    def _worker(self, opts: dict, cancel: Canceller):
        logger = Logger(self.log_path)
        put = self.events.put

        def report(pct, label, phase):
            put(("progress", pct, label, phase))

        try:
            span = 50 if opts["repair"] else 100
            report(0, "Starting cleanup", "cleanup")
            self.cleanup_result = run_cleaner(
                logger, progress=report, cancel=cancel, empty_bin=opts["bin"], span=span)
            if cancel.cancelled:
                put(("stopped",))
                return
            report(span, "Cleanup complete", "cleanup")

            if opts["repair"]:
                self.repair_result = run_repair(logger, progress=report, cancel=cancel)
                if cancel.cancelled:
                    put(("stopped",))
                    return

            report(100, "Maintenance complete", "done")
            put(("done",))
        except Exception as exc:
            logger.write(f"FATAL ERROR: {type(exc).__name__}: {exc}")
            put(("error", str(exc)))
        finally:
            logger.close()

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                try:
                    self._dispatch(event)
                except Exception:  # one bad event must never stop the UI from updating
                    pass
        except queue.Empty:
            pass
        self.root.after(80, self._poll_events)

    def _dispatch(self, event):
        kind = event[0]
        if kind == "progress":
            self._update_progress(event[1], event[2], event[3])
        elif kind == "done":
            self._finish("done")
        elif kind == "stopped":
            self._finish("stopped")
        elif kind == "error":
            self._finish("error", event[1])
        elif kind == "apps_loaded":
            self._on_apps_loaded(event[1], event[2])
        elif kind == "apps_error":
            self.apps_loading = False
            self.apps_status.set("Could not read the installed programs: " + event[1])
            self._update_uninstall_button()
        elif kind == "app_icon":
            self._on_app_icon(event[1], event[2], event[3])
        elif kind == "app_size":
            self._on_app_size(event[1], event[2], event[3])
        elif kind == "apps_sized":
            self._on_apps_sized(event[1])
        elif kind == "apps_prog":
            self.app_progress["value"] = event[1]
            self.apps_status.set(event[2])
        elif kind == "app_result":
            self._on_app_result(event[1])
        elif kind == "apps_done":
            self._on_apps_done(event[1])

    # ------------------------------------------------------------- finish
    def _finish(self, outcome: str, message: str = ""):
        self.running = False
        self.stage_name = ""
        self.outcome = outcome
        self.duration = int(time.perf_counter() - self.started_at) if self.started_at else 0

        cleanup, repair = self.cleanup_result, self.repair_result
        self.items_card.configure(text=f"{cleanup.get('items_removed', 0):,}")
        # Real numbers straight from the file system: free space now vs. before the run.
        self.disk_after = system_drive_usage()
        if self.disk_before and self.disk_after:
            self.space_recovered = max(0, self.disk_after[1] - self.disk_before[1])
        else:
            self.space_recovered = cleanup.get("bytes_removed", 0)
        self.space_card.configure(text=human_bytes(self.space_recovered))
        self._refresh_free_space()
        self.skip_card.configure(text=f"{cleanup.get('skipped', 0):,}")
        if self.last_options["repair"]:
            self.repair_card.configure(text=f"{repair.get('succeeded', 0)}/{len(PLAN)}")
        else:
            self.repair_card.configure(text="Skipped")

        self.start_btn.set_state(True, "RUN MAINTENANCE AGAIN  →")
        self.stop_btn.set_state(False, "STOP")
        self.chk_repair.set_enabled(True)
        self.chk_bin.set_enabled(True)
        self._set_report_enabled(bool(cleanup or repair))
        self._update_uninstall_button()
        self._write_json_report()

        if outcome == "done":
            failed = repair.get("failed", 0)
            note = self._free_note()
            self.status_var.set(
                ("Maintenance finished. Open the report for the full result." if not failed else
                 f"Maintenance finished with {failed} repair stage(s) needing attention. "
                 "Open the report for details.") + note)
            self._update_progress(100, self.status_var.get(), "done")
            if failed:
                self.phase_var.set(PHASE_LABEL["error"])
                for item in repair.get("results", []):
                    if not item["success"]:
                        self._mark_phase(PHASE_OF[item["name"]], "ERROR")
        elif outcome == "stopped":
            current = PHASE_OF.get(self._phase_from_label(), "cleanup")
            self._mark_phase(current, "STOPPED")
            self.phase_var.set(PHASE_LABEL["stopped"])
            self.status_var.set("Stopped. Partial results are available in the report.")
        else:
            self.phase_var.set(PHASE_LABEL["error"])
            self.status_var.set("Maintenance ended unexpectedly. See the report or logs.")
            messagebox.showerror(
                APP_NAME, "The maintenance worker stopped unexpectedly.\n\n" + message)

    def _free_note(self) -> str:
        if self.disk_before and self.disk_after:
            return (f" Free space on {self.sys_drive} {human_bytes(self.disk_before[1])} → "
                    f"{human_bytes(self.disk_after[1])}"
                    f" ({'+' if self.disk_after[1] >= self.disk_before[1] else '-'}"
                    f"{human_bytes(abs(self.disk_after[1] - self.disk_before[1]))}).")
        return ""

    def _refresh_free_space(self):
        usage = system_drive_usage()
        if usage:
            self.free_card.configure(text=human_bytes(usage[1]))

    def _phase_from_label(self) -> str:
        current = self.phase_var.get()
        for key, label in PHASE_LABEL.items():
            if label == current:
                return key
        return "cleanup"

    def _write_json_report(self):
        if not self.log_path:
            return
        payload = {
            "app": f"{APP_NAME} {APP_VERSION}",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "outcome": self.outcome,
            "duration_seconds": self.duration,
            "options": self.last_options,
            "disk": {
                "drive": self.sys_drive,
                "free_before": self.disk_before[1] if self.disk_before else None,
                "free_after": self.disk_after[1] if self.disk_after else None,
                "space_recovered": self.space_recovered,
            },
            "cleanup": self.cleanup_result,
            "repair": self.repair_result,
        }
        try:
            self.log_path.with_suffix(".json").write_text(
                json.dumps(payload, indent=2, default=str), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------- report
    def _open_logs(self):
        try:
            folder = log_dir()
            folder.mkdir(parents=True, exist_ok=True)
            if IS_WINDOWS:
                os.startfile(str(folder))  # noqa: S606 - opens Explorer on our own log folder
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not open the logs folder.\n\n{exc}")

    def _refresh_report_view(self):
        view = self.views["Report"]
        for child in view.winfo_children():
            child.destroy()

        cleanup, repair = self.cleanup_result, self.repair_result
        headline = {"done": "Completed", "stopped": "Stopped by user", "error": "Ended with an error"}
        inv = cleanup.get("inventory", {})
        meta = " · ".join(filter(None, (
            headline.get(self.outcome, ""),
            f"{self.duration // 60}m {self.duration % 60}s",
            inv.get("computer", ""),
            self._free_note().strip(),
        )))
        self._label(view, meta, 9, False, MUTED, BG).pack(anchor="w", pady=(2, 14))

        cards = tk.Frame(view, bg=BG)
        cards.pack(fill="x", pady=(0, 14))
        self._card(cards, 0, "Files cleaned", f"{cleanup.get('items_removed', 0):,}", GREEN)
        self._card(cards, 1, "Space recovered", human_bytes(self.space_recovered), PURPLE)
        repair_text = (f"{repair.get('succeeded', 0)}/{len(PLAN)}"
                       if self.last_options["repair"] else "Skipped")
        self._card(cards, 2, "Repair stages", repair_text, AMBER)
        self._card(cards, 3, "Skipped / in use", f"{cleanup.get('skipped', 0):,}", RED)

        table = self._panel(view)
        table.pack(fill="both", expand=True)
        table.grid_columnconfigure(2, weight=1)
        for col, head in enumerate(("Repair stage", "Status", "Details")):
            self._label(table, head.upper(), 8, True, MUTED).grid(
                row=0, column=col, sticky="w", padx=18, pady=12)

        results = repair.get("results", [])
        if not results:
            note = "Windows repair was not run." if not self.last_options["repair"] else "No repair stage finished."
            self._label(table, note, 9, False, MUTED).grid(
                row=1, column=0, columnspan=3, sticky="w", padx=18, pady=8)
        for row, item in enumerate(results, start=1):
            ok = item["success"]
            self._label(table, item["name"], 9, True).grid(row=row, column=0, sticky="w", padx=18, pady=6)
            self._label(table, "SUCCESS" if ok else "ATTENTION", 9, True, GREEN if ok else RED).grid(
                row=row, column=1, sticky="w", padx=18, pady=6)
            detail = item.get("message", "")
            if item.get("retried"):
                detail += " · automatic retry"
            self._label(table, detail, 9, False, MUTED).grid(row=row, column=2, sticky="w", padx=18, pady=6)

        footer = tk.Frame(view, bg=BG)
        footer.pack(fill="x", pady=12)
        self._label(footer, "Detailed logs are stored privately in AppData.", 8, False, DIM, BG).pack(side="left")
        self._button(footer, "BACK TO OVERVIEW", lambda: self._show_view("Overview"),
                     padx=14, pady=8).pack(side="right")
        self._button(footer, "OPEN LOGS FOLDER", self._open_logs,
                     padx=14, pady=8).pack(side="right", padx=(0, 10))

    def run(self):
        self.root.mainloop()
