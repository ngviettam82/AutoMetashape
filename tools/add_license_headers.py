#!/usr/bin/env python3
# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Adds or updates standardized SPDX Apache-2.0 license headers to all repository source files."""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXCLUDED_DIRS = {
    ".git",
    ".agents",
    "build",
    "dist",
    "recordings",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "eggs",
    ".eggs",
}

HASH_EXTENSIONS = {".py", ".sh"}
CPP_EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".cu", ".cuh"}
BAT_EXTENSIONS = {".bat"}
SUPPORTED_EXTENSIONS = HASH_EXTENSIONS | CPP_EXTENSIONS | BAT_EXTENSIONS

PYTHON_HEADER = """# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""

CPP_HEADER = """// Copyright (c) 2026, AutoMetashape Contributors.
// All rights reserved.
//
// SPDX-License-Identifier: Apache-2.0

"""

BAT_HEADER = """REM Copyright (c) 2026, AutoMetashape Contributors.
REM All rights reserved.
REM SPDX-License-Identifier: Apache-2.0

"""

PEP263_ENCODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


def update_headers():
    count = 0
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".")]

        for file in files:
            path = Path(root) / file
            suffix = path.suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS:
                continue

            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue

            # Check header region only (matches check_licenses.py)
            if "SPDX-License-Identifier: Apache-2.0" in content[:500]:
                continue

            if suffix in CPP_EXTENSIONS:
                header = CPP_HEADER
            elif suffix in BAT_EXTENSIONS:
                header = BAT_HEADER
            else:
                header = PYTHON_HEADER

            lines = content.splitlines(True)
            prefix_lines: list[str] = []
            idx = 0
            if suffix in HASH_EXTENSIONS and idx < len(lines) and lines[idx].startswith("#!"):
                prefix_lines.append(lines[idx])
                idx += 1
            if suffix == ".py" and idx < len(lines) and PEP263_ENCODING_RE.match(lines[idx]):
                prefix_lines.append(lines[idx])
                idx += 1
            if suffix in BAT_EXTENSIONS and idx < len(lines) and lines[idx].strip().lower() == "@echo off":
                prefix_lines.append(lines[idx])
                idx += 1

            rest = "".join(lines[idx:]).lstrip("\r\n")
            new_content = "".join(prefix_lines) + header + rest

            newline_mode = "\r\n" if suffix in BAT_EXTENSIONS else "\n"
            with open(path, "w", encoding="utf-8", newline=newline_mode) as fh:
                fh.write(new_content.replace("\r\n", "\n"))
            count += 1
            print(f"Added SPDX header: {path.relative_to(REPO_ROOT)}")

    print(f"\n[OK] Updated {count} files.")


if __name__ == "__main__":
    update_headers()
