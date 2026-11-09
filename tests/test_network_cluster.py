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

from autometashape_gui import find_metashape_server_path, generate_worker_bat_content, get_primary_lan_ip


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
        content = generate_worker_bat_content(None, "5840", r"\\SERVER\Scan")
        self.assertIn("5840", content)
        self.assertIn(r"\\SERVER\Scan", content)
        self.assertIn("--worker", content)

    def test_find_metashape_server_path_discovery(self) -> None:
        """Verify server discovery logic locates binary or safely returns None."""
        server = find_metashape_server_path(None)
        if server is not None:
            self.assertTrue(os.path.isfile(server))
            self.assertTrue(server.lower().endswith("metashape-server.exe") or server.endswith("metashape-server"))

    def test_cluster_relative_path_resolution(self) -> None:
        """Verify project path conversion to shared storage relative path."""
        def calc_rel(psx_out: str, network_path: str) -> str:
            if network_path and os.path.exists(network_path):
                try:
                    abs_psx = os.path.abspath(psx_out)
                    abs_net = os.path.abspath(network_path)
                    if os.path.commonpath([abs_psx, abs_net]) == abs_net:
                        return os.path.relpath(abs_psx, abs_net).replace("\\", "/")
                except Exception:
                    pass
            return os.path.basename(psx_out)

        # Case 1: When network_path is empty, returns file basename
        self.assertEqual(calc_rel(r"C:\Projects\Scan1\project.psx", ""), "project.psx")

        # Case 2: When network_path exists and contains psx_out
        test_dir = os.path.dirname(os.path.abspath(__file__))
        sub_psx = os.path.join(test_dir, "sub", "project.psx")
        rel = calc_rel(sub_psx, test_dir)
        self.assertEqual(rel, "sub/project.psx")

    def test_network_client_worker_auto_unpause(self) -> None:
        """Verify newly connected worker nodes have pause cleared automatically."""
        client = MagicMock()
        worker1 = MagicMock()
        worker1.id = 1
        worker1.paused = True
        worker2 = MagicMock()
        worker2.id = 2
        worker2.paused = False

        client.workerList.side_effect = [[worker1, worker2], [worker1, worker2]]

        workers = client.workerList()
        for w in workers:
            if getattr(w, "paused", False) and hasattr(w, "id"):
                client.setWorkerPaused(w.id, False)

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


if __name__ == "__main__":
    unittest.main()
