# Autonomous Photogrammetry Master Coordinator Evaluation Scenarios

Evaluation test scenarios for validating end-to-end multi-dataset auto-discovery, requirement parsing, cluster/standalone mode routing, and deliverable integrity reporting.

---

### Scenario 1: Multi-Dataset Folder Auto-Discovery & Sequential Execution
**Query:**
The operator points to `D:\Surveys` which contains 3 project subfolders: `Site_Alpha/` (800 photos), `Site_Beta/` (1,200 photos), and `Site_Gamma/` (950 photos). Run full reconstruction with the Pro preset and verify all deliverables.

**Expected behavior:**
1. The agent invokes the `autonomous-photogrammetry` master coordinator with `--scan-dir "D:\Surveys"`.
2. The coordinator scans `D:\Surveys`, excludes non-project folders, and discovers all 3 project subfolders.
3. Automatically synthesizes 3 discrete `QueueJob` instances without asking the user for a manual JSON manifest.
4. Pre-flight checks storage headroom (> 50 GB) before each job.
5. Processes each project sequentially using the Pro preset (alignment, USGS filtering, MVS, 8K texture, DEM, Ortho).
6. Executes `verify_project_deliverables` on each output directory upon stage completion.
7. Purges local NVMe scratch between jobs.
8. Produces a consolidated delivery summary table detailing all 3 projects.

**Pass/fail criteria:**
- All 3 projects are discovered and queued automatically.
- Deliverable verification reports status `PASS` for all 3 projects.
- Summary reports 3 total jobs, 3 completed, 0 failed.

---

### Scenario 2: Distributed Network Cluster Fleet Orchestration
**Query:**
Execute a batch of 2 datasets (`\\CLUSTER_SHARE\Flight_A`, `\\CLUSTER_SHARE\Flight_B`) across the distributed Metashape cluster server at `192.168.1.100:5840` with local NVMe scratch staging and independent worker locking.

**Expected behavior:**
1. Master coordinator delegates node and port reachability check to `cluster-fleet-operations`.
2. Invokes `cluster_supervisor.py` with `--network-enable --network-host 192.168.1.100 --network-independent-workers`.
3. Coordinates each project to run as a dedicated monolithic batch locked to 1 worker node (`worker_limit=1`).
4. Intermediate temporary octrees remain local to each worker's NVMe scratch drive.
5. On completion, synchronizes finalized `.psx` and assets to `\\CLUSTER_SHARE\Deliverables`.
6. Audits deliverable integrity and outputs final geomatics report.

**Pass/fail criteria:**
- Network cluster arguments are propagated cleanly to every queued job.
- Monolithic cluster batches are dispatched with `worker_limit=1`.
- All outputs on the central share pass deliverable integrity verification.

---

### Scenario 3: Storage Exhaustion Recovery & Deliverable Integrity Verification
**Query:**
Execute a multi-dataset queue where Project 1 succeeds, but Project 2's target volume drops below the 50 GB threshold (22 GB free).

**Expected behavior:**
1. Master coordinator runs Project 1.
2. Deliverable verification gate confirms Project 1 deliverables (`status == 'PASS'`).
3. Scratch directory for Project 1 is purged.
4. Before starting Project 2, the storage watchdog detects 22.0 GB free < 50.0 GB threshold.
5. The coordinator pauses Project 2 with `PAUSED_LOW_DISK` status and issues a high-priority storage hazard alert.
6. Does NOT crash with Win32 Error 112; leaves Project 1's deliverables completely uncorrupted.

**Pass/fail criteria:**
- Project 1 is verified `COMPLETED`.
- Project 2 is safely held in `PAUSED_LOW_DISK`.
- Error logs clearly state the storage exhaustion defense rationale.
