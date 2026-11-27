@echo off
rem AutoMetashape Node Updater (Native Windows - Zero Git Dependency)
rem Copyright (c) 2026, AutoMetashape Contributors.
rem SPDX-License-Identifier: Apache-2.0

chcp 65001 >nul 2>&1
setlocal DisableDelayedExpansion
title AutoMetashape Node Updater

:: 0. Decoupled Staging: If running directly inside target codebase, hand off to temporary copy
:: This releases all open file locks on update.bat itself so the codebase can be mirrored cleanly.
if not defined AUTOMETASHAPE_UPDATING goto :stage_self
goto :main

:stage_self
set "AUTOMETASHAPE_UPDATING=1"
set "AUTOMETASHAPE_ORIGINAL_DIR=%~dp0"
set "STAGE_BAT=%TEMP%\AutoMetashape_updater_%RANDOM%_%RANDOM%.bat"
copy /y "%~f0" "%STAGE_BAT%" >nul 2>&1
cmd.exe /c ""%STAGE_BAT%" %*"
set "STAGE_EXIT=%ERRORLEVEL%"
del /f /q "%STAGE_BAT%" >nul 2>&1
exit /b %STAGE_EXIT%

:main
echo =========================================================================
echo   AutoMetashape: Automated In-Place Node Updater (Release main)
echo =========================================================================

:: Resolve invocation directory (retaining original directory across temporary staging)
if defined AUTOMETASHAPE_ORIGINAL_DIR (
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

:: Parse optional action flag and headless flags safely via shift (no wildcard expansion)
set "ACTION_FLAG=%~1"
set "FORWARD_ARGS="
set "IS_HEADLESS="
set "HAS_HOST=0"
set "HAS_ROOT=0"
set "HAS_NON_INT=0"
call :parse_args %*

if not defined AUTOMETASHAPE_REPO_ZIP_URL (
    set "REPO_ZIP_URL=https://github.com/ngviettam82/AutoMetashape/archive/refs/heads/main.zip"
) else (
    set "REPO_ZIP_URL=%AUTOMETASHAPE_REPO_ZIP_URL%"
)
set "RUN_NONCE=%RANDOM%_%RANDOM%"
set "TEMP_ZIP=%TEMP%\AutoMetashape_main_%RUN_NONCE%.zip"
set "TEMP_EXTRACT=%TEMP%\AutoMetashape_extracted_%RUN_NONCE%"
set "WORKER_PID_FILE=%TEMP%\autometashape_worker.pid"
set "SERVER_PID_FILE=%TEMP%\autometashape_server.pid"

echo [1/4] Stopping tracked AutoMetashape cluster processes (PID-isolated)...
:: Terminate tracked worker Python process tree by PID only if process image matches Python/Metashape
if exist "%WORKER_PID_FILE%" (
    for /f "usebackq delims=" %%p in ("%WORKER_PID_FILE%") do (
        if not "%%p"=="" (
            set "AM_CHK_PID=%%p"
            powershell -NoProfile -ExecutionPolicy Bypass -Command ^
                "try { $pr = Get-Process -Id ([int]$env:AM_CHK_PID) -ErrorAction Stop; if ($pr.ProcessName -match '^(python|py|metashape)') { taskkill /F /T /PID $pr.Id | Out-Null } } catch {}" >nul 2>&1
        )
    )
    del /f /q "%WORKER_PID_FILE%" >nul 2>&1
)

:: Terminate tracked server Python process tree by PID only if updating server
if /i "%ACTION_FLAG%"=="--server" (
    if exist "%SERVER_PID_FILE%" (
        for /f "usebackq delims=" %%p in ("%SERVER_PID_FILE%") do (
            if not "%%p"=="" (
                set "AM_CHK_PID=%%p"
                powershell -NoProfile -ExecutionPolicy Bypass -Command ^
                    "try { $pr = Get-Process -Id ([int]$env:AM_CHK_PID) -ErrorAction Stop; if ($pr.ProcessName -match '^(python|py|metashape)') { taskkill /F /T /PID $pr.Id | Out-Null } } catch {}" >nul 2>&1
            )
        )
        del /f /q "%SERVER_PID_FILE%" >nul 2>&1
    )
)

:: Terminate AutoMetashape-owned launcher console windows and worker/server processes via command-line isolation
taskkill /F /T /FI "WINDOWTITLE eq AutoMetashape Cluster Worker Node*" >nul 2>&1
if /i "%ACTION_FLAG%"=="--server" (
    taskkill /F /T /FI "WINDOWTITLE eq AutoMetashape Cluster Server Daemon*" >nul 2>&1
)

set "AM_ACTION_FLAG=%ACTION_FLAG%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$myPid = $PID; $stopServer = ($env:AM_ACTION_FLAG -ieq '--server'); " ^
    "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { " ^
    "    $_.ProcessId -ne $myPid -and ( " ^
    "        ($_.Name -ieq 'metashape.exe' -and $_.CommandLine -like '*--worker*') -or " ^
    "        ($stopServer -and $_.Name -ieq 'metashape-server.exe') -or " ^
    "        (($_.Name -like 'python*' -or $_.Name -ieq 'py.exe') -and ($_.CommandLine -like '*launch_worker*' -or ($stopServer -and $_.CommandLine -like '*launch_server*'))) " ^
    "    ) " ^
    "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
ping -n 2 127.0.0.1 >nul

echo [2/4] Downloading latest AutoMetashape source code from GitHub (main)...
if exist "%TEMP_ZIP%" del /f /q "%TEMP_ZIP%"
set "AM_REPO_ZIP_URL=%REPO_ZIP_URL%"
set "AM_TEMP_ZIP=%TEMP_ZIP%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; " ^
    "try { " ^
    "    $uri = [uri]$env:AM_REPO_ZIP_URL; " ^
    "    if ($uri.Scheme -ine 'https') { throw ('Refusing non-HTTPS archive URL: ' + $env:AM_REPO_ZIP_URL) } " ^
    "    $wc = New-Object System.Net.WebClient; $wc.DownloadFile($env:AM_REPO_ZIP_URL, $env:AM_TEMP_ZIP); " ^
    "    $expectedSha = ($env:AUTOMETASHAPE_REPO_ZIP_SHA256 + '').Trim().ToLowerInvariant(); " ^
    "    if ($expectedSha) { " ^
    "        $actualSha = (Get-FileHash -LiteralPath $env:AM_TEMP_ZIP -Algorithm SHA256).Hash.ToLowerInvariant(); " ^
    "        if ($actualSha -ne $expectedSha) { throw ('SHA-256 mismatch: expected ' + $expectedSha + ', got ' + $actualSha) } " ^
    "    } " ^
    "} catch { Write-Host '[ERROR] Download exception:' $_.Exception.Message; exit 1 }"

if errorlevel 1 (
    echo [ERROR] Failed to download latest AutoMetashape release from upstream: "%REPO_ZIP_URL%"
    echo [ERROR] Silent fallback is disabled. Please verify internet access and proxy settings.
    goto :fail_cleanup
)

if not exist "%TEMP_ZIP%" (
    echo [ERROR] Downloaded package file missing: "%TEMP_ZIP%"
    echo [ERROR] Silent fallback is disabled.
    goto :fail_cleanup
)

echo [3/4] Extracting archive natively via PowerShell Expand-Archive...
if exist "%TEMP_EXTRACT%" rmdir /s /q "%TEMP_EXTRACT%"
set "AM_TEMP_EXTRACT=%TEMP_EXTRACT%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "try { " ^
    "    Add-Type -AssemblyName System.IO.Compression.FileSystem; " ^
    "    $destFull = [System.IO.Path]::GetFullPath($env:AM_TEMP_EXTRACT); " ^
    "    $destPrefix = if ($destFull.EndsWith([System.IO.Path]::DirectorySeparatorChar)) { $destFull } else { $destFull + [System.IO.Path]::DirectorySeparatorChar }; " ^
    "    $zip = [System.IO.Compression.ZipFile]::OpenRead($env:AM_TEMP_ZIP); " ^
    "    try { " ^
    "        foreach ($entry in $zip.Entries) { " ^
    "            $resolved = [System.IO.Path]::GetFullPath((Join-Path $destFull $entry.FullName)); " ^
    "            if (-not ($resolved.StartsWith($destPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or $resolved.Equals($destFull, [System.StringComparison]::OrdinalIgnoreCase))) { " ^
    "                throw ('Zip Slip path traversal detected: ' + $entry.FullName) " ^
    "            } " ^
    "        } " ^
    "    } finally { $zip.Dispose() } " ^
    "    Expand-Archive -LiteralPath $env:AM_TEMP_ZIP -DestinationPath $env:AM_TEMP_EXTRACT -Force " ^
    "} catch { Write-Host '[ERROR] Extract exception:' $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo [ERROR] Failed to extract archive.
    goto :fail_cleanup
)

:: Locate extracted root directory dynamically
set "EXTRACT_SOURCE="
if exist "%TEMP_EXTRACT%\AutoMetashape-main" set "EXTRACT_SOURCE=%TEMP_EXTRACT%\AutoMetashape-main"
if not defined EXTRACT_SOURCE (
    for /d %%D in ("%TEMP_EXTRACT%\*") do (
        if not defined EXTRACT_SOURCE if exist "%%D\launch_worker.bat" set "EXTRACT_SOURCE=%%D"
    )
)
if not defined EXTRACT_SOURCE (
    if exist "%TEMP_EXTRACT%\launch_worker.bat" set "EXTRACT_SOURCE=%TEMP_EXTRACT%"
)

if not defined EXTRACT_SOURCE (
    echo [ERROR] Could not locate AutoMetashape source directory inside extracted archive.
    goto :fail_cleanup
)

echo [4/4] Replacing codebase in "%TARGET_DIR%" (Purging obsolete files, preserving config)...
if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"

:: Backup existing configuration files with unique run nonce
set "CFG_BACKUP=%TEMP%\autometashape_worker_config_%RUN_NONCE%.json"
set "SRV_CFG_BACKUP=%TEMP%\autometashape_server_config_%RUN_NONCE%.json"
if exist "%TARGET_DIR%\worker_config.json" copy /y "%TARGET_DIR%\worker_config.json" "%CFG_BACKUP%" >nul 2>&1
if exist "%TARGET_DIR%\server_config.json" copy /y "%TARGET_DIR%\server_config.json" "%SRV_CFG_BACKUP%" >nul 2>&1

:: Mirror directory: copies new files, overwrites changed/older files, prunes obsolete files,
:: preserves configs, git history, runtime signals, and untracked .agents/ developer notes.
robocopy "%EXTRACT_SOURCE%" "%TARGET_DIR%" /MIR /XD .git .agents .cluster .venv .idea .vscode /XF worker_config.json server_config.json .update_pending .abort_pending *.log *.pid /W:1 /R:2 /NDL /NJH /NP
set "ROBO_CODE=%ERRORLEVEL%"

:: Sync tracked skill discovery metadata under .agents\ without disturbing untracked .agents\ folders
if %ROBO_CODE% LSS 8 (
    if exist "%EXTRACT_SOURCE%\.agents\skills.json" (
        if not exist "%TARGET_DIR%\.agents" mkdir "%TARGET_DIR%\.agents"
        copy /y "%EXTRACT_SOURCE%\.agents\skills.json" "%TARGET_DIR%\.agents\skills.json" >nul 2>&1
    )
    if exist "%EXTRACT_SOURCE%\.agents\skills" (
        robocopy "%EXTRACT_SOURCE%\.agents\skills" "%TARGET_DIR%\.agents\skills" /MIR /W:1 /R:2 /NDL /NJH /NP >nul 2>&1
    )
)

:: Ensure configuration is restored if it was missing from source
if exist "%CFG_BACKUP%" (
    if not exist "%TARGET_DIR%\worker_config.json" copy /y "%CFG_BACKUP%" "%TARGET_DIR%\worker_config.json" >nul 2>&1
    del /f /q "%CFG_BACKUP%" >nul 2>&1
)
if exist "%SRV_CFG_BACKUP%" (
    if not exist "%TARGET_DIR%\server_config.json" copy /y "%SRV_CFG_BACKUP%" "%TARGET_DIR%\server_config.json" >nul 2>&1
    del /f /q "%SRV_CFG_BACKUP%" >nul 2>&1
)

:: Clean up temporary files
if exist "%TEMP_ZIP%" del /f /q "%TEMP_ZIP%" >nul 2>&1
if exist "%TEMP_EXTRACT%" rmdir /s /q "%TEMP_EXTRACT%" >nul 2>&1

:: Verify robocopy completed without fatal errors (codes 0-7 indicate success; >=8 indicates copy failure/locked files)
if %ROBO_CODE% GEQ 8 (
    echo.
    echo =========================================================================
    echo   [ERROR] Robocopy encountered fatal errors mirroring files ^(Exit code %ROBO_CODE%^).
    echo   Some files could not be overwritten because they are locked by a running
    echo   Python process, Metashape Pro, or code editor.
    echo   Please close running applications and re-run update.bat.
    echo =========================================================================
    if not defined IS_HEADLESS pause
    exit /b %ROBO_CODE%
)

echo =========================================================================
echo   [SUCCESS] AutoMetashape codebase cleanly replaced and updated in-place!
echo   Target: "%TARGET_DIR%"
echo =========================================================================

if /i "%ACTION_FLAG%"=="--worker" (
    echo Launching cluster worker daemon...
    cd /d "%TARGET_DIR%"

    :: Smart UNC Share Auto-Inference: only infer if worker_config.json is absent or override is set
    set "ALLOW_UNC_INFER=0"
    if not exist "%TARGET_DIR%\worker_config.json" set "ALLOW_UNC_INFER=1"
    if defined AUTOMETASHAPE_INFER_HOST_FROM_UNC set "ALLOW_UNC_INFER=1"
    if "%ALLOW_UNC_INFER%"=="1" if "%SCRIPT_DIR:~0,2%"=="\\" (
        for /f "tokens=1,2 delims=\\" %%i in ("%SCRIPT_DIR:~2%") do (
            if "%HAS_HOST%"=="0" (
                set "INFERRED_HOST=%%i"
                echo [AUTO-INFERRED] Inferred cluster server: %%i
            )
            if "%HAS_ROOT%"=="0" (
                set "INFERRED_ROOT=\\%%i\%%j"
                echo [AUTO-INFERRED] Inferred storage root: "\\%%i\%%j"
            )
        )
    )
    if defined INFERRED_HOST set "FORWARD_ARGS=--host %INFERRED_HOST% %FORWARD_ARGS%"
    if defined INFERRED_ROOT set "FORWARD_ARGS=--root "%INFERRED_ROOT%" %FORWARD_ARGS%"

    :: Ensure non-interactive execution for headless node updates
    if "%HAS_NON_INT%"=="0" set "FORWARD_ARGS=--non-interactive %FORWARD_ARGS%"

    start "AutoMetashape Cluster Worker Node" cmd.exe /c ""%TARGET_DIR%\launch_worker.bat" %FORWARD_ARGS%"
) else if /i "%ACTION_FLAG%"=="--server" (
    echo Launching cluster server daemon...
    cd /d "%TARGET_DIR%"
    start "AutoMetashape Cluster Server Daemon" cmd.exe /c ""%TARGET_DIR%\launch_server.bat" %FORWARD_ARGS%"
) else (
    echo Node files are ready in "%TARGET_DIR%".
)
exit /b 0

:parse_args
set "_ARG_IDX=0"
:parse_loop
if [%1]==[] goto :eof
set "_CUR=%~1"
set "_CUR_RAW=%1"
if /i "%_CUR%"=="--non-interactive" (
    set "IS_HEADLESS=1"
    set "HAS_NON_INT=1"
)
if /i "%_CUR%"=="--headless" (
    set "IS_HEADLESS=1"
    set "HAS_NON_INT=1"
)
if /i "%_CUR%"=="--host" set "HAS_HOST=1"
if /i "%_CUR:~0,7%"=="--host=" set "HAS_HOST=1"
if /i "%_CUR%"=="--root" set "HAS_ROOT=1"
if /i "%_CUR:~0,7%"=="--root=" set "HAS_ROOT=1"
if /i "%_CUR%"=="--result-root" set "HAS_ROOT=1"
if /i "%_CUR:~0,14%"=="--result-root=" set "HAS_ROOT=1"
if /i "%_CUR%"=="--result-share" set "HAS_ROOT=1"
if /i "%_CUR:~0,15%"=="--result-share=" set "HAS_ROOT=1"
if "%_ARG_IDX%"=="0" (
    set "_ARG_IDX=1"
) else (
    if defined FORWARD_ARGS (
        set "FORWARD_ARGS=%FORWARD_ARGS% %_CUR_RAW%"
    ) else (
        set "FORWARD_ARGS=%_CUR_RAW%"
    )
)
shift
goto :parse_loop

:fail_cleanup
if exist "%TEMP_ZIP%" del /f /q "%TEMP_ZIP%" >nul 2>&1
if exist "%TEMP_EXTRACT%" rmdir /s /q "%TEMP_EXTRACT%" >nul 2>&1
if not defined IS_HEADLESS pause
exit /b 1
