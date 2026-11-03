@echo off
REM Copyright (c) 2026, AutoMetashape Contributors.
REM All rights reserved.
REM SPDX-License-Identifier: Apache-2.0

echo [BUILD] Installing AutoMetashape package in editable mode...
python -m pip install -e .
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Package installation failed.
    exit /b %ERRORLEVEL%
)
echo [BUILD] Validating agent skills...
python tools/skills/cli.py check
if %ERRORLEVEL% neq 0 (
    echo [ERROR] Skill validation failed.
    exit /b %ERRORLEVEL%
)
echo [BUILD] AutoMetashape build completed successfully.
