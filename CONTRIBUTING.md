# Contributing Guide

Welcome to AutoMetashape. All contributions, human and autonomous, must adhere to these standards.

---

## 1. Developer Certificate of Origin (DCO)

Every commit must include a DCO sign-off (`git commit -s`):
```text
Signed-off-by: Your Name <your.email@example.com>
```

---

## 2. Agent Development

Autonomous agents operating on this repository must observe:
- **Bounded Reading**: Read targeted functions and callers; avoid whole-tree context dumping.
- **Context Reuse**: Leverage loaded tools and diagnostics before issuing new probes.
- **Surgical Edits**: Formulate the smallest diff that satisfies the requirement; preserve unrelated workspace changes.
- **Validation Discipline**: Execute unit tests before and after modifications.
- **Zero Artifact Commits**: Never commit `.tmp`, `scratch/`, or transient agent logs.
- **No AI Attribution Lines**: Never include `Co-authored-by: AI...` trailers in commit messages.

---

## 3. Coding Style

- **Python**: Enforced via Ruff (`pyproject.toml`). Line length is 120 columns. Google docstrings required.
- **Licensing**: All new files must contain the Apache-2.0 SPDX header:
  ```python
  # Copyright (c) 2026, AutoMetashape Contributors.
  # All rights reserved.
  #
  # SPDX-License-Identifier: Apache-2.0
  ```
- **Logging**: Use standard logging; do not use raw `print()` for errors or warnings.

---

## 4. Testing & Validation

- All skills must pass validation via `python tools/skills/cli.py check`.
- License compliance is verified via `python tools/check_licenses.py`.
- Tests must pass the 4-question gate defined in `skills/developer/test-audit/SKILL.md`.

---

## 5. Build & Validation Tools

- **Compile / Setup**: Run `build.bat` (Windows) or `./build.sh` (Linux).
- **Format Code**: Run `format_code.bat` (Windows) or `./format_code.sh` (Linux).
- **Skill Validation**: Run `python tools/skills/cli.py check`.
- **License Check**: Run `python tools/check_licenses.py`.

---

## 6. Cluster Operations & Safeguards

All human contributors and autonomous agents managing Agisoft Metashape distributed processing clusters must strictly adhere to these operational invariants:

### 6.1 Operator Authority Gate
- **Zero Uncommanded Actions**: Autonomous agents and supervisors are strictly prohibited from executing batch aborts, changing task priorities, deleting queue items, or switching workers between projects without explicit, real-time operator authorization.
- **Transparent Exception Escalation**: When a task fails, stalls, or encounters a network disconnect, the agent must present diagnostic telemetry (hostname, batch ID, error string, and root cause) and wait for the operator's decision before performing any remedial intervention.

### 6.2 Process Scoping & Blast Radius Defense
- **Strict Ban on Host-Wide Process Kills**: Never execute `taskkill /IM metashape.exe`. Host-wide termination indiscriminately kills local compute processes when commands are run on dual-role nodes (machines hosting both coordinator services and compute workers).
- **Targeted PID Scoping**: All process termination on Windows must be strictly scoped by process identifier (`taskkill /F /T /PID <target_pid>`).
- **Atomic Remote Abort Signals**: Remote workers must be aborted exclusively via atomic UNC signal files (`.cluster/aborts/<node_name>.abort`). The remote worker daemon reaps its own process tree locally upon detecting the signal.

### 6.3 Mandatory Pre-Flight Project Materialization (`doc.save`)
- **Physical Archive Invariant**: Before calling `createBatch()` on the Network Server, the `.psx` project archive must physically exist on the shared storage filesystem (`assert os.path.isfile(psx_path)`).
- Submitting a batch referencing an unsaved or in-memory project causes remote workers to fail immediately with missing-file or I/O errors (`Can't open file`).
- Agents must always invoke canonical `scripts/submit_cluster_batch.py` which guarantees this invariant, rather than composing ad-hoc dispatch scripts.

### 6.4 Action-as-Code: Canonical Script Standard
- **Ban on Ad-Hoc Freehand Scripting**: Agents must never author improvised Python scripts in chat or scratch folders to manipulate the cluster queue or submit tasks.
- All cluster operations must strictly use the canonical, battle-tested utilities under `scripts/`:
  - `scripts/submit_cluster_batch.py`: Batch creation with guaranteed pre-flight `doc.save()` validation, optional `--exclude-folders` (with legacy worker shim), and timestamped alternate stub recovery when Metashape Server retains stale aborted batch keys.
  - `scripts/cluster_monitor.py`: Autonomous read-only telemetry, headroom audit, and deliverable verification.
  - `scripts/manage_queue.py`: Operator-directed queue and worker manipulation (`list`, `pause`, `unpause`, `set-priority`, `abort`, `pause-worker`, `unpause-worker`, `clear-worker --only-aborted`).
  - `scripts/inspect_checkpoint.py`: Non-destructive read-only inspection of `.psx` archives.
  - `scripts/cluster_abort.py`: Signal-based worker abort and self-healing restart.
  - `scripts/verify_deliverables.py`: Deterministic deliverable quality gate verification.
  - `scripts/update_fleet.py`: Rolling canary fleet updates.
  - `scripts/launch_server.py` & `scripts/launch_worker.py`: Cluster node daemons.

### 6.5 Prohibition of Script-Side Node Affinity Rejection
- **The Priority Thrashing Flaw**: Never implement client-side worker rejection by checking `COMPUTERNAME` inside batch execution scripts (e.g., `sys.exit(2)` if not the target node).
- In Metashape Server's priority queue, an idle worker will immediately re-request work and the server will re-dispatch the same highest-priority batch, creating an infinite, high-frequency claim-and-yield loop that starves all other cluster jobs.

### 6.6 Direct Storage Standard on Host Nodes
- When processing datasets residing on the local storage or shared drive of the current physical machine, pass `--no-local-scratch-staging`. Redundant local staging from a host UNC share to a local folder on the same machine wastes I/O bandwidth, risks disk exhaustion, and bypasses existing checkpoints.

### 6.7 Default Parameter & Zero-Neural Alignment Invariants
- Never dynamically downscale photogrammetry parameters (such as keypoint limits, tiepoint limits, or guided matching) based on photo count heuristics. Pipeline configurations remain default, canonical, and uncompromised.
- Keep `k4 = b1 = b2 = 0` and `adaptive_fitting = False` locked by default across alignment and USGS gradual-selection passes to prevent wide-angle UAV radial dome distortion.
- Maintain the zero-neural, zero-OS-Python boundary for multi-flight registration: `icp_bridge.py` and `icp_worker.py` must run strictly offline on CPU via the SHA-256-verified portable `.cluster/bridge_runtime/` bundle without introducing PyTorch, CUDA toolkit, or internet dependencies on worker nodes.

