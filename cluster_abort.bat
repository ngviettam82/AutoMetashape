@echo off
rem Copyright (c) 2026, AutoMetashape Contributors.
rem SPDX-License-Identifier: Apache-2.0

chcp 65001 > nul
setlocal enabledelayedexpansion
title AutoMetashape Cluster Remote Worker Abort Utility

set "SCRIPT_DIR=%~dp0"
set "ABORT_PY=%SCRIPT_DIR%scripts\cluster_abort.py"

:: 1. Locate Metashape binary or bundled Python
set "PYTHON_EXE="

if exist "%ProgramFiles%\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_EXE=%ProgramFiles%\Agisoft\Metashape Pro\python\python.exe"
) else if exist "%ProgramW6432%\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_EXE=%ProgramW6432%\Agisoft\Metashape Pro\python\python.exe"
) else if exist "C:\Program Files\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_EXE=C:\Program Files\Agisoft\Metashape Pro\python\python.exe"
)

if "%PYTHON_EXE%"=="" (
    for %%D in (C D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
        if exist "%%D:\Program Files\Agisoft\Metashape Pro\python\python.exe" (
            set "PYTHON_EXE=%%D:\Program Files\Agisoft\Metashape Pro\python\python.exe"
        )
    )
)

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

:: 2. Launch Cluster Abort Utility
if not "%PYTHON_EXE%"=="" (
    "%PYTHON_EXE%" "%ABORT_PY%" %*
    set "EXIT_CODE=!errorlevel!"
    if !EXIT_CODE! neq 0 (
        echo.
        echo [EXIT] Cluster abort utility terminated with exit code !EXIT_CODE!.
        exit /b !EXIT_CODE!
    )
) else (
    echo [ERROR] Python runtime was not found.
    echo Please install Agisoft Metashape Pro or Python 3.10+ to run the cluster abort utility.
    exit /b 1
)
