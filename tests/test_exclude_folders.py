# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for exclude-folder ingestion filtering and cluster RunScript shim."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

from autometashape.cluster import orchestrator as orch_mod
from autometashape.cluster.orchestrator import build_monolithic_runscript_task
from autometashape.core.telemetry import discover_image_subfolder_groups


class TestExcludeFolders(unittest.TestCase):
    """Verify subfolder exclusion across flat, nested, Unicode, and legacy-worker shim paths."""

    def test_discover_excludes_flat_nested_and_unicode_folders(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            rel_files = [
                "Nadir/n1.jpg",
                "OB1/o1.jpg",
                "Manual/m1.jpg",
                "0038_Manual/m2.jpg",
                "Cổng/DJI_001/g1.jpg",
                "CATP_0041_1/Manual/m3.jpg",
                "CATP_0041_1/Nadir/n2.jpg",
                "CATP_0156_1/0156-tru so CABD/DJI_004/c1.jpg",
            ]
            for rel in rel_files:
                fp = os.path.join(tmpdir, rel)
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                with open(fp, "wb") as fh:
                    fh.write(b"\xff\xd8\xff")

            groups = discover_image_subfolder_groups(tmpdir, exclude_folders="manual,cổng,tru so cabd")
            kept = sorted(os.path.relpath(p, tmpdir).replace("\\", "/") for paths in groups.values() for p in paths)
            self.assertEqual(kept, ["CATP_0041_1/Nadir/n2.jpg", "Nadir/n1.jpg", "OB1/o1.jpg"])

    def _extract_code(self, task_obj: object) -> str:
        if isinstance(task_obj, dict):
            return str(task_obj.get("params", {}).get("code") or task_obj.get("code", ""))
        params = getattr(task_obj, "params", None)
        if isinstance(params, dict) and "code" in params:
            return str(params["code"])
        return str(getattr(task_obj, "code", ""))

    def test_monolithic_runscript_bootstrap_compiles_and_clears_env(self) -> None:
        with patch.object(orch_mod, "Metashape", None):
            task_excl, _, _ = build_monolithic_runscript_task(
                images_dir=r"\\server\share\EMERGENCY\CATP_0095",
                output_dir=r"\\server\share\EMERGENCY\Result\CATP_0095",
                project_name="CATP_0095",
                network_path=r"\\server\share",
                exclude_folders="manual,cổng",
            )
            code_excl = self._extract_code(task_excl)
            compile(code_excl, "<bootstrap_excl>", "exec")
            self.assertIn("AUTOMETASHAPE_EXCLUDE_FOLDERS", code_excl)
            self.assertIn("manual,cổng", code_excl)

            task_plain, _, _ = build_monolithic_runscript_task(
                images_dir=r"\\server\share\EMERGENCY\CATP_0095",
                output_dir=r"\\server\share\EMERGENCY\Result\CATP_0095",
                project_name="CATP_0095",
                network_path=r"\\server\share",
            )
            code_plain = self._extract_code(task_plain)
            compile(code_plain, "<bootstrap_plain>", "exec")
            self.assertIn('os.environ.pop("AUTOMETASHAPE_EXCLUDE_FOLDERS", None)', code_plain)

    def test_legacy_worker_shim_filters_nested_paths_without_new_param(self) -> None:
        """Simulate a worker on 86870e8 whose discover_image_subfolder_groups lacks exclude_folders."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rel_files = [
                "Nadir/n1.jpg",
                "Manual/m1.jpg",
                "CATP_0041_1/Manual/m2.jpg",
                "CATP_0041_1/Nadir/n2.jpg",
                "Cổng/DJI_001/g1.jpg",
            ]
            for rel in rel_files:
                fp = os.path.join(tmpdir, rel)
                os.makedirs(os.path.dirname(fp), exist_ok=True)
                with open(fp, "wb") as fh:
                    fh.write(b"\xff\xd8\xff")

            # Legacy 2-arg function mimicking commit 86870e8
            def legacy_disc(images_dir: str, output_dir: str = None) -> dict:
                return discover_image_subfolder_groups(images_dir, output_dir=output_dir, exclude_folders="")

            import autometashape.core.telemetry as tel_mod

            real_fn = tel_mod.discover_image_subfolder_groups
            try:
                tel_mod.discover_image_subfolder_groups = legacy_disc
                if hasattr(tel_mod, "_orig_discover_image_subfolder_groups"):
                    delattr(tel_mod, "_orig_discover_image_subfolder_groups")

                with patch.object(orch_mod, "Metashape", None):
                    task_excl, _, _ = build_monolithic_runscript_task(
                        images_dir=tmpdir,
                        output_dir=os.path.join(tmpdir, "Result"),
                        project_name="LEGACY_TEST",
                        exclude_folders="manual,cổng",
                    )
                code_excl = self._extract_code(task_excl)
                # Execute only the setup portion before runpy.run_path
                setup_only = code_excl.split("sys.argv = ")[0]
                exec(compile(setup_only, "<legacy_shim>", "exec"), {"__builtins__": __builtins__})

                res = tel_mod.discover_image_subfolder_groups(tmpdir)
                kept = sorted(os.path.relpath(p, tmpdir).replace("\\", "/") for paths in res.values() for p in paths)
                self.assertEqual(kept, ["CATP_0041_1/Nadir/n2.jpg", "Nadir/n1.jpg"])
            finally:
                tel_mod.discover_image_subfolder_groups = real_fn
                if hasattr(tel_mod, "_orig_discover_image_subfolder_groups"):
                    delattr(tel_mod, "_orig_discover_image_subfolder_groups")
                os.environ.pop("AUTOMETASHAPE_EXCLUDE_FOLDERS", None)


if __name__ == "__main__":
    unittest.main()
