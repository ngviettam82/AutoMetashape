# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""IPC milestone protocol and machine-readable telemetry sentinels."""

from autometashape.ipc.protocol import (
    EXPORT_TAG_MAP,
    EXPORT_TAG_REGEX,
    IPC_COMPLETE,
    IPC_PAUSE_CROP,
    IPC_SENTINEL_PREFIX,
    IPC_SENTINEL_SUFFIX,
    RECON_TAG_MAP,
    RECON_TAG_REGEX,
    emit_ipc_error,
    emit_ipc_event,
    emit_ipc_stage,
    parse_ipc_line,
    set_active_status_file,
)

__all__ = [
    "IPC_SENTINEL_PREFIX",
    "IPC_SENTINEL_SUFFIX",
    "IPC_PAUSE_CROP",
    "IPC_COMPLETE",
    "RECON_TAG_REGEX",
    "EXPORT_TAG_REGEX",
    "RECON_TAG_MAP",
    "EXPORT_TAG_MAP",
    "set_active_status_file",
    "emit_ipc_stage",
    "emit_ipc_event",
    "emit_ipc_error",
    "parse_ipc_line",
]
