# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Hybrid temporal-spatial pairing graph generation for drone photogrammetry."""

import math
import os
from typing import Any, Dict, List, Set, Tuple
import numpy as np


def build_hybrid_matching_graph(
    metadata: List[Dict[str, Any]],
    max_temporal_dist: int = 8,
    max_spatial_neighbors: int = 25,
    max_spatial_dist_m: float = 30.0,
    output_pairs_path: str = None,
) -> Set[Tuple[str, str]]:
    """Builds a robust hybrid pairing graph guaranteeing connectivity even across GPS shadows."""
    valid_gps = [m for m in metadata if m["has_gps"]]
    if valid_gps:
        lat_ref = np.mean([m["lat"] for m in valid_gps])
        lon_ref = np.mean([m["lon"] for m in valid_gps])
        m_per_deg_lat = 111320.0
        m_per_deg_lon = 111320.0 * np.cos(np.radians(lat_ref))
    else:
        m_per_deg_lat = 111320.0
        m_per_deg_lon = 111320.0

    for m in metadata:
        if m["has_gps"]:
            m["x"] = (m["lon"] - lon_ref) * m_per_deg_lon
            m["y"] = (m["lat"] - lat_ref) * m_per_deg_lat
            m["z"] = m["alt"]
        else:
            m["x"], m["y"], m["z"] = None, None, None

    pairs = set()
    n = len(metadata)

    # 1. Sequential Temporal Connections
    for i in range(n):
        for step in range(1, max_temporal_dist + 1):
            if i + step < n:
                p1, p2 = sorted([metadata[i]["filename"], metadata[i + step]["filename"]])
                pairs.add((p1, p2))

    # 2. Top-K Spatial Proximity Connections
    for i in range(n):
        if not metadata[i]["has_gps"]:
            continue
        xi, yi, zi = metadata[i]["x"], metadata[i]["y"], metadata[i]["z"]
        dists = []
        for j in range(n):
            if i == j or not metadata[j]["has_gps"]:
                continue
            xj, yj, zj = metadata[j]["x"], metadata[j]["y"], metadata[j]["z"]
            dist = math.sqrt((xi - xj) ** 2 + (yi - yj) ** 2 + (zi - zj) ** 2)
            if dist <= max_spatial_dist_m:
                dists.append((dist, metadata[j]["filename"]))

        dists.sort(key=lambda x: x[0])
        for _, neighbor_fn in dists[:max_spatial_neighbors]:
            p1, p2 = sorted([metadata[i]["filename"], neighbor_fn])
            pairs.add((p1, p2))

    if output_pairs_path:
        os.makedirs(os.path.dirname(output_pairs_path), exist_ok=True)
        with open(output_pairs_path, "w") as f:
            for p1, p2 in sorted(pairs):
                f.write(f"{p1} {p2}\n")

    return pairs
