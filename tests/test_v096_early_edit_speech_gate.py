"""Early timeline edits need Japanese speech before they are trusted (behaviour; audit 71Б rewrite)."""

from pathlib import Path

from pudge.subtitle_formats import write_srt
from pudge.subtitles.timeline_alignment import (
    _WindowMatch,
    _early_edit_audio_verification_risk,
)


def _row(center: float, offset: float) -> _WindowMatch:
    return _WindowMatch(
        center=center,
        offset=offset,
        score=3.0,
        matched=12,
        source_count=14,
        reference_count=14,
        onset_coverage=0.9,
        onset_f1=0.85,
        activity_f1=0.85,
        mean_error=0.2,
        rank_delta=0.01,
        gap_fingerprint=0.9,
        edge_hint_distance=None,
    )


def test_bleach_like_early_clock_change_requires_speech_verification() -> None:
    risk = _early_edit_audio_verification_risk(
        [_row(38, 0), _row(146, 18), _row(218, -10), _row(254, -10)],
        {"applied": False, "delta_seconds": -1.3, "gap_seconds": 102, "boundary_source_time": 103},
        [
            {"applied": True, "boundary_index": 0, "old_source_time": 188.5, "new_source_time": 193.2},
            {"applied": True, "boundary_index": 0, "old_source_time": 193.2, "new_source_time": 211.0},
        ],
    )
    assert risk["required"] is True
    assert "early_path_clock_change" in risk["reasons"]
    assert "early_boundary_delayed_for_monotonicity" in risk["reasons"]


def test_otome_like_opening_gap_ambiguity_requires_speech_verification() -> None:
    risk = _early_edit_audio_verification_risk(
        [_row(70, -34), _row(178, -35), _row(250, -35), _row(286, -35)],
        {
            "applied": False,
            "delta_seconds": 1.98,
            "gap_seconds": 85.32,
            "boundary_source_time": 151.2,
        },
        [],
    )
    assert risk["required"] is True
    assert risk["reasons"] == ["opening_gap_clock_ambiguity"]


def test_stable_timeline_keeps_fast_path() -> None:
    risk = _early_edit_audio_verification_risk(
        [_row(60, -4), _row(168, -4.25), _row(240, -4), _row(312, -4)],
        {"applied": False, "delta_seconds": 0.5, "gap_seconds": 80, "boundary_source_time": 140},
        [],
    )
    assert risk["required"] is False


def test_unverified_early_edit_is_not_trusted_through_the_exact_release_clock(
    tmp_path: Path, monkeypatch
) -> None:
    """An exact Jimaku release whose embedded-reference timeline has a risky early edit goes
    to Japanese speech first. When speech cannot verify it, the release must not be accepted
    anyway as a strong exact-release clock. Routing a verifiable case through speech is
    test_v0101_embedded_opening_scaffold.py::test_optimize_candidates_routes_unstable_sparse_preop_through_stt."""
    import pudge.syncing as syncing
    from pudge.config import SyncConfig
    from pudge.models import SubtitleCandidate

    video = tmp_path / "[SubsPlease] Show - 05.mkv"
    video.write_bytes(b"video")
    source = tmp_path / "show-05.srt"
    reference = tmp_path / "embedded-eng.srt"
    aligned = tmp_path / "timeline.srt"
    cues = [(10.0 + 9.0 * i, 11.5 + 9.0 * i, f"台詞{i}") for i in range(130)]
    write_srt(cues, source)
    write_srt([(start + 2.0, end + 2.0, f"r{i}") for i, (start, end, _text) in enumerate(cues)], reference)
    write_srt([(start + 2.0, end + 2.0, text) for start, end, text in cues], aligned)
    # Every identity and timing signal is as strong as it gets for one exact release.
    candidate = SubtitleCandidate(
        path=source,
        source="jimaku",
        score=100.0,
        name=source.name,
        details={
            "episode_match": "exact",
            "entry_anilist_match": True,
            "entry_exact_title_match": True,
            "title_similarity": 100.0,
        },
    )
    timeline = {
        "reason": "applied",
        "accepted": True,
        "sync_was_successful": True,
        "timeline_alignment_reliable": True,
        "engine": "embedded-reference+timeline",
        "timeline_segments": [{"offset": 2.0}],
        "timeline_early_edit_audio_verification": {"required": True, "reasons": ["early_path_clock_change"]},
    }
    monkeypatch.setattr(
        syncing,
        "extract_embedded_timing_reference",
        lambda *_a, **_k: (reference, {"language": "eng", "title": "English"}),
    )
    monkeypatch.setattr(syncing, "_exact_release_zero_offset_result", lambda *_a, **_k: None)
    monkeypatch.setattr(syncing, "align_subtitle_timelines", lambda *_a, **_k: (aligned, dict(timeline)))
    monkeypatch.setattr(
        syncing,
        "synchronize_with_alass",
        lambda *_a, **_k: (
            aligned,
            {
                "reason": "applied",
                "sync_was_successful": True,
                "offset_seconds": 2.0,
                "alass_constant_shift": True,
            },
        ),
    )
    monkeypatch.setattr(
        syncing, "compare_timing_activity", lambda *_a, **_k: {"available": True, "weighted": 0.97}
    )
    speech: list[Path] = []
    monkeypatch.setattr(
        syncing,
        "_try_japanese_stt_fallback",
        lambda _video, subtitle, *_a, **_k: (
            speech.append(subtitle) or (None, {"reason": "stt_unavailable", "sync_was_successful": False})
        ),
    )

    _chosen, _output, result = syncing.optimize_candidates(
        video, [candidate], tmp_path / "cache", SyncConfig()
    )

    assert speech and set(speech) == {source}
    assert result["sync_was_successful"] is False
    assert result["reason"] == "timeline_edit_requires_audio_verification"
    assert result.get("selection_reason") != "exact_jimaku_strong_clock"
