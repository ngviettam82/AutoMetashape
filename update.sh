#!/usr/bin/env bash
# Copyright (c) 2026, AutoMetashape Contributors.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -d ".git" ] && command -v git &>/dev/null; then
    echo "[GIT DETECTED] Updating AutoMetashape in-place via git..."
    git fetch origin main
    git reset --hard origin/main
    echo "[SUCCESS] AutoMetashape updated to latest main branch."
    exit 0
fi

echo "[DOWNLOADING] Fetching latest archive from GitHub..."
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

curl -fsSL https://github.com/ngviettam82/AutoMetashape/archive/refs/heads/main.zip -o "$TMP_DIR/main.zip"
unzip -q "$TMP_DIR/main.zip" -d "$TMP_DIR/extracted"

SRC_DIR=$(find "$TMP_DIR/extracted" -mindepth 1 -maxdepth 1 -type d | head -n 1)
cp -rf "$SRC_DIR"/* "$SCRIPT_DIR"/

echo "[SUCCESS] AutoMetashape replaced and updated in-place (zero nesting)."
