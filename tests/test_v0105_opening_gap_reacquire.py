from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
from pudge.subtitles.timeline_alignment import (
    _activity_bins,
    _merge_activity,
    _post_opening_gap_reacquire,
)


def test_post_opening_gap_reacquires_dominant_later_clock_inside_silence() -> None:
    pre = [
        (34.0, 36.0, "pre"),
        (40.0, 42.0, "pre"),
        (48.0, 50.0, "pre"),
        (58.0, 60.0, "pre"),
        (70.0, 72.0, "pre"),
        (84.0, 86.0, "pre"),
        (100.0, 102.0, "pre"),
        (118.0, 120.0, "pre"),
        (140.0, 142.0, "pre"),
    ]
    post_starts = [244.5, 249.0, 254.0, 259.5, 265.0, 271.0, 277.5, 284.0, 290.5, 297.0, 304.0]
    post = [(value, value + 2.2, "post") for value in post_starts]
    source_cues = pre + post

    # Before the opening the source master is about 32.75s late. After the
    # long opening gap it is about 43s late. This is the shape from the real
    # Otomege S2E7 failure, where a weak -36s bridge delayed the correct clock.
    reference_cues = [
        (start - 32.75, end - 32.75, text) for start, end, text in pre
    ] + [
        (start - 43.0, end - 43.0, text) for start, end, text in post
    ]
    source_onsets = [start for start, _end, _text in source_cues]
    reference_onsets = sorted(start for start, _end, _text in reference_cues)
    source_bins = _activity_bins(_merge_activity(source_cues))
    reference_bins = _activity_bins(_merge_activity(reference_cues))

    segments = [
        {
            "first_center": 0.0,
            "last_center": 282.0,
            "offset_seconds": -32.75,
            "support": 2,
            "mean_score": 3.2,
            "mean_coverage": 0.95,
            "windows": [],
            "kind": "stable",
        },
        {
            "first_center": 282.0,
            "last_center": 287.5,
            "offset_seconds": -36.0,
            "support": 2,
            "mean_score": 3.1,
            "mean_coverage": 0.85,
            "windows": [],
            "kind": "stable",
        },
        {
            "first_center": 287.5,
            "last_center": 1000.0,
            "offset_seconds": -43.0,
            "support": 18,
            "mean_score": 3.3,
            "mean_coverage": 0.91,
            "windows": [],
            "kind": "stable",
        },
    ]

    new_segments, new_boundaries, diagnostics = _post_opening_gap_reacquire(
        source_cues,
        source_onsets,
        reference_onsets,
        source_bins,
        reference_bins,
        segments,
        [282.0, 287.5],
    )

    assert diagnostics["applied"] is True
    assert diagnostics["reason"] == "dominant_clock_reacquired_after_opening_gap"
    assert [round(float(item["offset_seconds"]), 2) for item in new_segments] == [-32.75, -43.0]
    assert len(new_boundaries) == 1
    assert 142.0 < new_boundaries[0] < 244.5
    assert new_segments[1]["kind"] == "post_opening_reacquire"


def test_upgrade_requeues_v6_stt_selection_when_opening_clock_was_reacquired(tmp_path):
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "Movies" / "pudge"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(cfg, log=lambda _message: None)

    video = cfg.library.root_dir / "Otome S2 - 07.mkv"
    video.write_bytes(b"video")
    subtitle = cfg.paths.cache_dir / "playback-srt" / "bad-stt.srt"
    subtitle.parent.mkdir(parents=True, exist_ok=True)
    subtitle.write_text("1\n00:05:23,000 --> 00:05:25,000\n日本語\n", encoding="utf-8")
    raw = cfg.paths.cache_dir / "jimaku" / "candidate.srt"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text("dummy", encoding="utf-8")

    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=159309,
            title="Otomege Sekai wa Mob ni Kibishii Sekai desu 2",
            episode=7,
            video_path=video,
            subtitle_path=subtitle,
            subtitle_origin="jimaku",
            state="ready",
        )
    )
    manager.db.record_subtitle_history(
        video_path=video,
        media_id=159309,
        episode=7,
        source="jimaku",
        candidate_name=raw.name,
        candidate_path=raw,
        status="selected",
        reason="Preparation completed",
        details={
            "alignment": {
                "selection_reason": "early_edit_japanese_speech_verification",
                "embedded_timeline_attempt": {
                    "timeline_algorithm": "timeline-v6.0-opening-gap-reacquire",
                    "timeline_opening_gap_reacquire": {
                        "applied": True,
                        "reason": "dominant_clock_reacquired_after_opening_gap",
                    },
                },
            }
        },
    )

    assert manager._requeue_stt_timeline_clock_conflicts() == 1
    repaired = manager.db.episode_by_path(video)
    assert repaired is not None
    assert repaired.subtitle_path is None
    assert repaired.state == "waiting_subtitles"
    assert manager.db.get_state("subtitle_stt_timeline_conflict_generation", "") == "2"
    assert manager._requeue_stt_timeline_clock_conflicts() == 0
