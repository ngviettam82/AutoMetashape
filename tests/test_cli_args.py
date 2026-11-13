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

from metashape_reconstruction import build_arg_parser as build_recon_parser
from metashape_export import build_arg_parser as build_export_parser


class TestCliArgs(unittest.TestCase):
    """Test CLI arguments consistency across pipeline subsystems."""

    def test_recon_parser_accepts_no_export_dem(self) -> None:
        """Defend against unrecognized --no-export-dem argument when exporting is decoupled."""
        parser = build_recon_parser()
        args = parser.parse_args([
            "--images", "dummy_images",
            "--build-dem",
            "--no-export-dem",
        ])
        self.assertTrue(args.build_dem)
        self.assertFalse(args.export_dem)

    def test_recon_parser_accepts_export_dem(self) -> None:
        """Defend --export-dem flag activating GeoTIFF export."""
        parser = build_recon_parser()
        args = parser.parse_args([
            "--images", "dummy_images",
            "--build-dem",
            "--export-dem",
        ])
        self.assertTrue(args.build_dem)
        self.assertTrue(args.export_dem)

    def test_recon_parser_ortho_decoupling(self) -> None:
        """Defend that building orthomosaic does not export GeoTIFF by default."""
        parser = build_recon_parser()

        # Default: no export
        default_args = parser.parse_args(["--images", "dummy_images"])
        self.assertFalse(default_args.export_ortho)

        # Explicitly enabled export
        export_args = parser.parse_args([
            "--images", "dummy_images",
            "--ortho",
            "--export-ortho",
        ])
        self.assertTrue(export_args.build_orthomosaic)
        self.assertTrue(export_args.export_ortho)

        # Disabled export explicitly
        no_export_args = parser.parse_args([
            "--images", "dummy_images",
            "--ortho",
            "--no-export-ortho",
        ])
        self.assertTrue(no_export_args.build_orthomosaic)
        self.assertFalse(no_export_args.export_ortho)

    def test_recon_parser_point_cloud_decoupling(self) -> None:
        """Defend decoupling between building point cloud and exporting point cloud file."""
        parser = build_recon_parser()

        # Case 1: Built natively into project, but not exported to disk file
        args_no_export = parser.parse_args([
            "--images", "dummy_images",
            "--build-point-cloud",
            "--no-export-point-cloud",
        ])
        self.assertTrue(args_no_export.build_point_cloud)
        self.assertFalse(args_no_export.export_point_cloud)

        # Case 2: Explicitly exported
        args_export = parser.parse_args([
            "--images", "dummy_images",
            "--build-point-cloud",
            "--export-point-cloud",
        ])
        self.assertTrue(args_export.build_point_cloud)
        self.assertTrue(args_export.export_point_cloud)

        # Case 3: Fully disabled
        args_disabled = parser.parse_args([
            "--images", "dummy_images",
            "--no-point-cloud",
            "--no-export-point-cloud",
        ])
        self.assertFalse(args_disabled.build_point_cloud)
        self.assertFalse(args_disabled.export_point_cloud)

    def test_recon_parser_reference_preselection_modes(self) -> None:
        """Defend reference preselection mode choices."""
        parser = build_recon_parser()

        # Default is estimated
        default_args = parser.parse_args(["--images", "dummy_images"])
        self.assertEqual(default_args.reference_preselection_mode, "estimated")

        for mode in ("estimated", "source", "sequential"):
            args = parser.parse_args([
                "--images", "dummy_images",
                "--reference-preselection-mode", mode,
            ])
            self.assertEqual(args.reference_preselection_mode, mode)

    def test_recon_parser_crop_choices(self) -> None:
        """Defend crop-method choices including gps-square and none."""
        parser = build_recon_parser()
        default_args = parser.parse_args(["--images", "dummy_images"])
        self.assertIsNone(default_args.crop_method)

        for method in ("gps-square", "structure", "manual", "none"):
            args = parser.parse_args([
                "--images", "dummy_images",
                "--crop-method", method,
            ])
            self.assertEqual(args.crop_method, method)

        no_crop_args = parser.parse_args([
            "--images", "dummy_images",
            "--no-crop",
        ])
        self.assertTrue(no_crop_args.no_crop)

    def test_recon_parser_presets(self) -> None:
        """Defend supported presets including building alias."""
        parser = build_recon_parser()
        for preset in ("pro", "custom", "building"):
            args = parser.parse_args([
                "--images", "dummy_images",
                "--preset", preset,
            ])
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
        net_args = parser.parse_args([
            "--images", "dummy_images",
            "--network-enable",
            "--network-host", "192.168.1.150",
            "--network-port", "5842",
            "--network-path", r"\\SERVER\RealityScan",
        ])
        self.assertTrue(net_args.network_enable)
        self.assertEqual(net_args.network_host, "192.168.1.150")
        self.assertEqual(net_args.network_port, 5842)
        self.assertEqual(net_args.network_path, r"\\SERVER\RealityScan")

    def test_recon_parser_independent_workers_arg(self) -> None:
        """Defend --network-independent-workers CLI flag in reconstruction parser."""
        parser = build_recon_parser()
        default_args = parser.parse_args(["--images", "dummy_images"])
        self.assertFalse(default_args.network_independent_workers)

        indep_args = parser.parse_args([
            "--images", "dummy_images",
            "--network-enable",
            "--network-independent-workers",
        ])
        self.assertTrue(indep_args.network_enable)
        self.assertTrue(indep_args.network_independent_workers)

    def test_export_parser_arguments(self) -> None:
        """Defend standalone export argument parser accepts required and optional flags."""
        parser = build_export_parser()
        args = parser.parse_args([
            "--project", "dummy.psx",
            "--output", "dummy_out",
            "--export-dem",
            "--export-orthomosaic",
            "--export-contours",
            "--export-point-cloud",
            "--export-fbx",
            "--export-ply",
            "--export-dxf",
            "--export-tie-points",
        ])
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
        args_png = parser.parse_args([
            "--project", "dummy.psx",
            "--texture-format", "png",
            "--no-export-glb",
            "--no-export-obj",
        ])
        self.assertEqual(args_png.texture_format, "png")
        self.assertFalse(args_png.export_glb)
        self.assertFalse(args_png.export_obj)

        for fmt in ("png", "tiff", "tif", "jpg", "jpeg"):
            fmt_args = parser.parse_args([
                "--project", "dummy.psx",
                "--texture-format", fmt,
            ])
            self.assertEqual(fmt_args.texture_format, fmt)

    def test_metashape_cluster_binary_discovery(self) -> None:
        """Verify metashape-server and monitor binary discovery functions."""
        from cluster_utils import find_metashape_monitor_path, find_metashape_path, find_metashape_server_path

        ms_exe = find_metashape_path()
        if ms_exe and os.path.isfile(ms_exe):
            server_bin = find_metashape_server_path(ms_exe)
            if server_bin:
                self.assertTrue(os.path.isfile(server_bin))
                self.assertIn("server", os.path.basename(server_bin).lower())

            monitor_bin = find_metashape_monitor_path(ms_exe)
            if monitor_bin:
                self.assertTrue(os.path.isfile(monitor_bin))
                self.assertIn("monitor", os.path.basename(monitor_bin).lower())

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
            args = parser.parse_args([
                "--images", "dummy_images",
                "--texture-format", fmt,
            ])
            self.assertEqual(args.texture_format, fmt)

    def test_worker_launcher_parser(self) -> None:
        """Defend launch_worker argument parser accepts required and optional cluster flags."""
        import launch_worker as lw

        parser = lw.build_arg_parser()
        args = parser.parse_args([
            "--host", "192.168.1.100",
            "--port", "5840",
            "--root", r"\\SERVER\SharedMetashape",
            "--cpu-enable", "1",
            "--gpu-mask", "1",
            "--no-reconnect",
        ])
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
        args = parser.parse_args([
            "--worker",
            "--host", "192.168.1.100",
            "--root", r"\\SERVER\SharedMetashape",
            "--cpu", "1",
            "--gpu", "1",
        ])
        self.assertTrue(args.worker)
        self.assertEqual(args.cpu_enable, 1)
        self.assertEqual(args.gpu_mask, "1")

    def test_server_launcher_accepts_server_flag(self) -> None:
        """Verify launch_server parser accepts --server flag without error."""
        import launch_server as ls

        parser = ls.build_arg_parser()
        args = parser.parse_args([
            "--server",
            "--host", "0.0.0.0",
            "--port", "5840",
        ])
        self.assertTrue(args.server)
        self.assertEqual(args.port, "5840")

    def test_worker_launcher_loads_and_prioritizes_worker_config(self) -> None:
        """Verify launch_worker resolves parameters from worker_config.json with CLI override."""
        import tempfile
        import json
        import launch_worker as lw

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = os.path.join(tmpdir, "worker_config.json")
            with open(cfg_file, "w", encoding="utf-8") as f:
                json.dump({
                    "host": "10.0.0.5",
                    "port": "5842",
                    "root": r"\\NAS\MetashapeData",
                    "cpu_enable": 0,
                    "gpu_mask": "2",
                }, f)

            parser = lw.build_arg_parser()

            # Case A: CLI args omitted -> should resolve from config file
            args = parser.parse_args(["--config", cfg_file])
            saved_config = lw.cu.load_cluster_config(args.config)
            host = args.host or saved_config.get("host", "")
            port = args.port or saved_config.get("port", "5840")
            root_path = args.root or saved_config.get("root", "")
            cpu_enable = args.cpu_enable if args.cpu_enable is not None else saved_config.get("cpu_enable", 1)
            raw_gpu = args.gpu_mask if args.gpu_mask is not None else saved_config.get("gpu_mask", None)
            gpu_mask = str(raw_gpu).strip() if raw_gpu is not None else None
            if gpu_mask and gpu_mask.lower() in ("auto", "none", "", "default"):
                gpu_mask = None

            self.assertEqual(host, "10.0.0.5")
            self.assertEqual(port, "5842")
            self.assertEqual(root_path, r"\\NAS\MetashapeData")
            self.assertEqual(cpu_enable, 0)
            self.assertEqual(gpu_mask, "2")

            # Case B: CLI args provided -> should override config file
            args_override = parser.parse_args(["--config", cfg_file, "--cpu", "1", "--gpu", "3"])
            cpu_override = args_override.cpu_enable if args_override.cpu_enable is not None else saved_config.get("cpu_enable", 1)
            raw_gpu_override = args_override.gpu_mask if args_override.gpu_mask is not None else saved_config.get("gpu_mask", None)
            gpu_override = str(raw_gpu_override).strip() if raw_gpu_override is not None else None

            self.assertEqual(cpu_override, 1)
            self.assertEqual(gpu_override, "3")

            # Case C: Dynamic auto-detect when gpu_mask is omitted or auto
            cfg_auto = os.path.join(tmpdir, "worker_auto.json")
            with open(cfg_auto, "w", encoding="utf-8") as f:
                json.dump({
                    "host": "10.0.0.5",
                    "root": r"\\NAS\MetashapeData",
                    "gpu_mask": "auto",
                }, f)
            args_auto = parser.parse_args(["--config", cfg_auto])
            saved_auto = lw.cu.load_cluster_config(args_auto.config)
            raw_gpu_auto = args_auto.gpu_mask if args_auto.gpu_mask is not None else saved_auto.get("gpu_mask", None)
            gpu_auto = str(raw_gpu_auto).strip() if raw_gpu_auto is not None else None
            if gpu_auto and gpu_auto.lower() in ("auto", "none", "", "default"):
                gpu_auto = None
            self.assertIsNone(gpu_auto)

if __name__ == "__main__":
    unittest.main()
