from __future__ import annotations

from pathlib import Path

from anime_mpv.config import AppConfig
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import LibraryAnime, LibraryEpisode


def test_scan_merges_pre_rename_and_pudge_rows_for_same_physical_episode(monkeypatch, tmp_path: Path) -> None:
    movies = tmp_path / "Movies"
    current_root = movies / "pudge"
    current_root.mkdir(parents=True)
    legacy_root = movies / "Anime MPV"

    video_name = "Seihantai na Kimi to Boku 2nd Season S02E06.mkv"
    current_video = current_root / video_name
    current_video.write_bytes(b"video")
    subtitle = current_root / "episode6.ja.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")

    cfg = AppConfig()
    cfg.library.root_dir = current_root
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.download_dirs = []
    cfg.anilist.enabled = False

    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=123,
            title="Seihantai na Kimi to Boku 2nd Season",
            titles=["Seihantai na Kimi to Boku 2nd Season"],
            episodes=12,
            duration=24,
        )
    )

    legacy_video = legacy_root / video_name
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=123,
            title="Seihantai na Kimi to Boku 2nd Season",
            episode=6,
            video_path=legacy_video,
            subtitle_path=subtitle,
            state="ready",
            torrent_hash="abc123",
            playback_duration=1440.0,
        )
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=123,
            title="Seihantai na Kimi to Boku 2nd Season",
            episode=6,
            video_path=current_video,
            state="local",
        )
    )
    manager.db.record_playback(legacy_video, 120.0, 1440.0)
    manager.db.record_playback(current_video, 0.0, 1440.0)

    monkeypatch.setattr(
        "anime_mpv.library.japanese_subtitle_details",
        lambda *_args, **_kwargs: ("none", None, None),
    )

    rows = manager.scan_library()
    persisted = manager.db.episodes(123)

    assert len(rows) == 1
    assert len(persisted) == 1
    assert persisted[0].video_path == current_video
    assert persisted[0].state == "ready"
    assert persisted[0].subtitle_path == subtitle
    assert persisted[0].torrent_hash == "abc123"
    assert persisted[0].playback_duration == 1440.0


def test_scan_rewrites_single_stale_legacy_row_before_rescan(monkeypatch, tmp_path: Path) -> None:
    movies = tmp_path / "Movies"
    current_root = movies / "pudge"
    current_root.mkdir(parents=True)
    legacy_root = movies / "Anime MPV"
    current_video = current_root / "Example Anime S01E03.mkv"
    current_video.write_bytes(b"video")

    cfg = AppConfig()
    cfg.library.root_dir = current_root
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.download_dirs = []
    cfg.anilist.enabled = False

    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(LibraryAnime(media_id=55, title="Example Anime", titles=["Example Anime"]))
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=55,
            title="Example Anime",
            episode=3,
            video_path=legacy_root / current_video.name,
            state="waiting_subtitles",
            torrent_hash="hash55",
        )
    )
    monkeypatch.setattr(
        "anime_mpv.library.japanese_subtitle_details",
        lambda *_args, **_kwargs: ("none", None, None),
    )

    manager.scan_library()
    rows = manager.db.episodes(55)

    assert len(rows) == 1
    assert rows[0].video_path == current_video
    assert rows[0].torrent_hash == "hash55"
