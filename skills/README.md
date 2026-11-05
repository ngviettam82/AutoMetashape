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

### Developer Skills

- `developer/coding-style/`: Enforces repository coding conventions, SPDX license headers, line limits, and formatting hygiene across Python, C++, CUDA, and Rust code.
- `developer/pr-workflow/`: Enforces atomic commits, conventional commit syntax, DCO sign-offs, and presubmit validation pipelines.
- `developer/test-audit/`: Audits unit and integration test suites against junk patterns, enforces mutation resistance, and guarantees authoring quality gates across pytest and CTest.

### User Skills

- `user/image-to-3d-model/`: Reconstructs high-precision 3D textured models (`.obj`, `.mtl`, `.glb`), GeoTIFF orthomosaics (`.tif`, `.tfw`), and native Agisoft Metashape projects (`.psx`) from raw image folders or aerial drone surveys. Features two primary presets: Building (600K faces, 16-page 8K textures, GPS crop) and Map (2M faces, 4-page 8K textures, no crop, GeoTIFF orthomosaic). Uses robust single-chunk global bundle adjustment by default with optional multi-chunk alignment.
- `user/photogrammetry-export/`: Exports photogrammetric 3D reconstructions into production formats including GLB with embedded PBR textures, Wavefront OBJ with 16-page 8K TIFF textures, and native Metashape projects with metric georeferencing. Supports standalone texture re-baking without recomputing depth maps.
