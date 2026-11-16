# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape Cluster Remote Worker Abort & Self-Healing Restart CLI.

Dispatches remote abort commands to stuck worker nodes, unfreezes the Metashape Server
batch scheduler state machine, and awaits self-healing worker reconnect.
"""

from __future__ import annotations

import argparse
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_SCRIPTS_DIR, ".."))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

from autometashape.cluster.abort import abort_remote_worker
from autometashape.cluster.config import load_cluster_config
from autometashape.cluster.hardware import get_primary_lan_ip


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser for cluster abort command."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Cluster Remote Worker Abort & Self-Healing Restart Utility",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--node",
        type=str,
        required=True,
        help="Target worker hostname or node name to forcibly abort and restart",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=None,
        help="Optional Metashape batch ID to unpause, abort, and demote on the server",
    )
    parser.add_argument(
        "--root",
        type=str,
        default="",
        help="Shared network storage UNC path (e.g. \\\\HOST_PC\\SharedMetashape)",
    )
    parser.add_argument(
        "--server",
        type=str,
        default="",
        help="Cluster Coordinator Server host (default: primary LAN IP:5840 or 127.0.0.1:5840)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Maximum seconds to wait for worker supervisor to acknowledge and reconnect",
    )
    return parser


def main() -> int:
    """Main execution function for cluster worker abort utility."""
    parser = build_arg_parser()
    args = parser.parse_args()

    # Resolve coordinator host
    server_host = args.server.strip()
    if not server_host:
        server_cfg = load_cluster_config(is_server=True)
        if server_cfg.get("host") and server_cfg.get("host") != "0.0.0.0":
            server_host = f"{server_cfg['host']}:{server_cfg.get('port', 5840)}"
        else:
            lan_ip = get_primary_lan_ip()
            server_host = f"{lan_ip}:5840"

    # Resolve shared root UNC path
    shared_root = args.root.strip()
    if not shared_root:
        worker_cfg = load_cluster_config(is_server=False)
        shared_root = worker_cfg.get("root", "").strip()

    target_node = args.node.strip()

    print("=" * 72)
    print("  AutoMetashape: Autonomous Cluster Worker Remote Abort Utility")
    print(f"  Target Worker   : {target_node}")
    print(f"  Target Batch ID : {args.batch if args.batch is not None else 'None (Node-level abort)'}")
    print(f"  Cluster Server  : {server_host}")
    print(f"  Shared Storage  : {shared_root or '(Local/Unspecified)'}")
    print(f"  Timeout Window  : {args.timeout:.1f}s")
    print("=" * 72, flush=True)

    print(f"\n[DISPATCH] Sending abort signal for worker '{target_node}'...", flush=True)
    res = abort_remote_worker(
        node_name=target_node,
        batch_id=args.batch,
        shared_root=shared_root,
        server_host=server_host,
        timeout=args.timeout,
    )

    if res.get("acknowledged"):
        print(f"[OK] Worker '{target_node}' acknowledged abort signal and terminated stuck process.")
    else:
        print(f"[NOTICE] Signal dispatched. Supervisor on '{target_node}' will handle abort on next watchdog cycle.")

    if res.get("reconnected"):
        print(f"[OK] Worker '{target_node}' successfully self-healed and reconnected to cluster!")
    elif res.get("acknowledged"):
        print(f"[STATUS] Worker '{target_node}' is currently restarting and auto-reconnecting.")

    if res.get("error"):
        print(f"[INFO] Notice: {res['error']}")

    print("\n" + "=" * 72)
    if res.get("success"):
        print(f"[SUCCESS] Remote abort for worker '{target_node}' executed successfully!")
        print("=" * 72)
        return 0
    else:
        print(f"[WARNING] Remote abort dispatched but acknowledgment timed out after {args.timeout:.1f}s.")
        print("=" * 72)
        return 1


if __name__ == "__main__":
    sys.exit(main())
