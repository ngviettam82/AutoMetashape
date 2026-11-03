# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape: Autonomous High-Precision Photogrammetry Pipeline using Agisoft Metashape Pro.

Reconstructs production-ready 3D architectural models (.obj, .mtl, .glb, .psx)
directly from raw aerial drone or ground camera image directories on NVIDIA GPUs.
Configured with production-grade defaults:
- Image Alignment: Medium quality (downscale=2), 40,000 keypoint limit, 25,000 tiepoint limit,
                   stationary tie points excluded, guided matching disabled, adaptive fitting enabled.
- Camera Optimization: Rolling shutter compensation enabled, Gradual Selection tie-point filtering.
- Bounding Box: gps-square auto-crop (smallest enclosing square of camera GPS poses + tie-point altitude bounds).
- Depth Maps & Meshing: Medium quality depth maps (downscale=4), Moderate depth filtering, 1,000,000 (1M) custom faces.
- Texture Atlas: 16-page 8K photographic mosaic blending texture atlas (hole filling and ghosting filter enabled).
"""

import argparse
import math
import os
import sys
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
    """Automatically crops chunk.region (bounding box) tightly around the main target structure.

    Uses Robust IRLS ray convergence to lock onto the majority target structure (even when 10-30%
    of cameras are aimed at secondary objects) and Median Absolute Deviation (MAD) spatial clustering
    to exclude all tie points belonging to external areas.

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

    local_x, local_y, local_z = [], [], []
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
        local_z.append(loc.z)
        valid_count += 1

    if valid_count < 100:
        print(f"  [Auto-Crop] Insufficient structure tie points ({valid_count}); preserving default region.", flush=True)
        return False

    local_x.sort()
    local_y.sort()
    local_z.sort()

    # 4. Robust MAD bounding: rejects secondary cluster points with 50% breakdown tolerance
    min_x, max_x = robust_bounds(local_x, k=3.0)
    min_y, max_y = robust_bounds(local_y, k=3.0)
    min_z, max_z = robust_bounds(local_z, k=3.0)

    # 5. Apply safety padding around the main structure
    span_x = (max_x - min_x) * (1.0 + 2.0 * padding)
    span_y = (max_y - min_y) * (1.0 + 2.0 * padding)
    span_z = (max_z - min_z) * (1.0 + 2.0 * padding)

    mid_local = Metashape.Vector([
        (min_x + max_x) * 0.5,
        (min_y + max_y) * 0.5,
        (min_z + max_z) * 0.5
    ])
    new_center = rot * mid_local
    new_size = Metashape.Vector([span_x, span_y, span_z])

    # Assign new region
    region = chunk.region
    region.center = new_center
    region.size = new_size
    chunk.region = region

    print(f"  [Auto-Crop] Successfully cropped region to main structure!", flush=True)
    print(f"  Center: ({new_center.x:.2f}, {new_center.y:.2f}, {new_center.z:.2f})", flush=True)
    print(f"  Size:   ({new_size.x:.2f} x {new_size.y:.2f} x {new_size.z:.2f}) [Padding: {int(padding * 100)}%]", flush=True)
    print(f"  Isolated Main Structure Tie Points: {valid_count:,} points", flush=True)
    return True


def auto_crop_camera_gps_square(chunk, padding: float = 0.05) -> bool:
    """Crops chunk.region to the smallest enclosing square containing camera GPS (X, Y) poses,
    with vertical bounds set by the minimum and maximum tie-point altitudes inside that square.

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

    # 2. Extract altitude range from tie points strictly inside the horizontal square
    pts = chunk.tie_points.points if chunk.tie_points else []
    if not pts:
        print("  [Auto-Crop-GPS] No tie points available; preserving default region.", flush=True)
        return False

    inside_z = []
    for pt in pts:
        if not pt.valid:
            continue
        loc = rot_t * pt.coord[:3]
        if sq_min_x <= loc.x <= sq_max_x and sq_min_y <= loc.y <= sq_max_y:
            inside_z.append(loc.z)

    if len(inside_z) < 50:
        print(f"  [Auto-Crop-GPS] Insufficient tie points inside square ({len(inside_z)}); preserving default region.", flush=True)
        return False

    inside_z.sort()
    # Robust lowest (ground) and highest (roof/spire) altitude: 1st to 99th percentile to exclude stray noise
    n_z = len(inside_z)
    min_z = inside_z[max(0, int(n_z * 0.01))]
    max_z = inside_z[min(n_z - 1, int(n_z * 0.99))]

    span_z = (max_z - min_z) * (1.0 + 2.0 * padding)
    mid_z = (min_z + max_z) * 0.5

    # 3. Assign new region
    mid_local = Metashape.Vector([mid_x, mid_y, mid_z])
    new_center = rot * mid_local
    new_size = Metashape.Vector([side, side, span_z])

    region = chunk.region
    region.center = new_center
    region.size = new_size
    chunk.region = region

    print(f"  [Auto-Crop-GPS] Successfully cropped region to Camera GPS Square!", flush=True)
    print(f"  Square Center: ({new_center.x:.2f}, {new_center.y:.2f}, {new_center.z:.2f})", flush=True)
    print(f"  Square Extents: ({side:.2f}m x {side:.2f}m x {span_z:.2f}m) [Padding: {int(padding * 100)}%]", flush=True)
    print(f"  Altitude Range: [{min_z:.2f}m (ground) to {max_z:.2f}m (roof)]", flush=True)
    print(f"  Tie Points Inside Square: {len(inside_z):,} points", flush=True)
    return True


def filter_tie_points(chunk, max_reproj_error: float = 0.4, max_uncertainty: float = 15.0):
    """Filters low-confidence tie points using Gradual Selection criteria."""
    print("[Gradual Selection] Filtering sparse tie points for camera optimization...", flush=True)
    f = Metashape.TiePoints.Filter()

    # 1. Reconstruction Uncertainty filter
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ReconstructionUncertainty)
        f.removePoints(max_uncertainty)
        print(f"  Applied Reconstruction Uncertainty threshold: {max_uncertainty}", flush=True)
    except Exception as e:
        print(f"  Reconstruction Uncertainty filter skipped: {e}", flush=True)

    # 2. Reprojection Error filter
    try:
        f.init(chunk, criterion=Metashape.TiePoints.Filter.ReprojectionError)
        f.removePoints(max_reproj_error)
        print(f"  Applied Reprojection Error threshold: {max_reproj_error} px", flush=True)
    except Exception as e:
        print(f"  Reprojection Error filter skipped: {e}", flush=True)

    # Optimize camera parameters
    print("  Optimizing camera lens parameters...", flush=True)
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
    )
    print("  Camera optimization complete.", flush=True)


def run_reconstruction(
    images_dir: str,
    output_dir: str = None,
    align_quality: str = "medium",
    keypoint_limit: int = 40000,
    tiepoint_limit: int = 25000,
    filter_stationary_points: bool = True,
    guided_matching: bool = False,
    adaptive_fitting: bool = True,
    filter_tiepoints: bool = True,
    depth_quality: str = "medium",
    depth_filtering: str = "moderate",
    custom_faces: int = 1000000,
    texture_pages: int = 16,
    fill_holes: bool = True,
    ghosting_filter: bool = True,
    rebake_texture: bool = False,
    crop_method: str = "gps-square",
    crop_padding: float = 0.05,
    min_quality: float = 0.5,
    rolling_shutter: bool = True,
    close_holes: bool = True,
    pause_after_crop: bool = False,
    force_restart: bool = False,
):
    t_start = time.time()
    if not output_dir:
        output_dir = os.path.join(images_dir, "model")
    os.makedirs(output_dir, exist_ok=True)
    psx_out = os.path.join(output_dir, "building_reconstruction.psx")

    print("=========================================================================")
    print(f"  AUTONOMOUS METASHAPE PHOTOGRAMMETRY PIPELINE [ARCHITECTURAL GRADE]")
    print("=========================================================================", flush=True)
    print(f"  Align Quality:      {align_quality.upper()} (Downscale: {ALIGN_DOWNSCALE_MAP.get(align_quality.lower(), 2)})")
    print(f"  Keypoints / Tie:    {keypoint_limit:,} / {tiepoint_limit:,}")
    print(f"  Exclude Stationary: {filter_stationary_points} | Guided Matching: {guided_matching} | Adaptive Fitting: {adaptive_fitting}")
    print(f"  Filter Tie Points:  {filter_tiepoints}")
    print(f"  Depth Maps Quality: {depth_quality.upper()} (Downscale: {DEPTH_DOWNSCALE_MAP.get(depth_quality.lower(), 4)}) | Filtering: {depth_filtering.capitalize()}")
    print(f"  Target Mesh Faces:  {custom_faces:,} (1M)")
    print(f"  Texture Atlas:      {texture_pages} pages @ 8192x8192 (8K) | Fill Holes: {fill_holes} | Ghosting Filter: {ghosting_filter}")
    print(f"  Rolling Shutter:    {rolling_shutter} | Hole Closing: {close_holes}")
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
    if not force_restart and os.path.exists(psx_out):
        print(f"\n[Checkpoint] Found existing project: {psx_out}", flush=True)
        try:
            doc.open(psx_out)
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
        chunk = doc.addChunk()
        chunk.label = "Production_Reconstruction"

    if not resumed:
        # Step 1: Recursively Scan and Ingest Images from Folder & Subfolders
        supported_exts = SUPPORTED_IMAGE_EXTS
        img_files = []
        abs_output = os.path.abspath(output_dir)
        for root, dirs, files in os.walk(images_dir):
            # Prune output directory and any existing model export directories
            dirs[:] = [
                d for d in dirs
                if not os.path.abspath(os.path.join(root, d)).startswith(abs_output)
                and not d.lower().startswith("model")
            ]
            if os.path.abspath(root).startswith(abs_output) or os.path.basename(root).lower().startswith("model"):
                continue
            for f in files:
                if f.lower().endswith(supported_exts):
                    img_files.append(os.path.join(root, f))
        img_files = sorted(img_files)

        if not img_files:
            raise ValueError(f"No valid image files found recursively in '{images_dir}'!")

        print(f"\n[1/6] Ingesting {len(img_files)} images found recursively across: {images_dir}", flush=True)
        chunk.addPhotos(img_files)
        print(f"  Successfully loaded {len(chunk.cameras)} cameras.", flush=True)

        # Step 1b: Image Sharpness Quality Estimation & Blurry Image Filtering
        if min_quality > 0.0:
            filter_image_quality(chunk, min_quality=min_quality)

        # Step 2: Feature Detection & Matching
        align_downscale = ALIGN_DOWNSCALE_MAP.get(align_quality.lower(), 2)
        print(f"\n[2/6] Executing GPU Feature Matching (Downscale: {align_downscale}, Keypoints: {keypoint_limit:,}, Tiepoints: {tiepoint_limit:,})...", flush=True)
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

        # Step 3: Camera Alignment
        print(f"\n[3/6] Aligning Cameras & Initial Bundle Adjustment (Adaptive Fitting: {adaptive_fitting})...", flush=True)
        t_align = time.time()
        chunk.alignCameras(adaptive_fitting=adaptive_fitting)
        aligned_count = sum(1 for c in chunk.cameras if c.transform and c.enabled)
        enabled_count = sum(1 for c in chunk.cameras if c.enabled)
        print(f"  Alignment finished in {(time.time() - t_align)/60:.2f} min. Aligned: {aligned_count}/{enabled_count} active cameras.", flush=True)

        # Camera Optimization with Rolling Shutter Compensation & Gradual Selection
        if rolling_shutter:
            for s in chunk.sensors:
                s.rolling_shutter = Metashape.Shutter.Model.Regularized
            print("  [Rolling Shutter] Enabled Regularized CMOS rolling shutter compensation.", flush=True)

        if filter_tiepoints:
            filter_tie_points(chunk, max_reproj_error=0.4, max_uncertainty=15.0)
        else:
            print("  [Gradual Selection] Tie-point cleaning and camera re-optimization skipped (--no-filter-tiepoints).", flush=True)

        # Step 3b: Automatic Bounding Box Crop (Default: gps-square)
        if crop_method == "gps-square":
            auto_crop_camera_gps_square(chunk, padding=crop_padding)
        elif crop_method == "structure":
            auto_crop_to_main_structure(chunk, padding=crop_padding)
        else:
            print("  [Auto-Crop] Cropping disabled; using default full-scene bounding box.", flush=True)

        # Save project checkpoint immediately after alignment and cropping
        doc.save(psx_out)
        print(f"[CHECKPOINT] Saved alignment checkpoint to: {psx_out}", flush=True)

        # Optional interactive pause for manual boundary fine-tuning
        if pause_after_crop:
            print("\n=========================================================================")
            print("  INTERACTIVE BOUNDING BOX INSPECTION")
            print("  Project checkpoint saved to:")
            print(f"  --> {psx_out}")
            print("  You may open this project in Metashape GUI to inspect or fine-tune the")
            print("  reconstruction box. If satisfied, save in GUI and return here.")
            print("=========================================================================", flush=True)
            input("Press [Enter] to continue with GPU MVS Depth Maps and Surface Meshing...")
            doc.open(psx_out)
            chunk = doc.chunk

    # Step 4: Multi-View Stereo Dense Depth Maps
    has_depth_maps = chunk.depth_maps is not None
    if has_depth_maps:
        print(f"\n[4/6] Reusing existing MVS Depth Maps from checkpoint ({len(chunk.depth_maps)} depth maps).", flush=True)
    else:
        depth_downscale = DEPTH_DOWNSCALE_MAP.get(depth_quality.lower(), 4)
        filter_mode_arg = DEPTH_FILTER_MAP.get(depth_filtering.lower(), Metashape.FilterMode.ModerateFiltering)
        print(f"\n[4/6] Computing GPU MVS Depth Maps (Quality: {depth_quality.capitalize()} [Downscale={depth_downscale}], Filtering: {depth_filtering.capitalize()})...", flush=True)
        t_depth = time.time()
        chunk.buildDepthMaps(
            downscale=depth_downscale,
            filter_mode=filter_mode_arg,
            reuse_depth=False,
        )
        print(f"  Depth maps completed in {(time.time() - t_depth)/60:.2f} min.", flush=True)
        doc.save(psx_out)
        print(f"[CHECKPOINT] Saved depth maps checkpoint to: {psx_out}", flush=True)

    # Step 5: High-Precision Arbitrary Surface Meshing
    has_model = chunk.model is not None
    if has_model:
        print(f"\n[5/6] Reusing existing Surface Mesh from checkpoint ({len(chunk.model.vertices):,} vertices, {len(chunk.model.faces):,} faces).", flush=True)
    else:
        print(f"\n[5/6] Reconstructing Arbitrary Surface Mesh from Depth Maps (Target: {custom_faces:,} faces)...", flush=True)
        t_mesh = time.time()
        if custom_faces > 0:
            face_count_arg = Metashape.FaceCount.CustomFaceCount
            face_count_custom_arg = custom_faces
            print(f"  Target Polygon Count: {custom_faces:,} faces", flush=True)
        else:
            face_count_arg = Metashape.FaceCount.MediumFaceCount
            face_count_custom_arg = 200000
            print(f"  Target Polygon Count: MediumFaceCount preset", flush=True)

        try:
            chunk.buildModel(
                surface_type=Metashape.SurfaceType.Arbitrary,
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
                Metashape.app.gpu_mask = 0  # Disable GPU acceleration for meshing
                chunk.buildModel(
                    surface_type=Metashape.SurfaceType.Arbitrary,
                    source_data=Metashape.DataSource.DepthMapsData,
                    interpolation=Metashape.Interpolation.EnabledInterpolation,
                    face_count=face_count_arg,
                    face_count_custom=face_count_custom_arg,
                    vertex_colors=True,
                )
                print("  [Fallback] CPU surface meshing succeeded!", flush=True)
            finally:
                Metashape.app.gpu_mask = orig_gpu_mask  # Restore GPU for texture baking
                print("  [Hardware] Restored GPU acceleration for texture baking.", flush=True)
        if close_holes and chunk.model:
            chunk.model.closeHoles(level=30)
            print("  Applied planar hole closing across window and surface voids (level 30%).", flush=True)

        mesh_verts = len(chunk.model.vertices)
        mesh_faces = len(chunk.model.faces)
        print(f"  Mesh completed in {(time.time() - t_mesh)/60:.2f} min! Geometry: {mesh_verts:,} vertices, {mesh_faces:,} faces.", flush=True)
        doc.save(psx_out)
        print(f"[CHECKPOINT] Saved mesh model checkpoint to: {psx_out}", flush=True)

    # Step 6: Multi-Page 8K Photogrammetric Texture Atlas
    current_pages = 0
    if chunk.model:
        if hasattr(chunk.model, "textures") and chunk.model.textures:
            current_pages = len(chunk.model.textures)
        elif hasattr(chunk.model, "texture") and chunk.model.texture():
            current_pages = 1

    if current_pages == texture_pages and not rebake_texture:
        print(f"\n[6/6] Reusing existing Texture Atlas from checkpoint ({current_pages} pages).", flush=True)
    else:
        if current_pages > 0 and current_pages != texture_pages:
            print(f"\n[6/6] Existing texture atlas has {current_pages} page(s), re-baking to requested {texture_pages} pages...", flush=True)
        elif rebake_texture:
            print(f"\n[6/6] Force re-baking texture atlas as requested ({texture_pages} pages)...", flush=True)
        else:
            print(f"\n[6/6] Baking {texture_pages}x 8192x8192 (8K) Photogrammetric Texture Atlas (Mosaic Blending, Hole Filling: {fill_holes}, Ghosting Filter: {ghosting_filter})...", flush=True)

        t_tex = time.time()
        chunk.buildUV(
            mapping_mode=Metashape.MappingMode.GenericMapping,
            page_count=texture_pages,
            texture_size=8192,
        )
        chunk.buildTexture(
            blending_mode=Metashape.BlendingMode.MosaicBlending,
            texture_size=8192,
            fill_holes=fill_holes,
            ghosting_filter=ghosting_filter,
        )
        print(f"  Texture baking finished in {(time.time() - t_tex)/60:.2f} min.", flush=True)
        doc.save(psx_out)
        print(f"[CHECKPOINT] Saved textured model checkpoint to: {psx_out}", flush=True)

    # Production Asset Export
    print("\n=========================================================================")
    print("  EXPORTING FINAL PRODUCTION ASSETS")
    print("=========================================================================")
    glb_out = os.path.join(output_dir, "model.glb")
    obj_out = os.path.join(output_dir, "model.obj")

    chunk.exportModel(
        glb_out,
        format=Metashape.ModelFormat.ModelFormatGLTF,
        embed_texture=True,
        save_normals=True,
        save_colors=True,
    )
    print(f"[EXPORT] Saved binary GLB: {glb_out} ({os.path.getsize(glb_out)/(1024*1024):.2f} MB)", flush=True)

    chunk.exportModel(
        obj_out,
        format=Metashape.ModelFormat.ModelFormatOBJ,
        texture_format=Metashape.ImageFormat.ImageFormatTIFF,
        save_texture=True,
        save_normals=True,
        save_colors=True,
    )
    print(f"[EXPORT] Saved Wavefront OBJ + TIFF: {obj_out} ({os.path.getsize(obj_out)/(1024*1024):.2f} MB)", flush=True)

    doc.save(psx_out)
    print(f"[EXPORT] Saved Metashape Pro Native Project: {psx_out}", flush=True)

    total_time = (time.time() - t_start) / 60.0
    print(f"\n[SUCCESS] Entire pipeline finished in {total_time:.2f} minutes!", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Autonomous Metashape 3D Reconstruction Pipeline (Medium Alignment Quality, 40K Keypoints, 25K Tiepoints, Exclude Stationary Points, Moderate Depth Filtering, 1M Faces, GPS-Square Crop)"
    )
    parser.add_argument("--images", required=True, help="Input directory containing raw images (scanned recursively across all subfolders)")
    parser.add_argument("--output", default=None, help="Output directory for 3D assets (default: <images_dir>/model)")
    parser.add_argument("--align-quality", choices=["highest", "high", "medium", "low"], default="medium", help="Image alignment accuracy/quality (default: medium)")
    parser.add_argument("--keypoint-limit", type=int, default=40000, help="Max keypoints per image (default: 40000)")
    parser.add_argument("--tiepoint-limit", type=int, default=25000, help="Max tiepoints per image (default: 25000)")
    parser.add_argument("--depth-quality", choices=["ultra", "high", "medium", "low"], default="medium", help="Depth map quality (default: medium)")
    parser.add_argument("--depth-filtering", choices=["mild", "moderate", "aggressive"], default="moderate", help="Depth map filtering mode (default: moderate)")
    parser.add_argument("--faces", type=int, default=1000000, help="Target polygon face count (default: 1000000 / 1M)")
    parser.add_argument("--guided-matching", action="store_true", default=False, help="Enable guided image matching (default: False)")
    parser.add_argument("--no-filter-stationary", dest="filter_stationary_points", action="store_false", default=True, help="Disable stationary tie point exclusion (default: stationary points are excluded)")
    parser.add_argument("--no-adaptive-fitting", dest="adaptive_fitting", action="store_false", default=True, help="Disable adaptive camera model fitting (default: enabled)")
    parser.add_argument(
        "--no-filter-tiepoints",
        dest="filter_tiepoints",
        action="store_false",
        default=True,
        help="Disable Gradual Selection tie-point filtering and camera re-optimization (keeps all aligned tie points intact)",
    )
    parser.add_argument("--no-rolling-shutter", dest="rolling_shutter", action="store_false", default=True, help="Disable CMOS rolling shutter regularized compensation (default: enabled)")
    parser.add_argument("--no-close-holes", dest="close_holes", action="store_false", default=True, help="Disable mesh hole closing (default: enabled at level 30)")
    parser.add_argument("--texture-pages", type=int, default=16, help="Number of 8K texture atlas pages (default: 16)")
    parser.add_argument("--no-fill-holes", dest="fill_holes", action="store_false", default=True, help="Disable texture hole filling (default: enabled)")
    parser.add_argument("--no-ghosting-filter", dest="ghosting_filter", action="store_false", default=True, help="Disable ghosting filter in texture blending (default: enabled)")
    parser.add_argument("--rebake-texture", action="store_true", default=False, help="Force re-baking texture atlas even if one already exists in checkpoint")
    parser.add_argument(
        "--crop-method",
        choices=["gps-square", "structure", "none"],
        default="gps-square",
        help="Bounding box crop method: 'gps-square' (smallest enclosing square of camera GPS poses with tie-point altitude bounds), 'structure' (optical ray convergence on main building walls), or 'none' (default: gps-square)",
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
    parser.add_argument("--force-restart", action="store_true", help="Force restart reconstruction from scratch, ignoring existing checkpoints")
    args = parser.parse_args()

    crop_method = "none" if args.no_crop else args.crop_method
    min_quality = 0.0 if args.disable_quality_filter else args.min_quality

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
        force_restart=args.force_restart,
    )
