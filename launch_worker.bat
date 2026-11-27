@echo off
rem Copyright (c) 2026, AutoMetashape Contributors.
rem SPDX-License-Identifier: Apache-2.0

chcp 65001 >nul 2>&1
setlocal DisableDelayedExpansion
title AutoMetashape Cluster Worker Node

set "SCRIPT_DIR=%~dp0"
set "AUTOMETASHAPE_DIR=%~dp0"
if "%AUTOMETASHAPE_DIR:~-1%"=="\" set "AUTOMETASHAPE_DIR=%AUTOMETASHAPE_DIR:~0,-1%"
set "LAUNCHER_PY=%SCRIPT_DIR%scripts\launch_worker.py"

:: 1. Locate Python executable (prioritize Metashape Pro bundled Python)
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
        if not defined PYTHON_EXE if exist "%%D:\Program Files (x86)\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_EXE=%%D:\Program Files (x86)\Agisoft\Metashape Pro\python\python.exe"
        if not defined PYTHON_EXE if exist "%%D:\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_EXE=%%D:\Agisoft\Metashape Pro\python\python.exe"
    )
)

:: If not found in Metashape Pro, check system Python (verifying runtime, avoids WindowsApps stub)
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

:: 2. Detect headless/non-interactive invocation safely via shift (no wildcard expansion)
set "IS_HEADLESS="
call :detect_headless %*

:: 3. Launch Resilient Worker Script via Python (Strictly no unmanaged binary fallback)
if defined PYTHON_EXE (
    "%PYTHON_EXE%" %PYTHON_ARGS% "%LAUNCHER_PY%" %*
    set "LAUNCH_EXIT=%ERRORLEVEL%"
    if not "%LAUNCH_EXIT%"=="0" (
        echo.
        echo [EXIT] Worker launcher terminated with exit code %LAUNCH_EXIT%.
        if not defined IS_HEADLESS pause
        exit /b %LAUNCH_EXIT%
    )
    exit /b 0
) else (
    echo [ERROR] Python runtime was not found.
    echo Please install Agisoft Metashape Pro or Python 3.9+ to run the resilient worker daemon.
    if not defined IS_HEADLESS pause
    exit /b 1
)

:detect_headless
if [%1]==[] goto :eof
if /i "%~1"=="--non-interactive" set "IS_HEADLESS=1"
if /i "%~1"=="--headless" set "IS_HEADLESS=1"
shift
goto :detect_headless
