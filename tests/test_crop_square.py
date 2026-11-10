# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for AutoMetashape minimum area enclosing horizontal rectangle crop algorithm."""

from __future__ import annotations

import math
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure scripts directory is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import metashape_reconstruction as mr


class MockCoord:
    """Mock for 4D homogeneous Metashape tie-point coordinate."""

    def __init__(self, x: float, y: float, z: float, w: float = 1.0) -> None:
        self.x = x
        self.y = y
        self.z = z
        self.w = w


class MockTiePoint:
    """Mock for Metashape.TiePoints.Point."""

    def __init__(self, x: float, y: float, z: float) -> None:
        self.valid = True
        self.coord = MockCoord(x, y, z, 1.0)


class MockVector:
    """Mock for Metashape.Vector supporting 3D components and arithmetic."""

    def __init__(self, items: list[float] | tuple[float, ...]) -> None:
        self.items = list(items)

    @property
    def x(self) -> float:
        return self.items[0]

    @property
    def y(self) -> float:
        return self.items[1]

    @property
    def z(self) -> float:
        return self.items[2]

    def __getitem__(self, idx: int) -> float:
        return self.items[idx]

    def __len__(self) -> int:
        return len(self.items)

    def __repr__(self) -> str:
        return f"MockVector({self.items})"


class MockMatrix:
    """Mock for Metashape.Matrix supporting 3x3 operations."""

    def __init__(self, rows: list[list[float]]) -> None:
        self.rows = [list(r) for r in rows]

    def t(self) -> MockMatrix:
        return MockMatrix([[self.rows[j][i] for j in range(3)] for i in range(3)])

    def __mul__(self, other: MockVector | MockMatrix) -> MockVector | MockMatrix:
        if isinstance(other, MockVector):
            ox, oy, oz = other.x, other.y, other.z
            rx = self.rows[0][0] * ox + self.rows[0][1] * oy + self.rows[0][2] * oz
            ry = self.rows[1][0] * ox + self.rows[1][1] * oy + self.rows[1][2] * oz
            rz = self.rows[2][0] * ox + self.rows[2][1] * oy + self.rows[2][2] * oz
            return MockVector([rx, ry, rz])
        elif isinstance(other, MockMatrix):
            res = [
                [sum(self.rows[i][k] * other.rows[k][j] for k in range(3)) for j in range(3)]
                for i in range(3)
            ]
            return MockMatrix(res)
        return NotImplemented


class TestCropSquare(unittest.TestCase):
    """Test auto_crop_camera_gps_square geometry, outlier filtering, and bounding box creation."""

    def setUp(self) -> None:
        self.mock_metashape = MagicMock()
        self.mock_metashape.Vector = MockVector
        self.mock_metashape.Matrix = MockMatrix

    def test_rejection_fewer_than_two_cameras(self) -> None:
        """Defend that crop is safely skipped when fewer than 2 cameras are aligned."""
        chunk = MagicMock()
        chunk.cameras = [MagicMock(transform=True, enabled=True)]

        with patch.object(mr, "Metashape", self.mock_metashape):
            result = mr.auto_crop_camera_gps_square(chunk)
            self.assertFalse(result)

    def test_minimum_area_square_detection_and_outlier_filtering(self) -> None:
        """Defend that minimum area horizontal enclosing square rotates correctly and prunes (0,0) GPS."""
        chunk = MagicMock()
        identity_rot = MockMatrix([
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ])
        chunk.region = MagicMock()
        chunk.region.rot = identity_rot
        chunk.region.size = MockVector([500.0, 500.0, 100.0])
        chunk.transform.scale = 1.0

        # Construct 4 cameras forming a 100m x 100m square, plus an invalid (0,0) GPS camera
        cameras = []
        coords = [
            (10.0, 10.0, 50.0, 105.8, 21.0),
            (110.0, 10.0, 50.0, 105.81, 21.0),
            (110.0, 110.0, 50.0, 105.81, 21.01),
            (10.0, 110.0, 50.0, 105.8, 21.01),
            # GPS (0, 0) outlier that must be pruned
            (0.0, 0.0, 0.0, 0.0, 0.0),
        ]
        for cx, cy, cz, gx, gy in coords:
            cam = MagicMock()
            cam.transform = True
            cam.enabled = True
            cam.center = MockVector([cx, cy, cz])
            cam.reference.location = MockVector([gx, gy, 0.0])
            cameras.append(cam)

        chunk.cameras = cameras
        chunk.tie_points.points = []

        with patch.object(mr, "Metashape", self.mock_metashape):
            result = mr.auto_crop_camera_gps_square(chunk, padding=0.0)
            self.assertTrue(result)

            # Enclosing square of a 100x100 axis-aligned box should be 100x100
            new_size = chunk.region.size
            self.assertAlmostEqual(new_size.x, 100.0, delta=1.0)
            self.assertAlmostEqual(new_size.y, 100.0, delta=1.0)
            # Center of the 10..110 square should be 60, 60
            new_center = chunk.region.center
            self.assertAlmostEqual(new_center.x, 60.0, delta=1.0)
            self.assertAlmostEqual(new_center.y, 60.0, delta=1.0)

    def test_minimum_area_square_finds_optimal_rotation(self) -> None:
        """Defend that the algorithm finds the rotated square rather than a larger axis-aligned box."""
        chunk = MagicMock()
        identity_rot = MockMatrix([
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ])
        chunk.region = MagicMock()
        chunk.region.rot = identity_rot
        chunk.region.size = MockVector([500.0, 500.0, 100.0])
        chunk.transform.scale = 1.0

        # Construct a 100m square rotated at 45 degrees
        d = 50.0 * math.sqrt(2.0)  # ~70.71m
        coords = [
            (0.0, d, 25.0),
            (d, 0.0, 25.0),
            (0.0, -d, 25.0),
            (-d, 0.0, 25.0),
        ]
        cameras = []
        for cx, cy, cz in coords:
            cam = MagicMock()
            cam.transform = True
            cam.enabled = True
            cam.center = MockVector([cx, cy, cz])
            cam.reference.location = MockVector([105.8, 21.0, 0.0])
            cameras.append(cam)

        chunk.cameras = cameras
        chunk.tie_points.points = []

        with patch.object(mr, "Metashape", self.mock_metashape):
            result = mr.auto_crop_camera_gps_square(chunk, padding=0.0)
            self.assertTrue(result)

            # An axis-aligned box would be 2*d ~ 141.42m
            # The optimal rotated square is 100.0m
            new_size = chunk.region.size
            self.assertAlmostEqual(new_size.x, 100.0, delta=2.0)
            self.assertAlmostEqual(new_size.y, 100.0, delta=2.0)

    def test_minimum_area_rectangle_arbitrary_shape(self) -> None:
        """Defend Option A minimum-area rectangle with independent width and length for arbitrary survey shapes."""
        chunk = MagicMock()
        identity_rot = MockMatrix([
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ])
        chunk.region = MagicMock()
        chunk.region.rot = identity_rot
        chunk.region.size = MockVector([500.0, 500.0, 100.0])
        chunk.transform.scale = 1.0

        # Construct a 120m x 40m rectangular flight pattern rotated at 30 degrees
        angle = math.radians(30.0)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        rect_corners = [
            (-60.0, -20.0),
            (60.0, -20.0),
            (60.0, 20.0),
            (-60.0, 20.0),
        ]
        cameras = []
        for rx, ry in rect_corners:
            cx = rx * cos_a - ry * sin_a
            cy = rx * sin_a + ry * cos_a
            cam = MagicMock()
            cam.transform = True
            cam.enabled = True
            cam.center = MockVector([cx, cy, 30.0])
            cam.reference.location = MockVector([105.8, 21.0, 0.0])
            cameras.append(cam)

        chunk.cameras = cameras
        chunk.tie_points.points = []

        with patch.object(mr, "Metashape", self.mock_metashape):
            result = mr.auto_crop_camera_gps_square(chunk, padding=0.0)
            self.assertTrue(result)

            new_size = chunk.region.size
            dims = sorted([new_size.x, new_size.y])
            # Independent dimensions: 40m x 120m (Area ~ 4,800 m², NOT forced square 120x120 = 14,400 m²)
            self.assertAlmostEqual(dims[0], 40.0, delta=2.0)
            self.assertAlmostEqual(dims[1], 120.0, delta=2.0)

    def test_drone_flight_altitude_fov_safety_margin(self) -> None:
        """Defend that drone flight altitude expands horizontal box by FOV margin so photo frustums are framed."""
        chunk = MagicMock()
        identity_rot = MockMatrix([
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ])
        chunk.region = MagicMock()
        chunk.region.rot = identity_rot
        chunk.region.size = MockVector([500.0, 500.0, 100.0])
        chunk.transform.scale = 1.0

        # 4 cameras forming a 100m x 100m grid at altitude Z = 100m
        cameras = []
        coords = [
            (0.0, 0.0, 100.0),
            (100.0, 0.0, 100.0),
            (100.0, 100.0, 100.0),
            (0.0, 100.0, 100.0),
        ]
        for cx, cy, cz in coords:
            cam = MagicMock()
            cam.transform = True
            cam.enabled = True
            cam.center = MockVector([cx, cy, cz])
            cam.reference.location = MockVector([105.8, 21.0, 0.0])
            cameras.append(cam)
        chunk.cameras = cameras

        # Ground tie points at Z = 0m
        # Flight height = 100m - 0m = 100m. FOV safety margin = 100m * 0.35 = 35.0m on each side.
        chunk.tie_points.points = [
            MockTiePoint(50.0, 50.0, 0.0),
            MockTiePoint(25.0, 75.0, 0.0),
            MockTiePoint(75.0, 25.0, 0.0),
        ]

        with patch.object(mr, "Metashape", self.mock_metashape):
            # With padding = 0.01 (1m margin < 35m FOV margin), FOV margin (35m) must take precedence
            result = mr.auto_crop_camera_gps_square(chunk, padding=0.01)
            self.assertTrue(result)

            new_size = chunk.region.size
            # 100m + 2 * 35m = 170m
            self.assertAlmostEqual(new_size.x, 170.0, delta=2.0)
            self.assertAlmostEqual(new_size.y, 170.0, delta=2.0)


if __name__ == "__main__":
    unittest.main()
