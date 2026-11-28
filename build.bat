@echo off
REM Copyright (c) 2026, AutoMetashape Contributors.
REM All rights reserved.
REM SPDX-License-Identifier: Apache-2.0

setlocal DisableDelayedExpansion
pushd "%~dp0"
set "BUILD_EXIT=0"

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

echo [BUILD] Auditing SPDX license compliance...
"%PYTHON_CMD%" %PYTHON_ARGS% tools/check_licenses.py
set "BUILD_EXIT=%ERRORLEVEL%"
if not "%BUILD_EXIT%"=="0" goto :license_fail

echo [BUILD] Validating agent skills...
"%PYTHON_CMD%" %PYTHON_ARGS% tools/skills/cli.py check
set "BUILD_EXIT=%ERRORLEVEL%"
if not "%BUILD_EXIT%"=="0" goto :skill_fail

echo [BUILD] Running unit test suite...
"%PYTHON_CMD%" %PYTHON_ARGS% -m unittest discover -s tests
set "BUILD_EXIT=%ERRORLEVEL%"
if not "%BUILD_EXIT%"=="0" goto :test_fail

echo [BUILD] AutoMetashape build completed successfully.
goto :cleanup

:license_fail
echo [ERROR] License compliance check failed.
goto :cleanup

:skill_fail
echo [ERROR] Skill validation failed.
goto :cleanup

:test_fail
echo [ERROR] Unit test suite failed.
goto :cleanup

:cleanup
popd
exit /b %BUILD_EXIT%
