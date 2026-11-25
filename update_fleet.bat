@echo off
rem Copyright (c) 2026, AutoMetashape Contributors.
rem SPDX-License-Identifier: Apache-2.0

chcp 65001 > nul
setlocal enabledelayedexpansion
title AutoMetashape Autonomous Worker Fleet Remote Updater

set "SCRIPT_DIR=%~dp0"
set "UPDATER_PY=%SCRIPT_DIR%scripts\update_fleet.py"

:: 1. Locate Metashape binary or bundled Python
set "PYTHON_EXE="
set "PYTHON_ARGS="

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

if "%PYTHON_EXE%"=="" (
    python.exe -c "import sys" >nul 2>nul
    if !errorlevel! equ 0 (
        set "PYTHON_EXE=python.exe"
    )
)

if "%PYTHON_EXE%"=="" (
    py.exe -3 -c "import sys" >nul 2>nul
    if !errorlevel! equ 0 (
        set "PYTHON_EXE=py.exe"
        set "PYTHON_ARGS=-3"
    )
)

:: 2. Launch Fleet Updater
if not "%PYTHON_EXE%"=="" (
    "%PYTHON_EXE%" %PYTHON_ARGS% "%UPDATER_PY%" %*
    set "EXIT_CODE=!errorlevel!"
    if !EXIT_CODE! neq 0 (
        echo.
        echo [EXIT] Fleet updater terminated with exit code !EXIT_CODE!.
        exit /b !EXIT_CODE!
    )
) else (
    echo [ERROR] Python runtime was not found.
    echo Please install Agisoft Metashape Pro or Python 3.10+ to run the fleet updater.
    exit /b 1
)
