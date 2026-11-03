@echo off
setlocal
cd /d "%~dp0"
echo =========================================================================
echo   AutoMetashape: Launching 3D Photogrammetry Studio GUI...
echo =========================================================================
python scripts\autometashape_gui.py %*
if errorlevel 1 pause
