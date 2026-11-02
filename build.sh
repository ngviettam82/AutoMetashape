#!/usr/bin/env bash
# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -e
echo "[BUILD] Installing GaussianSplat subsystem in editable mode..."
python -m pip install -e source/extensions/gaussian.splat
echo "[BUILD] Validating agent skills..."
python tools/skills/cli.py check
echo "[BUILD] Build completed successfully."
