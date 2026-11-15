# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Host-initiated autonomous worker fleet remote updater and lifecycle manager."""

from __future__ import annotations

import os
import time

try:
    import Metashape
except ImportError:
    Metashape = None


DEFAULT_REPO_ARCHIVE_URL = "https://github.com/ngviettam82/AutoMetashape/archive/refs/heads/main.zip"
CLUSTER_MAINTENANCE_REL_PATH = ".cluster/maintenance.psx"


def generate_worker_updater_code(repo_url: str = DEFAULT_REPO_ARCHIVE_URL, expected_hostname: str = "") -> str:
    """Generate self-contained Python code executed on the remote worker node via RunScript."""
    clean_expected = expected_hostname.strip().lower()
    return f'''import os
import shutil
import sys
import time
import urllib.request
import zipfile

log = []
node_name = os.environ.get("COMPUTERNAME", "unknown")
expected_node = "{clean_expected}"
if expected_node and expected_node != "unknown":
    if node_name.strip().lower() != expected_node:
        err_msg = f"Target node mismatch: task assigned to {{node_name}}, expected {{expected_node}}"
        log.append(f"FATAL: {{err_msg}}")
        raise RuntimeError(err_msg)

log.append(f"Started worker update on {{node_name}} at {{time.ctime()}}")
log.append(f"Python: {{sys.executable}}")

user_profile = os.environ.get("USERPROFILE", r"C:\\Users\\Default")
candidates = [
    os.environ.get("AUTOMETASHAPE_DIR", ""),
    os.path.join(user_profile, "Documents", "AutoMetashape"),
    os.path.join(user_profile, "AutoMetashape"),
    os.getcwd(),
]
target_dir = os.path.join(user_profile, "Documents", "AutoMetashape")
for cand in candidates:
    if cand and os.path.isdir(cand) and os.path.exists(os.path.join(cand, "launch_worker.bat")):
        target_dir = cand
        break
temp_dir = os.environ.get("TEMP", r"C:\\Temp")
zip_path = os.path.join(temp_dir, "AutoMetashape_remote_update.zip")
extract_dir = os.path.join(temp_dir, "AutoMetashape_remote_extracted")

url = "{repo_url}"
try:
    log.append(f"Downloading codebase from {{url}}...")
    req = urllib.request.Request(url, headers={{"User-Agent": "AutoMetashape-FleetUpdater/1.0"}})
    with urllib.request.urlopen(req, timeout=45) as resp, open(zip_path, "wb") as out_f:
        shutil.copyfileobj(resp, out_f)
    log.append(f"Downloaded zip ({{os.path.getsize(zip_path)}} bytes).")
except Exception as e:
    log.append(f"ERROR downloading: {{e}}")

if os.path.exists(zip_path):
    try:
        if os.path.exists(extract_dir):
            shutil.rmtree(extract_dir, ignore_errors=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
        log.append("Extracted archive successfully.")

        src_dir = os.path.join(extract_dir, "AutoMetashape-main")
        if not os.path.exists(src_dir):
            for d in os.listdir(extract_dir):
                cand = os.path.join(extract_dir, d)
                if os.path.isdir(cand) and os.path.exists(os.path.join(cand, "launch_worker.bat")):
                    src_dir = cand
                    break
        log.append(f"Extracted root: {{src_dir}}")

        os.makedirs(target_dir, exist_ok=True)
        copied = 0
        for root, dirs, files in os.walk(src_dir):
            rel = os.path.relpath(root, src_dir)
            dest = os.path.join(target_dir, rel)
            os.makedirs(dest, exist_ok=True)
            for file in files:
                if file in ("worker_config.json", "server_config.json"):
                    continue
                shutil.copy2(os.path.join(root, file), os.path.join(dest, file))
                copied += 1
        log.append(f"Successfully mirrored {{copied}} files into {{target_dir}}")

        try:
            os.remove(zip_path)
            shutil.rmtree(extract_dir, ignore_errors=True)
        except Exception:
            pass
    except Exception as e:
        log.append(f"ERROR during extraction/copy: {{e}}")

log.append(f"Completed worker update on {{node_name}} at {{time.ctime()}}")

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


def ensure_cluster_maintenance_project(shared_root: str) -> str:
    """Ensure .cluster/maintenance.psx exists on the shared network storage."""
    cluster_dir = os.path.join(shared_root, ".cluster")
    os.makedirs(cluster_dir, exist_ok=True)
    psx_path = os.path.join(cluster_dir, "maintenance.psx")
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
        files_dir = os.path.join(cluster_dir, "maintenance.files")
        os.makedirs(files_dir, exist_ok=True)
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

        # 5. Ensure maintenance project
        rel_project = ensure_cluster_maintenance_project(shared_root)

        # 6. Build RunScript task locked to target worker's hostname
        task = Metashape.Tasks.RunScript()
        task.code = generate_worker_updater_code(repo_url=repo_url, expected_hostname=result["hostname"] or "")
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

        # 9. Optional graceful restart via quitWorker
        if restart:
            try:
                client.quitWorker(wid)
                # Await auto-reconnect (10s reconnect window + 10s buffer)
                reconnect_deadline = time.time() + 30.0
                reconnected = False
                while time.time() < reconnect_deadline:
                    time.sleep(poll_interval)
                    current_workers = list_online_workers(client)
                    for cw in current_workers:
                        if (
                            cw.get("worker_id") != wid
                            and (
                                str(cw.get("hostname", "")).lower() == str(result["hostname"]).lower()
                                or str(result["hostaddr"]).split(":")[0] in str(cw.get("hostaddr", ""))
                            )
                            and str(cw.get("state", "")).lower() == "ready"
                        ):
                            result["relaunched"] = True
                            result["new_worker_id"] = cw.get("worker_id")
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
