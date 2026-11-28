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
import math
import os
import re
import struct
import sys
import zipfile
from typing import Any, Dict, List, Optional, Tuple

_GENERIC_ASSET_STEMS = frozenset(
    {
        "model",
        "building_reconstruction",
        "orthomosaic",
        "ortho",
        "dem",
        "dsm",
        "elevation",
        "point_cloud",
        "points",
        "report",
        "quality_report",
    }
)

_MTL_MAP_OPT_ARITY = {
    "-blendu": 1,
    "-blendv": 1,
    "-cc": 1,
    "-clamp": 1,
    "-mm": 2,
    "-o": 3,
    "-s": 3,
    "-t": 3,
    "-texres": 1,
    "-bm": 1,
    "-imfchan": 1,
}


def _extract_mtl_map_kd_filename(line: str) -> str:
    """Parse texture filename from an MTL ``map_Kd`` directive, skipping known optional flags."""
    stripped = line.strip()
    if not stripped.startswith("map_Kd"):
        return ""
    rest = stripped[6:]
    if not rest or not rest[0].isspace():
        return ""
    matches = list(re.finditer(r"\S+", rest))
    i = 0
    while i < len(matches):
        opt = matches[i].group(0).lower()
        if opt not in _MTL_MAP_OPT_ARITY:
            break
        arity = _MTL_MAP_OPT_ARITY[opt]
        i += 1
        skipped = 0
        while i < len(matches) and skipped < arity and matches[i].group(0).lower() not in _MTL_MAP_OPT_ARITY:
            tok = matches[i].group(0)
            if opt in ("-o", "-s", "-t") and skipped >= 1:
                try:
                    float(tok)
                except ValueError:
                    break
            i += 1
            skipped += 1
    return rest[matches[i].start() :].strip() if i < len(matches) else ""


def verify_obj_model(obj_path: str, min_size_bytes: int = 1000) -> Tuple[bool, List[str]]:
    """Validates Wavefront OBJ model integrity, vertex/face definitions, and MTL texture linking.

    Uses O(1) header+tail seek reads on large OBJ files (100-500 MB) to verify ``mtllib``,
    ``v `` vertices, and ``f `` polygons in milliseconds over SMB without streaming millions of lines.
    """
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

    def _scan_text_block(text: str) -> None:
        nonlocal has_vertex, has_face
        for line in text.splitlines():
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
                    mtl = parts[1].strip()
                    if mtl not in mtl_filenames:
                        mtl_filenames.append(mtl)

    try:
        window = 65536
        with open(obj_path, "rb") as bf:
            head_bytes = bf.read(window)
            head_text = head_bytes.decode("utf-8", errors="ignore")
            if size > window and "\n" in head_text:
                head_text = head_text.rsplit("\n", 1)[0]
            _scan_text_block(head_text)
            if size > window and (not has_face or not has_vertex):
                bf.seek(max(0, size - window))
                tail_bytes = bf.read(window)
                tail_text = tail_bytes.decode("utf-8", errors="ignore")
                if "\n" in tail_text:
                    tail_text = tail_text.split("\n", 1)[1]
                _scan_text_block(tail_text)

        # Full fallback scan only if head+tail did not contain both vertex and face lines
        if not (has_vertex and has_face) and size > 2 * window:
            with open(obj_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if stripped.startswith("v "):
                        has_vertex = True
                    elif stripped.startswith("f "):
                        has_face = True
                    elif stripped.startswith("mtllib "):
                        parts = stripped.split(None, 1)
                        if len(parts) > 1 and parts[1].strip() not in mtl_filenames:
                            mtl_filenames.append(parts[1].strip())
                    if has_vertex and has_face:
                        break
    except Exception as exc:
        return False, [f"Failed to read OBJ file: {exc}"]

    if not has_vertex:
        errors.append("OBJ file contains no vertex ('v ') definitions.")
    if not has_face:
        errors.append("OBJ file contains no face ('f ') polygon definitions.")

    # Validate Material & Texture Links
    base_real = os.path.realpath(base_dir)
    for mtl_name in mtl_filenames:
        mtl_parts = [p for p in mtl_name.replace("\\", "/").split("/") if p]
        mtl_path = os.path.join(base_dir, *mtl_parts) if mtl_parts else ""
        try:
            mtl_ok = bool(mtl_path) and os.path.commonpath([base_real, os.path.realpath(mtl_path)]) == base_real
        except ValueError:
            mtl_ok = False
        if not mtl_ok or not os.path.isfile(mtl_path):
            errors.append(f"Linked MTL material library does not exist or escapes base dir: {mtl_path or mtl_name}")
            continue

        try:
            with open(mtl_path, "r", encoding="utf-8", errors="ignore") as mf:
                for mline in mf:
                    tex_file = _extract_mtl_map_kd_filename(mline)
                    if tex_file:
                        tex_parts = [p for p in tex_file.replace("\\", "/").split("/") if p]
                        tex_path = os.path.join(base_dir, *tex_parts) if tex_parts else ""
                        try:
                            tex_in_base = (
                                bool(tex_path)
                                and os.path.commonpath([base_real, os.path.realpath(tex_path)]) == base_real
                            )
                        except ValueError:
                            tex_in_base = False
                        if not tex_in_base or not os.path.isfile(tex_path):
                            errors.append(f"MTL references missing texture atlas file: {tex_path or tex_file}")
                        elif os.path.getsize(tex_path) < 1000:
                            errors.append(f"Referenced texture file is undersized/empty: {tex_path}")
        except Exception as m_exc:
            errors.append(f"Failed to parse linked MTL material file '{mtl_path}': {m_exc}")

    return len(errors) == 0, errors


def verify_glb_model(glb_path: str, min_size_bytes: int = 1000) -> Tuple[bool, List[str]]:
    """Validates Binary glTF (.glb) container structure, binary header magic, and declared byte length."""
    if not os.path.isfile(glb_path):
        return False, [f"GLB file not found: {glb_path}"]

    size = os.path.getsize(glb_path)
    if size < min_size_bytes:
        return False, [f"GLB file size ({size} bytes) is below minimum threshold ({min_size_bytes} bytes)"]

    try:
        with open(glb_path, "rb") as f:
            hdr = f.read(12)
            if len(hdr) < 12 or hdr[:4] != b"glTF":
                return False, [f"Invalid GLB magic header: expected b'glTF', got {hdr[:4]!r}"]
            glb_version, declared_len = struct.unpack("<II", hdr[4:12])
            if glb_version != 2:
                return False, [f"Unsupported/invalid GLB version: expected 2, got {glb_version}"]
            if declared_len < 12 or declared_len != size:
                return False, [
                    f"Truncated/corrupted GLB container: header declares {declared_len} bytes, file is {size} bytes"
                ]
            if size >= 20:
                hdr0 = f.read(8)
                if len(hdr0) == 8:
                    chunk_len, chunk_type = struct.unpack("<I4s", hdr0)
                    if chunk_len > 0 or chunk_type != b"\x00\x00\x00\x00":
                        if chunk_type != b"JSON" or 20 + chunk_len > declared_len:
                            return False, [
                                f"Invalid/truncated GLB JSON chunk header: type={chunk_type!r}, len={chunk_len}"
                            ]
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


def verify_point_cloud(cloud_path: str, min_size_bytes: int = 256) -> Tuple[bool, List[str]]:
    """Validates Dense Point Cloud (LAZ, LAS, PLY) structure and header magic."""
    if not os.path.isfile(cloud_path):
        return False, [f"Point cloud file not found: {cloud_path}"]

    size = os.path.getsize(cloud_path)
    if size < min_size_bytes:
        return False, [f"Point cloud size ({size} bytes) is below minimum threshold ({min_size_bytes} bytes)"]

    ext = os.path.splitext(cloud_path)[1].lower()
    try:
        with open(cloud_path, "rb") as f:
            header = f.read(8)
            if ext in (".las", ".laz"):
                if header[:4] != b"LASF":
                    return False, [f"Invalid LAS/LAZ magic header: expected b'LASF', got {header[:4]!r}"]
            elif ext == ".ply":
                if not header.startswith(b"ply"):
                    return False, [f"Invalid PLY magic header: expected b'ply', got {header[:4]!r}"]
            elif ext == ".e57":
                if header != b"ASTM-E57":
                    return False, [f"Invalid E57 magic header: expected b'ASTM-E57', got {header!r}"]
            elif ext == ".xyz":
                f.seek(0)
                sample = f.read(16384)
                if b"\x00" in sample[:4096]:
                    return False, ["Invalid XYZ point cloud: binary NUL bytes in ASCII point stream"]
                valid_pts = 0
                lines_checked = 0
                for raw_line in sample.decode("utf-8", errors="ignore").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith(("#", "//")):
                        continue
                    lines_checked += 1
                    parts = [p for p in re.split(r"[\s,;]+", line) if p]
                    if len(parts) >= 3:
                        try:
                            vals = [float(parts[i]) for i in range(3)]
                            if all(math.isfinite(v) for v in vals):
                                valid_pts += 1
                                break
                        except ValueError:
                            pass
                    if lines_checked >= 64:
                        break
                if valid_pts == 0:
                    return False, ["Invalid XYZ point cloud: no valid X Y Z numeric coordinate triples found"]
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
            f.seek(max(0, size - 2048))
            tail = f.read(2048)
            if b"%%EOF" not in tail:
                return False, ["Truncated/corrupted PDF report: missing '%%EOF' trailer marker."]
    except Exception as exc:
        return False, [f"Failed to read PDF report header: {exc}"]

    return True, []


def verify_psx_project(psx_path: str) -> Tuple[bool, List[str]]:
    """Validate a Metashape .psx XML manifest and its companion .files/project.zip archive."""
    if not os.path.isfile(psx_path):
        return False, [f"PSX file not found: {psx_path}"]
    if os.path.getsize(psx_path) <= 0:
        return False, [f"PSX file is empty: {psx_path}"]
    try:
        with open(psx_path, "r", encoding="utf-8", errors="replace") as f:
            xml_txt = f.read(65536)
        if "<document" not in xml_txt:
            return False, [f"Invalid PSX manifest (missing '<document'): {psx_path}"]
    except Exception as exc:
        return False, [f"Failed to read PSX manifest '{psx_path}': {exc}"]

    files_dir = psx_path[:-4] + ".files" if psx_path.lower().endswith(".psx") else psx_path + ".files"
    project_zip = os.path.join(files_dir, "project.zip")
    if not os.path.isfile(project_zip):
        return False, [f"Missing Metashape project archive: {project_zip}"]
    try:
        with zipfile.ZipFile(project_zip, "r") as zf:
            bad = zf.testzip()
            if bad is not None:
                return False, [f"Corrupt ZIP entry '{bad}' in {project_zip}"]
            if "doc.xml" not in zf.namelist():
                return False, [f"Missing doc.xml inside {project_zip}"]
    except Exception as exc:
        return False, [f"Failed to read project.zip '{project_zip}': {exc}"]

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

    # Gather sorted regular files in deliverables directory (skip subdirs & broken symlinks)
    all_files = sorted(f for f in os.listdir(deliverables_dir) if os.path.isfile(os.path.join(deliverables_dir, f)))

    def _prefer_project_match(candidates: List[str]) -> Optional[str]:
        if not candidates:
            return None
        if not project_name:
            return candidates[0]
        pref = project_name.lower()

        def _rank(fname: str) -> tuple[int, str]:
            stem = os.path.splitext(fname)[0].lower()
            if stem == pref:
                return (0, fname.lower())
            if fname.lower().startswith(pref + "_"):
                return (1, fname.lower())
            if fname.lower().startswith(pref + "-"):
                return (2, fname.lower())
            if stem in _GENERIC_ASSET_STEMS:
                return (3, fname.lower())
            return (99, fname.lower())

        ranked = sorted((c for c in candidates if _rank(c)[0] < 99), key=_rank)
        return ranked[0] if ranked else None

    # Collect texture filenames referenced by MTL libraries so TIFF textures are never misclassified as DEM/Ortho
    mtl_referenced_tifs: set[str] = set()
    for mf_name in (f for f in all_files if f.lower().endswith(".mtl")):
        try:
            with open(os.path.join(deliverables_dir, mf_name), "r", encoding="utf-8", errors="ignore") as mf:
                for mline in mf:
                    tex_file = _extract_mtl_map_kd_filename(mline)
                    if tex_file:
                        mtl_referenced_tifs.add(os.path.basename(tex_file.replace("\\", "/")).lower())
        except Exception:
            pass

    # 1. 3D Model: OBJ + MTL
    obj_files = [f for f in all_files if f.lower().endswith(".obj")]
    target_obj = _prefer_project_match(obj_files)
    if target_obj:
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
    target_glb = _prefer_project_match(glb_files)
    if target_glb:
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

    # 3. Elevation & Orthomosaic (GeoTIFF — classify explicit _dem/_dsm suffixes before generic ortho tokens)
    tif_files = [f for f in all_files if f.lower().endswith((".tif", ".tiff")) and f.lower() not in mtl_referenced_tifs]
    dem_files = [f for f in tif_files if f.lower().endswith(("_dem.tif", "_dem.tiff", "_dsm.tif", "_dsm.tiff"))]
    dem_set = {f.lower() for f in dem_files}
    ortho_files = [
        f
        for f in tif_files
        if f.lower() not in dem_set
        and f.lower().endswith(("_orthomosaic.tif", "_orthomosaic.tiff", "_ortho.tif", "_ortho.tiff"))
    ]
    ortho_set = {f.lower() for f in ortho_files}
    for fname in tif_files:
        low_f = fname.lower()
        if low_f in dem_set or low_f in ortho_set:
            continue
        if re.search(r"(?:^|[_-])(?:dem|dsm|elevation)(?:[_.-]|$)", low_f) is not None:
            dem_files.append(fname)
            dem_set.add(low_f)
        elif re.search(r"(?:^|[_-])(?:orthomosaic|ortho)(?:[_.-]|$)", low_f) is not None:
            ortho_files.append(fname)
            ortho_set.add(low_f)

    target_dem = _prefer_project_match(dem_files)
    if target_dem:
        dem_full = os.path.join(deliverables_dir, target_dem)
        ok, errs = verify_geotiff(dem_full)
        assets_checked["dem"] = {
            "filename": target_dem,
            "path": dem_full,
            "size_bytes": os.path.getsize(dem_full),
            "status": "PASS" if ok else "FAIL",
            "errors": errs,
        }
        if not ok:
            errors.extend(errs)
    elif expected_assets and "dem" in expected_assets:
        errors.append("Expected GeoTIFF DEM file not found in deliverables directory.")

    target_ortho = _prefer_project_match(ortho_files)
    if target_ortho:
        ortho_full = os.path.join(deliverables_dir, target_ortho)
        ok, errs = verify_geotiff(ortho_full)
        assets_checked["ortho"] = {
            "filename": target_ortho,
            "path": ortho_full,
            "size_bytes": os.path.getsize(ortho_full),
            "status": "PASS" if ok else "FAIL",
            "errors": errs,
        }
        if not ok:
            errors.extend(errs)
    elif expected_assets and "ortho" in expected_assets:
        errors.append("Expected GeoTIFF Orthomosaic file not found in deliverables directory.")

    # 4. Dense Point Cloud (exclude _mesh.ply and _tie_points.ply; support .e57 and .xyz)
    cloud_files = [f for f in all_files if f.lower().endswith((".laz", ".las", ".ply", ".e57", ".xyz"))]
    dense_clouds = [
        f for f in cloud_files if not f.lower().endswith("_mesh.ply") and not f.lower().endswith("_tie_points.ply")
    ]
    target_cloud = _prefer_project_match(dense_clouds)
    if target_cloud:
        cloud_full = os.path.join(deliverables_dir, target_cloud)
        ok, errs = verify_point_cloud(cloud_full)
        assets_checked["point_cloud"] = {
            "filename": target_cloud,
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
    target_pdf = _prefer_project_match(pdf_files)
    if target_pdf:
        pdf_full = os.path.join(deliverables_dir, target_pdf)
        ok, errs = verify_pdf_report(pdf_full)
        assets_checked["report"] = {
            "filename": target_pdf,
            "path": pdf_full,
            "size_bytes": os.path.getsize(pdf_full),
            "status": "PASS" if ok else "FAIL",
            "errors": errs,
        }
        if not ok:
            errors.extend(errs)
    elif expected_assets and "report" in expected_assets:
        errors.append("Expected official PDF calibration report not found in deliverables directory.")

    # 6. Native Metashape Project Archive (.psx + .files/project.zip)
    psx_files = [f for f in all_files if f.lower().endswith(".psx")]
    should_check_psx = bool(expected_assets and "psx" in expected_assets) or (
        bool(psx_files) and not assets_checked and not expected_assets
    )
    if should_check_psx:
        target_psx = _prefer_project_match(psx_files)
        if target_psx:
            psx_full = os.path.join(deliverables_dir, target_psx)
            ok, errs = verify_psx_project(psx_full)
            assets_checked["psx"] = {
                "filename": target_psx,
                "path": psx_full,
                "size_bytes": os.path.getsize(psx_full),
                "status": "PASS" if ok else "FAIL",
                "errors": errs,
            }
            if not ok:
                errors.extend(errs)
        elif expected_assets and "psx" in expected_assets:
            errors.append("Expected Metashape project (.psx) not found in deliverables directory.")

    if not assets_checked and not errors:
        warnings.append("No recognizable photogrammetry assets (.obj, .glb, .tif, .laz, .pdf, .psx) found to verify.")

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
    parser = argparse.ArgumentParser(description="AutoMetashape Deliverable Quality and Integrity Verification Gate")
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
        choices=["obj", "glb", "dem", "ortho", "point_cloud", "report", "psx"],
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
    try:
        from autometashape.ipc.protocol import configure_utf8_stdio

        configure_utf8_stdio()
    except ImportError:
        pass
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
