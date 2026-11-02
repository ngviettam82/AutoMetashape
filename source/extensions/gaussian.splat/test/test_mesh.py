# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for mesh reconstruction and texture baking."""

import os
import tempfile
import numpy as np
import trimesh
from gaussian_splat.mesh import create_textured_model, load_ply_points


def test_load_ply_points():
    with tempfile.TemporaryDirectory() as tmpdir:
        ply_path = os.path.join(tmpdir, "test.ply")
        pts = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        cols = np.array([[255, 0, 0, 255], [0, 255, 0, 255], [0, 0, 255, 255]], dtype=np.uint8)
        cloud = trimesh.PointCloud(vertices=pts, colors=cols)
        cloud.export(ply_path)

        loaded_pts, loaded_cols = load_ply_points(ply_path)
        assert len(loaded_pts) == 3
        assert loaded_cols is not None


def test_create_textured_model():
    with tempfile.TemporaryDirectory() as tmpdir:
        ply_path = os.path.join(tmpdir, "cube.ply")
        out_dir = os.path.join(tmpdir, "output")

        # Generate a small 3D cube point cloud
        x = np.linspace(-1, 1, 10)
        grid = np.array(np.meshgrid(x, x, x)).reshape(3, -1).T
        cols = np.ones((len(grid), 4), dtype=np.uint8) * 180

        cloud = trimesh.PointCloud(vertices=grid, colors=cols)
        cloud.export(ply_path)

        success = create_textured_model(ply_path, out_dir, texture_res=512, pitch=0.5)
        assert success is True
        assert os.path.exists(os.path.join(out_dir, "model.obj"))
        assert os.path.exists(os.path.join(out_dir, "model.mtl"))
        assert os.path.exists(os.path.join(out_dir, "texture.jpg"))
