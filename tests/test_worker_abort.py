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
    _collect_descendant_pids_from_edges,
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
        """Verify kill_worker_process calls terminate, kill on timeout, and mocks OS process tools hermetically."""
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, None, 0]
        mock_proc.pid = 9999

        with (
            patch("autometashape.cluster.abort._enumerate_windows_descendant_pids", return_value=[]),
            patch("autometashape.cluster.abort.subprocess.run", return_value=MagicMock(returncode=0)),
            patch("autometashape.cluster.abort.os.killpg", create=True),
        ):
            res = kill_worker_process(mock_proc, timeout=0.2)
        self.assertTrue(res)
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_not_called()

        # Force-kill path when process never exits within timeout
        stuck_proc = MagicMock()
        stuck_proc.poll.return_value = None
        stuck_proc.pid = 9998
        with (
            patch("autometashape.cluster.abort._enumerate_windows_descendant_pids", return_value=[]),
            patch("autometashape.cluster.abort.subprocess.run", return_value=MagicMock(returncode=0)),
            patch("autometashape.cluster.abort.os.killpg", create=True),
            patch("autometashape.cluster.abort.time.sleep", return_value=None),
        ):
            res_force = kill_worker_process(stuck_proc, timeout=0.0)
        self.assertTrue(res_force)
        stuck_proc.kill.assert_called_once()

    def test_kill_worker_process_windows_reaps_tree_and_children_before_terminate(self) -> None:
        """On Windows, taskkill /F /T and child reaping MUST run before proc.terminate() to prevent stdio orphans."""
        events: list[str] = []
        mock_proc = MagicMock()
        mock_proc.pid = 26688
        mock_proc.poll.side_effect = [None, 0]
        mock_proc.terminate.side_effect = lambda: events.append("terminate")

        def fake_run(cmd, **kwargs):
            if isinstance(cmd, list) and cmd[:4] == ["taskkill", "/F", "/T", "/PID"]:
                events.append(f"taskkill:{cmd[4]}")
            return MagicMock(returncode=0)

        with (
            patch("sys.platform", "win32"),
            patch("autometashape.cluster.abort._enumerate_windows_descendant_pids", return_value=[30692, 30700]),
            patch("autometashape.cluster.abort.subprocess.run", side_effect=fake_run),
        ):
            self.assertTrue(kill_worker_process(mock_proc, timeout=0.2))

        self.assertEqual(events, ["taskkill:26688", "taskkill:30692", "taskkill:30700", "terminate"])

    def test_collect_descendant_pids_leaves_first_and_cycle_safe(self) -> None:
        """Verify multi-level process-tree traversal returns leaves first and ignores cycles."""
        edges = {26688: [30692, 30693], 30692: [40100], 40100: [26688]}
        desc = _collect_descendant_pids_from_edges(26688, edges)
        self.assertEqual(desc, [40100, 30693, 30692])
        self.assertNotIn(26688, desc)

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
        """Verify real launch_worker supervisor loop detects abort signal, kills child, cleans locks, and acks."""
        import launch_worker as lw

        with tempfile.TemporaryDirectory() as tmpdir:
            shared_root = os.path.join(tmpdir, "share")
            target_dir = os.path.join(tmpdir, "repo")
            cfg_file = os.path.join(tmpdir, "worker_config.json")
            fake_ms = os.path.join(tmpdir, "metashape.exe")
            os.makedirs(shared_root, exist_ok=True)
            os.makedirs(target_dir, exist_ok=True)
            with open(fake_ms, "w", encoding="utf-8") as f:
                f.write("stub")

            lock_file = os.path.join(target_dir, "project.files", "lock")
            os.makedirs(os.path.dirname(lock_file), exist_ok=True)
            with open(lock_file, "w", encoding="utf-8") as f:
                f.write("locked")

            dispatch_abort_signal(node_name="watchdog-node", batch_id=99, shared_root=shared_root)

            mock_proc = MagicMock()
            mock_proc.pid = 4321
            mock_proc.returncode = -1
            poll_state = {"killed": False}

            def _poll():
                return -1 if poll_state["killed"] else None

            def _kill(proc):
                poll_state["killed"] = True
                return True

            mock_proc.poll.side_effect = _poll

            with (
                patch.dict(os.environ, {"AUTOMETASHAPE_DIR": target_dir}),
                patch("cluster_utils.find_metashape_path", return_value=fake_ms),
                patch("cluster_utils.probe_metashape_gpu_mask", return_value=(1, [])),
                patch("cluster_utils.test_tcp_connection", return_value=True),
                patch("cluster_utils.test_unc_path", return_value=(True, "")),
                patch("autometashape.cluster.abort.get_local_hostname", return_value="watchdog-node"),
                patch("cluster_utils.get_local_hostname", return_value="watchdog-node"),
                patch("cluster_utils.kill_worker_process", side_effect=_kill) as mock_kill,
                patch("subprocess.Popen", return_value=mock_proc),
                patch("time.sleep", return_value=None),
                patch(
                    "sys.argv",
                    [
                        "launch_worker.py",
                        "--headless",
                        "--no-reconnect",
                        "--host",
                        "127.0.0.1",
                        "--root",
                        shared_root,
                        "--config",
                        cfg_file,
                    ],
                ),
            ):
                lw._run_worker_lifecycle()

            mock_kill.assert_called_once_with(mock_proc)
            self.assertFalse(os.path.exists(lock_file))
            ack_path = os.path.join(shared_root, ".cluster", ABORT_DIR_NAME, "watchdog-node.abort_ack")
            self.assertTrue(os.path.isfile(ack_path))
            self.assertIsNone(check_abort_signal(root_path=shared_root, node_name="watchdog-node"))

    def test_all_abort_broadcast_acknowledges_per_node_without_deleting_for_siblings(self) -> None:
        """Verify all.abort leaves broadcast intact for sibling workers while preventing self double-kill."""
        with tempfile.TemporaryDirectory() as shared_root:
            aborts_dir = os.path.join(shared_root, ".cluster", ABORT_DIR_NAME)
            os.makedirs(aborts_dir, exist_ok=True)
            all_abort = os.path.join(aborts_dir, "all.abort")
            with open(all_abort, "w", encoding="utf-8") as f:
                json.dump({"action": "abort_and_restart", "batch_id": 77, "timestamp": time.time() - 5.0}, f)

            sig_a = check_abort_signal(root_path=shared_root, node_name="NodeA")
            self.assertIsNotNone(sig_a)
            acknowledge_abort_signal(sig_a)

            # Broadcast file still exists for sibling NodeB, but NodeA does NOT re-trigger
            self.assertTrue(os.path.isfile(all_abort))
            self.assertIsNone(check_abort_signal(root_path=shared_root, node_name="NodeA"))
            sig_b = check_abort_signal(root_path=shared_root, node_name="NodeB")
            self.assertIsNotNone(sig_b)

    def test_abort_remote_worker_fallback_passes_args_via_argv_not_code_interpolation(self) -> None:
        """Verify Metashape subprocess fallback in abort_remote_worker never interpolates untrusted strings into code."""
        captured_cmd: list[str] = []
        captured_script: list[str] = []

        def _fake_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            if "-r" in cmd:
                script_p = cmd[cmd.index("-r") + 1]
                with open(script_p, "r", encoding="utf-8") as sf:
                    captured_script.append(sf.read())
            return MagicMock(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as shared_root:
            fake_ms = os.path.join(shared_root, "metashape.exe")
            with open(fake_ms, "w", encoding="utf-8") as f:
                f.write("stub")
            evil_host = "127.0.0.1'); import os; os.system('calc')#:5840"
            with (
                patch("autometashape.cluster.abort.Metashape", None),
                patch("autometashape.cluster.hardware.find_metashape_path", return_value=fake_ms),
                patch("autometashape.cluster.abort.subprocess.run", side_effect=_fake_run),
                patch("time.sleep", return_value=None),
            ):
                abort_remote_worker(
                    node_name="node1",
                    batch_id=12,
                    shared_root=shared_root,
                    server_host=evil_host,
                    timeout=0.05,
                )

            self.assertEqual(len(captured_script), 1)
            self.assertNotIn("os.system", captured_script[0])
            self.assertIn("sys.argv", captured_script[0])

    def test_remote_abort_never_executes_local_taskkill(self) -> None:
        """Verify remote abort targeting another node never runs local process kill commands."""
        mock_client = MagicMock()
        mock_client.batchList.return_value = {"batches": [{"batch_id": 99, "state": "running"}]}
        mock_client.workerList.return_value = {"workers": [{"hostname": "remote_node", "state": "ready"}]}

        with tempfile.TemporaryDirectory() as shared_root:
            with (
                patch("subprocess.run") as mock_subproc,
                patch("time.sleep", return_value=None),
            ):
                result = abort_remote_worker(
                    node_name="remote_node",
                    batch_id=99,
                    shared_root=shared_root,
                    server_host="127.0.0.1:5840",
                    timeout=1.0,
                    client=mock_client,
                )

                # Ensure subprocess.run was NEVER called with taskkill
                for call_args in mock_subproc.call_args_list:
                    cmd_list = call_args[0][0] if call_args[0] else []
                    self.assertNotIn("taskkill", cmd_list)
                    self.assertNotIn("/IM", cmd_list)

                self.assertTrue(result["success"])

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
