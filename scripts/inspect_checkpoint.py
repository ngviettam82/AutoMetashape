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
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
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
from autometashape.core.crs_utils import classify_crs
from autometashape.core.pipeline import _get_model_texture_page_count
from autometashape.ipc.protocol import configure_utf8_stdio


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
        "--json",
        action="store_true",
        help="Emit checkpoint summary as structured JSON",
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
    try:
        try:
            doc.open(norm_path, read_only=True, ignore_lock=True)
        except TypeError:
            try:
                doc.open(norm_path, read_only=True)
            except TypeError as exc:
                raise RuntimeError(
                    "Metashape Document.open() does not support read_only=True; refusing unsafe read-write open."
                ) from exc

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
            aligned_disabled_cams = sum(
                1 for cam in cams if getattr(cam, "transform", None) and not getattr(cam, "enabled", True)
            )

            # Tie points (Metashape 2.x uses chunk.tie_points; 1.x used chunk.point_cloud)
            tie_points = 0
            tp = getattr(c, "tie_points", None)
            if tp is not None:
                pts = getattr(tp, "points", None)
                tie_points = len(pts) if pts is not None else 0
            else:
                legacy_pc = getattr(c, "point_cloud", None)
                if legacy_pc is not None and hasattr(legacy_pc, "points"):
                    pts = getattr(legacy_pc, "points", None)
                    tie_points = len(pts) if pts is not None else 0

            # Depth maps (Metashape 2.x DepthMaps container exposes .keys())
            depth_maps = 0
            dms = getattr(c, "depth_maps", None)
            if dms is not None:
                if hasattr(dms, "keys"):
                    try:
                        depth_maps = len(dms.keys())
                    except Exception:
                        depth_maps = 1
                else:
                    try:
                        depth_maps = len(dms)
                    except (TypeError, AttributeError):
                        depth_maps = 1 if dms else 0

            # Dense point cloud (Metashape 2.x uses chunk.point_cloud.point_count; 1.x used dense_cloud)
            dense_points = 0
            pc2 = getattr(c, "point_cloud", None)
            if pc2 is not None and hasattr(pc2, "point_count"):
                dense_points = int(getattr(pc2, "point_count", 0) or 0)
            else:
                dpc = getattr(c, "dense_cloud", None) or getattr(c, "point_cloud_dense", None)
                if dpc is not None:
                    dense_points = int(getattr(dpc, "point_count", 0) or 0)

            # Mesh Model (prefer statistics() to avoid loading gigabytes of mesh buffers into RAM)
            model_faces = 0
            model_vertices = 0
            model = getattr(c, "model", None)
            if model is not None:
                stats_fn = getattr(model, "statistics", None)
                stats_obj = stats_fn() if callable(stats_fn) else None
                if stats_obj is not None:
                    sf = getattr(stats_obj, "faces", None)
                    sv = getattr(stats_obj, "vertices", None)
                    if isinstance(sf, int) and not isinstance(sf, bool):
                        model_faces = sf
                    if isinstance(sv, int) and not isinstance(sv, bool):
                        model_vertices = sv
                if stats_obj is None or (model_faces == 0 and model_vertices == 0):
                    faces = getattr(model, "faces", None)
                    verts = getattr(model, "vertices", None)
                    if not isinstance(faces, int) and faces is not None and model_faces == 0:
                        try:
                            model_faces = len(faces)
                        except TypeError:
                            pass
                    if not isinstance(verts, int) and verts is not None and model_vertices == 0:
                        try:
                            model_vertices = len(verts)
                        except TypeError:
                            pass

            # Orthomosaic, Elevation, CRS, Sensors, Markers & Textures
            has_ortho = getattr(c, "orthomosaic", None) is not None
            has_elevation = getattr(c, "elevation", None) is not None
            c_crs = getattr(c, "crs", None)
            if c_crs is None:
                crs_str = "None"
                crs_kind = "local"
            else:
                crs_name = getattr(c_crs, "name", None)
                crs_wkt = getattr(c_crs, "wkt", None)
                if isinstance(crs_name, str) and crs_name.strip():
                    crs_str = crs_name.strip()
                elif isinstance(crs_wkt, str) and crs_wkt.strip():
                    crs_str = crs_wkt.strip()[:80]
                else:
                    crs_str = "Unrecognized CRS"
                crs_kind = classify_crs(c_crs)
            sensor_count = len(getattr(c, "sensors", None) or [])
            marker_count = len(getattr(c, "markers", None) or [])
            texture_pages = _get_model_texture_page_count(model) if model is not None else 0

            c_data = {
                "index": idx,
                "label": label,
                "total_cameras": total_cams,
                "aligned_cameras": aligned_cams,
                "aligned_disabled_cameras": aligned_disabled_cams,
                "tie_points": tie_points,
                "depth_maps": depth_maps,
                "dense_points": dense_points,
                "model_faces": model_faces,
                "model_vertices": model_vertices,
                "has_orthomosaic": has_ortho,
                "has_elevation": has_elevation,
                "crs": crs_str,
                "crs_kind": crs_kind,
                "sensor_count": sensor_count,
                "marker_count": marker_count,
                "texture_pages": texture_pages,
            }
            chunks_info.append(c_data)

            print(f"\n[Chunk {idx + 1}] Label: '{label}'")
            print(
                f"  - Cameras        : {aligned_cams} aligned / {total_cams} total ({aligned_cams / total_cams * 100:.1f}%)"
                if total_cams
                else "  - Cameras        : 0"
            )
            print(f"  - CRS / Kind     : {crs_str} ({crs_kind})")
            print(f"  - Sensors/Markers: {sensor_count} sensor(s), {marker_count} marker(s)")
            print(f"  - Tie Points     : {tie_points:,}")
            print(f"  - Depth Maps     : {depth_maps:,}")
            print(f"  - Dense Points   : {dense_points:,}")
            print(
                f"  - 3D Mesh Model  : {model_faces:,} faces, {model_vertices:,} vertices ({texture_pages} tex page(s))"
                if model_faces
                else "  - 3D Mesh Model  : None"
            )
            print(f"  - Orthomosaic    : {'Present' if has_ortho else 'None'}")
            print(f"  - Elevation (DEM): {'Present' if has_elevation else 'None'}")

        print("\n" + "=" * 80)
        print("CHECKPOINT INSPECTION COMPLETE.")
        print("=" * 80)
        return {"project_path": norm_path, "chunks": chunks_info}
    finally:
        try:
            doc.clear()
        except Exception:
            pass


def main() -> int:
    """CLI entry point for checkpoint forensics."""
    configure_utf8_stdio()

    parser = build_arg_parser()
    args = parser.parse_args()

    # Plain Python Auto-Delegation
    if Metashape is None and not args.no_reexec:
        ms_exe = find_metashape_path()
        if ms_exe and os.path.isfile(ms_exe):
            cmd = [ms_exe]
            if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
                cmd.extend(["-platform", "offscreen"])
            cmd.extend(["-r", os.path.abspath(__file__), "--no-reexec"] + sys.argv[1:])
            res = subprocess.run(cmd)
            return res.returncode
        print("[ERROR] Agisoft Metashape Pro executable not found. Cannot inspect checkpoint.", file=sys.stderr)
        return 1
    if Metashape is None:
        print("[FATAL ERROR] Agisoft Metashape Python module is unavailable in this runtime.", file=sys.stderr)
        return 1

    try:
        if args.json:
            with redirect_stdout(io.StringIO()):
                info = inspect_project(args.project)
            print(json.dumps(info, indent=2))
        else:
            inspect_project(args.project)
        return 0
    except Exception as e:
        print(f"[FATAL ERROR] Checkpoint inspection failed: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
