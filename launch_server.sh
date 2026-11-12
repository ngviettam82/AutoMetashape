#!/usr/bin/env bash
# Copyright (c) 2026, AutoMetashape Contributors.
# SPDX-License-Identifier: Apache-2.0

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER_PY="${SCRIPT_DIR}/scripts/launch_server.py"

PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi

if [ -z "${PYTHON_BIN}" ]; then
    echo "[ERROR] Python was not found in PATH."
    exit 1
fi

exec "${PYTHON_BIN}" "${LAUNCHER_PY}" "$@"
