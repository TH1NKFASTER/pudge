from __future__ import annotations

import json
import time
from pathlib import Path

from anime_mpv.config import LLMConfig
from anime_mpv.llm import (
    OllamaClient,
    SEMANTIC_CACHE_REJECTED_TTL_SECONDS,
    SEMANTIC_CACHE_SCHEMA,
)
from anime_mpv.syncing import subtitle_quality_accepted


def test_exact_linked_movie_ignores_noisy_semantic_samples_when_timing_is_strong() -> None:
    accepted, reason = subtitle_quality_accepted(
        {
            "sync_was_successful": True,
            "timing_reference_validation": {
                "accepted": False,
                "total_samples": 5,
                "reason": "two sampled accessibility cues describe different on-screen text",
                "alignment_mode": "alass-timestamp",
                "structure_reason": "ok",
                "reference_output_structure": {
                    "retained_ratio": 1.0,
                    "source_cues": 812,
                    "aligned_cues": 812,
                },
                "reference_activity": {"available": True, "weighted": 0.86},
            },
            "candidate_context": {
                "source": "jimaku",
                "entry_anilist_match": True,
                "entry_exact_title_match": True,
                "exact_anilist_movie_entry": True,
                "episode_match": "exact",
                "media_format": "MOVIE",
                "subtitle_suffix": ".srt",
            },
        }
    )

    assert accepted is True
    assert "точный фильм" in reason


def test_exact_linked_movie_override_rejects_bitmap_subtitle() -> None:
    accepted, reason = subtitle_quality_accepted(
        {
            "sync_was_successful": True,
            "timing_reference_validation": {
                "accepted": False,
                "total_samples": 5,
                "reason": "semantic mismatch",
                "alignment_mode": "alass-timestamp",
                "structure_reason": "ok",
                "reference_output_structure": {"retained_ratio": 1.0},
                "reference_activity": {"available": True, "weighted": 0.95},
            },
            "candidate_context": {
                "source": "jimaku",
                "entry_anilist_match": True,
                "entry_exact_title_match": True,
                "exact_anilist_movie_entry": True,
                "episode_match": "exact",
                "media_format": "MOVIE",
                "subtitle_suffix": ".sup",
            },
        }
    )

    assert accepted is False
    assert reason == "semantic mismatch"


def test_rejected_semantic_cache_expires_quickly(tmp_path: Path, monkeypatch) -> None:
    japanese = tmp_path / "ja.srt"
    english = tmp_path / "en.srt"
    japanese.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n"
        "2\n00:00:05,000 --> 00:00:06,000\n元気ですか\n",
        encoding="utf-8",
    )
    english.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
        "2\n00:00:05,000 --> 00:00:06,000\nHow are you?\n",
        encoding="utf-8",
    )
    client = OllamaClient(
        LLMConfig(
            enabled=True,
            model="test-model",
            embedded_reference_sample_count=2,
            embedded_reference_phrases_per_sample=1,
        ),
        tmp_path / "cache",
    )
    calls = 0

    def fake_chat(_system: str, _user: str):
        nonlocal calls
        calls += 1
        return {
            "same_episode": False,
            "usable_for_timing": False,
            "similarity": 0.1,
            "matched_samples": 0,
            "total_samples": 2,
            "sample_scores": [0.1, 0.1],
            "reason": "mismatch",
        }

    monkeypatch.setattr(client, "_json_chat", fake_chat)
    first = client.compare_subtitle_semantics(japanese, english)
    assert first["cached"] is False
    assert calls == 1

    cache_files = list((tmp_path / "cache" / "llm-semantic").glob("*.json"))
    assert len(cache_files) == 1
    payload = json.loads(cache_files[0].read_text(encoding="utf-8"))
    payload["cached_at"] = time.time() - SEMANTIC_CACHE_REJECTED_TTL_SECONDS - 1
    cache_files[0].write_text(json.dumps(payload), encoding="utf-8")

    second = client.compare_subtitle_semantics(japanese, english)
    client.close()

    assert second["cached"] is False
    assert calls == 2
    assert SEMANTIC_CACHE_SCHEMA == "semantic-v4"
