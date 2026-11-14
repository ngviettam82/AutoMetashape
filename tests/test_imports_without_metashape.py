# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests verifying clean imports in headless/standard Python environments."""

from __future__ import annotations

import os
import sys
import unittest

# Ensure scripts directory is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))


class TestImportsWithoutMetashape(unittest.TestCase):
    """Test importing modules when Metashape is not present."""

    def test_import_recon_subsystem(self) -> None:
        """Defend that reconstruction utilities can be imported without Metashape C++ binary."""
        from metashape_reconstruction import (
            analyze_survey_physics,
            compute_utm_epsg,
            derive_pro_photogrammetry_plan,
            infer_project_name,
        )

        self.assertTrue(callable(analyze_survey_physics))
        self.assertTrue(callable(derive_pro_photogrammetry_plan))
        self.assertTrue(callable(infer_project_name))
        self.assertTrue(callable(compute_utm_epsg))

        # Test pure Python math logic
        epsg = compute_utm_epsg(105.85, 21.02)
        self.assertEqual(epsg, 32648)  # Northern hemisphere UTM Zone 48N

        p_name = infer_project_name(images_dir="/path/to/my_survey_site")
        self.assertEqual(p_name, "my_survey_site")

    def test_import_export_subsystem(self) -> None:
        """Defend that export module can be imported without Metashape C++ binary."""
        from metashape_export import build_arg_parser, infer_project_name

        self.assertTrue(callable(infer_project_name))
        self.assertTrue(callable(build_arg_parser))

        parser = build_arg_parser()
        self.assertIsNotNone(parser)


if __name__ == "__main__":
    unittest.main()
