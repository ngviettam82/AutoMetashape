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
            lbl_sub = tk.Label(
                header_bar,
                text=self.subtitle_text,
                font=("Segoe UI", 8),
                bg=UITheme.BG_SURFACE,
                fg=UITheme.TEXT_MUTED,
            )
            lbl_sub.pack(side=tk.RIGHT)


# ---------------------------------------------------------------------------
# Horizontal Pipeline Stepper (7 Reconstruction Milestones)
# ---------------------------------------------------------------------------
class StudioPipelineStepper(tk.Frame):
    """Refined milestone trajectory tracking all 7 pipeline stages."""

    STAGES = [
        ("1", "Ingest", "Quality filter"),
        ("2", "Align", "Bundle adjust"),
        ("3", "Crop", "GPS boundary"),
        ("4", "Depth", "Dense stereopsis"),
        ("5", "Mesh", "600K surface"),
        ("6", "Texture", "16-page 8K"),
        ("7", "Export", "GLB & TIFF"),
    ]

    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(parent, bg=UITheme.BG_SURFACE, **kwargs)
        self.stage_frames: list[tk.Frame] = []
        self.stage_indicators: list[tk.Label] = []
        self.stage_titles: list[tk.Label] = []
        self.stage_subtitles: list[tk.Label] = []
        self._build()

    def _build(self) -> None:
        cards_row = tk.Frame(self, bg=UITheme.BG_SURFACE)
        cards_row.pack(fill=tk.X, padx=0, pady=(0, 6))

        for i, (num, name, subtitle) in enumerate(self.STAGES):
            outer = tk.Frame(cards_row, bg=UITheme.BORDER, bd=1, relief=tk.FLAT)
            outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2)

            inner = tk.Frame(outer, bg=UITheme.BG_ROOT, padx=6, pady=6)
            inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

            top_row = tk.Frame(inner, bg=UITheme.BG_ROOT)
            top_row.pack(fill=tk.X)

            ind = tk.Label(
                top_row,
                text=f"{num}.",
                font=("Segoe UI", 8, "bold"),
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_DIM,
            )
            ind.pack(side=tk.LEFT)

            t = tk.Label(
                inner,
                text=name,
                font=("Segoe UI", 8, "bold"),
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_MUTED,
                anchor=tk.W,
            )
            t.pack(fill=tk.X, pady=(2, 0))

            s = tk.Label(
                inner,
                text=subtitle,
                font=("Segoe UI", 7),
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_DIM,
                anchor=tk.W,
            )
            s.pack(fill=tk.X)

            self.stage_frames.append(outer)
            self.stage_indicators.append(ind)
            self.stage_titles.append(t)
            self.stage_subtitles.append(s)

        # Status summary bar under stages
        info_bar = tk.Frame(self, bg=UITheme.BG_ROOT, padx=10, pady=5)
        info_bar.pack(fill=tk.X)

        self.lbl_pipeline_status = tk.Label(
            info_bar,
            text="Status: Ready to reconstruct",
            font=("Segoe UI", 8),
            bg=UITheme.BG_ROOT,
            fg=UITheme.TEXT_SECONDARY,
            anchor=tk.W,
        )
        self.lbl_pipeline_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.lbl_timer = tk.Label(
            info_bar,
            text="Elapsed: --:--",
            font=("Consolas", 8),
            bg=UITheme.BG_ROOT,
            fg=UITheme.TEXT_MUTED,
        )
        self.lbl_timer.pack(side=tk.RIGHT)

    def reset(self) -> None:
        for i, (num, name, subtitle) in enumerate(self.STAGES):
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
        self.lbl_pipeline_status.config(
            text="Status: Ready to reconstruct",
            fg=UITheme.TEXT_SECONDARY,
        )
        self.lbl_timer.config(text="Elapsed: --:--")

    def set_active(self, idx: int, description: str = "") -> None:
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

        if 0 <= idx < len(self.stage_frames):
            num = self.STAGES[idx][0]
            name = self.STAGES[idx][1]
            self._apply_style(
                idx,
                outer_bg=UITheme.ACCENT_CYAN,
                inner_bg="#131e2a",
                ind_text=f"▶ {num}",
                ind_color=UITheme.ACCENT_CYAN,
                title_color=UITheme.TEXT_PRIMARY,
                sub_color=UITheme.ACCENT_CYAN,
            )
            desc_text = f"Running Stage {idx+1}/7: {name}"
            if description:
                desc_text += f" - {description}"
            self.lbl_pipeline_status.config(text=desc_text, fg=UITheme.ACCENT_CYAN)

    def set_paused(self, idx: int, message: str = "") -> None:
        if 0 <= idx < len(self.stage_frames):
            num = self.STAGES[idx][0]
            self._apply_style(
                idx,
                outer_bg=UITheme.ACCENT_AMBER,
                inner_bg=UITheme.ACCENT_AMBER_BG,
                ind_text=f"⏸ {num}",
                ind_color=UITheme.ACCENT_AMBER_TXT,
                title_color=UITheme.TEXT_PRIMARY,
                sub_color=UITheme.ACCENT_AMBER_TXT,
            )
            self.lbl_pipeline_status.config(
                text=f"Paused at Stage {idx+1}/7: {message}",
                fg=UITheme.ACCENT_AMBER_TXT,
            )

    def set_all_done(self) -> None:
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
            text="Complete: All 7 stages finished successfully. Deliverables ready on disk.",
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
        self.start_time: float = 0.0
        self.active_mode = "recon"
        self.adv_expanded = False
        self.selected_preset_key = "architectural"
        self.current_layout_mode = None  # "2col" or "1col"
        self.preset_cards: dict[str, dict[str, tk.Widget]] = {}

        # Initialize Tkinter variables
        self._init_variables()

        # Build UI layout
        self._build_ui()

        # Bind global mousewheel for smooth canvas scrolling
        self._bind_global_mousewheel()

        # Activate default preset
        self._select_preset("architectural")

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
        self.var_align_quality = tk.StringVar(value="medium")
        self.var_faces = tk.StringVar(value="600000")
        self.var_tex_pages = tk.StringVar(value="16")
        self.var_crop = tk.StringVar(value="gps-square")
        self.var_depth_quality = tk.StringVar(value="medium")
        self.var_depth_filtering = tk.StringVar(value="moderate")

        self.var_rolling = tk.BooleanVar(value=True)
        self.var_stationary = tk.BooleanVar(value=True)
        self.var_filter_tiepoints = tk.BooleanVar(value=True)
        self.var_close_holes = tk.BooleanVar(value=True)
        self.var_fill_holes = tk.BooleanVar(value=True)
        self.var_ghosting = tk.BooleanVar(value=True)
        self.var_pause_crop = tk.BooleanVar(value=False)
        self.var_rebake = tk.BooleanVar(value=False)
        self.var_restart = tk.BooleanVar(value=False)

        self.var_psx_path = tk.StringVar()
        self.var_export_out = tk.StringVar()
        self.var_exp_format = tk.StringVar(value="tiff")
        self.var_exp_pages = tk.StringVar(value="16")
        self.var_exp_rebake = tk.BooleanVar(value=True)

        self.lbl_photo_status = tk.StringVar(
            value="No folder selected. Click 'Browse...' to choose your raw photos folder."
        )

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
        self.btn_tab_export.pack(side=tk.LEFT)

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
            length=220,
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

        # Responsive columns inside content_frame
        self.col_left = tk.Frame(self.content_frame, bg=UITheme.BG_ROOT)
        self.col_right = tk.Frame(self.content_frame, bg=UITheme.BG_ROOT)

        # Populate Left Column (Configuration & Parameters)
        self._build_folder_card(self.col_left)
        self._build_presets_card(self.col_left)
        self._build_advanced_settings_card(self.col_left)
        self._build_export_card(self.col_left)

        # Populate Right Column (Milestones & Execution Log)
        self._build_stepper_card(self.col_right)
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
        if canvas_width >= 960:
            if self.current_layout_mode != "2col":
                self.current_layout_mode = "2col"
                self.col_left.pack_forget()
                self.col_right.pack_forget()
                self.col_left.pack(
                    side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(14, 7), pady=6
                )
                self.col_right.pack(
                    side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(7, 14), pady=6
                )
        else:
            if self.current_layout_mode != "1col":
                self.current_layout_mode = "1col"
                self.col_left.pack_forget()
                self.col_right.pack_forget()
                self.col_left.pack(
                    side=tk.TOP, fill=tk.X, padx=14, pady=(6, 0)
                )
                self.col_right.pack(
                    side=tk.TOP, fill=tk.X, padx=14, pady=(6, 6)
                )

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
                units = int(-1 * (delta / 120))
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
            "architectural",
            "Architectural Standard (Default)",
            "600K faces · 16-page 8K · GPS crop",
            "Balanced production geometry with CMOS rolling shutter and mesh hole closing.",
        )
        _make_preset(
            "draft",
            "Rapid Draft Preview",
            "500K faces · 4-page 8K · Draft align",
            "Fast ~15-minute run for quick alignment, boundary checking, and preliminary geometry.",
        )
        _make_preset(
            "heritage",
            "Ultra Heritage Detail",
            "2M faces · 32-page 8K · Structure crop",
            "High photographic fidelity for complex architectural facades, fine masonry, and monuments.",
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
            title="3. Pipeline Parameters",
            subtitle="Optional advanced calibration and processing flags",
        )
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

        grid = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        grid.pack(fill=tk.X, pady=(6, 8))

        # Row 1: Align, Faces, Pages
        tk.Label(grid, text="Align Quality:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=0, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(grid, textvariable=self.var_align_quality, values=["medium", "high", "highest", "low"], state="readonly", width=10).grid(row=0, column=1, padx=(4, 14), pady=3)

        tk.Label(grid, text="Target Faces:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=0, column=2, sticky=tk.W, pady=3)
        ttk.Combobox(grid, textvariable=self.var_faces, values=["600000", "1000000", "500000", "2000000", "3000000"], width=10).grid(row=0, column=3, padx=(4, 14), pady=3)

        tk.Label(grid, text="Texture Pages:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=0, column=4, sticky=tk.W, pady=3)
        ttk.Combobox(grid, textvariable=self.var_tex_pages, values=["16", "8", "4", "2", "32"], state="readonly", width=8).grid(row=0, column=5, padx=4, pady=3)

        # Row 2: Crop, Depth Quality, Depth Filtering
        tk.Label(grid, text="Bounding Box:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=1, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(grid, textvariable=self.var_crop, values=["gps-square", "structure", "manual", "none"], state="readonly", width=10).grid(row=1, column=1, padx=(4, 14), pady=3)

        tk.Label(grid, text="Depth Quality:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=1, column=2, sticky=tk.W, pady=3)
        ttk.Combobox(grid, textvariable=self.var_depth_quality, values=["medium", "high", "ultra", "low"], state="readonly", width=10).grid(row=1, column=3, padx=(4, 14), pady=3)

        tk.Label(grid, text="Depth Filter:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).grid(row=1, column=4, sticky=tk.W, pady=3)
        ttk.Combobox(grid, textvariable=self.var_depth_filtering, values=["moderate", "mild", "aggressive"], state="readonly", width=8).grid(row=1, column=5, padx=4, pady=3)

        # Checkboxes
        chk_row1 = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_row1.pack(fill=tk.X, pady=(2, 4))

        def _c(t, v, parent=chk_row1):
            return tk.Checkbutton(
                parent,
                text=t,
                variable=v,
                font=("Segoe UI", 8),
                bg=UITheme.BG_SURFACE,
                fg=UITheme.TEXT_SECONDARY,
                selectcolor=UITheme.BG_ROOT,
                activebackground=UITheme.BG_SURFACE,
                activeforeground=UITheme.TEXT_PRIMARY,
                bd=0,
            )

        _c("CMOS Rolling Shutter", self.var_rolling).pack(side=tk.LEFT, padx=(0, 10))
        _c("Exclude Stationary Points", self.var_stationary).pack(side=tk.LEFT, padx=(0, 10))
        _c("Tie-Point Filtering", self.var_filter_tiepoints).pack(side=tk.LEFT, padx=(0, 10))
        _c("Close Mesh Holes", self.var_close_holes).pack(side=tk.LEFT)

        chk_row2 = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_row2.pack(fill=tk.X, pady=(2, 4))
        _c("Texture Hole Filling", self.var_fill_holes, parent=chk_row2).pack(side=tk.LEFT, padx=(0, 10))
        _c("Ghosting Filter", self.var_ghosting, parent=chk_row2).pack(side=tk.LEFT, padx=(0, 10))
        _c("Force restart from scratch", self.var_restart, parent=chk_row2).pack(side=tk.LEFT)

        chk_row3 = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_row3.pack(fill=tk.X, pady=(2, 4))
        _c("Pause after camera alignment for manual crop", self.var_pause_crop, parent=chk_row3).pack(side=tk.LEFT)

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
            height=12,
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

        if key == "architectural":
            self.var_align_quality.set("medium")
            self.var_faces.set("600000")
            self.var_tex_pages.set("16")
            self.var_crop.set("gps-square")
            self.var_depth_quality.set("medium")
            self.var_depth_filtering.set("moderate")
            self.var_rolling.set(True)
            self.var_stationary.set(True)
            self.var_close_holes.set(True)
            self.var_fill_holes.set(True)
            self.var_ghosting.set(True)
        elif key == "draft":
            self.var_align_quality.set("low")
            self.var_faces.set("500000")
            self.var_tex_pages.set("4")
            self.var_crop.set("gps-square")
            self.var_depth_quality.set("low")
            self.var_depth_filtering.set("mild")
            self.var_rolling.set(False)
            self.var_stationary.set(True)
            self.var_close_holes.set(True)
            self.var_fill_holes.set(True)
            self.var_ghosting.set(False)
        elif key == "heritage":
            self.var_align_quality.set("high")
            self.var_faces.set("2000000")
            self.var_tex_pages.set("32")
            self.var_crop.set("structure")
            self.var_depth_quality.set("high")
            self.var_depth_filtering.set("moderate")
            self.var_rolling.set(True)
            self.var_stationary.set(True)
            self.var_close_holes.set(True)
            self.var_fill_holes.set(True)
            self.var_ghosting.set(True)

    def _set_mode(self, mode: str) -> None:
        self.active_mode = mode
        if mode == "recon":
            self.btn_tab_recon.config(bg=UITheme.BG_SURFACE_RAISED, fg=UITheme.TEXT_PRIMARY)
            self.btn_tab_export.config(bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_SECONDARY)
            self.card_export.pack_forget()
            self.card_folder.pack(fill=tk.X, pady=(0, 6))
            self.card_preset.pack(fill=tk.X, pady=(0, 6))
            self.card_adv.pack(fill=tk.X, pady=(0, 6))
            self.btn_start.config(text="Start Reconstruction", bg=UITheme.ACCENT_GREEN, fg="white")
        else:
            self.btn_tab_export.config(bg=UITheme.BG_SURFACE_RAISED, fg=UITheme.TEXT_PRIMARY)
            self.btn_tab_recon.config(bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_SECONDARY)
            self.card_folder.pack_forget()
            self.card_preset.pack_forget()
            self.card_adv.pack_forget()
            self.card_export.pack(fill=tk.X, pady=(0, 6))
            self.btn_start.config(text="Export Assets", bg=UITheme.ACCENT_CYAN, fg=UITheme.BG_ROOT)

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

    def _browse_images_dir(self) -> None:
        chosen = filedialog.askdirectory(
            title="Select Raw Photos Directory",
            initialdir=self.var_images_dir.get().strip() or os.path.expanduser("~"),
        )
        if chosen:
            self.var_images_dir.set(os.path.normpath(chosen))

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

            if count > 0:
                ext_str = ", ".join(sorted(found_exts)[:4])
                size_str = format_file_size(total_bytes)
                self.lbl_photo_status.set(
                    f"{count:,} photos ({size_str}) · {ext_str} · Ready for reconstruction"
                )
            else:
                self.lbl_photo_status.set("No supported photos (.jpg, .dng, .tif, etc.) found in folder.")

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
        psx = (
            os.path.join(folder, "building_reconstruction.psx")
            if self.active_mode == "recon"
            else self.var_psx_path.get().strip()
        )

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
        else:
            self._start_export()

    def _start_reconstruction(self) -> None:
        img_dir = self.var_images_dir.get().strip()
        if not img_dir or not os.path.isdir(img_dir):
            messagebox.showwarning("Input Required", "Please select a valid raw photos folder.")
            return

        out_dir = self.var_output_dir.get().strip() or os.path.join(img_dir, "model")
        self.var_output_dir.set(out_dir)

        cmd = [
            self.metashape_exe,
            "-r",
            self.recon_script,
            "--images",
            img_dir,
            "--output",
            out_dir,
            "--align-quality",
            self.var_align_quality.get(),
            "--faces",
            str(self.var_faces.get()),
            "--texture-pages",
            str(self.var_tex_pages.get()),
            "--crop-method",
            self.var_crop.get(),
            "--depth-quality",
            self.var_depth_quality.get(),
            "--depth-filtering",
            self.var_depth_filtering.get(),
        ]

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
        if self.var_pause_crop.get():
            cmd.append("--pause-after-crop")
        if self.var_rebake.get():
            cmd.append("--rebake-texture")
        if self.var_restart.get():
            cmd.append("--force-restart")

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

        cmd = [
            self.metashape_exe,
            "-r",
            self.export_script,
            "--project",
            psx,
            "--output",
            out_dir,
            "--texture-format",
            self.var_exp_format.get(),
        ]

        if self.var_exp_rebake.get():
            cmd.append("--rebake-texture")
            cmd.extend(["--texture-pages", str(self.var_exp_pages.get())])

        self.stepper.reset()
        self.stepper.set_active(5, "Re-baking 8K Textures & Exporting Assets")
        self.frame_crop_banner.pack_forget()
        self._spawn_worker(cmd, "Export & Re-Bake")

    def _spawn_worker(self, cmd: list[str], task_name: str) -> None:
        self.is_running = True
        self.start_time = time.time()
        self.btn_start.config(state=tk.DISABLED, bg=UITheme.BG_SURFACE_RAISED)
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
                if code == 0:
                    self.log_queue.put(f"\n[SUCCESS] {task_name} finished in {elapsed:.2f} minutes!")
                    self.log_queue.put("__TASK_DONE_SUCCESS__")
                else:
                    self.log_queue.put(f"\n[ERROR] Process exited with code {code}")
                    self.log_queue.put("__TASK_DONE_ERROR__")
            except Exception as e:
                self.log_queue.put(f"\n[EXCEPTION] Process failed: {e}")
                self.log_queue.put("__TASK_DONE_ERROR__")
            finally:
                self.current_process = None

        threading.Thread(target=worker, daemon=True).start()

    def _parse_stepper(self, line: str) -> None:
        l = line.lower()
        if "[1/6]" in line or "ingesting" in l:
            self.root.after(0, lambda: self.stepper.set_active(0, "Ingesting photos & filtering blur"))
        elif "[2/6]" in line or "gpu feature matching" in l:
            self.root.after(0, lambda: self.stepper.set_active(1, "GPU Feature Matching"))
        elif "[3/6]" in line or "aligning cameras" in l:
            self.root.after(0, lambda: self.stepper.set_active(1, "Camera Bundle Adjustment"))
        elif "[auto-crop]" in line or "camera gps square" in l:
            self.root.after(0, lambda: self.stepper.set_active(2, "Locking GPS Bounding Box"))
        elif "[pause_crop]" in l or "pause for user to adjust bounding box" in l:
            self.root.after(0, self._on_pipeline_paused_for_crop)
        elif "[4/6]" in line or "mvs depth maps" in l:
            self.root.after(0, lambda: self.stepper.set_active(3, "MVS Dense Stereopsis"))
        elif "[5/6]" in line or "surface mesh" in l:
            self.root.after(0, lambda: self.stepper.set_active(4, "Fusing 600K Face Mesh & Closing Holes"))
        elif "[6/6]" in line or "baking" in l or "texture atlas" in l or "[texture]" in line:
            self.root.after(0, lambda: self.stepper.set_active(5, "Baking 16-Page 8K Texture Atlas"))
        elif "exporting final" in l or "[export]" in line or "[1/2] exporting" in l:
            self.root.after(0, lambda: self.stepper.set_active(6, "Exporting GLB, OBJ, 16x TIFF, PSX"))
        elif "[success]" in l:
            self.root.after(0, self.stepper.set_all_done)

    def _consume_log_queue(self) -> None:
        while not self.log_queue.empty():
            try:
                line = self.log_queue.get_nowait()
                if line == "__TASK_DONE_SUCCESS__":
                    self._on_finished(success=True)
                elif line == "__TASK_DONE_ERROR__":
                    self._on_finished(success=False)
                else:
                    tag = None
                    if any(s in line for s in ["[1/6]", "[2/6]", "[3/6]", "[4/6]", "[5/6]", "[6/6]", "[Auto-Crop]"]):
                        tag = "stage"
                    elif "[SUCCESS]" in line or "[CHECKPOINT]" in line:
                        tag = "success"
                    elif "[WARNING]" in line or "[Hardware]" in line or "[pause_crop]" in line:
                        tag = "warning"
                    elif "[ERROR]" in line or "[EXCEPTION]" in line:
                        tag = "error"
                    self._log(line, tag)
            except queue.Empty:
                break
        self.root.after(80, self._consume_log_queue)

    def _on_pipeline_paused_for_crop(self) -> None:
        self.stepper.set_paused(2, "Open Metashape Pro to Crop, then click Continue")
        self.lbl_status.config(
            text="Paused: Open Metashape Pro to Crop, then click Continue",
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
        self._log("4. Click 'Continue Reconstruction' below to resume 600K meshing.\n" + "─" * 70, "success")

        try:
            self.root.bell()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after_idle(lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

        folder = self._get_active_output_dir()
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

    def _on_stop_clicked(self) -> None:
        if self.current_process and self.is_running:
            if messagebox.askyesno("Cancel Process", "Are you sure you want to cancel the running reconstruction?"):
                self._log("\n[USER] Terminating active process...", "warning")
                try:
                    self.current_process.terminate()
                    time.sleep(0.5)
                    if self.current_process.poll() is None:
                        self.current_process.kill()
                    self._log("[USER] Process terminated.", "warning")
                except Exception as e:
                    self._log(f"[USER] Error terminating: {e}", "error")
                self._on_finished(success=False, user_cancelled=True)

    def _on_finished(self, success: bool, user_cancelled: bool = False) -> None:
        self.is_running = False
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

        if user_cancelled:
            self.lbl_status.config(text="● Status: Cancelled by user", fg=UITheme.ACCENT_AMBER_TXT)
        elif success:
            self.stepper.set_all_done()
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
