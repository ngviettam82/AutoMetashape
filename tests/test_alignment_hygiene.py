# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for pre-alignment hygiene, TakeoffZ0 normalization, lens lock, and tie-graph quarantine."""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
source_dir = os.path.join(repo_root, "source")
if source_dir not in sys.path:
    sys.path.insert(0, source_dir)

from autometashape.cluster import orchestrator as orch_mod
from autometashape.core import alignment_hygiene as ah
from autometashape.core import filtering as filt_mod
from autometashape.core import pipeline as pipe_mod
from autometashape.core import telemetry as tel_mod


class _Vec:
    """Minimal 3D vector stub matching Metashape.Vector attribute access."""

    def __init__(self, x: float, y: float, z: float) -> None:
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)


class _MetaNoGet:
    """Stub mimicking Metashape.MetaData which supports [] indexing but lacks .get()."""

    def __init__(self, data: dict[str, object]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> object:
        return self._data[key]


class _FakeCamera:
    def __init__(
        self,
        key: int,
        label: str,
        path: str,
        lon: float,
        lat: float,
        alt: float,
        est_lon: float | None = None,
        est_lat: float | None = None,
        est_alt: float | None = None,
        aligned: bool = True,
        enabled: bool = True,
        ref_enabled: bool = True,
        meta: dict[str, object] | None = None,
    ) -> None:
        self.key = key
        self.label = label
        self.enabled = enabled
        self.transform = object() if aligned else None
        self.center = _Vec(
            est_lon if est_lon is not None else lon,
            est_lat if est_lat is not None else lat,
            est_alt if est_alt is not None else alt,
        )
        self.reference = SimpleNamespace(enabled=ref_enabled, location=_Vec(lon, lat, alt))
        self.photo = SimpleNamespace(path=path, meta=_MetaNoGet(meta or {}))


class _IdentityMatrix:
    def mulp(self, vec: _Vec) -> _Vec:
        return vec


class _IdentityCrs:
    def project(self, vec: _Vec) -> _Vec:
        return vec


class TestAlignmentHygiene(unittest.TestCase):
    """Mutation-resistant tests for Stages 1-3 alignment hygiene and lens calibration lock."""

    def test_fc9113_registered_in_camera_hardware_db(self) -> None:
        """Verify DJI Air 3S (FC9113) 1-inch rolling shutter camera is registered."""
        self.assertIn("fc9113", tel_mod.CAMERA_HARDWARE_DB)
        spec = tel_mod.CAMERA_HARDWARE_DB["fc9113"]
        self.assertAlmostEqual(spec["sensor_w_mm"], 13.2)
        self.assertAlmostEqual(spec["sensor_h_mm"], 8.8)
        self.assertEqual(spec["shutter"], "rolling")

    def test_discover_ignores_metashape_files_dir_and_deduplicates_exact_copies(self) -> None:
        """Verify *.files dirs are skipped and exact copies deduped without dropping distinct photos."""
        with tempfile.TemporaryDirectory() as tmp:
            mission = os.path.join(tmp, "Mission")
            backup = os.path.join(tmp, "Mission_Backup")
            manual = os.path.join(tmp, "Manual")
            stray_files = os.path.join(tmp, "126-2.files")
            for d in (mission, backup, manual, stray_files):
                os.makedirs(d, exist_ok=True)

            payload_a = b"A" * 5000
            payload_b = b"B" * 5000  # Same byte size & filename as payload_a, different content!

            with open(os.path.join(mission, "DJI_0001.JPG"), "wb") as f:
                f.write(payload_a)
            with open(os.path.join(backup, "DJI_0001.JPG"), "wb") as f:
                f.write(payload_a)
            with open(os.path.join(manual, "DJI_0001.JPG"), "wb") as f:
                f.write(payload_b)
            with open(os.path.join(stray_files, "thumb.jpg"), "wb") as f:
                f.write(b"C" * 1024)

            groups = tel_mod.discover_image_subfolder_groups(tmp)
            self.assertNotIn("126-2.files", groups)
            self.assertIn("Mission", groups)
            self.assertIn("Manual", groups)
            # Mission_Backup had only an exact copy of Mission/DJI_0001.JPG -> deduped cleanly
            self.assertNotIn("Mission_Backup", groups)
            all_kept = [p for paths in groups.values() for p in paths]
            self.assertEqual(len(all_kept), 2)

    def test_sanitize_camera_gps_disables_null_island_and_gross_outliers(self) -> None:
        """Verify Null-Island (0,0), NaN, and >5km GPS spikes are disabled while photos stay enabled."""
        cams = [_FakeCamera(i, f"ok_{i}", f"/p/ok_{i}.jpg", 106.60 + i * 1e-5, 10.77, 80.0) for i in range(6)]
        # Null Island
        cams.append(_FakeCamera(10, "null_cam", "/p/null.jpg", 0.0, 0.0, 0.0))
        # NaN GPS
        cams.append(_FakeCamera(11, "nan_cam", "/p/nan.jpg", float("nan"), 10.77, 80.0))
        # 15 km jump
        cams.append(_FakeCamera(12, "jump_cam", "/p/jump.jpg", 106.75, 10.77, 80.0))

        chunk = SimpleNamespace(cameras=cams)
        res = ah.sanitize_camera_gps(chunk, max_jump_m=5000.0)
        self.assertEqual(res["null_island_disabled"], 2)
        self.assertEqual(res["outlier_disabled"], 1)
        self.assertEqual(res["valid_remaining"], 6)

        for c in cams[:6]:
            self.assertTrue(c.reference.enabled)
            self.assertTrue(c.enabled)
        for c in cams[6:]:
            self.assertFalse(c.reference.enabled)
            self.assertTrue(c.enabled)  # Photo remains enabled for visual matching

    def test_normalize_flight_takeoff_z0_shifts_onto_nadir_datum(self) -> None:
        """Verify per-flight barometer TakeoffZ0 shifts onto Nadir datum and sets (5,5,15)m accuracy."""
        cams = []
        # Nadir flight: Z0 = 100 - 90 = +10m
        for i in range(4):
            cams.append(
                _FakeCamera(
                    i,
                    f"n_{i}",
                    os.path.join("/proj", "Nadir", f"n_{i}.jpg"),
                    106.6,
                    10.77,
                    100.0,
                    meta={"DJI/AbsoluteAltitude": "+100.0", "DJI/RelativeAltitude": "+90.0"},
                )
            )
        # Oblique flight: Z0 = 98 - 70 = +28m -> needs -18m shift
        for i in range(4, 8):
            cams.append(
                _FakeCamera(
                    i,
                    f"ob_{i}",
                    os.path.join("/proj", "Oblique", f"ob_{i}.jpg"),
                    106.6,
                    10.77,
                    98.0,
                    meta={"DJI/AbsoluteAltitude": "+98.0", "DJI/RelativeAltitude": "+70.0"},
                )
            )
        # Manual flight: Z0 = 20 - 25 = -5m -> needs +15m shift
        for i in range(8, 12):
            cams.append(
                _FakeCamera(
                    i,
                    f"man_{i}",
                    os.path.join("/proj", "Manual", f"man_{i}.jpg"),
                    106.6,
                    10.77,
                    20.0,
                    meta={"DJI/AbsoluteAltitude": "+20.0", "DJI/RelativeAltitude": "+25.0"},
                )
            )

        chunk = SimpleNamespace(cameras=cams, camera_location_accuracy=None)
        with patch.object(ah, "Metashape", None):
            info = ah.normalize_flight_takeoff_z0(chunk)

        self.assertFalse(info["skipped"])
        self.assertTrue(info["anchor_label"].lower().endswith("nadir"))
        self.assertAlmostEqual(info["anchor_z0"], 10.0)
        self.assertAlmostEqual(cams[0].reference.location.z, 100.0)
        self.assertAlmostEqual(cams[4].reference.location.z, 80.0)  # 98 - 18
        self.assertAlmostEqual(cams[8].reference.location.z, 35.0)  # 20 + 15
        self.assertEqual(chunk.camera_location_accuracy, (5.0, 5.0, 15.0))

    def test_normalize_takeoff_z0_separates_nested_same_leaf_folders(self) -> None:
        """Verify nested Misson/1 and manual/1 sessions are not merged by identical leaf folder name."""
        cams = [
            _FakeCamera(
                i,
                f"mis_{i}",
                os.path.join("/proj", "Misson", "1", f"m_{i}.jpg"),
                106.6,
                10.77,
                100.0,
                meta={"DJI/AbsoluteAltitude": "+100.0", "DJI/RelativeAltitude": "+80.0"},
            )
            for i in range(4)
        ] + [
            _FakeCamera(
                10 + i,
                f"man_{i}",
                os.path.join("/proj", "manual", "1", f"u_{i}.jpg"),
                106.6,
                10.77,
                40.0,
                meta={"DJI/AbsoluteAltitude": "+40.0", "DJI/RelativeAltitude": "+30.0"},
            )
            for i in range(4)
        ]
        chunk = SimpleNamespace(cameras=cams, camera_location_accuracy=None)
        with patch.object(ah, "Metashape", None):
            info = ah.normalize_flight_takeoff_z0(chunk)

        self.assertEqual(len(info["shifts"]), 2)
        # Misson/1 Z0=20m (anchor), manual/1 Z0=10m -> manual/1 shifted by +10m to 50.0m
        self.assertAlmostEqual(cams[0].reference.location.z, 100.0)
        self.assertAlmostEqual(cams[4].reference.location.z, 50.0)

    def test_normalize_flight_takeoff_z0_skips_rtk_fixed_surveys(self) -> None:
        """Verify RTK Fixed surveys (RtkFlag=50) keep native altitudes and accuracy."""
        cams = [
            _FakeCamera(
                i,
                f"rtk_{i}",
                os.path.join("/proj", "Nadir" if i < 3 else "Oblique", f"r_{i}.jpg"),
                106.6,
                10.77,
                100.0 + i * 5.0,
                meta={
                    "DJI/RtkFlag": "50",
                    "DJI/AbsoluteAltitude": str(100.0 + i * 5.0),
                    "DJI/RelativeAltitude": "80.0",
                },
            )
            for i in range(6)
        ]
        chunk = SimpleNamespace(cameras=cams, camera_location_accuracy=(0.02, 0.02, 0.05))
        info = ah.normalize_flight_takeoff_z0(chunk)
        self.assertTrue(info["skipped"])
        self.assertEqual(info["reason"], "rtk_fixed")
        self.assertAlmostEqual(cams[5].reference.location.z, 125.0)
        self.assertEqual(chunk.camera_location_accuracy, (0.02, 0.02, 0.05))

    def test_lens_k4_b1_b2_and_adaptive_fitting_locked_everywhere(self) -> None:
        """Verify k4, b1, b2, and adaptive_fitting are strictly locked off across all optimization paths."""
        # 1. Pro plan & CLI parser defaults
        plan = tel_mod.derive_pro_photogrammetry_plan({"has_gps": True, "relief_ratio": 0.1, "shutter": "rolling"})
        self.assertFalse(plan["adaptive_fitting"])

        args = pipe_mod.build_arg_parser().parse_args(["--images", "dummy"])
        self.assertFalse(args.adaptive_fitting)
        self.assertTrue(args.alignment_hygiene)
        self.assertTrue(args.icp_bridge)

        # 2. Local gradual selection (filtering.py)
        sensor = SimpleNamespace(calibration=SimpleNamespace(k4=-18.88, b1=4.2, b2=-1.7))
        opt_calls: list[dict] = []

        class _FakeFilter:
            ReprojectionError = 1
            ReconstructionUncertainty = 2
            ProjectionAccuracy = 3
            ImageCount = 4

            def __init__(self) -> None:
                self.values = [0.2, 0.3, 0.4]

            def init(self, _chunk, criterion=None) -> None:
                self.values = [0.2, 0.3, 0.4]

            def removePoints(self, _thresh) -> None:
                pass

            def selectPoints(self, _thresh) -> None:
                pass

        fake_ms = SimpleNamespace(TiePoints=SimpleNamespace(Filter=_FakeFilter))
        fake_chunk = SimpleNamespace(
            sensors=[sensor],
            tie_points=SimpleNamespace(
                points=[SimpleNamespace(valid=True) for _ in range(10)],
                removeSelectedPoints=lambda: None,
            ),
            optimizeCameras=lambda **kw: opt_calls.append(kw),
        )
        with patch.object(filt_mod, "Metashape", fake_ms):
            filt_mod.execute_usgs_gradual_selection(fake_chunk)

        self.assertEqual(len(opt_calls), 4)
        for kw in opt_calls:
            self.assertFalse(kw.get("fit_k4", True))
            self.assertFalse(kw.get("fit_b1", True))
            self.assertFalse(kw.get("fit_b2", True))
            self.assertFalse(kw.get("adaptive_fitting", True))
        self.assertEqual(sensor.calibration.k4, 0.0)
        self.assertEqual(sensor.calibration.b1, 0.0)
        self.assertEqual(sensor.calibration.b2, 0.0)

        # 3. Cluster gradual selection tasks (orchestrator.py)
        mock_ms = MagicMock()
        created_opts = []

        def _make_opt():
            obj = SimpleNamespace()
            created_opts.append(obj)
            return obj

        mock_ms.Tasks.CleanTiePoints = lambda: SimpleNamespace()
        mock_ms.Tasks.OptimizeCameras = _make_opt
        with patch.object(orch_mod, "Metashape", mock_ms):
            tasks = orch_mod.build_cluster_gradual_selection_tasks()

        self.assertEqual(len(tasks), 8)
        self.assertEqual(len(created_opts), 4)
        for o in created_opts:
            self.assertFalse(o.fit_k4)
            self.assertFalse(o.fit_b1)
            self.assertFalse(o.fit_b2)
            self.assertFalse(o.adaptive_fitting)

    def test_tie_graph_quarantines_rogue_strips_and_spikes_while_queuing_rigid_bridge_island(self) -> None:
        """Verify tie-graph partitions main block, rigid Manual bridge, folded rogue strip, tiny strip, and distant block."""
        base_lon, base_lat = 106.6000, 10.7700
        deg_m = 1.0 / 111320.0
        cams: list[_FakeCamera] = []
        projs: dict[_FakeCamera, list[SimpleNamespace]] = {}

        def _add_cam(
            key: int,
            label: str,
            dx_m: float,
            dy_m: float,
            z: float,
            dz_err: float = 0.0,
            dxy_err: float = 0.0,
        ) -> _FakeCamera:
            lon = base_lon + dx_m * deg_m / math.cos(math.radians(base_lat))
            lat = base_lat + dy_m * deg_m
            est_lat = lat + dxy_err * deg_m
            c = _FakeCamera(key, label, f"/p/{label}.jpg", lon, lat, z, lon, est_lat, z + dz_err)
            cams.append(c)
            projs[c] = []
            return c

        def _connect_clique(group: list[_FakeCamera], start_tid: int, shared: int = 20) -> int:
            tid = start_tid
            for idx in range(len(group) - 1):
                for _ in range(shared):
                    projs[group[idx]].append(SimpleNamespace(track_id=tid))
                    projs[group[idx + 1]].append(SimpleNamespace(track_id=tid))
                    tid += 1
            return tid

        # 1. Main Mission island: 40 cams over [-50..+50]m, with cam #39 having a +12m GPS spike
        main_grp = [
            _add_cam(
                i, f"mis_{i}", (i % 8) * 14.0 - 50.0, (i // 8) * 20.0 - 40.0, 90.0, dz_err=(12.0 if i == 39 else 0.3)
            )
            for i in range(40)
        ]
        # 2. Rigid overlapping Manual island: 15 cams inside main footprint, constant +9m Z shift (spread < 0.5m)
        man_grp = [
            _add_cam(100 + i, f"man_{i}", i * 2.0 - 10.0, 0.0, 25.0, dz_err=9.0 + (i % 3) * 0.1) for i in range(15)
        ]
        # 3. Folded/torn rogue Mission island: 20 cams with wild alternating -25m/+20m Z errors
        rogue_grp = [
            _add_cam(200 + i, f"rogue_{i}", i * 2.0, 10.0, 90.0, dz_err=(-25.0 if i % 2 == 0 else 20.0))
            for i in range(20)
        ]
        # 4. Tiny detached river strip: 6 cams
        tiny_grp = [_add_cam(300 + i, f"tiny_{i}", 60.0 + i, 50.0, 90.0, dz_err=1.0) for i in range(6)]
        # 5. Coherent distant compound: 35 cams 500m East with healthy GPS (< 1m)
        dist_grp = [_add_cam(400 + i, f"dist_{i}", 500.0 + i * 2.0, 0.0, 90.0, dz_err=0.5) for i in range(35)]

        tid = 1
        for grp in (main_grp, man_grp, rogue_grp, tiny_grp, dist_grp):
            tid = _connect_clique(grp, tid, shared=20)

        # Add a weak 3-track false bridge between main_grp[0] and man_grp[0] (< edge_min=15) -> must NOT fuse!
        for _ in range(3):
            projs[main_grp[0]].append(SimpleNamespace(track_id=tid))
            projs[man_grp[0]].append(SimpleNamespace(track_id=tid))
            tid += 1

        chunk = SimpleNamespace(
            cameras=cams,
            sensors=[],
            crs=_IdentityCrs(),
            transform=SimpleNamespace(matrix=_IdentityMatrix()),
            tie_points=SimpleNamespace(projections=projs),
            optimizeCameras=lambda **_kw: None,
        )

        res = ah.classify_and_quarantine_islands(chunk, edge_min=15, reoptimize=False)

        self.assertEqual(len(res["main_island"]), 39)  # 40 minus the 1 spike camera
        self.assertEqual(res["spike_keys"], [39])
        self.assertEqual(len(res["bridge_candidates"]), 1)
        self.assertEqual(sorted(res["bridge_candidates"][0]), sorted(c.key for c in man_grp))
        self.assertEqual(len(res["kept_distant"]), 1)
        self.assertEqual(sorted(res["kept_distant"][0]), sorted(c.key for c in dist_grp))

        # Verify rogue strip (20) + tiny strip (6) + main spike (1) = 27 quarantined
        self.assertEqual(len(res["quarantined_keys"]), 27)
        for c in rogue_grp + tiny_grp + [main_grp[39]]:
            self.assertIsNone(c.transform)
            self.assertFalse(c.enabled)
        # Bridge candidate & distant block remain aligned/enabled
        for c in man_grp + dist_grp:
            self.assertIsNotNone(c.transform)
            self.assertTrue(c.enabled)

    def test_find_unaligned_overlapping_groups_splits_separate_buildings(self) -> None:
        """Verify unaligned close-range cameras split into per-building spatial rescue groups."""
        base_lon, base_lat = 106.6000, 10.7700
        deg_m = 1.0 / 111320.0

        def _cam(key: int, folder: str, dx_m: float, dy_m: float, aligned: bool) -> _FakeCamera:
            lon = base_lon + dx_m * deg_m / math.cos(math.radians(base_lat))
            lat = base_lat + dy_m * deg_m
            return _FakeCamera(
                key, f"c_{key}", os.path.join("/p", folder, f"c_{key}.jpg"), lon, lat, 50.0, aligned=aligned
            )

        main_cams = [_cam(1, "Mission", -200.0, -100.0, True), _cam(2, "Mission", 200.0, 100.0, True)]
        # Scattered water edge leftovers in already-aligned Mission folder (should NOT be rescued!)
        mission_leftovers = [_cam(50 + i, "Mission", -10.0 + i, 0.0, False) for i in range(2)]
        bldg_1 = [_cam(100 + i, "Manual_A", -120.0 + i, 0.0, False) for i in range(14)]
        bldg_2 = [_cam(200 + i, "Manual_B", 120.0 + i, 0.0, False) for i in range(16)]
        far_out = [_cam(300 + i, "Far_Manual", 900.0 + i, 0.0, False) for i in range(15)]

        chunk = SimpleNamespace(cameras=main_cams + mission_leftovers + bldg_1 + bldg_2 + far_out)
        groups = ah.find_unaligned_overlapping_groups(chunk, main_keys={1, 2}, min_cams=12, cluster_radius_m=80.0)
        self.assertEqual([len(g) for g in groups], [16, 14])


if __name__ == "__main__":
    unittest.main()
