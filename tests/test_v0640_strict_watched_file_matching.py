from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from anime_mpv.config import AppConfig
from anime_mpv.database import Database
from anime_mpv.library import scan_library
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import LibraryAnime, LibraryEpisode


def test_external_scan_never_falls_back_to_permissive_local_fuzzy_match(monkeypatch, tmp_path: Path):
    root = tmp_path / "Downloads"
    root.mkdir()
    video = root / "uma.mp4"
    video.write_bytes(b"video")
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(
        LibraryAnime(
            media_id=66,
            title="Azumanga Daiou THE ANIMATION",
            titles=["Azumanga Daiou THE ANIMATION"],
            progress=0,
        )
    )
    monkeypatch.setattr(
        "anime_mpv.library.japanese_subtitle_details",
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


def test_watched_folder_rejects_high_fuzzy_but_low_literal_title_match(monkeypatch, tmp_path: Path):
    root = tmp_path / "Downloads"
    root.mkdir()
    video = root / "PrimeJourney.mp4"
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
            # Simulate an API/search-ranking false positive. The outer score is
            # deliberately high; the strict literal title gate must still reject it.
            return [
                SimpleNamespace(
                    id=77,
                    titles=["Kino no Tabi: the Beautiful World"],
                    synonyms=["Kino's Journey"],
                    score=95.0,
                    episodes=13,
                    format="TV",
                    season_year=2003,
                ),
                SimpleNamespace(
                    id=78,
                    titles=["Something Else"],
                    synonyms=[],
                    score=50.0,
                    episodes=12,
                    format="TV",
                    season_year=2004,
                ),
            ]

        def close(self):
            pass

    monkeypatch.setattr("anime_mpv.manager.AniListClient", FakeAniListClient)
    monkeypatch.setattr(
        "anime_mpv.library.japanese_subtitle_details",
        lambda *_a, **_k: ("none", None, None),
    )

    manager = AnimeManager(config, log=lambda _message: None)
    rows = manager.scan_library()

    assert all(item.video_path.resolve() != video.resolve() for item in rows)
    assert manager.db.episode_by_path(video) is None


def test_scan_removes_existing_false_match_inside_active_watched_folder(monkeypatch, tmp_path: Path):
    root = tmp_path / "Downloads"
    root.mkdir()
    video = root / "uma.mp4"
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
            return [
                SimpleNamespace(
                    id=66,
                    titles=["Azumanga Daiou THE ANIMATION"],
                    synonyms=[],
                    score=95.0,
                    episodes=26,
                    format="TV",
                    season_year=2002,
                )
            ]

        def close(self):
            pass

    monkeypatch.setattr("anime_mpv.manager.AniListClient", FakeAniListClient)
    monkeypatch.setattr(
        "anime_mpv.library.japanese_subtitle_details",
        lambda *_a, **_k: ("none", None, None),
    )

    manager = AnimeManager(config, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(media_id=66, title="Azumanga Daiou THE ANIMATION", progress=0)
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=66,
            title="Azumanga Daiou THE ANIMATION",
            episode=1,
            video_path=video,
            state="waiting_subtitles",
        )
    )
    manager.db.ensure_subtitle_job(video, media_id=66, episode=1)

    manager.scan_library()

    assert manager.db.episode_by_path(video) is None
    assert all(str(row["video_path"]) != str(video.resolve()) for row in manager.db.subtitle_jobs())
