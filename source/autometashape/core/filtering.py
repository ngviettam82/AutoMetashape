# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Image sharpness filtering, spatial bounding box auto-crop, and USGS gradual selection."""

from __future__ import annotations

import math
import time

try:
    import Metashape
except ImportError:
    Metashape = None


def filter_image_quality(chunk, min_quality: float = 0.5) -> int:
    """Estimates sharpness quality for all ingested photos and disables blurry photos."""
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
    print(
        f"  [Quality Filter] Analyzed in {t_elapsed:.1f}s: {active_count}/{len(chunk.cameras)} sharp images passed (Quality >= {min_quality:.2f}).",
        flush=True,
    )
    if disabled_count > 0:
        print(
            f"  [Quality Filter] Automatically disabled {disabled_count} blurry/defocused images (< {min_quality:.2f}) to protect alignment accuracy.",
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
        return sorted_vals[0], sorted_vals[-1]

    inliers = [v for v in sorted_vals if abs(v - med) <= k * sigma]
    if not inliers:
        return sorted_vals[0], sorted_vals[-1]
    return min(inliers), max(inliers)


def solve_ray_convergence_irls(aligned_cams, iterations: int = 3) -> tuple:
    """Solves 3D camera optical ray convergence using Iteratively Reweighted Least Squares (IRLS)."""
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
        thresh = max(2.5 * med_res, 1.5)

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

    print(
        f"\n[Auto-Crop] Calculating robust 3D camera optical ray convergence from {len(aligned_cams)} cameras...",
        flush=True,
    )
    target_center, inlier_cams = solve_ray_convergence_irls(aligned_cams, iterations=3)

    if target_center is not None:
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

    inlier_centers = [c_pos for c_pos, _ in inlier_cams]
    dists = [(c - target_center).norm() for c in inlier_centers]
    dists.sort()
    if dists:
        idx_95 = min(int(len(dists) * 0.95), len(dists) - 1)
        dist_95 = dists[idx_95]
        max_radius = dist_95 * 1.25 if dist_95 > 0 else float("inf")
    else:
        max_radius = float("inf")

    pts = chunk.tie_points.points if chunk.tie_points else []
    if not pts:
        print("  [Auto-Crop] No tie points available; preserving default region.", flush=True)
        return False

    rot = chunk.region.rot
    rot_t = rot.t()

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

    min_x, max_x = robust_bounds(local_x, k=3.0)
    min_y, max_y = robust_bounds(local_y, k=3.0)

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
    print(f"  Horizontal Size: ({span_x:.2f}m x {span_y:.2f}m) [Padding: {int(padding * 100)}%]", flush=True)
    print(f"  Vertical Extent (Uncropped): {orig_span_z:.2f}m (Z Center: {orig_mid_z:.2f}m)", flush=True)
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
    rot_t = rot.t()

    cam_locs = [rot_t * c.center for c in valid_cams]
    xs = [p.x for p in cam_locs]
    ys = [p.y for p in cam_locs]
    cam_zs = [p.z for p in cam_locs]

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
        if sigma_d > 1e-3 and (d - med_d) > max(1000.0, 5.0 * sigma_d):
            continue
        inlier_xs.append(x)
        inlier_ys.append(y)
        inlier_zs.append(z)

    if len(inlier_xs) < 2:
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
        filtered_pt_zs = (
            [z for z in pt_zs if abs(z - med_pz) <= 4.0 * sigma_pz] if sigma_pz > 1e-3 else pt_zs
        )
        all_zs = inlier_zs + (filtered_pt_zs if filtered_pt_zs else pt_zs)
        med_cam_z = sorted(inlier_zs)[len(inlier_zs) // 2]
        flight_height = abs(med_cam_z - med_pz)
    else:
        all_zs = inlier_zs
        flight_height = 0.0

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
) -> dict:
    """Executes gentle architectural-grade gradual selection refinement."""
    if not chunk.tie_points or not chunk.tie_points.points:
        print("  [Gradual Selection] No tie points present to filter; skipping.", flush=True)
        return {}

    has_pts = bool(chunk.tie_points and getattr(chunk.tie_points, "points", None))
    initial_pts = sum(1 for p in chunk.tie_points.points if getattr(p, "valid", True)) if has_pts else 0
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
            adaptive_fitting=False,
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
            lim = vals[int(len(vals) * (1.0 - max_removal_ratio))]
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
            adaptive_fitting=False,
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
            lim = vals[int(len(vals) * (1.0 - max_removal_ratio))]
            eff_re = max(re_thresh, lim) if re_thresh < lim else re_thresh
        else:
            eff_re = re_thresh
        f.removePoints(eff_re)
        chunk.optimizeCameras(
            fit_f=True,
            fit_cx=True,
            fit_cy=True,
            fit_b1=True,
            fit_b2=True,
            fit_k1=True,
            fit_k2=True,
            fit_k3=True,
            fit_k4=True,
            fit_p1=True,
            fit_p2=True,
            adaptive_fitting=True,
            tiepoint_covariance=True,
        )
        print(
            f"  [Pass 3/4] Reprojection Error (RE <= {eff_re:.2f} px): Pruned residual outliers; optimized complete polynomial lens model (k4, b1, b2, adaptive).",
            flush=True,
        )
    except Exception as e:
        print(f"  [Pass 3/4] Reprojection Error skipped: {e}", flush=True)

    # Pass 4: Image Multiplicity (min_rays >= 2)
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ImageCount)
        if min_rays > 1:
            f.selectPoints(min_rays - 1)
            chunk.tie_points.removeSelectedPoints()
        chunk.optimizeCameras(
            fit_f=True,
            fit_cx=True,
            fit_cy=True,
            fit_b1=True,
            fit_b2=True,
            fit_k1=True,
            fit_k2=True,
            fit_k3=True,
            fit_k4=True,
            fit_p1=True,
            fit_p2=True,
            adaptive_fitting=True,
            tiepoint_covariance=True,
        )
        print(
            f"  [Pass 4/4] Multi-ray Multiplicity (Rays >= {min_rays}): Preserved structural multi-view rays; locked in bundle adjustment.",
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
