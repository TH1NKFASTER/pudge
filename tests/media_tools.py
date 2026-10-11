"""Markers for tests that need the real media tools (FFmpeg)."""

import shutil

import pytest

# The GitHub runners have no FFmpeg; on a Mac (and in Homebrew-based setups) these tests run.
requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg and ffprobe are required"
)
