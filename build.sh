#!/usr/bin/env bash
# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -e
echo "[BUILD] Installing AutoMetashape package in editable mode..."
python -m pip install -e .
echo "[BUILD] Validating agent skills..."
python tools/skills/cli.py check
echo "[BUILD] AutoMetashape build completed successfully."
