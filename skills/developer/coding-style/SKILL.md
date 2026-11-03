---
name: coding-style
description: "Enforces repository coding conventions, SPDX license headers, line length limits, and import hygiene across Python, C++, CUDA, and Rust code. Use when writing, formatting, refactoring, or reviewing code across the codebase."
audience: developer
status: stable
owners:
  - "@autometashape-maintainers"
---

# Coding Style and Engineering Standards

This skill defines and enforces the authoritative coding standards, automated formatters, and style invariants across all languages in the repository, following the NVIDIA Isaac Sim and Isaac Lab engineering conventions.

---

## When To Use

Use this skill when:
- Authoring new source files in Python, C++, CUDA, or Rust.
- Refactoring existing modules or reviewing pull requests for style conformance.
- Ensuring every new or modified file contains valid SPDX license headers.
- Running or configuring automated code formatters and linting gates.

Do not use this skill when:
- Executing high-level architectural design or planning broad multi-agent workflows.

---

## Workflow

### 1. License & Header Invariants
Every source file must begin with the project's canonical SPDX license header:
```python
# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0
```

### 2. Python Engineering Standards
- Target line length: 120 characters max (`pyproject.toml`).
- Follow Google Python Style Guide with strict type annotations on all public functions.
- Maintain import hygiene: group standard library, third-party packages, and local modules separately.
- Ban raw `print()` for production logging; use structured loggers or explicit progress reporting.

### 3. Automated Formatting
Always execute repository formatters before submitting changes:
```powershell
./format_code.bat
```
On POSIX environments:
```bash
./format_code.sh
```

---

## Validation

Verify code compliance before committing:
1. Run pre-commit checks or formatting scripts (`./format_code.bat` or `./format_code.sh`).
2. Run license and SPDX audit:
   ```powershell
   python tools/check_licenses.py
   ```
3. Verify that zero formatting diffs remain unstaged.

---

## Maintenance

- Keep `pyproject.toml` linter configurations synchronized across all development environments.
- Verify that formatters run cleanly in CI (`.github/workflows/lint-and-format.yml`).

---

## References

- [CONTRIBUTING.md](../../../CONTRIBUTING.md)
- [AGENTS.md](../../../AGENTS.md)
- [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
