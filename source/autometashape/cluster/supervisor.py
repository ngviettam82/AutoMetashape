# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Cluster queue supervisor, storage headroom watchdog, and deliverable verification gate.

Provides unattended runtime supervision, continuous storage exhaustion defense (Win32 Error 112),
post-reconstruction deliverable verification, and autonomous multi-dataset queue chaining.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from autometashape.cluster.staging import get_optimal_local_scratch_dir
from autometashape.core.verification import verify_project_deliverables


def check_storage_headroom(target_path: str, min_headroom_gb: float = 50.0) -> Tuple[bool, float]:
    """Inspects available disk storage headroom on the given filesystem volume.

    Args:
        target_path: File or directory path to inspect.
        min_headroom_gb: Minimum free space required in Gigabytes.

    Returns:
        Tuple of (has_sufficient_headroom: bool, free_gb: float).
    """
    try:
        inspect_dir = target_path
        if os.path.isfile(inspect_dir) or not os.path.exists(inspect_dir):
            inspect_dir = os.path.dirname(os.path.abspath(inspect_dir))
        while inspect_dir and not os.path.exists(inspect_dir):
            parent = os.path.dirname(inspect_dir)
            if parent == inspect_dir:
                break
            inspect_dir = parent

        usage = shutil.disk_usage(inspect_dir)
        free_gb = usage.free / (1024.0**3)
        return free_gb >= min_headroom_gb, round(free_gb, 2)
    except Exception:
        # Fallback if path is remote or unresolvable
        return True, 999.0


class QueueJob:
    """Represents a discrete reconstruction dataset job in the supervisor queue."""

    def __init__(
        self,
        images_dir: str,
        output_dir: Optional[str] = None,
        project_name: Optional[str] = None,
        preset: str = "pro",
        expected_assets: Optional[List[str]] = None,
        extra_args: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.images_dir = os.path.abspath(images_dir)
        self.project_name = project_name or os.path.basename(self.images_dir.rstrip("\\/"))
        self.output_dir = os.path.abspath(output_dir) if output_dir else os.path.join(self.images_dir, self.project_name)
        self.preset = preset
        self.expected_assets = expected_assets or ["obj", "dem", "ortho"]
        self.extra_args = extra_args or {}
        self.status: str = "PENDING"
        self.verification_report: Optional[Dict[str, Any]] = None
        self.error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "images_dir": self.images_dir,
            "output_dir": self.output_dir,
            "project_name": self.project_name,
            "preset": self.preset,
            "expected_assets": self.expected_assets,
            "status": self.status,
            "verification_report": self.verification_report,
            "error_message": self.error_message,
        }


class ClusterSupervisor:
    """Unattended cluster queue supervisor, watchdog, and deliverable verification manager."""

    def __init__(
        self,
        min_headroom_gb: float = 50.0,
        stop_on_failure: bool = False,
        scratch_dir: Optional[str] = None,
    ) -> None:
        self.min_headroom_gb = min_headroom_gb
        self.stop_on_failure = stop_on_failure
        self.scratch_dir = scratch_dir or get_optimal_local_scratch_dir()
        self.queue: List[QueueJob] = []

    def add_job(
        self,
        images_dir: str,
        output_dir: Optional[str] = None,
        project_name: Optional[str] = None,
        preset: str = "pro",
        expected_assets: Optional[List[str]] = None,
        extra_args: Optional[Dict[str, Any]] = None,
    ) -> QueueJob:
        """Enqueues a photogrammetry dataset reconstruction job."""
        job = QueueJob(
            images_dir=images_dir,
            output_dir=output_dir,
            project_name=project_name,
            preset=preset,
            expected_assets=expected_assets,
            extra_args=extra_args,
        )
        self.queue.append(job)
        return job

    def run_queue(self, execute_fn: Optional[Any] = None) -> Dict[str, Any]:
        """Processes all queued dataset jobs sequentially with storage watchdogs and verification gates.

        Args:
            execute_fn: Optional callable(job: QueueJob) -> None to execute reconstruction.
                        If None, imports and delegates to autometashape.core.pipeline.run_reconstruction.

        Returns:
            Summary dictionary with execution statistics and per-job verification reports.
        """
        print("=========================================================================")
        print("  AUTOMETASHAPE: AUTONOMOUS CLUSTER SUPERVISOR & QUEUE MANAGER")
        print(f"  Queued Datasets     : {len(self.queue)}")
        print(f"  Headroom Threshold  : {self.min_headroom_gb:.1f} GB")
        print(f"  Local Scratch Volume: {self.scratch_dir}")
        print("=========================================================================", flush=True)

        results: List[Dict[str, Any]] = []

        for idx, job in enumerate(self.queue, 1):
            print(f"\n[{idx}/{len(self.queue)}] Supervising Dataset: {job.project_name}")
            print(f"  * Photos Source: {job.images_dir}")
            print(f"  * Output Target: {job.output_dir}", flush=True)

            # 1. Pre-Flight Storage Headroom Watchdog
            has_headroom, free_gb = check_storage_headroom(job.output_dir, self.min_headroom_gb)
            if not has_headroom:
                err = (
                    f"Storage exhaustion hazard (Win32 Error 112 defense): "
                    f"Volume has only {free_gb:.1f} GB free (threshold: {self.min_headroom_gb:.1f} GB). "
                    f"Pausing job to prevent data corruption."
                )
                print(f"  [STORAGE ALERT] {err}", file=sys.stderr, flush=True)
                job.status = "PAUSED_LOW_DISK"
                job.error_message = err
                results.append(job.to_dict())
                if self.stop_on_failure:
                    print("  [SUPERVISOR] Stopping queue execution as stop_on_failure=True.", flush=True)
                    break
                continue

            # 2. Execute Reconstruction Pipeline
            job.status = "RUNNING"
            t_start = time.time()
            try:
                if execute_fn is not None:
                    execute_fn(job)
                else:
                    from autometashape.core.pipeline import run_reconstruction

                    run_reconstruction(
                        images_dir=job.images_dir,
                        output_dir=job.output_dir,
                        project_name=job.project_name,
                        preset=job.preset,
                        local_scratch_staging=True,
                        scratch_dir=self.scratch_dir,
                        **job.extra_args,
                    )
            except Exception as exc:
                err_msg = f"Reconstruction pipeline crashed: {exc}"
                print(f"  [PIPELINE ERROR] {err_msg}", file=sys.stderr, flush=True)
                job.status = "FAILED"
                job.error_message = err_msg
                results.append(job.to_dict())
                if self.stop_on_failure:
                    break
                continue

            elapsed_min = (time.time() - t_start) / 60.0

            # 3. Deliverable Quality & Integrity Gate
            print("  [QUALITY GATE] Auditing deliverable integrity...", flush=True)
            report = verify_project_deliverables(
                deliverables_dir=job.output_dir,
                project_name=job.project_name,
                expected_assets=job.expected_assets,
            )
            job.verification_report = report

            if report["status"] == "PASS":
                job.status = "COMPLETED"
                print(f"  [QUALITY GATE] [OK] Deliverables verified successfully ({elapsed_min:.2f} min).", flush=True)
            else:
                job.status = "VERIFICATION_FAILED"
                job.error_message = "; ".join(report["errors"])
                print(f"  [QUALITY GATE] [FAIL] Deliverable verification failed: {job.error_message}", file=sys.stderr, flush=True)

            results.append(job.to_dict())

            # 4. Scratch Staging Cleanup between Queue Items
            if job.status == "COMPLETED" and self.scratch_dir and os.path.isdir(self.scratch_dir):
                staged_sub = os.path.join(self.scratch_dir, f"{job.project_name}_local_stage")
                if os.path.isdir(staged_sub):
                    try:
                        shutil.rmtree(staged_sub, ignore_errors=True)
                        print(f"  [SUPERVISOR] Cleared local scratch staging: {staged_sub}", flush=True)
                    except Exception:
                        pass

            if job.status != "COMPLETED" and self.stop_on_failure:
                print("  [SUPERVISOR] Halting queue execution due to job failure.", flush=True)
                break

        passed_count = sum(1 for r in results if r["status"] == "COMPLETED")
        print("\n=========================================================================")
        print("  AUTOMETASHAPE: QUEUE SUPERVISION RUN SUMMARY")
        print(f"  Total Queued: {len(self.queue)} | Completed & Verified: {passed_count} | Failed/Paused: {len(results) - passed_count}")
        print("=========================================================================", flush=True)

        return {
            "total_jobs": len(self.queue),
            "completed_jobs": passed_count,
            "jobs": results,
        }


SUPPORTED_IMAGE_EXTS = (".jpg", ".jpeg", ".tif", ".tiff", ".png", ".dng")


def discover_dataset_projects(
    scan_dir: str,
    output_root: Optional[str] = None,
    preset: str = "pro",
    expected_assets: Optional[List[str]] = None,
    extra_args: Optional[Dict[str, Any]] = None,
) -> List[QueueJob]:
    """Recursively scans a root directory to discover discrete survey dataset projects.

    If scan_dir contains image-bearing subdirectories, each subfolder is queued as a separate project.
    If scan_dir directly contains survey images, it is queued as a single project.

    Args:
        scan_dir: Root directory to scan for datasets.
        output_root: Optional target directory where output deliverables folders will be placed.
        preset: Reconstruction preset ('pro', 'custom', 'building').
        expected_assets: Optional list of asset types expected for deliverable verification.
        extra_args: Additional CLI/pipeline arguments forwarded to run_reconstruction.

    Returns:
        List of initialized QueueJob objects.
    """
    scan_dir = os.path.abspath(scan_dir)
    if not os.path.isdir(scan_dir):
        raise ValueError(f"Scan directory does not exist or is not a directory: {scan_dir}")

    extra = extra_args or {}
    jobs: List[QueueJob] = []

    def count_images_in_dir(d: str) -> int:
        count = 0
        try:
            for root, _, files in os.walk(d):
                for f in files:
                    if f.lower().endswith(SUPPORTED_IMAGE_EXTS):
                        count += 1
                        if count >= 1:
                            return count
        except Exception:
            pass
        return count

    excluded_names = {"model", "models", "output", "outputs", "deliverables", "temp", "tmp", "__pycache__"}

    # 1. Inspect immediate subdirectories
    subdirs = []
    try:
        for entry in sorted(os.listdir(scan_dir)):
            full_p = os.path.join(scan_dir, entry)
            if os.path.isdir(full_p) and not entry.startswith((".", "$")):
                if entry.lower() not in excluded_names and not entry.lower().startswith("model"):
                    subdirs.append((entry, full_p))
    except Exception as exc:
        raise ValueError(f"Failed to list scan directory '{scan_dir}': {exc}")

    for folder_name, folder_path in subdirs:
        img_count = count_images_in_dir(folder_path)
        if img_count > 0:
            out_dir = os.path.join(output_root, folder_name) if output_root else os.path.join(folder_path, folder_name)
            job = QueueJob(
                images_dir=folder_path,
                output_dir=out_dir,
                project_name=folder_name,
                preset=preset,
                expected_assets=expected_assets,
                extra_args=dict(extra),
            )
            jobs.append(job)

    # 2. If no subdirectories had images, check if root itself contains images
    if not jobs:
        root_images = [
            f
            for f in os.listdir(scan_dir)
            if os.path.isfile(os.path.join(scan_dir, f)) and f.lower().endswith(SUPPORTED_IMAGE_EXTS)
        ]
        if root_images:
            project_name = os.path.basename(scan_dir.rstrip("\\/"))
            out_dir = os.path.join(output_root, project_name) if output_root else os.path.join(scan_dir, project_name)
            job = QueueJob(
                images_dir=scan_dir,
                output_dir=out_dir,
                project_name=project_name,
                preset=preset,
                expected_assets=expected_assets,
                extra_args=dict(extra),
            )
            jobs.append(job)

    return jobs


def build_arg_parser() -> argparse.ArgumentParser:
    """CLI argument parser for cluster supervisor."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Autonomous Cluster Queue Supervisor & Deliverable Verification Gate"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--manifest",
        "--queue",
        dest="manifest",
        default=None,
        help="Path to JSON manifest listing datasets to reconstruct sequentially",
    )
    group.add_argument(
        "--scan-dir",
        "--dataset-dir",
        dest="scan_dir",
        default=None,
        help="Root directory containing survey datasets to auto-discover and queue",
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help="Root directory where output deliverables folders will be created for auto-discovered datasets",
    )
    parser.add_argument(
        "--preset",
        choices=["pro", "custom", "building"],
        default="pro",
        help="Reconstruction preset for discovered datasets (default: pro)",
    )
    parser.add_argument(
        "--expect",
        nargs="+",
        choices=["obj", "glb", "dem", "ortho", "point_cloud", "report"],
        default=None,
        help="List of deliverable asset types that must be present and verified",
    )
    parser.add_argument(
        "--min-headroom-gb",
        type=float,
        default=50.0,
        help="Minimum free disk space (GB) on output volume before pausing (default: 50.0)",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="Halt entire queue execution immediately if any dataset fails verification",
    )
    # Distributed Cluster Passthrough Flags
    parser.add_argument(
        "--network-enable",
        action="store_true",
        default=False,
        help="Enable distributed network processing across worker nodes for all queued jobs",
    )
    parser.add_argument(
        "--network-host",
        default="127.0.0.1",
        help="Cluster coordinator server host IP or hostname (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--network-port",
        type=int,
        default=5840,
        help="Cluster coordinator server port (default: 5840)",
    )
    parser.add_argument(
        "--network-independent-workers",
        action="store_true",
        default=False,
        help="Lock each project to an independent worker node (worker_limit=1)",
    )
    return parser


def main() -> int:
    """CLI entry point for supervisor."""
    parser = build_arg_parser()
    args = parser.parse_args()

    supervisor = ClusterSupervisor(
        min_headroom_gb=args.min_headroom_gb,
        stop_on_failure=args.stop_on_failure,
    )

    extra_args: Dict[str, Any] = {}
    if args.network_enable:
        extra_args["network_enable"] = True
        extra_args["network_host"] = args.network_host
        extra_args["network_port"] = args.network_port
        extra_args["network_independent_workers"] = args.network_independent_workers

    if args.manifest:
        if not os.path.isfile(args.manifest):
            print(f"[FATAL] Manifest file not found: {args.manifest}", file=sys.stderr)
            return 1

        try:
            with open(args.manifest, "r", encoding="utf-8") as f:
                queue_data = json.load(f)
        except Exception as exc:
            print(f"[FATAL] Failed to parse JSON manifest: {exc}", file=sys.stderr)
            return 1

        if not isinstance(queue_data, list):
            print("[FATAL] Manifest JSON must be a list of dataset job objects.", file=sys.stderr)
            return 1

        for item in queue_data:
            merged_extra = dict(extra_args)
            if item.get("extra_args"):
                merged_extra.update(item["extra_args"])
            supervisor.add_job(
                images_dir=item["images_dir"],
                output_dir=item.get("output_dir"),
                project_name=item.get("project_name"),
                preset=item.get("preset", "pro"),
                expected_assets=item.get("expected_assets"),
                extra_args=merged_extra,
            )

    elif args.scan_dir:
        try:
            jobs = discover_dataset_projects(
                scan_dir=args.scan_dir,
                output_root=args.output_root,
                preset=args.preset,
                expected_assets=args.expect,
                extra_args=extra_args,
            )
        except Exception as exc:
            print(f"[FATAL] Failed to discover datasets in '{args.scan_dir}': {exc}", file=sys.stderr)
            return 1

        if not jobs:
            print(f"[WARNING] No survey image datasets found in '{args.scan_dir}'.", file=sys.stderr)
            return 0

        supervisor.queue.extend(jobs)

    summary = supervisor.run_queue()
    return 0 if summary["completed_jobs"] == summary["total_jobs"] else 1


if __name__ == "__main__":
    sys.exit(main())
