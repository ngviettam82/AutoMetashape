# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Wave F comprehensive stabilization regression tests (Commit 1-3 invariants)."""

from __future__ import annotations

import importlib
import inspect
import io
import json
import math
import os
import shlex
import sys
import tempfile
import typing
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")

for _p in (_SCRIPTS_DIR, _SOURCE_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import metashape_reconstruction as mr  # noqa: E402
from autometashape.cluster import abort as abort_mod  # noqa: E402
from autometashape.cluster import dual_share as dual_mod  # noqa: E402
from autometashape.cluster import hardware as hw_mod  # noqa: E402
from autometashape.cluster import orchestrator as orch_mod  # noqa: E402
from autometashape.core import alignment_hygiene as ah_mod  # noqa: E402
from autometashape.core import crs_utils as crs_mod  # noqa: E402
from autometashape.core import filtering as flt_mod  # noqa: E402
from autometashape.core import icp_bridge as ib_mod  # noqa: E402
from autometashape.core import pipeline as pipe_mod  # noqa: E402
from autometashape.core import telemetry as tel_mod  # noqa: E402
from autometashape.core import verification as ver_mod  # noqa: E402

import cluster_abort as abort_cli  # noqa: E402


class _WktCrs:
    """Minimal CRS stub with WKT and optional name attribute."""

    def __init__(self, wkt: str, name: str = "") -> None:
        self.wkt = wkt
        self.name = name or wkt


class TestWaveFBatchAndTyping(unittest.TestCase):
    """Verify Windows batch launcher hygiene and typing import resolution."""

    def test_windows_batch_launchers_valid_syntax_and_update_protections(self) -> None:
        """All modified .bat files keep clean setlocal syntax and update.bat protects .agents."""
        bat_names = (
            "build.bat",
            "format_code.bat",
            "update.bat",
            "launch_server.bat",
            "launch_worker.bat",
            "cluster_abort.bat",
            "update_fleet.bat",
        )
        for name in bat_names:
            path = os.path.join(_REPO_ROOT, name)
            self.assertTrue(os.path.isfile(path), msg=f"Missing launcher: {name}")
            with open(path, "r", encoding="utf-8") as fh:
                txt = fh.read()
            self.assertIn("setlocal", txt.lower(), msg=f"{name} missing setlocal")
        with open(os.path.join(_REPO_ROOT, "update.bat"), "r", encoding="utf-8") as ufh:
            upd_txt = ufh.read()
        self.assertIn(".agents", upd_txt)

    def test_typing_get_type_hints_resolves_without_name_errors(self) -> None:
        """Evaluate get_type_hints across modified modules (zero NameError; full check on Py 3.10+)."""
        mod_names = (
            "autometashape.core.crs_utils",
            "autometashape.core.alignment_hygiene",
            "autometashape.core.icp_bridge",
            "autometashape.core.filtering",
            "autometashape.core.verification",
            "autometashape.core.export",
            "autometashape.core.telemetry",
            "autometashape.core.pipeline",
            "autometashape.cluster.orchestrator",
            "autometashape.cluster.hardware",
            "autometashape.cluster.abort",
            "autometashape.cluster.dual_share",
            "autometashape.cluster.updater",
            "autometashape.cluster.supervisor",
            "autometashape.cluster.config",
        )
        fake_ms = SimpleNamespace(NetworkClient=type("NetworkClient", (), {}))
        failures: list[str] = []
        with patch.object(orch_mod, "Metashape", orch_mod.Metashape or fake_ms):
            for mname in mod_names:
                mod = importlib.import_module(mname)
                for fname, fn in inspect.getmembers(mod, inspect.isfunction):
                    if fn.__module__ != mod.__name__:
                        continue
                    try:
                        typing.get_type_hints(fn)
                    except TypeError as exc:
                        is_pep604 = "unsupported operand type(s) for |" in str(exc)
                        if sys.version_info >= (3, 10) or not is_pep604:
                            failures.append(f"{mname}.{fname}: TypeError: {exc}")
                    except Exception as exc:
                        failures.append(f"{mname}.{fname}: {type(exc).__name__}: {exc}")
        self.assertEqual(failures, [])


class TestWaveFCrsAndHygiene(unittest.TestCase):
    """Verify CRS classification, vertical units, compound bypass, geocentric & Option A Z0."""

    def test_crs_classification_compound_geoid_and_vertical_unit(self) -> None:
        """Distinguish Engineering PROJCS, geocentric ECEF, vertical foot units, and compound EGM bypass."""
        eng_proj = _WktCrs(
            'PROJCS["Local Engineering Grid",GEOGCS["WGS 84"],UNIT["metre",1]]',
            name="Local Engineering Grid",
        )
        self.assertEqual(crs_mod.classify_crs(eng_proj), "projected")

        ecef = _WktCrs('GEOCCS["WGS 84",DATUM["WGS_1984"],UNIT["metre",1]]')
        self.assertEqual(crs_mod.classify_crs(ecef), "geocentric")
        self.assertTrue(crs_mod.is_georeferenced_crs(ecef))

        comp_ft = _WktCrs(
            'COMPD_CS["WGS 84 / UTM zone 17N + NAVD88 height (ftUS)",'
            'PROJCS["UTM 17N",UNIT["metre",1]],'
            'VERT_CS["NAVD88",UNIT["US survey foot",0.3048006096012192]]]'
        )
        self.assertTrue(crs_mod.is_compound_or_geoid_crs(comp_ft))
        self.assertAlmostEqual(crs_mod.crs_linear_unit_to_meters(comp_ft), 1.0, places=6)
        self.assertAlmostEqual(crs_mod.crs_vertical_unit_to_meters(comp_ft), 1200.0 / 3937.0, places=6)

        wgs_egm96 = _WktCrs(
            'COMPD_CS["WGS 84 + EGM96 geoid height",'
            'GEOGCS["WGS 84"],VERT_CS["EGM96",UNIT["metre",1]]]'
        )
        transform_calls: list[tuple] = []

        class _FakeCs:
            def __init__(self, code: str) -> None:
                self.wkt = code

            @staticmethod
            def transform(vec, src, dst):
                transform_calls.append((tuple(vec), dst))
                return SimpleNamespace(x=vec[0], y=vec[1], z=vec[2] - 28.5)

        fake_ms = SimpleNamespace(CoordinateSystem=_FakeCs, Vector=lambda v: list(v))
        res = crs_mod.transform_wgs84_location_to_crs(
            (106.7, 10.8, 100.0),
            wgs_egm96,
            metashape_mod=fake_ms,
        )
        self.assertEqual(len(transform_calls), 1)
        self.assertEqual(res, (106.7, 10.8, 71.5))

    def test_sanitize_camera_gps_handles_geocentric_ecef(self) -> None:
        """Geocentric ECEF disables <1000m origin and >5km tangent outlier while keeping cluster."""
        base_ecef = (-2100000.0, 5600000.0, 1200000.0)
        cams = [
            SimpleNamespace(
                enabled=True,
                reference=SimpleNamespace(
                    enabled=True,
                    location=SimpleNamespace(x=base_ecef[0] + i * 20.0, y=base_ecef[1], z=base_ecef[2]),
                ),
            )
            for i in range(4)
        ]
        null_cam = SimpleNamespace(
            enabled=True,
            reference=SimpleNamespace(enabled=True, location=SimpleNamespace(x=0.0, y=0.0, z=0.0)),
        )
        outlier_cam = SimpleNamespace(
            enabled=True,
            reference=SimpleNamespace(
                enabled=True,
                location=SimpleNamespace(x=base_ecef[0] + 15000.0, y=base_ecef[1] + 15000.0, z=base_ecef[2]),
            ),
        )
        chunk = SimpleNamespace(
            crs=_WktCrs('GEOCCS["WGS 84",UNIT["metre",1]]'),
            cameras=cams + [null_cam, outlier_cam],
        )
        stats = ah_mod.sanitize_camera_gps(chunk, max_jump_m=5000.0)
        self.assertEqual(stats["null_island_disabled"], 1)
        self.assertEqual(stats["outlier_disabled"], 1)
        self.assertFalse(null_cam.reference.enabled)
        self.assertFalse(outlier_cam.reference.enabled)
        self.assertTrue(all(c.reference.enabled for c in cams))

    def test_takeoff_z0_option_a_idempotent_mixed_rtk_feet_and_disjoint_acc(self) -> None:
        """Option A skips local/geocentric, runs idempotently in feet, and preserves RTK cams."""
        local_chunk = SimpleNamespace(crs=_WktCrs('LOCAL_CS["Local"]'), cameras=[SimpleNamespace()])
        self.assertEqual(ah_mod.normalize_flight_takeoff_z0(local_chunk)["reason"], "local_crs")
        geocc_chunk = SimpleNamespace(crs=_WktCrs('GEOCCS["WGS 84"]'), cameras=[SimpleNamespace()])
        self.assertEqual(ah_mod.normalize_flight_takeoff_z0(geocc_chunk)["reason"], "geocentric_crs")

        ft_to_m = 1200.0 / 3937.0
        comp_ft = _WktCrs(
            'COMPD_CS["UTM + ftUS",PROJCS["UTM 17N",UNIT["metre",1]],'
            'VERT_CS["NAVD88",UNIT["US survey foot",0.3048006096012192]]]'
        )

        def _mk_cam(key: int, x: float, y: float, z_ft: float, aa: float, ra: float, rtk: bool = False):
            meta = {"DJI/AbsoluteAltitude": str(aa), "DJI/RelativeAltitude": str(ra)}
            if rtk:
                meta["DJI/RtkFlag"] = "50"
            return SimpleNamespace(
                key=key,
                label=f"CAM_{key}",
                enabled=True,
                meta={},
                photo=SimpleNamespace(path=f"/proj/cam_{key}.jpg", meta=meta),
                reference=SimpleNamespace(
                    enabled=True,
                    location=SimpleNamespace(x=x, y=y, z=z_ft),
                    accuracy=SimpleNamespace(
                        x=0.02 if rtk else 5.0,
                        y=0.02 if rtk else 5.0,
                        z=0.05 if rtk else 10.0,
                    ),
                ),
            )

        rtk_a = _mk_cam(1, 500000.0, 1200000.0, 100.0 / ft_to_m, aa=100.0, ra=50.0, rtk=True)
        rtk_b = _mk_cam(2, 500030.0, 1200030.0, 100.0 / ft_to_m, aa=100.0, ra=50.0, rtk=True)
        cons_a = _mk_cam(3, 500010.0, 1200010.0, 110.0 / ft_to_m, aa=110.0, ra=50.0)
        cons_b = _mk_cam(4, 500020.0, 1200020.0, 110.0 / ft_to_m, aa=110.0, ra=50.0)
        cons_c = _mk_cam(5, 500025.0, 1200025.0, 110.0 / ft_to_m, aa=110.0, ra=50.0)
        far_cam = _mk_cam(6, 505000.0, 1205000.0, 80.0 / ft_to_m, aa=80.0, ra=50.0)
        chunk = SimpleNamespace(
            crs=comp_ft,
            cameras=[rtk_a, rtk_b, cons_a, cons_b, cons_c, far_cam],
            meta={},
            camera_location_accuracy=None,
        )
        with patch.object(ah_mod, "Metashape", None), redirect_stdout(io.StringIO()):
            out1 = ah_mod.normalize_flight_takeoff_z0(chunk)
            z_after_1 = cons_a.reference.location.z
            out2 = ah_mod.normalize_flight_takeoff_z0(chunk)
            z_after_2 = cons_a.reference.location.z

        self.assertFalse(out1["skipped"])
        self.assertAlmostEqual(z_after_1 * ft_to_m, 100.0, places=3)
        self.assertAlmostEqual(z_after_2, z_after_1, places=6)
        self.assertAlmostEqual(rtk_a.reference.accuracy.z, 0.05, places=6)
        self.assertAlmostEqual(cons_a.reference.accuracy[2] * ft_to_m, 15.0, places=3)
        self.assertAlmostEqual(far_cam.reference.accuracy[2] * ft_to_m, 20.0, places=3)

    def test_catastrophic_gps_outlier_keeps_visually_consistent_pose(self) -> None:
        """Camera with catastrophic >25m GPS error that agrees with visual neighbors keeps pose."""
        base_lat, base_lon = 10.77, 106.66
        deg_m = 1.0 / 111320.0
        cos_lat = math.cos(math.radians(base_lat))

        class _Chunk:
            def __init__(self) -> None:
                self.crs = _WktCrs('GEOGCS["WGS 84"]')
                self.cameras = []
                self.tie_points = None

            def optimizeCameras(self, **_kw) -> None:
                pass

        cams = []
        # 1..6 anchored near datum (med_dz ~ 0); 7..9 consistent >25m ramp; 10 isolated spike
        dz_vals = [0.0, 0.0, 0.0, 0.0, 0.5, 1.0, 25.5, 26.5, 27.0, 45.0]
        for idx, dz_val in enumerate(dz_vals):
            lon = base_lon + (idx * 10.0) * deg_m / cos_lat
            cams.append(
                SimpleNamespace(
                    key=idx + 1,
                    label=f"C{idx + 1}",
                    enabled=True,
                    transform=object(),
                    center=SimpleNamespace(x=idx * 10.0, y=0.0, z=60.0),
                    photo=SimpleNamespace(path=f"/p/c{idx}.jpg", meta={}),
                    reference=SimpleNamespace(
                        enabled=True,
                        location=SimpleNamespace(x=lon, y=base_lat, z=60.0 + dz_val),
                    ),
                )
            )
        chunk = _Chunk()
        chunk.cameras = cams
        strong_adj = {
            1: {2, 3, 4, 5, 6},
            2: {1, 3, 4, 5, 6},
            3: {1, 2, 4, 5, 6},
            4: {1, 2, 3, 5, 6},
            5: {1, 2, 3, 4, 6},
            6: {1, 2, 3, 4, 5},
            7: {8, 9},
            8: {7, 9},
            9: {7, 8},
            10: {1, 2, 3, 4},
        }
        islands = [[c.key for c in cams]]
        residual_map = {c.key: (0.0, 0.0, dz_vals[c.key - 1], 0.0) for c in cams}

        with (
            patch.object(ah_mod, "_build_tie_point_graph_and_islands", return_value=(islands, strong_adj)),
            patch.object(ah_mod, "_compute_camera_gps_residual_vec", side_effect=lambda _ch, c: residual_map[c.key]),
            redirect_stdout(io.StringIO()),
        ):
            res = ah_mod.classify_and_quarantine_islands(chunk, reoptimize=False)

        self.assertIn(8, res["gps_only_disabled_keys"])
        self.assertNotIn(8, res["quarantined_keys"])
        self.assertIsNotNone(cams[7].transform)
        self.assertTrue(cams[7].enabled)
        self.assertFalse(cams[7].reference.enabled)
        self.assertIn(10, res["quarantined_keys"])


class TestWaveFIcpBridgeAndWorker(unittest.TestCase):
    """Verify Open3D worker pose cap & Wave F ICP Bridge gates, BFS frontier waves, and mixed RS."""

    def test_icp_quality_gate_spacing_and_normal_isotropy(self) -> None:
        """Large stage0_spacing_m widens median tolerance; degenerate normal covariance is rejected."""
        base = {
            "ok": True,
            "fitness_15cm": 0.65,
            "median_15cm_m": 0.13,
            "scale": 1.0,
            "tilt_deg": 0.5,
            "normal_min_eig_ratio": 0.08,
            "overlap_fraction": 0.50,
            "T_enu": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
        }
        passed_fine, _ = ib_mod.evaluate_icp_quality_gate(base, stage0_spacing_m=0.04)
        self.assertFalse(passed_fine)
        passed_coarse, reasons_coarse = ib_mod.evaluate_icp_quality_gate(base, stage0_spacing_m=0.10)
        self.assertTrue(passed_coarse, msg=f"Unexpected failure: {reasons_coarse}")

        flat_partial = dict(base, median_15cm_m=0.04, normal_min_eig_ratio=0.001, overlap_fraction=0.20)
        ok_flat, reasons_flat = ib_mod.evaluate_icp_quality_gate(flat_partial)
        self.assertFalse(ok_flat)
        self.assertTrue(any("planar_degenerate_overlap" in r for r in reasons_flat))

    def test_mixed_sensor_island_rolling_shutter_classification(self) -> None:
        """Mixed P1 mechanical and Mavic 3 rolling sensors receive distinct per-sensor RS flags."""
        p1_sensor = SimpleNamespace(key=1, label="Zenmuse P1", make="DJI", model="Zenmuse P1", rolling_shutter=None)
        m3_sensor = SimpleNamespace(key=2, label="L2D-20c", make="Hasselblad", model="L2D-20c", rolling_shutter=None)
        cam1 = SimpleNamespace(
            sensor=p1_sensor,
            photo=SimpleNamespace(
                path="/p/p1.jpg",
                meta={"Exif/Make": "DJI", "Exif/Model": "Zenmuse P1", "DJI/RelativeAltitude": "60.0"},
            ),
        )
        cam2 = SimpleNamespace(
            sensor=m3_sensor,
            photo=SimpleNamespace(
                path="/p/m3.jpg",
                meta={"Exif/Make": "Hasselblad", "Exif/Model": "L2D-20c", "DJI/RelativeAltitude": "60.0"},
            ),
        )
        chunk = SimpleNamespace(cameras=[cam1, cam2], sensors=[p1_sensor, m3_sensor])
        fake_ms = SimpleNamespace(Shutter=SimpleNamespace(Model=SimpleNamespace(Disabled=0, Regularized=1)))
        with patch.object(ib_mod, "Metashape", fake_ms):
            ib_mod._apply_island_rolling_shutter_policy(chunk, global_rolling_shutter=True)
        self.assertEqual(p1_sensor.rolling_shutter, 0)
        self.assertEqual(m3_sensor.rolling_shutter, 1)

    def test_bridge_bfs_transitive_wave_registers_chained_island(self) -> None:
        """Transitive island overlapping island_1 (not main) registers in Wave 2 via accepted_pool."""
        base_lon, base_lat = 106.6600, 10.7700
        deg_m = 1.0 / 111320.0
        cos_lat = math.cos(math.radians(base_lat))
        subproc_targets: list[str] = []

        class _FakeMat:
            def __init__(self, rows) -> None:
                self.rows = rows

            def __mul__(self, other):
                return _FakeMat(self.rows)

        def _mk_cam(k: int, north_m: float):
            lon = base_lon + (k % 2) * 5.0 * deg_m / cos_lat
            return SimpleNamespace(
                key=k,
                enabled=True,
                transform=object(),
                photo=SimpleNamespace(path=f"/proj/cam_{k}.jpg"),
                reference=SimpleNamespace(
                    enabled=True,
                    location=SimpleNamespace(x=lon, y=base_lat + north_m * deg_m, z=50.0),
                ),
            )

        class _FakeChunk:
            def __init__(self, key: int, label: str, cams: list[SimpleNamespace]) -> None:
                self.key = key
                self.label = label
                self.cameras = list(cams)
                self.sensors = [SimpleNamespace(calibration=SimpleNamespace(k4=0.0, b1=0.0, b2=0.0))]
                self.crs = _WktCrs('GEOGCS["WGS 84"]')
                self.transform = SimpleNamespace(
                    matrix=_FakeMat([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
                )
                self.depth_maps = None
                self.point_cloud = None

            def copy(self):
                copied_cams = [
                    SimpleNamespace(
                        key=x.key,
                        enabled=x.enabled,
                        transform=x.transform,
                        photo=SimpleNamespace(path=x.photo.path),
                        reference=SimpleNamespace(enabled=x.reference.enabled, location=x.reference.location),
                    )
                    for x in self.cameras
                ]
                c = _FakeChunk(doc._next_key(), self.label + "_copy", copied_cams)
                doc.chunks.append(c)
                return c

            def remove(self, to_rm) -> None:
                rm_keys = {c.key for c in to_rm}
                self.cameras = [c for c in self.cameras if c.key not in rm_keys]

            def optimizeCameras(self, **_kw) -> None:
                pass

            def exportPointCloud(self, path: str, **_kw) -> None:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("ply\n")

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

            def mergeChunks(self, chunks: list[int], **_kw) -> None:
                by_k = {c.key: c for c in self.chunks}
                merged_cams = [c for ck in chunks for c in by_k[ck].cameras]
                self.chunks.append(_FakeChunk(self._next_key(), "Merged", merged_cams))

            def remove(self, to_rm) -> None:
                rm_set = set(to_rm)
                self.chunks = [c for c in self.chunks if c not in rm_set]

        cams = [
            _mk_cam(1, 0.0),
            _mk_cam(2, 10.0),
            _mk_cam(10, 10.0),
            _mk_cam(11, 120.0),
            _mk_cam(20, 120.0),
            _mk_cam(21, 130.0),
        ]
        doc = _FakeDoc()
        orig = _FakeChunk(1, "Production_Reconstruction", cams)
        doc.chunks.append(orig)
        doc.chunk = orig

        def _fake_subproc(cmd, **_kw):
            subproc_targets.append(os.path.basename(cmd[cmd.index("--target") + 1]))
            out_path = cmd[cmd.index("--output") + 1]
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "ok": True,
                        "T_enu": [[1, 0, 0, 0.5], [0, 1, 0, 0.0], [0, 0, 1, 0.0], [0, 0, 0, 1]],
                        "scale": 1.0,
                        "tilt_deg": 0.1,
                        "fitness_15cm": 0.70,
                        "median_15cm_m": 0.04,
                    },
                    fh,
                )
            return SimpleNamespace(returncode=0)

        fake_ms = SimpleNamespace(
            Vector=lambda v: tuple(v),
            Matrix=_FakeMat,
            DataSource=SimpleNamespace(PointCloudData=1),
            PointCloudFormat=SimpleNamespace(PointCloudFormatPLY=1),
        )
        with tempfile.TemporaryDirectory() as staging_out:
            with (
                patch.object(ib_mod, "Metashape", fake_ms),
                patch.object(ib_mod, "ensure_bridge_runtime", return_value="/fake/python.exe"),
                patch.object(ib_mod, "_build_stage0_cloud", return_value=True),
                patch.object(ib_mod, "_compute_island_utm_frame", return_value=(object(), (0.0, 0.0, 0.0), 32648)),
                patch.object(
                    ib_mod,
                    "convert_utm_transform_to_ecef",
                    return_value=([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], 0.002),
                ),
                patch.object(ib_mod.subprocess, "run", side_effect=_fake_subproc),
                redirect_stdout(io.StringIO()),
            ):
                _final, stats = ib_mod.bridge_disconnected_islands(
                    doc=doc,
                    chunk=orig,
                    audit_result={
                        "main_island": [1, 2],
                        "bridge_candidates": [[10, 11], [20, 21]],
                        "kept_distant": [],
                    },
                    output_dir=staging_out,
                )

        self.assertEqual(stats["accepted"], 2)
        self.assertEqual(stats["rejected"], 0)
        self.assertEqual(len(subproc_targets), 2)
        self.assertNotEqual(subproc_targets[0], subproc_targets[1])


class TestWaveFPipelineVerificationAndCluster(unittest.TestCase):
    """Verify CLI round-trip, crop facade guard, shutter regex, MTL/DEM verification, and cluster tools."""

    def test_monolithic_args_roundtrip_through_pipeline_arg_parser(self) -> None:
        """build_monolithic_runscript_task emits flags cleanly parsed by pipeline.build_arg_parser()."""
        with tempfile.TemporaryDirectory() as net_root:
            img_dir = os.path.join(net_root, "Proj", "Photos")
            out_dir = os.path.join(net_root, "Result", "Proj")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            with patch.object(orch_mod, "Metashape", None):
                task, _script, _sf = orch_mod.build_monolithic_runscript_task(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="Proj",
                    network_path=net_root,
                    ortho_resolution=None,
                    photo_layout="multiplane",
                    reference_columns="nyxz",
                    reference_delimiter=",",
                    reference_crs="EPSG::4326",
                    reference_skip_rows=2,
                    create_markers=True,
                    source_rel="Proj/Photos",
                    multi_chunk="auto",
                    chunk_align_method="point",
                    ground_cell_size=0.5,
                    ground_max_angle=12.0,
                    ground_max_distance=0.4,
                )
            tokens = shlex.split(task.args.replace("\\", "/"), posix=True)
            parsed = pipe_mod.build_arg_parser().parse_args(tokens)
            self.assertIsNone(parsed.ortho_resolution)
            self.assertTrue(parsed.create_markers)
            self.assertEqual(parsed.source_rel, "Proj/Photos")
            self.assertEqual(parsed.photo_layout, "multiplane")
            self.assertEqual(parsed.reference_columns, "nyxz")
            self.assertEqual(parsed.reference_delimiter, ",")
            self.assertEqual(parsed.reference_crs, "EPSG::4326")
            self.assertEqual(parsed.reference_skip_rows, 2)
            self.assertEqual(parsed.multi_chunk, "auto")
            self.assertEqual(parsed.chunk_align_method, "point")
            self.assertAlmostEqual(parsed.ground_cell_size, 0.5)

    def test_crop_empty_tiepoints_facade_axis_guard_and_shutter_word_boundaries(self) -> None:
        """Collinear facade with empty tie points preserves perpendicular region axis without inflating 2D XY."""
        from tests.test_crop_square import MockMatrix, MockVector

        collinear_cams = [
            SimpleNamespace(
                enabled=True,
                transform=object(),
                reference=None,
                center=MockVector([float(i * 10), 0.0, 5.0]),
            )
            for i in range(4)
        ]
        region = SimpleNamespace(
            rot=MockMatrix([[1, 0, 0], [0, 1, 0], [0, 0, 1]]),
            center=MockVector([15.0, -5.0, 5.0]),
            size=MockVector([10.0, 60.0, 20.0]),
        )
        chunk = SimpleNamespace(
            crs=None,
            transform=None,
            cameras=collinear_cams,
            tie_points=None,
            region=region,
        )
        fake_ms = SimpleNamespace(Vector=MockVector, Matrix=MockMatrix)
        with patch.object(flt_mod, "Metashape", fake_ms), redirect_stdout(io.StringIO()):
            ok = flt_mod.auto_crop_camera_gps_square(chunk, padding=0.05)
        self.assertTrue(ok)
        self.assertAlmostEqual(chunk.region.size.y, 60.0, places=3)
        self.assertLess(chunk.region.size.x, 45.0)
        self.assertAlmostEqual(chunk.region.center.y, -5.0, places=3)

        self.assertEqual(tel_mod.classify_sensor_shutter(make="Canon", model="Airfield EOS"), "mechanical")
        self.assertEqual(tel_mod.classify_sensor_shutter(make="Sony", model="Miniature A7R"), "mechanical")
        self.assertEqual(tel_mod.classify_sensor_shutter(make="Apple", model="iPhone 15 Pro"), "rolling")
        self.assertEqual(tel_mod.classify_sensor_shutter(make="DJI", model="DJI Air 3"), "rolling")

    def test_per_sensor_quality_filter_and_mtl_dem_verification(self) -> None:
        """Per-sensor quality adaptation isolates hazy sensor; MTL spaces & _ortho_dem classify cleanly."""
        hazy_cams = [
            SimpleNamespace(enabled=True, sensor=SimpleNamespace(key=1), meta={"Image/Quality": "0.35"})
            for _ in range(7)
        ]
        sharp_cams = [
            SimpleNamespace(enabled=True, sensor=SimpleNamespace(key=2), meta={"Image/Quality": "0.80"})
            for _ in range(5)
        ] + [SimpleNamespace(enabled=True, sensor=SimpleNamespace(key=2), meta={"Image/Quality": "0.40"})]
        q_chunk = SimpleNamespace(cameras=hazy_cams + sharp_cams, analyzeImages=lambda: None)
        with patch.object(flt_mod, "Metashape", None), redirect_stdout(io.StringIO()):
            disabled = flt_mod.filter_image_quality(q_chunk, min_quality=0.5)
        self.assertEqual(disabled, 1)
        self.assertTrue(all(c.enabled for c in hazy_cams))
        self.assertFalse(sharp_cams[-1].enabled)

        self.assertEqual(
            ver_mod._extract_mtl_map_kd_filename("map_Kd -s 1 1 1 -o 0 0 0 my tex/diffuse map.png"),
            "my tex/diffuse map.png",
        )
        self.assertEqual(
            ver_mod._extract_mtl_map_kd_filename("map_Kd -mm 0.1 0.9 texture.tif"),
            "texture.tif",
        )

        with tempfile.TemporaryDirectory() as d:
            dem_p = os.path.join(d, "Site_ortho_dem.tif")
            ort_p = os.path.join(d, "Site_orthomosaic.tif")
            ply_p = os.path.join(d, "Site_point_cloud.ply")
            for tp in (dem_p, ort_p):
                with open(tp, "wb") as f:
                    f.write(b"II*\x00" + b"\x00" * 1020)
            with open(ply_p, "wb") as f:
                f.write(b"ply\nformat ascii 1.0\n" + b"0 0 0\n" * 50)
            ok_pc, _ = ver_mod.verify_point_cloud(ply_p)
            self.assertTrue(ok_pc)
            rep = ver_mod.verify_project_deliverables(
                d,
                project_name="Site",
                expected_assets=["dem", "ortho", "point_cloud"],
            )
            self.assertEqual(rep["status"], "PASS", msg=f"Unexpected verification failures: {rep}")
            self.assertEqual(rep["assets_checked"]["dem"]["filename"], "Site_ortho_dem.tif")
            self.assertEqual(rep["assets_checked"]["ortho"]["filename"], "Site_orthomosaic.tif")

    def test_local_export_all_failed_raises_and_sync_state_restores_globals(self) -> None:
        """run_reconstruction raises if every local export fails; _synced_state prevents Metashape mock leak."""
        with tempfile.TemporaryDirectory() as tmp:
            img_dir = os.path.join(tmp, "imgs")
            out_dir = os.path.join(tmp, "out")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(img_dir, "A.JPG"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")

            pchunk = MagicMock()
            pchunk.cameras = [SimpleNamespace(enabled=True, transform=object(), meta={}, reference=None)]
            pchunk.model = SimpleNamespace(vertices=[1, 2], faces=[1], textures=[])
            pchunk.exportModel.side_effect = OSError("Simulated Disk Full")
            pdoc = MagicMock()
            pdoc.addChunk.return_value = pchunk
            pdoc.chunk = pchunk
            pdoc.chunks = [pchunk]
            fake_ms = SimpleNamespace(
                app=SimpleNamespace(
                    gpu_mask=0,
                    enumGPUDevices=lambda: [],
                    settings=SimpleNamespace(network_enable=False),
                ),
                Document=lambda: pdoc,
                ModelFormat=SimpleNamespace(ModelFormatGLTF=1),
                ReferencePreselectionMode=SimpleNamespace(ReferencePreselectionEstimated=1),
            )
            with (
                patch.object(pipe_mod, "Metashape", fake_ms),
                self.assertRaisesRegex(RuntimeError, "All requested local deliverable exports failed"),
                redirect_stdout(io.StringIO()),
            ):
                pipe_mod.run_reconstruction(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="FailExp",
                    align_photos=False,
                    build_depth_maps=False,
                    build_mesh=False,
                    build_texture=False,
                    build_orthomosaic=False,
                    build_point_cloud=False,
                    export_glb=True,
                    export_obj=False,
                    resume=False,
                    local_scratch_staging=False,
                    network_enable=False,
                )

        prev_orch_ms = orch_mod.Metashape
        fake_crop_ms = MagicMock()
        dummy_chunk = SimpleNamespace(cameras=[SimpleNamespace(transform=True, enabled=True)])
        with patch.object(mr, "Metashape", fake_crop_ms), redirect_stdout(io.StringIO()):
            self.assertFalse(mr.auto_crop_camera_gps_square(dummy_chunk))
        self.assertIs(orch_mod.Metashape, prev_orch_ms)

    def test_cluster_ipv6_unc_traversal_broadcast_ack_and_root_precedence(self) -> None:
        """Verify IPv6 host/port parsing, POSIX UNC traversal guard, broadcast_ack, and --root priority."""
        self.assertEqual(hw_mod.split_host_port("[fd00::1]:5900"), ("fd00::1", 5900))
        self.assertEqual(hw_mod.split_host_port("fe80::1"), ("fe80::1", 5840))
        self.assertEqual(hw_mod.format_host_port("fd00::1", 5900), "[fd00::1]:5900")

        with patch.object(sys, "platform", "linux"):
            self.assertFalse(dual_mod.is_subpath("//srv/share/../other/Proj", "//srv/share"))

        with tempfile.TemporaryDirectory() as shared_root:
            aborts_dir = os.path.join(shared_root, ".cluster", "aborts")
            os.makedirs(aborts_dir, exist_ok=True)
            all_sig = os.path.join(aborts_dir, "all.abort")
            with open(all_sig, "w", encoding="utf-8") as f:
                json.dump({"target_node": "all", "batch_id": 42, "signal_id": "sig-1"}, f)
            ok = abort_mod.acknowledge_abort_signal(
                {
                    "node_name": "NODE-A",
                    "batch_id": 42,
                    "signal_id": "sig-1",
                    "_signal_path": all_sig,
                }
            )
            self.assertTrue(ok)
            self.assertTrue(os.path.isfile(all_sig))
            self.assertTrue(os.path.isfile(os.path.join(aborts_dir, "node-a.broadcast_ack")))

            cli_root = os.path.join(shared_root, "cli_root")
            env_root = os.path.join(shared_root, "env_root")
            os.makedirs(cli_root, exist_ok=True)
            os.makedirs(env_root, exist_ok=True)
            captured_roots: list[str] = []

            def _fake_abort(**kw):
                captured_roots.append(str(kw.get("shared_root")))
                return {"success": True, "acknowledged": True, "reconnected": True, "signal_dispatched": True}

            with (
                patch.dict(os.environ, {"AUTOMETASHAPE_CLUSTER_ROOT": env_root}),
                patch.object(sys, "argv", ["cluster_abort.py", "--node", "W1", "--root", cli_root, "--no-reexec"]),
                patch.object(abort_cli, "abort_remote_worker", side_effect=_fake_abort),
                redirect_stdout(io.StringIO()),
            ):
                rc = abort_cli.main()
            self.assertEqual(rc, 0)
            self.assertEqual(captured_roots, [cli_root])


if __name__ == "__main__":
    unittest.main()
