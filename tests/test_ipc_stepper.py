# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for AutoMetashape IPC stage protocol, sentinel parsing, and monotonic state machine."""

from __future__ import annotations

import os
import sys
import unittest

# Ensure scripts directory is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from autometashape_gui import parse_ipc_line, IPC_SENTINEL_PREFIX, IPC_SENTINEL_SUFFIX


class TestIpcStepper(unittest.TestCase):
    """Test IPC protocol parsing, sentinel tokens, and protection against loose string matching."""

    def test_ipc_sentinel_parsing(self) -> None:
        """Defend that explicit machine-readable sentinels are parsed with 100% accuracy."""
        cases = [
            (f"{IPC_SENTINEL_PREFIX}ingest:Ingesting photos & blur filter{IPC_SENTINEL_SUFFIX}", "ingest", "Ingesting photos & blur filter"),
            (f"{IPC_SENTINEL_PREFIX}match:GPU Feature Matching{IPC_SENTINEL_SUFFIX}", "match", "GPU Feature Matching"),
            (f"{IPC_SENTINEL_PREFIX}align:Camera Alignment & Tie Points{IPC_SENTINEL_SUFFIX}", "align", "Camera Alignment & Tie Points"),
            (f"{IPC_SENTINEL_PREFIX}depth:MVS Dense Stereopsis{IPC_SENTINEL_SUFFIX}", "depth", "MVS Dense Stereopsis"),
            (f"{IPC_SENTINEL_PREFIX}point_cloud:Generating Dense Point Cloud{IPC_SENTINEL_SUFFIX}", "point_cloud", "Generating Dense Point Cloud"),
            (f"{IPC_SENTINEL_PREFIX}mesh:Fusing Surface Mesh Geometry{IPC_SENTINEL_SUFFIX}", "mesh", "Fusing Surface Mesh Geometry"),
            (f"{IPC_SENTINEL_PREFIX}texture:Baking 8K Texture Atlas{IPC_SENTINEL_SUFFIX}", "texture", "Baking 8K Texture Atlas"),
            (f"{IPC_SENTINEL_PREFIX}dem:Building Digital Elevation Model{IPC_SENTINEL_SUFFIX}", "dem", "Building Digital Elevation Model"),
            (f"{IPC_SENTINEL_PREFIX}ortho:Generating Orthomosaic{IPC_SENTINEL_SUFFIX}", "ortho", "Generating Orthomosaic"),
            (f"{IPC_SENTINEL_PREFIX}export:Exporting Production Assets{IPC_SENTINEL_SUFFIX}", "export", "Exporting Production Assets"),
        ]
        for line, expected_key, expected_desc in cases:
            res = parse_ipc_line(line, mode="recon")
            self.assertIsNotNone(res, f"Failed to parse sentinel: {line}")
            self.assertEqual(res[0], expected_key)
            self.assertEqual(res[1], expected_desc)

    def test_rejection_of_loose_configuration_substrings(self) -> None:
        """Defend that configuration headers, CLI flags, and C++ internal logs DO NOT trigger transitions."""
        forbidden_lines = [
            "  Elevation (DEM):    ENABLED | Orthomosaic: ENABLED",
            "  Dense Point Cloud:  ENABLED (Classify Ground: False)",
            "  Depth Maps Quality: MEDIUM (Downscale: 4) | Filtering: Moderate",
            "Command: ... --ortho --no-export-ortho --build-dem --no-export-dem",
            "Pipeline (Survey Profiling, Point Cloud, Mesh & Orthomosaic)",
            "Marching cubes: part #2/3...",
            "Estimating total number of faces from 96 parts...",
            "reading 96 mesh parts...",
            "merging all parts...",
            "Peak memory used: 8.39 GB at 2026-11-07 22:21:37",
            "loading orthomosaic...",
            "saving elevation...",
            "random text with orthomosaic in the middle",
        ]
        for line in forbidden_lines:
            res = parse_ipc_line(line, mode="recon")
            self.assertIsNone(
                res,
                f"Loose substring falsely triggered milestone on line: '{line}' -> {res}"
            )

    def test_anchored_bracket_tag_parsing(self) -> None:
        """Defend that lines beginning with anchored [X/7] bracket tags parse accurately."""
        valid_tags = [
            ("[1/7] Ingesting 711 images found across: /path/to/imgs", "ingest"),
            ("[2/7] Executing GPU Feature Matching (Downscale: 2)...", "match"),
            ("[3/7] Aligning Cameras & Initial Bundle Adjustment...", "align"),
            ("[3b/7] Manual Bounding Box Crop selected: Auto-crop skipped.", "align"),
            ("[Auto-Crop-GPS] Computing minimum area horizontal enclosing square...", "align"),
            ("[4/7] Computing GPU MVS Depth Maps (Quality: Medium)...", "depth"),
            ("[4b/7] Generating Dense Point Cloud from Depth Maps...", "point_cloud"),
            ("[5/7] Reconstructing Arbitrary Surface Mesh from Depth Maps...", "mesh"),
            ("[6/7] Baking 22x 8192x8192 (8K) Photogrammetric Texture Atlas...", "texture"),
            ("[6b/7] Building Hierarchical 3D Tiled Model...", "tiled_model"),
            ("[7b/7] Building Digital Elevation Model (DEM/DSM)...", "dem"),
            ("[7b2/7] Generating Topographic Contours (Interval: 1.0m)...", "dem"),
            ("[7c/7] Generating Orthomosaic from Reconstructed Surface Mesh...", "ortho"),
            ("[7d/7] [EXPORT] Exporting Dense Point Cloud to: ...", "point_cloud"),
            ("[7e/7] [EXPORT] Exporting Official Calibration & Quality PDF Report...", "export"),
            ("  [7/7] EXPORTING PRODUCTION ASSETS (GLB, OBJ, TIFF, LAZ, DEM & PSX)", "export"),
        ]
        for line, expected_key in valid_tags:
            res = parse_ipc_line(line, mode="recon")
            self.assertIsNotNone(res, f"Failed to match valid anchored tag: '{line}'")
            self.assertEqual(res[0], expected_key, f"Tag mismatch for '{line}': expected {expected_key}, got {res[0]}")

    def test_export_mode_anchored_tags(self) -> None:
        """Defend standalone export stage tags parsing."""
        export_tags = [
            ("[Import] Importing 3D mesh model from: model.obj", "project"),
            ("[Model Info] Geometry: 10,000 vertices, 20,000 polygonal faces.", "mesh"),
            ("[Mesh] Applying Laplacian smoothing...", "mesh"),
            ("[Color Calibration] Executing radiometric calibration...", "texture"),
            ("[Texture] Baking 4x 8192x8192 (8K) Texture Atlas...", "texture"),
            ("[Delighting] Removing harsh ambient shadows...", "texture"),
            ("[1/2] Exporting Binary glTF (.glb)...", "models"),
            ("[2/2] Exporting Wavefront OBJ (.obj + .mtl)...", "models"),
            ("[Point Cloud] Exporting Dense Point Cloud...", "deliverables"),
            ("[DEM] Exporting GeoTIFF Elevation Model...", "deliverables"),
            ("[Orthomosaic] Exporting GeoTIFF Orthomosaic...", "deliverables"),
            ("[FBX] Exporting Autodesk FBX...", "models"),
            ("[PLY] Exporting Stanford PLY...", "models"),
            ("[DXF] Exporting AutoCAD DXF...", "models"),
            ("[Tie Points] Exporting Sparse Tie Points...", "models"),
            ("[Tiled Model] Exporting 3D Tiled Model...", "deliverables"),
            ("[Contours] Generating Topographic Contours...", "deliverables"),
            ("[Report] Exporting Official PDF Report...", "deliverables"),
        ]
        for line, expected_key in export_tags:
            res = parse_ipc_line(line, mode="export")
            self.assertIsNotNone(res, f"Failed to match export tag: '{line}'")
            self.assertEqual(res[0], expected_key, f"Export tag mismatch for '{line}': expected {expected_key}, got {res[0]}")

    def test_engine_ipc_sentinel_cross_engine_compatibility(self) -> None:
        """Defend that recon and export engine sentinels are completely unified with GUI parser."""
        import metashape_reconstruction as mr
        import metashape_export as me

        self.assertEqual(mr.IPC_SENTINEL_PREFIX, IPC_SENTINEL_PREFIX)
        self.assertEqual(mr.IPC_SENTINEL_SUFFIX, IPC_SENTINEL_SUFFIX)
        self.assertEqual(me.IPC_SENTINEL_PREFIX, IPC_SENTINEL_PREFIX)
        self.assertEqual(me.IPC_SENTINEL_SUFFIX, IPC_SENTINEL_SUFFIX)

        # Test simulated emission
        captured: list[str] = []
        old_print = print
        try:
            import builtins
            builtins.print = lambda *args, **kwargs: captured.append(" ".join(str(a) for a in args))
            mr.emit_ipc_stage("mesh", "Fusing Surface Mesh")
            mr.emit_ipc_event(mr.IPC_COMPLETE)
            me.emit_ipc_stage("deliverables", "Exporting Deliverables")
            me.emit_ipc_event(me.IPC_COMPLETE)
        finally:
            import builtins
            builtins.print = old_print

        self.assertEqual(len(captured), 4)
        res_mesh = parse_ipc_line(captured[0], mode="recon")
        self.assertEqual(res_mesh, ("mesh", "Fusing Surface Mesh"))
        self.assertEqual(captured[1], mr.IPC_COMPLETE)
        res_deliv = parse_ipc_line(captured[2], mode="export")
        self.assertEqual(res_deliv, ("deliverables", "Exporting Deliverables"))
        self.assertEqual(captured[3], me.IPC_COMPLETE)


if __name__ == "__main__":
    unittest.main()
