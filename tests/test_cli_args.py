# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for AutoMetashape CLI argument parsing consistency."""

from __future__ import annotations

import os
import sys
import unittest

# Ensure scripts directory is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from metashape_export import build_arg_parser as build_export_parser
from metashape_reconstruction import build_arg_parser as build_recon_parser


class TestCliArgs(unittest.TestCase):
    """Test CLI arguments consistency across pipeline subsystems."""

    def test_recon_parser_accepts_no_export_dem(self) -> None:
        """Defend against unrecognized --no-export-dem argument when exporting is decoupled."""
        parser = build_recon_parser()
        args = parser.parse_args(
            [
                "--images",
                "dummy_images",
                "--build-dem",
                "--no-export-dem",
            ]
        )
        self.assertTrue(args.build_dem)
        self.assertFalse(args.export_dem)

    def test_recon_parser_accepts_export_dem(self) -> None:
        """Defend --export-dem flag activating GeoTIFF export."""
        parser = build_recon_parser()
        args = parser.parse_args(
            [
                "--images",
                "dummy_images",
                "--build-dem",
                "--export-dem",
            ]
        )
        self.assertTrue(args.build_dem)
        self.assertTrue(args.export_dem)

    def test_recon_parser_ortho_decoupling(self) -> None:
        """Defend that building orthomosaic does not export GeoTIFF by default."""
        parser = build_recon_parser()

        # Default: no export
        default_args = parser.parse_args(["--images", "dummy_images"])
        self.assertFalse(default_args.export_ortho)

        # Explicitly enabled export
        export_args = parser.parse_args(
            [
                "--images",
                "dummy_images",
                "--ortho",
                "--export-ortho",
            ]
        )
        self.assertTrue(export_args.build_orthomosaic)
        self.assertTrue(export_args.export_ortho)

        # Disabled export explicitly
        no_export_args = parser.parse_args(
            [
                "--images",
                "dummy_images",
                "--ortho",
                "--no-export-ortho",
            ]
        )
        self.assertTrue(no_export_args.build_orthomosaic)
        self.assertFalse(no_export_args.export_ortho)

    def test_recon_parser_point_cloud_decoupling(self) -> None:
        """Defend decoupling between building point cloud and exporting point cloud file."""
        parser = build_recon_parser()

        # Case 1: Built natively into project, but not exported to disk file
        args_no_export = parser.parse_args(
            [
                "--images",
                "dummy_images",
                "--build-point-cloud",
                "--no-export-point-cloud",
            ]
        )
        self.assertTrue(args_no_export.build_point_cloud)
        self.assertFalse(args_no_export.export_point_cloud)

        # Case 2: Explicitly exported
        args_export = parser.parse_args(
            [
                "--images",
                "dummy_images",
                "--build-point-cloud",
                "--export-point-cloud",
            ]
        )
        self.assertTrue(args_export.build_point_cloud)
        self.assertTrue(args_export.export_point_cloud)

        # Case 3: Fully disabled
        args_disabled = parser.parse_args(
            [
                "--images",
                "dummy_images",
                "--no-point-cloud",
                "--no-export-point-cloud",
            ]
        )
        self.assertFalse(args_disabled.build_point_cloud)
        self.assertFalse(args_disabled.export_point_cloud)

    def test_recon_parser_reference_preselection_modes(self) -> None:
        """Defend reference preselection mode choices."""
        parser = build_recon_parser()

        # Default is estimated
        default_args = parser.parse_args(["--images", "dummy_images"])
        self.assertEqual(default_args.reference_preselection_mode, "estimated")

        for mode in ("estimated", "source", "sequential"):
            args = parser.parse_args(
                [
                    "--images",
                    "dummy_images",
                    "--reference-preselection-mode",
                    mode,
                ]
            )
            self.assertEqual(args.reference_preselection_mode, mode)

    def test_recon_parser_crop_choices(self) -> None:
        """Defend crop-method choices including gps-square and none."""
        parser = build_recon_parser()
        default_args = parser.parse_args(["--images", "dummy_images"])
        self.assertIsNone(default_args.crop_method)

        for method in ("gps-square", "structure", "manual", "none"):
            args = parser.parse_args(
                [
                    "--images",
                    "dummy_images",
                    "--crop-method",
                    method,
                ]
            )
            self.assertEqual(args.crop_method, method)

        no_crop_args = parser.parse_args(
            [
                "--images",
                "dummy_images",
                "--no-crop",
            ]
        )
        self.assertTrue(no_crop_args.no_crop)

    def test_recon_parser_presets(self) -> None:
        """Defend supported presets including building alias."""
        parser = build_recon_parser()
        for preset in ("pro", "custom", "building"):
            args = parser.parse_args(
                [
                    "--images",
                    "dummy_images",
                    "--preset",
                    preset,
                ]
            )
            self.assertEqual(args.preset, preset)

    def test_recon_parser_network_cluster_args(self) -> None:
        """Defend distributed network cluster CLI flags in reconstruction parser."""
        parser = build_recon_parser()
        # Default: disabled
        default_args = parser.parse_args(["--images", "dummy_images"])
        self.assertFalse(default_args.network_enable)
        self.assertEqual(default_args.network_host, "127.0.0.1")
        self.assertEqual(default_args.network_port, 5840)
        self.assertEqual(default_args.network_path, "")

        # Explicit network configuration
        net_args = parser.parse_args(
            [
                "--images",
                "dummy_images",
                "--network-enable",
                "--network-host",
                "192.168.1.150",
                "--network-port",
                "5842",
                "--network-path",
                r"\\SERVER\RealityScan",
            ]
        )
        self.assertTrue(net_args.network_enable)
        self.assertEqual(net_args.network_host, "192.168.1.150")
        self.assertEqual(net_args.network_port, 5842)
        self.assertEqual(net_args.network_path, r"\\SERVER\RealityScan")

    def test_recon_parser_independent_workers_arg(self) -> None:
        """Defend --network-independent-workers default=True and --no-network-independent-workers opt-out."""
        parser = build_recon_parser()
        default_args = parser.parse_args(["--images", "dummy_images"])
        self.assertTrue(default_args.network_independent_workers)
        self.assertTrue(default_args.local_scratch_staging)

        optout_args = parser.parse_args(
            [
                "--images",
                "dummy_images",
                "--network-enable",
                "--no-network-independent-workers",
            ]
        )
        self.assertTrue(optout_args.network_enable)
        self.assertFalse(optout_args.network_independent_workers)

        indep_args = parser.parse_args(
            [
                "--images",
                "dummy_images",
                "--network-enable",
                "--network-independent-workers",
            ]
        )
        self.assertTrue(indep_args.network_enable)
        self.assertTrue(indep_args.network_independent_workers)

    def test_export_parser_arguments(self) -> None:
        """Defend standalone export argument parser accepts required and optional flags."""
        parser = build_export_parser()
        args = parser.parse_args(
            [
                "--project",
                "dummy.psx",
                "--output",
                "dummy_out",
                "--export-dem",
                "--export-orthomosaic",
                "--export-contours",
                "--export-point-cloud",
                "--export-fbx",
                "--export-ply",
                "--export-dxf",
                "--export-tie-points",
            ]
        )
        self.assertEqual(args.project, "dummy.psx")
        self.assertEqual(args.output, "dummy_out")
        self.assertTrue(args.export_dem)
        self.assertTrue(args.export_orthomosaic)
        self.assertTrue(args.export_contours)
        self.assertTrue(args.export_point_cloud)
        self.assertTrue(args.export_fbx)
        self.assertTrue(args.export_ply)
        self.assertTrue(args.export_dxf)
        self.assertTrue(args.export_tie_points)
        self.assertTrue(args.export_glb)
        self.assertTrue(args.export_obj)

        # Defend PNG texture format and model export suppression
        args_png = parser.parse_args(
            [
                "--project",
                "dummy.psx",
                "--texture-format",
                "png",
                "--no-export-glb",
                "--no-export-obj",
            ]
        )
        self.assertEqual(args_png.texture_format, "png")
        self.assertFalse(args_png.export_glb)
        self.assertFalse(args_png.export_obj)

        for fmt in ("png", "tiff", "tif", "jpg", "jpeg"):
            fmt_args = parser.parse_args(
                [
                    "--project",
                    "dummy.psx",
                    "--texture-format",
                    fmt,
                ]
            )
            self.assertEqual(fmt_args.texture_format, fmt)

    def test_metashape_cluster_binary_discovery(self) -> None:
        """Verify metashape-server and monitor binary discovery functions deterministically."""
        import tempfile

        from cluster_utils import find_metashape_monitor_path, find_metashape_server_path

        with tempfile.TemporaryDirectory() as tmpdir:
            ms_name = "metashape.exe" if sys.platform == "win32" else "metashape"
            srv_name = "metashape-server.exe" if sys.platform == "win32" else "metashape-server"
            mon_name = "monitor.exe" if sys.platform == "win32" else "monitor"
            ms_exe = os.path.join(tmpdir, ms_name)
            srv_exe = os.path.join(tmpdir, srv_name)
            mon_exe = os.path.join(tmpdir, mon_name)
            for p in (ms_exe, srv_exe, mon_exe):
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write("stub")

            found_srv = find_metashape_server_path(ms_exe)
            self.assertEqual(os.path.normpath(found_srv), os.path.normpath(srv_exe))
            found_mon = find_metashape_monitor_path(ms_exe)
            self.assertEqual(os.path.normpath(found_mon), os.path.normpath(mon_exe))

    def test_generate_worker_bat_content(self) -> None:
        """Verify automated launcher batch file generation."""
        from cluster_utils import generate_worker_bat_content

        bat = generate_worker_bat_content(
            host="192.168.1.100",
            port="5840",
            root_path=r"\\OFFICE-NAS\MetashapeProjects",
        )
        self.assertIn("192.168.1.100:5840", bat)
        self.assertIn(r"\\OFFICE-NAS\MetashapeProjects", bat)
        self.assertIn("--worker", bat)
        self.assertIn("--root", bat)
        self.assertIn("@echo off", bat)

    def test_recon_parser_texture_format(self) -> None:
        """Defend --texture-format choices and default in reconstruction parser."""
        parser = build_recon_parser()
        default_args = parser.parse_args(["--images", "dummy_images"])
        self.assertEqual(default_args.texture_format, "png")

        for fmt in ("png", "tiff", "tif", "jpg", "jpeg"):
            args = parser.parse_args(
                [
                    "--images",
                    "dummy_images",
                    "--texture-format",
                    fmt,
                ]
            )
            self.assertEqual(args.texture_format, fmt)

    def test_worker_launcher_parser(self) -> None:
        """Defend launch_worker argument parser accepts required and optional cluster flags."""
        import launch_worker as lw

        parser = lw.build_arg_parser()
        args = parser.parse_args(
            [
                "--host",
                "192.168.1.100",
                "--port",
                "5840",
                "--root",
                r"\\SERVER\SharedMetashape",
                "--cpu-enable",
                "1",
                "--gpu-mask",
                "1",
                "--no-reconnect",
            ]
        )
        self.assertEqual(args.host, "192.168.1.100")
        self.assertEqual(args.port, "5840")
        self.assertEqual(args.root, r"\\SERVER\SharedMetashape")
        self.assertEqual(args.cpu_enable, 1)
        self.assertEqual(args.gpu_mask, "1")
        self.assertTrue(args.no_reconnect)

    def test_worker_launcher_accepts_worker_flag_and_aliases(self) -> None:
        """Verify launch_worker parser accepts --worker, --gpu, and --cpu aliases without error."""
        import launch_worker as lw

        parser = lw.build_arg_parser()
        args = parser.parse_args(
            [
                "--worker",
                "--host",
                "192.168.1.100",
                "--root",
                r"\\SERVER\SharedMetashape",
                "--cpu",
                "1",
                "--gpu",
                "1",
            ]
        )
        self.assertTrue(args.worker)
        self.assertEqual(args.cpu_enable, 1)
        self.assertEqual(args.gpu_mask, "1")

    def test_server_launcher_accepts_server_flag(self) -> None:
        """Verify launch_server parser accepts --server flag without error."""
        import launch_server as ls

        parser = ls.build_arg_parser()
        args = parser.parse_args(
            [
                "--server",
                "--host",
                "0.0.0.0",
                "--port",
                "5840",
            ]
        )
        self.assertTrue(args.server)
        self.assertEqual(args.port, "5840")

    def test_server_launcher_missing_binary_returns_code_1(self) -> None:
        """Verify launch_server returns exit code 1 when no Metashape binary is found."""
        from unittest.mock import patch

        import launch_server as ls

        with patch("sys.argv", ["launch_server.py"]):
            with patch("cluster_utils.find_metashape_path", return_value=None):
                with patch("cluster_utils.find_metashape_server_path", return_value=None):
                    code = ls._run_server_lifecycle()
                    self.assertEqual(code, 1)

    def test_server_launcher_missing_standalone_server_rejects_legacy_embedded(self) -> None:
        """Verify launch_server rejects Metashape 1.x embedded fallback and exits 1 if metashape-server.exe is absent."""
        import tempfile
        from unittest.mock import patch

        import launch_server as ls

        with tempfile.TemporaryDirectory() as tmpdir:
            dummy_ms = os.path.join(tmpdir, "dummy_metashape.exe")
            with open(dummy_ms, "w", encoding="utf-8") as f:
                f.write("mock")

            with patch("sys.argv", ["launch_server.py", "--host", "127.0.0.1", "--port", "5840"]):
                with patch("cluster_utils.find_metashape_path", return_value=dummy_ms):
                    with patch("cluster_utils.find_metashape_server_path", return_value=None):
                        with patch("subprocess.Popen") as mock_popen:
                            code = ls._run_server_lifecycle()
                            self.assertEqual(code, 1)
                            mock_popen.assert_not_called()

    def test_server_launcher_standalone_mode_command_structure(self) -> None:
        """Verify launch_server uses standalone syntax when metashape-server.exe is found."""
        import tempfile
        from unittest.mock import MagicMock, patch

        import launch_server as ls

        with tempfile.TemporaryDirectory() as tmpdir:
            dummy_srv = os.path.join(tmpdir, "dummy_metashape_server.exe")
            with open(dummy_srv, "w", encoding="utf-8") as f:
                f.write("mock")

            with patch("sys.argv", ["launch_server.py", "--host", "0.0.0.0", "--port", "5840"]):
                with patch("cluster_utils.find_metashape_server_path", return_value=dummy_srv):
                    with patch("cluster_utils.test_tcp_connection", return_value=False):
                        with patch("subprocess.Popen") as mock_popen:
                            mock_proc = MagicMock()
                            mock_proc.wait.return_value = 0
                            mock_proc.returncode = 0
                            mock_popen.return_value = mock_proc

                            code = ls._run_server_lifecycle()
                            self.assertEqual(code, 0)
                            cmd = mock_popen.call_args[0][0]
                            self.assertEqual(cmd[0], dummy_srv)
                            self.assertIn("--host", cmd)
                            self.assertIn("0.0.0.0:5840", cmd)
                            self.assertIn("--no-database", cmd)
                            self.assertIn("--resume-workers", cmd)

    def test_server_launcher_pid_recording_and_cleanup(self) -> None:
        """Verify server launcher records and purges its PID tracking file in an isolated temp path."""
        import tempfile
        from unittest.mock import patch

        import launch_server as ls

        with tempfile.TemporaryDirectory() as tmpdir:
            iso_pid_path = os.path.join(tmpdir, "autometashape_server.pid")
            with patch.object(ls, "PID_FILE_PATH", iso_pid_path):
                ls.record_server_pid()
                self.assertTrue(os.path.isfile(iso_pid_path))
                with open(iso_pid_path, "r", encoding="utf-8") as f:
                    pid = int(f.read().strip())
                self.assertEqual(pid, os.getpid())

                ls.clear_server_pid()
                self.assertFalse(os.path.isfile(iso_pid_path))

    def test_worker_launcher_loads_and_prioritizes_worker_config(self) -> None:
        """Verify launch_worker._run_worker_lifecycle resolves worker_config.json and honors CLI overrides."""
        import json
        import tempfile
        from unittest.mock import patch

        import launch_worker as lw

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ms = os.path.join(tmpdir, "metashape.exe")
            with open(fake_ms, "w", encoding="utf-8") as fh:
                fh.write("stub")
            cfg_file = os.path.join(tmpdir, "worker_config.json")
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "host": "10.0.0.5",
                        "port": "5842",
                        "root": r"\\NAS\MetashapeData",
                        "cpu_enable": 0,
                        "gpu_mask": "2",
                    },
                    f,
                )

            def _run_with(argv: list[str]) -> list[str]:
                with (
                    patch("cluster_utils.find_metashape_path", return_value=fake_ms),
                    patch("cluster_utils.probe_metashape_gpu_mask", return_value=(None, [])),
                    patch("cluster_utils.test_tcp_connection", return_value=True),
                    patch("cluster_utils.test_unc_path", return_value=(True, "")),
                    patch("subprocess.Popen") as mock_popen,
                    patch("sys.argv", argv),
                ):
                    mock_proc = mock_popen.return_value
                    mock_proc.wait.return_value = 0
                    mock_proc.returncode = 0
                    mock_proc.poll.return_value = 0
                    self.assertEqual(lw._run_worker_lifecycle(), 0)
                    return mock_popen.call_args[0][0]

            # Case A: CLI args omitted -> resolves from config file
            cmd_a = _run_with(["launch_worker.py", "--headless", "--no-reconnect", "--config", cfg_file])
            self.assertIn("10.0.0.5:5842", cmd_a)
            self.assertIn(r"\\NAS\MetashapeData", cmd_a)
            self.assertEqual(cmd_a[cmd_a.index("--cpu-enable") + 1], "0")
            self.assertEqual(cmd_a[cmd_a.index("--gpu-mask") + 1], "2")

            # Case B: CLI args provided -> overrides config file
            cmd_b = _run_with(
                ["launch_worker.py", "--headless", "--no-reconnect", "--config", cfg_file, "--cpu", "1", "--gpu", "3"]
            )
            self.assertEqual(cmd_b[cmd_b.index("--cpu-enable") + 1], "1")
            self.assertEqual(cmd_b[cmd_b.index("--gpu-mask") + 1], "3")

            # Case C: gpu_mask="auto" -> purged from command when probe returns None
            cfg_auto = os.path.join(tmpdir, "worker_auto.json")
            with open(cfg_auto, "w", encoding="utf-8") as f:
                json.dump({"host": "10.0.0.5", "root": r"\\NAS\MetashapeData", "gpu_mask": "auto"}, f)
            cmd_c = _run_with(["launch_worker.py", "--headless", "--no-reconnect", "--config", cfg_auto])
            self.assertNotIn("--gpu-mask", cmd_c)

    def test_worker_launcher_non_interactive_flags(self) -> None:
        """Verify launch_worker parser accepts --non-interactive and --headless flags."""
        import launch_worker as lw

        parser = lw.build_arg_parser()
        args1 = parser.parse_args(["--non-interactive"])
        self.assertTrue(args1.non_interactive)

        args2 = parser.parse_args(["--headless"])
        self.assertTrue(args2.non_interactive)

    def test_worker_launcher_headless_missing_params_exits_without_input(self) -> None:
        """Verify that launch_worker in headless mode exits with code 1 and never calls input()."""
        from unittest.mock import patch

        import launch_worker as lw

        # If input() were called, mock raises AssertionError
        with patch("builtins.input", side_effect=AssertionError("input() must not be called in headless mode!")):
            with patch("sys.argv", ["launch_worker.py", "--headless", "--config", "non_existent_config.json"]):
                ret = lw._run_worker_lifecycle()
                self.assertEqual(ret, 1)

    def test_worker_launcher_headless_resolves_env_vars(self) -> None:
        """Verify that launch_worker resolves AUTOMETASHAPE_CLUSTER_HOST and AUTOMETASHAPE_CLUSTER_ROOT."""
        import tempfile
        from unittest.mock import patch

        import launch_worker as lw

        env_vars = {
            "AUTOMETASHAPE_CLUSTER_HOST": "192.168.1.100",
            "AUTOMETASHAPE_CLUSTER_ROOT": r"\\SERVER\ClusterStorage",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ms = os.path.join(tmpdir, "metashape.exe")
            with open(fake_ms, "w", encoding="utf-8") as fh:
                fh.write("stub")
            cfg_file = os.path.join(tmpdir, "empty_worker_config.json")
            with (
                patch.dict(os.environ, env_vars),
                patch("cluster_utils.find_metashape_path", return_value=fake_ms),
                patch("cluster_utils.probe_metashape_gpu_mask", return_value=(None, [])),
                patch("cluster_utils.test_tcp_connection", return_value=True),
                patch("cluster_utils.test_unc_path", return_value=(True, "")),
                patch("subprocess.Popen") as mock_popen,
            ):
                mock_proc = mock_popen.return_value
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_proc.poll.return_value = 0

                with patch(
                    "sys.argv",
                    ["launch_worker.py", "--headless", "--no-reconnect", "--config", cfg_file],
                ):
                    ret = lw._run_worker_lifecycle()
                    self.assertEqual(ret, 0)
                    cmd = mock_popen.call_args[0][0]
                    self.assertIn("192.168.1.100:5840", cmd)
                    self.assertIn(r"\\SERVER\ClusterStorage", cmd)
                    self.assertNotIn("--gpu-mask", cmd)

    def test_worker_launcher_auto_persists_config_without_hardcoded_gpu(self) -> None:
        """Verify launch_worker auto-persists valid host/root without injecting hardcoded gpu_mask."""
        import json
        import tempfile
        from unittest.mock import patch

        import launch_worker as lw

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ms = os.path.join(tmpdir, "metashape.exe")
            with open(fake_ms, "w", encoding="utf-8") as fh:
                fh.write("stub")
            cfg_file = os.path.join(tmpdir, "worker_config.json")
            with (
                patch("cluster_utils.find_metashape_path", return_value=fake_ms),
                patch("cluster_utils.probe_metashape_gpu_mask", return_value=(None, [])),
                patch("cluster_utils.test_tcp_connection", return_value=True),
                patch("cluster_utils.test_unc_path", return_value=(True, "")),
                patch("subprocess.Popen") as mock_popen,
            ):
                mock_proc = mock_popen.return_value
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_proc.poll.return_value = 0

                with patch(
                    "sys.argv",
                    [
                        "launch_worker.py",
                        "--headless",
                        "--no-reconnect",
                        "--host",
                        "192.168.1.100",
                        "--root",
                        r"\\SERVER\ClusterStorage",
                        "--config",
                        cfg_file,
                    ],
                ):
                    ret = lw._run_worker_lifecycle()
                    self.assertEqual(ret, 0)

            self.assertTrue(os.path.isfile(cfg_file))
            with open(cfg_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["host"], "192.168.1.100")
            self.assertEqual(saved["root"], r"\\SERVER\ClusterStorage")
            self.assertNotIn("gpu_mask", saved)

    def test_worker_launcher_purges_legacy_hardcoded_gpu_from_config(self) -> None:
        """Verify launch_worker purges legacy hardcoded gpu_mask from pre-existing worker_config.json."""
        import json
        import tempfile
        from unittest.mock import patch

        import launch_worker as lw

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ms = os.path.join(tmpdir, "metashape.exe")
            with open(fake_ms, "w", encoding="utf-8") as fh:
                fh.write("stub")
            cfg_file = os.path.join(tmpdir, "worker_config.json")
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "host": "192.168.1.100",
                        "port": "5840",
                        "root": r"\\SERVER\ClusterStorage",
                        "cpu_enable": 1,
                        "gpu_mask": "1",
                    },
                    f,
                )

            with (
                patch("cluster_utils.find_metashape_path", return_value=fake_ms),
                patch("cluster_utils.probe_metashape_gpu_mask", return_value=(None, [])),
                patch("cluster_utils.test_tcp_connection", return_value=True),
                patch("cluster_utils.test_unc_path", return_value=(True, "")),
                patch("subprocess.Popen") as mock_popen,
            ):
                mock_proc = mock_popen.return_value
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_proc.poll.return_value = 0

                with patch(
                    "sys.argv",
                    [
                        "launch_worker.py",
                        "--headless",
                        "--no-reconnect",
                        "--config",
                        cfg_file,
                    ],
                ):
                    ret = lw._run_worker_lifecycle()
                    self.assertEqual(ret, 0)

            with open(cfg_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertNotIn("gpu_mask", saved)

    def test_worker_launcher_preserves_custom_gpu_mask_in_config(self) -> None:
        """Verify launch_worker preserves and respects custom non-legacy gpu_mask from worker_config.json."""
        import json
        import tempfile
        from unittest.mock import patch

        import launch_worker as lw

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ms = os.path.join(tmpdir, "metashape.exe")
            with open(fake_ms, "w", encoding="utf-8") as fh:
                fh.write("stub")
            cfg_file = os.path.join(tmpdir, "worker_config.json")
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "host": "192.168.1.100",
                        "port": "5840",
                        "root": r"\\SERVER\ClusterStorage",
                        "cpu_enable": 1,
                        "gpu_mask": "2",
                    },
                    f,
                )

            with (
                patch("cluster_utils.find_metashape_path", return_value=fake_ms),
                patch("cluster_utils.probe_metashape_gpu_mask", return_value=(None, [])),
                patch("cluster_utils.test_tcp_connection", return_value=True),
                patch("cluster_utils.test_unc_path", return_value=(True, "")),
                patch("subprocess.Popen") as mock_popen,
            ):
                mock_proc = mock_popen.return_value
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_proc.poll.return_value = 0

                with patch(
                    "sys.argv",
                    [
                        "launch_worker.py",
                        "--headless",
                        "--no-reconnect",
                        "--config",
                        cfg_file,
                    ],
                ):
                    ret = lw._run_worker_lifecycle()
                    self.assertEqual(ret, 0)
                    cmd = mock_popen.call_args[0][0]
                    self.assertIn("--gpu-mask", cmd)
                    idx = cmd.index("--gpu-mask")
                    self.assertEqual(cmd[idx + 1], "2")

            with open(cfg_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved.get("gpu_mask"), "2")

    def test_worker_launcher_explicit_gpu_mask_passed_to_metashape(self) -> None:
        """Verify launch_worker passes --gpu-mask when explicitly requested via CLI."""
        import tempfile
        from unittest.mock import patch

        import launch_worker as lw

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_ms = os.path.join(tmpdir, "metashape.exe")
            with open(fake_ms, "w", encoding="utf-8") as fh:
                fh.write("stub")
            cfg_file = os.path.join(tmpdir, "worker_config.json")
            with (
                patch("cluster_utils.find_metashape_path", return_value=fake_ms),
                patch("cluster_utils.probe_metashape_gpu_mask", return_value=(None, [])),
                patch("cluster_utils.test_tcp_connection", return_value=True),
                patch("cluster_utils.test_unc_path", return_value=(True, "")),
                patch("subprocess.Popen") as mock_popen,
            ):
                mock_proc = mock_popen.return_value
                mock_proc.wait.return_value = 0
                mock_proc.returncode = 0
                mock_proc.poll.return_value = 0

                with patch(
                    "sys.argv",
                    [
                        "launch_worker.py",
                        "--headless",
                        "--no-reconnect",
                        "--host",
                        "192.168.1.100",
                        "--root",
                        r"\\SERVER\ClusterStorage",
                        "--gpu-mask",
                        "2",
                        "--config",
                        cfg_file,
                    ],
                ):
                    ret = lw._run_worker_lifecycle()
                    self.assertEqual(ret, 0)
                    cmd = mock_popen.call_args[0][0]
                    self.assertIn("--gpu-mask", cmd)
                    idx = cmd.index("--gpu-mask")
                    self.assertEqual(cmd[idx + 1], "2")

    def test_local_scratch_staging_cli_defaults_and_opt_out(self) -> None:
        """Verify local_scratch_staging is enabled by default and supports explicit opt-out."""
        from metashape_reconstruction import build_arg_parser

        parser = build_arg_parser()

        # Invariant 1: Default is True
        args_default = parser.parse_args(["--images", "dummy_images"])
        self.assertTrue(args_default.local_scratch_staging)

        # Invariant 2: Explicit opt-out flags set it to False
        args_opt_out1 = parser.parse_args(["--images", "dummy_images", "--no-local-scratch-staging"])
        self.assertFalse(args_opt_out1.local_scratch_staging)

        args_opt_out2 = parser.parse_args(["--images", "dummy_images", "--no-stage-local"])
        self.assertFalse(args_opt_out2.local_scratch_staging)

        # Invariant 3: Explicit opt-in flags preserve True
        args_opt_in = parser.parse_args(["--images", "dummy_images", "--stage-local"])
        self.assertTrue(args_opt_in.local_scratch_staging)

    def test_supervisor_accepts_network_path_arg(self) -> None:
        """Verify cluster_supervisor parser accepts --network-path and main forwards it."""
        from autometashape.cluster.supervisor import build_arg_parser

        parser = build_arg_parser()
        args = parser.parse_args(
            [
                "--manifest",
                "dummy_manifest.json",
                "--network-enable",
                "--network-path",
                r"\\ctuav\catp",
            ]
        )
        self.assertEqual(args.network_path, r"\\ctuav\catp")
        self.assertTrue(args.network_enable)

    def test_supervisor_concurrency_cli_flags(self) -> None:
        """Verify cluster_supervisor parser supports --concurrent, --parallel-dispatch, and --sequential."""
        from autometashape.cluster.supervisor import build_arg_parser

        parser = build_arg_parser()

        # Default: None
        args_default = parser.parse_args(["--manifest", "dummy.json"])
        self.assertIsNone(args_default.concurrent)

        # Explicit concurrent opt-in
        args_concurrent = parser.parse_args(["--manifest", "dummy.json", "--concurrent"])
        self.assertTrue(args_concurrent.concurrent)

        # Alias: parallel-dispatch
        args_parallel = parser.parse_args(["--manifest", "dummy.json", "--parallel-dispatch"])
        self.assertTrue(args_parallel.concurrent)

        # Explicit sequential opt-out
        args_no_concurrent = parser.parse_args(["--manifest", "dummy.json", "--no-concurrent"])
        self.assertFalse(args_no_concurrent.concurrent)

        # Alias: sequential
        args_sequential = parser.parse_args(["--manifest", "dummy.json", "--sequential"])
        self.assertFalse(args_sequential.concurrent)

    def test_extract_unc_root_various_formats(self) -> None:
        """Verify extract_unc_root correctly resolves UNC roots and ignores local paths."""
        from autometashape.cluster.hardware import extract_unc_root

        self.assertEqual(extract_unc_root(r"\\ctuav\catp\101126\CATP_0205"), r"\\ctuav\catp")
        self.assertEqual(extract_unc_root("//ctuav/catp/101126/CATP_0205"), r"\\ctuav\catp")
        self.assertEqual(extract_unc_root(r"\\ctuav\catp"), r"\\ctuav\catp")
        self.assertEqual(extract_unc_root("C:\\images\\CATP_0205"), "")
        self.assertEqual(extract_unc_root("/var/data/images"), "")
        self.assertEqual(extract_unc_root(""), "")

    def test_recon_parser_default_keypoint_limit(self) -> None:
        """Defend default keypoint-limit is 50,000 to prevent RAM overflow on large datasets."""
        parser = build_recon_parser()
        args_default = parser.parse_args(["--images", "dummy_images"])
        self.assertEqual(args_default.keypoint_limit, 50000)

        args_custom = parser.parse_args(["--images", "dummy_images", "--keypoint-limit", "80000"])
        self.assertEqual(args_custom.keypoint_limit, 80000)


if __name__ == "__main__":
    unittest.main()
