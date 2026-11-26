# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests defending all P0/P1/P2 deep audit hardening fixes."""

from __future__ import annotations

import io
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
for _p in (_SOURCE_DIR, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import cluster_abort as cluster_abort_mod  # noqa: E402
import cluster_monitor as monitor_mod  # noqa: E402
import launch_worker as launch_worker_mod  # noqa: E402
import submit_cluster_batch as submit_mod  # noqa: E402
from autometashape.cluster.abort import check_abort_signal  # noqa: E402
from autometashape.cluster.dual_share import discover_dual_share_projects  # noqa: E402
from autometashape.cluster.hardware import (  # noqa: E402
    compute_discrete_gpu_mask,
    is_healthy_online_worker,
    is_integrated_gpu,
    probe_metashape_gpu_mask,
)
from autometashape.cluster.orchestrator import (  # noqa: E402
    CLUSTER_TICKETS_REL_DIR,
    cleanup_stale_dispatch_tickets,
    create_hidden_dispatch_ticket,
    safe_create_cluster_batch,
)
from autometashape.cluster.supervisor import check_storage_headroom  # noqa: E402
from autometashape.cluster.updater import generate_worker_updater_code, update_remote_worker  # noqa: E402
from autometashape.core import filtering as filtering_mod  # noqa: E402
from autometashape.core import pipeline as pipeline_mod  # noqa: E402
from autometashape.core.alignment_hygiene import (  # noqa: E402
    build_tie_point_islands,
    sanitize_camera_gps,
)
from autometashape.core.pipeline import resolve_checkpoint_chunk  # noqa: E402
from autometashape.core.telemetry import CAMERA_HARDWARE_DB  # noqa: E402
from autometashape.core.verification import (  # noqa: E402
    verify_glb_model,
    verify_obj_model,
    verify_project_deliverables,
)


class _FakeVec:
    def __init__(self, coords):
        self.x, self.y, self.z = float(coords[0]), float(coords[1]), float(coords[2])


class _FakeMat:
    def __init__(self, rows):
        self.rows = rows

    def t(self):
        return _FakeMat([[self.rows[j][i] for j in range(3)] for i in range(3)])

    def __mul__(self, other):
        if isinstance(other, _FakeVec):
            r = self.rows
            return _FakeVec(
                [
                    r[0][0] * other.x + r[0][1] * other.y + r[0][2] * other.z,
                    r[1][0] * other.x + r[1][1] * other.y + r[1][2] * other.z,
                    r[2][0] * other.x + r[2][1] * other.y + r[2][2] * other.z,
                ]
            )
        r, o = self.rows, other.rows
        return _FakeMat([[sum(r[i][k] * o[k][j] for k in range(3)) for j in range(3)] for i in range(3)])


class TestDynamicGpuBitmask(unittest.TestCase):
    """Defend dynamic Metashape-indexed GPU bitmask computation and worker self-healing."""

    def test_compute_discrete_gpu_mask_across_hardware_topologies(self) -> None:
        # Topology 1: Intel iGPU (idx 0) + RTX 5080 (idx 1) -> bitmask 2
        mask, selected = compute_discrete_gpu_mask(
            [
                {"name": "Intel(R) Graphics", "mem_size": 0},
                {"name": "NVIDIA GeForce RTX 5080", "mem_size": 16 * (1024**3)},
            ]
        )
        self.assertEqual(mask, 2)
        self.assertEqual([d["index"] for d in selected], [1])

        # Topology 2: AMD Ryzen 780M iGPU (idx 0) + RTX 5070 (idx 1) -> bitmask 2
        mask_amd, _ = compute_discrete_gpu_mask(
            [
                {"name": "AMD Radeon(TM) 780M Graphics", "mem_size": 512 * (1024**2)},
                {"name": "NVIDIA GeForce RTX 5070", "mem_size": 12 * (1024**3)},
            ]
        )
        self.assertEqual(mask_amd, 2)

        # Topology 3: Discrete-only machine (BIOS iGPU off) -> bitmask 1
        mask_single, _ = compute_discrete_gpu_mask(
            [
                {"name": "NVIDIA GeForce RTX 5080", "mem_size": 16 * (1024**3)},
            ]
        )
        self.assertEqual(mask_single, 1)

        # Topology 4: Dual discrete RTX + Intel iGPU at idx 0 -> bitmask 6 (2 | 4)
        mask_dual, selected_dual = compute_discrete_gpu_mask(
            [
                {"name": "Intel(R) UHD Graphics 770", "mem_size": 0},
                {"name": "NVIDIA GeForce RTX 5080", "mem_size": 16 * (1024**3)},
                {"name": "NVIDIA GeForce RTX 5080", "mem_size": 16 * (1024**3)},
            ]
        )
        self.assertEqual(mask_dual, 6)
        self.assertEqual([d["index"] for d in selected_dual], [1, 2])

        # Topology 5: Core Ultra Arc 140V iGPU excluded, discrete Arc A770 kept
        self.assertTrue(is_integrated_gpu("Intel(R) Arc(TM) 140V Graphics"))
        self.assertTrue(is_integrated_gpu("Microsoft Basic Render Driver"))
        self.assertFalse(is_integrated_gpu("Intel(R) Arc(TM) A770 Graphics"))
        self.assertFalse(is_integrated_gpu("Intel(R) Arc(TM) B580 Graphics"))
        mask_arc, selected_arc = compute_discrete_gpu_mask(
            [
                {"name": "Intel(R) Arc(TM) A770 Graphics", "mem_size": 16 * (1024**3)},
                {"name": "Intel(R) Arc(TM) 140V Graphics", "mem_size": 0},
            ]
        )
        self.assertEqual(mask_arc, 1)
        self.assertEqual([d["name"] for d in selected_arc], ["Intel(R) Arc(TM) A770 Graphics"])

    def test_probe_metashape_gpu_mask_parses_probe_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_exe = os.path.join(tmpdir, "metashape.exe")
            with open(fake_exe, "w", encoding="utf-8") as f:
                f.write("stub")

            payload = json.dumps(
                [
                    {"name": "Intel(R) Graphics", "mem_size": 0},
                    {"name": "NVIDIA GeForce RTX 5080", "mem_size": 16 * (1024**3)},
                ]
            )
            fake_proc = SimpleNamespace(
                returncode=0,
                stdout=f"Agisoft Metashape Professional\n===AM_GPUS_START==={payload}===AM_GPUS_END===\n",
                stderr="",
            )
            with patch("subprocess.run", return_value=fake_proc):
                mask, devs = probe_metashape_gpu_mask(fake_exe)
            self.assertEqual(mask, 2)
            self.assertEqual(len(devs), 1)
            self.assertEqual(devs[0]["name"], "NVIDIA GeForce RTX 5080")

    def test_launch_worker_passes_probed_dynamic_gpu_mask_and_respects_explicit_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_auto = os.path.join(tmpdir, "worker_auto.json")
            with (
                patch("cluster_utils.find_metashape_path", return_value="dummy_metashape.exe"),
                patch(
                    "cluster_utils.probe_metashape_gpu_mask",
                    return_value=(2, [{"index": 1, "name": "NVIDIA GeForce RTX 5080", "mem_mb": 16384}]),
                ),
                patch("os.path.isfile", return_value=True),
                patch("cluster_utils.test_tcp_connection", return_value=True),
                patch("cluster_utils.test_unc_path", return_value=(True, "")),
                patch("subprocess.Popen") as mock_popen,
            ):
                mock_proc = mock_popen.return_value
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_proc.poll.return_value = 0

                with patch(
                    "sys.argv",
                    [
                        "launch_worker.py",
                        "--headless",
                        "--no-reconnect",
                        "--host",
                        "10.5.10.202",
                        "--root",
                        r"\\10.5.10.202\Result",
                        "--config",
                        cfg_auto,
                    ],
                ):
                    ret = launch_worker_mod._run_worker_lifecycle()
                    self.assertEqual(ret, 0)
                    cmd = mock_popen.call_args[0][0]
                    env = mock_popen.call_args[1]["env"]
                    self.assertIn("--gpu-mask", cmd)
                    self.assertEqual(cmd[cmd.index("--gpu-mask") + 1], "2")
                    self.assertNotIn("AUTOMETASHAPE_EXPLICIT_GPU_MASK", env)

            # Explicit --gpu-mask 1 must persist gpu_mask_explicit=True and survive subsequent restarts
            cfg_explicit = os.path.join(tmpdir, "worker_explicit.json")
            with (
                patch("cluster_utils.find_metashape_path", return_value="dummy_metashape.exe"),
                patch("cluster_utils.probe_metashape_gpu_mask", return_value=(2, [])),
                patch("os.path.isfile", return_value=True),
                patch("cluster_utils.test_tcp_connection", return_value=True),
                patch("cluster_utils.test_unc_path", return_value=(True, "")),
                patch("subprocess.Popen") as mock_popen,
            ):
                mock_proc = mock_popen.return_value
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_proc.poll.return_value = 0

                with patch(
                    "sys.argv",
                    [
                        "launch_worker.py",
                        "--headless",
                        "--no-reconnect",
                        "--host",
                        "10.5.10.202",
                        "--root",
                        r"\\10.5.10.202\Result",
                        "--gpu-mask",
                        "1",
                        "--config",
                        cfg_explicit,
                    ],
                ):
                    self.assertEqual(launch_worker_mod._run_worker_lifecycle(), 0)
                    env = mock_popen.call_args[1]["env"]
                    self.assertEqual(env.get("AUTOMETASHAPE_EXPLICIT_GPU_MASK"), "1")

            with open(cfg_explicit, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved.get("gpu_mask"), "1")
            self.assertTrue(saved.get("gpu_mask_explicit"))

    def test_pipeline_worker_self_heals_poisoned_zero_gpu_mask(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            img_dir = os.path.join(tmpdir, "images")
            out_dir = os.path.join(tmpdir, "out")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(img_dir, "IMG_0001.JPG"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")

            fake_app = SimpleNamespace(
                gpu_mask=0,
                enumGPUDevices=lambda: [
                    {"name": "Intel(R) Graphics", "mem_size": 0},
                    {"name": "NVIDIA GeForce RTX 5080", "mem_size": 16 * (1024**3)},
                ],
                settings=SimpleNamespace(network_enable=False),
            )
            fake_doc = MagicMock()
            fake_chunk = MagicMock()
            fake_chunk.cameras = []
            fake_chunk.model = None
            fake_chunk.point_cloud = None
            fake_chunk.elevation = None
            fake_chunk.orthomosaic = None
            fake_doc.chunk = fake_chunk
            fake_doc.chunks = [fake_chunk]
            fake_doc.addChunk.return_value = fake_chunk
            fake_ms = SimpleNamespace(
                app=fake_app,
                Document=lambda: fake_doc,
                ReferencePreselectionMode=SimpleNamespace(ReferencePreselectionEstimated=0),
            )

            with (
                patch.object(pipeline_mod, "Metashape", fake_ms),
                patch.dict(os.environ, {}, clear=False),
            ):
                os.environ.pop("AUTOMETASHAPE_EXPLICIT_GPU_MASK", None)
                pipeline_mod.run_reconstruction(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="HealTest",
                    network_enable=False,
                    network_path=tmpdir,
                    local_scratch_staging=False,
                    align_photos=False,
                    build_depth_maps=False,
                    build_mesh=False,
                    build_texture=False,
                    build_dem=False,
                    build_orthomosaic=False,
                    build_point_cloud=False,
                    export_obj=False,
                    export_glb=False,
                )
            self.assertEqual(fake_app.gpu_mask, 2)

            # With AUTOMETASHAPE_EXPLICIT_GPU_MASK="0", operator CPU-only override MUST be honored
            fake_app.gpu_mask = 0
            with (
                patch.object(pipeline_mod, "Metashape", fake_ms),
                patch.dict(os.environ, {"AUTOMETASHAPE_EXPLICIT_GPU_MASK": "0"}),
            ):
                pipeline_mod.run_reconstruction(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="HonorZero",
                    network_enable=False,
                    network_path=tmpdir,
                    local_scratch_staging=False,
                    align_photos=False,
                    build_depth_maps=False,
                    build_mesh=False,
                    build_texture=False,
                    build_dem=False,
                    build_orthomosaic=False,
                    build_point_cloud=False,
                    export_obj=False,
                    export_glb=False,
                )
            self.assertEqual(fake_app.gpu_mask, 0)


class TestHiddenDispatchTicketsAndMonitor(unittest.TestCase):
    """Defend zero _dispatch_*.psx pollution in Result/ and accurate ticket/legacy monitor mapping."""

    def test_safe_create_falls_back_to_hidden_ticket_and_sets_priority_before_unpause(self) -> None:
        with tempfile.TemporaryDirectory() as shared_root:
            res_dir = os.path.join(shared_root, "Result", "CATP_0209")
            os.makedirs(res_dir, exist_ok=True)
            call_order: list[str] = []

            mock_net = MagicMock()
            mock_net.findBatch.return_value = -1
            mock_net.batchList.return_value = {
                "batches": [{"batch_id": 9, "path": "Result/CATP_0209/CATP_0209.psx", "state": "inprogress"}]
            }
            # Persistent Duplicate batch on canonical path, succeeds on hidden ticket path
            mock_net.createBatch.side_effect = [
                RuntimeError("Duplicate batch: Result/CATP_0209/CATP_0209.psx"),
                RuntimeError("Duplicate batch: Result/CATP_0209/CATP_0209.psx"),
                42,
            ]
            mock_net.setBatchPriority.side_effect = lambda bid, p: call_order.append(f"prio:{bid}:{p}")
            mock_net.setBatchWorkerLimit.side_effect = lambda bid, lim: call_order.append(f"limit:{bid}:{lim}")
            mock_net.setBatchPaused.side_effect = lambda bid, p: call_order.append(f"paused:{bid}:{p}")

            with patch("autometashape.cluster.orchestrator.time.sleep"):
                bid, attached = safe_create_cluster_batch(
                    mock_net,
                    "Result/CATP_0209/CATP_0209.psx",
                    [MagicMock()],
                    worker_limit=1,
                    force_fresh=True,
                    priority=100,
                    start_paused=False,
                    shared_root=shared_root,
                    ticket_project_name="CATP_0209",
                    ticket_rel_output_dir="Result/CATP_0209",
                )

            self.assertEqual(bid, 42)
            self.assertFalse(attached)
            # Final createBatch used hidden .cluster/tickets/ path
            final_rel = mock_net.createBatch.call_args_list[-1][0][0]
            self.assertTrue(final_rel.startswith(f"{CLUSTER_TICKETS_REL_DIR}/"))
            # Result/CATP_0209 has ZERO _dispatch_*.psx files!
            self.assertEqual([f for f in os.listdir(res_dir) if "_dispatch" in f.lower()], [])
            # Priority and worker limit MUST be configured BEFORE unpausing
            self.assertEqual(call_order, ["prio:42:100", "limit:42:1", "paused:42:False"])

    def test_monitor_resolves_hidden_tickets_and_legacy_dispatch_and_filters_aborted(self) -> None:
        with tempfile.TemporaryDirectory() as shared_root:
            proj_dir = os.path.join(shared_root, "Result", "SiteT")
            os.makedirs(proj_dir, exist_ok=True)
            with open(os.path.join(proj_dir, "SiteT.obj"), "w", encoding="utf-8") as f:
                f.write("mtllib SiteT.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n" + ("v 0 0 0\n" * 200))
            with open(os.path.join(proj_dir, "SiteT.mtl"), "w", encoding="utf-8") as f:
                f.write("newmtl m0\nmap_Kd SiteT.png\n")
            with open(os.path.join(proj_dir, "SiteT.png"), "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 512)
            with open(os.path.join(proj_dir, "SiteT_orthomosaic.tif"), "wb") as f:
                f.write(b"II*\x00" + b"\x00" * 1500)
            with open(os.path.join(proj_dir, "SiteT_orthomosaic.prj"), "w", encoding="utf-8") as f:
                f.write('PROJCS["WGS 84 / UTM zone 48N"]')
            with open(os.path.join(proj_dir, "SiteT_orthomosaic.tfw"), "w", encoding="utf-8") as f:
                f.write("0.05\n0\n0\n-0.05\n0\n0\n")

            ticket_rel = create_hidden_dispatch_ticket(shared_root, "SiteT", "Result/SiteT")
            t_name, t_dir = monitor_mod._resolve_batch_project_target(ticket_rel, [shared_root])
            self.assertEqual(os.path.normpath(t_dir), os.path.normpath(proj_dir))
            self.assertEqual(t_name, "SiteT")

            leg_name, leg_dir = monitor_mod._resolve_batch_project_target(
                "Result/SiteT/_dispatch_SiteT_1700000000.psx", [shared_root]
            )
            self.assertEqual(os.path.normpath(leg_dir), os.path.normpath(proj_dir))
            self.assertEqual(leg_name, "SiteT")

            fake_client = MagicMock()
            fake_client.workerList.return_value = {
                "workers": [
                    {"worker_id": 1, "hostname": "NodeA", "state": "ready", "gpu_mask": 2},
                    {"worker_id": 2, "hostname": "GhostNode", "state": "ready", "aborted": True},
                ]
            }
            fake_client.batchList.return_value = {
                "batches": [
                    {"batch_id": 10, "path": ticket_rel, "state": "completed"},
                    {"batch_id": 11, "path": "Result/SiteT/SiteT.psx", "state": "inprogress", "aborted": True},
                    {"batch_id": 12, "path": ".cluster/maintenance_nodea_1.psx", "state": "failed"},
                ]
            }
            fake_ms = SimpleNamespace(NetworkClient=lambda: fake_client)

            with (
                patch.object(monitor_mod, "Metashape", fake_ms),
                redirect_stdout(io.StringIO()),
            ):
                audit = monitor_mod.execute_audit(
                    server_host="127.0.0.1:5840",
                    shared_root=shared_root,
                    min_headroom=0.01,
                )

            self.assertEqual(len(audit["workers"]), 1)
            self.assertEqual(audit["workers"][0]["hostname"], "NodeA")
            self.assertIn(10, audit["deliverables"])
            self.assertTrue(audit["deliverables"][10]["intact"])
            self.assertNotIn(11, audit["deliverables"])
            self.assertEqual(audit["running_batches"], [])
            self.assertEqual(audit["errored_count"], 0)
            self.assertTrue(audit["healthy"], msg=f"Unexpected monitor issues: {audit['issues']}")

            # Cleanup stale tickets only deletes expired tickets
            tickets_dir = os.path.join(shared_root, ".cluster", "tickets")
            old_ts = time.time() - (20 * 86400)
            for fn in os.listdir(tickets_dir):
                os.utime(os.path.join(tickets_dir, fn), (old_ts, old_ts))
            removed = cleanup_stale_dispatch_tickets(shared_root, max_age_hours=24.0)
            self.assertGreaterEqual(removed, 2)

    def test_submit_cluster_batch_never_creates_dispatch_psx_in_result_dir(self) -> None:
        with tempfile.TemporaryDirectory() as shared_root:
            img_dir = os.path.join(shared_root, "Data", "SiteClean")
            out_dir = os.path.join(shared_root, "Result", "SiteClean")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(img_dir, "IMG_0001.JPG"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")

            fake_client = MagicMock()
            fake_client.findBatch.return_value = -1
            fake_client.batchList.return_value = {"batches": []}
            fake_client.createBatch.return_value = 77
            fake_doc = MagicMock()

            def _fake_save(p):
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write("<document/>")

            fake_doc.save.side_effect = _fake_save
            fake_ms = SimpleNamespace(
                NetworkClient=lambda: fake_client,
                Document=lambda: fake_doc,
                Tasks=SimpleNamespace(RunScript=lambda: MagicMock(toNetworkTask=lambda: MagicMock())),
            )

            with (
                patch.object(submit_mod, "Metashape", fake_ms),
                redirect_stdout(io.StringIO()),
            ):
                bid = submit_mod.submit_batch(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="SiteClean",
                    server_host="127.0.0.1:5840",
                    shared_root=shared_root,
                )

            self.assertEqual(bid, 77)
            files = os.listdir(out_dir)
            self.assertIn("SiteClean.psx", files)
            self.assertEqual([f for f in files if "_dispatch" in f.lower()], [])


class TestCoreAndClusterSafetyHardening(unittest.TestCase):
    """Defend crop Z preservation, hygiene CRS/tracks, verification guards, abort & updater safety."""

    def test_auto_crop_preserves_ground_z_when_tie_points_empty(self) -> None:
        identity = _FakeMat([[1, 0, 0], [0, 1, 0], [0, 0, 1]])
        fake_ms = SimpleNamespace(Vector=_FakeVec, Matrix=_FakeMat)
        cams = [
            SimpleNamespace(
                transform=True, enabled=True, center=_FakeVec([x, y, 80.0]), reference=SimpleNamespace(location=None)
            )
            for x, y in [(0, 0), (100, 0), (100, 100), (0, 100)]
        ]
        # Ground & building occupy Z=[0, 20] (center.z=10, size.z=20), drone flies at Z=80
        chunk = SimpleNamespace(
            cameras=cams,
            tie_points=None,
            transform=SimpleNamespace(scale=1.0),
            region=SimpleNamespace(rot=identity, center=_FakeVec([50, 50, 10]), size=_FakeVec([120, 120, 20])),
        )
        with patch.object(filtering_mod, "Metashape", fake_ms), redirect_stdout(io.StringIO()):
            ok = filtering_mod.auto_crop_camera_gps_square(chunk, padding=0.05)

        self.assertTrue(ok)
        z_min = chunk.region.center.z - 0.5 * chunk.region.size.z
        z_max = chunk.region.center.z + 0.5 * chunk.region.size.z
        self.assertLessEqual(z_min, 0.0)
        self.assertGreaterEqual(z_max, 80.0)

    def test_resolve_checkpoint_chunk_falls_back_when_canonical_is_empty(self) -> None:
        empty_canon = SimpleNamespace(label="Production_Reconstruction", cameras=[])
        aligned_real = SimpleNamespace(
            label="Chunk 1",
            cameras=[SimpleNamespace(transform=True, enabled=True) for _ in range(5)],
        )
        doc = SimpleNamespace(chunks=[empty_canon, aligned_real], chunk=empty_canon)
        ch, count, can_resume = resolve_checkpoint_chunk(doc)
        self.assertIs(ch, aligned_real)
        self.assertEqual(count, 5)
        self.assertTrue(can_resume)

    def test_alignment_hygiene_metric_crs_and_long_track_preservation(self) -> None:
        # Projected UTM coordinates in meters (300m apart) must NOT be multiplied by 111,320!
        cams = []
        for idx, (e, n, z) in enumerate([(500000.0, 1180000.0, 50.0), (500300.0, 1180000.0, 50.0)] * 3):
            ref = SimpleNamespace(location=SimpleNamespace(x=e, y=n, z=z), enabled=True)
            cams.append(SimpleNamespace(label=f"C{idx}", enabled=True, reference=ref))
        chunk = SimpleNamespace(
            crs=SimpleNamespace(wkt='PROJCS["WGS 84 / UTM zone 48N"]', name="WGS 84 / UTM zone 48N"),
            cameras=cams,
        )
        with redirect_stdout(io.StringIO()):
            stats = sanitize_camera_gps(chunk)
        self.assertEqual(stats["outlier_disabled"], 0)
        self.assertEqual(stats["valid_remaining"], 6)

        # Long track (>30 cameras) must stay connected instead of being discarded, and invalid points skipped
        class _HashCam:
            def __init__(self, key: int) -> None:
                self.key = key
                self.transform = True
                self.enabled = True

        all_cams = [_HashCam(i) for i in range(40)]
        # Add 16 valid shared tracks across all 40 cameras to exceed min_shared_tracks=15
        tp_points = [SimpleNamespace(track_id=tid, valid=True) for tid in range(16)]
        tp_points.append(SimpleNamespace(track_id=99, valid=False))
        proj_map = {c: [SimpleNamespace(track_id=tid) for tid in list(range(16)) + [99]] for c in all_cams}
        tp = SimpleNamespace(points=tp_points, projections=proj_map)
        islands = build_tie_point_islands(
            SimpleNamespace(cameras=all_cams, tie_points=tp), edge_min=15, max_track_len=20
        )
        self.assertEqual(len(islands), 1)
        self.assertEqual(len(islands[0]), 40)

    def test_verification_guards_and_telemetry_hasselblad_l1d(self) -> None:
        self.assertIn("l1d-20c", CAMERA_HARDWARE_DB)
        self.assertIn("Mavic 2 Pro", CAMERA_HARDWARE_DB["l1d-20c"]["name"])
        self.assertAlmostEqual(CAMERA_HARDWARE_DB["l1d-20c"]["sensor_w_mm"], 13.2)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Truncated GLB (header claims 10000 bytes, actual file is 2048 bytes) MUST fail
            glb_p = os.path.join(tmpdir, "SiteV.glb")
            with open(glb_p, "wb") as f:
                f.write(b"glTF" + struct.pack("<II", 2, 10000) + b"\x00" * 2036)
            ok_glb, issues_glb = verify_glb_model(glb_p)
            self.assertFalse(ok_glb)
            self.assertTrue(
                any("truncat" in m.lower() or "size" in m.lower() or "length" in m.lower() for m in issues_glb)
            )

            # OBJ with vertices at start and faces at tail (>128 KB gap) passes via head+tail seek
            obj_p = os.path.join(tmpdir, "SiteV.obj")
            mtl_p = os.path.join(tmpdir, "SiteV.mtl")
            tex_tif = os.path.join(tmpdir, "SiteV_u1_v1.tif")
            tp_ply = os.path.join(tmpdir, "SiteV_tie_points.ply")
            with open(obj_p, "wb") as f:
                f.write(b"mtllib SiteV.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\n")
                f.write(b"# padding\n" * 16000)
                f.write(b"f 1 2 3\n")
            with open(mtl_p, "w", encoding="utf-8") as f:
                f.write("newmtl m0\nmap_Kd SiteV_u1_v1.tif\n")
            with open(tex_tif, "wb") as f:
                f.write(b"II*\x00" + b"\x00" * 2048)
            with open(tp_ply, "wb") as f:
                f.write(b"ply\nformat binary_little_endian 1.0\nend_header\n" + b"\x00" * 512)

            ok_obj, _ = verify_obj_model(obj_p)
            self.assertTrue(ok_obj)

            # MTL texture .tif must NOT satisfy DEM or Ortho; _tie_points.ply must NOT satisfy point_cloud
            res = verify_project_deliverables(tmpdir, expected_assets=["obj", "dem", "ortho", "point_cloud"])
            self.assertEqual(res["status"], "FAIL")
            self.assertEqual(res["assets_checked"]["obj"]["status"], "PASS")
            self.assertNotIn("dem", res["assets_checked"])
            self.assertNotIn("ortho", res["assets_checked"])
            self.assertNotIn("point_cloud", res["assets_checked"])

    def test_updater_unc_repr_and_operator_paused_batch_preservation(self) -> None:
        script = generate_worker_updater_code(
            expected_hostname="Worker-A",
            shared_cluster_dir=r"\\10.5.10.202\Result\.cluster",
        )
        # Must compile as valid Python and evaluate shared_dir to the exact UNC path
        ns: dict = {}
        for line in script.splitlines():
            if line.startswith("shared_dir = "):
                exec(line, ns)
        self.assertEqual(ns["shared_dir"], r"\\10.5.10.202\Result\.cluster")

        # Operator-paused batch #10 must NOT be paused/unpaused by update_remote_worker
        mock_client = MagicMock()
        mock_client.workerList.side_effect = [
            {"workers": [{"worker_id": 1, "hostname": "W1", "state": "inprogress", "batch_id": 20}]},
            {"workers": [{"worker_id": 1, "hostname": "W1", "state": "ready"}]},
            {"workers": [{"worker_id": 1, "hostname": "W1", "state": "ready"}]},
            {"workers": [{"worker_id": 2, "hostname": "W1", "state": "ready"}]},
        ]
        mock_client.batchList.return_value = {
            "batches": [
                {"batch_id": 10, "state": "inprogress", "paused": True},
                {"batch_id": 20, "state": "inprogress", "paused": False},
                {"batch_id": 30, "state": "inprogress", "paused": False},
                {"batch_id": 99, "state": "completed"},
            ]
        }
        mock_client.createBatch.return_value = 99
        mock_ms = SimpleNamespace(Tasks=SimpleNamespace(RunScript=lambda: MagicMock(toNetworkTask=lambda: MagicMock())))

        with tempfile.TemporaryDirectory() as tmpdir, patch("autometashape.cluster.updater.Metashape", mock_ms):
            res = update_remote_worker(
                client=mock_client,
                worker_id_or_name=1,
                shared_root=tmpdir,
                timeout=5.0,
                poll_interval=0.01,
                force=True,
            )
        self.assertTrue(res["success"])
        paused_calls = [c.args for c in mock_client.setBatchPaused.call_args_list]
        self.assertNotIn((10, True), paused_calls)
        self.assertNotIn((10, False), paused_calls)
        self.assertIn((30, True), paused_calls)
        self.assertIn((30, False), paused_calls)

    def test_cluster_safety_abort_headroom_and_dual_share_model_folder(self) -> None:
        # 1. check_abort_signal rejects mismatched node_name in .abort_pending
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, ".abort_pending"), "w", encoding="utf-8") as f:
                json.dump({"node_name": "OTHER-NODE", "action": "autometashape_abort"}, f)
            self.assertIsNone(check_abort_signal(target_dir=tmpdir, node_name="MY-NODE"))

        # 2. cluster_abort refuses remote node abort when shared_root is missing (never touches live cluster)
        with (
            patch.object(cluster_abort_mod, "Metashape", MagicMock()),
            patch.object(cluster_abort_mod, "load_cluster_config", return_value={}),
            patch.dict(os.environ, {}, clear=False),
            patch("sys.argv", ["cluster_abort.py", "--node", "REMOTE-WORKER-XYZ", "--no-reexec"]),
            redirect_stdout(io.StringIO()),
        ):
            os.environ.pop("AUTOMETASHAPE_CLUSTER_ROOT", None)
            rc = cluster_abort_mod.main()
        self.assertEqual(rc, 1)

        # 3. check_storage_headroom fails CLOSED on disk_usage error
        with patch("shutil.disk_usage", side_effect=OSError("SMB disconnected")):
            ok, free_gb = check_storage_headroom(r"\\offline\share", min_headroom_gb=50.0)
        self.assertFalse(ok)
        self.assertEqual(free_gb, 0.0)

        # 4. is_healthy_online_worker rejects offline and aborted ghosts
        self.assertTrue(is_healthy_online_worker({"state": "ready", "aborted": False}))
        self.assertFalse(is_healthy_online_worker({"state": "offline", "aborted": False}))
        self.assertFalse(is_healthy_online_worker({"state": "ready", "aborted": True}))

        # 5. dual_share discovers legitimate "ModelHouse" while still skipping "output_old"
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as res_root:
            for d in ("ModelHouse", "output_old"):
                p = os.path.join(src_root, d)
                os.makedirs(p, exist_ok=True)
                with open(os.path.join(p, "DJI_0001.JPG"), "wb") as f:
                    f.write(b"\xff\xd8\xff\xd9")
            jobs = discover_dual_share_projects(src_root, res_root)
            self.assertEqual([j.project_name for j in jobs], ["ModelHouse"])


if __name__ == "__main__":
    unittest.main()
