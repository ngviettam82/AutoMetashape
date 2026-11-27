# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Wave A adversarial regression tests for P0 safety, data-integrity, and cluster fixes."""

from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
for _p in (_SOURCE_DIR, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from autometashape.cluster.abort import (
    ABORT_DIR_NAME,
    ABORT_SIGNALS_JSON,
    acknowledge_abort_signal,
    check_abort_signal,
    cleanup_scratch_locks,
    dispatch_abort_signal,
)
from autometashape.cluster.dual_share import extract_path_leaf_name, map_source_to_result_dir
from autometashape.cluster.orchestrator import (
    _find_blocking_batch_id,
    clear_stale_project_lock,
    safe_create_cluster_batch,
)
from autometashape.cluster.staging import reconcile_staged_psx_chunk_id, sync_staged_project_to_network
from autometashape.cluster.supervisor import QueueJob
from autometashape.core import export as export_mod
from autometashape.core.telemetry import discover_image_subfolder_groups
from autometashape.ipc import protocol as ipc_mod


class TestWaveASafetyAndIntegrity(unittest.TestCase):
    """Defend Wave A P0 safety invariants against regression."""

    def test_all_abort_broadcast_never_loops_into_aggregate_json(self) -> None:
        """Broadcast 'all' ack prevents aggregate JSON loops and preserves sibling delivery."""
        with tempfile.TemporaryDirectory() as shared_root:
            sig_path = dispatch_abort_signal("all", batch_id=501, shared_root=shared_root)
            self.assertTrue(os.path.isfile(sig_path))

            agg_json = os.path.join(shared_root, ".cluster", ABORT_SIGNALS_JSON)
            if os.path.isfile(agg_json):
                with open(agg_json, "r", encoding="utf-8") as af:
                    self.assertNotIn("all", json.load(af))

            # Worker A polls and acknowledges broadcast
            sig_a = check_abort_signal(root_path=shared_root, node_name="Worker-A.corp.local")
            self.assertIsNotNone(sig_a)
            self.assertEqual(sig_a["batch_id"], 501)
            self.assertTrue(acknowledge_abort_signal(sig_a))

            # Even if legacy abort_signals.json carries a matching 'all' signal_id, Worker A never loops
            with open(sig_path, "r", encoding="utf-8") as sf:
                sig_payload = json.load(sf)
            with open(agg_json, "w", encoding="utf-8") as af:
                json.dump({"all": sig_payload}, af)

            self.assertIsNone(check_abort_signal(root_path=shared_root, node_name="Worker-A.corp.local"))
            self.assertIsNone(check_abort_signal(root_path=shared_root, node_name="worker-a"))

            # Sibling Worker B still receives the broadcast
            sig_b = check_abort_signal(root_path=shared_root, node_name="Worker-B")
            self.assertIsNotNone(sig_b)
            self.assertEqual(sig_b["batch_id"], 501)

    def test_abort_signal_ttl_fqdn_and_path_traversal_sanitization(self) -> None:
        """Expired signals are ignored, FQDN/short hostnames match, and traversal chars cannot escape aborts/."""
        with tempfile.TemporaryDirectory() as shared_root:
            aborts_dir = os.path.join(shared_root, ".cluster", ABORT_DIR_NAME)
            os.makedirs(aborts_dir, exist_ok=True)

            # Traversal sanitization
            evil_sig = dispatch_abort_signal("../evil:node", batch_id=12, shared_root=shared_root)
            self.assertEqual(os.path.dirname(os.path.abspath(evil_sig)), os.path.abspath(aborts_dir))
            self.assertNotIn("..", os.path.basename(evil_sig))
            os.remove(evil_sig)

            # Expired signal is ignored cleanly
            stale_sig = os.path.join(aborts_dir, "render01.abort")
            with open(stale_sig, "w", encoding="utf-8") as fh:
                json.dump({"action": "abort_and_restart", "batch_id": 7, "timestamp": time.time() - 7200.0}, fh)
            self.assertIsNone(check_abort_signal(root_path=shared_root, node_name="render01.corp.local"))

            # Fresh short-name signal matches FQDN worker
            fresh_sig = dispatch_abort_signal("render01", batch_id=88, shared_root=shared_root)
            matched = check_abort_signal(root_path=shared_root, node_name="render01.corp.local")
            self.assertIsNotNone(matched)
            self.assertEqual(matched["_signal_path"], fresh_sig)
            self.assertEqual(matched["batch_id"], 88)

    def test_cleanup_scratch_locks_protects_package_locks_and_sibling_workers(self) -> None:
        """cleanup_scratch_locks must never delete repo/package locks or live sibling worker scratch locks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = os.path.join(tmpdir, "repo")
            scratch_root = os.path.join(tmpdir, "AutoMetashape_Scratch")
            os.makedirs(os.path.join(repo_dir, ".git"), exist_ok=True)

            uv_lock = os.path.join(repo_dir, "uv.lock")
            root_lock = os.path.join(repo_dir, "custom.lock")
            repo_proj_lock = os.path.join(repo_dir, "SiteA.files", "lock")
            os.makedirs(os.path.dirname(repo_proj_lock), exist_ok=True)
            for p in (uv_lock, root_lock, repo_proj_lock):
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write("lock")

            target_stage_lock = os.path.join(scratch_root, "SiteA_abc_1111_local_stage", "SiteA.files", "lock")
            sibling_stage_lock = os.path.join(scratch_root, "SiteB_xyz_2222_local_stage", "SiteB.files", "lock")
            os.makedirs(os.path.dirname(target_stage_lock), exist_ok=True)
            os.makedirs(os.path.dirname(sibling_stage_lock), exist_ok=True)
            with open(target_stage_lock, "w", encoding="utf-8") as fh:
                fh.write("dead")
            with open(sibling_stage_lock, "w", encoding="utf-8") as fh:
                fh.write("live")

            # Use scratch_root as target_dir as well, and verify repo_dir separately
            removed_repo = cleanup_scratch_locks(target_dir=repo_dir, worker_pid=1111)
            self.assertTrue(os.path.isfile(uv_lock))
            self.assertTrue(os.path.isfile(root_lock))
            self.assertFalse(os.path.exists(repo_proj_lock))
            self.assertIn(os.path.normpath(repo_proj_lock), [os.path.normpath(p) for p in removed_repo])

            removed_scratch = cleanup_scratch_locks(target_dir=scratch_root, worker_pid=1111)
            self.assertFalse(os.path.exists(target_stage_lock))
            self.assertIn(os.path.normpath(target_stage_lock), [os.path.normpath(p) for p in removed_scratch])
            self.assertTrue(os.path.isfile(sibling_stage_lock))

    def test_posix_launchers_use_start_new_session(self) -> None:
        """launch_worker and launch_server must pass start_new_session=True on POSIX so os.killpg works."""
        import launch_server as ls
        import launch_worker as lw

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ms = os.path.join(tmpdir, "metashape.sh")
            fake_srv = os.path.join(tmpdir, "metashape-server")
            cfg_worker = os.path.join(tmpdir, "worker_config.json")
            cfg_server = os.path.join(tmpdir, "server_config.json")
            for p in (fake_ms, fake_srv):
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write("#!/bin/sh\n")

            captured_worker_kwargs: list[dict] = []
            mock_proc = MagicMock(pid=1234, returncode=0)
            mock_proc.poll.return_value = 0

            def fake_worker_popen(cmd, **kwargs):
                captured_worker_kwargs.append(kwargs)
                return mock_proc

            with (
                patch("sys.platform", "linux"),
                patch.dict(os.environ, {"AUTOMETASHAPE_DIR": tmpdir}),
                patch("cluster_utils.find_metashape_path", return_value=fake_ms),
                patch("cluster_utils.probe_metashape_gpu_mask", return_value=(1, [])),
                patch("cluster_utils.test_tcp_connection", return_value=True),
                patch("cluster_utils.test_unc_path", return_value=(True, "")),
                patch("cluster_utils.check_abort_signal", return_value=None),
                patch("subprocess.Popen", side_effect=fake_worker_popen),
                patch(
                    "sys.argv",
                    [
                        "launch_worker.py",
                        "--headless",
                        "--no-reconnect",
                        "--host",
                        "127.0.0.1",
                        "--root",
                        tmpdir,
                        "--config",
                        cfg_worker,
                    ],
                ),
            ):
                lw._run_worker_lifecycle()
            self.assertTrue(captured_worker_kwargs and captured_worker_kwargs[0].get("start_new_session") is True)

            captured_server_kwargs: list[dict] = []

            def fake_server_popen(cmd, **kwargs):
                captured_server_kwargs.append(kwargs)
                m = MagicMock(returncode=0)
                m.wait.return_value = 0
                return m

            with (
                patch("sys.platform", "linux"),
                patch.dict(os.environ, {"AUTOMETASHAPE_DIR": tmpdir}),
                patch("cluster_utils.find_metashape_path", return_value=fake_ms),
                patch("cluster_utils.find_metashape_server_path", return_value=fake_srv),
                patch("cluster_utils.get_primary_lan_ip", return_value="10.0.0.5"),
                patch("cluster_utils.test_tcp_connection", return_value=False),
                patch("cluster_utils.save_cluster_config", return_value=cfg_server),
                patch("launch_server.check_update_pending", return_value=None),
                patch("subprocess.Popen", side_effect=fake_server_popen),
                patch(
                    "sys.argv",
                    [
                        "launch_server.py",
                        "--headless",
                        "--host",
                        "0.0.0.0",
                        "--port",
                        "5840",
                        "--config",
                        cfg_server,
                    ],
                ),
            ):
                rc = ls.main()
            self.assertEqual(rc, 0)
            self.assertTrue(captured_server_kwargs and captured_server_kwargs[0].get("start_new_session") is True)

    def test_duplicate_batch_respects_force_fresh_false_and_accepts_zero_id(self) -> None:
        """duplicate batch with force_fresh=False must attach to healthy/active/paused batches without abort/reap."""
        client = MagicMock()
        client.findBatch.side_effect = [-1, 0]
        client.batchList.side_effect = [
            {"batches": []},
            {"batches": [{"batch_id": 0, "path": "Site0/Site0.psx", "state": "active", "paused": False}]},
        ]
        client.batchInfo.return_value = {"batch_id": 0, "state": "active", "paused": False, "error_count": 0}
        client.createBatch.side_effect = RuntimeError("Duplicate batch: Site0/Site0.psx")

        bid, attached = safe_create_cluster_batch(client, "Site0/Site0.psx", [MagicMock()], force_fresh=False)
        self.assertEqual(bid, 0)
        self.assertTrue(attached)
        client.abortBatch.assert_not_called()
        client.setBatchPaused.assert_not_called()
        self.assertEqual(_find_blocking_batch_id(MagicMock(findBatch=lambda _p: 0), "Site0/Site0.psx"), 0)

    def test_clear_stale_project_lock_respects_posix_flock_and_absolute_paths(self) -> None:
        """clear_stale_project_lock supports absolute paths and refuses to unlink live POSIX-locked files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            abs_psx = os.path.join(tmpdir, "SiteAbs", "SiteAbs.psx")
            files_dir = os.path.join(tmpdir, "SiteAbs", "SiteAbs.files")
            os.makedirs(files_dir, exist_ok=True)
            lock_file = os.path.join(files_dir, "lock")
            with open(lock_file, "w", encoding="utf-8") as fh:
                fh.write("locked")

            fake_fcntl = types.SimpleNamespace(
                LOCK_EX=2,
                LOCK_NB=4,
                flock=MagicMock(side_effect=BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")),
            )
            with (
                patch("sys.platform", "linux"),
                patch.dict(sys.modules, {"fcntl": fake_fcntl}),
                patch("os.remove") as mock_rm,
            ):
                with self.assertRaises(RuntimeError):
                    clear_stale_project_lock("", abs_psx)
                mock_rm.assert_not_called()

    def test_sync_staged_project_symlink_enospc_and_rollback_safety(self) -> None:
        """Staging sync must guard symlink self-wipe, low space, Error 112 truncation, and partial .files rollback."""
        with tempfile.TemporaryDirectory() as tmp:
            stage_dir = os.path.join(tmp, "stage")
            net_dir = os.path.join(tmp, "net")
            os.makedirs(os.path.join(stage_dir, "S.files", "0"), exist_ok=True)
            with open(os.path.join(stage_dir, "S.psx"), "w", encoding="utf-8") as fh:
                fh.write("<psx/>")
            with open(os.path.join(stage_dir, "S.files", "0", "frame.zip"), "wb") as fh:
                fh.write(b"x" * 1024)
            with open(os.path.join(stage_dir, "S.obj"), "wb") as fh:
                fh.write(b"v 0 0 0\n" * 64)

            # 1. Symlink / realpath alias must return True without deleting stage_dir
            os.makedirs(net_dir, exist_ok=True)
            with (
                patch("autometashape.cluster.staging._is_same_physical_dir", return_value=True),
                patch("autometashape.cluster.staging.shutil.rmtree") as mock_rm,
            ):
                self.assertTrue(sync_staged_project_to_network(stage_dir, net_dir))
                mock_rm.assert_not_called()
            self.assertTrue(os.path.isdir(stage_dir))

            # Also verify _is_same_physical_dir catches realpath symlink aliases without global samefile mock
            from autometashape.cluster.staging import _is_same_physical_dir

            with patch(
                "autometashape.cluster.staging.os.path.realpath",
                side_effect=lambda p: stage_dir if p == net_dir else p,
            ):
                self.assertTrue(_is_same_physical_dir(stage_dir, net_dir))

            # 2. Pre-check rejects insufficient free disk space BEFORE writing net_dir (returns False, keeps stage)
            os.makedirs(net_dir, exist_ok=True)
            existing_obj = os.path.join(net_dir, "S.obj")
            with open(existing_obj, "wb") as fh:
                fh.write(b"GOOD_PREVIOUS_OBJ")
            with patch("autometashape.cluster.staging.shutil.disk_usage", return_value=SimpleNamespace(free=100)):
                self.assertFalse(sync_staged_project_to_network(stage_dir, net_dir))
            self.assertTrue(os.path.isdir(stage_dir))
            with open(existing_obj, "rb") as fh:
                self.assertEqual(fh.read(), b"GOOD_PREVIOUS_OBJ")

            # 3. Win32 Error 112 on copy2 -> .sync_tmp must NEVER overwrite existing valid deliverable
            def fail_copy2_112(src, dst, **kwargs):
                err = OSError("There is not enough space on the disk")
                err.winerror = 112
                err.errno = errno.ENOSPC
                raise err

            with patch("autometashape.cluster.staging.shutil.copy2", side_effect=fail_copy2_112):
                self.assertFalse(sync_staged_project_to_network(stage_dir, net_dir))
            self.assertTrue(os.path.isdir(stage_dir))
            with open(existing_obj, "rb") as fh:
                self.assertEqual(fh.read(), b"GOOD_PREVIOUS_OBJ")

            # 4. Partial fallback copytree failure after moved_old must purge partial d_path and restore old_d
            dest_files = os.path.join(net_dir, "S.files", "0")
            os.makedirs(dest_files, exist_ok=True)
            with open(os.path.join(dest_files, "good.zip"), "wb") as fh:
                fh.write(b"ORIGINAL_GOOD")

            real_copytree = __import__("shutil").copytree
            real_rename = os.rename
            fallback_hits = [0]

            def flaky_copytree(src, dst, *args, **kwargs):
                if ".sync_new_" in dst:
                    return real_copytree(src, dst, *args, **kwargs)
                fallback_hits[0] += 1
                os.makedirs(dst, exist_ok=True)
                with open(os.path.join(dst, "partial.bad"), "wb") as pf:
                    pf.write(b"PARTIAL")
                raise OSError(errno.EIO, "SMB drop mid-copy")

            def fail_second_rename(src, dst):
                if ".sync_new_" in src:
                    raise OSError("SMB lock on swap")
                return real_rename(src, dst)

            with (
                patch("autometashape.cluster.staging.shutil.copytree", side_effect=flaky_copytree),
                patch("autometashape.cluster.staging.os.rename", side_effect=fail_second_rename),
            ):
                self.assertFalse(sync_staged_project_to_network(stage_dir, net_dir))
            self.assertEqual(fallback_hits[0], 1)
            self.assertTrue(os.path.isdir(stage_dir))
            self.assertTrue(os.path.isfile(os.path.join(dest_files, "good.zip")))
            self.assertFalse(os.path.exists(os.path.join(net_dir, "S.files", "partial.bad")))

    def test_reconcile_staged_psx_chunk_id_restores_dst_backup_on_failure(self) -> None:
        """If ZipFile creation fails after directory swap, dst_dir must be restored from backup."""
        from tests.test_local_scratch_staging import TestLocalScratchStagingSystem

        with tempfile.TemporaryDirectory() as tmp:
            stage = os.path.join(tmp, "stage")
            TestLocalScratchStagingSystem._write_mock_psx(stage, "SiteR", ["2"])
            stale_zero = os.path.join(stage, "SiteR.files", "0")
            os.makedirs(stale_zero, exist_ok=True)
            with open(os.path.join(stale_zero, "keep.marker"), "w", encoding="utf-8") as fh:
                fh.write("preserve")

            with patch("autometashape.cluster.staging.os.replace", side_effect=OSError("disk error")):
                ok, old_id, new_id = reconcile_staged_psx_chunk_id(stage, "", "SiteR", target_chunk_id="0")
            self.assertFalse(ok)
            self.assertEqual((old_id, new_id), ("2", "0"))
            self.assertTrue(os.path.isfile(os.path.join(stage, "SiteR.files", "2", "chunk.zip")))
            self.assertTrue(os.path.isfile(os.path.join(stale_zero, "keep.marker")))

    def test_discover_image_subfolder_groups_allows_ancestor_or_equal_output_dir(self) -> None:
        """Ancestor or equal output_dir must never exclude image subfolders; proper child output_dir still excludes."""
        with tempfile.TemporaryDirectory() as parent_dir:
            imgs_dir = os.path.join(parent_dir, "Site_Photos")
            nadir_dir = os.path.join(imgs_dir, "Nadir")
            ob_dir = os.path.join(imgs_dir, "OB")
            child_out = os.path.join(imgs_dir, "Exports")
            for d in (nadir_dir, ob_dir, child_out):
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "IMG_001.JPG"), "wb") as fh:
                    fh.write(b"\xff\xd8\xff" + d.encode("utf-8"))

            g_eq = discover_image_subfolder_groups(imgs_dir, output_dir=imgs_dir)
            self.assertEqual(sorted(g_eq.keys()), ["Exports", "Nadir", "OB"])

            g_anc = discover_image_subfolder_groups(imgs_dir, output_dir=parent_dir)
            self.assertEqual(sorted(g_anc.keys()), ["Exports", "Nadir", "OB"])

            g_child = discover_image_subfolder_groups(imgs_dir, output_dir=child_out)
            self.assertEqual(sorted(g_child.keys()), ["Nadir", "OB"])

    def test_extract_path_leaf_name_handles_unc_and_drive_roots(self) -> None:
        """UNC share roots and drive roots must never collapse to empty project names."""
        self.assertEqual(extract_path_leaf_name(r"\\SRV\SiteShare"), "SiteShare")
        self.assertEqual(extract_path_leaf_name("\\\\?\\UNC\\SRV\\DataShare\\"), "DataShare")
        self.assertEqual(extract_path_leaf_name("D:\\"), "Drive_D")
        self.assertEqual(extract_path_leaf_name(r"\\SRV\Data\Site_01"), "Site_01")
        self.assertEqual(extract_path_leaf_name("/"), "Project")

        mapped = map_source_to_result_dir(r"\\SRV\SiteShare", r"\\SRV\SiteShare", r"\\SRV\ResultShare")
        self.assertTrue(mapped.endswith("SiteShare"))
        job = QueueJob(images_dir=r"\\SRV\SiteShare")
        self.assertTrue(bool(job.project_name))

    def test_export_project_assets_is_non_destructive_and_skips_unrequested_bake(self) -> None:
        """Export uses replace_asset=False, skips saving by default, and avoids unrequested 8K bake."""
        mock_doc = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.model = SimpleNamespace(vertices=[1, 2, 3], faces=[1, 2], textures=[], meta={})
        mock_chunk.elevation = None
        mock_chunk.orthomosaic = None
        mock_doc.chunk = mock_chunk

        fake_ms = SimpleNamespace(
            Document=lambda: mock_doc,
            app=SimpleNamespace(enumGPUDevices=lambda: []),
            ModelFormat=SimpleNamespace(ModelFormatGLTF=1, ModelFormatOBJ=2, ModelFormatFBX=3),
            ImageFormat=SimpleNamespace(ImageFormatPNG=1),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            psx = os.path.join(tmpdir, "Site.psx")
            files_dir = os.path.join(tmpdir, "Site.files")
            os.makedirs(files_dir, exist_ok=True)
            with open(psx, "w", encoding="utf-8") as fh:
                fh.write("<document/>")
            lock_path = os.path.join(files_dir, "lock")
            with open(lock_path, "w", encoding="utf-8") as fh:
                fh.write("active")

            with (
                patch.object(export_mod, "Metashape", fake_ms),
                patch("os.path.getsize", return_value=2048),
            ):
                export_mod.export_project_assets(
                    project_path=psx,
                    output_dir=tmpdir,
                    smooth_mesh=True,
                    decimate_mesh=500000,
                    export_glb=True,
                    export_obj=True,
                )

            # Lock file left untouched; doc.save NOT called; non-destructive replace_asset=False
            self.assertTrue(os.path.isfile(lock_path))
            mock_doc.save.assert_not_called()
            self.assertEqual(mock_chunk.smoothModel.call_args.kwargs.get("replace_asset"), False)
            self.assertEqual(mock_chunk.decimateModel.call_args.kwargs.get("replace_asset"), False)
            mock_chunk.buildUV.assert_not_called()
            mock_chunk.buildTexture.assert_not_called()
            for call in mock_chunk.exportModel.call_args_list:
                self.assertEqual(call.kwargs.get("save_texture"), False)

    def test_ipc_exact_sentinel_and_cp1252_safe_print(self) -> None:
        """Substring 'COMPLETE'/'PAUSE' tokens must not spoof completion, and cp1252 stdout must not crash."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sidecar = os.path.join(tmpdir, "_autometashape_status.json")
            ipc_mod.set_active_status_file(sidecar)
            try:
                ipc_mod.emit_ipc_stage("mesh", "Building Cầu Nhật Tân 3D Mesh")
                ipc_mod.emit_ipc_event("IPC_FAIL_INCOMPLETE_BUILD")
                ipc_mod.emit_ipc_event("COMPLETE_TOWER")
                ipc_mod.emit_ipc_event("PAUSE_RETRY")
                with open(sidecar, "r", encoding="utf-8") as fh:
                    st_mid = json.load(fh)
                self.assertEqual(st_mid["status"], "running")

                ipc_mod.emit_ipc_event(ipc_mod.IPC_PAUSE_CROP)
                with open(sidecar, "r", encoding="utf-8") as fh:
                    st_pause = json.load(fh)
                self.assertEqual(st_pause["status"], "paused")

                ipc_mod.emit_ipc_event(ipc_mod.IPC_COMPLETE)
                with open(sidecar, "r", encoding="utf-8") as fh:
                    st_done = json.load(fh)
                self.assertEqual(st_done["status"], "completed")
                self.assertEqual(st_done["progress"], 100.0)

                class _StrictCp1252Stream:
                    encoding = "cp1252"

                    def __init__(self) -> None:
                        self.chunks: list[str] = []

                    def write(self, s: str) -> int:
                        s.encode("cp1252", errors="strict")
                        self.chunks.append(s)
                        return len(s)

                    def flush(self) -> None:
                        pass

                strict_out = _StrictCp1252Stream()
                with patch("sys.stdout", strict_out):
                    ipc_mod.emit_ipc_stage("ingest", "Đang nạp ảnh Cổng Chính 🚀")
                self.assertTrue(any("C?ng Ch" in c or "ingest" in c for c in strict_out.chunks))
            finally:
                ipc_mod.set_active_status_file(None)


if __name__ == "__main__":
    unittest.main()
