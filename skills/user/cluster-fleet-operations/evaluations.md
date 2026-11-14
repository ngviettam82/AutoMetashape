# Cluster Fleet Operations Benchmark Evaluations

This evaluation suite defines benchmark scenarios to verify that autonomous agents correctly execute cluster fleet operations across Agisoft Metashape Pro distributed environments.

---

### Scenario 1: Cluster Fleet Startup & Diagnostic Verification

- **User Query**: *"Deploy the Metashape cluster coordinator server on port 5840 with root '\\\\ctuav\\catp', and launch a worker daemon with pre-flight network diagnostics."*
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
