---
name: image-to-3d-model
description: "Reconstructs high-precision 3D textured models (.obj, .mtl, .glb), dense point clouds (.laz, .las, .ply, .e57), digital elevation models (.tif), orthomosaic maps (.tif), and executive PDF calibration reports from raw image folders or drone surveys. Autonomously inspects and learns optimal parameters from dataset telemetry, flight geometry, sensor optics, and camera shutter mechanics (--preset auto / --profile), while supporting specialized presets (Building, Map). Features single-chunk global bundle adjustment, sub-0.5 sharpness filtering, CMOS rolling shutter compensation, MVS depth maps, ground/semantic point classification, and lossless TIFF texture atlases. Use when generating 3D models, classified dense point clouds, digital elevation models, orthomosaics, or calibration reports from drone or camera captures."
audience: user
status: stable
owners:
  - core-engineering
---

# High-Precision 3D Photogrammetry, Survey Telemetry Analysis & Architectural Reconstruction

Autonomous end-to-end photogrammetry and aerial survey pipeline transforming raw image folders (drone surveys, architectural captures, ground inspections) into high-density textured 3D models (`.obj`, `.mtl`, `.glb`), classified dense point clouds (`.laz`, `.las`, `.ply`, `.e57`), Digital Elevation Models (`dem.tif`), GeoTIFF orthomosaic maps (`orthomosaic.tif`), executive calibration quality reports (`report.pdf`), and native Agisoft Metashape Professional projects (`.psx`).

---

## When To Use

Use this skill when:
- The user provides an image folder (drone survey, building inspection, or ground photogrammetry) and requests a 3D model, classified point cloud, digital elevation model, or orthomosaic map.
- The user asks the agent to **learn and determine the best parameters** rather than forcing a rigid preset: the pipeline automatically inspects image telemetry, sensor resolution, camera models, and GPS flight geometry to dynamically configure the optimal reconstruction settings (`--preset auto` or `--profile`).
- Dense 3D Point Clouds are required: generates point clouds directly from depth maps (`chunk.buildPointCloud()`), filters low-confidence points, performs automatic ground classification (`classifyGroundPoints`), or applies AI semantic classification (`classifyPoints`) with export to compressed LAZ, LAS, PLY, E57, or XYZ.
- Topographic Digital Elevation Models (DEM/DSM/DTM) or GeoTIFF orthomosaics are needed with BigTIFF LZW compression and ESRI `.tfw` world positioning.
- Executive PDF Quality & Calibration Reports (`report.pdf`) are needed documenting camera calibration, reprojection errors, ground resolution, and survey overlap.
- Images are located across the main folder and nested subfolders: recursively loads all images into a single global bundle adjustment by default (`--multi-chunk disable`) to guarantee seamless, drift-free alignment and eliminate chunk merging artifacts, with optional multi-chunk isolation (`--multi-chunk auto`).
- Image inputs include any Metashape-supported format: standard raster formats (`.jpg`, `.png`, `.tif`, `.webp`, `.jp2`, `.jxl`, `.exr`), drone and camera RAW formats (`.dng`, `.cr2`, `.cr3`, `.nef`, `.arw`, `.iiq`, `.3fr`), or radiometric thermal formats (`.seq`, `.ara`).
- Automatic blurry image rejection is needed: automatically runs `chunk.analyzeImages()` and disables out-of-focus or motion-blurred photos ($<0.50$ quality index) to protect bundle adjustment accuracy.
- Reconstructing architectural structures, buildings, urban environments, or complex terrain requiring metric accuracy, flat walls, and crisp 90-degree corners.
- Calibrating rolling shutter distortion on consumer drone CMOS sensors and closing localized window openings.
- Requiring optimized meshes (600K faces for buildings, 2M faces for terrain maps) and lossless uncompressed texture atlases (16-page 8K $8192\times 8192$ TIFF mosaic photographic blending with hole filling and ghosting filter).

Do not use this skill when:
- Synthesizing 3D models from single images, sketches, or text prompts without multi-view photographic overlap.
- Running pure 2D image enhancement without 3D geometric triangulation.

---

## Workflow

```mermaid
flowchart TD
    A["Raw Image Directory<br/>(Scanned Recursively across Subfolders)"] --> B["1. Ingestion & Dataset Telemetry Profiling<br/>(EXIF / XMP Optics, GPS Extent, Shutter Analysis)"]
    B --> C["2. Sharpness Quality Estimation & Blurry Rejection<br/>(chunk.analyzeImages: auto-disables photos < 0.50)"]
    C --> D["3. GPU Feature Matching & Camera Alignment<br/>(Single-Chunk Global Bundle Adjustment, 40K/60K Keypoints, 25K Tiepoints, Guided Matching)"]
    D --> E["4. Bounding Box Cropping<br/>(gps-square | none | structure | manual)"]
    E --> F["5. Multi-View Stereo (MVS) Depth Maps<br/>(Medium Quality, Moderate / Aggressive Filtering)"]
    F --> G["6. Dense Point Cloud & Classification<br/>(buildPointCloud, Confidence Filtering, Ground / Semantic AI Classification)"]
    F --> H["7. Arbitrary Surface Meshing<br/>(Depth Maps Fusion, 600K-2M Faces, Planar Hole Closing)"]
    H --> I["8. 8K Photogrammetric Texture Atlas<br/>(4 to 16 Pages, 8192x8192 Lossless TIFF, Mosaic Blending)"]
    F --> J["9. Digital Elevation Model (DEM / DSM)<br/>(buildDem + GeoTIFF LZW Export)"]
    H --> K["10. GeoTIFF Orthomosaic<br/>(buildOrthomosaic + BigTIFF Export)"]
    D --> L["11. Executive PDF Calibration Report<br/>(exportReport: camera errors, overlap, GSD)"]
    G --> M["Production Deliverables: <images_dir>/model/<br/>• model.obj + model.mtl + model.tif<br/>• model.glb<br/>• point_cloud.laz (LAS / PLY / E57)<br/>• dem.tif + dem.tfw<br/>• orthomosaic.tif + orthomosaic.tfw<br/>• report.pdf<br/>• building_reconstruction.psx"]
    H --> M
    I --> M
    J --> M
    K --> M
    L --> M
```

### 1. Autonomous Survey Telemetry Profiling & Parameter Inference

Rather than requiring manual trial-and-error or rigid presets, AutoMetashape features an autonomous telemetry profiling engine (`--profile` and `--preset auto` / `--auto`). An agent or automated pipeline inspects the dataset's EXIF/XMP metadata and mathematically derives the optimal photogrammetric configuration:

#### Telemetry Inference Heuristics

1. **Optical Resolution & Keypoint Budget**:
   - $\text{Megapixels} \ge 30\text{ MP}$ (e.g. 45MP Nikon Z9, 61MP Sony A7R IV, 100MP Phase One): Ingests richer fine details; allocates **60,000 keypoints** and **25,000 tiepoints** with `guided_matching=True` to resolve micro-textures.
   - $\text{Megapixels} < 30\text{ MP}$ (e.g. 12MP / 20MP drone sensors): Allocates **40,000 keypoints** and **25,000 tiepoints** to balance memory and matching speed.

2. **Flight Profile & Survey Geometry**:
   - **Nadir Terrain Survey**: Detected when GPS availability $\ge 70\%$, horizontal geographic span $\ge 140\text{ meters}$, and altitude variance $\le 65\text{ meters}$.
     - *Inferred settings*: `crop_method="none"` (preserves uncropped site boundaries), `build_dem=True` (Digital Elevation Model), `build_orthomosaic=True` (orthophoto mosaic), `build_point_cloud=True`, `classify_ground=True` (ASPRS ground segmentation), `faces=2,000,000`, `texture_pages=4`, `depth_filtering="aggressive"`.
   - **Oblique Architecture / Building Survey**: Detected when GPS availability $\ge 70\%$, but horizontal span $< 140\text{ meters}$ or altitude variance $> 40\text{ meters}$ (indicating multi-tier facade orbits or vertical inspections).
     - *Inferred settings*: `crop_method="gps-square"` (tight horizontal bounding box with uncropped vertical elevation), `faces=600,000`, `texture_pages=16` (multi-page UV atlas for crisp wall details), `close_holes=True` (planar hole closing across window apertures), `depth_filtering="moderate"`.
   - **Terrestrial Close-Range / Handheld Walkthrough**: Detected when GPS availability $< 70\%$ or spatial bounding footprint $< 25\text{ meters}$.
     - *Inferred settings*: `crop_method="structure"` (computes camera optical ray convergence via Iteratively Reweighted Least Squares), `faces=600,000`, `texture_pages=16`, `close_holes=True`.

3. **Sensor Shutter Mechanics**:
   - **Rolling Shutter CMOS**: Automatically identified for consumer drone camera tags (e.g. `FC3582`, `FC3411`, `FC220`, `Mavic`, `Air`, `Mini`). Enables `sensor.rolling_shutter = Metashape.Shutter.Model.Regularized` to eliminate flight-induced wall stepping and reduce reprojection errors by up to 30%.
   - **Global Shutter / Professional Photogrammetry Payloads**: Disabled to prevent over-parameterization.

### 2. Processing Stages

1. **Ingestion & Blurry Image Filtering**:
   - Recursively traverses `<images_dir>` across all subdirectories, ingesting standard rasters (`.jpg`, `.png`, `.tif`, `.webp`, `.exr`), drone RAW formats (`.dng`, `.cr3`, `.nef`, `.arw`), and radiometric sequences (`.seq`, `.ara`).
   - Executes `chunk.analyzeImages()` to calculate edge gradient sharpness.
   - Photos with a quality index below $0.50$ units are automatically disabled (`camera.enabled = False`) to prevent tie-point contamination.

2. **Camera Alignment & Bundle Adjustment**:
   - Matching Quality: Medium (`downscale=2`) for optimal memory scaling.
   - Keypoint Limit: 40,000 (or 60,000 for $>30\text{MP}$); Tiepoint Limit: 25,000 tiepoints.
   - Guided Matching: Enabled by default (`guided_matching=True`) to discover subtle feature correspondences.
   - Stationary Point Filter: Enabled by default (`filter_stationary_points=True`) to reject sensor dust and drone propeller noise.
   - Single-Chunk Default (`--multi-chunk disable`): Solves all images globally in one bundle adjustment to prevent false alignment or chunk-merging offsets.
   - Gradual Tie-Point Selection: Prunes tiepoints with reprojection error $>0.4\text{ px}$ and reconstruction uncertainty $>15$ before final bundle adjustment re-optimization.

3. **Bounding Box Cropping Strategies**:
   - `gps-square` (Default for Building): Horizontal square enclosing all camera GPS coordinates (using Median Absolute Deviation outlier rejection) while leaving vertical altitude $Z$ uncropped.
   - `none` (Default for Map & Aerial Surveys): Retains the entire surveyed spatial footprint without clipping perimeter terrain.
   - `structure` (Iteratively Reweighted Least Squares): Computes the 3D line-of-sight intersection of all camera optical viewing axes.
   - `manual`: Saves project and pauses for manual region box positioning in Metashape Pro GUI.

4. **GPU Depth Maps & Dense Point Cloud**:
   - Computes dense per-pixel depth estimation using CUDA on NVIDIA GPUs.
   - Dense Point Cloud (`buildPointCloud`): Fuses depth maps into a 3D point cloud with RGB vertex colors and confidence indices (`source_data=DepthMapsData, point_colors=True, point_confidence=True`).
   - Confidence Filtering (`--filter-point-confidence <N>`): Filters noisy, low-confidence points ($< N$).
   - Ground Point Classification (`--classify-ground`): Separates bare-earth topography from vegetation and structures using `chunk.point_cloud.classifyGroundPoints(max_angle=15.0, max_distance=1.0, cell_size=50.0)`.
   - AI Semantic Classification (`--classify-points`): Applies multi-class deep learning classification assigning points to ASPRS standard classes (Ground, Low Vegetation, High Vegetation, Building, Road, Car).
   - Point Cloud Export: Exports dense point clouds in `.laz`, `.las`, `.ply`, `.e57`, or `.xyz` with coordinates, colors, normals, confidence, and classifications.

5. **Arbitrary Surface Meshing & Hole Closing**:
   - Fuses depth maps into an arbitrary 3D surface mesh.
   - Target polygon budget: 600K faces for architectural buildings; 2M faces for terrain maps; or dynamically scaled based on photo count ($1,500 - 2,500$ faces per photo).
   - Closes planar voids and window apertures with `chunk.model.closeHoles(level=30)`.
   - Resilient GPU-driver fallback: retries automatically on CPU if next-gen CUDA driver issues occur during octree meshing, restoring GPU acceleration for texture baking.

6. **Photogrammetric Texture Atlas**:
   - Generates non-overlapping UV coordinates with `GenericMapping`.
   - Bakes multi-page 8K ($8192\times 8192$) lossless TIFF texture maps using `MosaicBlending` with `fill_holes=True` and `ghosting_filter=True`.
   - 16 pages for architectural buildings (maximizing facade sharpness); 4 pages for aerial site maps.

7. **Elevation Models, Orthomosaics & Quality Reports**:
   - Digital Elevation Model (DEM / DSM / DTM): Generated via `chunk.buildDem()` using point cloud or depth map elevations; exported as GeoTIFF with BigTIFF LZW compression (`dem.tif` + `dem.tfw`).
   - GeoTIFF Orthomosaic: Generated via `chunk.buildOrthomosaic()` projected onto the surface mesh; exported as GeoTIFF with BigTIFF LZW compression and ESRI world file (`orthomosaic.tif` + `orthomosaic.tfw`).
   - Executive PDF Quality & Calibration Report: Generated via `chunk.exportReport(path, title=..., description=...)` documenting camera calibrations, tie-point statistics, reprojection error histograms, ground sampling distance (GSD), and survey overlap.
   - Production 3D Formats: Exports binary glTF (`model.glb`) with embedded PBR textures, Wavefront OBJ (`model.obj` + `model.mtl` + `model.tif`), and native Agisoft Metashape project (`building_reconstruction.psx`).

### 3. Multi-Project Sequential Batch Processing Protocol

When a parent directory contains multiple independent survey projects (e.g. `Surveys/Project_A/`, `Surveys/Project_B/`):

1. **Inspection**: Enumerate all immediate subdirectories in the parent folder. Verify photo counts and telemetry profiles.
2. **Sequential Execution**:
   - Execute each project independently:
     ```powershell
     & "C:/Program Files/Agisoft/Metashape Pro/metashape.exe" -r scripts/metashape_reconstruction.py --images "<parent>/<project>" --preset auto
     ```
   - Nested subfolders within a project (e.g. `Project_A/Flight_1`, `Project_A/Flight_2`) are solved in a single global bundle adjustment by default, ensuring zero chunk-merging false alignment.
3. **Verification Gate**:
   - Check `<project>/model/` contains: `model.obj`, `model.mtl`, `model.glb`, and `building_reconstruction.psx`.
   - Confirm camera alignment registration rate $\ge 90\%$.
   - Confirm exit code 0 and zero unhandled exceptions.
4. **Progressive Advancement**: Advance to Project $N+1$ only after Project $N$ passes verification.
5. **Batch Summary**: Present a consolidated Markdown table reporting photo counts, aligned cameras, face counts, and deliverable paths.

### 4. CLI Recipes & Usage

```powershell
# Autonomous Telemetry Profiling (Inspect Only):
& "C:/Program Files/Agisoft/Metashape Pro/metashape.exe" -r scripts/metashape_reconstruction.py --images "D:/Surveys/Site_A" --profile

# Autonomous End-to-End Reconstruction (Learned Parameters):
& "C:/Program Files/Agisoft/Metashape Pro/metashape.exe" -r scripts/metashape_reconstruction.py --images "D:/Surveys/Site_A" --preset auto

# Aerial Survey with Classified Dense Point Cloud & DEM:
& "C:/Program Files/Agisoft/Metashape Pro/metashape.exe" -r scripts/metashape_reconstruction.py --images "D:/Surveys/Topography" --preset map --build-point-cloud --point-cloud-format laz --classify-ground --build-dem --export-report

# Building Preset with Executive Calibration Report:
& "C:/Program Files/Agisoft/Metashape Pro/metashape.exe" -r scripts/metashape_reconstruction.py --images "D:/Surveys/HeritageBuilding" --preset building --export-report --report-title "Heritage Building Survey Quality Audit"

# Checkpoint Resume:
& "C:/Program Files/Agisoft/Metashape Pro/metashape.exe" -r scripts/metashape_reconstruction.py --images "D:/Surveys/Site_A" --resume
```

---

## Validation

Reconstruction outputs must satisfy the following quality criteria:
1. **Camera Quality Filtering**: All active cameras must have sharpness quality $\ge 0.50$ (blurry frames disabled).
2. **Camera Registration**: At least $90\%$ of active input images must be successfully registered in space.
3. **Point Cloud Integrity**: When point cloud generation is requested, `point_cloud.laz` (or `.las`, `.ply`, `.e57`) must be generated and non-empty.
4. **Surface Topology**: Arbitrary surface mesh targeting specified face count ($600\text{K} - 2\text{M}$), free from degenerate zero-area faces.
5. **Texture Atlas**: Resolution $\ge 8192\times 8192$ with valid UV mapping, hole filling, and zero unmapped black voids.
6. **Elevation Models & Orthomosaics**: GeoTIFF files (`dem.tif`, `orthomosaic.tif`) exported with accompanying `.tfw` world files and valid spatial resolutions.
7. **Quality Report**: `report.pdf` generated and readable containing camera calibration parameters and error estimates.
8. **Native Project**: Native `.psx` file must be successfully saved and loadable in Agisoft Metashape Professional GUI.

---

## Maintenance

- Ensure `metashape.exe` is installed at `C:/Program Files/Agisoft/Metashape Pro/metashape.exe` or configured on the system PATH.
- Verify NVIDIA GPU CUDA runtime with `nvidia-smi` to ensure Metashape GPU acceleration is active.
- Keep `scripts/metashape_reconstruction.py` updated to match Agisoft Metashape Python API versions.
- Maintain synchronization between `scripts/metashape_reconstruction.py`, `scripts/metashape_export.py`, and `scripts/autometashape_gui.py`.

---

## References

- [evaluations.md](evaluations.md)
- [scripts/metashape_reconstruction.py](../../../scripts/metashape_reconstruction.py)
- [scripts/metashape_export.py](../../../scripts/metashape_export.py)
- [Agisoft Metashape Professional User Manual (v2.x)](https://www.agisoft.com/downloads/user-manuals/)
- [USGS National Geospatial Program Photogrammetry Standards](https://www.usgs.gov/core-science-systems/ngp)
- [ASPRS LAS 1.4 Specification & Point Classification Standards](https://www.asprs.org/divisions-committees/lidar-division/laser-las-file-format-exchange-activities)
