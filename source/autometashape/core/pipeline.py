# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Core reconstruction pipeline: camera alignment, MVS depth maps, meshing, and texturing."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import shutil
import sys
import threading
import time

try:
    import Metashape
except ImportError:
    Metashape = None

from autometashape.cluster.hardware import extract_unc_root
from autometashape.cluster.orchestrator import (
    ALIGN_DOWNSCALE_MAP,
    DEPTH_DOWNSCALE_MAP,
    DEPTH_FILTER_MAP,
    POINT_CLOUD_FORMAT_MAP,
    PRESEL_MODE_MAP,
    SHAPES_FORMAT_MAP,
    SURFACE_TYPE_MAP,
    TARGET_TYPE_MAP,
    TILED_MODEL_FORMAT_MAP,
    _has_dense_points,
    build_cluster_crop_task,
    build_cluster_gradual_selection_tasks,
    build_monolithic_runscript_task,
    evaluate_cluster_batch_info,
    safe_create_cluster_batch,
)
from autometashape.cluster.staging import (
    check_scratch_disk_headroom,
    get_optimal_local_scratch_dir,
    get_psx_files_dir,
    sync_staged_project_to_network,
)
from autometashape.core.export import infer_project_name
from autometashape.core.filtering import (
    auto_crop_camera_gps_square,
    auto_crop_to_main_structure,
    filter_image_quality,
    filter_tie_points,
)
from autometashape.core.telemetry import (
    analyze_survey_physics,
    compute_utm_epsg,
    derive_pro_photogrammetry_plan,
    discover_image_subfolder_groups,
    print_pro_engineering_report,
)
from autometashape.ipc.protocol import (
    IPC_COMPLETE,
    IPC_PAUSE_CROP,
    emit_ipc_event,
    emit_ipc_stage,
    set_active_status_file,
)


def wait_for_user_resume(output_dir: str, psx_path: str) -> None:
    """Pauses pipeline execution to allow manual bounding box adjustment in Metashape GUI.

    Supports resuming via:
    1. Interactive terminal input [Enter].
    2. Standard input newline piped from a parent process / GUI.
    3. Sentinel signal file (.continue_crop) created in the output directory.
    """
    continue_file = os.path.join(output_dir, ".continue_crop")
    if os.path.exists(continue_file):
        try:
            os.remove(continue_file)
        except Exception:
            pass

    emit_ipc_event(IPC_PAUSE_CROP)
    print("\n=========================================================================", flush=True)
    print("  [PAUSE_CROP] PAUSED FOR MANUAL BOUNDING BOX IN AGISOFT METASHAPE PRO", flush=True)
    print("  Project checkpoint saved to:", flush=True)
    print(f"  --> {psx_path}", flush=True)
    print("  Instructions:", flush=True)
    print("  1. Open this project in Agisoft Metashape Pro GUI.", flush=True)
    print("  2. Use 'Resize Region' and 'Rotate Region' tools to frame the target structure.", flush=True)
    print("  3. Save the project inside Metashape Pro (Ctrl+S) and CLOSE Metashape Pro.", flush=True)
    print(
        "  4. In AutoMetashape Studio, click [[>] Continue Reconstruction] (or press [Enter] in terminal).", flush=True
    )
    print("=========================================================================", flush=True)

    # 1. Interactive terminal with Windows notification popup
    if sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
        if sys.platform == "win32":
            try:
                import winsound

                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass
            try:
                import ctypes
                import subprocess

                # MB_YESNO = 4, MB_ICONQUESTION = 0x20, MB_SYSTEMMODAL = 0x1000, IDYES = 6
                metashape_bin = os.path.normpath(os.path.join(os.path.dirname(sys.executable), "..", "metashape.exe"))
                if not os.path.isfile(metashape_bin):
                    from shutil import which

                    which_p = which("metashape.exe") or which("metashape")
                    if which_p and os.path.isfile(which_p):
                        metashape_bin = which_p
                    else:
                        for base in (
                            os.environ.get("PROGRAMFILES"),
                            os.environ.get("ProgramW6432"),
                            os.environ.get("PROGRAMFILES(X86)"),
                            os.environ.get("LOCALAPPDATA"),
                        ):
                            if base:
                                for sub in (
                                    os.path.join("Agisoft", "Metashape Pro", "metashape.exe"),
                                    os.path.join("Agisoft", "PhotoScan Pro", "photoscan.exe"),
                                    os.path.join("Programs", "Agisoft", "Metashape Pro", "metashape.exe"),
                                ):
                                    cand = os.path.join(base, sub)
                                    if os.path.isfile(cand):
                                        metashape_bin = cand
                                        break
                                if os.path.isfile(metashape_bin):
                                    break
                if not os.path.isfile(metashape_bin):
                    metashape_bin = "metashape.exe"

                res = ctypes.windll.user32.MessageBoxW(
                    0,
                    f"Camera alignment completed and saved to:\n{psx_path}\n\nWould you like to open Agisoft Metashape Pro now to adjust the bounding box?\n\n(Remember to save and CLOSE Metashape Pro before pressing Enter!)",
                    "AutoMetashape: Paused for Manual Cropping",
                    0x00000004 | 0x00000020 | 0x00001000,
                )
                if res == 6:  # IDYES
                    subprocess.Popen([metashape_bin, psx_path])
            except Exception:
                pass
        try:
            input("Press [Enter] to continue with GPU MVS Depth Maps and 600K Surface Meshing...")
            return
        except EOFError:
            pass

    # 2. GUI subprocess or non-interactive stream: background thread on stdin + file polling
    resume_flag = [False]

    def _stdin_reader():
        try:
            line = sys.stdin.readline()
            if line is not None:
                resume_flag[0] = True
        except Exception:
            pass

    t = threading.Thread(target=_stdin_reader, daemon=True)
    t.start()

    while not resume_flag[0]:
        if os.path.exists(continue_file):
            try:
                os.remove(continue_file)
            except Exception:
                pass
            resume_flag[0] = True
            break
        time.sleep(0.5)

    print("  [PAUSE_CROP] Resume signal received! Reloading modified project from disk...", flush=True)


def save_checkpoint(doc, target_path=None):
    """Safely saves Metashape document ensuring read_only is overridden and chunk is re-synchronized."""
    try:
        doc.read_only = False
    except Exception:
        pass

    try:
        if target_path and not getattr(doc, "path", None):
            doc.save(target_path)
        elif target_path:
            doc.save(target_path)
        else:
            doc.save()
    except Exception as e:
        # Fallback: force unlock and retry
        try:
            if target_path:
                files_dir = get_psx_files_dir(target_path)
                lock_file = os.path.join(files_dir, "lock")
                if os.path.isfile(lock_file):
                    try:
                        os.remove(lock_file)
                    except Exception:
                        pass
            doc.read_only = False
            if target_path:
                doc.save(target_path)
            else:
                doc.save()
        except Exception as retry_err:
            raise OSError(f"Failed to save Metashape project: {retry_err}") from e

    return doc.chunk


def run_reconstruction(
    images_dir: str,
    output_dir: str = None,
    align_quality: str = "high",
    align_photos: bool = True,
    build_depth_maps: bool = True,
    build_mesh: bool = True,
    build_texture: bool = True,
    keypoint_limit: int = 200000,
    keypoint_limit_per_mpx: int = 15000,
    tiepoint_limit: int = 25000,
    filter_stationary_points: bool = True,
    guided_matching: bool = True,
    adaptive_fitting: bool = True,
    reference_preselection_mode: str = "estimated",
    filter_tiepoints: bool = True,
    depth_quality: str = "high",
    depth_filtering: str = "moderate",
    custom_faces: int = None,
    texture_pages: int = None,
    points_spacing: float = 0.02,
    ortho_resolution: float = 0.02,
    ortho_cull_faces: bool = True,
    fill_holes: bool = True,
    ghosting_filter: bool = True,
    rebake_texture: bool = False,
    crop_method: str = None,
    crop_padding: float = 0.05,
    min_quality: float = 0.5,
    rolling_shutter: bool = True,
    close_holes: bool = False,
    pause_after_crop: bool = False,
    resume: bool = True,
    multi_chunk: str = "disable",
    chunk_align_method: str = "camera",
    preset: str = "pro",
    auto_params: bool = False,
    ground_cell_size: float = None,
    ground_max_angle: float = None,
    ground_max_distance: float = None,
    build_orthomosaic: bool = None,
    export_ortho: bool = False,
    build_dem: bool = None,
    export_dem: bool = False,
    build_point_cloud: bool = True,
    export_point_cloud: bool = False,
    point_cloud_format: str = "laz",
    classify_ground: bool = None,
    classify_points: bool = False,
    filter_point_confidence: int = None,
    export_report: bool = False,
    export_glb: bool = False,
    export_obj: bool = False,
    report_title: str = None,
    surface_type: str = "arbitrary",
    detect_markers: bool = False,
    target_type: str = "circular12",
    marker_tolerance: int = 50,
    import_reference: str = None,
    calibrate_colors: bool = False,
    smooth_mesh: bool = False,
    smooth_strength: float = 3.0,
    decimate_mesh: int = None,
    remove_lighting: bool = False,
    delighting_color_mode: bool = False,
    build_tiled_model: bool = False,
    tiled_model_format: str = "cesium",
    tile_size: int = 256,
    build_contours: bool = False,
    contour_interval: float = 1.0,
    contours_format: str = "shp",
    export_fbx: bool = False,
    export_ply: bool = False,
    export_dxf: bool = False,
    export_tie_points: bool = False,
    texture_format: str = "png",
    network_enable: bool = False,
    network_host: str = "127.0.0.1",
    network_port: int = 5840,
    network_path: str = "",
    network_independent_workers: bool = False,
    project_name: str | None = None,
    status_file: str | None = None,
    local_scratch_staging: bool = True,
    scratch_dir: str = "",
    min_scratch_free_gb: float = 100.0,
):
    """Executes the complete AutoMetashape photogrammetry reconstruction pipeline.

    Orchestrates end-to-end automated aerial and close-range photogrammetry processing:
    1. EXIF telemetry inspection, GSD calculation, and sensor optics compensation.
    2. Feature detection, tie-point matching, camera alignment, and self-calibration.
    3. Gradual tie-point selection filtering (Reprojection Error, Reconstruction Uncertainty, Projection Accuracy).
    4. MVS high-density depth map generation (CUDA/OpenCL hardware acceleration).
    5. Dense point cloud generation with automated ground and multiclass semantic classification.
    6. Arbitrary 3D surface mesh extraction with optional Laplacian smoothing and decimation.
    7. Multi-page 8K photogrammetric texture atlas parameterization and mosaic blending (16-page standard).
    8. Digital Elevation Model (DEM) and True Orthomosaic (direct mesh projection) generation.
    9. Deliverables export (glTF/GLB, Wavefront OBJ, Autodesk FBX, Stanford PLY, DXF, LAZ/LAS, GeoTIFF, PDF report).
    10. Distributed cluster processing support (Multi-Task delegate and Independent Project workers)
        with automated bad allocation memory detection and dynamic worker handoff.

    Args:
        images_dir: Directory containing input survey images (JPEG, TIFF, DNG).
        output_dir: Destination folder for output project (.psx) and exported assets.
        align_quality: Camera alignment resolution ("highest", "high", "medium", "low", "lowest").
        align_photos: Whether to run camera alignment.
        build_depth_maps: Whether to compute multi-view stereo depth maps.
        build_mesh: Whether to reconstruct 3D surface polygon mesh.
        build_texture: Whether to bake multi-page 8K texture atlas.
        keypoint_limit: Maximum feature keypoints detected per image.
        keypoint_limit_per_mpx: Adaptive keypoints detected per image megapixel.
        tiepoint_limit: Maximum tie-points matched per image pair.
        filter_stationary_points: Mask stationary tie points (e.g. camera rigs/lens dirt).
        guided_matching: Enable sub-pixel guided matching across image pairs.
        adaptive_fitting: Enable adaptive camera focal and lens distortion fitting.
        reference_preselection_mode: Pair preselection mode ("source", "estimated", "sequential").
        filter_tiepoints: Run USGS standard 3-stage gradual selection tie-point filtering.
        depth_quality: Depth map calculation quality level.
        depth_filtering: Depth map filtering mode ("mild", "moderate", "aggressive", "disabled").
        custom_faces: Target surface polygon count (e.g. 2,000,000 faces).
        texture_pages: Number of 8192x8192 (8K) texture atlas pages (default: 16).
        points_spacing: Point cloud sampling interval in meters.
        ortho_resolution: Ground sampling distance for exported orthomosaic in meters.
        ortho_cull_faces: Cull back-facing polygons during orthomosaic projection.
        fill_holes: Enable hole filling during texture blending.
        ghosting_filter: Enable ghosting artifact filter during texture blending.
        rebake_texture: Force re-baking UV parameterization and textures even if present.
        crop_method: Automated bounding box crop method ("gps", "tiepoints", or None).
        crop_padding: Safety bounding box margin ratio (default: 0.05).
        min_quality: Minimum image quality threshold for blur detection (0.0 to 1.0).
        rolling_shutter: Enable rolling shutter compensation for electronic shutters.
        close_holes: Planar surface hole closing level (0 to 100%).
        pause_after_crop: Pause execution for manual region editing in Metashape GUI.
        resume: Resume from existing project checkpoint without re-running finished stages.
        multi_chunk: Multi-chunk handling mode ("disable", "chunk_per_folder", "merge").
        chunk_align_method: Alignment method when merging chunks ("camera", "point").
        preset: Predefined processing configuration ("pro", "speed", "quality", "custom").
        auto_params: Automatically derive reconstruction settings from sensor telemetry.
        ground_cell_size: Cell size for automated ground classification in meters.
        ground_max_angle: Maximum terrain angle for ground classification in degrees.
        ground_max_distance: Maximum distance to terrain model for ground classification.
        build_orthomosaic: Build True Orthomosaic directly from 3D surface mesh.
        export_ortho: Export GeoTIFF True Orthomosaic raster.
        build_dem: Build Digital Elevation Model (DEM).
        export_dem: Export GeoTIFF DEM raster.
        build_point_cloud: Build dense point cloud.
        export_point_cloud: Export dense point cloud file.
        point_cloud_format: Format for exported point cloud ("laz", "las", "ply", "e57", "xyz").
        classify_ground: Run automated ground point classification.
        classify_points: Run multiclass AI semantic point classification.
        filter_point_confidence: Minimum point confidence threshold for cloud filtering.
        export_report: Export Metashape processing and calibration PDF report.
        export_glb: Export Binary glTF (.glb) model with embedded textures.
        export_obj: Export Wavefront OBJ (.obj) model with texture atlas.
        report_title: Custom title for generated PDF report.
        surface_type: Surface reconstruction type ("arbitrary" or "heightfield").
        detect_markers: Automatically detect coded/uncoded circular ground control targets.
        target_type: Marker target type ("circular12", "circular14", "circular16", "circular20").
        marker_tolerance: Detection tolerance for target markers (0 to 100).
        import_reference: Path to CSV reference file for camera coordinates / GCPs.
        calibrate_colors: Execute radiometric calibration and white balance.
        smooth_mesh: Apply Laplacian smoothing to surface mesh.
        smooth_strength: Smoothing strength factor.
        decimate_mesh: Target face count for mesh decimation.
        remove_lighting: Apply texture delighting (shadow/highlight removal).
        delighting_color_mode: Delighting color aggressiveness mode.
        build_tiled_model: Generate hierarchical 3D tiled model (LOD).
        tiled_model_format: Tiled model format ("cesium", "tls", "slpk", "osgb").
        tile_size: Pixel dimension for LOD tiles (default: 256).
        build_contours: Generate topographic contours from elevation model.
        contour_interval: Vertical elevation interval between contours in meters.
        contours_format: Topographic contour output format ("shp", "dxf", "geojson", "kml").
        export_fbx: Export Autodesk FBX (.fbx) model.
        export_ply: Export Stanford PLY (.ply) mesh model.
        export_dxf: Export AutoCAD DXF (.dxf) 3D faces model.
        export_tie_points: Export sparse tie points point cloud.
        texture_format: Texture atlas image format ("png", "tiff", "tif", "jpg", "jpeg").
        network_enable: Enable distributed Metashape network processing cluster.
        network_host: IP address or hostname of the cluster server.
        network_port: Port number of the cluster server (default: 5840).
        network_path: Network root path prefix for shared storage translation.
        network_independent_workers: Lock each project to an independent worker node executing
            strictly via the offline monolithic local scratch process (zero stage-by-stage fallback).
        project_name: Custom base name for project files and deliverables.
    """
    t_start = time.time()
    # Normalize paths and resolve relative paths against network_path if running on worker node
    if network_path:
        network_path = os.path.normpath(network_path.strip())
        if not os.path.isabs(images_dir) or not os.path.exists(images_dir):
            cand = os.path.normpath(os.path.join(network_path, images_dir))
            if os.path.exists(cand):
                images_dir = cand
        if output_dir and (not os.path.isabs(output_dir) or not os.path.exists(output_dir)):
            cand = os.path.normpath(os.path.join(network_path, output_dir))
            if os.path.exists(cand) or os.path.exists(os.path.dirname(cand)):
                output_dir = cand
        if status_file and not os.path.isabs(status_file):
            status_file = os.path.normpath(os.path.join(network_path, status_file))

    if not project_name:
        project_name = infer_project_name(images_dir=images_dir, output_dir=output_dir)
    if not output_dir:
        output_dir = os.path.join(images_dir, project_name)

    # Auto-detect shared storage UNC root when cluster mode is enabled and network_path is empty
    if not network_path and network_enable:
        detected_unc = extract_unc_root(output_dir) or extract_unc_root(images_dir)
        if detected_unc:
            network_path = detected_unc
            print(f"  [Network Cluster] Auto-detected shared storage UNC root: {network_path}", flush=True)

    # Resolve Local Scratch Staging (Mechanism B)
    if network_enable:
        # On cluster coordinator, tasks are dispatched to remote cluster workers.
        # Coordinator must save directly to shared network storage (output_dir) so all
        # worker nodes can access the project checkpoint over UNC.
        local_scratch_staging = False
    elif network_independent_workers:
        local_scratch_staging = True
    local_staging_dir = None
    target_network_output = None
    if local_scratch_staging:
        resolved_scratch = scratch_dir or get_optimal_local_scratch_dir(min_scratch_free_gb)
        has_space, free_gb = check_scratch_disk_headroom(resolved_scratch, min_scratch_free_gb)

        is_already_in_scratch = bool(
            resolved_scratch
            and (
                os.path.abspath(output_dir) == os.path.abspath(resolved_scratch)
                or os.path.abspath(output_dir).startswith(os.path.abspath(resolved_scratch) + os.sep)
            )
        )
        if has_space and resolved_scratch and not is_already_in_scratch:
            target_network_output = os.path.abspath(output_dir)
            local_staging_dir = os.path.join(resolved_scratch, f"{project_name}_local_stage")
            os.makedirs(local_staging_dir, exist_ok=True)
            output_dir = local_staging_dir
            print("=========================================================================", flush=True)
            print("  AUTOMETASHAPE: LOCAL HIGH-SPEED NVMe SCRATCH STAGING ENGAGED", flush=True)
            print(f"  Local Staging   : {local_staging_dir} ({free_gb:.1f} GB free)", flush=True)
            print(f"  Central Target  : {target_network_output}", flush=True)
            print("=========================================================================", flush=True)

            # Stage existing project checkpoint from central share if resume is requested
            if resume and target_network_output:
                cand_psx = os.path.join(target_network_output, f"{project_name}.psx")
                cand_legacy = os.path.join(target_network_output, "building_reconstruction.psx")
                net_psx = (
                    cand_psx if os.path.isfile(cand_psx) else (cand_legacy if os.path.isfile(cand_legacy) else None)
                )
                if net_psx:
                    local_psx = os.path.join(local_staging_dir, os.path.basename(net_psx))
                    print(
                        f"  [Local Scratch Staging] Staging existing project checkpoint from central share: "
                        f"{os.path.basename(net_psx)}...",
                        flush=True,
                    )
                    shutil.copy2(net_psx, local_psx)
                    net_files = get_psx_files_dir(net_psx)
                    local_files = os.path.join(local_staging_dir, os.path.basename(net_files))
                    if os.path.isdir(net_files):
                        print(
                            f"  [Local Scratch Staging] Staging project .files directory to local NVMe SSD: "
                            f"{os.path.basename(net_files)}...",
                            flush=True,
                        )
                        shutil.copytree(
                            net_files,
                            local_files,
                            dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("lock", "model.tmp", "*.tmp"),
                        )
            elif not resume and target_network_output:
                for old_p in (
                    os.path.join(target_network_output, f"{project_name}.psx"),
                    os.path.join(target_network_output, "building_reconstruction.psx"),
                ):
                    old_f = get_psx_files_dir(old_p)
                    if os.path.isdir(old_f):
                        try:
                            shutil.rmtree(old_f, ignore_errors=True)
                        except Exception:
                            pass
                    if os.path.isfile(old_p):
                        try:
                            os.remove(old_p)
                        except Exception:
                            pass
        else:
            print(
                f"[Local Scratch Staging] Insufficient local disk headroom "
                f"({free_gb:.1f} GB < {min_scratch_free_gb} GB). "
                f"Falling back to direct storage output: {output_dir}",
                flush=True,
            )

    os.makedirs(output_dir, exist_ok=True)
    psx_out = os.path.join(output_dir, f"{project_name}.psx")
    legacy_psx = os.path.join(output_dir, "building_reconstruction.psx")

    # Initialize active status sidecar immediately for real-time IPC tracking
    if not status_file:
        status_file = os.path.join(output_dir, "_autometashape_status.json")
    set_active_status_file(status_file)

    def _sync_staging_to_central_share():
        if target_network_output and local_staging_dir and os.path.isdir(local_staging_dir):
            print(
                f"\n[Local Scratch Staging] Synchronizing staged project to central share: {target_network_output}...",
                flush=True,
            )
            local_lock = os.path.join(local_staging_dir, f"{project_name}.files", "lock")
            if os.path.isfile(local_lock):
                try:
                    os.remove(local_lock)
                except Exception:
                    pass
            sync_ok = sync_staged_project_to_network(local_staging_dir, target_network_output)
            if sync_ok:
                if status_file and local_staging_dir in status_file:
                    set_active_status_file(status_file.replace(local_staging_dir, target_network_output))
                else:
                    set_active_status_file(None)
                print(
                    f"  [Local Scratch Staging] [OK] Successfully synchronized all deliverables to "
                    f"{target_network_output}!",
                    flush=True,
                )
            else:
                print(
                    f"  [Local Scratch Staging] [WARNING] Issue occurred during sync to {target_network_output}.",
                    file=sys.stderr,
                )

    if local_staging_dir:
        atexit.register(_sync_staging_to_central_share)

    # -----------------------------------------------------------------------
    # Pro Photogrammetry Survey Physics & Telemetry Resolution
    # -----------------------------------------------------------------------
    survey_physics = None
    pro_plan = None
    usgs_stats = None
    if preset == "building":
        preset = "pro"
    if preset == "custom":
        is_auto = False
    else:
        is_auto = True

    if is_auto:
        try:
            survey_physics = analyze_survey_physics(images_dir, output_dir=output_dir)
            pro_plan = derive_pro_photogrammetry_plan(survey_physics)
            print_pro_engineering_report(survey_physics, pro_plan)

            if crop_method is None:
                crop_method = pro_plan["crop_method"]
            if custom_faces is None:
                custom_faces = pro_plan["custom_faces"]
            if texture_pages is None:
                texture_pages = pro_plan["texture_pages"]
            if build_orthomosaic is None:
                build_orthomosaic = pro_plan["build_orthomosaic"]
            if build_dem is None:
                build_dem = pro_plan["build_dem"]
            if build_point_cloud is None:
                build_point_cloud = pro_plan["build_point_cloud"]
            if classify_ground is None:
                classify_ground = pro_plan["classify_ground"]
            if rolling_shutter is None:
                rolling_shutter = pro_plan["rolling_shutter"]
            if close_holes is None:
                close_holes = pro_plan["close_holes"]
            if depth_filtering is None:
                depth_filtering = pro_plan["depth_filtering"]
            if surface_type == "arbitrary" and pro_plan.get("surface_type") == "heightfield":
                surface_type = "heightfield"
            if not remove_lighting and pro_plan.get("remove_lighting"):
                remove_lighting = True
                delighting_color_mode = True
            if ground_cell_size is None:
                ground_cell_size = pro_plan.get("ground_cell_size", 35.0)
            if ground_max_angle is None:
                ground_max_angle = pro_plan.get("ground_max_angle", 15.0)
            if ground_max_distance is None:
                ground_max_distance = pro_plan.get("ground_max_distance", 0.5)
        except Exception as e_prof:
            print(
                f"  [Survey Physics] Note: Telemetry analysis ({e_prof}); using standard production geomatics defaults.",
                flush=True,
            )

    # Final safety fallbacks
    if crop_method is None:
        crop_method = "gps-square"
    if custom_faces is None:
        custom_faces = 2000000
    if texture_pages is None:
        texture_pages = 16
    is_pro_preset = preset == "pro" or is_auto
    if build_orthomosaic is None:
        build_orthomosaic = True if is_pro_preset else False
    if build_dem is None:
        build_dem = False
    if export_dem or build_contours:
        build_dem = True
    if build_point_cloud is None:
        build_point_cloud = True
    if classify_ground is None:
        classify_ground = False
    if close_holes is None:
        close_holes = False

    print("=========================================================================")
    if preset == "custom":
        preset_label = " [CUSTOM MANUAL CONFIGURATION]"
    elif preset:
        preset_label = f" [{preset.upper()} PRESET]"
    else:
        preset_label = " [AUTONOMOUS INFERENCE]" if is_auto else ""
    print(f"  AUTONOMOUS METASHAPE PHOTOGRAMMETRY PIPELINE{preset_label}")
    print("=========================================================================", flush=True)
    print(
        f"  Align Quality:      {align_quality.upper()} (Downscale: {ALIGN_DOWNSCALE_MAP.get(align_quality.lower(), 2)})"
    )
    print(f"  Keypoints / Tie:    {keypoint_limit:,} / {tiepoint_limit:,}")
    print(
        f"  Exclude Stationary: {filter_stationary_points} | Guided Matching: {guided_matching} | Adaptive Fitting: {adaptive_fitting}"
    )
    align_meth_label = chunk_align_method.upper() if chunk_align_method else "CAMERA"
    print(f"  Filter Tie Points:  {filter_tiepoints} | Multi-Chunk: {multi_chunk.upper()} (Method: {align_meth_label})")
    print(
        f"  Depth Maps Quality: {depth_quality.upper()} (Downscale: {DEPTH_DOWNSCALE_MAP.get(depth_quality.lower(), 4)}) | Filtering: {depth_filtering.capitalize()}"
    )
    if custom_faces >= 1_000_000:
        if custom_faces % 1_000_000 == 0:
            face_label = f"{custom_faces // 1_000_000}M"
        elif custom_faces % 100_000 == 0:
            face_label = f"{custom_faces / 1_000_000:.1f}M"
        else:
            face_label = f"{custom_faces // 1000}K"
    else:
        face_label = f"{custom_faces // 1000}K"
    print(f"  Target Mesh Faces:  {custom_faces:,} ({face_label})")
    print(
        f"  Texture Atlas:      {texture_pages} pages @ 8192x8192 (8K) | Fill Holes: {fill_holes} | Ghosting Filter: {ghosting_filter}"
    )
    print(
        f"  Bounding Box Crop:  {crop_method.upper()} | Rolling Shutter: {rolling_shutter} | Hole Closing: {close_holes}"
    )
    print(
        f"  Dense Point Cloud:  {'ENABLED' if build_point_cloud else 'DISABLED'} (Classify Ground: {classify_ground})"
    )
    print(
        f"  Elevation (DEM):    {'ENABLED' if build_dem else 'DISABLED'} | Orthomosaic: {'ENABLED' if build_orthomosaic else 'DISABLED'}"
    )
    print(f"  Quality PDF Report: {'ENABLED' if export_report else 'DISABLED'}")
    print(f"  Execution Mode:     {'RESUME CHECKPOINT (DEFAULT)' if resume else 'RESTART FROM SCRATCH'}")
    print("=========================================================================", flush=True)

    # Enable all available discrete high-performance GPUs (NVIDIA, AMD Radeon, Intel Arc), excluding integrated graphics
    gpus = (
        Metashape.app.enumGPUDevices() if hasattr(Metashape, "app") and hasattr(Metashape.app, "enumGPUDevices") else []
    )
    discrete_mask = 0
    active_gpu_names = []
    for i, g in enumerate(gpus):
        name = g.get("name", "").lower()
        vendor = g.get("vendor", "").lower()
        # Exclude integrated graphics (Intel UHD/HD, AMD APU Radeon)
        is_igpu = any(
            ig in name
            for ig in (
                "intel(r) graphics",
                "intel(r) hd",
                "intel(r) uhd",
                "intel(r) iris",
                "radeon(tm) graphics",
                "radeon graphics",
            )
        )
        if is_igpu:
            continue
        # Include discrete compute devices (NVIDIA, discrete AMD Radeon, discrete Intel Arc)
        is_discrete = any(
            k in vendor or k in name
            for k in ("nvidia", "geforce", "rtx", "quadro", "tesla", "radeon", "intel arc", "arc(tm)")
        )
        if is_discrete:
            discrete_mask |= 1 << i
            active_gpu_names.append(f"{g['name']} ({g['mem_size'] / (1024**3):.1f} GB VRAM)")

    # When running in network worker mode, omit setting gpu_mask so the server dynamically orchestrates hardware
    is_network_worker = any("--worker" in arg.lower() for arg in sys.argv) or (
        bool(network_path) and not network_enable
    )
    if not is_network_worker:
        if discrete_mask:
            Metashape.app.gpu_mask = discrete_mask
            print(f"[Hardware] GPU: {', '.join(active_gpu_names)} (Mask: {discrete_mask})", flush=True)
        elif gpus and not any(
            any(
                ig in g.get("name", "").lower()
                for ig in ("intel(r) graphics", "intel(r) hd", "intel(r) uhd", "radeon(tm) graphics")
            )
            for g in gpus
        ):
            Metashape.app.gpu_mask = (1 << len(gpus)) - 1
            print(f"[Hardware] GPU: {gpus[0]['name']} ({gpus[0]['mem_size'] / (1024**3):.1f} GB VRAM)", flush=True)
        else:
            Metashape.app.gpu_mask = 0
            print("[Hardware] Running on CPU.", flush=True)
    else:
        print("[Hardware] Network worker execution: honoring server/engine dynamic GPU orchestration.", flush=True)

    # Distributed Network Cluster Processing
    net_client = None
    cluster_enabled = False
    active_cluster_workers = []
    if network_enable and Metashape is not None and hasattr(Metashape, "app") and hasattr(Metashape.app, "settings"):
        try:
            print("\n=========================================================================")
            print("  AUTOMETASHAPE: DISTRIBUTED NETWORK PROCESSING CLUSTER")
            print(f"  Server Host: {network_host}:{network_port}")
            if network_path:
                print(f"  Storage Root (UNC): {network_path}")
            print("=========================================================================", flush=True)

            Metashape.app.settings.network_enable = True
            Metashape.app.settings.network_host = str(network_host).strip()
            Metashape.app.settings.network_port = int(network_port)
            if network_path:
                Metashape.app.settings.network_path = str(network_path).strip()

            if hasattr(Metashape, "NetworkClient"):
                client = Metashape.NetworkClient()
                try:
                    client.connect(f"{network_host}:{network_port}")
                    raw_workers = client.workerList()
                    if isinstance(raw_workers, dict):
                        workers = raw_workers.get("workers", [])
                    elif isinstance(raw_workers, (list, tuple)):
                        workers = list(raw_workers)
                    else:
                        workers = []

                    for w in workers:
                        w_id = w.get("worker_id") if isinstance(w, dict) else getattr(w, "id", None)
                        w_paused = w.get("paused", False) if isinstance(w, dict) else getattr(w, "paused", False)
                        if w_paused and w_id is not None:
                            try:
                                client.setWorkerPaused(w_id, False)
                            except Exception:
                                pass

                    raw_workers = client.workerList()
                    if isinstance(raw_workers, dict):
                        workers = raw_workers.get("workers", [])
                    elif isinstance(raw_workers, (list, tuple)):
                        workers = list(raw_workers)
                    else:
                        workers = []

                    active_cluster_workers = [
                        w
                        for w in workers
                        if not (w.get("paused", False) if isinstance(w, dict) else getattr(w, "paused", False))
                    ]
                    print("  [Network Cluster] Connected to server successfully!", flush=True)
                    print(
                        f"  [Network Cluster] Cluster Nodes: {len(workers)} ({len(active_cluster_workers)} active workers)",
                        flush=True,
                    )
                    for idx, w in enumerate(workers, 1):
                        if isinstance(w, dict):
                            host_label = w.get("hostname", "Node")
                            g_mask = w.get("gpu_mask", "Auto")
                            prio = w.get("priority", 0)
                            cpu_en = w.get("cpu_enabled", False)
                        else:
                            host_label = getattr(w, "host", getattr(w, "hostname", "Node"))
                            g_mask = getattr(w, "gpu_mask", "Auto")
                            prio = getattr(w, "priority", 0)
                            cpu_en = getattr(w, "cpu_enabled", False)
                        print(
                            f"    * Node {idx}: {host_label} (GPU Mask: {g_mask}, CPU: {'On' if cpu_en else 'Off'}, Priority: {prio})",
                            flush=True,
                        )

                    if len(active_cluster_workers) > 0:
                        net_client = client
                        cluster_enabled = True
                    else:
                        print(
                            "  [Network Cluster] [NOTICE] 0 active worker nodes connected to server. Will execute tasks locally.",
                            flush=True,
                        )
                except Exception as e_net_conn:
                    print(
                        f"  [Network Cluster] [NOTICE] Server connection failed at {network_host}:{network_port} ({e_net_conn}). Processing locally.",
                        flush=True,
                    )
        except Exception as e_net_init:
            print(
                f"  [Network Cluster] [WARNING] Error configuring network processing: {e_net_init}. Processing locally.",
                flush=True,
            )
    elif Metashape is not None and hasattr(Metashape, "app") and hasattr(Metashape.app, "settings"):
        try:
            Metashape.app.settings.network_enable = False
        except Exception:
            pass

    doc = Metashape.Document()
    resumed = False
    target_psx = psx_out if os.path.exists(psx_out) else (legacy_psx if os.path.exists(legacy_psx) else None)

    def _safe_reopen():
        load_path = (
            psx_out
            if os.path.exists(psx_out)
            else (target_psx if target_psx and os.path.exists(target_psx) else psx_out)
        )
        files_dir = get_psx_files_dir(load_path)
        lock_file = os.path.join(files_dir, "lock")
        if os.path.isfile(lock_file):
            try:
                os.remove(lock_file)
            except Exception:
                pass
        try:
            doc.open(load_path, read_only=False, ignore_lock=True)
        except Exception:
            try:
                doc.open(load_path, ignore_lock=True)
            except Exception:
                doc.open(load_path)
        try:
            doc.read_only = False
        except Exception:
            pass
        if status_file:
            set_active_status_file(status_file)
        if doc.chunk is None and len(doc.chunks) > 0:
            doc.chunk = doc.chunks[0]

    def get_cluster_rel_path(target_file: str = "") -> str:
        tgt = target_file or psx_out
        if network_path:
            norm_tgt = os.path.abspath(tgt).replace("\\\\", "/").replace("\\", "/")
            norm_net = network_path.strip().replace("\\\\", "/").replace("\\", "/").rstrip("/")

            # 1. Try standard commonpath if on same drive or both UNC
            try:
                abs_tgt = os.path.abspath(tgt)
                abs_net = os.path.abspath(network_path)
                if abs_net.startswith(r"\\") and abs_net.count("\\") == 3 and not abs_net.endswith("\\"):
                    abs_net_norm = abs_net + "\\"
                else:
                    abs_net_norm = abs_net
                if os.path.commonpath([abs_tgt, abs_net_norm]).lower().rstrip("\\") == abs_net.lower().rstrip("\\"):
                    return os.path.relpath(abs_tgt, abs_net_norm).replace("\\", "/")
            except Exception:
                pass

            # 2. Match share name from UNC path against target path components
            net_parts = [p for p in norm_net.split("/") if p]
            if net_parts:
                share_name = net_parts[-1].lower()
                tgt_parts = [p for p in norm_tgt.split("/") if p]
                tgt_lower = [p.lower() for p in tgt_parts]
                if share_name in tgt_lower:
                    idx = tgt_lower.index(share_name)
                    rel = "/".join(tgt_parts[idx + 1 :])
                    if rel:
                        return rel

            # 3. Fallback: match any common subfolder in net_path
            for i in range(len(net_parts) - 1, -1, -1):
                sub = net_parts[i].lower()
                tgt_parts = [p for p in norm_tgt.split("/") if p]
                tgt_lower = [p.lower() for p in tgt_parts]
                if sub in tgt_lower:
                    idx = tgt_lower.index(sub)
                    rel = "/".join(tgt_parts[idx + 1 :])
                    if rel:
                        return rel

        return os.path.basename(tgt.replace("\\", "/"))

    def get_cluster_export_path(target_file: str) -> str:
        """Returns an absolute network-accessible path (UNC or local absolute) for cluster export tasks."""
        norm_tgt = os.path.abspath(target_file).replace("\\", "/")
        if network_path:
            is_unc = network_path.startswith(r"\\") or network_path.startswith("//")
            clean_net = network_path.strip().replace("\\", "/").rstrip("/")
            if is_unc:
                clean_net = "//" + clean_net.lstrip("/")
            rel = get_cluster_rel_path(target_file)
            if rel and rel != norm_tgt and not rel.startswith(clean_net):
                return f"{clean_net}/{rel}"
        return norm_tgt

    def dispatch_cluster_task(
        tasks: Metashape.Task | list[Metashape.Task], task_desc: str, stage_key: str = ""
    ) -> bool:
        """Dispatch photogrammetry task(s) to the network cluster with real-time progress monitoring."""
        if not cluster_enabled or net_client is None:
            return False

        try:
            rel_psx = get_cluster_rel_path()
            if stage_key:
                emit_ipc_stage(stage_key, task_desc)
            print(
                f"\n[Network Cluster] Dispatching '{task_desc}' to cluster ({network_host}:{network_port})...",
                flush=True,
            )

            doc.save(psx_out)
            task_list = tasks if isinstance(tasks, (list, tuple)) else [tasks]
            chunk_target = doc.chunk or (doc.chunks[0] if doc.chunks else None)
            if chunk_target is None:
                print(f"  [Cluster] [ERROR] Document has no active chunk for '{task_desc}'", flush=True)
                return False
            if network_independent_workers:
                raise RuntimeError(
                    f"[Cluster] Illegal stage-by-stage network task dispatch attempted for '{task_desc}'. "
                    "The offline monolithic process is the exclusive execution method for independent workers."
                )
            net_tasks = [t.toNetworkTask(chunk_target) for t in task_list]
            w_limit = None
            batch_id, was_attached = safe_create_cluster_batch(
                net_client, rel_psx, net_tasks, worker_limit=w_limit, force_fresh=False
            )
            if was_attached:
                print(f"  [Cluster] Attached to existing batch #{batch_id} for '{task_desc}'", flush=True)

            t_task_start = time.time()
            last_progress = -1.0
            last_log_time = 0.0
            while True:
                time.sleep(1.0)
                b_info = net_client.batchInfo(batch_id)
                tasks_info = b_info.get("tasks", [])
                if tasks_info:
                    total_prog = sum(t.get("progress", 0.0) for t in tasks_info) / len(tasks_info)
                    active_task = next(
                        (t for t in tasks_info if t.get("state") in ("running", "submitted")), tasks_info[-1]
                    )
                    t_sub_name = active_task.get("name", task_desc)
                    sub_prog = active_task.get("progress", 0.0)
                    now_t = time.time()
                    if abs(total_prog - last_progress) >= 0.5 or (
                        now_t - last_log_time >= 10.0 and total_prog != last_progress
                    ):
                        elapsed_min = (now_t - t_task_start) / 60.0
                        if len(tasks_info) > 1:
                            print(
                                f"  [Cluster Progress] {task_desc} - {t_sub_name}: {sub_prog:.1f}% (Overall: {total_prog:.1f}%, Elapsed: {elapsed_min:.1f} min)",
                                flush=True,
                            )
                        else:
                            print(
                                f"  [Cluster Progress] {task_desc}: {total_prog:.1f}% (Elapsed: {elapsed_min:.1f} min)",
                                flush=True,
                            )
                        last_progress = total_prog
                        last_log_time = now_t

                b_status, b_success, b_err = evaluate_cluster_batch_info(b_info)
                if b_success:
                    elapsed_min = (time.time() - t_task_start) / 60.0
                    print(
                        f"  [Cluster] [OK] Task '{task_desc}' finished on cluster in {elapsed_min:.2f} min!", flush=True
                    )
                    break
                elif b_status in ("failed", "aborted"):
                    err_str = b_err or "unknown cluster error"
                    print(f"  [Cluster] [ERROR] Task '{task_desc}' {b_status}: {err_str}", flush=True)

                    is_bad_alloc = any(
                        any(term in str(e).lower() for term in ("bad allocation", "bad_alloc", "out of memory"))
                        for t in tasks_info
                        for e in t.get("errors", [])
                    )
                    if is_bad_alloc:
                        failed_t = next(
                            (
                                t
                                for t in tasks_info
                                if any(
                                    term in str(e).lower()
                                    for term in ("bad allocation", "bad_alloc", "out of memory")
                                    for e in t.get("errors", [])
                                )
                            ),
                            tasks_info[0] if tasks_info else None,
                        )
                        failed_w_id = failed_t.get("worker_id") if failed_t else None
                        print(
                            f"\n  [Cluster Memory Gate] Worker node {failed_w_id} encountered bad allocation (out of memory) on '{task_desc}'.",
                            flush=True,
                        )
                        print(
                            f"  [Cluster] Terminating task on Worker {failed_w_id} and routing to another cluster machine...",
                            flush=True,
                        )
                        emit_ipc_event(f"@@AUTOMETASHAPE_BAD_ALLOC:{stage_key or 'task'}:{failed_w_id}@@")

                        try:
                            if failed_w_id is not None:
                                net_client.setWorkerPaused(failed_w_id, True)

                            raw_w = net_client.workerList()
                            w_list = (
                                raw_w.get("workers", [])
                                if isinstance(raw_w, dict)
                                else (raw_w if isinstance(raw_w, (list, tuple)) else [])
                            )
                            other_workers = [
                                w
                                for w in w_list
                                if (w.get("worker_id") if isinstance(w, dict) else getattr(w, "id", None))
                                != failed_w_id
                                and not (w.get("paused", False) if isinstance(w, dict) else getattr(w, "paused", False))
                            ]

                            if other_workers:
                                print(
                                    f"  [Cluster] Found {len(other_workers)} other active cluster node(s). Re-dispatching '{task_desc}'...",
                                    flush=True,
                                )
                                retry_batch_id, _ = safe_create_cluster_batch(
                                    net_client,
                                    rel_psx,
                                    net_tasks,
                                    worker_limit=None,
                                    force_fresh=True,
                                )

                                retry_ok = False
                                while True:
                                    time.sleep(1.0)
                                    rb_info = net_client.batchInfo(retry_batch_id)
                                    rb_status, rb_success, rb_err = evaluate_cluster_batch_info(rb_info)
                                    if rb_success:
                                        print(
                                            f"  [Cluster] [OK] Task '{task_desc}' successfully finished on another cluster worker!",
                                            flush=True,
                                        )
                                        retry_ok = True
                                        break
                                    elif rb_status in ("failed", "aborted"):
                                        break

                                if retry_ok:
                                    break
                        except Exception as e_handoff:
                            print(
                                f"  [Cluster] Note: could not hand off task to another worker: {e_handoff}", flush=True
                            )
                        finally:
                            if failed_w_id is not None:
                                try:
                                    net_client.setWorkerPaused(failed_w_id, False)
                                except Exception:
                                    pass

                    print(f"  [Cluster] Falling back to local execution for '{task_desc}'...", flush=True)
                    return False

            _safe_reopen()
            return True

        except Exception as e_dispatch:
            print(
                f"  [Cluster] [WARNING] Cluster dispatch exception: {e_dispatch}. Falling back to local execution...",
                flush=True,
            )
            return False

    if resume and target_psx:
        print(f"\n[Checkpoint] Found existing project (resume enabled): {target_psx}", flush=True)
        try:
            files_dir = get_psx_files_dir(psx_out)
            lock_file = os.path.join(files_dir, "lock")
            if os.path.isfile(lock_file):
                try:
                    os.remove(lock_file)
                    print(f"  [Auto-Recovery] Removed stale lock file from previous session: {lock_file}", flush=True)
                except Exception as e_lock:
                    print(f"  [Warning] Could not remove lock file: {e_lock}", flush=True)
            try:
                doc.open(target_psx, read_only=False, ignore_lock=True)
            except Exception:
                try:
                    doc.open(target_psx, ignore_lock=True)
                except Exception:
                    doc.open(target_psx)
            try:
                doc.read_only = False
            except Exception:
                pass
            prod_chunk = None
            for c in doc.chunks:
                if c.label == "Production_Reconstruction":
                    prod_chunk = c
                    break
            if prod_chunk is None and len(doc.chunks) > 1:
                prod_chunk = max(doc.chunks, key=lambda c: sum(1 for cam in c.cameras if cam.transform and cam.enabled))
            if prod_chunk is not None:
                doc.chunk = prod_chunk
            chunk = doc.chunk
            aligned_count = sum(1 for c in chunk.cameras if c.transform and c.enabled)
            if aligned_count >= 2:
                print(
                    f"  Successfully loaded project with {len(chunk.cameras)} cameras ({aligned_count} aligned).",
                    flush=True,
                )
                print("  Skipping Steps 1-3 (Ingestion, Sharpness Filter, Feature Matching, Alignment).", flush=True)
                resumed = True
            else:
                print(
                    f"  Existing project has insufficient aligned cameras ({aligned_count}). Restarting from Step 1.",
                    flush=True,
                )
                doc = Metashape.Document()
                chunk = doc.addChunk()
                chunk.label = "Production_Reconstruction"
        except Exception as e:
            print(f"  Failed to load checkpoint ({e}). Starting fresh project.", flush=True)
            doc = Metashape.Document()
            chunk = doc.addChunk()
            chunk.label = "Production_Reconstruction"
    else:
        files_dir = get_psx_files_dir(psx_out)
        if not resume:
            if os.path.isdir(files_dir) or os.path.exists(psx_out) or (target_psx and os.path.exists(target_psx)):
                print(
                    f"\n[Run Mode] Starting fresh reconstruction from scratch (--no-resume specified). Overwriting previous run in: {output_dir}",
                    flush=True,
                )
                if os.path.isdir(files_dir):
                    try:
                        shutil.rmtree(files_dir, ignore_errors=True)
                    except Exception:
                        pass
                if os.path.isfile(psx_out):
                    try:
                        os.remove(psx_out)
                    except Exception:
                        pass
                if target_psx and target_psx != psx_out and os.path.isfile(target_psx):
                    try:
                        os.remove(target_psx)
                    except Exception:
                        pass
                if target_network_output and target_network_output != output_dir:
                    net_f = get_psx_files_dir(os.path.join(target_network_output, f"{project_name}.psx"))
                    if os.path.isdir(net_f):
                        try:
                            shutil.rmtree(net_f, ignore_errors=True)
                        except Exception:
                            pass
                    net_p = os.path.join(target_network_output, f"{project_name}.psx")
                    if os.path.isfile(net_p):
                        try:
                            os.remove(net_p)
                        except Exception:
                            pass
        chunk = doc.addChunk()
        chunk.label = "Production_Reconstruction"

    if not resumed:
        # Step 1: Discover Images (Single-folder or Multi-Subfolder Chunks)
        groups = discover_image_subfolder_groups(images_dir, output_dir=output_dir)
        total_photos = sum(len(imgs) for imgs in groups.values())
        if total_photos == 0:
            raise ValueError(f"No valid image files found recursively in '{images_dir}'!")

        align_downscale = ALIGN_DOWNSCALE_MAP.get(align_quality.lower(), 2)
        presel_mode_enum = PRESEL_MODE_MAP.get(
            (reference_preselection_mode or "estimated").lower(),
            Metashape.ReferencePreselectionMode.ReferencePreselectionEstimated,
        )

        use_multi_chunk = False
        if multi_chunk == "force" and len(groups) >= 1:
            use_multi_chunk = True
        elif multi_chunk == "auto" and len(groups) > 1:
            use_multi_chunk = True
        elif multi_chunk == "disable":
            use_multi_chunk = False

        if use_multi_chunk:
            print(
                f"\n[Multi-Chunk Mode] Detected {len(groups)} distinct image subfolders ({total_photos:,} photos total).",
                flush=True,
            )
            print(
                "  Ingesting each subfolder into an independent chunk for drift-free camera alignment and point-based RANSAC ICP registration...",
                flush=True,
            )

            for c in list(doc.chunks):
                if len(c.cameras) == 0:
                    doc.remove(c)

            created_chunks = []
            for idx, (grp_name, filepaths) in enumerate(groups.items()):
                sub_chunk = doc.addChunk()
                sub_chunk.label = f"Flight_{idx + 1}_{grp_name}"
                emit_ipc_stage(
                    "ingest",
                    f"Ingesting Chunk {idx + 1}/{len(groups)} ('{sub_chunk.label}'): {len(filepaths):,} photos",
                )
                print(
                    f"\n[1/7] Ingesting Chunk {idx + 1}/{len(groups)} ('{sub_chunk.label}'): {len(filepaths):,} photos...",
                    flush=True,
                )
                sub_chunk.addPhotos(filepaths)
                print(f"  Loaded {len(sub_chunk.cameras):,} cameras into '{sub_chunk.label}'.", flush=True)

                if min_quality > 0.0:
                    filter_image_quality(sub_chunk, min_quality=min_quality)

                if rolling_shutter:
                    for s in sub_chunk.sensors:
                        s.rolling_shutter = Metashape.Shutter.Model.Regularized
                    print(
                        f"  [Rolling Shutter] Enabled Regularized CMOS rolling shutter compensation on '{sub_chunk.label}'.",
                        flush=True,
                    )

                created_chunks.append(sub_chunk)

            # Step 2: Feature Detection & Matching per chunk
            for idx, c in enumerate(created_chunks):
                emit_ipc_stage("match", f"GPU Feature Matching for Chunk {idx + 1}/{len(created_chunks)}")
                print(
                    f"\n[2/7] GPU Feature Matching for Chunk {idx + 1}/{len(created_chunks)} ('{c.label}', {len(c.cameras):,} cameras)...",
                    flush=True,
                )
                t_match = time.time()
                c.matchPhotos(
                    downscale=align_downscale,
                    generic_preselection=True,
                    reference_preselection=True,
                    reference_preselection_mode=presel_mode_enum,
                    keypoint_limit=keypoint_limit,
                    keypoint_limit_per_mpx=keypoint_limit_per_mpx,
                    tiepoint_limit=tiepoint_limit,
                    mask_tiepoints=False,
                    filter_stationary_points=filter_stationary_points,
                    guided_matching=guided_matching,
                )
                print(f"  Matching finished for '{c.label}' in {(time.time() - t_match) / 60:.2f} min.", flush=True)

            # Step 3: Camera Alignment per chunk
            for idx, c in enumerate(created_chunks):
                emit_ipc_stage("align", f"Aligning Cameras for Chunk {idx + 1}/{len(created_chunks)}")
                print(
                    f"\n[3/7] Aligning Cameras for Chunk {idx + 1}/{len(created_chunks)} ('{c.label}', Adaptive Fitting: {adaptive_fitting})...",
                    flush=True,
                )
                t_align = time.time()
                c.alignCameras(adaptive_fitting=adaptive_fitting)
                aligned_count = sum(1 for cam in c.cameras if cam.transform and cam.enabled)
                enabled_count = sum(1 for cam in c.cameras if cam.enabled)
                print(
                    f"  Chunk '{c.label}' alignment finished in {(time.time() - t_align) / 60:.2f} min. Aligned: {aligned_count}/{enabled_count} active cameras.",
                    flush=True,
                )

                if filter_tiepoints:
                    usgs_stats = filter_tie_points(c)

                try:
                    c.updateTransform()
                except Exception:
                    pass

            aligned_chunks = [
                c for c in created_chunks if sum(1 for cam in c.cameras if cam.transform and cam.enabled) >= 2
            ]
            if len(aligned_chunks) == 0:
                raise RuntimeError(
                    "No cameras could be aligned across any flight subfolder! Please verify photo quality and overlap."
                )

            anchor_chunk = max(
                aligned_chunks, key=lambda c: sum(1 for cam in c.cameras if cam.transform and cam.enabled)
            )
            ref_key = anchor_chunk.key
            merge_chunk_keys = [c.key for c in created_chunks]
            align_chunk_keys = [c.key for c in aligned_chunks]

            if len(aligned_chunks) >= 2:
                align_mode = chunk_align_method.lower() if chunk_align_method else "camera"
                if align_mode == "point":
                    print(
                        f"\n[Chunk Alignment] Registering {len(align_chunk_keys)} aligned chunks onto anchor '{anchor_chunk.label}' using Point-Based RANSAC ICP (method=0, fit_scale=True)...",
                        flush=True,
                    )
                    try:
                        doc.alignChunks(
                            chunks=align_chunk_keys,
                            reference=ref_key,
                            method=0,
                            fit_scale=True,
                            downscale=align_downscale,
                            keypoint_limit=keypoint_limit,
                        )
                        print("  [Chunk Alignment] Point-Based RANSAC ICP chunk alignment succeeded!", flush=True)
                    except Exception as e_align:
                        print(
                            f"  [Chunk Alignment] Point-based ICP raised '{e_align}'; falling back to Camera/GPS-based registration (method=2)...",
                            flush=True,
                        )
                        try:
                            doc.alignChunks(
                                chunks=align_chunk_keys,
                                reference=ref_key,
                                method=2,
                                fit_scale=False,
                            )
                            print(
                                "  [Chunk Alignment] Camera/GPS-based chunk alignment (method=2) succeeded!", flush=True
                            )
                        except Exception as e_cam:
                            print(
                                f"  [Chunk Alignment] Camera-based alignment raised '{e_cam}'; proceeding with native camera transforms.",
                                flush=True,
                            )
                else:
                    print(
                        f"\n[Chunk Alignment] Registering {len(align_chunk_keys)} aligned chunks onto anchor '{anchor_chunk.label}' using Camera/GPS-based registration (method=2)...",
                        flush=True,
                    )
                    try:
                        doc.alignChunks(
                            chunks=align_chunk_keys,
                            reference=ref_key,
                            method=2,
                            fit_scale=False,
                        )
                        print("  [Chunk Alignment] Camera/GPS-based chunk alignment (method=2) succeeded!", flush=True)
                    except Exception as e_cam:
                        print(
                            f"  [Chunk Alignment] Camera-based registration raised '{e_cam}'; falling back to Point-Based RANSAC ICP (method=0)...",
                            flush=True,
                        )
                        try:
                            doc.alignChunks(
                                chunks=align_chunk_keys,
                                reference=ref_key,
                                method=0,
                                fit_scale=True,
                                downscale=align_downscale,
                                keypoint_limit=keypoint_limit,
                            )
                            print("  [Chunk Alignment] Point-Based RANSAC ICP chunk alignment succeeded!", flush=True)
                        except Exception as e_align:
                            print(
                                f"  [Chunk Alignment] Point-based alignment raised '{e_align}'; proceeding with native camera transforms.",
                                flush=True,
                            )
            else:
                print(
                    f"\n[Chunk Alignment] Anchor chunk '{anchor_chunk.label}' is the only flight with aligned cameras; skipping chunk registration.",
                    flush=True,
                )

            print(
                f"\n[Chunk Merging] Merging {len(merge_chunk_keys)} chunks into unified production reconstruction chunk...",
                flush=True,
            )
            try:
                doc.mergeChunks(chunks=merge_chunk_keys, merge_tiepoints=True)
            except Exception as e_merge:
                print(
                    f"  [Chunk Merging] Note: merge_tiepoints raised '{e_merge}'; merging with camera transforms preserved...",
                    flush=True,
                )
                doc.mergeChunks(chunks=merge_chunk_keys, merge_tiepoints=False)

            doc.chunk = doc.chunks[-1]
            chunk = doc.chunk
            chunk.label = "Production_Reconstruction"
            try:
                chunk.updateTransform()
            except Exception:
                pass
            aligned_total = sum(1 for cam in chunk.cameras if cam.transform and cam.enabled)
            enabled_total = sum(1 for cam in chunk.cameras if cam.enabled)
            print(
                f"  Successfully merged into '{chunk.label}' ({len(chunk.cameras)} cameras, {aligned_total}/{enabled_total} aligned).",
                flush=True,
            )

            if import_reference and os.path.isfile(import_reference):
                print(f"\n[GCP Reference] Importing ground control points from: {import_reference}...", flush=True)
                try:
                    chunk.importReference(path=import_reference, format=Metashape.ReferenceFormat.ReferenceFormatCSV)
                    chunk.updateTransform()
                    print("  Reference import and georeferencing transform updated.", flush=True)
                except Exception as e_ref:
                    print(f"  [WARNING] Reference import issue: {e_ref}. Continuing...", flush=True)

            if detect_markers:
                print(
                    f"\n[Target Detection] Detecting target markers (Type: {target_type}, Tolerance: {marker_tolerance})...",
                    flush=True,
                )
                try:
                    tt_enum = TARGET_TYPE_MAP.get(target_type.lower(), Metashape.TargetType.CircularTarget12bit)
                    chunk.detectMarkers(target_type=tt_enum, tolerance=marker_tolerance)
                    print(f"  Target detection completed! Found {len(chunk.markers)} markers.", flush=True)
                except Exception as e_mk:
                    print(f"  [WARNING] Marker detection issue: {e_mk}. Continuing...", flush=True)

        else:
            all_imgs = []
            for imgs in groups.values():
                all_imgs.extend(imgs)
            all_imgs = sorted(all_imgs)

            emit_ipc_stage("ingest", f"Ingesting {len(all_imgs)} images across: {images_dir}")
            print(f"\n[1/7] Ingesting {len(all_imgs)} images found across: {images_dir}", flush=True)
            chunk.addPhotos(all_imgs)
            print(f"  Successfully loaded {len(chunk.cameras)} cameras.", flush=True)

            if min_quality > 0.0:
                filter_image_quality(chunk, min_quality=min_quality)

            if rolling_shutter:
                for s in chunk.sensors:
                    s.rolling_shutter = Metashape.Shutter.Model.Regularized
                print("  [Rolling Shutter] Enabled Regularized CMOS rolling shutter compensation.", flush=True)

            if import_reference and os.path.isfile(import_reference):
                print(f"\n[GCP Reference] Importing ground control points from: {import_reference}...", flush=True)
                try:
                    chunk.importReference(path=import_reference, format=Metashape.ReferenceFormat.ReferenceFormatCSV)
                    chunk.updateTransform()
                    print("  Reference import and georeferencing transform updated.", flush=True)
                except Exception as e_ref:
                    print(f"  [WARNING] Reference import issue: {e_ref}. Continuing...", flush=True)

            if detect_markers:
                print(
                    f"\n[Target Detection] Detecting target markers (Type: {target_type}, Tolerance: {marker_tolerance})...",
                    flush=True,
                )
                try:
                    tt_enum = TARGET_TYPE_MAP.get(target_type.lower(), Metashape.TargetType.CircularTarget12bit)
                    chunk.detectMarkers(target_type=tt_enum, tolerance=marker_tolerance)
                    print(f"  Target detection completed! Found {len(chunk.markers)} markers.", flush=True)
                except Exception as e_mk:
                    print(f"  [WARNING] Marker detection issue: {e_mk}. Continuing...", flush=True)

        # -------------------------------------------------------------
        # Autonomous Unified Project Batch (Independent Project Mode)
        # -------------------------------------------------------------
        if cluster_enabled and network_independent_workers:
            if pause_after_crop or crop_method == "manual":
                raise ValueError(
                    "[Cluster] Independent worker mode strictly enforces unattended offline execution. "
                    "Manual cropping and pause_after_crop cannot be used when --network-independent-workers is active."
                )

        unified_succeeded = False
        if cluster_enabled and network_independent_workers:
            try:
                print("\n=========================================================================", flush=True)
                print("  AUTOMETASHAPE: AUTONOMOUS UNIFIED CLUSTER BATCH (INDEPENDENT MODE)", flush=True)
                print("  Full Project Pipeline Bundled into 1 Dedicated Worker Batch", flush=True)
                print("  Worker Node Lock: 1 node (Zero Intermediate LAN Syncs, Zero Worker Hopping)", flush=True)
                print("=========================================================================", flush=True)

                doc.save(psx_out)
                rel_psx = get_cluster_rel_path()

                has_depth = False
                if chunk and chunk.depth_maps is not None:
                    try:
                        has_depth = (
                            len(chunk.depth_maps.keys()) > 0
                            if hasattr(chunk.depth_maps, "keys")
                            else bool(chunk.depth_maps)
                        )
                    except Exception:
                        has_depth = bool(chunk.depth_maps)

                has_point_cloud = False
                if chunk and chunk.point_cloud is not None:
                    try:
                        has_point_cloud = getattr(chunk.point_cloud, "point_count", 0) > 0
                    except Exception:
                        has_point_cloud = False

                has_mesh = False
                if chunk and chunk.model is not None:
                    try:
                        has_mesh = len(chunk.model.faces) > 0 if hasattr(chunk.model, "faces") else True
                    except Exception:
                        has_mesh = False

                has_tex = False
                if has_mesh and chunk.model is not None and not rebake_texture:
                    try:
                        if hasattr(chunk.model, "textures"):
                            has_tex = len(chunk.model.textures) == (texture_pages or 16)
                        elif hasattr(chunk.model, "texture") and chunk.model.texture():
                            has_tex = (texture_pages or 16) == 1
                    except Exception:
                        has_tex = False

                has_dem_asset = bool(chunk and getattr(chunk, "elevation", None) is not None)
                has_ortho_asset = bool(chunk and getattr(chunk, "orthomosaic", None) is not None)

                u_align = align_photos and (not use_multi_chunk) and (not resumed)
                u_depth = build_depth_maps and not (resumed and has_depth)
                u_pc = build_point_cloud and not (resumed and has_point_cloud)
                u_mesh = build_mesh and not (resumed and has_mesh)
                u_tex = build_texture and not (resumed and has_tex)
                u_dem = build_dem and not (resumed and has_dem_asset)
                u_ortho = build_orthomosaic and not (resumed and has_ortho_asset)

                if resumed:
                    reused_items = []
                    if not u_align:
                        reused_items.append("Aligned Cameras")
                    if has_depth and not u_depth:
                        reused_items.append("Depth Maps")
                    if has_point_cloud and not u_pc:
                        reused_items.append("Point Cloud")
                    if has_mesh and not u_mesh:
                        reused_items.append("Surface Mesh")
                    if has_tex and not u_tex:
                        reused_items.append("Texture Atlas")
                    if has_dem_asset and not u_dem:
                        reused_items.append("DEM")
                    if has_ortho_asset and not u_ortho:
                        reused_items.append("Orthomosaic")
                    if reused_items:
                        print(
                            f"  [Cluster Checkpoint] Reusing completed assets from project: {', '.join(reused_items)}",
                            flush=True,
                        )

                # Check if all requested reconstruction stages are already satisfied in checkpoint
                if not any([u_align, u_depth, u_mesh, u_tex, u_dem, u_ortho, u_pc]):
                    print(
                        "  [Cluster Checkpoint] All reconstruction stages already complete in checkpoint. Skipping cluster dispatch.",
                        flush=True,
                    )
                    unified_succeeded = True
                else:
                    net_task, rel_script, status_file = build_monolithic_runscript_task(
                        images_dir=images_dir,
                        output_dir=target_network_output or output_dir,
                        project_name=project_name,
                        preset=preset,
                        align_quality=align_quality,
                        depth_quality=depth_quality,
                        depth_filtering=depth_filtering,
                        custom_faces=custom_faces,
                        texture_pages=texture_pages,
                        crop_method=crop_method,
                        texture_format=texture_format,
                        align_photos=u_align,
                        build_depth_maps=u_depth,
                        build_mesh=u_mesh,
                        build_texture=u_tex,
                        build_dem=u_dem,
                        build_orthomosaic=u_ortho,
                        build_point_cloud=u_pc,
                        export_obj=export_obj,
                        export_glb=export_glb,
                        export_fbx=export_fbx,
                        export_ply=export_ply,
                        export_dxf=export_dxf,
                        export_dem=export_dem,
                        export_ortho=export_ortho,
                        export_point_cloud=export_point_cloud,
                        point_cloud_format=point_cloud_format,
                        export_report=export_report,
                        resume=resume,
                        network_path=network_path,
                    )

                    batch_id, was_attached = safe_create_cluster_batch(
                        net_client, rel_psx, [net_task], worker_limit=1, force_fresh=False
                    )
                    if was_attached:
                        print(
                            f"  [Cluster] Attached to active Monolithic Batch ID={batch_id} on dedicated worker node.",
                            flush=True,
                        )
                    else:
                        print(
                            f"  [Cluster] Created Monolithic RunScript Batch ID={batch_id} locked to 1 dedicated worker node.",
                            flush=True,
                        )

                    t_u_start = time.time()
                    last_stage_key = None
                    last_progress = -1.0
                    last_log_time = 0.0

                    while True:
                        time.sleep(1.0)
                        b_info = net_client.batchInfo(batch_id)
                        tasks_info = b_info.get("tasks", [])

                        # Real-time sidecar status file telemetry from dedicated worker
                        if status_file and os.path.exists(status_file):
                            try:
                                with open(status_file, "r", encoding="utf-8") as sf:
                                    s_data = json.load(sf)
                                stage_k = s_data.get("stage_key", "recon")
                                stage_desc = s_data.get("stage_desc", "Reconstructing")
                                sub_prog = float(s_data.get("progress", 0.0))
                                worker_node = s_data.get("worker_node", "Worker Node")

                                if stage_k != last_stage_key:
                                    emit_ipc_stage(stage_k, stage_desc)
                                    print(
                                        f"\n>>> [Stage Transition] Active Stage: {stage_desc} (Node: {worker_node})",
                                        flush=True,
                                    )
                                    last_stage_key = stage_k

                                now_t = time.time()
                                if abs(sub_prog - last_progress) >= 0.5 or (
                                    now_t - last_log_time >= 10.0 and sub_prog != last_progress
                                ):
                                    elapsed_min = (now_t - t_u_start) / 60.0
                                    print(
                                        f"  [Cluster Progress] {stage_desc}: {sub_prog:.1f}% (Worker: {worker_node}, Elapsed: {elapsed_min:.1f} min)",
                                        flush=True,
                                    )
                                    last_progress = sub_prog
                                    last_log_time = now_t
                            except Exception:
                                pass
                        elif tasks_info:
                            active_task = next(
                                (t for t in tasks_info if t.get("state") in ("running", "inprogress", "submitted")),
                                tasks_info[-1],
                            )
                            t_sub_name = active_task.get("name", "RunScript")
                            sub_prog = active_task.get("progress", 0.0)

                            now_t = time.time()
                            if abs(sub_prog - last_progress) >= 0.5 or (
                                now_t - last_log_time >= 10.0 and sub_prog != last_progress
                            ):
                                elapsed_min = (now_t - t_u_start) / 60.0
                                print(
                                    f"  [Cluster Progress] Dedicated Worker Processing - {t_sub_name}: {sub_prog:.1f}% (Elapsed: {elapsed_min:.1f} min)",
                                    flush=True,
                                )
                                last_progress = sub_prog
                                last_log_time = now_t

                        b_status, b_success, b_err = evaluate_cluster_batch_info(b_info)
                        if b_success:
                            elapsed_min = (time.time() - t_u_start) / 60.0
                            print(
                                f"\n  [Cluster] [OK] Entire unified project completed on dedicated worker in {elapsed_min:.2f} min!",
                                flush=True,
                            )
                            unified_succeeded = True
                            break
                        elif b_status in ("failed", "aborted"):
                            err_str = b_err or "unknown cluster error"
                            print(f"  [Cluster] [ERROR] Unified batch {b_status}: {err_str}", flush=True)

                            failed_worker_id = None
                            for t in tasks_info:
                                if t.get("errors") and t.get("worker_id") is not None:
                                    failed_worker_id = t.get("worker_id")
                                    break
                            if failed_worker_id is None and tasks_info:
                                failed_worker_id = tasks_info[0].get("worker_id")

                            emit_ipc_event(f"@@AUTOMETASHAPE_UNIFIED_FAIL:{batch_id}:{failed_worker_id or 'unknown'}@@")

                            # Tier 1 Handoff / Resume: Re-dispatch RunScript task with resume=True to another cluster node
                            try:
                                if failed_worker_id is not None:
                                    try:
                                        net_client.setWorkerPaused(failed_worker_id, True)
                                    except Exception:
                                        pass

                                raw_w = net_client.workerList()
                                w_list = (
                                    raw_w.get("workers", [])
                                    if isinstance(raw_w, dict)
                                    else (raw_w if isinstance(raw_w, (list, tuple)) else [])
                                )
                                other_workers = [
                                    w
                                    for w in w_list
                                    if (w.get("worker_id") if isinstance(w, dict) else getattr(w, "id", None))
                                    != failed_worker_id
                                    and not (
                                        w.get("paused", False) if isinstance(w, dict) else getattr(w, "paused", False)
                                    )
                                ]

                                if other_workers:
                                    handoff_net_task, _, _ = build_monolithic_runscript_task(
                                        images_dir=images_dir,
                                        output_dir=target_network_output or output_dir,
                                        project_name=project_name,
                                        preset=preset,
                                        align_quality=align_quality,
                                        depth_quality=depth_quality,
                                        depth_filtering=depth_filtering,
                                        custom_faces=custom_faces,
                                        texture_pages=texture_pages,
                                        crop_method=crop_method,
                                        texture_format=texture_format,
                                        align_photos=u_align,
                                        build_depth_maps=u_depth,
                                        build_mesh=u_mesh,
                                        build_texture=u_tex,
                                        build_dem=u_dem,
                                        build_orthomosaic=u_ortho,
                                        build_point_cloud=u_pc,
                                        export_obj=export_obj,
                                        export_glb=export_glb,
                                        export_fbx=export_fbx,
                                        export_ply=export_ply,
                                        export_dxf=export_dxf,
                                        export_dem=export_dem,
                                        export_ortho=export_ortho,
                                        export_point_cloud=export_point_cloud,
                                        point_cloud_format=point_cloud_format,
                                        export_report=export_report,
                                        resume=True,
                                        network_path=network_path,
                                    )
                                    handoff_batch_id, _ = safe_create_cluster_batch(
                                        net_client, rel_psx, [handoff_net_task], worker_limit=1, force_fresh=True
                                    )
                                    print(
                                        f"  [Cluster] Created Handoff Continuation Monolithic Batch ID={handoff_batch_id} for another worker.",
                                        flush=True,
                                    )

                                    if failed_worker_id is not None:
                                        try:
                                            net_client.setWorkerPaused(failed_worker_id, False)
                                            print(
                                                f"  [Cluster] Worker {failed_worker_id} unpaused and freed to process the next project in the queue.",
                                                flush=True,
                                            )
                                        except Exception:
                                            pass

                                    h_start = time.time()
                                    h_last_stage = None
                                    h_last_prog = -1.0
                                    h_last_log = 0.0

                                    handoff_ok = False
                                    while True:
                                        time.sleep(1.0)
                                        hb_info = net_client.batchInfo(handoff_batch_id)
                                        hb_tasks = hb_info.get("tasks", [])

                                        if status_file and os.path.exists(status_file):
                                            try:
                                                with open(status_file, "r", encoding="utf-8") as sf:
                                                    hs_data = json.load(sf)
                                                hk_stage = hs_data.get("stage_key", "recon")
                                                hk_desc = hs_data.get("stage_desc", "Handoff Reconstructing")
                                                h_prog = float(hs_data.get("progress", 0.0))
                                                h_node = hs_data.get("worker_node", "Second Worker")
                                                if hk_stage != h_last_stage:
                                                    emit_ipc_stage(hk_stage, hk_desc)
                                                    print(
                                                        f"\n>>> [Handoff Stage] Active on Second Worker: {hk_desc} (Node: {h_node})",
                                                        flush=True,
                                                    )
                                                    h_last_stage = hk_stage

                                                h_now = time.time()
                                                if abs(h_prog - h_last_prog) >= 0.5 or (
                                                    h_now - h_last_log >= 10.0 and h_prog != h_last_prog
                                                ):
                                                    h_elapsed = (h_now - h_start) / 60.0
                                                    print(
                                                        f"  [Handoff Progress] {hk_desc}: {h_prog:.1f}% (Worker: {h_node}, Elapsed: {h_elapsed:.1f} min)",
                                                        flush=True,
                                                    )
                                                    h_last_prog = h_prog
                                                    h_last_log = h_now
                                            except Exception:
                                                pass
                                        elif hb_tasks:
                                            h_active = next(
                                                (
                                                    t
                                                    for t in hb_tasks
                                                    if t.get("state") in ("running", "inprogress", "submitted")
                                                ),
                                                hb_tasks[-1],
                                            )
                                            h_name = h_active.get("name", "RunScript")
                                            h_prog = h_active.get("progress", 0.0)
                                            h_now = time.time()
                                            if abs(h_prog - h_last_prog) >= 0.5 or (
                                                h_now - h_last_log >= 10.0 and h_prog != h_last_prog
                                            ):
                                                h_elapsed = (h_now - h_start) / 60.0
                                                print(
                                                    f"  [Handoff Progress] {h_name}: {h_prog:.1f}% (Elapsed: {h_elapsed:.1f} min)",
                                                    flush=True,
                                                )
                                                h_last_prog = h_prog
                                                h_last_log = h_now

                                        hb_status, hb_success, hb_err = evaluate_cluster_batch_info(hb_info)
                                        if hb_success:
                                            h_elapsed = (time.time() - h_start) / 60.0
                                            print(
                                                f"\n  [Cluster] [OK] Remaining tasks successfully completed on second cluster worker in {h_elapsed:.2f} min!",
                                                flush=True,
                                            )
                                            handoff_ok = True
                                            unified_succeeded = True
                                            break
                                        elif hb_status in ("failed", "aborted"):
                                            print(
                                                f"  [Cluster] [ERROR] Handoff batch {hb_status} on second worker: {hb_err}",
                                                flush=True,
                                            )
                                            break

                                    if handoff_ok:
                                        break
                                    else:
                                        # Tier 2: Repeated failure! Try restarting from scratch on a clean worker node
                                        print(
                                            "\n  [Cluster Recovery] Repeated failure: Handoff failed on secondary worker.",
                                            flush=True,
                                        )
                                        print(
                                            "  [Cluster Recovery] Attempting complete project restart from scratch on a clean worker node...",
                                            flush=True,
                                        )
                                        emit_ipc_event(f"@@AUTOMETASHAPE_RESTART_SCRATCH:{project_name}@@")
                                        try:
                                            files_dir = get_psx_files_dir(psx_out)
                                            if os.path.isdir(files_dir):
                                                try:
                                                    shutil.rmtree(files_dir, ignore_errors=True)
                                                except Exception:
                                                    pass
                                            if os.path.exists(psx_out):
                                                try:
                                                    os.remove(psx_out)
                                                except Exception:
                                                    pass

                                            scratch_net_task, _, _ = build_monolithic_runscript_task(
                                                images_dir=images_dir,
                                                output_dir=target_network_output or output_dir,
                                                project_name=project_name,
                                                preset=preset,
                                                align_quality=align_quality,
                                                depth_quality=depth_quality,
                                                depth_filtering=depth_filtering,
                                                custom_faces=custom_faces,
                                                texture_pages=texture_pages,
                                                crop_method=crop_method,
                                                texture_format=texture_format,
                                                align_photos=True,
                                                build_depth_maps=build_depth_maps,
                                                build_mesh=build_mesh,
                                                build_texture=build_texture,
                                                build_dem=build_dem,
                                                build_orthomosaic=build_orthomosaic,
                                                build_point_cloud=build_point_cloud,
                                                export_obj=export_obj,
                                                export_glb=export_glb,
                                                export_fbx=export_fbx,
                                                export_ply=export_ply,
                                                export_dxf=export_dxf,
                                                export_dem=export_dem,
                                                export_ortho=export_ortho,
                                                export_point_cloud=export_point_cloud,
                                                point_cloud_format=point_cloud_format,
                                                export_report=export_report,
                                                resume=False,
                                                network_path=network_path,
                                            )
                                            scratch_batch_id, _ = safe_create_cluster_batch(
                                                net_client,
                                                rel_psx,
                                                [scratch_net_task],
                                                worker_limit=1,
                                                force_fresh=True,
                                            )
                                            print(
                                                f"  [Cluster] Created Fresh Scratch Monolithic Batch ID={scratch_batch_id} on clean worker node.",
                                                flush=True,
                                            )

                                            sc_start = time.time()
                                            sc_last_stage = None
                                            sc_last_prog = -1.0
                                            sc_last_log = 0.0

                                            while True:
                                                time.sleep(1.0)
                                                sc_info = net_client.batchInfo(scratch_batch_id)
                                                sc_tasks = sc_info.get("tasks", [])

                                                if status_file and os.path.exists(status_file):
                                                    try:
                                                        with open(status_file, "r", encoding="utf-8") as sf:
                                                            scs_data = json.load(sf)
                                                        sck_stage = scs_data.get("stage_key", "recon")
                                                        sck_desc = scs_data.get("stage_desc", "Scratch Reconstructing")
                                                        sc_prog = float(scs_data.get("progress", 0.0))
                                                        sc_node = scs_data.get("worker_node", "Scratch Worker")
                                                        if sck_stage != sc_last_stage:
                                                            emit_ipc_stage(sck_stage, sck_desc)
                                                            print(
                                                                f"\n>>> [Scratch Stage] Active: {sck_desc} (Node: {sc_node})",
                                                                flush=True,
                                                            )
                                                            sc_last_stage = sck_stage

                                                        sc_now = time.time()
                                                        if abs(sc_prog - sc_last_prog) >= 0.5 or (
                                                            sc_now - sc_last_log >= 10.0 and sc_prog != sc_last_prog
                                                        ):
                                                            sc_elapsed = (sc_now - sc_start) / 60.0
                                                            print(
                                                                f"  [Scratch Progress] {sck_desc}: {sc_prog:.1f}% (Worker: {sc_node}, Elapsed: {sc_elapsed:.1f} min)",
                                                                flush=True,
                                                            )
                                                            sc_last_prog = sc_prog
                                                            sc_last_log = sc_now
                                                    except Exception:
                                                        pass
                                                elif sc_tasks:
                                                    sc_active = next(
                                                        (
                                                            t
                                                            for t in sc_tasks
                                                            if t.get("state") in ("running", "inprogress", "submitted")
                                                        ),
                                                        sc_tasks[-1],
                                                    )
                                                    sc_name = sc_active.get("name", "RunScript")
                                                    sc_prog = sc_active.get("progress", 0.0)
                                                    sc_now = time.time()
                                                    if abs(sc_prog - sc_last_prog) >= 0.5 or (
                                                        sc_now - sc_last_log >= 10.0 and sc_prog != sc_last_prog
                                                    ):
                                                        sc_elapsed = (sc_now - sc_start) / 60.0
                                                        print(
                                                            f"  [Scratch Progress] {sc_name}: {sc_prog:.1f}% (Elapsed: {sc_elapsed:.1f} min)",
                                                            flush=True,
                                                        )
                                                        sc_last_prog = sc_prog
                                                        sc_last_log = sc_now

                                                sc_status, sc_success, sc_err = evaluate_cluster_batch_info(sc_info)
                                                if sc_success:
                                                    sc_elapsed = (time.time() - sc_start) / 60.0
                                                    print(
                                                        f"\n  [Cluster] [OK] Fresh scratch reconstruction completed successfully in {sc_elapsed:.2f} min!",
                                                        flush=True,
                                                    )
                                                    unified_succeeded = True
                                                    break
                                                elif sc_status in ("failed", "aborted"):
                                                    print(
                                                        f"  [Cluster] [ERROR] Fresh scratch batch {sc_status}: {sc_err}",
                                                        flush=True,
                                                    )
                                                    break
                                        except Exception as e_scratch:
                                            print(
                                                f"  [Cluster Recovery] Scratch restart failed: {e_scratch}", flush=True
                                            )
                                            break
                            except Exception as e_failover:
                                print(f"  [Cluster] [ERROR] Failed to execute failover: {e_failover}", flush=True)
                            break

                if unified_succeeded:
                    _safe_reopen()
                    chunk = doc.chunk
                    total_time = (time.time() - t_start) / 60.0
                    print(f"\n[SUCCESS] Entire pipeline finished in {total_time:.2f} minutes!", flush=True)
                    emit_ipc_event(IPC_COMPLETE)
                    if survey_physics and pro_plan:
                        print_pro_engineering_report(survey_physics, pro_plan, usgs_stats=usgs_stats)
                    return
            except Exception as e_unified:
                print(
                    f"  [Cluster] [ERROR] Unified offline batch failed: {e_unified}",
                    flush=True,
                )
                unified_succeeded = False

        if cluster_enabled and network_independent_workers:
            raise RuntimeError(
                f"[Cluster] Independent worker offline batch execution failed for project '{project_name}'. "
                "The offline monolithic process is the only method permitted for independent workers; "
                "fallback to stage-by-stage network processing is strictly disabled."
            )

        # Fallback Stage-by-Stage Feature Matching & Camera Alignment (Single-Chunk Mode)
        if not use_multi_chunk:
            if not align_photos:
                print("\n[INFO] Photo feature matching and camera alignment skipped by workflow selection.", flush=True)
            else:
                emit_ipc_stage("match", "Executing GPU Feature Matching")
                print(
                    f"\n[2/7] Executing GPU Feature Matching (Downscale: {align_downscale}, Keypoints: {keypoint_limit:,}, Tiepoints: {tiepoint_limit:,})...",
                    flush=True,
                )
                t_match = time.time()
                matched = False
                if cluster_enabled:
                    try:
                        t_m = Metashape.Tasks.MatchPhotos()
                        t_m.downscale = align_downscale
                        t_m.generic_preselection = True
                        t_m.reference_preselection = True
                        t_m.reference_preselection_mode = presel_mode_enum
                        t_m.keypoint_limit = keypoint_limit
                        t_m.keypoint_limit_per_mpx = keypoint_limit_per_mpx
                        t_m.tiepoint_limit = tiepoint_limit
                        t_m.mask_tiepoints = False
                        t_m.filter_stationary_points = filter_stationary_points
                        t_m.guided_matching = guided_matching
                        matched = dispatch_cluster_task(t_m, "GPU Feature Matching", "match")
                        if matched:
                            chunk = doc.chunk
                    except Exception as e_m_dispatch:
                        print(
                            f"  [Cluster] MatchPhotos dispatch exception: {e_m_dispatch}. Running locally...",
                            flush=True,
                        )
                        matched = False

                if not matched:
                    chunk.matchPhotos(
                        downscale=align_downscale,
                        generic_preselection=True,
                        reference_preselection=True,
                        reference_preselection_mode=presel_mode_enum,
                        keypoint_limit=keypoint_limit,
                        keypoint_limit_per_mpx=keypoint_limit_per_mpx,
                        tiepoint_limit=tiepoint_limit,
                        mask_tiepoints=False,
                        filter_stationary_points=filter_stationary_points,
                        guided_matching=guided_matching,
                    )
                print(f"  Matching finished in {(time.time() - t_match) / 60:.2f} min.", flush=True)

                emit_ipc_stage("align", "Camera Alignment & Bundle Adjustment")
                print(
                    f"\n[3/7] Aligning Cameras & Initial Bundle Adjustment (Adaptive Fitting: {adaptive_fitting})...",
                    flush=True,
                )
                t_align = time.time()
                aligned = False
                if cluster_enabled:
                    try:
                        t_a = Metashape.Tasks.AlignCameras()
                        t_a.adaptive_fitting = adaptive_fitting
                        aligned = dispatch_cluster_task(t_a, "Camera Alignment & Bundle Adjustment", "align")
                        if aligned:
                            chunk = doc.chunk
                    except Exception as e_a_dispatch:
                        print(
                            f"  [Cluster] AlignCameras dispatch exception: {e_a_dispatch}. Running locally...",
                            flush=True,
                        )
                        aligned = False

                if not aligned:
                    chunk.alignCameras(adaptive_fitting=adaptive_fitting)

                aligned_count = sum(1 for c in chunk.cameras if c.transform and c.enabled)
                enabled_count = sum(1 for c in chunk.cameras if c.enabled)
                print(
                    f"  Alignment finished in {(time.time() - t_align) / 60:.2f} min. Aligned: {aligned_count}/{enabled_count} active cameras.",
                    flush=True,
                )

                if rolling_shutter:
                    for s in chunk.sensors:
                        s.rolling_shutter = Metashape.Shutter.Model.Regularized
                    print("  [Rolling Shutter] Enabled Regularized CMOS rolling shutter compensation.", flush=True)

                if filter_tiepoints:
                    cleaned_on_cluster = False
                    if cluster_enabled:
                        try:
                            clean_tasks = build_cluster_gradual_selection_tasks()
                            cleaned_on_cluster = dispatch_cluster_task(
                                clean_tasks,
                                "Gradual Selection Tie-Point Filtering & Camera Optimization (4 passes)",
                                "align",
                            )
                            if cleaned_on_cluster:
                                chunk = doc.chunk
                        except Exception as e_cl_clean:
                            print(
                                f"  [Cluster] Tie-point clean dispatch exception: {e_cl_clean}. Running locally...",
                                flush=True,
                            )
                            cleaned_on_cluster = False

                    if not cleaned_on_cluster:
                        usgs_stats = filter_tie_points(chunk)
                else:
                    print(
                        "  [Gradual Selection] Tie-point cleaning and camera re-optimization skipped (--no-filter-tiepoints).",
                        flush=True,
                    )

        if crop_method == "manual":
            print("\n[3b/7] Manual Bounding Box Crop selected: Auto-crop skipped.", flush=True)
            chunk = save_checkpoint(doc, psx_out)
            print(f"[CHECKPOINT] Saved alignment checkpoint to: {psx_out}", flush=True)
            wait_for_user_resume(output_dir, psx_out)
            _safe_reopen()
            chunk = doc.chunk
        elif crop_method in ("gps-square", "structure"):
            cropped_on_cluster = False
            if cluster_enabled and not pause_after_crop:
                try:
                    t_crop = build_cluster_crop_task(crop_method, padding=crop_padding)
                    if t_crop:
                        cropped_on_cluster = dispatch_cluster_task(
                            t_crop,
                            f"Worker Auto-Crop Bounding Box ({crop_method})",
                            "align",
                        )
                        if cropped_on_cluster:
                            chunk = doc.chunk
                except Exception as e_cl_cr:
                    print(f"  [Cluster] Crop dispatch exception: {e_cl_cr}. Running locally...", flush=True)
                    cropped_on_cluster = False

            if not cropped_on_cluster:
                if crop_method == "gps-square":
                    auto_crop_camera_gps_square(chunk, padding=crop_padding)
                else:
                    auto_crop_to_main_structure(chunk, padding=crop_padding)
                chunk = save_checkpoint(doc, psx_out)
                print(f"[CHECKPOINT] Saved alignment checkpoint to: {psx_out}", flush=True)
                if pause_after_crop:
                    wait_for_user_resume(output_dir, psx_out)
                    _safe_reopen()
                    chunk = doc.chunk
        else:
            print("  [Auto-Crop] Cropping disabled; using default full-scene bounding box.", flush=True)
            chunk = save_checkpoint(doc, psx_out)
            print(f"[CHECKPOINT] Saved alignment checkpoint to: {psx_out}", flush=True)
            if pause_after_crop:
                wait_for_user_resume(output_dir, psx_out)
                _safe_reopen()
                chunk = doc.chunk

    # Step 4: Multi-View Stereo Dense Depth Maps
    if not build_depth_maps:
        print("\n[INFO] GPU MVS Depth Maps generation skipped by workflow selection.", flush=True)
    else:
        has_depth_maps = False
        depth_count = 0
        if chunk.depth_maps is not None:
            try:
                depth_count = len(chunk.depth_maps.keys()) if hasattr(chunk.depth_maps, "keys") else 0
                has_depth_maps = depth_count > 0
            except Exception:
                has_depth_maps = bool(chunk.depth_maps)
                try:
                    depth_count = len(chunk.depth_maps.keys())
                except Exception:
                    depth_count = 0

        if has_depth_maps:
            emit_ipc_stage("depth", f"Reusing existing MVS Depth Maps ({depth_count} depth maps)")
            print(f"\n[4/7] Reusing existing MVS Depth Maps from checkpoint ({depth_count} depth maps).", flush=True)
        else:
            depth_downscale = DEPTH_DOWNSCALE_MAP.get(depth_quality.lower(), 4)
            filter_mode_arg = DEPTH_FILTER_MAP.get(depth_filtering.lower(), Metashape.FilterMode.ModerateFiltering)
            emit_ipc_stage("depth", f"Computing GPU MVS Depth Maps (Quality: {depth_quality.capitalize()})")
            print(
                f"\n[4/7] Computing GPU MVS Depth Maps (Quality: {depth_quality.capitalize()} [Downscale={depth_downscale}], Filtering: {depth_filtering.capitalize()})...",
                flush=True,
            )
            t_depth = time.time()
            depth_computed = False
            if cluster_enabled:
                try:
                    t_d = Metashape.Tasks.BuildDepthMaps()
                    t_d.downscale = depth_downscale
                    t_d.filter_mode = filter_mode_arg
                    t_d.reuse_depth = False
                    depth_computed = dispatch_cluster_task(
                        t_d, f"Computing GPU MVS Depth Maps ({depth_quality.capitalize()})", "depth"
                    )
                    if depth_computed:
                        chunk = doc.chunk
                except Exception as e_d_dispatch:
                    print(
                        f"  [Cluster] BuildDepthMaps dispatch exception: {e_d_dispatch}. Running locally...", flush=True
                    )
                    depth_computed = False

            if not depth_computed:
                chunk.buildDepthMaps(
                    downscale=depth_downscale,
                    filter_mode=filter_mode_arg,
                    reuse_depth=False,
                )
            print(f"  Depth maps completed in {(time.time() - t_depth) / 60:.2f} min.", flush=True)
            chunk = save_checkpoint(doc, psx_out)
            print(f"[CHECKPOINT] Saved depth maps checkpoint to: {psx_out}", flush=True)

    # Step 4b: Dense Point Cloud Generation & Classification
    if build_point_cloud:
        has_point_cloud = _has_dense_points(chunk)
        if has_point_cloud:
            emit_ipc_stage(
                "point_cloud", f"Reusing existing Dense Point Cloud ({chunk.point_cloud.point_count:,} points)"
            )
            print(
                f"\n[4b/7] Reusing existing Dense Point Cloud from checkpoint ({chunk.point_cloud.point_count:,} points).",
                flush=True,
            )
        else:
            emit_ipc_stage("point_cloud", "Generating Dense Point Cloud from Depth Maps")
            print("\n[4b/7] Generating Dense Point Cloud from Depth Maps (RGB Colors + Confidence)...", flush=True)
            t_pc = time.time()
            pc_computed = False
            if cluster_enabled:
                try:
                    t_p = Metashape.Tasks.BuildPointCloud()
                    t_p.source_data = Metashape.DataSource.DepthMapsData
                    t_p.point_colors = True
                    t_p.point_confidence = True
                    t_p.uniform_sampling = True
                    t_p.points_spacing = points_spacing
                    t_p.keep_depth = True
                    pc_computed = dispatch_cluster_task(
                        t_p, "Generating Dense Point Cloud from Depth Maps", "point_cloud"
                    )
                    if pc_computed:
                        chunk = doc.chunk
                except Exception as e_pc_disp:
                    print(
                        f"  [Cluster] BuildPointCloud dispatch exception: {e_pc_disp}. Running locally...", flush=True
                    )
                    pc_computed = False

            if not pc_computed:
                chunk.buildPointCloud(
                    source_data=Metashape.DataSource.DepthMapsData,
                    point_colors=True,
                    point_confidence=True,
                    uniform_sampling=True,
                    points_spacing=points_spacing,
                    keep_depth=True,
                )
            pc_count = (
                chunk.point_cloud.point_count
                if (chunk.point_cloud and isinstance(getattr(chunk.point_cloud, "point_count", None), int))
                else 0
            )
            print(
                f"  Point cloud completed in {(time.time() - t_pc) / 60:.2f} min! Density: {pc_count:,} 3D points.",
                flush=True,
            )

            if filter_point_confidence and chunk.point_cloud:
                print(f"  Filtering low-confidence points (< {filter_point_confidence})...", flush=True)
                chunk.point_cloud.setConfidenceFilter(filter_point_confidence, 255)
                chunk.point_cloud.removeSelectedPoints()
                chunk.point_cloud.resetFilters()

            if classify_ground and chunk.point_cloud:
                c_size = ground_cell_size if ground_cell_size is not None else 50.0
                m_ang = ground_max_angle if ground_max_angle is not None else 15.0
                m_dist = ground_max_distance if ground_max_distance is not None else 1.0
                print(
                    f"  Executing slope-adaptive ground point classification (cell_size={c_size:.1f}m, max_angle={m_ang:.1f} deg, max_distance={m_dist:.2f}m)...",
                    flush=True,
                )
                chunk.point_cloud.classifyGroundPoints(cell_size=c_size, max_angle=m_ang, max_distance=m_dist)
                print("  Ground classification completed.", flush=True)

            if classify_points and chunk.point_cloud:
                print("  Executing multiclass AI semantic point classification...", flush=True)
                chunk.point_cloud.classifyPoints()
                print("  Multiclass point classification completed.", flush=True)

            chunk = save_checkpoint(doc, psx_out)
            print(f"[CHECKPOINT] Saved dense point cloud checkpoint to: {psx_out}", flush=True)

    # Step 5: High-Precision Arbitrary Surface Meshing
    if not build_mesh:
        print("\n[INFO] 3D Surface Meshing skipped by workflow selection.", flush=True)
    else:
        chunk = doc.chunk
        if chunk.model is None:
            cand_obj = os.path.join(output_dir, f"{project_name}.obj")
            cand_obj_legacy = os.path.join(output_dir, "model.obj")
            target_obj = (
                cand_obj if os.path.isfile(cand_obj) else (cand_obj_legacy if os.path.isfile(cand_obj_legacy) else None)
            )
            if target_obj:
                print(f"\n[Auto-Recover] Found existing 3D mesh model on disk: {target_obj}. Re-linking...", flush=True)
                chunk.importModel(path=target_obj, format=Metashape.ModelFormat.ModelFormatOBJ)
                chunk = save_checkpoint(doc, psx_out)
                print(
                    f"  Successfully re-linked 3D model into project chunk ({len(chunk.model.vertices):,} vertices, {len(chunk.model.faces):,} faces).",
                    flush=True,
                )

        has_model = chunk.model is not None
        if has_model:
            emit_ipc_stage("mesh", f"Reusing existing Surface Mesh ({len(chunk.model.vertices):,} vertices)")
            print(
                f"\n[5/7] Reusing existing Surface Mesh from checkpoint ({len(chunk.model.vertices):,} vertices, {len(chunk.model.faces):,} faces).",
                flush=True,
            )
        else:
            emit_ipc_stage("mesh", f"Reconstructing Arbitrary Surface Mesh (Target: {custom_faces:,} faces)")
            print(
                f"\n[5/7] Reconstructing Arbitrary Surface Mesh from Depth Maps (Target: {custom_faces:,} faces)...",
                flush=True,
            )
            t_mesh = time.time()
            if custom_faces > 0:
                face_count_arg = Metashape.FaceCount.CustomFaceCount
                face_count_custom_arg = custom_faces
                print(f"  Target Polygon Count: {custom_faces:,} faces", flush=True)
            else:
                face_count_arg = Metashape.FaceCount.MediumFaceCount
                face_count_custom_arg = 200000
                print("  Target Polygon Count: MediumFaceCount preset", flush=True)

            surf_enum = SURFACE_TYPE_MAP.get(surface_type.lower(), Metashape.SurfaceType.Arbitrary)
            mesh_computed = False
            if cluster_enabled:
                try:
                    t_m = Metashape.Tasks.BuildModel()
                    t_m.surface_type = surf_enum
                    t_m.source_data = Metashape.DataSource.DepthMapsData
                    t_m.interpolation = Metashape.Interpolation.EnabledInterpolation
                    t_m.face_count = face_count_arg
                    t_m.face_count_custom = face_count_custom_arg
                    t_m.vertex_colors = True
                    t_m.vertex_confidence = True
                    t_m.replace_asset = True
                    mesh_computed = dispatch_cluster_task(t_m, f"Surface Mesh ({custom_faces:,} faces)", "mesh")
                    if mesh_computed:
                        chunk = doc.chunk
                except Exception as e_m_disp:
                    print(f"  [Cluster] BuildModel dispatch exception: {e_m_disp}. Running locally...", flush=True)
                    mesh_computed = False

            if not mesh_computed:
                try:
                    chunk.buildModel(
                        surface_type=surf_enum,
                        source_data=Metashape.DataSource.DepthMapsData,
                        interpolation=Metashape.Interpolation.EnabledInterpolation,
                        face_count=face_count_arg,
                        face_count_custom=face_count_custom_arg,
                        vertex_colors=True,
                        vertex_confidence=True,
                    )
                except Exception as e:
                    print(f"\n[WARNING] GPU meshing encountered driver exception: {e}", flush=True)
                    print("  [Fallback] Retrying surface meshing on CPU (all threads)...", flush=True)
                    orig_gpu_mask = Metashape.app.gpu_mask
                    try:
                        Metashape.app.gpu_mask = 0
                        chunk.buildModel(
                            surface_type=surf_enum,
                            source_data=Metashape.DataSource.DepthMapsData,
                            interpolation=Metashape.Interpolation.EnabledInterpolation,
                            face_count=face_count_arg,
                            face_count_custom=face_count_custom_arg,
                            vertex_colors=True,
                        )
                        print("  [Fallback] CPU surface meshing succeeded!", flush=True)
                    finally:
                        Metashape.app.gpu_mask = orig_gpu_mask
                        print("  [Hardware] Restored GPU acceleration for texture baking.", flush=True)
            if close_holes and chunk.model:
                chunk.model.closeHoles(level=30)
                print("  Applied planar hole closing across window and surface voids (level 30%).", flush=True)
            if smooth_mesh and chunk.model:
                print(f"  Applying Laplacian mesh smoothing (strength: {smooth_strength})...", flush=True)
                chunk.smoothModel(strength=smooth_strength, fix_borders=True, preserve_edges=True)
            if decimate_mesh and chunk.model and decimate_mesh > 0:
                print(f"  Decimating surface mesh to {decimate_mesh:,} faces...", flush=True)
                chunk.decimateModel(face_count=decimate_mesh)

            mesh_verts = len(chunk.model.vertices)
            mesh_faces = len(chunk.model.faces)
            print(
                f"  Mesh completed in {(time.time() - t_mesh) / 60:.2f} min! Geometry: {mesh_verts:,} vertices, {mesh_faces:,} faces.",
                flush=True,
            )
            chunk = save_checkpoint(doc, psx_out)
            print(f"[CHECKPOINT] Saved mesh model checkpoint to: {psx_out}", flush=True)

    # Step 6: Multi-Page 8K Photogrammetric Texture Atlas
    if not build_texture:
        print("\n[INFO] Texture Atlas generation skipped by workflow selection.", flush=True)
    current_pages = 0
    if chunk.model:
        if hasattr(chunk.model, "textures") and chunk.model.textures:
            current_pages = len(chunk.model.textures)
        elif hasattr(chunk.model, "texture") and chunk.model.texture():
            current_pages = 1

    if not build_texture or chunk.model is None:
        pass
    elif current_pages == texture_pages and not rebake_texture:
        emit_ipc_stage("texture", f"Reusing existing Texture Atlas ({current_pages} pages)")
        print(f"\n[6/7] Reusing existing Texture Atlas from checkpoint ({current_pages} pages).", flush=True)
    else:
        if current_pages > 0 and current_pages != texture_pages:
            emit_ipc_stage("texture", f"Re-baking Texture Atlas to requested {texture_pages} pages")
            print(
                f"\n[6/7] Existing texture atlas has {current_pages} page(s), re-baking to requested {texture_pages} pages...",
                flush=True,
            )
        elif rebake_texture:
            emit_ipc_stage("texture", f"Force re-baking Texture Atlas ({texture_pages} pages)")
            print(f"\n[6/7] Force re-baking texture atlas as requested ({texture_pages} pages)...", flush=True)
        else:
            emit_ipc_stage("texture", f"Baking {texture_pages}x 8192x8192 (8K) Texture Atlas")
            print(
                f"\n[6/7] Baking {texture_pages}x 8192x8192 (8K) Photogrammetric Texture Atlas (Mosaic Blending, Hole Filling: {fill_holes}, Ghosting Filter: {ghosting_filter})...",
                flush=True,
            )

        if calibrate_colors:
            print("\n[Color Calibration] Executing radiometric color & white balance calibration...", flush=True)
            try:
                chunk.calibrateColors(source_data=Metashape.DataSource.ModelData, white_balance=True)
                print("  Radiometric color calibration completed.", flush=True)
            except Exception as e_col:
                print(f"  [WARNING] Color calibration issue: {e_col}. Continuing...", flush=True)

        # Deterministic texture atlas allocation (identical across all projects and cluster nodes)
        eff_pages = texture_pages or 16
        print(f"  [Texture] Baking {eff_pages}x 8K texture atlas (deterministic parity across all nodes).", flush=True)

        t_tex = time.time()
        tex_computed = False
        if cluster_enabled:
            try:
                t_uv = Metashape.Tasks.BuildUV()
                t_uv.mapping_mode = Metashape.MappingMode.GenericMapping
                t_uv.page_count = eff_pages or 4
                t_uv.texture_size = 8192

                t_t = Metashape.Tasks.BuildTexture()
                t_t.blending_mode = Metashape.BlendingMode.MosaicBlending
                t_t.texture_size = 8192
                t_t.fill_holes = fill_holes
                t_t.ghosting_filter = ghosting_filter

                tex_computed = dispatch_cluster_task(
                    [t_uv, t_t], f"Baking {eff_pages or 16}x 8K Texture Atlas", "texture"
                )
                if tex_computed:
                    chunk = doc.chunk
            except Exception as e_tx_disp:
                print(
                    f"  [Cluster] BuildUV/BuildTexture dispatch exception: {e_tx_disp}. Running locally...", flush=True
                )
                tex_computed = False

        if not tex_computed:
            try:
                chunk.buildUV(
                    mapping_mode=Metashape.MappingMode.GenericMapping,
                    page_count=eff_pages or 16,
                    texture_size=8192,
                )
                chunk.buildTexture(
                    blending_mode=Metashape.BlendingMode.MosaicBlending,
                    texture_size=8192,
                    fill_holes=fill_holes,
                    ghosting_filter=ghosting_filter,
                )
            except Exception as e_tex_bake:
                print(f"  [Texture] Texture baking exception: {e_tex_bake}", flush=True)
                raise e_tex_bake
        print(f"  Texture baking finished in {(time.time() - t_tex) / 60:.2f} min.", flush=True)

        if remove_lighting and chunk.model:
            print(
                "\n[Delighting] Removing harsh ambient shadows and illumination to recover diffuse albedo...",
                flush=True,
            )
            try:
                chunk.removeLighting(color_mode=delighting_color_mode)
                print("  Texture delighting completed.", flush=True)
            except Exception as e_lt:
                print(f"  [WARNING] Texture delighting issue: {e_lt}. Continuing...", flush=True)
        chunk = save_checkpoint(doc, psx_out)
        print(f"[CHECKPOINT] Saved textured model checkpoint to: {psx_out}", flush=True)

    # Step 6b: Hierarchical 3D Tiled Model (LOD / Cesium 3D Tiles)
    if build_tiled_model:
        try:
            has_tiled = getattr(chunk, "tiled_model", None) is not None
            if not has_tiled:
                emit_ipc_stage("tiled_model", f"Building Hierarchical 3D Tiled Model (Tile Size: {tile_size})")
                print(f"\n[6b/7] Building Hierarchical 3D Tiled Model (Tile Size: {tile_size})...", flush=True)
                t_tile = time.time()
                tiled_computed = False
                if cluster_enabled:
                    try:
                        t_tl = Metashape.Tasks.BuildTiledModel()
                        t_tl.tile_size = tile_size
                        t_tl.source_data = Metashape.DataSource.DepthMapsData
                        t_tl.replace_asset = True
                        tiled_computed = dispatch_cluster_task(
                            t_tl, f"Building 3D Tiled Model (Tile Size: {tile_size})", "tiled_model"
                        )
                        if tiled_computed:
                            chunk = doc.chunk
                    except Exception as e_tl_disp:
                        print(
                            f"  [Cluster] BuildTiledModel dispatch exception: {e_tl_disp}. Running locally...",
                            flush=True,
                        )
                        tiled_computed = False

                if not tiled_computed:
                    chunk.buildTiledModel(
                        tile_size=tile_size,
                        source_data=Metashape.DataSource.DepthMapsData,
                    )
                print(f"  3D Tiled model completed in {(time.time() - t_tile) / 60:.2f} min.", flush=True)
                chunk = save_checkpoint(doc, psx_out)
            else:
                print("\n[6b/7] Reusing existing 3D Tiled Model from checkpoint.", flush=True)

            tiled_fmt_tuple = TILED_MODEL_FORMAT_MAP.get(
                tiled_model_format.lower(), (Metashape.TiledModelFormat.TiledModelFormatCesium, "tileset.json")
            )
            tiled_fmt, tiled_ext = tiled_fmt_tuple
            if tiled_model_format.lower() == "cesium":
                tiled_dir = os.path.join(output_dir, f"{project_name}_tiled_model")
                os.makedirs(tiled_dir, exist_ok=True)
                tiled_out = os.path.join(tiled_dir, "tileset.json")
            else:
                tiled_out = os.path.join(output_dir, f"{project_name}_tiled_model{tiled_ext}")
            print(f"\n[6b/7] [EXPORT] Exporting 3D Tiled Model to: {tiled_out}...", flush=True)
            chunk.exportTiledModel(
                path=tiled_out,
                format=tiled_fmt,
            )
            print(f"  [6b/7] [EXPORT] Saved 3D Tiled Model: {tiled_out}", flush=True)
        except Exception as e_tile:
            print(f"  [6b/7] [WARNING] 3D Tiled Model generation/export issue: {e_tile}. Continuing...", flush=True)

    # Step 7b: Digital Elevation Model (DEM) Generation & GeoTIFF Export
    if build_dem:
        try:
            has_dem = getattr(chunk, "elevation", None) is not None
            if not has_dem:
                emit_ipc_stage("dem", "Building Digital Elevation Model (DEM/DSM)")
                print("\n[7b/7] Building Digital Elevation Model (DEM/DSM)...", flush=True)
                t_dem = time.time()
                proj = Metashape.OrthoProjection()
                target_crs = chunk.crs
                if chunk.crs and not getattr(chunk.crs, "projcs", None):
                    aligned_gps = [
                        c for c in chunk.cameras if c.reference and c.reference.location and c.transform and c.enabled
                    ]
                    if aligned_gps:
                        avg_lon = sum(c.reference.location.x for c in aligned_gps) / len(aligned_gps)
                        avg_lat = sum(c.reference.location.y for c in aligned_gps) / len(aligned_gps)
                        if abs(avg_lat) > 0.001 or abs(avg_lon) > 0.001:
                            utm_epsg = compute_utm_epsg(avg_lon, avg_lat)
                            try:
                                target_crs = Metashape.CoordinateSystem(f"EPSG::{utm_epsg}")
                                print(
                                    f"  [Geodesy] Selected Projected Metric CRS: {target_crs.name} (EPSG:{utm_epsg}) for DEM metric scale.",
                                    flush=True,
                                )
                            except Exception:
                                target_crs = chunk.crs
                if target_crs:
                    try:
                        proj.crs = target_crs
                    except Exception:
                        pass
                dem_source = (
                    Metashape.DataSource.PointCloudData
                    if _has_dense_points(chunk)
                    else Metashape.DataSource.DepthMapsData
                )
                dem_computed = False
                if cluster_enabled:
                    try:
                        t_dm = Metashape.Tasks.BuildDem()
                        t_dm.source_data = dem_source
                        t_dm.interpolation = Metashape.Interpolation.EnabledInterpolation
                        t_dm.replace_asset = True
                        if target_crs:
                            t_dm.projection = proj
                        dem_computed = dispatch_cluster_task(t_dm, "Building Digital Elevation Model (DEM/DSM)", "dem")
                        if dem_computed:
                            chunk = doc.chunk
                    except Exception as e_dm_disp:
                        print(f"  [Cluster] BuildDem dispatch exception: {e_dm_disp}. Running locally...", flush=True)
                        dem_computed = False

                if not dem_computed:
                    chunk.buildDem(
                        source_data=dem_source,
                        interpolation=Metashape.Interpolation.EnabledInterpolation,
                        projection=proj,
                    )
                print(f"  [7b/7] DEM generation completed in {(time.time() - t_dem) / 60:.2f} min.", flush=True)
                chunk = save_checkpoint(doc, psx_out)
                print(f"  [7b/7] [CHECKPOINT] Saved DEM inside project: {psx_out}", flush=True)
            else:
                emit_ipc_stage("dem", "Reusing existing Digital Elevation Model from checkpoint")
                print("\n[7b/7] Reusing existing Digital Elevation Model from checkpoint.", flush=True)

            if (not cluster_enabled) and export_dem:
                dem_out = os.path.join(output_dir, f"{project_name}_dem.tif")
                print(f"\n[7b/7] [EXPORT] Exporting GeoTIFF DEM to: {dem_out}...", flush=True)
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
                print(
                    f"  [7b/7] [EXPORT] Saved GeoTIFF DEM: {dem_out} ({os.path.getsize(dem_out) / (1024 * 1024):.2f} MB)",
                    flush=True,
                )
            else:
                print(
                    "  [7b/7] [INFO] DEM saved in project checkpoint. GeoTIFF file export skipped as requested.",
                    flush=True,
                )
        except Exception as e_dem:
            print(f"  [7b/7] [WARNING] DEM generation/export encountered an issue: {e_dem}. Continuing...", flush=True)

    # Step 7b2: Topographic Contour Vectors
    if build_contours and (getattr(chunk, "elevation", None) is not None or build_dem):
        try:
            print(f"\n[7b2/7] Generating Topographic Contours (Interval: {contour_interval}m)...", flush=True)
            t_cnt = time.time()
            chunk.buildContours(
                source_data=Metashape.DataSource.ElevationData,
                interval=contour_interval,
                prevent_intersections=True,
            )
            print(f"  Contour lines generated in {(time.time() - t_cnt) / 60:.2f} min.", flush=True)

            sh_fmt, sh_ext = SHAPES_FORMAT_MAP.get(
                contours_format.lower(), (Metashape.ShapesFormat.ShapesFormatSHP, ".shp")
            )
            cnt_out = os.path.join(output_dir, f"{project_name}_contours{sh_ext}")
            print(f"  [7b2/7] [EXPORT] Exporting Contour Shapes to: {cnt_out}...", flush=True)
            chunk.exportShapes(
                path=cnt_out,
                format=sh_fmt,
                save_polylines=True,
                save_elevation=True,
                save_attributes=True,
            )
            print(f"  [7b2/7] [EXPORT] Saved Topographic Contours: {cnt_out}", flush=True)
        except Exception as e_cnt:
            print(f"  [7b2/7] [WARNING] Topographic contours issue: {e_cnt}. Continuing...", flush=True)

    # Step 7c: Orthomosaic Generation & GeoTIFF Export
    if build_orthomosaic:
        try:
            has_ortho = chunk.orthomosaic is not None
            if not has_ortho:
                emit_ipc_stage("ortho", "Generating Orthomosaic from Reconstructed Surface Mesh")
                print("\n[7c/7] Generating Orthomosaic from Reconstructed Surface Mesh...", flush=True)
                t_ortho = time.time()
                proj = Metashape.OrthoProjection()
                target_crs = chunk.crs
                if chunk.crs and not getattr(chunk.crs, "projcs", None):
                    aligned_gps = [
                        c for c in chunk.cameras if c.reference and c.reference.location and c.transform and c.enabled
                    ]
                    if aligned_gps:
                        avg_lon = sum(c.reference.location.x for c in aligned_gps) / len(aligned_gps)
                        avg_lat = sum(c.reference.location.y for c in aligned_gps) / len(aligned_gps)
                        if abs(avg_lat) > 0.001 or abs(avg_lon) > 0.001:
                            utm_epsg = compute_utm_epsg(avg_lon, avg_lat)
                            try:
                                target_crs = Metashape.CoordinateSystem(f"EPSG::{utm_epsg}")
                                print(
                                    f"  [Geodesy] Selected Projected Metric CRS: {target_crs.name} (EPSG:{utm_epsg}) for Orthomosaic metric scale.",
                                    flush=True,
                                )
                            except Exception:
                                target_crs = chunk.crs
                if target_crs:
                    try:
                        proj.crs = target_crs
                    except Exception:
                        pass
                ortho_computed = False
                surf_data = (
                    Metashape.DataSource.ElevationData
                    if (build_dem or (chunk and getattr(chunk, "elevation", None) is not None))
                    else Metashape.DataSource.ModelData
                )
                if cluster_enabled:
                    try:
                        t_om = Metashape.Tasks.BuildOrthomosaic()
                        t_om.surface_data = surf_data
                        t_om.replace_asset = True
                        t_om.blending_mode = Metashape.BlendingMode.MosaicBlending
                        t_om.fill_holes = fill_holes
                        t_om.ghosting_filter = ghosting_filter
                        t_om.cull_faces = ortho_cull_faces
                        t_om.projection = proj
                        if getattr(proj.crs, "projcs", None) and ortho_resolution > 0:
                            t_om.resolution = ortho_resolution
                        ortho_desc = (
                            "Generating True Orthomosaic from Elevation Surface (DEM)"
                            if surf_data == Metashape.DataSource.ElevationData
                            else "Generating Orthomosaic from Reconstructed Surface Mesh"
                        )
                        ortho_computed = dispatch_cluster_task(t_om, ortho_desc, "ortho")
                        if ortho_computed:
                            chunk = doc.chunk
                    except Exception as e_om_disp:
                        print(
                            f"  [Cluster] BuildOrthomosaic dispatch exception: {e_om_disp}. Running locally...",
                            flush=True,
                        )
                        ortho_computed = False

                if not ortho_computed:
                    chunk.buildOrthomosaic(
                        surface_data=surf_data,
                        blending_mode=Metashape.BlendingMode.MosaicBlending,
                        fill_holes=fill_holes,
                        ghosting_filter=ghosting_filter,
                        cull_faces=ortho_cull_faces,
                        projection=proj,
                        resolution=ortho_resolution if getattr(proj.crs, "projcs", None) else 0,
                    )
                print(
                    f"  [7c/7] Orthomosaic generation completed in {(time.time() - t_ortho) / 60:.2f} min ({chunk.orthomosaic.width}x{chunk.orthomosaic.height} px).",
                    flush=True,
                )
                chunk = save_checkpoint(doc, psx_out)
                print(f"  [7c/7] [CHECKPOINT] Saved orthomosaic inside project: {psx_out}", flush=True)
            else:
                emit_ipc_stage(
                    "ortho",
                    f"Reusing existing Orthomosaic from checkpoint ({chunk.orthomosaic.width}x{chunk.orthomosaic.height} px)",
                )
                print(
                    f"\n[7c/7] Reusing existing Orthomosaic from checkpoint ({chunk.orthomosaic.width}x{chunk.orthomosaic.height} px).",
                    flush=True,
                )

            if (not cluster_enabled) and export_ortho:
                ortho_out = os.path.join(output_dir, f"{project_name}_orthomosaic.tif")
                print(f"\n[7c/7] [EXPORT] Exporting GeoTIFF Orthomosaic to: {ortho_out}...", flush=True)
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
                print(
                    f"  [7c/7] [EXPORT] Saved GeoTIFF Orthomosaic: {ortho_out} ({os.path.getsize(ortho_out) / (1024 * 1024):.2f} MB)",
                    flush=True,
                )
            else:
                print(
                    "  [7c/7] [INFO] Orthomosaic saved in project checkpoint. GeoTIFF file export skipped as requested.",
                    flush=True,
                )
        except Exception as e_ortho:
            print(
                f"  [7c/7] [WARNING] Orthomosaic generation/export encountered an issue: {e_ortho}. Continuing...",
                flush=True,
            )

    # Production Asset Export
    emit_ipc_stage("export", "Exporting Production Assets (GLB, OBJ, TIFF, LAZ, DEM & PSX)")
    print("\n=========================================================================")
    print("  [7/7] EXPORTING PRODUCTION ASSETS (GLB, OBJ, TIFF, LAZ, DEM & PSX)")
    print("=========================================================================")
    glb_out = os.path.join(output_dir, f"{project_name}.glb")
    obj_out = os.path.join(output_dir, f"{project_name}.obj")

    cluster_exported = False
    if cluster_enabled:
        try:
            export_tasks = []
            tex_fmt = texture_format if texture_format else "png"
            fmt_lower = tex_fmt.lower()
            if fmt_lower in ("tiff", "tif"):
                tex_image_fmt = Metashape.ImageFormat.ImageFormatTIFF
            elif fmt_lower in ("jpg", "jpeg"):
                tex_image_fmt = Metashape.ImageFormat.ImageFormatJPEG
            else:
                tex_image_fmt = Metashape.ImageFormat.ImageFormatPNG

            if export_obj and chunk.model:
                t_obj = Metashape.Tasks.ExportModel()
                t_obj.format = Metashape.ModelFormat.ModelFormatOBJ
                t_obj.texture_format = tex_image_fmt
                t_obj.save_texture = True
                t_obj.save_normals = True
                t_obj.save_colors = True
                t_obj.path = get_cluster_export_path(obj_out)
                export_tasks.append(t_obj)

            if export_glb and chunk.model:
                t_glb = Metashape.Tasks.ExportModel()
                t_glb.format = Metashape.ModelFormat.ModelFormatGLTF
                t_glb.embed_texture = True
                t_glb.save_normals = True
                t_glb.save_colors = True
                t_glb.path = get_cluster_export_path(glb_out)
                export_tasks.append(t_glb)

            if export_fbx and chunk.model:
                fbx_out = os.path.join(output_dir, f"{project_name}.fbx")
                t_fbx = Metashape.Tasks.ExportModel()
                t_fbx.format = Metashape.ModelFormat.ModelFormatFBX
                t_fbx.save_texture = True
                t_fbx.save_normals = True
                t_fbx.save_colors = True
                t_fbx.path = get_cluster_export_path(fbx_out)
                export_tasks.append(t_fbx)

            if export_ply and chunk.model:
                ply_out = os.path.join(output_dir, f"{project_name}_mesh.ply")
                t_ply = Metashape.Tasks.ExportModel()
                t_ply.format = Metashape.ModelFormat.ModelFormatPLY
                t_ply.save_normals = True
                t_ply.save_colors = True
                t_ply.path = get_cluster_export_path(ply_out)
                export_tasks.append(t_ply)

            if export_dxf and chunk.model:
                dxf_out = os.path.join(output_dir, f"{project_name}.dxf")
                t_dxf = Metashape.Tasks.ExportModel()
                t_dxf.format = Metashape.ModelFormat.ModelFormatDXF
                t_dxf.save_normals = True
                t_dxf.path = get_cluster_export_path(dxf_out)
                export_tasks.append(t_dxf)

            if export_dem and (getattr(chunk, "elevation", None) is not None or build_dem):
                dem_out = os.path.join(output_dir, f"{project_name}_dem.tif")
                t_dem = Metashape.Tasks.ExportRaster()
                t_dem.source_data = Metashape.DataSource.ElevationData
                t_dem.image_format = Metashape.ImageFormat.ImageFormatTIFF
                comp = Metashape.ImageCompression()
                comp.tiff_compression = Metashape.ImageCompression.TiffCompression.TiffCompressionLZW
                comp.tiff_big = True
                t_dem.image_compression = comp
                t_dem.save_world = True
                t_dem.path = get_cluster_export_path(dem_out)
                export_tasks.append(t_dem)

            if export_ortho and (chunk.orthomosaic is not None or build_orthomosaic):
                ortho_out = os.path.join(output_dir, f"{project_name}_orthomosaic.tif")
                t_ortho = Metashape.Tasks.ExportRaster()
                t_ortho.source_data = Metashape.DataSource.OrthomosaicData
                t_ortho.image_format = Metashape.ImageFormat.ImageFormatTIFF
                comp = Metashape.ImageCompression()
                comp.tiff_compression = Metashape.ImageCompression.TiffCompression.TiffCompressionLZW
                comp.tiff_big = True
                t_ortho.image_compression = comp
                t_ortho.save_world = True
                t_ortho.save_alpha = True
                t_ortho.path = get_cluster_export_path(ortho_out)
                export_tasks.append(t_ortho)

            if export_point_cloud and (chunk.point_cloud is not None or build_point_cloud):
                pc_format_ext = point_cloud_format.lower() if point_cloud_format else "laz"
                pc_fmt, pc_ext = POINT_CLOUD_FORMAT_MAP.get(
                    pc_format_ext,
                    (Metashape.PointCloudFormat.PointCloudFormatLAZ, ".laz")
                    if Metashape is not None
                    else (None, ".laz"),
                )
                pc_out = os.path.join(output_dir, f"{project_name}_point_cloud{pc_ext}")
                t_pc = Metashape.Tasks.ExportPointCloud()
                t_pc.format = pc_fmt
                t_pc.source_data = Metashape.DataSource.PointCloudData
                t_pc.save_point_color = True
                t_pc.save_point_normal = True
                t_pc.save_point_confidence = True
                t_pc.save_point_classification = True
                t_pc.path = get_cluster_export_path(pc_out)
                export_tasks.append(t_pc)

            if export_report:
                report_out = os.path.join(output_dir, f"{project_name}_report.pdf")
                t_rep = Metashape.Tasks.ExportReport()
                t_rep.title = report_title or "AutoMetashape Processing & Calibration Report"
                t_rep.path = get_cluster_export_path(report_out)
                export_tasks.append(t_rep)

            if export_tasks:
                cluster_exported = dispatch_cluster_task(
                    export_tasks, f"Cluster Exporting Deliverables ({len(export_tasks)} formats)", "export"
                )
                if cluster_exported:
                    print(
                        f"  [Cluster] [OK] All {len(export_tasks)} production deliverables exported directly by cluster worker!",
                        flush=True,
                    )
        except Exception as e_cl_exp:
            print(
                f"  [Cluster] Note: cluster export delegation exception: {e_cl_exp}. Falling back to local export.",
                flush=True,
            )
            cluster_exported = False

    if (not cluster_exported) and export_glb and chunk.model:
        try:
            chunk.exportModel(
                glb_out,
                format=Metashape.ModelFormat.ModelFormatGLTF,
                embed_texture=True,
                save_normals=True,
                save_colors=True,
            )
            print(
                f"  [7/7] [EXPORT] Saved binary GLB: {glb_out} ({os.path.getsize(glb_out) / (1024 * 1024):.2f} MB)",
                flush=True,
            )
        except Exception as e_glb:
            print(f"  [7/7] [WARNING] GLB export issue: {e_glb}", flush=True)

    if (not cluster_exported) and export_obj and chunk.model:
        fmt_lower = (texture_format or "png").lower()
        if fmt_lower in ("tiff", "tif"):
            tex_image_fmt = Metashape.ImageFormat.ImageFormatTIFF
        elif fmt_lower in ("jpg", "jpeg"):
            tex_image_fmt = Metashape.ImageFormat.ImageFormatJPEG
        else:
            tex_image_fmt = Metashape.ImageFormat.ImageFormatPNG
        try:
            chunk.exportModel(
                obj_out,
                format=Metashape.ModelFormat.ModelFormatOBJ,
                texture_format=tex_image_fmt,
                save_texture=True,
                save_normals=True,
                save_colors=True,
            )
            print(
                f"  [7/7] [EXPORT] Saved Wavefront OBJ ({fmt_lower.upper()} textures): {obj_out} ({os.path.getsize(obj_out) / (1024 * 1024):.2f} MB)",
                flush=True,
            )
        except Exception as e_obj:
            print(f"  [7/7] [WARNING] OBJ export issue: {e_obj}", flush=True)

    if (not cluster_exported) and export_dem and getattr(chunk, "elevation", None) is not None:
        try:
            dem_out = os.path.join(output_dir, f"{project_name}_dem.tif")
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
            print(
                f"  [7/7] [EXPORT] Saved GeoTIFF DEM: {dem_out} ({os.path.getsize(dem_out) / (1024 * 1024):.2f} MB)",
                flush=True,
            )
        except Exception as e_dem:
            print(f"  [7/7] [WARNING] DEM fallback export issue: {e_dem}", flush=True)

    if (not cluster_exported) and export_ortho and chunk.orthomosaic is not None:
        try:
            ortho_out = os.path.join(output_dir, f"{project_name}_orthomosaic.tif")
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
            print(
                f"  [7/7] [EXPORT] Saved GeoTIFF Orthomosaic: {ortho_out} ({os.path.getsize(ortho_out) / (1024 * 1024):.2f} MB)",
                flush=True,
            )
        except Exception as e_ortho:
            print(f"  [7/7] [WARNING] Orthomosaic fallback export issue: {e_ortho}", flush=True)

    has_any_export = any(
        [
            export_glb,
            export_obj,
            export_fbx,
            export_ply,
            export_dxf,
            export_tie_points,
            export_dem,
            export_ortho,
            export_point_cloud,
            export_report,
            build_contours,
            build_tiled_model,
        ]
    )
    if not has_any_export:
        print(
            "  [7/7] [INFO] Deliverable file export skipped as requested: Metashape native project (.psx) saved only.",
            flush=True,
        )

    if (not cluster_exported) and export_fbx and chunk.model:
        fbx_out = os.path.join(output_dir, f"{project_name}.fbx")
        print(f"  [7/7] [EXPORT] Exporting Autodesk FBX to: {fbx_out}...", flush=True)
        try:
            chunk.exportModel(
                fbx_out,
                format=Metashape.ModelFormat.ModelFormatFBX,
                save_texture=True,
                save_normals=True,
                save_colors=True,
            )
            print(
                f"  [7/7] [EXPORT] Saved FBX: {fbx_out} ({os.path.getsize(fbx_out) / (1024 * 1024):.2f} MB)", flush=True
            )
        except Exception as e_fbx:
            print(f"  [7/7] [WARNING] FBX export issue: {e_fbx}", flush=True)

    if (not cluster_exported) and export_ply and chunk.model:
        ply_out = os.path.join(output_dir, f"{project_name}_mesh.ply")
        print(f"  [7/7] [EXPORT] Exporting Stanford PLY to: {ply_out}...", flush=True)
        try:
            chunk.exportModel(ply_out, format=Metashape.ModelFormat.ModelFormatPLY, save_normals=True, save_colors=True)
            print(
                f"  [7/7] [EXPORT] Saved PLY: {ply_out} ({os.path.getsize(ply_out) / (1024 * 1024):.2f} MB)", flush=True
            )
        except Exception as e_ply:
            print(f"  [7/7] [WARNING] PLY export issue: {e_ply}", flush=True)

    if (not cluster_exported) and export_dxf and chunk.model:
        dxf_out = os.path.join(output_dir, f"{project_name}.dxf")
        print(f"  [7/7] [EXPORT] Exporting AutoCAD DXF to: {dxf_out}...", flush=True)
        try:
            chunk.exportModel(dxf_out, format=Metashape.ModelFormat.ModelFormatDXF, save_normals=True)
            print(
                f"  [7/7] [EXPORT] Saved DXF: {dxf_out} ({os.path.getsize(dxf_out) / (1024 * 1024):.2f} MB)", flush=True
            )
        except Exception as e_dxf:
            print(f"  [7/7] [WARNING] DXF export issue: {e_dxf}", flush=True)

    if (not cluster_exported) and export_tie_points and chunk.tie_points:
        tp_out = os.path.join(output_dir, f"{project_name}_tie_points.ply")
        print(f"  [7/7] [EXPORT] Exporting Sparse Tie Points to: {tp_out}...", flush=True)
        try:
            chunk.exportPointCloud(
                path=tp_out,
                format=Metashape.PointCloudFormat.PointCloudFormatPLY,
                source_data=Metashape.DataSource.TiePointsData,
            )
            print(
                f"  [7/7] [EXPORT] Saved Tie Points: {tp_out} ({os.path.getsize(tp_out) / (1024 * 1024):.2f} MB)",
                flush=True,
            )
        except Exception as e_tp:
            print(f"  [7/7] [WARNING] Tie points export issue: {e_tp}", flush=True)

    # Step 7d: Dense Point Cloud Export
    if (not cluster_exported) and export_point_cloud:
        if _has_dense_points(chunk):
            pc_format_ext = point_cloud_format.lower() if point_cloud_format else "laz"
            pc_fmt, pc_ext = POINT_CLOUD_FORMAT_MAP.get(
                pc_format_ext,
                (Metashape.PointCloudFormat.PointCloudFormatLAZ, ".laz") if Metashape is not None else (None, ".laz"),
            )
            pc_out = os.path.join(output_dir, f"{project_name}_point_cloud{pc_ext}")
            print(f"\n[7d/7] [EXPORT] Exporting Dense Point Cloud to: {pc_out}...", flush=True)
            chunk.exportPointCloud(
                path=pc_out,
                format=pc_fmt,
                source_data=Metashape.DataSource.PointCloudData,
                save_point_color=True,
                save_point_normal=True,
                save_point_confidence=True,
                save_point_classification=True,
            )
            print(
                f"  [7d/7] [EXPORT] Saved Dense Point Cloud: {pc_out} ({os.path.getsize(pc_out) / (1024 * 1024):.2f} MB)",
                flush=True,
            )
    elif build_point_cloud and (not cluster_exported):
        print(
            f"  [7d/7] [INFO] Dense point cloud preserved in project checkpoint. {point_cloud_format.upper() if point_cloud_format else 'LAZ'} file export skipped as requested.",
            flush=True,
        )

    # Step 7e: Executive PDF Quality & Calibration Report
    if (not cluster_exported) and export_report:
        try:
            report_out = os.path.join(output_dir, f"{project_name}_report.pdf")
            print(
                f"\n[7e/7] [EXPORT] Exporting Official Calibration & Quality PDF Report to: {report_out}...", flush=True
            )
            rep_title = report_title or "AutoMetashape Processing & Calibration Report"
            chunk.exportReport(
                path=report_out,
                title=rep_title,
                description="AutoMetashape Autonomous Photogrammetry Pipeline",
            )
            print(
                f"  [7e/7] [EXPORT] Saved Quality Report: {report_out} ({os.path.getsize(report_out) / (1024 * 1024):.2f} MB)",
                flush=True,
            )
        except Exception as e_rep:
            print(f"  [7e/7] [WARNING] PDF Report export encountered an issue: {e_rep}. Continuing...", flush=True)

    chunk = save_checkpoint(doc, psx_out)
    print(f"  [7/7] [EXPORT] Saved Metashape Pro Native Project: {psx_out}", flush=True)

    # Clean up obsolete building_reconstruction.psx if present to ensure single project-named file
    if legacy_psx != psx_out and os.path.exists(legacy_psx):
        try:
            os.remove(legacy_psx)
        except Exception:
            pass

    total_time = (time.time() - t_start) / 60.0
    print(f"\n[SUCCESS] Entire pipeline finished in {total_time:.2f} minutes!", flush=True)
    emit_ipc_event(IPC_COMPLETE)

    if survey_physics and pro_plan:
        print_pro_engineering_report(survey_physics, pro_plan, usgs_stats=usgs_stats)

    # Synchronize staged project and deliverables from local NVMe scratch to central network share
    if local_staging_dir:
        try:
            atexit.unregister(_sync_staging_to_central_share)
        except Exception:
            pass
        _sync_staging_to_central_share()


def build_arg_parser() -> argparse.ArgumentParser:
    """Build and return the comprehensive command-line argument parser for AutoMetashape."""
    parser = argparse.ArgumentParser(
        description="AutoMetashape: Autonomous High-Precision Photogrammetry Pipeline (Survey Profiling, Point Cloud, Mesh & Orthomosaic)"
    )
    parser.add_argument(
        "--images",
        "--photos-dir",
        dest="images",
        required=True,
        help="Input directory containing raw images (scanned recursively across all subfolders)",
    )
    parser.add_argument(
        "--output", default=None, help="Output directory for 3D assets (default: <images_dir>/<project_name>)"
    )
    parser.add_argument(
        "--project-name",
        default=None,
        help="Base name for the project file, 3D model, textures, point cloud, and reports (default: folder name of the project)",
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help="Profile dataset telemetry, print inferred optimal parameters, and exit without reconstructing",
    )
    parser.add_argument(
        "--auto", action="store_true", help="Automatically profile dataset and infer optimal parameters (default mode)"
    )
    parser.add_argument(
        "--preset",
        choices=["pro", "custom", "building"],
        default="pro",
        help="Reconstruction preset: 'pro' (default: Production Geomatics Standard with gentle tie-point filtering, 2M Arbitrary 3D Mesh, 16-page 8K texture atlas, Orthomosaic & DEM) or 'custom' (manual configuration)",
    )
    # Granular Workflow Steps (Metashape Process Stages)
    parser.add_argument(
        "--align-photos",
        dest="align_photos",
        action="store_true",
        default=True,
        help="Execute camera matching and alignment (default: True)",
    )
    parser.add_argument(
        "--no-align-photos", dest="align_photos", action="store_false", help="Skip camera matching and alignment"
    )
    parser.add_argument(
        "--build-depth-maps",
        dest="build_depth_maps",
        action="store_true",
        default=True,
        help="Execute GPU MVS dense depth maps calculation (default: True)",
    )
    parser.add_argument(
        "--no-build-depth-maps",
        dest="build_depth_maps",
        action="store_false",
        help="Skip GPU MVS dense depth maps calculation",
    )
    parser.add_argument(
        "--build-mesh",
        dest="build_mesh",
        action="store_true",
        default=True,
        help="Execute arbitrary 3D surface mesh reconstruction (default: True)",
    )
    parser.add_argument(
        "--no-build-mesh", dest="build_mesh", action="store_false", help="Skip arbitrary 3D surface mesh reconstruction"
    )
    parser.add_argument(
        "--build-texture",
        dest="build_texture",
        action="store_true",
        default=True,
        help="Execute multi-page 8K texture atlas baking (default: True)",
    )
    parser.add_argument(
        "--no-build-texture", dest="build_texture", action="store_false", help="Skip multi-page 8K texture atlas baking"
    )
    parser.add_argument(
        "--pro", action="store_true", default=False, help="Execute the Pro Photogrammetry & Geomatics Engine"
    )
    parser.add_argument(
        "--ground-cell-size",
        type=float,
        default=None,
        help="Cell size in meters for slope-adaptive ground point classification",
    )
    parser.add_argument(
        "--ground-max-angle",
        type=float,
        default=None,
        help="Max terrain slope angle in degrees for ground point classification",
    )
    parser.add_argument(
        "--ground-max-distance", type=float, default=None, help="Max distance in meters for ground point classification"
    )
    parser.add_argument(
        "--align-quality",
        choices=["highest", "high", "medium", "low"],
        default="high",
        help="Image alignment accuracy/quality (default: high)",
    )
    parser.add_argument("--keypoint-limit", type=int, default=200000, help="Max keypoints per image (default: 200000)")
    parser.add_argument(
        "--keypoint-limit-per-mpx", type=int, default=15000, help="Max keypoints per megapixel (default: 15000)"
    )
    parser.add_argument("--tiepoint-limit", type=int, default=25000, help="Max tiepoints per image (default: 25000)")
    parser.add_argument(
        "--reference-preselection-mode",
        dest="reference_preselection_mode",
        choices=["estimated", "source", "sequential"],
        default="estimated",
        help="Reference preselection mode for photo matching: 'estimated' (default: robust for multi-drone/oblique flights), 'source' (raw EXIF GPS), or 'sequential'",
    )
    parser.add_argument(
        "--depth-quality",
        choices=["ultra", "high", "medium", "low"],
        default="high",
        help="Depth map quality (default: high)",
    )
    parser.add_argument(
        "--depth-filtering",
        choices=["mild", "moderate", "aggressive"],
        default=None,
        help="Depth map filtering mode (default: inferred or moderate)",
    )
    parser.add_argument(
        "--faces", type=int, default=None, help="Target polygon face count (default: 2,000,000 in 'pro' preset)"
    )
    parser.add_argument(
        "--guided-matching",
        dest="guided_matching",
        action="store_true",
        default=True,
        help="Enable guided image matching (default: True, finds additional tie points on high-resolution imagery)",
    )
    parser.add_argument(
        "--no-guided-matching",
        dest="guided_matching",
        action="store_false",
        help="Disable guided image matching",
    )
    parser.add_argument(
        "--no-filter-stationary",
        dest="filter_stationary_points",
        action="store_false",
        default=True,
        help="Disable stationary tie point exclusion (default: stationary points are excluded)",
    )
    parser.add_argument(
        "--no-adaptive-fitting",
        dest="adaptive_fitting",
        action="store_false",
        default=True,
        help="Disable adaptive camera model fitting (default: enabled)",
    )
    parser.add_argument(
        "--filter-tiepoints",
        dest="filter_tiepoints",
        action="store_true",
        default=True,
        help="Enable gentle gradual selection tie-point filtering and camera re-optimization (default: enabled)",
    )
    parser.add_argument(
        "--no-filter-tiepoints",
        dest="filter_tiepoints",
        action="store_false",
        help="Disable gentle gradual selection tie-point filtering (keeps all aligned tie points intact)",
    )
    parser.add_argument(
        "--rolling-shutter",
        dest="rolling_shutter",
        action="store_true",
        default=None,
        help="Force enable CMOS rolling shutter compensation",
    )
    parser.add_argument(
        "--no-rolling-shutter",
        dest="rolling_shutter",
        action="store_false",
        help="Disable CMOS rolling shutter compensation",
    )
    parser.add_argument(
        "--close-holes",
        dest="close_holes",
        action="store_true",
        default=None,
        help="Enable mesh hole closing (bridges voids up to level 30)",
    )
    parser.add_argument(
        "--no-close-holes",
        dest="close_holes",
        action="store_false",
        help="Disable mesh hole closing (default: disabled to prevent planar webbing artifacts)",
    )
    parser.add_argument(
        "--texture-pages",
        type=int,
        default=None,
        help="Number of 8K texture atlas pages (default: 16 in 'pro' preset, or custom specified)",
    )
    parser.add_argument(
        "--no-fill-holes",
        dest="fill_holes",
        action="store_false",
        default=True,
        help="Disable texture hole filling (default: enabled)",
    )
    parser.add_argument(
        "--no-ghosting-filter",
        dest="ghosting_filter",
        action="store_false",
        default=True,
        help="Disable ghosting filter in texture blending (default: enabled)",
    )
    parser.add_argument(
        "--rebake-texture",
        action="store_true",
        default=False,
        help="Force re-baking texture atlas even if one already exists in checkpoint",
    )
    parser.add_argument(
        "--crop-method",
        choices=["gps-square", "structure", "manual", "none"],
        default=None,
        help="Bounding box crop method: 'gps-square', 'structure', 'manual', or 'none' (default: inferred)",
    )
    parser.add_argument(
        "--no-crop-structure", dest="no_crop", action="store_true", help="Disable automatic bounding box crop"
    )
    parser.add_argument(
        "--crop-padding",
        type=float,
        default=0.05,
        help="Safety margin padding factor around bounding box (default: 0.05 / 5%%)",
    )
    parser.add_argument(
        "--min-quality",
        type=float,
        default=0.5,
        help="Minimum image sharpness quality threshold (default: 0.5; photos below this are disabled to prevent alignment blur)",
    )
    parser.add_argument(
        "--no-quality-filter",
        dest="disable_quality_filter",
        action="store_true",
        help="Disable automatic blurry image quality estimation and filtering",
    )
    parser.add_argument(
        "--pause-after-crop",
        action="store_true",
        help="Pause after alignment and crop for manual bounding box adjustment in GUI",
    )
    parser.add_argument(
        "--no-crop",
        dest="no_crop",
        action="store_true",
        help="Disable bounding box cropping (reconstruct full uncropped scene)",
    )
    # 3D Model Export Options (Default: False · Metashape project only)
    parser.add_argument(
        "--export-glb", dest="export_glb", action="store_true", default=False, help="Export binary glTF 3D model (.glb)"
    )
    parser.add_argument("--no-export-glb", dest="export_glb", action="store_false", help="Disable binary GLB export")
    parser.add_argument(
        "--export-obj",
        dest="export_obj",
        action="store_true",
        default=False,
        help="Export Wavefront OBJ model + texture atlas (.obj)",
    )
    parser.add_argument("--no-export-obj", dest="export_obj", action="store_false", help="Disable Wavefront OBJ export")
    parser.add_argument(
        "--texture-format",
        choices=["png", "tiff", "tif", "jpg", "jpeg"],
        default="png",
        help="Format for exported texture atlas files (default: png)",
    )
    # Point Cloud Options
    parser.add_argument(
        "--build-point-cloud",
        dest="build_point_cloud",
        action="store_true",
        default=True,
        help="Generate dense point cloud from depth maps (default: True)",
    )
    parser.add_argument(
        "--no-point-cloud", dest="build_point_cloud", action="store_false", help="Disable dense point cloud generation"
    )
    parser.add_argument(
        "--export-point-cloud",
        dest="export_point_cloud",
        action="store_true",
        default=False,
        help="Export dense point cloud file (default: False)",
    )
    parser.add_argument(
        "--no-export-point-cloud",
        dest="export_point_cloud",
        action="store_false",
        help="Disable dense point cloud export",
    )
    parser.add_argument(
        "--point-cloud-format",
        choices=["laz", "las", "ply", "e57", "xyz"],
        default="laz",
        help="Format for exported dense point cloud (default: laz)",
    )
    parser.add_argument(
        "--classify-ground",
        action="store_true",
        default=None,
        help="Classify ground vs non-ground points in dense point cloud",
    )
    parser.add_argument(
        "--classify-points",
        action="store_true",
        default=False,
        help="Perform multiclass AI semantic point cloud classification",
    )
    parser.add_argument(
        "--filter-point-confidence", type=int, default=None, help="Filter points with confidence below threshold"
    )
    # DEM / Elevation Options
    parser.add_argument(
        "--build-dem",
        action="store_true",
        default=None,
        help="Build Digital Elevation Model (DEM/DSM) (default: False)",
    )
    parser.add_argument("--no-dem", dest="build_dem", action="store_false", help="Disable DEM building and export")
    parser.add_argument(
        "--export-dem",
        dest="export_dem",
        action="store_true",
        default=False,
        help="Export GeoTIFF Digital Elevation Model (dem.tif)",
    )
    parser.add_argument(
        "--no-export-dem",
        dest="export_dem",
        action="store_false",
        help="Disable GeoTIFF Digital Elevation Model export",
    )
    # Orthomosaic Options
    parser.add_argument(
        "--ortho",
        "--build-orthomosaic",
        dest="build_orthomosaic",
        action="store_true",
        default=None,
        help="Build orthomosaic from reconstructed mesh and save in project (default: True in 'pro' preset)",
    )
    parser.add_argument(
        "--no-ortho",
        "--no-build-orthomosaic",
        dest="build_orthomosaic",
        action="store_false",
        help="Disable orthomosaic building",
    )
    parser.add_argument(
        "--export-ortho",
        dest="export_ortho",
        action="store_true",
        default=False,
        help="Export GeoTIFF True Orthomosaic (orthomosaic.tif)",
    )
    parser.add_argument(
        "--no-export-ortho",
        dest="export_ortho",
        action="store_false",
        help="Disable GeoTIFF True Orthomosaic file export",
    )
    # Quality Report Options
    parser.add_argument(
        "--export-report",
        dest="export_report",
        action="store_true",
        default=False,
        help="Export official Metashape PDF calibration & quality report (default: False)",
    )
    parser.add_argument(
        "--no-report",
        "--no-export-report",
        dest="export_report",
        action="store_false",
        help="Disable PDF quality report export",
    )
    parser.add_argument("--report-title", default=None, help="Custom title for PDF quality report")
    # Extended Metashape Functions: Targets, GCPs, Colors, Tiled Model, Contours & Delighting
    parser.add_argument(
        "--detect-markers", action="store_true", default=False, help="Detect coded target markers or AprilTags"
    )
    parser.add_argument(
        "--target-type",
        choices=[
            "circular12",
            "circular14",
            "circular16",
            "circular20",
            "cross",
            "apriltag16h5",
            "apriltag25h9",
            "apriltag36h11",
        ],
        default="circular12",
        help="Coded target type (default: circular12)",
    )
    parser.add_argument(
        "--marker-tolerance", type=int, default=50, help="Marker detector tolerance 0-100 (default: 50)"
    )
    parser.add_argument(
        "--import-reference", default=None, help="Path to ground control points (GCP) reference CSV/TXT file"
    )
    parser.add_argument(
        "--calibrate-colors",
        action="store_true",
        default=False,
        help="Perform radiometric color & white balance calibration",
    )
    parser.add_argument(
        "--surface-type",
        choices=["arbitrary", "heightfield"],
        default="arbitrary",
        help="Mesh surface type (default: arbitrary)",
    )
    parser.add_argument(
        "--smooth-mesh", action="store_true", default=False, help="Apply Laplacian smoothing to surface mesh"
    )
    parser.add_argument(
        "--smooth-strength", type=float, default=3.0, help="Laplacian smoothing strength (default: 3.0)"
    )
    parser.add_argument("--decimate-mesh", type=int, default=None, help="Decimate mesh to target face count")
    parser.add_argument(
        "--remove-lighting",
        action="store_true",
        default=False,
        help="Remove shadows and ambient lighting from texture (delighting)",
    )
    parser.add_argument(
        "--delighting-color-mode", action="store_true", default=False, help="Enable multi-color delighting mode"
    )
    parser.add_argument(
        "--build-tiled-model", action="store_true", default=False, help="Build hierarchical 3D tiled model (LOD)"
    )
    parser.add_argument(
        "--tiled-model-format",
        choices=["cesium", "tls", "slpk", "osgb"],
        default="cesium",
        help="Format for 3D tiled model (default: cesium)",
    )
    parser.add_argument("--tile-size", type=int, default=256, help="Tile size in pixels (default: 256)")
    parser.add_argument(
        "--build-contours", action="store_true", default=False, help="Generate topographic contour lines from DEM"
    )
    parser.add_argument(
        "--contour-interval", type=float, default=1.0, help="Contour elevation interval in meters (default: 1.0)"
    )
    parser.add_argument(
        "--contours-format",
        choices=["shp", "dxf", "geojson", "kml"],
        default="shp",
        help="Format for topographic contours (default: shp)",
    )
    parser.add_argument(
        "--export-fbx", action="store_true", default=False, help="Export 3D model in Autodesk FBX format"
    )
    parser.add_argument(
        "--export-ply", action="store_true", default=False, help="Export 3D model in Stanford PLY format"
    )
    parser.add_argument(
        "--export-dxf", action="store_true", default=False, help="Export 3D model in AutoCAD DXF format"
    )
    parser.add_argument(
        "--export-tie-points", action="store_true", default=False, help="Export sparse tie points in PLY format"
    )
    # Distributed Network Processing Cluster Options
    parser.add_argument(
        "--network-enable",
        action="store_true",
        default=False,
        help="Enable distributed network processing across worker nodes",
    )
    parser.add_argument(
        "--network-host", default="127.0.0.1", help="Metashape server hostname or IP address (default: 127.0.0.1)"
    )
    parser.add_argument("--network-port", type=int, default=5840, help="Metashape server port (default: 5840)")
    parser.add_argument(
        "--network-independent-workers",
        action="store_true",
        default=False,
        help=(
            "Lock each project to an independent worker executing exclusively via the offline monolithic "
            "local scratch process (worker_limit=1, zero stage-by-stage network fallback)"
        ),
    )
    parser.add_argument(
        "--local-scratch-staging",
        "--stage-local",
        dest="local_scratch_staging",
        action="store_true",
        default=True,
        help=(
            "Stage intermediate project files on local fast NVMe scratch volume "
            "and sync deliverables to central share upon completion (default: True)"
        ),
    )
    parser.add_argument(
        "--no-local-scratch-staging",
        "--no-stage-local",
        dest="local_scratch_staging",
        action="store_false",
        help="Disable local NVMe scratch staging and process directly on the network share",
    )
    parser.add_argument(
        "--scratch-dir",
        "--tmp",
        dest="scratch_dir",
        type=str,
        default="",
        help="Custom local NVMe scratch directory for staging",
    )
    parser.add_argument(
        "--min-scratch-free-gb",
        dest="min_scratch_free_gb",
        type=float,
        default=100.0,
        help=(
            "Minimum free disk space threshold (GB) required on local drive "
            "before engaging local staging (default: 100.0)"
        ),
    )
    parser.add_argument(
        "--network-path", default="", help="Shared network root directory (UNC path, e.g. \\\\SERVER\\RealityScan)"
    )
    # Multi-chunk options
    parser.add_argument(
        "--multi-chunk",
        choices=["auto", "force", "disable"],
        default="disable",
        help="Multi-chunk alignment mode: 'disable' (default: load all images into a single unified chunk for global bundle adjustment), 'auto' (create independent chunks for each image subfolder/flight and register them), or 'force'",
    )
    parser.add_argument(
        "--chunk-align-method",
        choices=["camera", "point"],
        default="camera",
        help="Chunk alignment method for multi-chunk mode: 'camera' (method=2, Camera/GPS-based, robust against symmetric structure inversion) or 'point' (method=0, Point-based RANSAC ICP) (default: camera)",
    )
    parser.add_argument(
        "--status-file",
        default=None,
        help="Path to JSON file for real-time IPC stage and progress reporting across cluster nodes",
    )
    parser.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Resume reconstruction from existing project checkpoint if available (default: True). Use --no-resume to force a fresh restart from scratch.",
    )
    return parser
