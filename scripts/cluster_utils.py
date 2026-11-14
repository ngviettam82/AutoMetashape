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
    DEFAULT_SERVER_CONFIG_FILENAME,
    DEFAULT_WORKER_CONFIG_FILENAME,
    check_scratch_disk_headroom,
    find_metashape_monitor_path,
    find_metashape_path,
    find_metashape_server_path,
    format_file_size,
    generate_worker_bat_content,
    get_default_config_path,
    get_optimal_local_scratch_dir,
    get_primary_lan_ip,
    get_psx_files_dir,
    is_integrated_gpu,
    load_cluster_config,
    normalize_unicode_path,
    save_cluster_config,
    sync_staged_project_to_network,
    test_tcp_connection,
    test_unc_path,
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
    "DEFAULT_WORKER_CONFIG_FILENAME",
    "EXPORT_TAG_MAP",
    "EXPORT_TAG_REGEX",
    "IPC_COMPLETE",
    "IPC_SENTINEL_PREFIX",
    "IPC_SENTINEL_SUFFIX",
    "RECON_TAG_MAP",
    "RECON_TAG_REGEX",
    "check_scratch_disk_headroom",
    "detect_gpu_devices",
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
    "json",
    "normalize_unicode_path",
    "os",
    "parse_ipc_line",
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
]
