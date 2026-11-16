# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Host-initiated autonomous worker fleet remote updater and lifecycle manager."""

from __future__ import annotations

import os
import shutil
import tempfile
import time

try:
    import Metashape
except ImportError:
    Metashape = None


DEFAULT_REPO_ARCHIVE_URL = "https://github.com/ngviettam82/AutoMetashape/archive/refs/heads/main.zip"
CLUSTER_MAINTENANCE_REL_PATH = ".cluster/maintenance.psx"
UPDATE_SIGNAL_FILENAME = ".update_pending"


def mirror_directory_with_prune(
    src_dir: str,
    dest_dir: str,
    protected_names: set[str] | None = None,
) -> tuple[int, int]:
    """Mirror files from src_dir to dest_dir with 1:1 Git parity, pruning obsolete files.

    Strictly preserves:
    - Machine-local configs (worker_config.json, server_config.json, .gitignore)
    - Runtime logs (*.log), PIDs (*.pid), and flags (*.flag, *.pending)
    - Subsystems (.cluster/, .git/, .venv/, .idea/, .vscode/, __pycache__/)

    Returns:
        tuple of (copied_file_count, pruned_file_count)
    """
    if protected_names is None:
        protected_names = {
            "worker_config.json",
            "server_config.json",
            ".gitignore",
        }

    os.makedirs(dest_dir, exist_ok=True)
    src_rel_files = set()
    copied = 0
    pruned = 0

    # 1. Walk src_dir and copy/overwrite files
    for root, _, files in os.walk(src_dir):
        for f in files:
            src_file = os.path.join(root, f)
            rel = os.path.relpath(src_file, src_dir)
            src_rel_files.add(rel)
            dst_file = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
            # Never overwrite machine-local config if it already exists
            if f in protected_names and os.path.isfile(dst_file):
                continue
            shutil.copy2(src_file, dst_file)
            copied += 1

    # 2. Walk dest_dir and prune obsolete repository files
    for root, dirs, files in os.walk(dest_dir, topdown=False):
        for f in files:
            dst_file = os.path.join(root, f)
            rel = os.path.relpath(dst_file, dest_dir)
            if f in protected_names:
                continue
            if any(rel.startswith(pfx) for pfx in (".git", ".cluster", ".venv", ".idea", ".vscode", "__pycache__")):
                continue
            if f.endswith((".log", ".pid", ".flag", ".pending")):
                continue
            if rel not in src_rel_files:
                try:
                    os.remove(dst_file)
                    pruned += 1
                except Exception:
                    pass

        # Remove empty directories (except protected ones)
        for d in dirs:
            dir_path = os.path.join(root, d)
            rel_d = os.path.relpath(dir_path, dest_dir)
            if any(rel_d.startswith(pfx) for pfx in (".git", ".cluster", ".venv", ".idea", ".vscode")):
                continue
            try:
                if os.path.exists(dir_path) and not os.listdir(dir_path):
                    os.rmdir(dir_path)
            except Exception:
                pass

    return copied, pruned


def check_update_pending(target_dir: str, node_name: str | None = None) -> dict | None:
    """Check if an autonomous update was requested via .update_pending signal file.

    Args:
        target_dir: Directory where repository code resides.
        node_name: Optional worker node hostname to check node-specific signal flag.

    Returns:
        Dictionary containing update signal payload, or None if no update is pending.
    """
    candidates = [
        os.path.join(target_dir, UPDATE_SIGNAL_FILENAME),
        os.path.join(tempfile.gettempdir(), UPDATE_SIGNAL_FILENAME),
    ]
    if node_name is None:
        target_node = os.environ.get("COMPUTERNAME", "").strip().lower()
    else:
        target_node = node_name.strip().lower()
    if target_node:
        candidates.append(os.path.join(tempfile.gettempdir(), f"{UPDATE_SIGNAL_FILENAME}_{target_node}"))

    for flag_path in candidates:
        try:
            with open(flag_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                continue
            import json

            data = json.loads(content)
            if isinstance(data, dict) and (data.get("action") == "autometashape_update" or "repo_url" in data):
                data["_flag_path"] = flag_path
                return data
        except Exception:
            continue
    return None


def perform_worker_codebase_update(signal_data: dict, target_dir: str) -> bool:
    """Download/copy archive, mirror with Git parity and prune, and clear signal file."""
    flag_path = signal_data.get("_flag_path")
    if not flag_path:
        flag_path = os.path.join(target_dir, UPDATE_SIGNAL_FILENAME)
    repo_url = signal_data.get("repo_url") or DEFAULT_REPO_ARCHIVE_URL
    shared_cluster_dir = signal_data.get("shared_cluster_dir", "")

    temp_dir = tempfile.mkdtemp(prefix="autometashape_update_")
    zip_path = os.path.join(temp_dir, "update.zip")
    extract_dir = os.path.join(temp_dir, "extracted")

    try:
        acquired = False
        # 1. Prefer shared LAN payload cache
        if shared_cluster_dir and os.path.isdir(shared_cluster_dir):
            lan_zip = os.path.join(shared_cluster_dir, "update_payload.zip")
            if os.path.isfile(lan_zip) and os.path.getsize(lan_zip) > 1024:
                try:
                    shutil.copy2(lan_zip, zip_path)
                    if os.path.isfile(zip_path) and os.path.getsize(zip_path) > 1024:
                        acquired = True
                except Exception:
                    pass

        # 2. Fallback to direct URL download
        if not acquired:
            import urllib.request

            req = urllib.request.Request(repo_url, headers={"User-Agent": "AutoMetashape-Supervisor/1.0"})
            with urllib.request.urlopen(req, timeout=45) as resp, open(zip_path, "wb") as out_f:
                shutil.copyfileobj(resp, out_f)

        # 3. Extract zip
        import zipfile

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        # 4. Resolve extracted root
        src_dir = extract_dir
        for d in os.listdir(extract_dir):
            cand = os.path.join(extract_dir, d)
            if os.path.isdir(cand) and (
                os.path.exists(os.path.join(cand, "launch_worker.bat"))
                or os.path.exists(os.path.join(cand, "pyproject.toml"))
            ):
                src_dir = cand
                break

        # 5. Mirror with prune
        mirror_directory_with_prune(src_dir, target_dir)

        # 6. Clean up flag
        if os.path.isfile(flag_path):
            try:
                os.remove(flag_path)
            except Exception:
                pass
        return True

    except Exception:
        # Clear flag on error to prevent infinite restart loop
        if os.path.isfile(flag_path):
            try:
                os.remove(flag_path)
            except Exception:
                pass
        return False
    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def generate_worker_updater_code(
    repo_url: str = DEFAULT_REPO_ARCHIVE_URL,
    expected_hostname: str = "",
    shared_cluster_dir: str = "",
) -> str:
    """Generate lightweight Python code executed on the remote worker node via RunScript.

    Writes the .update_pending signal file and shared status so the local persistent
    supervisor (launch_worker.py) can safely pull the codebase, mirror with Git-parity,
    and re-execute its process image upon quitWorker.
    """
    clean_expected = expected_hostname.strip().lower()
    clean_cluster_dir = shared_cluster_dir.replace("\\", "\\\\")
    return f'''import json
import os
import sys
import time

log = []
node_name = os.environ.get("COMPUTERNAME", "unknown")
expected_node = "{clean_expected}"
if expected_node and expected_node != "unknown":
    if node_name.strip().lower() != expected_node:
        err_msg = f"Target node mismatch: task assigned to {{node_name}}, expected {{expected_node}}"
        log.append(f"FATAL: {{err_msg}}")
        raise RuntimeError(err_msg)

log.append(f"Received worker update trigger on {{node_name}} at {{time.ctime()}}")
log.append(f"Python: {{sys.executable}}")

# 1. Resolve target directory: prioritize AUTOMETASHAPE_DIR env from launcher, fallback to Documents/AutoMetashape
target_dir = os.environ.get("AUTOMETASHAPE_DIR", "").strip()
if not target_dir or not os.path.isdir(target_dir):
    user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    # INVARIANT: %USERPROFILE%/Documents/AutoMetashape is the canonical hardcoded directory in the architecture.
    target_dir = os.path.join(user_profile, "Documents", "AutoMetashape")
os.makedirs(target_dir, exist_ok=True)
log.append(f"Target AutoMetashape directory: {{target_dir}}")

# 2. Write .update_pending signal file in target directory for persistent supervisor detection
flag_file = os.path.join(target_dir, "{UPDATE_SIGNAL_FILENAME}")
shared_dir = r"{clean_cluster_dir}"
flag_data = {{
    "action": "autometashape_update",
    "node_name": node_name,
    "repo_url": "{repo_url}",
    "shared_cluster_dir": shared_dir,
    "timestamp": time.time(),
    "target_dir": target_dir,
}}
with open(flag_file, "w", encoding="utf-8") as f:
    json.dump(flag_data, f, indent=2)
log.append(f"Registered update signal file: {{flag_file}}")

# 3. Write status to shared cluster directory for coordinator inspection
written = False
if shared_dir and os.path.isdir(shared_dir):
    try:
        res_file = os.path.join(shared_dir, f"{{node_name}}_update_result.txt")
        with open(res_file, "w", encoding="utf-8") as rf:
            rf.write("\\n".join(log) + "\\n")
        written = True
    except Exception:
        pass

if not written:
    try:
        doc = getattr(getattr(sys.modules.get("Metashape"), "app", None), "document", None)
        if doc and getattr(doc, "path", None):
            out_dir = os.path.dirname(doc.path)
            res_file = os.path.join(out_dir, f"{{node_name}}_update_result.txt")
            with open(res_file, "w", encoding="utf-8") as rf:
                rf.write("\\n".join(log) + "\\n")
    except Exception:
        pass
'''


def stage_cluster_update_payload(shared_root: str, repo_url: str = DEFAULT_REPO_ARCHIVE_URL) -> str:
    """Stages or caches the repository update archive zip on shared storage so workers can update over LAN.

    Returns:
        Absolute path to update_payload.zip on shared network storage, or empty string on failure.
    """
    if not shared_root:
        return ""
    cluster_dir = os.path.join(shared_root, ".cluster")
    os.makedirs(cluster_dir, exist_ok=True)
    payload_zip = os.path.join(cluster_dir, "update_payload.zip")

    # If payload is older than 5 minutes or missing, download fresh copy from repo_url
    needs_download = True
    if os.path.isfile(payload_zip):
        try:
            age = time.time() - os.path.getmtime(payload_zip)
            if age < 300.0 and os.path.getsize(payload_zip) > 1024:
                needs_download = False
        except Exception:
            pass

    if needs_download:
        import urllib.request

        try:
            req = urllib.request.Request(repo_url, headers={"User-Agent": "AutoMetashape-Coordinator/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(payload_zip, "wb") as out_f:
                shutil.copyfileobj(resp, out_f)
        except Exception:
            pass

    return payload_zip if os.path.isfile(payload_zip) else ""


def ensure_cluster_maintenance_project(shared_root: str) -> str:
    """Ensure .cluster/maintenance.psx exists on the shared network storage and clean stale locks."""
    cluster_dir = os.path.join(shared_root, ".cluster")
    os.makedirs(cluster_dir, exist_ok=True)
    psx_path = os.path.join(cluster_dir, "maintenance.psx")
    files_dir = os.path.join(cluster_dir, "maintenance.files")
    os.makedirs(files_dir, exist_ok=True)

    # Pre-clean any lingering local/network lock files from previous interrupted runs
    lock_file = os.path.join(files_dir, "lock")
    if os.path.isfile(lock_file):
        try:
            os.remove(lock_file)
        except Exception:
            pass

    if not os.path.isfile(psx_path):
        if Metashape is not None:
            try:
                doc = Metashape.Document()
                doc.save(psx_path)
                return CLUSTER_MAINTENANCE_REL_PATH
            except Exception:
                pass
        # Fallback to standard Metashape XML project structure
        xml_content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<document version="1.2.0" path="maintenance.files/project.zip"/>\n'
        )
        with open(psx_path, "w", encoding="utf-8") as f:
            f.write(xml_content)
    return CLUSTER_MAINTENANCE_REL_PATH


def list_online_workers(client) -> list[dict]:
    """Retrieve list of currently connected workers from Metashape Server."""
    if not client:
        return []
    info = client.workerList()
    if isinstance(info, dict):
        return [w for w in info.get("workers", []) if str(w.get("state", "")).lower() != "offline"]
    return []


def update_remote_worker(
    client,
    worker_id_or_name: str | int,
    shared_root: str,
    repo_url: str = DEFAULT_REPO_ARCHIVE_URL,
    timeout: float = 60.0,
    restart: bool = True,
    poll_interval: float = 2.0,
    force: bool = True,
) -> dict:
    """Dispatches remote update to a specific worker node with cluster isolation and force-abort capability.

    Args:
        client: Connected Metashape NetworkClient instance.
        worker_id_or_name: Worker ID, hostname, or IP address substring of the target node.
        shared_root: Shared network storage root containing .cluster maintenance files.
        repo_url: URL to repository archive zip.
        timeout: Maximum timeout in seconds to wait for update batch completion.
        restart: Whether to signal quitWorker for automatic daemon relaunch.
        poll_interval: Seconds between status polls.
        force: If True (default), aborts any active reconstruction processing on the worker so it can update
            immediately. If False, skips busy workers to preserve ongoing processing.

    Returns:
        Dictionary detailing success status, batch ID, relaunch confirmation, logs, or error details.
    """
    result = {
        "success": False,
        "worker_id": None,
        "hostname": None,
        "hostaddr": None,
        "batch_id": None,
        "relaunched": False,
        "new_worker_id": None,
        "new_hostaddr": None,
        "log": "",
        "error": None,
    }

    if Metashape is None or client is None:
        result["error"] = "Metashape Python API is not available"
        return result

    workers = list_online_workers(client)
    target = None
    target_str = str(worker_id_or_name).strip().lower()
    for w in workers:
        wid = str(w.get("worker_id", ""))
        hname = str(w.get("hostname", "")).lower()
        haddr = str(w.get("hostaddr", "")).lower()
        if target_str == wid or target_str == hname or target_str in haddr:
            target = w
            break

    if not target:
        result["error"] = f"Target worker '{worker_id_or_name}' not found among online workers"
        return result

    wid = target.get("worker_id")
    result["worker_id"] = wid
    result["hostname"] = target.get("hostname")
    result["hostaddr"] = target.get("hostaddr")
    node_hname = result["hostname"] or str(worker_id_or_name)

    paused_existing_batches: list[int] = []
    other_workers_paused: dict[int | str, bool] = {}

    try:
        # Pre-flight Check: Is target worker currently busy?
        target_state = str(target.get("state", "")).lower()
        if target_state not in ("ready", ""):
            if not force:
                result["error"] = (
                    f"Target worker '{node_hname}' (ID={wid}) is currently {target_state}. "
                    "Skipping update in safe mode (enable force to abort active processing)."
                )
                return result

            # FORCE MODE: Abort active processing to allow immediate update
            # 1. Temporarily pause queued/inprogress batches on server so worker cannot grab another job
            if hasattr(client, "batchList") and hasattr(client, "setBatchPaused"):
                try:
                    bl = client.batchList()
                    if isinstance(bl, dict):
                        for b in bl.get("batches", []):
                            bid = b.get("batch_id")
                            bstate = str(b.get("state", "")).lower()
                            if bstate in ("queued", "inprogress", "running", "active", "submitted", "pending"):
                                client.setBatchPaused(bid, True)
                                paused_existing_batches.append(bid)
                except Exception:
                    pass

            # 2. Abort any active in-progress batches on the server
            if hasattr(client, "abortBatch") and hasattr(client, "batchList"):
                try:
                    bl = client.batchList()
                    if isinstance(bl, dict):
                        for b in bl.get("batches", []):
                            bid = b.get("batch_id")
                            bstate = str(b.get("state", "")).lower()
                            if bstate in ("inprogress", "running", "active"):
                                client.abortBatch(bid)
                except Exception:
                    pass

            # 3. Wait for target worker to transition to 'ready'
            abort_start = time.time()
            target_ready = False
            while time.time() - abort_start < 10.0:
                time.sleep(1.0)
                refreshed = list_online_workers(client)
                for rw in refreshed:
                    if (
                        rw.get("worker_id") == wid or str(rw.get("hostname", "")).lower() == str(node_hname).lower()
                    ) and str(rw.get("state", "")).lower() == "ready":
                        target_ready = True
                        wid = rw.get("worker_id")
                        result["worker_id"] = wid
                        break
                if target_ready:
                    break

            # If still not ready after abort signal, force-terminate worker process via quitWorker for auto-reconnect
            if not target_ready:
                if hasattr(client, "quitWorker"):
                    try:
                        client.quitWorker(wid)
                    except Exception:
                        pass
                    reboot_deadline = time.time() + 30.0
                    while time.time() < reboot_deadline:
                        time.sleep(poll_interval)
                        refreshed = list_online_workers(client)
                        for rw in refreshed:
                            if (
                                str(rw.get("hostname", "")).lower() == str(node_hname).lower()
                                and str(rw.get("state", "")).lower() == "ready"
                            ):
                                wid = rw.get("worker_id")
                                result["worker_id"] = wid
                                result["hostname"] = rw.get("hostname")
                                result["hostaddr"] = rw.get("hostaddr")
                                target_ready = True
                                break
                        if target_ready:
                            break

            if not target_ready:
                result["error"] = f"Failed to force worker '{node_hname}' (ID={wid}) into ready state after abort"
                return result

        # 4. Cluster Isolation: Temporarily pause all OTHER workers so Metashape ONLY assigns update batch to target
        current_fleet = list_online_workers(client) if target_state not in ("ready", "") else workers
        for w in current_fleet:
            w_id = w.get("worker_id")
            if w_id != wid:
                other_workers_paused[w_id] = bool(w.get("paused", False))
                if hasattr(client, "setWorkerPaused"):
                    try:
                        client.setWorkerPaused(w_id, True)
                    except Exception:
                        pass
            else:
                if hasattr(client, "setWorkerPaused"):
                    try:
                        client.setWorkerPaused(wid, False)
                    except Exception:
                        pass

        # 5. Ensure maintenance project and stage update payload
        rel_project = ensure_cluster_maintenance_project(shared_root)
        cluster_shared_dir = os.path.join(shared_root, ".cluster")
        stage_cluster_update_payload(shared_root=shared_root, repo_url=repo_url)

        # 6. Build RunScript task locked to target worker's hostname and shared LAN cache
        task = Metashape.Tasks.RunScript()
        task.code = generate_worker_updater_code(
            repo_url=repo_url,
            expected_hostname=result["hostname"] or "",
            shared_cluster_dir=cluster_shared_dir,
        )
        net_task = task.toNetworkTask()

        # 7. Create batch locked to 1 worker
        try:
            batch_id = client.createBatch(rel_project, [net_task])
            result["batch_id"] = batch_id
            if hasattr(client, "setBatchWorkerLimit"):
                try:
                    client.setBatchWorkerLimit(batch_id, 1)
                except Exception:
                    pass
            client.setBatchPaused(batch_id, False)
        except Exception as e:
            result["error"] = f"Failed to dispatch update batch: {e}"
            return result

        # 8. Wait for batch completion
        start_t = time.time()
        batch_done = False
        while time.time() - start_t < timeout:
            batches_info = client.batchList()
            batch_entry = None
            if isinstance(batches_info, dict):
                for b in batches_info.get("batches", []):
                    if b.get("batch_id") == batch_id:
                        batch_entry = b
                        break
            if batch_entry:
                state = str(batch_entry.get("state", "")).lower()
                if state in ("completed", "finished"):
                    batch_done = True
                    break
                if state in ("failed", "aborted", "cancelled"):
                    result["error"] = f"Update batch failed on worker with state: {state}"
                    return result
            time.sleep(poll_interval)

        if not batch_done:
            # Abort the timed-out batch on the cluster server so it does not linger in the queue
            if hasattr(client, "abortBatch") and batch_id:
                try:
                    client.abortBatch(batch_id)
                except Exception:
                    pass
            result["error"] = f"Update batch timed out after {timeout} seconds"
            return result

        # Read node result log if available
        node_name = result["hostname"] or "unknown"
        res_path = os.path.join(shared_root, ".cluster", f"{node_name}_update_result.txt")
        if os.path.isfile(res_path):
            try:
                with open(res_path, "r", encoding="utf-8") as f:
                    result["log"] = f.read()
            except Exception:
                pass

        result["success"] = True

        # 9. Graceful restart via quitWorker: worker supervisor will detect .update_pending,
        # mirror code with Git-parity, and re-execute fresh process image.
        if restart:
            try:
                client.quitWorker(wid)
                # Await auto-reconnect (10s reconnect window + 25s buffer)
                reconnect_deadline = time.time() + 35.0
                reconnected = False
                target_host_ip = str(result["hostaddr"]).split(":")[0].strip() if result.get("hostaddr") else ""
                while time.time() < reconnect_deadline:
                    time.sleep(poll_interval)
                    current_workers = list_online_workers(client)
                    for cw in current_workers:
                        cw_id = cw.get("worker_id")
                        cw_hname = str(cw.get("hostname", "")).strip().lower()
                        cw_addr = str(cw.get("hostaddr", "")).strip()
                        cw_state = str(cw.get("state", "")).strip().lower()

                        match_hname = bool(result["hostname"] and cw_hname == str(result["hostname"]).strip().lower())
                        match_ip = bool(target_host_ip and target_host_ip != "127.0.0.1" and target_host_ip in cw_addr)

                        if cw_id != wid and (match_hname or match_ip) and cw_state == "ready":
                            result["relaunched"] = True
                            result["new_worker_id"] = cw_id
                            result["new_hostaddr"] = cw.get("hostaddr")
                            reconnected = True
                            break
                    if reconnected:
                        break
            except Exception as e:
                result["error"] = f"Update succeeded but quitWorker restart failed: {e}"

    finally:
        # 10. Guaranteed teardown: restore other workers' pause states
        for ow_id, was_paused in other_workers_paused.items():
            if hasattr(client, "setWorkerPaused"):
                try:
                    client.setWorkerPaused(ow_id, was_paused)
                except Exception:
                    pass

        # Restore any paused batches
        for bid in paused_existing_batches:
            if hasattr(client, "setBatchPaused"):
                try:
                    client.setBatchPaused(bid, False)
                except Exception:
                    pass

    return result


def update_all_workers_parallel(
    client,
    shared_root: str,
    repo_url: str = DEFAULT_REPO_ARCHIVE_URL,
    timeout: float = 90.0,
    restart: bool = True,
    poll_interval: float = 2.0,
    force: bool = True,
) -> list[dict]:
    """Dispatches update signals and lifecycle restarts across all online workers in parallel."""
    if Metashape is None or client is None:
        return [{"error": "Metashape Python API is not available", "success": False}]

    workers = list_online_workers(client)
    if not workers:
        return []

    # 1. Stage update payload once
    stage_cluster_update_payload(shared_root, repo_url)
    rel_project = ensure_cluster_maintenance_project(shared_root)
    cluster_shared_dir = os.path.join(shared_root, ".cluster")

    # 2. If force mode, pause queued and abort active tasks on busy workers
    paused_existing_batches = []
    if force:
        if hasattr(client, "batchList"):
            try:
                bl = client.batchList()
                if isinstance(bl, dict):
                    for b in bl.get("batches", []):
                        bid = b.get("batch_id")
                        bstate = str(b.get("state", "")).lower()
                        if bstate in ("queued", "pending", "submitted"):
                            if hasattr(client, "setBatchPaused"):
                                client.setBatchPaused(bid, True)
                                paused_existing_batches.append(bid)
                        elif bstate in ("inprogress", "running", "active"):
                            if hasattr(client, "abortBatch"):
                                client.abortBatch(bid)
            except Exception:
                pass

        # Await workers to transition to ready
        t0 = time.time()
        while time.time() - t0 < 10.0:
            time.sleep(1.0)
            fresh = list_online_workers(client)
            if all(str(w.get("state", "")).lower() in ("ready", "") for w in fresh):
                break

    # 3. Create update batch for each worker
    worker_batches: list[dict] = []
    for w in workers:
        wid = w.get("worker_id")
        hname = w.get("hostname", "")
        wstate = str(w.get("state", "")).lower()
        node_res = {
            "success": False,
            "worker_id": wid,
            "hostname": hname,
            "hostaddr": w.get("hostaddr"),
            "batch_id": None,
            "relaunched": False,
            "new_worker_id": None,
            "new_hostaddr": None,
            "log": "",
            "error": None,
        }

        if wstate not in ("ready", "") and not force:
            node_res["error"] = f"Worker {hname} is busy ({wstate}). Skipped in safe mode."
            worker_batches.append(node_res)
            continue

        try:
            task = Metashape.Tasks.RunScript()
            task.code = generate_worker_updater_code(
                repo_url=repo_url,
                expected_hostname=hname,
                shared_cluster_dir=cluster_shared_dir,
            )
            net_task = task.toNetworkTask()
            batch_id = client.createBatch(rel_project, [net_task])
            node_res["batch_id"] = batch_id
            if hasattr(client, "setBatchWorkerLimit"):
                client.setBatchWorkerLimit(batch_id, 1)
            client.setBatchPaused(batch_id, False)
        except Exception as e:
            node_res["error"] = f"Failed to dispatch batch: {e}"

        worker_batches.append(node_res)

    try:
        # 4. Poll until all dispatched batches finish
        start_t = time.time()
        while time.time() - start_t < timeout:
            all_done = True
            bl = client.batchList() if hasattr(client, "batchList") else {}
            batches_map = {}
            if isinstance(bl, dict):
                for b in bl.get("batches", []):
                    batches_map[b.get("batch_id")] = str(b.get("state", "")).lower()

            for nr in worker_batches:
                bid = nr.get("batch_id")
                if not bid or nr.get("success") or nr.get("error"):
                    continue
                state = batches_map.get(bid, "")
                if state in ("completed", "finished"):
                    nr["success"] = True
                elif state in ("failed", "aborted", "cancelled"):
                    nr["error"] = f"Batch failed with state: {state}"
                else:
                    all_done = False

            if all_done:
                break
            time.sleep(poll_interval)

        # 5. Read result logs and issue quitWorker for completed workers
        relaunching_nodes = []
        for nr in worker_batches:
            if nr.get("success"):
                hname = nr.get("hostname") or "unknown"
                rf_path = os.path.join(shared_root, ".cluster", f"{hname}_update_result.txt")
                if os.path.isfile(rf_path):
                    try:
                        with open(rf_path, "r", encoding="utf-8") as f:
                            nr["log"] = f.read()
                    except Exception:
                        pass
                if restart and hasattr(client, "quitWorker") and nr.get("worker_id"):
                    try:
                        client.quitWorker(nr["worker_id"])
                        relaunching_nodes.append(nr)
                    except Exception as e_quit:
                        nr["error"] = f"Update succeeded but quitWorker failed: {e_quit}"

        # 6. Await parallel reconnection
        if relaunching_nodes:
            reconnect_deadline = time.time() + 35.0
            while time.time() < reconnect_deadline:
                time.sleep(poll_interval)
                current_workers = list_online_workers(client)
                for nr in relaunching_nodes:
                    if nr.get("relaunched"):
                        continue
                    old_wid = nr.get("worker_id")
                    target_hname = str(nr.get("hostname", "")).strip().lower()
                    target_ip = str(nr.get("hostaddr", "")).split(":")[0].strip()

                    for cw in current_workers:
                        cw_id = cw.get("worker_id")
                        cw_hname = str(cw.get("hostname", "")).strip().lower()
                        cw_addr = str(cw.get("hostaddr", "")).strip()
                        cw_state = str(cw.get("state", "")).strip().lower()

                        match_hname = bool(target_hname and cw_hname == target_hname)
                        match_ip = bool(target_ip and target_ip != "127.0.0.1" and target_ip in cw_addr)

                        if cw_id != old_wid and (match_hname or match_ip) and cw_state == "ready":
                            nr["relaunched"] = True
                            nr["new_worker_id"] = cw_id
                            nr["new_hostaddr"] = cw.get("hostaddr")
                            break

                if all(nr.get("relaunched") for nr in relaunching_nodes):
                    break

    finally:
        # Restore any paused batches
        for bid in paused_existing_batches:
            if hasattr(client, "setBatchPaused"):
                try:
                    client.setBatchPaused(bid, False)
                except Exception:
                    pass

    return worker_batches
