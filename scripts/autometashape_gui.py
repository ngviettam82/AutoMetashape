# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape Graphical User Interface (Studio GUI).

Next-generation, agent-inspired desktop workspace for autonomous photogrammetry
3D reconstruction and export pipelines using Agisoft Metashape Pro.

Designed according to open-source agent GUI principles (OpenHands, AutoGen Studio,
ChatDev):
- Visual Pipeline Stepper with real-time milestone telemetry
- One-click Workflow Presets (Architectural Standard, Rapid Draft, Ultra Heritage)
- Smart Dataset & Deliverables Inspector with direct asset actions
- High-DPI dark theme with responsive dual-panel master/detail layout
- Non-blocking multi-threaded streaming console with syntax color-coding
"""

from __future__ import annotations

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

# High-DPI scaling on Windows
if sys.platform == "win32":
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Design System & Palette (Agent Dark Theme)
# ---------------------------------------------------------------------------
class Palette:
    BG_ROOT = "#0d1117"
    BG_SURFACE = "#161b22"
    BG_ELEVATED = "#21262d"
    BG_INPUT = "#0d1117"
    BORDER_SUBTLE = "#30363d"
    BORDER_FOCUS = "#58a6ff"

    TEXT_PRIMARY = "#f0f6fc"
    TEXT_SECONDARY = "#8b949e"
    TEXT_MUTED = "#6e7681"

    ACCENT_PRIMARY = "#238636"
    ACCENT_PRIMARY_HOVER = "#2ea043"
    ACCENT_BLUE = "#1f6feb"
    ACCENT_BLUE_HOVER = "#388bfd"
    ACCENT_RED = "#da3633"
    ACCENT_RED_HOVER = "#f85149"
    ACCENT_AMBER = "#d29922"
    ACCENT_PURPLE = "#8957e5"
    ACCENT_CYAN = "#39c5cf"

    CONSOLE_BG = "#0a0c10"
    CONSOLE_FG = "#c9d1d9"


# Supported image extensions for folder scanning
SUPPORTED_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".tif", ".tiff", ".bmp", ".webp",
    ".jp2", ".jxl", ".exr", ".dng", ".cr2", ".cr3", ".nef", ".arw",
    ".iiq", ".3fr", ".rw2", ".raf", ".raw", ".seq", ".ara",
}

DEFAULT_METASHAPE_PATHS = [
    r"C:\Program Files\Agisoft\Metashape Pro\metashape.exe",
    r"C:\Program Files\Agisoft\PhotoScan Pro\photoscan.exe",
]


def find_metashape_executable() -> str | None:
    """Locate Metashape Professional executable on the system."""
    for path in DEFAULT_METASHAPE_PATHS:
        if os.path.isfile(path):
            return path
    which_path = shutil.which("metashape")
    if which_path and os.path.isfile(which_path):
        return which_path
    return None


# ---------------------------------------------------------------------------
# Visual Stepper Widget (Agent Trajectory Timeline)
# ---------------------------------------------------------------------------
class PipelineStepper(tk.Frame):
    """Visual milestone stepper showing real-time agent progression."""

    STAGES = [
        ("1. Ingestion & Quality", "Scan photos & auto-filter blur < 0.5"),
        ("2. GPU Matching & Alignment", "40K keypoints, 25K tiepoints, rolling shutter"),
        ("3. Spatial Bounding Crop", "Auto-crop camera GPS horizontal footprint"),
        ("4. MVS Dense Depth Maps", "Multi-view stereopsis depth calculation"),
        ("5. Arbitrary Surface Meshing", "Generate 1M face mesh + hole closing"),
        ("6. 16-Page 8K Texture Atlas", "Mosaic photographic blending, fill holes"),
        ("7. Asset Packaging & Export", "Export binary GLB, OBJ, 16x TIFF, PSX"),
    ]

    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(parent, bg=Palette.BG_SURFACE, **kwargs)
        self.stage_cards: list[dict] = []
        self._build_stepper()

    def _build_stepper(self) -> None:
        for idx, (title, subtitle) in enumerate(self.STAGES):
            card = tk.Frame(self, bg=Palette.BG_SURFACE, highlightbackground=Palette.BORDER_SUBTLE, highlightthickness=1)
            card.pack(fill=tk.X, padx=4, pady=2)

            left_box = tk.Frame(card, bg=Palette.BG_SURFACE)
            left_box.pack(side=tk.LEFT, fill=tk.Y, padx=(6, 8), pady=4)

            lbl_badge = tk.Label(
                left_box,
                text="○",
                font=("Segoe UI", 11, "bold"),
                bg=Palette.BG_SURFACE,
                fg=Palette.TEXT_MUTED,
                width=2,
            )
            lbl_badge.pack(anchor=tk.CENTER)

            text_box = tk.Frame(card, bg=Palette.BG_SURFACE)
            text_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=3)

            lbl_title = tk.Label(
                text_box,
                text=title,
                font=("Segoe UI", 9, "bold"),
                bg=Palette.BG_SURFACE,
                fg=Palette.TEXT_PRIMARY,
                anchor=tk.W,
            )
            lbl_title.pack(fill=tk.X)

            lbl_sub = tk.Label(
                text_box,
                text=subtitle,
                font=("Segoe UI", 8),
                bg=Palette.BG_SURFACE,
                fg=Palette.TEXT_MUTED,
                anchor=tk.W,
            )
            lbl_sub.pack(fill=tk.X)

            lbl_status = tk.Label(
                card,
                text="Pending",
                font=("Segoe UI", 8, "bold"),
                bg=Palette.BG_SURFACE,
                fg=Palette.TEXT_MUTED,
                padx=8,
            )
            lbl_status.pack(side=tk.RIGHT, pady=4)

            self.stage_cards.append({
                "frame": card,
                "badge": lbl_badge,
                "title": lbl_title,
                "sub": lbl_sub,
                "status": lbl_status,
                "start_time": 0.0,
            })

    def reset(self) -> None:
        """Reset all stages to Pending state."""
        for card in self.stage_cards:
            card["frame"].config(highlightbackground=Palette.BORDER_SUBTLE, bg=Palette.BG_SURFACE)
            card["badge"].config(text="○", fg=Palette.TEXT_MUTED, bg=Palette.BG_SURFACE)
            card["title"].config(fg=Palette.TEXT_PRIMARY, bg=Palette.BG_SURFACE)
            card["sub"].config(bg=Palette.BG_SURFACE)
            card["status"].config(text="Pending", fg=Palette.TEXT_MUTED, bg=Palette.BG_SURFACE)
            card["start_time"] = 0.0

    def set_running(self, stage_idx: int) -> None:
        """Mark stage as actively running."""
        # Mark previous stages as completed if not already done
        for i in range(stage_idx):
            self.set_done(i)

        if 0 <= stage_idx < len(self.stage_cards):
            card = self.stage_cards[stage_idx]
            card["start_time"] = time.time()
            card["frame"].config(highlightbackground=Palette.ACCENT_BLUE, bg="#121d2f")
            card["badge"].config(text="▶", fg=Palette.ACCENT_CYAN, bg="#121d2f")
            card["title"].config(fg="#58a6ff", bg="#121d2f")
            card["sub"].config(bg="#121d2f")
            card["status"].config(text="Running...", fg=Palette.ACCENT_CYAN, bg="#121d2f")

    def set_done(self, stage_idx: int) -> None:
        """Mark stage as successfully finished."""
        if 0 <= stage_idx < len(self.stage_cards):
            card = self.stage_cards[stage_idx]
            elapsed_str = ""
            if card["start_time"] > 0:
                el = (time.time() - card["start_time"]) / 60.0
                elapsed_str = f" ({el:.1f}m)"

            card["frame"].config(highlightbackground="#1f4429", bg="#0e1f14")
            card["badge"].config(text="✓", fg="#3fb950", bg="#0e1f14")
            card["title"].config(fg=Palette.TEXT_PRIMARY, bg="#0e1f14")
            card["sub"].config(bg="#0e1f14")
            card["status"].config(text=f"Done{elapsed_str}", fg="#3fb950", bg="#0e1f14")

    def set_error(self, stage_idx: int) -> None:
        """Mark stage as failed."""
        if 0 <= stage_idx < len(self.stage_cards):
            card = self.stage_cards[stage_idx]
            card["frame"].config(highlightbackground=Palette.ACCENT_RED, bg="#2a1215")
            card["badge"].config(text="✕", fg=Palette.ACCENT_RED_HOVER, bg="#2a1215")
            card["status"].config(text="Failed", fg=Palette.ACCENT_RED_HOVER, bg="#2a1215")


# ---------------------------------------------------------------------------
# Main AutoMetashape Studio GUI
# ---------------------------------------------------------------------------
class AutoMetashapeStudio:
    """Advanced Agent-Style Photogrammetry Studio GUI."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AutoMetashape Studio - Autonomous 3D Photogrammetry")
        self.root.geometry("1180x850")
        self.root.minsize(1020, 720)
        self.root.configure(bg=Palette.BG_ROOT)

        # File paths
        self.repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.recon_script = os.path.join(self.repo_dir, "scripts", "metashape_reconstruction.py")
        self.export_script = os.path.join(self.repo_dir, "scripts", "metashape_export.py")

        # Hardware & Executable
        self.metashape_exe = find_metashape_executable()
        self.current_process: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.is_running = False
        self.start_time: float = 0.0
        self.active_mode = "recon"  # "recon" or "export"

        self._init_variables()
        self._build_layout()
        self._apply_preset("architectural")
        self._check_metashape()

        # Start background event loops
        self.root.after(100, self._process_log_queue)
        self.root.after(1000, self._poll_deliverables)

    def _init_variables(self) -> None:
        # Reconstruction Variables
        self.var_images_dir = tk.StringVar()
        self.var_output_dir = tk.StringVar()
        self.var_align_quality = tk.StringVar(value="medium")
        self.var_faces = tk.StringVar(value="1000000")
        self.var_tex_pages = tk.StringVar(value="16")
        self.var_crop = tk.StringVar(value="gps-square")
        self.var_depth_quality = tk.StringVar(value="medium")
        self.var_depth_filtering = tk.StringVar(value="moderate")

        self.var_rolling = tk.BooleanVar(value=True)
        self.var_stationary = tk.BooleanVar(value=True)
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

        # Dataset telemetry
        self.lbl_dataset_stats_text = tk.StringVar(value="Select an image folder to scan contents.")

    # -----------------------------------------------------------------------
    # Top-Level Layout Construction
    # -----------------------------------------------------------------------
    def _build_layout(self) -> None:
        # 1. Header Bar
        self._build_header()

        # 2. Main Workspace Split (Left: Control & Stepper | Right: Feed & Artifacts)
        main_paned = tk.PanedWindow(
            self.root,
            orient=tk.HORIZONTAL,
            bg=Palette.BORDER_SUBTLE,
            bd=0,
            sashwidth=4,
            sashpad=0,
        )
        main_paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))

        # Left Column: Configuration & Stepper
        left_container = tk.Frame(main_paned, bg=Palette.BG_ROOT, width=540)
        left_container.pack_propagate(False)
        main_paned.add(left_container, minsize=460)

        # Scrollable Left Frame
        canvas_left = tk.Canvas(left_container, bg=Palette.BG_ROOT, bd=0, highlightthickness=0)
        scrollbar_left = ttk.Scrollbar(left_container, orient=tk.VERTICAL, command=canvas_left.yview)
        self.scroll_frame = tk.Frame(canvas_left, bg=Palette.BG_ROOT)

        self.scroll_frame.bind(
            "<Configure>",
            lambda e: canvas_left.configure(scrollregion=canvas_left.bbox("all")),
        )
        canvas_window = canvas_left.create_window((0, 0), window=self.scroll_frame, anchor="nw")

        def _on_canvas_configure(event):
            canvas_left.itemconfig(canvas_window, width=event.width)

        canvas_left.bind("<Configure>", _on_canvas_configure)
        canvas_left.configure(yscrollcommand=scrollbar_left.set)

        canvas_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar_left.pack(side=tk.RIGHT, fill=tk.Y)

        self._build_left_panel(self.scroll_frame)

        # Right Column: Console Feed & Deliverables Inspector
        right_container = tk.Frame(main_paned, bg=Palette.BG_ROOT)
        main_paned.add(right_container, minsize=480)
        self._build_right_panel(right_container)

        # 3. Bottom Execution Bar
        self._build_bottom_bar()

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=Palette.BG_SURFACE, height=54)
        header.pack(fill=tk.X, padx=0, pady=(0, 6))

        left_box = tk.Frame(header, bg=Palette.BG_SURFACE)
        left_box.pack(side=tk.LEFT, padx=16, pady=8)

        logo = tk.Label(
            left_box,
            text="⚡ AutoMetashape",
            font=("Segoe UI", 13, "bold"),
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_PRIMARY,
        )
        logo.pack(side=tk.LEFT)

        pill_studio = tk.Label(
            left_box,
            text="STUDIO",
            font=("Segoe UI", 8, "bold"),
            bg=Palette.ACCENT_BLUE,
            fg="white",
            padx=6,
            pady=1,
        )
        pill_studio.pack(side=tk.LEFT, padx=(8, 0))

        sub = tk.Label(
            left_box,
            text="| Autonomous Architectural 3D Photogrammetry",
            font=("Segoe UI", 9),
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_SECONDARY,
        )
        sub.pack(side=tk.LEFT, padx=(8, 0))

        # Right Hardware Pills
        right_box = tk.Frame(header, bg=Palette.BG_SURFACE)
        right_box.pack(side=tk.RIGHT, padx=16, pady=10)

        self.lbl_metashape_status = tk.Label(
            right_box,
            text="Checking Metashape...",
            font=("Segoe UI", 8, "bold"),
            bg=Palette.BG_ELEVATED,
            fg=Palette.TEXT_MUTED,
            padx=8,
            pady=3,
        )
        self.lbl_metashape_status.pack(side=tk.RIGHT, padx=(6, 0))

        pill_gpu = tk.Label(
            right_box,
            text="NVIDIA CUDA Accelerated",
            font=("Segoe UI", 8, "bold"),
            bg="#163321",
            fg="#3fb950",
            padx=8,
            pady=3,
        )
        pill_gpu.pack(side=tk.RIGHT)

    # -----------------------------------------------------------------------
    # Left Panel: Workflow Modes, Presets, Controls & Stepper
    # -----------------------------------------------------------------------
    def _build_left_panel(self, parent: tk.Widget) -> None:
        pad_opts = {"fill": tk.X, "padx": 6, "pady": 4}

        # 1. Mode Pill Switcher
        mode_box = tk.Frame(parent, bg=Palette.BG_ROOT)
        mode_box.pack(**pad_opts)

        self.btn_mode_recon = tk.Button(
            mode_box,
            text="🚀 3D Reconstruction Pipeline",
            font=("Segoe UI", 9, "bold"),
            bg=Palette.ACCENT_BLUE,
            fg="white",
            activebackground=Palette.ACCENT_BLUE_HOVER,
            activeforeground="white",
            bd=0,
            padx=12,
            pady=6,
            relief=tk.FLAT,
            command=lambda: self._set_mode("recon"),
        )
        self.btn_mode_recon.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        self.btn_mode_export = tk.Button(
            mode_box,
            text="📦 Standalone Export & Re-Bake (.psx)",
            font=("Segoe UI", 9, "bold"),
            bg=Palette.BG_ELEVATED,
            fg=Palette.TEXT_SECONDARY,
            activebackground=Palette.BORDER_SUBTLE,
            activeforeground=Palette.TEXT_PRIMARY,
            bd=0,
            padx=12,
            pady=6,
            relief=tk.FLAT,
            command=lambda: self._set_mode("export"),
        )
        self.btn_mode_export.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))

        # Container for Reconstruction Controls
        self.frame_recon_container = tk.Frame(parent, bg=Palette.BG_ROOT)
        self.frame_recon_container.pack(fill=tk.BOTH, expand=True)

        # 2. Workflow Presets Card
        self._build_presets_card(self.frame_recon_container)

        # 3. Directory Picker Card
        self._build_dirs_card(self.frame_recon_container)

        # 4. Parameters Card
        self._build_parameters_card(self.frame_recon_container)

        # Container for Export Controls
        self.frame_export_container = tk.Frame(parent, bg=Palette.BG_ROOT)
        self._build_export_controls(self.frame_export_container)

        # 5. Visual Pipeline Stepper (Agent Trajectory)
        stepper_wrapper = tk.LabelFrame(
            parent,
            text=" AGENT TRAJECTORY (PIPELINE STEPPER) ",
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_SECONDARY,
            font=("Segoe UI", 8, "bold"),
            bd=1,
            relief=tk.GROOVE,
            padx=6,
            pady=6,
        )
        stepper_wrapper.pack(fill=tk.X, padx=6, pady=(6, 8))

        self.stepper = PipelineStepper(stepper_wrapper)
        self.stepper.pack(fill=tk.X)

    def _build_presets_card(self, parent: tk.Widget) -> None:
        card = tk.LabelFrame(
            parent,
            text=" WORKFLOW PRESETS ",
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_SECONDARY,
            font=("Segoe UI", 8, "bold"),
            bd=1,
            relief=tk.GROOVE,
            padx=8,
            pady=6,
        )
        card.pack(fill=tk.X, padx=6, pady=4)

        preset_row = tk.Frame(card, bg=Palette.BG_SURFACE)
        preset_row.pack(fill=tk.X)

        self.preset_btns: dict[str, tk.Button] = {}

        p_arch = tk.Button(
            preset_row,
            text="🏛️ Architectural (16P 8K)",
            font=("Segoe UI", 8, "bold"),
            bg=Palette.BG_ELEVATED,
            fg=Palette.TEXT_PRIMARY,
            bd=0,
            padx=8,
            pady=5,
            relief=tk.FLAT,
            command=lambda: self._apply_preset("architectural"),
        )
        p_arch.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.preset_btns["architectural"] = p_arch

        p_draft = tk.Button(
            preset_row,
            text="⚡ Rapid Draft",
            font=("Segoe UI", 8, "bold"),
            bg=Palette.BG_ELEVATED,
            fg=Palette.TEXT_PRIMARY,
            bd=0,
            padx=8,
            pady=5,
            relief=tk.FLAT,
            command=lambda: self._apply_preset("draft"),
        )
        p_draft.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.preset_btns["draft"] = p_draft

        p_heritage = tk.Button(
            preset_row,
            text="💎 Ultra Heritage",
            font=("Segoe UI", 8, "bold"),
            bg=Palette.BG_ELEVATED,
            fg=Palette.TEXT_PRIMARY,
            bd=0,
            padx=8,
            pady=5,
            relief=tk.FLAT,
            command=lambda: self._apply_preset("heritage"),
        )
        p_heritage.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.preset_btns["heritage"] = p_heritage

        self.lbl_preset_desc = tk.Label(
            card,
            text="",
            font=("Segoe UI", 8),
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_MUTED,
            justify=tk.LEFT,
            anchor=tk.W,
            wraplength=480,
        )
        self.lbl_preset_desc.pack(fill=tk.X, pady=(4, 0))

    def _build_dirs_card(self, parent: tk.Widget) -> None:
        card = tk.LabelFrame(
            parent,
            text=" DATASET & DESTINATION PATHS ",
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_SECONDARY,
            font=("Segoe UI", 8, "bold"),
            bd=1,
            relief=tk.GROOVE,
            padx=8,
            pady=6,
        )
        card.pack(fill=tk.X, padx=6, pady=4)

        # Raw Images Directory
        row0 = tk.Frame(card, bg=Palette.BG_SURFACE)
        row0.pack(fill=tk.X, pady=2)
        tk.Label(row0, text="Raw Images Directory:", font=("Segoe UI", 8, "bold"), bg=Palette.BG_SURFACE, fg=Palette.TEXT_PRIMARY).pack(side=tk.LEFT)

        btn_browse_img = tk.Button(
            row0,
            text="📂 Browse Folder...",
            font=("Segoe UI", 8, "bold"),
            bg=Palette.BG_ELEVATED,
            fg=Palette.TEXT_PRIMARY,
            bd=0,
            padx=8,
            pady=2,
            relief=tk.FLAT,
            command=self._browse_images_dir,
        )
        btn_browse_img.pack(side=tk.RIGHT)

        self.var_images_dir.trace_add("write", self._on_images_dir_changed)
        ent_img = tk.Entry(
            card,
            textvariable=self.var_images_dir,
            font=("Segoe UI", 9),
            bg=Palette.BG_INPUT,
            fg=Palette.TEXT_PRIMARY,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
        )
        ent_img.pack(fill=tk.X, pady=(2, 4))

        # Dataset Telemetry
        lbl_stats = tk.Label(
            card,
            textvariable=self.lbl_dataset_stats_text,
            font=("Segoe UI", 8),
            bg=Palette.BG_SURFACE,
            fg=Palette.ACCENT_CYAN,
            anchor=tk.W,
        )
        lbl_stats.pack(fill=tk.X, pady=(0, 4))

        # Output Directory
        row1 = tk.Frame(card, bg=Palette.BG_SURFACE)
        row1.pack(fill=tk.X, pady=2)
        tk.Label(row1, text="Deliverables Output Folder:", font=("Segoe UI", 8, "bold"), bg=Palette.BG_SURFACE, fg=Palette.TEXT_PRIMARY).pack(side=tk.LEFT)

        btn_browse_out = tk.Button(
            row1,
            text="📂 Browse...",
            font=("Segoe UI", 8, "bold"),
            bg=Palette.BG_ELEVATED,
            fg=Palette.TEXT_PRIMARY,
            bd=0,
            padx=8,
            pady=2,
            relief=tk.FLAT,
            command=self._browse_output_dir,
        )
        btn_browse_out.pack(side=tk.RIGHT)

        ent_out = tk.Entry(
            card,
            textvariable=self.var_output_dir,
            font=("Segoe UI", 9),
            bg=Palette.BG_INPUT,
            fg=Palette.TEXT_PRIMARY,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
        )
        ent_out.pack(fill=tk.X, pady=(2, 2))

    def _build_parameters_card(self, parent: tk.Widget) -> None:
        card = tk.LabelFrame(
            parent,
            text=" PIPELINE CONFIGURATION ",
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_SECONDARY,
            font=("Segoe UI", 8, "bold"),
            bd=1,
            relief=tk.GROOVE,
            padx=8,
            pady=6,
        )
        card.pack(fill=tk.X, padx=6, pady=4)

        grid = tk.Frame(card, bg=Palette.BG_SURFACE)
        grid.pack(fill=tk.X)

        # Row 1: Align Quality, Target Faces, Texture Pages
        tk.Label(grid, text="Align Quality:", font=("Segoe UI", 8), bg=Palette.BG_SURFACE, fg=Palette.TEXT_SECONDARY).grid(row=0, column=0, sticky=tk.W, pady=3)
        cbo_align = ttk.Combobox(grid, textvariable=self.var_align_quality, values=["medium", "high", "highest", "low"], state="readonly", width=10)
        cbo_align.grid(row=0, column=1, sticky=tk.W, padx=(4, 12), pady=3)

        tk.Label(grid, text="Target Faces:", font=("Segoe UI", 8), bg=Palette.BG_SURFACE, fg=Palette.TEXT_SECONDARY).grid(row=0, column=2, sticky=tk.W, pady=3)
        cbo_faces = ttk.Combobox(grid, textvariable=self.var_faces, values=["1000000", "500000", "2000000", "3000000"], width=10)
        cbo_faces.grid(row=0, column=3, sticky=tk.W, padx=(4, 12), pady=3)

        tk.Label(grid, text="Texture Pages:", font=("Segoe UI", 8), bg=Palette.BG_SURFACE, fg=Palette.TEXT_SECONDARY).grid(row=0, column=4, sticky=tk.W, pady=3)
        cbo_pages = ttk.Combobox(grid, textvariable=self.var_tex_pages, values=["16", "8", "4", "2", "32"], state="readonly", width=8)
        cbo_pages.grid(row=0, column=5, sticky=tk.W, padx=(4, 0), pady=3)

        # Row 2: Crop Method, Depth Quality, Depth Filtering
        tk.Label(grid, text="Auto-Crop:", font=("Segoe UI", 8), bg=Palette.BG_SURFACE, fg=Palette.TEXT_SECONDARY).grid(row=1, column=0, sticky=tk.W, pady=3)
        cbo_crop = ttk.Combobox(grid, textvariable=self.var_crop, values=["gps-square", "structure", "none"], state="readonly", width=10)
        cbo_crop.grid(row=1, column=1, sticky=tk.W, padx=(4, 12), pady=3)

        tk.Label(grid, text="Depth Quality:", font=("Segoe UI", 8), bg=Palette.BG_SURFACE, fg=Palette.TEXT_SECONDARY).grid(row=1, column=2, sticky=tk.W, pady=3)
        cbo_depth = ttk.Combobox(grid, textvariable=self.var_depth_quality, values=["medium", "high", "ultra", "low"], state="readonly", width=10)
        cbo_depth.grid(row=1, column=3, sticky=tk.W, padx=(4, 12), pady=3)

        tk.Label(grid, text="Filtering:", font=("Segoe UI", 8), bg=Palette.BG_SURFACE, fg=Palette.TEXT_SECONDARY).grid(row=1, column=4, sticky=tk.W, pady=3)
        cbo_filter = ttk.Combobox(grid, textvariable=self.var_depth_filtering, values=["moderate", "mild", "aggressive"], state="readonly", width=8)
        cbo_filter.grid(row=1, column=5, sticky=tk.W, padx=(4, 0), pady=3)

        # Checkboxes
        chk_box = tk.Frame(card, bg=Palette.BG_SURFACE)
        chk_box.pack(fill=tk.X, pady=(6, 2))

        def _chk(parent_w, text, var):
            c = tk.Checkbutton(
                parent_w,
                text=text,
                variable=var,
                font=("Segoe UI", 8),
                bg=Palette.BG_SURFACE,
                fg=Palette.TEXT_PRIMARY,
                selectcolor=Palette.BG_ELEVATED,
                activebackground=Palette.BG_SURFACE,
                activeforeground=Palette.TEXT_PRIMARY,
                highlightthickness=0,
                bd=0,
            )
            return c

        row_c1 = tk.Frame(chk_box, bg=Palette.BG_SURFACE)
        row_c1.pack(fill=tk.X)
        _chk(row_c1, "CMOS Rolling Shutter", self.var_rolling).pack(side=tk.LEFT, padx=(0, 10))
        _chk(row_c1, "Exclude Stationary Points", self.var_stationary).pack(side=tk.LEFT, padx=(0, 10))
        _chk(row_c1, "Close Mesh Holes (L30)", self.var_close_holes).pack(side=tk.LEFT)

        row_c2 = tk.Frame(chk_box, bg=Palette.BG_SURFACE)
        row_c2.pack(fill=tk.X, pady=(2, 0))
        _chk(row_c2, "Texture Hole Filling", self.var_fill_holes).pack(side=tk.LEFT, padx=(0, 10))
        _chk(row_c2, "Texture Ghosting Filter", self.var_ghosting).pack(side=tk.LEFT, padx=(0, 10))
        _chk(row_c2, "Pause for GUI Crop Check", self.var_pause_crop).pack(side=tk.LEFT)

        row_c3 = tk.Frame(chk_box, bg=Palette.BG_SURFACE)
        row_c3.pack(fill=tk.X, pady=(2, 0))
        _chk(row_c3, "Force Re-Bake Texture", self.var_rebake).pack(side=tk.LEFT, padx=(0, 10))
        _chk(row_c3, "Force Restart Scratch", self.var_restart).pack(side=tk.LEFT)

    def _build_export_controls(self, parent: tk.Widget) -> None:
        card = tk.LabelFrame(
            parent,
            text=" STANDALONE RE-BAKE & EXPORT (.PSX) ",
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_SECONDARY,
            font=("Segoe UI", 8, "bold"),
            bd=1,
            relief=tk.GROOVE,
            padx=8,
            pady=8,
        )
        card.pack(fill=tk.X, padx=6, pady=4)

        # PSX File
        row0 = tk.Frame(card, bg=Palette.BG_SURFACE)
        row0.pack(fill=tk.X, pady=2)
        tk.Label(row0, text="Metashape Project File (.psx):", font=("Segoe UI", 8, "bold"), bg=Palette.BG_SURFACE, fg=Palette.TEXT_PRIMARY).pack(side=tk.LEFT)

        btn_browse_psx = tk.Button(
            row0,
            text="📂 Browse .psx...",
            font=("Segoe UI", 8, "bold"),
            bg=Palette.BG_ELEVATED,
            fg=Palette.TEXT_PRIMARY,
            bd=0,
            padx=8,
            pady=2,
            relief=tk.FLAT,
            command=self._browse_psx_file,
        )
        btn_browse_psx.pack(side=tk.RIGHT)

        self.var_psx_path.trace_add("write", self._on_psx_path_changed)
        ent_psx = tk.Entry(
            card,
            textvariable=self.var_psx_path,
            font=("Segoe UI", 9),
            bg=Palette.BG_INPUT,
            fg=Palette.TEXT_PRIMARY,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
        )
        ent_psx.pack(fill=tk.X, pady=(2, 6))

        # Destination
        row1 = tk.Frame(card, bg=Palette.BG_SURFACE)
        row1.pack(fill=tk.X, pady=2)
        tk.Label(row1, text="Export Destination Folder:", font=("Segoe UI", 8, "bold"), bg=Palette.BG_SURFACE, fg=Palette.TEXT_PRIMARY).pack(side=tk.LEFT)

        btn_browse_exp = tk.Button(
            row1,
            text="📂 Browse...",
            font=("Segoe UI", 8, "bold"),
            bg=Palette.BG_ELEVATED,
            fg=Palette.TEXT_PRIMARY,
            bd=0,
            padx=8,
            pady=2,
            relief=tk.FLAT,
            command=self._browse_export_dir,
        )
        btn_browse_exp.pack(side=tk.RIGHT)

        ent_exp_out = tk.Entry(
            card,
            textvariable=self.var_export_out,
            font=("Segoe UI", 9),
            bg=Palette.BG_INPUT,
            fg=Palette.TEXT_PRIMARY,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
        )
        ent_exp_out.pack(fill=tk.X, pady=(2, 6))

        # Re-bake & Format row
        row2 = tk.Frame(card, bg=Palette.BG_SURFACE)
        row2.pack(fill=tk.X, pady=4)

        tk.Checkbutton(
            row2,
            text="Re-Bake Texture Atlas",
            variable=self.var_exp_rebake,
            font=("Segoe UI", 8, "bold"),
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_PRIMARY,
            selectcolor=Palette.BG_ELEVATED,
            bd=0,
        ).pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(row2, text="Pages:", font=("Segoe UI", 8), bg=Palette.BG_SURFACE, fg=Palette.TEXT_SECONDARY).pack(side=tk.LEFT)
        ttk.Combobox(row2, textvariable=self.var_exp_pages, values=["16", "8", "4", "2", "32"], state="readonly", width=6).pack(side=tk.LEFT, padx=(4, 12))

        tk.Label(row2, text="Format:", font=("Segoe UI", 8), bg=Palette.BG_SURFACE, fg=Palette.TEXT_SECONDARY).pack(side=tk.LEFT)
        ttk.Combobox(row2, textvariable=self.var_exp_format, values=["tiff", "jpeg"], state="readonly", width=8).pack(side=tk.LEFT, padx=4)

    # -----------------------------------------------------------------------
    # Right Panel: Deliverables Inspector & Live Agent Feed
    # -----------------------------------------------------------------------
    def _build_right_panel(self, parent: tk.Widget) -> None:
        # Deliverables & Artifacts Card
        self.card_artifacts = tk.LabelFrame(
            parent,
            text=" ARTIFACTS & DELIVERABLES INSPECTOR ",
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_SECONDARY,
            font=("Segoe UI", 8, "bold"),
            bd=1,
            relief=tk.GROOVE,
            padx=8,
            pady=6,
        )
        self.card_artifacts.pack(fill=tk.X, padx=6, pady=(4, 6))

        self.box_artifact_btns = tk.Frame(self.card_artifacts, bg=Palette.BG_SURFACE)
        self.box_artifact_btns.pack(fill=tk.X)

        self.lbl_no_artifacts = tk.Label(
            self.box_artifact_btns,
            text="No deliverables found in output folder yet. Click 'Launch' to generate.",
            font=("Segoe UI", 8),
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_MUTED,
        )
        self.lbl_no_artifacts.pack(anchor=tk.W)

        # Terminal Log Console
        console_frame = tk.LabelFrame(
            parent,
            text=" AGENT EXECUTION FEED & TELEMETRY ",
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_SECONDARY,
            font=("Segoe UI", 8, "bold"),
            bd=1,
            relief=tk.GROOVE,
            padx=6,
            pady=6,
        )
        console_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)

        # Toolbar above console
        console_bar = tk.Frame(console_frame, bg=Palette.BG_SURFACE)
        console_bar.pack(fill=tk.X, pady=(0, 4))

        self.var_autoscroll = tk.BooleanVar(value=True)
        tk.Checkbutton(
            console_bar,
            text="Auto-scroll",
            variable=self.var_autoscroll,
            font=("Segoe UI", 8),
            bg=Palette.BG_SURFACE,
            fg=Palette.TEXT_MUTED,
            selectcolor=Palette.BG_ELEVATED,
            bd=0,
        ).pack(side=tk.LEFT)

        btn_clear = tk.Button(
            console_bar,
            text="Clear Log",
            font=("Segoe UI", 8),
            bg=Palette.BG_ELEVATED,
            fg=Palette.TEXT_MUTED,
            bd=0,
            padx=6,
            pady=1,
            relief=tk.FLAT,
            command=self._clear_log,
        )
        btn_clear.pack(side=tk.RIGHT)

        btn_copy = tk.Button(
            console_bar,
            text="Copy Log",
            font=("Segoe UI", 8),
            bg=Palette.BG_ELEVATED,
            fg=Palette.TEXT_MUTED,
            bd=0,
            padx=6,
            pady=1,
            relief=tk.FLAT,
            command=self._copy_log,
        )
        btn_copy.pack(side=tk.RIGHT, padx=4)

        # Console ScrolledText
        self.txt_log = ScrolledText(
            console_frame,
            wrap=tk.WORD,
            bg=Palette.CONSOLE_BG,
            fg=Palette.CONSOLE_FG,
            insertbackground="white",
            font=("Consolas", 9),
            bd=0,
            highlightthickness=0,
        )
        self.txt_log.pack(fill=tk.BOTH, expand=True)

        # Syntax color tags
        self.txt_log.tag_config("stage", foreground="#58a6ff", font=("Consolas", 9, "bold"))
        self.txt_log.tag_config("success", foreground="#3fb950", font=("Consolas", 9, "bold"))
        self.txt_log.tag_config("warning", foreground="#d29922")
        self.txt_log.tag_config("error", foreground="#f85149", font=("Consolas", 9, "bold"))
        self.txt_log.tag_config("header", foreground="#39c5cf", font=("Consolas", 9, "bold"))

    # -----------------------------------------------------------------------
    # Bottom Execution Bar
    # -----------------------------------------------------------------------
    def _build_bottom_bar(self) -> None:
        bar = tk.Frame(self.root, bg=Palette.BG_SURFACE, height=60, bd=1, relief=tk.SOLID)
        bar.pack(fill=tk.X, side=tk.BOTTOM, padx=0, pady=0)

        inner = tk.Frame(bar, bg=Palette.BG_SURFACE)
        inner.pack(fill=tk.X, padx=16, pady=10)

        # Left: Big Launch Action
        self.btn_launch = tk.Button(
            inner,
            text="🚀  Launch 3D Reconstruction",
            font=("Segoe UI", 10, "bold"),
            bg=Palette.ACCENT_PRIMARY,
            fg="white",
            activebackground=Palette.ACCENT_PRIMARY_HOVER,
            activeforeground="white",
            bd=0,
            padx=16,
            pady=8,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._on_launch_clicked,
        )
        self.btn_launch.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_stop = tk.Button(
            inner,
            text="✕  Cancel Process",
            font=("Segoe UI", 9, "bold"),
            bg=Palette.BG_ELEVATED,
            fg=Palette.TEXT_MUTED,
            activebackground=Palette.ACCENT_RED,
            activeforeground="white",
            bd=0,
            padx=12,
            pady=8,
            state=tk.DISABLED,
            relief=tk.FLAT,
            command=self._on_stop_clicked,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 16))

        # Center Progress
        self.progress_bar = ttk.Progressbar(inner, mode="indeterminate", length=220)
        self.progress_bar.pack(side=tk.LEFT, padx=(0, 16))

        # Right: Status Pill
        self.lbl_status = tk.Label(
            inner,
            text="Status: Ready to reconstruct",
            font=("Segoe UI", 9, "bold"),
            bg=Palette.BG_SURFACE,
            fg="#3fb950",
        )
        self.lbl_status.pack(side=tk.RIGHT)

    # -----------------------------------------------------------------------
    # Presets Logic
    # -----------------------------------------------------------------------
    def _apply_preset(self, name: str) -> None:
        """Apply one of the three agent presets."""
        for p_name, btn in self.preset_btns.items():
            if p_name == name:
                btn.config(bg=Palette.ACCENT_BLUE, fg="white")
            else:
                btn.config(bg=Palette.BG_ELEVATED, fg=Palette.TEXT_PRIMARY)

        if name == "architectural":
            self.var_align_quality.set("medium")
            self.var_faces.set("1000000")
            self.var_tex_pages.set("16")
            self.var_crop.set("gps-square")
            self.var_depth_quality.set("medium")
            self.var_depth_filtering.set("moderate")
            self.var_rolling.set(True)
            self.var_stationary.set(True)
            self.var_close_holes.set(True)
            self.var_fill_holes.set(True)
            self.var_ghosting.set(True)
            self.lbl_preset_desc.config(
                text="🏛️ Architectural Standard (Recommended): Medium alignment, 40K keypoints, 25K tiepoints, 1M faces, 16-page 8K textures, GPS-square crop, rolling shutter ON."
            )
        elif name == "draft":
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
            self.lbl_preset_desc.config(
                text="⚡ Rapid Draft Preview: Fast execution for geometric and alignment verification (Low quality, 500K faces, 4-page textures, ~15 min)."
            )
        elif name == "heritage":
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
            self.lbl_preset_desc.config(
                text="💎 Ultra Heritage Detail: Maximum preservation for historical landmarks and intricate structures (High alignment, 2M faces, 32-page 8K textures, ray convergence crop)."
            )

    # -----------------------------------------------------------------------
    # Directory & Dataset Telemetry
    # -----------------------------------------------------------------------
    def _browse_images_dir(self) -> None:
        initial = self.var_images_dir.get().strip() or os.path.expanduser("~")
        chosen = filedialog.askdirectory(title="Select Raw Image Folder", initialdir=initial)
        if chosen:
            self.var_images_dir.set(os.path.normpath(chosen))

    def _browse_output_dir(self) -> None:
        initial = self.var_output_dir.get().strip() or self.var_images_dir.get().strip() or os.path.expanduser("~")
        chosen = filedialog.askdirectory(title="Select Deliverables Destination", initialdir=initial)
        if chosen:
            self.var_output_dir.set(os.path.normpath(chosen))

    def _browse_psx_file(self) -> None:
        initial = os.path.dirname(self.var_psx_path.get().strip()) or os.path.expanduser("~")
        chosen = filedialog.askopenfilename(
            title="Select Solved Metashape Project",
            filetypes=[("Metashape Project", "*.psx"), ("All Files", "*.*")],
            initialdir=initial,
        )
        if chosen:
            self.var_psx_path.set(os.path.normpath(chosen))

    def _browse_export_dir(self) -> None:
        initial = self.var_export_out.get().strip() or os.path.expanduser("~")
        chosen = filedialog.askdirectory(title="Select Export Destination", initialdir=initial)
        if chosen:
            self.var_export_out.set(os.path.normpath(chosen))

    def _on_images_dir_changed(self, *_args) -> None:
        """Scan selected image directory and display instant telemetry."""
        img_dir = self.var_images_dir.get().strip()
        if not img_dir or not os.path.isdir(img_dir):
            self.lbl_dataset_stats_text.set("Select an image folder to scan contents.")
            return

        # Auto-set output folder
        default_out = os.path.join(img_dir, "model")
        cur_out = self.var_output_dir.get().strip()
        if not cur_out or cur_out.endswith("model"):
            self.var_output_dir.set(default_out)

        # Asynchronous scan of image count
        def scan_worker():
            total_count = 0
            fmt_counts: dict[str, int] = {}
            for root, _dirs, files in os.walk(img_dir):
                if os.path.abspath(root).startswith(os.path.abspath(default_out)):
                    continue
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in SUPPORTED_EXTS:
                        total_count += 1
                        fmt_counts[ext] = fmt_counts.get(ext, 0) + 1

            if total_count > 0:
                top_fmts = sorted(fmt_counts.items(), key=lambda x: x[1], reverse=True)[:3]
                fmts_str = ", ".join(f"{cnt} {e.upper()}" for e, cnt in top_fmts)
                self.lbl_dataset_stats_text.set(f"✓ Found {total_count:,} photos ({fmts_str}) across directory and subfolders.")
            else:
                self.lbl_dataset_stats_text.set("⚠️ No supported photos found in directory! Check folder path.")

        threading.Thread(target=scan_worker, daemon=True).start()

    def _on_psx_path_changed(self, *_args) -> None:
        psx = self.var_psx_path.get().strip()
        if psx and os.path.isfile(psx):
            if not self.var_export_out.get().strip():
                self.var_export_out.set(os.path.dirname(psx))

    def _set_mode(self, mode: str) -> None:
        self.active_mode = mode
        if mode == "recon":
            self.btn_mode_recon.config(bg=Palette.ACCENT_BLUE, fg="white")
            self.btn_mode_export.config(bg=Palette.BG_ELEVATED, fg=Palette.TEXT_SECONDARY)
            self.frame_export_container.pack_forget()
            self.frame_recon_container.pack(fill=tk.BOTH, expand=True)
            self.btn_launch.config(text="🚀  Launch 3D Reconstruction", bg=Palette.ACCENT_PRIMARY)
        else:
            self.btn_mode_export.config(bg=Palette.ACCENT_BLUE, fg="white")
            self.btn_mode_recon.config(bg=Palette.BG_ELEVATED, fg=Palette.TEXT_SECONDARY)
            self.frame_recon_container.pack_forget()
            self.frame_export_container.pack(fill=tk.BOTH, expand=True)
            self.btn_launch.config(text="📦  Re-Bake Texture & Export Assets", bg=Palette.ACCENT_BLUE)

    def _check_metashape(self) -> None:
        if self.metashape_exe:
            self.lbl_metashape_status.config(
                text="Metashape Pro Detected",
                bg="#163321",
                fg="#3fb950",
            )
            self._log(f"[AutoMetashape Studio] Initialized successfully with: {self.metashape_exe}", "header")
        else:
            self.lbl_metashape_status.config(
                text="Metashape Not Found",
                bg="#2a1215",
                fg=Palette.ACCENT_RED_HOVER,
            )
            self._log("[WARNING] Metashape Professional not found in standard paths. You will be prompted to locate metashape.exe on launch.", "warning")

    # -----------------------------------------------------------------------
    # Deliverables & Artifacts Polling
    # -----------------------------------------------------------------------
    def _poll_deliverables(self) -> None:
        """Poll the active output folder and update interactive asset buttons."""
        target_dir = self.var_output_dir.get().strip() if self.active_mode == "recon" else self.var_export_out.get().strip()

        if target_dir and os.path.isdir(target_dir):
            glb_path = os.path.join(target_dir, "model.glb")
            obj_path = os.path.join(target_dir, "model.obj")
            psx_path = os.path.join(target_dir, "building_reconstruction.psx")
            tif_files = [f for f in os.listdir(target_dir) if f.startswith("model") and f.endswith(".tif")]

            has_assets = os.path.isfile(glb_path) or os.path.isfile(obj_path) or os.path.isfile(psx_path)

            if has_assets:
                for widget in self.box_artifact_btns.winfo_children():
                    widget.destroy()

                grid = tk.Frame(self.box_artifact_btns, bg=Palette.BG_SURFACE)
                grid.pack(fill=tk.X)

                # GLB Badge Button
                if os.path.isfile(glb_path):
                    sz = os.path.getsize(glb_path) / (1024 * 1024)
                    btn_glb = tk.Button(
                        grid,
                        text=f"📦 model.glb ({sz:.1f} MB)",
                        font=("Segoe UI", 8, "bold"),
                        bg=Palette.BG_ELEVATED,
                        fg="#58a6ff",
                        bd=1,
                        relief=tk.SOLID,
                        padx=8,
                        pady=3,
                        cursor="hand2",
                        command=lambda: os.startfile(glb_path),
                    )
                    btn_glb.pack(side=tk.LEFT, padx=(0, 6), pady=2)

                # OBJ Badge Button
                if os.path.isfile(obj_path):
                    sz = os.path.getsize(obj_path) / (1024 * 1024)
                    btn_obj = tk.Button(
                        grid,
                        text=f"📐 model.obj ({sz:.1f} MB)",
                        font=("Segoe UI", 8, "bold"),
                        bg=Palette.BG_ELEVATED,
                        fg=Palette.TEXT_PRIMARY,
                        bd=1,
                        relief=tk.SOLID,
                        padx=8,
                        pady=3,
                        cursor="hand2",
                        command=lambda: os.startfile(target_dir),
                    )
                    btn_obj.pack(side=tk.LEFT, padx=(0, 6), pady=2)

                # Textures Badge
                if tif_files:
                    btn_tex = tk.Button(
                        grid,
                        text=f"🎨 {len(tif_files)}x 8K Textures",
                        font=("Segoe UI", 8, "bold"),
                        bg=Palette.BG_ELEVATED,
                        fg=Palette.ACCENT_CYAN,
                        bd=1,
                        relief=tk.SOLID,
                        padx=8,
                        pady=3,
                        cursor="hand2",
                        command=lambda: os.startfile(target_dir),
                    )
                    btn_tex.pack(side=tk.LEFT, padx=(0, 6), pady=2)

                # PSX Badge Button
                if os.path.isfile(psx_path) and self.metashape_exe:
                    btn_psx = tk.Button(
                        grid,
                        text="🚀 Open in Metashape GUI",
                        font=("Segoe UI", 8, "bold"),
                        bg="#1f3725",
                        fg="#3fb950",
                        bd=1,
                        relief=tk.SOLID,
                        padx=8,
                        pady=3,
                        cursor="hand2",
                        command=lambda: subprocess.Popen([self.metashape_exe, psx_path]),
                    )
                    btn_psx.pack(side=tk.LEFT, padx=(0, 6), pady=2)

                # Open Directory Button
                btn_open_dir = tk.Button(
                    grid,
                    text="📂 Open Output Folder",
                    font=("Segoe UI", 8, "bold"),
                    bg=Palette.BG_ELEVATED,
                    fg=Palette.TEXT_MUTED,
                    bd=0,
                    padx=6,
                    pady=3,
                    relief=tk.FLAT,
                    command=lambda: os.startfile(target_dir),
                )
                btn_open_dir.pack(side=tk.RIGHT)

        self.root.after(3000, self._poll_deliverables)

    # -----------------------------------------------------------------------
    # Logging & Console
    # -----------------------------------------------------------------------
    def _log(self, text: str, tag: str | None = None) -> None:
        self.txt_log.insert(tk.END, text + "\n", tag)
        if self.var_autoscroll.get():
            self.txt_log.see(tk.END)

    def _clear_log(self) -> None:
        self.txt_log.delete("1.0", tk.END)

    def _copy_log(self) -> None:
        content = self.txt_log.get("1.0", tk.END)
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        messagebox.showinfo("Copied", "Console log copied to clipboard.")

    # -----------------------------------------------------------------------
    # Subprocess Execution & Milestone Tracking
    # -----------------------------------------------------------------------
    def _on_launch_clicked(self) -> None:
        if not self.metashape_exe or not os.path.isfile(self.metashape_exe):
            chosen = filedialog.askopenfilename(
                title="Locate metashape.exe",
                filetypes=[("Executable", "metashape.exe"), ("All Files", "*.*")],
            )
            if chosen and os.path.isfile(chosen):
                self.metashape_exe = os.path.normpath(chosen)
                self._check_metashape()
            else:
                messagebox.showerror("Error", "Valid Metashape Pro executable is required to run the pipeline.")
                return

        if self.active_mode == "recon":
            self._launch_reconstruction()
        else:
            self._launch_export()

    def _launch_reconstruction(self) -> None:
        img_dir = self.var_images_dir.get().strip()
        if not img_dir or not os.path.isdir(img_dir):
            messagebox.showwarning("Input Required", "Please select a valid raw images folder.")
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
        self._spawn_process(cmd, task_name="3D Reconstruction Pipeline")

    def _launch_export(self) -> None:
        psx_path = self.var_psx_path.get().strip()
        if not psx_path or not os.path.isfile(psx_path):
            messagebox.showwarning("Input Required", "Please select a valid Metashape project (.psx).")
            return

        out_dir = self.var_export_out.get().strip() or os.path.dirname(psx_path)
        self.var_export_out.set(out_dir)

        cmd = [
            self.metashape_exe,
            "-r",
            self.export_script,
            "--project",
            psx_path,
            "--output",
            out_dir,
            "--texture-format",
            self.var_exp_format.get(),
        ]

        if self.var_exp_rebake.get():
            cmd.append("--rebake-texture")
            cmd.extend(["--texture-pages", str(self.var_exp_pages.get())])

        self.stepper.reset()
        self.stepper.set_running(5)
        self._spawn_process(cmd, task_name="Texture Re-Bake & Export")

    def _spawn_process(self, cmd: list[str], task_name: str) -> None:
        self.is_running = True
        self.start_time = time.time()
        self.btn_launch.config(state=tk.DISABLED, bg=Palette.BG_ELEVATED)
        self.btn_stop.config(state=tk.NORMAL, bg=Palette.ACCENT_RED, fg="white")
        self.progress_bar.start(10)
        self.lbl_status.config(text=f"Status: Executing {task_name}...", fg=Palette.ACCENT_BLUE_HOVER)

        self._log(f"\n[{task_name}] Launching Autonomous Pipeline...", "header")
        self._log(f"Command: {' '.join(cmd)}\n" + "─" * 70)

        def worker() -> None:
            creation_flags = 0
            if sys.platform == "win32":
                creation_flags = subprocess.CREATE_NO_WINDOW

            try:
                self.current_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=creation_flags,
                )

                if self.current_process.stdout:
                    for line in iter(self.current_process.stdout.readline, ""):
                        if line:
                            clean_line = line.rstrip()
                            self.log_queue.put(clean_line)
                            self._parse_stepper_line(clean_line)
                    self.current_process.stdout.close()

                ret = self.current_process.wait()
                elapsed = (time.time() - self.start_time) / 60.0
                if ret == 0:
                    self.log_queue.put(f"\n[SUCCESS] {task_name} completed successfully in {elapsed:.2f} minutes!")
                    self.log_queue.put("__TASK_DONE_SUCCESS__")
                else:
                    self.log_queue.put(f"\n[ERROR] Pipeline failed with exit code: {ret}")
                    self.log_queue.put("__TASK_DONE_ERROR__")
            except Exception as e:
                self.log_queue.put(f"\n[EXCEPTION] Process runtime failure: {e}")
                self.log_queue.put("__TASK_DONE_ERROR__")
            finally:
                self.current_process = None

        threading.Thread(target=worker, daemon=True).start()

    def _parse_stepper_line(self, line: str) -> None:
        """Parse stdout lines in real-time to advance the visual stepper."""
        l = line.lower()
        if "[1/6]" in line or "ingesting" in l:
            self.root.after(0, lambda: self.stepper.set_running(0))
        elif "[2/6]" in line or "gpu feature matching" in l:
            self.root.after(0, lambda: self.stepper.set_running(1))
        elif "[3/6]" in line or "aligning cameras" in l:
            self.root.after(0, lambda: self.stepper.set_running(1))
        elif "[auto-crop]" in line or "camera gps square" in l:
            self.root.after(0, lambda: self.stepper.set_running(2))
        elif "[4/6]" in line or "mvs depth maps" in l or "computing gpu mvs" in l:
            self.root.after(0, lambda: self.stepper.set_running(3))
        elif "[5/6]" in line or "surface mesh" in l or "reconstructing arbitrary" in l:
            self.root.after(0, lambda: self.stepper.set_running(4))
        elif "[6/6]" in line or "baking" in l or "texture atlas" in l or "[texture]" in line:
            self.root.after(0, lambda: self.stepper.set_running(5))
        elif "exporting final" in l or "[export]" in line or "[1/2] exporting" in l:
            self.root.after(0, lambda: self.stepper.set_running(6))
        elif "[success]" in l:
            self.root.after(0, lambda: self.stepper.set_done(6))

    def _process_log_queue(self) -> None:
        """Consume messages from worker thread and render with syntax tags."""
        while not self.log_queue.empty():
            try:
                line = self.log_queue.get_nowait()
                if line == "__TASK_DONE_SUCCESS__":
                    self._on_task_finished(success=True)
                elif line == "__TASK_DONE_ERROR__":
                    self._on_task_finished(success=False)
                else:
                    tag = None
                    if any(s in line for s in ["[1/6]", "[2/6]", "[3/6]", "[4/6]", "[5/6]", "[6/6]", "[Auto-Crop]"]):
                        tag = "stage"
                    elif "[SUCCESS]" in line or "[CHECKPOINT]" in line:
                        tag = "success"
                    elif "[WARNING]" in line or "[Hardware]" in line:
                        tag = "warning"
                    elif "[ERROR]" in line or "[EXCEPTION]" in line:
                        tag = "error"
                    self._log(line, tag)
            except queue.Empty:
                break
        self.root.after(80, self._process_log_queue)

    def _on_stop_clicked(self) -> None:
        if self.current_process and self.is_running:
            if messagebox.askyesno("Cancel Process", "Are you sure you want to terminate the running photogrammetry task?"):
                self._log("\n[USER] Terminating process...", "warning")
                try:
                    self.current_process.terminate()
                    time.sleep(0.5)
                    if self.current_process.poll() is None:
                        self.current_process.kill()
                    self._log("[USER] Process terminated successfully.", "warning")
                except Exception as e:
                    self._log(f"[USER] Error terminating: {e}", "error")
                self._on_task_finished(success=False, user_cancelled=True)

    def _on_task_finished(self, success: bool, user_cancelled: bool = False) -> None:
        self.is_running = False
        self.progress_bar.stop()
        self.btn_launch.config(
            state=tk.NORMAL,
            bg=Palette.ACCENT_PRIMARY if self.active_mode == "recon" else Palette.ACCENT_BLUE,
        )
        self.btn_stop.config(state=tk.DISABLED, bg=Palette.BG_ELEVATED, fg=Palette.TEXT_MUTED)

        if user_cancelled:
            self.lbl_status.config(text="Status: Cancelled by user", fg=Palette.ACCENT_AMBER)
        elif success:
            self.stepper.set_done(6)
            self.lbl_status.config(text="Status: Reconstruction Complete!", fg="#3fb950")
            messagebox.showinfo("Pipeline Completed", "Photogrammetry 3D reconstruction finished successfully!\nDeliverables are ready in your output folder.")
        else:
            self.lbl_status.config(text="Status: Process Failed (See Log)", fg=Palette.ACCENT_RED_HOVER)
            messagebox.showerror("Execution Failed", "Pipeline encountered an error. Check the execution feed for details.")


def main() -> None:
    root = tk.Tk()
    app = AutoMetashapeStudio(root)
    root.mainloop()


if __name__ == "__main__":
    main()
