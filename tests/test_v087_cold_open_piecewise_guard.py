from pathlib import Path


def test_piecewise_does_not_reintroduce_rejected_cold_alias_via_broad_window(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression for Hyakkano S03E08 / SubsPlease release 32.

    The short +9.6s cold probe was correctly rejected, but the first broad
    0-134s window rediscovered the same cadence alias.  A second +2.6s cold
    probe also had a ~11s boundary error and must not be trusted merely because
    its offset is below 8 seconds.
    """
    import pudge.syncing as syncing
    from pudge.config import SyncConfig
    from pudge.subtitle_formats import write_srt

    aligned = tmp_path / "aligned.srt"
    reference = tmp_path / "reference.srt"
    cues = [(5.0 + i * 4.7, 6.6 + i * 4.7, f"cue-{i}") for i in range(285)]
    write_srt(cues, aligned)
    write_srt(cues, reference)

    monkeypatch.setattr(
        syncing,
        "_repair_sparse_cold_open",
        lambda aligned, *_args, **_kwargs: (
            aligned,
            {"applied": False, "reason": "test-no-sparse-repair"},
        ),
    )
    monkeypatch.setattr(
        syncing,
        "_repair_stable_opening_plateaus",
        lambda aligned, *_args, **_kwargs: (
            aligned,
            {"applied": False, "reason": "test-no-plateau-repair"},
        ),
    )

    def fake_window(
        *_args,
        region_start: float,
        region_end: float,
        **_kwargs,
    ) -> dict[str, object]:
        center = (region_start + region_end) / 2.0
        common: dict[str, object] = {
            "available": True,
            "confident": True,
            "score": 3.0,
            "matched_onsets": 12,
            "coverage": 0.8,
            "activity_overlap": 0.95,
            "activity_correlation": 0.8,
            "source_onsets": 14,
            "reference_onsets": 14,
            "first_edge_error": 0.2,
            "last_edge_error": 0.2,
            "minimum_matches": 4,
            "score_improvement": 0.2,
            "baseline": {"score": 2.8},
            "region_start": region_start,
            "region_end": region_end,
        }
        if abs(region_start) < 1e-6 and abs(region_end - 35.0) < 1e-6:
            return {
                **common,
                "shift_seconds": 9.6,
                "matched_onsets": 5,
                "coverage": 0.7143,
                "source_onsets": 8,
                "reference_onsets": 7,
                "first_edge_error": 1.471,
                "last_edge_error": 1.359,
                "score_improvement": 3.29,
            }
        if abs(region_start - 10.0) < 1e-6 and abs(region_end - 50.0) < 1e-6:
            return {
                **common,
                "shift_seconds": 2.6,
                "matched_onsets": 4,
                "coverage": 0.6667,
                "source_onsets": 6,
                "reference_onsets": 9,
                "first_edge_error": 0.43,
                "last_edge_error": 10.889,
                "score_improvement": 0.21,
            }
        # The first broad region starts at zero and spans roughly 134 seconds
        # for this episode duration.  It repeats the rejected +9.6s alias.
        if abs(region_start) < 1e-6 and region_end > 100.0:
            return {
                **common,
                "shift_seconds": 9.6,
                "matched_onsets": 5,
                "coverage": 0.625,
                "source_onsets": 8,
                "reference_onsets": 13,
                "first_edge_error": 1.471,
                "last_edge_error": 101.549,
                "score_improvement": 0.70,
            }
        return {**common, "shift_seconds": 0.15}

    monkeypatch.setattr(syncing, "_windowed_reference_shift", fake_window)

    output, result = syncing.repair_with_embedded_reference_piecewise(
        aligned,
        reference,
        tmp_path / "cache",
        SyncConfig(piecewise_repair=True),
    )

    assert output == aligned
    assert result["applied"] is False
    assert result["reason"] == "reference_piecewise_not_needed"
    assert len(result["weak_large_cold_probes"]) == 2
    assert result["contaminated_broad_times"]
    assert all(abs(offset) < 0.75 for _timepoint, offset in result["anchors"])
