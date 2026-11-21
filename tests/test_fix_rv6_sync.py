"""rv6 review of the subtitle synchronisation core (``pudge.syncing``).

- The embedded-reference piecewise repair reports the same edit boundaries when its
  output is already cached (a refused repair stays on disk), so the discontinuity gate
  decides a re-run like the first run.
- A forced run extracts the embedded timing track once, not once per candidate/pass.
- The piecewise step does not check the source subtitle when the baseline check is
  already unreliable (the result was never read).
"""

from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

from pudge import syncing
from pudge.config import SyncConfig
from pudge.subtitle_formats import write_exact_srt

# Invented lines: an English-like reference and a Japanese-like source, same moments.
OPENING = (250.0, 320.0)


def _tracks(tmp_path: Path) -> tuple[Path, Path]:
    rng = random.Random(7)
    reference: list[tuple[float, float, str]] = []
    moment = 5.0
    index = 0
    while moment < 1400.0:
        if OPENING[0] <= moment < OPENING[1]:
            moment = OPENING[1]
        duration = rng.uniform(1.0, 2.8)
        reference.append((moment, moment + duration, f"Line number {index} here"))
        index += 1
        moment += duration + rng.uniform(0.6, 4.5)
    # The cold open before the opening is 6 s early; everything after it is on time.
    source = [
        (start - 6.0, end - 6.0, f"台詞{k}です") if start < OPENING[0] else (start, end, f"台詞{k}です")
        for k, (start, end, _text) in enumerate(reference)
    ]
    reference_path, source_path = tmp_path / "reference.srt", tmp_path / "aligned.srt"
    write_exact_srt(reference, reference_path)
    write_exact_srt(source, source_path)
    return source_path, reference_path


def _extreme_alass_result(repair: dict[str, object]) -> dict[str, object]:
    return {
        "alass_shift_spread_seconds": 45.0,
        "alass_blocks": 4,
        "alass_distinct_shifts": 4,
        "reference_piecewise_repair": repair,
    }


def test_cached_reference_piecewise_keeps_its_edit_boundaries(tmp_path: Path) -> None:
    aligned, reference = _tracks(tmp_path)
    cache = tmp_path / "cache"
    first = syncing._repair_with_embedded_reference_piecewise_core(
        aligned, reference, cache, SyncConfig(), force=False
    )[1]
    assert first["edit_boundaries"], first.get("reason")
    assert list((cache / "reference-piecewise").glob("*.srt"))
    # The same files again (a non-forced preparation, the next candidate pass): cached output.
    again = syncing._repair_with_embedded_reference_piecewise_core(
        aligned, reference, cache, SyncConfig(), force=False
    )[1]
    assert again["edit_boundaries"] == first["edit_boundaries"]


def test_a_refused_cached_repair_keeps_the_discontinuity_gate_decision(tmp_path: Path, monkeypatch) -> None:
    aligned, reference = _tracks(tmp_path)
    cache = tmp_path / "cache"
    flat = {
        "available": True,
        "start": 0.5,
        "middle": 0.5,
        "end": 0.5,
        "full": 0.5,
        "weighted": 0.5,
    }
    # No activity gain: the repair is refused, its output stays in the cache.
    monkeypatch.setattr(syncing, "compare_timing_activity", lambda *a, **k: dict(flat))
    monkeypatch.setattr(syncing, "compare_dialogue_timing_activity", lambda *a, **k: dict(flat))
    decisions = []
    for _run in range(2):
        _path, repair = syncing._repair_with_embedded_reference_piecewise_core(
            aligned, reference, cache, SyncConfig(), force=False
        )
        assert repair["reason"] == "reference_piecewise_no_improvement"
        decisions.append(
            syncing._gate_embedded_reference_alass_discontinuity(
                _extreme_alass_result(repair), flat, reference_ok=True, reference_reason="ok"
            )
        )
    assert list((cache / "reference-piecewise").glob("*.srt"))
    assert decisions[0][0] is True
    assert decisions[0][2]["reason"] == "extreme_map_confirmed_by_piecewise_boundary"
    assert decisions[1][:2] == decisions[0][:2]
    assert decisions[1][2]["reason"] == decisions[0][2]["reason"]


TRACK = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "hevc"},
        {"index": 1, "codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}},
    ]
}
SRT = "".join(f"{n}\n00:00:{n:02d},000 --> 00:00:{n:02d},800\nLine {n}\n\n" for n in range(1, 30))


def _tool(tmp_path: Path, name: str) -> str:
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _fake_ffmpeg(monkeypatch, extractions: list[list[str]], text: str) -> None:
    def run(command, *args, **kwargs):
        if "-show_streams" in command:
            return subprocess.CompletedProcess(command, 0, json.dumps(TRACK), "")
        extractions.append(list(command))
        Path(command[-1]).write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(syncing.subprocess, "run", run)


def test_a_forced_run_extracts_the_timing_track_once(tmp_path: Path, monkeypatch) -> None:
    ffmpeg, ffprobe = _tool(tmp_path, "ffmpeg"), _tool(tmp_path, "ffprobe")
    video = tmp_path / "Show - 05.mkv"
    video.write_bytes(b"\0" * 1024)
    monkeypatch.setattr(syncing, "_TIMING_REFERENCES_FROM_THIS_PROCESS", {})
    monkeypatch.setattr(syncing, "_TIMING_REFERENCE_TRANSIENT_FAILURES", {})
    extractions: list[list[str]] = []
    _fake_ffmpeg(monkeypatch, extractions, SRT)

    def extract(force: bool):
        return syncing.extract_embedded_timing_reference(
            video, tmp_path / "cache", ffmpeg_path=ffmpeg, ffprobe_path=ffprobe, force=force
        )

    syncing.begin_forced_run(video)
    first, first_result = extract(True)
    assert first is not None and first_result["reason"] == "applied"
    # The next candidates and passes of the same forced run: the fresh copy.
    for force in (True, True, False):
        path, result = extract(force)
        assert path == first and result["cue_count"] == first_result["cue_count"]
    assert len(extractions) == 1
    # A new forced run extracts again.
    syncing.begin_forced_run(video)
    assert extract(True)[0] == first
    assert len(extractions) == 2


def test_a_forced_run_reads_a_track_without_cues_once(tmp_path: Path, monkeypatch) -> None:
    ffmpeg, ffprobe = _tool(tmp_path, "ffmpeg"), _tool(tmp_path, "ffprobe")
    video = tmp_path / "Show - 06.mkv"
    video.write_bytes(b"\0" * 1024)
    monkeypatch.setattr(syncing, "_TIMING_REFERENCES_FROM_THIS_PROCESS", {})
    monkeypatch.setattr(syncing, "_TIMING_REFERENCE_TRANSIENT_FAILURES", {})
    extractions: list[list[str]] = []
    _fake_ffmpeg(monkeypatch, extractions, "")
    syncing.begin_forced_run(video)
    for _call in range(3):
        path, result = syncing.extract_embedded_timing_reference(
            video, tmp_path / "cache", ffmpeg_path=ffmpeg, ffprobe_path=ffprobe, force=True
        )
        assert path is None and result["reason"] == "timing_reference_extract_failed"
    assert len(extractions) == 1


def test_unreliable_baseline_skips_the_source_segment_check(tmp_path: Path, monkeypatch) -> None:
    video, source, aligned = tmp_path / "video.mkv", tmp_path / "source.ass", tmp_path / "aligned.srt"
    for path in (video, source, aligned):
        path.write_text("x", encoding="utf-8")
    checked: list[Path] = []

    def evaluate(_video, subtitle, *args, **kwargs):
        checked.append(subtitle)
        return {"available": True, "reliable": True, "segments": []}

    monkeypatch.setattr(syncing, "evaluate_segment_alignment", evaluate)
    baseline = {"available": True, "reliable": False, "quality_reason": "too_many_large_jumps"}
    result = {"reason": "applied", "engine": "alass+ffsubsync", "segment_diagnostics": baseline}
    output, final = syncing._maybe_repair_piecewise(
        video,
        aligned,
        result,
        tmp_path / "cache",
        SyncConfig(),
        ffmpeg_path="ffmpeg",
        ffprobe_path="ffprobe",
        force=True,
        verbose=False,
        source_path=source,
    )
    assert output == aligned
    assert final["piecewise_repair_skipped"]["reason"] == "baseline_diagnostics_unreliable"
    assert checked == []
