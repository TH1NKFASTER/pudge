"""Cmd+Shift+L exports the debug context of what is open; manga OCR shows real volume
progress; Space on a paired light novel pauses/resumes reliably (behaviour, page harness;
audit 71Б rewrite)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from tests.i18n_support import en, ru

IDLE = {"startup_maintenance": {"running": False}, "ui_state_versions": {"ready": "1", "ui": "1"}}

# Opening a book or a volume waits for animation frames: tick until done.
SETTLE = """
const settle = async promise => {
  let done = false;
  promise.then(() => { done = true; }, () => { done = true; });
  for (let k = 0; k < 200 && !done; k++) await __tick(10);
};
const exportKeys = async init => { await __key('L', {code: 'KeyL', ...init}); await __tick(10); };
"""

# A manga volume of 20 pages, an anime for Anime Debug and a light novel (id 6) read
# together with audiobook 75; window.__actual is the audiobook player.
LIBRARY = """
window.__wait = ms => new Promise(resolve => setTimeout(resolve, ms));
__api.manga_state = async () => ({books: [{id: 3, title: 'Vol 1', series_title: 'Manga', page_count: 20, position: 0}], ocr_available: true});
__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 20, name: `p${index}.png`, spread: false,
                                         data_uri: 'data:image/png;base64,AAAA'});
__api.anime_debug_snapshot = async (mediaId, episode) => ({anime: {media_id: mediaId, title: 'Show'}, selected_episode: episode ?? 3});
__api.export_anime_debug_snapshot = async () => ({path: '/logs/anime-debug.json'});
window.__actual = {audiobook_id: 75, ln_book_id: 6, ln_chapter_index: 0, alignment: {ready: true, status: 'ready'}, linked: true,
                   playing: true, player_running: true, paused: false, position: 100, speed: 1, duration: 3600, chapter_char_offset_exact: 10};
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}],
  paired_audio: {ln_book_id: id, alignment_mode: 'ready',
                 book: {id: 75, playing: window.__actual.playing, player_running: true, paused: window.__actual.paused}}});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['本文です。'], tokens: [[]]});
__api.light_novel_paired_state = async () => { await window.__wait(5); return {...window.__actual}; };
__api.audiobook_set_paused = async (_id, paused) => {
  await window.__wait(30); window.__actual = {...window.__actual, playing: !paused, paused}; return {ok: true};
};
__api.light_novel_export_audio_sync_trace = async () => ({path: '/logs/paired-trace.json'});
"""
LN_SETTINGS = {"settings": {"review_gate_ln_enabled": False, "review_gate_manga_enabled": False}}


def test_cmd_shift_l_exports_the_debug_context_of_what_is_open(page) -> None:
    result = page.run(
        SETTLE
        + """
        const exports = () => __apiCalls.filter(call => call.name.includes('export')).map(call => call.name);
        const out = {};
        await exportKeys({metaKey: true});                                  // Cmd+L, Ctrl+Shift+L, a held key: nothing
        await exportKeys({ctrlKey: true, shiftKey: true});
        await exportKeys({metaKey: true, shiftKey: true, repeat: true});
        out.ignored = exports();
        await exportKeys({metaKey: true, shiftKey: true});
        out.plain = exports();

        void PudgeDebug.open({media_id: 7, title: 'Show'}); await __tick(10);
        out.debugActions = [...document.querySelectorAll('#pudgeDebugOverlay [data-debug-action]')].map(b => b.dataset.debugAction);
        await exportKeys({metaKey: true, shiftKey: true});
        out.debug = exports(); out.debugToast = __text('#toast');
        PudgeDebug.close();

        await settle(PudgeMangaReaderV2.openBook(3));
        out.mangaActions = [...document.querySelectorAll('#mangaReaderV2 [data-manga-v2-action]')].map(b => b.dataset.mangaV2Action);
        await __key('O', {shiftKey: true, code: 'KeyO'}); await __tick(10);
        out.shiftO = exports();
        await exportKeys({metaKey: true, shiftKey: true});
        out.manga = exports();
        await __click('#mangaReaderV2 [data-manga-v2-action=close]'); await __tick(10);

        await settle(openLightNovel(6));
        await exportKeys({metaKey: true, shiftKey: true});
        out.novel = exports();
        return {out, exported: t('debug.exported')};
        """,
        state=LN_SETTINGS,
        before=LIBRARY,
        api=IDLE,
    )
    out = result.value["out"]
    assert out["ignored"] == []
    assert out["plain"] == ["export_runtime_debug_bundle"]
    assert result.calls_of("export_runtime_debug_bundle") == [
        [{"page": "current", "window_active": True, "ln_book_id": 0}]
    ]
    assert out["debug"] == out["plain"] + ["export_anime_debug_snapshot"]
    assert result.calls_of("export_anime_debug_snapshot") == [[7, 3]]
    assert out["debugToast"].endswith(f"{result.value['exported']}: /logs/anime-debug.json")
    assert out["shiftO"] == out["debug"]  # no separate manga OCR debug key
    assert out["manga"] == out["debug"] + ["manga_export_ocr_debug"]
    assert result.calls_of("manga_export_ocr_debug")[0][0]["book"]["id"] == 3
    assert out["novel"] == out["manga"] + ["light_novel_export_audio_sync_trace"]
    # The shortcut replaced the export buttons.
    assert "export" not in out["debugActions"] and "ocr-debug" not in out["mangaActions"]


def test_runtime_debug_bundle_goes_to_the_debug_log_folder_and_is_revealed(
    tmp_path: Path, monkeypatch
) -> None:
    from pudge.logging_utils import debug_log_dir
    from tests.test_web_app import make_api

    api = make_api(tmp_path)
    opened: list[list[str]] = []
    monkeypatch.setattr("pudge.web_app.subprocess.Popen", lambda command, *a, **k: opened.append(command))

    result = api.export_runtime_debug_bundle({"page": "current", "window_active": True, "ln_book_id": 0})

    bundle = Path(result["path"])
    assert result["ok"] is True and bundle.parent == debug_log_dir() and bundle.is_file()  # never the Desktop
    with zipfile.ZipFile(bundle) as archive:
        assert json.loads(archive.read("manifest.json"))["frontend"] == {
            "page": "current",
            "window_active": True,
            "ln_book_id": 0,
        }
    assert opened == [["open", "-R", str(bundle)]]


# Volume OCR statuses in order (the toolbar button starts it).
OCR_STATUSES = [
    {
        "running": True,
        "state": "running",
        "processing": 1,
        "total_pages": 20,
        "processed_pages": 5,
        "cached_pages": 5,
        "current_page": 6,
    },
    {
        "running": True,
        "state": "running",
        "processing": 1,
        "total_pages": 20,
        "processed_pages": 12,
        "cached_pages": 12,
        "current_page": 13,
    },
    {
        "running": False,
        "state": "ready",
        "complete": True,
        "total_pages": 20,
        "processed_pages": 20,
        "cached_pages": 20,
        "completed": 20,
    },
]


def test_ocr_status_reports_real_volume_progress(page) -> None:
    result = page.run(
        SETTLE
        + """
        window.__ocrStatus = {running: false, total_pages: 20, cached_pages: 0};
        __api.manga_ocr_book_status = async () => window.__ocrStatus;
        __api.start_manga_ocr_book = async () => ({running: true, state: 'queued', queued: 1, total_pages: 20, processed_pages: 0, cached_pages: 0});
        await settle(PudgeMangaReaderV2.openBook(3));
        const button = () => __text('[data-manga-v2-action=ocr-book]');
        const shown = [];                                    // [progress, button] as they change
        new MutationObserver(() => {
          const text = __text('#mangaV2OcrProgress');
          if (text && shown[shown.length - 1]?.[0] !== text) shown.push([text, button()]);
        }).observe(document.getElementById('mangaV2OcrProgress'), {childList: true, characterData: true, subtree: true});
        const idle = {progress: __visible('#mangaV2OcrProgress'), button: button()};
        const [first, ...later] = STATUSES;
        window.__ocrStatus = first;
        void __click('[data-manga-v2-action=ocr-book]');
        for (const status of later) { await __tick(1000); window.__ocrStatus = status; }
        await __tick(1000);
        return {idle, shown,
                texts: {queued: PudgeI18n.t('manga.ocrQueued'), ready: PudgeI18n.t('manga.ocrReady'), page: PudgeI18n.t('manga.pageProcessing', {page: 6}),
                        page13: PudgeI18n.t('manga.pageProcessing', {page: 13}), volume: PudgeI18n.t('manga.ocrVolume'),
                        busy: PudgeI18n.t('manga.ocrRunning'), rebuild: PudgeI18n.t('manga.rebuildOcr')}};
        """.replace("STATUSES", json.dumps(OCR_STATUSES)),
        lang="ru",
        state=LN_SETTINGS,
        before=LIBRARY,
        api=IDLE,
    )
    out, texts = result.value, result.value["texts"]
    assert out["idle"] == {"progress": False, "button": texts["volume"]}
    assert out["shown"] == [
        [f"{texts['queued']} · 0/20 · 1", texts["busy"]],
        [f"OCR · 5/20 · {texts['page']}", texts["busy"]],
        [f"OCR · 12/20 · {texts['page13']}", texts["busy"]],
        [f"{texts['ready']} · 20/20", texts["rebuild"]],
    ]
    assert result.calls_of("start_manga_ocr_book")[-1] == [3, True]  # the button rebuilds the volume
    # Audit 68Б: labels are catalog entries.
    assert (en("manga.ocrQueued"), ru("manga.ocrQueued")) == ("OCR queued", "OCR в очереди")
    assert (en("manga.ocrReady"), ru("manga.ocrReady")) == ("OCR ready", "OCR готов")


# The paired reader is open; Space goes to the reader text.
SPACE = "await __key(' ', {code: 'Space'}, '#lnReaderScroll');"
PAUSES = "__apiCalls.filter(call => call.name === 'audiobook_set_paused').map(call => call.args[1])"


# Space presses faster than the player answers (no await between them).
PRESS_SPACE = "const pressSpace = times => { for (let k = 0; k < times; k++) document.getElementById('lnReaderScroll').dispatchEvent(new KeyboardEvent('keydown', {key: ' ', code: 'Space', bubbles: true, cancelable: true})); };"


def test_rapid_space_toggles_are_coalesced_to_final_intent(page) -> None:
    # Seven quick presses while the audiobook plays end paused with at most two player
    # commands and no restart; two more quick presses (there and back) leave it paused.
    result = page.run(
        SETTLE
        + PRESS_SPACE
        + """
        await settle(openLightNovel(6));
        await __tick(500);
        const before = __text('#lnPairedAudio');
        pressSpace(7);
        await __tick(2000);
        const seven = {tray: __text('#lnPairedAudio'), playing: window.__actual.playing, pauses: PAUSES};
        pressSpace(2);
        await __tick(2000);
        return {before, seven, two: {tray: __text('#lnPairedAudio'), playing: window.__actual.playing, pauses: PAUSES.slice(seven.pauses.length)},
                labels: {pause: PudgeI18n.t('ui.pause'), resume: PudgeI18n.t('ui.resume')}};
        """.replace("PAUSES", PAUSES),
        state=LN_SETTINGS,
        before=LIBRARY,
        api=IDLE,
    )
    out, labels = result.value, result.value["labels"]
    assert out["before"] == labels["pause"]
    assert out["seven"]["playing"] is False and out["seven"]["tray"] == labels["resume"]
    assert 1 <= len(out["seven"]["pauses"]) <= 2 and out["seven"]["pauses"][-1] is True
    assert out["two"]["playing"] is False and out["two"]["tray"] == labels["resume"]
    assert len(out["two"]["pauses"]) <= 2
    assert not result.called("light_novel_play_paired")


def test_toggle_arriving_during_position_apply_is_not_lost(page) -> None:
    # Paused audiobook: Space resumes; a second Space pressed while the reader is moving
    # to the audio position must pause again.
    result = page.run(
        SETTLE
        + PRESS_SPACE
        + """
        await settle(openLightNovel(6));
        await __tick(500);
        const apply = applyLnPairedPosition;
        let pressed = false;
        window.applyLnPairedPosition = async (...args) => {
          if (!pressed) { pressed = true; pressSpace(1); }
          return apply(...args);
        };
        SPACE
        await __tick(2000);
        return {pressed, playing: window.__actual.playing, pauses: PAUSES, after: __text('#lnPairedAudio'),
                resume: PudgeI18n.t('ui.resume')};
        """.replace("SPACE", SPACE).replace("PAUSES", PAUSES),
        state=LN_SETTINGS,
        before=LIBRARY + "window.__actual = {...window.__actual, playing: false, paused: true};",
        api=IDLE,
    )
    out = result.value
    assert out["pressed"] is True
    assert out["pauses"] == [False, True]
    assert out["playing"] is False and out["after"] == out["resume"]
    assert not result.called("light_novel_play_paired")


def test_reader_navigation_while_listening_is_traced_once_per_second_and_space_is_not_navigation(
    page,
) -> None:
    # Manual scrolling (PageDown; the arrows seek the audio) pauses the audio follow and is
    # written to the paired-audio trace (Cmd+Shift+L); key repeat must not flood the trace,
    # and Space (pause/resume) is not scrolling.
    result = page.run(
        SETTLE
        + """
        await settle(openLightNovel(6));
        await __tick(500);
        for (let k = 0; k < 5; k++) { await __key('PageDown', {}, '#lnReaderScroll'); await __tick(100); }
        await __tick(1000);
        await __key('PageDown', {}, '#lnReaderScroll');
        await __tick(3000);
        SPACE
        await __tick(2000);
        SPACE
        await __tick(2000);
        await exportKeys({metaKey: true, shiftKey: true});
        """.replace("SPACE", SPACE),
        state=LN_SETTINGS,
        before=LIBRARY,
        api=IDLE,
    )
    events = result.calls_of("light_novel_export_audio_sync_trace")[0][0]["events"]
    navigation = [event for event in events if event["event"] == "manual_navigation"]
    assert [event["reason"] for event in navigation] == ["scroll", "scroll"]
    assert result.calls_of("audiobook_set_paused") == [[75, True], [75, False]]
