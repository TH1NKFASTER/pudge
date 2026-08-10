from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.branding import APP_NAME, APP_SLUG, DEFAULT_LIBRARY_DIR, LEGACY_APP_NAMES, LEGACY_APP_SLUGS
from pudge.config import AppConfig
from pudge.database import Database
from pudge.library import scan_library
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from brand_migration import migrate_paths, rewrite_config

ROOT = Path(__file__).parents[1]


def test_product_is_now_pudge_with_legacy_migration_metadata() -> None:
    assert APP_NAME == "pudge"
    assert APP_SLUG == "pudge"
    assert DEFAULT_LIBRARY_DIR.name == "pudge"
    assert "Anime MPV" in LEGACY_APP_NAMES
    assert "anime-mpv" in LEGACY_APP_SLUGS

    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'brand_migration.py" paths' in installer
    assert 'brand_migration.py" config' in installer
    assert 'killall Dock' in installer
    assert 'ln -s "$APP_PATH" "$legacy_app"' in installer
    assert 'APP_PATH="$APP_DIR/$APP_NAME.app"' in installer



def test_brand_path_migration_moves_old_defaults_and_rewrites_config(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old_library = home / "Movies" / "Anime MPV"
    old_library.mkdir(parents=True)
    (old_library / "episode.mkv").write_bytes(b"video")
    old_config_dir = home / ".config" / "anime-mpv"
    old_config_dir.mkdir(parents=True)
    old_data = home / ".local" / "share" / "anime-mpv"
    old_data.mkdir(parents=True)
    (old_data / "library.sqlite3").write_bytes(b"db")
    old_cache = home / "Library" / "Caches" / "anime-mpv"
    old_cache.mkdir(parents=True)
    (old_cache / "cache.txt").write_text("cache", encoding="utf-8")
    logs = home / "Library" / "Logs"
    logs.mkdir(parents=True)
    (logs / "anime-mpv-energy.jsonl").write_text("{}\n", encoding="utf-8")
    config = old_config_dir / "config.toml"
    config.write_text(
        f'[paths]\ncache_dir = "{old_cache}"\n'
        f'[library]\nroot_dir = "{old_library}"\ndatabase_path = "{old_data / "library.sqlite3"}"\n'
        '[qbittorrent]\ncategory = "anime-mpv"\n',
        encoding="utf-8",
    )

    migrate_paths(
        home,
        app_name="pudge",
        app_slug="pudge",
        legacy_names=["Anime MPV"],
        legacy_slugs=["anime-mpv"],
    )
    new_config = home / ".config" / "pudge" / "config.toml"
    rewrite_config(
        new_config,
        home,
        app_name="pudge",
        app_slug="pudge",
        legacy_names=["Anime MPV"],
        legacy_slugs=["anime-mpv"],
    )

    assert (home / "Movies" / "pudge" / "episode.mkv").is_file()
    assert not old_library.exists()
    assert (home / ".local" / "share" / "pudge" / "library.sqlite3").is_file()
    assert (home / "Library" / "Caches" / "pudge" / "cache.txt").is_file()
    assert (logs / "pudge-energy.jsonl").is_file()
    text = new_config.read_text(encoding="utf-8")
    assert str(home / "Movies" / "pudge") in text
    assert str(home / ".local" / "share" / "pudge") in text
    assert 'category = "pudge"' in text

def test_managed_library_detaches_false_match_even_with_stale_torrent_hash(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "Library"
    root.mkdir()
    video = root / "catmahjong.mp4"
    video.write_bytes(b"video")
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=999, title="Mahoutsukai no Yoru", synonyms=["Mahoyo"]))
    db.upsert_episode(
        LibraryEpisode(
            media_id=999,
            title="Mahoutsukai no Yoru",
            episode=None,
            video_path=video,
            state="ready",
            torrent_hash="deadbeef",
        )
    )
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))

    rows = scan_library(root, db)
    assert len(rows) == 1
    assert rows[0].media_id is None
    persisted = db.episode_by_path(video)
    assert persisted is not None
    assert persisted.media_id is None
    assert persisted.title == "catmahjong"


def test_external_library_removes_false_match_with_stale_hash(monkeypatch, tmp_path: Path) -> None:
    watched = tmp_path / "Downloads"
    watched.mkdir()
    video = watched / "catmahjong.mp4"
    video.write_bytes(b"video")

    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "Library"
    cfg.library.root_dir.mkdir()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.download_dirs = [watched]
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"

    class FakeAniListClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def search(self, _identity):
            return [
                SimpleNamespace(
                    id=999,
                    titles=["Mahoutsukai no Yoru"],
                    synonyms=["Mahoyo"],
                    score=95.0,
                    episodes=1,
                    format="MOVIE",
                    season_year=2026,
                )
            ]

        def close(self):
            pass

    monkeypatch.setattr("pudge.manager.AniListClient", FakeAniListClient)
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))

    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(LibraryAnime(media_id=999, title="Mahoutsukai no Yoru", synonyms=["Mahoyo"]))
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=999,
            title="Mahoutsukai no Yoru",
            episode=None,
            video_path=video,
            state="ready",
            torrent_hash="deadbeef",
        )
    )

    manager.scan_library()
    assert manager.db.episode_by_path(video) is None


def test_empty_nested_library_directories_are_pruned_but_root_is_kept(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "pudge"
    cfg.library.root_dir.mkdir()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    nested = cfg.library.root_dir / "Anime A" / "Season 1" / "subs"
    nested.mkdir(parents=True)
    nonempty = cfg.library.root_dir / "Anime B"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("x", encoding="utf-8")

    manager = AnimeManager(cfg, log=lambda _message: None)
    removed = manager._prune_empty_library_dirs()

    assert removed == 3
    assert cfg.library.root_dir.is_dir()
    assert not (cfg.library.root_dir / "Anime A").exists()
    assert nonempty.is_dir()
