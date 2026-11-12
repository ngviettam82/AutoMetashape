@echo off
rem Copyright (c) 2026, AutoMetashape Contributors.
rem SPDX-License-Identifier: Apache-2.0

chcp 65001 > nul
setlocal enabledelayedexpansion
title AutoMetashape Cluster Worker Node

set "SCRIPT_DIR=%~dp0"
set "LAUNCHER_PY=%SCRIPT_DIR%scripts\launch_worker.py"

:: 1. Locate Python executable
set "PYTHON_EXE="
where python.exe >nul 2>nul
if !errorlevel! equ 0 (
    set "PYTHON_EXE=python.exe"
)

if "%PYTHON_EXE%"=="" (
    if exist "C:\Program Files\Agisoft\Metashape Pro\python\python.exe" (
        set "PYTHON_EXE=C:\Program Files\Agisoft\Metashape Pro\python\python.exe"
    )
)

if "%PYTHON_EXE%"=="" (
    for %%D in (C D E F) do (
        if exist "%%D:\Program Files\Agisoft\Metashape Pro\python\python.exe" (
            set "PYTHON_EXE=%%D:\Program Files\Agisoft\Metashape Pro\python\python.exe"
        )
    )
)

if "%PYTHON_EXE%"=="" (
    echo [ERROR] Python 3 was not found in PATH or Metashape Pro installation directory.
    echo Please install Python 3 or add it to PATH.
    pause
    exit /b 1
)

:: 2. Launch Resilient Worker Script
"%PYTHON_EXE%" "%LAUNCHER_PY%" %*
if !errorlevel! neq 0 (
    echo.
    echo [EXIT] Worker launcher terminated with exit code !errorlevel!.
    pause
)
