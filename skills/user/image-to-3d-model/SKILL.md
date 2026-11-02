---
name: image-to-3d-model
description: "Reconstructs a high-precision, production-grade 3D textured mesh (.obj, .mtl, .glb) and native Agisoft Metashape project (.psx) from an image folder or aerial drone survey, automating GPU feature matching, camera alignment, MVS depth maps, surface meshing, and 8K mosaic texture baking. Use when provided an image folder or drone survey to generate a 3D model."
audience: user
status: stable
owners:
  - core-engineering
---

# High-Precision 3D Photogrammetry & Architectural Reconstruction

Autonomous end-to-end photogrammetry pipeline transforming raw image folders (drone surveys, architectural captures, ground inspections) into high-density textured 3D models (`.obj`, `.mtl`, `.glb`) and native Agisoft Metashape Professional projects (`.psx`).

---

## When To Use

Use this skill when:
- The user provides an image folder (drone survey, building inspection, or ground photogrammetry) and requests a 3D model with realistic photographic texture.
- Reconstructing architectural structures, buildings, urban environments, or complex terrain requiring metric accuracy, flat walls, and crisp 90-degree corners.
- Inspecting or editing 3D models inside **Agisoft Metashape Professional**, Blender, Unreal Engine, 3ds Max, CAD, or GIS software.
- Requiring high polygon counts (1.5M to 5M+ faces) and high-resolution texture atlases (8K 8192×8192 mosaic photographic blending).

Do not use this skill when:
- Synthesizing 3D models from single images, sketches, or text prompts without multi-view photographic overlap.
- Running pure 2D image enhancement without 3D geometric triangulation.

---

## Workflow

```mermaid
flowchart TD
    A["Raw Image Directory<br/>(Provided by User)"] --> B["1. Ingestion & EXIF Metadata<br/>(Focal length, GPS, Altitude)"]
    B --> C["2. GPU CUDA Feature Matching<br/>(Full 4K, GPS Preselection)"]
    C --> D["3. Camera Alignment & Optimization<br/>(Adaptive Fitting & Gradual Selection)"]
    D --> E["4. Multi-View Stereo Depth Maps<br/>(Per-pixel GPU Raycasting, Mild Filter)"]
    E --> F["5. Arbitrary Surface Meshing<br/>(Direct DepthMapsData, 1.5M - 5M+ faces)"]
    F --> G["6. 8K Photogrammetric Texture Atlas<br/>(Mosaic Blending, Ghosting Filter)"]
    G --> H["Deliverables Export<br/>• model.obj + model.mtl<br/>• model.glb<br/>• project.psx"]
```

### 1. Ingestion & Telemetry Inspection
When given an image directory, verify image formats (`.jpg`, `.jpeg`, `.png`, `.tif`) and ensure NVIDIA GPU acceleration is available. Run the autonomous pipeline (Ultra Grade is default):
```powershell
& "C:/Program Files/Agisoft/Metashape Pro/metashape.exe" -r scripts/metashape_reconstruction.py --images "<path_to_images>" --output "<path_to_output>"
```

> [!NOTE]
> The pipeline strictly defaults to **Ultra Grade** (`--grade ultra` with 10,000,000 faces limit, full 4K MVS depth maps, tie-point Gradual Selection optimization, and uncompressed 8K TIFF textures). No manual flags are needed.

### 2. High-Accuracy GPU Matching & Alignment
- **Matching**: `downscale=1` (1:1 full resolution), `keypoint_limit=40000`, `tiepoint_limit=4000`. Uses GPS Reference Preselection so images match against spatial neighbors.
- **Alignment**: Solves camera positions $(X, Y, Z)$, orientations, and non-linear lens distortion ($f, c_x, c_y, k_1, k_2, k_3, k_4, p_1, p_2$).
- **Camera Optimization (Gradual Selection)**: Enabled by default. Prunes high reprojection error ($>0.4\text{ px}$) and high reconstruction uncertainty ($>15$) tie points before re-optimizing lens parameters.

### 3. GPU Multi-View Stereo (MVS) Depth Maps
- Computes dense per-pixel depth estimation using CUDA on NVIDIA GPUs.
- **Default (Ultra)**: `downscale=1` (full 4K depth maps per camera).
- Uses `MildFiltering` to retain fine architectural structures (antennas, window mullions, roof trim, columns).

### 4. Arbitrary Surface Polygonal Meshing
- Fuses visibility-consistent depth maps into a continuous polygonal mesh using `DataSource.DepthMapsData` (avoiding the intermediate dense point cloud and melted wax Poisson artifacts).
- Targets **10,000,000 faces limit** by default (Ultra Grade) to prevent unwieldy file bloat while maintaining extreme architectural precision.

### 5. Multi-Page 8K Photographic Texture Baking & Lossless TIFF Export
- Computes optimized UV parameterization (`GenericMapping`).
- Projects photographic pixels from all original 4K frames using `MosaicBlending` with `ghosting_filter=True` and `fill_holes=True`.
- Exports texture maps in **lossless uncompressed TIFF (`.tif`) format** (no JPEG compression artifacts) at **8192×8192 (8K)** resolution.

### 6. Production Deliverables
- `model.obj` + `model.mtl` + `model.tif`: Industry-standard mesh format with uncompressed 8K TIFF texture atlas compatible with all DCC/CAD tools.
- `model.glb`: Binary glTF format with embedded PBR textures for instant web and 3D viewer rendering.
- `building_reconstruction.psx`: Native Metashape Professional project retaining all camera alignments, depth maps, and UV textures for direct inspection and further editing.

---

## Validation

Reconstruction must satisfy the following quality criteria:
1. **Camera Registration**: At least $90\%$ of input images must be successfully registered in space.
2. **Surface Topology**: Arbitrary surface mesh with $\ge 1,500,000$ polygon faces, free from degenerate zero-area faces.
3. **Texture Atlas**: Resolution $\ge 8192\times 8192$ with valid UV mapping and zero unmapped black voids.
4. **Project File**: Native `.psx` file must be successfully saved and loadable in Agisoft Metashape Professional GUI.

---

## Maintenance

- Ensure `metashape.exe` is installed at `C:/Program Files/Agisoft/Metashape Pro/metashape.exe` or configured on the system PATH.
- Verify NVIDIA GPU CUDA runtime with `nvidia-smi` to ensure Metashape GPU acceleration is active.
- Keep `scripts/metashape_reconstruction.py` updated to match Agisoft Metashape Python API versions.

---

## References

- [Agisoft Metashape Professional User Manual](https://www.agisoft.com/downloads/user-manuals/)
- [USGS National Geospatial Program Photogrammetry Standards](https://www.usgs.gov/core-science-systems/ngp)
