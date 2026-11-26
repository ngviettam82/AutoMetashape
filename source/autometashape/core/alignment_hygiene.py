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

import math
import os
from collections import defaultdict, deque
from typing import Any

from autometashape.core.telemetry import calculate_haversine_meters, is_projected_crs

try:
    import Metashape
except ImportError:
    Metashape = None

_RTK_FIXED_FLAGS = {"50", "50.0"}


def _is_metric_crs(crs: Any) -> bool:
    """Return True if ``crs`` uses linear meter coordinates (projected CRS or LOCAL_CS) rather than degrees."""
    if crs is None:
        return False
    if is_projected_crs(crs):
        return True
    wkt = str(getattr(crs, "wkt", "") or "").upper()
    return "LOCAL_CS" in wkt


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
    """Reset unstable higher-order lens parameters (k4, b1, b2) to zero on all chunk sensors.

    Prevents severe corner distortion, radial dome warping, and edge tearing observed on consumer
    UAV wide-angle lenses when adaptive_fitting or fit_k4 is enabled.
    """
    sensors = getattr(chunk, "sensors", None) or []
    reset_count = 0
    for sensor in sensors:
        cal = getattr(sensor, "calibration", None)
        if cal is None:
            continue
        try:
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


def sanitize_camera_gps(chunk: Any, max_jump_m: float = 5000.0) -> dict[str, int]:
    """Disable Null-Island (0, 0), non-finite, and > max_jump_m GPS outlier references.

    Camera photos remain enabled for visual matching; only their corrupted GPS prior is disabled.
    Handles both geographic (WGS84 degrees) and projected/local (meter) coordinate systems cleanly.
    """
    cameras = getattr(chunk, "cameras", None) or []
    crs = getattr(chunk, "crs", None)
    metric_crs = _is_metric_crs(crs)
    null_disabled = 0
    outlier_disabled = 0
    valid_cams: list[tuple[Any, float, float]] = []

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
        if metric_crs:
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
            valid_cams.append((cam, x_val, y_val))

    if len(valid_cams) >= 3 and max_jump_m > 0.0:
        med_x = _median([x[1] for x in valid_cams])
        med_y = _median([x[2] for x in valid_cams])
        kept: list[tuple[Any, float, float]] = []
        for cam, xv, yv in valid_cams:
            dist_m = (
                math.hypot(xv - med_x, yv - med_y) if metric_crs else calculate_haversine_meters(med_y, med_x, yv, xv)
            )
            if dist_m > max_jump_m:
                cam.reference.enabled = False
                outlier_disabled += 1
            else:
                kept.append((cam, xv, yv))
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


def _is_rtk_fixed_survey(cameras: list[Any]) -> bool:
    """Return True if cameras represent an RTK/PPK high-precision survey (DJI or vendor-agnostic)."""
    rtk_tagged = 0
    rtk_fixed = 0
    high_prec_acc = 0
    acc_checked = 0
    for cam in cameras:
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
                if ax <= 0.5 and az <= 1.0:
                    high_prec_acc += 1
    if rtk_tagged >= 5 and (rtk_fixed / rtk_tagged) >= 0.5:
        return True
    if acc_checked >= 5 and (high_prec_acc / acc_checked) >= 0.5:
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


def _dominant_display_label(cams: list[Any]) -> str:
    """Return a concise human-readable label for logging only (never used for grouping/anchoring)."""
    counts: dict[str, int] = defaultdict(int)
    for cam in cams:
        parent_dir = _camera_session_id(cam)[0]
        counts[_session_display_label(parent_dir)] += 1
    if not counts:
        return "flight"
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


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
    Follows Agisoft's official ``Z = anchor_z0 + RelativeAltitude`` workflow per camera (100%
    idempotent, zero reliance on folder structure, timestamps, or EXIF body serial numbers), so
    even two simultaneous drones mixed into one flat directory align cleanly to one datum.
    """
    cameras = list(getattr(chunk, "cameras", None) or [])
    if not cameras:
        return {"skipped": True, "reason": "no_cameras", "shifts": {}}

    if _is_rtk_fixed_survey(cameras):
        print(
            "  [Alignment Hygiene] RTK Fixed survey detected — preserving native RTK altitudes and accuracy.",
            flush=True,
        )
        return {"skipped": True, "reason": "rtk_fixed", "shifts": {}}

    # Collect (cam, aa, ra, z0) strictly from EXIF/XMP physics (no folder/time assumptions)
    z0_entries: list[tuple[Any, float, float, float]] = []
    for cam in cameras:
        ref = getattr(cam, "reference", None)
        if ref is not None and not getattr(ref, "enabled", True):
            continue
        photo = getattr(cam, "photo", None)
        meta = getattr(photo, "meta", None) if photo else None
        aa = _parse_float(get_camera_meta(meta, "DJI/AbsoluteAltitude"))
        ra = _parse_float(get_camera_meta(meta, "DJI/RelativeAltitude"))
        if aa is not None and ra is not None:
            z0_entries.append((cam, aa, ra, aa - ra))

    shifts: dict[str, float] = {}
    anchor_label = ""
    ref_z0 = anchor_z0_override

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

        if ref_z0 is None:
            # Pure physics anchor selection: highest aerial RelativeAltitude band (>=25m preferred),
            # breaking ties within 5m altitude bands by largest camera count (never reads folder names).
            high_alt = [cs for cs in cluster_stats if cs["med_ra"] >= 25.0]
            pool = high_alt if high_alt else cluster_stats
            anchor_cs = max(pool, key=lambda cs: (round(cs["med_ra"] / 5.0), cs["count"]))
            ref_z0 = anchor_cs["med_z0"]
            anchor_label = anchor_cs["label"]
        else:
            anchor_label = "external_anchor"

        shifted_cams = 0
        for idx, cs in enumerate(cluster_stats, 1):
            dz_med = ref_z0 - cs["med_z0"]
            key_label = f"{cs['label']}[z0={cs['med_z0']:.1f}m]"
            if key_label in shifts:
                key_label = f"{cs['label']}#{idx}[z0={cs['med_z0']:.1f}m]"
            shifts[key_label] = dz_med

        # Apply exact idempotent per-camera normalization: Z_new = ref_z0 + RelativeAltitude
        for cam, _aa, ra, _z0 in z0_entries:
            ref = getattr(cam, "reference", None)
            if ref is None or not getattr(ref, "enabled", True) or getattr(ref, "location", None) is None:
                continue
            loc = ref.location
            target_z = ref_z0 + ra
            if abs(float(loc.z) - target_z) > 0.01:
                if Metashape is not None and hasattr(Metashape, "Vector"):
                    ref.location = Metashape.Vector([loc.x, loc.y, target_z])
                else:
                    loc.z = target_z
                shifted_cams += 1

        max_shift = max((abs(v) for v in shifts.values()), default=0.0)
        if len(shifts) > 1 or max_shift > 0.05:
            summary = ", ".join(f"{k}:{v:+.2f}m" for k, v in sorted(shifts.items()))
            print(
                f"  [Alignment Hygiene] Normalized TakeoffZ0 onto '{anchor_label}' ({ref_z0:.2f}m, "
                f"shifted {shifted_cams} cam(s)): {summary}",
                flush=True,
            )

    if set_anisotropic_accuracy:
        try:
            if Metashape is not None and hasattr(Metashape, "Vector"):
                chunk.camera_location_accuracy = Metashape.Vector([xy_accuracy_m, xy_accuracy_m, z_accuracy_m])
            else:
                chunk.camera_location_accuracy = (xy_accuracy_m, xy_accuracy_m, z_accuracy_m)
        except Exception:
            pass

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
        dz = float(est.z - ref_loc.z)
        if _is_metric_crs(crs):
            dx = float(est.x - ref_loc.x)
            dy = float(est.y - ref_loc.y)
        else:
            dy = float(est.y - ref_loc.y) * 111320.0
            dx = float(est.x - ref_loc.x) * 111320.0 * math.cos(math.radians(float(ref_loc.y)))
        return dx, dy, dz, math.hypot(dx, dy)
    except Exception:
        return None


def compute_camera_gps_residual(chunk: Any, camera: Any) -> tuple[float, float] | None:
    """Return ``(dZ_m, dXY_m)`` between estimated camera position and GPS reference, or ``None``."""
    vec = _compute_camera_gps_residual_vec(chunk, camera)
    if vec is None:
        return None
    return vec[2], vec[3]


def _camera_altitude_m(cam: Any) -> float | None:
    """Return camera RelativeAltitude (preferred) or reference Z elevation in meters."""
    photo = getattr(cam, "photo", None)
    meta = getattr(photo, "meta", None) if photo else None
    ra = _parse_float(get_camera_meta(meta, "DJI/RelativeAltitude"))
    if ra is not None:
        return ra
    ref = getattr(cam, "reference", None)
    loc = getattr(ref, "location", None) if ref is not None else None
    return _parse_float(getattr(loc, "z", None)) if loc is not None else None


def _camera_focal_px(cam: Any) -> float | None:
    """Return calibrated focal length in pixels for zoom-ratio comparison, if available."""
    sensor = getattr(cam, "sensor", None)
    if sensor is None:
        return None
    cal = getattr(sensor, "calibration", None)
    f_val = _parse_float(getattr(cal, "f", None)) if cal is not None else None
    if f_val is not None and f_val > 1.0:
        return f_val
    fl_mm = _parse_float(getattr(sensor, "focal_length", None))
    if fl_mm is not None and fl_mm > 0.1:
        return fl_mm
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
    gps_jump_m: float = 1.5,
) -> str | None:
    """Return discontinuity reason if two sizable sub-blocks across a 1-bridge differ physically."""
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

    # 3. Altitude regime jump (e.g. 75m aerial vs 10m close-range)
    za = [v for k in side_a if (v := _camera_altitude_m(by_key[k])) is not None]
    zb = [v for k in side_b if (v := _camera_altitude_m(by_key[k])) is not None]
    if za and zb:
        dz_alt = abs(_median(za) - _median(zb))
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
                    # Preserve dense close-range / monument connectivity in O(n_k) without O(n_k^2) blowup
                    ordered = sorted(ckeys)
                    window = min(6, n_k - 1)
                    for i in range(n_k):
                        ki = ordered[i]
                        adj_i = adj[ki]
                        for step in range(1, window + 1):
                            kj = ordered[(i + step) % n_k]
                            if kj != ki:
                                adj_i[kj] += 1
                                adj[kj][ki] += 1

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


def _cam_xy_meters(cam: Any, ref_x: float, ref_y: float, metric_crs: bool = False) -> tuple[float, float] | None:
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
        return (xv - ref_x, yv - ref_y)
    if abs(xv) < 1e-4 and abs(yv) < 1e-4:
        return None
    cos_lat = math.cos(math.radians(ref_y))
    return ((xv - ref_x) * 111320.0 * cos_lat, (yv - ref_y) * 111320.0)


def _compute_main_xy_bounds(
    cams: list[Any],
    metric_crs: bool = False,
) -> tuple[float, float, float, float, float, float] | None:
    """Return ``(ref_x, ref_y, min_x, max_x, min_y, max_y)`` for valid referenced cameras."""
    coords: list[tuple[float, float]] = []
    for cam in cams:
        ref = getattr(cam, "reference", None)
        if ref is None or not getattr(ref, "enabled", True) or getattr(ref, "location", None) is None:
            continue
        xv = _parse_float(getattr(ref.location, "x", None))
        yv = _parse_float(getattr(ref.location, "y", None))
        if xv is not None and yv is not None and (metric_crs or not (abs(xv) < 1e-4 and abs(yv) < 1e-4)):
            coords.append((xv, yv))
    if len(coords) < 2:
        return None
    ref_x = _median([c[0] for c in coords])
    ref_y = _median([c[1] for c in coords])
    if metric_crs:
        xs = [x - ref_x for x, _ in coords]
        ys = [y - ref_y for _, y in coords]
    else:
        cos_lat = math.cos(math.radians(ref_y))
        xs = [(lon - ref_x) * 111320.0 * cos_lat for lon, _ in coords]
        ys = [(lat - ref_y) * 111320.0 for _, lat in coords]
    return (ref_x, ref_y, min(xs), max(xs), min(ys), max(ys))


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
    """Partition tie-graph islands into main block, ICP bridge candidates, distant blocks, or rogue quarantine.

    Quarantines detached tiny/folded rogue islands and genuine isolated GPS-spike cameras inside the
    main block (verified against visual tie-graph neighbors so smooth multi-battery consumer GPS or
    barometer drift is never falsely quarantined), while preserving coherent overlapping secondary
    islands for Stage-4 Zero-Neural Open3D ICP registration.
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

    metric_crs = _is_metric_crs(getattr(chunk, "crs", None))
    main_keys = list(islands[0])
    main_cams = [by_key[k] for k in main_keys if k in by_key]
    main_bounds = _compute_main_xy_bounds(main_cams, metric_crs=metric_crs)

    print(
        f"  [Tie-Graph Audit] Detected {len(islands)} tie-point island(s) (edge>={edge_min}): "
        f"{[len(x) for x in islands]}",
        flush=True,
    )

    bridge_candidates: list[list[int]] = []
    kept_distant: list[list[int]] = []
    quarantined: set[int] = set()

    for idx, comp in enumerate(islands[1:], 2):
        dzs: list[float] = []
        dxys: list[float] = []
        for k in comp:
            res = compute_camera_gps_residual(chunk, by_key[k])
            if res is not None:
                dzs.append(res[0])
                dxys.append(res[1])

        if dzs:
            med_dz = _median(dzs)
            med_dxy = _median(dxys)
            int_dz_spread = _percentile([abs(v - med_dz) for v in dzs], 0.90)
            int_dxy_spread = _percentile([abs(v - med_dxy) for v in dxys], 0.90)
            max_dz = max(abs(v) for v in dzs)
            max_xy = max(dxys)
        else:
            int_dz_spread = 0.0
            int_dxy_spread = 0.0
            max_dz = 0.0
            max_xy = 0.0

        is_rigid = int_dz_spread <= max_internal_dz_spread_m and int_dxy_spread <= max_internal_dxy_spread_m

        overlap_ratio = 0.0
        if main_bounds is not None:
            ref_lon, ref_lat, min_x, max_x, min_y, max_y = main_bounds
            gps_checked = 0
            gps_inside = 0
            for k in comp:
                xy = _cam_xy_meters(by_key[k], ref_lon, ref_lat, metric_crs=metric_crs)
                if xy is None:
                    continue
                gps_checked += 1
                if (min_x - overlap_margin_m) <= xy[0] <= (max_x + overlap_margin_m) and (
                    min_y - overlap_margin_m
                ) <= xy[1] <= (max_y + overlap_margin_m):
                    gps_inside += 1
            overlap_ratio = (gps_inside / gps_checked) if gps_checked > 0 else 1.0
        else:
            # No GPS available — assume potential overlap for sizable islands
            overlap_ratio = 1.0

        if len(comp) >= min_bridge_cams and is_rigid and overlap_ratio >= 0.30:
            bridge_candidates.append(comp)
            print(
                f"    Island #{idx} ({len(comp)} cams | spread dZ={int_dz_spread:.1f}m, dXY={int_dxy_spread:.1f}m, "
                f"overlap={overlap_ratio * 100:.0f}%): queued for Stage-4 Open3D ICP Bridge.",
                flush=True,
            )
        elif (
            len(comp) >= min_distant_block_cams
            and is_rigid
            and overlap_ratio < 0.30
            and max_dz <= 5.0
            and max_xy <= 7.0
        ):
            kept_distant.append(comp)
            print(
                f"    Island #{idx} ({len(comp)} cams | max|dZ|={max_dz:.1f}m, max_dXY={max_xy:.1f}m, "
                f"overlap={overlap_ratio * 100:.0f}%): coherent non-overlapping compound — kept.",
                flush=True,
            )
        else:
            quarantined.update(comp)
            print(
                f"    Island #{idx} ({len(comp)} cams | spread dZ={int_dz_spread:.1f}m, max|dZ|={max_dz:.1f}m, "
                f"max_dXY={max_xy:.1f}m, overlap={overlap_ratio * 100:.0f}%): QUARANTINED.",
                flush=True,
            )

    # Check for genuine isolated GPS-spike cameras inside main_island (only if main_island has healthy GPS).
    # Require either a catastrophic teleport (>25m Z / >30m XY) OR an abrupt jump relative to strongly
    # connected visual neighbors so smooth multi-battery barometer/GPS drift (e.g. 7-11m across a 600m site)
    # never causes false quarantines on well-aligned flight lines.
    main_res: dict[int, tuple[float, float]] = {}
    for k in main_keys:
        res = compute_camera_gps_residual(chunk, by_key[k])
        if res is not None:
            main_res[k] = res

    spike_keys: list[int] = []
    if len(main_res) >= 5:
        main_med_dz = _median([v[0] for v in main_res.values()])
        main_med_dxy = _median([v[1] for v in main_res.values()])
        if abs(main_med_dz) <= 3.5 and main_med_dxy <= 5.0:
            for k, (dz, dxy) in main_res.items():
                exceeds_global = abs(dz - main_med_dz) > max_spike_dz_m or dxy > max_spike_dxy_m
                if not exceeds_global:
                    continue
                is_catastrophic = abs(dz - main_med_dz) > max_catastrophic_dz_m or dxy > max_catastrophic_dxy_m
                if not is_catastrophic:
                    nbr_res = [main_res[nb] for nb in strong_adj.get(k, ()) if nb in main_res]
                    if nbr_res:
                        nbr_med_dz = _median([v[0] for v in nbr_res])
                        nbr_med_dxy = _median([v[1] for v in nbr_res])
                        if (
                            abs(dz - nbr_med_dz) <= max_neighbor_dz_jump_m
                            and abs(dxy - nbr_med_dxy) <= max_neighbor_dxy_jump_m
                        ):
                            continue
                quarantined.add(k)
                spike_keys.append(k)
                print(
                    f"    Quarantining main-block GPS-spike camera {by_key[k].label} (dZ={dz:+.2f}m, dXY={dxy:.2f}m).",
                    flush=True,
                )

    for k in quarantined:
        cam = by_key[k]
        cam.transform = None
        cam.enabled = False

    clean_main = [k for k in main_keys if k not in quarantined]

    # Only re-optimize here if no bridge candidates remain (when bridging, Stage 4 optimizes ch_main cleanly)
    if quarantined and reoptimize and not bridge_candidates:
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
    }


def _camera_physical_band(cam: Any) -> tuple[Any, int, int]:
    """Return folder- and timestamp-agnostic physical flight signature ``(sensor_id, alt_15m, z0_2m)``.

    Metashape Pro natively splits distinct zoom levels (e.g. 1x vs 2x ``DigitalZoomRatio``) and
    distinct drone hardware into separate ``cam.sensor`` groups at ``addPhotos()``. Combining
    ``sensor_id`` with quantized flight altitude and barometer datum ``z0`` cleanly separates
    unaligned close-range/different-zoom flights from aligned aerial flights even inside a flat
    mixed directory with zero time delay.
    """
    sensor_id = _camera_sensor_id(cam)
    photo = getattr(cam, "photo", None)
    meta = getattr(photo, "meta", None) if photo else None
    ra = _parse_float(get_camera_meta(meta, "DJI/RelativeAltitude"))
    aa = _parse_float(get_camera_meta(meta, "DJI/AbsoluteAltitude"))
    if ra is not None:
        alt_band = int(round(ra / 15.0))
    else:
        ref = getattr(cam, "reference", None)
        loc = getattr(ref, "location", None) if ref is not None else None
        zv = _parse_float(getattr(loc, "z", None)) if loc is not None else None
        alt_band = int(round(zv / 15.0)) if zv is not None else 0
    z0_band = int(round((aa - ra) / 2.0)) if (aa is not None and ra is not None) else 0
    return (sensor_id, alt_band, z0_band)


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

    Completely folder- and timestamp-agnostic: filters by physical flight band
    ``(sensor_id, altitude_band_15m, z0_cluster_2m)`` aligned ratio (skipping scattered edge
    dropouts from already-aligned flight bands) and rejects stationary landing-pad/tripod stacks
    lacking real flight motion (``xy_span < min_motion_xy_m`` and ``z_span < min_motion_z_m``).
    """
    cameras = list(getattr(chunk, "cameras", None) or [])
    by_key = {c.key: c for c in cameras}
    excl = excluded_keys or set()

    metric_crs = _is_metric_crs(getattr(chunk, "crs", None))
    main_cams = [by_key[k] for k in main_keys if k in by_key]
    main_bounds = _compute_main_xy_bounds(main_cams, metric_crs=metric_crs)
    if main_bounds is None:
        return []
    ref_lon, ref_lat, min_x, max_x, min_y, max_y = main_bounds

    band_total: dict[tuple[Any, int, int], int] = defaultdict(int)
    band_in_main: dict[tuple[Any, int, int], int] = defaultdict(int)
    for cam in cameras:
        if not getattr(cam, "enabled", True):
            continue
        band = _camera_physical_band(cam)
        band_total[band] += 1
        if cam.key in main_keys:
            band_in_main[band] += 1

    unaligned_xy: list[tuple[int, float, float, float | None]] = []
    for cam in cameras:
        if cam.key in main_keys or cam.key in excl:
            continue
        if not getattr(cam, "enabled", True) or getattr(cam, "transform", None) is not None:
            continue
        # Skip ground-idle / landing-pad frames (DJI RelativeAltitude <= 0.5m) before clustering
        # so stationary takeoff/landing shots can never contaminate nearby close-range flights.
        photo = getattr(cam, "photo", None)
        meta = getattr(photo, "meta", None) if photo else None
        ra_val = _parse_float(get_camera_meta(meta, "DJI/RelativeAltitude"))
        if ra_val is not None and ra_val <= 0.5:
            continue
        band = _camera_physical_band(cam)
        b_tot = band_total.get(band, 0)
        if b_tot > 0 and (band_in_main.get(band, 0) / b_tot) >= max_folder_aligned_ratio:
            continue
        xy = _cam_xy_meters(cam, ref_lon, ref_lat, metric_crs=metric_crs)
        if xy is None:
            continue
        if (min_x - overlap_margin_m) <= xy[0] <= (max_x + overlap_margin_m) and (min_y - overlap_margin_m) <= xy[
            1
        ] <= (max_y + overlap_margin_m):
            unaligned_xy.append((cam.key, xy[0], xy[1], _camera_altitude_m(cam)))

    if len(unaligned_xy) < min_cams:
        return []

    # Connected-component spatial clustering within cluster_radius_m and 25m vertical continuity
    visited: set[int] = set()
    groups: list[list[int]] = []
    r2 = cluster_radius_m * cluster_radius_m
    for idx in range(len(unaligned_xy)):
        if idx in visited:
            continue
        comp_indices: list[int] = []
        queue = deque([idx])
        visited.add(idx)
        while queue:
            u = queue.popleft()
            _uk, ux, uy, uz = unaligned_xy[u]
            comp_indices.append(u)
            for v in range(len(unaligned_xy)):
                if v in visited:
                    continue
                _vk, vx, vy, vz = unaligned_xy[v]
                dx = ux - vx
                dy = uy - vy
                if (dx * dx + dy * dy) <= r2 and (uz is None or vz is None or abs(uz - vz) <= 25.0):
                    visited.add(v)
                    queue.append(v)
        if len(comp_indices) >= min_cams:
            xs = [unaligned_xy[i][1] for i in comp_indices]
            ys = [unaligned_xy[i][2] for i in comp_indices]
            zs = [unaligned_xy[i][3] for i in comp_indices if unaligned_xy[i][3] is not None]
            xy_span = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
            z_span = (max(zs) - min(zs)) if len(zs) >= 2 else 0.0
            if xy_span < min_motion_xy_m and z_span < min_motion_z_m:
                continue
            groups.append([unaligned_xy[i][0] for i in comp_indices])

    groups.sort(key=len, reverse=True)
    return groups
