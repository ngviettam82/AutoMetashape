# Agent Skills Catalog

This directory contains repository-owned agent skills following the **NVIDIA Isaac Lab** architecture and multi-asset skill standards.

---

## Authoring Standards
All skills must conform to:
1. Valid YAML frontmatter: `name`, `description` (with "Use when" clause), `audience` (`developer` | `user`), `status` (`stable` | `experimental` | `deprecated`), and non-empty `owners`.
2. 5 mandatory Markdown headings: `## When To Use`, `## Workflow`, `## Validation`, `## Maintenance`, `## References`.
3. User skills must include an `evaluations.md` containing at least 3 concrete test scenarios.
4. Validation is strictly enforced by running:
   ```bash
   python tools/skills/cli.py check
   ```

---

## Catalog

Developer skills:

- `developer/coding-style/`: Enforces repository coding conventions, SPDX license headers, line limits, and formatting hygiene across Python, C++, CUDA, and Rust code.
- `developer/test-audit/`: Audits unit and integration test suites against junk patterns, enforces mutation resistance, and guarantees authoring quality gates across pytest and CTest.

User skills:

- `user/image-to-3d-model/`: Reconstructs a production-ready textured 3D model (.obj, .mtl, .glb) and 3D Gaussian Splatting radiance field directly from an image set or drone aerial survey, automating telemetry extraction, GPS spatial pair matching, bundle adjustment, and surface-aligned mesh baking.
