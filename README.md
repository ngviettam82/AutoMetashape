# AutoMetashape

**AutoMetashape** is an autonomous, high-precision photogrammetry pipeline that transforms aerial drone surveys and ground image collections into millimeter-accurate 3D models (`.obj`, `.mtl`, `.tif`, `.glb`, `.psx`) using **Agisoft Metashape Professional** and **NVIDIA CUDA** hardware acceleration.

---

## Key Features

- **Desktop GUI Studio**: Interactive graphical interface (`gui.bat`) to select image folders, destination paths, and monitor live reconstruction logs.
- **Recursive Image Discovery**: Scans target folders and all nested subdirectories (supports all raster, drone RAW, camera RAW, and thermal imagery).
- **Automated Sharpness Filtering**: Disables out-of-focus or motion-blurred photos ($<0.5$ quality index) before feature matching.
- **Rolling Shutter Compensation**: Models drone CMOS rolling shutter motion, reducing reprojection errors by up to 30%.
- **Intelligent Bounding Box**: Auto-crops scene to camera GPS footprint or optical ray convergence to eliminate distant horizon noise.
- **Architectural Surface Meshing**: 1,000,000 polygon faces with planar hole closing across window openings.
- **16-Page 8K Texture Atlas**: Multi-page ($16 \times 8192 \times 8192$) uncompressed TIFF mosaic blending with hole filling and ghosting filter.
- **Dedicated `<input_folder>/model/` Destination**: Encapsulates deliverables and native editable `.psx` project co-located.

---

## Graphical User Interface (GUI)

Launch the interactive 3D Photogrammetry Studio:

```powershell
# Double-click or run:
.\gui.bat
```

Or via Python:
```powershell
python scripts/autometashape_gui.py
```

The GUI provides:
- One-click folder pickers for Input Image Folder and Output Directory.
- Visual toggle for Alignment Quality, Face Count, 16-page 8K Textures, and Crop Mode.
- Tab for fast texture re-baking on existing `.psx` projects without re-running camera alignment.
- Live streaming execution console, progress bar, and instant "Open Output Folder" / "Open in Metashape GUI" buttons.

---

## Command Line Quickstart

Run autonomous reconstruction directly from PowerShell:

```powershell
& "C:/Program Files/Agisoft/Metashape Pro/metashape.exe" -r scripts/metashape_reconstruction.py --images "C:/path/to/your/images"
```

All production deliverables are automatically placed in `C:/path/to/your/images/model/`:
- `model.obj` + `model.mtl` + `model.tif` (1M faces, 16-page 8192×8192 uncompressed 8K TIFF)
- `model.glb` (self-contained binary glTF model with embedded PBR textures)
- `building_reconstruction.psx` + `building_reconstruction.files/` (native Agisoft Metashape Pro project)

To re-bake the 16-page 8K texture atlas on an existing solved project:
```powershell
& "C:/Program Files/Agisoft/Metashape Pro/metashape.exe" -r scripts/metashape_export.py --project "C:/path/to/your/images/model/building_reconstruction.psx" --rebake-texture --texture-pages 16
```

---

## Skill Integration

This repository hosts repository skills under `skills/`:
- **`image-to-3d-model`**: Autonomous end-to-end photogrammetry pipeline.
- **`photogrammetry-export`**: Fast export and texture re-baking from existing projects.
- **`coding-style`**, **`pr-workflow`**, **`test-audit`**: Engineering standard enforcement.

---

## License

This project is licensed under the Apache 2.0 License - see the [LICENSE](LICENSE) file for details.
