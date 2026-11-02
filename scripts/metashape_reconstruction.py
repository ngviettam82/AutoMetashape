# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape: Autonomous High-Precision Photogrammetry Pipeline using Agisoft Metashape Pro.

Reconstructs production-ready 3D architectural models (.obj, .mtl, .glb, .psx)
directly from raw aerial drone or ground camera image directories on NVIDIA GPUs.
Supports both 'high' and 'ultra' reconstruction grades:
- high: High accuracy alignment, High MVS depth maps (2K), HighFaceCount (~2M faces), 1x 8K texture.
- ultra: High accuracy alignment, Gradual Selection camera optimization, Ultra-High MVS depth maps (4K),
         5,000,000+ custom faces, and multi-page 8K photographic mosaic texture atlas.
"""

import argparse
import math
import os
import sys
import time
import Metashape


def calculate_percentile(sorted_list, p: float) -> float:
    """Calculates percentile p (0.0 to 100.0) from a sorted list of numerical values."""
    if not sorted_list:
        return 0.0
    k = (len(sorted_list) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_list[int(k)]
    d0 = sorted_list[int(f)] * (c - k)
    d1 = sorted_list[int(c)] * (k - f)
    return d0 + d1


def auto_crop_to_main_structure(chunk, padding: float = 0.15) -> bool:
    """Automatically crops chunk.region (bounding box) tightly around the main target structure.

    Computes the least-squares 3D intersection of camera optical viewing axes to pinpoint the
    focal centroid of the survey, filters candidate sparse tie points to eliminate distant horizon/sky
    outliers, and sets chunk.region center and extents with a configurable safety padding.

    Args:
        chunk: Active Metashape.Chunk.
        padding: Safety margin fraction applied around bounding box dimensions (default: 0.15 / 15%).

    Returns:
        True if bounding box was successfully updated; False if skipped.
    """
    aligned_cams = [c for c in chunk.cameras if c.transform]
    if len(aligned_cams) < 2:
        print("  [Auto-Crop] Fewer than 2 aligned cameras; skipping bounding box crop.", flush=True)
        return False

    print(f"\n[Auto-Crop] Calculating 3D camera optical ray convergence from {len(aligned_cams)} cameras...", flush=True)

    # 1. Solve 3D Ray Convergence using Least-Squares Line Intersection
    # Optical ray: L_i(t) = c_i + t * d_i (d_i is normalized forward optical axis)
    # Projection matrix P_i = I - d_i * d_i^T
    # Sum(P_i) * x = Sum(P_i * c_i)
    M = Metashape.Matrix([[0.0] * 3, [0.0] * 3, [0.0] * 3])
    V = Metashape.Vector([0.0, 0.0, 0.0])
    v_z = Metashape.Vector([0.0, 0.0, 1.0])
    cam_centers = []

    for cam in aligned_cams:
        c_pos = cam.center
        cam_centers.append(c_pos)
        d = (cam.transform.rotation() * v_z).normalized()

        # Projection onto plane orthogonal to viewing ray
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

    target_center = None
    try:
        if abs(M.det()) > 1e-4:
            target_center = M.inv() * V
            print(f"  [Auto-Crop] Optical ray convergence centroid: ({target_center.x:.2f}, {target_center.y:.2f}, {target_center.z:.2f})", flush=True)
    except Exception as e:
        print(f"  [Auto-Crop] Optical convergence solve notice: {e}", flush=True)

    if target_center is None:
        target_center = Metashape.Vector([
            sum(c.x for c in cam_centers) / len(cam_centers),
            sum(c.y for c in cam_centers) / len(cam_centers),
            sum(c.z for c in cam_centers) / len(cam_centers)
        ])
        print(f"  [Auto-Crop] Using camera cluster centroid: ({target_center.x:.2f}, {target_center.y:.2f}, {target_center.z:.2f})", flush=True)

    # 2. Estimate survey radius and filter horizon points
    dists = [(c - target_center).norm() for c in cam_centers]
    dists.sort()
    med_dist = dists[len(dists) // 2]
    max_radius = med_dist * 0.85 if med_dist > 0 else float("inf")

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

    # 4. Percentile clipping to reject stray outliers (sky / ground noise)
    min_x = calculate_percentile(local_x, 2.0)
    max_x = calculate_percentile(local_x, 98.0)
    min_y = calculate_percentile(local_y, 2.0)
    max_y = calculate_percentile(local_y, 98.0)
    min_z = calculate_percentile(local_z, 1.0)
    max_z = calculate_percentile(local_z, 99.0)

    # 5. Apply safety padding
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
    print(f"  Isolated Structure Tie Points: {valid_count:,} points", flush=True)
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
    grade: str = "ultra",
    custom_faces: int = 10000000,
    crop_structure: bool = True,
    crop_padding: float = 0.15,
    pause_after_crop: bool = False,
):
    t_start = time.time()
    if not output_dir:
        output_dir = os.path.join(images_dir, "model")
    os.makedirs(output_dir, exist_ok=True)

    print("=========================================================================")
    print(f"  AUTONOMOUS METASHAPE PHOTOGRAMMETRY PIPELINE [GRADE: {grade.upper()}]")
    print("=========================================================================", flush=True)

    # Enable all available NVIDIA GPUs
    Metashape.app.gpu_mask = 1
    gpus = Metashape.app.enumGPUDevices()
    if gpus:
        print(f"[Hardware] GPU: {gpus[0]['name']} ({gpus[0]['mem_size'] / (1024**3):.1f} GB VRAM)", flush=True)
    else:
        print("[Hardware] Running on CPU.", flush=True)

    doc = Metashape.Document()
    chunk = doc.addChunk()
    chunk.label = "Production_Reconstruction"

    # Step 1: Recursively Scan and Ingest Images from Folder & Subfolders
    supported_exts = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
    img_files = []
    abs_output = os.path.abspath(output_dir)
    for root, _dirs, files in os.walk(images_dir):
        # Prevent scanning files in the output directory if located inside images_dir
        if os.path.abspath(root).startswith(abs_output):
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

    # Step 2: Feature Detection & Matching
    print("\n[2/6] Executing GPU Feature Matching (GPS Reference Preselection)...", flush=True)
    t_match = time.time()
    chunk.matchPhotos(
        downscale=1,  # High accuracy full-resolution
        generic_preselection=True,
        reference_preselection=True,
        keypoint_limit=40000,
        tiepoint_limit=4000,
        mask_tiepoints=True,
        filter_stationary_points=True,
    )
    print(f"  Matching finished in {(time.time() - t_match)/60:.2f} min.", flush=True)

    # Step 3: Camera Alignment
    print("\n[3/6] Aligning Cameras & Initial Bundle Adjustment...", flush=True)
    t_align = time.time()
    chunk.alignCameras(adaptive_fitting=True)
    aligned_count = sum(1 for c in chunk.cameras if c.transform)
    print(f"  Alignment finished in {(time.time() - t_align)/60:.2f} min. Aligned: {aligned_count}/{len(chunk.cameras)} cameras.", flush=True)

    # Camera Optimization for Ultra Grade
    if grade == "ultra":
        filter_tie_points(chunk, max_reproj_error=0.4, max_uncertainty=15.0)

    # Step 3b: Automatic Bounding Box Crop to Main Target Structure
    psx_out = os.path.join(output_dir, "building_reconstruction.psx")
    if crop_structure:
        auto_crop_to_main_structure(chunk, padding=crop_padding)

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
        # Reload project if modified in external GUI
        doc.open(psx_out)
        chunk = doc.chunk

    # Step 4: Multi-View Stereo Dense Depth Maps
    depth_downscale = 1 if grade == "ultra" else 2
    print(f"\n[4/6] Computing GPU MVS Depth Maps (Downscale={depth_downscale}, MildFiltering)...", flush=True)
    t_depth = time.time()
    chunk.buildDepthMaps(
        downscale=depth_downscale,
        filter_mode=Metashape.FilterMode.MildFiltering,
        reuse_depth=False,
    )
    print(f"  Depth maps completed in {(time.time() - t_depth)/60:.2f} min.", flush=True)

    # Step 5: High-Precision Arbitrary Surface Meshing
    print(f"\n[5/6] Reconstructing Arbitrary Surface Mesh from Depth Maps...", flush=True)
    t_mesh = time.time()
    if grade == "ultra" and custom_faces > 0:
        face_count_arg = custom_faces
        print(f"  Target Polygon Count: {custom_faces:,} faces", flush=True)
    else:
        face_count_arg = Metashape.FaceCount.HighFaceCount
        print(f"  Target Polygon Count: HighFaceCount preset", flush=True)

    chunk.buildModel(
        surface_type=Metashape.SurfaceType.Arbitrary,
        source_data=Metashape.DataSource.DepthMapsData,
        interpolation=Metashape.Interpolation.EnabledInterpolation,
        face_count=face_count_arg,
    )
    mesh_verts = len(chunk.model.vertices)
    mesh_faces = len(chunk.model.faces)
    print(f"  Mesh completed in {(time.time() - t_mesh)/60:.2f} min! Geometry: {mesh_verts:,} vertices, {mesh_faces:,} faces.", flush=True)

    # Step 6: Multi-Page 8K Photogrammetric Texture Atlas
    tex_pages = 4 if grade == "ultra" else 1
    print(f"\n[6/6] Baking {tex_pages}x 8192x8192 (8K) Photogrammetric Texture Atlas (Mosaic Blending)...", flush=True)
    t_tex = time.time()
    chunk.buildUV(
        mapping_mode=Metashape.MappingMode.GenericMapping,
        page_count=tex_pages,
        texture_size=8192,
    )
    chunk.buildTexture(
        blending_mode=Metashape.BlendingMode.MosaicBlending,
        texture_size=8192,
        fill_holes=True,
        ghosting_filter=True,
    )
    print(f"  Texture baking finished in {(time.time() - t_tex)/60:.2f} min.", flush=True)

    # Production Asset Export
    print("\n=========================================================================")
    print("  EXPORTING FINAL PRODUCTION ASSETS")
    print("=========================================================================", flush=True)
    glb_out = os.path.join(output_dir, "model.glb")
    obj_out = os.path.join(output_dir, "model.obj")

    chunk.exportModel(glb_out, format=Metashape.ModelFormat.ModelFormatGLTF)
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
        description="Autonomous Metashape 3D Reconstruction Pipeline (Default: Ultra Grade, 10M Faces, TIFF Textures, Auto-Crop)"
    )
    parser.add_argument("--images", required=True, help="Input directory containing raw images (scanned recursively across all subfolders)")
    parser.add_argument("--output", default=None, help="Output directory for 3D assets (default: <images_dir>/model)")
    parser.add_argument("--grade", choices=["high", "ultra"], default="ultra", help="Reconstruction grade preset (default: ultra)")
    parser.add_argument("--faces", type=int, default=10000000, help="Custom face count for ultra grade (default: 10000000 / 10M)")
    parser.add_argument("--no-crop-structure", dest="crop_structure", action="store_false", help="Disable automatic bounding box crop to main structure")
    parser.set_defaults(crop_structure=True)
    parser.add_argument("--crop-padding", type=float, default=0.15, help="Safety margin padding factor around main structure (default: 0.15 / 15%%)")
    parser.add_argument("--pause-after-crop", action="store_true", help="Pause after alignment and crop for manual bounding box adjustment in GUI")
    args = parser.parse_args()

    run_reconstruction(
        images_dir=args.images,
        output_dir=args.output,
        grade=args.grade,
        custom_faces=args.faces,
        crop_structure=args.crop_structure,
        crop_padding=args.crop_padding,
        pause_after_crop=args.pause_after_crop,
    )
