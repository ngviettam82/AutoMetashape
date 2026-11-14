# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Configuration persistence and serialization for cluster server and worker daemons."""

from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_WORKER_CONFIG_FILENAME = "worker_config.json"
DEFAULT_SERVER_CONFIG_FILENAME = "server_config.json"


def get_default_config_path(filename: str = DEFAULT_WORKER_CONFIG_FILENAME) -> str:
    """Returns absolute path to configuration file in repository root or user profile."""
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    local_conf = os.path.join(repo_root, filename)
    if os.path.exists(local_conf) or os.access(repo_root, os.W_OK):
        return local_conf

    user_dir = os.path.join(os.path.expanduser("~"), ".autometashape")
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, filename)


def load_cluster_config(config_path: str | None = None, filename: str = DEFAULT_WORKER_CONFIG_FILENAME) -> dict[str, Any]:
    """Load JSON cluster configuration from disk."""
    path = config_path or get_default_config_path(filename)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cluster_config(
    config: dict[str, Any], config_path: str | None = None, filename: str = DEFAULT_WORKER_CONFIG_FILENAME
) -> str:
    """Save cluster configuration dictionary to JSON file on disk atomically."""
    path = config_path or get_default_config_path(filename)
    parent_dir = os.path.dirname(os.path.abspath(path))
    if parent_dir and not os.path.exists(parent_dir):
        os.makedirs(parent_dir, exist_ok=True)

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)
    return path
