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

- `developer/coding-style/`: Enforces repository coding conventions, SPDX license headers, line limits, and formatting hygiene across Python, Windows Batch, and shell scripts.
- `developer/pr-workflow/`: Enforces atomic commits, conventional commit syntax, DCO sign-offs, and presubmit validation pipelines.
- `developer/test-audit/`: Audits unit and integration test suites against junk patterns, enforces mutation resistance, and guarantees authoring quality gates across Python `unittest` suites.

### User Skills

- `user/cluster-fleet-operations/`: Unified Agisoft Metashape Pro distributed cluster orchestration and supervision runbook. Covers server/worker provisioning across single or dual-SSD shares, dynamic Metashape `enumGPUDevices()` GPU bitmask discovery, fixed-NVMe local scratch staging, portable offline `.cluster/bridge_runtime/` Open3D provisioning, canonical batch dispatch (`submit_cluster_batch.py`, `cluster_supervisor.py`, `--start-paused`, hidden `.cluster/tickets/` recovery), storage headroom watchdogs (Win32 Error 112 defense), read-only telemetry under the Operator Authority Gate (`cluster_monitor.py`, `manage_queue.py`), deliverable integrity verification, targeted GUI lock PID forensics, signal-based worker aborts, and canary/selective remote fleet updates.
- `user/image-to-3d-model/`: Reconstructs high-precision 3D textured models (`.obj`, `.mtl`, `.glb`), opt-in dense point clouds (`.laz`, `.las`), 3D tiles (Cesium, TLS), elevation models (DEM), topographic contours, metric 5 cm/px GeoTIFF orthomosaics, and native Agisoft Metashape projects (`.psx`) across single or multi-project directories. Features 4-stage folder-/time-agnostic zero-neural alignment hygiene (1D `TakeoffZ0` clustering, `pt.valid` Tarjan 1-edge hinge cutting, neighbor-continuity GPS-spike guard) & sub-island-isolated CPU Open3D Sim(3) ICP bridging, `k4=b1=b2=0` lens lock, and real-time Web Studio telemetry synchronization.
- `user/photogrammetry-export/`: Exports photogrammetric 3D reconstructions into production formats including GLB with embedded PBR textures, Wavefront OBJ with 8K TIFF textures, Autodesk FBX, Stanford PLY, point clouds, Cesium 3D Tiles, DEM, contours, metric 5 cm/px orthomosaics (`--ortho-resolution`), and PDF reports. Functions as the decoupled background export worker within the pipelined multi-project overlap architecture.
- `user/photogrammetry-forensic-audit/`: Performs deep read-only forensic quality and alignment audits on completed `.psx` archives, raw photo folders, and production 3D deliverables under the Operator Authority Gate. Uses deliverable header checks, single-pass `pt.valid` tie-graph topology, direct 3D tie-point voxel nearest-neighbor verification, 64 KB DJI XMP GPS parsing, `cam.meta["Image/Quality"]` inspection, and 25 m spatial grid coverage analysis to detect double-shells, detached ICP fragments, mixed multi-site folders (`> 5 km`), and false GPS-spike coverage loss.
