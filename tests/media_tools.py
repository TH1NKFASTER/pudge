"""Markers for tests that need the real media tools (FFmpeg)."""

import shutil
from pathlib import Path

import pytest

# The GitHub runners have no FFmpeg; on a Mac (and in Homebrew-based setups) these tests run.
requires_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg and ffprobe are required"
)


def paired_ffmpeg() -> tuple[str, str]:
    """An ``(ffmpeg, ffprobe)`` pair installed together, as absolute paths when found.

    Given an absolute ffmpeg path, the app only uses the ffprobe next to it
    (``syncing._ffprobe_beside``: another folder's ffprobe may be another build). Homebrew
    installs both in one folder; a host whose first ``ffmpeg`` on PATH is a lone launcher
    (``/usr/local/bin/ffmpeg`` wrapping ``/usr/bin/ffmpeg``, no ffprobe beside it) made every
    probe-based step of those tests silently fall back. Prefer the ffmpeg beside the ffprobe.
    """
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if ffprobe and ffmpeg and not Path(ffmpeg).with_name("ffprobe").is_file():
        beside = Path(ffprobe).with_name("ffmpeg")
        if beside.is_file():
            ffmpeg = str(beside)
    return ffmpeg or "ffmpeg", ffprobe or "ffprobe"
