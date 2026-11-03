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

import argparse
import os
import sys
import time
import Metashape


def export_project_assets(
    project_path: str,
    output_dir: str = None,
    texture_format: str = "tiff",
    rebake_texture: bool = False,
    texture_pages: int = 16,
    fill_holes: bool = True,
    ghosting_filter: bool = True,
):
    """Exports production assets from a solved Metashape project."""
    if not os.path.exists(project_path):
        raise FileNotFoundError(f"Project file not found: {project_path}")

    project_dir = os.path.dirname(os.path.abspath(project_path))
    if not output_dir:
        output_dir = project_dir
    os.makedirs(output_dir, exist_ok=True)

    print("=========================================================================")
    print("  AUTOMETASHAPE: PRODUCTION ASSET EXPORT")
    print(f"  Project: {project_path}")
    print(f"  Destination: {output_dir}")
    print("=========================================================================", flush=True)

    doc = Metashape.Document()
    try:
        doc.open(project_path)
    except Exception as e:
        raise RuntimeError(f"Failed to open Metashape project '{project_path}': {e}")

    chunk = doc.chunk
    if not chunk:
        raise ValueError(f"Project '{project_path}' contains no active chunk!")

    if not chunk.model:
        raise ValueError(f"Project '{project_path}' contains no reconstructed 3D mesh model!")

    mesh_verts = len(chunk.model.vertices)
    mesh_faces = len(chunk.model.faces)
    print(f"[Model Info] Geometry: {mesh_verts:,} vertices, {mesh_faces:,} polygonal faces.", flush=True)

    # Optional / Conditional texture re-baking
    current_pages = 0
    if hasattr(chunk.model, "textures") and chunk.model.textures:
        current_pages = len(chunk.model.textures)
    elif hasattr(chunk.model, "texture") and chunk.model.texture():
        current_pages = 1

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
        print(f"  Updated project saved: {project_path}", flush=True)

    glb_out = os.path.join(output_dir, "model.glb")
    obj_out = os.path.join(output_dir, "model.obj")

    # 1. Export Binary GLTF/GLB
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

    # 2. Export Wavefront OBJ + TIFF textures
    tex_fmt = Metashape.ImageFormat.ImageFormatTIFF if texture_format.lower() == "tiff" else Metashape.ImageFormat.ImageFormatJPEG
    print(f"\n[2/2] Exporting Wavefront OBJ (.obj + .mtl) with {texture_format.upper()} texture atlas...", flush=True)
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

    print("\n[SUCCESS] Production assets exported successfully!", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="AutoMetashape Export CLI: Export production 3D assets from existing Metashape project (.psx)"
    )
    parser.add_argument("--project", required=True, help="Path to existing Metashape project file (.psx)")
    parser.add_argument("--output", default=None, help="Output directory for exported 3D assets (default: same folder as .psx)")
    parser.add_argument("--texture-format", choices=["tiff", "jpeg"], default="tiff", help="Texture atlas format (default: tiff)")
    parser.add_argument("--rebake-texture", action="store_true", default=False, help="Re-bake UV parameterization and 8K texture atlas before export")
    parser.add_argument("--texture-pages", type=int, default=16, help="Number of 8K texture atlas pages if re-baking (default: 16)")
    parser.add_argument("--no-fill-holes", dest="fill_holes", action="store_false", default=True, help="Disable texture hole filling if re-baking (default: enabled)")
    parser.add_argument("--no-ghosting-filter", dest="ghosting_filter", action="store_false", default=True, help="Disable ghosting filter if re-baking (default: enabled)")
    args = parser.parse_args()

    export_project_assets(
        project_path=args.project,
        output_dir=args.output,
        texture_format=args.texture_format,
        rebake_texture=args.rebake_texture,
        texture_pages=args.texture_pages,
        fill_holes=args.fill_holes,
        ghosting_filter=args.ghosting_filter,
    )
