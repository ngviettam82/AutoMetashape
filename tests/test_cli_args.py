# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for AutoMetashape CLI argument parsing consistency."""

from __future__ import annotations

import os
import sys
import unittest

# Ensure scripts directory is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from metashape_reconstruction import build_arg_parser as build_recon_parser
from metashape_export import build_arg_parser as build_export_parser


class TestCliArgs(unittest.TestCase):
    """Test CLI arguments consistency across pipeline subsystems."""

    def test_recon_parser_accepts_no_export_dem(self) -> None:
        """Defend against unrecognized --no-export-dem argument when exporting is decoupled."""
        parser = build_recon_parser()
        args = parser.parse_args([
            "--images", "dummy_images",
            "--build-dem",
            "--no-export-dem",
        ])
        self.assertTrue(args.build_dem)
        self.assertFalse(args.export_dem)

    def test_recon_parser_accepts_export_dem(self) -> None:
        """Defend --export-dem flag activating GeoTIFF export."""
        parser = build_recon_parser()
        args = parser.parse_args([
            "--images", "dummy_images",
            "--build-dem",
            "--export-dem",
        ])
        self.assertTrue(args.build_dem)
        self.assertTrue(args.export_dem)

    def test_recon_parser_reference_preselection_modes(self) -> None:
        """Defend reference preselection mode choices."""
        parser = build_recon_parser()

        # Default is estimated
        default_args = parser.parse_args(["--images", "dummy_images"])
        self.assertEqual(default_args.reference_preselection_mode, "estimated")

        for mode in ("estimated", "source", "sequential"):
            args = parser.parse_args([
                "--images", "dummy_images",
                "--reference-preselection-mode", mode,
            ])
            self.assertEqual(args.reference_preselection_mode, mode)

    def test_recon_parser_presets(self) -> None:
        """Defend supported presets including building alias."""
        parser = build_recon_parser()
        for preset in ("pro", "custom", "building"):
            args = parser.parse_args([
                "--images", "dummy_images",
                "--preset", preset,
            ])
            self.assertEqual(args.preset, preset)

    def test_export_parser_arguments(self) -> None:
        """Defend standalone export argument parser accepts required and optional flags."""
        parser = build_export_parser()
        args = parser.parse_args([
            "--project", "dummy.psx",
            "--output", "dummy_out",
            "--export-dem",
            "--export-orthomosaic",
            "--export-contours",
            "--export-point-cloud",
            "--export-fbx",
            "--export-ply",
            "--export-dxf",
            "--export-tie-points",
        ])
        self.assertEqual(args.project, "dummy.psx")
        self.assertEqual(args.output, "dummy_out")
        self.assertTrue(args.export_dem)
        self.assertTrue(args.export_orthomosaic)
        self.assertTrue(args.export_contours)
        self.assertTrue(args.export_point_cloud)
        self.assertTrue(args.export_fbx)
        self.assertTrue(args.export_ply)
        self.assertTrue(args.export_dxf)
        self.assertTrue(args.export_tie_points)


if __name__ == "__main__":
    unittest.main()
