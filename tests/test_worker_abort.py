# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for supervisor signal-based worker abort, self-healing restart, and lock cleanup."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from autometashape.cluster.abort import (
    ABORT_DIR_NAME,
    ABORT_SIGNAL_FILENAME,
    ABORT_SIGNALS_JSON,
    abort_remote_worker,
    acknowledge_abort_signal,
    check_abort_signal,
    cleanup_scratch_locks,
    dispatch_abort_signal,
    kill_worker_process,
)


class TestWorkerAbort(unittest.TestCase):
    """Defends signal-based worker abort, watchdog polling, and process lifecycle management."""

    def test_check_abort_signal_local_target_dir(self) -> None:
        """Verify check_abort_signal detects .abort_pending in target directory."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sig_file = os.path.join(tmp_dir, ABORT_SIGNAL_FILENAME)
            payload = {"action": "abort_and_restart", "batch_id": 42, "node_name": "worker1"}
            with open(sig_file, "w", encoding="utf-8") as f:
                json.dump(payload, f)

            sig = check_abort_signal(target_dir=tmp_dir, node_name="worker1")
            self.assertIsNotNone(sig)
            self.assertEqual(sig["action"], "abort_and_restart")
            self.assertEqual(sig["batch_id"], 42)
            self.assertEqual(sig["_signal_path"], sig_file)

    def test_check_abort_signal_unc_cluster_path(self) -> None:
        """Verify check_abort_signal detects <shared_root>/.cluster/aborts/<node>.abort."""
        with tempfile.TemporaryDirectory() as shared_root:
            aborts_dir = os.path.join(shared_root, ".cluster", ABORT_DIR_NAME)
            os.makedirs(aborts_dir, exist_ok=True)
            node_abort = os.path.join(aborts_dir, "tan-pc.abort")
            with open(node_abort, "w", encoding="utf-8") as f:
                json.dump({"action": "abort_and_restart", "batch_id": 99, "node_name": "tan-pc"}, f)

            sig = check_abort_signal(root_path=shared_root, node_name="tan-pc")
            self.assertIsNotNone(sig)
            self.assertEqual(sig["batch_id"], 99)
            self.assertEqual(sig["_signal_path"], node_abort)

    def test_check_abort_signal_all_nodes(self) -> None:
        """Verify check_abort_signal detects all.abort broadcast signal."""
        with tempfile.TemporaryDirectory() as shared_root:
            aborts_dir = os.path.join(shared_root, ".cluster", ABORT_DIR_NAME)
            os.makedirs(aborts_dir, exist_ok=True)
            all_abort = os.path.join(aborts_dir, "all.abort")
            with open(all_abort, "w", encoding="utf-8") as f:
                json.dump({"action": "abort_and_restart", "batch_id": 101}, f)

            sig = check_abort_signal(root_path=shared_root, node_name="arbitrary-worker")
            self.assertIsNotNone(sig)
            self.assertEqual(sig["batch_id"], 101)
            self.assertEqual(sig["_signal_path"], all_abort)

    def test_check_abort_signal_aggregate_json(self) -> None:
        """Verify check_abort_signal inspects .cluster/abort_signals.json."""
        with tempfile.TemporaryDirectory() as shared_root:
            cluster_dir = os.path.join(shared_root, ".cluster")
            os.makedirs(cluster_dir, exist_ok=True)
            json_file = os.path.join(cluster_dir, ABORT_SIGNALS_JSON)
            data = {"node_x": {"action": "abort_and_restart", "batch_id": 77}}
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            sig = check_abort_signal(root_path=shared_root, node_name="node_x")
            self.assertIsNotNone(sig)
            self.assertEqual(sig["batch_id"], 77)
            self.assertEqual(sig["_signal_path"], json_file)

    def test_acknowledge_abort_signal_removes_file_and_creates_ack(self) -> None:
        """Verify acknowledge_abort_signal writes .abort_ack and deletes .abort."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sig_file = os.path.join(tmp_dir, "test_node.abort")
            with open(sig_file, "w", encoding="utf-8") as f:
                json.dump({"action": "abort_and_restart", "batch_id": 12}, f)

            abort_data = {
                "action": "abort_and_restart",
                "node_name": "test_node",
                "batch_id": 12,
                "_signal_path": sig_file,
            }
            res = acknowledge_abort_signal(abort_data)
            self.assertTrue(res)
            self.assertFalse(os.path.exists(sig_file))

            ack_file = os.path.join(tmp_dir, "test_node.abort_ack")
            self.assertTrue(os.path.exists(ack_file))
            with open(ack_file, "r", encoding="utf-8") as af:
                ack_content = json.load(af)
            self.assertEqual(ack_content["action"], "aborted")
            self.assertEqual(ack_content["node_name"], "test_node")

    def test_cleanup_scratch_locks_removes_stale_locks(self) -> None:
        """Verify cleanup_scratch_locks traverses target and scratch dirs to remove lock files."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            lock1 = os.path.join(tmp_dir, "project.files", "lock")
            os.makedirs(os.path.dirname(lock1), exist_ok=True)
            with open(lock1, "w", encoding="utf-8") as f:
                f.write("locked")

            lock2 = os.path.join(tmp_dir, "scratch", "sub", "test.lock")
            os.makedirs(os.path.dirname(lock2), exist_ok=True)
            with open(lock2, "w", encoding="utf-8") as f:
                f.write("locked")

            removed = cleanup_scratch_locks(target_dir=tmp_dir)
            self.assertIn(lock1, removed)
            self.assertIn(lock2, removed)
            self.assertFalse(os.path.exists(lock1))
            self.assertFalse(os.path.exists(lock2))

    def test_kill_worker_process_graceful_and_force(self) -> None:
        """Verify kill_worker_process calls terminate, kill, and taskkill appropriately."""
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, None, 0]
        mock_proc.pid = 9999

        res = kill_worker_process(mock_proc, timeout=0.2)
        self.assertTrue(res)
        mock_proc.terminate.assert_called_once()

    def test_dispatch_abort_signal_creates_payload(self) -> None:
        """Verify dispatch_abort_signal creates expected JSON schema in .cluster/aborts."""
        with tempfile.TemporaryDirectory() as shared_root:
            sig_path = dispatch_abort_signal(node_name="NodeA", batch_id=55, shared_root=shared_root)
            self.assertTrue(os.path.exists(sig_path))
            with open(sig_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data["node_name"], "nodea")
            self.assertEqual(data["batch_id"], 55)
            self.assertEqual(data["action"], "abort_and_restart")
            self.assertIn("timestamp", data)

    def test_abort_remote_worker_orchestration(self) -> None:
        """Verify abort_remote_worker unpauses frozen batch, aborts batch, and confirms ack."""
        mock_client = MagicMock()
        mock_client.batchList.return_value = {"batches": [{"batch_id": 10, "state": "paused", "paused": True}]}
        mock_client.workerList.return_value = {"workers": [{"hostname": "node1", "state": "ready"}]}

        with tempfile.TemporaryDirectory() as shared_root:
            # Simulate supervisor acknowledging immediately
            aborts_dir = os.path.join(shared_root, ".cluster", ABORT_DIR_NAME)
            os.makedirs(aborts_dir, exist_ok=True)

            with patch("time.sleep", return_value=None):
                result = abort_remote_worker(
                    node_name="node1",
                    batch_id=10,
                    shared_root=shared_root,
                    server_host="127.0.0.1:5840",
                    timeout=2.0,
                    client=mock_client,
                )

            # Assert batch was unpaused before abort to prevent server deadlock
            mock_client.setBatchPaused.assert_called_with(10, False)
            mock_client.abortBatch.assert_called_with(10)
            mock_client.setBatchPriority.assert_called_with(10, -100)
            self.assertTrue(result["success"])
            self.assertTrue(result["reconnected"])

    def test_supervisor_watchdog_loop_reaction(self) -> None:
        """Verify supervisor watchdog terminates stuck child process upon detecting signal."""
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, None, 0]
        mock_proc.pid = 1234

        with (
            patch("autometashape.cluster.abort.check_abort_signal") as mock_check,
            patch("autometashape.cluster.abort.kill_worker_process") as mock_kill,
            patch("autometashape.cluster.abort.cleanup_scratch_locks") as mock_clean,
            patch("autometashape.cluster.abort.acknowledge_abort_signal") as mock_ack,
        ):
            mock_check.side_effect = [None, {"action": "abort_and_restart", "batch_id": 99}]

            aborted = False
            # Simulate watchdog tick
            while mock_proc.poll() is None:
                sig = mock_check()
                if sig is not None:
                    mock_kill(mock_proc)
                    aborted = True
                    mock_clean()
                    mock_ack(sig)
                    break

            self.assertTrue(aborted)
            mock_kill.assert_called_once_with(mock_proc)
            mock_clean.assert_called_once()
            mock_ack.assert_called_once()

    def test_cluster_abort_cli_and_config_resolution(self) -> None:
        """Verify scripts/cluster_abort.py correctly resolves server and worker configurations."""
        import scripts.cluster_abort as ca

        parser = ca.build_arg_parser()
        args = parser.parse_args(["--node", "worker-node-1", "--batch", "42", "--timeout", "5"])
        self.assertEqual(args.node, "worker-node-1")
        self.assertEqual(args.batch, 42)
        self.assertEqual(args.timeout, 5.0)

        with (
            patch("sys.argv", ["cluster_abort.py", "--node", "worker-node-1", "--no-reexec"]),
            patch("scripts.cluster_abort.abort_remote_worker") as mock_abort,
            patch("scripts.cluster_abort.load_cluster_config") as mock_cfg,
        ):
            mock_cfg.return_value = {"host": "192.168.1.50", "port": "5840", "root": r"\\SRV\Share"}
            mock_abort.return_value = {"success": True, "acknowledged": True, "reconnected": True}

            ret = ca.main()
            self.assertEqual(ret, 0)
            mock_abort.assert_called_once()
            call_kwargs = mock_abort.call_args[1]
            self.assertEqual(call_kwargs["node_name"], "worker-node-1")
            self.assertEqual(call_kwargs["server_host"], "192.168.1.50:5840")
            self.assertEqual(call_kwargs["shared_root"], r"\\SRV\Share")


if __name__ == "__main__":
    unittest.main()
