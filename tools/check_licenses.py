#!/usr/bin/env python3
# Copyright (c) 2026, AETHER Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""
Automated License & SPDX Compliance Validator
Modeled after NVIDIA Isaac Lab's license compliance gate.

Validates:
1. Source file SPDX headers (SPDX-License-Identifier: Apache-2.0).
2. Copyright sovereignty (assigned to AETHER Project Developers, zero NVIDIA claiming).
3. Existence and integrity of LICENSE and THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import os
import re
import sys
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

SOURCE_EXTENSIONS = {".py", ".cpp", ".h", ".hpp", ".cu"}

SPDX_PATTERN = re.compile(r"SPDX-License-Identifier:\s*Apache-2\.0", re.IGNORECASE)
FORBIDDEN_COPYRIGHT_PATTERN = re.compile(r"Copyright\s+(\(c\)\s+)?\d{4}(-\d{4})?,?\s*NVIDIA", re.IGNORECASE)


def check_source_spdx_headers() -> list[str]:
    errors = []
    scanned_count = 0

    for root, dirs, files in os.walk(REPO_ROOT):
        # Exclude directories
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".")]

        for file in files:
            file_path = Path(root) / file
            if file_path.suffix.lower() in SOURCE_EXTENSIONS:
                scanned_count += 1
                try:
                    content = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue

                rel_path = file_path.relative_to(REPO_ROOT).as_posix()

                # 1. Check for SPDX License Identifier
                if not SPDX_PATTERN.search(content[:500]):
                    errors.append(f"{rel_path}: Missing valid 'SPDX-License-Identifier: Apache-2.0' header")

                # 2. Check for unauthorized NVIDIA copyright claims
                if FORBIDDEN_COPYRIGHT_PATTERN.search(content[:500]):
                    errors.append(
                        f"{rel_path}: Contains unauthorized 'Copyright NVIDIA' claim. "
                        f"Project code must be owned by project authors."
                    )

    print(f"[INFO] Scanned {scanned_count} source files for SPDX headers.")
    return errors


def check_license_files() -> list[str]:
    errors = []

    license_file = REPO_ROOT / "LICENSE"
    if not license_file.is_file():
        errors.append("Missing root 'LICENSE' file.")
    else:
        content = license_file.read_text(encoding="utf-8")
        if "Apache License" not in content and "Version 2.0" not in content:
            errors.append("Root LICENSE does not match Apache-2.0 specification.")

    notices_file = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
    if not notices_file.is_file():
        errors.append("Missing canonical 'THIRD_PARTY_NOTICES.md' file.")
    else:
        content = notices_file.read_text(encoding="utf-8")
        if "Isaac Lab" not in content:
            errors.append("THIRD_PARTY_NOTICES.md missing required attribution entries.")

    header_template = REPO_ROOT / ".github" / "LICENSE_HEADER.txt"
    if not header_template.is_file():
        errors.append("Missing '.github/LICENSE_HEADER.txt' template.")

    return errors


def main() -> int:
    print("=========================================================================")
    print("  AETHER-SWARM: License Compliance & SPDX Audit (Isaac Lab Standard)")
    print("=========================================================================")

    all_errors = []

    print("\n[1/2] Auditing repository license files & notices...")
    license_errors = check_license_files()
    if license_errors:
        all_errors.extend(license_errors)
    else:
        print("[OK] LICENSE, THIRD_PARTY_NOTICES.md, and header templates verified.")

    print("\n[2/2] Auditing source code SPDX headers & copyright sovereignty...")
    spdx_errors = check_source_spdx_headers()
    if spdx_errors:
        all_errors.extend(spdx_errors)
    else:
        print("[OK] All source files contain valid SPDX-License-Identifier: Apache-2.0.")

    print("\n=========================================================================")
    if all_errors:
        print(f"[FAIL] License audit failed with {len(all_errors)} violation(s):")
        for err in all_errors:
            print(f"  - {err}")
        return 1

    print("[SUCCESS] License & IP compliance audit passed with 0 violations.")
    print("=========================================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
