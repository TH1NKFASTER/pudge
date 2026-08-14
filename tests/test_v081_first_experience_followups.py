from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.manga import MangaService
from pudge.web_app import WebAppApi


ROOT = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    return config


def test_light_novel_bookmark_is_explicit_and_resettable(tmp_path: Path) -> None:
    source = tmp_path / "Novel.txt"
    source.write_text("第一章\n\n本文です。\n\n第二章\n\n続きです。", encoding="utf-8")
    service = LightNovelService(_config(tmp_path))
    book = service.import_file(source)

    saved = service.save_bookmark(book["id"], 0, 0.42, source="manual")
    service.chapter_fast(book["id"], 0)
    reopened = service.open_book(book["id"])

    assert saved["source"] == "manual"
    assert reopened["current_offset"] == 0.42
    assert reopened["bookmark_source"] == "manual"
    reset = service.reset_position(book["id"])
    assert reset["current_chapter"] == 0
    assert reset["current_offset"] == 0
    assert reset["bookmark_source"] is None


def test_light_novel_auto_bookmark_setting_round_trips(tmp_path: Path) -> None:
    service = LightNovelService(_config(tmp_path))
    assert service.settings_payload()["auto_bookmarks"] is True
    assert service.save_settings({"auto_bookmarks": False})["auto_bookmarks"] is False
    assert LightNovelService(_config(tmp_path)).settings_payload()["auto_bookmarks"] is False


def test_download_payload_exposes_qbit_style_metrics() -> None:
    api = object.__new__(WebAppApi)
    api.manager = SimpleNamespace(torrent_backend_name=lambda: "aria2")
    item = SimpleNamespace(
        torrent_hash="abc123",
        name="Example Episode",
        state="active",
        progress=0.5,
        media_id=77,
        episode=3,
        is_batch=False,
        save_path="/tmp/example",
        added_on=10,
        completed_on=0,
        raw={
            "backend": "aria2",
            "total_size": 1000,
            "downloaded": 500,
            "download_speed": 100,
            "upload_speed": 20,
            "num_seeders": 8,
            "num_connections": 11,
            "listed_seeders": 22,
            "listed_leechers": 4,
        },
    )
    anime = SimpleNamespace(title="Example")

    payload = api._download_payload(item, {77: anime})

    assert payload["hash"] == "abc123"
    assert payload["total_bytes"] == 1000
    assert payload["download_speed"] == 100
    assert payload["upload_speed"] == 20
    assert payload["seeders"] == 8
    assert payload["peers"] == 3
    assert payload["listed_seeders"] == 22
    assert payload["listed_peers"] == 4
    assert payload["eta_seconds"] == 5


def test_manga_anilist_cover_is_served_from_local_cache(tmp_path: Path) -> None:
    page = io.BytesIO()
    Image.new("RGB", (40, 60), "navy").save(page, format="JPEG")
    archive = tmp_path / "Manga v01.cbz"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("001.jpg", page.getvalue())
    service = MangaService(
        Database(tmp_path / "library.sqlite3"),
        cache_dir=tmp_path / "cache",
        python="/bin/false",
    )
    book = service.import_file(archive)
    cover_url = "https://cdn.example.test/cover.jpg"
    target = service._remote_cover_target(cover_url)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(page.getvalue())

    bound = service.bind_anilist(book["id"], 123, cover_url=cover_url)

    assert bound["cover_source"] == "anilist_cache"
    assert bound["cover_url"].startswith("data:image/jpeg;base64,")
    assert bound["remote_cover_url"] == cover_url


def test_followup_frontend_contracts() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    debug = (ROOT / "pudge/web/debug.js").read_text(encoding="utf-8")

    assert "function anilistCredentialFlow" in html
    assert "data-anilist-copy" in html
    assert "data-anilist-back" in html
    assert 'id="s_ln_nyaa_category"' not in html
    assert "function openDownloadCenter" in html
    assert "torrent_download_action" in html
    assert 'data-context-action="downloads"' in html
    assert 'data-debug-action="downloads"' in debug
    assert "function scheduleLnAutoBookmark" in html
    assert "},20000)" in html
    assert 'data-ln-context-action="reset-position"' in html
    assert "Object.keys(LN_WORD_COLOR_PRESETS).find(matches)" in html
    assert "applyLnReaderSettingsPatch({word_mark_style:ui.lnWordMarkStyle},true)" in html
    assert "applyLnReaderAppearanceFromControls(true);return;}if(button.id==='lnUnknownFuriganaToggle')" not in html
    assert 'id="lnAutoNyaa"' not in html
    assert "Nyaa ${listedSeeders} / ${listedPeers}" in html
