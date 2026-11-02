# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for hybrid matching graph generation."""

from gaussian_splat.matching import build_hybrid_matching_graph


def test_build_hybrid_matching_graph_sequential():
    """Test sequential temporal connectivity without GPS."""
    metadata = [
        {"id": i, "filename": f"img_{i:04d}.jpg", "has_gps": False}
        for i in range(10)
    ]
    pairs = build_hybrid_matching_graph(metadata, max_temporal_dist=2)
    assert len(pairs) > 0
    # Every consecutive pair (i, i+1) must exist
    for i in range(9):
        p1, p2 = sorted([f"img_{i:04d}.jpg", f"img_{i+1:04d}.jpg"])
        assert (p1, p2) in pairs


def test_build_hybrid_matching_graph_spatial():
    """Test spatial proximity connectivity with GPS."""
    metadata = [
        {
            "id": 0,
            "filename": "img_0000.jpg",
            "has_gps": True,
            "lat": 10.75,
            "lon": 106.68,
            "alt": 20.0
        },
        {
            "id": 1,
            "filename": "img_0001.jpg",
            "has_gps": True,
            "lat": 10.75001,
            "lon": 106.68001,
            "alt": 20.0
        }
    ]
    pairs = build_hybrid_matching_graph(metadata, max_spatial_dist_m=50.0)
    assert ("img_0000.jpg", "img_0001.jpg") in pairs
