# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical Cluster Telemetry, Health, and Headroom Monitoring CLI.

Provides non-intrusive read-only telemetry audits of active worker nodes,
batch progress, storage headroom (Win32 Error 112 defense), and automated
deliverable integrity verification.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

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
    """Construct CLI argument parser for cluster monitoring."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Canonical Cluster Fleet Telemetry & Health Audit",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--server",
        type=str,
        default="",
        help="Cluster Coordinator Server host (default: from config or LAN:5840)",
    )
    parser.add_argument(
        "--root",
        type=str,
        default="",
        help="Shared cluster network storage path to audit headroom and deliverables",
    )
    parser.add_argument(
        "--min-headroom",
        type=float,
        default=50.0,
        help="Minimum required free storage headroom in GB",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output telemetry audit as structured JSON",
    )
    parser.add_argument(
        "--no-reexec",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def resolve_server_address(cli_server: str) -> str:
    """Dynamically determine server host and port without hardcoded IPs."""
    if cli_server.strip():
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


def resolve_storage_paths(cli_root: str, batches: List[Dict[str, Any]]) -> List[str]:
    """Dynamically discover storage paths and UNC shares to audit."""
    paths: List[str] = []
    if cli_root.strip():
        paths.append(os.path.normpath(cli_root.strip()))

    cfg = load_cluster_config(filename=DEFAULT_SERVER_CONFIG_FILENAME)
    if cfg.get("root") and cfg["root"] not in paths:
        paths.append(os.path.normpath(cfg["root"]))

    # Inspect batch paths to extract network shares dynamically
    for b in batches:
        b_path = b.get("path", "")
        if b_path.startswith("\\\\"):
            parts = os.path.normpath(b_path).split("\\")
            if len(parts) >= 4:
                share_root = f"\\\\{parts[2]}\\{parts[3]}"
                if share_root not in paths:
                    paths.append(share_root)

    if not paths:
        # Default to local drive
        paths.append(os.path.abspath(os.sep))

    return paths


def audit_headroom(paths: List[str]) -> Dict[str, float]:
    """Measure free storage space in GB across audited paths."""
    headroom: Dict[str, float] = {}
    for p in paths:
        try:
            _, _, free_bytes = shutil.disk_usage(p)
            headroom[p] = round(free_bytes / (1024**3), 2)
        except Exception:
            headroom[p] = 0.0
    return headroom


def verify_deliverables(result_dir: str, proj_name: str) -> Dict[str, Any]:
    """Inspect deliverables in result directory for production integrity."""
    if not os.path.isdir(result_dir):
        return {"intact": False, "reason": "Directory does not exist"}

    obj_files = glob.glob(os.path.join(result_dir, "*.obj"))
    obj_sz = (os.path.getsize(obj_files[0]) / (1024 * 1024)) if obj_files else 0.0

    mtl_files = glob.glob(os.path.join(result_dir, "*.mtl"))
    png_files = sorted(glob.glob(os.path.join(result_dir, "*.png")) + glob.glob(os.path.join(result_dir, "*.tif")))
    tex_files = [p for p in png_files if proj_name in os.path.basename(p) and "_orthomosaic" not in p]

    ortho_files = glob.glob(os.path.join(result_dir, "*_orthomosaic.tif"))
    if not ortho_files:
        ortho_files = [p for p in glob.glob(os.path.join(result_dir, "*.tif")) if "_orthomosaic" in p]
    ortho_sz = (os.path.getsize(ortho_files[0]) / (1024 * 1024)) if ortho_files else 0.0

    prj_files = glob.glob(os.path.join(result_dir, "*.prj"))
    tfw_files = glob.glob(os.path.join(result_dir, "*.tfw"))

    intact = bool(
        obj_files
        and obj_sz >= 1.0
        and len(tex_files) >= 1
        and ortho_files
        and ortho_sz >= 1.0
        and prj_files
        and tfw_files
    )

    return {
        "intact": intact,
        "obj": os.path.basename(obj_files[0]) if obj_files else None,
        "obj_sz_mb": round(obj_sz, 1),
        "mtl": bool(mtl_files),
        "textures_count": len(tex_files),
        "ortho": os.path.basename(ortho_files[0]) if ortho_files else None,
        "ortho_sz_mb": round(ortho_sz, 1),
        "spatial": bool(prj_files and tfw_files),
    }


def execute_audit(
    server_host: str = "",
    shared_root: str = "",
    min_headroom: float = 50.0,
    json_output: bool = False,
) -> Dict[str, Any]:
    """Execute complete read-only cluster monitoring audit."""
    if Metashape is None:
        raise RuntimeError("Metashape module not loaded. Run via metashape.exe -r.")

    resolved_server = resolve_server_address(server_host)
    srv_ip, srv_port_str = resolved_server.split(":")
    srv_port = int(srv_port_str)

    client = Metashape.NetworkClient()
    client.connect(srv_ip, srv_port)

    # 1. Query Workers
    w_resp = client.workerList() or {}
    workers: List[Dict[str, Any]] = w_resp.get("workers", []) if isinstance(w_resp, dict) else []

    # 2. Query Batches
    b_resp = client.batchList() or {}
    batches: List[Dict[str, Any]] = b_resp.get("batches", []) if isinstance(b_resp, dict) else []

    # 3. Headroom Audit
    storage_paths = resolve_storage_paths(shared_root, batches)
    headroom = audit_headroom(storage_paths)
    headroom_ok = all(free_gb >= min_headroom for free_gb in headroom.values())

    # Filter active workers
    active_workers = [w for w in workers if str(w.get("state", "")).lower() != "offline"]

    # 3. Headroom Audit
    storage_paths = resolve_storage_paths(shared_root, batches)
    headroom = audit_headroom(storage_paths)
    headroom_ok = all(free_gb >= min_headroom for free_gb in headroom.values())

    # Categorize batches
    running_batches = [b for b in batches if str(b.get("state", "")).lower() in ("inprogress", "running")]
    pending_batches = [b for b in batches if str(b.get("state", "")).lower() in ("pending", "submitted", "queued")]
    completed_batches = [b for b in batches if str(b.get("state", "")).lower() in ("completed", "done")]

    # 4. Deliverable Verification of Completed Batches
    deliverable_results: Dict[int, Dict[str, Any]] = {}
    for b in completed_batches:
        bid = b.get("batch_id")
        bpath = b.get("path", "")
        pname = os.path.splitext(os.path.basename(bpath))[0]
        # Resolve full path of result directory across storage roots
        rdir = ""
        for sp in storage_paths:
            candidate = os.path.normpath(os.path.join(sp, os.path.dirname(bpath)))
            if os.path.isdir(candidate):
                rdir = candidate
                break
        if rdir:
            deliverable_results[bid] = verify_deliverables(rdir, pname)

    audit_data: Dict[str, Any] = {
        "timestamp": time.time(),
        "server": resolved_server,
        "headroom": headroom,
        "headroom_ok": headroom_ok,
        "min_headroom_gb": min_headroom,
        "workers_online": len(active_workers),
        "workers": active_workers,
        "total_batches": len(batches),
        "running_count": len(running_batches),
        "pending_count": len(pending_batches),
        "completed_count": len(completed_batches),
        "running_batches": running_batches,
        "deliverables": deliverable_results,
    }

    if json_output:
        print(json.dumps(audit_data, indent=2))
        return audit_data

    # Human-Readable Formatting
    now_str = time.ctime(audit_data["timestamp"])
    print("=" * 80)
    print(f"AUTOMETASHAPE CLUSTER MONITOR AUDIT - {now_str}")
    print("=" * 80)

    # Headroom
    headroom_strs = [f"{p}: {gb:.2f} GB free" for p, gb in headroom.items()]
    print(f"[Headroom ({'PASS' if headroom_ok else 'WARN'})] {' | '.join(headroom_strs)}")

    # Workers
    print(f"\n[Fleet Health] {len(active_workers)} active workers online:")
    for w in active_workers:
        wid = w.get("worker_id", "?")
        hname = w.get("hostname", "unknown")
        state = w.get("state", "unknown")
        bid = w.get("batch_id", "none")
        res = w.get("resource_usage", {})
        cpu = res.get("cpu_usage", 0.0) * 100
        ram = res.get("memory_usage", 0.0) * 100
        print(
            f"  - Worker {wid:>2} ({hname:<18}): State={state:<10} | Batch #{str(bid):<3} | CPU: {cpu:>6.1f}% | RAM: {ram:>6.1f}%"
        )

    # Queue Status
    print(
        f"\n[Queue Status] Total: {len(batches)} | Running: {len(running_batches)} | Pending: {len(pending_batches)} | Completed: {len(completed_batches)}"
    )

    # Running Projects Detail
    if running_batches:
        print("\n[Running Projects Progress]:")
        for b in running_batches:
            bid = b.get("batch_id")
            prio = b.get("priority", 0)
            bpath = b.get("path", "")
            pname = os.path.splitext(os.path.basename(bpath))[0]
            # Match worker hostname
            assigned_w = next((w.get("hostname") for w in active_workers if w.get("batch_id") == bid), "unassigned")
            stage = "executing"
            stage_desc = ""
            # Inspect sidecar if present
            for sp in storage_paths:
                sidecar_path = os.path.join(sp, os.path.dirname(bpath), "_autometashape_status.json")
                if os.path.isfile(sidecar_path):
                    try:
                        with open(sidecar_path, "r", encoding="utf-8") as sf:
                            sdata = json.load(sf)
                            stage = sdata.get("stage", stage)
                            stage_desc = sdata.get("description", "")
                    except Exception:
                        pass
                    break
            desc_str = f" | {stage_desc}" if stage_desc else ""
            print(f"  * Batch #{bid:>2} [Prio {prio:>3}] ({assigned_w:<15}): Project={pname} | Stage={stage}{desc_str}")

    # Deliverables Audit
    if deliverable_results:
        print(f"\n[Completed Batches Deliverable Audit] ({len(deliverable_results)} audited):")
        for bid, d in deliverable_results.items():
            status = "PASS: VERIFIED" if d.get("intact") else "FAIL: INCOMPLETE"
            print(
                f"  [{status}] Batch #{bid}: OBJ {d.get('obj_sz_mb', 0)} MB | {d.get('textures_count', 0)} tex | Ortho {d.get('ortho_sz_mb', 0)} MB | Spatial={d.get('spatial', False)}"
            )

    print("\n" + "=" * 80)
    print("MONITOR AUDIT COMPLETE - Cluster healthy and operational.")
    print("=" * 80)
    return audit_data


def main() -> int:
    """CLI entry point for cluster monitoring."""
    parser = build_arg_parser()
    args = parser.parse_args()

    # Plain Python Auto-Delegation
    if Metashape is None and not args.no_reexec:
        ms_exe = find_metashape_path()
        if ms_exe and os.path.isfile(ms_exe):
            cmd = [ms_exe, "-r", os.path.abspath(__file__)] + sys.argv[1:] + ["--no-reexec"]
            res = subprocess.run(cmd)
            return res.returncode
        print("[ERROR] Agisoft Metashape Pro executable not found. Cannot run cluster monitor.", file=sys.stderr)
        return 1

    try:
        execute_audit(
            server_host=args.server,
            shared_root=args.root,
            min_headroom=args.min_headroom,
            json_output=args.json,
        )
        return 0
    except Exception as e:
        print(f"[FATAL ERROR] Cluster monitor audit failed: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
