from __future__ import annotations

import io
import zipfile
from pathlib import Path

from PIL import Image

from pudge.config import AppConfig, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.manga import MangaService
from pudge.web_app import WebAppApi

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
MANGA_JS = ROOT / "pudge" / "web" / "manga_reader_v2.js"
MANGA_CSS = ROOT / "pudge" / "web" / "manga_reader_v2.css"
WEB_APP = ROOT / "pudge" / "web_app.py"
MEDIA_JS = ROOT / "pudge" / "web" / "media.js"


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    return AnimeManager(cfg, log=lambda _message: None)


def test_diagnose_materializes_seihantai_episode_7_from_persisted_complete_download(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=210031,
            title="Seihantai na Kimi to Boku 2nd Season",
            status="CURRENT",
            progress=6,
            episodes=13,
            media_status="RELEASING",
        )
    )
    old = tmp_path / "episode6.mkv"
    old.write_bytes(b"old")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=210031,
            title="Seihantai na Kimi to Boku 2nd Season",
            episode=6,
            media_episode=6,
            release_episode=18,
            video_path=old,
            state="ready",
        )
    )
    folder = manager.config.library.root_dir / "Seihantai na Kimi to Boku 2nd Season"
    folder.mkdir(parents=True)
    video = folder / "[SubsPlease] Seihantai na Kimi to Boku - 19 (1080p) [C9121DE6].mkv"
    video.write_bytes(b"video")
    item = DownloadItem(
        torrent_hash="f3a099e5c9a9519b1b406a23202f94e1e9d07157",
        name=video.name,
        state="complete",
        progress=1.0,
        save_path=str(folder),
        content_path=str(video),
        media_id=210031,
        episode=7,
        media_episode=7,
        release_episode=19,
        completed_on=123,
        raw={"backend": "aria2"},
    )
    manager.db.upsert_download(item)
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *_a, **_k: ("none", None))
    monkeypatch.setattr("pudge.manager.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))

    diagnosis = manager.diagnose_episode(210031, 7)
    row = manager.db.episode_by_path(video.resolve())

    assert row is not None
    assert row.episode == 7 and row.media_episode == 7 and row.release_episode == 19
    assert diagnosis["checks"][2]["detail"] == str(video.resolve())
    assert diagnosis["checks"][2]["ok"] is True
    assert diagnosis["checks"][4]["detail"] != "Missing job"


def test_home_does_not_use_completed_episode_6_download_as_episode_7_state(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    anime = LibraryAnime(
        media_id=200637,
        title="100 Girlfriends S3",
        status="CURRENT",
        progress=6,
        episodes=12,
        media_status="RELEASING",
        next_airing_episode=8,
        next_airing_at=2_000_000_000,
    )
    api.manager.db.upsert_anime(anime)
    video = tmp_path / "S03E06.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=200637,
            title=anime.title,
            episode=6,
            media_episode=6,
            release_episode=6,
            video_path=video,
            state="ready",
        )
    )
    api.manager.db.upsert_download(
        DownloadItem(
            torrent_hash="9" * 40,
            name=video.name,
            state="complete",
            progress=1.0,
            save_path=str(tmp_path),
            content_path=str(video),
            media_id=200637,
            episode=6,
            media_episode=6,
            release_episode=6,
            raw={},
        )
    )

    sections = api._home_sections([anime], {anime.media_id: anime})
    card = next(row for row in sections["waiting"] if row["media_id"] == 200637)

    assert card["next_episode"] == 7
    assert "download" not in card
    assert card["presentation"]["status"] != "waiting_preparation"
    api.close()


def _write_cbz(path: Path) -> None:
    image = Image.new("RGB", (2, 2), "white")
    data = io.BytesIO()
    image.save(data, format="PNG")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", data.getvalue())


def test_manga_remove_books_is_exact_and_keeps_user_archives(tmp_path: Path) -> None:
    service = MangaService(_manager(tmp_path).db, cache_dir=tmp_path / "cache")
    first = tmp_path / "Series Vol 1.cbz"
    second = tmp_path / "Series Vol 2.cbz"
    _write_cbz(first)
    _write_cbz(second)
    a = service.import_file(first)
    b = service.import_file(second)

    assert service.remove_books([a["id"]]) == 1
    remaining = {int(row["id"]) for row in service.state()["books"]}
    assert int(a["id"]) not in remaining and int(b["id"]) in remaining
    assert first.is_file() and second.is_file()


def test_ln_and_manga_have_context_and_cmd_multi_selection_contract() -> None:
    html = HTML.read_text(encoding="utf-8")
    manga = MANGA_JS.read_text(encoding="utf-8")
    css = MANGA_CSS.read_text(encoding="utf-8")
    assert 'id="pageSelectionActions"' in html
    assert 'data-ln-context-action="select"' in html
    assert "e.metaKey" in html and "toggleLnSelection" in html
    assert "Cancel selection" in html and "data-selection-action=\"delete\"" in html
    assert "data-selection-action=\"ocr\"" in html
    assert "selectedBookIds = new Set()" in manga
    assert 'data-manga-context-action="select"' in manga
    assert "event.metaKey" in manga and "toggleSelection" in manga
    assert "manga_remove_books" in manga
    assert "ocrSelected" in manga
    assert ".manga-v2-volume.selected" in css


def test_drop_routing_contract_uses_pywebview_full_paths_and_focus_event() -> None:
    source = WEB_APP.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    media = MEDIA_JS.read_text(encoding="utf-8")
    assert "pywebviewFullPath" in source
    assert "DOMEventHandler" in source
    assert "def import_dropped_paths" in source
    assert "self.manager.import_local_video(path)" in source
    assert "self.light_novels.import_file(path, explicit=True)" in source
    assert "self.manga.import_file(path)" in source
    assert "self.manga.import_images(image_paths)" in source
    assert "self.audiobooks.import_file(path)" in source
    assert "pudge-files-imported" in source and "pudge-files-imported" in html
    assert "scrollIntoView" in html and "drop-focus" in html
    assert 'data-audiobook-id="${Number(book.id)}"' in media
