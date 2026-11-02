@echo off
REM Copyright (c) 2026, GaussianSplat Contributors.
REM All rights reserved.
REM SPDX-License-Identifier: Apache-2.0

echo [FORMAT] Checking licenses and formatting source code...
python tools/check_licenses.py
if %ERRORLEVEL% neq 0 (
    echo [FORMAT] Inserting missing license headers...
    python tools/add_license_headers.py
)
python -m ruff format source/ tools/ skills/
python -m ruff check --fix source/ tools/ skills/
echo [FORMAT] Validating agent skills...
python tools/skills/cli.py check
