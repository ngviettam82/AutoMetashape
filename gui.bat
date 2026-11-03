@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM 1. Detect Agisoft Metashape Pro bundled Python
set "PYTHON_EXE="
if exist "C:\Program Files\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_EXE=C:\Program Files\Agisoft\Metashape Pro\python\python.exe"
) else if exist "C:\Program Files (x86)\Agisoft\Metashape Pro\python\python.exe" (
    set "PYTHON_EXE=C:\Program Files (x86)\Agisoft\Metashape Pro\python\python.exe"
) else (
    REM 2. Fall back to system Python on PATH
    where python >nul 2>nul
    if !ERRORLEVEL! equ 0 (
        set "PYTHON_EXE=python"
    )
)

if "!PYTHON_EXE!"=="" (
    echo =========================================================================
    echo   [ERROR] Python executable not found.
    echo   Neither Agisoft Metashape Pro Python nor system Python was detected.
    echo   Please install Agisoft Metashape Pro or add Python to your system PATH.
    echo =========================================================================
    pause
    exit /b 1
)

echo =========================================================================
echo   AutoMetashape: Launching 3D Photogrammetry Studio GUI...
echo   Python Runtime: !PYTHON_EXE!
echo =========================================================================

"!PYTHON_EXE!" scripts\autometashape_gui.py %*
if errorlevel 1 pause
