@echo off
REM Copyright (c) 2026, GaussianSplat Contributors.
REM All rights reserved.
REM SPDX-License-Identifier: Apache-2.0

echo [BUILD] Installing GaussianSplat subsystem in editable mode...
python -m pip install -e source/extensions/gaussian.splat
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Build failed.
    exit /b %ERRORLEVEL%
)
echo [BUILD] Validating agent skills...
python tools/skills/cli.py check
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Skill validation failed.
    exit /b %ERRORLEVEL%
)
echo [BUILD] Build completed successfully.
