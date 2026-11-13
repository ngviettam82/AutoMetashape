# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure scripts directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import cluster_utils as cu


class TestClusterUtils(unittest.TestCase):
    """Test suite for cluster_utils module functions and diagnostics."""

    def test_parse_ipc_line_sentinel(self) -> None:
        """Verify parse_ipc_line parses valid sentinel lines."""
        line = "@@AUTOMETASHAPE_STAGE:depth:MVS Dense Stereopsis@@"
        parsed = cu.parse_ipc_line(line)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed, ("depth", "MVS Dense Stereopsis"))

    def test_parse_ipc_line_anchored_tags(self) -> None:
        """Verify parse_ipc_line recognizes anchored [X/Y] stage tags."""
        line = "[2/7] Matching photos..."
        parsed = cu.parse_ipc_line(line, mode="recon")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], "match")

        # Export mode
        export_line = "[1/2] Exporting Binary glTF (.glb)..."
        parsed_exp = cu.parse_ipc_line(export_line, mode="export")
        self.assertIsNotNone(parsed_exp)
        self.assertEqual(parsed_exp[0], "models")

    def test_parse_ipc_line_ignores_non_milestone(self) -> None:
        """Verify parse_ipc_line returns None for non-milestone lines."""
        self.assertIsNone(cu.parse_ipc_line("Elevation: ENABLED | Orthomosaic: ENABLED"))
        self.assertIsNone(cu.parse_ipc_line(""))
        self.assertIsNone(cu.parse_ipc_line("Just some normal output"))

    def test_format_file_size(self) -> None:
        """Verify format_file_size formats bytes to human-readable strings."""
        self.assertEqual(cu.format_file_size(500), "500 B")
        self.assertEqual(cu.format_file_size(2048), "2.0 KB")
        self.assertEqual(cu.format_file_size(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(cu.format_file_size(2 * 1024 * 1024 * 1024), "2.00 GB")

    def test_get_primary_lan_ip(self) -> None:
        """Verify get_primary_lan_ip returns a non-empty string."""
        ip = cu.get_primary_lan_ip()
        self.assertIsInstance(ip, str)
        self.assertTrue(len(ip) >= 7)

    def test_normalize_unicode_path(self) -> None:
        """Verify normalize_unicode_path handles Vietnamese diacritics."""
        p = cu.normalize_unicode_path("C:/Surveys/Khảo sát Đà Nẵng/ảnh/")
        self.assertTrue("Đà Nẵng" in p)

    def test_config_load_and_save(self) -> None:
        """Verify saving and loading JSON configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            conf_path = os.path.join(tmpdir, "test_worker_config.json")
            data = {"host": "192.168.1.100", "port": "5840", "root": r"\\SERVER\SharedMetashape"}
            saved_path = cu.save_cluster_config(data, conf_path)
            self.assertEqual(saved_path, conf_path)
            self.assertTrue(os.path.isfile(conf_path))

            loaded = cu.load_cluster_config(conf_path)
            self.assertEqual(loaded, data)

    def test_unc_path_validation(self) -> None:
        """Verify test_unc_path accurately verifies directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            valid, err = cu.test_unc_path(tmpdir)
            self.assertTrue(valid)
            self.assertEqual(err, "")

            invalid, err = cu.test_unc_path(os.path.join(tmpdir, "non_existent_folder_xyz"))
            self.assertFalse(invalid)
            self.assertIn("does not exist", err)

            empty_valid, empty_err = cu.test_unc_path("")
            self.assertFalse(empty_valid)
            self.assertIn("cannot be empty", empty_err)

    @patch("socket.socket")
    def test_tcp_connection_mock(self, mock_socket_cls: MagicMock) -> None:
        """Verify test_tcp_connection handles success and failure."""
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock

        # Success case
        self.assertTrue(cu.test_tcp_connection("127.0.0.1", 5840))

        # Failure case
        mock_sock.connect.side_effect = OSError("Connection refused")
        self.assertFalse(cu.test_tcp_connection("127.0.0.1", 5840))

    def test_generate_worker_bat_dynamic_paths(self) -> None:
        """Verify generate_worker_bat_content uses dynamic environment variables and drive scans."""
        bat = cu.generate_worker_bat_content(host="192.168.1.100", port="5840", root_path=r"\\SERVER\SharedMetashape")
        self.assertIn("%ProgramFiles%\\Agisoft\\Metashape Pro\\metashape.exe", bat)
        self.assertIn("%ProgramW6432%\\Agisoft\\Metashape Pro\\metashape.exe", bat)
        self.assertIn("for %%D in (C D E F G)", bat)
        self.assertIn("192.168.1.100:5840", bat)
        self.assertIn(r"\\SERVER\SharedMetashape", bat)

    @patch("socket.gethostbyname")
    @patch("socket.gethostname")
    @patch("socket.socket")
    def test_get_primary_lan_ip_airgapped_fallback(
        self,
        mock_socket_cls: MagicMock,
        mock_gethostname: MagicMock,
        mock_gethostbyname: MagicMock,
    ) -> None:
        """Verify get_primary_lan_ip recovers local IP on airgapped offline cluster networks."""
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock
        mock_sock.connect.side_effect = OSError("Network is unreachable")
        mock_gethostname.return_value = "cluster-node-1"
        mock_gethostbyname.return_value = "10.2.8.43"

        ip = cu.get_primary_lan_ip()
        self.assertEqual(ip, "10.2.8.43")

    @patch("socket.gethostbyname")
    @patch("socket.gethostname")
    @patch("socket.socket")
    def test_get_primary_lan_ip_offline_loopback(
        self,
        mock_socket_cls: MagicMock,
        mock_gethostname: MagicMock,
        mock_gethostbyname: MagicMock,
    ) -> None:
        """Verify get_primary_lan_ip falls back to loopback when no IP is resolvable."""
        mock_sock = MagicMock()
        mock_socket_cls.return_value = mock_sock
        mock_sock.connect.side_effect = OSError("Network unreachable")
        mock_gethostname.return_value = "offline-node"
        mock_gethostbyname.return_value = "127.0.0.1"

        ip = cu.get_primary_lan_ip()
        self.assertEqual(ip, "127.0.0.1")

    def test_is_integrated_gpu(self) -> None:
        """Verify is_integrated_gpu correctly identifies integrated graphics vs discrete GPUs."""
        # Integrated GPUs should be detected
        self.assertTrue(cu.is_integrated_gpu("Intel(R) UHD Graphics 770"))
        self.assertTrue(cu.is_integrated_gpu("Intel(R) HD Graphics 630"))
        self.assertTrue(cu.is_integrated_gpu("Intel(R) Iris(R) Xe Graphics"))
        self.assertTrue(cu.is_integrated_gpu("AMD Radeon(TM) Graphics"))
        self.assertTrue(cu.is_integrated_gpu("AMD Radeon Graphics"))

        # Discrete GPUs should NOT be flagged as integrated
        self.assertFalse(cu.is_integrated_gpu("NVIDIA GeForce RTX 5080"))
        self.assertFalse(cu.is_integrated_gpu("NVIDIA GeForce RTX 4090"))
        self.assertFalse(cu.is_integrated_gpu("AMD Radeon RX 7900 XTX"))
        self.assertFalse(cu.is_integrated_gpu("Intel(R) Arc(TM) A770 Graphics"))

    def test_calculate_optimal_gpu_mask(self) -> None:
        """Verify dynamic GPU detection defers to Metashape native discovery (None) rather than synthetic masks."""
        # 0 GPUs (CPU only)
        mask0, names0 = cu.calculate_optimal_gpu_mask([])
        self.assertEqual(mask0, 0)
        self.assertEqual(names0, [])

        # 1 Discrete GPU -> None (defers to Metashape Pro native engine benchmark)
        gpus1 = [{"index": 0, "name": "RTX 4090", "mem_mb": 24576}]
        mask1, names1 = cu.calculate_optimal_gpu_mask(gpus1)
        self.assertIsNone(mask1)
        self.assertEqual(len(names1), 1)
        self.assertIn("RTX 4090", names1[0])

        # 2 GPUs (Dual GPU worker) -> None (defers to native multi-GPU discovery)
        gpus2 = [
            {"index": 0, "name": "RTX 4090", "mem_mb": 24576},
            {"index": 1, "name": "RTX 4090", "mem_mb": 24576},
        ]
        mask2, names2 = cu.calculate_optimal_gpu_mask(gpus2)
        self.assertIsNone(mask2)
        self.assertEqual(len(names2), 2)

        # 4 GPUs (Quad GPU cluster workstation) -> None (defers to native discovery)
        gpus4 = [{"index": i, "name": "RTX A6000", "mem_mb": 49152} for i in range(4)]
        mask4, names4 = cu.calculate_optimal_gpu_mask(gpus4)
        self.assertIsNone(mask4)
        self.assertEqual(len(names4), 4)

    def test_generate_worker_bat_with_gpu_mask(self) -> None:
        """Verify generate_worker_bat_content omits --gpu-mask by default and handles explicit overrides."""
        # Default behavior: no gpu_mask specified -> omitted from batch file
        bat_default = cu.generate_worker_bat_content(
            host="192.168.1.100",
            port="5840",
            root_path=r"\\SERVER\SharedMetashape",
        )
        self.assertNotIn("--gpu-mask", bat_default)

        # Multi-GPU manual override mask
        bat_dual = cu.generate_worker_bat_content(
            host="192.168.1.100",
            port="5840",
            root_path=r"\\SERVER\SharedMetashape",
            cpu_enable=0,
            gpu_mask="3",
        )
        self.assertIn("--cpu-enable 0", bat_dual)
        self.assertIn("--gpu-mask 3", bat_dual)

        # CPU only mode (gpu_mask = 0) -> omitted from batch file
        bat_cpu = cu.generate_worker_bat_content(
            host="192.168.1.100",
            port="5840",
            root_path=r"\\SERVER\SharedMetashape",
            cpu_enable=1,
            gpu_mask="0",
        )
        self.assertIn("--cpu-enable 1", bat_cpu)
        self.assertNotIn("--gpu-mask", bat_cpu)

        # Auto mode -> omitted from batch file
        bat_auto = cu.generate_worker_bat_content(
            host="192.168.1.100",
            port="5840",
            root_path=r"\\SERVER\SharedMetashape",
            gpu_mask="Auto",
        )
        self.assertNotIn("--gpu-mask", bat_auto)

if __name__ == "__main__":
    unittest.main()
