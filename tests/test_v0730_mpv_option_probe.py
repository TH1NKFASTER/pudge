import os
import shutil
import subprocess

import pytest

from pudge import player
from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.player import mpv_supports_option as real_probe  # captured before any fixture patch


def _fake_mpv(tmp_path, counter):
    script = tmp_path / "mpv"
    script.write_text(
        f"#!/bin/sh\necho x >> '{counter}'\necho ' --macos-app-activation-policy  Choices: regular accessory'\necho ' --pause  Flag'\n"
    )
    script.chmod(0o755)
    return str(script)


def test_probe_runs_once_per_executable_identity(tmp_path):
    player._MPV_OPTION_CACHE.clear()
    counter = tmp_path / "calls"
    mpv = _fake_mpv(tmp_path, counter)
    assert real_probe(mpv, "macos-app-activation-policy") is True
    assert real_probe(mpv, "pause") is True
    assert real_probe(mpv, "missing-option") is False
    assert counter.read_text().count("x") == 1
    stat = os.stat(mpv)
    os.utime(mpv, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))  # replaced binary → re-probe
    assert real_probe(mpv, "pause") is True
    assert counter.read_text().count("x") == 2


def test_probe_failure_never_breaks_playback(tmp_path, monkeypatch):
    player._MPV_OPTION_CACHE.clear()
    mpv = _fake_mpv(tmp_path, tmp_path / "calls")

    class NotAContextManager:
        def __init__(self, *_a, **_k):
            pass

    monkeypatch.setattr(subprocess, "Popen", NotAContextManager)
    assert real_probe(mpv, "pause") is False
    assert real_probe(str(tmp_path / "missing-mpv"), "pause") is False


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg is required")
def test_cross_file_window_keeps_audio_from_every_file(tmp_path):
    # A window across two files of a book (in a folder with an apostrophe) holds the end of
    # the first file and the start of the second. With the concat demuxer newer FFmpeg kept
    # only the first part, and list-file quoting broke on apostrophes.
    folder = tmp_path / "Kino's Journey"
    folder.mkdir()
    files = []
    for index, tone in enumerate((440, 880)):
        path = folder / f"part {index + 1}.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency={tone}:duration=3",
                "-ac",
                "1",
                "-ar",
                "16000",
                str(path),
            ],
            check=True,
            timeout=30,
        )
        files.append(
            {
                "index": index,
                "path": str(path),
                "title": path.stem,
                "duration": 3.0,
                "start": 3.0 * index,
                "end": 3.0 * index + 3.0,
            }
        )
    service = AudiobookService(
        Database(tmp_path / "library.sqlite3"), ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache"
    )
    book = service._upsert(path=folder, title="Kino's Journey", duration=6.0, files=files, chapters=[])
    destination = folder / "window.flac"

    start, duration = service._extract_global_audio_window(
        int(book["id"]), destination, start=2.0, duration=2.0
    )

    assert (start, duration) == (2.0, 2.0)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(destination)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert abs(float(probe.stdout) - 2.0) < 0.1
    assert [path.name for path in folder.iterdir() if path.is_dir()] == []  # the parts were cleaned up
