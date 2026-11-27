@echo off
rem Copyright (c) 2026, AutoMetashape Contributors.
rem SPDX-License-Identifier: Apache-2.0

chcp 65001 >nul 2>&1
setlocal DisableDelayedExpansion
title AutoMetashape Cluster Remote Worker Abort Utility

set "SCRIPT_DIR=%~dp0"
set "ABORT_PY=%SCRIPT_DIR%scripts\cluster_abort.py"

:: 1. Locate Metashape binary (embedded Metashape Python module) or standalone Python
set "METASHAPE_EXE="

if not defined METASHAPE_EXE if defined AUTOMETASHAPE_METASHAPE_PATH if exist "%AUTOMETASHAPE_METASHAPE_PATH%" set "METASHAPE_EXE=%AUTOMETASHAPE_METASHAPE_PATH%"
if not defined METASHAPE_EXE if defined METASHAPE_EXE if exist "%METASHAPE_EXE%" set "METASHAPE_EXE=%METASHAPE_EXE%"
if not defined METASHAPE_EXE if defined ProgramFiles if exist "%ProgramFiles%\Agisoft\Metashape Pro\metashape.exe" set "METASHAPE_EXE=%ProgramFiles%\Agisoft\Metashape Pro\metashape.exe"
if not defined METASHAPE_EXE if defined ProgramW6432 if exist "%ProgramW6432%\Agisoft\Metashape Pro\metashape.exe" set "METASHAPE_EXE=%ProgramW6432%\Agisoft\Metashape Pro\metashape.exe"
if not defined METASHAPE_EXE if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\Programs\Agisoft\Metashape Pro\metashape.exe" set "METASHAPE_EXE=%LOCALAPPDATA%\Programs\Agisoft\Metashape Pro\metashape.exe"
if not defined METASHAPE_EXE if exist "C:\Program Files\Agisoft\Metashape Pro\metashape.exe" set "METASHAPE_EXE=C:\Program Files\Agisoft\Metashape Pro\metashape.exe"

if not defined METASHAPE_EXE (
    for %%D in (C D E F G) do (
        if not defined METASHAPE_EXE if exist "%%D:\Program Files\Agisoft\Metashape Pro\metashape.exe" set "METASHAPE_EXE=%%D:\Program Files\Agisoft\Metashape Pro\metashape.exe"
    )
)

if defined METASHAPE_EXE (
    "%METASHAPE_EXE%" -r "%ABORT_PY%" %*
    set "EXIT_CODE=%ERRORLEVEL%"
    if not "%EXIT_CODE%"=="0" (
        echo.
        echo [EXIT] Cluster abort utility terminated with exit code %EXIT_CODE%.
        exit /b %EXIT_CODE%
    )
    exit /b 0
)

:: 2. Fallback to bundled Python or system Python if metashape.exe is not found
set "PYTHON_EXE="
set "PYTHON_ARGS="

if not defined PYTHON_EXE if defined AUTOMETASHAPE_PYTHON_EXE if exist "%AUTOMETASHAPE_PYTHON_EXE%" set "PYTHON_EXE=%AUTOMETASHAPE_PYTHON_EXE%"

if not defined PYTHON_EXE if defined ProgramFiles if exist "%ProgramFiles%\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_EXE=%ProgramFiles%\Agisoft\Metashape Pro\python\python.exe"
if not defined PYTHON_EXE if defined ProgramW6432 if exist "%ProgramW6432%\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_EXE=%ProgramW6432%\Agisoft\Metashape Pro\python\python.exe"
if not defined PYTHON_EXE if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\Programs\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Agisoft\Metashape Pro\python\python.exe"
if not defined PYTHON_EXE if exist "C:\Program Files\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_EXE=C:\Program Files\Agisoft\Metashape Pro\python\python.exe"

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

if defined PYTHON_EXE (
    "%PYTHON_EXE%" %PYTHON_ARGS% "%ABORT_PY%" %*
    set "EXIT_CODE=%ERRORLEVEL%"
    if not "%EXIT_CODE%"=="0" (
        echo.
        echo [EXIT] Cluster abort utility terminated with exit code %EXIT_CODE%.
        exit /b %EXIT_CODE%
    )
    exit /b 0
) else (
    echo [ERROR] Agisoft Metashape Pro or Python runtime was not found.
    echo Please install Agisoft Metashape Pro or Python 3.9+ to run the cluster abort utility.
    exit /b 1
)
