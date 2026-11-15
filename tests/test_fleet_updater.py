# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for host-initiated autonomous worker fleet remote updater."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from autometashape.cluster.updater import (
    CLUSTER_MAINTENANCE_REL_PATH,
    DEFAULT_REPO_ARCHIVE_URL,
    ensure_cluster_maintenance_project,
    generate_worker_updater_code,
    list_online_workers,
    update_remote_worker,
)

from scripts.update_fleet import build_arg_parser


class TestFleetUpdater(unittest.TestCase):
    """Test suite for cluster worker remote updating and lifecycle management."""

    def test_generate_worker_updater_code(self) -> None:
        """Verify generated remote worker updater script content."""
        code = generate_worker_updater_code("https://example.com/repo.zip")
        self.assertIn("https://example.com/repo.zip", code)
        self.assertIn("urllib.request", code)
        self.assertIn("zipfile", code)
        self.assertIn("worker_config.json", code)
        self.assertIn("server_config.json", code)
        self.assertIn("update_result.txt", code)

    def test_ensure_cluster_maintenance_project(self) -> None:
        """Verify maintenance project creation on shared storage."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            rel = ensure_cluster_maintenance_project(tmp_dir)
            self.assertEqual(rel, CLUSTER_MAINTENANCE_REL_PATH)
            full_path = os.path.join(tmp_dir, ".cluster", "maintenance.psx")
            self.assertTrue(os.path.isfile(full_path))
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("<document", content)
            self.assertIn("maintenance.files", content)

    def test_list_online_workers(self) -> None:
        """Verify online worker filtering logic."""
        mock_client = MagicMock()
        mock_client.workerList.return_value = {
            "workers": [
                {"worker_id": 1, "hostname": "Node-1", "state": "ready"},
                {"worker_id": 2, "hostname": "Node-2", "state": "offline"},
                {"worker_id": 3, "hostname": "Node-3", "state": "inprogress"},
            ]
        }
        online = list_online_workers(mock_client)
        self.assertEqual(len(online), 2)
        ids = [w["worker_id"] for w in online]
        self.assertIn(1, ids)
        self.assertIn(3, ids)
        self.assertNotIn(2, ids)

    def test_update_remote_worker_target_not_found(self) -> None:
        """Verify update_remote_worker fails cleanly when target is missing."""
        mock_client = MagicMock()
        mock_client.workerList.return_value = {"workers": []}
        mock_metashape = MagicMock()
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("autometashape.cluster.updater.Metashape", mock_metashape):
                res = update_remote_worker(mock_client, "NonExistentNode", tmp_dir)
                self.assertFalse(res["success"])
                self.assertIn("not found", res["error"])

    def test_update_remote_worker_success_lifecycle(self) -> None:
        """Verify full remote update and auto-reconnect restart lifecycle."""
        mock_client = MagicMock()
        mock_client.workerList.side_effect = [
            # Initial lookup
            {"workers": [{"worker_id": 1, "hostname": "Admin-pc", "hostaddr": "10.5.10.254:54390", "state": "ready"}]},
            # Polling for batch 10
            {"workers": [{"worker_id": 1, "hostname": "Admin-pc", "hostaddr": "10.5.10.254:54390", "state": "ready"}]},
            # After quitWorker: old instance offline / new instance online
            {
                "workers": [
                    {"worker_id": 1, "hostname": "Admin-pc", "hostaddr": "10.5.10.254:54390", "state": "offline"},
                    {"worker_id": 2, "hostname": "Admin-pc", "hostaddr": "10.5.10.254:60082", "state": "ready"},
                ]
            },
        ]
        mock_client.createBatch.return_value = 10
        mock_client.batchList.return_value = {"batches": [{"batch_id": 10, "state": "completed"}]}

        mock_metashape = MagicMock()
        mock_task = MagicMock()
        mock_net_task = MagicMock()
        mock_task.toNetworkTask.return_value = mock_net_task
        mock_metashape.Tasks.RunScript.return_value = mock_task

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("autometashape.cluster.updater.Metashape", mock_metashape):
                res = update_remote_worker(
                    client=mock_client,
                    worker_id_or_name=1,
                    shared_root=tmp_dir,
                    timeout=5.0,
                    restart=True,
                    poll_interval=0.01,
                )

                self.assertTrue(res["success"])
                self.assertEqual(res["batch_id"], 10)
                self.assertTrue(res["relaunched"])
                self.assertEqual(res["new_worker_id"], 2)
                mock_client.createBatch.assert_called_once()
                mock_client.quitWorker.assert_called_once_with(1)

    def test_build_arg_parser(self) -> None:
        """Verify CLI argument parser for update_fleet."""
        parser = build_arg_parser()
        args = parser.parse_args(["--host", "10.0.0.1", "--root", r"\\SRV\Share", "--node", "canary"])
        self.assertEqual(args.host, "10.0.0.1")
        self.assertEqual(args.root, r"\\SRV\Share")
        self.assertEqual(args.node, "canary")
        self.assertFalse(args.no_restart)
        self.assertTrue(args.force)

        # Test safe flag overrides force default
        safe_args = parser.parse_args(["--safe"])
        self.assertFalse(safe_args.force)
        no_force_args = parser.parse_args(["--no-force"])
        self.assertFalse(no_force_args.force)

    def test_generate_worker_updater_code_node_lock(self) -> None:
        """Verify target worker node hostname lock is generated in script."""
        code = generate_worker_updater_code("https://example.com/repo.zip", expected_hostname="Node-Alpha")
        self.assertIn("node-alpha", code)
        self.assertIn("Target node mismatch", code)
        self.assertIn("raise RuntimeError", code)

    def test_update_remote_worker_safe_mode_skips_busy(self) -> None:
        """Verify update_remote_worker skips busy worker when force=False."""
        mock_client = MagicMock()
        mock_client.workerList.return_value = {
            "workers": [
                {"worker_id": 1, "hostname": "Worker-1", "state": "inprogress"},
            ]
        }
        mock_metashape = MagicMock()
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("autometashape.cluster.updater.Metashape", mock_metashape):
                res = update_remote_worker(
                    client=mock_client,
                    worker_id_or_name=1,
                    shared_root=tmp_dir,
                    force=False,
                )
                self.assertFalse(res["success"])
                self.assertIn("currently inprogress", res["error"])
                self.assertIn("safe mode", res["error"])
                mock_client.createBatch.assert_not_called()

    def test_update_remote_worker_force_mode_aborts_busy_worker(self) -> None:
        """Verify update_remote_worker aborts busy task when force=True (default)."""
        mock_client = MagicMock()
        mock_client.workerList.side_effect = [
            # Initial lookup: worker is busy
            {"workers": [{"worker_id": 1, "hostname": "Worker-1", "hostaddr": "10.0.0.1:5000", "state": "inprogress"}]},
            # Polling after abortBatch: worker becomes ready
            {"workers": [{"worker_id": 1, "hostname": "Worker-1", "hostaddr": "10.0.0.1:5000", "state": "ready"}]},
            # Polling before batch creation
            {"workers": [{"worker_id": 1, "hostname": "Worker-1", "hostaddr": "10.0.0.1:5000", "state": "ready"}]},
            # Post restart lookup
            {"workers": [{"worker_id": 2, "hostname": "Worker-1", "hostaddr": "10.0.0.1:5001", "state": "ready"}]},
        ]
        mock_client.batchList.return_value = {
            "batches": [
                {"batch_id": 50, "state": "inprogress"},
                {"batch_id": 99, "state": "completed"},
            ]
        }
        mock_client.createBatch.return_value = 99

        mock_metashape = MagicMock()
        mock_task = MagicMock()
        mock_task.toNetworkTask.return_value = MagicMock()
        mock_metashape.Tasks.RunScript.return_value = mock_task

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("autometashape.cluster.updater.Metashape", mock_metashape):
                res = update_remote_worker(
                    client=mock_client,
                    worker_id_or_name=1,
                    shared_root=tmp_dir,
                    timeout=5.0,
                    restart=True,
                    poll_interval=0.01,
                    force=True,
                )
                self.assertTrue(res["success"])
                # Verified active batch was aborted
                mock_client.abortBatch.assert_called_with(50)
                # Verified batch was locked to 1 worker
                mock_client.setBatchWorkerLimit.assert_called_with(99, 1)

    def test_update_remote_worker_isolates_other_workers(self) -> None:
        """Verify other workers are temporarily paused and restored during target update."""
        mock_client = MagicMock()
        mock_client.workerList.side_effect = [
            # 2 workers online
            {
                "workers": [
                    {"worker_id": 1, "hostname": "Target-Node", "state": "ready", "paused": False},
                    {"worker_id": 2, "hostname": "Other-Node", "state": "ready", "paused": False},
                ]
            },
            # Polling during isolation
            {
                "workers": [
                    {"worker_id": 1, "hostname": "Target-Node", "state": "ready", "paused": False},
                    {"worker_id": 2, "hostname": "Other-Node", "state": "ready", "paused": True},
                ]
            },
            # Post restart
            {
                "workers": [
                    {"worker_id": 3, "hostname": "Target-Node", "state": "ready", "paused": False},
                    {"worker_id": 2, "hostname": "Other-Node", "state": "ready", "paused": True},
                ]
            },
        ]
        mock_client.createBatch.return_value = 100
        mock_client.batchList.return_value = {"batches": [{"batch_id": 100, "state": "completed"}]}

        mock_metashape = MagicMock()
        mock_task = MagicMock()
        mock_task.toNetworkTask.return_value = MagicMock()
        mock_metashape.Tasks.RunScript.return_value = mock_task

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("autometashape.cluster.updater.Metashape", mock_metashape):
                res = update_remote_worker(
                    client=mock_client,
                    worker_id_or_name=1,
                    shared_root=tmp_dir,
                    timeout=5.0,
                    restart=True,
                    poll_interval=0.01,
                )
                self.assertTrue(res["success"])
                # Verified other worker was paused during batch
                mock_client.setWorkerPaused.assert_any_call(2, True)
                # Verified other worker was restored in finally teardown
                mock_client.setWorkerPaused.assert_any_call(2, False)


if __name__ == "__main__":
    unittest.main()
