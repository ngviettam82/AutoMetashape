# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Canonical Cluster Batch Submission CLI.

Safely initializes photogrammetry projects, guarantees physical .psx project
materialization on disk before dispatch (preventing missing-file worker crashes),
configures pipeline stages, and submits batches to the Metashape Network Server.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Any, List, Optional

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
from autometashape.cluster.dual_share import (
    discover_dual_share_projects,
    extract_path_leaf_name,
    is_subpath,
    relpath_under,
    resolve_result_network_root,
    validate_dual_share_topology,
)
from autometashape.cluster.hardware import extract_unc_root, find_metashape_path, get_primary_lan_ip
from autometashape.cluster.orchestrator import (
    _find_blocking_batch_id,
    _norm_cluster_path,
    _safe_abort_batch,
    _wait_for_batch_path_release,
    build_monolithic_runscript_task,
    clear_stale_project_lock,
    resolve_batch_canonical_path,
    safe_create_cluster_batch,
)
from autometashape.core.telemetry import (
    SUPPORTED_IMAGE_EXTS,
    _is_ignored_survey_dir,
    _is_supported_image_file,
    parse_exclude_folder_patterns,
)
from autometashape.ipc.protocol import configure_utf8_stdio

IMAGE_EXTENSIONS = SUPPORTED_IMAGE_EXTS


def get_cluster_relative_path(target_path: str, shared_root: str) -> str:
    """Compute cluster-relative POSIX path for Metashape Network Server."""
    if shared_root and is_subpath(target_path, shared_root):
        rel = relpath_under(target_path, shared_root)
        if rel and rel != ".":
            return rel
        try:
            rel_fb = os.path.relpath(target_path, shared_root).replace("\\", "/")
            if not rel_fb.startswith(".."):
                return rel_fb
        except Exception:
            pass
    return target_path.replace("\\", "/")


def build_arg_parser() -> argparse.ArgumentParser:
    """Construct argument parser for cluster batch submission."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Canonical Cluster Batch Submission Utility",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--images",
        type=str,
        default="",
        help="Path to folder containing raw capture images (UNC or local path). Optional when --source-share is used.",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="",
        help="Target output directory for deliverables and .psx archive (default: <images>/Result)",
    )
    parser.add_argument(
        "--source-share",
        "--data-root",
        dest="source_share",
        type=str,
        default="",
        help="Data Source Share (SSD #1, read-only raw image root) for dual-share multi-SSD workflows",
    )
    parser.add_argument(
        "--result-share",
        "--result-root",
        dest="result_share",
        type=str,
        default="",
        help="Result Share (SSD #2, writable .psx + deliverables root and Metashape --root) for dual-share workflows",
    )
    parser.add_argument(
        "--project-name",
        type=str,
        default="",
        help="Project label (default: basename of images folder)",
    )
    parser.add_argument(
        "--priority",
        type=int,
        default=50,
        help="Scheduler dispatch priority integer",
    )
    parser.add_argument(
        "--server",
        type=str,
        default="",
        help="Metashape Server host (default: from server_config.json or primary LAN:5840)",
    )
    parser.add_argument(
        "--root",
        type=str,
        default="",
        help="Shared cluster storage root path (default: auto-detected from result-share, output UNC, or server config)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume execution from existing .psx checkpoint if present in output folder",
    )
    parser.add_argument(
        "--no-local-scratch-staging",
        action="store_true",
        help="Bypass NVMe local scratch copy and process directly on shared storage",
    )
    parser.add_argument(
        "--preset",
        type=str,
        default="pro",
        choices=["pro", "custom", "building"],
        help="Reconstruction quality preset",
    )
    parser.add_argument(
        "--ortho-resolution",
        type=float,
        default=0.05,
        help="Orthomosaic build and export ground sampling distance in meters/pixel (default: 0.05 = 5 cm/px)",
    )
    parser.add_argument(
        "--keypoint-limit",
        type=int,
        default=None,
        help="Maximum keypoints per image during alignment (default: preset/pro plan = 50000)",
    )
    parser.add_argument(
        "--keypoint-limit-per-mpx",
        type=int,
        default=None,
        help="Keypoint limit per megapixel for guided matching (default: preset/pro plan = 2000)",
    )
    parser.add_argument(
        "--tiepoint-limit",
        type=int,
        default=None,
        help="Maximum tie points per image during alignment (default: preset/pro plan = 40000)",
    )
    parser.add_argument(
        "--build-point-cloud",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Explicitly enable (--build-point-cloud) or disable (--no-build-point-cloud) dense point cloud generation",
    )
    parser.add_argument(
        "--no-point-cloud",
        dest="build_point_cloud",
        action="store_false",
        help="Alias for --no-build-point-cloud",
    )
    parser.add_argument(
        "--worker-limit",
        type=int,
        default=1,
        help="Maximum concurrent workers assigned to this batch (default: 1 for monolithic batch)",
    )
    parser.add_argument(
        "--no-guided-matching",
        action="store_true",
        help="Disable guided matching during photo alignment to reduce RAM usage on large datasets",
    )
    parser.add_argument(
        "--exclude-folders",
        type=str,
        default="",
        help="Comma-separated case-insensitive subfolder patterns to exclude from ingestion (e.g. 'manual,cổng')",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Abort any existing active batch for this project before submitting a fresh batch (default: attach safely)",
    )
    parser.add_argument(
        "--start-paused",
        action="store_true",
        help="Submit the batch in paused state so the operator can inspect or stage before running",
    )
    parser.add_argument(
        "--allow-hidden-ticket",
        action="store_true",
        help="Opt in to hidden .cluster/tickets/ fallback if server still holds a duplicate canonical path",
    )
    parser.add_argument(
        "--min-headroom",
        type=float,
        default=50.0,
        help="Minimum free disk space (GB) required on Result share before submission (default: 50.0)",
    )
    parser.add_argument(
        "--discovery-depth",
        type=int,
        default=1,
        help="Folder discovery depth in --source-share mode (1=immediate subdirs, 0=recursive leaf dirs)",
    )
    parser.add_argument(
        "--no-reexec",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def find_photos_in_directory(images_dir: str, exclude_folders: str = "") -> List[str]:
    """Recursively search for supported image files in images_dir, skipping ignored/excluded folders."""
    photos: List[str] = []
    if not os.path.isdir(images_dir):
        return photos
    excl_pats = parse_exclude_folder_patterns(exclude_folders)
    for root, dirs, files in os.walk(images_dir):
        dirs[:] = [
            d
            for d in dirs
            if not _is_ignored_survey_dir(d) and not any(p in d.lower() for p in excl_pats)
        ]
        for f in sorted(files):
            if _is_supported_image_file(f):
                photos.append(os.path.normpath(os.path.join(root, f)))
    return photos


def resolve_server_address(cli_server: str) -> str:
    """Dynamically determine server host and port without hardcoded IPs."""
    if cli_server.strip():
        host, port = _split_host_port(cli_server.strip(), 5840)
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

    cfg = load_cluster_config(filename=DEFAULT_SERVER_CONFIG_FILENAME)
    if cfg.get("host") and cfg.get("host") != "0.0.0.0":
        host, port = _split_host_port(str(cfg["host"]), int(cfg.get("port", 5840) or 5840))
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"

    lan_ip = get_primary_lan_ip()
    if lan_ip and lan_ip != "127.0.0.1":
        return f"{lan_ip}:5840"

    return "127.0.0.1:5840"


def resolve_shared_root(cli_root: str, target_path: str) -> str:
    """Dynamically discover shared root UNC or filesystem path without hardcoding."""
    if cli_root.strip():
        return os.path.normpath(cli_root.strip())

    # If target_path is UNC (\\\\server\\share\\..., //server/share/..., or \\\\?\\UNC\\...), extract root share
    unc_root = extract_unc_root(target_path)
    if unc_root:
        return unc_root

    norm_target = os.path.normpath(target_path)
    cfg = load_cluster_config(filename=DEFAULT_SERVER_CONFIG_FILENAME)
    if cfg.get("root"):
        return os.path.normpath(cfg["root"])

    # Fallback to directory of target path
    return os.path.dirname(norm_target) if os.path.isfile(norm_target) else norm_target


def submit_batch(
    images_dir: str,
    output_dir: str = "",
    project_name: str = "",
    priority: int = 50,
    server_host: str = "",
    shared_root: str = "",
    resume: bool = False,
    no_local_scratch_staging: bool = False,
    preset: str = "pro",
    worker_limit: int = 1,
    no_guided_matching: bool = False,
    ortho_resolution: float = 0.05,
    keypoint_limit: Optional[int] = None,
    keypoint_limit_per_mpx: Optional[int] = None,
    tiepoint_limit: Optional[int] = None,
    build_point_cloud: Optional[bool] = None,
    exclude_folders: str = "",
    force: bool = False,
    start_paused: bool = False,
    allow_hidden_ticket: bool = False,
) -> int:
    """Create project, guarantee physical doc.save materialization, and submit cluster batch."""
    if Metashape is None:
        raise RuntimeError("Metashape Python module is not loaded. Must run via metashape.exe -r.")

    norm_images = os.path.normpath(images_dir)
    if not os.path.isdir(norm_images):
        raise FileNotFoundError(f"Input image directory does not exist: {norm_images}")

    proj_name = project_name.strip() or extract_path_leaf_name(norm_images)
    norm_output = os.path.normpath(output_dir.strip()) if output_dir.strip() else os.path.join(norm_images, "Result")
    os.makedirs(norm_output, exist_ok=True)

    psx_path = os.path.join(norm_output, f"{proj_name}.psx")
    resolved_root = resolve_shared_root(shared_root, psx_path)
    resolved_server = resolve_server_address(server_host)

    print("=" * 72, flush=True)
    print("  AutoMetashape: Canonical Cluster Batch Submission", flush=True)
    print(f"  Project Name     : {proj_name}", flush=True)
    print(f"  Images Directory : {norm_images}", flush=True)
    print(f"  Output Directory : {norm_output}", flush=True)
    print(f"  Project PSX      : {psx_path}", flush=True)
    print(f"  Cluster Server   : {resolved_server}", flush=True)
    print(f"  Shared Root      : {resolved_root}", flush=True)
    print(f"  Preset           : {preset}", flush=True)
    print(f"  Ortho Resolution : {ortho_resolution:.4f} m/px", flush=True)
    print(f"  Priority Integer : {priority}", flush=True)
    print(f"  Resume Checkpoint: {resume}", flush=True)
    print(f"  Direct Storage   : {no_local_scratch_staging}", flush=True)
    print("=" * 72, flush=True)

    # Calculate cluster-relative path and connect BEFORE modifying psx_path on disk so
    # re-running submit_cluster_batch.py never clobbers a live worker's active .psx archive.
    rel_psx = get_cluster_relative_path(psx_path, resolved_root)
    rel_output_dir = get_cluster_relative_path(norm_output, resolved_root)

    print(f"\n[CONNECT] Connecting to server at {resolved_server}...", flush=True)
    server_ip, server_port = _split_host_port(resolved_server, 5840)

    from autometashape.cluster.hardware import connect_network_client, normalize_rpc_list_response

    net_client = Metashape.NetworkClient()
    connect_network_client(net_client, server_ip, server_port)

    try:
        norm_target = _norm_cluster_path(rel_psx)
        raw_b = net_client.batchList()
        existing_batches = normalize_rpc_list_response(raw_b, "batches")
        aborted_ids: set[int] = set()
        for b in existing_batches:
            b_raw_path = b.get("path")
            b_canon_path = resolve_batch_canonical_path(b_raw_path, resolved_root)
            if _norm_cluster_path(b_raw_path) != norm_target and _norm_cluster_path(b_canon_path) != norm_target:
                continue
            b_state = str(b.get("state", "")).lower()
            b_err = int(b.get("error_count") or 0)
            is_active = b_state in (
                "active",
                "inprogress",
                "running",
                "submitted",
                "pending",
                "queued",
                "paused",
            ) and not (b.get("failed") or b.get("aborted"))
            if not force and is_active and b_err == 0 and b.get("batch_id") is not None:
                bid = int(b.get("batch_id"))
                print(
                    f"  [OK] Project '{proj_name}' already has healthy active Batch #{bid} ({b_state}); "
                    "attaching safely without overwriting .psx (pass --force to replace).",
                    flush=True,
                )
                return bid
            if force and b.get("batch_id") is not None and b_state not in ("completed", "finished"):
                # Abort active canonical or hidden-ticket batch BEFORE saving fresh .psx stub
                bid_abort = int(b.get("batch_id"))
                if is_active or b_state != "aborted":
                    _safe_abort_batch(net_client, bid_abort, is_paused=bool(b.get("paused") or b_state == "paused"))
                aborted_ids.add(bid_abort)

        if force:
            fb_id = _find_blocking_batch_id(net_client, rel_psx)
            if fb_id is not None:
                if fb_id not in aborted_ids:
                    _safe_abort_batch(net_client, fb_id, is_paused=True)
                    aborted_ids.add(fb_id)
            if aborted_ids:
                blocker = _wait_for_batch_path_release(
                    net_client,
                    rel_psx,
                    aborted_ids,
                    timeout_s=30.0,
                    shared_root=resolved_root,
                    allow_reap=True,
                )
                if blocker is not None and not allow_hidden_ticket:
                    raise RuntimeError(
                        f"Cannot replace '{rel_psx}': Metashape Server still holds batch #{blocker} after 30s. "
                        "Reap the worker holding that batch (scripts/cluster_abort.py) before retrying."
                    )
                if blocker is None:
                    clear_stale_project_lock(resolved_root, rel_psx)

        # -------------------------------------------------------------------------
        # MANDATORY ARCHITECTURAL INVARIANT: PHYSICAL PROJECT MATERIALIZATION
        # Always release .psx locks via doc.clear() before dispatching to workers.
        # -------------------------------------------------------------------------
        doc = Metashape.Document()
        try:
            if resume and os.path.isfile(psx_path):
                print(f"\n[CHECKPOINT] Verifying existing project archive: {psx_path}...", flush=True)
                try:
                    doc.open(psx_path, read_only=True, ignore_lock=True)
                except TypeError:
                    doc.open(psx_path)
                print(f"[OK] Checkpoint verified with {len(doc.chunks)} chunk(s).", flush=True)
            else:
                print("\n[INGEST] Verifying raw survey images...", flush=True)
                photos = find_photos_in_directory(norm_images, exclude_folders=exclude_folders)
                if not photos:
                    raise ValueError(f"No supported image files found under: {norm_images}")
                excl_note = f" (excluding folders: {exclude_folders})" if exclude_folders.strip() else ""
                print(
                    f"  Verified {len(photos)} photos in source folder{excl_note} (deferred ingest on worker).",
                    flush=True,
                )

                chunk = doc.addChunk()
                chunk.label = "Production_Reconstruction"

                # MANDATORY: Save minimal valid .psx stub to Result Share and verify existence BEFORE submission!
                print(f"  [MATERIALIZE] Saving project stub to shared disk: {psx_path}...", flush=True)
                doc.save(psx_path)
                if not os.path.isfile(psx_path):
                    raise IOError(
                        f"[FATAL ERROR] Project file was not created on disk: {psx_path}. "
                        "Aborting dispatch to protect workers from missing-file crashes."
                    )
                print(
                    f"  [OK] Physical .psx project archive materialized successfully ({os.path.getsize(psx_path)} bytes).",
                    flush=True,
                )
        finally:
            try:
                doc.clear()
            except Exception:
                pass

        # -------------------------------------------------------------------------
        # BUILD MONOLITHIC NETWORK TASK
        # -------------------------------------------------------------------------
        print("\n[DISPATCH] Constructing cluster network task...", flush=True)
        task_kwargs: dict[str, Any] = {
            "images_dir": norm_images,
            "output_dir": norm_output,
            "project_name": proj_name,
            "preset": preset,
            "resume": resume,
            "guided_matching": not no_guided_matching,
            "local_scratch": not no_local_scratch_staging,
            "network_path": resolved_root,
            "ortho_resolution": ortho_resolution,
        }
        if preset == "pro":
            # Allow worker Pro telemetry profiling to compute adaptive faces/textures/filtering/crop
            task_kwargs.setdefault("depth_filtering", None)
            task_kwargs.setdefault("custom_faces", None)
            task_kwargs.setdefault("texture_pages", None)
            task_kwargs.setdefault("crop_method", None)
        if keypoint_limit is not None:
            task_kwargs["keypoint_limit"] = keypoint_limit
        if keypoint_limit_per_mpx is not None:
            task_kwargs["keypoint_limit_per_mpx"] = keypoint_limit_per_mpx
        if tiepoint_limit is not None:
            task_kwargs["tiepoint_limit"] = tiepoint_limit
        if build_point_cloud is not None:
            task_kwargs["build_point_cloud"] = build_point_cloud
        if exclude_folders and exclude_folders.strip():
            task_kwargs["exclude_folders"] = exclude_folders.strip()

        res_task = build_monolithic_runscript_task(**task_kwargs)
        net_task = res_task[0] if isinstance(res_task, tuple) else res_task.toNetworkTask()

        # Submit batch via idempotent safe_create_cluster_batch:
        # - Applies priority & worker_limit BEFORE unpausing (zero priority race)
        # - Always dispatches under canonical <project>.psx (refusing silent ticket renames unless opted in)
        print(f"  Submitting batch for '{rel_psx}'...", flush=True)
        batch_id, attached_existing = safe_create_cluster_batch(
            net_client,
            rel_psx,
            [net_task],
            worker_limit=worker_limit,
            force_fresh=force,
            priority=priority,
            start_paused=start_paused,
            shared_root=resolved_root,
            ticket_project_name=proj_name,
            ticket_rel_output_dir=rel_output_dir,
            allow_hidden_ticket=allow_hidden_ticket,
        )
        if attached_existing:
            print(f"  [OK] Attached to existing healthy Batch #{batch_id}", flush=True)
        else:
            print(f"  [OK] Batch created successfully with ID: #{batch_id}", flush=True)

        print("=" * 72, flush=True)
        state_label = "staged (paused)" if start_paused else "active"
        print(
            f"[SUCCESS] Project '{proj_name}' is {state_label} on cluster as Batch #{batch_id} (Priority: {priority})!",
            flush=True,
        )
        print("=" * 72, flush=True)
        return batch_id
    finally:
        if hasattr(net_client, "disconnect"):
            try:
                net_client.disconnect()
            except Exception:
                pass


def main() -> int:
    """CLI entry point for cluster batch submission."""
    configure_utf8_stdio()
    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.images and not args.source_share:
        parser.error("Either --images or --source-share (with --result-share) must be provided.")
    if args.source_share and not args.result_share and not args.output:
        parser.error("--source-share requires --result-share (or --output) to define the writable Result SSD share.")

    # Plain Python Auto-Delegation
    if Metashape is None and not args.no_reexec:
        ms_exe = find_metashape_path()
        if ms_exe and os.path.isfile(ms_exe):
            cmd = [ms_exe]
            if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
                cmd.extend(["-platform", "offscreen"])
            cmd.extend(["-r", os.path.abspath(__file__), "--no-reexec"] + sys.argv[1:])
            res = subprocess.run(cmd)
            return res.returncode
        print("[ERROR] Agisoft Metashape Pro executable not found. Cannot submit cluster batch.", file=sys.stderr)
        return 1
    if Metashape is None:
        print("[FATAL ERROR] Agisoft Metashape Python module is unavailable in this runtime.", file=sys.stderr)
        return 1

    try:
        if args.source_share:
            result_share = args.result_share or args.output
            topo = validate_dual_share_topology(
                args.source_share,
                result_share,
                result_network_root=args.root,
                min_headroom_gb=args.min_headroom,
            )
            if not topo["valid"]:
                for err in topo["errors"]:
                    print(f"[FATAL ERROR] {err}", file=sys.stderr, flush=True)
                return 1
            for warn in topo.get("warnings", []):
                print(f"[WARNING] {warn}", flush=True)

            resolved_root = resolve_result_network_root(result_share, explicit_root=args.root)
            if args.discovery_depth < 0:
                print(
                    f"[FATAL ERROR] --discovery-depth must be >= 0 (got {args.discovery_depth}).",
                    file=sys.stderr,
                )
                return 1
            disc_kwargs: dict[str, Any] = {"result_network_root": resolved_root}
            if args.discovery_depth != 1:
                disc_kwargs["discovery_depth"] = args.discovery_depth
            if args.exclude_folders:
                disc_kwargs["exclude_folders"] = args.exclude_folders
            projects = discover_dual_share_projects(
                args.source_share,
                result_share,
                **disc_kwargs,
            )
            excl_pats = [
                unicodedata.normalize("NFC", p).lower()
                for p in parse_exclude_folder_patterns(args.exclude_folders)
            ]
            if excl_pats and projects:
                filtered_projects = []
                for proj in projects:
                    rel_p = relpath_under(proj.images_dir, args.source_share)
                    if rel_p is None:
                        try:
                            rel_p = os.path.relpath(proj.images_dir, args.source_share).replace("\\", "/")
                        except ValueError:
                            rel_p = proj.images_dir.replace("\\", "/")
                    rel_parts = unicodedata.normalize("NFC", rel_p).lower().split("/")
                    if any(any(pat in seg for pat in excl_pats) for seg in rel_parts):
                        continue
                    filtered_projects.append(proj)
                projects = filtered_projects
            if not projects:
                print(
                    f"[FATAL ERROR] No image datasets discovered under source share: {args.source_share}",
                    file=sys.stderr,
                )
                return 1

            submitted_ids: List[int] = []
            failed_projects: List[str] = []
            for proj in projects:
                try:
                    bid = submit_batch(
                        images_dir=proj.images_dir,
                        output_dir=proj.output_dir,
                        project_name=args.project_name
                        if len(projects) == 1 and args.project_name
                        else proj.project_name,
                        priority=args.priority,
                        server_host=args.server,
                        shared_root=resolved_root,
                        resume=args.resume,
                        no_local_scratch_staging=args.no_local_scratch_staging,
                        preset=args.preset,
                        worker_limit=args.worker_limit,
                        no_guided_matching=args.no_guided_matching,
                        ortho_resolution=args.ortho_resolution,
                        keypoint_limit=args.keypoint_limit,
                        keypoint_limit_per_mpx=args.keypoint_limit_per_mpx,
                        tiepoint_limit=args.tiepoint_limit,
                        build_point_cloud=args.build_point_cloud,
                        exclude_folders=args.exclude_folders,
                        force=args.force,
                        start_paused=args.start_paused,
                        allow_hidden_ticket=args.allow_hidden_ticket,
                    )
                    if bid >= 0:
                        submitted_ids.append(bid)
                except Exception as proj_exc:
                    failed_projects.append(f"{proj.project_name}: {proj_exc}")
                    print(
                        f"[ERROR] Failed to submit project '{proj.project_name}': {proj_exc}. Continuing...",
                        file=sys.stderr,
                        flush=True,
                    )
            if failed_projects:
                print(
                    f"[SUMMARY] Submitted {len(submitted_ids)}/{len(projects)} project(s); {len(failed_projects)} failed.",
                    file=sys.stderr,
                    flush=True,
                )
            return 0 if len(submitted_ids) == len(projects) else 1

        effective_root = args.root
        if args.result_share:
            effective_root = resolve_result_network_root(args.result_share, explicit_root=args.root)
        if args.output:
            effective_output = args.output
        elif args.result_share:
            single_proj_name = args.project_name.strip() or extract_path_leaf_name(os.path.normpath(args.images))
            effective_output = os.path.join(os.path.normpath(args.result_share), single_proj_name)
        else:
            effective_output = ""

        # Win32 Error 112 defense in single-project mode: verify headroom on result share / output volume
        min_hr = float(getattr(args, "min_headroom", 0.0) or 0.0)
        if min_hr > 0.0:
            from autometashape.cluster.supervisor import check_storage_headroom

            hr_target = effective_root.strip() or effective_output.strip() or args.images
            if hr_target:
                hr_ok, hr_free = check_storage_headroom(hr_target, min_headroom_gb=min_hr)
                if not hr_ok:
                    print(
                        f"[FATAL ERROR] Insufficient storage headroom on '{hr_target}': "
                        f"{hr_free:.1f} GB free < {min_hr:.1f} GB required.",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 1

        batch_id = submit_batch(
            images_dir=args.images,
            output_dir=effective_output,
            project_name=args.project_name,
            priority=args.priority,
            server_host=args.server,
            shared_root=effective_root,
            resume=args.resume,
            no_local_scratch_staging=args.no_local_scratch_staging,
            preset=args.preset,
            worker_limit=args.worker_limit,
            no_guided_matching=args.no_guided_matching,
            ortho_resolution=args.ortho_resolution,
            keypoint_limit=args.keypoint_limit,
            keypoint_limit_per_mpx=args.keypoint_limit_per_mpx,
            tiepoint_limit=args.tiepoint_limit,
            build_point_cloud=args.build_point_cloud,
            exclude_folders=args.exclude_folders,
            force=args.force,
            start_paused=args.start_paused,
            allow_hidden_ticket=args.allow_hidden_ticket,
        )
        return 0 if batch_id >= 0 else 1
    except Exception as e:
        print(f"\n[FATAL ERROR] Cluster batch submission failed: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
