# Evaluation Scenarios: Image-to-3D Model Reconstruction

### Scenario 1: Multi-Drone Architectural Building Reconstruction
- **Query**: "Here is my drone image folder at `data/survey/building_a` containing multiple flight subfolders. Reconstruct a 3D model with 16-page 8K textures."
- **Expected behavior**:
  - Automatically loads all photos across subfolders into unified global bundle adjustment (default `--multi-chunk disable` ensures zero chunk-merging false alignment artifacts).

  - Automatically calculates `gps-square` horizontal bounding box while preserving full vertical elevation range.
  - Builds CUDA MVS depth maps and fuses into a 2,000,000 (2M) polygon face arbitrary 3D mesh (customizable via `--faces`).
  - Bakes a 16-page 8K ($16 \times 8192 \times 8192$) texture atlas (`--texture-format tiff` or default `png`) with mosaic blending, hole filling, and ghosting suppression.
  - Generates `<project_name>.obj`, `<project_name>.mtl`, 8K texture pages, `<project_name>.glb` (when `--export-glb` is passed), and native editable `<project_name>.psx`.
- **Pass/fail criteria**:
  - Pass: >90% of views registered; 2M face mesh generated; 16-page 8K texture atlas baked; `.obj` and `.psx` created in target output directory.
  - Fail: Duplicate ghost walls from barometric drift; failing to align multiple subfolders; untextured or non-manifold mesh.

### Scenario 2: Aerial Site Survey & Terrain Mapping (Pro Preset)
- **Query**: "Process this aerial drone map dataset at `data/survey/site_ortho` using the pro preset to give me a high-density terrain mesh and orthomosaic."
- **Expected behavior**:
  - Uses `--preset pro` and applies the Production Geomatics standard: 2,000,000 (2M) face arbitrary 3D mesh, deterministic 16-page 8K texture atlas, gentle tie-point filtering, and native metric 5 cm/px GeoTIFF orthomosaic generation (with `--build-dem` opt-in).
  - Computes dense depth maps and meshes arbitrary surface to 2M faces for fine topographic relief.
  - Generates 2D GeoTIFF orthomosaic directly inside the Metashape project and exports `<project_name>_orthomosaic.tif` alongside `.tfw` and `.prj` files with LZW BigTIFF compression at `0.05 m/px` in projected UTM CRS.
- **Pass/fail criteria**:
  - Pass: 2M face mesh; `<project_name>_orthomosaic.tif`, `.tfw`, and `.prj` generated at 5 cm/px; saved inside `<project_name>.psx`.
  - Fail: Orthomosaic missing, exported in unprojected degrees, or failing with uncaught coordinate projection error.

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


### Scenario 4: Sequential Multi-Project Batch Loop with Per-Project Verification Gate
- **Query**: "There is a folder at `D:/Surveys/Batch_March`, containing subfolders that are each an independent project. Loop workflow for each project using the default building config, process every project, and check every project that has been done to see if there is any wrong. Must process each project one at a time, then once the project is done and verified to be correct, process the next project, until all projects are done. List this folder for the projects."
- **Expected behavior**:
  - Enumerate all immediate subfolders inside `D:/Surveys/Batch_March` and present a structured list of detected projects with photo counts to the user.
  - Sequentially process Project 1 with `scripts/metashape_reconstruction.py --images "D:/Surveys/Batch_March/<Project_1>"`. All subfolder images within `Project_1` are solved in a single global bundle adjustment by default (`--multi-chunk disable`), avoiding chunk merging errors.
  - Verify Project 1 deliverables: confirm `<project_name>.obj`, `<project_name>.mtl`, 16-page 8K textures, `<project_name>_orthomosaic.tif`, and `<project_name>.psx` exist and are non-empty; verify registration $\ge 90\%$.
  - Only after Project 1 verification passes, advance to Project 2 and repeat.
  - After all projects finish, output a comprehensive summary table with deliverable paths and quality metrics.
- **Pass/fail criteria**:
  - Pass: All projects processed sequentially; each project verified before moving to the next; subfolder photos aligned in global bundle adjustment; zero project collisions.
  - Fail: Passing the batch root folder directly into `--images` (which erroneously fuses separate projects together); proceeding to next project without verifying the current one; unhandled project crash halting subsequent projects without diagnosis.


### Scenario 5: Pro Photogrammetry Survey Physics Audit & USGS 4-Pass Optimization
- **Query**: "I have an aerial survey folder at `data/survey/quarry_inspection`. Do not use a hardcoded preset; act as a senior photogrammetrist, audit the survey optics, calculate the true GSD, run USGS gradual selection bundle adjustment, and reconstruct 3D assets with dense point cloud, DEM, and quality report."
- **Expected behavior**:
  - Uses `--preset pro` (or `--profile`), inspecting image telemetry, sensor hardware pitch, optical GSD in cm/px, flying height $H_{AGL}$, forward/lateral overlap, and shutter mechanism.
  - Automatically identifies camera shutter physics (e.g. disables rolling shutter compensation on mechanical leaf shutters like DJI FC3411, Phantom 4 Pro, or Zenmuse L2; preserves rolling shutter on Hasselblad Mavic 3 L2D-20c).
  - Executes the USGS 4-Pass Gradual Selection protocol (`RU <= 15.0`, `PA <= 3.0`, `RE <= 0.50`, `ImageCount >= 2`, capped at `<= 10%` per pass) with staged camera parameter unfreezing ($f, cx, cy \to k_1..k_3, p_1..p_2 \to k_4, b_1, b_2$ with adaptive fitting and tie point covariance).
  - Opts into `buildPointCloud` (`--build-point-cloud --export-point-cloud`), slope-adaptive ground classification (`--classify-ground`), `buildDem` (`--build-dem --export-dem`), `buildOrthomosaic` (`--export-ortho`), and exports an executive PDF quality report (`--export-report`).
  - Outputs the executive Geomatics Engineering Summary with initial vs optimized RMS reprojection errors.
- **Pass/fail criteria**:
  - Pass: Profiling audits optics and GSD correctly; executes USGS 4-pass optimization reducing RMS reprojection error; deliverables include `<project_name>.laz`, `<project_name>_dem.tif`, `<project_name>_orthomosaic.tif`, `<project_name>_report.pdf`, `<project_name>.obj`, and `<project_name>.psx`.
  - Fail: Blindly falling back to rigid manual presets; failing to extract EXIF telemetry; fitting distortion coefficients to high-uncertainty tie points; crashing during point cloud or DEM generation.

### Scenario 6: Dense Point Cloud Generation & Ground Point Classification
- **Query**: "Process this drone photogrammetry dataset at `data/survey/forest_clearing` to generate a 3D point cloud with automatic bare-earth ground classification and export it to compressed LAZ format."
- **Expected behavior**:
  - Runs with `--build-point-cloud --point-cloud-format laz --classify-ground`.
  - Generates dense point cloud from depth maps with RGB vertex colors and confidence indices.
  - Executes `chunk.point_cloud.classifyGroundPoints(max_angle=15.0, max_distance=1.0, cell_size=50.0)`.
  - Exports georeferenced point cloud to `model/point_cloud.laz` with embedded classification, confidence, colors, and normal vectors.
- **Pass/fail criteria**:
  - Pass: `point_cloud.laz` is exported and non-empty; ground points classified into ASPRS Class 2 (Ground); project checkpoint saved.
  - Fail: Point cloud missing; ground classification skipped; unhandled exception during export.


### Scenario 7: Hierarchical 3D Tiled Model (Cesium 3D Tiles) & Topographic Contours
- **Query**: "Process this open-pit mine drone survey to generate streaming Cesium 3D Tiles and 1-meter topographic contour vectors for our GIS portal."
- **Expected behavior**:
  - Executes with `--build-tiled-model --tiled-model-format cesium --build-dem --build-contours --contour-interval 1.0 --contours-format shp`.
  - Builds multiresolution hierarchical LOD 3D tiles and exports `model/tiled_model/tileset.json` with `.b3dm` tile payloads.
  - Builds DEM and generates vector contour polylines with 1.0m elevation intervals, exporting `model/contours.shp` (or `.dxf`).
- **Pass/fail criteria**:
  - Pass: `tileset.json` created with valid LOD bounding hierarchy; `contours.shp` exported with elevation attributes; project saved in `.psx`.
  - Fail: Missing tileset or unhandled contour generation crash.

### Scenario 8: Target Marker Detection, GCP Georeferencing & Texture Delighting
- **Query**: "Reconstruct this heritage temple facade. We placed circular 12-bit coded targets and have ground surveyed GCP coordinates in `gcps.csv`. Also remove harsh direct sunlight shadows from the texture atlas."
- **Expected behavior**:
  - Executes with `--detect-markers --target-type circular12 --import-reference "gcps.csv" --remove-lighting --export-fbx`.
  - Automatically identifies circular coded targets on photos, loads CSV reference coordinates, and updates bundle adjustment transformation.
  - Generates 8K texture atlas and applies `chunk.removeLighting()` to eliminate cast shadows and ambient occlusion.
  - Exports production assets including `model.fbx`, `model.glb`, and `model.obj`.
- **Pass/fail criteria**:
  - Pass: Markers detected; reference imported; diffuse albedo texture delighted; FBX and GLB exported.
  - Fail: Failing to detect targets; crashing on reference CSV import; shadow removal failure.
