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

from autometashape.cluster.config import (
    DEFAULT_SERVER_CONFIG_FILENAME,
    load_cluster_config,
)
from autometashape.cluster.hardware import find_metashape_path, get_primary_lan_ip
from autometashape.cluster.orchestrator import (
    _norm_cluster_path,
    build_monolithic_runscript_task,
)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".dng", ".png"}


def get_cluster_relative_path(target_path: str, shared_root: str) -> str:
    """Compute cluster-relative POSIX path for Metashape Network Server."""
    if shared_root:
        try:
            rel = os.path.relpath(target_path, shared_root).replace("\\", "/")
            if not rel.startswith(".."):
                return rel
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
        required=True,
        help="Path to folder containing raw capture images (UNC or local path)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="",
        help="Target output directory for deliverables and .psx archive (default: <images>/Result)",
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
        help="Shared cluster storage root path (default: auto-detected from server config or image path)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume execution from existing .psx checkpoint if present in output folder",
    )
    parser.add_argument(
        "--no-local-scratch-staging",
        action="store_true",
        help="Bypass NVMe local scratch copy and process directly on shared storage (mandatory for host data)",
    )
    parser.add_argument(
        "--preset",
        type=str,
        default="pro",
        choices=["fast", "standard", "pro", "ultra"],
        help="Reconstruction quality preset",
    )
    parser.add_argument(
        "--worker-limit",
        type=int,
        default=1,
        help="Maximum concurrent workers assigned to this batch (default: 1 for monolithic batch)",
    )
    parser.add_argument(
        "--no-reexec",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def find_photos_in_directory(images_dir: str) -> List[str]:
    """Recursively search for supported image files in images_dir."""
    photos: List[str] = []
    if not os.path.isdir(images_dir):
        return photos
    for root, _, files in os.walk(images_dir):
        for f in sorted(files):
            ext = os.path.splitext(f)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                photos.append(os.path.normpath(os.path.join(root, f)))
    return photos


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


def resolve_shared_root(cli_root: str, target_path: str) -> str:
    """Dynamically discover shared root UNC or filesystem path without hardcoding."""
    if cli_root.strip():
        return os.path.normpath(cli_root.strip())

    # If target_path is UNC (\\\\server\\share\\...), extract the root share directly
    norm_target = os.path.normpath(target_path)
    if norm_target.startswith("\\\\"):
        parts = norm_target.split("\\")
        if len(parts) >= 4:
            return f"\\\\{parts[2]}\\{parts[3]}"

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
) -> int:
    """Create project, guarantee physical doc.save materialization, and submit cluster batch."""
    if Metashape is None:
        raise RuntimeError("Metashape Python module is not loaded. Must run via metashape.exe -r.")

    norm_images = os.path.normpath(images_dir)
    if not os.path.isdir(norm_images):
        raise FileNotFoundError(f"Input image directory does not exist: {norm_images}")

    proj_name = project_name.strip() or os.path.basename(norm_images)
    norm_output = os.path.normpath(output_dir.strip()) if output_dir.strip() else os.path.join(norm_images, "Result")
    os.makedirs(norm_output, exist_ok=True)

    psx_path = os.path.join(norm_output, f"{proj_name}.psx")
    resolved_root = resolve_shared_root(shared_root, psx_path)
    resolved_server = resolve_server_address(server_host)

    print("=" * 72, flush=True)
    print("  AutoMetashape: Canonical Cluster Batch Submission", flush=True)
    print(f"  Project Name    : {proj_name}", flush=True)
    print(f"  Images Directory: {norm_images}", flush=True)
    print(f"  Output Directory: {norm_output}", flush=True)
    print(f"  Project PSX     : {psx_path}", flush=True)
    print(f"  Cluster Server  : {resolved_server}", flush=True)
    print(f"  Shared Root     : {resolved_root}", flush=True)
    print(f"  Priority Integer: {priority}", flush=True)
    print(f"  Resume Checkpoint: {resume}", flush=True)
    print(f"  Direct Storage  : {no_local_scratch_staging}", flush=True)
    print("=" * 72, flush=True)

    # -------------------------------------------------------------------------
    # MANDATORY ARCHITECTURAL INVARIANT: PHYSICAL PROJECT MATERIALIZATION
    # -------------------------------------------------------------------------
    doc = Metashape.Document()
    if resume and os.path.isfile(psx_path):
        print(f"\n[CHECKPOINT] Opening existing project archive: {psx_path}...", flush=True)
        doc.open(psx_path)
        print(f"[OK] Checkpoint opened with {len(doc.chunks)} chunk(s).", flush=True)
    else:
        print("\n[INGEST] Scanning raw survey images...", flush=True)
        photos = find_photos_in_directory(norm_images)
        if not photos:
            raise ValueError(f"No supported image files found under: {norm_images}")
        print(f"  Found {len(photos)} photos.", flush=True)

        chunk = doc.addChunk()
        chunk.label = "Production_Reconstruction"
        print(f"  Adding {len(photos)} photos to canonical chunk '{chunk.label}'...", flush=True)
        chunk.addPhotos(photos)

        # MANDATORY: Save doc to disk and verify physical file existence BEFORE submission!
        print(f"  [MATERIALIZE] Saving document to shared disk: {psx_path}...", flush=True)
        doc.save(psx_path)
        if not os.path.isfile(psx_path):
            raise IOError(
                f"[FATAL ERROR] Project file was not created on disk: {psx_path}. "
                "Aborting dispatch to protect workers from missing-file crashes."
            )
        print(f"  [OK] Physical .psx project archive materialized successfully ({os.path.getsize(psx_path)} bytes).", flush=True)

    # -------------------------------------------------------------------------
    # BUILD MONOLITHIC NETWORK TASK
    # -------------------------------------------------------------------------
    print("\n[DISPATCH] Constructing cluster network task...", flush=True)
    script_task = build_monolithic_runscript_task(
        images_dir=norm_images,
        output_dir=norm_output,
        project_name=proj_name,
        preset=preset,
        resume=resume,
        local_scratch=not no_local_scratch_staging,
    )
    net_task = script_task.toNetworkTask()

    # Calculate cluster-relative path
    rel_psx = get_cluster_relative_path(psx_path, resolved_root)

    # Connect to Metashape Server
    print(f"  Connecting to server at {resolved_server}...", flush=True)
    server_ip, server_port_str = resolved_server.split(":")
    server_port = int(server_port_str)

    net_client = Metashape.NetworkClient()
    net_client.connect(server_ip, server_port)

    # Submit batch
    print(f"  Submitting batch for '{rel_psx}'...", flush=True)
    batch_id = net_client.createBatch(rel_psx, [net_task])
    print(f"  [OK] Batch created successfully with ID: #{batch_id}", flush=True)

    # Configure priority and worker limits
    try:
        net_client.setBatchPriority(batch_id, priority)
    except Exception as e_prio:
        print(f"  [Notice] Could not set priority ({e_prio})", flush=True)

    if hasattr(net_client, "setBatchWorkerLimit"):
        try:
            net_client.setBatchWorkerLimit(batch_id, worker_limit)
        except Exception:
            pass

    # Ensure batch is unpaused
    try:
        net_client.setBatchPaused(batch_id, False)
    except Exception:
        pass

    print("=" * 72, flush=True)
    print(f"[SUCCESS] Project '{proj_name}' is active on cluster as Batch #{batch_id} (Priority: {priority})!", flush=True)
    print("=" * 72, flush=True)
    return batch_id


def main() -> int:
    """CLI entry point for cluster batch submission."""
    parser = build_arg_parser()
    args = parser.parse_args()

    # Plain Python Auto-Delegation
    if Metashape is None and not args.no_reexec:
        ms_exe = find_metashape_path()
        if ms_exe and os.path.isfile(ms_exe):
            cmd = [ms_exe, "-r", os.path.abspath(__file__)] + sys.argv[1:] + ["--no-reexec"]
            res = subprocess.run(cmd)
            return res.returncode
        print("[ERROR] Agisoft Metashape Pro executable not found. Cannot submit cluster batch.", file=sys.stderr)
        return 1

    try:
        batch_id = submit_batch(
            images_dir=args.images,
            output_dir=args.output,
            project_name=args.project_name,
            priority=args.priority,
            server_host=args.server,
            shared_root=args.root,
            resume=args.resume,
            no_local_scratch_staging=args.no_local_scratch_staging,
            preset=args.preset,
            worker_limit=args.worker_limit,
        )
        return 0 if batch_id > 0 else 1
    except Exception as e:
        print(f"\n[FATAL ERROR] Cluster batch submission failed: {e}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
