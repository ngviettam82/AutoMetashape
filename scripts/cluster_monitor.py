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
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Tuple

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
    extract_unc_root,
    find_metashape_path,
    format_host_port,
    get_primary_lan_ip,
    is_healthy_online_worker,
    normalize_rpc_list_response,
)
from autometashape.core.verification import verify_project_deliverables
from autometashape.ipc.protocol import configure_utf8_stdio


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
        "--advisory",
        action="store_true",
        help="Always exit with code 0 even when cluster health issues are detected",
    )
    parser.add_argument(
        "--export-dashboard",
        type=str,
        default="",
        help="Optional path to write web dashboard telemetry (e.g. web/projects_data.js)",
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


_LEGACY_DISPATCH_RE = re.compile(r"^_dispatch_(.+?)(?:_\d{10,13})?$", re.IGNORECASE)


def _is_maintenance_batch_path(batch_path: str) -> bool:
    """Return True if a batch path belongs to internal .cluster/ maintenance/updater tasks (not project tickets)."""
    norm = (batch_path or "").strip().replace("\\", "/").lstrip("/").lower()
    if norm.startswith(".cluster/tickets/") or "/.cluster/tickets/" in norm:
        return False
    return norm.startswith(".cluster/") or "/.cluster/" in norm


def _resolve_batch_project_target(
    batch_path: str,
    storage_paths: List[str],
) -> Tuple[str, str]:
    """Resolve canonical ``(project_name, result_dir)`` for a batch path, supporting tickets & legacy stubs."""
    raw = (batch_path or "").strip()
    norm = raw.replace("\\", "/")
    is_abs_or_unc = raw.startswith("\\\\") or norm.startswith("//") or os.path.isabs(raw)
    rel_clean = norm.lstrip("/")

    # 1. Hidden .cluster/tickets/<stem>.psx -> read sibling <stem>.ticket.json metadata
    if rel_clean.lower().startswith(".cluster/tickets/") or "/.cluster/tickets/" in rel_clean.lower():
        stem = os.path.splitext(os.path.basename(rel_clean))[0]
        rel_dir = os.path.dirname(rel_clean)
        meta_candidates: List[Tuple[str, str]] = []
        if is_abs_or_unc:
            abs_dir = os.path.dirname(os.path.normpath(raw))
            meta_candidates.append(
                (os.path.dirname(os.path.dirname(abs_dir)), os.path.join(abs_dir, f"{stem}.ticket.json"))
            )
        for sp in storage_paths:
            meta_candidates.append(
                (sp, os.path.normpath(os.path.join(sp, ".cluster", "tickets", f"{stem}.ticket.json")))
            )
            if rel_dir and not is_abs_or_unc:
                meta_candidates.append((sp, os.path.normpath(os.path.join(sp, rel_dir, f"{stem}.ticket.json"))))
        for base_sp, meta_p in meta_candidates:
            if os.path.isfile(meta_p):
                try:
                    with open(meta_p, "r", encoding="utf-8") as mf:
                        meta = json.load(mf)
                    pname = str(meta.get("project_name") or "").strip()
                    rel_out = str(meta.get("rel_output_dir") or "").replace("\\", "/").strip("/")
                    if pname:
                        rdir = os.path.normpath(os.path.join(base_sp, rel_out)) if (rel_out and base_sp) else ""
                        return pname, rdir
                except Exception:
                    pass
        # Never fall through to .cluster/tickets/ as a deliverables directory when sidecar is absent
        return stem, ""

    raw_stem = os.path.splitext(os.path.basename(rel_clean))[0]
    m_leg = _LEGACY_DISPATCH_RE.match(raw_stem)
    pname = m_leg.group(1) if m_leg else raw_stem

    if is_abs_or_unc:
        cand_abs = os.path.dirname(os.path.normpath(raw))
        if os.path.isdir(cand_abs):
            return pname, cand_abs

    rel_parent = os.path.dirname(rel_clean).lstrip("/")
    for sp in storage_paths:
        cand = os.path.normpath(os.path.join(sp, rel_parent)) if rel_parent else os.path.normpath(sp)
        if os.path.isdir(cand):
            return pname, cand
    return pname, ""


def resolve_storage_paths(
    cli_root: str,
    batches: List[Dict[str, Any]],
    source_root: str = "",
) -> List[str]:
    """Dynamically discover storage paths and UNC shares to audit.

    Writable Result roots (explicit ``cli_root``, ``server_config.json`` root, and batch
    UNC roots) always precede read-only ``source_root`` so deliverable and sidecar lookups
    never accidentally resolve into the raw source photo share.
    """
    paths: List[str] = []
    if cli_root.strip():
        paths.append(os.path.normpath(cli_root.strip()))

    cfg = load_cluster_config(filename=DEFAULT_SERVER_CONFIG_FILENAME)
    if cfg.get("root") and os.path.normpath(cfg["root"]) not in paths:
        paths.append(os.path.normpath(cfg["root"]))

    # Inspect batch paths to extract network shares dynamically (supports both \\server\share and //server/share)
    for b in batches:
        b_path = b.get("path", "") or ""
        share_root = extract_unc_root(b_path)
        if share_root and share_root not in paths:
            paths.append(share_root)

    if source_root.strip():
        norm_src = os.path.normpath(source_root.strip())
        if norm_src not in paths:
            paths.append(norm_src)

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


def _obj_declares_mtllib(obj_path: str) -> bool:
    """Return True if the first 64 KB of an OBJ file references an external ``mtllib`` library."""
    try:
        with open(obj_path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(65536)
        return any(line.strip().lower().startswith("mtllib ") for line in head.splitlines())
    except Exception:
        return False


def verify_deliverables(result_dir: str, proj_name: str) -> Dict[str, Any]:
    """Inspect deliverables in result directory via canonical structural verification."""
    if not os.path.isdir(result_dir):
        return {"intact": False, "reason": "Directory does not exist"}

    escaped_dir = glob.escape(result_dir)
    obj_files = sorted(glob.glob(os.path.join(escaped_dir, "*.obj")))
    obj_bytes = os.path.getsize(obj_files[0]) if obj_files else 0
    obj_sz = obj_bytes / (1024 * 1024)

    glb_files = sorted(glob.glob(os.path.join(escaped_dir, "*.glb")))
    mtl_files = sorted(glob.glob(os.path.join(escaped_dir, "*.mtl")))
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

    ortho_files = sorted(glob.glob(os.path.join(escaped_dir, "*_orthomosaic.tif")))
    if not ortho_files:
        ortho_files = sorted(
            p for p in glob.glob(os.path.join(escaped_dir, "*.tif")) if "_orthomosaic" in os.path.basename(p).lower()
        )
    ortho_bytes = os.path.getsize(ortho_files[0]) if ortho_files else 0
    ortho_sz = ortho_bytes / (1024 * 1024)

    dem_files = sorted(
        p
        for p in (glob.glob(os.path.join(escaped_dir, "*.tif")) + glob.glob(os.path.join(escaped_dir, "*.tiff")))
        if re.search(r"(?:^|[_-])(?:dem|dsm|elevation)(?:[_.-]|$)", os.path.basename(p).lower()) is not None
    )
    cloud_files = [
        p
        for ext in ("*.laz", "*.las", "*.ply", "*.e57", "*.xyz")
        for p in glob.glob(os.path.join(escaped_dir, ext))
        if not os.path.basename(p).lower().endswith(("_mesh.ply", "_tie_points.ply"))
    ]
    pdf_files = sorted(glob.glob(os.path.join(escaped_dir, "*.pdf")))
    prj_files = glob.glob(os.path.join(escaped_dir, "*.prj"))
    tfw_files = glob.glob(os.path.join(escaped_dir, "*.tfw"))

    present_expected: List[str] = []
    if obj_files:
        present_expected.append("obj")
    if glb_files:
        present_expected.append("glb")
    if ortho_files:
        present_expected.append("ortho")
    if dem_files:
        present_expected.append("dem")
    if cloud_files:
        present_expected.append("point_cloud")
    if pdf_files:
        present_expected.append("report")

    if present_expected:
        canonical = verify_project_deliverables(
            result_dir, project_name=proj_name, expected_assets=present_expected
        )
        requires_mtl_tex = ("obj" in present_expected) and (bool(mtl_files) or _obj_declares_mtllib(obj_files[0]))
        mtl_tex_ok = (bool(mtl_files) and len(tex_files) >= 1) if requires_mtl_tex else True
        intact = bool(canonical.get("status") == "PASS" and mtl_tex_ok)
    else:
        canonical = verify_project_deliverables(result_dir, project_name=proj_name, expected_assets=["psx"])
        intact = bool(canonical.get("status") == "PASS")

    result: Dict[str, Any] = {
        "intact": intact,
        "obj": os.path.basename(obj_files[0]) if obj_files else None,
        "obj_sz_mb": round(obj_sz, 1),
        "mtl": bool(mtl_files),
        "textures_count": len(tex_files),
        "ortho": os.path.basename(ortho_files[0]) if ortho_files else None,
        "ortho_sz_mb": round(ortho_sz, 1),
        "spatial": bool(prj_files and tfw_files),
    }
    if not intact and canonical.get("errors"):
        result["reason"] = "; ".join(canonical["errors"])
    return result


def _is_oom_error_text(text: str) -> bool:
    """Return True if an error message or type indicates an out-of-memory / bad_alloc fault."""
    low = text.lower()
    return "bad allocation" in low or "bad_alloc" in low or "out of memory" in low or "out-of-memory" in low


def format_worker_resource_usage(worker: Dict[str, Any]) -> Tuple[float, float]:
    """Convert Metashape NetworkClient worker resource_usage percentages to (cpu_pct, ram_gb).

    Metashape Pro 2.x (empirically verified on 2.2.2 build 20889) reports both
    ``resource_usage.cpu_usage`` and ``resource_usage.memory_usage`` directly as
    ``0.0–100.0`` percentages relative to total CPU capacity and ``worker.memory_size`` (bytes).
    Falls back safely when ``memory_usage`` is reported in raw bytes (``> 100``) or
    ``memory_size`` is unavailable.
    """
    res = worker.get("resource_usage", {}) or {}
    cpu_pct = float(res.get("cpu_usage", 0.0) or 0.0)

    raw_mem_pct = float(res.get("memory_usage", 0.0) or 0.0)
    mem_size_bytes = float(worker.get("memory_size", 0.0) or 0.0)
    if raw_mem_pct > 100.0:
        ram_gb = raw_mem_pct / (1024.0**3)
    elif mem_size_bytes > 0.0:
        ram_gb = (raw_mem_pct / 100.0) * (mem_size_bytes / (1024.0**3))
    else:
        ram_gb = 0.0
    return cpu_pct, ram_gb


def audit_memory_allocation_faults(
    batches: List[Dict[str, Any]],
    storage_paths: List[str],
) -> List[Dict[str, Any]]:
    """Inspect all batches for std::bad_alloc, out-of-memory crashes, or allocation exceptions."""
    faults: List[Dict[str, Any]] = []

    for b in batches:
        bid = b.get("batch_id")
        bpath = b.get("path", "") or ""
        pname, rdir = _resolve_batch_project_target(bpath, storage_paths)
        err_by_type = b.get("error_count_by_type") or {}
        last_err = b.get("last_error") or {}

        is_oom = False
        oom_count = 0
        reasons: List[str] = []

        # 1. Cluster Server recorded error types
        if "out-of-memory" in err_by_type:
            is_oom = True
            cnt = err_by_type["out-of-memory"]
            oom_count += cnt
            reasons.append(f"{cnt} out-of-memory event(s) recorded by cluster")

        last_err_msg = str(last_err.get("message", "")) if isinstance(last_err, dict) else str(last_err)
        last_err_type = str(last_err.get("type", "")) if isinstance(last_err, dict) else ""
        if _is_oom_error_text(last_err_msg) or _is_oom_error_text(last_err_type):
            is_oom = True
            reasons.append(f"Last error: '{last_err_msg}' ({last_err_type})")

        # 2. Check task-level error records (both singular 'task' and plural 'tasks' schemas)
        task_records: List[Dict[str, Any]] = []
        single_task = b.get("task")
        if isinstance(single_task, dict):
            task_records.append(single_task)
        plural_tasks = b.get("tasks")
        if isinstance(plural_tasks, list):
            task_records.extend(t for t in plural_tasks if isinstance(t, dict))
        for task_rec in task_records:
            for terr in task_rec.get("errors") or []:
                if not isinstance(terr, dict):
                    continue
                tmsg = str(terr.get("message", ""))
                ttype = str(terr.get("type", ""))
                if _is_oom_error_text(tmsg) or _is_oom_error_text(ttype):
                    is_oom = True
                    entry_str = f"Task error: '{tmsg}'"
                    if entry_str not in reasons:
                        reasons.append(entry_str)

        # 3. Check status sidecar for recorded errors
        sidecar_dirs = [rdir] if rdir else []
        if not sidecar_dirs:
            rel_parent = os.path.dirname(bpath.replace("\\", "/")).lstrip("/")
            sidecar_dirs = [os.path.normpath(os.path.join(sp, rel_parent)) for sp in storage_paths]
        for candidate in sidecar_dirs:
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
    srv_ip, srv_port = _split_host_port(resolved_server, 5840)

    client = connect_network_client(Metashape.NetworkClient(), srv_ip, srv_port)
    try:
        # 1. Query Workers (supports dict, list, or attr-based RPC returns)
        workers: List[Dict[str, Any]] = normalize_rpc_list_response(client.workerList(), "workers")

        # 2. Query Batches
        batches: List[Dict[str, Any]] = normalize_rpc_list_response(client.batchList(), "batches")
    finally:
        if hasattr(client, "disconnect"):
            try:
                client.disconnect()
            except Exception:
                pass

    # 3. Headroom Audit
    storage_paths = resolve_storage_paths(shared_root, batches, source_root=source_root)
    headroom = audit_headroom(storage_paths)
    # Only enforce writable headroom threshold and resolve deliverables/sidecars on Result roots;
    # if source_root is explicitly separate and read-only, audit its free space without ever
    # searching raw photo folders for deliverables or failing writable Result headroom.
    norm_source = os.path.normcase(os.path.normpath(source_root.strip())) if source_root.strip() else ""
    result_storage_paths = [
        p for p in storage_paths if not norm_source or os.path.normcase(os.path.normpath(p)) != norm_source
    ] or list(storage_paths)
    checked_headroom = {p: headroom[p] for p in result_storage_paths if p in headroom}
    headroom_ok = all(free_gb >= min_headroom for free_gb in (checked_headroom or headroom).values())

    # Filter healthy active workers (exclude offline & aborted ghost worker records)
    active_workers = [w for w in workers if is_healthy_online_worker(w)]

    # 4. Memory Allocation Faults Audit (passive post-mortem std::bad_alloc / OOM reporting)
    allocation_faults = audit_memory_allocation_faults(batches, result_storage_paths)
    oom_batch_ids = {f["batch_id"] for f in allocation_faults}

    # Categorize batches (skip internal .cluster/ maintenance batches from errored_batches)
    running_batches = [
        b for b in batches if str(b.get("state", "")).lower() in ("inprogress", "running") and not b.get("aborted")
    ]
    pending_batches = [b for b in batches if str(b.get("state", "")).lower() in ("pending", "submitted", "queued")]
    completed_batches = [b for b in batches if str(b.get("state", "")).lower() in ("completed", "done", "finished")]
    errored_batches = [
        b
        for b in batches
        if not _is_maintenance_batch_path(b.get("path", ""))
        and not b.get("aborted")
        and (b.get("failed") or int(b.get("error_count") or 0) > 0 or str(b.get("state", "")).lower() == "failed")
    ]

    # 5. Deliverable Verification of Completed Photogrammetry Batches (skip internal .cluster/ maintenance batches)
    deliverable_results: Dict[int, Dict[str, Any]] = {}
    for b in completed_batches:
        bid = b.get("batch_id")
        bpath = b.get("path", "") or ""
        if _is_maintenance_batch_path(bpath) or b.get("aborted"):
            continue
        pname, rdir = _resolve_batch_project_target(bpath, result_storage_paths)
        if rdir:
            res = verify_deliverables(rdir, pname)
            if bid in oom_batch_ids:
                res["intact"] = False
                res["oom_fault"] = True
            res.setdefault("project", pname)
            res.setdefault("path", rdir)
            deliverable_results[bid] = res
        else:
            deliverable_results[bid] = {
                "intact": False,
                "reason": "Deliverables directory not found",
                "project": pname,
                "path": bpath,
                "obj": None,
                "obj_sz_mb": 0.0,
                "mtl": False,
                "textures_count": 0,
                "ortho": None,
                "ortho_sz_mb": 0.0,
                "spatial": False,
            }

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
        print(json.dumps(audit_data, indent=2, default=str))
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
        wid_str = str(w.get("worker_id") if w.get("worker_id") is not None else "?")
        hname = str(w.get("hostname") or "unknown")
        state = str(w.get("state") or "unknown")
        if w.get("paused"):
            state = f"{state}(paused)"
        bid_val = w.get("batch_id")
        bid_str = str(bid_val if bid_val is not None else "none")
        gmask = str(w.get("gpu_mask") if w.get("gpu_mask") is not None else "?")
        cpu_pct, ram_gb = format_worker_resource_usage(w)
        print(
            f"  - Worker {wid_str:>2} ({hname:<18}): State={state:<14} | GPU={gmask} | Batch #{bid_str:<3} | CPU: {cpu_pct:>5.1f}% | RAM: {ram_gb:>5.1f} GB"
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
            bid_str = str(f.get("batch_id") if f.get("batch_id") is not None else "?")
            pname = f["project"]
            state = f["state"]
            reasons_str = "; ".join(f["reasons"])
            print(f"  * Batch #{bid_str:>2} [Project: {pname}] (State: {state}):")
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
            bid_str = str(bid if bid is not None else "?")
            prio_str = str(b.get("priority") if b.get("priority") is not None else 0)
            bpath = b.get("path", "") or ""
            pname, rdir = _resolve_batch_project_target(bpath, result_storage_paths)
            # Match worker hostname
            assigned_w = next(
                (str(w.get("hostname") or "unassigned") for w in active_workers if w.get("batch_id") == bid),
                "unassigned",
            )
            stage = "executing"
            stage_desc = ""
            # Inspect sidecar strictly under Result storage roots
            sidecar_dirs = (
                [rdir]
                if rdir
                else [
                    os.path.normpath(os.path.join(sp, os.path.dirname(bpath.replace("\\", "/")).lstrip("/")))
                    for sp in result_storage_paths
                ]
            )
            for sdir in sidecar_dirs:
                sidecar_path = os.path.join(sdir, "_autometashape_status.json")
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
            print(
                f"  * Batch #{bid_str:>2} [Prio {prio_str:>3}] ({assigned_w:<15}): Project={pname} | Stage={stage}{desc_str}"
            )

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


def export_dashboard_telemetry(audit_data: Dict[str, Any], output_path: str) -> str:
    """Atomically write dashboard telemetry JSON/JS snapshot from monitor audit results."""
    entries: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    for bid, d in (audit_data.get("deliverables") or {}).items():
        if not isinstance(d, dict):
            continue
        pname = str(d.get("project") or f"Batch_{bid}")
        ppath = str(d.get("path") or "")
        deliv_parts = []
        if d.get("obj"):
            deliv_parts.append(str(d["obj"]))
        if d.get("ortho"):
            deliv_parts.append(str(d["ortho"]))
        if d.get("textures_count"):
            deliv_parts.append(f"{d['textures_count']} tex")
        uid = f"batch-{bid}"
        seen_ids.add(uid)
        intact = bool(d.get("intact"))
        psx_path = (
            os.path.join(ppath, f"{pname}.psx") if ppath and not ppath.lower().endswith(".psx") else ppath
        )
        entries.append(
            {
                "id": uid,
                "name": pname,
                "path": ppath,
                "psxPath": psx_path,
                "sensor": "Cluster Survey",
                "photos": 0,
                "aligned": 0,
                "gsd": None,
                "faces": None,
                "status": "completed" if intact else "ready",
                "statusLabel": "Verified" if intact else "Attention",
                "deliverables": ", ".join(deliv_parts) or str(d.get("reason") or "--"),
                "meshDefault": f"{d.get('obj_sz_mb', 0)} MB" if d.get("obj") else "--",
                "texMem": f"{d.get('textures_count', 0)} files" if d.get("textures_count") else "--",
                "selected": False,
            }
        )

    for b in audit_data.get("running_batches") or []:
        if not isinstance(b, dict):
            continue
        bid = b.get("batch_id")
        uid = f"batch-{bid}"
        if uid in seen_ids:
            continue
        bpath = str(b.get("path") or "")
        pname = os.path.splitext(os.path.basename(bpath.replace("\\", "/")))[0] or f"Batch_{bid}"
        entries.append(
            {
                "id": uid,
                "name": pname,
                "path": os.path.dirname(bpath) if bpath.lower().endswith(".psx") else bpath,
                "psxPath": bpath,
                "sensor": "Cluster Survey",
                "photos": 0,
                "aligned": 0,
                "gsd": None,
                "faces": None,
                "status": "processing",
                "statusLabel": str(b.get("state") or "Processing"),
                "deliverables": "In progress",
                "meshDefault": "--",
                "texMem": "--",
                "selected": False,
            }
        )

    abs_out = os.path.abspath(output_path)
    out_dir = os.path.dirname(abs_out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    tmp_path = f"{abs_out}.tmp.{os.getpid()}"
    payload = (
        "// AutoMetashape Studio - Survey Projects Telemetry Data\n"
        "// Generated by: scripts/cluster_monitor.py --export-dashboard\n"
        f"window.SURVEY_PROJECTS = {json.dumps(entries, indent=2, ensure_ascii=False)};\n"
    )
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, abs_out)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    return abs_out


def main() -> int:
    """CLI entry point for cluster monitoring."""
    configure_utf8_stdio()

    parser = build_arg_parser()
    args = parser.parse_args()

    # Plain Python Auto-Delegation
    if Metashape is None and not args.no_reexec:
        ms_exe = find_metashape_path()
        if ms_exe and os.path.isfile(ms_exe):
            cmd = [ms_exe]
            if sys.platform.startswith("linux"):
                cmd.extend(["-platform", "offscreen"])
            cmd.extend(["-r", os.path.abspath(__file__), "--no-reexec"] + sys.argv[1:])
            res = subprocess.run(cmd)
            return res.returncode
        print("[ERROR] Agisoft Metashape Pro executable not found. Cannot run cluster monitor.", file=sys.stderr)
        return 1
    if Metashape is None:
        print("[FATAL ERROR] Agisoft Metashape Python module is unavailable in this runtime.", file=sys.stderr)
        return 1

    try:
        audit_data = execute_audit(
            server_host=args.server,
            shared_root=args.root,
            min_headroom=args.min_headroom,
            json_output=args.json,
            source_root=args.source_root,
        )
        if getattr(args, "export_dashboard", ""):
            out_dash = export_dashboard_telemetry(audit_data, args.export_dashboard)
            if not args.json:
                print(f"[Dashboard] Exported telemetry snapshot to: {out_dash}")
        return 0 if (audit_data.get("healthy") or args.advisory) else 1
    except Exception as e:
        print(f"[FATAL ERROR] Cluster monitor audit failed: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
