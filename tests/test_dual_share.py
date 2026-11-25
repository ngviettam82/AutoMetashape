# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for Dual Shared Folder (Multi-SSD) cluster orchestration."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")

for p in (_SCRIPTS_DIR, _SOURCE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from autometashape.cluster.dual_share import (
    discover_dual_share_projects,
    is_subpath,
    is_unc_path,
    map_source_to_result_dir,
    resolve_result_network_root,
    run_dual_share_cluster_pipeline,
    validate_dual_share_topology,
)

from scripts.launch_worker import build_arg_parser as build_worker_parser
from scripts.submit_cluster_batch import build_arg_parser as build_submit_parser


class TestDualShareTopology(unittest.TestCase):
    """Test suite verifying Dual Shared Folder (SSD #1 Data Source + SSD #2 Result Share) behavior."""

    def test_is_subpath_prevents_sibling_prefix_collision(self) -> None:
        """Verify is_subpath rejects sibling prefix collisions like \\SRV\\Data2 vs \\SRV\\Data."""
        self.assertTrue(is_subpath(r"\\SRV\Data\Site_A", r"\\SRV\Data"))
        self.assertTrue(is_subpath(r"\\srv\data\site_a\sub", r"\\SRV\Data"))
        self.assertTrue(is_subpath(r"\\SRV\Data", r"\\SRV\Data"))
        self.assertFalse(is_subpath(r"\\SRV\Data2\Site_A", r"\\SRV\Data"))
        self.assertFalse(is_subpath(r"\\SRV\Data_Backup\Site_A", r"\\SRV\Data"))
        self.assertFalse(is_subpath(r"D:\Result\Site_A", r"C:\Source"))

    def test_resolve_result_network_root_prefers_result_share_over_source(self) -> None:
        """Verify Metashape --root always anchors to the Result Share (SSD #2)."""
        self.assertEqual(
            resolve_result_network_root(r"\\NAS\SSD2_Results\Batch_01"),
            r"\\NAS\SSD2_Results",
        )
        self.assertEqual(
            resolve_result_network_root(r"\\NAS\SSD2_Results\Batch_01", explicit_root=r"\\NAS\SSD2_Results"),
            r"\\NAS\SSD2_Results",
        )
        self.assertTrue(is_unc_path(r"\\NAS\SSD2_Results"))
        self.assertFalse(is_unc_path(r"D:\LocalSSD\Results"))

    def test_validate_dual_share_topology_accepts_distinct_accessible_shares(self) -> None:
        """Verify validation passes when source is readable and result is writable with headroom."""
        with tempfile.TemporaryDirectory() as src_dir, tempfile.TemporaryDirectory() as res_dir:
            report = validate_dual_share_topology(
                source_share=src_dir,
                result_share=res_dir,
                min_headroom_gb=0.01,
            )
            self.assertTrue(report["valid"], msg=f"Unexpected validation errors: {report['errors']}")
            self.assertTrue(report["result_writable"])
            self.assertGreater(report["result_free_gb"], 0.0)
            self.assertEqual(report["errors"], [])

    def test_validate_dual_share_topology_rejects_missing_source(self) -> None:
        """Verify validation fails cleanly when Data Source share does not exist."""
        with tempfile.TemporaryDirectory() as res_dir:
            missing_src = os.path.join(res_dir, "NonExistent_SSD1")
            report = validate_dual_share_topology(
                source_share=missing_src,
                result_share=res_dir,
                min_headroom_gb=0.01,
            )
            self.assertFalse(report["valid"])
            self.assertTrue(any("unreachable" in e.lower() or "not a directory" in e.lower() for e in report["errors"]))

    def test_validate_dual_share_topology_warns_when_source_equals_result(self) -> None:
        """Verify using identical source and result shares emits a topology warning."""
        with tempfile.TemporaryDirectory() as same_dir:
            report = validate_dual_share_topology(
                source_share=same_dir,
                result_share=same_dir,
                min_headroom_gb=0.01,
            )
            self.assertTrue(report["valid"])
            self.assertTrue(any("same" in w.lower() or "identical" in w.lower() for w in report["warnings"]))

    def test_map_source_to_result_dir_preserves_project_subpath(self) -> None:
        """Verify project folder mapping mirrors relative hierarchy onto Result Share SSD #2."""
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as res_root:
            site_a = os.path.join(src_root, "2026_Batch", "Site_Alpha")
            os.makedirs(site_a, exist_ok=True)

            mapped = map_source_to_result_dir(site_a, src_root, res_root)
            expected = os.path.normpath(os.path.join(res_root, "2026_Batch", "Site_Alpha"))
            self.assertEqual(os.path.normpath(mapped), expected)

            # Direct single-project root maps into <res_root>/<basename(src_root)>
            mapped_single = map_source_to_result_dir(src_root, src_root, res_root)
            expected_single = os.path.normpath(os.path.join(res_root, os.path.basename(src_root)))
            self.assertEqual(os.path.normpath(mapped_single), expected_single)

    def test_discover_dual_share_projects_multi_and_single_project(self) -> None:
        """Verify discovery splits read-only SSD #1 source from writable SSD #2 results."""
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as res_root:
            site_a = os.path.join(src_root, "Site_Alpha")
            site_b = os.path.join(src_root, "Site_Beta")
            ignored_out = os.path.join(src_root, "output_old")
            for d in (site_a, site_b, ignored_out):
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "DJI_0001.JPG"), "wb") as f:
                    f.write(b"\xff\xd8\xff\xd9")

            jobs = discover_dual_share_projects(src_root, res_root)
            names = [j.project_name for j in jobs]
            self.assertEqual(names, ["Site_Alpha", "Site_Beta"])

            for job in jobs:
                self.assertTrue(is_subpath(job.images_dir, src_root))
                self.assertTrue(is_subpath(job.output_dir, res_root))
                self.assertFalse(is_subpath(job.output_dir, src_root))
                # Default expected assets exclude DEM (decoupled/opt-in)
                self.assertEqual(job.expected_assets, ["obj", "ortho"])

            # Single-project root discovery
            single_jobs = discover_dual_share_projects(site_a, res_root)
            self.assertEqual(len(single_jobs), 1)
            self.assertEqual(single_jobs[0].project_name, "Site_Alpha")
            self.assertEqual(os.path.normpath(single_jobs[0].images_dir), os.path.normpath(site_a))
            self.assertEqual(
                os.path.normpath(single_jobs[0].output_dir),
                os.path.normpath(os.path.join(res_root, "Site_Alpha")),
            )

    def test_run_dual_share_cluster_pipeline_wires_supervisor_and_defaults(self) -> None:
        """Verify run_dual_share_cluster_pipeline enforces 5cm ortho, no point cloud, and result --root."""
        with tempfile.TemporaryDirectory() as src_root, tempfile.TemporaryDirectory() as res_root:
            site_a = os.path.join(src_root, "Site_Alpha")
            os.makedirs(site_a, exist_ok=True)
            with open(os.path.join(site_a, "IMG_001.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")

            captured_jobs = []
            captured_init = {}

            class _StubSupervisor:
                def __init__(self, **kwargs):
                    captured_init.update(kwargs)
                    self.queue = []

                def run_concurrent_queue(self, execute_fn=None, poll_interval=2.0):
                    captured_jobs.extend(self.queue)
                    return {"total": len(self.queue), "completed": len(self.queue), "failed": 0, "jobs": []}

                def run_queue(self, execute_fn=None):
                    return self.run_concurrent_queue(execute_fn=execute_fn)

            with patch("autometashape.cluster.supervisor.ClusterSupervisor", _StubSupervisor):
                summary = run_dual_share_cluster_pipeline(
                    source_share=src_root,
                    result_share=res_root,
                    server_host="10.0.0.10",
                    min_headroom_gb=0.01,
                )

            self.assertEqual(summary["total"], 1)
            self.assertEqual(summary["completed"], 1)
            self.assertEqual(len(captured_jobs), 1)
            job = captured_jobs[0]
            self.assertTrue(is_subpath(job.images_dir, src_root))
            self.assertTrue(is_subpath(job.output_dir, res_root))
            self.assertEqual(job.extra_args.get("network_path"), os.path.normpath(res_root))
            self.assertFalse(job.extra_args.get("build_point_cloud"))
            self.assertAlmostEqual(job.extra_args.get("ortho_resolution"), 0.05)

    def test_cli_parsers_support_dual_share_flags(self) -> None:
        """Verify submit_cluster_batch and launch_worker expose dual-share CLI flags cleanly."""
        sub_parser = build_submit_parser()
        sub_args = sub_parser.parse_args(
            [
                "--source-share",
                r"\\SRV\SSD1_Photos",
                "--result-share",
                r"\\SRV\SSD2_Results",
                "--preset",
                "pro",
                "--ortho-resolution",
                "0.05",
                "--tiepoint-limit",
                "40000",
                "--keypoint-limit-per-mpx",
                "2000",
                "--no-build-point-cloud",
            ]
        )
        self.assertEqual(sub_args.source_share, r"\\SRV\SSD1_Photos")
        self.assertEqual(sub_args.result_share, r"\\SRV\SSD2_Results")
        self.assertAlmostEqual(sub_args.ortho_resolution, 0.05)
        self.assertEqual(sub_args.tiepoint_limit, 40000)
        self.assertEqual(sub_args.keypoint_limit_per_mpx, 2000)
        self.assertFalse(sub_args.build_point_cloud)

        # Legacy nonexistent preset must be rejected
        with self.assertRaises(SystemExit):
            sub_parser.parse_args(["--images", "dummy", "--preset", "ultra"])

        wrk_parser = build_worker_parser()
        wrk_args = wrk_parser.parse_args(
            [
                "--host",
                "10.0.0.10",
                "--result-share",
                r"\\SRV\SSD2_Results",
                "--source-share",
                r"\\SRV\SSD1_Photos",
            ]
        )
        self.assertEqual(wrk_args.root, r"\\SRV\SSD2_Results")
        self.assertEqual(wrk_args.source_root, r"\\SRV\SSD1_Photos")


if __name__ == "__main__":
    unittest.main()
