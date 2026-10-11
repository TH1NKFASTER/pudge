"""v0.7.65: pausing an audiobook does not wait for mpv, reader controls, manga covers and
progress tooltips, Settings categories, library selection, manga text orientation and
Live Text selection, LN parse-ahead and energy diagnostics fixed (behaviour; audit 71Б rewrite).

Listen-together pause reaches mpv before any slow state read (window side):
tests/test_v0720_first_six_contract.py. Clicks hit the word under the pointer (manga):
tests/test_v0727_manga_exact_hitboxes.py."""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from css_support import css_rules
from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig, load_config
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.web_app import WebAppApi
from t71g03_support import MANGA
from t71g05_support import READER, READER_SETTINGS, reader_before
from tests.i18n_support import en, ru


def test_pausing_an_audiobook_sends_the_command_without_waiting_for_mpv(tmp_path: Path) -> None:
    # mpv answers IPC slowly while busy; a pause used to wait for the reply (up to 1 s).
    service = AudiobookService(
        Database(tmp_path / "library.sqlite3"), ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache"
    )
    folder = Path(tempfile.mkdtemp(prefix="pudge-ipc-"))
    path = folder / "mpv.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    received: list[bytes] = []
    done = threading.Event()

    def mpv() -> None:  # reads the command, never replies
        client, _ = server.accept()
        received.append(client.recv(4096))
        done.wait(5)
        client.close()

    thread = threading.Thread(target=mpv, daemon=True)
    thread.start()
    service._players[5] = SimpleNamespace(poll=lambda: None)
    service._ipc_paths[5] = path
    try:
        started = time.monotonic()
        result = service.set_paused(5, True)
        elapsed = time.monotonic() - started
    finally:
        done.set()
        thread.join(5)
        server.close()
        path.unlink(missing_ok=True)
        folder.rmdir()
    assert result == {"ok": True, "book_id": 5, "playing": False, "player_running": True, "paused": True}
    assert elapsed < 0.5
    assert [json.loads(line) for line in received[0].splitlines()] == [
        {"command": ["set_property", "pause", True]}
    ]


def test_reader_controls_keep_their_right_edge_without_audio() -> None:
    actions = css_rules("index.html").declarations(".ln-reader-actions")
    assert (actions["margin-left"], actions["flex"], actions["white-space"]) == ("auto", "0 0 auto", "nowrap")


# Covers: 5 has a local cover and AniList artwork remembered from an earlier session;
# 6 has nothing at all.
COVERS = r"""
const MakeImage = window.Image;
window.Image = function Image() {
  const img = MakeImage();
  img.decode = () => { img.__naturalWidth = 200; img.__naturalHeight = 300; return Promise.resolve(); };
  return img;
};
"""


def test_manga_cards_prefer_the_local_cover_and_otherwise_show_the_cover_placeholder(page) -> None:
    books = [
        {
            "id": 5,
            "title": "Local",
            "series_title": "Local",
            "page_count": 4,
            "anilist_id": 88,
            "local_cover_url": "http://127.0.0.1:9/api/manga/cover/5.jpg",
        },
        {"id": 6, "title": "Bare", "series_title": "Bare", "page_count": 4},
    ]
    result = page.run(
        r"""
        setPage('manga'); await __tick(200);
        const card = id => {
          const img = document.querySelector(`img[data-cover-book="${id}"]`);
          const placeholder = document.querySelector(`[data-cover-placeholder="${id}"]`);
          return {image: __visible(`img[data-cover-book="${id}"]`) ? img.getAttribute('src') : null,
                  placeholder: __visible(`[data-cover-placeholder="${id}"]`) ? placeholder.textContent : null,
                  generic: placeholder.classList.contains('cover-placeholder')};
        };
        return {local: card(5), bare: card(6), label: t('label.cover')};
        """,
        before=COVERS,
        api={"manga_state": {"books": books, "ocr_available": False}},
        localStorage={
            "pudge.manga.anilist.covers.v1": json.dumps({"88": "https://s4.anilist.co/file/cached-88.jpg"})
        },
    )
    out = result.value
    assert out["local"] == {
        "image": "http://127.0.0.1:9/api/manga/cover/5.jpg",
        "placeholder": None,
        "generic": True,
    }
    # The app-wide cover placeholder (logo style), not a "漫" glyph.
    assert out["bare"] == {"image": None, "placeholder": out["label"], "generic": True}
    assert out["label"] != "漫"


def test_job_center_is_gone_from_the_window_and_the_bridge(page) -> None:
    # Jobs are still tracked internally (tests/test_v0730_p10_deletion_matrix.py, test_v093_*).
    result = page.run(
        r"""
        return {nav: [...document.querySelectorAll('[data-page]')].map(n => n.dataset.page),
                jobs: Boolean(document.getElementById('jobs') || document.getElementById('jobCenterContent')),
                global: typeof window.openJobCenter};
        """
    )
    assert (
        "jobs" not in result.value["nav"]
        and result.value["jobs"] is False
        and result.value["global"] == "undefined"
    )
    assert not any(
        result.called(name) for name in ("job_center_state", "job_center_cancel", "job_center_retry")
    )
    for name in ("job_center_state", "job_center_cancel", "job_center_retry"):
        assert not hasattr(WebAppApi, name)


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_percentage_chips_show_the_same_progress_tooltip_as_the_progress_bars(page, lang) -> None:
    novel = {
        "id": 4,
        "title": "Novel",
        "volume": 1,
        "character_count": 12345,
        "read_character_count": 2345,
        "reading_progress": 0.19,
    }
    result = page.run(
        r"""
        const hover = async selector => {
          await __fire(selector, 'mouseover', {}, 'MouseEvent');
          const tip = document.getElementById('titleTooltip');
          const shown = tip.classList.contains('show') ? tip.innerText : null;
          await __fire(selector, 'mouseout', {}, 'MouseEvent');
          return shown;
        };
        const chip = (root, card) => [...document.querySelectorAll(`${root} ${card} .ln-card-meta span`)].find(n => n.textContent.endsWith('%'));
        setPage('lightnovels'); await __tick(100);
        const novelChip = chip('#lightNovelsContent', '[data-ln-book="4"]');
        novelChip.id = 'novelChip';
        const out = {novel: {chip: novelChip.textContent, tip: await hover('#novelChip'), bar: await hover('#lightNovelsContent [data-ln-book="4"] .ln-card-progress')}};
        setPage('manga'); await __tick(200);
        const mangaChip = chip('#mangaContent', '[data-manga-book="3"]');
        mangaChip.id = 'mangaChip';
        out.manga = {chip: mangaChip.textContent, tip: await hover('#mangaChip'), bar: await hover('#mangaContent [data-manga-book="3"] .ln-card-progress')};
        const locale = ui.lang === 'ru' ? 'ru-RU' : 'en-US';
        out.expected = {novel: tn('uiA.ln.characterProgress', 12345, {read: (2345).toLocaleString(locale), total: (12345).toLocaleString(locale)}),
                        manga: PudgeI18n.tn('manga.readOfPages', 6, {read: 3})};
        return out;
        """,
        lang=lang,
        before=MANGA + "window.MANGA.books[0].read_pages = 3;",
        api={"light_novel_state": {"version": 1, "settings": {}, "books": [novel]}},
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    out = result.value
    assert out["novel"] == {"chip": "19%", "tip": out["expected"]["novel"], "bar": out["expected"]["novel"]}
    assert out["manga"]["chip"] == "50%"
    assert out["manga"]["tip"] == out["manga"]["bar"] == out["expected"]["manga"]


# Low-confidence Vision rectangles around Japanese text are columns even when the box is
# wide; Latin text in the same kind of box stays horizontal.
LOW_CONFIDENCE = {
    "text": "これは縦書き",
    "x": 0.4,
    "y": 0.5,
    "width": 0.3,
    "height": 0.1,
    "detector": "vision-rectangles",
    "confidence": 0.3,
}
LATIN = {
    "text": "SFX BOOM",
    "x": 0.1,
    "y": 0.1,
    "width": 0.3,
    "height": 0.1,
    "detector": "vision-rectangles",
    "confidence": 0.3,
}


def test_wide_low_confidence_japanese_box_is_read_as_vertical_text(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        return [...document.querySelectorAll('.manga-v2-text-region')].map(node => ({
          text: node.getAttribute('aria-label'), vertical: node.classList.contains('vertical-text'),
          reason: node.dataset.orientationReason}));
        """,
        before=reader_before({0: [LOW_CONFIDENCE, LATIN]}),
        state=READER_SETTINGS,
    )
    rows = {row["text"]: row for row in result.value}
    assert rows["これは縦書き"] == {
        "text": "これは縦書き",
        "vertical": True,
        "reason": "low-confidence-vision-japanese",
    }
    assert rows["SFX BOOM"]["vertical"] is False


def test_live_text_selection_opens_a_word_card_only_when_it_is_a_short_one_over_the_page(page) -> None:
    # WebKit Live Text selects text in the page image; a click on the selection opens it in
    # the word card. A stale selection elsewhere in the window (long, or wider than the page)
    # must not hijack clicks on the page.
    result = page.run(
        READER
        + r"""
        await openReader();
        const note = document.createElement('p');
        document.body.appendChild(note);
        const clickWithSelection = async (text, rect) => {
          note.textContent = text;
          const range = document.createRange(); range.selectNodeContents(note); range.__rect = rect;
          getSelection().removeAllRanges(); getSelection().addRange(range);
          const img = document.querySelector('.manga-v2-page-frame img');
          const at = {clientX: rect.x + rect.width / 2, clientY: rect.y + rect.height / 2};
          img.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, ...at}));
          img.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, detail: 1, ...at}));
          await __tick(50);
          const term = cardTerm();
          PudgeReadingTools.study.closeIfOpen(); getSelection().removeAllRanges(); await __tick(10);
          return term;
        };
        return {
          short: await clickWithSelection('猫', {x: 50, y: 60, width: 20, height: 20}),
          long: await clickWithSelection('猫'.repeat(49), {x: 50, y: 60, width: 20, height: 20}),
          wide: await clickWithSelection('猫', {x: 10, y: 60, width: 170, height: 20}),
          latin: await clickWithSelection('cat', {x: 50, y: 60, width: 20, height: 20}),
          outside: await clickWithSelection('猫', {x: 400, y: 60, width: 20, height: 20}),
        };
        """,
        before=reader_before({0: []}, {"猫": ["猫"]}),
        state=READER_SETTINGS,
    )
    assert result.value == {"short": "猫", "long": None, "wide": None, "latin": None, "outside": None}


SETTINGS = r"""
setPage('settings'); await __tick(50);
const tabs = () => [...document.querySelectorAll('.settings-categories [data-settings-category]')].map(b => [b.dataset.settingsCategory, b.textContent]);
const tabOf = id => document.getElementById(id)?.closest('.setting-block')?.dataset.settingsCategory ?? null;
const shownOn = async (name, id) => { await __click(`.settings-categories [data-settings-category="${name}"]`); await __tick(10); return __visible('#' + id); };
"""


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_settings_have_five_categories_and_manga_ocr_moves_to_advanced_once_installed(page, lang) -> None:
    assert [
        en(key)
        for key in (
            "settingsUi.essential",
            "nav.library",
            "settingsUi.subtitles",
            "ui.downloads",
            "ui.advanced",
        )
    ] == ["Essential", "Library", "Subtitles", "Downloads", "Advanced"]
    assert [
        ru(key)
        for key in (
            "settingsUi.essential",
            "nav.library",
            "settingsUi.subtitles",
            "ui.downloads",
            "ui.advanced",
        )
    ] == ["Основное", "Библиотека", "Субтитры", "Загрузки", "Дополнительно"]
    result = page.run(
        SETTINGS
        + r"""
        const out = {tabs: tabs(), jiten: tabOf('s_ln_jiten_key'), heading: $('s_ln_jiten_key').closest('.setting-block').querySelector('h3').textContent,
                     notInstalled: {tab: tabOf('mangaOcrStatus'), essential: await shownOn('essential', 'mangaOcrStatus')},
                     removed: ['s_ln_parse_ahead', 's_energy_monitoring', 's_energy_sample'].filter(id => document.getElementById(id)),
                     expected: ['settingsUi.essential', 'nav.library', 'settingsUi.subtitles', 'ui.downloads', 'ui.advanced'].map(key => t(key)),
                     jitenHeading: t('ui.jitenJpdb')};
        // Once installed, the engine is no longer something to set up first.
        await shownOn('essential', 'mangaOcrStatus');
        await __click('#installMangaOcr'); await __tick(50);
        out.ready = {tab: tabOf('mangaOcrStatus'), essential: __visible('#mangaOcrStatus'), advanced: await shownOn('advanced', 'mangaOcrStatus')};
        // A home-screen action opens Settings on the right tab.
        PudgeSettings.focusAction('configure_jimaku'); await __tick(10);
        out.focus = {jimaku: __visible('#s_jimaku'), focused: document.activeElement?.id};
        return out;
        """,
        lang=lang,
        before=(
            "window.OCR_STATE = 'not_installed';"
            "__api.manga_ocr_status = async () => ({state: window.OCR_STATE, backend: 'pudge', running: false});"
            "__api.install_manga_ocr = async () => { window.OCR_STATE = 'ready'; return {ok: true}; };"
        ),
    )
    out = result.value
    assert [label for _key, label in out["tabs"]] == out["expected"]
    assert [key for key, _label in out["tabs"]] == [
        "essential",
        "library",
        "subtitles",
        "downloads",
        "advanced",
    ]
    assert (out["jiten"], out["heading"]) == ("essential", out["jitenHeading"])
    assert out["notInstalled"] == {"tab": "essential", "essential": True}
    assert out["ready"] == {"tab": "advanced", "essential": False, "advanced": True}
    # Parse-ahead and energy sampling are no longer settings.
    assert out["removed"] == []
    assert out["focus"] == {"jimaku": True, "focused": "s_jimaku"}


def test_leaving_the_light_novel_or_manga_library_clears_its_card_selection(page) -> None:
    result = page.run(
        r"""
        setPage('lightnovels'); await __tick(100);
        await __click('#lightNovelsContent [data-ln-book="4"]', {metaKey: true}); await __tick(20);
        const out = {ln: {selected: [...ui.lnSelection]}};
        setPage('current'); await __tick(50);
        setPage('lightnovels'); await __tick(100);
        out.ln.back = {selection: [...ui.lnSelection], cards: document.querySelectorAll('#lightNovelsContent .ln-card.selected').length};
        setPage('manga'); await __tick(200);
        await __click('#mangaContent [data-manga-book="3"]', {metaKey: true}); await __tick(20);
        out.manga = {selected: PudgeMangaReaderV2.selectedBookIds()};
        setPage('lightnovels'); await __tick(50);
        setPage('manga'); await __tick(200);
        out.manga.back = {selection: PudgeMangaReaderV2.selectedBookIds(), cards: document.querySelectorAll('#mangaContent .ln-card.selected').length};
        return out;
        """,
        before=MANGA,
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [{"id": 4, "title": "Novel", "volume": 1}],
            }
        },
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    out = result.value
    assert out["ln"] == {"selected": [4], "back": {"selection": [], "cards": 0}}
    assert out["manga"] == {"selected": [3], "back": {"selection": [], "cards": 0}}


def test_light_novel_parse_ahead_is_always_the_next_chapter(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True)
    service = LightNovelService(cfg)
    assert service.settings().parse_ahead == "next"
    for asked in ("book", "current"):
        service.save_settings({"parse_ahead": asked})
        assert service.settings().parse_ahead == "next"
    # A value an older version stored is ignored too.
    with service._connection() as conn:
        conn.execute("INSERT OR REPLACE INTO ln_settings(key,value) VALUES('parse_ahead','book')")
    assert service.settings().parse_ahead == "next"


def test_energy_diagnostics_follow_the_config_and_start_with_the_app(tmp_path: Path, monkeypatch) -> None:
    from pudge import web_app
    from pudge.energy_diagnostics import EnergyDiagnosticsMonitor
    from test_web_app import make_api

    path = tmp_path / "config.toml"
    path.write_text(
        "[diagnostics]\nenergy_monitoring_enabled = false\nenergy_sample_seconds = 5\n", encoding="utf-8"
    )
    config = load_config(path)
    # The [diagnostics] section is honoured (the interval is at least 10 s).
    assert (config.diagnostics.energy_monitoring_enabled, config.diagnostics.energy_sample_seconds) == (
        False,
        10.0,
    )
    # Without the section: on, every 30 s.
    assert (load_config(tmp_path / "none.toml").diagnostics.energy_monitoring_enabled) is True
    assert load_config(tmp_path / "none.toml").diagnostics.energy_sample_seconds == 30.0
    # The window app starts sampling as it starts on macOS (in Safe Mode too:
    # tests/test_v0724_v37_energy_performance.py).
    started: list[EnergyDiagnosticsMonitor] = []
    monkeypatch.setattr(EnergyDiagnosticsMonitor, "start", lambda monitor: started.append(monitor))
    monkeypatch.setattr(web_app, "sys", SimpleNamespace(**{**vars(sys), "platform": "darwin"}))
    api = make_api(tmp_path / "app")
    try:
        assert started == [api.energy_monitor]
        assert api.energy_monitor.interval_seconds == 30.0
    finally:
        api.close()
