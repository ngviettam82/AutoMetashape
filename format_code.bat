@echo off
REM Copyright (c) 2026, AutoMetashape Contributors.
REM All rights reserved.
REM SPDX-License-Identifier: Apache-2.0

setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Detect Agisoft Metashape Python or system Python
set "PYTHON_CMD="
if defined ProgramFiles if exist "%ProgramFiles%\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_CMD=%ProgramFiles%\Agisoft\Metashape Pro\python\python.exe"
) else if defined ProgramW6432 if exist "%ProgramW6432%\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_CMD=%ProgramW6432%\Agisoft\Metashape Pro\python\python.exe"
) else if defined ProgramFiles(x86) if exist "%ProgramFiles(x86)%\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_CMD=%ProgramFiles(x86)%\Agisoft\Metashape Pro\python\python.exe"
) else if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\Programs\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Agisoft\Metashape Pro\python\python.exe"
)

if "%PYTHON_CMD%"=="" (
    for %%D in (C D E F G) do (
        if exist "%%D:\Program Files\Agisoft\Metashape Pro\python\python.exe" (
            set "PYTHON_CMD=%%D:\Program Files\Agisoft\Metashape Pro\python\python.exe"
        )
    )
)

if "%PYTHON_CMD%"=="" (
    python.exe -c "import sys" >nul 2>nul
    if !ERRORLEVEL! equ 0 (
        set "PYTHON_CMD=python.exe"
    )
)

if "%PYTHON_CMD%"=="" (
    py.exe -3 -c "import sys" >nul 2>nul
    if !ERRORLEVEL! equ 0 (
        set "PYTHON_CMD=py.exe -3"
    )
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
    set "TARGETS="
    if exist "source\" set "TARGETS=!TARGETS! source/"
    if exist "scripts\" set "TARGETS=!TARGETS! scripts/"
    if exist "tools\" set "TARGETS=!TARGETS! tools/"
    if exist "skills\" set "TARGETS=!TARGETS! skills/"
    if defined TARGETS (
        ruff format !TARGETS!
        ruff check --fix !TARGETS!
    )
)
echo [FORMAT] Validating agent skills...
"!PYTHON_CMD!" tools/skills/cli.py check
