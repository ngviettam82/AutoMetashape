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
import concurrent.futures
import inspect
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

from autometashape.cluster.dual_share import (
    discover_dual_share_projects,
    is_subpath,
    validate_dual_share_topology,
)
from autometashape.cluster.hardware import extract_unc_root, find_metashape_path
from autometashape.cluster.orchestrator import (
    build_monolithic_runscript_task,
    evaluate_cluster_batch_info,
    safe_create_cluster_batch,
)
from autometashape.cluster.staging import get_optimal_local_scratch_dir
from autometashape.core.telemetry import SUPPORTED_IMAGE_EXTS
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


def _derive_default_expected_assets(extra_args: Optional[Dict[str, Any]] = None) -> List[str]:
    """Derive default deliverable verification targets from the job's enabled export flags."""
    extra = extra_args or {}
    assets: List[str] = []
    if extra.get("export_obj", True):
        assets.append("obj")
    if extra.get("export_glb", False):
        assets.append("glb")
    if extra.get("export_dem", False):
        assets.append("dem")
    if extra.get("export_ortho", True):
        assets.append("ortho")
    if extra.get("export_point_cloud", False):
        assets.append("point_cloud")
    if extra.get("export_report", False):
        assets.append("report")
    return assets or ["obj", "ortho"]


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
        self.output_dir = (
            os.path.abspath(output_dir) if output_dir else os.path.join(self.images_dir, self.project_name)
        )
        self.preset = preset
        self.extra_args = extra_args or {}
        self.expected_assets = (
            list(expected_assets) if expected_assets is not None else _derive_default_expected_assets(self.extra_args)
        )
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
        concurrent: bool = False,
    ) -> None:
        self.min_headroom_gb = min_headroom_gb
        self.stop_on_failure = stop_on_failure
        self.scratch_dir = scratch_dir or get_optimal_local_scratch_dir()
        self.concurrent = concurrent
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

    def run_queue(
        self,
        execute_fn: Optional[Any] = None,
        concurrent: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Processes all queued dataset jobs sequentially with storage watchdogs and verification gates.

        Args:
            execute_fn: Optional callable(job: QueueJob) -> None to execute reconstruction.
                        If None, imports and delegates to autometashape.core.pipeline.run_reconstruction.
            concurrent: Optional bool to override instance-level self.concurrent mode.
                        If True, delegates to self.run_concurrent_queue(execute_fn=execute_fn).

        Returns:
            Summary dictionary with execution statistics and per-job verification reports.
        """
        is_concurrent = self.concurrent if concurrent is None else concurrent
        if is_concurrent:
            return self.run_concurrent_queue(execute_fn=execute_fn)

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
                # Auto-detect network_path from UNC path if not provided
                if job.extra_args.get("network_enable") and not job.extra_args.get("network_path"):
                    detected_unc = extract_unc_root(job.output_dir or "") or extract_unc_root(job.images_dir or "")
                    if detected_unc:
                        job.extra_args["network_path"] = detected_unc
                        print(
                            f"  [Cluster Supervisor] Auto-detected cluster storage root from UNC: {detected_unc}",
                            flush=True,
                        )

                if execute_fn is not None:
                    execute_fn(job)
                else:
                    from autometashape.core.pipeline import run_reconstruction

                    sig_params = inspect.signature(run_reconstruction).parameters
                    reserved_keys = {
                        "images_dir",
                        "output_dir",
                        "project_name",
                        "preset",
                        "local_scratch_staging",
                        "scratch_dir",
                    }
                    is_net = bool(job.extra_args.get("network_enable"))
                    forward_kwargs = {
                        k: v for k, v in job.extra_args.items() if k in sig_params and k not in reserved_keys
                    }
                    if not is_net:
                        # Local sequential execution does not use Metashape Network Processing;
                        # omitting network_path ensures pipeline.py runs local discrete GPU selection.
                        forward_kwargs.pop("network_path", None)
                        use_local_scratch = bool(
                            job.extra_args.get(
                                "local_scratch_staging",
                                job.extra_args.get("local_scratch", True),
                            )
                        )
                    else:
                        use_local_scratch = False

                    run_reconstruction(
                        images_dir=job.images_dir,
                        output_dir=job.output_dir,
                        project_name=job.project_name,
                        preset=job.preset,
                        local_scratch_staging=use_local_scratch,
                        scratch_dir=self.scratch_dir,
                        **forward_kwargs,
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
                print(
                    f"  [QUALITY GATE] [FAIL] Deliverable verification failed: {job.error_message}",
                    file=sys.stderr,
                    flush=True,
                )

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
        print(
            f"  Total Queued: {len(self.queue)} | Completed & Verified: {passed_count} | Failed/Paused: {len(results) - passed_count}"
        )
        print("=========================================================================", flush=True)

        return {
            "total_jobs": len(self.queue),
            "completed_jobs": passed_count,
            "jobs": results,
        }

    def _run_concurrent_mock_queue(
        self,
        execute_fn: Any,
        max_workers: int = 8,
    ) -> Dict[str, Any]:
        """Executes queued jobs concurrently via thread pool with pre-flight watchdogs and verification gates."""
        print("=========================================================================")
        print("  AUTOMETASHAPE: CONCURRENT MULTI-PROJECT MOCK QUEUE DISPATCH")
        print(f"  Queued Datasets     : {len(self.queue)}")
        print(f"  Headroom Threshold  : {self.min_headroom_gb:.1f} GB")
        print(f"  Max Concurrent Slots: {max_workers}")
        print("=========================================================================", flush=True)

        results: List[Dict[str, Any]] = []
        eligible_jobs: List[QueueJob] = []

        # Phase 1: Pre-flight Storage Headroom Watchdog
        for job in self.queue:
            has_headroom, free_gb = check_storage_headroom(job.output_dir, self.min_headroom_gb)
            if not has_headroom:
                err = (
                    f"Storage exhaustion hazard (Win32 Error 112 defense): "
                    f"Volume has only {free_gb:.1f} GB free (threshold: {self.min_headroom_gb:.1f} GB). "
                    f"Pausing job to prevent data corruption."
                )
                print(f"  [STORAGE ALERT] [{job.project_name}] {err}", file=sys.stderr, flush=True)
                job.status = "PAUSED_LOW_DISK"
                job.error_message = err
                results.append(job.to_dict())
                if self.stop_on_failure:
                    print("  [SUPERVISOR] Stopping queue dispatch as stop_on_failure=True.", flush=True)
                    break
            else:
                eligible_jobs.append(job)

        if eligible_jobs and not (self.stop_on_failure and any(j.status == "PAUSED_LOW_DISK" for j in self.queue)):

            def _worker_wrapper(job: QueueJob) -> QueueJob:
                job.status = "RUNNING"
                t_start = time.time()
                try:
                    execute_fn(job)
                except Exception as exc:
                    job.status = "FAILED"
                    job.error_message = f"Execution failed: {exc}"
                    return job

                elapsed_min = (time.time() - t_start) / 60.0
                report = verify_project_deliverables(
                    deliverables_dir=job.output_dir,
                    project_name=job.project_name,
                    expected_assets=job.expected_assets,
                )
                job.verification_report = report
                if report["status"] == "PASS":
                    job.status = "COMPLETED"
                    print(
                        f"  [QUALITY GATE] [OK] [{job.project_name}] Deliverables verified "
                        f"successfully ({elapsed_min:.2f} min).",
                        flush=True,
                    )
                else:
                    job.status = "VERIFICATION_FAILED"
                    job.error_message = "; ".join(report.get("errors", []))
                    print(
                        f"  [QUALITY GATE] [FAIL] [{job.project_name}] Deliverable verification failed: "
                        f"{job.error_message}",
                        file=sys.stderr,
                        flush=True,
                    )

                if job.status == "COMPLETED" and self.scratch_dir and os.path.isdir(self.scratch_dir):
                    staged_sub = os.path.join(self.scratch_dir, f"{job.project_name}_local_stage")
                    if os.path.isdir(staged_sub):
                        try:
                            shutil.rmtree(staged_sub, ignore_errors=True)
                        except Exception:
                            pass

                return job

            pool_size = max(1, min(len(eligible_jobs), max_workers))
            with concurrent.futures.ThreadPoolExecutor(max_workers=pool_size) as executor:
                future_to_job = {executor.submit(_worker_wrapper, job): job for job in eligible_jobs}
                for future in concurrent.futures.as_completed(future_to_job):
                    res_job = future.result()
                    results.append(res_job.to_dict())
                    if res_job.status != "COMPLETED" and self.stop_on_failure:
                        print(
                            "  [SUPERVISOR] Halting queue execution due to job failure (stop_on_failure=True).",
                            flush=True,
                        )
                        executor.shutdown(wait=False, cancel_futures=True)
                        break

        # Collect any unexecuted pending jobs
        executed_job_names = {r["project_name"] for r in results}
        for job in self.queue:
            if job.project_name not in executed_job_names:
                results.append(job.to_dict())

        passed_count = sum(1 for r in results if r["status"] == "COMPLETED")
        print("\n=========================================================================")
        print("  AUTOMETASHAPE: CONCURRENT QUEUE RUN SUMMARY")
        print(
            f"  Total Queued: {len(self.queue)} | Completed & Verified: {passed_count} | "
            f"Failed/Paused: {len(results) - passed_count}"
        )
        print("=========================================================================", flush=True)

        return {
            "total_jobs": len(self.queue),
            "completed_jobs": passed_count,
            "failed_jobs": len(results) - passed_count,
            "jobs": results,
            "results": results,
        }

    def run_concurrent_queue(
        self,
        execute_fn: Optional[Any] = None,
        poll_interval: float = 2.0,
    ) -> Dict[str, Any]:
        """Dispatches all independent project jobs concurrently to the cluster and supervises in parallel.

        Args:
            execute_fn: Optional callable(job: QueueJob) -> None for testing or mock execution.
                        If provided, delegates to _run_concurrent_mock_queue using thread pools.
            poll_interval: Frequency in seconds to poll cluster server batch status and sidecar telemetry.

        Returns:
            Summary dictionary with execution statistics and per-job verification reports.
        """
        if execute_fn is not None:
            return self._run_concurrent_mock_queue(execute_fn)

        if Metashape is None:
            raise RuntimeError("Metashape module is not available for real cluster execution.")

        net_client = Metashape.NetworkClient()
        first_job_extra = self.queue[0].extra_args if self.queue else {}
        net_host = first_job_extra.get("network_host", "127.0.0.1")
        net_port = int(first_job_extra.get("network_port", 5840))

        try:
            net_client.connect(f"{net_host}:{net_port}")
            print(f"  [Cluster Supervisor] Connected to coordinator at {net_host}:{net_port}", flush=True)
        except Exception as exc:
            raise RuntimeError(f"Failed to connect to cluster coordinator at {net_host}:{net_port}: {exc}") from exc

        # Respect Operator Authority Gate: never auto-unpause paused workers; report passively if paused
        try:
            raw_w = net_client.workerList()
            w_list = (
                raw_w.get("workers", [])
                if isinstance(raw_w, dict)
                else (raw_w if isinstance(raw_w, (list, tuple)) else [])
            )
            paused_names = [
                str(w.get("hostname", w.get("worker_id")) if isinstance(w, dict) else getattr(w, "id", "?"))
                for w in w_list
                if (w.get("paused", False) if isinstance(w, dict) else getattr(w, "paused", False))
                and str(w.get("state", "") if isinstance(w, dict) else getattr(w, "state", "")).lower() != "offline"
            ]
            if paused_names:
                print(
                    f"  [Cluster Supervisor] Notice: {len(paused_names)} paused online worker(s) left untouched "
                    f"(Operator Authority Gate): {', '.join(paused_names)}",
                    flush=True,
                )
        except Exception:
            pass

        active_batches: Dict[int, QueueJob] = {}
        batch_status_files: Dict[int, str] = {}
        completed_results: List[Dict[str, Any]] = []

        print("=========================================================================")
        print("  AUTOMETASHAPE: CONCURRENT MULTI-PROJECT CLUSTER DISPATCH (MODE 2)")
        print(f"  Total Queued Projects: {len(self.queue)}")
        print(f"  Coordinator Host     : {net_host}:{net_port}")
        print(f"  Headroom Threshold   : {self.min_headroom_gb:.1f} GB")
        print("=========================================================================", flush=True)

        # Phase 1: Rapid Pre-flight & Monolithic Batch Dispatch
        for job in self.queue:
            has_headroom, free_gb = check_storage_headroom(job.output_dir, self.min_headroom_gb)
            if not has_headroom:
                err = (
                    f"Storage exhaustion hazard (Win32 Error 112 defense): "
                    f"Volume has only {free_gb:.1f} GB free (threshold: {self.min_headroom_gb:.1f} GB). "
                    f"Pausing job to prevent data corruption."
                )
                print(f"  [STORAGE ALERT] [{job.project_name}] {err}", file=sys.stderr, flush=True)
                job.status = "PAUSED_LOW_DISK"
                job.error_message = err
                completed_results.append(job.to_dict())
                if self.stop_on_failure:
                    print("  [SUPERVISOR] Stopping queue dispatch as stop_on_failure=True.", flush=True)
                    break
                continue

            os.makedirs(job.output_dir, exist_ok=True)
            psx_path = os.path.join(job.output_dir, f"{job.project_name}.psx")
            if not os.path.exists(psx_path):
                if hasattr(Metashape, "Document"):
                    doc = Metashape.Document()
                    doc.save(psx_path)
                    if hasattr(doc, "clear"):
                        doc.clear()

            net_path = (
                job.extra_args.get("network_path")
                or extract_unc_root(job.output_dir)
                or extract_unc_root(job.images_dir)
            )
            if net_path:
                job.extra_args["network_path"] = net_path

            if net_path and is_subpath(psx_path, net_path):
                rel_psx = os.path.relpath(psx_path, net_path).replace("\\", "/")
            else:
                rel_psx = psx_path.replace("\\", "/")

            # Filter extra_args to only arguments supported by build_monolithic_runscript_task
            sig = inspect.signature(build_monolithic_runscript_task)
            forward_kwargs = {k: v for k, v in job.extra_args.items() if k in sig.parameters and k != "network_path"}

            net_task, rel_script, status_file = build_monolithic_runscript_task(
                images_dir=job.images_dir,
                output_dir=job.output_dir,
                project_name=job.project_name,
                preset=job.preset,
                network_path=net_path,
                **forward_kwargs,
            )

            try:
                batch_id, was_attached = safe_create_cluster_batch(
                    net_client, rel_psx, [net_task], worker_limit=1, force_fresh=False
                )
                job.status = "RUNNING"
                active_batches[batch_id] = job
                batch_status_files[batch_id] = status_file
                attach_str = "attached to existing" if was_attached else "created new"
                print(
                    f"  [Dispatched] Project '{job.project_name}' -> Batch #{batch_id} ({attach_str}, worker_limit=1)",
                    flush=True,
                )
            except Exception as exc:
                err_msg = f"Failed to dispatch cluster batch for {job.project_name}: {exc}"
                print(f"  [DISPATCH ERROR] {err_msg}", file=sys.stderr, flush=True)
                job.status = "FAILED"
                job.error_message = err_msg
                completed_results.append(job.to_dict())
                if self.stop_on_failure:
                    break

        # Phase 2: Parallel Telemetry & Verification Supervision Loop
        print("\n[SUPERVISOR] All eligible projects dispatched. Entering parallel supervision loop...\n", flush=True)
        last_stage_keys: Dict[int, str] = {}

        while active_batches:
            time.sleep(poll_interval)
            for batch_id in list(active_batches.keys()):
                job = active_batches[batch_id]
                try:
                    b_info = net_client.batchInfo(batch_id)
                except Exception as e_poll:
                    print(
                        f"  [Cluster Warning] Failed to poll batch #{batch_id}: {e_poll}",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue

                # Check sidecar telemetry
                st_file = batch_status_files.get(batch_id)
                if st_file and os.path.exists(st_file):
                    try:
                        with open(st_file, "r", encoding="utf-8") as sf:
                            s_data = json.load(sf)
                        stage_k = s_data.get("stage_key")
                        if stage_k and stage_k != last_stage_keys.get(batch_id):
                            w_node = s_data.get("worker_node", "worker")
                            st_desc = s_data.get("stage_desc", stage_k)
                            print(
                                f"  [{job.project_name} | Batch #{batch_id}] Stage: {st_desc} ({w_node})",
                                flush=True,
                            )
                            last_stage_keys[batch_id] = stage_k
                    except Exception:
                        pass

                b_status, b_success, b_err = evaluate_cluster_batch_info(b_info)
                if b_success:
                    print(
                        f"\n  [{job.project_name} | Batch #{batch_id}] Reconstruction Complete! Running Quality Gate...",
                        flush=True,
                    )
                    report = verify_project_deliverables(
                        deliverables_dir=job.output_dir,
                        project_name=job.project_name,
                        expected_assets=job.expected_assets,
                    )
                    job.verification_report = report
                    if report["status"] == "PASS":
                        job.status = "COMPLETED"
                        print(
                            f"  [QUALITY GATE] [OK] [{job.project_name}] Deliverables verified successfully.",
                            flush=True,
                        )
                    else:
                        job.status = "VERIFICATION_FAILED"
                        job.error_message = "; ".join(report.get("errors", []))
                        print(
                            f"  [QUALITY GATE] [FAIL] [{job.project_name}] Deliverable verification failed: "
                            f"{job.error_message}",
                            file=sys.stderr,
                            flush=True,
                        )

                    if self.scratch_dir and os.path.isdir(self.scratch_dir):
                        staged_sub = os.path.join(self.scratch_dir, f"{job.project_name}_local_stage")
                        if os.path.isdir(staged_sub):
                            try:
                                shutil.rmtree(staged_sub, ignore_errors=True)
                            except Exception:
                                pass

                    completed_results.append(job.to_dict())
                    del active_batches[batch_id]

                    if job.status != "COMPLETED" and self.stop_on_failure:
                        print("  [SUPERVISOR] Halting remaining batches due to verification failure.", flush=True)
                        for rem_b_id in list(active_batches.keys()):
                            try:
                                net_client.abortBatch(rem_b_id)
                            except Exception:
                                pass
                            rem_j = active_batches[rem_b_id]
                            rem_j.status = "ABORTED"
                            completed_results.append(rem_j.to_dict())
                        active_batches.clear()
                        break

                elif b_status in ("failed", "aborted"):
                    # Critical Metashape 2.2+ defense: server keeps state='inprogress' and retries infinitely
                    # on generic-error / io-error / process-terminated unless explicitly aborted.
                    raw_server_state = str(b_info.get("state", "")).lower() if isinstance(b_info, dict) else ""
                    if raw_server_state not in ("completed", "failed", "aborted", "cancelled"):
                        try:
                            net_client.abortBatch(batch_id)
                        except Exception:
                            pass
                    print(
                        f"\n  [ERROR] [{job.project_name} | Batch #{batch_id}] Batch failed: {b_err}",
                        file=sys.stderr,
                        flush=True,
                    )
                    job.status = "FAILED"
                    job.error_message = b_err
                    completed_results.append(job.to_dict())
                    del active_batches[batch_id]

                    if self.stop_on_failure:
                        print(
                            "  [SUPERVISOR] Halting remaining batches due to batch failure (stop_on_failure=True).",
                            flush=True,
                        )
                        for rem_b_id in list(active_batches.keys()):
                            try:
                                net_client.abortBatch(rem_b_id)
                            except Exception:
                                pass
                            rem_j = active_batches[rem_b_id]
                            rem_j.status = "ABORTED"
                            completed_results.append(rem_j.to_dict())
                        active_batches.clear()
                        break

        # Collect any unexecuted pending jobs if stopped on failure
        executed_job_names = {r["project_name"] for r in completed_results}
        for job in self.queue:
            if job.project_name not in executed_job_names:
                completed_results.append(job.to_dict())

        passed_count = sum(1 for r in completed_results if r["status"] == "COMPLETED")
        print("\n=========================================================================")
        print("  AUTOMETASHAPE: CONCURRENT MULTI-PROJECT RUN SUMMARY")
        print(
            f"  Total Queued: {len(self.queue)} | Completed & Verified: {passed_count} | "
            f"Failed/Paused: {len(completed_results) - passed_count}"
        )
        print("=========================================================================", flush=True)

        return {
            "total_jobs": len(self.queue),
            "completed_jobs": passed_count,
            "failed_jobs": len(completed_results) - passed_count,
            "jobs": completed_results,
            "results": completed_results,
        }


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
            for _root, _, files in os.walk(d):
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
        raise ValueError(f"Failed to list scan directory '{scan_dir}': {exc}") from exc

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
    group.add_argument(
        "--source-share",
        "--data-root",
        dest="source_share",
        default=None,
        help="Dual-SSD Data Source shared folder (SSD #1, read-only raw photos; pair with --result-share)",
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help="Root directory where output deliverables folders will be created for auto-discovered datasets",
    )
    parser.add_argument(
        "--result-share",
        "--result-root",
        dest="result_share",
        default=None,
        help="Dual-SSD Result shared folder (SSD #2, writable .psx + deliverables; also resolves Metashape --root)",
    )
    parser.add_argument(
        "--ortho-resolution",
        type=float,
        default=0.05,
        help="Orthomosaic pixel size in meters (default: 0.05 = 5 cm/px; 0 = native GSD)",
    )
    parser.add_argument(
        "--build-point-cloud",
        dest="build_point_cloud",
        action="store_true",
        default=False,
        help="Enable dense point cloud generation (disabled by default in 'pro' preset)",
    )
    parser.add_argument(
        "--no-point-cloud",
        dest="build_point_cloud",
        action="store_false",
        help="Disable dense point cloud generation",
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
    # Concurrency Control Flags
    parser.add_argument(
        "--concurrent",
        "--parallel-dispatch",
        action="store_true",
        dest="concurrent",
        default=None,
        help="Enable concurrent multi-project dispatch across dedicated cluster workers (Mode 2)",
    )
    parser.add_argument(
        "--no-concurrent",
        "--sequential",
        action="store_false",
        dest="concurrent",
        help="Force sequential single-project queue execution (Mode 1)",
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
        help="Lock each project to an independent worker executing exclusively via offline local scratch process",
    )
    parser.add_argument(
        "--network-path",
        default="",
        help="Shared network root directory (UNC path, e.g. \\\\SERVER\\SharedMetashape)",
    )
    parser.add_argument(
        "--no-reexec",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def main() -> int:
    """CLI entry point for supervisor."""
    parser = build_arg_parser()
    args = parser.parse_args()

    # Plain Python Auto-Delegation into Agisoft Metashape Pro runtime
    if Metashape is None and not getattr(args, "no_reexec", False):
        ms_exe = find_metashape_path()
        if ms_exe and os.path.isfile(ms_exe):
            entry_script = (
                os.path.abspath(sys.argv[0]) if sys.argv and os.path.isfile(sys.argv[0]) else os.path.abspath(__file__)
            )
            cmd = [ms_exe, "-r", entry_script] + sys.argv[1:] + ["--no-reexec"]
            res = subprocess.run(cmd)
            return res.returncode
        print(
            "[ERROR] Agisoft Metashape Pro executable not found. Run via 'metashape.exe -r'.",
            file=sys.stderr,
        )
        return 1

    # Determine concurrency mode:
    # 1. Explicit --concurrent / --parallel-dispatch -> True
    # 2. Explicit --no-concurrent / --sequential -> False
    # 3. Default: True if --network-independent-workers is active, otherwise False
    is_concurrent = False
    if args.concurrent is not None:
        is_concurrent = args.concurrent
    elif args.network_independent_workers:
        is_concurrent = True

    supervisor = ClusterSupervisor(
        min_headroom_gb=args.min_headroom_gb,
        stop_on_failure=args.stop_on_failure,
        concurrent=is_concurrent,
    )

    expected_set = set(args.expect) if args.expect else {"obj", "ortho"}
    extra_args: Dict[str, Any] = {
        "ortho_resolution": args.ortho_resolution,
        "build_point_cloud": args.build_point_cloud or ("point_cloud" in expected_set),
        "export_obj": "obj" in expected_set,
        "export_ortho": "ortho" in expected_set,
        "export_glb": "glb" in expected_set,
        "export_dem": "dem" in expected_set,
        "build_dem": "dem" in expected_set,
        "export_point_cloud": "point_cloud" in expected_set,
        "export_report": "report" in expected_set,
    }
    if args.network_enable:
        extra_args["network_enable"] = True
        extra_args["network_host"] = args.network_host
        extra_args["network_port"] = args.network_port
        extra_args["network_independent_workers"] = args.network_independent_workers
        if args.network_path:
            extra_args["network_path"] = args.network_path

    if args.source_share:
        target_result = args.result_share or args.output_root
        if not target_result:
            print(
                "[FATAL] --result-share (or --output-root) is required when using --source-share.",
                file=sys.stderr,
            )
            return 1
        validation = validate_dual_share_topology(
            source_share=args.source_share,
            result_share=target_result,
            result_network_root=args.network_path,
            min_headroom_gb=args.min_headroom_gb,
        )
        for warn in validation["warnings"]:
            print(f"[Dual-Share Warning] {warn}", flush=True)
        if not validation["valid"]:
            print(f"[FATAL] Dual-share validation failed: {'; '.join(validation['errors'])}", file=sys.stderr)
            return 1
        extra_args["network_path"] = validation["result_network_root"]
        try:
            jobs = discover_dual_share_projects(
                source_share=validation["source_share"],
                result_share=validation["result_share"],
                preset=args.preset,
                expected_assets=args.expect,
                extra_args=extra_args,
                result_network_root=validation["result_network_root"],
            )
        except Exception as exc:
            print(f"[FATAL] Failed to discover dual-share datasets in '{args.source_share}': {exc}", file=sys.stderr)
            return 1
        if not jobs:
            print(f"[WARNING] No survey image datasets found in '{args.source_share}'.", file=sys.stderr)
            return 0
        supervisor.queue.extend(jobs)

    elif args.manifest:
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
