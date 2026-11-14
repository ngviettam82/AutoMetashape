---
name: cluster-fleet-operations
description: "Orchestrates Agisoft Metashape Pro distributed processing clusters, worker node provisioning, dynamic multi-GPU discovery, local NVMe scratch staging, hourly health monitoring, and canary fleet updates. Use when deploying, managing, updating, or troubleshooting distributed photogrammetry clusters and multi-worker fleets."
audience: user
status: stable
owners:
  - "@autometashape-maintainers"
---

# Distributed Processing Cluster & Autonomous Fleet Operations

Comprehensive operational runbook and architectural standard for provisioning, orchestrating, monitoring, and updating Agisoft Metashape Professional distributed processing clusters. Covers cluster server daemons, worker node provisioning with pre-flight TCP/UNC diagnostics, dynamic native multi-GPU discovery, local NVMe scratch staging, continuous autonomous health audits, and zero-touch canary fleet update protocols.

---

## When To Use

Use this skill when:
- Deploying or starting the central Cluster Coordinator Server (`metashape-server.exe`) on a LAN host.
- Provisioning or starting remote worker nodes (`launch_worker.bat`) across workstation fleets.
- Configuring unattended worker provisioning via `worker_config.json` with zero-touch automation.
- Diagnosing network reachability (TCP port 5840), shared UNC storage path access, or GPU device discovery.
- Managing Local NVMe Scratch Staging (`--stage-local` / `--local-scratch-staging`) to eliminate SMB network congestion and Win32 Error 112 (`ERROR_DISK_FULL`).
- Executing the Autonomous 1-Hour Health & Self-Healing Monitoring Cycle for multi-hour cluster reconstructions.
- Performing rolling fleet updates (`update.bat`) across remote worker nodes using the Single Canary Node Verification Gate.
- Troubleshooting worker state transitions (`inprogress -> offline -> ready`), stalled tasks, or network disconnects.

Do not use this skill when:
- Performing single-node local photogrammetry reconstructions without network distribution (use `skills/user/image-to-3d-model/`).
- Exporting, format converting, or baking deliverables from an existing project archive (use `skills/user/photogrammetry-export/`).

---

## Workflow

```mermaid
flowchart TD
    subgraph Host_Coordinator["Cluster Coordinator Host (e.g. SERVER / 192.168.1.100)"]
        ServerProc["metashape-server.exe\n(TCP 5840 Control Daemon)"]
        SharedStorage["Shared UNC Root (\\\\HOST\\SharedMetashape)"]
        StatusSidecar["_autometashape_status.json\n(Milestone IPC Telemetry)"]
    end

    subgraph Remote_Worker_Fleet["Remote Worker Fleet (Nodes: tan, RJUQ6L8, etc.)"]
        WorkerProc["Metashape Worker Daemon\n(launch_worker.bat)"]
        LocalScratch["Local Fast Scratch NVMe\n(C:\\AutoMetashape_Scratch or D:\\)"]
        DiscreteGPU["Discrete RTX GPUs\n(Native C++ Discovery)"]
    end

    ServerProc <-->|TCP 5840 Task Queue & Heartbeats| WorkerProc
    SharedStorage -->|Read Photo Assets & Project Files| WorkerProc
    WorkerProc <-->|High-Speed NVMe I/O (75+ GB Temp Leaves)| LocalScratch
    WorkerProc <-->|CUDA Kernels| DiscreteGPU
    WorkerProc -->|Atomic Write on Stage Completion| SharedStorage
    WorkerProc -->|Push Real-Time Sidecar Events| StatusSidecar
```

### 1. Cluster Coordinator Server Provisioning
On the Coordinator workstation hosting the shared network storage:
1. Verify network share accessibility via UNC format (`\\HOST_PC\SharedMetashape`).
2. Launch the cluster coordinator server daemon:
   ```powershell
   .\launch_server.bat --port 5840 --root "\\HOST_PC\SharedMetashape"
   ```
3. Confirm server listening state:
   ```powershell
   Test-NetConnection -ComputerName 127.0.0.1 -Port 5840
   ```

### 2. Worker Node Provisioning & Pre-Flight Diagnostics
On each remote worker node across the local network:
1. Interactive Launch with Guided Wizard (stores `worker_config.json`):
   ```powershell
   .\launch_worker.bat
   ```
2. Unattended / Headless Deployment:
   Place `worker_config.json` in the AutoMetashape root directory:
   ```json
   {
     "host": "10.2.8.43",
     "port": "5840",
     "root": "\\\\HOST_PC\\SharedMetashape",
     "cpu_enable": 1
   }
   ```
   Execute non-interactively:
   ```powershell
   .\launch_worker.bat --non-interactive
   ```
3. Pre-Flight Diagnostics Built-in:
   - Validates Metashape Pro executable across `%ProgramFiles%` and all drives `C:`, `D:`, `E:`, `F:`, `G:`.
   - Tests TCP port reachability to server host.
   - Tests Shared UNC storage directory access and read/write permissions.
   - **Native Multi-GPU Discovery**: When `--gpu-mask` is omitted or `"Auto"` (default), Metashape Pro dynamically discovers, benchmarks, and engages all discrete GPUs natively, bypassing integrated graphics.
   - **Auto-Reconnect Daemon**: Automatically reconnects every 10s if the coordinator restarts or network fluctuates.

### 3. Local NVMe Scratch Staging Management
During large-scale reconstructions (1,000+ to 10,000+ photos), intermediate scratch data (depth maps, point clouds, and octree meshing leaves) reaches 75–150+ GB.
- **Default Staging**: Enabled by default (`--stage-local`).
- **Dynamic NVMe Discovery**: The worker daemon automatically discovers the fastest non-system NVMe SSD with the highest free capacity (e.g., `D:\AutoMetashape_Scratch`).
- **Headroom Fallback**: Requires $\ge 100\text{ GB}$ free space (configurable via `--min-scratch-free-gb`). Safely falls back to direct network storage if local space is insufficient.
- **Atomic Deliverable Sync**: Consolidated deliverables and project files are synchronized atomically to the central network share upon completion, and local scratch is cleanly purged.
- **Opt-Out**: Pass `--no-stage-local` to bypass local staging.

### 4. Autonomous 1-Hour Monitoring & Self-Healing Protocol
For multi-hour cluster runs, execute the continuous monitoring cycle:
1. **Telemetry & Milestone Inspection**:
   ```python
   import Metashape

   client = Metashape.NetworkClient()
   client.connect("10.2.8.43:5840")
   workers = client.workerList()
   batches = client.batchList()
   ```
2. **Sidecar Inspection**: Read `_autometashape_status.json` from the shared output directory.
3. **Structured Hourly Status Reporting**:
   - Active Batches: Current stage, subtask completion %, elapsed time, ETA.
   - Worker Health: Active/idle states, GPU utilization, memory usage.
   - Deliverables: Verified files on disk (`.obj`, `.glb`, `orthomosaic.tif`).
4. **Self-Healing Diagnostics**:
   - *Dropped Worker*: Ping host, alert user, or trigger daemon restart.
   - *Stalled Task (>45m 0% progress)*: Trigger safe checkpoint resume.
   - *Storage Latency / Access Drops*: Audit SMB UNC path connectivity.

> [!IMPORTANT]
> **Strict Non-Destructive Code Governance Boundary**:
> - Permitted: Network reconnection, daemon restarts, batch retry via `--resume`, lockfile cleanup, and structured progress reporting.
> - Strictly Prohibited: Never autonomously rewrite, mutate, refactor, or modify core reconstruction code or pipeline scripts during monitoring runs without explicit user authorization.

### 5. Host-Initiated Autonomous Worker Fleet Update Protocol
When maintaining or upgrading remote cluster worker nodes from the Coordinator host:
1. **Zero Fleet-Wide Blind Kills**: Never kill or push updates across all nodes simultaneously.
2. **Single Canary Node Verification Gate**:
   - Dispatch update probe to exactly **ONE canary worker node** first (e.g. `tan`).
   - Monitor canary state transition:
     $$\text{inprogress (probe)} \longrightarrow \text{offline (releasing locks)} \longrightarrow \text{ready (online with fresh code)}$$
   - Confirm canary node completes self-update and re-registers as `ready` within **60–90 seconds**.
   - Only proceed across remaining worker nodes in staggered waves after canary passes.
3. **Preserve Host / Coordinator Exemption**: Coordinator host must be explicitly excluded from worker update batches.
4. **Zero-Touch Headless Invariants**: Worker nodes execute non-interactively with automatic UNC path inference and dynamic GPU discovery.

---

## Validation

Verify that all procedures and skill contracts execute cleanly:

```bash
# Validate skill architecture and governance rules
python tools/skills/cli.py check

# Audit license compliance
python tools/check_licenses.py

# Execute test suite covering cluster utilities and CLI arguments
python -m unittest tests/test_cluster_utils.py tests/test_cli_args.py
```

---

## Maintenance

- Review and audit worker connectivity and protocol compatibility with each Agisoft Metashape Pro minor/major update.
- Verify that `worker_config.json` schema remains backward-compatible across existing fleet deployments.
- Periodically validate NVMe scratch purge logic to guarantee zero residual disk leak on remote worker workstations.

---

## References

- Contributing guide: [CONTRIBUTING.md](../../../CONTRIBUTING.md)
- Image to 3D model skill: [skills/user/image-to-3d-model/SKILL.md](../image-to-3d-model/SKILL.md)
- Benchmark evaluation suite: [evaluations.md](evaluations.md)
