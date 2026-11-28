# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Autonomous signal-based worker abort, self-healing restart, and lock cleanup."""

from __future__ import annotations

import ctypes
import ipaddress
import json
import os
import re
import signal
import string
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

try:
    import Metashape
except ImportError:
    Metashape = None

from autometashape.cluster.hardware import (
    _is_fixed_local_drive_fast,
    connect_network_client,
    get_local_hostname,
    is_healthy_online_worker,
    normalize_rpc_list_response,
    split_host_port,
)

ABORT_SIGNAL_FILENAME = ".abort_pending"
ABORT_DIR_NAME = "aborts"
ABORT_SIGNALS_JSON = "abort_signals.json"
DEFAULT_ABORT_SIGNAL_TTL_S = 3600.0

_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260
_IGNORED_LOCK_SCAN_DIRS = frozenset({".git", ".venv", ".agents", "node_modules", "__pycache__", ".cluster"})
_PROTECTED_PACKAGE_LOCKS = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "cargo.lock",
        "pdm.lock",
        "pipfile.lock",
        "yarn.lock",
        "composer.lock",
        "gemfile.lock",
        "flake.lock",
        "bun.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
    }
)
_STAGE_DIR_PID_RE = re.compile(r"_(\d+)_local_stage$", re.IGNORECASE)


def _sanitize_node_filename(node_name: str) -> str:
    """Sanitize a node identifier so it cannot escape ``aborts/`` or contain invalid filename chars."""
    raw = str(node_name or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9._-]+", "_", raw)
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip("._-")
    return cleaned or "unknown"


def _is_ip_literal(value: str) -> bool:
    """Return True if ``value`` is a valid IPv4 or IPv6 literal address."""
    try:
        ipaddress.ip_address(str(value or "").strip().strip("[]"))
        return True
    except ValueError:
        return False


def _short_hostname(value: str) -> str:
    """Return short DNS label for ``value``, keeping IPv4/IPv6 literals intact."""
    clean = str(value or "").strip().lower()
    if not clean or _is_ip_literal(clean):
        return clean
    return clean.split(".", 1)[0]


def _hostnames_match(a: str, b: str) -> bool:
    """Match node hostnames across FQDN/short-name and localhost aliases (IPv4/IPv6 safe)."""
    na = str(a or "").strip().lower().strip("[]")
    nb = str(b or "").strip().lower().strip("[]")
    if not na or not nb:
        return False
    if na == nb:
        return True
    na_is_ip = _is_ip_literal(na)
    nb_is_ip = _is_ip_literal(nb)
    if not na_is_ip and not nb_is_ip:
        sa = na.split(".", 1)[0]
        sb = nb.split(".", 1)[0]
        if sa and sa == sb:
            return True
    loopback = ("localhost", "127.0.0.1", "::1")
    if na in loopback or nb in loopback:
        local = str(get_local_hostname() or "").strip().lower()
        local_is_ip = _is_ip_literal(local)
        local_short = "" if (not local or local_is_ip) else local.split(".", 1)[0]
        other = nb if na in loopback else na
        other_is_ip = _is_ip_literal(other)
        other_short = "" if (not other or other_is_ip) else other.split(".", 1)[0]
        if other in loopback:
            return True
        return other in (local, local_short) or (
            bool(other_short) and not other_is_ip and other_short in (local, local_short)
        )
    return False


def _parse_iso_timestamp(value: Any) -> Optional[float]:
    """Parse float epoch or ISO-8601 UTC timestamp string safely under Python 3.9+."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    txt = str(value).strip()
    if not txt:
        return None
    try:
        return float(txt)
    except ValueError:
        pass
    try:
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _is_signal_expired(data: Dict[str, Any], ttl_s: float = DEFAULT_ABORT_SIGNAL_TTL_S) -> bool:
    """Return True if an abort signal payload has passed its explicit expiry or TTL."""
    now_ts = time.time()
    exp_ts = _parse_iso_timestamp(data.get("expires_at"))
    if exp_ts is not None:
        return now_ts > exp_ts
    sig_ts = _parse_iso_timestamp(data.get("timestamp"))
    if sig_ts is not None and ttl_s > 0:
        return (now_ts - sig_ts) > ttl_s
    return False


def _node_ack_candidates(aborts_dir: str, resolved_node: str) -> List[str]:
    """Return ordered unique per-node ack candidate paths (``.broadcast_ack`` and ``.abort_ack``)."""
    clean_node = str(resolved_node or "").strip().lower()
    is_ip = _is_ip_literal(clean_node)
    safe_node = _sanitize_node_filename(clean_node)
    short_node = safe_node if is_ip else _sanitize_node_filename(clean_node.split(".", 1)[0])
    paths: List[str] = []
    for name in (safe_node, short_node):
        if name and name != "unknown":
            for ext in (".broadcast_ack", ".abort_ack"):
                cand = os.path.join(aborts_dir, f"{name}{ext}")
                if cand not in paths:
                    paths.append(cand)
    if not is_ip and short_node and short_node != "unknown" and os.path.isdir(aborts_dir):
        prefix = f"{short_node}."
        try:
            for fname in sorted(os.listdir(aborts_dir)):
                low = fname.lower()
                if low.startswith(prefix) and low.endswith((".broadcast_ack", ".abort_ack")):
                    cand = os.path.join(aborts_dir, fname)
                    if cand not in paths:
                        paths.append(cand)
        except OSError:
            pass
    return paths


def _is_broadcast_already_acked(
    aborts_dir: str,
    resolved_node: str,
    sig_data: Dict[str, Any],
    sig_mtime: Optional[float] = None,
) -> bool:
    """Check whether this node already acknowledged a broadcast signal via signal_id or ack mtime."""
    if not aborts_dir or not resolved_node or resolved_node == "unknown":
        return False
    sig_id = str(sig_data.get("signal_id") or "").strip()
    for ack_path in _node_ack_candidates(aborts_dir, resolved_node):
        if not os.path.isfile(ack_path):
            continue
        try:
            with open(ack_path, "r", encoding="utf-8") as af:
                ack_json = json.load(af)
            if isinstance(ack_json, dict):
                acked_ids: set[str] = set()
                if ack_json.get("signal_id"):
                    acked_ids.add(str(ack_json["signal_id"]))
                for sid in ack_json.get("acked_signal_ids") or []:
                    if sid:
                        acked_ids.add(str(sid))
                if sig_id and sig_id in acked_ids:
                    return True
        except Exception:
            pass
        if not sig_id and sig_mtime is not None:
            try:
                if os.path.getmtime(ack_path) >= sig_mtime:
                    return True
            except OSError:
                pass
    return False


def _is_pid_alive(pid: int) -> bool:
    """Best-effort check whether local OS PID is currently active."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_uint32()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return int(exit_code.value) == 259
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


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

        # 4. Fallback terminate/wait + force-kill escalation if handle still reports alive
        exited_gracefully = False
        still_running = not callable(poll_fn) or poll_fn() is None
        if still_running and callable(terminate_fn):
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
        if still_running and not exited_gracefully and callable(poll_fn) and poll_fn() is None:
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

    # Non-Windows (or missing PID): graceful terminate followed by guaranteed process-group SIGKILL
    exited_gracefully = False
    captured_pgid: Optional[int] = None
    if proc_pid and hasattr(os, "getpgid") and hasattr(os, "killpg"):
        try:
            pgid = os.getpgid(int(proc_pid))
            if pgid == int(proc_pid) and pgid != os.getpgid(0):
                captured_pgid = pgid
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
        kill_fn = getattr(proc, "kill", None)
        if callable(kill_fn):
            try:
                kill_fn()
            except Exception:
                pass

    # Always sweep the isolated session process group with SIGKILL even if parent exited early
    if captured_pgid is not None and hasattr(os, "killpg") and hasattr(signal, "SIGKILL"):
        try:
            os.killpg(captured_pgid, signal.SIGKILL)
        except Exception:
            pass

    wait_fn = getattr(proc, "wait", None)
    if callable(wait_fn):
        try:
            wait_fn(timeout=1.0)
        except Exception:
            pass

    return True


def cleanup_scratch_locks(
    target_dir: str = "",
    root_path: str = "",
    max_depth: int = 3,
    worker_pid: Optional[int] = None,
    killed_pids: Optional[set[int]] = None,
) -> List[str]:
    """Remove stale Metashape lock files in scratch staging dirs or project ``.files`` dirs.

    Bounded to max_depth, skips protected package manager lockfiles (``uv.lock``,
    ``poetry.lock``, ``Cargo.lock``, etc.), and leaves live sibling workers' staging locks untouched.
    """
    removed_locks: List[str] = []
    lock_dirs: List[str] = []
    target_pids: set[int] = {int(p) for p in (killed_pids or ()) if p is not None and int(p) > 0}
    if worker_pid is not None and int(worker_pid) > 0:
        target_pids.add(int(worker_pid))

    if target_dir and os.path.isdir(target_dir):
        lock_dirs.append(target_dir)

    env_scratch = os.environ.get("AUTOMETASHAPE_SCRATCH_DIR", "").strip()
    if env_scratch and os.path.isdir(env_scratch) and env_scratch not in lock_dirs:
        lock_dirs.append(env_scratch)

    # Local scratch directories across fixed local drives only (never touch mapped network shares or shared .cluster/)
    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            if not _is_fixed_local_drive_fast(letter):
                continue
            scratch_cand = rf"{letter}:\AutoMetashape_Scratch"
            if os.path.isdir(scratch_cand) and scratch_cand not in lock_dirs:
                lock_dirs.append(scratch_cand)
    else:
        for cand in [
            "/mnt/scratch/AutoMetashape_Scratch",
            "/var/tmp/AutoMetashape_Scratch",
            "/tmp/AutoMetashape_Scratch",
            os.path.expanduser("~/AutoMetashape_Scratch"),
        ]:
            if os.path.isdir(cand) and cand not in lock_dirs:
                lock_dirs.append(cand)

    temp_scratch = os.path.join(tempfile.gettempdir(), "AutoMetashape_Scratch")
    if os.path.isdir(temp_scratch) and temp_scratch not in lock_dirs:
        lock_dirs.append(temp_scratch)

    for d in lock_dirs:
        if not os.path.isdir(d):
            continue
        is_repo_target = bool(
            target_dir
            and os.path.normcase(os.path.abspath(d)) == os.path.normcase(os.path.abspath(target_dir))
            and (os.path.isdir(os.path.join(d, ".git")) or os.path.isfile(os.path.join(d, "pyproject.toml")))
        )
        try:
            norm_base = os.path.normpath(d)
            base_depth = norm_base.rstrip(r"\/").count(os.sep)
            for root, dirs, files in os.walk(norm_base):
                kept_dirs: List[str] = []
                for sub in dirs:
                    if sub.lower() in _IGNORED_LOCK_SCAN_DIRS:
                        continue
                    m_stage = _STAGE_DIR_PID_RE.search(sub)
                    if m_stage:
                        stage_pid = int(m_stage.group(1))
                        if (target_pids and stage_pid not in target_pids) or _is_pid_alive(stage_pid):
                            continue
                    kept_dirs.append(sub)
                dirs[:] = kept_dirs

                norm_root = os.path.normpath(root)
                cur_depth = norm_root.rstrip(r"\/").count(os.sep) - base_depth
                if cur_depth >= max_depth:
                    dirs.clear()  # Prune deeper traversal to avoid massive I/O

                rel_root = os.path.relpath(norm_root, norm_base)
                rel_parts = [p.lower() for p in rel_root.replace("\\", "/").split("/") if p and p != "."]
                in_metashape_dir = any(
                    p.endswith(".files") or p == "scratch" or p.endswith("_local_stage") for p in rel_parts
                )
                for f in files:
                    low_f = f.lower()
                    if low_f in _PROTECTED_PACKAGE_LOCKS:
                        continue
                    if is_repo_target and not in_metashape_dir:
                        continue
                    if low_f == "lock" or low_f.endswith(".lock"):
                        lock_path = os.path.join(norm_root, f)
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
    Broadcast ``all.abort`` signals are deduplicated per-node by ``signal_id`` and ack mtime,
    and stale signals older than their TTL are skipped cleanly.
    """
    resolved_node = (node_name or get_local_hostname()).strip().lower()
    is_ip = _is_ip_literal(resolved_node)
    safe_node = _sanitize_node_filename(resolved_node)
    short_node = safe_node if is_ip else _sanitize_node_filename(resolved_node.split(".", 1)[0])
    node_aliases: List[str] = []
    for n in (resolved_node, safe_node, short_node):
        if n and n != "unknown" and n not in node_aliases:
            node_aliases.append(n)
    file_aliases: List[str] = []
    for n in (safe_node, short_node):
        if n and n != "unknown" and n not in file_aliases:
            file_aliases.append(n)

    temp_dir = tempfile.gettempdir()
    aborts_dir = os.path.join(root_path, ".cluster", ABORT_DIR_NAME) if root_path and os.path.isdir(root_path) else ""

    # 1. Fast local checks (Zero network I/O)
    candidates: List[str] = []
    if target_dir:
        candidates.append(os.path.join(target_dir, ABORT_SIGNAL_FILENAME))
    candidates.append(os.path.join(temp_dir, ABORT_SIGNAL_FILENAME))
    for alias in file_aliases:
        candidates.append(os.path.join(temp_dir, f"{ABORT_SIGNAL_FILENAME}_{alias}"))

    # 2. UNC Direct per-node file checks (exact aliases, then FQDN/short-name matches, then all.abort)
    if aborts_dir and os.path.isdir(aborts_dir):
        for alias in file_aliases:
            candidates.append(os.path.join(aborts_dir, f"{alias}.abort"))
        try:
            for fname in sorted(os.listdir(aborts_dir)):
                low_f = fname.lower()
                if not low_f.endswith(".abort") or low_f == "all.abort":
                    continue
                stem = low_f[:-6]
                if resolved_node != "unknown" and _hostnames_match(stem, resolved_node):
                    cand = os.path.join(aborts_dir, fname)
                    if cand not in candidates:
                        candidates.append(cand)
        except OSError:
            pass
        candidates.append(os.path.join(aborts_dir, "all.abort"))

    for flag_path in candidates:
        if not os.path.isfile(flag_path):
            continue
        is_broadcast = os.path.basename(flag_path).lower() == "all.abort"
        try:
            sig_mtime = os.path.getmtime(flag_path)
        except OSError:
            sig_mtime = None
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

            if _is_signal_expired(data):
                continue

            if is_broadcast and _is_broadcast_already_acked(
                os.path.dirname(flag_path), resolved_node, data, sig_mtime=sig_mtime
            ):
                continue

            # Guard against wrong-node execution: if a signal specifies a target node_name, verify it matches us
            sig_target = str(data.get("node_name") or "").strip().lower()
            if (
                sig_target
                and sig_target not in ("all", "unknown")
                and resolved_node != "unknown"
                and not _hostnames_match(sig_target, resolved_node)
            ):
                continue

            data["_signal_path"] = flag_path
            if is_broadcast or sig_target in ("", "all", "unknown"):
                if resolved_node and resolved_node != "unknown":
                    data["_target_node"] = sig_target or ("all" if is_broadcast else "")
                    data["node_name"] = resolved_node
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
                json_mtime = os.path.getmtime(json_sig)
                with open(json_sig, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    signals_sub = data.get("signals") if isinstance(data.get("signals"), dict) else None
                    matched_key: Optional[str] = None
                    entry: Any = None
                    for cand_key in [*node_aliases, "all"]:
                        if isinstance(data.get(cand_key), dict):
                            entry = data[cand_key]
                            matched_key = cand_key
                            break
                        if signals_sub and isinstance(signals_sub.get(cand_key), dict):
                            entry = signals_sub[cand_key]
                            matched_key = cand_key
                            break
                    if entry is None and resolved_node != "unknown":
                        for k_scan, v_scan in data.items():
                            is_host_entry = isinstance(v_scan, dict) and k_scan != "signals"
                            if is_host_entry and _hostnames_match(k_scan, resolved_node):
                                entry = v_scan
                                matched_key = k_scan
                                break
                        if entry is None and signals_sub:
                            for k_scan, v_scan in signals_sub.items():
                                if isinstance(v_scan, dict) and _hostnames_match(k_scan, resolved_node):
                                    entry = v_scan
                                    matched_key = k_scan
                                    break
                    if isinstance(entry, dict) and matched_key is not None:
                        if _is_signal_expired(entry):
                            return None
                        if matched_key == "all" and _is_broadcast_already_acked(
                            aborts_dir, resolved_node, entry, sig_mtime=json_mtime
                        ):
                            return None
                        entry_copy = dict(entry)
                        entry_copy["_signal_path"] = json_sig
                        entry_copy["_json_key"] = matched_key
                        if matched_key == "all" and resolved_node and resolved_node != "unknown":
                            entry_copy["_target_node"] = "all"
                            entry_copy["node_name"] = resolved_node
                        return entry_copy
            except Exception:
                pass

    return None


def _write_node_ack(
    ack_path: str,
    node_name: str,
    batch_id: Any,
    signal_id: Optional[str],
) -> bool:
    """Atomically write ``.abort_ack`` preserving recent acknowledged ``signal_id`` entries."""
    os.makedirs(os.path.dirname(os.path.abspath(ack_path)), exist_ok=True)
    prior_ids: List[str] = []
    if os.path.isfile(ack_path):
        try:
            with open(ack_path, "r", encoding="utf-8") as af:
                old_ack = json.load(af)
            if isinstance(old_ack, dict):
                prior_ids = [str(x) for x in (old_ack.get("acked_signal_ids") or []) if x]
                if old_ack.get("signal_id") and str(old_ack["signal_id"]) not in prior_ids:
                    prior_ids.append(str(old_ack["signal_id"]))
        except Exception:
            pass
    if signal_id and signal_id not in prior_ids:
        prior_ids.append(signal_id)
    ack_payload: Dict[str, Any] = {
        "action": "aborted",
        "node_name": node_name,
        "batch_id": batch_id,
        "signal_id": signal_id,
        "acked_signal_ids": prior_ids[-32:],
        "ack_timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _atomic_write_json(ack_path, ack_payload)
        return True
    except Exception:
        return False


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
    raw_node = str(abort_data.get("node_name") or "").strip().lower()
    if raw_node in ("", "all", "unknown"):
        raw_node = str(get_local_hostname() or "unknown").strip().lower()
    node_name = raw_node
    safe_node = _sanitize_node_filename(node_name)
    short_node = safe_node if _is_ip_literal(node_name) else _sanitize_node_filename(node_name.split(".", 1)[0])
    batch_id = abort_data.get("batch_id", "unknown")
    sig_id = str(abort_data.get("signal_id") or "").strip() or None

    ack_written = False
    if sig_path and os.path.isfile(sig_path):
        if sig_path.endswith(ABORT_SIGNALS_JSON):
            key = str(abort_data.get("_json_key") or node_name)
            if key == "all" and safe_node != "unknown":
                aborts_dir = os.path.join(os.path.dirname(sig_path), ABORT_DIR_NAME)
                ack_written = _write_node_ack(
                    os.path.join(aborts_dir, f"{safe_node}.abort_ack"),
                    node_name=node_name,
                    batch_id=batch_id,
                    signal_id=sig_id,
                )
                _write_node_ack(
                    os.path.join(aborts_dir, f"{safe_node}.broadcast_ack"),
                    node_name=node_name,
                    batch_id=batch_id,
                    signal_id=sig_id,
                )
            else:
                try:
                    with open(sig_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
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
                os.path.join(os.path.dirname(sig_path), safe_node)
                if (is_broadcast and safe_node and safe_node != "unknown")
                else os.path.splitext(sig_path)[0]
            )
            ack_path = ack_base + ".abort_ack"
            ack_written = _write_node_ack(ack_path, node_name=node_name, batch_id=batch_id, signal_id=sig_id)
            if is_broadcast and safe_node and safe_node != "unknown":
                _write_node_ack(
                    os.path.join(os.path.dirname(sig_path), f"{safe_node}.broadcast_ack"),
                    node_name=node_name,
                    batch_id=batch_id,
                    signal_id=sig_id,
                )
            # Never delete broadcast all.abort so sibling workers also receive the broadcast
            if not is_broadcast:
                try:
                    os.remove(sig_path)
                except Exception:
                    pass
            # Purge matching node entries from sibling .cluster/abort_signals.json
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
                        for k_purge in {node_name, safe_node, short_node}:
                            if k_purge in agg_data:
                                del agg_data[k_purge]
                                changed = True
                            if "signals" in agg_data and isinstance(agg_data["signals"], dict):
                                if k_purge in agg_data["signals"]:
                                    del agg_data["signals"][k_purge]
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

    for alias in {safe_node, short_node}:
        if not alias or alias == "unknown":
            continue
        node_temp_abort = os.path.join(tempfile.gettempdir(), f"{ABORT_SIGNAL_FILENAME}_{alias}")
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
        node_name: Target worker node hostname (or ``"all"`` for broadcast).
        batch_id: Optional Metashape batch ID being aborted.
        shared_root: Shared network storage root containing .cluster directory.
        local_dir: Optional local directory to write .abort_pending.

    Returns:
        Absolute path to the created signal file.
    """
    clean_node = str(node_name).strip().lower()
    safe_file_node = "all" if clean_node == "all" else _sanitize_node_filename(clean_node)
    now_utc = datetime.now(timezone.utc)
    payload = {
        "action": "abort_and_restart",
        "node_name": clean_node,
        "batch_id": batch_id,
        "signal_id": uuid.uuid4().hex,
        "timestamp": now_utc.isoformat(),
        "expires_at": (now_utc + timedelta(seconds=DEFAULT_ABORT_SIGNAL_TTL_S)).isoformat(),
    }

    if shared_root:
        aborts_dir = os.path.join(shared_root, ".cluster", ABORT_DIR_NAME)
        os.makedirs(aborts_dir, exist_ok=True)
        # Clear any stale acknowledgment from an earlier targeted abort before dispatching the new signal
        if clean_node != "all":
            stale_ack = os.path.join(aborts_dir, f"{safe_file_node}.abort_ack")
            if os.path.isfile(stale_ack):
                try:
                    os.remove(stale_ack)
                except OSError:
                    pass

        agg_path = os.path.join(shared_root, ".cluster", ABORT_SIGNALS_JSON)
        try:
            agg_data: Dict[str, Any] = {}
            if os.path.isfile(agg_path):
                with open(agg_path, "r", encoding="utf-8") as rf:
                    loaded = json.load(rf)
                if isinstance(loaded, dict):
                    agg_data = loaded
            if clean_node == "all":
                # Never leave an un-acknowledged "all" entry in abort_signals.json (would cause an infinite kill loop)
                changed_agg = False
                if "all" in agg_data:
                    del agg_data["all"]
                    changed_agg = True
                if isinstance(agg_data.get("signals"), dict) and "all" in agg_data["signals"]:
                    del agg_data["signals"]["all"]
                    changed_agg = True
                if changed_agg:
                    _atomic_write_json(agg_path, agg_data)
            else:
                agg_data[clean_node] = payload
                _atomic_write_json(agg_path, agg_data)
        except Exception:
            pass

        sig_path = os.path.join(aborts_dir, f"{safe_file_node}.abort")
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
    safe_node = _sanitize_node_filename(clean_node)
    result: Dict[str, Any] = {
        "success": False,
        "node_name": clean_node,
        "batch_id": batch_id,
        "signal_dispatched": False,
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
                host, port = split_host_port(server_host)
                connect_network_client(net_client, host, port)
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
                    host_str, port_num = split_host_port(server_host)
                    helper_code = (
                        "import Metashape, sys\n"
                        "host = sys.argv[1]\n"
                        "port = int(sys.argv[2])\n"
                        "bid = int(sys.argv[3])\n"
                        "c = Metashape.NetworkClient()\n"
                        "try:\n"
                        "    try:\n"
                        "        c.connect(host, port)\n"
                        "    except TypeError:\n"
                        "        hp = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'\n"
                        "        c.connect(hp)\n"
                        "    bl = c.batchList()\n"
                        "    batches = (bl.get('batches', []) if isinstance(bl, dict) "
                        "else (bl if isinstance(bl, (list, tuple)) else []))\n"
                        "    for b in batches:\n"
                        "        if isinstance(b, dict) and b.get('batch_id') == bid:\n"
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
                    for b in normalize_rpc_list_response(batch_list_fn(), "batches"):
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

        # Record pre-abort worker IDs & hostnames for target node (supports both targeted and broadcast "all")
        is_broadcast_abort = clean_node == "all"
        pre_abort_worker_ids: set[Any] = set()
        pre_abort_hosts: set[str] = set()
        if client is not None and hasattr(client, "workerList"):
            try:
                for w in normalize_rpc_list_response(client.workerList(), "workers"):
                    if not is_healthy_online_worker(w):
                        continue
                    w_hname = str(w.get("hostname", "")).strip().lower()
                    if is_broadcast_abort or _hostnames_match(w_hname, clean_node):
                        if w_hname:
                            pre_abort_hosts.add(w_hname)
                        wid = w.get("worker_id")
                        if wid is not None:
                            pre_abort_worker_ids.add(wid)
            except Exception:
                pass

        # Step 4: Write abort signal file atomically
        sig_path = ""
        dispatched_signal_id = ""
        try:
            sig_path = dispatch_abort_signal(
                node_name=clean_node,
                batch_id=batch_id,
                shared_root=shared_root,
            )
            result["signal_dispatched"] = True
            if sig_path and os.path.isfile(sig_path):
                try:
                    with open(sig_path, "r", encoding="utf-8") as sf:
                        dispatched_signal_id = str((json.load(sf) or {}).get("signal_id") or "").strip()
                except Exception:
                    dispatched_signal_id = ""
        except Exception as sig_err:
            result["error"] = f"Failed to write abort signal file: {sig_err}"
            return result

        # Step 5: Wait up to timeout for supervisor acknowledgment and reconnect
        aborts_dir = os.path.join(shared_root, ".cluster", ABORT_DIR_NAME) if shared_root else ""
        ack_path = os.path.join(aborts_dir, f"{safe_node}.abort_ack") if (aborts_dir and not is_broadcast_abort) else ""

        start_t = time.time()
        ack_seen = False
        reconnected = False

        while time.time() - start_t < timeout:
            if is_broadcast_abort:
                if sig_path and not os.path.isfile(sig_path):
                    ack_seen = True
                elif aborts_dir and os.path.isdir(aborts_dir):
                    if pre_abort_hosts:
                        if all(
                            _is_broadcast_already_acked(
                                aborts_dir,
                                h,
                                {"signal_id": dispatched_signal_id} if dispatched_signal_id else {},
                                sig_mtime=start_t - 0.1,
                            )
                            for h in pre_abort_hosts
                        ):
                            ack_seen = True
                    else:
                        try:
                            for fname in os.listdir(aborts_dir):
                                if not fname.lower().endswith((".broadcast_ack", ".abort_ack")):
                                    continue
                                fpath = os.path.join(aborts_dir, fname)
                                with open(fpath, "r", encoding="utf-8") as af:
                                    adata = json.load(af)
                                if isinstance(adata, dict) and (
                                    (dispatched_signal_id and str(adata.get("signal_id") or "") == dispatched_signal_id)
                                    or dispatched_signal_id in (adata.get("acked_signal_ids") or [])
                                ):
                                    ack_seen = True
                                    break
                        except Exception:
                            pass
            else:
                if ack_path and os.path.isfile(ack_path):
                    ack_seen = True
                elif sig_path and not os.path.isfile(sig_path):
                    ack_seen = True

            if client is not None and hasattr(client, "workerList"):
                try:
                    workers_now = normalize_rpc_list_response(client.workerList(), "workers")
                    if is_broadcast_abort:
                        ready_hosts: set[str] = set()
                        for w in workers_now:
                            if not is_healthy_online_worker(w):
                                continue
                            w_hname = str(w.get("hostname", "")).strip().lower()
                            w_state = str(w.get("state", "")).strip().lower()
                            w_id = w.get("worker_id")
                            is_fresh_worker = (w_id is None) or (w_id not in pre_abort_worker_ids)
                            if w_state == "ready" and is_fresh_worker:
                                if w_hname:
                                    ready_hosts.add(w_hname)
                                elif not pre_abort_hosts:
                                    reconnected = True
                        if pre_abort_hosts and pre_abort_hosts.issubset(ready_hosts):
                            reconnected = True
                    else:
                        for w in workers_now:
                            if not is_healthy_online_worker(w):
                                continue
                            w_hname = str(w.get("hostname", "")).strip().lower()
                            w_state = str(w.get("state", "")).strip().lower()
                            w_id = w.get("worker_id")
                            is_fresh_worker = (w_id is None) or (w_id not in pre_abort_worker_ids)
                            if _hostnames_match(w_hname, clean_node) and w_state == "ready" and is_fresh_worker:
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
