#!/usr/bin/env python3

# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""
Agent Skills Toolchain CLI (AutoMetashape & Isaac Lab Architecture)

Supports:
- check:  Comprehensive validation of frontmatter, 5 mandatory headings, link integrity,
          line limits, evaluations, and cross-platform discovery symlinks.
- list:   Pretty-print catalog of all registered agent skills.
- create: Scaffold a new skill package with SKILL.md and evaluations.md.
- sync:   Synchronize and repair .agents/skills/ discovery symlinks / text shims.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Constants & Invariants
ALLOWED_CATEGORIES = {"developer", "custodian", "domain", "user"}
ALLOWED_AUDIENCES = {"developer", "user"}
ALLOWED_STATUSES = {"experimental", "stable", "deprecated"}
GENERIC_TERMS = {"helper", "utils", "tools", "util", "tool", "helpers"}
VENDOR_TAG_PATTERN = re.compile(r"\b(claude|cursor|anthropic|openai|gemini|chatgpt)\b", re.IGNORECASE)
NAME_REGEX = re.compile(r"^[a-z0-9-]{1,64}$")

MANDATORY_SECTIONS = [
    "## When To Use",
    "## Workflow",
    "## Validation",
    "## Maintenance",
    "## References",
]

MAX_LINE_COUNT = 500
MAX_DESCRIPTION_LEN = 1024


def parse_simple_yaml_frontmatter(content: str) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
    """Extracts and parses simple YAML frontmatter without external dependencies."""
    if not content.startswith("---"):
        return None, content, "File does not start with '---' frontmatter boundary"

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, content, "Unterminated frontmatter; missing closing '---'"

    yaml_block = parts[1].strip()
    body = parts[2]

    data: Dict[str, Any] = {}
    current_key: Optional[str] = None
    is_list = False

    for raw_line in yaml_block.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("- ") and current_key:
            item_val = stripped[2:].strip().strip("\"'")
            if is_list:
                data[current_key].append(item_val)
            else:
                data[current_key] = [item_val]
                is_list = True
            continue

        if ":" in stripped:
            key, val = stripped.split(":", 1)
            key = key.strip()
            val = val.strip()

            if val == "":
                current_key = key
                data[current_key] = []
                is_list = True
            else:
                current_key = key
                is_list = False
                val = val.strip("\"'")
                data[current_key] = val
        else:
            if current_key and not is_list and isinstance(data.get(current_key), str):
                data[current_key] = f"{data[current_key]} {stripped}".strip()

    return data, body, None


class SkillValidator:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root
        self.skills_dir = repo_root / "skills"
        self.agents_skills_dir = repo_root / ".agents" / "skills"
        self.skills_json_path = repo_root / ".agents" / "skills.json"
        self.readme_path = self.skills_dir / "README.md"

    def list_skills(self) -> List[Dict[str, Any]]:
        """Scans and lists all valid skills."""
        skills: List[Dict[str, Any]] = []
        if not self.skills_dir.is_dir():
            return skills

        for category_dir in sorted(self.skills_dir.iterdir()):
            if not category_dir.is_dir() or category_dir.name not in ALLOWED_CATEGORIES:
                continue

            for skill_dir in sorted(category_dir.iterdir()):
                skill_file = skill_dir / "SKILL.md"
                if not skill_file.is_file():
                    continue

                content = skill_file.read_text(encoding="utf-8")
                metadata, _, err = parse_simple_yaml_frontmatter(content)
                if metadata:
                    skills.append({
                        "slug": skill_dir.name,
                        "category": category_dir.name,
                        "name": metadata.get("name", skill_dir.name),
                        "audience": metadata.get("audience", "unknown"),
                        "status": metadata.get("status", "unknown"),
                        "description": str(metadata.get("description", "")),
                        "path": skill_file,
                    })
        return skills

    def validate_all(self) -> Tuple[bool, List[str]]:
        """Runs full validation suite against all skills."""
        errors: List[str] = []

        if not self.skills_dir.is_dir():
            return False, ["'skills/' directory does not exist at repo root"]

        if not self.readme_path.is_file():
            errors.append("skills/README.md catalog index is missing")
        readme_content = self.readme_path.read_text(encoding="utf-8") if self.readme_path.is_file() else ""

        skills = self.list_skills()
        if not skills:
            errors.append("Zero skills discovered in 'skills/' catalog")

        discovered_slugs: Set[str] = set()

        for s in skills:
            slug = s["slug"]
            category = s["category"]
            skill_file: Path = s["path"]

            discovered_slugs.add(slug)
            skill_errors = self._validate_single_skill(skill_file, category, slug, readme_content)
            for err in skill_errors:
                errors.append(f"[{category}/{slug}] {err}")

            # Validate Discovery symlink in .agents/skills/
            sym_err = self._validate_symlink(category, slug)
            if sym_err:
                errors.append(f"[{category}/{slug}] {sym_err}")

        # Check for orphaned aliases in .agents/skills/
        if self.agents_skills_dir.is_dir():
            for item in self.agents_skills_dir.iterdir():
                if item.name not in discovered_slugs and not item.name.startswith("."):
                    errors.append(f"[orphaned alias] .agents/skills/{item.name} has no matching skill in skills/")

        # Validate .agents/skills.json dual-channel discovery file
        if not self.skills_json_path.is_file():
            errors.append("Missing '.agents/skills.json' dual-channel discovery file")
        else:
            try:
                data = json.loads(self.skills_json_path.read_text(encoding="utf-8"))
                entries = data.get("entries", [])
                if not isinstance(entries, list) or len(entries) == 0:
                    errors.append("'.agents/skills.json' must contain a non-empty 'entries' list")
                else:
                    paths = [e.get("path") for e in entries if isinstance(e, dict)]
                    if not any(p in paths for p in ["skills/developer", "skills/domain", "skills/user"]):
                        errors.append("'.agents/skills.json' missing core category paths ('skills/developer', 'skills/domain', 'skills/user')")
            except Exception as e:
                errors.append(f"Invalid JSON in '.agents/skills.json': {e}")

        return len(errors) == 0, errors

    def _validate_single_skill(
        self,
        skill_file: Path,
        category: str,
        slug: str,
        readme_content: str,
    ) -> List[str]:
        errs: List[str] = []
        raw_text = skill_file.read_text(encoding="utf-8")

        # 1. Total line limit check
        lines = raw_text.splitlines()
        if len(lines) > MAX_LINE_COUNT:
            errs.append(f"Exceeds max line limit ({len(lines)} > {MAX_LINE_COUNT})")

        # 2. Check for Windows backslashes in non-code, non-path text
        raw_without_escapes = raw_text.replace(r"\b", "").replace(r"\le", "").replace(r"\ge", "").replace(r"\cdot", "").replace(r"\mathbf", "").replace(r"\hat", "").replace(r"\mathbb", "").replace(r"\in", "").replace(r"\times", "").replace(r"\text", "").replace(r"\partial", "").replace(r"\arg", "").replace(r"\min", "").replace(r"\max", "").replace(r"\middle", "").replace(r"\quad", "").replace(r"\forall", "").replace(r"\psi", "").replace(r"\eta", "").replace(r"\tau", "").replace(r"\alpha", "").replace(r"\beta", "").replace(r"\Delta", "").replace(r"\left", "").replace(r"\right", "").replace(r"\mathcal", "").replace(r"\bigcap", "").replace(r"\;", "").replace(r"\;", "")
        backslash_match = re.search(r"[A-Za-z0-9_.-]+\\[A-Za-z0-9_.-]+", raw_without_escapes)
        if backslash_match:
            errs.append(f"Contains Windows backslash in path '{backslash_match.group(0)}'; must use forward slashes")

        # 3. YAML Frontmatter Parsing
        meta, body, parse_err = parse_simple_yaml_frontmatter(raw_text)
        if parse_err or not meta:
            errs.append(f"Frontmatter parsing error: {parse_err}")
            return errs

        # 4. Frontmatter Field Validation
        name = meta.get("name")
        if not name or not isinstance(name, str):
            errs.append("Missing required 'name' field in frontmatter")
        else:
            if not NAME_REGEX.match(name):
                errs.append(f"'name' ('{name}') must match ^[a-z0-9-]{{1,64}}$")
            if any(term in name.split("-") for term in GENERIC_TERMS):
                errs.append(f"'name' ('{name}') contains prohibited generic term")
            if VENDOR_TAG_PATTERN.search(name):
                errs.append(f"'name' ('{name}') contains prohibited vendor tag")

        desc = meta.get("description")
        if not desc or not isinstance(desc, str):
            errs.append("Missing required 'description' field in frontmatter")
        else:
            if len(desc) > MAX_DESCRIPTION_LEN:
                errs.append(f"'description' length ({len(desc)}) exceeds limit ({MAX_DESCRIPTION_LEN})")
            if "<" in desc or ">" in desc:
                errs.append("'description' contains prohibited HTML/XML tags")
            if "Use when" not in desc:
                errs.append("'description' must contain mandatory trigger phrase 'Use when'")

        audience = meta.get("audience")
        if not audience or audience not in ALLOWED_AUDIENCES:
            errs.append(f"'audience' ('{audience}') must be one of {sorted(ALLOWED_AUDIENCES)}")

        status = meta.get("status")
        if not status or status not in ALLOWED_STATUSES:
            errs.append(f"'status' ('{status}') must be one of {sorted(ALLOWED_STATUSES)}")

        owners = meta.get("owners")
        if not owners or not isinstance(owners, list) or len(owners) == 0:
            errs.append("'owners' must be a non-empty list of maintainer identifiers")

        # 5. Section Heading Integrity (Exact 5 H2 headers)
        found_sections: List[str] = []
        for line in body.splitlines():
            line_str = line.strip()
            if line_str.startswith("## "):
                found_sections.append(line_str)

        for mandatory in MANDATORY_SECTIONS:
            if mandatory not in found_sections:
                errs.append(f"Missing mandatory section header '{mandatory}'")

        # 6. Relative Markdown Link Verification
        markdown_links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", body)
        for _, link in markdown_links:
            clean_link = link.strip()
            if clean_link.startswith(("http://", "https://", "mailto:", "#")):
                continue
            clean_path = clean_link.split("#")[0].strip()
            if not clean_path:
                continue

            target_path = (skill_file.parent / clean_path).resolve()
            if not target_path.exists():
                errs.append(f"Broken relative markdown link to '{clean_path}'")

        # 7. Backticked Repo Paths Verification
        backtick_matches = re.findall(r"`((?:source|skills|tools|docs|scripts|tests|\.agents)/[a-zA-Z0-9_./-]+)`", body)
        for b_path in backtick_matches:
            target_f = (self.repo_root / b_path).resolve()
            if not target_f.exists():
                errs.append(f"Referenced path '{b_path}' does not exist on disk")

        # 8. Check registration in skills/README.md
        if readme_content:
            if slug not in readme_content:
                errs.append(f"Skill '{slug}' is not registered in 'skills/README.md'")

        # 9. Evaluations check for user & domain skills or skills with evaluations.md
        eval_file = skill_file.parent / "evaluations.md"
        requires_eval = (audience == "user") or (category in {"domain", "user"}) or eval_file.exists()
        if requires_eval:
            if not eval_file.is_file():
                errs.append(f"Skill requires companion 'evaluations.md' at '{eval_file.name}'")
            else:
                eval_content = eval_file.read_text(encoding="utf-8")
                scenarios = re.findall(r"(?m)^#{2,3}\s+Scenario\b", eval_content)
                if len(scenarios) < 3:
                    errs.append(f"'evaluations.md' must contain >= 3 scenarios (found {len(scenarios)})")
                if "pass" not in eval_content.lower() or "fail" not in eval_content.lower():
                    errs.append("'evaluations.md' must define explicit Pass/Fail criteria")

        return errs

    def _validate_symlink(self, category: str, slug: str) -> Optional[str]:
        """Validates discovery alias under .agents/skills/<slug>."""
        if not self.agents_skills_dir.is_dir():
            return f"Directory '{self.agents_skills_dir.relative_to(self.repo_root)}' does not exist"

        link_target_path = self.agents_skills_dir / slug
        if not link_target_path.exists() and not link_target_path.is_symlink():
            return f"Missing discovery alias at '.agents/skills/{slug}'"

        expected_rel = f"../../skills/{category}/{slug}"
        # Allow domain skills to satisfy legacy user category aliases
        alt_expected_rel = f"../../skills/domain/{slug}" if category == "user" else f"../../skills/user/{slug}"

        # True symlink
        if link_target_path.is_symlink():
            try:
                target = os.readlink(str(link_target_path)).replace("\\", "/")
                resolved = (link_target_path.parent / target).resolve()
                expected_resolved = (self.skills_dir / category / slug).resolve()
                alt_resolved = (self.skills_dir / ("domain" if category == "user" else "user") / slug).resolve()
                if resolved != expected_resolved and resolved != alt_resolved:
                    return f"Symlink points to '{target}', expected target '{expected_rel}'"
            except Exception as e:
                return f"Error reading symlink: {e}"
        # Plain text file shim (Git on Windows when symlinks disabled)
        elif link_target_path.is_file():
            text = link_target_path.read_text(encoding="utf-8").strip().replace("\\", "/")
            if text != expected_rel and text != alt_expected_rel and not text.endswith(f"skills/{category}/{slug}") and not text.endswith(f"skills/domain/{slug}"):
                return f"Symlink text shim contains '{text}', expected '{expected_rel}'"
        else:
            return f"Alias at '.agents/skills/{slug}' is neither a symlink nor a 1-line text shim"

        return None

    def sync_symlinks(self) -> Tuple[int, int, List[str]]:
        """Synchronizes and repairs all discovery symlinks in .agents/skills/."""
        created = 0
        pruned = 0
        logs: List[str] = []

        self.agents_skills_dir.mkdir(parents=True, exist_ok=True)
        skills = self.list_skills()
        active_slugs: Set[str] = set()

        # Prioritize canonical domain/developer/custodian over legacy user
        category_priority = {"domain": 0, "developer": 1, "custodian": 2, "user": 3}
        sorted_skills = sorted(skills, key=lambda x: (category_priority.get(x["category"], 99), x["slug"]))

        for s in sorted_skills:
            slug = s["slug"]
            category = s["category"]
            if slug in active_slugs:
                continue
            active_slugs.add(slug)

            link_path = self.agents_skills_dir / slug
            expected_rel = f"../../skills/{category}/{slug}"

            # Create or update if missing or incorrect
            needs_update = False
            if not link_path.exists() and not link_path.is_symlink():
                needs_update = True
            elif link_path.is_file() and not link_path.is_symlink():
                content = link_path.read_text(encoding="utf-8").strip().replace("\\", "/")
                if content != expected_rel and not content.endswith(f"skills/{category}/{slug}"):
                    needs_update = True

            if needs_update:
                try:
                    # Attempt true relative symlink first
                    if link_path.is_symlink() or link_path.is_file():
                        link_path.unlink()
                    os.symlink(expected_rel, str(link_path))
                    created += 1
                    logs.append(f"Created symlink: .agents/skills/{slug} -> {expected_rel}")
                except (OSError, NotImplementedError):
                    # Fallback to cross-platform Git 1-line text shim
                    link_path.write_text(f"{expected_rel}\n", encoding="utf-8")
                    created += 1
                    logs.append(f"Created text shim: .agents/skills/{slug} -> {expected_rel}")

        # Prune dead aliases
        for item in self.agents_skills_dir.iterdir():
            if item.name not in active_slugs and not item.name.startswith("."):
                if item.is_symlink() or item.is_file():
                    item.unlink()
                    pruned += 1
                    logs.append(f"Pruned orphaned alias: .agents/skills/{item.name}")

        # Synchronize .agents/skills.json dual-channel discovery file
        categories = [p.name for p in sorted(self.skills_dir.iterdir()) if p.is_dir() and p.name in ALLOWED_CATEGORIES]
        entries = [{"path": f"skills/{c}"} for c in categories]
        if {"path": "skills"} not in entries:
            entries.append({"path": "skills"})
        skills_json_data = {"entries": entries}
        self.skills_json_path.parent.mkdir(parents=True, exist_ok=True)
        self.skills_json_path.write_text(json.dumps(skills_json_data, indent=2) + "\n", encoding="utf-8")
        logs.append(f"Synchronized .agents/skills.json with {len(entries)} discovery entries")

        return created, pruned, logs

    def create_skill(self, category: str, slug: str, audience: Optional[str] = None) -> Path:
        """Scaffolds a new skill directory with templates."""
        if category not in ALLOWED_CATEGORIES:
            raise ValueError(f"Category '{category}' must be one of {sorted(ALLOWED_CATEGORIES)}")
        if not NAME_REGEX.match(slug):
            raise ValueError(f"Slug '{slug}' must match ^[a-z0-9-]{{1,64}}$")

        target_dir = self.skills_dir / category / slug
        if target_dir.exists():
            raise FileExistsError(f"Skill already exists at '{target_dir}'")

        target_dir.mkdir(parents=True, exist_ok=True)
        resolved_audience = audience if audience else ("user" if category in {"domain", "user"} else "developer")

        # SKILL.md template
        skill_content = f"""---
name: {slug}
description: Concise operational description of {slug}. Use when performing {slug} tasks across autonomous operations.
audience: {resolved_audience}
status: experimental
owners:
  - "@autometashape-maintainers"
---

# {slug.replace('-', ' ').title()}

Operational guidance and technical specification for {slug}.

---

## When To Use

Use when:
- Executing initial {slug} operational procedures.
- Auditing or reviewing {slug} contracts.

---

## Workflow

1. **Step 1: Preparation**:
   - Define input parameters and prerequisites.

2. **Step 2: Execution**:
   - Execute the standard procedure.

---

## Validation

Verify that all procedures execute cleanly:

```bash
python tools/skills/cli.py check
```

---

## Maintenance

- Review and update operational procedures quarterly.

---

## References

- Contributing guide: `docs/contributing.md`
- Benchmark evaluation suite: [evaluations.md](evaluations.md)
"""
        (target_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")

        # evaluations.md template
        eval_content = f"""# {slug.replace('-', ' ').title()} Benchmark Evaluations

This evaluation suite defines benchmark scenarios to verify that autonomous agents correctly execute {slug}.

---

### Scenario 1: Standard Execution

- **User Query**: *"Run standard procedure for {slug}."*
- **Expected Agent Action**:
  1. Inspect configuration.
  2. Execute workflow step 1 and step 2.
- **Pass Criteria**: Clean completion with 0 errors.
- **Fail Criteria**: Unhandled exception or incomplete steps.

---

### Scenario 2: Error Recovery

- **User Query**: *"Execute {slug} with abnormal parameter inputs."*
- **Expected Agent Action**:
  1. Validate parameters and reject invalid values.
  2. Recover cleanly.
- **Pass Criteria**: Clean rejection with descriptive error message.
- **Fail Criteria**: System crash or silent failure.

---

### Scenario 3: Regression Verification

- **User Query**: *"Verify that {slug} causes zero regressions."*
- **Expected Agent Action**:
  1. Run validation commands.
- **Pass Criteria**: All verification tests pass.
- **Fail Criteria**: Any test failure.
"""
        (target_dir / "evaluations.md").write_text(eval_content, encoding="utf-8")

        # Automatically sync alias
        self.sync_symlinks()
        return target_dir


def main():
    parser = argparse.ArgumentParser(description="Autonomous Systems Agent Skills CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # check
    subparsers.add_parser("check", help="Validate all repository agent skills against governance rules")

    # list
    subparsers.add_parser("list", help="List all registered agent skills")

    # sync
    subparsers.add_parser("sync", help="Synchronize and repair .agents/skills/ discovery symlinks and shims")

    # create
    create_parser = subparsers.add_parser("create", help="Scaffold a new skill directory with templates")
    create_parser.add_argument("category", choices=sorted(ALLOWED_CATEGORIES), help="Skill category")
    create_parser.add_argument("slug", help="Unique skill identifier slug")
    create_parser.add_argument("--audience", choices=sorted(ALLOWED_AUDIENCES), help="Target audience override")

    args = parser.parse_args()

    script_path = Path(__file__).resolve()
    repo_root = script_path.parent.parent.parent
    validator = SkillValidator(repo_root)

    if args.command == "list":
        skills = validator.list_skills()
        if not skills:
            print("No registered skills found.")
            sys.exit(0)
        print(f"{'SLUG':<24} {'CATEGORY':<12} {'AUDIENCE':<12} {'STATUS':<12} {'DESCRIPTION'}")
        print("=" * 105)
        for s in skills:
            desc = s['description'][:42] + "..." if len(s['description']) > 42 else s['description']
            print(f"{s['slug']:<24} {s['category']:<12} {s['audience']:<12} {s['status']:<12} {desc}")
        sys.exit(0)

    elif args.command == "sync":
        print("Synchronizing .agents/skills/ discovery aliases...")
        created, pruned, logs = validator.sync_symlinks()
        for log in logs:
            print(f"  [*] {log}")
        print(f"[OK] Synchronization complete ({created} created/updated, {pruned} pruned).")
        sys.exit(0)

    elif args.command == "create":
        try:
            skill_dir = validator.create_skill(args.category, args.slug, args.audience)
            print(f"[OK] Successfully scaffolded new skill package at: {skill_dir}")
            sys.exit(0)
        except Exception as e:
            print(f"[ERROR] Failed to create skill: {e}")
            sys.exit(1)

    elif args.command == "check":
        print("Validating repository agent skills architecture...")
        passed, errors = validator.validate_all()
        if passed:
            print("[OK] All agent skills passed validation successfully.")
            sys.exit(0)
        else:
            print(f"[FAIL] Agent skills validation failed with {len(errors)} error(s):")
            for e in errors:
                print(f"  - {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
