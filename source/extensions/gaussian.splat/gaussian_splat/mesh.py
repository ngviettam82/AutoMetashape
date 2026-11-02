# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Surface reconstruction, UV parameterization, and texture map baking."""

import os
from typing import Tuple
import numpy as np
from PIL import Image
import trimesh


def load_ply_points(ply_path: str) -> Tuple[np.ndarray, np.ndarray]:
    """Loads 3D points and vertex colors from a PLY point cloud."""
    mesh = trimesh.load(ply_path)
    if isinstance(mesh, trimesh.PointCloud):
        points = np.array(mesh.vertices)
        colors = np.array(mesh.colors) if hasattr(mesh, "colors") else None
        return points, colors
    elif hasattr(mesh, "vertices"):
        return np.array(mesh.vertices), (
            np.array(mesh.visual.vertex_colors) if hasattr(mesh.visual, "vertex_colors") else None
        )
    return np.empty((0, 3)), None


def estimate_normals(points: np.ndarray, k: int = 15) -> np.ndarray:
    """Estimates geometric surface normals using PCA on k-nearest neighbors."""
    from scipy.spatial import KDTree

    tree = KDTree(points)
    normals = np.zeros_like(points)
    _, idxs = tree.query(points, k=k)

    for i, idx in enumerate(idxs):
        neighbors = points[idx]
        cov = np.cov(neighbors.T)
        eigvals, eigvecs = np.linalg.eigh(cov)
        normals[i] = eigvecs[:, 0]

    return normals


def create_textured_model(ply_path: str, output_dir: str, texture_res: int = 2048) -> bool:
    """Reconstructs a watertight polygon mesh and bakes an OBJ/MTL/GLB texture atlas."""
    os.makedirs(output_dir, exist_ok=True)
    points, colors = load_ply_points(ply_path)
    if len(points) == 0:
        return False

    normals = estimate_normals(points, k=15)
    center = points.mean(axis=0)
    outward = points - center
    flip = (normals * outward).sum(axis=1) < 0
    normals[flip] *= -1.0

    cloud = trimesh.PointCloud(vertices=points, colors=colors)
    try:
        mesh = trimesh.convex.convex_hull(cloud)
    except Exception:
        mesh = trimesh.voxel.ops.points_to_marching_cubes(points, pitch=1.0)

    verts = mesh.vertices - mesh.vertices.mean(axis=0)
    theta = np.arctan2(verts[:, 1], verts[:, 0])
    u = (theta + np.pi) / (2 * np.pi)
    z_min, z_max = verts[:, 2].min(), verts[:, 2].max()
    v = (verts[:, 2] - z_min) / (z_max - z_min + 1e-6)
    uv = np.stack([u, v], axis=1)

    tex_img = np.ones((texture_res, texture_res, 3), dtype=np.uint8) * 200
    if colors is not None and len(colors) > 0:
        pt_verts = points - points.mean(axis=0)
        pt_theta = np.arctan2(pt_verts[:, 1], pt_verts[:, 0])
        pt_u = np.clip(((pt_theta + np.pi) / (2 * np.pi) * (texture_res - 1)).astype(int), 0, texture_res - 1)
        pt_v = np.clip(
            ((pt_verts[:, 2] - z_min) / (z_max - z_min + 1e-6) * (texture_res - 1)).astype(int), 0, texture_res - 1
        )
        pt_v = texture_res - 1 - pt_v

        rgb = colors[:, :3]
        for ui, vi, col in zip(pt_u, pt_v, rgb):
            u_min, u_max = max(0, ui - 2), min(texture_res, ui + 3)
            v_min, v_max = max(0, vi - 2), min(texture_res, vi + 3)
            tex_img[v_min:v_max, u_min:u_max] = col

        from scipy.ndimage import gaussian_filter

        mask = (tex_img == 200).all(axis=2, keepdims=True)
        blurred = gaussian_filter(tex_img.astype(float), sigma=3)
        tex_img = np.where(mask, blurred.astype(np.uint8), tex_img)

    texture_path = os.path.join(output_dir, "texture.jpg")
    Image.fromarray(tex_img).save(texture_path, quality=95)

    obj_path = os.path.join(output_dir, "model.obj")
    mtl_path = os.path.join(output_dir, "model.mtl")

    with open(mtl_path, "w") as f:
        f.write("# Building 3D Material\nnewmtl material_0\nKa 1.0 1.0 1.0\nKd 1.0 1.0 1.0\n")
        f.write("Ks 0.1 0.1 0.1\nd 1.0\nillum 2\nmap_Kd texture.jpg\n")

    with open(obj_path, "w") as f:
        f.write("# 3D Textured Building Reconstruction\nmtllib model.mtl\n")
        for v_pos in mesh.vertices:
            f.write(f"v {v_pos[0]:.4f} {v_pos[1]:.4f} {v_pos[2]:.4f}\n")
        for uv_pos in uv:
            f.write(f"vt {uv_pos[0]:.4f} {uv_pos[1]:.4f}\n")
        for n_pos in mesh.vertex_normals:
            f.write(f"vn {n_pos[0]:.4f} {n_pos[1]:.4f} {n_pos[2]:.4f}\n")
        f.write("usemtl material_0\ns 1\n")
        for face in mesh.faces:
            f1, f2, f3 = face + 1
            f.write(f"f {f1}/{f1}/{f1} {f2}/{f2}/{f2} {f3}/{f3}/{f3}\n")

    try:
        glb_path = os.path.join(output_dir, "model.glb")
        material = trimesh.visual.material.PBRMaterial(
            baseColorTexture=Image.open(texture_path), metallicFactor=0.1, roughnessFactor=0.8
        )
        textured_mesh = trimesh.Trimesh(
            vertices=mesh.vertices,
            faces=mesh.faces,
            vertex_normals=mesh.vertex_normals,
            visual=trimesh.visual.TextureVisuals(uv=uv, material=material),
        )
        textured_mesh.export(glb_path)
    except Exception:
        pass

    return True
