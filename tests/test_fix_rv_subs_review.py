"""Subtitle-area review fixes: atomic caches, no stray temporaries, edge-input crashes,
fewer ffprobe runs, a lighter ``pudge.syncing`` import and a faster (identical) activity score.

All data is invented; no network and no real media.
"""

from __future__ import annotations

import json
import os
import random
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

from pudge import audio_timeline, syncing
from pudge.config import SyncConfig
from pudge.subtitle_formats import write_srt
from pudge.subtitles.jobs import SubtitleJobReporter
from pudge.subtitles.models import SubtitleJobStage

REPO = Path(__file__).resolve().parents[1]


def _cues(seed: int, count: int, shift: float = 0.0) -> list[tuple[float, float, str]]:
    rng = random.Random(seed)
    cues: list[tuple[float, float, str]] = []
    moment = 3.0
    for index in range(count):
        moment += rng.uniform(0.5, 9.0)
        cues.append((moment + shift, moment + shift + rng.uniform(0.6, 3.5), f"line {index}"))
    return cues


# --- the activity correlation: the same numbers as the original loop ---------------------------


def _old_activity_correlation(source_cues, reference_cues, shift_seconds, *, bin_seconds=0.50):
    """The implementation before the review (verbatim), the reference for identical output."""
    shifted = [
        (start + shift_seconds, end + shift_seconds, text)
        for start, end, text in source_cues
        if end + shift_seconds > 0.0
    ]
    if not shifted or not reference_cues:
        return 0.0
    duration = max(
        max(end for _start, end, _text in shifted),
        max(end for _start, end, _text in reference_cues),
    )
    if duration <= 0.0:
        return 0.0
    step = max(0.20, min(1.0, float(bin_seconds)))
    count = max(1, int(duration / step) + 1)

    def mask(cues):
        diff = [0] * (count + 1)
        for start, end, _text in cues:
            left = max(0, min(count - 1, int(start / step)))
            right = max(left + 1, min(count, int(end / step) + 1))
            diff[left] += 1
            diff[right] -= 1
        active = 0
        result = []
        for index in range(count):
            active += diff[index]
            result.append(active > 0)
        return result

    source_mask = mask(shifted)
    reference_mask = mask(reference_cues)
    n11 = n10 = n01 = n00 = 0
    for source_active, reference_active in zip(source_mask, reference_mask):
        if source_active and reference_active:
            n11 += 1
        elif source_active:
            n10 += 1
        elif reference_active:
            n01 += 1
        else:
            n00 += 1
    denominator = ((n11 + n10) * (n11 + n01) * (n00 + n10) * (n00 + n01)) ** 0.5
    if denominator <= 0.0:
        return 0.0
    return (n11 * n00 - n10 * n01) / denominator


def test_activity_correlation_is_identical_to_the_original_loop() -> None:
    cases = [
        (_cues(1, 400), _cues(1, 400, 1.3)),
        (_cues(2, 50), _cues(3, 60)),
        (_cues(4, 5), _cues(4, 5, -2.0)),
        ([(0.0, 0.01, "a")], [(0.0, 0.01, "b")]),  # one bin
        ([(5.0, 5.0, "a")], [(0.0, 900.0, "b")]),  # the reference active everywhere
    ]
    for source, reference in cases:
        for shift in (-40.0, -3.25, -0.5, 0.0, 0.05, 1.3, 7.0, 60.0):
            for bin_seconds in (0.2, 0.5, 1.0, 3.0):
                assert syncing._activity_correlation_for_shift(
                    source, reference, shift, bin_seconds=bin_seconds
                ) == _old_activity_correlation(source, reference, shift, bin_seconds=bin_seconds)


# --- gaps from packet timestamps -----------------------------------------------------------------


def test_gaps_from_packets_survives_a_shared_timestamp_with_and_without_duration() -> None:
    rows = [(0.0, 0.021), (0.021, 0.021), (0.021, None), (0.042, 0.021), (1.0, 0.021), (1.021, 0.021)]
    # sorted() over these tuples raised TypeError (None against a float at the same time).
    assert audio_timeline.gaps_from_packets(rows) == ((0.063, 0.937),)


def test_gaps_from_packets_unchanged_on_ordinary_rows() -> None:
    rng = random.Random(5)
    rows = [(round(index * 0.0213, 6), 0.0213) for index in range(400)]
    rows += [(round(9.0 + index * 0.0213, 6), 0.0213) for index in range(100)]
    rng.shuffle(rows)
    assert audio_timeline.gaps_from_packets(rows) == ((8.52, 0.48),)
    # Without durations the usual one comes from neighbours in packet (file) order.
    without_durations = [(pts, None) for pts, _duration in sorted(rows)]
    assert audio_timeline.gaps_from_packets(without_durations) == ((8.52, 0.48),)


def test_inner_gaps_by_index_reuses_the_scan_by_position(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"\0" * 64)
    audio_timeline.remember(
        video,
        {
            "format": {"start_time": "0.000000"},
            "streams": [
                {"index": 0, "codec_type": "video", "start_time": "0.000000"},
                {"index": 1, "codec_type": "audio", "start_time": "0.000000", "channels": 2},
                {"index": 2, "codec_type": "audio", "start_time": "0.000000", "channels": 2},
            ],
        },
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="0.0,0.02\n0.02,0.02\n", stderr="")

    monkeypatch.setattr(audio_timeline.subprocess, "run", fake_run)
    assert audio_timeline.inner_gaps(video, "ffprobe", position=1) == ()
    assert audio_timeline.inner_gaps(video, "ffprobe", index=2) == ()  # the same track: cached
    assert len(commands) == 1 and commands[0][commands[0].index("-select_streams") + 1] == "a:1"
    # An index the probe does not know is still scanned by index.
    assert audio_timeline.inner_gaps(video, "ffprobe", index=7) == ()
    assert commands[-1][commands[-1].index("-select_streams") + 1] == "7"


# --- the shared speech file -----------------------------------------------------------------------


class _FakePipe:
    def __init__(self, values):
        self.values = values

    def fit(self, _video):
        return self

    def transform(self, _video):
        return self.values


def _fake_ffsubsync(monkeypatch, values) -> None:
    np = pytest.importorskip("numpy")
    ffsubsync_impl = pytest.importorskip("ffsubsync.ffsubsync")
    monkeypatch.setattr(ffsubsync_impl, "make_reference_pipe", lambda _args: _FakePipe(np.asarray(values)))
    monkeypatch.setattr(syncing, "_ffsubsync_audio_stream", lambda *_a, **_k: None)
    monkeypatch.setattr(syncing, "_ffsubsync_audio_delay", lambda *_a, **_k: 0.0)


def test_speech_reference_is_published_whole_and_leaves_no_temporary(tmp_path: Path, monkeypatch) -> None:
    np = pytest.importorskip("numpy")
    _fake_ffsubsync(monkeypatch, [0.0, 1.0, 1.0, 0.0])
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    output, result = syncing.prepare_speech_reference(video, tmp_path / "cache", SyncConfig())
    assert result["reason"] == "applied" and output is not None
    with np.load(output) as payload:
        assert payload["speech"].tolist() == [0.0, 1.0, 1.0, 0.0]
    assert sorted(path.name for path in output.parent.iterdir()) == [output.name]


def test_an_interrupted_speech_write_is_never_a_cache_hit(tmp_path: Path, monkeypatch) -> None:
    np = pytest.importorskip("numpy")
    _fake_ffsubsync(monkeypatch, [1.0] * 1000)

    def cut_off(target, **_arrays):
        # Half an archive, then the process is stopped (a quit or a killed worker).
        if hasattr(target, "write"):
            target.write(b"PK\x03\x04 half an archive")
        else:
            Path(target).write_bytes(b"PK\x03\x04 half an archive")
        raise KeyboardInterrupt

    monkeypatch.setattr(np, "savez_compressed", cut_off)
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    with pytest.raises(KeyboardInterrupt):
        syncing.prepare_speech_reference(video, tmp_path / "cache", SyncConfig())
    assert list((tmp_path / "cache" / "speech").iterdir()) == []

    def disk_full(target, **_arrays):
        target.write(b"PK\x03\x04 half an archive")
        raise OSError("No space left on device")

    monkeypatch.setattr(np, "savez_compressed", disk_full)
    output, result = syncing.prepare_speech_reference(video, tmp_path / "cache", SyncConfig())
    assert output is None and result["reason"] == "speech_cache_error"
    assert list((tmp_path / "cache" / "speech").iterdir()) == []


def test_a_damaged_speech_file_means_no_speech_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("numpy")
    damaged = tmp_path / "speech.npz"
    damaged.write_bytes(b"PK\x03\x04 truncated")
    monkeypatch.setattr(syncing, "_has_embedded_text_subtitle", lambda *_a, **_k: False)
    monkeypatch.setattr(syncing, "prepare_speech_reference", lambda *_a, **_k: (damaged, {}))
    assert (
        syncing._audio_speech_activity(tmp_path / "v.mkv", tmp_path, SyncConfig(), ffmpeg_path="ffmpeg")
        is None
    )


def test_speech_activity_values_are_python_floats(tmp_path: Path, monkeypatch) -> None:
    np = pytest.importorskip("numpy")
    speech = tmp_path / "speech.npz"
    np.savez_compressed(speech, speech=np.array([0, 1, 1, 0], dtype=np.int8))
    monkeypatch.setattr(syncing, "_has_embedded_text_subtitle", lambda *_a, **_k: False)
    monkeypatch.setattr(syncing, "prepare_speech_reference", lambda *_a, **_k: (speech, {}))
    values = syncing._audio_speech_activity(tmp_path / "v.mkv", tmp_path, SyncConfig(), ffmpeg_path="ffmpeg")
    assert values == [0.0, 1.0, 1.0, 0.0] and all(type(value) is float for value in values)


def test_audio_speech_activity_still_wraps_zipfile_errors() -> None:
    # The guard above relies on numpy raising BadZipFile, which is no OSError/ValueError.
    assert not issubclass(zipfile.BadZipFile, (OSError, ValueError, KeyError))


# --- ALASS publishing ----------------------------------------------------------------------------


def _fake_alass(tmp_path: Path) -> Path:
    script = tmp_path / "alass"
    script.write_text(f"#!{sys.executable}\nimport shutil, sys\nshutil.copyfile(sys.argv[2], sys.argv[3])\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_a_failed_alass_publish_leaves_no_hidden_copy(tmp_path: Path, monkeypatch) -> None:
    reference, source = tmp_path / "reference.srt", tmp_path / "source.srt"
    write_srt(_cues(7, 20), reference)
    write_srt(_cues(8, 20), source)
    cache = tmp_path / "cache"

    def full_disk(*_args, **_kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(syncing, "_write_metadata", full_disk)
    # An ALASS failure like any other (rv4): the candidate comparison goes on.
    failed, failure = syncing.synchronize_with_alass(
        reference, source, cache, SyncConfig(), alass_path=str(_fake_alass(tmp_path))
    )
    assert failed == source and failure["reason"] == "alass_error"
    assert failure["sync_was_successful"] is False
    assert [path.name for path in (cache / "alass").iterdir()] == []

    monkeypatch.undo()
    output, result = syncing.synchronize_with_alass(
        reference, source, cache, SyncConfig(), alass_path=str(_fake_alass(tmp_path))
    )
    assert result["reason"] == "applied"
    assert sorted(path.name for path in (cache / "alass").iterdir()) == sorted(
        [output.name, output.name + ".json"]
    )


# --- container duration --------------------------------------------------------------------------


def test_the_duration_is_probed_once_per_file(tmp_path: Path) -> None:
    counter = tmp_path / "count"
    ffprobe = tmp_path / "ffprobe"
    ffprobe.write_text(
        f"#!{sys.executable}\nimport pathlib\np = pathlib.Path({str(counter)!r})\n"
        "p.write_text(str(int(p.read_text() or 0) + 1) if p.exists() else '1')\nprint('1440.5')\n"
    )
    ffprobe.chmod(ffprobe.stat().st_mode | stat.S_IEXEC)
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    assert syncing._probe_duration(video, str(ffprobe)) == 1440.5
    assert syncing._probe_duration(video, str(ffprobe)) == 1440.5
    assert counter.read_text() == "1"
    video.write_bytes(b"another video")  # a replaced file is probed again
    os.utime(video, ns=(1, 1))
    assert syncing._probe_duration(video, str(ffprobe)) == 1440.5
    assert counter.read_text() == "2"


def test_a_failed_duration_probe_is_tried_again(tmp_path: Path) -> None:
    ffprobe = tmp_path / "ffprobe"
    ffprobe.write_text(f"#!{sys.executable}\nprint('N/A')\n")
    ffprobe.chmod(ffprobe.stat().st_mode | stat.S_IEXEC)
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    assert syncing._probe_duration(video, str(ffprobe)) is None
    ffprobe.write_text(f"#!{sys.executable}\nprint('12.0')\n")
    assert syncing._probe_duration(video, str(ffprobe)) == 12.0


# --- worker status and downloads -------------------------------------------------------------------


def test_job_status_with_a_path_detail_is_written(tmp_path: Path) -> None:
    status, trace = tmp_path / "status.json", tmp_path / "trace.jsonl"
    SubtitleJobReporter(status, trace).update(SubtitleJobStage.ALIGNING, video=Path("/v/Show - 01.mkv"))
    assert json.loads(status.read_text())["details"]["video"] == "/v/Show - 01.mkv"
    assert json.loads(trace.read_text().splitlines()[0])["details"]["video"] == "/v/Show - 01.mkv"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["status.json", "trace.jsonl"]


def test_a_failed_job_status_write_leaves_no_temporary(tmp_path: Path, monkeypatch) -> None:
    status = tmp_path / "status.json"

    def full_disk(self, target):
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "replace", full_disk)
    SubtitleJobReporter(status).update(SubtitleJobStage.ALIGNING, video="x")
    assert list(tmp_path.iterdir()) == []


def test_a_failed_jimaku_download_write_leaves_no_partial_file(tmp_path: Path, monkeypatch) -> None:
    from pudge.providers import jimaku
    from pudge.providers.jimaku import JimakuClient, JimakuError
    from pudge.models import JimakuFile

    client = JimakuClient("https://jimaku.cc", "key")
    client.client = httpx.Client(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"1\n"))
    )
    item = JimakuFile(
        url="https://jimaku.cc/entry/1/download/Show - 05.srt",
        name="Show - 05.srt",
        size=2,
        last_modified="2024-01-01T00:00:00Z",
    )

    def full_disk(_source, _target):
        raise OSError("No space left on device")

    monkeypatch.setattr(jimaku.os, "replace", full_disk)
    try:
        with pytest.raises(JimakuError):
            client.download(item, tmp_path)
    finally:
        client.close()
    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []


# --- import cost ---------------------------------------------------------------------------------


def test_importing_syncing_does_not_load_the_llm_client() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import sys, pudge.syncing; print('pudge.llm' in sys.modules)"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert completed.stdout.strip() == "False"


# --- the short-clock search's onset test: the same answers as the generator it replaced ----------


def test_onset_hits_answers_like_the_original_generator() -> None:
    from pudge.subtitles.timeline_alignment import _onset_hits
    import bisect

    def old(reference_onsets, target, tolerance):
        position = bisect.bisect_left(reference_onsets, target)
        return any(
            0 <= k < len(reference_onsets) and abs(reference_onsets[k] - target) <= tolerance
            for k in (position - 1, position)
        )

    rng = random.Random(11)
    references = [[], [5.0], [0.0, 0.0, 1.0], sorted(rng.uniform(0, 1500) for _ in range(400))]
    for reference in references:
        targets = [-1.0, 0.0, 0.35, 5.0, 5.35, 5.3500001, 1e9, float("nan"), float("inf")]
        targets += [value + rng.uniform(-0.5, 0.5) for value in reference[:50]]
        targets += [rng.uniform(-10, 1600) for _ in range(300)]
        for target in targets:
            for tolerance in (0.0, 0.35, 1.0):
                assert _onset_hits(reference, target, tolerance) == old(reference, target, tolerance)
