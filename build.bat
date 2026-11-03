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

echo [BUILD] Validating agent skills...
"!PYTHON_CMD!" tools/skills/cli.py check
if !ERRORLEVEL! neq 0 (
    echo [ERROR] Skill validation failed.
    exit /b !ERRORLEVEL!
)
echo [BUILD] AutoMetashape build completed successfully.
