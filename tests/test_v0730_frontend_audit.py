"""Window audit v0.7.30: manga reader page loading, progress and review parts, study backend,
review gate settings, light-novel entry, sidebar shortcuts and seeking, Jiten readings,
reader shortcuts, grammar points and assistant furigana (behaviour; audit 71Б rewrite of
tests/js/frontend_audit.cjs, which rewrote module sources to export their internals and ran
handlers cut out of index.html).

Covered elsewhere: reading progress counted only for pages turned past
(test_v0723_nested_manga_cleanup_delete_ui.py::test_manga_progress_counts_only_pages_turned_past_or_the_last_page),
closing the assistant with either reader (test_reading_assistant.py::test_reader_toolbar_button_and_closing_on_reader_exit),
the newest light novel winning over a slower one and closing while it opens
(test_v0730_ln_entry_contract.py), the sidebar fetching the book at "Go to reading"
(test_sidebar_companion_v4.py::test_go_to_reading_from_the_sidebar_continues_the_running_narration_in_the_reader),
the vertical strip's preload margin (test_patch_d1_memory.py::test_vertical_manga_keeps_only_nearby_pages_decoded)."""

from __future__ import annotations

import json

import pytest

from t71g03_support import MANGA, READER
from t71g10_support import ENTRY, READER_VIEW
from t71g06_support import reader
from t71g07_support import CLICKS
from test_v0728_mokuro_jiten_statistics_g10 import mokuro
from test_sidebar_companion_followup import BOOK, FIRST, SLOT, audio, reviews
from study_page_support import EXPLAIN, GRAMMAR, LLM_ON, LN_BOOK, SELECTION
from test_reading_assistant import CHAT
from t71js_support import SAVES, STRIP, VERTICAL, pages


def _mode(mode: str) -> dict[str, str]:
    return {"pudge.manga.reader.v2": json.dumps({"mode": mode})}


NO_GATES = {"settings": {"review_gate_manga_enabled": False, "review_gate_ln_enabled": False}}
SHOWN = r"""
const shown = () => [...document.querySelectorAll('#mangaV2Pages .manga-v2-page-frame')].map(f => Number(f.dataset.pageIndex));
const fetched = () => callsOf('manga_page').map(c => c[1]);
"""


@pytest.mark.parametrize(
    ("mode", "shown", "fetched", "turned"),
    [("single", [4], [4, 3, 5], [5]), ("double", [4, 5], [4, 5, 2, 6], [6, 7])],
)
def test_page_turns_load_the_shown_pages_and_one_turn_either_way(page, mode, shown, fetched, turned) -> None:
    # Old bug: the preload step was undefined after a change, so a page turn broke half-way.
    result = page.run(
        READER
        + SHOWN
        + r"""
        await open(3);
        const out = {shown: shown(), fetched: fetched()};
        await __click('#mangaReaderV2 [data-manga-v2-action="next"]'); await __tick(200);
        out.turned = shown();
        return out;
        """,
        before=MANGA + pages(12, ocr=False) + "window.MANGA.books[0].position = 4;",
        localStorage=_mode(mode),
    )
    # Volume 3 opens at page 5: the page(s) shown, then the pages one turn back and one ahead.
    assert result.value == {"shown": shown, "fetched": fetched, "turned": turned}


def test_late_progress_replies_change_only_their_own_volume_and_never_lower_it(page) -> None:
    result = page.run(
        READER
        + STRIP
        + r"""
        await openStrip(3); await see(0);
        SAVES.hold = true;
        await see(80); await see(90);                       // two read-through saves of volume 3 on their way
        await __click('#mangaReaderV2 [data-manga-v2-action="close"]'); await __tick(100);
        SAVES.settle('resolve', row => row.index === 79); await __tick(10);    // one arrives with the reader closed
        const out = {closed: saves()};
        await openStrip(4); await see(0);
        SAVES.settle('resolve', row => row.id === 3); await __tick(10);        // the other while volume 4 is open
        await see(10); await see(20);                       // volume 4: read through 9, then 19 ...
        SAVES.settle('resolve', row => row.index === 19); await __tick(10);    // ... answered in the other order
        SAVES.settle('resolve'); SAVES.hold = false; await __tick(10);
        await see(15); await see(16); await see(30);        // back up a little, then on
        out.marks = marks();
        return out;
        """,
        before=MANGA + pages(100, ocr=False) + SAVES,
        **VERTICAL,
    )
    out = result.value
    assert out["closed"]["toasts"] == 0  # a late reply is no failure
    # Volume 3's progress does not count for volume 4: its pages 9 and 19 are saved; once 20
    # pages are confirmed, the older reply (10) does not lower it: 15 is not saved again.
    assert out["marks"] == [[3, 79], [3, 89], [4, 9], [4, 19], [4, 29]]


def test_scrolling_into_the_next_review_part_keeps_the_strip_and_loads_its_pages_only_once_approved(
    page,
) -> None:
    # Reviews come every 20 pages. Old bugs: approving the next part rebuilt the vertical strip
    # and centred the new page (the strip jumped under the reader's eyes).
    result = page.run(
        READER
        + STRIP
        + r"""
        await openStrip(3);
        const strip = [19, 20, 21].map(frame), watchers = __observers.intersection.filter(o => !o.disconnected);
        const preload = watchers.find(o => o !== visibility());
        preload.__fire([19, 20, 21].map(i => ({target: frame(i)}))); await __tick(100);   // pages 19-21 near the screen
        const out = {beforeApproval: callsOf('manga_page').map(c => c[1])};
        visibility().__fire([{target: frame(20), intersectionRatio: .7}, {target: frame(21), isIntersecting: false, intersectionRatio: 0}]);
        await __tick(300);
        out.after = {label: __text('#mangaV2PageLabel'), fetched: callsOf('manga_page').map(c => c[1]),
                     sameStrip: strip.every((node, k) => node === frame(19 + k) && node.isConnected),
                     sameWatchers: __observers.intersection.filter(o => !o.disconnected).length === watchers.length && watchers.every(o => !o.disconnected),
                     centred: Boolean(frame(20).__scrolls || frame(21).__scrolls)};
        return out;
        """,
        before=MANGA
        + pages(60)
        + "window.MANGA.books[0].position = 19; window.MANGA.books[0].read_pages = 20;",
        localStorage=_mode("vertical"),
    )
    out = result.value
    assert out["beforeApproval"] == [19]  # the next part's pages wait for its review
    assert result.calls_of("content_review_begin") == [["manga", 3, 0], ["manga", 3, 20]]
    assert out["after"] == {
        "label": "21 / 60",
        "fetched": [19, 20, 21],
        "sameStrip": True,
        "sameWatchers": True,
        "centred": False,
    }


def test_one_review_opens_a_whole_part_of_twenty_pages(page) -> None:
    result = page.run(
        READER
        + r"""
        const go = async number => {
          await __click('#mangaV2PageLabel'); await __tick(20);
          await __click(`[data-manga-v2-page-option="${number}"]`); await __tick(100);
          return __text('#mangaV2PageLabel');
        };
        await open(3);
        await __click('#mangaReaderV2 [data-manga-v2-action="next"]'); await __tick(100);
        return [await go(20), await go(21), await go(22), await go(19), await go(41)];
        """,
        before=MANGA + pages(100),
        localStorage=_mode("single"),
    )
    assert result.value == ["20 / 100", "21 / 100", "22 / 100", "19 / 100", "41 / 100"]
    # Pages 1-20, 21-40 and 41-60 each needed one review (the gate granted every one at once).
    assert result.calls_of("content_review_begin") == [["manga", 3, 0], ["manga", 3, 20], ["manga", 3, 40]]


def test_reviews_switched_off_in_settings_neither_ask_the_backend_nor_show_the_gate(page) -> None:
    result = page.run(
        READER
        + r"""
        await __settle(openLightNovel(178)); await __tick(100);
        const ln = {open: $('lnReaderShell').classList.contains('open'), title: __text('#lnReaderTitle')};
        await __click('#lnReaderClose'); await __tick(50);
        await open(3);
        await __click('#mangaV2PageLabel'); await __tick(20);
        await __click('[data-manga-v2-page-option="30"]'); await __tick(100);
        return {ln, manga: __text('#mangaV2PageLabel'), gate: document.getElementById('pudgeReviewGate')};
        """,
        before=ENTRY + MANGA + pages(60),
        state={"settings": {"review_gate_ln_enabled": False, "review_gate_manga_enabled": False}},
        localStorage=_mode("single"),
    )
    assert result.value == {"ln": {"open": True, "title": "Book 178"}, "manga": "30 / 60", "gate": None}
    assert not result.called("content_review_begin")


@pytest.mark.parametrize(("backend", "jiten_link"), [("jpdb", False), ("jiten", True)])
def test_manga_words_open_the_card_of_the_chosen_study_service(page, backend, jiten_link) -> None:
    # A Mokuro block (looked up as a whole) and a Live Text selection open the word card of
    # the service chosen in the light-novel settings; the Jiten link is only for Jiten words.
    result = reader(
        page,
        CLICKS
        + r"""
        const at = async (x, y, options) => {
          const term = await clickImage(x / W, 1 - y / H, 0, 0, options);
          const out = {term, jitenLink: Boolean(document.querySelector('#pudgeStudyCard [data-pudge-study-jiten]'))};
          await closeCard(); return out;
        };
        const decks = __apiCalls.filter(c => c.name === 'study_decks').length;
        const out = {block: await at(300, 800)};
        liveTextSelect('猫', {left: 700, top: 300, width: 50, height: 100});
        out.selection = await at(725, 350, {press: false, keepSelection: true});
        out.decks = __apiCalls.filter(c => c.name === 'study_decks').slice(decks).map(c => c.args[0]);
        return out;
        """,
        regions={0: [mokuro("日本語", 0.2, 0.3, 0.2, 0.3)]},
        parse={"日本語": [["日本", 11, 0], ["語", 12, 0]], "猫": [["猫", 1, 0]]},
        api={"light_novel_state": {"version": 1, "settings": {"study_backend": backend}, "books": []}},
    )
    out = result.value
    assert out["block"] == {"term": "日本", "jitenLink": jiten_link}
    assert out["selection"] == {"term": "猫", "jitenLink": jiten_link}
    assert set(out["decks"]) <= {backend}  # the card's decks come from that service
    assert out["decks"] or backend == "jiten"  # (Jiten's were loaded with the window)


def test_a_failing_audiobook_answer_of_an_abandoned_book_does_not_disturb_the_newer_one(page) -> None:
    # Book 1 (with an audiobook) is opened, then book 2 before book 1's audiobook answer came;
    # that answer then fails.
    result = page.run(
        READER_VIEW
        + r"""
        window.LINKS[1] = link({playing: true, player_running: true, paused: false}, {ln_book_id: 1});
        window.HOLD.audiobook_review_context = true;
        const first = openLightNovel(1); await __tick(20);
        window.HOLD.audiobook_review_context = false;
        await __settle(openLightNovel(2)); await __tick(50);
        window.CONTEXT_FAILS = true; window.release('audiobook_review_context');
        await __settle(first); await __tick(50);
        return {title: __text('#lnReaderTitle'), reader: view().reader, text: __text('#lnReader'), toasts: __toasts()};
        """,
        before=ENTRY
        + r"""
        __api.audiobook_review_context = id => later('audiobook_review_context', () => {
          if (window.CONTEXT_FAILS) throw new Error('old context failure');
          return {ok: true};
        });
        """,
        state=NO_GATES,
    )
    out = result.value
    assert out["title"] == "Book 2" and out["reader"] is True and "第0章" in out["text"]
    assert out["toasts"] == []
    assert {call[0] for call in result.calls_of("light_novel_chapter")} == {2}
    assert [call[0]["ln_book_id"] for call in result.calls_of("audiobook_reader_context")] == [2]


def test_typing_in_a_text_field_keeps_the_readers_shortcuts_out(page) -> None:
    # The assistant's input (a textarea) or an editable field over the manga reader: Shift+D
    # and the arrows are typing there, not the debug overlay or page turns.
    manga = reader(
        page,
        r"""
        const mode = () => $('mangaReaderV2').dataset.debugOverlay || 'off';
        const out = {};
        for (const html of ['<textarea></textarea>', '<div contenteditable="true">x</div>']) {
          const holder = document.createElement('div'); holder.innerHTML = html; $('mangaReaderV2').appendChild(holder);
          const field = holder.firstElementChild; field.focus();
          const d = await __key('D', {shiftKey: true}, field), arrow = await __key('ArrowLeft', {}, field); await __tick(200);
          out[html] = {mode: mode(), page: __text('#mangaV2PageLabel'), prevented: d.defaultPrevented || arrow.defaultPrevented};
          holder.remove();
        }
        await __key('D', {shiftKey: true}, 'body'); await __tick(20);
        out.reader = mode();
        return out;
        """,
    )
    for field in ("<textarea></textarea>", '<div contenteditable="true">x</div>'):
        assert manga.value[field] == {"mode": "off", "page": "1 / 4", "prevented": False}, field
    assert manga.value["reader"] == "regions"
    # Cmd+A in such a field selects its own text, not the light-novel chapter.
    ln = page.run(
        READER_VIEW
        + r"""
        await __settle(openLightNovel(178)); await __tick(100);
        const out = {};
        for (const html of ['<textarea>abc</textarea>', '<div contenteditable="true">abc</div>']) {
          const holder = document.createElement('div'); holder.innerHTML = html; document.body.appendChild(holder);
          const field = holder.firstElementChild; field.focus(); getSelection().removeAllRanges();
          const press = await __key('a', {metaKey: true, code: 'KeyA'}, field);
          out[html] = {prevented: press.defaultPrevented, readerSelected: getSelection().rangeCount > 0 && getSelection().getRangeAt(0).startContainer === $('lnReader')};
          holder.remove();
        }
        return out;
        """,
        before=ENTRY,
        state=NO_GATES,
    )
    assert all(value == {"prevented": False, "readerSelected": False} for value in ln.value.values()), (
        ln.value
    )


def test_space_reveals_the_sidebar_card_only_when_the_card_is_what_the_user_sees(page) -> None:
    result = page.run(
        SLOT
        + r"""
        await __tick(1100);
        const press = async (target = 'body') => {
          const event = await __key(' ', {code: 'Space'}, target); await __tick(10);
          return {taken: event.defaultPrevented, revealed: current().answer};
        };
        const out = {};
        slot().hidden = true; out.hiddenSlot = await press(); slot().hidden = false;
        $('pudgeSidebarCompanion').style.display = 'none'; out.notDisplayed = await press(); $('pudgeSidebarCompanion').style.display = '';
        await __settle(openLightNovel(178)); await __tick(100);
        out.lightNovel = await press();
        await __click('#lnReaderClose'); await __tick(50);
        await __settle(PudgeMangaReaderV2.openBook(3)); await __tick(100);
        out.manga = await press();
        await __click('#mangaReaderV2 [data-manga-v2-action="close"]'); await __tick(50);
        showWatchOrderModal(); await __tick(10);
        out.dialog = await press();
        closeModal(); await __tick(10);
        const nav = document.querySelector('.nav button'); nav.focus();
        out.buttonOutside = await press(nav);
        nav.blur();
        out.visible = await press();
        return out;
        """,
        before=reviews(FIRST) + ENTRY + MANGA,
        state=NO_GATES,
    )
    out = result.value
    untouched = {"taken": False, "revealed": False}
    for case in ("hiddenSlot", "notDisplayed", "lightNovel", "manga", "dialog", "buttonOutside"):
        assert out[case] == untouched, case
    assert out["visible"] == {"taken": True, "revealed": True}


def test_a_failed_seek_on_the_sidebar_timeline_is_reported(page) -> None:
    result = page.run(
        r"""
        await __tick(100);
        await __input('.sidebar-audio [data-sc-audio-timeline]', '25'); await __tick(20);
        return __toasts();
        """,
        before=audio(BOOK) + "__api.audiobook_seek_to = async () => { throw new Error('seek offline'); };",
    )
    assert result.value == ["seek offline"]
    assert result.calls_of("audiobook_seek_to") == [[5, 25]]


def test_jiten_readings_of_a_card_are_fetched_once_and_late_details_still_arrive(page) -> None:
    # A card shown again and again (the sidebar and the review gate repaint often) asks for its
    # dictionary details once; an incomplete first answer is completed in the background.
    result = page.run(
        r"""
        const card = {wordId: 2, readingIndex: 0, wordTextPlain: '素性'};
        const show = () => {
          const root = document.createElement('div'); root.innerHTML = '<div class="answer"></div>'; document.body.appendChild(root);
          return root;
        };
        const words = () => __apiCalls.filter(c => c.name === 'study_word').length;
        const accounts = () => __apiCalls.filter(c => c.name === 'study_provider_capabilities').length;
        __api.study_provider_capabilities = async () => ({account_key: 'jiten:A'});
        __api.study_word = async value => ({...value, dictionary_complete: true, primary_reading: 'そせい', all_readings: ['そせい', 'しゅせい']});
        const root = show(), out = {};
        const accountsBefore = accounts();
        for (let k = 0; k < 6; k++) { PudgeJitenWords.hydrateReadings(card, root, '.answer', 'readings', () => card); await __tick(1000); }
        await __tick(60000);
        out.complete = {words: words(), accounts: accounts() - accountsBefore, line: root.querySelector('.readings')?.textContent};
        // Another account; its first answer has no dictionary details yet.
        let tries = 0;
        __api.study_provider_capabilities = async () => ({account_key: 'jiten:B'});
        __api.study_word = async value => ++tries === 1 ? value
          : {...value, dictionary_complete: true, primary_reading: 'そせい', all_readings: ['そせい', 'すぞう']};
        const cold = show();
        PudgeJitenWords.hydrateReadings(card, cold, '.answer', 'readings', () => card); await __tick(10);
        out.first = cold.querySelector('.readings')?.textContent ?? null;
        await __tick(1000);
        out.later = cold.querySelector('.readings')?.textContent ?? null;
        return out;
        """
    )
    out = result.value
    assert out["complete"] == {"words": 1, "accounts": 2, "line": "しゅせい"}
    assert out["first"] is None and out["later"] == "すぞう"


def test_grammar_points_with_unusual_ids_are_still_selected_and_shown(page) -> None:
    # The point ids come from the model's answer; quotes or brackets in them must not break
    # selecting a point from the sentence.
    analysis = {
        "status": "complete",
        "sentence": "猫が好きだ。",
        "points": [
            {
                "id": 'point"]odd',
                "pattern": "～が好き",
                "spans": [{"start": 1, "end": 4, "quote": "が好き"}],
                "anchored": True,
                "certainty": "high",
                "explanation": "liking",
            },
            {
                "id": "p[2]'",
                "pattern": "だ",
                "spans": [{"start": 4, "end": 5, "quote": "だ"}],
                "anchored": True,
            },
        ],
    }
    result = page.run(
        SELECTION
        + EXPLAIN
        + r"""
        window.GRAMMAR_ANSWERS.push(ANALYSIS);
        await explain(0);
        const box = grammarBoxes().at(-1);
        const choose = async text => {
          const segment = [...box.querySelectorAll('.pudge-grammar-seg')].find(s => s.textContent === text);
          await __click(segment); await __tick(10);
          const active = [...box.querySelectorAll('[data-grammar-point].active')];
          return {points: active.map(p => p.dataset.grammarPoint), shown: active.map(p => Boolean(p.__scrolls))};
        };
        return {first: await choose('が好き'), second: await choose('だ')};
        """.replace("ANALYSIS", json.dumps(analysis, ensure_ascii=False)),
        before=LN_BOOK + GRAMMAR,
        state={"settings": {"review_gate_ln_enabled": False, **LLM_ON}},
    )
    assert result.value == {
        "first": {"points": ['point"]odd'], "shown": [True]},
        "second": {"points": ["p[2]'"], "shown": [True]},
    }


def test_assistant_furigana_reach_an_answer_redrawn_while_its_words_were_looked_up(page) -> None:
    # The answer's Japanese is looked up (Jiten) to add furigana; a quick follow-up question
    # redraws the conversation meanwhile. The furigana still appear on the answer on screen.
    result = page.run(
        SELECTION
        + EXPLAIN
        + r"""
        let release;
        __api.study_parse_text = async text => {
          if (text === '猫です' && !release) await new Promise(resolve => { release = resolve; });
          const paragraphs = text.split('\n');
          return {paragraphs, tokens: paragraphs.map(p => p === '猫です' ? [{start: 0, end: 1, wordId: 101, readingIndex: 0, rubies: [{start: 0, end: 1, text: 'ねこ'}]}] : []),
                  vocabulary: [{wordId: 101, readingIndex: 0, spelling: '猫', reading: 'ねこ', meaningsChunks: [['cat']], cardState: ['new']}],
                  settings: {study_backend: 'jiten'}};
        };
        const ask = async text => { await __input('[data-pa-input]', text, {change: false}); await __key('Enter', {code: 'Enter'}, '[data-pa-input]'); await __tick(50); };
        const answer = () => [...document.querySelectorAll('#pudgeAssistant .pa-msg')].find(m => m.textContent.includes('です'));
        window.GRAMMAR_ANSWERS.push({status: 'complete', sentence: '猫が好きだ。', translation: 'I like cats', points: []});
        await explain(0);
        window.CHAT.push({ok: true, reply: '猫です'});
        await ask('これは？');                          // the answer arrives; its lookup waits
        const first = answer();
        window.CHAT.push({ok: true, reply: 'ok'});
        await ask('もう一つ');                          // the conversation is redrawn
        const out = {redrawn: answer() !== first};
        release(); await __tick(100);
        out.furigana = [...answer().querySelectorAll('rt')].map(rt => rt.textContent);
        return out;
        """,
        before=LN_BOOK + GRAMMAR + CHAT,
        state={"settings": {"review_gate_ln_enabled": False, **LLM_ON}},
    )
    assert result.value == {"redrawn": True, "furigana": ["ねこ"]}
