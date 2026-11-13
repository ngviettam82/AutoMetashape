@echo off
rem Copyright (c) 2026, AutoMetashape Contributors.
rem SPDX-License-Identifier: Apache-2.0

chcp 65001 > nul
setlocal enabledelayedexpansion
title AutoMetashape - In-Place Updater

echo =======================================================================
echo   AutoMetashape - 1-Click In-Place Updater
echo =======================================================================
echo.

set "TARGET_DIR=%~dp0"
if "%TARGET_DIR:~-1%"=="\" set "TARGET_DIR=%TARGET_DIR:~0,-1%"

:: 1. Check if git repository is present
where git.exe >nul 2>nul
if !errorlevel! equ 0 (
    if exist "%TARGET_DIR%\.git" (
        echo [GIT DETECTED] Updating AutoMetashape in-place via git...
        cd /d "%TARGET_DIR%"
        git fetch origin main
        if !errorlevel! equ 0 (
            git reset --hard origin/main
            if !errorlevel! equ 0 (
                echo.
                echo =======================================================================
                echo   [SUCCESS] AutoMetashape updated to latest main branch!
                echo   Configuration files (worker_config.json) preserved.
                echo =======================================================================
                pause
                exit /b 0
            )
        )
        echo [WARNING] Git pull encountered an issue. Falling back to direct GitHub archive download...
    )
)

:: 2. Download and in-place replace (Zero Nesting)
echo [DOWNLOADING] Fetching latest production release from GitHub...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$repoZip = 'https://github.com/ngviettam82/AutoMetashape/archive/refs/heads/main.zip'; " ^
    "$tempDir = Join-Path $env:TEMP ('autometashape_update_' + [System.Guid]::NewGuid().ToString('N')); " ^
    "$zipPath = Join-Path $tempDir 'main.zip'; " ^
    "$extractPath = Join-Path $tempDir 'extracted'; " ^
    "$targetDir = '%TARGET_DIR%'; " ^
    "try { " ^
    "    New-Item -ItemType Directory -Path $extractPath -Force | Out-Null; " ^
    "    Write-Host '  [1/3] Downloading latest main.zip...'; " ^
    "    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; " ^
    "    Invoke-WebRequest -Uri $repoZip -OutFile $zipPath -UseBasicParsing; " ^
    "    Write-Host '  [2/3] Extracting archive...'; " ^
    "    Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force; " ^
    "    $sourceRoot = Get-ChildItem -Path $extractPath | Where-Object { $_.PSIsContainer } | Select-Object -First 1; " ^
    "    if (-not $sourceRoot) { throw 'Extraction root folder not found.'; } " ^
    "    Write-Host '  [3/3] Replacing files in place (zero nesting)...'; " ^
    "    Get-ChildItem -Path $sourceRoot.FullName -Force | ForEach-Object { " ^
    "        Copy-Item -Path $_.FullName -Destination $targetDir -Recurse -Force; " ^
    "    }; " ^
    "    Write-Host ''; " ^
    "    Write-Host '=======================================================================' -ForegroundColor Green; " ^
    "    Write-Host '  [SUCCESS] AutoMetashape replaced and updated in-place!' -ForegroundColor Green; " ^
    "    Write-Host ('  Folder: ' + $targetDir) -ForegroundColor Green; " ^
    "    Write-Host '  No nested folders created. Existing configurations preserved.' -ForegroundColor Green; " ^
    "    Write-Host '=======================================================================' -ForegroundColor Green; " ^
    "} catch { " ^
    "    Write-Host ('[ERROR] Update failed: ' + $_.Exception.Message) -ForegroundColor Red; " ^
    "    exit 1; " ^
    "} finally { " ^
    "    if (Test-Path $tempDir) { Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue; } " ^
    "}"

if !errorlevel! neq 0 (
    echo.
    echo [ERROR] Update could not complete.
    pause
    exit /b 1
)

echo.
pause
