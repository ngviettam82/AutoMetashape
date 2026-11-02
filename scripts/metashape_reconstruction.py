# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Autonomous High-Precision Photogrammetry Pipeline using Agisoft Metashape Pro.

Reconstructs production-ready 3D architectural models (.obj, .mtl, .glb, .psx)
directly from raw aerial drone or ground camera image directories on NVIDIA GPUs.
Supports both 'high' and 'ultra' reconstruction grades:
- high: High accuracy alignment, High MVS depth maps (2K), HighFaceCount (~2M faces), 1x 8K texture.
- ultra: High accuracy alignment, Gradual Selection camera optimization, Ultra-High MVS depth maps (4K),
         5,000,000+ custom faces, and multi-page 8K photographic mosaic texture atlas.
"""

import argparse
import os
import sys
import time
import Metashape


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


def run_reconstruction(images_dir: str, output_dir: str, grade: str = "ultra", custom_faces: int = 10000000):
    t_start = time.time()
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

    # Step 1: Scan and Ingest Images
    supported_exts = (".jpg", ".jpeg", ".png", ".tif", ".tiff")
    img_files = sorted([
        os.path.join(images_dir, f)
        for f in os.listdir(images_dir)
        if f.lower().endswith(supported_exts)
    ])
    if not img_files:
        raise ValueError(f"No valid image files found in '{images_dir}'!")

    print(f"\n[1/6] Ingesting {len(img_files)} images from: {images_dir}", flush=True)
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
    psx_out = os.path.join(output_dir, "building_reconstruction.psx")

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
    parser = argparse.ArgumentParser(description="Autonomous Metashape 3D Reconstruction Pipeline (Default: Ultra Grade, 10M Faces, TIFF Textures)")
    parser.add_argument("--images", required=True, help="Input directory containing raw images")
    parser.add_argument("--output", default="data/final_model", help="Output directory for 3D assets (default: data/final_model)")
    parser.add_argument("--grade", choices=["high", "ultra"], default="ultra", help="Reconstruction grade preset (default: ultra)")
    parser.add_argument("--faces", type=int, default=10000000, help="Custom face count for ultra grade (default: 10000000 / 10M)")
    args = parser.parse_args()

    run_reconstruction(
        images_dir=args.images,
        output_dir=args.output,
        grade=args.grade,
        custom_faces=args.faces,
    )
