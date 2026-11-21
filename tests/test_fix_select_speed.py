"""Candidate preparation speed: work that depends only on the video is shared, hot loops stay exact.

* The segment check (``evaluate_segment_alignment``) runs for every candidate (and again after
  a piecewise repair) on the same audio windows: their voice activity is computed once per
  episode and handed to ffsubsync as a speech reference, not decoded and VAD-ed per candidate.
* The timeline window scan (``_match_onsets``, ``_activity_f1``, ``_nearest_distance``) is
  faster but returns exactly what the former code returned (compared here with copies of it).
"""

from __future__ import annotations

import bisect
import math
import random
import statistics
from dataclasses import replace
from pathlib import Path

import pytest

from pudge import syncing
from pudge.config import SyncConfig
from pudge.subtitles import timeline_alignment as ta

# ------------------------------------------------------------------ segment speech ---


def _write_srt(path: Path, cues: list[tuple[float, float, str]]) -> Path:
    def stamp(value: float) -> str:
        millis = int(round(value * 1000))
        hours, millis = divmod(millis, 3_600_000)
        minutes, millis = divmod(millis, 60_000)
        seconds, millis = divmod(millis, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"

    blocks = [f"{index}\n{stamp(a)} --> {stamp(b)}\n{text}\n" for index, (a, b, text) in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


@pytest.fixture
def segment_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A 600 s video, fake window audio (one file per window), counted VAD and ffsubsync."""
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    cache = tmp_path / "cache"
    cache.mkdir()
    calls = {"vad": [], "sync": []}

    def extract(video_path, start, duration, cache_dir, **_kwargs):
        audio = Path(cache_dir) / "segment-audio" / f"window-{start:.3f}-{duration:.3f}.flac"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"flac")
        return audio, None

    def prepare(source, cache_dir, config, *, ffmpeg_path=None, force=False, verbose=False):
        calls["vad"].append(Path(source).name)
        output = Path(cache_dir) / "speech" / f"digest-{Path(source).stem}.npz"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"speech")
        return output, {"reason": "applied"}

    def sync(audio, subtitle, cache_dir, config, *, reference=None, **_kwargs):
        calls["sync"].append((Path(audio).name, reference))
        return subtitle, {"sync_was_successful": True, "offset_seconds": 0.0, "alignment_score": 10.0}

    monkeypatch.setattr(syncing, "_extract_audio_segment", extract)
    monkeypatch.setattr(syncing, "prepare_speech_reference", prepare)
    monkeypatch.setattr(syncing, "synchronize_subtitle", sync)
    monkeypatch.setattr(syncing, "_probe_duration", lambda *_a, **_k: 600.0)
    monkeypatch.setattr(syncing, "_SEGMENT_SPEECH_FROM_THIS_PROCESS", set(), raising=False)
    subtitles = []
    for number in range(3):
        cues = [(5.0 + 4.0 * k + 0.1 * number, 6.5 + 4.0 * k, f"line {k}") for k in range(145)]
        subtitles.append(_write_srt(tmp_path / f"candidate-{number}.srt", cues))
    config = replace(
        SyncConfig(), segment_count=5, segment_window_seconds=60.0, segment_max_offset_seconds=10.0
    )
    return video, cache, config, subtitles, calls


@pytest.mark.parametrize("force", [False, True])
def test_segment_windows_are_voice_analysed_once_for_all_candidates(segment_world, force: bool) -> None:
    video, cache, config, subtitles, calls = segment_world
    for subtitle in subtitles:
        result = syncing.evaluate_segment_alignment(
            video, subtitle, cache, config, ffmpeg_path="ffmpeg", ffprobe_path="ffprobe", force=force
        )
        assert result["attempted_segments"] == 5
    windows = {name for name, _reference in calls["sync"]}
    assert len(windows) == 5
    # One VAD per window for the whole episode (a forced re-preparation too), not per candidate.
    assert sorted(calls["vad"]) == sorted(windows)
    # ffsubsync gets the shared speech of its own window every time.
    by_window: dict[str, set[object]] = {}
    for name, reference in calls["sync"]:
        assert reference is not None
        assert Path(reference).is_file()
        by_window.setdefault(name, set()).add(reference)
    assert all(len(refs) == 1 for refs in by_window.values())
    assert len({next(iter(refs)) for refs in by_window.values()}) == 5


def test_a_forced_preparation_in_a_new_process_recomputes_the_segment_speech(segment_world) -> None:
    video, cache, config, subtitles, calls = segment_world
    syncing.evaluate_segment_alignment(video, subtitles[0], cache, config, ffmpeg_path="ffmpeg", force=False)
    assert len(calls["vad"]) == 5
    syncing.evaluate_segment_alignment(video, subtitles[1], cache, config, ffmpeg_path="ffmpeg", force=False)
    assert len(calls["vad"]) == 5  # reused from the cache
    syncing._SEGMENT_SPEECH_FROM_THIS_PROCESS.clear()  # a new process
    syncing.evaluate_segment_alignment(video, subtitles[2], cache, config, ffmpeg_path="ffmpeg", force=True)
    assert len(calls["vad"]) == 10


def test_without_segment_speech_ffsubsync_reads_the_window_audio(segment_world, monkeypatch) -> None:
    video, cache, config, subtitles, calls = segment_world
    monkeypatch.setattr(
        syncing, "prepare_speech_reference", lambda *a, **k: (None, {"reason": "speech_cache_error"})
    )
    syncing.evaluate_segment_alignment(video, subtitles[0], cache, config, ffmpeg_path="ffmpeg")
    assert calls["sync"] and all(reference is None for _name, reference in calls["sync"])


# ------------------------------------------------------------------ exact hot loops ---


def _old_match_onsets(source, reference, *, offset, tolerance=ta._ONSET_TOLERANCE):
    if not source or not reference:
        return 0, tolerance, 0.0
    mapped = [value + offset for value in source]
    i = j = 0
    errors: list[float] = []
    pairs: list[tuple[float, float]] = []
    while i < len(mapped) and j < len(reference):
        delta = reference[j] - mapped[i]
        if abs(delta) <= tolerance:
            errors.append(abs(delta))
            pairs.append((source[i], reference[j]))
            i += 1
            j += 1
        elif delta < -tolerance:
            j += 1
        else:
            i += 1
    matched = len(pairs)
    mean_error = statistics.fmean(errors) if errors else tolerance
    gap_errors = []
    for (source_a, ref_a), (source_b, ref_b) in zip(pairs, pairs[1:]):
        source_gap = source_b - source_a
        ref_gap = ref_b - ref_a
        if source_gap <= 0.05 or ref_gap <= 0.05:
            continue
        gap_errors.append(abs(source_gap - ref_gap))
    if len(gap_errors) >= 2:
        gap_fingerprint = math.exp(-float(statistics.median(gap_errors)) / 1.35)
    elif len(gap_errors) == 1:
        gap_fingerprint = 0.55 * math.exp(-gap_errors[0] / 1.35)
    else:
        gap_fingerprint = 0.0
    return matched, mean_error, gap_fingerprint


def _onsets(rng: random.Random, count: int, span: float) -> list[float]:
    values = sorted(round(rng.uniform(0.0, span), rng.choice([1, 2, 3])) for _ in range(count))
    if rng.random() < 0.3 and values:  # duplicates and zero gaps
        values += values[: rng.randint(1, 4)]
        values.sort()
    return values


def test_onset_matching_returns_exactly_what_the_former_code_returned() -> None:
    rng = random.Random(1729)
    for _case in range(3000):
        source = _onsets(rng, rng.randint(0, 40), 80.0)
        reference = _onsets(rng, rng.randint(0, 40), 80.0)
        offset = rng.choice([0.0, -0.25, 1.0, 2.75, -13.0, rng.uniform(-6, 6)])
        assert ta._match_onsets(source, reference, offset=offset) == _old_match_onsets(
            source, reference, offset=offset
        )
    # identical copies: every onset matched, zero errors
    same = [1.0 + 2.0 * k for k in range(30)]
    assert ta._match_onsets(same, same, offset=0.0) == _old_match_onsets(same, same, offset=0.0)


def test_activity_overlap_returns_exactly_what_the_former_code_returned() -> None:
    rng = random.Random(4104)
    for _case in range(400):
        reference = {rng.randint(0, 3000) for _ in range(rng.randint(0, 1500))}
        if rng.random() < 0.1:
            reference |= {-rng.randint(1, 5)}  # negative bins: brute-force path
        reference_sorted = sorted(reference)
        source_all = {rng.randint(0, 3000) for _ in range(rng.randint(1, 1500))}
        start = rng.uniform(0.0, 1400.0)
        end = start + rng.choice([2.0, 8.0, 72.0, 300.0])
        first = int(math.floor(start / ta._GRID_SECONDS))
        last = int(math.ceil(end / ta._GRID_SECONDS))
        window = list(ta._bins_in_range(source_all, first, last))
        for offset in (-120.0, -7.5, 0.0, 0.25, 3.0, 44.0, 119.0):
            shift = int(round(offset / ta._GRID_SECONDS))
            overlap = sum(1 for index in window if index + shift in reference)
            if window:
                assert ta._shifted_overlap(window, reference, shift) == overlap
            expected_count = sum(1 for index in reference if first + shift <= index < last + shift)
            expected = (
                0.0 if not window or not expected_count else 2.0 * overlap / (len(window) + expected_count)
            )
            fast = ta._activity_f1(
                source_all,
                reference,
                window_start=start,
                window_end=end,
                offset=offset,
                source_window=window,
                reference_sorted=reference_sorted,
            )
            plain = ta._activity_f1(source_all, reference, window_start=start, window_end=end, offset=offset)
            assert fast == plain == expected


def test_nearest_distance_returns_exactly_what_the_former_code_returned() -> None:
    def old(values, target):
        if not values:
            return 999.0
        index = bisect.bisect_left(values, target)
        options = []
        if index < len(values):
            options.append(abs(values[index] - target))
        if index > 0:
            options.append(abs(values[index - 1] - target))
        return min(options) if options else 999.0

    rng = random.Random(87539319)
    for _case in range(3000):
        values = _onsets(rng, rng.randint(0, 12), 50.0)
        target = rng.choice([rng.uniform(-5.0, 55.0), *(values[:1] or [0.0])])
        assert ta._nearest_distance(values, target) == old(values, target)
