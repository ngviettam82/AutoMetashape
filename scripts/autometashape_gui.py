# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape Studio - Modern High-Precision Desktop Photogrammetry GUI.

A user-friendly, responsive desktop application for autonomous 3D photogrammetry.
Designed following modern open-source UI/UX principles and studio design patterns:
- Windows Per-Monitor v2 DPI auto-scaling and dynamic layout metrics
- 1-2-3 Progressive Disclosure (Input -> Preset -> Start)
- Sleek flat card architecture with VS Code/GitHub dark slate aesthetics
- Interactive Visual Breadcrumb Stepper (7-stage pipeline milestones)
- Interactive Preset Selection Cards with hover and active states
- Real-time photo statistics scanner (photo count, disk size, camera RAW formats)
- Collapsible Advanced Fine-Tuning (zero clutter by default)
- Seamless Manual Bounding Box pause and resume workflow
- Real-time streaming log console with syntax highlights and direct OS actions
"""

from __future__ import annotations

import ctypes
import math
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
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (-4) provides native scaling
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
    except Exception:
        try:
            # PROCESS_PER_MONITOR_DPI_AWARE (2)
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                # PROCESS_SYSTEM_DPI_AWARE (1)
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Visual Theme Tokens (Studio Dark Slate Aesthetics)
# ---------------------------------------------------------------------------
class UITheme:
    """Modern studio palette inspired by VS Code, GitHub Desktop, and Blender."""

    # Surfaces
    BG_ROOT = "#0d1117"             # Deep space / canvas background
    BG_SURFACE = "#161b22"          # Surface card container
    BG_SURFACE_ALT = "#1f2937"      # Elevated container / header
    BG_INPUT = "#0d1117"            # Input text fields
    BG_CARD_HOVER = "#1c2433"       # Preset card hover
    BG_CARD_ACTIVE = "#172554"      # Preset card selected state
    BG_CONSOLE = "#06090f"          # Terminal console deep black

    # Borders
    BORDER_SUBTLE = "#21262d"       # Default divider border
    BORDER_CARD = "#30363d"         # Card outline
    BORDER_HOVER = "#484f58"        # Interactive hover outline
    BORDER_ACTIVE = "#38bdf8"       # Active sky cyan outline
    BORDER_SUCCESS = "#238636"      # Success green outline

    # Typography
    TEXT_WHITE = "#f0f6fc"          # High-contrast headers
    TEXT_BODY = "#e6edf3"           # Primary body text
    TEXT_MUTED = "#8b949e"          # Secondary descriptive labels
    TEXT_DIM = "#6e7681"            # Dim footnotes / watermarks

    # Accents & Semantics
    ACCENT_CYAN = "#38bdf8"         # Brand highlight / sky cyan
    ACCENT_CYAN_HOVER = "#0284c7"
    ACCENT_BLUE = "#3b82f6"         # Primary blue
    ACCENT_BLUE_HOVER = "#2563eb"
    ACCENT_GREEN = "#238636"        # Action / Run emerald green
    ACCENT_GREEN_HOVER = "#2ea043"
    ACCENT_GREEN_BG = "#064e3b"     # Milestone success surface
    ACCENT_GREEN_TXT = "#6ee7b7"    # Milestone success text
    ACCENT_AMBER = "#d29922"        # Warning / Paused amber
    ACCENT_AMBER_BG = "#451a03"     # Paused surface
    ACCENT_AMBER_TXT = "#fbbf24"    # Paused text
    ACCENT_RED = "#da3633"          # Cancel / Error red
    ACCENT_RED_HOVER = "#b62324"


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
# Modern Studio Card Container
# ---------------------------------------------------------------------------
class StudioCard(tk.Frame):
    """Sleek flat container with subtle border and optional header badge."""

    def __init__(
        self,
        parent: tk.Widget,
        title: str = "",
        badge: str = "",
        icon: str = "",
        accent_color: str = UITheme.ACCENT_CYAN,
        **kwargs,
    ) -> None:
        super().__init__(
            parent,
            bg=UITheme.BORDER_CARD,
            bd=1,
            relief=tk.FLAT,
            **kwargs,
        )
        self.inner = tk.Frame(self, bg=UITheme.BG_SURFACE)
        self.inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        self.title_text = title
        self.badge_text = badge
        self.icon_text = icon
        self.accent_color = accent_color

        if title or icon or badge:
            self._build_header()

        self.body = tk.Frame(self.inner, bg=UITheme.BG_SURFACE)
        self.body.pack(fill=tk.BOTH, expand=True, padx=14, pady=(6, 12))

    def _build_header(self) -> None:
        header_bar = tk.Frame(self.inner, bg=UITheme.BG_SURFACE)
        header_bar.pack(fill=tk.X, padx=14, pady=(10, 4))

        left = tk.Frame(header_bar, bg=UITheme.BG_SURFACE)
        left.pack(side=tk.LEFT, fill=tk.Y)

        # Left accent pill bar
        pill = tk.Frame(left, bg=self.accent_color, width=3, height=14)
        pill.pack(side=tk.LEFT, padx=(0, 8), pady=2)
        pill.pack_propagate(False)

        title_full = f"{self.icon_text}  {self.title_text}" if self.icon_text else self.title_text
        lbl_title = tk.Label(
            left,
            text=title_full.strip(),
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_WHITE,
        )
        lbl_title.pack(side=tk.LEFT)

        if self.badge_text:
            lbl_badge = tk.Label(
                header_bar,
                text=self.badge_text,
                font=("Segoe UI", 8),
                bg=UITheme.BG_SURFACE_ALT,
                fg=UITheme.TEXT_MUTED,
                padx=8,
                pady=2,
            )
            lbl_badge.pack(side=tk.RIGHT)


# ---------------------------------------------------------------------------
# Horizontal Pipeline Stepper (7 Reconstruction Stages)
# ---------------------------------------------------------------------------
class StudioPipelineStepper(tk.Frame):
    """Modern horizontal trajectory component tracking all 7 pipeline milestones."""

    STAGES = [
        ("1", "Ingest", "Sharpness & Blur Check"),
        ("2", "Align", "Camera Bundle Adjustment"),
        ("3", "Crop", "Horizontal Boundary Box"),
        ("4", "Depth", "MVS Dense Stereopsis"),
        ("5", "Mesh", "600K Face Surface Fusion"),
        ("6", "Texture", "16-Page 8K Mosaic Atlas"),
        ("7", "Export", "GLB, OBJ, TIFF & PSX"),
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
            outer = tk.Frame(cards_row, bg=UITheme.BORDER_CARD, bd=1, relief=tk.FLAT)
            outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=2)

            inner = tk.Frame(outer, bg=UITheme.BG_ROOT, padx=6, pady=6)
            inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

            top_row = tk.Frame(inner, bg=UITheme.BG_ROOT)
            top_row.pack(fill=tk.X)

            ind = tk.Label(
                top_row,
                text=f"○ {num}",
                font=("Segoe UI", 8, "bold"),
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_DIM,
            )
            ind.pack(side=tk.LEFT)

            t = tk.Label(
                inner,
                text=name,
                font=("Segoe UI", 9, "bold"),
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

        # Status summary banner under stages
        info_bar = tk.Frame(self, bg=UITheme.BG_ROOT, padx=10, pady=5)
        info_bar.pack(fill=tk.X)

        self.lbl_pipeline_status = tk.Label(
            info_bar,
            text="Pipeline Status: Waiting for user to start...",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_ROOT,
            fg=UITheme.TEXT_MUTED,
            anchor=tk.W,
        )
        self.lbl_pipeline_status.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.lbl_timer = tk.Label(
            info_bar,
            text="Elapsed: --:--",
            font=("Consolas", 8),
            bg=UITheme.BG_ROOT,
            fg=UITheme.TEXT_DIM,
        )
        self.lbl_timer.pack(side=tk.RIGHT)

    def reset(self) -> None:
        for i, (num, name, subtitle) in enumerate(self.STAGES):
            outer = self.stage_frames[i]
            inner = outer.winfo_children()[0]
            outer.config(bg=UITheme.BORDER_CARD)
            inner.config(bg=UITheme.BG_ROOT)
            for child in inner.winfo_children():
                child.config(bg=UITheme.BG_ROOT)
                if hasattr(child, "winfo_children"):
                    for sub in child.winfo_children():
                        sub.config(bg=UITheme.BG_ROOT)
            self.stage_indicators[i].config(text=f"○ {num}", fg=UITheme.TEXT_DIM)
            self.stage_titles[i].config(fg=UITheme.TEXT_MUTED)
            self.stage_subtitles[i].config(fg=UITheme.TEXT_DIM)
        self.lbl_pipeline_status.config(
            text="Pipeline Status: Ready to start",
            fg=UITheme.TEXT_MUTED,
        )
        self.lbl_timer.config(text="Elapsed: --:--")

    def set_active(self, idx: int, description: str = "") -> None:
        # Mark all previous steps completed
        for i in range(idx):
            self._apply_style(
                i,
                outer_bg=UITheme.ACCENT_GREEN,
                inner_bg=UITheme.ACCENT_GREEN_BG,
                ind_text="✓ Done",
                ind_color=UITheme.ACCENT_GREEN_TXT,
                title_color=UITheme.TEXT_WHITE,
                sub_color=UITheme.ACCENT_GREEN_TXT,
            )

        # Mark current step active
        if 0 <= idx < len(self.stage_frames):
            num = self.STAGES[idx][0]
            name = self.STAGES[idx][1]
            self._apply_style(
                idx,
                outer_bg=UITheme.ACCENT_CYAN,
                inner_bg=UITheme.BG_CARD_ACTIVE,
                ind_text=f"▶ {num} RUNNING",
                ind_color=UITheme.ACCENT_CYAN,
                title_color=UITheme.TEXT_WHITE,
                sub_color=UITheme.ACCENT_CYAN,
            )
            desc_text = f"▶ Stage {idx+1}/7: {name}"
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
                ind_text=f"⏸ {num} PAUSED",
                ind_color=UITheme.ACCENT_AMBER_TXT,
                title_color=UITheme.TEXT_WHITE,
                sub_color=UITheme.ACCENT_AMBER_TXT,
            )
            self.lbl_pipeline_status.config(
                text=f"⏸️ Stage {idx+1}/7 Paused: {message}",
                fg=UITheme.ACCENT_AMBER_TXT,
            )

    def set_all_done(self) -> None:
        for i in range(len(self.stage_frames)):
            self._apply_style(
                i,
                outer_bg=UITheme.ACCENT_GREEN,
                inner_bg=UITheme.ACCENT_GREEN_BG,
                ind_text="✓ Done",
                ind_color=UITheme.ACCENT_GREEN_TXT,
                title_color=UITheme.TEXT_WHITE,
                sub_color=UITheme.ACCENT_GREEN_TXT,
            )
        self.lbl_pipeline_status.config(
            text="✓ All 7 Stages Completed Successfully! Deliverables saved to disk.",
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
    """User-friendly, modern photogrammetry desktop studio."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AutoMetashape Studio - Autonomous 3D Photogrammetry")
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
        self.active_mode = "recon"  # "recon" or "export"
        self.adv_expanded = False
        self.selected_preset_key = "architectural"
        self.preset_cards: dict[str, dict[str, tk.Widget]] = {}

        # Initialize Tkinter variables
        self._init_variables()

        # Build UI layout
        self._build_ui()

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

        # Tell Tkinter to scale coordinate rendering
        try:
            self.root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass

        # Compute screen work area
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()

        # Smart dynamic window size: comfortable default bounded by monitor
        win_w = min(1280, max(940, int(sw * 0.58)))
        win_h = min(960, max(720, int(sh * 0.84)))

        # Center on screen
        pos_x = max(0, (sw - win_w) // 2)
        pos_y = max(0, (sh - win_h) // 2 - 25)

        self.root.geometry(f"{win_w}x{win_h}+{pos_x}+{pos_y}")
        self.root.minsize(min(880, sw - 40), min(640, sh - 60))

    def _init_ttk_styles(self) -> None:
        """Configure native TTK Clam theme for dark slate widgets."""
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        # TCombobox dark styling
        self.style.configure(
            "TCombobox",
            fieldbackground=UITheme.BG_INPUT,
            background=UITheme.BORDER_CARD,
            foreground=UITheme.TEXT_WHITE,
            darkcolor=UITheme.BORDER_SUBTLE,
            lightcolor=UITheme.BORDER_CARD,
            bordercolor=UITheme.BORDER_CARD,
            arrowcolor=UITheme.ACCENT_CYAN,
            selectbackground=UITheme.ACCENT_CYAN,
            selectforeground=UITheme.BG_ROOT,
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", UITheme.BG_INPUT), ("active", UITheme.BG_INPUT)],
            foreground=[("readonly", UITheme.TEXT_WHITE)],
            bordercolor=[("focus", UITheme.ACCENT_CYAN)],
        )

        # TProgressbar cyan styling
        self.style.configure(
            "Horizontal.TProgressbar",
            troughcolor=UITheme.BG_ROOT,
            background=UITheme.ACCENT_CYAN,
            bordercolor=UITheme.BORDER_CARD,
            lightcolor=UITheme.ACCENT_CYAN,
            darkcolor=UITheme.ACCENT_CYAN,
        )

        # Vertical.TScrollbar dark styling
        self.style.configure(
            "Vertical.TScrollbar",
            background=UITheme.BORDER_CARD,
            troughcolor=UITheme.BG_ROOT,
            bordercolor=UITheme.BG_ROOT,
            arrowcolor=UITheme.TEXT_MUTED,
        )
        self.style.map(
            "Vertical.TScrollbar",
            background=[("active", UITheme.BORDER_HOVER)],
        )

    def _init_variables(self) -> None:
        # Reconstruction Variables
        self.var_images_dir = tk.StringVar()
        self.var_output_dir = tk.StringVar()
        self.var_align_quality = tk.StringVar(value="medium")
        self.var_faces = tk.StringVar(value="600000")
        self.var_tex_pages = tk.StringVar(value="16")
        self.var_crop = tk.StringVar(value="gps-square")
        self.var_depth_quality = tk.StringVar(value="medium")
        self.var_depth_filtering = tk.StringVar(value="moderate")

        # Feature Toggles
        self.var_rolling = tk.BooleanVar(value=True)
        self.var_stationary = tk.BooleanVar(value=True)
        self.var_filter_tiepoints = tk.BooleanVar(value=True)
        self.var_close_holes = tk.BooleanVar(value=True)
        self.var_fill_holes = tk.BooleanVar(value=True)
        self.var_ghosting = tk.BooleanVar(value=True)
        self.var_pause_crop = tk.BooleanVar(value=False)
        self.var_rebake = tk.BooleanVar(value=False)
        self.var_restart = tk.BooleanVar(value=False)

        # Standalone Export Variables
        self.var_psx_path = tk.StringVar()
        self.var_export_out = tk.StringVar()
        self.var_exp_format = tk.StringVar(value="tiff")
        self.var_exp_pages = tk.StringVar(value="16")
        self.var_exp_rebake = tk.BooleanVar(value=True)

        self.lbl_photo_status = tk.StringVar(
            value="No folder selected yet. Click 'Browse Folder...' to begin."
        )

    # -----------------------------------------------------------------------
    # Main UI Layout Construction
    # -----------------------------------------------------------------------
    def _build_ui(self) -> None:
        # Top Studio Header
        self._build_header()

        # Pipeline Mode Navigation Tabs
        self._build_mode_bar()

        # Manual Crop Alert Notification Banner (hidden by default)
        self._build_crop_pause_banner()

        # Scrollable Main Workspace
        self._build_scrollable_workspace()

        # Fixed Bottom Action Bar
        self._build_bottom_bar()

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=UITheme.BG_SURFACE, height=48)
        header.pack(fill=tk.X, padx=0, pady=(0, 2))

        left = tk.Frame(header, bg=UITheme.BG_SURFACE)
        left.pack(side=tk.LEFT, padx=16, pady=8)

        lbl_logo = tk.Label(
            left,
            text="⚡ AutoMetashape Studio",
            font=("Segoe UI", 12, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_WHITE,
        )
        lbl_logo.pack(side=tk.LEFT)

        lbl_sub = tk.Label(
            left,
            text=" |  Autonomous Architectural 3D Photogrammetry",
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
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_SURFACE_ALT,
            fg=UITheme.TEXT_MUTED,
            padx=10,
            pady=3,
        )
        self.lbl_metashape_badge.pack(side=tk.RIGHT)

    def _build_mode_bar(self) -> None:
        bar = tk.Frame(self.root, bg=UITheme.BG_ROOT, pady=6)
        bar.pack(fill=tk.X, padx=14)

        self.btn_tab_recon = tk.Button(
            bar,
            text="🚀  3D Reconstruction (Raw Photos ➔ 3D Model)",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.ACCENT_CYAN,
            fg=UITheme.BG_ROOT,
            activebackground=UITheme.ACCENT_CYAN_HOVER,
            activeforeground="white",
            bd=0,
            padx=16,
            pady=6,
            relief=tk.FLAT,
            cursor="hand2",
            command=lambda: self._set_mode("recon"),
        )
        self.btn_tab_recon.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_tab_export = tk.Button(
            bar,
            text="📦  Re-Bake Texture & Export (.psx)",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_MUTED,
            activebackground=UITheme.BG_SURFACE_ALT,
            activeforeground=UITheme.TEXT_WHITE,
            bd=0,
            padx=16,
            pady=6,
            relief=tk.FLAT,
            cursor="hand2",
            command=lambda: self._set_mode("export"),
        )
        self.btn_tab_export.pack(side=tk.LEFT)

    def _build_crop_pause_banner(self) -> None:
        """Prominent alert banner that appears when manual cropping is requested."""
        self.frame_crop_banner = tk.Frame(
            self.root,
            bg=UITheme.ACCENT_AMBER_BG,
            bd=1,
            relief=tk.FLAT,
            padx=14,
            pady=8,
        )
        # Hidden initially; packed when paused for crop

        inner = tk.Frame(self.frame_crop_banner, bg=UITheme.ACCENT_AMBER_BG)
        inner.pack(fill=tk.X)

        lbl_icon = tk.Label(
            inner,
            text="⏸️",
            font=("Segoe UI", 14),
            bg=UITheme.ACCENT_AMBER_BG,
            fg=UITheme.ACCENT_AMBER_TXT,
        )
        lbl_icon.pack(side=tk.LEFT, padx=(0, 10))

        text_block = tk.Frame(inner, bg=UITheme.ACCENT_AMBER_BG)
        text_block.pack(side=tk.LEFT, fill=tk.X, expand=True)

        lbl_heading = tk.Label(
            text_block,
            text="PIPELINE PAUSED FOR MANUAL BOUNDING BOX CROPPING",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.ACCENT_AMBER_BG,
            fg=UITheme.ACCENT_AMBER_TXT,
            anchor=tk.W,
        )
        lbl_heading.pack(fill=tk.X)

        lbl_instruct = tk.Label(
            text_block,
            text="1. Open Metashape Pro  ➜  2. Adjust & save bounding box (Ctrl+S)  ➜  3. Close Metashape  ➜  4. Click Continue below",
            font=("Segoe UI", 8),
            bg=UITheme.ACCENT_AMBER_BG,
            fg=UITheme.TEXT_BODY,
            anchor=tk.W,
        )
        lbl_instruct.pack(fill=tk.X)

        btn_launch = tk.Button(
            inner,
            text="🚀 Open Metashape Now",
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

    def _build_scrollable_workspace(self) -> None:
        canvas = tk.Canvas(self.root, bg=UITheme.BG_ROOT, bd=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.root, orient=tk.VERTICAL, command=canvas.yview)
        self.content_frame = tk.Frame(canvas, bg=UITheme.BG_ROOT)

        self.content_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        win = canvas.create_window((0, 0), window=self.content_frame, anchor="nw")

        def _resize_canvas(event):
            canvas.itemconfig(win, width=event.width)

        canvas.bind("<Configure>", _resize_canvas)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(14, 0), pady=(0, 4))
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 4))

        # --- RECONSTRUCTION WORKFLOW WIDGETS ---
        self.wrapper_recon = tk.Frame(self.content_frame, bg=UITheme.BG_ROOT)
        self.wrapper_recon.pack(fill=tk.X, expand=True)

        # Card 1: Input Photos & Deliverables Folder
        self._build_folder_card(self.wrapper_recon)

        # Card 2: One-Click Presets
        self._build_presets_card(self.wrapper_recon)

        # Card 3: Collapsible Advanced Pipeline Settings
        self._build_advanced_settings_card(self.wrapper_recon)

        # --- STANDALONE EXPORT WIDGETS ---
        self.wrapper_export = tk.Frame(self.content_frame, bg=UITheme.BG_ROOT)
        self._build_export_card(self.wrapper_export)

        # Card 4: Horizontal Pipeline Stepper (Visual Trajectory)
        self._build_stepper_card(self.content_frame)

        # Card 5: Real-time Execution Log & Action Bar
        self._build_log_card(self.content_frame)

    # -----------------------------------------------------------------------
    # Step 1: Input Photos & Output Folder Card
    # -----------------------------------------------------------------------
    def _build_folder_card(self, parent: tk.Widget) -> None:
        card = StudioCard(
            parent,
            title="1. INPUT PHOTOS & OUTPUT FOLDER",
            icon="📁",
            badge="Step 1 of 3",
            accent_color=UITheme.ACCENT_CYAN,
        )
        card.pack(fill=tk.X, pady=(0, 8))

        # Raw Images Row
        row_img = tk.Frame(card.body, bg=UITheme.BG_SURFACE)
        row_img.pack(fill=tk.X, pady=(0, 2))

        lbl_img_title = tk.Label(
            row_img,
            text="Raw Photos Directory:",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_WHITE,
        )
        lbl_img_title.pack(side=tk.LEFT)

        btn_browse_img = tk.Button(
            row_img,
            text="📂 Browse Folder...",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.ACCENT_BLUE,
            fg="white",
            activebackground=UITheme.ACCENT_BLUE_HOVER,
            activeforeground="white",
            bd=0,
            padx=12,
            pady=3,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._browse_images_dir,
        )
        btn_browse_img.pack(side=tk.RIGHT)

        self.var_images_dir.trace_add("write", self._on_images_dir_changed)
        ent_img = tk.Entry(
            card.body,
            textvariable=self.var_images_dir,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_WHITE,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
        )
        ent_img.pack(fill=tk.X, pady=(3, 4))

        # Photo count & size summary pill badge
        stats_frame = tk.Frame(card.body, bg=UITheme.BG_ROOT, padx=8, pady=4)
        stats_frame.pack(fill=tk.X, pady=(0, 8))

        lbl_stats = tk.Label(
            stats_frame,
            textvariable=self.lbl_photo_status,
            font=("Segoe UI", 8),
            bg=UITheme.BG_ROOT,
            fg=UITheme.ACCENT_CYAN,
            anchor=tk.W,
        )
        lbl_stats.pack(fill=tk.X)

        # Output Folder Row
        row_out = tk.Frame(card.body, bg=UITheme.BG_SURFACE)
        row_out.pack(fill=tk.X, pady=(0, 2))

        lbl_out_title = tk.Label(
            row_out,
            text="Deliverables Destination Folder (GLB, OBJ, TIFF, PSX):",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_WHITE,
        )
        lbl_out_title.pack(side=tk.LEFT)

        btn_browse_out = tk.Button(
            row_out,
            text="📂 Change Destination...",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_ALT,
            fg=UITheme.TEXT_WHITE,
            activebackground=UITheme.BORDER_HOVER,
            bd=0,
            padx=10,
            pady=2,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._browse_output_dir,
        )
        btn_browse_out.pack(side=tk.RIGHT)

        ent_out = tk.Entry(
            card.body,
            textvariable=self.var_output_dir,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_WHITE,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
        )
        ent_out.pack(fill=tk.X, pady=(3, 2))

    # -----------------------------------------------------------------------
    # Step 2: Quality Presets Card
    # -----------------------------------------------------------------------
    def _build_presets_card(self, parent: tk.Widget) -> None:
        card = StudioCard(
            parent,
            title="2. CHOOSE WORKFLOW PRESET (ONE-CLICK SETUP)",
            icon="🎛️",
            badge="Step 2 of 3",
            accent_color=UITheme.ACCENT_CYAN,
        )
        card.pack(fill=tk.X, pady=(0, 8))

        row = tk.Frame(card.body, bg=UITheme.BG_SURFACE)
        row.pack(fill=tk.X)

        def _make_preset(
            key: str,
            icon_title: str,
            specs: str,
            desc: str,
            is_recommended: bool = False,
        ):
            outer = tk.Frame(row, bg=UITheme.BORDER_CARD, bd=1, relief=tk.FLAT, cursor="hand2")
            outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=3)

            inner = tk.Frame(outer, bg=UITheme.BG_ROOT, padx=10, pady=8)
            inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

            top = tk.Frame(inner, bg=UITheme.BG_ROOT)
            top.pack(fill=tk.X)

            t = tk.Label(
                top,
                text=icon_title,
                font=("Segoe UI", 9, "bold"),
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_WHITE,
            )
            t.pack(side=tk.LEFT)

            radio_badge = tk.Label(
                top,
                text="○ Select",
                font=("Segoe UI", 7, "bold"),
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_DIM,
            )
            radio_badge.pack(side=tk.RIGHT)

            spec_lbl = tk.Label(
                inner,
                text=specs,
                font=("Segoe UI", 8, "bold"),
                bg=UITheme.BG_ROOT,
                fg=UITheme.ACCENT_CYAN,
                anchor=tk.W,
            )
            spec_lbl.pack(fill=tk.X, pady=(4, 2))

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

            # Store references
            self.preset_cards[key] = {
                "outer": outer,
                "inner": inner,
                "title": t,
                "badge": radio_badge,
                "spec": spec_lbl,
                "desc": d,
                "top": top,
            }

            # Click & hover bindings
            for w in [outer, inner, t, radio_badge, spec_lbl, d, top]:
                w.bind("<Button-1>", lambda e, k=key: self._select_preset(k))
                w.bind("<Enter>", lambda e, k=key: self._on_preset_hover(k, True))
                w.bind("<Leave>", lambda e, k=key: self._on_preset_hover(k, False))

        _make_preset(
            "architectural",
            "🏛️ Architectural Standard",
            "600K Faces • 16-Page 8K • GPS Crop",
            "Production architectural balance with rolling shutter compensation and hole closing (Default).",
            is_recommended=True,
        )
        _make_preset(
            "draft",
            "⚡ Rapid Draft Preview",
            "500K Faces • 4-Page 8K • Fast Low",
            "Fast ~15-minute run for quick alignment, boundary checking, and draft geometry evaluation.",
        )
        _make_preset(
            "heritage",
            "💎 Ultra Heritage Detail",
            "2M Faces • 32-Page 8K • Ray Crop",
            "Maximum photographic fidelity for historic landmarks, high-density stonework, and museums.",
        )

    def _on_preset_hover(self, key: str, is_hover: bool) -> None:
        if key == self.selected_preset_key:
            return
        card = self.preset_cards.get(key)
        if card:
            card["outer"].config(bg=UITheme.BORDER_HOVER if is_hover else UITheme.BORDER_CARD)

    # -----------------------------------------------------------------------
    # Step 3: Collapsible Advanced Pipeline Settings
    # -----------------------------------------------------------------------
    def _build_advanced_settings_card(self, parent: tk.Widget) -> None:
        self.card_adv = StudioCard(
            parent,
            title="3. ADVANCED PIPELINE FINE-TUNING",
            icon="⚙️",
            badge="Optional",
            accent_color=UITheme.ACCENT_CYAN,
        )
        self.card_adv.pack(fill=tk.X, pady=(0, 8))

        # Toggle bar
        bar_toggle = tk.Frame(self.card_adv.body, bg=UITheme.BG_SURFACE)
        bar_toggle.pack(fill=tk.X)

        self.btn_toggle_adv = tk.Button(
            bar_toggle,
            text="▶ Show Advanced Parameters (Alignment, 600K Meshing, Texture Flags)",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.ACCENT_CYAN,
            bd=0,
            padx=4,
            pady=3,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._toggle_advanced,
        )
        self.btn_toggle_adv.pack(side=tk.LEFT)

        # Hidden body frame
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

        # Checkboxes Row 1
        chk_row1 = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_row1.pack(fill=tk.X, pady=(2, 4))

        def _c(t, v, parent=chk_row1):
            return tk.Checkbutton(
                parent,
                text=t,
                variable=v,
                font=("Segoe UI", 8),
                bg=UITheme.BG_SURFACE,
                fg=UITheme.TEXT_WHITE,
                selectcolor=UITheme.BG_ROOT,
                activebackground=UITheme.BG_SURFACE,
                activeforeground=UITheme.TEXT_WHITE,
                bd=0,
            )

        _c("CMOS Rolling Shutter", self.var_rolling).pack(side=tk.LEFT, padx=(0, 10))
        _c("Exclude Stationary Points", self.var_stationary).pack(side=tk.LEFT, padx=(0, 10))
        _c("Tie-Point Filtering", self.var_filter_tiepoints).pack(side=tk.LEFT, padx=(0, 10))
        _c("Close Mesh Holes", self.var_close_holes).pack(side=tk.LEFT, padx=(0, 10))
        _c("Hole Filling", self.var_fill_holes).pack(side=tk.LEFT, padx=(0, 10))
        _c("Ghosting Filter", self.var_ghosting).pack(side=tk.LEFT)

        # Checkboxes Row 2
        chk_row2 = tk.Frame(self.body_adv, bg=UITheme.BG_SURFACE)
        chk_row2.pack(fill=tk.X, pady=(2, 4))

        _c("⏸️ Pause After Crop (Inspect in Metashape Pro)", self.var_pause_crop, parent=chk_row2).pack(side=tk.LEFT, padx=(0, 14))
        _c("Force Restart from Scratch", self.var_restart, parent=chk_row2).pack(side=tk.LEFT)

    def _toggle_advanced(self) -> None:
        self.adv_expanded = not self.adv_expanded
        if self.adv_expanded:
            self.body_adv.pack(fill=tk.X, pady=(4, 2))
            self.btn_toggle_adv.config(text="▼ Hide Advanced Parameters")
        else:
            self.body_adv.pack_forget()
            self.btn_toggle_adv.config(text="▶ Show Advanced Parameters (Alignment, 600K Meshing, Texture Flags)")

    # -----------------------------------------------------------------------
    # Step 4: Standalone Export Card
    # -----------------------------------------------------------------------
    def _build_export_card(self, parent: tk.Widget) -> None:
        card = StudioCard(
            parent,
            title="STANDALONE RE-BAKE & EXPORT (.PSX)",
            icon="📦",
            badge="Export Mode",
            accent_color=UITheme.ACCENT_BLUE,
        )
        card.pack(fill=tk.X, pady=(0, 8))

        # PSX File Row
        row0 = tk.Frame(card.body, bg=UITheme.BG_SURFACE)
        row0.pack(fill=tk.X, pady=(0, 2))

        lbl_psx = tk.Label(
            row0,
            text="Metashape Project File (.psx):",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_WHITE,
        )
        lbl_psx.pack(side=tk.LEFT)

        btn_browse_psx = tk.Button(
            row0,
            text="📂 Browse .psx...",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.ACCENT_BLUE,
            fg="white",
            bd=0,
            padx=12,
            pady=2,
            relief=tk.FLAT,
            command=self._browse_psx_file,
        )
        btn_browse_psx.pack(side=tk.RIGHT)

        self.var_psx_path.trace_add("write", self._on_psx_path_changed)
        ent_psx = tk.Entry(
            card.body,
            textvariable=self.var_psx_path,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_WHITE,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
        )
        ent_psx.pack(fill=tk.X, pady=(2, 6))

        # Destination Row
        row1 = tk.Frame(card.body, bg=UITheme.BG_SURFACE)
        row1.pack(fill=tk.X, pady=(4, 2))

        lbl_dest = tk.Label(
            row1,
            text="Export Destination Folder:",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_WHITE,
        )
        lbl_dest.pack(side=tk.LEFT)

        btn_browse_exp = tk.Button(
            row1,
            text="📂 Change Destination...",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_ALT,
            fg=UITheme.TEXT_WHITE,
            bd=0,
            padx=10,
            pady=2,
            relief=tk.FLAT,
            command=self._browse_export_dir,
        )
        btn_browse_exp.pack(side=tk.RIGHT)

        ent_exp = tk.Entry(
            card.body,
            textvariable=self.var_export_out,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_WHITE,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
        )
        ent_exp.pack(fill=tk.X, pady=(2, 6))

        # Options Row
        row2 = tk.Frame(card.body, bg=UITheme.BG_SURFACE)
        row2.pack(fill=tk.X, pady=4)

        tk.Checkbutton(
            row2,
            text="Re-Bake Texture Atlas",
            variable=self.var_exp_rebake,
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.TEXT_WHITE,
            selectcolor=UITheme.BG_ROOT,
            bd=0,
        ).pack(side=tk.LEFT, padx=(0, 14))

        tk.Label(row2, text="Pages:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(row2, textvariable=self.var_exp_pages, values=["16", "8", "4", "2", "32"], state="readonly", width=6).pack(side=tk.LEFT, padx=(4, 14))

        tk.Label(row2, text="Format:", font=("Segoe UI", 8), bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(row2, textvariable=self.var_exp_format, values=["tiff", "jpeg"], state="readonly", width=8).pack(side=tk.LEFT, padx=4)

    # -----------------------------------------------------------------------
    # Step 5: Stepper & Trajectory
    # -----------------------------------------------------------------------
    def _build_stepper_card(self, parent: tk.Widget) -> None:
        card = StudioCard(
            parent,
            title="PIPELINE PROGRESS (7 STAGES)",
            icon="📊",
            badge="Milestones",
            accent_color=UITheme.ACCENT_CYAN,
        )
        card.pack(fill=tk.X, pady=(0, 8))

        self.stepper = StudioPipelineStepper(card.body)
        self.stepper.pack(fill=tk.X)

    # -----------------------------------------------------------------------
    # Step 6: Console Feed & Deliverables
    # -----------------------------------------------------------------------
    def _build_log_card(self, parent: tk.Widget) -> None:
        card = StudioCard(
            parent,
            title="REAL-TIME EXECUTION LOG",
            icon="💻",
            badge="Console",
            accent_color=UITheme.ACCENT_CYAN,
        )
        card.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # Console ScrolledText
        self.txt_log = ScrolledText(
            card.body,
            wrap=tk.WORD,
            bg=UITheme.BG_CONSOLE,
            fg=UITheme.TEXT_BODY,
            insertbackground="white",
            font=("Consolas", 9),
            bd=0,
            highlightthickness=0,
            height=10,
        )
        self.txt_log.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        # Syntax tags
        self.txt_log.tag_config("stage", foreground=UITheme.ACCENT_CYAN, font=("Consolas", 9, "bold"))
        self.txt_log.tag_config("success", foreground=UITheme.ACCENT_GREEN_TXT, font=("Consolas", 9, "bold"))
        self.txt_log.tag_config("warning", foreground=UITheme.ACCENT_AMBER_TXT)
        self.txt_log.tag_config("error", foreground="#f87171", font=("Consolas", 9, "bold"))

        # Action bar under console
        action_bar = tk.Frame(card.body, bg=UITheme.BG_SURFACE)
        action_bar.pack(fill=tk.X)

        self.btn_open_folder = tk.Button(
            action_bar,
            text="📂 Open Output Folder",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_SURFACE_ALT,
            fg=UITheme.TEXT_WHITE,
            activebackground=UITheme.BORDER_HOVER,
            bd=0,
            padx=12,
            pady=4,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._on_open_output_folder,
        )
        self.btn_open_folder.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_open_metashape = tk.Button(
            action_bar,
            text="🚀 Open in Metashape GUI",
            font=("Segoe UI", 8, "bold"),
            bg="#132a1c",
            fg="#6ee7b7",
            activebackground="#1e462f",
            activeforeground="white",
            bd=0,
            padx=12,
            pady=4,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._on_open_metashape_gui,
        )
        self.btn_open_metashape.pack(side=tk.LEFT, padx=(0, 8))

        btn_copy = tk.Button(
            action_bar,
            text="📋 Copy Log",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_ALT,
            fg=UITheme.TEXT_MUTED,
            activebackground=UITheme.BORDER_HOVER,
            bd=0,
            padx=10,
            pady=4,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._copy_log,
        )
        btn_copy.pack(side=tk.RIGHT)

        btn_clear = tk.Button(
            action_bar,
            text="🗑️ Clear",
            font=("Segoe UI", 8),
            bg=UITheme.BG_SURFACE_ALT,
            fg=UITheme.TEXT_MUTED,
            activebackground=UITheme.BORDER_HOVER,
            bd=0,
            padx=10,
            pady=4,
            relief=tk.FLAT,
            cursor="hand2",
            command=lambda: self.txt_log.delete("1.0", tk.END),
        )
        btn_clear.pack(side=tk.RIGHT, padx=4)

    # -----------------------------------------------------------------------
    # Bottom Action Bar
    # -----------------------------------------------------------------------
    def _build_bottom_bar(self) -> None:
        bar = tk.Frame(self.root, bg=UITheme.BG_SURFACE, height=64)
        bar.pack(fill=tk.X, side=tk.BOTTOM, padx=0, pady=0)

        inner = tk.Frame(bar, bg=UITheme.BG_SURFACE)
        inner.pack(fill=tk.X, padx=16, pady=10)

        self.btn_start = tk.Button(
            inner,
            text="🚀  Start 3D Reconstruction",
            font=("Segoe UI", 11, "bold"),
            bg=UITheme.ACCENT_GREEN,
            fg="white",
            activebackground=UITheme.ACCENT_GREEN_HOVER,
            activeforeground="white",
            bd=0,
            padx=20,
            pady=8,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._on_start_clicked,
        )
        self.btn_start.pack(side=tk.LEFT, padx=(0, 10))

        self.btn_stop = tk.Button(
            inner,
            text="✕  Cancel Process",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE_ALT,
            fg=UITheme.TEXT_MUTED,
            activebackground=UITheme.ACCENT_RED,
            activeforeground="white",
            bd=0,
            padx=14,
            pady=8,
            state=tk.DISABLED,
            relief=tk.FLAT,
            command=self._on_stop_clicked,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 16))

        self.btn_continue_crop = tk.Button(
            inner,
            text="▶️  Continue Reconstruction (Manual Crop Done)",
            font=("Segoe UI", 10, "bold"),
            bg=UITheme.ACCENT_CYAN,
            fg=UITheme.BG_ROOT,
            activebackground=UITheme.ACCENT_CYAN_HOVER,
            activeforeground="white",
            bd=0,
            padx=16,
            pady=8,
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
            text="● Status: Ready to reconstruct",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_SURFACE,
            fg=UITheme.ACCENT_GREEN_TXT,
        )
        self.lbl_status.pack(side=tk.RIGHT)

    # -----------------------------------------------------------------------
    # Event Handlers & Preset Logic
    # -----------------------------------------------------------------------
    def _select_preset(self, key: str) -> None:
        """Visual card selection logic with modern accent styling."""
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
                inner.config(bg=UITheme.BG_CARD_ACTIVE)
                top.config(bg=UITheme.BG_CARD_ACTIVE)
                t.config(bg=UITheme.BG_CARD_ACTIVE, fg=UITheme.TEXT_WHITE)
                b.config(text="● SELECTED", bg=UITheme.ACCENT_CYAN, fg=UITheme.BG_ROOT)
                s.config(bg=UITheme.BG_CARD_ACTIVE, fg=UITheme.ACCENT_CYAN)
                d.config(bg=UITheme.BG_CARD_ACTIVE, fg=UITheme.TEXT_BODY)
            else:
                outer.config(bg=UITheme.BORDER_CARD)
                inner.config(bg=UITheme.BG_ROOT)
                top.config(bg=UITheme.BG_ROOT)
                t.config(bg=UITheme.BG_ROOT, fg=UITheme.TEXT_WHITE)
                b.config(text="○ Select", bg=UITheme.BG_ROOT, fg=UITheme.TEXT_DIM)
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
            self.btn_tab_recon.config(bg=UITheme.ACCENT_CYAN, fg=UITheme.BG_ROOT)
            self.btn_tab_export.config(bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED)
            self.wrapper_export.pack_forget()
            self.wrapper_recon.pack(fill=tk.X, expand=True)
            self.btn_start.config(text="🚀  Start 3D Reconstruction", bg=UITheme.ACCENT_GREEN)
        else:
            self.btn_tab_export.config(bg=UITheme.ACCENT_CYAN, fg=UITheme.BG_ROOT)
            self.btn_tab_recon.config(bg=UITheme.BG_SURFACE, fg=UITheme.TEXT_MUTED)
            self.wrapper_recon.pack_forget()
            self.wrapper_export.pack(fill=tk.X, expand=True)
            self.btn_start.config(text="📦  Re-Bake Texture & Export Assets", bg=UITheme.ACCENT_BLUE)

    def _check_metashape(self) -> None:
        if self.metashape_exe:
            self.lbl_metashape_badge.config(
                text="● Metashape Pro Connected",
                bg=UITheme.ACCENT_GREEN_BG,
                fg=UITheme.ACCENT_GREEN_TXT,
            )
            self._log(f"[AutoMetashape Studio] System initialized with Metashape Pro: {self.metashape_exe}", "stage")
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
            self.lbl_photo_status.set("No folder selected yet. Click 'Browse Folder...' to begin.")
            return

        default_out = os.path.join(folder, "model")
        cur_out = self.var_output_dir.get().strip()
        if not cur_out or cur_out.endswith("model"):
            self.var_output_dir.set(default_out)

        self.lbl_photo_status.set("🔍 Scanning directory and subfolders for supported photos...")

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
                    f"✓ Found {count:,} photos ({size_str}) • Formats: {ext_str} • Ready for reconstruction"
                )
            else:
                self.lbl_photo_status.set("⚠️ No supported photos (.jpg, .dng, .tif, etc.) found in folder!")

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
        self.btn_start.config(state=tk.DISABLED, bg=UITheme.BG_SURFACE_ALT)
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
            text="⏸️ Paused: Open Metashape Pro to Crop, then click Continue",
            fg=UITheme.ACCENT_AMBER_TXT,
        )
        self.progress_bar.stop()

        # Reveal alert banner & continue button
        self.frame_crop_banner.pack(fill=tk.X, padx=14, pady=(0, 6), before=self.wrapper_recon.master)
        self.btn_continue_crop.pack(side=tk.LEFT, padx=(0, 10), before=self.btn_stop)

        self._log("\n" + "═" * 70, "warning")
        self._log("⏸️  PIPELINE PAUSED: MANUAL CROPPING IN AGISOFT METASHAPE PRO", "warning")
        self._log("1. Click '🚀 Open in Metashape GUI' above to open your project in Metashape Pro.", "stage")
        self._log("2. In Metashape, use 'Resize Region' / 'Rotate Region' tools to crop the building.", "stage")
        self._log("3. Press Ctrl+S in Metashape Pro to save, then CLOSE Metashape Pro.", "stage")
        self._log("4. Click '▶️ Continue Reconstruction (Manual Crop Done)' below to resume 600K meshing.\n" + "═" * 70, "success")

        # Audio/visual alert
        try:
            self.root.bell()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after_idle(lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

        # Interactive popup notification
        folder = self._get_active_output_dir()
        psx = os.path.join(folder, "building_reconstruction.psx")

        open_now = messagebox.askyesno(
            "Manual Bounding Box in Metashape Pro",
            "Camera alignment completed and saved to:\n"
            f"{psx}\n\n"
            "Would you like to open this project in Agisoft Metashape Pro now to adjust your bounding box?\n\n"
            "• Click [Yes] to launch Metashape Pro automatically.\n"
            "• In Metashape, adjust the bounding box, press Ctrl+S to save, then CLOSE Metashape Pro.\n"
            "• Return here and click '▶️ Continue Reconstruction' to resume.",
            icon="question",
        )
        if open_now:
            self._on_open_metashape_gui()

    def _on_continue_crop_clicked(self) -> None:
        self.btn_continue_crop.pack_forget()
        self.frame_crop_banner.pack_forget()
        self.progress_bar.start(10)
        self.lbl_status.config(
            text="● Resuming Reconstruction with Manual Box...",
            fg=UITheme.ACCENT_CYAN,
        )
        self.stepper.set_active(3, "Resuming MVS Depth Maps...")
        self._log("[USER] Sent resume signal. Continuing pipeline with manual bounding box...", "stage")

        # 1. Send newline to stdin
        if self.current_process and self.current_process.stdin:
            try:
                self.current_process.stdin.write("\n")
                self.current_process.stdin.flush()
            except Exception as e:
                self._log(f"[Warning] stdin write: {e}", "warning")

        # 2. Write sentinel file as resilient fallback
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
            bg=UITheme.ACCENT_GREEN if self.active_mode == "recon" else UITheme.ACCENT_BLUE,
        )
        self.btn_stop.config(state=tk.DISABLED, bg=UITheme.BG_SURFACE_ALT, fg=UITheme.TEXT_MUTED)

        if user_cancelled:
            self.lbl_status.config(text="● Status: Cancelled by user", fg=UITheme.ACCENT_AMBER_TXT)
        elif success:
            self.stepper.set_all_done()
            self.lbl_status.config(text="✓ Status: Completed Successfully!", fg=UITheme.ACCENT_GREEN_TXT)
            messagebox.showinfo(
                "Completed",
                "Reconstruction finished successfully!\nDeliverables are saved in your output folder.",
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
