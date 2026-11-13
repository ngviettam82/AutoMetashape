@echo off
rem AutoMetashape Node Updater (Native Windows - Zero Git Dependency)
rem Copyright (c) 2026, AutoMetashape Contributors.
rem SPDX-License-Identifier: Apache-2.0

setlocal EnableDelayedExpansion
title AutoMetashape Node Updater

echo =========================================================================
echo   AutoMetashape: Automated In-Place Node Updater (Release main)
echo =========================================================================

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

:: If running inside an existing AutoMetashape directory, update in place; otherwise use Documents
if exist "%SCRIPT_DIR%\launch_worker.bat" (
    set "TARGET_DIR=%SCRIPT_DIR%"
) else (
    set "TARGET_DIR=%USERPROFILE%\Documents\AutoMetashape"
)

set "REPO_ZIP_URL=https://github.com/ngviettam82/AutoMetashape/archive/refs/heads/main.zip"
if not "%AUTOMETASHAPE_LAN_ZIP%"=="" (
    set "LAN_ZIP=%AUTOMETASHAPE_LAN_ZIP%"
) else (
    set "LAN_ZIP=\\SERVER\SharedMetashape\AutoMetashape_main.zip"
)
set "TEMP_ZIP=%TEMP%\AutoMetashape_main.zip"
set "TEMP_EXTRACT=%TEMP%\AutoMetashape_extracted"

echo [1/4] Terminating active Metashape worker processes...
taskkill /F /IM metashape.exe >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq AutoMetashape Cluster Worker Node*" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*launch_worker*' -or $_.CommandLine -like '*--worker*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
timeout /t 2 /nobreak >nul

echo [2/4] Downloading latest AutoMetashape source code from GitHub (main)...
if exist "%TEMP_ZIP%" del /f /q "%TEMP_ZIP%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object System.Net.WebClient).DownloadFile('%REPO_ZIP_URL%', '%TEMP_ZIP%')" >nul 2>&1

if not exist "%TEMP_ZIP%" (
    echo [NOTICE] GitHub download unavailable. Checking LAN cache fallback...
    if exist "%LAN_ZIP%" copy /y "%LAN_ZIP%" "%TEMP_ZIP%" >nul 2>&1
)

if not exist "%TEMP_ZIP%" (
    echo [ERROR] Could not retrieve AutoMetashape package from GitHub or LAN share.
    pause
    exit /b 1
)

echo [3/4] Extracting archive natively via PowerShell Expand-Archive...
if exist "%TEMP_EXTRACT%" rmdir /s /q "%TEMP_EXTRACT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Expand-Archive -Path '%TEMP_ZIP%' -DestinationPath '%TEMP_EXTRACT%' -Force"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Failed to extract archive.
    pause
    exit /b 1
)

echo [4/4] Placing fresh upstream AutoMetashape into %TARGET_DIR%...
if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"

:: Move files directly in place without nesting AutoMetashape-main
robocopy "%TEMP_EXTRACT%\AutoMetashape-main" "%TARGET_DIR%" /E /MOVE /NFL /NDL /NJH /NJS /nc /ns /np >nul 2>&1

:: Clean up any nested folder left behind by older manual unzips
if exist "%TARGET_DIR%\AutoMetashape-main" (
    robocopy "%TARGET_DIR%\AutoMetashape-main" "%TARGET_DIR%" /E /MOVE /NFL /NDL /NJH /NJS /nc /ns /np >nul 2>&1
    rmdir /s /q "%TARGET_DIR%\AutoMetashape-main" >nul 2>&1
)

del /f /q "%TEMP_ZIP%" >nul 2>&1
if exist "%TEMP_EXTRACT%" rmdir /s /q "%TEMP_EXTRACT%" >nul 2>&1

:: Write default worker configuration if none exists
if not exist "%TARGET_DIR%\worker_config.json" (
    (
        echo {
        echo   "host": "192.168.1.100",
        echo   "port": "5840",
        echo   "root": "\\\\SERVER\\SharedMetashape",
        echo   "cpu_enable": 1,
        echo   "gpu_mask": "1"
        echo }
    ) > "%TARGET_DIR%\worker_config.json"
)

echo =========================================================================
echo   [SUCCESS] AutoMetashape node successfully updated in-place (Zero Nesting)!
echo   Target: %TARGET_DIR%
echo =========================================================================

:: Optional launch flags
if "%~1"=="--worker" (
    echo Launching cluster worker daemon...
    cd /d "%TARGET_DIR%"
    start "AutoMetashape Cluster Worker Node" cmd.exe /c "launch_worker.bat %*"
) else if "%~1"=="--server" (
    echo Launching cluster server daemon...
    cd /d "%TARGET_DIR%"
    start "AutoMetashape Cluster Server Daemon" cmd.exe /c "launch_server.bat %*"
) else (
    echo Node files are ready in %TARGET_DIR%.
)
