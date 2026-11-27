# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Host-initiated autonomous worker fleet remote updater and lifecycle manager."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.parse
import zipfile
from typing import Any

try:
    import Metashape
except ImportError:
    Metashape = None


from autometashape.cluster.abort import dispatch_abort_signal
from autometashape.cluster.hardware import get_local_hostname, is_healthy_online_worker
from autometashape.cluster.orchestrator import _safe_abort_batch

DEFAULT_REPO_ARCHIVE_URL = "https://github.com/ngviettam82/AutoMetashape/archive/refs/heads/main.zip"
CLUSTER_MAINTENANCE_REL_PATH = ".cluster/maintenance.psx"
UPDATE_SIGNAL_FILENAME = ".update_pending"
_PROTECTED_TOP_DIRS = frozenset({".git", ".cluster", ".venv", ".agents", ".idea", ".vscode"})
_PROTECTED_SIGNAL_FILES = frozenset({".update_pending", ".abort_pending"})


def _validate_archive_url(repo_url: str) -> None:
    """Validate update archive URL scheme (HTTPS required unless explicitly allowed for local testing)."""
    parsed = urllib.parse.urlparse(str(repo_url or "").strip())
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    allow_insecure = os.environ.get("AUTOMETASHAPE_ALLOW_INSECURE_UPDATE_URL", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if scheme == "https" and host:
        return
    if scheme == "http" and (host in ("127.0.0.1", "localhost", "::1") or allow_insecure):
        return
    raise ValueError(f"Refusing untrusted or non-HTTPS update archive URL: {repo_url!r}")


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_extract_update_zip(
    zip_path: str,
    extract_dir: str,
    max_files: int = 50000,
    max_bytes: int = 512 * 1024 * 1024,
) -> None:
    """Safely extract update ZIP archive rejecting Zip Slip, symlinks, corruption, and zip bombs."""
    os.makedirs(extract_dir, exist_ok=True)
    real_base = os.path.realpath(extract_dir)
    total_files = 0
    total_bytes = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            raise zipfile.BadZipFile(f"Corrupt ZIP entry: {bad}")
        for info in zf.infolist():
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError(f"Refusing to extract symlink from update archive: {info.filename}")
            clean_rel = info.filename.replace("\\", "/")
            if clean_rel.startswith("/") or os.path.isabs(clean_rel) or os.path.splitdrive(clean_rel)[0]:
                raise ValueError(f"Unsafe absolute path in update archive: {info.filename}")
            parts = [p for p in clean_rel.split("/") if p]
            if not parts or any(p == ".." for p in parts):
                raise ValueError(f"Unsafe traversal path in update archive: {info.filename}")
            target_path = os.path.realpath(os.path.join(real_base, *parts))
            try:
                if os.path.commonpath([real_base, target_path]) != real_base:
                    raise ValueError(f"Archive member escapes target dir: {info.filename}")
            except ValueError as exc:
                raise ValueError(f"Archive member escapes target dir: {info.filename}") from exc
            if info.is_dir():
                os.makedirs(target_path, exist_ok=True)
                continue
            total_files += 1
            total_bytes += max(0, int(info.file_size))
            if total_files > max_files or total_bytes > max_bytes:
                raise ValueError(f"Update archive exceeds safety limit ({total_files} files, {total_bytes} bytes)")
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            with zf.open(info, "r") as src, open(target_path, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _copy2_with_retry(src_file: str, dst_file: str, retries: int = 3) -> None:
    """Copy a file with brief retries on Windows PermissionError (e.g. AV scanner locks)."""
    for attempt in range(retries):
        try:
            shutil.copy2(src_file, dst_file)
            return
        except PermissionError:
            if attempt >= retries - 1:
                raise
            time.sleep(0.25 * (attempt + 1))


def mirror_directory_with_prune(
    src_dir: str,
    dest_dir: str,
    protected_names: set[str] | None = None,
) -> tuple[int, int]:
    """Mirror files from src_dir to dest_dir with 1:1 Git parity, pruning obsolete files.

    Strictly preserves:
    - Machine-local configs (worker_config.json, server_config.json, .gitignore)
    - Runtime logs (*.log), PIDs (*.pid), and flags (*.flag, *.pending, .update_pending, .abort_pending)
    - Subsystems (.cluster/, .git/, .venv/, .agents/, .idea/, .vscode/)

    Returns:
        tuple of (copied_file_count, pruned_file_count)
    """
    if protected_names is None:
        protected_names = {
            "worker_config.json",
            "server_config.json",
            ".gitignore",
        }

    protected_lower = {p.lower() for p in protected_names}
    protected_dirs_lower = {d.lower() for d in _PROTECTED_TOP_DIRS}
    protected_signals_lower = {s.lower() for s in _PROTECTED_SIGNAL_FILES}

    os.makedirs(dest_dir, exist_ok=True)
    src_rel_files_norm: set[str] = set()
    copied = 0
    pruned = 0

    # 1. Walk src_dir and copy/overwrite files (skipping protected top-level dirs & signal files)
    for root, dirs, files in os.walk(src_dir):
        if os.path.abspath(root) == os.path.abspath(src_dir):
            dirs[:] = [d for d in dirs if d.lower() not in protected_dirs_lower]
        for f in files:
            if f.lower() in protected_signals_lower:
                continue
            src_file = os.path.join(root, f)
            rel = os.path.relpath(src_file, src_dir)
            parts = rel.replace("\\", "/").split("/")
            if parts and parts[0].lower() in protected_dirs_lower:
                continue
            src_rel_files_norm.add(os.path.normcase(rel))
            dst_file = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
            # Never overwrite machine-local config if it already exists
            if f.lower() in protected_lower and os.path.isfile(dst_file):
                continue
            _copy2_with_retry(src_file, dst_file)
            copied += 1

    # 2. Walk dest_dir and prune obsolete repository files
    for root, dirs, files in os.walk(dest_dir, topdown=False):
        for f in files:
            dst_file = os.path.join(root, f)
            rel = os.path.relpath(dst_file, dest_dir)
            parts = rel.replace("\\", "/").split("/")
            f_low = f.lower()
            if f_low in protected_lower or f_low in protected_signals_lower:
                continue
            if parts and parts[0].lower() in protected_dirs_lower:
                continue
            if any(p.lower() == "__pycache__" for p in parts):
                continue
            if f_low.endswith((".log", ".pid", ".flag", ".pending")):
                continue
            if os.path.normcase(rel) not in src_rel_files_norm:
                try:
                    os.remove(dst_file)
                    pruned += 1
                except Exception:
                    pass

        # Remove empty directories (except protected ones)
        for d in dirs:
            dir_path = os.path.join(root, d)
            rel_d = os.path.relpath(dir_path, dest_dir)
            d_parts = rel_d.replace("\\", "/").split("/")
            if d_parts and d_parts[0].lower() in protected_dirs_lower:
                continue
            try:
                if os.path.exists(dir_path) and not os.listdir(dir_path):
                    os.rmdir(dir_path)
            except Exception:
                pass

    return copied, pruned


def check_update_pending(target_dir: str, node_name: str | None = None) -> dict | None:
    """Check if an autonomous update was requested via .update_pending signal file.

    Args:
        target_dir: Directory where repository code resides.
        node_name: Optional worker node hostname to check node-specific signal flag.

    Returns:
        Dictionary containing update signal payload, or None if no update is pending.
    """
    candidates = [
        os.path.join(target_dir, UPDATE_SIGNAL_FILENAME),
        os.path.join(tempfile.gettempdir(), UPDATE_SIGNAL_FILENAME),
    ]
    if node_name is None:
        target_node = get_local_hostname().strip().lower()
    else:
        target_node = node_name.strip().lower()
    if target_node:
        candidates.append(os.path.join(tempfile.gettempdir(), f"{UPDATE_SIGNAL_FILENAME}_{target_node}"))

    for flag_path in candidates:
        try:
            with open(flag_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content:
                continue
            import json

            data = json.loads(content)
            if isinstance(data, dict) and (data.get("action") == "autometashape_update" or "repo_url" in data):
                data["_flag_path"] = flag_path
                return data
        except Exception:
            continue
    return None


def perform_worker_codebase_update(signal_data: dict, target_dir: str) -> bool:
    """Download/copy archive, mirror with Git parity and prune, and clear signal file."""
    flag_path = signal_data.get("_flag_path")
    if not flag_path:
        flag_path = os.path.join(target_dir, UPDATE_SIGNAL_FILENAME)
    repo_url = signal_data.get("repo_url") or DEFAULT_REPO_ARCHIVE_URL
    shared_cluster_dir = signal_data.get("shared_cluster_dir", "")

    temp_dir = tempfile.mkdtemp(prefix="autometashape_update_")
    zip_path = os.path.join(temp_dir, "update.zip")
    extract_dir = os.path.join(temp_dir, "extracted")

    try:
        acquired = False
        expected_sha = str(signal_data.get("sha256") or "").strip().lower()
        # 1. Prefer shared LAN payload cache (verify ZIP integrity + SHA-256 before accepting)
        if shared_cluster_dir and os.path.isdir(shared_cluster_dir):
            lan_zip = os.path.join(shared_cluster_dir, "update_payload.zip")
            lan_meta = os.path.join(shared_cluster_dir, "update_payload.meta.json")
            meta_sha = ""
            if os.path.isfile(lan_meta):
                try:
                    with open(lan_meta, "r", encoding="utf-8") as mf:
                        meta_obj = json.load(mf)
                    if isinstance(meta_obj, dict):
                        meta_sha = str(meta_obj.get("sha256") or "").strip().lower()
                except Exception:
                    meta_sha = ""
            check_sha = expected_sha or meta_sha
            if os.path.isfile(lan_zip) and os.path.getsize(lan_zip) > 1024:
                try:
                    shutil.copy2(lan_zip, zip_path)
                    if os.path.isfile(zip_path) and os.path.getsize(zip_path) > 1024:
                        if check_sha and _sha256_file(zip_path).lower() != check_sha:
                            acquired = False
                        else:
                            with zipfile.ZipFile(zip_path, "r") as zf:
                                if zf.testzip() is None:
                                    acquired = True
                except Exception:
                    acquired = False

        # 2. Fallback to direct HTTPS URL download
        if not acquired:
            import urllib.request

            _validate_archive_url(repo_url)
            req = urllib.request.Request(repo_url, headers={"User-Agent": "AutoMetashape-Supervisor/1.0"})
            with urllib.request.urlopen(req, timeout=45) as resp, open(zip_path, "wb") as out_f:
                shutil.copyfileobj(resp, out_f)
            if expected_sha and _sha256_file(zip_path).lower() != expected_sha:
                raise RuntimeError("Downloaded update archive failed SHA-256 integrity check")

        # 3. Extract zip safely (rejects Zip Slip, symlinks, corruption, and zip bombs)
        _safe_extract_update_zip(zip_path, extract_dir)

        # 4. Resolve extracted root and verify payload structure before pruning target_dir
        src_dir = extract_dir
        for d in os.listdir(extract_dir):
            cand = os.path.join(extract_dir, d)
            if os.path.isdir(cand) and (
                os.path.exists(os.path.join(cand, "launch_worker.bat"))
                or os.path.exists(os.path.join(cand, "pyproject.toml"))
            ):
                src_dir = cand
                break

        if not (
            os.path.isfile(os.path.join(src_dir, "scripts", "launch_worker.py"))
            or os.path.isfile(os.path.join(src_dir, "pyproject.toml"))
            or os.path.isfile(os.path.join(src_dir, "launch_worker.bat"))
        ):
            raise RuntimeError(f"Extracted update archive at '{src_dir}' does not resemble an AutoMetashape repository")

        # 5. Mirror with prune
        mirror_directory_with_prune(src_dir, target_dir)

        # 6. Clean up flag
        if os.path.isfile(flag_path):
            try:
                os.remove(flag_path)
            except Exception:
                pass
        return True

    except Exception as exc:
        print(f"[SUPERVISOR ERROR] Codebase update failed: {exc}", file=sys.stderr, flush=True)
        # Clear flag on error to prevent infinite restart loop
        if os.path.isfile(flag_path):
            try:
                os.remove(flag_path)
            except Exception:
                pass
        return False
    finally:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def _read_payload_meta_sha256(shared_root: str) -> str:
    """Read cached SHA-256 hex digest from ``.cluster/update_payload.meta.json`` if present."""
    if not shared_root:
        return ""
    meta_path = os.path.join(shared_root, ".cluster", "update_payload.meta.json")
    if not os.path.isfile(meta_path):
        return ""
    try:
        with open(meta_path, "r", encoding="utf-8") as mf:
            obj = json.load(mf)
        return str(obj.get("sha256") or "").strip().lower() if isinstance(obj, dict) else ""
    except Exception:
        return ""


def generate_worker_updater_code(
    repo_url: str = DEFAULT_REPO_ARCHIVE_URL,
    expected_hostname: str = "",
    shared_cluster_dir: str = "",
    expected_sha256: str = "",
) -> str:
    """Generate lightweight Python code executed on the remote worker node via RunScript.

    Writes the .update_pending signal file and shared status so the local persistent
    supervisor (launch_worker.py) can safely pull the codebase, mirror with Git-parity,
    and re-execute its process image upon quitWorker.
    """
    clean_expected = expected_hostname.strip().lower()
    clean_sha = expected_sha256.strip().lower()
    return f"""import json
import os
import socket
import sys
import tempfile
import time

log = []
node_name = (os.environ.get("COMPUTERNAME") or "").strip() or socket.gethostname() or "unknown"
node_low = node_name.strip().lower()
node_short = node_low.split(".", 1)[0]
expected_node = {clean_expected!r}
if expected_node and expected_node != "unknown":
    exp_short = expected_node.split(".", 1)[0]
    if node_low != expected_node and node_short != exp_short:
        err_msg = f"Target node mismatch: task assigned to {{node_name}}, expected {{expected_node}}"
        log.append(f"FATAL: {{err_msg}}")
        raise RuntimeError(err_msg)

log.append(f"Received worker update trigger on {{node_name}} at {{time.ctime()}}")
log.append(f"Python: {{sys.executable}}")

# 1. Resolve target directory: prioritize AUTOMETASHAPE_DIR env from launcher, fallback to Documents/AutoMetashape
env_dir = os.environ.get("AUTOMETASHAPE_DIR", "").strip()
user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
default_dir = os.path.join(user_profile, "Documents", "AutoMetashape")
has_known_dir = bool(env_dir and os.path.isdir(env_dir))
target_dir = env_dir if has_known_dir else default_dir
if not has_known_dir and os.path.isdir(default_dir):
    has_known_dir = True
os.makedirs(target_dir, exist_ok=True)
log.append(f"Target AutoMetashape directory: {{target_dir}}")

# 2. Write .update_pending signal file atomically in target directory (plus temp fallback for custom dirs)
flag_file = os.path.join(target_dir, {UPDATE_SIGNAL_FILENAME!r})
shared_dir = {shared_cluster_dir!r}
flag_data = {{
    "action": "autometashape_update",
    "node_name": node_name,
    "repo_url": {repo_url!r},
    "shared_cluster_dir": shared_dir,
    "timestamp": time.time(),
    "target_dir": target_dir,
}}
expected_sha = {clean_sha!r}
if expected_sha:
    flag_data["sha256"] = expected_sha
signal_targets = [flag_file]
if not has_known_dir:
    signal_targets.append(os.path.join(tempfile.gettempdir(), f"{UPDATE_SIGNAL_FILENAME}_{{node_short}}"))
for dest_flag in signal_targets:
    tmp_flag = f"{{dest_flag}}.tmp.{{os.getpid()}}"
    with open(tmp_flag, "w", encoding="utf-8") as f:
        json.dump(flag_data, f, indent=2)
    os.replace(tmp_flag, dest_flag)
log.append(f"Registered update signal file: {{flag_file}}")

# 3. Write status to shared cluster directory for coordinator inspection
written = False
if shared_dir and os.path.isdir(shared_dir):
    try:
        res_file = os.path.join(shared_dir, f"{{node_name}}_update_result.txt")
        with open(res_file, "w", encoding="utf-8") as rf:
            rf.write("\\n".join(log) + "\\n")
        written = True
    except Exception:
        pass

if not written:
    try:
        doc = getattr(getattr(sys.modules.get("Metashape"), "app", None), "document", None)
        if doc and getattr(doc, "path", None):
            out_dir = os.path.dirname(doc.path)
            res_file = os.path.join(out_dir, f"{{node_name}}_update_result.txt")
            with open(res_file, "w", encoding="utf-8") as rf:
                rf.write("\\n".join(log) + "\\n")
    except Exception:
        pass
"""


def stage_cluster_update_payload(
    shared_root: str,
    repo_url: str = DEFAULT_REPO_ARCHIVE_URL,
    force_refresh: bool = False,
) -> str:
    """Stages or caches the repository update archive zip on shared storage so workers can update over LAN.

    Returns:
        Absolute path to update_payload.zip on shared network storage, or empty string on failure.
    """
    if not shared_root:
        return ""
    cluster_dir = os.path.join(shared_root, ".cluster")
    os.makedirs(cluster_dir, exist_ok=True)
    payload_zip = os.path.join(cluster_dir, "update_payload.zip")
    payload_meta = os.path.join(cluster_dir, "update_payload.meta.json")

    # Reuse cache only when not force-refreshing, repo_url matches, and 0 <= age < 300s
    needs_download = True
    if not force_refresh and os.path.isfile(payload_zip):
        try:
            age = time.time() - os.path.getmtime(payload_zip)
            cached_url = ""
            if os.path.isfile(payload_meta):
                with open(payload_meta, "r", encoding="utf-8") as mf:
                    cached_url = str((json.load(mf) or {}).get("repo_url", ""))
            if (
                0.0 <= age < 300.0
                and os.path.getsize(payload_zip) > 1024
                and (not cached_url or cached_url == repo_url)
            ):
                needs_download = False
        except Exception:
            pass

    if needs_download:
        import urllib.request

        tmp_zip = f"{payload_zip}.tmp.{os.getpid()}"
        try:
            _validate_archive_url(repo_url)
            req = urllib.request.Request(repo_url, headers={"User-Agent": "AutoMetashape-Coordinator/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(tmp_zip, "wb") as out_f:
                shutil.copyfileobj(resp, out_f)
            # Validate zip integrity before publishing atomically to shared cache
            with zipfile.ZipFile(tmp_zip, "r") as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise zipfile.BadZipFile(f"Corrupt entry in archive: {bad}")
            payload_sha = _sha256_file(tmp_zip)
            os.replace(tmp_zip, payload_zip)
            tmp_meta = f"{payload_meta}.tmp.{os.getpid()}"
            try:
                with open(tmp_meta, "w", encoding="utf-8") as mf:
                    json.dump(
                        {"repo_url": repo_url, "sha256": payload_sha, "staged_at": time.time()},
                        mf,
                        indent=2,
                    )
                os.replace(tmp_meta, payload_meta)
            except OSError:
                if os.path.isfile(tmp_meta):
                    try:
                        os.remove(tmp_meta)
                    except OSError:
                        pass
        except Exception as exc:
            print(
                f"[WARNING] Failed to refresh shared update payload from {repo_url}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            if os.path.isfile(tmp_zip):
                try:
                    os.remove(tmp_zip)
                except OSError:
                    pass
            # Do not serve a stale/mismatched cached ZIP when an explicit refresh failed
            if force_refresh:
                return ""
            if os.path.isfile(payload_meta):
                try:
                    with open(payload_meta, "r", encoding="utf-8") as mf:
                        if str((json.load(mf) or {}).get("repo_url", "")) != repo_url:
                            return ""
                except Exception:
                    return ""

    return payload_zip if os.path.isfile(payload_zip) else ""


def _sanitize_worker_slug(worker_slug: str) -> str:
    """Convert a worker hostname or ID into a filesystem-safe ASCII slug."""
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(worker_slug or "").strip())
    return cleaned.strip("_").lower()


def _abort_stale_maintenance_batches(client, rel_project: str) -> None:
    """Abort any lingering cluster batch referencing ``rel_project`` to prevent Duplicate batch errors."""
    norm_target = rel_project.replace("\\", "/").strip("/").lower()
    if hasattr(client, "batchList") and hasattr(client, "abortBatch"):
        try:
            bl = client.batchList()
            if isinstance(bl, dict):
                for b in bl.get("batches", []):
                    bpath = str(b.get("path") or "").replace("\\", "/").strip("/").lower()
                    bid = b.get("batch_id")
                    if bid is not None and bpath == norm_target:
                        is_paused = bool(b.get("paused")) or str(b.get("state", "")).lower() == "paused"
                        _safe_abort_batch(client, bid, is_paused=is_paused)
        except Exception:
            pass


def ensure_cluster_maintenance_project(shared_root: str, worker_slug: str = "") -> str:
    """Ensure a valid Metashape maintenance .psx exists on shared storage and clean stale locks."""
    cluster_dir = os.path.join(shared_root, ".cluster")
    os.makedirs(cluster_dir, exist_ok=True)
    slug = _sanitize_worker_slug(worker_slug)
    base_name = f"maintenance_{slug}" if slug else "maintenance"
    rel_path = f".cluster/{base_name}.psx" if slug else CLUSTER_MAINTENANCE_REL_PATH
    psx_path = os.path.join(cluster_dir, f"{base_name}.psx")
    files_dir = os.path.join(cluster_dir, f"{base_name}.files")
    os.makedirs(files_dir, exist_ok=True)

    # Pre-clean any lingering local/network lock files from previous interrupted runs
    lock_file = os.path.join(files_dir, "lock")
    if os.path.isfile(lock_file):
        try:
            os.remove(lock_file)
        except Exception:
            pass

    project_zip = os.path.join(files_dir, "project.zip")
    if not os.path.isfile(psx_path) or not os.path.isfile(project_zip):
        if Metashape is not None and hasattr(Metashape, "Document"):
            doc = None
            try:
                doc = Metashape.Document()
                doc.save(psx_path)
                if os.path.isfile(psx_path):
                    return rel_path
            except Exception:
                pass
            finally:
                if doc is not None and hasattr(doc, "clear"):
                    try:
                        doc.clear()
                    except Exception:
                        pass
        # Fallback: write BOTH the .psx XML manifest AND a valid minimal project.zip archive
        xml_content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<document version="1.2.0" path="{base_name}.files/project.zip"/>\n'
        )
        with open(psx_path, "w", encoding="utf-8") as f:
            f.write(xml_content)
        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<document version="1.2.0">\n  <chunks next_id="0"/>\n</document>\n'
        )
        with zipfile.ZipFile(project_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("doc.xml", doc_xml)
    return rel_path


def list_online_workers(client) -> list[dict]:
    """Retrieve list of currently connected, non-aborted workers from Metashape Server."""
    if not client:
        return []
    info = client.workerList()
    if isinstance(info, dict):
        return [w for w in info.get("workers", []) if is_healthy_online_worker(w)]
    return []


def _extract_host_ip(hostaddr: str) -> str:
    """Extract host/IP portion from 'host:port' (or '[ipv6]:port') string."""
    raw = str(hostaddr or "").strip()
    if not raw:
        return ""
    if raw.startswith("[") and "]" in raw:
        return raw[1 : raw.index("]")].strip().lower()
    return raw.rsplit(":", 1)[0].strip().lower() if ":" in raw else raw.lower()


def update_remote_worker(
    client,
    worker_id_or_name: str | int,
    shared_root: str,
    repo_url: str = DEFAULT_REPO_ARCHIVE_URL,
    timeout: float = 60.0,
    restart: bool = True,
    poll_interval: float = 2.0,
    force: bool = True,
) -> dict:
    """Dispatches remote update to a specific worker node with cluster isolation and force-abort capability.

    Args:
        client: Connected Metashape NetworkClient instance.
        worker_id_or_name: Worker ID, hostname, or IP address substring of the target node.
        shared_root: Shared network storage root containing .cluster maintenance files.
        repo_url: URL to repository archive zip.
        timeout: Maximum timeout in seconds to wait for update batch completion.
        restart: Whether to signal quitWorker for automatic daemon relaunch.
        poll_interval: Seconds between status polls.
        force: If True (default), aborts any active reconstruction processing on the worker so it can update
            immediately. If False, skips busy workers to preserve ongoing processing.

    Returns:
        Dictionary detailing success status, batch ID, relaunch confirmation, logs, or error details.
    """
    result = {
        "success": False,
        "worker_id": None,
        "hostname": None,
        "hostaddr": None,
        "batch_id": None,
        "relaunched": False,
        "new_worker_id": None,
        "new_hostaddr": None,
        "log": "",
        "error": None,
    }

    if Metashape is None or client is None:
        result["error"] = "Metashape Python API is not available"
        return result

    workers = list_online_workers(client)
    target = None
    target_str = str(worker_id_or_name).strip().lower()
    for w in workers:
        wid = str(w.get("worker_id", ""))
        hname = str(w.get("hostname", "")).lower()
        haddr = str(w.get("hostaddr", "")).lower()
        hip = _extract_host_ip(haddr)
        if target_str == wid or target_str == hname or target_str == haddr or (hip and target_str == hip):
            target = w
            break

    if not target:
        result["error"] = f"Target worker '{worker_id_or_name}' not found among online workers"
        return result

    wid = target.get("worker_id")
    result["worker_id"] = wid
    result["hostname"] = target.get("hostname")
    result["hostaddr"] = target.get("hostaddr")
    node_hname = result["hostname"] or str(worker_id_or_name)

    paused_existing_batches: list[int] = []
    other_workers_paused: dict[int | str, bool] = {}
    orig_target_paused = bool(target.get("paused", False))

    # Stage fresh update payload BEFORE pausing any batches or workers
    cluster_shared_dir = os.path.join(shared_root, ".cluster")
    stage_cluster_update_payload(shared_root=shared_root, repo_url=repo_url, force_refresh=True)
    payload_sha = _read_payload_meta_sha256(shared_root)

    try:
        # Pre-flight Check: Is target worker currently busy?
        target_state = str(target.get("state", "")).lower()
        if target_state not in ("ready", ""):
            if not force:
                result["error"] = (
                    f"Target worker '{node_hname}' (ID={wid}) is currently {target_state}. "
                    "Skipping update in safe mode (enable force to abort active processing)."
                )
                return result

            # FORCE MODE: Abort active processing ONLY on target worker to allow immediate update
            target_bid = target.get("batch_id")
            batches_snapshot: list[dict] = []
            if hasattr(client, "batchList"):
                try:
                    bl = client.batchList()
                    if isinstance(bl, dict):
                        batches_snapshot = list(bl.get("batches", []))
                except Exception:
                    batches_snapshot = []

            # If workerList() omitted batch_id, identify the specific active batch running on `wid`
            if target_bid is None and batches_snapshot:
                active_inprog = [
                    b
                    for b in batches_snapshot
                    if str(b.get("state", "")).lower() in ("inprogress", "running", "active")
                    and b.get("batch_id") is not None
                ]
                if hasattr(client, "batchInfo"):
                    for b in active_inprog:
                        bid = b.get("batch_id")
                        try:
                            binfo = client.batchInfo(bid)
                            if isinstance(binfo, dict):
                                if any(
                                    isinstance(t, dict) and t.get("worker_id") == wid for t in binfo.get("tasks", [])
                                ):
                                    target_bid = bid
                                    break
                        except Exception:
                            pass
                if target_bid is None and len(active_inprog) == 1:
                    target_bid = active_inprog[0].get("batch_id")

            # 1. Temporarily pause unpaused queued/active batches (EXCEPT target_bid, never unpausing operator-paused work)
            if batches_snapshot and hasattr(client, "setBatchPaused"):
                try:
                    for b in batches_snapshot:
                        bid = b.get("batch_id")
                        bstate = str(b.get("state", "")).lower()
                        if (
                            bid != target_bid
                            and not b.get("paused")
                            and bstate in ("queued", "inprogress", "running", "active", "submitted", "pending")
                        ):
                            client.setBatchPaused(bid, True)
                            paused_existing_batches.append(bid)
                except Exception:
                    pass

            # 2. Unpause (if paused) and abort ONLY the active batch running on target worker
            if hasattr(client, "abortBatch") and target_bid is not None:
                try:
                    if hasattr(client, "setBatchPaused"):
                        client.setBatchPaused(target_bid, False)
                except Exception:
                    pass
                try:
                    client.abortBatch(target_bid)
                except Exception:
                    pass

            # 3. Wait for target worker to transition to 'ready'
            abort_start = time.time()
            target_ready = False
            while time.time() - abort_start < 10.0:
                time.sleep(1.0)
                refreshed = list_online_workers(client)
                for rw in refreshed:
                    if (
                        rw.get("worker_id") == wid or str(rw.get("hostname", "")).lower() == str(node_hname).lower()
                    ) and str(rw.get("state", "")).lower() == "ready":
                        target_ready = True
                        wid = rw.get("worker_id")
                        result["worker_id"] = wid
                        break
                if target_ready:
                    break

            # If still not ready after abortBatch, send watchdog .abort signal FIRST and await fresh
            # worker reconnect before falling back to quitWorker (prevents double-restart races).
            if not target_ready:
                pre_abort_ids = {
                    w.get("worker_id")
                    for w in list_online_workers(client)
                    if str(w.get("hostname", "")).strip().lower() == str(node_hname).strip().lower()
                    and w.get("worker_id") is not None
                }
                if wid is not None:
                    pre_abort_ids.add(wid)

                abort_sig_path = ""
                if shared_root and os.path.isdir(shared_root) and node_hname:
                    try:
                        abort_sig_path = dispatch_abort_signal(
                            node_name=str(node_hname), batch_id=target_bid, shared_root=shared_root
                        )
                    except Exception:
                        abort_sig_path = ""

                # Phase A: wait up to 10s for watchdog .abort signal to cleanly reap & respawn worker
                if abort_sig_path:
                    wd_deadline = time.time() + 10.0
                    while time.time() < wd_deadline:
                        time.sleep(min(poll_interval, 1.0))
                        try:
                            refreshed = list_online_workers(client)
                        except Exception:
                            continue
                        for rw in refreshed:
                            rw_id = rw.get("worker_id")
                            if (
                                str(rw.get("hostname", "")).strip().lower() == str(node_hname).strip().lower()
                                and str(rw.get("state", "")).strip().lower() == "ready"
                                and (rw_id is None or rw_id not in pre_abort_ids)
                            ):
                                wid = rw_id
                                result["worker_id"] = wid
                                result["hostname"] = rw.get("hostname")
                                result["hostaddr"] = rw.get("hostaddr")
                                target_ready = True
                                break
                        if target_ready:
                            break

                # Phase B: fallback to quitWorker only if watchdog did not complete within grace window
                if not target_ready and hasattr(client, "quitWorker"):
                    if abort_sig_path and os.path.isfile(abort_sig_path):
                        try:
                            os.remove(abort_sig_path)
                        except OSError:
                            pass
                    try:
                        client.quitWorker(wid)
                    except Exception:
                        pass
                    reboot_deadline = time.time() + 25.0
                    while time.time() < reboot_deadline:
                        time.sleep(poll_interval)
                        try:
                            refreshed = list_online_workers(client)
                        except Exception:
                            continue
                        for rw in refreshed:
                            rw_id = rw.get("worker_id")
                            if (
                                str(rw.get("hostname", "")).strip().lower() == str(node_hname).strip().lower()
                                and str(rw.get("state", "")).strip().lower() == "ready"
                                and (rw_id is None or rw_id not in pre_abort_ids)
                            ):
                                wid = rw_id
                                result["worker_id"] = wid
                                result["hostname"] = rw.get("hostname")
                                result["hostaddr"] = rw.get("hostaddr")
                                target_ready = True
                                break
                        if target_ready:
                            break

            if not target_ready:
                result["error"] = f"Failed to force worker '{node_hname}' (ID={wid}) into ready state after abort"
                return result

        # 4. Cluster Isolation: Temporarily pause all OTHER workers and record ALL pause states for restoration
        current_fleet = list_online_workers(client) if target_state not in ("ready", "") else workers
        for w in current_fleet:
            w_id = w.get("worker_id")
            other_workers_paused[w_id] = orig_target_paused if w_id == wid else bool(w.get("paused", False))
            if hasattr(client, "setWorkerPaused"):
                try:
                    client.setWorkerPaused(w_id, w_id != wid)
                except Exception:
                    pass

        # 5. Ensure per-worker maintenance project
        rel_project = ensure_cluster_maintenance_project(shared_root, worker_slug=f"{node_hname}_{wid}")

        # 6. Build RunScript task locked to target worker's hostname and shared LAN cache
        task = Metashape.Tasks.RunScript()
        task.code = generate_worker_updater_code(
            repo_url=repo_url,
            expected_hostname=result["hostname"] or "",
            shared_cluster_dir=cluster_shared_dir,
            expected_sha256=payload_sha,
        )
        net_task = task.toNetworkTask()

        # 7. Abort any stale maintenance batch on rel_project and create new batch locked to 1 worker
        _abort_stale_maintenance_batches(client, rel_project)
        try:
            batch_id = client.createBatch(rel_project, [net_task])
            result["batch_id"] = batch_id
            if hasattr(client, "setBatchWorkerLimit"):
                try:
                    client.setBatchWorkerLimit(batch_id, 1)
                except Exception:
                    pass
            client.setBatchPaused(batch_id, False)
        except Exception as e:
            result["error"] = f"Failed to dispatch update batch: {e}"
            return result

        # 8. Wait for batch completion (detecting Metashape 2.2.2 infinite-retry error_count > 0!)
        start_t = time.time()
        batch_done = False
        while time.time() - start_t < timeout:
            try:
                batches_info = client.batchList()
            except Exception:
                time.sleep(poll_interval)
                continue
            batch_entry = None
            if isinstance(batches_info, dict):
                for b in batches_info.get("batches", []):
                    if b.get("batch_id") == batch_id:
                        batch_entry = b
                        break
            if batch_entry:
                state = str(batch_entry.get("state", "")).lower()
                err_cnt = int(batch_entry.get("error_count", 0) or 0)
                is_failed = bool(batch_entry.get("failed") or batch_entry.get("aborted") or err_cnt > 0)
                if is_failed or state in ("failed", "aborted", "cancelled"):
                    if state not in ("completed", "finished", "failed", "aborted", "cancelled") and hasattr(
                        client, "abortBatch"
                    ):
                        try:
                            client.abortBatch(batch_id)
                        except Exception:
                            pass
                    last_err = batch_entry.get("last_error")
                    err_detail = (
                        last_err.get("message")
                        if isinstance(last_err, dict) and last_err.get("message")
                        else f"state={state}, error_count={err_cnt}"
                    )
                    result["error"] = f"Update batch failed on worker ({err_detail})"
                    return result
                if state in ("completed", "finished"):
                    batch_done = True
                    break
            time.sleep(poll_interval)

        if not batch_done:
            # Abort the timed-out batch on the cluster server so it does not linger in the queue
            if hasattr(client, "abortBatch") and batch_id is not None:
                try:
                    client.abortBatch(batch_id)
                except Exception:
                    pass
            result["error"] = f"Update batch timed out after {timeout} seconds"
            return result

        # Read node result log if available
        node_name = result["hostname"] or "unknown"
        res_path = os.path.join(shared_root, ".cluster", f"{node_name}_update_result.txt")
        if os.path.isfile(res_path):
            try:
                with open(res_path, "r", encoding="utf-8") as f:
                    result["log"] = f.read()
            except Exception:
                pass

        result["success"] = True

        # 9. Graceful restart via quitWorker: worker supervisor will detect .update_pending,
        # mirror code with Git-parity, and re-execute fresh process image (including GPU probe).
        if restart:
            try:
                pre_restart_ids = {
                    w.get("worker_id") for w in current_fleet if w.get("worker_id") is not None
                }
                if wid is not None:
                    pre_restart_ids.add(wid)
                client.quitWorker(wid)
                reconnect_deadline = time.time() + max(60.0, timeout)
                reconnected = False
                target_host_ip = _extract_host_ip(str(result.get("hostaddr") or ""))
                while time.time() < reconnect_deadline:
                    time.sleep(poll_interval)
                    try:
                        current_workers = list_online_workers(client)
                    except Exception:
                        continue
                    for cw in current_workers:
                        cw_id = cw.get("worker_id")
                        cw_hname = str(cw.get("hostname", "")).strip().lower()
                        cw_addr = str(cw.get("hostaddr", "")).strip()
                        cw_state = str(cw.get("state", "")).strip().lower()

                        match_hname = bool(result["hostname"] and cw_hname == str(result["hostname"]).strip().lower())
                        cw_ip = _extract_host_ip(cw_addr)
                        match_ip = bool(
                            target_host_ip
                            and target_host_ip not in ("127.0.0.1", "::1")
                            and cw_ip == target_host_ip.lower()
                        )

                        if cw_id not in pre_restart_ids and (match_hname or match_ip) and cw_state == "ready":
                            result["relaunched"] = True
                            result["new_worker_id"] = cw_id
                            result["new_hostaddr"] = cw.get("hostaddr")
                            if orig_target_paused and cw_id is not None:
                                other_workers_paused[cw_id] = True
                            reconnected = True
                            break
                    if reconnected:
                        break
            except Exception as e:
                result["error"] = f"Update succeeded but quitWorker restart failed: {e}"

    finally:
        # 10. Guaranteed teardown: restore other workers' pause states
        for ow_id, was_paused in other_workers_paused.items():
            if hasattr(client, "setWorkerPaused"):
                try:
                    client.setWorkerPaused(ow_id, was_paused)
                except Exception:
                    pass

        # Restore any paused batches
        for bid in paused_existing_batches:
            if hasattr(client, "setBatchPaused"):
                try:
                    client.setBatchPaused(bid, False)
                except Exception:
                    pass

    return result


def update_all_workers_parallel(
    client,
    shared_root: str,
    repo_url: str = DEFAULT_REPO_ARCHIVE_URL,
    timeout: float = 90.0,
    restart: bool = True,
    poll_interval: float = 2.0,
    force: bool = True,
) -> list[dict]:
    """Dispatches update signals and lifecycle restarts across all online workers in parallel."""
    if Metashape is None or client is None:
        return [{"error": "Metashape Python API is not available", "success": False}]

    workers = list_online_workers(client)
    if not workers:
        return []

    # 1. Stage fresh update payload once BEFORE pausing any batches or workers
    stage_cluster_update_payload(shared_root, repo_url, force_refresh=True)
    payload_sha = _read_payload_meta_sha256(shared_root)
    cluster_shared_dir = os.path.join(shared_root, ".cluster")

    paused_existing_batches: list[int] = []
    orig_worker_paused: dict[int | str, bool] = {}
    orig_paused_by_host: dict[str, bool] = {
        str(w.get("hostname", "")).strip().lower(): bool(w.get("paused", False)) for w in workers if w.get("hostname")
    }
    worker_batches: list[dict] = []

    try:
        # 2. If force mode, pause queued batches and abort active tasks on busy workers
        if force:
            if hasattr(client, "batchList"):
                try:
                    bl = client.batchList()
                    if isinstance(bl, dict):
                        for b in bl.get("batches", []):
                            bid = b.get("batch_id")
                            bstate = str(b.get("state", "")).lower()
                            if bstate in ("queued", "pending", "submitted") and not b.get("paused"):
                                if hasattr(client, "setBatchPaused"):
                                    client.setBatchPaused(bid, True)
                                    paused_existing_batches.append(bid)
                            elif bstate in ("inprogress", "running", "active") and not b.get("aborted"):
                                _safe_abort_batch(client, bid, is_paused=bool(b.get("paused")))
                except Exception:
                    pass

            # Await workers to transition to ready (only poll if any worker was busy)
            if any(str(w.get("state", "")).lower() not in ("ready", "") for w in workers):
                t0 = time.time()
                while time.time() - t0 < 10.0:
                    time.sleep(1.0)
                    try:
                        fresh = list_online_workers(client)
                    except Exception:
                        continue
                    if fresh and all(str(w.get("state", "")).lower() in ("ready", "") for w in fresh):
                        workers = fresh
                        break

                # Workers blocked inside C++ RunScript ignore abortBatch; send watchdog .abort FIRST
                # and only fall back to quitWorker after grace window (avoids double-restart race).
                still_busy = [
                    w for w in list_online_workers(client) if str(w.get("state", "")).lower() not in ("ready", "")
                ]
                if still_busy:
                    pre_ids_by_host: dict[str, set[Any]] = {}
                    dispatched_sigs: dict[str, str] = {}
                    for bw in still_busy:
                        bwid = bw.get("worker_id")
                        bhname = str(bw.get("hostname", "")).strip()
                        bkey = bhname.lower()
                        if bkey:
                            pre_ids_by_host.setdefault(bkey, set())
                            if bwid is not None:
                                pre_ids_by_host[bkey].add(bwid)
                            if shared_root:
                                try:
                                    dispatched_sigs[bkey] = dispatch_abort_signal(
                                        node_name=bhname, batch_id=bw.get("batch_id"), shared_root=shared_root
                                    )
                                except Exception:
                                    pass

                    # Grace window for watchdog reap (~10s)
                    if dispatched_sigs:
                        wd_deadline = time.time() + 10.0
                        while time.time() < wd_deadline:
                            time.sleep(min(poll_interval, 1.0))
                            try:
                                fresh = list_online_workers(client)
                            except Exception:
                                continue
                            remaining = []
                            for bkey, pre_ids in pre_ids_by_host.items():
                                has_fresh_ready = any(
                                    str(w.get("hostname", "")).strip().lower() == bkey
                                    and str(w.get("state", "")).lower() in ("ready", "")
                                    and (w.get("worker_id") is None or w.get("worker_id") not in pre_ids)
                                    for w in fresh
                                )
                                if not has_fresh_ready:
                                    remaining.append(bkey)
                            if not remaining:
                                workers = fresh
                                break

                    # Fallback quitWorker ONLY for nodes still busy after watchdog grace window
                    still_stuck = [
                        w for w in list_online_workers(client) if str(w.get("state", "")).lower() not in ("ready", "")
                    ]
                    if still_stuck and hasattr(client, "quitWorker"):
                        for bw in still_stuck:
                            bwid = bw.get("worker_id")
                            bkey = str(bw.get("hostname", "")).strip().lower()
                            sig_p = dispatched_sigs.get(bkey, "")
                            if sig_p and os.path.isfile(sig_p):
                                try:
                                    os.remove(sig_p)
                                except OSError:
                                    pass
                            if bwid is not None:
                                try:
                                    client.quitWorker(bwid)
                                except Exception:
                                    pass
                        reboot_deadline = time.time() + 25.0
                        expected_names = {
                            str(w.get("hostname", "")).strip().lower() for w in workers if w.get("hostname")
                        }
                        while time.time() < reboot_deadline:
                            time.sleep(min(poll_interval, 2.0))
                            try:
                                fresh = list_online_workers(client)
                            except Exception:
                                continue
                            ready_names = {
                                str(w.get("hostname", "")).strip().lower()
                                for w in fresh
                                if str(w.get("state", "")).lower() in ("ready", "")
                            }
                            if fresh and expected_names and expected_names.issubset(ready_names):
                                workers = fresh
                                break
                        else:
                            refreshed = list_online_workers(client)
                            if refreshed:
                                workers = refreshed

        # 3. Record original pause state across refreshed fleet so we isolate each node cleanly during signal dispatch
        for w in workers:
            w_id = w.get("worker_id")
            if w_id is not None:
                hkey = str(w.get("hostname", "")).strip().lower()
                orig_worker_paused[w_id] = orig_paused_by_host.get(hkey, bool(w.get("paused", False)))

        for w in workers:
            wid = w.get("worker_id")
            hname = w.get("hostname", "")
            wstate = str(w.get("state", "")).lower()
            node_res = {
                "success": False,
                "worker_id": wid,
                "hostname": hname,
                "hostaddr": w.get("hostaddr"),
                "batch_id": None,
                "relaunched": False,
                "new_worker_id": None,
                "new_hostaddr": None,
                "log": "",
                "error": None,
            }

            if wstate not in ("ready", "") and not force:
                node_res["error"] = f"Worker {hname} is busy ({wstate}). Skipped in safe mode."
                worker_batches.append(node_res)
                continue

            # Isolate target worker (pause others) to avoid Metashape worker-stealing / retry thrashing
            if hasattr(client, "setWorkerPaused"):
                for other_w in workers:
                    other_id = other_w.get("worker_id")
                    if other_id is not None:
                        try:
                            client.setWorkerPaused(other_id, other_id != wid)
                        except Exception:
                            pass

            rel_project = ensure_cluster_maintenance_project(shared_root, worker_slug=f"{hname}_{wid}")
            _abort_stale_maintenance_batches(client, rel_project)

            try:
                task = Metashape.Tasks.RunScript()
                task.code = generate_worker_updater_code(
                    repo_url=repo_url,
                    expected_hostname=hname,
                    shared_cluster_dir=cluster_shared_dir,
                    expected_sha256=payload_sha,
                )
                net_task = task.toNetworkTask()
                batch_id = client.createBatch(rel_project, [net_task])
                node_res["batch_id"] = batch_id
                if hasattr(client, "setBatchWorkerLimit"):
                    client.setBatchWorkerLimit(batch_id, 1)
                client.setBatchPaused(batch_id, False)
            except Exception as e:
                node_res["error"] = f"Failed to dispatch batch: {e}"
                worker_batches.append(node_res)
                continue

            # Poll this isolated worker's quick signal batch (~1-2s) with infinite-retry abort defense
            start_t = time.time()
            while time.time() - start_t < timeout:
                try:
                    bl = client.batchList() if hasattr(client, "batchList") else {}
                except Exception:
                    time.sleep(poll_interval)
                    continue
                batch_entry = None
                if isinstance(bl, dict):
                    for b in bl.get("batches", []):
                        if b.get("batch_id") == batch_id:
                            batch_entry = b
                            break
                if batch_entry:
                    state = str(batch_entry.get("state", "")).lower()
                    err_cnt = int(batch_entry.get("error_count", 0) or 0)
                    is_failed = bool(batch_entry.get("failed") or batch_entry.get("aborted") or err_cnt > 0)
                    if is_failed or state in ("failed", "aborted", "cancelled"):
                        if state not in ("completed", "finished", "failed", "aborted", "cancelled") and hasattr(
                            client, "abortBatch"
                        ):
                            try:
                                client.abortBatch(batch_id)
                            except Exception:
                                pass
                        last_err = batch_entry.get("last_error")
                        err_detail = (
                            last_err.get("message")
                            if isinstance(last_err, dict) and last_err.get("message")
                            else f"state={state}"
                        )
                        node_res["error"] = f"Batch failed ({err_detail})"
                        break
                    if state in ("completed", "finished"):
                        node_res["success"] = True
                        break
                time.sleep(poll_interval)

            if not node_res["success"] and not node_res["error"]:
                if hasattr(client, "abortBatch"):
                    try:
                        client.abortBatch(batch_id)
                    except Exception:
                        pass
                node_res["error"] = f"Update batch timed out after {timeout}s"

            worker_batches.append(node_res)

        # Restore worker pause states before parallel daemon restarts
        if hasattr(client, "setWorkerPaused"):
            for orig_wid, was_paused in orig_worker_paused.items():
                try:
                    client.setWorkerPaused(orig_wid, was_paused)
                except Exception:
                    pass

        # 5. Read result logs and issue quitWorker in parallel for all completed workers
        pre_restart_ids = {w.get("worker_id") for w in workers if w.get("worker_id") is not None}
        claimed_new_ids: set[Any] = set()
        relaunching_nodes = []
        for nr in worker_batches:
            if nr.get("success"):
                hname = nr.get("hostname") or "unknown"
                rf_path = os.path.join(shared_root, ".cluster", f"{hname}_update_result.txt")
                if os.path.isfile(rf_path):
                    try:
                        with open(rf_path, "r", encoding="utf-8") as f:
                            nr["log"] = f.read()
                    except Exception:
                        pass
                if restart and hasattr(client, "quitWorker") and nr.get("worker_id") is not None:
                    try:
                        client.quitWorker(nr["worker_id"])
                        relaunching_nodes.append(nr)
                    except Exception as e_quit:
                        nr["error"] = f"Update succeeded but quitWorker failed: {e_quit}"

        # 6. Await parallel reconnection (budget allows ZIP mirror + Metashape GPU probe on startup)
        if relaunching_nodes:
            reconnect_deadline = time.time() + max(60.0, timeout)
            while time.time() < reconnect_deadline:
                time.sleep(poll_interval)
                try:
                    current_workers = list_online_workers(client)
                except Exception:
                    continue
                for nr in relaunching_nodes:
                    if nr.get("relaunched"):
                        continue
                    target_hname = str(nr.get("hostname", "")).strip().lower()
                    target_ip = _extract_host_ip(str(nr.get("hostaddr") or ""))

                    for cw in current_workers:
                        cw_id = cw.get("worker_id")
                        cw_hname = str(cw.get("hostname", "")).strip().lower()
                        cw_addr = str(cw.get("hostaddr", "")).strip()
                        cw_state = str(cw.get("state", "")).strip().lower()

                        match_hname = bool(target_hname and cw_hname == target_hname)
                        cw_ip = _extract_host_ip(cw_addr)
                        match_ip = bool(
                            target_ip and target_ip not in ("127.0.0.1", "::1") and cw_ip == target_ip.lower()
                        )

                        if (
                            cw_id not in pre_restart_ids
                            and cw_id not in claimed_new_ids
                            and (match_hname or match_ip)
                            and cw_state == "ready"
                        ):
                            nr["relaunched"] = True
                            nr["new_worker_id"] = cw_id
                            nr["new_hostaddr"] = cw.get("hostaddr")
                            if cw_id is not None:
                                claimed_new_ids.add(cw_id)
                            if orig_paused_by_host.get(target_hname) and cw_id is not None:
                                orig_worker_paused[cw_id] = True
                            break

                if all(nr.get("relaunched") for nr in relaunching_nodes):
                    break

    finally:
        # Always restore worker pause states and paused batches
        if hasattr(client, "setWorkerPaused"):
            for orig_wid, was_paused in orig_worker_paused.items():
                try:
                    client.setWorkerPaused(orig_wid, was_paused)
                except Exception:
                    pass
        for bid in paused_existing_batches:
            if hasattr(client, "setBatchPaused"):
                try:
                    client.setBatchPaused(bid, False)
                except Exception:
                    pass

    return worker_batches
