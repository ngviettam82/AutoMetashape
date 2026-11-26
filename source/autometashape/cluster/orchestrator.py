# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Distributed cluster batch orchestrator, monolithic runscript bundling, and failover management."""

from __future__ import annotations

import json
import os
import subprocess
import time
import zipfile

try:
    import Metashape
except ImportError:
    Metashape = None


from autometashape.cluster.dual_share import normalize_share_path, relpath_under

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


def _compute_utm_epsg(lon: float, lat: float) -> int:
    """Calculates standard WGS 84 / UTM Zone EPSG code via core telemetry module."""
    from autometashape.core.telemetry import compute_utm_epsg

    return compute_utm_epsg(lon, lat)


compute_utm_epsg = _compute_utm_epsg

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
    o3.fit_f = o3.fit_cx = o3.fit_cy = True
    o3.fit_b1 = o3.fit_b2 = False
    o3.fit_k1 = o3.fit_k2 = o3.fit_k3 = True
    o3.fit_k4 = False
    o3.fit_p1 = o3.fit_p2 = True
    o3.adaptive_fitting = False
    o3.tiepoint_covariance = True
    tasks.extend([t3, o3])

    # Pass 4: Multi-ray Multiplicity
    t4 = Metashape.Tasks.CleanTiePoints()
    t4.criterion = Metashape.TiePoints.Criterion.ImageCount
    t4.threshold = max(1, min_rays - 1)
    o4 = Metashape.Tasks.OptimizeCameras()
    o4.fit_f = o4.fit_cx = o4.fit_cy = True
    o4.fit_b1 = o4.fit_b2 = False
    o4.fit_k1 = o4.fit_k2 = o4.fit_k3 = True
    o4.fit_k4 = False
    o4.fit_p1 = o4.fit_p2 = True
    o4.adaptive_fitting = False
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
            "        min_z, max_z = min(all_zs), max(all_zs)\n"
            "        span_z = max(chunk.region.size.z, (max_z - min_z) * (1.0 + 2.0 * padding))\n"
            "        mid_z = (min_z + max_z) * 0.5\n"
            "    else:\n"
            "        orig_mid_z = (rot_t * chunk.region.center).z\n"
            "        orig_span_z = chunk.region.size.z\n"
            "        all_zs = inlier_zs + [orig_mid_z - 0.5 * orig_span_z, orig_mid_z + 0.5 * orig_span_z]\n"
            "        flight_height = max(0.0, abs((sorted(inlier_zs)[len(inlier_zs) // 2]) - orig_mid_z))\n"
            "        min_z, max_z = min(all_zs), max(all_zs)\n"
            "        span_z = max(orig_span_z, (max_z - min_z) * (1.0 + 2.0 * padding))\n"
            "        mid_z = (min_z + max_z) * 0.5\n"
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
    keypoint_limit: int | None = None,
    keypoint_limit_per_mpx: int | None = None,
    tiepoint_limit: int | None = None,
    depth_quality: str = "high",
    depth_filtering: str | None = "moderate",
    custom_faces: int | None = 2000000,
    texture_pages: int | None = 16,
    crop_method: str | None = "gps-square",
    texture_format: str = "png",
    ortho_resolution: float = 0.05,
    align_photos: bool = True,
    build_depth_maps: bool = True,
    build_mesh: bool = True,
    build_texture: bool = True,
    build_dem: bool = False,
    build_orthomosaic: bool = True,
    build_point_cloud: bool = False,
    export_obj: bool = True,
    export_glb: bool = False,
    export_fbx: bool = False,
    export_ply: bool = False,
    export_dxf: bool = False,
    export_dem: bool = False,
    export_ortho: bool = True,
    export_point_cloud: bool = False,
    point_cloud_format: str = "laz",
    export_tie_points: bool = False,
    export_report: bool = False,
    resume: bool = True,
    guided_matching: bool = True,
    local_scratch: bool = True,
    network_path: str = "",
    status_file: str | None = None,
    surface_type: str | None = None,
    rolling_shutter: bool | None = None,
    filter_tiepoints: bool | None = None,
    build_contours: bool = False,
    contour_interval: float = 1.0,
    contours_format: str = "shp",
    build_tiled_model: bool = False,
    tiled_model_format: str = "cesium",
    tile_size: int = 256,
    classify_ground: bool | None = None,
    classify_points: bool = False,
    remove_lighting: bool = False,
    delighting_color_mode: bool = False,
    calibrate_colors: bool = False,
    detect_markers: bool = False,
    target_type: str = "circular12",
    marker_tolerance: int = 50,
    import_reference: str | None = None,
    smooth_mesh: bool = False,
    smooth_strength: float = 3.0,
    decimate_mesh: int | None = None,
    exclude_folders: list[str] | tuple[str, ...] | str | None = None,
    alignment_hygiene: bool | None = None,
    icp_bridge: bool | None = None,
    adaptive_fitting: bool | None = None,
    filter_stationary_points: bool | None = None,
    reference_preselection_mode: str | None = None,
    min_quality: float | None = None,
    disable_quality_filter: bool = False,
    crop_padding: float | None = None,
    close_holes: bool | None = None,
    fill_holes: bool | None = None,
    ghosting_filter: bool | None = None,
    rebake_texture: bool = False,
    points_spacing: float | None = None,
    filter_point_confidence: int | None = None,
    ortho_cull_faces: bool | None = None,
    report_title: str | None = None,
    scratch_dir: str | None = None,
    min_scratch_free_gb: float | None = None,
) -> tuple:
    """Constructs an atomic monolithic Metashape.Tasks.RunScript NetworkTask."""
    if not status_file:
        status_file = os.path.join(output_dir, "_autometashape_status.json")

    # Standard relative entry point on worker local repository
    rel_script = "scripts/metashape_reconstruction.py"
    norm_net = normalize_share_path(network_path) if network_path and network_path.strip() else ""

    rel_images = (
        relpath_under(images_dir, norm_net, case_insensitive=True) if norm_net else None
    ) or images_dir.replace("\\", "/")
    rel_output = (
        relpath_under(output_dir, norm_net, case_insensitive=True) if norm_net else None
    ) or output_dir.replace("\\", "/")
    rel_status = (
        relpath_under(status_file, norm_net, case_insensitive=True) if norm_net else None
    ) or status_file.replace("\\", "/")

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
        "--texture-format",
        texture_format,
        "--ortho-resolution",
        str(ortho_resolution if ortho_resolution is not None else 0.05),
        "--status-file",
        rel_status,
    ]
    if depth_filtering is not None:
        worker_args.extend(["--depth-filtering", depth_filtering])
    if custom_faces is not None:
        worker_args.extend(["--faces", str(custom_faces)])
    if texture_pages is not None:
        worker_args.extend(["--texture-pages", str(texture_pages)])
    if crop_method is not None:
        worker_args.extend(["--crop-method", crop_method])
    if keypoint_limit is not None:
        worker_args.extend(["--keypoint-limit", str(keypoint_limit)])
    if keypoint_limit_per_mpx is not None:
        worker_args.extend(["--keypoint-limit-per-mpx", str(keypoint_limit_per_mpx)])
    if tiepoint_limit is not None:
        worker_args.extend(["--tiepoint-limit", str(tiepoint_limit)])
    if surface_type:
        worker_args.extend(["--surface-type", surface_type])
    if rolling_shutter is True:
        worker_args.append("--rolling-shutter")
    elif rolling_shutter is False:
        worker_args.append("--no-rolling-shutter")
    if filter_tiepoints is False:
        worker_args.append("--no-filter-tiepoints")
    if alignment_hygiene is False:
        worker_args.append("--no-alignment-hygiene")
    if icp_bridge is False:
        worker_args.append("--no-icp-bridge")
    if adaptive_fitting is True:
        worker_args.append("--adaptive-fitting")
    elif adaptive_fitting is False:
        worker_args.append("--no-adaptive-fitting")
    if filter_stationary_points is False:
        worker_args.append("--no-filter-stationary")
    if reference_preselection_mode:
        worker_args.extend(["--reference-preselection-mode", reference_preselection_mode])
    if min_quality is not None:
        worker_args.extend(["--min-quality", str(min_quality)])
    if disable_quality_filter:
        worker_args.append("--no-quality-filter")
    if crop_padding is not None:
        worker_args.extend(["--crop-padding", str(crop_padding)])
    if close_holes is True:
        worker_args.append("--close-holes")
    elif close_holes is False:
        worker_args.append("--no-close-holes")
    if fill_holes is False:
        worker_args.append("--no-fill-holes")
    if ghosting_filter is False:
        worker_args.append("--no-ghosting-filter")
    if rebake_texture:
        worker_args.append("--rebake-texture")
    if points_spacing is not None:
        worker_args.extend(["--points-spacing", str(points_spacing)])
    if (
        isinstance(filter_point_confidence, int)
        and not isinstance(filter_point_confidence, bool)
        and filter_point_confidence > 0
    ):
        worker_args.extend(["--filter-point-confidence", str(filter_point_confidence)])
    if ortho_cull_faces is False:
        worker_args.append("--no-ortho-cull-faces")
    if report_title:
        worker_args.extend(["--report-title", report_title])
    if scratch_dir:
        worker_args.extend(["--scratch-dir", scratch_dir])
    if min_scratch_free_gb is not None:
        worker_args.extend(["--min-scratch-free-gb", str(min_scratch_free_gb)])
    if network_path:
        worker_args.extend(["--network-path", network_path])
    if resume:
        worker_args.append("--resume")
    else:
        worker_args.append("--no-resume")

    if local_scratch:
        worker_args.append("--local-scratch-staging")
    else:
        worker_args.append("--no-local-scratch-staging")

    if not align_photos:
        worker_args.append("--no-align-photos")
    if not build_depth_maps:
        worker_args.append("--no-build-depth-maps")
    if not build_mesh:
        worker_args.append("--no-build-mesh")
    if not build_texture:
        worker_args.append("--no-build-texture")
    if build_dem:
        worker_args.append("--build-dem")
    else:
        worker_args.append("--no-dem")
    if build_orthomosaic:
        worker_args.append("--ortho")
    else:
        worker_args.append("--no-ortho")
    if build_point_cloud:
        worker_args.append("--build-point-cloud")
    else:
        worker_args.append("--no-point-cloud")
    if not guided_matching:
        worker_args.append("--no-guided-matching")

    if build_contours:
        worker_args.extend(
            [
                "--build-contours",
                "--contour-interval",
                str(contour_interval),
                "--contours-format",
                contours_format,
            ]
        )
    if build_tiled_model:
        worker_args.extend(
            [
                "--build-tiled-model",
                "--tiled-model-format",
                tiled_model_format,
                "--tile-size",
                str(tile_size),
            ]
        )
    if classify_ground:
        worker_args.append("--classify-ground")
    if classify_points:
        worker_args.append("--classify-points")
    if remove_lighting:
        worker_args.append("--remove-lighting")
        if delighting_color_mode:
            worker_args.append("--delighting-color-mode")
    if calibrate_colors:
        worker_args.append("--calibrate-colors")
    if detect_markers:
        worker_args.extend(
            [
                "--detect-markers",
                "--target-type",
                target_type,
                "--marker-tolerance",
                str(marker_tolerance),
            ]
        )
    if import_reference:
        rel_ref = (
            relpath_under(import_reference, norm_net, case_insensitive=True) if norm_net else None
        ) or import_reference.replace("\\", "/")
        worker_args.extend(["--import-reference", rel_ref])
    if smooth_mesh:
        worker_args.extend(["--smooth-mesh", "--smooth-strength", str(smooth_strength)])
    if decimate_mesh and decimate_mesh > 0:
        worker_args.extend(["--decimate-mesh", str(decimate_mesh)])

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
    if export_tie_points:
        worker_args.append("--export-tie-points")
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
    arg_tokens = repr(worker_args)

    excl_tokens: list[str] = []
    if exclude_folders:
        if isinstance(exclude_folders, str):
            excl_tokens = [tok.strip().lower() for tok in exclude_folders.split(",") if tok.strip()]
        else:
            for item in exclude_folders:
                excl_tokens.extend(tok.strip().lower() for tok in str(item).split(",") if tok.strip())

    if excl_tokens:
        excl_csv = ",".join(excl_tokens)
        exclude_setup_code = f"""os.environ["AUTOMETASHAPE_EXCLUDE_FOLDERS"] = {excl_csv!r}
import inspect as _am_ins
import autometashape.core.telemetry as _am_tel
if "exclude_folders" not in _am_ins.signature(_am_tel.discover_image_subfolder_groups).parameters:
    _am_orig_disc = getattr(_am_tel, "_orig_discover_image_subfolder_groups", _am_tel.discover_image_subfolder_groups)
    _am_tel._orig_discover_image_subfolder_groups = _am_orig_disc
    def _am_filtered_disc(images_dir, output_dir=None):
        _res = _am_orig_disc(images_dir, output_dir=output_dir)
        _raw = os.environ.get("AUTOMETASHAPE_EXCLUDE_FOLDERS", "")
        _pats = tuple(p.strip().lower() for p in _raw.split(",") if p.strip())
        if not _pats:
            return _res
        _out = {{}}
        _skipped_imgs = 0
        for _g, _paths in _res.items():
            if any(p in _g.lower() for p in _pats):
                _skipped_imgs += len(_paths)
                continue
            _kept = []
            for _fp in _paths:
                try:
                    _rel_dir = os.path.relpath(os.path.dirname(_fp), images_dir).replace("\\\\", "/")
                except Exception:
                    _rel_dir = os.path.dirname(_fp).replace("\\\\", "/")
                _segs = [s.lower() for s in _rel_dir.split("/") if s and s != "."]
                if any(any(p in seg for p in _pats) for seg in _segs):
                    _skipped_imgs += 1
                    continue
                _kept.append(_fp)
            if _kept:
                _out[_g] = _kept
        if _skipped_imgs:
            print(f"  [Ingest Filter] Excluded {{_skipped_imgs}} image(s) matching {{list(_pats)}}.", flush=True)
        return _out
    _am_tel.discover_image_subfolder_groups = _am_filtered_disc
    if "autometashape.core.pipeline" in sys.modules:
        sys.modules["autometashape.core.pipeline"].discover_image_subfolder_groups = _am_filtered_disc
"""
    else:
        exclude_setup_code = 'os.environ.pop("AUTOMETASHAPE_EXCLUDE_FOLDERS", None)\n'

    bootstrap_code = f"""import os, runpy, sys

user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
_repo_candidates = [
    os.environ.get("AUTOMETASHAPE_DIR", "").strip(),
    os.path.join(user_profile, "Documents", "AutoMetashape"),
    os.path.join(user_profile, "AutoMetashape"),
    os.getcwd(),
]
repo_dir = next((c for c in _repo_candidates if c and os.path.isfile(os.path.join(c, "scripts", "metashape_reconstruction.py"))), _repo_candidates[1])
script_path = os.path.join(repo_dir, "scripts", "metashape_reconstruction.py")
source_dir = os.path.join(repo_dir, "source")
if not os.path.isfile(script_path):
    raise FileNotFoundError(f"[Worker] Local AutoMetashape script not found: {{script_path}}")
if source_dir not in sys.path:
    sys.path.insert(0, source_dir)
{exclude_setup_code}sys.argv = [script_path] + {arg_tokens}
runpy.run_path(script_path, run_name="__main__")
"""

    if Metashape is not None:
        rs = Metashape.Tasks.RunScript()
        rs.code = bootstrap_code
        rs.args = cmd_args
        net_task = rs.toNetworkTask()
        try:
            if hasattr(net_task, "params") and isinstance(net_task.params, dict):
                net_task.params["code"] = bootstrap_code
                net_task.params["args"] = cmd_args
        except Exception:
            pass
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
        net_task.code = bootstrap_code
        net_task.args = cmd_args

    return net_task, rel_script, status_file


def build_unified_cluster_pipeline_tasks(
    chunk,
    output_dir: str,
    project_name: str,
    align_photos: bool = True,
    align_quality: str = "high",
    reference_preselection_mode: str = "estimated",
    keypoint_limit: int = 50000,
    keypoint_limit_per_mpx: int = 2000,
    tiepoint_limit: int = 40000,
    filter_stationary_points: bool = True,
    guided_matching: bool = True,
    adaptive_fitting: bool = True,
    filter_tiepoints: bool = True,
    crop_method: str = "gps-square",
    crop_padding: float = 0.05,
    build_depth_maps: bool = True,
    depth_quality: str = "high",
    depth_filtering: str = "moderate",
    build_point_cloud: bool = False,
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
    ortho_resolution: float = 0.05,
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
        t_match.keypoint_limit = keypoint_limit or 50000
        t_match.keypoint_limit_per_mpx = keypoint_limit_per_mpx or 2000
        t_match.tiepoint_limit = tiepoint_limit or 40000
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
    from autometashape.core.telemetry import is_projected_crs

    proj = Metashape.OrthoProjection()
    target_crs = chunk.crs if chunk else None
    if chunk and chunk.crs and not is_projected_crs(chunk.crs):
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
        if ortho_resolution and ortho_resolution > 0:
            t_om.resolution = float(ortho_resolution)
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
        if target_crs:
            t_dem.projection = proj
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
        if target_crs:
            t_ortho.projection = proj
        if ortho_resolution and ortho_resolution > 0:
            t_ortho.resolution = float(ortho_resolution)
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


CLUSTER_TICKETS_REL_DIR = ".cluster/tickets"


def _slugify_ticket_name(project_name: str) -> str:
    """Convert a project name into an ASCII filesystem-safe slug for hidden cluster tickets."""
    import unicodedata

    normalized = unicodedata.normalize("NFKD", str(project_name or "project").strip())
    cleaned = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in ("-", "_"))) else "_" for ch in normalized)
    return (cleaned.strip("_") or "project")[:80]


def resolve_batch_canonical_path(batch_path: str | None, shared_root: str = "") -> str:
    """Resolve a cluster batch path to its canonical project path (unwrapping hidden ``.cluster/tickets/``)."""
    raw = str(batch_path or "").strip()
    if not raw:
        return ""
    norm = raw.replace("\\", "/")
    low = norm.lower()
    if (low.startswith(f"{CLUSTER_TICKETS_REL_DIR}/") or f"/{CLUSTER_TICKETS_REL_DIR}/" in low) and shared_root:
        stem = os.path.splitext(os.path.basename(norm))[0]
        rel_dir = os.path.dirname(norm.lstrip("/"))
        meta_candidates = [os.path.join(shared_root, CLUSTER_TICKETS_REL_DIR, f"{stem}.ticket.json")]
        if rel_dir:
            meta_candidates.insert(0, os.path.join(shared_root, rel_dir, f"{stem}.ticket.json"))
        for meta_p in meta_candidates:
            if os.path.isfile(meta_p):
                try:
                    with open(meta_p, "r", encoding="utf-8") as mf:
                        meta = json.load(mf)
                    canon = str(meta.get("canonical_rel_psx") or "").strip()
                    if canon:
                        return canon
                    rel_out = str(meta.get("rel_output_dir") or "").replace("\\", "/").strip("/")
                    pname = str(meta.get("project_name") or "").strip()
                    if rel_out and pname:
                        return f"{rel_out}/{pname}.psx"
                except Exception:
                    pass
    return raw


def cleanup_stale_dispatch_tickets(
    shared_root: str,
    max_age_hours: float = 168.0,
    protected_paths: set[str] | None = None,
) -> int:
    """Prune old unreferenced ephemeral ticket files under ``<shared_root>/.cluster/tickets/``."""
    if not shared_root:
        return 0
    tickets_dir = os.path.join(shared_root, ".cluster", "tickets")
    if not os.path.isdir(tickets_dir):
        return 0
    protected_stems: set[str] = set()
    for p in protected_paths or set():
        base = os.path.basename(str(p or "").replace("\\", "/"))
        if base:
            protected_stems.add(os.path.splitext(base)[0].lower())
    cutoff = time.time() - max(0.0, max_age_hours) * 3600.0
    removed = 0
    try:
        import shutil

        for entry in os.listdir(tickets_dir):
            full = os.path.join(tickets_dir, entry)
            stem = entry[:-12] if entry.lower().endswith(".ticket.json") else os.path.splitext(entry)[0]
            if stem.lower() in protected_stems:
                continue
            try:
                if os.path.getmtime(full) > cutoff:
                    continue
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
                else:
                    os.remove(full)
                if not os.path.exists(full):
                    removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


def create_hidden_dispatch_ticket(
    shared_root: str,
    project_name: str,
    rel_output_dir: str = "",
    canonical_rel_psx: str = "",
    protected_paths: set[str] | None = None,
) -> str:
    """Materialize a minimal hidden scheduler ticket under ``.cluster/tickets/`` (never in ``Result/``).

    Used ONLY when Metashape Network Server retains an un-evictable aborted batch key for the
    canonical ``<project>.psx`` path. Keeps the user's deliverables directory spotless with a
    single canonical ``<project>.psx`` while writing a tiny ``.ticket.json`` metadata sidecar so
    ``cluster_monitor.py`` still verifies the real Result directory accurately.

    Returns:
        Cluster-relative POSIX path to the hidden ticket ``.psx`` (e.g. ``.cluster/tickets/<slug>_<ts>.psx``).
    """
    cleanup_stale_dispatch_tickets(shared_root, protected_paths=protected_paths)
    tickets_dir = os.path.join(shared_root, ".cluster", "tickets")
    os.makedirs(tickets_dir, exist_ok=True)
    slug = _slugify_ticket_name(project_name)
    stem = f"{slug}_{int(time.time() * 1000)}_{os.getpid()}"
    psx_path = os.path.join(tickets_dir, f"{stem}.psx")
    files_dir = os.path.join(tickets_dir, f"{stem}.files")
    os.makedirs(files_dir, exist_ok=True)
    project_zip = os.path.join(files_dir, "project.zip")

    materialized = False
    if Metashape is not None and hasattr(Metashape, "Document"):
        stub_doc = Metashape.Document()
        try:
            stub_doc.addChunk().label = "Dispatch_Ticket"
            stub_doc.save(psx_path)
            materialized = os.path.isfile(psx_path)
        except Exception:
            materialized = False
        finally:
            try:
                stub_doc.clear()
            except Exception:
                pass

    if not materialized:
        with open(psx_path, "w", encoding="utf-8") as fh:
            fh.write(
                f'<?xml version="1.0" encoding="UTF-8"?>\n<document version="1.2.0" path="{stem}.files/project.zip"/>\n'
            )
        chunk_dir = os.path.join(files_dir, "0")
        os.makedirs(chunk_dir, exist_ok=True)
        with zipfile.ZipFile(os.path.join(chunk_dir, "chunk.zip"), "w", compression=zipfile.ZIP_DEFLATED) as zc:
            zc.writestr("doc.xml", '<?xml version="1.0" encoding="UTF-8"?>\n<chunk version="1.2.0"/>\n')
        with zipfile.ZipFile(project_zip, "w", compression=zipfile.ZIP_DEFLATED) as zp:
            zp.writestr(
                "doc.xml",
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<document version="1.2.0">\n'
                '  <chunks next_id="1" active_id="0">\n'
                '    <chunk id="0" path="0/chunk.zip"/>\n'
                "  </chunks>\n"
                "</document>\n",
            )

    meta_path = os.path.join(tickets_dir, f"{stem}.ticket.json")
    try:
        with open(meta_path, "w", encoding="utf-8") as mf:
            json.dump(
                {
                    "project_name": project_name,
                    "rel_output_dir": rel_output_dir.replace("\\", "/").strip("/"),
                    "canonical_rel_psx": canonical_rel_psx.replace("\\", "/").lstrip("/"),
                    "created_at": time.time(),
                },
                mf,
                indent=2,
            )
    except OSError:
        pass

    return f"{CLUSTER_TICKETS_REL_DIR}/{stem}.psx"


def _safe_abort_batch(net_client, batch_id: int, is_paused: bool = False) -> None:
    """Unpause (if needed) and abort a cluster batch without freezing the Metashape scheduler."""
    if is_paused and hasattr(net_client, "setBatchPaused"):
        try:
            net_client.setBatchPaused(batch_id, False)
        except Exception:
            pass
    try:
        net_client.abortBatch(batch_id)
    except Exception:
        pass


def safe_create_cluster_batch(
    net_client: Metashape.NetworkClient,
    rel_psx: str,
    net_tasks: list,
    worker_limit: int | None = 1,
    force_fresh: bool = True,
    priority: int | None = None,
    start_paused: bool = False,
    shared_root: str = "",
    ticket_project_name: str = "",
    ticket_rel_output_dir: str = "",
) -> tuple[int, bool]:
    """Safely creates a cluster batch, resolving 'Duplicate batch' conflicts on the Network Server."""
    if net_client is None:
        raise ValueError("NetworkClient is not connected or None")

    norm_target = _norm_cluster_path(rel_psx)
    done_states = ("completed", "finished")
    terminal_states = ("completed", "finished", "failed", "aborted", "cancelled")

    if hasattr(net_client, "findBatch"):
        try:
            fb_id = net_client.findBatch(rel_psx)
            if isinstance(fb_id, int) and not isinstance(fb_id, bool) and fb_id > 0:
                fb_info = net_client.batchInfo(fb_id)
                fb_state = (
                    fb_info.get("state", "") if isinstance(fb_info, dict) else getattr(fb_info, "state", "")
                ).lower()
                fb_err_cnt = int((fb_info.get("error_count") if isinstance(fb_info, dict) else 0) or 0)
                fb_aborted = bool(isinstance(fb_info, dict) and (fb_info.get("failed") or fb_info.get("aborted")))
                fb_paused = bool(isinstance(fb_info, dict) and fb_info.get("paused"))
                fb_healthy_active = (
                    fb_state in ("inprogress", "running", "submitted", "pending", "queued")
                    and not fb_aborted
                    and fb_err_cnt == 0
                )
                if not force_fresh and fb_healthy_active:
                    print(
                        f"  [Cluster] Attaching to active batch #{fb_id} via findBatch for '{rel_psx}' ({fb_state}).",
                        flush=True,
                    )
                    return fb_id, True
                elif fb_state not in done_states and (
                    (force_fresh and fb_state != "aborted") or (fb_state not in terminal_states and not fb_aborted)
                ):
                    _safe_abort_batch(net_client, fb_id, is_paused=fb_paused)
                    time.sleep(0.5)
        except Exception:
            pass

    known_batch_paths: set[str] = set()
    try:
        raw_b = net_client.batchList()
        batches = (
            raw_b.get("batches", []) if isinstance(raw_b, dict) else (raw_b if isinstance(raw_b, (list, tuple)) else [])
        )
        for b in batches:
            b_path = b.get("path") if isinstance(b, dict) else getattr(b, "path", None)
            if b_path:
                known_batch_paths.add(str(b_path))
            canon_b_path = resolve_batch_canonical_path(b_path, shared_root)
            if _norm_cluster_path(b_path) == norm_target or _norm_cluster_path(canon_b_path) == norm_target:
                b_id = b.get("batch_id") if isinstance(b, dict) else getattr(b, "id", getattr(b, "batch_id", None))
                b_state = (b.get("state") if isinstance(b, dict) else getattr(b, "state", "")).lower()
                b_err_cnt = int((b.get("error_count") if isinstance(b, dict) else 0) or 0)
                b_aborted = bool(isinstance(b, dict) and (b.get("failed") or b.get("aborted")))
                b_paused = bool(isinstance(b, dict) and b.get("paused"))
                b_healthy_active = (
                    b_state in ("inprogress", "running", "submitted", "pending", "queued")
                    and not b_aborted
                    and b_err_cnt == 0
                )
                if not force_fresh and b_healthy_active:
                    print(f"  [Cluster] Attaching to active batch #{b_id} for '{rel_psx}' ({b_state}).", flush=True)
                    return b_id, True
                elif (
                    b_id is not None
                    and b_state not in done_states
                    and ((force_fresh and b_state != "aborted") or (b_state not in terminal_states and not b_aborted))
                ):
                    _safe_abort_batch(net_client, b_id, is_paused=b_paused)
                    time.sleep(0.5)
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
                    if b_path:
                        known_batch_paths.add(str(b_path))
                    canon_b_path = resolve_batch_canonical_path(b_path, shared_root)
                    if _norm_cluster_path(b_path) == norm_target or _norm_cluster_path(canon_b_path) == norm_target:
                        b_id = b.get("batch_id") if isinstance(b, dict) else getattr(b, "id", None)
                        b_state = (b.get("state") if isinstance(b, dict) else getattr(b, "state", "")).lower()
                        b_paused = bool(isinstance(b, dict) and b.get("paused"))
                        if b_id is not None and b_state not in done_states:
                            _safe_abort_batch(net_client, b_id, is_paused=b_paused)
                            aborted_ids.add(b_id)
                if not aborted_ids and hasattr(net_client, "findBatch"):
                    try:
                        fb_id = net_client.findBatch(rel_psx)
                        if (
                            isinstance(fb_id, int)
                            and not isinstance(fb_id, bool)
                            and fb_id > 0
                            and fb_id not in aborted_ids
                        ):
                            _safe_abort_batch(net_client, fb_id, is_paused=True)
                    except Exception:
                        pass
                time.sleep(1.0)
                try:
                    batch_id = net_client.createBatch(rel_psx, net_tasks)
                except Exception as e_retry_dup:
                    if "duplicate batch" in str(e_retry_dup).lower() and shared_root and os.path.isdir(shared_root):
                        # Metashape Server 2.2.2 locks hard-killed aborted RunScript batch keys in RAM until
                        # server restart. Dispatch via a hidden .cluster/tickets/ stub so Result/<project>/
                        # NEVER gets polluted with a second _dispatch_*.psx project file.
                        proj_label = ticket_project_name or os.path.splitext(os.path.basename(rel_psx))[0]
                        rel_out = ticket_rel_output_dir or os.path.dirname(rel_psx.replace("\\", "/"))
                        ticket_rel = create_hidden_dispatch_ticket(
                            shared_root=shared_root,
                            project_name=proj_label,
                            rel_output_dir=rel_out,
                            canonical_rel_psx=rel_psx,
                            protected_paths=known_batch_paths,
                        )
                        print(
                            f"  [Cluster Recovery] Server holds stale aborted key for '{rel_psx}'; "
                            f"dispatching via hidden ticket '{ticket_rel}' (Result folder stays clean).",
                            flush=True,
                        )
                        batch_id = net_client.createBatch(ticket_rel, net_tasks)
                    else:
                        raise e_retry_dup
            except Exception as e_retry:
                raise e_retry
        else:
            raise e_create

    # Configure priority and worker limits BEFORE unpausing so idle workers never claim at wrong priority
    if priority is not None and hasattr(net_client, "setBatchPriority"):
        try:
            net_client.setBatchPriority(batch_id, priority)
        except Exception:
            pass
    if worker_limit is not None and worker_limit > 0:
        try:
            net_client.setBatchWorkerLimit(batch_id, worker_limit)
        except Exception:
            pass
    try:
        net_client.setBatchPaused(batch_id, bool(start_paused))
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
    tasks_info = b_info.get("tasks") or []

    has_failed = bool(
        state in ("failed", "aborted")
        or b_info.get("failed")
        or b_info.get("aborted")
        or int(b_info.get("error_count") or 0) > 0
        or any(
            str(t.get("state") or "").lower() in ("failed", "aborted") or t.get("failed") or t.get("aborted")
            for t in tasks_info
            if isinstance(t, dict)
        )
    )

    if has_failed:
        err_msgs = []
        seen_msgs = set()

        def _add_msg(msg: str) -> None:
            cleaned = msg.strip()
            if cleaned and cleaned not in seen_msgs:
                seen_msgs.add(cleaned)
                err_msgs.append(cleaned)

        last_err = b_info.get("last_error")
        if isinstance(last_err, dict) and last_err.get("message"):
            _add_msg(str(last_err["message"]))
        elif isinstance(last_err, str) and last_err.strip():
            _add_msg(last_err)
        for t in tasks_info:
            for err in t.get("errors", []):
                if isinstance(err, dict):
                    _add_msg(str(err.get("message") or err.get("type") or err))
                else:
                    _add_msg(str(err))
        resolved_state = "aborted" if (state == "aborted" or b_info.get("aborted")) else "failed"
        err_str = "; ".join(err_msgs) if err_msgs else f"Batch {resolved_state} on cluster node"
        return resolved_state, False, err_str

    if state == "completed":
        return "completed", True, ""

    return state or "inprogress", False, ""
