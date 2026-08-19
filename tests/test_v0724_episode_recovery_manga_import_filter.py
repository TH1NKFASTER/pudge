from __future__ import annotations

import io
import zipfile
from pathlib import Path

from PIL import Image

from pudge.config import AppConfig
from pudge.database import Database
from pudge.debug_snapshot import DebugSnapshotService
from pudge.light_novels import LightNovelService
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.manga import MangaService
from pudge.providers.aria2 import Aria2Client

ROOT = Path(__file__).parents[1]


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    return AnimeManager(cfg, log=lambda _message: None)


def test_debug_defaults_to_next_episode_instead_of_previous_ready_row(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=200637,
            title="The 100 Girlfriends Season 3",
            status="CURRENT",
            progress=6,
            episodes=12,
            format="TV",
        )
    )
    video = tmp_path / "S03E06.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title="The 100 Girlfriends Season 3",
            episode=6,
            media_episode=6,
            release_episode=6,
            video_path=video,
            state="ready",
        )
    )
    monkeypatch.setattr(manager, "diagnose_episode", lambda _media, episode: {"episode": episode})

    snapshot = DebugSnapshotService(
        manager,
        cache_dir=tmp_path / "cache",
        runtime_log_path=tmp_path / "runtime.log",
    ).snapshot(200637)

    assert snapshot["selected_episode"] == 7
    assert snapshot["selected_local_episode"] is None
    assert snapshot["summary"]["diagnosis"]["episode"] == 7
    assert [row["episode"] for row in snapshot["available_episodes"]] == [6, 7]
    assert snapshot["available_episodes"][-1]["state"] == "missing"


def test_debug_episode_selector_hides_single_episode_media() -> None:
    js = (ROOT / "pudge" / "web" / "debug.js").read_text(encoding="utf-8")
    assert "totalEpisodes===1" in js
    assert "format==='MOVIE'" in js


def test_aria2_orphaned_metadata_recovers_completed_episode_rows(tmp_path: Path, monkeypatch) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    seihantai = downloads / "[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p) [C9121DE6].mkv"
    hyakkano = downloads / "[SubsPlease] Hyakkano - 31 (1080p) [31BF37F3].mkv"
    seihantai.write_bytes(b"s" * 20)
    hyakkano.write_bytes(b"h" * 20)

    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    client._save_metadata(
        {
            "gid-seihantai": {
                "info_hash": "f3a099e5c9a9519b1b406a23202f94e1e9d07157",
                "title": seihantai.name,
                "save_path": str(downloads),
                "media_id": 210031,
                "episode": 7,
                "anime_title": "Seihantai na Kimi to Boku 2nd Season",
                "completed_on": 10,
            },
            "gid-hyakkano": {
                "info_hash": "a08b000000000000000000000000000000000000",
                "title": hyakkano.name,
                "save_path": str(downloads),
                "media_id": 200637,
                "episode": 7,
                "anime_title": "Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin no Kanojo 3rd Season",
                "completed_on": 11,
            },
        }
    )
    monkeypatch.setattr(client, "_all_statuses", lambda: [])
    try:
        rows = client.torrents()
    finally:
        client.close()

    by_media = {row.media_id: row for row in rows}
    assert by_media[210031].media_episode == 7
    assert by_media[210031].release_episode == 19
    assert Path(by_media[210031].content_path) == seihantai.resolve()
    assert by_media[200637].media_episode == 7
    assert by_media[200637].release_episode == 31
    assert Path(by_media[200637].content_path) == hyakkano.resolve()


def test_orphaned_aria2_item_recreates_seihantai_episode_row(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / "Seihantai na Kimi to Boku 2nd Season"
    folder.mkdir(parents=True)
    video = folder / "[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p) [C9121DE6].mkv"
    video.write_bytes(b"video")
    manager.db.upsert_anime(
        LibraryAnime(media_id=210031, title="Seihantai na Kimi to Boku 2nd Season", episodes=13, status="CURRENT")
    )
    cache = manager.config.paths.cache_dir / "anilist-release-numbering" / "210031.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('{"offset":12,"resolver_version":2,"prequel_titles":[]}', encoding="utf-8")

    aria = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    aria._save_metadata({
        "forgotten": {
            "info_hash": "f3a099e5c9a9519b1b406a23202f94e1e9d07157",
            "title": video.name,
            "save_path": str(folder),
            "media_id": 210031,
            "episode": 7,
            "anime_title": "Seihantai na Kimi to Boku 2nd Season",
            "completed_on": 10,
        }
    })
    monkeypatch.setattr(aria, "_all_statuses", lambda: [])
    orphan = aria.torrents()[0]
    aria.close()
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_a, **_k: ("none", None))
    monkeypatch.setattr("pudge.manager.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))

    count = manager._register_completed_download(orphan)
    row = manager.db.episode_by_path(video.resolve())

    assert count == 1
    assert row is not None
    assert row.episode == 7
    assert row.media_episode == 7
    assert row.release_episode == 19
    assert row.state == "waiting_subtitles"


def _write_cbz(path: Path) -> None:
    image = Image.new("RGB", (2, 2), "white")
    data = io.BytesIO()
    image.save(data, format="PNG")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", data.getvalue())


def test_manga_remove_series_keeps_source_files(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    first = tmp_path / "Example Manga Vol 1.cbz"
    second = tmp_path / "Example Manga Vol 2.cbz"
    _write_cbz(first)
    _write_cbz(second)
    first_book = service.import_file(first)
    service.import_file(second)

    removed = service.remove_series(first_book["id"])

    assert removed == 2
    assert service.state()["books"] == []
    assert first.is_file() and second.is_file()
    js = (ROOT / "pudge" / "web" / "manga_reader_v2.js").read_text(encoding="utf-8")
    assert 'data-manga-context-action="remove-series"' in js
    assert "manga_remove_series" in js


def test_light_novel_auto_import_language_gate_accepts_japanese_and_rejects_junk(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    service = LightNovelService(cfg)

    japanese = tmp_path / "rezero.txt"
    japanese.write_text(("これは日本語のライトノベル本文です。彼は街を歩き、彼女と話しました。" * 30), encoding="utf-8")
    technical = tmp_path / "README.txt"
    technical.write_text(("Python package build validation dependency links requirements source code license. " * 50), encoding="utf-8")
    tiny = tmp_path / "BUILD_VALIDATION.txt"
    tiny.write_text("build validation ok", encoding="utf-8")

    jp_profile = service.source_language_profile(japanese)
    en_profile = service.source_language_profile(technical)
    tiny_profile = service.source_language_profile(tiny)

    assert jp_profile["accepted"] is True
    assert jp_profile["japanese_ratio"] > 0.5
    assert en_profile["accepted"] is False
    assert tiny_profile["accepted"] is False


def test_light_novel_managed_folder_scan_skips_non_japanese_technical_files(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    service = LightNovelService(cfg)
    (service.root / "README.txt").write_text("technical dependency metadata " * 80, encoding="utf-8")
    (service.root / "Japanese.txt").write_text("これは日本語の本文です。物語が続きます。" * 40, encoding="utf-8")

    assert service.scan_downloaded() == 1
    titles = [book["title"] for book in service.books()]
    assert any("Japanese" in title for title in titles)
    assert not any("README" in title for title in titles)
