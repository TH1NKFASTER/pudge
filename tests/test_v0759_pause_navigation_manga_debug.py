"""Read together: Pause keeps the live mpv player and the exact place; choosing a chapter or
scrolling by hand holds the reader's audio follow for a while. Manga: the OCR debug log
(Cmd+Shift+L, no toolbar button) carries what the overlay drew, the pointer, the selection
and the OCR answers (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import base64
import json
import zipfile
from pathlib import Path

from desktop_isolation import patch_webapp_popen

from pudge.web_app import WebAppApi
from t71g21_support import READER, paired
from tests.t71g06_support import column, reader
from tests.t71g07_support import CLICKS, DEBUG_EXPORT

TRACE_EXPORT = "__api.light_novel_export_audio_sync_trace = async () => ({path: '/logs/ln-trace.json'});"


def test_pause_keeps_the_live_player_and_the_exact_place(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader(); await __tick(1500);
        await __key(' ', {code: 'Space'}, 'body'); await __tick(100);       // Pause
        const out = {paused: shown(), label: __text('#lnPairedAudio')};
        await __tick(2000);
        out.later = shown();                                                // the highlight waits where it was
        await __key(' ', {code: 'Space'}, 'body'); await __tick(100);       // Resume
        out.resumed = shown();
        out.texts = [t('ui.resume'), t('ui.pause')];
        out.resumeLabel = __text('#lnPairedAudio');
        return out;
        """,
        **paired(True),
    )
    out = result.value
    assert result.calls_of("audiobook_set_paused") == [[75, True], [75, False]]
    assert not result.called("audiobook_stop") and not result.called("light_novel_play_paired")
    assert (out["label"], out["resumeLabel"]) == tuple(out["texts"])
    assert out["later"] == out["paused"]
    assert out["paused"] <= out["resumed"] < out["paused"] + 0.2  # continues from the same place


# Two chapters; the narration is in the first one.
TWO_CHAPTERS = (
    r"""
const openOneChapter = __api.light_novel_open;
__api.light_novel_open = async id => ({...(await openOneChapter(id)), chapters: [{chapter_index: 0, title: 'One'}, {chapter_index: 1, title: 'Two'}]});
"""
    + TRACE_EXPORT
)


def test_choosing_a_chapter_holds_the_audio_follow_for_five_seconds(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader(); await __tick(1000);
        await __click('#lnChapterPickerButton');
        await __click('[data-ln-chapter-option="1"]'); await __tick(100);
        const chapters = [ui.lnChapter.chapter_index];
        await __tick(4500); chapters.push(ui.lnChapter.chapter_index);      // 4.6 s: still where the user went
        await __tick(1500); chapters.push(ui.lnChapter.chapter_index);      // then back to the narration
        await __key('L', {code: 'KeyL', metaKey: true, shiftKey: true}); await __tick(20);
        return chapters;
        """,
        **paired(True, extra=TWO_CHAPTERS),
    )
    assert result.value == [1, 1, 0]
    [[trace]] = result.calls_of("light_novel_export_audio_sync_trace")
    holds = [event for event in trace["events"] if event["event"] == "manual_chapter_hold"]
    assert holds and {(event["visible_chapter"], event["target_chapter"]) for event in holds} == {(1, 0)}


def test_scrolling_by_hand_holds_auto_scroll_for_two_seconds(page) -> None:
    # The reader follows the spoken word when it leaves the screen; a scroll with the
    # trackpad holds that for 2 s, after which the reader comes back to the word even if
    # it is still the same word.
    result = page.run(
        READER
        + r"""
        await openReader();
        const reader = $('lnReader'), scroller = $('lnReaderScroll');
        reader.innerHTML = '<p data-ln-paragraph="0" data-ln-audio-start="0" data-ln-audio-end="30">'
          + ['一', '二', '三'].map((w, i) => `<span class="ln-word" data-ln-token="0:${i}" data-ln-audio-start="${i * 10}" data-ln-audio-end="${i * 10 + 10}">${w}</span>`).join('') + '</p>';
        scroller.__rect = {x: 0, y: 0, width: 800, height: 600};
        reader.querySelectorAll('.ln-word').forEach((w, i) => { w.__rect = {x: 0, y: 2000 + i * 40, width: 20, height: 30}; });
        const followed = [];
        Object.getPrototypeOf(reader).scrollIntoView = function () { followed.push(this.textContent); };
        ui.lnPairedState = {...ui.lnPairedState, playing: true};
        const paint = (position, offset) => renderLnPairedPosition({...pairedState(), playing: true, position, ln_chapter_index: 0}, offset, {speechActive: true});
        const out = {};
        paint(101.0, 5); out.first = [...followed];                          // the word is off screen: follow it
        await __tick(1200);
        scroller.dispatchEvent(new WheelEvent('wheel', {deltaY: 40, bubbles: true}));
        await __tick(500); paint(101.5, 6); out.during = [...followed];      // 0.5 s after the scroll: held
        await __tick(1600); paint(102.2, 7); out.after = [...followed];      // 2.1 s after: followed again, same word
        await exportLnPairedTrace();
        return out;
        """,
        **paired(extra=TRACE_EXPORT),
    )
    out = result.value
    assert (out["first"], out["during"], out["after"]) == (["一"], ["一"], ["一", "一"])
    [[trace]] = result.calls_of("light_novel_export_audio_sync_trace")
    assert [event["reason"] for event in trace["events"] if event["event"] == "manual_scroll_hold"] == [
        "scroll"
    ]


# One vertical bubble; the debug log is asked with Cmd+Shift+L.
BUBBLE = column("猫が好き", 0.7, 0.8, source="vision-character-box", status=None)


def test_manga_ocr_debug_log_records_overlay_pointer_selection_and_ocr(page) -> None:
    result = reader(
        page,
        CLICKS
        + r"""
        const out = {buttons: [...document.querySelectorAll('#mangaReaderV2 [data-manga-v2-action]')].map(b => b.dataset.mangaV2Action)};
        // Lay the overlay out like the browser does (dom_lite has no layout).
        for (const node of document.querySelectorAll('#mangaV2Pages .manga-v2-text-region')) {
          node.__rect = {x: parseFloat(node.style.left) * W / 100, y: parseFloat(node.style.top) * H / 100,
                         width: parseFloat(node.style.width) * W / 100, height: parseFloat(node.style.height) * H / 100};
        }
        out.word = await clickText(.725, .763);
        // The user selects the bubble's OCR text copy.
        const copy = document.querySelector('#mangaV2Pages .manga-v2-selection-content');
        const range = document.createRange(); range.selectNodeContents(copy);
        window.getSelection().removeAllRanges(); window.getSelection().addRange(range);
        document.dispatchEvent(new Event('selectionchange'));
        const log = await debugLog();
        const [overlay] = log.frames[0].overlays;
        out.overlay = {text: overlay.text, words: overlay.parsed_token_count, box: Object.values(overlay.normalized_to_image).map(v => Math.round(v * 1000) / 1000)};
        out.events = log.events.filter(e => ['pointer_click', 'selection', 'ocr_result'].includes(e.event)).map(e => ({
          event: e.event, page: e.page_index, region: e.region_index ?? null, text: e.text ?? e.token_text ?? null,
          regions: e.region_count ?? null, under: e.elements ? e.elements.map(el => el.region_index) : null}));
        out.toast = __toasts().pop(); out.saved = t('manga.ocrLogSaved', {path: '/logs/manga-ocr-debug.zip'});
        return out;
        """,
        regions={0: [BUBBLE]},
        parse={"猫が好き": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0]]},
        before=DEBUG_EXPORT,
    )
    out = result.value
    # The log is a shortcut: the reader has no OCR debug button.
    assert "ocr-debug" not in out["buttons"]
    assert out["word"] == "猫"
    # What the overlay drew, in page-image fractions (top-down): the bubble x .7, top .2.
    assert out["overlay"] == {"text": "猫が好き", "words": 3, "box": [0.7, 0.2, 0.05, 0.3]}
    # The OCR answer for the page, the click (and what lay under the pointer) and the selection.
    assert out["events"] == [
        {"event": "ocr_result", "page": 0, "region": None, "text": None, "regions": 1, "under": None},
        {"event": "pointer_click", "page": 0, "region": 0, "text": "", "regions": None, "under": ["0"]},
        {"event": "selection", "page": 0, "region": 0, "text": "猫が好き", "regions": None, "under": None},
    ]
    assert out["toast"] == out["saved"]


class _FakeManga:
    def page(self, book_id: int, page_index: int) -> dict[str, object]:
        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"debug-page").decode("ascii")
        return {
            "book_id": book_id,
            "page_index": page_index,
            "name": "page.png",
            "data_uri": f"data:image/png;base64,{png}",
        }

    def text_regions(
        self,
        book_id: int,
        page_index: int,
        *,
        refresh: bool,
        cached_only: bool,
    ) -> dict[str, object]:
        return {
            "book_id": book_id,
            "page_index": page_index,
            "cached": True,
            "available": True,
            "regions": [
                {
                    "text": "日本語",
                    "orientation": "vertical",
                    "x": 0.7,
                    "y": 0.2,
                    "width": 0.1,
                    "height": 0.4,
                    "confidence": 0.9,
                }
            ],
        }


def test_manga_ocr_debug_export_writes_zip_and_visual_overlay(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    patch_webapp_popen(monkeypatch, lambda *args, **kwargs: None)

    api = WebAppApi.__new__(WebAppApi)
    api.manga = _FakeManga()

    result = api.manga_export_ocr_debug(
        {
            "book": {"id": 7, "title": "Debug manga"},
            "page_index": 2,
            "frames": [
                {
                    "page_index": 2,
                    "overlays": [
                        {
                            "region_index": 0,
                            "text": "日本語",
                            "parsed_token_count": 1,
                            "normalized_to_image": {
                                "left": 0.68,
                                "top": 0.18,
                                "width": 0.14,
                                "height": 0.44,
                            },
                        }
                    ],
                }
            ],
            "events": [{"event": "pointer_click"}],
        }
    )

    archive = Path(result["path"])
    assert archive.is_file()
    assert "debug" in archive.parts
    assert "Downloads" not in archive.parts

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        assert {"snapshot.json", "quality-report.json", "overlay.html", "page.png"} <= names
        snapshot = json.loads(bundle.read("snapshot.json"))
        assert snapshot["backend_ocr"]["regions"][0]["text"] == "日本語"
        quality = json.loads(bundle.read("quality-report.json"))
        assert quality["schema"] == "pudge-manga-ocr-quality-report-v1"
        assert quality["region_count"] == 1
        assert quality["regions"][0]["text"] == "日本語"
        assert "probabilities" in quality["note"]
        overlay = bundle.read("overlay.html").decode("utf-8")
        assert "Backend regions" in overlay
        assert "Rendered overlay" in overlay
        assert "B0" in overlay
        assert "F0" in overlay
        assert "quality-report.json" in overlay
