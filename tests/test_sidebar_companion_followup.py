"""The sidebar companion: Jiten review card and audiobook mini-player (behaviour; audit 71Б
rewrite: page harness instead of source text and of tests/js/sidebar_companion.cjs)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from css_support import css_rules
from pudge.web_app import WebAppApi


def card(word_id: int, word: str = "選ぶ", ruby: str = "選[えら]ぶ", **extra) -> dict:
    return {
        "wordId": word_id,
        "readingIndex": 0,
        "wordTextPlain": word,
        "readings": [{"readingIndex": 0, "text": word, "rubyText": ruby}],
        "pitchAccents": ["2"],
        "definitions": [{"meanings": ["choose", "select"]}],
        "exampleSentence": {"text": "**選ぶ**のは難しい"},
        "sourceDeckName": "Long source",
        **extra,
    }


FIRST, SECOND, THIRD = (
    card(1),
    card(2, "古い", "古[ふる]い", pitchAccents=[]),
    card(3, "新", "新[しん]", pitchAccents=[]),
)

# Continuous reviews (the next card at once); the backend answers from window.dueCards
# without the cards the page excludes (shown, queued, pending, recently reviewed).
REVIEWS = """
window.dueCards = CARDS;
__api.sidebar_due_review_cards = async (count, excluded) => ({cards: window.dueCards
  .filter(c => !excluded.includes(`${c.wordId}:${c.readingIndex}`)).slice(0, count)});
__api.sidebar_review_settings = async value => ({interval: value || 'continuous'});
"""


def reviews(*cards: dict) -> str:
    return REVIEWS.replace("CARDS", json.dumps(list(cards), ensure_ascii=False))


# Test-side helpers: the review slot, its current card and the previous (just graded) card.
SLOT = r"""
const slot = () => document.querySelector('[data-sidebar-due]');
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
const view = selector => {
  const node = slot().querySelector(selector);
  if (!node) return null;
  const word = node.querySelector('.sidebar-due-word');
  return {text: node.innerText, word: word?.querySelector('.sidebar-due-front-base')?.textContent ?? word?.textContent ?? null,
          reading: word?.querySelector('.sidebar-due-front-reading')?.textContent ?? null,
          readingShown: Boolean(word?.querySelector('.show-reading')), readingReserved: Boolean(word?.querySelector('.hide-reading')),
          lines: [...node.querySelectorAll('.sidebar-due-base-line')].map(line => line.textContent),
          width: word ? Number(word.dataset.charWidth) : null,
          moras: [...node.querySelectorAll('.pudge-pitch-mora')].map(m => [m.textContent, m.className.replace('pudge-pitch-mora', '').trim()]),
          answer: Boolean(node.querySelector('.sidebar-due-answer')),
          undo: node.querySelector('[data-sc-undo]') ? !node.querySelector('[data-sc-undo]').disabled : null};
};
const current = () => view('.sidebar-due-card');
const previous = () => view('.sidebar-due-previous');
const calls = name => __apiCalls.filter(call => call.name === name).map(call => call.args);
"""


def test_review_card_reveals_grades_and_undoes_with_the_keyboard(page) -> None:
    result = page.run(
        SLOT
        + r"""
        await __tick(1100);
        const out = {front: current(), showAnswer: [__text('[data-sc-reveal]'), PudgeI18n.t('gate.showAnswer')]};
        const space = await __key(' ', {code: 'Space'});
        out.revealed = {prevented: space.defaultPrevented, card: current()};
        out.labels = {example: PudgeI18n.t('gate.example'), source: PudgeI18n.t('table.source'),
                      good: PudgeI18n.t('review.action.good'), undo: PudgeI18n.t('manga.undo')};
        const save = deferred();
        __api.sidebar_due_review_submit = () => save.promise;
        await __key('3', {code: 'Digit3'});                          // the Good shortcut focuses Good ...
        out.focused = document.activeElement?.innerText;
        await __key(' ', {code: 'Space'});                           // ... and Space presses it
        out.saving = {current: current(), previous: previous()};
        save.resolve({ok: true, outcome: 'confirmed'}); await __tick(10);
        out.saved = {current: current(), previous: previous()};
        __api.sidebar_due_review_undo = async () => ({ok: true, outcome: 'undone'});
        const undo = await __key('z', {metaKey: true});
        out.undone = {prevented: undo.defaultPrevented, current: current(), previous: previous()};
        // Grade again: Undo stays possible for ten seconds only.
        await __key(' ', {code: 'Space'}); await __key('1', {code: 'Digit1'}); await __key(' ', {code: 'Space'});
        __api.sidebar_due_review_submit = async () => ({ok: true, outcome: 'confirmed'});
        await __tick(9000);
        out.within = previous().undo;
        await __tick(1100);
        out.after = previous()?.undo ?? null;
        await __key('z', {metaKey: true});
        return out;
        """,
        before=reviews(FIRST, SECOND, THIRD),
    )
    out = result.value
    front = out["front"]
    assert out["showAnswer"][0] == out["showAnswer"][1]
    # The reading (with pitch) is reserved above the word from the first frame, hidden until the answer.
    assert (front["word"], front["reading"], front["readingReserved"]) == ("選ぶ", "えらぶ", True)
    assert front["moras"] == [["え", "low rise"], ["ら", "high drop"], ["ぶ", "low"]]
    revealed = out["revealed"]["card"]
    assert out["revealed"]["prevented"] is True and revealed["readingShown"] is True
    assert revealed["text"].count("選ぶ") == 2  # the word once, and once in the example (without **)
    assert "選ぶのは難しい" in revealed["text"] and "**" not in revealed["text"]
    for text in ("choose", "select", "Long source", out["labels"]["example"], out["labels"]["source"]):
        assert text in revealed["text"]
    assert len(revealed["moras"]) == 3  # the answer repeats neither the word nor its pitch
    assert out["focused"].startswith(out["labels"]["good"])
    # The next prefetched card is shown while the provider is still saving.
    assert out["saving"]["current"]["word"] == "古い" and out["saving"]["previous"]["word"] == "選ぶ"
    assert out["saving"]["previous"]["text"].split("\n")[-2:] == [
        out["labels"]["good"],
        out["labels"]["undo"],
    ]
    assert (
        "choose" not in out["saving"]["previous"]["text"]
        and "Long source" not in out["saving"]["previous"]["text"]
    )
    assert out["saved"]["current"]["word"] == "古い"
    [[word_id, reading, grade, attempt]] = result.calls_of("sidebar_due_review_submit")[:1]
    assert (word_id, reading, grade) == (1, 0, "good") and attempt
    assert "1:0" in result.calls_of("sidebar_due_review_cards")[-1][1]  # never fetched back while saving
    # Cmd+Z: the graded card returns to the front, the next one waits in the queue.
    assert out["undone"]["prevented"] is True and out["undone"]["previous"] is None
    assert out["undone"]["current"]["word"] == "選ぶ" and out["undone"]["current"]["readingShown"] is False
    assert result.calls_of("sidebar_due_review_undo") == [[1, 0]]
    assert out["within"] is True and out["after"] is False


def test_reading_stays_attached_to_plain_kana_long_words_and_words_without_pitch(page) -> None:
    plain = {**FIRST, "readings": [{"readingIndex": 0, "text": "えらぶ"}]}
    long_word = {**plain, "wordId": 4, "wordTextPlain": "非常に長い日本語の単語"}
    no_pitch = {**plain, "wordId": 5, "pitchAccents": []}
    alternates = {
        **FIRST,
        "wordId": 6,
        "readings": [*FIRST["readings"], {"readingIndex": 1, "text": "撰ぶ", "rubyText": "撰[よ]ぶ"}],
    }
    result = page.run(
        SLOT
        + r"""
        const show = async next => {
          window.dueCards = [next];
          window.dispatchEvent(new CustomEvent('pudge-review-completed', {detail: {}}));  // reviewed elsewhere: start over
          await __tick(1100);
          return current();
        };
        const out = {};
        for (const [name, next] of Object.entries(CASES)) out[name] = await show(next);
        await __key(' ', {code: 'Space'});
        out.alternatesAnswer = __text('[data-sidebar-due] .sidebar-due-answer');
        return out;
        """.replace(
            "CASES",
            json.dumps(
                {"plain": plain, "long": long_word, "noPitch": no_pitch, "alternates": alternates},
                ensure_ascii=False,
            ),
        ),
        before=reviews(FIRST),
    )
    out = result.value
    assert (out["plain"]["reading"], out["plain"]["readingReserved"], len(out["plain"]["moras"])) == (
        "えらぶ",
        True,
        3,
    )
    assert out["long"]["lines"] == ["非常に長い日", "本語の単語"] and out["long"]["width"] == 6
    assert out["long"]["reading"] == "えらぶ" and len(out["long"]["moras"]) == 3
    assert (out["noPitch"]["reading"], out["noPitch"]["moras"]) == ("えらぶ", [])
    assert out["plain"]["width"] == 2
    # The answer lists the other readings only (not the one already above the word).
    assert "よぶ" in out["alternatesAnswer"] and "えらぶ" not in out["alternatesAnswer"]
    rules = css_rules("sidebar_companion.css")
    assert rules.get('.sidebar-due-word[data-char-width="2"]', "font-size") == "42px"  # short words are big
    assert (
        rules.get(".sidebar-due-front-ruby.hide-reading .sidebar-due-front-reading", "visibility") == "hidden"
    )
    assert (
        rules.get(".sidebar-due-front-ruby.show-reading .sidebar-due-front-reading", "visibility")
        == "visible"
    )


def test_the_displayed_reading_opens_in_jiten_without_grading(page) -> None:
    linked = {
        **FIRST,
        "wordId": 3,
        "readingIndex": 2,
        "readings": [{"readingIndex": 2, "text": "選ぶ", "rubyText": "選[えら]ぶ"}],
    }
    result = page.run(
        SLOT
        + r"""
        await __tick(1100);
        const hrefs = () => [...slot().querySelectorAll('a.sidebar-due-word')].map(a => a.getAttribute('href'));
        const out = {front: hrefs()};
        await __click('[data-sidebar-due] .sidebar-due-card .sidebar-due-word');
        out.afterFront = current().readingShown;
        await __key(' ', {code: 'Space'});
        out.answer = hrefs();
        await __click('[data-sidebar-due] .sidebar-due-card .sidebar-due-word');
        out.stillRevealed = current().readingShown;
        __api.sidebar_due_review_submit = async () => ({ok: true, outcome: 'confirmed'});
        await __click('[data-sc-grade="good"]'); await __tick(10);
        out.previous = [...slot().querySelectorAll('.sidebar-due-previous a.sidebar-due-word')].map(a => a.getAttribute('href'));
        await __click('[data-sidebar-due] .sidebar-due-previous .sidebar-due-word');
        return out;
        """,
        before=reviews(linked, SECOND),
    )
    out = result.value
    url = "https://jiten.moe/vocabulary/3/2"
    assert out["front"] == [url] and out["answer"] == [url] and out["previous"] == [url]
    assert (
        out["afterFront"] is False and out["stillRevealed"] is True
    )  # opening Jiten neither reveals nor grades
    assert result.calls_of("open_url") == [[url], [url], [url]]
    assert result.calls_of("sidebar_due_review_submit")[0][:3] == [3, 2, "good"]


def test_empty_is_shown_only_after_a_completed_fetch(page) -> None:
    result = page.run(
        SLOT
        + r"""
        const labels = {loading: PudgeI18n.t('sidebar.loadingCards'), empty: PudgeI18n.t('sidebar.noCardsDue'),
                        error: PudgeI18n.t('sidebar.couldNotLoadCards'), retry: PudgeI18n.t('media.retry')};
        const first = deferred();
        __api.sidebar_due_review_cards = () => first.promise;
        window.dispatchEvent(new CustomEvent('pudge-review-completed', {detail: {}}));
        const out = {labels, loading: __text('[data-sidebar-due]')};
        first.resolve({cards: []}); await __tick(1100);
        out.empty = __text('[data-sidebar-due]');
        const refill = deferred();
        __api.sidebar_due_review_cards = () => refill.promise;
        window.dispatchEvent(new CustomEvent('pudge-review-completed', {detail: {}}));
        out.refilling = __text('[data-sidebar-due]');
        refill.resolve({cards: [CARD]}); await __tick(1100);
        out.card = current()?.word;
        __api.sidebar_due_review_cards = async () => ({});                          // malformed answer
        window.dispatchEvent(new CustomEvent('pudge-review-completed', {detail: {}})); await __tick(10);
        out.malformed = __text('[data-sidebar-due]');
        __api.sidebar_due_review_cards = async () => ({cards: [CARD]});
        await __click('[data-sc-retry]'); await __tick(10);
        out.retried = current()?.word;
        return out;
        """.replace("CARD", json.dumps(SECOND, ensure_ascii=False)),
        before=reviews(),
    )
    out = result.value
    labels = out["labels"]
    assert labels["loading"] in out["loading"] and labels["empty"] not in out["loading"]
    assert labels["empty"] in out["empty"]
    assert labels["loading"] in out["refilling"] and labels["empty"] not in out["refilling"]
    assert out["card"] == "古い"
    assert (
        labels["error"] in out["malformed"]
        and labels["retry"] in out["malformed"]
        and labels["empty"] not in out["malformed"]
    )
    assert out["retried"] == "古い"
    # Without the backend method at all: an error with Retry, never "No cards due".
    missing = page.run(
        SLOT + "await __tick(1100); return __text('[data-sidebar-due]');",
        before=reviews(),
        missingApi=["sidebar_due_review_cards", "sidebar_due_review_card"],
    )
    assert labels["error"] in missing.value and labels["empty"] not in missing.value


def test_review_interval_is_an_advanced_setting_kept_by_the_backend(page) -> None:
    code = (
        SLOT
        + r"""
    await __tick(100);
    setPage('settings', true); await __tick(100);
    const select = $('s_sidebar_review_interval'), block = select.closest('.setting-block');
    const out = {value: select.value, stored: localStorage.getItem('pudge.sidebarReview.interval.v1'),
                 options: [...select.options].map(o => o.value), essential: block.checkVisibility()};
    await __click('.settings-categories [data-settings-category="advanced"]');
    out.advanced = block.checkVisibility();
    out.saved = calls('sidebar_review_settings');
    await __input('#s_sidebar_review_interval', 'continuous'); await __tick(10);
    return out;
    """
    )
    stored = {"pudge.sidebarReview.interval.v1": "continuous"}
    # The backend's value wins over this browser origin's storage.
    kept = page.run(
        code,
        localStorage=stored,
        before="__api.sidebar_review_settings = async value => ({interval: value || '600000'});",
    )
    assert kept.value["value"] == "600000" and kept.value["stored"] == "600000"
    assert "continuous" in kept.value["options"]
    assert kept.value["essential"] is False and kept.value["advanced"] is True
    assert kept.value["saved"] == [[]] and kept.calls_of("sidebar_review_settings") == [[], ["continuous"]]
    # Nothing saved in the backend yet: the local value is migrated there.
    legacy = page.run(
        code, localStorage=stored, before="__api.sidebar_review_settings = async value => ({interval: ''});"
    )
    assert legacy.value["value"] == "continuous"
    assert legacy.value["saved"] == [[], ["continuous"]]


MANY = [card(n, f"語{n}", f"語[ご]{n}", pitchAccents=[]) for n in range(1, 21)]


def test_continuous_reviews_prefetch_ahead_and_survive_a_failed_save(page) -> None:
    result = page.run(
        SLOT
        + r"""
        await __tick(1100);
        const fetches = () => calls('sidebar_due_review_cards').length;
        const grade = async () => { await __click('[data-sc-reveal]'); await __click('[data-sc-grade="good"]'); await __tick(10); };
        const out = {first: calls('sidebar_due_review_cards')[0], start: fetches(), words: [current().word], counts: []};
        __api.sidebar_due_review_submit = async () => ({ok: true, outcome: 'confirmed'});
        for (let i = 0; i < 6; i++) { await grade(); out.words.push(current().word); out.counts.push(fetches()); }
        __api.sidebar_due_review_submit = async () => ({ok: false, message: 'offline'});
        await grade();
        out.failed = {current: current(), toasts: __toasts()};
        __api.sidebar_due_review_submit = async () => ({ok: true, outcome: 'confirmed'});
        await __click('[data-sc-grade="good"]'); await __tick(10);
        out.retried = current().word;
        return out;
        """,
        before=reviews(*MANY),
    )
    out = result.value
    assert out["first"][0] == 12  # twelve cards ahead
    assert out["words"] == ["語1", "語2", "語3", "語4", "語5", "語6", "語7"]
    # Eleven left after the first card: no fetch until fewer than seven wait.
    assert out["counts"][:4] == [out["start"]] * 4 and out["counts"][4] == out["start"] + 1
    # A rejected save puts the same card back with its answer open; the next one still waits.
    assert out["failed"]["current"]["word"] == "語7" and out["failed"]["current"]["answer"] is True
    assert out["failed"]["toasts"][-1] == "offline"
    assert out["retried"] == "語8"


def test_one_fetch_fills_one_slot_and_stale_answers_are_dropped(page) -> None:
    shared = page.run(
        SLOT
        + r"""
        await __tick(1100);                       // start-up fetch and the due check wait for one answer
        const fetches = calls('sidebar_due_review_cards').length;
        window.answer({cards: CARDS}); await __tick(10);
        const out = {fetches, word: current().word};
        __api.sidebar_due_review_cards = async () => ({cards: []});
        __api.sidebar_due_review_submit = async () => ({ok: true, outcome: 'confirmed'});
        await __click('[data-sc-reveal]'); await __click('[data-sc-grade="good"]'); await __tick(10);
        out.next = current()?.word;
        return out;
        """.replace("CARDS", json.dumps([FIRST, SECOND], ensure_ascii=False)),
        before=reviews()
        + "__api.sidebar_due_review_cards = () => new Promise(resolve => { window.answer = resolve; });",
    )
    assert shared.value == {"fetches": 1, "word": "選ぶ", "next": "古い"}
    # A card reviewed in another window while a fetch was running never comes back from it.
    stale = page.run(
        SLOT
        + r"""
        await __tick(1100);
        const old = deferred();
        __api.sidebar_due_review_cards = () => old.promise;
        const running = PudgeSidebarCompanion.prefetch(true);
        __api.sidebar_due_review_cards = async () => ({cards: [SECOND]});
        window.dispatchEvent(new CustomEvent('pudge-review-completed', {detail: {backend: 'jiten', wordId: 1, readingIndex: 0}}));
        old.resolve({cards: [FIRST]}); await running; await __tick(1100);
        const out = {word: current().word};
        __api.sidebar_due_review_submit = async () => ({ok: true, outcome: 'confirmed'});
        __api.sidebar_due_review_cards = async () => ({cards: []});
        await __click('[data-sc-reveal]'); await __click('[data-sc-grade="good"]'); await __tick(6000);
        out.next = current()?.word ?? null;
        return out;
        """.replace("SECOND", json.dumps(SECOND, ensure_ascii=False)).replace(
            "FIRST", json.dumps(FIRST, ensure_ascii=False)
        ),
        before=reviews(THIRD),
    )
    assert stale.value == {"word": "古い", "next": None}


def test_undo_pressed_while_saving_runs_after_the_save(page) -> None:
    result = page.run(
        SLOT
        + r"""
        await __tick(1100);
        const save = deferred();
        __api.sidebar_due_review_submit = () => save.promise;
        __api.sidebar_due_review_undo = async () => ({ok: true, outcome: 'undone'});
        await __click('[data-sc-reveal]'); await __click('[data-sc-grade="hard"]');
        await __click('[data-sc-undo]');
        const out = {undosWhileSaving: calls('sidebar_due_review_undo').length, undoButton: previous().undo};
        save.resolve({ok: true, outcome: 'confirmed'}); await __tick(10);
        out.undos = calls('sidebar_due_review_undo'); out.word = current().word; out.previous = previous();
        return out;
        """,
        before=reviews(FIRST, SECOND),
    )
    assert result.value == {
        "undosWhileSaving": 0,
        "undoButton": False,
        "undos": [[1, 0]],
        "word": "選ぶ",
        "previous": None,
    }


def test_a_running_audiobook_owns_the_sidebar_and_its_space_key(page) -> None:
    paused = {
        "id": 5,
        "title": "Book",
        "player_running": True,
        "playing": False,
        "position": 30,
        "duration": 600,
    }
    result = page.run(
        SLOT
        + r"""
        await __tick(1100);
        const state = () => ({reviews: __visible('[data-sidebar-due]'), audio: __visible('.sidebar-audio')});
        const out = {idle: state()};
        PudgeSidebarCompanion.acceptAudioState({books: [PAUSED]});
        const space = await __key(' ', {code: 'Space'});
        out.player = {...state(), spaceTaken: space.defaultPrevented, revealed: current().answer};
        PudgeSidebarCompanion.acceptAudioState({books: [PAUSED], float_open: true});
        out.floating = state();
        PudgeSidebarCompanion.acceptAudioState({books: [{...PAUSED, player_running: false}]});
        out.stopped = state();
        return out;
        """.replace("PAUSED", json.dumps(paused)),
        before=reviews(FIRST),
        localStorage={"pudge.sidebarAudio.last.v1": "5"},  # a remembered book alone does not hide reviews
    )
    out = result.value
    assert out["idle"] == {"reviews": True, "audio": False}
    assert out["player"] == {"reviews": False, "audio": True, "spaceTaken": False, "revealed": False}
    assert out["floating"]["reviews"] is True  # the player moved to the floating window
    assert out["stopped"] == {"reviews": True, "audio": False}
    # The companion stays inside the sidebar and scrolls there; the bottom block keeps its place.
    rules = css_rules("sidebar_companion.css")
    assert (rules.get("#app>.sidebar", "min-height"), rules.get("#app>.sidebar", "overflow")) == (
        "0",
        "hidden",
    )
    assert (
        rules.get(".sidebar>.sidebar-bottom", "flex"),
        rules.get(".sidebar>.sidebar-bottom", "margin-top"),
    ) == ("0 0 auto", "8px")
    assert (rules.get(".sidebar-companion", "overflow-y"), rules.get(".sidebar-companion", "max-width")) == (
        "auto",
        "100%",
    )


def test_energy_saving_hides_the_review_slot_and_stops_prefetch_and_shortcuts(page) -> None:
    result = page.run(
        SLOT
        + r"""
        await __tick(1100);
        await __settle(PudgeSidebarCompanion.prefetch(true));
        const before = calls('sidebar_due_review_cards').length;
        __api.power_status = async () => ({mode: 'energy_saving', reasons: ['manual']});
        await __tick(120000);                                   // the power check switches energy saving on
        const space = await __key(' ', {code: 'Space'});
        return {energy: document.documentElement.classList.contains('energy-saving'), refills: calls('sidebar_due_review_cards').length - before,
                visible: __visible('[data-sidebar-due]'), word: current().word, spaceTaken: space.defaultPrevented, revealed: current().answer};
        """,
        before=reviews(FIRST, SECOND),
    )
    assert result.value == {
        "energy": True,
        "refills": 0,
        "visible": False,
        "word": "選ぶ",
        "spaceTaken": False,
        "revealed": False,
    }
    meta = {**FIRST, "frequencyRank": 100, "partsOfSpeech": ["noun"], "isLeech": True}
    revealed = page.run(
        SLOT
        + "await __tick(1100); await __click('[data-sc-reveal]'); return __text('[data-sidebar-due] .sidebar-due-answer');",
        before=reviews(meta),
    )
    assert "noun" not in revealed.value and "100" not in revealed.value  # no frequency / part-of-speech line


BOOK = {
    "id": 5,
    "title": "A Long Audiobook Title",
    "playing": True,
    "player_running": True,
    "position": 30,
    "duration": 600,
    "speed": 1.5,
    "cover_url": "covers/book.jpg",
    "chapters": [
        {"start": 0, "end": 20, "title": "One"},
        {"start": 20, "end": 40, "title": "Two"},
        {"start": 40, "end": 600, "title": "Three"},
    ],
}
# The sidebar asks sidebar_audio_state; tests change window.audioBooks between polls.
AUDIO = """
window.audioBooks = BOOKS;
__api.sidebar_audio_state = async () => ({books: window.audioBooks});
__api.audiobook_seek_to = async (id, at) => { window.audioBooks = window.audioBooks.map(b => ({...b, position: at})); return {}; };
__api.audiobook_set_speed = async (id, speed) => { window.audioBooks = window.audioBooks.map(b => ({...b, speed})); return {book: window.audioBooks[0]}; };
"""


def audio(*books: dict) -> str:
    return reviews() + AUDIO.replace("BOOKS", json.dumps(list(books)))


def test_sidebar_player_updates_in_place_with_compact_controls(page) -> None:
    result = page.run(
        r"""
        await __tick(100);
        const player = () => document.querySelector('.sidebar-audio');
        const first = player();
        const out = {text: first.innerText, cover: first.querySelector('[data-sc-audio="open"] img')?.getAttribute('src'),
                     toggle: __text('[data-sc-audio="toggle"]'), speed: __text('[data-sc-audio="speed"]'),
                     seeks: [...first.querySelectorAll('[data-sc-audio="seek"]')].map(b => b.innerText),
                     chapter: __text('[data-sc-chapter]')};
        window.audioBooks = [{...window.audioBooks[0], position: 31}];
        await __tick(1000);                                     // the next poll while playing
        out.same = player() === first && first.isConnected;
        out.position = [__text('[data-sc-position]'), __text('[data-sc-duration]')];
        await __click('.sidebar-audio [data-sc-audio="seek"][data-delta="-15"]');
        await __click('.sidebar-audio [data-sc-audio="seek"][data-delta="15"]');
        await __click('[data-sc-audio="speed"]');
        out.speeds = [...document.querySelectorAll('#sidebarSpeedMenu [data-sc-speed]')].map(b => [b.innerText, b.getAttribute('aria-checked')]);
        await __click('#sidebarSpeedMenu [data-sc-speed="2"]'); await __tick(10);
        out.speedAfter = __text('[data-sc-audio="speed"]');
        await __input('.sidebar-audio [data-sc-audio-timeline]', '120'); await __tick(10);
        out.stillSame = player() === first;
        return out;
        """,
        before=audio(BOOK),
    )
    out = result.value
    assert "A Long Audiobook Title" not in out["text"]  # compact: cover, chapter and controls only
    assert out["cover"] == "covers/book.jpg" and out["chapter"] == "Two"
    assert (out["toggle"], out["speed"], out["seeks"]) == ("Ⅱ", "1.5×", ["↶", "↷"])
    assert out["same"] is True and out["stillSame"] is True  # polling updates the player, never rebuilds it
    assert out["position"] == ["0:31", "10:00"]
    assert result.calls_of("audiobook_seek") == [[5, -15], [5, 15]]
    assert [label for label, _checked in out["speeds"]] == [
        "0.75×",
        "1×",
        "1.25×",
        "1.5×",
        "1.75×",
        "2×",
        "2.5×",
        "3×",
    ]
    assert [label for label, checked in out["speeds"] if checked == "true"] == ["1.5×"]
    assert result.calls_of("audiobook_set_speed") == [[5, 2]] and out["speedAfter"] == "2×"
    assert result.calls_of("audiobook_seek_to") == [[5, 120]]
    rules = css_rules("sidebar_companion.css")
    assert (rules.get(".sidebar-audio-cover", "width"), rules.get(".sidebar-audio-cover", "height")) == (
        "92px",
        "126px",
    )


def test_chapter_buttons_restart_the_chapter_or_go_to_the_neighbour(page) -> None:
    result = page.run(
        r"""
        await __tick(100);
        const at = async position => { window.audioBooks = [{...window.audioBooks[0], position}]; await __tick(1000); };
        await at(28); await __click('[data-sc-audio="previous-chapter"]'); await __tick(10);   // 8 s in: chapter start
        await at(24); await __click('[data-sc-audio="previous-chapter"]'); await __tick(10);   // 4 s in: previous chapter
        await __click('[data-sc-audio="next-chapter"]'); await __tick(10);
        await at(500);
        return {lastNextDisabled: $('pudgeSidebarCompanion').querySelector('[data-sc-audio="next-chapter"]').disabled};
        """,
        before=audio(BOOK),
    )
    assert result.calls_of("audiobook_seek_to") == [[5, 20], [5, 0], [5, 40]]
    assert result.value["lastNextDisabled"] is True


def test_a_starting_player_is_not_hidden_by_a_stale_stopped_sample(page) -> None:
    starting = {**BOOK, "id": 8, "playing": True, "player_running": True}
    result = page.run(
        r"""
        await __tick(100);
        PudgeSidebarCompanion.acceptAudioState({books: [STARTING]}, {optimistic: true});
        await __tick(1000);                                     // the backend has not seen the player yet
        const out = {starting: __visible('.sidebar-audio')};
        PudgeSidebarCompanion.acceptAudioState({books: []}, {rollback: true});   // the start failed
        out.rolledBack = __visible('.sidebar-audio');
        return out;
        """.replace("STARTING", json.dumps(starting)),
        before=audio(),
    )
    assert result.value == {"starting": True, "rolledBack": False}


LINKED = {**BOOK, "playing": False, "linked_light_novel": {"id": 11, "title": "Novel"}}
# An illustration and punctuation take no time in the narration: offset 2.5 is inside 丙丁.
CHAPTER = {
    "paragraphs": ["[[PUDGE_LN_IMAGE_URL:covers/one.png]]", "甲乙 丙丁、戊己"],
    "tokens": [[], [{"start": 6, "end": 8}, {"start": 0, "end": 2}, {"start": 3, "end": 5}]],
    "vocabulary": [],
}
LIVE = """
window.pairedState = {ln_chapter_index: 1, chapter_char_offset_exact: 2.5, playing: false, position: 30};
window.playingSince = 0;   // while playing, the backend position moves with the clock
__api.light_novel_paired_state = async () => ({...window.pairedState, position: window.pairedState.position
  + (window.pairedState.playing ? (Date.now() - window.playingSince) / 1000 : 0)});
__api.light_novel_chapter = async () => (CHAPTER);
""".replace("CHAPTER", json.dumps(CHAPTER, ensure_ascii=False))
LIVE_VIEW = r"""
const live = () => {
  const host = document.querySelector('[data-sc-live-text]'), word = host?.querySelector('.ln-paired-word-current');
  return {text: host?.innerText ?? null, word: word?.textContent ?? null,
          progress: word?.style.getPropertyValue('--ln-paired-word-progress') ?? null};
};
"""


def test_live_reading_text_follows_the_narration_in_the_sidebar(page) -> None:
    result = page.run(
        LIVE_VIEW
        + r"""
        await __tick(1500);
        const out = {paused: live()};
        // Playing with an anchor path: between backend samples the highlight moves frame by frame.
        const samples = () => __apiCalls.filter(call => call.name === 'light_novel_paired_state').length;
        const sampled = samples(), sample = __api.light_novel_paired_state;
        window.pairedState = {ln_chapter_index: 1, playing: true, position: 12, speed: 1,
                              anchor_window: {points: [{time: 10, offset: 0}, {time: 20, offset: 6}]}};
        window.playingSince = Date.now();
        window.audioBooks = [{...window.audioBooks[0], playing: true, position: 12}];
        while (samples() === sampled) await __tick(20);
        await __tick(20);
        let release;
        __api.light_novel_paired_state = () => new Promise(resolve => { release = () => resolve(sample()); });  // no newer sample yet
        out.playing = live();
        await __tick(1500);
        out.later = live();
        // Selecting text stops following and pauses the narration.
        __api.light_novel_paired_state = sample; release?.();
        const node = document.querySelector('[data-sc-live-text] .ln-paired-word-current').firstChild;
        const range = document.createRange(); range.setStart(node, 0); range.setEnd(node, 1);
        range.cloneContents = () => { const part = document.createDocumentFragment(); part.append(range.toString()); return part; };  // not in dom_lite
        getSelection().addRange(range);
        await __fire('[data-sc-live-text] .ln-paired-word-current', 'mouseup', {}, 'MouseEvent'); await __tick(10);
        const frozen = live().word;
        await __tick(3000);
        out.frozen = [frozen, live().word];
        return out;
        """,
        before=audio(LINKED) + LIVE,
    )
    out = result.value
    assert out["paused"] == {"text": "甲乙 丙丁、戊己", "word": "丙丁", "progress": "25.0%"}
    assert out["playing"]["word"] == "甲乙"
    assert out["later"]["word"] == "丙丁"
    assert result.calls_of("audiobook_set_paused") == [[5, True]]
    assert out["frozen"][1] == out["frozen"][0]
    assert result.calls_of("light_novel_chapter") == [[11, 1]]  # the chapter is fetched once
    rules = css_rules("sidebar_companion.css")
    assert rules.get(".sidebar-ln-live", "height") == "7.75em"


def test_live_text_shows_a_window_around_the_spoken_place_of_a_long_paragraph(page) -> None:
    long_chapter = {"paragraphs": ["あ" * 200 + "猫" + "い" * 400], "tokens": [], "vocabulary": []}
    result = page.run(
        LIVE_VIEW + "await __tick(1500); return live();",
        before=audio(LINKED)
        + LIVE.replace(
            json.dumps(CHAPTER, ensure_ascii=False), json.dumps(long_chapter, ensure_ascii=False)
        ).replace("chapter_char_offset_exact: 2.5", "chapter_char_offset_exact: 200.5"),
    )
    out = result.value
    assert out["word"] is not None and "猫" in out["word"]
    assert len(out["text"]) <= 540 and "猫" in out["text"]


def test_voiced_sound_marks_do_not_shift_the_spoken_place(page) -> None:
    # ｶﾞ (half-width + mark) and か + U+3099 are one sound each, like on the Mac reader's clock.
    voiced = {
        "paragraphs": ["ｶﾞが次"],
        "tokens": [[{"start": 0, "end": 2}, {"start": 2, "end": 4}, {"start": 4, "end": 5}]],
        "vocabulary": [],
    }
    result = page.run(
        LIVE_VIEW + "await __tick(1500); return live();",
        before=audio(LINKED)
        + LIVE.replace(json.dumps(CHAPTER, ensure_ascii=False), json.dumps(voiced, ensure_ascii=False)),
    )
    assert (result.value["word"], result.value["progress"]) == ("次", "50.0%")


def test_live_text_is_refreshed_often_only_while_the_narration_plays(page) -> None:
    result = page.run(
        r"""
        const samples = () => __apiCalls.filter(call => call.name === 'light_novel_paired_state').length;
        await __tick(2000);
        let start = samples(); await __tick(14000);
        const paused = samples() - start;
        window.pairedState = {...window.pairedState, playing: true}; window.playingSince = Date.now();
        window.audioBooks = [{...window.audioBooks[0], playing: true}];
        await __tick(2000);
        start = samples(); await __tick(14000);
        return {paused, playing: samples() - start};
        """,
        before=audio(LINKED) + LIVE,
    )
    assert result.value["paused"] <= 10 and result.value["playing"] >= 25


# Backend side of the sidebar queue.


class _Service:
    def __init__(self):
        self.undone = []
        self.submitted = []

    def strict_review_candidates(self, *, required, **_kwargs):
        assert required >= 4
        return {
            "cards": [
                {"wordId": 1, "readingIndex": 0, "wordTextPlain": "外", "sourceDeckName": "Other"},
                {"wordId": 2, "readingIndex": 0, "wordTextPlain": "古", "sourceDeckName": "My Anime"},
                {"wordId": 3, "readingIndex": 0, "wordTextPlain": "新", "sourceDeckName": "My Anime"},
            ]
        }

    def jiten_live_states(self, pairs, *, force=False):
        assert force is False
        return [
            {"wordId": 1, "readingIndex": 0, "states": ["young", "due"]},
            {"wordId": 2, "readingIndex": 0, "states": ["mature", "due"]},
            {"wordId": 3, "readingIndex": 0, "states": ["young", "due"]},
        ]

    def settings(self):
        return SimpleNamespace(review_mode="native", jiten_api_key="test-key", study_backend="jiten")

    def strict_review_submit(self, word_id, reading_index, grade, *, attempt_id):
        self.submitted.append((word_id, reading_index, grade, attempt_id))
        return {"saved": True}

    def strict_review_undo(self, word_id, reading_index):
        self.undone.append((word_id, reading_index))
        return {"undone": True}


class _DB:
    def anime_list(self):
        return [SimpleNamespace(title="My Anime", titles=["マイアニメ"], synonyms=[])]


def _api() -> tuple[WebAppApi, _Service]:
    service = _Service()
    obj = object.__new__(WebAppApi)
    obj.light_novels = service
    obj.manager = SimpleNamespace(db=_DB())
    obj.logger = SimpleNamespace(info=lambda *_a, **_kw: None)
    return obj, service


def test_sidebar_queue_prioritizes_anime_then_younger_due() -> None:
    api, _service = _api()
    result = api.sidebar_due_review_cards(3)
    assert [card["wordId"] for card in result["cards"]] == [3, 2, 1]
    assert result["cards"][0]["pudgeSidebarAnimeSource"] is True
    assert result["cards"][0]["pudgeSidebarAge"] == "newer"
    assert result["cards"][1]["pudgeSidebarAge"] == "older"


def test_sidebar_undo_is_server_side_limited_to_ten_seconds() -> None:
    api, service = _api()
    submitted = api.sidebar_due_review_submit(3, 0, "good", "attempt-1")
    assert submitted["outcome"] == "confirmed"
    undone = api.sidebar_due_review_undo(3, 0)
    assert undone["outcome"] == "undone"
    assert service.undone == [(3, 0)]

    api._sidebar_due_last_review = (3, 0, time.monotonic() - 11.0)
    expired = api.sidebar_due_review_undo(3, 0)
    assert expired["outcome"] == "expired"
    assert service.undone == [(3, 0)]


def test_sidebar_prefetch_excludes_visible_and_pending_cards() -> None:
    api, service = _api()
    seen = []
    original = service.strict_review_candidates

    def candidates(*, required, exclude_keys):
        seen.append(exclude_keys)
        return original(required=required)

    service.strict_review_candidates = candidates
    result = api.sidebar_due_review_cards(3, ["3:0", "1:0", "bad"])
    assert seen == [{"3:0", "1:0"}]
    assert [card["wordId"] for card in result["cards"]] == [2]
