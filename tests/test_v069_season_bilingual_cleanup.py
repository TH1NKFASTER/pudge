from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.providers.nyaa import _expected_season, score_release
from pudge.subtitle_formats import clean_srt_for_playback, parse_srt


def _release(title: str) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link="",
        torrent_url="",
        info_hash=title,
        size_text="1 GiB",
        size_bytes=1024**3,
        seeders=100,
        leechers=1,
        downloads=500,
        trusted=True,
        remake=False,
        group="Erai-raws",
    )


def _score(anime: LibraryAnime, title: str) -> NyaaRelease:
    return score_release(
        _release(title),
        anime,
        episode=5,
        batch=False,
        trusted_groups=["Erai-raws"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )


def test_trailing_numeric_and_roman_sequel_titles_require_season_two_release(tmp_path: Path) -> None:
    prequel = {"relation_type": "PREQUEL", "media_id": 1, "title": "Season one"}
    otomege = LibraryAnime(
        media_id=159309,
        title="Otomege Sekai wa Mob ni Kibishii Sekai desu 2",
        relations=[prequel],
    )
    youjo = LibraryAnime(
        media_id=999,
        title="Youjo Senki II",
        relations=[prequel],
    )

    assert _expected_season(otomege) == 2
    assert _expected_season(youjo) == 2

    wrong = _score(
        otomege,
        "[Erai-raws] Otomege Sekai wa Mob ni Kibishii Sekai Desu - 05 [1080p]",
    )
    right = _score(
        otomege,
        "[Erai-raws] Otomege Sekai wa Mob ni Kibishii Sekai Desu 2 - 05 [1080p]",
    )
    assert "season-not-specified" in wrong.reasons
    assert "season=2" in right.reasons
    assert right.score > wrong.score + 70

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    manager = AnimeManager(cfg, log=lambda _message: None)
    assert manager._release_is_allowed_for_auto(wrong) is False
    assert manager._release_is_allowed_for_auto(right) is True


def _ts(value: float) -> str:
    total_ms = round(value * 1000)
    hours, total_ms = divmod(total_ms, 3_600_000)
    minutes, total_ms = divmod(total_ms, 60_000)
    seconds, millis = divmod(total_ms, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def test_parallel_japanese_chinese_srt_removes_translation_track(tmp_path: Path) -> None:
    blocks: list[str] = []
    clock = 1.0
    index = 1
    for number in range(24):
        japanese = "これは日本語の台詞です"
        chinese = "这是中文翻译台词"
        blocks.append(
            f"{index}\n{_ts(clock)} --> {_ts(clock + 1.2)}\n{japanese}"
        )
        index += 1
        clock += 1.3
        blocks.append(
            f"{index}\n{_ts(clock)} --> {_ts(clock + 0.3)}\n{chinese}"
        )
        index += 1
        clock += 0.4
    # Ambiguous kanji-only Japanese dialogue should survive the bilingual filter.
    blocks.append(f"{index}\n{_ts(clock)} --> {_ts(clock + 0.8)}\n大丈夫?")

    source = tmp_path / "bilingual.srt"
    source.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    cleaned, result = clean_srt_for_playback(source, tmp_path / "cache")

    assert result["bilingual_cjk"] is True
    assert int(result["bilingual_removed"]) >= 24
    payload = cleaned.read_text(encoding="utf-8")
    assert "这是中文翻译台词" not in payload
    assert "これは日本語の台詞です" in payload
    assert "大丈夫?" in payload
    assert len(parse_srt(cleaned)) == 25


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.anilist.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.agent.delete_only_managed_files = True
    return AnimeManager(cfg, log=lambda _message: None)


def test_cleanup_deletes_downloaded_episode_when_torrent_hash_was_lost(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "managed.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=10,
            title="Managed",
            episode=5,
            video_path=video,
            state="ready",
            torrent_hash="",
        ),
        downloaded_at=1.0,
    )
    manager.db.schedule_cleanup(video, 0.0)

    assert manager.cleanup() == 1
    assert not video.exists()
    assert manager.db.episode_by_path(video) is None


def test_cleanup_keeps_unmanaged_episode_without_hash(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "local.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=11,
            title="Local",
            episode=1,
            video_path=video,
            state="ready",
            torrent_hash="",
        )
    )
    manager.db.schedule_cleanup(video, 0.0)

    assert manager.cleanup() == 0
    assert video.exists()
    assert manager.db.episode_by_path(video) is not None
