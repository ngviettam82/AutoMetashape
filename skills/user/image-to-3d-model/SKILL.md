---
name: image-to-3d-model
description: "Reconstructs a production-ready textured 3D model (.obj, .mtl, .glb) and 3D Gaussian Splatting radiance field directly from an image set or drone aerial survey, automating telemetry extraction, GPS spatial pair matching, bundle adjustment, and surface-aligned mesh baking. Use when provided an image folder or aerial capture to generate a textured 3D model."
audience: user
status: stable
owners:
  - core-engineering
---

# Image-to-3D Model Reconstruction

Autonomous end-to-end photogrammetry and radiance field reconstruction runbook for transforming raw image collections into textured 3D models (`.obj`, `.mtl`, `.glb`) and 3D Gaussian Splatting (`.ply`) assets.

---

## When To Use

Use this skill when:
- The user provides an image folder (drone survey, hand-held camera walkthrough, or ground photogrammetry) and requests a 3D model with texture.
- Processing aerial or oblique drone flights with consumer GNSS errors or intermittent GPS shadow.
- Requiring both real-time neural radiance fields (`.ply` / `.splat`) and traditional polygon meshes (`.obj` / `.glb`) for Blender, Unreal Engine, or CAD.
- Executing 100% open-source, from-scratch photogrammetry without commercial software dependencies.

Do not use this skill when:
- Synthesizing single-image or text-to-3D generative models without multi-view multi-angle photographic overlap.
- Running pure 2D image enhancement or super-resolution without spatial triangulation.

---

## Workflow

### 1. Telemetry Ingestion & Spatial Preprocessing
1. Scan the input image directory:
   ```bash
   python -m gaussian_splat.cli --images <path/to/images> --output <path/to/output>
   ```
2. The pipeline automatically inspects EXIF and XMP tags for GNSS coordinates, relative altitude, and gimbal pitch/yaw.
3. Frames with missing GPS (`0.0, 0.0`) are automatically connected to adjacent frames via the **Temporal Flight Graph** ($|i - j| \le 8$).
4. Frames with active GPS are clustered using 3D Euclidean spatial proximity ($d \le 30\text{m}$), generating the optimized `hybrid_pairs.txt` graph.

### 2. Open-Source Structure from Motion (SfM)
1. Runs multi-threaded SIFT feature extraction into a local SQLite database (`database.db`).
2. Executes two-view RANSAC geometric verification across the hybrid pairs.
3. Solves camera intrinsics ($f_x, f_y, c_x, c_y, k_1, k_2$), extrinsics ($R, t$), and triangulates sparse 3D tie points (`sparse/0/points3D.ply`).

### 3. Surface-Aligned Gaussian Splatting & Texture Baking
1. Spawns planar Gaussian surfels initialized from the sparse tie points.
2. Optimizes surface radiance and geometry using `gsplat` with normal consistency regularization.
3. Generates a watertight polygon triangle mesh via Poisson surface reconstruction.
4. Unwraps UV coordinates and bakes multi-view high-resolution photographic textures into `texture.jpg`.
5. Exports production assets:
   - `model.obj` + `model.mtl` + `texture.jpg`
   - `model.glb` (self-contained glTF binary)
   - `points3D.ply` (sparse & dense radiance field point cloud)

---

## Validation

Verify reconstruction quality through the following gates:
1. **Pose Registration**: At least $90\%$ of input images must be successfully registered in `sparse/0/images.bin`.
2. **Reprojection Error**: Mean bundle adjustment reprojection error must be under $1.2\text{ pixels}$.
3. **Geometry Integrity**: The exported `model.obj` or `model.glb` must be non-empty, watertight, and load without vertex winding errors.
4. **Texture Completeness**: The baked `texture.jpg` must have valid UV mapping without unpainted black patches.

---

## Maintenance

- Ensure `colmap.exe` binary in `bin/colmap/` or system PATH matches version $4.0+$ with CUDA support.
- If images have non-standard EXIF structures, update `source/extensions/gaussian.splat/gaussian_splat/telemetry.py`.
- Keep PyTorch CUDA runtime aligned with host NVIDIA GPU driver.

---

## References

- [2D Gaussian Splatting for Geometrically Accurate Radiance Fields](https://github.com/hbb1/2d-gaussian-splatting) (SIGGRAPH 2024)
- [SuGaR: Surface-Aligned Gaussian Splatting](https://github.com/Anttwo/SuGaR) (CVPR 2024)
- [gsplat: CUDA Acceleration Library for Radiance Fields](https://github.com/nerfstudio-project/gsplat)
- [COLMAP: Structure-from-Motion and Multi-View Stereo](https://colmap.github.io/)
