# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape Studio - Professional Desktop Photogrammetry GUI.

A high-taste, responsive desktop application for autonomous 3D photogrammetry.
Designed following modern design-taste principles and app-screen standards:
- Responsive snap-scale grid layout (two-column master-detail on desktop, single-column stack on compact)
- Global smooth mouse-wheel & touchpad scrolling across all surfaces
- Full-width canvas utilization eliminating dead whitespace and clipped controls
- Neutral deep dark ground (#0c0c0e) with quiet 1px boundaries (#27272a)
- Colour applied strictly to data and semantic states, not chrome (<= 3% colour share)
- Zero decorative emoji in greetings, labels, nav, or buttons
- Clear typographic hierarchy with 4.5:1+ contrast across all text
- Windows Per-Monitor v2 DPI auto-scaling and dynamic monitor-aware layout
- 1-2-3 Progressive Disclosure (Inputs -> Preset -> Run)
- Integrated 7-stage milestone tracker and seamless manual crop pause/resume
- Real-time execution log with syntax highlighting and direct OS actions
"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
import webbrowser

try:
    from metashape_reconstruction import analyze_survey_physics, derive_pro_photogrammetry_plan, infer_project_name
except ImportError:
    try:
        from scripts.metashape_reconstruction import analyze_survey_physics, derive_pro_photogrammetry_plan, infer_project_name
    except ImportError:
        analyze_survey_physics = None
        derive_pro_photogrammetry_plan = None
        infer_project_name = None

# ---------------------------------------------------------------------------
# High-DPI Crisp Text & Per-Monitor v2 Awareness (Windows 10 / 11)
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Visual Theme Tokens (Design-Taste App Screen Standards)
# ---------------------------------------------------------------------------
class UITheme:
    """Neutral dark app-screen palette following Leonxlnx/taste-skill rules."""

    # Ground & Surfaces (Neutral deep dark, zero blue/navy tint)
    BG_ROOT = "#0c0c0e"             # Neutral ground
    BG_SURFACE = "#151518"          # 1 tone step above ground
    BG_SURFACE_RAISED = "#1c1c20"   # Raised panels / hover
    BG_INPUT = "#101012"            # Crisp dark input field fill
    BG_CONSOLE = "#08080a"          # Pure terminal console black
    BG_ACTIVE = "#1e1e24"           # Active surface

    # Borders & Rules (Quiet 1px lines, no loud glowing outlines)
    BORDER = "#27272a"              # Quiet 1px panel border
    BORDER_CONTROL = "#36363a"      # Input / control outline
    BORDER_HOVER = "#484850"        # Hover outline
    BORDER_FOCUS = "#38bdf8"        # Focus outline
    RULE = "#1e1e22"                # Subtle inner divider

    # Typography (High contrast, neo-grotesk hierarchy)
    TEXT_PRIMARY = "#ffffff"        # Headings & primary labels
    TEXT_SECONDARY = "#a1a1aa"      # Form labels & descriptions
    TEXT_MUTED = "#71717a"          # Captions, meta, and secondary notes
    TEXT_DIM = "#52525b"            # Placeholder & disabled text

    # Data Accents (Restrained semantic application)
    ACCENT_CYAN = "#38bdf8"         # Sky cyan (for active milestone & primary CTA)
    ACCENT_CYAN_HOVER = "#0284c7"
    ACCENT_GREEN = "#247b50"        # Semantic positive / success
    ACCENT_GREEN_BG = "#102318"     # Subtle green tint
    ACCENT_GREEN_TXT = "#3fc07a"    # Accessible mint text
    ACCENT_AMBER = "#d97706"        # Semantic warning / paused
    ACCENT_AMBER_BG = "#261706"     # Subtle amber tint
    ACCENT_AMBER_TXT = "#f59e0b"    # Accessible amber text
    ACCENT_RED = "#dc2626"          # Semantic error / stop
    ACCENT_RED_HOVER = "#b91c1c"


# ---------------------------------------------------------------------------
# Supported Media Extensions
# ---------------------------------------------------------------------------
SUPPORTED_IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".tif", ".tiff", ".bmp", ".dib",
    ".webp", ".jp2", ".j2k", ".jpc", ".j2c", ".jxl", ".exr", ".tga",
    ".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf",
    ".sr2", ".iiq", ".cap", ".3fr", ".fff", ".rw2", ".orf", ".raf",
    ".raw", ".rwl", ".pef", ".ptx", ".seq", ".ara",
}

DEFAULT_METASHAPE_PATHS = [
    r"C:\Program Files\Agisoft\Metashape Pro\metashape.exe",
    r"C:\Program Files\Agisoft\PhotoScan Pro\photoscan.exe",
]


def find_metashape_path() -> str | None:
    """Locate Agisoft Metashape Pro executable on the system."""
    for p in DEFAULT_METASHAPE_PATHS:
        if os.path.isfile(p):
            return p
    which_p = shutil.which("metashape")
    if which_p and os.path.isfile(which_p):
        return which_p
    return None


def format_file_size(size_bytes: int) -> str:
    """Format bytes into human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


# ---------------------------------------------------------------------------
# Studio Card Container (Quiet 1px Border, No Eyebrows)
# ---------------------------------------------------------------------------
class StudioCard(tk.Frame):
    """Sleek container with quiet 1px line, zero decorative eyebrows."""

    def __init__(
        self,
        parent: tk.Widget,
        title: str = "",
        subtitle: str = "",
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            bg=UITheme.BORDER,
            bd=1,
            relief=tk.FLAT,
            **kwargs,
        )
        self.inner = tk.Frame(self, bg=UITheme.BG_SURFACE)
        self.inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        self.title_text = title
        self.subtitle_text = subtitle

        if title or subtitle:
            self._build_header()

        self.body = tk.Frame(self.inner, bg=UITheme.BG_SURFACE)
        self.body.pack(fill=tk.BOTH, expand=True, padx=14, pady=(4, 12))

    def _build_header(self) -> None:
        header_bar = tk.Frame(self.inner, bg=UITheme.BG_SURFACE)
        header_bar.pack(fill=tk.X, padx=14, pady=(10, 4))

        lbl_title = tk.Label(
            header_bar,
            text=self.title_text,
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_PRIMARY,
        )
        lbl_title.pack(side=tk.LEFT)

        if self.subtitle_text:
            self.lbl_sub = tk.Label(
                header_bar,
                text=self.subtitle_text,
                font=("Segoe UI", 8),
                bg=UITheme.BG_SURFACE,
                fg=UITheme.TEXT_MUTED,
            )
            self.lbl_sub.pack(side=tk.RIGHT)

    def set_subtitle(self, text: str) -> None:
        self.subtitle_text = text
        if hasattr(self, "lbl_sub"):
            self.lbl_sub.config(text=text)


# ---------------------------------------------------------------------------
# Pipeline Milestone Definitions
# ---------------------------------------------------------------------------
STAGES_RECON = [
    ("1", "Ingest", "Photos & blur filter"),
    ("2", "Match", "GPU feature matching"),
    ("3", "Align", "Cameras & crop"),
    ("4", "Depth", "Dense stereopsis"),
    ("5", "Mesh", "Surface mesh"),
    ("6", "Texture", "Texture atlas"),
    ("7", "Export", "GLB, OBJ & TIFF"),
]

STAGES_EXPORT = [
    ("1", "Project", "Load .psx project"),
    ("2", "Verify", "Validate 3D mesh"),
    ("3", "Texture", "Re-bake atlas"),
    ("4", "glTF", "Export binary GLB"),
    ("5", "Wavefront", "Export OBJ & TIFF"),
]

STAGES_MULTI = [
    ("1", "Discover", "Scan & detect inputs"),
    ("2", "Queue", "Order survey batch"),
    ("3", "Execute", "Sequential run"),
    ("4", "Telemetry", "GSD & error report"),
]


# ---------------------------------------------------------------------------
# Horizontal Pipeline Stepper (Dynamic Milestones Tracker)
# ---------------------------------------------------------------------------
class StudioPipelineStepper(tk.Frame):
    """Refined milestone trajectory tracking all pipeline stages with honest 1:1 mapping."""

    def __init__(
        self,
        parent: tk.Widget,
        stages: list[tuple[str, str, str]] = None,
        **kwargs,
    ) -> None:
        super().__init__(parent, bg=UITheme.BG_SURFACE, **kwargs)
        self.stage_frames: list[tk.Frame] = []
        self.stage_indicators: list[tk.Label] = []
        self.stage_titles: list[tk.Label] = []
        self.stage_subtitles: list[tk.Label] = []

        self.cards_row = tk.Frame(self, bg=UITheme.BG_SURFACE)
        self.cards_row.pack(fill=tk.X, padx=0, pady=(0, 6))

        # Status summary bar under stages
        self.info_bar = tk.Frame(self, bg=UITheme.BG_ROOT, padx=10, pady=5)
        self.info_bar.pack(fill=tk.X)

        self.lbl_pipeline_status = tk.Label(
            self.info_bar,
            text="Status: Ready to reconstruct",
            font=("Segoe UI", 8),
            bg=UITheme.BG_ROOT,
            fg=UITheme.TEXT_SECONDARY,
            anchor=tk.W,
        )
        self.lbl_pipeline_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.lbl_timer = tk.Label(
            self.info_bar,
            text="Elapsed: --:--",
            font=("Consolas", 8),
            bg=UITheme.BG_ROOT,
            fg=UITheme.TEXT_MUTED,
        )
        self.lbl_timer.pack(side=tk.RIGHT)

        self.build_stages(stages or STAGES_RECON)

    def build_stages(self, stages: list[tuple[str, str, str]]) -> None:
        """Dynamically construct stage pill cards scaled to milestone count."""
        self.stages = stages
        for child in self.cards_row.winfo_children():
            child.destroy()

        self.stage_frames.clear()
        self.stage_indicators.clear()
        self.stage_titles.clear()
        self.stage_subtitles.clear()

        n = len(self.stages)
        pad_x = 3 if n > 8 else (4 if n > 6 else 6)
        pad_y = 3 if n > 8 else (4 if n > 6 else 6)
        f_ind = ("Segoe UI", 7, "bold") if n > 8 else ("Segoe UI", 8, "bold")
        f_title = ("Segoe UI", 7, "bold") if n > 8 else ("Segoe UI", 8, "bold")
        f_sub = ("Segoe UI", 6) if n > 8 else ("Segoe UI", 7)

        for i, (num, name, subtitle) in enumerate(self.stages):
            outer = tk.Frame(self.cards_row, bg=UITheme.BORDER, bd=1, relief=tk.FLAT)
            outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=1 if n > 8 else 2)

            inner = tk.Frame(outer, bg=UITheme.BG_ROOT, padx=pad_x, pady=pad_y)
            inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

            top_row = tk.Frame(inner, bg=UITheme.BG_ROOT)
            top_row.pack(fill=tk.X)

            ind = tk.Label(
                top_row,
                text=f"{num}.",
                font=f_ind,
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_DIM,
            )
            ind.pack(side=tk.LEFT)

            t = tk.Label(
                inner,
                text=name,
                font=f_title,
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_MUTED,
                anchor=tk.W,
            )
            t.pack(fill=tk.X, pady=(2, 0))

            s = tk.Label(
                inner,
                text=subtitle,
                font=f_sub,
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_DIM,
                anchor=tk.W,
            )
            s.pack(fill=tk.X)

            self.stage_frames.append(outer)
            self.stage_indicators.append(ind)
            self.stage_titles.append(t)
            self.stage_subtitles.append(s)

        self.reset()

    def reset(self) -> None:
        self.current_active_idx = -1
        self.is_done = False
        self.is_paused = False
        self.pause_idx = -1
        self.pause_msg = ""
        self.current_desc = ""

        for i, (num, name, subtitle) in enumerate(self.stages):
            outer = self.stage_frames[i]
            inner = outer.winfo_children()[0]
            outer.config(bg=UITheme.BORDER)
            inner.config(bg=UITheme.BG_ROOT)
            for child in inner.winfo_children():
                child.config(bg=UITheme.BG_ROOT)
                if hasattr(child, "winfo_children"):
                    for sub in child.winfo_children():
                        sub.config(bg=UITheme.BG_ROOT)
            self.stage_indicators[i].config(text=f"{num}.", fg=UITheme.TEXT_DIM)
            self.stage_titles[i].config(fg=UITheme.TEXT_MUTED)
            self.stage_subtitles[i].config(fg=UITheme.TEXT_DIM)
        default_status = "Status: Ready to reconstruct" if len(self.stages) == 7 else "Status: Ready to export"
        self.lbl_pipeline_status.config(
            text=default_status,
            fg=UITheme.TEXT_SECONDARY,
        )
        self.lbl_timer.config(text="Elapsed: --:--")

    def get_state(self) -> dict:
        return {
            "stages": list(self.stages),
            "active_idx": getattr(self, "current_active_idx", -1),
            "is_done": getattr(self, "is_done", False),
            "is_paused": getattr(self, "is_paused", False),
            "pause_idx": getattr(self, "pause_idx", -1),
            "pause_msg": getattr(self, "pause_msg", ""),
            "desc": getattr(self, "current_desc", ""),
            "status_text": self.lbl_pipeline_status.cget("text"),
            "status_fg": self.lbl_pipeline_status.cget("fg"),
            "timer_text": self.lbl_timer.cget("text"),
        }

    def restore_state(self, state: dict) -> None:
        stages = state.get("stages", STAGES_RECON)
        if self.stages != stages or len(self.stage_frames) != len(stages):
            self.build_stages(stages)

        if state.get("is_done"):
            self.set_all_done()
        elif state.get("is_paused") and state.get("pause_idx", -1) >= 0:
            self.set_paused(state["pause_idx"], state.get("pause_msg", ""))
        elif state.get("active_idx", -1) >= 0:
            self.set_active(state["active_idx"], state.get("desc", ""))
        else:
            self.reset()

        if "status_text" in state:
            self.lbl_pipeline_status.config(
                text=state["status_text"],
                fg=state.get("status_fg", UITheme.TEXT_SECONDARY),
            )
        if "timer_text" in state:
            self.lbl_timer.config(text=state["timer_text"])

    def set_active(self, idx: int, description: str = "") -> None:
        if not (0 <= idx < len(self.stage_frames)):
            return
        self.current_active_idx = idx
        self.current_desc = description
        self.is_done = False
        self.is_paused = False

        # 1. Past stages (< idx): mark completed with checkmark
        for i in range(idx):
            self._apply_style(
                i,
                outer_bg=UITheme.BORDER,
                inner_bg=UITheme.ACCENT_GREEN_BG,
                ind_text="✓",
                ind_color=UITheme.ACCENT_GREEN_TXT,
                title_color=UITheme.TEXT_PRIMARY,
                sub_color=UITheme.ACCENT_GREEN_TXT,
            )

        # 2. Current stage (== idx): mark active with cyan indicator
        num = self.stages[idx][0]
        name = self.stages[idx][1]
        self._apply_style(
            idx,
            outer_bg=UITheme.ACCENT_CYAN,
            inner_bg="#131e2a",
            ind_text=f"▶ {num}",
            ind_color=UITheme.ACCENT_CYAN,
            title_color=UITheme.TEXT_PRIMARY,
            sub_color=UITheme.ACCENT_CYAN,
        )

        # 3. Future stages (> idx): strictly reset to pending (num., muted text)
        for i in range(idx + 1, len(self.stage_frames)):
            f_num = self.stages[i][0]
            self._apply_style(
                i,
                outer_bg=UITheme.BORDER,
                inner_bg=UITheme.BG_ROOT,
                ind_text=f"{f_num}.",
                ind_color=UITheme.TEXT_DIM,
                title_color=UITheme.TEXT_MUTED,
                sub_color=UITheme.TEXT_DIM,
            )

        desc_text = f"Running Stage {idx+1}/{len(self.stages)}: {name}"
        if description:
            desc_text += f" - {description}"
        self.lbl_pipeline_status.config(text=desc_text, fg=UITheme.ACCENT_CYAN)

    def set_paused(self, idx: int, message: str = "") -> None:
        if not (0 <= idx < len(self.stage_frames)):
            return
        self.is_paused = True
        self.pause_idx = idx
        self.pause_msg = message
        self.is_done = False

        for i in range(idx):
            self._apply_style(
                i,
                outer_bg=UITheme.BORDER,
                inner_bg=UITheme.ACCENT_GREEN_BG,
                ind_text="✓",
                ind_color=UITheme.ACCENT_GREEN_TXT,
                title_color=UITheme.TEXT_PRIMARY,
                sub_color=UITheme.ACCENT_GREEN_TXT,
            )

        num = self.stages[idx][0]
        self._apply_style(
            idx,
            outer_bg=UITheme.ACCENT_AMBER,
            inner_bg=UITheme.ACCENT_AMBER_BG,
            ind_text=f"⏸ {num}",
            ind_color=UITheme.ACCENT_AMBER_TXT,
            title_color=UITheme.TEXT_PRIMARY,
            sub_color=UITheme.ACCENT_AMBER_TXT,
        )

        for i in range(idx + 1, len(self.stage_frames)):
            f_num = self.stages[i][0]
            self._apply_style(
                i,
                outer_bg=UITheme.BORDER,
                inner_bg=UITheme.BG_ROOT,
                ind_text=f"{f_num}.",
                ind_color=UITheme.TEXT_DIM,
                title_color=UITheme.TEXT_MUTED,
                sub_color=UITheme.TEXT_DIM,
            )

        self.lbl_pipeline_status.config(
            text=f"Paused at Stage {idx+1}/{len(self.stages)}: {message}",
            fg=UITheme.ACCENT_AMBER_TXT,
        )

    def set_all_done(self) -> None:
        self.is_done = True
        self.is_paused = False
        self.current_active_idx = len(self.stage_frames)
        for i in range(len(self.stage_frames)):
            self._apply_style(
                i,
                outer_bg=UITheme.BORDER,
                inner_bg=UITheme.ACCENT_GREEN_BG,
                ind_text="✓",
                ind_color=UITheme.ACCENT_GREEN_TXT,
                title_color=UITheme.TEXT_PRIMARY,
                sub_color=UITheme.ACCENT_GREEN_TXT,
            )
        self.lbl_pipeline_status.config(
            text=f"Complete: All {len(self.stages)} stages finished successfully. Deliverables ready on disk.",
            fg=UITheme.ACCENT_GREEN_TXT,
        )

    def _apply_style(
        self,
        idx: int,
        outer_bg: str,
        inner_bg: str,
        ind_text: str,
        ind_color: str,
        title_color: str,
        sub_color: str,
    ) -> None:
        outer = self.stage_frames[idx]
        inner = outer.winfo_children()[0]
        outer.config(bg=outer_bg)
        inner.config(bg=inner_bg)
        for child in inner.winfo_children():
            child.config(bg=inner_bg)
            if hasattr(child, "winfo_children"):
                for sub in child.winfo_children():
                    sub.config(bg=inner_bg)
        self.stage_indicators[idx].config(text=ind_text, fg=ind_color)
        self.stage_titles[idx].config(fg=title_color)
        self.stage_subtitles[idx].config(fg=sub_color)

    def update_elapsed(self, seconds: float) -> None:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        self.lbl_timer.config(text=f"Elapsed: {mins:02d}:{secs:02d}")


# ---------------------------------------------------------------------------
# Main Application Window
# ---------------------------------------------------------------------------
class AutoMetashapeStudio:
    """Professional, responsive photogrammetry desktop studio with snap-scale layout."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AutoMetashape Studio - 3D Photogrammetry")
        self.root.configure(bg=UITheme.BG_ROOT)

        # Scale detection & Monitor-Aware Geometry
        self._init_dpi_and_geometry()

        # Configure dark TTK styles ('clam' theme)
        self._init_ttk_styles()

        # Paths & executables
        self.repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.recon_script = os.path.join(self.repo_dir, "scripts", "metashape_reconstruction.py")
        self.export_script = os.path.join(self.repo_dir, "scripts", "metashape_export.py")
        self.metashape_exe = find_metashape_path()

        # State tracking
        self.current_process: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.is_running = False
        self.user_cancelled = False
        self.worker_mode: str | None = None
        self.start_time: float = 0.0
        self.active_mode = "recon"
        self.adv_expanded = False
        self.selected_preset_key = "pro"
        self.current_layout_mode = None  # "2col" or "1col"
        self.preset_cards: dict[str, dict[str, tk.Widget]] = {}

        # Dynamic tab stage keys and state cache
        self.mode_stage_keys: dict[str, list[str]] = {
            "recon": ["ingest", "match", "align", "depth", "point_cloud", "mesh", "texture", "export", "report"],
            "export": ["project", "mesh", "texture", "glb", "obj", "point_cloud"],
            "multi": ["discover", "select", "execute", "done"],
        }
        self.mode_states = {
            "recon": {
                "stages": list(STAGES_RECON),
                "active_idx": -1,
                "is_done": False,
                "is_paused": False,
                "pause_idx": -1,
                "pause_msg": "",
                "desc": "",
                "status_text": "Status: Ready to reconstruct",
                "status_fg": UITheme.TEXT_SECONDARY,
                "timer_text": "Elapsed: --:--",
            },
            "export": {
                "stages": list(STAGES_EXPORT),
                "active_idx": -1,
                "is_done": False,
                "is_paused": False,
                "pause_idx": -1,
                "pause_msg": "",
                "desc": "",
                "status_text": "Status: Ready to export",
                "status_fg": UITheme.TEXT_SECONDARY,
                "timer_text": "Elapsed: --:--",
            },
            "multi": {
                "stages": list(STAGES_MULTI),
                "active_idx": -1,
                "is_done": False,
                "is_paused": False,
                "pause_idx": -1,
                "pause_msg": "",
                "desc": "",
                "status_text": "Status: Ready to batch process",
                "status_fg": UITheme.TEXT_SECONDARY,
                "timer_text": "Elapsed: --:--",
            },
        }

        # Initialize Tkinter variables
        self._init_variables()

        # Build UI layout
        self._build_ui()

        # Bind global mousewheel for smooth canvas scrolling
        self._bind_global_mousewheel()

        # Activate default preset
        self._select_preset("pro")

        # Intercept window close for safe exit
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Verify Metashape connectivity
        self._check_metashape()

        # Periodic queue consumer & timer
        self.root.after(80, self._consume_log_queue)
        self.root.after(1000, self._update_clock)

    def _init_dpi_and_geometry(self) -> None:
        """Dynamically center and scale the window based on display metrics."""
        dpi = 96.0
        try:
            dpi = float(self.root.winfo_fpixels("1i"))
        except Exception:
            pass

        self.scale = max(1.0, dpi / 96.0)

        try:
            self.root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        win_w = min(1360, max(1000, int(sw * 0.65)))
        win_h = min(980, max(740, int(sh * 0.86)))

        pos_x = max(0, (sw - win_w) // 2)
        pos_y = max(0, (sh - win_h) // 2 - 25)

        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.root.minsize(min(920, sw - 40), min(660, sh - 60))

    def _init_ttk_styles(self) -> None:
        """Configure native TTK Clam theme for dark slate widgets."""
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        self.style.configure(
            "TCombobox",
            fieldbackground=UITheme.BG_INPUT,
            background=UITheme.BG_SURFACE_RAISED,
            foreground=UITheme.TEXT_PRIMARY,
            darkcolor=UITheme.BORDER,
            lightcolor=UITheme.BORDER,
            bordercolor=UITheme.BORDER,
            arrowcolor=UITheme.TEXT_SECONDARY,
            selectbackground=UITheme.ACCENT_CYAN,
            selectforeground=UITheme.BG_ROOT,
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", UITheme.BG_INPUT), ("active", UITheme.BG_INPUT)],
            foreground=[("readonly", UITheme.TEXT_PRIMARY)],
            bordercolor=[("focus", UITheme.BORDER_FOCUS)],
        )

        self.style.configure(
            "Horizontal.TProgressbar",
            troughcolor=UITheme.BG_ROOT,
            background=UITheme.ACCENT_CYAN,
            bordercolor=UITheme.BORDER,
            lightcolor=UITheme.ACCENT_CYAN,
            darkcolor=UITheme.ACCENT_CYAN,
        )

        self.style.configure(
            "Vertical.TScrollbar",
            background=UITheme.BORDER,
            troughcolor=UITheme.BG_ROOT,
            bordercolor=UITheme.BG_ROOT,
            arrowcolor=UITheme.TEXT_MUTED,
        )
        self.style.map(
            "Vertical.TScrollbar",
            background=[("active", UITheme.BORDER_HOVER)],
        )

    def _init_variables(self) -> None:
        self.var_images_dir = tk.StringVar()
        self.var_output_dir = tk.StringVar()
        self.var_align_quality = tk.StringVar(value="high")
        self.var_keypoint_limit = tk.StringVar(value="40000")
        self.var_tiepoint_limit = tk.StringVar(value="25000")
        self.var_faces = tk.StringVar(value="2000000")
        self.var_tex_pages = tk.StringVar(value="16")
        self.var_crop = tk.StringVar(value="gps-square")
        self.var_depth_quality = tk.StringVar(value="high")
        self.var_depth_filtering = tk.StringVar(value="moderate")
        self.var_multi_chunk = tk.StringVar(value="disable")
        self.var_chunk_align_method = tk.StringVar(value="camera")

        self.var_guided = tk.BooleanVar(value=True)
        self.var_rolling = tk.BooleanVar(value=True)
        self.var_stationary = tk.BooleanVar(value=True)
        self.var_adaptive_fitting = tk.BooleanVar(value=True)
        self.var_filter_tiepoints = tk.BooleanVar(value=True)
        self.var_close_holes = tk.BooleanVar(value=True)
        self.var_fill_holes = tk.BooleanVar(value=True)
        self.var_ghosting = tk.BooleanVar(value=True)
        self.var_pause_crop = tk.BooleanVar(value=False)
        self.var_rebake = tk.BooleanVar(value=False)
        self.var_resume = tk.BooleanVar(value=False)
        self.var_ortho = tk.BooleanVar(value=False)
        self.var_build_point_cloud = tk.BooleanVar(value=True)
        self.var_export_point_cloud = tk.BooleanVar(value=True)
        self.var_pc_format = tk.StringVar(value="laz")
        self.var_classify_ground = tk.BooleanVar(value=False)
        self.var_classify_points = tk.BooleanVar(value=False)
        self.var_build_dem = tk.BooleanVar(value=False)
        self.var_export_dem = tk.BooleanVar(value=False)
        self.var_export_report = tk.BooleanVar(value=True)

        self.var_exp_point_cloud = tk.BooleanVar(value=True)
        self.var_exp_pc_format = tk.StringVar(value="laz")
        self.var_exp_dem = tk.BooleanVar(value=False)
        self.var_exp_ortho = tk.BooleanVar(value=False)
        self.var_exp_report = tk.BooleanVar(value=False)

        # Extended Metashape Functions: Targets, GCPs, Meshing, Tiled Model, Contours & Delighting
        self.var_detect_markers = tk.BooleanVar(value=False)
        self.var_target_type = tk.StringVar(value="circular12")
        self.var_marker_tolerance = tk.StringVar(value="50")
        self.var_import_reference = tk.StringVar(value="")
        self.var_calibrate_colors = tk.BooleanVar(value=False)
        self.var_surface_type = tk.StringVar(value="arbitrary")
        self.var_smooth_mesh = tk.BooleanVar(value=False)
        self.var_smooth_strength = tk.StringVar(value="3.0")
        self.var_decimate_mesh = tk.StringVar(value="")
        self.var_export_fbx = tk.BooleanVar(value=False)
        self.var_export_ply = tk.BooleanVar(value=False)
        self.var_export_dxf = tk.BooleanVar(value=False)
        self.var_export_tie_points = tk.BooleanVar(value=False)

        self.var_build_tiled_model = tk.BooleanVar(value=False)
        self.var_tiled_model_format = tk.StringVar(value="cesium")
        self.var_tile_size = tk.StringVar(value="256")

        self.var_build_contours = tk.BooleanVar(value=False)
        self.var_contour_interval = tk.StringVar(value="1.0")
        self.var_contours_format = tk.StringVar(value="shp")

        self.var_remove_lighting = tk.BooleanVar(value=False)

        # Export Tab Extended Controls
        self.var_exp_tiled_model = tk.BooleanVar(value=False)
        self.var_exp_tiled_format = tk.StringVar(value="cesium")
        self.var_exp_contours = tk.BooleanVar(value=False)
        self.var_exp_contour_interval = tk.StringVar(value="1.0")
        self.var_exp_contours_format = tk.StringVar(value="shp")
        self.var_exp_remove_lighting = tk.BooleanVar(value=False)
        self.var_exp_calibrate_colors = tk.BooleanVar(value=False)
        self.var_exp_smooth_mesh = tk.BooleanVar(value=False)
        self.var_exp_export_fbx = tk.BooleanVar(value=False)
        self.var_exp_export_ply = tk.BooleanVar(value=False)
        self.var_exp_tie_points = tk.BooleanVar(value=False)

        self.var_psx_path = tk.StringVar()
        self.var_export_out = tk.StringVar()
        self.var_exp_format = tk.StringVar(value="tiff")
        self.var_exp_pages = tk.StringVar(value="16")
        self.var_exp_rebake = tk.BooleanVar(value=True)

        self.lbl_photo_status = tk.StringVar(
            value="No folder selected. Click 'Browse...' to choose your raw photos folder."
        )

        # Multi-Project Studio Batch Variables
        self.var_multi_root = tk.StringVar(value=r"C:\Users\ADMIN\Documents\Reality Scan\Metashape\00. CATP")
        self.var_multi_preset = tk.StringVar(value="pro (Professional Default)")
        self.var_multi_faces = tk.StringVar(value="2000000")
        self.var_multi_tex_pages = tk.StringVar(value="8")
        self.var_multi_crop = tk.StringVar(value="gps-square")
        self.multi_discovered_projects: list[dict] = []
        self.multi_project_vars: dict[str, tk.BooleanVar] = {}
        self.batch_running: bool = False
        self.batch_queue: list[dict] = []
        self.batch_current_idx: int = -1
        self.lbl_multi_scan_status = tk.StringVar(value="0 projects detected")

        # Synchronize dynamic pipeline milestones whenever options toggle
        for _v in [
            self.var_build_point_cloud,
            self.var_export_point_cloud,
            self.var_build_dem,
            self.var_export_dem,
            self.var_ortho,
            self.var_build_tiled_model,
            self.var_build_contours,
            self.var_export_report,
            self.var_rebake,
            self.var_exp_rebake,
            self.var_exp_remove_lighting,
            self.var_exp_point_cloud,
            self.var_exp_dem,
            self.var_exp_ortho,
            self.var_exp_tiled_model,
            self.var_exp_contours,
            self.var_exp_export_fbx,
            self.var_exp_export_ply,
            self.var_exp_report,
        ]:
            _v.trace_add("write", lambda *_: self._sync_dynamic_stepper())

    # -----------------------------------------------------------------------
    # Main UI Layout Construction
    # -----------------------------------------------------------------------
    def _build_ui(self) -> None:
        # 1. Top Header Bar (Full width top)
        self._build_header()

        # 2. Mode Selector Bar (Full width top)
        self._build_mode_bar()

        # 3. Manual Crop Pause Banner (Full width top, hidden initially)
        self._build_crop_pause_banner()

        # 4. Bottom Action Bar (Packed FIRST with side=tk.BOTTOM so it spans 100% full width)
        self._build_bottom_bar()

        # 5. Center Scrollable Workspace (Packed in the remaining center space)
        self._build_scrollable_workspace()

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=UITheme.BG_SURFACE, height=44)
        header.pack(side=tk.TOP, fill=tk.X, padx=0, pady=(0, 2))

        left = tk.Frame(header, bg=UITheme.BG_SURFACE)
        left.pack(side=tk.LEFT, padx=16, pady=8)

        lbl_logo = tk.Label(
            left,
            text="AutoMetashape Studio",
            font=("Segoe UI", 11, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_PRIMARY,
        )
        lbl_logo.pack(side=tk.LEFT)

        lbl_sub = tk.Label(
            left,
            text=" ·  Autonomous 3D Photogrammetry",
            font=("Segoe UI", 9),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_MUTED,
        )
        lbl_sub.pack(side=tk.LEFT, padx=(4, 0))

        right = tk.Frame(header, bg=UITheme.BG_SURFACE)
        right.pack(side=tk.RIGHT, padx=16, pady=8)

        self.lbl_metashape_badge = tk.Label(
            right,
            text="Checking Metashape Pro...",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_MUTED,
            padx=10,
            pady=3,
        )
        self.lbl_metashape_badge.pack(side=tk.RIGHT)

    def _build_mode_bar(self) -> None:
        bar = tk.Frame(self.root, bg=UITheme.BG_ROOT, pady=6)
        bar.pack(side=tk.TOP, fill=tk.X, padx=14)

        self.btn_tab_recon = tk.Button(
            bar,
            text="3D Reconstruction",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_PRIMARY,
            activebackground=UITheme.BG_ACTIVE,
            activeforeground=UITheme.TEXT_PRIMARY,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=16,
            pady=5,
            cursor="hand2",
            command=lambda: self._set_mode("recon"),
        )
        self.btn_tab_recon.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_tab_export = tk.Button(
            bar,
            text="Texture Re-Bake & Export",
            font=("Segoe UI", 9),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_SECONDARY,
            activebackground=UITheme.BG_SURFACE_RAISED,
            activeforeground=UITheme.TEXT_PRIMARY,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=16,
            pady=5,
            cursor="hand2",
            command=lambda: self._set_mode("export"),
        )
        self.btn_tab_export.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_tab_multi = tk.Button(
            bar,
            text="Multi-Project Studio",
            font=("Segoe UI", 9),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_SECONDARY,
            activebackground=UITheme.BG_SURFACE_RAISED,
            activeforeground=UITheme.TEXT_PRIMARY,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=16,
            pady=5,
            cursor="hand2",
            command=lambda: self._set_mode("multi"),
        )
        self.btn_tab_multi.pack(side=tk.LEFT)

        btn_web_panel = tk.Button(
            bar,
            text="Open Web Studio Panel ↗",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.ACCENT_CYAN,
            activebackground=UITheme.BG_ACTIVE,
            activeforeground="white",
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=12,
            pady=4,
            cursor="hand2",
            command=self._open_web_studio,
        )
        btn_web_panel.pack(side=tk.RIGHT)

    def _build_crop_pause_banner(self) -> None:
        """Quiet, high-contrast alert banner displayed when manual cropping is active."""
        self.frame_crop_banner = tk.Frame(
            self.root,
            bg=UITheme.ACCENT_AMBER_BG,
            bd=1,
            relief=tk.FLAT,
            padx=14,
            pady=8,
        )

        inner = tk.Frame(self.frame_crop_banner, bg=UITheme.ACCENT_AMBER_BG)
        inner.pack(fill=tk.X)

        text_block = tk.Frame(inner, bg=UITheme.ACCENT_AMBER_BG)
        text_block.pack(side=tk.LEFT, fill=tk.X, expand=True)

        lbl_heading = tk.Label(
            text_block,
            text="PIPELINE PAUSED: MANUAL BOUNDING BOX IN AGISOFT METASHAPE PRO",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.ACCENT_AMBER_BG,
            fg=UITheme.ACCENT_AMBER_TXT,
            anchor=tk.W,
        )
        lbl_heading.pack(fill=tk.X)

        lbl_instruct = tk.Label(
            text_block,
            text="1. Open project in Metashape Pro  ➜  2. Adjust & save bounding box (Ctrl+S)  ➜  3. Close Metashape  ➜  4. Click Continue below",
            font=("Segoe UI", 8),
            bg=UITheme.ACCENT_AMBER_BG,
            fg=UITheme.TEXT_SECONDARY,
            anchor=tk.W,
        )
        lbl_instruct.pack(fill=tk.X)

        btn_launch = tk.Button(
            inner,
            text="Open Metashape",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.ACCENT_AMBER,
            fg=UITheme.BG_ROOT,
            activebackground="#b45309",
            activeforeground="white",
            bd=0,
            padx=12,
            pady=4,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._on_open_metashape_gui,
        )
        btn_launch.pack(side=tk.RIGHT, padx=4)

    def _build_bottom_bar(self) -> None:
        bar = tk.Frame(self.root, bg=UITheme.BG_SURFACE, height=58)
        bar.pack(side=tk.BOTTOM, fill=tk.X, padx=0, pady=0)

        inner = tk.Frame(bar, bg=UITheme.BG_SURFACE)
        inner.pack(fill=tk.X, padx=16, pady=10)

        self.btn_start = tk.Button(
            inner,
            text="Start Reconstruction",
            font=("Segoe UI", 10, "bold"),
            bg=UITheme.ACCENT_GREEN,
            fg="white",
            activebackground="#2ea043",
            activeforeground="white",
            bd=0,
            padx=20,
            pady=7,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._on_start_clicked,
        )
        self.btn_start.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_stop = tk.Button(
            inner,
            text="Cancel",
            font=("Segoe UI", 9),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_MUTED,
            activebackground=UITheme.ACCENT_RED,
            activeforeground="white",
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=14,
            pady=6,
            state=tk.DISABLED,
            command=self._on_stop_clicked,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 16))

        self.btn_continue_crop = tk.Button(
            inner,
            text="Continue Reconstruction",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.ACCENT_CYAN,
            fg=UITheme.BG_ROOT,
            activebackground=UITheme.ACCENT_CYAN_HOVER,
            activeforeground="white",
            bd=0,
            padx=16,
            pady=7,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._on_continue_crop_clicked,
        )

        self.progress_bar = ttk.Progressbar(
            inner,
            style="Horizontal.TProgressbar",
            mode="indeterminate",
            length=140,
        )
        self.progress_bar.pack(side=tk.LEFT, padx=(0, 16))

        self.lbl_status = tk.Label(
            inner,
            text="● Ready",
            font=("Segoe UI", 9),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_SECONDARY,
        )
        self.lbl_status.pack(side=tk.RIGHT)

    def _build_scrollable_workspace(self) -> None:
        """Construct full-width scrollable container with snap-scale two-column / single-column responsive layout."""
        self.center_frame = tk.Frame(self.root, bg=UITheme.BG_ROOT)
        self.center_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.scrollbar = ttk.Scrollbar(self.center_frame, orient=tk.VERTICAL)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.canvas = tk.Canvas(
            self.center_frame,
            bg=UITheme.BG_ROOT,
            bd=0,
            highlightthickness=0,
            yscrollcommand=self.scrollbar.set,
        )
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.config(command=self.canvas.yview)

        # Content container inside canvas
        self.content_frame = tk.Frame(self.canvas, bg=UITheme.BG_ROOT)
        self.canvas_window_id = self.canvas.create_window(
            (0, 0), window=self.content_frame, anchor="nw"
        )

        # Full-width Stepper Card spanning entire content width
        self._build_stepper_card(self.content_frame)

        # Responsive columns inside content_frame
        self.col_left = tk.Frame(self.content_frame, bg=UITheme.BG_ROOT)
        self.col_right = tk.Frame(self.content_frame, bg=UITheme.BG_ROOT)

        # Populate Left Column (Configuration & Parameters)
        self._build_folder_card(self.col_left)
        self._build_presets_card(self.col_left)
        self._build_advanced_settings_card(self.col_left)
        self._build_export_card(self.col_left)
        self._build_multi_project_card(self.col_left)

        # Populate Right Column (Execution Log with matching height)
        self._build_log_card(self.col_right)

        # Responsive snap layout callback on window resize
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.content_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )

    def _on_canvas_configure(self, event: tk.Event) -> None:
        """Snap-scale UI layout based on available canvas width."""
        canvas_width = event.width
        self.canvas.itemconfig(self.canvas_window_id, width=canvas_width)

        # Snap breakpoint: >= 960px -> 2 columns side-by-side; < 960px -> 1 column vertical stack
        # Stepper is pinned to row 0 across the full width, with zero clipping on any display size
        if canvas_width >= 960:
            if self.current_layout_mode != "2col":
                self.current_layout_mode = "2col"
                self.card_stepper.grid_forget()
                self.col_left.grid_forget()
                self.col_right.grid_forget()
                self.content_frame.columnconfigure(0, weight=1, uniform="col")
                self.content_frame.columnconfigure(1, weight=1, uniform="col")
                self.card_stepper.grid(row=0, column=0, columnspan=2, sticky="ew", padx=14, pady=(6, 4))
                self.col_left.grid(row=1, column=0, sticky="nsew", padx=(14, 7), pady=(2, 6))
                self.col_right.grid(row=1, column=1, sticky="nsew", padx=(7, 14), pady=(2, 6))
        else:
            if self.current_layout_mode != "1col":
                self.current_layout_mode = "1col"
                self.card_stepper.grid_forget()
                self.col_left.grid_forget()
                self.col_right.grid_forget()
                self.content_frame.columnconfigure(0, weight=1, uniform="")
                self.content_frame.columnconfigure(1, weight=0, uniform="")
                self.card_stepper.grid(row=0, column=0, columnspan=1, sticky="ew", padx=14, pady=(6, 4))
                self.col_left.grid(row=1, column=0, sticky="ew", padx=14, pady=(2, 0))
                self.col_right.grid(row=2, column=0, sticky="ew", padx=14, pady=(6, 6))

        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _bind_global_mousewheel(self) -> None:
        """Enable mouse-wheel & touchpad scrolling across all widgets."""
        def _on_mousewheel(event: tk.Event) -> None:
            # Let the log text box handle its own scroll if mouse is over it
            try:
                w = event.widget
                if str(w).startswith(str(self.txt_log)):
                    return
            except Exception:
                pass

            delta = event.delta
            if delta != 0:
                if abs(delta) >= 120:
                    units = int(-1 * (delta / 120))
                else:
                    units = -1 if delta > 0 else 1
                self.canvas.yview_scroll(units, "units")

        self.root.bind_all("<MouseWheel>", _on_mousewheel)
        self.root.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.root.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    # -----------------------------------------------------------------------
    # Step 1: Input Photos & Output Folder Card
    # -----------------------------------------------------------------------
    def _build_folder_card(self, parent: tk.Widget) -> None:
        self.card_folder = StudioCard(
            parent,
            title="1. Photos & Output Destination",
            subtitle="Survey photos & model export path",
        )
        self.card_folder.pack(fill=tk.X, pady=(0, 6))

        # Raw Images Row
        row_img = tk.Frame(self.card_folder.body, bg=UITheme.BG_SURFACE)
        row_img.pack(fill=tk.X, pady=(0, 2))

        lbl_img_title = tk.Label(
            row_img,
            text="Photos Directory",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_PRIMARY,
        )
        lbl_img_title.pack(side=tk.LEFT)

        btn_browse_img = tk.Button(
            row_img,
            text="Browse...",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_PRIMARY,
            activebackground=UITheme.BORDER_HOVER,
            activeforeground="white",
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=12,
            pady=2,
            cursor="hand2",
            command=self._browse_images_dir,
        )
        btn_browse_img.pack(side=tk.RIGHT)

        self.var_images_dir.trace_add("write", self._on_images_dir_changed)
        ent_img = tk.Entry(
            self.card_folder.body,
            textvariable=self.var_images_dir,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_PRIMARY,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER_CONTROL,
        )
        ent_img.pack(fill=tk.X, pady=(3, 4))

        # Photo count & size summary line
        stats_frame = tk.Frame(self.card_folder.body, bg=UITheme.BG_ROOT, padx=8, pady=4)
        stats_frame.pack(fill=tk.X, pady=(0, 8))

        lbl_stats = tk.Label(
            stats_frame,
            textvariable=self.lbl_photo_status,
            font=("Segoe UI", 8),
            bg=UITheme.BG_ROOT,
            fg=UITheme.TEXT_SECONDARY,
            anchor=tk.W,
        )
        lbl_stats.pack(fill=tk.X)

        # Output Folder Row
        row_out = tk.Frame(self.card_folder.body, bg=UITheme.BG_SURFACE)
        row_out.pack(fill=tk.X, pady=(0, 2))

        lbl_out_title = tk.Label(
            row_out,
            text="Deliverables Destination (GLB, OBJ, TIFF, PSX)",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_PRIMARY,
        )
        lbl_out_title.pack(side=tk.LEFT)

        btn_browse_out = tk.Button(
            row_out,
            text="Change...",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_PRIMARY,
            activebackground=UITheme.BORDER_HOVER,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=10,
            pady=2,
            cursor="hand2",
            command=self._browse_output_dir,
        )
        btn_browse_out.pack(side=tk.RIGHT)

        ent_out = tk.Entry(
            self.card_folder.body,
            textvariable=self.var_output_dir,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_PRIMARY,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER_CONTROL,
        )
        ent_out.pack(fill=tk.X, pady=(3, 2))

        bar_diag = tk.Frame(self.card_folder.body, bg=UITheme.BG_SURFACE)
        bar_diag.pack(fill=tk.X, pady=(6, 2))

        btn_diag = tk.Button(
            bar_diag,
            text="🔬 Inspect Survey Physics & Optics Telemetry",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.ACCENT_CYAN,
            activebackground=UITheme.BORDER_HOVER,
            activeforeground="white",
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=10,
            pady=3,
            cursor="hand2",
            command=self._diagnose_survey_physics,
        )
        btn_diag.pack(side=tk.LEFT)

    # -----------------------------------------------------------------------
    # Step 2: Quality Presets Card
    # -----------------------------------------------------------------------
    def _build_presets_card(self, parent: tk.Widget) -> None:
        self.card_preset = StudioCard(
            parent,
            title="2. Workflow Preset",
            subtitle="One-click parameters for reconstruction fidelity",
        )
        self.card_preset.pack(fill=tk.X, pady=(0, 6))

        container = tk.Frame(self.card_preset.body, bg=UITheme.BG_SURFACE)
        container.pack(fill=tk.X)

        def _make_preset(
            key: str,
            title: str,
            specs: str,
            desc: str,
        ):
            outer = tk.Frame(container, bg=UITheme.BORDER, bd=1, relief=tk.FLAT, cursor="hand2")
            outer.pack(fill=tk.X, pady=3)

            inner = tk.Frame(outer, bg=UITheme.BG_ROOT, padx=10, pady=8)
            inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

            top = tk.Frame(inner, bg=UITheme.BG_ROOT)
            top.pack(fill=tk.X)

            t = tk.Label(
                top,
                text=title,
                font=("Segoe UI", 9, "bold"),
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_PRIMARY,
            )
            t.pack(side=tk.LEFT)

            radio_badge = tk.Label(
                top,
                text="○ Select",
                font=("Segoe UI", 8),
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_MUTED,
            )
            radio_badge.pack(side=tk.RIGHT)

            spec_lbl = tk.Label(
                inner,
                text=specs,
                font=("Segoe UI", 8),
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_SECONDARY,
                anchor=tk.W,
            )
            spec_lbl.pack(fill=tk.X, pady=(2, 1))

            d = tk.Label(
                inner,
                text=desc,
                font=("Segoe UI", 8),
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_MUTED,
                justify=tk.LEFT,
                anchor=tk.W,
            )
            d.pack(fill=tk.X)

            self.preset_cards[key] = {
                "outer": outer,
                "inner": inner,
                "title": t,
                "badge": radio_badge,
                "spec": spec_lbl,
                "desc": d,
                "top": top,
            }

            for w in [outer, inner, t, radio_badge, spec_lbl, d, top]:
                w.bind("<Button-1>", lambda e, k=key: self._select_preset(k))
                w.bind("<Enter>", lambda e, k=key: self._on_preset_hover(k, True))
                w.bind("<Leave>", lambda e, k=key: self._on_preset_hover(k, False))

        _make_preset(
            "pro",
            "Pro Photogrammetry & Geomatics Engine (Default)",
            "Physics GSD · USGS 4-pass · 1:1 Texel budget · Shutter audit",
            "Autonomous geomatics workflow: evaluates physical sensor pitch, exact GSD, shutter mechanics (leaf vs rolling), USGS 4-pass gradual selection bundle adjustment, slope-adaptive ground filtering, and 1:1 texel density budgeting.",
        )
        _make_preset(
            "building",
            "Building Reconstruction",
            "High Quality · 2M faces · 16-page 8K · GPS crop",
            "Balanced architectural geometry with GPS-square bounding box, 16-page 8K texture atlas, and CMOS rolling shutter compensation.",
        )
        _make_preset(
            "map",
            "Aerial Map & Orthomosaic",
            "High Quality · 3M faces · 4-page 8K · GPS crop · Ortho TIFF",
            "Terrain mapping with GPS-square bounding box: 2M face mesh, 4-page 8K texture atlas, DEM elevation, and geo-referenced Orthomosaic GeoTIFF.",
        )
        _make_preset(
            "custom",
            "Custom Configuration",
            "Manual parameters · Full override · Custom face budget",
            "Manual photogrammetry parameters: customize polygon density, texture atlas pages, keypoint budgets, depth filtering, hole closing, and geomatics flags.",
        )

    def _on_preset_hover(self, key: str, is_hover: bool) -> None:
        if key == self.selected_preset_key:
            return
        card = self.preset_cards.get(key)
        if card:
            card["outer"].config(bg=UITheme.BORDER_HOVER if is_hover else UITheme.BORDER)

    # -----------------------------------------------------------------------
    # Step 3: Collapsible Advanced Pipeline Settings
    # -----------------------------------------------------------------------
    def _build_advanced_settings_card(self, parent: tk.Widget) -> None:
        self.card_adv = StudioCard(
            parent,
            title="3. Custom Pipeline Parameters",
            subtitle="Manual calibration, face density, and processing flags (Custom Preset)",
        )
        # Advanced config card is only visible when the user selects the Custom preset
        if getattr(self, "selected_preset_key", "pro") == "custom":
            self.card_adv.pack(fill=tk.X, pady=(0, 6))

        bar_toggle = tk.Frame(self.card_adv.body, bg=UITheme.BG_SURFACE)
        bar_toggle.pack(fill=tk.X)

        self.btn_toggle_adv = tk.Button(
            bar_toggle,
            text="▶ Show advanced parameters (mesh density, camera optimization, texture atlas)",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.ACCENT_CYAN,
            bd=0,
            padx=2,
            pady=3,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._toggle_advanced,
        )
        self.btn_toggle_adv.pack(side=tk.LEFT)

        self.body_adv = tk.Frame(self.card_adv.body, bg=UITheme.BG_SURFACE)

        def _make_sec_header(parent_widget: tk.Widget, badge: str, title: str, top_pad: int = 8) -> None:
            hdr = tk.Frame(parent_widget, bg=UITheme.BG_SURFACE)
            hdr.pack(fill=tk.X, pady=(top_pad, 4))

            badge_lbl = tk.Label(
                hdr,
                text=badge,
                font=("Segoe UI", 7, "bold"),
                bg=UITheme.BG_ROOT,
                fg=UITheme.ACCENT_CYAN,
                padx=6,
                pady=1,
                bd=1,
                relief=tk.SOLID,
            )
            badge_lbl.pack(side=tk.LEFT, padx=(0, 6))

            title_lbl = tk.Label(
                hdr,
                text=title,
                font=("Segoe UI", 8, "bold"),
                bg=UITheme.BG_SURFACE,
                fg=UITheme.TEXT_PRIMARY,
            )
            title_lbl.pack(side=tk.LEFT)

            line = tk.Frame(hdr, bg=UITheme.RULE, height=1)
            line.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0), pady=4)

        def _c(parent_widget: tk.Widget, text: str, var: tk.Variable) -> tk.Checkbutton:
            return tk.Checkbutton(
                parent_widget,
                text=text,
                variable=var,
                font=("Segoe UI", 8),
                bg=UITheme.BG_SURFACE,
                fg=UITheme.TEXT_SECONDARY,
                selectcolor=UITheme.BG_ROOT,
                activebackground=UITheme.BG_SURFACE,
                activeforeground=UITheme.TEXT_PRIMARY,
                bd=0,
            )

        # -------------------------------------------------------------------
        # Process 1: Align & Feature Matching
        # -------------------------------------------------------------------
        _make_sec_header(self.body_adv, "ALIGN", "1. Feature Matching & Camera Alignment", top_pad=6)

        grid_align = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        grid_align.pack(fill=tk.X, pady=(2, 4))

        tk.Label(grid_align, text="Align Quality:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=0, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(grid_align, textvariable=self.var_align_quality, values=["high", "ultra", "highest", "medium", "low"], state="readonly", width=10).grid(row=0, column=1, padx=(4, 14), pady=3)

        tk.Label(grid_align, text="Keypoint Limit:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=0, column=2, sticky=tk.W, pady=3)
        ttk.Combobox(grid_align, textvariable=self.var_keypoint_limit, values=["40000", "20000", "60000", "80000", "100000"], width=10).grid(row=0, column=3, padx=(4, 14), pady=3)

        tk.Label(grid_align, text="Tiepoint Limit:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=0, column=4, sticky=tk.W, pady=3)
        ttk.Combobox(grid_align, textvariable=self.var_tiepoint_limit, values=["25000", "10000", "40000", "50000", "0"], width=8).grid(row=0, column=5, padx=4, pady=3)

        tk.Label(grid_align, text="Multi-Chunk:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=1, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(grid_align, textvariable=self.var_multi_chunk, values=["disable", "auto", "force"], state="readonly", width=10).grid(row=1, column=1, padx=(4, 14), pady=3)

        tk.Label(grid_align, text="Chunk Align:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=1, column=2, sticky=tk.W, pady=3)
        ttk.Combobox(grid_align, textvariable=self.var_chunk_align_method, values=["camera", "point"], state="readonly", width=10).grid(row=1, column=3, padx=(4, 14), pady=3)

        chk_align_1 = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_align_1.pack(fill=tk.X, pady=(2, 2))
        _c(chk_align_1, "Guided Image Matching", self.var_guided).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_align_1, "Exclude Stationary Points", self.var_stationary).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_align_1, "Adaptive Camera Fitting", self.var_adaptive_fitting).pack(side=tk.LEFT)

        chk_align_2 = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_align_2.pack(fill=tk.X, pady=(2, 2))
        _c(chk_align_2, "CMOS Rolling Shutter", self.var_rolling).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_align_2, "Tie-Point Filtering", self.var_filter_tiepoints).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_align_2, "Radiometric Color Calibration", self.var_calibrate_colors).pack(side=tk.LEFT)

        chk_align_targets = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_align_targets.pack(fill=tk.X, pady=(2, 4))
        _c(chk_align_targets, "Detect Coded Targets", self.var_detect_markers).pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(chk_align_targets, text="Target:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(chk_align_targets, textvariable=self.var_target_type, values=["circular12", "circular14", "circular16", "circular20", "cross", "apriltag16h5", "apriltag25h9", "apriltag36h11"], state="readonly", width=12).pack(side=tk.LEFT, padx=(4, 10))

        row_gcp = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        row_gcp.pack(fill=tk.X, pady=(2, 4))
        tk.Label(row_gcp, text="GCP Reference (CSV):", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ent_ref = tk.Entry(row_gcp, textvariable=self.var_import_reference, font=("Segoe UI", 8), bg=UITheme.BG_INPUT, fg=UITheme.TEXT_PRIMARY, insertbackground="white", bd=1, relief=tk.SOLID)
        ent_ref.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)
        tk.Button(row_gcp, text="Browse...", font=("Segoe UI", 7), bg=UITheme.BG_SURFACE_RAISED, fg=UITheme.TEXT_PRIMARY, command=self._browse_reference_file).pack(side=tk.RIGHT)

        # -------------------------------------------------------------------
        # Process 2: Bounding Box & Region
        # -------------------------------------------------------------------
        _make_sec_header(self.body_adv, "REGION", "2. Bounding Box & Region", top_pad=8)

        grid_region = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        grid_region.pack(fill=tk.X, pady=(2, 2))

        tk.Label(grid_region, text="Bounding Box:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=0, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(grid_region, textvariable=self.var_crop, values=["gps-square", "none", "structure", "manual"], state="readonly", width=10).grid(row=0, column=1, padx=(4, 14), pady=3)

        chk_region = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_region.pack(fill=tk.X, pady=(2, 4))
        _c(chk_region, "Pause after camera alignment for manual crop", self.var_pause_crop).pack(side=tk.LEFT)

        # -------------------------------------------------------------------
        # Process 3: Build Model (Depth Maps & Mesh)
        # -------------------------------------------------------------------
        _make_sec_header(self.body_adv, "MODEL", "3. Build Model (Depth Maps & Mesh)", top_pad=8)

        grid_model = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        grid_model.pack(fill=tk.X, pady=(2, 2))

        tk.Label(grid_model, text="Depth Quality:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=0, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(grid_model, textvariable=self.var_depth_quality, values=["high", "ultra", "medium", "low"], state="readonly", width=10).grid(row=0, column=1, padx=(4, 14), pady=3)

        tk.Label(grid_model, text="Depth Filter:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=0, column=2, sticky=tk.W, pady=3)
        ttk.Combobox(grid_model, textvariable=self.var_depth_filtering, values=["moderate", "mild", "aggressive"], state="readonly", width=10).grid(row=0, column=3, padx=(4, 14), pady=3)

        tk.Label(grid_model, text="Target Faces:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=0, column=4, sticky=tk.W, pady=3)
        ttk.Combobox(grid_model, textvariable=self.var_faces, values=["1000000", "2000000", "3000000", "4000000", "5000000"], width=8).grid(row=0, column=5, padx=4, pady=3)

        grid_model2 = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        grid_model2.pack(fill=tk.X, pady=(2, 2))
        tk.Label(grid_model2, text="Surface Type:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=0, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(grid_model2, textvariable=self.var_surface_type, values=["arbitrary", "heightfield"], state="readonly", width=10).grid(row=0, column=1, padx=(4, 14), pady=3)

        chk_model = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_model.pack(fill=tk.X, pady=(2, 2))
        _c(chk_model, "Close Mesh Holes", self.var_close_holes).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_model, "Laplacian Mesh Smoothing", self.var_smooth_mesh).pack(side=tk.LEFT, padx=(0, 10))

        chk_model_exp = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_model_exp.pack(fill=tk.X, pady=(2, 4))
        _c(chk_model_exp, "Export FBX", self.var_export_fbx).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_model_exp, "Export PLY", self.var_export_ply).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_model_exp, "Export DXF", self.var_export_dxf).pack(side=tk.LEFT)

        # -------------------------------------------------------------------
        # Process 4: Dense Point Cloud & Classification
        # -------------------------------------------------------------------
        _make_sec_header(self.body_adv, "POINT CLOUD", "4. Dense Point Cloud & Classification", top_pad=8)

        chk_pc1 = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_pc1.pack(fill=tk.X, pady=(2, 2))
        _c(chk_pc1, "Build Dense Point Cloud", self.var_build_point_cloud).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_pc1, "Export Point Cloud", self.var_export_point_cloud).pack(side=tk.LEFT, padx=(0, 10))

        tk.Label(chk_pc1, text="Format:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT, padx=(4, 2))
        ttk.Combobox(chk_pc1, textvariable=self.var_pc_format, values=["laz", "las", "ply", "e57", "xyz"], state="readonly", width=6).pack(side=tk.LEFT)

        chk_pc2 = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_pc2.pack(fill=tk.X, pady=(2, 4))
        _c(chk_pc2, "Classify Ground Points", self.var_classify_ground).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_pc2, "Classify Points (Multiclass AI)", self.var_classify_points).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_pc2, "Export Sparse Tie Points", self.var_export_tie_points).pack(side=tk.LEFT)

        # -------------------------------------------------------------------
        # Process 5: Hierarchical 3D Tiled Model (LOD Web GIS)
        # -------------------------------------------------------------------
        _make_sec_header(self.body_adv, "TILED MODEL", "5. Hierarchical 3D Tiled Model (LOD / Web GIS)", top_pad=8)

        chk_tiled = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_tiled.pack(fill=tk.X, pady=(2, 4))
        _c(chk_tiled, "Build 3D Tiled Model (LOD)", self.var_build_tiled_model).pack(side=tk.LEFT, padx=(0, 10))
        tk.Label(chk_tiled, text="Format:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(chk_tiled, textvariable=self.var_tiled_model_format, values=["cesium", "tls", "slpk", "osgb"], state="readonly", width=8).pack(side=tk.LEFT, padx=(4, 10))
        tk.Label(chk_tiled, text="Tile Size:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(chk_tiled, textvariable=self.var_tile_size, values=["256", "512"], state="readonly", width=6).pack(side=tk.LEFT, padx=4)

        # -------------------------------------------------------------------
        # Process 6: Elevation (DEM) & Topographic Contours
        # -------------------------------------------------------------------
        _make_sec_header(self.body_adv, "ELEVATION & CONTOURS", "6. Elevation Model (DEM) & Topographic Contours", top_pad=8)

        chk_dem = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_dem.pack(fill=tk.X, pady=(2, 2))
        _c(chk_dem, "Generate Orthomosaic GeoTIFF", self.var_ortho).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_dem, "Generate Digital Elevation Model (DEM)", self.var_build_dem).pack(side=tk.LEFT)

        chk_cnt = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_cnt.pack(fill=tk.X, pady=(2, 4))
        _c(chk_cnt, "Generate Topographic Contours", self.var_build_contours).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(chk_cnt, text="Interval (m):", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(chk_cnt, textvariable=self.var_contour_interval, values=["0.5", "1.0", "2.0", "5.0", "10.0"], width=6).pack(side=tk.LEFT, padx=(4, 10))
        tk.Label(chk_cnt, text="Format:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(chk_cnt, textvariable=self.var_contours_format, values=["shp", "dxf", "geojson", "kml"], state="readonly", width=8).pack(side=tk.LEFT, padx=4)

        # -------------------------------------------------------------------
        # Process 7: Texture Atlas & Delighting
        # -------------------------------------------------------------------
        _make_sec_header(self.body_adv, "TEXTURE", "7. Texture Atlas & Delighting", top_pad=8)

        grid_tex = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        grid_tex.pack(fill=tk.X, pady=(2, 2))

        tk.Label(grid_tex, text="Texture Pages:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=0, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(grid_tex, textvariable=self.var_tex_pages, values=["16", "8", "4", "2", "32"], state="readonly", width=10).grid(row=0, column=1, padx=(4, 14), pady=3)

        chk_tex = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_tex.pack(fill=tk.X, pady=(2, 4))
        _c(chk_tex, "Texture Hole Filling", self.var_fill_holes).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_tex, "Ghosting Filter", self.var_ghosting).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_tex, "Delighting (Remove Shadows)", self.var_remove_lighting).pack(side=tk.LEFT)

        # -------------------------------------------------------------------
        # Process 8: Quality Report & Lifecycle
        # -------------------------------------------------------------------
        _make_sec_header(self.body_adv, "REPORT", "8. Quality Report & Lifecycle", top_pad=8)

        chk_lifecycle = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_lifecycle.pack(fill=tk.X, pady=(2, 6))
        _c(chk_lifecycle, "Export Calibration & Quality PDF Report", self.var_export_report).pack(side=tk.LEFT, padx=(0, 10))
        _c(chk_lifecycle, "Resume from checkpoint (skip completed steps)", self.var_resume).pack(side=tk.LEFT)

    def _toggle_advanced(self) -> None:
        self.adv_expanded = not self.adv_expanded
        if self.adv_expanded:
            self.body_adv.pack(fill=tk.X, pady=(4, 2))
            self.btn_toggle_adv.config(text="▼ Hide advanced parameters")
        else:
            self.body_adv.pack_forget()
            self.btn_toggle_adv.config(text="▶ Show advanced parameters (mesh density, camera optimization, texture atlas)")
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    # -----------------------------------------------------------------------
    # Standalone Export Card
    # -----------------------------------------------------------------------
    def _build_export_card(self, parent: tk.Widget) -> None:
        self.card_export = StudioCard(
            parent,
            title="Standalone Texture Re-Bake & Export",
            subtitle="Process solved Metashape project without recomputing depth maps",
        )
        # Initially hidden in recon mode
        if self.active_mode != "export":
            self.card_export.pack_forget()

        row0 = tk.Frame(self.card_export.body, bg=UITheme.BG_SURFACE)
        row0.pack(fill=tk.X, pady=(0, 2))

        lbl_psx = tk.Label(
            row0,
            text="Metashape Project File (.psx)",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_PRIMARY,
        )
        lbl_psx.pack(side=tk.LEFT)

        btn_browse_psx = tk.Button(
            row0,
            text="Browse...",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_PRIMARY,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=12,
            pady=2,
            command=self._browse_psx_file,
        )
        btn_browse_psx.pack(side=tk.RIGHT)

        self.var_psx_path.trace_add("write", self._on_psx_path_changed)
        ent_psx = tk.Entry(
            self.card_export.body,
            textvariable=self.var_psx_path,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_PRIMARY,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER_CONTROL,
        )
        ent_psx.pack(fill=tk.X, pady=(2, 6))

        row1 = tk.Frame(self.card_export.body, bg=UITheme.BG_SURFACE)
        row1.pack(fill=tk.X, pady=(4, 2))

        lbl_dest = tk.Label(
            row1,
            text="Export Destination Folder",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_PRIMARY,
        )
        lbl_dest.pack(side=tk.LEFT)

        btn_browse_exp = tk.Button(
            row1,
            text="Change...",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_PRIMARY,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=10,
            pady=2,
            command=self._browse_export_dir,
        )
        btn_browse_exp.pack(side=tk.RIGHT)

        ent_exp = tk.Entry(
            self.card_export.body,
            textvariable=self.var_export_out,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_PRIMARY,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER_CONTROL,
        )
        ent_exp.pack(fill=tk.X, pady=(2, 6))

        row2 = tk.Frame(self.card_export.body, bg=UITheme.BG_SURFACE)
        row2.pack(fill=tk.X, pady=4)

        tk.Checkbutton(
            row2,
            text="Re-Bake Texture Atlas",
            variable=self.var_exp_rebake,
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_PRIMARY,
            selectcolor=UITheme.BG_ROOT,
            bd=0,
        ).pack(side=tk.LEFT, padx=(0, 14))

        tk.Label(row2, text="Pages:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(row2, textvariable=self.var_exp_pages, values=["16", "8", "4", "2", "32"], state="readonly", width=6).pack(side=tk.LEFT, padx=(4, 14))

        tk.Label(row2, text="Format:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(row2, textvariable=self.var_exp_format, values=["tiff", "jpeg"], state="readonly", width=8).pack(side=tk.LEFT, padx=4)

        # Extended Export Controls: Point Cloud, DEM, Ortho, Tiled, Contours, FBX/PLY, Report
        row3 = tk.Frame(self.card_export.body, bg=UITheme.BG_SURFACE)
        row3.pack(fill=tk.X, pady=2)
        tk.Checkbutton(row3, text="Export Point Cloud", variable=self.var_exp_point_cloud, font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_PRIMARY, selectcolor=UITheme.BG_ROOT, bd=0).pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(row3, text="Format:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(row3, textvariable=self.var_exp_pc_format, values=["laz", "las", "ply", "e57", "xyz"], state="readonly", width=6).pack(side=tk.LEFT, padx=(4, 12))
        tk.Checkbutton(row3, text="Export Tie Points", variable=self.var_exp_tie_points, font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_PRIMARY, selectcolor=UITheme.BG_ROOT, bd=0).pack(side=tk.LEFT)

        row4 = tk.Frame(self.card_export.body, bg=UITheme.BG_SURFACE)
        row4.pack(fill=tk.X, pady=2)
        tk.Checkbutton(row4, text="Export DEM (GeoTIFF)", variable=self.var_exp_dem, font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_PRIMARY, selectcolor=UITheme.BG_ROOT, bd=0).pack(side=tk.LEFT, padx=(0, 12))
        tk.Checkbutton(row4, text="Export Orthomosaic (GeoTIFF)", variable=self.var_exp_ortho, font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_PRIMARY, selectcolor=UITheme.BG_ROOT, bd=0).pack(side=tk.LEFT)

        row5 = tk.Frame(self.card_export.body, bg=UITheme.BG_SURFACE)
        row5.pack(fill=tk.X, pady=2)
        tk.Checkbutton(row5, text="Export 3D Tiled Model (LOD)", variable=self.var_exp_tiled_model, font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_PRIMARY, selectcolor=UITheme.BG_ROOT, bd=0).pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(row5, text="Format:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(row5, textvariable=self.var_exp_tiled_format, values=["cesium", "tls", "slpk", "osgb"], state="readonly", width=8).pack(side=tk.LEFT, padx=4)

        row6 = tk.Frame(self.card_export.body, bg=UITheme.BG_SURFACE)
        row6.pack(fill=tk.X, pady=2)
        tk.Checkbutton(row6, text="Export Contours", variable=self.var_exp_contours, font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_PRIMARY, selectcolor=UITheme.BG_ROOT, bd=0).pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(row6, text="Interval:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(row6, textvariable=self.var_exp_contour_interval, values=["0.5", "1.0", "2.0", "5.0"], width=5).pack(side=tk.LEFT, padx=(2, 8))
        tk.Label(row6, text="Format:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(row6, textvariable=self.var_exp_contours_format, values=["shp", "dxf", "geojson", "kml"], state="readonly", width=7).pack(side=tk.LEFT, padx=4)

        row7 = tk.Frame(self.card_export.body, bg=UITheme.BG_SURFACE)
        row7.pack(fill=tk.X, pady=2)
        tk.Checkbutton(row7, text="Export FBX", variable=self.var_exp_export_fbx, font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_PRIMARY, selectcolor=UITheme.BG_ROOT, bd=0).pack(side=tk.LEFT, padx=(0, 8))
        tk.Checkbutton(row7, text="Export PLY", variable=self.var_exp_export_ply, font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_PRIMARY, selectcolor=UITheme.BG_ROOT, bd=0).pack(side=tk.LEFT, padx=(0, 8))
        tk.Checkbutton(row7, text="Delighting", variable=self.var_exp_remove_lighting, font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_PRIMARY, selectcolor=UITheme.BG_ROOT, bd=0).pack(side=tk.LEFT, padx=(0, 8))
        tk.Checkbutton(row7, text="Quality PDF Report", variable=self.var_exp_report, font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_PRIMARY, selectcolor=UITheme.BG_ROOT, bd=0).pack(side=tk.LEFT)

    # -----------------------------------------------------------------------
    # Multi-Project Studio Card (Batch Selection & Queue)
    # -----------------------------------------------------------------------
    def _build_multi_project_card(self, parent: tk.Widget) -> None:
        self.card_multi = StudioCard(
            parent,
            title="Multi-Project Studio & Batch Queue",
            subtitle="Batch process multiple drone survey datasets with unified preset",
        )
        if self.active_mode != "multi":
            self.card_multi.pack_forget()

        body = self.card_multi.body

        # 1. Survey Root Directory
        row_root = tk.Frame(body, bg=UITheme.BG_SURFACE)
        row_root.pack(fill=tk.X, pady=(0, 4))

        tk.Label(
            row_root,
            text="Survey Root Directory",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_PRIMARY,
        ).pack(side=tk.LEFT)

        btn_browse_root = tk.Button(
            row_root,
            text="Browse...",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_PRIMARY,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=10,
            pady=2,
            command=self._browse_multi_root,
        )
        btn_browse_root.pack(side=tk.RIGHT, padx=(4, 0))

        btn_scan = tk.Button(
            row_root,
            text="Scan Subfolders",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_ACTIVE,
            fg=UITheme.ACCENT_CYAN,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=10,
            pady=2,
            command=self._scan_multi_projects,
        )
        btn_scan.pack(side=tk.RIGHT)

        ent_root = tk.Entry(
            body,
            textvariable=self.var_multi_root,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_PRIMARY,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER_CONTROL,
        )
        ent_root.pack(fill=tk.X, pady=(2, 6))

        # 2. Status & Quick Selection Bar
        row_sel = tk.Frame(body, bg=UITheme.BG_SURFACE)
        row_sel.pack(fill=tk.X, pady=(4, 4))

        self.lbl_multi_count = tk.Label(
            row_sel,
            textvariable=self.lbl_multi_scan_status,
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_MUTED,
        )
        self.lbl_multi_count.pack(side=tk.LEFT)

        btn_add = tk.Button(
            row_sel,
            text="+ Add Folder...",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_PRIMARY,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=8,
            pady=1,
            command=self._add_custom_multi_project,
        )
        btn_add.pack(side=tk.RIGHT, padx=(4, 0))

        btn_none = tk.Button(
            row_sel,
            text="Deselect All",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_MUTED,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=8,
            pady=1,
            command=lambda: self._select_all_multi(False),
        )
        btn_none.pack(side=tk.RIGHT, padx=(4, 0))

        btn_all = tk.Button(
            row_sel,
            text="Select All",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_PRIMARY,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=8,
            pady=1,
            command=lambda: self._select_all_multi(True),
        )
        btn_all.pack(side=tk.RIGHT)

        # 3. Scrollable Project Table Container
        tbl_container = tk.Frame(body, bg=UITheme.BORDER, bd=1, relief=tk.FLAT)
        tbl_container.pack(fill=tk.BOTH, expand=True, pady=(2, 8))

        self.canvas_multi = tk.Canvas(tbl_container, bg=UITheme.BG_INPUT, bd=0, highlightthickness=0, height=180)
        multi_scroll = ttk.Scrollbar(tbl_container, orient=tk.VERTICAL, command=self.canvas_multi.yview)
        self.frame_multi_items = tk.Frame(self.canvas_multi, bg=UITheme.BG_INPUT)

        self.canvas_multi.create_window((0, 0), window=self.frame_multi_items, anchor="nw")
        self.canvas_multi.configure(yscrollcommand=multi_scroll.set)

        self.canvas_multi.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        multi_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.frame_multi_items.bind(
            "<Configure>",
            lambda e: self.canvas_multi.configure(scrollregion=self.canvas_multi.bbox("all")),
        )

        # 4. Batch Pipeline Execution Options
        row_cfg = tk.Frame(body, bg=UITheme.BG_SURFACE)
        row_cfg.pack(fill=tk.X, pady=(6, 4))

        col1 = tk.Frame(row_cfg, bg=UITheme.BG_SURFACE)
        col1.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        tk.Label(col1, text="Preset:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(anchor=tk.W)
        ttk.Combobox(
            col1,
            textvariable=self.var_multi_preset,
            values=["pro (Professional Default)", "high (High Fidelity)", "low (Fast Preview)", "building (Architecture)"],
            state="readonly",
        ).pack(fill=tk.X, pady=(2, 0))

        col2 = tk.Frame(row_cfg, bg=UITheme.BG_SURFACE)
        col2.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        tk.Label(col2, text="Mesh Face Count:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(anchor=tk.W)
        ttk.Combobox(
            col2,
            textvariable=self.var_multi_faces,
            values=["1000000", "2000000", "3000000", "4000000", "5000000"],
            state="normal",
        ).pack(fill=tk.X, pady=(2, 0))

        row_cfg2 = tk.Frame(body, bg=UITheme.BG_SURFACE)
        row_cfg2.pack(fill=tk.X, pady=(4, 6))

        col3 = tk.Frame(row_cfg2, bg=UITheme.BG_SURFACE)
        col3.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        tk.Label(col3, text="8K Texture Pages:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(anchor=tk.W)
        ttk.Combobox(
            col3,
            textvariable=self.var_multi_tex_pages,
            values=["8", "16", "4", "2"],
            state="readonly",
        ).pack(fill=tk.X, pady=(2, 0))

        col4 = tk.Frame(row_cfg2, bg=UITheme.BG_SURFACE)
        col4.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))
        tk.Label(col4, text="Crop Method:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(anchor=tk.W)
        ttk.Combobox(
            col4,
            textvariable=self.var_multi_crop,
            values=["gps-square", "manual", "gps-circle", "none"],
            state="readonly",
        ).pack(fill=tk.X, pady=(2, 0))

        # 5. Dual Launch & Clipboard Actions
        row_actions = tk.Frame(body, bg=UITheme.BG_SURFACE)
        row_actions.pack(fill=tk.X, pady=(8, 2))

        btn_open_web = tk.Button(
            row_actions,
            text="Open Web Studio Panel (Geist UI) ↗",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.ACCENT_CYAN,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=12,
            pady=5,
            cursor="hand2",
            command=self._open_web_studio,
        )
        btn_open_web.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        btn_copy_cli = tk.Button(
            row_actions,
            text="Copy Batch CLI",
            font=("Segoe UI", 9),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_PRIMARY,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=12,
            pady=5,
            cursor="hand2",
            command=self._copy_batch_cli,
        )
        btn_copy_cli.pack(side=tk.RIGHT, padx=(6, 0))

    # -----------------------------------------------------------------------
    # Stepper Card
    # -----------------------------------------------------------------------
    def _build_stepper_card(self, parent: tk.Widget) -> None:
        self.card_stepper = StudioCard(
            parent,
            title="Pipeline Milestones",
            subtitle="7-stage automated photogrammetry trajectory",
        )
        self.card_stepper.pack(fill=tk.X, pady=(0, 6))

        self.stepper = StudioPipelineStepper(self.card_stepper.body)
        self.stepper.pack(fill=tk.X)

    # -----------------------------------------------------------------------
    # Console Feed & Deliverables
    # -----------------------------------------------------------------------
    def _build_log_card(self, parent: tk.Widget) -> None:
        self.card_log = StudioCard(
            parent,
            title="Execution Log",
            subtitle="Real-time Metashape subprocess output feed",
        )
        self.card_log.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # Dark-themed console container with dark TTK scrollbar
        log_container = tk.Frame(
            self.card_log.body,
            bg=UITheme.BG_CONSOLE,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
        )
        log_container.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        log_scrollbar = ttk.Scrollbar(log_container, orient=tk.VERTICAL)
        log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.txt_log = tk.Text(
            log_container,
            wrap=tk.WORD,
            bg=UITheme.BG_CONSOLE,
            fg=UITheme.TEXT_SECONDARY,
            insertbackground="white",
            font=("Consolas", 9),
            bd=0,
            highlightthickness=0,
            yscrollcommand=log_scrollbar.set,
            height=20,
        )
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scrollbar.config(command=self.txt_log.yview)

        self.txt_log.tag_config("stage", foreground=UITheme.ACCENT_CYAN, font=("Consolas", 9, "bold"))
        self.txt_log.tag_config("success", foreground=UITheme.ACCENT_GREEN_TXT, font=("Consolas", 9, "bold"))
        self.txt_log.tag_config("warning", foreground=UITheme.ACCENT_AMBER_TXT)
        self.txt_log.tag_config("error", foreground="#f87171", font=("Consolas", 9, "bold"))

        action_bar = tk.Frame(self.card_log.body, bg=UITheme.BG_SURFACE)
        action_bar.pack(fill=tk.X)

        self.btn_open_folder = tk.Button(
            action_bar,
            text="Open Output Folder",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_PRIMARY,
            activebackground=UITheme.BORDER_HOVER,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=12,
            pady=3,
            cursor="hand2",
            command=self._on_open_output_folder,
        )
        self.btn_open_folder.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_open_metashape = tk.Button(
            action_bar,
            text="Open in Metashape",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_PRIMARY,
            activebackground=UITheme.BORDER_HOVER,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=12,
            pady=3,
            cursor="hand2",
            command=self._on_open_metashape_gui,
        )
        self.btn_open_metashape.pack(side=tk.LEFT, padx=(0, 8))

        btn_copy = tk.Button(
            action_bar,
            text="Copy Log",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_MUTED,
            activebackground=UITheme.BORDER_HOVER,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=10,
            pady=3,
            cursor="hand2",
            command=self._copy_log,
        )
        btn_copy.pack(side=tk.RIGHT)

        btn_clear = tk.Button(
            action_bar,
            text="Clear",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_RAISED,
            fg=UITheme.TEXT_MUTED,
            activebackground=UITheme.BORDER_HOVER,
            bd=1,
            relief=tk.SOLID,
            highlightbackground=UITheme.BORDER,
            padx=10,
            pady=3,
            cursor="hand2",
            command=lambda: self.txt_log.delete("1.0", tk.END),
        )
        btn_clear.pack(side=tk.RIGHT, padx=4)

    # -----------------------------------------------------------------------
    # Event Handlers & Preset Logic
    # -----------------------------------------------------------------------
    def _select_preset(self, key: str) -> None:
        """Visual card selection logic with quiet accent styling."""
        self.selected_preset_key = key

        # Show config parameter card ONLY if Custom preset is chosen
        if hasattr(self, "card_adv"):
            if key == "custom":
                self.card_adv.pack(fill=tk.X, pady=(0, 6))
                if not getattr(self, "adv_expanded", False):
                    self._toggle_advanced()
            else:
                self.card_adv.pack_forget()
        for p_key, card in self.preset_cards.items():
            outer = card["outer"]
            inner = card["inner"]
            t = card["title"]
            b = card["badge"]
            s = card["spec"]
            d = card["desc"]
            top = card["top"]

            if p_key == key:
                outer.config(bg=UITheme.ACCENT_CYAN)
                inner.config(bg=UITheme.BG_SURFACE_RAISED)
                top.config(bg=UITheme.BG_SURFACE_RAISED)
                t.config(bg=UITheme.BG_SURFACE_RAISED, fg=UITheme.TEXT_PRIMARY)
                b.config(text="● Selected", bg=UITheme.BG_SURFACE_RAISED, fg=UITheme.ACCENT_CYAN)
                s.config(bg=UITheme.BG_SURFACE_RAISED, fg=UITheme.TEXT_PRIMARY)
                d.config(bg=UITheme.BG_SURFACE_RAISED, fg=UITheme.TEXT_SECONDARY)
            else:
                outer.config(bg=UITheme.BORDER)
                inner.config(bg=UITheme.BG_ROOT)
                top.config(bg=UITheme.BG_ROOT)
                t.config(bg=UITheme.BG_ROOT, fg=UITheme.TEXT_SECONDARY)
                b.config(text="○ Select", bg=UITheme.BG_ROOT, fg=UITheme.TEXT_MUTED)
                s.config(bg=UITheme.BG_ROOT, fg=UITheme.TEXT_MUTED)
                d.config(bg=UITheme.BG_ROOT, fg=UITheme.TEXT_MUTED)

        if key in ("pro", "auto"):
            self.var_align_quality.set("high")
            self.var_keypoint_limit.set("40000")
            self.var_tiepoint_limit.set("25000")
            self.var_faces.set("2500000")
            self.var_tex_pages.set("16")
            self.var_crop.set("gps-square")
            self.var_depth_quality.set("high")
            self.var_depth_filtering.set("moderate")
            self.var_guided.set(True)
            self.var_rolling.set(True)
            self.var_stationary.set(True)
            self.var_adaptive_fitting.set(True)
            self.var_close_holes.set(True)
            self.var_fill_holes.set(True)
            self.var_ghosting.set(True)
            self.var_ortho.set(False)
            self.var_build_dem.set(False)
            self.var_build_point_cloud.set(True)
            self.var_export_point_cloud.set(True)
            self.var_classify_ground.set(False)
            self.var_export_report.set(True)
        elif key == "building":
            self.var_align_quality.set("high")
            self.var_keypoint_limit.set("40000")
            self.var_tiepoint_limit.set("25000")
            self.var_faces.set("2000000")
            self.var_tex_pages.set("16")
            self.var_crop.set("gps-square")
            self.var_depth_quality.set("high")
            self.var_depth_filtering.set("moderate")
            self.var_guided.set(True)
            self.var_rolling.set(True)
            self.var_stationary.set(True)
            self.var_adaptive_fitting.set(True)
            self.var_close_holes.set(True)
            self.var_fill_holes.set(True)
            self.var_ghosting.set(True)
            self.var_ortho.set(False)
            self.var_build_dem.set(False)
            self.var_build_point_cloud.set(True)
            self.var_export_point_cloud.set(True)
            self.var_classify_ground.set(False)
            self.var_export_report.set(True)
        elif key == "map":
            self.var_align_quality.set("high")
            self.var_keypoint_limit.set("40000")
            self.var_tiepoint_limit.set("25000")
            self.var_faces.set("3000000")
            self.var_tex_pages.set("4")
            self.var_crop.set("gps-square")
            self.var_depth_quality.set("high")
            self.var_depth_filtering.set("moderate")
            self.var_guided.set(True)
            self.var_rolling.set(True)
            self.var_stationary.set(True)
            self.var_adaptive_fitting.set(True)
            self.var_close_holes.set(True)
            self.var_fill_holes.set(True)
            self.var_ghosting.set(True)
            self.var_ortho.set(True)
            self.var_build_dem.set(True)
            self.var_build_point_cloud.set(True)
            self.var_export_point_cloud.set(True)
            self.var_classify_ground.set(True)
            self.var_export_report.set(True)
        elif key == "custom":
            # Custom preset preserves current manual user parameters
            pass

        self._sync_dynamic_stepper()

    def _diagnose_survey_physics(self) -> None:
        """Audits survey telemetry, optics, GSD, and shutter physics in background thread."""
        img_dir = self.var_images_dir.get().strip()
        if not img_dir or not os.path.isdir(img_dir):
            messagebox.showwarning(
                "Survey Diagnosis",
                "Please select a valid raw images directory first.",
                parent=self.root,
            )
            return

        if not analyze_survey_physics:
            messagebox.showerror(
                "Survey Diagnosis",
                "Metashape reconstruction module could not be imported.",
                parent=self.root,
            )
            return

        self._log("\n" + "=" * 70, "stage")
        self._log("[DIAGNOSIS] Auditing survey telemetry, sensor optics, and GSD...", "stage")
        self._log(f"  Target Directory: {img_dir}", "dim")

        def _worker():
            try:
                phys = analyze_survey_physics(img_dir, sample_size=50)
                plan = derive_pro_photogrammetry_plan(phys)

                def _apply():
                    sh_str = "Mechanical Leaf Shutter [RS Comp: DISABLED]" if phys.get("shutter") == "mechanical" else "CMOS Rolling Shutter [RS Comp: ENABLED]"
                    fwd_ov = phys.get("forward_overlap_pct")
                    lat_ov = phys.get("lateral_overlap_pct")
                    ov_text = f"{fwd_ov:.1f}% fwd / {lat_ov:.1f}% lat" if fwd_ov is not None and lat_ov is not None else "Terrestrial / Uncalibrated"

                    self._log(f"  Sensor Hardware:   {phys.get('sensor_name')}", "success")
                    self._log(f"  Sensor Geometry:   {phys.get('sensor_w_mm', 0):.1f}x{phys.get('sensor_h_mm', 0):.1f}mm (Pitch: {phys.get('pixel_pitch_um', 0):.2f} µm)", "text")
                    self._log(f"  Shutter Mechanism: {sh_str}", "text")
                    self._log(f"  Flying Altitude:   {phys.get('flight_height_m', 0):.1f} m AGL", "text")
                    self._log(f"  Optical GSD:       {phys.get('gsd_cm', 0):.2f} cm/pixel", "success")
                    self._log(f"  Survey Overlap:    {ov_text}", "text")
                    self._log(f"  Dynamic Range:     Shadow clip: {phys.get('shadow_clip_ratio', 0)*100:.1f}% | Delighting: {'ACTIVATED' if plan.get('remove_lighting') else 'Not required'}", "text")
                    self._log(f"  Topographic Relief:Span: {phys.get('horizontal_span_m', 0):.1f}m | Delta Z: {phys.get('alt_span_m', 0):.1f}m | R={phys.get('relief_ratio', 0):.3f} -> {plan.get('surface_type', 'arbitrary').upper()}", "text")
                    self._log(f"  USGS Optimization: 4-Pass Gradual Selection (RU<=12 -> PA<=2.5 -> RE<=0.35 -> Multiplicity>=3)", "stage")
                    self._log("=" * 70 + "\n", "stage")

                    # Automatically sync parameters into UI
                    self.var_crop.set(plan["crop_method"])
                    self.var_faces.set(str(plan["custom_faces"]))
                    self.var_tex_pages.set(str(plan["texture_pages"]))
                    self.var_rolling.set(plan["rolling_shutter"])
                    self.var_surface_type.set(plan["surface_type"])
                    self.var_remove_lighting.set(plan["remove_lighting"])
                    self.var_ortho.set(plan["build_orthomosaic"])
                    self.var_build_dem.set(plan["build_dem"])
                    self.var_build_point_cloud.set(plan["build_point_cloud"])
                    self.var_classify_ground.set(plan["classify_ground"])

                    messagebox.showinfo(
                        "Survey Physics Diagnosis Complete",
                        f"Detected Sensor: {phys.get('sensor_name')}\n"
                        f"Optical GSD: {phys.get('gsd_cm', 0):.2f} cm/pixel (Altitude: {phys.get('flight_height_m', 0):.1f} m AGL)\n"
                        f"Shutter Physics: {sh_str}\n"
                        f"Surface Type: {plan.get('surface_type', 'arbitrary').upper()} (Relief R: {phys.get('relief_ratio', 0):.3f})\n"
                        f"Recommended Faces: {plan['custom_faces']:,} | Pages: {plan['texture_pages']}x 8K\n\n"
                        "Pipeline parameters have been calibrated to this survey's optical physics.",
                        parent=self.root,
                    )

                self.root.after(0, _apply)
            except Exception as e:
                self.root.after(0, lambda err=str(e): self._log(f"[DIAGNOSIS ERROR] {err}", "error"))

        threading.Thread(target=_worker, daemon=True).start()

    def get_dynamic_recon_stages(self) -> tuple[list[str], list[tuple[str, str, str]]]:
        """Dynamically compute photogrammetry stages based on active options."""
        keys: list[str] = ["ingest", "match", "align", "depth"]
        stages: list[tuple[str, str, str]] = [
            ("1", "Ingest", "Photos & blur filter"),
            ("2", "Match", "GPU feature matching"),
            ("3", "Align", "Cameras & crop"),
            ("4", "Depth", "Dense stereopsis"),
        ]

        if hasattr(self, "var_build_point_cloud") and self.var_build_point_cloud.get():
            keys.append("point_cloud")
            stages.append((str(len(stages) + 1), "Point Cloud", "Dense 3D point cloud"))

        keys.append("mesh")
        stages.append((str(len(stages) + 1), "Mesh", "Surface mesh"))

        keys.append("texture")
        stages.append((str(len(stages) + 1), "Texture", "Texture atlas"))

        if hasattr(self, "var_build_tiled_model") and self.var_build_tiled_model.get():
            keys.append("tiled_model")
            stages.append((str(len(stages) + 1), "Tiled Model", "Hierarchical 3D tiles"))

        keys.append("export")
        stages.append((str(len(stages) + 1), "Export", "GLB, OBJ & TIFF"))

        if (hasattr(self, "var_build_dem") and self.var_build_dem.get()) or (hasattr(self, "var_export_dem") and self.var_export_dem.get()):
            keys.append("dem")
            stages.append((str(len(stages) + 1), "DEM", "GeoTIFF DEM"))

        if hasattr(self, "var_build_contours") and self.var_build_contours.get():
            keys.append("contours")
            stages.append((str(len(stages) + 1), "Contours", "Topographic vectors"))

        if hasattr(self, "var_ortho") and self.var_ortho.get():
            keys.append("orthomosaic")
            stages.append((str(len(stages) + 1), "Orthomosaic", "GeoTIFF aerial ortho"))

        if hasattr(self, "var_export_report") and self.var_export_report.get():
            keys.append("report")
            stages.append((str(len(stages) + 1), "Report", "Calibration PDF report"))

        return keys, stages

    def get_dynamic_export_stages(self) -> tuple[list[str], list[tuple[str, str, str]]]:
        """Dynamically compute export stages based on active deliverables."""
        keys: list[str] = ["project", "mesh"]
        stages: list[tuple[str, str, str]] = [
            ("1", "Project", "Load .psx project"),
            ("2", "Verify", "Validate 3D mesh"),
        ]

        if hasattr(self, "var_exp_rebake") and self.var_exp_rebake.get():
            keys.append("texture")
            stages.append((str(len(stages) + 1), "Texture", "Re-bake atlas"))

        if hasattr(self, "var_exp_remove_lighting") and self.var_exp_remove_lighting.get():
            keys.append("delight")
            stages.append((str(len(stages) + 1), "Delight", "Remove lighting"))

        keys.append("glb")
        stages.append((str(len(stages) + 1), "glTF", "Export binary GLB"))

        keys.append("obj")
        stages.append((str(len(stages) + 1), "Wavefront", "Export OBJ & TIFF"))

        if hasattr(self, "var_exp_point_cloud") and self.var_exp_point_cloud.get():
            keys.append("point_cloud")
            stages.append((str(len(stages) + 1), "Point Cloud", "Export dense cloud"))

        if hasattr(self, "var_exp_dem") and self.var_exp_dem.get():
            keys.append("dem")
            stages.append((str(len(stages) + 1), "DEM", "Export GeoTIFF DEM"))

        if hasattr(self, "var_exp_ortho") and self.var_exp_ortho.get():
            keys.append("orthomosaic")
            stages.append((str(len(stages) + 1), "Orthomosaic", "Export Orthomosaic"))

        if hasattr(self, "var_exp_tiled_model") and self.var_exp_tiled_model.get():
            keys.append("tiled_model")
            stages.append((str(len(stages) + 1), "Tiled Model", "Export 3D tiles"))

        if hasattr(self, "var_exp_contours") and self.var_exp_contours.get():
            keys.append("contours")
            stages.append((str(len(stages) + 1), "Contours", "Export contours"))

        if hasattr(self, "var_exp_export_fbx") and self.var_exp_export_fbx.get():
            keys.append("fbx")
            stages.append((str(len(stages) + 1), "FBX", "Export Autodesk FBX"))

        if hasattr(self, "var_exp_export_ply") and self.var_exp_export_ply.get():
            keys.append("ply")
            stages.append((str(len(stages) + 1), "PLY", "Export Stanford PLY"))

        if hasattr(self, "var_exp_report") and self.var_exp_report.get():
            keys.append("report")
            stages.append((str(len(stages) + 1), "Report", "Export PDF report"))

        return keys, stages

    def get_dynamic_multi_stages(self) -> tuple[list[str], list[tuple[str, str, str]]]:
        """Dynamically compute multi-project batch stages based on selected queue."""
        selected: list[dict] = []
        if hasattr(self, "multi_discovered_projects") and hasattr(self, "multi_project_vars"):
            selected = [
                p for p in self.multi_discovered_projects
                if self.multi_project_vars.get(p["path"], tk.BooleanVar()).get()
            ]

        keys: list[str] = []
        stages: list[tuple[str, str, str]] = []

        if 1 <= len(selected) <= 7:
            for i, p in enumerate(selected):
                keys.append(f"proj_{i}")
                stages.append((str(i + 1), p["name"][:12], f"{p.get('photo_count', 0)} photos"))
        elif len(selected) > 7:
            keys = ["discover", "queue", "execute", "done"]
            stages = [
                ("1", "Discover", "Discovered surveys"),
                ("2", "Queue", f"{len(selected)} projects in batch"),
                ("3", "Execute", "Sequential execution"),
                ("4", "Deliverables", "Batch completion"),
            ]
        else:
            keys = ["discover", "select", "execute", "done"]
            stages = [
                ("1", "Discover", "Scan & detect inputs"),
                ("2", "Select", "Choose projects"),
                ("3", "Execute", "Sequential run"),
                ("4", "Deliverables", "Batch completion"),
            ]

        return keys, stages

    def _find_stage_index_by_key(self, mode: str, key: str) -> int:
        stages = getattr(self, "mode_stage_keys", {}).get(mode, [])
        try:
            return stages.index(key)
        except ValueError:
            return -1

    def _sync_dynamic_stepper(self) -> None:
        """Dynamically update stepper milestones to match currently selected processes."""
        if getattr(self, "is_running", False):
            return

        r_keys, r_stages = self.get_dynamic_recon_stages()
        e_keys, e_stages = self.get_dynamic_export_stages()
        m_keys, m_stages = self.get_dynamic_multi_stages()

        if hasattr(self, "mode_stage_keys"):
            self.mode_stage_keys["recon"] = r_keys
            self.mode_stage_keys["export"] = e_keys
            self.mode_stage_keys["multi"] = m_keys

        if hasattr(self, "mode_states"):
            self.mode_states["recon"]["stages"] = list(r_stages)
            self.mode_states["export"]["stages"] = list(e_stages)
            self.mode_states["multi"]["stages"] = list(m_stages)

        if not hasattr(self, "stepper") or not hasattr(self, "card_stepper"):
            return

        cur_mode = getattr(self, "active_mode", "recon")
        cur_stages = self.mode_states[cur_mode]["stages"]
        self.stepper.build_stages(cur_stages)

        if cur_mode == "recon":
            self.card_stepper.set_subtitle(f"{len(cur_stages)}-stage automated photogrammetry trajectory")
        elif cur_mode == "export":
            self.card_stepper.set_subtitle(f"{len(cur_stages)}-stage production asset export & processing")
        elif cur_mode == "multi":
            sel_cnt = len(self.batch_queue) if getattr(self, "batch_running", False) else len([
                p for p in getattr(self, "multi_discovered_projects", [])
                if self.multi_project_vars.get(p["path"], tk.BooleanVar()).get()
            ])
            self.card_stepper.set_subtitle(f"Batch execution trajectory across {sel_cnt} project(s)")

    def _set_mode(self, mode: str) -> None:
        if mode == self.active_mode:
            return

        # 1. Snapshot current tab's stepper state
        if hasattr(self, "stepper"):
            self.mode_states[self.active_mode] = self.stepper.get_state()
        self.active_mode = mode

        # 2. Toggle tab buttons & left cards
        if mode == "recon":
            self.btn_tab_recon.config(bg=UITheme.BG_SURFACE_RAISED, fg=UITheme.TEXT_PRIMARY)
            self.btn_tab_export.config(bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_SECONDARY)
            self.btn_tab_multi.config(bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_SECONDARY)
            self.card_export.pack_forget()
            self.card_multi.pack_forget()
            self.card_folder.pack(fill=tk.X, pady=(0, 6))
            self.card_preset.pack(fill=tk.X, pady=(0, 6))
            if getattr(self, "selected_preset_key", "pro") == "custom":
                self.card_adv.pack(fill=tk.X, pady=(0, 6))
            else:
                self.card_adv.pack_forget()
            self.btn_start.config(
                text="Start Reconstruction",
                bg=UITheme.BG_SURFACE_RAISED if self.is_running else UITheme.ACCENT_GREEN,
                fg=UITheme.TEXT_MUTED if self.is_running else "white",
                state=tk.DISABLED if self.is_running else tk.NORMAL,
            )
        elif mode == "export":
            self.btn_tab_export.config(bg=UITheme.BG_SURFACE_RAISED, fg=UITheme.TEXT_PRIMARY)
            self.btn_tab_recon.config(bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_SECONDARY)
            self.btn_tab_multi.config(bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_SECONDARY)
            self.card_folder.pack_forget()
            self.card_preset.pack_forget()
            self.card_adv.pack_forget()
            self.card_multi.pack_forget()
            self.card_export.pack(fill=tk.X, pady=(0, 6))
            self.btn_start.config(
                text="Export Assets",
                bg=UITheme.BG_SURFACE_RAISED if self.is_running else UITheme.ACCENT_CYAN,
                fg=UITheme.TEXT_MUTED if self.is_running else UITheme.BG_ROOT,
                state=tk.DISABLED if self.is_running else tk.NORMAL,
            )
        elif mode == "multi":
            self.btn_tab_multi.config(bg=UITheme.BG_SURFACE_RAISED, fg=UITheme.TEXT_PRIMARY)
            self.btn_tab_recon.config(bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_SECONDARY)
            self.btn_tab_export.config(bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_SECONDARY)
            self.card_folder.pack_forget()
            self.card_preset.pack_forget()
            self.card_adv.pack_forget()
            self.card_export.pack_forget()
            self.card_multi.pack(fill=tk.X, pady=(0, 6))
            self.btn_start.config(
                text="Run Selected Projects (Batch)",
                bg=UITheme.BG_SURFACE_RAISED if self.is_running else UITheme.ACCENT_GREEN,
                fg=UITheme.TEXT_MUTED if self.is_running else "white",
                state=tk.DISABLED if self.is_running else tk.NORMAL,
            )

        # 3. Restore the target mode's stepper state or sync dynamic stepper
        if not self.is_running:
            self._sync_dynamic_stepper()
        elif hasattr(self, "stepper"):
            self.stepper.restore_state(self.mode_states[mode])
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _check_metashape(self) -> None:
        if self.metashape_exe:
            self.lbl_metashape_badge.config(
                text="● Metashape Connected",
                bg=UITheme.ACCENT_GREEN_BG,
                fg=UITheme.ACCENT_GREEN_TXT,
            )
            self._log(f"[AutoMetashape Studio] System initialized with Metashape: {self.metashape_exe}", "stage")
        else:
            self.lbl_metashape_badge.config(
                text="▲ Metashape Not Found",
                bg=UITheme.ACCENT_AMBER_BG,
                fg=UITheme.ACCENT_AMBER_TXT,
            )
            self._log("[WARNING] Metashape executable not found in default paths. You will be prompted to locate it on start.", "warning")

    def _browse_reference_file(self) -> None:
        p = filedialog.askopenfilename(
            title="Select Ground Control Points (GCP) Reference CSV",
            filetypes=[("CSV & Text Files", "*.csv;*.txt"), ("All Files", "*.*")],
        )
        if p:
            self.var_import_reference.set(p)

    def _browse_images_dir(self) -> None:
        chosen = filedialog.askdirectory(
            title="Select Raw Photos Directory",
            initialdir=self.var_images_dir.get().strip() or os.path.expanduser("~"),
        )
        if chosen:
            self.var_images_dir.set(os.path.normpath(chosen))

    # -----------------------------------------------------------------------
    # Multi-Project Studio Batch Helpers
    # -----------------------------------------------------------------------
    def _browse_multi_root(self) -> None:
        chosen = filedialog.askdirectory(
            title="Select Parent Survey Directory",
            initialdir=self.var_multi_root.get().strip() or os.path.expanduser("~"),
        )
        if chosen:
            self.var_multi_root.set(os.path.normpath(chosen))
            self._scan_multi_projects()

    def _add_custom_multi_project(self) -> None:
        chosen = filedialog.askdirectory(
            title="Select Drone Survey Project Folder",
            initialdir=self.var_multi_root.get().strip() or os.path.expanduser("~"),
        )
        if chosen:
            path = os.path.normpath(chosen)
            if any(p["path"] == path for p in self.multi_discovered_projects):
                messagebox.showinfo("Already Added", "This folder is already in the project list.")
                return
            proj_info = self._analyze_project_dir(path)
            self.multi_discovered_projects.append(proj_info)
            self._populate_multi_project_rows()

    def _analyze_project_dir(self, dir_path: str) -> dict:
        name = os.path.basename(dir_path)
        direct_img_count = 0
        nested_img_count = 0
        has_psx = False
        models = []

        try:
            for entry in os.scandir(dir_path):
                if entry.is_file():
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext in SUPPORTED_IMAGE_EXTS and ext != ".tif":
                        direct_img_count += 1
                    elif ext == ".psx":
                        has_psx = True
                elif entry.is_dir():
                    if entry.name.lower().startswith("model") or entry.name.lower() in ("export", "exports", "output", "outputs"):
                        models.append(entry.name)
                    elif entry.name.lower() in ("images", "photos", "dji", "survey", "raw"):
                        for sub_entry in os.scandir(entry.path):
                            if sub_entry.is_file() and os.path.splitext(sub_entry.name)[1].lower() in SUPPORTED_IMAGE_EXTS:
                                nested_img_count += 1
        except Exception:
            pass

        total_photos = direct_img_count + nested_img_count

        status_text = "Ready"
        if models:
            status_text = f"Completed ({', '.join(models)})"
        elif has_psx:
            status_text = "Project PSX exists"
        elif total_photos > 0:
            status_text = f"{total_photos} photos"
        else:
            status_text = "Empty / No photos"

        return {
            "name": name,
            "path": dir_path,
            "photo_count": total_photos,
            "direct_photos": direct_img_count,
            "has_psx": has_psx,
            "models": models,
            "status": status_text,
        }

    def _scan_multi_projects(self) -> None:
        root_dir = self.var_multi_root.get().strip()
        if not root_dir or not os.path.isdir(root_dir):
            messagebox.showwarning("Directory Required", "Please select a valid parent survey directory.")
            return

        self._log(f"[Multi-Project Studio] Scanning root directory: {root_dir}", "stage")
        self.multi_discovered_projects.clear()
        EXCLUDED = {"model", "models", "output", "outputs", "export", "exports", "__pycache__", ".git"}

        try:
            for entry in os.scandir(root_dir):
                if entry.is_dir() and not entry.name.startswith(".") and entry.name.lower() not in EXCLUDED and not entry.name.lower().startswith("model"):
                    proj_info = self._analyze_project_dir(entry.path)
                    if proj_info["photo_count"] > 0 or proj_info["has_psx"] or proj_info["models"]:
                        self.multi_discovered_projects.append(proj_info)
        except Exception as e:
            self._log(f"[Multi-Project Studio] Scan error: {e}", "error")

        root_info = self._analyze_project_dir(root_dir)
        if root_info["direct_photos"] > 0 and not any(p["path"] == root_dir for p in self.multi_discovered_projects):
            self.multi_discovered_projects.insert(0, root_info)

        self._populate_multi_project_rows()

    def _populate_multi_project_rows(self) -> None:
        for w in self.frame_multi_items.winfo_children():
            w.destroy()

        self.multi_project_vars.clear()

        if not self.multi_discovered_projects:
            tk.Label(
                self.frame_multi_items,
                text="No projects found with photos or Metashape models. Click '+ Add Folder...' to add one manually.",
                font=("Segoe UI", 9),
                bg=UITheme.BG_INPUT,
                fg=UITheme.TEXT_MUTED,
                pady=16,
            ).pack(fill=tk.X)
            self.lbl_multi_scan_status.set("0 projects detected")
            return

        for p in self.multi_discovered_projects:
            var = tk.BooleanVar(value=True)
            self.multi_project_vars[p["path"]] = var

            row = tk.Frame(self.frame_multi_items, bg=UITheme.BG_SURFACE, bd=1, relief=tk.FLAT)
            row.pack(fill=tk.X, padx=4, pady=2)

            cb = tk.Checkbutton(
                row,
                text=p["name"],
                variable=var,
                font=("Segoe UI", 9, "bold"),
                bg=UITheme.BG_SURFACE,
                fg=UITheme.TEXT_PRIMARY,
                selectcolor=UITheme.BG_ROOT,
                bd=0,
                command=self._update_multi_selection_count,
            )
            cb.pack(side=tk.LEFT, padx=(6, 8), pady=4)

            badge_text = f"{p['photo_count']} photos"
            if p["models"]:
                badge_text += f" | {', '.join(p['models'])}"

            tk.Label(
                row,
                text=badge_text,
                font=("Consolas", 8),
                bg=UITheme.BG_SURFACE_RAISED,
                fg=UITheme.ACCENT_CYAN if p["models"] else UITheme.TEXT_SECONDARY,
                padx=6,
                pady=1,
            ).pack(side=tk.LEFT, padx=4)

            btn_open = tk.Button(
                row,
                text="Open Folder",
                font=("Segoe UI", 7),
                bg=UITheme.BG_SURFACE_RAISED,
                fg=UITheme.TEXT_MUTED,
                bd=0,
                padx=6,
                pady=1,
                cursor="hand2",
                command=lambda target=p["path"]: self._open_dir_in_os(target),
            )
            btn_open.pack(side=tk.RIGHT, padx=6)

        self._update_multi_selection_count()

    def _update_multi_selection_count(self) -> None:
        sel = sum(1 for v in self.multi_project_vars.values() if v.get())
        total = len(self.multi_discovered_projects)
        self.lbl_multi_scan_status.set(f"{total} projects detected ({sel} selected)")
        self._sync_dynamic_stepper()

    def _select_all_multi(self, selected: bool) -> None:
        for v in self.multi_project_vars.values():
            v.set(selected)
        self._update_multi_selection_count()

    def _open_dir_in_os(self, path: str) -> None:
        if path and os.path.exists(path):
            try:
                os.startfile(path)
            except Exception as e:
                self._log(f"[Multi-Project Studio] Could not open directory: {e}", "warning")

    def _open_web_studio(self) -> None:
        web_file = os.path.join(self.repo_dir, "web", "projects_dashboard.html")
        if not os.path.isfile(web_file):
            web_file = os.path.abspath("web/projects_dashboard.html")
        if os.path.isfile(web_file):
            webbrowser.open_new_tab(f"file:///{os.path.normpath(web_file).replace(os.sep, '/')}")
            self._log(f"[Multi-Project Studio] Opened Web Studio Dashboard in browser: {web_file}", "stage")
        else:
            messagebox.showerror("Error", f"Web dashboard file not found:\n{web_file}")

    def _copy_batch_cli(self) -> None:
        selected = [p for p in self.multi_discovered_projects if self.multi_project_vars.get(p["path"], tk.BooleanVar()).get()]
        if not selected:
            messagebox.showwarning("No Projects Selected", "Please select at least one project to generate CLI commands.")
            return

        cmds = []
        exe = self.metashape_exe or "metashape.exe"
        preset = self.var_multi_preset.get().split()[0]
        faces = self.var_multi_faces.get().split()[0]
        pages = self.var_multi_tex_pages.get().split()[0]
        crop = self.var_multi_crop.get()

        for p in selected:
            p_img = p["path"]
            p_out = os.path.join(p_img, "model")
            cmd = f'"{exe}" -r "{self.recon_script}" --images "{p_img}" --output "{p_out}" --project-name "{p["name"]}" --preset {preset} --faces {faces} --texture-pages {pages} --crop-method {crop}'
            cmds.append(cmd)

        cli_text = "\n\n".join(cmds)
        self.root.clipboard_clear()
        self.root.clipboard_append(cli_text)
        self._log(f"[Multi-Project Studio] Copied {len(selected)} batch command(s) to clipboard.", "success")
        messagebox.showinfo("CLI Commands Copied", f"Copied {len(selected)} batch reconstruction command(s) to clipboard.")

    def _start_multi_batch(self) -> None:
        if not self.metashape_exe or not os.path.isfile(self.metashape_exe):
            chosen = filedialog.askopenfilename(
                title="Locate metashape.exe",
                filetypes=[("Executable", "metashape.exe"), ("All Files", "*.*")],
            )
            if chosen and os.path.isfile(chosen):
                self.metashape_exe = os.path.normpath(chosen)
                self._check_metashape()
            else:
                messagebox.showerror("Error", "Metashape Pro executable is required.")
                return

        selected = [p for p in self.multi_discovered_projects if self.multi_project_vars.get(p["path"], tk.BooleanVar()).get()]
        if not selected:
            messagebox.showwarning("No Projects Selected", "Please select at least one project to batch process.")
            return

        self.batch_running = True
        self.batch_queue = selected
        self.batch_current_idx = 0
        self._run_next_batch_item()

    def _run_next_batch_item(self) -> None:
        if not self.batch_running or self.batch_current_idx >= len(self.batch_queue):
            self.batch_running = False
            self._log("\n" + "=" * 70, "success")
            self._log(f"[BATCH COMPLETE] All {len(self.batch_queue)} survey projects processed successfully!", "success")
            self._log("=" * 70 + "\n", "success")
            messagebox.showinfo("Batch Complete", f"All {len(self.batch_queue)} survey projects have been processed successfully!")
            self._on_finished(success=True)
            return

        item = self.batch_queue[self.batch_current_idx]
        img_dir = item["path"]
        proj_name = item["name"]
        total_items = len(self.batch_queue)
        current_num = self.batch_current_idx + 1

        self._log(f"\n[{current_num}/{total_items}] Starting Batch Project: {proj_name} ({img_dir})", "stage")

        preset = self.var_multi_preset.get().split()[0]
        faces = self.var_multi_faces.get().split()[0]
        pages = self.var_multi_tex_pages.get().split()[0]
        crop = self.var_multi_crop.get()
        out_dir = os.path.join(img_dir, "model")

        cmd = [
            self.metashape_exe,
            "-r",
            self.recon_script,
            "--images",
            img_dir,
            "--output",
            out_dir,
            "--project-name",
            proj_name,
            "--preset",
            preset,
            "--faces",
            str(faces),
            "--texture-pages",
            str(pages),
            "--crop-method",
            crop,
            "--build-point-cloud",
            "--export-point-cloud",
        ]

        self.stepper.reset()
        if total_items <= 7:
            self.stepper.set_active(self.batch_current_idx, f"Processing {proj_name}...")
        else:
            self.stepper.set_active(2, f"Project {current_num}/{total_items}: {proj_name}")
        self._spawn_worker(cmd, f"Batch [{current_num}/{total_items}]: {proj_name}")


    def _browse_output_dir(self) -> None:
        chosen = filedialog.askdirectory(
            title="Select Output Directory",
            initialdir=self.var_output_dir.get().strip() or self.var_images_dir.get().strip() or os.path.expanduser("~"),
        )
        if chosen:
            self.var_output_dir.set(os.path.normpath(chosen))

    def _browse_psx_file(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Select Metashape Project",
            filetypes=[("Metashape Project", "*.psx"), ("All Files", "*.*")],
            initialdir=os.path.dirname(self.var_psx_path.get().strip()) or os.path.expanduser("~"),
        )
        if chosen:
            self.var_psx_path.set(os.path.normpath(chosen))
            if not self.is_running:
                self.stepper.reset()
                self.mode_states["export"] = self.stepper.get_state()
                self.lbl_status.config(text="● Ready", fg=UITheme.TEXT_SECONDARY)

    def _browse_export_dir(self) -> None:
        chosen = filedialog.askdirectory(
            title="Select Export Directory",
            initialdir=self.var_export_out.get().strip() or os.path.expanduser("~"),
        )
        if chosen:
            self.var_export_out.set(os.path.normpath(chosen))

    def _on_images_dir_changed(self, *_args) -> None:
        folder = self.var_images_dir.get().strip()
        if not folder or not os.path.isdir(folder):
            self.lbl_photo_status.set("No folder selected. Click 'Browse...' to choose your raw photos folder.")
            return

        # When a new project folder is loaded, reset recon progress to clean state if not currently running
        if not self.is_running:
            self.stepper.reset()
            self.mode_states["recon"] = self.stepper.get_state()
            self.lbl_status.config(text="● Ready", fg=UITheme.TEXT_SECONDARY)

        default_out = os.path.join(folder, "model")
        cur_out = self.var_output_dir.get().strip()
        if not cur_out or cur_out.endswith("model"):
            self.var_output_dir.set(default_out)

        self.lbl_photo_status.set("Scanning directory and subfolders for supported photos...")

        def _scan():
            count = 0
            total_bytes = 0
            found_exts = set()

            for root, dirs, files in os.walk(folder):
                dirs[:] = [
                    d for d in dirs
                    if not os.path.abspath(os.path.join(root, d)).startswith(os.path.abspath(default_out))
                    and not d.lower().startswith("model")
                ]
                if os.path.abspath(root).startswith(os.path.abspath(default_out)) or os.path.basename(root).lower().startswith("model"):
                    continue
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in SUPPORTED_IMAGE_EXTS:
                        count += 1
                        found_exts.add(ext.replace(".", "").upper())
                        try:
                            total_bytes += os.path.getsize(os.path.join(root, f))
                        except Exception:
                            pass

            subfolder_count = 0
            try:
                for entry in sorted(os.listdir(folder)):
                    full_entry = os.path.join(folder, entry)
                    if os.path.isdir(full_entry) and not entry.lower().startswith("model") and not entry.startswith("."):
                        if not (os.path.abspath(full_entry).startswith(os.path.abspath(default_out))):
                            has_img = False
                            for _, _, fs in os.walk(full_entry):
                                if any(f.lower().endswith(tuple(SUPPORTED_IMAGE_EXTS)) for f in fs):
                                    has_img = True
                                    break
                            if has_img:
                                subfolder_count += 1
            except Exception:
                pass

            if count > 0:
                ext_str = ", ".join(sorted(found_exts)[:4])
                size_str = format_file_size(total_bytes)
                if subfolder_count > 1:
                    st_msg = (
                        f"{count:,} photos ({size_str}) across {subfolder_count} flights · {ext_str} · Multi-chunk alignment enabled"
                    )
                else:
                    st_msg = (
                        f"{count:,} photos ({size_str}) · {ext_str} · Ready for reconstruction"
                    )
            else:
                st_msg = "No supported photos (.jpg, .dng, .tif, etc.) found in folder."

            self.log_queue.put(f"__STATUS_PHOTO__{st_msg}")

        threading.Thread(target=_scan, daemon=True).start()

    def _on_psx_path_changed(self, *_args) -> None:
        psx = self.var_psx_path.get().strip()
        if psx and os.path.isfile(psx):
            if not self.var_export_out.get().strip():
                self.var_export_out.set(os.path.dirname(psx))

    def _get_active_output_dir(self) -> str:
        if self.active_mode == "recon":
            out = self.var_output_dir.get().strip()
            if not out:
                img_dir = self.var_images_dir.get().strip()
                if img_dir:
                    out = os.path.join(img_dir, "model")
            return out
        elif self.active_mode == "multi":
            if self.batch_queue and 0 <= self.batch_current_idx < len(self.batch_queue):
                return os.path.join(self.batch_queue[self.batch_current_idx]["path"], "model")
            return self.var_multi_root.get().strip()
        return self.var_export_out.get().strip()

    def _on_open_output_folder(self) -> None:
        folder = self._get_active_output_dir()
        if folder and os.path.isdir(folder):
            os.startfile(folder)
        else:
            messagebox.showinfo("Folder Not Found", f"Output directory does not exist yet:\n{folder}")

    def _on_open_metashape_gui(self) -> None:
        if not self.metashape_exe:
            messagebox.showerror("Error", "Metashape Pro executable not configured.")
            return

        folder = self._get_active_output_dir()
        if self.active_mode in ("recon", "multi"):
            psx = None
            if folder and os.path.isdir(folder):
                candidates = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".psx")]
                if candidates:
                    psx = candidates[0]
            if not psx and folder:
                psx = os.path.join(folder, "building_reconstruction.psx")
        else:
            psx = self.var_psx_path.get().strip()

        if psx and os.path.isfile(psx):
            subprocess.Popen([self.metashape_exe, psx])
        else:
            messagebox.showinfo("Project Not Found", f"No Metashape project found at:\n{psx}")

    def _copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.txt_log.get("1.0", tk.END))
        messagebox.showinfo("Copied", "Execution log copied to clipboard.")

    def _log(self, text: str, tag: str | None = None) -> None:
        self.txt_log.insert(tk.END, text + "\n", tag)
        self.txt_log.see(tk.END)

    def _update_clock(self) -> None:
        if self.is_running and self.start_time > 0:
            elapsed = time.time() - self.start_time
            self.stepper.update_elapsed(elapsed)
        self.root.after(1000, self._update_clock)

    # -----------------------------------------------------------------------
    # Subprocess Execution & Live Parsing
    # -----------------------------------------------------------------------
    def _on_start_clicked(self) -> None:
        if not self.metashape_exe or not os.path.isfile(self.metashape_exe):
            chosen = filedialog.askopenfilename(
                title="Locate metashape.exe",
                filetypes=[("Executable", "metashape.exe"), ("All Files", "*.*")],
            )
            if chosen and os.path.isfile(chosen):
                self.metashape_exe = os.path.normpath(chosen)
                self._check_metashape()
            else:
                messagebox.showerror("Error", "Metashape Pro executable is required.")
                return

        if self.active_mode == "recon":
            self._start_reconstruction()
        elif self.active_mode == "export":
            self._start_export()
        elif self.active_mode == "multi":
            self._start_multi_batch()

    def _start_reconstruction(self) -> None:
        img_dir = self.var_images_dir.get().strip()
        if not img_dir or not os.path.isdir(img_dir):
            messagebox.showwarning("Input Required", "Please select a valid raw photos folder.")
            return

        out_dir = self.var_output_dir.get().strip() or os.path.join(img_dir, "model")
        self.var_output_dir.set(out_dir)
        proj_name = infer_project_name(images_dir=img_dir, output_dir=out_dir) if infer_project_name else os.path.basename(img_dir)

        preset_key = getattr(self, "selected_preset_key", "pro")

        cmd = [
            self.metashape_exe,
            "-r",
            self.recon_script,
            "--images",
            img_dir,
            "--output",
            out_dir,
            "--project-name",
            proj_name,
        ]

        if preset_key in ("pro", "building", "map"):
            # Selected preset defines and owns its parameters; do NOT pass manual config overrides
            cmd.extend(["--preset", preset_key])
        else:
            # Custom preset: full manual control, pass all explicit config parameters
            cmd.extend([
                "--preset", "custom",
                "--align-quality", self.var_align_quality.get(),
                "--faces", str(self.var_faces.get()),
                "--texture-pages", str(self.var_tex_pages.get()),
                "--crop-method", self.var_crop.get(),
                "--depth-quality", self.var_depth_quality.get(),
                "--depth-filtering", self.var_depth_filtering.get(),
            ])
            if hasattr(self, "var_guided") and not self.var_guided.get():
                cmd.append("--no-guided-matching")
            if hasattr(self, "var_keypoint_limit") and self.var_keypoint_limit.get().strip() != "40000":
                cmd.extend(["--keypoint-limit", self.var_keypoint_limit.get().strip()])
            if hasattr(self, "var_tiepoint_limit") and self.var_tiepoint_limit.get().strip() != "25000":
                cmd.extend(["--tiepoint-limit", self.var_tiepoint_limit.get().strip()])
            if hasattr(self, "var_adaptive_fitting") and not self.var_adaptive_fitting.get():
                cmd.append("--no-adaptive-fitting")
            if not self.var_filter_tiepoints.get():
                cmd.append("--no-filter-tiepoints")
            if not self.var_rolling.get():
                cmd.append("--no-rolling-shutter")
            if not self.var_stationary.get():
                cmd.append("--no-filter-stationary")
            if not self.var_close_holes.get():
                cmd.append("--no-close-holes")
            if not self.var_fill_holes.get():
                cmd.append("--no-fill-holes")
            if not self.var_ghosting.get():
                cmd.append("--no-ghosting-filter")
            if self.var_ortho.get():
                cmd.append("--ortho")
            if hasattr(self, "var_build_dem") and self.var_build_dem.get():
                cmd.append("--build-dem")
            if hasattr(self, "var_build_tiled_model") and self.var_build_tiled_model.get():
                cmd.append("--build-tiled-model")
                cmd.extend(["--tiled-model-format", self.var_tiled_model_format.get()])
                cmd.extend(["--tile-size", self.var_tile_size.get()])
            if hasattr(self, "var_build_contours") and self.var_build_contours.get():
                cmd.append("--build-contours")
                cmd.extend(["--contour-interval", self.var_contour_interval.get()])
                cmd.extend(["--contours-format", self.var_contours_format.get()])
            if hasattr(self, "var_surface_type") and self.var_surface_type.get() != "arbitrary":
                cmd.extend(["--surface-type", self.var_surface_type.get()])
            if hasattr(self, "var_detect_markers") and self.var_detect_markers.get():
                cmd.append("--detect-markers")
                cmd.extend(["--target-type", self.var_target_type.get()])
            if hasattr(self, "var_import_reference") and self.var_import_reference.get().strip():
                cmd.extend(["--import-reference", self.var_import_reference.get().strip()])
            if hasattr(self, "var_calibrate_colors") and self.var_calibrate_colors.get():
                cmd.append("--calibrate-colors")
            if hasattr(self, "var_smooth_mesh") and self.var_smooth_mesh.get():
                cmd.append("--smooth-mesh")
            if hasattr(self, "var_remove_lighting") and self.var_remove_lighting.get():
                cmd.append("--remove-lighting")
            if hasattr(self, "var_export_fbx") and self.var_export_fbx.get():
                cmd.append("--export-fbx")
            if hasattr(self, "var_export_ply") and self.var_export_ply.get():
                cmd.append("--export-ply")
            if hasattr(self, "var_export_dxf") and self.var_export_dxf.get():
                cmd.append("--export-dxf")
            if hasattr(self, "var_export_tie_points") and self.var_export_tie_points.get():
                cmd.append("--export-tie-points")

        # Global runtime & deliverable options applicable across all modes
        if hasattr(self, "var_build_point_cloud") and self.var_build_point_cloud.get():
            cmd.append("--build-point-cloud")
        else:
            cmd.append("--no-point-cloud")
        if hasattr(self, "var_export_point_cloud") and self.var_export_point_cloud.get():
            cmd.append("--export-point-cloud")
        else:
            cmd.append("--no-export-point-cloud")
        if hasattr(self, "var_pc_format") and self.var_pc_format.get() != "laz":
            cmd.extend(["--point-cloud-format", self.var_pc_format.get()])
        if hasattr(self, "var_classify_ground") and self.var_classify_ground.get():
            cmd.append("--classify-ground")
        if hasattr(self, "var_classify_points") and self.var_classify_points.get():
            cmd.append("--classify-points")
        if hasattr(self, "var_export_report") and not self.var_export_report.get():
            cmd.append("--no-report")
        if self.var_pause_crop.get():
            cmd.append("--pause-after-crop")
        if self.var_rebake.get():
            cmd.append("--rebake-texture")
        if self.var_resume.get():
            cmd.append("--resume")
        if hasattr(self, "var_multi_chunk") and self.var_multi_chunk.get() != "disable":
            cmd.extend(["--multi-chunk", self.var_multi_chunk.get()])
        if hasattr(self, "var_chunk_align_method") and self.var_chunk_align_method.get() != "camera":
            cmd.extend(["--chunk-align-method", self.var_chunk_align_method.get()])

        self.stepper.reset()
        self.frame_crop_banner.pack_forget()
        self._spawn_worker(cmd, "3D Reconstruction")

    def _start_export(self) -> None:
        psx = self.var_psx_path.get().strip()
        if not psx or not os.path.isfile(psx):
            messagebox.showwarning("Input Required", "Please select a valid Metashape project (.psx).")
            return

        out_dir = self.var_export_out.get().strip() or os.path.dirname(psx)
        self.var_export_out.set(out_dir)
        proj_name = infer_project_name(output_dir=out_dir, project_file=psx) if infer_project_name else os.path.splitext(os.path.basename(psx))[0]

        cmd = [
            self.metashape_exe,
            "-r",
            self.export_script,
            "--project",
            psx,
            "--output",
            out_dir,
            "--project-name",
            proj_name,
            "--texture-format",
            self.var_exp_format.get(),
        ]

        if self.var_exp_rebake.get():
            cmd.append("--rebake-texture")
            cmd.extend(["--texture-pages", str(self.var_exp_pages.get())])
        if hasattr(self, "var_exp_point_cloud") and self.var_exp_point_cloud.get():
            cmd.append("--export-point-cloud")
        if hasattr(self, "var_exp_pc_format") and self.var_exp_pc_format.get():
            cmd.extend(["--point-cloud-format", self.var_exp_pc_format.get()])
        if hasattr(self, "var_exp_dem") and self.var_exp_dem.get():
            cmd.append("--export-dem")
        if hasattr(self, "var_exp_ortho") and self.var_exp_ortho.get():
            cmd.append("--export-orthomosaic")
        if hasattr(self, "var_exp_report") and self.var_exp_report.get():
            cmd.append("--export-report")
        if hasattr(self, "var_exp_tiled_model") and self.var_exp_tiled_model.get():
            cmd.append("--export-tiled-model")
            cmd.extend(["--tiled-model-format", self.var_exp_tiled_format.get()])
        if hasattr(self, "var_exp_contours") and self.var_exp_contours.get():
            cmd.append("--export-contours")
            cmd.extend(["--contour-interval", self.var_exp_contour_interval.get()])
            cmd.extend(["--contours-format", self.var_exp_contours_format.get()])
        if hasattr(self, "var_exp_remove_lighting") and self.var_exp_remove_lighting.get():
            cmd.append("--remove-lighting")
        if hasattr(self, "var_exp_smooth_mesh") and self.var_exp_smooth_mesh.get():
            cmd.append("--smooth-mesh")
        if hasattr(self, "var_exp_export_fbx") and self.var_exp_export_fbx.get():
            cmd.append("--export-fbx")
        if hasattr(self, "var_exp_export_ply") and self.var_exp_export_ply.get():
            cmd.append("--export-ply")
        if hasattr(self, "var_exp_tie_points") and self.var_exp_tie_points.get():
            cmd.append("--export-tie-points")

        self.stepper.reset()
        self.stepper.set_active(0, "Loading Project & Validating Mesh")
        self.frame_crop_banner.pack_forget()
        self._spawn_worker(cmd, "Export & Re-Bake")

    def _spawn_worker(self, cmd: list[str], task_name: str) -> None:
        self.is_running = True
        self.user_cancelled = False
        self.worker_mode = self.active_mode
        self.start_time = time.time()
        # Clean queue from any previous runs
        while not self.log_queue.empty():
            try:
                self.log_queue.get_nowait()
            except queue.Empty:
                break
        self.btn_start.config(state=tk.DISABLED, bg=UITheme.BG_SURFACE_RAISED, fg=UITheme.TEXT_MUTED)
        self.btn_stop.config(state=tk.NORMAL, bg=UITheme.ACCENT_RED, fg="white")
        self.progress_bar.start(10)
        self.lbl_status.config(text=f"● Running {task_name}...", fg=UITheme.ACCENT_CYAN)

        self._log(f"\n[{task_name}] Starting execution...", "stage")
        self._log(f"Command: {' '.join(cmd)}\n" + "─" * 70)

        def worker():
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = subprocess.CREATE_NO_WINDOW

            try:
                self.current_process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=creation_flags,
                )

                if self.current_process.stdout:
                    for line in iter(self.current_process.stdout.readline, ""):
                        if line:
                            clean = line.rstrip()
                            self.log_queue.put(clean)
                            self._parse_stepper(clean)
                    self.current_process.stdout.close()

                code = self.current_process.wait()
                elapsed = (time.time() - self.start_time) / 60.0
                if getattr(self, "user_cancelled", False):
                    self.log_queue.put(f"\n[CANCELLED] Task cancelled by user safely after {elapsed:.2f} minutes.")
                    self.log_queue.put("__TASK_DONE_CANCELLED__")
                elif code == 0:
                    self.log_queue.put(f"\n[SUCCESS] {task_name} finished in {elapsed:.2f} minutes!")
                    self.log_queue.put("__TASK_DONE_SUCCESS__")
                else:
                    self.log_queue.put(f"\n[ERROR] Process exited with code {code}")
                    self.log_queue.put("__TASK_DONE_ERROR__")
            except Exception as e:
                if not getattr(self, "user_cancelled", False):
                    self.log_queue.put(f"\n[EXCEPTION] Process failed: {e}")
                    self.log_queue.put("__TASK_DONE_ERROR__")
            finally:
                self.current_process = None

        threading.Thread(target=worker, daemon=True).start()

    def _update_stepper_mode(self, mode: str, idx: int, desc: str) -> None:
        def _apply():
            if mode in self.mode_states:
                st = self.mode_states[mode]
                if idx < st.get("active_idx", -1):
                    # Do not rewind active milestone
                    return
                st["active_idx"] = idx
                st["desc"] = desc
                st["is_done"] = False
                st["is_paused"] = False
                if 0 <= idx < len(st["stages"]):
                    num = st["stages"][idx][0]
                    name = st["stages"][idx][1]
                    st["status_text"] = f"Running Stage {idx+1}/{len(st['stages'])}: {name} - {desc}"
                    st["status_fg"] = UITheme.ACCENT_CYAN
            if self.active_mode == mode:
                self.stepper.set_active(idx, desc)
        self.root.after(0, _apply)

    def _complete_stepper_mode(self, mode: str) -> None:
        def _apply():
            if mode in self.mode_states:
                st = self.mode_states[mode]
                st["is_done"] = True
                st["is_paused"] = False
                st["active_idx"] = len(st["stages"])
                st["status_text"] = f"Complete: All {len(st['stages'])} stages finished successfully. Deliverables ready on disk."
                st["status_fg"] = UITheme.ACCENT_GREEN_TXT
            if self.active_mode == mode:
                self.stepper.set_all_done()
        self.root.after(0, _apply)

    def _parse_stepper(self, line: str) -> None:
        l = line.lower()
        mode = self.worker_mode or self.active_mode
        if mode == "recon":
            target_key = None
            desc = ""
            if "[1/7]" in line or "[1/6]" in line or "ingesting photos" in l or "ingesting chunk" in l:
                target_key = "ingest"
                desc = "Ingesting photos & blur filter"
            elif "[2/7]" in line or "[2/6]" in line or "gpu feature matching" in l:
                target_key = "match"
                desc = "GPU Feature Matching"
            elif "[3/7]" in line or "[3/6]" in line or "aligning cameras" in l:
                target_key = "align"
                desc = "Camera Alignment & Bundle Adjustment"
            elif "[auto-crop]" in line:
                target_key = "align"
                desc = "Alignment & Bounding Box Crop"
            elif "[pause_crop]" in line:
                self.root.after(0, self._on_pipeline_paused_for_crop)
                return
            elif "[4b/7]" in line or "dense point cloud" in l:
                target_key = "point_cloud"
                desc = "Generating Dense Point Cloud"
            elif "[5/7]" in line or "[5/6]" in line or "surface mesh" in l:
                target_key = "mesh"
                desc = "Fusing Surface Mesh & Closing Holes"
            elif "skipping steps 1-3" in l or "[4/7]" in line or "[4/6]" in line or "depth maps" in l:
                target_key = "depth"
                desc = "MVS Dense Stereopsis"
            elif "[6/7]" in line or "[6/6]" in line or ("baking" in l and "texture" in l) or "re-baking texture atlas" in l:
                target_key = "texture"
                desc = "Baking 8K Texture Atlas"
            elif "[6b/7]" in line or "hierarchical 3d tiled model" in l or "tiled model" in l:
                target_key = "tiled_model"
                desc = "Building Hierarchical 3D Tiled Model"
            elif "[7/7]" in line or "exporting production assets" in l or "saved binary glb" in l:
                target_key = "export"
                desc = "Exporting GLB, OBJ & Production Assets"
            elif "[7b/7]" in line or "digital elevation model" in l or "dem generation" in l or "geotiff dem" in l:
                target_key = "dem"
                desc = "Building & Exporting GeoTIFF DEM"
            elif "[7b2/7]" in line or "contour shapes" in l or "topographic contours" in l:
                target_key = "contours"
                desc = "Generating Topographic Contours"
            elif "[7c/7]" in line or "orthomosaic" in l:
                target_key = "orthomosaic"
                desc = "Building & Exporting GeoTIFF Orthomosaic"
            elif "[7d/7]" in line or "exporting dense point cloud" in l:
                target_key = "point_cloud" if "point_cloud" in self.mode_stage_keys.get("recon", []) else "export"
                desc = "Exporting Dense Point Cloud Deliverable"
            elif "[7e/7]" in line or "quality report" in l or "official calibration" in l:
                target_key = "report"
                desc = "Exporting Official Quality PDF Report"
            elif "[success]" in l and "entire pipeline finished" in l:
                self._complete_stepper_mode("recon")
                return

            if target_key:
                idx = self._find_stage_index_by_key("recon", target_key)
                if idx >= 0:
                    self._update_stepper_mode("recon", idx, desc)

        elif mode == "export":
            target_key = None
            desc = ""
            if "project:" in l or "loading metashape project" in l or "[import]" in l or "re-linked model saved" in l:
                target_key = "project"
                desc = "Loading Metashape Project"
            elif "[model info]" in l or "polygonal faces" in l or "[mesh]" in l:
                target_key = "mesh"
                desc = "Validating 3D Mesh Geometry"
            elif "[color calibration]" in l:
                desc = "Radiometric Color Calibration"
            elif "[texture]" in l or "texture atlas" in l or "re-baking" in l:
                target_key = "texture"
                desc = "Re-baking Texture Atlas"
            elif "[delighting]" in l or "ambient shadows" in l:
                target_key = "delight"
                desc = "Delighting & Shadow Removal"
            elif "[1/2]" in line or "binary gltf" in l or ".glb" in l:
                target_key = "glb"
                desc = "Exporting Binary GLB"
            elif "[2/2]" in line or "wavefront obj" in l or ".obj" in l:
                target_key = "obj"
                desc = "Exporting Wavefront OBJ & TIFF"
            elif "[point cloud]" in l or "dense point cloud" in l:
                target_key = "point_cloud"
                desc = "Exporting Dense Point Cloud"
            elif "[dem]" in l or "geotiff elevation" in l:
                target_key = "dem"
                desc = "Exporting GeoTIFF Elevation Model"
            elif "[orthomosaic]" in l:
                target_key = "orthomosaic"
                desc = "Exporting GeoTIFF Orthomosaic"
            elif "[tiled model]" in l:
                target_key = "tiled_model"
                desc = "Building & Exporting 3D Tiles"
            elif "[contours]" in l:
                target_key = "contours"
                desc = "Exporting Topographic Contours"
            elif "[fbx]" in l:
                target_key = "fbx"
                desc = "Exporting Autodesk FBX"
            elif "[ply]" in l:
                target_key = "ply"
                desc = "Exporting Stanford PLY"
            elif "[report]" in l:
                target_key = "report"
                desc = "Exporting Official PDF Report"
            elif "[success]" in l:
                self._complete_stepper_mode("export")
                return

            if target_key:
                idx = self._find_stage_index_by_key("export", target_key)
                if idx >= 0:
                    self._update_stepper_mode("export", idx, desc)

        elif mode == "multi":
            total_items = len(self.batch_queue) if getattr(self, "batch_queue", None) else 1
            cur_idx = getattr(self, "batch_current_idx", 0)
            proj_name = self.batch_queue[cur_idx]["name"] if getattr(self, "batch_queue", None) and cur_idx < len(self.batch_queue) else "Project"

            sub_desc = ""
            if "[1/7]" in line or "ingest" in l:
                sub_desc = "Ingesting photos"
            elif "[2/7]" in line or "gpu feature" in l:
                sub_desc = "Feature Matching"
            elif "[3/7]" in line or "aligning cameras" in l:
                sub_desc = "Camera Alignment"
            elif "[4/7]" in line or "depth maps" in l:
                sub_desc = "Depth Maps"
            elif "[4b/7]" in line or "point cloud" in l:
                sub_desc = "Dense Point Cloud"
            elif "[5/7]" in line or "surface mesh" in l:
                sub_desc = "Surface Mesh"
            elif "[6/7]" in line or "texture" in l:
                sub_desc = "Texture Atlas"
            elif "[7/7]" in line or "[export]" in l:
                sub_desc = "Exporting Deliverables"

            if total_items <= 7:
                desc = f"{proj_name} ({sub_desc})" if sub_desc else f"Processing {proj_name}..."
                self._update_stepper_mode("multi", cur_idx, desc)
            else:
                desc = f"Batch [{cur_idx+1}/{total_items}] {proj_name}: {sub_desc}" if sub_desc else f"Batch [{cur_idx+1}/{total_items}] {proj_name}"
                self._update_stepper_mode("multi", 2, desc)

    def _consume_log_queue(self) -> None:
        while not self.log_queue.empty():
            try:
                line = self.log_queue.get_nowait()
                if line.startswith("__STATUS_PHOTO__"):
                    self.lbl_photo_status.set(line[len("__STATUS_PHOTO__"):])
                elif line == "__TASK_DONE_SUCCESS__":
                    self._on_finished(success=True)
                elif line == "__TASK_DONE_CANCELLED__":
                    self._on_finished(success=False, user_cancelled=True)
                elif line == "__TASK_DONE_ERROR__":
                    if not getattr(self, "user_cancelled", False):
                        self._on_finished(success=False, user_cancelled=False)
                else:
                    tag = None
                    if any(s in line for s in ["[1/7]", "[2/7]", "[3/7]", "[4/7]", "[4b/7]", "[5/7]", "[6/7]", "[6b/7]", "[7/7]", "[7b/7]", "[7b2/7]", "[7c/7]", "[7d/7]", "[7e/7]", "[1/6]", "[2/6]", "[3/6]", "[4/6]", "[5/6]", "[6/6]", "[1/2]", "[2/2]", "[Auto-Crop]", "[EXPORT]", "[Point Cloud]", "[DEM]", "[Orthomosaic]", "[Contours]", "[Tiled Model]"]):
                        tag = "stage"
                    elif "[SUCCESS]" in line or "[CHECKPOINT]" in line:
                        tag = "success"
                    elif "[WARNING]" in line or "[Hardware]" in line or "[pause_crop]" in line or "[CANCELLED]" in line:
                        tag = "warning"
                    elif "[ERROR]" in line or "[EXCEPTION]" in line:
                        tag = "error"
                    self._log(line, tag)
            except queue.Empty:
                break
        self.root.after(80, self._consume_log_queue)

    def _on_pipeline_paused_for_crop(self) -> None:
        crop_idx = self._find_stage_index_by_key("recon", "align")
        if crop_idx < 0:
            crop_idx = 2
        self.stepper.set_paused(crop_idx, "Open Metashape Pro to Crop, then click Continue")
        self.lbl_status.config(
            text="Paused: Crop in Metashape & click Continue",
            fg=UITheme.ACCENT_AMBER_TXT,
        )
        self.progress_bar.stop()

        self.frame_crop_banner.pack(side=tk.TOP, fill=tk.X, padx=14, pady=(0, 6), before=self.center_frame)
        self.btn_continue_crop.pack(side=tk.LEFT, padx=(0, 10), before=self.btn_stop)

        self._log("\n" + "─" * 70, "warning")
        self._log("PIPELINE PAUSED: MANUAL CROPPING IN AGISOFT METASHAPE PRO", "warning")
        self._log("1. Click 'Open in Metashape' above to open your project in Metashape Pro.", "stage")
        self._log("2. In Metashape, use 'Resize Region' / 'Rotate Region' tools to crop the building.", "stage")
        self._log("3. Press Ctrl+S in Metashape Pro to save, then close Metashape Pro.", "stage")
        self._log("4. Click 'Continue Reconstruction' below to resume surface meshing.\n" + "─" * 70, "success")

        try:
            self.root.bell()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after_idle(lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

        folder = self._get_active_output_dir()
        psx = None
        if folder and os.path.isdir(folder):
            candidates = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".psx")]
            if candidates:
                psx = candidates[0]
        if not psx and folder:
            psx = os.path.join(folder, "building_reconstruction.psx")

        open_now = messagebox.askyesno(
            "Manual Bounding Box in Metashape Pro",
            "Camera alignment completed and saved to:\n"
            f"{psx}\n\n"
            "Would you like to open this project in Agisoft Metashape Pro now to adjust your bounding box?\n\n"
            "• Click [Yes] to launch Metashape Pro automatically.\n"
            "• In Metashape, adjust the bounding box, press Ctrl+S to save, then close Metashape Pro.\n"
            "• Return here and click 'Continue Reconstruction' to resume.",
            icon="question",
        )
        if open_now:
            self._on_open_metashape_gui()

    def _on_continue_crop_clicked(self) -> None:
        self.btn_continue_crop.pack_forget()
        self.frame_crop_banner.pack_forget()
        self.progress_bar.start(10)
        self.lbl_status.config(
            text="● Resuming reconstruction with manual box...",
            fg=UITheme.ACCENT_CYAN,
        )
        self.stepper.set_active(3, "Resuming MVS Depth Maps...")
        self._log("[USER] Sent resume signal. Continuing pipeline with manual bounding box...", "stage")

        if self.current_process and self.current_process.stdin:
            try:
                self.current_process.stdin.write("\n")
                self.current_process.stdin.flush()
            except Exception as e:
                self._log(f"[Warning] stdin write: {e}", "warning")

        out_dir = self._get_active_output_dir()
        if out_dir and os.path.isdir(out_dir):
            try:
                sentinel = os.path.join(out_dir, ".continue_crop")
                with open(sentinel, "w") as sf:
                    sf.write("continue\n")
            except Exception:
                pass

    def _clean_project_locks(self) -> None:
        """Removes any orphaned lock files from the active output project folder."""
        try:
            out_dir = self._get_active_output_dir()
            if out_dir and os.path.isdir(out_dir):
                for root_dir, _dirs, files in os.walk(out_dir):
                    for f in files:
                        if f.lower() == "lock":
                            lock_p = os.path.join(root_dir, f)
                            try:
                                os.remove(lock_p)
                                self._log(f"[SAFE-EXIT] Cleaned up project lock: {os.path.basename(lock_p)}", "stage")
                            except Exception:
                                pass
        except Exception:
            pass

    def _on_stop_clicked(self) -> None:
        if self.current_process and self.is_running:
            if messagebox.askyesno("Cancel Process", "Are you sure you want to cancel the running task?"):
                self.user_cancelled = True
                self.batch_running = False
                self._log("\n[USER] Cancel requested. Safely terminating background process...", "warning")
                try:
                    self.current_process.terminate()
                    time.sleep(0.4)
                    if self.current_process.poll() is None:
                        self.current_process.kill()
                except Exception as e:
                    self._log(f"[USER] Error terminating process: {e}", "error")

                self._clean_project_locks()
                self._log("[SAFE-EXIT] Process cancelled safely. Existing checkpoints remain intact.", "stage")
                self._on_finished(success=False, user_cancelled=True)

    def _on_window_close(self) -> None:
        if self.is_running and self.current_process:
            if not messagebox.askyesno("Exit AutoMetashape", "A task is currently running in the background.\n\nCancel task and exit safely?"):
                return
            self.user_cancelled = True
            try:
                self.current_process.terminate()
                time.sleep(0.3)
                if self.current_process.poll() is None:
                    self.current_process.kill()
            except Exception:
                pass
            self._clean_project_locks()
        self.root.destroy()

    def _on_finished(self, success: bool, user_cancelled: bool = False) -> None:
        finished_mode = self.worker_mode or self.active_mode
        self.is_running = False
        self.worker_mode = None
        self.progress_bar.stop()
        if hasattr(self, "btn_continue_crop"):
            self.btn_continue_crop.pack_forget()
        if hasattr(self, "frame_crop_banner"):
            self.frame_crop_banner.pack_forget()

        self.btn_start.config(
            state=tk.NORMAL,
            bg=UITheme.ACCENT_GREEN if self.active_mode == "recon" else UITheme.ACCENT_CYAN,
            fg="white" if self.active_mode == "recon" else UITheme.BG_ROOT,
        )
        self.btn_stop.config(state=tk.DISABLED, bg=UITheme.BG_SURFACE_RAISED, fg=UITheme.TEXT_MUTED)

        if getattr(self, "batch_running", False):
            if user_cancelled:
                self.batch_running = False
                self.lbl_status.config(text="● Status: Batch cancelled safely", fg=UITheme.ACCENT_AMBER_TXT)
                self._clean_project_locks()
            elif success:
                self._log(f"[BATCH ITEM SUCCESS] Project {self.batch_queue[self.batch_current_idx]['name']} complete.", "success")
                self.batch_current_idx += 1
                if self.batch_current_idx < len(self.batch_queue):
                    self.root.after(1000, self._run_next_batch_item)
                    return
                else:
                    self.batch_running = False
                    self._complete_stepper_mode("multi")
                    self.lbl_status.config(text="✓ Status: Batch Complete", fg=UITheme.ACCENT_GREEN_TXT)
                    self._log("\n" + "=" * 70, "success")
                    self._log(f"[BATCH COMPLETE] All {len(self.batch_queue)} survey projects processed successfully!", "success")
                    self._log("=" * 70 + "\n", "success")
                    messagebox.showinfo("Batch Complete", f"All {len(self.batch_queue)} survey projects have been processed successfully!")
            else:
                self.batch_running = False
                self.lbl_status.config(text="▲ Status: Batch Process Failed", fg="#f87171")
                messagebox.showerror(
                    "Batch Failed",
                    f"Project '{self.batch_queue[self.batch_current_idx]['name']}' failed. Batch halted.\nCheck console log for details.",
                )
            return

        if user_cancelled:
            self.lbl_status.config(text="● Status: Cancelled safely", fg=UITheme.ACCENT_AMBER_TXT)
            self._clean_project_locks()
        elif success:
            self._complete_stepper_mode(finished_mode)
            self.lbl_status.config(text="✓ Status: Complete", fg=UITheme.ACCENT_GREEN_TXT)
            messagebox.showinfo(
                "Completed",
                "Reconstruction finished successfully.\nDeliverables are saved in your output folder.",
            )
        else:
            self.lbl_status.config(text="▲ Status: Process Failed (See Log)", fg="#f87171")
            messagebox.showerror(
                "Failed",
                "Process encountered an error. Check console log for details.",
            )


def main() -> None:
    root = tk.Tk()
    app = AutoMetashapeStudio(root)
    root.mainloop()


if __name__ == "__main__":
    main()
