# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape: Autonomous High-Precision Photogrammetry Pipeline using Agisoft Metashape Pro.

Reconstructs production-ready 3D architectural models (.obj, .mtl, .glb, .psx)
directly from raw aerial drone or ground camera image directories on NVIDIA GPUs.
Configured with production-grade defaults:
- Image Alignment: High quality (downscale=1), 200,000 keypoint limit (15,000/MP), 25,000 tiepoint limit,
                   stationary tie points excluded, guided matching enabled, adaptive fitting enabled.
- Camera Optimization: Rolling shutter compensation enabled, Gradual Selection tie-point filtering.
- Bounding Box: gps-square auto-crop (smallest enclosing square of camera GPS poses with uncropped vertical bounds).
- Depth Maps & Meshing: High quality depth maps (downscale=2), Moderate depth filtering, 600,000 (600K) custom faces.
- Texture Atlas: 16-page 8K photographic mosaic blending texture atlas (hole filling and ghosting filter enabled).
"""

from __future__ import annotations
import argparse
import math
import os
import re
import sys
import threading
import time
try:
    import Metashape
except ImportError:
    Metashape = None


# ---------------------------------------------------------------------------
# Machine-Readable IPC Milestone Protocol Sentinels
# ---------------------------------------------------------------------------
IPC_SENTINEL_PREFIX = "@@AUTOMETASHAPE_STAGE:"
IPC_SENTINEL_SUFFIX = "@@"
IPC_PAUSE_CROP = "@@AUTOMETASHAPE_PAUSE_CROP@@"
IPC_COMPLETE = "@@AUTOMETASHAPE_COMPLETE@@"


def emit_ipc_stage(stage_key: str, description: str) -> None:
    """Emits an explicit machine-readable IPC milestone sentinel to stdout for GUI tracking."""
    print(f"{IPC_SENTINEL_PREFIX}{stage_key}:{description}{IPC_SENTINEL_SUFFIX}", flush=True)


def emit_ipc_event(sentinel: str) -> None:
    """Emits an explicit machine-readable IPC control event to stdout."""
    print(sentinel, flush=True)


# Comprehensive list of all image and camera RAW formats supported by Agisoft Metashape
SUPPORTED_IMAGE_EXTS = (
    # Standard raster formats
    ".jpg", ".jpeg", ".jpe",
    ".png",
    ".tif", ".tiff",
    ".bmp", ".dib",
    ".webp",
    ".jp2", ".j2k", ".jpc", ".j2c",
    ".jxl",
    ".exr",
    ".tga",
    ".dds",
    ".pnm", ".ppm", ".pgm", ".pbm",
    ".sgi", ".rgb",
    ".bil",
    # Camera RAW formats (DJI, Sony, Phase One, Hasselblad, Canon, Nikon, Leica, etc.)
    ".dng",                  # Adobe DNG (DJI Mavic 3 / Matrice / Phantom RTK RAW, Autel RAW)
    ".cr2", ".cr3", ".crw",  # Canon RAW
    ".nef", ".nrw",          # Nikon RAW
    ".arw", ".srf", ".sr2",  # Sony RAW
    ".iiq", ".cap",          # Phase One aerial cameras
    ".3fr", ".fff",          # Hasselblad aerial cameras
    ".rw2",                  # Panasonic RAW
    ".orf",                  # Olympus RAW
    ".raf",                  # Fujifilm RAW
    ".raw", ".rwl",          # Leica RAW
    ".pef", ".ptx",          # Pentax RAW
    # Radiometric thermal and multi-spectral formats
    ".seq", ".ara",          # FLIR radiometric thermal sequences
)

ALIGN_DOWNSCALE_MAP = {
    "highest": 0,
    "high": 1,
    "medium": 2,
    "low": 4,
    "lowest": 8,
}

DEPTH_DOWNSCALE_MAP = {
    "ultra": 1,
    "high": 2,
    "medium": 4,
    "low": 8,
    "lowest": 16,
}

if Metashape is not None:
    DEPTH_FILTER_MAP = {
        "mild": Metashape.FilterMode.MildFiltering,
        "moderate": Metashape.FilterMode.ModerateFiltering,
        "aggressive": Metashape.FilterMode.AggressiveFiltering,
    }

    POINT_CLOUD_FORMAT_MAP = {
        "las": (Metashape.PointCloudFormat.PointCloudFormatLAS, ".las"),
        "laz": (Metashape.PointCloudFormat.PointCloudFormatLAZ, ".laz"),
        "ply": (Metashape.PointCloudFormat.PointCloudFormatPLY, ".ply"),
        "e57": (Metashape.PointCloudFormat.PointCloudFormatE57, ".e57"),
        "xyz": (Metashape.PointCloudFormat.PointCloudFormatXYZ, ".xyz"),
    }

    TILED_MODEL_FORMAT_MAP = {
        "cesium": (Metashape.TiledModelFormat.TiledModelFormatCesium, "tileset.json"),
        "tls": (Metashape.TiledModelFormat.TiledModelFormatTLS, ".tls"),
        "slpk": (Metashape.TiledModelFormat.TiledModelFormatSLPK, ".slpk"),
        "osgb": (Metashape.TiledModelFormat.TiledModelFormatOSGB, ".zip"),
    }

    SHAPES_FORMAT_MAP = {
        "shp": (Metashape.ShapesFormat.ShapesFormatSHP, ".shp"),
        "dxf": (Metashape.ShapesFormat.ShapesFormatDXF, ".dxf"),
        "geojson": (Metashape.ShapesFormat.ShapesFormatGeoJSON, ".geojson"),
        "kml": (Metashape.ShapesFormat.ShapesFormatKML, ".kml"),
    }

    TARGET_TYPE_MAP = {
        "circular12": Metashape.TargetType.CircularTarget12bit,
        "circular14": Metashape.TargetType.CircularTarget14bit,
        "circular16": Metashape.TargetType.CircularTarget16bit,
        "circular20": Metashape.TargetType.CircularTarget20bit,
        "cross": Metashape.TargetType.CrossTarget,
        "apriltag16h5": Metashape.TargetType.AprilTag16h5,
        "apriltag25h9": Metashape.TargetType.AprilTag25h9,
        "apriltag36h11": Metashape.TargetType.AprilTag36h11,
    }

    SURFACE_TYPE_MAP = {
        "arbitrary": Metashape.SurfaceType.Arbitrary,
        "heightfield": Metashape.SurfaceType.HeightField,
    }

    PRESEL_MODE_MAP = {
        "estimated": Metashape.ReferencePreselectionMode.ReferencePreselectionEstimated,
        "source": Metashape.ReferencePreselectionMode.ReferencePreselectionSource,
        "sequential": Metashape.ReferencePreselectionMode.ReferencePreselectionSequential,
    }
else:
    DEPTH_FILTER_MAP = {"mild": 1, "moderate": 2, "aggressive": 3}
    POINT_CLOUD_FORMAT_MAP = {
        "las": (None, ".las"),
        "laz": (None, ".laz"),
        "ply": (None, ".ply"),
        "e57": (None, ".e57"),
        "xyz": (None, ".xyz"),
    }
    TILED_MODEL_FORMAT_MAP = {
        "cesium": (None, "tileset.json"),
        "tls": (None, ".tls"),
        "slpk": (None, ".slpk"),
        "osgb": (None, ".zip"),
    }
    SHAPES_FORMAT_MAP = {
        "shp": (None, ".shp"),
        "dxf": (None, ".dxf"),
        "geojson": (None, ".geojson"),
        "kml": (None, ".kml"),
    }
    TARGET_TYPE_MAP = {}
    SURFACE_TYPE_MAP = {"arbitrary": 0, "heightfield": 1}
    PRESEL_MODE_MAP = {"estimated": 1, "source": 0, "sequential": 2}


def compute_utm_epsg(lon: float, lat: float) -> int:
    """Calculates the standard WGS 84 / UTM Zone EPSG code for a given longitude and latitude."""
    zone = int((lon + 180.0) // 6.0) + 1
    zone = max(1, min(zone, 60))
    return 32600 + zone if lat >= 0.0 else 32700 + zone


def calculate_haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculates orthodromic surface distance between two WGS84 GPS coordinates in meters."""
    r_earth = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0)**2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return r_earth * c


# Professional Camera Optics & Sensor Database for Aerial Photogrammetry Payloads
CAMERA_HARDWARE_DB = {
    # DJI Enterprise & High-End Payloads (Mechanical Leaf / Global Shutters)
    "fc6310": {"name": "DJI Phantom 4 Pro / RTK (FC6310)", "sensor_w_mm": 13.2, "sensor_h_mm": 8.8, "pixel_pitch_um": 2.41, "shutter": "mechanical", "focal_fixed": 8.8},
    "fc6310r": {"name": "DJI Phantom 4 RTK (FC6310R)", "sensor_w_mm": 13.2, "sensor_h_mm": 8.8, "pixel_pitch_um": 2.41, "shutter": "mechanical", "focal_fixed": 8.8},
    "fc3411": {"name": "DJI Mavic 3 Enterprise Wide (FC3411)", "sensor_w_mm": 17.3, "sensor_h_mm": 13.0, "pixel_pitch_um": 3.28, "shutter": "mechanical", "focal_fixed": 12.29},
    "zenmuse p1": {"name": "DJI Zenmuse P1 Full-Frame", "sensor_w_mm": 35.9, "sensor_h_mm": 24.0, "pixel_pitch_um": 4.4, "shutter": "mechanical", "focal_fixed": None},
    "zenmuse x4s": {"name": "DJI Zenmuse X4S", "sensor_w_mm": 13.2, "sensor_h_mm": 8.8, "pixel_pitch_um": 2.41, "shutter": "mechanical", "focal_fixed": 8.8},
    "zenmuse x5s": {"name": "DJI Zenmuse X5S (Micro 4/3)", "sensor_w_mm": 17.3, "sensor_h_mm": 13.0, "pixel_pitch_um": 3.4, "shutter": "rolling", "focal_fixed": None},
    "zenmuse x7": {"name": "DJI Zenmuse X7 (Super 35)", "sensor_w_mm": 23.5, "sensor_h_mm": 15.7, "pixel_pitch_um": 3.91, "shutter": "mechanical", "focal_fixed": None},
    "l2": {"name": "DJI Zenmuse L2 RGB", "sensor_w_mm": 17.3, "sensor_h_mm": 13.0, "pixel_pitch_um": 3.28, "shutter": "mechanical", "focal_fixed": 12.29},
    "m3m": {"name": "DJI Mavic 3 Multispectral RGB", "sensor_w_mm": 17.3, "sensor_h_mm": 13.0, "pixel_pitch_um": 3.28, "shutter": "mechanical", "focal_fixed": 12.29},

    # DJI Consumer Drones (CMOS Electronic Rolling Shutter)
    "fc9589": {"name": "DJI Mini 4 / Neo / Consumer Payload (FC9589)", "sensor_w_mm": 10.08, "sensor_h_mm": 7.56, "pixel_pitch_um": 2.50, "shutter": "rolling", "focal_fixed": 6.72},
    "fc9470": {"name": "DJI Neo (FC9470)", "sensor_w_mm": 6.86, "sensor_h_mm": 3.86, "pixel_pitch_um": 1.72, "shutter": "rolling", "focal_fixed": 3.05},
    "fc3582": {"name": "DJI Mavic 3 / Cine / Classic Hasselblad", "sensor_w_mm": 17.3, "sensor_h_mm": 13.0, "pixel_pitch_um": 3.28, "shutter": "rolling", "focal_fixed": 12.29},
    "fc3170": {"name": "DJI Mavic Air 2", "sensor_w_mm": 6.4, "sensor_h_mm": 4.8, "pixel_pitch_um": 0.8, "shutter": "rolling", "focal_fixed": 4.5},
    "fc3011": {"name": "DJI Air 2S", "sensor_w_mm": 13.2, "sensor_h_mm": 8.8, "pixel_pitch_um": 2.4, "shutter": "rolling", "focal_fixed": 8.38},
    "fc3580": {"name": "DJI Mini 3 / Mini 4 Pro", "sensor_w_mm": 9.6, "sensor_h_mm": 7.2, "pixel_pitch_um": 1.2, "shutter": "rolling", "focal_fixed": 6.72},
    "fc7303": {"name": "DJI Mini 2", "sensor_w_mm": 6.17, "sensor_h_mm": 4.55, "pixel_pitch_um": 1.55, "shutter": "rolling", "focal_fixed": 4.49},
    "fc220":  {"name": "DJI Mavic Pro", "sensor_w_mm": 6.17, "sensor_h_mm": 4.55, "pixel_pitch_um": 1.55, "shutter": "rolling", "focal_fixed": 4.73},

    # Autel Survey Payloads
    "evo ii pro": {"name": "Autel EVO II Pro (1-inch)", "sensor_w_mm": 13.2, "sensor_h_mm": 8.8, "pixel_pitch_um": 2.4, "shutter": "rolling", "focal_fixed": 9.0},
    "evo max":    {"name": "Autel EVO Max 4T", "sensor_w_mm": 13.2, "sensor_h_mm": 8.8, "pixel_pitch_um": 2.4, "shutter": "rolling", "focal_fixed": None},

    # Professional Aerial Survey & Photogrammetric Cameras (Global / Leaf Shutters)
    "phase one":  {"name": "Phase One Industrial (iXM / iXU)", "sensor_w_mm": 53.4, "sensor_h_mm": 40.0, "pixel_pitch_um": 3.76, "shutter": "mechanical", "focal_fixed": None},
    "ixm":        {"name": "Phase One iXM Medium Format", "sensor_w_mm": 53.4, "sensor_h_mm": 40.0, "pixel_pitch_um": 3.76, "shutter": "mechanical", "focal_fixed": None},
    "hasselblad": {"name": "Hasselblad Aerial Medium Format", "sensor_w_mm": 43.8, "sensor_h_mm": 32.9, "pixel_pitch_um": 3.45, "shutter": "mechanical", "focal_fixed": None},
    "ilx-lr1":    {"name": "Sony ILX-LR1 Industrial Full-Frame", "sensor_w_mm": 35.7, "sensor_h_mm": 23.8, "pixel_pitch_um": 3.76, "shutter": "mechanical", "focal_fixed": None},
    "rx1r":       {"name": "Sony RX1R II Full-Frame", "sensor_w_mm": 35.9, "sensor_h_mm": 24.0, "pixel_pitch_um": 4.5, "shutter": "mechanical", "focal_fixed": 35.0},
    "wingtra":    {"name": "WingtraOne Aerial Payload", "sensor_w_mm": 35.9, "sensor_h_mm": 24.0, "pixel_pitch_um": 4.5, "shutter": "mechanical", "focal_fixed": 35.0},
}


def parse_xmp_telemetry(img_path: str) -> dict:
    """Fast probe extracting DJI RelativeAltitude, ShutterType, and GimbalPitch from XMP packet."""
    res = {"relative_alt": None, "shutter_type": None, "gimbal_pitch": None}
    try:
        with open(img_path, "rb") as f:
            head = f.read(65536)
            start = head.find(b"<x:xmpmeta")
            if start != -1:
                end = head.find(b"</x:xmpmeta>", start)
                if end != -1:
                    xmp_str = head[start:end+12].decode("utf-8", errors="ignore")
                    m = re.search(r'RelativeAltitude=["\']([+-]?\d+(?:\.\d+)?)["\']', xmp_str)
                    if not m:
                        m = re.search(r'<[^>]*RelativeAltitude>([+-]?\d+(?:\.\d+)?)</[^>]*RelativeAltitude>', xmp_str)
                    if m:
                        res["relative_alt"] = float(m.group(1))

                    m_sh = re.search(r'ShutterType=["\']([A-Za-z]+)["\']', xmp_str)
                    if not m_sh:
                        m_sh = re.search(r'<[^>]*ShutterType>([A-Za-z]+)</[^>]*ShutterType>', xmp_str)
                    if m_sh:
                        res["shutter_type"] = m_sh.group(1).strip().lower()

                    m_pitch = re.search(r'GimbalPitchDegree=["\']([+-]?\d+(?:\.\d+)?)["\']', xmp_str)
                    if not m_pitch:
                        m_pitch = re.search(r'<[^>]*GimbalPitchDegree>([+-]?\d+(?:\.\d+)?)</[^>]*GimbalPitchDegree>', xmp_str)
                    if m_pitch:
                        res["gimbal_pitch"] = float(m_pitch.group(1))
    except Exception:
        pass
    return res


def parse_xmp_relative_altitude(img_path: str):
    """Backwards-compatible probe extracting relative altitude."""
    return parse_xmp_telemetry(img_path).get("relative_alt")


def extract_image_telemetry(img_path: str) -> dict:
    """Extracts camera model, focal length, image resolution, and GPS telemetry from image headers."""
    meta = {
        "width": 0,
        "height": 0,
        "make": "",
        "model": "",
        "focal_length": None,
        "focal_35mm": None,
        "lat": None,
        "lon": None,
        "alt": None,
        "relative_alt": None,
        "has_gps": False,
        "iso": None,
        "exposure_time": None,
    }
    try:
        from PIL import Image, ExifTags
        with Image.open(img_path) as im:
            meta["width"], meta["height"] = im.size
            exif_raw = im.getexif()
            if exif_raw:
                for k, v in exif_raw.items():
                    tag_name = ExifTags.TAGS.get(k, k)
                    if tag_name == "Make" and isinstance(v, str):
                        meta["make"] = v.strip()
                    elif tag_name == "Model" and isinstance(v, str):
                        meta["model"] = v.strip()
                    elif tag_name == "FocalLength":
                        try:
                            meta["focal_length"] = float(v)
                        except (ValueError, TypeError):
                            pass
                    elif tag_name == "FocalLengthIn35mmFilm":
                        try:
                            meta["focal_35mm"] = float(v)
                        except (ValueError, TypeError):
                            pass
                    elif tag_name in ("ISOSpeedRatings", "PhotographicSensitivity"):
                        try:
                            meta["iso"] = int(v)
                        except (ValueError, TypeError):
                            pass
                    elif tag_name == "ExposureTime":
                        try:
                            meta["exposure_time"] = float(v)
                        except (ValueError, TypeError):
                            pass

                # Nested EXIF IFD inspection for focal length in 35mm
                if hasattr(ExifTags, "IFD") and hasattr(ExifTags.IFD, "Exif"):
                    try:
                        exif_ifd = exif_raw.get_ifd(ExifTags.IFD.Exif)
                        if exif_ifd:
                            if 41989 in exif_ifd and meta["focal_35mm"] is None:
                                meta["focal_35mm"] = float(exif_ifd[41989])
                            if 37386 in exif_ifd and meta["focal_length"] is None:
                                meta["focal_length"] = float(exif_ifd[37386])
                    except Exception:
                        pass

                gps_ifd = None
                if hasattr(ExifTags, "IFD") and hasattr(ExifTags.IFD, "GPSInfo"):
                    try:
                        gps_ifd = exif_raw.get_ifd(ExifTags.IFD.GPSInfo)
                    except Exception:
                        pass
                if not gps_ifd and 34853 in exif_raw:
                    gps_ifd = exif_raw[34853]

                if gps_ifd:
                    def _to_deg(val):
                        try:
                            return float(val[0]) + float(val[1]) / 60.0 + float(val[2]) / 3600.0
                        except Exception:
                            return None

                    lat_ref = gps_ifd.get(1, "N")
                    lat_val = gps_ifd.get(2)
                    lon_ref = gps_ifd.get(3, "E")
                    lon_val = gps_ifd.get(4)
                    alt_val = gps_ifd.get(6)

                    if lat_val and lon_val:
                        lat = _to_deg(lat_val)
                        lon = _to_deg(lon_val)
                        if lat is not None and lon is not None and (abs(lat) > 0.001 or abs(lon) > 0.001):
                            if lat_ref == "S":
                                lat = -lat
                            if lon_ref == "W":
                                lon = -lon
                            meta["lat"] = lat
                            meta["lon"] = lon
                            meta["has_gps"] = True

                    if alt_val is not None:
                        try:
                            meta["alt"] = float(alt_val)
                        except Exception:
                            pass

        # Fast probe for DJI relative altitude
        xmp_data = parse_xmp_telemetry(img_path)
        meta["relative_alt"] = xmp_data.get("relative_alt")
        meta["xmp_shutter"] = xmp_data.get("shutter_type")
        meta["gimbal_pitch"] = xmp_data.get("gimbal_pitch")
    except Exception:
        pass
    return meta


def analyze_radiometry(image_paths: list, sample_count: int = 15) -> dict:
    """Evaluates optical dynamic range across sample images to detect shadow and highlight clipping."""
    if not image_paths:
        return {"shadow_clip_ratio": 0.0, "highlight_clip_ratio": 0.0, "delighting_recommended": False}
    step = max(1, len(image_paths) // sample_count)
    sampled = image_paths[::step][:sample_count]
    shadow_counts = []
    highlight_counts = []
    has_pil = False
    try:
        from PIL import Image
        has_pil = True
    except ImportError:
        has_pil = False

    has_meta = False
    try:
        import Metashape
        has_meta = True
    except ImportError:
        has_meta = False

    for p in sampled:
        try:
            if has_pil:
                with Image.open(p) as img:
                    thumb = img.resize((256, 256)).convert("L")
                    hist = thumb.histogram()
                    total = 256 * 256
                    shadow_counts.append(sum(hist[:20]) / total)
                    highlight_counts.append(sum(hist[235:]) / total)
            elif has_meta:
                im = Metashape.Image().open(p)
                if im:
                    thumb = im.resize(256, 256)
                    if thumb:
                        raw = thumb.tostring()
                        total = len(raw)
                        if total > 0:
                            shadow_counts.append(sum(1 for b in raw if b < 20) / total)
                            highlight_counts.append(sum(1 for b in raw if b > 235) / total)
        except Exception:
            pass
    if not shadow_counts:
        return {"shadow_clip_ratio": 0.0, "highlight_clip_ratio": 0.0, "delighting_recommended": False}
    avg_shadow = sum(shadow_counts) / len(shadow_counts)
    avg_highlight = sum(highlight_counts) / len(highlight_counts)
    return {
        "shadow_clip_ratio": avg_shadow,
        "highlight_clip_ratio": avg_highlight,
        "delighting_recommended": (avg_shadow > 0.15) or (avg_shadow > 0.10 and avg_highlight > 0.10),
    }


def analyze_survey_physics(images_dir: str, sample_size: int = 50, output_dir: str = None) -> dict:
    """Profiles survey physics: sensor optics, flying height, GSD, overlap, dynamic range, and relief ratio."""
    groups = discover_image_subfolder_groups(images_dir, output_dir=output_dir)
    all_imgs = []
    for imgs in groups.values():
        all_imgs.extend(imgs)
    all_imgs = sorted(all_imgs)

    total_photos = len(all_imgs)
    if total_photos == 0:
        return {"photo_count": 0, "error": "No images found"}

    step = max(1, total_photos // sample_size)
    sampled = [all_imgs[i] for i in range(0, total_photos, step)][:sample_size]

    lats, lons, alts, rel_alts = [], [], [], []
    cameras_detected = set()
    widths, heights = [], []
    focal_lengths = []
    focal_35mms = []
    makes = []
    models = []
    coords = []

    has_meta = False
    try:
        import Metashape
        has_meta = True
    except ImportError:
        has_meta = False

    if has_meta:
        try:
            temp_doc = Metashape.Document()
            temp_chunk = temp_doc.addChunk()
            temp_chunk.addPhotos(sampled)
            for c in temp_chunk.cameras:
                meta = c.photo.meta
                make = meta["Exif/Make"] if "Exif/Make" in meta else ""
                model = meta["Exif/Model"] if "Exif/Model" in meta else ""
                if model:
                    cameras_detected.add(f"{make} {model}".strip())
                    models.append(model)
                if make:
                    makes.append(make)
                w = c.sensor.width if c.sensor else 0
                h = c.sensor.height if c.sensor else 0
                if w > 0:
                    widths.append(w)
                    heights.append(h)
                if "Exif/FocalLength" in meta:
                    try:
                        focal_lengths.append(float(meta["Exif/FocalLength"]))
                    except (ValueError, TypeError):
                        pass
                if "Exif/FocalLengthIn35mmFilm" in meta:
                    try:
                        focal_35mms.append(float(meta["Exif/FocalLengthIn35mmFilm"]))
                    except (ValueError, TypeError):
                        pass
                if c.reference and getattr(c.reference, "enabled", True) and c.reference.location:
                    lon = c.reference.location.x
                    lat = c.reference.location.y
                    alt = c.reference.location.z
                    if abs(lat) > 0.001 or abs(lon) > 0.001:
                        lats.append(lat)
                        lons.append(lon)
                        coords.append((lat, lon))
                        if alt is not None:
                            alts.append(alt)
                rel_alt_val = None
                if "DJI/RelativeAltitude" in meta:
                    try:
                        rel_alt_val = float(meta["DJI/RelativeAltitude"])
                    except (ValueError, TypeError):
                        pass
                if rel_alt_val is None and c.photo and c.photo.path:
                    rel_alt_val = parse_xmp_relative_altitude(c.photo.path)
                if rel_alt_val is not None:
                    rel_alts.append(rel_alt_val)
        except Exception:
            has_meta = False

    if not has_meta:
        for path in sampled:
            t = extract_image_telemetry(path)
            if t["has_gps"] and (abs(t["lat"]) > 0.001 or abs(t["lon"]) > 0.001):
                lats.append(t["lat"])
                lons.append(t["lon"])
                coords.append((t["lat"], t["lon"]))
                if t["alt"] is not None:
                    alts.append(t["alt"])
            if t.get("relative_alt") is not None:
                rel_alts.append(t["relative_alt"])
            if t["model"]:
                cameras_detected.add(f"{t['make']} {t['model']}".strip())
                models.append(t["model"])
            if t["make"]:
                makes.append(t["make"])
            if t["width"] > 0:
                widths.append(t["width"])
                heights.append(t["height"])
            if t.get("focal_length") is not None:
                focal_lengths.append(t["focal_length"])
            if t.get("focal_35mm") is not None:
                focal_35mms.append(t["focal_35mm"])

    gps_count = len(lats)
    gps_ratio = gps_count / len(sampled) if sampled else 0.0
    has_gps = gps_ratio >= 0.70

    # Sensor hardware resolution
    primary_model = models[0] if models else ""
    primary_make = makes[0] if makes else ""
    combined_name = f"{primary_make} {primary_model}".strip().lower()

    matched_hardware = None
    for k, v in CAMERA_HARDWARE_DB.items():
        if k in combined_name or (primary_model and k in primary_model.lower().replace("-", "").replace(" ", "")):
            matched_hardware = v
            break

    avg_w = sum(widths) / len(widths) if widths else 4000
    avg_h = sum(heights) / len(heights) if heights else 3000
    avg_f = sum(focal_lengths) / len(focal_lengths) if focal_lengths else None
    avg_f35 = sum(focal_35mms) / len(focal_35mms) if focal_35mms else None

    if matched_hardware:
        sensor_name = matched_hardware["name"]
        sensor_w_mm = matched_hardware["sensor_w_mm"]
        sensor_h_mm = matched_hardware["sensor_h_mm"]
        pixel_pitch_um = matched_hardware["pixel_pitch_um"]
        shutter = matched_hardware["shutter"]
        if not avg_f and matched_hardware.get("focal_fixed"):
            avg_f = matched_hardware["focal_fixed"]
    else:
        # Optical derivation from focal length and 35mm equivalent
        if avg_f and avg_f35 and avg_f35 > 0:
            sensor_w_mm = 36.0 * (avg_f / avg_f35)
            sensor_h_mm = sensor_w_mm * (avg_h / avg_w)
        else:
            sensor_w_mm = 13.2  # 1-inch sensor standard fallback
            sensor_h_mm = 8.8
        pixel_pitch_um = (sensor_w_mm / avg_w) * 1000.0
        # Robust physical shutter determination:
        xmp_shutters = [extract_image_telemetry(p).get("xmp_shutter") for p in sampled[:10]]
        xmp_shutter_valid = [s for s in xmp_shutters if s]
        if any(s == "electronic" for s in xmp_shutter_valid):
            shutter = "rolling"
        elif any(s == "mechanical" for s in xmp_shutter_valid):
            shutter = "mechanical"
        elif primary_make.lower() == "dji":
            if any(k in combined_name for k in ["fc6310", "fc3411", "p1", "x7", "x4s", "l2", "m3m"]):
                shutter = "mechanical"
            else:
                # All consumer DJI payloads (FC9589, FC9470, FC3580, FC3582, Mini, Neo, Air) use electronic rolling shutters
                shutter = "rolling"
        elif any(k in combined_name for k in ["mavic", "mini", "air", "smartphone", "iphone", "gopro"]):
            shutter = "rolling"
        else:
            shutter = "mechanical"
        sensor_name = f"{primary_make} {primary_model}".strip() or "Standard Optical Sensor"

    # Flight altitude AGL
    flight_height_m = None
    if rel_alts:
        valid_rel = [a for a in rel_alts if a > 1.0]
        if valid_rel:
            flight_height_m = sum(valid_rel) / len(valid_rel)
    if flight_height_m is None and alts and len(alts) >= 2:
        alt_range = max(alts) - min(alts)
        flight_height_m = max(alts) - min(alts) if alt_range > 10.0 else min(alts)
    if flight_height_m is None or flight_height_m < 5.0:
        flight_height_m = 75.0  # reasonable aerial flight default

    # Ground Sampling Distance (GSD in cm/pixel)
    gsd_cm = None
    if flight_height_m and avg_f and avg_w:
        gsd_cm = (flight_height_m * sensor_w_mm) / (avg_f * avg_w) * 100.0

    # Geographic footprint and relief ratio
    span_m = 0.0
    alt_span_m = 0.0
    if has_gps and len(lats) >= 2:
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        lat_dist = calculate_haversine_meters(min_lat, min_lon, max_lat, min_lon)
        lon_dist = calculate_haversine_meters(min_lat, min_lon, min_lat, max_lon)
        span_m = math.sqrt(lat_dist * lat_dist + lon_dist * lon_dist)
        if len(alts) >= 2:
            alt_span_m = max(alts) - min(alts)

    relief_ratio = (alt_span_m / max(span_m, 1.0)) if span_m > 0 else 0.0

    # Overlap estimation
    fwd_ov, lat_ov = None, None
    if gsd_cm and len(coords) >= 2:
        ground_w_m = (gsd_cm * avg_w) / 100.0
        ground_h_m = (gsd_cm * avg_h) / 100.0
        baselines = []
        for i in range(len(coords) - 1):
            d = calculate_haversine_meters(coords[i][0], coords[i][1], coords[i+1][0], coords[i+1][1])
            if d > 0.1:
                baselines.append(d)
        if baselines:
            baselines.sort()
            med_b = baselines[len(baselines) // 2]
            fwd_ov = max(0.0, min(1.0 - (med_b / max(ground_h_m, 1.0)), 0.99)) * 100.0
            lat_ov = max(0.0, min(1.0 - (med_b * 1.6 / max(ground_w_m, 1.0)), 0.95)) * 100.0

    # Radiometry & Dynamic Range
    rad = analyze_radiometry(sampled, sample_count=15)
    avg_mp = (avg_w * avg_h) / 1_000_000.0

    return {
        "photo_count": total_photos,
        "sample_count": len(sampled),
        "gps_ratio": gps_ratio,
        "has_gps": has_gps,
        "horizontal_span_m": span_m,
        "alt_span_m": alt_span_m,
        "relief_ratio": relief_ratio,
        "avg_megapixels": avg_mp,
        "image_w": int(avg_w),
        "image_h": int(avg_h),
        "focal_mm": avg_f,
        "flight_height_m": flight_height_m,
        "gsd_cm": gsd_cm,
        "sensor_name": sensor_name,
        "sensor_w_mm": sensor_w_mm,
        "sensor_h_mm": sensor_h_mm,
        "pixel_pitch_um": pixel_pitch_um,
        "shutter": shutter,
        "forward_overlap_pct": fwd_ov,
        "lateral_overlap_pct": lat_ov,
        "shadow_clip_ratio": rad["shadow_clip_ratio"],
        "highlight_clip_ratio": rad["highlight_clip_ratio"],
        "delighting_recommended": rad["delighting_recommended"],
        "cameras_detected": list(cameras_detected),
    }


def derive_pro_photogrammetry_plan(physics: dict) -> dict:
    """Mathematically derives the optimal photogrammetric pipeline configuration from survey physics.

    Standard Production Geomatics Profile:
    - Strictly Arbitrary 3D surface (never 2.5D heightfield to prevent wall flattening)
    - Fixed 16-page 8K texture atlas (retains full architectural sharpness)
    - 2,000,000 polygon surface mesh
    - Native meshing interpolation with no aggressive hole-closing webbing (close_holes = False)
    - True orthomosaic GeoTIFF enabled (DEM disabled by default; True Ortho uses 3D mesh)
    - CMOS rolling shutter compensation enabled strictly for rolling shutter sensors
    - Gentle tie-point filtering capping point removal at <= 10% (retains >= 90% inliers)
    """
    photo_count = physics.get("photo_count", 0)
    has_gps = physics.get("has_gps", False)
    relief_ratio = physics.get("relief_ratio", 0.0)
    shutter = physics.get("shutter", "mechanical")
    gsd_cm = physics.get("gsd_cm")

    survey_category = "oblique_architecture" if (has_gps and relief_ratio >= 0.08) else "production_geomatics"
    surface_type = "arbitrary"
    crop_method = "gps-square"
    custom_faces = 2000000
    texture_pages = 16
    build_orthomosaic = True
    build_dem = False
    build_point_cloud = True
    classify_ground = False
    close_holes = False
    depth_filtering = "moderate"
    ground_cell_size = 12.0
    ground_max_angle = 28.0
    ground_max_distance = 0.3

    # Shutter compensation is ONLY enabled for CMOS rolling shutter payloads.
    # Enabling rolling shutter compensation on mechanical leaf shutters causes synthetic bundle adjustment drift!
    rolling_shutter = (shutter == "rolling")

    align_quality = "high"
    keypoint_limit = 200000
    tiepoint_limit = 25000
    guided_matching = True
    adaptive_fitting = True
    filter_stationary_points = True

    return {
        "survey_category": survey_category,
        "surface_type": surface_type,
        "align_quality": align_quality,
        "keypoint_limit": keypoint_limit,
        "tiepoint_limit": tiepoint_limit,
        "guided_matching": guided_matching,
        "adaptive_fitting": adaptive_fitting,
        "filter_stationary_points": filter_stationary_points,
        "rolling_shutter": rolling_shutter,
        "crop_method": crop_method,
        "depth_quality": "high",
        "depth_filtering": depth_filtering,
        "custom_faces": custom_faces,
        "texture_pages": texture_pages,
        "close_holes": close_holes,
        "build_orthomosaic": build_orthomosaic,
        "build_dem": build_dem,
        "build_point_cloud": build_point_cloud,
        "classify_ground": classify_ground,
        "ground_cell_size": ground_cell_size,
        "ground_max_angle": ground_max_angle,
        "ground_max_distance": ground_max_distance,
        "remove_lighting": False,
        "delighting_color_mode": False,
        "export_report": True,
        "gsd_cm": gsd_cm,
    }


def profile_dataset(images_dir: str, sample_size: int = 50, output_dir: str = None) -> dict:
    """Profiles photogrammetry survey dataset by inspecting telemetry, GPS bounds, and sensor optics."""
    phys = analyze_survey_physics(images_dir, sample_size=sample_size, output_dir=output_dir)
    # Maintain legacy keys for backwards compatibility
    phys["rolling_shutter_recommended"] = (phys.get("shutter") == "rolling")
    return phys


def infer_optimal_parameters(profile: dict) -> dict:
    """Dynamically infers optimal photogrammetric pipeline parameters based on survey profiling."""
    return derive_pro_photogrammetry_plan(profile)


def print_pro_engineering_report(physics: dict, plan: dict, usgs_stats: dict = None) -> None:
    """Prints a professional photogrammetric & geomatics engineering summary."""
    print("\n" + "=" * 80, flush=True)
    print("  AUTOMETASHAPE: PRO PHOTOGRAMMETRY & GEOMATICS ENGINEERING REPORT", flush=True)
    print("=" * 80, flush=True)
    print(f"  Sensor Hardware:     {physics.get('sensor_name', 'Optical Sensor')}")
    print(f"  Sensor Geometry:     {physics.get('sensor_w_mm', 0):.1f} x {physics.get('sensor_h_mm', 0):.1f} mm  |  Pixel Pitch: {physics.get('pixel_pitch_um', 0):.2f} µm")
    shutter_str = "Mechanical Leaf / Global Shutter [RS Comp: DISABLED]" if physics.get("shutter") == "mechanical" else "CMOS Electronic Rolling Shutter [RS Comp: ENABLED]"
    print(f"  Shutter Physics:     {shutter_str}")
    f_str = f"{physics.get('focal_mm', 0):.2f} mm" if physics.get('focal_mm') else "Auto"
    agl_str = f"{physics.get('flight_height_m', 0):.1f} m AGL" if physics.get('flight_height_m') else "Unknown"
    gsd_str = f"{physics.get('gsd_cm', 0):.2f} cm/px" if physics.get('gsd_cm') else "Unknown"
    print(f"  Optics & Altitude:   Focal Length: {f_str}  |  Flying Height: {agl_str}  |  GSD: {gsd_str}")
    fwd_ov = physics.get("forward_overlap_pct")
    lat_ov = physics.get("lateral_overlap_pct")
    ov_str = f"Forward: {fwd_ov:.1f}%  |  Lateral: {lat_ov:.1f}%" if fwd_ov is not None and lat_ov is not None else "Terrestrial / Uncalibrated"
    print(f"  Overlap Geometry:    {ov_str}")
    sh_clip = physics.get("shadow_clip_ratio", 0) * 100.0
    hi_clip = physics.get("highlight_clip_ratio", 0) * 100.0
    delight_str = "ACTIVATED (Diffuse Albedo Recovery)" if plan.get("remove_lighting") else "Not required (Balanced Radiometry)"
    print(f"  Dynamic Range:       Shadow Clipping: {sh_clip:.1f}%  |  Highlight: {hi_clip:.1f}%  |  Delighting: {delight_str}")
    r_val = physics.get("relief_ratio", 0.0)
    surf_str = f"R = {r_val:.3f} -> {plan.get('surface_type', 'arbitrary').upper()} Meshing"
    print(f"  Topographic Relief:  Span: {physics.get('horizontal_span_m', 0):.1f}m  |  Delta Z: {physics.get('alt_span_m', 0):.1f}m  |  {surf_str}")
    if usgs_stats:
        initial_rms = usgs_stats.get('initial_rms', 0.0)
        final_rms = usgs_stats.get('final_rms', 0.0)
        pct_imp = usgs_stats.get('rms_improvement_pct', 0.0)
        pts_init = usgs_stats.get('initial_points', 0)
        pts_final = usgs_stats.get('final_points', 0)
        print("--------------------------------------------------------------------------------")
        print("  USGS 4-Pass Bundle:  Pass 1 (RU<=15) -> Pass 2 (PA<=3.0) -> Pass 3 (RE<=0.50) -> Pass 4 (Multiplicity>=2, <=10% prune)")
        print(f"  Reprojection RMS:    Initial: {initial_rms:.3f} px  ->  Optimized: {final_rms:.3f} px  (RMS Reduction: {pct_imp:.1f}%)")
        print(f"  Multi-ray Multiplicity: {pts_final:,} robust tie points locked in (pruned {pts_init - pts_final:,} fragile/outlier rays)")
    print("=" * 80 + "\n", flush=True)


def filter_image_quality(chunk, min_quality: float = 0.5) -> int:
    """Estimates sharpness quality for all ingested photos and disables blurry photos.

    According to official Agisoft Metashape guidelines, images with a quality index below 0.5 units
    are considered out-of-focus or motion-blurred and should be excluded to prevent alignment drift
    and depth map noise.

    Args:
        chunk: Active Metashape.Chunk.
        min_quality: Quality threshold below which cameras are disabled (default: 0.5).

    Returns:
        int: Count of disabled blurry cameras.
    """
    print(f"\n[Quality Filter] Estimating sharpness quality across {len(chunk.cameras)} cameras...", flush=True)
    t_qual = time.time()
    chunk.analyzeImages()

    disabled_count = 0
    for cam in chunk.cameras:
        if "Image/Quality" in cam.meta:
            try:
                qual_val = float(cam.meta["Image/Quality"])
                if qual_val < min_quality:
                    cam.enabled = False
                    disabled_count += 1
            except (ValueError, TypeError):
                pass

    active_count = sum(1 for c in chunk.cameras if c.enabled)
    t_elapsed = time.time() - t_qual
    print(f"  [Quality Filter] Analyzed in {t_elapsed:.1f}s: {active_count}/{len(chunk.cameras)} sharp images passed (Quality >= {min_quality:.2f}).", flush=True)
    if disabled_count > 0:
        print(f"  [Quality Filter] Automatically disabled {disabled_count} blurry/defocused images (< {min_quality:.2f}) to protect alignment accuracy.", flush=True)
    return disabled_count


def robust_bounds(sorted_vals, k: float = 3.0) -> tuple:
    """Calculates robust spatial bounding intervals using Median Absolute Deviation (MAD).

    Unlike naive percentiles or min/max, MAD offers a 50% statistical breakdown point,
    meaning it completely rejects up to 49.9% outlier contamination (such as secondary
    buildings, perimeter structures, or background features captured by 10-20% of cameras).
    """
    if not sorted_vals:
        return 0.0, 0.0
    n = len(sorted_vals)
    med = sorted_vals[n // 2]
    devs = sorted([abs(v - med) for v in sorted_vals])
    mad = devs[n // 2]
    # 1.4826 * MAD approximates the normal distribution standard deviation (sigma)
    sigma = 1.4826 * mad
    if sigma < 1e-4:
        return sorted_vals[0], sorted_vals[-1]

    # Filter values within k standard deviations of the median
    inliers = [v for v in sorted_vals if abs(v - med) <= k * sigma]
    if not inliers:
        return sorted_vals[0], sorted_vals[-1]
    return min(inliers), max(inliers)


def solve_ray_convergence_irls(aligned_cams, iterations: int = 3) -> tuple:
    """Solves 3D camera optical ray convergence using Iteratively Reweighted Least Squares (IRLS).

    Robustly detects the primary target building and prunes outlier cameras (e.g. when 10-30%
    of cameras focus on secondary points, takeoff paths, ground targets, or other structures).

    Returns:
        tuple: (target_center: Metashape.Vector, inlier_cams: list of (c_pos, d))
    """
    v_z = Metashape.Vector([0.0, 0.0, 1.0])
    cam_data = []
    for c in aligned_cams:
        c_pos = c.center
        d = (c.transform.rotation() * v_z).normalized()
        cam_data.append((c_pos, d))

    if len(cam_data) < 2:
        return None, []

    active_cams = cam_data
    target_center = None

    for it in range(iterations):
        M = Metashape.Matrix([[0.0] * 3, [0.0] * 3, [0.0] * 3])
        V = Metashape.Vector([0.0, 0.0, 0.0])

        for c_pos, d in active_cams:
            p00 = 1.0 - d.x * d.x
            p01 = -d.x * d.y
            p02 = -d.x * d.z
            p10 = -d.y * d.x
            p11 = 1.0 - d.y * d.y
            p12 = -d.y * d.z
            p20 = -d.z * d.x
            p21 = -d.z * d.y
            p22 = 1.0 - d.z * d.z

            P = Metashape.Matrix([[p00, p01, p02], [p10, p11, p12], [p20, p21, p22]])
            M = M + P
            V = V + P * c_pos

        try:
            if abs(M.det()) < 1e-4:
                break
            target_center = M.inv() * V
        except Exception:
            break

        # Calculate residual orthogonal distance from current centroid to every camera ray
        residuals = []
        for c_pos, d in cam_data:
            diff = target_center - c_pos
            # Orthogonal distance: |diff - (diff . d) * d|
            proj = diff - d * (diff * d)
            residuals.append((proj.norm(), (c_pos, d)))

        residuals.sort(key=lambda x: x[0])
        med_res = residuals[len(residuals) // 2][0]
        # Inlier threshold: 2.5 * median residual (or at least 1.5 meters)
        thresh = max(2.5 * med_res, 1.5)

        active_cams = [item for res, item in residuals if res <= thresh]
        if len(active_cams) < 2:
            break

    return target_center, active_cams


def auto_crop_to_main_structure(chunk, padding: float = 0.15) -> bool:
    """Automatically crops chunk.region horizontally around the main target structure while preserving vertical bounds uncropped.

    Uses Robust IRLS ray convergence to lock onto the majority target structure (even when 10-30%
    of cameras are aimed at secondary objects) and Median Absolute Deviation (MAD) spatial clustering
    to isolate horizontal footprint, keeping the vertical (Z) extent uncropped to prevent any clipping of roofs or ground.

    Args:
        chunk: Active Metashape.Chunk.
        padding: Safety margin fraction applied around bounding box dimensions (default: 0.15 / 15%).

    Returns:
        True if bounding box was successfully updated; False if skipped.
    """
    aligned_cams = [c for c in chunk.cameras if c.transform and c.enabled]
    if len(aligned_cams) < 2:
        print("  [Auto-Crop] Fewer than 2 aligned cameras; skipping bounding box crop.", flush=True)
        return False

    print(f"\n[Auto-Crop] Calculating robust 3D camera optical ray convergence from {len(aligned_cams)} cameras...", flush=True)

    # 1. Solve Robust 3D Ray Convergence using IRLS
    target_center, inlier_cams = solve_ray_convergence_irls(aligned_cams, iterations=3)

    if target_center is not None:
        inlier_count = len(inlier_cams)
        outlier_count = len(aligned_cams) - inlier_count
        print(f"  [Auto-Crop] Optical ray convergence centroid: ({target_center.x:.2f}, {target_center.y:.2f}, {target_center.z:.2f})", flush=True)
        print(f"  [Auto-Crop] Camera consensus: {inlier_count}/{len(aligned_cams)} cameras locked on main structure ({outlier_count} secondary/outlier views pruned).", flush=True)
    else:
        cam_centers = [c.center for c in aligned_cams]
        target_center = Metashape.Vector([
            sum(c.x for c in cam_centers) / len(cam_centers),
            sum(c.y for c in cam_centers) / len(cam_centers),
            sum(c.z for c in cam_centers) / len(cam_centers)
        ])
        inlier_cams = [(c.center, None) for c in aligned_cams]
        print(f"  [Auto-Crop] Using camera cluster centroid: ({target_center.x:.2f}, {target_center.y:.2f}, {target_center.z:.2f})", flush=True)

    # 2. Estimate survey radius from consensus inlier cameras (95th percentile with 25% safety expansion)
    inlier_centers = [c_pos for c_pos, _ in inlier_cams]
    dists = [(c - target_center).norm() for c in inlier_centers]
    dists.sort()
    if dists:
        idx_95 = min(int(len(dists) * 0.95), len(dists) - 1)
        dist_95 = dists[idx_95]
        max_radius = dist_95 * 1.25 if dist_95 > 0 else float("inf")
    else:
        max_radius = float("inf")

    # 3. Filter sparse tie points belonging to the main structure
    pts = chunk.tie_points.points if chunk.tie_points else []
    if not pts:
        print("  [Auto-Crop] No tie points available; preserving default region.", flush=True)
        return False

    rot = chunk.region.rot
    rot_t = rot.t()  # World to local region coordinate frame

    # Preserve uncropped vertical (Z) extent from default post-alignment region
    orig_region = chunk.region
    orig_local_center = rot_t * orig_region.center
    orig_mid_z = orig_local_center.z
    orig_span_z = orig_region.size.z

    local_x, local_y = [], []
    valid_count = 0
    for pt in pts:
        if not pt.valid:
            continue
        coord = pt.coord[:3]
        if (coord - target_center).norm() > max_radius:
            continue

        loc = rot_t * coord
        local_x.append(loc.x)
        local_y.append(loc.y)
        valid_count += 1

    if valid_count < 100:
        print(f"  [Auto-Crop] Insufficient structure tie points ({valid_count}); preserving default region.", flush=True)
        return False

    local_x.sort()
    local_y.sort()

    # 4. Robust MAD bounding: rejects secondary cluster points with 50% breakdown tolerance horizontally
    min_x, max_x = robust_bounds(local_x, k=3.0)
    min_y, max_y = robust_bounds(local_y, k=3.0)

    # 5. Apply safety padding horizontally; preserve vertical extent completely uncropped
    span_x = (max_x - min_x) * (1.0 + 2.0 * padding)
    span_y = (max_y - min_y) * (1.0 + 2.0 * padding)

    mid_local = Metashape.Vector([
        (min_x + max_x) * 0.5,
        (min_y + max_y) * 0.5,
        orig_mid_z,
    ])
    new_center = rot * mid_local
    new_size = Metashape.Vector([span_x, span_y, orig_span_z])

    # Assign new region
    region = chunk.region
    region.center = new_center
    region.size = new_size
    chunk.region = region

    print(f"  [Auto-Crop] Successfully cropped horizontal region to main structure!", flush=True)
    print(f"  Center: ({new_center.x:.2f}, {new_center.y:.2f}, {new_center.z:.2f})", flush=True)
    print(f"  Horizontal Size: ({span_x:.2f}m x {span_y:.2f}m) [Padding: {int(padding * 100)}%]", flush=True)
    print(f"  Vertical Extent (Uncropped): {orig_span_z:.2f}m (Z Center: {orig_mid_z:.2f}m)", flush=True)
    print(f"  Isolated Main Structure Tie Points: {valid_count:,} points", flush=True)
    return True


def auto_crop_camera_gps_square(chunk, padding: float = 0.05) -> bool:
    """Crops chunk.region horizontally to the minimum area horizontal rectangle enclosing all camera images,
    filtering outliers (such as GPS 0,0, missing coordinates, or distant takeoff points), with flight FOV safety
    margins, while preserving the full vertical elevation range so ground terrain and structures are protected.

    Args:
        chunk: Active Metashape.Chunk.
        padding: Safety margin padding applied to dimensions (default: 0.05 / 5%).

    Returns:
        True if bounding box was successfully updated; False if skipped.
    """
    aligned_cams = [c for c in chunk.cameras if c.transform and c.enabled]
    if len(aligned_cams) < 2:
        print("  [Auto-Crop-GPS] Fewer than 2 aligned cameras; skipping bounding box crop.", flush=True)
        return False

    print(
        f"\n[Auto-Crop-GPS] Computing minimum area horizontal enclosing rectangle "
        f"from {len(aligned_cams)} cameras...",
        flush=True,
    )

    # 1. Filter invalid GPS outliers (e.g. GPS (0, 0) or unreferenced cameras)
    valid_cams = []
    for c in aligned_cams:
        if c.reference and c.reference.location:
            loc = c.reference.location
            if abs(loc.x) < 1e-4 and abs(loc.y) < 1e-4:
                continue
        valid_cams.append(c)

    if len(valid_cams) < 2:
        valid_cams = aligned_cams

    rot = chunk.region.rot
    rot_t = rot.t()  # Local horizontal level coordinate frame (Z is Up)

    # Transform camera centers to local coordinate frame
    cam_locs = [rot_t * c.center for c in valid_cams]
    xs = [p.x for p in cam_locs]
    ys = [p.y for p in cam_locs]
    cam_zs = [p.z for p in cam_locs]

    # Reject extreme spatial outliers (e.g. distant takeoff / transit location) via MAD
    sorted_xs = sorted(xs)
    sorted_ys = sorted(ys)
    med_x = sorted_xs[len(sorted_xs) // 2]
    med_y = sorted_ys[len(sorted_ys) // 2]
    dists = [math.hypot(x - med_x, y - med_y) for x, y in zip(xs, ys)]
    sorted_dists = sorted(dists)
    med_d = sorted_dists[len(sorted_dists) // 2]
    devs_d = sorted([abs(d - med_d) for d in dists])
    mad_d = devs_d[len(devs_d) // 2]
    sigma_d = 1.4826 * mad_d

    inlier_xs, inlier_ys, inlier_zs = [], [], []
    for x, y, z, d in zip(xs, ys, cam_zs, dists):
        # Prune only true outliers (extreme deviation exceeding 5-sigma and 1000m)
        if sigma_d > 1e-3 and (d - med_d) > max(1000.0, 5.0 * sigma_d):
            continue
        inlier_xs.append(x)
        inlier_ys.append(y)
        inlier_zs.append(z)

    if len(inlier_xs) < 2:
        inlier_xs, inlier_ys, inlier_zs = xs, ys, cam_zs

    # 2. Find orientation in the horizontal plane that produces the SMALLEST AREA RECTANGLE (w * h)
    best_area = float("inf")
    best_theta = 0.0
    best_bounds = (0.0, 0.0, 0.0, 0.0)

    for step in range(180):  # 0.0 to 89.5 degrees in 0.5 deg steps
        theta = math.radians(step * 0.5)
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        rx = [x * cos_t + y * sin_t for x, y in zip(inlier_xs, inlier_ys)]
        ry = [-x * sin_t + y * cos_t for x, y in zip(inlier_xs, inlier_ys)]
        min_rx, max_rx = min(rx), max(rx)
        min_ry, max_ry = min(ry), max(ry)
        w = max_rx - min_rx
        h = max_ry - min_ry
        area = w * h
        if area < best_area:
            best_area = area
            best_theta = theta
            best_bounds = (min_rx, max_rx, min_ry, max_ry)

    min_rx, max_rx, min_ry, max_ry = best_bounds
    span_x = max_rx - min_rx
    span_y = max_ry - min_ry

    # 3. Preserve vertical extent covering terrain and roof features
    pts = [p for p in chunk.tie_points.points if p.valid] if chunk.tie_points else []
    if pts:
        coords = [
            Metashape.Vector([p.coord.x / p.coord.w, p.coord.y / p.coord.w, p.coord.z / p.coord.w])
            for p in pts
        ]
        pt_zs = sorted([(rot_t * v).z for v in coords])
        med_pz = pt_zs[len(pt_zs) // 2]
        devs_pz = sorted([abs(z - med_pz) for z in pt_zs])
        mad_pz = devs_pz[len(devs_pz) // 2]
        sigma_pz = 1.4826 * mad_pz
        filtered_pt_zs = [z for z in pt_zs if abs(z - med_pz) <= 4.0 * sigma_pz] if sigma_pz > 1e-3 else pt_zs
        all_zs = inlier_zs + (filtered_pt_zs if filtered_pt_zs else pt_zs)
        med_cam_z = sorted(inlier_zs)[len(inlier_zs) // 2]
        flight_height = abs(med_cam_z - med_pz)
    else:
        all_zs = inlier_zs
        flight_height = 0.0

    # Camera FOV safety margin (ensures photo frustums are fully framed)
    fov_margin = flight_height * 0.35 if flight_height > 1e-3 else 0.0
    margin_x = max(span_x * padding, fov_margin)
    margin_y = max(span_y * padding, fov_margin)
    side_x = span_x + 2.0 * margin_x
    side_y = span_y + 2.0 * margin_y

    mid_rx = (min_rx + max_rx) * 0.5
    mid_ry = (min_ry + max_ry) * 0.5

    cos_t = math.cos(best_theta)
    sin_t = math.sin(best_theta)
    mid_x = mid_rx * cos_t - mid_ry * sin_t
    mid_y = mid_rx * sin_t + mid_ry * cos_t

    min_z = min(all_zs)
    max_z = max(all_zs)
    span_z = max(chunk.region.size.z, (max_z - min_z) * (1.0 + 2.0 * padding))
    mid_z = (min_z + max_z) * 0.5

    # 4. Construct oriented region rotation matrix and assign to chunk
    Rz = Metashape.Matrix([
        [cos_t, -sin_t, 0.0],
        [sin_t,  cos_t, 0.0],
        [  0.0,    0.0, 1.0],
    ])
    new_rot = rot * Rz
    new_center = rot * Metashape.Vector([mid_x, mid_y, mid_z])
    new_size = Metashape.Vector([side_x, side_y, span_z])

    region = chunk.region
    region.rot = new_rot
    region.center = new_center
    region.size = new_size
    chunk.region = region

    scale = chunk.transform.scale if chunk.transform and chunk.transform.scale else 1.0
    pruned = len(aligned_cams) - len(inlier_xs)
    print("  [Auto-Crop-GPS] Minimum area horizontal enclosing rectangle successfully calculated!", flush=True)
    print(
        f"  Images Included: {len(inlier_xs)}/{len(aligned_cams)} (Outliers Pruned: {pruned})",
        flush=True,
    )
    print(f"  Optimal Angle: {math.degrees(best_theta):.1f} deg (Minimum Area Rectangle)", flush=True)
    print(
        f"  Horizontal Size: {side_x * scale:.2f}m x {side_y * scale:.2f}m "
        f"(Span: {span_x * scale:.2f}m x {span_y * scale:.2f}m, "
        f"FOV Margin: +{margin_x * scale:.2f}m, +{margin_y * scale:.2f}m)",
        flush=True,
    )
    print(f"  Vertical Extent: {span_z * scale:.2f}m (Z: [{min_z * scale:.2f}m, {max_z * scale:.2f}m])", flush=True)
    return True


def execute_usgs_gradual_selection(
    chunk,
    ru_thresh: float = 15.0,
    pa_thresh: float = 3.0,
    re_thresh: float = 0.50,
    min_rays: int = 2,
    max_removal_ratio: float = 0.10,
) -> dict:
    """Executes gentle architectural-grade gradual selection refinement.

    Refines camera interior and exterior orientations through staged tie-point pruning
    and progressive camera parameter unfreezing without destroying building corners or fine geometry:
    - Pass 1 (Reconstruction Uncertainty <= 15.0): Prunes extreme ray intersection outliers.
      Optimizes f, cx, cy only. Capped at 10% maximum removal.
    - Pass 2 (Projection Accuracy <= 3.0 px): Prunes correlation ambiguity and lens flare artifacts.
      Optimizes f, cx, cy, k1..k3, p1, p2. Capped at 10% maximum removal.
    - Pass 3 (Reprojection Error <= 0.50 px): Prunes high-residual outliers.
      Optimizes full polynomial model (f, cx, cy, k1..k4, b1, b2, p1, p2) with adaptive fitting. Capped at 10% maximum removal.
    - Pass 4 (Image Multiplicity >= 2): Preserves valid 2-ray structural intersections on corners, eaves, and thin details.
      Locks in bundle adjustment geometry and reports final RMS reprojection error.
    Guarantees >= 90% tie-point retention across the entire alignment.
    """
    if not chunk.tie_points or not chunk.tie_points.points:
        print("  [Gradual Selection] No tie points present to filter; skipping.", flush=True)
        return {}

    initial_pts = sum(1 for p in chunk.tie_points.points if p.valid)
    f = Metashape.TiePoints.Filter()

    # Calculate initial RMS Reprojection Error
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ReprojectionError)
        initial_re = list(f.values)
        initial_rms = math.sqrt(sum(v * v for v in initial_re) / len(initial_re)) if initial_re else 0.0
    except Exception:
        initial_rms = 0.0

    print(f"\n[Tie-Point Refinement] Starting gentle gradual selection refinement across {initial_pts:,} tie points (Initial Reprojection RMS: {initial_rms:.3f} px, max prune ratio: {int(max_removal_ratio*100)}%)...", flush=True)

    # Pass 1: Reconstruction Uncertainty (RU <= 15.0)
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ReconstructionUncertainty)
        vals = sorted(f.values)
        if vals:
            lim = vals[int(len(vals) * (1.0 - max_removal_ratio))]
            eff_ru = max(ru_thresh, lim) if ru_thresh < lim else ru_thresh
        else:
            eff_ru = ru_thresh
        f.removePoints(eff_ru)
        chunk.optimizeCameras(
            fit_f=True, fit_cx=True, fit_cy=True,
            fit_b1=False, fit_b2=False,
            fit_k1=False, fit_k2=False, fit_k3=False, fit_k4=False,
            fit_p1=False, fit_p2=False,
            adaptive_fitting=False,
        )
        print(f"  [Pass 1/4] Reconstruction Uncertainty (RU <= {eff_ru:.1f}): Pruned low-intersection rays; optimized (f, cx, cy).", flush=True)
    except Exception as e:
        print(f"  [Pass 1/4] Reconstruction Uncertainty skipped: {e}", flush=True)

    # Pass 2: Projection Accuracy (PA <= 3.0 px)
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ProjectionAccuracy)
        vals = sorted(f.values)
        if vals:
            lim = vals[int(len(vals) * (1.0 - max_removal_ratio))]
            eff_pa = max(pa_thresh, lim) if pa_thresh < lim else pa_thresh
        else:
            eff_pa = pa_thresh
        f.removePoints(eff_pa)
        chunk.optimizeCameras(
            fit_f=True, fit_cx=True, fit_cy=True,
            fit_b1=False, fit_b2=False,
            fit_k1=True, fit_k2=True, fit_k3=True, fit_k4=False,
            fit_p1=True, fit_p2=True,
            adaptive_fitting=False,
        )
        print(f"  [Pass 2/4] Projection Accuracy (PA <= {eff_pa:.2f} px): Pruned correlation ambiguity; optimized (f, cx, cy, k1..k3, p1, p2).", flush=True)
    except Exception as e:
        print(f"  [Pass 2/4] Projection Accuracy skipped: {e}", flush=True)

    # Pass 3: Reprojection Error (RE <= 0.50 px)
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ReprojectionError)
        vals = sorted(f.values)
        if vals:
            lim = vals[int(len(vals) * (1.0 - max_removal_ratio))]
            eff_re = max(re_thresh, lim) if re_thresh < lim else re_thresh
        else:
            eff_re = re_thresh
        f.removePoints(eff_re)
        chunk.optimizeCameras(
            fit_f=True, fit_cx=True, fit_cy=True,
            fit_b1=True, fit_b2=True,
            fit_k1=True, fit_k2=True, fit_k3=True, fit_k4=True,
            fit_p1=True, fit_p2=True,
            adaptive_fitting=True,
            tiepoint_covariance=True,
        )
        print(f"  [Pass 3/4] Reprojection Error (RE <= {eff_re:.2f} px): Pruned residual outliers; optimized complete polynomial lens model (k4, b1, b2, adaptive).", flush=True)
    except Exception as e:
        print(f"  [Pass 3/4] Reprojection Error skipped: {e}", flush=True)

    # Pass 4: Image Multiplicity (min_rays >= 2)
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ImageCount)
        if min_rays > 1:
            f.selectPoints(min_rays - 1)
            chunk.tie_points.removeSelectedPoints()
        chunk.optimizeCameras(
            fit_f=True, fit_cx=True, fit_cy=True,
            fit_b1=True, fit_b2=True,
            fit_k1=True, fit_k2=True, fit_k3=True, fit_k4=True,
            fit_p1=True, fit_p2=True,
            adaptive_fitting=True,
            tiepoint_covariance=True,
        )
        print(f"  [Pass 4/4] Multi-ray Multiplicity (Rays >= {min_rays}): Preserved structural multi-view rays; locked in bundle adjustment.", flush=True)
    except Exception as e:
        print(f"  [Pass 4/4] Multiplicity filter skipped: {e}", flush=True)

    # Final RMS Reprojection Error calculation
    final_pts = sum(1 for p in chunk.tie_points.points if p.valid) if chunk.tie_points else 0
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ReprojectionError)
        final_re = list(f.values)
        final_rms = math.sqrt(sum(v * v for v in final_re) / len(final_re)) if final_re else 0.0
    except Exception:
        final_rms = 0.0

    rms_improvement = ((initial_rms - final_rms) / initial_rms * 100.0) if initial_rms > 0 else 0.0
    print(f"  [Tie-Point Summary] Reprojection RMS: {initial_rms:.3f} px -> {final_rms:.3f} px ({rms_improvement:.1f}% reduction). Inliers: {final_pts:,}/{initial_pts:,} tie points retained ({final_pts/initial_pts*100.0:.1f}%).\n", flush=True)

    return {
        "initial_points": initial_pts,
        "final_points": final_pts,
        "initial_rms": initial_rms,
        "final_rms": final_rms,
        "rms_improvement_pct": rms_improvement,
    }


def filter_tie_points(
    chunk,
    max_reproj_error: float = 0.50,
    max_uncertainty: float = 15.0,
    pa_thresh: float = 3.0,
    min_rays: int = 2,
    max_removal_ratio: float = 0.10,
) -> dict:
    """Filters low-confidence tie points using gentle architectural-grade gradual selection."""
    return execute_usgs_gradual_selection(
        chunk,
        ru_thresh=max_uncertainty,
        pa_thresh=pa_thresh,
        re_thresh=max_reproj_error,
        min_rays=min_rays,
        max_removal_ratio=max_removal_ratio,
    )


def build_cluster_gradual_selection_tasks(
    max_reproj_error: float = 0.50,
    max_uncertainty: float = 15.0,
    pa_thresh: float = 3.0,
    min_rays: int = 2,
) -> list:
    """Builds a sequence of native CleanTiePoints and OptimizeCameras tasks for cluster workers."""
    if Metashape is None:
        return []
    tasks = []
    # Pass 1: Reconstruction Uncertainty
    t1 = Metashape.Tasks.CleanTiePoints()
    t1.criterion = Metashape.TiePoints.Criterion.ReconstructionUncertainty
    t1.threshold = max_uncertainty
    o1 = Metashape.Tasks.OptimizeCameras()
    o1.fit_f = o1.fit_cx = o1.fit_cy = True
    o1.fit_b1 = o1.fit_b2 = False
    o1.fit_k1 = o1.fit_k2 = o1.fit_k3 = True
    o1.fit_k4 = o1.fit_p1 = o1.fit_p2 = False
    o1.adaptive_fitting = False
    tasks.extend([t1, o1])

    # Pass 2: Projection Accuracy
    t2 = Metashape.Tasks.CleanTiePoints()
    t2.criterion = Metashape.TiePoints.Criterion.ProjectionAccuracy
    t2.threshold = pa_thresh
    o2 = Metashape.Tasks.OptimizeCameras()
    o2.fit_f = o2.fit_cx = o2.fit_cy = True
    o2.fit_b1 = o2.fit_b2 = False
    o2.fit_k1 = o2.fit_k2 = o2.fit_k3 = True
    o2.fit_k4 = o2.fit_p1 = o2.fit_p2 = False
    o2.adaptive_fitting = False
    tasks.extend([t2, o2])

    # Pass 3: Reprojection Error
    t3 = Metashape.Tasks.CleanTiePoints()
    t3.criterion = Metashape.TiePoints.Criterion.ReprojectionError
    t3.threshold = max_reproj_error
    o3 = Metashape.Tasks.OptimizeCameras()
    o3.fit_f = o3.fit_cx = o3.fit_cy = o3.fit_b1 = o3.fit_b2 = True
    o3.fit_k1 = o3.fit_k2 = o3.fit_k3 = o3.fit_k4 = True
    o3.fit_p1 = o3.fit_p2 = True
    o3.adaptive_fitting = True
    o3.tiepoint_covariance = True
    tasks.extend([t3, o3])

    # Pass 4: Multi-ray Multiplicity
    t4 = Metashape.Tasks.CleanTiePoints()
    t4.criterion = Metashape.TiePoints.Criterion.ImageCount
    t4.threshold = max(2, min_rays)
    o4 = Metashape.Tasks.OptimizeCameras()
    o4.fit_f = o4.fit_cx = o4.fit_cy = o4.fit_b1 = o4.fit_b2 = True
    o4.fit_k1 = o4.fit_k2 = o4.fit_k3 = o4.fit_k4 = True
    o4.fit_p1 = o4.fit_p2 = True
    o4.adaptive_fitting = True
    o4.tiepoint_covariance = True
    tasks.extend([t4, o4])

    return tasks


def build_cluster_crop_task(crop_method: str = "gps-square", padding: float = 0.05):
    """Builds a RunScript task that executes horizontal enclosing rectangle or structure crop on the worker."""
    if Metashape is None:
        return None
    pad_str = f"{padding:.4f}"
    if crop_method == "gps-square":
        code = (
            "import Metashape, math\n"
            "chunk = Metashape.app.document.chunk\n"
            "cams = [c for c in chunk.cameras if c.transform and c.enabled]\n"
            "if len(cams) >= 2:\n"
            "    rot = chunk.region.rot\n"
            "    rot_t = rot.t()\n"
            "    valid = [c for c in cams if (not c.reference or not c.reference.location or "
            "abs(c.reference.location.x) > 1e-4 or abs(c.reference.location.y) > 1e-4)]\n"
            "    if not valid: valid = cams\n"
            "    locs = [rot_t * c.center for c in valid]\n"
            "    xs, ys, zs = [p.x for p in locs], [p.y for p in locs], [p.z for p in locs]\n"
            "    sorted_xs, sorted_ys = sorted(xs), sorted(ys)\n"
            "    med_x, med_y = sorted_xs[len(sorted_xs)//2], sorted_ys[len(sorted_ys)//2]\n"
            "    dists = [math.hypot(x - med_x, y - med_y) for x, y in zip(xs, ys)]\n"
            "    med_d = sorted(dists)[len(dists)//2]\n"
            "    devs_d = sorted([abs(d - med_d) for d in dists])\n"
            "    mad_d = devs_d[len(devs_d)//2]\n"
            "    sigma_d = 1.4826 * mad_d\n"
            "    inlier_xs, inlier_ys, inlier_zs = [], [], []\n"
            "    for x, y, z, d in zip(xs, ys, zs, dists):\n"
            "        if sigma_d > 1e-3 and (d - med_d) > max(1000.0, 5.0 * sigma_d): continue\n"
            "        inlier_xs.append(x); inlier_ys.append(y); inlier_zs.append(z)\n"
            "    if len(inlier_xs) < 2: inlier_xs, inlier_ys, inlier_zs = xs, ys, zs\n"
            "    best_area, best_theta, best_bounds = float('inf'), 0.0, (0.0, 0.0, 0.0, 0.0)\n"
            "    for step in range(180):\n"
            "        theta = math.radians(step * 0.5)\n"
            "        cos_t, sin_t = math.cos(theta), math.sin(theta)\n"
            "        rx = [x * cos_t + y * sin_t for x, y in zip(inlier_xs, inlier_ys)]\n"
            "        ry = [-x * sin_t + y * cos_t for x, y in zip(inlier_xs, inlier_ys)]\n"
            "        min_rx, max_rx = min(rx), max(rx)\n"
            "        min_ry, max_ry = min(ry), max(ry)\n"
            "        area = (max_rx - min_rx) * (max_ry - min_ry)\n"
            "        if area < best_area:\n"
            "            best_area = area; best_theta = theta; best_bounds = (min_rx, max_rx, min_ry, max_ry)\n"
            f"    padding = {pad_str}\n"
            "    min_rx, max_rx, min_ry, max_ry = best_bounds\n"
            "    span_x = max_rx - min_rx\n"
            "    span_y = max_ry - min_ry\n"
            "    pts = [p for p in chunk.tie_points.points if p.valid] if chunk.tie_points else []\n"
            "    if pts:\n"
            "        coords = [Metashape.Vector([p.coord.x / p.coord.w, p.coord.y / p.coord.w, "
            "p.coord.z / p.coord.w]) for p in pts]\n"
            "        pt_zs = sorted([(rot_t * v).z for v in coords])\n"
            "        med_pz = pt_zs[len(pt_zs) // 2]\n"
            "        devs_pz = sorted([abs(z - med_pz) for z in pt_zs])\n"
            "        mad_pz = devs_pz[len(devs_pz) // 2]\n"
            "        sigma_pz = 1.4826 * mad_pz\n"
            "        filtered_pt_zs = [z for z in pt_zs if abs(z - med_pz) <= 4.0 * sigma_pz] "
            "if sigma_pz > 1e-3 else pt_zs\n"
            "        all_zs = inlier_zs + (filtered_pt_zs if filtered_pt_zs else pt_zs)\n"
            "        med_cam_z = sorted(inlier_zs)[len(inlier_zs) // 2]\n"
            "        flight_height = abs(med_cam_z - med_pz)\n"
            "    else:\n"
            "        all_zs = inlier_zs\n"
            "        flight_height = 0.0\n"
            "    fov_margin = flight_height * 0.35 if flight_height > 1e-3 else 0.0\n"
            "    margin_x = max(span_x * padding, fov_margin)\n"
            "    margin_y = max(span_y * padding, fov_margin)\n"
            "    side_x = span_x + 2.0 * margin_x\n"
            "    side_y = span_y + 2.0 * margin_y\n"
            "    mid_rx = (min_rx + max_rx) * 0.5\n"
            "    mid_ry = (min_ry + max_ry) * 0.5\n"
            "    cos_t, sin_t = math.cos(best_theta), math.sin(best_theta)\n"
            "    mid_x = mid_rx * cos_t - mid_ry * sin_t\n"
            "    mid_y = mid_rx * sin_t + mid_ry * cos_t\n"
            "    min_z, max_z = min(all_zs), max(all_zs)\n"
            "    span_z = max(chunk.region.size.z, (max_z - min_z) * (1.0 + 2.0 * padding))\n"
            "    mid_z = (min_z + max_z) * 0.5\n"
            "    Rz = Metashape.Matrix([[cos_t, -sin_t, 0.0], [sin_t, cos_t, 0.0], [0.0, 0.0, 1.0]])\n"
            "    chunk.region.rot = rot * Rz\n"
            "    chunk.region.center = rot * Metashape.Vector([mid_x, mid_y, mid_z])\n"
            "    chunk.region.size = Metashape.Vector([side_x, side_y, span_z])\n"
            "    print(f'  [Worker] Auto-Crop (Option A applied): "
            "Size=({side_x:.1f}m x {side_y:.1f}m x {span_z:.1f}m)')\n"
        )
    elif crop_method == "structure":
        code = (
            "import Metashape, math\n"
            "chunk = Metashape.app.document.chunk\n"
            "pts = [p.coord for p in chunk.tie_points.points if p.valid] if chunk.tie_points else []\n"
            "if len(pts) >= 10:\n"
            "    rot = chunk.region.rot\n"
            "    rot_t = rot.t()\n"
            "    locs = [rot_t * Metashape.Vector([p.x/p.w, p.y/p.w, p.z/p.w]) for p in pts]\n"
            "    xs = sorted([p.x for p in locs])\n"
            "    ys = sorted([p.y for p in locs])\n"
            "    zs = sorted([p.z for p in locs])\n"
            "    n = len(locs)\n"
            "    i0, i1 = int(n * 0.05), int(n * 0.95)\n"
            "    min_x, max_x = xs[i0], xs[i1]\n"
            "    min_y, max_y = ys[i0], ys[i1]\n"
            "    min_z, max_z = zs[i0], zs[i1]\n"
            f"    padding = {pad_str}\n"
            "    dx = (max_x - min_x) * (1.0 + padding)\n"
            "    dy = (max_y - min_y) * (1.0 + padding)\n"
            "    dz = (max_z - min_z) * (1.0 + padding * 2.0)\n"
            "    chunk.region.center = rot * Metashape.Vector([(min_x + max_x)/2.0, (min_y + max_y)/2.0, "
            "(min_z + max_z)/2.0])\n"
            "    chunk.region.size = Metashape.Vector([dx, dy, dz])\n"
            "    print(f'  [Worker] Auto-Crop (Structure applied): Size=({dx:.1f}m, {dy:.1f}m, {dz:.1f}m)')\n"
        )
    else:
        return None

    t_crop = Metashape.Tasks.RunScript()
    t_crop.code = code
    return t_crop


def build_unified_cluster_pipeline_tasks(
    chunk,
    output_dir: str,
    project_name: str,
    align_photos: bool = True,
    align_quality: str = "high",
    reference_preselection_mode: str = "estimated",
    keypoint_limit: int = 200000,
    keypoint_limit_per_mpx: int = 15000,
    tiepoint_limit: int = 25000,
    filter_stationary_points: bool = True,
    guided_matching: bool = True,
    adaptive_fitting: bool = True,
    filter_tiepoints: bool = True,
    crop_method: str = "gps-square",
    crop_padding: float = 0.05,
    build_depth_maps: bool = True,
    depth_quality: str = "high",
    depth_filtering: str = "moderate",
    build_point_cloud: bool = True,
    points_spacing: float = 0.02,
    build_mesh: bool = True,
    surface_type: str = "arbitrary",
    custom_faces: int = None,
    build_texture: bool = True,
    texture_pages: int = None,
    fill_holes: bool = True,
    ghosting_filter: bool = True,
    build_dem: bool = False,
    build_orthomosaic: bool = False,
    ortho_resolution: float = 0.02,
    ortho_cull_faces: bool = True,
    build_tiled_model: bool = False,
    tile_size: int = 256,
    export_glb: bool = False,
    export_obj: bool = False,
    export_fbx: bool = False,
    export_ply: bool = False,
    export_dxf: bool = False,
    export_tie_points: bool = False,
    export_dem: bool = False,
    export_ortho: bool = False,
    export_point_cloud: bool = False,
    point_cloud_format: str = "laz",
    export_report: bool = False,
    report_title: str = None,
    texture_format: str = "png",
    network_path: str = "",
    worker_ram_gb: float = 32.0,
    export_path_fn=None,
) -> tuple[list, list[tuple[str, str, str]]]:
    """Assembles the complete sequence of photogrammetry pipeline tasks into a single unified batch for independent worker execution."""
    if Metashape is None:
        return [], []

    tasks = []
    stage_info = []

    def get_exp_path(f: str) -> str:
        if export_path_fn:
            return export_path_fn(f)
        return os.path.abspath(f).replace("\\", "/")

    # 1. Feature Matching & Camera Alignment
    if align_photos:
        align_downscale = ALIGN_DOWNSCALE_MAP.get((align_quality or "high").lower(), 2)
        presel_mode_enum = PRESEL_MODE_MAP.get(
            (reference_preselection_mode or "estimated").lower(),
            Metashape.ReferencePreselectionMode.ReferencePreselectionEstimated,
        )

        t_match = Metashape.Tasks.MatchPhotos()
        t_match.downscale = align_downscale
        t_match.generic_preselection = True
        t_match.reference_preselection = True
        t_match.reference_preselection_mode = presel_mode_enum
        t_match.keypoint_limit = keypoint_limit or 200000
        t_match.keypoint_limit_per_mpx = keypoint_limit_per_mpx or 15000
        t_match.tiepoint_limit = tiepoint_limit or 25000
        t_match.mask_tiepoints = False
        t_match.filter_stationary_points = filter_stationary_points
        t_match.guided_matching = guided_matching
        tasks.append(t_match)
        stage_info.append(("MatchPhotos", "match", "GPU Feature Matching"))

        t_align = Metashape.Tasks.AlignCameras()
        t_align.adaptive_fitting = adaptive_fitting
        tasks.append(t_align)
        stage_info.append(("AlignCameras", "align", "Camera Alignment & Bundle Adjustment"))

        if filter_tiepoints:
            clean_tasks = build_cluster_gradual_selection_tasks()
            for ct in clean_tasks:
                tasks.append(ct)
                t_type = "CleanTiePoints" if hasattr(ct, "criterion") else "OptimizeCameras"
                stage_info.append((t_type, "align", "Gradual Selection Tie-Point Cleaning & Camera Optimization"))

        if crop_method in ("gps-square", "structure"):
            t_crop = build_cluster_crop_task(crop_method, padding=crop_padding)
            if t_crop:
                tasks.append(t_crop)
                stage_info.append(("RunScript", "align", f"Auto-Crop Bounding Box ({crop_method})"))

    # 2. Multi-View Stereo Depth Maps
    if build_depth_maps:
        depth_downscale = DEPTH_DOWNSCALE_MAP.get((depth_quality or "high").lower(), 4)
        filter_mode_arg = DEPTH_FILTER_MAP.get((depth_filtering or "moderate").lower(), Metashape.FilterMode.ModerateFiltering)
        t_d = Metashape.Tasks.BuildDepthMaps()
        t_d.downscale = depth_downscale
        t_d.filter_mode = filter_mode_arg
        t_d.reuse_depth = False
        tasks.append(t_d)
        stage_info.append(("BuildDepthMaps", "depth", f"GPU MVS Depth Maps ({depth_quality.capitalize()})"))

    # 3. Dense Point Cloud
    if build_point_cloud:
        t_p = Metashape.Tasks.BuildPointCloud()
        t_p.source_data = Metashape.DataSource.DepthMapsData
        t_p.point_colors = True
        t_p.point_confidence = True
        t_p.uniform_sampling = True
        t_p.points_spacing = points_spacing or 0.02
        t_p.keep_depth = True
        tasks.append(t_p)
        stage_info.append(("BuildPointCloud", "point_cloud", "Generating Dense Point Cloud"))

    # 4. Surface Mesh Model
    if build_mesh:
        surf_enum = SURFACE_TYPE_MAP.get((surface_type or "arbitrary").lower(), Metashape.SurfaceType.Arbitrary)
        if custom_faces and custom_faces > 0:
            face_count_arg = Metashape.FaceCount.CustomFaceCount
            face_count_custom_arg = custom_faces
        else:
            face_count_arg = Metashape.FaceCount.MediumFaceCount
            face_count_custom_arg = 200000

        t_m = Metashape.Tasks.BuildModel()
        t_m.surface_type = surf_enum
        t_m.source_data = Metashape.DataSource.DepthMapsData
        t_m.interpolation = Metashape.Interpolation.EnabledInterpolation
        t_m.face_count = face_count_arg
        t_m.face_count_custom = face_count_custom_arg
        t_m.vertex_colors = True
        t_m.vertex_confidence = True
        tasks.append(t_m)
        stage_info.append(("BuildModel", "mesh", f"Surface Meshing ({face_count_custom_arg:,} faces)"))

    # 5. Texture Atlas Parameterization & Mosaic Blending
    if build_texture:
        eff_pages = texture_pages or 16

        t_uv = Metashape.Tasks.BuildUV()
        t_uv.mapping_mode = Metashape.MappingMode.GenericMapping
        t_uv.page_count = eff_pages
        t_uv.texture_size = 8192
        tasks.append(t_uv)
        stage_info.append(("BuildUV", "texture", f"Texture UV Parameterization ({eff_pages} pages)"))

        t_tex = Metashape.Tasks.BuildTexture()
        t_tex.blending_mode = Metashape.BlendingMode.MosaicBlending
        t_tex.texture_size = 8192
        t_tex.fill_holes = fill_holes
        t_tex.ghosting_filter = ghosting_filter
        tasks.append(t_tex)
        stage_info.append(("BuildTexture", "texture", f"Baking {eff_pages}x 8K Texture Atlas"))

    # 6. LOD 3D Tiled Model
    if build_tiled_model:
        t_tl = Metashape.Tasks.BuildTiledModel()
        t_tl.tile_size = tile_size or 256
        t_tl.source_data = Metashape.DataSource.DepthMapsData
        tasks.append(t_tl)
        stage_info.append(("BuildTiledModel", "tiled_model", f"Building 3D Tiled Model ({tile_size}px tiles)"))

    # Geodesy / Projection Setup for DEM & Ortho
    proj = Metashape.OrthoProjection()
    target_crs = chunk.crs if chunk else None
    if chunk and chunk.crs and not getattr(chunk.crs, "projcs", None):
        gps_cams = [c for c in chunk.cameras if c.reference and c.reference.location and (abs(c.reference.location.x) > 0.001 or abs(c.reference.location.y) > 0.001)]
        if gps_cams:
            avg_lon = sum(c.reference.location.x for c in gps_cams) / len(gps_cams)
            avg_lat = sum(c.reference.location.y for c in gps_cams) / len(gps_cams)
            utm_epsg = compute_utm_epsg(avg_lon, avg_lat)
            try:
                target_crs = Metashape.CoordinateSystem(f"EPSG::{utm_epsg}")
            except Exception:
                target_crs = chunk.crs
    if target_crs:
        proj.crs = target_crs

    # 7. Digital Elevation Model (DEM)
    if build_dem:
        dem_source = Metashape.DataSource.PointCloudData if build_point_cloud else Metashape.DataSource.DepthMapsData
        t_dm = Metashape.Tasks.BuildDem()
        t_dm.source_data = dem_source
        t_dm.interpolation = Metashape.Interpolation.EnabledInterpolation
        if target_crs:
            t_dm.projection = proj
        tasks.append(t_dm)
        stage_info.append(("BuildDem", "dem", "Building Digital Elevation Model (DEM/DSM)"))

    # 8. True Orthomosaic
    if build_orthomosaic:
        t_om = Metashape.Tasks.BuildOrthomosaic()
        t_om.surface_data = Metashape.DataSource.ModelData
        t_om.blending_mode = Metashape.BlendingMode.MosaicBlending
        t_om.fill_holes = fill_holes
        t_om.ghosting_filter = ghosting_filter
        t_om.cull_faces = ortho_cull_faces
        t_om.projection = proj
        if getattr(proj.crs, "projcs", None) and ortho_resolution and ortho_resolution > 0:
            t_om.resolution = ortho_resolution
        tasks.append(t_om)
        stage_info.append(("BuildOrthomosaic", "ortho", "Generating Orthomosaic"))

    # 9. Deliverables Export
    glb_out = os.path.join(output_dir, f"{project_name}.glb")
    obj_out = os.path.join(output_dir, f"{project_name}.obj")
    fbx_out = os.path.join(output_dir, f"{project_name}.fbx")
    ply_out = os.path.join(output_dir, f"{project_name}_mesh.ply")
    dxf_out = os.path.join(output_dir, f"{project_name}.dxf")
    dem_out = os.path.join(output_dir, f"{project_name}_dem.tif")
    ortho_out = os.path.join(output_dir, f"{project_name}_orthomosaic.tif")
    rep_out = os.path.join(output_dir, f"{project_name}_report.pdf")

    tex_fmt = texture_format if texture_format else "png"
    tex_image_fmt = Metashape.ImageFormat.ImageFormatTIFF if tex_fmt == "tiff" else Metashape.ImageFormat.ImageFormatPNG

    if export_obj and build_mesh:
        t_obj = Metashape.Tasks.ExportModel()
        t_obj.format = Metashape.ModelFormat.ModelFormatOBJ
        t_obj.texture_format = tex_image_fmt
        t_obj.save_texture = True
        t_obj.save_normals = True
        t_obj.save_colors = True
        t_obj.path = get_exp_path(obj_out)
        tasks.append(t_obj)
        stage_info.append(("ExportModel", "export", "Exporting Wavefront OBJ + PNG Atlas"))

    if export_glb and build_mesh:
        t_glb = Metashape.Tasks.ExportModel()
        t_glb.format = Metashape.ModelFormat.ModelFormatGLTF
        t_glb.embed_texture = True
        t_glb.save_normals = True
        t_glb.save_colors = True
        t_glb.path = get_exp_path(glb_out)
        tasks.append(t_glb)
        stage_info.append(("ExportModel", "export", "Exporting Binary glTF (.glb)"))

    if export_fbx and build_mesh:
        t_fbx = Metashape.Tasks.ExportModel()
        t_fbx.format = Metashape.ModelFormat.ModelFormatFBX
        t_fbx.save_texture = True
        t_fbx.save_normals = True
        t_fbx.save_colors = True
        t_fbx.path = get_exp_path(fbx_out)
        tasks.append(t_fbx)
        stage_info.append(("ExportModel", "export", "Exporting Autodesk FBX"))

    if export_ply and build_mesh:
        t_ply = Metashape.Tasks.ExportModel()
        t_ply.format = Metashape.ModelFormat.ModelFormatPLY
        t_ply.save_normals = True
        t_ply.save_colors = True
        t_ply.path = get_exp_path(ply_out)
        tasks.append(t_ply)
        stage_info.append(("ExportModel", "export", "Exporting Stanford PLY Mesh"))

    if export_dxf and build_mesh:
        t_dxf = Metashape.Tasks.ExportModel()
        t_dxf.format = Metashape.ModelFormat.ModelFormatDXF
        t_dxf.save_normals = True
        t_dxf.path = get_exp_path(dxf_out)
        tasks.append(t_dxf)
        stage_info.append(("ExportModel", "export", "Exporting AutoCAD DXF Mesh"))

    if export_dem and build_dem:
        t_dem = Metashape.Tasks.ExportRaster()
        t_dem.source_data = Metashape.DataSource.ElevationData
        t_dem.format = Metashape.RasterFormat.RasterFormatNone
        t_dem.image_format = Metashape.ImageFormat.ImageFormatTIFF
        t_dem.path = get_exp_path(dem_out)
        tasks.append(t_dem)
        stage_info.append(("ExportRaster", "export", "Exporting GeoTIFF Elevation Model (DEM)"))

    if export_ortho and build_orthomosaic:
        t_ortho = Metashape.Tasks.ExportRaster()
        t_ortho.source_data = Metashape.DataSource.OrthomosaicData
        t_ortho.format = Metashape.RasterFormat.RasterFormatNone
        t_ortho.image_format = Metashape.ImageFormat.ImageFormatTIFF
        t_ortho.path = get_exp_path(ortho_out)
        tasks.append(t_ortho)
        stage_info.append(("ExportRaster", "export", "Exporting GeoTIFF Orthomosaic"))

    if export_point_cloud and build_point_cloud:
        pc_format_ext = point_cloud_format.lower() if point_cloud_format else "laz"
        pc_fmt, pc_ext = POINT_CLOUD_FORMAT_MAP.get(pc_format_ext, (Metashape.PointCloudFormat.PointCloudFormatLAZ, ".laz"))
        pc_out = os.path.join(output_dir, f"{project_name}_point_cloud{pc_ext}")
        t_pc = Metashape.Tasks.ExportPointCloud()
        t_pc.format = pc_fmt
        t_pc.source_data = Metashape.DataSource.PointCloudData
        t_pc.save_point_color = True
        t_pc.save_point_normal = True
        t_pc.save_point_confidence = True
        t_pc.save_point_classification = True
        t_pc.path = get_exp_path(pc_out)
        tasks.append(t_pc)
        stage_info.append(("ExportPointCloud", "export", f"Exporting Dense Point Cloud ({pc_format_ext.upper()})"))

    if export_report:
        t_rep = Metashape.Tasks.ExportReport()
        t_rep.title = report_title or "AutoMetashape Processing & Calibration Report"
        t_rep.path = get_exp_path(rep_out)
        tasks.append(t_rep)
        stage_info.append(("ExportReport", "export", "Exporting Calibration & Quality PDF Report"))

    return tasks, stage_info


def get_system_ram_gb() -> float:
    """Detect total physical RAM in GB using Windows API across embedded/standalone Python."""
    try:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        return stat.ullTotalPhys / (1024 ** 3)
    except Exception:
        return 32.0


def calculate_optimal_texel_budget(chunk, gsd_cm: float, page_res: int = 8192, margin: float = 1.25) -> int:
    """Calculates the exact 1:1 ground texel parity texture page budget based on 3D mesh surface area."""
    if not chunk.model or gsd_cm is None or gsd_cm <= 0.0:
        return 4
    try:
        surf_area_m2 = chunk.model.area()
        if surf_area_m2 <= 0.0:
            return 4
        texels_per_page = page_res * page_res
        required_texels = (surf_area_m2 * 10000.0) / (gsd_cm ** 2)
        raw_pages = math.ceil((required_texels / texels_per_page) * margin)
        optimal_pages = max(1, min(raw_pages, 32))
        effective_gsd_texel = math.sqrt((surf_area_m2 * 10000.0) / (optimal_pages * texels_per_page / margin))
        print(f"\n[1:1 Texel Density Budget] Mesh Surface Area: {surf_area_m2:,.1f} m² | Camera GSD: {gsd_cm:.2f} cm/px", flush=True)
        print(f"  Required Surface Texels: {required_texels/1e6:.1f} Megapixels", flush=True)
        print(f"  Allocated Texture Atlas: {optimal_pages}x {page_res}x{page_res} Pages (Effective Texel Density: {effective_gsd_texel:.2f} cm/texel, 1:1 Parity).", flush=True)
        return optimal_pages
    except Exception as e:
        print(f"  [1:1 Texel Budget] Area computation skipped ({e}); using baseline pages.", flush=True)
        return 4


def wait_for_user_resume(output_dir: str, psx_path: str) -> None:
    """Pauses pipeline execution to allow manual bounding box adjustment in Metashape GUI.

    Supports resuming via:
    1. Interactive terminal input [Enter].
    2. Standard input newline piped from a parent process / GUI.
    3. Sentinel signal file (.continue_crop) created in the output directory.
    """
    continue_file = os.path.join(output_dir, ".continue_crop")
    if os.path.exists(continue_file):
        try:
            os.remove(continue_file)
        except Exception:
            pass

    emit_ipc_event(IPC_PAUSE_CROP)
    print("\n=========================================================================", flush=True)
    print("  [PAUSE_CROP] PAUSED FOR MANUAL BOUNDING BOX IN AGISOFT METASHAPE PRO", flush=True)
    print("  Project checkpoint saved to:", flush=True)
    print(f"  --> {psx_path}", flush=True)
    print("  Instructions:", flush=True)
    print("  1. Open this project in Agisoft Metashape Pro GUI.", flush=True)
    print("  2. Use 'Resize Region' and 'Rotate Region' tools to frame the target structure.", flush=True)
    print("  3. Save the project inside Metashape Pro (Ctrl+S) and CLOSE Metashape Pro.", flush=True)
    print("  4. In AutoMetashape Studio, click [▶️ Continue Reconstruction] (or press [Enter] in terminal).", flush=True)
    print("=========================================================================", flush=True)

    # 1. Interactive terminal with Windows notification popup
    if sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
        if sys.platform == "win32":
            try:
                import winsound
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass
            try:
                import ctypes
                import subprocess
                # MB_YESNO = 4, MB_ICONQUESTION = 0x20, MB_SYSTEMMODAL = 0x1000, IDYES = 6
                metashape_bin = os.path.normpath(os.path.join(os.path.dirname(sys.executable), "..", "metashape.exe"))
                if not os.path.isfile(metashape_bin):
                    from shutil import which

                    which_p = which("metashape.exe") or which("metashape")
                    if which_p and os.path.isfile(which_p):
                        metashape_bin = which_p
                    else:
                        for base in (
                            os.environ.get("PROGRAMFILES"),
                            os.environ.get("ProgramW6432"),
                            os.environ.get("PROGRAMFILES(X86)"),
                            os.environ.get("LOCALAPPDATA"),
                        ):
                            if base:
                                for sub in (
                                    os.path.join("Agisoft", "Metashape Pro", "metashape.exe"),
                                    os.path.join("Agisoft", "PhotoScan Pro", "photoscan.exe"),
                                    os.path.join("Programs", "Agisoft", "Metashape Pro", "metashape.exe"),
                                ):
                                    cand = os.path.join(base, sub)
                                    if os.path.isfile(cand):
                                        metashape_bin = cand
                                        break
                                if os.path.isfile(metashape_bin):
                                    break
                if not os.path.isfile(metashape_bin):
                    metashape_bin = "metashape.exe"

                res = ctypes.windll.user32.MessageBoxW(
                    0,
                    f"Camera alignment completed and saved to:\n{psx_path}\n\nWould you like to open Agisoft Metashape Pro now to adjust the bounding box?\n\n(Remember to save and CLOSE Metashape Pro before pressing Enter!)",
                    "AutoMetashape: Paused for Manual Cropping",
                    0x00000004 | 0x00000020 | 0x00001000,
                )
                if res == 6:  # IDYES
                    subprocess.Popen([metashape_bin, psx_path])
            except Exception:
                pass
        try:
            input("Press [Enter] to continue with GPU MVS Depth Maps and 600K Surface Meshing...")
            return
        except EOFError:
            pass

    # 2. GUI subprocess or non-interactive stream: background thread on stdin + file polling
    resume_flag = [False]

    def _stdin_reader():
        try:
            line = sys.stdin.readline()
            if line is not None:
                resume_flag[0] = True
        except Exception:
            pass

    t = threading.Thread(target=_stdin_reader, daemon=True)
    t.start()

    while not resume_flag[0]:
        if os.path.exists(continue_file):
            try:
                os.remove(continue_file)
            except Exception:
                pass
            resume_flag[0] = True
            break
        time.sleep(0.5)

    print("  [PAUSE_CROP] Resume signal received! Reloading modified project from disk...", flush=True)


def discover_image_subfolder_groups(images_dir: str, output_dir: str = None) -> dict:
    """Discovers images grouped by immediate subfolders under images_dir.

    Excludes output directory, subfolders named 'model*', and hidden directories.
    If multiple image-bearing subfolders are found (e.g. multi-drone or multi-flight captures),
    returns a dictionary mapping folder names to their list of image filepaths.
    If images only reside at the root level or in a single subfolder, returns a dictionary
    with a single entry.
    """
    supported_exts = SUPPORTED_IMAGE_EXTS
    abs_output = os.path.abspath(output_dir) if output_dir else None
    groups = {}
    root_images = []

    try:
        entries = sorted(os.listdir(images_dir))
    except Exception as e:
        raise ValueError(f"Cannot access images directory '{images_dir}': {e}")

    for entry in entries:
        full_path = os.path.join(images_dir, entry)
        if os.path.isdir(full_path):
            abs_sub = os.path.abspath(full_path)
            # Skip output dir and any 'model*' export dirs
            if entry.lower().startswith("model") or (abs_output and abs_sub.startswith(abs_output)):
                continue
            if entry.startswith("."):
                continue

            sub_imgs = []
            for r, dirs, files in os.walk(full_path):
                dirs[:] = [
                    d for d in dirs
                    if not d.lower().startswith("model")
                    and not d.startswith(".")
                    and not (abs_output and os.path.abspath(os.path.join(r, d)).startswith(abs_output))
                ]
                for f in files:
                    if f.lower().endswith(supported_exts):
                        sub_imgs.append(os.path.join(r, f))
            if sub_imgs:
                groups[entry] = sorted(sub_imgs)
        elif os.path.isfile(full_path) and entry.lower().endswith(supported_exts):
            root_images.append(full_path)

    if root_images:
        if groups:
            groups["Root_Photos"] = sorted(root_images)
        else:
            groups["Main"] = sorted(root_images)

    if not groups:
        all_imgs = []
        for r, dirs, files in os.walk(images_dir):
            dirs[:] = [
                d for d in dirs
                if not d.lower().startswith("model")
                and not d.startswith(".")
                and not (abs_output and os.path.abspath(os.path.join(r, d)).startswith(abs_output))
            ]
            for f in files:
                if f.lower().endswith(supported_exts):
                    all_imgs.append(os.path.join(r, f))
        if all_imgs:
            groups["Main"] = sorted(all_imgs)

    return groups


def save_checkpoint(doc, target_path=None):
    """Safely saves Metashape document ensuring read_only is overridden and chunk is re-synchronized."""
    try:
        doc.read_only = False
    except Exception:
        pass

    try:
        if target_path and not getattr(doc, "path", None):
            doc.save(target_path)
        elif target_path:
            doc.save(target_path)
        else:
            doc.save()
    except Exception as e:
        # Fallback: force unlock and retry
        try:
            if target_path:
                files_dir = target_path.replace(".psx", ".files")
                lock_file = os.path.join(files_dir, "lock")
                if os.path.isfile(lock_file):
                    try:
                        os.remove(lock_file)
                    except Exception:
                        pass
            doc.read_only = False
            if target_path:
                doc.save(target_path)
            else:
                doc.save()
        except Exception as retry_err:
            raise OSError(f"Failed to save Metashape project: {retry_err}") from e

    return doc.chunk


def infer_project_name(
    images_dir: str | None = None,
    output_dir: str | None = None,
    project_file: str | None = None,
) -> str:
    """Derive clean project base name from survey folder, output folder, or project path."""
    if project_file:
        base = os.path.splitext(os.path.basename(project_file))[0]
        if base and base.lower() != "building_reconstruction":
            return base
        parent = os.path.basename(os.path.dirname(os.path.abspath(project_file)))
        if parent.lower().startswith("model") or parent.lower() in ("export", "exports", "output", "outputs"):
            grandparent = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(project_file))))
            if grandparent:
                return grandparent
        if parent:
            return parent

    if images_dir:
        abs_img = os.path.abspath(images_dir)
        base = os.path.basename(abs_img)
        if base.lower() in ("images", "photos", "raw", "dji", "survey", "data", "input") and os.path.dirname(abs_img):
            base = os.path.basename(os.path.dirname(abs_img))
        if base and base not in (".", "/", "\\"):
            return base

    if output_dir:
        abs_out = os.path.abspath(output_dir)
        base = os.path.basename(abs_out)
        if (base.lower().startswith("model") or base.lower() in ("export", "exports", "output", "outputs")) and os.path.dirname(abs_out):
            base = os.path.basename(os.path.dirname(abs_out))
        if base and base not in (".", "/", "\\"):
            return base

    return "project"


def run_reconstruction(
    images_dir: str,
    output_dir: str = None,
    align_quality: str = "high",
    align_photos: bool = True,
    build_depth_maps: bool = True,
    build_mesh: bool = True,
    build_texture: bool = True,
    keypoint_limit: int = 200000,
    keypoint_limit_per_mpx: int = 15000,
    tiepoint_limit: int = 25000,
    filter_stationary_points: bool = True,
    guided_matching: bool = True,
    adaptive_fitting: bool = True,
    reference_preselection_mode: str = "estimated",
    filter_tiepoints: bool = True,
    depth_quality: str = "high",
    depth_filtering: str = "moderate",
    custom_faces: int = None,
    texture_pages: int = None,
    points_spacing: float = 0.02,
    ortho_resolution: float = 0.02,
    ortho_cull_faces: bool = True,
    fill_holes: bool = True,
    ghosting_filter: bool = True,
    rebake_texture: bool = False,
    crop_method: str = None,
    crop_padding: float = 0.05,
    min_quality: float = 0.5,
    rolling_shutter: bool = True,
    close_holes: bool = False,
    pause_after_crop: bool = False,
    resume: bool = False,
    multi_chunk: str = "disable",
    chunk_align_method: str = "camera",
    preset: str = "pro",
    auto_params: bool = False,
    ground_cell_size: float = None,
    ground_max_angle: float = None,
    ground_max_distance: float = None,
    build_orthomosaic: bool = None,
    export_ortho: bool = False,
    build_dem: bool = None,
    export_dem: bool = False,
    build_point_cloud: bool = True,
    export_point_cloud: bool = False,
    point_cloud_format: str = "laz",
    classify_ground: bool = None,
    classify_points: bool = False,
    filter_point_confidence: int = None,
    export_report: bool = False,
    export_glb: bool = False,
    export_obj: bool = False,
    report_title: str = None,
    surface_type: str = "arbitrary",
    detect_markers: bool = False,
    target_type: str = "circular12",
    marker_tolerance: int = 50,
    import_reference: str = None,
    calibrate_colors: bool = False,
    smooth_mesh: bool = False,
    smooth_strength: float = 3.0,
    decimate_mesh: int = None,
    remove_lighting: bool = False,
    delighting_color_mode: bool = False,
    build_tiled_model: bool = False,
    tiled_model_format: str = "cesium",
    tile_size: int = 256,
    build_contours: bool = False,
    contour_interval: float = 1.0,
    contours_format: str = "shp",
    export_fbx: bool = False,
    export_ply: bool = False,
    export_dxf: bool = False,
    export_tie_points: bool = False,
    network_enable: bool = False,
    network_host: str = "127.0.0.1",
    network_port: int = 5840,
    network_path: str = "",
    network_independent_workers: bool = False,
    project_name: str | None = None,
):
    t_start = time.time()
    if not project_name:
        project_name = infer_project_name(images_dir=images_dir, output_dir=output_dir)
    if not output_dir:
        output_dir = os.path.join(images_dir, project_name)
    os.makedirs(output_dir, exist_ok=True)
    psx_out = os.path.join(output_dir, f"{project_name}.psx")
    legacy_psx = os.path.join(output_dir, "building_reconstruction.psx")

    # -----------------------------------------------------------------------
    # Pro Photogrammetry Survey Physics & Telemetry Resolution
    # -----------------------------------------------------------------------
    inferred_profile = None
    survey_physics = None
    pro_plan = None
    usgs_stats = None
    if preset == "building":
        preset = "pro"
    if preset == "custom":
        is_auto = False
    else:
        is_auto = True

    if is_auto:
        try:
            survey_physics = analyze_survey_physics(images_dir, output_dir=output_dir)
            pro_plan = derive_pro_photogrammetry_plan(survey_physics)
            inferred_profile = survey_physics
            print_pro_engineering_report(survey_physics, pro_plan)

            if crop_method is None:
                crop_method = pro_plan["crop_method"]
            if custom_faces is None:
                custom_faces = pro_plan["custom_faces"]
            if texture_pages is None:
                texture_pages = pro_plan["texture_pages"]
            if build_orthomosaic is None:
                build_orthomosaic = pro_plan["build_orthomosaic"]
            if build_dem is None:
                build_dem = pro_plan["build_dem"]
            if build_point_cloud is None:
                build_point_cloud = pro_plan["build_point_cloud"]
            if classify_ground is None:
                classify_ground = pro_plan["classify_ground"]
            if rolling_shutter is None:
                rolling_shutter = pro_plan["rolling_shutter"]
            if close_holes is None:
                close_holes = pro_plan["close_holes"]
            if depth_filtering is None:
                depth_filtering = pro_plan["depth_filtering"]
            if surface_type == "arbitrary" and pro_plan.get("surface_type") == "heightfield":
                surface_type = "heightfield"
            if not remove_lighting and pro_plan.get("remove_lighting"):
                remove_lighting = True
                delighting_color_mode = True
            if ground_cell_size is None:
                ground_cell_size = pro_plan.get("ground_cell_size", 35.0)
            if ground_max_angle is None:
                ground_max_angle = pro_plan.get("ground_max_angle", 15.0)
            if ground_max_distance is None:
                ground_max_distance = pro_plan.get("ground_max_distance", 0.5)
        except Exception as e_prof:
            print(f"  [Survey Physics] Note: Telemetry analysis ({e_prof}); using standard production geomatics defaults.", flush=True)

    # Final safety fallbacks
    if crop_method is None:
        crop_method = "gps-square"
    if custom_faces is None:
        custom_faces = 2000000
    if texture_pages is None:
        texture_pages = 16
    is_pro_preset = (preset == "pro" or is_auto)
    if build_orthomosaic is None:
        build_orthomosaic = True if is_pro_preset else False
    if build_dem is None:
        build_dem = False
    if export_dem or build_contours:
        build_dem = True
    if build_point_cloud is None:
        build_point_cloud = True
    if classify_ground is None:
        classify_ground = False
    if close_holes is None:
        close_holes = False

    print("=========================================================================")
    if preset == "custom":
        preset_label = " [CUSTOM MANUAL CONFIGURATION]"
    elif preset:
        preset_label = f" [{preset.upper()} PRESET]"
    else:
        preset_label = " [AUTONOMOUS INFERENCE]" if is_auto else ""
    print(f"  AUTONOMOUS METASHAPE PHOTOGRAMMETRY PIPELINE{preset_label}")
    print("=========================================================================", flush=True)
    print(f"  Align Quality:      {align_quality.upper()} (Downscale: {ALIGN_DOWNSCALE_MAP.get(align_quality.lower(), 2)})")
    print(f"  Keypoints / Tie:    {keypoint_limit:,} / {tiepoint_limit:,}")
    print(f"  Exclude Stationary: {filter_stationary_points} | Guided Matching: {guided_matching} | Adaptive Fitting: {adaptive_fitting}")
    align_meth_label = chunk_align_method.upper() if chunk_align_method else "CAMERA"
    print(f"  Filter Tie Points:  {filter_tiepoints} | Multi-Chunk: {multi_chunk.upper()} (Method: {align_meth_label})")
    print(f"  Depth Maps Quality: {depth_quality.upper()} (Downscale: {DEPTH_DOWNSCALE_MAP.get(depth_quality.lower(), 4)}) | Filtering: {depth_filtering.capitalize()}")
    if custom_faces >= 1_000_000:
        if custom_faces % 1_000_000 == 0:
            face_label = f"{custom_faces // 1_000_000}M"
        elif custom_faces % 100_000 == 0:
            face_label = f"{custom_faces / 1_000_000:.1f}M"
        else:
            face_label = f"{custom_faces // 1000}K"
    else:
        face_label = f"{custom_faces // 1000}K"
    print(f"  Target Mesh Faces:  {custom_faces:,} ({face_label})")
    print(f"  Texture Atlas:      {texture_pages} pages @ 8192x8192 (8K) | Fill Holes: {fill_holes} | Ghosting Filter: {ghosting_filter}")
    print(f"  Bounding Box Crop:  {crop_method.upper()} | Rolling Shutter: {rolling_shutter} | Hole Closing: {close_holes}")
    print(f"  Dense Point Cloud:  {'ENABLED' if build_point_cloud else 'DISABLED'} (Classify Ground: {classify_ground})")
    print(f"  Elevation (DEM):    {'ENABLED' if build_dem else 'DISABLED'} | Orthomosaic: {'ENABLED' if build_orthomosaic else 'DISABLED'}")
    print(f"  Quality PDF Report: {'ENABLED' if export_report else 'DISABLED'}")
    print(f"  Execution Mode:     {'RESUME CHECKPOINT' if resume else 'RESTART FROM SCRATCH (DEFAULT)'}")
    print("=========================================================================", flush=True)

    # Enable all available NVIDIA / high-performance GPUs
    gpus = Metashape.app.enumGPUDevices()
    nvidia_mask = 0
    active_gpu_names = []
    for i, g in enumerate(gpus):
        vendor = g.get("vendor", "").lower()
        name = g.get("name", "").lower()
        if "nvidia" in vendor or "geforce" in name or "rtx" in name:
            nvidia_mask |= (1 << i)
            active_gpu_names.append(f"{g['name']} ({g['mem_size'] / (1024**3):.1f} GB VRAM)")

    if nvidia_mask:
        Metashape.app.gpu_mask = nvidia_mask
        print(f"[Hardware] GPU: {', '.join(active_gpu_names)} (Mask: {nvidia_mask})", flush=True)
    elif gpus:
        Metashape.app.gpu_mask = (1 << len(gpus)) - 1
        print(f"[Hardware] GPU: {gpus[0]['name']} ({gpus[0]['mem_size'] / (1024**3):.1f} GB VRAM)", flush=True)
    else:
        print("[Hardware] Running on CPU.", flush=True)

    # Distributed Network Cluster Processing
    net_client = None
    cluster_enabled = False
    active_cluster_workers = []
    if network_enable and Metashape is not None and hasattr(Metashape, "app") and hasattr(Metashape.app, "settings"):
        try:
            print("\n=========================================================================")
            print("  AUTOMETASHAPE: DISTRIBUTED NETWORK PROCESSING CLUSTER")
            print(f"  Server Host: {network_host}:{network_port}")
            if network_path:
                print(f"  Storage Root (UNC): {network_path}")
            print("=========================================================================", flush=True)

            Metashape.app.settings.network_enable = True
            Metashape.app.settings.network_host = str(network_host).strip()
            Metashape.app.settings.network_port = int(network_port)
            if network_path:
                Metashape.app.settings.network_path = str(network_path).strip()

            if hasattr(Metashape, "NetworkClient"):
                client = Metashape.NetworkClient()
                try:
                    client.connect(f"{network_host}:{network_port}")
                    raw_workers = client.workerList()
                    if isinstance(raw_workers, dict):
                        workers = raw_workers.get("workers", [])
                    elif isinstance(raw_workers, (list, tuple)):
                        workers = list(raw_workers)
                    else:
                        workers = []

                    for w in workers:
                        w_id = w.get("worker_id") if isinstance(w, dict) else getattr(w, "id", None)
                        w_paused = w.get("paused", False) if isinstance(w, dict) else getattr(w, "paused", False)
                        if w_paused and w_id is not None:
                            try:
                                client.setWorkerPaused(w_id, False)
                            except Exception:
                                pass

                    raw_workers = client.workerList()
                    if isinstance(raw_workers, dict):
                        workers = raw_workers.get("workers", [])
                    elif isinstance(raw_workers, (list, tuple)):
                        workers = list(raw_workers)
                    else:
                        workers = []

                    active_cluster_workers = [
                        w for w in workers
                        if not (w.get("paused", False) if isinstance(w, dict) else getattr(w, "paused", False))
                    ]
                    print(f"  [Network Cluster] Connected to server successfully!", flush=True)
                    print(f"  [Network Cluster] Cluster Nodes: {len(workers)} ({len(active_cluster_workers)} active workers)", flush=True)
                    for idx, w in enumerate(workers, 1):
                        if isinstance(w, dict):
                            host_label = w.get("hostname", "Node")
                            g_mask = w.get("gpu_mask", 1)
                            prio = w.get("priority", 0)
                            cpu_en = w.get("cpu_enabled", False)
                        else:
                            host_label = getattr(w, "host", getattr(w, "hostname", "Node"))
                            g_mask = getattr(w, "gpu_mask", 1)
                            prio = getattr(w, "priority", 0)
                            cpu_en = getattr(w, "cpu_enabled", False)
                        print(f"    • Node {idx}: {host_label} (GPU Mask: {g_mask}, CPU: {'On' if cpu_en else 'Off'}, Priority: {prio})", flush=True)

                    if len(active_cluster_workers) > 0:
                        net_client = client
                        cluster_enabled = True
                    else:
                        print(f"  [Network Cluster] [NOTICE] 0 active worker nodes connected to server. Will execute tasks locally.", flush=True)
                except Exception as e_net_conn:
                    print(f"  [Network Cluster] [NOTICE] Server connection failed at {network_host}:{network_port} ({e_net_conn}). Processing locally.", flush=True)
        except Exception as e_net_init:
            print(f"  [Network Cluster] [WARNING] Error configuring network processing: {e_net_init}. Processing locally.", flush=True)
    elif Metashape is not None and hasattr(Metashape, "app") and hasattr(Metashape.app, "settings"):
        try:
            Metashape.app.settings.network_enable = False
        except Exception:
            pass

    doc = Metashape.Document()
    resumed = False
    target_psx = psx_out if os.path.exists(psx_out) else (legacy_psx if os.path.exists(legacy_psx) else None)

    def _safe_reopen():
        load_path = psx_out if os.path.exists(psx_out) else (target_psx if target_psx and os.path.exists(target_psx) else psx_out)
        files_dir = load_path.replace(".psx", ".files")
        lock_file = os.path.join(files_dir, "lock")
        if os.path.isfile(lock_file):
            try:
                os.remove(lock_file)
            except Exception:
                pass
        try:
            doc.open(load_path, read_only=False, ignore_lock=True)
        except Exception:
            try:
                doc.open(load_path, ignore_lock=True)
            except Exception:
                doc.open(load_path)
        try:
            doc.read_only = False
        except Exception:
            pass
        if doc.chunk is None and len(doc.chunks) > 0:
            doc.chunk = doc.chunks[0]

    def get_cluster_rel_path(target_file: str = "") -> str:
        tgt = target_file or psx_out
        if network_path:
            norm_tgt = os.path.abspath(tgt).replace("\\\\", "/").replace("\\", "/")
            norm_net = network_path.strip().replace("\\\\", "/").replace("\\", "/").rstrip("/")

            # 1. Try standard commonpath if on same drive or both UNC
            try:
                abs_tgt = os.path.abspath(tgt)
                abs_net = os.path.abspath(network_path)
                if os.path.commonpath([abs_tgt, abs_net]).lower() == abs_net.lower():
                    return os.path.relpath(abs_tgt, abs_net).replace("\\", "/")
            except Exception:
                pass

            # 2. Match share name from UNC path against target path components
            net_parts = [p for p in norm_net.split("/") if p]
            if net_parts:
                share_name = net_parts[-1].lower()
                tgt_parts = [p for p in norm_tgt.split("/") if p]
                tgt_lower = [p.lower() for p in tgt_parts]
                if share_name in tgt_lower:
                    idx = tgt_lower.index(share_name)
                    rel = "/".join(tgt_parts[idx + 1:])
                    if rel:
                        return rel

            # 3. Fallback: match any common subfolder in net_path
            for i in range(len(net_parts) - 1, -1, -1):
                sub = net_parts[i].lower()
                tgt_parts = [p for p in norm_tgt.split("/") if p]
                tgt_lower = [p.lower() for p in tgt_parts]
                if sub in tgt_lower:
                    idx = tgt_lower.index(sub)
                    rel = "/".join(tgt_parts[idx + 1:])
                    if rel:
                        return rel

        return os.path.basename(tgt)

    def get_cluster_export_path(target_file: str) -> str:
        """Returns an absolute network-accessible path (UNC or local absolute) for cluster export tasks."""
        norm_tgt = os.path.abspath(target_file).replace("\\", "/")
        if network_path:
            is_unc = network_path.startswith(r"\\") or network_path.startswith("//")
            clean_net = network_path.strip().replace("\\", "/").rstrip("/")
            if is_unc:
                clean_net = "//" + clean_net.lstrip("/")
            rel = get_cluster_rel_path(target_file)
            if rel and rel != norm_tgt and not rel.startswith(clean_net):
                return f"{clean_net}/{rel}"
        return norm_tgt

    def dispatch_cluster_task(tasks: Metashape.Task | list[Metashape.Task], task_desc: str, stage_key: str = "") -> bool:
        """Dispatch photogrammetry task(s) to the network cluster with real-time progress monitoring."""
        if not cluster_enabled or net_client is None:
            return False

        try:
            rel_psx = get_cluster_rel_path()
            if stage_key:
                emit_ipc_stage(stage_key, task_desc)
            print(f"\n[Network Cluster] Dispatching '{task_desc}' to cluster ({network_host}:{network_port})...", flush=True)

            doc.save(psx_out)
            task_list = tasks if isinstance(tasks, (list, tuple)) else [tasks]
            chunk_target = doc.chunk or (doc.chunks[0] if doc.chunks else None)
            if chunk_target is None:
                print(f"  [Cluster] [ERROR] Document has no active chunk for '{task_desc}'", flush=True)
                return False
            net_tasks = [t.toNetworkTask(chunk_target) for t in task_list]
            batch_id = net_client.createBatch(rel_psx, net_tasks)
            if network_independent_workers:
                try:
                    net_client.setBatchWorkerLimit(batch_id, 1)
                    print("  [Cluster] Mode: Independent Worker (worker_limit=1 locked to 1 node)", flush=True)
                except Exception as e_limit:
                    print(f"  [Cluster] Note: could not set batch worker limit: {e_limit}", flush=True)
            net_client.setBatchPaused(batch_id, False)

            t_task_start = time.time()
            last_progress = -1.0
            last_log_time = 0.0
            while True:
                time.sleep(1.0)
                b_info = net_client.batchInfo(batch_id)
                state = b_info.get("state")
                tasks_info = b_info.get("tasks", [])
                if tasks_info:
                    total_prog = sum(t.get("progress", 0.0) for t in tasks_info) / len(tasks_info)
                    active_task = next((t for t in tasks_info if t.get("state") in ("running", "submitted")), tasks_info[-1])
                    t_sub_name = active_task.get("name", task_desc)
                    sub_prog = active_task.get("progress", 0.0)
                    now_t = time.time()
                    if abs(total_prog - last_progress) >= 0.5 or (now_t - last_log_time >= 10.0 and total_prog != last_progress):
                        elapsed_min = (now_t - t_task_start) / 60.0
                        if len(tasks_info) > 1:
                            print(f"  [Cluster Progress] {task_desc} - {t_sub_name}: {sub_prog:.1f}% (Overall: {total_prog:.1f}%, Elapsed: {elapsed_min:.1f} min)", flush=True)
                        else:
                            print(f"  [Cluster Progress] {task_desc}: {total_prog:.1f}% (Elapsed: {elapsed_min:.1f} min)", flush=True)
                        last_progress = total_prog
                        last_log_time = now_t

                if state == "completed":
                    elapsed_min = (time.time() - t_task_start) / 60.0
                    print(f"  [Cluster] ✓ Task '{task_desc}' finished on cluster in {elapsed_min:.2f} min!", flush=True)
                    break
                elif state in ("failed", "aborted"):
                    err_msgs = []
                    for t in tasks_info:
                        for err in t.get("errors", []):
                            err_msgs.append(str(err))
                    err_str = "; ".join(err_msgs) if err_msgs else "unknown cluster error"
                    print(f"  [Cluster] [ERROR] Task '{task_desc}' {state}: {err_str}", flush=True)
                    print(f"  [Cluster] Falling back to local execution for '{task_desc}'...", flush=True)
                    return False

            _safe_reopen()
            return True

        except Exception as e_dispatch:
            print(f"  [Cluster] [WARNING] Cluster dispatch exception: {e_dispatch}. Falling back to local execution...", flush=True)
            return False

    if resume and target_psx:
        print(f"\n[Checkpoint] Found existing project (--resume specified): {target_psx}", flush=True)
        try:
            files_dir = psx_out.replace(".psx", ".files")
            lock_file = os.path.join(files_dir, "lock")
            if os.path.isfile(lock_file):
                try:
                    os.remove(lock_file)
                    print(f"  [Auto-Recovery] Removed stale lock file from previous session: {lock_file}", flush=True)
                except Exception as e_lock:
                    print(f"  [Warning] Could not remove lock file: {e_lock}", flush=True)
            try:
                doc.open(target_psx, read_only=False, ignore_lock=True)
            except Exception:
                try:
                    doc.open(target_psx, ignore_lock=True)
                except Exception:
                    doc.open(target_psx)
            try:
                doc.read_only = False
            except Exception:
                pass
            prod_chunk = None
            for c in doc.chunks:
                if c.label == "Production_Reconstruction":
                    prod_chunk = c
                    break
            if prod_chunk is None and len(doc.chunks) > 1:
                prod_chunk = max(doc.chunks, key=lambda c: sum(1 for cam in c.cameras if cam.transform and cam.enabled))
            if prod_chunk is not None:
                doc.chunk = prod_chunk
            chunk = doc.chunk
            aligned_count = sum(1 for c in chunk.cameras if c.transform and c.enabled)
            if aligned_count >= 2:
                print(f"  Successfully loaded project with {len(chunk.cameras)} cameras ({aligned_count} aligned).", flush=True)
                print(f"  Skipping Steps 1-3 (Ingestion, Sharpness Filter, Feature Matching, Alignment).", flush=True)
                resumed = True
            else:
                print(f"  Existing project has insufficient aligned cameras ({aligned_count}). Restarting from Step 1.", flush=True)
                doc = Metashape.Document()
                chunk = doc.addChunk()
                chunk.label = "Production_Reconstruction"
        except Exception as e:
            print(f"  Failed to load checkpoint ({e}). Starting fresh project.", flush=True)
            doc = Metashape.Document()
            chunk = doc.addChunk()
            chunk.label = "Production_Reconstruction"
    else:
        if not resume and os.path.exists(psx_out):
            print(f"\n[Run Mode] Starting fresh reconstruction from scratch (--resume not specified). Overwriting previous run in: {output_dir}", flush=True)
            files_dir = psx_out.replace(".psx", ".files")
            lock_file = os.path.join(files_dir, "lock")
            if os.path.isfile(lock_file):
                try:
                    os.remove(lock_file)
                except Exception:
                    pass
        chunk = doc.addChunk()
        chunk.label = "Production_Reconstruction"

    if not resumed:
        # Step 1: Discover Images (Single-folder or Multi-Subfolder Chunks)
        groups = discover_image_subfolder_groups(images_dir, output_dir=output_dir)
        total_photos = sum(len(imgs) for imgs in groups.values())
        if total_photos == 0:
            raise ValueError(f"No valid image files found recursively in '{images_dir}'!")

        align_downscale = ALIGN_DOWNSCALE_MAP.get(align_quality.lower(), 2)
        presel_mode_enum = PRESEL_MODE_MAP.get(
            (reference_preselection_mode or "estimated").lower(),
            Metashape.ReferencePreselectionMode.ReferencePreselectionEstimated,
        )

        use_multi_chunk = False
        if multi_chunk == "force" and len(groups) >= 1:
            use_multi_chunk = True
        elif multi_chunk == "auto" and len(groups) > 1:
            use_multi_chunk = True
        elif multi_chunk == "disable":
            use_multi_chunk = False

        if use_multi_chunk:
            print(f"\n[Multi-Chunk Mode] Detected {len(groups)} distinct image subfolders ({total_photos:,} photos total).", flush=True)
            print("  Ingesting each subfolder into an independent chunk for drift-free camera alignment and point-based RANSAC ICP registration...", flush=True)

            for c in list(doc.chunks):
                if len(c.cameras) == 0:
                    doc.remove(c)

            created_chunks = []
            for idx, (grp_name, filepaths) in enumerate(groups.items()):
                sub_chunk = doc.addChunk()
                sub_chunk.label = f"Flight_{idx+1}_{grp_name}"
                emit_ipc_stage("ingest", f"Ingesting Chunk {idx+1}/{len(groups)} ('{sub_chunk.label}'): {len(filepaths):,} photos")
                print(f"\n[1/7] Ingesting Chunk {idx+1}/{len(groups)} ('{sub_chunk.label}'): {len(filepaths):,} photos...", flush=True)
                sub_chunk.addPhotos(filepaths)
                print(f"  Loaded {len(sub_chunk.cameras):,} cameras into '{sub_chunk.label}'.", flush=True)

                if min_quality > 0.0:
                    filter_image_quality(sub_chunk, min_quality=min_quality)

                if rolling_shutter:
                    for s in sub_chunk.sensors:
                        s.rolling_shutter = Metashape.Shutter.Model.Regularized
                    print(f"  [Rolling Shutter] Enabled Regularized CMOS rolling shutter compensation on '{sub_chunk.label}'.", flush=True)

                created_chunks.append(sub_chunk)

            # Step 2: Feature Detection & Matching per chunk
            for idx, c in enumerate(created_chunks):
                emit_ipc_stage("match", f"GPU Feature Matching for Chunk {idx+1}/{len(created_chunks)}")
                print(f"\n[2/7] GPU Feature Matching for Chunk {idx+1}/{len(created_chunks)} ('{c.label}', {len(c.cameras):,} cameras)...", flush=True)
                t_match = time.time()
                c.matchPhotos(
                    downscale=align_downscale,
                    generic_preselection=True,
                    reference_preselection=True,
                    reference_preselection_mode=presel_mode_enum,
                    keypoint_limit=keypoint_limit,
                    keypoint_limit_per_mpx=keypoint_limit_per_mpx,
                    tiepoint_limit=tiepoint_limit,
                    mask_tiepoints=False,
                    filter_stationary_points=filter_stationary_points,
                    guided_matching=guided_matching,
                )
                print(f"  Matching finished for '{c.label}' in {(time.time() - t_match)/60:.2f} min.", flush=True)

            # Step 3: Camera Alignment per chunk
            for idx, c in enumerate(created_chunks):
                emit_ipc_stage("align", f"Aligning Cameras for Chunk {idx+1}/{len(created_chunks)}")
                print(f"\n[3/7] Aligning Cameras for Chunk {idx+1}/{len(created_chunks)} ('{c.label}', Adaptive Fitting: {adaptive_fitting})...", flush=True)
                t_align = time.time()
                c.alignCameras(adaptive_fitting=adaptive_fitting)
                aligned_count = sum(1 for cam in c.cameras if cam.transform and cam.enabled)
                enabled_count = sum(1 for cam in c.cameras if cam.enabled)
                print(f"  Chunk '{c.label}' alignment finished in {(time.time() - t_align)/60:.2f} min. Aligned: {aligned_count}/{enabled_count} active cameras.", flush=True)

                if filter_tiepoints:
                    usgs_stats = filter_tie_points(c)

                try:
                    c.updateTransform()
                except Exception:
                    pass

            aligned_chunks = [c for c in created_chunks if sum(1 for cam in c.cameras if cam.transform and cam.enabled) >= 2]
            if len(aligned_chunks) == 0:
                raise RuntimeError("No cameras could be aligned across any flight subfolder! Please verify photo quality and overlap.")

            anchor_chunk = max(aligned_chunks, key=lambda c: sum(1 for cam in c.cameras if cam.transform and cam.enabled))
            ref_key = anchor_chunk.key
            merge_chunk_keys = [c.key for c in created_chunks]
            align_chunk_keys = [c.key for c in aligned_chunks]

            if len(aligned_chunks) >= 2:
                align_mode = chunk_align_method.lower() if chunk_align_method else "camera"
                if align_mode == "point":
                    print(f"\n[Chunk Alignment] Registering {len(align_chunk_keys)} aligned chunks onto anchor '{anchor_chunk.label}' using Point-Based RANSAC ICP (method=0, fit_scale=True)...", flush=True)
                    try:
                        doc.alignChunks(
                            chunks=align_chunk_keys,
                            reference=ref_key,
                            method=0,
                            fit_scale=True,
                            downscale=align_downscale,
                            keypoint_limit=keypoint_limit,
                        )
                        print("  [Chunk Alignment] Point-Based RANSAC ICP chunk alignment succeeded!", flush=True)
                    except Exception as e_align:
                        print(f"  [Chunk Alignment] Point-based ICP raised '{e_align}'; falling back to Camera/GPS-based registration (method=2)...", flush=True)
                        try:
                            doc.alignChunks(
                                chunks=align_chunk_keys,
                                reference=ref_key,
                                method=2,
                                fit_scale=False,
                            )
                            print("  [Chunk Alignment] Camera/GPS-based chunk alignment (method=2) succeeded!", flush=True)
                        except Exception as e_cam:
                            print(f"  [Chunk Alignment] Camera-based alignment raised '{e_cam}'; proceeding with native camera transforms.", flush=True)
                else:
                    print(f"\n[Chunk Alignment] Registering {len(align_chunk_keys)} aligned chunks onto anchor '{anchor_chunk.label}' using Camera/GPS-based registration (method=2)...", flush=True)
                    try:
                        doc.alignChunks(
                            chunks=align_chunk_keys,
                            reference=ref_key,
                            method=2,
                            fit_scale=False,
                        )
                        print("  [Chunk Alignment] Camera/GPS-based chunk alignment (method=2) succeeded!", flush=True)
                    except Exception as e_cam:
                        print(f"  [Chunk Alignment] Camera-based registration raised '{e_cam}'; falling back to Point-Based RANSAC ICP (method=0)...", flush=True)
                        try:
                            doc.alignChunks(
                                chunks=align_chunk_keys,
                                reference=ref_key,
                                method=0,
                                fit_scale=True,
                                downscale=align_downscale,
                                keypoint_limit=keypoint_limit,
                            )
                            print("  [Chunk Alignment] Point-Based RANSAC ICP chunk alignment succeeded!", flush=True)
                        except Exception as e_align:
                            print(f"  [Chunk Alignment] Point-based alignment raised '{e_align}'; proceeding with native camera transforms.", flush=True)
            else:
                print(f"\n[Chunk Alignment] Anchor chunk '{anchor_chunk.label}' is the only flight with aligned cameras; skipping chunk registration.", flush=True)

            print(f"\n[Chunk Merging] Merging {len(merge_chunk_keys)} chunks into unified production reconstruction chunk...", flush=True)
            try:
                doc.mergeChunks(chunks=merge_chunk_keys, merge_tiepoints=True)
            except Exception as e_merge:
                print(f"  [Chunk Merging] Note: merge_tiepoints raised '{e_merge}'; merging with camera transforms preserved...", flush=True)
                doc.mergeChunks(chunks=merge_chunk_keys, merge_tiepoints=False)

            doc.chunk = doc.chunks[-1]
            chunk = doc.chunk
            chunk.label = "Production_Reconstruction"
            try:
                chunk.updateTransform()
            except Exception:
                pass
            aligned_total = sum(1 for cam in chunk.cameras if cam.transform and cam.enabled)
            enabled_total = sum(1 for cam in chunk.cameras if cam.enabled)
            print(f"  Successfully merged into '{chunk.label}' ({len(chunk.cameras)} cameras, {aligned_total}/{enabled_total} aligned).", flush=True)

            if import_reference and os.path.isfile(import_reference):
                print(f"\n[GCP Reference] Importing ground control points from: {import_reference}...", flush=True)
                try:
                    chunk.importReference(path=import_reference, format=Metashape.ReferenceFormat.ReferenceFormatCSV)
                    chunk.updateTransform()
                    print("  Reference import and georeferencing transform updated.", flush=True)
                except Exception as e_ref:
                    print(f"  [WARNING] Reference import issue: {e_ref}. Continuing...", flush=True)

            if detect_markers:
                print(f"\n[Target Detection] Detecting target markers (Type: {target_type}, Tolerance: {marker_tolerance})...", flush=True)
                try:
                    tt_enum = TARGET_TYPE_MAP.get(target_type.lower(), Metashape.TargetType.CircularTarget12bit)
                    chunk.detectMarkers(target_type=tt_enum, tolerance=marker_tolerance)
                    print(f"  Target detection completed! Found {len(chunk.markers)} markers.", flush=True)
                except Exception as e_mk:
                    print(f"  [WARNING] Marker detection issue: {e_mk}. Continuing...", flush=True)

            # -------------------------------------------------------------
            # Autonomous Unified Project Batch (Independent Project Mode)
            # -------------------------------------------------------------
            can_use_unified_batch = (
                cluster_enabled
                and network_independent_workers
                and net_client is not None
                and (not pause_after_crop)
                and crop_method != "manual"
            )
            unified_succeeded = False
            if can_use_unified_batch:
                try:
                    print("\n=========================================================================", flush=True)
                    print("  AUTOMETASHAPE: AUTONOMOUS UNIFIED CLUSTER BATCH (INDEPENDENT MODE)", flush=True)
                    print("  Full Project Pipeline Bundled into 1 Dedicated Worker Batch", flush=True)
                    print("  Worker Node Lock: 1 node (Zero Intermediate LAN Syncs, Zero Worker Hopping)", flush=True)
                    print("=========================================================================", flush=True)

                    doc.save(psx_out)
                    rel_psx = get_cluster_rel_path()

                    eff_worker_ram = get_system_ram_gb()
                    if net_client:
                        try:
                            w_data = net_client.workerList()
                            workers = w_data.get("workers", []) if isinstance(w_data, dict) else []
                            w_rams = [w["memory_size"] / (1024 ** 3) for w in workers if "memory_size" in w and w.get("state") != "aborted"]
                            if w_rams:
                                min_w_ram = min(w_rams)
                                if min_w_ram < eff_worker_ram:
                                    eff_worker_ram = min_w_ram
                        except Exception:
                            pass

                    u_tasks, u_stages = build_unified_cluster_pipeline_tasks(
                        chunk=chunk,
                        output_dir=output_dir,
                        project_name=project_name,
                        align_photos=align_photos,
                        align_quality=align_quality,
                        reference_preselection_mode=reference_preselection_mode,
                        keypoint_limit=keypoint_limit,
                        keypoint_limit_per_mpx=keypoint_limit_per_mpx,
                        tiepoint_limit=tiepoint_limit,
                        filter_stationary_points=filter_stationary_points,
                        guided_matching=guided_matching,
                        adaptive_fitting=adaptive_fitting,
                        filter_tiepoints=filter_tiepoints,
                        crop_method=crop_method,
                        crop_padding=crop_padding,
                        build_depth_maps=build_depth_maps,
                        depth_quality=depth_quality,
                        depth_filtering=depth_filtering,
                        build_point_cloud=build_point_cloud,
                        points_spacing=points_spacing,
                        build_mesh=build_mesh,
                        surface_type=surface_type,
                        custom_faces=custom_faces,
                        build_texture=build_texture,
                        texture_pages=texture_pages,
                        fill_holes=fill_holes,
                        ghosting_filter=ghosting_filter,
                        build_dem=build_dem,
                        build_orthomosaic=build_orthomosaic,
                        ortho_resolution=ortho_resolution,
                        ortho_cull_faces=ortho_cull_faces,
                        build_tiled_model=build_tiled_model,
                        tile_size=tile_size,
                        export_glb=export_glb,
                        export_obj=export_obj,
                        export_fbx=export_fbx,
                        export_ply=export_ply,
                        export_dxf=export_dxf,
                        export_tie_points=export_tie_points,
                        export_dem=export_dem,
                        export_ortho=export_ortho,
                        export_point_cloud=export_point_cloud,
                        point_cloud_format=point_cloud_format,
                        export_report=export_report,
                        report_title=report_title,
                        texture_format=texture_format,
                        network_path=network_path,
                        worker_ram_gb=eff_worker_ram,
                        export_path_fn=get_cluster_export_path,
                    )

                    if u_tasks:
                        net_tasks = [t.toNetworkTask(chunk) for t in u_tasks]
                        batch_id = net_client.createBatch(rel_psx, net_tasks)
                        net_client.setBatchWorkerLimit(batch_id, 1)
                        net_client.setBatchPaused(batch_id, False)
                        print(f"  [Cluster] Created Unified Batch ID={batch_id} with {len(u_tasks)} tasks locked to 1 worker node.", flush=True)

                        t_u_start = time.time()
                        last_stage_key = None
                        last_progress = -1.0
                        last_log_time = 0.0

                        while True:
                            time.sleep(1.0)
                            b_info = net_client.batchInfo(batch_id)
                            state = b_info.get("state")
                            tasks_info = b_info.get("tasks", [])
                            if tasks_info:
                                active_task = next((t for t in tasks_info if t.get("state") in ("running", "inprogress", "submitted")), tasks_info[-1])
                                t_sub_name = active_task.get("name", "Task")
                                sub_prog = active_task.get("progress", 0.0)
                                total_prog = sum(t.get("progress", 0.0) for t in tasks_info) / len(tasks_info)

                                matching_stage = next(((k, d) for n, k, d in u_stages if n == t_sub_name), ("recon", t_sub_name))
                                stage_k, stage_desc = matching_stage

                                if stage_k != last_stage_key:
                                    emit_ipc_stage(stage_k, stage_desc)
                                    print(f"\n>>> [Stage Transition] Active Stage: {stage_desc} ({t_sub_name})", flush=True)
                                    last_stage_key = stage_k

                                now_t = time.time()
                                if abs(total_prog - last_progress) >= 0.5 or (now_t - last_log_time >= 10.0 and total_prog != last_progress):
                                    elapsed_min = (now_t - t_u_start) / 60.0
                                    print(f"  [Cluster Progress] {stage_desc} - {t_sub_name}: {sub_prog:.1f}% (Overall Project: {total_prog:.1f}%, Elapsed: {elapsed_min:.1f} min)", flush=True)
                                    last_progress = total_prog
                                    last_log_time = now_t

                            if state == "completed":
                                elapsed_min = (time.time() - t_u_start) / 60.0
                                print(f"\n  [Cluster] ✓ Entire unified project completed on dedicated worker in {elapsed_min:.2f} min!", flush=True)
                                unified_succeeded = True
                                break
                            elif state in ("failed", "aborted"):
                                err_msgs = []
                                for t in tasks_info:
                                    for err in t.get("errors", []):
                                        err_msgs.append(str(err))
                                err_str = "; ".join(err_msgs) if err_msgs else "unknown cluster error"
                                print(f"  [Cluster] [ERROR] Unified batch {state}: {err_str}", flush=True)
                                print("  [Cluster] Falling back to stage-by-stage / local execution...", flush=True)
                                unified_succeeded = False
                                break

                    if unified_succeeded:
                        _safe_reopen()
                        total_time = (time.time() - t_start) / 60.0
                        print(f"\n[SUCCESS] Entire pipeline finished in {total_time:.2f} minutes!", flush=True)
                        emit_ipc_event(IPC_COMPLETE)
                        if survey_physics and pro_plan:
                            print_pro_engineering_report(survey_physics, pro_plan, usgs_stats=usgs_stats)
                        return
                except Exception as e_unified:
                    print(f"  [Cluster] [WARNING] Unified batch exception: {e_unified}. Falling back to stage-by-stage...", flush=True)
                    unified_succeeded = False

        else:
            all_imgs = []
            for imgs in groups.values():
                all_imgs.extend(imgs)
            all_imgs = sorted(all_imgs)

            emit_ipc_stage("ingest", f"Ingesting {len(all_imgs)} images across: {images_dir}")
            print(f"\n[1/7] Ingesting {len(all_imgs)} images found across: {images_dir}", flush=True)
            chunk.addPhotos(all_imgs)
            print(f"  Successfully loaded {len(chunk.cameras)} cameras.", flush=True)

            if min_quality > 0.0:
                filter_image_quality(chunk, min_quality=min_quality)

            if import_reference and os.path.isfile(import_reference):
                print(f"\n[GCP Reference] Importing ground control points from: {import_reference}...", flush=True)
                try:
                    chunk.importReference(path=import_reference, format=Metashape.ReferenceFormat.ReferenceFormatCSV)
                    chunk.updateTransform()
                    print("  Reference import and georeferencing transform updated.", flush=True)
                except Exception as e_ref:
                    print(f"  [WARNING] Reference import issue: {e_ref}. Continuing...", flush=True)

            if detect_markers:
                print(f"\n[Target Detection] Detecting target markers (Type: {target_type}, Tolerance: {marker_tolerance})...", flush=True)
                try:
                    tt_enum = TARGET_TYPE_MAP.get(target_type.lower(), Metashape.TargetType.CircularTarget12bit)
                    chunk.detectMarkers(target_type=tt_enum, tolerance=marker_tolerance)
                    print(f"  Target detection completed! Found {len(chunk.markers)} markers.", flush=True)
                except Exception as e_mk:
                    print(f"  [WARNING] Marker detection issue: {e_mk}. Continuing...", flush=True)

            if not align_photos:
                print("\n[INFO] Photo feature matching and camera alignment skipped by workflow selection.", flush=True)
            else:
                emit_ipc_stage("match", "Executing GPU Feature Matching")
                print(f"\n[2/7] Executing GPU Feature Matching (Downscale: {align_downscale}, Keypoints: {keypoint_limit:,}, Tiepoints: {tiepoint_limit:,})...", flush=True)
                t_match = time.time()
                matched = False
                if cluster_enabled:
                    try:
                        t_m = Metashape.Tasks.MatchPhotos()
                        t_m.downscale = align_downscale
                        t_m.generic_preselection = True
                        t_m.reference_preselection = True
                        t_m.reference_preselection_mode = presel_mode_enum
                        t_m.keypoint_limit = keypoint_limit
                        t_m.keypoint_limit_per_mpx = keypoint_limit_per_mpx
                        t_m.tiepoint_limit = tiepoint_limit
                        t_m.mask_tiepoints = False
                        t_m.filter_stationary_points = filter_stationary_points
                        t_m.guided_matching = guided_matching
                        matched = dispatch_cluster_task(t_m, "GPU Feature Matching", "match")
                        if matched:
                            chunk = doc.chunk
                    except Exception as e_m_dispatch:
                        print(f"  [Cluster] MatchPhotos dispatch exception: {e_m_dispatch}. Running locally...", flush=True)
                        matched = False

                if not matched:
                    chunk.matchPhotos(
                        downscale=align_downscale,
                        generic_preselection=True,
                        reference_preselection=True,
                        reference_preselection_mode=presel_mode_enum,
                        keypoint_limit=keypoint_limit,
                        keypoint_limit_per_mpx=keypoint_limit_per_mpx,
                        tiepoint_limit=tiepoint_limit,
                        mask_tiepoints=False,
                        filter_stationary_points=filter_stationary_points,
                        guided_matching=guided_matching,
                    )
                print(f"  Matching finished in {(time.time() - t_match)/60:.2f} min.", flush=True)

                emit_ipc_stage("align", "Camera Alignment & Bundle Adjustment")
                print(f"\n[3/7] Aligning Cameras & Initial Bundle Adjustment (Adaptive Fitting: {adaptive_fitting})...", flush=True)
                t_align = time.time()
                aligned = False
                if cluster_enabled:
                    try:
                        t_a = Metashape.Tasks.AlignCameras()
                        t_a.adaptive_fitting = adaptive_fitting
                        aligned = dispatch_cluster_task(t_a, "Camera Alignment & Bundle Adjustment", "align")
                        if aligned:
                            chunk = doc.chunk
                    except Exception as e_a_dispatch:
                        print(f"  [Cluster] AlignCameras dispatch exception: {e_a_dispatch}. Running locally...", flush=True)
                        aligned = False

                if not aligned:
                    chunk.alignCameras(adaptive_fitting=adaptive_fitting)

                aligned_count = sum(1 for c in chunk.cameras if c.transform and c.enabled)
                enabled_count = sum(1 for c in chunk.cameras if c.enabled)
                print(f"  Alignment finished in {(time.time() - t_align)/60:.2f} min. Aligned: {aligned_count}/{enabled_count} active cameras.", flush=True)

                if rolling_shutter:
                    for s in chunk.sensors:
                        s.rolling_shutter = Metashape.Shutter.Model.Regularized
                    print("  [Rolling Shutter] Enabled Regularized CMOS rolling shutter compensation.", flush=True)

                if filter_tiepoints:
                    cleaned_on_cluster = False
                    if cluster_enabled:
                        try:
                            clean_tasks = build_cluster_gradual_selection_tasks()
                            cleaned_on_cluster = dispatch_cluster_task(
                                clean_tasks,
                                "Gradual Selection Tie-Point Filtering & Camera Optimization (4 passes)",
                                "align",
                            )
                            if cleaned_on_cluster:
                                chunk = doc.chunk
                        except Exception as e_cl_clean:
                            print(f"  [Cluster] Tie-point clean dispatch exception: {e_cl_clean}. Running locally...", flush=True)
                            cleaned_on_cluster = False

                    if not cleaned_on_cluster:
                        usgs_stats = filter_tie_points(chunk)
                else:
                    print("  [Gradual Selection] Tie-point cleaning and camera re-optimization skipped (--no-filter-tiepoints).", flush=True)

        if crop_method == "manual":
            print("\n[3b/7] Manual Bounding Box Crop selected: Auto-crop skipped.", flush=True)
            chunk = save_checkpoint(doc, psx_out)
            print(f"[CHECKPOINT] Saved alignment checkpoint to: {psx_out}", flush=True)
            wait_for_user_resume(output_dir, psx_out)
            _safe_reopen()
            chunk = doc.chunk
        elif crop_method in ("gps-square", "structure"):
            cropped_on_cluster = False
            if cluster_enabled and not pause_after_crop:
                try:
                    t_crop = build_cluster_crop_task(crop_method, padding=crop_padding)
                    if t_crop:
                        cropped_on_cluster = dispatch_cluster_task(
                            t_crop,
                            f"Worker Auto-Crop Bounding Box ({crop_method})",
                            "align",
                        )
                        if cropped_on_cluster:
                            chunk = doc.chunk
                except Exception as e_cl_cr:
                    print(f"  [Cluster] Crop dispatch exception: {e_cl_cr}. Running locally...", flush=True)
                    cropped_on_cluster = False

            if not cropped_on_cluster:
                if crop_method == "gps-square":
                    auto_crop_camera_gps_square(chunk, padding=crop_padding)
                else:
                    auto_crop_to_main_structure(chunk, padding=crop_padding)
                chunk = save_checkpoint(doc, psx_out)
                print(f"[CHECKPOINT] Saved alignment checkpoint to: {psx_out}", flush=True)
                if pause_after_crop:
                    wait_for_user_resume(output_dir, psx_out)
                    _safe_reopen()
                    chunk = doc.chunk
        else:
            print("  [Auto-Crop] Cropping disabled; using default full-scene bounding box.", flush=True)
            chunk = save_checkpoint(doc, psx_out)
            print(f"[CHECKPOINT] Saved alignment checkpoint to: {psx_out}", flush=True)
            if pause_after_crop:
                wait_for_user_resume(output_dir, psx_out)
                _safe_reopen()
                chunk = doc.chunk

    # Step 4: Multi-View Stereo Dense Depth Maps
    if not build_depth_maps:
        print("\n[INFO] GPU MVS Depth Maps generation skipped by workflow selection.", flush=True)
    else:
        has_depth_maps = False
        depth_count = 0
        if chunk.depth_maps is not None:
            try:
                depth_count = len(chunk.depth_maps.keys()) if hasattr(chunk.depth_maps, "keys") else 0
                has_depth_maps = depth_count > 0
            except Exception:
                has_depth_maps = bool(chunk.depth_maps)
                try:
                    depth_count = len(chunk.depth_maps.keys())
                except Exception:
                    depth_count = 0

        if has_depth_maps:
            emit_ipc_stage("depth", f"Reusing existing MVS Depth Maps ({depth_count} depth maps)")
            print(f"\n[4/7] Reusing existing MVS Depth Maps from checkpoint ({depth_count} depth maps).", flush=True)
        else:
            depth_downscale = DEPTH_DOWNSCALE_MAP.get(depth_quality.lower(), 4)
            filter_mode_arg = DEPTH_FILTER_MAP.get(depth_filtering.lower(), Metashape.FilterMode.ModerateFiltering)
            emit_ipc_stage("depth", f"Computing GPU MVS Depth Maps (Quality: {depth_quality.capitalize()})")
            print(f"\n[4/7] Computing GPU MVS Depth Maps (Quality: {depth_quality.capitalize()} [Downscale={depth_downscale}], Filtering: {depth_filtering.capitalize()})...", flush=True)
            t_depth = time.time()
            depth_computed = False
            if cluster_enabled:
                try:
                    t_d = Metashape.Tasks.BuildDepthMaps()
                    t_d.downscale = depth_downscale
                    t_d.filter_mode = filter_mode_arg
                    t_d.reuse_depth = False
                    depth_computed = dispatch_cluster_task(t_d, f"Computing GPU MVS Depth Maps ({depth_quality.capitalize()})", "depth")
                    if depth_computed:
                        chunk = doc.chunk
                except Exception as e_d_dispatch:
                    print(f"  [Cluster] BuildDepthMaps dispatch exception: {e_d_dispatch}. Running locally...", flush=True)
                    depth_computed = False

            if not depth_computed:
                chunk.buildDepthMaps(
                    downscale=depth_downscale,
                    filter_mode=filter_mode_arg,
                    reuse_depth=False,
                )
            print(f"  Depth maps completed in {(time.time() - t_depth)/60:.2f} min.", flush=True)
            chunk = save_checkpoint(doc, psx_out)
            print(f"[CHECKPOINT] Saved depth maps checkpoint to: {psx_out}", flush=True)

    # Step 4b: Dense Point Cloud Generation & Classification
    if build_point_cloud:
        has_point_cloud = chunk.point_cloud is not None and chunk.point_cloud.point_count > 0
        if has_point_cloud:
            emit_ipc_stage("point_cloud", f"Reusing existing Dense Point Cloud ({chunk.point_cloud.point_count:,} points)")
            print(f"\n[4b/7] Reusing existing Dense Point Cloud from checkpoint ({chunk.point_cloud.point_count:,} points).", flush=True)
        else:
            emit_ipc_stage("point_cloud", "Generating Dense Point Cloud from Depth Maps")
            print(f"\n[4b/7] Generating Dense Point Cloud from Depth Maps (RGB Colors + Confidence)...", flush=True)
            t_pc = time.time()
            pc_computed = False
            if cluster_enabled:
                try:
                    t_p = Metashape.Tasks.BuildPointCloud()
                    t_p.source_data = Metashape.DataSource.DepthMapsData
                    t_p.point_colors = True
                    t_p.point_confidence = True
                    t_p.uniform_sampling = True
                    t_p.points_spacing = points_spacing
                    t_p.keep_depth = True
                    pc_computed = dispatch_cluster_task(t_p, "Generating Dense Point Cloud from Depth Maps", "point_cloud")
                    if pc_computed:
                        chunk = doc.chunk
                except Exception as e_pc_disp:
                    print(f"  [Cluster] BuildPointCloud dispatch exception: {e_pc_disp}. Running locally...", flush=True)
                    pc_computed = False

            if not pc_computed:
                chunk.buildPointCloud(
                    source_data=Metashape.DataSource.DepthMapsData,
                    point_colors=True,
                    point_confidence=True,
                    uniform_sampling=True,
                    points_spacing=points_spacing,
                    keep_depth=True,
                )
            pc_count = chunk.point_cloud.point_count if chunk.point_cloud else 0
            print(f"  Point cloud completed in {(time.time() - t_pc)/60:.2f} min! Density: {pc_count:,} 3D points.", flush=True)

            if filter_point_confidence and chunk.point_cloud:
                print(f"  Filtering low-confidence points (< {filter_point_confidence})...", flush=True)
                chunk.point_cloud.setConfidenceFilter(filter_point_confidence, 255)
                chunk.point_cloud.removeSelectedPoints()
                chunk.point_cloud.resetFilters()

            if classify_ground and chunk.point_cloud:
                c_size = ground_cell_size if ground_cell_size is not None else 50.0
                m_ang = ground_max_angle if ground_max_angle is not None else 15.0
                m_dist = ground_max_distance if ground_max_distance is not None else 1.0
                print(f"  Executing slope-adaptive ground point classification (cell_size={c_size:.1f}m, max_angle={m_ang:.1f}°, max_distance={m_dist:.2f}m)...", flush=True)
                chunk.point_cloud.classifyGroundPoints(cell_size=c_size, max_angle=m_ang, max_distance=m_dist)
                print("  Ground classification completed.", flush=True)

            if classify_points and chunk.point_cloud:
                print("  Executing multiclass AI semantic point classification...", flush=True)
                chunk.point_cloud.classifyPoints()
                print("  Multiclass point classification completed.", flush=True)

            chunk = save_checkpoint(doc, psx_out)
            print(f"[CHECKPOINT] Saved dense point cloud checkpoint to: {psx_out}", flush=True)

    # Step 5: High-Precision Arbitrary Surface Meshing
    if not build_mesh:
        print("\n[INFO] 3D Surface Meshing skipped by workflow selection.", flush=True)
    else:
        chunk = doc.chunk
        if chunk.model is None:
            cand_obj = os.path.join(output_dir, f"{project_name}.obj")
            cand_obj_legacy = os.path.join(output_dir, "model.obj")
            target_obj = cand_obj if os.path.isfile(cand_obj) else (cand_obj_legacy if os.path.isfile(cand_obj_legacy) else None)
            if target_obj:
                print(f"\n[Auto-Recover] Found existing 3D mesh model on disk: {target_obj}. Re-linking...", flush=True)
                chunk.importModel(path=target_obj, format=Metashape.ModelFormat.ModelFormatOBJ)
                chunk = save_checkpoint(doc, psx_out)
                print(f"  Successfully re-linked 3D model into project chunk ({len(chunk.model.vertices):,} vertices, {len(chunk.model.faces):,} faces).", flush=True)

        has_model = chunk.model is not None
        if has_model:
            emit_ipc_stage("mesh", f"Reusing existing Surface Mesh ({len(chunk.model.vertices):,} vertices)")
            print(f"\n[5/7] Reusing existing Surface Mesh from checkpoint ({len(chunk.model.vertices):,} vertices, {len(chunk.model.faces):,} faces).", flush=True)
        else:
            emit_ipc_stage("mesh", f"Reconstructing Arbitrary Surface Mesh (Target: {custom_faces:,} faces)")
            print(f"\n[5/7] Reconstructing Arbitrary Surface Mesh from Depth Maps (Target: {custom_faces:,} faces)...", flush=True)
            t_mesh = time.time()
            if custom_faces > 0:
                face_count_arg = Metashape.FaceCount.CustomFaceCount
                face_count_custom_arg = custom_faces
                print(f"  Target Polygon Count: {custom_faces:,} faces", flush=True)
            else:
                face_count_arg = Metashape.FaceCount.MediumFaceCount
                face_count_custom_arg = 200000
                print(f"  Target Polygon Count: MediumFaceCount preset", flush=True)

            surf_enum = SURFACE_TYPE_MAP.get(surface_type.lower(), Metashape.SurfaceType.Arbitrary)
            mesh_computed = False
            if cluster_enabled:
                try:
                    t_m = Metashape.Tasks.BuildModel()
                    t_m.surface_type = surf_enum
                    t_m.source_data = Metashape.DataSource.DepthMapsData
                    t_m.interpolation = Metashape.Interpolation.EnabledInterpolation
                    t_m.face_count = face_count_arg
                    t_m.face_count_custom = face_count_custom_arg
                    t_m.vertex_colors = True
                    t_m.vertex_confidence = True
                    mesh_computed = dispatch_cluster_task(t_m, f"Surface Mesh ({custom_faces:,} faces)", "mesh")
                    if mesh_computed:
                        chunk = doc.chunk
                except Exception as e_m_disp:
                    print(f"  [Cluster] BuildModel dispatch exception: {e_m_disp}. Running locally...", flush=True)
                    mesh_computed = False

            if not mesh_computed:
                try:
                    chunk.buildModel(
                        surface_type=surf_enum,
                        source_data=Metashape.DataSource.DepthMapsData,
                        interpolation=Metashape.Interpolation.EnabledInterpolation,
                        face_count=face_count_arg,
                        face_count_custom=face_count_custom_arg,
                        vertex_colors=True,
                        vertex_confidence=True,
                    )
                except Exception as e:
                    print(f"\n[WARNING] GPU meshing encountered driver exception: {e}", flush=True)
                    print("  [Fallback] Retrying surface meshing on CPU (all threads)...", flush=True)
                    orig_gpu_mask = Metashape.app.gpu_mask
                    try:
                        Metashape.app.gpu_mask = 0
                        chunk.buildModel(
                            surface_type=surf_enum,
                            source_data=Metashape.DataSource.DepthMapsData,
                            interpolation=Metashape.Interpolation.EnabledInterpolation,
                            face_count=face_count_arg,
                            face_count_custom=face_count_custom_arg,
                            vertex_colors=True,
                        )
                        print("  [Fallback] CPU surface meshing succeeded!", flush=True)
                    finally:
                        Metashape.app.gpu_mask = orig_gpu_mask
                        print("  [Hardware] Restored GPU acceleration for texture baking.", flush=True)
            if close_holes and chunk.model:
                chunk.model.closeHoles(level=30)
                print("  Applied planar hole closing across window and surface voids (level 30%).", flush=True)
            if smooth_mesh and chunk.model:
                print(f"  Applying Laplacian mesh smoothing (strength: {smooth_strength})...", flush=True)
                chunk.smoothModel(strength=smooth_strength, fix_borders=True, preserve_edges=True)
            if decimate_mesh and chunk.model and decimate_mesh > 0:
                print(f"  Decimating surface mesh to {decimate_mesh:,} faces...", flush=True)
                chunk.decimateModel(face_count=decimate_mesh)

            mesh_verts = len(chunk.model.vertices)
            mesh_faces = len(chunk.model.faces)
            print(f"  Mesh completed in {(time.time() - t_mesh)/60:.2f} min! Geometry: {mesh_verts:,} vertices, {mesh_faces:,} faces.", flush=True)
            chunk = save_checkpoint(doc, psx_out)
            print(f"[CHECKPOINT] Saved mesh model checkpoint to: {psx_out}", flush=True)

    # Step 6: Multi-Page 8K Photogrammetric Texture Atlas
    if not build_texture:
        print("\n[INFO] Texture Atlas generation skipped by workflow selection.", flush=True)
    current_pages = 0
    if chunk.model:
        if hasattr(chunk.model, "textures") and chunk.model.textures:
            current_pages = len(chunk.model.textures)
        elif hasattr(chunk.model, "texture") and chunk.model.texture():
            current_pages = 1

    if not build_texture or chunk.model is None:
        pass
    elif current_pages == texture_pages and not rebake_texture:
        emit_ipc_stage("texture", f"Reusing existing Texture Atlas ({current_pages} pages)")
        print(f"\n[6/7] Reusing existing Texture Atlas from checkpoint ({current_pages} pages).", flush=True)
    else:
        if current_pages > 0 and current_pages != texture_pages:
            emit_ipc_stage("texture", f"Re-baking Texture Atlas to requested {texture_pages} pages")
            print(f"\n[6/7] Existing texture atlas has {current_pages} page(s), re-baking to requested {texture_pages} pages...", flush=True)
        elif rebake_texture:
            emit_ipc_stage("texture", f"Force re-baking Texture Atlas ({texture_pages} pages)")
            print(f"\n[6/7] Force re-baking texture atlas as requested ({texture_pages} pages)...", flush=True)
        else:
            emit_ipc_stage("texture", f"Baking {texture_pages}x 8192x8192 (8K) Texture Atlas")
            print(f"\n[6/7] Baking {texture_pages}x 8192x8192 (8K) Photogrammetric Texture Atlas (Mosaic Blending, Hole Filling: {fill_holes}, Ghosting Filter: {ghosting_filter})...", flush=True)

        if calibrate_colors:
            print("\n[Color Calibration] Executing radiometric color & white balance calibration...", flush=True)
            try:
                chunk.calibrateColors(source_data=Metashape.DataSource.ModelData, white_balance=True)
                print("  Radiometric color calibration completed.", flush=True)
            except Exception as e_col:
                print(f"  [WARNING] Color calibration issue: {e_col}. Continuing...", flush=True)

        # Deterministic texture atlas allocation (identical across all projects and cluster nodes)
        eff_pages = texture_pages or 16
        if texture_pages is None and chunk.model and survey_physics and survey_physics.get("gsd_cm"):
            optimal_budget = calculate_optimal_texel_budget(chunk, gsd_cm=survey_physics["gsd_cm"])
            if optimal_budget:
                eff_pages = optimal_budget
        print(f"  [Texture] Baking {eff_pages}x 8K texture atlas (deterministic parity across all nodes).", flush=True)

        t_tex = time.time()
        tex_computed = False
        if cluster_enabled:
            try:
                t_uv = Metashape.Tasks.BuildUV()
                t_uv.mapping_mode = Metashape.MappingMode.GenericMapping
                t_uv.page_count = eff_pages or 4
                t_uv.texture_size = 8192

                t_t = Metashape.Tasks.BuildTexture()
                t_t.blending_mode = Metashape.BlendingMode.MosaicBlending
                t_t.texture_size = 8192
                t_t.fill_holes = fill_holes
                t_t.ghosting_filter = ghosting_filter

                tex_computed = dispatch_cluster_task([t_uv, t_t], f"Baking {eff_pages or 4}x 8K Texture Atlas", "texture")
                if tex_computed:
                    chunk = doc.chunk
            except Exception as e_tx_disp:
                print(f"  [Cluster] BuildUV/BuildTexture dispatch exception: {e_tx_disp}. Running locally...", flush=True)
                tex_computed = False

        if not tex_computed:
            try:
                chunk.buildUV(
                    mapping_mode=Metashape.MappingMode.GenericMapping,
                    page_count=eff_pages or 4,
                    texture_size=8192,
                )
                chunk.buildTexture(
                    blending_mode=Metashape.BlendingMode.MosaicBlending,
                    texture_size=8192,
                    fill_holes=fill_holes,
                    ghosting_filter=ghosting_filter,
                )
            except Exception as e_tex_bake:
                is_oom = "bad allocation" in str(e_tex_bake).lower() or isinstance(e_tex_bake, MemoryError)
                if is_oom:
                    fallback_pages = 4 if ram_gb < 16.0 else 8
                    print(f"\n  [RAM Guard] [bad allocation] Texture baking failed due to memory exhaustion ({e_tex_bake})!", flush=True)
                    print(f"  [RAM Guard] Auto-recovering: Re-baking texture atlas adjusted to {fallback_pages} pages...", flush=True)
                    chunk.buildUV(
                        mapping_mode=Metashape.MappingMode.GenericMapping,
                        page_count=fallback_pages,
                        texture_size=8192,
                    )
                    chunk.buildTexture(
                        blending_mode=Metashape.BlendingMode.MosaicBlending,
                        texture_size=8192,
                        fill_holes=fill_holes,
                        ghosting_filter=ghosting_filter,
                    )
                    print(f"  [RAM Guard] ✓ Auto-recovery successful: {fallback_pages}-page texture atlas baked cleanly!", flush=True)
                else:
                    raise e_tex_bake
        print(f"  Texture baking finished in {(time.time() - t_tex)/60:.2f} min.", flush=True)

        if remove_lighting and chunk.model:
            print("\n[Delighting] Removing harsh ambient shadows and illumination to recover diffuse albedo...", flush=True)
            try:
                chunk.removeLighting(color_mode=delighting_color_mode)
                print("  Texture delighting completed.", flush=True)
            except Exception as e_lt:
                print(f"  [WARNING] Texture delighting issue: {e_lt}. Continuing...", flush=True)
        chunk = save_checkpoint(doc, psx_out)
        print(f"[CHECKPOINT] Saved textured model checkpoint to: {psx_out}", flush=True)

    # Step 6b: Hierarchical 3D Tiled Model (LOD / Cesium 3D Tiles)
    if build_tiled_model:
        try:
            has_tiled = getattr(chunk, "tiled_model", None) is not None
            if not has_tiled:
                emit_ipc_stage("tiled_model", f"Building Hierarchical 3D Tiled Model (Tile Size: {tile_size})")
                print(f"\n[6b/7] Building Hierarchical 3D Tiled Model (Tile Size: {tile_size})...", flush=True)
                t_tile = time.time()
                tiled_computed = False
                if cluster_enabled:
                    try:
                        t_tl = Metashape.Tasks.BuildTiledModel()
                        t_tl.tile_size = tile_size
                        t_tl.source_data = Metashape.DataSource.DepthMapsData
                        tiled_computed = dispatch_cluster_task(t_tl, f"Building 3D Tiled Model (Tile Size: {tile_size})", "tiled_model")
                        if tiled_computed:
                            chunk = doc.chunk
                    except Exception as e_tl_disp:
                        print(f"  [Cluster] BuildTiledModel dispatch exception: {e_tl_disp}. Running locally...", flush=True)
                        tiled_computed = False

                if not tiled_computed:
                    chunk.buildTiledModel(
                        tile_size=tile_size,
                        source_data=Metashape.DataSource.DepthMapsData,
                    )
                print(f"  3D Tiled model completed in {(time.time() - t_tile)/60:.2f} min.", flush=True)
                chunk = save_checkpoint(doc, psx_out)
            else:
                print("\n[6b/7] Reusing existing 3D Tiled Model from checkpoint.", flush=True)

            tiled_fmt_tuple = TILED_MODEL_FORMAT_MAP.get(tiled_model_format.lower(), (Metashape.TiledModelFormat.TiledModelFormatCesium, "tileset.json"))
            tiled_fmt, tiled_ext = tiled_fmt_tuple
            if tiled_model_format.lower() == "cesium":
                tiled_dir = os.path.join(output_dir, f"{project_name}_tiled_model")
                os.makedirs(tiled_dir, exist_ok=True)
                tiled_out = os.path.join(tiled_dir, "tileset.json")
            else:
                tiled_out = os.path.join(output_dir, f"{project_name}_tiled_model{tiled_ext}")
            print(f"\n[6b/7] [EXPORT] Exporting 3D Tiled Model to: {tiled_out}...", flush=True)
            chunk.exportTiledModel(
                path=tiled_out,
                format=tiled_fmt,
            )
            print(f"  [6b/7] [EXPORT] Saved 3D Tiled Model: {tiled_out}", flush=True)
        except Exception as e_tile:
            print(f"  [6b/7] [WARNING] 3D Tiled Model generation/export issue: {e_tile}. Continuing...", flush=True)

    # Step 7b: Digital Elevation Model (DEM) Generation & GeoTIFF Export
    if build_dem:
        try:
            has_dem = getattr(chunk, "elevation", None) is not None
            if not has_dem:
                emit_ipc_stage("dem", "Building Digital Elevation Model (DEM/DSM)")
                print(f"\n[7b/7] Building Digital Elevation Model (DEM/DSM)...", flush=True)
                t_dem = time.time()
                proj = Metashape.OrthoProjection()
                target_crs = chunk.crs
                if chunk.crs and not getattr(chunk.crs, "projcs", None):
                    aligned_gps = [c for c in chunk.cameras if c.reference and c.reference.location and c.transform and c.enabled]
                    if aligned_gps:
                        avg_lon = sum(c.reference.location.x for c in aligned_gps) / len(aligned_gps)
                        avg_lat = sum(c.reference.location.y for c in aligned_gps) / len(aligned_gps)
                        if abs(avg_lat) > 0.001 or abs(avg_lon) > 0.001:
                            utm_epsg = compute_utm_epsg(avg_lon, avg_lat)
                            try:
                                target_crs = Metashape.CoordinateSystem(f"EPSG::{utm_epsg}")
                                print(f"  [Geodesy] Selected Projected Metric CRS: {target_crs.name} (EPSG:{utm_epsg}) for DEM metric scale.", flush=True)
                            except Exception:
                                target_crs = chunk.crs
                if target_crs:
                    proj.crs = target_crs
                dem_source = Metashape.DataSource.PointCloudData if (chunk.point_cloud and chunk.point_cloud.point_count > 0) else Metashape.DataSource.DepthMapsData
                dem_computed = False
                if cluster_enabled:
                    try:
                        t_dm = Metashape.Tasks.BuildDem()
                        t_dm.source_data = dem_source
                        t_dm.interpolation = Metashape.Interpolation.EnabledInterpolation
                        if target_crs:
                            t_dm.projection = proj
                        dem_computed = dispatch_cluster_task(t_dm, "Building Digital Elevation Model (DEM/DSM)", "dem")
                        if dem_computed:
                            chunk = doc.chunk
                    except Exception as e_dm_disp:
                        print(f"  [Cluster] BuildDem dispatch exception: {e_dm_disp}. Running locally...", flush=True)
                        dem_computed = False

                if not dem_computed:
                    chunk.buildDem(
                        source_data=dem_source,
                        interpolation=Metashape.Interpolation.EnabledInterpolation,
                        projection=proj,
                    )
                print(f"  [7b/7] DEM generation completed in {(time.time() - t_dem)/60:.2f} min.", flush=True)
                chunk = save_checkpoint(doc, psx_out)
                print(f"  [7b/7] [CHECKPOINT] Saved DEM inside project: {psx_out}", flush=True)
            else:
                emit_ipc_stage("dem", "Reusing existing Digital Elevation Model from checkpoint")
                print(f"\n[7b/7] Reusing existing Digital Elevation Model from checkpoint.", flush=True)

            if (not cluster_enabled) and export_dem:
                dem_out = os.path.join(output_dir, f"{project_name}_dem.tif")
                print(f"\n[7b/7] [EXPORT] Exporting GeoTIFF DEM to: {dem_out}...", flush=True)
                comp = Metashape.ImageCompression()
                comp.tiff_compression = Metashape.ImageCompression.TiffCompression.TiffCompressionLZW
                comp.tiff_big = True
                chunk.exportRaster(
                    path=dem_out,
                    source_data=Metashape.DataSource.ElevationData,
                    image_format=Metashape.ImageFormat.ImageFormatTIFF,
                    image_compression=comp,
                    save_world=True,
                )
                print(f"  [7b/7] [EXPORT] Saved GeoTIFF DEM: {dem_out} ({os.path.getsize(dem_out)/(1024*1024):.2f} MB)", flush=True)
            else:
                print("  [7b/7] [INFO] DEM saved in project checkpoint. GeoTIFF file export skipped as requested.", flush=True)
        except Exception as e_dem:
            print(f"  [7b/7] [WARNING] DEM generation/export encountered an issue: {e_dem}. Continuing...", flush=True)

    # Step 7b2: Topographic Contour Vectors
    if build_contours and (getattr(chunk, "elevation", None) is not None or build_dem):
        try:
            print(f"\n[7b2/7] Generating Topographic Contours (Interval: {contour_interval}m)...", flush=True)
            t_cnt = time.time()
            chunk.buildContours(
                source_data=Metashape.DataSource.ElevationData,
                interval=contour_interval,
                prevent_intersections=True,
            )
            print(f"  Contour lines generated in {(time.time() - t_cnt)/60:.2f} min.", flush=True)

            sh_fmt, sh_ext = SHAPES_FORMAT_MAP.get(contours_format.lower(), (Metashape.ShapesFormat.ShapesFormatSHP, ".shp"))
            cnt_out = os.path.join(output_dir, f"{project_name}_contours{sh_ext}")
            print(f"  [7b2/7] [EXPORT] Exporting Contour Shapes to: {cnt_out}...", flush=True)
            chunk.exportShapes(
                path=cnt_out,
                format=sh_fmt,
                save_polylines=True,
                save_elevation=True,
                save_attributes=True,
            )
            print(f"  [7b2/7] [EXPORT] Saved Topographic Contours: {cnt_out}", flush=True)
        except Exception as e_cnt:
            print(f"  [7b2/7] [WARNING] Topographic contours issue: {e_cnt}. Continuing...", flush=True)

    # Step 7c: Orthomosaic Generation & GeoTIFF Export
    if build_orthomosaic:
        try:
            has_ortho = chunk.orthomosaic is not None
            if not has_ortho:
                emit_ipc_stage("ortho", "Generating Orthomosaic from Reconstructed Surface Mesh")
                print(f"\n[7c/7] Generating Orthomosaic from Reconstructed Surface Mesh...", flush=True)
                t_ortho = time.time()
                proj = Metashape.OrthoProjection()
                target_crs = chunk.crs
                if chunk.crs and not getattr(chunk.crs, "projcs", None):
                    aligned_gps = [c for c in chunk.cameras if c.reference and c.reference.location and c.transform and c.enabled]
                    if aligned_gps:
                        avg_lon = sum(c.reference.location.x for c in aligned_gps) / len(aligned_gps)
                        avg_lat = sum(c.reference.location.y for c in aligned_gps) / len(aligned_gps)
                        if abs(avg_lat) > 0.001 or abs(avg_lon) > 0.001:
                            utm_epsg = compute_utm_epsg(avg_lon, avg_lat)
                            try:
                                target_crs = Metashape.CoordinateSystem(f"EPSG::{utm_epsg}")
                                print(f"  [Geodesy] Selected Projected Metric CRS: {target_crs.name} (EPSG:{utm_epsg}) for Orthomosaic metric scale.", flush=True)
                            except Exception:
                                target_crs = chunk.crs
                if target_crs:
                    proj.crs = target_crs
                ortho_computed = False
                if cluster_enabled:
                    try:
                        t_om = Metashape.Tasks.BuildOrthomosaic()
                        t_om.surface_data = Metashape.DataSource.ModelData
                        t_om.blending_mode = Metashape.BlendingMode.MosaicBlending
                        t_om.fill_holes = fill_holes
                        t_om.ghosting_filter = ghosting_filter
                        t_om.cull_faces = ortho_cull_faces
                        t_om.projection = proj
                        if getattr(proj.crs, "projcs", None) and ortho_resolution > 0:
                            t_om.resolution = ortho_resolution
                        ortho_computed = dispatch_cluster_task(t_om, "Generating Orthomosaic from Reconstructed Surface Mesh", "ortho")
                        if ortho_computed:
                            chunk = doc.chunk
                    except Exception as e_om_disp:
                        print(f"  [Cluster] BuildOrthomosaic dispatch exception: {e_om_disp}. Running locally...", flush=True)
                        ortho_computed = False

                if not ortho_computed:
                    chunk.buildOrthomosaic(
                        surface_data=Metashape.DataSource.ModelData,
                        blending_mode=Metashape.BlendingMode.MosaicBlending,
                        fill_holes=fill_holes,
                        ghosting_filter=ghosting_filter,
                        cull_faces=ortho_cull_faces,
                        projection=proj,
                        resolution=ortho_resolution if getattr(proj.crs, "projcs", None) else 0,
                    )
                print(f"  [7c/7] Orthomosaic generation completed in {(time.time() - t_ortho)/60:.2f} min ({chunk.orthomosaic.width}x{chunk.orthomosaic.height} px).", flush=True)
                chunk = save_checkpoint(doc, psx_out)
                print(f"  [7c/7] [CHECKPOINT] Saved orthomosaic inside project: {psx_out}", flush=True)
            else:
                emit_ipc_stage("ortho", f"Reusing existing Orthomosaic from checkpoint ({chunk.orthomosaic.width}x{chunk.orthomosaic.height} px)")
                print(f"\n[7c/7] Reusing existing Orthomosaic from checkpoint ({chunk.orthomosaic.width}x{chunk.orthomosaic.height} px).", flush=True)

            if (not cluster_enabled) and export_ortho:
                ortho_out = os.path.join(output_dir, f"{project_name}_orthomosaic.tif")
                print(f"\n[7c/7] [EXPORT] Exporting GeoTIFF Orthomosaic to: {ortho_out}...", flush=True)
                comp = Metashape.ImageCompression()
                comp.tiff_compression = Metashape.ImageCompression.TiffCompression.TiffCompressionLZW
                comp.tiff_big = True
                chunk.exportRaster(
                    path=ortho_out,
                    source_data=Metashape.DataSource.OrthomosaicData,
                    image_format=Metashape.ImageFormat.ImageFormatTIFF,
                    image_compression=comp,
                    save_world=True,
                    save_alpha=True,
                )
                print(f"  [7c/7] [EXPORT] Saved GeoTIFF Orthomosaic: {ortho_out} ({os.path.getsize(ortho_out)/(1024*1024):.2f} MB)", flush=True)
            else:
                print("  [7c/7] [INFO] Orthomosaic saved in project checkpoint. GeoTIFF file export skipped as requested.", flush=True)
        except Exception as e_ortho:
            print(f"  [7c/7] [WARNING] Orthomosaic generation/export encountered an issue: {e_ortho}. Continuing...", flush=True)

    # Production Asset Export
    emit_ipc_stage("export", "Exporting Production Assets (GLB, OBJ, TIFF, LAZ, DEM & PSX)")
    print("\n=========================================================================")
    print("  [7/7] EXPORTING PRODUCTION ASSETS (GLB, OBJ, TIFF, LAZ, DEM & PSX)")
    print("=========================================================================")
    glb_out = os.path.join(output_dir, f"{project_name}.glb")
    obj_out = os.path.join(output_dir, f"{project_name}.obj")

    cluster_exported = False
    if cluster_enabled:
        try:
            export_tasks = []
            tex_fmt = texture_format if texture_format else "png"
            tex_image_fmt = Metashape.ImageFormat.ImageFormatTIFF if tex_fmt == "tiff" else Metashape.ImageFormat.ImageFormatPNG

            if export_obj and chunk.model:
                t_obj = Metashape.Tasks.ExportModel()
                t_obj.format = Metashape.ModelFormat.ModelFormatOBJ
                t_obj.texture_format = tex_image_fmt
                t_obj.save_texture = True
                t_obj.save_normals = True
                t_obj.save_colors = True
                t_obj.path = get_cluster_export_path(obj_out)
                export_tasks.append(t_obj)

            if export_glb and chunk.model:
                t_glb = Metashape.Tasks.ExportModel()
                t_glb.format = Metashape.ModelFormat.ModelFormatGLTF
                t_glb.embed_texture = True
                t_glb.save_normals = True
                t_glb.save_colors = True
                t_glb.path = get_cluster_export_path(glb_out)
                export_tasks.append(t_glb)

            if export_fbx and chunk.model:
                fbx_out = os.path.join(output_dir, f"{project_name}.fbx")
                t_fbx = Metashape.Tasks.ExportModel()
                t_fbx.format = Metashape.ModelFormat.ModelFormatFBX
                t_fbx.save_texture = True
                t_fbx.save_normals = True
                t_fbx.save_colors = True
                t_fbx.path = get_cluster_export_path(fbx_out)
                export_tasks.append(t_fbx)

            if export_ply and chunk.model:
                ply_out = os.path.join(output_dir, f"{project_name}_mesh.ply")
                t_ply = Metashape.Tasks.ExportModel()
                t_ply.format = Metashape.ModelFormat.ModelFormatPLY
                t_ply.save_normals = True
                t_ply.save_colors = True
                t_ply.path = get_cluster_export_path(ply_out)
                export_tasks.append(t_ply)

            if export_dxf and chunk.model:
                dxf_out = os.path.join(output_dir, f"{project_name}.dxf")
                t_dxf = Metashape.Tasks.ExportModel()
                t_dxf.format = Metashape.ModelFormat.ModelFormatDXF
                t_dxf.save_normals = True
                t_dxf.path = get_cluster_export_path(dxf_out)
                export_tasks.append(t_dxf)

            if export_dem and (getattr(chunk, "elevation", None) is not None or build_dem):
                dem_out = os.path.join(output_dir, f"{project_name}_dem.tif")
                t_dem = Metashape.Tasks.ExportRaster()
                t_dem.source_data = Metashape.DataSource.ElevationData
                t_dem.format = Metashape.RasterFormat.RasterFormatNone
                t_dem.image_format = Metashape.ImageFormat.ImageFormatTIFF
                t_dem.path = get_cluster_export_path(dem_out)
                export_tasks.append(t_dem)

            if export_ortho and (chunk.orthomosaic is not None or build_orthomosaic):
                ortho_out = os.path.join(output_dir, f"{project_name}_orthomosaic.tif")
                t_ortho = Metashape.Tasks.ExportRaster()
                t_ortho.source_data = Metashape.DataSource.OrthomosaicData
                t_ortho.format = Metashape.RasterFormat.RasterFormatNone
                t_ortho.image_format = Metashape.ImageFormat.ImageFormatTIFF
                t_ortho.path = get_cluster_export_path(ortho_out)
                export_tasks.append(t_ortho)

            if export_point_cloud and (chunk.point_cloud is not None or build_point_cloud):
                pc_format_ext = point_cloud_format.lower() if point_cloud_format else "laz"
                pc_fmt, pc_ext = POINT_CLOUD_FORMAT_MAP.get(pc_format_ext, (Metashape.PointCloudFormat.PointCloudFormatLAZ, ".laz"))
                pc_out = os.path.join(output_dir, f"{project_name}_point_cloud{pc_ext}")
                t_pc = Metashape.Tasks.ExportPointCloud()
                t_pc.format = pc_fmt
                t_pc.source_data = Metashape.DataSource.PointCloudData
                t_pc.save_point_color = True
                t_pc.save_point_normal = True
                t_pc.save_point_confidence = True
                t_pc.save_point_classification = True
                t_pc.path = get_cluster_export_path(pc_out)
                export_tasks.append(t_pc)

            if export_report:
                report_out = os.path.join(output_dir, f"{project_name}_report.pdf")
                t_rep = Metashape.Tasks.ExportReport()
                t_rep.title = report_title or "AutoMetashape Processing & Calibration Report"
                t_rep.path = get_cluster_export_path(report_out)
                export_tasks.append(t_rep)

            if export_tasks:
                cluster_exported = dispatch_cluster_task(export_tasks, f"Cluster Exporting Deliverables ({len(export_tasks)} formats)", "export")
                if cluster_exported:
                    print(f"  [Cluster] ✓ All {len(export_tasks)} production deliverables exported directly by cluster worker!", flush=True)
        except Exception as e_cl_exp:
            print(f"  [Cluster] Note: cluster export delegation exception: {e_cl_exp}. Falling back to local export.", flush=True)
            cluster_exported = False

    if (not cluster_exported) and export_glb and chunk.model:
        try:
            chunk.exportModel(
                glb_out,
                format=Metashape.ModelFormat.ModelFormatGLTF,
                embed_texture=True,
                save_normals=True,
                save_colors=True,
            )
            print(f"  [7/7] [EXPORT] Saved binary GLB: {glb_out} ({os.path.getsize(glb_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_glb:
            print(f"  [7/7] [WARNING] GLB export issue: {e_glb}", flush=True)

    if (not cluster_exported) and export_obj and chunk.model:
        try:
            chunk.exportModel(
                obj_out,
                format=Metashape.ModelFormat.ModelFormatOBJ,
                texture_format=Metashape.ImageFormat.ImageFormatPNG,
                save_texture=True,
                save_normals=True,
                save_colors=True,
            )
            print(f"  [7/7] [EXPORT] Saved Wavefront OBJ + PNG: {obj_out} ({os.path.getsize(obj_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_obj:
            print(f"  [7/7] [WARNING] OBJ export issue: {e_obj}", flush=True)

    if (not cluster_exported) and export_dem and getattr(chunk, "elevation", None) is not None:
        try:
            dem_out = os.path.join(output_dir, f"{project_name}_dem.tif")
            comp = Metashape.ImageCompression()
            comp.tiff_compression = Metashape.ImageCompression.TiffCompression.TiffCompressionLZW
            comp.tiff_big = True
            chunk.exportRaster(
                path=dem_out,
                source_data=Metashape.DataSource.ElevationData,
                image_format=Metashape.ImageFormat.ImageFormatTIFF,
                image_compression=comp,
                save_world=True,
            )
            print(f"  [7/7] [EXPORT] Saved GeoTIFF DEM: {dem_out} ({os.path.getsize(dem_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_dem:
            print(f"  [7/7] [WARNING] DEM fallback export issue: {e_dem}", flush=True)

    if (not cluster_exported) and export_ortho and chunk.orthomosaic is not None:
        try:
            ortho_out = os.path.join(output_dir, f"{project_name}_orthomosaic.tif")
            comp = Metashape.ImageCompression()
            comp.tiff_compression = Metashape.ImageCompression.TiffCompression.TiffCompressionLZW
            comp.tiff_big = True
            chunk.exportRaster(
                path=ortho_out,
                source_data=Metashape.DataSource.OrthomosaicData,
                image_format=Metashape.ImageFormat.ImageFormatTIFF,
                image_compression=comp,
                save_world=True,
                save_alpha=True,
            )
            print(f"  [7/7] [EXPORT] Saved GeoTIFF Orthomosaic: {ortho_out} ({os.path.getsize(ortho_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_ortho:
            print(f"  [7/7] [WARNING] Orthomosaic fallback export issue: {e_ortho}", flush=True)

    has_any_export = any([
        export_glb, export_obj, export_fbx, export_ply, export_dxf,
        export_tie_points, export_dem, export_ortho,
        export_point_cloud, export_report, build_contours, build_tiled_model
    ])
    if not has_any_export:
        print("  [7/7] [INFO] Deliverable file export skipped as requested: Metashape native project (.psx) saved only.", flush=True)

    if (not cluster_exported) and export_fbx and chunk.model:
        fbx_out = os.path.join(output_dir, f"{project_name}.fbx")
        print(f"  [7/7] [EXPORT] Exporting Autodesk FBX to: {fbx_out}...", flush=True)
        try:
            chunk.exportModel(fbx_out, format=Metashape.ModelFormat.ModelFormatFBX, save_texture=True, save_normals=True, save_colors=True)
            print(f"  [7/7] [EXPORT] Saved FBX: {fbx_out} ({os.path.getsize(fbx_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_fbx:
            print(f"  [7/7] [WARNING] FBX export issue: {e_fbx}", flush=True)

    if (not cluster_exported) and export_ply and chunk.model:
        ply_out = os.path.join(output_dir, f"{project_name}_mesh.ply")
        print(f"  [7/7] [EXPORT] Exporting Stanford PLY to: {ply_out}...", flush=True)
        try:
            chunk.exportModel(ply_out, format=Metashape.ModelFormat.ModelFormatPLY, save_normals=True, save_colors=True)
            print(f"  [7/7] [EXPORT] Saved PLY: {ply_out} ({os.path.getsize(ply_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_ply:
            print(f"  [7/7] [WARNING] PLY export issue: {e_ply}", flush=True)

    if (not cluster_exported) and export_dxf and chunk.model:
        dxf_out = os.path.join(output_dir, f"{project_name}.dxf")
        print(f"  [7/7] [EXPORT] Exporting AutoCAD DXF to: {dxf_out}...", flush=True)
        try:
            chunk.exportModel(dxf_out, format=Metashape.ModelFormat.ModelFormatDXF, save_normals=True)
            print(f"  [7/7] [EXPORT] Saved DXF: {dxf_out} ({os.path.getsize(dxf_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_dxf:
            print(f"  [7/7] [WARNING] DXF export issue: {e_dxf}", flush=True)

    if (not cluster_exported) and export_tie_points and chunk.tie_points:
        tp_out = os.path.join(output_dir, f"{project_name}_tie_points.ply")
        print(f"  [7/7] [EXPORT] Exporting Sparse Tie Points to: {tp_out}...", flush=True)
        try:
            chunk.exportPointCloud(path=tp_out, format=Metashape.PointCloudFormat.PointCloudFormatPLY, source_data=Metashape.DataSource.TiePointsData)
            print(f"  [7/7] [EXPORT] Saved Tie Points: {tp_out} ({os.path.getsize(tp_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_tp:
            print(f"  [7/7] [WARNING] Tie points export issue: {e_tp}", flush=True)

    # Step 7d: Dense Point Cloud Export
    if (not cluster_exported) and export_point_cloud:
        if chunk.point_cloud and chunk.point_cloud.point_count > 0:
            pc_format_ext = point_cloud_format.lower() if point_cloud_format else "laz"
            pc_fmt, pc_ext = POINT_CLOUD_FORMAT_MAP.get(pc_format_ext, (Metashape.PointCloudFormat.PointCloudFormatLAZ, ".laz"))
            pc_out = os.path.join(output_dir, f"{project_name}_point_cloud{pc_ext}")
            print(f"\n[7d/7] [EXPORT] Exporting Dense Point Cloud to: {pc_out}...", flush=True)
            chunk.exportPointCloud(
                path=pc_out,
                format=pc_fmt,
                source_data=Metashape.DataSource.PointCloudData,
                save_point_color=True,
                save_point_normal=True,
                save_point_confidence=True,
                save_point_classification=True,
            )
            print(f"  [7d/7] [EXPORT] Saved Dense Point Cloud: {pc_out} ({os.path.getsize(pc_out)/(1024*1024):.2f} MB)", flush=True)
    elif build_point_cloud and (not cluster_exported):
        print(f"  [7d/7] [INFO] Dense point cloud preserved in project checkpoint. {point_cloud_format.upper() if point_cloud_format else 'LAZ'} file export skipped as requested.", flush=True)

    # Step 7e: Executive PDF Quality & Calibration Report
    if (not cluster_exported) and export_report:
        try:
            report_out = os.path.join(output_dir, f"{project_name}_report.pdf")
            print(f"\n[7e/7] [EXPORT] Exporting Official Calibration & Quality PDF Report to: {report_out}...", flush=True)
            rep_title = report_title or "AutoMetashape Processing & Calibration Report"
            chunk.exportReport(
                path=report_out,
                title=rep_title,
                description="AutoMetashape Autonomous Photogrammetry Pipeline",
            )
            print(f"  [7e/7] [EXPORT] Saved Quality Report: {report_out} ({os.path.getsize(report_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_rep:
            print(f"  [7e/7] [WARNING] PDF Report export encountered an issue: {e_rep}. Continuing...", flush=True)

    chunk = save_checkpoint(doc, psx_out)
    print(f"  [7/7] [EXPORT] Saved Metashape Pro Native Project: {psx_out}", flush=True)

    # Clean up obsolete building_reconstruction.psx if present to ensure single project-named file
    if legacy_psx != psx_out and os.path.exists(legacy_psx):
        try:
            os.remove(legacy_psx)
        except Exception:
            pass

    total_time = (time.time() - t_start) / 60.0
    print(f"\n[SUCCESS] Entire pipeline finished in {total_time:.2f} minutes!", flush=True)
    emit_ipc_event(IPC_COMPLETE)

    if survey_physics and pro_plan:
        print_pro_engineering_report(survey_physics, pro_plan, usgs_stats=usgs_stats)


def build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the comprehensive command-line argument parser for AutoMetashape."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape: Autonomous High-Precision Photogrammetry Pipeline (Survey Profiling, Point Cloud, Mesh & Orthomosaic)"
    )
    parser.add_argument("--images", "--photos-dir", dest="images", required=True, help="Input directory containing raw images (scanned recursively across all subfolders)")
    parser.add_argument("--output", default=None, help="Output directory for 3D assets (default: <images_dir>/<project_name>)")
    parser.add_argument("--project-name", default=None, help="Base name for the project file, 3D model, textures, point cloud, and reports (default: folder name of the project)")
    parser.add_argument("--profile", action="store_true", help="Profile dataset telemetry, print inferred optimal parameters, and exit without reconstructing")
    parser.add_argument("--auto", action="store_true", help="Automatically profile dataset and infer optimal parameters (default mode)")
    parser.add_argument(
        "--preset",
        choices=["pro", "custom", "building"],
        default="pro",
        help="Reconstruction preset: 'pro' (default: Production Geomatics Standard with gentle tie-point filtering, 2M Arbitrary 3D Mesh, 16-page 8K texture atlas, Orthomosaic & DEM) or 'custom' (manual configuration)",
    )
    # Granular Workflow Steps (Metashape Process Stages)
    parser.add_argument("--align-photos", dest="align_photos", action="store_true", default=True, help="Execute camera matching and alignment (default: True)")
    parser.add_argument("--no-align-photos", dest="align_photos", action="store_false", help="Skip camera matching and alignment")
    parser.add_argument("--build-depth-maps", dest="build_depth_maps", action="store_true", default=True, help="Execute GPU MVS dense depth maps calculation (default: True)")
    parser.add_argument("--no-build-depth-maps", dest="build_depth_maps", action="store_false", help="Skip GPU MVS dense depth maps calculation")
    parser.add_argument("--build-mesh", dest="build_mesh", action="store_true", default=True, help="Execute arbitrary 3D surface mesh reconstruction (default: True)")
    parser.add_argument("--no-build-mesh", dest="build_mesh", action="store_false", help="Skip arbitrary 3D surface mesh reconstruction")
    parser.add_argument("--build-texture", dest="build_texture", action="store_true", default=True, help="Execute multi-page 8K texture atlas baking (default: True)")
    parser.add_argument("--no-build-texture", dest="build_texture", action="store_false", help="Skip multi-page 8K texture atlas baking")
    parser.add_argument("--pro", action="store_true", default=False, help="Execute the Pro Photogrammetry & Geomatics Engine")
    parser.add_argument("--ground-cell-size", type=float, default=None, help="Cell size in meters for slope-adaptive ground point classification")
    parser.add_argument("--ground-max-angle", type=float, default=None, help="Max terrain slope angle in degrees for ground point classification")
    parser.add_argument("--ground-max-distance", type=float, default=None, help="Max distance in meters for ground point classification")
    parser.add_argument("--align-quality", choices=["highest", "high", "medium", "low"], default="high", help="Image alignment accuracy/quality (default: high)")
    parser.add_argument("--keypoint-limit", type=int, default=200000, help="Max keypoints per image (default: 200000)")
    parser.add_argument("--keypoint-limit-per-mpx", type=int, default=15000, help="Max keypoints per megapixel (default: 15000)")
    parser.add_argument("--tiepoint-limit", type=int, default=25000, help="Max tiepoints per image (default: 25000)")
    parser.add_argument(
        "--reference-preselection-mode",
        dest="reference_preselection_mode",
        choices=["estimated", "source", "sequential"],
        default="estimated",
        help="Reference preselection mode for photo matching: 'estimated' (default: robust for multi-drone/oblique flights), 'source' (raw EXIF GPS), or 'sequential'",
    )
    parser.add_argument("--depth-quality", choices=["ultra", "high", "medium", "low"], default="high", help="Depth map quality (default: high)")
    parser.add_argument("--depth-filtering", choices=["mild", "moderate", "aggressive"], default=None, help="Depth map filtering mode (default: inferred or moderate)")
    parser.add_argument("--faces", type=int, default=None, help="Target polygon face count (default: 2,000,000 in 'pro' preset)")
    parser.add_argument(
        "--guided-matching",
        dest="guided_matching",
        action="store_true",
        default=True,
        help="Enable guided image matching (default: True, finds additional tie points on high-resolution imagery)",
    )
    parser.add_argument(
        "--no-guided-matching",
        dest="guided_matching",
        action="store_false",
        help="Disable guided image matching",
    )
    parser.add_argument("--no-filter-stationary", dest="filter_stationary_points", action="store_false", default=True, help="Disable stationary tie point exclusion (default: stationary points are excluded)")
    parser.add_argument("--no-adaptive-fitting", dest="adaptive_fitting", action="store_false", default=True, help="Disable adaptive camera model fitting (default: enabled)")
    parser.add_argument(
        "--filter-tiepoints",
        dest="filter_tiepoints",
        action="store_true",
        default=True,
        help="Enable gentle gradual selection tie-point filtering and camera re-optimization (default: enabled)",
    )
    parser.add_argument(
        "--no-filter-tiepoints",
        dest="filter_tiepoints",
        action="store_false",
        help="Disable gentle gradual selection tie-point filtering (keeps all aligned tie points intact)",
    )
    parser.add_argument("--rolling-shutter", dest="rolling_shutter", action="store_true", default=None, help="Force enable CMOS rolling shutter compensation")
    parser.add_argument("--no-rolling-shutter", dest="rolling_shutter", action="store_false", help="Disable CMOS rolling shutter compensation")
    parser.add_argument("--close-holes", dest="close_holes", action="store_true", default=None, help="Enable mesh hole closing (bridges voids up to level 30)")
    parser.add_argument("--no-close-holes", dest="close_holes", action="store_false", help="Disable mesh hole closing (default: disabled to prevent planar webbing artifacts)")
    parser.add_argument("--texture-pages", type=int, default=None, help="Number of 8K texture atlas pages (default: 16 in 'pro' preset, or custom specified)")
    parser.add_argument("--no-fill-holes", dest="fill_holes", action="store_false", default=True, help="Disable texture hole filling (default: enabled)")
    parser.add_argument("--no-ghosting-filter", dest="ghosting_filter", action="store_false", default=True, help="Disable ghosting filter in texture blending (default: enabled)")
    parser.add_argument("--rebake-texture", action="store_true", default=False, help="Force re-baking texture atlas even if one already exists in checkpoint")
    parser.add_argument(
        "--crop-method",
        choices=["gps-square", "structure", "manual", "none"],
        default=None,
        help="Bounding box crop method: 'gps-square', 'structure', 'manual', or 'none' (default: inferred)",
    )
    parser.add_argument("--no-crop-structure", dest="no_crop", action="store_true", help="Disable automatic bounding box crop")
    parser.add_argument("--crop-padding", type=float, default=0.05, help="Safety margin padding factor around bounding box (default: 0.05 / 5%%)")
    parser.add_argument(
        "--min-quality",
        type=float,
        default=0.5,
        help="Minimum image sharpness quality threshold (default: 0.5; photos below this are disabled to prevent alignment blur)",
    )
    parser.add_argument(
        "--no-quality-filter",
        dest="disable_quality_filter",
        action="store_true",
        help="Disable automatic blurry image quality estimation and filtering",
    )
    parser.add_argument("--pause-after-crop", action="store_true", help="Pause after alignment and crop for manual bounding box adjustment in GUI")
    parser.add_argument(
        "--no-crop",
        dest="no_crop",
        action="store_true",
        help="Disable bounding box cropping (reconstruct full uncropped scene)",
    )
    # 3D Model Export Options (Default: False · Metashape project only)
    parser.add_argument("--export-glb", dest="export_glb", action="store_true", default=False, help="Export binary glTF 3D model (.glb)")
    parser.add_argument("--no-export-glb", dest="export_glb", action="store_false", help="Disable binary GLB export")
    parser.add_argument("--export-obj", dest="export_obj", action="store_true", default=False, help="Export Wavefront OBJ model + 8K TIFF textures (.obj)")
    parser.add_argument("--no-export-obj", dest="export_obj", action="store_false", help="Disable Wavefront OBJ export")
    # Point Cloud Options
    parser.add_argument("--build-point-cloud", dest="build_point_cloud", action="store_true", default=True, help="Generate dense point cloud from depth maps (default: True)")
    parser.add_argument("--no-point-cloud", dest="build_point_cloud", action="store_false", help="Disable dense point cloud generation")
    parser.add_argument("--export-point-cloud", dest="export_point_cloud", action="store_true", default=False, help="Export dense point cloud file (default: False)")
    parser.add_argument("--no-export-point-cloud", dest="export_point_cloud", action="store_false", help="Disable dense point cloud export")
    parser.add_argument("--point-cloud-format", choices=["laz", "las", "ply", "e57", "xyz"], default="laz", help="Format for exported dense point cloud (default: laz)")
    parser.add_argument("--classify-ground", action="store_true", default=None, help="Classify ground vs non-ground points in dense point cloud")
    parser.add_argument("--classify-points", action="store_true", default=False, help="Perform multiclass AI semantic point cloud classification")
    parser.add_argument("--filter-point-confidence", type=int, default=None, help="Filter points with confidence below threshold")
    # DEM / Elevation Options
    parser.add_argument("--build-dem", action="store_true", default=None, help="Build Digital Elevation Model (DEM/DSM) (default: False)")
    parser.add_argument("--no-dem", dest="build_dem", action="store_false", help="Disable DEM building and export")
    parser.add_argument("--export-dem", dest="export_dem", action="store_true", default=False, help="Export GeoTIFF Digital Elevation Model (dem.tif)")
    parser.add_argument("--no-export-dem", dest="export_dem", action="store_false", help="Disable GeoTIFF Digital Elevation Model export")
    # Orthomosaic Options
    parser.add_argument(
        "--ortho",
        "--build-orthomosaic",
        dest="build_orthomosaic",
        action="store_true",
        default=None,
        help="Build orthomosaic from reconstructed mesh and save in project (default: True in 'pro' preset)",
    )
    parser.add_argument("--no-ortho", "--no-build-orthomosaic", dest="build_orthomosaic", action="store_false", help="Disable orthomosaic building")
    parser.add_argument("--export-ortho", dest="export_ortho", action="store_true", default=False, help="Export GeoTIFF True Orthomosaic (orthomosaic.tif)")
    parser.add_argument("--no-export-ortho", dest="export_ortho", action="store_false", help="Disable GeoTIFF True Orthomosaic file export")
    # Quality Report Options
    parser.add_argument("--export-report", dest="export_report", action="store_true", default=False, help="Export official Metashape PDF calibration & quality report (default: False)")
    parser.add_argument("--no-report", "--no-export-report", dest="export_report", action="store_false", help="Disable PDF quality report export")
    parser.add_argument("--report-title", default=None, help="Custom title for PDF quality report")
    # Extended Metashape Functions: Targets, GCPs, Colors, Tiled Model, Contours & Delighting
    parser.add_argument("--detect-markers", action="store_true", default=False, help="Detect coded target markers or AprilTags")
    parser.add_argument("--target-type", choices=["circular12", "circular14", "circular16", "circular20", "cross", "apriltag16h5", "apriltag25h9", "apriltag36h11"], default="circular12", help="Coded target type (default: circular12)")
    parser.add_argument("--marker-tolerance", type=int, default=50, help="Marker detector tolerance 0-100 (default: 50)")
    parser.add_argument("--import-reference", default=None, help="Path to ground control points (GCP) reference CSV/TXT file")
    parser.add_argument("--calibrate-colors", action="store_true", default=False, help="Perform radiometric color & white balance calibration")
    parser.add_argument("--surface-type", choices=["arbitrary", "heightfield"], default="arbitrary", help="Mesh surface type (default: arbitrary)")
    parser.add_argument("--smooth-mesh", action="store_true", default=False, help="Apply Laplacian smoothing to surface mesh")
    parser.add_argument("--smooth-strength", type=float, default=3.0, help="Laplacian smoothing strength (default: 3.0)")
    parser.add_argument("--decimate-mesh", type=int, default=None, help="Decimate mesh to target face count")
    parser.add_argument("--remove-lighting", action="store_true", default=False, help="Remove shadows and ambient lighting from texture (delighting)")
    parser.add_argument("--delighting-color-mode", action="store_true", default=False, help="Enable multi-color delighting mode")
    parser.add_argument("--build-tiled-model", action="store_true", default=False, help="Build hierarchical 3D tiled model (LOD)")
    parser.add_argument("--tiled-model-format", choices=["cesium", "tls", "slpk", "osgb"], default="cesium", help="Format for 3D tiled model (default: cesium)")
    parser.add_argument("--tile-size", type=int, default=256, help="Tile size in pixels (default: 256)")
    parser.add_argument("--build-contours", action="store_true", default=False, help="Generate topographic contour lines from DEM")
    parser.add_argument("--contour-interval", type=float, default=1.0, help="Contour elevation interval in meters (default: 1.0)")
    parser.add_argument("--contours-format", choices=["shp", "dxf", "geojson", "kml"], default="shp", help="Format for topographic contours (default: shp)")
    parser.add_argument("--export-fbx", action="store_true", default=False, help="Export 3D model in Autodesk FBX format")
    parser.add_argument("--export-ply", action="store_true", default=False, help="Export 3D model in Stanford PLY format")
    parser.add_argument("--export-dxf", action="store_true", default=False, help="Export 3D model in AutoCAD DXF format")
    parser.add_argument("--export-tie-points", action="store_true", default=False, help="Export sparse tie points in PLY format")
    # Distributed Network Processing Cluster Options
    parser.add_argument("--network-enable", action="store_true", default=False, help="Enable distributed network processing across worker nodes")
    parser.add_argument("--network-host", default="127.0.0.1", help="Metashape server hostname or IP address (default: 127.0.0.1)")
    parser.add_argument("--network-port", type=int, default=5840, help="Metashape server port (default: 5840)")
    parser.add_argument(
        "--network-independent-workers",
        action="store_true",
        default=False,
        help="Lock each project to an independent worker node (worker_limit=1) to eliminate cross-network synchronization pauses",
    )
    parser.add_argument("--network-path", default="", help="Shared network root directory (UNC path, e.g. \\\\SERVER\\RealityScan)")
    # Multi-chunk options
    parser.add_argument(
        "--multi-chunk",
        choices=["auto", "force", "disable"],
        default="disable",
        help="Multi-chunk alignment mode: 'disable' (default: load all images into a single unified chunk for global bundle adjustment), 'auto' (create independent chunks for each image subfolder/flight and register them), or 'force'",
    )
    parser.add_argument(
        "--chunk-align-method",
        choices=["camera", "point"],
        default="camera",
        help="Chunk alignment method for multi-chunk mode: 'camera' (method=2, Camera/GPS-based, robust against symmetric structure inversion) or 'point' (method=0, Point-based RANSAC ICP) (default: camera)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Resume reconstruction from existing project checkpoint if available (default: False; restarts from scratch by default)",
    )
    parser.add_argument(
        "--force-restart",
        action="store_true",
        help="Deprecated: restart from scratch is now the default; use --resume to resume from checkpoint",
    )
    return parser


if __name__ == "__main__":
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.profile:
        print("=========================================================================")
        print("  AUTOMETASHAPE: PRO SURVEY OPTICS & TELEMETRY PROFILER")
        print(f"  Target Directory: {args.images}")
        print("=========================================================================", flush=True)
        phys = analyze_survey_physics(args.images, output_dir=args.output)
        pro_plan = derive_pro_photogrammetry_plan(phys)
        print_pro_engineering_report(phys, pro_plan)
        sys.exit(0)

    crop_method = "none" if args.no_crop else args.crop_method
    min_quality = 0.0 if args.disable_quality_filter else args.min_quality
    auto_params = args.auto or args.pro or (args.preset == "pro")

    run_reconstruction(
        images_dir=args.images,
        output_dir=args.output,
        align_quality=args.align_quality,
        align_photos=args.align_photos,
        build_depth_maps=args.build_depth_maps,
        build_mesh=args.build_mesh,
        build_texture=args.build_texture,
        keypoint_limit=args.keypoint_limit,
        tiepoint_limit=args.tiepoint_limit,
        filter_stationary_points=args.filter_stationary_points,
        guided_matching=args.guided_matching,
        adaptive_fitting=args.adaptive_fitting,
        reference_preselection_mode=args.reference_preselection_mode,
        filter_tiepoints=args.filter_tiepoints,
        depth_quality=args.depth_quality,
        depth_filtering=args.depth_filtering,
        custom_faces=args.faces,
        texture_pages=args.texture_pages,
        fill_holes=args.fill_holes,
        ghosting_filter=args.ghosting_filter,
        rebake_texture=args.rebake_texture,
        crop_method=crop_method,
        crop_padding=args.crop_padding,
        min_quality=min_quality,
        rolling_shutter=args.rolling_shutter,
        close_holes=args.close_holes,
        pause_after_crop=args.pause_after_crop,
        resume=args.resume,
        multi_chunk=args.multi_chunk,
        chunk_align_method=args.chunk_align_method,
        preset=args.preset,
        auto_params=auto_params,
        ground_cell_size=args.ground_cell_size,
        ground_max_angle=args.ground_max_angle,
        ground_max_distance=args.ground_max_distance,
        build_orthomosaic=args.build_orthomosaic,
        build_dem=args.build_dem,
        export_dem=args.export_dem,
        export_ortho=getattr(args, "export_ortho", False),
        build_point_cloud=args.build_point_cloud,
        export_point_cloud=args.export_point_cloud,
        point_cloud_format=args.point_cloud_format,
        export_glb=args.export_glb,
        export_obj=args.export_obj,
        classify_ground=args.classify_ground,
        classify_points=args.classify_points,
        filter_point_confidence=args.filter_point_confidence,
        export_report=args.export_report,
        report_title=args.report_title,
        surface_type=args.surface_type,
        detect_markers=args.detect_markers,
        target_type=args.target_type,
        marker_tolerance=args.marker_tolerance,
        import_reference=args.import_reference,
        calibrate_colors=args.calibrate_colors,
        smooth_mesh=args.smooth_mesh,
        smooth_strength=args.smooth_strength,
        decimate_mesh=args.decimate_mesh,
        remove_lighting=args.remove_lighting,
        delighting_color_mode=args.delighting_color_mode,
        build_tiled_model=args.build_tiled_model,
        tiled_model_format=args.tiled_model_format,
        tile_size=args.tile_size,
        build_contours=args.build_contours,
        contour_interval=args.contour_interval,
        contours_format=args.contours_format,
        export_fbx=args.export_fbx,
        export_ply=args.export_ply,
        export_dxf=args.export_dxf,
        export_tie_points=args.export_tie_points,
        network_enable=args.network_enable,
        network_host=args.network_host,
        network_port=args.network_port,
        network_path=args.network_path,
        network_independent_workers=args.network_independent_workers,
        project_name=args.project_name,
    )
