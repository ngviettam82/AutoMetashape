# Cluster Supervisor Evaluation Scenarios

Evaluation test scenarios for validating unattended cluster supervision, storage headroom watchdogs, deliverable integrity gates, canonical batch submission, and multi-dataset queue chaining.

---

### Scenario 1: Storage Headroom Exhaustion & Win32 Error 112 Defense
**Query:**
The network share `\\CLUSTER_SHARE\storage` has dropped to 28 GB free space. Run the cluster supervisor on `queue.json` with a 50 GB headroom safety threshold.

**Expected behavior:**
1. The supervisor calls `check_storage_headroom` against the target output volume.
2. Identifies that 28.0 GB < 50.0 GB threshold.
3. Does NOT launch the resource-intensive MVS reconstruction.
4. Marks the job status as `PAUSED_LOW_DISK` and logs a storage exhaustion hazard alert to stderr.
5. Prevents cascading Win32 Error 112 crashes and preserves existing output assets intact.

**Pass/fail criteria:**
- Process exits cleanly without crashing.
- Job status is reported as `PAUSED_LOW_DISK`.
- Zero temporary scratch files or corrupt project writes are committed to the disk.

---

### Scenario 2: Corrupted Deliverable Detection & Quarantine Gate
**Query:**
Verify deliverables in `C:\Outputs\Survey_0167` where the OBJ file exists but has zero face definitions and references a missing texture `texture_0.png`.

**Expected behavior:**
1. The supervisor invokes `verify_project_deliverables` against `C:\Outputs\Survey_0167`.
2. Inspects `Survey_0167.obj` and detects absence of `f ` face polygon definitions.
3. Inspects linked `Survey_0167.mtl` and detects that `texture_0.png` does not exist on disk.
4. Returns status `FAIL` with explicit error items in the report.
5. Does not clear local scratch staging for this project, preserving intermediate files for diagnostic inspection.

**Pass/fail criteria:**
- `verify_project_deliverables` returns `status == 'FAIL'`.
- `errors` contains face polygon omission and missing texture file errors.
- CLI exit code is non-zero (1).

---

### Scenario 3: Multi-Dataset Autonomous Queue Chaining with Scratch Cleanup
**Query:**
Execute a batch queue containing 2 projects (`Survey_0167` and `Survey_0204`). Project 1 passes verification; Project 2 should immediately launch and clean Project 1's scratch.

**Expected behavior:**
1. Supervisor inspects headroom: 350 GB > 50 GB. Launches Project 1.
2. Project 1 finishes. Supervisor runs `verify_project_deliverables`: returns `PASS`.
3. Supervisor purges `{scratch_dir}\Survey_0167_local_stage`.
4. Supervisor advances immediately to Project 2 without waiting for user input.
5. Project 2 finishes and passes verification.
6. Summary reports 2 total jobs, 2 completed, 0 failed.

**Pass/fail criteria:**
- Both datasets transition from `PENDING` -> `RUNNING` -> `COMPLETED`.
- Scratch directory for Project 1 is verified empty before Project 2 starts.
- Summary dictionary returns `completed_jobs == 2` and `total_jobs == 2`.

---

### Scenario 4: Canonical Batch Dispatch with Pre-Flight Physical .psx Materialization Gate
**Query:**
Dispatch a new photogrammetry project to the cluster server using `scripts/submit_cluster_batch.py` from raw photo captures.

**Expected behavior:**
1. The submission utility scans the input image directory and confirms supported image files.
2. Dynamically resolves server host, port, and shared root without hardcoded addresses.
3. Initializes `Metashape.Document()`, creates chunk `Production_Reconstruction`, and adds cameras.
4. **Enforces Pre-Flight Physical Materialization**: Invokes `doc.save(psx_path)` and verifies `os.path.isfile(psx_path)` on the shared storage filesystem before dispatching.
5. Submits the batch via `client.createBatch()` and sets target priority and worker limits.
6. Guarantees that remote workers claiming the batch find an existing physical `.psx` archive, preventing missing-file I/O crashes.

**Pass/fail criteria:**
- Script exits with code 0.
- Physical `.psx` project archive exists on disk and is non-empty before batch creation (locks released via `doc.clear()`).
- Server returns a valid `batch_id > 0` via idempotent `safe_create_cluster_batch()`.

---

### Scenario 5: Dual Shared Folder (Multi-SSD) Dispatch & 5 cm/px Orthomosaic Gate
**Query:**
Dispatch a multi-project batch reading raw survey photos from read-only SSD #1 (`\\CLUSTER_SHARE\DataSSD`) and writing `.psx` archives and 5 cm/px orthomosaics to writable SSD #2 (`\\CLUSTER_SHARE\ResultSSD`) using the `pro` preset.

**Expected behavior:**
1. Validates `DataSSD` read-only and verifies `ResultSSD` writability + headroom (`>= 50 GB`), anchoring Metashape `--root` strictly to `ResultSSD`.
2. Discovers all project subfolders under `DataSSD` and maps mirrored output folders under `ResultSSD`.
3. Dispatches monolithic worker batches with `pro` defaults (`tiepoint_limit=40000`, `keypoint_limit_per_mpx=2000`, `build_point_cloud=False`, `ortho_resolution=0.05`).
4. Leaves `DataSSD` 100% unmodified and verifies `["obj", "ortho"]` deliverables on `ResultSSD` with exact `0.05 m/px` pixel scale.

**Pass/fail criteria:**
- `validate_dual_share_topology` returns `valid == True` with `result_network_root` rooted on `ResultSSD`.
- Zero files written into `DataSSD`.
- Exported orthomosaic `.tfw` reports `0.05 m/px` and dense point cloud count is `0`.
