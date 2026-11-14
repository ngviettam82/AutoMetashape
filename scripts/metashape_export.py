# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape Production Asset Export CLI & Backward-Compatibility Facade.

Decoupled export engine for Agisoft Metashape Professional projects (.psx).
Exports 3D models (GLB, OBJ, FBX, PLY, DXF), dense point clouds (LAZ, LAS, PLY, E57),
3D tiles (Cesium, TLS, SLPK), elevation models (DEM), topographic contours (SHP, DXF),
orthomosaics, and calibration reports.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

# Ensure canonical source/ directory is available on sys.path
_SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "source"))
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

try:
    import Metashape
except ImportError:
    Metashape = None

import autometashape.core.export as _export_mod
from autometashape.core.export import (
    POINT_CLOUD_FORMAT_MAP,
    SHAPES_FORMAT_MAP,
    TILED_MODEL_FORMAT_MAP,
    infer_project_name,
)
from autometashape.ipc.protocol import (
    IPC_COMPLETE,
    IPC_PAUSE_CROP,
    IPC_SENTINEL_PREFIX,
    IPC_SENTINEL_SUFFIX,
    emit_ipc_error,
    emit_ipc_event,
    emit_ipc_stage,
    set_active_status_file,
)


def export_project_assets(*args, **kwargs):
    """Facade for export_project_assets ensuring compatibility with module-level Metashape mocks."""
    _export_mod.Metashape = Metashape
    if Metashape is not None and getattr(Metashape, "PointCloudFormat", None) is not None:
        _export_mod.POINT_CLOUD_FORMAT_MAP = {
            "las": (Metashape.PointCloudFormat.PointCloudFormatLAS, ".las"),
            "laz": (Metashape.PointCloudFormat.PointCloudFormatLAZ, ".laz"),
            "ply": (Metashape.PointCloudFormat.PointCloudFormatPLY, ".ply"),
            "e57": (Metashape.PointCloudFormat.PointCloudFormatE57, ".e57"),
            "xyz": (Metashape.PointCloudFormat.PointCloudFormatXYZ, ".xyz"),
        }
    return _export_mod.export_project_assets(*args, **kwargs)


def build_arg_parser() -> argparse.ArgumentParser:
    """Constructs command-line argument parser for standalone production asset export."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Export CLI: Export production 3D assets from existing Metashape project (.psx)"
    )
    parser.add_argument("--project", required=True, help="Path to existing Metashape project file (.psx)")
    parser.add_argument(
        "--output", default=None, help="Output directory for exported 3D assets (default: same folder as .psx)"
    )
    parser.add_argument(
        "--project-name",
        default=None,
        help="Base name for exported 3D model, textures, point cloud, and reports (default: folder project name)",
    )
    parser.add_argument(
        "--texture-format",
        choices=["png", "tiff", "tif", "jpg", "jpeg"],
        default="png",
        help="Texture atlas format (default: png)",
    )
    parser.add_argument(
        "--export-glb",
        dest="export_glb",
        action="store_true",
        default=True,
        help="Export binary glTF (.glb) model (default: True)",
    )
    parser.add_argument("--no-export-glb", dest="export_glb", action="store_false", help="Disable binary glTF (.glb) export")
    parser.add_argument(
        "--export-obj",
        dest="export_obj",
        action="store_true",
        default=True,
        help="Export Wavefront OBJ (.obj) model (default: True)",
    )
    parser.add_argument("--no-export-obj", dest="export_obj", action="store_false", help="Disable Wavefront OBJ (.obj) export")
    parser.add_argument(
        "--rebake-texture",
        action="store_true",
        default=False,
        help="Re-bake UV parameterization and 8K texture atlas before export",
    )
    parser.add_argument(
        "--texture-pages", type=int, default=16, help="Number of 8K texture atlas pages if re-baking (default: 16)"
    )
    parser.add_argument(
        "--no-fill-holes",
        dest="fill_holes",
        action="store_false",
        default=True,
        help="Disable texture hole filling if re-baking (default: enabled)",
    )
    parser.add_argument(
        "--no-ghosting-filter",
        dest="ghosting_filter",
        action="store_false",
        default=True,
        help="Disable ghosting filter if re-baking (default: enabled)",
    )
    parser.add_argument(
        "--import-model", default=None, help="Optional path to external 3D model (.obj) to import/re-link into the project"
    )
    parser.add_argument(
        "--export-point-cloud", dest="export_point_cloud", action="store_true", default=False, help="Export dense point cloud file"
    )
    parser.add_argument(
        "--no-export-point-cloud", dest="export_point_cloud", action="store_false", help="Disable dense point cloud file export"
    )
    parser.add_argument(
        "--point-cloud-format",
        choices=["laz", "las", "ply", "e57", "xyz"],
        default="laz",
        help="Format for exported dense point cloud (default: laz)",
    )
    parser.add_argument(
        "--export-dem",
        dest="export_dem",
        action="store_true",
        default=False,
        help="Export GeoTIFF Digital Elevation Model (dem.tif)",
    )
    parser.add_argument(
        "--no-export-dem", dest="export_dem", action="store_false", help="Disable GeoTIFF Digital Elevation Model export"
    )
    parser.add_argument(
        "--export-orthomosaic",
        dest="export_orthomosaic",
        action="store_true",
        default=False,
        help="Export GeoTIFF Orthomosaic (orthomosaic.tif)",
    )
    parser.add_argument(
        "--no-export-orthomosaic", dest="export_orthomosaic", action="store_false", help="Disable GeoTIFF Orthomosaic export"
    )
    parser.add_argument(
        "--export-report",
        dest="export_report",
        action="store_true",
        default=False,
        help="Export official Metashape PDF calibration & quality report",
    )
    parser.add_argument(
        "--no-report",
        "--no-export-report",
        dest="export_report",
        action="store_false",
        help="Disable PDF quality report export",
    )
    parser.add_argument("--report-title", default=None, help="Custom title for PDF quality report")
    parser.add_argument(
        "--export-tiled-model", action="store_true", default=False, help="Export hierarchical 3D tiled model (LOD)"
    )
    parser.add_argument(
        "--tiled-model-format",
        choices=["cesium", "tls", "slpk", "osgb"],
        default="cesium",
        help="Format for 3D tiled model (default: cesium)",
    )
    parser.add_argument("--tile-size", type=int, default=256, help="Tile size in pixels (default: 256)")
    parser.add_argument(
        "--export-contours", action="store_true", default=False, help="Generate and export topographic contour lines from DEM"
    )
    parser.add_argument(
        "--contour-interval", type=float, default=1.0, help="Contour elevation interval in meters (default: 1.0)"
    )
    parser.add_argument(
        "--contours-format",
        choices=["shp", "dxf", "geojson", "kml"],
        default="shp",
        help="Format for topographic contours (default: shp)",
    )
    parser.add_argument(
        "--remove-lighting",
        action="store_true",
        default=False,
        help="Remove shadows and ambient lighting from texture (delighting)",
    )
    parser.add_argument(
        "--delighting-color-mode", action="store_true", default=False, help="Enable multi-color delighting mode"
    )
    parser.add_argument(
        "--calibrate-colors", action="store_true", default=False, help="Perform radiometric color & white balance calibration"
    )
    parser.add_argument(
        "--smooth-mesh", action="store_true", default=False, help="Apply Laplacian smoothing to surface mesh"
    )
    parser.add_argument(
        "--smooth-strength", type=float, default=3.0, help="Laplacian smoothing strength (default: 3.0)"
    )
    parser.add_argument("--decimate-mesh", type=int, default=None, help="Decimate mesh to target face count")
    parser.add_argument("--export-fbx", action="store_true", default=False, help="Export 3D model in Autodesk FBX format")
    parser.add_argument("--export-ply", action="store_true", default=False, help="Export 3D model in Stanford PLY format")
    parser.add_argument("--export-dxf", action="store_true", default=False, help="Export 3D model in AutoCAD DXF format")
    parser.add_argument(
        "--export-tie-points", action="store_true", default=False, help="Export sparse tie points in PLY format"
    )
    return parser


if __name__ == "__main__":
    parser = build_arg_parser()
    args = parser.parse_args()

    # Seamless auto-handoff: if executed in standard Python, launch via Metashape Pro
    if Metashape is None:
        try:
            import cluster_utils as cu

            ms_exe = cu.find_metashape_path()
        except Exception:
            ms_exe = None

        if ms_exe and os.path.isfile(ms_exe):
            print("[AutoMetashape] Metashape Python environment not detected in current interpreter.")
            print(f"[AutoMetashape] Automatically launching via Metashape Pro: {ms_exe}", flush=True)
            relaunch_cmd = [ms_exe, "-r", os.path.abspath(__file__)] + sys.argv[1:]
            proc = subprocess.run(relaunch_cmd)
            sys.exit(proc.returncode)
        else:
            print(
                "[FATAL ERROR] Agisoft Metashape Python module is not loaded and metashape.exe was not found.",
                file=sys.stderr,
            )
            print("Please ensure Agisoft Metashape Professional is installed, or execute via:", file=sys.stderr)
            print(
                f"  & \"<path_to_metashape.exe>\" -r scripts/metashape_export.py {' '.join(sys.argv[1:])}",
                file=sys.stderr,
            )
            sys.exit(1)

    export_project_assets(
        project_path=args.project,
        output_dir=args.output,
        texture_format=args.texture_format,
        rebake_texture=args.rebake_texture,
        texture_pages=args.texture_pages,
        fill_holes=args.fill_holes,
        ghosting_filter=args.ghosting_filter,
        import_model_path=args.import_model,
        export_point_cloud=args.export_point_cloud,
        point_cloud_format=args.point_cloud_format,
        export_dem=args.export_dem,
        export_orthomosaic=args.export_orthomosaic,
        export_report=args.export_report,
        report_title=args.report_title,
        export_tiled_model=args.export_tiled_model,
        tiled_model_format=args.tiled_model_format,
        tile_size=args.tile_size,
        export_contours=args.export_contours,
        contour_interval=args.contour_interval,
        contours_format=args.contours_format,
        remove_lighting=args.remove_lighting,
        delighting_color_mode=args.delighting_color_mode,
        calibrate_colors=args.calibrate_colors,
        smooth_mesh=args.smooth_mesh,
        smooth_strength=args.smooth_strength,
        decimate_mesh=args.decimate_mesh,
        export_glb=args.export_glb,
        export_obj=args.export_obj,
        export_fbx=args.export_fbx,
        export_ply=args.export_ply,
        export_dxf=args.export_dxf,
        export_tie_points=args.export_tie_points,
        project_name=args.project_name,
    )
