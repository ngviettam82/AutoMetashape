# Copyright (c) 2026, AutoMetashape Contributors.
# All rights reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for hardened checkpoint chunk resolution and resume logic."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SOURCE_DIR = os.path.join(_REPO_ROOT, "source")
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
if _SOURCE_DIR not in sys.path:
    sys.path.insert(0, _SOURCE_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from autometashape.core.pipeline import resolve_checkpoint_chunk


class MockCamera:
    """Mock Metashape camera object for testing alignment evaluation."""

    def __init__(self, transform: bool = True, enabled: bool = True) -> None:
        self.transform = MagicMock() if transform else None
        self.enabled = enabled


class MockChunk:
    """Mock Metashape chunk object."""

    def __init__(self, label: str = "Chunk 1", cameras: list[MockCamera] | None = None) -> None:
        self.label = label
        self.cameras = cameras if cameras is not None else []


class MockDocument:
    """Mock Metashape Document with chunks and active chunk."""

    def __init__(self, chunks: list[MockChunk] | None = None) -> None:
        self.chunks = chunks if chunks is not None else []
        self.chunk = self.chunks[0] if self.chunks else None

    def addChunk(self) -> MockChunk:
        new_c = MockChunk("Production_Reconstruction")
        self.chunks.append(new_c)
        self.chunk = new_c
        return new_c


class TestCheckpointLoader(unittest.TestCase):
    """Test suite defending hardened checkpoint loading against AttributeError and state bugs."""

    def test_empty_chunks_returns_no_resume(self) -> None:
        """Verify that a document with empty chunks list returns safely without AttributeError."""
        doc = MockDocument(chunks=[])
        chunk, aligned, can_resume = resolve_checkpoint_chunk(doc)
        self.assertIsNone(chunk)
        self.assertEqual(aligned, 0)
        self.assertFalse(can_resume)

    def test_single_default_chunk_with_aligned_cameras_resumes(self) -> None:
        """Verify that 1 chunk with default label 'Chunk 1' and aligned cameras successfully resumes."""
        cameras = [MockCamera(transform=True), MockCamera(transform=True), MockCamera(transform=False)]
        chunk1 = MockChunk(label="Chunk 1", cameras=cameras)
        doc = MockDocument(chunks=[chunk1])

        chunk, aligned, can_resume = resolve_checkpoint_chunk(doc)
        self.assertIsNotNone(chunk)
        self.assertEqual(chunk.label, "Chunk 1")
        self.assertEqual(aligned, 2)
        self.assertTrue(can_resume)
        self.assertEqual(doc.chunk, chunk1)

    def test_single_default_chunk_with_insufficient_aligned_cameras(self) -> None:
        """Verify that 1 chunk with default label and <2 aligned cameras returns can_resume=False."""
        cameras = [MockCamera(transform=True), MockCamera(transform=False)]
        chunk1 = MockChunk(label="Chunk 1", cameras=cameras)
        doc = MockDocument(chunks=[chunk1])

        chunk, aligned, can_resume = resolve_checkpoint_chunk(doc)
        self.assertIsNotNone(chunk)
        self.assertEqual(aligned, 1)
        self.assertFalse(can_resume)

    def test_canonical_production_reconstruction_label_precedence(self) -> None:
        """Verify canonical Production_Reconstruction chunk is preferred even if another chunk has cameras."""
        chunk_scratch = MockChunk(label="Scratch_Chunk", cameras=[MockCamera(True), MockCamera(True), MockCamera(True)])
        chunk_prod = MockChunk(label="Production_Reconstruction", cameras=[MockCamera(True), MockCamera(True)])
        doc = MockDocument(chunks=[chunk_scratch, chunk_prod])

        chunk, aligned, can_resume = resolve_checkpoint_chunk(doc)
        self.assertEqual(chunk, chunk_prod)
        self.assertEqual(aligned, 2)
        self.assertTrue(can_resume)
        self.assertEqual(doc.chunk, chunk_prod)

    def test_highest_aligned_cameras_fallback_when_canonical_absent(self) -> None:
        """Verify that when Production_Reconstruction is absent, chunk with most aligned cameras is selected."""
        chunk_low = MockChunk(label="Chunk 1", cameras=[MockCamera(True)])
        chunk_high = MockChunk(label="Chunk 2", cameras=[MockCamera(True), MockCamera(True), MockCamera(True)])
        doc = MockDocument(chunks=[chunk_low, chunk_high])

        chunk, aligned, can_resume = resolve_checkpoint_chunk(doc)
        self.assertEqual(chunk, chunk_high)
        self.assertEqual(aligned, 3)
        self.assertTrue(can_resume)
        self.assertEqual(doc.chunk, chunk_high)

    def test_chunk_missing_cameras_attribute_safe(self) -> None:
        """Verify chunks lacking a cameras attribute or with None cameras do not raise AttributeError."""
        dummy_chunk = MagicMock()
        del dummy_chunk.cameras
        doc = MockDocument(chunks=[dummy_chunk])

        chunk, aligned, can_resume = resolve_checkpoint_chunk(doc)
        self.assertIsNone(chunk)
        self.assertEqual(aligned, 0)
        self.assertFalse(can_resume)

    def test_dynamic_chunk_wrapper_identity_defense(self) -> None:
        """Verify resolve_checkpoint_chunk handles C++ binding transient wrappers where id() changes."""

        class DynamicChunkWrapper(MockChunk):
            def __eq__(self, other: object) -> bool:
                return isinstance(other, MockChunk) and other.label == self.label

        chunk = DynamicChunkWrapper("Production_Reconstruction", [MockCamera(True), MockCamera(True)])

        class DynamicDocument:
            def __init__(self, c: MockChunk) -> None:
                self.chunks = [c]
                self._c = c

            @property
            def chunk(self) -> MockChunk:
                # Returns a brand new wrapper object on every attribute access
                return DynamicChunkWrapper(self._c.label, self._c.cameras)

            @chunk.setter
            def chunk(self, val: MockChunk) -> None:
                self._c = val

        doc = DynamicDocument(chunk)
        resolved_chunk, aligned, can_resume = resolve_checkpoint_chunk(doc)
        self.assertIsNotNone(resolved_chunk)
        self.assertEqual(aligned, 2)
        self.assertTrue(can_resume)


if __name__ == "__main__":
    unittest.main()
