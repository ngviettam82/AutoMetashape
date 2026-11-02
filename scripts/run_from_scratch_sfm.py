# Copyright (c) 2026, AETHER Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

import os, sys, subprocess, time

WORKSPACE = r"c:\Users\ADMIN\Documents\Code\GaussianSplat"
COLMAP_EXE = os.path.join(WORKSPACE, "bin", "colmap", "bin", "colmap.exe")
IMAGES_DIR = os.path.join(WORKSPACE, "data", "toanha1", "images")
SFM_DIR = os.path.join(WORKSPACE, "data", "from_scratch_sfm")
DB_PATH = os.path.join(SFM_DIR, "database.db")
PAIRS_PATH = os.path.join(SFM_DIR, "hybrid_pairs.txt")
SPARSE_DIR = os.path.join(SFM_DIR, "sparse")

def run_cmd(cmd_list, desc):
    print(f"\n=======================================================")
    print(f"[RUNNING] {desc}")
    print(f"Command: {' '.join(cmd_list)}")
    print(f"=======================================================", flush=True)
    t0 = time.time()
    p = subprocess.Popen(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    
    last_lines = []
    count = 0
    for line in iter(p.stdout.readline, ''):
        line_str = line.strip()
        count += 1
        if any(keyword in line_str.lower() for keyword in ["processed file", "elapsed time", "registered", "bundle adjustment", "iter", "images:"]):
            if "processed file" in line_str.lower():
                if count % 25 == 0 or "978" in line_str:
                    print(f"  > {line_str}", flush=True)
            else:
                print(f"  > {line_str}", flush=True)
        last_lines.append(line_str)
        if len(last_lines) > 20:
            last_lines.pop(0)
            
    p.wait()
    t1 = time.time()
    if p.returncode != 0:
        print(f"\n[ERROR] {desc} failed with exit code {p.returncode}")
        print("Last lines of output:")
        for l in last_lines:
            print("  ", l)
        sys.exit(p.returncode)
    print(f"[SUCCESS] {desc} completed in {t1 - t0:.2f} seconds ({ (t1 - t0)/60:.2f} min).", flush=True)

def main():
    if not os.path.exists(COLMAP_EXE):
        print(f"[ERROR] COLMAP executable not found at {COLMAP_EXE}")
        sys.exit(1)
        
    os.makedirs(SFM_DIR, exist_ok=True)
    os.makedirs(SPARSE_DIR, exist_ok=True)
    
    if os.path.exists(DB_PATH):
        print(f"Cleaning existing database: {DB_PATH}")
        os.remove(DB_PATH)

    # 1. Feature Extraction (SIFT multi-threaded across 20 cores, 2000px max dimension)
    extract_cmd = [
        COLMAP_EXE, "feature_extractor",
        "--database_path", DB_PATH,
        "--image_path", IMAGES_DIR,
        "--ImageReader.single_camera", "1",
        "--ImageReader.camera_model", "SIMPLE_RADIAL",
        "--FeatureExtraction.use_gpu", "0",
        "--FeatureExtraction.num_threads", "20",
        "--FeatureExtraction.max_image_size", "2000",
        "--SiftExtraction.max_num_features", "8192"
    ]
    run_cmd(extract_cmd, "Stage 1: Multi-threaded SIFT Feature Extraction (978 images)")

    # 2. Two-View Feature Matching (using the 16.5k Hybrid Pairs)
    if os.path.exists(PAIRS_PATH):
        match_cmd = [
            COLMAP_EXE, "matches_importer",
            "--database_path", DB_PATH,
            "--match_list_path", PAIRS_PATH,
            "--match_type", "pairs",
            "--FeatureMatching.use_gpu", "0",
            "--FeatureMatching.num_threads", "20"
        ]
        run_cmd(match_cmd, "Stage 2: Guided Two-View Matching (16.5k Hybrid Pairs)")
    else:
        match_cmd = [
            COLMAP_EXE, "spatial_matcher",
            "--database_path", DB_PATH,
            "--SpatialMatching.max_distance", "35",
            "--FeatureMatching.use_gpu", "0",
            "--FeatureMatching.num_threads", "20"
        ]
        run_cmd(match_cmd, "Stage 2: Spatial Proximity Matching")

    # 3. Incremental Structure-from-Motion (Mapper)
    mapper_cmd = [
        COLMAP_EXE, "mapper",
        "--database_path", DB_PATH,
        "--image_path", IMAGES_DIR,
        "--output_path", SPARSE_DIR,
        "--Mapper.ba_refine_principal_point", "1",
        "--Mapper.ba_refine_extra_params", "1",
        "--Mapper.min_num_matches", "15",
        "--Mapper.num_threads", "20"
    ]
    run_cmd(mapper_cmd, "Stage 3: Incremental Bundle Adjustment (SfM)")

    # 4. Model Converter to PLY for 3DGS
    sparse0 = os.path.join(SPARSE_DIR, "0")
    if os.path.exists(sparse0):
        ply_path = os.path.join(sparse0, "points3D.ply")
        convert_cmd = [
            COLMAP_EXE, "model_converter",
            "--input_path", sparse0,
            "--output_path", ply_path,
            "--output_type", "PLY"
        ]
        run_cmd(convert_cmd, "Stage 4: Export Sparse Point Cloud to PLY")
        
        # 5. Model Converter to Text format
        convert_txt_cmd = [
            COLMAP_EXE, "model_converter",
            "--input_path", sparse0,
            "--output_path", sparse0,
            "--output_type", "TXT"
        ]
        run_cmd(convert_txt_cmd, "Stage 5: Export Cameras and Poses to TXT")
        
        print(f"\n[DONE] From-scratch SfM sparse reconstruction finished successfully!")
        print(f"Cameras, poses, and points saved in: {sparse0}")

if __name__ == "__main__":
    main()
