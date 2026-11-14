# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Host-initiated autonomous worker fleet remote updater and lifecycle manager."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

try:
    import Metashape
except ImportError:
    Metashape = None

from autometashape.cluster.hardware import (
    find_metashape_path,
    get_primary_lan_ip,
    test_tcp_connection,
)

DEFAULT_REPO_ARCHIVE_URL = "https://github.com/ngviettam82/AutoMetashape/archive/refs/heads/main.zip"
CLUSTER_MAINTENANCE_REL_PATH = ".cluster/maintenance.psx"


def generate_worker_updater_code(repo_url: str = DEFAULT_REPO_ARCHIVE_URL) -> str:
    """Generate self-contained Python code executed on the remote worker node via RunScript."""
    return f'''import os
import shutil
import sys
import time
import urllib.request
import zipfile

log = []
node_name = os.environ.get("COMPUTERNAME", "unknown")
log.append(f"Started worker update on {{node_name}} at {{time.ctime()}}")
log.append(f"Python: {{sys.executable}}")

user_profile = os.environ.get("USERPROFILE", r"C:\\Users\\Default")
target_dir = os.path.join(user_profile, "Documents", "AutoMetashape")
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
        xml_content = '<?xml version="1.0" encoding="UTF-8"?>\n<document version="1.2.0" path="maintenance.files/project.zip"/>\n'
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
) -> dict:
    """Dispatches remote update to a specific worker node and optionally triggers auto-reconnect restart."""
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

    # 1. Ensure maintenance project
    rel_project = ensure_cluster_maintenance_project(shared_root)

    # 2. Build RunScript task
    task = Metashape.Tasks.RunScript()
    task.code = generate_worker_updater_code(repo_url=repo_url)
    net_task = task.toNetworkTask()

    # 3. Create batch
    try:
        batch_id = client.createBatch(rel_project, [net_task])
        result["batch_id"] = batch_id
        client.setBatchPaused(batch_id, False)
    except Exception as e:
        result["error"] = f"Failed to dispatch update batch: {e}"
        return result

    # 4. Wait for batch completion
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

    # 5. Optional graceful restart via quitWorker
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

    return result
