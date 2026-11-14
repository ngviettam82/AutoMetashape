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

if command -v ruff >/dev/null 2>&1; then
    TARGET_DIRS=()
    [ -d "source" ] && TARGET_DIRS+=("source/")
    [ -d "scripts" ] && TARGET_DIRS+=("scripts/")
    [ -d "tools" ] && TARGET_DIRS+=("tools/")
    [ -d "skills" ] && TARGET_DIRS+=("skills/")
    if [ ${#TARGET_DIRS[@]} -gt 0 ]; then
        ruff format "${TARGET_DIRS[@]}"
        ruff check --fix "${TARGET_DIRS[@]}"
    fi
fi

echo "[FORMAT] Validating agent skills..."
"$PYTHON_CMD" tools/skills/cli.py check
