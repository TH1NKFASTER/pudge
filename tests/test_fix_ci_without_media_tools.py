"""CI runs on machines without FFmpeg (behaviour).

The v0.8.0 CI failed there: the macOS smoke step still selected a test that was removed with the
browser companion (pytest found nothing: exit 5), iPhone tests made their audio and video with
the real ffmpeg, and a transcription cancelled before it started reported "FFmpeg missing"."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from source_policy import source
from t71g01_support import load_workflow, run_step, workflow_step

from pudge.audiobooks import AudiobookService
from pudge.database import Database

ROOT = Path(__file__).resolve().parents[1]


def _python_only_path(tmp_path: Path) -> Path:
    """A PATH folder with this Python as `python` and nothing else (no ffmpeg).

    A wrapper, not a symlink: a virtual environment's Python started through a symlink in
    another folder does not find its environment (and so not pytest)."""
    folder = tmp_path / "bin"
    folder.mkdir()
    wrapper = folder / "python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return folder


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "PUDGE_HOME": str(tmp_path / "home"),
        "PUDGE_RUNTIME_LOG_PATH": str(tmp_path / "runtime.log"),
        "TMPDIR": str(tmp_path),
    }


def test_the_macos_smoke_step_runs_its_srt_fallback_tests(tmp_path: Path) -> None:
    # policy: the workflow file is configuration for GitHub; its step is run here.
    step = workflow_step(
        load_workflow(source(".github/workflows/ci.yml")),
        "macos-smoke",
        "Verify SRT fallback without Homebrew media tools",
    )
    done = run_step(step, ROOT, tools=_python_only_path(tmp_path), env=_isolated_env(tmp_path))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "2 passed" in done.stdout


def test_ffmpeg_tests_are_skipped_where_ffmpeg_is_missing(tmp_path: Path) -> None:
    env = {**os.environ, **_isolated_env(tmp_path), "PATH": str(_python_only_path(tmp_path))}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_iphone_read_along.py"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "3 skipped" in done.stdout


def test_a_transcription_cancelled_before_it_starts_is_cancelled_without_ffmpeg(tmp_path: Path) -> None:
    service = AudiobookService(
        Database(tmp_path / "audio.sqlite3"),
        ffprobe="/definitely/missing/ffprobe",
        ffmpeg="/definitely/missing/ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )
    source_file = tmp_path / "short.m4b"
    source_file.write_bytes(b"audio")
    book = service._upsert(
        path=source_file,
        title="Short",
        duration=120.0,
        files=[
            {
                "index": 0,
                "path": str(source_file),
                "title": "Short",
                "duration": 120.0,
                "start": 0.0,
                "end": 120.0,
            }
        ],
        chapters=[{"index": 0, "title": "Short", "start": 0.0, "end": 120.0}],
    )
    book_id = int(book["id"])
    cancel = threading.Event()
    cancel.set()
    service._transcription_cancel_events[book_id] = cancel
    done = threading.Event()
    service._transcribe_worker(book_id, service._transcript_path(book_id), done)
    assert done.is_set()
    assert service.transcription_status(book_id)["status"] == "cancelled"
