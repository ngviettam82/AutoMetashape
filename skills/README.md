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

- `user/cluster-fleet-operations/`: Orchestrates Agisoft Metashape Pro distributed processing clusters, worker node provisioning, dynamic multi-GPU discovery, local NVMe scratch staging, hourly health monitoring, and canary fleet updates.
- `user/image-to-3d-model/`: Reconstructs high-precision 3D textured models (`.obj`, `.mtl`, `.glb`), dense point clouds (`.laz`, `.las`), 3D tiles (Cesium, TLS), elevation models (DEM), topographic contours, GeoTIFF orthomosaics, and native Agisoft Metashape projects (`.psx`). Features distributed LAN cluster processing with dynamic work-stealing, pipelined compute-export overlap engine (decoupled CPU export while GPU workers process subsequent projects), and real-time Web Studio telemetry synchronization.
- `user/photogrammetry-export/`: Exports photogrammetric 3D reconstructions into production formats including GLB with embedded PBR textures, Wavefront OBJ with 8K TIFF textures, Autodesk FBX, Stanford PLY, point clouds, Cesium 3D Tiles, DEM, contours, and PDF reports. Functions as the decoupled background export worker within the pipelined multi-project overlap architecture.
