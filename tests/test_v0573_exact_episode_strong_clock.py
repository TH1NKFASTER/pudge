from __future__ import annotations

from pathlib import Path

from pudge.config import SyncConfig
from pudge.models import SubtitleCandidate
from pudge.syncing import optimize_candidates, subtitle_quality_accepted


def _candidate(path: Path, *, activity_score: float = 0.93) -> SubtitleCandidate:
    return SubtitleCandidate(
        path=path,
        source="jimaku",
        score=99.0,
        name="[shincaps] Mushoku Tensei III - 06.srt",
        episode=6,
        verified_japanese=True,
        details={
            "episode_match": "exact",
            "entry_anilist_match": True,
            "entry_exact_title_match": True,
            "title_similarity": 100.0,
            "media_format": "TV",
            "_test_activity": activity_score,
        },
    )


def test_exact_numbered_episode_strong_clock_skips_noisy_llm_and_audio(
    tmp_path: Path,
    monkeypatch,
) -> None:
    video = tmp_path / "Mushoku Tensei III - 06.mkv"
    subtitle = tmp_path / "Mushoku Tensei III - 06.srt"
    reference = tmp_path / "english.srt"
    aligned = tmp_path / "aligned.srt"
    for path in (video, subtitle, reference, aligned):
        path.write_text("x", encoding="utf-8")

    candidate = _candidate(subtitle)
    monkeypatch.setattr(
        "pudge.syncing.extract_embedded_timing_reference",
        lambda *args, **kwargs: (reference, {"language": "eng", "title": "CR"}),
    )
    monkeypatch.setattr(
        "pudge.syncing.synchronize_with_alass",
        lambda *args, **kwargs: (
            aligned,
            {
                "reason": "applied",
                "sync_was_successful": True,
                "offset_seconds": -42.87,
                "alass_constant_shift": False,
            },
        ),
    )
    monkeypatch.setattr(
        "pudge.syncing._validate_embedded_reference_output",
        lambda *args, **kwargs: (
            True,
            "ok",
            {
                "retained_ratio": 1.0,
                "source_cues": 409,
                "aligned_cues": 409,
            },
        ),
    )
    monkeypatch.setattr(
        "pudge.syncing.compare_timing_activity",
        lambda *args, **kwargs: {"available": True, "weighted": 0.93},
    )
    monkeypatch.setattr(
        "pudge.syncing.repair_with_embedded_reference_piecewise",
        lambda *args, **kwargs: (aligned, {"applied": False}),
    )
    monkeypatch.setattr(
        "pudge.syncing.prepare_speech_reference",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("audio fallback must not run")
        ),
    )

    selected, output, result = optimize_candidates(
        video,
        [candidate],
        tmp_path / "cache",
        SyncConfig(),
        llm=object(),
        validate_embedded_reference_with_llm=True,
    )

    assert selected is candidate
    assert output == aligned
    assert result["selection_reason"] == "exact_jimaku_strong_clock"
    validation = result["timing_reference_validation"]
    assert validation["semantic_check_skipped"] is True


def test_exact_numbered_episode_strong_clock_still_requires_very_high_activity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "episode.srt"
    reference = tmp_path / "english.srt"
    aligned = tmp_path / "aligned.srt"
    for path in (video, subtitle, reference, aligned):
        path.write_text("x", encoding="utf-8")

    candidate = _candidate(subtitle, activity_score=0.91)
    monkeypatch.setattr(
        "pudge.syncing.extract_embedded_timing_reference",
        lambda *args, **kwargs: (reference, {}),
    )
    monkeypatch.setattr(
        "pudge.syncing.synchronize_with_alass",
        lambda *args, **kwargs: (
            aligned,
            {"reason": "applied", "sync_was_successful": True},
        ),
    )
    monkeypatch.setattr(
        "pudge.syncing._validate_embedded_reference_output",
        lambda *args, **kwargs: (
            True,
            "ok",
            {"retained_ratio": 1.0, "source_cues": 409, "aligned_cues": 409},
        ),
    )
    monkeypatch.setattr(
        "pudge.syncing.compare_timing_activity",
        lambda *args, **kwargs: {"available": True, "weighted": 0.91},
    )

    class RejectingLLM:
        def compare_subtitle_semantics(self, *args, **kwargs):
            return {
                "accepted": False,
                "total_samples": 6,
                "reason": "different episode",
            }

    # Stop immediately after proving the strict shortcut did not activate.
    monkeypatch.setattr(
        "pudge.syncing.prepare_speech_reference",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no shortcut")),
    )

    try:
        optimize_candidates(
            video,
            [candidate],
            tmp_path / "cache",
            SyncConfig(),
            llm=RejectingLLM(),
            validate_embedded_reference_with_llm=True,
        )
    except RuntimeError as exc:
        assert str(exc) == "no shortcut"
    else:
        raise AssertionError("low activity unexpectedly activated the strict shortcut")


def test_quality_gate_accepts_mushoku_exact_episode_shape() -> None:
    accepted, reason = subtitle_quality_accepted(
        {
            "sync_was_successful": True,
            "timing_reference_validation": {
                "accepted": False,
                "total_samples": 6,
                "reason": "noisy semantic samples",
                "alignment_mode": "alass-timestamp",
                "structure_reason": "ok",
                "reference_output_structure": {
                    "retained_ratio": 1.0,
                    "source_cues": 409,
                    "aligned_cues": 409,
                },
                "reference_activity": {"available": True, "weighted": 0.93},
            },
            "candidate_context": {
                "source": "jimaku",
                "entry_anilist_match": True,
                "entry_exact_title_match": True,
                "episode_match": "exact",
                "media_format": "TV",
                "title_similarity": 100.0,
                "filename_score": 99.0,
                "subtitle_suffix": ".srt",
            },
        }
    )

    assert accepted is True
    assert "точная серия" in reason
