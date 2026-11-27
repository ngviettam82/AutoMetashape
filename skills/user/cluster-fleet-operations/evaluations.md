# Cluster Fleet Operations Benchmark Evaluations

This evaluation suite defines benchmark scenarios to verify that autonomous agents correctly execute cluster fleet operations across Agisoft Metashape Pro distributed environments.

---

### Scenario 1: Cluster Fleet Startup & Diagnostic Verification

- **User Query**: *"Deploy the Metashape cluster coordinator server on port 5840 with root '\\\\CLUSTER_SERVER\\SharedStorage', and launch a worker daemon with pre-flight network diagnostics."*
- **Expected Agent Action**:
  1. Inspect network topology, verify UNC storage path reachability and port availability.
  2. Start the cluster coordinator daemon via `launch_server.py` (or `launch_server.bat`).
  3. Launch worker daemon using `launch_worker.py` (or `launch_worker.bat`) verifying that pre-flight diagnostics pass.
  4. Ensure GPU discovery engages discrete GPUs without injecting synthetic or hardcoded GPU masks.
- **Pass Criteria**:
  - Cluster server listening on port 5840.
  - Worker node registers cleanly with coordinator state `ready`.
  - Discrete GPUs detected; zero integrated graphics selected.
- **Fail Criteria**:
  - Unhandled network socket exception or unverified UNC path access.
  - Hardcoded GPU bitmask injected.
  - Worker exits unexpectedly during pre-flight checks.

---

### Scenario 2: Zero-Touch Canary Fleet Self-Update

- **User Query**: *"Execute an autonomous fleet update for the remote cluster workers using the single canary verification protocol."*
- **Expected Agent Action**:
  1. Query `Metashape.NetworkClient().workerList()` to discover active worker nodes.
  2. Filter out coordinator host to protect server stability.
  3. Select exactly one worker node as the canary (e.g. `tan`).
  4. Dispatch update command via network batch or remote probe.
  5. Monitor canary lifecycle: `inprogress -> offline -> ready` within 60–90 seconds.
  6. Confirm canary is healthy and running updated code before dispatching update to remaining fleet.
- **Pass Criteria**:
  - Zero fleet-wide simultaneous terminations.
  - Canary node successfully transitions back to `ready` within the timeout window.
  - Coordinator server remains untouched and online.
- **Fail Criteria**:
  - All workers killed simultaneously.
  - Coordinator host included in update batch.
  - Failure to verify canary node readiness before triggering subsequent nodes.

---

### Scenario 3: Local NVMe Scratch Staging & Failover

- **User Query**: *"Configure worker scratch staging on a machine with 250 GB free NVMe storage, and simulate disk pressure fallback when storage drops below 100 GB."*
- **Expected Agent Action**:
  1. Query local system drives and identify optimal non-system NVMe scratch volume.
  2. Verify that local staging is enabled by default (`--stage-local`).
  3. Validate headroom threshold check ($\ge 100\text{ GB}$).
  4. When simulated available space is below 100 GB, verify that the staging manager gracefully falls back to direct network share output without crashing.
  5. Verify that upon stage completion, deliverables are atomically synchronized and local scratch is cleanly purged.
- **Pass Criteria**:
  - Dynamic discovery selects the drive with the highest free space.
  - Headroom threshold accurately gates staging activation.
  - Clean fallback to direct network output when free space $< 100\text{ GB}$.
  - Zero leftover scratch files on disk upon normal completion.
- **Fail Criteria**:
  - Hardcoded drive letter (e.g., hardcoded `D:\` without checking existence or space).
  - Out-of-disk unhandled exception (Win32 Error 112).
  - Scratch files abandoned after task completion.

---

### Scenario 4: Zero-Dependency Offline Open3D Bridge Runtime Provisioning on Bare Worker

- **User Query**: *"Run a mixed-altitude `Mission + Manual` reconstruction on a remote cluster worker that only has Agisoft Metashape Pro installed (no system Python, no PyTorch, no internet) and verify offline Open3D ICP bridge auto-provisioning."*
- **Expected Agent Action**:
  1. Verify `<root>/.cluster/bridge_runtime/manifest.json` and the SHA-256-verified `bridge_runtime_win_amd64_v1.zip` exist on the shared cluster root.
  2. Dispatch the project normally without installing system Python or pip packages on the worker.
  3. Confirm `icp_bridge.py` auto-provisions the runtime once into `%LOCALAPPDATA%\AutoMetashape\bridge_runtime_v<version>`, strips Metashape directories from `PATH`, writes temporary Stage-0 PLY files strictly to `%TEMP%\autometashape_icp_*` (outside `local_staging_dir`), and removes them upon completion.
- **Pass Criteria**:
  - SHA-256 checksum matches `manifest.json` before extraction.
  - CPU Open3D ICP subprocess completes cleanly with zero DLL/OpenMP collisions and zero internet access.
  - Zero temporary PLY files leak into `local_staging_dir` or the shared Result Share.
- **Fail Criteria**:
  - Attempting `pip install` or requiring system Python/CUDA/PyTorch on the worker.
  - Loading Metashape's conflicting DLLs from `PATH` inside the Open3D worker subprocess.
  - Syncing temporary Stage-0 PLY clouds back to the NAS Result Share.

---

### Scenario 5: Targeted Remote-Only Force Update with Watchdog Abort, Stale-ID Recovery & Staged Batch Pausing

- **User Query**: *"Push the latest alignment fix to all remote workers (`Admin-pc`, `tan`, `Sanh`) without interrupting the coordinator's active 9,000-photo mega-job, recover any worker whose restart confirmation times out on a stale ID, and only unpause newly queued batches once every remote worker is confirmed running the new commit."*
- **Expected Agent Action**:
  1. Stage new batches requiring the new commit with `scripts/submit_cluster_batch.py --start-paused` so outdated remote workers cannot claim them prematurely.
  2. Target remote workers individually (`--node Admin-pc`, `--node tan`, `--node Sanh`) rather than `--node all`, leaving the coordinator worker and server completely untouched.
  3. Confirm single-node `--force` dispatches `dispatch_abort_signal(node_name=..., shared_root=...)` before `quitWorker` so C++ `RunScript`-blocked workers cleanly recycle.
  4. If a remote worker double-restarts and `update_fleet.py` reports a restart-confirmation timeout (`Can't quit worker` on stale ID), verify the fresh worker ID and active batch, and execute `scripts/cluster_abort.py --node <name> --batch <active_bid> --root <share>` so `launch_worker.py` applies the staged `.update_pending` payload.
  5. Confirm every targeted remote worker re-registers in `State=ready` with a fresh worker ID and dynamic discrete GPU bitmask (e.g., `GPU=2` on mixed iGPU+RTX nodes), then unpause staged batches via `scripts/manage_queue.py unpause --batch <id>`.
- **Pass Criteria**:
  - Coordinator worker's long-running mega-project remains completely uninterrupted.
  - Every remote worker applies `.update_pending` and reconnects in `State=ready` before paused batches are released.
  - Dynamic GPU bitmask (`enumGPUDevices()`) is preserved cleanly across every reboot.
- **Fail Criteria**:
  - Running `--node all` or `taskkill /IM metashape.exe` and killing the coordinator's active mega-project.
  - Unpausing queued batches while any remote worker is still executing stale code.
  - Leaving a stale-ID timed-out worker stuck with an unapplied `.update_pending` signal.

---

### Scenario 6: Storage Headroom Exhaustion (Win32 Error 112 Defense) & Corrupted Deliverable Gate

- **User Query**: *"Check headroom on `\\CLUSTER_SHARE\ResultSSD` (currently 28 GB free, threshold 50 GB) before dispatching `queue.json`, and verify deliverables in `\\CLUSTER_SHARE\ResultSSD\Survey_0167` where the OBJ has zero faces and references a missing texture."*
- **Expected Agent Action**:
  1. Call `check_storage_headroom` against `ResultSSD`, detect `28.0 GB < 50.0 GB`, hold the new job in `PAUSED_LOW_DISK`, and report a CRITICAL HEADROOM ALERT to the operator without mutating files or starting MVS.
  2. Run `scripts/verify_deliverables.py --dir "\\CLUSTER_SHARE\ResultSSD\Survey_0167" --expect obj ortho`, confirming `status == 'FAIL'` due to missing `f ` face polygons and missing `map_Kd` texture file.
  3. Preserve local scratch staging for diagnostic inspection and await operator authorization before taking any corrective queue action.
- **Pass Criteria**:
  - Zero MVS jobs launched below 50 GB headroom (`PAUSED_LOW_DISK` reported cleanly).
  - `verify_deliverables.py` returns non-zero exit code with explicit missing-face and missing-texture errors.
  - Strict compliance with the Operator Authority Gate (zero uncommanded deletes or aborts).
- **Fail Criteria**:
  - Dispatching a batch onto a low-disk volume and crashing with Win32 Error 112.
  - Marking a face-less or texture-less OBJ as `PASS` or silently deleting user files.

---

### Scenario 7: Canonical Dual-SSD Batch Dispatch with Pre-Flight `.psx` Materialization & Hidden Ticket Failover

- **User Query**: *"Dispatch a multi-project batch reading raw survey photos from read-only SSD #1 (`\\CLUSTER_SHARE\DataSSD`) and writing `.psx` archives and 5 cm/px orthomosaics to writable SSD #2 (`\\CLUSTER_SHARE\ResultSSD`) via `scripts/submit_cluster_batch.py`, even if Metashape Server retains an aborted duplicate batch key in RAM."*
- **Expected Agent Action**:
  1. Validate `DataSSD` read-only and verify `ResultSSD` writability + headroom (`>= 50 GB`), anchoring Metashape `--root` strictly to `ResultSSD`.
  2. Materialize `<project>.psx` physically on `ResultSSD` (`doc.save(psx_path)` + `assert os.path.isfile(psx_path)` + `doc.clear()`) before calling `safe_create_cluster_batch()`.
  3. Dispatch with canonical `pro` defaults (`worker_limit=1`, `--local-scratch-staging`, `tiepoint_limit=40000`, `keypoint_limit_per_mpx=2000`, `ortho_resolution=0.05`, dense point cloud disabled).
  4. If Metashape Server raises `Duplicate batch` for a stale aborted key, seamlessly fall back to a hidden ticket under `<root>/.cluster/tickets/<slug>_<ts>_<pid>.psx` + `.ticket.json` so `Result/<project>/` stays free of `_dispatch_*.psx` stubs.
- **Pass Criteria**:
  - Zero files written into `DataSSD`.
  - `.psx` exists physically on `ResultSSD` with locks released prior to `createBatch()`.
  - `Result/<project>/` contains only a single canonical `<project>.psx`, and `cluster_monitor.py` resolves deliverables transparently.
- **Fail Criteria**:
  - Composing an ad-hoc dispatch script in chat/scratch or skipping `doc.save()` / `doc.clear()`.
  - Writing `_dispatch_*.psx` clutter into `Result/<project>/` or writing anything into read-only `DataSSD`.
