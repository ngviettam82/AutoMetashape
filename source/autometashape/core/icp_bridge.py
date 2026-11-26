# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Zero-Neural Multi-Island Open3D ICP Bridge & Portable Offline Runtime Provisioner.

Runs inside Agisoft Metashape's bundled Python 3.9+ environment with zero third-party
dependencies. When disconnected overlapping camera islands or unaligned close-range flights exist,
auto-provisions the lightweight portable CPU Python 3.11 + Open3D runtime from the shared cluster
NAS into ``%LOCALAPPDATA%\\AutoMetashape\\bridge_runtime_v*`` once per worker, registers each
island independently via ``icp_worker.py``, enforces a strict Pre-MVS Quality Gate, converts
shifted-UTM Sim(3) transforms into exact ECEF matrices in pure Python, and merges passing islands
via ``mergeChunks(merge_tiepoints=False)``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from typing import Any

from autometashape.core.alignment_hygiene import (
    normalize_flight_takeoff_z0,
    optimize_cameras_no_k4,
    sanitize_camera_gps,
)
from autometashape.core.telemetry import compute_utm_epsg

try:
    import Metashape
except ImportError:
    Metashape = None

_ICP_WORKER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icp_worker.py")
_DEFAULT_RUNTIME_DIRNAME = "bridge_runtime"
_DEFAULT_RUNTIME_SUBPATH = os.path.join(".cluster", "bridge_runtime")


# ---------------------------------------------------------------------------
# Pure-Python 3x3 SVD & Exact Sim(3) Umeyama (Zero NumPy Dependency)
# ---------------------------------------------------------------------------


def _mat3_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _mat3_t(a: list[list[float]]) -> list[list[float]]:
    return [[a[j][i] for j in range(3)] for i in range(3)]


def _mat3_vec(a: list[list[float]], v: list[float]) -> list[float]:
    return [a[i][0] * v[0] + a[i][1] * v[1] + a[i][2] * v[2] for i in range(3)]


def _mat3_det(a: list[list[float]]) -> float:
    return (
        a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
        - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
        + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
    )


def _jacobi_eig_sym3(sym: list[list[float]], max_iter: int = 50) -> tuple[list[float], list[list[float]]]:
    """Compute eigenvalues and column eigenvectors of a real 3x3 symmetric matrix."""
    a = [row[:] for row in sym]
    v = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]

    for _ in range(max_iter):
        # Find largest off-diagonal entry
        p, q = 0, 1
        max_off = abs(a[0][1])
        for i, j in ((0, 2), (1, 2)):
            if abs(a[i][j]) > max_off:
                max_off = abs(a[i][j])
                p, q = i, j
        if max_off < 1e-15:
            break

        diff = a[q][q] - a[p][p]
        if abs(a[p][q]) < abs(diff) * 1e-36:
            t_val = a[p][q] / diff
        else:
            phi = diff / (2.0 * a[p][q])
            t_val = 1.0 / (abs(phi) + math.sqrt(phi * phi + 1.0))
            if phi < 0.0:
                t_val = -t_val
        c_val = 1.0 / math.sqrt(1.0 + t_val * t_val)
        s_val = t_val * c_val
        tau = s_val / (1.0 + c_val)

        apq = a[p][q]
        a[p][q] = 0.0
        a[q][p] = 0.0
        a[p][p] -= t_val * apq
        a[q][q] += t_val * apq
        for r in range(3):
            if r != p and r != q:
                arp = a[r][p]
                arq = a[r][q]
                a[r][p] = arp - s_val * (arq + tau * arp)
                a[p][r] = a[r][p]
                a[r][q] = arq + s_val * (arp - tau * arq)
                a[q][r] = a[r][q]
        for r in range(3):
            vrp = v[r][p]
            vrq = v[r][q]
            v[r][p] = vrp - s_val * (vrq + tau * vrp)
            v[r][q] = vrq + s_val * (vrp - tau * vrq)

    evals = [a[0][0], a[1][1], a[2][2]]
    order = sorted(range(3), key=lambda idx: evals[idx], reverse=True)
    evals_sorted = [evals[i] for i in order]
    v_sorted = [[v[row][i] for i in order] for row in range(3)]
    return evals_sorted, v_sorted


def _svd3(mat: list[list[float]]) -> tuple[list[list[float]], list[float], list[list[float]]]:
    """Compute exact 3x3 SVD ``mat = U @ diag(S) @ Vt`` in pure Python."""
    ata = _mat3_mul(_mat3_t(mat), mat)
    evals, v_mat = _jacobi_eig_sym3(ata)
    s_vals = [math.sqrt(max(ev, 0.0)) for ev in evals]

    u_cols: list[list[float]] = []
    for k in range(3):
        v_col = [v_mat[r][k] for r in range(3)]
        mv = _mat3_vec(mat, v_col)
        if s_vals[k] > 1e-12:
            u_cols.append([x / s_vals[k] for x in mv])
        else:
            u_cols.append([0.0, 0.0, 0.0])

    # Orthonormalize degenerate / near-zero singular columns cleanly
    def _norm(vec: list[float]) -> float:
        return math.sqrt(sum(x * x for x in vec))

    def _dot(v1: list[float], v2: list[float]) -> float:
        return sum(a * b for a, b in zip(v1, v2))

    def _cross(v1: list[float], v2: list[float]) -> list[float]:
        return [
            v1[1] * v2[2] - v1[2] * v2[1],
            v1[2] * v2[0] - v1[0] * v2[2],
            v1[0] * v2[1] - v1[1] * v2[0],
        ]

    for k in range(3):
        if _norm(u_cols[k]) < 1e-9:
            # Pick canonical axis least aligned with prior columns
            best_cand = [1.0, 0.0, 0.0]
            best_len = -1.0
            for axis in ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]):
                cand = axis[:]
                for j in range(k):
                    proj = _dot(cand, u_cols[j])
                    cand = [cand[idx] - proj * u_cols[j][idx] for idx in range(3)]
                clen = _norm(cand)
                if clen > best_len:
                    best_len = clen
                    best_cand = cand
            u_cols[k] = [x / max(best_len, 1e-12) for x in best_cand]
        else:
            for j in range(k):
                proj = _dot(u_cols[k], u_cols[j])
                u_cols[k] = [u_cols[k][idx] - proj * u_cols[j][idx] for idx in range(3)]
            n_k = _norm(u_cols[k])
            if n_k > 1e-12:
                u_cols[k] = [x / n_k for x in u_cols[k]]

    if s_vals[2] <= 1e-12:
        c2 = _cross(u_cols[0], u_cols[1])
        nc2 = _norm(c2)
        if nc2 > 1e-12:
            u_cols[2] = [x / nc2 for x in c2]

    u_mat = [[u_cols[col][row] for col in range(3)] for row in range(3)]
    vt_mat = _mat3_t(v_mat)
    return u_mat, s_vals, vt_mat


def fit_sim3_umeyama_3d(
    src_pts: list[tuple[float, float, float]],
    dst_pts: list[tuple[float, float, float]],
    with_scale: bool = True,
) -> tuple[list[list[float]], float, float]:
    """Fit exact 4x4 Sim(3) transform ``dst ≈ T @ src`` in pure Python.

    Returns:
        Tuple of ``(T_4x4, scale, max_fit_error_m)``.
    """
    n = len(src_pts)
    if n < 3 or len(dst_pts) != n:
        raise ValueError("fit_sim3_umeyama_3d requires >= 3 paired 3D points")

    mu_s = [sum(p[i] for p in src_pts) / n for i in range(3)]
    mu_d = [sum(p[i] for p in dst_pts) / n for i in range(3)]

    xc = [[p[i] - mu_s[i] for i in range(3)] for p in src_pts]
    yc = [[p[i] - mu_d[i] for i in range(3)] for p in dst_pts]

    cov = [[0.0, 0.0, 0.0] for _ in range(3)]
    var_s = 0.0
    for xs, yd in zip(xc, yc):
        var_s += xs[0] * xs[0] + xs[1] * xs[1] + xs[2] * xs[2]
        for i in range(3):
            for j in range(3):
                cov[i][j] += yd[i] * xs[j]
    var_s /= n
    for i in range(3):
        for j in range(3):
            cov[i][j] /= n

    u_mat, s_vals, vt_mat = _svd3(cov)
    sign = [1.0, 1.0, 1.0]
    if _mat3_det(u_mat) * _mat3_det(vt_mat) < 0.0:
        sign[2] = -1.0
    sign_mat = [[sign[0], 0.0, 0.0], [0.0, sign[1], 0.0], [0.0, 0.0, sign[2]]]
    rot = _mat3_mul(_mat3_mul(u_mat, sign_mat), vt_mat)

    if with_scale:
        scale = sum(s_vals[i] * sign[i] for i in range(3)) / max(var_s, 1e-12)
    else:
        scale = 1.0

    r_mu_s = _mat3_vec(rot, mu_s)
    trans = [mu_d[i] - scale * r_mu_s[i] for i in range(3)]

    t_mat = [
        [scale * rot[0][0], scale * rot[0][1], scale * rot[0][2], trans[0]],
        [scale * rot[1][0], scale * rot[1][1], scale * rot[1][2], trans[1]],
        [scale * rot[2][0], scale * rot[2][1], scale * rot[2][2], trans[2]],
        [0.0, 0.0, 0.0, 1.0],
    ]

    max_err = 0.0
    for sp, dp in zip(src_pts, dst_pts):
        pred = [t_mat[i][0] * sp[0] + t_mat[i][1] * sp[1] + t_mat[i][2] * sp[2] + t_mat[i][3] for i in range(3)]
        err = math.sqrt(sum((pred[i] - dp[i]) ** 2 for i in range(3)))
        if err > max_err:
            max_err = err

    return t_mat, scale, max_err


def convert_utm_transform_to_ecef(
    t_utm_4x4: list[list[float]],
    utm_crs: Any,
    shift_xyz: tuple[float, float, float],
) -> tuple[list[list[float]], float]:
    """Convert a shifted-UTM 4x4 Sim(3) transform into an exact ECEF 4x4 Sim(3) matrix."""
    probes = (
        (0.0, 0.0, 0.0),
        (20.0, 0.0, 0.0),
        (0.0, 20.0, 0.0),
        (0.0, 0.0, 20.0),
        (-15.0, 15.0, -5.0),
        (15.0, -15.0, 10.0),
    )
    sx, sy, sz = shift_xyz
    src_ecef: list[tuple[float, float, float]] = []
    dst_ecef: list[tuple[float, float, float]] = []

    for px, py, pz in probes:
        tx = t_utm_4x4[0][0] * px + t_utm_4x4[0][1] * py + t_utm_4x4[0][2] * pz + t_utm_4x4[0][3]
        ty = t_utm_4x4[1][0] * px + t_utm_4x4[1][1] * py + t_utm_4x4[1][2] * pz + t_utm_4x4[1][3]
        tz = t_utm_4x4[2][0] * px + t_utm_4x4[2][1] * py + t_utm_4x4[2][2] * pz + t_utm_4x4[2][3]
        if Metashape is not None and hasattr(Metashape, "Vector"):
            p_src = utm_crs.unproject(Metashape.Vector([px + sx, py + sy, pz + sz]))
            p_dst = utm_crs.unproject(Metashape.Vector([tx + sx, ty + sy, tz + sz]))
        else:
            p_src = utm_crs.unproject((px + sx, py + sy, pz + sz))
            p_dst = utm_crs.unproject((tx + sx, ty + sy, tz + sz))
        src_ecef.append((float(p_src[0]), float(p_src[1]), float(p_src[2])))
        dst_ecef.append((float(p_dst[0]), float(p_dst[1]), float(p_dst[2])))

    t_ecef, _sc, max_err_m = fit_sim3_umeyama_3d(src_ecef, dst_ecef, with_scale=True)
    return t_ecef, max_err_m * 1000.0


# ---------------------------------------------------------------------------
# Pre-MVS Quality Gate
# ---------------------------------------------------------------------------


def evaluate_icp_quality_gate(
    metrics: dict[str, Any],
    max_abs_k4: float = 0.0,
    min_fitness_15cm: float = 0.35,
    max_median_15cm_m: float = 0.10,
    min_scale: float = 0.90,
    max_scale: float = 1.10,
    max_tilt_deg: float = 5.0,
    max_allowed_k4: float = 0.05,
) -> tuple[bool, list[str]]:
    """Evaluate strict Pre-MVS acceptance thresholds for a registered secondary island."""
    reasons: list[str] = []
    if not metrics.get("ok", False):
        reasons.append(f"worker_failed:{metrics.get('reason', 'unknown')}")
        return False, reasons

    fitness = float(metrics.get("fitness_15cm", 0.0))
    med_err = float(metrics.get("median_15cm_m", 999.0))
    scale = float(metrics.get("scale", 0.0))
    tilt_deg = float(metrics.get("tilt_deg", metrics.get("rotation_deg", 999.0)))

    if fitness < min_fitness_15cm:
        reasons.append(f"fitness_15cm={fitness * 100:.1f}% < {min_fitness_15cm * 100:.0f}%")
    if med_err > max_median_15cm_m:
        reasons.append(f"median_15cm={med_err * 100:.2f}cm > {max_median_15cm_m * 100:.0f}cm")
    if not (min_scale <= scale <= max_scale):
        reasons.append(f"scale={scale:.4f} outside [{min_scale:.2f}, {max_scale:.2f}]")
    if tilt_deg > max_tilt_deg:
        reasons.append(f"tilt={tilt_deg:.2f}deg > {max_tilt_deg:.1f}deg")
    if abs(max_abs_k4) >= max_allowed_k4:
        reasons.append(f"|k4|={abs(max_abs_k4):.4f} >= {max_allowed_k4:.2f}")

    return len(reasons) == 0, reasons


# ---------------------------------------------------------------------------
# Portable Offline Open3D Runtime Discovery & Auto-Provisioning
# ---------------------------------------------------------------------------


def _extract_unc_share_root(path: str | None) -> str | None:
    """Extract ``\\\\HOST\\Share`` from a UNC path, or ``None`` if not a UNC path."""
    if not path:
        return None
    norm = path.strip().replace("/", "\\")
    match = re.match(r"^(\\\\[^\\]+\\[^\\]+)", norm)
    return match.group(1) if match else None


def _candidate_bundle_dirs(
    network_path: str = "",
    images_dir: str = "",
    output_dir: str = "",
) -> list[str]:
    """Return ordered candidate directories that may host ``manifest.json`` + runtime zip."""
    candidates: list[str] = []
    env_dir = os.environ.get("AUTOMETASHAPE_BRIDGE_RUNTIME_DIR", "").strip()
    if env_dir:
        candidates.append(env_dir)

    roots: list[str] = []
    if network_path and network_path.strip():
        roots.append(network_path.strip())
    if Metashape is not None and hasattr(Metashape, "app") and hasattr(Metashape.app, "settings"):
        try:
            ms_net = str(getattr(Metashape.app.settings, "network_path", "") or "").strip()
            if ms_net:
                roots.append(ms_net)
        except Exception:
            pass
    for p in (images_dir, output_dir):
        share = _extract_unc_share_root(p)
        if share:
            roots.append(share)

    seen: set[str] = set()
    for root in roots:
        sub = os.path.join(root, _DEFAULT_RUNTIME_SUBPATH)
        key = os.path.normpath(sub).lower()
        if key not in seen:
            seen.add(key)
            candidates.append(sub)
    return candidates


def _local_runtime_base_dir() -> str:
    """Return per-worker local cache directory outside both repo and local scratch staging."""
    override = os.environ.get("AUTOMETASHAPE_BRIDGE_CACHE_DIR", "").strip()
    if override:
        return override
    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        return os.path.join(local_app, "AutoMetashape")
    return os.path.join(os.path.expanduser("~"), ".cache", "AutoMetashape")


def _clean_bridge_env() -> dict[str, str]:
    """Build an isolated subprocess environment that ignores Metashape Python and host DLL paths."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in ("PYTHONPATH", "PYTHONHOME")}
    path_key = next((k for k in env if k.upper() == "PATH"), None)
    if path_key:
        filtered = [seg for seg in env[path_key].split(os.pathsep) if seg and "metashape" not in seg.lower()]
        env[path_key] = os.pathsep.join(filtered)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def _cleanup_stale_icp_temp_dirs(max_age_hours: float = 24.0, temp_root: str | None = None) -> int:
    """Purge orphaned ``autometashape_icp_*`` temp folders left behind by hard-killed workers."""
    root = temp_root or tempfile.gettempdir()
    if not os.path.isdir(root):
        return 0
    cutoff = time.time() - max(0.0, max_age_hours) * 3600.0
    removed = 0
    try:
        for entry in os.listdir(root):
            if not entry.startswith("autometashape_icp_"):
                continue
            full = os.path.join(root, entry)
            if not os.path.isdir(full):
                continue
            try:
                if os.path.getmtime(full) <= cutoff:
                    shutil.rmtree(full, ignore_errors=True)
                    if not os.path.exists(full):
                        removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


def _verify_bridge_python(python_exe: str, timeout_s: float = 30.0) -> bool:
    """Verify that ``python_exe`` can import Open3D, SciPy cKDTree, and NumPy cleanly."""
    if not python_exe or not os.path.isfile(python_exe):
        return False
    try:
        res = subprocess.run(
            [
                python_exe,
                "-s",
                "-c",
                "import numpy, open3d; from scipy.spatial import cKDTree; print(open3d.__version__)",
            ],
            env=_clean_bridge_env(),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return res.returncode == 0
    except Exception:
        return False


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_bridge_runtime(
    network_path: str = "",
    images_dir: str = "",
    output_dir: str = "",
) -> str | None:
    """Locate or auto-provision the isolated portable Python 3.11 + Open3D executable.

    Provisioning unpacks ``<share>\\.cluster\\bridge_runtime\\bridge_runtime_win_amd64_v1.zip``
    once into ``%LOCALAPPDATA%\\AutoMetashape\\bridge_runtime_v<version>`` (outside repo and
    outside local NVMe scratch staging). Requires zero system Python, zero admin rights, and
    zero internet access on worker nodes.
    """
    # 1. Explicit environment override
    env_py = os.environ.get("AUTOMETASHAPE_BRIDGE_PYTHON", "").strip()
    if env_py and _verify_bridge_python(env_py):
        return env_py

    cache_base = _local_runtime_base_dir()

    # 2. Check shared NAS bundle manifest and auto-provision into local cache if needed
    for bundle_dir in _candidate_bundle_dirs(network_path, images_dir, output_dir):
        manifest_path = os.path.join(bundle_dir, "manifest.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            version = str(manifest.get("version", "1.0.0")).strip()
            filename = str(manifest.get("filename", "")).strip()
            py_rel = str(manifest.get("python_relpath", "python.exe")).strip()
            expected_sha = str(manifest.get("sha256", "")).strip().lower()
            zip_path = os.path.join(bundle_dir, filename)
        except Exception as exc:
            print(f"  [ICP Runtime] Warning: unreadable manifest at {manifest_path}: {exc}", flush=True)
            continue

        safe_ver = re.sub(r"[^0-9A-Za-z._-]", "_", version)
        target_dir = os.path.join(cache_base, f"{_DEFAULT_RUNTIME_DIRNAME}_v{safe_ver}")
        target_py = os.path.join(target_dir, py_rel)
        marker_file = os.path.join(target_dir, ".provisioned_sha256")

        if os.path.isfile(target_py):
            marker_ok = True
            if expected_sha and os.path.isfile(marker_file):
                try:
                    with open(marker_file, "r", encoding="utf-8") as mf:
                        marker_ok = mf.read().strip().lower() == expected_sha
                except OSError:
                    marker_ok = False
            if marker_ok and _verify_bridge_python(target_py):
                return target_py

        if not filename or not os.path.isfile(zip_path):
            continue

        print(
            f"  [ICP Runtime] Auto-provisioning portable Open3D runtime v{version} from {zip_path} "
            f"into {target_dir} (one-time per worker)...",
            flush=True,
        )
        t_prov = time.time()
        os.makedirs(cache_base, exist_ok=True)
        stage_dir = os.path.join(cache_base, f".staging_bridge_{os.getpid()}_{int(time.time() * 1000)}")
        try:
            if expected_sha:
                actual_sha = _sha256_file(zip_path).lower()
                if actual_sha != expected_sha:
                    print(
                        f"  [ICP Runtime] [WARNING] Checksum mismatch on {zip_path}: "
                        f"expected {expected_sha[:12]}..., got {actual_sha[:12]}...",
                        flush=True,
                    )
                    continue

            os.makedirs(stage_dir, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(stage_dir)

            staged_py = os.path.join(stage_dir, py_rel)
            if not _verify_bridge_python(staged_py):
                print("  [ICP Runtime] [WARNING] Extracted runtime failed Open3D smoke check.", flush=True)
                shutil.rmtree(stage_dir, ignore_errors=True)
                continue

            if expected_sha:
                with open(os.path.join(stage_dir, ".provisioned_sha256"), "w", encoding="utf-8") as mf:
                    mf.write(expected_sha)

            if os.path.isdir(target_dir):
                # Another concurrent worker may have finished installing the exact same version
                if _verify_bridge_python(target_py):
                    shutil.rmtree(stage_dir, ignore_errors=True)
                    return target_py
                shutil.rmtree(target_dir, ignore_errors=True)

            try:
                os.replace(stage_dir, target_dir)
            except OSError:
                if _verify_bridge_python(target_py):
                    shutil.rmtree(stage_dir, ignore_errors=True)
                    return target_py
                raise

            print(
                f"  [ICP Runtime] [OK] Provisioned portable Open3D runtime v{version} in {time.time() - t_prov:.1f}s.",
                flush=True,
            )
            return target_py
        except Exception as exc:
            print(f"  [ICP Runtime] [WARNING] Provisioning failed ({exc}).", flush=True)
            shutil.rmtree(stage_dir, ignore_errors=True)

    # 3. Fallback: any already-cached verified runtime in local cache_base
    if os.path.isdir(cache_base):
        for entry in sorted(os.listdir(cache_base), reverse=True):
            if entry.startswith(f"{_DEFAULT_RUNTIME_DIRNAME}_v"):
                cand = os.path.join(cache_base, entry, "python.exe")
                if _verify_bridge_python(cand):
                    return cand

    # 4. Developer workstation fallback (.agents/bridge_venv)
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    dev_py = os.path.join(repo_root, ".agents", "bridge_venv", "Scripts", "python.exe")
    if _verify_bridge_python(dev_py):
        return dev_py

    return None


# ---------------------------------------------------------------------------
# Stage-4 Multi-Island Zero-Neural ICP Orchestration
# ---------------------------------------------------------------------------


def _max_chunk_abs_k4(chunk: Any) -> float:
    sensors = getattr(chunk, "sensors", None) or []
    vals = []
    for s in sensors:
        cal = getattr(s, "calibration", None)
        if cal is not None:
            try:
                vals.append(abs(float(getattr(cal, "k4", 0.0))))
            except (TypeError, ValueError):
                pass
    return max(vals) if vals else 0.0


def _clear_stage0_dense_assets(chunk: Any) -> None:
    """Clear temporary Stage-0 downscale=4 depth maps and point cloud before High MVS."""
    for attr in ("depth_maps", "point_cloud"):
        try:
            if getattr(chunk, attr, None) is not None:
                setattr(chunk, attr, None)
        except Exception:
            pass


def _build_stage0_cloud(chunk: Any) -> bool:
    """Build fast downscale=4 uniform-sampled (4 cm) dense cloud purely for ICP registration.

    Using ``uniform_sampling=True, points_spacing=0.04`` cuts PLY size and Open3D peak memory by
    5-10x on large multi-building projects (critical on 32 GB RAM cluster workers) with zero
    accuracy loss because ``icp_worker.py`` downsamples to 5 cm voxels before registration.
    """
    try:
        chunk.buildDepthMaps(
            downscale=4,
            filter_mode=Metashape.FilterMode.ModerateFiltering,
            reuse_depth=False,
        )
        try:
            chunk.buildPointCloud(
                source_data=Metashape.DataSource.DepthMapsData,
                point_colors=True,
                point_confidence=False,
                uniform_sampling=True,
                points_spacing=0.04,
                keep_depth=False,
            )
        except (TypeError, NameError):
            chunk.buildPointCloud(
                source_data=Metashape.DataSource.DepthMapsData,
                point_colors=True,
                point_confidence=False,
                keep_depth=False,
            )
        pc = getattr(chunk, "point_cloud", None)
        return pc is not None and getattr(pc, "point_count", 0) >= 200
    except Exception as exc:
        print(f"  [ICP Bridge] Stage-0 dense cloud failed on '{chunk.label}': {exc}", flush=True)
        return False


def _compute_island_utm_frame(
    island_chunk: Any,
    main_chunk: Any,
) -> tuple[Any, tuple[float, float, float], int] | None:
    """Compute a local shifted UTM CRS centered on ``island_chunk`` aligned cameras."""
    aligned = [c for c in island_chunk.cameras if c.transform and c.enabled]
    if not aligned or island_chunk.transform is None or island_chunk.transform.matrix is None:
        return None
    t_isl = island_chunk.transform.matrix
    ecefs = [t_isl.mulp(c.center) for c in aligned]
    n = len(ecefs)
    origin_ecef = Metashape.Vector(
        [
            sum(p.x for p in ecefs) / n,
            sum(p.y for p in ecefs) / n,
            sum(p.z for p in ecefs) / n,
        ]
    )
    origin_wgs = main_chunk.crs.project(origin_ecef)
    epsg = compute_utm_epsg(float(origin_wgs.x), float(origin_wgs.y))
    utm_crs = Metashape.CoordinateSystem(f"EPSG::{epsg}")
    origin_utm = utm_crs.project(origin_ecef)
    shift_xyz = (round(float(origin_utm.x), 1), round(float(origin_utm.y), 1), round(float(origin_utm.z), 1))
    return utm_crs, shift_xyz, epsg


def _merge_chunks_preserve_poses(doc: Any, chunk_keys: list[int]) -> Any:
    """Merge chunks with merge_tiepoints=False (safe when zero cross-chunk SIFT pairs exist)."""
    doc.mergeChunks(chunks=chunk_keys, merge_tiepoints=False)
    return doc.chunks[-1]


def bridge_disconnected_islands(
    doc: Any,
    chunk: Any,
    audit_result: dict[str, Any],
    unaligned_groups: list[list[int]] | None = None,
    anchor_z0: float | None = None,
    network_path: str = "",
    images_dir: str = "",
    output_dir: str = "",
    psx_out: str = "",
    align_downscale: int = 1,
    rolling_shutter: bool = True,
    filter_stationary_points: bool = True,
    guided_matching: bool = True,
    icp_timeout_s: float = 300.0,
) -> tuple[Any, dict[str, Any]]:
    """Register disconnected overlapping islands and unaligned close-range clusters via CPU Open3D ICP.

    Zero-overhead fast path: returns immediately when no bridge candidates or unaligned overlapping
    groups exist. Temporary PLY exports live strictly in ``tempfile.mkdtemp`` outside
    ``output_dir`` / ``local_staging_dir`` and are always cleaned up in a ``finally:`` block.
    """
    bridge_candidates: list[list[int]] = list(audit_result.get("bridge_candidates") or [])
    rescue_groups: list[list[int]] = list(unaligned_groups or [])

    if not bridge_candidates and not rescue_groups:
        return chunk, {"triggered": False, "accepted": 0, "rejected": 0}

    total_candidates = len(bridge_candidates) + len(rescue_groups)
    print(
        f"\n[ICP Bridge] Triggered for {total_candidates} disconnected overlapping island(s) "
        f"({len(bridge_candidates)} aligned + {len(rescue_groups)} unaligned rescue group(s)).",
        flush=True,
    )

    bridge_py = ensure_bridge_runtime(
        network_path=network_path,
        images_dir=images_dir,
        output_dir=output_dir,
    )
    if not bridge_py:
        print(
            "  [ICP Bridge] [WARNING] Portable Open3D runtime not available. Quarantining "
            "unbridged overlapping islands to prevent double-surface ghosting.",
            flush=True,
        )
        by_key = {c.key: c for c in chunk.cameras}
        quarantined_cnt = 0
        for comp in bridge_candidates:
            for k in comp:
                cam = by_key.get(k)
                if cam is not None:
                    cam.transform = None
                    cam.enabled = False
                    quarantined_cnt += 1
        if quarantined_cnt:
            try:
                optimize_cameras_no_k4(chunk, tiepoint_covariance=True)
            except Exception:
                pass
        return chunk, {
            "triggered": True,
            "runtime_missing": True,
            "accepted": 0,
            "rejected": total_candidates,
            "quarantined_cameras": quarantined_cnt,
        }

    def _cam_identity(cam: Any) -> tuple[str, Any]:
        photo = getattr(cam, "photo", None)
        p = getattr(photo, "path", "") if photo is not None else ""
        if p:
            return ("path", os.path.normcase(os.path.abspath(p)))
        return ("key", int(cam.key))

    # Capture stable path-based identities BEFORE doc.save() (which reloads chunks in Metashape 2.2+)
    pre_by_key = {c.key: c for c in chunk.cameras}
    main_keep_keys: set[int] = set(audit_result.get("main_island") or [])
    for dist_comp in audit_result.get("kept_distant") or []:
        main_keep_keys.update(dist_comp)
    main_keep_ids = {_cam_identity(pre_by_key[k]) for k in main_keep_keys if k in pre_by_key}
    bridge_candidate_ids: list[set[tuple[str, Any]]] = [
        {_cam_identity(pre_by_key[k]) for k in comp if k in pre_by_key} for comp in bridge_candidates
    ]
    rescue_photo_groups: list[list[str]] = []
    for grp in rescue_groups:
        paths = [
            pre_by_key[k].photo.path
            for k in grp
            if k in pre_by_key and getattr(pre_by_key[k], "photo", None) and pre_by_key[k].photo.path
        ]
        if len(paths) >= 12:
            rescue_photo_groups.append(sorted(paths))

    # Ensure document is project-backed before building Stage-0 dense clouds; rebind chunk after save!
    if psx_out and not getattr(doc, "path", ""):
        try:
            doc.save(psx_out)
        except Exception:
            pass

    orig_chunk = getattr(doc, "chunk", None) or (doc.chunks[0] if getattr(doc, "chunks", None) else chunk)

    temp_chunks: list[Any] = []
    accepted_chunks: list[Any] = []
    bridged_cam_paths: set[str] = set()
    island_reports: list[dict[str, Any]] = []

    # Strictly outside local_staging_dir / output_dir so staging sync never uploads temp PLYs
    _cleanup_stale_icp_temp_dirs(max_age_hours=24.0)
    tmp_icp_dir = tempfile.mkdtemp(prefix="autometashape_icp_")
    try:
        # 1. Build clean isolated main Mission chunk (only main_island + kept_distant)
        ch_main = orig_chunk.copy()
        ch_main.label = "_ICP_Main_Block"
        temp_chunks.append(ch_main)
        remove_from_main = [c for c in ch_main.cameras if _cam_identity(c) not in main_keep_ids]
        if remove_from_main:
            ch_main.remove(remove_from_main)
        for _fn in ("updateTransform", "resetRegion"):
            if hasattr(ch_main, _fn):
                try:
                    getattr(ch_main, _fn)()
                except Exception:
                    pass
        optimize_cameras_no_k4(ch_main, tiepoint_covariance=True)
        if hasattr(ch_main, "resetRegion"):
            try:
                ch_main.resetRegion()
            except Exception:
                pass

        print(
            f"  [ICP Bridge] Building Stage-0 Medium (downscale=4) target cloud on main block "
            f"({len(ch_main.cameras)} cameras)...",
            flush=True,
        )
        if not _build_stage0_cloud(ch_main):
            print(
                "  [ICP Bridge] [WARNING] Main block Stage-0 dense cloud failed; quarantining secondary islands.",
                flush=True,
            )
            _clear_stage0_dense_assets(ch_main)
            ch_main.label = "Production_Reconstruction"
            doc.remove([orig_chunk])
            doc.chunk = ch_main
            return ch_main, {"triggered": True, "accepted": 0, "rejected": total_candidates}

        # 2. Prepare all candidate island chunks
        candidate_chunks: list[tuple[str, Any]] = []

        for idx, comp_ids in enumerate(bridge_candidate_ids, 1):
            if not comp_ids:
                continue
            ch_isl = orig_chunk.copy()
            ch_isl.label = f"_ICP_Island_{idx}"
            temp_chunks.append(ch_isl)
            to_remove = [c for c in ch_isl.cameras if _cam_identity(c) not in comp_ids]
            if to_remove:
                ch_isl.remove(to_remove)
            # Disable rolling shutter on isolated close-range/secondary orbits: Regularized RS
            # causes 30-45% bundle-adjustment scale collapse on slow/hovering Manual orbits.
            if Metashape is not None and hasattr(Metashape, "Shutter"):
                for sensor in getattr(ch_isl, "sensors", None) or []:
                    try:
                        sensor.rolling_shutter = Metashape.Shutter.Model.Disabled
                    except Exception:
                        pass
            if hasattr(ch_isl, "updateTransform"):
                try:
                    ch_isl.updateTransform()
                except Exception:
                    pass
            try:
                optimize_cameras_no_k4(ch_isl, tiepoint_covariance=True)
            except Exception as exc:
                print(f"  [ICP Bridge] Island #{idx} optimization note: {exc}", flush=True)
            for _fn in ("updateTransform", "resetRegion"):
                if hasattr(ch_isl, _fn):
                    try:
                        getattr(ch_isl, _fn)()
                    except Exception:
                        pass
            candidate_chunks.append((f"aligned_island_{idx}", ch_isl))

        for idx, paths in enumerate(rescue_photo_groups, 1):
            ch_res = doc.addChunk()
            ch_res.label = f"_ICP_Rescue_{idx}"
            temp_chunks.append(ch_res)
            ch_res.addPhotos(paths)
            # Keep rolling shutter disabled on close-range rescue orbits to prevent scale collapse
            if Metashape is not None and hasattr(Metashape, "Shutter"):
                for sensor in getattr(ch_res, "sensors", None) or []:
                    try:
                        sensor.rolling_shutter = Metashape.Shutter.Model.Disabled
                    except Exception:
                        pass
            sanitize_camera_gps(ch_res)
            normalize_flight_takeoff_z0(ch_res, anchor_z0_override=anchor_z0)
            try:
                ch_res.matchPhotos(
                    downscale=align_downscale,
                    generic_preselection=True,
                    reference_preselection=True,
                    reference_preselection_mode=Metashape.ReferencePreselectionMode.ReferencePreselectionSource,
                    keypoint_limit=60000,
                    keypoint_limit_per_mpx=0,
                    tiepoint_limit=0,
                    filter_stationary_points=filter_stationary_points,
                    guided_matching=guided_matching,
                )
                ch_res.alignCameras(adaptive_fitting=False)
                al_cnt = sum(1 for c in ch_res.cameras if c.transform and c.enabled)
                if al_cnt < 12:
                    print(
                        f"  [ICP Bridge] Rescue group #{idx} aligned only {al_cnt}/{len(ch_res.cameras)} "
                        "cameras (< 12) — skipping.",
                        flush=True,
                    )
                    island_reports.append({"id": f"rescue_{idx}", "passed": False, "reasons": ["insufficient_aligned"]})
                    continue
                optimize_cameras_no_k4(ch_res, tiepoint_covariance=True)
                candidate_chunks.append((f"rescue_{idx}", ch_res))
            except Exception as exc:
                print(f"  [ICP Bridge] Rescue group #{idx} alignment failed ({exc}) — skipping.", flush=True)
                island_reports.append({"id": f"rescue_{idx}", "passed": False, "reasons": [str(exc)]})

        # 3. Register each candidate independently via CPU Open3D Sim(3) ICP + Quality Gate
        for isl_id, ch_isl in candidate_chunks:
            al_cams = [c for c in ch_isl.cameras if c.transform and c.enabled]
            print(
                f"  [ICP Bridge] Registering {isl_id} ({len(al_cams)} aligned cameras)...",
                flush=True,
            )
            if not _build_stage0_cloud(ch_isl):
                island_reports.append({"id": isl_id, "passed": False, "reasons": ["stage0_cloud_failed"]})
                continue

            utm_info = _compute_island_utm_frame(ch_isl, ch_main)
            if utm_info is None:
                island_reports.append({"id": isl_id, "passed": False, "reasons": ["no_utm_frame"]})
                continue
            utm_crs, shift_xyz, epsg = utm_info
            shift_vec = Metashape.Vector(list(shift_xyz))

            mis_ply = os.path.join(tmp_icp_dir, f"{isl_id}_target_utm.ply")
            isl_ply = os.path.join(tmp_icp_dir, f"{isl_id}_source_utm.ply")
            res_json = os.path.join(tmp_icp_dir, f"{isl_id}_result.json")

            try:
                ch_main.exportPointCloud(
                    path=mis_ply,
                    source_data=Metashape.DataSource.PointCloudData,
                    format=Metashape.PointCloudFormat.PointCloudFormatPLY,
                    crs=utm_crs,
                    shift=shift_vec,
                    save_point_normal=True,
                    save_point_color=True,
                    binary=True,
                )
                ch_isl.exportPointCloud(
                    path=isl_ply,
                    source_data=Metashape.DataSource.PointCloudData,
                    format=Metashape.PointCloudFormat.PointCloudFormatPLY,
                    crs=utm_crs,
                    shift=shift_vec,
                    save_point_normal=True,
                    save_point_color=True,
                    binary=True,
                )
            except Exception as exc:
                print(f"  [ICP Bridge] PLY export failed for {isl_id}: {exc}", flush=True)
                island_reports.append({"id": isl_id, "passed": False, "reasons": [f"ply_export:{exc}"]})
                continue

            try:
                proc = subprocess.run(
                    [
                        bridge_py,
                        "-s",
                        _ICP_WORKER_SCRIPT,
                        "--target",
                        mis_ply,
                        "--source",
                        isl_ply,
                        "--output",
                        res_json,
                        "--colored-refine",
                    ],
                    env=_clean_bridge_env(),
                    timeout=icp_timeout_s,
                )
                if proc.returncode != 0 or not os.path.isfile(res_json):
                    island_reports.append({"id": isl_id, "passed": False, "reasons": [f"worker_rc={proc.returncode}"]})
                    continue
                with open(res_json, "r", encoding="utf-8") as f:
                    metrics = json.load(f)
            except Exception as exc:
                print(f"  [ICP Bridge] Worker failed for {isl_id}: {exc}", flush=True)
                island_reports.append({"id": isl_id, "passed": False, "reasons": [f"worker_exc:{exc}"]})
                continue
            finally:
                # Immediately delete per-island temp PLYs to keep disk usage tiny on multi-building projects
                for p in (mis_ply, isl_ply):
                    if os.path.isfile(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass

            max_k4 = _max_chunk_abs_k4(ch_isl)
            passed, reasons = evaluate_icp_quality_gate(metrics, max_abs_k4=max_k4)
            if not passed:
                print(
                    f"  [ICP Quality Gate] REJECTED & QUARANTINED {isl_id}: {', '.join(reasons)}",
                    flush=True,
                )
                island_reports.append({"id": isl_id, "passed": False, "reasons": reasons, "metrics": metrics})
                continue

            t_ecef_list, fit_mm = convert_utm_transform_to_ecef(metrics["T_enu"], utm_crs, shift_xyz)
            print(
                f"  [ICP Quality Gate] PASSED {isl_id} (EPSG::{epsg}, UTM->ECEF={fit_mm:.3f}mm): "
                f"fit(<15cm)={metrics['fitness_15cm'] * 100:.1f}%, "
                f"MedErr={metrics['median_15cm_m'] * 100:.2f}cm, "
                f"scale={metrics['scale']:.5f}, tilt={metrics.get('tilt_deg', 0.0):.2f}deg.",
                flush=True,
            )
            ch_isl.transform.matrix = Metashape.Matrix(t_ecef_list) * ch_isl.transform.matrix
            accepted_chunks.append(ch_isl)
            for cam in ch_isl.cameras:
                if getattr(cam, "photo", None) and cam.photo.path:
                    bridged_cam_paths.add(os.path.normcase(os.path.abspath(cam.photo.path)))
            island_reports.append({"id": isl_id, "passed": True, "fit_mm": fit_mm, "metrics": metrics})

        # 4. Merge accepted islands with ch_main (or keep clean ch_main alone) and clean up temp chunks
        if accepted_chunks:
            merge_keys = [ch_main.key] + [c.key for c in accepted_chunks]
            merged = _merge_chunks_preserve_poses(doc, merge_keys)
            merged.label = "Production_Reconstruction"
            _clear_stage0_dense_assets(merged)

            # Disable noisy GPS reference on bridged island cameras so ICP-locked poses are never disturbed
            disabled_gps = 0
            for cam in merged.cameras:
                if (
                    getattr(cam, "photo", None)
                    and cam.photo.path
                    and os.path.normcase(os.path.abspath(cam.photo.path)) in bridged_cam_paths
                    and getattr(cam, "reference", None) is not None
                ):
                    cam.reference.enabled = False
                    disabled_gps += 1

            doc.remove([orig_chunk] + temp_chunks)
            doc.chunk = merged
            print(
                f"  [ICP Bridge] [OK] Merged main block + {len(accepted_chunks)} verified island(s) "
                f"({len(merged.cameras)} cameras total; locked {disabled_gps} bridged camera GPS refs).",
                flush=True,
            )
            final_chunk = merged
        else:
            _clear_stage0_dense_assets(ch_main)
            ch_main.label = "Production_Reconstruction"
            to_drop = [orig_chunk] + [c for c in temp_chunks if c is not ch_main]
            doc.remove(to_drop)
            doc.chunk = ch_main
            print(
                f"  [ICP Bridge] 0/{len(candidate_chunks)} candidate island(s) passed Quality Gate; "
                "proceeding cleanly with main block only.",
                flush=True,
            )
            final_chunk = ch_main

        if psx_out:
            try:
                doc.save(psx_out)
                final_chunk = getattr(doc, "chunk", None) or (
                    doc.chunks[0] if getattr(doc, "chunks", None) else final_chunk
                )
            except Exception:
                pass

        return final_chunk, {
            "triggered": True,
            "accepted": len(accepted_chunks),
            "rejected": len(island_reports) - len(accepted_chunks),
            "islands": island_reports,
        }
    finally:
        shutil.rmtree(tmp_icp_dir, ignore_errors=True)
