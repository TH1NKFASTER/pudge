from pudge.syncing import subtitle_quality_accepted


def test_exact_numbered_jimaku_episode_can_override_noisy_llm_title_card_sample():
    result = {
        "sync_was_successful": True,
        "timing_reference_validation": {
            "total_samples": 6,
            "accepted": False,
            "reason": "one title-card sample looked unrelated",
            "alignment_mode": "alass-timestamp",
            "structure_reason": "ok",
            "reference_output_structure": {
                "retained_ratio": 1.0,
                "source_cues": 336,
                "aligned_cues": 336,
            },
            "reference_activity": {"available": True, "weighted": 0.9206},
        },
        "candidate_context": {
            "source": "jimaku",
            "entry_anilist_match": True,
            "entry_exact_title_match": True,
            "episode_match": "exact",
            "media_format": "TV",
            # Japanese filenames can have zero Latin-title similarity and a
            # lower filename score even though the Jimaku entry itself is exact.
            "title_similarity": 0.0,
            "filename_score": 77.0,
            "subtitle_suffix": ".srt",
        },
    }

    accepted, reason = subtitle_quality_accepted(result)

    assert accepted is True
    assert "точная серия AniList/Jimaku" in reason


def test_exact_numbered_episode_still_requires_strong_timing_structure():
    result = {
        "sync_was_successful": True,
        "timing_reference_validation": {
            "total_samples": 6,
            "accepted": False,
            "reason": "semantic mismatch",
            "alignment_mode": "alass-timestamp",
            "structure_reason": "ok",
            "reference_output_structure": {
                "retained_ratio": 1.0,
                "source_cues": 336,
                "aligned_cues": 330,
            },
            "reference_activity": {"available": True, "weighted": 0.70},
        },
        "candidate_context": {
            "source": "jimaku",
            "entry_anilist_match": True,
            "entry_exact_title_match": True,
            "episode_match": "exact",
            "media_format": "TV",
            "subtitle_suffix": ".srt",
        },
    }

    accepted, _ = subtitle_quality_accepted(result)
    assert accepted is False
