# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests verifying photogrammetry geomatics audit requirements."""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure repository root and scripts directory are on sys.path
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
scripts_dir = os.path.join(repo_root, "scripts")
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

import metashape_reconstruction as mr


class TestGeomaticsAudit(unittest.TestCase):
    """Verifies that unified pipeline tasks adhere to photogrammetry geomatics standards."""

    def setUp(self):
        self.mock_meta = MagicMock()
        self.mock_meta.ReferencePreselectionMode.ReferencePreselectionEstimated = 1
        self.mock_meta.FilterMode.ModerateFiltering = 2
        self.mock_meta.DataSource.DepthMapsData = 1
        self.mock_meta.DataSource.ElevationData = 2
        self.mock_meta.DataSource.OrthomosaicData = 3
        self.mock_meta.DataSource.PointCloudData = 4
        self.mock_meta.DataSource.TiePointsData = 5
        self.mock_meta.DataSource.ModelData = 6
        self.mock_meta.SurfaceType.Arbitrary = 0
        self.mock_meta.Interpolation.EnabledInterpolation = 1
        self.mock_meta.FaceCount.CustomFaceCount = 1
        self.mock_meta.MappingMode.GenericMapping = 0
        self.mock_meta.BlendingMode.MosaicBlending = 1
        self.mock_meta.ModelFormat.ModelFormatOBJ = 1
        self.mock_meta.ModelFormat.ModelFormatGLTF = 2
        self.mock_meta.ModelFormat.ModelFormatDXF = 3
        self.mock_meta.ImageFormat.ImageFormatPNG = 1
        self.mock_meta.RasterFormat.RasterFormatNone = 0
        self.mock_meta.ImageFormat.ImageFormatTIFF = 2
        self.mock_meta.PointCloudFormat.PointCloudFormatLAZ = 1
        self.mock_meta.PointCloudFormat.PointCloudFormatPLY = 2
        self.mock_meta.TiledModelFormat.TiledModelFormatCesium = 1

        self.mock_meta.Tasks.MatchPhotos = lambda: MagicMock(name="MatchPhotos")
        self.mock_meta.Tasks.AlignCameras = lambda: MagicMock(name="AlignCameras")
        self.mock_meta.Tasks.CleanTiePoints = lambda: MagicMock(name="CleanTiePoints", criterion=1)
        self.mock_meta.Tasks.OptimizeCameras = lambda: MagicMock(name="OptimizeCameras")
        self.mock_meta.Tasks.RunScript = lambda: MagicMock(name="RunScript", code="chunk.region.size = ...")
        self.mock_meta.Tasks.BuildDepthMaps = lambda: MagicMock(name="BuildDepthMaps")
        self.mock_meta.Tasks.BuildPointCloud = lambda: MagicMock(name="BuildPointCloud")
        self.mock_meta.Tasks.BuildModel = lambda: MagicMock(name="BuildModel")
        self.mock_meta.Tasks.BuildUV = lambda: MagicMock(name="BuildUV", page_count=16)
        self.mock_meta.Tasks.BuildTexture = lambda: MagicMock(name="BuildTexture")
        self.mock_meta.Tasks.BuildDem = lambda: MagicMock(name="BuildDem")
        self.mock_meta.Tasks.BuildOrthomosaic = lambda: MagicMock(name="BuildOrthomosaic")
        self.mock_meta.Tasks.BuildTiledModel = lambda: MagicMock(name="BuildTiledModel")
        self.mock_meta.Tasks.ExportModel = lambda: MagicMock(name="ExportModel")
        self.mock_meta.Tasks.ExportRaster = lambda: MagicMock(name="ExportRaster")
        self.mock_meta.Tasks.ExportPointCloud = lambda: MagicMock(name="ExportPointCloud")
        self.mock_meta.Tasks.ExportReport = lambda: MagicMock(name="ExportReport")
        self.mock_meta.Tasks.ExportTiledModel = lambda: MagicMock(name="ExportTiledModel")

    def test_downscale_mappings_accuracy(self):
        """High align must be downscale 1, high depth must be downscale 2."""
        self.assertEqual(mr.ALIGN_DOWNSCALE_MAP["highest"], 0)
        self.assertEqual(mr.ALIGN_DOWNSCALE_MAP["high"], 1)
        self.assertEqual(mr.ALIGN_DOWNSCALE_MAP["medium"], 2)
        self.assertEqual(mr.DEPTH_DOWNSCALE_MAP["ultra"], 1)
        self.assertEqual(mr.DEPTH_DOWNSCALE_MAP["high"], 2)
        self.assertEqual(mr.DEPTH_DOWNSCALE_MAP["medium"], 4)

    def test_ortho_uses_dem_surface_when_dem_is_built(self):
        """When DEM is generated, orthomosaic must project onto ElevationData."""
        mock_chunk = MagicMock()
        mock_chunk.crs = None
        with patch.object(mr, "Metashape", self.mock_meta):
            tasks, stages = mr.build_unified_cluster_pipeline_tasks(
                chunk=mock_chunk,
                output_dir="out",
                project_name="proj",
                align_photos=False,
                build_depth_maps=False,
                build_mesh=False,
                build_texture=False,
                build_dem=True,
                build_orthomosaic=True,
                crop_method="manual",
            )
            t_om = next(t for t, (name, _, _) in zip(tasks, stages) if name == "BuildOrthomosaic")
            self.assertEqual(t_om.surface_data, self.mock_meta.DataSource.ElevationData)

    def test_ortho_uses_model_surface_when_no_dem_is_built(self):
        """When no DEM is generated, orthomosaic must project onto ModelData."""
        mock_chunk = MagicMock()
        mock_chunk.crs = None
        with patch.object(mr, "Metashape", self.mock_meta):
            tasks, stages = mr.build_unified_cluster_pipeline_tasks(
                chunk=mock_chunk,
                output_dir="out",
                project_name="proj",
                align_photos=False,
                build_depth_maps=False,
                build_mesh=True,
                build_texture=False,
                build_dem=False,
                build_orthomosaic=True,
                crop_method="manual",
            )
            t_om = next(t for t, (name, _, _) in zip(tasks, stages) if name == "BuildOrthomosaic")
            self.assertEqual(t_om.surface_data, self.mock_meta.DataSource.ModelData)

    def test_export_tie_points_included_in_unified_batch(self):
        """Export tie points option must emit ExportPointCloud task with TiePointsData."""
        mock_chunk = MagicMock()
        with patch.object(mr, "Metashape", self.mock_meta):
            tasks, stages = mr.build_unified_cluster_pipeline_tasks(
                chunk=mock_chunk,
                output_dir="out",
                project_name="proj",
                align_photos=False,
                build_depth_maps=False,
                build_mesh=False,
                build_texture=False,
                export_tie_points=True,
                crop_method="manual",
            )
            t_tp = next(
                t for t, (name, key, desc) in zip(tasks, stages) if name == "ExportPointCloud" and "Tie Points" in desc
            )
            self.assertEqual(t_tp.source_data, self.mock_meta.DataSource.TiePointsData)
            self.assertEqual(t_tp.format, self.mock_meta.PointCloudFormat.PointCloudFormatPLY)

    def test_tiled_model_export_included_in_unified_batch(self):
        """Build tiled model must append both BuildTiledModel and ExportTiledModel tasks."""
        mock_chunk = MagicMock()
        with patch.object(mr, "Metashape", self.mock_meta):
            tasks, stages = mr.build_unified_cluster_pipeline_tasks(
                chunk=mock_chunk,
                output_dir="out",
                project_name="proj",
                align_photos=False,
                build_depth_maps=False,
                build_mesh=False,
                build_texture=False,
                build_tiled_model=True,
                tiled_model_format="cesium",
                crop_method="manual",
            )
            stage_names = [name for name, _, _ in stages]
            self.assertIn("BuildTiledModel", stage_names)
            self.assertIn("ExportTiledModel", stage_names)

    def test_raster_export_includes_compression_and_world_file(self):
        """DEM and Ortho exports must configure LZW compression and save_world=True."""
        mock_chunk = MagicMock()
        with patch.object(mr, "Metashape", self.mock_meta):
            tasks, stages = mr.build_unified_cluster_pipeline_tasks(
                chunk=mock_chunk,
                output_dir="out",
                project_name="proj",
                align_photos=False,
                build_depth_maps=False,
                build_mesh=False,
                build_texture=False,
                build_dem=True,
                export_dem=True,
                build_orthomosaic=True,
                export_ortho=True,
                crop_method="manual",
            )
            t_dem = next(t for t, (name, key, desc) in zip(tasks, stages) if name == "ExportRaster" and "DEM" in desc)
            self.assertTrue(t_dem.save_world)
            self.assertIsNotNone(t_dem.image_compression)
            self.assertNotEqual(getattr(t_dem, "format", None), self.mock_meta.RasterFormat.RasterFormatNone)

            t_ortho = next(
                t for t, (name, key, desc) in zip(tasks, stages) if name == "ExportRaster" and "Orthomosaic" in desc
            )
            self.assertTrue(t_ortho.save_world)
            self.assertTrue(t_ortho.save_alpha)
            self.assertIsNotNone(t_ortho.image_compression)
            self.assertNotEqual(getattr(t_ortho, "format", None), self.mock_meta.RasterFormat.RasterFormatNone)


if __name__ == "__main__":
    unittest.main()
