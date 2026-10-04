from __future__ import annotations

from pathlib import Path

import pudge.syncing as syncing
from pudge.config import SyncConfig
from pudge.subtitle_formats import parse_srt, write_srt


def test_dialogue_only_rescue_accepts_sdh_heavy_opening(tmp_path: Path, monkeypatch) -> None:
    """Regression distilled from Mushoku Tensei S03E14 CR vs AT-X captions.

    The spoken opening is about 5.3 s late after the global ALASS map, while
    parenthesized SFX/music captions make the all-cue activity metric prefer the
    broken clock.  Dialogue-only activity must be allowed to rescue the local
    embedded-reference repair.
    """
    aligned = tmp_path / "aligned.srt"
    reference = tmp_path / "reference.srt"

    ref_open = [34.69, 36.93, 45.44, 54.28, 57.79, 61.29, 66.79, 68.92,
                75.98, 77.93, 82.48, 92.73, 97.60, 99.95]
    # Spoken Japanese cues are late by ~5.3 s in the bad global map.
    candidate = [(t + 5.3, t + 7.0, f"台詞{i}") for i, t in enumerate(ref_open)]
    # Dense SDH captions that do not exist in the translated reference.
    candidate += [
        (8.0 + i * 3.0, 10.2 + i * 3.0, f"（効果音{i}）")
        for i in range(33)
    ]
    # Opening music marker should not drive dialogue validation.
    candidate += [(120.0, 198.0, "♬～")]

    ref = [(t, t + 1.7, f"EN {i}") for i, t in enumerate(ref_open)]
    # Plenty of already-correct post-opening dialogue keeps the middle stable.
    for i in range(50):
        t = 206.0 + i * 10.0
        candidate.append((t, t + 1.8, f"後半{i}"))
        ref.append((t + 0.2, t + 2.0, f"Later {i}"))

    candidate.sort()
    ref.sort()
    write_srt(candidate, aligned, preserve_order=True)
    write_srt(ref, reference, preserve_order=True)

    def fake_window(*_args, region_start: float, region_end: float, **_kwargs):
        center = (region_start + region_end) / 2.0
        shift = -5.3 if center <= 90.0 else 0.2
        return {
            "available": True,
            "confident": True,
            "shift_seconds": shift,
            "score": 2.5,
            "matched_onsets": 10,
            "coverage": 0.8,
            "activity_overlap": 0.8,
            "activity_correlation": 0.5,
            "source_onsets": 12,
            "reference_onsets": 12,
            "first_edge_error": 0.2,
            "last_edge_error": 0.2,
            "minimum_matches": 4,
            "score_improvement": 0.5,
            "baseline": {"score": 1.0},
            "region_start": region_start,
            "region_end": region_end,
        }

    # Reproduce the incident: all-cue activity says the repair is worse because
    # it scores SDH/SFX captions against a dialogue-only translation track.
    def fake_all_activity(path: Path, _reference: Path, priority_seconds: float = 180.0):
        repaired = Path(path) != aligned
        return {
            "available": True,
            "start": 0.515 if repaired else 0.541,
            "middle": 0.926 if repaired else 0.925,
            "end": 0.95,
            "full": 0.91,
            "weighted": 0.648 if repaired else 0.659,
        }

    def fake_retime(cues, _corrections):
        repaired = [
            (start - 5.3, end - 5.3, text) if start < 120.0 else (start, end, text)
            for start, end, text in cues
        ]
        return repaired, {"reason": "safe"}

    monkeypatch.setattr(syncing, "_windowed_reference_shift", fake_window)
    monkeypatch.setattr(syncing, "compare_timing_activity", fake_all_activity)
    monkeypatch.setattr(syncing, "_retime_cues_without_reordering", fake_retime)

    output, result = syncing.repair_with_embedded_reference_piecewise(
        aligned, reference, tmp_path / "cache", SyncConfig(piecewise_repair=True), force=True
    )

    assert output != aligned
    assert result["applied"] is True
    assert result["dialogue_rescue"] is True
    assert result["cold_absolute_quality_ok"] is False
    assert result["dialogue_start_gain"] >= 0.08
    assert result["dialogue_weighted_gain"] >= 0.025

    repaired = parse_srt(output)
    first_dialogue = next(cue for cue in repaired if cue[2] == "台詞0")
    assert abs(first_dialogue[0] - ref_open[0]) < 1.0
