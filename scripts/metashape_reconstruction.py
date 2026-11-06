# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape: Autonomous High-Precision Photogrammetry Pipeline using Agisoft Metashape Pro.

Reconstructs production-ready 3D architectural models (.obj, .mtl, .glb, .psx)
directly from raw aerial drone or ground camera image directories on NVIDIA GPUs.
Configured with production-grade defaults:
- Image Alignment: Medium quality (downscale=2), 40,000 keypoint limit, 25,000 tiepoint limit,
                   stationary tie points excluded, guided matching enabled, adaptive fitting enabled.
- Camera Optimization: Rolling shutter compensation enabled, Gradual Selection tie-point filtering.
- Bounding Box: gps-square auto-crop (smallest enclosing square of camera GPS poses with uncropped vertical bounds).
- Depth Maps & Meshing: Medium quality depth maps (downscale=4), Moderate depth filtering, 600,000 (600K) custom faces.
- Texture Atlas: 16-page 8K photographic mosaic blending texture atlas (hole filling and ghosting filter enabled).
"""

import argparse
import math
import os
import re
import sys
import threading
import time
import Metashape


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


def parse_xmp_relative_altitude(img_path: str):
    """Fast probe extracting DJI RelativeAltitude from XMP header packet."""
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
                        return float(m.group(1))
    except Exception:
        pass
    return None


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
        meta["relative_alt"] = parse_xmp_relative_altitude(img_path)
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
        shutter = "rolling" if any(k in combined_name for k in ["mavic", "mini", "air", "smartphone", "iphone"]) else "mechanical"
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
    """Mathematically derives the optimal photogrammetric pipeline configuration from survey physics."""
    photo_count = physics.get("photo_count", 0)
    has_gps = physics.get("has_gps", False)
    span_m = physics.get("horizontal_span_m", 0.0)
    alt_span = physics.get("alt_span_m", 0.0)
    relief_ratio = physics.get("relief_ratio", 0.0)
    shutter = physics.get("shutter", "mechanical")
    avg_mp = physics.get("avg_megapixels", 12.0)
    delight_rec = physics.get("delighting_recommended", False)
    gsd_cm = physics.get("gsd_cm")

    is_planar = has_gps and (relief_ratio < 0.08) and (alt_span <= 65.0) and (span_m >= 80.0)
    is_architecture = has_gps and not is_planar

    if is_planar:
        survey_category = "nadir_terrain_mapping"
        surface_type = "heightfield"
        crop_method = "gps-square"
        custom_faces = min(max(photo_count * 5000, 1000000), 5000000)
        texture_pages = 4
        build_orthomosaic = True
        build_dem = True
        build_point_cloud = True
        classify_ground = True
        close_holes = False
        depth_filtering = "aggressive"
        ground_cell_size = 35.0
        ground_max_angle = 12.0
        ground_max_distance = 0.6
    elif is_architecture:
        survey_category = "oblique_architecture"
        surface_type = "arbitrary"
        crop_method = "gps-square"
        custom_faces = min(max(photo_count * 6000, 1000000), 5000000)
        texture_pages = 16
        build_orthomosaic = False
        build_dem = False
        build_point_cloud = True
        classify_ground = False
        close_holes = True
        depth_filtering = "moderate"
        ground_cell_size = 12.0
        ground_max_angle = 28.0
        ground_max_distance = 0.3
    else:
        survey_category = "terrestrial_close_range"
        surface_type = "arbitrary"
        crop_method = "gps-square"
        custom_faces = min(max(photo_count * 4000, 1000000), 5000000)
        texture_pages = 16
        build_orthomosaic = False
        build_dem = False
        build_point_cloud = True
        classify_ground = False
        close_holes = True
        depth_filtering = "moderate"
        ground_cell_size = 10.0
        ground_max_angle = 30.0
        ground_max_distance = 0.25

    # Shutter compensation is ONLY enabled for CMOS rolling shutter payloads.
    # Enabling rolling shutter compensation on mechanical leaf shutters causes synthetic bundle adjustment drift!
    rolling_shutter = (shutter == "rolling")

    align_quality = "medium"
    keypoint_limit = 60000 if avg_mp >= 30.0 else 40000
    tiepoint_limit = 25000
    guided_matching = True
    adaptive_fitting = True
    filter_stationary_points = (survey_category != "terrestrial_close_range")

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
        "depth_quality": "medium",
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
        "remove_lighting": delight_rec,
        "delighting_color_mode": delight_rec,
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
        print("  USGS 4-Pass Bundle:  Pass 1 (RU<=12) -> Pass 2 (PA<=2.5) -> Pass 3 (RE<=0.35) -> Pass 4 (Multiplicity>=3)")
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
    """Crops chunk.region horizontally to the smallest enclosing square of camera GPS poses,
    preserving vertical (Z) bounds completely uncropped to protect roof and ground features.

    This is the default cropping method, minimizing manual alignment steps to virtually zero.

    Args:
        chunk: Active Metashape.Chunk.
        padding: Safety margin padding applied to square dimensions and height (default: 0.05 / 5%).

    Returns:
        True if bounding box was successfully updated; False if skipped.
    """
    aligned_cams = [c for c in chunk.cameras if c.transform and c.enabled]
    if len(aligned_cams) < 2:
        print("  [Auto-Crop-GPS] Fewer than 2 aligned cameras; skipping bounding box crop.", flush=True)
        return False

    print(f"\n[Auto-Crop-GPS] Computing camera horizontal envelope from {len(aligned_cams)} cameras...", flush=True)

    rot = chunk.region.rot
    rot_t = rot.t()  # World to local level coordinate frame

    # 1. Transform camera poses to local horizontal frame
    cam_local_x, cam_local_y = [], []
    for c in aligned_cams:
        loc = rot_t * c.center
        cam_local_x.append(loc.x)
        cam_local_y.append(loc.y)

    cam_local_x.sort()
    cam_local_y.sort()

    # Apply robust MAD bounding to reject distant takeoff/landing outlier cameras (e.g. 10% outlier poses)
    min_cx, max_cx = robust_bounds(cam_local_x, k=3.0)
    min_cy, max_cy = robust_bounds(cam_local_y, k=3.0)

    span_x = max_cx - min_cx
    span_y = max_cy - min_cy
    mid_x = (min_cx + max_cx) * 0.5
    mid_y = (min_cy + max_cy) * 0.5

    # Smallest square enclosing camera footprint (with padding)
    side = max(span_x, span_y) * (1.0 + 2.0 * padding)
    half_side = side * 0.5

    sq_min_x = mid_x - half_side
    sq_max_x = mid_x + half_side
    sq_min_y = mid_y - half_side
    sq_max_y = mid_y + half_side

    # Preserve uncropped vertical (Z) extent from default post-alignment region
    orig_region = chunk.region
    orig_local_center = rot_t * orig_region.center
    orig_mid_z = orig_local_center.z
    orig_span_z = orig_region.size.z

    # 2. Assign new region: horizontal square crop (X, Y) with uncropped vertical (Z)
    mid_local = Metashape.Vector([mid_x, mid_y, orig_mid_z])
    new_center = rot * mid_local
    new_size = Metashape.Vector([side, side, orig_span_z])

    region = chunk.region
    region.center = new_center
    region.size = new_size
    chunk.region = region

    print(f"  [Auto-Crop-GPS] Successfully cropped horizontal region to Camera GPS Square!", flush=True)
    print(f"  Square Center: ({new_center.x:.2f}, {new_center.y:.2f}, {new_center.z:.2f})", flush=True)
    print(f"  Horizontal Footprint: ({side:.2f}m x {side:.2f}m) [Padding: {int(padding * 100)}%]", flush=True)
    print(f"  Vertical Extent (Uncropped): {orig_span_z:.2f}m (Z Center: {orig_mid_z:.2f}m)", flush=True)
    return True


def execute_usgs_gradual_selection(
    chunk,
    ru_thresh: float = 12.0,
    pa_thresh: float = 2.5,
    re_thresh: float = 0.35,
    min_rays: int = 3,
    max_removal_ratio: float = 0.50,
) -> dict:
    """Executes the USGS National Geospatial Program 4-Pass Gradual Selection protocol.

    Refines camera interior and exterior orientations through staged tie-point pruning
    and progressive camera parameter unfreezing:
    - Pass 1 (Reconstruction Uncertainty <= 12.0): Prunes points with poor ray intersection angles.
      Optimizes f, cx, cy only (protects distortion polynomials from outlier bias).
    - Pass 2 (Projection Accuracy <= 2.5 px): Prunes ambiguous tie points with poor pixel correlation.
      Optimizes f, cx, cy, k1..k3, p1, p2 (fits radial and tangential distortion).
    - Pass 3 (Reprojection Error <= 0.35 px): Prunes high-residual points.
      Optimizes full polynomial model (f, cx, cy, k1..k4, b1, b2, p1, p2) with adaptive fitting.
    - Pass 4 (Image Multiplicity >= 3): Discards fragile 2-ray intersections.
      Locks in bundle adjustment geometry and reports final RMS reprojection error.
    """
    if not chunk.tie_points or not chunk.tie_points.points:
        print("  [Gradual Selection] No tie points present to filter; skipping.", flush=True)
        return {}

    initial_pts = len(chunk.tie_points.points)
    f = Metashape.TiePoints.Filter()

    # Calculate initial RMS Reprojection Error
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ReprojectionError)
        initial_re = list(f.values)
        initial_rms = math.sqrt(sum(v * v for v in initial_re) / len(initial_re)) if initial_re else 0.0
    except Exception:
        initial_rms = 0.0

    print(f"\n[USGS Gradual Selection] Starting 4-pass bundle adjustment refinement across {initial_pts:,} tie points (Initial Reprojection RMS: {initial_rms:.3f} px)...", flush=True)

    # Pass 1: Reconstruction Uncertainty (RU <= 12.0)
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

    # Pass 2: Projection Accuracy (PA <= 2.5 px)
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

    # Pass 3: Reprojection Error (RE <= 0.35 px)
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

    # Pass 4: Image Multiplicity (min_rays >= 3)
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ImageCount)
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
        print(f"  [Pass 4/4] Multi-ray Multiplicity (Rays >= {min_rays}): Eliminated fragile 2-ray ties; locked in bundle adjustment.", flush=True)
    except Exception as e:
        print(f"  [Pass 4/4] Multiplicity filter skipped: {e}", flush=True)

    # Final RMS Reprojection Error calculation
    final_pts = len(chunk.tie_points.points) if chunk.tie_points else 0
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ReprojectionError)
        final_re = list(f.values)
        final_rms = math.sqrt(sum(v * v for v in final_re) / len(final_re)) if final_re else 0.0
    except Exception:
        final_rms = 0.0

    rms_improvement = ((initial_rms - final_rms) / initial_rms * 100.0) if initial_rms > 0 else 0.0
    print(f"  [USGS Optimization Summary] Reprojection RMS: {initial_rms:.3f} px -> {final_rms:.3f} px ({rms_improvement:.1f}% reduction). Inliers: {final_pts:,}/{initial_pts:,} tie points retained.\n", flush=True)

    return {
        "initial_points": initial_pts,
        "final_points": final_pts,
        "initial_rms": initial_rms,
        "final_rms": final_rms,
        "rms_improvement_pct": rms_improvement,
    }


def filter_tie_points(chunk, max_reproj_error: float = 0.35, max_uncertainty: float = 12.0) -> dict:
    """Filters low-confidence tie points using the USGS 4-Pass Gradual Selection protocol."""
    return execute_usgs_gradual_selection(chunk, ru_thresh=max_uncertainty, re_thresh=max_reproj_error)


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
                    metashape_bin = r"C:\Program Files\Agisoft\Metashape Pro\metashape.exe"
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
    align_quality: str = "medium",
    keypoint_limit: int = 40000,
    tiepoint_limit: int = 25000,
    filter_stationary_points: bool = True,
    guided_matching: bool = True,
    adaptive_fitting: bool = True,
    filter_tiepoints: bool = True,
    depth_quality: str = "medium",
    depth_filtering: str = "moderate",
    custom_faces: int = None,
    texture_pages: int = None,
    fill_holes: bool = True,
    ghosting_filter: bool = True,
    rebake_texture: bool = False,
    crop_method: str = None,
    crop_padding: float = 0.05,
    min_quality: float = 0.5,
    rolling_shutter: bool = True,
    close_holes: bool = True,
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
    build_dem: bool = None,
    export_dem: bool = False,
    build_point_cloud: bool = True,
    export_point_cloud: bool = True,
    point_cloud_format: str = "laz",
    classify_ground: bool = None,
    classify_points: bool = False,
    filter_point_confidence: int = None,
    export_report: bool = True,
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
    project_name: str | None = None,
):
    t_start = time.time()
    if not output_dir:
        output_dir = os.path.join(images_dir, "model")
    os.makedirs(output_dir, exist_ok=True)
    if not project_name:
        project_name = infer_project_name(images_dir=images_dir, output_dir=output_dir)
    psx_out = os.path.join(output_dir, f"{project_name}.psx")
    legacy_psx = os.path.join(output_dir, "building_reconstruction.psx")

    # -----------------------------------------------------------------------
    # Pro Photogrammetry Survey Physics & Telemetry Resolution
    # -----------------------------------------------------------------------
    inferred_profile = None
    survey_physics = None
    pro_plan = None
    usgs_stats = None
    if preset in ("building", "map", "custom"):
        is_auto = False
    else:
        is_auto = (preset in ("pro", "auto")) or auto_params or (preset is None and crop_method is None and custom_faces is None)
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
            print(f"  [Survey Physics] Note: Telemetry analysis ({e_prof}); using standard architectural defaults.", flush=True)

    elif preset == "map":
        if crop_method is None:
            crop_method = "gps-square"
        if custom_faces is None:
            custom_faces = 3000000
        if texture_pages is None:
            texture_pages = 4
        if build_orthomosaic is None:
            build_orthomosaic = True
        if build_dem is None:
            build_dem = True
        if build_point_cloud is None:
            build_point_cloud = True
        if classify_ground is None:
            classify_ground = True
        if close_holes is None:
            close_holes = False
    elif preset == "building":
        if crop_method is None:
            crop_method = "gps-square"
        if custom_faces is None:
            custom_faces = 2000000
        if texture_pages is None:
            texture_pages = 16
        if build_orthomosaic is None:
            build_orthomosaic = False
        if build_dem is None:
            build_dem = False
        if build_point_cloud is None:
            build_point_cloud = True
        if classify_ground is None:
            classify_ground = False
        if close_holes is None:
            close_holes = True
    elif preset == "custom":
        # Custom manual configuration: user-supplied parameters are strictly preserved
        pass

    # Final safety fallbacks
    if crop_method is None:
        crop_method = "gps-square"
    if custom_faces is None:
        custom_faces = 2000000
    if texture_pages is None:
        texture_pages = 16
    if build_orthomosaic is None:
        build_orthomosaic = False
    if build_dem is None:
        build_dem = False
    if build_point_cloud is None:
        build_point_cloud = True
    if classify_ground is None:
        classify_ground = False
    if close_holes is None:
        close_holes = True

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

    doc = Metashape.Document()
    resumed = False
    target_psx = psx_out if os.path.exists(psx_out) else (legacy_psx if os.path.exists(legacy_psx) else None)
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
                print(f"\n[2/7] GPU Feature Matching for Chunk {idx+1}/{len(created_chunks)} ('{c.label}', {len(c.cameras):,} cameras)...", flush=True)
                t_match = time.time()
                c.matchPhotos(
                    downscale=align_downscale,
                    generic_preselection=True,
                    reference_preselection=True,
                    keypoint_limit=keypoint_limit,
                    tiepoint_limit=tiepoint_limit,
                    mask_tiepoints=True,
                    filter_stationary_points=filter_stationary_points,
                    guided_matching=guided_matching,
                )
                print(f"  Matching finished for '{c.label}' in {(time.time() - t_match)/60:.2f} min.", flush=True)

            # Step 3: Camera Alignment per chunk
            for idx, c in enumerate(created_chunks):
                print(f"\n[3/7] Aligning Cameras for Chunk {idx+1}/{len(created_chunks)} ('{c.label}', Adaptive Fitting: {adaptive_fitting})...", flush=True)
                t_align = time.time()
                c.alignCameras(adaptive_fitting=adaptive_fitting)
                aligned_count = sum(1 for cam in c.cameras if cam.transform and cam.enabled)
                enabled_count = sum(1 for cam in c.cameras if cam.enabled)
                print(f"  Chunk '{c.label}' alignment finished in {(time.time() - t_align)/60:.2f} min. Aligned: {aligned_count}/{enabled_count} active cameras.", flush=True)

                if filter_tiepoints:
                    usgs_stats = filter_tie_points(c, max_reproj_error=0.35, max_uncertainty=12.0)

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

        else:
            all_imgs = []
            for imgs in groups.values():
                all_imgs.extend(imgs)
            all_imgs = sorted(all_imgs)

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

            print(f"\n[2/7] Executing GPU Feature Matching (Downscale: {align_downscale}, Keypoints: {keypoint_limit:,}, Tiepoints: {tiepoint_limit:,})...", flush=True)
            t_match = time.time()
            chunk.matchPhotos(
                downscale=align_downscale,
                generic_preselection=True,
                reference_preselection=True,
                keypoint_limit=keypoint_limit,
                tiepoint_limit=tiepoint_limit,
                mask_tiepoints=True,
                filter_stationary_points=filter_stationary_points,
                guided_matching=guided_matching,
            )
            print(f"  Matching finished in {(time.time() - t_match)/60:.2f} min.", flush=True)

            print(f"\n[3/7] Aligning Cameras & Initial Bundle Adjustment (Adaptive Fitting: {adaptive_fitting})...", flush=True)
            t_align = time.time()
            chunk.alignCameras(adaptive_fitting=adaptive_fitting)
            aligned_count = sum(1 for c in chunk.cameras if c.transform and c.enabled)
            enabled_count = sum(1 for c in chunk.cameras if c.enabled)
            print(f"  Alignment finished in {(time.time() - t_align)/60:.2f} min. Aligned: {aligned_count}/{enabled_count} active cameras.", flush=True)

            if rolling_shutter:
                for s in chunk.sensors:
                    s.rolling_shutter = Metashape.Shutter.Model.Regularized
                print("  [Rolling Shutter] Enabled Regularized CMOS rolling shutter compensation.", flush=True)

            if filter_tiepoints:
                usgs_stats = filter_tie_points(chunk, max_reproj_error=0.35, max_uncertainty=12.0)
            else:
                print("  [Gradual Selection] Tie-point cleaning and camera re-optimization skipped (--no-filter-tiepoints).", flush=True)

        def _safe_reopen():
            files_dir = psx_out.replace(".psx", ".files")
            lock_file = os.path.join(files_dir, "lock")
            if os.path.isfile(lock_file):
                try:
                    os.remove(lock_file)
                except Exception:
                    pass
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

        if crop_method == "manual":
            print("\n[3b/7] Manual Bounding Box Crop selected: Auto-crop skipped.", flush=True)
            chunk = save_checkpoint(doc, psx_out)
            print(f"[CHECKPOINT] Saved alignment checkpoint to: {psx_out}", flush=True)
            wait_for_user_resume(output_dir, psx_out)
            _safe_reopen()
            chunk = doc.chunk
        elif crop_method == "gps-square":
            auto_crop_camera_gps_square(chunk, padding=crop_padding)
            chunk = save_checkpoint(doc, psx_out)
            print(f"[CHECKPOINT] Saved alignment checkpoint to: {psx_out}", flush=True)
            if pause_after_crop:
                wait_for_user_resume(output_dir, psx_out)
                _safe_reopen()
                chunk = doc.chunk
        elif crop_method == "structure":
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
        print(f"\n[4/7] Reusing existing MVS Depth Maps from checkpoint ({depth_count} depth maps).", flush=True)
    else:
        depth_downscale = DEPTH_DOWNSCALE_MAP.get(depth_quality.lower(), 4)
        filter_mode_arg = DEPTH_FILTER_MAP.get(depth_filtering.lower(), Metashape.FilterMode.ModerateFiltering)
        print(f"\n[4/7] Computing GPU MVS Depth Maps (Quality: {depth_quality.capitalize()} [Downscale={depth_downscale}], Filtering: {depth_filtering.capitalize()})...", flush=True)
        t_depth = time.time()
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
            print(f"\n[4b/7] Reusing existing Dense Point Cloud from checkpoint ({chunk.point_cloud.point_count:,} points).", flush=True)
        else:
            print(f"\n[4b/7] Generating Dense Point Cloud from Depth Maps (RGB Colors + Confidence)...", flush=True)
            t_pc = time.time()
            chunk.buildPointCloud(
                source_data=Metashape.DataSource.DepthMapsData,
                point_colors=True,
                point_confidence=True,
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
        print(f"\n[5/7] Reusing existing Surface Mesh from checkpoint ({len(chunk.model.vertices):,} vertices, {len(chunk.model.faces):,} faces).", flush=True)
    else:
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
        try:
            chunk.buildModel(
                surface_type=surf_enum,
                source_data=Metashape.DataSource.DepthMapsData,
                interpolation=Metashape.Interpolation.EnabledInterpolation,
                face_count=face_count_arg,
                face_count_custom=face_count_custom_arg,
                vertex_colors=True,
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
    current_pages = 0
    if chunk.model:
        if hasattr(chunk.model, "textures") and chunk.model.textures:
            current_pages = len(chunk.model.textures)
        elif hasattr(chunk.model, "texture") and chunk.model.texture():
            current_pages = 1

    if current_pages == texture_pages and not rebake_texture:
        print(f"\n[6/7] Reusing existing Texture Atlas from checkpoint ({current_pages} pages).", flush=True)
    else:
        if current_pages > 0 and current_pages != texture_pages:
            print(f"\n[6/7] Existing texture atlas has {current_pages} page(s), re-baking to requested {texture_pages} pages...", flush=True)
        elif rebake_texture:
            print(f"\n[6/7] Force re-baking texture atlas as requested ({texture_pages} pages)...", flush=True)
        else:
            print(f"\n[6/7] Baking {texture_pages}x 8192x8192 (8K) Photogrammetric Texture Atlas (Mosaic Blending, Hole Filling: {fill_holes}, Ghosting Filter: {ghosting_filter})...", flush=True)

        if calibrate_colors:
            print("\n[Color Calibration] Executing radiometric color & white balance calibration...", flush=True)
            try:
                chunk.calibrateColors(source_data=Metashape.DataSource.ModelData, white_balance=True)
                print("  Radiometric color calibration completed.", flush=True)
            except Exception as e_col:
                print(f"  [WARNING] Color calibration issue: {e_col}. Continuing...", flush=True)

        # Dynamically size texture atlas for 1:1 ground texel parity if in pro mode or unforced
        eff_pages = texture_pages
        if (texture_pages is None or is_auto) and chunk.model and survey_physics and survey_physics.get("gsd_cm"):
            optimal_budget = calculate_optimal_texel_budget(chunk, gsd_cm=survey_physics["gsd_cm"])
            if optimal_budget:
                eff_pages = optimal_budget

        t_tex = time.time()
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
                print(f"\n[6b/7] Building Hierarchical 3D Tiled Model (Tile Size: {tile_size})...", flush=True)
                t_tile = time.time()
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

    # Production Asset Export
    print("\n=========================================================================")
    print("  [7/7] EXPORTING PRODUCTION ASSETS (GLB, OBJ, TIFF, LAZ, DEM & PSX)")
    print("=========================================================================")
    glb_out = os.path.join(output_dir, f"{project_name}.glb")
    obj_out = os.path.join(output_dir, f"{project_name}.obj")

    chunk.exportModel(
        glb_out,
        format=Metashape.ModelFormat.ModelFormatGLTF,
        embed_texture=True,
        save_normals=True,
        save_colors=True,
    )
    print(f"  [7/7] [EXPORT] Saved binary GLB: {glb_out} ({os.path.getsize(glb_out)/(1024*1024):.2f} MB)", flush=True)

    chunk.exportModel(
        obj_out,
        format=Metashape.ModelFormat.ModelFormatOBJ,
        texture_format=Metashape.ImageFormat.ImageFormatTIFF,
        save_texture=True,
        save_normals=True,
        save_colors=True,
    )
    print(f"  [7/7] [EXPORT] Saved Wavefront OBJ + TIFF: {obj_out} ({os.path.getsize(obj_out)/(1024*1024):.2f} MB)", flush=True)

    if export_fbx and chunk.model:
        fbx_out = os.path.join(output_dir, f"{project_name}.fbx")
        print(f"  [7/7] [EXPORT] Exporting Autodesk FBX to: {fbx_out}...", flush=True)
        try:
            chunk.exportModel(fbx_out, format=Metashape.ModelFormat.ModelFormatFBX, save_texture=True, save_normals=True, save_colors=True)
            print(f"  [7/7] [EXPORT] Saved FBX: {fbx_out} ({os.path.getsize(fbx_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_fbx:
            print(f"  [7/7] [WARNING] FBX export issue: {e_fbx}", flush=True)

    if export_ply and chunk.model:
        ply_out = os.path.join(output_dir, f"{project_name}_mesh.ply")
        print(f"  [7/7] [EXPORT] Exporting Stanford PLY to: {ply_out}...", flush=True)
        try:
            chunk.exportModel(ply_out, format=Metashape.ModelFormat.ModelFormatPLY, save_normals=True, save_colors=True)
            print(f"  [7/7] [EXPORT] Saved PLY: {ply_out} ({os.path.getsize(ply_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_ply:
            print(f"  [7/7] [WARNING] PLY export issue: {e_ply}", flush=True)

    if export_dxf and chunk.model:
        dxf_out = os.path.join(output_dir, f"{project_name}.dxf")
        print(f"  [7/7] [EXPORT] Exporting AutoCAD DXF to: {dxf_out}...", flush=True)
        try:
            chunk.exportModel(dxf_out, format=Metashape.ModelFormat.ModelFormatDXF, save_normals=True)
            print(f"  [7/7] [EXPORT] Saved DXF: {dxf_out} ({os.path.getsize(dxf_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_dxf:
            print(f"  [7/7] [WARNING] DXF export issue: {e_dxf}", flush=True)

    if export_tie_points and chunk.tie_points:
        tp_out = os.path.join(output_dir, f"{project_name}_tie_points.ply")
        print(f"  [7/7] [EXPORT] Exporting Sparse Tie Points to: {tp_out}...", flush=True)
        try:
            chunk.exportPointCloud(path=tp_out, format=Metashape.PointCloudFormat.PointCloudFormatPLY, source_data=Metashape.DataSource.TiePointsData)
            print(f"  [7/7] [EXPORT] Saved Tie Points: {tp_out} ({os.path.getsize(tp_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_tp:
            print(f"  [7/7] [WARNING] Tie points export issue: {e_tp}", flush=True)

    # Step 7b: Digital Elevation Model (DEM) Generation & GeoTIFF Export
    if build_dem:
        try:
            has_dem = getattr(chunk, "elevation", None) is not None
            if not has_dem:
                print(f"\n[7b/7] Building Digital Elevation Model (DEM/DSM)...", flush=True)
                t_dem = time.time()
                proj = Metashape.OrthoProjection()
                if chunk.crs:
                    proj.crs = chunk.crs
                dem_source = Metashape.DataSource.PointCloudData if (chunk.point_cloud and chunk.point_cloud.point_count > 0) else Metashape.DataSource.DepthMapsData
                chunk.buildDem(
                    source_data=dem_source,
                    interpolation=Metashape.Interpolation.EnabledInterpolation,
                    projection=proj,
                )
                print(f"  [7b/7] DEM generation completed in {(time.time() - t_dem)/60:.2f} min.", flush=True)
                chunk = save_checkpoint(doc, psx_out)
                print(f"  [7b/7] [CHECKPOINT] Saved DEM inside project: {psx_out}", flush=True)
            else:
                print(f"\n[7b/7] Reusing existing Digital Elevation Model from checkpoint.", flush=True)

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
                print(f"\n[7c/7] Generating Orthomosaic from Reconstructed Surface Mesh...", flush=True)
                t_ortho = time.time()
                proj = Metashape.OrthoProjection()
                if chunk.crs:
                    proj.crs = chunk.crs
                chunk.buildOrthomosaic(
                    surface_data=Metashape.DataSource.ModelData,
                    blending_mode=Metashape.BlendingMode.MosaicBlending,
                    fill_holes=fill_holes,
                    ghosting_filter=ghosting_filter,
                    projection=proj,
                )
                print(f"  [7c/7] Orthomosaic generation completed in {(time.time() - t_ortho)/60:.2f} min ({chunk.orthomosaic.width}x{chunk.orthomosaic.height} px).", flush=True)
                chunk = save_checkpoint(doc, psx_out)
                print(f"  [7c/7] [CHECKPOINT] Saved orthomosaic inside project: {psx_out}", flush=True)
            else:
                print(f"\n[7c/7] Reusing existing Orthomosaic from checkpoint ({chunk.orthomosaic.width}x{chunk.orthomosaic.height} px).", flush=True)

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
        except Exception as e_ortho:
            print(f"  [7c/7] [WARNING] Orthomosaic generation/export encountered an issue: {e_ortho}. Continuing...", flush=True)

    # Step 7d: Dense Point Cloud Export
    if export_point_cloud or build_point_cloud:
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

    # Step 7e: Executive PDF Quality & Calibration Report
    if export_report:
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

    # Maintain dual-compatibility aliases for legacy model.glb and building_reconstruction.psx
    if project_name.lower() != "model":
        legacy_glb = os.path.join(output_dir, "model.glb")
        legacy_obj = os.path.join(output_dir, "model.obj")
        if not os.path.exists(legacy_glb) and os.path.exists(glb_out):
            try:
                os.link(glb_out, legacy_glb)
            except Exception:
                pass
        if not os.path.exists(legacy_obj) and os.path.exists(obj_out):
            try:
                os.link(obj_out, legacy_obj)
            except Exception:
                pass
        if legacy_psx != psx_out and not os.path.exists(legacy_psx):
            try:
                with open(legacy_psx, "w", encoding="utf-8") as f_leg:
                    f_leg.write(f'<?xml version="1.0" encoding="UTF-8"?>\n<document version="1.2.0" path="{project_name}.files/project.zip"/>\n')
            except Exception:
                pass

    total_time = (time.time() - t_start) / 60.0
    print(f"\n[SUCCESS] Entire pipeline finished in {total_time:.2f} minutes!", flush=True)

    if survey_physics and pro_plan:
        print_pro_engineering_report(survey_physics, pro_plan, usgs_stats=usgs_stats)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AutoMetashape: Autonomous High-Precision Photogrammetry Pipeline (Survey Profiling, Point Cloud, Mesh & Orthomosaic)"
    )
    parser.add_argument("--images", required=True, help="Input directory containing raw images (scanned recursively across all subfolders)")
    parser.add_argument("--output", default=None, help="Output directory for 3D assets (default: <images_dir>/model)")
    parser.add_argument("--project-name", default=None, help="Base name for the project file, 3D model, textures, point cloud, and reports (default: folder name of the project)")
    parser.add_argument("--profile", action="store_true", help="Profile dataset telemetry, print inferred optimal parameters, and exit without reconstructing")
    parser.add_argument("--auto", action="store_true", help="Automatically profile dataset and infer optimal parameters (default mode)")
    parser.add_argument(
        "--preset",
        choices=["pro", "auto", "building", "map", "custom"],
        default="pro",
        help="Reconstruction preset: 'pro' (default: physics-driven survey optics, USGS 4-pass bundle adjustment, 1:1 texel budget), 'auto' (learned from telemetry), 'building', 'map', or 'custom' (manual configuration)",
    )
    parser.add_argument("--pro", action="store_true", default=False, help="Execute the Pro Photogrammetry & Geomatics Engine")
    parser.add_argument("--ground-cell-size", type=float, default=None, help="Cell size in meters for slope-adaptive ground point classification")
    parser.add_argument("--ground-max-angle", type=float, default=None, help="Max terrain slope angle in degrees for ground point classification")
    parser.add_argument("--ground-max-distance", type=float, default=None, help="Max distance in meters for ground point classification")
    parser.add_argument("--align-quality", choices=["highest", "high", "medium", "low"], default="medium", help="Image alignment accuracy/quality (default: medium)")
    parser.add_argument("--keypoint-limit", type=int, default=40000, help="Max keypoints per image (default: 40000)")
    parser.add_argument("--tiepoint-limit", type=int, default=25000, help="Max tiepoints per image (default: 25000)")
    parser.add_argument("--depth-quality", choices=["ultra", "high", "medium", "low"], default="medium", help="Depth map quality (default: medium)")
    parser.add_argument("--depth-filtering", choices=["mild", "moderate", "aggressive"], default=None, help="Depth map filtering mode (default: inferred or moderate)")
    parser.add_argument("--faces", type=int, default=None, help="Target polygon face count (default: inferred in 1M-5M range or 2,000,000)")
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
        "--no-filter-tiepoints",
        dest="filter_tiepoints",
        action="store_false",
        default=True,
        help="Disable Gradual Selection tie-point filtering and camera re-optimization (keeps all aligned tie points intact)",
    )
    parser.add_argument("--rolling-shutter", dest="rolling_shutter", action="store_true", default=None, help="Force enable CMOS rolling shutter compensation")
    parser.add_argument("--no-rolling-shutter", dest="rolling_shutter", action="store_false", help="Disable CMOS rolling shutter compensation")
    parser.add_argument("--no-close-holes", dest="close_holes", action="store_false", default=True, help="Disable mesh hole closing (default: enabled at level 30)")
    parser.add_argument("--texture-pages", type=int, default=None, help="Number of 8K texture atlas pages (default: inferred or 16 for building, 4 for map)")
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
    # Point Cloud Options
    parser.add_argument("--build-point-cloud", dest="build_point_cloud", action="store_true", default=True, help="Generate dense point cloud from depth maps (default: True)")
    parser.add_argument("--no-point-cloud", dest="build_point_cloud", action="store_false", help="Disable dense point cloud generation")
    parser.add_argument("--export-point-cloud", dest="export_point_cloud", action="store_true", default=True, help="Export dense point cloud file (default: True)")
    parser.add_argument("--no-export-point-cloud", dest="export_point_cloud", action="store_false", help="Disable dense point cloud export")
    parser.add_argument("--point-cloud-format", choices=["laz", "las", "ply", "e57", "xyz"], default="laz", help="Format for exported dense point cloud (default: laz)")
    parser.add_argument("--classify-ground", action="store_true", default=None, help="Classify ground vs non-ground points in dense point cloud")
    parser.add_argument("--classify-points", action="store_true", default=False, help="Perform multiclass AI semantic point cloud classification")
    parser.add_argument("--filter-point-confidence", type=int, default=None, help="Filter points with confidence below threshold")
    # DEM / Elevation Options
    parser.add_argument("--build-dem", action="store_true", default=None, help="Build Digital Elevation Model (DEM/DSM)")
    parser.add_argument("--export-dem", action="store_true", default=False, help="Export GeoTIFF Digital Elevation Model (dem.tif)")
    # Orthomosaic Options
    parser.add_argument(
        "--ortho",
        "--build-orthomosaic",
        dest="build_orthomosaic",
        action="store_true",
        default=None,
        help="Build orthomosaic from reconstructed mesh and export GeoTIFF (default: inferred or enabled in 'map' preset)",
    )
    # Quality Report Options
    parser.add_argument("--export-report", dest="export_report", action="store_true", default=True, help="Export official Metashape PDF calibration & quality report (default: True)")
    parser.add_argument("--no-report", dest="export_report", action="store_false", help="Disable PDF quality report export")
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
    auto_params = args.auto or args.pro or (args.preset in ("pro", "auto"))

    run_reconstruction(
        images_dir=args.images,
        output_dir=args.output,
        align_quality=args.align_quality,
        keypoint_limit=args.keypoint_limit,
        tiepoint_limit=args.tiepoint_limit,
        filter_stationary_points=args.filter_stationary_points,
        guided_matching=args.guided_matching,
        adaptive_fitting=args.adaptive_fitting,
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
        build_point_cloud=args.build_point_cloud,
        export_point_cloud=args.export_point_cloud,
        point_cloud_format=args.point_cloud_format,
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
        project_name=args.project_name,
    )
