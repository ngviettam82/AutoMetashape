# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Standalone CPU Open3D Multi-Scale Sim(3) Point-to-Plane + Gated Colored ICP Worker.

Executed inside the worker node's isolated portable Python 3.11 + Open3D runtime
(``%LOCALAPPDATA%\\AutoMetashape\\bridge_runtime_v*``), completely decoupled from Metashape's
bundled Python 3.9 and requiring zero system Python, zero PyTorch, and zero CUDA drivers.

Usage:
    python -s icp_worker.py --target mission.ply --source island.ply --output result.json
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import sys
import time
from typing import Any

import numpy as np
import open3d as o3d
from scipy.spatial import cKDTree


def _resolve_o3d_readable_path(path: str) -> str:
    """Convert non-ASCII Windows paths to 8.3 short paths so Open3D's ANSI C++ fopen succeeds."""
    if sys.platform != "win32" or path.isascii():
        return path
    try:
        buf = ctypes.create_unicode_buffer(4096)
        n = ctypes.windll.kernel32.GetShortPathNameW(os.path.abspath(path), buf, 4096)
        if 0 < n < 4096:
            return buf.value
    except Exception:
        pass
    return path


def umeyama_sim3(
    src: np.ndarray,
    dst: np.ndarray,
    with_scale: bool = True,
) -> tuple[float, np.ndarray, np.ndarray]:
    """Compute exact least-squares Sim(3) transform ``dst ≈ scale * (R @ src) + t``."""
    if len(src) < 3 or len(dst) != len(src):
        raise ValueError(f"umeyama_sim3 requires >= 3 matching point pairs, got src={len(src)}, dst={len(dst)}")
    mu_s = src.mean(axis=0)
    mu_d = dst.mean(axis=0)
    xc = src - mu_s
    yc = dst - mu_d
    cov = (yc.T @ xc) / len(src)
    u_mat, singular_vals, vt_mat = np.linalg.svd(cov)
    sign_mat = np.eye(3, dtype=np.float64)
    if np.linalg.det(u_mat) * np.linalg.det(vt_mat) < 0.0:
        sign_mat[2, 2] = -1.0
    rot = u_mat @ sign_mat @ vt_mat
    if with_scale:
        var_s = float(np.sum(xc * xc) / len(src))
        if var_s <= 0.0:
            raise ValueError("Degenerate zero-variance source point set in umeyama_sim3")
        scale = float(np.sum(singular_vals * np.diag(sign_mat)) / var_s)
    else:
        scale = 1.0
    trans = mu_d - scale * (rot @ mu_s)
    return scale, rot, trans


def _rot_z(deg: float) -> np.ndarray:
    rad = math.radians(deg)
    c_val, s_val = math.cos(rad), math.sin(rad)
    return np.array(
        [[c_val, -s_val, 0.0], [s_val, c_val, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def _write_result(out_json: str, payload: dict[str, Any]) -> None:
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def _prepare_normals(pcd: o3d.geometry.PointCloud, radius: float) -> None:
    if not pcd.has_normals():
        pcd.estimate_normals(
            search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=radius, max_nn=30),
        )
    else:
        pcd.normalize_normals()


def run_icp(
    target_ply: str,
    source_ply: str,
    out_json: str,
    colored_refine: bool = True,
    crop_xy_margin: float = 22.0,
    crop_z_margin: float = 25.0,
    search_xy_m: float = 8.0,
    search_z_m: float = 7.5,
    search_yaw_deg: float = 10.0,
) -> int:
    """Register ``source_ply`` onto ``target_ply`` via coarse search + multi-scale Tukey Sim(3) ICP."""
    t0 = time.time()
    print(f"[ICP] Loading Target cloud: {target_ply}", flush=True)
    tgt_full = o3d.io.read_point_cloud(_resolve_o3d_readable_path(target_ply))
    print(f"[ICP] Loading Source cloud: {source_ply}", flush=True)
    src_full = o3d.io.read_point_cloud(_resolve_o3d_readable_path(source_ply))
    print(
        f"[ICP] Loaded Target={len(tgt_full.points):,} pts, Source={len(src_full.points):,} pts",
        flush=True,
    )

    if len(src_full.points) < 200 or len(tgt_full.points) < 200:
        _write_result(
            out_json,
            {
                "ok": False,
                "reason": "insufficient_input_points",
                "target_points": len(tgt_full.points),
                "source_points": len(src_full.points),
                "fitness_15cm": 0.0,
                "rmse_15cm_m": 999.0,
                "median_15cm_m": 999.0,
                "scale": 1.0,
                "tilt_deg": 0.0,
                "rotation_deg": 0.0,
                "yaw_deg": 0.0,
                "T_enu": np.eye(4, dtype=np.float64).tolist(),
            },
        )
        return 0

    src_pts = np.asarray(src_full.points)
    # Use robust [1, 99] percentiles for initial ROI crop so stray Stage-0 outliers cannot distort crop/pivot
    s_min_raw, s_max_raw = np.percentile(src_pts, [1.0, 99.0], axis=0)
    # Use a wide +/-45 m Z crop initially so non-DJI flights lacking RelativeAltitude XMP still overlap in ROI
    eff_z_margin = max(crop_z_margin, 45.0)
    crop_min = np.array([s_min_raw[0] - crop_xy_margin, s_min_raw[1] - crop_xy_margin, s_min_raw[2] - eff_z_margin])
    crop_max = np.array([s_max_raw[0] + crop_xy_margin, s_max_raw[1] + crop_xy_margin, s_max_raw[2] + eff_z_margin])
    bbox = o3d.geometry.AxisAlignedBoundingBox(crop_min, crop_max)
    tgt_crop = tgt_full.crop(bbox)
    print(f"[ICP] Cropped Target around ROI: {len(tgt_crop.points):,} pts", flush=True)

    if len(tgt_crop.points) < 200:
        _write_result(
            out_json,
            {
                "ok": False,
                "reason": "insufficient_roi_overlap",
                "target_roi_points": len(tgt_crop.points),
                "source_points": len(src_full.points),
                "fitness_15cm": 0.0,
                "rmse_15cm_m": 999.0,
                "median_15cm_m": 999.0,
                "scale": 1.0,
                "tilt_deg": 0.0,
                "rotation_deg": 0.0,
                "yaw_deg": 0.0,
                "T_enu": np.eye(4, dtype=np.float64).tolist(),
            },
        )
        return 0

    src_ds0 = src_full.voxel_down_sample(0.05)
    tgt_ds0 = tgt_crop.voxel_down_sample(0.05)
    src_clean, _ = src_ds0.remove_statistical_outlier(nb_neighbors=16, std_ratio=2.0)
    tgt_clean, _ = tgt_ds0.remove_statistical_outlier(nb_neighbors=16, std_ratio=2.0)
    print(
        f"[ICP] Cleaned 5cm voxel clouds: Target={len(tgt_clean.points):,}, Source={len(src_clean.points):,}",
        flush=True,
    )

    if len(src_clean.points) < 100 or len(tgt_clean.points) < 100:
        _write_result(
            out_json,
            {
                "ok": False,
                "reason": "insufficient_cleaned_points",
                "fitness_15cm": 0.0,
                "rmse_15cm_m": 999.0,
                "median_15cm_m": 999.0,
                "scale": 1.0,
                "tilt_deg": 0.0,
                "rotation_deg": 0.0,
                "yaw_deg": 0.0,
                "T_enu": np.eye(4, dtype=np.float64).tolist(),
            },
        )
        return 0

    # Compute yaw pivot and XY mask bounds strictly from outlier-cleaned cloud
    src_clean_pts = np.asarray(src_clean.points)
    s_min = src_clean_pts.min(axis=0)
    s_max = src_clean_pts.max(axis=0)
    s_ctr = 0.5 * (s_min + s_max)

    # Stage 1: Fast vectorized multi-start coarse search around normalized GPS prior
    src_c = np.asarray(src_clean.voxel_down_sample(0.35).points)
    tgt_c = np.asarray(tgt_clean.voxel_down_sample(0.25).points)
    if len(src_c) < 30 or len(tgt_c) < 30:
        src_c = src_clean_pts
        tgt_c = np.asarray(tgt_clean.points)

    src_rel = src_c - s_ctr
    tree = cKDTree(tgt_c)

    # Initialize best_score=0.0 so zero-match grids preserve the (0,0,0,0) GPS prior instead of teleporting to a corner
    best_score = 0.0
    best_params = (0.0, 0.0, 0.0, 0.0)
    print(
        f"[ICP] Stage 1: Coarse multi-start search ({len(src_c):,} vs {len(tgt_c):,} pts)...",
        flush=True,
    )

    yaw_grid = np.arange(-search_yaw_deg, search_yaw_deg + 0.1, 5.0)
    xy_grid = np.arange(-search_xy_m, search_xy_m + 0.1, 2.0)

    # Auto-detect large vertical barometer drift (> 4m) when XMP RelativeAltitude was unavailable.
    # Always preserve the zero-centered band alongside any shifted band so roof/facade islands
    # (whose median Z legitimately sits above ground-dominated target clouds) never exclude dZ=0.
    xy_mask = (
        (tgt_c[:, 0] >= s_min[0] - 5.0)
        & (tgt_c[:, 0] <= s_max[0] + 5.0)
        & (tgt_c[:, 1] >= s_min[1] - 5.0)
        & (tgt_c[:, 1] <= s_max[1] + 5.0)
    )
    dz_center = 0.0
    if int(np.sum(xy_mask)) >= 50:
        dz_est = float(np.median(tgt_c[xy_mask, 2]) - np.median(src_c[:, 2]))
        if 4.0 < abs(dz_est) <= 40.0:
            dz_center = round(dz_est / 1.5) * 1.5
            print(
                f"[ICP] Detected vertical Z median offset ({dz_est:+.1f}m); searching both 0.0m and {dz_center:+.1f}m Z bands.",
                flush=True,
            )
    z_zero_band = np.arange(-search_z_m, search_z_m + 0.1, 1.5)
    if dz_center != 0.0:
        z_shift_band = np.arange(dz_center - search_z_m, dz_center + search_z_m + 0.1, 1.5)
        z_grid = np.unique(np.round(np.concatenate([z_zero_band, z_shift_band]), 4))
    else:
        z_grid = z_zero_band

    # Deterministically cap Stage-1a query points to <= 3,500 (5-20x faster with identical peak precision)
    if len(src_c) > 3500:
        stride = max(1, len(src_c) // 3500)
        src_q_rel = src_rel[::stride]
    else:
        src_q_rel = src_rel

    # Avoid per-call OS thread-pool spawn overhead across thousands of tiny coarse queries (50x+ faster on Windows)
    coarse_workers = -1 if len(src_q_rel) >= 25000 else 1

    for dyaw in yaw_grid:
        rz_mat = _rot_z(float(dyaw))
        rot_pts = (src_q_rel @ rz_mat.T) + s_ctr
        for dx in xy_grid:
            for dy in xy_grid:
                for dz in z_grid:
                    shifted = rot_pts + np.array([dx, dy, dz], dtype=np.float64)
                    dists, _ = tree.query(shifted, distance_upper_bound=0.50, workers=coarse_workers)
                    inl = dists < 0.50
                    score = float(np.sum(1.0 - (dists[inl] / 0.50) ** 2))
                    if score > best_score:
                        best_score = score
                        best_params = (float(dx), float(dy), float(dz), float(dyaw))

    # Pass 1b: Refine around best coarse peak (0.5m XYZ, 1 deg yaw)
    # CRITICAL: Reset best_score_1b=0.0 because tighter 0.35m kernel values are <= 0.50m scores, while zero-match preserves Pass 1a!
    bx, by, bz, byaw = best_params
    best_score_1b = 0.0
    for dyaw in np.linspace(byaw - 3.0, byaw + 3.0, 7):
        rz_mat = _rot_z(float(dyaw))
        rot_pts = (src_q_rel @ rz_mat.T) + s_ctr
        for dx in np.linspace(bx - 1.5, bx + 1.5, 7):
            for dy in np.linspace(by - 1.5, by + 1.5, 7):
                for dz in np.linspace(bz - 1.5, bz + 1.5, 7):
                    shifted = rot_pts + np.array([dx, dy, dz], dtype=np.float64)
                    dists, _ = tree.query(shifted, distance_upper_bound=0.35, workers=coarse_workers)
                    inl = dists < 0.35
                    score = float(np.sum(1.0 - (dists[inl] / 0.35) ** 2))
                    if score > best_score_1b:
                        best_score_1b = score
                        best_params = (float(dx), float(dy), float(dz), float(dyaw))
    if best_score_1b > 0.0:
        best_score = best_score_1b

    bx, by, bz, byaw = best_params
    rz_mat = _rot_z(byaw)
    t_curr = np.eye(4, dtype=np.float64)
    t_curr[:3, :3] = rz_mat
    t_curr[:3, 3] = s_ctr - (rz_mat @ s_ctr) + np.array([bx, by, bz], dtype=np.float64)
    print(
        f"[ICP] Stage 1 Coarse peak: dXY=({bx:+.2f}, {by:+.2f})m, dZ={bz:+.2f}m, "
        f"dYaw={byaw:+.1f}deg (score={best_score:.1f}/{len(src_q_rel)}, {time.time() - t0:.1f}s)",
        flush=True,
    )

    # Stage 2: Multi-scale Tukey Point-to-Plane ICP + tightly-gated Sim(3) scale refinement
    scale_curr = 1.0
    s_005_cached = None
    t_005_cached = None
    for voxel, max_dist, tukey_k, do_scale in (
        (0.20, 0.80, 0.35, False),
        (0.12, 0.45, 0.20, True),
        (0.08, 0.25, 0.12, True),
        (0.05, 0.15, 0.08, False),
    ):
        # src_clean / tgt_clean are already 5cm voxel downsampled — reuse directly at voxel == 0.05
        if voxel <= 0.051:
            s_ds = src_clean
            t_ds = tgt_clean
        else:
            s_ds = src_clean.voxel_down_sample(voxel)
            t_ds = tgt_clean.voxel_down_sample(voxel)
        if len(s_ds.points) < 50 or len(t_ds.points) < 50:
            continue
        _prepare_normals(t_ds, radius=voxel * 4.0)
        _prepare_normals(s_ds, radius=voxel * 4.0)
        if voxel <= 0.051:
            s_005_cached = s_ds
            t_005_cached = t_ds

        s_scaled = o3d.geometry.PointCloud(s_ds)
        s_scaled.points = o3d.utility.Vector3dVector(np.asarray(s_scaled.points) * scale_curr)

        t_rigid = np.eye(4, dtype=np.float64)
        t_rigid[:3, :3] = t_curr[:3, :3] / scale_curr
        t_rigid[:3, 3] = t_curr[:3, 3]

        loss = o3d.pipelines.registration.TukeyLoss(k=tukey_k)
        est = o3d.pipelines.registration.TransformationEstimationPointToPlane(loss)
        reg = o3d.pipelines.registration.registration_icp(
            s_scaled,
            t_ds,
            max_correspondence_distance=max_dist,
            init=t_rigid,
            estimation_method=est,
            criteria=o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=80),
        )
        t_curr = np.eye(4, dtype=np.float64)
        t_curr[:3, :3] = scale_curr * reg.transformation[:3, :3]
        t_curr[:3, 3] = reg.transformation[:3, 3]

        if do_scale:
            sp = np.asarray(s_ds.points)
            tp = np.asarray(t_ds.points)
            sp_tr = (sp @ t_curr[:3, :3].T) + t_curr[:3, 3]
            tt = cKDTree(tp)
            dd, ii = tt.query(sp_tr, distance_upper_bound=max_dist * 0.7, workers=-1)
            valid = dd < (max_dist * 0.7)
            if int(np.sum(valid)) >= 500:
                s_new, r_new, tr_new = umeyama_sim3(sp[valid], tp[ii[valid]], with_scale=True)
                if 0.85 <= s_new <= 1.15 and r_new[2, 2] > 0.95:
                    scale_curr = s_new
                    t_curr = np.eye(4, dtype=np.float64)
                    t_curr[:3, :3] = scale_curr * r_new
                    t_curr[:3, 3] = tr_new

        print(
            f"  [ICP voxel={voxel:.2f}m] fitness={reg.fitness:.3f} | "
            f"inlier_rmse={reg.inlier_rmse * 100:.2f} cm | scale={scale_curr:.5f} | "
            f"t=({t_curr[0, 3]:+.2f}, {t_curr[1, 3]:+.2f}, {t_curr[2, 3]:+.2f})",
            flush=True,
        )

    # Build final 5cm target KDTree once and reuse across Stage 3 check and final verification
    sp_f = np.asarray(src_clean.points)
    tp_f = np.asarray(tgt_clean.points)
    tt_f = cKDTree(tp_f)

    # Stage 3: Tightly-gated Colored ICP tangent-plane lock (accepted only if geometric error preserved)
    colored_accepted = False
    if colored_refine and src_clean.has_colors() and tgt_clean.has_colors():
        s_col = s_005_cached if s_005_cached is not None else src_clean
        t_col = t_005_cached if t_005_cached is not None else tgt_clean
        if len(s_col.points) >= 100 and len(t_col.points) >= 100:
            if not t_col.has_normals():
                _prepare_normals(t_col, radius=0.20)
            if not s_col.has_normals():
                _prepare_normals(s_col, radius=0.20)
            s_col_scaled = o3d.geometry.PointCloud(s_col)
            s_col_scaled.points = o3d.utility.Vector3dVector(np.asarray(s_col_scaled.points) * scale_curr)
            t_rig_init = np.eye(4, dtype=np.float64)
            t_rig_init[:3, :3] = t_curr[:3, :3] / scale_curr
            t_rig_init[:3, 3] = t_curr[:3, 3]
            try:
                reg_col = o3d.pipelines.registration.registration_colored_icp(
                    s_col_scaled,
                    t_col,
                    max_correspondence_distance=0.12,
                    init=t_rig_init,
                    estimation_method=o3d.pipelines.registration.TransformationEstimationForColoredICP(
                        lambda_geometric=0.968,
                    ),
                    criteria=o3d.pipelines.registration.ICPConvergenceCriteria(
                        relative_fitness=1e-6,
                        relative_rmse=1e-6,
                        max_iteration=50,
                    ),
                )
                t_cand = np.eye(4, dtype=np.float64)
                t_cand[:3, :3] = scale_curr * reg_col.transformation[:3, :3]
                t_cand[:3, 3] = reg_col.transformation[:3, 3]

                d_cur, _ = tt_f.query(
                    (sp_f @ t_curr[:3, :3].T) + t_curr[:3, 3],
                    distance_upper_bound=0.15,
                    workers=-1,
                )
                d_cnd, _ = tt_f.query(
                    (sp_f @ t_cand[:3, :3].T) + t_cand[:3, 3],
                    distance_upper_bound=0.15,
                    workers=-1,
                )
                v_cur, v_cnd = d_cur < 0.15, d_cnd < 0.15
                rmse_cur = float(np.sqrt(np.mean(d_cur[v_cur] ** 2))) if np.any(v_cur) else 999.0
                rmse_cnd = float(np.sqrt(np.mean(d_cnd[v_cnd] ** 2))) if np.any(v_cnd) else 999.0
                fit_cur, fit_cnd = float(np.mean(v_cur)), float(np.mean(v_cnd))
                r_cnd = t_cand[:3, :3] / scale_curr
                tilt_cnd = float(np.degrees(np.arccos(np.clip(r_cnd[2, 2], -1.0, 1.0))))
                print(
                    f"  [Colored ICP Check] P2L: fit={fit_cur * 100:.1f}% rmse={rmse_cur * 100:.2f}cm -> "
                    f"Colored: fit={fit_cnd * 100:.1f}% rmse={rmse_cnd * 100:.2f}cm tilt={tilt_cnd:.2f}deg",
                    flush=True,
                )
                if fit_cnd >= fit_cur - 0.005 and rmse_cnd <= rmse_cur + 0.002 and tilt_cnd <= 5.0:
                    t_curr = t_cand
                    colored_accepted = True
                    print("  [Colored ICP] ACCEPTED (geometric fit preserved + tangent color locked).", flush=True)
                else:
                    print("  [Colored ICP] REJECTED (kept tighter Point-to-Plane pose).", flush=True)
            except Exception as exc:
                print(f"  [Colored ICP] Skipped ({exc})", flush=True)

    # Final geometric verification on 5cm cloud (reusing tt_f)
    sp_tr = (sp_f @ t_curr[:3, :3].T) + t_curr[:3, 3]
    dd, _ = tt_f.query(sp_tr, distance_upper_bound=0.15, workers=-1)
    valid = dd < 0.15
    fitness = float(np.mean(valid))
    rmse_15 = float(np.sqrt(np.mean(dd[valid] ** 2))) if np.any(valid) else 999.0
    med_15 = float(np.median(dd[valid])) if np.any(valid) else 999.0

    r_fin = t_curr[:3, :3] / scale_curr
    rot_deg = float(np.degrees(np.arccos(np.clip((np.trace(r_fin) - 1.0) * 0.5, -1.0, 1.0))))
    yaw_deg = float(np.degrees(np.arctan2(r_fin[1, 0], r_fin[0, 0])))
    tilt_deg = float(np.degrees(np.arccos(np.clip(r_fin[2, 2], -1.0, 1.0))))
    elapsed_s = time.time() - t0

    print(
        f"[ICP COMPLETE] fitness(<15cm)={fitness * 100:.1f}% | RMSE={rmse_15 * 100:.2f} cm | "
        f"MedErr={med_15 * 100:.2f} cm | scale={scale_curr:.5f} | tilt={tilt_deg:.2f} deg "
        f"(yaw={yaw_deg:+.2f} deg) in {elapsed_s:.1f}s",
        flush=True,
    )

    _write_result(
        out_json,
        {
            "ok": True,
            "T_enu": t_curr.tolist(),
            "scale": scale_curr,
            "rotation_deg": rot_deg,
            "yaw_deg": yaw_deg,
            "tilt_deg": tilt_deg,
            "translation_m": t_curr[:3, 3].tolist(),
            "fitness_15cm": fitness,
            "rmse_15cm_m": rmse_15,
            "median_15cm_m": med_15,
            "colored_accepted": colored_accepted,
            "elapsed_s": round(elapsed_s, 2),
        },
    )
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AutoMetashape CPU Open3D Sim(3) ICP Bridge Worker")
    parser.add_argument("--target", required=True, help="Target (main Mission) dense cloud PLY path")
    parser.add_argument("--source", required=True, help="Source (secondary island) dense cloud PLY path")
    parser.add_argument("--output", required=True, help="Output JSON metrics & transform path")
    parser.add_argument(
        "--colored-refine",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run gated Colored ICP tangent refinement after Point-to-Plane ICP (default: True)",
    )
    parser.add_argument("--crop-xy-margin", type=float, default=22.0)
    parser.add_argument("--crop-z-margin", type=float, default=25.0)
    parser.add_argument("--search-xy", type=float, default=8.0)
    parser.add_argument("--search-z", type=float, default=7.5)
    parser.add_argument("--search-yaw", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return run_icp(
        target_ply=args.target,
        source_ply=args.source,
        out_json=args.output,
        colored_refine=args.colored_refine,
        crop_xy_margin=args.crop_xy_margin,
        crop_z_margin=args.crop_z_margin,
        search_xy_m=args.search_xy,
        search_z_m=args.search_z,
        search_yaw_deg=args.search_yaw,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
