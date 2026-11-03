---
name: pr-workflow
description: "Enforces atomic commits, conventional commit syntax, DCO sign-offs, and presubmit validation pipelines. Use when preparing pull requests, creating commits, or verifying contribution readiness."
audience: developer
status: stable
owners:
  - "@autometashape-maintainers"
---

# Pull Request & Commit Workflow

This skill guides the preparation, validation, and submission of pull requests and commits in accordance with the repository's governance and DCO standards.

---

## When To Use

Use this skill when:
- Creating a feature branch and preparing commits.
- Formatting commit messages with Conventional Commit syntax.
- Ensuring DCO sign-offs (`git commit -s`) and absence of AI co-author trailers.
- Running required presubmit validation before pushing to origin.

Do not use this skill when:
- Performing raw code refactoring or designing local algorithm implementations.

---

## Workflow

### 1. Branch Isolation
Always work on a focused feature branch:
```bash
git checkout -b feature/<descriptive-name>
```

### 2. Presubmit Validation Gate
Execute project validation tools before staging changes:
```powershell
python tools/skills/cli.py check
python tools/check_licenses.py
./format_code.bat
```

### 3. Atomic Conventional Commits
- Use imperative, capitalized subjects with valid type prefix: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`.
- Do not append a trailing period.
- Add DCO sign-off (`-s`).
- **Strictly ban AI co-author or attribution trailers** (e.g. `Co-authored-by: AI...`).
```bash
git commit -s -m "feat: Implement automatic rolling shutter compensation"
```

---

## Validation

Verify PR readiness:
1. Inspect staged changes with `git diff --staged`.
2. Confirm branch is up to date with `main`.
3. Verify CI pipelines pass cleanly on GitHub.

---

## Maintenance

- Review pull request checklists periodically to incorporate new project validation gates.
- Ensure branch protection rules align with presubmit requirements.

---

## References

- [CONTRIBUTING.md](../../../CONTRIBUTING.md)
- [AGENTS.md](../../../AGENTS.md)
- [Conventional Commits Specification](https://www.conventionalcommits.org/)
