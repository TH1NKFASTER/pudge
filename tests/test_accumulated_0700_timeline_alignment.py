from pathlib import Path

from pudge.subtitles.timeline_alignment import align_subtitle_timelines
from pudge.subtitle_formats import write_srt


def _write(path: Path, rows: list[tuple[float, float, str]]) -> Path:
    write_srt(rows, path, preserve_order=True)
    return path


def _dialogue_rows(start: float, count: int, *, spacing: float = 4.0):
    rows = []
    for index in range(count):
        cue_start = start + index * spacing
        duration = 1.3 + (index % 3) * 0.25
        rows.append((cue_start, cue_start + duration, f"line {index}"))
    return rows


def test_timeline_alignment_constant_offset(tmp_path: Path) -> None:
    source = _dialogue_rows(20.0, 80)
    reference = [
        (start + 7.5, end + 7.5, text)
        for start, end, text in source
    ]
    source_path = _write(tmp_path / "source.srt", source)
    reference_path = _write(tmp_path / "reference.srt", reference)

    output, result = align_subtitle_timelines(
        source_path,
        reference_path,
        tmp_path / "cache",
        max_offset_seconds=30.0,
        force=True,
    )

    assert output != source_path
    assert result["accepted"] is True
    assert result["timeline_alignment_reliable"] is True
    assert abs(result["timeline_segments"][0]["offset_seconds"] - 7.5) <= 1.0


def test_timeline_alignment_recovers_large_edit_jump(tmp_path: Path) -> None:
    # The same subtitle content was authored against a different master:
    # before the opening it needs -12s; after the edit it needs +90s.
    source = _dialogue_rows(20.0, 24, spacing=3.0)
    source += _dialogue_rows(220.0, 105, spacing=5.0)

    reference = []
    for start, end, text in source:
        offset = -12.0 if start < 150.0 else 90.0
        reference.append((start + offset, end + offset, text))

    # Translation segmentation is not identical: split some English cues.
    segmented = []
    for index, (start, end, text) in enumerate(reference):
        if index % 11 == 0 and end - start > 0.8:
            middle = (start + end) / 2.0
            segmented.append((start, middle, text + " a"))
            segmented.append((middle + 0.08, end + 0.15, text + " b"))
        else:
            segmented.append((start, end, text))

    source_path = _write(tmp_path / "jp.srt", source)
    reference_path = _write(tmp_path / "en.srt", segmented)

    output, result = align_subtitle_timelines(
        source_path,
        reference_path,
        tmp_path / "cache",
        max_offset_seconds=120.0,
        force=True,
    )

    assert output != source_path
    assert result["accepted"] is True
    segments = result["timeline_segments"]
    assert len(segments) >= 2
    assert abs(segments[0]["offset_seconds"] - (-12.0)) <= 2.0
    assert abs(segments[-1]["offset_seconds"] - 90.0) <= 2.0
    jumps = result["timeline_boundaries"]
    assert any(abs(item["jump_seconds"]) >= 90.0 for item in jumps)


def test_timeline_alignment_rejects_unrelated_dense_clocks(tmp_path: Path) -> None:
    source = _dialogue_rows(10.0, 100, spacing=4.1)
    # Similar density, deliberately different rhythm. This is the kind of case
    # where raw activity alone can look deceptively strong.
    reference = []
    current = 11.0
    for index in range(100):
        current += 2.0 + ((index * 7) % 9) * 0.73
        reference.append((current, current + 1.1, f"other {index}"))

    source_path = _write(tmp_path / "source.srt", source)
    reference_path = _write(tmp_path / "reference.srt", reference)

    _output, result = align_subtitle_timelines(
        source_path,
        reference_path,
        tmp_path / "cache",
        max_offset_seconds=60.0,
        force=True,
    )

    assert result["accepted"] is False


def test_timeline_alignment_uses_raw_cue_onsets_when_activity_merges(tmp_path: Path) -> None:
    source = []
    current = 10.0
    for index in range(110):
        duration = 2.8
        source.append((current, current + duration, f"jp {index}"))
        current += 2.65 + (index % 5) * 0.11

    reference = []
    for index, (start, end, text) in enumerate(source):
        shifted_start = start + 8.25
        shifted_end = end + 8.25
        reference.append((shifted_start, shifted_end, text))
        if index % 9 == 0:
            reference.append(
                (shifted_start + 1.0, shifted_start + 1.55, text + " split")
            )

    source_path = _write(tmp_path / "dense-jp.srt", source)
    reference_path = _write(tmp_path / "dense-en.srt", reference)

    output, result = align_subtitle_timelines(
        source_path,
        reference_path,
        tmp_path / "cache",
        max_offset_seconds=30.0,
        force=True,
    )

    assert output != source_path, result
    assert result["accepted"] is True, result
    counts = result["timeline_signal_counts"]
    assert counts["source_raw_onsets"] >= 100
    assert counts["source_activity_regions"] < counts["source_raw_onsets"] / 2
    assert abs(result["timeline_segments"][0]["offset_seconds"] - 8.25) <= 1.0
