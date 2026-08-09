from __future__ import annotations

import json
import time

from anime_mpv.cli import _jimaku_episode_aliases
from anime_mpv.config import AppConfig
from anime_mpv.models import AniListAnime
from anime_mpv.syncing import subtitle_quality_accepted


def _quality_result(*, activity: float, matched: int, score: float, exact_title: bool, cues: int):
    return {
        "sync_was_successful": True,
        "timing_reference_validation": {
            "total_samples": 6,
            "matched_samples": matched,
            "accepted": False,
            "reason": "semantic spot-check rejected some broadcast-master samples",
            "alignment_mode": "alass-timestamp",
            "structure_reason": "ok",
            "reference_output_structure": {
                "retained_ratio": 1.0,
                "source_cues": cues,
                "aligned_cues": cues,
            },
            "reference_activity": {"available": True, "weighted": activity},
        },
        "candidate_context": {
            "source": "jimaku",
            "entry_anilist_match": True,
            "entry_exact_title_match": exact_title,
            "episode_match": "exact",
            "media_format": "TV",
            "filename_score": score,
            "subtitle_suffix": ".srt",
        },
    }


def test_otome_exact_jimaku_episode_survives_single_bad_title_card_sample():
    # Real v0.6.64 log: 351/351 cues retained, activity ~=0.835 while the
    # semantic explanation says only the title-card sample differs.
    accepted, reason = subtitle_quality_accepted(
        _quality_result(activity=0.8352, matched=1, score=103.0, exact_title=True, cues=351)
    )
    assert accepted is True
    assert "точная серия AniList/Jimaku" in reason


def test_bleach_exact_absolute_episode_rejects_unstable_audio_clock():
    # Real v0.6.67 playback log: identity is exact, but the selected ffsubsync
    # clock is still about 4.5 seconds wrong.  The multi-window validator also
    # reports full-range oscillation and large jumps, so identity must not
    # override the timing failure.
    result = _quality_result(
        activity=0.6793, matched=2, score=98.4, exact_title=False, cues=276
    )
    result["segment_diagnostics"] = {
        "available": True,
        "reliable": False,
        "quality_reason": (
            "too_many_boundary_hits,full_range_oscillation,too_many_large_jumps"
        ),
    }
    accepted, reason = subtitle_quality_accepted(result)
    assert accepted is False
    assert "тайминг нестабилен" in reason


def test_exact_episode_still_rejects_weak_clock_and_semantics():
    accepted, _ = subtitle_quality_accepted(
        _quality_result(activity=0.50, matched=1, score=103.0, exact_title=True, cues=351)
    )
    assert accepted is False


def test_bleach_absolute_jimaku_alias_is_mapped_back_to_current_cour(tmp_path):
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    anime = AniListAnime(
        id=185874,
        titles=["BLEACH: Sennen Kessen-hen - Kashin-tan"],
        synonyms=[],
        season_year=2026,
        episodes=13,
        format="TV",
    )
    cache = cfg.paths.cache_dir / "anilist-episode-offset" / "185874.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"media_id": 185874, "offset": 40, "chain": [], "updated_at": time.time()}),
        encoding="utf-8",
    )

    aliases = _jimaku_episode_aliases(anime, 43, cfg, __import__("logging").getLogger("test"))

    assert aliases == (3,)



def test_rejected_alass_tries_constant_offset_before_audio(tmp_path, monkeypatch):
    from pathlib import Path

    from anime_mpv.config import SyncConfig
    from anime_mpv.subtitle_formats import write_srt
    from anime_mpv.syncing import optimize_subtitle

    video = tmp_path / "Bleach.2004.S17E43.mkv"
    source = tmp_path / "Nanako-Bleach-E43.srt"
    reference = tmp_path / "embedded-english.srt"
    alass = tmp_path / "alass.srt"
    video.write_bytes(b"video")
    cues = [(20.0 + i * 5.0, 21.5 + i * 5.0, f"JA-{i}") for i in range(30)]
    write_srt(cues, source)
    write_srt([(s + 10.5, e + 10.5, f"EN-{i}") for i, (s, e, _t) in enumerate(cues)], reference)
    # Simulate a non-linear ALASS result that samples the wrong scenes.
    write_srt([(s + 15.7, e + 15.7, t) for s, e, t in cues], alass)

    monkeypatch.setattr(
        "anime_mpv.syncing.extract_embedded_timing_reference",
        lambda *args, **kwargs: (reference, {"language": "eng", "title": "English"}),
    )
    monkeypatch.setattr(
        "anime_mpv.syncing.synchronize_with_alass",
        lambda *args, **kwargs: (
            alass,
            {
                "reason": "applied",
                "sync_was_successful": True,
                "offset_seconds": 15.7,
                "alass_constant_shift": False,
            },
        ),
    )
    monkeypatch.setattr(
        "anime_mpv.syncing.estimate_constant_subtitle_offsets",
        lambda *args, **kwargs: [
            {
                "available": True,
                "usable_for_semantic_sampling": True,
                "offset_seconds": 10.5,
                "matched_onsets": 28,
                "matched_regions": 8,
            }
        ],
    )

    class LLM:
        def __init__(self):
            self.calls = 0

        def compare_subtitle_semantics(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "accepted": False,
                    "reason": "wrong timestamp samples",
                    "similarity": 0.15,
                    "matched_samples": 2,
                    "total_samples": 6,
                }
            return {
                "accepted": True,
                "reason": "same scenes after constant shift",
                "similarity": 0.95,
                "matched_samples": 6,
                "total_samples": 6,
            }

    monkeypatch.setattr(
        "anime_mpv.syncing.synchronize_subtitle",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("audio/FFT must not run after constant-offset recovery")
        ),
    )
    llm = LLM()
    output, result = optimize_subtitle(
        video,
        source,
        tmp_path / "cache",
        SyncConfig(engine="auto", compare_engines=True),
        llm=llm,
        validate_embedded_reference_with_llm=True,
    )

    assert llm.calls == 2
    assert output != alass
    assert result["engine"] == "embedded-reference+constant-offset"
    assert result["offset_seconds"] == 10.5
    assert result["timing_reference_validation"]["alignment_mode"] == "constant-offset-timestamp"
