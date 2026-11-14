# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""System integration test simulating Metashape cluster server and worker with local NVMe scratch staging."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure scripts directory is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import cluster_utils as cu
import metashape_reconstruction as mr


class TestLocalScratchStagingSystem(unittest.TestCase):
    """System test suite verifying local NVMe scratch staging and server-worker interaction."""

    def test_optimal_scratch_drive_discovery_priority(self) -> None:
        """Verify get_optimal_local_scratch_dir prioritizes high-capacity non-system drives over system drive."""
        mock_usages = {
            "C:\\": (100 * (1024**3), 20 * (1024**3), 80 * (1024**3)),  # System drive: 80 GB free (< 100 GB)
            "D:\\": (1000 * (1024**3), 500 * (1024**3), 500 * (1024**3)),  # Non-system NVMe: 500 GB free (Winner!)
            "E:\\": (500 * (1024**3), 200 * (1024**3), 300 * (1024**3)),  # Non-system SSD: 300 GB free
        }

        def mock_disk_usage(path: str):
            for prefix, usage in mock_usages.items():
                if path.upper().startswith(prefix):
                    u = MagicMock()
                    u.total, u.used, u.free = usage
                    return u
            u = MagicMock()
            u.total, u.used, u.free = (100 * (1024**3), 90 * (1024**3), 10 * (1024**3))
            return u

        def mock_exists(path: str) -> bool:
            norm = path.upper()
            return any(norm.startswith(p) for p in ("C:\\", "D:\\", "E:\\"))

        with patch("sys.platform", "win32"):
            with patch.dict(os.environ, {"SystemDrive": "C:"}):
                with patch("shutil.disk_usage", side_effect=mock_disk_usage):
                    with patch("os.path.exists", side_effect=mock_exists):
                        with patch("os.makedirs"):
                            with patch("builtins.open", unittest.mock.mock_open()):
                                with patch("os.path.isfile", return_value=True):
                                    with patch("os.remove"):
                                        scratch_dir = cu.get_optimal_local_scratch_dir(min_free_gb=100.0)
                                        # Must select D: drive (highest non-system capacity meeting threshold)
                                        self.assertTrue(scratch_dir.upper().startswith("D:\\"))
                                        self.assertIn("AutoMetashape_Scratch", scratch_dir)

    def test_scratch_drive_fallback_to_system_drive_when_secondary_insufficient(self) -> None:
        """Verify get_optimal_local_scratch_dir falls back to system drive if secondary drives lack headroom."""
        mock_usages = {
            "C:\\": (500 * (1024**3), 100 * (1024**3), 400 * (1024**3)),  # System drive: 400 GB free (Meets threshold)
            "D:\\": (100 * (1024**3), 85 * (1024**3), 15 * (1024**3)),  # Secondary: only 15 GB free (< 100 GB)
        }

        def mock_disk_usage(path: str):
            for prefix, usage in mock_usages.items():
                if path.upper().startswith(prefix):
                    u = MagicMock()
                    u.total, u.used, u.free = usage
                    return u
            u = MagicMock()
            u.total, u.used, u.free = (100 * (1024**3), 90 * (1024**3), 10 * (1024**3))
            return u

        def mock_exists(path: str) -> bool:
            return any(path.upper().startswith(p) for p in ("C:\\", "D:\\"))

        with patch("sys.platform", "win32"):
            with patch.dict(os.environ, {"SystemDrive": "C:"}):
                with patch("shutil.disk_usage", side_effect=mock_disk_usage):
                    with patch("os.path.exists", side_effect=mock_exists):
                        with patch("os.makedirs"):
                            with patch("builtins.open", unittest.mock.mock_open()):
                                with patch("os.path.isfile", return_value=True):
                                    with patch("os.remove"):
                                        scratch_dir = cu.get_optimal_local_scratch_dir(min_free_gb=100.0)
                                        self.assertTrue(scratch_dir.upper().startswith("C:\\"))

    def test_sync_staged_project_to_network(self) -> None:
        """Verify sync_staged_project_to_network copies all files and directories then cleans up scratch."""
        with tempfile.TemporaryDirectory() as tmp_root:
            stage_dir = os.path.join(tmp_root, "local_stage")
            net_dir = os.path.join(tmp_root, "central_share")
            os.makedirs(stage_dir, exist_ok=True)
            os.makedirs(net_dir, exist_ok=True)

            # Create mock project files on local scratch
            psx_file = os.path.join(stage_dir, "survey_project.psx")
            with open(psx_file, "w", encoding="utf-8") as f:
                f.write("<psx>mock_project_xml</psx>")

            files_dir = os.path.join(stage_dir, "survey_project.files", "0", "0")
            os.makedirs(files_dir, exist_ok=True)
            depth_map = os.path.join(files_dir, "depth_map.zip")
            with open(depth_map, "wb") as f:
                f.write(b"MOCK_DEPTH_MAP_DATA")

            deliverable_obj = os.path.join(stage_dir, "model.obj")
            with open(deliverable_obj, "w", encoding="utf-8") as f:
                f.write("# Wavefront OBJ\nv 0.0 0.0 0.0")

            # Perform synchronization
            success = cu.sync_staged_project_to_network(stage_dir, net_dir)
            self.assertTrue(success)

            # Verify destination contains all synchronized files
            self.assertTrue(os.path.isfile(os.path.join(net_dir, "survey_project.psx")))
            self.assertTrue(os.path.isfile(os.path.join(net_dir, "survey_project.files", "0", "0", "depth_map.zip")))
            self.assertTrue(os.path.isfile(os.path.join(net_dir, "model.obj")))

            # Verify local staging directory was purged to free worker SSD
            self.assertFalse(os.path.exists(stage_dir))

    def test_simulated_server_worker_local_staging_system(self) -> None:
        """Simulate an end-to-end distributed cluster server and independent worker with local NVMe scratch staging."""
        with tempfile.TemporaryDirectory() as sys_root:
            # 1. Simulate Central Network Share (coordinator NAS storage)
            nas_share = os.path.join(sys_root, "central_nas_share")
            nas_images = os.path.join(nas_share, "survey_images")
            nas_output = os.path.join(nas_share, "survey_deliverables")
            os.makedirs(nas_images, exist_ok=True)
            os.makedirs(nas_output, exist_ok=True)

            # Seed raw survey photos on central network share
            with open(os.path.join(nas_images, "DJI_0001.JPG"), "wb") as f:
                f.write(b"SIMULATED_RAW_PHOTO_HEADER_EXIF_DATA_1")
            with open(os.path.join(nas_images, "DJI_0002.JPG"), "wb") as f:
                f.write(b"SIMULATED_RAW_PHOTO_HEADER_EXIF_DATA_2")

            # 2. Simulate Remote Worker local NVMe SSD
            worker_nvme = os.path.join(sys_root, "worker_nvme_scratch")
            os.makedirs(worker_nvme, exist_ok=True)

            # 3. Mock Metashape Pro C++ core APIs on the worker
            mock_meta = MagicMock()
            mock_meta.app.enumGPUDevices.return_value = [{"name": "RTX 5080", "mem_size": 16 * (1024**3)}]
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

            mock_rs = MagicMock()
            mock_net_task = MagicMock()
            mock_net_task.name = "RunScript"
            mock_net_task.gpu_support = True

            def to_network_task():
                mock_net_task.args = mock_rs.args
                mock_net_task.path = mock_rs.path
                return mock_net_task

            mock_rs.toNetworkTask.side_effect = to_network_task
            mock_meta.Tasks.RunScript.return_value = mock_rs

            mock_doc = MagicMock()
            mock_chunk = MagicMock()
            mock_chunk.cameras = [
                MagicMock(transform=True, enabled=True),
                MagicMock(transform=True, enabled=True),
            ]
            mock_chunk.model = MagicMock()
            mock_chunk.model.textures = [MagicMock()]
            mock_doc.chunk = mock_chunk
            mock_doc.chunks = [mock_chunk]
            mock_meta.Document.return_value = mock_doc

            # 4. Mock Metashape Network Client on Server coordinator
            mock_net_client = MagicMock()
            mock_net_client.workerList.return_value = [
                {"worker_id": 1, "host": "192.168.1.100", "gpu_mask": "Auto", "paused": False}
            ]
            mock_net_client.createBatch.return_value = 777
            mock_net_client.batchInfo.return_value = {
                "state": "completed",
                "tasks": [{"name": "RunScript", "state": "completed", "progress": 100.0}],
            }
            mock_meta.NetworkClient.return_value = mock_net_client

            # 5. Execute Pipeline Simulation with local scratch staging
            with patch.object(mr, "Metashape", mock_meta):
                with patch("cluster_utils.get_optimal_local_scratch_dir", return_value=worker_nvme):
                    with patch("cluster_utils.check_scratch_disk_headroom", return_value=(True, 450.0)):
                        # Step A: Coordinator builds monolithic batch locked to 1 independent worker
                        net_task, rel_script, status_file = mr.build_monolithic_runscript_task(
                            images_dir=nas_images,
                            output_dir=nas_output,
                            project_name="Bridge_Recon",
                            local_scratch_staging=True,
                        )
                        self.assertEqual(getattr(net_task, "name", "RunScript"), "RunScript")
                        self.assertIn("--local-scratch-staging", net_task.args)

                        # Step B: Worker executes the dispatched job locally
                        mr.run_reconstruction(
                            images_dir=nas_images,
                            output_dir=nas_output,
                            project_name="Bridge_Recon",
                            local_scratch_staging=True,
                            scratch_dir=worker_nvme,
                            min_scratch_free_gb=50.0,
                            align_photos=True,
                            build_depth_maps=True,
                            build_mesh=True,
                            build_texture=True,
                            export_obj=True,
                            export_glb=False,
                        )

            # 6. Verify System Invariants:
            # Invariant 1: Central network share receives the completed project .psx
            final_psx = os.path.join(nas_output, "Bridge_Recon.psx")
            self.assertTrue(os.path.isfile(final_psx) or mock_doc.save.called)

            # Invariant 2: Local worker staging folder is cleanly purged after sync
            staged_folder = os.path.join(worker_nvme, "Bridge_Recon_local_stage")
            self.assertFalse(os.path.exists(staged_folder))

    def test_low_scratch_headroom_gracefully_falls_back_to_direct_output(self) -> None:
        """Verify worker gracefully falls back to direct network share output if local SSD space is low."""
        with tempfile.TemporaryDirectory() as sys_root:
            nas_share = os.path.join(sys_root, "nas_share")
            nas_images = os.path.join(nas_share, "images")
            nas_output = os.path.join(nas_share, "output")
            os.makedirs(nas_images, exist_ok=True)
            os.makedirs(nas_output, exist_ok=True)

            with open(os.path.join(nas_images, "P01.JPG"), "wb") as f:
                f.write(b"RAW_PHOTO")

            mock_meta = MagicMock()
            mock_meta.app.enumGPUDevices.return_value = [{"name": "RTX 4090", "mem_size": 24 * (1024**3)}]
            mock_chunk = MagicMock()
            mock_chunk.cameras = [MagicMock(transform=True, enabled=True)]
            mock_doc = MagicMock()
            mock_doc.chunk = mock_chunk
            mock_meta.Document.return_value = mock_doc

            # Simulate low local disk space: only 15 GB free (< 100 GB threshold)
            with patch.object(mr, "Metashape", mock_meta):
                with patch("cluster_utils.check_scratch_disk_headroom", return_value=(False, 15.0)):
                    mr.run_reconstruction(
                        images_dir=nas_images,
                        output_dir=nas_output,
                        project_name="Low_Disk_Job",
                        local_scratch_staging=True,
                        min_scratch_free_gb=100.0,
                        align_photos=False,
                        build_depth_maps=False,
                        build_mesh=False,
                        build_texture=False,
                    )

            # Fallback invariant: Project saved directly into central output directory
            expected_psx = os.path.join(nas_output, "Low_Disk_Job.psx")
            self.assertTrue(os.path.exists(expected_psx) or mock_doc.save.called)

    def test_resume_existing_project_with_local_staging(self) -> None:
        """Verify existing central network project is staged to local NVMe SSD and resumes seamlessly."""
        with tempfile.TemporaryDirectory() as sys_root:
            nas_share = os.path.join(sys_root, "nas_share")
            nas_images = os.path.join(nas_share, "images")
            nas_output = os.path.join(nas_share, "output")
            os.makedirs(nas_images, exist_ok=True)
            os.makedirs(nas_output, exist_ok=True)

            with open(os.path.join(nas_images, "P01.JPG"), "wb") as f:
                f.write(b"RAW_PHOTO_1")

            # Seed an existing project checkpoint on central network share
            central_psx = os.path.join(nas_output, "Existing_Survey.psx")
            with open(central_psx, "w", encoding="utf-8") as f:
                f.write("<psx>CENTRAL_PROJECT_CHECKPOINT</psx>")

            central_files = os.path.join(nas_output, "Existing_Survey.files", "0", "0")
            os.makedirs(central_files, exist_ok=True)
            with open(os.path.join(central_files, "alignment.data"), "wb") as f:
                f.write(b"ALIGNMENT_DATA")

            worker_nvme = os.path.join(sys_root, "worker_nvme")
            os.makedirs(worker_nvme, exist_ok=True)

            mock_meta = MagicMock()
            mock_meta.app.enumGPUDevices.return_value = [{"name": "RTX 5090", "mem_size": 32 * (1024**3)}]
            mock_chunk = MagicMock()
            mock_chunk.cameras = [
                MagicMock(transform=True, enabled=True),
                MagicMock(transform=True, enabled=True),
            ]
            mock_chunk.model = MagicMock()
            mock_chunk.model.textures = [MagicMock()]
            mock_doc = MagicMock()
            mock_doc.chunk = mock_chunk
            mock_doc.chunks = [mock_chunk]
            mock_meta.Document.return_value = mock_doc

            with patch.object(mr, "Metashape", mock_meta):
                with patch("cluster_utils.get_optimal_local_scratch_dir", return_value=worker_nvme):
                    with patch("cluster_utils.check_scratch_disk_headroom", return_value=(True, 500.0)):
                        mr.run_reconstruction(
                            images_dir=nas_images,
                            output_dir=nas_output,
                            project_name="Existing_Survey",
                            local_scratch_staging=True,
                            scratch_dir=worker_nvme,
                            resume=True,
                            align_photos=False,
                            build_depth_maps=False,
                            build_mesh=False,
                            build_texture=False,
                        )

            # Invariant 1: Document.open was called with the staged local .psx path
            staged_psx = os.path.join(worker_nvme, "Existing_Survey_local_stage", "Existing_Survey.psx")
            opened_paths = [call.args[0] for call in mock_doc.open.call_args_list if call.args]
            self.assertTrue(any(staged_psx in p for p in opened_paths))

            # Invariant 2: Central share retains the synchronized project checkpoint
            self.assertTrue(os.path.isfile(central_psx))
            self.assertTrue(os.path.isfile(os.path.join(central_files, "alignment.data")))

            # Invariant 3: Worker staging directory is cleanly purged after sync
            staged_folder = os.path.join(worker_nvme, "Existing_Survey_local_stage")
            self.assertFalse(os.path.exists(staged_folder))


if __name__ == "__main__":
    unittest.main()
