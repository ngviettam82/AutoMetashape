# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape Studio - User-Friendly Desktop Photogrammetry GUI.

A clean, human-centered desktop application for autonomous 3D photogrammetry.
Designed following modern open-source UI/UX principles:
- 1-2-3 Progressive Disclosure (Input -> Preset -> Start)
- Collapsible Advanced Fine-Tuning (zero clutter by default)
- Interactive Visual Breadcrumb Stepper (7-stage pipeline milestones)
- One-Click Presets with visual selection cards
- Real-time streaming log console with syntax highlights and direct OS actions
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

# Windows High-DPI crisp text rendering
if sys.platform == "win32":
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Visual Palette (Modern Dark Theme)
# ---------------------------------------------------------------------------
class UITheme:
    BG_ROOT = "#0f172a"          # Deep slate background
    BG_CARD = "#1e293b"          # Surface card background
    BG_CARD_ACTIVE = "#1e3a5f"   # Selected card highlight
    BG_INPUT = "#0f172a"         # Input field background
    BG_ELEVATED = "#334155"      # Secondary buttons / badges
    BG_CONSOLE = "#020617"       # Terminal dark background

    BORDER = "#334155"           # Card borders
    BORDER_ACTIVE = "#38bdf8"    # Active cyan border
    BORDER_SUCCESS = "#10b981"   # Success emerald border

    TEXT_TITLE = "#f8fafc"       # Pure white
    TEXT_BODY = "#e2e8f0"        # Light gray text
    TEXT_MUTED = "#94a3b8"       # Subtle gray
    TEXT_DIM = "#64748b"         # De-emphasized text

    ACCENT_GREEN = "#10b981"     # Start / Success emerald
    ACCENT_GREEN_HOVER = "#059669"
    ACCENT_BLUE = "#3b82f6"      # Primary royal blue
    ACCENT_BLUE_HOVER = "#2563eb"
    ACCENT_RED = "#ef4444"       # Stop / Error red
    ACCENT_RED_HOVER = "#dc2626"
    ACCENT_AMBER = "#f59e0b"     # Warning / In-progress
    ACCENT_CYAN = "#38bdf8"      # Active pill cyan


SUPPORTED_IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".tif", ".tiff", ".bmp", ".webp",
    ".jp2", ".jxl", ".exr", ".dng", ".cr2", ".cr3", ".nef", ".arw",
    ".iiq", ".3fr", ".rw2", ".raf", ".raw", ".seq", ".ara",
}

DEFAULT_METASHAPE_PATHS = [
    r"C:\Program Files\Agisoft\Metashape Pro\metashape.exe",
    r"C:\Program Files\Agisoft\PhotoScan Pro\photoscan.exe",
]


def find_metashape_path() -> str | None:
    """Locate Metashape Pro executable on the system."""
    for p in DEFAULT_METASHAPE_PATHS:
        if os.path.isfile(p):
            return p
    which_p = shutil.which("metashape")
    if which_p and os.path.isfile(which_p):
        return which_p
    return None


# ---------------------------------------------------------------------------
# Horizontal Visual Stepper (Pipeline Milestones)
# ---------------------------------------------------------------------------
class HorizontalStepper(tk.Frame):
    """Clean horizontal breadcrumb tracking the 7 reconstruction stages."""

    STEPS = [
        "1. Ingest",
        "2. Align",
        "3. Crop",
        "4. Depth",
        "5. Mesh (600K)",
        "6. Texture (16P)",
        "7. Export",
    ]

    def __init__(self, parent: tk.Widget, **kwargs) -> None:
        super().__init__(parent, bg=UITheme.BG_CARD, **kwargs)
        self.cards: list[tk.Label] = []
        self._build()

    def _build(self) -> None:
        row = tk.Frame(self, bg=UITheme.BG_CARD)
        row.pack(fill=tk.X, padx=4, pady=4)

        for i, name in enumerate(self.STEPS):
            lbl = tk.Label(
                row,
                text=name,
                font=("Segoe UI", 8, "bold"),
                bg=UITheme.BG_ROOT,
                fg=UITheme.TEXT_MUTED,
                bd=1,
                relief=tk.SOLID,
                padx=6,
                pady=5,
            )
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
            self.cards.append(lbl)

        self.lbl_stage_info = tk.Label(
            self,
            text="Pipeline Status: Waiting for user to start...",
            font=("Segoe UI", 8),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_MUTED,
            anchor=tk.W,
        )
        self.lbl_stage_info.pack(fill=tk.X, padx=6, pady=(2, 2))

    def reset(self) -> None:
        for lbl in self.cards:
            lbl.config(bg=UITheme.BG_ROOT, fg=UITheme.TEXT_MUTED, relief=tk.SOLID)
        self.lbl_stage_info.config(text="Pipeline Status: Ready to start", fg=UITheme.TEXT_MUTED)

    def set_active(self, idx: int, description: str = "") -> None:
        for i in range(idx):
            self.cards[i].config(bg="#064e3b", fg="#6ee7b7", relief=tk.SOLID)  # Completed green
        if 0 <= idx < len(self.cards):
            self.cards[idx].config(bg="#1e3a5f", fg=UITheme.ACCENT_CYAN, relief=tk.SOLID)
            desc_text = f"▶ Active Stage ({idx+1}/7): {self.STEPS[idx]}"
            if description:
                desc_text += f" - {description}"
            self.lbl_stage_info.config(text=desc_text, fg=UITheme.ACCENT_CYAN)

    def set_all_done(self) -> None:
        for lbl in self.cards:
            lbl.config(bg="#064e3b", fg="#6ee7b7", relief=tk.SOLID)
        self.lbl_stage_info.config(text="✓ All 7 Stages Completed Successfully!", fg="#10b981")


# ---------------------------------------------------------------------------
# Main Studio Window
# ---------------------------------------------------------------------------
class AutoMetashapeStudio:
    """User-friendly photogrammetry desktop studio."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AutoMetashape Studio - 3D Photogrammetry")
        self.root.geometry("1020x860")
        self.root.minsize(880, 680)
        self.root.configure(bg=UITheme.BG_ROOT)

        # Paths
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

        self._init_variables()
        self._build_ui()
        self._select_preset("architectural")
        self._check_metashape()

        # Event loops
        self.root.after(80, self._consume_log_queue)

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

        self.lbl_photo_status = tk.StringVar(value="No folder selected yet. Click 'Browse Folder' to begin.")

    # -----------------------------------------------------------------------
    # UI Layout Construction
    # -----------------------------------------------------------------------
    def _build_ui(self) -> None:
        # Header Banner
        self._build_header()

        # Mode Selector Bar (Reconstruction vs Export)
        self._build_mode_bar()

        # Main Scrollable Workspace
        self._build_main_workspace()

        # Bottom Action Bar
        self._build_bottom_bar()

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=UITheme.BG_CARD, height=52)
        header.pack(fill=tk.X, padx=0, pady=(0, 2))

        left = tk.Frame(header, bg=UITheme.BG_CARD)
        left.pack(side=tk.LEFT, padx=16, pady=8)

        lbl_logo = tk.Label(
            left,
            text="⚡ AutoMetashape Studio",
            font=("Segoe UI", 13, "bold"),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_TITLE,
        )
        lbl_logo.pack(side=tk.LEFT)

        lbl_sub = tk.Label(
            left,
            text=" |  Autonomous Architectural 3D Photogrammetry",
            font=("Segoe UI", 9),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_MUTED,
        )
        lbl_sub.pack(side=tk.LEFT, padx=(4, 0))

        right = tk.Frame(header, bg=UITheme.BG_CARD)
        right.pack(side=tk.RIGHT, padx=16, pady=8)

        self.lbl_metashape_badge = tk.Label(
            right,
            text="Checking Metashape...",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_ELEVATED,
            fg=UITheme.TEXT_MUTED,
            padx=8,
            pady=3,
        )
        self.lbl_metashape_badge.pack(side=tk.RIGHT)

    def _build_mode_bar(self) -> None:
        bar = tk.Frame(self.root, bg=UITheme.BG_ROOT, pady=6)
        bar.pack(fill=tk.X, padx=12)

        self.btn_tab_recon = tk.Button(
            bar,
            text="🚀  3D Reconstruction (Photos ➔ 3D Model)",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.ACCENT_BLUE,
            fg="white",
            activebackground=UITheme.ACCENT_BLUE_HOVER,
            activeforeground="white",
            bd=0,
            padx=16,
            pady=6,
            relief=tk.FLAT,
            cursor="hand2",
            command=lambda: self._set_mode("recon"),
        )
        self.btn_tab_recon.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_tab_export = tk.Button(
            bar,
            text="📦  Re-Bake Texture & Export (.psx)",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_MUTED,
            activebackground=UITheme.BG_ELEVATED,
            activeforeground=UITheme.TEXT_TITLE,
            bd=0,
            padx=16,
            pady=6,
            relief=tk.FLAT,
            cursor="hand2",
            command=lambda: self._set_mode("export"),
        )
        self.btn_tab_export.pack(side=tk.LEFT)

    def _build_main_workspace(self) -> None:
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

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(12, 0), pady=(0, 6))
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 4))

        # --- RECONSTRUCTION WORKFLOW WIDGETS ---
        self.wrapper_recon = tk.Frame(self.content_frame, bg=UITheme.BG_ROOT)
        self.wrapper_recon.pack(fill=tk.X, expand=True)

        # 1. Image & Destination Folders Card
        self._build_folder_card(self.wrapper_recon)

        # 2. Workflow Presets Card
        self._build_presets_card(self.wrapper_recon)

        # 3. Collapsible Advanced Settings
        self._build_advanced_settings_card(self.wrapper_recon)

        # --- STANDALONE EXPORT WIDGETS ---
        self.wrapper_export = tk.Frame(self.content_frame, bg=UITheme.BG_ROOT)
        self._build_export_card(self.wrapper_export)

        # 4. Pipeline Stepper (Visual Trajectory)
        self._build_stepper_card(self.content_frame)

        # 5. Live Execution Feed & Actions
        self._build_log_card(self.content_frame)

    # -----------------------------------------------------------------------
    # Step 1: Folder Selection Card
    # -----------------------------------------------------------------------
    def _build_folder_card(self, parent: tk.Widget) -> None:
        card = tk.LabelFrame(
            parent,
            text=" 1. INPUT PHOTOS & OUTPUT FOLDER ",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_TITLE,
            bd=1,
            relief=tk.GROOVE,
            padx=12,
            pady=10,
        )
        card.pack(fill=tk.X, pady=(0, 8))

        # Raw Images Row
        row_img = tk.Frame(card, bg=UITheme.BG_CARD)
        row_img.pack(fill=tk.X, pady=(0, 2))

        tk.Label(
            row_img,
            text="Raw Photos Directory:",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_BODY,
        ).pack(side=tk.LEFT)

        btn_browse_img = tk.Button(
            row_img,
            text="📂 Browse Folder...",
            font=("Segoe UI", 9, "bold"),
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
            card,
            textvariable=self.var_images_dir,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_TITLE,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
        )
        ent_img.pack(fill=tk.X, pady=(2, 4))

        # Photo count badge
        lbl_stats = tk.Label(
            card,
            textvariable=self.lbl_photo_status,
            font=("Segoe UI", 8),
            bg=UITheme.BG_CARD,
            fg=UITheme.ACCENT_CYAN,
            anchor=tk.W,
        )
        lbl_stats.pack(fill=tk.X, pady=(0, 6))

        # Output Folder Row
        row_out = tk.Frame(card, bg=UITheme.BG_CARD)
        row_out.pack(fill=tk.X, pady=(4, 2))

        tk.Label(
            row_out,
            text="Deliverables Destination:",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_BODY,
        ).pack(side=tk.LEFT)

        btn_browse_out = tk.Button(
            row_out,
            text="📂 Change Destination...",
            font=("Segoe UI", 8),
            bg=UITheme.BG_ELEVATED,
            fg=UITheme.TEXT_TITLE,
            bd=0,
            padx=10,
            pady=2,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._browse_output_dir,
        )
        btn_browse_out.pack(side=tk.RIGHT)

        ent_out = tk.Entry(
            card,
            textvariable=self.var_output_dir,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_TITLE,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
        )
        ent_out.pack(fill=tk.X, pady=(2, 2))

    # -----------------------------------------------------------------------
    # Step 2: Quality Presets Card
    # -----------------------------------------------------------------------
    def _build_presets_card(self, parent: tk.Widget) -> None:
        card = tk.LabelFrame(
            parent,
            text=" 2. CHOOSE WORKFLOW PRESET (ONE-CLICK SETUP) ",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_TITLE,
            bd=1,
            relief=tk.GROOVE,
            padx=12,
            pady=10,
        )
        card.pack(fill=tk.X, pady=(0, 8))

        row = tk.Frame(card, bg=UITheme.BG_CARD)
        row.pack(fill=tk.X)

        self.preset_frames: dict[str, tk.Frame] = {}

        def _make_preset(key: str, icon_title: str, subtitle: str):
            f = tk.Frame(row, bg=UITheme.BG_ROOT, bd=1, relief=tk.SOLID, padx=10, pady=8, cursor="hand2")
            f.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)

            t = tk.Label(f, text=icon_title, font=("Segoe UI", 9, "bold"), bg=UITheme.BG_ROOT, fg=UITheme.TEXT_TITLE)
            t.pack(anchor=tk.W)

            s = tk.Label(f, text=subtitle, font=("Segoe UI", 8), bg=UITheme.BG_ROOT, fg=UITheme.TEXT_MUTED, justify=tk.LEFT)
            s.pack(anchor=tk.W, pady=(2, 0))

            # Bind click
            f.bind("<Button-1>", lambda e: self._select_preset(key))
            t.bind("<Button-1>", lambda e: self._select_preset(key))
            s.bind("<Button-1>", lambda e: self._select_preset(key))

            self.preset_frames[key] = f

        _make_preset(
            "architectural",
            "🏛️ Architectural Standard",
            "16-Page 8K textures, 600K faces\nGPS crop, rolling shutter ON (Default)",
        )
        _make_preset(
            "draft",
            "⚡ Rapid Draft Preview",
            "Fast ~15 min run for alignment\nand geometry check (500K faces, 4P)",
        )
        _make_preset(
            "heritage",
            "💎 Ultra Heritage Detail",
            "32-Page 8K textures, 2M faces\nOptical ray crop, max preservation",
        )

    # -----------------------------------------------------------------------
    # Step 3: Collapsible Advanced Pipeline Settings
    # -----------------------------------------------------------------------
    def _build_advanced_settings_card(self, parent: tk.Widget) -> None:
        self.card_adv = tk.LabelFrame(
            parent,
            text=" ⚙️ ADVANCED PIPELINE FINE-TUNING ",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_MUTED,
            bd=1,
            relief=tk.GROOVE,
            padx=12,
            pady=6,
        )
        self.card_adv.pack(fill=tk.X, pady=(0, 8))

        # Toggle bar
        bar_toggle = tk.Frame(self.card_adv, bg=UITheme.BG_CARD)
        bar_toggle.pack(fill=tk.X)

        self.btn_toggle_adv = tk.Button(
            bar_toggle,
            text="▶ Show Advanced Parameters (Alignment, Meshing, Texture Flags)",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_CARD,
            fg=UITheme.ACCENT_CYAN,
            bd=0,
            padx=4,
            pady=3,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._toggle_advanced,
        )
        self.btn_toggle_adv.pack(side=tk.LEFT)

        # Hidden body
        self.body_adv = tk.Frame(self.card_adv, bg=UITheme.BG_CARD)

        grid = tk.Frame(self.body_adv, bg=UITheme.BG_CARD)
        grid.pack(fill=tk.X, pady=(4, 6))

        # Row 1: Align, Faces, Pages
        tk.Label(grid, text="Align Quality:", font=("Segoe UI", 8), bg=UITheme.BG_CARD, fg=UITheme.TEXT_MUTED).grid(row=0, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(grid, textvariable=self.var_align_quality, values=["medium", "high", "highest", "low"], state="readonly", width=10).grid(row=0, column=1, padx=(4, 12), pady=3)

        tk.Label(grid, text="Target Faces:", font=("Segoe UI", 8), bg=UITheme.BG_CARD, fg=UITheme.TEXT_MUTED).grid(row=0, column=2, sticky=tk.W, pady=3)
        ttk.Combobox(grid, textvariable=self.var_faces, values=["600000", "1000000", "500000", "2000000", "3000000"], width=10).grid(row=0, column=3, padx=(4, 12), pady=3)

        tk.Label(grid, text="Texture Pages:", font=("Segoe UI", 8), bg=UITheme.BG_CARD, fg=UITheme.TEXT_MUTED).grid(row=0, column=4, sticky=tk.W, pady=3)
        ttk.Combobox(grid, textvariable=self.var_tex_pages, values=["16", "8", "4", "2", "32"], state="readonly", width=8).grid(row=0, column=5, padx=4, pady=3)

        # Row 2: Crop, Depth Quality, Depth Filtering
        tk.Label(grid, text="Bounding Box:", font=("Segoe UI", 8), bg=UITheme.BG_CARD, fg=UITheme.TEXT_MUTED).grid(row=1, column=0, sticky=tk.W, pady=3)
        ttk.Combobox(grid, textvariable=self.var_crop, values=["gps-square", "structure", "manual", "none"], state="readonly", width=10).grid(row=1, column=1, padx=(4, 12), pady=3)

        tk.Label(grid, text="Depth Quality:", font=("Segoe UI", 8), bg=UITheme.BG_CARD, fg=UITheme.TEXT_MUTED).grid(row=1, column=2, sticky=tk.W, pady=3)
        ttk.Combobox(grid, textvariable=self.var_depth_quality, values=["medium", "high", "ultra", "low"], state="readonly", width=10).grid(row=1, column=3, padx=(4, 12), pady=3)

        tk.Label(grid, text="Depth Filter:", font=("Segoe UI", 8), bg=UITheme.BG_CARD, fg=UITheme.TEXT_MUTED).grid(row=1, column=4, sticky=tk.W, pady=3)
        ttk.Combobox(grid, textvariable=self.var_depth_filtering, values=["moderate", "mild", "aggressive"], state="readonly", width=8).grid(row=1, column=5, padx=4, pady=3)

        # Checkboxes
        chk_row = tk.Frame(self.body_adv, bg=UITheme.BG_CARD)
        chk_row.pack(fill=tk.X, pady=(2, 4))

        def _c(t, v, parent=chk_row):
            return tk.Checkbutton(
                parent,
                text=t,
                variable=v,
                font=("Segoe UI", 8),
                bg=UITheme.BG_CARD,
                fg=UITheme.TEXT_BODY,
                selectcolor=UITheme.BG_ROOT,
                activebackground=UITheme.BG_CARD,
                activeforeground=UITheme.TEXT_TITLE,
                bd=0,
            )

        _c("CMOS Rolling Shutter", self.var_rolling).pack(side=tk.LEFT, padx=(0, 8))
        _c("Exclude Stationary", self.var_stationary).pack(side=tk.LEFT, padx=(0, 8))
        _c("Tie-Point Cleaning", self.var_filter_tiepoints).pack(side=tk.LEFT, padx=(0, 8))
        _c("Close Mesh Holes (L30)", self.var_close_holes).pack(side=tk.LEFT, padx=(0, 8))
        _c("Texture Hole Filling", self.var_fill_holes).pack(side=tk.LEFT, padx=(0, 8))
        _c("Ghosting Filter", self.var_ghosting).pack(side=tk.LEFT, padx=(0, 8))

        chk_row2 = tk.Frame(self.body_adv, bg=UITheme.BG_CARD)
        chk_row2.pack(fill=tk.X, pady=(2, 4))
        _c("⏸️ Pause After Crop (Inspect in Metashape Pro)", self.var_pause_crop, parent=chk_row2).pack(side=tk.LEFT, padx=(0, 12))
        _c("Force Restart from Scratch", self.var_restart, parent=chk_row2).pack(side=tk.LEFT)

    def _toggle_advanced(self) -> None:
        self.adv_expanded = not self.adv_expanded
        if self.adv_expanded:
            self.body_adv.pack(fill=tk.X, pady=(4, 2))
            self.btn_toggle_adv.config(text="▼ Hide Advanced Parameters")
        else:
            self.body_adv.pack_forget()
            self.btn_toggle_adv.config(text="▶ Show Advanced Parameters (Alignment, Meshing, Texture Flags)")

    # -----------------------------------------------------------------------
    # Step 4: Standalone Export Card
    # -----------------------------------------------------------------------
    def _build_export_card(self, parent: tk.Widget) -> None:
        card = tk.LabelFrame(
            parent,
            text=" STANDALONE RE-BAKE & EXPORT (.PSX) ",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_TITLE,
            bd=1,
            relief=tk.GROOVE,
            padx=12,
            pady=10,
        )
        card.pack(fill=tk.X, pady=(0, 8))

        # PSX File
        row0 = tk.Frame(card, bg=UITheme.BG_CARD)
        row0.pack(fill=tk.X, pady=(0, 2))
        tk.Label(row0, text="Metashape Project File (.psx):", font=("Segoe UI", 9, "bold"), bg=UITheme.BG_CARD, fg=UITheme.TEXT_BODY).pack(side=tk.LEFT)

        btn_browse_psx = tk.Button(
            row0,
            text="📂 Browse .psx...",
            font=("Segoe UI", 9, "bold"),
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
            card,
            textvariable=self.var_psx_path,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_TITLE,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
        )
        ent_psx.pack(fill=tk.X, pady=(2, 6))

        # Destination
        row1 = tk.Frame(card, bg=UITheme.BG_CARD)
        row1.pack(fill=tk.X, pady=(4, 2))
        tk.Label(row1, text="Export Destination Folder:", font=("Segoe UI", 9, "bold"), bg=UITheme.BG_CARD, fg=UITheme.TEXT_BODY).pack(side=tk.LEFT)

        btn_browse_exp = tk.Button(
            row1,
            text="📂 Change Destination...",
            font=("Segoe UI", 8),
            bg=UITheme.BG_ELEVATED,
            fg=UITheme.TEXT_TITLE,
            bd=0,
            padx=10,
            pady=2,
            relief=tk.FLAT,
            command=self._browse_export_dir,
        )
        btn_browse_exp.pack(side=tk.RIGHT)

        ent_exp = tk.Entry(
            card,
            textvariable=self.var_export_out,
            font=("Segoe UI", 9),
            bg=UITheme.BG_INPUT,
            fg=UITheme.TEXT_TITLE,
            insertbackground="white",
            bd=1,
            relief=tk.SOLID,
        )
        ent_exp.pack(fill=tk.X, pady=(2, 6))

        # Re-bake Options
        row2 = tk.Frame(card, bg=UITheme.BG_CARD)
        row2.pack(fill=tk.X, pady=4)

        tk.Checkbutton(
            row2,
            text="Re-Bake Texture Atlas",
            variable=self.var_exp_rebake,
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_BODY,
            selectcolor=UITheme.BG_ROOT,
            bd=0,
        ).pack(side=tk.LEFT, padx=(0, 12))

        tk.Label(row2, text="Pages:", font=("Segoe UI", 8), bg=UITheme.BG_CARD, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(row2, textvariable=self.var_exp_pages, values=["16", "8", "4", "2", "32"], state="readonly", width=6).pack(side=tk.LEFT, padx=(4, 12))

        tk.Label(row2, text="Format:", font=("Segoe UI", 8), bg=UITheme.BG_CARD, fg=UITheme.TEXT_MUTED).pack(side=tk.LEFT)
        ttk.Combobox(row2, textvariable=self.var_exp_format, values=["tiff", "jpeg"], state="readonly", width=8).pack(side=tk.LEFT, padx=4)

    # -----------------------------------------------------------------------
    # Step 5: Stepper & Trajectory
    # -----------------------------------------------------------------------
    def _build_stepper_card(self, parent: tk.Widget) -> None:
        card = tk.LabelFrame(
            parent,
            text=" PIPELINE PROGRESS (7 STAGES) ",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_TITLE,
            bd=1,
            relief=tk.GROOVE,
            padx=8,
            pady=6,
        )
        card.pack(fill=tk.X, pady=(0, 8))

        self.stepper = HorizontalStepper(card)
        self.stepper.pack(fill=tk.X)

    # -----------------------------------------------------------------------
    # Step 6: Console Feed & Deliverables
    # -----------------------------------------------------------------------
    def _build_log_card(self, parent: tk.Widget) -> None:
        card = tk.LabelFrame(
            parent,
            text=" REAL-TIME EXECUTION LOG ",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_CARD,
            fg=UITheme.TEXT_TITLE,
            bd=1,
            relief=tk.GROOVE,
            padx=10,
            pady=6,
        )
        card.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # Console ScrolledText
        self.txt_log = ScrolledText(
            card,
            wrap=tk.WORD,
            bg=UITheme.BG_CONSOLE,
            fg=UITheme.TEXT_BODY,
            insertbackground="white",
            font=("Consolas", 9),
            bd=0,
            highlightthickness=0,
            height=11,
        )
        self.txt_log.pack(fill=tk.BOTH, expand=True, pady=(0, 6))

        # Syntax tags
        self.txt_log.tag_config("stage", foreground=UITheme.ACCENT_CYAN, font=("Consolas", 9, "bold"))
        self.txt_log.tag_config("success", foreground=UITheme.ACCENT_GREEN, font=("Consolas", 9, "bold"))
        self.txt_log.tag_config("warning", foreground=UITheme.ACCENT_AMBER)
        self.txt_log.tag_config("error", foreground=UITheme.ACCENT_RED, font=("Consolas", 9, "bold"))

        # Action bar under console
        action_bar = tk.Frame(card, bg=UITheme.BG_CARD)
        action_bar.pack(fill=tk.X)

        self.btn_open_folder = tk.Button(
            action_bar,
            text="📂 Open Output Folder",
            font=("Segoe UI", 8, "bold"),
            bg=UITheme.BG_ELEVATED,
            fg=UITheme.TEXT_TITLE,
            bd=0,
            padx=10,
            pady=4,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._on_open_output_folder,
        )
        self.btn_open_folder.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_open_metashape = tk.Button(
            action_bar,
            text="🚀 Open in Metashape GUI",
            font=("Segoe UI", 8, "bold"),
            bg="#1f3725",
            fg="#3fb950",
            bd=0,
            padx=10,
            pady=4,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._on_open_metashape_gui,
        )
        self.btn_open_metashape.pack(side=tk.LEFT, padx=(0, 6))

        btn_copy = tk.Button(
            action_bar,
            text="📋 Copy Log",
            font=("Segoe UI", 8),
            bg=UITheme.BG_ELEVATED,
            fg=UITheme.TEXT_MUTED,
            bd=0,
            padx=8,
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
            bg=UITheme.BG_ELEVATED,
            fg=UITheme.TEXT_MUTED,
            bd=0,
            padx=8,
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
        bar = tk.Frame(self.root, bg=UITheme.BG_CARD, height=64)
        bar.pack(fill=tk.X, side=tk.BOTTOM, padx=0, pady=0)

        inner = tk.Frame(bar, bg=UITheme.BG_CARD)
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
            bg=UITheme.BG_ELEVATED,
            fg=UITheme.TEXT_MUTED,
            activebackground=UITheme.ACCENT_RED,
            activeforeground="white",
            bd=0,
            padx=12,
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
            fg="white",
            activebackground=UITheme.ACCENT_BLUE,
            activeforeground="white",
            bd=0,
            padx=16,
            pady=8,
            relief=tk.FLAT,
            cursor="hand2",
            command=self._on_continue_crop_clicked,
        )

        self.progress_bar = ttk.Progressbar(inner, mode="indeterminate", length=220)
        self.progress_bar.pack(side=tk.LEFT, padx=(0, 16))

        self.lbl_status = tk.Label(
            inner,
            text="Status: Ready to reconstruct",
            font=("Segoe UI", 9, "bold"),
            bg=UITheme.BG_CARD,
            fg="#10b981",
        )
        self.lbl_status.pack(side=tk.RIGHT)

    # -----------------------------------------------------------------------
    # Event Handlers & Logics
    # -----------------------------------------------------------------------
    def _select_preset(self, key: str) -> None:
        """Visual card selection logic."""
        for p_key, frame in self.preset_frames.items():
            if p_key == key:
                frame.config(bg=UITheme.BG_CARD_ACTIVE, bd=2, highlightbackground=UITheme.ACCENT_CYAN)
                for child in frame.winfo_children():
                    child.config(bg=UITheme.BG_CARD_ACTIVE)
            else:
                frame.config(bg=UITheme.BG_ROOT, bd=1, highlightbackground=UITheme.BORDER)
                for child in frame.winfo_children():
                    child.config(bg=UITheme.BG_ROOT)

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
            self.btn_tab_recon.config(bg=UITheme.ACCENT_BLUE, fg="white")
            self.btn_tab_export.config(bg=UITheme.BG_CARD, fg=UITheme.TEXT_MUTED)
            self.wrapper_export.pack_forget()
            self.wrapper_recon.pack(fill=tk.X, expand=True)
            self.btn_start.config(text="🚀  Start 3D Reconstruction", bg=UITheme.ACCENT_GREEN)
        else:
            self.btn_tab_export.config(bg=UITheme.ACCENT_BLUE, fg="white")
            self.btn_tab_recon.config(bg=UITheme.BG_CARD, fg=UITheme.TEXT_MUTED)
            self.wrapper_recon.pack_forget()
            self.wrapper_export.pack(fill=tk.X, expand=True)
            self.btn_start.config(text="📦  Re-Bake Texture & Export Assets", bg=UITheme.ACCENT_BLUE)

    def _check_metashape(self) -> None:
        if self.metashape_exe:
            self.lbl_metashape_badge.config(
                text="● Metashape Pro Connected",
                bg="#064e3b",
                fg="#6ee7b7",
            )
            self._log(f"[AutoMetashape Studio] System initialized with Metashape: {self.metashape_exe}", "stage")
        else:
            self.lbl_metashape_badge.config(
                text="▲ Metashape Not Found",
                bg="#451a03",
                fg=UITheme.ACCENT_AMBER,
            )
            self._log("[WARNING] Metashape executable not found in default paths. You will be prompted to locate it on start.", "warning")

    def _browse_images_dir(self) -> None:
        chosen = filedialog.askdirectory(title="Select Raw Photos Directory", initialdir=self.var_images_dir.get().strip() or os.path.expanduser("~"))
        if chosen:
            self.var_images_dir.set(os.path.normpath(chosen))

    def _browse_output_dir(self) -> None:
        chosen = filedialog.askdirectory(title="Select Output Directory", initialdir=self.var_output_dir.get().strip() or self.var_images_dir.get().strip() or os.path.expanduser("~"))
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
        chosen = filedialog.askdirectory(title="Select Export Directory", initialdir=self.var_export_out.get().strip() or os.path.expanduser("~"))
        if chosen:
            self.var_export_out.set(os.path.normpath(chosen))

    def _on_images_dir_changed(self, *_args) -> None:
        folder = self.var_images_dir.get().strip()
        if not folder or not os.path.isdir(folder):
            self.lbl_photo_status.set("No folder selected yet. Click 'Browse Folder' to begin.")
            return

        # Auto-set output folder
        default_out = os.path.join(folder, "model")
        cur_out = self.var_output_dir.get().strip()
        if not cur_out or cur_out.endswith("model"):
            self.var_output_dir.set(default_out)

        # Scan photos
        def _scan():
            count = 0
            for root, dirs, files in os.walk(folder):
                dirs[:] = [
                    d for d in dirs
                    if not os.path.abspath(os.path.join(root, d)).startswith(os.path.abspath(default_out))
                    and not d.lower().startswith("model")
                ]
                if os.path.abspath(root).startswith(os.path.abspath(default_out)) or os.path.basename(root).lower().startswith("model"):
                    continue
                for f in files:
                    if os.path.splitext(f)[1].lower() in SUPPORTED_IMAGE_EXTS:
                        count += 1
            if count > 0:
                self.lbl_photo_status.set(f"✓ Found {count:,} photos in folder and subfolders (ready for reconstruction)")
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
        psx = os.path.join(folder, "building_reconstruction.psx") if self.active_mode == "recon" else self.var_psx_path.get().strip()

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

    # -----------------------------------------------------------------------
    # Subprocess Execution & Live Stepper Parsing
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
        self._spawn_worker(cmd, "Export & Re-Bake")

    def _spawn_worker(self, cmd: list[str], task_name: str) -> None:
        self.is_running = True
        self.start_time = time.time()
        self.btn_start.config(state=tk.DISABLED, bg=UITheme.BG_ELEVATED)
        self.btn_stop.config(state=tk.NORMAL, bg=UITheme.ACCENT_RED, fg="white")
        self.progress_bar.start(10)
        self.lbl_status.config(text=f"Status: Running {task_name}...", fg=UITheme.ACCENT_CYAN)

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
                    self.log_queue.put(f"\n[ERROR] Process exited with error code {code}")
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
        elif "[pause_crop]" in l:
            self.root.after(0, lambda: self.stepper.set_active(2, "Paused: Manual Crop in Metashape"))
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
                    elif "[WARNING]" in line or "[Hardware]" in line:
                        tag = "warning"
                    elif "[ERROR]" in line or "[EXCEPTION]" in line:
                        tag = "error"
                    self._log(line, tag)
            except queue.Empty:
                break
        self.root.after(80, self._consume_log_queue)

    def _on_pipeline_paused_for_crop(self) -> None:
        self.lbl_status.config(
            text="Status: ⏸️ Paused — Open Metashape Pro to Crop, then click Continue",
            fg=UITheme.ACCENT_AMBER,
        )
        self.progress_bar.stop()
        self.btn_continue_crop.pack(side=tk.LEFT, padx=(0, 10), before=self.btn_stop)
        self._log("\n" + "═" * 70, "warning")
        self._log("⏸️  PIPELINE PAUSED: MANUAL CROPPING IN AGISOFT METASHAPE PRO", "warning")
        self._log("1. Click '🚀 Open in Metashape GUI' above to open your project in Metashape Pro.", "stage")
        self._log("2. In Metashape, use 'Resize Region' / 'Rotate Region' tools to crop the building.", "stage")
        self._log("3. Press Ctrl+S in Metashape Pro to save, then CLOSE Metashape Pro.", "stage")
        self._log("4. Click '▶️ Continue Reconstruction (Manual Crop Done)' below to resume 600K meshing.\n" + "═" * 70, "success")

        # Visual and audio alert to notify user
        try:
            self.root.bell()
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after_idle(lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass

        # Interactive popup notification dialog
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
        self.progress_bar.start(10)
        self.lbl_status.config(
            text="Status: Resuming Reconstruction with Manual Box...",
            fg=UITheme.ACCENT_CYAN,
        )
        self._log("[USER] Sent resume signal. Continuing pipeline with manual bounding box...", "stage")

        # 1. Send newline to process stdin
        if self.current_process and self.current_process.stdin:
            try:
                self.current_process.stdin.write("\n")
                self.current_process.stdin.flush()
            except Exception as e:
                self._log(f"[Warning] stdin write: {e}", "warning")

        # 2. Write sentinel file to output folder as reliable fallback
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
        self.btn_start.config(
            state=tk.NORMAL,
            bg=UITheme.ACCENT_GREEN if self.active_mode == "recon" else UITheme.ACCENT_BLUE,
        )
        self.btn_stop.config(state=tk.DISABLED, bg=UITheme.BG_ELEVATED, fg=UITheme.TEXT_MUTED)

        if user_cancelled:
            self.lbl_status.config(text="Status: Cancelled by user", fg=UITheme.ACCENT_AMBER)
        elif success:
            self.stepper.set_all_done()
            self.lbl_status.config(text="Status: Completed Successfully!", fg=UITheme.ACCENT_GREEN)
            messagebox.showinfo("Completed", "Reconstruction finished successfully!\nDeliverables are saved in your output folder.")
        else:
            self.lbl_status.config(text="Status: Process Failed (See Log)", fg=UITheme.ACCENT_RED)
            messagebox.showerror("Failed", "Process encountered an error. Check console log for details.")


def main() -> None:
    root = tk.Tk()
    app = AutoMetashapeStudio(root)
    root.mainloop()


if __name__ == "__main__":
    main()
