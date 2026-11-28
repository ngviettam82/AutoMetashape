# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Alignment hygiene: pre-alignment GPS sanitization, TakeoffZ0 normalization, lens lock, and island quarantine.

Runs 100% natively inside Agisoft Metashape's bundled Python 3.9+ environment with zero external
dependencies. Defends multi-flight, non-RTK, and close-range UAV reconstructions against:
  1. Null-Island (0, 0) and > 5 km GPS outlier references.
  2. Inter-flight consumer barometer datum shifts (TakeoffZ0 = AbsoluteAltitude - RelativeAltitude).
  3. Runaway k4 / affinity (b1, b2) polynomial self-calibration blowup (hard-locked off).
  4. Detached rogue tie-graph sub-islands and individual GPS-spike cameras.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict, deque
from typing import Any

from autometashape.core.crs_utils import (
    classify_crs,
    crs_linear_unit_to_meters,
    crs_vertical_unit_to_meters,
    is_linear_crs,
)
from autometashape.core.telemetry import calculate_haversine_meters

try:
    import Metashape
except ImportError:
    Metashape = None

_RTK_FIXED_FLAGS = {"50", "50.0"}


def _is_metric_crs(crs: Any) -> bool:
    """Return True if ``crs`` uses linear coordinates (projected CRS or LOCAL/ENGCRS) rather than degrees."""
    return is_linear_crs(crs)


def get_camera_meta(meta: Any, key: str, default: Any = None) -> Any:
    """Safely read a metadata key from Metashape.MetaData (which lacks .get()) or a plain dict."""
    if meta is None:
        return default
    if isinstance(meta, dict):
        val = meta.get(key, default)
        return default if val is None else val
    try:
        val = meta[key]
        return default if val is None else val
    except Exception:
        return default


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).strip().lstrip("+"))
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError):
        return None


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round((len(s) - 1) * pct))))
    return s[idx]


def zero_unstable_lens_params(chunk: Any) -> int:
    """Reset unstable higher-order lens parameters on non-fixed sensors.

    Resets ``b1`` and ``b2`` across all non-fixed sensors and resets ``k4`` on frame/generic
    sensors while preserving radial ``k4`` on Fisheye, Spherical, and Cylindrical optics where
    high-order radial distortion is physically required.
    """
    sensors = getattr(chunk, "sensors", None) or []
    reset_count = 0
    for sensor in sensors:
        if getattr(sensor, "fixed", False):
            continue
        cal = getattr(sensor, "calibration", None)
        if cal is None:
            continue
        s_type = str(getattr(sensor, "type", "") or "").lower()
        is_wide_model = "fisheye" in s_type or "spherical" in s_type or "cylindrical" in s_type
        try:
            if not is_wide_model:
                cal.k4 = 0.0
            cal.b1 = 0.0
            cal.b2 = 0.0
            sensor.calibration = cal
            reset_count += 1
        except Exception:
            pass
    return reset_count


def optimize_cameras_no_k4(chunk: Any, tiepoint_covariance: bool = False) -> None:
    """Run clean bundle adjustment with k4, b1, b2, and adaptive_fitting strictly disabled."""
    zero_unstable_lens_params(chunk)
    chunk.optimizeCameras(
        fit_f=True,
        fit_cx=True,
        fit_cy=True,
        fit_b1=False,
        fit_b2=False,
        fit_k1=True,
        fit_k2=True,
        fit_k3=True,
        fit_k4=False,
        fit_p1=True,
        fit_p2=True,
        adaptive_fitting=False,
        tiepoint_covariance=tiepoint_covariance,
    )


def _inlier_spatial_mask(
    pts_xy: list[tuple[float, float]],
    max_jump_m: float = 5000.0,
    keep_secondary_components: bool = True,
) -> list[bool]:
    """Return boolean mask of spatially connected inliers using an ``O(N)`` 2D spatial hash grid.

    Points within ``max_jump_m`` form connected components so long linear corridors (>10 km) stay
    in a single component without centroid-distance amputation, while isolated GPS jumps remain
    isolated and are masked False.
    """
    n = len(pts_xy)
    if n == 0:
        return []
    if n == 1 or max_jump_m <= 0.0:
        return [True] * n

    step = max(1.0, float(max_jump_m))
    r2 = float(max_jump_m) * float(max_jump_m)
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    valid_coords: dict[int, tuple[float, float]] = {}
    for idx, (x_v, y_v) in enumerate(pts_xy):
        if math.isfinite(x_v) and math.isfinite(y_v):
            valid_coords[idx] = (x_v, y_v)
            cells[(int(math.floor(x_v / step)), int(math.floor(y_v / step)))].append(idx)

    if not valid_coords:
        return [False] * n

    visited: set[int] = set()
    components: list[list[int]] = []
    for start in range(n):
        if start in visited or start not in valid_coords:
            continue
        comp: list[int] = []
        queue = deque([start])
        visited.add(start)
        while queue:
            u = queue.popleft()
            ux, uy = valid_coords[u]
            cx = int(math.floor(ux / step))
            cy = int(math.floor(uy / step))
            comp.append(u)
            for dcx in (-1, 0, 1):
                for dcy in (-1, 0, 1):
                    for v in cells.get((cx + dcx, cy + dcy), ()):
                        if v in visited:
                            continue
                        vx, vy = valid_coords[v]
                        dx = ux - vx
                        dy = uy - vy
                        if (dx * dx + dy * dy) <= r2:
                            visited.add(v)
                            queue.append(v)
        components.append(comp)

    largest_comp = max(components, key=len)
    mask = [False] * n
    if keep_secondary_components and len(largest_comp) >= 3:
        min_keep = max(3, int(0.15 * len(valid_coords)))
        kept_any = False
        for comp in components:
            if len(comp) >= min_keep:
                kept_any = True
                for idx in comp:
                    mask[idx] = True
        if not kept_any:
            for idx in largest_comp:
                mask[idx] = True
    else:
        for idx in largest_comp:
            mask[idx] = True
    return mask


def sanitize_camera_gps(chunk: Any, max_jump_m: float = 5000.0) -> dict[str, int]:
    """Disable Null-Island (0, 0), non-finite, and disconnected > max_jump_m GPS outlier references.

    Camera photos remain enabled for visual matching; only their corrupted GPS prior is disabled.
    Handles geographic (WGS84), geocentric (ECEF), projected (meters/feet), and local engineering
    CRS cleanly without amputating long (>10 km) linear corridors.
    """
    cameras = getattr(chunk, "cameras", None) or []
    crs = getattr(chunk, "crs", None)
    crs_kind = classify_crs(crs)
    is_geocentric = crs_kind == "geocentric"
    metric_crs = _is_metric_crs(crs)
    unit_scale = crs_linear_unit_to_meters(crs) if metric_crs else 1.0
    null_disabled = 0
    outlier_disabled = 0
    valid_cams: list[tuple[Any, float, float, float]] = []

    for cam in cameras:
        ref = getattr(cam, "reference", None)
        if ref is None or not getattr(ref, "enabled", True):
            continue
        loc = getattr(ref, "location", None)
        if loc is None:
            continue
        x_val = _parse_float(getattr(loc, "x", None))
        y_val = _parse_float(getattr(loc, "y", None))
        z_val = _parse_float(getattr(loc, "z", None))
        if is_geocentric:
            is_invalid = (
                x_val is None
                or y_val is None
                or z_val is None
                or math.sqrt(x_val * x_val + y_val * y_val + z_val * z_val) < 1000.0
            )
        elif metric_crs:
            is_invalid = x_val is None or y_val is None or z_val is None
        else:
            is_invalid = (
                x_val is None
                or y_val is None
                or z_val is None
                or (abs(x_val) < 1e-4 and abs(y_val) < 1e-4)
                or abs(x_val) > 180.0
                or abs(y_val) > 90.0
            )
        if is_invalid:
            ref.enabled = False
            null_disabled += 1
        else:
            valid_cams.append((cam, x_val, y_val, z_val))

    if len(valid_cams) >= 3 and max_jump_m > 0.0:
        if is_geocentric:
            med_x = _median([x[1] for x in valid_cams])
            med_y = _median([x[2] for x in valid_cams])
            med_z = _median([x[3] for x in valid_cams])
            r_med = math.sqrt(med_x * med_x + med_y * med_y + med_z * med_z)
            h_xy = math.hypot(med_x, med_y)
            if r_med > 1000.0 and h_xy > 1e-6:
                ex, ey = -med_y / h_xy, med_x / h_xy
                uz = med_z / r_med
                nx, ny, nz = -uz * ey, uz * ex, (med_x / r_med) * ey - (med_y / r_med) * ex
                pts_m = [
                    (
                        (xv - med_x) * ex + (yv - med_y) * ey,
                        (xv - med_x) * nx + (yv - med_y) * ny + (zv - med_z) * nz,
                    )
                    for _c, xv, yv, zv in valid_cams
                ]
            else:
                pts_m = [(xv - med_x, yv - med_y) for _c, xv, yv, _zv in valid_cams]
        elif metric_crs:
            med_x = _median([x[1] for x in valid_cams])
            med_y = _median([x[2] for x in valid_cams])
            pts_m = [((xv - med_x) * unit_scale, (yv - med_y) * unit_scale) for _c, xv, yv, _zv in valid_cams]
        else:
            ref_lon_rad = math.atan2(
                sum(math.sin(math.radians(x[1])) for x in valid_cams),
                sum(math.cos(math.radians(x[1])) for x in valid_cams),
            )
            ref_lon_deg = math.degrees(ref_lon_rad)
            wrapped_offsets = [((x[1] - ref_lon_deg + 180.0) % 360.0) - 180.0 for x in valid_cams]
            med_x = ((ref_lon_deg + _median(wrapped_offsets) + 180.0) % 360.0) - 180.0
            med_y = _median([x[2] for x in valid_cams])
            cos_lat = max(1e-3, math.cos(math.radians(med_y)))
            pts_m = [
                ((((xv - med_x + 180.0) % 360.0) - 180.0) * 111320.0 * cos_lat, (yv - med_y) * 111320.0)
                for _c, xv, yv, _zv in valid_cams
            ]

        keep_mask = _inlier_spatial_mask(pts_m, max_jump_m=max_jump_m, keep_secondary_components=True)
        kept: list[tuple[Any, float, float, float]] = []
        for (cam, xv, yv, zv), is_inlier in zip(valid_cams, keep_mask):
            if not is_inlier:
                cam.reference.enabled = False
                outlier_disabled += 1
            else:
                kept.append((cam, xv, yv, zv))
        valid_cams = kept

    if null_disabled or outlier_disabled:
        print(
            f"  [Alignment Hygiene] Sanitized GPS priors: disabled {null_disabled} Null-Island/invalid "
            f"and {outlier_disabled} gross outlier (>{max_jump_m:.0f}m) reference(s); "
            f"{len(valid_cams)} valid GPS prior(s) retained.",
            flush=True,
        )

    return {
        "null_island_disabled": null_disabled,
        "outlier_disabled": outlier_disabled,
        "valid_remaining": len(valid_cams),
    }


def _is_camera_rtk_or_high_precision(
    cam: Any,
    vert_unit_m: float = 1.0,
    horiz_unit_m: float = 1.0,
) -> bool:
    """Return True if an enabled camera carries an RTK Fixed flag or sub-meter reference accuracy."""
    if not getattr(cam, "enabled", True):
        return False
    photo = getattr(cam, "photo", None)
    meta = getattr(photo, "meta", None) if photo else None
    flag = get_camera_meta(meta, "DJI/RtkFlag")
    if flag is not None and str(flag).strip() in _RTK_FIXED_FLAGS:
        return True
    ref = getattr(cam, "reference", None)
    acc = getattr(ref, "accuracy", None) if ref is not None else None
    if acc is not None:
        ax = _parse_float(getattr(acc, "x", None))
        az = _parse_float(getattr(acc, "z", None))
        if ax is not None and az is not None and (ax * horiz_unit_m) <= 0.5 and (az * vert_unit_m) <= 1.0:
            return True
    return False


def _is_rtk_fixed_survey(cameras: list[Any], crs: Any = None) -> bool:
    """Return True only if the majority of all enabled cameras hold RTK/PPK high-precision priors."""
    enabled_cams = [cam for cam in cameras if getattr(cam, "enabled", True)]
    total_enabled = max(1, len(enabled_cams))
    vert_unit_m = crs_vertical_unit_to_meters(crs) if crs is not None else 1.0
    horiz_unit_m = crs_linear_unit_to_meters(crs) if (crs is not None and _is_metric_crs(crs)) else 1.0
    rtk_tagged = 0
    rtk_fixed = 0
    high_prec_acc = 0
    acc_checked = 0
    for cam in enabled_cams:
        photo = getattr(cam, "photo", None)
        meta = getattr(photo, "meta", None) if photo else None
        flag = get_camera_meta(meta, "DJI/RtkFlag")
        if flag is not None:
            rtk_tagged += 1
            if str(flag).strip() in _RTK_FIXED_FLAGS:
                rtk_fixed += 1
        ref = getattr(cam, "reference", None)
        acc = getattr(ref, "accuracy", None) if ref is not None else None
        if acc is not None:
            ax = _parse_float(getattr(acc, "x", None))
            az = _parse_float(getattr(acc, "z", None))
            if ax is not None and az is not None:
                acc_checked += 1
                if _is_camera_rtk_or_high_precision(cam, vert_unit_m=vert_unit_m, horiz_unit_m=horiz_unit_m):
                    high_prec_acc += 1
    if rtk_tagged >= 5 and (rtk_fixed / total_enabled) >= 0.5:
        return True
    if acc_checked >= 5 and (high_prec_acc / total_enabled) >= 0.5:
        return True
    return False


def _camera_session_id(cam: Any) -> tuple[str, str]:
    """Derive a stable flight-session grouping key (normalized_parent_dir, drone_serial_or_model)."""
    photo = getattr(cam, "photo", None)
    path = getattr(photo, "path", "") if photo else ""
    parent_dir = os.path.normcase(os.path.normpath(os.path.dirname(path))) if path else "root"
    meta = getattr(photo, "meta", None) if photo else None
    serial = (
        get_camera_meta(meta, "Exif/BodySerialNumber")
        or get_camera_meta(meta, "DJI/DroneSerialNumber")
        or get_camera_meta(meta, "Exif/Model")
        or ""
    )
    return (parent_dir, str(serial).strip())


def _session_display_label(parent_dir: str) -> str:
    """Format the trailing 1-2 folder segments of a session directory for concise logs."""
    if not parent_dir or parent_dir == "root":
        return "root"
    parts = [p for p in parent_dir.replace("\\", "/").split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1] if parts else "root"


def _cluster_z0_entries(
    entries: list[tuple[Any, float, float, float]],
    gap_threshold_m: float = 1.0,
) -> list[list[tuple[Any, float, float, float]]]:
    """Partition ``(cam, aa, ra, z0)`` records into physical flight-datum clusters via 1D gap splitting.

    Completely independent of folder layout, filenames, timestamps, and camera serial numbers:
    simultaneous multi-drone flights dumped into the same folder separate cleanly whenever their
    barometer datum ``z0 = AbsoluteAltitude - RelativeAltitude`` differs by ``> gap_threshold_m``.
    """
    if not entries:
        return []
    ordered = sorted(entries, key=lambda item: item[3])
    clusters: list[list[tuple[Any, float, float, float]]] = [[ordered[0]]]
    for item in ordered[1:]:
        if (item[3] - clusters[-1][-1][3]) > gap_threshold_m:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    return clusters


def _project_cams_xy_meters(
    cams: list[Any],
    metric_crs: bool = False,
    unit_to_m: float = 1.0,
) -> tuple[float, float, list[tuple[Any, float, float]]] | None:
    """Compute block median reference ``(ref_x, ref_y)`` and local East/North meters for valid GPS cameras."""
    coords: list[tuple[Any, float, float]] = []
    for cam in cams:
        ref = getattr(cam, "reference", None)
        if ref is None or not getattr(ref, "enabled", True) or getattr(ref, "location", None) is None:
            continue
        xv = _parse_float(getattr(ref.location, "x", None))
        yv = _parse_float(getattr(ref.location, "y", None))
        if xv is not None and yv is not None and (metric_crs or not (abs(xv) < 1e-4 and abs(yv) < 1e-4)):
            coords.append((cam, xv, yv))
    if len(coords) < 2:
        return None
    ref_y = _median([c[2] for c in coords])
    if metric_crs:
        ref_x = _median([c[1] for c in coords])
        projected = [(cam, (xv - ref_x) * unit_to_m, (yv - ref_y) * unit_to_m) for cam, xv, yv in coords]
    else:
        ref_lon_deg = math.degrees(
            math.atan2(
                sum(math.sin(math.radians(c[1])) for c in coords),
                sum(math.cos(math.radians(c[1])) for c in coords),
            )
        )
        wrapped = [((c[1] - ref_lon_deg + 180.0) % 360.0) - 180.0 for c in coords]
        ref_x = ((ref_lon_deg + _median(wrapped) + 180.0) % 360.0) - 180.0
        cos_lat = max(1e-3, math.cos(math.radians(ref_y)))
        projected = [
            (cam, (((xv - ref_x + 180.0) % 360.0) - 180.0) * 111320.0 * cos_lat, (yv - ref_y) * 111320.0)
            for cam, xv, yv in coords
        ]
    return ref_x, ref_y, projected


def _score_2d_footprint(
    pts_xy: list[tuple[float, float]],
    step: float,
    minor_ratio: float = 0.24,
    major_ratio: float = 0.80,
) -> tuple[float, int]:
    """Compute dimensionless PCA-aware grid coverage and cell count for 2D ``pts_xy`` at ``step``."""
    n_in = len(pts_xy)
    sub_step = max(step / 5.0, step * 0.05)
    grid_main: dict[tuple[int, int], int] = defaultdict(int)
    grid_sub: set[tuple[int, int]] = set()
    for mx, my in pts_xy:
        grid_main[(int(math.floor(mx / step)), int(math.floor(my / step)))] += 1
        grid_sub.add((int(math.floor(mx / sub_step)), int(math.floor(my / sub_step))))

    mean_x = sum(p[0] for p in pts_xy) / n_in
    mean_y = sum(p[1] for p in pts_xy) / n_in
    vxx = sum((p[0] - mean_x) ** 2 for p in pts_xy) / n_in
    vyy = sum((p[1] - mean_y) ** 2 for p in pts_xy) / n_in
    vxy = sum((p[0] - mean_x) * (p[1] - mean_y) for p in pts_xy) / n_in
    disc = math.sqrt(max(0.0, (vxx - vyy) ** 2 + 4.0 * vxy * vxy))
    minor_std = math.sqrt(max(0.0, 0.5 * ((vxx + vyy) - disc)))
    major_std = math.sqrt(max(0.0, 0.5 * ((vxx + vyy) + disc)))

    if minor_std < minor_ratio * step:
        multi_cells = sum(1 for cnt in grid_main.values() if cnt >= 2)
        if major_std >= major_ratio * step:
            eff_cells = max(1, multi_cells, int(round(math.sqrt(len(grid_main)))))
        else:
            eff_cells = max(1, multi_cells)
    else:
        eff_cells = max(1, len(grid_main))

    sub_weight = (sub_step / step) ** 2
    eff_cov = float(eff_cells) + sub_weight * float(len(grid_sub))
    return eff_cov, eff_cells


def compute_block_backbone_score(
    cameras: list[Any],
    metric_crs: bool = False,
    cell_m: float = 25.0,
    unit_to_m: float = 1.0,
    shared_local_step: float | None = None,
) -> tuple[float, int, int, bool]:
    """Score a camera block by spatial coverage × sqrt(camera_count) using pure 2D geometry.

    Args:
        cameras: Cameras belonging to the block/island/chunk.
        metric_crs: True if chunk CRS uses linear Cartesian coordinates.
        cell_m: Primary grid cell resolution in meters for metric blocks.
        unit_to_m: Linear unit conversion factor to meters when ``metric_crs=True``.
        shared_local_step: Optional chunk-wide local grid step for comparing unreferenced islands.

    Returns:
        ``(score, eff_cells, n_cams, has_spatial_footprint)``.
    """
    n_cams = len(cameras)
    if n_cams == 0:
        return (0.0, 0, 0, False)

    proj = _project_cams_xy_meters(cameras, metric_crs=metric_crs, unit_to_m=unit_to_m)
    if proj is not None:
        _rx, _ry, pts_full = proj
        raw_pts = [(mx, my) for _cam, mx, my in pts_full]
        mask = _inlier_spatial_mask(raw_pts, max_jump_m=5000.0, keep_secondary_components=False)
        inliers = [pt for pt, ok in zip(raw_pts, mask) if ok]
        if len(inliers) >= 2:
            eff_cov, eff_cells = _score_2d_footprint(
                inliers,
                step=cell_m,
                minor_ratio=6.0 / cell_m,
                major_ratio=20.0 / cell_m,
            )
            return (eff_cov * math.sqrt(float(n_cams)), eff_cells, n_cams, True)

    # Unreferenced / LOCAL_CS fallback using aligned camera centers
    centers: list[tuple[float, float]] = []
    for cam in cameras:
        if getattr(cam, "transform", None) is None:
            continue
        ctr = getattr(cam, "center", None)
        if ctr is None:
            continue
        cx = _parse_float(getattr(ctr, "x", None))
        cy = _parse_float(getattr(ctr, "y", None))
        if cx is not None and cy is not None:
            centers.append((cx, cy))
    if len(centers) >= 2:
        grid_step = shared_local_step if (shared_local_step is not None and shared_local_step > 1e-6) else None
        if grid_step is None:
            cxs = [p[0] for p in centers]
            cys = [p[1] for p in centers]
            s_max = max(
                _percentile(cxs, 0.95) - _percentile(cxs, 0.05),
                _percentile(cys, 0.95) - _percentile(cys, 0.05),
            )
            if s_max > 1e-3:
                grid_step = max(s_max / 10.0, 1e-3)
        if grid_step is not None:
            eff_cov, eff_cells = _score_2d_footprint(centers, step=grid_step)
            return (eff_cov * math.sqrt(float(n_cams)), eff_cells, n_cams, False)

    return (math.sqrt(float(n_cams)), 1, n_cams, False)


def _evaluate_island_rigidity(
    chunk: Any,
    comp_keys: list[int],
    by_key: dict[int, Any],
    max_internal_dz_spread_m: float = 6.0,
    max_internal_dxy_spread_m: float = 8.0,
    strong_adj: dict[int, list[int]] | None = None,
) -> dict[str, Any]:
    """Evaluate internal GPS residual consistency and rigidity for a connected camera block."""
    vec_by_k: dict[int, tuple[float, float, float, float]] = {}
    for k in comp_keys:
        cam = by_key.get(k)
        if cam is None:
            continue
        vec = _compute_camera_gps_residual_vec(chunk, cam)
        if vec is not None:
            vec_by_k[k] = vec

    dzs = [v[2] for v in vec_by_k.values()]
    dxys = [v[3] for v in vec_by_k.values()]
    if dzs:
        med_dz = _median(dzs)
        med_dxy = _median(dxys)
        int_dz_spread = _percentile([abs(v - med_dz) for v in dzs], 0.90)
        int_dxy_spread = _percentile([abs(v - med_dxy) for v in dxys], 0.90)
        max_dz = max(abs(v) for v in dzs)
        max_xy = max(dxys)
        p95_dz = _percentile([abs(v) for v in dzs], 0.95)
        p95_xy = _percentile(dxys, 0.95)
    else:
        int_dz_spread = 0.0
        int_dxy_spread = 0.0
        max_dz = 0.0
        max_xy = 0.0
        p95_dz = 0.0
        p95_xy = 0.0

    nbr_dz_diffs: list[float] = []
    nbr_dxy_diffs: list[float] = []
    if strong_adj and len(vec_by_k) >= 2:
        for u, u_vec in vec_by_k.items():
            for v in strong_adj.get(u, ()):
                if v > u and v in vec_by_k:
                    v_vec = vec_by_k[v]
                    nbr_dz_diffs.append(abs(u_vec[2] - v_vec[2]))
                    nbr_dxy_diffs.append(math.hypot(u_vec[0] - v_vec[0], u_vec[1] - v_vec[1]))

    strict_rigid = int_dz_spread <= max_internal_dz_spread_m and int_dxy_spread <= max_internal_dxy_spread_m
    smooth_drift_rigid = (
        bool(nbr_dz_diffs)
        and int_dz_spread <= 15.0
        and int_dxy_spread <= 12.0
        and _percentile(nbr_dz_diffs, 0.90) <= 3.5
        and _percentile(nbr_dxy_diffs, 0.90) <= 4.0
    )
    is_rigid = strict_rigid or smooth_drift_rigid
    return {
        "gps_count": len(dzs),
        "int_dz_spread": int_dz_spread,
        "int_dxy_spread": int_dxy_spread,
        "max_dz": max_dz,
        "max_xy": max_xy,
        "p95_dz": p95_dz,
        "p95_xy": p95_xy,
        "is_rigid": is_rigid,
    }


def _dominant_display_label(cams: list[Any]) -> str:
    """Return a concise human-readable label for logging only (never used for grouping/anchoring)."""
    counts: dict[str, int] = defaultdict(int)
    for cam in cams:
        parent_dir = _camera_session_id(cam)[0]
        counts[_session_display_label(parent_dir)] += 1
    if not counts:
        return "flight"
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _set_accuracy_vec(target: Any, attr: str, ax: float, az: float) -> None:
    """Assign an anisotropic ``(x, y, z)`` accuracy vector using ``Metashape.Vector`` when available."""
    try:
        if Metashape is not None and hasattr(Metashape, "Vector"):
            setattr(target, attr, Metashape.Vector([float(ax), float(ax), float(az)]))
        else:
            setattr(target, attr, (float(ax), float(ax), float(az)))
    except Exception:
        pass


def _cam_z0_state_key(cam: Any) -> str:
    """Return stable per-camera identifier for tracking idempotent TakeoffZ0 deltas."""
    photo = getattr(cam, "photo", None)
    path = getattr(photo, "path", "") if photo else ""
    if isinstance(path, str) and path.strip():
        return os.path.normcase(os.path.abspath(path))
    return f"k:{getattr(cam, 'key', id(cam))}:{getattr(cam, 'label', '')}"


def _read_z0_applied_map(chunk: Any) -> dict[str, float]:
    """Read previously applied TakeoffZ0 vertical deltas (in meters) from chunk metadata."""
    in_mem = getattr(chunk, "_autometashape_z0_applied_map", None)
    if isinstance(in_mem, dict):
        return {str(k): float(v) for k, v in in_mem.items()}
    raw = get_camera_meta(getattr(chunk, "meta", None), "_autometashape_z0_applied")
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                return {str(k): float(v) for k, v in loaded.items()}
        except Exception:
            pass
    return {}


def _write_z0_applied_map(chunk: Any, applied_map: dict[str, float]) -> None:
    """Persist applied TakeoffZ0 vertical deltas on chunk object and persistent metadata."""
    try:
        setattr(chunk, "_autometashape_z0_applied_map", dict(applied_map))
    except Exception:
        pass
    c_meta = getattr(chunk, "meta", None)
    if c_meta is not None:
        try:
            c_meta["_autometashape_z0_applied"] = json.dumps(applied_map)
        except Exception:
            pass


def normalize_flight_takeoff_z0(
    chunk: Any,
    set_anisotropic_accuracy: bool = True,
    xy_accuracy_m: float = 5.0,
    z_accuracy_m: float = 15.0,
    anchor_z0_override: float | None = None,
) -> dict[str, Any]:
    """Normalize consumer barometer datum shifts onto a single vertical datum (folder- & time-agnostic).

    For non-RTK DJI/consumer UAVs, ``AbsoluteAltitude`` suffers 5-40 m barometer/GPS datum offsets
    between batteries, drones, or days while ``RelativeAltitude`` stays consistent above takeoff.
    Applies idempotent relative deltas in native CRS vertical units while preserving RTK cameras,
    externally imported GCP/camera references, geoid undulations, and spatially disjoint corridors.
    """
    cameras = list(getattr(chunk, "cameras", None) or [])
    if not cameras:
        return {"skipped": True, "reason": "no_cameras", "shifts": {}}

    crs = getattr(chunk, "crs", None)
    crs_kind = classify_crs(crs)
    if crs is not None and crs_kind in ("local", "geocentric"):
        return {"skipped": True, "reason": f"{crs_kind}_crs", "shifts": {}}

    if _is_rtk_fixed_survey(cameras, crs=crs):
        print(
            "  [Alignment Hygiene] RTK Fixed survey detected — preserving native RTK altitudes and accuracy.",
            flush=True,
        )
        return {"skipped": True, "reason": "rtk_fixed", "shifts": {}}

    metric_crs = _is_metric_crs(crs)
    horiz_unit_m = crs_linear_unit_to_meters(crs) if (crs is not None and metric_crs) else 1.0
    vert_unit_m = crs_vertical_unit_to_meters(crs) if crs is not None else 1.0

    # Collect shiftable (cam, aa, ra, z0) entries plus any high-precision RTK anchor cameras
    z0_entries: list[tuple[Any, float, float, float]] = []
    rtk_z0s: list[float] = []
    rtk_cams: list[Any] = []
    has_any_rtk = False
    for cam in cameras:
        ref = getattr(cam, "reference", None)
        if ref is not None and not getattr(ref, "enabled", True):
            continue
        photo = getattr(cam, "photo", None)
        meta = getattr(photo, "meta", None) if photo else None
        aa = _parse_float(get_camera_meta(meta, "DJI/AbsoluteAltitude"))
        ra = _parse_float(get_camera_meta(meta, "DJI/RelativeAltitude"))
        if _is_camera_rtk_or_high_precision(cam, vert_unit_m=vert_unit_m, horiz_unit_m=horiz_unit_m):
            has_any_rtk = True
            rtk_cams.append(cam)
            if aa is not None and ra is not None:
                rtk_z0s.append(aa - ra)
            continue
        if str(get_camera_meta(getattr(cam, "meta", None), "_autometashape_external_ref", "")) == "1":
            continue
        if aa is not None and ra is not None:
            z0_entries.append((cam, aa, ra, aa - ra))

    shifts: dict[str, float] = {}
    anchor_label = ""
    ref_z0 = anchor_z0_override
    skipped_clusters: list[tuple[dict[str, Any], float]] = []
    eligible_cams: list[Any] = []
    eligible_cam_ids: set[int] = set()

    if z0_entries:
        clusters = _cluster_z0_entries(z0_entries, gap_threshold_m=1.0)
        cluster_stats = []
        for cl in clusters:
            cl_cams = [x[0] for x in cl]
            cl_med_z0 = _median([x[3] for x in cl])
            cl_med_ra = _median([x[2] for x in cl])
            cluster_stats.append(
                {
                    "cams": cl_cams,
                    "entries": cl,
                    "count": len(cl),
                    "med_z0": cl_med_z0,
                    "med_ra": cl_med_ra,
                    "label": _dominant_display_label(cl_cams),
                }
            )

        anchor_cs: dict[str, Any] | None = None
        anchor_cams_xy: list[Any] = []
        if ref_z0 is None:
            if rtk_z0s:
                ref_z0 = _median(rtk_z0s)
                anchor_label = "rtk_anchor"
                anchor_cams_xy = rtk_cams
            else:
                min_anchor_cams = min(5, max(2, len(z0_entries) // 10)) if len(z0_entries) >= 6 else 1
                high_alt = [
                    cs for cs in cluster_stats if cs["med_ra"] >= 25.0 and cs["count"] >= min_anchor_cams
                ]
                min_count_pool = [cs for cs in cluster_stats if cs["count"] >= min_anchor_cams]
                pool = high_alt if high_alt else (min_count_pool if min_count_pool else cluster_stats)
                anchor_cs = max(
                    pool,
                    key=lambda cs: (
                        compute_block_backbone_score(
                            cs["cams"], metric_crs=metric_crs, unit_to_m=horiz_unit_m
                        )[0],
                        round(cs["med_ra"] / 5.0),
                    ),
                )
                ref_z0 = anchor_cs["med_z0"]
                anchor_label = anchor_cs["label"]
                anchor_cams_xy = anchor_cs["cams"]
        else:
            anchor_label = "external_anchor"

        anchor_xy_bounds: tuple[float, float, float, float, float, float] | None = None
        ov_margin_m = 200.0
        if anchor_z0_override is None and anchor_cams_xy:
            proj_a = _project_cams_xy_meters(anchor_cams_xy, metric_crs=metric_crs, unit_to_m=horiz_unit_m)
            if proj_a is not None:
                a_rx, a_ry, a_pts = proj_a
                a_xs = [p[1] for p in a_pts]
                a_ys = [p[2] for p in a_pts]
                a_min_x, a_max_x = min(a_xs), max(a_xs)
                a_min_y, a_max_y = min(a_ys), max(a_ys)
                anchor_diag_m = math.hypot(a_max_x - a_min_x, a_max_y - a_min_y)
                ov_margin_m = max(200.0, 2.0 * anchor_diag_m)
                anchor_xy_bounds = (a_rx, a_ry, a_min_x, a_max_x, a_min_y, a_max_y)

        shifted_cams = 0
        for idx, cs in enumerate(cluster_stats, 1):
            dz_med = ref_z0 - cs["med_z0"]
            if anchor_z0_override is None and abs(dz_med) > 60.0:
                print(
                    f"  [Alignment Hygiene] [WARNING] Skipping TakeoffZ0 shift on '{cs['label']}' "
                    f"(|dz0|={abs(dz_med):.1f}m > 60m — likely distinct terrain/rooftop launch).",
                    flush=True,
                )
                skipped_clusters.append((cs, dz_med))
                continue
            if anchor_z0_override is None and cs is not anchor_cs and anchor_xy_bounds is not None:
                a_rx, a_ry, a_min_x, a_max_x, a_min_y, a_max_y = anchor_xy_bounds
                c_xys = [
                    xy
                    for cl_cam in cs["cams"]
                    if (xy := _cam_xy_meters(cl_cam, a_rx, a_ry, metric_crs=metric_crs, unit_to_m=horiz_unit_m))
                    is not None
                ]
                if c_xys:
                    c_min_x = min(p[0] for p in c_xys)
                    c_max_x = max(p[0] for p in c_xys)
                    c_min_y = min(p[1] for p in c_xys)
                    c_max_y = max(p[1] for p in c_xys)
                    overlaps_xy = (
                        c_max_x >= a_min_x - ov_margin_m
                        and c_min_x <= a_max_x + ov_margin_m
                        and c_max_y >= a_min_y - ov_margin_m
                        and c_min_y <= a_max_y + ov_margin_m
                    )
                    if not overlaps_xy:
                        print(
                            f"  [Alignment Hygiene] [WARNING] Skipping TakeoffZ0 shift on '{cs['label']}' "
                            f"(zero XY overlap within {ov_margin_m:.0f}m of anchor — likely distant launch).",
                            flush=True,
                        )
                        skipped_clusters.append((cs, dz_med))
                        continue
            for cl_cam in cs["cams"]:
                eligible_cam_ids.add(id(cl_cam))
                eligible_cams.append(cl_cam)
            key_label = f"{cs['label']}[z0={cs['med_z0']:.1f}m]"
            if key_label in shifts:
                key_label = f"{cs['label']}#{idx}[z0={cs['med_z0']:.1f}m]"
            shifts[key_label] = dz_med

        # Apply exact idempotent relative delta in native CRS vertical units (preserves geoid/datum)
        applied_map = _read_z0_applied_map(chunk)
        safe_vert_m = vert_unit_m if vert_unit_m > 0.0 else 1.0
        for cam, _aa, _ra, cam_z0 in z0_entries:
            if id(cam) not in eligible_cam_ids:
                continue
            ref = getattr(cam, "reference", None)
            if ref is None or not getattr(ref, "enabled", True) or getattr(ref, "location", None) is None:
                continue
            loc = ref.location
            c_state_key = _cam_z0_state_key(cam)
            target_delta_m = ref_z0 - cam_z0
            prev_dz_m = float(applied_map.get(c_state_key, 0.0))
            step_dz_m = target_delta_m - prev_dz_m
            if abs(step_dz_m) > 0.01:
                new_z = float(loc.z) + (step_dz_m / safe_vert_m)
                if Metashape is not None and hasattr(Metashape, "Vector"):
                    ref.location = Metashape.Vector([loc.x, loc.y, new_z])
                else:
                    loc.z = new_z
                shifted_cams += 1
            applied_map[c_state_key] = target_delta_m
        _write_z0_applied_map(chunk, applied_map)

        max_shift = max((abs(v) for v in shifts.values()), default=0.0)
        if len(shifts) > 1 or max_shift > 0.05:
            summary = ", ".join(f"{k}:{v:+.2f}m" for k, v in sorted(shifts.items()))
            print(
                f"  [Alignment Hygiene] Normalized TakeoffZ0 onto '{anchor_label}' ({ref_z0:.2f}m, "
                f"shifted {shifted_cams} cam(s)): {summary}",
                flush=True,
            )

    if set_anisotropic_accuracy:
        scale_xy = horiz_unit_m if (crs is not None and metric_crs and horiz_unit_m > 0.0) else 1.0
        scale_z = vert_unit_m if (crs is not None and metric_crs and vert_unit_m > 0.0) else 1.0
        xy_set = xy_accuracy_m / scale_xy
        z_set = z_accuracy_m / scale_z
        if has_any_rtk:
            for e_cam in eligible_cams:
                e_ref = getattr(e_cam, "reference", None)
                if e_ref is not None:
                    _set_accuracy_vec(e_ref, "accuracy", xy_set, z_set)
        else:
            _set_accuracy_vec(chunk, "camera_location_accuracy", xy_set, z_set)

        for sk_cs, sk_dz in skipped_clusters:
            if abs(sk_dz) > z_accuracy_m:
                loosen_z = max(z_accuracy_m, abs(sk_dz)) / scale_z
                for sk_cam in sk_cs["cams"]:
                    if _is_camera_rtk_or_high_precision(sk_cam, vert_unit_m=vert_unit_m, horiz_unit_m=horiz_unit_m):
                        continue
                    sk_ref = getattr(sk_cam, "reference", None)
                    if sk_ref is not None:
                        _set_accuracy_vec(sk_ref, "accuracy", xy_set, loosen_z)

    return {
        "skipped": False,
        "anchor_label": anchor_label,
        "anchor_z0": ref_z0,
        "shifts": shifts,
    }


def _compute_camera_gps_residual_vec(chunk: Any, camera: Any) -> tuple[float, float, float, float] | None:
    """Return ``(dX_m, dY_m, dZ_m, dXY_m)`` between estimated camera position and GPS reference."""
    if not getattr(camera, "transform", None) or not getattr(camera, "enabled", True):
        return None
    ref = getattr(camera, "reference", None)
    if ref is None or not getattr(ref, "enabled", True) or getattr(ref, "location", None) is None:
        return None
    crs = getattr(chunk, "crs", None)
    transform = getattr(chunk, "transform", None)
    matrix = getattr(transform, "matrix", None) if transform is not None else None
    if crs is None or matrix is None or getattr(camera, "center", None) is None:
        return None
    try:
        ref_loc = ref.location
        est = crs.project(matrix.mulp(camera.center))
        vert_scale = crs_vertical_unit_to_meters(crs)
        if classify_crs(crs) == "geocentric":
            dx = float(est.x - ref_loc.x)
            dy = float(est.y - ref_loc.y)
            dz = float(est.z - ref_loc.z)
        elif _is_metric_crs(crs):
            unit_scale = crs_linear_unit_to_meters(crs)
            dx = float(est.x - ref_loc.x) * unit_scale
            dy = float(est.y - ref_loc.y) * unit_scale
            dz = float(est.z - ref_loc.z) * vert_scale
        else:
            dz = float(est.z - ref_loc.z) * vert_scale
            dy = float(est.y - ref_loc.y) * 111320.0
            dlon = ((float(est.x) - float(ref_loc.x) + 180.0) % 360.0) - 180.0
            dx = dlon * 111320.0 * max(1e-3, math.cos(math.radians(float(ref_loc.y))))
        if not (math.isfinite(dx) and math.isfinite(dy) and math.isfinite(dz)):
            return None
        return dx, dy, dz, math.hypot(dx, dy)
    except Exception:
        return None


def compute_camera_gps_residual(chunk: Any, camera: Any) -> tuple[float, float] | None:
    """Return ``(dZ_m, dXY_m)`` between estimated camera position and GPS reference, or ``None``."""
    vec = _compute_camera_gps_residual_vec(chunk, camera)
    if vec is None:
        return None
    return vec[2], vec[3]


def _camera_altitude_with_frame(cam: Any, vert_unit_m: float = 1.0) -> tuple[float, str] | None:
    """Return ``(altitude_m, frame)`` where ``frame`` is ``'agl'`` (RelativeAltitude) or ``'msl'``."""
    photo = getattr(cam, "photo", None)
    meta = getattr(photo, "meta", None) if photo else None
    ra = _parse_float(get_camera_meta(meta, "DJI/RelativeAltitude"))
    if ra is not None:
        return (ra, "agl")
    ref = getattr(cam, "reference", None)
    loc = getattr(ref, "location", None) if ref is not None else None
    zv = _parse_float(getattr(loc, "z", None)) if loc is not None else None
    if zv is not None:
        return (zv * vert_unit_m, "msl")
    return None


def _camera_altitude_m(cam: Any) -> float | None:
    """Return camera RelativeAltitude (preferred) or reference Z elevation in meters."""
    res = _camera_altitude_with_frame(cam)
    return res[0] if res is not None else None


def _camera_focal_px(cam: Any) -> float | None:
    """Return unit-consistent normalized focal length (``f / sensor_width``) for zoom-ratio comparison.

    Normalizing by image width prevents false zoom-discontinuity cuts between different-resolution
    sensors sharing the same FOV and prevents mixing pixels (``cal.f``) with millimeters
    (``sensor.focal_length``).
    """
    sensor = getattr(cam, "sensor", None)
    if sensor is None:
        return None
    sw = _parse_float(getattr(sensor, "width", None))
    cal = getattr(sensor, "calibration", None)
    f_val = _parse_float(getattr(cal, "f", None)) if cal is not None else None
    if f_val is not None and f_val > 1.0:
        if sw is not None and sw > 0.0:
            return f_val / sw
        return f_val
    fl_mm = _parse_float(getattr(sensor, "focal_length", None))
    pw_mm = _parse_float(getattr(sensor, "pixel_width", None))
    if fl_mm is not None and fl_mm > 0.1 and sw is not None and sw > 0.0 and pw_mm is not None and pw_mm > 0.0:
        return fl_mm / (sw * pw_mm)
    return None


def _camera_sensor_id(cam: Any) -> Any:
    """Return stable per-sensor identifier (Metashape splits distinct drones & zoom levels here)."""
    sensor = getattr(cam, "sensor", None)
    if sensor is None:
        return None
    return getattr(sensor, "key", id(sensor))


def _find_tarjan_bridges(
    nodes: list[int],
    strong_adj: dict[int, list[int]],
) -> list[tuple[int, int, int, int]]:
    """Iterative Tarjan bridge-finding in O(V + E) — stack-safe for 50,000+ camera graphs.

    Returns list of ``(u, v, subtree_size_v, comp_size)`` for every 1-bridge edge ``(u, v)``.
    """
    disc: dict[int, int] = {}
    low: dict[int, int] = {}
    parent: dict[int, int | None] = {}
    subtree_size: dict[int, int] = {}
    bridges_raw: list[tuple[int, int, int]] = []  # (u, v, root)
    comp_sizes: dict[int, int] = {}
    timer = 0

    for root in nodes:
        if root in disc:
            continue
        disc[root] = low[root] = timer
        timer += 1
        parent[root] = None
        subtree_size[root] = 1
        # Explicit stack entries: (node, neighbor_index)
        stack: list[tuple[int, int]] = [(root, 0)]
        comp_count = 1
        while stack:
            u, ni = stack[-1]
            nbrs = strong_adj.get(u, ())
            if ni < len(nbrs):
                stack[-1] = (u, ni + 1)
                v = nbrs[ni]
                if v not in disc:
                    parent[v] = u
                    disc[v] = low[v] = timer
                    timer += 1
                    subtree_size[v] = 1
                    comp_count += 1
                    stack.append((v, 0))
                elif v != parent[u]:
                    if disc[v] < low[u]:
                        low[u] = disc[v]
            else:
                stack.pop()
                p = parent[u]
                if p is not None:
                    subtree_size[p] += subtree_size[u]
                    if low[u] < low[p]:
                        low[p] = low[u]
                    if low[u] > disc[p]:
                        bridges_raw.append((p, u, root))
        comp_sizes[root] = comp_count

    return [(u, v, subtree_size[v], comp_sizes[root]) for u, v, root in bridges_raw]


def _side_has_regime_discontinuity(
    chunk: Any,
    by_key: dict[int, Any],
    side_a: list[int],
    side_b: list[int],
    bridge_weight: int,
    focal_ratio_thresh: float = 1.25,
    altitude_diff_m: float = 15.0,
    gps_jump_m: float = 3.0,
) -> str | None:
    """Return discontinuity reason if two sizable sub-blocks across a weak 1-bridge differ physically."""
    if bridge_weight >= 80:
        return None

    # 1. Zoom / calibrated focal-length discontinuity (e.g. 1x Manual vs 2x Aerial)
    fa = [v for k in side_a if (v := _camera_focal_px(by_key[k])) is not None]
    fb = [v for k in side_b if (v := _camera_focal_px(by_key[k])) is not None]
    if fa and fb:
        med_fa, med_fb = _median(fa), _median(fb)
        lo_f, hi_f = min(med_fa, med_fb), max(med_fa, med_fb)
        if lo_f > 0.0 and (hi_f / lo_f) >= focal_ratio_thresh:
            return f"zoom_ratio={hi_f / lo_f:.2f}x"

    # 2. Distinct sensor/zoom group held by a weak (<50 tracks) single camera pair
    if bridge_weight < 50:
        sa_cnt: dict[Any, int] = defaultdict(int)
        sb_cnt: dict[Any, int] = defaultdict(int)
        for k in side_a:
            sid = _camera_sensor_id(by_key[k])
            if sid is not None:
                sa_cnt[sid] += 1
        for k in side_b:
            sid = _camera_sensor_id(by_key[k])
            if sid is not None:
                sb_cnt[sid] += 1
        if sa_cnt and sb_cnt:
            dom_a = max(sa_cnt.items(), key=lambda kv: kv[1])[0]
            dom_b = max(sb_cnt.items(), key=lambda kv: kv[1])[0]
            if dom_a != dom_b:
                return f"sensor_mismatch({dom_a}!={dom_b},w={bridge_weight})"

    # 3. Altitude regime jump (compare strictly within matching AGL or MSL frame)
    vert_unit_m = crs_vertical_unit_to_meters(getattr(chunk, "crs", None))
    za_pairs = [
        res for k in side_a if (res := _camera_altitude_with_frame(by_key[k], vert_unit_m=vert_unit_m)) is not None
    ]
    zb_pairs = [
        res for k in side_b if (res := _camera_altitude_with_frame(by_key[k], vert_unit_m=vert_unit_m)) is not None
    ]
    za_agl = [v for v, fr in za_pairs if fr == "agl"]
    zb_agl = [v for v, fr in zb_pairs if fr == "agl"]
    za_msl = [v for v, fr in za_pairs if fr == "msl"]
    zb_msl = [v for v, fr in zb_pairs if fr == "msl"]
    if za_agl and zb_agl:
        dz_alt = abs(_median(za_agl) - _median(zb_agl))
        if dz_alt >= altitude_diff_m:
            return f"alt_diff={dz_alt:.1f}m"
    elif za_msl and zb_msl:
        dz_alt = abs(_median(za_msl) - _median(zb_msl))
        if dz_alt >= altitude_diff_m:
            return f"alt_diff={dz_alt:.1f}m"

    # 4. Signed GPS residual jump across the hinge (already drifted sub-block)
    ra_vecs = [v for k in side_a if (v := _compute_camera_gps_residual_vec(chunk, by_key[k])) is not None]
    rb_vecs = [v for k in side_b if (v := _compute_camera_gps_residual_vec(chunk, by_key[k])) is not None]
    if len(ra_vecs) >= 3 and len(rb_vecs) >= 3:
        dx_diff = _median([v[0] for v in ra_vecs]) - _median([v[0] for v in rb_vecs])
        dy_diff = _median([v[1] for v in ra_vecs]) - _median([v[1] for v in rb_vecs])
        dz_diff = _median([v[2] for v in ra_vecs]) - _median([v[2] for v in rb_vecs])
        xy_jump = math.hypot(dx_diff, dy_diff)
        if abs(dz_diff) >= gps_jump_m or xy_jump >= gps_jump_m:
            return f"gps_jump(dXY={xy_jump:.2f}m,dZ={dz_diff:+.2f}m)"

    return None


def _build_tie_point_graph_and_islands(
    chunk: Any,
    edge_min: int = 15,
    max_track_len: int = 40,
    cut_weak_hinges: bool = True,
    min_bridge_cams: int = 12,
) -> tuple[list[list[int]], dict[int, list[int]]]:
    """Build valid tie-point adjacency graph, cut fragile cross-regime 1-bridges, and return islands."""
    aligned = [c for c in (getattr(chunk, "cameras", None) or []) if getattr(c, "transform", None) and c.enabled]
    if not aligned:
        return [], {}

    by_key = {c.key: c for c in aligned}
    adj: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    tie_points = getattr(chunk, "tie_points", None)
    if tie_points is not None:
        # Filter to valid triangulated tie points when available (excludes pruned outliers & 2D-only tracks)
        valid_tids: set[int] | None = None
        raw_pts = getattr(tie_points, "points", None)
        if raw_pts is not None:
            try:
                vset = {p.track_id for p in raw_pts if getattr(p, "valid", True)}
                if vset or len(raw_pts) > 0:
                    valid_tids = vset
            except Exception:
                valid_tids = None

        projs = getattr(tie_points, "projections", None)
        if projs is not None:
            track_cams: dict[int, list[int]] = defaultdict(list)
            for cam in aligned:
                try:
                    cam_projs = projs[cam]
                except Exception:
                    continue
                seen_tracks: set[int] = set()
                for pr in cam_projs:
                    tid = pr.track_id
                    if valid_tids is not None and tid not in valid_tids:
                        continue
                    if tid not in seen_tracks:
                        seen_tracks.add(tid)
                        track_cams[tid].append(cam.key)
            for ckeys in track_cams.values():
                n_k = len(ckeys)
                if 2 <= n_k <= max_track_len:
                    for i in range(n_k):
                        ki = ckeys[i]
                        adj_i = adj[ki]
                        for j in range(i + 1, n_k):
                            kj = ckeys[j]
                            adj_i[kj] += 1
                            adj[kj][ki] += 1
                elif n_k > max_track_len:
                    # Preserve dense close-range and cross-flight anchor tracks in O(n_k) without double-counting
                    ordered = sorted(ckeys)
                    window = min(4, n_k - 1)
                    stride = max(1, n_k // 5)
                    seen_pairs: set[tuple[int, int]] = set()
                    for i in range(n_k):
                        ki = ordered[i]
                        for step in range(1, window + 1):
                            kj = ordered[(i + step) % n_k]
                            if kj != ki:
                                seen_pairs.add((ki, kj) if ki < kj else (kj, ki))
                        for jump in range(1, 5):
                            kj = ordered[(i + jump * stride) % n_k]
                            if kj != ki:
                                seen_pairs.add((ki, kj) if ki < kj else (kj, ki))
                    for u_k, v_k in seen_pairs:
                        adj[u_k][v_k] += 1
                        adj[v_k][u_k] += 1

    strong_adj: dict[int, list[int]] = {
        cam.key: [v for v, w in adj[cam.key].items() if w >= edge_min] for cam in aligned
    }

    # Weak 1-edge hinge guard: cut fragile single-camera-pair bridges between sizable blocks
    # ONLY when physical zoom/sensor/altitude/GPS discontinuity exists (preserves uniform corridors).
    cut_edges: set[tuple[int, int]] = set()
    if cut_weak_hinges and len(aligned) >= 2 * min_bridge_cams:
        node_keys = [c.key for c in aligned]
        bridges = _find_tarjan_bridges(node_keys, strong_adj)
        for u, v, sub_v, comp_sz in bridges:
            if min(sub_v, comp_sz - sub_v) < min_bridge_cams:
                continue
            edge_key = (min(u, v), max(u, v))
            if edge_key in cut_edges:
                continue

            # Collect exact side A (from u) and side B (from v) without crossing edge_key or prior cuts
            blocked = cut_edges | {edge_key}

            def _collect_side(start: int, blocked_edges: set[tuple[int, int]]) -> list[int]:
                seen_s: set[int] = {start}
                q_s = deque([start])
                out: list[int] = []
                while q_s:
                    cur = q_s.popleft()
                    out.append(cur)
                    for nb in strong_adj.get(cur, ()):
                        if nb not in seen_s and (min(cur, nb), max(cur, nb)) not in blocked_edges:
                            seen_s.add(nb)
                            q_s.append(nb)
                return out

            side_a = _collect_side(u, blocked)
            side_b = _collect_side(v, blocked)
            if len(side_a) < min_bridge_cams or len(side_b) < min_bridge_cams:
                continue

            w_uv = adj[u].get(v, 0)
            reason = _side_has_regime_discontinuity(chunk, by_key, side_a, side_b, bridge_weight=w_uv)
            if reason is not None:
                cut_edges.add(edge_key)
                print(
                    f"  [Tie-Graph Audit] Cut fragile 1-edge hinge ({by_key[u].label} <-> {by_key[v].label}, "
                    f"w={w_uv}, sides={len(side_a)}|{len(side_b)}, {reason}) for clean ICP isolation.",
                    flush=True,
                )

    if cut_edges:
        strong_adj = {u: [v for v in nbrs if (min(u, v), max(u, v)) not in cut_edges] for u, nbrs in strong_adj.items()}

    visited: set[int] = set()
    islands: list[list[int]] = []
    for cam in aligned:
        if cam.key in visited:
            continue
        comp: list[int] = []
        queue = deque([cam.key])
        visited.add(cam.key)
        while queue:
            u = queue.popleft()
            comp.append(u)
            for v in strong_adj.get(u, ()):
                if v not in visited:
                    visited.add(v)
                    queue.append(v)
        islands.append(comp)

    islands.sort(key=len, reverse=True)
    return islands, strong_adj


def build_tie_point_islands(
    chunk: Any,
    edge_min: int = 15,
    max_track_len: int = 40,
    cut_weak_hinges: bool = True,
    min_bridge_cams: int = 12,
) -> list[list[int]]:
    """Build exact camera connectivity islands from shared valid tie-point tracks.

    Two aligned, enabled cameras are connected only when they share at least ``edge_min`` valid
    tie tracks, and fragile 1-edge hinges spanning physical zoom/altitude/GPS discontinuities are
    cut cleanly. Never relies on ``len(chunk.components)`` or folder names.
    """
    islands, _ = _build_tie_point_graph_and_islands(
        chunk,
        edge_min=edge_min,
        max_track_len=max_track_len,
        cut_weak_hinges=cut_weak_hinges,
        min_bridge_cams=min_bridge_cams,
    )
    return islands


def _cam_xy_meters(
    cam: Any,
    ref_x: float,
    ref_y: float,
    metric_crs: bool = False,
    unit_to_m: float = 1.0,
) -> tuple[float, float] | None:
    """Convert a camera's reference into approximate local East/North meters."""
    ref = getattr(cam, "reference", None)
    if ref is None or not getattr(ref, "enabled", True) or getattr(ref, "location", None) is None:
        return None
    loc = ref.location
    xv = _parse_float(getattr(loc, "x", None))
    yv = _parse_float(getattr(loc, "y", None))
    if xv is None or yv is None:
        return None
    if metric_crs:
        return ((xv - ref_x) * unit_to_m, (yv - ref_y) * unit_to_m)
    if abs(xv) < 1e-4 and abs(yv) < 1e-4:
        return None
    cos_lat = max(1e-3, math.cos(math.radians(ref_y)))
    dlon = ((xv - ref_x + 180.0) % 360.0) - 180.0
    return (dlon * 111320.0 * cos_lat, (yv - ref_y) * 111320.0)


def _compute_main_xy_bounds(
    cams: list[Any],
    metric_crs: bool = False,
    unit_to_m: float = 1.0,
) -> tuple[float, float, float, float, float, float] | None:
    """Return ``(ref_x, ref_y, min_x, max_x, min_y, max_y)`` for valid referenced cameras."""
    proj = _project_cams_xy_meters(cams, metric_crs=metric_crs, unit_to_m=unit_to_m)
    if proj is None:
        return None
    ref_x, ref_y, pts = proj
    xs = [mx for _cam, mx, _my in pts]
    ys = [my for _cam, _mx, my in pts]
    if len(pts) >= 20:
        return (
            ref_x,
            ref_y,
            _percentile(xs, 0.02),
            _percentile(xs, 0.98),
            _percentile(ys, 0.02),
            _percentile(ys, 0.98),
        )
    return (ref_x, ref_y, min(xs), max(xs), min(ys), max(ys))


def _symmetric_bounds_overlap_ratio(
    cams_a: list[Any],
    bounds_a: tuple[float, float, float, float, float, float] | None,
    cams_b: list[Any],
    bounds_b: tuple[float, float, float, float, float, float] | None,
    metric_crs: bool,
    overlap_margin_m: float,
    unit_to_m: float = 1.0,
) -> float:
    """Compute bidirectional spatial overlap ratio ``max(frac(B in A), frac(A in B))``."""

    def _frac_inside(
        cams: list[Any],
        bounds: tuple[float, float, float, float, float, float] | None,
        fallback: float,
    ) -> float:
        if bounds is None:
            return fallback
        ref_x, ref_y, min_x, max_x, min_y, max_y = bounds
        checked = 0
        inside = 0
        for cam in cams:
            xy = _cam_xy_meters(cam, ref_x, ref_y, metric_crs=metric_crs, unit_to_m=unit_to_m)
            if xy is None:
                continue
            checked += 1
            if (min_x - overlap_margin_m) <= xy[0] <= (max_x + overlap_margin_m) and (min_y - overlap_margin_m) <= xy[
                1
            ] <= (max_y + overlap_margin_m):
                inside += 1
        return (inside / checked) if checked > 0 else fallback

    frac_b_in_a = _frac_inside(cams_b, bounds_a, 1.0)
    frac_a_in_b = _frac_inside(cams_a, bounds_b, 0.0)
    return max(frac_b_in_a, frac_a_in_b)


def classify_and_quarantine_islands(
    chunk: Any,
    edge_min: int = 15,
    min_bridge_cams: int = 12,
    min_distant_block_cams: int = 30,
    max_internal_dz_spread_m: float = 6.0,
    max_internal_dxy_spread_m: float = 8.0,
    max_spike_dz_m: float = 7.0,
    max_spike_dxy_m: float = 10.0,
    max_catastrophic_dz_m: float = 25.0,
    max_catastrophic_dxy_m: float = 30.0,
    max_neighbor_dz_jump_m: float = 6.0,
    max_neighbor_dxy_jump_m: float = 8.0,
    overlap_margin_m: float = 35.0,
    reoptimize: bool = True,
) -> dict[str, Any]:
    """Partition tie-graph islands into spatial backbone block, ICP bridge candidates, distant blocks, or quarantine.

    Selects the primary backbone block among sizable (``>= min_bridge_cams``), internally rigid
    islands using spatial coverage × sqrt(camera_count) so compact close-range bursts can never
    hijack wide-area scene backbones regardless of folder naming or photo counts, while using
    symmetric overlap checks so compact secondary blocks overlapping wider blocks (and vice versa)
    always enter Stage-4 Open3D ICP registration.
    """
    cameras = list(getattr(chunk, "cameras", None) or [])
    by_key = {c.key: c for c in cameras}
    islands, strong_adj = _build_tie_point_graph_and_islands(
        chunk,
        edge_min=edge_min,
        cut_weak_hinges=True,
        min_bridge_cams=min_bridge_cams,
    )
    if not islands:
        return {
            "main_island": [],
            "bridge_candidates": [],
            "kept_distant": [],
            "quarantined_keys": [],
            "spike_keys": [],
        }

    crs = getattr(chunk, "crs", None)
    metric_crs = _is_metric_crs(crs)
    unit_scale = crs_linear_unit_to_meters(crs) if metric_crs else 1.0

    # Compute chunk-wide shared grid step so unreferenced / LOCAL_CS islands are scored on a uniform scale
    shared_local_step: float | None = None
    all_cxs: list[float] = []
    all_cys: list[float] = []
    for comp in islands:
        for k in comp:
            cam = by_key.get(k)
            if cam is None or getattr(cam, "transform", None) is None:
                continue
            ctr = getattr(cam, "center", None)
            if ctr is None:
                continue
            cx = _parse_float(getattr(ctr, "x", None))
            cy = _parse_float(getattr(ctr, "y", None))
            if cx is not None and cy is not None:
                all_cxs.append(cx)
                all_cys.append(cy)
    if len(all_cxs) >= 2:
        s_max_all = max(
            _percentile(all_cxs, 0.95) - _percentile(all_cxs, 0.05),
            _percentile(all_cys, 0.95) - _percentile(all_cys, 0.05),
        )
        if s_max_all > 1e-3:
            shared_local_step = max(s_max_all / 10.0, 1e-3)

    rigidity = [
        _evaluate_island_rigidity(
            chunk,
            comp,
            by_key,
            max_internal_dz_spread_m=max_internal_dz_spread_m,
            max_internal_dxy_spread_m=max_internal_dxy_spread_m,
            strong_adj=strong_adj,
        )
        for comp in islands
    ]
    backbone_scores = [
        compute_block_backbone_score(
            [by_key[k] for k in comp if k in by_key],
            metric_crs=metric_crs,
            unit_to_m=unit_scale,
            shared_local_step=shared_local_step,
        )
        for comp in islands
    ]
    eligible_backbone = [
        i for i, comp in enumerate(islands) if len(comp) >= min_bridge_cams and rigidity[i]["is_rigid"]
    ]
    main_idx = (
        max(
            eligible_backbone,
            key=lambda i: (1 if backbone_scores[i][3] else 0, backbone_scores[i][0], len(islands[i])),
        )
        if eligible_backbone
        else 0
    )

    main_keys = list(islands[main_idx])
    main_cams = [by_key[k] for k in main_keys if k in by_key]
    main_bounds = _compute_main_xy_bounds(main_cams, metric_crs=metric_crs, unit_to_m=unit_scale)

    print(
        f"  [Tie-Graph Audit] Detected {len(islands)} tie-point island(s) (edge>={edge_min}): "
        f"{[len(x) for x in islands]}",
        flush=True,
    )
    if main_idx != 0:
        m_score, m_cells, m_cams, _ = backbone_scores[main_idx]
        c0_score, c0_cells, c0_cams, _ = backbone_scores[0]
        print(
            f"  [Tie-Graph Audit] Selected spatial backbone island #{main_idx + 1} "
            f"({m_cams} cams, {m_cells} cells, score={m_score:.1f}) over compact higher-count island "
            f"({c0_cams} cams, {c0_cells} cells, score={c0_score:.1f}).",
            flush=True,
        )

    bridge_candidates: list[list[int]] = []
    bridge_meta: list[tuple[list[Any], tuple[float, float, float, float, float, float] | None]] = []
    kept_distant: list[list[int]] = []
    deferred_quarantine: list[
        tuple[int, list[int], list[Any], tuple[float, float, float, float, float, float] | None, float]
    ] = []
    quarantined: set[int] = set()

    for idx, comp in enumerate(islands, 1):
        if idx - 1 == main_idx:
            continue
        rig = rigidity[idx - 1]
        int_dz_spread = rig["int_dz_spread"]
        int_dxy_spread = rig["int_dxy_spread"]
        p95_dz = rig["p95_dz"]
        p95_xy = rig["p95_xy"]
        max_dz = rig["max_dz"]
        max_xy = rig["max_xy"]
        is_rigid = rig["is_rigid"]

        comp_cams = [by_key[k] for k in comp if k in by_key]
        comp_bounds = _compute_main_xy_bounds(comp_cams, metric_crs=metric_crs, unit_to_m=unit_scale)
        overlap_ratio = _symmetric_bounds_overlap_ratio(
            main_cams,
            main_bounds,
            comp_cams,
            comp_bounds,
            metric_crs=metric_crs,
            overlap_margin_m=overlap_margin_m,
            unit_to_m=unit_scale,
        )

        if len(comp) >= min_bridge_cams and is_rigid and overlap_ratio >= 0.30:
            bridge_candidates.append(comp)
            bridge_meta.append((comp_cams, comp_bounds))
            print(
                f"    Island #{idx} ({len(comp)} cams | spread dZ={int_dz_spread:.1f}m, dXY={int_dxy_spread:.1f}m, "
                f"overlap={overlap_ratio * 100:.0f}%): queued for Stage-4 Open3D ICP Bridge.",
                flush=True,
            )
        elif (
            len(comp) >= min_distant_block_cams
            and is_rigid
            and overlap_ratio < 0.30
            and p95_dz <= 5.0
            and p95_xy <= 7.0
        ):
            kept_distant.append(comp)
            print(
                f"    Island #{idx} ({len(comp)} cams | p95|dZ|={p95_dz:.1f}m, p95_dXY={p95_xy:.1f}m, "
                f"overlap={overlap_ratio * 100:.0f}%): coherent non-overlapping compound — kept.",
                flush=True,
            )
        else:
            deferred_quarantine.append((idx, comp, comp_cams, comp_bounds, overlap_ratio))

    # 1-hop transitive overlap promotion: a rigid secondary block overlapping an accepted bridge candidate
    for idx, comp, comp_cams, comp_bounds, overlap_ratio in deferred_quarantine:
        rig = rigidity[idx - 1]
        promoted_transitive = False
        if bridge_meta and len(comp) >= min_bridge_cams and rig["is_rigid"]:
            for br_cams, br_bounds in bridge_meta:
                br_ov = _symmetric_bounds_overlap_ratio(
                    br_cams,
                    br_bounds,
                    comp_cams,
                    comp_bounds,
                    metric_crs=metric_crs,
                    overlap_margin_m=overlap_margin_m,
                    unit_to_m=unit_scale,
                )
                if br_ov >= 0.30:
                    promoted_transitive = True
                    bridge_candidates.append(comp)
                    print(
                        f"    Island #{idx} ({len(comp)} cams | transitive overlap={br_ov * 100:.0f}%): "
                        "queued for Stage-4 Open3D ICP Bridge.",
                        flush=True,
                    )
                    break
        if not promoted_transitive:
            quarantined.update(comp)
            print(
                f"    Island #{idx} ({len(comp)} cams | spread dZ={rig['int_dz_spread']:.1f}m, "
                f"max|dZ|={rig['max_dz']:.1f}m, max_dXY={rig['max_xy']:.1f}m, "
                f"overlap={overlap_ratio * 100:.0f}%): QUARANTINED.",
                flush=True,
            )

    # Check for genuine isolated GPS-spike cameras inside main_island (only if main_island has healthy GPS).
    # Require either a catastrophic teleport (>25m Z / >30m XY) OR an abrupt jump relative to strongly
    # connected visual neighbors so smooth multi-battery barometer/GPS drift (e.g. 7-11m across a 600m site)
    # never causes false quarantines on well-aligned flight lines.
    main_res: dict[int, tuple[float, float, float, float]] = {}
    for k in main_keys:
        vec = _compute_camera_gps_residual_vec(chunk, by_key[k])
        if vec is not None:
            main_res[k] = vec

    spike_keys: list[int] = []
    gps_only_disabled: set[int] = set()
    if len(main_res) >= 5:
        main_med_dz = _median([v[2] for v in main_res.values()])
        main_med_dxy = _median([v[3] for v in main_res.values()])
        if abs(main_med_dz) <= 3.5 and main_med_dxy <= 5.0:
            for k, (dx, dy, dz, dxy) in main_res.items():
                exceeds_global = abs(dz - main_med_dz) > max_spike_dz_m or dxy > max_spike_dxy_m
                if not exceeds_global:
                    continue
                is_catastrophic = abs(dz - main_med_dz) > max_catastrophic_dz_m or dxy > max_catastrophic_dxy_m
                nbr_vecs = [main_res[nb] for nb in strong_adj.get(k, ()) if nb in main_res]
                if nbr_vecs:
                    nbr_med_dx = _median([v[0] for v in nbr_vecs])
                    nbr_med_dy = _median([v[1] for v in nbr_vecs])
                    nbr_med_dz = _median([v[2] for v in nbr_vecs])
                    nbr_xy_jump = math.hypot(dx - nbr_med_dx, dy - nbr_med_dy)
                    if not is_catastrophic:
                        if abs(dz - nbr_med_dz) <= max_neighbor_dz_jump_m and nbr_xy_jump <= max_neighbor_dxy_jump_m:
                            continue
                    elif abs(dz - nbr_med_dz) <= 3.0 and nbr_xy_jump <= 5.0:
                        gps_only_disabled.add(k)
                        continue
                quarantined.add(k)
                spike_keys.append(k)
                print(
                    f"    Quarantining main-block GPS-spike camera {by_key[k].label} (dZ={dz:+.2f}m, dXY={dxy:.2f}m).",
                    flush=True,
                )

    for k in gps_only_disabled:
        cam = by_key[k]
        if getattr(cam, "reference", None) is not None:
            cam.reference.enabled = False
        print(
            f"    Disabling drifted GPS prior on visually consistent camera {cam.label} (pose kept).",
            flush=True,
        )

    for k in quarantined:
        cam = by_key[k]
        cam.transform = None
        cam.enabled = False
        if getattr(cam, "reference", None) is not None:
            cam.reference.enabled = False

    clean_main = [k for k in main_keys if k not in quarantined]

    # Only re-optimize here if no bridge candidates remain (when bridging, Stage 4 optimizes ch_main cleanly)
    if (quarantined or gps_only_disabled) and reoptimize and not bridge_candidates:
        try:
            optimize_cameras_no_k4(chunk, tiepoint_covariance=True)
        except Exception as exc:
            print(f"  [Tie-Graph Audit] Post-quarantine optimization note: {exc}", flush=True)

    print(
        f"  [Tie-Graph Audit] Summary: main={len(clean_main)}, bridge_candidates={len(bridge_candidates)}, "
        f"kept_distant={len(kept_distant)}, quarantined={len(quarantined)} (spikes={len(spike_keys)}).",
        flush=True,
    )
    return {
        "main_island": clean_main,
        "bridge_candidates": bridge_candidates,
        "kept_distant": kept_distant,
        "quarantined_keys": sorted(quarantined),
        "spike_keys": sorted(spike_keys),
        "gps_only_disabled_keys": sorted(gps_only_disabled),
    }


def _camera_physical_band(cam: Any, vert_unit_m: float = 1.0) -> tuple[Any, int, int, str]:
    """Return folder- and timestamp-agnostic physical flight signature ``(sensor_id, alt_15m, z0_2m, alt_frame)``.

    Separating ``alt_frame`` (``'agl'`` vs ``'msl'``) ensures DJI RelativeAltitude flights and
    non-DJI MSL cameras are never collapsed into a single physical flight band.
    """
    sensor_id = _camera_sensor_id(cam)
    photo = getattr(cam, "photo", None)
    meta = getattr(photo, "meta", None) if photo else None
    ra = _parse_float(get_camera_meta(meta, "DJI/RelativeAltitude"))
    aa = _parse_float(get_camera_meta(meta, "DJI/AbsoluteAltitude"))
    if ra is not None:
        alt_band = int(round(ra / 15.0))
        alt_frame = "agl"
    else:
        ref = getattr(cam, "reference", None)
        loc = getattr(ref, "location", None) if ref is not None else None
        zv = _parse_float(getattr(loc, "z", None)) if loc is not None else None
        alt_m = zv * vert_unit_m if zv is not None else None
        alt_band = int(round(alt_m / 15.0)) if alt_m is not None else 0
        alt_frame = "msl" if alt_m is not None else "none"
    z0_band = int(round((aa - ra) / 2.0)) if (aa is not None and ra is not None) else 0
    return (sensor_id, alt_band, z0_band, alt_frame)


def find_unaligned_overlapping_groups(
    chunk: Any,
    main_keys: set[int],
    excluded_keys: set[int] | None = None,
    min_cams: int = 12,
    overlap_margin_m: float = 35.0,
    cluster_radius_m: float = 80.0,
    max_folder_aligned_ratio: float = 0.35,
    min_motion_xy_m: float = 3.0,
    min_motion_z_m: float = 2.0,
) -> list[list[int]]:
    """Group enabled unaligned cameras from unaligned flights overlapping the main island for Stage-4 ICP.

    Completely folder- and timestamp-agnostic: preserves partial-flight dropouts with >= 20
    unaligned frames and moving walkarounds while rejecting stationary landing-pad stacks.
    """
    cameras = list(getattr(chunk, "cameras", None) or [])
    by_key = {c.key: c for c in cameras}
    excl = excluded_keys or set()

    crs = getattr(chunk, "crs", None)
    metric_crs = _is_metric_crs(crs)
    unit_scale = crs_linear_unit_to_meters(crs) if metric_crs else 1.0
    vert_unit_m = crs_vertical_unit_to_meters(crs)
    main_cams = [by_key[k] for k in main_keys if k in by_key]
    main_bounds = _compute_main_xy_bounds(main_cams, metric_crs=metric_crs, unit_to_m=unit_scale)
    if main_bounds is None:
        return []
    ref_lon, ref_lat, min_x, max_x, min_y, max_y = main_bounds

    band_total: dict[tuple[Any, int, int, str], int] = defaultdict(int)
    band_in_main: dict[tuple[Any, int, int, str], int] = defaultdict(int)
    for cam in cameras:
        if not getattr(cam, "enabled", True):
            continue
        band = _camera_physical_band(cam, vert_unit_m=vert_unit_m)
        band_total[band] += 1
        if cam.key in main_keys:
            band_in_main[band] += 1

    main_pts_xy: list[tuple[float, float]] = []
    for cam in main_cams:
        xy_m = _cam_xy_meters(cam, ref_lon, ref_lat, metric_crs=metric_crs, unit_to_m=unit_scale)
        if xy_m is not None:
            main_pts_xy.append(xy_m)

    raw_unaligned: list[tuple[int, float, float, tuple[float, str] | None, float | None]] = []
    for cam in cameras:
        if cam.key in main_keys or cam.key in excl:
            continue
        if not getattr(cam, "enabled", True) or getattr(cam, "transform", None) is not None:
            continue
        band = _camera_physical_band(cam, vert_unit_m=vert_unit_m)
        b_tot = band_total.get(band, 0)
        b_al = band_in_main.get(band, 0)
        b_unal = b_tot - b_al
        if b_tot > 0 and (b_al / b_tot) >= max_folder_aligned_ratio and b_unal < max(min_cams, 20):
            continue
        xy = _cam_xy_meters(cam, ref_lon, ref_lat, metric_crs=metric_crs, unit_to_m=unit_scale)
        if xy is None:
            continue
        ref = getattr(cam, "reference", None)
        loc = getattr(ref, "location", None) if ref is not None else None
        raw_z = _parse_float(getattr(loc, "z", None)) if loc is not None else None
        msl_z = raw_z * vert_unit_m if raw_z is not None else None
        raw_unaligned.append(
            (cam.key, xy[0], xy[1], _camera_altitude_with_frame(cam, vert_unit_m=vert_unit_m), msl_z)
        )

    if len(raw_unaligned) < min_cams:
        return []

    combined_xy = main_pts_xy + [(item[1], item[2]) for item in raw_unaligned]
    inlier_mask = _inlier_spatial_mask(combined_xy, max_jump_m=5000.0, keep_secondary_components=True)
    n_main_pts = len(main_pts_xy)
    unaligned_xy = [item for idx, item in enumerate(raw_unaligned) if inlier_mask[n_main_pts + idx]]

    if len(unaligned_xy) < min_cams:
        return []

    eff_cluster_radius_m = float(cluster_radius_m)
    if abs(eff_cluster_radius_m - 80.0) <= 1e-6 and len(unaligned_xy) >= min_cams:
        sample_xy = [(item[1], item[2]) for item in unaligned_xy[:400]]
        nn_dists: list[float] = []
        for idx_s, (sx, sy) in enumerate(sample_xy):
            best_d2 = float("inf")
            for idx_o, (ox, oy) in enumerate(sample_xy):
                if idx_o == idx_s:
                    continue
                d2 = (sx - ox) * (sx - ox) + (sy - oy) * (sy - oy)
                if 1e-4 < d2 < best_d2:
                    best_d2 = d2
            if math.isfinite(best_d2):
                nn_dists.append(math.sqrt(best_d2))
        if nn_dists:
            med_nn = _median(nn_dists)
            if med_nn > 40.0:
                eff_cluster_radius_m = max(80.0, min(300.0, 2.5 * med_nn))

    def _vertical_compatible(
        u_alt: tuple[float, str] | None,
        u_msl: float | None,
        v_alt: tuple[float, str] | None,
        v_msl: float | None,
    ) -> bool:
        if u_alt is None or v_alt is None:
            return True
        uz, u_frame = u_alt
        vz, v_frame = v_alt
        if u_frame == v_frame:
            if u_frame == "agl":
                if abs(uz - vz) <= 25.0:
                    return True
                if uz >= 20.0 and vz >= 20.0:
                    return (max(uz, vz) / min(uz, vz)) <= 2.2
                return False
            return abs(uz - vz) <= 35.0
        if u_msl is not None and v_msl is not None:
            return abs(u_msl - v_msl) <= 35.0
        return True

    # Cluster unaligned cameras via O(N) 2D spatial hash grid BEFORE overlap gating
    cell_sz = max(1.0, eff_cluster_radius_m)
    grid_buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for idx, (_k, cx, cy, _alt, _msl) in enumerate(unaligned_xy):
        grid_buckets[(int(math.floor(cx / cell_sz)), int(math.floor(cy / cell_sz)))].append(idx)

    visited: set[int] = set()
    groups: list[list[int]] = []
    r2 = eff_cluster_radius_m * eff_cluster_radius_m
    for idx in range(len(unaligned_xy)):
        if idx in visited:
            continue
        comp_indices: list[int] = []
        queue = deque([idx])
        visited.add(idx)
        while queue:
            u = queue.popleft()
            _uk, ux, uy, u_alt, u_msl = unaligned_xy[u]
            comp_indices.append(u)
            gcx = int(math.floor(ux / cell_sz))
            gcy = int(math.floor(uy / cell_sz))
            for dcx in (-1, 0, 1):
                for dcy in (-1, 0, 1):
                    for v in grid_buckets.get((gcx + dcx, gcy + dcy), ()):
                        if v in visited:
                            continue
                        _vk, vx, vy, v_alt, v_msl = unaligned_xy[v]
                        dx = ux - vx
                        dy = uy - vy
                        if (dx * dx + dy * dy) <= r2 and _vertical_compatible(u_alt, u_msl, v_alt, v_msl):
                            visited.add(v)
                            queue.append(v)
        if len(comp_indices) >= min_cams:
            xs = [unaligned_xy[i][1] for i in comp_indices]
            ys = [unaligned_xy[i][2] for i in comp_indices]
            zs = [unaligned_xy[i][3][0] for i in comp_indices if unaligned_xy[i][3] is not None]
            c_min_x, c_max_x = min(xs), max(xs)
            c_min_y, c_max_y = min(ys), max(ys)
            xy_span = math.hypot(c_max_x - c_min_x, c_max_y - c_min_y)
            z_span = (max(zs) - min(zs)) if len(zs) >= 2 else 0.0
            if xy_span < min_motion_xy_m and z_span < min_motion_z_m:
                continue

            in_main = sum(
                1
                for i in comp_indices
                if (min_x - overlap_margin_m) <= unaligned_xy[i][1] <= (max_x + overlap_margin_m)
                and (min_y - overlap_margin_m) <= unaligned_xy[i][2] <= (max_y + overlap_margin_m)
            )
            frac_cluster_in_main = in_main / len(comp_indices)
            main_in_cluster = (
                sum(
                    1
                    for mx, my in main_pts_xy
                    if (c_min_x - overlap_margin_m) <= mx <= (c_max_x + overlap_margin_m)
                    and (c_min_y - overlap_margin_m) <= my <= (c_max_y + overlap_margin_m)
                )
                if main_pts_xy
                else 0
            )
            frac_main_in_cluster = (main_in_cluster / len(main_pts_xy)) if main_pts_xy else 0.0
            if in_main >= min_cams or frac_cluster_in_main >= 0.10 or frac_main_in_cluster >= 0.30:
                groups.append([unaligned_xy[i][0] for i in comp_indices])

    groups.sort(
        key=lambda grp: (
            compute_block_backbone_score(
                [by_key[k] for k in grp if k in by_key], metric_crs=metric_crs, unit_to_m=unit_scale
            )[0],
            len(grp),
        ),
        reverse=True,
    )
    return groups
