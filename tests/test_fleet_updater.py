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

from autometashape.cluster.hardware import find_metashape_python_path
from autometashape.cluster.updater import (
    CLUSTER_MAINTENANCE_REL_PATH,
    DEFAULT_REPO_ARCHIVE_URL,
    UPDATE_SIGNAL_FILENAME,
    ensure_cluster_maintenance_project,
    generate_worker_updater_code,
    list_online_workers,
    mirror_directory_with_prune,
    perform_worker_codebase_update,
    stage_cluster_update_payload,
    update_all_workers_parallel,
    update_remote_worker,
)

from scripts.launch_worker import check_update_pending
from scripts.update_fleet import build_arg_parser


class TestFleetUpdater(unittest.TestCase):
    """Test suite for cluster worker remote updating and lifecycle management."""

    def test_generate_worker_updater_code(self) -> None:
        """Verify generated remote worker updater script content."""
        code = generate_worker_updater_code("https://example.com/repo.zip")
        self.assertIn("https://example.com/repo.zip", code)
        self.assertIn("Documents", code)
        self.assertIn("AutoMetashape", code)
        self.assertIn(".update_pending", code)
        self.assertIn("update_result.txt", code)

    def test_ensure_cluster_maintenance_project(self) -> None:
        """Verify maintenance project creation on shared storage and lock cleanup."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create a stale lock file
            files_dir = os.path.join(tmp_dir, ".cluster", "maintenance.files")
            os.makedirs(files_dir, exist_ok=True)
            lock_path = os.path.join(files_dir, "lock")
            with open(lock_path, "w") as f:
                f.write("stale_lock")
            self.assertTrue(os.path.isfile(lock_path))

            rel = ensure_cluster_maintenance_project(tmp_dir)
            self.assertEqual(rel, CLUSTER_MAINTENANCE_REL_PATH)
            # Assert lock file was deleted
            self.assertFalse(os.path.isfile(lock_path))
            full_path = os.path.join(tmp_dir, ".cluster", "maintenance.psx")
            self.assertTrue(os.path.isfile(full_path))
            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("<document", content)
            self.assertIn("maintenance.files", content)

    def test_stage_cluster_update_payload(self) -> None:
        """Verify update payload staging to shared network directory."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            cluster_dir = os.path.join(tmp_dir, ".cluster")
            os.makedirs(cluster_dir, exist_ok=True)
            payload_path = os.path.join(cluster_dir, "update_payload.zip")
            # Create a valid pre-existing dummy zip
            with open(payload_path, "wb") as f:
                f.write(b"PK" + b"\x00" * 2048)

            staged = stage_cluster_update_payload(tmp_dir)
            self.assertEqual(staged, payload_path)
            self.assertTrue(os.path.isfile(staged))

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

    def test_update_remote_worker_timeout_aborts_batch(self) -> None:
        """Verify update_remote_worker aborts timed-out batch on the server."""
        mock_client = MagicMock()
        mock_client.workerList.return_value = {
            "workers": [
                {"worker_id": 1, "hostname": "Target-Node", "state": "ready", "paused": False},
            ]
        }
        mock_client.createBatch.return_value = 202
        # Never completes
        mock_client.batchList.return_value = {"batches": [{"batch_id": 202, "state": "inprogress"}]}

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
                    timeout=0.05,
                    restart=True,
                    poll_interval=0.01,
                )
                self.assertFalse(res["success"])
                self.assertIn("timed out", res["error"])
                # Verified batch was cleanly aborted on server to prevent orphan lingering
                mock_client.abortBatch.assert_called_with(202)

    def test_mirror_directory_with_prune(self) -> None:
        """Verify Git-parity file mirroring and obsolete file pruning with config preservation."""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            # Source (new codebase)
            with open(os.path.join(src, "active.py"), "w") as f:
                f.write("print('v2')")
            with open(os.path.join(src, "new_feature.py"), "w") as f:
                f.write("print('new')")

            # Destination (installed worker)
            with open(os.path.join(dst, "active.py"), "w") as f:
                f.write("print('v1')")
            with open(os.path.join(dst, "obsolete_file.py"), "w") as f:
                f.write("print('deprecated')")
            with open(os.path.join(dst, "worker_config.json"), "w") as f:
                f.write('{"host": "server:5840", "root": "\\\\share"}')
            with open(os.path.join(dst, "runtime.log"), "w") as f:
                f.write("log data")

            copied, pruned = mirror_directory_with_prune(src, dst)
            self.assertEqual(copied, 2)
            self.assertEqual(pruned, 1)

            # Assert new files copied
            self.assertTrue(os.path.isfile(os.path.join(dst, "new_feature.py")))
            with open(os.path.join(dst, "active.py")) as f:
                self.assertEqual(f.read(), "print('v2')")

            # Assert obsolete file pruned
            self.assertFalse(os.path.exists(os.path.join(dst, "obsolete_file.py")))

            # Assert local configs and logs preserved
            self.assertTrue(os.path.isfile(os.path.join(dst, "worker_config.json")))
            self.assertTrue(os.path.isfile(os.path.join(dst, "runtime.log")))

    def test_check_update_pending(self) -> None:
        """Verify supervisor correctly detects .update_pending signal file and loads payload."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            self.assertIsNone(check_update_pending(tmp_dir))

            flag_file = os.path.join(tmp_dir, UPDATE_SIGNAL_FILENAME)
            with open(flag_file, "w", encoding="utf-8") as f:
                f.write('{"repo_url": "https://example.com/repo.zip", "shared_cluster_dir": "C:\\temp"}')

            data = check_update_pending(tmp_dir)
            self.assertIsNotNone(data)
            self.assertEqual(data.get("repo_url"), "https://example.com/repo.zip")
            self.assertEqual(data.get("_flag_path"), flag_file)

    def test_find_metashape_python_path(self) -> None:
        """Verify discovery of Metashape Pro bundled Python executable."""
        py_path = find_metashape_python_path()
        self.assertIsNotNone(py_path)
        self.assertTrue(isinstance(py_path, str))

    def test_update_all_workers_parallel(self) -> None:
        """Verify parallel rollout dispatches update batches across all online workers."""
        mock_client = MagicMock()
        mock_client.workerList.return_value = {
            "workers": [
                {"worker_id": 1, "hostname": "Worker-A", "state": "ready"},
                {"worker_id": 2, "hostname": "Worker-B", "state": "ready"},
            ]
        }
        mock_client.createBatch.side_effect = [101, 102]
        mock_client.batchList.return_value = {
            "batches": [
                {"batch_id": 101, "state": "completed"},
                {"batch_id": 102, "state": "completed"},
            ]
        }

        mock_metashape = MagicMock()
        mock_task = MagicMock()
        mock_task.toNetworkTask.return_value = MagicMock()
        mock_metashape.Tasks.RunScript.return_value = mock_task

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch("autometashape.cluster.updater.Metashape", mock_metashape):
                results = update_all_workers_parallel(
                    client=mock_client,
                    shared_root=tmp_dir,
                    timeout=5.0,
                    restart=False,
                    poll_interval=0.01,
                )
                self.assertEqual(len(results), 2)
                self.assertTrue(results[0]["success"])
                self.assertTrue(results[1]["success"])
                self.assertEqual(results[0]["batch_id"], 101)
                self.assertEqual(results[1]["batch_id"], 102)

    def test_list_online_workers_excludes_aborted_ghosts(self) -> None:
        """Verify stale aborted=True ghost workers are excluded from online update targets."""
        mock_client = MagicMock()
        mock_client.workerList.return_value = {
            "workers": [
                {"worker_id": 5, "hostname": "node-a", "state": "inprogress", "aborted": True},
                {"worker_id": 6, "hostname": "node-b", "state": "offline", "aborted": False},
                {"worker_id": 17, "hostname": "node-a", "state": "ready", "aborted": False},
            ]
        }
        online = list_online_workers(mock_client)
        self.assertEqual([w["worker_id"] for w in online], [17])


if __name__ == "__main__":
    unittest.main()
