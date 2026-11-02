---
name: coding-style
description: "Enforces repository coding conventions, SPDX license headers, line limits, and formatting hygiene across Python, C++, CUDA, and Rust code. Use when creating or modifying source code in the repository."
audience: developer
status: stable
owners:
  - core-engineering
---

# Coding Style & Formatting Hygiene

Repository standards governing code syntax, SPDX licensing headers, line-length constraints, and automated formatting across all source files.

---

## When To Use

Use this skill when:
- Authoring new Python, C++, CUDA, or shell scripts in `source/`, `scripts/`, or `tools/`.
- Reviewing pull requests or checking automated pre-commit hooks.
- Fixing linting, formatting, or missing SPDX header failures.

---

## Workflow

1. **License Headers**:
   Every authored file must begin with the standard Apache-2.0 SPDX header:
   ```python
   # Copyright (c) 2026, GaussianSplat Contributors.
   # All rights reserved.
   #
   # SPDX-License-Identifier: Apache-2.0
   ```
2. **Formatting**:
   Run the project formatter before staging commits:
   - Windows: `format_code.bat`
   - Linux: `./format_code.sh`
3. **Python Standards**:
   - Ruff 120-column limit.
   - Google docstring format.
   - Explicit type annotations.

---

## Validation

- Run `python tools/check_licenses.py` to ensure 100% SPDX header coverage.
- Run `ruff check source/` to confirm zero lint errors.

---

## Maintenance

- Update `.github/LICENSE_HEADER.txt` when modifying project copyright terms.
- Keep `pyproject.toml` ruff and formatting rules synchronized.

---

## References

- [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
- [SPDX License Identifiers](https://spdx.org/licenses/)
