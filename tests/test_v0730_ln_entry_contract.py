"""Opening the light-novel reader of a book that has an audiobook, Space as play/pause, the
shared reading clock, the book menu and late chapter answers (behaviour; audit 71Б rewrite of
tests/js/ln_entry_contract.cjs, which ran functions cut out of index.html).

"Read and listen" with an idle audiobook (transport shown, nothing started, Back returns to
Audiobooks) is test_future_sidebar_read_together.py::test_read_and_listen_opens_the_player_and_returns_to_audiobooks."""

from __future__ import annotations

import json

import pytest

from t71g10_support import ENTRY, READER_VIEW
from t71g21_support import READER, paired
from tests.i18n_support import en, ru

PLAYING = "{playing: true, player_running: true, paused: false}"
PAUSED = "{playing: false, player_running: true, paused: true}"
IDLE = "{playing: false, player_running: false, paused: false}"


@pytest.mark.parametrize(("flags", "playing"), [(PLAYING, True), (PAUSED, False)])
def test_reading_a_book_whose_audiobook_is_live_joins_it_without_a_review_gate(page, flags, playing) -> None:
    result = page.run(
        READER_VIEW
        + r"""
        window.LINKS[178] = link(FLAGS);
        window.bookmark[178] = [3, 0.2];
        window.PAIRED = {audiobook_id: 281, ln_book_id: 178, ln_chapter_index: 3, linked: true, playing: PLAYING, player_running: true,
                         paused: !PLAYING, position: 120, speed: 1, duration: 900, alignment: {ready: false, status: 'aligning'}};
        await __settle(openLightNovel(178)); await __tick(50);
        const out = {opened: view(), transportAtOpen: transport()};
        await __tick(1500);                                      // a poll: alignment still running
        out.polled = view();
        out.time = __text('#lnPairedTime');
        out.space = await space(); await __tick(20);
        out.labels = {pause: t('ui.pause'), resume: t('ui.resume')};
        return out;
        """.replace("FLAGS", flags).replace("PLAYING", json.dumps(playing)),
        before=ENTRY,
    )
    out = result.value
    label = out["labels"]["pause" if playing else "resume"]
    assert result.calls_of("content_review_begin") == []  # the narration is not interrupted by reviews
    assert out["opened"] == {
        "reader": True,
        "tray": True,
        "controls": True,
        "button": label,
        "disabled": False,
        "autoScroll": True,
    }
    assert out["transportAtOpen"] == []  # opening never seeks, plays or stops
    assert result.calls_of("audiobook_review_context") == [[281, "ln"]]
    [[announced]] = result.calls_of("audiobook_reader_context")
    assert {key: announced[key] for key in ("open", "ln_book_id", "audiobook_id")} == {
        "open": True,
        "ln_book_id": 178,
        "audiobook_id": 281,
    }
    assert announced["generation"] > 0
    assert result.calls_of("light_novel_chapter")[0] == [178, 3]  # the reader opens at its bookmark
    # Basic controls do not wait for the alignment; only following the text does.
    assert out["polled"] == {**out["opened"], "autoScroll": False}
    if not playing:
        assert out["time"].startswith("2:00")  # a paused position does not run on
    assert out["space"] is True
    assert result.calls_of("audiobook_set_paused") == [[281, playing]]


@pytest.mark.parametrize("linked", [True, False])
def test_reading_a_book_with_an_idle_or_no_audiobook_keeps_the_review_gate_and_space_scrolls(
    page, linked
) -> None:
    result = page.run(
        READER_VIEW
        + r"""
        if (LINKED) window.LINKS[178] = link(IDLE);
        await __settle(openLightNovel(178)); await __tick(50);
        return {view: view(), space: await space(), listen: t('ui.listen')};
        """.replace("LINKED", json.dumps(linked)).replace("IDLE", IDLE),
        before=ENTRY,
    )
    out = result.value
    assert result.calls_of("content_review_begin") == [["ln", 178, 0]]
    # Linked: the Listen button is there, the transport stays folded; no audiobook: no tray.
    assert out["view"]["tray"] is linked and out["view"]["controls"] is False
    if linked:
        assert out["view"]["button"] == out["listen"]
    assert out["space"] is False  # Space scrolls the page
    assert not any(
        result.called(name) for name in ("audiobook_set_paused", "light_novel_play_paired", "audiobook_play")
    )
    assert result.calls_of("audiobook_review_context") == ([[281, "ln"]] if linked else [])


def test_a_sidebar_handoff_is_used_only_for_its_own_audiobook(page) -> None:
    # From the sidebar player: the snapshot of audiobook 5 must not replace the backend's link
    # of this book to audiobook 281 ...
    other = page.run(
        READER_VIEW
        + r"""
        window.LINKS[7] = link(IDLE, {ln_book_id: 7});
        await __settle(openLightNovel(7, {readTogether: true, audiobookId: 5, audioBook: {id: 5},
          pairedState: {audiobook_id: 5, ln_chapter_index: 2, playing: true, player_running: true, position: 3}}));
        await __tick(50);
        return {view: view(), listen: t('ui.listen')};
        """.replace("IDLE", IDLE),
        before=ENTRY,
    )
    assert other.calls_of("audiobook_review_context") == [[281, "ln"]]
    assert other.value["view"]["button"] == other.value["listen"]  # nothing was started
    assert other.calls_of("light_novel_chapter")[0] == [7, 0]  # no jump to the snapshot's chapter
    # ... while a snapshot of the book's own audiobook (no backend link yet) takes the reader there.
    own = page.run(
        READER_VIEW
        + r"""
        await __settle(openLightNovel(7, {readTogether: true, audiobookId: 5, audioBook: {id: 5, title: 'A'},
          pairedState: {audiobook_id: 5, ln_chapter_index: 1, playing: false, player_running: true, paused: true, position: 3}}));
        await __tick(50);
        return {view: view(), transport: transport(), resume: t('ui.resume')};
        """,
        before=ENTRY,
    )
    assert own.calls_of("audiobook_review_context") == [[5, "ln"]]
    assert own.value["view"]["controls"] is True and own.value["view"]["button"] == own.value["resume"]
    assert own.calls_of("light_novel_chapter")[0] == [7, 1]
    assert own.value["transport"] == []


def test_the_last_book_chosen_wins_over_a_slower_earlier_one(page) -> None:
    # Book 1's answers come late (its metadata, then its audiobook context); book 2 was opened meanwhile.
    for slow in ("light_novel_open", "audiobook_review_context"):
        result = page.run(
            READER_VIEW
            + r"""
            window.LINKS[1] = link(PLAYING, {ln_book_id: 1});
            window.HOLD[SLOW] = true;
            const first = openLightNovel(1); await __tick(20);
            window.HOLD[SLOW] = false;
            await __settle(openLightNovel(2)); await __tick(50);
            window.release(SLOW); await __settle(first); await __tick(50);
            return {title: __text('#lnReaderTitle'), reader: view().reader};
            """.replace("PLAYING", PLAYING).replace("SLOW", json.dumps(slow)),
            before=ENTRY,
        )
        assert result.value == {"title": "Book 2", "reader": True}, slow
        assert (
            result.calls_of("light_novel_chapter")[0] == [2, 0]
            and len(result.calls_of("light_novel_chapter")) <= 2
        ), slow
        assert [call[0]["ln_book_id"] for call in result.calls_of("audiobook_reader_context")] == [2], slow
        assert not result.called("audiobook_set_paused") and not result.called("audiobook_seek")


def test_closing_the_reader_while_it_opens_stays_closed(page) -> None:
    result = page.run(
        READER_VIEW
        + r"""
        window.LINKS[178] = link(PLAYING);
        window.HOLD.audiobook_review_context = true;
        const pending = openLightNovel(178); await __tick(20);
        const opening = view().reader;
        await __click('#lnReaderClose'); await __tick(20);
        window.HOLD.audiobook_review_context = false;
        window.release('audiobook_review_context'); await __settle(pending); await __tick(1500);
        const out = {opening, after: view().reader, text: __text('#lnReader'), entered: entered()};
        // Back and forth three times: each announcement is newer than the one before.
        for (let k = 0; k < 3; k++) {
          await __settle(openLightNovel(178)); await __tick(50);
          await __click('#lnReaderClose'); await __tick(50);
        }
        out.later = entered().slice(out.entered.length);
        out.transport = transport();
        return out;
        """.replace("PLAYING", PLAYING),
        before=ENTRY,
    )
    out = result.value
    assert out["opening"] is True and out["after"] is False  # the late answer does not reopen it
    assert "第" not in out["text"]  # no chapter was drawn into the closed reader
    assert [(row["open"], row["audiobook_id"]) for row in out["entered"]] == [
        (False, 281)
    ]  # the abandoned entry stays silent
    assert [row["open"] for row in out["later"]] == [True, False] * 3
    generations = [row["generation"] for row in out["entered"] + out["later"]]
    assert generations == sorted(set(generations))
    assert out["transport"] == []  # leaving and returning never touches playback


SPACE_CASES = r"""
const playing = {audiobook_id: 281, ln_book_id: 178, ln_chapter_index: 0, linked: true, playing: true, player_running: true, paused: false,
                 position: 120, speed: 1, alignment: {ready: false, status: 'aligning'}};
const set = state => { window.PAIRED = {...state}; ui.lnPairedState = {...state}; ui.lnPairedTransportDesired = null; };
const press = async (state, init = {}, target = document.body) => {
  set(state); const before = transport().length;
  const prevented = await space(init, target); await __tick(10);
  return {prevented, toggles: transport().length - before};
};
const inReader = html => { const holder = document.createElement('div'); holder.innerHTML = html; $('lnReader').appendChild(holder); return holder.firstElementChild; };
"""


def test_space_plays_and_pauses_a_live_narration_and_leaves_every_other_space_alone(page) -> None:
    result = page.run(
        READER_VIEW
        + SPACE_CASES
        + r"""
        window.LINKS[178] = link(PLAYING);
        await __settle(openLightNovel(178)); await __tick(50);
        ui.lnPairedExpanded = false;                     // the transport folded away
        const out = {playing: await press(playing), held: await press(playing, {repeat: true}),
                     paused: await press({...playing, playing: false, paused: true}),
                     idle: await press({...playing, playing: false, player_running: false}),
                     otherBook: await press({...playing, audiobook_id: 999})};
        for (const [name, init] of Object.entries({composing: {isComposing: true}, meta: {metaKey: true}, shift: {shiftKey: true},
                                                   ctrl: {ctrlKey: true}, alt: {altKey: true}})) out[name] = await press(playing, init);
        // Space already taken by another handler.
        set(playing); const taken = new KeyboardEvent('keydown', {key: ' ', code: 'Space', bubbles: true, cancelable: true});
        taken.preventDefault(); const before = transport().length; document.body.dispatchEvent(taken); await __tick(10);
        out.taken = transport().length - before;
        // Controls that own Space themselves.
        out.owners = {};
        for (const html of ['<input>', '<textarea></textarea>', '<select><option>a</option></select>', '<button>b</button>',
                            '<a href="#x">a</a>', '<div contenteditable="true">e</div>', '<div role="button">r</div>'])
          out.owners[html] = await press(playing, {}, inReader(html));
        // Surfaces that come first: a review card's own Space, the review gate, shortcut recording.
        const grade = PudgeReadingTools.study.handleReviewKeydown; PudgeReadingTools.study.handleReviewKeydown = () => true;
        out.reviewCard = await press(playing); PudgeReadingTools.study.handleReviewKeydown = grade;
        const gate = PudgeReviewGate.handleKeydown; PudgeReviewGate.handleKeydown = () => true;
        out.reviewGate = await press(playing); PudgeReviewGate.handleKeydown = gate;
        ui.shortcutCaptureTarget = document.createElement('button');
        out.recording = (await press(playing)).toggles; ui.shortcutCaptureTarget = null;
        await __click('#lnReaderClose'); await __tick(20);
        out.closed = await press(playing);
        return out;
        """.replace("PLAYING", PLAYING),
        before=ENTRY,
    )
    out = result.value
    toggle, nothing = {"prevented": True, "toggles": 1}, {"prevented": False, "toggles": 0}
    assert out["playing"] == toggle and out["paused"] == toggle
    assert out["held"] == {"prevented": True, "toggles": 0}  # holding Space: no scrolling, no extra toggles
    assert out["idle"] == nothing and out["otherBook"] == nothing
    for name in ("composing", "meta", "shift", "ctrl", "alt", "reviewCard", "reviewGate", "closed"):
        assert out[name] == nothing, name
    assert out["taken"] == 0 and out["recording"] == 0
    assert all(value["toggles"] == 0 for value in out["owners"].values()), out["owners"]


def test_reader_and_sidebar_compute_the_same_text_offset_for_every_clock_shape(page) -> None:
    result = page.run(
        r"""
        const states = [
          {anchor_window: {activity_clock: true, path: [{time: 0, offset: 0}, {time: 8, offset: 8}], activity: [{start: 0, end: 2}, {start: 6, end: 8}]}},
          {anchor_window: {activity_clock: true, path: [{time: 0, offset: 0}, {time: 4, offset: 3, wall_clock_from_previous: true}, {time: 8, offset: 9}], activity: [{start: 5, end: 6}]}},
          {anchor_window: {path: [{time: 1, offset: 0}, {time: 1, offset: 4}, {time: 1, offset: 4}, {time: 3, offset: 10}]}},
          {anchor_window: {left_time: 2770, left_offset: 11770, right_time: 2780, right_offset: 11800}},
          {anchor_window: {points: [{time: 10, offset: 5}, {time: 12, offset: 5}, {time: 13, offset: 9}]}},
          {anchor_window: null, chapter_char_offset_exact: 42.5},
          {anchor_window: {path: [{time: 0, offset: 3}, {time: 2, offset: 1}]}, chapter_char_offset: 7},
        ];
        let compared = 0, worst = 0, nan = 0;
        for (const state of states) for (let time = -1; time <= 2790; time += time < 20 ? .037 : (time < 2760 ? 500 : .13)) {
          const reader = lnPairedOffsetAtTime(state, time), sidebar = PudgePairedAudioClock.offsetAtTime(state, time);
          compared++;
          if (Number.isNaN(sidebar) || Number.isNaN(reader)) { if (Number.isNaN(sidebar) !== Number.isNaN(reader)) nan++; continue; }
          worst = Math.max(worst, Math.abs(reader - sidebar));
        }
        return {compared, worst, nan};
        """
    )
    assert result.value["compared"] > 1000
    assert result.value["worst"] <= 1e-6 and result.value["nan"] == 0


def test_highlight_jumps_with_the_clock_and_waits_while_the_reader_is_scrolled(page) -> None:
    # The reader used to cap the highlight at 0.65 characters per frame: after a jump it crawled.
    result = page.run(
        READER
        + r"""
        await openReader();
        const out = {start: paintAt(1.0, 101.0)};
        await __tick(50);
        out.jump = paintAt(7.5, 107.5);
        // Scrolling by hand: the paint waits (the text under the pointer must not move) ...
        $('lnReaderScroll').dispatchEvent(new Event('scroll'));
        out.scrolling = paintAt(9.5, 109.5);
        // ... and catches up once the scrolling settles.
        await __tick(400);
        out.settled = {word: current(), offset: shown()};
        return out;
        """,
        **paired(playing=False),
    )
    out = result.value
    assert out["start"] == {"word": 0, "offset": pytest.approx(1.0)}
    assert out["jump"] == {"word": 3, "offset": pytest.approx(7.5)}
    assert out["scrolling"] == out["jump"]
    assert out["settled"] == {"word": 4, "offset": pytest.approx(9.5)}


MENU = r"""
const book = extra => ({id: 1, title: 'B', current_chapter: 0, current_offset: 0, bookmark_source: null, bookmark_updated_at: null, ...extra});
const actions = () => [...document.querySelectorAll('#contextMenu [data-ln-context-action]')].map(b => b.dataset.lnContextAction);
"""
# label, book, (Find on Jiten, Find audiobook on Nyaa, Reset reading progress)
MENU_ROWS = [
    ("AniList linked", {"anilist_id": 123}, (False, True, False)),
    ("AniList null", {"anilist_id": None}, (True, True, False)),
    ("AniList 0", {"anilist_id": 0}, (True, True, False)),
    ("AniList '0'", {"anilist_id": "0"}, (True, True, False)),
    ("audiobook linked", {"paired_audio": {"book": {"id": 281}}}, (True, False, False)),
    ("link without an audiobook id", {"paired_audio": {"book": {}}}, (True, True, False)),
    ("read into the chapter", {"current_offset": 0.3}, (True, True, True)),
    ("a later chapter", {"current_chapter": 2}, (True, True, True)),
    (
        "bookmark at the start",
        {"bookmark_source": "reader", "bookmark_updated_at": "2026-10-01"},
        (True, True, True),
    ),
    ("bookmark time only", {"bookmark_updated_at": "2026-10-01"}, (True, True, True)),
    (
        "finished only",
        {"finished": True, "reading_progress": 1, "updated_at": "2026-10-01"},
        (True, True, False),
    ),
    (
        "reset, still finished",
        {"finished": True, "reading_progress": 100, "current_chapter": 0, "current_offset": 0},
        (True, True, False),
    ),
]


def test_book_menu_offers_only_the_actions_that_apply(page) -> None:
    result = page.run(
        MENU
        + r"""
        const rows = ROWS.map(([label, extra]) => { showLnBookMenu(book(extra), 0, 0); return [label, actions()]; });
        showLnSeriesMenu([book({id: 1, anilist_id: 0}), book({id: 2, anilist_id: 55})], 0, 0);
        const series = actions();
        await __click('#contextMenu [data-ln-context-action="open-anilist"]'); await __tick(10);
        showLnSeriesMenu([book({id: 1, anilist_id: 0}), book({id: 2, anilist_id: null})], 0, 0);
        return {rows, series, unlinkedSeries: actions()};
        """.replace("ROWS", json.dumps([[label, extra] for label, extra, _ in MENU_ROWS])),
    )
    for (label, _extra, (jiten, nyaa, reset)), (_label, items) in zip(MENU_ROWS, result.value["rows"]):
        assert ("jiten-search" in items, "find-audiobook-nyaa" in items, "reset-position" in items) == (
            jiten,
            nyaa,
            reset,
        ), label
        assert ("finish" in items) != ("unfinish" in items), label
    # A series is linked when one of its volumes is; its AniList actions use that volume.
    assert "jiten-search" not in result.value["series"]
    assert result.calls_of("open_url") == [["https://anilist.co/manga/55"]]
    assert "jiten-search" in result.value["unlinkedSeries"]


def test_read_and_listen_menu_label(page) -> None:
    for lang, say in (("en", en), ("ru", ru)):
        result = page.run(
            MENU
            + r"""
            showLnBookMenu(book({paired_audio: {book: {id: 281}}}), 0, 0);
            return __text('#contextMenu [data-ln-context-action="read-together"]');
            """,
            lang=lang,
        )
        assert result.value == say("ui.readAndListen")
    assert (en("ui.readAndListen"), ru("ui.readAndListen")) == ("Read and listen", "Читать и слушать")


def test_a_late_failure_of_an_older_chapter_request_does_not_replace_the_current_chapter(page) -> None:
    result = page.run(
        READER_VIEW
        + r"""
        await __settle(openLightNovel(178)); await __tick(100);
        window.HOLD.light_novel_chapter = true;
        const old = loadLightNovelChapter(2, 0, {audioFollow: true}); await __tick(10);   // asked first ...
        const fresh = loadLightNovelChapter(3, 0, {audioFollow: true}); await __tick(10); // ... then the user moved on
        window.CHAPTER_FAILS[2] = 'old chapter failed';
        window.release('light_novel_chapter');           // chapter 2 fails (late) ...
        window.release('light_novel_chapter');           // ... chapter 3 arrives
        const out = {fresh: await __settle(fresh), old: await __settle(old), shown: __text('#lnReader')};
        // A failure of the chapter asked for last is shown, and reported to the caller.
        window.HOLD.light_novel_chapter = false; window.CHAPTER_FAILS[2] = 'boom';
        try { await __settle(loadLightNovelChapter(2, 0, {audioFollow: true})); out.failed = null; } catch (error) { out.failed = error.message; }
        out.error = __text('#lnReader');
        return out;
        """,
        before=ENTRY,
    )
    out = result.value
    assert out["fresh"] is True and out["old"] is False
    assert "第3章" in out["shown"] and "old chapter failed" not in out["shown"]
    assert out["failed"] == "boom" and "boom" in out["error"]


def test_a_failing_look_ahead_of_the_next_chapter_is_not_an_interface_error(page) -> None:
    # Reading chapter 0, the reader fetches chapter 1 ahead; that request fails (e.g. the backend
    # cannot parse it). The user sees the error only when opening chapter 1, not as a toast now.
    result = page.run(
        READER_VIEW
        + r"""
        window.CHAPTER_FAILS[1] = 'chapter 1 is broken';
        await __settle(openLightNovel(178)); await __tick(200);
        return {toasts: __toasts(), interface: PudgeI18n.t('ui.interfaceError', {text: 'chapter 1 is broken'})};
        """,
        before=ENTRY,
        allow_errors=True,
    )
    assert result.value["interface"] not in result.value["toasts"]
    assert result.errors == []
