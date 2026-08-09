from pathlib import Path

import pytest

from anime_mpv.subtitle_formats import parse_srt, write_srt
from anime_mpv.syncing import refine_with_embedded_reference_groups


def test_split_merge_does_not_scale_japanese_cues_to_longer_english_groups(tmp_path: Path) -> None:
    candidate: list[tuple[float, float, str]] = []
    reference: list[tuple[float, float, str]] = []
    for index in range(24):
        base = 10.0 + index * 7.0
        candidate.extend(
            [
                (base + 0.20, base + 1.60, f"日本語 {index}a"),
                (base + 1.70, base + 4.20, f"日本語 {index}b"),
            ]
        )
        # A normal localization pattern: one English cue is materially longer
        # than the two Japanese broadcast captions combined.  Its outer span
        # must not be used to rescale the internal Japanese boundary.
        reference.append((base, base + 4.85, f"English {index}"))

    aligned = tmp_path / "aligned.srt"
    embedded = tmp_path / "english.srt"
    write_srt(candidate, aligned)
    write_srt(reference, embedded)

    output, result = refine_with_embedded_reference_groups(
        aligned, embedded, tmp_path / "cache", force=True
    )

    assert output == aligned
    assert result["applied"] is False
    assert result["reason"] in {
        "reference_groups_insufficient_coverage",
        "reference_groups_no_safe_improvement",
    }
    assert parse_srt(output) == parse_srt(aligned)


def test_applied_group_refinement_preserves_each_japanese_duration(tmp_path: Path) -> None:
    candidate: list[tuple[float, float, str]] = []
    reference: list[tuple[float, float, str]] = []
    for index in range(24):
        base = 8.0 + index * 7.0
        candidate.extend(
            [
                (base + 0.30, base + 1.80, f"日本語 {index}a"),
                (base + 1.90, base + 4.30, f"日本語 {index}b"),
            ]
        )
        reference.append((base, base + 4.0, f"English {index}"))

    aligned = tmp_path / "aligned.srt"
    embedded = tmp_path / "english.srt"
    write_srt(candidate, aligned)
    write_srt(reference, embedded)

    output, result = refine_with_embedded_reference_groups(
        aligned, embedded, tmp_path / "cache", force=True
    )

    assert result["applied"] is True
    before = parse_srt(aligned)
    after = parse_srt(output)
    assert len(before) == len(after)
    assert result["large_duration_changes"] == 0
    for old, new in zip(before, after):
        assert new[1] - new[0] == pytest.approx(old[1] - old[0], abs=0.002)
