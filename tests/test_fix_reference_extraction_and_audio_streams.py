"""Embedded reference extraction and the audio stream used for alignment.

SYNC-8: extracting the English reference track had a fixed 90 s timeout, but
ffmpeg reads the whole container: a BD remux on a USB disk or NAS timed out on
every attempt and nothing remembered it. The timeout now grows with the file
size. A timeout or ffmpeg error is remembered in the process (one preparation's
candidates do not each wait); a track without cues is stored for a day (not for
a forced run).

``_select_timing_reference_stream`` gave its "CR" bonus to any title containing
"cr" ("Descriptive", "Screen"); only the whole word (or Crunchyroll) counts now.

SYNC-11: the ffsubsync VAD used ffmpeg's default audio stream (most channels)
and the segment check ``-map 0:a:0``, so on English 5.1 + Japanese 2.0 both
listened to the dub. Both now use the Japanese track.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import types
from dataclasses import replace
from pathlib import Path

import pytest

from pudge import syncing
from pudge.config import SyncConfig

GB = 1024**3


def _tool(tmp_path: Path, name: str) -> str:
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _video(tmp_path: Path, size: int) -> Path:
    video = tmp_path / "[BD] Show - 05.mkv"
    with video.open("wb") as handle:
        handle.truncate(size)  # sparse
    return video


ENGLISH_TRACK = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc"},
        {"index": 1, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
    ]
}


def _fake_run(calls: list[dict], *, ffmpeg_outcome):
    def run(command, *args, **kwargs):
        calls.append({"command": list(command), "timeout": kwargs.get("timeout")})
        if "-show_streams" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(ENGLISH_TRACK), "")
        return ffmpeg_outcome(command, kwargs)

    return run


def test_reference_timeout_scales_with_size_and_failure_is_remembered(tmp_path: Path, monkeypatch) -> None:
    ffmpeg, ffprobe = _tool(tmp_path, "ffmpeg"), _tool(tmp_path, "ffprobe")
    video = _video(tmp_path, 8 * GB)
    calls: list[dict] = []
    monkeypatch.setattr(syncing, "_TIMING_REFERENCE_TRANSIENT_FAILURES", {})

    def timeout(command, kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(syncing.subprocess, "run", _fake_run(calls, ffmpeg_outcome=timeout))
    cache = tmp_path / "cache"
    path, result = syncing.extract_embedded_timing_reference(
        video, cache, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe
    )
    assert path is None and result["reason"] == "timing_reference_extract_failed"
    extraction = [call for call in calls if "-map" in call["command"]]
    assert len(extraction) == 1
    # 8 GB at a slow 20 MB/s is ~410 s, not 90 s.
    assert extraction[0]["timeout"] == pytest.approx(8 * GB / (20 * 1024 * 1024))

    # The other candidates of this preparation (also a forced one) do not wait again.
    calls.clear()
    for force in (False, True):
        path, result = syncing.extract_embedded_timing_reference(
            video, cache, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe, force=force
        )
        assert path is None and result["remembered_failure"] is True
    assert not [call for call in calls if "-map" in call["command"]]
    # A timeout may be passing (slow disk): nothing on disk, the next preparation tries again.
    assert not list((cache / "timing-reference").glob("*.failed.json"))
    monkeypatch.setattr(syncing, "_TIMING_REFERENCE_TRANSIENT_FAILURES", {})
    syncing.extract_embedded_timing_reference(video, cache, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)
    assert [call for call in calls if "-map" in call["command"]]


def test_ffmpeg_error_is_not_stored_but_an_empty_track_is(tmp_path: Path, monkeypatch) -> None:
    assert syncing._timing_reference_extract_timeout(300 * 1024 * 1024) == 90.0
    assert syncing._timing_reference_extract_timeout(60 * GB) == 600.0
    ffmpeg, ffprobe = _tool(tmp_path, "ffmpeg"), _tool(tmp_path, "ffprobe")
    video = _video(tmp_path, 1024)
    calls: list[dict] = []
    monkeypatch.setattr(syncing, "_TIMING_REFERENCE_TRANSIENT_FAILURES", {})
    cache = tmp_path / "cache"

    def failing(command, kwargs):
        return subprocess.CompletedProcess(command, 1, "Input/output error", "")

    monkeypatch.setattr(syncing.subprocess, "run", _fake_run(calls, ffmpeg_outcome=failing))
    syncing.extract_embedded_timing_reference(video, cache, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)
    assert not list((cache / "timing-reference").glob("*.failed.json"))
    _path, result = syncing.extract_embedded_timing_reference(
        video, cache, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe
    )
    assert result["remembered_failure"] is True  # in this process only

    # ffmpeg read the whole track without one cue: stored for a day.
    monkeypatch.setattr(syncing, "_TIMING_REFERENCE_TRANSIENT_FAILURES", {})

    def empty(command, kwargs):
        Path(command[-1]).write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(syncing.subprocess, "run", _fake_run(calls, ffmpeg_outcome=empty))
    syncing.extract_embedded_timing_reference(video, cache, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe)
    assert list((cache / "timing-reference").glob("*.failed.json"))
    monkeypatch.setattr(syncing, "_TIMING_REFERENCE_TRANSIENT_FAILURES", {})
    calls.clear()
    _path, result = syncing.extract_embedded_timing_reference(
        video, cache, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe
    )
    assert result["remembered_failure"] is True
    assert not [call for call in calls if "-map" in call["command"]]
    marker = next((cache / "timing-reference").glob("*.failed.json"))
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["failed_at"] = time.time() - 25 * 3600
    marker.write_text(json.dumps(payload), encoding="utf-8")
    _path, result = syncing.extract_embedded_timing_reference(
        video, cache, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe
    )
    assert "remembered_failure" not in result  # a day later: tried again
    payload["failed_at"] = time.time()
    marker.write_text(json.dumps(payload), encoding="utf-8")

    # Fresh ignores the stored marker; a success clears it.
    def writes(command, kwargs):
        Path(command[-1]).write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(syncing.subprocess, "run", _fake_run(calls, ffmpeg_outcome=writes))
    path, result = syncing.extract_embedded_timing_reference(
        video, cache, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe, force=True
    )
    assert path is not None and result["reason"] == "applied"
    assert not list((cache / "timing-reference").glob("*.failed.json"))


def test_cr_title_bonus_is_a_whole_word() -> None:
    def stream(index: int, title: str, default: int = 0) -> dict:
        return {
            "index": index,
            "codec_type": "subtitle",
            "codec_name": "ass",
            "tags": {"language": "eng", "title": title},
            "disposition": {"default": default},
        }

    picked = syncing._select_timing_reference_stream([stream(2, "English"), stream(3, "Descriptive English")])
    assert picked["index"] == 2
    picked = syncing._select_timing_reference_stream([stream(2, "English"), stream(3, "Screen text")])
    assert picked["index"] == 2
    for title in ("English [CR]", "English (Crunchyroll)", "Full English", "Dialogue"):
        picked = syncing._select_timing_reference_stream([stream(2, "English"), stream(3, title)])
        assert picked["index"] == 3, title


DUAL_AUDIO = {
    "streams": [
        {"index": 1, "codec_name": "truehd", "tags": {"language": "eng", "title": "English 5.1"}},
        {"index": 2, "codec_name": "flac", "tags": {"language": "jpn", "title": "Japanese 2.0"}},
    ]
}


def _probe_run(payload: dict, calls: list[list[str]]):
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if command and str(command[0]).endswith("ffprobe"):
            calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return real_run(command, *args, **kwargs)

    return run


def test_japanese_audio_stream_spec_for_dual_audio(tmp_path: Path, monkeypatch) -> None:
    ffmpeg = _tool(tmp_path, "ffmpeg")
    _tool(tmp_path, "ffprobe")
    video = _video(tmp_path, 1024)
    probes: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _probe_run(DUAL_AUDIO, probes))
    syncing._JAPANESE_AUDIO_SPEC_CACHE.clear()
    assert syncing._japanese_audio_stream_spec(video, ffmpeg) == "0:a:1"
    assert syncing._japanese_audio_stream_spec(video, ffmpeg) == "0:a:1"
    assert len(probes) == 1  # once per file
    single = {"streams": [{"index": 1, "codec_name": "aac", "tags": {"language": "jpn"}}]}
    other = tmp_path / "Show - 06.mkv"
    other.write_bytes(b"v")
    monkeypatch.setattr(subprocess, "run", _probe_run(single, probes))
    assert syncing._japanese_audio_stream_spec(other, ffmpeg) is None  # the default is the same track
    untagged = {"streams": [{"index": 1, "codec_name": "aac"}, {"index": 2, "codec_name": "aac"}]}
    third = tmp_path / "Show - 07.mkv"
    third.write_bytes(b"v")
    monkeypatch.setattr(subprocess, "run", _probe_run(untagged, probes))
    assert syncing._japanese_audio_stream_spec(third, ffmpeg) is None  # ambiguous: keep the default
    assert syncing._japanese_audio_stream_spec(tmp_path / "segment.flac", ffmpeg) is None


def test_segment_audio_maps_the_japanese_track(tmp_path: Path, monkeypatch) -> None:
    ffmpeg = _tool(tmp_path, "ffmpeg")
    video = _video(tmp_path, 1024)
    syncing._JAPANESE_AUDIO_SPEC_CACHE.clear()
    monkeypatch.setattr(syncing, "_japanese_audio_stream_spec", lambda *_a, **_k: "0:a:1")
    commands: list[list[str]] = []

    def run(command, *args, **kwargs):
        commands.append(list(command))
        Path(command[-1]).write_bytes(b"fLaC")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(syncing.subprocess, "run", run)
    output, error = syncing._extract_audio_segment(
        video, 10.0, 30.0, tmp_path / "cache", ffmpeg_path=ffmpeg, force=False
    )
    assert error is None and output is not None
    command = commands[-1]
    assert command[command.index("-map") + 1] == "0:a:1?"


class _FakeFfsubsync:
    """Stands in for the ffsubsync package: records argument lists, writes the output."""

    def __init__(self) -> None:
        self.runs: list[list[str]] = []
        self.references: list[list[str]] = []

    def install(self, monkeypatch) -> None:
        fake = self
        module = types.ModuleType("ffsubsync.ffsubsync")

        class Parser:
            def parse_args(self, arguments):
                return list(arguments)

        def run(arguments):
            fake.runs.append(list(arguments))
            Path(arguments[arguments.index("-o") + 1]).write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8"
            )
            return {"sync_was_successful": True, "offset_seconds": 0.5, "framerate_scale_factor": 1.0}

        class Pipe:
            def fit(self, _path):
                return self

            def transform(self, _path):
                import numpy as np

                return np.zeros(4)

        def make_reference_pipe(arguments):
            fake.references.append(list(arguments))
            return Pipe()

        module.make_parser = Parser
        module.run = run
        module.make_reference_pipe = make_reference_pipe
        package = types.ModuleType("ffsubsync")
        package.ffsubsync = module
        monkeypatch.setitem(sys.modules, "ffsubsync", package)
        monkeypatch.setitem(sys.modules, "ffsubsync.ffsubsync", module)


def test_ffsubsync_uses_the_japanese_track(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("numpy")
    fake = _FakeFfsubsync()
    fake.install(monkeypatch)
    video = _video(tmp_path, 1024)
    subtitle = tmp_path / "Show - 05.ja.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    monkeypatch.setattr(syncing, "_japanese_audio_stream_spec", lambda *_a, **_k: "0:a:1")
    monkeypatch.setattr(syncing, "_has_embedded_text_subtitle", lambda *_a, **_k: False)
    config = SyncConfig()

    reference, result = syncing.prepare_speech_reference(video, tmp_path / "cache", config)
    assert reference is not None, result
    arguments = fake.references[-1]
    assert arguments[arguments.index("--reference-stream") + 1] == "0:a:1"

    _path, result = syncing.synchronize_subtitle(video, subtitle, tmp_path / "cache", config)
    assert result["sync_was_successful"] is True
    arguments = fake.runs[-1]
    assert arguments[arguments.index("--reference-stream") + 1] == "0:a:1"
    # Against the shared speech file the stream is already chosen.
    syncing.synchronize_subtitle(
        video, subtitle, tmp_path / "cache", config, reference=reference, tag="other"
    )
    assert "--reference-stream" not in fake.runs[-1]


def test_audio_stream_is_not_named_when_it_would_turn_off_the_subtitle_reference(
    tmp_path: Path, monkeypatch
) -> None:
    # ffsubsync tries an embedded text subtitle only while no stream (or a
    # subtitle stream) is named; with "subs_then_*" and such a track, keep it.
    pytest.importorskip("numpy")
    fake = _FakeFfsubsync()
    fake.install(monkeypatch)
    video = _video(tmp_path, 1024)
    monkeypatch.setattr(syncing, "_japanese_audio_stream_spec", lambda *_a, **_k: "0:a:1")
    monkeypatch.setattr(syncing, "_has_embedded_text_subtitle", lambda *_a, **_k: True)
    config = SyncConfig()
    assert "subs" in config.vad
    reference, _result = syncing.prepare_speech_reference(video, tmp_path / "cache", config)
    assert reference is not None and "--reference-stream" not in fake.references[-1]
    # A detector without subtitles: the Japanese track is named.
    syncing.prepare_speech_reference(video, tmp_path / "cache", replace(config, vad="webrtc"))
    arguments = fake.references[-1]
    assert arguments[arguments.index("--reference-stream") + 1] == "0:a:1"
    # The segment check still maps the Japanese track (it reads audio only).
    assert syncing._ffsubsync_audio_stream(video, None, "subs_then_webrtc") is None
    monkeypatch.setattr(syncing, "_has_embedded_text_subtitle", lambda *_a, **_k: False)
    assert syncing._ffsubsync_audio_stream(video, None, "subs_then_webrtc") == "0:a:1"


def test_text_subtitle_probe(tmp_path: Path, monkeypatch) -> None:
    ffmpeg = _tool(tmp_path, "ffmpeg")
    _tool(tmp_path, "ffprobe")
    monkeypatch.setattr(syncing, "_TEXT_SUBTITLE_STREAM_CACHE", {})
    answers = {
        "a.mkv": {"streams": [{"codec_name": "hdmv_pgs_subtitle"}]},
        "b.mkv": {"streams": [{"codec_name": "hdmv_pgs_subtitle"}, {"codec_name": "ass"}]},
    }

    def run(command, *args, **kwargs):
        return subprocess.CompletedProcess(command, 0, json.dumps(answers[Path(command[-1]).name]), "")

    monkeypatch.setattr(syncing.subprocess, "run", run)
    for name, expected in (("a.mkv", False), ("b.mkv", True)):
        video = tmp_path / name
        video.write_bytes(b"v")
        assert syncing._has_embedded_text_subtitle(video, ffmpeg) is expected
