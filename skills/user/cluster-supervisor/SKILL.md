---
name: cluster-supervisor
description: "Supervises cluster fleet telemetry and deliverable verification gates in strict read-only reporting mode. Enforces the Operator Authority Gate: monitors storage headroom watchdogs (Win32 Error 112 defense), audits memory allocation faults (std::bad_alloc / OOM), and verifies deliverable integrity without uncommanded intervention. AI agents and supervisors report health and anomalies to the operator; all queue alterations, aborts, priority shifts, and recovery decisions lie strictly with the human operator. Use when monitoring cluster batch telemetry, diagnosing memory faults or stalled nodes, tracking storage headroom, or validating production 3D assets under the Operator Authority Gate."
audience: user
status: stable
owners:
  - "@autometashape-maintainers"
---

# Autonomous Cluster Supervisor & Deliverable Verification Gate

Comprehensive operational runbook for lights-out photogrammetry cluster telemetry reporting, storage headroom watchdogs (Win32 Error 112 defense), memory allocation fault auditing (`std::bad_alloc` / OOM), automated deliverable quality verification gates, and the Operator Authority Gate.

---

## When To Use

Use this skill when:
- Monitoring multi-hour, overnight, or multi-dataset photogrammetry cluster jobs in strict read-only reporting mode.
- Tracking storage headroom (`>= 50 GB`) and warning against catastrophic cluster stalls (`Win32 Error 112: ERROR_DISK_FULL`).
- Auditing memory allocation faults (`std::bad_alloc`, OOM, C++ allocation limits) across worker nodes.
- Verifying deliverable integrity (OBJ vertices/faces, linked MTL textures, GeoTIFF CRS projection tags, dense point cloud byte density, and PDF calibration reports) upon task completion.
- Formulating diagnostic reports and actionable recommendations for the human operator.

Do not use this skill to:
- Auto-abort batches, modify priorities, or alter cluster queues without explicit, real-time user permission (strictly prohibited by the Operator Authority Gate).
- Automatically degrade reconstruction settings or lower quality presets without operator instruction.
- Deploy or update worker nodes and server daemons (use `skills/user/cluster-fleet-operations/`).
- Executing an interactive, single-project photogrammetry reconstruction (use `skills/user/image-to-3d-model/`).
- Running standalone asset format conversions from an existing `.psx` archive (use `skills/user/photogrammetry-export/`).

---

## Workflow

```mermaid
flowchart TD
    subgraph Multi_Dataset_Queue["Multi-Dataset Queue (queue.json / --scan-dir)"]
        Job1["Project A (CATP_0167)"]
        Job2["Project B (CATP_0204)"]
        Job3["Project C (CATP_0205)"]
    end

    subgraph Headroom_Watchdog["Continuous Headroom Watchdog"]
        Sample["Inspect Central Share & NVMe Scratch"]
        Check{"Free Space >= 50 GB?"}
        Sample --> Check
        Check -->|No| PauseAlert["Pause Ingestion / Safe Quarantine\n(Defense against Win32 Error 112)"]
    end

    subgraph Execution_Modes["Cluster Execution Modes"]
        Check -->|Yes| RouteMode{"Mode Selection"}
        RouteMode -->|Mode 1: Sequential / Centralized| SeqDispatch["Sequential Stage Dispatch\nAll workers collaborate on 1 project"]
        RouteMode -->|Mode 2: Concurrent / Independent| ConcDispatch["Rapid Concurrent Batch Dispatch\nEach project locked to 1 dedicated worker\n(worker_limit=1)"]
    end

    subgraph Integrity_Gate["Deliverable Integrity Gate"]
        CheckOBJ["Validate OBJ Geometry & MTL Link"]
        CheckTIFF["Validate GeoTIFF Header & EPSG CRS"]
        CheckCloud["Validate LAZ/LAS/PLY Header & Size"]
        CheckPDF["Validate Official Quality PDF Report"]
    end

    Job1 --> Sample
    Job2 --> Sample
    Job3 --> Sample
    SeqDispatch --> RunSeq["Execute Stage-by-Stage"]
    ConcDispatch --> RunConc["Parallel Worker Fleet Execution"]
    RunSeq --> Integrity_Gate
    RunConc --> Integrity_Gate
    Integrity_Gate --> Eval{"All Assets PASS?"}
    Eval -->|PASS| Purge["Purge Local NVMe Scratch Directory"]
    Purge --> Advance["Advance / Complete Queue Item"]
    Eval -->|FAIL| Quarantine["Log Errors & Quarantine Deliverables"]
```

### 1. Storage Headroom Watchdog (Win32 Error 112 Defense)
Metashape MVS depth maps and octree leave partitions peak heavily during dense reconstruction. If the host volume runs out of free space, Metashape crashes with `Win32 Error 112`.

1. **Pre-Flight Disk Sampling**:
   Before launching any queued reconstruction, inspect disk space on both target network output and local NVMe scratch:
   ```powershell
   python -c "from autometashape.cluster.supervisor import check_storage_headroom; ok, free = check_storage_headroom('\\\\CLUSTER_SHARE\\storage', 50.0); print(f'Headroom OK: {ok} ({free} GB free)')"
   ```
2. **Watchdog Reporting Protocol**:
   - Minimum safe threshold: **50.0 GB** free space.
   - If free space $< 50.0\text{ GB}$, the supervisor logs a **CRITICAL HEADROOM ALERT** and reports it immediately to the operator.
   - In accordance with the **Operator Authority Gate**, the supervisor reports the low-disk state so the human operator can decide whether to clear scratch directories, pause batches, or expand network capacity. Automated routines do not silently delete or re-route user assets.

### 2. Automated Deliverable Quality & Integrity Gate
Never mark a reconstruction finished simply because `metashape.exe` exited code `0`. Run the deterministic deliverable verification gate:

```powershell
python scripts/verify_deliverables.py --dir "\\CLUSTER_SHARE\Khảo sát Địa hình Đà Nẵng" --expect obj dem ortho point_cloud report
```

The verification gate executes 5 structural integrity checks:
1. **Wavefront OBJ**:
   - File size $\ge 1,000$ bytes.
   - Verified vertex definitions (`v x y z`) and polygon faces (`f v1 v2 v3`).
   - `mtllib` link points to an existing `.mtl` material file.
   - All `map_Kd` texture images referenced inside `.mtl` exist, are readable, and $> 1,000$ bytes.
2. **Binary glTF (GLB)**:
   - File size $\ge 1,000$ bytes; binary magic matches `b'glTF'`.
3. **GeoTIFF (Elevation DEM & True Orthomosaic)**:
   - File size $\ge 1,000$ bytes.
   - Header matches valid TIFF/BigTIFF magics (`II*\0`, `MM\0*`, `II+\0`, `MM\0+`).
4. **Dense Point Cloud (LAZ, LAS, PLY)**:
   - File size $\ge 50,000$ bytes.
   - LAS/LAZ magic matches `b'LASF'`; PLY starts with `b'ply'`.
5. **Quality PDF Report**:
   - File size $\ge 15,000$ bytes; starts with PDF magic `b'%PDF'`.

Machine-readable JSON output can be requested using `--json`:
```json
{
  "status": "PASS",
  "deliverables_dir": "C:\\Outputs\\Project_A",
  "assets_checked": {
    "obj": { "status": "PASS", "size_bytes": 145023940 },
    "dem": { "status": "PASS", "size_bytes": 84201944 },
    "ortho": { "status": "PASS", "size_bytes": 312049102 },
    "point_cloud": { "status": "PASS", "size_bytes": 204918204 },
    "report": { "status": "PASS", "size_bytes": 142095 }
  },
  "errors": [],
  "warnings": []
}
```

### 3. Execution Modes: Centralized vs Independent Dispatch

The supervisor supports two distinct cluster operational architectures:

#### Mode 1: Centralized Task Mode (`--sequential` / `--no-concurrent`)
- **Behavior**: Datasets in the queue are processed one project at a time. For each project, the supervisor breaks the reconstruction down into granular network tasks (MatchPhotos, BuildDepthMaps) where **all available cluster workers collaborate concurrently on the same project**.
- **Best For**: Single massive surveys (e.g., 5,000+ images) requiring the aggregate compute and VRAM of the entire GPU fleet.
- **Invocation**:
  ```powershell
  python scripts/cluster_supervisor.py --manifest queue.json --network-enable --network-host 10.5.10.202 --sequential
  ```

#### Mode 2: Independent Project Mode (`--concurrent` / `--parallel-dispatch`)
- **Behavior**: All queued dataset projects are submitted to the Metashape Server scheduler simultaneously as atomic monolithic batches locked to 1 dedicated worker node (`worker_limit=1`). Each assigned worker stages project data into its local NVMe scratch volume and executes the complete pipeline independently with zero inter-worker network synchronization overhead.
- **Default Behavior**: When `--network-independent-workers` is enabled, concurrent dispatch is active by default.
- **Best For**: Daily drone batches consisting of multiple small-to-medium datasets (e.g., 500-1,500 photos each) where running $N$ projects simultaneously on $N$ GPUs yields maximum total throughput.
- **Invocation**:
  ```powershell
  python scripts/cluster_supervisor.py --scan-dir "\\ctuav\catp\101126" --output-root "\\ctuav\catp\101126" `
      --network-enable --network-host 10.5.10.202 --network-independent-workers --concurrent
  ```

### 4. Multi-Dataset Queue Chaining & Discovery
To process a sequence of survey datasets without human intervention:

1. **Auto-Discovery from Single or Dual Shared Folders (Multi-SSD)**:
   ```powershell
   # Single share:
   python scripts/cluster_supervisor.py --scan-dir "\\CLUSTER_SHARE\storage\101126" --output-root "\\CLUSTER_SHARE\storage\101126" `
       --preset pro --min-headroom-gb 50.0 --concurrent

   # Dual-SSD topology (SSD #1 read-only Data Source -> SSD #2 writable Result Share as Metashape --root):
   python scripts/cluster_supervisor.py `
       --source-share "\\CLUSTER_SHARE\DataSSD\101126" `
       --result-share "\\CLUSTER_SHARE\ResultSSD\101126" `
       --preset pro --ortho-resolution 0.05 --min-headroom-gb 50.0 `
       --network-enable --network-host 10.5.10.202 --network-independent-workers --concurrent
   ```

2. **Construct a Queue Manifest (`queue.json`)**:
   ```json
   [
     {
       "images_dir": "\\\\CLUSTER_SHARE\\storage\\Survey_0167",
       "output_dir": "\\\\CLUSTER_SHARE\\storage\\Deliverables_0167",
       "project_name": "Survey_0167",
       "preset": "pro",
       "expected_assets": ["obj", "dem", "ortho", "report"]
     },
     {
       "images_dir": "\\\\CLUSTER_SHARE\\storage\\Survey_0204",
       "output_dir": "\\\\CLUSTER_SHARE\\storage\\Deliverables_0204",
       "project_name": "Survey_0204",
       "preset": "pro",
       "expected_assets": ["obj", "dem", "ortho", "report"]
     }
   ]
   ```

3. **Supervision Lifecycle**:
   - **Pre-flight Headroom Watchdog**: Inspects free storage capacity across target output share before queue dispatch.
   - **Dispatch**: Submits batches to Metashape scheduler (sequentially for Mode 1 or concurrently with `worker_limit=1` for Mode 2).
   - **Telemetry Monitoring**: Non-blocking polling loop checks `batchInfo()` and worker sidecar `_autometashape_status.json` every 2.0s.
   - **Quality Gate**: Upon batch completion, runs structural verification on Wavefront OBJ, textures, GeoTIFFs, point clouds, and PDF reports.
   - **Scratch Purge**: Clears temporary local NVMe staging caches upon verified deliverable pass.


### 5. Signal-Based Worker Abort & Operator-Directed Recovery
During cluster execution, if an independent batch or worker freezes, deadlocks, or encounters an allocation fault (`std::bad_alloc`), recovery is triggered **strictly upon explicit operator authorization**. The supervisor reports the deadlock or fault state to the operator, who directs whether to abort and demote the batch.

1. **Deadlock Defense via Server Unpause**:
   When Metashape Server freezes due to a paused batch (`batch.paused == True`), the scheduler cannot re-evaluate worker queues. The abort orchestrator calls `client.setBatchPaused(batch_id, False)` before invoking `client.abortBatch(batch_id)`.
2. **Priority Demotion**:
   The batch priority is demoted to `-100` (`client.setBatchPriority(batch_id, -100)`) to prevent the scheduler from immediately re-dispatching the cancelled task when the worker reconnects.
3. **Signal Dispatch & Process Tree Kill**:
   Writes `<unc_root>/.cluster/aborts/<node_name>.abort`. The worker supervisor's non-blocking watchdog detects the signal, executes `taskkill /F /T /PID` to terminate both the parent daemon and any child processes, purges locks, and auto-reconnects to the cluster in 1.0s.
4. **Execution (Operator Authorized Only)**:
   ```powershell
   .\cluster_abort.bat --node DESKTOP-IFA3707 --batch 3 --server 192.168.1.100:5840 --root "\\SERVER\SharedMetashape"
   ```

### 6. Hardened Checkpoint Chunk Resolution & Recovery
When resuming a project checkpoint via `--resume`, older or GUI-modified projects often lack canonical `Production_Reconstruction` chunk labels, contain single default chunks (`Chunk 1`), or have empty chunk lists.

The unified chunk resolver (`resolve_checkpoint_chunk(doc)`) protects against `AttributeError: 'NoneType' object has no attribute 'cameras'` with deterministic fallback:
1. **Canonical Label Priority**: Checks if any chunk matches `Production_Reconstruction`.
2. **Highest Aligned Camera Fallback**: If canonical label is absent, counts aligned cameras across all chunks in a single $O(N)$ pass (`cam.transform and cam.enabled`) and selects the chunk with the highest count.
3. **Alignment Threshold**: If aligned cameras $\ge 2$, safely resumes downstream MVS depth maps and meshing, skipping camera alignment. If $< 2$, initializes a clean production chunk and restarts alignment from Step 1.

### 7. Canonical Batch Submission & Pre-Flight Physical Materialization
When submitting a standalone or queued batch to the cluster, agents and operators must invoke `scripts/submit_cluster_batch.py` rather than composing ad-hoc Python scripts in chat or scratch directories.

1. **Mandatory Physical Materialization Invariant**:
   - `scripts/submit_cluster_batch.py` verifies source image presence, materializes a valid `.psx` project stub on the Result Share, verifies `os.path.isfile(psx_path)`, releases locks via `doc.clear()`, and submits through idempotent `safe_create_cluster_batch()`:
     ```python
     doc.save(psx_path)
     assert os.path.isfile(psx_path), "Project file must exist on disk before dispatch"
     doc.clear()
     ```
   - This prevents worker failure modes where remote nodes claim a task whose project file has not yet been flushed or materialized to shared network storage, and prevents `.psx` lock contention.
2. **Dual-SSD & Direct Storage Standard**:
   - Pass `--source-share` (SSD #1, raw photos) and `--result-share` (SSD #2, writable `.psx` + deliverables) to utilize two separate SSD shares without mixing I/O.
   - For datasets physically hosted on the current machine's storage, pass `--no-local-scratch-staging` to eliminate redundant local NVMe staging and disk exhaustion.
3. **Execution**:
   ```powershell
   # Single-project dual-SSD submission (5 cm/px ortho, no dense point cloud in pro preset):
   python scripts/submit_cluster_batch.py `
       --images "\\CLUSTER_SHARE\DataSSD\Survey_0167" `
       --output "\\CLUSTER_SHARE\ResultSSD\Survey_0167" `
       --root "\\CLUSTER_SHARE\ResultSSD" `
       --preset pro --ortho-resolution 0.05 --priority 50
   ```

### 8. Cluster Telemetry & The Operator Authority Gate

**Core Invariant**: The supervisor and AI agent operate strictly as **Read-Only Telemetry Reporters**. All operational decisions (aborts, cancellations, priority changes, re-queuing, setting changes) **lie exclusively with the human operator**. AI agents are strictly prohibited from auto-deciding or mutating cluster state without real-time operator authorization.

1. **Strict Read-Only Telemetry Standard**:
   Execute non-intrusive fleet audits inspecting worker health, batch stage progress, memory allocation faults (`std::bad_alloc` / OOM), and storage headroom ($\ge 50\text{ GB}$):
   ```powershell
   python scripts/cluster_monitor.py
   ```
   - All observations, memory faults, and progress metrics are reported to the human operator in chat.
   - The supervisor does NOT auto-degrade settings, auto-retry, or auto-cancel failed tasks.

2. **Operator Authority Gate Workflow**:
   Under the **Operator Authority Gate**, any mutation to the cluster queue or worker nodes requires explicit, real-time user permission:
   - **Step 1: Observe & Report**: Collect telemetry and present findings and diagnoses clearly to the operator.
   - **Step 2: Propose Action**: Suggest recommended remedies (e.g., "Batch #58 encountered bad allocation. Recommend setting priority to 100 with `--no-guided-matching`").
   - **Step 3: Await Operator Decision**: Do nothing until the human operator confirms the plan.
   - **Step 4: Execute Canonical Utility**: Once confirmed, invoke the canonical scripts (`scripts/manage_queue.py`, `scripts/cluster_abort.py`, or `scripts/submit_cluster_batch.py`).

3. **Canonical Queue Management Operations**:
   Once explicitly approved by the operator, execute modifications via canonical utilities:
   ```powershell
   # List active and queued batches
   python scripts/manage_queue.py list

   # Pause / Unpause a batch
   python scripts/manage_queue.py pause --batch 12
   python scripts/manage_queue.py unpause --batch 12

   # Adjust priority safely
   python scripts/manage_queue.py set-priority --batch 29 --priority 100

   # Abort a batch upon operator direction
   python scripts/manage_queue.py abort --batch 58 --demote-priority -100
   ```

4. **Anti-Patterns & Prohibitions**:
   - **Strict Ban on Autonomous Agent Decisions**: Never abort tasks, adjust priorities, or re-submit batches without explicit operator permission.
   - **Strict Ban on Silent Settings Degradation**: Never auto-downgrade quality presets (e.g., auto-decimating textures, disabling guided matching silently). Maintain strict fidelity unless the operator directs a change.
   - **Strict Ban on Script-Side Node Affinity Rejection**: Never inject `if COMPUTERNAME != target_node: sys.exit(2)` into task execution scripts.
   - **Strict Ban on Ad-Hoc Chat Scripts**: Strictly invoke canonical utilities under `scripts/`.

### 9. Checkpoint Forensics & Diagnostics
To inspect an existing `.psx` archive non-destructively without risking accidental chunk wipeout:
```powershell
python scripts/inspect_checkpoint.py --project "\\CLUSTER_SHARE\storage\Survey_0167\Result\Survey_0167.psx"
```

---

## Validation

Verify that supervisor components, canonical scripts, and deliverable validators pass all quality checks:

```powershell
# 1. Run unit test suite
python -m unittest tests/test_verification.py tests/test_supervisor.py tests/test_checkpoint_loader.py tests/test_worker_abort.py tests/test_cluster_dispatch.py

# 2. Run skills validation
python tools/skills/cli.py check

# 3. Test standalone deliverable verification CLI
python scripts/verify_deliverables.py --help

# 4. Test canonical batch submission CLI
python scripts/submit_cluster_batch.py --help
```

---

## Maintenance

- **Threshold Tuning**: Adjust `min_headroom_gb` (default: 50 GB) based on network share capacity and multi-thousand photo project demands.
- **Format Extension**: When introducing new export formats (e.g., Cesium 3D Tiles), extend `verify_project_deliverables` in `source/autometashape/core/verification.py`.

---

## References

- Contributing guide: [CONTRIBUTING.md](../../../CONTRIBUTING.md)
- Deliverable Verification Engine: [source/autometashape/core/verification.py](../../../source/autometashape/core/verification.py)
- Cluster Supervisor Engine: [source/autometashape/cluster/supervisor.py](../../../source/autometashape/cluster/supervisor.py)
- Distributed Cluster Fleet Operations: [skills/user/cluster-fleet-operations/SKILL.md](../cluster-fleet-operations/SKILL.md)
- Supervisor Evaluation Suite: [evaluations.md](evaluations.md)
