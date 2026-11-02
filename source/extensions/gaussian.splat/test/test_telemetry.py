# Copyright (c) 2026, GaussianSplat Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for drone telemetry extraction."""

import os
from gaussian_splat.telemetry import extract_drone_metadata


def test_extract_drone_metadata_empty(tmp_path):
    """Test telemetry extraction on empty directory."""
    meta = extract_drone_metadata(str(tmp_path))
    assert meta == []


def test_extract_drone_metadata_with_dummy_file(tmp_path):
    """Test telemetry extraction with dummy non-exif file."""
    dummy_file = tmp_path / "dummy_0001.JPG"
    dummy_file.write_text("not real image")
    meta = extract_drone_metadata(str(tmp_path))
    assert len(meta) == 1
    assert meta[0]["filename"] == "dummy_0001.JPG"
    assert meta[0]["has_gps"] is False
