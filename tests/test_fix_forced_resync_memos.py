"""SMALL1 (review select #7, syncing-1 #7): a user's forced re-sync ("resync" from the UI or the
phone) really recomputes. Before, a forced run in the same process got the in-process timeline
alignment memo and, for 30 minutes, a remembered timing-reference timeout of the video
(``remembered_failure``). Both are forgotten once at the start of a forced run; inside the run
the other candidates still reuse them, and a non-forced run is unchanged."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pudge import syncing
from pudge.subtitles import timeline_alignment as ta

TRACK = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc"},
        {"index": 1, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
    ]
}


def _tool(tmp_path: Path, name: str) -> str:
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _video(tmp_path: Path, name: str) -> Path:
    video = tmp_path / name
    video.write_bytes(b"\0" * 1024)
    return video


def _timeouts(monkeypatch, calls: list[list[str]]) -> None:
    def run(command, *args, **kwargs):
        if "-show_streams" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(TRACK), "")
        calls.append(list(command))
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(syncing.subprocess, "run", run)


def test_a_forced_run_retries_a_remembered_reference_timeout(tmp_path: Path, monkeypatch) -> None:
    ffmpeg, ffprobe = _tool(tmp_path, "ffmpeg"), _tool(tmp_path, "ffprobe")
    video, other = _video(tmp_path, "Show - 05.mkv"), _video(tmp_path, "Show - 06.mkv")
    monkeypatch.setattr(syncing, "_TIMING_REFERENCE_TRANSIENT_FAILURES", {})
    calls: list[list[str]] = []
    _timeouts(monkeypatch, calls)
    cache = tmp_path / "cache"

    def extract(path: Path, force: bool):
        return syncing.extract_embedded_timing_reference(
            path, cache, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe, force=force
        )

    for path in (video, other):
        extract(path, False)
    assert len(calls) == 2
    # Non-forced, and forced calls inside one run: remembered, no second wait.
    assert extract(video, False)[1]["remembered_failure"] is True
    assert extract(video, True)[1]["remembered_failure"] is True
    assert len(calls) == 2

    syncing.begin_forced_run(video)  # a user's resync of this video starts
    _path, result = extract(video, True)
    assert not result.get("remembered_failure") and len(calls) == 3  # was remembered for 30 min
    # The run's own new failure protects its other candidates again.
    assert extract(video, True)[1]["remembered_failure"] is True
    # Another video's memory is kept.
    assert extract(other, False)[1]["remembered_failure"] is True
    assert len(calls) == 3


def test_a_forced_run_recomputes_the_alignment_memo(tmp_path: Path, monkeypatch) -> None:
    source, reference = tmp_path / "ja.srt", tmp_path / "en.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nあ\n", encoding="utf-8")
    reference.write_text("1\n00:00:03,000 --> 00:00:04,000\nx\n", encoding="utf-8")
    output = tmp_path / "out.srt"
    output.write_text("x", encoding="utf-8")
    calls: list[Path] = []

    def fake(src, ref, cache_dir, *, max_offset_seconds=120.0, force=False):
        calls.append(src)
        return output, {"accepted": True, "run": len(calls)}

    monkeypatch.setattr(ta, "_align_subtitle_timelines", fake)
    monkeypatch.setattr(ta, "_ALIGNMENT_MEMO", {})
    ta.align_subtitle_timelines(source, reference, tmp_path)
    ta.align_subtitle_timelines(source, reference, tmp_path)
    assert len(calls) == 1
    syncing.begin_forced_run(tmp_path / "video.mkv")
    _out, result = ta.align_subtitle_timelines(source, reference, tmp_path, force=True)
    assert len(calls) == 2 and result["run"] == 2  # was the memo of the earlier run
    # The rest of the forced run (a second selection pass) reuses the fresh result.
    assert ta.align_subtitle_timelines(source, reference, tmp_path, force=True)[1]["run"] == 2
    assert len(calls) == 2


def test_only_a_resync_starts_a_forced_run(tmp_path: Path, monkeypatch) -> None:
    from argparse import Namespace

    from pudge import cli
    from pudge.config import AppConfig

    started: list[Path] = []

    class Stop(Exception):
        pass

    def stop(*_a, **_k):
        raise Stop

    monkeypatch.setattr(syncing, "begin_forced_run", started.append)
    monkeypatch.setattr(cli, "invalidate_final_pipeline_result", stop)
    monkeypatch.setattr(cli, "parse_anime_filename", stop)
    monkeypatch.setattr(
        cli, "SubtitleJobReporter", Namespace(from_environment=lambda: Namespace(update=lambda *a, **k: None))
    )
    config = AppConfig()
    config.paths.cache_dir = tmp_path / "cache"
    video = _video(tmp_path, "Show - 05.mkv")
    for resync in (False, True):
        try:
            cli.process_video(video, Namespace(prepare_only=True, resync=resync), config, None)
        except Stop:
            pass
    assert started == [video.resolve()]
