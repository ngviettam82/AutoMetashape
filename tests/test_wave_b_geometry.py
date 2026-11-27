# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Adversarial Wave B tests for CRS/units, AGL/MSL physics, corridors, ICP worker, and filtering."""

from __future__ import annotations

import json
import math
import os
import random
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

_SOURCE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "source"))
if _SOURCE_ROOT not in sys.path:
    sys.path.insert(0, _SOURCE_ROOT)

from autometashape.core import alignment_hygiene as ah
from autometashape.core import crs_utils as cu
from autometashape.core import filtering as flt
from autometashape.core import icp_bridge as ib
from autometashape.core.telemetry import compute_utm_epsg, is_projected_crs

_ICP_WORKER_PATH = os.path.join(_SOURCE_ROOT, "autometashape", "core", "icp_worker.py")


class _Vec3:
    """Lightweight 3D/4D vector stub with indexable components."""

    def __init__(self, coords: list[float] | tuple[float, ...]) -> None:
        vals = [float(v) for v in coords]
        self.x = vals[0] if len(vals) > 0 else 0.0
        self.y = vals[1] if len(vals) > 1 else 0.0
        self.z = vals[2] if len(vals) > 2 else 0.0
        self.w = vals[3] if len(vals) > 3 else 1.0

    def __getitem__(self, idx: int) -> float:
        return (self.x, self.y, self.z, self.w)[idx]

    def __add__(self, other: _Vec3) -> _Vec3:
        return _Vec3([self.x + other.x, self.y + other.y, self.z + other.z])

    def __sub__(self, other: _Vec3) -> _Vec3:
        return _Vec3([self.x - other.x, self.y - other.y, self.z - other.z])

    def __mul__(self, other: object) -> object:
        if isinstance(other, _Vec3):
            return self.x * other.x + self.y * other.y + self.z * other.z
        fac = float(other)
        return _Vec3([self.x * fac, self.y * fac, self.z * fac])

    def norm(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def normalized(self) -> _Vec3:
        n = self.norm()
        return _Vec3([self.x / n, self.y / n, self.z / n]) if n > 0.0 else _Vec3([0.0, 0.0, 0.0])


class _Mat:
    """Lightweight 3x3 / 4x4 matrix stub compatible with Metashape.Matrix API."""

    def __init__(self, rows: list[list[float]] | None = None) -> None:
        if rows is None:
            self._m = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
        else:
            self._m = [list(r) for r in rows]

    def mulp(self, v: _Vec3) -> _Vec3:
        if len(self._m) == 4:
            x = self._m[0][0] * v.x + self._m[0][1] * v.y + self._m[0][2] * v.z + self._m[0][3]
            y = self._m[1][0] * v.x + self._m[1][1] * v.y + self._m[1][2] * v.z + self._m[1][3]
            z = self._m[2][0] * v.x + self._m[2][1] * v.y + self._m[2][2] * v.z + self._m[2][3]
            return _Vec3([x, y, z])
        return self.__mul__(v)

    def __add__(self, other: _Mat) -> _Mat:
        n = len(self._m)
        return _Mat([[self._m[i][j] + other._m[i][j] for j in range(n)] for i in range(n)])

    def __mul__(self, other: object) -> object:
        if isinstance(other, _Vec3):
            x = self._m[0][0] * other.x + self._m[0][1] * other.y + self._m[0][2] * other.z
            y = self._m[1][0] * other.x + self._m[1][1] * other.y + self._m[1][2] * other.z
            z = self._m[2][0] * other.x + self._m[2][1] * other.y + self._m[2][2] * other.z
            return _Vec3([x, y, z])
        if isinstance(other, _Mat):
            n = len(self._m)
            out = [[sum(self._m[i][k] * other._m[k][j] for k in range(n)) for j in range(n)] for i in range(n)]
            return _Mat(out)
        raise TypeError(type(other))

    def det(self) -> float:
        m = self._m
        if len(m) == 3:
            return (
                m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
            )
        return 1.0

    def t(self) -> _Mat:
        n = len(self._m)
        return _Mat([[self._m[j][i] for j in range(n)] for i in range(n)])

    def inv(self) -> _Mat:
        n = len(self._m)
        aug = [list(self._m[i]) + [1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
        for col in range(n):
            piv = max(range(col, n), key=lambda r: abs(aug[r][col]))
            aug[col], aug[piv] = aug[piv], aug[col]
            div = aug[col][col] or 1.0
            aug[col] = [v / div for v in aug[col]]
            for r in range(n):
                if r != col and aug[r][col] != 0.0:
                    fac = aug[r][col]
                    aug[r] = [aug[r][j] - fac * aug[col][j] for j in range(2 * n)]
        return _Mat([row[n:] for row in aug])

    def scale(self) -> float:
        col0 = math.sqrt(self._m[0][0] ** 2 + self._m[1][0] ** 2 + self._m[2][0] ** 2)
        return col0 if col0 > 0.0 else 1.0

    def rotation(self) -> _Mat:
        s = self.scale()
        return _Mat([[self._m[i][j] / s for j in range(3)] for i in range(3)])


class _ProjsByCam:
    """Metashape-compatible TiePoints.projections adapter indexed by camera objects."""

    def __init__(self, by_key: dict[int, list[SimpleNamespace]]) -> None:
        self._by_key = by_key

    def __getitem__(self, cam: Any) -> list[SimpleNamespace]:
        return self._by_key.get(cam.key, [])


def _identity_crs_wkt(wkt: str, projcs: str | None = None, name: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        wkt=wkt,
        projcs=projcs,
        name=name,
        project=lambda v: _Vec3([v.x, v.y, v.z]),
        unproject=lambda v: _Vec3([v[0], v[1], v[2]]),
    )


def _write_ply(path: str, pts: list[tuple[float, float, float]]) -> None:
    """Write ASCII PLY point cloud for real Open3D worker tests."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            f"ply\nformat ascii 1.0\nelement vertex {len(pts)}\n"
            "property float x\nproperty float y\nproperty float z\nend_header\n"
        )
        for px, py, pz in pts:
            f.write(f"{px:.5f} {py:.5f} {pz:.5f}\n")


def _build_building_shell_pts(seed: int = 7, x_offset: float = 0.0) -> list[tuple[float, float, float]]:
    """Create asymmetric 3D walls + pitched roof + bay cloud with rich multi-axis normals."""
    rng = random.Random(seed)
    pts: list[tuple[float, float, float]] = []
    # 4 vertical exterior walls spanning [0..14] x [0..12] x [0..8]
    for _ in range(2000):
        pts.append((0.0 + x_offset, rng.uniform(0.0, 12.0), rng.uniform(0.0, 8.0)))
        pts.append((14.0 + x_offset, rng.uniform(0.0, 12.0), rng.uniform(0.0, 8.0)))
        pts.append((rng.uniform(0.0, 14.0) + x_offset, 0.0, rng.uniform(0.0, 8.0)))
        pts.append((rng.uniform(0.0, 14.0) + x_offset, 12.0, rng.uniform(0.0, 8.0)))
    # Pitched gable roof
    for _ in range(3000):
        rx = rng.uniform(0.0, 14.0) + x_offset
        ry = rng.uniform(0.0, 12.0)
        rz = 8.0 + 3.0 * (1.0 - abs(ry - 6.0) / 6.0)
        pts.append((rx, ry, rz))
    # Asymmetric protruding wing bay [14..17] x [2..6] x [1..6]
    for _ in range(1200):
        bz = rng.uniform(1.0, 6.0)
        pts.append((17.0 + x_offset, rng.uniform(2.0, 6.0), bz))
        pts.append((rng.uniform(14.0, 17.0) + x_offset, 2.0, bz))
        pts.append((rng.uniform(14.0, 17.0) + x_offset, 6.0, bz))
    return pts


class TestWaveBGeometry(unittest.TestCase):
    """Comprehensive adversarial test suite for Wave B geometry, CRS, ICP, and cropping fixes."""

    # ------------------------------------------------------------------
    # 1. CRS & Linear Unit Conversion (G-C14, G-H13, G-H15)
    # ------------------------------------------------------------------

    def test_crs_classification_units_and_polar_ups_zones(self) -> None:
        wkt_local = 'LOCAL_CS["Local Coordinates",UNIT["metre",1]]'
        wkt_eng2 = 'ENGCRS["Plant Frame",EDATUM["Origin"],CS[Cartesian,3],LENGTHUNIT["foot",0.3048]]'
        wkt_sp_us = (
            'PROJCS["NAD83 / California zone 3 (ftUS)",'
            'GEOGCS["NAD83",UNIT["degree",0.0174532925199433]],'
            'UNIT["US survey foot",0.3048006096012192]]'
        )
        wkt_bound = (
            'BOUNDCRS[SOURCECRS[PROJCRS["StatePlane",'
            'BASEGEOGCRS["NAD83",ANGLEUNIT["degree",0.0174532925199433]],'
            'CONVERSION["SP"],CS[Cartesian,2],LENGTHUNIT["FOOT_US"]]],'
            'TARGETCRS[GEOGCRS["WGS 84"]]]'
        )
        wkt_comp_vert_m = (
            'COMPOUNDCRS["SP + NAVD88",PROJCRS["SP",BASEGEOGCRS["NAD83",ANGLEUNIT["degree",0.01745329]],'
            'CS[Cartesian,2],LENGTHUNIT["foot",0.3048]],VERTCRS["NAVD88",LENGTHUNIT["metre",1]]]'
        )
        wkt_geo = 'GEOGCRS["WGS 84",ANGLEUNIT["degree",0.0174532925199433]]'

        self.assertEqual(cu.classify_crs(_identity_crs_wkt(wkt_local)), "local")
        self.assertEqual(cu.classify_crs(_identity_crs_wkt(wkt_eng2)), "local")
        self.assertEqual(cu.classify_crs(_identity_crs_wkt(wkt_sp_us)), "projected")
        self.assertEqual(cu.classify_crs(_identity_crs_wkt(wkt_bound)), "projected")
        self.assertEqual(cu.classify_crs(_identity_crs_wkt(wkt_geo)), "geographic")
        self.assertFalse(is_projected_crs(_identity_crs_wkt(wkt_eng2)))
        self.assertTrue(is_projected_crs(_identity_crs_wkt(wkt_comp_vert_m)))

        self.assertAlmostEqual(cu.crs_linear_unit_to_meters(_identity_crs_wkt(wkt_sp_us)), 1200.0 / 3937.0, places=8)
        self.assertAlmostEqual(cu.crs_linear_unit_to_meters(_identity_crs_wkt(wkt_bound)), 1200.0 / 3937.0, places=8)
        self.assertAlmostEqual(cu.crs_linear_unit_to_meters(_identity_crs_wkt(wkt_comp_vert_m)), 0.3048, places=6)
        self.assertAlmostEqual(cu.crs_linear_unit_to_meters(_identity_crs_wkt(wkt_eng2)), 0.3048, places=6)

        self.assertEqual(cu.compute_metric_epsg(10.0, 86.0), 32661)
        self.assertEqual(cu.compute_metric_epsg(10.0, -85.0), 32761)
        self.assertEqual(compute_utm_epsg(10.0, 86.0), 32661)
        self.assertEqual(compute_utm_epsg(106.7, 10.8), 32648)

    def test_us_survey_feet_sanitize_and_residual_scaling(self) -> None:
        ft_to_m = 1200.0 / 3937.0
        wkt_sp_us = (
            'PROJCS["NAD83 / StatePlane (ftUS)",'
            'GEOGCS["NAD83",UNIT["degree",0.0174532925199433]],'
            'UNIT["US survey foot",0.3048006096012192]]'
        )
        crs = _identity_crs_wkt(wkt_sp_us)
        # 4 cameras separated by 3500 ft (~1066m <= 5000m) + 1 cam 12000 ft (~3657m <= 5000m) + 1 cam 30000 ft (>5000m)
        locs_ft = [
            (0.0, 0.0, 300.0),
            (3500.0, 0.0, 300.0),
            (3500.0, 3500.0, 300.0),
            (0.0, 3500.0, 300.0),
            (12000.0, 0.0, 300.0),
            (30000.0, 30000.0, 300.0),
        ]
        cams = []
        for idx, (fx, fy, fz) in enumerate(locs_ft):
            cams.append(
                SimpleNamespace(
                    key=idx,
                    enabled=True,
                    transform=_Mat(),
                    center=_Vec3([fx, fy, fz + 1.0 / ft_to_m]),
                    reference=SimpleNamespace(enabled=True, location=_Vec3([fx, fy, fz])),
                )
            )
        chunk = SimpleNamespace(crs=crs, transform=SimpleNamespace(matrix=_Mat()), cameras=cams)
        res_san = ah.sanitize_camera_gps(chunk, max_jump_m=5000.0)
        # 12000ft (3657.6m) survives US survey foot scaling; 30000ft (~12.9km) is disabled
        self.assertEqual(res_san["outlier_disabled"], 1)
        self.assertTrue(cams[4].reference.enabled)
        self.assertFalse(cams[5].reference.enabled)

        dz_m, dxy_m = ah.compute_camera_gps_residual(chunk, cams[0])
        self.assertAlmostEqual(dz_m, 1.0, places=3)
        self.assertAlmostEqual(dxy_m, 0.0, places=3)

    # ------------------------------------------------------------------
    # 2. AGL vs MSL Altitude Separation (G-C6, G-H8)
    # ------------------------------------------------------------------

    def test_agl_vs_msl_rolling_shutter_and_hinge_discontinuity(self) -> None:
        # Non-DJI ground walkaround in highland city at 502m MSL over 500m ground tie points -> RS MUST disable
        ground_pts = [
            SimpleNamespace(valid=True, coord=_Vec3([float(i % 5), float(i // 5), 500.0, 1.0]))
            for i in range(30)
        ]
        msl_ground_chunk = SimpleNamespace(
            crs=_identity_crs_wkt('GEOGCRS["WGS 84"]'),
            transform=SimpleNamespace(matrix=_Mat()),
            tie_points=SimpleNamespace(points=ground_pts),
            cameras=[
                SimpleNamespace(
                    photo=SimpleNamespace(meta={}),
                    reference=SimpleNamespace(enabled=True, location=_Vec3([106.7, 10.8, 502.0])),
                )
                for _ in range(10)
            ],
        )
        # High-altitude non-DJI flight at 570m MSL over 500m ground -> 70m AGL >= 25m -> RS preserved
        msl_aerial_chunk = SimpleNamespace(
            crs=_identity_crs_wkt('GEOGCRS["WGS 84"]'),
            transform=SimpleNamespace(matrix=_Mat()),
            tie_points=SimpleNamespace(points=ground_pts),
            cameras=[
                SimpleNamespace(
                    photo=SimpleNamespace(meta={}),
                    reference=SimpleNamespace(enabled=True, location=_Vec3([106.7, 10.8, 570.0])),
                )
                for _ in range(10)
            ],
        )
        fake_vec_ms = SimpleNamespace(Vector=lambda v: _Vec3(v))
        with patch.object(ib, "Metashape", fake_vec_ms):
            self.assertTrue(ib._should_disable_island_rolling_shutter(msl_ground_chunk, True))
            self.assertFalse(ib._should_disable_island_rolling_shutter(msl_aerial_chunk, True))

        # Hinge discontinuity must NOT compare DJI 50m AGL against non-DJI 550m MSL
        s_shared = SimpleNamespace(key=0, width=4000, focal_length=24.0, calibration=SimpleNamespace(f=3000.0))
        by_key = {}
        side_a = []
        side_b = []
        for i in range(12):
            c_a = SimpleNamespace(
                key=i,
                sensor=s_shared,
                photo=SimpleNamespace(meta={"DJI/RelativeAltitude": "50.0"}),
                reference=SimpleNamespace(enabled=True, location=_Vec3([0.0, 0.0, 550.0])),
                transform=None,
                enabled=True,
            )
            c_b = SimpleNamespace(
                key=100 + i,
                sensor=s_shared,
                photo=SimpleNamespace(meta={}),
                reference=SimpleNamespace(enabled=True, location=_Vec3([0.0, 0.0, 550.0])),
                transform=None,
                enabled=True,
            )
            by_key[c_a.key] = c_a
            by_key[c_b.key] = c_b
            side_a.append(c_a.key)
            side_b.append(c_b.key)
        dummy_chunk = SimpleNamespace(crs=None, transform=None)
        self.assertIsNone(ah._side_has_regime_discontinuity(dummy_chunk, by_key, side_a, side_b, bridge_weight=40))
        self.assertNotEqual(ah._camera_physical_band(by_key[0]), ah._camera_physical_band(by_key[100]))

    # ------------------------------------------------------------------
    # 3. 1-Camera Hijack, RTK Denominator & Multi-Elevation Launch (G-C7, G-H6)
    # ------------------------------------------------------------------

    def test_rtk_denominator_1cam_hijack_and_multi_elevation_launch(self) -> None:
        # 10 RTK photos + 200 non-RTK photos MUST NOT trigger RTK survey shortcut
        mixed_cams = [
            SimpleNamespace(
                enabled=True,
                photo=SimpleNamespace(meta={"DJI/RtkFlag": "50"}),
                reference=SimpleNamespace(enabled=True, accuracy=_Vec3([0.02, 0.02, 0.03])),
            )
            for _ in range(10)
        ] + [
            SimpleNamespace(
                enabled=True,
                photo=SimpleNamespace(meta={}),
                reference=SimpleNamespace(enabled=True, accuracy=None),
            )
            for _ in range(200)
        ]
        self.assertFalse(ah._is_rtk_fixed_survey(mixed_cams))

        # 1 stray camera with RA=30m, z0=35m cannot hijack 20 low-altitude cameras at z0=10m
        cams_hijack = []
        for i in range(20):
            cams_hijack.append(
                SimpleNamespace(
                    enabled=True,
                    photo=SimpleNamespace(
                        path=f"/grid/img_{i}.jpg",
                        meta={"DJI/RelativeAltitude": "15.0", "DJI/AbsoluteAltitude": "25.0"},
                    ),
                    reference=SimpleNamespace(
                        enabled=True, location=_Vec3([106.7 + (i % 5) * 0.0005, 10.8 + (i // 5) * 0.0005, 25.0])
                    ),
                )
            )
        cams_hijack.append(
            SimpleNamespace(
                enabled=True,
                photo=SimpleNamespace(
                    path="/glitch/img_99.jpg",
                    meta={"DJI/RelativeAltitude": "30.0", "DJI/AbsoluteAltitude": "65.0"},
                ),
                reference=SimpleNamespace(enabled=True, location=_Vec3([106.7, 10.8, 65.0])),
            )
        )
        res_hijack = ah.normalize_flight_takeoff_z0(SimpleNamespace(crs=None, cameras=cams_hijack))
        self.assertAlmostEqual(res_hijack["anchor_z0"], 10.0, places=2)

        # Rooftop launch 100m higher (|dz0| = 100m > 60m) MUST NOT be shifted down into valley ground
        cams_cliff = []
        for i in range(20):
            cams_cliff.append(
                SimpleNamespace(
                    enabled=True,
                    photo=SimpleNamespace(
                        path=f"/valley/v_{i}.jpg",
                        meta={"DJI/RelativeAltitude": "50.0", "DJI/AbsoluteAltitude": "60.0"},
                    ),
                    reference=SimpleNamespace(
                        enabled=True, location=_Vec3([106.7 + (i % 5) * 0.0005, 10.8 + (i // 5) * 0.0005, 60.0])
                    ),
                )
            )
        for j in range(10):
            cams_cliff.append(
                SimpleNamespace(
                    enabled=True,
                    photo=SimpleNamespace(
                        path=f"/cliff/c_{j}.jpg",
                        meta={"DJI/RelativeAltitude": "40.0", "DJI/AbsoluteAltitude": "150.0"},
                    ),
                    reference=SimpleNamespace(enabled=True, location=_Vec3([106.701 + j * 0.0002, 10.8, 150.0])),
                )
            )
        ah.normalize_flight_takeoff_z0(SimpleNamespace(crs=None, cameras=cams_cliff))
        self.assertAlmostEqual(cams_cliff[-1].reference.location.z, 150.0, places=2)

    # ------------------------------------------------------------------
    # 4. Long Corridors & Smooth Barometer Rigidity (G-C8, G-C9, G-C12)
    # ------------------------------------------------------------------

    def test_corridor_no_amputation_scores_higher_and_smooth_barometer_rigid(self) -> None:
        utm_crs = _identity_crs_wkt('PROJCS["WGS 84 / UTM zone 48N",UNIT["metre",1]]', name="UTM48N")
        # 15 km highway corridor: 300 cameras spaced 50m along X
        corridor_cams = [
            SimpleNamespace(
                key=i,
                enabled=True,
                transform=_Mat(),
                center=_Vec3([float(i * 50), 0.0, 80.0]),
                reference=SimpleNamespace(enabled=True, location=_Vec3([float(i * 50), 0.0, 80.0])),
            )
            for i in range(300)
        ]
        corridor_chunk = SimpleNamespace(crs=utm_crs, cameras=corridor_cams)
        san = ah.sanitize_camera_gps(corridor_chunk, max_jump_m=5000.0)
        self.assertEqual(san["outlier_disabled"], 0)
        self.assertEqual(san["valid_remaining"], 300)

        # 1.5 km straight corridor (60 cams, minor_std=0) beats 30m compact orbit (40 cams)
        corr_1500 = corridor_cams[:60]
        compact_orbit = [
            SimpleNamespace(
                key=1000 + i,
                enabled=True,
                transform=_Mat(),
                center=_Vec3([12.0 * math.cos(2 * math.pi * i / 40), 12.0 * math.sin(2 * math.pi * i / 40), 15.0]),
                reference=SimpleNamespace(
                    enabled=True,
                    location=_Vec3(
                        [12.0 * math.cos(2 * math.pi * i / 40), 12.0 * math.sin(2 * math.pi * i / 40), 15.0]
                    ),
                ),
            )
            for i in range(40)
        ]
        sc_corr, cells_corr, _, _ = ah.compute_block_backbone_score(corr_1500, metric_crs=True)
        sc_orb, _, _, _ = ah.compute_block_backbone_score(compact_orbit, metric_crs=True)
        self.assertGreater(cells_corr, 4)
        self.assertGreater(sc_corr, sc_orb)

        # Aerial 80-cam block with smooth 7.5m barometer drift (exceeds hard 6m spread, neighbor diff < 1m)
        aerial_cams = []
        pts = []
        for idx in range(80):
            gx = (idx % 10) * 30.0
            gy = (idx // 10) * 30.0
            drift_z = 7.5 * (idx / 79.0)
            aerial_cams.append(
                SimpleNamespace(
                    key=idx,
                    label=f"aer_{idx}",
                    enabled=True,
                    transform=_Mat(),
                    center=_Vec3([gx, gy, 80.0 + drift_z]),
                    reference=SimpleNamespace(enabled=True, location=_Vec3([gx, gy, 80.0])),
                    sensor=SimpleNamespace(key=1),
                )
            )
        track_id = 0
        projs: dict[int, list[SimpleNamespace]] = {}
        # Build 2D grid edges (right and down neighbors) so smooth barometer drift has no artificial 1-bridges
        for r in range(8):
            for c in range(10):
                u = r * 10 + c
                for vr, vc in ((r, c + 1), (r + 1, c)):
                    if vr < 8 and vc < 10:
                        v = vr * 10 + vc
                        for _ in range(16):
                            pts.append(SimpleNamespace(valid=True, track_id=track_id))
                            projs.setdefault(u, []).append(SimpleNamespace(track_id=track_id))
                            projs.setdefault(v, []).append(SimpleNamespace(track_id=track_id))
                            track_id += 1

        shed_cams = [
            SimpleNamespace(
                key=200 + j,
                label=f"shed_{j}",
                enabled=True,
                transform=_Mat(),
                center=_Vec3([5.0 + (j % 4), 5.0 + (j // 4), 10.0]),
                reference=SimpleNamespace(enabled=True, location=_Vec3([5.0 + (j % 4), 5.0 + (j // 4), 10.0])),
                sensor=SimpleNamespace(key=2),
            )
            for j in range(12)
        ]
        for j in range(11):
            for _ in range(16):
                pts.append(SimpleNamespace(valid=True, track_id=track_id))
                projs.setdefault(200 + j, []).append(SimpleNamespace(track_id=track_id))
                projs.setdefault(200 + j + 1, []).append(SimpleNamespace(track_id=track_id))
                track_id += 1

        chunk = SimpleNamespace(
            crs=utm_crs,
            transform=SimpleNamespace(matrix=_Mat()),
            cameras=aerial_cams + shed_cams,
            sensors=[],
            tie_points=SimpleNamespace(points=pts, projections=_ProjsByCam(projs)),
            optimizeCameras=lambda **_kw: None,
        )
        audit = ah.classify_and_quarantine_islands(chunk, reoptimize=False)
        self.assertEqual(len(audit["main_island"]), 80)
        self.assertEqual(audit["quarantined_keys"], [])

    # ------------------------------------------------------------------
    # 5. Unreferenced Shared Scale, Transitive Overlap & LOCAL_CS Quarantine
    # ------------------------------------------------------------------

    def test_unreferenced_shared_scale_transitive_and_local_cs_quarantine(self) -> None:
        local_crs = _identity_crs_wkt('ENGCRS["Custom",EDATUM["O"],CS[Cartesian,3],LENGTHUNIT["metre",1]]')
        # Island A: 24 cams spanning 500x500; Island B: 40 cams packed into 5x5
        cams_a = [
            SimpleNamespace(
                key=i,
                label=f"wide_{i}",
                enabled=True,
                transform=_Mat(),
                center=_Vec3([(i % 6) * 100.0, (i // 6) * 125.0, 50.0]),
                reference=None,
                sensor=SimpleNamespace(key=1),
            )
            for i in range(24)
        ]
        cams_b = [
            SimpleNamespace(
                key=100 + j,
                label=f"shed_{j}",
                enabled=True,
                transform=_Mat(),
                center=_Vec3([(j % 8) * 0.7, (j // 8) * 0.7, 5.0]),
                reference=None,
                sensor=SimpleNamespace(key=2),
            )
            for j in range(40)
        ]
        pts = []
        projs: dict[int, list[SimpleNamespace]] = {}
        tid = 0
        for group in (cams_a, cams_b):
            for idx in range(len(group) - 1):
                for _ in range(16):
                    pts.append(SimpleNamespace(valid=True, track_id=tid))
                    projs.setdefault(group[idx].key, []).append(SimpleNamespace(track_id=tid))
                    projs.setdefault(group[idx + 1].key, []).append(SimpleNamespace(track_id=tid))
                    tid += 1

        chunk = SimpleNamespace(
            crs=local_crs,
            transform=SimpleNamespace(matrix=_Mat()),
            cameras=cams_a + cams_b,
            sensors=[],
            tie_points=SimpleNamespace(points=pts, projections=_ProjsByCam(projs)),
            optimizeCameras=lambda **_kw: None,
        )
        audit = ah.classify_and_quarantine_islands(chunk, reoptimize=False)
        self.assertEqual(len(audit["main_island"]), 24)
        self.assertEqual(len(audit["bridge_candidates"]), 1)

        # LOCAL_CS / ENGCRS bridge_disconnected_islands MUST quarantine bridge_candidates
        doc = SimpleNamespace(chunk=chunk, chunks=[chunk])
        _out_chunk, stats = ib.bridge_disconnected_islands(doc, chunk, audit)
        self.assertEqual(stats["reason"], "no_georef_crs")
        self.assertEqual(stats["quarantined_cameras"], 40)
        self.assertTrue(all(c.transform is None and not c.enabled for c in cams_b))

        # Transitive 1-hop overlap & p95 kept_distant check
        utm_crs = _identity_crs_wkt('PROJCS["UTM",UNIT["metre",1]]')
        c_main = [
            SimpleNamespace(
                key=i,
                label=f"m_{i}",
                enabled=True,
                transform=_Mat(),
                center=_Vec3([float((i % 5) * 20), float((i // 5) * 20), 50.0]),
                reference=SimpleNamespace(
                    enabled=True,
                    location=_Vec3([float((i % 5) * 20), float((i // 5) * 20), 50.0]),
                ),
                sensor=SimpleNamespace(key=1),
            )
            for i in range(25)
        ]
        # Island B (14 cams) overlaps Main (at x=60..200) -> bridge_candidate
        c_b = [
            SimpleNamespace(
                key=100 + i,
                label=f"b_{i}",
                enabled=True,
                transform=_Mat(),
                center=_Vec3([60.0 + i * 10.0, 20.0, 40.0]),
                reference=SimpleNamespace(enabled=True, location=_Vec3([60.0 + i * 10.0, 20.0, 40.0])),
                sensor=SimpleNamespace(key=2),
            )
            for i in range(14)
        ]
        # Island C (14 cams) at x=180..310 (outside Main's 35m margin, 36% overlap with B) -> 1-hop transitive bridge!
        c_c = [
            SimpleNamespace(
                key=200 + i,
                label=f"c_{i}",
                enabled=True,
                transform=_Mat(),
                center=_Vec3([180.0 + i * 10.0, 20.0, 40.0]),
                reference=SimpleNamespace(enabled=True, location=_Vec3([180.0 + i * 10.0, 20.0, 40.0])),
                sensor=SimpleNamespace(key=3),
            )
            for i in range(14)
        ]
        # Distant compound (30 cams) at x=5000..5200 with 1 outlier cam dZ=6.5m -> p95<=5m -> kept_distant!
        c_d = [
            SimpleNamespace(
                key=300 + i,
                label=f"d_{i}",
                enabled=True,
                transform=_Mat(),
                center=_Vec3([5000.0 + i * 5.0, 0.0, 50.0 + (6.5 if i == 29 else 0.5)]),
                reference=SimpleNamespace(enabled=True, location=_Vec3([5000.0 + i * 5.0, 0.0, 50.0])),
                sensor=SimpleNamespace(key=4),
            )
            for i in range(30)
        ]
        pts2 = []
        projs2: dict[int, list[SimpleNamespace]] = {}
        tid2 = 0
        for grp in (c_main, c_b, c_c, c_d):
            for idx in range(len(grp) - 1):
                for _ in range(16):
                    pts2.append(SimpleNamespace(valid=True, track_id=tid2))
                    projs2.setdefault(grp[idx].key, []).append(SimpleNamespace(track_id=tid2))
                    projs2.setdefault(grp[idx + 1].key, []).append(SimpleNamespace(track_id=tid2))
                    tid2 += 1
        chunk2 = SimpleNamespace(
            crs=utm_crs,
            transform=SimpleNamespace(matrix=_Mat()),
            cameras=c_main + c_b + c_c + c_d,
            sensors=[],
            tie_points=SimpleNamespace(points=pts2, projections=_ProjsByCam(projs2)),
            optimizeCameras=lambda **_kw: None,
        )
        audit2 = ah.classify_and_quarantine_islands(chunk2, reoptimize=False)
        self.assertEqual(len(audit2["bridge_candidates"]), 2)
        self.assertEqual(len(audit2["kept_distant"]), 1)
        self.assertEqual(audit2["quarantined_keys"], [])

    # ------------------------------------------------------------------
    # 6. _ICP_Rescue CRS Sync, T_enu Gate & Dynamic Promotion (G-C2, G-C3, G-H17)
    # ------------------------------------------------------------------

    def test_evaluate_gate_requires_t_enu_and_rescue_syncs_projected_crs(self) -> None:
        ok_no_flag, _ = ib.evaluate_icp_quality_gate(
            {"ok": True, "fitness_15cm": 0.7, "median_15cm_m": 0.03, "scale": 1.0, "tilt_deg": 0.5},
            require_transform=False,
        )
        self.assertTrue(ok_no_flag)
        ok_req, reasons_req = ib.evaluate_icp_quality_gate(
            {"ok": True, "fitness_15cm": 0.7, "median_15cm_m": 0.03, "scale": 1.0, "tilt_deg": 0.5},
            require_transform=True,
        )
        self.assertFalse(ok_req)
        self.assertIn("T_enu=missing", reasons_req)

        utm_crs = _identity_crs_wkt('PROJCS["WGS 84 / UTM zone 48N",UNIT["metre",1]]', name="UTM48N")
        gradual_calls: list[str] = []

        class _PromoChunk:
            def __init__(self, key: int, label: str, cameras: list[Any]) -> None:
                self.key = key
                self.label = label
                self.cameras = list(cameras)
                self.crs = utm_crs
                self.sensors = [SimpleNamespace(rolling_shutter=None, fixed=False, calibration=None)]
                self.markers: list[Any] = []
                self.point_cloud = SimpleNamespace(point_count=1000)
                self.depth_maps = None
                self.tie_points = SimpleNamespace(points=[SimpleNamespace(valid=True)])
                self.transform = SimpleNamespace(matrix=_Mat())
                self.region = None

            def copy(self) -> _PromoChunk:
                cloned = [
                    SimpleNamespace(
                        key=c.key,
                        label=c.label,
                        photo=c.photo,
                        transform=c.transform,
                        enabled=c.enabled,
                        center=c.center,
                        sensor=c.sensor,
                        reference=SimpleNamespace(
                            enabled=c.reference.enabled,
                            location=_Vec3([c.reference.location.x, c.reference.location.y, c.reference.location.z]),
                            accuracy=_Vec3([1.0, 1.0, 1.0]),
                        ),
                    )
                    for c in self.cameras
                ]
                new_c = _PromoChunk(doc._next_key(), self.label, cloned)
                doc.chunks.append(new_c)
                return new_c

            def remove(self, to_rm: list[Any]) -> None:
                rm_set = {id(c) for c in to_rm}
                self.cameras = [c for c in self.cameras if id(c) not in rm_set]

            def addPhotos(self, paths: list[str]) -> None:
                # Simulate Metashape addPhotos() placing raw WGS84 degrees onto camera references!
                for idx, p in enumerate(paths):
                    self.cameras.append(
                        SimpleNamespace(
                            key=500 + idx,
                            label=os.path.basename(p),
                            photo=SimpleNamespace(path=p, meta={"DJI/RelativeAltitude": "80.0"}),
                            transform=None,
                            enabled=True,
                            center=_Vec3([686000.0 + (idx % 6) * 50.0, 1190000.0 + (idx // 6) * 50.0, 80.0]),
                            sensor=self.sensors[0],
                            reference=SimpleNamespace(
                                enabled=True,
                                location=_Vec3([106.7, 10.8, 80.0]),
                                accuracy=_Vec3([1.0, 1.0, 1.0]),
                            ),
                        )
                    )

            def matchPhotos(self, **_kw: Any) -> None:
                pass

            def alignCameras(self, **_kw: Any) -> None:
                for c in self.cameras:
                    c.transform = _Mat()

            def optimizeCameras(self, **_kw: Any) -> None:
                pass

            def clearPointCloud(self) -> None:
                pass

            def clearDepthMaps(self) -> None:
                pass

        class _Doc:
            def __init__(self) -> None:
                self._k = 10
                self.chunks: list[_PromoChunk] = []
                self.chunk: _PromoChunk | None = None
                self.path = ""

            def _next_key(self) -> int:
                self._k += 1
                return self._k

            def addChunk(self) -> _PromoChunk:
                c = _PromoChunk(self._next_key(), "Chunk", [])
                self.chunks.append(c)
                return c

            def mergeChunks(self, chunks: list[int], **_kw: Any) -> None:
                by_k = {c.key: c for c in self.chunks}
                mc: list[Any] = []
                for ck in chunks:
                    mc.extend(by_k[ck].cameras)
                self.chunks.append(_PromoChunk(self._next_key(), "Merged", mc))

            def remove(self, rm: list[Any]) -> None:
                rm_set = set(rm)
                self.chunks = [c for c in self.chunks if c not in rm_set]

        doc = _Doc()
        main_cams = [
            SimpleNamespace(
                key=i,
                label=f"m_{i}.jpg",
                photo=SimpleNamespace(path=f"/p/m_{i}.jpg", meta={"DJI/RelativeAltitude": "15.0"}),
                transform=_Mat(),
                enabled=True,
                center=_Vec3([686000.0 + (i % 4) * 2.0, 1190000.0 + (i // 4) * 2.0, 15.0]),
                sensor=SimpleNamespace(key=1),
                reference=SimpleNamespace(
                    enabled=True,
                    location=_Vec3([686000.0 + (i % 4) * 2.0, 1190000.0 + (i // 4) * 2.0, 15.0]),
                    accuracy=_Vec3([1.0, 1.0, 1.0]),
                ),
            )
            for i in range(12)
        ]
        dist_cams = [
            SimpleNamespace(
                key=100 + i,
                label=f"d_{i}.jpg",
                photo=SimpleNamespace(path=f"/p/d_{i}.jpg", meta={"DJI/RelativeAltitude": "80.0"}),
                transform=_Mat(),
                enabled=True,
                center=_Vec3([690000.0 + (i % 6) * 30.0, 1190000.0 + (i // 6) * 30.0, 80.0]),
                sensor=SimpleNamespace(key=2),
                reference=SimpleNamespace(
                    enabled=True,
                    location=_Vec3([690000.0 + (i % 6) * 30.0, 1190000.0 + (i // 6) * 30.0, 80.0]),
                    accuracy=_Vec3([1.0, 1.0, 1.0]),
                ),
            )
            for i in range(30)
        ]
        res_cams = [
            SimpleNamespace(
                key=200 + i,
                label=f"r_{i:02d}.jpg",
                photo=SimpleNamespace(path=f"/p/r_{i:02d}.jpg", meta={"DJI/RelativeAltitude": "80.0"}),
                transform=None,
                enabled=True,
                center=_Vec3([686000.0 + (i % 6) * 50.0, 1190000.0 + (i // 6) * 50.0, 80.0]),
                sensor=SimpleNamespace(key=3),
                reference=SimpleNamespace(
                    enabled=True,
                    location=_Vec3([686000.0 + (i % 6) * 50.0, 1190000.0 + (i // 6) * 50.0, 80.0]),
                    accuracy=_Vec3([1.0, 1.0, 1.0]),
                ),
            )
            for i in range(24)
        ]
        orig = _PromoChunk(1, "Orig", main_cams + dist_cams + res_cams)
        doc.chunks.append(orig)
        doc.chunk = orig

        fake_ms = SimpleNamespace(
            Vector=lambda v: _Vec3(v),
            Matrix=_Mat,
            DataSource=SimpleNamespace(PointCloudData=1),
            PointCloudFormat=SimpleNamespace(PointCloudFormatPLY=1),
            ReferencePreselectionMode=SimpleNamespace(ReferencePreselectionSource=1),
            Shutter=SimpleNamespace(Model=SimpleNamespace(Disabled=0, Regularized=2)),
        )

        def _stage0_promo_fails(ch_arg: Any) -> bool:
            if ch_arg.label.startswith("_ICP_Rescue") or ch_arg.label.startswith("rescue_"):
                return False
            return True

        def _fake_isolate(ch_arg: Any, label_prefix: str = "", **_kw: Any) -> list[tuple[str, Any]]:
            return [(label_prefix, ch_arg)]

        def _record_gradual(ch_arg: Any) -> dict[str, Any]:
            gradual_calls.append(ch_arg.label)
            return {}

        recorded_res_xs: list[float] = []
        orig_sanitize = ib.sanitize_camera_gps

        def _spy_sanitize(ch_arg: Any, *a: Any, **kw: Any) -> dict[str, int]:
            if ch_arg.label.startswith("_ICP_Rescue"):
                recorded_res_xs.extend(c.reference.location.x for c in ch_arg.cameras)
            return orig_sanitize(ch_arg, *a, **kw)

        with (
            patch.object(ib, "Metashape", fake_ms),
            patch.object(ib, "ensure_bridge_runtime", return_value="/fake/py"),
            patch.object(ib, "_build_stage0_cloud", side_effect=_stage0_promo_fails),
            patch.object(ib, "_isolate_coherent_sub_islands", side_effect=_fake_isolate),
            patch.object(ib, "sanitize_camera_gps", side_effect=_spy_sanitize),
            patch.object(flt, "execute_usgs_gradual_selection", side_effect=_record_gradual),
        ):
            final_chunk, _stats = ib.bridge_disconnected_islands(
                doc=doc,
                chunk=orig,
                audit_result={
                    "main_island": [c.key for c in main_cams],
                    "bridge_candidates": [],
                    "kept_distant": [[c.key for c in dist_cams]],
                },
                unaligned_groups=[[c.key for c in res_cams]],
            )

        self.assertTrue(recorded_res_xs and all(x > 685000.0 for x in recorded_res_xs))
        self.assertTrue(gradual_calls)
        self.assertEqual(len(final_chunk.cameras), 42)

    # ------------------------------------------------------------------
    # 7. Walkaround & Partial-Flight Unaligned Rescue (G-C13, G-H16)
    # ------------------------------------------------------------------

    def test_walkaround_and_partial_flight_unaligned_rescue(self) -> None:
        utm_crs = _identity_crs_wkt('PROJCS["WGS 84 / UTM zone 48N",UNIT["metre",1]]', name="UTM48N")
        s_air = SimpleNamespace(key=1, model="FC9113")
        s_hand = SimpleNamespace(key=2, model="Canon")

        main_cams = [
            SimpleNamespace(
                key=i,
                enabled=True,
                transform=_Mat(),
                sensor=s_air,
                photo=SimpleNamespace(meta={"DJI/RelativeAltitude": "60.0", "DJI/AbsoluteAltitude": "70.0"}),
                reference=SimpleNamespace(enabled=True, location=_Vec3([(i % 6) * 20.0, (i // 6) * 20.0, 70.0])),
            )
            for i in range(36)
        ]
        walk_cams = [
            SimpleNamespace(
                key=200 + i,
                enabled=True,
                transform=None,
                sensor=s_hand,
                photo=SimpleNamespace(meta={"DJI/RelativeAltitude": "0.0", "DJI/AbsoluteAltitude": "10.0"}),
                reference=SimpleNamespace(enabled=True, location=_Vec3([float(i * 2), float((i % 3) * 5), 10.0])),
            )
            for i in range(16)
        ]
        pad_cams = [
            SimpleNamespace(
                key=400 + i,
                enabled=True,
                transform=None,
                sensor=SimpleNamespace(key=3, model="PadCam"),
                photo=SimpleNamespace(meta={"DJI/RelativeAltitude": "0.0", "DJI/AbsoluteAltitude": "10.0"}),
                reference=SimpleNamespace(enabled=True, location=_Vec3([95.0 + i * 0.005, 95.0, 10.0])),
            )
            for i in range(14)
        ]
        partial_main = [
            SimpleNamespace(
                key=500 + i,
                enabled=True,
                transform=_Mat(),
                sensor=s_air,
                photo=SimpleNamespace(meta={"DJI/RelativeAltitude": "60.0", "DJI/AbsoluteAltitude": "70.0"}),
                reference=SimpleNamespace(
                    enabled=True, location=_Vec3([float((i % 5) * 25), float((i // 5) * 25), 70.0])
                ),
            )
            for i in range(30)
        ]
        partial_unal = [
            SimpleNamespace(
                key=600 + i,
                enabled=True,
                transform=None,
                sensor=s_air,
                photo=SimpleNamespace(meta={"DJI/RelativeAltitude": "60.0", "DJI/AbsoluteAltitude": "70.0"}),
                reference=SimpleNamespace(enabled=True, location=_Vec3([10.0 + i * 4.0, 40.0, 70.0])),
            )
            for i in range(25)
        ]

        all_cams = main_cams + partial_main + walk_cams + pad_cams + partial_unal
        chunk = SimpleNamespace(crs=utm_crs, cameras=all_cams)
        main_keys = {c.key for c in main_cams + partial_main}
        groups = ah.find_unaligned_overlapping_groups(chunk, main_keys=main_keys)
        all_rescued = {k for grp in groups for k in grp}
        self.assertTrue({c.key for c in walk_cams}.issubset(all_rescued))
        self.assertTrue({c.key for c in partial_unal}.issubset(all_rescued))
        self.assertTrue(all(c.key not in all_rescued for c in pad_cams))

    # ------------------------------------------------------------------
    # 8. Open3D icp_worker Weighted Umeyama + Real Mid-Grid & Equal-Wing E2E
    # ------------------------------------------------------------------

    def test_real_open3d_umeyama_weights_mid_grid_and_equal_wing_seam(self) -> None:
        bridge_py = ib.ensure_bridge_runtime()
        if not bridge_py:
            self.skipTest("Portable Open3D bridge runtime not cached locally")

        # 1) Weighted Tukey umeyama_sim3 outlier resistance (executed inside bridge_py which ships numpy)
        umeyama_probe = (
            "import math, sys, numpy as np\n"
            + f"sys.path.insert(0, {_SOURCE_ROOT!r})\n"
            + "from autometashape.core.icp_worker import umeyama_sim3\n"
            "rng = np.random.default_rng(19)\n"
            "src_clean = rng.uniform(-15.0, 15.0, size=(200, 3))\n"
            "th = math.radians(3.0)\n"
            "r_true = np.array([[math.cos(th), -math.sin(th), 0.0], "
            "[math.sin(th), math.cos(th), 0.0], [0.0, 0.0, 1.0]])\n"
            "t_true = np.array([1.2, -0.8, 0.4])\n"
            "dst_clean = 1.02 * (src_clean @ r_true.T) + t_true\n"
            "src_all = np.vstack([src_clean, rng.uniform(-30.0, 30.0, size=(20, 3))])\n"
            + "dst_all = np.vstack([dst_clean, rng.uniform(80.0, 200.0, size=(20, 3))])\n"
            "weights = np.concatenate([np.ones(200), np.zeros(20)])\n"
            "s_u, _, _ = umeyama_sim3(src_all, dst_all, with_scale=True, weights=None)\n"
            "s_w, _, t_w = umeyama_sim3(src_all, dst_all, with_scale=True, weights=weights)\n"
            "assert abs(s_u - 1.02) > 0.1, s_u\n"
            "assert abs(s_w - 1.02) < 1e-4, s_w\n"
            "assert float(np.linalg.norm(t_w - t_true)) < 1e-4\n"
        )
        probe_res = subprocess.run(
            [bridge_py, "-c", umeyama_probe],
            env=ib._clean_bridge_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(probe_res.returncode, 0, msg=probe_res.stderr)

        with tempfile.TemporaryDirectory() as tmpdir:
            # 2) Exact mid-grid blind spot (+1.0, +1.0, +0.75) on building shell
            tgt_pts = _build_building_shell_pts(seed=11)
            src_pts = [(x - 1.0, y - 1.0, z - 0.75) for x, y, z in tgt_pts]

            tgt_ply = os.path.join(tmpdir, "mid_tgt.ply")
            src_ply = os.path.join(tmpdir, "mid_src.ply")
            out_mid = os.path.join(tmpdir, "mid_out.json")
            _write_ply(tgt_ply, tgt_pts)
            _write_ply(src_ply, src_pts)

            proc = subprocess.run(
                [
                    bridge_py,
                    "-s",
                    _ICP_WORKER_PATH,
                    "--target",
                    tgt_ply,
                    "--source",
                    src_ply,
                    "--output",
                    out_mid,
                    "--no-colored-refine",
                ],
                env=ib._clean_bridge_env(),
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            with open(out_mid, encoding="utf-8") as f:
                m_mid = json.load(f)
            self.assertTrue(m_mid["ok"])
            self.assertGreater(m_mid["fitness_15cm"], 0.75)
            self.assertLess(m_mid["median_15cm_m"], 0.04)
            tx, ty, tz = m_mid["translation_m"]
            err_mid = math.sqrt((tx - 1.0) ** 2 + (ty - 1.0) ** 2 + (tz - 0.75) ** 2)
            self.assertLess(err_mid, 0.12)

            # 3) Equal-size wings sharing central textured seam [20..30] with -4 deg yaw perturbation
            rng = random.Random(42)
            wing_t = _build_building_shell_pts(seed=21, x_offset=13.0)
            wing_t_flat = [(rng.uniform(0.0, 20.0), rng.uniform(0.0, 12.0), 0.0) for _ in range(3000)]
            tgt_equal = wing_t + wing_t_flat

            wing_s_seam = [p for p in wing_t if p[0] >= 20.0]
            wing_s_ext = [(rng.uniform(30.0, 50.0), rng.uniform(0.0, 12.0), 0.0) for _ in range(3000)]
            src_aligned = wing_s_seam + wing_s_ext
            px, py, pz = 25.0, 6.0, 4.0
            th = math.radians(-4.0)
            ct, st = math.cos(th), math.sin(th)
            src_perturbed = [
                (
                    (x - px) * ct - (y - py) * st + px - 0.8,
                    (x - px) * st + (y - py) * ct + py + 0.6,
                    z - 0.5,
                )
                for x, y, z in src_aligned
            ]

            eq_tgt_ply = os.path.join(tmpdir, "eq_tgt.ply")
            eq_src_ply = os.path.join(tmpdir, "eq_src.ply")
            out_eq = os.path.join(tmpdir, "eq_out.json")
            _write_ply(eq_tgt_ply, tgt_equal)
            _write_ply(eq_src_ply, src_perturbed)
            proc_eq = subprocess.run(
                [
                    bridge_py,
                    "-s",
                    _ICP_WORKER_PATH,
                    "--target",
                    eq_tgt_ply,
                    "--source",
                    eq_src_ply,
                    "--output",
                    out_eq,
                    "--no-colored-refine",
                ],
                env=ib._clean_bridge_env(),
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(proc_eq.returncode, 0, msg=proc_eq.stderr)
            with open(out_eq, encoding="utf-8") as f:
                m_eq = json.load(f)
            self.assertTrue(m_eq["ok"])
            self.assertGreater(m_eq["overlap_fitness_15cm"], 0.60)
            self.assertLess(m_eq["median_15cm_m"], 0.08)

    # ------------------------------------------------------------------
    # 9. Universal Filtering Scenes (G-C10, G-C11, G-H4, G-H5, G-H10)
    # ------------------------------------------------------------------

    def test_filtering_indoor_cliff_corridor_hazy_and_lens_locks(self) -> None:
        class _FilterCls:
            ReprojectionError = 0
            ReconstructionUncertainty = 1
            ProjectionAccuracy = 2
            ImageCount = 3

            def __init__(self) -> None:
                self.values = [2, 2, 2]

            def init(self, *_a: Any, **_kw: Any) -> None:
                pass

            def removePoints(self, *_a: Any) -> None:
                pass

            def selectPoints(self, *_a: Any) -> None:
                pass

        fake_ms = SimpleNamespace(
            Vector=lambda v: _Vec3(v),
            Matrix=_Mat,
            Camera=SimpleNamespace(Type=SimpleNamespace(Regular=0, Dummy=1)),
            TiePoints=SimpleNamespace(Filter=_FilterCls),
        )

        # 1) Indoor outward-facing camera ring (radius 2m) surrounded by +-8m room walls
        room_cams = []
        for k in range(8):
            ang = 2.0 * math.pi * k / 8.0
            cx, cy = 2.0 * math.cos(ang), 2.0 * math.sin(ang)
            rot_rows = [
                [-math.sin(ang), 0.0, math.cos(ang), cx],
                [math.cos(ang), 0.0, math.sin(ang), cy],
                [0.0, 1.0, 0.0, 1.5],
                [0.0, 0.0, 0.0, 1.0],
            ]
            room_cams.append(
                SimpleNamespace(
                    enabled=True,
                    center=_Vec3([cx, cy, 1.5]),
                    transform=_Mat(rot_rows),
                )
            )
        wall_pts = []
        for idx in range(200):
            s = -8.0 + 16.0 * (idx % 50) / 49.0
            if idx < 50:
                wx, wy = s, 8.0
            elif idx < 100:
                wx, wy = s, -8.0
            elif idx < 150:
                wx, wy = 8.0, s
            else:
                wx, wy = -8.0, s
            wall_pts.append(SimpleNamespace(valid=True, coord=_Vec3([wx, wy, 1.5, 1.0])))

        room_chunk = SimpleNamespace(
            cameras=room_cams,
            transform=SimpleNamespace(scale=1.0),
            region=SimpleNamespace(
                center=_Vec3([0.0, 0.0, 1.5]),
                size=_Vec3([30.0, 30.0, 10.0]),
                rot=_Mat([[1, 0, 0], [0, 1, 0], [0, 0, 1]]),
            ),
            tie_points=SimpleNamespace(points=wall_pts),
        )
        with patch.object(flt, "Metashape", fake_ms):
            self.assertTrue(flt.auto_crop_to_main_structure(room_chunk, padding=0.05))
        self.assertGreaterEqual(room_chunk.region.size.x, 15.0)
        self.assertGreaterEqual(room_chunk.region.size.y, 15.0)

        # 2) Vertical cliff photographed from x=0, tie points extending to x=+30m
        cliff_cams = [
            SimpleNamespace(
                enabled=True,
                transform=_Mat(),
                center=_Vec3([0.0, float(i * 10), 10.0]),
                reference=SimpleNamespace(enabled=True, location=_Vec3([106.7, 10.8 + i * 0.0001, 10.0])),
            )
            for i in range(6)
        ]
        cliff_pts = [
            SimpleNamespace(valid=True, coord=_Vec3([30.0, float(i % 50), float((i % 4) * 10), 1.0]))
            for i in range(100)
        ]
        cliff_chunk = SimpleNamespace(
            crs=_identity_crs_wkt('GEOGCRS["WGS 84"]'),
            cameras=cliff_cams,
            transform=SimpleNamespace(scale=1.0),
            region=SimpleNamespace(
                center=_Vec3([15.0, 25.0, 20.0]),
                size=_Vec3([100.0, 100.0, 50.0]),
                rot=_Mat([[1, 0, 0], [0, 1, 0], [0, 0, 1]]),
            ),
            tie_points=SimpleNamespace(points=cliff_pts),
        )
        with patch.object(flt, "Metashape", fake_ms):
            self.assertTrue(flt.auto_crop_camera_gps_square(cliff_chunk, padding=0.05))
        self.assertGreaterEqual(cliff_chunk.region.center.x + 0.5 * cliff_chunk.region.size.x, 29.5)

        # 3) 5km linear corridor in auto_crop_camera_gps_square must not amputate ends
        corr_cams = [
            SimpleNamespace(
                enabled=True,
                transform=_Mat(),
                center=_Vec3([float(i * 100), 0.0, 80.0]),
                reference=SimpleNamespace(enabled=True, location=_Vec3([float(i * 100), 0.0, 80.0])),
            )
            for i in range(51)
        ]
        corr_crop_chunk = SimpleNamespace(
            crs=_identity_crs_wkt('PROJCS["UTM",UNIT["metre",1]]'),
            cameras=corr_cams,
            transform=SimpleNamespace(scale=1.0),
            region=SimpleNamespace(
                center=_Vec3([2500.0, 0.0, 40.0]),
                size=_Vec3([6000.0, 200.0, 100.0]),
                rot=_Mat([[1, 0, 0], [0, 1, 0], [0, 0, 1]]),
            ),
            tie_points=SimpleNamespace(points=[]),
        )
        with patch.object(flt, "Metashape", fake_ms):
            self.assertTrue(flt.auto_crop_camera_gps_square(corr_crop_chunk, padding=0.0))
        self.assertGreaterEqual(max(corr_crop_chunk.region.size.x, corr_crop_chunk.region.size.y), 4990.0)

        # 4) Hazy low-contrast images adapt quality threshold instead of collapsing to 2
        hazy_cams = [
            SimpleNamespace(enabled=True, type=0, meta={"Image/Quality": f"{0.35 + (i % 6) * 0.015:.3f}"})
            for i in range(20)
        ] + [SimpleNamespace(enabled=True, type=1, meta={"Image/Quality": "0.10"})]
        hazy_chunk = SimpleNamespace(cameras=hazy_cams, analyzeImages=lambda: None)
        with patch.object(flt, "Metashape", fake_ms):
            disabled_hazy = flt.filter_image_quality(hazy_chunk, min_quality=0.5)
        self.assertEqual(disabled_hazy, 0)
        self.assertEqual(sum(1 for c in hazy_cams[:-1] if c.enabled), 20)

        # 5) execute_usgs_gradual_selection Pass 3 respects sensor.fixed and preserves Fisheye k4
        s_fixed = SimpleNamespace(fixed=True, type="Frame", calibration=SimpleNamespace(k4=0.123, b1=0.05, b2=-0.04))
        s_fish = SimpleNamespace(fixed=False, type="Fisheye", calibration=SimpleNamespace(k4=-0.25, b1=0.02, b2=0.03))
        s_frame = SimpleNamespace(fixed=False, type="Frame", calibration=SimpleNamespace(k4=0.09, b1=0.01, b2=0.01))
        grad_chunk = SimpleNamespace(
            tie_points=SimpleNamespace(
                points=[SimpleNamespace(valid=True) for _ in range(5)],
                removeSelectedPoints=lambda: None,
            ),
            sensors=[s_fixed, s_fish, s_frame],
            optimizeCameras=lambda **_kw: None,
        )
        with patch.object(flt, "Metashape", fake_ms):
            flt.execute_usgs_gradual_selection(grad_chunk)
        self.assertAlmostEqual(s_fixed.calibration.k4, 0.123, places=6)
        self.assertAlmostEqual(s_fixed.calibration.b1, 0.05, places=6)
        self.assertAlmostEqual(s_fish.calibration.k4, -0.25, places=6)
        self.assertEqual(s_fish.calibration.b1, 0.0)
        self.assertEqual(s_frame.calibration.k4, 0.0)


if __name__ == "__main__":
    unittest.main()
