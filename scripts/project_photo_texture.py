# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""High-Fidelity Multi-View Photogrammetric Texture Baker.

Projects real 4K drone photographs directly onto the reconstructed 3D building
mesh using camera pose reprojection, view-angle selection, and affine UV warping.
"""

import os
import sys
import time
from collections import defaultdict
from typing import Dict, List, Tuple

import cv2
import numpy as np
import pycolmap
import trimesh
import xatlas
from PIL import Image
from scipy.spatial import KDTree


def load_cameras(colmap_dir: str, images_dir: str) -> List[Dict]:
    """Loads all camera extrinsics, intrinsics, and image paths from COLMAP."""
    rec = pycolmap.Reconstruction(colmap_dir)
    cameras = []
    for img_id, img in rec.images.items():
        if not img.has_camera_id():
            continue
        cam = rec.cameras[img.camera_id]
        img_path = os.path.join(images_dir, img.name)
        if not os.path.exists(img_path):
            continue

        pose = img.cam_from_world()
        R = pose.rotation.matrix()
        t = pose.translation
        C = pose.inverse().translation

        params = cam.params
        K = np.eye(3, dtype=np.float32)
        if cam.model_name in ["PINHOLE", "SIMPLE_RADIAL"]:
            K[0, 0] = params[0]
            K[1, 1] = params[0]
            K[0, 2] = params[1]
            K[1, 2] = params[2]
        elif cam.model_name in ["OPENCV", "FULL_OPENCV"]:
            K[0, 0] = params[0]
            K[1, 1] = params[1]
            K[0, 2] = params[2]
            K[1, 2] = params[3]
        else:
            K[0, 0] = params[0]
            K[1, 1] = params[0]
            K[0, 2] = cam.width / 2.0
            K[1, 2] = cam.height / 2.0

        cameras.append({
            "id": img_id,
            "name": img.name,
            "path": img_path,
            "R": R,
            "t": t,
            "C": C,
            "K": K,
            "width": cam.width,
            "height": cam.height,
        })
    return cameras


def bake_photographic_mesh(
    mesh_path: str,
    colmap_dir: str,
    images_dir: str,
    output_dir: str,
    texture_res: int = 4096,
    max_faces: int = 80000,
):
    """Bakes true multi-view drone photographs onto the 3D building mesh."""
    t0 = time.time()
    os.makedirs(output_dir, exist_ok=True)
    print(f"[1/5] Loading building mesh from: {mesh_path}")
    raw_mesh = trimesh.load(mesh_path)
    
    # Simplify to high-density clean polygon budget
    if len(raw_mesh.faces) > max_faces:
        print(f"Simplifying mesh from {len(raw_mesh.faces)} to {max_faces} faces...")
        mesh = raw_mesh.simplify_quadric_decimation(face_count=max_faces)
    else:
        mesh = raw_mesh

    # Apply Taubin surface smoothing to preserve sharp architectural edges while eliminating noise
    trimesh.smoothing.filter_taubin(mesh, lamb=0.5, nu=-0.53, iterations=10)
    print(f"Prepared mesh: {len(mesh.vertices)} vertices, {len(mesh.faces)} faces.")

    # UV Parameterization with xatlas
    print(f"[2/5] Computing non-overlapping UV parameterization with xatlas...")
    vmapping, indices, uvs = xatlas.parametrize(mesh.vertices, mesh.faces)
    verts = mesh.vertices[vmapping]
    faces = indices
    print(f"UV charts mapped: {len(verts)} vertices, {len(faces)} faces.")

    # Load cameras
    print(f"[3/5] Ingesting calibrated camera poses from COLMAP...")
    cameras = load_cameras(colmap_dir, images_dir)
    print(f"Loaded {len(cameras)} cameras.")

    # Compute optimal camera view per face
    print(f"[4/5] Computing optimal camera viewing angles for {len(faces)} faces...")
    v0 = verts[faces[:, 0]]
    v1 = verts[faces[:, 1]]
    v2 = verts[faces[:, 2]]
    face_centers = (v0 + v1 + v2) / 3.0
    tm = trimesh.Trimesh(vertices=verts, faces=faces)
    face_normals = tm.face_normals

    cam_positions = np.array([c["C"] for c in cameras])
    cam_tree = KDTree(cam_positions)
    _, nearest_cam_indices = cam_tree.query(face_centers, k=25)

    cam_to_faces = defaultdict(list)
    unassigned_faces = []

    for f_idx in range(len(faces)):
        fc = face_centers[f_idx]
        fn = face_normals[f_idx]
        cand_indices = nearest_cam_indices[f_idx]

        best_score = -1.0
        best_cam_idx = -1

        for c_idx in cand_indices:
            cam = cameras[c_idx]
            ray = cam["C"] - fc
            dist = np.linalg.norm(ray)
            if dist < 0.1 or dist > 60.0:
                continue
            cos_theta = np.dot(fn, ray / dist)
            if cos_theta < 0.2:
                continue

            # Check if triangle projects inside image
            v_tri = verts[faces[f_idx]]
            v_cam = (cam["R"] @ v_tri.T).T + cam["t"]
            if (v_cam[:, 2] <= 0.5).any():
                continue

            px_x = (cam["K"][0, 0] * v_cam[:, 0] / v_cam[:, 2]) + cam["K"][0, 2]
            px_y = (cam["K"][1, 1] * v_cam[:, 1] / v_cam[:, 2]) + cam["K"][1, 2]

            margin_x = cam["width"] * 0.02
            margin_y = cam["height"] * 0.02
            if (
                px_x.min() < margin_x
                or px_x.max() > (cam["width"] - margin_x)
                or px_y.min() < margin_y
                or px_y.max() > (cam["height"] - margin_y)
            ):
                continue

            score = cos_theta / dist
            if score > best_score:
                best_score = score
                best_cam_idx = c_idx

        if best_cam_idx >= 0:
            cam_to_faces[best_cam_idx].append(f_idx)
        else:
            unassigned_faces.append(f_idx)

    print(f"Assigned {len(faces) - len(unassigned_faces)} faces across {len(cam_to_faces)} camera views.")

    # Texture canvas
    print(f"[5/5] Baking {texture_res}x{texture_res} photographic texture atlas...")
    tex_img = np.zeros((texture_res, texture_res, 3), dtype=np.uint8)
    tex_mask = np.zeros((texture_res, texture_res), dtype=np.uint8)

    # Scale UVs to texture pixels
    u_px = uvs[:, 0] * (texture_res - 1)
    v_px = (1.0 - uvs[:, 1]) * (texture_res - 1)
    uv_px = np.stack([u_px, v_px], axis=1).astype(np.float32)

    total_baked = 0
    # Process camera batches
    for cam_idx, face_list in cam_to_faces.items():
        cam = cameras[cam_idx]
        photo = cv2.imread(cam["path"])
        if photo is None:
            continue

        scale_x = photo.shape[1] / float(cam["width"])
        scale_y = photo.shape[0] / float(cam["height"])
        K = cam["K"].copy()
        K[0] *= scale_x
        K[1] *= scale_y
        R = cam["R"]
        t = cam["t"]

        for f_idx in face_list:
            f = faces[f_idx]
            v_tri = verts[f]
            dst_tri = uv_px[f]

            # Project into photo
            v_cam = (R @ v_tri.T).T + t
            px_x = (K[0, 0] * v_cam[:, 0] / v_cam[:, 2]) + K[0, 2]
            px_y = (K[1, 1] * v_cam[:, 1] / v_cam[:, 2]) + K[1, 2]
            src_tri = np.stack([px_x, px_y], axis=1).astype(np.float32)

            dx, dy, dw, dh = cv2.boundingRect(dst_tri)
            sx, sy, sw, sh = cv2.boundingRect(src_tri)

            if dw < 1 or dh < 1 or sw < 1 or sh < 1:
                continue
            if sx < 0 or sy < 0 or sx + sw > photo.shape[1] or sy + sh > photo.shape[0]:
                continue
            if dx < 0 or dy < 0 or dx + dw > texture_res or dy + dh > texture_res:
                continue

            src_crop = photo[sy : sy + sh, sx : sx + sw]
            src_tri_local = src_tri - np.array([sx, sy], dtype=np.float32)
            dst_tri_local = dst_tri - np.array([dx, dy], dtype=np.float32)

            try:
                M = cv2.getAffineTransform(src_tri_local[:3], dst_tri_local[:3])
                warped = cv2.warpAffine(src_crop, M, (dw, dh), borderMode=cv2.BORDER_REFLECT_101)

                mask = np.zeros((dh, dw), dtype=np.uint8)
                cv2.fillConvexPoly(mask, dst_tri_local[:3].astype(np.int32), 255)

                dest_patch = tex_img[dy : dy + dh, dx : dx + dw]
                mask_3d = (mask > 0)[:, :, None]
                tex_img[dy : dy + dh, dx : dx + dw] = np.where(mask_3d, warped, dest_patch)
                tex_mask[dy : dy + dh, dx : dx + dw] = np.where(mask > 0, 255, tex_mask[dy : dy + dh, dx : dx + dw])
                total_baked += 1
            except Exception:
                continue

    print(f"Photographic reprojection complete ({total_baked} faces). Inpainting occluded areas...")

    # Load dense point cloud colors for fallback
    pts_ply = trimesh.load(os.path.join(colmap_dir, "points3D.ply"))
    pts = np.array(pts_ply.vertices)
    cols = np.array(pts_ply.colors)[:, :3]
    pt_tree = KDTree(pts)
    _, pt_idxs = pt_tree.query(verts, k=1)
    vert_colors = cols[pt_idxs]
    face_colors = vert_colors[faces].mean(axis=1).astype(np.uint8)

    # Fill any untextured faces with photogrammetric surface colors
    for f_idx in unassigned_faces:
        dst_tri = uv_px[faces[f_idx]]
        dx, dy, dw, dh = cv2.boundingRect(dst_tri)
        if dw < 1 or dh < 1 or dx < 0 or dy < 0 or dx + dw > texture_res or dy + dh > texture_res:
            continue
        col = face_colors[f_idx].tolist()
        bgr = [int(col[2]), int(col[1]), int(col[0])]
        cv2.fillConvexPoly(tex_img, dst_tri.astype(np.int32), bgr)
        cv2.fillConvexPoly(tex_mask, dst_tri.astype(np.int32), 255)

    # Inpaint background margins and seams via morphological edge dilation
    tex_rgb = cv2.cvtColor(tex_img, cv2.COLOR_BGR2RGB)
    empty_mask = (tex_mask == 0)
    kernel = np.ones((7, 7), np.uint8)
    dilated = cv2.dilate(tex_rgb, kernel, iterations=3)
    final_texture = np.where(empty_mask[:, :, None], dilated, tex_rgb)

    # Save texture map
    tex_path = os.path.join(output_dir, "texture.jpg")
    Image.fromarray(final_texture).save(tex_path, quality=95)
    print(f"[SUCCESS] Saved 4K photogrammetric texture: {tex_path}")

    # Export textured OBJ & MTL
    obj_path = os.path.join(output_dir, "model.obj")
    mtl_path = os.path.join(output_dir, "model.mtl")

    with open(mtl_path, "w") as f:
        f.write("# Building 3D Material\nnewmtl material_0\nKa 1.0 1.0 1.0\nKd 1.0 1.0 1.0\n")
        f.write("Ks 0.1 0.1 0.1\nd 1.0\nillum 2\nmap_Kd texture.jpg\n")

    with open(obj_path, "w") as f:
        f.write("# 3D Textured Building Reconstruction\nmtllib model.mtl\n")
        for v in verts:
            f.write(f"v {v[0]:.4f} {v[1]:.4f} {v[2]:.4f}\n")
        for uv in uvs:
            f.write(f"vt {uv[0]:.4f} {uv[1]:.4f}\n")
        for n in tm.vertex_normals:
            f.write(f"vn {n[0]:.4f} {n[1]:.4f} {n[2]:.4f}\n")
        f.write("usemtl material_0\ns 1\n")
        for face in faces:
            f1, f2, f3 = face + 1
            f.write(f"f {f1}/{f1}/{f1} {f2}/{f2}/{f2} {f3}/{f3}/{f3}\n")

    print(f"[SUCCESS] Saved Wavefront OBJ: {obj_path} ({os.path.getsize(obj_path)/(1024*1024):.2f} MB)")

    # Export GLB (binary glTF) with embedded 4K photo texture
    glb_path = os.path.join(output_dir, "model.glb")
    mat = trimesh.visual.material.PBRMaterial(
        baseColorTexture=Image.open(tex_path),
        metallicFactor=0.05,
        roughnessFactor=0.8,
    )
    textured_mesh = trimesh.Trimesh(
        vertices=verts,
        faces=faces,
        vertex_normals=tm.vertex_normals,
        visual=trimesh.visual.TextureVisuals(uv=uvs, material=mat),
    )
    textured_mesh.export(glb_path)
    print(f"[SUCCESS] Saved self-contained GLB: {glb_path} ({os.path.getsize(glb_path)/(1024*1024):.2f} MB)")
    print(f"End-to-end texture baking completed in {time.time() - t0:.2f} seconds!")


if __name__ == "__main__":
    bake_photographic_mesh(
        mesh_path=r"data\dense\building_clean.ply",
        colmap_dir=r"data\from_scratch_sfm\sparse\1",
        images_dir=r"data\toanha1\images",
        output_dir=r"data\final_model",
        texture_res=4096,
        max_faces=80000,
    )
