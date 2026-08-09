from pathlib import Path

from anime_mpv.models import SubtitleCandidate
from anime_mpv.syncing import (
    _exact_jimaku_timing_consensus,
    _rank_embedded_reference_candidates,
)


def _item(path: Path, *, activity: float, score: float, structure_reason: str = "ok"):
    path.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
    candidate = SubtitleCandidate(
        path=path,
        source="jimaku",
        score=score,
        name=path.name,
        details={
            "episode_match": "exact",
            "entry_anilist_match": True,
            "entry_exact_title_match": True,
            "title_similarity": 100.0,
        },
    )
    return (
        (1.0, activity, float(path.suffix == ".srt"), score),
        candidate,
        path,
        {"sync_was_successful": True},
        {"available": True, "weighted": activity},
        {
            "reason": structure_reason,
            "retained_ratio": 1.0,
            "source_cues": 150,
            "aligned_cues": 150,
        },
    )


def test_tiny_activity_difference_does_not_override_prefer_srt(tmp_path: Path) -> None:
    srt = _item(tmp_path / "[shincaps] Grand Blue S03E05.srt", activity=0.8935, score=107.0)
    ass = _item(tmp_path / "[shincaps] Grand Blue S03E05.ass", activity=0.8940, score=101.0)

    ranked, metadata = _rank_embedded_reference_candidates([ass, srt], prefer_srt=True)

    assert ranked[0][1].path.suffix == ".srt"
    assert metadata["format_preference_applied"] is True
    assert metadata["raw_best"].endswith(".ass")
    assert metadata["selected"].endswith(".srt")


def test_material_activity_advantage_still_allows_ass_to_win(tmp_path: Path) -> None:
    srt = _item(tmp_path / "same-release.srt", activity=0.8935, score=107.0)
    ass = _item(tmp_path / "same-release.ass", activity=0.9040, score=101.0)

    ranked, metadata = _rank_embedded_reference_candidates([srt, ass], prefer_srt=True)

    assert ranked[0][1].path.suffix == ".ass"
    assert metadata["format_preference_applied"] is False


def test_invalid_srt_structure_never_wins_tolerance_tie(tmp_path: Path) -> None:
    srt = _item(
        tmp_path / "broken.srt",
        activity=0.9000,
        score=110.0,
        structure_reason="too_many_missing_cues",
    )
    ass = _item(tmp_path / "valid.ass", activity=0.8990, score=100.0)

    ranked, _metadata = _rank_embedded_reference_candidates([srt, ass], prefer_srt=True)

    assert ranked[0][1].path.suffix == ".ass"


def test_exact_strong_clock_consensus_honors_srt_tolerance_order(tmp_path: Path) -> None:
    srt = _item(tmp_path / "same-release.srt", activity=0.9395, score=107.0)
    ass = _item(tmp_path / "same-release.ass", activity=0.9400, score=101.0)

    ranked, _metadata = _rank_embedded_reference_candidates([ass, srt], prefer_srt=True)
    selected, payload = _exact_jimaku_timing_consensus(ranked)

    assert selected is not None
    assert selected[1].path.suffix == ".srt"
    assert payload["reason"] == "exact_jimaku_strong_clock"


def test_generation_eleven_requeues_only_generated_playback_outputs(tmp_path: Path) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.manager import AnimeManager
    from anime_mpv.manager_models import LibraryEpisode

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.set_state("subtitle_validation_generation", "9")

    for folder, episode in (("playback-srt", 5), ("alass", 6)):
        video = cfg.library.root_dir / f"Anime - {episode:02d}.mkv"
        subtitle = cfg.paths.cache_dir / folder / f"v11-Anime-{episode:02d}.srt"
        video.parent.mkdir(parents=True, exist_ok=True)
        subtitle.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"video")
        subtitle.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n日本語\n",
            encoding="utf-8",
        )
        manager.db.upsert_episode(
            LibraryEpisode(
                media_id=1,
                title="Anime",
                episode=episode,
                video_path=video,
                subtitle_path=subtitle,
                state="ready",
            )
        )

    assert manager._requeue_legacy_generated_subtitles() == 1
    playback = manager.db.episode_by_path(cfg.library.root_dir / "Anime - 05.mkv")
    alass = manager.db.episode_by_path(cfg.library.root_dir / "Anime - 06.mkv")
    assert playback is not None and playback.subtitle_path is None
    assert alass is not None and alass.subtitle_path is not None
    assert manager.db.get_state("subtitle_validation_generation", "") == "14"
