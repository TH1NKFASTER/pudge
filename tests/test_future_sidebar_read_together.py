"""Reading assistant geometry, "Read and listen" from the light-novel shelf and the sidebar
companion's audiobook / review slot (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

from css_support import css_rules
from study_page_support import GATE, GATE_VIEW
from t71g18_support import ASSISTANT_LAYOUT, LLM, NO_LN_GATE, PAIRED_SHELF, READER_VIEW, paired_ln

GEOMETRY = "pudge.assistant.geometry.v1"
ASSISTANT = (
    ASSISTANT_LAYOUT
    + r"""
const panel = () => $('pudgeAssistant');
const rect = () => { const r = panel().getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height}; };
const pointer = (sel, type, init) => __fire('#pudgeAssistant ' + sel, type, init, 'PointerEvent');
const saved = () => __apiCalls.filter(c => c.name === 'ui_preference_save' && c.args[0] === 'pudge.assistant.geometry.v1').map(c => c.args[1]);
"""
)


def test_assistant_geometry_is_saved_restored_and_reset(page) -> None:
    moved = page.run(
        ASSISTANT
        + r"""
        PudgeAssistant.show();
        const out = {start: rect()};
        await pointer('.pa-head', 'pointerdown', {button: 0, pointerId: 1, clientX: 900, clientY: 30});
        await pointer('.pa-head', 'pointermove', {pointerId: 1, clientX: 700, clientY: 30});
        await pointer('.pa-head', 'pointerup', {pointerId: 1}); await __tick(10);
        out.dragged = rect(); out.afterDrag = saved().at(-1);
        await pointer('[data-pa-resize=w]', 'pointerdown', {button: 0, pointerId: 2, clientX: 568, clientY: 300});
        await pointer('[data-pa-resize=w]', 'pointermove', {pointerId: 2, clientX: 468, clientY: 300});
        await pointer('[data-pa-resize=w]', 'pointerup', {pointerId: 2}); await __tick(10);
        out.resized = rect(); out.afterResize = saved().at(-1);
        // The panel changed size by itself (content, CSS): the new box is saved shortly after.
        panel().style.height = '500px';
        for (const observer of __observers.resize) if (observer.targets.includes(panel())) observer.__fire();
        await __tick(300);
        out.observed = saved().at(-1);
        // A smaller window pulls the panel inside and saves where it went.
        innerWidth = 800; window.dispatchEvent(new Event('resize')); await __tick(10);
        out.window = {rect: rect(), saved: saved().at(-1)};
        return out;
        """,
        viewport={"width": 1200, "height": 800},
        state={"settings": LLM},
    ).value
    assert moved["dragged"] == {**moved["start"], "x": moved["start"]["x"] - 200}
    assert moved["afterDrag"] == moved["dragged"]
    assert moved["resized"] == {
        **moved["dragged"],
        "x": moved["dragged"]["x"] - 100,
        "width": moved["dragged"]["width"] + 100,
    }
    assert moved["afterResize"] == moved["resized"]
    assert moved["observed"] == {**moved["resized"], "height": 500}
    window = moved["window"]["rect"]
    assert window["x"] + window["width"] <= 800 and moved["window"]["saved"] == window

    # The next start shows the panel where it was left; ✦ puts it back at the default place.
    reopened = page.run(
        ASSISTANT
        + r"""
        PudgeAssistant.show();
        const out = {restored: rect()};
        await __click('#pudgeAssistant [data-pa-reset-geometry]'); await __tick(10);
        out.reset = rect(); out.saved = saved().at(-1);
        out.label = $('pudgeAssistant').querySelector('[data-pa-reset-geometry]').getAttribute('aria-label');
        out.expected = PudgeI18n.t('assistant.resetAssistantSizeAndPosition');
        return out;
        """,
        viewport={"width": 1200, "height": 800},
        state={"settings": LLM},
        bootstrap={"schema": 1, "values": {GEOMETRY: moved["afterResize"]}},
    ).value
    assert reopened["restored"] == moved["afterResize"]
    assert reopened["reset"] == moved["start"] and reopened["saved"] == moved["start"]
    assert reopened["label"] == reopened["expected"]


def test_assistant_edges_and_header_show_their_resize_and_move_cursors() -> None:
    rules = css_rules("reading_assistant.css")
    assert rules.get(".pa-head", "cursor") == "move"
    cursors = {
        edge: rules.get(f".pa-resize-{edge}", "cursor")
        for edge in ("n", "s", "e", "w", "ne", "sw", "nw", "se")
    }
    assert cursors == {
        "n": "ns-resize",
        "s": "ns-resize",
        "e": "ew-resize",
        "w": "ew-resize",
        "ne": "nesw-resize",
        "sw": "nesw-resize",
        "nw": "nwse-resize",
        "se": "nwse-resize",
    }


READ_TOGETHER = (
    READER_VIEW
    + r"""
const saves = () => __apiCalls.filter(c => c.name === 'ui_preference_save' && c.args[0] === 'pudge.readTogether.v1').map(c => c.args[1]);
const menu = async action => {
  setPage('lightnovels', true); await __tick(100);
  await __fire('#lightNovelsContent [data-ln-book="6"]', 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
  const items = [...document.querySelectorAll('#contextMenu [data-ln-context-action]')].filter(b => b.checkVisibility()).map(b => b.innerText);
  await __click(`#contextMenu [data-ln-context-action="${action}"]`); await __tick(400);
  return items;
};
const close = async () => { await __click('#lnReaderClose'); await __tick(200); return {page: ui.page, audiobooks: __visible('#audiobooks')}; };
"""
)


def test_read_and_listen_opens_the_player_and_returns_to_audiobooks(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            READ_TOGETHER
            + r"""
            const out = {items: await menu('read-together'), label: PudgeI18n.t('ui.readAndListen'),
                         reader: $('lnReaderShell').classList.contains('open'), transport: transport(), listen: PudgeI18n.t('ui.listen')};
            out.session = saves().at(-1);
            out.closed = await close();
            out.cleared = saves().at(-1);
            // Plain "Read": the player stays folded and Back stays on the shelf.
            await menu('read');
            out.plain = {reader: $('lnReaderShell').classList.contains('open'), transport: transport()};
            out.plainClosed = await close();
            return out;
            """,
            lang=lang,
            before=paired_ln(False),
            state=NO_LN_GATE,
            api=PAIRED_SHELF,
        )
        out = result.value
        assert out["items"].count(out["label"]) == 1
        assert (
            out["reader"] is True and out["transport"]["button"] is True and out["transport"]["controls"] >= 3
        )
        assert out["transport"]["label"] == out["listen"]
        assert {key: out["session"][key] for key in ("bookId", "audiobookId", "returnPage")} == {
            "bookId": 6,
            "audiobookId": 75,
            "returnPage": "audiobooks",
        }
        assert out["closed"] == {"page": "audiobooks", "audiobooks": True} and out["cleared"] is None
        assert out["plain"]["reader"] is True and out["plain"]["transport"]["controls"] == 0
        assert out["plainClosed"]["page"] == "lightnovels"
    # A book without an audiobook has no "Read and listen".
    lonely = page.run(
        READ_TOGETHER + "return {items: await menu('read'), label: PudgeI18n.t('ui.readAndListen')};",
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [{"id": 6, "title": "Novel", "volume": 1}],
            }
        },
        before=paired_ln(False),
        state=NO_LN_GATE,
    )
    assert lonely.value["items"] and lonely.value["label"] not in lonely.value["items"]


def test_plain_read_of_a_book_whose_narration_runs_shows_the_player(page) -> None:
    # A live linked session (the player runs) unfolds the transport even for plain "Read".
    result = page.run(
        READ_TOGETHER
        + "await menu('read'); return {transport: transport(), pause: PudgeI18n.t('ui.pause')};",
        before=paired_ln(True),
        state=NO_LN_GATE,
        api=PAIRED_SHELF,
    )
    assert result.value["transport"]["controls"] >= 3
    assert result.value["transport"]["label"] == result.value["pause"]


# The sidebar mini-player of audiobook 5, which narrates light novel 11 (Jiten words).
SIDEBAR_LIVE = r"""
window.audioBooks = [{id: 5, title: 'Book', playing: true, player_running: true, position: 30, duration: 600, speed: 1,
                      chapters: [{start: 0, end: 600, title: 'One'}], linked_light_novel: {id: 11, title: 'Novel'}}];
__api.sidebar_audio_state = async () => ({books: window.audioBooks});
__api.light_novel_paired_state = async () => ({ln_chapter_index: 0, chapter_char_offset_exact: 1.5, playing: true, position: 30});
__api.light_novel_chapter = async () => ({paragraphs: ['猫が好きです。'],
  tokens: [[{start: 0, end: 1, wordId: 101, readingIndex: 0}, {start: 1, end: 2, wordId: 102, readingIndex: 0}, {start: 2, end: 4, wordId: 103, readingIndex: 0}]],
  vocabulary: [{wordId: 101, readingIndex: 0, spelling: '猫', reading: 'ねこ', meaningsChunks: [['cat']], cardState: ['new']},
               {wordId: 102, readingIndex: 0, spelling: 'が', reading: 'が', cardState: ['known']},
               {wordId: 103, readingIndex: 0, spelling: '好き', reading: 'すき', meaningsChunks: [['liked']], cardState: ['learning']}]});
__api.audiobook_set_paused = async () => ({ok: true});
__api.audiobook_add_bookmark = async () => ({ok: true});
"""


def test_sidebar_live_text_offers_study_cards_and_translation_and_selection_pauses(page) -> None:
    result = page.run(
        r"""
        await __tick(1500);
        const live = '[data-sc-live-text]';
        const out = {spoken: __text(`${live} .ln-paired-word-current`)};
        await __fire(`${live} [data-pudge-study-token]`, 'pointerover', {}, 'PointerEvent'); await __tick(1500);
        out.studyCard = {open: __visible('#pudgeStudyCard'), text: __text('#pudgeStudyCard')};
        const range = document.createRange(); range.selectNodeContents(document.querySelector(`${live} p`));
        getSelection().removeAllRanges(); getSelection().addRange(range);
        await __fire(`${live} p`, 'mouseup', {button: 0}, 'MouseEvent'); await __tick(50);
        out.translateOffered = __visible('#pudgeTranslationPop [data-pudge-translate-now]');
        await __click('.sidebar-audio [data-sc-audio="bookmark"]'); await __tick(10);
        return out;
        """,
        before=SIDEBAR_LIVE,
    )
    out = result.value
    assert out["spoken"] == "が"
    assert out["studyCard"]["open"] is True and "猫" in out["studyCard"]["text"]
    assert out["translateOffered"] is True
    # Selecting pauses the narration; it never stops the player.
    assert result.calls_of("audiobook_set_paused") == [[5, True]] and not result.called("audiobook_stop")
    assert result.calls_of("audiobook_add_bookmark") == [[5, ""]]


NOW = 1790856000000  # the page's wall clock at start (2026-10-01 12:00 UTC)
DUE = r"""
window.dueCards = [{wordId: 1, readingIndex: 0, wordTextPlain: '選ぶ', readings: [{readingIndex: 0, text: '選ぶ', rubyText: '選[えら]ぶ'}],
                    definitions: [{meanings: ['choose']}]}];
__api.sidebar_due_review_cards = async (count, excluded) => ({cards: window.dueCards.filter(c => !excluded.includes(`${c.wordId}:${c.readingIndex}`)).slice(0, count)});
__api.sidebar_review_settings = async value => ({interval: value || '180000'});
__api.study_provider_capabilities = async () => ({account_key: 'jiten:me', configured: true});
__api.study_action = async () => ({ok: true, outcome: 'confirmed'});
__api.study_states = async payload => ({ok: true, account_scope: 'jiten:me', states: payload.words.map(([w, r]) =>
  ({ok: true, wordId: w, readingIndex: r, states: ['young'], normalizedState: 'learning', studyDeckIds: []}))});
"""
DUE_VIEW = r"""
const dueShown = () => Boolean(document.querySelector('[data-sidebar-due] .sidebar-due-card'));
const label = () => __text('[data-sidebar-due] .sidebar-due-label');
const lastAt = () => Number(localStorage.getItem('pudge.sidebarReview.lastAt.v1'));
"""
# Three minutes between sidebar cards; the last review was a minute ago (before a restart).
LAST_MINUTE = {"localStorage": {"pudge.sidebarReview.lastAt.v1": str(NOW - 60_000)}}
# A word reviewed in the reader's study card (reading_tools.js).
READER_REVIEW = r"""
const box = document.createElement('div');
box.innerHTML = PudgeReadingTools.study.renderParsedText({paragraphs: ['少し'], tokens: [[{wordId: 7, readingIndex: 0, start: 0, end: 2}]],
  vocabulary: [{wordId: 7, readingIndex: 0, spelling: '少し', states: ['young'], studyDeckIds: []}]}, {backend: 'jiten'});
document.body.appendChild(box); await __tick(100);
await __click('[data-pudge-study-token]'); await __tick(50);
await __click('#pudgeStudyCard [data-pudge-study-review="good"]'); await __tick(50);
"""


def test_sidebar_card_waits_for_the_interval_by_the_wall_clock(page) -> None:
    result = page.run(
        DUE_VIEW
        + r"""
        await __tick(1100);
        const out = {start: dueShown()};
        await __tick(117000); out.at118s = dueShown();
        await __tick(4000); out.at122s = dueShown();
        // Reviewing elsewhere (the reader's study card) restarts the wait.
        """
        + READER_REVIEW
        + r"""
        out.reviewed = {shown: dueShown(), lastAt: lastAt(), now: Date.now()};
        await __tick(178000); out.before3min = dueShown();
        await __tick(4000); out.after3min = dueShown();
        return out;
        """,
        before=DUE,
        **LAST_MINUTE,
    )
    out = result.value
    assert (out["start"], out["at118s"], out["at122s"]) == (False, False, True)
    assert result.calls_of("study_action")[0][0]["action"] == "review"
    assert (
        out["reviewed"]["shown"] is False and abs(out["reviewed"]["lastAt"] - out["reviewed"]["now"]) < 1000
    )
    assert (out["before3min"], out["after3min"]) == (False, True)


def test_a_review_gate_review_also_restarts_the_sidebar_wait(page) -> None:
    result = page.run(
        DUE_VIEW
        + GATE_VIEW
        + r"""
        await __tick(122000);
        const out = {before: dueShown()};
        await startPlay(EPISODE); await __tick(20);
        await space(); await gradeKey(3); await space(); await __tick(50);   // reveal, choose Good, confirm
        out.after = {shown: dueShown(), lastAt: lastAt(), now: Date.now()};
        return out;
        """,
        before=DUE + GATE,
        **LAST_MINUTE,
    )
    out = result.value
    assert out["before"] is True
    assert result.called("review_gate_review")
    assert out["after"]["shown"] is False and abs(out["after"]["lastAt"] - out["after"]["now"]) < 1000


def test_sidebar_says_it_waits_for_the_next_review_during_the_interval(page) -> None:
    result = page.run(
        DUE_VIEW
        + "await __tick(5000); return {label: label(), waiting: PudgeI18n.t('sidebar.waitingForNextReview')};",
        before=DUE,
        **LAST_MINUTE,
    )
    assert result.value["label"].endswith(result.value["waiting"])


def test_review_interval_choices_have_no_off(page) -> None:
    # The sidebar review cannot be switched off: the shortest wait is a minute, or continuous.
    result = page.run(
        "setPage('settings', true); await __tick(50); return [...$('s_sidebar_review_interval').options].map(o => o.value);",
        before=DUE,
    )
    assert result.value == ["60000", "180000", "300000", "600000", "1800000", "continuous"]
