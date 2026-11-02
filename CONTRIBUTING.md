# Contributing Guide

Welcome to GaussianSplat. All contributions, human and autonomous, must adhere to these standards.

---

## 1. Developer Certificate of Origin (DCO)

Every commit must include a DCO sign-off (`git commit -s`):
```text
Signed-off-by: Your Name <your.email@example.com>
```

---

## 2. Agent Development

Autonomous agents operating on this repository must observe:
- **Bounded Reading**: Read targeted functions and callers; avoid whole-tree context dumping.
- **Context Reuse**: Leverage loaded tools and diagnostics before issuing new probes.
- **Surgical Edits**: Formulate the smallest diff that satisfies the requirement; preserve unrelated workspace changes.
- **Validation Discipline**: Execute unit tests before and after modifications.
- **Zero Artifact Commits**: Never commit `.tmp`, `scratch/`, or transient agent logs.
- **No AI Attribution Lines**: Never include `Co-authored-by: AI...` trailers in commit messages.

---

## 3. Coding Style

- **Python**: Enforced via Ruff (`pyproject.toml`). Line length is 120 columns. Google docstrings required.
- **Licensing**: All new files must contain the Apache-2.0 SPDX header:
  ```python
  # Copyright (c) 2026, GaussianSplat Contributors.
  # All rights reserved.
  #
  # SPDX-License-Identifier: Apache-2.0
  ```
- **Logging**: Use standard logging; do not use raw `print()` for errors or warnings.

---

## 4. Unit Testing

- Unit tests reside co-located with subsystems in `source/extensions/*/test/`.
- Run pytest:
  ```bash
  python -m pytest source/extensions/gaussian.splat/test
  ```
- Tests must pass the 4-question gate defined in `skills/developer/test-audit/SKILL.md`.

---

## 5. Build & Validation Tools

- **Compile / Setup**: Run `build.bat` (Windows) or `./build.sh` (Linux).
- **Format Code**: Run `format_code.bat` (Windows) or `./format_code.sh` (Linux).
- **Skill Validation**: Run `python tools/skills/cli.py check`.
