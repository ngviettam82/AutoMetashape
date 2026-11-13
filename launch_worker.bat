@echo off
rem Copyright (c) 2026, AutoMetashape Contributors.
rem SPDX-License-Identifier: Apache-2.0

chcp 65001 > nul
setlocal enabledelayedexpansion
title AutoMetashape Cluster Worker Node

set "SCRIPT_DIR=%~dp0"
set "LAUNCHER_PY=%SCRIPT_DIR%scripts\launch_worker.py"

:: 1. Locate Python executable (prioritize Metashape Pro bundled Python)
set "PYTHON_EXE="

if exist "%ProgramFiles%\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_EXE=%ProgramFiles%\Agisoft\Metashape Pro\python\python.exe"
) else if exist "%ProgramW6432%\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_EXE=%ProgramW6432%\Agisoft\Metashape Pro\python\python.exe"
) else if exist "C:\Program Files\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_EXE=C:\Program Files\Agisoft\Metashape Pro\python\python.exe"
)

if "%PYTHON_EXE%"=="" (
    for %%D in (C D E F G) do (
        if exist "%%D:\Program Files\Agisoft\Metashape Pro\python\python.exe" (
            set "PYTHON_EXE=%%D:\Program Files\Agisoft\Metashape Pro\python\python.exe"
        )
    )
)

:: If not found in Metashape Pro, check system Python (verifying runtime, avoids WindowsApps stub)
if "%PYTHON_EXE%"=="" (
    python.exe -c "import sys" >nul 2>nul
    if !errorlevel! equ 0 (
        set "PYTHON_EXE=python.exe"
    )
)

if "%PYTHON_EXE%"=="" (
    py.exe -3 -c "import sys" >nul 2>nul
    if !errorlevel! equ 0 (
        set "PYTHON_EXE=py.exe -3"
    )
)

:: 2. Detect headless/non-interactive invocation
set "IS_HEADLESS="
for %%A in (%*) do (
    if /i "%%~A"=="--non-interactive" set "IS_HEADLESS=1"
    if /i "%%~A"=="--headless" set "IS_HEADLESS=1"
)

:: 3. Launch Resilient Worker Script via Python (Strictly no unmanaged binary fallback)
if not "%PYTHON_EXE%"=="" (
    "%PYTHON_EXE%" "%LAUNCHER_PY%" %*
    set "LAUNCH_EXIT=!errorlevel!"
    if !LAUNCH_EXIT! neq 0 (
        echo.
        echo [EXIT] Worker launcher terminated with exit code !LAUNCH_EXIT!.
        if not defined IS_HEADLESS pause
        exit /b !LAUNCH_EXIT!
    )
) else (
    echo [ERROR] Python runtime was not found.
    echo Please install Agisoft Metashape Pro or Python 3.10+ to run the resilient worker daemon.
    if not defined IS_HEADLESS pause
    exit /b 1
)
