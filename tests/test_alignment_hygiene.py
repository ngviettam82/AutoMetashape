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
        sensor: object | None = None,
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
        self.sensor = sensor


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
                if criterion == self.ImageCount:
                    self.values = [1.0] + [3.0] * 19
                else:
                    self.values = [0.2, 0.3, 0.4]

            def removePoints(self, _thresh) -> None:
                pass

            def selectPoints(self, _thresh) -> None:
                pass

        fake_pts = [SimpleNamespace(valid=True) for _ in range(10)]

        def _remove_selected() -> None:
            for pt in fake_pts:
                if pt.valid:
                    pt.valid = False
                    break

        fake_ms = SimpleNamespace(TiePoints=SimpleNamespace(Filter=_FakeFilter))
        fake_chunk = SimpleNamespace(
            sensors=[sensor],
            tie_points=SimpleNamespace(
                points=fake_pts,
                removeSelectedPoints=_remove_selected,
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

    def test_normalize_takeoff_z0_flat_mixed_folder_simultaneous_drones_and_idempotency(self) -> None:
        """Verify two simultaneous drones mixed in one flat folder normalize cleanly and idempotently."""
        cams = []
        # Interleave Drone A (z0=52.6m, RA=75m) and Drone B (z0=57.2m, RA=10m) in the EXACT same flat folder!
        for i in range(6):
            cams.append(
                _FakeCamera(
                    i * 2,
                    f"flat_{i * 2}",
                    os.path.join("/proj", "Photos", f"img_{i * 2:03d}.jpg"),
                    106.6,
                    10.77,
                    127.6,
                    meta={"DJI/AbsoluteAltitude": "+127.6", "DJI/RelativeAltitude": "+75.0"},
                )
            )
            cams.append(
                _FakeCamera(
                    i * 2 + 1,
                    f"flat_{i * 2 + 1}",
                    os.path.join("/proj", "Photos", f"img_{i * 2 + 1:03d}.jpg"),
                    106.6,
                    10.77,
                    67.2,
                    meta={"DJI/AbsoluteAltitude": "+67.2", "DJI/RelativeAltitude": "+10.0"},
                )
            )
        chunk = SimpleNamespace(cameras=cams, camera_location_accuracy=None)
        with patch.object(ah, "Metashape", None):
            info1 = ah.normalize_flight_takeoff_z0(chunk)
            # Re-run a second time to verify exact idempotency (no double-shift!)
            info2 = ah.normalize_flight_takeoff_z0(chunk, anchor_z0_override=info1["anchor_z0"])

        self.assertEqual(len(info1["shifts"]), 2)
        self.assertAlmostEqual(info1["anchor_z0"], 52.6, places=2)
        self.assertAlmostEqual(info2["anchor_z0"], 52.6, places=2)
        for i in range(6):
            self.assertAlmostEqual(cams[i * 2].reference.location.z, 127.6, places=2)
            self.assertAlmostEqual(cams[i * 2 + 1].reference.location.z, 62.6, places=2)  # 52.6 + 10.0

    def test_find_unaligned_groups_flat_mixed_folder_zoom_split_and_rejects_stationary_pad(self) -> None:
        """Verify flat-folder rescue separates 1x vs 2x zoom sensors and rejects stationary landing pads."""
        base_lon, base_lat = 106.6000, 10.7700
        deg_m = 1.0 / 111320.0
        s_2x = SimpleNamespace(key=1, calibration=SimpleNamespace(f=5910.0), focal_length=6.72)
        s_1x = SimpleNamespace(key=0, calibration=SimpleNamespace(f=2970.0), focal_length=6.72)

        def _cam(key: int, dx_m: float, dy_m: float, ra: float, aligned: bool, sensor: object) -> _FakeCamera:
            lon = base_lon + dx_m * deg_m / math.cos(math.radians(base_lat))
            lat = base_lat + dy_m * deg_m
            return _FakeCamera(
                key,
                f"img_{key}",
                os.path.join("/p", "MixedFlat", f"img_{key}.jpg"),
                lon,
                lat,
                50.0 + ra,
                aligned=aligned,
                meta={"DJI/AbsoluteAltitude": str(50.0 + ra), "DJI/RelativeAltitude": str(ra)},
                sensor=sensor,
            )

        # 40 aligned 2x Mission cameras + 14 unaligned 2x edge dropouts (same zoom/alt/z0 -> 74% aligned -> skip!)
        main_2x = [_cam(i, (i % 10) * 20.0 - 100.0, (i // 10) * 20.0 - 40.0, 75.0, True, s_2x) for i in range(40)]
        drop_2x = [_cam(100 + i, -40.0 + i * 2.0, 10.0, 75.0, False, s_2x) for i in range(14)]
        # 16 unaligned 1x zoom cameras in the EXACT same flat folder, same z0=50m, even same altitude band (75m)!
        zoom_1x_orbit = [_cam(200 + i, 20.0 + i * 2.0, -10.0, 75.0, False, s_1x) for i in range(16)]
        # 18 stationary landing-pad photos (RA=0.0m, <0.5m span, different alt band -> must be rejected by motion!)
        pad_stack = [_cam(300 + i, 0.01 * i, 0.0, 0.0, False, s_1x) for i in range(18)]

        chunk = SimpleNamespace(cameras=main_2x + drop_2x + zoom_1x_orbit + pad_stack)
        groups = ah.find_unaligned_overlapping_groups(
            chunk,
            main_keys={c.key for c in main_2x},
            min_cams=12,
            cluster_radius_m=80.0,
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(groups[0]), sorted(c.key for c in zoom_1x_orbit))

    def test_tie_graph_cuts_fragile_single_edge_zoom_hinge_but_preserves_uniform_corridor(self) -> None:
        """Verify 1-edge zoom/altitude hinge is cut for ICP while same-regime corridor waist stays unified."""
        base_lon, base_lat = 106.6000, 10.7700
        deg_m = 1.0 / 111320.0
        s_2x = SimpleNamespace(key=1, calibration=SimpleNamespace(f=5910.0), focal_length=6.72)
        s_1x = SimpleNamespace(key=0, calibration=SimpleNamespace(f=2970.0), focal_length=6.72)

        def _make_cam(key: int, dx_m: float, ra: float, sensor: object) -> _FakeCamera:
            lon = base_lon + dx_m * deg_m / math.cos(math.radians(base_lat))
            return _FakeCamera(
                key,
                f"c_{key}",
                f"/p/c_{key}.jpg",
                lon,
                base_lat,
                50.0 + ra,
                meta={"DJI/AbsoluteAltitude": str(50.0 + ra), "DJI/RelativeAltitude": str(ra)},
                sensor=sensor,
            )

        def _make_dense_ring(group: list[_FakeCamera], projs: dict, start_tid: int, shared: int = 20) -> int:
            tid = start_tid
            n = len(group)
            for i in range(n):
                for step in (1, 2):
                    j = (i + step) % n
                    for _ in range(shared):
                        projs[group[i]].append(SimpleNamespace(track_id=tid))
                        projs[group[j]].append(SimpleNamespace(track_id=tid))
                        tid += 1
            return tid

        # Case A: 20 aerial 2x (75m) hinged by ONE 22-track edge to 15 Manual 1x (10m) -> MUST CUT!
        aer = [_make_cam(i, float(i * 5), 75.0, s_2x) for i in range(20)]
        man = [_make_cam(100 + i, float(i * 2), 10.0, s_1x) for i in range(15)]
        projs_a: dict[_FakeCamera, list[SimpleNamespace]] = {c: [] for c in aer + man}
        tid = _make_dense_ring(aer, projs_a, 1)
        tid = _make_dense_ring(man, projs_a, tid)
        for _ in range(22):
            projs_a[aer[0]].append(SimpleNamespace(track_id=tid))
            projs_a[man[0]].append(SimpleNamespace(track_id=tid))
            tid += 1
        pts_a = [SimpleNamespace(track_id=t, valid=True) for t in range(1, tid)]
        chunk_a = SimpleNamespace(
            cameras=aer + man,
            crs=_IdentityCrs(),
            transform=SimpleNamespace(matrix=_IdentityMatrix()),
            tie_points=SimpleNamespace(projections=projs_a, points=pts_a),
        )
        islands_a = ah.build_tie_point_islands(chunk_a, edge_min=15, cut_weak_hinges=True, min_bridge_cams=12)
        self.assertEqual([len(x) for x in islands_a], [20, 15])

        # Case B: Same-zoom same-altitude corridor waist (15 West <-> 15 East, 1 edge) -> MUST STAY UNIFIED!
        west = [_make_cam(i, float(i * 5), 75.0, s_2x) for i in range(15)]
        east = [_make_cam(100 + i, float(80 + i * 5), 75.0, s_2x) for i in range(15)]
        projs_b: dict[_FakeCamera, list[SimpleNamespace]] = {c: [] for c in west + east}
        tid = _make_dense_ring(west, projs_b, 1)
        tid = _make_dense_ring(east, projs_b, tid)
        for _ in range(22):
            projs_b[west[-1]].append(SimpleNamespace(track_id=tid))
            projs_b[east[0]].append(SimpleNamespace(track_id=tid))
            tid += 1
        pts_b = [SimpleNamespace(track_id=t, valid=True) for t in range(1, tid)]
        chunk_b = SimpleNamespace(
            cameras=west + east,
            crs=_IdentityCrs(),
            transform=SimpleNamespace(matrix=_IdentityMatrix()),
            tie_points=SimpleNamespace(projections=projs_b, points=pts_b),
        )
        islands_b = ah.build_tie_point_islands(chunk_b, edge_min=15, cut_weak_hinges=True, min_bridge_cams=12)
        self.assertEqual([len(x) for x in islands_b], [30])

    def test_main_block_neighbor_continuity_protects_smooth_drift_but_quarantines_spikes(self) -> None:
        """Verify smooth multi-battery drift (-7.6m Z / 10.3m XY) is preserved while isolated spikes quarantine."""
        base_lon, base_lat = 106.6000, 10.7700
        deg_m = 1.0 / 111320.0
        cams: list[_FakeCamera] = []
        projs: dict[_FakeCamera, list[SimpleNamespace]] = {}

        def _add(key: int, dx_m: float, dz_err: float, dxy_err: float) -> _FakeCamera:
            lon = base_lon + dx_m * deg_m / math.cos(math.radians(base_lat))
            est_lat = base_lat + dxy_err * deg_m
            c = _FakeCamera(key, f"c_{key}", f"/p/c_{key}.jpg", lon, base_lat, 80.0, lon, est_lat, 80.0 + dz_err)
            cams.append(c)
            projs[c] = []
            return c

        # 20 healthy core cameras (dZ=+0.2m, dXY=1.0m)
        core = [_add(i, float(i * 5), 0.2, 1.0) for i in range(20)]
        # 8 smoothly drifting flight-strip cameras (gradual transition to dZ=-7.6m, dXY=10.3m, neighbor diff < 2m)
        smooth_strip = [
            _add(100 + i, 100.0 + i * 5.0, -1.0 * (i + 1) if i < 7 else -7.6, 2.0 + 1.2 * i) for i in range(8)
        ]
        # 1 genuine local spike connected directly to core[5] (dZ=+12.5m vs neighbor +0.2m)
        local_spike = _add(200, 25.0, 12.5, 1.0)
        # 1 catastrophic teleport connected to smooth_strip[-1] (dZ=+28.0m)
        catastrophic = _add(201, 140.0, 28.0, 1.0)

        tid = 1
        chain = core + smooth_strip
        for i in range(len(chain) - 1):
            for _ in range(20):
                projs[chain[i]].append(SimpleNamespace(track_id=tid))
                projs[chain[i + 1]].append(SimpleNamespace(track_id=tid))
                tid += 1
        # Connect smooth_strip with multiple edges so it isn't a 1-bridge
        for i in range(len(chain) - 2):
            for _ in range(20):
                projs[chain[i]].append(SimpleNamespace(track_id=tid))
                projs[chain[i + 2]].append(SimpleNamespace(track_id=tid))
                tid += 1
        for spike_cam, anchor_cam in ((local_spike, core[5]), (catastrophic, smooth_strip[-1])):
            for _ in range(20):
                projs[spike_cam].append(SimpleNamespace(track_id=tid))
                projs[anchor_cam].append(SimpleNamespace(track_id=tid))
                tid += 1

        pts = [SimpleNamespace(track_id=t, valid=True) for t in range(1, tid)]
        chunk = SimpleNamespace(
            cameras=cams,
            sensors=[],
            crs=_IdentityCrs(),
            transform=SimpleNamespace(matrix=_IdentityMatrix()),
            tie_points=SimpleNamespace(projections=projs, points=pts),
            optimizeCameras=lambda **_kw: None,
        )
        res = ah.classify_and_quarantine_islands(chunk, edge_min=15, reoptimize=False)
        self.assertEqual(sorted(res["spike_keys"]), [200, 201])
        for c in smooth_strip:
            self.assertTrue(c.enabled)
            self.assertIsNotNone(c.transform)

    def test_catp0046_compact_manual_more_photos_does_not_hijack_wide_aerial_backbone(self) -> None:
        """Verify wider rigid aerial island (45 cams, 200x200m) beats denser compact manual island (60 cams, 38x38m)."""
        base_lon, base_lat = 106.6000, 10.7700
        deg_m = 1.0 / 111320.0
        cos_lat = math.cos(math.radians(base_lat))
        cams: list[_FakeCamera] = []
        projs: dict[_FakeCamera, list[SimpleNamespace]] = {}

        def _add(key: int, label: str, dx_m: float, dy_m: float, z: float, ra: float) -> _FakeCamera:
            lon = base_lon + dx_m * deg_m / cos_lat
            lat = base_lat + dy_m * deg_m
            c = _FakeCamera(
                key,
                label,
                f"/p/{label}.jpg",
                lon,
                lat,
                z,
                meta={"DJI/AbsoluteAltitude": str(z), "DJI/RelativeAltitude": str(ra)},
            )
            cams.append(c)
            projs[c] = []
            return c

        def _chain(group: list[_FakeCamera], start_tid: int) -> int:
            tid = start_tid
            for i in range(len(group) - 1):
                for _ in range(20):
                    projs[group[i]].append(SimpleNamespace(track_id=tid))
                    projs[group[i + 1]].append(SimpleNamespace(track_id=tid))
                    tid += 1
            return tid

        # 60 compact Manual cameras in [-19..+19] x [-19..+19] (38x38m < 40m cliff test, 4x 25m cells)
        man_grp = [_add(i, f"man_{i}", -19.0 + (i % 6) * 7.6, -19.0 + (i // 6) * 4.2, 33.0, 4.0) for i in range(60)]
        # 45 wide Aerial cameras across [-100..+100] x [-100..+100] (25-36x 25m cells, ~1.25 cams/cell)
        aer_grp = [
            _add(100 + i, f"aer_{i}", -100.0 + (i % 9) * 25.0, -100.0 + (i // 9) * 50.0, 130.0, 105.0)
            for i in range(45)
        ]

        tid = _chain(man_grp, 1)
        tid = _chain(aer_grp, tid)
        pts = [SimpleNamespace(track_id=t, valid=True) for t in range(1, tid)]
        chunk = SimpleNamespace(
            cameras=cams,
            sensors=[],
            crs=_IdentityCrs(),
            transform=SimpleNamespace(matrix=_IdentityMatrix()),
            tie_points=SimpleNamespace(projections=projs, points=pts),
            optimizeCameras=lambda **_kw: None,
        )
        res = ah.classify_and_quarantine_islands(chunk, edge_min=15, reoptimize=False)

        self.assertEqual(sorted(res["main_island"]), sorted(c.key for c in aer_grp))
        self.assertEqual(len(res["bridge_candidates"]), 1)
        self.assertEqual(sorted(res["bridge_candidates"][0]), sorted(c.key for c in man_grp))
        self.assertEqual(res["quarantined_keys"], [])

    def test_compact_building_beats_sparse_transit_strip_and_rejects_non_rigid_backbone(self) -> None:
        """Verify rigid 2D building (40 cams) beats 1D diagonal transit strip (14 cams) & exploded 200m rogue (50 cams)."""
        base_lon, base_lat = 106.6000, 10.7700
        deg_m = 1.0 / 111320.0
        cos_lat = math.cos(math.radians(base_lat))
        cams: list[_FakeCamera] = []
        projs: dict[_FakeCamera, list[SimpleNamespace]] = {}

        def _add(key: int, label: str, dx_m: float, dy_m: float, dz_err: float = 0.0) -> _FakeCamera:
            lon = base_lon + dx_m * deg_m / cos_lat
            lat = base_lat + dy_m * deg_m
            c = _FakeCamera(key, label, f"/p/{label}.jpg", lon, lat, 80.0, lon, lat, 80.0 + dz_err)
            cams.append(c)
            projs[c] = []
            return c

        def _chain(group: list[_FakeCamera], start_tid: int) -> int:
            tid = start_tid
            for i in range(len(group) - 1):
                for _ in range(20):
                    projs[group[i]].append(SimpleNamespace(track_id=tid))
                    projs[group[i + 1]].append(SimpleNamespace(track_id=tid))
                    tid += 1
            return tid

        # 1. Exploded/non-rigid 50-cam island across 200x200m (wider & more cams, but spread_dZ=20m -> MUST NOT be backbone)
        rogue_grp = [
            _add(i, f"rogue_{i}", -100.0 + (i % 10) * 22.0, -100.0 + (i // 10) * 50.0, 20.0 if i % 2 == 0 else -20.0)
            for i in range(50)
        ]
        # 2. Rigid 2D compact building (40 cams in [-20..+20] x [-20..+20], minor_std >= 6m, 4 cells)
        bldg_grp = [_add(100 + i, f"bldg_{i}", -20.0 + (i % 5) * 10.0, -20.0 + (i // 5) * 5.7, 0.2) for i in range(40)]
        # 3. Sparse 1D diagonal transit strip (14 cams over 260m, minor_std=0 < 6.0 -> collapses to 1 multi-cam cell)
        transit_grp = [_add(200 + i, f"trans_{i}", -130.0 + i * 20.0, -130.0 + i * 20.0, 0.1) for i in range(14)]

        tid = _chain(rogue_grp, 1)
        tid = _chain(bldg_grp, tid)
        tid = _chain(transit_grp, tid)
        pts = [SimpleNamespace(track_id=t, valid=True) for t in range(1, tid)]
        chunk = SimpleNamespace(
            cameras=cams,
            sensors=[],
            crs=_IdentityCrs(),
            transform=SimpleNamespace(matrix=_IdentityMatrix()),
            tie_points=SimpleNamespace(projections=projs, points=pts),
            optimizeCameras=lambda **_kw: None,
        )
        res = ah.classify_and_quarantine_islands(chunk, edge_min=15, reoptimize=False)

        self.assertEqual(sorted(res["main_island"]), sorted(c.key for c in bldg_grp))
        self.assertEqual(sorted(res["bridge_candidates"][0]), sorted(c.key for c in transit_grp))
        self.assertEqual(sorted(res["quarantined_keys"]), sorted(c.key for c in rogue_grp))

    def test_symmetric_overlap_and_unclipped_wide_unaligned_rescue(self) -> None:
        """Verify wide unaligned flight (70m+110m enclosing main) is returned 100% intact without clipping."""
        base_lon, base_lat = 106.6000, 10.7700
        deg_m = 1.0 / 111320.0
        cos_lat = math.cos(math.radians(base_lat))
        s_man = SimpleNamespace(key=0, calibration=SimpleNamespace(f=2970.0), focal_length=6.72)
        s_aer = SimpleNamespace(key=1, calibration=SimpleNamespace(f=5910.0), focal_length=6.72)

        def _cam(key: int, dx_m: float, dy_m: float, ra: float, aligned: bool, sensor: object) -> _FakeCamera:
            lon = base_lon + dx_m * deg_m / cos_lat
            lat = base_lat + dy_m * deg_m
            return _FakeCamera(
                key,
                f"c_{key}",
                f"/p/grp/c_{key}.jpg",
                lon,
                lat,
                25.0 + ra,
                aligned=aligned,
                meta={"DJI/AbsoluteAltitude": str(25.0 + ra), "DJI/RelativeAltitude": str(ra)},
                sensor=sensor,
            )

        # Pass-1 aligned compact main (20 cams in 40x40m)
        man_main = [_cam(i, -20.0 + (i % 5) * 10.0, -20.0 + (i // 5) * 13.0, 10.0, True, s_man) for i in range(20)]
        # Unaligned wide aerial survey: 60 cams across 200x200m (>70% outside main_bounds+35m, RA=70m & 110m)
        aer_wide = [
            _cam(
                100 + i,
                -100.0 + (i % 6) * 40.0,
                -100.0 + (i // 6) * 22.0,
                70.0 if i < 30 else 110.0,
                False,
                s_aer,
            )
            for i in range(60)
        ]

        chunk = SimpleNamespace(cameras=man_main + aer_wide)
        groups = ah.find_unaligned_overlapping_groups(chunk, main_keys={c.key for c in man_main}, min_cams=12)
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(groups[0]), sorted(c.key for c in aer_wide))

    def test_single_manual_or_local_cs_project_still_selects_cleanly(self) -> None:
        """Verify LOCAL_CS/no-GPS single island stays main and wide facade beats dense doorway burst."""

        class _LocalCam:
            def __init__(self, idx: int) -> None:
                self.key = idx + 1
                self.label = f"loc_{idx}"
                self.enabled = True
                self.transform = object()
                self.center = _Vec((idx % 5) * 4.5, (idx // 5) * 4.5, 5.0)
                self.reference = SimpleNamespace(enabled=False, location=None)
                self.photo = SimpleNamespace(path=f"/local/loc_{idx}.jpg", meta=_MetaNoGet({}))
                self.sensor = None

        # 1. LOCAL_CS unreferenced chunk (location=None, distinct cam.center positions)
        cams_local = []
        projs_local = {}
        for i in range(20):
            c = _LocalCam(i)
            cams_local.append(c)
            projs_local[c] = []
        tid = 1
        for i in range(len(cams_local) - 1):
            for _ in range(18):
                projs_local[cams_local[i]].append(SimpleNamespace(track_id=tid))
                projs_local[cams_local[i + 1]].append(SimpleNamespace(track_id=tid))
                tid += 1
        local_chunk = SimpleNamespace(
            cameras=cams_local,
            sensors=[],
            crs=SimpleNamespace(wkt='LOCAL_CS["Local"]'),
            transform=SimpleNamespace(matrix=_IdentityMatrix()),
            tie_points=SimpleNamespace(
                projections=projs_local,
                points=[SimpleNamespace(track_id=t, valid=True) for t in range(1, tid)],
            ),
            optimizeCameras=lambda **_kw: None,
        )
        res_local = ah.classify_and_quarantine_islands(local_chunk, edge_min=15, reoptimize=False)
        self.assertEqual(len(res_local["main_island"]), 20)
        self.assertEqual(res_local["quarantined_keys"], [])

        # 2. Wide 24m facade (100 cams) vs 4m doorway burst (120 cams) inside same 25m cell -> facade wins!
        base_lon, base_lat = 106.6000, 10.7700
        deg_m = 1.0 / 111320.0
        cos_lat = math.cos(math.radians(base_lat))
        facade = [
            _FakeCamera(
                i,
                f"f_{i}",
                f"/f/f_{i}.jpg",
                base_lon + (1.0 + (i % 10) * 2.5) * deg_m / cos_lat,
                base_lat + (1.0 + (i // 10) * 1.5) * deg_m,
                20.0,
            )
            for i in range(100)
        ]
        doorway = [
            _FakeCamera(
                200 + i,
                f"d_{i}",
                f"/d/d_{i}.jpg",
                base_lon + (10.5 + (i % 12) * 0.3) * deg_m / cos_lat,
                base_lat + (10.5 + (i // 12) * 0.2) * deg_m,
                20.0,
            )
            for i in range(120)
        ]
        s_fac = ah.compute_block_backbone_score(facade)[0]
        s_door = ah.compute_block_backbone_score(doorway)[0]
        self.assertGreater(s_fac, s_door)


if __name__ == "__main__":
    unittest.main()
