#!/usr/bin/env python3
# Copyright (c) 2026, AETHER Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""
Adds or updates standardized SPDX Apache-2.0 license headers to all repository source files.
"""

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

PYTHON_HEADER = """# Copyright (c) 2026, AETHER Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""

CPP_HEADER = """// Copyright (c) 2026, AETHER Project Developers.
// All rights reserved.
//
// SPDX-License-Identifier: Apache-2.0

"""

def update_headers():
    count = 0
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".")]

        for file in files:
            path = Path(root) / file
            suffix = path.suffix.lower()
            if suffix not in {".py", ".cpp", ".h", ".hpp", ".cu"}:
                continue

            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue

            # Check if it already has SPDX
            if "SPDX-License-Identifier: Apache-2.0" in content:
                continue

            header = CPP_HEADER if suffix in {".cpp", ".h", ".hpp", ".cu"} else PYTHON_HEADER

            # Preserve shebang if present
            if content.startswith("#!"):
                lines = content.splitlines(True)
                shebang = lines[0]
                rest = "".join(lines[1:])
                new_content = shebang + "\n" + header + rest.lstrip()
            else:
                new_content = header + content.lstrip()

            path.write_text(new_content, encoding="utf-8")
            count += 1
            print(f"Added SPDX header: {path.relative_to(REPO_ROOT)}")

    print(f"\n[OK] Updated {count} files.")

if __name__ == "__main__":
    update_headers()
