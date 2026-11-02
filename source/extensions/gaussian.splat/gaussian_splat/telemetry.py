# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Drone telemetry extraction and coordinate projection."""

import glob
import os
import re
from typing import Any, Dict, List
import numpy as np


def extract_drone_metadata(images_dir: str) -> List[Dict[str, Any]]:
    """Extracts GPS, altitude, and gimbal telemetry from image EXIF/XMP headers."""
    raw_paths = (
        glob.glob(os.path.join(images_dir, "*.JPG"))
        + glob.glob(os.path.join(images_dir, "*.jpg"))
        + glob.glob(os.path.join(images_dir, "*.png"))
    )
    # Deduplicate paths (handles Windows case-insensitive glob matches)
    seen = set()
    image_paths = []
    for p in sorted(raw_paths):
        norm = os.path.normcase(os.path.abspath(p))
        if norm not in seen:
            seen.add(norm)
            image_paths.append(p)

    metadata = []
    for idx, p in enumerate(image_paths):
        basename = os.path.basename(p)
        try:
            with open(p, "rb") as f:
                data = f.read(65536)
            data_str = str(data)

            m_lat = re.search(r'drone-dji:GpsLatitude="([^"]+)"', data_str)
            m_lon = re.search(r'drone-dji:GpsLongitude="([^"]+)"', data_str)
            m_alt = re.search(r'drone-dji:RelativeAltitude="([^"]+)"', data_str)
            m_yaw = re.search(r'drone-dji:GimbalYawDegree="([^"]+)"', data_str)
            m_pitch = re.search(r'drone-dji:GimbalPitchDegree="([^"]+)"', data_str)

            lat = float(m_lat.group(1)) if m_lat else 0.0
            lon = float(m_lon.group(1)) if m_lon else 0.0
            alt = float(m_alt.group(1)) if m_alt else 0.0
            yaw = float(m_yaw.group(1)) if m_yaw else 0.0
            pitch = float(m_pitch.group(1)) if m_pitch else 0.0

            has_gps = lat > 1.0 and lon > 50.0
        except Exception:
            lat, lon, alt, yaw, pitch, has_gps = 0.0, 0.0, 0.0, 0.0, 0.0, False

        metadata.append(
            {
                "id": idx,
                "filename": basename,
                "path": p,
                "has_gps": has_gps,
                "lat": lat,
                "lon": lon,
                "alt": alt,
                "yaw": yaw,
                "pitch": pitch,
            }
        )

    return metadata
