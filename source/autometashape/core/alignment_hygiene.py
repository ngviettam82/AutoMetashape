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

from autometashape.core.telemetry import calculate_haversine_meters

try:
    import Metashape
except ImportError:
    Metashape = None

_RTK_FIXED_FLAGS = {"50", "50.0"}


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
    """
    cameras = getattr(chunk, "cameras", None) or []
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
        lon = _parse_float(getattr(loc, "x", None))
        lat = _parse_float(getattr(loc, "y", None))
        alt = _parse_float(getattr(loc, "z", None))
        is_invalid = (
            lon is None
            or lat is None
            or alt is None
            or (abs(lon) < 1e-4 and abs(lat) < 1e-4)
            or abs(lon) > 180.0
            or abs(lat) > 90.0
        )
        if is_invalid:
            ref.enabled = False
            null_disabled += 1
        else:
            valid_cams.append((cam, lon, lat))

    if len(valid_cams) >= 3 and max_jump_m > 0.0:
        med_lon = _median([x[1] for x in valid_cams])
        med_lat = _median([x[2] for x in valid_cams])
        kept: list[tuple[Any, float, float]] = []
        for cam, lon, lat in valid_cams:
            dist_m = calculate_haversine_meters(med_lat, med_lon, lat, lon)
            if dist_m > max_jump_m:
                cam.reference.enabled = False
                outlier_disabled += 1
            else:
                kept.append((cam, lon, lat))
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
    """Return True if at least half of RTK-tagged cameras report RTK Fixed (flag 50)."""
    rtk_tagged = 0
    rtk_fixed = 0
    for cam in cameras:
        photo = getattr(cam, "photo", None)
        meta = getattr(photo, "meta", None) if photo else None
        flag = get_camera_meta(meta, "DJI/RtkFlag")
        if flag is not None:
            rtk_tagged += 1
            if str(flag).strip() in _RTK_FIXED_FLAGS:
                rtk_fixed += 1
    return rtk_tagged >= 5 and (rtk_fixed / rtk_tagged) >= 0.5


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


def normalize_flight_takeoff_z0(
    chunk: Any,
    set_anisotropic_accuracy: bool = True,
    xy_accuracy_m: float = 5.0,
    z_accuracy_m: float = 15.0,
    anchor_z0_override: float | None = None,
) -> dict[str, Any]:
    """Normalize per-flight consumer barometer datum shifts onto the primary Mission datum.

    For non-RTK DJI/consumer UAVs, AbsoluteAltitude suffers 5-40 m barometer drift between
    batteries/days while RelativeAltitude stays accurate within a flight. Shifting each flight
    session by ``ref_z0 - median(AbsoluteAltitude - RelativeAltitude)`` aligns all flights onto a
    single vertical datum before matching and bundle adjustment.
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

    z0_by_session: dict[tuple[str, str], list[float]] = defaultdict(list)
    rel_by_session: dict[tuple[str, str], list[float]] = defaultdict(list)

    for cam in cameras:
        ref = getattr(cam, "reference", None)
        if ref is not None and not getattr(ref, "enabled", True):
            continue
        photo = getattr(cam, "photo", None)
        meta = getattr(photo, "meta", None) if photo else None
        aa = _parse_float(get_camera_meta(meta, "DJI/AbsoluteAltitude"))
        ra = _parse_float(get_camera_meta(meta, "DJI/RelativeAltitude"))
        if aa is not None and ra is not None:
            sid = _camera_session_id(cam)
            z0_by_session[sid].append(aa - ra)
            rel_by_session[sid].append(ra)

    shifts: dict[str, float] = {}
    anchor_label = ""
    ref_z0 = anchor_z0_override

    if z0_by_session:
        med_z0 = {sid: _median(vals) for sid, vals in z0_by_session.items()}
        med_rel = {sid: _median(vals) for sid, vals in rel_by_session.items()}

        if ref_z0 is None:
            # Prefer a high-altitude Nadir session when present, else highest median RelativeAltitude
            nadir_high = [sid for sid in med_z0 if "nadir" in sid[0].lower() and med_rel[sid] >= 25.0]
            if nadir_high:
                anchor_sid = max(nadir_high, key=lambda s: (len(z0_by_session[s]), med_rel[s]))
            else:
                anchor_sid = max(med_z0.keys(), key=lambda s: (round(med_rel[s] / 5.0), len(z0_by_session[s])))
            ref_z0 = med_z0[anchor_sid]
            anchor_label = _session_display_label(anchor_sid[0])
        else:
            anchor_label = "external_anchor"

        shifted_cams = 0
        for sid, z0_val in med_z0.items():
            dz = ref_z0 - z0_val
            disp = _session_display_label(sid[0])
            key_label = f"{disp}[{sid[1]}]" if sid[1] else disp
            shifts[key_label] = dz

        for cam in cameras:
            ref = getattr(cam, "reference", None)
            if ref is None or not getattr(ref, "enabled", True) or getattr(ref, "location", None) is None:
                continue
            sid = _camera_session_id(cam)
            if sid not in med_z0:
                continue
            dz = ref_z0 - med_z0[sid]
            if abs(dz) > 0.01:
                loc = ref.location
                if Metashape is not None and hasattr(Metashape, "Vector"):
                    ref.location = Metashape.Vector([loc.x, loc.y, loc.z + dz])
                else:
                    loc.z = loc.z + dz
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


def compute_camera_gps_residual(chunk: Any, camera: Any) -> tuple[float, float] | None:
    """Return ``(dZ_m, dXY_m)`` between estimated camera position and GPS reference, or ``None``."""
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
        dlat = float(est.y - ref_loc.y) * 111320.0
        dlon = float(est.x - ref_loc.x) * 111320.0 * math.cos(math.radians(float(ref_loc.y)))
        return dz, math.hypot(dlat, dlon)
    except Exception:
        return None


def build_tie_point_islands(chunk: Any, edge_min: int = 15, max_track_len: int = 40) -> list[list[int]]:
    """Build exact camera connectivity islands from shared tie-point tracks.

    Two aligned, enabled cameras are connected only when they share at least ``edge_min`` tie
    tracks. Never relies on ``len(chunk.components)`` which masks weakly/unconnected sub-blocks.
    """
    aligned = [c for c in (getattr(chunk, "cameras", None) or []) if getattr(c, "transform", None) and c.enabled]
    if not aligned:
        return []

    adj: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    tie_points = getattr(chunk, "tie_points", None)
    if tie_points is not None:
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
            for v, weight in adj[u].items():
                if weight >= edge_min and v not in visited:
                    visited.add(v)
                    queue.append(v)
        islands.append(comp)

    islands.sort(key=len, reverse=True)
    return islands


def _cam_xy_meters(cam: Any, ref_lon: float, ref_lat: float) -> tuple[float, float] | None:
    """Convert a camera's GPS reference into approximate local East/North meters."""
    ref = getattr(cam, "reference", None)
    if ref is None or not getattr(ref, "enabled", True) or getattr(ref, "location", None) is None:
        return None
    loc = ref.location
    lon = _parse_float(getattr(loc, "x", None))
    lat = _parse_float(getattr(loc, "y", None))
    if lon is None or lat is None or (abs(lon) < 1e-4 and abs(lat) < 1e-4):
        return None
    cos_lat = math.cos(math.radians(ref_lat))
    return ((lon - ref_lon) * 111320.0 * cos_lat, (lat - ref_lat) * 111320.0)


def _compute_main_xy_bounds(
    cams: list[Any],
) -> tuple[float, float, float, float, float, float] | None:
    """Return ``(ref_lon, ref_lat, min_x, max_x, min_y, max_y)`` for valid GPS cameras."""
    coords: list[tuple[float, float]] = []
    for cam in cams:
        ref = getattr(cam, "reference", None)
        if ref is None or not getattr(ref, "enabled", True) or getattr(ref, "location", None) is None:
            continue
        lon = _parse_float(getattr(ref.location, "x", None))
        lat = _parse_float(getattr(ref.location, "y", None))
        if lon is not None and lat is not None and not (abs(lon) < 1e-4 and abs(lat) < 1e-4):
            coords.append((lon, lat))
    if len(coords) < 2:
        return None
    ref_lon = _median([c[0] for c in coords])
    ref_lat = _median([c[1] for c in coords])
    cos_lat = math.cos(math.radians(ref_lat))
    xs = [(lon - ref_lon) * 111320.0 * cos_lat for lon, _ in coords]
    ys = [(lat - ref_lat) * 111320.0 for _, lat in coords]
    return (ref_lon, ref_lat, min(xs), max(xs), min(ys), max(ys))


def classify_and_quarantine_islands(
    chunk: Any,
    edge_min: int = 15,
    min_bridge_cams: int = 12,
    min_distant_block_cams: int = 30,
    max_internal_dz_spread_m: float = 6.0,
    max_internal_dxy_spread_m: float = 8.0,
    max_spike_dz_m: float = 7.0,
    max_spike_dxy_m: float = 10.0,
    overlap_margin_m: float = 35.0,
    reoptimize: bool = True,
) -> dict[str, Any]:
    """Partition tie-graph islands into main block, ICP bridge candidates, distant blocks, or rogue quarantine.

    Quarantines detached tiny/folded rogue islands and individual GPS-spike cameras inside the main
    block, while preserving coherent overlapping secondary islands (e.g. Manual/close-range flights
    or individual buildings) for Stage-4 Zero-Neural Open3D ICP registration.
    """
    cameras = list(getattr(chunk, "cameras", None) or [])
    by_key = {c.key: c for c in cameras}
    islands = build_tie_point_islands(chunk, edge_min=edge_min)
    if not islands:
        return {
            "main_island": [],
            "bridge_candidates": [],
            "kept_distant": [],
            "quarantined_keys": [],
            "spike_keys": [],
        }

    main_keys = list(islands[0])
    main_cams = [by_key[k] for k in main_keys if k in by_key]
    main_bounds = _compute_main_xy_bounds(main_cams)

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
                xy = _cam_xy_meters(by_key[k], ref_lon, ref_lat)
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

    # Check for individual GPS-spike cameras inside main_island (only if main_island itself has healthy GPS)
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
                if abs(dz - main_med_dz) > max_spike_dz_m or dxy > max_spike_dxy_m:
                    quarantined.add(k)
                    spike_keys.append(k)
                    print(
                        f"    Quarantining main-block GPS-spike camera {by_key[k].label} "
                        f"(dZ={dz:+.2f}m, dXY={dxy:.2f}m).",
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


def find_unaligned_overlapping_groups(
    chunk: Any,
    main_keys: set[int],
    excluded_keys: set[int] | None = None,
    min_cams: int = 12,
    overlap_margin_m: float = 35.0,
    cluster_radius_m: float = 80.0,
    max_folder_aligned_ratio: float = 0.35,
) -> list[list[int]]:
    """Group enabled unaligned cameras from unaligned flights overlapping the main island for Stage-4 ICP.

    Only considers cameras belonging to folders where fewer than ``max_folder_aligned_ratio``
    (default 35%) of enabled photos aligned into the main block, avoiding wasted rescue passes on
    scattered featureless/water edge frames left over from already-aligned Mission folders.
    """
    cameras = list(getattr(chunk, "cameras", None) or [])
    by_key = {c.key: c for c in cameras}
    excl = excluded_keys or set()

    main_cams = [by_key[k] for k in main_keys if k in by_key]
    main_bounds = _compute_main_xy_bounds(main_cams)
    if main_bounds is None:
        return []
    ref_lon, ref_lat, min_x, max_x, min_y, max_y = main_bounds

    folder_total: dict[str, int] = defaultdict(int)
    folder_in_main: dict[str, int] = defaultdict(int)
    for cam in cameras:
        if not getattr(cam, "enabled", True):
            continue
        folder = _camera_session_id(cam)[0]
        folder_total[folder] += 1
        if cam.key in main_keys:
            folder_in_main[folder] += 1

    unaligned_xy: list[tuple[int, str, float, float]] = []
    for cam in cameras:
        if cam.key in main_keys or cam.key in excl:
            continue
        if not getattr(cam, "enabled", True) or getattr(cam, "transform", None) is not None:
            continue
        folder = _camera_session_id(cam)[0]
        f_tot = folder_total.get(folder, 0)
        if f_tot > 0 and (folder_in_main.get(folder, 0) / f_tot) >= max_folder_aligned_ratio:
            continue
        xy = _cam_xy_meters(cam, ref_lon, ref_lat)
        if xy is None:
            continue
        if (min_x - overlap_margin_m) <= xy[0] <= (max_x + overlap_margin_m) and (min_y - overlap_margin_m) <= xy[
            1
        ] <= (max_y + overlap_margin_m):
            unaligned_xy.append((cam.key, folder, xy[0], xy[1]))

    if len(unaligned_xy) < min_cams:
        return []

    # Connected-component spatial clustering within each folder (or within cluster_radius_m)
    visited: set[int] = set()
    groups: list[list[int]] = []
    r2 = cluster_radius_m * cluster_radius_m
    for idx in range(len(unaligned_xy)):
        if idx in visited:
            continue
        comp_keys: list[int] = []
        queue = deque([idx])
        visited.add(idx)
        while queue:
            u = queue.popleft()
            uk, _uf, ux, uy = unaligned_xy[u]
            comp_keys.append(uk)
            for v in range(len(unaligned_xy)):
                if v in visited:
                    continue
                _vk, _vf, vx, vy = unaligned_xy[v]
                dx = ux - vx
                dy = uy - vy
                if (dx * dx + dy * dy) <= r2:
                    visited.add(v)
                    queue.append(v)
        if len(comp_keys) >= min_cams:
            groups.append(comp_keys)

    groups.sort(key=len, reverse=True)
    return groups
