# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Universal CRS classification, linear unit parsing, and coordinate transform helpers."""

from __future__ import annotations

import math
import re
from typing import Any

_ANGULAR_UNIT_NAMES = frozenset(
    {
        "degree",
        "degrees",
        "deg",
        "radian",
        "radians",
        "rad",
        "grad",
        "grads",
        "gon",
        "arc-second",
        "arc-minute",
        "arcsecond",
        "arcminute",
    }
)

_KNOWN_LINEAR_UNIT_FACTORS: dict[str, float] = {
    "m": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "metre": 1.0,
    "metres": 1.0,
    "us survey foot": 1200.0 / 3937.0,
    "us_survey_foot": 1200.0 / 3937.0,
    "foot_us": 1200.0 / 3937.0,
    "survey foot": 1200.0 / 3937.0,
    "ftus": 1200.0 / 3937.0,
    "u.s. survey foot": 1200.0 / 3937.0,
    "foot (us survey)": 1200.0 / 3937.0,
    "foot": 0.3048,
    "feet": 0.3048,
    "international foot": 0.3048,
    "international_foot": 0.3048,
    "ft": 0.3048,
    "km": 1000.0,
    "kilometre": 1000.0,
    "kilometer": 1000.0,
    "kilometres": 1000.0,
    "kilometers": 1000.0,
    "cm": 0.01,
    "centimetre": 0.01,
    "centimeter": 0.01,
    "centimetres": 0.01,
    "centimeters": 0.01,
    "mm": 0.001,
    "millimetre": 0.001,
    "millimeter": 0.001,
    "millimetres": 0.001,
    "millimeters": 0.001,
}

_STRIP_NESTED_CRS_KEYWORDS = (
    "BASEGEOGCRS",
    "GEOGCRS",
    "GEODCRS",
    "GEOGCS",
    "GEOCCS",
    "VERT_CS",
    "VERTCRS",
    "VERTICALCRS",
    "TARGETCRS",
    "ABRIDGEDTRANSFORMATION",
    "DERIVINGCONVERSION",
)

_UNIT_BLOCK_RE = re.compile(
    r'(?:LENGTHUNIT|UNIT)\s*\[\s*"([^"]+)"\s*(?:,\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?))?',
    re.IGNORECASE,
)


def _crs_wkt_upper(crs: Any) -> str:
    wkt = getattr(crs, "wkt", None)
    return wkt.strip().upper() if isinstance(wkt, str) else ""


def _crs_name_upper(crs: Any) -> str:
    name = getattr(crs, "name", None)
    return name.strip().upper() if isinstance(name, str) else ""


def _strip_wkt_blocks(wkt: str, keywords: tuple[str, ...]) -> str:
    """Remove bracket-balanced nested WKT blocks whose header keyword matches ``keywords``."""
    out: list[str] = []
    idx = 0
    n_len = len(wkt)
    upper = wkt.upper()
    sorted_kw = sorted(keywords, key=len, reverse=True)
    while idx < n_len:
        matched_kw = None
        for kw in sorted_kw:
            if upper.startswith(kw, idx):
                prev_ok = idx == 0 or not (upper[idx - 1].isalnum() or upper[idx - 1] == "_")
                after = idx + len(kw)
                pos = after
                while pos < n_len and wkt[pos].isspace():
                    pos += 1
                if prev_ok and pos < n_len and wkt[pos] == "[":
                    matched_kw = (kw, pos)
                    break
        if matched_kw is None:
            out.append(wkt[idx])
            idx += 1
            continue
        depth = 0
        pos = matched_kw[1]
        in_quotes = False
        while pos < n_len:
            ch = wkt[pos]
            if ch == '"':
                in_quotes = not in_quotes
            elif not in_quotes:
                if ch == "[":
                    depth += 1
                elif ch == "]":
                    depth -= 1
                    if depth == 0:
                        pos += 1
                        break
            pos += 1
        idx = pos
    return "".join(out)


def classify_crs(crs: Any) -> str:
    """Classify a Metashape/mock CRS object as ``'local'``, ``'projected'``, or ``'geographic'``."""
    if crs is None:
        return "local"
    wkt = _crs_wkt_upper(crs)
    name = _crs_name_upper(crs)
    if (
        "LOCAL_CS" in wkt
        or "ENGCRS" in wkt
        or "ENGINEERINGCRS" in wkt
        or "LOCAL COORDINATES" in name
        or "ENGINEERING" in name
    ):
        return "local"
    raw_projcs = getattr(crs, "projcs", None)
    has_projcs = bool(raw_projcs) and not hasattr(raw_projcs, "assert_called")
    if has_projcs or "PROJCS" in wkt or "PROJCRS" in wkt:
        return "projected"
    if "GEOGCS" in wkt or "GEOGCRS" in wkt or "GEODCRS" in wkt or "GEOCCS" in wkt:
        return "geographic"
    return "geographic"


def crs_linear_unit_to_meters(crs: Any) -> float:
    """Return horizontal linear unit scale factor to meters for ``crs`` (default ``1.0``)."""
    if crs is None:
        return 1.0
    wkt_raw = getattr(crs, "wkt", None)
    if not isinstance(wkt_raw, str) or not wkt_raw.strip():
        return 1.0

    stripped = _strip_wkt_blocks(wkt_raw, _STRIP_NESTED_CRS_KEYWORDS)
    candidates = list(_UNIT_BLOCK_RE.finditer(stripped))
    if not candidates:
        candidates = list(_UNIT_BLOCK_RE.finditer(wkt_raw))

    for match in reversed(candidates):
        raw_name = match.group(1).strip().lower()
        norm_name = re.sub(r"\s+", " ", raw_name)
        if norm_name in _ANGULAR_UNIT_NAMES:
            continue
        raw_factor = match.group(2)
        if raw_factor is not None:
            try:
                factor = float(raw_factor)
                if math.isfinite(factor) and factor > 0.0:
                    return factor
            except ValueError:
                pass
        if norm_name in _KNOWN_LINEAR_UNIT_FACTORS:
            return _KNOWN_LINEAR_UNIT_FACTORS[norm_name]
        if raw_name in _KNOWN_LINEAR_UNIT_FACTORS:
            return _KNOWN_LINEAR_UNIT_FACTORS[raw_name]
    return 1.0


def is_linear_crs(crs: Any) -> bool:
    """Return True if ``crs`` is a projected or local linear Cartesian coordinate system."""
    if crs is None:
        return False
    return classify_crs(crs) in ("projected", "local")


def is_local_or_engineering_crs(crs: Any) -> bool:
    """Return True if ``crs`` is None or a local/engineering coordinate system."""
    return crs is None or classify_crs(crs) == "local"


def is_georeferenced_crs(crs: Any) -> bool:
    """Return True if ``crs`` is a georeferenced geographic or projected coordinate system."""
    return crs is not None and classify_crs(crs) in ("projected", "geographic")


def compute_metric_epsg(lon: float, lat: float) -> int:
    """Return WGS 84 UTM or Polar Stereographic (UPS) metric EPSG code for ``(lon, lat)``."""
    if not (math.isfinite(lon) and math.isfinite(lat)):
        raise ValueError(f"Non-finite coordinates for metric EPSG calculation: lon={lon!r}, lat={lat!r}")
    if lat > 84.0:
        return 32661
    if lat < -80.0:
        return 32761
    clamped_lon = max(-180.0, min(180.0, float(lon)))
    zone = 60 if clamped_lon >= 180.0 else int(math.floor((clamped_lon + 180.0) / 6.0)) + 1
    zone = max(1, min(zone, 60))
    return 32600 + zone if lat >= 0.0 else 32700 + zone


def transform_wgs84_location_to_crs(
    loc_xyz: tuple[float, float, float],
    target_crs: Any,
    metashape_mod: Any = None,
) -> tuple[float, float, float] | None:
    """Transform a ``(lon, lat, alt)`` WGS 84 coordinate into ``target_crs``, or return None."""
    if target_crs is None or is_local_or_engineering_crs(target_crs):
        return None
    if not loc_xyz or len(loc_xyz) < 3:
        return None
    try:
        x_in, y_in, z_in = float(loc_xyz[0]), float(loc_xyz[1]), float(loc_xyz[2])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(x_in) and math.isfinite(y_in) and math.isfinite(z_in)):
        return None

    wkt_upper = _crs_wkt_upper(target_crs)
    if classify_crs(target_crs) == "geographic":
        if "4326" in wkt_upper or "WGS 84" in wkt_upper or "WGS_1984" in wkt_upper or not wkt_upper:
            return (x_in, y_in, z_in)

    ms = metashape_mod
    if ms is None:
        try:
            import Metashape as ms_mod

            ms = ms_mod
        except ImportError:
            ms = None
    if ms is None or not hasattr(ms, "CoordinateSystem") or not hasattr(ms, "Vector"):
        return None

    try:
        wgs84_crs = ms.CoordinateSystem("EPSG::4326")
        transformed = ms.CoordinateSystem.transform(ms.Vector([x_in, y_in, z_in]), wgs84_crs, target_crs)
        tx, ty, tz = float(transformed.x), float(transformed.y), float(transformed.z)
        if not (math.isfinite(tx) and math.isfinite(ty) and math.isfinite(tz)):
            return None
        return (tx, ty, tz)
    except Exception:
        return None
