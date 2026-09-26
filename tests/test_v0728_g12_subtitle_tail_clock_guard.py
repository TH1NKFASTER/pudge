"""Synthetic regression for a spurious +59s late subtitle clock transition."""
from __future__ import annotations

from pathlib import Path

from pudge.subtitle_formats import parse_srt, write_srt
from pudge.subtitles.timeline_alignment import (
    _suppress_false_tail_jump_from_duration_anchors,
    align_subtitle_timelines,
)


def _cues(count: int = 44) -> list[tuple[float, float, str]]:
    return [
        (1216 + n * 3.17, 1216 + n * 3.17 + 1.3 + n % 3 * .31, f"cue {n}")
        for n in range(count)
    ]


def _segments() -> list[dict[str, object]]:
    return [
        {"offset_seconds": 0.0, "support": 22, "mean_score": 0.9, "mean_coverage": .9},
        {"offset_seconds": 59.0, "support": 1, "mean_score": 0.7, "mean_coverage": .5},
    ]


def test_g12_suppress_false_tail_jump_with_many_old_clock_cue_anchors() -> None:
    source = _cues()
    # Dense, misleading reference activity includes two coincidental +59s cues,
    # but the independent dialogue edges agree with the unshifted clock.
    reference = [(start + .04, end + .06, text) for start, end, text in source]
    reference += [
        (source[index][0] + 59, source[index][1] + 59, "unrelated sign")
        for index in (1, 14)
    ]
    segments, boundaries, report = _suppress_false_tail_jump_from_duration_anchors(
        source, reference, _segments(), [1216.0],
    )
    assert report["applied"] is True
    assert report["old_clock_duration_anchors"] >= 25
    assert report["proposed_clock_duration_anchors"] <= 3
    assert len(segments) == 1 and boundaries == []
    assert segments[0]["offset_seconds"] == 0.0


def test_g12_real_late_edit_stays_aligned() -> None:
    source = _cues()
    reference = [(start + 59, end + 59, text) for start, end, text in source]
    segments, boundaries, report = _suppress_false_tail_jump_from_duration_anchors(
        source, reference, _segments(), [1216.0],
    )
    assert report["applied"] is False
    assert len(segments) == 2 and boundaries == [1216.0]
    assert report["old_clock_duration_anchors"] == 0
    assert report["proposed_clock_duration_anchors"] >= 25


def test_g12_short_ambiguous_tail_does_not_guess() -> None:
    source = _cues(9)
    reference = [(start, end, text) for start, end, text in source]
    segments, boundaries, report = _suppress_false_tail_jump_from_duration_anchors(
        source, reference, _segments(), [1216.0],
    )
    assert report["applied"] is False
    assert len(segments) == 2 and boundaries == [1216.0]


def test_g12_alignment_cache_signature_changes(tmp_path: Path) -> None:
    source = [(20 + index * 4.0, 21.3 + index * 4.0 + index % 3 * .25, str(index)) for index in range(80)]
    japanese = tmp_path / "ja.srt"
    english = tmp_path / "en.srt"
    write_srt(source, japanese, preserve_order=True)
    write_srt([(start + 7.5, end + 7.5, text) for start, end, text in source], english, preserve_order=True)
    output, report = align_subtitle_timelines(japanese, english, tmp_path, force=True)
    assert report["accepted"] is True
    assert report["timeline_algorithm"] == "timeline-v6.19-edge-zones-audio-verify"
    assert abs(parse_srt(output)[-1][0] - source[-1][0] - 7.5) <= 1.0
