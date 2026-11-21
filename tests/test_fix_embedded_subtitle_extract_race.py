"""Two requests for one episode's embedded subtitles at once (the phone's download and its
subtitle fetch) extract once and both get the file; no shared .part file."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from pudge import iphone_media


def test_concurrent_extractions_share_one_run(tmp_path, monkeypatch) -> None:
    runs: list[str] = []

    def fake_run(command, **_kwargs):
        out = Path(command[-1])
        runs.append(out.name)
        time.sleep(0.05)
        out.write_text("1\n00:00:01,000 --> 00:00:02,000\nテスト\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(iphone_media.subprocess, "run", fake_run)
    target = tmp_path / "video" / "embedded-1-s2-10-20.srt"
    video = tmp_path / "e.mkv"
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with iphone_media._embedded_subtitle_lock(target):
                if not target.is_file():
                    iphone_media._extract_embedded_subtitle("ffmpeg", video, 2, target)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(runs) == 1 and runs[0].endswith(".part.srt") and runs[0] != "embedded-1-s2-10-20.part.srt"
    assert target.read_text(encoding="utf-8").endswith("テスト\n")
    assert not [name for name in os.listdir(target.parent) if name.endswith(".part.srt")]


def test_a_failed_extraction_leaves_no_part_file(tmp_path, monkeypatch) -> None:
    def failing(command, **_kwargs):
        Path(command[-1]).write_text("half", encoding="utf-8")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(iphone_media.subprocess, "run", failing)
    target = tmp_path / "video" / "embedded-2-s3-1-2.srt"
    try:
        iphone_media._extract_embedded_subtitle("ffmpeg", tmp_path / "v.mkv", 3, target)
    except subprocess.CalledProcessError:
        pass
    assert not target.exists()
    assert not list(target.parent.glob("*.part.srt"))
