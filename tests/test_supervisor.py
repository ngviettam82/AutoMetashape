# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit test suite for cluster queue supervisor, storage headroom watchdog, and queue chaining."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "source")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from autometashape.cluster.supervisor import (
    ClusterSupervisor,
    QueueJob,
    check_storage_headroom,
    discover_dataset_projects,
)


class TestClusterSupervisor(unittest.TestCase):
    """Defends autonomous cluster queue supervision, storage exhaustion protection, and queue chaining."""

    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp(prefix="test_supervisor_")

    def tearDown(self) -> None:
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_check_storage_headroom(self) -> None:
        """Verify disk storage headroom inspection logic."""
        mock_usage_sufficient = MagicMock()
        mock_usage_sufficient.free = 150 * (1024**3)  # 150 GB

        with patch("shutil.disk_usage", return_value=mock_usage_sufficient):
            ok, free = check_storage_headroom(self.test_dir, min_headroom_gb=50.0)
            self.assertTrue(ok)
            self.assertEqual(free, 150.0)

        mock_usage_low = MagicMock()
        mock_usage_low.free = 25 * (1024**3)  # 25 GB

        with patch("shutil.disk_usage", return_value=mock_usage_low):
            ok, free = check_storage_headroom(self.test_dir, min_headroom_gb=50.0)
            self.assertFalse(ok)
            self.assertEqual(free, 25.0)

    def test_supervisor_low_disk_pauses_execution(self) -> None:
        """Verify that low disk space pauses the job and defends against Win32 Error 112."""
        supervisor = ClusterSupervisor(min_headroom_gb=50.0, stop_on_failure=True)
        job = supervisor.add_job(
            images_dir=os.path.join(self.test_dir, "photos_0167"),
            output_dir=os.path.join(self.test_dir, "deliverables_0167"),
            project_name="Job_Low_Disk",
        )

        mock_usage = MagicMock()
        mock_usage.free = 15 * (1024**3)  # 15 GB < 50 GB threshold

        executed = False

        def dummy_execute(j):
            nonlocal executed
            executed = True

        with patch("shutil.disk_usage", return_value=mock_usage):
            summary = supervisor.run_queue(execute_fn=dummy_execute)

        self.assertFalse(executed, "Execution function should NOT be called when headroom is low.")
        self.assertEqual(job.status, "PAUSED_LOW_DISK")
        self.assertEqual(summary["completed_jobs"], 0)
        self.assertIn("Storage exhaustion hazard", job.error_message)

    def test_supervisor_successful_queue_chaining(self) -> None:
        """Verify end-to-end sequential queue progression with deliverable verification."""
        scratch_dir = os.path.join(self.test_dir, "scratch")
        os.makedirs(scratch_dir, exist_ok=True)

        supervisor = ClusterSupervisor(
            min_headroom_gb=20.0,
            stop_on_failure=True,
            scratch_dir=scratch_dir,
        )

        job1 = supervisor.add_job(
            images_dir=os.path.join(self.test_dir, "photos_1"),
            output_dir=os.path.join(self.test_dir, "out_1"),
            project_name="Survey_1",
            expected_assets=["glb"],
        )
        job2 = supervisor.add_job(
            images_dir=os.path.join(self.test_dir, "photos_2"),
            output_dir=os.path.join(self.test_dir, "out_2"),
            project_name="Survey_2",
            expected_assets=["glb"],
        )

        mock_usage = MagicMock()
        mock_usage.free = 200 * (1024**3)  # 200 GB

        def mock_execute(j: QueueJob):
            # Create output folder and dummy valid GLB
            os.makedirs(j.output_dir, exist_ok=True)
            with open(os.path.join(j.output_dir, f"{j.project_name}.glb"), "wb") as f:
                f.write(b"glTF" + b"\x00" * 2000)

            # Create dummy scratch staging folder
            staged_folder = os.path.join(scratch_dir, f"{j.project_name}_local_stage")
            os.makedirs(staged_folder, exist_ok=True)
            with open(os.path.join(staged_folder, "temp.tmp"), "wb") as f:
                f.write(b"transient_data")

        with patch("shutil.disk_usage", return_value=mock_usage):
            summary = supervisor.run_queue(execute_fn=mock_execute)

        self.assertEqual(summary["total_jobs"], 2)
        self.assertEqual(summary["completed_jobs"], 2)
        self.assertEqual(job1.status, "COMPLETED")
        self.assertEqual(job2.status, "COMPLETED")

        # Verify scratch staging folder for job 1 was purged
        staged_1 = os.path.join(scratch_dir, "Survey_1_local_stage")
        self.assertFalse(os.path.exists(staged_1), "Staged scratch for Job 1 should be purged after verification.")

    def test_supervisor_stops_on_verification_failure(self) -> None:
        """Verify that supervisor halts queue when deliverable verification fails and stop_on_failure=True."""
        supervisor = ClusterSupervisor(min_headroom_gb=10.0, stop_on_failure=True)
        job1 = supervisor.add_job(
            images_dir=os.path.join(self.test_dir, "photos_fail"),
            output_dir=os.path.join(self.test_dir, "out_fail"),
            project_name="Survey_Fail",
            expected_assets=["glb"],
        )
        job2 = supervisor.add_job(
            images_dir=os.path.join(self.test_dir, "photos_skipped"),
            output_dir=os.path.join(self.test_dir, "out_skipped"),
            project_name="Survey_Skipped",
            expected_assets=["glb"],
        )

        mock_usage = MagicMock()
        mock_usage.free = 100 * (1024**3)

        def mock_bad_execute(j: QueueJob):
            # Create empty directory without expected deliverables
            os.makedirs(j.output_dir, exist_ok=True)

        with patch("shutil.disk_usage", return_value=mock_usage):
            summary = supervisor.run_queue(execute_fn=mock_bad_execute)

        self.assertEqual(job1.status, "VERIFICATION_FAILED")
        self.assertEqual(job2.status, "PENDING")
        self.assertEqual(summary["completed_jobs"], 0)

    def test_discover_dataset_projects_multi_subfolders(self) -> None:
        """Verify recursive multi-project subfolder discovery."""
        scan_dir = os.path.join(self.test_dir, "scan_multi")
        out_root = os.path.join(self.test_dir, "out_root")
        sub_a = os.path.join(scan_dir, "Project_Alpha")
        sub_b = os.path.join(scan_dir, "Project_Beta")
        sub_out = os.path.join(scan_dir, "output")

        os.makedirs(sub_a, exist_ok=True)
        os.makedirs(sub_b, exist_ok=True)
        os.makedirs(sub_out, exist_ok=True)

        with open(os.path.join(sub_a, "DJI_0001.JPG"), "w") as f:
            f.write("dummy")
        with open(os.path.join(sub_b, "DJI_0002.DNG"), "w") as f:
            f.write("dummy")
        with open(os.path.join(sub_out, "ignored.jpg"), "w") as f:
            f.write("dummy")

        jobs = discover_dataset_projects(scan_dir, output_root=out_root, preset="pro")
        self.assertEqual(len(jobs), 2)
        names = {j.project_name for j in jobs}
        self.assertEqual(names, {"Project_Alpha", "Project_Beta"})
        for j in jobs:
            self.assertTrue(j.output_dir.startswith(out_root))

    def test_discover_dataset_projects_single_root(self) -> None:
        """Verify single project root discovery when photos reside at root."""
        scan_dir = os.path.join(self.test_dir, "single_project")
        os.makedirs(scan_dir, exist_ok=True)
        with open(os.path.join(scan_dir, "photo.tif"), "w") as f:
            f.write("dummy")

        jobs = discover_dataset_projects(scan_dir, preset="building")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].project_name, "single_project")
        self.assertEqual(jobs[0].preset, "building")

    def test_discover_dataset_projects_with_network_flags(self) -> None:
        """Verify propagation of network cluster execution parameters."""
        scan_dir = os.path.join(self.test_dir, "scan_net")
        sub_dir = os.path.join(scan_dir, "Job_Net")
        os.makedirs(sub_dir, exist_ok=True)
        with open(os.path.join(sub_dir, "cam.png"), "w") as f:
            f.write("dummy")

        extra = {
            "network_enable": True,
            "network_host": "10.5.10.202",
            "network_port": 5840,
            "network_independent_workers": True,
        }
        jobs = discover_dataset_projects(scan_dir, extra_args=extra)
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].extra_args.get("network_enable"))
        self.assertEqual(jobs[0].extra_args.get("network_host"), "10.5.10.202")
        self.assertTrue(jobs[0].extra_args.get("network_independent_workers"))


if __name__ == "__main__":
    unittest.main()
