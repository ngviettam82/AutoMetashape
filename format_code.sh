#!/usr/bin/env bash
# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
cd "$(dirname "$0")"

# Detect Python
PYTHON_CMD=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD="python"
fi

if [ -z "$PYTHON_CMD" ]; then
    echo "[ERROR] Python not found."
    exit 1
fi

echo "[FORMAT] Checking licenses and formatting source code..."
if ! "$PYTHON_CMD" tools/check_licenses.py; then
    echo "[FORMAT] Inserting missing license headers..."
    "$PYTHON_CMD" tools/add_license_headers.py
fi

RUFF_CMD=""
if command -v ruff >/dev/null 2>&1; then
    RUFF_CMD="ruff"
elif "$PYTHON_CMD" -m ruff --version >/dev/null 2>&1; then
    RUFF_CMD="$PYTHON_CMD -m ruff"
fi

if [ -n "$RUFF_CMD" ]; then
    $RUFF_CMD format .
    $RUFF_CMD check .
fi

echo "[FORMAT] Validating agent skills..."
"$PYTHON_CMD" tools/skills/cli.py check
