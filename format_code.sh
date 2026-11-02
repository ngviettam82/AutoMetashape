#!/usr/bin/env bash
# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -e
echo "[FORMAT] Checking licenses and formatting source code..."
python tools/check_licenses.py || python tools/add_license_headers.py
python -m ruff format source/ tools/ skills/
python -m ruff check --fix source/ tools/ skills/
echo "[FORMAT] Validating agent skills..."
python tools/skills/cli.py check
