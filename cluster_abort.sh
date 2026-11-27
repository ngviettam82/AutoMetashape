#!/usr/bin/env bash
# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ABORT_PY="${SCRIPT_DIR}/scripts/cluster_abort.py"

METASHAPE_BIN=""
for env_cand in "${AUTOMETASHAPE_METASHAPE_PATH:-}" "${METASHAPE_EXE:-}"; do
    if [ -n "${env_cand}" ] && [ -x "${env_cand}" ]; then
        METASHAPE_BIN="${env_cand}"
        break
    fi
done

if [ -z "${METASHAPE_BIN}" ]; then
    for cand in \
        "/opt/metashape-pro/metashape.sh" \
        "/opt/metashape-pro/metashape" \
        "/Applications/MetashapePro.app/Contents/MacOS/MetashapePro" \
        "/usr/local/bin/metashape" \
        "/usr/bin/metashape"; do
        if [ -x "$cand" ]; then
            METASHAPE_BIN="$cand"
            break
        fi
    done
fi

if [ -n "${METASHAPE_BIN}" ]; then
    if [ "$(uname -s)" = "Linux" ]; then
        exec "${METASHAPE_BIN}" -platform offscreen -r "${ABORT_PY}" --no-reexec "$@"
    fi
    exec "${METASHAPE_BIN}" -r "${ABORT_PY}" --no-reexec "$@"
fi

PYTHON_EXE=""
if [ -n "${AUTOMETASHAPE_PYTHON_EXE:-}" ] && [ -x "${AUTOMETASHAPE_PYTHON_EXE}" ]; then
    PYTHON_EXE="${AUTOMETASHAPE_PYTHON_EXE}"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_EXE="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_EXE="python"
fi

if [ -n "${PYTHON_EXE}" ]; then
    exec "${PYTHON_EXE}" "${ABORT_PY}" "$@"
fi

echo "[ERROR] Metashape Pro or Python runtime was not found." >&2
exit 1
