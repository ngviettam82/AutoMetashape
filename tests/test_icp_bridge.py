# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Zero-Neural Open3D ICP Bridge, pure-Python Sim(3), Quality Gate, and Runtime Provisioner."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
source_dir = os.path.join(repo_root, "source")
if source_dir not in sys.path:
    sys.path.insert(0, source_dir)

from autometashape.core import icp_bridge as ib


def _euler_matrix(rx_deg: float, ry_deg: float, rz_deg: float) -> list[list[float]]:
    ax, ay, az = map(math.radians, (rx_deg, ry_deg, rz_deg))
    cx, sx = math.cos(ax), math.sin(ax)
    cy, sy = math.cos(ay), math.sin(ay)
    cz, sz = math.cos(az), math.sin(az)
    rx = [[1.0, 0.0, 0.0], [0.0, cx, -sx], [0.0, sx, cx]]
    ry = [[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]]
    rz = [[cz, -sz, 0.0], [sz, cz, 0.0], [0.0, 0.0, 1.0]]
    return ib._mat3_mul(rz, ib._mat3_mul(ry, rx))


class TestIcpBridge(unittest.TestCase):
    """Mutation-resistant tests for Stage-4 Zero-Neural ICP Bridge and offline runtime provisioner."""

    def test_fit_sim3_umeyama_3d_exact_recovery(self) -> None:
        """Verify pure-Python 3D Umeyama Sim(3) recovers exact scale, rotation, and translation."""
        src = [
            (0.0, 0.0, 0.0),
            (20.0, 0.0, 0.0),
            (0.0, 20.0, 0.0),
            (0.0, 0.0, 20.0),
            (-15.0, 15.0, -5.0),
            (15.0, -15.0, 10.0),
        ]
        true_scale = 0.9875
        true_rot = _euler_matrix(3.2, -2.1, 11.4)
        true_t = (12.5, -8.3, 4.1)

        dst = []
        for p in src:
            rp = ib._mat3_vec(true_rot, list(p))
            dst.append((true_scale * rp[0] + true_t[0], true_scale * rp[1] + true_t[1], true_scale * rp[2] + true_t[2]))

        t_mat, scale, max_err = ib.fit_sim3_umeyama_3d(src, dst, with_scale=True)
        self.assertAlmostEqual(scale, true_scale, places=9)
        self.assertLess(max_err, 1e-9)
        for i in range(3):
            self.assertAlmostEqual(t_mat[i][3], true_t[i], places=8)
            for j in range(3):
                self.assertAlmostEqual(t_mat[i][j], true_scale * true_rot[i][j], places=8)

    def test_convert_utm_transform_to_ecef_submillimeter_fit(self) -> None:
        """Verify shifted-UTM to ECEF Sim(3) probe conversion stays strictly sub-millimeter."""
        rot_world = _euler_matrix(35.0, -20.0, 62.0)
        earth_offset = (1234567.0, 5678901.0, 1180000.0)

        class _AffineCrs:
            def unproject(self, pt):
                v = ib._mat3_vec(rot_world, [float(pt[0]), float(pt[1]), float(pt[2])])
                return (v[0] + earth_offset[0], v[1] + earth_offset[1], v[2] + earth_offset[2])

        r_utm = _euler_matrix(0.3, -0.2, 1.15)
        s_utm = 0.997
        t_utm = [
            [s_utm * r_utm[0][0], s_utm * r_utm[0][1], s_utm * r_utm[0][2], 1.5],
            [s_utm * r_utm[1][0], s_utm * r_utm[1][1], s_utm * r_utm[1][2], -2.0],
            [s_utm * r_utm[2][0], s_utm * r_utm[2][1], s_utm * r_utm[2][2], 0.8],
            [0.0, 0.0, 0.0, 1.0],
        ]
        with patch.object(ib, "Metashape", None):
            t_ecef, err_mm = ib.convert_utm_transform_to_ecef(t_utm, _AffineCrs(), (675000.0, 1190000.0, 20.0))

        self.assertEqual(len(t_ecef), 4)
        self.assertLess(err_mm, 0.01)

    def test_evaluate_icp_quality_gate_pass_and_all_rejections(self) -> None:
        """Verify Pre-MVS Quality Gate passes clean ICP and rejects each individual threshold breach."""
        good = {
            "ok": True,
            "fitness_15cm": 0.70,
            "median_15cm_m": 0.045,
            "scale": 0.997,
            "tilt_deg": 0.4,
        }
        passed, reasons = ib.evaluate_icp_quality_gate(good, max_abs_k4=0.0)
        self.assertTrue(passed)
        self.assertEqual(reasons, [])

        # Low fitness
        ok_f, r_f = ib.evaluate_icp_quality_gate({**good, "fitness_15cm": 0.34})
        self.assertFalse(ok_f)
        self.assertTrue(any("fitness_15cm" in r for r in r_f))

        # High median error
        ok_m, r_m = ib.evaluate_icp_quality_gate({**good, "median_15cm_m": 0.11})
        self.assertFalse(ok_m)
        self.assertTrue(any("median_15cm" in r for r in r_m))

        # Scale out of bounds (both low & high)
        self.assertFalse(ib.evaluate_icp_quality_gate({**good, "scale": 0.89})[0])
        self.assertFalse(ib.evaluate_icp_quality_gate({**good, "scale": 1.11})[0])

        # Excessive tilt
        ok_t, r_t = ib.evaluate_icp_quality_gate({**good, "tilt_deg": 5.5})
        self.assertFalse(ok_t)
        self.assertTrue(any("tilt" in r for r in r_t))

        # Runaway k4
        ok_k, r_k = ib.evaluate_icp_quality_gate(good, max_abs_k4=0.06)
        self.assertFalse(ok_k)
        self.assertTrue(any("|k4|" in r for r in r_k))

        # Worker reported failure
        self.assertFalse(ib.evaluate_icp_quality_gate({"ok": False, "reason": "insufficient_roi_overlap"})[0])

        # Non-finite NaN / Inf metrics MUST be rejected (never silently pass float comparisons)
        for bad_key in ("fitness_15cm", "median_15cm_m", "scale", "tilt_deg"):
            for bad_val in (float("nan"), float("inf"), float("-inf")):
                ok_nf, reasons_nf = ib.evaluate_icp_quality_gate({**good, bad_key: bad_val})
                self.assertFalse(ok_nf, f"Expected {bad_key}={bad_val} to fail quality gate")
                self.assertTrue(reasons_nf)
        self.assertFalse(ib.evaluate_icp_quality_gate(good, max_abs_k4=float("nan"))[0])

    def test_ensure_bridge_runtime_rejects_zip_slip_and_traversal_manifest(self) -> None:
        """Verify runtime provisioner blocks Zip Slip archive entries and path traversal in manifest.json."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = os.path.join(tmp, "nas_bundle")
            cache_dir = os.path.join(tmp, "local_cache")
            os.makedirs(bundle_dir, exist_ok=True)

            # 1. Zip Slip payload
            zip_path = os.path.join(bundle_dir, "evil.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("../pwned.txt", "escape")
                zf.writestr("python.exe", "stub")
            with open(zip_path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            with open(os.path.join(bundle_dir, "manifest.json"), "w", encoding="utf-8") as mf:
                json.dump(
                    {"version": "9.9.9", "filename": "evil.zip", "python_relpath": "python.exe", "sha256": digest}, mf
                )
            env = {
                "AUTOMETASHAPE_BRIDGE_PYTHON": "",
                "AUTOMETASHAPE_BRIDGE_RUNTIME_DIR": bundle_dir,
                "AUTOMETASHAPE_BRIDGE_CACHE_DIR": cache_dir,
            }
            cache_prefix = os.path.normcase(os.path.abspath(cache_dir))

            def _only_cache_python(py_path: str) -> bool:
                return bool(py_path) and os.path.normcase(os.path.abspath(py_path)).startswith(cache_prefix)

            with patch.dict(os.environ, env, clear=False):
                with patch.object(ib, "_verify_bridge_python", side_effect=_only_cache_python):
                    self.assertIsNone(ib.ensure_bridge_runtime())
            self.assertFalse(os.path.exists(os.path.join(cache_dir, "pwned.txt")))

            # 2. Manifest python_relpath traversal
            with open(os.path.join(bundle_dir, "manifest.json"), "w", encoding="utf-8") as mf:
                json.dump(
                    {
                        "version": "9.9.8",
                        "filename": "../evil.zip",
                        "python_relpath": "../../python.exe",
                        "sha256": digest,
                    },
                    mf,
                )
            with patch.dict(os.environ, env, clear=False):
                with patch.object(ib, "_verify_bridge_python", side_effect=_only_cache_python):
                    self.assertIsNone(ib.ensure_bridge_runtime())

    def test_bridge_short_circuits_cleanly_on_local_cs_without_provisioning(self) -> None:
        """Verify non-georeferenced LOCAL_CS chunks skip runtime provisioning cleanly."""
        chunk = SimpleNamespace(
            label="LocalScan",
            crs=SimpleNamespace(wkt='LOCAL_CS["Local Coordinates"]'),
            cameras=[],
        )
        with patch.object(ib, "ensure_bridge_runtime") as mock_rt:
            out_chunk, stats = ib.bridge_disconnected_islands(
                doc=SimpleNamespace(),
                chunk=chunk,
                audit_result={"main_island": [1, 2], "bridge_candidates": [[3, 4]]},
                unaligned_groups=[],
            )
        self.assertIs(out_chunk, chunk)
        self.assertFalse(stats["triggered"])
        mock_rt.assert_not_called()

    def test_ensure_bridge_runtime_provisions_offline_zip_and_checks_sha256(self) -> None:
        """Verify atomic offline provisioning from NAS bundle zip and SHA-256 tamper protection."""
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = os.path.join(tmp, "nas_bundle")
            cache_dir = os.path.join(tmp, "local_cache")
            os.makedirs(bundle_dir, exist_ok=True)

            zip_name = "bridge_runtime_test.zip"
            zip_path = os.path.join(bundle_dir, zip_name)
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("python.exe", "mock-python-binary")
                zf.writestr("Lib/site-packages/sitecustomize.py", "# ok\n")

            with open(zip_path, "rb") as zfh:
                digest = hashlib.sha256(zfh.read()).hexdigest()
            manifest_path = os.path.join(bundle_dir, "manifest.json")
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "version": "1.0.0",
                        "filename": zip_name,
                        "python_relpath": "python.exe",
                        "sha256": digest,
                    },
                    f,
                )

            env = {
                "AUTOMETASHAPE_BRIDGE_PYTHON": "",
                "AUTOMETASHAPE_BRIDGE_RUNTIME_DIR": bundle_dir,
                "AUTOMETASHAPE_BRIDGE_CACHE_DIR": cache_dir,
            }
            with patch.dict(os.environ, env, clear=False):
                with patch.object(ib, "_verify_bridge_python", side_effect=lambda p: os.path.isfile(p)):
                    py1 = ib.ensure_bridge_runtime()
                    self.assertIsNotNone(py1)
                    self.assertTrue(os.path.isfile(py1))
                    self.assertTrue(os.path.normpath(py1).startswith(os.path.normpath(cache_dir)))

                    # Second call reuses already-provisioned local runtime without touching zip
                    os.remove(zip_path)
                    py2 = ib.ensure_bridge_runtime()
                    self.assertEqual(py1, py2)

            # Tampered checksum must be refused on fresh cache dir
            cache_bad = os.path.join(tmp, "local_cache_bad")
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.writestr("python.exe", "tampered")
            env_bad = {**env, "AUTOMETASHAPE_BRIDGE_CACHE_DIR": cache_bad}
            with patch.dict(os.environ, env_bad, clear=False):
                with patch.object(ib, "_verify_bridge_python", return_value=False):
                    self.assertIsNone(ib.ensure_bridge_runtime())
                    self.assertFalse(os.path.exists(os.path.join(cache_bad, "bridge_runtime_v1.0.0")))

    def test_clean_bridge_env_strips_pythonpath_and_metashape_dirs(self) -> None:
        """Verify bridge subprocess env removes PYTHONPATH/PYTHONHOME and Metashape DLL paths."""
        fake_path = os.pathsep.join(
            [
                r"C:\Program Files\Agisoft\Metashape Pro",
                r"C:\Program Files\Agisoft\Metashape Pro\python",
                r"C:\Windows\System32",
            ]
        )
        with patch.dict(
            os.environ,
            {
                "PYTHONPATH": "/bad/path",
                "PYTHONHOME": "/bad/home",
                "PATH": fake_path,
            },
            clear=False,
        ):
            env = ib._clean_bridge_env()
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONHOME", env)
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        path_val = env.get("PATH") or env.get("Path") or ""
        self.assertNotIn("metashape", path_val.lower())
        self.assertIn("System32", path_val)

    def test_cleanup_stale_icp_temp_dirs_removes_only_expired_orphans(self) -> None:
        """Verify stale autometashape_icp_* temp dirs are purged while active/unrelated dirs stay."""
        with tempfile.TemporaryDirectory() as tmp:
            stale = os.path.join(tmp, "autometashape_icp_old123")
            fresh = os.path.join(tmp, "autometashape_icp_new456")
            other = os.path.join(tmp, "unrelated_old_dir")
            for d in (stale, fresh, other):
                os.makedirs(d, exist_ok=True)
            old_ts = os.path.getmtime(stale) - 48 * 3600
            os.utime(stale, (old_ts, old_ts))
            os.utime(other, (old_ts, old_ts))

            removed = ib._cleanup_stale_icp_temp_dirs(max_age_hours=24.0, temp_root=tmp)
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(stale))
            self.assertTrue(os.path.exists(fresh))
            self.assertTrue(os.path.exists(other))

    def test_build_stage0_cloud_uses_4cm_uniform_sampling_for_32gb_ram_safety(self) -> None:
        """Verify Stage-0 dense cloud enables 4 cm uniform sampling to protect 32 GB RAM workers."""
        pc_kwargs: dict = {}

        class _Chunk:
            label = "test"
            point_cloud = SimpleNamespace(point_count=5000)

            def buildDepthMaps(self, **kw) -> None:
                self.depth_kw = kw

            def buildPointCloud(self, **kw) -> None:
                pc_kwargs.update(kw)

        fake_ms = SimpleNamespace(
            FilterMode=SimpleNamespace(ModerateFiltering=2),
            DataSource=SimpleNamespace(DepthMapsData=1),
        )
        with patch.object(ib, "Metashape", fake_ms):
            ok = ib._build_stage0_cloud(_Chunk())
        self.assertTrue(ok)
        self.assertTrue(pc_kwargs["uniform_sampling"])
        self.assertAlmostEqual(pc_kwargs["points_spacing"], 0.04)

    def test_bridge_fast_path_skips_when_no_candidates(self) -> None:
        """Verify zero-overhead fast path when all cameras belong to one coherent block."""
        chunk = SimpleNamespace(label="Production_Reconstruction")
        with patch.object(ib, "ensure_bridge_runtime") as mock_rt:
            out_chunk, stats = ib.bridge_disconnected_islands(
                doc=SimpleNamespace(),
                chunk=chunk,
                audit_result={"main_island": [1, 2, 3], "bridge_candidates": []},
                unaligned_groups=[],
            )
        self.assertIs(out_chunk, chunk)
        self.assertFalse(stats["triggered"])
        mock_rt.assert_not_called()

    def test_bridge_writes_temp_plys_outside_output_dir_and_filters_by_quality_gate(self) -> None:
        """Verify temp PLY exports stay outside output_dir, passing islands merge cleanly, failing islands quarantine."""
        exported_paths: list[str] = []
        merge_calls: list[dict] = []

        class _FakeMat:
            def __init__(self, rows) -> None:
                self.rows = rows

            def __mul__(self, other):
                return _FakeMat(self.rows)

        class _FakeChunk:
            def __init__(self, key: int, label: str, cameras: list[SimpleNamespace]) -> None:
                self.key = key
                self.label = label
                self.cameras = list(cameras)
                self.sensors = [SimpleNamespace(calibration=SimpleNamespace(k4=0.0, b1=0.0, b2=0.0))]
                self.transform = SimpleNamespace(
                    matrix=_FakeMat([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
                )
                self.depth_maps = None
                self.point_cloud = None

            def copy(self):
                copied = _FakeChunk(
                    key=doc._next_key(),
                    label=self.label + "_copy",
                    cameras=[
                        SimpleNamespace(
                            key=c.key,
                            enabled=c.enabled,
                            transform=c.transform,
                            photo=SimpleNamespace(path=c.photo.path),
                            reference=SimpleNamespace(enabled=c.reference.enabled),
                        )
                        for c in self.cameras
                    ],
                )
                doc.chunks.append(copied)
                return copied

            def remove(self, cams_to_remove) -> None:
                rm_keys = {c.key for c in cams_to_remove}
                self.cameras = [c for c in self.cameras if c.key not in rm_keys]

            def optimizeCameras(self, **_kw) -> None:
                pass

            def exportPointCloud(self, path: str, **_kw) -> None:
                exported_paths.append(path)
                with open(path, "w", encoding="utf-8") as f:
                    f.write("ply\n")

        class _FakeDoc:
            def __init__(self) -> None:
                self._k = 10
                self.chunks: list[_FakeChunk] = []
                self.chunk: _FakeChunk | None = None
                self.path = ""

            def _next_key(self) -> int:
                self._k += 1
                return self._k

            def save(self, p: str) -> None:
                self.path = p

            def mergeChunks(self, chunks: list[int], **kw) -> None:
                merge_calls.append({"chunks": list(chunks), **kw})
                by_k = {c.key: c for c in self.chunks}
                merged_cams = []
                for ck in chunks:
                    merged_cams.extend(by_k[ck].cameras)
                m = _FakeChunk(self._next_key(), "Merged", merged_cams)
                self.chunks.append(m)

            def remove(self, to_rm) -> None:
                rm_set = set(to_rm)
                self.chunks = [c for c in self.chunks if c not in rm_set]

        doc = _FakeDoc()
        cams = [
            SimpleNamespace(
                key=k,
                enabled=True,
                transform=object(),
                photo=SimpleNamespace(path=f"/proj/cam_{k}.jpg"),
                reference=SimpleNamespace(enabled=True),
            )
            for k in (1, 2, 10, 11, 20, 21)
        ]
        orig = _FakeChunk(1, "Production_Reconstruction", cams)
        doc.chunks.append(orig)
        doc.chunk = orig

        call_idx = {"n": 0}

        def _fake_subproc(cmd, **_kw):
            out_path = cmd[cmd.index("--output") + 1]
            call_idx["n"] += 1
            if call_idx["n"] == 1:
                # Island 1 PASSES Quality Gate
                payload = {
                    "ok": True,
                    "T_enu": [[1, 0, 0, 1.0], [0, 1, 0, -0.5], [0, 0, 1, 0.2], [0, 0, 0, 1]],
                    "scale": 0.998,
                    "tilt_deg": 0.3,
                    "fitness_15cm": 0.72,
                    "median_15cm_m": 0.04,
                }
            else:
                # Island 2 FAILS Quality Gate (fitness=0.12)
                payload = {
                    "ok": True,
                    "T_enu": [[1, 0, 0, 0.0], [0, 1, 0, 0.0], [0, 0, 1, 0.0], [0, 0, 0, 1]],
                    "scale": 1.0,
                    "tilt_deg": 0.1,
                    "fitness_15cm": 0.12,
                    "median_15cm_m": 0.14,
                }
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            return SimpleNamespace(returncode=0)

        fake_ms = SimpleNamespace(
            Vector=lambda v: tuple(v),
            Matrix=_FakeMat,
            DataSource=SimpleNamespace(PointCloudData=1),
            PointCloudFormat=SimpleNamespace(PointCloudFormatPLY=1),
        )

        with tempfile.TemporaryDirectory() as staging_out:
            with (
                patch.object(ib, "Metashape", fake_ms),
                patch.object(ib, "ensure_bridge_runtime", return_value="/fake/python.exe"),
                patch.object(ib, "_build_stage0_cloud", return_value=True),
                patch.object(ib, "_compute_island_utm_frame", return_value=(object(), (0.0, 0.0, 0.0), 32648)),
                patch.object(
                    ib,
                    "convert_utm_transform_to_ecef",
                    return_value=([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], 0.002),
                ),
                patch.object(ib.subprocess, "run", side_effect=_fake_subproc),
            ):
                final_chunk, stats = ib.bridge_disconnected_islands(
                    doc=doc,
                    chunk=orig,
                    audit_result={
                        "main_island": [1, 2],
                        "bridge_candidates": [[10, 11], [20, 21]],
                        "kept_distant": [],
                    },
                    output_dir=staging_out,
                )

            self.assertTrue(stats["triggered"])
            self.assertEqual(stats["accepted"], 1)
            self.assertEqual(stats["rejected"], 1)
            self.assertEqual(len(doc.chunks), 1)
            self.assertIs(doc.chunk, final_chunk)
            self.assertEqual(final_chunk.label, "Production_Reconstruction")

            # Merge MUST use merge_tiepoints=False and include ONLY main + island 1 (NOT island 2!)
            self.assertEqual(len(merge_calls), 1)
            self.assertFalse(merge_calls[0]["merge_tiepoints"])
            kept_keys = sorted(c.key for c in final_chunk.cameras)
            self.assertEqual(kept_keys, [1, 2, 10, 11])

            # Bridged island cameras (10, 11) must have GPS references disabled; main (1, 2) kept enabled
            by_k = {c.key: c for c in final_chunk.cameras}
            self.assertTrue(by_k[1].reference.enabled)
            self.assertFalse(by_k[10].reference.enabled)
            self.assertFalse(by_k[11].reference.enabled)

            # Temp PLYs MUST live outside staging_out AND be completely cleaned up after return
            self.assertEqual(len(exported_paths), 4)
            abs_stage = os.path.normcase(os.path.abspath(staging_out))
            for ep in exported_paths:
                self.assertFalse(os.path.normcase(os.path.abspath(ep)).startswith(abs_stage))
                self.assertFalse(os.path.exists(os.path.dirname(ep)))

    def test_bridge_promotes_wider_rescued_backbone_over_compact_initial_main(self) -> None:
        """Verify Stage-4 promotes wider rigid rescue backbone over compact Pass-1 main and keeps RS Regularized."""
        base_lon, base_lat = 106.6000, 10.7700
        deg_m = 1.0 / 111320.0
        cos_lat = math.cos(math.radians(base_lat))

        class _Mat:
            def __init__(self, rows) -> None:
                self.rows = rows

            def __mul__(self, other):
                return _Mat(self.rows)

        class _Cam:
            def __init__(self, key: int, label: str, path: str, lon: float, lat: float, ra: float, aligned: bool):
                self.key = key
                self.label = label
                self.enabled = True
                self.transform = object() if aligned else None
                self.photo = SimpleNamespace(
                    path=path,
                    meta={"DJI/AbsoluteAltitude": str(25.0 + ra), "DJI/RelativeAltitude": str(ra)},
                )
                self.reference = SimpleNamespace(
                    enabled=True,
                    location=SimpleNamespace(x=lon, y=lat, z=25.0 + ra),
                )
                self.center = SimpleNamespace(x=lon, y=lat, z=25.0 + ra)
                self.sensor = None

        man_cams = []
        for i in range(20):
            dx = -15.0 + (i % 5) * 7.5
            dy = -15.0 + (i // 5) * 10.0
            man_cams.append(
                _Cam(
                    i + 1,
                    f"man_{i:02d}.jpg",
                    os.path.abspath(f"/proj/Manual/man_{i:02d}.jpg"),
                    base_lon + dx * deg_m / cos_lat,
                    base_lat + dy * deg_m,
                    10.0,
                    True,
                )
            )
        aer_cams = []
        for i in range(30):
            dx = -100.0 + (i % 6) * 40.0
            dy = -100.0 + (i // 6) * 50.0
            aer_cams.append(
                _Cam(
                    100 + i,
                    f"aer_{i:02d}.jpg",
                    os.path.abspath(f"/proj/Aerial/aer_{i:02d}.jpg"),
                    base_lon + dx * deg_m / cos_lat,
                    base_lat + dy * deg_m,
                    105.0,
                    False,
                )
            )
        by_path = {c.photo.path: c for c in man_cams + aer_cams}
        stage0_built: list[str] = []

        class _PromoChunk:
            def __init__(
                self, key: int, label: str, cameras: list[Any] | None = None, sensors: list[Any] | None = None
            ):
                self.key = key
                self.label = label
                self.cameras = list(cameras or [])
                self.sensors = list(
                    sensors
                    or [
                        SimpleNamespace(
                            rolling_shutter=None,
                            calibration=SimpleNamespace(k4=0.0, b1=0.0, b2=0.0),
                        )
                    ]
                )
                self.crs = SimpleNamespace(
                    wkt='GEOGCS["WGS 84",DATUM["WGS_1984"]]',
                    project=lambda v: v,
                )
                self.transform = SimpleNamespace(matrix=_Mat([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]))
                self.tie_points = None
                self.depth_maps = None
                self.point_cloud = None

            def addPhotos(self, paths: list[str]) -> None:
                for p in paths:
                    src = by_path[os.path.abspath(p)]
                    cloned = _Cam(
                        src.key,
                        src.label,
                        src.photo.path,
                        src.reference.location.x,
                        src.reference.location.y,
                        float(src.photo.meta["DJI/RelativeAltitude"]),
                        False,
                    )
                    cloned.sensor = self.sensors[0]
                    self.cameras.append(cloned)

            def matchPhotos(self, **_kw) -> None:
                pass

            def alignCameras(self, **_kw) -> None:
                for c in self.cameras:
                    c.transform = object()

            def copy(self):
                copied = _PromoChunk(
                    doc._next_key(),
                    self.label + "_copy",
                    [
                        _Cam(
                            c.key,
                            c.label,
                            c.photo.path,
                            c.reference.location.x,
                            c.reference.location.y,
                            float(c.photo.meta["DJI/RelativeAltitude"]),
                            c.transform is not None,
                        )
                        for c in self.cameras
                    ],
                )
                doc.chunks.append(copied)
                return copied

            def remove(self, rm_cams) -> None:
                rm_keys = {c.key for c in rm_cams}
                self.cameras = [c for c in self.cameras if c.key not in rm_keys]

            def optimizeCameras(self, **_kw) -> None:
                pass

            def exportPointCloud(self, path: str, **_kw) -> None:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("ply\n")

        class _PromoDoc:
            def __init__(self) -> None:
                self._k = 10
                self.chunks: list[_PromoChunk] = []
                self.chunk: _PromoChunk | None = None
                self.path = ""

            def _next_key(self) -> int:
                self._k += 1
                return self._k

            def addChunk(self) -> _PromoChunk:
                c = _PromoChunk(self._next_key(), "Chunk")
                self.chunks.append(c)
                return c

            def remove(self, to_rm) -> None:
                rm_set = set(to_rm)
                self.chunks = [c for c in self.chunks if c not in rm_set]

        doc = _PromoDoc()
        orig = _PromoChunk(1, "Production_Reconstruction", man_cams + aer_cams)
        doc.chunks.append(orig)
        doc.chunk = orig

        def _fake_stage0(ch_obj: _PromoChunk) -> bool:
            stage0_built.append(ch_obj.label)
            return True

        def _fail_icp(cmd, **_kw):
            out_path = cmd[cmd.index("--output") + 1]
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "ok": True,
                        "T_enu": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
                        "scale": 1.0,
                        "tilt_deg": 0.1,
                        "fitness_15cm": 0.10,
                        "median_15cm_m": 0.04,
                    },
                    f,
                )
            return SimpleNamespace(returncode=0)

        fake_ms = SimpleNamespace(
            Vector=lambda v: SimpleNamespace(x=v[0], y=v[1], z=v[2]),
            Matrix=_Mat,
            DataSource=SimpleNamespace(PointCloudData=1),
            PointCloudFormat=SimpleNamespace(PointCloudFormatPLY=1),
            ReferencePreselectionMode=SimpleNamespace(ReferencePreselectionSource=1),
            Shutter=SimpleNamespace(Model=SimpleNamespace(Disabled="Disabled", Regularized="Regularized")),
        )
        with (
            patch.object(ib, "Metashape", fake_ms),
            patch.object(ib, "ensure_bridge_runtime", return_value="/fake/python.exe"),
            patch.object(ib, "_build_stage0_cloud", side_effect=_fake_stage0),
            patch.object(ib, "_compute_island_utm_frame", return_value=(object(), (0.0, 0.0, 0.0), 32648)),
            patch.object(ib.subprocess, "run", side_effect=_fail_icp),
        ):
            final_chunk, stats = ib.bridge_disconnected_islands(
                doc=doc,
                chunk=orig,
                audit_result={"main_island": [c.key for c in man_cams], "bridge_candidates": [], "kept_distant": []},
                unaligned_groups=[[c.key for c in aer_cams]],
                rolling_shutter=True,
            )

        # Wide aerial rescue chunk was promoted BEFORE building Stage-0 target cloud, kept Regularized RS,
        # and survived even after compact Manual failed ICP Quality Gate!
        self.assertEqual(stage0_built[0], "_ICP_Rescue_1")
        self.assertEqual(final_chunk.sensors[0].rolling_shutter, "Regularized")
        self.assertTrue(stats["triggered"])
        self.assertEqual(stats["accepted"], 0)
        self.assertEqual(stats["rejected"], 1)
        self.assertEqual(len(doc.chunks), 1)
        self.assertEqual(len(final_chunk.cameras), 30)
        self.assertTrue(all(c.label.startswith("aer_") for c in final_chunk.cameras))

    def test_evaluate_icp_quality_gate_partial_overlap_and_guards(self) -> None:
        """Verify partial-overlap ICP acceptance and strict minimum inlier/overlap guards."""
        partial_good = {
            "ok": True,
            "fitness_15cm": 0.22,
            "overlap_fitness_15cm": 0.68,
            "overlap_fraction": 0.25,
            "inlier_count_15cm": 2500,
            "median_15cm_m": 0.04,
            "scale": 1.002,
            "tilt_deg": 0.6,
        }
        self.assertEqual(ib.evaluate_icp_quality_gate(partial_good), (True, []))

        # Reject tiny corner graze (inliers < 1000)
        self.assertFalse(ib.evaluate_icp_quality_gate({**partial_good, "inlier_count_15cm": 200})[0])
        # Reject insufficient ROI fraction (< 0.15)
        self.assertFalse(ib.evaluate_icp_quality_gate({**partial_good, "overlap_fraction": 0.05})[0])
        # Reject poor alignment inside ROI (< 0.45)
        self.assertFalse(ib.evaluate_icp_quality_gate({**partial_good, "overlap_fitness_15cm": 0.35})[0])
        # Reject NaN overlap metrics
        self.assertFalse(ib.evaluate_icp_quality_gate({**partial_good, "overlap_fitness_15cm": float("nan")})[0])
        # Still enforce median/scale/tilt even when partial overlap metrics look good
        self.assertFalse(ib.evaluate_icp_quality_gate({**partial_good, "median_15cm_m": 0.14})[0])
        self.assertFalse(ib.evaluate_icp_quality_gate({**partial_good, "tilt_deg": 6.5})[0])

    def test_isolate_coherent_sub_islands_prunes_rogue_fragments(self) -> None:
        """Verify rescue chunk splitting into [19, 8, 5] + 3 unaligned keeps ONLY the 19 coherent cams."""

        class _Cam:
            def __init__(self, key: int, label: str, path: str, transform: Any) -> None:
                self.key = key
                self.label = label
                self.enabled = True
                self.transform = transform
                self.photo = SimpleNamespace(path=path)
                self.reference = SimpleNamespace(enabled=True, location=None)
                self.sensor = None

        def _make_grouped_chunk(group_sizes: list[int], unaligned_cnt: int = 0) -> Any:
            cameras: list[_Cam] = []
            points = []
            projs: dict[Any, list[SimpleNamespace]] = {}
            cam_key = 1
            track_id = 1
            for g_idx, g_size in enumerate(group_sizes):
                g_cams = []
                for j in range(g_size):
                    c = _Cam(cam_key, f"g{g_idx}_c{j}", f"/proj/g{g_idx}_c{j}.jpg", object())
                    cameras.append(c)
                    g_cams.append(c)
                    projs[c] = []
                    cam_key += 1
                # Connect consecutive pairs within group with 20 valid shared tie tracks (> edge_min=15)
                for j in range(len(g_cams) - 1):
                    for _ in range(20):
                        points.append(SimpleNamespace(track_id=track_id, valid=True))
                        projs[g_cams[j]].append(SimpleNamespace(track_id=track_id))
                        projs[g_cams[j + 1]].append(SimpleNamespace(track_id=track_id))
                        track_id += 1
            for u in range(unaligned_cnt):
                c = _Cam(cam_key, f"unal_{u}", f"/proj/unal_{u}.jpg", None)
                cameras.append(c)
                projs[c] = []
                cam_key += 1

            class _SubChunk:
                def __init__(self, label: str, cams: list[Any]) -> None:
                    self.label = label
                    self.cameras = list(cams)
                    self.sensors = [SimpleNamespace(calibration=SimpleNamespace(k4=0.0, b1=0.0, b2=0.0))]
                    self.tie_points = SimpleNamespace(points=points, projections=projs)
                    self.opt_calls = 0

                def copy(self):
                    cloned_cams = [
                        _Cam(c.key + 1000, c.label, c.photo.path, c.transform)  # Simulate Metashape key renumbering
                        for c in self.cameras
                    ]
                    return _SubChunk(self.label + "_copy", cloned_cams)

                def remove(self, rm_cams) -> None:
                    rm_ids = {id(c) for c in rm_cams}
                    self.cameras = [c for c in self.cameras if id(c) not in rm_ids]

                def optimizeCameras(self, **_kw) -> None:
                    self.opt_calls += 1

                def updateTransform(self) -> None:
                    pass

                def resetRegion(self) -> None:
                    pass

            return _SubChunk("_ICP_Rescue_1", cameras)

        # Case 1: CATP_0038 regression — [19, 8, 5] + 3 unaligned -> keep ONLY the 19 coherent cameras
        ch1 = _make_grouped_chunk([19, 8, 5], unaligned_cnt=3)
        temps1: list[Any] = []
        res1 = ib._isolate_coherent_sub_islands(ch1, "rescue_1", temps1, min_cams=12)
        self.assertEqual(len(res1), 1)
        self.assertEqual(res1[0][0], "rescue_1")
        self.assertEqual(len(res1[0][1].cameras), 19)
        self.assertTrue(all(c.label.startswith("g0_") for c in res1[0][1].cameras))
        self.assertEqual(ch1.opt_calls, 1)
        self.assertEqual(temps1, [])

        # Case 2: Multiple sizable internal islands [15, 13, 4] -> split into 2 isolated chunks & drop 4-cam fragment
        ch2 = _make_grouped_chunk([15, 13, 4], unaligned_cnt=2)
        temps2: list[Any] = []
        res2 = ib._isolate_coherent_sub_islands(ch2, "rescue_2", temps2, min_cams=12)
        self.assertEqual(len(res2), 2)
        self.assertEqual([lbl for lbl, _ in res2], ["rescue_2_sub1", "rescue_2_sub2"])
        self.assertEqual(len(res2[0][1].cameras), 15)
        self.assertEqual(len(res2[1][1].cameras), 13)
        self.assertTrue(all(c.label.startswith("g0_") for c in res2[0][1].cameras))
        self.assertTrue(all(c.label.startswith("g1_") for c in res2[1][1].cameras))
        self.assertEqual(len(temps2), 1)
        self.assertIs(temps2[0], res2[1][1])

        # Case 3: Shattered chunk [7, 7, 6] (20 aligned total, but max island < 12) -> rejected cleanly
        ch3 = _make_grouped_chunk([7, 7, 6])
        temps3: list[Any] = []
        res3 = ib._isolate_coherent_sub_islands(ch3, "rescue_3", temps3, min_cams=12)
        self.assertEqual(res3, [])
        self.assertEqual(temps3, [])

    def test_bridge_rescue_prunes_detached_fragments_before_merge(self) -> None:
        """End-to-end check that detached internal fragments in _ICP_Rescue never reach mergeChunks()."""

        class _Cam:
            def __init__(self, key: int, label: str, path: str, transform: Any, ref_enabled: bool = True) -> None:
                self.key = key
                self.label = label
                self.enabled = True
                self.transform = transform
                self.photo = SimpleNamespace(path=path)
                self.reference = SimpleNamespace(enabled=ref_enabled, location=None)
                self.sensor = None

        class _FakeMat:
            def __init__(self, rows) -> None:
                self.rows = rows

            def __mul__(self, other):
                return _FakeMat(self.rows)

        class _RescueChunk:
            def __init__(self, key: int, label: str, cameras: list[Any] | None = None) -> None:
                self.key = key
                self.label = label
                self.cameras = list(cameras or [])
                self.sensors = [SimpleNamespace(calibration=SimpleNamespace(k4=0.0, b1=0.0, b2=0.0))]
                self.transform = SimpleNamespace(
                    matrix=_FakeMat([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
                )
                self.tie_points = None
                self.depth_maps = None
                self.point_cloud = None

            def addPhotos(self, paths: list[str]) -> None:
                for i, p in enumerate(paths, 1):
                    self.cameras.append(_Cam(100 + i, os.path.basename(p), p, None))

            def matchPhotos(self, **_kw) -> None:
                pass

            def alignCameras(self, **_kw) -> None:
                # 14 coherent (0..13) + 6 detached fragment (14..19) + 2 unaligned (20..21)
                pts = []
                projs: dict[Any, list[SimpleNamespace]] = {c: [] for c in self.cameras}
                tid = 1
                for idx, c in enumerate(self.cameras):
                    if idx < 20:
                        c.transform = object()
                for grp in (self.cameras[:14], self.cameras[14:20]):
                    for j in range(len(grp) - 1):
                        for _ in range(18):
                            pts.append(SimpleNamespace(track_id=tid, valid=True))
                            projs[grp[j]].append(SimpleNamespace(track_id=tid))
                            projs[grp[j + 1]].append(SimpleNamespace(track_id=tid))
                            tid += 1
                self.tie_points = SimpleNamespace(points=pts, projections=projs)

            def copy(self):
                copied = _RescueChunk(
                    doc._next_key(),
                    self.label + "_copy",
                    [_Cam(c.key, c.label, c.photo.path, c.transform, c.reference.enabled) for c in self.cameras],
                )
                doc.chunks.append(copied)
                return copied

            def remove(self, rm_cams) -> None:
                rm_keys = {c.key for c in rm_cams}
                self.cameras = [c for c in self.cameras if c.key not in rm_keys]

            def optimizeCameras(self, **_kw) -> None:
                pass

            def exportPointCloud(self, path: str, **_kw) -> None:
                with open(path, "w", encoding="utf-8") as f:
                    f.write("ply\n")

        class _FakeDoc:
            def __init__(self) -> None:
                self._k = 10
                self.chunks: list[_RescueChunk] = []
                self.chunk: _RescueChunk | None = None
                self.path = ""

            def _next_key(self) -> int:
                self._k += 1
                return self._k

            def addChunk(self) -> _RescueChunk:
                c = _RescueChunk(self._next_key(), "Chunk")
                self.chunks.append(c)
                return c

            def mergeChunks(self, chunks: list[int], **_kw) -> None:
                by_k = {c.key: c for c in self.chunks}
                merged_cams = []
                for ck in chunks:
                    merged_cams.extend(by_k[ck].cameras)
                self.chunks.append(_RescueChunk(self._next_key(), "Merged", merged_cams))

            def remove(self, to_rm) -> None:
                rm_set = set(to_rm)
                self.chunks = [c for c in self.chunks if c not in rm_set]

        doc = _FakeDoc()
        main_cams = [_Cam(k, f"main_{k}.jpg", f"/proj/main_{k}.jpg", object()) for k in (1, 2)]
        res_cams = [_Cam(10 + i, f"res_{i:02d}.jpg", f"/proj/res_{i:02d}.jpg", None) for i in range(22)]
        orig = _RescueChunk(1, "Production_Reconstruction", main_cams + res_cams)
        doc.chunks.append(orig)
        doc.chunk = orig

        def _pass_icp(cmd, **_kw):
            out_path = cmd[cmd.index("--output") + 1]
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "ok": True,
                        "T_enu": [[1, 0, 0, 0.2], [0, 1, 0, -0.1], [0, 0, 1, 0.05], [0, 0, 0, 1]],
                        "scale": 1.0,
                        "tilt_deg": 0.2,
                        "fitness_15cm": 0.78,
                        "median_15cm_m": 0.03,
                    },
                    f,
                )
            return SimpleNamespace(returncode=0)

        fake_ms = SimpleNamespace(
            Vector=lambda v: tuple(v),
            Matrix=_FakeMat,
            DataSource=SimpleNamespace(PointCloudData=1),
            PointCloudFormat=SimpleNamespace(PointCloudFormatPLY=1),
            ReferencePreselectionMode=SimpleNamespace(ReferencePreselectionSource=1),
        )
        with (
            patch.object(ib, "Metashape", fake_ms),
            patch.object(ib, "ensure_bridge_runtime", return_value="/fake/python.exe"),
            patch.object(ib, "_build_stage0_cloud", return_value=True),
            patch.object(ib, "_compute_island_utm_frame", return_value=(object(), (0.0, 0.0, 0.0), 32648)),
            patch.object(
                ib,
                "convert_utm_transform_to_ecef",
                return_value=([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], 0.001),
            ),
            patch.object(ib.subprocess, "run", side_effect=_pass_icp),
        ):
            final_chunk, stats = ib.bridge_disconnected_islands(
                doc=doc,
                chunk=orig,
                audit_result={"main_island": [1, 2], "bridge_candidates": [], "kept_distant": []},
                unaligned_groups=[[c.key for c in res_cams]],
            )

        self.assertTrue(stats["triggered"])
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["rejected"], 0)
        # Main (2) + coherent rescue primary island (14) = 16; detached 6-cam fragment & 2 unaligned MUST be excluded!
        final_labels = sorted(c.label for c in final_chunk.cameras)
        self.assertEqual(len(final_labels), 16)
        self.assertIn("res_00.jpg", final_labels)
        self.assertIn("res_13.jpg", final_labels)
        self.assertNotIn("res_14.jpg", final_labels)
        self.assertNotIn("res_19.jpg", final_labels)
        self.assertNotIn("res_21.jpg", final_labels)

    def test_icp_worker_end_to_end_on_synthetic_building_cloud(self) -> None:
        """Integration test running real icp_worker.py via portable Open3D runtime if available."""
        bridge_py = ib.ensure_bridge_runtime(network_path=os.environ.get("AUTOMETASHAPE_CLUSTER_ROOT", ""))
        if not bridge_py:
            self.skipTest("Portable Open3D bridge runtime not available in this environment.")

        with tempfile.TemporaryDirectory(prefix="am_icp_e2e_") as tmp:
            tgt_ply = os.path.join(tmp, "target.ply")
            src_ply = os.path.join(tmp, "source.ply")
            out_json = os.path.join(tmp, "result.json")

            # Generate synthetic L-shaped building roof + walls with known offset (dXY=(2,-1.5), dZ=+1.5, yaw=+4deg, scale=0.99)
            gen_code = (
                "import math, numpy as np, open3d as o3d\n"
                "rng = np.random.default_rng(42)\n"
                "gx, gy = np.meshgrid(np.linspace(-6, 6, 60), np.linspace(-5, 5, 50))\n"
                "roof = np.column_stack([gx.ravel(), gy.ravel(), 5.0 - 0.15 * np.abs(gx.ravel())])\n"
                "wx, wz = np.meshgrid(np.linspace(-6, 6, 60), np.linspace(0, 4.1, 25))\n"
                "w1 = np.column_stack([wx.ravel(), np.full(wx.size, -5.0), wz.ravel()])\n"
                "w2 = np.column_stack([wx.ravel(), np.full(wx.size, 5.0), wz.ravel()])\n"
                "wy, wz2 = np.meshgrid(np.linspace(-5, 5, 50), np.linspace(0, 4.1, 25))\n"
                "w3 = np.column_stack([np.full(wy.size, -6.0), wy.ravel(), wz2.ravel()])\n"
                "pts_t = np.vstack([roof, w1, w2, w3])\n"
                "cols = np.clip(0.3 + 0.04 * pts_t[:, 0:3] / 6.0 + 0.2, 0.0, 1.0)\n"
                "p_t = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts_t))\n"
                "p_t.colors = o3d.utility.Vector3dVector(cols)\n"
                "p_t.estimate_normals()\n"
                f"o3d.io.write_point_cloud({tgt_ply!r}, p_t)\n"
                "ang = math.radians(-4.0)\n"
                "R = np.array([[math.cos(ang), -math.sin(ang), 0], [math.sin(ang), math.cos(ang), 0], [0, 0, 1]])\n"
                "pts_s = ((pts_t - np.array([2.0, -1.5, 1.5])) / 0.99) @ R.T\n"
                "p_s = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts_s))\n"
                "p_s.colors = o3d.utility.Vector3dVector(cols)\n"
                "p_s.estimate_normals()\n"
                f"o3d.io.write_point_cloud({src_ply!r}, p_s)\n"
            )
            gen_res = subprocess.run(
                [bridge_py, "-s", "-c", gen_code], env=ib._clean_bridge_env(), capture_output=True, text=True
            )
            self.assertEqual(gen_res.returncode, 0, gen_res.stderr)

            run_res = subprocess.run(
                [
                    bridge_py,
                    "-s",
                    ib._ICP_WORKER_SCRIPT,
                    "--target",
                    tgt_ply,
                    "--source",
                    src_ply,
                    "--output",
                    out_json,
                ],
                env=ib._clean_bridge_env(),
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(run_res.returncode, 0, run_res.stderr)
            with open(out_json, "r", encoding="utf-8") as f:
                metrics = json.load(f)

            passed, reasons = ib.evaluate_icp_quality_gate(metrics)
            self.assertTrue(passed, f"Expected pass, got reasons={reasons}, metrics={metrics}")
            self.assertGreater(metrics["fitness_15cm"], 0.90)
            self.assertGreater(metrics["overlap_fitness_15cm"], 0.90)
            self.assertGreater(metrics["overlap_fraction"], 0.90)
            self.assertGreaterEqual(metrics["inlier_count_15cm"], 1000)
            self.assertLess(metrics["median_15cm_m"], 0.03)
            self.assertAlmostEqual(metrics["scale"], 0.99, delta=0.015)
            self.assertLess(metrics["tilt_deg"], 1.0)


if __name__ == "__main__":
    unittest.main()
