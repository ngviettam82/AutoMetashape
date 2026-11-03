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
- `developer/pr-workflow/`: Enforces atomic commits, conventional commit syntax, DCO sign-offs, and presubmit validation pipelines.
- `developer/test-audit/`: Audits unit and integration test suites against junk patterns, enforces mutation resistance, and guarantees authoring quality gates across pytest and CTest.

User skills:

- `user/image-to-3d-model/`: Reconstructs a production-grade textured 3D mesh (.obj, .mtl, .glb) and native Agisoft Metashape project (.psx) from an image folder or drone survey with automated quality assessment and GPU acceleration.
- `user/photogrammetry-export/`: Exports photogrammetric 3D reconstructions into production formats including GLB with embedded PBR textures, Wavefront OBJ with 8K TIFF textures, and native Metashape projects with metric georeferencing.
