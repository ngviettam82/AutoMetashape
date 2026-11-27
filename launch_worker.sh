#!/usr/bin/env bash
# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export AUTOMETASHAPE_DIR="${SCRIPT_DIR}"

PYTHON_EXE=""
if [ -n "${AUTOMETASHAPE_PYTHON_EXE:-}" ] && [ -x "${AUTOMETASHAPE_PYTHON_EXE}" ]; then
    PYTHON_EXE="${AUTOMETASHAPE_PYTHON_EXE}"
fi

if [ -z "${PYTHON_EXE}" ]; then
    for cand in \
        "/opt/metashape-pro/python/bin/python3" \
        "/opt/metashape-pro/python/bin/python" \
        "/Applications/MetashapePro.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3" \
        "$HOME/metashape-pro/python/bin/python3"; do
        if [ -x "$cand" ]; then
            PYTHON_EXE="$cand"
            break
        fi
    done
fi

if [ -z "${PYTHON_EXE}" ]; then
    if command -v python3 >/dev/null 2>&1; then
        PYTHON_EXE="python3"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_EXE="python"
    fi
fi

if [ -z "${PYTHON_EXE}" ]; then
    echo "[ERROR] Python runtime was not found. Install Metashape Pro or Python 3.9+." >&2
    exit 1
fi

exec "${PYTHON_EXE}" "${SCRIPT_DIR}/scripts/launch_worker.py" "$@"
