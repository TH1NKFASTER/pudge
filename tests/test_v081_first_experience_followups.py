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


# Window follow-ups (behaviour; audit 71Б rewrite of the former source-text "contracts").

ANIME = {
    "media_id": 1,
    "title": "Show",
    "episodes": 12,
    "next_episode": 3,
    "local": {"state": "waiting_subtitles", "episode": 3},
}
DOWNLOAD = {
    "hash": "abc",
    "name": "Show - 03.mkv",
    "media_id": 1,
    "episode": 3,
    "state": "downloading",
    "progress": 0.5,
    "seeders": 8,
    "peers": 3,
    "listed_seeders": 22,
    "listed_peers": 4,
    "backend_id": "aria2",
    "total_bytes": 1000,
    "downloaded_bytes": 500,
}


def test_settings_anilist_flow_opens_the_key_page_copies_and_goes_back(page) -> None:
    result = page.run(
        """
        const flow = () => ({tokenEnabled: !$('s_anilist_token').disabled, summary: document.querySelector('#s_anilist_flow [data-anilist-client-summary]')?.innerText});
        await __input('#s_anilist_enabled', true);
        await __input('#s_anilist_client', '12345');
        await __click('#s_anilist_flow [data-anilist-continue]');
        const continued = flow();
        await __click('#anilistAuthorize');
        await __click('#s_anilist_flow [data-anilist-copy]');
        const toast = __text('#toast');
        await __click('#s_anilist_flow [data-anilist-back]');
        return {continued, back: flow(), toast, copiedText: PudgeI18n.t('ui.redirectUrlCopied'), copied: __clipboard,
                nyaaCategory: Boolean(document.getElementById('s_ln_nyaa_category'))};
        """,
    )
    out = result.value
    assert out["continued"] == {"tokenEnabled": True, "summary": "12345"}
    assert out["back"]["tokenEnabled"] is False
    assert result.calls_of("open_url") == [
        ["https://anilist.co/api/v2/oauth/authorize?client_id=12345&response_type=token"]
    ]
    assert out["copied"] == ["https://anilist.co/api/v2/oauth/pin"] and out["toast"].endswith(
        out["copiedText"]
    )
    assert out["nyaaCategory"] is False  # the LN Nyaa category setting was removed


def test_downloads_open_from_the_card_menu_and_from_debug(page) -> None:
    result = page.run(
        """
        const openMenu = async () => { document.querySelector('#current [data-media-id="1"] [data-action="card-menu"]').click(); await __flush(); };
        await openMenu();
        const menuItem = document.querySelector('#contextMenu [data-context-action="downloads"]')?.innerText;
        await __click('#contextMenu [data-context-action="downloads"]');
        await __tick(50);
        const center = {title: __text('#modalTitle'), body: __text('#modalBody')};
        await __click('#modalBody [data-action="torrent-pause"][data-hash="abc"]');
        await __tick(10);
        await __click('#modalClose');
        await openMenu();
        await __click('#contextMenu [data-context-action="debug"]');
        await __tick(10);
        const debugOpen = $('pudgeDebugOverlay').classList.contains('open');
        const modalAfterClose = __visible('#modalBackdrop');
        $('modalTitle').textContent = '';
        await __click('#pudgeDebugOverlay [data-debug-action="downloads"]');
        await __tick(50);
        return {menuItem, expected: t('action.openDownloads'), downloads: PudgeI18n.t('ui.downloads'), center, debugOpen, modalAfterClose,
                debugClosed: !$('pudgeDebugOverlay').classList.contains('open'), reopened: __visible('#modalBackdrop') && __text('#modalTitle')};
        """,
        state={
            "current": [ANIME],
            "home": {"waiting": [ANIME]},
            "downloads": [{"media_id": 1, "hash": "abc", "state": "downloading"}],
            "settings": {"torrents_enabled": True},
        },
        api={
            "torrent_downloads": {"backend": "aria2", "downloads": [DOWNLOAD]},
            "torrent_download_action": {"backend": "aria2", "downloads": [{**DOWNLOAD, "state": "paused"}]},
        },
    )
    out = result.value
    assert out["menuItem"] == out["expected"]
    assert out["center"]["title"] == f"{out['downloads']} · Show"
    assert "8 / 3 · Nyaa 22 / 4" in out["center"]["body"]  # live peers, then what Nyaa listed
    assert result.calls_of("torrent_download_action") == [["abc", "pause", False, "aria2"]]
    assert out["modalAfterClose"] is False
    assert out["debugOpen"] is True and out["debugClosed"] is True
    assert out["reopened"] == f"{out['downloads']} · Show"
    assert {args[0] for args in result.calls_of("torrent_downloads")} == {1}  # only this anime's downloads


# A light novel opened in the reader.
LN_API = {
    "light_novel_state": {
        "version": 1,
        "books": [{"id": 7, "title": "Novel", "volume": 1, "character_count": 100}],
        "settings": {"auto_bookmarks": True, "word_mark_style": "underline"},
    },
    "light_novel_open": {
        "id": 7,
        "title": "Novel",
        "chapters": [{"chapter_index": 0, "title": "Ch 1"}],
        "current_chapter": 0,
        "current_offset": 0,
        "paired_audio": None,
    },
    "light_novel_chapter": {"chapter_index": 0, "title": "Ch 1", "paragraphs": ["本文です。", "続きです。"]},
    "content_review_begin": {"granted": True, "reason": "granted"},
    "light_novel_bookmark": {"current_chapter": 0, "current_offset": 0.5, "bookmark_source": "auto"},
}
OPEN_READER = """
const opening = openLightNovel(7);
await __tick(200);
await opening;
const bookmarks = () => __apiCalls.filter(call => call.name === 'light_novel_bookmark').map(call => call.args);
const saves = () => __apiCalls.filter(call => call.name === 'light_novel_save_settings').map(call => call.args[0]);
"""


def test_ln_reader_bookmarks_automatically_after_20_quiet_seconds(page) -> None:
    result = page.run(
        OPEN_READER
        + """
        const out = {open: $('lnReaderShell').classList.contains('open'), text: __text('#lnReader')};
        await __fire('#lnReaderScroll', 'scroll');
        await __tick(15000);
        await __fire('#lnReaderScroll', 'scroll');              // still reading: the bookmark waits
        await __tick(15000);
        out.afterScrolling = bookmarks();
        await __tick(5100);
        out.afterQuiet = bookmarks();
        out.autoNyaa = Boolean(document.getElementById('lnAutoNyaa'));
        return out;
        """,
        api=LN_API,
    )
    out = result.value
    assert out["open"] is True and "本文です。" in out["text"]
    assert out["afterScrolling"] == []
    assert out["afterQuiet"] == [[7, 0, 0, "auto"]]
    assert out["autoNyaa"] is False  # the LN "auto Nyaa" switch was removed

    off = dict(
        LN_API, light_novel_state={**LN_API["light_novel_state"], "settings": {"auto_bookmarks": False}}
    )
    quiet = page.run(
        OPEN_READER + "await __fire('#lnReaderScroll', 'scroll'); await __tick(60000); return bookmarks();",
        api=off,
    )
    assert quiet.value == []


def test_ln_reader_toggles_save_only_their_own_setting(page) -> None:
    result = page.run(
        OPEN_READER
        + """
        const out = {marks: [$('lnWordMarksToggle').innerText]};
        await __click('#lnWordMarksToggle');
        await __tick(300);
        out.marks.push($('lnWordMarksToggle').innerText);
        out.afterMarks = saves();
        await __click('#lnUnknownFuriganaToggle');
        await __tick(300);
        out.afterFurigana = saves().slice(out.afterMarks.length);
        return out;
        """,
        api=LN_API,
    )
    out = result.value
    assert out["marks"][0] != out["marks"][1]
    assert out["afterMarks"] == [{"word_mark_style": "color"}]
    assert out["afterFurigana"] == [{"show_furigana": False}]


def test_ln_word_colours_typed_as_css_select_the_matching_preset(page) -> None:
    result = page.run(
        OPEN_READER
        + """
        const preset = LN_WORD_COLOR_PRESETS.jpdb;
        const names = LN_WORD_COLOR_STATES.map(([name]) => name);
        const css = colors => ':root {\\n' + names.map((name, i) => `  --pudge-study-${name}: ${colors[i]};`).join('\\n') + '\\n}';
        const out = {before: $('lnrWordTheme').value};
        await __input('#lnrCustomCss', css(preset));
        await __tick(300);
        out.matching = $('lnrWordTheme').value;
        await __input('#lnrCustomCss', css([...preset.slice(0, 8), '#123456']));
        await __tick(300);
        out.changed = $('lnrWordTheme').value;
        out.saved = saves().map(patch => patch.word_color_theme);
        return out;
        """,
        api=LN_API,
    )
    out = result.value
    assert out["before"] != "jpdb"
    assert out["matching"] == "jpdb" and out["changed"] == "custom"
    assert out["saved"] == ["jpdb", "custom"]


def test_ln_card_menu_resets_the_reading_position(page) -> None:
    books = [
        {
            "id": 1,
            "title": "Started",
            "series_key": "started",
            "volume": 1,
            "character_count": 1000,
            "current_chapter": 2,
            "current_offset": 0.4,
        },
        {"id": 3, "title": "Fresh", "series_key": "fresh", "volume": 1, "character_count": 1000},
    ]
    reset_state = {
        "version": 2,
        "settings": {},
        "books": [{**books[0], "current_chapter": 0, "current_offset": 0}, books[1]],
    }
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('lightnovels');
            await __tick(50);
            const menuFor = async id => {
              await __fire(`#lightNovelsContent [data-ln-book="${id}"]`, 'contextmenu', {clientX: 10, clientY: 10}, 'MouseEvent');
              return [...document.querySelectorAll('#contextMenu [data-ln-context-action]')].map(item => item.dataset.lnContextAction);
            };
            const fresh = await menuFor(3), started = await menuFor(1);
            const label = document.querySelector('#contextMenu [data-ln-context-action="reset-position"]').innerText;
            await __click('#contextMenu [data-ln-context-action="reset-position"]');
            const question = __text('#pudgeConfirmMessage');
            await __click('[data-pudge-confirm="ok"]');
            await __tick(50);
            return {fresh, started, label, question, toast: __text('#toast'), texts: {
              label: PudgeI18n.t('ui.resetReadingProgress'), question: PudgeI18n.t('ui.resetThisVolumeBookmarkAnd'), done: PudgeI18n.t('ui.readingProgressReset')}};
            """,
            lang=lang,
            api={
                "light_novel_state": {"version": 1, "settings": {}, "books": books},
                "light_novel_reset_position": {"state": reset_state},
            },
        )
        out = result.value
        assert "reset-position" not in out["fresh"] and "reset-position" in out["started"]
        assert out["label"] == out["texts"]["label"] and out["question"] == out["texts"]["question"]
        assert result.calls_of("light_novel_reset_position") == [[1]]
        assert out["toast"].endswith(out["texts"]["done"])
