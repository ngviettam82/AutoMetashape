# Evaluation Scenarios: Image-to-3D Model Reconstruction

### Scenario 1: Multi-Drone Architectural Building Reconstruction
- **Query**: "Here is my drone image folder at `data/survey/building_a` containing multiple flight subfolders. Reconstruct a 3D model with 16-page 8K textures."
- **Expected behavior**:
  - Automatically identifies separate flight subfolders, initializes independent chunks per flight, and calibrates sensors independently.
  - Registers flight chunks using Point-Based RANSAC ICP (`doc.alignChunks(method=0)`) with automatic Camera/GPS fallback.
  - Automatically calculates `gps-square` horizontal bounding box while preserving full vertical elevation range.
  - Builds CUDA MVS depth maps and fuses into 600,000 polygon faces with Level-30 planar hole closing.
  - Bakes a 16-page 8K ($16 \times 8192 \times 8192$) lossless TIFF texture atlas with mosaic blending, hole filling, and ghosting suppression.
  - Generates `model.obj`, `model.mtl`, `model.tif`, `model.glb`, and native editable `building_reconstruction.psx`.
- **Pass/fail criteria**:
  - Pass: >90% of views registered; 600K face mesh generated; 16-page 8K texture atlas baked; `.glb` and `.psx` created in `model/`.
  - Fail: Duplicate ghost walls from barometric drift; failing to align multiple subfolders; untextured or non-manifold mesh.

### Scenario 2: Aerial Site Survey & Terrain Mapping (Map Preset)
- **Query**: "Process this aerial drone map dataset at `data/survey/site_ortho` using the map preset to give me a high-density terrain mesh and orthomosaic."
- **Expected behavior**:
  - Detects `--preset map` and sets default target faces to 5,000,000 (5M), texture pages to 4, crop method to `none` (uncropped terrain boundary), and enables GeoTIFF orthomosaic.
  - Computes dense depth maps across the complete surveyed footprint without clipping perimeter vegetation or access roads.
  - Meshes arbitrary surface to 5M faces for fine topographic relief.
  - Generates 2D GeoTIFF orthomosaic directly inside the Metashape project and exports `orthomosaic.tif` alongside `orthomosaic.tfw` world file with LZW BigTIFF compression.
- **Pass/fail criteria**:
  - Pass: Complete boundary uncropped; 5M face mesh; `orthomosaic.tif` and `orthomosaic.tfw` generated; saved inside `.psx`.
  - Fail: Scene cropped by building bounding box; orthomosaic missing or failing with uncaught coordinate projection error.

### Scenario 3: Ground Handheld Walkthrough Photogrammetry & Checkpoint Resume
- **Query**: "I took 150 photos walking around a monument without any GPS. Reconstruct it using optical ray convergence, and if interrupted, resume without restarting."
- **Expected behavior**:
  - Detects absence of GPS telemetry and resolves camera trajectory using visual feature matching and adaptive distortion fitting.
  - Applies Iteratively Reweighted Least Squares (IRLS) optical ray convergence (`--crop-method structure`) to isolate the monument from horizon background.
  - When `--resume` is specified, detects completed camera alignment checkpoints and resumes immediately at depth map computation or meshing.
  - Fuses watertight textured 3D model and native `.psx` project.
- **Pass/fail criteria**:
  - Pass: Optical ray convergence successfully centers region; `--resume` skips previously completed stages; `.glb` and `.obj` generated.
  - Fail: Crashing due to missing GPS; `--resume` re-running all stages from scratch; clipping target monument geometry.
