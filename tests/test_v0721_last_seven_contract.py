from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
import pudge.reading_audio_alignment as ralign
from pudge.reading_audio_alignment import align_light_novel_to_transcript

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
VN_JS = ROOT / "pudge" / "web" / "visual_novels.js"
MANGA_JS = ROOT / "pudge" / "web" / "manga_reader_v2.js"


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_download_current = True
    cfg.nyaa.torrents_enabled = False
    cfg.qbittorrent.enabled = True
    return AnimeManager(cfg, log=lambda _message: None)


def _release(title: str, score: float, *, source: str = "rss", info_hash: str = "1" * 40) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link=f"https://example/{info_hash}",
        torrent_url=f"https://example/{info_hash}.torrent",
        info_hash=info_hash,
        size_text="1.2 GiB",
        size_bytes=1200 * 1024 * 1024,
        seeders=0 if source != "nyaa" else 15,
        leechers=0,
        downloads=0,
        trusted=True,
        remake=False,
        category_id="subsplease-rss" if source == "rss" else ("shana-rss" if source == "shana" else "1_2"),
        group="SubsPlease",
        score=score,
        reasons=["exact-title-phrase", "ep=2"],
    )


def test_alignment_interpolates_missing_middle_chapter(monkeypatch) -> None:
    chapters = [
        {"chapter_index": 1, "title": "One", "text": ("あいうえおかきくけこ" * 16) + "。"},
        {"chapter_index": 2, "title": "Two", "text": ("さしすせそたちつてと" * 12) + "。"},
        {"chapter_index": 3, "title": "Three", "text": ("なにぬねのはひふへほ" * 16) + "。"},
    ]
    segments = [
        {"text": "あいうえおかきくけこ" * 16, "start": 0.0, "end": 6.0},
        {"text": "なにぬねのはひふへほ" * 16, "start": 12.0, "end": 18.0},
    ]
    len1 = len(ralign.normalize_reading_text(chapters[0]["text"]))
    len2 = len(ralign.normalize_reading_text(chapters[1]["text"]))
    gap = len1 + len2

    def fake_best_dense_anchor_chain(_novel: str, _transcript: str):
        anchors = [(index, index) for index in range(0, min(80, len1), 10)]
        anchors.extend((gap + index, len1 + index) for index in range(0, min(80, len1), 10))
        return 8, anchors

    monkeypatch.setattr(ralign, "_best_dense_anchor_chain", fake_best_dense_anchor_chain)

    alignment = align_light_novel_to_transcript(chapters, segments, duration=18.0, model="unit")
    found = {int(row["chapter_index"]): row for row in alignment["chapters"]}
    assert {1, 2, 3}.issubset(found)
    assert found[1]["end"] <= found[2]["start"] <= found[2]["end"] <= found[3]["start"] + 0.5


def test_search_order_is_subsplease_then_nyaa_then_shana(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    media_id = 42
    manager.db.upsert_anime(LibraryAnime(media_id=media_id, title="Example Show", status="CURRENT", progress=0, episodes=12))
    calls: list[str] = []

    def rss(*_args, **_kwargs):
        calls.append("subsplease")
        return []

    def nyaa(*_args, **_kwargs):
        calls.append("nyaa")
        return []

    def shana(*_args, **_kwargs):
        calls.append("shana")
        return [_release("[SubsPlease] Example Show - 02 (1080p)", 99.0, source="shana", info_hash="2" * 40)]

    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", rss)
    monkeypatch.setattr("pudge.manager.search_ranked", nyaa)
    monkeypatch.setattr("pudge.manager.search_shana_ranked", shana)
    monkeypatch.setattr(manager, "_release_is_allowed_for_auto", lambda _item: False)

    releases = manager.search_releases(media_id, episode=2, batch=False)
    assert calls == ["subsplease", "nyaa", "shana"]
    assert releases and releases[0].category_id == "shana-rss"


def test_torrent_switch_defaults_off_and_preserves_config(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.nyaa.torrents_enabled = False
    write_config(cfg, cfg.config_path)
    loaded = load_config(cfg.config_path)
    assert loaded.nyaa.torrents_enabled is False
    assert "torrents_enabled = false" in cfg.config_path.read_text(encoding="utf-8")


def test_manager_requires_toggle_even_when_backend_is_configured(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    assert manager.downloads_configured() is True
    assert manager.downloads_enabled() is False
    manager.config.nyaa.torrents_enabled = True
    assert manager.downloads_enabled() is True


def test_settings_remove_legacy_toggles_and_custom_name_actions() -> None:
    html = HTML.read_text(encoding="utf-8")
    vn = VN_JS.read_text(encoding="utf-8")
    manga = MANGA_JS.read_text(encoding="utf-8")
    assert "s_subsplease_rss" not in html
    assert "s_subsplease_preferred" not in html
    assert "s_nyaa_auto" not in html
    assert "s_codecs" not in html
    assert "s_sources" not in html
    assert 'data-ln-context-action="name-cues"' not in html
    assert "vnNames" not in vn
    assert 'data-manga-context-action="names"' not in manga


def test_ui_exposes_torrent_toggle_and_warning_copy() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert 'id="torrentToggleButton"' in html
    assert 'id="torrentToggleButton"' in html
    assert 'data-action="torrent-toggle"' not in html
    assert 'Torrents are off by default' in html


def test_feed_history_persists_release_rows(tmp_path: Path) -> None:
    from pudge.providers.nyaa import _load_release_history, _merge_release_history

    path = tmp_path / "release-feeds" / "shana.json"
    release = _release(
        "[SubsPlease] Example Show - 02 (1080p)",
        0.0,
        source="shana",
        info_hash="a" * 40,
    )
    merged = _merge_release_history(path, [release], source="shana")
    assert len(merged) == 1
    assert path.is_file()
    loaded = _load_release_history(path)
    assert len(loaded) == 1
    assert loaded[0].title == release.title
    assert loaded[0].link == release.link
    assert loaded[0].info_hash == release.info_hash


def test_torrents_off_still_discovers_and_queues_waiting(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=77,
            title="Example Show",
            status="CURRENT",
            progress=0,
            episodes=1,
        )
    )
    release = _release(
        "[SubsPlease] Example Show - 01 (1080p)",
        150.0,
        info_hash="b" * 40,
    )
    monkeypatch.setattr(manager, "search_releases", lambda *_args, **_kwargs: [release])
    monkeypatch.setattr(manager, "_release_is_allowed_for_auto", lambda _item: True)
    monkeypatch.setattr(
        manager,
        "add_release",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must stay queued while off")),
    )

    assert manager.auto_search_current() == 1
    intent = manager.download_intents.get(77, 1, False)
    assert intent is not None
    assert intent["state"] == "waiting"
    assert manager.download_intents.waiting_count() == 1


def test_first_run_config_keeps_torrent_traffic_off(tmp_path: Path) -> None:
    loaded = load_config(tmp_path / "missing-config.toml")
    assert loaded.nyaa.torrents_enabled is False


def test_ln_and_manga_share_header_import_button_without_duplicate_manga_heading() -> None:
    html = HTML.read_text(encoding="utf-8")
    manga = MANGA_JS.read_text(encoding="utf-8")
    assert '<button id="pageImportButton" class="primary" hidden>Import</button>' in html
    assert '<button id="lnImport" hidden>Import</button>' in html
    assert 'id="mangaImportV2" hidden' in manga
    assert "<h2>${ru() ? 'Манга' : 'Manga'}</h2>" not in manga
    assert "if(ui.page==='lightnovels')$('lnImport')?.click();else if(ui.page==='manga')$('mangaImportV2')?.click();" in html
