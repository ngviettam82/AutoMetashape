# AutoMetashape

**AutoMetashape** is an autonomous, high-precision photogrammetry pipeline that transforms aerial drone surveys and ground image collections into millimeter-accurate 3D models (`.obj`, `.mtl`, `.tif`, `.glb`, `.psx`) using **Agisoft Metashape Professional** and **NVIDIA CUDA** hardware acceleration.

---

## Key Features

- **Recursive Image Discovery**: Scans target folders and all nested subdirectories (flight batches, multi-camera rigs).
- **Default `<input_folder>/model/` Destination**: Automatically encapsulates all reconstructed assets in a dedicated `model/` subfolder.
- **Ultra-Grade Default Precision**:
  - Full-resolution 4K feature extraction (`downscale=1`, 40,000 keypoints).
  - Spatial GPS reference preselection for fast, robust bundle adjustment.
  - Gradual Selection tie-point optimization (pruning error $>0.4\text{ px}$ and uncertainty $>15$).
  - Per-pixel MVS depth map generation on GPU (`MildFiltering`).
  - Watertight Arbitrary Surface meshing with **10,000,000 polygon face limit**.
  - **Lossless uncompressed 8K TIFF (`.tif`)** photographic mosaic texture atlases.

---

## Quickstart

Run the autonomous reconstruction on any image directory:

```powershell
& "C:/Program Files/Agisoft/Metashape Pro/metashape.exe" -r scripts/metashape_reconstruction.py --images "C:/path/to/your/images"
```

All production deliverables are automatically placed in `C:/path/to/your/images/model/`:
- `model.obj` + `model.mtl` + `model.tif` (10M faces, 8192×8192 uncompressed 8K TIFF)
- `model.glb` (self-contained binary glTF model)
- `building_reconstruction.psx` + `building_reconstruction.files/` (native Agisoft Metashape Pro project)

---

## Skill Integration

This repository hosts the **`image-to-3d-model`** skill, allowing AI coding agents to autonomously orchestrate end-to-end 3D reconstructions simply by receiving a folder path.

---

## License

This project is licensed under the Apache 2.0 License - see the [LICENSE](LICENSE) file for details.
