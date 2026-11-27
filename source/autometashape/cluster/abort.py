# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Autonomous signal-based worker abort, self-healing restart, and lock cleanup."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    import Metashape
except ImportError:
    Metashape = None

from autometashape.cluster.hardware import (
    _is_fixed_local_drive_fast,
    get_local_hostname,
    is_healthy_online_worker,
)

ABORT_SIGNAL_FILENAME = ".abort_pending"
ABORT_DIR_NAME = "aborts"
ABORT_SIGNALS_JSON = "abort_signals.json"


_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260
_IGNORED_LOCK_SCAN_DIRS = frozenset({".git", ".venv", ".agents", "node_modules", "__pycache__", ".cluster"})


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * _MAX_PATH),
    ]


def _split_host_port(server_host: str, default_port: int = 5840) -> tuple[str, int]:
    """Parse ``host[:port]`` or ``[ipv6]:port`` cleanly without splitting IPv6 colons."""
    raw = str(server_host or "127.0.0.1").strip()
    if raw.startswith("[") and "]" in raw:
        end = raw.index("]")
        host = raw[1:end]
        rest = raw[end + 1 :]
        if rest.startswith(":") and rest[1:].isdigit():
            return host, int(rest[1:])
        return host, default_port
    if raw.count(":") == 1:
        h, p = raw.rsplit(":", 1)
        if p.isdigit():
            return h, int(p)
    return raw, default_port


def _atomic_write_json(target_path: str, payload: Any, retries: int = 3) -> None:
    """Atomically write JSON with PID+TID temp path, SMB retry, and orphan cleanup."""
    tmp_path = f"{target_path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        for attempt in range(retries):
            try:
                os.replace(tmp_path, target_path)
                return
            except (PermissionError, OSError):
                if attempt >= retries - 1:
                    raise
                time.sleep(0.15 * (attempt + 1))
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def _collect_descendant_pids_from_edges(root_pid: int, parent_to_children: Dict[int, List[int]]) -> List[int]:
    """Return descendant PIDs of ``root_pid`` in leaves-first order (excluding ``root_pid``)."""
    visited: set[int] = {root_pid}
    bfs_order: List[int] = []
    queue: deque[int] = deque([root_pid])
    while queue:
        curr = queue.popleft()
        for child in parent_to_children.get(curr, ()):
            if child not in visited and child > 0:
                visited.add(child)
                bfs_order.append(child)
                queue.append(child)
    bfs_order.reverse()
    return bfs_order


def _enumerate_windows_descendant_pids(root_pid: int) -> List[int]:
    """Snapshot all live Windows descendant PIDs of ``root_pid`` (leaves first).

    Works even if ``root_pid`` has already exited because Windows preserves
    ``th32ParentProcessID`` on orphaned child processes (such as Metashape's
    ``--worker --dispatch stdio`` child process that inherits the worker TCP socket).
    """
    if sys.platform != "win32" or not root_pid or root_pid <= 0:
        return []

    try:
        windll = getattr(ctypes, "windll", None)
        kernel32 = getattr(windll, "kernel32", None)
        if kernel32 is None:
            return []

        create_snap = kernel32.CreateToolhelp32Snapshot
        create_snap.restype = ctypes.c_void_p
        create_snap.argtypes = [ctypes.c_uint32, ctypes.c_uint32]

        proc_first = kernel32.Process32FirstW
        proc_first.restype = ctypes.c_int
        proc_first.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]

        proc_next = kernel32.Process32NextW
        proc_next.restype = ctypes.c_int
        proc_next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]

        invalid_handle = ctypes.c_void_p(-1).value
        snap = create_snap(_TH32CS_SNAPPROCESS, 0)
        if not isinstance(snap, int) or snap in (0, invalid_handle):
            return []

        parent_to_children: Dict[int, List[int]] = {}
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = proc_first(snap, ctypes.byref(entry))
            while ok:
                pid_val = int(entry.th32ProcessID)
                ppid_val = int(entry.th32ParentProcessID)
                parent_to_children.setdefault(ppid_val, []).append(pid_val)
                ok = proc_next(snap, ctypes.byref(entry))
        finally:
            close_handle(snap)

        return _collect_descendant_pids_from_edges(int(root_pid), parent_to_children)
    except Exception:
        return []


def cleanup_stale_worker_processes(target_pid: Optional[int] = None) -> None:
    """Safely terminate targeted orphaned metashape worker processes on this host."""
    if sys.platform == "win32" and target_pid:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(target_pid)], capture_output=True, timeout=2)
        except Exception:
            pass


def kill_worker_process(proc: Any, timeout: float = 2.0) -> bool:
    """Forcibly and deterministically terminate the worker process and its entire process tree.

    On Windows, ``taskkill /F /T /PID`` MUST run BEFORE ``proc.terminate()``: calling
    ``proc.terminate()`` first kills only the parent daemon, breaking Windows process-tree
    traversal so ``--worker --dispatch stdio`` child processes survive as orphans holding
    the inherited TCP socket open on the cluster server.

    Args:
        proc: subprocess.Popen instance representing the worker process.
        timeout: Maximum seconds to wait for graceful termination before force killing.

    Returns:
        True if process terminated successfully, False otherwise.
    """
    if proc is None:
        return True

    proc_pid = getattr(proc, "pid", None)
    poll_fn = getattr(proc, "poll", None)
    terminate_fn = getattr(proc, "terminate", None)

    if proc_pid and sys.platform == "win32":
        # 1. Snapshot all descendant PIDs while parent is still alive (or even if already dead)
        descendants = _enumerate_windows_descendant_pids(int(proc_pid))

        # 2. Tree-kill the parent BEFORE proc.terminate() so Windows kills parent + children together
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc_pid)],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass

        # 3. Reap any captured descendant PIDs explicitly (defense-in-depth against detached children)
        for child_pid in descendants:
            if child_pid != proc_pid:
                cleanup_stale_worker_processes(child_pid)

        # 4. Fallback terminate/wait if handle still reports alive (or in unit-test mocks)
        still_running = not callable(poll_fn) or poll_fn() is None
        if still_running and callable(terminate_fn):
            try:
                terminate_fn()
                wait_deadline = time.time() + timeout
                while time.time() < wait_deadline:
                    if callable(poll_fn) and poll_fn() is not None:
                        break
                    time.sleep(0.05)
            except Exception:
                pass
        wait_fn = getattr(proc, "wait", None)
        if callable(wait_fn):
            try:
                wait_fn(timeout=1.0)
            except Exception:
                pass
        return True

    # Non-Windows (or missing PID): graceful terminate (and dedicated process group if isolated) followed by force kill
    exited_gracefully = False
    if proc_pid and hasattr(os, "getpgid") and hasattr(os, "killpg"):
        try:
            pgid = os.getpgid(int(proc_pid))
            if pgid == int(proc_pid) and pgid != os.getpgid(0):
                os.killpg(pgid, signal.SIGTERM)
        except Exception:
            pass

    if callable(terminate_fn):
        try:
            terminate_fn()
            wait_deadline = time.time() + timeout
            while time.time() < wait_deadline:
                if callable(poll_fn) and poll_fn() is not None:
                    exited_gracefully = True
                    break
                time.sleep(0.05)
        except Exception:
            pass

    if not exited_gracefully and callable(poll_fn) and poll_fn() is None:
        if proc_pid and hasattr(os, "getpgid") and hasattr(os, "killpg") and hasattr(signal, "SIGKILL"):
            try:
                pgid = os.getpgid(int(proc_pid))
                if pgid == int(proc_pid) and pgid != os.getpgid(0):
                    os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
        kill_fn = getattr(proc, "kill", None)
        if callable(kill_fn):
            try:
                kill_fn()
            except Exception:
                pass

    wait_fn = getattr(proc, "wait", None)
    if callable(wait_fn):
        try:
            wait_fn(timeout=1.0)
        except Exception:
            pass

    return True


def cleanup_scratch_locks(target_dir: str = "", root_path: str = "", max_depth: int = 3) -> List[str]:
    """Remove any stale lock files in target_dir, temp dir, or local scratch staging dirs.

    Bounded to max_depth to prevent expensive recursive scans of large project or scratch trees.

    Args:
        target_dir: Active project target directory to inspect.
        root_path: Shared network storage root containing .cluster maintenance files.
        max_depth: Maximum directory recursion depth (default: 3).

    Returns:
        List of paths of removed lock files.
    """
    removed_locks: List[str] = []
    lock_dirs: List[str] = []

    if target_dir and os.path.isdir(target_dir):
        lock_dirs.append(target_dir)

    # Local scratch directories across fixed local drives only (never touch mapped network shares or shared .cluster/)
    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            if not _is_fixed_local_drive_fast(letter):
                continue
            scratch_cand = rf"{letter}:\AutoMetashape_Scratch"
            if os.path.isdir(scratch_cand):
                lock_dirs.append(scratch_cand)
    else:
        for cand in [
            "/mnt/scratch/AutoMetashape_Scratch",
            "/var/tmp/AutoMetashape_Scratch",
            "/tmp/AutoMetashape_Scratch",
            os.path.expanduser("~/AutoMetashape_Scratch"),
        ]:
            if os.path.isdir(cand):
                lock_dirs.append(cand)

    temp_scratch = os.path.join(tempfile.gettempdir(), "AutoMetashape_Scratch")
    if os.path.isdir(temp_scratch):
        lock_dirs.append(temp_scratch)

    for d in lock_dirs:
        if not os.path.isdir(d):
            continue
        try:
            base_depth = d.rstrip(r"\/").count(os.sep)
            for root, dirs, files in os.walk(d):
                dirs[:] = [sub for sub in dirs if sub.lower() not in _IGNORED_LOCK_SCAN_DIRS]
                cur_depth = root.rstrip(r"\/").count(os.sep) - base_depth
                if cur_depth >= max_depth:
                    dirs.clear()  # Prune deeper traversal to avoid massive I/O
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

    Optimized for zero network contention: inspects local fast paths first, then
    direct per-node signal files, with shared aggregate JSON checked only as a fallback.

    Args:
        target_dir: Active worker working directory.
        root_path: Shared network storage UNC path.
        node_name: Current host node name (default: COMPUTERNAME or 'unknown').

    Returns:
        Dictionary containing abort signal metadata and _signal_path, or None.
    """
    resolved_node = (node_name or get_local_hostname()).strip().lower()
    temp_dir = tempfile.gettempdir()

    # 1. Fast local checks (Zero network I/O)
    candidates: List[str] = []
    if target_dir:
        candidates.append(os.path.join(target_dir, ABORT_SIGNAL_FILENAME))
    candidates.append(os.path.join(temp_dir, ABORT_SIGNAL_FILENAME))
    if resolved_node and resolved_node != "unknown":
        candidates.append(os.path.join(temp_dir, f"{ABORT_SIGNAL_FILENAME}_{resolved_node}"))

    # 2. UNC Direct per-node file checks (Atomic single stat, zero contention)
    if root_path and os.path.isdir(root_path):
        aborts_dir = os.path.join(root_path, ".cluster", ABORT_DIR_NAME)
        if os.path.isdir(aborts_dir):
            if resolved_node and resolved_node != "unknown":
                candidates.append(os.path.join(aborts_dir, f"{resolved_node}.abort"))
            candidates.append(os.path.join(aborts_dir, "all.abort"))

    for flag_path in candidates:
        if not os.path.isfile(flag_path):
            continue
        # For broadcast all.abort, skip if this node already wrote a newer per-node .abort_ack
        if os.path.basename(flag_path).lower() == "all.abort" and resolved_node and resolved_node != "unknown":
            node_ack = os.path.join(os.path.dirname(flag_path), f"{resolved_node}.abort_ack")
            if os.path.isfile(node_ack):
                try:
                    if os.path.getmtime(node_ack) >= os.path.getmtime(flag_path):
                        continue
                except OSError:
                    pass
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

            # Guard against wrong-node execution: if a signal specifies a target node_name, verify it matches us
            sig_target = str(data.get("node_name") or "").strip().lower()
            if sig_target and sig_target not in (resolved_node, "all", "unknown") and resolved_node != "unknown":
                continue

            data["_signal_path"] = flag_path
            data.setdefault("node_name", resolved_node)
            data.setdefault("batch_id", "unknown")
            return data
        except Exception:
            continue

    # 3. Fallback: Shared aggregate json (Only read if no direct signal file was found)
    if root_path and os.path.isdir(root_path):
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
    node_name = str(abort_data.get("node_name", get_local_hostname())).lower()
    batch_id = abort_data.get("batch_id", "unknown")

    ack_written = False
    if sig_path and os.path.isfile(sig_path):
        if sig_path.endswith(ABORT_SIGNALS_JSON):
            try:
                with open(sig_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                key = abort_data.get("_json_key") or node_name
                if isinstance(data, dict):
                    if key in data and key != "all":
                        del data[key]
                    elif (
                        "signals" in data
                        and isinstance(data["signals"], dict)
                        and key in data["signals"]
                        and key != "all"
                    ):
                        del data["signals"][key]
                    _atomic_write_json(sig_path, data)
                ack_written = True
            except Exception:
                pass
        elif sig_path.endswith(".abort"):
            is_broadcast = os.path.basename(sig_path).lower() == "all.abort"
            ack_base = (
                os.path.join(os.path.dirname(sig_path), node_name)
                if (is_broadcast and node_name and node_name != "unknown")
                else os.path.splitext(sig_path)[0]
            )
            ack_path = ack_base + ".abort_ack"
            ack_payload = {
                "action": "aborted",
                "node_name": node_name,
                "batch_id": batch_id,
                "ack_timestamp": datetime.now(timezone.utc).isoformat(),
            }
            try:
                _atomic_write_json(ack_path, ack_payload)
                ack_written = True
            except Exception:
                pass
            # Never delete broadcast all.abort so sibling workers also receive the broadcast
            if not is_broadcast:
                try:
                    os.remove(sig_path)
                except Exception:
                    pass
            # Also purge matching node entry from sibling .cluster/abort_signals.json
            # to prevent the watchdog from firing a second kill on the next poll.
            parent_dir = os.path.dirname(sig_path)
            cluster_dir = (
                os.path.dirname(parent_dir) if os.path.basename(parent_dir).lower() == ABORT_DIR_NAME else parent_dir
            )
            agg_json = os.path.join(cluster_dir, ABORT_SIGNALS_JSON)
            if os.path.isfile(agg_json) and not is_broadcast:
                try:
                    with open(agg_json, "r", encoding="utf-8") as rf:
                        agg_data = json.load(rf)
                    changed = False
                    if isinstance(agg_data, dict):
                        if node_name in agg_data:
                            del agg_data[node_name]
                            changed = True
                        if "signals" in agg_data and isinstance(agg_data["signals"], dict):
                            if node_name in agg_data["signals"]:
                                del agg_data["signals"][node_name]
                                changed = True
                    if changed:
                        _atomic_write_json(agg_json, agg_data)
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
    if os.path.isfile(node_temp_abort):
        try:
            os.remove(node_temp_abort)
        except Exception:
            pass

    return ack_written


def dispatch_abort_signal(
    node_name: str,
    batch_id: Optional[int] = None,
    shared_root: str = "",
    local_dir: str = "",
) -> str:
    """Dispatches an abort signal file for a target worker node using atomic replace.

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
        # Clear any stale acknowledgment from an earlier abort before dispatching the new signal
        stale_ack = os.path.join(aborts_dir, f"{clean_node}.abort_ack")
        if os.path.isfile(stale_ack):
            try:
                os.remove(stale_ack)
            except OSError:
                pass

        # Write aggregate abort_signals.json FIRST so a worker acknowledging <node>.abort
        # always sees and clears the aggregate entry cleanly instead of triggering a second kill.
        agg_path = os.path.join(shared_root, ".cluster", ABORT_SIGNALS_JSON)
        try:
            agg_data: Dict[str, Any] = {}
            if os.path.isfile(agg_path):
                with open(agg_path, "r", encoding="utf-8") as rf:
                    loaded = json.load(rf)
                if isinstance(loaded, dict):
                    agg_data = loaded
            agg_data[clean_node] = payload
            _atomic_write_json(agg_path, agg_data)
        except Exception:
            pass

        sig_path = os.path.join(aborts_dir, f"{clean_node}.abort")
        _atomic_write_json(sig_path, payload)
        return sig_path

    out_dir = local_dir or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    sig_path = os.path.join(out_dir, ABORT_SIGNAL_FILENAME)
    _atomic_write_json(sig_path, payload)
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

    owned_client = False
    try:
        # Step 1: Connect to Metashape Server (direct or via metashape.exe -r runner)
        if client is None and Metashape is not None:
            try:
                net_client = Metashape.NetworkClient()
                host, port = _split_host_port(server_host)
                net_client.connect(host, port)
                client = net_client
                owned_client = True
            except Exception as e:
                # Server connection is desirable but optional for signal file dispatch
                result["error"] = f"Warning: Could not connect to Metashape server ({e})"
        elif client is None and Metashape is None and batch_id is not None:
            # Fallback for plain Python: unpause & abort batch on server via metashape.exe -r helper
            # Pass host, port, and batch_id strictly via sys.argv (zero code interpolation).
            try:
                from autometashape.cluster.hardware import find_metashape_path

                ms_exe = find_metashape_path()
                if ms_exe and os.path.isfile(ms_exe):
                    host_str, port_num = _split_host_port(server_host)
                    helper_code = (
                        "import Metashape, sys\n"
                        "host = sys.argv[1]\n"
                        "port = int(sys.argv[2])\n"
                        "bid = int(sys.argv[3])\n"
                        "c = Metashape.NetworkClient()\n"
                        "try:\n"
                        "    c.connect(host, port)\n"
                        "    bl = c.batchList()\n"
                        "    for b in (bl.get('batches', []) if isinstance(bl, dict) else []):\n"
                        "        if b.get('batch_id') == bid:\n"
                        "            if b.get('paused') or str(b.get('state', '')).lower() == 'paused':\n"
                        "                c.setBatchPaused(bid, False)\n"
                        "            break\n"
                        "    c.abortBatch(bid)\n"
                        "    try:\n"
                        "        c.setBatchPriority(bid, -100)\n"
                        "    except Exception:\n"
                        "        pass\n"
                        "finally:\n"
                        "    try:\n"
                        "        c.disconnect()\n"
                        "    except Exception:\n"
                        "        pass\n"
                    )
                    fd, tmp_helper = tempfile.mkstemp(prefix="_ms_abort_", suffix=".py")
                    with os.fdopen(fd, "w", encoding="utf-8") as hf:
                        hf.write(helper_code)
                    try:
                        cmd = [ms_exe]
                        env = os.environ.copy()
                        if sys.platform.startswith("linux") and not env.get("DISPLAY"):
                            cmd.extend(["-platform", "offscreen"])
                            env.setdefault("QT_QPA_PLATFORM", "offscreen")
                        cmd.extend(["-r", tmp_helper, host_str, str(port_num), str(int(batch_id))])
                        proc_res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=15)
                        if proc_res.returncode != 0:
                            err_tail = (proc_res.stderr or proc_res.stdout or "").strip()
                            result["error"] = f"Warning: fallback server abort exited {proc_res.returncode}: {err_tail}"
                    finally:
                        if os.path.isfile(tmp_helper):
                            try:
                                os.remove(tmp_helper)
                            except Exception:
                                pass
            except Exception as helper_err:
                result["error"] = f"Warning during fallback server abort: {helper_err}"

        # Step 2 & 3: Unpause batch if paused, abort batch, and demote priority
        if client is not None and batch_id is not None:
            try:
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

                if hasattr(client, "abortBatch"):
                    client.abortBatch(batch_id)

                if hasattr(client, "setBatchPriority"):
                    try:
                        client.setBatchPriority(batch_id, -100)
                    except Exception:
                        pass
            except Exception as batch_err:
                if not result["error"]:
                    result["error"] = f"Warning during batch abort: {batch_err}"

        # Record pre-abort worker IDs for target node so we only count a genuinely fresh reconnect
        pre_abort_worker_ids: set[Any] = set()
        if client is not None and hasattr(client, "workerList"):
            try:
                w_pre = client.workerList()
                if isinstance(w_pre, dict):
                    for w in w_pre.get("workers", []):
                        if not is_healthy_online_worker(w):
                            continue
                        if str(w.get("hostname", "")).strip().lower() == clean_node:
                            wid = w.get("worker_id")
                            if wid is not None:
                                pre_abort_worker_ids.add(wid)
            except Exception:
                pass

        # Step 4: Write abort signal file atomically
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
            if ack_path and os.path.isfile(ack_path):
                ack_seen = True
            elif sig_path and not os.path.isfile(sig_path):
                ack_seen = True

            if client is not None and hasattr(client, "workerList"):
                try:
                    w_info = client.workerList()
                    if isinstance(w_info, dict):
                        for w in w_info.get("workers", []):
                            if not is_healthy_online_worker(w):
                                continue
                            w_hname = str(w.get("hostname", "")).strip().lower()
                            w_state = str(w.get("state", "")).strip().lower()
                            w_id = w.get("worker_id")
                            is_fresh_worker = (w_id is None) or (w_id not in pre_abort_worker_ids)
                            if w_hname == clean_node and w_state == "ready" and is_fresh_worker:
                                reconnected = True
                                break
                except Exception:
                    pass

            if ack_seen and (reconnected or client is None):
                break

            time.sleep(poll_interval)

        if ack_path and os.path.isfile(ack_path):
            try:
                os.remove(ack_path)
            except Exception:
                pass

        result["acknowledged"] = ack_seen
        result["reconnected"] = reconnected
        result["success"] = ack_seen or reconnected
        return result
    finally:
        if owned_client and client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
