from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode


def test_v0637_distinct_watched_dirs_migrate_to_new_key(tmp_path: Path) -> None:
    watched = tmp_path / "Media"
    subtitles = tmp_path / "Subs"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[paths]\ndownload_dirs = ["{watched}"]\nsubtitle_dirs = ["{subtitles}"]\n',
        encoding="utf-8",
    )

    config = load_config(config_path)
    assert config.paths.download_dirs == [watched.resolve()]
    assert config.paths.subtitle_dirs == [subtitles.resolve()]

    write_config(config, config_path)
    text = config_path.read_text(encoding="utf-8")
    assert "watched_media_dirs =" in text
    assert "download_dirs =" not in text


def test_legacy_downloads_mirrored_as_subtitles_are_not_watched(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[paths]\ndownload_dirs = ["{downloads}"]\nsubtitle_dirs = ["{downloads}"]\n',
        encoding="utf-8",
    )

    config = load_config(config_path)
    assert config.paths.subtitle_dirs == [downloads.resolve()]
    assert config.paths.download_dirs == []


def test_scan_removes_unresolved_external_row_outside_active_watch_roots(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    library.mkdir()
    external = tmp_path / "Downloads" / "Azumanga Daiou THE ANIMATION batch.mkv"
    external.parent.mkdir()
    external.write_bytes(b"video")

    config = AppConfig()
    config.library.root_dir = library
    config.library.database_path = tmp_path / "library.sqlite3"
    config.paths.download_dirs = []
    config.anilist.enabled = False
    manager = AnimeManager(config, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(media_id=66, title="Azumanga Daiou THE ANIMATION", status="PLANNING", progress=0)
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=66,
            title="Azumanga Daiou THE ANIMATION",
            episode=1,
            video_path=external,
            state="waiting_subtitles",
        )
    )
    manager.db.ensure_subtitle_job(external, media_id=66, episode=1)

    manager.scan_library()

    assert manager.db.episode_by_path(external) is None
    assert all(str(row["video_path"]) != str(external) for row in manager.db.subtitle_jobs())


def test_scan_preserves_ready_external_row_when_watch_folder_removed(tmp_path: Path) -> None:
    library = tmp_path / "Library"
    library.mkdir()
    external = tmp_path / "Elsewhere" / "Finished.mkv"
    external.parent.mkdir()
    external.write_bytes(b"video")
    subtitle = external.with_suffix(".srt")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")

    config = AppConfig()
    config.library.root_dir = library
    config.library.database_path = tmp_path / "library.sqlite3"
    config.paths.download_dirs = []
    config.anilist.enabled = False
    manager = AnimeManager(config, log=lambda _message: None)
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=None,
            title="Finished",
            episode=1,
            video_path=external,
            subtitle_path=subtitle,
            state="ready",
        )
    )

    manager.scan_library()
    assert manager.db.episode_by_path(external) is not None


def test_caught_up_never_uses_readiness_diagnosis() -> None:
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert "function mediaInHomeSection(section,mediaId)" in html
    assert "if(mediaInHomeSection('caught_up',anime.media_id))return false;" in html
