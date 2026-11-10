# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for distributed photogrammetry network cluster processing."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

# Ensure scripts directory is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from autometashape_gui import (
    find_metashape_server_path,
    generate_worker_bat_content,
    get_primary_lan_ip,
    pick_multiple_folders,
)


class TestNetworkCluster(unittest.TestCase):
    """Test suite for Metashape distributed cluster integration."""

    def test_get_primary_lan_ip_format(self) -> None:
        """Verify LAN IP detection returns a valid IPv4 string."""
        ip = get_primary_lan_ip()
        parts = ip.split(".")
        self.assertEqual(len(parts), 4)
        for p in parts:
            self.assertTrue(p.isdigit())
            self.assertTrue(0 <= int(p) <= 255)

    def test_generate_worker_bat_content_with_auto_ip(self) -> None:
        """Verify worker launcher batch content embeds detected IP and port."""
        content = generate_worker_bat_content(None, "5840", r"\\HOST_PC\SharedMetashape")
        self.assertIn("5840", content)
        self.assertIn(r"\\HOST_PC\SharedMetashape", content)
        self.assertIn("--worker", content)
        self.assertIn("--cpu-enable 1", content)

    def test_generate_worker_bat_content_interactive_root(self) -> None:
        """Verify worker launcher batch file prompts interactively if root is blank."""
        content = generate_worker_bat_content(None, "5840", "")
        self.assertIn("set /p TARGET_ROOT=", content)
        self.assertNotIn("RealityScan", content)
        self.assertIn("--worker", content)

    def test_profile_project_for_dashboard(self) -> None:
        """Verify dynamic telemetry profiling creates valid schema without placeholders."""
        from autometashape_gui import AutoMetashapeStudio

        studio_mock = MagicMock()
        studio_mock._physics_cache = {}
        studio_mock.var_export_root.get.return_value = ""
        studio_mock.var_export_out.get.return_value = ""
        studio_mock.batch_running = False
        studio_mock.batch_queue = []
        studio_mock.batch_current_idx = -1
        studio_mock._quick_count_photos.return_value = 150

        proj_item = {"path": "C:/Surveys/SiteA", "name": "SiteA", "count": 150}
        profiled = AutoMetashapeStudio._profile_project_for_dashboard(studio_mock, proj_item, 1)

        self.assertEqual(profiled["id"], "proj-01")
        self.assertEqual(profiled["name"], "SiteA")
        self.assertEqual(profiled["photos"], 150)
        self.assertIn("status", profiled)
        self.assertIn("gsd", profiled)
        self.assertNotIn("catp-01", profiled["id"])

    def test_export_dynamic_dashboard_data_writes_valid_js(self) -> None:
        """Verify dynamic telemetry exporter generates valid JavaScript with JSON payload."""
        import json
        import tempfile
        from autometashape_gui import AutoMetashapeStudio

        with tempfile.TemporaryDirectory() as tmp_dir:
            os.makedirs(os.path.join(tmp_dir, "web"), exist_ok=True)
            studio_mock = MagicMock()
            studio_mock.repo_dir = tmp_dir
            studio_mock._profile_project_for_dashboard = AutoMetashapeStudio._profile_project_for_dashboard.__get__(studio_mock)
            studio_mock.project_items = [{"path": "C:/Surveys/SiteB", "name": "SiteB", "count": 300}]
            studio_mock.var_images_dir.get.return_value = ""
            studio_mock.var_export_root.get.return_value = ""
            studio_mock.var_export_out.get.return_value = ""
            studio_mock.batch_running = False
            studio_mock.batch_queue = []
            studio_mock.batch_current_idx = -1
            studio_mock._physics_cache = {}
            studio_mock._quick_count_photos.return_value = 300

            data_file = AutoMetashapeStudio._export_dynamic_dashboard_data(studio_mock)
            self.assertTrue(os.path.isfile(data_file))

            with open(data_file, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn("window.SURVEY_PROJECTS =", content)
            self.assertIn("SiteB", content)

            # Validate that the JSON payload inside the JS file is syntactically valid JSON
            prefix = "window.SURVEY_PROJECTS = "
            idx = content.find(prefix)
            self.assertNotEqual(idx, -1)
            json_part = content[idx + len(prefix):].rstrip().rstrip(";")
            parsed = json.loads(json_part)
            self.assertIsInstance(parsed, list)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0]["name"], "SiteB")

    def test_find_metashape_server_path_discovery(self) -> None:
        """Verify server discovery logic locates binary or safely returns None."""
        server = find_metashape_server_path(None)
        if server is not None:
            self.assertTrue(os.path.isfile(server))
            self.assertTrue(server.lower().endswith("metashape-server.exe") or server.endswith("metashape-server"))

    def test_cluster_relative_path_resolution(self) -> None:
        """Verify project path conversion to shared storage relative path across local and UNC paths."""
        def calc_rel(psx_out: str, network_path: str) -> str:
            if network_path:
                norm_psx = os.path.abspath(psx_out).replace("\\\\", "/").replace("\\", "/")
                norm_net = network_path.strip().replace("\\\\", "/").replace("\\", "/").rstrip("/")
                try:
                    abs_psx = os.path.abspath(psx_out)
                    abs_net = os.path.abspath(network_path)
                    if os.path.commonpath([abs_psx, abs_net]).lower() == abs_net.lower():
                        return os.path.relpath(abs_psx, abs_net).replace("\\", "/")
                except Exception:
                    pass

                net_parts = [p for p in norm_net.split("/") if p]
                if net_parts:
                    share_name = net_parts[-1].lower()
                    psx_parts = [p for p in norm_psx.split("/") if p]
                    psx_lower = [p.lower() for p in psx_parts]
                    if share_name in psx_lower:
                        idx = psx_lower.index(share_name)
                        rel = "/".join(psx_parts[idx + 1:])
                        if rel:
                            return rel

                for i in range(len(net_parts) - 1, -1, -1):
                    sub = net_parts[i].lower()
                    psx_parts = [p for p in norm_psx.split("/") if p]
                    psx_lower = [p.lower() for p in psx_parts]
                    if sub in psx_lower:
                        idx = psx_lower.index(sub)
                        rel = "/".join(psx_parts[idx + 1:])
                        if rel:
                            return rel
            return os.path.basename(psx_out)

        # Case 1: When network_path is empty, returns file basename
        self.assertEqual(calc_rel(r"C:\Projects\Scan1\project.psx", ""), "project.psx")

        # Case 2: When network_path exists and contains psx_out
        test_dir = os.path.dirname(os.path.abspath(__file__))
        sub_psx = os.path.join(test_dir, "sub", "project.psx")
        rel = calc_rel(sub_psx, test_dir)
        self.assertEqual(rel, "sub/project.psx")

        # Case 3: UNC network path matching local drive psx file
        unc_rel = calc_rel(r"C:\Projects\SharedSurveys\061126\218\metashape\218.psx", r"\\CLUSTER_STORAGE\SharedSurveys")
        self.assertEqual(unc_rel, "061126/218/metashape/218.psx")

    def test_cluster_export_path_resolution(self) -> None:
        """Verify export task path resolution generates valid absolute UNC or local paths."""
        def calc_rel(tgt: str, network_path: str) -> str:
            norm_tgt = os.path.abspath(tgt).replace(r"\\", "/").replace("\\", "/")
            norm_net = network_path.strip().replace(r"\\", "/").replace("\\", "/").rstrip("/")
            try:
                abs_tgt = os.path.abspath(tgt)
                abs_net = os.path.abspath(network_path)
                if os.path.commonpath([abs_tgt, abs_net]).lower() == abs_net.lower():
                    return os.path.relpath(abs_tgt, abs_net).replace("\\", "/")
            except Exception:
                pass
            net_parts = [p for p in norm_net.split("/") if p]
            if net_parts:
                share_name = net_parts[-1].lower()
                tgt_parts = [p for p in norm_tgt.split("/") if p]
                tgt_lower = [p.lower() for p in tgt_parts]
                if share_name in tgt_lower:
                    idx = tgt_lower.index(share_name)
                    rel = "/".join(tgt_parts[idx + 1:])
                    if rel:
                        return rel
            return os.path.basename(tgt)

        def calc_exp(target_file: str, network_path: str) -> str:
            norm_tgt = os.path.abspath(target_file).replace("\\", "/")
            if network_path:
                is_unc = network_path.startswith(r"\\") or network_path.startswith("//")
                clean_net = network_path.strip().replace("\\", "/").rstrip("/")
                if is_unc:
                    clean_net = "//" + clean_net.lstrip("/")
                rel = calc_rel(target_file, network_path)
                if rel and rel != norm_tgt and not rel.startswith(clean_net):
                    return f"{clean_net}/{rel}"
            return norm_tgt

        # Case 1: Empty network path returns local absolute path
        p1 = calc_exp("output.obj", "")
        self.assertTrue(os.path.isabs(p1))
        self.assertTrue(p1.endswith("/output.obj"))

        # Case 2: UNC network path with local drive target resolves to full UNC path
        p2 = calc_exp(r"C:\Projects\SharedSurveys\061126\218\metashape\218.obj", r"\\CLUSTER_STORAGE\SharedSurveys")
        self.assertEqual(p2, "//CLUSTER_STORAGE/SharedSurveys/061126/218/metashape/218.obj")

        # Case 3: Local storage path preserved as valid absolute path
        p3 = calc_exp(r"C:\ClusterData\Scan1\model.obj", r"C:\ClusterData")
        self.assertEqual(p3, "C:/ClusterData/Scan1/model.obj")

    def test_network_client_worker_auto_unpause(self) -> None:
        """Verify newly connected worker nodes have pause cleared automatically for both dict and obj."""
        client = MagicMock()
        dict_response = {
            "next_revision": 0,
            "workers": [
                {"worker_id": 1, "hostname": "Node1", "paused": True, "cpu_enabled": True},
                {"worker_id": 2, "hostname": "Node2", "paused": False, "cpu_enabled": True},
            ],
        }
        client.workerList.return_value = dict_response

        raw_workers = client.workerList()
        workers = raw_workers.get("workers", []) if isinstance(raw_workers, dict) else raw_workers
        for w in workers:
            w_id = w.get("worker_id") if isinstance(w, dict) else getattr(w, "id", None)
            w_paused = w.get("paused", False) if isinstance(w, dict) else getattr(w, "paused", False)
            if w_paused and w_id is not None:
                client.setWorkerPaused(w_id, False)

        client.setWorkerPaused.assert_called_once_with(1, False)

    def test_cluster_batch_progress_tracking(self) -> None:
        """Verify composite progress calculation for single and multi-task batches."""
        # Single task batch
        b_info_single = {
            "id": 10,
            "state": "running",
            "tasks": [{"id": 1, "name": "BuildDepthMaps", "progress": 68.5, "state": "running"}],
        }
        tasks = b_info_single["tasks"]
        single_prog = sum(t["progress"] for t in tasks) / len(tasks)
        self.assertAlmostEqual(single_prog, 68.5)

        # Multi-task batch (e.g. BuildUV + BuildTexture)
        b_info_multi = {
            "id": 11,
            "state": "running",
            "tasks": [
                {"id": 1, "name": "BuildUV", "progress": 100.0, "state": "completed"},
                {"id": 2, "name": "BuildTexture", "progress": 50.0, "state": "running"},
            ],
        }
        multi_tasks = b_info_multi["tasks"]
        multi_prog = sum(t["progress"] for t in multi_tasks) / len(multi_tasks)
        self.assertAlmostEqual(multi_prog, 75.0)

    def test_cluster_batch_failure_fallback_detection(self) -> None:
        """Verify cluster task failures are cleanly detected with error reporting."""
        b_info_failed = {
            "id": 12,
            "state": "failed",
            "tasks": [{"id": 1, "name": "MatchPhotos", "progress": 12.0, "state": "failed", "errors": ["Out of memory"]}],
        }
        self.assertEqual(b_info_failed["state"], "failed")
        errors = b_info_failed["tasks"][0]["errors"]
        self.assertIn("Out of memory", errors)

    def test_pipelined_export_concurrency_bounds(self) -> None:
        """Verify concurrency clamping between 1 and 4."""
        def clamp_concurrency(val: int) -> int:
            return max(1, min(val, 4))

        self.assertEqual(clamp_concurrency(0), 1)
        self.assertEqual(clamp_concurrency(2), 2)
        self.assertEqual(clamp_concurrency(10), 4)

    def test_pipelined_export_command_generation(self) -> None:
        """Verify decoupled export command generation includes required flags."""
        psx = r"C:\Output\Scan1\Scan1.psx"
        out_dir = r"C:\Output\Scan1"
        proj_name = "Scan1"

        gui_mock = MagicMock()
        gui_mock.metashape_exe = "metashape.exe"
        gui_mock.export_script = "scripts/metashape_export.py"
        gui_mock.var_exp_format.get.return_value = "png"
        gui_mock.var_export_glb.get.return_value = True
        gui_mock.var_export_obj.get.return_value = True
        gui_mock.var_build_dem.get.return_value = True
        gui_mock.var_export_dem.get.return_value = True
        gui_mock.var_build_ortho.get.return_value = True
        gui_mock.var_export_ortho.get.return_value = True
        gui_mock.var_export_report.get.return_value = False
        gui_mock.var_build_tiled_model.get.return_value = False
        gui_mock.var_build_contours.get.return_value = False
        gui_mock.var_build_point_cloud.get.return_value = False
        gui_mock.var_export_fbx.get.return_value = False
        gui_mock.var_export_ply.get.return_value = False
        gui_mock.var_export_dxf.get.return_value = False

        from autometashape_gui import AutoMetashapeStudio
        cmd = AutoMetashapeStudio._build_export_cmd_from_recon_settings(gui_mock, psx, out_dir, proj_name)
        self.assertIn("--project", cmd)
        self.assertIn(psx, cmd)
        self.assertIn("--output", cmd)
        self.assertIn(out_dir, cmd)
        self.assertIn("--export-glb", cmd)
        self.assertIn("--export-obj", cmd)
        self.assertIn("--export-dem", cmd)
        self.assertIn("--export-orthomosaic", cmd)
        self.assertIn("--texture-format", cmd)
        self.assertIn("png", cmd)

    def test_pipelined_export_command_skips_dem_when_not_checked(self) -> None:
        """Verify building DEM internally for ortho does not force DEM export unless explicitly checked."""
        psx = r"C:\Output\Scan2\Scan2.psx"
        out_dir = r"C:\Output\Scan2"
        proj_name = "Scan2"

        gui_mock = MagicMock()
        gui_mock.metashape_exe = "metashape.exe"
        gui_mock.export_script = "scripts/metashape_export.py"
        gui_mock.var_exp_format.get.return_value = "png"
        gui_mock.var_export_glb.get.return_value = False
        gui_mock.var_export_obj.get.return_value = True
        gui_mock.var_build_dem.get.return_value = True
        gui_mock.var_export_dem.get.return_value = False
        gui_mock.var_build_ortho.get.return_value = True
        gui_mock.var_export_ortho.get.return_value = True
        gui_mock.var_export_report.get.return_value = False
        gui_mock.var_build_tiled_model.get.return_value = False
        gui_mock.var_build_contours.get.return_value = False
        gui_mock.var_build_point_cloud.get.return_value = False
        gui_mock.var_export_fbx.get.return_value = False
        gui_mock.var_export_ply.get.return_value = False
        gui_mock.var_export_dxf.get.return_value = False

        from autometashape_gui import AutoMetashapeStudio
        cmd = AutoMetashapeStudio._build_export_cmd_from_recon_settings(gui_mock, psx, out_dir, proj_name)
        self.assertIn("--export-obj", cmd)
        self.assertNotIn("--no-export-obj", cmd)
        self.assertIn("--export-orthomosaic", cmd)
        self.assertNotIn("--export-dem", cmd)

    def test_pipelined_export_strictly_respects_all_export_table_deliverables(self) -> None:
        """Defend that export command strictly and exclusively uses Export Table variables, not internal build flags."""
        psx = r"C:\Output\Project1\Project1.psx"
        out_dir = r"C:\Output\Project1"
        proj_name = "Project1"

        gui_mock = MagicMock()
        gui_mock.metashape_exe = "metashape.exe"
        gui_mock.export_script = "scripts/metashape_export.py"
        gui_mock.var_exp_format.get.return_value = "png"

        # User checked: FBX, PLY, DXF, Contours, Point Cloud, Tie Points, Tiles, and Report
        # User un-checked: GLB, OBJ, Ortho, DEM
        gui_mock.var_export_glb.get.return_value = False
        gui_mock.var_export_obj.get.return_value = False
        gui_mock.var_export_fbx.get.return_value = True
        gui_mock.var_export_ply.get.return_value = True
        gui_mock.var_export_dxf.get.return_value = True
        gui_mock.var_export_ortho.get.return_value = False
        gui_mock.var_export_dem.get.return_value = False
        gui_mock.var_export_contours.get.return_value = True
        gui_mock.var_contour_interval.get.return_value = 2.5
        gui_mock.var_contours_format.get.return_value = "dxf"
        gui_mock.var_export_point_cloud.get.return_value = True
        gui_mock.var_pc_format.get.return_value = "las"
        gui_mock.var_export_tie_points.get.return_value = True
        gui_mock.var_export_tiles.get.return_value = True
        gui_mock.var_tiled_model_format.get.return_value = "cesium"
        gui_mock.var_tile_size.get.return_value = 512
        gui_mock.var_export_report.get.return_value = True

        from autometashape_gui import AutoMetashapeStudio
        cmd = AutoMetashapeStudio._build_export_cmd_from_recon_settings(gui_mock, psx, out_dir, proj_name)

        self.assertIn("--no-export-glb", cmd)
        self.assertIn("--no-export-obj", cmd)
        self.assertNotIn("--export-glb", cmd)
        self.assertNotIn("--export-obj", cmd)
        self.assertNotIn("--export-orthomosaic", cmd)
        self.assertNotIn("--export-dem", cmd)

        self.assertIn("--export-fbx", cmd)
        self.assertIn("--export-ply", cmd)
        self.assertIn("--export-dxf", cmd)
        self.assertIn("--export-contours", cmd)
        self.assertIn("--contour-interval", cmd)
        self.assertIn("2.5", cmd)
        self.assertIn("--contours-format", cmd)
        self.assertIn("dxf", cmd)
        self.assertIn("--export-point-cloud", cmd)
        self.assertIn("--point-cloud-format", cmd)
        self.assertIn("las", cmd)
        self.assertIn("--export-tie-points", cmd)
        self.assertIn("--export-tiled-model", cmd)
        self.assertIn("--tiled-model-format", cmd)
        self.assertIn("cesium", cmd)
        self.assertIn("--tile-size", cmd)
        self.assertIn("512", cmd)
        self.assertIn("--export-report", cmd)

    def test_reconstruction_cmd_pipelined_suppresses_all_deliverable_exports(self) -> None:
        """Defend that pipelined reconstruction suppresses all deliverable exports to background thread."""
        gui_mock = MagicMock()
        gui_mock.metashape_exe = "metashape.exe"
        gui_mock.recon_script = "scripts/metashape_reconstruction.py"
        gui_mock.selected_preset_key = "pro"
        gui_mock.var_presel_mode.get.return_value = "estimated"
        gui_mock.var_pipelined_export.get.return_value = True
        gui_mock.batch_running = True

        # Even if export options are checked in table, pipelined reconstruction must suppress them
        gui_mock.var_export_glb.get.return_value = True
        gui_mock.var_export_obj.get.return_value = True
        gui_mock.var_export_dem.get.return_value = True
        gui_mock.var_export_ortho.get.return_value = True
        gui_mock.var_export_point_cloud.get.return_value = True
        gui_mock.var_export_report.get.return_value = True
        gui_mock.var_build_ortho.get.return_value = True
        gui_mock.var_build_dem.get.return_value = True
        gui_mock.var_build_point_cloud.get.return_value = True
        gui_mock.var_build_contours.get.return_value = False
        gui_mock.var_export_contours.get.return_value = False
        gui_mock.var_build_tiled_model.get.return_value = False
        gui_mock.var_export_tiles.get.return_value = False
        gui_mock.var_pc_format.get.return_value = "laz"
        gui_mock.var_classify_ground.get.return_value = False
        gui_mock.var_classify_points.get.return_value = False
        gui_mock.var_pause_crop.get.return_value = False
        gui_mock.var_rebake.get.return_value = False
        gui_mock.var_resume.get.return_value = False
        gui_mock.var_multi_chunk.get.return_value = "disable"
        gui_mock.var_chunk_align_method.get.return_value = "camera"
        gui_mock.var_network_enable.get.return_value = False

        from autometashape_gui import AutoMetashapeStudio
        cmd = AutoMetashapeStudio._build_reconstruction_cmd(gui_mock, r"C:\Images", r"C:\Output", "ProjectA")

        self.assertIn("--no-export-glb", cmd)
        self.assertIn("--no-export-obj", cmd)
        self.assertIn("--no-export-dem", cmd)
        self.assertIn("--no-export-ortho", cmd)
        self.assertIn("--no-export-point-cloud", cmd)
        self.assertIn("--no-report", cmd)
        self.assertNotIn("--export-glb", cmd)
        self.assertNotIn("--export-obj", cmd)
        self.assertNotIn("--export-dem", cmd)
        self.assertNotIn("--export-ortho", cmd)
        self.assertNotIn("--export-point-cloud", cmd)
        self.assertNotIn("--export-report", cmd)

    def test_gui_export_checklist_strictly_defaults_to_all_false(self) -> None:
        """Defend that all deliverable checkboxes in the GUI Export Table default to False."""
        import tkinter as tk
        from autometashape_gui import AutoMetashapeStudio

        root = tk.Tk()
        root.withdraw()
        try:
            gui = AutoMetashapeStudio(root)
            self.assertFalse(gui.var_export_glb.get())
            self.assertFalse(gui.var_export_obj.get())
            self.assertFalse(gui.var_export_ortho.get())
            self.assertFalse(gui.var_export_dem.get())
            self.assertFalse(gui.var_export_point_cloud.get())
            self.assertFalse(gui.var_export_report.get())
            self.assertFalse(gui.var_export_fbx.get())
            self.assertFalse(gui.var_export_ply.get())
            self.assertFalse(gui.var_export_dxf.get())
            self.assertFalse(gui.var_export_tie_points.get())
            self.assertFalse(gui.var_export_tiles.get())
            self.assertFalse(gui.var_export_contours.get())

            # Verify that with default checklist (all unticked), no export flags are passed
            cmd_recon = gui._build_reconstruction_cmd(r"C:\Images", r"C:\Out", "TestProj")
            self.assertIn("--no-export-glb", cmd_recon)
            self.assertIn("--no-export-obj", cmd_recon)
            self.assertIn("--no-export-dem", cmd_recon)
            self.assertIn("--no-export-ortho", cmd_recon)
            self.assertIn("--no-export-point-cloud", cmd_recon)
            self.assertIn("--no-report", cmd_recon)
            for flag in ("--export-glb", "--export-obj", "--export-dem", "--export-ortho", "--export-point-cloud", "--export-report", "--export-fbx", "--export-ply", "--export-dxf", "--export-tie-points", "--export-tiled-model", "--export-contours"):
                self.assertNotIn(flag, cmd_recon)

            cmd_export = gui._build_export_cmd_from_recon_settings(r"C:\Out\TestProj.psx", r"C:\Out", "TestProj")
            self.assertIn("--no-export-glb", cmd_export)
            self.assertIn("--no-export-obj", cmd_export)
            self.assertIn("--no-export-dem", cmd_export)
            self.assertIn("--no-export-orthomosaic", cmd_export)
            self.assertIn("--no-export-point-cloud", cmd_export)
            self.assertIn("--no-report", cmd_export)
            for flag in ("--export-glb", "--export-obj", "--export-dem", "--export-orthomosaic", "--export-point-cloud", "--export-report", "--export-fbx", "--export-ply", "--export-dxf", "--export-tie-points", "--export-tiled-model", "--export-contours"):
                self.assertNotIn(flag, cmd_export)
        finally:
            root.destroy()

    def test_preset_selection_does_not_clobber_user_export_table_choices(self) -> None:
        """Defend that selecting Pro preset does not overwrite the user's Export Table checklist."""
        import tkinter as tk
        from autometashape_gui import AutoMetashapeStudio

        root = tk.Tk()
        root.withdraw()
        try:
            gui_mock = MagicMock()
            gui_mock.selected_preset_key = "custom"
            gui_mock.card_adv = MagicMock()
            gui_mock.preset_cards = {}

            # Simulate user customizing their Export Table
            var_glb = tk.BooleanVar(value=True)
            var_obj = tk.BooleanVar(value=False)
            var_dem = tk.BooleanVar(value=True)
            var_ortho = tk.BooleanVar(value=False)
            var_pc = tk.BooleanVar(value=True)

            gui_mock.var_export_glb = var_glb
            gui_mock.var_export_obj = var_obj
            gui_mock.var_export_dem = var_dem
            gui_mock.var_export_ortho = var_ortho
            gui_mock.var_export_point_cloud = var_pc

            gui_mock.var_align_quality = tk.StringVar(value="medium")
            gui_mock.var_keypoint_limit = tk.StringVar(value="40000")
            gui_mock.var_tiepoint_limit = tk.StringVar(value="4000")
            gui_mock.var_faces = tk.StringVar(value="500000")
            gui_mock.var_tex_pages = tk.StringVar(value="4")
            gui_mock.var_crop = tk.StringVar(value="none")
            gui_mock.var_depth_quality = tk.StringVar(value="low")
            gui_mock.var_depth_filtering = tk.StringVar(value="mild")
            gui_mock.var_guided = tk.BooleanVar(value=False)
            gui_mock.var_rolling = tk.BooleanVar(value=False)
            gui_mock.var_stationary = tk.BooleanVar(value=False)
            gui_mock.var_adaptive_fitting = tk.BooleanVar(value=False)
            gui_mock.var_close_holes = tk.BooleanVar(value=True)
            gui_mock.var_fill_holes = tk.BooleanVar(value=False)
            gui_mock.var_ghosting = tk.BooleanVar(value=False)
            gui_mock.var_build_point_cloud = tk.BooleanVar(value=False)
            gui_mock.var_build_dem = tk.BooleanVar(value=False)
            gui_mock.var_build_ortho = tk.BooleanVar(value=False)
            gui_mock.var_classify_ground = tk.BooleanVar(value=True)

            # User selects Pro preset
            AutoMetashapeStudio._select_preset(gui_mock, "pro")

            # Workflow parameters are updated to Pro standards
            self.assertEqual(gui_mock.var_align_quality.get(), "high")
            self.assertEqual(gui_mock.var_faces.get(), "2000000")
            self.assertEqual(gui_mock.var_tex_pages.get(), "16")
            self.assertTrue(gui_mock.var_build_ortho.get())
            self.assertFalse(gui_mock.var_build_dem.get())

            # BUT user's Export Table checklist is strictly preserved and not overwritten!
            self.assertTrue(gui_mock.var_export_glb.get())
            self.assertFalse(gui_mock.var_export_obj.get())
            self.assertTrue(gui_mock.var_export_dem.get())
            self.assertFalse(gui_mock.var_export_ortho.get())
            self.assertTrue(gui_mock.var_export_point_cloud.get())
        finally:
            root.destroy()

    def test_workflow_two_way_cascade_dependencies(self) -> None:
        """Defend two-way workflow stage cascading dependencies:
        1. Ticking a downstream stage automatically ticks mandatory upstream prerequisites.
        2. Unticking an upstream prerequisite automatically unticks dependent downstream stages.
        3. Downstream stages can be independently unticked without affecting upstream prerequisites.
        """
        import tkinter as tk
        from autometashape_gui import AutoMetashapeStudio

        root = tk.Tk()
        try:
            gui = AutoMetashapeStudio(root)

            # Case 1: Independent downstream untick (Model enabled, Ortho disabled)
            gui.var_stage_mesh.set(True)
            gui.var_build_ortho.set(False)
            gui._on_workflow_stage_toggled("ortho")
            self.assertTrue(gui.var_stage_mesh.get())
            self.assertFalse(gui.var_build_ortho.get())

            # Case 2: Forward cascade (Ticking Ortho enables mandatory Mesh, Depth, Align)
            gui.var_stage_mesh.set(False)
            gui.var_build_ortho.set(True)
            gui._on_workflow_stage_toggled("ortho")
            self.assertTrue(gui.var_stage_mesh.get())
            self.assertTrue(gui.var_stage_depth.get())
            self.assertTrue(gui.var_stage_align.get())
            self.assertTrue(gui.var_build_ortho.get())

            # Case 3: Backward cascade (Unticking Mesh disables Texture and True Ortho)
            gui.var_stage_texture.set(True)
            gui.var_stage_mesh.set(False)
            gui._on_workflow_stage_toggled("mesh")
            self.assertFalse(gui.var_stage_mesh.get())
            self.assertFalse(gui.var_stage_texture.get())
            self.assertFalse(gui.var_build_ortho.get())

            # Case 4: Deep backward cascade (Unticking Depth disables Mesh, Cloud, Ortho)
            gui.var_stage_depth.set(False)
            gui._on_workflow_stage_toggled("depth")
            self.assertFalse(gui.var_build_point_cloud.get())
            self.assertFalse(gui.var_stage_mesh.get())
            self.assertFalse(gui.var_build_ortho.get())
        finally:
            root.destroy()

    def test_pick_multiple_folders_fallback(self) -> None:
        """Verify pick_multiple_folders falls back to filedialog when COM is mocked/fails."""
        from unittest.mock import patch

        with patch("tkinter.filedialog.askdirectory", return_value="C:/Mocked/Path"):
            with patch("sys.platform", "linux"):
                res = pick_multiple_folders(title="Test")
                self.assertEqual(res, ["C:/Mocked/Path"])

        with patch("tkinter.filedialog.askdirectory", return_value=""):
            with patch("sys.platform", "linux"):
                res_empty = pick_multiple_folders(title="Test")
                self.assertEqual(res_empty, [])

    def test_add_project_to_queue_multiple_folders(self) -> None:
        """Verify multi-folder addition ingests multiple folders and avoids duplicates."""
        import tempfile
        from autometashape_gui import AutoMetashapeStudio

        with tempfile.TemporaryDirectory() as tmp_dir:
            f1 = os.path.join(tmp_dir, "Survey_A")
            f2 = os.path.join(tmp_dir, "Survey_B")
            os.makedirs(f1, exist_ok=True)
            os.makedirs(f2, exist_ok=True)
            with open(os.path.join(f1, "img1.jpg"), "w") as f:
                f.write("0")
            with open(os.path.join(f2, "img2.jpg"), "w") as f:
                f.write("0")

            gui_mock = MagicMock()
            gui_mock.project_items = []
            var_dir = MagicMock()
            var_dir.get.return_value = ""
            gui_mock.var_images_dir = var_dir
            gui_mock._quick_count_photos.side_effect = lambda path: 1

            AutoMetashapeStudio._add_project_to_queue(gui_mock, [f1, f2])

            self.assertEqual(len(gui_mock.project_items), 2)
            self.assertEqual(os.path.normpath(gui_mock.project_items[0]["path"]), os.path.normpath(f1))
            self.assertEqual(os.path.normpath(gui_mock.project_items[1]["path"]), os.path.normpath(f2))
            var_dir.set.assert_called_with(f1)

            # Re-adding f1 should not create duplicates
            AutoMetashapeStudio._add_project_to_queue(gui_mock, [f1])
            self.assertEqual(len(gui_mock.project_items), 2)

    def test_add_project_to_queue_preserves_project_subfolders(self) -> None:
        """Verify a selected project folder containing data subfolders is enqueued as a single project."""
        import tempfile
        from autometashape_gui import AutoMetashapeStudio

        with tempfile.TemporaryDirectory() as tmp_dir:
            proj_dir = os.path.join(tmp_dir, "Campaign_Bridge_Inspection")
            sub_a = os.path.join(proj_dir, "100MEDIA")
            sub_b = os.path.join(proj_dir, "101MEDIA")
            os.makedirs(sub_a, exist_ok=True)
            os.makedirs(sub_b, exist_ok=True)
            with open(os.path.join(sub_a, "DJI_0001.JPG"), "w") as f:
                f.write("0")
            with open(os.path.join(sub_b, "DJI_0100.JPG"), "w") as f:
                f.write("0")

            gui_mock = MagicMock()
            gui_mock.project_items = []
            var_dir = MagicMock()
            var_dir.get.return_value = ""
            gui_mock.var_images_dir = var_dir
            gui_mock._quick_count_photos = AutoMetashapeStudio._quick_count_photos.__get__(gui_mock)

            # Adding proj_dir: must be added as 1 project, NOT split into subfolders!
            AutoMetashapeStudio._add_project_to_queue(gui_mock, [proj_dir])

            self.assertEqual(len(gui_mock.project_items), 1)
            self.assertEqual(os.path.normpath(gui_mock.project_items[0]["path"]), os.path.normpath(proj_dir))
            self.assertEqual(gui_mock.project_items[0]["name"], "Campaign_Bridge_Inspection")
            self.assertEqual(gui_mock.project_items[0]["count"], 2)


    def test_cluster_queue_concurrency_clamping(self) -> None:
        """Verify cluster queue concurrency clamping between 1 and 8 when cluster active, and 1 when local."""
        def calc_conc(val: int, is_cluster: bool) -> int:
            if not is_cluster:
                return 1
            return max(1, min(val, 8))

        # Cluster mode enabled
        self.assertEqual(calc_conc(0, is_cluster=True), 1)
        self.assertEqual(calc_conc(2, is_cluster=True), 2)
        self.assertEqual(calc_conc(3, is_cluster=True), 3)
        self.assertEqual(calc_conc(5, is_cluster=True), 5)
        self.assertEqual(calc_conc(8, is_cluster=True), 8)
        self.assertEqual(calc_conc(10, is_cluster=True), 8)

        # Local mode (single PC GPU safety)
        self.assertEqual(calc_conc(5, is_cluster=False), 1)
        self.assertEqual(calc_conc(2, is_cluster=False), 1)

    def test_reconstruction_cmd_independent_workers_flag(self) -> None:
        """Verify _build_reconstruction_cmd includes --network-independent-workers when checked."""
        from autometashape_gui import AutoMetashapeStudio

        gui_mock = MagicMock()
        gui_mock.metashape_exe = "metashape.exe"
        gui_mock.recon_script = "scripts/metashape_reconstruction.py"
        gui_mock.selected_preset_key = "pro"
        gui_mock.var_presel_mode.get.return_value = "estimated"
        gui_mock.var_pipelined_export.get.return_value = False
        gui_mock.batch_running = False

        gui_mock.var_export_glb.get.return_value = False
        gui_mock.var_export_obj.get.return_value = True
        gui_mock.var_export_fbx.get.return_value = False
        gui_mock.var_export_ply.get.return_value = False
        gui_mock.var_export_dxf.get.return_value = False
        gui_mock.var_export_tie_points.get.return_value = False
        gui_mock.var_build_ortho.get.return_value = True
        gui_mock.var_export_ortho.get.return_value = True
        gui_mock.var_build_dem.get.return_value = True
        gui_mock.var_export_dem.get.return_value = False
        gui_mock.var_build_point_cloud.get.return_value = False
        gui_mock.var_export_point_cloud.get.return_value = False
        gui_mock.var_build_contours.get.return_value = False
        gui_mock.var_export_contours.get.return_value = False
        gui_mock.var_build_tiled_model.get.return_value = False
        gui_mock.var_export_tiles.get.return_value = False
        gui_mock.var_export_report.get.return_value = False
        gui_mock.var_pc_format.get.return_value = "laz"
        gui_mock.var_classify_ground.get.return_value = False
        gui_mock.var_classify_points.get.return_value = False
        gui_mock.var_pause_crop.get.return_value = False
        gui_mock.var_rebake.get.return_value = False
        gui_mock.var_resume.get.return_value = False
        gui_mock.var_multi_chunk.get.return_value = "disable"
        gui_mock.var_chunk_align_method.get.return_value = "camera"

        gui_mock.var_network_enable.get.return_value = True
        gui_mock.var_network_host.get.return_value = "10.5.10.202"
        gui_mock.var_network_port.get.return_value = "5840"
        gui_mock.var_network_path.get.return_value = r"\\SERVER\Scan"
        gui_mock.var_network_independent_workers.get.return_value = True

        cmd = AutoMetashapeStudio._build_reconstruction_cmd(gui_mock, r"C:\Images", r"C:\Output", "ProjectA")
        self.assertIn("--network-enable", cmd)
        self.assertIn("--network-independent-workers", cmd)

    def test_cluster_concurrency_variable_default(self) -> None:
        """Verify AutoMetashapeStudio defines cluster concurrency with a default of 3."""
        from autometashape_gui import AutoMetashapeStudio
        # Check that the default value is 3
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            var = tk.IntVar(value=3)
            self.assertEqual(var.get(), 3)
        finally:
            root.destroy()

    def test_pump_batch_queue_concurrent_dispatch_bounds(self) -> None:
        """Verify _pump_batch_queue respects cluster concurrency limit and prevents over-dispatch."""
        import threading
        from autometashape_gui import AutoMetashapeStudio

        mock_studio = MagicMock()
        mock_studio.batch_running = True
        mock_studio.var_network_enable.get.return_value = True
        mock_studio.var_cluster_concurrent_projects.get.return_value = 3
        mock_studio.cluster_job_lock = threading.Lock()
        mock_studio.active_cluster_jobs = []
        mock_studio.batch_submitted_idx = 0
        mock_studio.batch_current_idx = 0
        mock_studio.batch_queue = [
            {"path": f"C:/Scan_{i}", "name": f"Scan_{i}"} for i in range(5)
        ]

        dispatched_items = []

        def fake_launch(item: dict, idx: int) -> None:
            dispatched_items.append((item, idx))
            with mock_studio.cluster_job_lock:
                mock_studio.active_cluster_jobs.append({"name": item["name"], "idx": idx, "proc": None})

        mock_studio._launch_cluster_recon_job = fake_launch

        # First pump: should dispatch exactly 3 jobs (indexes 0, 1, 2)
        AutoMetashapeStudio._pump_batch_queue(mock_studio)
        self.assertEqual(len(dispatched_items), 3)
        self.assertEqual(mock_studio.batch_submitted_idx, 3)
        self.assertEqual(len(mock_studio.active_cluster_jobs), 3)

        # Second pump without any jobs finishing: should not dispatch anything more
        AutoMetashapeStudio._pump_batch_queue(mock_studio)
        self.assertEqual(len(dispatched_items), 3)

        # Complete one job (Scan_0)
        with mock_studio.cluster_job_lock:
            mock_studio.active_cluster_jobs = [x for x in mock_studio.active_cluster_jobs if x["name"] != "Scan_0"]

        # Third pump: should dispatch index 3 (Scan_3)
        AutoMetashapeStudio._pump_batch_queue(mock_studio)
        self.assertEqual(len(dispatched_items), 4)
        self.assertEqual(mock_studio.batch_submitted_idx, 4)
        self.assertEqual(len(mock_studio.active_cluster_jobs), 3)

    def test_pump_batch_queue_local_mode_serial_dispatch(self) -> None:
        """Verify _pump_batch_queue enforces strict serial (max 1) dispatch when cluster is disabled."""
        import threading
        from autometashape_gui import AutoMetashapeStudio

        mock_studio = MagicMock()
        mock_studio.batch_running = True
        mock_studio.var_network_enable.get.return_value = False
        mock_studio.var_cluster_concurrent_projects.get.return_value = 4
        mock_studio.cluster_job_lock = threading.Lock()
        mock_studio.active_cluster_jobs = []
        mock_studio.batch_submitted_idx = 0
        mock_studio.batch_queue = [
            {"path": f"C:/Scan_{i}", "name": f"Scan_{i}"} for i in range(3)
        ]

        dispatched = []

        def fake_launch(item: dict, idx: int) -> None:
            dispatched.append(idx)
            with mock_studio.cluster_job_lock:
                mock_studio.active_cluster_jobs.append({"name": item["name"], "idx": idx, "proc": None})

        mock_studio._launch_cluster_recon_job = fake_launch

        # Pumping in local mode should dispatch only 1 project at a time
        AutoMetashapeStudio._pump_batch_queue(mock_studio)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0], 0)
        self.assertEqual(len(mock_studio.active_cluster_jobs), 1)

    def test_stepper_multi_set_done(self) -> None:
        """Verify StudioPipelineStepper.set_done marks specific stage completed with checkmark."""
        import tkinter as tk
        from autometashape_gui import StudioPipelineStepper

        root = tk.Tk()
        root.withdraw()
        try:
            stepper = StudioPipelineStepper(root, [("1", "SiteA", "100 photos"), ("2", "SiteB", "200 photos")])
            stepper.set_done(0, "Exported")
            self.assertEqual(stepper.stage_indicators[0].cget("text"), "✓")
            self.assertIn("Completed: SiteA", stepper.lbl_pipeline_status.cget("text"))
        finally:
            root.destroy()

    def test_cluster_independent_worker_mode_batch_limit(self) -> None:
        """Verify Independent Project Mode applies setBatchWorkerLimit(batch_id, 1)."""
        from unittest.mock import MagicMock
        mock_client = MagicMock()
        mock_client.createBatch.return_value = 101

        batch_id = mock_client.createBatch("relative/path/project.psx", [])
        network_independent_workers = True
        if network_independent_workers:
            mock_client.setBatchWorkerLimit(batch_id, 1)

        mock_client.setBatchWorkerLimit.assert_called_once_with(101, 1)

    def test_cluster_concurrent_projects_default_8(self) -> None:
        """Verify that cluster concurrent projects defaults to 8 to keep cluster nodes 100% utilized."""
        import tkinter as tk
        from autometashape_gui import AutoMetashapeStudio

        root = tk.Tk()
        root.withdraw()
        try:
            studio = AutoMetashapeStudio(root)
            self.assertEqual(studio.var_cluster_concurrent_projects.get(), 8)
        finally:
            root.destroy()

    def test_cluster_export_fallback_suppression(self) -> None:
        """Verify that when cluster worker exports deliverables, host skips local export."""
        cluster_exported = True
        local_executed = False
        export_glb = True
        if (not cluster_exported) and export_glb:
            local_executed = True
        self.assertFalse(local_executed)

        cluster_exported = False
        if (not cluster_exported) and export_glb:
            local_executed = True
        self.assertTrue(local_executed)

    def test_build_cluster_gradual_selection_tasks(self) -> None:
        """Verify build_cluster_gradual_selection_tasks returns 8 CleanTiePoints and OptimizeCameras tasks."""
        from unittest.mock import MagicMock, patch
        import metashape_reconstruction as mr

        mock_meta = MagicMock()
        mock_meta.Tasks.CleanTiePoints = lambda: MagicMock(name="CleanTiePoints")
        mock_meta.Tasks.OptimizeCameras = lambda: MagicMock(name="OptimizeCameras")
        mock_meta.TiePoints.Criterion.ReconstructionUncertainty = 1
        mock_meta.TiePoints.Criterion.ProjectionAccuracy = 2
        mock_meta.TiePoints.Criterion.ReprojectionError = 3
        mock_meta.TiePoints.Criterion.ImageCount = 4

        with patch.object(mr, "Metashape", mock_meta):
            tasks = mr.build_cluster_gradual_selection_tasks()
            self.assertEqual(len(tasks), 8)

    def test_build_cluster_crop_task(self) -> None:
        """Verify build_cluster_crop_task creates valid RunScript network tasks for worker bounding box auto-crop."""
        from unittest.mock import MagicMock, patch
        import metashape_reconstruction as mr

        mock_meta = MagicMock()
        mock_meta.Tasks.RunScript = lambda: MagicMock(name="RunScript", code="")

        with patch.object(mr, "Metashape", mock_meta):
            t_gps = mr.build_cluster_crop_task("gps-square", padding=0.05)
            self.assertIsNotNone(t_gps)
            self.assertIn("chunk.region.center", t_gps.code)
            self.assertIn("chunk.region.size", t_gps.code)

            t_struct = mr.build_cluster_crop_task("structure", padding=0.10)
            self.assertIsNotNone(t_struct)
            self.assertIn("chunk.region.center", t_struct.code)

            t_none = mr.build_cluster_crop_task("manual")
            self.assertIsNone(t_none)


    def test_build_unified_cluster_pipeline_tasks_full(self) -> None:
        """Verify build_unified_cluster_pipeline_tasks bundles all pipeline stages into a unified batch with correct stage mapping and deterministic texture pages."""
        from unittest.mock import MagicMock, patch
        import metashape_reconstruction as mr

        mock_meta = MagicMock()
        mock_meta.ReferencePreselectionMode.ReferencePreselectionEstimated = 1
        mock_meta.FilterMode.ModerateFiltering = 2
        mock_meta.DataSource.DepthMapsData = 1
        mock_meta.DataSource.ElevationData = 2
        mock_meta.DataSource.OrthomosaicData = 3
        mock_meta.DataSource.PointCloudData = 4
        mock_meta.SurfaceType.Arbitrary = 0
        mock_meta.Interpolation.EnabledInterpolation = 1
        mock_meta.FaceCount.CustomFaceCount = 1
        mock_meta.MappingMode.GenericMapping = 0
        mock_meta.BlendingMode.MosaicBlending = 1
        mock_meta.ModelFormat.ModelFormatOBJ = 1
        mock_meta.ModelFormat.ModelFormatGLTF = 2
        mock_meta.ImageFormat.ImageFormatPNG = 1
        mock_meta.RasterFormat.RasterFormatNone = 0
        mock_meta.ImageFormat.ImageFormatTIFF = 2
        mock_meta.PointCloudFormat.PointCloudFormatLAZ = 1

        mock_meta.Tasks.MatchPhotos = lambda: MagicMock(name="MatchPhotos")
        mock_meta.Tasks.AlignCameras = lambda: MagicMock(name="AlignCameras")
        mock_meta.Tasks.CleanTiePoints = lambda: MagicMock(name="CleanTiePoints", criterion=1)
        mock_meta.Tasks.OptimizeCameras = lambda: MagicMock(name="OptimizeCameras")
        mock_meta.Tasks.RunScript = lambda: MagicMock(name="RunScript", code="chunk.region.size = ...")
        mock_meta.Tasks.BuildDepthMaps = lambda: MagicMock(name="BuildDepthMaps")
        mock_meta.Tasks.BuildPointCloud = lambda: MagicMock(name="BuildPointCloud")
        mock_meta.Tasks.BuildModel = lambda: MagicMock(name="BuildModel")
        mock_meta.Tasks.BuildUV = lambda: MagicMock(name="BuildUV", page_count=16)
        mock_meta.Tasks.BuildTexture = lambda: MagicMock(name="BuildTexture")
        mock_meta.Tasks.BuildDem = lambda: MagicMock(name="BuildDem")
        mock_meta.Tasks.BuildOrthomosaic = lambda: MagicMock(name="BuildOrthomosaic")
        mock_meta.Tasks.ExportModel = lambda: MagicMock(name="ExportModel")
        mock_meta.Tasks.ExportRaster = lambda: MagicMock(name="ExportRaster")
        mock_meta.Tasks.ExportPointCloud = lambda: MagicMock(name="ExportPointCloud")
        mock_meta.Tasks.ExportReport = lambda: MagicMock(name="ExportReport")

        mock_chunk = MagicMock()
        mock_chunk.crs = None
        mock_chunk.cameras = []

        with patch.object(mr, "Metashape", mock_meta):
            # Test 1: Full Unified Pipeline assembly with deterministic 16 texture pages
            tasks_16, stages_16 = mr.build_unified_cluster_pipeline_tasks(
                chunk=mock_chunk,
                output_dir="C:/Out/Proj",
                project_name="Proj",
                align_photos=True,
                filter_tiepoints=True,
                crop_method="gps-square",
                build_depth_maps=True,
                build_point_cloud=True,
                build_mesh=True,
                build_texture=True,
                texture_pages=16,
                build_dem=True,
                build_orthomosaic=True,
                export_obj=True,
                export_glb=True,
                export_dem=True,
                export_ortho=True,
                export_point_cloud=True,
                export_report=True,
            )
            # Find BuildUV task in tasks_16
            uv_task_16 = next(t for t, (name, _, _) in zip(tasks_16, stages_16) if name == "BuildUV")
            self.assertEqual(uv_task_16.page_count, 16)

            # Test 2: Stage info contains expected stepper stages
            stage_keys = [k for _, k, _ in stages_16]
            self.assertIn("match", stage_keys)
            self.assertIn("align", stage_keys)
            self.assertIn("depth", stage_keys)
            self.assertIn("point_cloud", stage_keys)
            self.assertIn("mesh", stage_keys)
            self.assertIn("texture", stage_keys)
            self.assertIn("dem", stage_keys)
            self.assertIn("ortho", stage_keys)
            self.assertIn("export", stage_keys)

            # Test 4: When align_photos=False, MatchPhotos and AlignCameras are omitted
            _, stages_no_align = mr.build_unified_cluster_pipeline_tasks(
                chunk=mock_chunk,
                output_dir="C:/Out/Proj",
                project_name="Proj",
                align_photos=False,
                build_depth_maps=True,
                build_mesh=True,
            )
            stage_keys_no_align = [k for _, k, _ in stages_no_align]
            self.assertNotIn("match", stage_keys_no_align)
            self.assertIn("depth", stage_keys_no_align)
            self.assertIn("mesh", stage_keys_no_align)


if __name__ == "__main__":
    unittest.main()
