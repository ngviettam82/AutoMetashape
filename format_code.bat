@echo off
REM Copyright (c) 2026, AutoMetashape Contributors.
REM All rights reserved.
REM SPDX-License-Identifier: Apache-2.0

setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Detect Agisoft Metashape Python or system Python
set "PYTHON_CMD="
if exist "C:\Program Files\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_CMD=C:\Program Files\Agisoft\Metashape Pro\python\python.exe"
) else if exist "C:\Program Files (x86)\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_CMD=C:\Program Files (x86)\Agisoft\Metashape Pro\python\python.exe"
) else (
    where python >nul 2>nul
    if !ERRORLEVEL! equ 0 set "PYTHON_CMD=python"
)

if "!PYTHON_CMD!"=="" (
    echo [ERROR] Python not found.
    exit /b 1
)

echo [FORMAT] Checking licenses and formatting source code...
"!PYTHON_CMD!" tools/check_licenses.py
if !ERRORLEVEL! neq 0 (
    echo [FORMAT] Inserting missing license headers...
    "!PYTHON_CMD!" tools/add_license_headers.py
)
where ruff >nul 2>nul
if !ERRORLEVEL! equ 0 (
    ruff format scripts/ tools/ skills/
    ruff check --fix scripts/ tools/ skills/
)
echo [FORMAT] Validating agent skills...
"!PYTHON_CMD!" tools/skills/cli.py check
