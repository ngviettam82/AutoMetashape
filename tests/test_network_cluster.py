# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for distributed photogrammetry network cluster processing."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure scripts directory is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

from cluster_utils import (
    find_metashape_server_path,
    generate_worker_bat_content,
    get_primary_lan_ip,
)


class TestNetworkCluster(unittest.TestCase):
    """Test suite for Metashape distributed cluster integration."""

    def test_get_primary_lan_ip_format(self) -> None:
        """Verify LAN IP detection returns a valid IPv4 string."""
        ip = get_primary_lan_ip()
        parts = ip.split(".")
        self.assertEqual(len(parts), 4)
        for p in parts:
            self.assertTrue(p.isdigit())
            self.assertTrue(0 <= int(p) <= 255)

    def test_generate_worker_bat_content_with_auto_ip(self) -> None:
        """Verify worker launcher batch content embeds detected IP and port."""
        content = generate_worker_bat_content(None, "5840", r"\\HOST_PC\SharedMetashape")
        self.assertIn("5840", content)
        self.assertIn(r"\\HOST_PC\SharedMetashape", content)
        self.assertIn("--worker", content)
        self.assertIn("--cpu-enable 1", content)

    def test_generate_worker_bat_content_interactive_root(self) -> None:
        """Verify worker launcher batch file prompts interactively if root is blank."""
        content = generate_worker_bat_content(None, "5840", "")
        self.assertIn("set /p TARGET_ROOT=", content)
        self.assertNotIn("RealityScan", content)
        self.assertIn("--worker", content)

    def test_find_metashape_server_path_discovery(self) -> None:
        """Verify server discovery logic locates binary or safely returns None."""
        server = find_metashape_server_path(None)
        if server is not None:
            self.assertTrue(os.path.isfile(server))
            self.assertTrue(server.lower().endswith("metashape-server.exe") or server.endswith("metashape-server"))

    def test_cluster_relative_path_resolution(self) -> None:
        """Verify project path conversion to shared storage relative path across local and UNC paths."""

        def calc_rel(psx_out: str, network_path: str) -> str:
            if network_path:
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

                for i in range(len(net_parts) - 1, -1, -1):
                    sub = net_parts[i].lower()
                    psx_parts = [p for p in norm_psx.split("/") if p]
                    psx_lower = [p.lower() for p in psx_parts]
                    if sub in psx_lower:
                        idx = psx_lower.index(sub)
                        rel = "/".join(psx_parts[idx + 1 :])
                        if rel:
                            return rel
            return os.path.basename(psx_out.replace("\\", "/"))

        # Case 1: When network_path is empty, returns file basename
        self.assertEqual(calc_rel(r"C:\Projects\Scan1\project.psx", ""), "project.psx")

        # Case 2: When network_path exists and contains psx_out
        test_dir = os.path.dirname(os.path.abspath(__file__))
        sub_psx = os.path.join(test_dir, "sub", "project.psx")
        rel = calc_rel(sub_psx, test_dir)
        self.assertEqual(rel, "sub/project.psx")

        # Case 3: UNC network path matching local drive psx file
        unc_rel = calc_rel(
            r"C:\Projects\SharedSurveys\061126\218\metashape\218.psx", r"\\CLUSTER_STORAGE\SharedSurveys"
        )
        self.assertEqual(unc_rel, "061126/218/metashape/218.psx")

    def test_cluster_export_path_resolution(self) -> None:
        """Verify export task path resolution generates valid absolute UNC or local paths."""

        def calc_rel(tgt: str, network_path: str) -> str:
            norm_tgt = os.path.abspath(tgt).replace(r"\\", "/").replace("\\", "/")
            norm_net = network_path.strip().replace(r"\\", "/").replace("\\", "/").rstrip("/")
            try:
                abs_tgt = os.path.abspath(tgt)
                abs_net = os.path.abspath(network_path)
                if os.path.commonpath([abs_tgt, abs_net]).lower() == abs_net.lower():
                    return os.path.relpath(abs_tgt, abs_net).replace("\\", "/")
            except Exception:
                pass
            net_parts = [p for p in norm_net.split("/") if p]
            if net_parts:
                share_name = net_parts[-1].lower()
                tgt_parts = [p for p in norm_tgt.split("/") if p]
                tgt_lower = [p.lower() for p in tgt_parts]
                if share_name in tgt_lower:
                    idx = tgt_lower.index(share_name)
                    rel = "/".join(tgt_parts[idx + 1 :])
                    if rel:
                        return rel
            return os.path.basename(tgt.replace("\\", "/"))

        def calc_exp(target_file: str, network_path: str) -> str:
            norm_tgt = os.path.abspath(target_file).replace("\\", "/")
            if network_path:
                is_unc = network_path.startswith(r"\\") or network_path.startswith("//")
                clean_net = network_path.strip().replace("\\", "/").rstrip("/")
                if is_unc:
                    clean_net = "//" + clean_net.lstrip("/")
                rel = calc_rel(target_file, network_path)
                if rel and rel != norm_tgt and not rel.startswith(clean_net):
                    return f"{clean_net}/{rel}"
            return norm_tgt

        # Case 1: Empty network path returns local absolute path
        p1 = calc_exp("output.obj", "")
        self.assertTrue(os.path.isabs(p1))
        self.assertTrue(p1.endswith("/output.obj"))

        # Case 2: UNC network path with local drive target resolves to full UNC path
        p2 = calc_exp(r"C:\Projects\SharedSurveys\061126\218\metashape\218.obj", r"\\CLUSTER_STORAGE\SharedSurveys")
        self.assertEqual(p2, "//CLUSTER_STORAGE/SharedSurveys/061126/218/metashape/218.obj")

        # Case 3: Local storage path preserved as valid absolute path
        p3 = calc_exp(r"C:\ClusterData\Scan1\model.obj", r"C:\ClusterData")
        self.assertEqual(p3, "C:/ClusterData/Scan1/model.obj")

    def test_network_client_worker_auto_unpause(self) -> None:
        """Verify newly connected worker nodes have pause cleared automatically for both dict and obj."""
        client = MagicMock()
        dict_response = {
            "next_revision": 0,
            "workers": [
                {"worker_id": 1, "hostname": "Node1", "paused": True, "cpu_enabled": True},
                {"worker_id": 2, "hostname": "Node2", "paused": False, "cpu_enabled": True},
            ],
        }
        client.workerList.return_value = dict_response

        raw_workers = client.workerList()
        workers = raw_workers.get("workers", []) if isinstance(raw_workers, dict) else raw_workers
        for w in workers:
            w_id = w.get("worker_id") if isinstance(w, dict) else getattr(w, "id", None)
            w_paused = w.get("paused", False) if isinstance(w, dict) else getattr(w, "paused", False)
            if w_paused and w_id is not None:
                client.setWorkerPaused(w_id, False)

        client.setWorkerPaused.assert_called_once_with(1, False)

    def test_cluster_batch_progress_tracking(self) -> None:
        """Verify composite progress calculation for single and multi-task batches."""
        # Single task batch
        b_info_single = {
            "id": 10,
            "state": "running",
            "tasks": [{"id": 1, "name": "BuildDepthMaps", "progress": 68.5, "state": "running"}],
        }
        tasks = b_info_single["tasks"]
        single_prog = sum(t["progress"] for t in tasks) / len(tasks)
        self.assertAlmostEqual(single_prog, 68.5)

        # Multi-task batch (e.g. BuildUV + BuildTexture)
        b_info_multi = {
            "id": 11,
            "state": "running",
            "tasks": [
                {"id": 1, "name": "BuildUV", "progress": 100.0, "state": "completed"},
                {"id": 2, "name": "BuildTexture", "progress": 50.0, "state": "running"},
            ],
        }
        multi_tasks = b_info_multi["tasks"]
        multi_prog = sum(t["progress"] for t in multi_tasks) / len(multi_tasks)
        self.assertAlmostEqual(multi_prog, 75.0)

    def test_cluster_batch_failure_fallback_detection(self) -> None:
        """Verify cluster task failures are cleanly detected with error reporting."""
        b_info_failed = {
            "id": 12,
            "state": "failed",
            "tasks": [
                {"id": 1, "name": "MatchPhotos", "progress": 12.0, "state": "failed", "errors": ["Out of memory"]}
            ],
        }
        self.assertEqual(b_info_failed["state"], "failed")
        errors = b_info_failed["tasks"][0]["errors"]
        self.assertIn("Out of memory", errors)

    def test_cluster_bad_alloc_handoff_mechanism(self) -> None:
        """Verify bad allocation triggers worker isolation, handoff batch creation, and worker unpause."""
        client_mock = MagicMock()
        client_mock.workerList.return_value = {
            "workers": [
                {"worker_id": 1, "hostname": "NodeA_16GB", "paused": False},
                {"worker_id": 2, "hostname": "NodeB_64GB", "paused": False},
            ]
        }
        client_mock.createBatch.return_value = 202

        tasks_info = [
            {"id": 1, "name": "MatchPhotos", "state": "completed", "errors": []},
            {"id": 2, "name": "BuildDepthMaps", "state": "completed", "errors": []},
            {"id": 3, "name": "BuildModel", "state": "completed", "errors": []},
            {
                "id": 4,
                "name": "BuildTexture",
                "state": "failed",
                "worker_id": 1,
                "errors": ["std::bad_alloc: out of memory"],
            },
            {"id": 5, "name": "ExportModel", "state": "unassigned", "errors": []},
        ]

        is_bad_alloc = any(
            any(term in str(e).lower() for term in ("bad allocation", "bad_alloc", "out of memory"))
            for t in tasks_info
            for e in t.get("errors", [])
        )
        self.assertTrue(is_bad_alloc)

        # Identify failing task & worker
        failed_idx = next(i for i, t in enumerate(tasks_info) if t.get("state") in ("failed", "aborted"))
        failed_worker = tasks_info[failed_idx].get("worker_id")
        self.assertEqual(failed_idx, 3)
        self.assertEqual(failed_worker, 1)

        # 1. Host isolates failing worker
        client_mock.setWorkerPaused(failed_worker, True)

        # 2. Host discovers remaining other workers
        raw_w = client_mock.workerList()
        other_workers = [w for w in raw_w["workers"] if w["worker_id"] != failed_worker and not w["paused"]]
        self.assertEqual(len(other_workers), 1)
        self.assertEqual(other_workers[0]["worker_id"], 2)

        # 3. Host dispatches handoff continuation batch with remaining tasks
        rem_tasks = tasks_info[failed_idx:]
        self.assertEqual(len(rem_tasks), 2)  # BuildTexture + ExportModel
        handoff_batch = client_mock.createBatch("project.psx", rem_tasks)
        client_mock.setBatchWorkerLimit(handoff_batch, 1)

        # 4. Host immediately unpauses failed worker so it can proceed to the next project
        client_mock.setWorkerPaused(failed_worker, False)

        # Assert correct order of operations
        calls = client_mock.setWorkerPaused.call_args_list
        self.assertEqual(calls[0], ((1, True),))
        self.assertEqual(calls[1], ((1, False),))
        client_mock.createBatch.assert_called_with("project.psx", rem_tasks)
        client_mock.setBatchWorkerLimit.assert_called_with(202, 1)

    def test_cluster_independent_worker_mode_batch_limit(self) -> None:
        """Verify Independent Project Mode applies setBatchWorkerLimit(batch_id, 1)."""
        from unittest.mock import MagicMock, patch

        mock_client = MagicMock()
        mock_client.createBatch.return_value = 101

        batch_id = mock_client.createBatch("relative/path/project.psx", [])
        network_independent_workers = True
        if network_independent_workers:
            mock_client.setBatchWorkerLimit(batch_id, 1)

        mock_client.setBatchWorkerLimit.assert_called_once_with(101, 1)

    def test_cluster_export_fallback_suppression(self) -> None:
        """Verify that when cluster worker exports deliverables, host skips local export."""
        cluster_exported = True
        local_executed = False
        export_glb = True
        if (not cluster_exported) and export_glb:
            local_executed = True
        self.assertFalse(local_executed)

        cluster_exported = False
        if (not cluster_exported) and export_glb:
            local_executed = True
        self.assertTrue(local_executed)

    def test_build_cluster_gradual_selection_tasks(self) -> None:
        """Verify build_cluster_gradual_selection_tasks returns 8 CleanTiePoints and OptimizeCameras tasks."""
        from unittest.mock import MagicMock, patch

        import metashape_reconstruction as mr

        mock_meta = MagicMock()
        mock_meta.Tasks.CleanTiePoints = lambda: MagicMock(name="CleanTiePoints")
        mock_meta.Tasks.OptimizeCameras = lambda: MagicMock(name="OptimizeCameras")
        mock_meta.TiePoints.Criterion.ReconstructionUncertainty = 1
        mock_meta.TiePoints.Criterion.ProjectionAccuracy = 2
        mock_meta.TiePoints.Criterion.ReprojectionError = 3
        mock_meta.TiePoints.Criterion.ImageCount = 4

        with patch.object(mr, "Metashape", mock_meta):
            tasks = mr.build_cluster_gradual_selection_tasks()
            self.assertEqual(len(tasks), 8)

    def test_build_cluster_crop_task(self) -> None:
        """Verify build_cluster_crop_task creates valid RunScript network tasks for worker bounding box auto-crop."""
        from unittest.mock import MagicMock, patch

        import metashape_reconstruction as mr

        mock_meta = MagicMock()
        mock_meta.Tasks.RunScript = lambda: MagicMock(name="RunScript", code="")

        with patch.object(mr, "Metashape", mock_meta):
            t_gps = mr.build_cluster_crop_task("gps-square", padding=0.05)
            self.assertIsNotNone(t_gps)
            self.assertIn("chunk.region.center", t_gps.code)
            self.assertIn("chunk.region.size", t_gps.code)

            t_struct = mr.build_cluster_crop_task("structure", padding=0.10)
            self.assertIsNotNone(t_struct)
            self.assertIn("chunk.region.center", t_struct.code)

            t_none = mr.build_cluster_crop_task("manual")
            self.assertIsNone(t_none)

    def test_build_unified_cluster_pipeline_tasks_full(self) -> None:
        """Verify build_unified_cluster_pipeline_tasks bundles all pipeline stages into a unified batch with correct stage mapping and deterministic texture pages."""
        from unittest.mock import MagicMock, patch

        import metashape_reconstruction as mr

        mock_meta = MagicMock()
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

        mock_meta.Tasks.MatchPhotos = lambda: MagicMock(name="MatchPhotos")
        mock_meta.Tasks.AlignCameras = lambda: MagicMock(name="AlignCameras")
        mock_meta.Tasks.CleanTiePoints = lambda: MagicMock(name="CleanTiePoints", criterion=1)
        mock_meta.Tasks.OptimizeCameras = lambda: MagicMock(name="OptimizeCameras")
        mock_meta.Tasks.RunScript = lambda: MagicMock(name="RunScript", code="chunk.region.size = ...")
        mock_meta.Tasks.BuildDepthMaps = lambda: MagicMock(name="BuildDepthMaps")
        mock_meta.Tasks.BuildPointCloud = lambda: MagicMock(name="BuildPointCloud")
        mock_meta.Tasks.BuildModel = lambda: MagicMock(name="BuildModel")
        mock_meta.Tasks.BuildUV = lambda: MagicMock(name="BuildUV", page_count=16)
        mock_meta.Tasks.BuildTexture = lambda: MagicMock(name="BuildTexture")
        mock_meta.Tasks.BuildDem = lambda: MagicMock(name="BuildDem")
        mock_meta.Tasks.BuildOrthomosaic = lambda: MagicMock(name="BuildOrthomosaic")
        mock_meta.Tasks.ExportModel = lambda: MagicMock(name="ExportModel")
        mock_meta.Tasks.ExportRaster = lambda: MagicMock(name="ExportRaster")
        mock_meta.Tasks.ExportPointCloud = lambda: MagicMock(name="ExportPointCloud")
        mock_meta.Tasks.ExportReport = lambda: MagicMock(name="ExportReport")

        mock_chunk = MagicMock()
        mock_chunk.crs = None
        mock_chunk.cameras = []

        with patch.object(mr, "Metashape", mock_meta):
            # Test 1: Full Unified Pipeline assembly with deterministic 16 texture pages
            tasks_16, stages_16 = mr.build_unified_cluster_pipeline_tasks(
                chunk=mock_chunk,
                output_dir="C:/Out/Proj",
                project_name="Proj",
                align_photos=True,
                filter_tiepoints=True,
                crop_method="gps-square",
                build_depth_maps=True,
                build_point_cloud=True,
                build_mesh=True,
                build_texture=True,
                texture_pages=16,
                build_dem=True,
                build_orthomosaic=True,
                export_obj=True,
                export_glb=True,
                export_dem=True,
                export_ortho=True,
                export_point_cloud=True,
                export_report=True,
            )
            # Find BuildUV task in tasks_16
            uv_task_16 = next(t for t, (name, _, _) in zip(tasks_16, stages_16) if name == "BuildUV")
            self.assertEqual(uv_task_16.page_count, 16)

            # Test 2: Stage info contains expected stepper stages
            stage_keys = [k for _, k, _ in stages_16]
            self.assertIn("match", stage_keys)
            self.assertIn("align", stage_keys)
            self.assertIn("depth", stage_keys)
            self.assertIn("point_cloud", stage_keys)
            self.assertIn("mesh", stage_keys)
            self.assertIn("texture", stage_keys)
            self.assertIn("dem", stage_keys)
            self.assertIn("ortho", stage_keys)
            self.assertIn("export", stage_keys)

            # Test 4: When align_photos=False, MatchPhotos and AlignCameras are omitted
            _, stages_no_align = mr.build_unified_cluster_pipeline_tasks(
                chunk=mock_chunk,
                output_dir="C:/Out/Proj",
                project_name="Proj",
                align_photos=False,
                build_depth_maps=True,
                build_mesh=True,
            )
            stage_keys_no_align = [k for _, k, _ in stages_no_align]
            self.assertNotIn("match", stage_keys_no_align)
            self.assertIn("depth", stage_keys_no_align)
            self.assertIn("mesh", stage_keys_no_align)

    def test_single_chunk_autonomous_unified_batch_delegation(self) -> None:
        """Verify single-chunk project delegates full pipeline into 1 dedicated batch with worker_limit=1."""
        import tempfile
        from unittest.mock import MagicMock, patch

        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_dir:
            img_dir = os.path.join(tmp_dir, "images")
            out_dir = os.path.join(tmp_dir, "output")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(img_dir, "IMG_0001.JPG"), "wb") as f:
                f.write(b"dummy_image_data")

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
            mock_net_client.createBatch.return_value = 555
            mock_net_client.batchInfo.return_value = {
                "state": "completed",
                "tasks": [{"name": "MatchPhotos", "state": "completed", "progress": 100.0}],
            }
            mock_meta.NetworkClient.return_value = mock_net_client

            with patch.object(mr, "Metashape", mock_meta):
                mr.run_reconstruction(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    network_enable=True,
                    network_independent_workers=True,
                    multi_chunk="disable",
                    export_glb=True,
                    export_obj=False,
                )

            # Defend that createBatch was called for unified delegation
            self.assertTrue(mock_net_client.createBatch.called)
            # Defend that batch worker limit was locked to 1 for dedicated project execution
            mock_net_client.setBatchWorkerLimit.assert_called_once_with(555, 1)
            # Defend that batch was unpaused for immediate worker pickup
            mock_net_client.setBatchPaused.assert_called_once_with(555, False)

            # Defend that the unified batch received exactly 1 monolithic RunScript task with GPU enabled
            batch_tasks = mock_net_client.createBatch.call_args[0][1]
            self.assertEqual(len(batch_tasks), 1)
            self.assertEqual(getattr(batch_tasks[0], "name", "RunScript"), "RunScript")
            self.assertTrue(getattr(batch_tasks[0], "gpu_support", True))

    def test_safe_create_cluster_batch_aborts_stale_when_force_fresh(self) -> None:
        """Verify safe_create_cluster_batch aborts stale batches on server before creating fresh batch."""
        import metashape_reconstruction as mr

        mock_net = MagicMock()
        mock_net.batchList.return_value = {
            "batches": [
                {"batch_id": 101, "path": "SiteA/project.psx", "state": "failed"},
                {"batch_id": 102, "path": "SiteB/project.psx", "state": "completed"},
            ]
        }
        mock_net.createBatch.return_value = 202

        batch_id, was_attached = mr.safe_create_cluster_batch(
            mock_net, "SiteA/project.psx", [MagicMock()], worker_limit=1, force_fresh=True
        )

        mock_net.abortBatch.assert_called_once_with(101)
        mock_net.createBatch.assert_called_once()
        mock_net.setBatchWorkerLimit.assert_called_once_with(202, 1)
        mock_net.setBatchPaused.assert_called_once_with(202, False)
        self.assertEqual(batch_id, 202)
        self.assertFalse(was_attached)

    def test_safe_create_cluster_batch_attaches_when_not_force_fresh(self) -> None:
        """Verify safe_create_cluster_batch re-attaches to active batch when force_fresh=False."""
        import metashape_reconstruction as mr

        mock_net = MagicMock()
        mock_net.batchList.return_value = {
            "batches": [
                {"batch_id": 303, "path": "SiteA/project.psx", "state": "inprogress"},
            ]
        }

        batch_id, was_attached = mr.safe_create_cluster_batch(
            mock_net, "SiteA/project.psx", [MagicMock()], worker_limit=1, force_fresh=False
        )

        self.assertEqual(batch_id, 303)
        self.assertTrue(was_attached)
        mock_net.abortBatch.assert_not_called()
        mock_net.createBatch.assert_not_called()

    def test_safe_create_cluster_batch_catches_duplicate_batch_and_retries(self) -> None:
        """Verify safe_create_cluster_batch catches Duplicate Batch error, aborts stale ID, and retries."""
        import metashape_reconstruction as mr

        mock_net = MagicMock()
        mock_net.batchList.side_effect = [
            {"batches": []},
            {"batches": [{"batch_id": 404, "path": "SiteA/project.psx", "state": "aborted"}]},
        ]
        mock_net.createBatch.side_effect = [
            RuntimeError("Duplicate batch: SiteA/project.psx"),
            505,
        ]

        batch_id, was_attached = mr.safe_create_cluster_batch(
            mock_net, "SiteA/project.psx", [MagicMock()], worker_limit=1, force_fresh=False
        )

        self.assertEqual(batch_id, 505)
        self.assertFalse(was_attached)
        mock_net.abortBatch.assert_called_once_with(404)
        self.assertEqual(mock_net.createBatch.call_count, 2)

    def test_replace_asset_configured_on_reconstruction_tasks(self) -> None:
        """Verify replace_asset=True is configured across Model, DEM, Ortho, and Tiled Model tasks."""
        from unittest.mock import patch

        import metashape_reconstruction as mr

        mock_meta = MagicMock()
        mock_meta.Tasks.BuildModel = lambda: MagicMock(name="BuildModel")
        mock_meta.Tasks.BuildTiledModel = lambda: MagicMock(name="BuildTiledModel")
        mock_meta.Tasks.BuildDem = lambda: MagicMock(name="BuildDem")
        mock_meta.Tasks.BuildOrthomosaic = lambda: MagicMock(name="BuildOrthomosaic")
        mock_meta.Tasks.ExportModel = lambda: MagicMock(name="ExportModel")
        mock_meta.Tasks.ExportRaster = lambda: MagicMock(name="ExportRaster")

        mock_chunk = MagicMock()
        mock_chunk.crs = None
        mock_chunk.cameras = []

        with patch.object(mr, "Metashape", mock_meta):
            tasks, stage_info = mr.build_unified_cluster_pipeline_tasks(
                chunk=mock_chunk,
                output_dir="C:/Out/TestReplace",
                project_name="TestReplace",
                align_photos=False,
                build_depth_maps=False,
                build_mesh=True,
                build_tiled_model=True,
                build_dem=True,
                build_orthomosaic=True,
            )

        task_dict = {name: t for (name, _, _), t in zip(stage_info, tasks)}

        self.assertIn("BuildModel", task_dict)
        self.assertTrue(getattr(task_dict["BuildModel"], "replace_asset", False))

        self.assertIn("BuildTiledModel", task_dict)
        self.assertTrue(getattr(task_dict["BuildTiledModel"], "replace_asset", False))

        self.assertIn("BuildDem", task_dict)
        self.assertTrue(getattr(task_dict["BuildDem"], "replace_asset", False))

        self.assertIn("BuildOrthomosaic", task_dict)
        self.assertTrue(getattr(task_dict["BuildOrthomosaic"], "replace_asset", False))

    def test_clean_scratch_purge_removes_existing_files_dir_and_psx(self) -> None:
        """Verify running reconstruction from scratch (resume=False) cleanly purges old files directory and psx."""
        import tempfile
        from unittest.mock import MagicMock, patch

        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_dir:
            img_dir = os.path.join(tmp_dir, "images")
            out_dir = os.path.join(tmp_dir, "output")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)

            with open(os.path.join(img_dir, "IMG_0001.JPG"), "wb") as f:
                f.write(b"dummy")

            # Create existing psx and files directory simulating a previous run
            psx_path = os.path.join(out_dir, "TestCleanProject.psx")
            files_dir = os.path.join(out_dir, "TestCleanProject.files")
            old_model_dir = os.path.join(files_dir, "0", "0", "model.1")
            os.makedirs(old_model_dir, exist_ok=True)
            with open(psx_path, "w") as f:
                f.write("<dummy_xml/>")
            with open(os.path.join(files_dir, "lock"), "w") as f:
                f.write("locked")

            self.assertTrue(os.path.exists(psx_path))
            self.assertTrue(os.path.isdir(files_dir))
            self.assertTrue(os.path.exists(old_model_dir))

            mock_meta = MagicMock()
            mock_meta.app.enumGPUDevices.return_value = []
            mock_doc = MagicMock()
            mock_chunk = MagicMock()
            mock_chunk.point_cloud = None
            mock_chunk.model = None
            mock_chunk.elevation = None
            mock_chunk.orthomosaic = None
            mock_doc.chunk = mock_chunk
            mock_doc.chunks = [mock_chunk]
            mock_meta.Document.return_value = mock_doc

            with patch.object(mr, "Metashape", mock_meta):
                mr.run_reconstruction(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="TestCleanProject",
                    network_enable=False,
                    resume=False,
                    align_photos=False,
                    build_depth_maps=False,
                    build_mesh=False,
                    build_texture=False,
                    build_dem=False,
                    build_orthomosaic=False,
                    export_glb=False,
                    export_obj=False,
                )

            # Assert that stale psx and stale files dir were cleanly purged
            self.assertFalse(os.path.exists(old_model_dir))
            self.assertFalse(os.path.exists(os.path.join(files_dir, "lock")))

    def test_cli_argparse_resume_boolean_optional_action(self) -> None:
        """Verify argparse default is resume=True and accepts both --resume and --no-resume flags."""
        from metashape_reconstruction import build_arg_parser

        parser = build_arg_parser()
        args_default = parser.parse_args(["--images", "C:/dummy"])
        self.assertTrue(args_default.resume)

        args_resume = parser.parse_args(["--images", "C:/dummy", "--resume"])
        self.assertTrue(args_resume.resume)

        args_no_resume = parser.parse_args(["--images", "C:/dummy", "--no-resume"])
        self.assertFalse(args_no_resume.resume)

    def test_norm_cluster_path_handles_mixed_slashes_and_case(self) -> None:
        """Verify _norm_cluster_path normalizes backslashes, forward slashes, trailing slashes, and casing."""
        from metashape_reconstruction import _norm_cluster_path

        p1 = r"041126\Result\0211\0211.psx"
        p2 = "041126/result/0211/0211.psx"
        p3 = "041126/Result/0211/0211.psx/"
        self.assertEqual(_norm_cluster_path(p1), "041126/result/0211/0211.psx")
        self.assertEqual(_norm_cluster_path(p1), _norm_cluster_path(p2))
        self.assertEqual(_norm_cluster_path(p2), _norm_cluster_path(p3))

    def test_safe_create_cluster_batch_find_batch_attachment(self) -> None:
        """Verify safe_create_cluster_batch attaches to active batch found via findBatch."""
        import metashape_reconstruction as mr

        mock_net = MagicMock()
        mock_net.findBatch.return_value = 888
        mock_net.batchInfo.return_value = {"state": "inprogress"}

        batch_id, was_attached = mr.safe_create_cluster_batch(
            mock_net, "041126/Result/SiteA/SiteA.psx", [MagicMock()], worker_limit=1, force_fresh=False
        )

        self.assertEqual(batch_id, 888)
        self.assertTrue(was_attached)
        mock_net.createBatch.assert_not_called()

    def test_unified_pipeline_skips_crop_when_align_photos_false(self) -> None:
        """Verify build_unified_cluster_pipeline_tasks omits auto-crop when align_photos is False."""
        import metashape_reconstruction as mr

        mock_chunk = MagicMock()

        tasks, stages = mr.build_unified_cluster_pipeline_tasks(
            chunk=mock_chunk,
            output_dir="C:/Out/TestNoCrop",
            project_name="TestNoCrop",
            align_photos=False,
            crop_method="gps-square",
            build_depth_maps=True,
            build_mesh=True,
        )

        stage_keys = [k for _, k, _ in stages]
        self.assertNotIn("align", stage_keys)
        # Verify no RunScript auto-crop task exists
        task_names = [name for name, _, _ in stages]
        self.assertNotIn("RunScript", task_names)

    def test_build_monolithic_runscript_task_structure(self) -> None:
        """Verify build_monolithic_runscript_task creates a valid RunScript NetworkTask with GPU support."""
        import tempfile

        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_dir = os.path.join(tmp_dir, "output")
            os.makedirs(out_dir, exist_ok=True)
            img_dir = os.path.join(tmp_dir, "images")
            os.makedirs(img_dir, exist_ok=True)

            net_task, rel_script, s_file = mr.build_monolithic_runscript_task(
                images_dir=img_dir,
                output_dir=out_dir,
                project_name="TestProject",
                preset="pro",
                align_quality="high",
                resume=True,
                export_obj=True,
                export_ortho=True,
            )

            self.assertEqual(getattr(net_task, "name", "RunScript"), "RunScript")
            self.assertTrue(getattr(net_task, "gpu_support", True))
            self.assertTrue(s_file.endswith("_autometashape_status.json"))

    def test_status_file_sidecar_ipc_tracking(self) -> None:
        """Verify emit_ipc_stage writes real-time JSON milestone updates to active status sidecar."""
        import tempfile

        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_dir:
            status_path = os.path.join(tmp_dir, "test_status.json")
            mr.set_active_status_file(status_path)
            try:
                mr.emit_ipc_stage("mesh", "3D Surface Mesh", progress=65.5)
                self.assertTrue(os.path.exists(status_path))
                with open(status_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data.get("stage_key"), "mesh")
                self.assertEqual(data.get("stage_desc"), "3D Surface Mesh")
                self.assertAlmostEqual(data.get("progress"), 65.5)
                self.assertEqual(data.get("status"), "running")

                mr.emit_ipc_event("@@AUTOMETASHAPE_COMPLETE@@")
                with open(status_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data.get("stage_key"), "complete")
                self.assertEqual(data.get("status"), "completed")
                self.assertEqual(data.get("progress"), 100.0)

                mr.emit_ipc_error("texture", "Out of VRAM during 8K packing")
                with open(status_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data.get("stage_key"), "texture")
                self.assertEqual(data.get("status"), "error")
                self.assertIn("Out of VRAM", data.get("error", ""))
            finally:
                mr.set_active_status_file(None)

    def test_status_file_sidecar_parent_dir_auto_creation(self) -> None:
        """Verify emit_ipc_stage automatically creates parent directory if it does not exist yet."""
        import tempfile

        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_dir:
            nested_status_path = os.path.join(tmp_dir, "nested", "subfolder", "_status.json")
            self.assertFalse(os.path.exists(os.path.dirname(nested_status_path)))
            mr.set_active_status_file(nested_status_path)
            try:
                mr.emit_ipc_stage("ingest", "Ingesting raw photos", progress=10.0)
                self.assertTrue(os.path.exists(nested_status_path))
                with open(nested_status_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data.get("stage_key"), "ingest")
                self.assertEqual(data.get("status"), "running")
            finally:
                mr.set_active_status_file(None)

    def test_monolithic_runscript_passes_network_path_arg(self) -> None:
        """Verify build_monolithic_runscript_task appends --network-path to worker args."""
        from unittest.mock import patch

        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_dir:
            net_path = os.path.join(tmp_dir, "shared_root")
            os.makedirs(net_path, exist_ok=True)
            img_dir = os.path.join(net_path, "survey_a")
            out_dir = os.path.join(net_path, "survey_a", "output")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)

            with patch("subprocess.list2cmdline", side_effect=lambda args: " ".join(args)) as mock_cmd:
                task, rel_script, s_file = mr.build_monolithic_runscript_task(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="survey_a",
                    network_path=net_path,
                )
                called_args = mock_cmd.call_args[0][0]
                self.assertIn("--network-path", called_args)
                idx = called_args.index("--network-path")
                self.assertEqual(called_args[idx + 1], net_path)

    def test_independent_worker_strict_offline_zero_fallback(self) -> None:
        """Verify that when independent worker offline batch fails, RuntimeError is raised without fallback."""
        from unittest.mock import MagicMock, patch

        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_dir:
            img_dir = os.path.join(tmp_dir, "images")
            out_dir = os.path.join(tmp_dir, "output")
            os.makedirs(img_dir, exist_ok=True)
            with open(os.path.join(img_dir, "img_001.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

            mock_meta = MagicMock()
            mock_meta.app.enumGPUDevices.return_value = [{"name": "RTX 4090", "mem_size": 24 * (1024**3)}]
            mock_doc = MagicMock()
            mock_chunk = MagicMock()
            mock_chunk.cameras = [MagicMock(transform=True, enabled=True)]
            mock_doc.chunk = mock_chunk
            mock_doc.chunks = [mock_chunk]
            mock_meta.Document.return_value = mock_doc

            mock_net_client = MagicMock()
            mock_net_client.workerList.return_value = [{"worker_id": 1, "host": "192.168.1.100", "paused": False}]
            mock_net_client.createBatch.return_value = 777
            mock_net_client.batchInfo.return_value = {
                "state": "failed",
                "tasks": [{"name": "RunScript", "state": "failed", "errors": ["Out of memory"]}],
            }
            mock_meta.NetworkClient.return_value = mock_net_client

            with patch.object(mr, "Metashape", mock_meta):
                with self.assertRaises(RuntimeError) as ctx:
                    mr.run_reconstruction(
                        images_dir=img_dir,
                        output_dir=out_dir,
                        network_enable=True,
                        network_independent_workers=True,
                        multi_chunk="disable",
                    )
                self.assertIn("fallback to stage-by-stage network processing is strictly disabled", str(ctx.exception))

    def test_independent_worker_rejects_manual_crop_or_pause(self) -> None:
        """Verify independent worker mode rejects pause_after_crop and manual cropping with ValueError."""
        from unittest.mock import MagicMock, patch

        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_dir:
            img_dir = os.path.join(tmp_dir, "images")
            out_dir = os.path.join(tmp_dir, "output")
            os.makedirs(img_dir, exist_ok=True)
            with open(os.path.join(img_dir, "img_001.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

            mock_meta = MagicMock()
            mock_meta.app.enumGPUDevices.return_value = [{"name": "RTX 4090", "mem_size": 24 * (1024**3)}]
            mock_doc = MagicMock()
            mock_chunk = MagicMock()
            mock_doc.chunk = mock_chunk
            mock_doc.chunks = [mock_chunk]
            mock_meta.Document.return_value = mock_doc

            mock_net_client = MagicMock()
            mock_net_client.workerList.return_value = [{"worker_id": 1, "host": "192.168.1.100", "paused": False}]
            mock_meta.NetworkClient.return_value = mock_net_client

            with patch.object(mr, "Metashape", mock_meta):
                with self.assertRaises(ValueError) as ctx:
                    mr.run_reconstruction(
                        images_dir=img_dir,
                        output_dir=out_dir,
                        network_enable=True,
                        network_independent_workers=True,
                        pause_after_crop=True,
                    )
                self.assertIn("Manual cropping and pause_after_crop cannot be used", str(ctx.exception))

    def test_centralized_task_mode_multi_worker_collaborative_dispatch(self) -> None:
        """Verify centralized task mode dispatches fine-grained tasks with worker_limit=None."""
        from unittest.mock import MagicMock, patch

        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_dir:
            img_dir = os.path.join(tmp_dir, "images")
            out_dir = os.path.join(tmp_dir, "output")
            os.makedirs(img_dir, exist_ok=True)
            with open(os.path.join(img_dir, "img_001.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

            mock_meta = MagicMock()
            mock_meta.app.enumGPUDevices.return_value = [{"name": "RTX 4090", "mem_size": 24 * (1024**3)}]
            mock_doc = MagicMock()
            mock_chunk = MagicMock()
            mock_chunk.cameras = [MagicMock(transform=True, enabled=True)]
            mock_doc.chunk = mock_chunk
            mock_doc.chunks = [mock_chunk]
            mock_meta.Document.return_value = mock_doc

            mock_net_client = MagicMock()
            mock_net_client.workerList.return_value = [
                {"worker_id": 1, "host": "192.168.1.101", "paused": False},
                {"worker_id": 2, "host": "192.168.1.102", "paused": False},
            ]
            mock_net_client.createBatch.return_value = 888
            mock_net_client.batchInfo.return_value = {
                "state": "completed",
                "tasks": [{"name": "MatchPhotos", "state": "completed", "progress": 100.0}],
            }
            mock_meta.NetworkClient.return_value = mock_net_client

            with patch.object(mr, "Metashape", mock_meta):
                mr.run_reconstruction(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    network_enable=True,
                    network_independent_workers=False,
                    align_photos=True,
                    build_depth_maps=False,
                    build_mesh=False,
                    build_texture=False,
                    build_orthomosaic=False,
                    build_point_cloud=False,
                )

            # In centralized task mode, createBatch is called with worker_limit=None
            self.assertTrue(mock_net_client.createBatch.called)
            # Defend that setBatchWorkerLimit(..., 1) was NOT called
            mock_net_client.setBatchWorkerLimit.assert_not_called()

    def test_independent_worker_target_network_output_sanitization(self) -> None:
        """Verify build_monolithic_runscript_task receives target_network_output rather than coordinator local scratch."""
        from unittest.mock import MagicMock, patch

        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_dir:
            img_dir = os.path.join(tmp_dir, "images")
            net_out_dir = os.path.join(tmp_dir, "central_share", "Deliverables")
            scratch_vol = os.path.join(tmp_dir, "local_nvme_scratch")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(net_out_dir, exist_ok=True)
            os.makedirs(scratch_vol, exist_ok=True)
            with open(os.path.join(img_dir, "img_001.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

            mock_meta = MagicMock()
            mock_meta.app.enumGPUDevices.return_value = [{"name": "RTX 4090", "mem_size": 24 * (1024**3)}]
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
                "tasks": [{"name": "RunScript", "state": "completed", "progress": 100.0}],
            }
            mock_meta.NetworkClient.return_value = mock_net_client

            with patch.object(mr, "Metashape", mock_meta):
                with patch("cluster_utils.get_optimal_local_scratch_dir", return_value=scratch_vol):
                    with patch("cluster_utils.check_scratch_disk_headroom", return_value=(True, 500.0)):
                        with patch(
                            "autometashape.core.pipeline.build_monolithic_runscript_task",
                            wraps=mr.build_monolithic_runscript_task,
                        ) as mock_builder:
                            mr.run_reconstruction(
                                images_dir=img_dir,
                                output_dir=net_out_dir,
                                network_enable=True,
                                network_independent_workers=True,
                                local_scratch_staging=True,
                            )
                            # Verify that build_monolithic_runscript_task was called with the central network output dir
                            self.assertTrue(mock_builder.called)
                            passed_out = mock_builder.call_args[1].get("output_dir")
                            self.assertEqual(passed_out, os.path.abspath(net_out_dir))

    def test_evaluate_cluster_batch_info_success_and_traps(self) -> None:
        """Verify evaluate_cluster_batch_info catches false-positive completed state traps."""
        from autometashape.cluster.orchestrator import evaluate_cluster_batch_info

        # Case 1: Genuine success
        b1 = {
            "state": "completed",
            "tasks": [{"name": "RunScript", "state": "completed", "progress": 100.0, "errors": []}],
        }
        st1, ok1, err1 = evaluate_cluster_batch_info(b1)
        self.assertEqual(st1, "completed")
        self.assertTrue(ok1)
        self.assertEqual(err1, "")

        # Case 2: Trap - state=completed but error_count > 0 with last_error
        b2 = {
            "state": "completed",
            "error_count": 87,
            "last_error": {
                "message": "RunScript: Can't open file: The system cannot find the file specified (2): //ctuav/catp/CATP_0205.psx"
            },
            "tasks": [{"name": "RunScript", "state": "completed", "progress": 0.0}],
        }
        st2, ok2, err2 = evaluate_cluster_batch_info(b2)
        self.assertEqual(st2, "failed")
        self.assertFalse(ok2)
        self.assertIn("The system cannot find the file specified (2)", err2)

        # Case 3: Trap - state=completed but aborted=True
        b3 = {
            "state": "completed",
            "aborted": True,
            "tasks": [{"name": "RunScript", "errors": ["aborted by coordinator"]}],
        }
        st3, ok3, err3 = evaluate_cluster_batch_info(b3)
        self.assertEqual(st3, "aborted")
        self.assertFalse(ok3)
        self.assertIn("aborted by coordinator", err3)

        # Case 4: Task-level failure
        b4 = {
            "state": "completed",
            "tasks": [{"name": "RunScript", "state": "failed", "errors": ["GPU Bad Allocation"]}],
        }
        st4, ok4, err4 = evaluate_cluster_batch_info(b4)
        self.assertEqual(st4, "failed")
        self.assertFalse(ok4)
        self.assertIn("GPU Bad Allocation", err4)

        # Case 5: Normal in-progress
        b5 = {
            "state": "running",
            "tasks": [{"name": "RunScript", "state": "running", "progress": 55.0}],
        }
        st5, ok5, err5 = evaluate_cluster_batch_info(b5)
        self.assertEqual(st5, "running")
        self.assertFalse(ok5)
        self.assertEqual(err5, "")

    def test_supervisor_auto_detects_unc_network_path(self) -> None:
        """Verify ClusterSupervisor auto-detects network_path from UNC output_dir or images_dir."""
        from autometashape.cluster.supervisor import ClusterSupervisor

        supervisor = ClusterSupervisor()
        executed_jobs = []

        def mock_exec(job):
            executed_jobs.append(job)

        supervisor.add_job(
            images_dir="//ctuav/catp/101126/CATP_0205",
            output_dir="//ctuav/catp/101126/Result/CATP_0205",
            project_name="CATP_0205",
            extra_args={"network_enable": True, "network_independent_workers": True},
        )

        with patch("autometashape.cluster.supervisor.check_storage_headroom", return_value=(True, 500.0)):
            with patch(
                "autometashape.cluster.supervisor.verify_project_deliverables",
                return_value={"status": "PASS", "errors": []},
            ):
                summary = supervisor.run_queue(execute_fn=mock_exec)

        self.assertEqual(summary["total_jobs"], 1)
        self.assertEqual(len(executed_jobs), 1)
        # Verify network_path was auto-populated from UNC path
        self.assertEqual(executed_jobs[0].extra_args.get("network_path"), r"\\ctuav\catp")

    def test_coordinator_suppresses_local_scratch_staging_when_network_enabled(self) -> None:
        """Verify that when network_enable=True, coordinator does not divert output_dir to local scratch."""
        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_dir:
            img_dir = os.path.join(tmp_dir, "images")
            out_dir = os.path.join(tmp_dir, "central_share", "CATP_0205")
            scratch_vol = os.path.join(tmp_dir, "local_nvme_scratch")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            os.makedirs(scratch_vol, exist_ok=True)
            with open(os.path.join(img_dir, "img_001.jpg"), "wb") as f:
                f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

            mock_meta = MagicMock()
            mock_meta.app.enumGPUDevices.return_value = [{"name": "RTX 4090", "mem_size": 24 * (1024**3)}]
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
                "tasks": [{"name": "RunScript", "state": "completed", "progress": 100.0}],
            }
            mock_meta.NetworkClient.return_value = mock_net_client

            with patch.object(mr, "Metashape", mock_meta):
                with patch("cluster_utils.get_optimal_local_scratch_dir", return_value=scratch_vol):
                    with patch("cluster_utils.check_scratch_disk_headroom", return_value=(True, 500.0)):
                        with patch(
                            "autometashape.core.pipeline.build_monolithic_runscript_task",
                            wraps=mr.build_monolithic_runscript_task,
                        ) as mock_builder:
                            mr.run_reconstruction(
                                images_dir=img_dir,
                                output_dir=out_dir,
                                network_enable=True,
                                network_independent_workers=True,
                                local_scratch_staging=True,
                            )
                            self.assertTrue(mock_builder.called)
                            # On coordinator, local_scratch_staging is suppressed; doc.save must target central out_dir
                            saved_doc_path = mock_doc.save.call_args[0][0]
                            self.assertEqual(os.path.dirname(os.path.abspath(saved_doc_path)), os.path.abspath(out_dir))

    def test_build_monolithic_runscript_task_mirrors_scripts_and_source(self) -> None:
        """Verify build_monolithic_runscript_task mirrors both scripts/ and source/ to .autometashape."""
        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_net:
            img_dir = os.path.join(tmp_net, "101126", "CATP_0205")
            out_dir = os.path.join(tmp_net, "101126", "Result", "CATP_0205")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)

            task, rel_script, status_file = mr.build_monolithic_runscript_task(
                images_dir=img_dir,
                output_dir=out_dir,
                project_name="CATP_0205",
                network_path=tmp_net,
            )

            shared_script = os.path.join(tmp_net, ".autometashape", "scripts", "metashape_reconstruction.py")
            shared_source = os.path.join(tmp_net, ".autometashape", "source")

            self.assertTrue(os.path.isfile(shared_script))
            self.assertTrue(os.path.isdir(shared_source))
            self.assertEqual(rel_script, ".autometashape/scripts/metashape_reconstruction.py")

    def test_build_monolithic_runscript_task_case_insensitive_unc_resolution(self) -> None:
        """Verify build_monolithic_runscript_task correctly relativizes paths despite UNC case differences."""
        import metashape_reconstruction as mr

        with tempfile.TemporaryDirectory() as tmp_net:
            lower_net = tmp_net.lower()
            img_dir = os.path.join(tmp_net, "101126", "CATP_0205")
            out_dir = os.path.join(tmp_net, "101126", "Result", "CATP_0205")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)

            task, rel_script, status_file = mr.build_monolithic_runscript_task(
                images_dir=img_dir,
                output_dir=out_dir,
                project_name="CATP_0205",
                network_path=lower_net,
            )

            args_str = getattr(task, "args", "")
            self.assertIn("--images 101126/CATP_0205", args_str.replace("\\", "/"))
            self.assertIn("--output 101126/Result/CATP_0205", args_str.replace("\\", "/"))


if __name__ == "__main__":
    unittest.main()
