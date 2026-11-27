# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Hermetic regression tests defending Wave 1-3 deep audit fixes across core, cluster, and CLI."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
for _p in (_SOURCE_DIR, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from autometashape.cluster.dual_share import is_subpath, normalize_share_path
from autometashape.cluster.hardware import compute_discrete_gpu_mask, extract_unc_root, is_integrated_gpu
from autometashape.cluster.orchestrator import (
    clear_stale_project_lock,
    create_hidden_dispatch_ticket,
    safe_create_cluster_batch,
)
from autometashape.core import export as export_mod
from autometashape.core import telemetry as telemetry_mod

from scripts import cluster_monitor as monitor_mod
from scripts import manage_queue as mq_mod


class TestWaveHardeningRegressions(unittest.TestCase):
    """Mutation-resistant tests defending Wave 1-3 core, cluster, and CLI hardening fixes."""

    def test_telemetry_compd_cs_and_fc3411_vs_m3e_shutter(self) -> None:
        """Verify COMPD_CS containing PROJCS is detected as projected and FC3411 (Air 2S) stays rolling shutter."""
        compd_utm = SimpleNamespace(
            wkt='COMPD_CS["WGS 84 / UTM zone 48N + EGM96 height",PROJCS["WGS 84 / UTM zone 48N",GEOGCS["WGS 84"]],VERT_CS["EGM96"]]'
        )
        compd_geog = SimpleNamespace(wkt='COMPD_CS["WGS 84 + EGM96",GEOGCS["WGS 84"],VERT_CS["EGM96"]]')
        self.assertTrue(telemetry_mod.is_projected_crs(compd_utm))
        self.assertFalse(telemetry_mod.is_projected_crs(compd_geog))

        # FC3411 is DJI Air 2S (electronic rolling shutter); M3E / M4E are mechanical leaf shutters
        self.assertEqual(telemetry_mod.CAMERA_HARDWARE_DB["fc3411"]["shutter"], "rolling")
        self.assertEqual(telemetry_mod.CAMERA_HARDWARE_DB["m3e"]["shutter"], "mechanical")
        self.assertEqual(telemetry_mod.CAMERA_HARDWARE_DB["m4e"]["shutter"], "mechanical")

    def test_hardware_unicode_trademark_gpu_filtering_and_unc_root(self) -> None:
        """Verify Unicode ®/™ GPU names filter iGPUs accurately and extract_unc_root handles extended UNC."""
        self.assertTrue(is_integrated_gpu("Intel® Iris® Xe Graphics"))
        self.assertTrue(is_integrated_gpu("AMD Radeon™ 780M Graphics"))
        self.assertFalse(is_integrated_gpu("Intel® Arc™ A770 Graphics"))
        self.assertFalse(is_integrated_gpu("NVIDIA GeForce RTX™ 5080"))

        mask, selected = compute_discrete_gpu_mask(
            [
                {"name": "Intel® UHD Graphics 770", "mem_size": 0},
                {"name": "NVIDIA GeForce RTX™ 5080", "mem_size": 16 * (1024**3)},
            ]
        )
        self.assertEqual(mask, 2)
        self.assertEqual([d["index"] for d in selected], [1])

        self.assertEqual(extract_unc_root(r"\\?\UNC\srv\share\sub\file.psx"), r"\\srv\share")
        self.assertEqual(extract_unc_root(r"\\?\C:\local\file.psx"), "")

    def test_dual_share_rejects_unc_dotdot_escape(self) -> None:
        """Verify UNC paths with '..' escaping share root are rejected by normalize_share_path and is_subpath."""
        self.assertEqual(normalize_share_path(r"\\SRV\Data\A\..\B"), r"\\SRV\Data\B")
        self.assertFalse(is_subpath(r"\\SRV\Data\..\Other\Site", r"\\SRV\Data"))
        self.assertFalse(is_subpath(r"\\SRV\Data\..\..\evil", r"\\SRV\Data"))

    def test_export_clears_doc_on_error_and_validates_import_model(self) -> None:
        """Verify export_project_assets always calls doc.clear() and raises FileNotFoundError on missing import."""
        fake_doc = MagicMock()
        fake_chunk = MagicMock()
        fake_chunk.model = MagicMock(vertices=[1, 2, 3], faces=[1], textures=[MagicMock()])
        fake_chunk.exportModel.side_effect = RuntimeError("Disk full")
        fake_doc.chunk = fake_chunk
        fake_doc.chunks = [fake_chunk]

        fake_ms = SimpleNamespace(
            Document=lambda: fake_doc,
            app=SimpleNamespace(enumGPUDevices=lambda: []),
            ModelFormat=SimpleNamespace(ModelFormatGLTF=1, ModelFormatOBJ=2),
            ImageFormat=SimpleNamespace(ImageFormatPNG=1),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            psx = os.path.join(tmpdir, "Site.psx")
            with open(psx, "w", encoding="utf-8") as fh:
                fh.write("<document/>")

            with patch.object(export_mod, "Metashape", fake_ms):
                with self.assertRaises(FileNotFoundError):
                    export_mod.export_project_assets(
                        project_path=psx,
                        output_dir=tmpdir,
                        import_model_path=os.path.join(tmpdir, "missing.obj"),
                    )
                fake_doc.open.assert_not_called()
                fake_doc.clear.assert_not_called()

                fake_doc.clear.reset_mock()
                with self.assertRaises(RuntimeError):
                    export_mod.export_project_assets(
                        project_path=psx,
                        output_dir=tmpdir,
                        export_glb=True,
                        export_obj=False,
                    )
                fake_doc.clear.assert_called_once()
                # GLB export explicitly sets save_texture=True
                self.assertTrue(fake_chunk.exportModel.call_args.kwargs.get("save_texture"))

    def test_canonical_batch_preserves_paused_and_clears_stale_lock_post_release(self) -> None:
        """Verify safe_create_cluster_batch preserves operator-paused batches and clears stale lock after release."""
        # 1. force_fresh=False preserves operator-paused healthy batch
        mock_paused = MagicMock()
        mock_paused.findBatch.return_value = 77
        mock_paused.batchInfo.return_value = {"batch_id": 77, "state": "paused", "paused": True, "error_count": 0}
        bid, attached = safe_create_cluster_batch(
            mock_paused, "Result/SiteP/SiteP.psx", [MagicMock()], force_fresh=False
        )
        self.assertEqual(bid, 77)
        self.assertTrue(attached)
        mock_paused.abortBatch.assert_not_called()

        # 2. clear_stale_project_lock removes leftover .files/lock and raises cleanly on OSError
        with tempfile.TemporaryDirectory() as shared_root:
            lock_dir = os.path.join(shared_root, "Result", "SiteL", "SiteL.files")
            os.makedirs(lock_dir, exist_ok=True)
            lock_file = os.path.join(lock_dir, "lock")
            with open(lock_file, "w", encoding="utf-8") as fh:
                fh.write("stale")

            cleared, removed_path = clear_stale_project_lock(shared_root, "Result/SiteL/SiteL.psx")
            self.assertTrue(cleared)
            self.assertEqual(os.path.normpath(removed_path), os.path.normpath(lock_file))
            self.assertFalse(os.path.exists(lock_file))

            # Active OS handle (PermissionError / WinError 32) raises RuntimeError instead of ignoring
            with open(lock_file, "w", encoding="utf-8") as fh:
                fh.write("active")
            with patch("os.remove", side_effect=PermissionError("WinError 32: file in use")):
                with self.assertRaises(RuntimeError):
                    clear_stale_project_lock(shared_root, "Result/SiteL/SiteL.psx")

    def test_manage_queue_and_filter_clear_worker_rc_and_ticket_display(self) -> None:
        """Verify manage_queue uses AND filtering when both ID+host given, returns 1 on RPC failure, unwraps tickets."""
        client = MagicMock()
        client.workerList.return_value = {
            "workers": [
                {"worker_id": 1, "hostname": "NodeA", "state": "ready"},
                {"worker_id": 2, "hostname": "NodeB", "state": "ready"},
            ]
        }
        # Mismatched ID + hostname must NOT match either worker
        self.assertEqual(mq_mod.cmd_set_worker_paused(client, True, worker_id=1, hostname="NodeB"), 1)
        client.setWorkerPaused.assert_not_called()
        # Exact match succeeds
        self.assertEqual(mq_mod.cmd_set_worker_paused(client, True, worker_id=1, hostname="NodeA"), 0)
        client.setWorkerPaused.assert_called_once_with(1, True)

        # clear_worker returns 1 when all RPC calls fail
        failing_client = MagicMock()
        failing_client.workerList.return_value = {"workers": [{"worker_id": 9, "hostname": "Ghost", "aborted": True}]}
        failing_client.abortWorker.side_effect = RuntimeError("RPC failed")
        failing_client.quitWorker.side_effect = RuntimeError("RPC failed")
        self.assertEqual(mq_mod.cmd_clear_worker(failing_client, 9, only_aborted=True), 1)

        # cmd_list unwraps .cluster/tickets/ to canonical project path
        with tempfile.TemporaryDirectory() as shared_root:
            ticket_rel = create_hidden_dispatch_ticket(
                shared_root,
                "CATP_0031",
                rel_output_dir="CAN_CHAY_LAI/Result/CATP_0031",
                canonical_rel_psx="CAN_CHAY_LAI/Result/CATP_0031/CATP_0031.psx",
            )
            list_client = MagicMock()
            list_client.batchList.return_value = {
                "batches": [{"batch_id": 42, "priority": 50, "state": "pending", "path": ticket_rel}]
            }
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(mq_mod.cmd_list(list_client, "all", shared_root=shared_root), 0)
            self.assertIn("CAN_CHAY_LAI/Result/CATP_0031/CATP_0031.psx", buf.getvalue())

    def test_cluster_monitor_exit_code_and_missing_deliverables_dir(self) -> None:
        """Verify cluster_monitor exits 1 when unhealthy (0 with --advisory) and flags missing result dirs."""
        fake_client = MagicMock()
        fake_client.workerList.return_value = {"workers": [{"worker_id": 1, "hostname": "NodeA", "state": "ready"}]}
        fake_client.batchList.return_value = {
            "batches": [{"batch_id": 10, "path": "Result/MissingSite/MissingSite.psx", "state": "finished"}]
        }
        fake_ms = SimpleNamespace(NetworkClient=lambda: fake_client)

        with tempfile.TemporaryDirectory() as shared_root:
            with (
                patch.object(monitor_mod, "Metashape", fake_ms),
                redirect_stdout(io.StringIO()),
            ):
                audit = monitor_mod.execute_audit(
                    server_host="127.0.0.1:5840", shared_root=shared_root, min_headroom=0.01
                )
            self.assertFalse(audit["healthy"])
            self.assertIn(10, audit["deliverables"])
            self.assertFalse(audit["deliverables"][10]["intact"])

            with (
                patch.object(monitor_mod, "Metashape", fake_ms),
                patch.object(
                    sys,
                    "argv",
                    ["cluster_monitor.py", "--server", "127.0.0.1:5840", "--root", shared_root, "--no-reexec"],
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(monitor_mod.main(), 1)

            with (
                patch.object(monitor_mod, "Metashape", fake_ms),
                patch.object(
                    sys,
                    "argv",
                    [
                        "cluster_monitor.py",
                        "--server",
                        "127.0.0.1:5840",
                        "--root",
                        shared_root,
                        "--advisory",
                        "--no-reexec",
                    ],
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(monitor_mod.main(), 0)


if __name__ == "__main__":
    unittest.main()
