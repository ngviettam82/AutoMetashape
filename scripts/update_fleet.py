# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape Autonomous Worker Fleet Remote Updater CLI.

Dispatches in-place codebase updates and graceful auto-reconnect restarts across
connected Metashape Pro cluster worker nodes from the coordinator host.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_SCRIPTS_DIR, ".."))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

try:
    import Metashape
except ImportError:
    Metashape = None

from autometashape.cluster.hardware import find_metashape_path, get_primary_lan_ip
from autometashape.cluster.updater import (
    DEFAULT_REPO_ARCHIVE_URL,
    list_online_workers,
    update_remote_worker,
)


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for fleet updater."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Autonomous Worker Fleet Remote Updater",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        type=str,
        default="",
        help="Cluster Coordinator Server host (default: primary LAN IP or 127.0.0.1:5840)",
    )
    parser.add_argument(
        "--root",
        type=str,
        default="",
        help="Shared network storage UNC path (e.g. \\\\HOST_PC\\SharedMetashape)",
    )
    parser.add_argument(
        "--node",
        type=str,
        default="",
        help="Target worker node name or ID (or 'canary' for single node, or 'all')",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Only update codebase files without triggering worker daemon auto-reconnect restart",
    )
    parser.add_argument(
        "--repo-url",
        type=str,
        default=DEFAULT_REPO_ARCHIVE_URL,
        help="GitHub release or archive zip URL",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all online cluster workers and exit",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Maximum timeout in seconds per worker update batch",
    )
    return parser


def main() -> int:
    """CLI entrypoint with automatic Metashape runtime re-exec."""
    parser = build_arg_parser()
    args = parser.parse_args()

    # If Metashape module is not loaded in current Python runtime, re-exec via metashape.exe
    if Metashape is None:
        ms_exe = find_metashape_path()
        if not ms_exe or not os.path.isfile(ms_exe):
            print("[FATAL ERROR] Agisoft Metashape Professional binary not found.", file=sys.stderr)
            return 1
        cmd = [ms_exe, "-r", os.path.abspath(__file__)] + sys.argv[1:]
        proc = subprocess.run(cmd)
        return proc.returncode

    # Resolve host
    host = args.host.strip()
    if not host:
        lan_ip = get_primary_lan_ip()
        host = f"{lan_ip}:5840" if lan_ip != "127.0.0.1" else "127.0.0.1:5840"
    elif ":" not in host:
        host = f"{host}:5840"

    print("=" * 72)
    print("  AutoMetashape Autonomous Worker Fleet Remote Updater")
    print(f"  Cluster Server : {host}")
    print(f"  Storage Root   : {args.root or '(not specified)'}")
    print("=" * 72)

    client = Metashape.NetworkClient()
    try:
        client.connect(host)
    except Exception as e:
        print(f"[FATAL ERROR] Failed to connect to Metashape Server at {host}: {e}", file=sys.stderr)
        return 1

    online_workers = list_online_workers(client)
    if args.list:
        print(f"\n[Fleet] Discovered {len(online_workers)} online worker node(s):")
        for w in online_workers:
            print(
                f"  - Worker ID={w.get('worker_id')} | Host={w.get('hostaddr')} | "
                f"Name={w.get('hostname')} | State={w.get('state')} | Paused={w.get('paused')}"
            )
        return 0

    if not online_workers:
        print("[NOTICE] No active cluster workers are currently connected to the server.")
        return 0

    if not args.root:
        print("[FATAL ERROR] --root shared storage path is required for remote fleet update.", file=sys.stderr)
        return 1

    # Resolve target nodes
    targets = []
    target_arg = args.node.strip()
    if not target_arg or target_arg.lower() == "canary":
        # Default to single canary node
        targets = [online_workers[0]]
        print(f"[Canary Protocol] Selected single canary node: {targets[0].get('hostname')} (ID={targets[0].get('worker_id')})")
    elif target_arg.lower() == "all":
        targets = online_workers
        print(f"[Fleet Update] Updating all {len(targets)} connected worker node(s) in sequence...")
    else:
        matched = None
        for w in online_workers:
            if (
                str(w.get("worker_id")) == target_arg
                or str(w.get("hostname", "")).lower() == target_arg.lower()
                or target_arg in str(w.get("hostaddr", ""))
            ):
                matched = w
                break
        if not matched:
            print(f"[ERROR] Worker node '{target_arg}' not found among online workers.", file=sys.stderr)
            return 1
        targets = [matched]

    # Execute updates
    success_count = 0
    fail_count = 0
    for node in targets:
        nid = node.get("worker_id")
        nname = node.get("hostname")
        naddr = node.get("hostaddr")
        print(f"\n>>> Dispatching / updating node: {nname} (ID={nid}, Addr={naddr})...")

        res = update_remote_worker(
            client=client,
            worker_id_or_name=nid,
            shared_root=args.root,
            repo_url=args.repo_url,
            timeout=args.timeout,
            restart=not args.no_restart,
        )

        if res.get("success"):
            success_count += 1
            print(f"  [OK] Codebase mirrored successfully on {nname} (Batch #{res.get('batch_id')})!")
            if res.get("relaunched"):
                print(f"  [OK] Node successfully restarted into fresh worker (New ID={res.get('new_worker_id')}, Addr={res.get('new_hostaddr')})!")
            elif not args.no_restart:
                print(f"  [WARNING] Codebase updated, but restart confirmation timed out.")
        else:
            fail_count += 1
            print(f"  [FAILED] Worker update failed on {nname}: {res.get('error')}", file=sys.stderr)

    print("\n" + "=" * 72)
    print(f"  Update Complete: {success_count} succeeded, {fail_count} failed.")
    print("=" * 72)
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
