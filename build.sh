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

echo "[BUILD] Validating agent skills..."
"$PYTHON_CMD" tools/skills/cli.py check

echo "[BUILD] Running unit test suite..."
"$PYTHON_CMD" -m unittest discover -s tests

echo "[BUILD] AutoMetashape build completed successfully."
