@echo off
REM Copyright (c) 2026, AutoMetashape Contributors.
REM All rights reserved.
REM SPDX-License-Identifier: Apache-2.0

setlocal DisableDelayedExpansion
pushd "%~dp0"
set "FMT_EXIT=0"

REM Detect Agisoft Metashape Python or system Python
set "PYTHON_CMD="
set "PYTHON_ARGS="
if not defined PYTHON_CMD if defined AUTOMETASHAPE_PYTHON_EXE if exist "%AUTOMETASHAPE_PYTHON_EXE%" set "PYTHON_CMD=%AUTOMETASHAPE_PYTHON_EXE%"
if not defined PYTHON_CMD if defined AUTOMETASHAPE_METASHAPE_PATH (
    for %%P in ("%AUTOMETASHAPE_METASHAPE_PATH%") do if exist "%%~dpPpython\python.exe" set "PYTHON_CMD=%%~dpPpython\python.exe"
)
if not defined PYTHON_CMD if defined METASHAPE_EXE (
    for %%P in ("%METASHAPE_EXE%") do if exist "%%~dpPpython\python.exe" set "PYTHON_CMD=%%~dpPpython\python.exe"
)
if not defined PYTHON_CMD if defined ProgramFiles if exist "%ProgramFiles%\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_CMD=%ProgramFiles%\Agisoft\Metashape Pro\python\python.exe"
if not defined PYTHON_CMD if defined ProgramW6432 if exist "%ProgramW6432%\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_CMD=%ProgramW6432%\Agisoft\Metashape Pro\python\python.exe"
if not defined PYTHON_CMD if defined ProgramFiles(x86) if exist "%ProgramFiles(x86)%\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_CMD=%ProgramFiles(x86)%\Agisoft\Metashape Pro\python\python.exe"
if not defined PYTHON_CMD if defined LOCALAPPDATA if exist "%LOCALAPPDATA%\Programs\Agisoft\Metashape Pro\python\python.exe" set "PYTHON_CMD=%LOCALAPPDATA%\Programs\Agisoft\Metashape Pro\python\python.exe"

if not defined PYTHON_CMD (
    for %%D in (C D E F G) do (
        if not defined PYTHON_CMD if exist "%%D:\Program Files\Agisoft\Metashape Pro\python\python.exe" (
            set "PYTHON_CMD=%%D:\Program Files\Agisoft\Metashape Pro\python\python.exe"
        )
    )
)

if not defined PYTHON_CMD (
    python.exe -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python.exe"
)

if not defined PYTHON_CMD (
    py.exe -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_CMD=py.exe"
        set "PYTHON_ARGS=-3"
    )
)

if not defined PYTHON_CMD (
    echo [ERROR] Python not found.
    popd
    exit /b 1
)

echo [FORMAT] Checking licenses and formatting source code...
"%PYTHON_CMD%" %PYTHON_ARGS% tools/check_licenses.py
if errorlevel 1 (
    echo [FORMAT] Inserting missing license headers...
    "%PYTHON_CMD%" %PYTHON_ARGS% tools/add_license_headers.py
    "%PYTHON_CMD%" %PYTHON_ARGS% tools/check_licenses.py
    if errorlevel 1 (
        set "FMT_EXIT=%ERRORLEVEL%"
        echo [ERROR] License header validation failed.
        goto :cleanup
    )
)

set "RUFF_EXE="
set "RUFF_ARGS="
where ruff >nul 2>nul
if not errorlevel 1 set "RUFF_EXE=ruff"

if not defined RUFF_EXE (
    python -m ruff --version >nul 2>nul
    if not errorlevel 1 (
        set "RUFF_EXE=python"
        set "RUFF_ARGS=-m ruff"
    )
)

if not defined RUFF_EXE (
    "%PYTHON_CMD%" %PYTHON_ARGS% -m ruff --version >nul 2>nul
    if not errorlevel 1 (
        set "RUFF_EXE=%PYTHON_CMD%"
        set "RUFF_ARGS=%PYTHON_ARGS% -m ruff"
    )
)

if defined RUFF_EXE (
    echo [FORMAT] Running ruff format...
    "%RUFF_EXE%" %RUFF_ARGS% format .
    if errorlevel 1 (
        set "FMT_EXIT=%ERRORLEVEL%"
        echo [ERROR] ruff format failed.
        goto :cleanup
    )
    echo [FORMAT] Running ruff check...
    "%RUFF_EXE%" %RUFF_ARGS% check .
    if errorlevel 1 (
        set "FMT_EXIT=%ERRORLEVEL%"
        echo [ERROR] ruff check failed.
        goto :cleanup
    )
) else (
    echo [WARN] ruff not found in PATH or Python environment; skipping ruff format/check.
    echo [WARN] Install ruff ^("pip install ruff"^) to mirror GitHub Actions Lint and Format checks locally.
)

echo [FORMAT] Validating agent skills...
"%PYTHON_CMD%" %PYTHON_ARGS% tools/skills/cli.py check
if errorlevel 1 (
    set "FMT_EXIT=%ERRORLEVEL%"
    echo [ERROR] Skill validation failed.
    goto :cleanup
)

:cleanup
popd
exit /b %FMT_EXIT%
