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
import subprocess
import sys

try:
    import Metashape
except ImportError:
    Metashape = None

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_SCRIPTS_DIR, ".."))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

from autometashape.cluster.abort import _split_host_port, abort_remote_worker
from autometashape.cluster.config import (
    DEFAULT_SERVER_CONFIG_FILENAME,
    DEFAULT_WORKER_CONFIG_FILENAME,
    load_cluster_config,
)
from autometashape.cluster.hardware import get_primary_lan_ip
from autometashape.ipc.protocol import configure_utf8_stdio


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
    parser.add_argument("--no-reexec", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    """Main execution function for cluster worker abort utility."""
    configure_utf8_stdio()
    parser = build_arg_parser()
    args = parser.parse_args()

    # If running plain Python without Metashape module, auto-delegate through metashape.exe -r
    # to gain direct access to Metashape.NetworkClient for coordinator server operations.
    if Metashape is None and not args.no_reexec:
        from autometashape.cluster.hardware import find_metashape_path

        ms_exe = find_metashape_path()
        if ms_exe and os.path.isfile(ms_exe):
            cmd = [ms_exe]
            if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
                cmd.extend(["-platform", "offscreen"])
            cmd.extend(["-r", os.path.abspath(__file__), "--no-reexec"] + sys.argv[1:])
            res = subprocess.run(cmd)
            return res.returncode

    # Resolve coordinator host & shared root (check CLI -> env -> server_config -> worker_config)
    server_cfg = load_cluster_config(filename=DEFAULT_SERVER_CONFIG_FILENAME)
    raw_server = args.server.strip()
    if raw_server:
        srv_ip, srv_port = _split_host_port(raw_server, 5840)
        server_host = f"[{srv_ip}]:{srv_port}" if ":" in srv_ip else f"{srv_ip}:{srv_port}"
    elif server_cfg.get("host") and server_cfg.get("host") != "0.0.0.0":
        srv_ip, srv_port = _split_host_port(str(server_cfg["host"]), int(server_cfg.get("port", 5840) or 5840))
        server_host = f"[{srv_ip}]:{srv_port}" if ":" in srv_ip else f"{srv_ip}:{srv_port}"
    else:
        lan_ip = get_primary_lan_ip()
        server_host = f"{lan_ip}:5840"

    shared_root = args.root.strip()
    if not shared_root:
        shared_root = os.environ.get("AUTOMETASHAPE_CLUSTER_ROOT", "").strip()
    if not shared_root:
        shared_root = str(server_cfg.get("root", "") or server_cfg.get("network_path", "")).strip()
    if not shared_root:
        worker_cfg = load_cluster_config(filename=DEFAULT_WORKER_CONFIG_FILENAME)
        shared_root = str(worker_cfg.get("root", "") or worker_cfg.get("network_path", "")).strip()

    target_node = args.node.strip()

    # Safety guard: refuse to fall back to local %TEMP%\.abort_pending when targeting a remote node!
    from autometashape.cluster.abort import _hostnames_match
    from autometashape.cluster.hardware import get_local_hostname

    local_host = get_local_hostname().strip().lower()
    target_low = target_node.strip().lower()
    is_local_target = target_low in ("localhost", "127.0.0.1", "::1", "[::1]") or _hostnames_match(
        target_low, local_host
    )
    if not shared_root and not is_local_target:
        print(
            f"[FATAL ERROR] Cannot dispatch remote abort to '{target_node}' without a shared storage root "
            "(--root, AUTOMETASHAPE_CLUSTER_ROOT, server_config.json, or worker_config.json). "
            "Refusing to fall back to local %TEMP% so the coordinator's own worker is never killed.",
            file=sys.stderr,
            flush=True,
        )
        return 1

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
    elif res.get("signal_dispatched", True):
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
    elif not res.get("signal_dispatched", True) or (
        res.get("error") and "failed to write" in str(res.get("error")).lower()
    ):
        print(
            f"[ERROR] Failed to write abort signal for worker '{target_node}': {res.get('error') or 'unknown error'}",
            file=sys.stderr,
        )
        print("=" * 72)
        return 1
    else:
        print(f"[WARNING] Remote abort dispatched but acknowledgment timed out after {args.timeout:.1f}s.")
        print("=" * 72)
        return 1


if __name__ == "__main__":
    sys.exit(main())
