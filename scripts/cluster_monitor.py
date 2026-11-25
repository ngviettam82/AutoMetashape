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
        "--result-root",
        "--result-share",
        dest="root",
        type=str,
        default="",
        help="Shared cluster Result storage path (SSD #2) to audit headroom and deliverables",
    )
    parser.add_argument(
        "--source-root",
        "--source-share",
        dest="source_root",
        type=str,
        default="",
        help="Optional Data Source share (SSD #1) to include in headroom monitoring",
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


def resolve_storage_paths(
    cli_root: str,
    batches: List[Dict[str, Any]],
    source_root: str = "",
) -> List[str]:
    """Dynamically discover storage paths and UNC shares to audit."""
    paths: List[str] = []
    if cli_root.strip():
        paths.append(os.path.normpath(cli_root.strip()))

    if source_root.strip():
        norm_src = os.path.normpath(source_root.strip())
        if norm_src not in paths:
            paths.append(norm_src)

    cfg = load_cluster_config(filename=DEFAULT_SERVER_CONFIG_FILENAME)
    if cfg.get("root") and os.path.normpath(cfg["root"]) not in paths:
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

    escaped_dir = glob.escape(result_dir)
    obj_files = glob.glob(os.path.join(escaped_dir, "*.obj"))
    obj_bytes = os.path.getsize(obj_files[0]) if obj_files else 0
    obj_sz = obj_bytes / (1024 * 1024)

    mtl_files = glob.glob(os.path.join(escaped_dir, "*.mtl"))
    img_candidates = sorted(
        glob.glob(os.path.join(escaped_dir, "*.png"))
        + glob.glob(os.path.join(escaped_dir, "*.tif"))
        + glob.glob(os.path.join(escaped_dir, "*.tiff"))
        + glob.glob(os.path.join(escaped_dir, "*.jpg"))
        + glob.glob(os.path.join(escaped_dir, "*.jpeg"))
    )
    proj_low = proj_name.lower()
    tex_files = [
        p
        for p in img_candidates
        if proj_low in os.path.basename(p).lower()
        and "_orthomosaic" not in os.path.basename(p).lower()
        and "_dem" not in os.path.basename(p).lower()
    ]

    ortho_files = glob.glob(os.path.join(escaped_dir, "*_orthomosaic.tif"))
    if not ortho_files:
        ortho_files = [
            p for p in glob.glob(os.path.join(escaped_dir, "*.tif")) if "_orthomosaic" in os.path.basename(p).lower()
        ]
    ortho_bytes = os.path.getsize(ortho_files[0]) if ortho_files else 0
    ortho_sz = ortho_bytes / (1024 * 1024)

    prj_files = glob.glob(os.path.join(escaped_dir, "*.prj"))
    tfw_files = glob.glob(os.path.join(escaped_dir, "*.tfw"))

    # Align minimum non-trivial byte threshold (1,000 bytes) with canonical verification.py so
    # compact meshes and 25x-lighter 5 cm/px GeoTIFF orthomosaics are never falsely flagged.
    intact = bool(
        obj_files
        and obj_bytes >= 1000
        and mtl_files
        and len(tex_files) >= 1
        and ortho_files
        and ortho_bytes >= 1000
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


def _is_oom_error_text(text: str) -> bool:
    """Return True if an error message or type indicates an out-of-memory / bad_alloc fault."""
    low = text.lower()
    return "bad allocation" in low or "bad_alloc" in low or "out of memory" in low or "out-of-memory" in low


def format_worker_resource_usage(worker: Dict[str, Any]) -> Tuple[float, float]:
    """Convert Metashape NetworkClient worker resource_usage percentages to (cpu_pct, ram_gb).

    Metashape Pro 2.x (empirically verified on 2.2.2 build 20889) reports both
    ``resource_usage.cpu_usage`` and ``resource_usage.memory_usage`` directly as
    ``0.0–100.0`` percentages relative to total CPU capacity and ``worker.memory_size`` (bytes).
    """
    res = worker.get("resource_usage", {}) or {}
    cpu_pct = float(res.get("cpu_usage", 0.0) or 0.0)

    raw_mem_pct = float(res.get("memory_usage", 0.0) or 0.0)
    mem_size_bytes = float(worker.get("memory_size", 0.0) or 0.0)
    if mem_size_bytes > 0.0:
        ram_gb = (raw_mem_pct / 100.0) * (mem_size_bytes / (1024.0**3))
    else:
        ram_gb = raw_mem_pct
    return cpu_pct, ram_gb


def audit_memory_allocation_faults(
    batches: List[Dict[str, Any]],
    storage_paths: List[str],
) -> List[Dict[str, Any]]:
    """Inspect all batches for std::bad_alloc, out-of-memory crashes, or allocation exceptions."""
    faults: List[Dict[str, Any]] = []

    for b in batches:
        bid = b.get("batch_id")
        bpath = b.get("path", "")
        pname = os.path.splitext(os.path.basename(bpath))[0]
        err_by_type = b.get("error_count_by_type", {})
        last_err = b.get("last_error", {})

        is_oom = False
        oom_count = 0
        reasons: List[str] = []

        # 1. Cluster Server recorded error types
        if "out-of-memory" in err_by_type:
            is_oom = True
            cnt = err_by_type["out-of-memory"]
            oom_count += cnt
            reasons.append(f"{cnt} out-of-memory event(s) recorded by cluster")

        last_err_msg = str(last_err.get("message", ""))
        last_err_type = str(last_err.get("type", ""))
        if _is_oom_error_text(last_err_msg) or _is_oom_error_text(last_err_type):
            is_oom = True
            reasons.append(f"Last error: '{last_err_msg}' ({last_err_type})")

        # 2. Check task-level error records if present
        task = b.get("task", {})
        for terr in task.get("errors", []):
            tmsg = str(terr.get("message", ""))
            ttype = str(terr.get("type", ""))
            if _is_oom_error_text(tmsg) or _is_oom_error_text(ttype):
                is_oom = True
                entry_str = f"Task error: '{tmsg}'"
                if entry_str not in reasons:
                    reasons.append(entry_str)

        # 3. Check status sidecar for recorded errors
        for sp in storage_paths:
            candidate = os.path.normpath(os.path.join(sp, os.path.dirname(bpath)))
            sidecar_path = os.path.join(candidate, "_autometashape_status.json")
            if os.path.isfile(sidecar_path):
                try:
                    with open(sidecar_path, "r", encoding="utf-8") as sf:
                        sdata = json.load(sf)
                    serr = str(sdata.get("error", ""))
                    if _is_oom_error_text(serr):
                        is_oom = True
                        entry_str = f"Sidecar error: {sdata.get('error')}"
                        if entry_str not in reasons:
                            reasons.append(entry_str)
                except Exception:
                    pass
                break

        if is_oom:
            faults.append(
                {
                    "batch_id": bid,
                    "project": pname,
                    "path": bpath,
                    "state": b.get("state", "unknown"),
                    "aborted": b.get("aborted", False),
                    "failed": b.get("failed", False),
                    "oom_count": oom_count,
                    "reasons": reasons,
                    "last_error": last_err,
                }
            )

    return faults


def execute_audit(
    server_host: str = "",
    shared_root: str = "",
    min_headroom: float = 50.0,
    json_output: bool = False,
    source_root: str = "",
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
    storage_paths = resolve_storage_paths(shared_root, batches, source_root=source_root)
    headroom = audit_headroom(storage_paths)
    # Only enforce writable headroom threshold on Result/non-read-only roots; if source_root is explicitly
    # separate and read-only, we still report its free space without failing writable Result headroom.
    norm_source = os.path.normpath(source_root.strip()).lower() if source_root.strip() else ""
    checked_headroom = {
        p: gb for p, gb in headroom.items() if not norm_source or os.path.normpath(p).lower() != norm_source
    }
    headroom_ok = all(free_gb >= min_headroom for free_gb in (checked_headroom or headroom).values())

    # Filter active workers
    active_workers = [w for w in workers if str(w.get("state", "")).lower() != "offline"]

    # 4. Memory Allocation Faults Audit (passive post-mortem std::bad_alloc / OOM reporting)
    allocation_faults = audit_memory_allocation_faults(batches, storage_paths)
    oom_batch_ids = {f["batch_id"] for f in allocation_faults}

    # Categorize batches
    running_batches = [b for b in batches if str(b.get("state", "")).lower() in ("inprogress", "running")]
    pending_batches = [b for b in batches if str(b.get("state", "")).lower() in ("pending", "submitted", "queued")]
    completed_batches = [b for b in batches if str(b.get("state", "")).lower() in ("completed", "done")]
    errored_batches = [
        b
        for b in batches
        if b.get("failed") or int(b.get("error_count", 0) or 0) > 0 or str(b.get("state", "")).lower() == "failed"
    ]

    # 5. Deliverable Verification of Completed Batches
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
            res = verify_deliverables(rdir, pname)
            if bid in oom_batch_ids:
                res["intact"] = False
                res["oom_fault"] = True
            deliverable_results[bid] = res

    incomplete_deliverables = [bid for bid, d in deliverable_results.items() if not d.get("intact")]
    issues: List[str] = []
    if not headroom_ok:
        issues.append(f"Result storage headroom below {min_headroom:.1f} GB")
    if not active_workers:
        issues.append("No active workers online")
    if allocation_faults:
        issues.append(f"{len(allocation_faults)} batch(es) reported OOM/bad_alloc")
    if errored_batches:
        issues.append(f"{len(errored_batches)} batch(es) recorded execution errors")
    if incomplete_deliverables:
        issues.append(f"{len(incomplete_deliverables)} completed batch(es) failed deliverable verification")

    healthy = len(issues) == 0

    audit_data: Dict[str, Any] = {
        "timestamp": time.time(),
        "server": resolved_server,
        "healthy": healthy,
        "issues": issues,
        "headroom": headroom,
        "headroom_ok": headroom_ok,
        "min_headroom_gb": min_headroom,
        "workers_online": len(active_workers),
        "workers": active_workers,
        "total_batches": len(batches),
        "running_count": len(running_batches),
        "pending_count": len(pending_batches),
        "completed_count": len(completed_batches),
        "errored_count": len(errored_batches),
        "running_batches": running_batches,
        "deliverables": deliverable_results,
        "allocation_faults": allocation_faults,
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

    # Workers (passive CPU % & RAM GB telemetry — no artificial RAM guard)
    print(f"\n[Fleet Health] {len(active_workers)} active workers online:")
    for w in active_workers:
        wid = w.get("worker_id", "?")
        hname = w.get("hostname", "unknown")
        state = w.get("state", "unknown")
        bid = w.get("batch_id", "none")
        cpu_pct, ram_gb = format_worker_resource_usage(w)
        print(
            f"  - Worker {wid:>2} ({hname:<18}): State={state:<10} | Batch #{str(bid):<3} | CPU: {cpu_pct:>5.1f}% | RAM: {ram_gb:>5.1f} GB"
        )

    # Queue Status
    print(
        f"\n[Queue Status] Total: {len(batches)} | Running: {len(running_batches)} | Pending: {len(pending_batches)} | Completed: {len(completed_batches)} | Errored: {len(errored_batches)}"
    )

    # Memory Allocation Faults Section (passive post-mortem std::bad_alloc / OOM reporting)
    print("\n" + "-" * 80)
    print("[Memory Allocation Audit (std::bad_alloc / OOM Post-Mortem)]")
    print("-" * 80)
    if allocation_faults:
        print(f"  [ALERT: {len(allocation_faults)} BATCH(ES) FAULTED ON std::bad_alloc / OOM]")
        for f in allocation_faults:
            bid = f["batch_id"]
            pname = f["project"]
            state = f["state"]
            reasons_str = "; ".join(f["reasons"])
            print(f"  * Batch #{bid:>2} [Project: {pname}] (State: {state}):")
            print(f"    - Failure Diagnosis: {reasons_str}")
            print(
                "    - Corrective Action: Worker physical RAM exhausted. Resubmit with '--no-guided-matching' or reduced keypoints limit."
            )
    else:
        print("  [PASS: NO BAD_ALLOC DETECTED] No out-of-memory faults recorded across cluster batches.")

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
                sidecar_path = os.path.normpath(os.path.join(sp, os.path.dirname(bpath), "_autometashape_status.json"))
                if os.path.isfile(sidecar_path):
                    try:
                        with open(sidecar_path, "r", encoding="utf-8") as sf:
                            sdata = json.load(sf)
                            stage = sdata.get("stage_key") or sdata.get("stage") or stage
                            stage_desc = sdata.get("stage_desc") or sdata.get("description", "")
                            worker_node = sdata.get("worker_node")
                            if worker_node and assigned_w == "unassigned":
                                assigned_w = worker_node
                    except Exception:
                        pass
                    break
            desc_str = f" | {stage_desc}" if stage_desc else ""
            print(f"  * Batch #{bid:>2} [Prio {prio:>3}] ({assigned_w:<15}): Project={pname} | Stage={stage}{desc_str}")

    # Deliverables Audit
    if deliverable_results:
        print(f"\n[Completed Batches Deliverable Audit] ({len(deliverable_results)} audited):")
        for bid, d in deliverable_results.items():
            if d.get("oom_fault"):
                status = "FAIL: BAD_ALLOC (OOM)"
            elif d.get("intact"):
                status = "PASS: VERIFIED"
            else:
                status = "FAIL: INCOMPLETE"
            print(
                f"  [{status}] Batch #{bid}: OBJ {d.get('obj_sz_mb', 0)} MB | {d.get('textures_count', 0)} tex | Ortho {d.get('ortho_sz_mb', 0)} MB | Spatial={d.get('spatial', False)}"
            )

    print("\n" + "=" * 80)
    if healthy:
        print("MONITOR AUDIT COMPLETE [PASS] - Cluster healthy and operational.")
    else:
        print(f"MONITOR AUDIT COMPLETE [WARN] - Attention required: {'; '.join(issues)}")
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
            source_root=args.source_root,
        )
        return 0
    except Exception as e:
        print(f"[FATAL ERROR] Cluster monitor audit failed: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
