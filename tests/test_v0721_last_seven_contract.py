"""Release items of v0.7.21 (behaviour; audit 71Б rewrite of the window checks)."""

from __future__ import annotations

import json
from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
import pudge.reading_audio_alignment as ralign
from pudge.reading_audio_alignment import align_light_novel_to_transcript
from tests.i18n_support import en, ru


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
    manager.db.upsert_anime(
        LibraryAnime(media_id=media_id, title="Example Show", status="CURRENT", progress=0, episodes=12)
    )
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


# --- Window -------------------------------------------------------------------------------

# Old settings controls (SubsPlease RSS / preferred, automatic Nyaa, codec and source lists)
# and the custom character-name actions of LN, VN and manga were removed in v0.7.21.
LEGACY_CONTROLS = ["s_subsplease_rss", "s_subsplease_preferred", "s_nyaa_auto", "s_codecs", "s_sources"]
LIBRARY = {
    "light_novel_state": {
        "version": 1,
        "settings": {},
        "books": [
            {
                "id": 1,
                "title": "Spice 1",
                "series_title": "Spice",
                "series_key": "spice",
                "volume": 1,
                "anilist_id": 31,
            },
            {
                "id": 2,
                "title": "Spice 2",
                "series_title": "Spice",
                "series_key": "spice",
                "volume": 2,
                "anilist_id": 31,
            },
            {"id": 9, "title": "Solo", "volume": 1, "anilist_id": 5},
        ],
    },
    "manga_state": {
        "books": [
            {
                "id": 11,
                "title": "Food v1",
                "series_title": "Food",
                "series_key": "food",
                "volume": 1,
                "page_count": 10,
                "anilist_id": 86082,
            },
            {
                "id": 12,
                "title": "Food v2",
                "series_title": "Food",
                "series_key": "food",
                "volume": 2,
                "page_count": 10,
                "anilist_id": 86082,
            },
            {
                "id": 20,
                "title": "Solo v1",
                "series_title": "Solo",
                "series_key": "solo",
                "volume": 1,
                "page_count": 10,
                "anilist_id": 7,
            },
        ],
        "ocr_available": True,
    },
    "visual_novel_state": {"windows": [], "transcript": []},
}


def test_removed_settings_and_character_name_actions_are_gone_everywhere(page) -> None:
    result = page.run(
        r"""
        const menu = async selector => {
          await __fire(selector, 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent'); await __tick(10);
          const items = [...document.querySelectorAll('#contextMenu button')].filter(b => b.checkVisibility())
            .map(b => [b.dataset.lnContextAction || b.dataset.mangaContextAction, b.innerText]);
          await __key('Escape'); return items;
        };
        const out = {};
        setPage('settings'); await __tick(50);
        const tabs = [...document.querySelectorAll('.settings-categories [data-settings-category]')];
        const shown = new Set();
        for (const tab of tabs) {
          tab.click(); await __tick(5);
          for (const control of document.querySelectorAll('#settingsContent [id^="s_"]')) if (control.checkVisibility()) shown.add(control.id);
        }
        out.settings = {legacy: LEGACY.filter(id => document.getElementById(id) || shown.has(id)), tabs: tabs.length, shown: shown.size};
        setPage('lightnovels'); await __tick(100);
        out.ln = [...await menu('#lightNovelsContent [data-ln-book="9"]'), ...await menu('#lightNovelsContent [data-ln-series-key="spice"] .ln-series-head')];
        setPage('manga'); await __tick(100);
        out.manga = [...await menu('#mangaContent [data-manga-book="20"]'), ...await menu('#mangaContent [data-manga-series-key="food"] .ln-series-head')];
        setPage('visualnovels'); await __tick(100);
        out.vn = [...document.querySelectorAll('#visualNovelsContent button')].map(b => [b.id, b.innerText]);
        out.names = t('ui.characterNames');
        return out;
        """.replace("LEGACY", json.dumps(LEGACY_CONTROLS)),
        api=LIBRARY,
    )
    out = result.value
    assert out["settings"]["legacy"] == [] and out["settings"]["tabs"] == 5 and out["settings"]["shown"] > 50
    names = out["names"]
    for where in ("ln", "manga"):
        actions = [action for action, _label in out[where]]
        assert actions and not {"names", "name-cues"} & set(actions), where
        assert names not in [label for _action, label in out[where]], where
    assert out["vn"] and names not in [label for _id, label in out["vn"]]
    assert "vnNames" not in [button_id for button_id, _label in out["vn"]]


def test_torrent_switch_is_the_only_toggle_and_its_hover_says_torrents_are_off_by_default(page) -> None:
    assert en("ui.torrentsAreOffByDefault").startswith("Torrents are off by default")
    assert en("ui.torrentsAreOffByDefault2") == "Torrents are off by default"
    assert ru("ui.torrentsAreOffByDefault2") == "Торренты выключены по умолчанию"
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const button = () => document.getElementById('torrentToggleButton');
            const off = {visible: __visible('#torrentToggleButton'), title: button().title};
            window.fetch = async (url, init) => new Response(JSON.stringify({ok: true, enabled: true, desired_enabled: true, transition: 'on'}), {status: 200});
            await __click('#torrentToggleButton'); await __tick(10);
            const on = {title: button().title};
            const toggles = [];
            for (const name of ['current', 'planned', 'lightnovels', 'manga', 'audiobooks', 'settings']) {
              setPage(name); await __tick(30);
              toggles.push(document.querySelectorAll('[data-action="torrent-toggle"]').length);
            }
            return {off, on, toggles, switches: document.querySelectorAll('#torrentToggleButton').length,
                    texts: {off: PudgeI18n.t('ui.torrentsAreOffByDefault2'), on: PudgeI18n.t('ui.clickToTurnTorrentTraffic')}};
            """,
            lang=lang,
        )
        out = result.value
        assert out["off"] == {"visible": True, "title": out["texts"]["off"]}
        assert out["on"] == {"title": out["texts"]["on"]}
        # One switch in the sidebar; no page renders a second (old Downloads page) toggle.
        assert out["switches"] == 1 and out["toggles"] == [0] * 6


def test_light_novels_and_manga_share_the_header_import_button(page) -> None:
    assert en("uiB.import") == "Import"
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const out = {label: t('uiB.import')};
            for (const name of ['lightnovels', 'manga']) {
              setPage(name); await __tick(100);
              const content = document.getElementById(name === 'lightnovels' ? 'lightNovelsContent' : 'mangaContent');
              out[name] = {header: __visible('#pageImportButton'), text: __text('#pageImportButton'),
                           ownButtons: [...content.querySelectorAll('button')].filter(b => b.checkVisibility() && b.innerText === out.label).length,
                           headings: [...document.querySelectorAll(`#${name} h1, #${name} h2`)].filter(h => h.checkVisibility()).map(h => h.innerText)};
              await __click('#pageImportButton'); await __tick(50);
            }
            return out;
            """,
            lang=lang,
            api={
                **LIBRARY,
                "choose_light_novel_file": {"cancelled": True},
                "choose_manga_folder": {"cancelled": True},
            },
        )
        out = result.value
        for name in ("lightnovels", "manga"):
            # The page header's Import button; no second Import button or "Manga" heading in the page.
            assert out[name] == {"header": True, "text": out["label"], "ownButtons": 0, "headings": []}, name
        assert [call["name"] for call in result.calls if call["name"].startswith("choose_")] == [
            "choose_light_novel_file",
            "choose_manga_folder",
        ]
