# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Image sharpness filtering, spatial bounding box auto-crop, and USGS gradual selection."""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque

from autometashape.core.crs_utils import is_linear_crs

try:
    import Metashape
except ImportError:
    Metashape = None


def filter_image_quality(chunk, min_quality: float = 0.5) -> int:
    """Estimates sharpness quality for all ingested photos and disables blurry photos."""
    print(f"\n[Quality Filter] Estimating sharpness quality across {len(chunk.cameras)} cameras...", flush=True)
    t_qual = time.time()
    chunk.analyzeImages()

    regular_type = (
        getattr(getattr(getattr(Metashape, "Camera", None), "Type", None), "Regular", None)
        if Metashape is not None
        else None
    )

    disabled_count = 0
    per_sensor: dict[Any, list[Any]] = defaultdict(list)
    sensor_keep_alive: list[Any] = []
    for cam in chunk.cameras:
        if not getattr(cam, "enabled", True):
            continue
        if regular_type is not None and getattr(cam, "type", regular_type) != regular_type:
            continue
        s_obj = getattr(cam, "sensor", None)
        if s_obj is not None:
            sensor_keep_alive.append(s_obj)
            s_key: Any = getattr(s_obj, "key", None)
            if s_key is None:
                s_key = id(s_obj)
        else:
            s_key = None
        per_sensor[s_key].append(cam)

    eff_cutoffs: list[float] = []
    for _s_key, grp_cams in per_sensor.items():
        scored_enabled = []
        for cam in grp_cams:
            if "Image/Quality" in cam.meta:
                try:
                    qual_val = float(cam.meta["Image/Quality"])
                    if math.isfinite(qual_val):
                        scored_enabled.append((qual_val, cam))
                except (ValueError, TypeError):
                    pass

        eff_min_quality = float(min_quality)
        below = [(q, c) for q, c in scored_enabled if q < eff_min_quality]
        if len(scored_enabled) >= 6 and (len(below) / len(scored_enabled)) > 0.80:
            sorted_q = sorted(q for q, _c in scored_enabled)
            med_q = sorted_q[len(sorted_q) // 2]
            eff_min_quality = min(eff_min_quality, max(0.15, med_q * 0.65))
            print(
                f"  [Quality Filter] Uniformly low-contrast/hazy sensor group detected "
                f"({len(below)}/{len(scored_enabled)} < {min_quality:.2f}); "
                f"adapting quality cutoff to {eff_min_quality:.2f}.",
                flush=True,
            )
            below = [(q, c) for q, c in scored_enabled if q < eff_min_quality]
        eff_cutoffs.append(eff_min_quality)

        # Never disable cameras down to < 2 remaining per sensor group
        max_disable = max(0, len(grp_cams) - 2)
        if len(below) > max_disable and len(grp_cams) >= 2:
            below.sort(key=lambda item: item[0])
            to_disable = [c for _q, c in below[:max_disable]]
            print(
                f"  [Quality Filter] Guard: clamping disable count from {len(below)} to {len(to_disable)} "
                f"to retain at least 2 sharpest cameras.",
                flush=True,
            )
        else:
            to_disable = [c for _q, c in below]

        for cam in to_disable:
            cam.enabled = False
            disabled_count += 1

    active_count = sum(1 for c in chunk.cameras if c.enabled)
    t_elapsed = time.time() - t_qual
    shown_cutoff = min(eff_cutoffs) if eff_cutoffs else float(min_quality)
    print(
        f"  [Quality Filter] Analyzed in {t_elapsed:.1f}s: "
        f"{active_count}/{len(chunk.cameras)} sharp images passed (Quality >= {shown_cutoff:.2f}).",
        flush=True,
    )
    if disabled_count > 0:
        print(
            f"  [Quality Filter] Automatically disabled {disabled_count} blurry/defocused images "
            f"(< {shown_cutoff:.2f}) to protect alignment accuracy.",
            flush=True,
        )
    return disabled_count


def robust_bounds(sorted_vals, k: float = 3.0) -> tuple:
    """Calculates robust spatial bounding intervals using Median Absolute Deviation (MAD)."""
    if not sorted_vals:
        return 0.0, 0.0
    n = len(sorted_vals)
    med = sorted_vals[n // 2]
    devs = sorted([abs(v - med) for v in sorted_vals])
    mad = devs[n // 2]
    sigma = 1.4826 * mad
    if sigma < 1e-4:
        if n >= 20:
            lo_idx = int(n * 0.05)
            hi_idx = min(n - 1, max(lo_idx, int(n * 0.95)))
            return sorted_vals[lo_idx], sorted_vals[hi_idx]
        return sorted_vals[0], sorted_vals[-1]

    inliers = [v for v in sorted_vals if abs(v - med) <= k * sigma]
    if not inliers:
        return sorted_vals[0], sorted_vals[-1]
    return min(inliers), max(inliers)


def solve_ray_convergence_irls(aligned_cams, iterations: int = 3, scale: float = 1.0) -> tuple:
    """Solves 3D camera optical ray convergence using Iteratively Reweighted Least Squares (IRLS)."""
    eff_scale = scale if (isinstance(scale, (int, float)) and scale > 0.0) else 1.0
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

    for _ in range(iterations):
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

        residuals = []
        for c_pos, d in cam_data:
            diff = target_center - c_pos
            proj = diff - d * (diff * d)
            residuals.append((proj.norm(), (c_pos, d)))

        residuals.sort(key=lambda x: x[0])
        med_res = residuals[len(residuals) // 2][0]
        thresh = max(2.5 * med_res, 1.5 / eff_scale)

        active_cams = [item for res, item in residuals if res <= thresh]
        if len(active_cams) < 2:
            break

    return target_center, active_cams


def auto_crop_to_main_structure(chunk, padding: float = 0.15) -> bool:
    """Automatically crops chunk.region horizontally around main target structure preserving vertical bounds."""
    aligned_cams = [c for c in chunk.cameras if c.transform and c.enabled]
    if len(aligned_cams) < 2:
        print("  [Auto-Crop] Fewer than 2 aligned cameras; skipping bounding box crop.", flush=True)
        return False

    raw_scale = getattr(getattr(chunk, "transform", None), "scale", None)
    scale = float(raw_scale) if (isinstance(raw_scale, (int, float)) and raw_scale > 0.0) else 1.0
    print(
        f"\n[Auto-Crop] Calculating robust 3D camera optical ray convergence from {len(aligned_cams)} cameras...",
        flush=True,
    )
    target_center, inlier_cams = solve_ray_convergence_irls(aligned_cams, iterations=3, scale=scale)
    ray_converged = target_center is not None

    if ray_converged:
        inlier_count = len(inlier_cams)
        outlier_count = len(aligned_cams) - inlier_count
        print(
            f"  [Auto-Crop] Optical ray convergence centroid: ({target_center.x:.2f}, {target_center.y:.2f}, {target_center.z:.2f})",
            flush=True,
        )
        print(
            f"  [Auto-Crop] Camera consensus: {inlier_count}/{len(aligned_cams)} cameras locked on main structure ({outlier_count} secondary/outlier views pruned).",
            flush=True,
        )
    else:
        cam_centers = [c.center for c in aligned_cams]
        target_center = Metashape.Vector(
            [
                sum(c.x for c in cam_centers) / len(cam_centers),
                sum(c.y for c in cam_centers) / len(cam_centers),
                sum(c.z for c in cam_centers) / len(cam_centers),
            ]
        )
        inlier_cams = [(c.center, None) for c in aligned_cams]
        print(
            f"  [Auto-Crop] Using camera cluster centroid: ({target_center.x:.2f}, {target_center.y:.2f}, {target_center.z:.2f})",
            flush=True,
        )

    rot = chunk.region.rot
    rot_t = rot.t()
    target_local = rot_t * target_center

    inward_dots: list[float] = []
    cam_all = [c.center for c in aligned_cams if getattr(c, "center", None) is not None]
    if cam_all:
        cam_mean = Metashape.Vector(
            [
                sum(p.x for p in cam_all) / len(cam_all),
                sum(p.y for p in cam_all) / len(cam_all),
                sum(p.z for p in cam_all) / len(cam_all),
            ]
        )
        for c in aligned_cams:
            try:
                to_mean = cam_mean - c.center
                norm_tm = math.sqrt(to_mean.x**2 + to_mean.y**2 + to_mean.z**2)
                if norm_tm <= 1e-6:
                    continue
                ray_dir = c.transform.mulp(Metashape.Vector([0.0, 0.0, 1.0])) - c.center
                norm_r = math.sqrt(ray_dir.x**2 + ray_dir.y**2 + ray_dir.z**2)
                if norm_r <= 1e-6:
                    continue
                dot_val = (to_mean.x * ray_dir.x + to_mean.y * ray_dir.y + to_mean.z * ray_dir.z) / (norm_tm * norm_r)
                if math.isfinite(dot_val):
                    inward_dots.append(dot_val)
            except Exception:
                pass
    dot_inward = sorted(inward_dots)[len(inward_dots) // 2] if inward_dots else 1.0
    outward_or_parallel = (not ray_converged) or (dot_inward <= 0.25)

    inlier_centers = [c_pos for c_pos, _ in inlier_cams]
    if outward_or_parallel:
        max_radius = float("inf")
    else:
        dists_xy = []
        for c_pos in inlier_centers:
            cl = rot_t * c_pos
            dists_xy.append(math.hypot(cl.x - target_local.x, cl.y - target_local.y))
        dists_xy.sort()
        if dists_xy:
            idx_95 = min(int(len(dists_xy) * 0.95), len(dists_xy) - 1)
            dist_95 = dists_xy[idx_95]
            max_radius = dist_95 * 1.25 if dist_95 > 0 else float("inf")
        else:
            max_radius = float("inf")

    pts = chunk.tie_points.points if chunk.tie_points else []
    if not pts:
        print("  [Auto-Crop] No tie points available; preserving default region.", flush=True)
        return False

    orig_region = chunk.region
    orig_local_center = rot_t * orig_region.center
    orig_mid_z = orig_local_center.z
    orig_span_z = orig_region.size.z

    local_x, local_y = [], []
    valid_count = 0
    for pt in pts:
        if not pt.valid:
            continue
        w = getattr(pt.coord, "w", 1.0)
        if abs(w) <= 1e-12:
            continue
        coord = Metashape.Vector([pt.coord.x / w, pt.coord.y / w, pt.coord.z / w])
        loc = rot_t * coord
        if math.hypot(loc.x - target_local.x, loc.y - target_local.y) > max_radius:
            continue

        local_x.append(loc.x)
        local_y.append(loc.y)
        valid_count += 1

    if valid_count < 100:
        print(
            f"  [Auto-Crop] Insufficient structure tie points ({valid_count}); preserving default region.", flush=True
        )
        return False

    local_x.sort()
    local_y.sort()

    k_rb = 3.5 if outward_or_parallel else 3.0
    min_x, max_x = robust_bounds(local_x, k=k_rb)
    min_y, max_y = robust_bounds(local_y, k=k_rb)
    if outward_or_parallel and cam_all:
        cam_local = [rot_t * p for p in cam_all]
        cxs = [p.x for p in cam_local]
        cys = [p.y for p in cam_local]
        min_x = min(min_x, min(cxs))
        max_x = max(max_x, max(cxs))
        min_y = min(min_y, min(cys))
        max_y = max(max_y, max(cys))

    span_x = (max_x - min_x) * (1.0 + 2.0 * padding)
    span_y = (max_y - min_y) * (1.0 + 2.0 * padding)

    mid_local = Metashape.Vector(
        [
            (min_x + max_x) * 0.5,
            (min_y + max_y) * 0.5,
            orig_mid_z,
        ]
    )
    new_center = rot * mid_local
    new_size = Metashape.Vector([span_x, span_y, orig_span_z])

    region = chunk.region
    region.center = new_center
    region.size = new_size
    chunk.region = region

    print("  [Auto-Crop] Successfully cropped horizontal region to main structure!", flush=True)
    print(f"  Center: ({new_center.x:.2f}, {new_center.y:.2f}, {new_center.z:.2f})", flush=True)
    print(
        f"  Horizontal Size: ({span_x * scale:.2f}m x {span_y * scale:.2f}m) [Padding: {int(padding * 100)}%]",
        flush=True,
    )
    print(
        f"  Vertical Extent (Uncropped): {orig_span_z * scale:.2f}m (Z Center: {orig_mid_z * scale:.2f}m)", flush=True
    )
    print(f"  Isolated Main Structure Tie Points: {valid_count:,} points", flush=True)
    return True


def auto_crop_camera_gps_square(chunk, padding: float = 0.05) -> bool:
    """Crops chunk.region horizontally to the minimum area horizontal rectangle enclosing camera images."""
    aligned_cams = [c for c in chunk.cameras if c.transform and c.enabled]
    if len(aligned_cams) < 2:
        print("  [Auto-Crop-GPS] Fewer than 2 aligned cameras; skipping bounding box crop.", flush=True)
        return False

    print(
        f"\n[Auto-Crop-GPS] Computing minimum area horizontal enclosing rectangle from {len(aligned_cams)} cameras...",
        flush=True,
    )

    raw_scale = getattr(getattr(chunk, "transform", None), "scale", None)
    scale = float(raw_scale) if (isinstance(raw_scale, (int, float)) and raw_scale > 0.0) else 1.0
    skip_null_island = not is_linear_crs(getattr(chunk, "crs", None))

    valid_cams = []
    for c in aligned_cams:
        if skip_null_island and c.reference and getattr(c.reference, "enabled", True) and c.reference.location:
            loc = c.reference.location
            if abs(loc.x) < 1e-4 and abs(loc.y) < 1e-4:
                continue
        valid_cams.append(c)

    if len(valid_cams) < 2:
        valid_cams = aligned_cams

    rot = chunk.region.rot
    rot_t = rot.t()

    cam_locs = [rot_t * c.center for c in valid_cams]
    xs = [p.x for p in cam_locs]
    ys = [p.y for p in cam_locs]
    cam_zs = [p.z for p in cam_locs]

    # Connected-component nearest-neighbor pruning so >1 km linear corridors & multi-building campuses survive
    n_cams = len(cam_locs)
    if n_cams >= 3:
        nn_dists: list[float] = []
        try:
            from scipy.spatial import cKDTree

            coords_2d = list(zip(xs, ys))
            dists_2d, _ = cKDTree(coords_2d).query(coords_2d, k=2)
            nn_dists = [float(row[1]) for row in dists_2d if math.isfinite(float(row[1]))]
        except Exception:
            nn_dists = []
        if not nn_dists:
            step_s = max(1, n_cams // 500)
            sample_idx = list(range(0, n_cams, step_s))[:500]
            for i in sample_idx:
                best_d2 = float("inf")
                for j in sample_idx:
                    if i == j:
                        continue
                    d2 = (xs[i] - xs[j]) ** 2 + (ys[i] - ys[j]) ** 2
                    if d2 < best_d2:
                        best_d2 = d2
                if math.isfinite(best_d2):
                    nn_dists.append(math.sqrt(best_d2))
        med_nn = sorted(nn_dists)[len(nn_dists) // 2] if nn_dists else 100.0 / scale
        conn_thresh = max(1000.0 / scale, 10.0 * med_nn)
        step_g = max(1e-3, conn_thresh)
        r2_g = conn_thresh * conn_thresh
        buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i in range(n_cams):
            buckets[(int(math.floor(xs[i] / step_g)), int(math.floor(ys[i] / step_g)))].append(i)
        visited: set[int] = set()
        comps: list[list[int]] = []
        for start in range(n_cams):
            if start in visited:
                continue
            comp: list[int] = []
            q = deque([start])
            visited.add(start)
            while q:
                u = q.popleft()
                comp.append(u)
                gx = int(math.floor(xs[u] / step_g))
                gy = int(math.floor(ys[u] / step_g))
                for dgx in (-1, 0, 1):
                    for dgy in (-1, 0, 1):
                        for v in buckets.get((gx + dgx, gy + dgy), ()):
                            if v in visited:
                                continue
                            if ((xs[u] - xs[v]) ** 2 + (ys[u] - ys[v]) ** 2) <= r2_g:
                                visited.add(v)
                                q.append(v)
            comps.append(comp)
        min_keep = max(2, int(0.15 * n_cams))
        keep_idxs = [i for c in comps if len(c) >= min_keep for i in c]
        if len(keep_idxs) < 2:
            keep_idxs = max(comps, key=len)
        inlier_xs = [xs[i] for i in keep_idxs]
        inlier_ys = [ys[i] for i in keep_idxs]
        inlier_zs = [cam_zs[i] for i in keep_idxs]
    else:
        inlier_xs, inlier_ys, inlier_zs = xs, ys, cam_zs

    best_area = float("inf")
    best_theta = 0.0
    best_bounds = (0.0, 0.0, 0.0, 0.0)

    for step in range(180):
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

    raw_pts = getattr(chunk.tie_points, "points", None) if chunk.tie_points else None
    pts = []
    if raw_pts:
        n_tp = len(raw_pts) if hasattr(raw_pts, "__len__") else 0
        if n_tp > 50000 and hasattr(raw_pts, "__getitem__"):
            tp_step = max(1, n_tp // 50000)
            tp_iter = (raw_pts[idx] for idx in range(0, n_tp, tp_step))
        else:
            tp_iter = iter(raw_pts)
        for p in tp_iter:
            if p.valid and abs(getattr(p.coord, "w", 1.0)) > 1e-12:
                pts.append(p)

    orig_size_x = None
    orig_size_y = None
    orig_local_center = None
    try:
        raw_sz = getattr(getattr(chunk, "region", None), "size", None)
        if raw_sz is not None:
            if isinstance(getattr(raw_sz, "x", None), (int, float)):
                orig_size_x = float(raw_sz.x)
            if isinstance(getattr(raw_sz, "y", None), (int, float)):
                orig_size_y = float(raw_sz.y)
        orig_local_center = rot_t * chunk.region.center
    except Exception:
        pass

    pt_locals: list = []
    if pts:
        for p in pts:
            wc = getattr(p.coord, "w", 1.0)
            pt_locals.append(rot_t * Metashape.Vector([p.coord.x / wc, p.coord.y / wc, p.coord.z / wc]))
        pt_zs = sorted([v.z for v in pt_locals])
        med_pz = pt_zs[len(pt_zs) // 2]
        devs_pz = sorted([abs(z - med_pz) for z in pt_zs])
        mad_pz = devs_pz[len(devs_pz) // 2]
        sigma_pz = 1.4826 * mad_pz
        p005 = pt_zs[max(0, int(0.005 * (len(pt_zs) - 1)))]
        p995 = pt_zs[min(len(pt_zs) - 1, int(0.995 * (len(pt_zs) - 1)))]
        z_floor = min(med_pz - max(4.0 * sigma_pz, 5.0 / scale), p005)
        z_ceil = max(med_pz + max(4.0 * sigma_pz, 5.0 / scale), p995)
        filtered_pt_zs = [z for z in pt_zs if z_floor <= z <= z_ceil] if sigma_pz > (1e-3 / scale) else pt_zs
        all_zs = inlier_zs + (filtered_pt_zs if filtered_pt_zs else pt_zs)
        zs_sorted = sorted(inlier_zs)
        cam_z_75 = zs_sorted[min(len(zs_sorted) - 1, int(0.75 * (len(zs_sorted) - 1)))]
        flight_height = abs(cam_z_75 - med_pz)
        min_z = min(all_zs)
        max_z = max(all_zs)
        span_z = max(chunk.region.size.z, (max_z - min_z) * (1.0 + 2.0 * padding))
        mid_z = (min_z + max_z) * 0.5
    else:
        # Preserve original vertical center & span when tie points are empty (e.g. post-ICP mergeChunks)
        # so the bounding box never shifts up to drone flight altitude and slices off the ground/building.
        orig_mid_z = None
        orig_span_z = None
        try:
            raw_mid = (rot_t * chunk.region.center).z
            raw_span = chunk.region.size.z
            if isinstance(raw_mid, (int, float)) and isinstance(raw_span, (int, float)):
                orig_mid_z = float(raw_mid)
                orig_span_z = float(raw_span)
        except Exception:
            pass
        if orig_mid_z is not None and orig_span_z is not None and orig_span_z > 0.0:
            orig_z_min = orig_mid_z - 0.5 * orig_span_z
            orig_z_max = orig_mid_z + 0.5 * orig_span_z
            all_zs = inlier_zs + [orig_z_min, orig_z_max]
            zs_sorted = sorted(inlier_zs)
            cam_z_75 = zs_sorted[min(len(zs_sorted) - 1, int(0.75 * (len(zs_sorted) - 1)))]
            flight_height = max(0.0, abs(cam_z_75 - orig_mid_z))
            min_z = min(all_zs)
            max_z = max(all_zs)
            span_z = max(orig_span_z, (max_z - min_z) * (1.0 + 2.0 * padding))
            mid_z = (min_z + max_z) * 0.5
        else:
            flight_height = 0.0
            min_z = min(inlier_zs)
            max_z = max(inlier_zs)
            fallback_span = (max_z - min_z) * (1.0 + 2.0 * padding)
            raw_size_z = getattr(getattr(chunk, "region", None), "size", None)
            size_z_val = getattr(raw_size_z, "z", None)
            span_z = (
                max(float(size_z_val), fallback_span)
                if isinstance(size_z_val, (int, float))
                else max(10.0 / scale, fallback_span)
            )
            mid_z = (min_z + max_z) * 0.5

    fov_margin = flight_height * 0.35 if flight_height > (1e-3 / scale) else 0.0
    margin_x = max(span_x * padding, fov_margin)
    margin_y = max(span_y * padding, fov_margin)
    min_side = 1.0 / scale
    cos_t = math.cos(best_theta)
    sin_t = math.sin(best_theta)

    exp_min_rx = min_rx - margin_x
    exp_max_rx = max_rx + margin_x
    exp_min_ry = min_ry - margin_y
    exp_max_ry = max_ry + margin_y
    if pt_locals:
        pt_rxs = sorted([v.x * cos_t + v.y * sin_t for v in pt_locals])
        pt_rys = sorted([-v.x * sin_t + v.y * cos_t for v in pt_locals])
        idx_p1 = max(0, int(0.01 * (len(pt_rxs) - 1)))
        idx_p99 = min(len(pt_rxs) - 1, int(0.99 * (len(pt_rxs) - 1)))
        exp_min_rx = min(exp_min_rx, pt_rxs[idx_p1])
        exp_max_rx = max(exp_max_rx, pt_rxs[idx_p99])
        exp_min_ry = min(exp_min_ry, pt_rys[idx_p1])
        exp_max_ry = max(exp_max_ry, pt_rys[idx_p99])

    side_x = max(exp_max_rx - exp_min_rx, min_side)
    side_y = max(exp_max_ry - exp_min_ry, min_side)

    mid_rx = (exp_min_rx + exp_max_rx) * 0.5
    mid_ry = (exp_min_ry + exp_max_ry) * 0.5
    if not pts:
        raw_span_rx = exp_max_rx - exp_min_rx
        raw_span_ry = exp_max_ry - exp_min_ry
        if (
            raw_span_rx < (2.0 / scale)
            and orig_size_x is not None
            and math.isfinite(orig_size_x)
            and orig_size_x > side_x
        ):
            side_x = orig_size_x
            if orig_local_center is not None:
                try:
                    orig_rx = float(orig_local_center.x) * cos_t + float(orig_local_center.y) * sin_t
                    if math.isfinite(orig_rx):
                        mid_rx = orig_rx
                except Exception:
                    pass
        if (
            raw_span_ry < (2.0 / scale)
            and orig_size_y is not None
            and math.isfinite(orig_size_y)
            and orig_size_y > side_y
        ):
            side_y = orig_size_y
            if orig_local_center is not None:
                try:
                    orig_ry = -float(orig_local_center.x) * sin_t + float(orig_local_center.y) * cos_t
                    if math.isfinite(orig_ry):
                        mid_ry = orig_ry
                except Exception:
                    pass

    mid_x = mid_rx * cos_t - mid_ry * sin_t
    mid_y = mid_rx * sin_t + mid_ry * cos_t

    Rz = Metashape.Matrix(
        [
            [cos_t, -sin_t, 0.0],
            [sin_t, cos_t, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
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
    print(
        f"  Vertical Extent: {span_z * scale:.2f}m (Z: [{min_z * scale:.2f}m, {max_z * scale:.2f}m])",
        flush=True,
    )
    return True


def execute_usgs_gradual_selection(
    chunk,
    ru_thresh: float = 15.0,
    pa_thresh: float = 3.0,
    re_thresh: float = 0.50,
    min_rays: int = 2,
    max_removal_ratio: float = 0.10,
    adaptive_fitting: bool = False,
) -> dict:
    """Executes gentle architectural-grade gradual selection refinement."""
    if not chunk.tie_points or not chunk.tie_points.points:
        print("  [Gradual Selection] No tie points present to filter; skipping.", flush=True)
        return {}

    initial_pts = sum(1 for p in chunk.tie_points.points if getattr(p, "valid", True))
    if initial_pts == 0:
        print("  [Gradual Selection] No valid tie points present to filter; skipping.", flush=True)
        return {}
    f = Metashape.TiePoints.Filter()

    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ReprojectionError)
        initial_re = list(f.values)
        initial_rms = math.sqrt(sum(v * v for v in initial_re) / len(initial_re)) if initial_re else 0.0
    except Exception:
        initial_rms = 0.0

    print(
        f"\n[Tie-Point Refinement] Starting gentle gradual selection refinement across {initial_pts:,} tie points (Initial Reprojection RMS: {initial_rms:.3f} px, max prune ratio: {int(max_removal_ratio * 100)}%)...",
        flush=True,
    )

    def _lock_unstable_lens_params() -> None:
        for s in getattr(chunk, "sensors", None) or []:
            if getattr(s, "fixed", False):
                continue
            cal = getattr(s, "calibration", None)
            if cal is not None:
                s_type = str(getattr(s, "type", "") or "").lower()
                is_wide_model = "fisheye" in s_type or "spherical" in s_type or "cylindrical" in s_type
                try:
                    if not is_wide_model:
                        cal.k4 = 0.0
                    cal.b1 = 0.0
                    cal.b2 = 0.0
                    s.calibration = cal
                except Exception:
                    pass

    _lock_unstable_lens_params()

    # Pass 1: Reconstruction Uncertainty (RU <= 15.0)
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ReconstructionUncertainty)
        vals = sorted(f.values)
        if vals:
            lim_idx = min(len(vals) - 1, max(0, int(len(vals) * (1.0 - max_removal_ratio))))
            lim = vals[lim_idx]
            eff_ru = max(ru_thresh, lim) if ru_thresh < lim else ru_thresh
        else:
            eff_ru = ru_thresh
        f.removePoints(eff_ru)
        chunk.optimizeCameras(
            fit_f=True,
            fit_cx=True,
            fit_cy=True,
            fit_b1=False,
            fit_b2=False,
            fit_k1=False,
            fit_k2=False,
            fit_k3=False,
            fit_k4=False,
            fit_p1=False,
            fit_p2=False,
            adaptive_fitting=bool(adaptive_fitting),
        )
        print(
            f"  [Pass 1/4] Reconstruction Uncertainty (RU <= {eff_ru:.1f}): Pruned low-intersection rays; optimized (f, cx, cy).",
            flush=True,
        )
    except Exception as e:
        print(f"  [Pass 1/4] Reconstruction Uncertainty skipped: {e}", flush=True)

    # Pass 2: Projection Accuracy (PA <= 3.0 px)
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ProjectionAccuracy)
        vals = sorted(f.values)
        if vals:
            lim_idx = min(len(vals) - 1, max(0, int(len(vals) * (1.0 - max_removal_ratio))))
            lim = vals[lim_idx]
            eff_pa = max(pa_thresh, lim) if pa_thresh < lim else pa_thresh
        else:
            eff_pa = pa_thresh
        f.removePoints(eff_pa)
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
            adaptive_fitting=bool(adaptive_fitting),
        )
        print(
            f"  [Pass 2/4] Projection Accuracy (PA <= {eff_pa:.2f} px): Pruned correlation ambiguity; optimized (f, cx, cy, k1..k3, p1, p2).",
            flush=True,
        )
    except Exception as e:
        print(f"  [Pass 2/4] Projection Accuracy skipped: {e}", flush=True)

    # Pass 3: Reprojection Error (RE <= 0.50 px)
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ReprojectionError)
        vals = sorted(f.values)
        if vals:
            lim_idx = min(len(vals) - 1, max(0, int(len(vals) * (1.0 - max_removal_ratio))))
            lim = vals[lim_idx]
            eff_re = max(re_thresh, lim) if re_thresh < lim else re_thresh
        else:
            eff_re = re_thresh
        f.removePoints(eff_re)
        _lock_unstable_lens_params()
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
            adaptive_fitting=bool(adaptive_fitting),
            tiepoint_covariance=True,
        )
        print(
            f"  [Pass 3/4] Reprojection Error (RE <= {eff_re:.2f} px): Pruned residual outliers; optimized stable lens model (f, cx, cy, k1..k3, p1, p2; k4/b1/b2 locked).",
            flush=True,
        )
    except Exception as e:
        print(f"  [Pass 3/4] Reprojection Error skipped: {e}", flush=True)

    # Pass 4: Image Multiplicity (min_rays >= 2)
    try:
        before_p4 = sum(1 for p in chunk.tie_points.points if getattr(p, "valid", True))
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ImageCount)
        removed_p4 = 0
        if min_rays > 1:
            vals_p4 = list(getattr(f, "values", []) or [])
            below_cnt = sum(1 for v in vals_p4 if v < min_rays)
            if below_cnt > 0 and vals_p4 and (below_cnt / len(vals_p4)) <= max_removal_ratio:
                f.selectPoints(min_rays - 1)
                chunk.tie_points.removeSelectedPoints()
                after_p4 = sum(1 for p in chunk.tie_points.points if getattr(p, "valid", True))
                removed_p4 = max(0, before_p4 - after_p4)
            elif below_cnt > 0:
                print(
                    f"  [Pass 4/4] Multiplicity removal ({below_cnt}/{len(vals_p4)}) exceeds "
                    f"max_removal_ratio={max_removal_ratio:.2f}; preserving tie points.",
                    flush=True,
                )
        _lock_unstable_lens_params()
        if removed_p4 > 0:
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
                adaptive_fitting=bool(adaptive_fitting),
                tiepoint_covariance=True,
            )
        print(
            f"  [Pass 4/4] Multi-ray Multiplicity (Rays >= {min_rays}): Preserved structural multi-view rays; "
            f"removed {removed_p4:,} point(s) (k4/b1/b2 locked).",
            flush=True,
        )
    except Exception as e:
        print(f"  [Pass 4/4] Multiplicity filter skipped: {e}", flush=True)

    final_pts = sum(1 for p in chunk.tie_points.points if p.valid) if chunk.tie_points else 0
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ReprojectionError)
        final_re = list(f.values)
        final_rms = math.sqrt(sum(v * v for v in final_re) / len(final_re)) if final_re else 0.0
    except Exception:
        final_rms = 0.0

    rms_improvement = ((initial_rms - final_rms) / initial_rms * 100.0) if initial_rms > 0 else 0.0
    retention_pct = (final_pts / initial_pts * 100.0) if initial_pts > 0 else 0.0
    print(
        f"  [Tie-Point Summary] Reprojection RMS: {initial_rms:.3f} px -> {final_rms:.3f} px "
        f"({rms_improvement:.1f}% reduction). Inliers: {final_pts:,}/{initial_pts:,} tie points "
        f"retained ({retention_pct:.1f}%).\n",
        flush=True,
    )

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
    adaptive_fitting: bool = False,
) -> dict:
    """Filters low-confidence tie points using gentle architectural-grade gradual selection."""
    return execute_usgs_gradual_selection(
        chunk,
        ru_thresh=max_uncertainty,
        pa_thresh=pa_thresh,
        re_thresh=max_reproj_error,
        min_rays=min_rays,
        max_removal_ratio=max_removal_ratio,
        adaptive_fitting=bool(adaptive_fitting),
    )
