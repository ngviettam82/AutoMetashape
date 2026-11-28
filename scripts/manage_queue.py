# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical Cluster Queue & Batch Priority Management CLI.

Provides safe, deterministic, operator-gated queue management for Metashape Network Server:
listing batches, pausing, unpausing, setting priority, and aborting tasks without race conditions.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Any, Dict, List

try:
    import Metashape
except ImportError:
    Metashape = None

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_SCRIPTS_DIR, ".."))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

from autometashape.cluster.abort import _split_host_port
from autometashape.cluster.config import (
    DEFAULT_SERVER_CONFIG_FILENAME,
    load_cluster_config,
)
from autometashape.cluster.hardware import (
    connect_network_client,
    find_metashape_path,
    format_host_port,
    get_primary_lan_ip,
    is_healthy_online_worker,
    normalize_rpc_list_response,
)
from autometashape.cluster.orchestrator import resolve_batch_canonical_path
from autometashape.ipc.protocol import configure_utf8_stdio


def _parse_gpu_mask(val: str) -> int:
    """Parse decimal or hex GPU bitmask string (e.g. ``'3'``, ``'0x3'``) and reject negatives."""
    try:
        parsed = int(str(val).strip(), 0)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"invalid integer GPU mask: {val!r}") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"GPU mask must be non-negative (got {parsed})")
    return parsed


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for queue management."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Canonical Cluster Queue & Priority Management Utility",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Queue command to execute")

    # Command: list
    list_p = subparsers.add_parser("list", help="List all batches on the cluster server")
    list_p.add_argument("--server", type=str, default="", help="Cluster server host")
    list_p.add_argument("--root", type=str, default="", help="Shared cluster storage root (for ticket resolution)")
    list_p.add_argument(
        "--filter", type=str, default="all", choices=["all", "running", "pending", "completed"], help="Filter by state"
    )

    # Command: set-priority
    prio_p = subparsers.add_parser("set-priority", help="Change priority of a batch")
    prio_p.add_argument("--batch", type=int, required=True, help="Batch ID to modify")
    prio_p.add_argument("--priority", type=int, required=True, help="New priority integer")
    prio_p.add_argument("--server", type=str, default="", help="Cluster server host")

    # Command: pause
    pause_p = subparsers.add_parser("pause", help="Pause a pending or running batch")
    pause_p.add_argument("--batch", type=int, required=True, help="Batch ID to pause")
    pause_p.add_argument("--server", type=str, default="", help="Cluster server host")

    # Command: unpause
    unpause_p = subparsers.add_parser("unpause", help="Unpause a paused batch")
    unpause_p.add_argument("--batch", type=int, required=True, help="Batch ID to unpause")
    unpause_p.add_argument("--server", type=str, default="", help="Cluster server host")

    # Command: abort
    abort_p = subparsers.add_parser("abort", help="Abort a batch and optionally demote its priority")
    abort_p.add_argument("--batch", type=int, required=True, help="Batch ID to abort")
    abort_p.add_argument("--demote-priority", type=int, default=-100, help="Priority to assign after abort")
    abort_p.add_argument("--server", type=str, default="", help="Cluster server host")

    # Command: pause-worker
    pw_p = subparsers.add_parser("pause-worker", help="Pause one or more cluster workers by ID or hostname")
    pw_p.add_argument("--worker-id", type=int, default=None, help="Specific worker ID to pause")
    pw_p.add_argument("--hostname", type=str, default="", help="Pause all online workers matching hostname")
    pw_p.add_argument("--server", type=str, default="", help="Cluster server host")

    # Command: unpause-worker
    uw_p = subparsers.add_parser("unpause-worker", help="Unpause one or more cluster workers by ID or hostname")
    uw_p.add_argument("--worker-id", type=int, default=None, help="Specific worker ID to unpause")
    uw_p.add_argument("--hostname", type=str, default="", help="Unpause all online workers matching hostname")
    uw_p.add_argument("--server", type=str, default="", help="Cluster server host")

    # Command: clear-worker (disconnect stale/aborted ghost worker records)
    cw_p = subparsers.add_parser("clear-worker", help="Disconnect a stale/aborted worker ID on the server")
    cw_p.add_argument("--worker-id", type=int, required=True, help="Worker ID to abort/quit on the server")
    cw_p.add_argument("--only-aborted", action="store_true", help="Safety guard: refuse unless worker.aborted is True")
    cw_p.add_argument("--server", type=str, default="", help="Cluster server host")

    # Command: set-worker-gpu-mask
    gpu_p = subparsers.add_parser("set-worker-gpu-mask", help="Update live GPU bitmask for an online cluster worker")
    gpu_p.add_argument("--worker-id", type=int, default=None, help="Specific worker ID to update")
    gpu_p.add_argument("--hostname", type=str, default="", help="Update all healthy online workers matching hostname")
    gpu_p.add_argument(
        "--gpu-mask",
        type=_parse_gpu_mask,
        required=True,
        help="Integer GPU bitmask (decimal or hex, e.g. 2, 3, or 0x3)",
    )
    gpu_p.add_argument("--server", type=str, default="", help="Cluster server host")

    parser.add_argument("--no-reexec", action="store_true", help=argparse.SUPPRESS)
    return parser


def resolve_server_address(cli_server: str) -> str:
    """Dynamically determine server host and port without hardcoded IPs."""
    if cli_server and cli_server.strip():
        host, port = _split_host_port(cli_server.strip(), 5840)
        return format_host_port(host, port)

    cfg = load_cluster_config(filename=DEFAULT_SERVER_CONFIG_FILENAME)
    if cfg.get("host") and cfg.get("host") != "0.0.0.0":
        host, port = _split_host_port(str(cfg["host"]), int(cfg.get("port", 5840) or 5840))
        return format_host_port(host, port)

    lan_ip = get_primary_lan_ip()
    if lan_ip and lan_ip != "127.0.0.1":
        return format_host_port(lan_ip, 5840)

    return "127.0.0.1:5840"


def connect_client(server_str: str) -> Any:
    """Connect to Metashape NetworkClient."""
    resolved = resolve_server_address(server_str)
    srv_ip, srv_port = _split_host_port(resolved, 5840)
    client = Metashape.NetworkClient()
    return connect_network_client(client, srv_ip, srv_port)


def cmd_list(client: Any, state_filter: str, shared_root: str = "") -> int:
    """List batches with status and details."""
    batches: List[Dict[str, Any]] = normalize_rpc_list_response(client.batchList(), "batches")

    eff_root = shared_root.strip()
    if not eff_root:
        cfg = load_cluster_config(filename=DEFAULT_SERVER_CONFIG_FILENAME)
        eff_root = str(cfg.get("root") or "").strip()

    if state_filter != "all":
        if state_filter == "running":
            batches = [b for b in batches if str(b.get("state", "")).lower() in ("inprogress", "running")]
        elif state_filter == "pending":
            batches = [b for b in batches if str(b.get("state", "")).lower() in ("pending", "submitted", "queued")]
        elif state_filter == "completed":
            batches = [b for b in batches if str(b.get("state", "")).lower() in ("completed", "done", "finished")]

    print("=" * 80)
    print(f"{'ID':>4} | {'Priority':>8} | {'State':<12} | {'Project Path'}")
    print("-" * 80)
    for b in batches:
        bid_str = str(b.get("batch_id") if b.get("batch_id") is not None else "?")
        prio_str = str(b.get("priority") if b.get("priority") is not None else 0)
        state = str(b.get("state") or "unknown")
        if b.get("paused"):
            state = f"{state} (paused)"
        path = str(b.get("path") or "")
        canon = resolve_batch_canonical_path(path, eff_root) if eff_root else path
        display_path = f"{path} -> {canon}" if (canon and canon != path) else path
        print(f"{bid_str:>4} | {prio_str:>8} | {state:<12} | {display_path}")
    print("=" * 80)
    print(f"Total Batches Listed: {len(batches)}")
    return 0


def cmd_set_priority(client: Any, batch_id: int, priority: int) -> int:
    """Modify priority of a specific batch."""
    print(f"Setting priority of Batch #{batch_id} to {priority}...", flush=True)
    client.setBatchPriority(batch_id, priority)
    print(f"[OK] Batch #{batch_id} priority updated to {priority}.")
    return 0


def cmd_pause(client: Any, batch_id: int) -> int:
    """Pause a batch."""
    print(f"Pausing Batch #{batch_id}...", flush=True)
    client.setBatchPaused(batch_id, True)
    print(f"[OK] Batch #{batch_id} paused.")
    return 0


def cmd_unpause(client: Any, batch_id: int) -> int:
    """Unpause a batch."""
    print(f"Unpausing Batch #{batch_id}...", flush=True)
    client.setBatchPaused(batch_id, False)
    print(f"[OK] Batch #{batch_id} unpaused.")
    return 0


def cmd_abort(client: Any, batch_id: int, demote_priority: int) -> int:
    """Abort a batch and demote its priority."""
    print(f"Aborting Batch #{batch_id} on cluster server...", flush=True)
    # If paused, unpause first to avoid scheduler freeze
    try:
        client.setBatchPaused(batch_id, False)
    except Exception:
        pass

    client.abortBatch(batch_id)
    try:
        client.setBatchPriority(batch_id, demote_priority)
        print(f"[OK] Batch #{batch_id} aborted and demoted to Priority {demote_priority}.")
    except Exception:
        print(f"[OK] Batch #{batch_id} aborted.")
    return 0


def _is_ip_literal(value: str) -> bool:
    """Return True if ``value`` is a valid IPv4 or IPv6 literal address."""
    import ipaddress

    try:
        ipaddress.ip_address(str(value or "").strip().strip("[]"))
        return True
    except ValueError:
        return False


def _match_workers(
    workers: List[Dict[str, Any]],
    worker_id: int | None = None,
    hostname: str = "",
) -> List[tuple[Any, str]]:
    """Match online workers by ID and/or hostname with short-hostname fallback (IP-safe)."""
    target_host = hostname.strip().lower()
    target_is_ip = _is_ip_literal(target_host)
    target_short = "" if (not target_host or target_is_ip) else target_host.split(".", 1)[0]
    exact_matches: List[tuple[Any, str]] = []
    short_matches: List[tuple[Any, str]] = []
    for w in workers:
        wid = w.get("worker_id")
        whost_raw = str(w.get("hostname", "") or "")
        whost = whost_raw.strip().lower()
        whost_is_ip = _is_ip_literal(whost)
        whost_short = "" if (not whost or whost_is_ip) else whost.split(".", 1)[0]
        can_short = bool(target_short and whost_short and not target_is_ip and not whost_is_ip)
        if worker_id is not None and target_host:
            if wid == worker_id and whost == target_host:
                exact_matches.append((wid, whost_raw))
            elif wid == worker_id and can_short and whost_short == target_short:
                short_matches.append((wid, whost_raw))
        elif worker_id is not None:
            if wid == worker_id:
                exact_matches.append((wid, whost_raw))
        elif target_host and is_healthy_online_worker(w):
            if whost == target_host:
                exact_matches.append((wid, whost_raw))
            elif can_short and whost_short == target_short:
                short_matches.append((wid, whost_raw))
    return exact_matches or short_matches


def cmd_set_worker_paused(client: Any, paused: bool, worker_id: int | None = None, hostname: str = "") -> int:
    """Pause or unpause cluster workers matching worker_id and/or hostname."""
    if worker_id is None and not hostname.strip():
        print("[ERROR] Provide either --worker-id or --hostname.", file=sys.stderr)
        return 1
    workers = normalize_rpc_list_response(client.workerList(), "workers")
    matched = _match_workers(workers, worker_id=worker_id, hostname=hostname)
    if not matched:
        print(f"[WARNING] No online worker matched worker_id={worker_id} hostname='{hostname}'.")
        return 1
    verb = "Pausing" if paused else "Unpausing"
    for wid, wname in matched:
        print(f"{verb} worker #{wid} ({wname})...", flush=True)
        client.setWorkerPaused(wid, paused)
        print(f"[OK] Worker #{wid} ({wname}) paused={paused}.")
    return 0


def cmd_clear_worker(client: Any, worker_id: int, only_aborted: bool = False) -> int:
    """Disconnect a stale or aborted worker record on the server."""
    workers = normalize_rpc_list_response(client.workerList(), "workers")
    target = next((w for w in workers if w.get("worker_id") == worker_id), None)
    if target is None:
        print(f"[INFO] Worker #{worker_id} is not in server workerList.")
        return 0
    if only_aborted and not target.get("aborted"):
        print(f"[REFUSED] Worker #{worker_id} ({target.get('hostname')}) is not marked aborted.", file=sys.stderr)
        return 1
    print(f"Clearing worker #{worker_id} ({target.get('hostname')}, batch={target.get('batch_id')})...", flush=True)
    dispatched = 0
    for fn_name in ("abortWorker", "quitWorker"):
        if hasattr(client, fn_name):
            try:
                getattr(client, fn_name)(worker_id)
                dispatched += 1
                print(f"  [OK] {fn_name}({worker_id}) dispatched.")
            except Exception as e:
                print(f"  [Notice] {fn_name}({worker_id}): {e}")
    if dispatched == 0:
        print(f"[ERROR] Failed to dispatch abortWorker/quitWorker for worker #{worker_id}.", file=sys.stderr)
        return 1
    return 0


def cmd_set_worker_gpu_mask(
    client: Any,
    gpu_mask: int,
    worker_id: int | None = None,
    hostname: str = "",
) -> int:
    """Set live GPU bitmask on healthy online workers matching worker_id and/or hostname."""
    if worker_id is None and not hostname.strip():
        print("[ERROR] Provide either --worker-id or --hostname.", file=sys.stderr)
        return 1
    workers = normalize_rpc_list_response(client.workerList(), "workers")
    matched = _match_workers(workers, worker_id=worker_id, hostname=hostname)
    if not matched:
        print(f"[WARNING] No online worker matched worker_id={worker_id} hostname='{hostname}'.")
        return 1
    for wid, wname in matched:
        print(f"Setting GPU mask of worker #{wid} ({wname}) to {gpu_mask} (0x{gpu_mask:X})...", flush=True)
        client.setWorkerGpuMask(wid, gpu_mask)
        print(f"[OK] Worker #{wid} ({wname}) gpu_mask={gpu_mask}.")
    return 0


def main() -> int:
    """CLI entry point for queue management."""
    configure_utf8_stdio()

    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Plain Python Auto-Delegation
    if Metashape is None and not getattr(args, "no_reexec", False):
        ms_exe = find_metashape_path()
        if ms_exe and os.path.isfile(ms_exe):
            cmd = [ms_exe]
            if sys.platform.startswith("linux"):
                cmd.extend(["-platform", "offscreen"])
            cmd.extend(["-r", os.path.abspath(__file__), "--no-reexec"] + sys.argv[1:])
            res = subprocess.run(cmd)
            return res.returncode
        print("[ERROR] Agisoft Metashape Pro executable not found. Cannot manage cluster queue.", file=sys.stderr)
        return 1
    if Metashape is None:
        print("[FATAL ERROR] Agisoft Metashape Python module is unavailable in this runtime.", file=sys.stderr)
        return 1

    client = None
    try:
        client = connect_client(getattr(args, "server", ""))
        if args.command == "list":
            return cmd_list(client, args.filter, shared_root=getattr(args, "root", ""))
        elif args.command == "set-priority":
            return cmd_set_priority(client, args.batch, args.priority)
        elif args.command == "pause":
            return cmd_pause(client, args.batch)
        elif args.command == "unpause":
            return cmd_unpause(client, args.batch)
        elif args.command == "abort":
            return cmd_abort(client, args.batch, args.demote_priority)
        elif args.command == "pause-worker":
            return cmd_set_worker_paused(client, True, worker_id=args.worker_id, hostname=args.hostname)
        elif args.command == "unpause-worker":
            return cmd_set_worker_paused(client, False, worker_id=args.worker_id, hostname=args.hostname)
        elif args.command == "clear-worker":
            return cmd_clear_worker(client, args.worker_id, only_aborted=args.only_aborted)
        elif args.command == "set-worker-gpu-mask":
            return cmd_set_worker_gpu_mask(client, args.gpu_mask, worker_id=args.worker_id, hostname=args.hostname)
        return 0
    except Exception as e:
        print(f"[FATAL ERROR] Queue operation '{args.command}' failed: {e}", file=sys.stderr, flush=True)
        return 1
    finally:
        if client is not None and hasattr(client, "disconnect"):
            try:
                client.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
