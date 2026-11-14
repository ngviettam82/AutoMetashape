# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Core photogrammetry pipeline subsystems: optics physics, filtering, export, and reconstruction."""

from autometashape.core.export import (
    export_project_assets,
    infer_project_name,
)
from autometashape.core.filtering import (
    auto_crop_camera_gps_square,
    auto_crop_to_main_structure,
    execute_usgs_gradual_selection,
    filter_image_quality,
    filter_tie_points,
    robust_bounds,
    solve_ray_convergence_irls,
)
from autometashape.core.pipeline import (
    _has_dense_points,
    build_arg_parser,
    run_reconstruction,
    save_checkpoint,
    wait_for_user_resume,
)
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

__all__ = [
    "CAMERA_HARDWARE_DB",
    "SUPPORTED_IMAGE_EXTS",
    "_has_dense_points",
    "analyze_radiometry",
    "analyze_survey_physics",
    "auto_crop_camera_gps_square",
    "auto_crop_to_main_structure",
    "build_arg_parser",
    "calculate_haversine_meters",
    "compute_utm_epsg",
    "derive_pro_photogrammetry_plan",
    "discover_image_subfolder_groups",
    "execute_usgs_gradual_selection",
    "export_project_assets",
    "extract_image_telemetry",
    "filter_image_quality",
    "filter_tie_points",
    "infer_project_name",
    "parse_xmp_relative_altitude",
    "parse_xmp_telemetry",
    "print_pro_engineering_report",
    "robust_bounds",
    "run_reconstruction",
    "save_checkpoint",
    "solve_ray_convergence_irls",
    "wait_for_user_resume",
]
