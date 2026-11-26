@echo off
REM Copyright (c) 2026, AutoMetashape Contributors.
REM All rights reserved.
REM SPDX-License-Identifier: Apache-2.0

setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM Detect Agisoft Metashape Python or system Python
set "PYTHON_CMD="
set "PYTHON_ARGS="
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
        set "PYTHON_CMD=py.exe"
        set "PYTHON_ARGS=-3"
    )
)

if "!PYTHON_CMD!"=="" (
    echo [ERROR] Python not found.
    exit /b 1
)

echo [FORMAT] Checking licenses and formatting source code...
"!PYTHON_CMD!" %PYTHON_ARGS% tools/check_licenses.py
if !ERRORLEVEL! neq 0 (
    echo [FORMAT] Inserting missing license headers...
    "!PYTHON_CMD!" %PYTHON_ARGS% tools/add_license_headers.py
    "!PYTHON_CMD!" %PYTHON_ARGS% tools/check_licenses.py
    if !ERRORLEVEL! neq 0 (
        echo [ERROR] License header validation failed.
        exit /b !ERRORLEVEL!
    )
)

set "RUFF_EXE="
set "RUFF_ARGS="
where ruff >nul 2>nul
if !ERRORLEVEL! equ 0 (
    set "RUFF_EXE=ruff"
) else (
    python -m ruff --version >nul 2>nul
    if !ERRORLEVEL! equ 0 (
        set "RUFF_EXE=python"
        set "RUFF_ARGS=-m ruff"
    ) else (
        "!PYTHON_CMD!" %PYTHON_ARGS% -m ruff --version >nul 2>nul
        if !ERRORLEVEL! equ 0 (
            set "RUFF_EXE=!PYTHON_CMD!"
            set "RUFF_ARGS=%PYTHON_ARGS% -m ruff"
        )
    )
)
if defined RUFF_EXE (
    echo [FORMAT] Running ruff format...
    "!RUFF_EXE!" %RUFF_ARGS% format .
    if !ERRORLEVEL! neq 0 (
        echo [ERROR] ruff format failed.
        exit /b !ERRORLEVEL!
    )
    echo [FORMAT] Running ruff check...
    "!RUFF_EXE!" %RUFF_ARGS% check .
    if !ERRORLEVEL! neq 0 (
        echo [ERROR] ruff check failed.
        exit /b !ERRORLEVEL!
    )
) else (
    echo [WARN] ruff not found in PATH or Python environment; skipping ruff format/check.
    echo [WARN] Install ruff ^("pip install ruff"^) to mirror GitHub Actions Lint and Format checks locally.
)
echo [FORMAT] Validating agent skills...
"!PYTHON_CMD!" %PYTHON_ARGS% tools/skills/cli.py check
if !ERRORLEVEL! neq 0 (
    echo [ERROR] Skill validation failed.
    exit /b !ERRORLEVEL!
)
