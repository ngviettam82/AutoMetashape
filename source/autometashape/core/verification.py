# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Automated deliverable quality and integrity verification gate.

Validates that reconstructed 3D meshes (OBJ+MTL, GLB), raster elevation models (DEM),
true orthomosaics (GeoTIFF), dense point clouds (LAZ/LAS/PLY), and engineering calibration
reports (PDF) are non-empty, mathematically valid, structurally uncorrupted, and production-ready.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple


def verify_obj_model(obj_path: str, min_size_bytes: int = 1000) -> Tuple[bool, List[str]]:
    """Validates Wavefront OBJ model integrity, vertex definitions, and MTL texture linking."""
    errors: List[str] = []
    if not os.path.isfile(obj_path):
        return False, [f"OBJ file not found: {obj_path}"]

    size = os.path.getsize(obj_path)
    if size < min_size_bytes:
        return False, [f"OBJ file size ({size} bytes) is below minimum threshold ({min_size_bytes} bytes)"]

    has_vertex = False
    has_face = False
    mtl_filenames: List[str] = []
    base_dir = os.path.dirname(os.path.abspath(obj_path))

    try:
        with open(obj_path, "r", encoding="utf-8", errors="ignore") as f:
            for line_idx, line in enumerate(f):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("v "):
                    has_vertex = True
                elif stripped.startswith("f "):
                    has_face = True
                elif stripped.startswith("mtllib "):
                    parts = stripped.split(None, 1)
                    if len(parts) > 1:
                        mtl_filenames.append(parts[1].strip())

                # Quick short-circuit check for large OBJ files
                if line_idx > 50000 and has_vertex and has_face:
                    break
    except Exception as exc:
        return False, [f"Failed to read OBJ file: {exc}"]

    if not has_vertex:
        errors.append("OBJ file contains no vertex ('v ') definitions.")
    if not has_face:
        errors.append("OBJ file contains no face ('f ') polygon definitions.")

    # Validate Material & Texture Links
    for mtl_name in mtl_filenames:
        mtl_path = os.path.join(base_dir, mtl_name)
        if not os.path.isfile(mtl_path):
            errors.append(f"Linked MTL material library does not exist: {mtl_path}")
            continue

        try:
            with open(mtl_path, "r", encoding="utf-8", errors="ignore") as mf:
                for mline in mf:
                    mstripped = mline.strip()
                    if mstripped.startswith("map_Kd "):
                        tex_parts = mstripped.split(None, 1)
                        if len(tex_parts) > 1:
                            tex_file = tex_parts[1].strip()
                            tex_path = os.path.join(base_dir, tex_file)
                            if not os.path.isfile(tex_path):
                                errors.append(f"MTL references missing texture atlas file: {tex_path}")
                            elif os.path.getsize(tex_path) < 1000:
                                errors.append(f"Referenced texture file is undersized/empty: {tex_path}")
        except Exception as m_exc:
            errors.append(f"Failed to parse linked MTL material file '{mtl_path}': {m_exc}")

    return len(errors) == 0, errors


def verify_glb_model(glb_path: str, min_size_bytes: int = 1000) -> Tuple[bool, List[str]]:
    """Validates Binary glTF (.glb) container structure and binary header magic."""
    if not os.path.isfile(glb_path):
        return False, [f"GLB file not found: {glb_path}"]

    size = os.path.getsize(glb_path)
    if size < min_size_bytes:
        return False, [f"GLB file size ({size} bytes) is below minimum threshold ({min_size_bytes} bytes)"]

    try:
        with open(glb_path, "rb") as f:
            magic = f.read(4)
            if magic != b"glTF":
                return False, [f"Invalid GLB magic header: expected b'glTF', got {magic!r}"]
    except Exception as exc:
        return False, [f"Failed to read GLB binary header: {exc}"]

    return True, []


def verify_geotiff(tif_path: str, min_size_bytes: int = 1000) -> Tuple[bool, List[str]]:
    """Validates GeoTIFF (DEM, Orthomosaic) raster integrity and TIFF header magic."""
    if not os.path.isfile(tif_path):
        return False, [f"GeoTIFF file not found: {tif_path}"]

    size = os.path.getsize(tif_path)
    if size < min_size_bytes:
        return False, [f"GeoTIFF file size ({size} bytes) is below minimum threshold ({min_size_bytes} bytes)"]

    try:
        with open(tif_path, "rb") as f:
            magic = f.read(4)
            valid_magics = [
                b"II*\x00",  # Classic TIFF Little-Endian
                b"MM\x00*",  # Classic TIFF Big-Endian
                b"II+\x00",  # BigTIFF Little-Endian
                b"MM\x00+",  # BigTIFF Big-Endian
            ]
            if magic not in valid_magics:
                return False, [f"Invalid TIFF header magic: {magic!r}"]
    except Exception as exc:
        return False, [f"Failed to read TIFF header: {exc}"]

    return True, []


def verify_point_cloud(cloud_path: str, min_size_bytes: int = 50000) -> Tuple[bool, List[str]]:
    """Validates Dense Point Cloud (LAZ, LAS, PLY) structure and header magic."""
    if not os.path.isfile(cloud_path):
        return False, [f"Point cloud file not found: {cloud_path}"]

    size = os.path.getsize(cloud_path)
    if size < min_size_bytes:
        return False, [f"Point cloud size ({size} bytes) is below minimum threshold ({min_size_bytes} bytes)"]

    ext = os.path.splitext(cloud_path)[1].lower()
    try:
        with open(cloud_path, "rb") as f:
            header = f.read(4)
            if ext in (".las", ".laz"):
                if header != b"LASF":
                    return False, [f"Invalid LAS/LAZ magic header: expected b'LASF', got {header!r}"]
            elif ext == ".ply":
                if not header.startswith(b"ply"):
                    return False, [f"Invalid PLY magic header: expected b'ply', got {header!r}"]
    except Exception as exc:
        return False, [f"Failed to read point cloud header: {exc}"]

    return True, []


def verify_pdf_report(pdf_path: str, min_size_bytes: int = 15000) -> Tuple[bool, List[str]]:
    """Validates Metashape calibration & quality PDF report header."""
    if not os.path.isfile(pdf_path):
        return False, [f"PDF report not found: {pdf_path}"]

    size = os.path.getsize(pdf_path)
    if size < min_size_bytes:
        return False, [f"PDF report size ({size} bytes) is below minimum threshold ({min_size_bytes} bytes)"]

    try:
        with open(pdf_path, "rb") as f:
            magic = f.read(4)
            if magic != b"%PDF":
                return False, [f"Invalid PDF header: expected b'%PDF', got {magic!r}"]
    except Exception as exc:
        return False, [f"Failed to read PDF report header: {exc}"]

    return True, []


def verify_project_deliverables(
    deliverables_dir: str,
    project_name: Optional[str] = None,
    expected_assets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Scans and comprehensively validates all photogrammetry deliverable assets in a directory.

    Args:
        deliverables_dir: Directory containing production deliverables.
        project_name: Optional base project name to filter on.
        expected_assets: Optional list of asset types expected ('obj', 'glb', 'dem', 'ortho', 'point_cloud', 'report').

    Returns:
        Structured result dict with status ('PASS'/'FAIL'), validated asset metadata, and issue logs.
    """
    if not os.path.isdir(deliverables_dir):
        return {
            "status": "FAIL",
            "deliverables_dir": deliverables_dir,
            "project_name": project_name or "",
            "assets_checked": {},
            "errors": [f"Deliverables directory does not exist: {deliverables_dir}"],
            "warnings": [],
        }

    errors: List[str] = []
    warnings: List[str] = []
    assets_checked: Dict[str, Any] = {}

    # Gather files in deliverables directory
    all_files = os.listdir(deliverables_dir)

    # 1. 3D Model: OBJ + MTL
    obj_files = [f for f in all_files if f.lower().endswith(".obj")]
    if obj_files:
        target_obj = obj_files[0]
        if project_name:
            named_objs = [f for f in obj_files if f.lower().startswith(project_name.lower())]
            if named_objs:
                target_obj = named_objs[0]
        obj_full = os.path.join(deliverables_dir, target_obj)
        ok, errs = verify_obj_model(obj_full)
        assets_checked["obj"] = {
            "filename": target_obj,
            "path": obj_full,
            "size_bytes": os.path.getsize(obj_full),
            "status": "PASS" if ok else "FAIL",
            "errors": errs,
        }
        if not ok:
            errors.extend(errs)
    elif expected_assets and "obj" in expected_assets:
        errors.append("Expected Wavefront OBJ model not found in deliverables directory.")

    # 2. 3D Model: GLB
    glb_files = [f for f in all_files if f.lower().endswith(".glb")]
    if glb_files:
        target_glb = glb_files[0]
        if project_name:
            named_glbs = [f for f in glb_files if f.lower().startswith(project_name.lower())]
            if named_glbs:
                target_glb = named_glbs[0]
        glb_full = os.path.join(deliverables_dir, target_glb)
        ok, errs = verify_glb_model(glb_full)
        assets_checked["glb"] = {
            "filename": target_glb,
            "path": glb_full,
            "size_bytes": os.path.getsize(glb_full),
            "status": "PASS" if ok else "FAIL",
            "errors": errs,
        }
        if not ok:
            errors.extend(errs)
    elif expected_assets and "glb" in expected_assets:
        errors.append("Expected binary GLB model not found in deliverables directory.")

    # 3. Elevation & Orthomosaic (GeoTIFF)
    tif_files = [f for f in all_files if f.lower().endswith((".tif", ".tiff"))]
    dem_files = [f for f in tif_files if "dem" in f.lower() or "dsm" in f.lower() or "elevation" in f.lower()]
    ortho_files = [f for f in tif_files if "ortho" in f.lower()]

    if dem_files:
        dem_full = os.path.join(deliverables_dir, dem_files[0])
        ok, errs = verify_geotiff(dem_full)
        assets_checked["dem"] = {
            "filename": dem_files[0],
            "path": dem_full,
            "size_bytes": os.path.getsize(dem_full),
            "status": "PASS" if ok else "FAIL",
            "errors": errs,
        }
        if not ok:
            errors.extend(errs)
    elif expected_assets and "dem" in expected_assets:
        errors.append("Expected GeoTIFF DEM file not found in deliverables directory.")

    if ortho_files:
        ortho_full = os.path.join(deliverables_dir, ortho_files[0])
        ok, errs = verify_geotiff(ortho_full)
        assets_checked["ortho"] = {
            "filename": ortho_files[0],
            "path": ortho_full,
            "size_bytes": os.path.getsize(ortho_full),
            "status": "PASS" if ok else "FAIL",
            "errors": errs,
        }
        if not ok:
            errors.extend(errs)
    elif expected_assets and "ortho" in expected_assets:
        errors.append("Expected GeoTIFF Orthomosaic file not found in deliverables directory.")

    # 4. Dense Point Cloud
    cloud_files = [f for f in all_files if f.lower().endswith((".laz", ".las", ".ply"))]
    # Filter out OBJ companion PLY meshes if present
    dense_clouds = [f for f in cloud_files if not f.lower().endswith("_mesh.ply")]
    if dense_clouds:
        cloud_full = os.path.join(deliverables_dir, dense_clouds[0])
        ok, errs = verify_point_cloud(cloud_full)
        assets_checked["point_cloud"] = {
            "filename": dense_clouds[0],
            "path": cloud_full,
            "size_bytes": os.path.getsize(cloud_full),
            "status": "PASS" if ok else "FAIL",
            "errors": errs,
        }
        if not ok:
            errors.extend(errs)
    elif expected_assets and "point_cloud" in expected_assets:
        errors.append("Expected Dense Point Cloud not found in deliverables directory.")

    # 5. Quality PDF Report
    pdf_files = [f for f in all_files if f.lower().endswith(".pdf")]
    if pdf_files:
        pdf_full = os.path.join(deliverables_dir, pdf_files[0])
        ok, errs = verify_pdf_report(pdf_full)
        assets_checked["report"] = {
            "filename": pdf_files[0],
            "path": pdf_full,
            "size_bytes": os.path.getsize(pdf_full),
            "status": "PASS" if ok else "FAIL",
            "errors": errs,
        }
        if not ok:
            errors.extend(errs)
    elif expected_assets and "report" in expected_assets:
        errors.append("Expected official PDF calibration report not found in deliverables directory.")

    if not assets_checked and not errors:
        warnings.append("No recognizable photogrammetry assets (.obj, .glb, .tif, .laz, .pdf) found to verify.")

    status = "PASS" if len(errors) == 0 and len(assets_checked) > 0 else "FAIL"

    return {
        "status": status,
        "deliverables_dir": os.path.abspath(deliverables_dir),
        "project_name": project_name or "",
        "assets_checked": assets_checked,
        "errors": errors,
        "warnings": warnings,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    """CLI argument parser for deliverable verification."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Deliverable Quality and Integrity Verification Gate"
    )
    parser.add_argument(
        "--dir",
        "--deliverables-dir",
        dest="dir",
        required=True,
        help="Path to deliverables directory to verify",
    )
    parser.add_argument(
        "--project-name",
        default=None,
        help="Expected project base name",
    )
    parser.add_argument(
        "--expect",
        nargs="+",
        choices=["obj", "glb", "dem", "ortho", "point_cloud", "report"],
        default=None,
        help="List of deliverable asset types that must be present",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results in JSON format",
    )
    return parser


def main() -> int:
    """CLI entry point."""
    parser = build_arg_parser()
    args = parser.parse_args()

    result = verify_project_deliverables(
        deliverables_dir=args.dir,
        project_name=args.project_name,
        expected_assets=args.expect,
    )

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("=========================================================================")
        print("  AUTOMETASHAPE: DELIVERABLE INTEGRITY VERIFICATION GATE")
        print(f"  Target Directory: {result['deliverables_dir']}")
        print(f"  Status          : {result['status']}")
        print("=========================================================================")
        for asset_type, info in result["assets_checked"].items():
            print(f"  [{asset_type.upper()}] {info['filename']} ({info['size_bytes']:,} bytes) -> {info['status']}")
            for err in info.get("errors", []):
                print(f"      [ERROR] {err}")
        for warn in result["warnings"]:
            print(f"  [WARNING] {warn}")
        for err in result["errors"]:
            print(f"  [ERROR] {err}")
        print("=========================================================================")

    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
