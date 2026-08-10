from pathlib import Path

import pudge.syncing as syncing
from pudge.config import SyncConfig
from pudge.subtitle_formats import parse_srt, write_srt


def test_reference_piecewise_does_not_smear_cold_open_shift_across_long_gap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    aligned = tmp_path / "aligned.srt"
    reference = tmp_path / "reference.srt"
    cues = [
        (5.0, 8.0, "cold one"),
        (11.0, 13.0, "cold two"),
        *[
            (120.0 + index * 5.0, 122.0 + index * 5.0, f"dialogue {index}")
            for index in range(30)
        ],
    ]
    write_srt(cues, aligned)
    write_srt(cues, reference)

    def fake_window(
        *_args,
        region_start: float,
        region_end: float,
        **_kwargs,
    ) -> dict[str, object]:
        center = (region_start + region_end) / 2.0
        shift = 4.45 if center < 100.0 else 0.30
        return {
            "available": True,
            "confident": True,
            "shift_seconds": shift,
            "score": 1.0,
            "matched_onsets": 10,
            "coverage": 0.8,
            "activity_overlap": 0.9,
            "activity_correlation": 0.8,
            "source_onsets": 12,
            "reference_onsets": 12,
            "first_edge_error": 0.1,
            "last_edge_error": 0.1,
            "minimum_matches": 4,
            "score_improvement": 0.1,
            "baseline": {"score": 0.9},
            "region_start": region_start,
            "region_end": region_end,
        }

    def fake_activity(path: Path, _reference: Path, priority_seconds: float | None = None):
        repaired = Path(path) != aligned
        return {
            "available": True,
            "start": 0.90 if repaired else 0.80,
            "middle": 0.90,
            "weighted": 0.90 if repaired else 0.80,
        }

    monkeypatch.setattr(syncing, "_windowed_reference_shift", fake_window)
    monkeypatch.setattr(syncing, "compare_timing_activity", fake_activity)

    output, result = syncing.repair_with_embedded_reference_piecewise(
        aligned,
        reference,
        tmp_path / "cache",
        SyncConfig(piecewise_repair=True),
        force=True,
    )

    assert result["applied"] is True
    assert result["edit_boundaries"] == [
        {
            "left_time": 82.5,
            "right_time": 122.83333333333331,
            "boundary": 120.0,
            "left_offset": 4.45,
            "right_offset": 0.3,
            "gap_seconds": 107.0,
        }
    ]

    repaired = parse_srt(output)
    assert repaired[0][0] == 9.45
    assert repaired[1][0] == 15.45
    # The first line after the opening gap must use the post-edit clock
    # immediately, not an interpolated +2s correction.
    assert repaired[2][0] == 120.30
    assert repaired[3][0] == 125.30
