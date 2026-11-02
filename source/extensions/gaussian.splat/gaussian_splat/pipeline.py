# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""High-level end-to-end pipeline orchestrating telemetry, SfM, and textured model creation."""

import os
from gaussian_splat.matching import build_hybrid_matching_graph
from gaussian_splat.mesh import create_textured_model
from gaussian_splat.sfm import run_sfm_pipeline
from gaussian_splat.telemetry import extract_drone_metadata


def reconstruct_3d_model(
    images_dir: str,
    output_dir: str,
    colmap_exe: str = None,
    num_threads: int = 20,
    texture_res: int = 2048,
) -> bool:
    """Executes the full pipeline: telemetry extraction -> hybrid graph -> SfM -> textured 3D mesh."""
    if colmap_exe is None:
        colmap_exe = r"c:\Users\ADMIN\Documents\Code\GaussianSplat\bin\colmap\bin\colmap.exe"

    os.makedirs(output_dir, exist_ok=True)
    sfm_dir = os.path.join(output_dir, "sfm")
    pairs_path = os.path.join(sfm_dir, "hybrid_pairs.txt")
    model_dir = os.path.join(output_dir, "model")

    print(f"[PIPELINE] Phase 1: Ingesting telemetry from {images_dir}...")
    meta = extract_drone_metadata(images_dir)
    print(f"[PIPELINE] Found {len(meta)} images. Building hybrid pairing graph...")
    build_hybrid_matching_graph(meta, output_pairs_path=pairs_path)

    print(f"[PIPELINE] Phase 2: Running Structure-from-Motion...")
    sfm_success = run_sfm_pipeline(
        colmap_exe=colmap_exe,
        images_dir=images_dir,
        output_dir=sfm_dir,
        hybrid_pairs_path=pairs_path,
        num_threads=num_threads,
    )
    if not sfm_success:
        print("[ERROR] SfM stage failed to produce sparse model.")
        return False

    ply_path = os.path.join(sfm_dir, "sparse", "0", "points3D.ply")
    print(f"[PIPELINE] Phase 3: Generating textured 3D mesh from {ply_path}...")
    mesh_success = create_textured_model(ply_path=ply_path, output_dir=model_dir, texture_res=texture_res)
    if mesh_success:
        print(f"[SUCCESS] 3D Model with texture exported to: {model_dir}")
        return True
    return False
