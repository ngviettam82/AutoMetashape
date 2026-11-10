# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""AutoMetashape: Production Asset Export for Solved Metashape Projects.

Loads an existing Agisoft Metashape Professional project (.psx) and exports
production-ready 3D assets without re-running camera matching or depth maps:
- Binary glTF (.glb) with embedded PBR textures (for instant web/3D viewing)
- Wavefront OBJ (.obj + .mtl) with lossless 8K TIFF textures (for CAD/DCC tools)
- Preserves local metric scale and ellipsoidal elevation
"""

from __future__ import annotations
import argparse
import os
import time
try:
    import Metashape
except ImportError:
    Metashape = None


# ---------------------------------------------------------------------------
# Machine-Readable IPC Milestone Protocol Sentinels
# ---------------------------------------------------------------------------
IPC_SENTINEL_PREFIX = "@@AUTOMETASHAPE_STAGE:"
IPC_SENTINEL_SUFFIX = "@@"
IPC_COMPLETE = "@@AUTOMETASHAPE_COMPLETE@@"


def emit_ipc_stage(stage_key: str, description: str) -> None:
    """Emits an explicit machine-readable IPC milestone sentinel to stdout for GUI tracking."""
    print(f"{IPC_SENTINEL_PREFIX}{stage_key}:{description}{IPC_SENTINEL_SUFFIX}", flush=True)


def emit_ipc_event(sentinel: str) -> None:
    """Emits an explicit machine-readable IPC control event to stdout."""
    print(sentinel, flush=True)


if Metashape is not None:
    POINT_CLOUD_FORMAT_MAP = {
        "las": (Metashape.PointCloudFormat.PointCloudFormatLAS, ".las"),
        "laz": (Metashape.PointCloudFormat.PointCloudFormatLAZ, ".laz"),
        "ply": (Metashape.PointCloudFormat.PointCloudFormatPLY, ".ply"),
        "e57": (Metashape.PointCloudFormat.PointCloudFormatE57, ".e57"),
        "xyz": (Metashape.PointCloudFormat.PointCloudFormatXYZ, ".xyz"),
    }

    TILED_MODEL_FORMAT_MAP = {
        "cesium": (Metashape.TiledModelFormat.TiledModelFormatCesium, "tileset.json"),
        "tls": (Metashape.TiledModelFormat.TiledModelFormatTLS, ".tls"),
        "slpk": (Metashape.TiledModelFormat.TiledModelFormatSLPK, ".slpk"),
        "osgb": (Metashape.TiledModelFormat.TiledModelFormatOSGB, ".zip"),
    }

    SHAPES_FORMAT_MAP = {
        "shp": (Metashape.ShapesFormat.ShapesFormatSHP, ".shp"),
        "dxf": (Metashape.ShapesFormat.ShapesFormatDXF, ".dxf"),
        "geojson": (Metashape.ShapesFormat.ShapesFormatGeoJSON, ".geojson"),
        "kml": (Metashape.ShapesFormat.ShapesFormatKML, ".kml"),
    }
else:
    POINT_CLOUD_FORMAT_MAP = {
        "las": (None, ".las"),
        "laz": (None, ".laz"),
        "ply": (None, ".ply"),
        "e57": (None, ".e57"),
        "xyz": (None, ".xyz"),
    }
    TILED_MODEL_FORMAT_MAP = {
        "cesium": (None, "tileset.json"),
        "tls": (None, ".tls"),
        "slpk": (None, ".slpk"),
        "osgb": (None, ".zip"),
    }
    SHAPES_FORMAT_MAP = {
        "shp": (None, ".shp"),
        "dxf": (None, ".dxf"),
        "geojson": (None, ".geojson"),
        "kml": (None, ".kml"),
    }


def infer_project_name(
    images_dir: str | None = None,
    output_dir: str | None = None,
    project_file: str | None = None,
) -> str:
    """Derive clean project base name from survey folder, output folder, or project path."""
    if project_file:
        base = os.path.splitext(os.path.basename(project_file))[0]
        if base and base.lower() != "building_reconstruction":
            return base
        parent = os.path.basename(os.path.dirname(os.path.abspath(project_file)))
        if parent.lower().startswith("model") or parent.lower() in ("export", "exports", "output", "outputs"):
            grandparent = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(project_file))))
            if grandparent:
                return grandparent
        if parent:
            return parent

    if images_dir:
        abs_img = os.path.abspath(images_dir)
        base = os.path.basename(abs_img)
        if base.lower() in ("images", "photos", "raw", "dji", "survey", "data", "input") and os.path.dirname(abs_img):
            base = os.path.basename(os.path.dirname(abs_img))
        if base and base not in (".", "/", "\\"):
            return base

    if output_dir:
        abs_out = os.path.abspath(output_dir)
        base = os.path.basename(abs_out)
        if (base.lower().startswith("model") or base.lower() in ("export", "exports", "output", "outputs")) and os.path.dirname(abs_out):
            base = os.path.basename(os.path.dirname(abs_out))
        if base and base not in (".", "/", "\\"):
            return base

    return "project"


def export_project_assets(
    project_path: str,
    output_dir: str = None,
    texture_format: str = "png",
    rebake_texture: bool = False,
    texture_pages: int = 16,
    fill_holes: bool = True,
    ghosting_filter: bool = True,
    import_model_path: str = None,
    export_point_cloud: bool = False,
    point_cloud_format: str = "laz",
    export_dem: bool = False,
    export_orthomosaic: bool = False,
    export_report: bool = False,
    report_title: str = None,
    export_tiled_model: bool = False,
    tiled_model_format: str = "cesium",
    tile_size: int = 256,
    export_contours: bool = False,
    contour_interval: float = 1.0,
    contours_format: str = "shp",
    remove_lighting: bool = False,
    delighting_color_mode: bool = False,
    calibrate_colors: bool = False,
    smooth_mesh: bool = False,
    smooth_strength: float = 3.0,
    decimate_mesh: int = None,
    export_glb: bool = True,
    export_obj: bool = True,
    export_fbx: bool = False,
    export_ply: bool = False,
    export_dxf: bool = False,
    export_tie_points: bool = False,
    project_name: str | None = None,
):
    """Exports production assets from a solved Metashape project."""
    if not os.path.exists(project_path):
        raise FileNotFoundError(f"Project file not found: {project_path}")

    project_dir = os.path.dirname(os.path.abspath(project_path))
    if not output_dir:
        output_dir = project_dir
    os.makedirs(output_dir, exist_ok=True)
    if not project_name:
        project_name = infer_project_name(output_dir=output_dir, project_file=project_path)

    print("=========================================================================")
    print("  AUTOMETASHAPE: PRODUCTION ASSET EXPORT")
    print(f"  Project: {project_path}")
    print(f"  Destination: {output_dir}")
    print("=========================================================================", flush=True)

    emit_ipc_stage("project", f"Loading Metashape Project: {os.path.basename(project_path)}")
    doc = Metashape.Document()
    files_dir = project_path.replace(".psx", ".files")
    lock_file = os.path.join(files_dir, "lock")
    if os.path.isfile(lock_file):
        try:
            os.remove(lock_file)
        except Exception:
            pass
    try:
        doc.open(project_path, read_only=False, ignore_lock=True)
    except Exception:
        try:
            doc.open(project_path, ignore_lock=True)
        except Exception:
            doc.open(project_path)
    try:
        doc.read_only = False
    except Exception:
        pass

    # Enable all available NVIDIA / high-performance GPUs
    gpus = Metashape.app.enumGPUDevices()
    nvidia_mask = 0
    active_gpu_names = []
    for i, g in enumerate(gpus):
        vendor = g.get("vendor", "").lower()
        name = g.get("name", "").lower()
        if "nvidia" in vendor or "geforce" in name or "rtx" in name:
            nvidia_mask |= (1 << i)
            active_gpu_names.append(f"{g['name']} ({g['mem_size'] / (1024**3):.1f} GB VRAM)")

    if nvidia_mask:
        Metashape.app.gpu_mask = nvidia_mask
        print(f"[Hardware] GPU: {', '.join(active_gpu_names)} (Mask: {nvidia_mask})", flush=True)
    elif gpus:
        Metashape.app.gpu_mask = (1 << len(gpus)) - 1
        print(f"[Hardware] GPU: {gpus[0]['name']} ({gpus[0]['mem_size'] / (1024**3):.1f} GB VRAM)", flush=True)

    chunk = doc.chunk
    if not chunk:
        raise ValueError(f"Project '{project_path}' contains no active chunk!")

    # Explicit or automatic model re-linking/import
    if import_model_path and os.path.isfile(import_model_path):
        print(f"[Import] Importing 3D mesh model from: {import_model_path}", flush=True)
        chunk.importModel(path=import_model_path, format=Metashape.ModelFormat.ModelFormatOBJ)
        doc.save()
        chunk = doc.chunk
        print(f"[Import] Re-linked model saved to project: {project_path}", flush=True)
    elif not chunk.model:
        # Auto-recover if model.obj exists in output or project directory
        candidates = [
            os.path.join(output_dir, "model.obj"),
            os.path.join(project_dir, "model.obj"),
        ]
        recovered = False
        for cand in candidates:
            if os.path.isfile(cand):
                print(f"[Auto-Recover] Project chunk has no model, but found existing: {cand}", flush=True)
                chunk.importModel(path=cand, format=Metashape.ModelFormat.ModelFormatOBJ)
                doc.save()
                chunk = doc.chunk
                print(f"[Auto-Recover] Re-linked '{cand}' into project successfully!", flush=True)
                recovered = True
                break
        if not recovered:
            raise ValueError(f"Project '{project_path}' contains no reconstructed 3D mesh model!")

    mesh_verts = len(chunk.model.vertices)
    mesh_faces = len(chunk.model.faces)
    emit_ipc_stage("mesh", f"Validating 3D Mesh Geometry ({mesh_verts:,} vertices, {mesh_faces:,} faces)")
    print(f"[Model Info] Geometry: {mesh_verts:,} vertices, {mesh_faces:,} polygonal faces.", flush=True)

    if smooth_mesh and chunk.model:
        print(f"[Mesh] Applying Laplacian smoothing (strength: {smooth_strength})...", flush=True)
        chunk.smoothModel(strength=smooth_strength, fix_borders=True, preserve_edges=True)
        doc.save()

    if decimate_mesh and chunk.model and decimate_mesh > 0:
        print(f"[Mesh] Decimating surface mesh to {decimate_mesh:,} faces...", flush=True)
        chunk.decimateModel(face_count=decimate_mesh)
        doc.save()

    # Optional / Conditional texture re-baking
    current_pages = 0
    if hasattr(chunk.model, "textures") and chunk.model.textures:
        current_pages = len(chunk.model.textures)
    elif hasattr(chunk.model, "texture") and chunk.model.texture():
        current_pages = 1

    if calibrate_colors or rebake_texture or current_pages == 0 or (remove_lighting and chunk.model):
        emit_ipc_stage("texture", "Processing Texture Atlas & Delighting")

    if calibrate_colors:
        print("\n[Color Calibration] Executing radiometric calibration & white balance...", flush=True)
        try:
            chunk.calibrateColors(source_data=Metashape.DataSource.ModelData, white_balance=True)
            doc.save()
        except Exception as e_col:
            print(f"  [WARNING] Color calibration issue: {e_col}. Continuing...", flush=True)

    if rebake_texture or current_pages == 0:
        print(f"\n[Texture] Baking {texture_pages}x 8192x8192 (8K) Texture Atlas (Mosaic Blending, Fill Holes: {fill_holes}, Ghosting Filter: {ghosting_filter})...", flush=True)
        t_tex = time.time()
        chunk.buildUV(
            mapping_mode=Metashape.MappingMode.GenericMapping,
            page_count=texture_pages,
            texture_size=8192,
        )
        chunk.buildTexture(
            blending_mode=Metashape.BlendingMode.MosaicBlending,
            texture_size=8192,
            fill_holes=fill_holes,
            ghosting_filter=ghosting_filter,
        )
        print(f"  Texture baking completed in {(time.time() - t_tex)/60:.2f} min.", flush=True)
        doc.save()
        chunk = doc.chunk
        print(f"  Updated project saved: {project_path}", flush=True)

    if remove_lighting and chunk.model:
        print("\n[Delighting] Removing harsh ambient shadows and illumination...", flush=True)
        try:
            chunk.removeLighting(color_mode=delighting_color_mode)
            doc.save()
            print("  Texture delighting completed.", flush=True)
        except Exception as e_lt:
            print(f"  [WARNING] Texture delighting issue: {e_lt}. Continuing...", flush=True)

    glb_out = os.path.join(output_dir, f"{project_name}.glb")
    obj_out = os.path.join(output_dir, f"{project_name}.obj")

    # 1. Export Binary GLTF/GLB
    if export_glb and chunk.model:
        emit_ipc_stage("models", "Exporting Binary glTF (.glb) Model")
        print("\n[1/2] Exporting Binary glTF (.glb) with embedded PBR textures...", flush=True)
        t_glb = time.time()
        chunk.exportModel(
            glb_out,
            format=Metashape.ModelFormat.ModelFormatGLTF,
            embed_texture=True,
            save_normals=True,
            save_colors=True,
        )
        glb_size = os.path.getsize(glb_out) / (1024 * 1024)
        print(f"  Saved: {glb_out} ({glb_size:.2f} MB) in {(time.time() - t_glb):.1f}s.", flush=True)

    # 2. Export Wavefront OBJ + textures
    if export_obj and chunk.model:
        fmt_lower = (texture_format or "png").lower()
        if fmt_lower in ("tiff", "tif"):
            tex_fmt = Metashape.ImageFormat.ImageFormatTIFF
        elif fmt_lower in ("jpg", "jpeg"):
            tex_fmt = Metashape.ImageFormat.ImageFormatJPEG
        else:
            tex_fmt = Metashape.ImageFormat.ImageFormatPNG
        emit_ipc_stage("models", f"Exporting Wavefront OBJ (.obj) with {fmt_lower.upper()} texture atlas")
        print(f"\n[2/2] Exporting Wavefront OBJ (.obj + .mtl) with {fmt_lower.upper()} texture atlas...", flush=True)
        t_obj = time.time()
        chunk.exportModel(
            obj_out,
            format=Metashape.ModelFormat.ModelFormatOBJ,
            texture_format=tex_fmt,
            save_texture=True,
            save_normals=True,
            save_colors=True,
        )
        obj_size = os.path.getsize(obj_out) / (1024 * 1024)
        print(f"  Saved: {obj_out} ({obj_size:.2f} MB) in {(time.time() - t_obj):.1f}s.", flush=True)

    # 3. Optional Dense Point Cloud Export
    emit_ipc_stage("deliverables", "Exporting Production Deliverables (Cloud, DEM, Ortho, Report)")
    if export_point_cloud and chunk.point_cloud and chunk.point_cloud.point_count > 0:
        pc_format_ext = point_cloud_format.lower() if point_cloud_format else "laz"
        pc_fmt, pc_ext = POINT_CLOUD_FORMAT_MAP.get(pc_format_ext, (Metashape.PointCloudFormat.PointCloudFormatLAZ, ".laz"))
        pc_out = os.path.join(output_dir, f"{project_name}_point_cloud{pc_ext}")
        print(f"\n[Point Cloud] Exporting Dense Point Cloud to: {pc_out}...", flush=True)
        chunk.exportPointCloud(
            path=pc_out,
            format=pc_fmt,
            source_data=Metashape.DataSource.PointCloudData,
            save_point_color=True,
            save_point_normal=True,
            save_point_confidence=True,
            save_point_classification=True,
        )
        print(f"  Saved Point Cloud: {pc_out} ({os.path.getsize(pc_out)/(1024*1024):.2f} MB)", flush=True)

    # 4. Optional DEM Export
    if export_dem:
        has_dem = getattr(chunk, "elevation", None) is not None
        if not has_dem:
            print("\n[DEM] Project contains no pre-built elevation model. Generating on-demand before export...", flush=True)
            try:
                src_data = Metashape.DataSource.DepthMapsData
                if getattr(chunk, "point_cloud", None) is not None:
                    src_data = Metashape.DataSource.PointCloudData
                elif getattr(chunk, "depth_maps", None) is not None:
                    src_data = Metashape.DataSource.DepthMapsData
                elif getattr(chunk, "model", None) is not None:
                    src_data = Metashape.DataSource.ModelData
                chunk.buildDem(source_data=src_data)
                doc.save()
                has_dem = getattr(chunk, "elevation", None) is not None
                print("  [DEM] Elevation model built and saved successfully.", flush=True)
            except Exception as e_dem_build:
                print(f"  [WARNING] Could not build DEM on-demand: {e_dem_build}.", flush=True)

        if has_dem:
            dem_out = os.path.join(output_dir, f"{project_name}_dem.tif")
            print(f"\n[DEM] Exporting GeoTIFF Elevation Model to: {dem_out}...", flush=True)
            comp = Metashape.ImageCompression()
            comp.tiff_compression = Metashape.ImageCompression.TiffCompression.TiffCompressionLZW
            comp.tiff_big = True
            chunk.exportRaster(
                path=dem_out,
                source_data=Metashape.DataSource.ElevationData,
                image_format=Metashape.ImageFormat.ImageFormatTIFF,
                image_compression=comp,
                save_world=True,
            )
            print(f"  Saved DEM: {dem_out} ({os.path.getsize(dem_out)/(1024*1024):.2f} MB)", flush=True)
        else:
            print("\n[DEM] Note: Project contains no elevation model; skipping DEM export.", flush=True)

    # 5. Optional Orthomosaic Export
    if export_orthomosaic:
        has_ortho = getattr(chunk, "orthomosaic", None) is not None
        if not has_ortho:
            print("\n[Orthomosaic] Project contains no pre-built orthomosaic. Generating on-demand before export...", flush=True)
            try:
                if getattr(chunk, "elevation", None) is not None:
                    surf_data = Metashape.DataSource.ElevationData
                elif getattr(chunk, "model", None) is not None:
                    surf_data = Metashape.DataSource.ModelData
                else:
                    surf_data = Metashape.DataSource.ElevationData
                chunk.buildOrthomosaic(surface_data=surf_data)
                doc.save()
                has_ortho = getattr(chunk, "orthomosaic", None) is not None
                print("  [Orthomosaic] Orthomosaic built and saved successfully.", flush=True)
            except Exception as e_ortho_build:
                print(f"  [WARNING] Could not build Orthomosaic on-demand: {e_ortho_build}.", flush=True)

        if has_ortho:
            ortho_out = os.path.join(output_dir, f"{project_name}_orthomosaic.tif")
            print(f"\n[Orthomosaic] Exporting GeoTIFF Orthomosaic to: {ortho_out}...", flush=True)
            comp = Metashape.ImageCompression()
            comp.tiff_compression = Metashape.ImageCompression.TiffCompression.TiffCompressionLZW
            comp.tiff_big = True
            chunk.exportRaster(
                path=ortho_out,
                source_data=Metashape.DataSource.OrthomosaicData,
                image_format=Metashape.ImageFormat.ImageFormatTIFF,
                image_compression=comp,
                save_world=True,
                save_alpha=True,
            )
            print(f"  Saved Orthomosaic: {ortho_out} ({os.path.getsize(ortho_out)/(1024*1024):.2f} MB)", flush=True)
        else:
            print("\n[Orthomosaic] Note: Project contains no orthomosaic; skipping export.", flush=True)

    # Export FBX
    if export_fbx and chunk.model:
        fbx_out = os.path.join(output_dir, f"{project_name}.fbx")
        print(f"\n[FBX] Exporting Autodesk FBX to: {fbx_out}...", flush=True)
        try:
            chunk.exportModel(fbx_out, format=Metashape.ModelFormat.ModelFormatFBX, save_texture=True, save_normals=True, save_colors=True)
            print(f"  Saved FBX: {fbx_out} ({os.path.getsize(fbx_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_fbx:
            print(f"  [WARNING] FBX export issue: {e_fbx}", flush=True)

    # Export PLY
    if export_ply and chunk.model:
        ply_out = os.path.join(output_dir, f"{project_name}_mesh.ply")
        print(f"\n[PLY] Exporting Stanford PLY to: {ply_out}...", flush=True)
        try:
            chunk.exportModel(ply_out, format=Metashape.ModelFormat.ModelFormatPLY, save_normals=True, save_colors=True)
            print(f"  Saved PLY: {ply_out} ({os.path.getsize(ply_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_ply:
            print(f"  [WARNING] PLY export issue: {e_ply}", flush=True)

    # Export DXF
    if export_dxf and chunk.model:
        dxf_out = os.path.join(output_dir, f"{project_name}.dxf")
        print(f"\n[DXF] Exporting AutoCAD DXF to: {dxf_out}...", flush=True)
        try:
            chunk.exportModel(dxf_out, format=Metashape.ModelFormat.ModelFormatDXF, save_normals=True)
            print(f"  Saved DXF: {dxf_out} ({os.path.getsize(dxf_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_dxf:
            print(f"  [WARNING] DXF export issue: {e_dxf}", flush=True)

    # Export Sparse Tie Points
    if export_tie_points and chunk.tie_points:
        tp_out = os.path.join(output_dir, f"{project_name}_tie_points.ply")
        print(f"\n[Tie Points] Exporting Sparse Tie Points to: {tp_out}...", flush=True)
        try:
            chunk.exportPointCloud(path=tp_out, format=Metashape.PointCloudFormat.PointCloudFormatPLY, source_data=Metashape.DataSource.TiePointsData)
            print(f"  Saved Tie Points: {tp_out} ({os.path.getsize(tp_out)/(1024*1024):.2f} MB)", flush=True)
        except Exception as e_tp:
            print(f"  [WARNING] Tie points export issue: {e_tp}", flush=True)

    # Export Hierarchical 3D Tiled Model
    if export_tiled_model:
        try:
            has_tiled = getattr(chunk, "tiled_model", None) is not None
            if not has_tiled:
                print(f"\n[Tiled Model] Building Hierarchical 3D Tiled Model (Tile Size: {tile_size})...", flush=True)
                chunk.buildTiledModel(tile_size=tile_size, source_data=Metashape.DataSource.DepthMapsData)
                doc.save()

            tiled_fmt_tuple = TILED_MODEL_FORMAT_MAP.get(tiled_model_format.lower(), (Metashape.TiledModelFormat.TiledModelFormatCesium, "tileset.json"))
            tiled_fmt, tiled_ext = tiled_fmt_tuple
            if tiled_model_format.lower() == "cesium":
                tiled_dir = os.path.join(output_dir, f"{project_name}_tiled_model")
                os.makedirs(tiled_dir, exist_ok=True)
                tiled_out = os.path.join(tiled_dir, "tileset.json")
            else:
                tiled_out = os.path.join(output_dir, f"{project_name}_tiled_model{tiled_ext}")
            print(f"\n[Tiled Model] Exporting 3D Tiled Model to: {tiled_out}...", flush=True)
            chunk.exportTiledModel(path=tiled_out, format=tiled_fmt)
            print(f"  Saved 3D Tiled Model: {tiled_out}", flush=True)
        except Exception as e_tile:
            print(f"  [WARNING] 3D Tiled Model export issue: {e_tile}. Continuing...", flush=True)

    # Export Topographic Contours
    if export_contours:
        has_dem = getattr(chunk, "elevation", None) is not None
        if not has_dem:
            print("\n[Contours] Topographic contours require an elevation model. Generating DEM on-demand...", flush=True)
            try:
                src_data = Metashape.DataSource.DepthMapsData
                if getattr(chunk, "point_cloud", None) is not None:
                    src_data = Metashape.DataSource.PointCloudData
                elif getattr(chunk, "depth_maps", None) is not None:
                    src_data = Metashape.DataSource.DepthMapsData
                elif getattr(chunk, "model", None) is not None:
                    src_data = Metashape.DataSource.ModelData
                chunk.buildDem(source_data=src_data)
                doc.save()
                has_dem = getattr(chunk, "elevation", None) is not None
            except Exception as e_dem_cnt:
                print(f"  [WARNING] Could not build DEM for contours: {e_dem_cnt}", flush=True)

        if has_dem:
            try:
                print(f"\n[Contours] Generating Topographic Contours (Interval: {contour_interval}m)...", flush=True)
                chunk.buildContours(source_data=Metashape.DataSource.ElevationData, interval=contour_interval, prevent_intersections=True)
                sh_fmt, sh_ext = SHAPES_FORMAT_MAP.get(contours_format.lower(), (Metashape.ShapesFormat.ShapesFormatSHP, ".shp"))
                cnt_out = os.path.join(output_dir, f"{project_name}_contours{sh_ext}")
                print(f"  Exporting Contour Shapes to: {cnt_out}...", flush=True)
                chunk.exportShapes(path=cnt_out, format=sh_fmt, save_polylines=True, save_elevation=True, save_attributes=True)
                print(f"  Saved Topographic Contours: {cnt_out}", flush=True)
            except Exception as e_cnt:
                print(f"  [WARNING] Contour export issue: {e_cnt}. Continuing...", flush=True)
        else:
            print("\n[Contours] Project contains no built elevation model; skipping contour export.", flush=True)

    # 6. Optional Executive PDF Report Export
    if export_report:
        report_out = os.path.join(output_dir, f"{project_name}_report.pdf")
        print(f"\n[Report] Exporting Calibration & Quality PDF Report to: {report_out}...", flush=True)
        rep_title = report_title or "AutoMetashape Quality & Calibration Report"
        chunk.exportReport(
            path=report_out,
            title=rep_title,
            description="AutoMetashape Production Export",
        )
        print(f"  Saved Report: {report_out} ({os.path.getsize(report_out)/(1024*1024):.2f} MB)", flush=True)

    print("\n[SUCCESS] Production assets exported successfully!", flush=True)
    emit_ipc_event(IPC_COMPLETE)


def build_arg_parser() -> argparse.ArgumentParser:
    """Constructs command-line argument parser for standalone production asset export."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape Export CLI: Export production 3D assets from existing Metashape project (.psx)"
    )
    parser.add_argument("--project", required=True, help="Path to existing Metashape project file (.psx)")
    parser.add_argument("--output", default=None, help="Output directory for exported 3D assets (default: same folder as .psx)")
    parser.add_argument("--project-name", default=None, help="Base name for exported 3D model, textures, point cloud, and reports (default: folder project name)")
    parser.add_argument("--texture-format", choices=["png", "tiff", "tif", "jpg", "jpeg"], default="png", help="Texture atlas format (default: png)")
    parser.add_argument("--export-glb", dest="export_glb", action="store_true", default=True, help="Export binary glTF (.glb) model (default: True)")
    parser.add_argument("--no-export-glb", dest="export_glb", action="store_false", help="Disable binary glTF (.glb) export")
    parser.add_argument("--export-obj", dest="export_obj", action="store_true", default=True, help="Export Wavefront OBJ (.obj) model (default: True)")
    parser.add_argument("--no-export-obj", dest="export_obj", action="store_false", help="Disable Wavefront OBJ (.obj) export")
    parser.add_argument("--rebake-texture", action="store_true", default=False, help="Re-bake UV parameterization and 8K texture atlas before export")
    parser.add_argument("--texture-pages", type=int, default=16, help="Number of 8K texture atlas pages if re-baking (default: 16)")
    parser.add_argument("--no-fill-holes", dest="fill_holes", action="store_false", default=True, help="Disable texture hole filling if re-baking (default: enabled)")
    parser.add_argument("--no-ghosting-filter", dest="ghosting_filter", action="store_false", default=True, help="Disable ghosting filter if re-baking (default: enabled)")
    parser.add_argument("--import-model", default=None, help="Optional path to external 3D model (.obj) to import/re-link into the project")
    parser.add_argument("--export-point-cloud", dest="export_point_cloud", action="store_true", default=False, help="Export dense point cloud file")
    parser.add_argument("--no-export-point-cloud", dest="export_point_cloud", action="store_false", help="Disable dense point cloud file export")
    parser.add_argument("--point-cloud-format", choices=["laz", "las", "ply", "e57", "xyz"], default="laz", help="Format for exported dense point cloud (default: laz)")
    parser.add_argument("--export-dem", dest="export_dem", action="store_true", default=False, help="Export GeoTIFF Digital Elevation Model (dem.tif)")
    parser.add_argument("--no-export-dem", dest="export_dem", action="store_false", help="Disable GeoTIFF Digital Elevation Model export")
    parser.add_argument("--export-orthomosaic", dest="export_orthomosaic", action="store_true", default=False, help="Export GeoTIFF Orthomosaic (orthomosaic.tif)")
    parser.add_argument("--no-export-orthomosaic", dest="export_orthomosaic", action="store_false", help="Disable GeoTIFF Orthomosaic export")
    parser.add_argument("--export-report", dest="export_report", action="store_true", default=False, help="Export official Metashape PDF calibration & quality report")
    parser.add_argument("--no-report", "--no-export-report", dest="export_report", action="store_false", help="Disable PDF quality report export")
    parser.add_argument("--report-title", default=None, help="Custom title for PDF quality report")
    parser.add_argument("--export-tiled-model", action="store_true", default=False, help="Export hierarchical 3D tiled model (LOD)")
    parser.add_argument("--tiled-model-format", choices=["cesium", "tls", "slpk", "osgb"], default="cesium", help="Format for 3D tiled model (default: cesium)")
    parser.add_argument("--tile-size", type=int, default=256, help="Tile size in pixels (default: 256)")
    parser.add_argument("--export-contours", action="store_true", default=False, help="Generate and export topographic contour lines from DEM")
    parser.add_argument("--contour-interval", type=float, default=1.0, help="Contour elevation interval in meters (default: 1.0)")
    parser.add_argument("--contours-format", choices=["shp", "dxf", "geojson", "kml"], default="shp", help="Format for topographic contours (default: shp)")
    parser.add_argument("--remove-lighting", action="store_true", default=False, help="Remove shadows and ambient lighting from texture (delighting)")
    parser.add_argument("--delighting-color-mode", action="store_true", default=False, help="Enable multi-color delighting mode")
    parser.add_argument("--calibrate-colors", action="store_true", default=False, help="Perform radiometric color & white balance calibration")
    parser.add_argument("--smooth-mesh", action="store_true", default=False, help="Apply Laplacian smoothing to surface mesh")
    parser.add_argument("--smooth-strength", type=float, default=3.0, help="Laplacian smoothing strength (default: 3.0)")
    parser.add_argument("--decimate-mesh", type=int, default=None, help="Decimate mesh to target face count")
    parser.add_argument("--export-fbx", action="store_true", default=False, help="Export 3D model in Autodesk FBX format")
    parser.add_argument("--export-ply", action="store_true", default=False, help="Export 3D model in Stanford PLY format")
    parser.add_argument("--export-dxf", action="store_true", default=False, help="Export 3D model in AutoCAD DXF format")
    parser.add_argument("--export-tie-points", action="store_true", default=False, help="Export sparse tie points in PLY format")
    return parser


if __name__ == "__main__":
    parser = build_arg_parser()
    args = parser.parse_args()

    export_project_assets(
        project_path=args.project,
        output_dir=args.output,
        texture_format=args.texture_format,
        rebake_texture=args.rebake_texture,
        texture_pages=args.texture_pages,
        fill_holes=args.fill_holes,
        ghosting_filter=args.ghosting_filter,
        import_model_path=args.import_model,
        export_point_cloud=args.export_point_cloud,
        point_cloud_format=args.point_cloud_format,
        export_dem=args.export_dem,
        export_orthomosaic=args.export_orthomosaic,
        export_report=args.export_report,
        report_title=args.report_title,
        export_tiled_model=args.export_tiled_model,
        tiled_model_format=args.tiled_model_format,
        tile_size=args.tile_size,
        export_contours=args.export_contours,
        contour_interval=args.contour_interval,
        contours_format=args.contours_format,
        remove_lighting=args.remove_lighting,
        delighting_color_mode=args.delighting_color_mode,
        calibrate_colors=args.calibrate_colors,
        smooth_mesh=args.smooth_mesh,
        smooth_strength=args.smooth_strength,
        decimate_mesh=args.decimate_mesh,
        export_glb=args.export_glb,
        export_obj=args.export_obj,
        export_fbx=args.export_fbx,
        export_ply=args.export_ply,
        export_dxf=args.export_dxf,
        export_tie_points=args.export_tie_points,
        project_name=args.project_name,
    )
