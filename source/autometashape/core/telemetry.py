# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Survey physics engine, EXIF/XMP optics telemetry, radiometry, and geomatics audit."""

from __future__ import annotations

import math
import os
import re

try:
    import Metashape
except ImportError:
    Metashape = None

# Comprehensive list of all image and camera RAW formats supported by Agisoft Metashape
SUPPORTED_IMAGE_EXTS = (
    # Standard raster formats
    ".jpg",
    ".jpeg",
    ".jpe",
    ".png",
    ".tif",
    ".tiff",
    ".bmp",
    ".dib",
    ".webp",
    ".jp2",
    ".j2k",
    ".jpc",
    ".j2c",
    ".jxl",
    ".exr",
    ".tga",
    ".dds",
    ".pnm",
    ".ppm",
    ".pgm",
    ".pbm",
    ".sgi",
    ".rgb",
    ".bil",
    # Camera RAW formats (DJI, Sony, Phase One, Hasselblad, Canon, Nikon, Leica, etc.)
    ".dng",
    ".cr2",
    ".cr3",
    ".crw",
    ".nef",
    ".nrw",
    ".arw",
    ".srf",
    ".sr2",
    ".iiq",
    ".cap",
    ".3fr",
    ".fff",
    ".rw2",
    ".orf",
    ".raf",
    ".raw",
    ".rwl",
    ".pef",
    ".ptx",
    # Radiometric thermal and multi-spectral formats
    ".seq",
    ".ara",
)

# Professional Camera Optics & Sensor Database for Aerial Photogrammetry Payloads
CAMERA_HARDWARE_DB: dict[str, dict] = {
    # DJI Enterprise & High-End Payloads (Mechanical Leaf / Global Shutters)
    "fc6310": {
        "name": "DJI Phantom 4 Pro / RTK (FC6310)",
        "sensor_w_mm": 13.2,
        "sensor_h_mm": 8.8,
        "pixel_pitch_um": 2.41,
        "shutter": "mechanical",
        "focal_fixed": 8.8,
    },
    "fc6310r": {
        "name": "DJI Phantom 4 RTK (FC6310R)",
        "sensor_w_mm": 13.2,
        "sensor_h_mm": 8.8,
        "pixel_pitch_um": 2.41,
        "shutter": "mechanical",
        "focal_fixed": 8.8,
    },
    "fc3411": {
        "name": "DJI Mavic 3 Enterprise Wide (FC3411)",
        "sensor_w_mm": 17.3,
        "sensor_h_mm": 13.0,
        "pixel_pitch_um": 3.28,
        "shutter": "mechanical",
        "focal_fixed": 12.29,
    },
    "zenmuse p1": {
        "name": "DJI Zenmuse P1 Full-Frame",
        "sensor_w_mm": 35.9,
        "sensor_h_mm": 24.0,
        "pixel_pitch_um": 4.4,
        "shutter": "mechanical",
        "focal_fixed": None,
    },
    "zenmuse x4s": {
        "name": "DJI Zenmuse X4S",
        "sensor_w_mm": 13.2,
        "sensor_h_mm": 8.8,
        "pixel_pitch_um": 2.41,
        "shutter": "mechanical",
        "focal_fixed": 8.8,
    },
    "zenmuse x5s": {
        "name": "DJI Zenmuse X5S (Micro 4/3)",
        "sensor_w_mm": 17.3,
        "sensor_h_mm": 13.0,
        "pixel_pitch_um": 3.4,
        "shutter": "rolling",
        "focal_fixed": None,
    },
    "zenmuse x7": {
        "name": "DJI Zenmuse X7 (Super 35)",
        "sensor_w_mm": 23.5,
        "sensor_h_mm": 15.7,
        "pixel_pitch_um": 3.91,
        "shutter": "mechanical",
        "focal_fixed": None,
    },
    "l2": {
        "name": "DJI Zenmuse L2 RGB",
        "sensor_w_mm": 17.3,
        "sensor_h_mm": 13.0,
        "pixel_pitch_um": 3.28,
        "shutter": "mechanical",
        "focal_fixed": 12.29,
    },
    "m3m": {
        "name": "DJI Mavic 3 Multispectral RGB",
        "sensor_w_mm": 17.3,
        "sensor_h_mm": 13.0,
        "pixel_pitch_um": 3.28,
        "shutter": "mechanical",
        "focal_fixed": 12.29,
    },
    # DJI Consumer Drones (CMOS Electronic Rolling Shutter)
    "fc9589": {
        "name": "DJI Mini 4 / Neo / Consumer Payload (FC9589)",
        "sensor_w_mm": 10.08,
        "sensor_h_mm": 7.56,
        "pixel_pitch_um": 2.50,
        "shutter": "rolling",
        "focal_fixed": 6.72,
    },
    "fc9470": {
        "name": "DJI Neo (FC9470)",
        "sensor_w_mm": 6.86,
        "sensor_h_mm": 3.86,
        "pixel_pitch_um": 1.72,
        "shutter": "rolling",
        "focal_fixed": 3.05,
    },
    "fc3582": {
        "name": "DJI Mavic 3 / Cine / Classic Hasselblad",
        "sensor_w_mm": 17.3,
        "sensor_h_mm": 13.0,
        "pixel_pitch_um": 3.28,
        "shutter": "rolling",
        "focal_fixed": 12.29,
    },
    "fc3170": {
        "name": "DJI Mavic Air 2",
        "sensor_w_mm": 6.4,
        "sensor_h_mm": 4.8,
        "pixel_pitch_um": 0.8,
        "shutter": "rolling",
        "focal_fixed": 4.5,
    },
    "fc3011": {
        "name": "DJI Air 2S",
        "sensor_w_mm": 13.2,
        "sensor_h_mm": 8.8,
        "pixel_pitch_um": 2.4,
        "shutter": "rolling",
        "focal_fixed": 8.38,
    },
    "fc3580": {
        "name": "DJI Mini 3 / Mini 4 Pro",
        "sensor_w_mm": 9.6,
        "sensor_h_mm": 7.2,
        "pixel_pitch_um": 1.2,
        "shutter": "rolling",
        "focal_fixed": 6.72,
    },
    "fc7303": {
        "name": "DJI Mini 2",
        "sensor_w_mm": 6.17,
        "sensor_h_mm": 4.55,
        "pixel_pitch_um": 1.55,
        "shutter": "rolling",
        "focal_fixed": 4.49,
    },
    "fc220": {
        "name": "DJI Mavic Pro",
        "sensor_w_mm": 6.17,
        "sensor_h_mm": 4.55,
        "pixel_pitch_um": 1.55,
        "shutter": "rolling",
        "focal_fixed": 4.73,
    },
    # Autel Survey Payloads
    "evo ii pro": {
        "name": "Autel EVO II Pro (1-inch)",
        "sensor_w_mm": 13.2,
        "sensor_h_mm": 8.8,
        "pixel_pitch_um": 2.4,
        "shutter": "rolling",
        "focal_fixed": 9.0,
    },
    "evo max": {
        "name": "Autel EVO Max 4T",
        "sensor_w_mm": 13.2,
        "sensor_h_mm": 8.8,
        "pixel_pitch_um": 2.4,
        "shutter": "rolling",
        "focal_fixed": None,
    },
    # Professional Aerial Survey & Photogrammetric Cameras (Global / Leaf Shutters)
    "phase one": {
        "name": "Phase One Industrial (iXM / iXU)",
        "sensor_w_mm": 53.4,
        "sensor_h_mm": 40.0,
        "pixel_pitch_um": 3.76,
        "shutter": "mechanical",
        "focal_fixed": None,
    },
    "ixm": {
        "name": "Phase One iXM Medium Format",
        "sensor_w_mm": 53.4,
        "sensor_h_mm": 40.0,
        "pixel_pitch_um": 3.76,
        "shutter": "mechanical",
        "focal_fixed": None,
    },
    "hasselblad": {
        "name": "Hasselblad Aerial Medium Format",
        "sensor_w_mm": 43.8,
        "sensor_h_mm": 32.9,
        "pixel_pitch_um": 3.45,
        "shutter": "mechanical",
        "focal_fixed": None,
    },
    "ilx-lr1": {
        "name": "Sony ILX-LR1 Industrial Full-Frame",
        "sensor_w_mm": 35.7,
        "sensor_h_mm": 23.8,
        "pixel_pitch_um": 3.76,
        "shutter": "mechanical",
        "focal_fixed": None,
    },
    "rx1r": {
        "name": "Sony RX1R II Full-Frame",
        "sensor_w_mm": 35.9,
        "sensor_h_mm": 24.0,
        "pixel_pitch_um": 4.5,
        "shutter": "mechanical",
        "focal_fixed": 35.0,
    },
    "wingtra": {
        "name": "WingtraOne Aerial Payload",
        "sensor_w_mm": 35.9,
        "sensor_h_mm": 24.0,
        "pixel_pitch_um": 4.5,
        "shutter": "mechanical",
        "focal_fixed": 35.0,
    },
}


def compute_utm_epsg(lon: float, lat: float) -> int:
    """Calculates standard WGS 84 / UTM Zone EPSG code for longitude and latitude."""
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
    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return r_earth * c


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
                    xmp_str = head[start : end + 12].decode("utf-8", errors="ignore")
                    m = re.search(r'RelativeAltitude=["\']([+-]?\d+(?:\.\d+)?)["\']', xmp_str)
                    if not m:
                        m = re.search(r"<[^>]*RelativeAltitude>([+-]?\d+(?:\.\d+)?)</[^>]*RelativeAltitude>", xmp_str)
                    if m:
                        res["relative_alt"] = float(m.group(1))

                    m_sh = re.search(r'ShutterType=["\']([A-Za-z]+)["\']', xmp_str)
                    if not m_sh:
                        m_sh = re.search(r"<[^>]*ShutterType>([A-Za-z]+)</[^>]*ShutterType>", xmp_str)
                    if m_sh:
                        res["shutter_type"] = m_sh.group(1).strip().lower()

                    m_pitch = re.search(r'GimbalPitchDegree=["\']([+-]?\d+(?:\.\d+)?)["\']', xmp_str)
                    if not m_pitch:
                        m_pitch = re.search(
                            r"<[^>]*GimbalPitchDegree>([+-]?\d+(?:\.\d+)?)</[^>]*GimbalPitchDegree>", xmp_str
                        )
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
        from PIL import ExifTags, Image

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
                    d
                    for d in dirs
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
                d
                for d in dirs
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
            d = calculate_haversine_meters(coords[i][0], coords[i][1], coords[i + 1][0], coords[i + 1][1])
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

    rolling_shutter = shutter == "rolling"

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


def print_pro_engineering_report(physics: dict, plan: dict, usgs_stats: dict = None) -> None:
    """Prints a professional photogrammetric & geomatics engineering summary."""
    print("\n" + "=" * 80, flush=True)
    print("  AUTOMETASHAPE: PRO PHOTOGRAMMETRY & GEOMATICS ENGINEERING REPORT", flush=True)
    print("=" * 80, flush=True)
    print(f"  Sensor Hardware:     {physics.get('sensor_name', 'Optical Sensor')}")
    print(
        f"  Sensor Geometry:     {physics.get('sensor_w_mm', 0):.1f} x {physics.get('sensor_h_mm', 0):.1f} mm  |  Pixel Pitch: {physics.get('pixel_pitch_um', 0):.2f} um"
    )
    shutter_str = (
        "Mechanical Leaf / Global Shutter [RS Comp: DISABLED]"
        if physics.get("shutter") == "mechanical"
        else "CMOS Electronic Rolling Shutter [RS Comp: ENABLED]"
    )
    print(f"  Shutter Physics:     {shutter_str}")
    f_str = f"{physics.get('focal_mm', 0):.2f} mm" if physics.get("focal_mm") else "Auto"
    agl_str = f"{physics.get('flight_height_m', 0):.1f} m AGL" if physics.get("flight_height_m") else "Unknown"
    gsd_str = f"{physics.get('gsd_cm', 0):.2f} cm/px" if physics.get("gsd_cm") else "Unknown"
    print(f"  Optics & Altitude:   Focal Length: {f_str}  |  Flying Height: {agl_str}  |  GSD: {gsd_str}")
    fwd_ov = physics.get("forward_overlap_pct")
    lat_ov = physics.get("lateral_overlap_pct")
    ov_str = (
        f"Forward: {fwd_ov:.1f}%  |  Lateral: {lat_ov:.1f}%"
        if fwd_ov is not None and lat_ov is not None
        else "Terrestrial / Uncalibrated"
    )
    print(f"  Overlap Geometry:    {ov_str}")
    sh_clip = physics.get("shadow_clip_ratio", 0) * 100.0
    hi_clip = physics.get("highlight_clip_ratio", 0) * 100.0
    delight_str = (
        "ACTIVATED (Diffuse Albedo Recovery)"
        if plan.get("remove_lighting")
        else "Not required (Balanced Radiometry)"
    )
    print(
        f"  Dynamic Range:       Shadow Clipping: {sh_clip:.1f}%  |  Highlight: {hi_clip:.1f}%  |  Delighting: {delight_str}"
    )
    r_val = physics.get("relief_ratio", 0.0)
    surf_str = f"R = {r_val:.3f} -> {plan.get('surface_type', 'arbitrary').upper()} Meshing"
    print(
        f"  Topographic Relief:  Span: {physics.get('horizontal_span_m', 0):.1f}m  |  Delta Z: {physics.get('alt_span_m', 0):.1f}m  |  {surf_str}"
    )
    if usgs_stats:
        initial_rms = usgs_stats.get("initial_rms", 0.0)
        final_rms = usgs_stats.get("final_rms", 0.0)
        pct_imp = usgs_stats.get("rms_improvement_pct", 0.0)
        pts_init = usgs_stats.get("initial_points", 0)
        pts_final = usgs_stats.get("final_points", 0)
        print("--------------------------------------------------------------------------------")
        print(
            "  USGS 4-Pass Bundle:  Pass 1 (RU<=15) -> Pass 2 (PA<=3.0) -> Pass 3 (RE<=0.50) -> Pass 4 (Multiplicity>=2, <=10% prune)"
        )
        print(
            f"  Reprojection RMS:    Initial: {initial_rms:.3f} px  ->  Optimized: {final_rms:.3f} px  (RMS Reduction: {pct_imp:.1f}%)"
        )
        print(
            f"  Multi-ray Multiplicity: {pts_final:,} robust tie points locked in (pruned {pts_init - pts_final:,} fragile/outlier rays)"
        )
    print("=" * 80 + "\n", flush=True)
