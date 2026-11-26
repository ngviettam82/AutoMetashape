# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Cluster Utilities Backward-Compatibility Facade.

This module re-exports the canonical cluster and IPC APIs from
``autometashape.cluster`` and ``autometashape.ipc`` to maintain 100% backward
compatibility with existing scripts, batch launchers, and unit tests.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import string
import subprocess
import sys
import tempfile

# Ensure canonical source/ directory is available on sys.path
_SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "source"))
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

from autometashape.cluster import (
    ABORT_DIR_NAME,
    ABORT_SIGNAL_FILENAME,
    ABORT_SIGNALS_JSON,
    abort_remote_worker,
    acknowledge_abort_signal,
    check_abort_signal,
    cleanup_scratch_locks,
    cleanup_stale_worker_processes,
    dispatch_abort_signal,
    kill_worker_process,
    DEFAULT_SERVER_CONFIG_FILENAME,
    DEFAULT_WORKER_CONFIG_FILENAME,
    check_scratch_disk_headroom,
    compute_discrete_gpu_mask,
    discover_dual_share_projects,
    find_metashape_monitor_path,
    find_metashape_path,
    find_metashape_python_path,
    find_metashape_server_path,
    UPDATE_SIGNAL_FILENAME,
    check_update_pending,
    mirror_directory_with_prune,
    perform_worker_codebase_update,
    format_file_size,
    generate_worker_bat_content,
    get_default_config_path,
    get_local_hostname,
    get_optimal_local_scratch_dir,
    get_primary_lan_ip,
    get_psx_files_dir,
    is_healthy_online_worker,
    is_integrated_gpu,
    is_subpath,
    is_unc_path,
    load_cluster_config,
    map_source_to_result_dir,
    normalize_share_path,
    normalize_unicode_path,
    probe_metashape_gpu_mask,
    resolve_result_network_root,
    run_dual_share_cluster_pipeline,
    save_cluster_config,
    evaluate_cluster_batch_info,
    extract_unc_root,
    sync_staged_project_to_network,
    test_tcp_connection,
    test_unc_path,
    validate_dual_share_topology,
)
from autometashape.cluster.hardware import detect_gpu_devices as _detect_gpu_devices
from autometashape.ipc import (
    EXPORT_TAG_MAP,
    EXPORT_TAG_REGEX,
    IPC_COMPLETE,
    IPC_SENTINEL_PREFIX,
    IPC_SENTINEL_SUFFIX,
    RECON_TAG_MAP,
    RECON_TAG_REGEX,
    parse_ipc_line,
)


def detect_gpu_devices():
    """Facade for detect_gpu_devices respecting module-level patches."""
    import autometashape.cluster.hardware as hw

    orig_shutil_which = hw.shutil.which
    orig_subproc_run = hw.subprocess.run
    try:
        hw.shutil.which = shutil.which
        hw.subprocess.run = subprocess.run
        return _detect_gpu_devices()
    finally:
        hw.shutil.which = orig_shutil_which
        hw.subprocess.run = orig_subproc_run


__all__ = [
    "DEFAULT_SERVER_CONFIG_FILENAME",
    "ABORT_DIR_NAME",
    "ABORT_SIGNAL_FILENAME",
    "ABORT_SIGNALS_JSON",
    "abort_remote_worker",
    "acknowledge_abort_signal",
    "check_abort_signal",
    "cleanup_scratch_locks",
    "cleanup_stale_worker_processes",
    "dispatch_abort_signal",
    "kill_worker_process",
    "DEFAULT_WORKER_CONFIG_FILENAME",
    "EXPORT_TAG_MAP",
    "EXPORT_TAG_REGEX",
    "IPC_COMPLETE",
    "IPC_SENTINEL_PREFIX",
    "IPC_SENTINEL_SUFFIX",
    "RECON_TAG_MAP",
    "RECON_TAG_REGEX",
    "check_scratch_disk_headroom",
    "compute_discrete_gpu_mask",
    "detect_gpu_devices",
    "discover_dual_share_projects",
    "evaluate_cluster_batch_info",
    "extract_unc_root",
    "find_metashape_monitor_path",
    "find_metashape_path",
    "find_metashape_python_path",
    "find_metashape_server_path",
    "UPDATE_SIGNAL_FILENAME",
    "check_update_pending",
    "mirror_directory_with_prune",
    "perform_worker_codebase_update",
    "format_file_size",
    "generate_worker_bat_content",
    "get_default_config_path",
    "get_local_hostname",
    "get_optimal_local_scratch_dir",
    "get_primary_lan_ip",
    "get_psx_files_dir",
    "is_healthy_online_worker",
    "is_integrated_gpu",
    "is_subpath",
    "is_unc_path",
    "json",
    "load_cluster_config",
    "map_source_to_result_dir",
    "normalize_share_path",
    "normalize_unicode_path",
    "os",
    "parse_ipc_line",
    "probe_metashape_gpu_mask",
    "resolve_result_network_root",
    "run_dual_share_cluster_pipeline",
    "save_cluster_config",
    "shutil",
    "socket",
    "string",
    "subprocess",
    "sync_staged_project_to_network",
    "sys",
    "tempfile",
    "test_tcp_connection",
    "test_unc_path",
    "validate_dual_share_topology",
]
