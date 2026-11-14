# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape Autonomous Photogrammetry CLI & Backward-Compatibility Facade.

Monolithic runner and backward-compatible entry point for Agisoft Metashape Professional.
Executes autonomous drone survey photogrammetry, tie point filtering, MVS stereopsis,
surface meshing, texture baking, DEM, orthomosaics, and cluster batch orchestration.
"""

from __future__ import annotations

import argparse
import os
import sys

# Ensure canonical source/ directory is available on sys.path
_SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "source"))
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

try:
    import Metashape
except ImportError:
    Metashape = None

# Subsystem modules
import autometashape.cluster.config as _config_mod
import autometashape.cluster.hardware as _hardware_mod
import autometashape.cluster.orchestrator as _orchestrator_mod
import autometashape.cluster.staging as _staging_mod
import autometashape.core.export as _export_mod
import autometashape.core.filtering as _filtering_mod
import autometashape.core.pipeline as _pipeline_mod
import autometashape.core.telemetry as _telemetry_mod
import autometashape.ipc.protocol as _ipc_mod

# IPC Re-exports
from autometashape.ipc.protocol import (
    IPC_COMPLETE,
    IPC_PAUSE_CROP,
    IPC_SENTINEL_PREFIX,
    IPC_SENTINEL_SUFFIX,
    _ACTIVE_STATUS_FILE,
    emit_ipc_error,
    emit_ipc_event,
    emit_ipc_stage,
    set_active_status_file,
)

# Telemetry Re-exports
from autometashape.core.telemetry import (
    CAMERA_HARDWARE_DB,
    SUPPORTED_IMAGE_EXTS,
    analyze_radiometry,
    analyze_survey_physics,
    calculate_haversine_meters,
    compute_utm_epsg,
    derive_pro_photogrammetry_plan,
    discover_image_subfolder_groups,
    extract_image_telemetry,
    parse_xmp_relative_altitude,
    parse_xmp_telemetry,
    print_pro_engineering_report,
)

# Filtering Re-exports
from autometashape.core.filtering import (
    auto_crop_camera_gps_square as _impl_auto_crop_camera_gps_square,
    auto_crop_to_main_structure as _impl_auto_crop_to_main_structure,
    execute_usgs_gradual_selection as _impl_execute_usgs_gradual_selection,
    filter_image_quality as _impl_filter_image_quality,
    filter_tie_points as _impl_filter_tie_points,
    robust_bounds,
    solve_ray_convergence_irls,
)

# Cluster & Orchestrator Re-exports
from autometashape.cluster.orchestrator import (
    ALIGN_DOWNSCALE_MAP,
    DEPTH_DOWNSCALE_MAP,
    DEPTH_FILTER_MAP,
    POINT_CLOUD_FORMAT_MAP,
    PRESEL_MODE_MAP,
    SURFACE_TYPE_MAP,
    TILED_MODEL_FORMAT_MAP,
    _has_dense_points,
    _norm_cluster_path,
    build_cluster_crop_task as _impl_build_cluster_crop_task,
    build_cluster_gradual_selection_tasks as _impl_build_cluster_gradual_selection_tasks,
    build_monolithic_runscript_task as _impl_build_monolithic_runscript_task,
    build_unified_cluster_pipeline_tasks as _impl_build_unified_cluster_pipeline_tasks,
    safe_create_cluster_batch as _impl_safe_create_cluster_batch,
)
from autometashape.cluster.staging import (
    check_scratch_disk_headroom,
    get_optimal_local_scratch_dir,
    get_psx_files_dir,
)
from autometashape.core.export import (
    infer_project_name,
)
from autometashape.core.pipeline import (
    build_arg_parser,
    run_reconstruction as _impl_run_reconstruction,
    save_checkpoint as _impl_save_checkpoint,
    wait_for_user_resume as _impl_wait_for_user_resume,
)


def _sync_state() -> None:
    """Synchronizes module-level test mocks (Metashape, status file, cluster_utils) to subsystems."""
    _pipeline_mod.Metashape = Metashape
    _orchestrator_mod.Metashape = Metashape
    _filtering_mod.Metashape = Metashape
    _export_mod.Metashape = Metashape
    _ipc_mod._ACTIVE_STATUS_FILE = _ACTIVE_STATUS_FILE

    if Metashape is not None:
        if getattr(Metashape, "FilterMode", None) is not None:
            new_dfm = {
                "mild": Metashape.FilterMode.MildFiltering,
                "moderate": Metashape.FilterMode.ModerateFiltering,
                "aggressive": Metashape.FilterMode.AggressiveFiltering,
            }
            _orchestrator_mod.DEPTH_FILTER_MAP = new_dfm
            globals()["DEPTH_FILTER_MAP"] = new_dfm
        if getattr(Metashape, "PointCloudFormat", None) is not None:
            new_pcfm = {
                "las": (Metashape.PointCloudFormat.PointCloudFormatLAS, ".las"),
                "laz": (Metashape.PointCloudFormat.PointCloudFormatLAZ, ".laz"),
                "ply": (Metashape.PointCloudFormat.PointCloudFormatPLY, ".ply"),
                "e57": (Metashape.PointCloudFormat.PointCloudFormatE57, ".e57"),
            }
            _orchestrator_mod.POINT_CLOUD_FORMAT_MAP = new_pcfm
            _export_mod.POINT_CLOUD_FORMAT_MAP = new_pcfm
            globals()["POINT_CLOUD_FORMAT_MAP"] = new_pcfm

    if "cluster_utils" in sys.modules:
        _cu = sys.modules["cluster_utils"]
        if hasattr(_cu, "get_optimal_local_scratch_dir"):
            _pipeline_mod.get_optimal_local_scratch_dir = _cu.get_optimal_local_scratch_dir
            _staging_mod.get_optimal_local_scratch_dir = _cu.get_optimal_local_scratch_dir
        if hasattr(_cu, "check_scratch_disk_headroom"):
            _pipeline_mod.check_scratch_disk_headroom = _cu.check_scratch_disk_headroom
            _staging_mod.check_scratch_disk_headroom = _cu.check_scratch_disk_headroom


def auto_crop_camera_gps_square(*args, **kwargs):
    _sync_state()
    return _impl_auto_crop_camera_gps_square(*args, **kwargs)


def auto_crop_to_main_structure(*args, **kwargs):
    _sync_state()
    return _impl_auto_crop_to_main_structure(*args, **kwargs)


def execute_usgs_gradual_selection(*args, **kwargs):
    _sync_state()
    return _impl_execute_usgs_gradual_selection(*args, **kwargs)


def filter_tie_points(*args, **kwargs):
    _sync_state()
    return _impl_filter_tie_points(*args, **kwargs)


def filter_image_quality(*args, **kwargs):
    _sync_state()
    return _impl_filter_image_quality(*args, **kwargs)


def safe_create_cluster_batch(*args, **kwargs):
    _sync_state()
    return _impl_safe_create_cluster_batch(*args, **kwargs)


def build_cluster_crop_task(*args, **kwargs):
    _sync_state()
    return _impl_build_cluster_crop_task(*args, **kwargs)


def build_cluster_gradual_selection_tasks(*args, **kwargs):
    _sync_state()
    return _impl_build_cluster_gradual_selection_tasks(*args, **kwargs)


def build_monolithic_runscript_task(*args, **kwargs):
    _sync_state()
    return _impl_build_monolithic_runscript_task(*args, **kwargs)


def build_unified_cluster_pipeline_tasks(*args, **kwargs):
    _sync_state()
    return _impl_build_unified_cluster_pipeline_tasks(*args, **kwargs)


def save_checkpoint(*args, **kwargs):
    _sync_state()
    return _impl_save_checkpoint(*args, **kwargs)


def wait_for_user_resume(*args, **kwargs):
    _sync_state()
    return _impl_wait_for_user_resume(*args, **kwargs)


def run_reconstruction(*args, **kwargs):
    _sync_state()
    return _impl_run_reconstruction(*args, **kwargs)


if __name__ == "__main__":
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.profile:
        print("=========================================================================")
        print("  AUTOMETASHAPE: PRO SURVEY OPTICS & TELEMETRY PROFILER")
        print(f"  Target Directory: {args.images}")
        print("=========================================================================", flush=True)

        physics = analyze_survey_physics(args.images, sample_size=50, output_dir=args.output)
        plan = derive_pro_photogrammetry_plan(physics)
        print_pro_engineering_report(physics, plan)
        sys.exit(0)

    try:
        run_reconstruction(
            images_dir=args.images,
            output_dir=args.output,
            align_quality=args.align_quality,
            depth_quality=args.depth_quality,
            depth_filtering=args.depth_filtering,
            faces=args.faces,
            texture_pages=args.texture_pages,
            align_photos=args.align_photos,
            build_depth_maps=args.build_depth_maps,
            build_mesh=args.build_mesh,
            build_texture=args.build_texture,
            rebake_texture=args.rebake_texture,
            pro_mode=args.pro or args.preset == "pro",
            preset=args.preset,
            crop_method=args.crop_method,
            crop_padding=args.crop_padding,
            min_quality=args.min_quality,
            disable_quality_filter=args.disable_quality_filter,
            pause_after_crop=args.pause_after_crop,
            no_crop=args.no_crop,
            keypoint_limit=args.keypoint_limit,
            keypoint_limit_per_mpx=args.keypoint_limit_per_mpx,
            tiepoint_limit=args.tiepoint_limit,
            reference_preselection_mode=args.reference_preselection_mode,
            guided_matching=args.guided_matching,
            filter_stationary_points=args.filter_stationary_points,
            adaptive_fitting=args.adaptive_fitting,
            filter_tiepoints=args.filter_tiepoints,
            rolling_shutter=args.rolling_shutter,
            close_holes=args.close_holes,
            fill_holes=args.fill_holes,
            ghosting_filter=args.ghosting_filter,
            multi_chunk=args.multi_chunk,
            chunk_align_method=args.chunk_align_method,
            ground_cell_size=args.ground_cell_size,
            ground_max_angle=args.ground_max_angle,
            ground_max_distance=args.ground_max_distance,
            build_dem=args.build_dem,
            export_dem=args.export_dem,
            build_orthomosaic=getattr(args, "build_orthomosaic", None),
            export_ortho=getattr(args, "export_ortho", False),
            build_point_cloud=args.build_point_cloud,
            export_point_cloud=args.export_point_cloud,
            point_cloud_format=args.point_cloud_format,
            export_glb=args.export_glb,
            export_obj=args.export_obj,
            classify_ground=args.classify_ground,
            classify_points=args.classify_points,
            filter_point_confidence=args.filter_point_confidence,
            export_report=args.export_report,
            report_title=args.report_title,
            surface_type=args.surface_type,
            detect_markers=args.detect_markers,
            target_type=args.target_type,
            marker_tolerance=args.marker_tolerance,
            import_reference=args.import_reference,
            calibrate_colors=args.calibrate_colors,
            smooth_mesh=args.smooth_mesh,
            smooth_strength=args.smooth_strength,
            decimate_mesh=args.decimate_mesh,
            remove_lighting=args.remove_lighting,
            delighting_color_mode=args.delighting_color_mode,
            build_tiled_model=args.build_tiled_model,
            tiled_model_format=args.tiled_model_format,
            tile_size=args.tile_size,
            build_contours=args.build_contours,
            contour_interval=args.contour_interval,
            contours_format=args.contours_format,
            export_fbx=args.export_fbx,
            export_ply=args.export_ply,
            export_dxf=args.export_dxf,
            export_tie_points=args.export_tie_points,
            texture_format=getattr(args, "texture_format", "png"),
            network_enable=args.network_enable,
            network_host=args.network_host,
            network_port=args.network_port,
            network_path=args.network_path,
            network_independent_workers=args.network_independent_workers,
            local_scratch_staging=args.local_scratch_staging,
            scratch_dir=args.scratch_dir,
            min_scratch_free_gb=args.min_scratch_free_gb,
            project_name=args.project_name,
            status_file=getattr(args, "status_file", None),
            resume=getattr(args, "resume", True),
        )
    except Exception as exc:
        emit_ipc_error("pipeline", str(exc))
        raise
