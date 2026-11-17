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

from autometashape.cluster.config import (
    DEFAULT_SERVER_CONFIG_FILENAME,
    load_cluster_config,
)
from autometashape.cluster.hardware import find_metashape_path, get_primary_lan_ip


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

    parser.add_argument("--no-reexec", action="store_true", help=argparse.SUPPRESS)
    return parser


def resolve_server_address(cli_server: str) -> str:
    """Dynamically determine server host and port without hardcoded IPs."""
    if cli_server and cli_server.strip():
        srv = cli_server.strip()
        return srv if ":" in srv else f"{srv}:5840"

    cfg = load_cluster_config(filename=DEFAULT_SERVER_CONFIG_FILENAME)
    if cfg.get("host") and cfg.get("host") != "0.0.0.0":
        port = cfg.get("port", 5840)
        return f"{cfg['host']}:{port}"

    lan_ip = get_primary_lan_ip()
    if lan_ip and lan_ip != "127.0.0.1":
        return f"{lan_ip}:5840"

    return "127.0.0.1:5840"


def connect_client(server_str: str) -> Any:
    """Connect to Metashape NetworkClient."""
    resolved = resolve_server_address(server_str)
    srv_ip, srv_port_str = resolved.split(":")
    client = Metashape.NetworkClient()
    client.connect(srv_ip, int(srv_port_str))
    return client


def cmd_list(client: Any, state_filter: str) -> int:
    """List batches with status and details."""
    b_resp = client.batchList() or {}
    batches: List[Dict[str, Any]] = b_resp.get("batches", []) if isinstance(b_resp, dict) else []

    if state_filter != "all":
        if state_filter == "running":
            batches = [b for b in batches if str(b.get("state", "")).lower() in ("inprogress", "running")]
        elif state_filter == "pending":
            batches = [b for b in batches if str(b.get("state", "")).lower() in ("pending", "submitted", "queued")]
        elif state_filter == "completed":
            batches = [b for b in batches if str(b.get("state", "")).lower() in ("completed", "done")]

    print("=" * 80)
    print(f"{'ID':>4} | {'Priority':>8} | {'State':<12} | {'Project Path'}")
    print("-" * 80)
    for b in batches:
        bid = b.get("batch_id", "?")
        prio = b.get("priority", 0)
        state = b.get("state", "unknown")
        if b.get("paused"):
            state = f"{state} (paused)"
        path = b.get("path", "")
        print(f"{bid:>4} | {prio:>8} | {state:<12} | {path}")
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


def main() -> int:
    """CLI entry point for queue management."""
    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Plain Python Auto-Delegation
    if Metashape is None and not getattr(args, "no_reexec", False):
        ms_exe = find_metashape_path()
        if ms_exe and os.path.isfile(ms_exe):
            cmd = [ms_exe, "-r", os.path.abspath(__file__), "--no-reexec"] + sys.argv[1:]
            res = subprocess.run(cmd)
            return res.returncode
        print("[ERROR] Agisoft Metashape Pro executable not found. Cannot manage cluster queue.", file=sys.stderr)
        return 1

    try:
        client = connect_client(getattr(args, "server", ""))
        if args.command == "list":
            return cmd_list(client, args.filter)
        elif args.command == "set-priority":
            return cmd_set_priority(client, args.batch, args.priority)
        elif args.command == "pause":
            return cmd_pause(client, args.batch)
        elif args.command == "unpause":
            return cmd_unpause(client, args.batch)
        elif args.command == "abort":
            return cmd_abort(client, args.batch, args.demote_priority)
        return 0
    except Exception as e:
        print(f"[FATAL ERROR] Queue operation '{args.command}' failed: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
