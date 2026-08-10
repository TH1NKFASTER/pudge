from __future__ import annotations

from pathlib import Path

from pudge.subtitle_formats import convert_to_plain_srt, parse_srt


def test_broadcast_ass_parallel_lines_keep_shared_timing(tmp_path: Path) -> None:
    source = tmp_path / "grand-blue.ass"
    source.write_text(
        """[Script Info]
Title: Grand Blue regression

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:49.08,0:00:51.58,Default,,0,0,0,,{\\pos(552,407)}そうなのか？
Dialogue: 0,0:00:49.08,0:00:51.58,Default,,0,0,0,,{\\pos(172,497)}まったく➡
Dialogue: 0,0:00:51.58,0:00:54.09,Default,,0,0,0,,{\\pos(172,437)}何を急に
Dialogue: 0,0:00:51.58,0:00:54.09,Default,,0,0,0,,{\\pos(192,497)}変なこと言いだしてんだか。
Dialogue: 0,0:00:54.09,0:00:56.26,Default,,0,0,0,,飲み過ぎじゃないの？
Dialogue: 0,0:00:54.09,0:00:56.26,Default,,0,0,0,,だから➡
""",
        encoding="utf-8",
    )

    output, result = convert_to_plain_srt(
        source,
        tmp_path / "cache",
        ffmpeg_path="/definitely/missing/ffmpeg",
    )

    assert result["converted"] is True
    cues = parse_srt(output)
    assert len(cues) == 3
    assert cues[0][0] == 49.08
    assert cues[0][1] == 51.48  # 100 ms safety gap before the next cue.
    assert cues[0][2] == "そうなのか？\nまったく➡"
    assert cues[1][2] == "何を急に\n変なこと言いだしてんだか。"
    assert cues[2][2] == "飲み過ぎじゃないの？\nだから➡"
    assert all(end - start > 2.0 for start, end, _text in cues)


def test_near_identical_parallel_ass_boundaries_are_merged(tmp_path: Path) -> None:
    source = tmp_path / "near-parallel.ass"
    source.write_text(
        """[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:10.00,0:00:12.00,Default,,0,0,0,,第一行
Dialogue: 0,0:00:10.05,0:00:12.08,Default,,0,0,0,,第二行
""",
        encoding="utf-8",
    )

    output, _result = convert_to_plain_srt(
        source,
        tmp_path / "cache",
        ffmpeg_path="/definitely/missing/ffmpeg",
    )

    cues = parse_srt(output)
    assert cues == [(10.0, 12.08, "第一行\n第二行")]


def test_real_partial_overlap_is_not_collapsed_into_one_cue(tmp_path: Path) -> None:
    source = tmp_path / "partial-overlap.ass"
    source.write_text(
        """[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:10.00,0:00:15.00,Default,,0,0,0,,第一
Dialogue: 0,0:00:14.50,0:00:18.00,Default,,0,0,0,,第二
""",
        encoding="utf-8",
    )

    output, _result = convert_to_plain_srt(
        source,
        tmp_path / "cache",
        ffmpeg_path="/definitely/missing/ffmpeg",
    )

    cues = parse_srt(output)
    assert len(cues) == 2
    assert cues[0] == (10.0, 14.4, "第一")
    assert cues[1] == (14.5, 18.0, "第二")


def test_generation_eight_requeues_only_old_playback_srt(tmp_path: Path) -> None:
    from pudge.config import AppConfig
    from pudge.manager import AnimeManager
    from pudge.manager_models import LibraryEpisode

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.set_state("subtitle_validation_generation", "7")

    for folder, episode in (("playback-srt", 5), ("alass", 6)):
        video = cfg.library.root_dir / f"Anime - {episode:02d}.mkv"
        subtitle = cfg.paths.cache_dir / folder / f"v10-Anime-{episode:02d}.srt"
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
    assert manager.db.get_state("subtitle_validation_generation", "") == "16"
