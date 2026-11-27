@echo off
rem Copyright (c) 2026, AutoMetashape Contributors.
rem SPDX-License-Identifier: Apache-2.0

chcp 65001 >nul 2>&1
setlocal DisableDelayedExpansion
title AutoMetashape Autonomous Worker Fleet Remote Updater

set "SCRIPT_DIR=%~dp0"
set "UPDATER_PY=%SCRIPT_DIR%scripts\update_fleet.py"

:: 1. Locate Metashape bundled Python or system Python
set "PYTHON_EXE="
set "PYTHON_ARGS="

if not defined PYTHON_EXE if defined AUTOMETASHAPE_PYTHON_EXE if exist "%AUTOMETASHAPE_PYTHON_EXE%" set "PYTHON_EXE=%AUTOMETASHAPE_PYTHON_EXE%"
if not defined PYTHON_EXE if defined AUTOMETASHAPE_METASHAPE_PATH (
    for %%P in ("%AUTOMETASHAPE_METASHAPE_PATH%") do if exist "%%~dpPpython\python.exe" set "PYTHON_EXE=%%~dpPpython\python.exe"
)
if not defined PYTHON_EXE if defined METASHAPE_EXE (
    for %%P in ("%METASHAPE_EXE%") do if exist "%%~dpPpython\python.exe" set "PYTHON_EXE=%%~dpPpython\python.exe"
)
if not defined PYTHON_EXE if defined ProgramFiles if exist "%ProgramFiles%\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_EXE=%ProgramFiles%\Agisoft\Metashape Pro\python\python.exe"
if not defined PYTHON_EXE if defined ProgramW6432 if exist "%ProgramW6432%\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_EXE=%ProgramW6432%\Agisoft\Metashape Pro\python\python.exe"
if not defined PYTHON_EXE if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\Programs\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Agisoft\Metashape Pro\python\python.exe"
if not defined PYTHON_EXE if exist "C:\Program Files\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_EXE=C:\Program Files\Agisoft\Metashape Pro\python\python.exe"

if not defined PYTHON_EXE (
    for %%D in (C D E F G) do (
        if not defined PYTHON_EXE if exist "%%D:\Program Files\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_EXE=%%D:\Program Files\Agisoft\Metashape Pro\python\python.exe"
    )
)

if not defined PYTHON_EXE (
    python.exe -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYTHON_EXE=python.exe"
)

if not defined PYTHON_EXE (
    py.exe -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_EXE=py.exe"
        set "PYTHON_ARGS=-3"
    )
)

:: 2. Launch Fleet Updater
if defined PYTHON_EXE (
    "%PYTHON_EXE%" %PYTHON_ARGS% "%UPDATER_PY%" %*
    set "EXIT_CODE=%ERRORLEVEL%"
    if not "%EXIT_CODE%"=="0" (
        echo.
        echo [EXIT] Fleet updater terminated with exit code %EXIT_CODE%.
        exit /b %EXIT_CODE%
    )
    exit /b 0
) else (
    echo [ERROR] Python runtime was not found.
    echo Please install Agisoft Metashape Pro or Python 3.9+ to run the fleet updater.
    exit /b 1
)
