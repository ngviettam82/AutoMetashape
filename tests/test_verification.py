# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit test suite for automated deliverable quality and integrity verification gates."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "source")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from autometashape.core.verification import (
    verify_geotiff,
    verify_glb_model,
    verify_obj_model,
    verify_pdf_report,
    verify_point_cloud,
    verify_project_deliverables,
)


class TestDeliverableVerification(unittest.TestCase):
    """Defends quality and integrity verification checks across all photogrammetry asset types."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="test_verification_")

    def tearDown(self) -> None:
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_verify_obj_model_valid(self) -> None:
        """Verify that a well-formed OBJ with MTL and existing texture atlas passes."""
        obj_path = os.path.join(self.test_dir, "model.obj")
        mtl_path = os.path.join(self.test_dir, "model.mtl")
        tex_path = os.path.join(self.test_dir, "texture.png")

        # Create dummy texture
        with open(tex_path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1500)

        # Create dummy MTL referencing texture
        with open(mtl_path, "w", encoding="utf-8") as f:
            f.write("newmtl material_0\nKa 1.0 1.0 1.0\nKd 1.0 1.0 1.0\nmap_Kd texture.png\n")

        # Create dummy OBJ with vertices, faces, and mtllib
        with open(obj_path, "w", encoding="utf-8") as f:
            f.write("mtllib model.mtl\nusemtl material_0\n")
            f.write("v 0.0 0.0 0.0\nv 1.0 0.0 0.0\nv 0.0 1.0 0.0\n")
            f.write("vt 0.0 0.0\nvt 1.0 0.0\nvt 0.0 1.0\n")
            f.write("f 1/1/1 2/2/1 3/3/1\n")
            f.write("# Dummy padding to exceed 1000 bytes threshold\n" + ("# comment\n" * 100))

        ok, errs = verify_obj_model(obj_path, min_size_bytes=500)
        self.assertTrue(ok, f"Expected valid OBJ to pass, but got errors: {errs}")
        self.assertEqual(len(errs), 0)

    def test_verify_obj_model_missing_faces(self) -> None:
        """Verify that an OBJ with no face definitions fails."""
        obj_path = os.path.join(self.test_dir, "no_faces.obj")
        with open(obj_path, "w", encoding="utf-8") as f:
            f.write("v 0.0 0.0 0.0\nv 1.0 0.0 0.0\n" + ("# padding\n" * 100))

        ok, errs = verify_obj_model(obj_path, min_size_bytes=100)
        self.assertFalse(ok)
        self.assertTrue(any("face" in e.lower() for e in errs))

    def test_verify_obj_model_missing_texture(self) -> None:
        """Verify that an OBJ with an MTL referencing a missing texture fails."""
        obj_path = os.path.join(self.test_dir, "model_missing_tex.obj")
        mtl_path = os.path.join(self.test_dir, "missing.mtl")

        with open(mtl_path, "w", encoding="utf-8") as f:
            f.write("newmtl mat\nmap_Kd nonexistent_texture.png\n")

        with open(obj_path, "w", encoding="utf-8") as f:
            f.write("mtllib missing.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n" + ("# pad\n" * 100))

        ok, errs = verify_obj_model(obj_path, min_size_bytes=100)
        self.assertFalse(ok)
        self.assertTrue(any("missing texture" in e.lower() for e in errs))

    def test_verify_glb_model(self) -> None:
        """Verify binary GLB container validation."""
        valid_glb = os.path.join(self.test_dir, "valid.glb")
        invalid_glb = os.path.join(self.test_dir, "invalid.glb")

        with open(valid_glb, "wb") as f:
            f.write(b"glTF" + b"\x00" * 1200)

        with open(invalid_glb, "wb") as f:
            f.write(b"CORR" + b"\x00" * 1200)

        ok_valid, _ = verify_glb_model(valid_glb, min_size_bytes=500)
        self.assertTrue(ok_valid)

        ok_invalid, errs = verify_glb_model(invalid_glb, min_size_bytes=500)
        self.assertFalse(ok_invalid)
        self.assertTrue(any("magic header" in e.lower() for e in errs))

    def test_verify_geotiff(self) -> None:
        """Verify GeoTIFF raster header validation."""
        valid_le = os.path.join(self.test_dir, "valid_le.tif")
        valid_be = os.path.join(self.test_dir, "valid_be.tif")
        corrupt_tif = os.path.join(self.test_dir, "corrupt.tif")

        with open(valid_le, "wb") as f:
            f.write(b"II*\x00" + b"\x00" * 1500)
        with open(valid_be, "wb") as f:
            f.write(b"MM\x00*" + b"\x00" * 1500)
        with open(corrupt_tif, "wb") as f:
            f.write(b"BAD0" + b"\x00" * 1500)

        self.assertTrue(verify_geotiff(valid_le, min_size_bytes=500)[0])
        self.assertTrue(verify_geotiff(valid_be, min_size_bytes=500)[0])
        self.assertFalse(verify_geotiff(corrupt_tif, min_size_bytes=500)[0])

    def test_verify_point_cloud(self) -> None:
        """Verify LAS, LAZ, and PLY point cloud header validation."""
        valid_laz = os.path.join(self.test_dir, "points.laz")
        valid_ply = os.path.join(self.test_dir, "points.ply")
        corrupt_laz = os.path.join(self.test_dir, "corrupt.laz")

        with open(valid_laz, "wb") as f:
            f.write(b"LASF" + b"\x00" * 60000)
        with open(valid_ply, "wb") as f:
            f.write(b"ply\nformat ascii 1.0\n" + b"\x00" * 60000)
        with open(corrupt_laz, "wb") as f:
            f.write(b"NOTL" + b"\x00" * 60000)

        self.assertTrue(verify_point_cloud(valid_laz, min_size_bytes=1000)[0])
        self.assertTrue(verify_point_cloud(valid_ply, min_size_bytes=1000)[0])
        self.assertFalse(verify_point_cloud(corrupt_laz, min_size_bytes=1000)[0])

    def test_verify_pdf_report(self) -> None:
        """Verify official PDF calibration report header."""
        valid_pdf = os.path.join(self.test_dir, "report.pdf")
        invalid_pdf = os.path.join(self.test_dir, "bad.pdf")

        with open(valid_pdf, "wb") as f:
            f.write(b"%PDF-1.4\n" + b"\x00" * 20000)
        with open(invalid_pdf, "wb") as f:
            f.write(b"TEXT" + b"\x00" * 20000)

        self.assertTrue(verify_pdf_report(valid_pdf, min_size_bytes=1000)[0])
        self.assertFalse(verify_pdf_report(invalid_pdf, min_size_bytes=1000)[0])

    def test_verify_project_deliverables_directory(self) -> None:
        """Verify end-to-end directory scanning and deliverable verification."""
        # Populate full valid deliverables package
        with open(os.path.join(self.test_dir, "Survey_0167.glb"), "wb") as f:
            f.write(b"glTF" + b"\x00" * 2000)

        with open(os.path.join(self.test_dir, "Survey_0167_dem.tif"), "wb") as f:
            f.write(b"II*\x00" + b"\x00" * 2000)

        with open(os.path.join(self.test_dir, "Survey_0167_ortho.tif"), "wb") as f:
            f.write(b"II*\x00" + b"\x00" * 2000)

        with open(os.path.join(self.test_dir, "Survey_0167.laz"), "wb") as f:
            f.write(b"LASF" + b"\x00" * 60000)

        with open(os.path.join(self.test_dir, "Survey_0167_report.pdf"), "wb") as f:
            f.write(b"%PDF-1.4\n" + b"\x00" * 20000)

        report = verify_project_deliverables(
            deliverables_dir=self.test_dir,
            project_name="Survey_0167",
            expected_assets=["glb", "dem", "ortho", "point_cloud", "report"],
        )

        self.assertEqual(report["status"], "PASS")
        self.assertEqual(len(report["errors"]), 0)
        self.assertIn("glb", report["assets_checked"])
        self.assertIn("dem", report["assets_checked"])
        self.assertIn("ortho", report["assets_checked"])
        self.assertIn("point_cloud", report["assets_checked"])
        self.assertIn("report", report["assets_checked"])


if __name__ == "__main__":
    unittest.main()
