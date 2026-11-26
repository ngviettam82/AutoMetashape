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

- `user/autonomous-photogrammetry/`: Master coordinator for end-to-end autonomous photogrammetry operations across single or dual-SSD shared folder topologies. Discovers project datasets, structures execution queues, coordinates cluster fleets or standalone GPUs, enforces storage headroom watchdogs, executes `pro` 3D reconstructions (50K keypoints, 40K tiepoints, zero-neural alignment hygiene & Open3D ICP bridging, `k4=b1=b2=0` lens lock, 5 cm/px orthomosaics, dense point cloud opt-in), verifies deliverable integrity, and reports executive engineering summaries.
- `user/cluster-fleet-operations/`: Orchestrates Agisoft Metashape Pro distributed processing clusters, single/dual-SSD worker node provisioning, dynamic multi-GPU discovery, local fixed-NVMe scratch staging, portable offline `.cluster/bridge_runtime/` Open3D provisioning, passive read-only telemetry monitoring (no artificial RAM guard), canary fleet updates, and signal-based worker abort & self-healing restarts.
- `user/cluster-supervisor/`: Supervises unattended cluster batch execution across single or dual shared folders, storage headroom exhaustion watchdog (Win32 Error 112 defense), Metashape 2.2.2 infinite-retry abort defense, canonical batch submission (`--exclude-folders`, alternate stub recovery) & worker queue controls (`pause-worker`, `unpause-worker`, `clear-worker`), automated deliverable integrity verification, and multi-dataset queue chaining.
- `user/image-to-3d-model/`: Reconstructs high-precision 3D textured models (`.obj`, `.mtl`, `.glb`), opt-in dense point clouds (`.laz`, `.las`), 3D tiles (Cesium, TLS), elevation models (DEM), topographic contours, metric 5 cm/px GeoTIFF orthomosaics, and native Agisoft Metashape projects (`.psx`). Features 4-stage zero-neural alignment hygiene & CPU Open3D Sim(3) ICP bridging, `k4=b1=b2=0` lens lock, distributed LAN cluster processing, and real-time Web Studio telemetry synchronization.
- `user/photogrammetry-export/`: Exports photogrammetric 3D reconstructions into production formats including GLB with embedded PBR textures, Wavefront OBJ with 8K TIFF textures, Autodesk FBX, Stanford PLY, point clouds, Cesium 3D Tiles, DEM, contours, metric 5 cm/px orthomosaics (`--ortho-resolution`), and PDF reports. Functions as the decoupled background export worker within the pipelined multi-project overlap architecture.
