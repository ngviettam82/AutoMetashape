# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for canonical cluster dispatch, queue management, and pre-flight validation."""

import os
import sys
import tempfile
import unittest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")

for p in (_SCRIPTS_DIR, _SOURCE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from scripts.cluster_monitor import audit_headroom, build_arg_parser as build_monitor_parser
from scripts.inspect_checkpoint import build_arg_parser as build_inspect_parser
from scripts.manage_queue import build_arg_parser as build_queue_parser
from scripts.submit_cluster_batch import (
    build_arg_parser as build_submit_parser,
    find_photos_in_directory,
    resolve_server_address,
    resolve_shared_root,
)


class TestClusterDispatchValidation(unittest.TestCase):
    """Test suite for canonical cluster CLI tools and pre-flight safeguards."""

    def test_submit_batch_arg_parser(self):
        """Verify submit_cluster_batch argument parser accepts all required and optional flags."""
        parser = build_submit_parser()
        args = parser.parse_args([
            "--images", r"\\SRV\Share\Survey_01",
            "--output", r"\\SRV\Share\Survey_01\Result",
            "--priority", "75",
            "--server", "192.168.1.50:5840",
            "--root", r"\\SRV\Share",
            "--resume",
            "--no-local-scratch-staging",
            "--preset", "ultra",
            "--worker-limit", "2",
        ])
        self.assertEqual(args.images, r"\\SRV\Share\Survey_01")
        self.assertEqual(args.output, r"\\SRV\Share\Survey_01\Result")
        self.assertEqual(args.priority, 75)
        self.assertEqual(args.server, "192.168.1.50:5840")
        self.assertTrue(args.resume)
        self.assertTrue(args.no_local_scratch_staging)
        self.assertEqual(args.preset, "ultra")
        self.assertEqual(args.worker_limit, 2)

    def test_dynamic_server_resolution(self):
        """Verify server address resolution without hardcoded fallback."""
        self.assertEqual(resolve_server_address("10.0.0.5:5840"), "10.0.0.5:5840")
        self.assertEqual(resolve_server_address("10.0.0.5"), "10.0.0.5:5840")
        # Empty string resolution uses LAN IP or fallback
        resolved = resolve_server_address("")
        self.assertTrue(":" in resolved)
        self.assertTrue(int(resolved.split(":")[1]) > 0)

    def test_dynamic_root_resolution(self):
        """Verify dynamic extraction of shared root from UNC and filesystem paths."""
        self.assertEqual(resolve_shared_root(r"\\SRV\CustomRoot", r"\\SRV\CustomRoot\Sub"), r"\\SRV\CustomRoot")
        self.assertEqual(resolve_shared_root("", r"\\NAS_HOST\SharedFolder\2026\Project.psx"), r"\\NAS_HOST\SharedFolder")

    def test_find_photos_in_directory(self):
        """Verify recursive photo file discovery."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            sub = os.path.join(tmp_dir, "sub")
            os.makedirs(sub, exist_ok=True)
            f1 = os.path.join(tmp_dir, "img1.JPG")
            f2 = os.path.join(sub, "img2.tif")
            f3 = os.path.join(sub, "notes.txt")
            for f in (f1, f2, f3):
                with open(f, "w", encoding="utf-8") as h:
                    h.write("dummy")

            photos = find_photos_in_directory(tmp_dir)
            self.assertEqual(len(photos), 2)
            self.assertIn(os.path.normpath(f1), photos)
            self.assertIn(os.path.normpath(f2), photos)

    def test_queue_manager_arg_parser(self):
        """Verify manage_queue CLI argument parser subcommands."""
        parser = build_queue_parser()
        args_list = parser.parse_args(["list", "--filter", "running"])
        self.assertEqual(args_list.command, "list")
        self.assertEqual(args_list.filter, "running")

        args_prio = parser.parse_args(["set-priority", "--batch", "42", "--priority", "100"])
        self.assertEqual(args_prio.command, "set-priority")
        self.assertEqual(args_prio.batch, 42)
        self.assertEqual(args_prio.priority, 100)

        args_pause = parser.parse_args(["pause", "--batch", "7"])
        self.assertEqual(args_pause.command, "pause")
        self.assertEqual(args_pause.batch, 7)

        args_abort = parser.parse_args(["abort", "--batch", "9", "--demote-priority", "-50"])
        self.assertEqual(args_abort.command, "abort")
        self.assertEqual(args_abort.batch, 9)
        self.assertEqual(args_abort.demote_priority, -50)

    def test_checkpoint_inspector_arg_parser(self):
        """Verify inspect_checkpoint argument parsing."""
        parser = build_inspect_parser()
        args = parser.parse_args(["--project", r"\\SRV\Share\Project.psx"])
        self.assertEqual(args.project, r"\\SRV\Share\Project.psx")

    def test_monitor_arg_parser(self):
        """Verify cluster_monitor argument parsing."""
        parser = build_monitor_parser()
        args = parser.parse_args(["--min-headroom", "75.0", "--json"])
        self.assertEqual(args.min_headroom, 75.0)
        self.assertTrue(args.json)

    def test_headroom_audit_measurement(self):
        """Verify headroom measurement across existing and non-existing paths."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            res = audit_headroom([tmp_dir, r"Z:\NonExistentDrive_AutoMetashape_Test"])
            self.assertIn(tmp_dir, res)
            self.assertGreater(res[tmp_dir], 0.0)
            self.assertEqual(res[r"Z:\NonExistentDrive_AutoMetashape_Test"], 0.0)


if __name__ == "__main__":
    unittest.main()
