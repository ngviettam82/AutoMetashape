@echo off
rem AutoMetashape Node Updater (Native Windows - Zero Git Dependency)
rem Copyright (c) 2026, AutoMetashape Contributors.
rem SPDX-License-Identifier: Apache-2.0

setlocal EnableDelayedExpansion
title AutoMetashape Node Updater

:: 0. Decoupled Staging: If running directly inside target codebase, hand off to temporary copy
:: This releases all open file locks on update.bat itself so the codebase can be mirrored cleanly.
if not defined AUTOMETASHAPE_UPDATING (
    set "AUTOMETASHAPE_UPDATING=1"
    set "AUTOMETASHAPE_ORIGINAL_DIR=%~dp0"
    set "STAGE_BAT=%TEMP%\AutoMetashape_updater.bat"
    copy /y "%~f0" "!STAGE_BAT!" >nul 2>&1
    cmd.exe /c ""!STAGE_BAT!" %*"
    exit /b !errorlevel!
)

echo =========================================================================
echo   AutoMetashape: Automated In-Place Node Updater (Release main)
echo =========================================================================

:: Resolve invocation directory (retaining original directory across temporary staging)
if not "%AUTOMETASHAPE_ORIGINAL_DIR%"=="" (
    set "SCRIPT_DIR=%AUTOMETASHAPE_ORIGINAL_DIR%"
) else (
    set "SCRIPT_DIR=%~dp0"
)
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

:: If launched from a UNC network share, target must always be local Documents folder.
:: If running inside a local AutoMetashape folder, update in place.
if "%SCRIPT_DIR:~0,2%"=="\\" (
    set "TARGET_DIR=%USERPROFILE%\Documents\AutoMetashape"
) else if exist "%SCRIPT_DIR%\launch_worker.bat" (
    set "TARGET_DIR=%SCRIPT_DIR%"
) else (
    set "TARGET_DIR=%USERPROFILE%\Documents\AutoMetashape"
)

:: Parse optional action flag early to correctly gate process termination (--server vs worker)
set "ACTION_FLAG=%~1"
set "FORWARD_ARGS="
if not "%ACTION_FLAG%"=="" (
    for /f "tokens=1,* delims= " %%a in ("%*") do set "FORWARD_ARGS=%%b"
)

:: Detect headless/non-interactive invocation so automated runs never block on pause
set "IS_HEADLESS="
for %%A in (%*) do (
    if /i "%%~A"=="--non-interactive" set "IS_HEADLESS=1"
    if /i "%%~A"=="--headless" set "IS_HEADLESS=1"
)

set "REPO_ZIP_URL=https://github.com/ngviettam82/AutoMetashape/archive/refs/heads/main.zip"
set "TEMP_ZIP=%TEMP%\AutoMetashape_main.zip"
set "TEMP_EXTRACT=%TEMP%\AutoMetashape_extracted"
set "WORKER_PID_FILE=%TEMP%\autometashape_worker.pid"
set "SERVER_PID_FILE=%TEMP%\autometashape_server.pid"

echo [1/4] Stopping tracked AutoMetashape cluster processes (PID-isolated)...
:: Terminate tracked worker Python process tree by PID (preserves unrelated interactive Metashape GUI sessions)
if exist "%WORKER_PID_FILE%" (
    for /f "usebackq delims=" %%p in ("%WORKER_PID_FILE%") do (
        if not "%%p"=="" taskkill /F /T /PID %%p >nul 2>&1
    )
    del /f /q "%WORKER_PID_FILE%" >nul 2>&1
)

:: Terminate tracked server Python process tree by PID only if updating server
if "%ACTION_FLAG%"=="--server" (
    if exist "%SERVER_PID_FILE%" (
        for /f "usebackq delims=" %%p in ("%SERVER_PID_FILE%") do (
            if not "%%p"=="" taskkill /F /T /PID %%p >nul 2>&1
        )
        del /f /q "%SERVER_PID_FILE%" >nul 2>&1
    )
)

:: Terminate AutoMetashape-owned launcher console windows and worker/server processes via command-line isolation
taskkill /F /T /FI "WINDOWTITLE eq AutoMetashape Cluster Worker Node*" >nul 2>&1
if "%ACTION_FLAG%"=="--server" (
    taskkill /F /T /FI "WINDOWTITLE eq AutoMetashape Cluster Server Daemon*" >nul 2>&1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$myPid = $PID; $stopServer = ('%ACTION_FLAG%' -eq '--server'); " ^
    "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { " ^
    "    $_.ProcessId -ne $myPid -and ( " ^
    "        ($_.Name -ieq 'metashape.exe' -and $_.CommandLine -like '*--worker*') -or " ^
    "        ($stopServer -and $_.Name -ieq 'metashape-server.exe') -or " ^
    "        (($_.Name -like 'python*' -or $_.Name -ieq 'py.exe') -and ($_.CommandLine -like '*launch_worker*' -or ($stopServer -and $_.CommandLine -like '*launch_server*'))) " ^
    "    ) " ^
    "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
timeout /t 1 /nobreak >nul

echo [2/4] Downloading latest AutoMetashape source code from GitHub (main)...
if exist "%TEMP_ZIP%" del /f /q "%TEMP_ZIP%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; try { $wc = New-Object System.Net.WebClient; $wc.DownloadFile('%REPO_ZIP_URL%', '%TEMP_ZIP%') } catch { Write-Host '[ERROR] Download exception:' $_.Exception.Message; exit 1 }"

if %ERRORLEVEL% neq 0 (
    echo [ERROR] Failed to download latest AutoMetashape release from upstream: %REPO_ZIP_URL%
    echo [ERROR] Silent fallback is disabled. Please verify internet access and proxy settings.
    if not defined IS_HEADLESS pause
    exit /b 1
)

if not exist "%TEMP_ZIP%" (
    echo [ERROR] Downloaded package file missing: %TEMP_ZIP%
    echo [ERROR] Silent fallback is disabled.
    if not defined IS_HEADLESS pause
    exit /b 1
)

echo [3/4] Extracting archive natively via PowerShell Expand-Archive...
if exist "%TEMP_EXTRACT%" rmdir /s /q "%TEMP_EXTRACT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "Expand-Archive -Path '%TEMP_ZIP%' -DestinationPath '%TEMP_EXTRACT%' -Force"
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Failed to extract archive.
    if not defined IS_HEADLESS pause
    exit /b 1
)

:: Locate extracted root directory dynamically
set "EXTRACT_SOURCE="
if exist "%TEMP_EXTRACT%\AutoMetashape-main" set "EXTRACT_SOURCE=%TEMP_EXTRACT%\AutoMetashape-main"
if "!EXTRACT_SOURCE!"=="" (
    for /d %%D in ("%TEMP_EXTRACT%\*") do (
        if exist "%%D\launch_worker.bat" set "EXTRACT_SOURCE=%%D"
    )
)
if "!EXTRACT_SOURCE!"=="" (
    if exist "%TEMP_EXTRACT%\launch_worker.bat" set "EXTRACT_SOURCE=%TEMP_EXTRACT%"
)

if "!EXTRACT_SOURCE!"=="" (
    echo [ERROR] Could not locate AutoMetashape source directory inside extracted archive.
    if not defined IS_HEADLESS pause
    exit /b 1
)

echo [4/4] Replacing codebase in %TARGET_DIR% (Purging obsolete files, preserving config)...
if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"

:: Backup existing configuration files
set "CFG_BACKUP=%TEMP%\autometashape_worker_config_backup.json"
if exist "%TARGET_DIR%\worker_config.json" copy /y "%TARGET_DIR%\worker_config.json" "%CFG_BACKUP%" >nul 2>&1

:: Mirror directory: copies new files, overwrites changed/older files, prunes obsolete files, preserves configs and git history
robocopy "!EXTRACT_SOURCE!" "%TARGET_DIR%" /MIR /XD .git /XF worker_config.json server_config.json /W:1 /R:2 /NDL /NJH /NP
set "ROBO_CODE=!ERRORLEVEL!"

:: Ensure configuration is restored if it was missing from source
if exist "%CFG_BACKUP%" (
    if not exist "%TARGET_DIR%\worker_config.json" copy /y "%CFG_BACKUP%" "%TARGET_DIR%\worker_config.json" >nul 2>&1
    del /f /q "%CFG_BACKUP%" >nul 2>&1
)

:: Clean up temporary files
del /f /q "%TEMP_ZIP%" >nul 2>&1
if exist "%TEMP_EXTRACT%" rmdir /s /q "%TEMP_EXTRACT%" >nul 2>&1

:: Verify robocopy completed without fatal errors (codes 0-7 indicate success; >=8 indicates copy failure/locked files)
if !ROBO_CODE! GEQ 8 (
    echo.
    echo =========================================================================
    echo   [ERROR] Robocopy encountered fatal errors mirroring files (Exit code !ROBO_CODE!).
    echo   Some files could not be overwritten because they are locked by a running
    echo   Python process, Metashape Pro, or code editor.
    echo   Please close running applications and re-run update.bat.
    echo =========================================================================
    if not defined IS_HEADLESS pause
    exit /b !ROBO_CODE!
)

echo =========================================================================
echo   [SUCCESS] AutoMetashape codebase cleanly replaced and updated in-place!
echo   Target: %TARGET_DIR%
echo =========================================================================

if "%ACTION_FLAG%"=="--worker" (
    echo Launching cluster worker daemon...
    cd /d "%TARGET_DIR%"

    :: Smart UNC Share Auto-Inference: if no --host/--root passed and SCRIPT_DIR is a UNC path
    set "HAS_HOST=0"
    set "HAS_ROOT=0"
    if defined FORWARD_ARGS (
        echo !FORWARD_ARGS! | findstr /i /c:"--host" >nul 2>&1
        if !errorlevel! equ 0 set "HAS_HOST=1"
        echo !FORWARD_ARGS! | findstr /i /c:"--root" >nul 2>&1
        if !errorlevel! equ 0 set "HAS_ROOT=1"
    )

    if "%SCRIPT_DIR:~0,2%"=="\\" (
        for /f "tokens=1,2 delims=\\" %%i in ("%SCRIPT_DIR:~2%") do (
            if !HAS_HOST! equ 0 (
                set "INFERRED_HOST=%%i"
                set "FORWARD_ARGS=--host !INFERRED_HOST! !FORWARD_ARGS!"
                echo [AUTO-INFERRED] Inferred cluster server: !INFERRED_HOST!
            )
            if !HAS_ROOT! equ 0 (
                set "INFERRED_ROOT=\\%%i\%%j"
                set "FORWARD_ARGS=--root !INFERRED_ROOT! !FORWARD_ARGS!"
                echo [AUTO-INFERRED] Inferred storage root: !INFERRED_ROOT!
            )
        )
    )

    :: Ensure non-interactive execution for headless node updates
    set "HAS_NON_INT=0"
    if defined FORWARD_ARGS (
        echo !FORWARD_ARGS! | findstr /i /c:"--non-interactive" >nul 2>&1
        if !errorlevel! equ 0 set "HAS_NON_INT=1"
        echo !FORWARD_ARGS! | findstr /i /c:"--headless" >nul 2>&1
        if !errorlevel! equ 0 set "HAS_NON_INT=1"
    )
    if !HAS_NON_INT! equ 0 (
        set "FORWARD_ARGS=--non-interactive !FORWARD_ARGS!"
    )

    start "AutoMetashape Cluster Worker Node" cmd.exe /c "launch_worker.bat !FORWARD_ARGS!"
) else if "%ACTION_FLAG%"=="--server" (
    echo Launching cluster server daemon...
    cd /d "%TARGET_DIR%"
    start "AutoMetashape Cluster Server Daemon" cmd.exe /c "launch_server.bat !FORWARD_ARGS!"
) else (
    echo Node files are ready in %TARGET_DIR%.
)
