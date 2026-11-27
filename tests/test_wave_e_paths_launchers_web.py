# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Wave E hardening tests for paths, config persistence, launchers, RPC adapters, and dashboard."""

from __future__ import annotations

import io
import os
import posixpath
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")

for p in (_SCRIPTS_DIR, _SOURCE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from autometashape.cluster import (  # noqa: E402
    config as cfg_mod,
    dual_share as ds_mod,
    hardware as hw_mod,
    orchestrator as orch_mod,
)
from scripts import (  # noqa: E402
    cluster_monitor as mon_mod,
    inspect_checkpoint as insp_mod,
    launch_worker as lw_mod,
    manage_queue as mq_mod,
)


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestWaveEPathsLaunchersAndWeb(unittest.TestCase):
    """Adversarial regression suite for Wave E cross-platform, launcher, RPC, and dashboard hardening."""

    def test_dual_share_extended_unc_posix_and_unicode_nfc(self) -> None:
        """Win32 extended UNC/local paths, POSIX // paths, UNC traversal, and NFD/NFC match accurately."""
        self.assertFalse(ds_mod.is_unc_path(r"\\?\C:\Data\SiteA"))
        self.assertFalse(ds_mod.is_unc_path(r"\\.\PHYSICALDRIVE0"))
        self.assertTrue(ds_mod.is_unc_path(r"\\?\UNC\SRV\Share\SiteA"))
        self.assertTrue(ds_mod.is_unc_path(r"\\SRV\Share"))

        with patch.object(ds_mod.sys, "platform", "linux"), patch.object(ds_mod.os, "path", posixpath):
            self.assertFalse(ds_mod.is_unc_path("//tmp/nonexistent_survey/SiteA"))
            self.assertFalse(ds_mod.is_unc_path("//mnt/nas/SiteA"))
            norm_posix = ds_mod.normalize_share_path("//tmp/survey_dir/SiteA")
            self.assertEqual(norm_posix, "/tmp/survey_dir/SiteA")

        self.assertFalse(ds_mod.is_subpath(r"\\?\UNC\srv\share\..\escape", r"\\srv\share"))
        self.assertTrue(ds_mod.is_subpath(r"\\?\UNC\srv\share\Batch\SiteA", r"\\srv\share"))
        self.assertEqual(ds_mod.relpath_under(r"\\?\UNC\srv\share\Batch\SiteA", r"\\srv\share"), "Batch/SiteA")

        nfd_child = unicodedata.normalize("NFD", r"\\srv\share\Đà Nẵng\Cổng")
        nfc_parent = unicodedata.normalize("NFC", r"\\srv\share\Đà Nẵng")
        self.assertTrue(ds_mod.is_subpath(nfd_child, nfc_parent))
        self.assertIsNotNone(ds_mod.relpath_under(nfd_child, nfc_parent))

    def test_dual_share_mixed_root_discovery_depth_appledouble_and_excludes(self) -> None:
        """Discovery preserves mixed root+subfolder photos, honors depth & excludes, skips AppleDouble."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. Mixed root + subfolder tree preserves root photos as unified project
            mixed_src = os.path.join(tmpdir, "mixed_src")
            mixed_res = os.path.join(tmpdir, "mixed_res")
            os.makedirs(os.path.join(mixed_src, "Oblique"), exist_ok=True)
            with open(os.path.join(mixed_src, "DJI_0001.JPG"), "wb") as fh:
                fh.write(b"\xff\xd8\xff")
            with open(os.path.join(mixed_src, "._DJI_0001.JPG"), "wb") as fh:
                fh.write(b"appledouble")
            with open(os.path.join(mixed_src, "Oblique", "OBL_0001.JPG"), "wb") as fh:
                fh.write(b"\xff\xd8\xff")

            with redirect_stdout(io.StringIO()):
                mixed_jobs = ds_mod.discover_dual_share_projects(mixed_src, mixed_res)
            self.assertEqual(len(mixed_jobs), 1)
            self.assertEqual(os.path.normpath(mixed_jobs[0].images_dir), os.path.normpath(mixed_src))

            # 2. Multi-level tree: depth=1 sees 1 grouped parent vs depth=2 separates multi-site leaves
            nested_src = os.path.join(tmpdir, "nested_src")
            nested_res = os.path.join(tmpdir, "nested_res")
            for sub in ("2026_Q1/Site_A", "2026_Q1/Site_B", "Manual"):
                p = os.path.join(nested_src, sub)
                os.makedirs(p, exist_ok=True)
                with open(os.path.join(p, "IMG_01.JPG"), "wb") as fh:
                    fh.write(b"\xff\xd8\xff")

            d1_jobs = ds_mod.discover_dual_share_projects(nested_src, nested_res, discovery_depth=1)
            self.assertEqual([j.project_name for j in d1_jobs], ["2026_Q1", "Manual"])

            d2_jobs = ds_mod.discover_dual_share_projects(
                nested_src,
                nested_res,
                exclude_folders="manual",
                discovery_depth=2,
            )
            self.assertEqual([j.project_name for j in d2_jobs], ["Site_A", "Site_B"])
            self.assertEqual(
                os.path.normpath(d2_jobs[0].output_dir),
                os.path.normpath(os.path.join(nested_res, "2026_Q1", "Site_A")),
            )

    def test_config_site_packages_guard_writable_probe_and_corrupt_warning(self) -> None:
        """Config paths avoid site-packages & read-only NTFS dirs, honor env dir, and warn on corrupt JSON."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = os.path.join(tmpdir, "home")
            fake_site = os.path.join(tmpdir, "Python39", "Lib", "site-packages", "autometashape", "cluster", "cfg.py")
            os.makedirs(os.path.dirname(fake_site), exist_ok=True)

            with (
                patch.object(cfg_mod, "__file__", fake_site),
                patch.dict(os.environ, {}, clear=False),
                patch("os.path.expanduser", return_value=fake_home),
            ):
                os.environ.pop("AUTOMETASHAPE_CONFIG_DIR", None)
                resolved = cfg_mod.get_default_config_path("worker_config.json", for_write=True)
                self.assertEqual(
                    os.path.normpath(resolved),
                    os.path.normpath(os.path.join(fake_home, ".autometashape", "worker_config.json")),
                )

            # Real repo checkout falls back to ~/.autometashape when write probe fails
            with (
                patch.object(cfg_mod, "_is_dir_writable", return_value=False),
                patch.dict(os.environ, {}, clear=False),
                patch("os.path.expanduser", return_value=fake_home),
            ):
                os.environ.pop("AUTOMETASHAPE_CONFIG_DIR", None)
                rw_fallback = cfg_mod.get_default_config_path("worker_config.json", for_write=True)
                self.assertEqual(
                    os.path.normpath(rw_fallback),
                    os.path.normpath(os.path.join(fake_home, ".autometashape", "worker_config.json")),
                )

            # AUTOMETASHAPE_CONFIG_DIR override takes priority
            custom_dir = os.path.join(tmpdir, "custom_cfg")
            with patch.dict(os.environ, {"AUTOMETASHAPE_CONFIG_DIR": custom_dir}):
                env_cfg = cfg_mod.get_default_config_path("server_config.json", for_write=True)
                self.assertEqual(
                    os.path.normpath(env_cfg),
                    os.path.normpath(os.path.join(custom_dir, "server_config.json")),
                )

            # Corrupt JSON emits warning and returns empty dict
            bad_json = os.path.join(tmpdir, "bad.json")
            with open(bad_json, "w", encoding="utf-8") as fh:
                fh.write("{not valid json")
            err_buf = io.StringIO()
            with redirect_stderr(err_buf):
                loaded = cfg_mod.load_cluster_config(bad_json)
            self.assertEqual(loaded, {})
            self.assertIn("WARNING", err_buf.getvalue())

    def test_hardware_gpu_mask_clamp_lan_override_and_rpc_helpers(self) -> None:
        """GPU classifiers, 32-bit clamp, NFC normalization, LAN override, RPC adapters, and BAT quoting work."""
        nfd_path = unicodedata.normalize("NFD", "C:/Surveys/Đà Nẵng/ảnh")
        nfc_expected = os.path.normpath(unicodedata.normalize("NFC", nfd_path))
        self.assertEqual(hw_mod.normalize_unicode_path(nfd_path), nfc_expected)

        self.assertTrue(hw_mod.is_integrated_gpu("AMD Radeon(TM) RX Vega 8 Graphics"))
        self.assertTrue(hw_mod.is_integrated_gpu("Intel(R) Arc(TM) 140V Graphics"))
        self.assertFalse(hw_mod.is_integrated_gpu("AMD Radeon RX Vega 64"))
        self.assertFalse(hw_mod.is_integrated_gpu("Intel(R) Arc(TM) C770 Graphics"))

        gpus_33 = [{"name": "NVIDIA RTX 5080", "vendor": "NVIDIA"}] + [
            {"name": "Dummy GPU", "vendor": "NVIDIA"} for _ in range(32)
        ]
        mask, selected = hw_mod.compute_discrete_gpu_mask(gpus_33)
        self.assertEqual(mask, (1 << 31) - 1)
        self.assertEqual(len(selected), 31)

        with patch.dict(os.environ, {"AUTOMETASHAPE_LAN_IP": "10.99.0.5"}):
            self.assertEqual(hw_mod.get_primary_lan_ip(), "10.99.0.5")

        self.assertEqual(hw_mod.format_host_port("::1", 5840), "[::1]:5840")
        self.assertEqual(hw_mod.format_host_port("[fe80::1]", 5840), "[fe80::1]:5840")
        self.assertEqual(hw_mod.normalize_rpc_list_response([{"batch_id": 1}, None], "batches"), [{"batch_id": 1}])
        self.assertEqual(
            hw_mod.normalize_rpc_list_response(SimpleNamespace(workers=[SimpleNamespace(worker_id=2)]), "workers"),
            [{"worker_id": 2}],
        )

        class _OneArgClient:
            def __init__(self) -> None:
                self.calls = []

            def connect(self, *args) -> None:
                self.calls.append(args)
                if len(args) > 1:
                    raise TypeError("takes 1 argument")

        cli = _OneArgClient()
        hw_mod.connect_network_client(cli, "::1", 5840)
        self.assertEqual(cli.calls, [("::1", 5840), ("[::1]:5840",)])

        bat = hw_mod.generate_worker_bat_content(host="192.168.1.10", port=5840, root_path="")
        self.assertIn("setlocal DisableDelayedExpansion", bat)
        self.assertNotIn("enabledelayedexpansion", bat.lower())
        self.assertIn(":double_drive_slash", bat)
        self.assertIn('--root "%CLI_ROOT%"', bat)
        self.assertIn('echo [OK] Shared Network Root   : "%TARGET_ROOT%"', bat)

    def test_manage_queue_monitor_checkpoint_launch_worker_and_orchestrator(self) -> None:
        """Queue hex mask, monitor bytes/dashboard, read-only inspect, zero-compute guard, and env cleanup work."""
        parser = mq_mod.build_arg_parser()
        parsed_hex = parser.parse_args(["set-worker-gpu-mask", "--hostname", "n1", "--gpu-mask", "0x3"])
        self.assertEqual(parsed_hex.gpu_mask, 3)
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            parser.parse_args(["set-worker-gpu-mask", "--hostname", "n1", "--gpu-mask", "-1"])

        list_client = MagicMock()
        list_client.workerList.return_value = [{"worker_id": 5, "hostname": "NodeA.corp.local", "state": "ready"}]
        list_client.batchList.return_value = [{"batch_id": 7, "priority": 10, "state": "pending", "path": "S/S.psx"}]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(mq_mod.cmd_list(list_client, "all"), 0)
            self.assertEqual(mq_mod.cmd_set_worker_paused(list_client, True, hostname="NodeA"), 0)
        list_client.setWorkerPaused.assert_called_once_with(5, True)

        _, gb_bytes = mon_mod.format_worker_resource_usage(
            {"memory_size": 0, "resource_usage": {"cpu_usage": 1.0, "memory_usage": float(8 * (1024**3))}}
        )
        self.assertAlmostEqual(gb_bytes, 8.0, places=2)
        _, gb_unknown = mon_mod.format_worker_resource_usage(
            {"memory_size": 0, "resource_usage": {"cpu_usage": 1.0, "memory_usage": 50.0}}
        )
        self.assertAlmostEqual(gb_unknown, 0.0, places=2)

        with tempfile.TemporaryDirectory() as tmpdir:
            dash_js = os.path.join(tmpdir, "projects_data.js")
            audit_payload = {
                "deliverables": {
                    12: {
                        "intact": True,
                        "project": "SiteX",
                        "path": tmpdir,
                        "obj": "SiteX.obj",
                        "obj_sz_mb": 12.5,
                        "textures_count": 4,
                    }
                },
                "running_batches": [],
            }
            mon_mod.export_dashboard_telemetry(audit_payload, dash_js)
            content = _read_text(dash_js)
            self.assertIn("window.SURVEY_PROJECTS =", content)
            self.assertIn('"SiteX"', content)

            psx_p = os.path.join(tmpdir, "Check.psx")
            with open(psx_p, "w", encoding="utf-8") as fh:
                fh.write("<document/>")

            def _rw_reject(*args, **kwargs):
                if kwargs:
                    raise TypeError("unexpected kwargs")
                raise AssertionError("Must NEVER fall back to read-write doc.open(path)!")

            ro_doc = MagicMock()
            ro_doc.open.side_effect = _rw_reject
            with (
                patch.object(insp_mod, "Metashape", SimpleNamespace(Document=lambda: ro_doc)),
                self.assertRaises(RuntimeError),
            ):
                insp_mod.inspect_project(psx_p)

            stats_doc = MagicMock()
            stats_doc.chunks = [
                SimpleNamespace(
                    label="C1",
                    cameras=[
                        SimpleNamespace(transform=object(), enabled=True),
                        SimpleNamespace(transform=object(), enabled=False),
                    ],
                    tie_points=SimpleNamespace(points=[1]),
                    point_cloud=None,
                    depth_maps=None,
                    model=SimpleNamespace(statistics=lambda: SimpleNamespace(faces=500, vertices=300)),
                    orthomosaic=None,
                    elevation=None,
                    crs=None,
                    sensors=[],
                    markers=[],
                )
            ]
            with (
                patch.object(insp_mod, "Metashape", SimpleNamespace(Document=lambda: stats_doc)),
                redirect_stdout(io.StringIO()),
            ):
                info = insp_mod.inspect_project(psx_p)
            self.assertEqual(info["chunks"][0]["model_faces"], 500)
            self.assertEqual(info["chunks"][0]["aligned_disabled_cameras"], 1)

            # 0-compute guard in launch_worker forces --cpu-enable 1
            fake_ms_exe = os.path.join(tmpdir, "metashape.exe")
            with open(fake_ms_exe, "wb") as fh:
                fh.write(b"MZ")
            cfg_file = os.path.join(tmpdir, "worker_config.json")
            captured_cmds = []
            lw_args = lw_mod.build_arg_parser().parse_args(
                [
                    "--host",
                    "::1",
                    "--root",
                    tmpdir,
                    "--cpu-enable",
                    "0",
                    "--config",
                    cfg_file,
                    "--non-interactive",
                    "--no-reconnect",
                ]
            )
            fake_proc = MagicMock(returncode=0)
            fake_proc.poll.return_value = 0

            def _fake_popen(cmd, **kwargs):
                captured_cmds.append(cmd)
                return fake_proc

            with (
                patch.object(lw_mod.cu, "find_metashape_path", return_value=fake_ms_exe),
                patch.object(lw_mod.cu, "probe_metashape_gpu_mask", return_value=(0, [])),
                patch.object(lw_mod.cu, "detect_gpu_devices", return_value=[]),
                patch.object(lw_mod.cu, "test_tcp_connection", return_value=True),
                patch.object(lw_mod.cu, "test_unc_path", return_value=(True, "")),
                patch("subprocess.Popen", side_effect=_fake_popen),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                lw_mod._execute_worker_lifecycle(lw_args)
            self.assertEqual(len(captured_cmds), 1)
            cmd0 = captured_cmds[0]
            self.assertEqual(cmd0[cmd0.index("--host") + 1], "[::1]:5840")
            self.assertEqual(cmd0[cmd0.index("--cpu-enable") + 1], "1")

            # orchestrator leading-'-' flag & env restoration
            with patch.object(orch_mod, "Metashape", None):
                task, _, _ = orch_mod.build_monolithic_runscript_task(
                    images_dir=tmpdir,
                    output_dir=tmpdir,
                    project_name="-LeadDashSite",
                    exclude_folders="manual",
                )
            self.assertIn("--project-name=-LeadDashSite", task.args)
            self.assertIn("finally:", task.code)

    def test_launchers_and_dashboard_static_integrity(self) -> None:
        """Windows .bat, new POSIX .sh scripts (LF + SPDX), and offline/XSS-safe HTML pass checks."""
        for bat_name in ("update.bat", "launch_worker.bat", "launch_server.bat"):
            bat_text = _read_text(os.path.join(_REPO_ROOT, bat_name))
            self.assertIn("if [%1]==[] goto :eof", bat_text, msg=f"{bat_name} missing empty-arg-safe shift check")

        update_bat = _read_text(os.path.join(_REPO_ROOT, "update.bat"))
        self.assertIn('echo Node files are ready in "%TARGET_DIR%".', update_bat)

        sh_files = ("launch_server.sh", "launch_worker.sh", "cluster_abort.sh", "update_fleet.sh")
        bash_bin = None
        for cand in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"):
            if os.path.isfile(cand):
                bash_bin = cand
                break
        for sh_name in sh_files:
            sh_path = os.path.join(_REPO_ROOT, sh_name)
            self.assertTrue(os.path.isfile(sh_path), msg=f"Missing POSIX launcher: {sh_name}")
            with open(sh_path, "rb") as fh:
                raw_bytes = fh.read()
            self.assertNotIn(b"\r", raw_bytes, msg=f"CRLF detected in {sh_name}; must be LF only")
            sh_text = raw_bytes.decode("utf-8")
            self.assertIn("SPDX-License-Identifier: Apache-2.0", sh_text)
            if bash_bin:
                chk = subprocess.run([bash_bin, "-n", sh_path], capture_output=True, text=True)
                self.assertEqual(chk.returncode, 0, msg=f"bash -n failed on {sh_name}: {chk.stderr}")

        html_text = _read_text(os.path.join(_REPO_ROOT, "web", "projects_dashboard.html"))
        self.assertNotIn("fonts.googleapis.com", html_text)
        self.assertNotIn(".innerHTML", html_text)
        self.assertNotIn("new Function", html_text)
        self.assertIn('window.location.protocol === "file:"', html_text)
        self.assertIn('id="shell-select"', html_text)


if __name__ == "__main__":
    unittest.main()
