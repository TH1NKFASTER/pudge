from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.config import AppConfig, write_config
from pudge.filename import title_similarity
from pudge.library import scan_library
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


def test_catmahjong_explains_old_mahoyo_false_positive_but_external_scan_rejects_it(monkeypatch, tmp_path: Path):
    # This is the exact old failure mode: WRatio/token matching considered the
    # unrelated filename "catmahjong" 60% similar to the short synonym "Mahoyo",
    # just above the legacy local matcher threshold of 58.
    assert title_similarity("catmahjong", "Mahoyo") >= 58.0

    root = tmp_path / "Downloads"
    root.mkdir()
    video = root / "catmahjong.mp4"
    video.write_bytes(b"video")

    from pudge.database import Database

    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(
        LibraryAnime(
            media_id=999,
            title="Mahoutsukai no Yoru",
            titles=["Mahoutsukai no Yoru"],
            synonyms=["Mahoyo"],
            media_status="NOT_YET_RELEASED",
        )
    )
    monkeypatch.setattr(
        "pudge.library.japanese_subtitle_details",
        lambda *_a, **_k: ("none", None, None),
    )

    rows = scan_library(
        root,
        db,
        anime_resolver=lambda _identity: None,
        require_anime_match=True,
    )

    assert rows == []
    assert db.episode_by_path(video) is None


def test_scan_revalidates_and_removes_old_ready_false_match(monkeypatch, tmp_path: Path):
    root = tmp_path / "Downloads"
    root.mkdir()
    video = root / "catmahjong.mp4"
    video.write_bytes(b"video")

    config = AppConfig()
    config.library.root_dir = tmp_path / "Library"
    config.library.root_dir.mkdir()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.paths.download_dirs = [root]
    config.anilist.enabled = True
    config.anilist.access_token = "token"

    class FakeAniListClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def search(self, _identity):
            # The API candidate may score high internally, but literal title
            # similarity remains far below the strict watched-folder threshold.
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
    monkeypatch.setattr(
        "pudge.library.japanese_subtitle_details",
        lambda *_a, **_k: ("none", None, None),
    )

    manager = AnimeManager(config, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=999,
            title="Mahoutsukai no Yoru",
            synonyms=["Mahoyo"],
            media_status="NOT_YET_RELEASED",
        )
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=999,
            title="Mahoutsukai no Yoru",
            episode=None,
            video_path=video,
            state="ready",
        )
    )

    manager.scan_library()

    assert manager.db.episode_by_path(video) is None


def test_future_local_match_is_library_only_never_ready_home(tmp_path: Path):
    watched = tmp_path / "Downloads"
    watched.mkdir()
    video = watched / "Mahoutsukai no Yoru.mkv"
    video.write_bytes(b"video")

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "Library"
    cfg.paths.download_dirs = [watched]
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)

    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=999,
            title="Mahoutsukai no Yoru",
            status="PLANNING",
            episodes=1,
            format="MOVIE",
            media_status="NOT_YET_RELEASED",
            start_date="2099-11-01",
        )
    )
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=999,
            title="Mahoutsukai no Yoru",
            episode=None,
            video_path=video,
            state="ready",
        )
    )

    state = api.get_state()

    assert any(group["media_id"] == 999 for group in state["library"])
    library_group = next(group for group in state["library"] if group["media_id"] == 999)
    assert library_group["watched_folder"] is True
    assert all(
        item.get("media_id") != 999
        for section in state["home"].values()
        if isinstance(section, list)
        for item in section
    )
