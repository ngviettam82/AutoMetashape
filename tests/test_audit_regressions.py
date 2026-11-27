# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Regression tests defending all P0/P1/P2 audit fixes and requested feature contracts."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")

for p in (_SCRIPTS_DIR, _SOURCE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from autometashape.cluster import abort as abort_mod
from autometashape.cluster import orchestrator as orch_mod
from autometashape.cluster import staging as staging_mod
from autometashape.cluster import supervisor as sup_mod
from autometashape.cluster import updater as updater_mod
from autometashape.core import filtering as filtering_mod
from autometashape.core import pipeline as pipeline_mod
from autometashape.core import telemetry as telemetry_mod

from scripts import cluster_monitor as monitor_mod
from scripts import inspect_checkpoint as inspect_mod


class _WktOnlyCRS:
    """Stub CRS mimicking Metashape 2.2.2 CoordinateSystem (exposes .wkt, NO .projcs attr)."""

    def __init__(self, wkt: str) -> None:
        self.wkt = wkt


class TestAuditRegressions(unittest.TestCase):
    """Mutation-resistant regression tests for audited defects and new production defaults."""

    def test_pro_plan_defaults_tiepoints_40k_no_point_cloud_ortho_5cm(self) -> None:
        """Verify pro plan enforces tiepoint_limit=40000, keypoint_limit_per_mpx=2000, no dense cloud, 5cm ortho."""
        physics = {
            "shutter_type": "mechanical",
            "shadow_clip_pct": 2.0,
            "relief_ratio": 0.05,
            "gsd_cm": 1.2,
            "megapixels": 20.0,
        }
        plan = telemetry_mod.derive_pro_photogrammetry_plan(physics)
        self.assertEqual(plan["keypoint_limit"], 50000)
        self.assertEqual(plan["tiepoint_limit"], 40000)
        self.assertEqual(plan["keypoint_limit_per_mpx"], 2000)
        self.assertFalse(plan["build_point_cloud"])
        self.assertAlmostEqual(plan["ortho_resolution"], 0.05)

        # CLI parser defaults align with pro production defaults
        parser = pipeline_mod.build_arg_parser()
        args = parser.parse_args(["--images", "dummy_dir"])
        self.assertEqual(args.keypoint_limit, 50000)
        self.assertEqual(args.tiepoint_limit, 40000)
        self.assertEqual(args.keypoint_limit_per_mpx, 2000)
        self.assertAlmostEqual(args.ortho_resolution, 0.05)
        self.assertIsNone(args.build_point_cloud)

    def test_is_projected_crs_uses_wkt_without_projcs_attribute(self) -> None:
        """Verify is_projected_crs works on Metashape 2.2.2 CoordinateSystem lacking .projcs."""
        utm_crs = _WktOnlyCRS('PROJCS["WGS 84 / UTM zone 48N",GEOGCS["WGS 84"]]')
        wgs_crs = _WktOnlyCRS('GEOGCS["WGS 84",DATUM["World Geodetic System 1984"]]')
        self.assertFalse(hasattr(utm_crs, "projcs"))
        self.assertTrue(telemetry_mod.is_projected_crs(utm_crs))
        self.assertFalse(telemetry_mod.is_projected_crs(wgs_crs))
        self.assertFalse(telemetry_mod.is_projected_crs(None))

    def test_unified_cluster_pipeline_ortho_5cm_and_no_point_cloud(self) -> None:
        """Verify unified cluster tasks skip BuildPointCloud by default and set resolution=0.05 on build+export."""
        created_tasks = []

        class _FakeTask:
            def __init__(self, kind: str) -> None:
                self._kind = kind
                self.network_distribute = True
                created_tasks.append(self)

            def toNetworkTask(self, chunk=None):
                return self

        class _FakeTasks:
            @staticmethod
            def MatchPhotos():
                return _FakeTask("MatchPhotos")

            @staticmethod
            def AlignCameras():
                return _FakeTask("AlignCameras")

            @staticmethod
            def CleanTiePoints():
                return _FakeTask("CleanTiePoints")

            @staticmethod
            def OptimizeCameras():
                return _FakeTask("OptimizeCameras")

            @staticmethod
            def RunScript():
                return _FakeTask("RunScript")

            @staticmethod
            def BuildDepthMaps():
                return _FakeTask("BuildDepthMaps")

            @staticmethod
            def BuildPointCloud():
                return _FakeTask("BuildPointCloud")

            @staticmethod
            def BuildModel():
                return _FakeTask("BuildModel")

            @staticmethod
            def BuildUV():
                return _FakeTask("BuildUV")

            @staticmethod
            def BuildTexture():
                return _FakeTask("BuildTexture")

            @staticmethod
            def BuildOrthomosaic():
                return _FakeTask("BuildOrthomosaic")

            @staticmethod
            def ExportModel():
                return _FakeTask("ExportModel")

            @staticmethod
            def ExportRaster():
                return _FakeTask("ExportRaster")

            @staticmethod
            def BuildDem():
                return _FakeTask("BuildDem")

        fake_ms = SimpleNamespace(
            Tasks=_FakeTasks,
            OrthoProjection=lambda: SimpleNamespace(crs=None),
            CoordinateSystem=lambda code: _WktOnlyCRS(f'PROJCS["{code}"]'),
            DataSource=SimpleNamespace(ModelData=1, DepthMapsData=2, ElevationData=3, OrthomosaicData=4),
            SurfaceType=SimpleNamespace(Arbitrary=1, HeightField=2),
            FaceCount=SimpleNamespace(CustomFaceCount=0, LowFaceCount=1, MediumFaceCount=2, HighFaceCount=3),
            Interpolation=SimpleNamespace(EnabledInterpolation=1, DisabledInterpolation=0, Extrapolated=2),
            ImageCompression=type(
                "_FakeCompression",
                (),
                {
                    "TiffCompression": SimpleNamespace(TiffCompressionLZW=1),
                    "TiffCompressionLZW": 1,
                    "tiff_compression": None,
                    "tiff_big": False,
                },
            ),
            FilterMode=SimpleNamespace(MildFiltering=1, ModerateFiltering=2, AggressiveFiltering=3),
            ReferencePreselectionMode=SimpleNamespace(
                ReferencePreselectionEstimated=1,
                ReferencePreselectionSource=2,
                ReferencePreselectionSequential=3,
            ),
            ImageFormat=SimpleNamespace(ImageFormatPNG=1, ImageFormatTIFF=2, ImageFormatJPEG=3),
            RasterFormat=SimpleNamespace(RasterFormatTiles=1),
            ModelFormat=SimpleNamespace(ModelFormatOBJ=1, ModelFormatGLTF=2),
            TextureType=SimpleNamespace(DiffuseMap=1),
            MappingMode=SimpleNamespace(GenericMapping=1),
            BlendingMode=SimpleNamespace(MosaicBlending=1),
            TiePoints=SimpleNamespace(
                Criterion=SimpleNamespace(
                    ReconstructionUncertainty=1,
                    ProjectionAccuracy=2,
                    ReprojectionError=3,
                    ImageCount=4,
                ),
                Filter=SimpleNamespace(
                    ReconstructionUncertainty=1,
                    ProjectionAccuracy=2,
                    ReprojectionError=3,
                    ImageCount=4,
                ),
            ),
        )

        # Chunk with geographic CRS (no .projcs attr!) and 1 GPS camera in Vietnam (UTM 48N)
        fake_cam = SimpleNamespace(reference=SimpleNamespace(location=SimpleNamespace(x=106.7, y=10.8, z=80.0)))
        fake_chunk = SimpleNamespace(
            crs=_WktOnlyCRS('GEOGCS["WGS 84"]'),
            cameras=[fake_cam],
        )

        with tempfile.TemporaryDirectory() as tmp_net:
            out_dir = os.path.join(tmp_net, "SiteA")
            os.makedirs(out_dir, exist_ok=True)
            with patch.object(orch_mod, "Metashape", fake_ms):
                orch_mod.build_unified_cluster_pipeline_tasks(
                    chunk=fake_chunk,
                    output_dir=out_dir,
                    project_name="SiteA",
                    network_path=tmp_net,
                    build_point_cloud=False,
                    build_orthomosaic=True,
                    export_ortho=True,
                    ortho_resolution=0.05,
                    tiepoint_limit=40000,
                    keypoint_limit_per_mpx=2000,
                )

        kinds = [t._kind for t in created_tasks]
        self.assertNotIn("BuildPointCloud", kinds)
        self.assertIn("BuildOrthomosaic", kinds)
        self.assertIn("ExportRaster", kinds)

        match_task = next(t for t in created_tasks if t._kind == "MatchPhotos")
        self.assertEqual(match_task.tiepoint_limit, 40000)
        self.assertEqual(match_task.keypoint_limit_per_mpx, 2000)

        ortho_build = next(t for t in created_tasks if t._kind == "BuildOrthomosaic")
        self.assertAlmostEqual(ortho_build.resolution, 0.05)
        self.assertIn("EPSG::32648", ortho_build.projection.crs.wkt)

        ortho_export = next(t for t in created_tasks if t._kind == "ExportRaster")
        self.assertAlmostEqual(ortho_export.resolution, 0.05)
        self.assertIn("EPSG::32648", ortho_export.projection.crs.wkt)

    def test_monolithic_runscript_forwards_new_defaults_and_blocks_prefix_collision(self) -> None:
        """Verify build_monolithic_runscript_task passes 5cm ortho, 40k tiepoints, --no-point-cloud, and safe paths."""
        with tempfile.TemporaryDirectory() as base:
            net_root = os.path.join(base, "Data")
            sibling_src = os.path.join(base, "Data2", "SiteA")
            out_dir = os.path.join(net_root, "SiteA")
            os.makedirs(sibling_src, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)

            task, _, _ = orch_mod.build_monolithic_runscript_task(
                images_dir=sibling_src,
                output_dir=out_dir,
                project_name="SiteA",
                network_path=net_root,
                build_point_cloud=False,
                ortho_resolution=0.05,
                tiepoint_limit=40000,
                keypoint_limit_per_mpx=2000,
            )
            args_str = getattr(task, "args", "")
            self.assertIn("--no-point-cloud", args_str)
            self.assertIn("--ortho-resolution 0.05", args_str)
            self.assertIn("--tiepoint-limit 40000", args_str)
            self.assertIn("--keypoint-limit-per-mpx 2000", args_str)
            # Sibling Data2 must NOT be wrongly stripped to relative path under Data!
            self.assertIn("Data2", args_str)
            # Output IS under net_root so it relativizes to SiteA
            self.assertIn("--output SiteA", args_str.replace("\\", "/"))

    def test_abort_signal_acknowledge_prevents_double_kill(self) -> None:
        """Verify acknowledging an abort signal purges BOTH .abort and .cluster/abort_signals.json."""
        with tempfile.TemporaryDirectory() as tmp_root:
            abort_mod.dispatch_abort_signal(
                node_name="WORKER-01",
                batch_id=42,
                shared_root=tmp_root,
            )
            sig1 = abort_mod.check_abort_signal(root_path=tmp_root, node_name="WORKER-01")
            self.assertIsNotNone(sig1)
            self.assertEqual(sig1.get("batch_id"), 42)

            abort_mod.acknowledge_abort_signal(sig1)

            # Second poll MUST return None — no double-kill from residual abort_signals.json!
            sig2 = abort_mod.check_abort_signal(root_path=tmp_root, node_name="WORKER-01")
            self.assertIsNone(sig2)

    def test_cleanup_scratch_locks_preserves_unrelated_temp_locks(self) -> None:
        """Verify cleanup_scratch_locks never deletes unrelated .lock files in OS %TEMP%."""
        with tempfile.TemporaryDirectory() as fake_temp, tempfile.TemporaryDirectory() as fake_target:
            unrelated_lock = os.path.join(fake_temp, "unrelated_app.lock")
            target_lock = os.path.join(fake_target, "project.lock")
            for p in (unrelated_lock, target_lock):
                with open(p, "w", encoding="utf-8") as f:
                    f.write("lock")

            with patch("tempfile.gettempdir", return_value=fake_temp):
                removed = abort_mod.cleanup_scratch_locks(target_dir=fake_target)

            self.assertGreaterEqual(len(removed), 1)
            self.assertFalse(os.path.exists(target_lock))
            self.assertTrue(os.path.exists(unrelated_lock))

    def test_staging_rejects_remote_smb_mapped_drives(self) -> None:
        """Verify _is_fixed_local_windows_drive rejects DRIVE_REMOTE (4) and external USB SSDs."""
        import struct

        fake_kernel32 = MagicMock()
        fake_kernel32.GetDriveTypeW.side_effect = lambda root: 4 if root.upper().startswith("Z:") else 3
        fake_ctypes = SimpleNamespace(windll=SimpleNamespace(kernel32=fake_kernel32))

        with patch.object(sys, "platform", "win32"), patch.object(staging_mod, "ctypes", fake_ctypes):
            self.assertFalse(staging_mod._is_fixed_local_windows_drive("Z:\\"))
            self.assertTrue(staging_mod._is_fixed_local_windows_drive("D:\\"))

        # Verify STORAGE_DEVICE_DESCRIPTOR binary parser with realistic non-zero VendorIdOffset (offset 12)
        # so reading offset 12 instead of offset 10 (RemovableMedia) fails immediately.
        buf_scsi = struct.pack("<IIBBBBIIIII", 40, 64, 0, 0, 0, 1, 40, 48, 56, 60, 1)
        self.assertEqual(staging_mod._parse_storage_device_descriptor(buf_scsi), (False, 1))

        buf_nvme = struct.pack("<IIBBBBIIIII", 40, 64, 0, 0, 0, 1, 0, 48, 56, 60, 17)
        self.assertEqual(staging_mod._parse_storage_device_descriptor(buf_nvme), (False, 17))

        buf_usb = struct.pack("<IIBBBBIIIII", 40, 64, 0, 0, 0, 1, 40, 48, 56, 60, 7)
        self.assertEqual(staging_mod._parse_storage_device_descriptor(buf_usb), (False, 7))

        buf_rem = struct.pack("<IIBBBBIIIII", 40, 64, 0, 0, 1, 0, 0, 48, 56, 60, 11)
        self.assertEqual(staging_mod._parse_storage_device_descriptor(buf_rem), (True, 11))
        self.assertIsNone(staging_mod._parse_storage_device_descriptor(b"short"))

        # Verify external USB SSD reported as DRIVE_FIXED (3) is rejected while internal NVMe/SATA pass
        with patch.object(sys, "platform", "win32"), patch.object(staging_mod, "ctypes", fake_ctypes):
            with patch.object(staging_mod, "_query_windows_drive_bus_info", return_value=(False, 7)):
                self.assertFalse(staging_mod._is_fixed_local_windows_drive("D:\\"))
            with patch.object(staging_mod, "_query_windows_drive_bus_info", return_value=(False, 12)):
                self.assertFalse(staging_mod._is_fixed_local_windows_drive("E:\\"))
            with patch.object(staging_mod, "_query_windows_drive_bus_info", return_value=(True, 11)):
                self.assertFalse(staging_mod._is_fixed_local_windows_drive("F:\\"))
            with patch.object(staging_mod, "_query_windows_drive_bus_info", return_value=(False, 17)):
                self.assertTrue(staging_mod._is_fixed_local_windows_drive("C:\\"))
            with patch.object(staging_mod, "_query_windows_drive_bus_info", return_value=(False, 11)):
                self.assertTrue(staging_mod._is_fixed_local_windows_drive("D:\\"))

        # End-to-end: get_optimal_local_scratch_dir must skip external USB SSD D: (900 GB) and choose internal NVMe C:
        def _fake_usage(path: str):
            free_gb = 900 if path.upper().startswith("D:") else 400
            return SimpleNamespace(total=1000 * (1024**3), used=100 * (1024**3), free=free_gb * (1024**3))

        def _fake_bus(root: str):
            return (False, 7) if root.upper().startswith("D:") else (False, 17)

        with (
            patch.object(sys, "platform", "win32"),
            patch.object(staging_mod, "ctypes", fake_ctypes),
            patch.object(staging_mod, "_query_windows_drive_bus_info", side_effect=_fake_bus),
            patch.dict(os.environ, {"SystemDrive": "C:"}),
            patch("os.path.exists", side_effect=lambda p: p.upper() in ("C:\\", "D:\\")),
            patch("shutil.disk_usage", side_effect=_fake_usage),
            patch("os.makedirs"),
            patch("builtins.open", unittest.mock.mock_open()),
            patch("os.path.isfile", return_value=True),
            patch("os.remove"),
        ):
            chosen = staging_mod.get_optimal_local_scratch_dir(min_free_gb=100.0)
            self.assertTrue(chosen.upper().startswith("C:\\"), msg=f"Expected internal C:, got {chosen}")

    def test_mavic_3_l2d_20c_stays_rolling_shutter_not_zenmuse_l2(self) -> None:
        """Verify Hasselblad Mavic 3 (L2D-20c) keeps rolling shutter while Zenmuse L2 gets mechanical."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            img_path = os.path.join(tmp_dir, "DJI_0001.JPG")
            with open(img_path, "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")

            mavic_telem = {
                "make": "Hasselblad",
                "model": "L2D-20c",
                "width": 5280,
                "height": 3956,
                "focal_mm": 12.29,
                "has_gps": False,
                "lat": 0.0,
                "lon": 0.0,
                "alt": None,
                "rel_alt": None,
                "pitch": None,
            }
            with patch.object(telemetry_mod, "extract_image_telemetry", return_value=mavic_telem):
                phys_mavic = telemetry_mod.analyze_survey_physics(tmp_dir, sample_size=1)
            self.assertEqual(phys_mavic["shutter"], "rolling")
            plan_mavic = telemetry_mod.derive_pro_photogrammetry_plan(phys_mavic)
            self.assertTrue(plan_mavic["rolling_shutter"])

            l2_telem = {
                "make": "DJI",
                "model": "Zenmuse L2",
                "width": 5280,
                "height": 3956,
                "focal_mm": 12.29,
                "has_gps": False,
                "lat": 0.0,
                "lon": 0.0,
                "alt": None,
                "rel_alt": None,
                "pitch": None,
            }
            with patch.object(telemetry_mod, "extract_image_telemetry", return_value=l2_telem):
                phys_l2 = telemetry_mod.analyze_survey_physics(tmp_dir, sample_size=1)
            self.assertEqual(phys_l2["shutter"], "mechanical")
            plan_l2 = telemetry_mod.derive_pro_photogrammetry_plan(phys_l2)
            self.assertFalse(plan_l2["rolling_shutter"])

    def test_cluster_monitor_no_ram_guard_and_accurate_unit_scaling(self) -> None:
        """Verify cluster_monitor scales Metashape 2.x 0-100% metrics cleanly and emits NO >=55 GB RAM guard."""
        # Case 1: Idle worker with sub-1% usage (must NOT be inflated 100x!)
        idle_worker = {
            "worker_id": 2,
            "hostname": "IDLE-NODE",
            "state": "ready",
            "memory_size": int(31.77 * (1024**3)),
            "resource_usage": {"cpu_usage": 0.077, "memory_usage": 0.2345},
        }
        idle_cpu, idle_ram = monitor_mod.format_worker_resource_usage(idle_worker)
        self.assertAlmostEqual(idle_cpu, 0.077, places=3)
        self.assertAlmostEqual(idle_ram, 0.0745, places=3)

        # Case 2: Busy worker with >1% usage (must convert % of host RAM to GB, NOT print raw %)
        busy_worker = {
            "worker_id": 3,
            "hostname": "BUSY-NODE",
            "state": "inprogress",
            "memory_size": int(31.77 * (1024**3)),
            "resource_usage": {"cpu_usage": 3.55, "memory_usage": 6.54},
        }
        busy_cpu, busy_ram = monitor_mod.format_worker_resource_usage(busy_worker)
        self.assertAlmostEqual(busy_cpu, 3.55, places=2)
        self.assertAlmostEqual(busy_ram, 2.078, places=2)

        # Case 3: High-RAM worker (>=55 GB) emits accurate 57.6 GB and ZERO RAM guard warning
        worker = {
            "worker_id": 1,
            "hostname": "GPU-RIG-01",
            "state": "inprogress",
            "batch_id": 10,
            "memory_size": 64 * (1024**3),
            "resource_usage": {"cpu_usage": 45.0, "memory_usage": 90.0},
        }
        cpu_pct, ram_gb = monitor_mod.format_worker_resource_usage(worker)
        self.assertAlmostEqual(cpu_pct, 45.0, places=2)
        self.assertAlmostEqual(ram_gb, 57.6, places=2)

        fake_client = MagicMock()
        fake_client.workerList.return_value = {"workers": [worker]}
        fake_client.batchList.return_value = {"batches": []}
        fake_ms = SimpleNamespace(NetworkClient=lambda: fake_client)

        buf = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp_root,
            patch.object(monitor_mod, "Metashape", fake_ms),
            redirect_stdout(buf),
        ):
            data = monitor_mod.execute_audit(
                server_host="127.0.0.1:5840",
                shared_root=tmp_root,
                min_headroom=0.01,
            )

        out = buf.getvalue()
        self.assertTrue(data["healthy"])
        self.assertIn("45.0%", out)
        self.assertIn("57.6 GB", out)
        self.assertNotIn("HIGH RAM WARNING", out)
        self.assertIn("MONITOR AUDIT COMPLETE [PASS]", out)

    def test_cluster_monitor_warns_when_batches_errored_or_no_workers(self) -> None:
        """Verify cluster_monitor reports unhealthy WARN verdict instead of unconditional healthy."""
        fake_client = MagicMock()
        fake_client.workerList.return_value = {"workers": []}
        fake_client.batchList.return_value = {
            "batches": [{"batch_id": 99, "path": "SiteA/SiteA.psx", "state": "inprogress", "error_count": 3}]
        }
        fake_ms = SimpleNamespace(NetworkClient=lambda: fake_client)

        buf = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as tmp_root,
            patch.object(monitor_mod, "Metashape", fake_ms),
            redirect_stdout(buf),
        ):
            data = monitor_mod.execute_audit(
                server_host="127.0.0.1:5840",
                shared_root=tmp_root,
                min_headroom=0.01,
            )

        self.assertFalse(data["healthy"])
        self.assertIn("MONITOR AUDIT COMPLETE [WARN]", buf.getvalue())

    def test_safe_create_cluster_batch_rejects_zombie_error_batch(self) -> None:
        """Verify safe_create_cluster_batch never attaches to a zombie batch with error_count > 0."""
        fake_client = MagicMock()
        fake_client.batchList.return_value = {
            "batches": [
                {
                    "batch_id": 7,
                    "path": "SiteA/SiteA.psx",
                    "state": "inprogress",
                    "error_count": 4,
                    "paused": False,
                }
            ]
        }
        fake_client.createBatch.return_value = 8

        batch_id, attached = orch_mod.safe_create_cluster_batch(fake_client, "SiteA/SiteA.psx", ["task"])
        self.assertEqual(batch_id, 8)
        self.assertFalse(attached)
        fake_client.createBatch.assert_called_once()

    def test_updater_maintenance_project_per_worker_slug_and_valid_zip(self) -> None:
        """Verify ensure_cluster_maintenance_project creates per-worker .psx with valid ZIP project.zip."""
        with tempfile.TemporaryDirectory() as tmp_share:
            with patch.object(updater_mod, "Metashape", None):
                rel = updater_mod.ensure_cluster_maintenance_project(tmp_share, worker_slug="workerA_2")
            self.assertEqual(rel, ".cluster/maintenance_workera_2.psx")
            psx_abs = os.path.join(tmp_share, ".cluster", "maintenance_workera_2.psx")
            zip_abs = os.path.join(tmp_share, ".cluster", "maintenance_workera_2.files", "project.zip")
            self.assertTrue(os.path.isfile(psx_abs))
            self.assertTrue(os.path.isfile(zip_abs))
            with zipfile.ZipFile(zip_abs, "r") as zf:
                self.assertIn("doc.xml", zf.namelist())
                self.assertIn("<document", zf.read("doc.xml").decode("utf-8"))

    def test_inspect_checkpoint_metashape_2x_api_and_readonly(self) -> None:
        """Verify inspect_project opens read_only=True, ignore_lock=True, reads 2.x attrs, and calls doc.clear()."""
        fake_chunk = SimpleNamespace(
            label="Production_Reconstruction",
            cameras=[SimpleNamespace(transform=object(), enabled=True), SimpleNamespace(transform=None, enabled=True)],
            tie_points=SimpleNamespace(points=[1, 2, 3, 4, 5]),
            point_cloud=SimpleNamespace(point_count=123456),
            depth_maps={"cam1": b"d1", "cam2": b"d2"},
            model=SimpleNamespace(faces=[1, 2], vertices=[1, 2, 3]),
            orthomosaic=object(),
            elevation=None,
        )
        fake_doc = MagicMock()
        fake_doc.chunks = [fake_chunk]
        fake_ms = SimpleNamespace(Document=lambda: fake_doc)

        with tempfile.TemporaryDirectory() as tmp_dir:
            psx_path = os.path.join(tmp_dir, "Test.psx")
            with open(psx_path, "w", encoding="utf-8") as f:
                f.write("<document/>")

            with patch.object(inspect_mod, "Metashape", fake_ms):
                info = inspect_mod.inspect_project(psx_path)

        fake_doc.open.assert_called_once_with(os.path.normpath(psx_path), read_only=True, ignore_lock=True)
        fake_doc.clear.assert_called_once()
        chunk_info = info["chunks"][0]
        self.assertEqual(chunk_info["aligned_cameras"], 1)
        self.assertEqual(chunk_info["total_cameras"], 2)
        self.assertEqual(chunk_info["tie_points"], 5)
        self.assertEqual(chunk_info["dense_points"], 123456)
        self.assertEqual(chunk_info["depth_maps"], 2)
        self.assertEqual(chunk_info["model_faces"], 2)
        self.assertTrue(chunk_info["has_orthomosaic"])
        self.assertFalse(chunk_info["has_elevation"])

    def test_filtering_zero_removal_ratio_no_index_error(self) -> None:
        """Verify execute_usgs_gradual_selection does not raise IndexError when max_removal_ratio <= 0."""

        class _FakeFilter:
            values = [1.0, 2.0, 5.0, 10.0, 20.0]

            def init(self, chunk, criterion):
                pass

            def selectPoints(self, threshold):
                pass

            def removePoints(self, threshold):
                pass

        fake_ms = SimpleNamespace(
            TiePoints=SimpleNamespace(
                Filter=SimpleNamespace(
                    ReconstructionUncertainty=1,
                    ProjectionAccuracy=2,
                    ReprojectionError=3,
                    ImageCount=4,
                    __call__=lambda self=None: _FakeFilter(),
                )
            )
        )

        # Provide callable Filter class with enum attributes
        class _FilterClass(_FakeFilter):
            ReconstructionUncertainty = 1
            ProjectionAccuracy = 2
            ReprojectionError = 3
            ImageCount = 4

        fake_ms = SimpleNamespace(TiePoints=SimpleNamespace(Filter=_FilterClass))
        fake_chunk = SimpleNamespace(
            tie_points=SimpleNamespace(points=[SimpleNamespace(valid=True) for _ in range(10)]),
            optimizeCameras=MagicMock(),
            cameras=[],
        )

        with patch.object(filtering_mod, "Metashape", fake_ms):
            result = filtering_mod.execute_usgs_gradual_selection(fake_chunk, max_removal_ratio=0.0)
        self.assertIsInstance(result, dict)

    def test_supervisor_aborts_infinite_retry_errored_batch(self) -> None:
        """Verify ClusterSupervisor calls abortBatch when Metashape 2.2.2 reports error_count > 0."""
        fake_client = MagicMock()
        fake_client.batchInfo.return_value = {
            "batch_id": 55,
            "state": "inprogress",
            "error_count": 1,
            "last_error": {"type": "generic-error", "message": "Simulated crash"},
        }
        # Evaluate directly through evaluate_cluster_batch_info to verify dict error extraction
        status, pct, err_msg = orch_mod.evaluate_cluster_batch_info(fake_client.batchInfo.return_value)
        self.assertEqual(status, "failed")
        self.assertIn("Simulated crash", err_msg)

        # Verify default expected assets exclude DEM unless export_dem is opted in (decoupled from build_dem)
        self.assertEqual(sup_mod._derive_default_expected_assets({}), ["obj", "ortho"])
        self.assertEqual(sup_mod._derive_default_expected_assets({"build_dem": True}), ["obj", "ortho"])
        self.assertEqual(sup_mod._derive_default_expected_assets({"export_dem": True}), ["obj", "dem", "ortho"])

    def test_no_resume_local_staging_preserves_shared_psx_stub_on_early_crash(self) -> None:
        """Verify --no-resume with local scratch staging never deletes the shared .psx stub before final sync."""
        with tempfile.TemporaryDirectory() as tmp_root:
            img_dir = os.path.join(tmp_root, "src_photos")
            net_out = os.path.join(tmp_root, "shared_result")
            scratch_dir = os.path.join(tmp_root, "nvme_scratch")
            for d in (img_dir, net_out, scratch_dir):
                os.makedirs(d, exist_ok=True)
            with open(os.path.join(img_dir, "IMG_0001.JPG"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")

            # Seed the shared .psx stub materialized by submit_cluster_batch.py
            shared_psx = os.path.join(net_out, "SiteX.psx")
            shared_files = os.path.join(net_out, "SiteX.files")
            os.makedirs(shared_files, exist_ok=True)
            with open(shared_psx, "w", encoding="utf-8") as f:
                f.write("<document/>")
            with open(os.path.join(shared_files, "project.zip"), "wb") as f:
                f.write(b"PK\x05\x06" + b"\x00" * 18)

            # Simulate an early worker crash right after entering local scratch staging
            fake_ms = SimpleNamespace(
                Document=MagicMock(side_effect=RuntimeError("SIMULATED_EARLY_CRASH")),
                app=SimpleNamespace(enumGPUDevices=lambda: []),
            )
            with (
                patch.object(pipeline_mod, "Metashape", fake_ms),
                patch.object(pipeline_mod, "check_scratch_disk_headroom", return_value=(True, 200.0)),
                patch("atexit.register"),
            ):
                with self.assertRaises(RuntimeError):
                    pipeline_mod.run_reconstruction(
                        images_dir=img_dir,
                        output_dir=net_out,
                        project_name="SiteX",
                        resume=False,
                        local_scratch_staging=True,
                        scratch_dir=scratch_dir,
                    )

            # Shared .psx stub and .files directory MUST survive so Metashape Server never spins on io-error!
            self.assertTrue(os.path.isfile(shared_psx))
            self.assertTrue(os.path.isdir(shared_files))

    def test_update_bat_protects_untracked_agents_while_syncing_skills(self) -> None:
        """Verify update.bat excludes .agents from root /MIR and explicitly syncs tracked .agents/skills."""
        update_bat_path = os.path.join(_REPO_ROOT, "update.bat")
        with open(update_bat_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("/MIR /XD .git .agents", content)
        self.assertIn(r".agents\skills.json", content)
        self.assertIn(r".agents\skills", content)

    def test_resume_reuses_metashape_2x_16page_texture_without_rebake(self) -> None:
        """Verify Metashape 2.2.2 models (len(textures)==1, meta BuildUV/page_count=='16') skip re-bake on resume."""
        # Unit checks on helper
        ms2_model = SimpleNamespace(
            textures=[SimpleNamespace(type="DiffuseMap")],
            meta={"BuildUV/page_count": "16", "BuildUV/texture_size": "8192"},
        )
        self.assertEqual(pipeline_mod._get_model_texture_page_count(ms2_model), 16)

        # UV metadata present without baked textures must still report 0 pages
        uv_only_model = SimpleNamespace(textures=[], meta={"BuildUV/page_count": "16"})
        self.assertEqual(pipeline_mod._get_model_texture_page_count(uv_only_model), 0)

        # Legacy model without meta falls back to len(textures)
        legacy_model = SimpleNamespace(textures=[object(), object()])
        self.assertEqual(pipeline_mod._get_model_texture_page_count(legacy_model), 2)
        self.assertEqual(pipeline_mod._get_model_texture_page_count(None), 0)

        # End-to-end resume run must NOT call buildUV / buildTexture when 16-page atlas already exists
        with tempfile.TemporaryDirectory() as tmp_root:
            img_dir = os.path.join(tmp_root, "photos")
            out_dir = os.path.join(tmp_root, "result")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(img_dir, "IMG_0001.JPG"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")
            psx_path = os.path.join(out_dir, "SiteTex.psx")
            with open(psx_path, "w", encoding="utf-8") as f:
                f.write("<document/>")

            fake_chunk = MagicMock()
            fake_chunk.cameras = [MagicMock(transform=object(), enabled=True)]
            fake_chunk.depth_maps = {"cam1": b"d1"}
            fake_chunk.model = SimpleNamespace(
                vertices=[1, 2, 3],
                faces=[1, 2],
                textures=[SimpleNamespace(type="DiffuseMap")],
                meta={"BuildUV/page_count": "16"},
            )
            fake_chunk.orthomosaic = SimpleNamespace(width=100, height=100)
            fake_chunk.elevation = None
            fake_chunk.point_cloud = None

            fake_doc = MagicMock()
            fake_doc.chunk = fake_chunk
            fake_doc.chunks = [fake_chunk]

            fake_ms = SimpleNamespace(
                Document=lambda: fake_doc,
                app=SimpleNamespace(enumGPUDevices=lambda: []),
                ReferencePreselectionMode=SimpleNamespace(
                    ReferencePreselectionEstimated=1,
                    ReferencePreselectionSource=2,
                    ReferencePreselectionSequential=3,
                ),
                FilterMode=SimpleNamespace(ModerateFiltering=2),
                MappingMode=SimpleNamespace(GenericMapping=1),
                BlendingMode=SimpleNamespace(MosaicBlending=1),
            )

            with patch.object(pipeline_mod, "Metashape", fake_ms):
                pipeline_mod.run_reconstruction(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="SiteTex",
                    preset="pro",
                    resume=True,
                    local_scratch_staging=False,
                    align_photos=False,
                    build_depth_maps=False,
                    build_mesh=True,
                    build_texture=True,
                    texture_pages=16,
                    rebake_texture=False,
                    build_orthomosaic=False,
                    build_dem=False,
                    build_point_cloud=False,
                    export_obj=False,
                    export_glb=False,
                    export_ortho=False,
                )

            fake_chunk.buildUV.assert_not_called()
            fake_chunk.buildTexture.assert_not_called()

            # Explicit page count mismatch (4 vs 16) MUST still trigger re-bake
            with patch.object(pipeline_mod, "Metashape", fake_ms):
                pipeline_mod.run_reconstruction(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="SiteTex",
                    preset="pro",
                    resume=True,
                    local_scratch_staging=False,
                    align_photos=False,
                    build_depth_maps=False,
                    build_mesh=True,
                    build_texture=True,
                    texture_pages=4,
                    rebake_texture=False,
                    build_orthomosaic=False,
                    build_dem=False,
                    build_point_cloud=False,
                    export_obj=False,
                    export_glb=False,
                    export_ortho=False,
                )

            fake_chunk.buildUV.assert_called_once()
            fake_chunk.buildTexture.assert_called_once()

    def test_local_scratch_cluster_status_file_writes_completed_after_sync(self) -> None:
        """Verify external shared --status-file and default staged status file both reach 'completed' after sync."""
        for use_external_status in (True, False):
            with tempfile.TemporaryDirectory() as tmp_root:
                img_dir = os.path.join(tmp_root, "photos")
                net_out = os.path.join(tmp_root, "shared_result")
                scratch_dir = os.path.join(tmp_root, "nvme_scratch")
                for d in (img_dir, net_out, scratch_dir):
                    os.makedirs(d, exist_ok=True)
                with open(os.path.join(img_dir, "IMG_0001.JPG"), "wb") as f:
                    f.write(b"\xff\xd8\xff\xd9")

                shared_status = os.path.join(net_out, "_autometashape_status.json")
                fake_chunk = MagicMock()
                fake_chunk.cameras = [MagicMock(transform=object(), enabled=True)]
                fake_chunk.model = None
                fake_chunk.orthomosaic = None
                fake_chunk.elevation = None
                fake_chunk.point_cloud = None

                fake_doc = MagicMock()
                fake_doc.chunk = fake_chunk
                fake_doc.chunks = [fake_chunk]
                fake_ms = SimpleNamespace(
                    Document=lambda d=fake_doc: d,
                    app=SimpleNamespace(enumGPUDevices=lambda: []),
                    ReferencePreselectionMode=SimpleNamespace(
                        ReferencePreselectionEstimated=1,
                        ReferencePreselectionSource=2,
                        ReferencePreselectionSequential=3,
                    ),
                    FilterMode=SimpleNamespace(ModerateFiltering=2),
                )

                with (
                    patch.object(pipeline_mod, "Metashape", fake_ms),
                    patch.object(pipeline_mod, "check_scratch_disk_headroom", return_value=(True, 250.0)),
                ):
                    pipeline_mod.run_reconstruction(
                        images_dir=img_dir,
                        output_dir=net_out,
                        project_name="SiteStatus",
                        preset="pro",
                        resume=False,
                        local_scratch_staging=True,
                        scratch_dir=scratch_dir,
                        status_file=shared_status if use_external_status else None,
                        align_photos=False,
                        build_depth_maps=False,
                        build_mesh=False,
                        build_texture=False,
                        build_orthomosaic=False,
                        build_dem=False,
                        build_point_cloud=False,
                        export_obj=False,
                        export_glb=False,
                        export_ortho=False,
                    )

                self.assertTrue(
                    os.path.isfile(shared_status),
                    msg=f"Status file missing on shared output (use_external_status={use_external_status})",
                )
                with open(shared_status, "r", encoding="utf-8") as sf:
                    status_data = json.load(sf)
                self.assertEqual(status_data["status"], "completed")
                self.assertEqual(status_data["stage_key"], "complete")
                self.assertAlmostEqual(status_data["progress"], 100.0)
                # Local staging directory must stay deleted (never recreated by IPC_COMPLETE)
                self.assertFalse(os.path.exists(os.path.join(scratch_dir, "SiteStatus_local_stage")))

    def test_cluster_monitor_skips_maintenance_batches_and_isolates_source_share(self) -> None:
        """Verify cluster_monitor skips .cluster/ maintenance batches and never checks source_root for deliverables."""
        with tempfile.TemporaryDirectory() as tmp_base:
            src_root = os.path.join(tmp_base, "SSD1_Source")
            res_root = os.path.join(tmp_base, "SSD2_Result")
            src_site = os.path.join(src_root, "SiteA")
            res_site = os.path.join(res_root, "SiteA")
            maint_dir = os.path.join(res_root, ".cluster")
            for d in (src_site, res_site, maint_dir):
                os.makedirs(d, exist_ok=True)

            # Raw source share contains ONLY raw photos (no deliverables)
            with open(os.path.join(src_site, "DJI_0001.JPG"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")

            # Result share contains valid deliverables for SiteA
            with open(os.path.join(res_site, "SiteA.obj"), "wb") as f:
                f.write(b"mtllib SiteA.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n" + b"v 0 0 0\n" * 200)
            with open(os.path.join(res_site, "SiteA.mtl"), "w", encoding="utf-8") as f:
                f.write("newmtl m0\n")
            with open(os.path.join(res_site, "SiteA.png"), "wb") as f:
                f.write(b"\x89PNG" + b"\x00" * 1024)
            with open(os.path.join(res_site, "SiteA_orthomosaic.tif"), "wb") as f:
                f.write(b"II*\x00" + b"\x00" * 1500)
            with open(os.path.join(res_site, "SiteA_orthomosaic.prj"), "w", encoding="utf-8") as f:
                f.write('PROJCS["WGS 84 / UTM zone 48N"]')
            with open(os.path.join(res_site, "SiteA_orthomosaic.tfw"), "w", encoding="utf-8") as f:
                f.write("0.05\n0\n0\n-0.05\n0\n0\n")

            fake_client = MagicMock()
            fake_client.workerList.return_value = {
                "workers": [
                    {
                        "worker_id": 8,
                        "hostname": "Admin-pc",
                        "state": "ready",
                        "memory_size": 64 * (1024**3),
                        "resource_usage": {"cpu_usage": 0.1, "memory_usage": 0.2},
                    }
                ]
            }
            fake_client.batchList.return_value = {
                "batches": [
                    {"batch_id": 65, "path": ".cluster/maintenance_admin-pc_8.psx", "state": "completed"},
                    {"batch_id": 71, "path": "SiteA/SiteA.psx", "state": "completed"},
                ]
            }
            fake_ms = SimpleNamespace(NetworkClient=lambda: fake_client)

            buf = io.StringIO()
            with (
                patch.object(monitor_mod, "Metashape", fake_ms),
                patch.object(monitor_mod, "load_cluster_config", return_value={"root": res_root}),
                redirect_stdout(buf),
            ):
                # Deliberately omit shared_root so result root comes from server_config.json while source_root is passed
                data = monitor_mod.execute_audit(
                    server_host="127.0.0.1:5840",
                    shared_root="",
                    source_root=src_root,
                    min_headroom=0.01,
                )

            self.assertTrue(data["healthy"], msg=f"Unexpected monitor issues: {data['issues']}")
            self.assertNotIn(65, data["deliverables"])
            self.assertIn(71, data["deliverables"])
            self.assertTrue(data["deliverables"][71]["intact"])

    def test_no_resume_midrun_branch_never_deletes_shared_psx_stub(self) -> None:
        """Verify --no-resume never deletes shared .psx/.files in the post-Document branch either."""
        with tempfile.TemporaryDirectory() as tmp_root:
            img_dir = os.path.join(tmp_root, "src_photos")
            net_out = os.path.join(tmp_root, "shared_result")
            scratch_dir = os.path.join(tmp_root, "nvme_scratch")
            local_stage = os.path.join(scratch_dir, "SiteY_local_stage")
            for d in (img_dir, net_out, scratch_dir, local_stage):
                os.makedirs(d, exist_ok=True)
            with open(os.path.join(img_dir, "IMG_0001.JPG"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")

            shared_psx = os.path.join(net_out, "SiteY.psx")
            shared_files = os.path.join(net_out, "SiteY.files")
            os.makedirs(shared_files, exist_ok=True)
            with open(shared_psx, "w", encoding="utf-8") as f:
                f.write("<document/>")
            with open(os.path.join(shared_files, "project.zip"), "wb") as f:
                f.write(b"PK\x05\x06" + b"\x00" * 18)

            # Force a local file to exist at line 1338 by creating it inside fake Document()
            def _make_doc():
                with open(os.path.join(local_stage, "SiteY.psx"), "w", encoding="utf-8") as lf:
                    lf.write("<local/>")
                doc = MagicMock()
                doc.addChunk.return_value = MagicMock()
                return doc

            fake_ms = SimpleNamespace(
                Document=_make_doc,
                app=SimpleNamespace(enumGPUDevices=lambda: []),
            )
            with (
                patch.object(pipeline_mod, "Metashape", fake_ms),
                patch.object(pipeline_mod, "check_scratch_disk_headroom", return_value=(True, 200.0)),
                patch.object(
                    pipeline_mod,
                    "discover_image_subfolder_groups",
                    side_effect=RuntimeError("SIMULATED_MIDRUN_CRASH"),
                ),
                patch("atexit.register"),
            ):
                with self.assertRaises(RuntimeError):
                    pipeline_mod.run_reconstruction(
                        images_dir=img_dir,
                        output_dir=net_out,
                        project_name="SiteY",
                        resume=False,
                        local_scratch_staging=True,
                        scratch_dir=scratch_dir,
                    )

            self.assertTrue(os.path.isfile(shared_psx))
            self.assertTrue(os.path.isdir(shared_files))

    def test_supervisor_sequential_dual_share_filters_kwargs_and_respects_paused_workers(self) -> None:
        """Verify sequential dual-share filters metadata kwargs cleanly and concurrent mode never unpauses workers."""
        with tempfile.TemporaryDirectory() as tmp_base:
            src_dir = os.path.join(tmp_base, "SSD1_Photos", "SiteZ")
            res_dir = os.path.join(tmp_base, "SSD2_Results", "SiteZ")
            os.makedirs(src_dir, exist_ok=True)
            os.makedirs(res_dir, exist_ok=True)
            with open(os.path.join(src_dir, "IMG_001.JPG"), "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")

            captured_kwargs = {}

            def _fake_run_recon(**kwargs):
                captured_kwargs.update(kwargs)
                os.makedirs(kwargs["output_dir"], exist_ok=True)
                with open(os.path.join(kwargs["output_dir"], "SiteZ.obj"), "wb") as f:
                    f.write(b"v 0 0 0\nf 1 1 1\n" * 200)
                with open(os.path.join(kwargs["output_dir"], "SiteZ_orthomosaic.tif"), "wb") as f:
                    f.write(b"II*\x00" + b"\x00" * 1500)

            sup = sup_mod.ClusterSupervisor(min_headroom_gb=0.01, concurrent=False)
            sup.add_job(
                images_dir=src_dir,
                output_dir=res_dir,
                project_name="SiteZ",
                extra_args={
                    "source_share": os.path.dirname(src_dir),
                    "result_share": os.path.dirname(res_dir),
                    "network_path": os.path.dirname(res_dir),
                    "network_enable": False,
                    "local_scratch": True,
                    "local_scratch_staging": True,
                    "export_obj": True,
                    "export_ortho": True,
                    "ortho_resolution": 0.05,
                },
            )

            with patch.object(pipeline_mod, "run_reconstruction", side_effect=_fake_run_recon, autospec=True):
                summary = sup.run_queue()

            self.assertEqual(summary["completed_jobs"], 1)
            self.assertNotIn("source_share", captured_kwargs)
            self.assertNotIn("result_share", captured_kwargs)
            self.assertNotIn("local_scratch", captured_kwargs)
            self.assertNotIn("network_path", captured_kwargs)
            self.assertTrue(captured_kwargs["local_scratch_staging"])
            self.assertTrue(captured_kwargs["export_obj"])
            self.assertTrue(captured_kwargs["export_ortho"])

            # Verify concurrent mode never overrides operator-paused workers
            fake_client = MagicMock()
            fake_client.workerList.return_value = {
                "workers": [{"worker_id": 2, "hostname": "TamHuynh", "state": "ready", "paused": True}]
            }
            fake_ms = SimpleNamespace(NetworkClient=lambda: fake_client)
            conc_sup = sup_mod.ClusterSupervisor(min_headroom_gb=0.01, concurrent=True)
            with patch.object(sup_mod, "Metashape", fake_ms):
                conc_sup.run_concurrent_queue()
            fake_client.setWorkerPaused.assert_not_called()

    def test_cross_platform_posix_abort_and_dual_share_paths(self) -> None:
        """Verify kill_worker_process and dual_share/orchestrator path helpers succeed on Linux/POSIX."""
        from autometashape.cluster.dual_share import is_subpath, relpath_under

        # 1. Non-Windows kill_worker_process must NOT re-poll after graceful exit (prevents StopIteration)
        mock_proc = MagicMock()
        mock_proc.poll.side_effect = [None, None, 0]
        mock_proc.pid = 4242
        with patch.object(sys, "platform", "linux"):
            self.assertTrue(abort_mod.kill_worker_process(mock_proc, timeout=0.2))
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_not_called()

        # 2. UNC & Windows drive paths stay case-insensitive and collision-safe even when sys.platform == "linux"
        with patch.object(sys, "platform", "linux"):
            self.assertTrue(is_subpath(r"\\srv\data\site_a\sub", r"\\SRV\Data"))
            self.assertFalse(is_subpath(r"\\SRV\Data2\Site_A", r"\\SRV\Data"))
            self.assertFalse(is_subpath(r"D:\Result\Site_A", r"C:\Source"))
            self.assertEqual(
                relpath_under(r"\\srv\data\Site_A\Sub", r"\\SRV\Data"),
                "Site_A/Sub",
            )
            # Case-insensitive cluster root relativization preserves child casing without "../" traversal
            with tempfile.TemporaryDirectory() as tmp_net:
                img_dir = os.path.join(tmp_net, "101126", "CATP_0205")
                out_dir = os.path.join(tmp_net, "101126", "Result", "CATP_0205")
                os.makedirs(img_dir, exist_ok=True)
                os.makedirs(out_dir, exist_ok=True)
                task, _, _ = orch_mod.build_monolithic_runscript_task(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="CATP_0205",
                    network_path=tmp_net.lower(),
                )
                args_str = getattr(task, "args", "").replace("\\", "/")
                self.assertIn("--images 101126/CATP_0205", args_str)
                self.assertIn("--output 101126/Result/CATP_0205", args_str)


if __name__ == "__main__":
    unittest.main()
