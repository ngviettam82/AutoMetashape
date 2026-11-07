# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for on-demand geomatics generation during export."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure scripts directory is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import metashape_export


class TestExportOnDemand(unittest.TestCase):
    """Test on-demand creation of DEM and Orthomosaics during export."""

    def test_export_builds_dem_on_demand_when_missing(self) -> None:
        """Defend that missing DEM triggers chunk.buildDem() before export."""
        mock_doc = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.model = MagicMock()
        mock_chunk.model.vertices = [1, 2, 3]
        mock_chunk.model.faces = [1, 2]
        mock_chunk.model.textures = [MagicMock()]
        mock_chunk.elevation = None  # Initially missing
        mock_chunk.orthomosaic = None
        mock_doc.chunk = mock_chunk

        def mock_build_dem(*args, **kwargs):
            mock_chunk.elevation = MagicMock()

        mock_chunk.buildDem.side_effect = mock_build_dem

        with patch.object(metashape_export, "Metashape", create=True) as mock_meta:
            mock_meta.Document.return_value = mock_doc
            mock_meta.app.enumGPUDevices.return_value = []
            mock_meta.DataSource.ElevationData = 1
            mock_meta.DataSource.DepthMapsData = 2
            mock_meta.DataSource.ModelData = 3
            mock_meta.DataSource.PointCloudData = 4
            mock_meta.DataSource.OrthomosaicData = 5
            mock_meta.ImageFormat.ImageFormatTIFF = 1
            mock_meta.ModelFormat.ModelFormatOBJ = 1
            mock_meta.ModelFormat.ModelFormatGLTF = 2
            mock_meta.ImageCompression.TiffCompression.TiffCompressionLZW = 1

            with patch("os.path.exists", return_value=True), \
                 patch("os.path.getsize", return_value=1024), \
                 patch("os.makedirs"):
                metashape_export.export_project_assets(
                    project_path="dummy.psx",
                    output_dir="dummy_out",
                    export_dem=True,
                )

        # Verify buildDem was called
        self.assertTrue(mock_chunk.buildDem.called)
        self.assertTrue(mock_chunk.exportRaster.called)

    def test_export_builds_ortho_on_demand_when_missing(self) -> None:
        """Defend that missing Orthomosaic triggers chunk.buildOrthomosaic() before export."""
        mock_doc = MagicMock()
        mock_chunk = MagicMock()
        mock_chunk.model = MagicMock()
        mock_chunk.model.vertices = [1, 2, 3]
        mock_chunk.model.faces = [1, 2]
        mock_chunk.model.textures = [MagicMock()]
        mock_chunk.elevation = MagicMock()
        mock_chunk.orthomosaic = None  # Initially missing
        mock_doc.chunk = mock_chunk

        def mock_build_ortho(*args, **kwargs):
            mock_chunk.orthomosaic = MagicMock()

        mock_chunk.buildOrthomosaic.side_effect = mock_build_ortho

        with patch.object(metashape_export, "Metashape", create=True) as mock_meta:
            mock_meta.Document.return_value = mock_doc
            mock_meta.app.enumGPUDevices.return_value = []
            mock_meta.DataSource.ElevationData = 1
            mock_meta.DataSource.OrthomosaicData = 5
            mock_meta.ImageFormat.ImageFormatTIFF = 1
            mock_meta.ModelFormat.ModelFormatOBJ = 1
            mock_meta.ModelFormat.ModelFormatGLTF = 2
            mock_meta.ImageCompression.TiffCompression.TiffCompressionLZW = 1

            with patch("os.path.exists", return_value=True), \
                 patch("os.path.getsize", return_value=1024), \
                 patch("os.makedirs"):
                metashape_export.export_project_assets(
                    project_path="dummy.psx",
                    output_dir="dummy_out",
                    export_orthomosaic=True,
                )

        # Verify buildOrthomosaic was called
        self.assertTrue(mock_chunk.buildOrthomosaic.called)
        self.assertTrue(mock_chunk.exportRaster.called)


if __name__ == "__main__":
    unittest.main()
