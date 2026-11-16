# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Autonomous signal-based worker abort, self-healing restart, and lock cleanup."""

from __future__ import annotations

import json
import os
import string
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import Metashape
except ImportError:
    Metashape = None

ABORT_SIGNAL_FILENAME = ".abort_pending"
ABORT_DIR_NAME = "aborts"
ABORT_SIGNALS_JSON = "abort_signals.json"


def cleanup_stale_worker_processes() -> None:
    """Terminate orphaned metashape worker processes on this host to prevent duplicate connections."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/IM", "metashape.exe"], capture_output=True, timeout=2)
            time.sleep(0.5)
            subprocess.run(["taskkill", "/F", "/IM", "metashape.exe"], capture_output=True, timeout=2)
        except Exception:
            pass


def kill_worker_process(proc: Any, timeout: float = 2.0) -> bool:
    """Forcibly and deterministically terminate the worker process and its process tree.

    Args:
        proc: subprocess.Popen instance representing the worker process.
        timeout: Maximum seconds to wait for graceful termination before force killing.

    Returns:
        True if process terminated successfully, False otherwise.
    """
    if proc is None:
        return True

    # 1. Check if already terminated
    poll_fn = getattr(proc, "poll", None)
    if callable(poll_fn) and poll_fn() is not None:
        return True

    # 2. Attempt graceful termination
    terminate_fn = getattr(proc, "terminate", None)
    if callable(terminate_fn):
        try:
            terminate_fn()
            wait_deadline = time.time() + timeout
            while time.time() < wait_deadline:
                if callable(poll_fn) and poll_fn() is not None:
                    return True
                time.sleep(0.1)
        except Exception:
            pass

    # 3. Force kill if still running
    kill_fn = getattr(proc, "kill", None)
    if callable(kill_fn):
        try:
            kill_fn()
            time.sleep(0.3)
        except Exception:
            pass

    # 4. Windows taskkill tree kill defense
    proc_pid = getattr(proc, "pid", None)
    if proc_pid and sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc_pid)],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

    # 5. Clean up any remaining orphaned metashape.exe instances
    cleanup_stale_worker_processes()
    return True


def cleanup_scratch_locks(target_dir: str = "", root_path: str = "") -> List[str]:
    """Remove any stale lock files in target_dir, temp dir, or local scratch staging dirs.

    Args:
        target_dir: Active project target directory to inspect.
        root_path: Shared network storage root containing .cluster maintenance files.

    Returns:
        List of paths of removed lock files.
    """
    removed_locks: List[str] = []
    lock_dirs: List[str] = []

    if target_dir and os.path.isdir(target_dir):
        lock_dirs.append(target_dir)

    temp_dir = tempfile.gettempdir()
    if os.path.isdir(temp_dir):
        lock_dirs.append(temp_dir)

    if root_path and os.path.isdir(root_path):
        cluster_dir = os.path.join(root_path, ".cluster")
        if os.path.isdir(cluster_dir):
            lock_dirs.append(cluster_dir)

    # Local scratch directories across system drives
    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            scratch_cand = rf"{letter}:\AutoMetashape_Scratch"
            if os.path.isdir(scratch_cand):
                lock_dirs.append(scratch_cand)
    else:
        for cand in ["/mnt/scratch", "/var/tmp", "/tmp", os.path.expanduser("~/AutoMetashape_Scratch")]:
            if os.path.isdir(cand):
                lock_dirs.append(cand)

    for d in lock_dirs:
        if not os.path.isdir(d):
            continue
        try:
            for root, _, files in os.walk(d):
                for f in files:
                    if f.lower() == "lock" or f.lower().endswith(".lock"):
                        lock_path = os.path.join(root, f)
                        try:
                            os.remove(lock_path)
                            removed_locks.append(lock_path)
                        except Exception:
                            pass
        except Exception:
            pass

    return removed_locks


def check_abort_signal(
    target_dir: str = "",
    root_path: str = "",
    node_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Check if an autonomous abort signal was requested for this worker node.

    Inspects both local filesystem signal paths and UNC cluster signal paths.

    Args:
        target_dir: Active worker working directory.
        root_path: Shared network storage UNC path.
        node_name: Current host node name (default: COMPUTERNAME or 'unknown').

    Returns:
        Dictionary containing abort signal metadata and _signal_path, or None.
    """
    resolved_node = (node_name or os.environ.get("COMPUTERNAME", "unknown")).strip().lower()
    temp_dir = tempfile.gettempdir()

    candidates: List[str] = []
    if target_dir:
        candidates.append(os.path.join(target_dir, ABORT_SIGNAL_FILENAME))
    candidates.append(os.path.join(temp_dir, ABORT_SIGNAL_FILENAME))
    if resolved_node and resolved_node != "unknown":
        candidates.append(os.path.join(temp_dir, f"{ABORT_SIGNAL_FILENAME}_{resolved_node}"))

    # UNC Cluster signal paths
    if root_path and os.path.isdir(root_path):
        aborts_dir = os.path.join(root_path, ".cluster", ABORT_DIR_NAME)
        if resolved_node and resolved_node != "unknown":
            candidates.append(os.path.join(aborts_dir, f"{resolved_node}.abort"))
        candidates.append(os.path.join(aborts_dir, "all.abort"))

        # Shared aggregate json
        json_sig = os.path.join(root_path, ".cluster", ABORT_SIGNALS_JSON)
        if os.path.isfile(json_sig):
            try:
                with open(json_sig, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    entry = data.get(resolved_node) or data.get("all")
                    if not entry and "signals" in data and isinstance(data["signals"], dict):
                        entry = data["signals"].get(resolved_node) or data["signals"].get("all")
                    if isinstance(entry, dict):
                        entry_copy = dict(entry)
                        entry_copy["_signal_path"] = json_sig
                        entry_copy["_json_key"] = (
                            resolved_node
                            if resolved_node in data or ("signals" in data and resolved_node in data["signals"])
                            else "all"
                        )
                        return entry_copy
            except Exception:
                pass

    for flag_path in candidates:
        if not os.path.isfile(flag_path):
            continue
        try:
            with open(flag_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            data: Dict[str, Any] = {}
            if content:
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        data = parsed
                except Exception:
                    data = {"action": "abort_and_restart", "raw_content": content}
            else:
                data = {"action": "abort_and_restart"}

            data["_signal_path"] = flag_path
            data.setdefault("node_name", resolved_node)
            data.setdefault("batch_id", "unknown")
            return data
        except Exception:
            continue

    return None


def acknowledge_abort_signal(abort_data: Dict[str, Any]) -> bool:
    """Acknowledge and clear the abort signal file to prevent re-triggering.

    Args:
        abort_data: Abort signal metadata dict previously returned by check_abort_signal.

    Returns:
        True if acknowledgment succeeded, False otherwise.
    """
    if not isinstance(abort_data, dict):
        return False

    sig_path = abort_data.get("_signal_path")
    node_name = str(abort_data.get("node_name", os.environ.get("COMPUTERNAME", "unknown"))).lower()
    batch_id = abort_data.get("batch_id", "unknown")

    ack_written = False
    if sig_path and os.path.isfile(sig_path):
        if sig_path.endswith(ABORT_SIGNALS_JSON):
            try:
                with open(sig_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                key = abort_data.get("_json_key") or node_name
                if key in data:
                    del data[key]
                elif "signals" in data and key in data["signals"]:
                    del data["signals"][key]
                with open(sig_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                ack_written = True
            except Exception:
                pass
        elif sig_path.endswith(".abort"):
            ack_path = os.path.splitext(sig_path)[0] + ".abort_ack"
            ack_payload = {
                "action": "aborted",
                "node_name": node_name,
                "batch_id": batch_id,
                "ack_timestamp": datetime.now(timezone.utc).isoformat(),
            }
            try:
                with open(ack_path, "w", encoding="utf-8") as af:
                    json.dump(ack_payload, af, indent=2)
                ack_written = True
            except Exception:
                pass
            try:
                os.remove(sig_path)
            except Exception:
                pass
        else:
            try:
                os.remove(sig_path)
                ack_written = True
            except Exception:
                pass

    # Clean any local .abort_pending file as well
    temp_abort = os.path.join(tempfile.gettempdir(), ABORT_SIGNAL_FILENAME)
    if os.path.isfile(temp_abort):
        try:
            os.remove(temp_abort)
        except Exception:
            pass

    node_temp_abort = os.path.join(tempfile.gettempdir(), f"{ABORT_SIGNAL_FILENAME}_{node_name}")
    if os.path.isfile(node_temp_temp := node_temp_abort):
        try:
            os.remove(node_temp_temp)
        except Exception:
            pass

    return ack_written


def dispatch_abort_signal(
    node_name: str,
    batch_id: Optional[int] = None,
    shared_root: str = "",
    local_dir: str = "",
) -> str:
    """Dispatches an abort signal file for a target worker node.

    Args:
        node_name: Target worker node hostname.
        batch_id: Optional Metashape batch ID being aborted.
        shared_root: Shared network storage root containing .cluster directory.
        local_dir: Optional local directory to write .abort_pending.

    Returns:
        Absolute path to the created signal file.
    """
    clean_node = str(node_name).strip().lower()
    payload = {
        "action": "abort_and_restart",
        "node_name": clean_node,
        "batch_id": batch_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if shared_root:
        aborts_dir = os.path.join(shared_root, ".cluster", ABORT_DIR_NAME)
        os.makedirs(aborts_dir, exist_ok=True)
        sig_path = os.path.join(aborts_dir, f"{clean_node}.abort")
        with open(sig_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        # Also update aggregate abort_signals.json
        agg_path = os.path.join(shared_root, ".cluster", ABORT_SIGNALS_JSON)
        try:
            agg_data: Dict[str, Any] = {}
            if os.path.isfile(agg_path):
                with open(agg_path, "r", encoding="utf-8") as rf:
                    agg_data = json.load(rf)
            agg_data[clean_node] = payload
            with open(agg_path, "w", encoding="utf-8") as wf:
                json.dump(agg_data, wf, indent=2)
        except Exception:
            pass

        return sig_path

    out_dir = local_dir or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    sig_path = os.path.join(out_dir, ABORT_SIGNAL_FILENAME)
    with open(sig_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return sig_path


def abort_remote_worker(
    node_name: str,
    batch_id: Optional[int] = None,
    shared_root: str = "",
    server_host: str = "127.0.0.1:5840",
    timeout: float = 15.0,
    poll_interval: float = 1.0,
    client: Any = None,
) -> Dict[str, Any]:
    """Remotely abort a stuck worker node, unfreeze the server state machine, and await self-healing restart.

    Args:
        node_name: Hostname or worker ID of the target worker PC.
        batch_id: Optional batch ID to unpause and abort.
        shared_root: Shared network storage UNC path.
        server_host: Cluster Coordinator Server host (default: 127.0.0.1:5840).
        timeout: Maximum seconds to wait for worker termination and reconnect (default: 15.0).
        poll_interval: Status polling frequency in seconds.
        client: Optional pre-connected Metashape NetworkClient instance.

    Returns:
        Dictionary detailing operation results, acknowledgment status, and reconnect state.
    """
    clean_node = str(node_name).strip().lower()
    result: Dict[str, Any] = {
        "success": False,
        "node_name": clean_node,
        "batch_id": batch_id,
        "acknowledged": False,
        "reconnected": False,
        "error": None,
    }

    # Step 1: Connect to Metashape Server
    if client is None and Metashape is not None:
        try:
            net_client = Metashape.NetworkClient()
            host = server_host
            port = 5840
            if ":" in server_host:
                parts = server_host.split(":", 1)
                host = parts[0]
                try:
                    port = int(parts[1])
                except ValueError:
                    pass
            net_client.connect(host, port)
            client = net_client
        except Exception as e:
            # Server connection is desirable but optional for signal file dispatch
            result["error"] = f"Warning: Could not connect to Metashape server ({e})"

    # Step 2 & 3: Unpause batch if paused, abort batch, and demote priority
    if client is not None and batch_id is not None:
        try:
            # Check if batch is paused
            batch_list_fn = getattr(client, "batchList", None)
            if callable(batch_list_fn):
                bl = batch_list_fn()
                if isinstance(bl, dict):
                    for b in bl.get("batches", []):
                        if b.get("batch_id") == batch_id:
                            is_paused = bool(b.get("paused", False)) or str(b.get("state", "")).lower() == "paused"
                            if is_paused and hasattr(client, "setBatchPaused"):
                                client.setBatchPaused(batch_id, False)
                            break

            # Abort batch
            if hasattr(client, "abortBatch"):
                client.abortBatch(batch_id)

            # Demote priority to prevent immediate re-dispatch
            if hasattr(client, "setBatchPriority"):
                try:
                    client.setBatchPriority(batch_id, -100)
                except Exception:
                    pass
        except Exception as batch_err:
            if not result["error"]:
                result["error"] = f"Warning during batch abort: {batch_err}"

    # Step 4: Write abort signal file
    sig_path = ""
    try:
        sig_path = dispatch_abort_signal(
            node_name=clean_node,
            batch_id=batch_id,
            shared_root=shared_root,
        )
    except Exception as sig_err:
        result["error"] = f"Failed to write abort signal file: {sig_err}"
        return result

    # Step 5: Wait up to timeout for supervisor acknowledgment and reconnect
    aborts_dir = os.path.join(shared_root, ".cluster", ABORT_DIR_NAME) if shared_root else ""
    ack_path = os.path.join(aborts_dir, f"{clean_node}.abort_ack") if aborts_dir else ""

    start_t = time.time()
    ack_seen = False
    reconnected = False

    while time.time() - start_t < timeout:
        # Check acknowledgment: ack file created or signal file removed
        if ack_path and os.path.isfile(ack_path):
            ack_seen = True
        elif sig_path and not os.path.isfile(sig_path):
            ack_seen = True

        # Check worker list for reconnection / ready state
        if client is not None and hasattr(client, "workerList"):
            try:
                w_info = client.workerList()
                if isinstance(w_info, dict):
                    for w in w_info.get("workers", []):
                        w_hname = str(w.get("hostname", "")).strip().lower()
                        w_state = str(w.get("state", "")).strip().lower()
                        if w_hname == clean_node and w_state == "ready":
                            reconnected = True
                            break
            except Exception:
                pass

        if ack_seen and (reconnected or client is None):
            break

        time.sleep(poll_interval)

    # Clean up ack file if present to leave clean state
    if ack_path and os.path.isfile(ack_path):
        try:
            os.remove(ack_path)
        except Exception:
            pass

    result["acknowledged"] = ack_seen
    result["reconnected"] = reconnected
    result["success"] = ack_seen or reconnected
    return result
