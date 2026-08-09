from __future__ import annotations

from pathlib import Path

from anime_mpv.config import AppConfig, write_config
from anime_mpv.database import Database
from anime_mpv.library import scan_library
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease
from anime_mpv.providers.anilist import AniListError
from anime_mpv.web_app import WebAppApi


def test_managed_planning_folder_restores_movie_identity(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "pudge"
    movie_dir = root / "Boku no Hero Academia THE MOVIE: Futari no Hero"
    movie_dir.mkdir(parents=True)
    video = movie_dir / "Boku.no.Hero.Academia.Two.Heroes.1080p.BluRay.Opus5.1.x265-FLE.mkv"
    video.write_bytes(b"video")
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(
        LibraryAnime(
            media_id=100723,
            title="Boku no Hero Academia THE MOVIE: Futari no Hero",
            titles=["My Hero Academia: Two Heroes"],
            format="MOVIE",
        )
    )
    db.upsert_anime(
        LibraryAnime(
            media_id=126659,
            title="Boku no Hero Academia THE MOVIE: World Heroes' Mission",
            format="MOVIE",
        )
    )
    monkeypatch.setattr(
        "anime_mpv.library.japanese_subtitle_details",
        lambda *_a, **_k: ("none", None, None),
    )

    rows = scan_library(root, db)

    assert len(rows) == 1
    assert rows[0].media_id == 100723
    assert rows[0].title == "Boku no Hero Academia THE MOVIE: Futari no Hero"


def test_add_release_persists_anilist_identity_sidecar_and_pudge_tag(monkeypatch, tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "pudge"
    cfg.qbittorrent.enabled = True
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(media_id=100723, title="Boku no Hero Academia THE MOVIE: Futari no Hero", format="MOVIE")
    )
    captured: dict[str, object] = {}

    class FakeClient:
        def add_release(self, _release, **kwargs):
            captured.update(kwargs)

        def close(self):
            pass

    monkeypatch.setattr(manager, "qbt_client", lambda: FakeClient())
    manager.add_release(
        100723,
        NyaaRelease(
            title="Boku no Hero Academia Two Heroes 1080p BluRay Opus5.1 x265-FLE",
            link="",
            torrent_url="",
            info_hash="abc",
            size_text="15 GiB",
            size_bytes=15 * 1024**3,
            seeders=10,
            leechers=0,
            downloads=10,
            trusted=True,
            remake=False,
        ),
        episode=None,
        batch=True,
    )

    target = cfg.library.root_dir / "Boku no Hero Academia THE MOVIE_ Futari no Hero"
    assert (target / ".anilist.id").read_text(encoding="utf-8") == "100723"
    assert "pudge" in captured["tags"]


def test_qbt_legacy_app_tag_and_category_migrate_to_pudge(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "pudge"
    cfg.qbittorrent.enabled = True
    cfg.qbittorrent.category = "pudge"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(LibraryAnime(media_id=1, title="Example"))
    item = DownloadItem(
        torrent_hash="abc",
        name="Example",
        state="downloading",
        progress=0.5,
        save_path=str(cfg.library.root_dir),
        content_path=str(cfg.library.root_dir / "Example.mkv"),
        media_id=1,
        episode=1,
        raw={"category": "anime-mpv", "_tag_set": ["anime-mpv", "anilist: 1", "episode: 1"]},
    )

    class FakeClient:
        def __init__(self):
            self.metadata = []
            self.categories = []

        def torrents(self, *, category=""):
            assert category == ""
            return [item]

        def ensure_category(self, category, save_path):
            self.categories.append((category, save_path))

        def set_metadata(self, torrent_hash, *, category, tags):
            self.metadata.append((torrent_hash, category, list(tags)))

        def close(self):
            pass

    client = FakeClient()
    monkeypatch.setattr(manager, "qbt_client", lambda: client)

    manager.sync_downloads()

    assert client.metadata
    _hash, category, tags = client.metadata[0]
    assert category == "pudge"
    assert "pudge" in tags
    assert "anime-mpv" not in tags


def test_bitmap_movie_never_appears_ready_when_ocr_is_off(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "pudge"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.matching.ocr_image_subtitles = False
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    api.manager.db.upsert_anime(
        LibraryAnime(media_id=178788, title="Demon Slayer: Kimetsu no Yaiba - Infinity Castle", format="MOVIE")
    )
    video = cfg.library.root_dir / "Demon Slayer" / "movie.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    sup = tmp_path / "cache" / "movie.sup"
    sup.parent.mkdir(parents=True, exist_ok=True)
    sup.write_bytes(b"PG")
    # Simulate a stale legacy DB row that incorrectly said Ready despite bitmap provenance.
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=178788,
            title="Demon Slayer: Kimetsu no Yaiba - Infinity Castle",
            episode=None,
            video_path=video,
            subtitle_path=sup,
            subtitle_origin="bitmap",
            state="ready",
        )
    )

    state = api.get_state()
    group = next(row for row in state["library"] if row["media_id"] == 178788)

    assert group["ready_count"] == 0
    assert group["waiting_count"] == 1
    assert group["episodes"][0]["state"] == "waiting_text_subtitles"
    assert group["episodes"][0]["subtitle_source"] == "image"


def test_catmahjong_stale_match_is_removed_even_when_anilist_is_rate_limited(monkeypatch, tmp_path: Path) -> None:
    watched = tmp_path / "Downloads"
    watched.mkdir()
    video = watched / "catmahjong.mp4"
    video.write_bytes(b"video")
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "pudge"
    cfg.library.root_dir.mkdir()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.download_dirs = [watched]
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(LibraryAnime(media_id=999, title="Mahoutsukai no Yoru", synonyms=["Mahoyo"], format="MOVIE"))
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

    class RateLimitedClient:
        def __init__(self, *_a, **_k):
            pass

        def search(self, _identity):
            raise AniListError("HTTP 429")

        def close(self):
            pass

    monkeypatch.setattr("anime_mpv.manager.AniListClient", RateLimitedClient)
    monkeypatch.setattr(
        "anime_mpv.library.japanese_subtitle_details",
        lambda *_a, **_k: ("none", None, None),
    )

    manager.scan_library()

    assert manager.db.episode_by_path(video) is None


def test_web_state_exposes_pudge_brand_for_sidebar(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "pudge"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)

    state = api.get_state()

    assert state["branding"]["name"] == "pudge"
    assert state["branding"]["slug"] == "pudge"

def test_asset_server_renders_brand_before_state_load(tmp_path: Path) -> None:
    from anime_mpv.config import AppConfig
    from anime_mpv.web_app import WebAppApi, _start_asset_server

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    server, _url = _start_asset_server(api)
    try:
        rendered = (cfg.paths.cache_dir / "web-ui" / "index.html").read_text(encoding="utf-8")
        assert '<span id="appBrandName">pudge</span>' in rendered
        assert '<title id="documentTitle">pudge</title>' in rendered
        assert "__APP_NAME__" not in rendered
    finally:
        server.shutdown()
        server.server_close()
