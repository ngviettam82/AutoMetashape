# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests defending Vietnamese Unicode character support across paths, filenames, and IPC streams."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure scripts directory is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import metashape_export as me
import metashape_reconstruction as mr
from cluster_utils import (
    generate_worker_bat_content,
    parse_ipc_line,
)


class TestVietnameseUnicodePaths(unittest.TestCase):
    """Test suite for Vietnamese Unicode characters (đ, ê, ơ, ư, à, á, ả, ã, ạ, ô, ắ, ậ, etc.) in paths and files."""

    def test_infer_project_name_vietnamese_paths(self) -> None:
        """Verify infer_project_name extracts clean Vietnamese project names from paths."""
        # Standard folder with Vietnamese diacritics
        name1 = mr.infer_project_name(images_dir="C:/Surveys/Khảo sát Đà Nẵng")
        self.assertEqual(name1, "Khảo sát Đà Nẵng")

        # Container subfolders in Vietnamese (ảnh, ảnh chụp, dữ liệu)
        name2 = mr.infer_project_name(images_dir="C:/Surveys/Dự án Cầu Rồng/ảnh")
        self.assertEqual(name2, "Dự án Cầu Rồng")

        name3 = mr.infer_project_name(images_dir="C:/Surveys/Dự án Cầu Rồng/ảnh chụp")
        self.assertEqual(name3, "Dự án Cầu Rồng")

        name4 = mr.infer_project_name(images_dir="C:/Surveys/Dự án Cầu Rồng/dữ liệu")
        self.assertEqual(name4, "Dự án Cầu Rồng")

        # Project file path with Vietnamese characters
        name5 = mr.infer_project_name(project_file="C:/Surveys/Khảo sát Đà Nẵng/Khảo sát Đà Nẵng.psx")
        self.assertEqual(name5, "Khảo sát Đà Nẵng")

        # In export script as well
        name6 = me.infer_project_name(images_dir="C:/Surveys/Công trình Thủy điện Trị An/ảnh")
        self.assertEqual(name6, "Công trình Thủy điện Trị An")

    def test_get_psx_files_dir_vietnamese(self) -> None:
        """Verify get_psx_files_dir replaces only the extension without corrupting directories containing 'psx'."""
        path1 = r"C:\Khảo sát\Đà Nẵng\Dự án.psx"
        self.assertEqual(mr.get_psx_files_dir(path1), r"C:\Khảo sát\Đà Nẵng\Dự án.files")

        # Path where directory name contains 'psx'
        path2 = r"C:\psx_surveys\Khảo sát Đà Nẵng\model.psx"
        self.assertEqual(mr.get_psx_files_dir(path2), r"C:\psx_surveys\Khảo sát Đà Nẵng\model.files")

        # Uppercase .PSX
        path3 = r"C:\Khảo sát\Dự án.PSX"
        self.assertEqual(mr.get_psx_files_dir(path3), r"C:\Khảo sát\Dự án.files")

    def test_discover_image_subfolder_groups_vietnamese(self) -> None:
        """Verify discover_image_subfolder_groups navigates directories and files with Vietnamese names."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            survey_dir = os.path.join(tmp_dir, "Khảo sát Địa hình Đà Nẵng")
            flight1 = os.path.join(survey_dir, "Chuyến bay 1 - Sáng")
            flight2 = os.path.join(survey_dir, "Chuyến bay 2 - Chiều")
            os.makedirs(flight1, exist_ok=True)
            os.makedirs(flight2, exist_ok=True)

            img1 = os.path.join(flight1, "Ảnh_chụp_001.JPG")
            img2 = os.path.join(flight2, "Ảnh_chụp_002.JPG")
            with open(img1, "wb") as f:
                f.write(b"dummy_image_1")
            with open(img2, "wb") as f:
                f.write(b"dummy_image_2")

            groups = mr.discover_image_subfolder_groups(survey_dir)
            self.assertIn("Chuyến bay 1 - Sáng", groups)
            self.assertIn("Chuyến bay 2 - Chiều", groups)
            self.assertEqual(len(groups["Chuyến bay 1 - Sáng"]), 1)
            self.assertEqual(len(groups["Chuyến bay 2 - Chiều"]), 1)
            self.assertEqual(groups["Chuyến bay 1 - Sáng"][0], img1)
            self.assertEqual(groups["Chuyến bay 2 - Chiều"][0], img2)

    def test_calc_rel_cluster_vietnamese_paths(self) -> None:
        """Verify cluster relative path calculation handles Vietnamese paths across drives and UNC shares."""

        def calc_rel(psx_out: str, network_path: str) -> str:
            norm_psx = os.path.abspath(psx_out).replace("\\\\", "/").replace("\\", "/")
            norm_net = network_path.strip().replace("\\\\", "/").replace("\\", "/").rstrip("/")
            try:
                abs_psx = os.path.abspath(psx_out)
                abs_net = os.path.abspath(network_path)
                if os.path.commonpath([abs_psx, abs_net]).lower() == abs_net.lower():
                    return os.path.relpath(abs_psx, abs_net).replace("\\", "/")
            except Exception:
                pass
            net_parts = [p for p in norm_net.split("/") if p]
            if net_parts:
                share_name = net_parts[-1].lower()
                psx_parts = [p for p in norm_psx.split("/") if p]
                psx_lower = [p.lower() for p in psx_parts]
                if share_name in psx_lower:
                    idx = psx_lower.index(share_name)
                    rel = "/".join(psx_parts[idx + 1 :])
                    if rel:
                        return rel
            return os.path.basename(psx_out)

        rel1 = calc_rel(r"C:\Khảo sát\Đà Nẵng\0152.psx", r"C:\Khảo sát")
        self.assertEqual(rel1, "Đà Nẵng/0152.psx")

        rel2 = calc_rel(r"\\HOST_PC\Khảo sát\Đà Nẵng\0152.psx", r"\\HOST_PC\Khảo sát")
        self.assertEqual(rel2, "Đà Nẵng/0152.psx")

    def test_parse_ipc_line_vietnamese_descriptions(self) -> None:
        """Verify parse_ipc_line preserves Vietnamese characters in milestone descriptions."""
        line = "@@AUTOMETASHAPE_STAGE:ingest:Đang tải 150 ảnh từ thư mục Khảo sát Đà Nẵng@@"
        parsed = parse_ipc_line(line)
        self.assertIsNotNone(parsed)
        key, desc = parsed
        self.assertEqual(key, "ingest")
        self.assertEqual(desc, "Đang tải 150 ảnh từ thư mục Khảo sát Đà Nẵng")

    def test_generate_worker_bat_content_chcp_65001_and_vietnamese_path(self) -> None:
        """Verify generate_worker_bat_content configures UTF-8 code page (chcp 65001) for Vietnamese root paths."""
        bat = generate_worker_bat_content("192.168.1.100", "5840", r"\\HOST\Dự án Khảo sát Đà Nẵng")
        self.assertIn("chcp 65001 > nul", bat)
        self.assertIn(r"\\HOST\Dự án Khảo sát Đà Nẵng", bat)

    def test_dashboard_telemetry_vietnamese_preservation(self) -> None:
        """Verify dynamic dashboard JSON telemetry preserves literal Vietnamese text with ensure_ascii=False."""
        profiled = [
            {
                "id": "proj-01",
                "name": "Khảo sát Cầu Rồng - Đà Nẵng",
                "photos": 250,
                "status": "Ready",
                "gsd": 1.25,
            }
        ]
        dumped = json.dumps(profiled, indent=2, ensure_ascii=False)
        self.assertIn("Khảo sát Cầu Rồng - Đà Nẵng", dumped)
        self.assertNotIn("\\u", dumped)

    def test_run_reconstruction_vietnamese_dataset_mock(self) -> None:
        """Verify full run_reconstruction workflow with Vietnamese paths and dataset names."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            survey_dir = os.path.join(tmp_dir, "Khảo sát Địa hình Đà Nẵng")
            out_dir = os.path.join(tmp_dir, "Kết quả xuất 3D")
            flight1 = os.path.join(survey_dir, "Chuyến bay 1")
            os.makedirs(flight1, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)

            with open(os.path.join(flight1, "Ảnh_0001.JPG"), "wb") as f:
                f.write(b"dummy")

            mock_meta = MagicMock()
            mock_meta.app.enumGPUDevices.return_value = [{"name": "RTX 4090", "mem_size": 24 * (1024**3)}]
            mock_meta.ReferencePreselectionMode.ReferencePreselectionEstimated = 1
            mock_meta.FilterMode.ModerateFiltering = 2
            mock_meta.DataSource.DepthMapsData = 1
            mock_meta.DataSource.ElevationData = 2
            mock_meta.DataSource.OrthomosaicData = 3
            mock_meta.DataSource.PointCloudData = 4
            mock_meta.SurfaceType.Arbitrary = 0
            mock_meta.Interpolation.EnabledInterpolation = 1
            mock_meta.FaceCount.CustomFaceCount = 1
            mock_meta.MappingMode.GenericMapping = 0
            mock_meta.BlendingMode.MosaicBlending = 1
            mock_meta.ModelFormat.ModelFormatOBJ = 1
            mock_meta.ModelFormat.ModelFormatGLTF = 2
            mock_meta.ImageFormat.ImageFormatPNG = 1
            mock_meta.RasterFormat.RasterFormatNone = 0
            mock_meta.ImageFormat.ImageFormatTIFF = 2
            mock_meta.PointCloudFormat.PointCloudFormatLAZ = 1

            mock_doc = MagicMock()
            mock_chunk = MagicMock()
            mock_chunk.cameras = [MagicMock(transform=True, enabled=True)]
            mock_doc.chunk = mock_chunk
            mock_doc.chunks = [mock_chunk]
            mock_meta.Document.return_value = mock_doc

            mock_net_client = MagicMock()
            mock_net_client.workerList.return_value = [{"worker_id": 1, "host": "192.168.1.100", "paused": False}]
            mock_net_client.createBatch.return_value = 999
            mock_net_client.batchInfo.return_value = {
                "state": "completed",
                "tasks": [{"name": "MatchPhotos", "state": "completed", "progress": 100.0}],
            }
            mock_meta.NetworkClient.return_value = mock_net_client

            with patch.object(mr, "Metashape", mock_meta):
                mr.run_reconstruction(
                    images_dir=survey_dir,
                    output_dir=out_dir,
                    network_enable=True,
                    network_independent_workers=True,
                    multi_chunk="disable",
                    export_glb=True,
                    export_obj=True,
                )

            self.assertTrue(mock_net_client.createBatch.called)
            call_args = mock_net_client.createBatch.call_args
            rel_psx = call_args[0][0]
            self.assertIn("Khảo sát Địa hình Đà Nẵng.psx", rel_psx)


if __name__ == "__main__":
    unittest.main()
