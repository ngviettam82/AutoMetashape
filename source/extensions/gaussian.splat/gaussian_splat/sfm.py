# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Structure from Motion (SfM) runner wrapping COLMAP and PyCOLMAP."""

import os
import subprocess
import sys
import time


def run_sfm_pipeline(
    colmap_exe: str,
    images_dir: str,
    output_dir: str,
    hybrid_pairs_path: str = None,
    num_threads: int = 20,
    max_image_size: int = 2000,
) -> bool:
    """Executes feature extraction, matching, and incremental bundle adjustment."""
    db_path = os.path.join(output_dir, "database.db")
    sparse_dir = os.path.join(output_dir, "sparse")
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(sparse_dir, exist_ok=True)

    if os.path.exists(db_path):
        os.remove(db_path)

    # 1. Feature Extraction
    extract_cmd = [
        colmap_exe,
        "feature_extractor",
        "--database_path",
        db_path,
        "--image_path",
        images_dir,
        "--ImageReader.single_camera",
        "1",
        "--ImageReader.camera_model",
        "SIMPLE_RADIAL",
        "--FeatureExtraction.use_gpu",
        "0",
        "--FeatureExtraction.num_threads",
        str(num_threads),
        "--FeatureExtraction.max_image_size",
        str(max_image_size),
        "--SiftExtraction.max_num_features",
        "8192",
    ]
    subprocess.run(extract_cmd, check=True)

    # 2. Two-view matching
    if hybrid_pairs_path and os.path.exists(hybrid_pairs_path):
        match_cmd = [
            colmap_exe,
            "matches_importer",
            "--database_path",
            db_path,
            "--match_list_path",
            hybrid_pairs_path,
            "--match_type",
            "pairs",
            "--FeatureMatching.use_gpu",
            "0",
            "--FeatureMatching.num_threads",
            str(num_threads),
        ]
    else:
        match_cmd = [
            colmap_exe,
            "spatial_matcher",
            "--database_path",
            db_path,
            "--SpatialMatching.max_distance",
            "35",
            "--FeatureMatching.use_gpu",
            "0",
            "--FeatureMatching.num_threads",
            str(num_threads),
        ]
    subprocess.run(match_cmd, check=True)

    # 3. Incremental Mapper
    mapper_cmd = [
        colmap_exe,
        "mapper",
        "--database_path",
        db_path,
        "--image_path",
        images_dir,
        "--output_path",
        sparse_dir,
        "--Mapper.ba_refine_principal_point",
        "1",
        "--Mapper.ba_refine_extra_params",
        "1",
        "--Mapper.min_num_matches",
        "15",
        "--Mapper.num_threads",
        str(num_threads),
    ]
    subprocess.run(mapper_cmd, check=True)

    # 4. Model converter
    sparse0 = os.path.join(sparse_dir, "0")
    if os.path.exists(sparse0):
        ply_path = os.path.join(sparse0, "points3D.ply")
        subprocess.run(
            [colmap_exe, "model_converter", "--input_path", sparse0, "--output_path", ply_path, "--output_type", "PLY"],
            check=True,
        )
        subprocess.run(
            [colmap_exe, "model_converter", "--input_path", sparse0, "--output_path", sparse0, "--output_type", "TXT"],
            check=True,
        )
        return True
    return False
