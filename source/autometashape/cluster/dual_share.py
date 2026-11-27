# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Dual Shared Folder (Multi-SSD) Cluster Orchestration.

Enables splitting raw survey photo I/O (Data Source Shared Folder on SSD #1)
from Metashape project checkpoints and deliverables (Result Shared Folder on SSD #2).

In Metashape Network Processing:
- Workers mount `--root` to the Result Shared Folder (SSD #2) so every batch `.psx`
  project file resolves relative to `--root`.
- Raw survey images remain read-only on the Data Source Shared Folder (SSD #1) and
  are read directly over UNC (`//SERVER/SSD1_Data/...`) with zero raw-file duplication.
"""

from __future__ import annotations

import ntpath
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from typing import Any, Dict, List, Optional

from autometashape.cluster.hardware import extract_unc_root

_INVALID_LEAF_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_POSIX_ROOT_DIRS = frozenset(
    {
        "mnt",
        "tmp",
        "var",
        "home",
        "users",
        "volumes",
        "opt",
        "etc",
        "srv",
        "media",
        "data",
        "workspace",
        "root",
        "private",
    }
)


def _nfc(text: str) -> str:
    """Normalize Unicode strings to NFC so macOS NFD paths match Windows/CLI NFC strings."""
    return unicodedata.normalize("NFC", text)


def extract_path_leaf_name(path: str, fallback: str = "Project") -> str:
    """Extract a non-empty sanitized project name from UNC roots, drive roots, or nested folders."""
    raw = str(path or "").strip().rstrip("\\/")
    if not raw:
        return fallback

    # Handle Win32 extended UNC prefix \\?\UNC\server\share and \\?\C:\...
    low = raw.lower()
    if low.startswith("\\\\?\\unc\\") or low.startswith("//?/unc/"):
        raw = "\\\\" + raw[8:]
    elif low.startswith("\\\\?\\") or low.startswith("//?/"):
        raw = raw[4:]

    # Drive root (e.g. "D:", "D:\", "D:/") -> ntpath.basename returns ""
    stripped_drive = raw.rstrip("\\/")
    if _is_windows_drive_path(stripped_drive) and len(stripped_drive) == 2:
        return f"Drive_{stripped_drive[0].upper()}"

    segs = [s for s in stripped_drive.replace("\\", "/").split("/") if s and s not in (".", "..")]
    candidate = segs[-1] if segs else ""
    if candidate and len(candidate) == 2 and candidate[0].isascii() and candidate[0].isalpha() and candidate[1] == ":":
        candidate = f"Drive_{candidate[0].upper()}"
    candidate = _INVALID_LEAF_CHARS_RE.sub("_", candidate).strip(" .")
    return candidate or fallback


def _is_windows_drive_path(path: str) -> bool:
    """Return True if ``path`` starts with an ASCII Windows drive designator (e.g. ``C:\\`` or ``D:/``)."""
    cleaned = (path or "").strip()
    return len(cleaned) >= 2 and cleaned[0].isascii() and cleaned[0].isalpha() and cleaned[1] == ":"


def _unc_escapes_share_root(raw_path: str) -> bool:
    """Return True if a UNC path contains ``..`` segments attempting to escape above ``\\\\server\\share``."""
    cleaned = (raw_path or "").strip()
    if not is_unc_path(cleaned):
        return False
    low = cleaned.lower()
    if low.startswith("\\\\?\\unc\\") or low.startswith("//?/unc/"):
        cleaned = "\\\\" + cleaned[8:]
    parts = [p for p in cleaned.lstrip("\\/").replace("/", "\\").split("\\") if p]
    if len(parts) < 2:
        return True
    depth = 0
    for seg in parts[2:]:
        if seg == ".":
            continue
        if seg == "..":
            if depth == 0:
                return True
            depth -= 1
        else:
            depth += 1
    return False


def _split_drive_and_segments(path: str, win_style: bool) -> tuple[str, List[str]]:
    """Split a normalized path into its drive/UNC share root and non-empty path segments."""
    if win_style:
        win_path = path.replace("/", "\\")
        drv, tail = ntpath.splitdrive(win_path)
        segs = [s for s in tail.split("\\") if s]
        return drv, segs
    drv, tail = os.path.splitdrive(path)
    segs = [s for s in tail.replace("\\", "/").split("/") if s]
    return drv, segs


def is_subpath(child: str, parent: str, case_insensitive: Optional[bool] = None) -> bool:
    """Return True if ``child`` is equal to or nested beneath ``parent``.

    Compares normalized drive/UNC share roots and exact path segments with Windows/SMB
    case-folding and Unicode NFC normalization, eliminating prefix collisions
    (``\\\\SRV\\Data2`` vs ``\\\\SRV\\Data``), UNC ``..`` traversal escapes, and
    cross-platform ``posixpath``/``ntpath`` discrepancies.
    """
    if not child or not parent:
        return False
    if _unc_escapes_share_root(child) or _unc_escapes_share_root(parent):
        return False
    try:
        abs_child = normalize_share_path(child)
        abs_parent = normalize_share_path(parent)
        if not abs_child or not abs_parent:
            return False
        win_style = (
            is_unc_path(abs_child)
            or is_unc_path(abs_parent)
            or _is_windows_drive_path(abs_child)
            or _is_windows_drive_path(abs_parent)
        )
        fold_case = (sys.platform == "win32" or win_style) if case_insensitive is None else case_insensitive
        cmp_child = _nfc(abs_child.lower() if fold_case else abs_child)
        cmp_parent = _nfc(abs_parent.lower() if fold_case else abs_parent)
        drv_c, segs_c = _split_drive_and_segments(cmp_child, win_style=win_style)
        drv_p, segs_p = _split_drive_and_segments(cmp_parent, win_style=win_style)
        if drv_c != drv_p:
            return False
        return len(segs_c) >= len(segs_p) and segs_c[: len(segs_p)] == segs_p
    except ValueError:
        return False


def relpath_under(child: str, parent: str, case_insensitive: Optional[bool] = None) -> Optional[str]:
    """Return POSIX-slash relative path from ``parent`` to ``child`` if nested, else ``None``.

    Preserves original segment casing from ``child`` even when matching case-insensitively
    and across NFC/NFD Unicode representations.
    """
    if not is_subpath(child, parent, case_insensitive=case_insensitive):
        return None
    abs_child = normalize_share_path(child)
    abs_parent = normalize_share_path(parent)
    win_style = (
        is_unc_path(abs_child)
        or is_unc_path(abs_parent)
        or _is_windows_drive_path(abs_child)
        or _is_windows_drive_path(abs_parent)
    )
    _, orig_segs_c = _split_drive_and_segments(abs_child, win_style=win_style)
    _, orig_segs_p = _split_drive_and_segments(abs_parent, win_style=win_style)
    rel_segs = orig_segs_c[len(orig_segs_p) :]
    if any(seg == ".." for seg in rel_segs):
        return None
    return "/".join(rel_segs) if rel_segs else "."


def is_unc_path(path: str) -> bool:
    """Return True if ``path`` is a Windows/POSIX UNC network share path."""
    cleaned = (path or "").strip()
    if not cleaned:
        return False
    low = cleaned.lower()
    if low.startswith("\\\\?\\unc\\") or low.startswith("//?/unc/"):
        rest = [p for p in cleaned[8:].replace("/", "\\").split("\\") if p]
        return len(rest) >= 2
    if low.startswith(("\\\\?\\", "//?/", "\\\\.\\", "//./")):
        return False
    if cleaned.startswith(r"\\"):
        rest = [p for p in cleaned[2:].replace("/", "\\").split("\\") if p]
        return len(rest) >= 1
    if cleaned.startswith("//"):
        rest = [p for p in cleaned[2:].split("/") if p]
        if not rest:
            return False
        if sys.platform == "win32":
            return True
        if rest[0].lower() in _POSIX_ROOT_DIRS or os.path.exists("/" + cleaned.lstrip("/")):
            return False
        return True
    return False


def normalize_share_path(path: str) -> str:
    """Normalize a local, extended Win32, or UNC share path cleanly across platforms."""
    raw = (path or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    if low.startswith("\\\\?\\unc\\") or low.startswith("//?/unc/"):
        body = raw[8:].lstrip("\\/").replace("/", "\\")
        if not body:
            return ""
        normed = ntpath.normpath(f"\\\\{body}")
        return normed.rstrip("\\")
    if low.startswith(("\\\\?\\", "//?/")):
        raw_local = raw[4:].replace("/", "\\")
        if _is_windows_drive_path(raw_local):
            return ntpath.normpath(raw_local)
        return os.path.normpath(raw_local)
    if is_unc_path(raw):
        body = raw.lstrip("\\/").replace("/", "\\")
        if not body:
            return ""
        normed = ntpath.normpath(f"\\\\{body}")
        return normed.rstrip("\\")
    if _is_windows_drive_path(raw):
        return ntpath.normpath(raw.replace("/", "\\"))
    if raw.startswith("//"):
        return os.path.normpath(os.path.abspath("/" + raw.lstrip("/")))
    return os.path.normpath(os.path.abspath(raw))


def resolve_result_network_root(result_share: str, explicit_root: str = "") -> str:
    """Resolve the Metashape cluster ``--root`` strictly from the result share.

    Never derives ``--root`` from ``source_share``, ensuring `.psx` projects and
    cluster batch paths stay rooted on the Result SSD share.
    """
    if explicit_root and explicit_root.strip():
        return normalize_share_path(explicit_root)
    unc_root = extract_unc_root(result_share)
    if unc_root:
        return unc_root
    return normalize_share_path(result_share)


def map_source_to_result_dir(
    source_project_dir: str,
    source_share: str,
    result_share: str,
    project_name: Optional[str] = None,
) -> str:
    """Map a project directory under ``source_share`` to its mirrored folder under ``result_share``.

    - If ``source_project_dir`` is a nested subfolder beneath ``source_share`` (e.g.
      ``\\\\SRV\\SSD1\\BatchA\\Site_01`` under ``\\\\SRV\\SSD1``), mirrors the relative
      path under ``result_share`` (``\\\\SRV\\SSD2\\BatchA\\Site_01``).
    - If ``source_project_dir`` equals ``source_share`` (single-project root), uses
      ``result_share\\<project_name>`` unless ``result_share`` already ends with ``project_name``.
    """
    norm_src_proj = normalize_share_path(source_project_dir)
    norm_src_root = normalize_share_path(source_share)
    norm_res_root = normalize_share_path(result_share)

    if norm_src_root:
        rel = relpath_under(norm_src_proj, norm_src_root)
        if rel not in (None, ".", ""):
            if is_unc_path(norm_res_root) or _is_windows_drive_path(norm_res_root):
                return ntpath.normpath(ntpath.join(norm_res_root, rel.replace("/", "\\")))
            return os.path.normpath(os.path.join(norm_res_root, *rel.split("/")))

    use_nt = is_unc_path(norm_res_root) or _is_windows_drive_path(norm_res_root)
    fold_case = sys.platform == "win32" or use_nt
    inferred_name = (project_name or "").strip() or extract_path_leaf_name(norm_src_proj)
    res_base = ntpath.basename(norm_res_root.rstrip("\\/")) if use_nt else os.path.basename(norm_res_root.rstrip("\\/"))
    if inferred_name:
        cmp_base = _nfc(res_base.lower() if fold_case else res_base)
        cmp_name = _nfc(inferred_name.lower() if fold_case else inferred_name)
        if cmp_base == cmp_name:
            return norm_res_root
    if not inferred_name:
        return norm_res_root
    if use_nt:
        return ntpath.normpath(ntpath.join(norm_res_root, inferred_name))
    return os.path.normpath(os.path.join(norm_res_root, inferred_name))


def validate_dual_share_topology(
    source_share: str,
    result_share: str,
    result_network_root: str = "",
    min_headroom_gb: float = 50.0,
    require_unc: bool = False,
) -> Dict[str, Any]:
    """Pre-flight validation for a Dual Shared Folder (Data Source + Result) topology.

    Checks:
    1. ``source_share`` exists and is readable (strictly read-only check; never requires
       write access or disk headroom on the source photo SSD).
    2. ``result_share`` exists (or can be created), is writable, and satisfies
       ``min_headroom_gb`` (Win32 Error 112 defense on the Result SSD).
    3. ``result_network_root`` contains ``result_share`` so Metashape batch relative paths
       always resolve cleanly against worker ``--root``.
    4. Emits warnings (or errors if ``require_unc=True``) when local drive letters are used
       in a distributed cluster where remote workers require UNC paths.
    """
    errors: List[str] = []
    warnings: List[str] = []

    norm_source = normalize_share_path(source_share)
    norm_result = normalize_share_path(result_share)
    resolved_net_root = resolve_result_network_root(norm_result, result_network_root)

    if not norm_source:
        errors.append("Data source shared folder (source_share) cannot be empty.")
    elif not os.path.isdir(norm_source):
        errors.append(f"Data source shared folder is unreachable or not a directory: {norm_source}")
    else:
        try:
            os.listdir(norm_source)
        except Exception as exc:
            errors.append(f"Data source shared folder is not readable ({norm_source}): {exc}")

    result_free_gb = 0.0
    result_writable = False
    if not norm_result:
        errors.append("Result shared folder (result_share) cannot be empty.")
    else:
        try:
            os.makedirs(norm_result, exist_ok=True)
        except Exception as exc:
            errors.append(f"Cannot create or access result shared folder ({norm_result}): {exc}")

        if os.path.isdir(norm_result):
            probe_fd = None
            probe_path = ""
            try:
                probe_fd, probe_path = tempfile.mkstemp(prefix=".am_write_probe_", dir=norm_result)
                os.close(probe_fd)
                probe_fd = None
                os.remove(probe_path)
                result_writable = True
            except Exception as exc:
                if probe_fd is not None:
                    try:
                        os.close(probe_fd)
                    except Exception:
                        pass
                if probe_path and os.path.exists(probe_path):
                    try:
                        os.remove(probe_path)
                    except Exception:
                        pass
                errors.append(f"Result shared folder is not writable ({norm_result}): {exc}")

            try:
                usage = shutil.disk_usage(norm_result)
                result_free_gb = round(usage.free / (1024**3), 2)
                if min_headroom_gb > 0 and result_free_gb < min_headroom_gb:
                    errors.append(
                        f"Insufficient headroom on result share ({norm_result}): "
                        f"{result_free_gb:.1f} GB free < {min_headroom_gb:.1f} GB threshold."
                    )
            except Exception as exc:
                warnings.append(f"Could not measure free space on result share ({norm_result}): {exc}")

    if resolved_net_root and norm_result and not is_subpath(norm_result, resolved_net_root):
        errors.append(
            f"Result folder '{norm_result}' is not located under Metashape worker root '{resolved_net_root}'."
        )

    if norm_source and norm_result:
        fold_same = (
            sys.platform == "win32"
            or is_unc_path(norm_source)
            or is_unc_path(norm_result)
            or _is_windows_drive_path(norm_source)
            or _is_windows_drive_path(norm_result)
        )
        same_dir = norm_source.lower() == norm_result.lower() if fold_same else norm_source == norm_result
        if same_dir:
            warnings.append(
                f"Data source folder and Result folder point to the same directory ('{norm_source}'). "
                "Use two separate shared folders/SSDs to decouple read-only photo I/O from writable results."
            )
        elif is_subpath(norm_result, norm_source) or is_subpath(norm_source, norm_result):
            warnings.append(
                f"Data source folder ('{norm_source}') and Result folder ('{norm_result}') overlap on the same "
                "directory tree. Separate shares/SSDs are recommended for maximum multi-SSD throughput."
            )

    if norm_source and not is_unc_path(norm_source):
        msg = (
            f"Data source folder '{norm_source}' is a local path, not a UNC share (\\\\SERVER\\Share). "
            "Remote cluster workers will only be able to read photos if they share the same path."
        )
        if require_unc:
            errors.append(msg)
        else:
            warnings.append(msg)

    if norm_result and not is_unc_path(norm_result):
        msg = (
            f"Result folder '{norm_result}' is a local path, not a UNC share (\\\\SERVER\\Share). "
            "Remote cluster workers require a UNC path matching worker --root."
        )
        if require_unc:
            errors.append(msg)
        else:
            warnings.append(msg)

    return {
        "valid": len(errors) == 0,
        "source_share": norm_source,
        "result_share": norm_result,
        "result_network_root": resolved_net_root,
        "result_free_gb": result_free_gb,
        "result_writable": result_writable,
        "errors": errors,
        "warnings": warnings,
    }


def _dir_has_direct_images(directory: str) -> bool:
    """Return True if ``directory`` directly contains at least one non-hidden supported image file."""
    from autometashape.core.telemetry import _is_supported_image_file

    try:
        for entry in os.listdir(directory):
            if _is_supported_image_file(entry) and os.path.isfile(os.path.join(directory, entry)):
                return True
    except Exception:
        return False
    return False


def _dir_contains_images(directory: str, exclude_patterns: tuple[str, ...] = ()) -> bool:
    """Return True as soon as any supported survey image is found under ``directory``."""
    from autometashape.core.telemetry import _is_ignored_survey_dir, _is_supported_image_file

    try:
        for _root, dirs, files in os.walk(directory):
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith("$")
                and not _is_ignored_survey_dir(d)
                and not any(pat in _nfc(d).lower() for pat in exclude_patterns)
            ]
            for fname in files:
                if _is_supported_image_file(fname):
                    return True
    except Exception:
        return False
    return False


def discover_dual_share_projects(
    source_share: str,
    result_share: str,
    preset: str = "pro",
    expected_assets: Optional[List[str]] = None,
    extra_args: Optional[Dict[str, Any]] = None,
    result_network_root: str = "",
    exclude_folders: str = "",
    discovery_depth: int = 1,
    include_all_folders: bool = False,
) -> List[Any]:
    """Discover discrete survey projects under ``source_share`` and map outputs to ``result_share``.

    Supports:
    - Single-project / mixed root+subfolder trees: if ``source_share`` directly contains
      survey photos, queues one unified project preserving both root and subfolder images.
    - Multi-project trees up to ``discovery_depth`` (``0`` = recursive leaf descent):
      descends image-free container directories and mirrors relative hierarchy onto
      ``result_share``, keeping multi-site trees separated.

    Every job has ``extra_args["network_path"]`` pinned to the resolved ``result_network_root``
    (SSD #2), while ``job.images_dir`` retains the full path on ``source_share`` (SSD #1).
    """
    from autometashape.cluster.supervisor import QueueJob
    from autometashape.core.telemetry import parse_exclude_folder_patterns

    norm_source = normalize_share_path(source_share)
    norm_result = normalize_share_path(result_share)
    if not norm_source or not os.path.isdir(norm_source):
        raise ValueError(f"Data source shared folder does not exist or is not a directory: {source_share}")
    if discovery_depth < 0:
        raise ValueError(f"discovery_depth must be >= 0 (got {discovery_depth})")

    resolved_net_root = resolve_result_network_root(norm_result, result_network_root)
    base_extra = dict(extra_args or {})
    if resolved_net_root:
        base_extra["network_path"] = resolved_net_root
    base_extra.setdefault("source_share", norm_source)
    base_extra.setdefault("result_share", norm_result)

    excl_patterns = tuple(_nfc(p) for p in parse_exclude_folder_patterns(exclude_folders))

    excluded_names = {
        "model",
        "models",
        "model_export",
        "model_output",
        "output",
        "outputs",
        "deliverables",
        "result",
        "results",
        "temp",
        "tmp",
        "__pycache__",
    }
    excluded_prefixes = ("output_", "outputs_", "result_", "results_", "temp_", "tmp_")

    if _dir_has_direct_images(norm_source):
        try:
            has_img_sub = any(
                os.path.isdir(os.path.join(norm_source, e))
                and not e.startswith((".", "$"))
                and not e.lower().endswith(".files")
                and _dir_contains_images(os.path.join(norm_source, e), excl_patterns)
                for e in os.listdir(norm_source)
            )
        except Exception:
            has_img_sub = False
        if has_img_sub:
            print(
                f"  [Dual-Share Notice] '{norm_source}' contains root photos alongside subfolder(s); "
                "queuing as one unified project to preserve root photos.",
                flush=True,
            )
        project_name = extract_path_leaf_name(norm_source)
        out_dir = map_source_to_result_dir(
            source_project_dir=norm_source,
            source_share=norm_source,
            result_share=norm_result,
            project_name=project_name,
        )
        return [
            QueueJob(
                images_dir=norm_source,
                output_dir=out_dir,
                project_name=project_name,
                preset=preset,
                expected_assets=expected_assets,
                extra_args=dict(base_extra),
            )
        ]

    jobs: List[QueueJob] = []

    def _scan_dir(current_dir: str, depth: int) -> None:
        try:
            entries = sorted(os.listdir(current_dir))
        except Exception as exc:
            if depth == 1:
                raise ValueError(f"Failed to list data source shared folder '{current_dir}': {exc}") from exc
            return

        for entry in entries:
            full_p = os.path.join(current_dir, entry)
            if not os.path.isdir(full_p) or entry.startswith((".", "$")):
                continue
            entry_l = _nfc(entry).lower()
            if entry_l.endswith(".files"):
                continue
            if excl_patterns and any(pat in entry_l for pat in excl_patterns):
                continue
            if not include_all_folders and (
                entry_l in excluded_names or entry_l.startswith(excluded_prefixes)
            ):
                if _dir_contains_images(full_p, excl_patterns):
                    print(
                        f"  [Dual-Share Notice] Skipped folder '{entry}' matching default exclusion pattern "
                        "(pass include_all_folders=True to include).",
                        flush=True,
                    )
                continue

            if _dir_has_direct_images(full_p):
                out_dir = map_source_to_result_dir(
                    source_project_dir=full_p,
                    source_share=norm_source,
                    result_share=norm_result,
                    project_name=entry,
                )
                jobs.append(
                    QueueJob(
                        images_dir=full_p,
                        output_dir=out_dir,
                        project_name=entry,
                        preset=preset,
                        expected_assets=expected_assets,
                        extra_args=dict(base_extra),
                    )
                )
            elif discovery_depth == 0 or depth < discovery_depth:
                _scan_dir(full_p, depth + 1)
            elif _dir_contains_images(full_p, excl_patterns):
                out_dir = map_source_to_result_dir(
                    source_project_dir=full_p,
                    source_share=norm_source,
                    result_share=norm_result,
                    project_name=entry,
                )
                jobs.append(
                    QueueJob(
                        images_dir=full_p,
                        output_dir=out_dir,
                        project_name=entry,
                        preset=preset,
                        expected_assets=expected_assets,
                        extra_args=dict(base_extra),
                    )
                )

    _scan_dir(norm_source, 1)
    return jobs


def run_dual_share_cluster_pipeline(
    source_share: str,
    result_share: str,
    server_host: str = "127.0.0.1",
    server_port: int = 5840,
    preset: str = "pro",
    ortho_resolution: float = 0.05,
    build_point_cloud: bool = False,
    build_dem: bool = False,
    build_orthomosaic: Optional[bool] = None,
    export_obj: bool = True,
    export_ortho: bool = True,
    export_dem: bool = False,
    export_glb: bool = False,
    export_point_cloud: bool = False,
    export_report: bool = False,
    resume: bool = True,
    local_scratch: bool = True,
    concurrent: bool = True,
    min_headroom_gb: float = 50.0,
    stop_on_failure: bool = False,
    result_network_root: str = "",
    expected_assets: Optional[List[str]] = None,
    extra_args: Optional[Dict[str, Any]] = None,
    execute_fn: Optional[Any] = None,
    poll_interval: float = 2.0,
    exclude_folders: str = "",
    discovery_depth: int = 1,
    include_all_folders: bool = False,
) -> Dict[str, Any]:
    """Execute end-to-end distributed photogrammetry across two separate shared folders (Multi-SSD).

    Reads raw imagery directly from ``source_share`` (SSD #1, read-only) and writes all
    Metashape `.psx` checkpoints and production deliverables to ``result_share`` (SSD #2,
    acting as Metashape cluster ``--root``).
    """
    from autometashape.cluster.supervisor import ClusterSupervisor

    validation = validate_dual_share_topology(
        source_share=source_share,
        result_share=result_share,
        result_network_root=result_network_root,
        min_headroom_gb=min_headroom_gb,
        require_unc=False,
    )
    for w in validation["warnings"]:
        print(f"  [Dual-Share Warning] {w}", flush=True)
    if not validation["valid"]:
        raise ValueError("Dual-share topology validation failed: " + "; ".join(validation["errors"]))

    raw_host = str(server_host).strip()
    if raw_host.startswith("[") and "]" in raw_host:
        bracket_end = raw_host.index("]")
        server_host = raw_host[1:bracket_end]
        rest = raw_host[bracket_end + 1 :]
        if rest.startswith(":") and rest[1:].isdigit():
            server_port = int(rest[1:])
    elif raw_host.count(":") == 1:
        host_part, port_part = raw_host.rsplit(":", 1)
        if port_part.isdigit():
            server_host = host_part
            server_port = int(port_part)

    eff_build_ortho = (True if export_ortho else False) if build_orthomosaic is None else bool(build_orthomosaic)
    merged_extra: Dict[str, Any] = {
        "network_enable": True,
        "network_host": server_host,
        "network_port": int(server_port),
        "network_independent_workers": True,
        "network_path": validation["result_network_root"],
        "ortho_resolution": ortho_resolution,
        "build_point_cloud": build_point_cloud or export_point_cloud,
        "build_dem": build_dem or export_dem,
        "build_orthomosaic": eff_build_ortho,
        "export_obj": export_obj,
        "export_ortho": export_ortho,
        "export_dem": export_dem,
        "export_glb": export_glb,
        "export_point_cloud": export_point_cloud,
        "export_report": export_report,
        "resume": resume,
        "local_scratch": local_scratch,
        "local_scratch_staging": local_scratch,
    }
    if extra_args:
        merged_extra.update(extra_args)

    disc_kwargs: Dict[str, Any] = {}
    if exclude_folders:
        disc_kwargs["exclude_folders"] = exclude_folders
    if discovery_depth != 1:
        disc_kwargs["discovery_depth"] = discovery_depth
    if include_all_folders:
        disc_kwargs["include_all_folders"] = include_all_folders

    jobs = discover_dual_share_projects(
        source_share=validation["source_share"],
        result_share=validation["result_share"],
        preset=preset,
        expected_assets=expected_assets,
        extra_args=merged_extra,
        result_network_root=validation["result_network_root"],
        **disc_kwargs,
    )

    if not jobs:
        return {
            "total_jobs": 0,
            "completed_jobs": 0,
            "failed_jobs": 0,
            "jobs": [],
            "results": [],
            "topology": validation,
        }

    supervisor = ClusterSupervisor(
        min_headroom_gb=min_headroom_gb,
        stop_on_failure=stop_on_failure,
        concurrent=concurrent,
    )
    supervisor.queue.extend(jobs)

    if concurrent:
        summary = supervisor.run_concurrent_queue(execute_fn=execute_fn, poll_interval=poll_interval)
    else:
        summary = supervisor.run_queue(execute_fn=execute_fn, concurrent=False)
    summary["topology"] = validation
    return summary
