# AutoMetashape

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Release](https://img.shields.io/badge/Release-v1.4.0-green.svg)](https://github.com/ngviettam82/AutoMetashape/releases/tag/v1.4.0)
[![Python](https://img.shields.io/badge/Python-3.10%2B-informational.svg)](https://www.python.org/)
[![Hardware](https://img.shields.io/badge/CUDA-Accelerated-76B900.svg?logo=nvidia)](https://developer.nvidia.com/cuda-zone)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)]()
[![Engine](https://img.shields.io/badge/Engine-Agisoft%20Metashape%20Pro%202.x-orange.svg)](https://www.agisoft.com/)

**AutoMetashape** is an autonomous, high-precision photogrammetry and aerial survey pipeline that transforms drone imagery and ground camera captures into millimeter-accurate 3D models (`.obj`, `.mtl`, `.glb`, `.fbx`, `.ply`), classified dense point clouds (`.laz`, `.las`, `.ply`, `.e57`), hierarchical 3D Tiled Models (Cesium 3D Tiles, TLS, SLPK), Digital Elevation Models (`dem.tif`), topographic contour vector maps (`.shp`, `.dxf`), GeoTIFF orthomosaics (`.tif`, `.tfw`), executive calibration reports (`report.pdf`), and native Agisoft Metashape projects (`.psx`) with **NVIDIA CUDA** GPU acceleration.

Available both as an interactive desktop studio application (**AutoMetashape Studio**) and as a scriptable command-line interface.

---

## Download v1.4.0

Download the standalone v1.4.0 release package directly from GitHub Releases:

📦 **[AutoMetashape-v1.4.0.zip](https://github.com/ngviettam82/AutoMetashape/releases/download/v1.4.0/AutoMetashape-v1.4.0.zip)**  
Release details and release notes: [GitHub Release v1.4.0](https://github.com/ngviettam82/AutoMetashape/releases/tag/v1.4.0)

---

## Quickstart

### 0. 1-Click In-Place Updater (Zero Git Dependency)

Update any node (coordinator or remote worker) directly from GitHub `main` with zero folder nesting:
```powershell
# Update in-place:
.\update.bat

# Update and immediately launch worker daemon:
.\update.bat --worker

# Update and immediately launch cluster server:
.\update.bat --server
```

### 1. Launch Distributed Cluster Daemons (1-Click)

**Worker Node Workstations (Run on any network PC with a GPU):**
```powershell
# Windows 1-Click (interactively saves configuration on first run):
.\launch_worker.bat
```

**Coordinator Workstation (Starts the Cluster Server):**
```powershell
# Windows 1-Click:
.\launch_server.bat
```

### 2. Autonomous CLI Execution

Run photogrammetry reconstruction directly from PowerShell or terminal. Standard Python automatically discovers Metashape Pro across all installed drives and delegates execution seamlessly:

```powershell
# Pro Photogrammetry & Geomatics Engine (Default):
python scripts/metashape_reconstruction.py --images "C:/path/to/your/images" --preset pro

# Drone Survey with Dense Point Cloud, DEM, Contours & Cesium 3D Tiles:
& "C:/Program Files/Agisoft/Metashape Pro/metashape.exe" -r scripts/metashape_reconstruction.py \
    --images "C:/path/to/your/images" \
    --preset pro \
    --build-point-cloud --build-dem --build-contours --build-tiled-model

# Building Facade with Coded Target Detection, Delighting & FBX Export:
& "C:/Program Files/Agisoft/Metashape Pro/metashape.exe" -r scripts/metashape_reconstruction.py \
    --images "C:/path/to/your/images" \
    --preset building \
    --detect-markers --remove-lighting --export-fbx
```

All production deliverables are automatically placed in `<export_folder>/<project_name>/` (or `<images_dir>/<project_name>/`).

---

## Key Features

### 1. Complete Agisoft Metashape Processing Pipeline
AutoMetashape supports all core photogrammetric operations in Agisoft Metashape Professional:
- **🎯 Target & Marker Detection (`--detect-markers`, `--target-type`)**: Automatically identifies coded targets (Circular 12, 14, 16, 20-bit, Cross targets, AprilTags 16h5, 25h9, 36h11) across photos via `chunk.detectMarkers()`.
- **📍 GCP Reference & RTK Georeferencing (`--import-reference`)**: Imports ground survey control points (GCPs) or antenna offsets CSV/TXT via `chunk.importReference()` and computes coordinate system transformations.
- **🎨 Radiometric Color Calibration (`--calibrate-colors`)**: Equalizes camera sensor exposure, vignetting, and white balance across photos via `chunk.calibrateColors()`.
- **☁️ Dense Point Clouds & Semantic AI (`--build-point-cloud`, `--classify-ground`, `--classify-points`)**: Computes dense 3D point clouds with RGB colors, filters low-confidence points, classifies bare-earth ground topography, and applies deep learning multi-class semantic segmentation (ASPRS LiDAR classes: Ground, Vegetation, Building, Road, Car).
- **🌐 Hierarchical 3D Tiled Models (`--build-tiled-model`, `--tiled-model-format`)**: Generates multiresolution LOD tilesets in Cesium 3D Tiles (`tileset.json` + `b3dm`) for streaming in CesiumJS and Unreal Engine, Agisoft TLS (`.tls`), Esri Scene Layer Package (`.slpk`), or OpenSceneGraph (`.osgb`).
- **🗺️ Digital Elevation Models (DEM) & Topographic Contours (`--build-dem`, `--build-contours`)**: Reconstructs metric elevation rasters (GeoTIFF with BigTIFF LZW) and vector contour lines at custom intervals (0.5m, 1m, 2m, 5m) exported as ESRI Shapefile (`.shp`), AutoCAD (`.dxf`), GeoJSON, or KML.
- **☀️ Texture Delighting / Shadow Removal (`--remove-lighting`)**: Removes harsh sunlight shadows and ambient occlusion using `chunk.removeLighting()` to output true photographic diffuse albedo textures for game engines and VFX.
- **📐 Mesh Topology, Smoothing & Multi-Format Export**: Arbitrary or HeightField meshing, planar hole closing (`closeHoles`), Laplacian mesh smoothing (`--smooth-mesh`), decimation (`--decimate-mesh`), and exports to Autodesk FBX (`.fbx`), Stanford PLY (`.ply`), AutoCAD DXF (`.dxf`), binary glTF (`.glb`), and Wavefront OBJ (`.obj` + `.mtl` + `.tif`).

### 2. Pro Photogrammetry & Geomatics Engine
- **🔬 Survey Physics & Optics Telemetry Audit (`--profile`, `--preset pro`, Default)**:
  - Audits physical sensor geometry, pixel pitch in $\mu\text{m}$, mean flying height $H_{AGL}$, and calculates the exact Ground Sampling Distance (GSD in cm/pixel).
  - Evaluates pairwise camera baselines to estimate forward and lateral overlap percentages.
  - Samples luminance histograms to detect underexposed shadows ($Y < 20$) and highlight clipping ($Y > 235$), triggering texture delighting when shadow clipping exceeds $15\%$.
- **⚙️ Camera Shutter Physics Conformance**:
  - Automatically identifies mechanical leaf shutters (e.g. DJI FC3411, Phantom 4 Pro FC6310, Zenmuse P1, Phase One, Hasselblad) and **disables rolling shutter compensation** to eliminate synthetic bundle adjustment drift.
  - Enables rolling shutter compensation only on CMOS rolling shutter payloads (DJI Mini, Mavic Air, consumer smartphones).
- **📐 USGS 4-Pass Gradual Selection Bundle Adjustment**:
  - Staged parameter unfreezing schedule: Pass 1 (RU $\le 12 \to f, cx, cy$) $\to$ Pass 2 (PA $\le 2.5\text{ px} \to k_1..k_3, p_1, p_2$) $\to$ Pass 3 (RE $\le 0.35\text{ px} \to k_4, b_1, b_2$, adaptive fitting) $\to$ Pass 4 (Image multiplicity $\ge 3$).
  - Reports initial vs optimized RMS reprojection errors with reduction percentages.
- **🎨 1:1 Ground Texel Density Budgeting**:
  - Dynamically calculates the exact 8K texture atlas page count needed to match optical camera resolution on the 3D mesh surface, preventing blurry textures or bloated empty UV sheets.
- **⛰️ Slope-Adaptive Ground Point Filtering & Morphological Meshing**:
  - Derives terrain relief ratio $R = \Delta Z / D_{diag}$: sets planar sites ($R < 0.08$) to `HeightField` meshing with large ground cell sizes ($35\text{m}$), and complex 3D structures ($R \ge 0.08$) to `Arbitrary` meshing with steep slope angles ($28^\circ$).
- **🏢 Building Preset (`--preset building`)**:
  - 600K polygon faces, 16-page 8K texture atlas, Level-30 planar hole closing, and `gps-square` bounding box.
- **🗺️ Aerial Map Preset (`--preset map`)**:
  - 2M polygon faces, 4-page 8K texture atlas, full uncropped terrain extent, and GeoTIFF orthomosaic.

### 3. Robust Single-Chunk Global Alignment & Multi-Chunk Registration
- **Single-Chunk Global Alignment (Default, `--multi-chunk disable`)**: To prevent false alignment artifacts or coordinate shifts caused by chunk merging, AutoMetashape defaults to loading all photos across all subfolders into a single unified chunk, solving bundle adjustment globally.
- **Optional Multi-Chunk Mode (`--multi-chunk auto`)**: When enabled, automatically assigns nested flight subfolders (`flight_1/`, `flight_2/`) to independent chunks and solves sensor calibrations independently.
- **Camera/GPS-Based Registration (`method=2`, Default)**: Aligns secondary chunks to the anchor flight using camera reference coordinates to prevent 180-degree rotation flips on symmetric structures.
- **Point-Based RANSAC ICP Fallback (`method=0`)**: If camera reference coordinates are unavailable or degraded, automatically falls back to point-based ICP (`fit_scale=True`).

### 4. Comprehensive Image & Sensor Format Ingestion
Recursively scans image folders and nested directories for all supported formats:
- **Standard Raster**: `.jpg`, `.jpeg`, `.png`, `.tif`, `.tiff`, `.bmp`, `.webp`, `.jp2`, `.jxl`, `.exr`, `.tga`
- **Drone & Camera RAW**: DJI DNG RAW (Mavic 3 Enterprise, Matrice 300/350 RTK, Phantom 4 RTK, Autel EVO II/MAX), Canon CR2/CR3, Nikon NEF, Sony ARW, Phase One IIQ, Hasselblad 3FR, Fujifilm RAF, Panasonic RW2
- **Radiometric Thermal**: FLIR SEQ, ARA

---

## Distributed Network Processing & Resilient Worker Launcher

AutoMetashape runs as a lean, production-grade, headless engine orchestrated directly by AI Agents and CLI workflows:

- **1-Click Resilient Worker Node (`launch_worker.bat`)**:
  - Automatically loads server address and shared network root from `worker_config.json`.
  - First-run interactive setup wizard with LAN auto-discovery.
  - Built-in pre-flight health diagnostics (Metashape executable discovery, TCP port 5840 reachability test, and Shared UNC storage access check).
  - Resilient auto-reconnect loop: automatically reconnects if the coordinator restarts or network drops temporarily.
- **Atomic Monolithic `RunScript` Delegation**:
  - Independent project workflows are bundled into single-node network tasks (`workitem_count = 1`), eliminating worker hopping and LAN latency between stages.
  - Intermediate checkpoints saved to `.files` for instant resumption (`--resume`).
- **Autonomous AI Agent Skill Protocol**:
  - Managed via [`skills/user/image-to-3d-model`](skills/user/image-to-3d-model/SKILL.md) for 1-hour automated progress audits and self-healing dropped network workers.

---

## Pipeline Architecture

```mermaid
flowchart TD
    A["Raw Image Directory<br/>(Scanned Recursively across Subfolders)"] --> B["1. Ingestion & Telemetry Profiling<br/>(EXIF / XMP Optics, GPS Extent, Shutter Analysis)"]
    B --> C["2. Quality Filtering, Markers & GCPs<br/>(Sharpness Filter < 0.50, detectMarkers, importReference)"]
    C --> D["3. GPU Feature Matching & Camera Alignment<br/>(Single-Chunk Global Bundle Adjustment, 40K/60K Keypoints, 25K Tiepoints, Guided Matching)"]
    D --> E["4. Bounding Box Cropping<br/>(gps-square | none | structure | manual)"]
    E --> F["5. Multi-View Stereo (MVS) Depth Maps<br/>(Medium Quality, Moderate / Aggressive Filtering)"]
    F --> G["6. Dense Point Cloud & AI Classification<br/>(buildPointCloud, Confidence Filtering, Ground / Semantic AI Classification)"]
    F --> H["7. Surface Meshing & Topology<br/>(Arbitrary / HeightField, 600K-2M Faces, Smoothing, Decimation)"]
    H --> I["8. Texture Atlas & Delighting<br/>(8K Lossless TIFF, Mosaic Blending, removeLighting Diffuse Albedo)"]
    F --> J["9. Hierarchical 3D Tiled Model<br/>(buildTiledModel: Cesium 3D Tiles, TLS, SLPK, OSGB)"]
    F --> K["10. Digital Elevation Model & Contours<br/>(buildDem + buildContours: GeoTIFF, SHP, DXF, GeoJSON)"]
    H --> L["11. GeoTIFF Orthomosaic<br/>(buildOrthomosaic + BigTIFF LZW Export)"]
    D --> M["12. Executive PDF Calibration Report<br/>(exportReport: camera errors, overlap, GSD)"]
    G --> N["Output Directory: <images_dir>/model/<br/>• 3D Models: model.glb, model.obj, model.fbx, model.ply, model.dxf<br/>• Dense Cloud: point_cloud.laz, tie_points.ply<br/>• 3D Tiles: tiled_model/tileset.json (Cesium), .tls, .slpk<br/>• Elevation & Contours: dem.tif, contours.shp, contours.dxf<br/>• Ortho & Report: orthomosaic.tif, report.pdf<br/>• Project Archive: building_reconstruction.psx"]
    H --> N
    I --> N
    J --> N
    K --> N
    L --> N
    M --> N
```

---

## Production Deliverables

All outputs are automatically organized inside the dedicated `<images_dir>/model/` folder:

| File | Format | Description |
|---|---|---|
| `model.obj` + `model.mtl` | Wavefront OBJ | Metric polygonal mesh with vertex normals and material definitions |
| `model.tif` (pages 1..N) | 8K TIFF | Uncompressed $8192\times 8192$ multi-page photogrammetric texture atlas |
| `model.glb` | Binary glTF | Standalone 3D model with embedded PBR textures for instant web and viewer loading |
| `model.fbx` | Autodesk FBX | Production 3D asset with embedded textures for 3ds Max, Maya, Blender, and Unreal |
| `model_mesh.ply` | Stanford PLY | Metric 3D mesh with vertex normals and vertex color definitions |
| `point_cloud.laz` (or `.las`, `.ply`, `.e57`) | Dense Point Cloud | 3D point cloud with RGB colors, normals, confidence, and ASPRS ground/semantic classifications |
| `tiled_model/tileset.json` | Cesium 3D Tiles | Multiresolution hierarchical LOD 3D tileset for streaming web GIS viewers |
| `dem.tif` + `dem.tfw` | GeoTIFF DEM | Digital Elevation Model (DSM/DTM) raster with BigTIFF LZW compression and world file |
| `contours.shp` (or `.dxf`) | Topographic Contours | Vector elevation contour polylines with metric height attributes |
| `orthomosaic.tif` + `orthomosaic.tfw` | GeoTIFF Ortho | Geo-referenced orthomosaic raster with LZW BigTIFF compression and world file |
| `report.pdf` | PDF Report | Executive processing, camera calibration, reprojection error, and flight overlap report |
| `building_reconstruction.psx` | Agisoft PSX | Native editable Metashape Professional project preserving all calibrations and depth maps |

---

## Agent Skills & Architecture

This repository adheres to the **NVIDIA Isaac Lab** multi-asset skill architecture and governance standards:

- [`skills/user/image-to-3d-model`](skills/user/image-to-3d-model/SKILL.md): Autonomous end-to-end photogrammetry pipeline with survey telemetry profiling, target detection, GCP georeferencing, dense point clouds, 3D tiles, DEM, contours, texture delighting, and PDF reporting.
- [`skills/user/photogrammetry-export`](skills/user/photogrammetry-export/SKILL.md): Production asset export, 3D tiles, contours, point clouds, DEM, FBX/PLY, and texture delighting from existing projects.
- [`skills/developer/coding-style`](skills/developer/coding-style/SKILL.md): Repository coding conventions, SPDX license headers, line limits, and formatting hygiene.
- [`skills/developer/pr-workflow`](skills/developer/pr-workflow/SKILL.md): Conventional commits, DCO sign-offs, and presubmit validation pipelines.
- [`skills/developer/test-audit`](skills/developer/test-audit/SKILL.md): Test suite quality gates and mutation resistance verification.

Validate all repository agent skills:
```powershell
python tools/skills/cli.py check
```

---

## System Requirements

- **Operating System**: Windows 10/11 (64-bit) or Linux (Ubuntu 20.04/22.04 LTS).
- **Engine**: Agisoft Metashape Professional 2.0 or 2.1+ (verified on 2.2.2 Pro, build 20889).
- **Python**: Python 3.10, 3.11, or 3.12 (or Metashape bundled Python environment).
- **GPU**: NVIDIA GPU with CUDA support (RTX 30-series, RTX 40-series, RTX 50-series, or Quadro/Tesla).
- **RAM**: 32 GB RAM recommended for Building preset; 32-64 GB RAM recommended for 2M-face Map presets and dense point clouds.

---

## Contributing

Contributions are welcome! Please read [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`AGENTS.md`](AGENTS.md) before submitting pull requests:
- Sign all commits with Developer Certificate of Origin (`git commit -s`).
- Follow Conventional Commits format (`feat:`, `fix:`, `docs:`, etc.).
- Validate skills (`python tools/skills/cli.py check`) and license compliance (`python tools/check_licenses.py`).
- Run code formatting via `.\format_code.bat` or `./format_code.sh`.

---

## License

This project is licensed under the Apache 2.0 License - see the [LICENSE](LICENSE) file for details.
