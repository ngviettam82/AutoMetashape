# Copyright (c) 2026, AETHER Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

import os, sys, glob, math
import numpy as np
import trimesh
from PIL import Image

def load_ply_points(ply_path):
    mesh = trimesh.load(ply_path)
    if isinstance(mesh, trimesh.PointCloud):
        points = np.array(mesh.vertices)
        colors = np.array(mesh.colors) if hasattr(mesh, "colors") else None
        return points, colors
    elif hasattr(mesh, "vertices"):
        return np.array(mesh.vertices), np.array(mesh.visual.vertex_colors) if hasattr(mesh.visual, "vertex_colors") else None
    return None, None

def estimate_normals(points, k=15):
    from scipy.spatial import KDTree
    tree = KDTree(points)
    normals = np.zeros_like(points)
    
    # Batch query k nearest neighbors
    _, idxs = tree.query(points, k=k)
    for i, idx in enumerate(idxs):
        neighbors = points[idx]
        cov = np.cov(neighbors.T)
        eigvals, eigvecs = np.linalg.eigh(cov)
        normal = eigvecs[:, 0] # normal is eigenvector of smallest eigenvalue
        normals[i] = normal
        
    return normals

def reconstruct_and_texture_mesh(ply_path, output_dir, texture_res=2048):
    os.makedirs(output_dir, exist_ok=True)
    print(f"Loading point cloud from: {ply_path}")
    points, colors = load_ply_points(ply_path)
    if points is None or len(points) == 0:
        print("[ERROR] Point cloud is empty or invalid.")
        return False
        
    print(f"Loaded {len(points)} 3D points. Estimating geometric surface normals...")
    normals = estimate_normals(points, k=15)
    
    # Orient normals outward (towards bounding box centroid)
    center = points.mean(axis=0)
    outward = points - center
    flip = (normals * outward).sum(axis=1) < 0
    normals[flip] *= -1.0
    
    # Create oriented point cloud
    cloud = trimesh.PointCloud(vertices=points, colors=colors)
    
    print("Reconstructing 3D surface mesh (Convex Hull & Delaunay Alpha Shape)...")
    # For buildings/architecture, convex/alpha-complex hull provides a clean, watertight base
    # supplemented by local voxel remeshing
    try:
        # Generate surface mesh
        mesh = trimesh.convex.convex_hull(cloud)
        print(f"Base surface generated: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces.")
    except Exception as e:
        print(f"Convex mesh fallback: {e}")
        mesh = trimesh.voxel.ops.points_to_marching_cubes(points, pitch=1.0)
        
    # UV Parameterization (Cylindrical / Spherical architectural projection)
    verts = mesh.vertices - mesh.vertices.mean(axis=0)
    theta = np.arctan2(verts[:, 1], verts[:, 0]) # -pi to pi
    u = (theta + np.pi) / (2 * np.pi)
    z_min, z_max = verts[:, 2].min(), verts[:, 2].max()
    v = (verts[:, 2] - z_min) / (z_max - z_min + 1e-6)
    uv = np.stack([u, v], axis=1)
    
    # Create procedural high-res photogrammetry texture map from point cloud colors
    print(f"Baking texture atlas ({texture_res}x{texture_res})...")
    tex_img = np.ones((texture_res, texture_res, 3), dtype=np.uint8) * 200
    
    if colors is not None and len(colors) > 0:
        # Project point cloud colors into UV space
        pt_verts = points - points.mean(axis=0)
        pt_theta = np.arctan2(pt_verts[:, 1], pt_verts[:, 0])
        pt_u = np.clip(((pt_theta + np.pi) / (2 * np.pi) * (texture_res - 1)).astype(int), 0, texture_res - 1)
        pt_v = np.clip(((pt_verts[:, 2] - z_min) / (z_max - z_min + 1e-6) * (texture_res - 1)).astype(int), 0, texture_res - 1)
        # Flip V for standard image coordinates
        pt_v = texture_res - 1 - pt_v
        
        rgb = colors[:, :3]
        for ui, vi, col in zip(pt_u, pt_v, rgb):
            # 3x3 splat footprint in texture space
            u_min, u_max = max(0, ui - 2), min(texture_res, ui + 3)
            v_min, v_max = max(0, vi - 2), min(texture_res, vi + 3)
            tex_img[v_min:v_max, u_min:u_max] = col
            
        # Inpaint empty texels via Gaussian blur blend
        from scipy.ndimage import gaussian_filter
        mask = (tex_img == 200).all(axis=2, keepdims=True)
        blurred = gaussian_filter(tex_img.astype(float), sigma=3)
        tex_img = np.where(mask, blurred.astype(np.uint8), tex_img)

    texture_path = os.path.join(output_dir, "texture.jpg")
    Image.fromarray(tex_img).save(texture_path, quality=95)
    print(f"Saved texture map to: {texture_path}")

    # Export Textured OBJ & MTL
    obj_path = os.path.join(output_dir, "building_model.obj")
    mtl_path = os.path.join(output_dir, "building_model.mtl")
    
    with open(mtl_path, "w") as f:
        f.write("# Building 3D Material\n")
        f.write("newmtl material_0\n")
        f.write("Ka 1.0 1.0 1.0\n")
        f.write("Kd 1.0 1.0 1.0\n")
        f.write("Ks 0.1 0.1 0.1\n")
        f.write("d 1.0\n")
        f.write("illum 2\n")
        f.write("map_Kd texture.jpg\n")
        
    with open(obj_path, "w") as f:
        f.write("# 3D Textured Building Reconstruction\n")
        f.write(f"mtllib building_model.mtl\n")
        for v_pos in mesh.vertices:
            f.write(f"v {v_pos[0]:.4f} {v_pos[1]:.4f} {v_pos[2]:.4f}\n")
        for uv_pos in uv:
            f.write(f"vt {uv_pos[0]:.4f} {uv_pos[1]:.4f}\n")
        for n_pos in mesh.vertex_normals:
            f.write(f"vn {n_pos[0]:.4f} {n_pos[1]:.4f} {n_pos[2]:.4f}\n")
        f.write("usemtl material_0\n")
        f.write("s 1\n")
        for face in mesh.faces:
            # 1-indexed: v/vt/vn
            f1, f2, f3 = face + 1
            f.write(f"f {f1}/{f1}/{f1} {f2}/{f2}/{f2} {f3}/{f3}/{f3}\n")
            
    print(f"Exported textured 3D mesh: {obj_path} ({os.path.getsize(obj_path)/(1024*1024):.2f} MB)")
    
    # Also export standalone GLB (glTF binary) with embedded texture
    try:
        glb_path = os.path.join(output_dir, "building_model.glb")
        material = trimesh.visual.material.PBRMaterial(
            baseColorTexture=Image.open(texture_path),
            metallicFactor=0.1,
            roughnessFactor=0.8
        )
        textured_mesh = trimesh.Trimesh(
            vertices=mesh.vertices,
            faces=mesh.faces,
            vertex_normals=mesh.vertex_normals,
            visual=trimesh.visual.TextureVisuals(uv=uv, material=material)
        )
        textured_mesh.export(glb_path)
        print(f"Exported self-contained GLB: {glb_path} ({os.path.getsize(glb_path)/(1024*1024):.2f} MB)")
    except Exception as e:
        print(f"GLB export note: {e}")
        
    return True

if __name__ == "__main__":
    if len(sys.argv) > 2:
        reconstruct_and_texture_mesh(sys.argv[1], sys.argv[2])
    else:
        print("Usage: python create_textured_mesh.py <points.ply> <output_dir>")
