# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape Graphical User Interface (GUI).

Native desktop application for configuring and executing autonomous
photogrammetry 3D reconstruction and export pipelines using Agisoft Metashape Pro.
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

# Default executable location
DEFAULT_METASHAPE_PATHS = [
    r"C:\Program Files\Agisoft\Metashape Pro\metashape.exe",
    r"C:\Program Files\Agisoft\PhotoScan Pro\photoscan.exe",
]


def find_metashape_executable() -> str | None:
    """Locate Metashape Professional executable on the host system."""
    for path in DEFAULT_METASHAPE_PATHS:
        if os.path.isfile(path):
            return path
    which_path = shutil.which("metashape")
    if which_path and os.path.isfile(which_path):
        return which_path
    return None


class AutoMetashapeGUI:
    """Tkinter-based GUI for AutoMetashape photogrammetry pipeline."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("AutoMetashape - 3D Photogrammetry Studio")
        self.root.geometry("960x780")
        self.root.minsize(800, 620)

        # Base directories
        self.repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.recon_script = os.path.join(self.repo_dir, "scripts", "metashape_reconstruction.py")
        self.export_script = os.path.join(self.repo_dir, "scripts", "metashape_export.py")

        # Subprocess tracking
        self.current_process: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.is_running = False
        self.start_time: float = 0.0

        # Metashape path
        self.metashape_exe = find_metashape_executable()

        self._init_style()
        self._build_ui()
        self._check_metashape_installed()

        # Start periodic log polling
        self.root.after(100, self._process_log_queue)

    def _init_style(self) -> None:
        """Initialize ttk styling."""
        self.style = ttk.Style(self.root)
        available_themes = self.style.theme_names()
        if "vista" in available_themes:
            self.style.theme_use("vista")
        elif "clam" in available_themes:
            self.style.theme_use("clam")

        self.style.configure("Title.TLabel", font=("Segoe UI", 13, "bold"))
        self.style.configure("Subtitle.TLabel", font=("Segoe UI", 9), foreground="#555555")
        self.style.configure("Header.TLabel", font=("Segoe UI", 10, "bold"))
        self.style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
        self.style.configure("Action.TButton", font=("Segoe UI", 10, "bold"), padding=6)

    def _build_ui(self) -> None:
        """Build main UI layout."""
        # Top Header Frame
        header_frame = ttk.Frame(self.root, padding="12 8 12 4")
        header_frame.pack(fill=tk.X)

        title_label = ttk.Label(header_frame, text="AutoMetashape 3D Photogrammetry Studio", style="Title.TLabel")
        title_label.pack(anchor=tk.W)

        subtitle_label = ttk.Label(
            header_frame,
            text="Autonomous high-precision architectural photogrammetry pipeline (16-Page 8K textures, 1M faces, GPS crop)",
            style="Subtitle.TLabel",
        )
        subtitle_label.pack(anchor=tk.W)

        # Notebook (Tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=False, padx=12, pady=6)

        # Tab 1: Full Reconstruction Pipeline
        self.tab_recon = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_recon, text=" 3D Reconstruction Pipeline ")
        self._build_recon_tab(self.tab_recon)

        # Tab 2: Export & Texture Re-baking
        self.tab_export = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_export, text=" Export & Texture Re-Bake (.psx) ")
        self._build_export_tab(self.tab_export)

        # Controls & Execution Frame
        exec_frame = ttk.Frame(self.root, padding="12 4 12 4")
        exec_frame.pack(fill=tk.X)

        btn_box = ttk.Frame(exec_frame)
        btn_box.pack(side=tk.LEFT, fill=tk.X)

        self.btn_start = ttk.Button(
            btn_box,
            text="Start 3D Reconstruction",
            style="Action.TButton",
            command=self._on_start_clicked,
        )
        self.btn_start.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_stop = ttk.Button(
            btn_box,
            text="Cancel / Stop",
            state=tk.DISABLED,
            command=self._on_stop_clicked,
        )
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 8))

        self.progress_bar = ttk.Progressbar(btn_box, mode="indeterminate", length=180)
        self.progress_bar.pack(side=tk.LEFT, padx=(4, 12))

        self.lbl_status = ttk.Label(exec_frame, text="Status: Ready", style="Status.TLabel", foreground="#2e7d32")
        self.lbl_status.pack(side=tk.RIGHT)

        # Console Log Frame
        log_frame = ttk.LabelFrame(self.root, text=" Pipeline Execution Console ", padding="8 6 8 8")
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 6))

        self.txt_log = ScrolledText(
            log_frame,
            wrap=tk.WORD,
            background="#1e1e1e",
            foreground="#d4d4d4",
            insertbackground="white",
            font=("Consolas", 9),
            height=12,
        )
        self.txt_log.pack(fill=tk.BOTH, expand=True)

        # Utility Buttons Below Console
        util_frame = ttk.Frame(self.root, padding="12 2 12 10")
        util_frame.pack(fill=tk.X)

        self.btn_open_output = ttk.Button(
            util_frame,
            text="Open Output Folder",
            command=self._on_open_output_folder,
        )
        self.btn_open_output.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_open_gui = ttk.Button(
            util_frame,
            text="Open in Metashape GUI",
            command=self._on_open_in_metashape_gui,
        )
        self.btn_open_gui.pack(side=tk.LEFT, padx=(0, 6))

        btn_clear_log = ttk.Button(
            util_frame,
            text="Clear Console",
            command=self._clear_log,
        )
        btn_clear_log.pack(side=tk.RIGHT)

        # Update button text on tab switch
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _build_recon_tab(self, parent: ttk.Frame) -> None:
        """Build contents of the 3D Reconstruction tab."""
        # Folder Selection Frame
        dir_frame = ttk.LabelFrame(parent, text=" Input / Output Directories ", padding=8)
        dir_frame.pack(fill=tk.X, pady=(0, 6))

        # Images Directory
        ttk.Label(dir_frame, text="Images Directory:").grid(row=0, column=0, sticky=tk.W, pady=3)
        self.var_images_dir = tk.StringVar()
        self.var_images_dir.trace_add("write", self._on_images_dir_changed)
        ent_images = ttk.Entry(dir_frame, textvariable=self.var_images_dir, width=65)
        ent_images.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=3)
        btn_browse_images = ttk.Button(dir_frame, text="Browse...", command=self._browse_images_dir)
        btn_browse_images.grid(row=0, column=2, padx=2, pady=3)

        # Output Directory
        ttk.Label(dir_frame, text="Output Directory:").grid(row=1, column=0, sticky=tk.W, pady=3)
        self.var_output_dir = tk.StringVar()
        ent_output = ttk.Entry(dir_frame, textvariable=self.var_output_dir, width=65)
        ent_output.grid(row=1, column=1, sticky=tk.EW, padx=6, pady=3)
        btn_browse_output = ttk.Button(dir_frame, text="Browse...", command=self._browse_output_dir)
        btn_browse_output.grid(row=1, column=2, padx=2, pady=3)

        dir_frame.columnconfigure(1, weight=1)

        # Parameters Frame
        params_frame = ttk.LabelFrame(parent, text=" Reconstruction Parameters ", padding=8)
        params_frame.pack(fill=tk.X, pady=(0, 4))

        # Row 1: Alignment Quality, Face Count, Texture Pages, Auto-Crop
        ttk.Label(params_frame, text="Align Quality:").grid(row=0, column=0, sticky=tk.W, padx=4, pady=3)
        self.var_align_quality = tk.StringVar(value="medium")
        cbo_align = ttk.Combobox(params_frame, textvariable=self.var_align_quality, values=["medium", "high", "highest", "low"], state="readonly", width=12)
        cbo_align.grid(row=0, column=1, sticky=tk.W, padx=4, pady=3)

        ttk.Label(params_frame, text="Target Faces:").grid(row=0, column=2, sticky=tk.W, padx=8, pady=3)
        self.var_faces = tk.StringVar(value="1000000")
        cbo_faces = ttk.Combobox(params_frame, textvariable=self.var_faces, values=["1000000", "500000", "2000000", "3000000"], width=12)
        cbo_faces.grid(row=0, column=3, sticky=tk.W, padx=4, pady=3)

        ttk.Label(params_frame, text="Texture Pages:").grid(row=0, column=4, sticky=tk.W, padx=8, pady=3)
        self.var_tex_pages = tk.StringVar(value="16")
        cbo_pages = ttk.Combobox(params_frame, textvariable=self.var_tex_pages, values=["16", "8", "4", "2", "32"], state="readonly", width=10)
        cbo_pages.grid(row=0, column=5, sticky=tk.W, padx=4, pady=3)

        # Row 2: Crop Method, Depth Quality, Depth Filtering
        ttk.Label(params_frame, text="Auto-Crop Method:").grid(row=1, column=0, sticky=tk.W, padx=4, pady=3)
        self.var_crop = tk.StringVar(value="gps-square")
        cbo_crop = ttk.Combobox(params_frame, textvariable=self.var_crop, values=["gps-square", "structure", "none"], state="readonly", width=12)
        cbo_crop.grid(row=1, column=1, sticky=tk.W, padx=4, pady=3)

        ttk.Label(params_frame, text="Depth Quality:").grid(row=1, column=2, sticky=tk.W, padx=8, pady=3)
        self.var_depth_quality = tk.StringVar(value="medium")
        cbo_depth = ttk.Combobox(params_frame, textvariable=self.var_depth_quality, values=["medium", "high", "ultra", "low"], state="readonly", width=12)
        cbo_depth.grid(row=1, column=3, sticky=tk.W, padx=4, pady=3)

        ttk.Label(params_frame, text="Depth Filtering:").grid(row=1, column=4, sticky=tk.W, padx=8, pady=3)
        self.var_depth_filtering = tk.StringVar(value="moderate")
        cbo_filter = ttk.Combobox(params_frame, textvariable=self.var_depth_filtering, values=["moderate", "mild", "aggressive"], state="readonly", width=10)
        cbo_filter.grid(row=1, column=5, sticky=tk.W, padx=4, pady=3)

        # Checkboxes Frame
        opts_frame = ttk.Frame(params_frame, padding="0 4 0 0")
        opts_frame.grid(row=2, column=0, columnspan=6, sticky=tk.W, pady=(4, 0))

        self.var_rolling = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_frame, text="CMOS Rolling Shutter", variable=self.var_rolling).grid(row=0, column=0, sticky=tk.W, padx=(4, 12))

        self.var_stationary = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_frame, text="Exclude Stationary Points", variable=self.var_stationary).grid(row=0, column=1, sticky=tk.W, padx=(0, 12))

        self.var_close_holes = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_frame, text="Close Mesh Holes (Level 30)", variable=self.var_close_holes).grid(row=0, column=2, sticky=tk.W, padx=(0, 12))

        self.var_fill_holes = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_frame, text="Fill Texture Holes", variable=self.var_fill_holes).grid(row=0, column=3, sticky=tk.W, padx=(0, 12))

        self.var_ghosting = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_frame, text="Ghosting Filter", variable=self.var_ghosting).grid(row=0, column=4, sticky=tk.W)

        # Advanced options row
        opts_frame2 = ttk.Frame(params_frame, padding="0 2 0 0")
        opts_frame2.grid(row=3, column=0, columnspan=6, sticky=tk.W, pady=(2, 0))

        self.var_pause_crop = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts_frame2, text="Pause after crop for manual GUI inspection", variable=self.var_pause_crop).grid(row=0, column=0, sticky=tk.W, padx=(4, 12))

        self.var_rebake = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts_frame2, text="Force re-bake texture on existing project", variable=self.var_rebake).grid(row=0, column=1, sticky=tk.W, padx=(0, 12))

        self.var_restart = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts_frame2, text="Force restart from scratch", variable=self.var_restart).grid(row=0, column=2, sticky=tk.W)

    def _build_export_tab(self, parent: ttk.Frame) -> None:
        """Build contents of the Project Export & Texture Re-baking tab."""
        box_frame = ttk.LabelFrame(parent, text=" Project File & Export Settings ", padding=8)
        box_frame.pack(fill=tk.X, pady=(0, 6))

        # Project File (.psx)
        ttk.Label(box_frame, text="Metashape Project (.psx):").grid(row=0, column=0, sticky=tk.W, pady=4)
        self.var_psx_path = tk.StringVar()
        self.var_psx_path.trace_add("write", self._on_psx_path_changed)
        ent_psx = ttk.Entry(box_frame, textvariable=self.var_psx_path, width=65)
        ent_psx.grid(row=0, column=1, sticky=tk.EW, padx=6, pady=4)
        btn_browse_psx = ttk.Button(box_frame, text="Browse...", command=self._browse_psx_file)
        btn_browse_psx.grid(row=0, column=2, padx=2, pady=4)

        # Export Output Directory
        ttk.Label(box_frame, text="Export Destination:").grid(row=1, column=0, sticky=tk.W, pady=4)
        self.var_export_out_dir = tk.StringVar()
        ent_export_out = ttk.Entry(box_frame, textvariable=self.var_export_out_dir, width=65)
        ent_export_out.grid(row=1, column=1, sticky=tk.EW, padx=6, pady=4)
        btn_browse_exp_out = ttk.Button(box_frame, text="Browse...", command=self._browse_export_out_dir)
        btn_browse_exp_out.grid(row=1, column=2, padx=2, pady=4)

        box_frame.columnconfigure(1, weight=1)

        # Export Options
        opt_box = ttk.LabelFrame(parent, text=" Texture Re-Baking Options ", padding=8)
        opt_box.pack(fill=tk.X, pady=(0, 4))

        self.var_exp_rebake = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_box, text="Re-bake 8K Texture Atlas before export", variable=self.var_exp_rebake).grid(row=0, column=0, sticky=tk.W, padx=4, pady=4)

        ttk.Label(opt_box, text="Texture Pages:").grid(row=0, column=1, sticky=tk.W, padx=(16, 4), pady=4)
        self.var_exp_pages = tk.StringVar(value="16")
        cbo_exp_pages = ttk.Combobox(opt_box, textvariable=self.var_exp_pages, values=["16", "8", "4", "2", "32"], state="readonly", width=8)
        cbo_exp_pages.grid(row=0, column=2, sticky=tk.W, padx=4, pady=4)

        ttk.Label(opt_box, text="Format:").grid(row=0, column=3, sticky=tk.W, padx=(16, 4), pady=4)
        self.var_exp_format = tk.StringVar(value="tiff")
        cbo_exp_fmt = ttk.Combobox(opt_box, textvariable=self.var_exp_format, values=["tiff", "jpeg"], state="readonly", width=8)
        cbo_exp_fmt.grid(row=0, column=4, sticky=tk.W, padx=4, pady=4)

        ttk.Label(
            parent,
            text="Exports binary glTF (.glb) with embedded PBR textures, Wavefront OBJ (.obj + .mtl), and lossless 16-page 8K TIFFs.",
            style="Subtitle.TLabel",
        ).pack(anchor=tk.W, pady=4)

    # -------------------------------------------------------------------------
    # UI Callbacks & Helpers
    # -------------------------------------------------------------------------
    def _check_metashape_installed(self) -> None:
        """Verify Metashape is available."""
        if self.metashape_exe:
            self._log(f"[AutoMetashape] Found Metashape executable: {self.metashape_exe}")
        else:
            self._log("[WARNING] Metashape executable not found in default paths! Please locate metashape.exe.")
            self.lbl_status.config(text="Status: Metashape not found", foreground="#d32f2f")

    def _browse_images_dir(self) -> None:
        initial = self.var_images_dir.get().strip() or os.path.expanduser("~")
        chosen = filedialog.askdirectory(title="Select Raw Image Folder", initialdir=initial)
        if chosen:
            self.var_images_dir.set(os.path.normpath(chosen))

    def _browse_output_dir(self) -> None:
        initial = self.var_output_dir.get().strip() or self.var_images_dir.get().strip() or os.path.expanduser("~")
        chosen = filedialog.askdirectory(title="Select Output Folder", initialdir=initial)
        if chosen:
            self.var_output_dir.set(os.path.normpath(chosen))

    def _browse_psx_file(self) -> None:
        initial = os.path.dirname(self.var_psx_path.get().strip()) or os.path.expanduser("~")
        chosen = filedialog.askopenfilename(
            title="Select Metashape Project",
            filetypes=[("Metashape Project", "*.psx"), ("All Files", "*.*")],
            initialdir=initial,
        )
        if chosen:
            self.var_psx_path.set(os.path.normpath(chosen))

    def _browse_export_out_dir(self) -> None:
        initial = self.var_export_out_dir.get().strip() or os.path.expanduser("~")
        chosen = filedialog.askdirectory(title="Select Export Destination Folder", initialdir=initial)
        if chosen:
            self.var_export_out_dir.set(os.path.normpath(chosen))

    def _on_images_dir_changed(self, *_args) -> None:
        """Auto-populate output directory to <images_dir>/model when image folder is selected."""
        img_dir = self.var_images_dir.get().strip()
        if img_dir and os.path.isdir(img_dir):
            default_out = os.path.join(img_dir, "model")
            current_out = self.var_output_dir.get().strip()
            if not current_out or current_out.endswith("model"):
                self.var_output_dir.set(default_out)

    def _on_psx_path_changed(self, *_args) -> None:
        """Auto-populate export directory to project folder."""
        psx_path = self.var_psx_path.get().strip()
        if psx_path:
            p_dir = os.path.dirname(psx_path)
            if not self.var_export_out_dir.get().strip():
                self.var_export_out_dir.set(p_dir)

    def _on_tab_changed(self, _event=None) -> None:
        """Update action button label depending on active tab."""
        tab_idx = self.notebook.index(self.notebook.select())
        if tab_idx == 0:
            self.btn_start.config(text="Start 3D Reconstruction")
        else:
            self.btn_start.config(text="Re-Bake & Export Assets")

    def _log(self, text: str) -> None:
        """Append log line to UI console."""
        self.txt_log.insert(tk.END, text + "\n")
        self.txt_log.see(tk.END)

    def _clear_log(self) -> None:
        self.txt_log.delete("1.0", tk.END)

    def _on_open_output_folder(self) -> None:
        """Open the output folder in Windows Explorer."""
        tab_idx = self.notebook.index(self.notebook.select())
        if tab_idx == 0:
            target = self.var_output_dir.get().strip()
        else:
            target = self.var_export_out_dir.get().strip()

        if target and os.path.isdir(target):
            try:
                os.startfile(target)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to open folder: {e}")
        else:
            messagebox.showinfo("Folder Not Found", f"Output directory does not exist yet:\n{target}")

    def _on_open_in_metashape_gui(self) -> None:
        """Open the current .psx project in Agisoft Metashape Professional GUI."""
        if not self.metashape_exe or not os.path.isfile(self.metashape_exe):
            messagebox.showerror("Error", "Metashape executable not found.")
            return

        tab_idx = self.notebook.index(self.notebook.select())
        if tab_idx == 0:
            out_dir = self.var_output_dir.get().strip()
            psx_path = os.path.join(out_dir, "building_reconstruction.psx")
        else:
            psx_path = self.var_psx_path.get().strip()

        if psx_path and os.path.isfile(psx_path):
            self._log(f"[GUI] Launching Metashape GUI: {psx_path}")
            subprocess.Popen([self.metashape_exe, psx_path])
        else:
            messagebox.showinfo("Project Not Found", f"No Metashape project found at:\n{psx_path}")

    # -------------------------------------------------------------------------
    # Command Construction & Execution
    # -------------------------------------------------------------------------
    def _on_start_clicked(self) -> None:
        """Dispatch execution based on the active tab."""
        if not self.metashape_exe or not os.path.isfile(self.metashape_exe):
            chosen = filedialog.askopenfilename(
                title="Locate metashape.exe",
                filetypes=[("Executable", "metashape.exe"), ("All Files", "*.*")],
            )
            if chosen and os.path.isfile(chosen):
                self.metashape_exe = os.path.normpath(chosen)
            else:
                messagebox.showerror("Error", "Cannot proceed without a valid Metashape executable.")
                return

        tab_idx = self.notebook.index(self.notebook.select())
        if tab_idx == 0:
            self._start_reconstruction()
        else:
            self._start_export()

    def _start_reconstruction(self) -> None:
        """Construct arguments and launch 3D reconstruction script."""
        images_dir = self.var_images_dir.get().strip()
        if not images_dir or not os.path.isdir(images_dir):
            messagebox.showwarning("Input Required", "Please select a valid raw images directory.")
            return

        out_dir = self.var_output_dir.get().strip()
        if not out_dir:
            out_dir = os.path.join(images_dir, "model")
            self.var_output_dir.set(out_dir)

        # Build command line
        cmd = [
            self.metashape_exe,
            "-r",
            self.recon_script,
            "--images",
            images_dir,
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

        self._execute_subprocess(cmd, task_name="3D Reconstruction Pipeline")

    def _start_export(self) -> None:
        """Construct arguments and launch asset export script."""
        psx_path = self.var_psx_path.get().strip()
        if not psx_path or not os.path.isfile(psx_path):
            messagebox.showwarning("Input Required", "Please select a valid Metashape project file (.psx).")
            return

        out_dir = self.var_export_out_dir.get().strip()
        if not out_dir:
            out_dir = os.path.dirname(psx_path)
            self.var_export_out_dir.set(out_dir)

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

        self._execute_subprocess(cmd, task_name="Asset Export & Re-Baking")

    def _execute_subprocess(self, cmd: list[str], task_name: str) -> None:
        """Spawn background process and pipe live stdout to console."""
        self.is_running = True
        self.start_time = time.time()
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.progress_bar.start(10)
        self.lbl_status.config(text=f"Status: Running {task_name}...", foreground="#1565c0")

        self._log(f"\n[{task_name}] Starting execution...")
        self._log(f"Command: {' '.join(cmd)}\n" + "-" * 75)

        def worker() -> None:
            try:
                # Windows-specific creation flag to avoid popping up extra CMD window
                creation_flags = 0
                if sys.platform == "win32":
                    creation_flags = subprocess.CREATE_NO_WINDOW

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
                            self.log_queue.put(line.rstrip())
                    self.current_process.stdout.close()

                return_code = self.current_process.wait()
                elapsed = (time.time() - self.start_time) / 60.0
                if return_code == 0:
                    self.log_queue.put(f"\n[SUCCESS] {task_name} finished successfully in {elapsed:.2f} minutes!")
                    self.log_queue.put("__TASK_DONE_SUCCESS__")
                else:
                    self.log_queue.put(f"\n[ERROR] Process exited with error code: {return_code}")
                    self.log_queue.put("__TASK_DONE_ERROR__")
            except Exception as e:
                self.log_queue.put(f"\n[EXCEPTION] Failed to run process: {e}")
                self.log_queue.put("__TASK_DONE_ERROR__")
            finally:
                self.current_process = None

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

    def _on_stop_clicked(self) -> None:
        """Cancel and terminate the active background subprocess."""
        if self.current_process and self.is_running:
            if messagebox.askyesno("Cancel Process", "Are you sure you want to cancel the running reconstruction?"):
                self._log("\n[USER] Terminating active process...")
                try:
                    self.current_process.terminate()
                    time.sleep(0.5)
                    if self.current_process.poll() is None:
                        self.current_process.kill()
                    self._log("[USER] Process terminated.")
                except Exception as e:
                    self._log(f"[USER] Error terminating process: {e}")
                self._on_task_finished(success=False, user_cancelled=True)

    def _process_log_queue(self) -> None:
        """Consume lines from log queue and update UI."""
        while not self.log_queue.empty():
            try:
                line = self.log_queue.get_nowait()
                if line == "__TASK_DONE_SUCCESS__":
                    self._on_task_finished(success=True)
                elif line == "__TASK_DONE_ERROR__":
                    self._on_task_finished(success=False)
                else:
                    self._log(line)
            except queue.Empty:
                break
        self.root.after(100, self._process_log_queue)

    def _on_task_finished(self, success: bool, user_cancelled: bool = False) -> None:
        """Reset controls and update status label upon completion."""
        self.is_running = False
        self.progress_bar.stop()
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)

        if user_cancelled:
            self.lbl_status.config(text="Status: Cancelled by user", foreground="#e65100")
        elif success:
            self.lbl_status.config(text="Status: Completed Successfully!", foreground="#2e7d32")
            messagebox.showinfo("Finished", "Processing completed successfully!\nDeliverables are saved in the output directory.")
        else:
            self.lbl_status.config(text="Status: Process Failed", foreground="#c62828")
            messagebox.showerror("Failed", "Process encountered an error. Check console logs for details.")


def main() -> None:
    root = tk.Tk()
    app = AutoMetashapeGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
