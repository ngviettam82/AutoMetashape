# Global Agent Guidelines

## Contribution Guide & Project Governance

Always read the workspace root `AGENTS.md` and relevant sections of the project's contribution guide (`CONTRIBUTING.md`):

- [Agent Development](CONTRIBUTING.md#agent-development) for bounded search, context reuse, worktree isolation, and validation discipline.
- [Coding Style](CONTRIBUTING.md#coding-style) for repository-specific standards, formatting, and import hygiene.
- [Unit Testing](CONTRIBUTING.md#unit-testing) for test quality, authoring gates, and mutation verification; consult repository test skills (`skills/developer/test-audit/SKILL.md`).
- [Build & Validation Tools](CONTRIBUTING.md#build--validation-tools) for project launchers (`build.bat`/`build.sh`, `format_code.bat`/`format_code.sh`, pre-commit hooks).

The project contribution guide owns shared rules. Follow them instead of assuming global defaults.

## Agent Workflow

- Follow a more-specific `AGENTS.md` in the directory or subsystem being changed if one exists.
- Preserve unrelated workspace changes and **never commit generated plans, scratch files, or agent artifacts**.
- Identify the behavior's owner and make the smallest surgical change that resolves the defect.
- Run the narrowest relevant checks during editing, then the required project validation suite (`build.bat` / `./build.sh` or pre-commit checks).
- Run project code formatting (`format_code.bat` or `./format_code.sh`) before submitting changes.
- Research before concluding: ground root-cause diagnosis in verified log traces, official documentation, and empirical reproductions rather than superficial guesses.
- Keep exploratory research, intermediate drafts, and multi-agent notes strictly isolated in `.agents/` (untracked).

## Commits and Branches

- Work on a feature branch; do not commit directly to the default branch (`main` / `master`).
- Keep commits focused and atomic.
- Use imperative, capitalized Conventional Commit subjects (e.g. `feat: Implement ...`, `fix: Correct ...`) with no trailing period.
- Add DCO sign-off (`git commit -s`) using real name and email if required by the repository.
- Inspect staged changes before committing (`git diff --staged`).
- **Strictly ban AI co-author or attribution lines** (e.g. `Co-authored-by: AI...`, `Generated-by:...`).
- Prefer follow-up commits over amending commits while addressing review feedback.

## Repository Skills

- Keep repository-owned skills in canonical `skills/`; do not duplicate their contents in tool-specific discovery directories.
- Validate skill changes using the project's skill validator (`python tools/skills/cli.py check`).
- Universal discovery symlinks or aliases reside in `.agents/skills/<name>/` pointing back to canonical skill directories under `skills/`.
- Keep skills concise and point to maintained documentation and source examples.
