# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Distributed cluster batch orchestrator, monolithic runscript bundling, and failover management."""

from __future__ import annotations

import os
import shutil
import subprocess
import time

try:
    import Metashape
except ImportError:
    Metashape = None


def _compute_utm_epsg(lon: float, lat: float) -> int:
    """Calculates standard WGS 84 / UTM Zone EPSG code for longitude and latitude."""
    zone = int((lon + 180.0) // 6.0) + 1
    zone = max(1, min(zone, 60))
    return 32600 + zone if lat >= 0.0 else 32700 + zone


# Alignment & depth quality maps for cluster tasks
ALIGN_DOWNSCALE_MAP = {
    "highest": 0,
    "high": 1,
    "medium": 2,
    "low": 4,
    "lowest": 8,
}

DEPTH_DOWNSCALE_MAP = {
    "ultra": 1,
    "high": 2,
    "medium": 4,
    "low": 8,
    "lowest": 16,
}

if Metashape is not None:
    DEPTH_FILTER_MAP = {
        "mild": Metashape.FilterMode.MildFiltering,
        "moderate": Metashape.FilterMode.ModerateFiltering,
        "aggressive": Metashape.FilterMode.AggressiveFiltering,
    }
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
    SURFACE_TYPE_MAP = {
        "arbitrary": Metashape.SurfaceType.Arbitrary,
        "heightfield": Metashape.SurfaceType.HeightField,
    }
    PRESEL_MODE_MAP = {
        "estimated": Metashape.ReferencePreselectionMode.ReferencePreselectionEstimated,
        "source": Metashape.ReferencePreselectionMode.ReferencePreselectionSource,
        "sequential": Metashape.ReferencePreselectionMode.ReferencePreselectionSequential,
    }
    SHAPES_FORMAT_MAP = {
        "shp": (Metashape.ShapesFormat.ShapesFormatSHP, ".shp"),
        "dxf": (Metashape.ShapesFormat.ShapesFormatDXF, ".dxf"),
        "geojson": (Metashape.ShapesFormat.ShapesFormatGeoJSON, ".geojson"),
        "kml": (Metashape.ShapesFormat.ShapesFormatKML, ".kml"),
    }
    TARGET_TYPE_MAP = {
        "circular12": Metashape.TargetType.CircularTarget12bit,
        "circular14": Metashape.TargetType.CircularTarget14bit,
        "circular16": Metashape.TargetType.CircularTarget16bit,
        "circular20": Metashape.TargetType.CircularTarget20bit,
        "cross": Metashape.TargetType.CrossTarget,
        "apriltag16h5": Metashape.TargetType.AprilTag16h5,
        "apriltag25h9": Metashape.TargetType.AprilTag25h9,
        "apriltag36h11": Metashape.TargetType.AprilTag36h11,
    }
else:
    DEPTH_FILTER_MAP = {"mild": 1, "moderate": 2, "aggressive": 3}
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
    TARGET_TYPE_MAP = {}
    SURFACE_TYPE_MAP = {"arbitrary": 0, "heightfield": 1}
    PRESEL_MODE_MAP = {"estimated": 1, "source": 0, "sequential": 2}


def _norm_cluster_path(p: str | None) -> str:
    """Normalize file path for case-insensitive cluster path matching."""
    if not p:
        return ""
    return p.strip().replace("\\", "/").rstrip("/").lower()


def _has_dense_points(chunk) -> bool:
    """Helper to safely check if chunk has dense points, resilient against Mock objects in unit tests."""
    if chunk and chunk.point_cloud is not None:
        cnt = getattr(chunk.point_cloud, "point_count", 0)
        if isinstance(cnt, (int, float)):
            return cnt > 0
    return False


def build_cluster_gradual_selection_tasks(
    max_reproj_error: float = 0.50,
    max_uncertainty: float = 15.0,
    pa_thresh: float = 3.0,
    min_rays: int = 2,
) -> list:
    """Builds a sequence of native CleanTiePoints and OptimizeCameras tasks for cluster workers."""
    if Metashape is None:
        return []
    tasks = []
    # Pass 1: Reconstruction Uncertainty
    t1 = Metashape.Tasks.CleanTiePoints()
    t1.criterion = Metashape.TiePoints.Criterion.ReconstructionUncertainty
    t1.threshold = max_uncertainty
    o1 = Metashape.Tasks.OptimizeCameras()
    o1.fit_f = o1.fit_cx = o1.fit_cy = True
    o1.fit_b1 = o1.fit_b2 = False
    o1.fit_k1 = o1.fit_k2 = o1.fit_k3 = True
    o1.fit_k4 = o1.fit_p1 = o1.fit_p2 = False
    o1.adaptive_fitting = False
    tasks.extend([t1, o1])

    # Pass 2: Projection Accuracy
    t2 = Metashape.Tasks.CleanTiePoints()
    t2.criterion = Metashape.TiePoints.Criterion.ProjectionAccuracy
    t2.threshold = pa_thresh
    o2 = Metashape.Tasks.OptimizeCameras()
    o2.fit_f = o2.fit_cx = o2.fit_cy = True
    o2.fit_b1 = o2.fit_b2 = False
    o2.fit_k1 = o2.fit_k2 = o2.fit_k3 = True
    o2.fit_k4 = o2.fit_p1 = o2.fit_p2 = False
    o2.adaptive_fitting = False
    tasks.extend([t2, o2])

    # Pass 3: Reprojection Error
    t3 = Metashape.Tasks.CleanTiePoints()
    t3.criterion = Metashape.TiePoints.Criterion.ReprojectionError
    t3.threshold = max_reproj_error
    o3 = Metashape.Tasks.OptimizeCameras()
    o3.fit_f = o3.fit_cx = o3.fit_cy = o3.fit_b1 = o3.fit_b2 = True
    o3.fit_k1 = o3.fit_k2 = o3.fit_k3 = o3.fit_k4 = True
    o3.fit_p1 = o3.fit_p2 = True
    o3.adaptive_fitting = True
    o3.tiepoint_covariance = True
    tasks.extend([t3, o3])

    # Pass 4: Multi-ray Multiplicity
    t4 = Metashape.Tasks.CleanTiePoints()
    t4.criterion = Metashape.TiePoints.Criterion.ImageCount
    t4.threshold = max(2, min_rays)
    o4 = Metashape.Tasks.OptimizeCameras()
    o4.fit_f = o4.fit_cx = o4.fit_cy = o4.fit_b1 = o4.fit_b2 = True
    o4.fit_k1 = o4.fit_k2 = o4.fit_k3 = o4.fit_k4 = True
    o4.fit_p1 = o4.fit_p2 = True
    o4.adaptive_fitting = True
    o4.tiepoint_covariance = True
    tasks.extend([t4, o4])

    return tasks


def build_cluster_crop_task(crop_method: str = "gps-square", padding: float = 0.05):
    """Builds a RunScript task that executes horizontal enclosing rectangle or structure crop on the worker."""
    if Metashape is None:
        return None
    pad_str = f"{padding:.4f}"
    if crop_method == "gps-square":
        code = (
            "import Metashape, math\n"
            "chunk = Metashape.app.document.chunk\n"
            "cams = [c for c in chunk.cameras if c.transform and c.enabled]\n"
            "if len(cams) >= 2:\n"
            "    rot = chunk.region.rot\n"
            "    rot_t = rot.t()\n"
            "    valid = [c for c in cams if (not c.reference or not c.reference.location or "
            "abs(c.reference.location.x) > 1e-4 or abs(c.reference.location.y) > 1e-4)]\n"
            "    if not valid: valid = cams\n"
            "    locs = [rot_t * c.center for c in valid]\n"
            "    xs, ys, zs = [p.x for p in locs], [p.y for p in locs], [p.z for p in locs]\n"
            "    sorted_xs, sorted_ys = sorted(xs), sorted(ys)\n"
            "    med_x, med_y = sorted_xs[len(sorted_xs)//2], sorted_ys[len(sorted_ys)//2]\n"
            "    dists = [math.hypot(x - med_x, y - med_y) for x, y in zip(xs, ys)]\n"
            "    med_d = sorted(dists)[len(dists)//2]\n"
            "    devs_d = sorted([abs(d - med_d) for d in dists])\n"
            "    mad_d = devs_d[len(devs_d)//2]\n"
            "    sigma_d = 1.4826 * mad_d\n"
            "    inlier_xs, inlier_ys, inlier_zs = [], [], []\n"
            "    for x, y, z, d in zip(xs, ys, zs, dists):\n"
            "        if sigma_d > 1e-3 and (d - med_d) > max(1000.0, 5.0 * sigma_d): continue\n"
            "        inlier_xs.append(x); inlier_ys.append(y); inlier_zs.append(z)\n"
            "    if len(inlier_xs) < 2: inlier_xs, inlier_ys, inlier_zs = xs, ys, zs\n"
            "    best_area, best_theta, best_bounds = float('inf'), 0.0, (0.0, 0.0, 0.0, 0.0)\n"
            "    for step in range(180):\n"
            "        theta = math.radians(step * 0.5)\n"
            "        cos_t, sin_t = math.cos(theta), math.sin(theta)\n"
            "        rx = [x * cos_t + y * sin_t for x, y in zip(inlier_xs, inlier_ys)]\n"
            "        ry = [-x * sin_t + y * cos_t for x, y in zip(inlier_xs, inlier_ys)]\n"
            "        min_rx, max_rx = min(rx), max(rx)\n"
            "        min_ry, max_ry = min(ry), max(ry)\n"
            "        area = (max_rx - min_rx) * (max_ry - min_ry)\n"
            "        if area < best_area:\n"
            "            best_area = area; best_theta = theta; best_bounds = (min_rx, max_rx, min_ry, max_ry)\n"
            f"    padding = {pad_str}\n"
            "    min_rx, max_rx, min_ry, max_ry = best_bounds\n"
            "    span_x = max_rx - min_rx\n"
            "    span_y = max_ry - min_ry\n"
            "    pts = [p for p in chunk.tie_points.points if p.valid] if chunk.tie_points else []\n"
            "    if pts:\n"
            "        coords = [Metashape.Vector([p.coord.x / p.coord.w, p.coord.y / p.coord.w, "
            "p.coord.z / p.coord.w]) for p in pts]\n"
            "        pt_zs = sorted([(rot_t * v).z for v in coords])\n"
            "        med_pz = pt_zs[len(pt_zs) // 2]\n"
            "        devs_pz = sorted([abs(z - med_pz) for z in pt_zs])\n"
            "        mad_pz = devs_pz[len(devs_pz) // 2]\n"
            "        sigma_pz = 1.4826 * mad_pz\n"
            "        filtered_pt_zs = [z for z in pt_zs if abs(z - med_pz) <= 4.0 * sigma_pz] "
            "if sigma_pz > 1e-3 else pt_zs\n"
            "        all_zs = inlier_zs + (filtered_pt_zs if filtered_pt_zs else pt_zs)\n"
            "        med_cam_z = sorted(inlier_zs)[len(inlier_zs) // 2]\n"
            "        flight_height = abs(med_cam_z - med_pz)\n"
            "    else:\n"
            "        all_zs = inlier_zs\n"
            "        flight_height = 0.0\n"
            "    fov_margin = flight_height * 0.35 if flight_height > 1e-3 else 0.0\n"
            "    margin_x = max(span_x * padding, fov_margin)\n"
            "    margin_y = max(span_y * padding, fov_margin)\n"
            "    side_x = span_x + 2.0 * margin_x\n"
            "    side_y = span_y + 2.0 * margin_y\n"
            "    mid_rx = (min_rx + max_rx) * 0.5\n"
            "    mid_ry = (min_ry + max_ry) * 0.5\n"
            "    cos_t, sin_t = math.cos(best_theta), math.sin(best_theta)\n"
            "    mid_x = mid_rx * cos_t - mid_ry * sin_t\n"
            "    mid_y = mid_rx * sin_t + mid_ry * cos_t\n"
            "    min_z, max_z = min(all_zs), max(all_zs)\n"
            "    span_z = max(chunk.region.size.z, (max_z - min_z) * (1.0 + 2.0 * padding))\n"
            "    mid_z = (min_z + max_z) * 0.5\n"
            "    Rz = Metashape.Matrix([[cos_t, -sin_t, 0.0], [sin_t, cos_t, 0.0], [0.0, 0.0, 1.0]])\n"
            "    chunk.region.rot = rot * Rz\n"
            "    chunk.region.center = rot * Metashape.Vector([mid_x, mid_y, mid_z])\n"
            "    chunk.region.size = Metashape.Vector([side_x, side_y, span_z])\n"
            "    print(f'  [Worker] Auto-Crop (Option A applied): "
            "Size=({side_x:.1f}m x {side_y:.1f}m x {span_z:.1f}m)')\n"
        )
    elif crop_method == "structure":
        code = (
            "import Metashape, math\n"
            "chunk = Metashape.app.document.chunk\n"
            "pts = [p.coord for p in chunk.tie_points.points if p.valid] if chunk.tie_points else []\n"
            "if len(pts) >= 10:\n"
            "    rot = chunk.region.rot\n"
            "    rot_t = rot.t()\n"
            "    locs = [rot_t * Metashape.Vector([p.x/p.w, p.y/p.w, p.z/p.w]) for p in pts]\n"
            "    xs = sorted([p.x for p in locs])\n"
            "    ys = sorted([p.y for p in locs])\n"
            "    zs = sorted([p.z for p in locs])\n"
            "    n = len(locs)\n"
            "    i0, i1 = int(n * 0.05), int(n * 0.95)\n"
            "    min_x, max_x = xs[i0], xs[i1]\n"
            "    min_y, max_y = ys[i0], ys[i1]\n"
            "    min_z, max_z = zs[i0], zs[i1]\n"
            f"    padding = {pad_str}\n"
            "    dx = (max_x - min_x) * (1.0 + padding)\n"
            "    dy = (max_y - min_y) * (1.0 + padding)\n"
            "    dz = (max_z - min_z) * (1.0 + padding * 2.0)\n"
            "    chunk.region.center = rot * Metashape.Vector([(min_x + max_x)/2.0, (min_y + max_y)/2.0, "
            "(min_z + max_z)/2.0])\n"
            "    chunk.region.size = Metashape.Vector([dx, dy, dz])\n"
            "    print(f'  [Worker] Auto-Crop (Structure applied): Size=({dx:.1f}m, {dy:.1f}m, {dz:.1f}m)')\n"
        )
    else:
        return None

    t_crop = Metashape.Tasks.RunScript()
    t_crop.code = code
    return t_crop


def build_monolithic_runscript_task(
    images_dir: str,
    output_dir: str,
    project_name: str,
    preset: str = "pro",
    align_quality: str = "high",
    depth_quality: str = "high",
    depth_filtering: str = "moderate",
    custom_faces: int | None = 2000000,
    texture_pages: int | None = 16,
    crop_method: str | None = "none",
    texture_format: str = "png",
    align_photos: bool = True,
    build_depth_maps: bool = True,
    build_mesh: bool = True,
    build_texture: bool = True,
    build_dem: bool = False,
    build_orthomosaic: bool = True,
    build_point_cloud: bool = True,
    export_obj: bool = True,
    export_glb: bool = False,
    export_fbx: bool = False,
    export_ply: bool = False,
    export_dxf: bool = False,
    export_dem: bool = False,
    export_ortho: bool = True,
    export_point_cloud: bool = False,
    point_cloud_format: str = "laz",
    export_report: bool = False,
    resume: bool = True,
    network_path: str = "",
    status_file: str | None = None,
) -> tuple:
    """Constructs an atomic monolithic Metashape.Tasks.RunScript NetworkTask."""
    if not status_file:
        status_file = os.path.join(output_dir, "_autometashape_status.json")

    # Resolve runner script and source package
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    current_script = os.path.join(repo_root, "scripts", "metashape_reconstruction.py")
    current_source = os.path.join(repo_root, "source")
    shared_script = None
    norm_net = os.path.normpath(network_path.strip()) if network_path and network_path.strip() else ""
    if norm_net and os.path.isdir(norm_net):
        shared_autometashape = os.path.join(norm_net, ".autometashape")
        shared_scripts_dir = os.path.join(shared_autometashape, "scripts")
        shared_source_dir = os.path.join(shared_autometashape, "source")
        try:
            os.makedirs(shared_scripts_dir, exist_ok=True)
            shared_script = os.path.join(shared_scripts_dir, "metashape_reconstruction.py")
            if os.path.exists(current_script):
                shutil.copy2(current_script, shared_script)
            if os.path.isdir(current_source):
                shutil.copytree(current_source, shared_source_dir, dirs_exist_ok=True)
        except Exception:
            shared_script = None

    if shared_script and os.path.exists(shared_script):
        try:
            rel_script = os.path.relpath(shared_script, norm_net).replace("\\", "/")
        except Exception:
            rel_script = shared_script.replace("\\", "/")
    else:
        if norm_net and os.path.abspath(current_script).startswith(os.path.abspath(norm_net)):
            rel_script = os.path.relpath(current_script, norm_net).replace("\\", "/")
        else:
            rel_script = current_script.replace("\\", "/")

    if norm_net and os.path.abspath(images_dir).startswith(os.path.abspath(norm_net)):
        rel_images = os.path.relpath(images_dir, norm_net).replace("\\", "/")
    else:
        rel_images = images_dir.replace("\\", "/")

    if norm_net and os.path.abspath(output_dir).startswith(os.path.abspath(norm_net)):
        rel_output = os.path.relpath(output_dir, norm_net).replace("\\", "/")
    else:
        rel_output = output_dir.replace("\\", "/")

    if norm_net and os.path.abspath(status_file).startswith(os.path.abspath(norm_net)):
        rel_status = os.path.relpath(status_file, norm_net).replace("\\", "/")
    else:
        rel_status = status_file.replace("\\", "/")

    worker_args = [
        "--images",
        rel_images,
        "--output",
        rel_output,
        "--project-name",
        project_name,
        "--preset",
        preset,
        "--align-quality",
        align_quality,
        "--depth-quality",
        depth_quality,
        "--depth-filtering",
        depth_filtering,
        "--faces",
        str(custom_faces or 2000000),
        "--texture-pages",
        str(texture_pages or 16),
        "--crop-method",
        crop_method or "none",
        "--texture-format",
        texture_format,
        "--status-file",
        rel_status,
    ]
    if network_path:
        worker_args.extend(["--network-path", network_path])
    if resume:
        worker_args.append("--resume")
    else:
        worker_args.append("--no-resume")

    # Independent worker monolithic task unconditionally executes via offline local scratch staging
    worker_args.append("--local-scratch-staging")

    if not align_photos:
        worker_args.append("--no-align-photos")
    if not build_depth_maps:
        worker_args.append("--no-build-depth-maps")
    if not build_mesh:
        worker_args.append("--no-build-mesh")
    if not build_texture:
        worker_args.append("--no-build-texture")
    if not build_dem:
        worker_args.append("--no-build-dem")
    if not build_orthomosaic:
        worker_args.append("--no-build-orthomosaic")
    if not build_point_cloud:
        worker_args.append("--no-build-point-cloud")

    if export_obj:
        worker_args.append("--export-obj")
    if export_glb:
        worker_args.append("--export-glb")
    if export_fbx:
        worker_args.append("--export-fbx")
    if export_ply:
        worker_args.append("--export-ply")
    if export_dxf:
        worker_args.append("--export-dxf")
    if export_dem:
        worker_args.append("--export-dem")
    if export_ortho:
        worker_args.append("--export-ortho")
    if export_point_cloud:
        worker_args.append("--export-point-cloud")
        worker_args.extend(["--point-cloud-format", point_cloud_format])
    if export_report:
        worker_args.append("--export-report")

    cmd_args = subprocess.list2cmdline(worker_args)
    if Metashape is not None:
        rs = Metashape.Tasks.RunScript()
        rs.path = rel_script
        rs.args = cmd_args
        net_task = rs.toNetworkTask()
        try:
            net_task.gpu_support = True
        except Exception:
            pass
        if not isinstance(getattr(net_task, "name", None), str):
            net_task.name = "RunScript"
    else:
        from unittest.mock import MagicMock

        net_task = MagicMock()
        net_task.name = "RunScript"
        net_task.gpu_support = True
        net_task.args = cmd_args

    return net_task, rel_script, status_file


def build_unified_cluster_pipeline_tasks(
    chunk,
    output_dir: str,
    project_name: str,
    align_photos: bool = True,
    align_quality: str = "high",
    reference_preselection_mode: str = "estimated",
    keypoint_limit: int = 200000,
    keypoint_limit_per_mpx: int = 15000,
    tiepoint_limit: int = 25000,
    filter_stationary_points: bool = True,
    guided_matching: bool = True,
    adaptive_fitting: bool = True,
    filter_tiepoints: bool = True,
    crop_method: str = "gps-square",
    crop_padding: float = 0.05,
    build_depth_maps: bool = True,
    depth_quality: str = "high",
    depth_filtering: str = "moderate",
    build_point_cloud: bool = True,
    points_spacing: float = 0.02,
    build_mesh: bool = True,
    surface_type: str = "arbitrary",
    custom_faces: int = None,
    build_texture: bool = True,
    texture_pages: int = None,
    fill_holes: bool = True,
    ghosting_filter: bool = True,
    build_dem: bool = False,
    build_orthomosaic: bool = False,
    ortho_resolution: float = 0.02,
    ortho_cull_faces: bool = True,
    build_tiled_model: bool = False,
    tile_size: int = 256,
    tiled_model_format: str = "cesium",
    export_glb: bool = False,
    export_obj: bool = False,
    export_fbx: bool = False,
    export_ply: bool = False,
    export_dxf: bool = False,
    export_tie_points: bool = False,
    export_dem: bool = False,
    export_ortho: bool = False,
    export_point_cloud: bool = False,
    point_cloud_format: str = "laz",
    export_report: bool = False,
    report_title: str = None,
    texture_format: str = "png",
    network_path: str = "",
    export_path_fn=None,
) -> tuple[list, list[tuple[str, str, str]]]:
    """Assembles the complete sequence of photogrammetry pipeline tasks into a single unified batch."""
    if Metashape is None:
        return [], []

    tasks = []
    stage_info = []

    def get_exp_path(f: str) -> str:
        if export_path_fn:
            return export_path_fn(f)
        return os.path.abspath(f).replace("\\", "/")

    # 1. Feature Matching & Camera Alignment
    if align_photos:
        align_downscale = ALIGN_DOWNSCALE_MAP.get((align_quality or "high").lower(), 1)
        presel_mode_enum = PRESEL_MODE_MAP.get(
            (reference_preselection_mode or "estimated").lower(),
            Metashape.ReferencePreselectionMode.ReferencePreselectionEstimated,
        )

        t_match = Metashape.Tasks.MatchPhotos()
        t_match.downscale = align_downscale
        t_match.generic_preselection = True
        t_match.reference_preselection = True
        t_match.reference_preselection_mode = presel_mode_enum
        t_match.keypoint_limit = keypoint_limit or 200000
        t_match.keypoint_limit_per_mpx = keypoint_limit_per_mpx or 15000
        t_match.tiepoint_limit = tiepoint_limit or 25000
        t_match.mask_tiepoints = False
        t_match.filter_stationary_points = filter_stationary_points
        t_match.guided_matching = guided_matching
        tasks.append(t_match)
        stage_info.append(("MatchPhotos", "match", "GPU Feature Matching"))

        t_align = Metashape.Tasks.AlignCameras()
        t_align.adaptive_fitting = adaptive_fitting
        tasks.append(t_align)
        stage_info.append(("AlignCameras", "align", "Camera Alignment & Bundle Adjustment"))

        if filter_tiepoints:
            clean_tasks = build_cluster_gradual_selection_tasks()
            for ct in clean_tasks:
                tasks.append(ct)
                t_type = "CleanTiePoints" if hasattr(ct, "criterion") else "OptimizeCameras"
                stage_info.append((t_type, "align", "Gradual Selection Tie-Point Cleaning & Camera Optimization"))

    # Auto-Crop Bounding Box
    if align_photos and crop_method in ("gps-square", "structure"):
        t_crop = build_cluster_crop_task(crop_method, padding=crop_padding)
        if t_crop:
            tasks.append(t_crop)
            stage_info.append(("RunScript", "align", f"Auto-Crop Bounding Box ({crop_method})"))

    # 2. Multi-View Stereo Depth Maps
    if build_depth_maps:
        depth_downscale = DEPTH_DOWNSCALE_MAP.get((depth_quality or "high").lower(), 2)
        filter_mode_arg = DEPTH_FILTER_MAP.get(
            (depth_filtering or "moderate").lower(), Metashape.FilterMode.ModerateFiltering
        )
        t_d = Metashape.Tasks.BuildDepthMaps()
        t_d.downscale = depth_downscale
        t_d.filter_mode = filter_mode_arg
        t_d.reuse_depth = False
        tasks.append(t_d)
        stage_info.append(("BuildDepthMaps", "depth", f"GPU MVS Depth Maps ({depth_quality.capitalize()})"))

    # 3. Dense Point Cloud
    if build_point_cloud:
        t_p = Metashape.Tasks.BuildPointCloud()
        t_p.source_data = Metashape.DataSource.DepthMapsData
        t_p.point_colors = True
        t_p.point_confidence = True
        t_p.uniform_sampling = True
        t_p.points_spacing = points_spacing or 0.02
        t_p.keep_depth = True
        tasks.append(t_p)
        stage_info.append(("BuildPointCloud", "point_cloud", "Generating Dense Point Cloud"))

    # 4. Surface Mesh Model
    if build_mesh:
        surf_enum = SURFACE_TYPE_MAP.get((surface_type or "arbitrary").lower(), Metashape.SurfaceType.Arbitrary)
        if custom_faces and custom_faces > 0:
            face_count_arg = Metashape.FaceCount.CustomFaceCount
            face_count_custom_arg = custom_faces
        else:
            face_count_arg = Metashape.FaceCount.MediumFaceCount
            face_count_custom_arg = 200000

        t_m = Metashape.Tasks.BuildModel()
        t_m.surface_type = surf_enum
        t_m.source_data = Metashape.DataSource.DepthMapsData
        t_m.interpolation = Metashape.Interpolation.EnabledInterpolation
        t_m.face_count = face_count_arg
        t_m.face_count_custom = face_count_custom_arg
        t_m.vertex_colors = True
        t_m.vertex_confidence = True
        t_m.replace_asset = True
        tasks.append(t_m)
        stage_info.append(("BuildModel", "mesh", f"Surface Meshing ({face_count_custom_arg:,} faces)"))

    # 5. Texture Atlas Parameterization & Mosaic Blending
    if build_texture:
        eff_pages = texture_pages or 16

        t_uv = Metashape.Tasks.BuildUV()
        t_uv.mapping_mode = Metashape.MappingMode.GenericMapping
        t_uv.page_count = eff_pages
        t_uv.texture_size = 8192
        tasks.append(t_uv)
        stage_info.append(("BuildUV", "texture", f"Texture UV Parameterization ({eff_pages} pages)"))

        t_tex = Metashape.Tasks.BuildTexture()
        t_tex.blending_mode = Metashape.BlendingMode.MosaicBlending
        t_tex.texture_size = 8192
        t_tex.fill_holes = fill_holes
        t_tex.ghosting_filter = ghosting_filter
        tasks.append(t_tex)
        stage_info.append(("BuildTexture", "texture", f"Baking {eff_pages}x 8K Texture Atlas"))

    # 6. LOD 3D Tiled Model
    if build_tiled_model:
        t_tl = Metashape.Tasks.BuildTiledModel()
        t_tl.tile_size = tile_size or 256
        t_tl.source_data = Metashape.DataSource.DepthMapsData
        t_tl.replace_asset = True
        tasks.append(t_tl)
        stage_info.append(("BuildTiledModel", "tiled_model", f"Building 3D Tiled Model ({tile_size}px tiles)"))

        tiled_fmt_tuple = TILED_MODEL_FORMAT_MAP.get(
            (tiled_model_format or "cesium").lower(),
            (Metashape.TiledModelFormat.TiledModelFormatCesium, "tileset.json"),
        )
        tiled_fmt, tiled_ext = tiled_fmt_tuple
        if (tiled_model_format or "cesium").lower() == "cesium":
            tiled_dir = os.path.join(output_dir, f"{project_name}_tiled_model")
            tiled_out = os.path.join(tiled_dir, "tileset.json")
        else:
            tiled_out = os.path.join(output_dir, f"{project_name}_tiled_model{tiled_ext}")
        t_etl = Metashape.Tasks.ExportTiledModel()
        t_etl.format = tiled_fmt
        t_etl.path = get_exp_path(tiled_out)
        tasks.append(t_etl)
        stage_info.append(
            ("ExportTiledModel", "export", f"Exporting 3D Tiled Model ({(tiled_model_format or 'cesium').upper()})")
        )

    # Geodesy / Projection Setup for DEM & Ortho
    proj = Metashape.OrthoProjection()
    target_crs = chunk.crs if chunk else None
    if chunk and chunk.crs and not getattr(chunk.crs, "projcs", None):
        gps_cams = [
            c
            for c in chunk.cameras
            if c.reference
            and c.reference.location
            and (abs(c.reference.location.x) > 0.001 or abs(c.reference.location.y) > 0.001)
        ]
        if gps_cams:
            avg_lon = sum(c.reference.location.x for c in gps_cams) / len(gps_cams)
            avg_lat = sum(c.reference.location.y for c in gps_cams) / len(gps_cams)
            utm_epsg = _compute_utm_epsg(avg_lon, avg_lat)
            try:
                target_crs = Metashape.CoordinateSystem(f"EPSG::{utm_epsg}")
            except Exception:
                target_crs = chunk.crs
    if target_crs:
        try:
            proj.crs = target_crs
        except Exception:
            pass

    # 7. Digital Elevation Model (DEM)
    if build_dem:
        dem_source = Metashape.DataSource.PointCloudData if build_point_cloud else Metashape.DataSource.DepthMapsData
        t_dm = Metashape.Tasks.BuildDem()
        t_dm.source_data = dem_source
        t_dm.interpolation = Metashape.Interpolation.EnabledInterpolation
        t_dm.replace_asset = True
        if target_crs:
            t_dm.projection = proj
        tasks.append(t_dm)
        stage_info.append(("BuildDem", "dem", "Building Digital Elevation Model (DEM/DSM)"))

    # 8. True Orthomosaic
    if build_orthomosaic:
        t_om = Metashape.Tasks.BuildOrthomosaic()
        t_om.surface_data = Metashape.DataSource.ElevationData if build_dem else Metashape.DataSource.ModelData
        t_om.replace_asset = True
        t_om.blending_mode = Metashape.BlendingMode.MosaicBlending
        t_om.fill_holes = fill_holes
        t_om.ghosting_filter = ghosting_filter
        t_om.cull_faces = ortho_cull_faces
        t_om.projection = proj
        if getattr(proj.crs, "projcs", None) and ortho_resolution and ortho_resolution > 0:
            t_om.resolution = ortho_resolution
        tasks.append(t_om)
        stage_info.append(("BuildOrthomosaic", "ortho", "Generating Orthomosaic"))

    # 9. Deliverables Export
    glb_out = os.path.join(output_dir, f"{project_name}.glb")
    obj_out = os.path.join(output_dir, f"{project_name}.obj")
    fbx_out = os.path.join(output_dir, f"{project_name}.fbx")
    ply_out = os.path.join(output_dir, f"{project_name}_mesh.ply")
    dxf_out = os.path.join(output_dir, f"{project_name}.dxf")
    dem_out = os.path.join(output_dir, f"{project_name}_dem.tif")
    ortho_out = os.path.join(output_dir, f"{project_name}_orthomosaic.tif")
    rep_out = os.path.join(output_dir, f"{project_name}_report.pdf")

    tex_fmt = texture_format if texture_format else "png"
    fmt_lower = tex_fmt.lower()
    if fmt_lower in ("tiff", "tif"):
        tex_image_fmt = Metashape.ImageFormat.ImageFormatTIFF
    elif fmt_lower in ("jpg", "jpeg"):
        tex_image_fmt = Metashape.ImageFormat.ImageFormatJPEG
    else:
        tex_image_fmt = Metashape.ImageFormat.ImageFormatPNG

    if export_obj and build_mesh:
        t_obj = Metashape.Tasks.ExportModel()
        t_obj.format = Metashape.ModelFormat.ModelFormatOBJ
        t_obj.texture_format = tex_image_fmt
        t_obj.save_texture = True
        t_obj.save_normals = True
        t_obj.save_colors = True
        t_obj.path = get_exp_path(obj_out)
        tasks.append(t_obj)
        stage_info.append(("ExportModel", "export", f"Exporting Wavefront OBJ + {fmt_lower.upper()} Atlas"))

    if export_glb and build_mesh:
        t_glb = Metashape.Tasks.ExportModel()
        t_glb.format = Metashape.ModelFormat.ModelFormatGLTF
        t_glb.embed_texture = True
        t_glb.save_normals = True
        t_glb.save_colors = True
        t_glb.path = get_exp_path(glb_out)
        tasks.append(t_glb)
        stage_info.append(("ExportModel", "export", "Exporting Binary glTF (.glb)"))

    if export_fbx and build_mesh:
        t_fbx = Metashape.Tasks.ExportModel()
        t_fbx.format = Metashape.ModelFormat.ModelFormatFBX
        t_fbx.save_texture = True
        t_fbx.save_normals = True
        t_fbx.save_colors = True
        t_fbx.path = get_exp_path(fbx_out)
        tasks.append(t_fbx)
        stage_info.append(("ExportModel", "export", "Exporting Autodesk FBX"))

    if export_ply and build_mesh:
        t_ply = Metashape.Tasks.ExportModel()
        t_ply.format = Metashape.ModelFormat.ModelFormatPLY
        t_ply.save_normals = True
        t_ply.save_colors = True
        t_ply.path = get_exp_path(ply_out)
        tasks.append(t_ply)
        stage_info.append(("ExportModel", "export", "Exporting Stanford PLY Mesh"))

    if export_dxf and build_mesh:
        t_dxf = Metashape.Tasks.ExportModel()
        t_dxf.format = Metashape.ModelFormat.ModelFormatDXF
        t_dxf.save_normals = True
        t_dxf.path = get_exp_path(dxf_out)
        tasks.append(t_dxf)
        stage_info.append(("ExportModel", "export", "Exporting AutoCAD DXF Mesh"))

    if export_tie_points:
        tp_out = os.path.join(output_dir, f"{project_name}_tie_points.ply")
        t_tp = Metashape.Tasks.ExportPointCloud()
        t_tp.format = Metashape.PointCloudFormat.PointCloudFormatPLY
        t_tp.source_data = Metashape.DataSource.TiePointsData
        t_tp.path = get_exp_path(tp_out)
        tasks.append(t_tp)
        stage_info.append(("ExportPointCloud", "export", "Exporting Sparse Tie Points (PLY)"))

    if export_dem and build_dem:
        t_dem = Metashape.Tasks.ExportRaster()
        t_dem.source_data = Metashape.DataSource.ElevationData
        t_dem.image_format = Metashape.ImageFormat.ImageFormatTIFF
        comp = Metashape.ImageCompression()
        comp.tiff_compression = Metashape.ImageCompression.TiffCompression.TiffCompressionLZW
        comp.tiff_big = True
        t_dem.image_compression = comp
        t_dem.save_world = True
        t_dem.path = get_exp_path(dem_out)
        tasks.append(t_dem)
        stage_info.append(("ExportRaster", "export", "Exporting GeoTIFF Elevation Model (DEM)"))

    if export_ortho and build_orthomosaic:
        t_ortho = Metashape.Tasks.ExportRaster()
        t_ortho.source_data = Metashape.DataSource.OrthomosaicData
        t_ortho.image_format = Metashape.ImageFormat.ImageFormatTIFF
        comp = Metashape.ImageCompression()
        comp.tiff_compression = Metashape.ImageCompression.TiffCompression.TiffCompressionLZW
        comp.tiff_big = True
        t_ortho.image_compression = comp
        t_ortho.save_world = True
        t_ortho.save_alpha = True
        t_ortho.path = get_exp_path(ortho_out)
        tasks.append(t_ortho)
        stage_info.append(("ExportRaster", "export", "Exporting GeoTIFF Orthomosaic"))

    if export_point_cloud and build_point_cloud:
        pc_format_ext = point_cloud_format.lower() if point_cloud_format else "laz"
        pc_fmt, pc_ext = POINT_CLOUD_FORMAT_MAP.get(
            pc_format_ext,
            (Metashape.PointCloudFormat.PointCloudFormatLAZ, ".laz") if Metashape is not None else (None, ".laz"),
        )
        pc_out = os.path.join(output_dir, f"{project_name}_point_cloud{pc_ext}")
        t_pc = Metashape.Tasks.ExportPointCloud()
        t_pc.format = pc_fmt
        t_pc.source_data = Metashape.DataSource.PointCloudData
        t_pc.save_point_color = True
        t_pc.save_point_normal = True
        t_pc.save_point_confidence = True
        t_pc.save_point_classification = True
        t_pc.path = get_exp_path(pc_out)
        tasks.append(t_pc)
        stage_info.append(("ExportPointCloud", "export", f"Exporting Dense Point Cloud ({pc_format_ext.upper()})"))

    if export_report:
        t_rep = Metashape.Tasks.ExportReport()
        t_rep.title = report_title or "AutoMetashape Processing & Calibration Report"
        t_rep.path = get_exp_path(rep_out)
        tasks.append(t_rep)
        stage_info.append(("ExportReport", "export", "Exporting Calibration & Quality PDF Report"))

    return tasks, stage_info


def safe_create_cluster_batch(
    net_client: Metashape.NetworkClient,
    rel_psx: str,
    net_tasks: list,
    worker_limit: int | None = 1,
    force_fresh: bool = True,
) -> tuple[int, bool]:
    """Safely creates a cluster batch, resolving 'Duplicate batch' conflicts on the Network Server."""
    if net_client is None:
        raise ValueError("NetworkClient is not connected or None")

    norm_target = _norm_cluster_path(rel_psx)

    if hasattr(net_client, "findBatch"):
        try:
            fb_id = net_client.findBatch(rel_psx)
            if fb_id is not None and fb_id > 0:
                fb_info = net_client.batchInfo(fb_id)
                fb_state = (
                    fb_info.get("state", "") if isinstance(fb_info, dict) else getattr(fb_info, "state", "")
                ).lower()
                if not force_fresh and fb_state in ("inprogress", "running", "submitted", "pending", "queued"):
                    print(
                        f"  [Cluster] Attaching to active batch #{fb_id} via findBatch for '{rel_psx}' ({fb_state}).",
                        flush=True,
                    )
                    return fb_id, True
                else:
                    try:
                        net_client.abortBatch(fb_id)
                        time.sleep(0.5)
                    except Exception:
                        pass
        except Exception:
            pass

    try:
        raw_b = net_client.batchList()
        batches = (
            raw_b.get("batches", []) if isinstance(raw_b, dict) else (raw_b if isinstance(raw_b, (list, tuple)) else [])
        )
        for b in batches:
            b_path = b.get("path") if isinstance(b, dict) else getattr(b, "path", None)
            if _norm_cluster_path(b_path) == norm_target:
                b_id = b.get("batch_id") if isinstance(b, dict) else getattr(b, "id", getattr(b, "batch_id", None))
                b_state = (b.get("state") if isinstance(b, dict) else getattr(b, "state", "")).lower()
                if not force_fresh and b_state in ("inprogress", "running", "submitted", "pending", "queued"):
                    print(f"  [Cluster] Attaching to active batch #{b_id} for '{rel_psx}' ({b_state}).", flush=True)
                    return b_id, True
                else:
                    try:
                        net_client.abortBatch(b_id)
                        time.sleep(0.5)
                    except Exception:
                        pass
    except Exception as e_pre:
        print(f"  [Cluster] Note: Pre-dispatch batch check: {e_pre}", flush=True)

    try:
        batch_id = net_client.createBatch(rel_psx, net_tasks)
    except Exception as e_create:
        if "duplicate batch" in str(e_create).lower():
            try:
                aborted_ids = set()
                raw_b = net_client.batchList()
                batches = (
                    raw_b.get("batches", [])
                    if isinstance(raw_b, dict)
                    else (raw_b if isinstance(raw_b, (list, tuple)) else [])
                )
                for b in batches:
                    b_path = b.get("path") if isinstance(b, dict) else getattr(b, "path", None)
                    if _norm_cluster_path(b_path) == norm_target:
                        b_id = b.get("batch_id") if isinstance(b, dict) else getattr(b, "id", None)
                        if b_id is not None:
                            try:
                                net_client.abortBatch(b_id)
                                aborted_ids.add(b_id)
                            except Exception:
                                pass
                if not aborted_ids and hasattr(net_client, "findBatch"):
                    try:
                        fb_id = net_client.findBatch(rel_psx)
                        if fb_id is not None and fb_id not in aborted_ids:
                            net_client.abortBatch(fb_id)
                    except Exception:
                        pass
                time.sleep(1.0)
                batch_id = net_client.createBatch(rel_psx, net_tasks)
            except Exception as e_retry:
                raise e_retry
        else:
            raise e_create

    if worker_limit is not None and worker_limit > 0:
        try:
            net_client.setBatchWorkerLimit(batch_id, worker_limit)
        except Exception:
            pass
    try:
        net_client.setBatchPaused(batch_id, False)
    except Exception:
        pass

    return batch_id, False


def evaluate_cluster_batch_info(b_info: dict) -> tuple[str, bool, str]:
    """Evaluates Metashape batchInfo dict and determines status, success flag, and error message.

    Metashape NetworkClient.batchInfo() can report state='completed' even when the batch
    was aborted or encountered errors (e.g. workitems failed). This function inspects
    all failure signals (failed, aborted, error_count, task errors) to prevent false-positive
    completion traps.

    Args:
        b_info: Dictionary returned by Metashape.NetworkClient.batchInfo(batch_id).

    Returns:
        tuple (status_str, is_success, error_description):
            status_str: 'completed', 'failed', 'aborted', or 'inprogress'
            is_success: True if genuinely completed with 0 errors, False otherwise
            error_description: Consolidated error message if failed/aborted, else empty string
    """
    if not isinstance(b_info, dict):
        return "failed", False, "Invalid batchInfo structure received from cluster server"

    state = str(b_info.get("state") or "").lower()
    tasks_info = b_info.get("tasks", [])

    has_failed = bool(
        state in ("failed", "aborted")
        or b_info.get("failed")
        or b_info.get("aborted")
        or b_info.get("error_count", 0) > 0
        or any(t.get("state") in ("failed", "aborted") or t.get("failed") or t.get("aborted") for t in tasks_info)
    )

    if has_failed:
        err_msgs = []
        last_err = b_info.get("last_error")
        if isinstance(last_err, dict) and last_err.get("message"):
            err_msgs.append(str(last_err["message"]))
        for t in tasks_info:
            for err in t.get("errors", []):
                err_msgs.append(str(err))
        resolved_state = "aborted" if (state == "aborted" or b_info.get("aborted")) else "failed"
        err_str = "; ".join(err_msgs) if err_msgs else f"Batch {resolved_state} on cluster node"
        return resolved_state, False, err_str

    if state == "completed":
        return "completed", True, ""

    return state or "inprogress", False, ""
