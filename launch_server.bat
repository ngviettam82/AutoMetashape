@echo off
rem Copyright (c) 2026, AutoMetashape Contributors.
rem SPDX-License-Identifier: Apache-2.0

chcp 65001 > nul
setlocal enabledelayedexpansion
title AutoMetashape Cluster Server Daemon

set "SCRIPT_DIR=%~dp0"
set "LAUNCHER_PY=%SCRIPT_DIR%scripts\launch_server.py"

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

:: 2. Launch Server Script via Python, or fallback directly to Metashape server binary
if not "%PYTHON_EXE%"=="" (
    "%PYTHON_EXE%" "%LAUNCHER_PY%" %*
    if !errorlevel! neq 0 (
        echo.
        echo [EXIT] Server launcher terminated with exit code !errorlevel!.
        pause
    )
) else (
    echo [NOTICE] Python executable not found. Falling back to direct Metashape server binary...
    set "SERVER_BIN="
    if exist "%ProgramFiles%\Agisoft\Metashape Pro\metashape-server.exe" (
        set "SERVER_BIN=%ProgramFiles%\Agisoft\Metashape Pro\metashape-server.exe"
    ) else if exist "%ProgramW6432%\Agisoft\Metashape Pro\metashape-server.exe" (
        set "SERVER_BIN=%ProgramW6432%\Agisoft\Metashape Pro\metashape-server.exe"
    )
    if "!SERVER_BIN!"=="" (
        for %%D in (C D E F G) do (
            if exist "%%D:\Program Files\Agisoft\Metashape Pro\metashape-server.exe" (
                set "SERVER_BIN=%%D:\Program Files\Agisoft\Metashape Pro\metashape-server.exe"
            )
        )
    )
    if "!SERVER_BIN!"=="" (
        if exist "%ProgramFiles%\Agisoft\Metashape Pro\metashape.exe" (
            set "SERVER_BIN=%ProgramFiles%\Agisoft\Metashape Pro\metashape.exe"
        ) else if exist "%ProgramW6432%\Agisoft\Metashape Pro\metashape.exe" (
            set "SERVER_BIN=%ProgramW6432%\Agisoft\Metashape Pro\metashape.exe"
        )
    )
    if "!SERVER_BIN!"=="" (
        for %%D in (C D E F G) do (
            if exist "%%D:\Program Files\Agisoft\Metashape Pro\metashape.exe" (
                set "SERVER_BIN=%%D:\Program Files\Agisoft\Metashape Pro\metashape.exe"
            )
        )
    )
    if not "!SERVER_BIN!"=="" (
        "!SERVER_BIN!" --host 0.0.0.0:5840 %*
    ) else (
        echo [ERROR] Metashape Server binary was not found. Please verify Metashape Pro is installed.
        pause
        exit /b 1
    )
)
