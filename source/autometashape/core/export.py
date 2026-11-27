# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Decoupled production export engine for photogrammetric models, textures, and GIS deliverables."""

from __future__ import annotations

import os
import sys
import time

try:
    import Metashape
except ImportError:
    Metashape = None

from autometashape.core.telemetry import compute_utm_epsg, is_projected_crs
from autometashape.ipc.protocol import (
    IPC_COMPLETE,
    emit_ipc_event,
    emit_ipc_stage,
)

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


_EXPORT_SUBDIR_NAMES = frozenset(
    {"model", "models", "model_export", "model_output", "export", "exports", "output", "outputs", "result", "results"}
)


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
        if parent.lower() in _EXPORT_SUBDIR_NAMES:
            grandparent = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(project_file))))
            if grandparent:
                return grandparent
        if parent:
            return parent

    if images_dir:
        abs_img = os.path.abspath(images_dir)
        base = os.path.basename(abs_img)
        if base.lower() in (
            "images",
            "photos",
            "raw",
            "dji",
            "survey",
            "data",
            "input",
            "ảnh",
            "anh",
            "ảnh chụp",
            "anh chup",
            "dữ liệu",
            "du lieu",
        ) and os.path.dirname(abs_img):
            base = os.path.basename(os.path.dirname(abs_img))
        if base and base not in (".", "/", "\\"):
            return base

    if output_dir:
        abs_out = os.path.abspath(output_dir)
        base = os.path.basename(abs_out)
        if base.lower() in _EXPORT_SUBDIR_NAMES and os.path.dirname(abs_out):
            base = os.path.basename(os.path.dirname(abs_out))
        if base and base not in (".", "/", "\\"):
            return base

    return "project"


def _is_local_or_unreferenced_crs(crs) -> bool:
    """Return True when ``crs`` is None or a local/non-georeferenced coordinate system."""
    if crs is None:
        return True
    wkt = str(getattr(crs, "wkt", "") or "").upper()
    return "LOCAL_CS" in wkt


def _infer_model_format_from_path(model_path: str):
    """Infer Metashape ModelFormat enum from mesh file extension (defaulting to OBJ)."""
    if Metashape is None or not hasattr(Metashape, "ModelFormat"):
        return None
    ext = os.path.splitext(model_path)[1].lower()
    mapping = {
        ".obj": getattr(Metashape.ModelFormat, "ModelFormatOBJ", None),
        ".fbx": getattr(Metashape.ModelFormat, "ModelFormatFBX", None),
        ".ply": getattr(Metashape.ModelFormat, "ModelFormatPLY", None),
        ".glb": getattr(Metashape.ModelFormat, "ModelFormatGLTF", None),
        ".gltf": getattr(Metashape.ModelFormat, "ModelFormatGLTF", None),
        ".dxf": getattr(Metashape.ModelFormat, "ModelFormatDXF", None),
        ".stl": getattr(Metashape.ModelFormat, "ModelFormatSTL", None),
        ".3ds": getattr(Metashape.ModelFormat, "ModelFormat3DS", None),
    }
    return mapping.get(ext) or getattr(Metashape.ModelFormat, "ModelFormatOBJ", None)


def _build_metric_ortho_projection(chunk):
    """Build an OrthoProjection with projected metric UTM CRS when chunk CRS is geographic."""
    if Metashape is None or not hasattr(Metashape, "OrthoProjection"):
        return None
    proj = Metashape.OrthoProjection()
    target_crs = getattr(chunk, "crs", None)
    if _is_local_or_unreferenced_crs(target_crs):
        if target_crs is not None:
            try:
                proj.crs = target_crs
            except Exception:
                pass
        return proj
    if target_crs and not is_projected_crs(target_crs):
        import math

        lons: list[float] = []
        lats: list[float] = []
        for c in getattr(chunk, "cameras", None) or []:
            ref = getattr(c, "reference", None)
            if (
                ref is None
                or not getattr(ref, "enabled", True)
                or getattr(ref, "location", None) is None
                or not getattr(c, "transform", None)
                or not getattr(c, "enabled", True)
            ):
                continue
            lon, lat = float(ref.location.x), float(ref.location.y)
            if (abs(lon) > 1e-4 or abs(lat) > 1e-4) and abs(lon) <= 180.0 and abs(lat) <= 90.0:
                lons.append(lon)
                lats.append(lat)
        if not lons:
            # GCP-only georeferenced fallback: derive lon/lat from estimated camera centers
            mat = getattr(getattr(chunk, "transform", None), "matrix", None)
            if mat is not None:
                try:
                    wgs84 = Metashape.CoordinateSystem("EPSG::4326")
                    for c in getattr(chunk, "cameras", None) or []:
                        if getattr(c, "transform", None) and getattr(c, "enabled", True) and getattr(c, "center", None):
                            pt = wgs84.project(mat.mulp(c.center))
                            lon, lat = float(pt.x), float(pt.y)
                            if math.isfinite(lon) and math.isfinite(lat) and abs(lon) <= 180.0 and abs(lat) <= 90.0:
                                lons.append(lon)
                                lats.append(lat)
                except Exception:
                    pass
        if lons:
            lats.sort()
            ref_lon = math.degrees(
                math.atan2(sum(math.sin(math.radians(x)) for x in lons), sum(math.cos(math.radians(x)) for x in lons))
            )
            wrapped = sorted(((x - ref_lon + 180.0) % 360.0) - 180.0 for x in lons)
            med_lon = ((ref_lon + wrapped[len(wrapped) // 2] + 180.0) % 360.0) - 180.0
            med_lat = lats[len(lats) // 2]
            utm_epsg = compute_utm_epsg(med_lon, med_lat)
            try:
                target_crs = Metashape.CoordinateSystem(f"EPSG::{utm_epsg}")
            except Exception:
                target_crs = chunk.crs
    if target_crs:
        try:
            proj.crs = target_crs
        except Exception:
            pass
    return proj


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
    ortho_resolution: float = 0.05,
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
    save_project_changes: bool = False,
    bake_missing_texture: bool = False,
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

    if import_model_path and not os.path.isfile(import_model_path):
        raise FileNotFoundError(f"Requested import_model_path does not exist: {import_model_path}")

    emit_ipc_stage("project", f"Loading Metashape Project: {os.path.basename(project_path)}")
    doc = Metashape.Document()
    try:
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

        return _export_opened_document(
            doc=doc,
            project_path=project_path,
            project_dir=project_dir,
            output_dir=output_dir,
            project_name=project_name,
            texture_format=texture_format,
            rebake_texture=rebake_texture,
            texture_pages=texture_pages,
            fill_holes=fill_holes,
            ghosting_filter=ghosting_filter,
            import_model_path=import_model_path,
            export_point_cloud=export_point_cloud,
            point_cloud_format=point_cloud_format,
            export_dem=export_dem,
            export_orthomosaic=export_orthomosaic,
            ortho_resolution=ortho_resolution,
            export_report=export_report,
            report_title=report_title,
            export_tiled_model=export_tiled_model,
            tiled_model_format=tiled_model_format,
            tile_size=tile_size,
            export_contours=export_contours,
            contour_interval=contour_interval,
            contours_format=contours_format,
            remove_lighting=remove_lighting,
            delighting_color_mode=delighting_color_mode,
            calibrate_colors=calibrate_colors,
            smooth_mesh=smooth_mesh,
            smooth_strength=smooth_strength,
            decimate_mesh=decimate_mesh,
            export_glb=export_glb,
            export_obj=export_obj,
            export_fbx=export_fbx,
            export_ply=export_ply,
            export_dxf=export_dxf,
            export_tie_points=export_tie_points,
            save_project_changes=save_project_changes,
            bake_missing_texture=bake_missing_texture,
        )
    finally:
        try:
            doc.clear()
        except Exception:
            pass


def _export_opened_document(
    doc,
    project_path: str,
    project_dir: str,
    output_dir: str,
    project_name: str,
    texture_format: str,
    rebake_texture: bool,
    texture_pages: int,
    fill_holes: bool,
    ghosting_filter: bool,
    import_model_path: str | None,
    export_point_cloud: bool,
    point_cloud_format: str,
    export_dem: bool,
    export_orthomosaic: bool,
    ortho_resolution: float,
    export_report: bool,
    report_title: str | None,
    export_tiled_model: bool,
    tiled_model_format: str,
    tile_size: int,
    export_contours: bool,
    contour_interval: float,
    contours_format: str,
    remove_lighting: bool,
    delighting_color_mode: bool,
    calibrate_colors: bool,
    smooth_mesh: bool,
    smooth_strength: float,
    decimate_mesh: int | None,
    export_glb: bool,
    export_obj: bool,
    export_fbx: bool,
    export_ply: bool,
    export_dxf: bool,
    export_tie_points: bool,
    save_project_changes: bool = False,
    bake_missing_texture: bool = False,
):

    from autometashape.cluster.hardware import compute_discrete_gpu_mask

    gpus = (
        Metashape.app.enumGPUDevices() if hasattr(Metashape, "app") and hasattr(Metashape.app, "enumGPUDevices") else []
    )
    discrete_mask, selected_gpu_devs = compute_discrete_gpu_mask(list(gpus))
    active_gpu_names = [
        f"{d['name']} ({d['mem_mb'] / 1024:.1f} GB VRAM)" if d.get("mem_mb") else d["name"] for d in selected_gpu_devs
    ]

    is_network_worker = any("--worker" in arg.lower() for arg in sys.argv)
    explicit_worker_mask = os.environ.get("AUTOMETASHAPE_EXPLICIT_GPU_MASK", "").strip()
    if explicit_worker_mask:
        print(
            f"[Hardware] Honoring explicit operator GPU mask ({explicit_worker_mask}).",
            flush=True,
        )
    elif not is_network_worker:
        if discrete_mask:
            Metashape.app.gpu_mask = discrete_mask
            print(f"[Hardware] GPU: {', '.join(active_gpu_names)} (Mask: {discrete_mask})", flush=True)
    elif discrete_mask > 0:
        cur_mask = getattr(Metashape.app, "gpu_mask", None)
        if cur_mask in (0, 0xFFFFFFFF, -1, None):
            try:
                Metashape.app.gpu_mask = discrete_mask
                print(
                    f"[Hardware] Network worker GPU: {', '.join(active_gpu_names)} (Mask: {discrete_mask})",
                    flush=True,
                )
            except Exception:
                pass

    chunk = doc.chunk
    if not chunk:
        raise ValueError(f"Project '{project_path}' contains no active chunk!")

    needs_mesh = any(
        [
            export_glb,
            export_obj,
            export_fbx,
            export_ply,
            export_dxf,
            rebake_texture,
            smooth_mesh,
            bool(decimate_mesh and decimate_mesh > 0),
            remove_lighting,
            calibrate_colors,
        ]
    )

    if import_model_path and os.path.isfile(import_model_path):
        print(f"[Import] Importing 3D mesh model from: {import_model_path}", flush=True)
        imp_fmt = _infer_model_format_from_path(import_model_path)
        chunk.importModel(path=import_model_path, format=imp_fmt)
        if save_project_changes:
            doc.save()
            chunk = doc.chunk
        print(f"[Import] Re-linked model loaded into session: {project_path}", flush=True)
    elif not chunk.model and needs_mesh:
        candidates = [
            os.path.join(output_dir, f"{project_name}.obj"),
            os.path.join(project_dir, f"{project_name}.obj"),
            os.path.join(output_dir, "model.obj"),
            os.path.join(project_dir, "model.obj"),
        ]
        recovered = False
        for cand in candidates:
            if os.path.isfile(cand):
                print(f"[Auto-Recover] Project chunk has no model, but found existing: {cand}", flush=True)
                chunk.importModel(path=cand, format=Metashape.ModelFormat.ModelFormatOBJ)
                if save_project_changes:
                    doc.save()
                    chunk = doc.chunk
                print(f"[Auto-Recover] Re-linked '{cand}' into session successfully!", flush=True)
                recovered = True
                break
        if not recovered:
            raise ValueError(f"Project '{project_path}' contains no reconstructed 3D mesh model!")

    has_baked_texture = True
    if chunk.model:
        mesh_verts = len(chunk.model.vertices)
        mesh_faces = len(chunk.model.faces)
        emit_ipc_stage("mesh", f"Validating 3D Mesh Geometry ({mesh_verts:,} vertices, {mesh_faces:,} faces)")
        print(f"[Model Info] Geometry: {mesh_verts:,} vertices, {mesh_faces:,} polygonal faces.", flush=True)

        if smooth_mesh:
            print(f"[Mesh] Applying Laplacian smoothing (strength: {smooth_strength})...", flush=True)
            try:
                chunk.smoothModel(strength=smooth_strength, fix_borders=True, preserve_edges=True, replace_asset=False)
            except TypeError:
                chunk.smoothModel(strength=smooth_strength, fix_borders=True, preserve_edges=True)
            if save_project_changes:
                doc.save()
                chunk = doc.chunk

        if decimate_mesh and decimate_mesh > 0:
            print(f"[Mesh] Decimating surface mesh to {decimate_mesh:,} faces...", flush=True)
            try:
                chunk.decimateModel(face_count=decimate_mesh, replace_asset=False)
            except TypeError:
                chunk.decimateModel(face_count=decimate_mesh)
            if save_project_changes:
                doc.save()
                chunk = doc.chunk

        current_pages = 0
        try:
            from autometashape.core.pipeline import _get_model_texture_page_count

            current_pages = _get_model_texture_page_count(chunk.model)
        except Exception:
            if hasattr(chunk.model, "textures") and chunk.model.textures:
                current_pages = len(chunk.model.textures)
            elif hasattr(chunk.model, "texture") and chunk.model.texture():
                current_pages = 1

        needs_textured_mesh = any([export_glb, export_obj, export_fbx])
        will_bake_texture = rebake_texture or (current_pages == 0 and needs_textured_mesh and bake_missing_texture)
        has_baked_texture = current_pages > 0 or will_bake_texture
        if current_pages == 0 and needs_textured_mesh and not will_bake_texture:
            print(
                "  [Texture] Mesh has no existing texture atlas and --bake-missing-texture is False; "
                "exporting untextured geometry without automatic 16x 8K bake.",
                flush=True,
            )

        if calibrate_colors or will_bake_texture or remove_lighting:
            emit_ipc_stage("texture", "Processing Texture Atlas & Delighting")

        if calibrate_colors:
            print("\n[Color Calibration] Executing radiometric calibration & white balance...", flush=True)
            try:
                chunk.calibrateColors(source_data=Metashape.DataSource.ModelData, white_balance=True)
                if save_project_changes:
                    doc.save()
                    chunk = doc.chunk
            except Exception as e_col:
                print(f"  [WARNING] Color calibration issue: {e_col}. Continuing...", flush=True)

        if will_bake_texture:
            print(
                f"\n[Texture] Baking {texture_pages}x 8192x8192 (8K) Texture Atlas (Mosaic Blending, Fill Holes: {fill_holes}, Ghosting Filter: {ghosting_filter})...",
                flush=True,
            )
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
            print(f"  Texture baking completed in {(time.time() - t_tex) / 60:.2f} min.", flush=True)
            if save_project_changes:
                doc.save()
                chunk = doc.chunk
                print(f"  Updated project saved: {project_path}", flush=True)

        if remove_lighting:
            print("\n[Delighting] Removing harsh ambient shadows and illumination...", flush=True)
            try:
                chunk.removeLighting(color_mode=delighting_color_mode)
                if save_project_changes:
                    doc.save()
                    chunk = doc.chunk
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
            save_texture=has_baked_texture,
            embed_texture=has_baked_texture,
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
            save_texture=has_baked_texture,
            save_normals=True,
            save_colors=True,
        )
        obj_size = os.path.getsize(obj_out) / (1024 * 1024)
        print(f"  Saved: {obj_out} ({obj_size:.2f} MB) in {(time.time() - t_obj):.1f}s.", flush=True)

    # 3. Optional Dense Point Cloud Export
    emit_ipc_stage("deliverables", "Exporting Production Deliverables (Cloud, DEM, Ortho, Report)")
    if export_point_cloud and chunk.point_cloud and chunk.point_cloud.point_count > 0:
        pc_format_ext = point_cloud_format.lower() if point_cloud_format else "laz"
        pc_fmt, pc_ext = POINT_CLOUD_FORMAT_MAP.get(
            pc_format_ext,
            (Metashape.PointCloudFormat.PointCloudFormatLAZ, ".laz") if Metashape is not None else (None, ".laz"),
        )
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
        print(f"  Saved Point Cloud: {pc_out} ({os.path.getsize(pc_out) / (1024 * 1024):.2f} MB)", flush=True)

    # 4. Optional DEM Export
    if export_dem:
        has_dem = getattr(chunk, "elevation", None) is not None
        if not has_dem:
            print(
                "\n[DEM] Project contains no pre-built elevation model. Generating on-demand before export...",
                flush=True,
            )
            try:
                src_data = Metashape.DataSource.DepthMapsData
                if getattr(chunk, "point_cloud", None) is not None:
                    src_data = Metashape.DataSource.PointCloudData
                elif getattr(chunk, "depth_maps", None) is not None:
                    src_data = Metashape.DataSource.DepthMapsData
                elif getattr(chunk, "model", None) is not None:
                    src_data = Metashape.DataSource.ModelData
                dem_kwargs = {"source_data": src_data}
                proj_dem = _build_metric_ortho_projection(chunk)
                if proj_dem is not None:
                    dem_kwargs["projection"] = proj_dem
                chunk.buildDem(**dem_kwargs)
                if save_project_changes:
                    doc.save()
                    chunk = doc.chunk
                    print("  [DEM] Elevation model built and saved successfully.", flush=True)
                else:
                    print("  [DEM] Elevation model built in memory for export.", flush=True)
                has_dem = getattr(chunk, "elevation", None) is not None
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
            print(f"  Saved DEM: {dem_out} ({os.path.getsize(dem_out) / (1024 * 1024):.2f} MB)", flush=True)
        else:
            print("\n[DEM] Note: Project contains no elevation model; skipping DEM export.", flush=True)

    # 5. Optional Orthomosaic Export
    if export_orthomosaic:
        has_ortho = getattr(chunk, "orthomosaic", None) is not None
        eff_ortho_res = float(ortho_resolution) if ortho_resolution and ortho_resolution > 0 else 0.0
        proj_om = _build_metric_ortho_projection(chunk)
        proj_om_crs = getattr(proj_om, "crs", None) if proj_om is not None else getattr(chunk, "crs", None)
        ortho_crs_is_metric = is_projected_crs(proj_om_crs) or _is_local_or_unreferenced_crs(proj_om_crs)
        if not has_ortho:
            res_label = (
                f"{eff_ortho_res * 100:.1f} cm/px" if (eff_ortho_res > 0 and ortho_crs_is_metric) else "native GSD"
            )
            print(
                f"\n[Orthomosaic] Project contains no pre-built orthomosaic. Generating on-demand ({res_label}) before export...",
                flush=True,
            )
            try:
                if getattr(chunk, "elevation", None) is not None:
                    surf_data = Metashape.DataSource.ElevationData
                elif getattr(chunk, "model", None) is not None:
                    surf_data = Metashape.DataSource.ModelData
                else:
                    surf_data = Metashape.DataSource.ElevationData
                ortho_kwargs = {"surface_data": surf_data}
                if eff_ortho_res > 0 and ortho_crs_is_metric:
                    ortho_kwargs["resolution"] = eff_ortho_res
                if proj_om is not None:
                    ortho_kwargs["projection"] = proj_om
                chunk.buildOrthomosaic(**ortho_kwargs)
                if save_project_changes:
                    doc.save()
                    chunk = doc.chunk
                    print("  [Orthomosaic] Orthomosaic built and saved successfully.", flush=True)
                else:
                    print("  [Orthomosaic] Orthomosaic built in memory for export.", flush=True)
                has_ortho = getattr(chunk, "orthomosaic", None) is not None
            except Exception as e_ortho_build:
                print(f"  [WARNING] Could not build Orthomosaic on-demand: {e_ortho_build}.", flush=True)

        if has_ortho:
            ortho_out = os.path.join(output_dir, f"{project_name}_orthomosaic.tif")
            om_obj = getattr(chunk, "orthomosaic", None)
            om_crs = (
                getattr(om_obj, "crs", None)
                or getattr(getattr(om_obj, "projection", None), "crs", None)
                or getattr(chunk, "crs", None)
            )
            om_is_metric = is_projected_crs(om_crs) or _is_local_or_unreferenced_crs(om_crs)
            res_label = f"{eff_ortho_res * 100:.1f} cm/px" if (eff_ortho_res > 0 and om_is_metric) else "native GSD"
            print(f"\n[Orthomosaic] Exporting GeoTIFF Orthomosaic ({res_label}) to: {ortho_out}...", flush=True)
            comp = Metashape.ImageCompression()
            comp.tiff_compression = Metashape.ImageCompression.TiffCompression.TiffCompressionLZW
            comp.tiff_big = True
            export_ortho_kwargs = {
                "path": ortho_out,
                "source_data": Metashape.DataSource.OrthomosaicData,
                "image_format": Metashape.ImageFormat.ImageFormatTIFF,
                "image_compression": comp,
                "save_world": True,
                "save_alpha": True,
            }
            if eff_ortho_res > 0 and om_is_metric:
                export_ortho_kwargs["resolution"] = eff_ortho_res
            chunk.exportRaster(**export_ortho_kwargs)
            print(f"  Saved Orthomosaic: {ortho_out} ({os.path.getsize(ortho_out) / (1024 * 1024):.2f} MB)", flush=True)
        else:
            print("\n[Orthomosaic] Note: Project contains no orthomosaic; skipping export.", flush=True)

    # Export FBX
    if export_fbx and chunk.model:
        fbx_out = os.path.join(output_dir, f"{project_name}.fbx")
        print(f"\n[FBX] Exporting Autodesk FBX to: {fbx_out}...", flush=True)
        try:
            chunk.exportModel(
                fbx_out,
                format=Metashape.ModelFormat.ModelFormatFBX,
                save_texture=has_baked_texture,
                save_normals=True,
                save_colors=True,
            )
            print(f"  Saved FBX: {fbx_out} ({os.path.getsize(fbx_out) / (1024 * 1024):.2f} MB)", flush=True)
        except Exception as e_fbx:
            print(f"  [WARNING] FBX export issue: {e_fbx}", flush=True)

    # Export PLY
    if export_ply and chunk.model:
        ply_out = os.path.join(output_dir, f"{project_name}_mesh.ply")
        print(f"\n[PLY] Exporting Stanford PLY to: {ply_out}...", flush=True)
        try:
            chunk.exportModel(ply_out, format=Metashape.ModelFormat.ModelFormatPLY, save_normals=True, save_colors=True)
            print(f"  Saved PLY: {ply_out} ({os.path.getsize(ply_out) / (1024 * 1024):.2f} MB)", flush=True)
        except Exception as e_ply:
            print(f"  [WARNING] PLY export issue: {e_ply}", flush=True)

    # Export DXF
    if export_dxf and chunk.model:
        dxf_out = os.path.join(output_dir, f"{project_name}.dxf")
        print(f"\n[DXF] Exporting AutoCAD DXF to: {dxf_out}...", flush=True)
        try:
            chunk.exportModel(dxf_out, format=Metashape.ModelFormat.ModelFormatDXF, save_normals=True)
            print(f"  Saved DXF: {dxf_out} ({os.path.getsize(dxf_out) / (1024 * 1024):.2f} MB)", flush=True)
        except Exception as e_dxf:
            print(f"  [WARNING] DXF export issue: {e_dxf}", flush=True)

    # Export Sparse Tie Points
    if export_tie_points and chunk.tie_points:
        tp_out = os.path.join(output_dir, f"{project_name}_tie_points.ply")
        print(f"\n[Tie Points] Exporting Sparse Tie Points to: {tp_out}...", flush=True)
        try:
            chunk.exportPointCloud(
                path=tp_out,
                format=Metashape.PointCloudFormat.PointCloudFormatPLY,
                source_data=Metashape.DataSource.TiePointsData,
            )
            print(f"  Saved Tie Points: {tp_out} ({os.path.getsize(tp_out) / (1024 * 1024):.2f} MB)", flush=True)
        except Exception as e_tp:
            print(f"  [WARNING] Tie points export issue: {e_tp}", flush=True)

    # Export Hierarchical 3D Tiled Model
    if export_tiled_model:
        try:
            has_tiled = getattr(chunk, "tiled_model", None) is not None
            if not has_tiled:
                print(f"\n[Tiled Model] Building Hierarchical 3D Tiled Model (Tile Size: {tile_size})...", flush=True)
                tile_src = (
                    Metashape.DataSource.DepthMapsData
                    if getattr(chunk, "depth_maps", None) is not None
                    else Metashape.DataSource.ModelData
                )
                chunk.buildTiledModel(tile_size=tile_size, source_data=tile_src)
                if save_project_changes:
                    doc.save()
                    chunk = doc.chunk

            tiled_fmt_key = (tiled_model_format or "cesium").lower()
            tiled_fmt_tuple = TILED_MODEL_FORMAT_MAP.get(
                tiled_fmt_key,
                (Metashape.TiledModelFormat.TiledModelFormatCesium, "tileset.json")
                if Metashape is not None
                else (None, "tileset.json"),
            )
            tiled_fmt, tiled_ext = tiled_fmt_tuple
            if tiled_fmt_key == "cesium":
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
            print(
                "\n[Contours] Topographic contours require an elevation model. Generating DEM on-demand...", flush=True
            )
            try:
                src_data = Metashape.DataSource.DepthMapsData
                if getattr(chunk, "point_cloud", None) is not None:
                    src_data = Metashape.DataSource.PointCloudData
                elif getattr(chunk, "depth_maps", None) is not None:
                    src_data = Metashape.DataSource.DepthMapsData
                elif getattr(chunk, "model", None) is not None:
                    src_data = Metashape.DataSource.ModelData
                dem_kwargs = {"source_data": src_data}
                proj_dem = _build_metric_ortho_projection(chunk)
                if proj_dem is not None:
                    dem_kwargs["projection"] = proj_dem
                chunk.buildDem(**dem_kwargs)
                if save_project_changes:
                    doc.save()
                    chunk = doc.chunk
                has_dem = getattr(chunk, "elevation", None) is not None
            except Exception as e_dem_cnt:
                print(f"  [WARNING] Could not build DEM for contours: {e_dem_cnt}", flush=True)

        if has_dem:
            try:
                print(f"\n[Contours] Generating Topographic Contours (Interval: {contour_interval}m)...", flush=True)
                chunk.buildContours(
                    source_data=Metashape.DataSource.ElevationData,
                    interval=contour_interval,
                    prevent_intersections=True,
                )
                cnt_fmt_key = (contours_format or "shp").lower()
                sh_fmt, sh_ext = SHAPES_FORMAT_MAP.get(
                    cnt_fmt_key,
                    (Metashape.ShapesFormat.ShapesFormatSHP, ".shp") if Metashape is not None else (None, ".shp"),
                )
                cnt_out = os.path.join(output_dir, f"{project_name}_contours{sh_ext}")
                print(f"  Exporting Contour Shapes to: {cnt_out}...", flush=True)
                chunk.exportShapes(
                    path=cnt_out, format=sh_fmt, save_polylines=True, save_elevation=True, save_attributes=True
                )
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
        print(f"  Saved Report: {report_out} ({os.path.getsize(report_out) / (1024 * 1024):.2f} MB)", flush=True)

    print("\n[SUCCESS] Production assets exported successfully!", flush=True)
    emit_ipc_event(IPC_COMPLETE)
