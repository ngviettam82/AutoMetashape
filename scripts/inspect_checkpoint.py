# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical Checkpoint Forensics & Diagnostic Inspection CLI.

Safely opens Agisoft Metashape (.psx) project archives in read-only mode,
diagnosing chunk structures, camera alignment counts, tie points, depth maps,
and dense models without altering or clearing project data.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Any, Dict, List

try:
    import Metashape
except ImportError:
    Metashape = None

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_SCRIPTS_DIR, ".."))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

from autometashape.cluster.hardware import find_metashape_path


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for checkpoint inspection."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Canonical Checkpoint Inspection & Diagnostic Utility",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--project",
        type=str,
        required=True,
        help="Path to Metashape project archive (.psx)",
    )
    parser.add_argument(
        "--no-reexec",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def inspect_project(psx_path: str) -> Dict[str, Any]:
    """Inspect project archive and report chunk structure non-destructively."""
    if Metashape is None:
        raise RuntimeError("Metashape module not loaded. Run via metashape.exe -r.")

    norm_path = os.path.normpath(psx_path.strip())
    if not os.path.isfile(norm_path):
        raise FileNotFoundError(f"Project archive does not exist: {norm_path}")

    doc = Metashape.Document()
    doc.open(norm_path)

    chunks_info: List[Dict[str, Any]] = []
    print("=" * 80)
    print(f"AUTOMETASHAPE CHECKPOINT FORENSICS: {norm_path}")
    print("=" * 80)
    print(f"Total Chunks Found: {len(doc.chunks)}")

    for idx, c in enumerate(doc.chunks):
        label = getattr(c, "label", f"Chunk {idx + 1}")
        cams = getattr(c, "cameras", None) or []
        total_cams = len(cams)
        aligned_cams = sum(1 for cam in cams if getattr(cam, "transform", None) and getattr(cam, "enabled", True))

        # Tie points
        tie_points = 0
        pc = getattr(c, "point_cloud", None)
        if pc is not None:
            pts = getattr(pc, "points", None)
            tie_points = len(pts) if pts is not None else 0

        # Depth maps
        depth_maps = 0
        dms = getattr(c, "depth_maps", None)
        if dms is not None:
            depth_maps = len(dms)

        # Dense point cloud
        dense_points = 0
        dpc = getattr(c, "dense_cloud", None) or getattr(c, "point_cloud_dense", None)
        if dpc is not None:
            dense_points = getattr(dpc, "point_count", 0)

        # Mesh Model
        model_faces = 0
        model_vertices = 0
        model = getattr(c, "model", None)
        if model is not None:
            faces = getattr(model, "faces", None)
            verts = getattr(model, "vertices", None)
            model_faces = len(faces) if faces is not None else 0
            model_vertices = len(verts) if verts is not None else 0

        # Orthomosaic
        has_ortho = getattr(c, "orthomosaic", None) is not None
        has_elevation = getattr(c, "elevation", None) is not None

        c_data = {
            "index": idx,
            "label": label,
            "total_cameras": total_cams,
            "aligned_cameras": aligned_cams,
            "tie_points": tie_points,
            "depth_maps": depth_maps,
            "dense_points": dense_points,
            "model_faces": model_faces,
            "model_vertices": model_vertices,
            "has_orthomosaic": has_ortho,
            "has_elevation": has_elevation,
        }
        chunks_info.append(c_data)

        print(f"\n[Chunk {idx + 1}] Label: '{label}'")
        print(
            f"  - Cameras       : {aligned_cams} aligned / {total_cams} total ({aligned_cams / total_cams * 100:.1f}%)"
            if total_cams
            else "  - Cameras: 0"
        )
        print(f"  - Tie Points    : {tie_points:,}")
        print(f"  - Depth Maps    : {depth_maps:,}")
        print(f"  - Dense Points  : {dense_points:,}")
        print(
            f"  - 3D Mesh Model : {model_faces:,} faces, {model_vertices:,} vertices"
            if model_faces
            else "  - 3D Mesh Model: None"
        )
        print(f"  - Orthomosaic   : {'Present' if has_ortho else 'None'}")
        print(f"  - Elevation (DEM): {'Present' if has_elevation else 'None'}")

    print("\n" + "=" * 80)
    print("CHECKPOINT INSPECTION COMPLETE.")
    print("=" * 80)
    return {"project_path": norm_path, "chunks": chunks_info}


def main() -> int:
    """CLI entry point for checkpoint forensics."""
    parser = build_arg_parser()
    args = parser.parse_args()

    # Plain Python Auto-Delegation
    if Metashape is None and not args.no_reexec:
        ms_exe = find_metashape_path()
        if ms_exe and os.path.isfile(ms_exe):
            cmd = [ms_exe, "-r", os.path.abspath(__file__)] + sys.argv[1:] + ["--no-reexec"]
            res = subprocess.run(cmd)
            return res.returncode
        print("[ERROR] Agisoft Metashape Pro executable not found. Cannot inspect checkpoint.", file=sys.stderr)
        return 1

    try:
        inspect_project(args.project)
        return 0
    except Exception as e:
        print(f"[FATAL ERROR] Checkpoint inspection failed: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
