# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Hermetic adversarial regression tests for Wave C telemetry, multi-chunk, resume, and export hardening."""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)

from autometashape.core import export as export_mod
from autometashape.core import pipeline as pipeline_mod
from autometashape.core import telemetry as telemetry_mod


class _WktCrs:
    """Minimal CRS stub matching Metashape CoordinateSystem (exposes .wkt without .projcs)."""

    def __init__(self, wkt: str) -> None:
        self.wkt = wkt
        self.name = wkt


class TestWaveCPipelineHardening(unittest.TestCase):
    """Mutation-resistant tests defending Wave C telemetry, pipeline, and export fixes."""

    def test_discover_skips_appledouble_hidden_and_deduplicates_raw_jpeg_pairs(self) -> None:
        """AppleDouble/hidden files are skipped and co-located RAW sidecars defer to raster siblings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            mission_dir = os.path.join(tmpdir, "Mission_A")
            hidden_dir = os.path.join(tmpdir, ".Trash")
            os.makedirs(mission_dir, exist_ok=True)
            os.makedirs(hidden_dir, exist_ok=True)

            files_to_create = [
                os.path.join(mission_dir, "._IMG_0001.JPG"),
                os.path.join(mission_dir, ".hidden_preview.png"),
                os.path.join(mission_dir, "IMG_0001.JPG"),
                os.path.join(mission_dir, "IMG_0001.DNG"),
                os.path.join(mission_dir, "IMG_0002.DNG"),
                os.path.join(tmpdir, "._root_sidecar.JPG"),
                os.path.join(hidden_dir, "IMG_9999.JPG"),
            ]
            for fpath in files_to_create:
                with open(fpath, "wb") as fh:
                    fh.write(b"\xff\xd8\xff\xd9")

            with redirect_stdout(io.StringIO()):
                groups = telemetry_mod.discover_image_subfolder_groups(tmpdir)

            self.assertEqual(list(groups.keys()), ["Mission_A"])
            names = [os.path.basename(p) for p in groups["Mission_A"]]
            self.assertEqual(names, ["IMG_0001.JPG", "IMG_0002.DNG"])

            with redirect_stdout(io.StringIO()):
                groups_with_raw = telemetry_mod.discover_image_subfolder_groups(
                    tmpdir, deduplicate_raw_pairs=False
                )
            names_raw = [os.path.basename(p) for p in groups_with_raw["Mission_A"]]
            self.assertEqual(names_raw, ["IMG_0001.DNG", "IMG_0001.JPG", "IMG_0002.DNG"])

    def test_deduplicate_photo_groups_edge_hash_collision_requires_full_sha256(self) -> None:
        """Files sharing identical name, size, head, middle, and tail slices but differing inside stay distinct."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sub1 = os.path.join(tmpdir, "Flight_1")
            sub2 = os.path.join(tmpdir, "Flight_2")
            sub3 = os.path.join(tmpdir, "Flight_Backup")
            for d in (sub1, sub2, sub3):
                os.makedirs(d, exist_ok=True)

            base_payload = bytearray(b"\x11" * 32768)
            other_payload = bytearray(base_payload)
            other_payload[8192] = 0x99  # Outside head (0:4096), middle (14336:18432), tail (28672:32768)

            p1 = os.path.join(sub1, "DJI_0001.JPG")
            p2 = os.path.join(sub2, "DJI_0001.JPG")
            p3 = os.path.join(sub3, "DJI_0001.JPG")
            with open(p1, "wb") as f1, open(p2, "wb") as f2, open(p3, "wb") as f3:
                f1.write(base_payload)
                f2.write(other_payload)
                f3.write(base_payload)

            groups = {"Flight_1": [p1], "Flight_2": [p2], "Flight_Backup": [p3]}
            with redirect_stdout(io.StringIO()):
                deduped, removed = telemetry_mod.deduplicate_photo_groups(groups)

            self.assertEqual(removed, 1)
            self.assertIn("Flight_1", deduped)
            self.assertIn("Flight_2", deduped)
            self.assertNotIn("Flight_Backup", deduped)

    def test_parse_xmp_telemetry_reads_tail_and_straddling_xmp(self) -> None:
        """XMP packets stored in file tails (>256 KB) or straddling 256 KB parse accurately."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tail_img = os.path.join(tmpdir, "tail_meta.dng")
            tail_xmp = (
                b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF>'
                b'<rdf:Description drone-dji:RelativeAltitude="+48.5" '
                b'drone-dji:ShutterType="Electronic" '
                b'drone-dji:GimbalPitchDegree="-35.0"/></rdf:RDF></x:xmpmeta>'
            )
            with open(tail_img, "wb") as fh:
                fh.write(b"\x00" * 290_000)
                fh.write(tail_xmp)
                fh.write(b"\x00" * 512)

            res_tail = telemetry_mod.parse_xmp_telemetry(tail_img)
            self.assertAlmostEqual(res_tail["relative_alt"], 48.5)
            self.assertEqual(res_tail["shutter_type"], "electronic")
            self.assertAlmostEqual(res_tail["gimbal_pitch"], -35.0)

            straddle_img = os.path.join(tmpdir, "straddle.dng")
            straddle_xmp = (
                b'<x:xmpmeta xmlns:x="adobe:ns:meta/">'
                b'RelativeAltitude="+62.25" ShutterType="Mechanical" '
                b'GimbalPitchDegree="-90.0"</x:xmpmeta>'
            )
            with open(straddle_img, "wb") as fh:
                fh.write(b"\xff" * 262_100)
                fh.write(straddle_xmp)

            res_straddle = telemetry_mod.parse_xmp_telemetry(straddle_img)
            self.assertAlmostEqual(res_straddle["relative_alt"], 62.25)
            self.assertEqual(res_straddle["shutter_type"], "mechanical")
            self.assertAlmostEqual(res_straddle["gimbal_pitch"], -90.0)

    def test_analyze_radiometry_normalizes_16bit_and_float_tiff(self) -> None:
        """16-bit integer and float TIFFs normalize cleanly without false 8-bit 255 clipping."""

        class _FakeThumb:
            def __init__(self, mode: str, pixels: list[float]) -> None:
                self.mode = mode
                self._pixels = pixels

            def resize(self, _size):
                return self

            def getextrema(self):
                return (min(self._pixels), max(self._pixels))

            def getdata(self):
                return self._pixels

            def convert(self, _mode: str):
                raise AssertionError("High-bit TIFFs must NOT call lossy .convert('L')")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as tmpdir:
            img16 = os.path.join(tmpdir, "midgray16.tif")
            img_hi16 = os.path.join(tmpdir, "clip16.tif")
            img_flt = os.path.join(tmpdir, "float01.tif")
            for p in (img16, img_hi16, img_flt):
                with open(p, "wb") as fh:
                    fh.write(b"II*\x00")

            thumb_map = {
                img16: _FakeThumb("I;16", [32768] * 1024),
                img_hi16: _FakeThumb("I;16", [64000] * 1024),
                img_flt: _FakeThumb("F", [0.5] * 1024),
            }
            fake_pil_image = SimpleNamespace(open=lambda path: thumb_map[path])
            fake_pil = SimpleNamespace(Image=fake_pil_image)

            with patch.dict(sys.modules, {"PIL": fake_pil, "PIL.Image": fake_pil_image}):
                rad16 = telemetry_mod.analyze_radiometry([img16], sample_count=1)
                self.assertAlmostEqual(rad16["shadow_clip_ratio"], 0.0)
                self.assertAlmostEqual(rad16["highlight_clip_ratio"], 0.0)

                rad_hi16 = telemetry_mod.analyze_radiometry([img_hi16], sample_count=1)
                self.assertAlmostEqual(rad_hi16["highlight_clip_ratio"], 1.0)

                rad_flt = telemetry_mod.analyze_radiometry([img_flt], sample_count=1)
                self.assertAlmostEqual(rad_flt["shadow_clip_ratio"], 0.0)
                self.assertAlmostEqual(rad_flt["highlight_clip_ratio"], 0.0)

    def test_mixed_fleet_per_sensor_rolling_shutter_and_explicit_override(self) -> None:
        """Mixed fleets enable Regularized per rolling sensor while honoring explicit CLI overrides."""
        fake_ms = SimpleNamespace(
            Shutter=SimpleNamespace(Model=SimpleNamespace(Disabled="Disabled", Regularized="Regularized"))
        )

        def _make_chunk():
            s_mech = SimpleNamespace(
                meta={"Exif/Make": "DJI", "Exif/Model": "M3E"},
                label="M3E",
                rolling_shutter="Disabled",
            )
            s_roll = SimpleNamespace(
                meta={"Exif/Make": "DJI", "Exif/Model": "Mini 4 Pro"},
                label="FC8482",
                rolling_shutter="Disabled",
            )
            c1 = SimpleNamespace(sensor=s_mech, photo=SimpleNamespace(meta=s_mech.meta, path=""))
            c2 = SimpleNamespace(sensor=s_roll, photo=SimpleNamespace(meta=s_roll.meta, path=""))
            return SimpleNamespace(sensors=[s_mech, s_roll], cameras=[c1, c2]), s_mech, s_roll

        with patch.object(pipeline_mod, "Metashape", fake_ms), redirect_stdout(io.StringIO()):
            # Auto inference (even when global majority was False): rolling gets Regularized, mech stays Disabled
            chunk_auto, s_mech, s_roll = _make_chunk()
            pipeline_mod._apply_rolling_shutter_to_chunk(chunk_auto, False, explicit_override=False)
            self.assertEqual(s_mech.rolling_shutter, "Disabled")
            self.assertEqual(s_roll.rolling_shutter, "Regularized")

            # Explicit --rolling-shutter forces Regularized across all sensors
            chunk_on, s_mech, s_roll = _make_chunk()
            pipeline_mod._apply_rolling_shutter_to_chunk(chunk_on, True, explicit_override=True)
            self.assertEqual(s_mech.rolling_shutter, "Regularized")
            self.assertEqual(s_roll.rolling_shutter, "Regularized")

            # Explicit --no-rolling-shutter forces Disabled across all sensors
            chunk_off, s_mech, s_roll = _make_chunk()
            s_roll.rolling_shutter = "Regularized"
            pipeline_mod._apply_rolling_shutter_to_chunk(chunk_off, False, explicit_override=True)
            self.assertEqual(s_mech.rolling_shutter, "Disabled")
            self.assertEqual(s_roll.rolling_shutter, "Disabled")

        with tempfile.TemporaryDirectory() as tmpdir:
            for name in ("01_M3E.JPG", "02_M3E.JPG", "03_MINI4.JPG"):
                with open(os.path.join(tmpdir, name), "wb") as fh:
                    fh.write(b"\xff\xd8\xff\xd9")

            def _fake_telem(path: str) -> dict:
                if "MINI4" in path:
                    return {
                        "make": "DJI",
                        "model": "Mini 4 Pro",
                        "width": 4000,
                        "height": 3000,
                        "focal_length": 6.72,
                        "focal_35mm": 24.0,
                        "has_gps": False,
                        "lat": None,
                        "lon": None,
                        "alt": None,
                    }
                return {
                    "make": "DJI",
                    "model": "M3E",
                    "width": 5280,
                    "height": 3956,
                    "focal_length": 12.29,
                    "focal_35mm": 24.0,
                    "has_gps": False,
                    "lat": None,
                    "lon": None,
                    "alt": None,
                }

            with (
                patch.object(telemetry_mod, "extract_image_telemetry", side_effect=_fake_telem),
                patch.object(telemetry_mod, "parse_xmp_telemetry", return_value={}),
            ):
                phys = telemetry_mod.analyze_survey_physics(tmpdir, sample_size=10)
            self.assertEqual(phys["shutter"], "mechanical")
            self.assertTrue(phys["has_mixed_shutters"])
            self.assertEqual(phys["sensor_shutters"], {"DJI M3E": "mechanical", "DJI Mini 4 Pro": "rolling"})

    def test_detect_markers_runs_before_import_reference(self) -> None:
        """detectMarkers must execute strictly before importReference so CSV GCPs bind to targets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            img_dir = os.path.join(tmpdir, "images")
            out_dir = os.path.join(tmpdir, "out")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            for name in ("IMG_0001.JPG", "IMG_0002.JPG"):
                with open(os.path.join(img_dir, name), "wb") as fh:
                    fh.write(b"\xff\xd8\xff\xd9")
            csv_path = os.path.join(tmpdir, "gcp.csv")
            with open(csv_path, "w", encoding="utf-8") as fh:
                fh.write("label,x,y,z\ntarget 1,106.7,10.8,5.0\n")

            call_order: list[str] = []
            chunk = MagicMock()
            chunk.cameras = []
            chunk.sensors = []
            chunk.markers = [SimpleNamespace(label="target 1")]
            chunk.model = None
            chunk.point_cloud = None
            chunk.elevation = None
            chunk.orthomosaic = None
            chunk.depth_maps = None
            chunk.detectMarkers.side_effect = lambda **_kw: call_order.append("detectMarkers")
            chunk.importReference.side_effect = lambda **_kw: call_order.append("importReference")

            doc = MagicMock()
            doc.chunk = chunk
            doc.chunks = [chunk]
            doc.addChunk.return_value = chunk

            fake_ms = SimpleNamespace(
                app=SimpleNamespace(
                    gpu_mask=0,
                    enumGPUDevices=lambda: [],
                    settings=SimpleNamespace(network_enable=False),
                ),
                Document=lambda: doc,
                ReferencePreselectionMode=SimpleNamespace(ReferencePreselectionEstimated=1),
                ReferenceFormat=SimpleNamespace(ReferenceFormatCSV=1),
                TargetType=SimpleNamespace(CircularTarget12bit=1),
            )
            with patch.object(pipeline_mod, "Metashape", fake_ms), redirect_stdout(io.StringIO()):
                pipeline_mod.run_reconstruction(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="MarkerOrder",
                    align_photos=False,
                    build_depth_maps=False,
                    build_mesh=False,
                    build_texture=False,
                    build_orthomosaic=False,
                    detect_markers=True,
                    import_reference=csv_path,
                    resume=False,
                    local_scratch_staging=False,
                    network_enable=False,
                )

            self.assertEqual(call_order, ["detectMarkers", "importReference"])

    def test_multichunk_anchor_z0_disjoint_align_and_skip_unaligned_merge(self) -> None:
        """Multi-chunk picks wide high-alt Z0 anchor, skips method=2 on disjoint GPS, skips unaligned merge."""
        with tempfile.TemporaryDirectory() as tmpdir:
            img_dir = os.path.join(tmpdir, "Survey")
            out_dir = os.path.join(tmpdir, "Out")
            for sub, count in (("00_small", 2), ("50_unaligned", 2), ("99_backbone", 4)):
                sdir = os.path.join(img_dir, sub)
                os.makedirs(sdir, exist_ok=True)
                for idx in range(count):
                    with open(os.path.join(sdir, f"{sub}_IMG_{idx:03d}.JPG"), "wb") as fh:
                        fh.write(b"\xff\xd8\xff\xe0" + f"{sub}_{idx}".encode("ascii"))
            os.makedirs(out_dir, exist_ok=True)

            class _MCChunk:
                def __init__(self, key: int) -> None:
                    self.key = key
                    self.label = ""
                    self.crs = _WktCrs('GEOGCS["WGS 84"]')
                    self.transform = SimpleNamespace(matrix=object())
                    self.cameras = []
                    self.sensors = []
                    self.markers = []
                    self.model = None
                    self.point_cloud = None
                    self.elevation = None
                    self.orthomosaic = None
                    self.depth_maps = None
                    self.tie_points = None
                    self.region = SimpleNamespace(center=None, size=None, rot=None)

                def addPhotos(self, paths):
                    coords_small = [(106.7000, 10.8000, 40.0), (106.7001, 10.8000, 40.0)]
                    coords_back = [
                        (106.7000, 10.8000, 120.0),
                        (106.7025, 10.8000, 120.0),
                        (106.7000, 10.8025, 120.0),
                        (106.7025, 10.8025, 120.0),
                    ]
                    coords_unaligned = [(106.7002, 10.8002, 50.0), (106.7003, 10.8002, 50.0)]
                    if "99_backbone" in self.label:
                        pts = coords_back
                        prefix = "back"
                    elif "50_unaligned" in self.label:
                        pts = coords_unaligned
                        prefix = "unal"
                    else:
                        pts = coords_small
                        prefix = "small"
                    for i, p in enumerate(paths):
                        lon, lat, alt = pts[i % len(pts)]
                        self.cameras.append(
                            SimpleNamespace(
                                key=self.key * 100 + i,
                                label=f"{prefix}_{i:02d}",
                                enabled=True,
                                transform=None,
                                center=SimpleNamespace(x=float(i), y=0.0, z=alt),
                                reference=SimpleNamespace(
                                    enabled=True,
                                    location=SimpleNamespace(x=lon, y=lat, z=alt),
                                ),
                                sensor=None,
                                meta={"Image/Quality": "0.9"},
                                photo=SimpleNamespace(path=p, meta={"Image/Quality": "0.9"}),
                            )
                        )

                def analyzeImages(self, **_kw):
                    pass

                def matchPhotos(self, **_kw):
                    pass

                def alignCameras(self, **_kw):
                    if "50_unaligned" in self.label:
                        return
                    for cam in self.cameras:
                        cam.transform = object()

                def updateTransform(self):
                    pass

            class _MCDoc:
                def __init__(self) -> None:
                    self.chunks: list[_MCChunk] = []
                    self.chunk = None
                    self._next_key = 0
                    self.seen_labels: dict[int, str] = {}
                    self.align_calls: list[dict] = []
                    self.merge_calls: list[dict] = []

                def addChunk(self) -> _MCChunk:
                    c = _MCChunk(self._next_key)
                    self._next_key += 1
                    self.chunks.append(c)
                    self.chunk = c
                    return c

                def remove(self, c):
                    if c.key in self.chunks or c in self.chunks:
                        if c.label:
                            self.seen_labels[c.key] = c.label
                    if c in self.chunks:
                        self.chunks.remove(c)

                def alignChunks(self, **kw):
                    self.align_calls.append(kw)

                def mergeChunks(self, **kw):
                    for c in self.chunks:
                        if c.label:
                            self.seen_labels[c.key] = c.label
                    self.merge_calls.append(kw)
                    merged = _MCChunk(self._next_key)
                    self._next_key += 1
                    for c in list(self.chunks):
                        if c.key in kw.get("chunks", []):
                            merged.cameras.extend(c.cameras)
                    self.chunks.append(merged)
                    self.chunk = merged

                def save(self, *_a, **_kw):
                    pass

                def clear(self):
                    pass

            mc_doc = _MCDoc()
            z0_calls: list[tuple[str, float | None]] = []

            def _track_z0(ch, anchor_z0_override=None):
                z0_calls.append((ch.label, anchor_z0_override))
                return {"anchor_z0": 120.0 if anchor_z0_override is None else anchor_z0_override}

            fake_ms = SimpleNamespace(
                app=SimpleNamespace(
                    gpu_mask=0,
                    enumGPUDevices=lambda: [],
                    settings=SimpleNamespace(network_enable=False),
                ),
                Document=lambda: mc_doc,
                ReferencePreselectionMode=SimpleNamespace(ReferencePreselectionEstimated=1),
                Shutter=SimpleNamespace(Model=SimpleNamespace(Disabled="Disabled", Regularized="Regularized")),
            )
            with (
                patch.object(pipeline_mod, "Metashape", fake_ms),
                patch.object(pipeline_mod, "normalize_flight_takeoff_z0", side_effect=_track_z0),
                patch.object(pipeline_mod, "sanitize_camera_gps"),
                redirect_stdout(io.StringIO()),
            ):
                pipeline_mod.run_reconstruction(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="MultiSite",
                    multi_chunk="force",
                    chunk_align_method="camera",
                    align_photos=True,
                    build_depth_maps=False,
                    build_mesh=False,
                    build_texture=False,
                    build_orthomosaic=False,
                    crop_method="none",
                    filter_tiepoints=False,
                    icp_bridge=False,
                    resume=False,
                    local_scratch_staging=False,
                    network_enable=False,
                )

            # Backbone folder (not alphabetical 00_small) must be Z0 anchor (override is None)
            self.assertEqual(len(z0_calls), 3)
            self.assertIn("99_backbone", z0_calls[0][0])
            self.assertIsNone(z0_calls[0][1])
            self.assertTrue(all(c[1] == 120.0 for c in z0_calls[1:]))

            # Disjoint georeferenced flights must NOT call alignChunks(method=2)
            self.assertEqual(mc_doc.align_calls, [])

            # Merge must include ONLY the 2 aligned chunks, excluding unaligned 50_unaligned
            unaligned_key = next(k for k, lbl in mc_doc.seen_labels.items() if "50_unaligned" in lbl)
            small_key = next(k for k, lbl in mc_doc.seen_labels.items() if "00_small" in lbl)
            back_key = next(k for k, lbl in mc_doc.seen_labels.items() if "99_backbone" in lbl)
            self.assertEqual(len(mc_doc.merge_calls), 1)
            merged_keys = mc_doc.merge_calls[0]["chunks"]
            self.assertEqual(set(merged_keys), {small_key, back_key})
            self.assertNotIn(unaligned_key, merged_keys)

        # Verify shared-station (method=2 >=3 shared photo identities), shared-marker (method=1), and georef checks
        shared_cam_a = SimpleNamespace(
            cameras=[
                SimpleNamespace(
                    label=f"CAM_0{i}",
                    photo=SimpleNamespace(path=f"/proj/Flight1/CAM_0{i}.JPG", meta={}),
                    reference=None,
                )
                for i in (1, 2, 3)
            ]
        )
        shared_cam_b = SimpleNamespace(
            cameras=[
                SimpleNamespace(
                    label=f"CAM_0{i}",
                    photo=SimpleNamespace(path=f"/proj/Flight1/CAM_0{i}.JPG", meta={}),
                    reference=None,
                )
                for i in (1, 2, 3)
            ]
        )
        self.assertTrue(pipeline_mod._chunks_share_camera_labels([shared_cam_a, shared_cam_b]))

        exif_a = SimpleNamespace(
            cameras=[
                SimpleNamespace(
                    label=f"DJI_000{i}",
                    photo=SimpleNamespace(
                        path=f"/nas_a/DJI_000{i}.JPG",
                        meta={"Exif/DateTimeOriginal": f"2026:11:01 10:00:0{i}", "Exif/BodySerialNumber": "SN123"},
                    ),
                    reference=None,
                )
                for i in (1, 2, 3)
            ]
        )
        exif_b = SimpleNamespace(
            cameras=[
                SimpleNamespace(
                    label=f"DJI_000{i}",
                    photo=SimpleNamespace(
                        path=f"/nas_b/DJI_000{i}.JPG",
                        meta={"Exif/DateTimeOriginal": f"2026:11:01 10:00:0{i}", "Exif/BodySerialNumber": "SN123"},
                    ),
                    reference=None,
                )
                for i in (1, 2, 3)
            ]
        )
        self.assertTrue(pipeline_mod._chunks_share_camera_labels([exif_a, exif_b]))

        too_few_a = SimpleNamespace(cameras=shared_cam_a.cameras[:2])
        too_few_b = SimpleNamespace(cameras=shared_cam_b.cameras[:2])
        self.assertFalse(pipeline_mod._chunks_share_camera_labels([too_few_a, too_few_b]))

        generic_a = SimpleNamespace(
            cameras=[
                SimpleNamespace(label=f"DJI_000{i}", photo=SimpleNamespace(path=f"/f1/DJI_000{i}.JPG"), reference=None)
                for i in (1, 2, 3, 4)
            ]
        )
        generic_b = SimpleNamespace(
            cameras=[
                SimpleNamespace(label=f"DJI_000{i}", photo=SimpleNamespace(path=f"/f2/DJI_000{i}.JPG"), reference=None)
                for i in (1, 2, 3, 4)
            ]
        )
        self.assertFalse(pipeline_mod._chunks_share_camera_labels([generic_a, generic_b]))

        disjoint_a = SimpleNamespace(
            markers=[SimpleNamespace(label="GCP1"), SimpleNamespace(label="GCP2"), SimpleNamespace(label="GCP3")],
            cameras=[SimpleNamespace(label="F1_01", reference=None)],
        )
        disjoint_b = SimpleNamespace(
            markers=[SimpleNamespace(label="GCP1"), SimpleNamespace(label="GCP2"), SimpleNamespace(label="GCP3")],
            cameras=[SimpleNamespace(label="F2_01", reference=None)],
        )
        self.assertFalse(pipeline_mod._chunks_share_camera_labels([disjoint_a, disjoint_b]))
        self.assertTrue(pipeline_mod._chunks_share_marker_labels([disjoint_a, disjoint_b]))

        # End-to-end check: unreferenced chunks with 3 shared markers select method=1 via run_reconstruction
        with tempfile.TemporaryDirectory() as tmp2:
            img2 = os.path.join(tmp2, "MarkersSite")
            out2 = os.path.join(tmp2, "Out2")
            os.makedirs(out2, exist_ok=True)
            for sub in ("PartA", "PartB"):
                sdir = os.path.join(img2, sub)
                os.makedirs(sdir, exist_ok=True)
                for idx in range(2):
                    with open(os.path.join(sdir, f"{sub}_{idx}.JPG"), "wb") as fh:
                        fh.write(b"\xff\xd8\xff\xe0" + f"{sub}_{idx}".encode("ascii"))

            class _MKChunk(_MCChunk):
                def __init__(self, key: int) -> None:
                    super().__init__(key)
                    self.crs = None
                    self.transform = SimpleNamespace(matrix=None)
                    self.markers = [
                        SimpleNamespace(label="M1"),
                        SimpleNamespace(label="M2"),
                        SimpleNamespace(label="M3"),
                    ]

                def addPhotos(self, paths):
                    for i, p in enumerate(paths):
                        self.cameras.append(
                            SimpleNamespace(
                                key=self.key * 100 + i,
                                label=f"{self.label}_{i}",
                                enabled=True,
                                transform=None,
                                center=SimpleNamespace(x=float(i), y=0.0, z=20.0),
                                reference=SimpleNamespace(enabled=False, location=None),
                                sensor=None,
                                meta={"Image/Quality": "0.9"},
                                photo=SimpleNamespace(path=p, meta={"Image/Quality": "0.9"}),
                            )
                        )

            class _MKDoc(_MCDoc):
                def addChunk(self) -> _MCChunk:
                    c = _MKChunk(self._next_key)
                    self._next_key += 1
                    self.chunks.append(c)
                    self.chunk = c
                    return c

            mk_doc = _MKDoc()
            mk_ms = SimpleNamespace(
                app=SimpleNamespace(
                    gpu_mask=0,
                    enumGPUDevices=lambda: [],
                    settings=SimpleNamespace(network_enable=False),
                ),
                Document=lambda: mk_doc,
                ReferencePreselectionMode=SimpleNamespace(ReferencePreselectionEstimated=1),
                Shutter=SimpleNamespace(Model=SimpleNamespace(Disabled="Disabled", Regularized="Regularized")),
            )
            with patch.object(pipeline_mod, "Metashape", mk_ms), redirect_stdout(io.StringIO()):
                pipeline_mod.run_reconstruction(
                    images_dir=img2,
                    output_dir=out2,
                    project_name="MarkerMulti",
                    multi_chunk="force",
                    chunk_align_method="camera",
                    align_photos=True,
                    alignment_hygiene=False,
                    build_depth_maps=False,
                    build_mesh=False,
                    build_texture=False,
                    build_orthomosaic=False,
                    crop_method="none",
                    filter_tiepoints=False,
                    icp_bridge=False,
                    resume=False,
                    local_scratch_staging=False,
                    network_enable=False,
                )
            self.assertEqual(len(mk_doc.align_calls), 1)
            self.assertEqual(mk_doc.align_calls[0]["method"], 1)

    def test_resume_rejects_partial_depth_maps_zero_face_mesh_and_untextured_export(self) -> None:
        """Partial depth maps, 0-face meshes, active POSIX locks, and untextured exports are guarded."""
        cams_20 = [SimpleNamespace(transform=object(), enabled=True) for _ in range(20)]
        partial_dm_chunk = SimpleNamespace(cameras=cams_20, depth_maps={i: b"d" for i in range(2)})
        complete_dm_chunk = SimpleNamespace(cameras=cams_20, depth_maps={i: b"d" for i in range(15)})
        self.assertEqual(pipeline_mod._has_complete_depth_maps(partial_dm_chunk), (False, 2))
        self.assertEqual(pipeline_mod._has_complete_depth_maps(complete_dm_chunk), (True, 15))

        self.assertFalse(pipeline_mod._has_valid_mesh(SimpleNamespace(model=None)))
        self.assertFalse(pipeline_mod._has_valid_mesh(SimpleNamespace(model=SimpleNamespace(faces=[]))))
        self.assertTrue(pipeline_mod._has_valid_mesh(SimpleNamespace(model=SimpleNamespace(faces=[1, 2, 3]))))

        # Active POSIX lock must NEVER be unlinked; stale lock IS removed
        with tempfile.TemporaryDirectory() as tmpdir:
            psx_path = os.path.join(tmpdir, "SiteLock.psx")
            files_dir = os.path.join(tmpdir, "SiteLock.files")
            os.makedirs(files_dir, exist_ok=True)
            lock_file = os.path.join(files_dir, "lock")
            with open(lock_file, "w", encoding="utf-8") as fh:
                fh.write("pid")

            fake_fcntl_busy = SimpleNamespace(
                LOCK_EX=2,
                LOCK_NB=4,
                LOCK_UN=8,
                flock=MagicMock(side_effect=BlockingIOError("Resource busy")),
            )
            with (
                patch.object(sys, "platform", "linux"),
                patch.dict(sys.modules, {"fcntl": fake_fcntl_busy}),
                redirect_stdout(io.StringIO()),
            ):
                self.assertFalse(pipeline_mod._safe_remove_stale_psx_lock(psx_path))
                self.assertTrue(os.path.isfile(lock_file))

            fake_fcntl_free = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8, flock=MagicMock())
            with (
                patch.object(sys, "platform", "linux"),
                patch.dict(sys.modules, {"fcntl": fake_fcntl_free}),
                redirect_stdout(io.StringIO()),
            ):
                self.assertTrue(pipeline_mod._safe_remove_stale_psx_lock(psx_path))
                self.assertFalse(os.path.isfile(lock_file))

        # Untextured mesh export must pass save_texture=False and embed_texture=False
        with tempfile.TemporaryDirectory() as tmpdir:
            img_dir = os.path.join(tmpdir, "images")
            out_dir = os.path.join(tmpdir, "out")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(img_dir, "IMG_0001.JPG"), "wb") as fh:
                fh.write(b"\xff\xd8\xff\xd9")

            export_calls: list[dict] = []

            def _fake_export_model(path, **kwargs):
                with open(path, "wb") as fh:
                    fh.write(b"glTF")
                export_calls.append({"path": path, **kwargs})

            untex_model = SimpleNamespace(
                vertices=[1, 2, 3],
                faces=[1, 2],
                textures=[],
                texture=lambda: None,
                meta={},
            )
            chunk = SimpleNamespace(
                label="Prod",
                crs=None,
                cameras=[],
                sensors=[],
                markers=[],
                model=untex_model,
                point_cloud=None,
                elevation=None,
                orthomosaic=None,
                depth_maps=None,
                tie_points=None,
                addPhotos=lambda *_a, **_kw: None,
                analyzeImages=lambda *_a, **_kw: None,
                exportModel=_fake_export_model,
            )
            doc = MagicMock()
            doc.chunk = chunk
            doc.chunks = [chunk]
            doc.addChunk.return_value = chunk

            fake_ms = SimpleNamespace(
                app=SimpleNamespace(
                    gpu_mask=0,
                    enumGPUDevices=lambda: [],
                    settings=SimpleNamespace(network_enable=False),
                ),
                Document=lambda: doc,
                ReferencePreselectionMode=SimpleNamespace(ReferencePreselectionEstimated=1),
                ModelFormat=SimpleNamespace(ModelFormatGLTF=1, ModelFormatOBJ=2, ModelFormatFBX=3),
                ImageFormat=SimpleNamespace(ImageFormatPNG=1, ImageFormatTIFF=2, ImageFormatJPEG=3),
            )
            with patch.object(pipeline_mod, "Metashape", fake_ms), redirect_stdout(io.StringIO()):
                pipeline_mod.run_reconstruction(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="UntexSite",
                    align_photos=False,
                    build_depth_maps=False,
                    build_mesh=False,
                    build_texture=False,
                    build_orthomosaic=False,
                    export_glb=True,
                    export_obj=True,
                    export_fbx=True,
                    resume=False,
                    local_scratch_staging=False,
                    network_enable=False,
                )

            self.assertEqual(len(export_calls), 3)
            for call in export_calls:
                self.assertFalse(call["save_texture"])
            glb_call = next(c for c in export_calls if c["path"].endswith(".glb"))
            self.assertFalse(glb_call["embed_texture"])

    def test_wgs84_point_cloud_uses_projected_crs_and_feet_crs_scales_ortho_resolution(self) -> None:
        """Geographic chunks export projected UTM clouds; foot CRSes scale meter ortho_resolution."""
        # 1. export.infer_project_name UNC share roots and drive roots
        self.assertEqual(export_mod.infer_project_name(images_dir=r"\\SRV\Site01"), "Site01")
        self.assertEqual(export_mod.infer_project_name(images_dir=r"\\SRV\Site01\images"), "Site01")
        self.assertEqual(export_mod.infer_project_name(images_dir=r"E:\\"), "project")
        self.assertTrue(export_mod._is_local_or_unreferenced_crs(_WktCrs('ENGCRS["Local"]')))

        us_ft_factor = 0.3048006096012192
        us_ft_wkt = (
            'PROJCS["NAD83 / Texas Central (ftUS)",'
            'GEOGCS["NAD83"],'
            'UNIT["US survey foot",0.3048006096012192]]'
        )
        expected_ft_res = 0.05 / us_ft_factor

        # 2. export.py point cloud UTC CRS + feet ortho_resolution scaling
        with tempfile.TemporaryDirectory() as tmpdir:
            psx = os.path.join(tmpdir, "SiteGeo.psx")
            with open(psx, "w", encoding="utf-8") as fh:
                fh.write("<doc/>")

            pc_calls: list[dict] = []

            def _fake_export_pc(**kwargs):
                with open(kwargs["path"], "wb") as fh:
                    fh.write(b"LAZ")
                pc_calls.append(kwargs)

            cam = SimpleNamespace(
                enabled=True,
                transform=object(),
                reference=SimpleNamespace(enabled=True, location=SimpleNamespace(x=106.7, y=10.8, z=80.0)),
            )
            geo_chunk = MagicMock()
            geo_chunk.crs = _WktCrs('GEOGCS["WGS 84"]')
            geo_chunk.cameras = [cam]
            geo_chunk.markers = []
            geo_chunk.model = None
            geo_chunk.elevation = None
            geo_chunk.orthomosaic = None
            geo_chunk.point_cloud = SimpleNamespace(point_count=500)
            geo_chunk.tie_points = object()
            geo_chunk.exportPointCloud.side_effect = _fake_export_pc

            geo_doc = MagicMock()
            geo_doc.chunk = geo_chunk
            geo_doc.chunks = [geo_chunk]

            fake_ms = SimpleNamespace(
                Document=lambda: geo_doc,
                app=SimpleNamespace(enumGPUDevices=lambda: []),
                OrthoProjection=lambda: SimpleNamespace(crs=None),
                CoordinateSystem=lambda code: _WktCrs(f'PROJCS["{code}",UNIT["metre",1]]'),
                PointCloudFormat=SimpleNamespace(PointCloudFormatLAZ=1, PointCloudFormatPLY=2),
                DataSource=SimpleNamespace(PointCloudData=1, TiePointsData=2),
            )
            with patch.object(export_mod, "Metashape", fake_ms), redirect_stdout(io.StringIO()):
                export_mod.export_project_assets(
                    project_path=psx,
                    output_dir=tmpdir,
                    export_glb=False,
                    export_obj=False,
                    export_point_cloud=True,
                    export_tie_points=True,
                )

            self.assertEqual(len(pc_calls), 2)
            for call in pc_calls:
                self.assertIn("crs", call)
                self.assertIn("EPSG::32648", call["crs"].wkt)

            # Feet CRS orthomosaic build + export scales 0.05m -> ~0.16404 ft
            feet_crs = _WktCrs(us_ft_wkt)
            raster_calls: list[dict] = []
            build_om_calls: list[dict] = []

            def _build_om(**kw):
                build_om_calls.append(kw)
                feet_chunk.orthomosaic = SimpleNamespace(crs=feet_crs, width=100, height=100)

            def _export_raster(**kw):
                with open(kw["path"], "wb") as fh:
                    fh.write(b"II*\x00")
                raster_calls.append(kw)

            feet_chunk = MagicMock()
            feet_chunk.crs = feet_crs
            feet_chunk.cameras = []
            feet_chunk.markers = []
            feet_chunk.model = None
            feet_chunk.elevation = object()
            feet_chunk.orthomosaic = None
            feet_chunk.buildOrthomosaic.side_effect = _build_om
            feet_chunk.exportRaster.side_effect = _export_raster

            feet_doc = MagicMock()
            feet_doc.chunk = feet_chunk
            feet_doc.chunks = [feet_chunk]

            fake_ms_ft = SimpleNamespace(
                Document=lambda: feet_doc,
                app=SimpleNamespace(enumGPUDevices=lambda: []),
                OrthoProjection=lambda: SimpleNamespace(crs=None),
                CoordinateSystem=lambda code: _WktCrs(f'PROJCS["{code}"]'),
                DataSource=SimpleNamespace(ElevationData=1, ModelData=2, OrthomosaicData=3),
                ImageFormat=SimpleNamespace(ImageFormatTIFF=1),
                ImageCompression=type(
                    "_Comp",
                    (),
                    {
                        "TiffCompression": SimpleNamespace(TiffCompressionLZW=1),
                        "tiff_compression": None,
                        "tiff_big": False,
                    },
                ),
            )
            with patch.object(export_mod, "Metashape", fake_ms_ft), redirect_stdout(io.StringIO()):
                export_mod.export_project_assets(
                    project_path=psx,
                    output_dir=tmpdir,
                    export_glb=False,
                    export_obj=False,
                    export_orthomosaic=True,
                    ortho_resolution=0.05,
                )

            self.assertAlmostEqual(build_om_calls[0]["resolution"], 0.05, places=6)
            self.assertAlmostEqual(raster_calls[0]["resolution"], 0.05, places=6)

        # 3. pipeline.py also projects WGS84 clouds to UTM and scales feet ortho_resolution
        with tempfile.TemporaryDirectory() as tmpdir:
            img_dir = os.path.join(tmpdir, "images")
            out_dir = os.path.join(tmpdir, "out")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(img_dir, "IMG_0001.JPG"), "wb") as fh:
                fh.write(b"\xff\xd8\xff\xd9")

            pipe_pc_calls: list[dict] = []

            feet_crs = _WktCrs(us_ft_wkt)
            geo_cam = SimpleNamespace(
                enabled=True,
                transform=object(),
                meta={"Image/Quality": "0.9"},
                reference=SimpleNamespace(enabled=True, location=SimpleNamespace(x=106.7, y=10.8, z=80.0)),
            )
            pchunk = MagicMock()
            pchunk.label = "Pipe"
            pchunk.crs = _WktCrs('GEOGCS["WGS 84"]')
            pchunk.cameras = [geo_cam]
            pchunk.sensors = []
            pchunk.markers = []
            pchunk.model = None
            pchunk.elevation = None
            pchunk.orthomosaic = None
            pchunk.point_cloud = SimpleNamespace(point_count=300)
            pchunk.tie_points = object()
            pchunk.exportPointCloud.side_effect = lambda **kw: (
                open(kw["path"], "wb").close() or pipe_pc_calls.append(kw)
            )

            pdoc = MagicMock()
            pdoc.chunk = pchunk
            pdoc.chunks = [pchunk]
            pdoc.addChunk.return_value = pchunk

            fake_ms_pipe = SimpleNamespace(
                app=SimpleNamespace(
                    gpu_mask=0,
                    enumGPUDevices=lambda: [],
                    settings=SimpleNamespace(network_enable=False),
                ),
                Document=lambda: pdoc,
                OrthoProjection=lambda: SimpleNamespace(crs=None),
                CoordinateSystem=lambda code: _WktCrs(f'PROJCS["{code}",UNIT["metre",1]]'),
                ReferencePreselectionMode=SimpleNamespace(ReferencePreselectionEstimated=1),
                PointCloudFormat=SimpleNamespace(PointCloudFormatLAZ=1, PointCloudFormatPLY=2),
                DataSource=SimpleNamespace(
                    PointCloudData=1,
                    TiePointsData=2,
                    ElevationData=3,
                    ModelData=4,
                    OrthomosaicData=5,
                ),
            )
            with patch.object(pipeline_mod, "Metashape", fake_ms_pipe), redirect_stdout(io.StringIO()):
                pipeline_mod.run_reconstruction(
                    images_dir=img_dir,
                    output_dir=out_dir,
                    project_name="PipeGeo",
                    align_photos=False,
                    build_depth_maps=False,
                    build_mesh=False,
                    build_texture=False,
                    build_orthomosaic=False,
                    build_point_cloud=False,
                    export_point_cloud=True,
                    export_tie_points=True,
                    resume=False,
                    local_scratch_staging=False,
                    network_enable=False,
                )

            self.assertEqual(len(pipe_pc_calls), 2)
            for call in pipe_pc_calls:
                self.assertIn("EPSG::32648", call["crs"].wkt)

            self.assertAlmostEqual(
                pipeline_mod._scale_resolution_to_crs_units(0.05, feet_crs),
                expected_ft_res,
                places=6,
            )
            self.assertAlmostEqual(
                pipeline_mod._scale_resolution_to_crs_units(0.05, _WktCrs('PROJCS["UTM",UNIT["metre",1]]')),
                0.05,
                places=6,
            )

    def test_derive_pro_photogrammetry_plan_coarse_gsd_and_gpsless(self) -> None:
        """Coarse GSD scales ortho resolution to avoid upsampling; GPS-less scans disable orthomosaic."""
        coarse = telemetry_mod.derive_pro_photogrammetry_plan(
            {"has_gps": True, "shutter": "mechanical", "relief_ratio": 0.04, "gsd_cm": 15.0}
        )
        self.assertAlmostEqual(coarse["ortho_resolution"], 0.15)
        self.assertTrue(coarse["build_orthomosaic"])

        fine = telemetry_mod.derive_pro_photogrammetry_plan(
            {"has_gps": True, "shutter": "mechanical", "relief_ratio": 0.04, "gsd_cm": 1.2}
        )
        self.assertAlmostEqual(fine["ortho_resolution"], 0.05)
        self.assertTrue(fine["build_orthomosaic"])

        no_gps = telemetry_mod.derive_pro_photogrammetry_plan(
            {"has_gps": False, "shutter": "mechanical", "relief_ratio": 0.0, "gsd_cm": None}
        )
        self.assertFalse(no_gps["build_orthomosaic"])
        self.assertEqual(no_gps["crop_method"], "structure")


if __name__ == "__main__":
    unittest.main()
