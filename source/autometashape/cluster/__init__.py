# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Distributed processing cluster, hardware discovery, staging, and configuration."""

from autometashape.cluster.config import (
    DEFAULT_SERVER_CONFIG_FILENAME,
    DEFAULT_WORKER_CONFIG_FILENAME,
    get_default_config_path,
    load_cluster_config,
    save_cluster_config,
)
from autometashape.cluster.hardware import (
    detect_gpu_devices,
    extract_unc_root,
    find_metashape_monitor_path,
    find_metashape_path,
    find_metashape_server_path,
    format_file_size,
    generate_worker_bat_content,
    get_primary_lan_ip,
    is_integrated_gpu,
    normalize_unicode_path,
    test_tcp_connection,
    test_unc_path,
)
from autometashape.cluster.orchestrator import (
    ALIGN_DOWNSCALE_MAP,
    DEPTH_DOWNSCALE_MAP,
    DEPTH_FILTER_MAP,
    POINT_CLOUD_FORMAT_MAP,
    PRESEL_MODE_MAP,
    SHAPES_FORMAT_MAP,
    SURFACE_TYPE_MAP,
    TARGET_TYPE_MAP,
    TILED_MODEL_FORMAT_MAP,
    _has_dense_points,
    _norm_cluster_path,
    build_cluster_crop_task,
    build_cluster_gradual_selection_tasks,
    build_monolithic_runscript_task,
    build_unified_cluster_pipeline_tasks,
    evaluate_cluster_batch_info,
    safe_create_cluster_batch,
)
from autometashape.cluster.staging import (
    check_scratch_disk_headroom,
    get_optimal_local_scratch_dir,
    get_psx_files_dir,
    sync_staged_project_to_network,
)
from autometashape.cluster.supervisor import (
    ClusterSupervisor,
    QueueJob,
    check_storage_headroom,
    discover_dataset_projects,
)

__all__ = [
    "ALIGN_DOWNSCALE_MAP",
    "ClusterSupervisor",
    "DEFAULT_SERVER_CONFIG_FILENAME",
    "DEFAULT_WORKER_CONFIG_FILENAME",
    "DEPTH_DOWNSCALE_MAP",
    "DEPTH_FILTER_MAP",
    "POINT_CLOUD_FORMAT_MAP",
    "PRESEL_MODE_MAP",
    "QueueJob",
    "SHAPES_FORMAT_MAP",
    "SURFACE_TYPE_MAP",
    "TARGET_TYPE_MAP",
    "TILED_MODEL_FORMAT_MAP",
    "_has_dense_points",
    "_norm_cluster_path",
    "build_cluster_crop_task",
    "build_cluster_gradual_selection_tasks",
    "build_monolithic_runscript_task",
    "build_unified_cluster_pipeline_tasks",
    "check_scratch_disk_headroom",
    "check_storage_headroom",
    "detect_gpu_devices",
    "discover_dataset_projects",
    "evaluate_cluster_batch_info",
    "extract_unc_root",
    "find_metashape_monitor_path",
    "find_metashape_path",
    "find_metashape_server_path",
    "format_file_size",
    "generate_worker_bat_content",
    "get_default_config_path",
    "get_optimal_local_scratch_dir",
    "get_primary_lan_ip",
    "get_psx_files_dir",
    "is_integrated_gpu",
    "load_cluster_config",
    "normalize_unicode_path",
    "safe_create_cluster_batch",
    "save_cluster_config",
    "sync_staged_project_to_network",
    "test_tcp_connection",
    "test_unc_path",
]
