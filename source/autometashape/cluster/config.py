# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Configuration persistence and serialization for cluster server and worker daemons."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from typing import Any

DEFAULT_WORKER_CONFIG_FILENAME = "worker_config.json"
DEFAULT_SERVER_CONFIG_FILENAME = "server_config.json"


def _is_dir_writable(dir_path: str) -> bool:
    """Return True if a directory supports actual file creation (NTFS ACL & read-only mount safe)."""
    if not dir_path or not os.path.isdir(dir_path):
        return False
    fd = None
    probe_path = ""
    try:
        fd, probe_path = tempfile.mkstemp(prefix=".am_cfg_probe_", dir=dir_path)
        os.close(fd)
        fd = None
        os.remove(probe_path)
        return True
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if probe_path and os.path.exists(probe_path):
            try:
                os.remove(probe_path)
            except OSError:
                pass
        return False


def get_default_config_path(
    filename: str = DEFAULT_WORKER_CONFIG_FILENAME,
    for_write: bool = False,
) -> str:
    """Return absolute path to cluster config file, avoiding read-only or site-packages dirs."""
    env_dir = os.environ.get("AUTOMETASHAPE_CONFIG_DIR", "").strip()
    if env_dir:
        abs_env_dir = os.path.abspath(env_dir)
        if for_write or not os.path.isdir(abs_env_dir):
            os.makedirs(abs_env_dir, exist_ok=True)
        return os.path.join(abs_env_dir, filename)

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    is_repo_checkout = os.path.isfile(os.path.join(repo_root, "pyproject.toml")) and os.path.isdir(
        os.path.join(repo_root, "scripts")
    )
    if is_repo_checkout:
        local_conf = os.path.join(repo_root, filename)
        if os.path.exists(local_conf) and (not for_write or _is_dir_writable(repo_root)):
            return local_conf
        if not os.path.exists(local_conf) and _is_dir_writable(repo_root):
            return local_conf

    user_dir = os.path.join(os.path.expanduser("~"), ".autometashape")
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, filename)


def load_cluster_config(
    config_path: str | None = None, filename: str = DEFAULT_WORKER_CONFIG_FILENAME
) -> dict[str, Any]:
    """Load JSON cluster configuration from disk."""
    path = config_path or get_default_config_path(filename, for_write=False)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            print(
                f"[WARNING] Cluster config '{path}' must be a JSON object, got {type(data).__name__}; ignoring.",
                file=sys.stderr,
            )
            return {}
        except Exception as exc:
            print(f"[WARNING] Failed to load cluster config '{path}': {exc}", file=sys.stderr)
            return {}
    return {}


def save_cluster_config(
    config: dict[str, Any], config_path: str | None = None, filename: str = DEFAULT_WORKER_CONFIG_FILENAME
) -> str:
    """Save cluster configuration dictionary to JSON file on disk atomically."""
    path = config_path or get_default_config_path(filename, for_write=True)
    parent_dir = os.path.dirname(os.path.abspath(path))
    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)

    tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        for attempt in range(3):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if attempt >= 2:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    return path
