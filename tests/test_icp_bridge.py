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

    def test_icp_worker_end_to_end_on_synthetic_building_cloud(self) -> None:
        """Integration test running real icp_worker.py via portable Open3D runtime if available."""
        bridge_py = ib.ensure_bridge_runtime(network_path=r"\\10.5.10.202\catp")
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
            self.assertLess(metrics["median_15cm_m"], 0.03)
            self.assertAlmostEqual(metrics["scale"], 0.99, delta=0.015)
            self.assertLess(metrics["tilt_deg"], 1.0)


if __name__ == "__main__":
    unittest.main()
