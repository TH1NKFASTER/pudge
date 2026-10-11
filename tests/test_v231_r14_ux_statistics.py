from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pudge.consumption import ConsumptionLedger
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.database import Database
from study_page_support import LN_BOOK
from tests.css_support import css_rules


def _record_session(
    tmp_path: Path,
    *,
    kind: str,
    media_kind: str,
    method: str,
    seconds: float,
) -> tuple[ConsumptionStatistics, str]:
    db = Database(tmp_path / f"{kind}-{int(seconds)}.sqlite3")
    ledger = ConsumptionLedger(db)
    media = ledger.ensure_media(
        kind=media_kind,
        title=f"R14 {kind}",
        aliases=[("stable", f"r14:{kind}:{seconds}")],
        source_revision="r14-test",
        metadata={"page_count": 10, "duration_seconds": 1200, "character_count": 1000},
    )
    start = 1_700_000_000.0
    session = ledger.start_session(
        kind=kind,
        media_uuid=media.media_uuid,
        measurement_method=method,
        started_at_utc=start,
    )
    remaining = float(seconds)
    offset = 0.0
    while remaining > 1e-6:
        chunk = min(13.0, remaining)
        if kind == "manga":
            locator0 = locator1 = {"page_id": "p1", "page_index": 0}
            payload = {"page_count": 10}
        elif kind == "light_novel":
            locator0 = {"chapter_key": "c1", "character_offset": int(offset)}
            locator1 = {"chapter_key": "c1", "character_offset": int(offset + chunk)}
            payload = {"character_count": 1000}
        else:
            locator0 = {"position_seconds": offset}
            locator1 = {"position_seconds": offset + chunk}
            payload = {"duration_seconds": 1200, "seek_or_discontinuity": False}
        ledger.append_interval(
            session_id=session,
            kind=kind,
            interval_start_utc=start + offset,
            interval_end_utc=start + offset + chunk,
            elapsed_monotonic_ms=chunk * 1000.0,
            media=[
                {
                    "media_uuid": media.media_uuid,
                    "source_revision": "r14-test",
                    "role": "primary",
                    "locator_start": locator0,
                    "locator_end": locator1,
                }
            ],
            payload=payload,
        )
        offset += chunk
        remaining -= chunk
    ledger.end_session(session, ended_at_utc=start + seconds)
    return ConsumptionStatistics(db), media.media_uuid


def test_short_automatic_manga_session_is_absent_from_time_journal_and_volume(tmp_path: Path) -> None:
    stats, media_uuid = _record_session(
        tmp_path,
        kind="manga",
        media_kind="manga",
        method="reader_visible_heartbeat",
        seconds=15,
    )
    result = stats.query({"period": "all"}, now=1_700_001_000)
    assert result["summary"]["total_seconds"] == pytest.approx(0)
    assert result["summary"]["sessions"] == 0
    assert result["journal"] == []
    assert media_uuid not in result["volume"]["by_media"]


def test_one_minute_automatic_manga_session_counts_normally(tmp_path: Path) -> None:
    stats, media_uuid = _record_session(
        tmp_path,
        kind="manga",
        media_kind="manga",
        method="reader_visible_heartbeat",
        seconds=60,
    )
    result = stats.query({"period": "all"}, now=1_700_001_000)
    assert result["summary"]["total_seconds"] == pytest.approx(60)
    assert result["summary"]["sessions"] == 1
    assert len(result["journal"]) == 1
    assert media_uuid in result["volume"]["by_media"]


def test_short_anime_session_remains_counted(tmp_path: Path) -> None:
    stats, _media_uuid = _record_session(
        tmp_path,
        kind="anime",
        media_kind="anime_episode",
        method="bounded_active_chunks",
        seconds=15,
    )
    result = stats.query({"period": "all"}, now=1_700_001_000)
    assert result["summary"]["total_seconds"] == pytest.approx(15)
    assert result["summary"]["sessions"] == 1


def _recorded_by_the_app(
    tmp_path: Path, recorder: str, seconds: float
) -> tuple[ConsumptionStatistics, ConsumptionLedger, list]:
    """Activity written the way the app's own recorders write it."""
    db = Database(tmp_path / f"{recorder}-{int(seconds)}.sqlite3")
    ledger = ConsumptionLedger(db)
    source = tmp_path / f"{recorder}.bin"
    source.write_bytes(b"synthetic")
    with db.connect() as conn:
        manga = conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,read_pages,reading_direction,source_fingerprint,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (str(source), "Volume", 10, 1, 1, "rtl", "fp", 1.0, 1.0),
        ).lastrowid
        audio = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,?,?,?)",
            (str(source), "Book", 1200.0, 1.0, 1.0),
        ).lastrowid
    if recorder == "manga reader":
        events = ledger.record_reader_activity(
            kind="manga",
            local_id=manga,
            active_seconds=seconds,
            locator_start={"page_index": 0},
            locator_end={"page_index": 1},
        )
    elif recorder == "audiobook player":
        events = ledger.record_audiobook_activity(
            book_id=audio, active_seconds=seconds, position_start=0.0, position_end=seconds
        )
    else:
        capture = ledger.begin_vn_capture(title="Game", executable_hint="game", window_id=5, generation=1)
        events = ledger.record_vn_runtime(capture["session_id"], active_seconds=seconds, capture_count=3)
        ledger.end_vn_capture(capture["session_id"])
    return ConsumptionStatistics(db), ledger, events


@pytest.mark.parametrize("recorder", ["manga reader", "audiobook player", "visual novel capture"])
def test_app_recorders_leave_out_sessions_under_a_minute(tmp_path: Path, recorder: str) -> None:
    # A reader opened for a few seconds, a short listen, a short capture: noise, not activity.
    short, _ledger, _events = _recorded_by_the_app(tmp_path, recorder, 15)
    result = short.query({"period": "all"}, now=time.time() + 5)
    assert (result["summary"]["total_seconds"], result["summary"]["sessions"], result["journal"]) == (
        0,
        0,
        [],
    )
    assert result["volume"]["by_media"] == {}
    # A minute counts.
    minute, _ledger, _events = _recorded_by_the_app(tmp_path, recorder, 60)
    result = minute.query({"period": "all"}, now=time.time() + 5)
    assert result["summary"]["total_seconds"] == pytest.approx(60)
    assert result["summary"]["sessions"] == 1


def test_short_session_that_the_user_corrected_counts(tmp_path: Path) -> None:
    stats, _ledger, [event] = _recorded_by_the_app(tmp_path, "manga reader", 15)
    stats.correction(
        target_type="event", target_id=event.event_id, duration_seconds=15, reason="I did read it"
    )
    result = stats.query({"period": "all"}, now=time.time() + 5)
    assert result["summary"]["total_seconds"] == pytest.approx(15)
    assert result["summary"]["sessions"] == 1


def test_statistics_chart_spreads_a_short_period_over_the_whole_width(page, tmp_path: Path) -> None:
    # Bars had a fixed max-width of 54 px: a 7-day chart filled a small part of the panel.
    stats, _ledger, _events = _recorded_by_the_app(tmp_path, "manga reader", 60)
    now = time.time()
    payloads = {
        period: json.loads(json.dumps(stats.query({"period": period}, now=now), default=str))
        for period in ("7d", "30d")
    }
    result = page.run(
        r"""
        __api.consumption_statistics_query = async scope => (PAYLOADS)[scope.period];
        const chart = () => { const el = document.querySelector('#statsBody .stats-chart');
          return {columns: el.style.getPropertyValue('--stats-columns'), bars: el.querySelectorAll('.stats-bar-wrap .stats-bar').length}; };
        setPage('statistics', true); await __tick(50);
        const month = chart();
        await __click('[data-stats-period="7d"]'); await __tick(50);
        return {month, week: chart()};
        """.replace("PAYLOADS", json.dumps(payloads)),
        now=now * 1000,
    )
    out = result.value
    assert out["month"] == {"columns": "30", "bars": 30}
    assert out["week"] == {"columns": "7", "bars": 7}
    rules = css_rules("statistics.css")
    assert rules.get(".stats-chart", "display") == "grid"
    assert (
        rules.get(".stats-chart", "grid-template-columns")
        == "repeat(var(--stats-columns,1),minmax(20px,1fr))"
    )
    for selector in (".stats-bar-wrap", ".stats-bar"):
        assert rules.get(selector, "width") == "100%" and rules.get(selector, "max-width") is None, selector
    assert rules.get(".stats-bar-wrap", "min-width") == "20px"


# Light novel 6 open in the reader with a Jiten key (tests/study_page_support.py LN_BOOK).
LN_READER = {
    "before": LN_BOOK,
    "state": {"settings": {"review_gate_ln_enabled": False}},
    "api": {
        "light_novel_state": {"version": 1, "settings": {"jiten_api_key": "key"}, "books": []},
        "study_decks": [{"id": 12, "name": "Deck A"}, {"id": 13, "name": "Deck B"}],
    },
}
OPEN_READER = r"""
await __settle(openLightNovel(6)); await __tick(300);
const cardOpen = () => document.getElementById('pudgeStudyCard')?.classList.contains('open') || false;
const openCard = async selector => { await __click(selector); await __tick(100); };
"""


def test_choosing_a_study_deck_keeps_the_word_card_open_and_preselects_it_next_time(page) -> None:
    # The deck list (a PudgeSelect menu under <body>, outside the card) used to count as a
    # click outside the card: picking a deck closed the card.
    result = page.run(
        OPEN_READER
        + r"""
        await openCard('#lnReader [data-ln-token="0:0"]');
        const shell = $('pudgeStudyDeck').closest('.pudge-select');
        await __fire('#pudgeStudyCard .pudge-select:has(#pudgeStudyDeck) .pudge-select-button', 'pointerdown', {button: 0}, 'PointerEvent');
        const menu = [...document.querySelectorAll('.pudge-select-menu.open')];
        const option = menu.flatMap(m => [...m.querySelectorAll('.pudge-select-option')]).find(b => b.textContent === 'Deck B');
        option.click(); await __tick(20);
        const out = {menuUnderCard: Boolean(shell && menu.length && !menu[0].closest('#pudgeStudyCard')),
                     open: cardOpen(), deck: $('pudgeStudyDeck').value, menuOpen: document.querySelectorAll('.pudge-select-menu.open').length};
        PudgeReadingTools.study.close(); await __tick(20);
        await openCard('#lnReader [data-ln-token="1:0"]');                 // another word: the same deck
        out.next = {open: cardOpen(), deck: $('pudgeStudyDeck').value};
        return out;
        """,
        **LN_READER,
    )
    out = result.value
    assert out["menuUnderCard"] is True
    assert (out["open"], out["deck"], out["menuOpen"]) == (True, "13", 0)
    assert out["next"] == {"open": True, "deck": "13"}
    assert result.calls_of("set_study_deck_preference") == [["jiten", "13"]]


def test_double_and_triple_click_on_words_do_not_select_them(page) -> None:
    # A double/triple click on reader words selected a word/paragraph (WebKit's own selection)
    # instead of just toggling the word card; a single press still starts a drag selection.
    result = page.run(
        OPEN_READER
        + r"""
        const press = (selector, init) => { const event = new MouseEvent('mousedown', {bubbles: true, cancelable: true, button: 0, ...init});
          document.querySelector(selector).dispatchEvent(event); return event.defaultPrevented; };
        const word = '#lnReader [data-ln-token="0:0"]';
        const out = {single: press(word, {detail: 1}), double: press(word, {detail: 2}), triple: press(word, {detail: 3}),
                     right: press(word, {detail: 2, button: 2}), toolbar: press('#lnReaderClose', {detail: 2})};
        const range = document.createRange(); range.selectNodeContents(document.querySelector(word));
        getSelection().removeAllRanges(); getSelection().addRange(range);
        const dbl = new MouseEvent('dblclick', {bubbles: true, cancelable: true, detail: 2});
        document.querySelector(word).dispatchEvent(dbl);
        out.dblclick = {prevented: dbl.defaultPrevented, selected: !getSelection().isCollapsed};
        return out;
        """,
        **LN_READER,
    )
    assert result.value == {
        "single": False,
        "double": True,
        "triple": True,
        "right": False,
        "toolbar": False,
        "dblclick": {"prevented": True, "selected": False},
    }


# Home: an episode to continue, a new downloaded episode (both play on click) and one that
# waits for its release (opens AniList).
HOME = {
    "home": {
        "continue_watching": [
            {
                "media_id": 1,
                "title": "Continue",
                "episode": 2,
                "video_path": "/v/continue.mkv",
                "resume_start": 60,
                "cover": "/covers/continue.jpg",
            }
        ],
        "new_ready": [
            {
                "media_id": 2,
                "title": "Ready",
                "next_episode": 3,
                "cover": "/covers/ready.jpg",
                "local": {"video_path": "/v/ready.mkv", "episode": 3, "state": "ready"},
            }
        ],
        "waiting": [
            {
                "media_id": 3,
                "title": "Waiting",
                "next_episode": 4,
                "cover": "/covers/waiting.jpg",
                "site_url": "https://anilist.co/anime/3",
            }
        ],
    }
}


def test_mouse_drag_on_a_playable_anime_card_never_opens_the_cover_preview(page) -> None:
    # Pressing a playable card with a slightly moving mouse turned the click into a cover
    # preview. The trackpad pinch still opens the preview on any cover.
    result = page.run(
        r"""
        setPage('current', true); renderCurrent(); await __tick(50);
        const covers = {continue: '[data-media-id="1"] .airing-cover-shell img', ready: '[data-media-id="2"] .airing-cover-shell img',
                        waiting: '[data-media-id="3"] .airing-cover-shell img'};
        const drag = async selector => {
          document.querySelector(selector).__rect = {x: 100, y: 100, width: 120, height: 170};
          const at = (type, x) => { const event = new PointerEvent(type, {bubbles: true, cancelable: true, button: 0, pointerId: 3, pointerType: 'mouse', clientX: x, clientY: 150});
            document.querySelector(selector).dispatchEvent(event); return event.defaultPrevented; };
          const pressPrevented = at('pointerdown', 150);
          for (const x of [170, 220, 330]) at('pointermove', x);
          const opened = PudgeCoverPreview.isOpen(), peeks = document.querySelectorAll('.pudge-cover-peek').length;
          at('pointerup', 330); await __tick(400);
          PudgeCoverPreview.close(); await __tick(400);
          return {pressPrevented, opened, peeks};
        };
        const pinch = async selector => {
          for (const [type, scale] of [['gesturestart', 1], ['gesturechange', 1.5], ['gestureend', 1.5]]) {
            const event = new Event(type, {bubbles: true, cancelable: true}); Object.assign(event, {scale, clientX: 150, clientY: 150});
            document.querySelector(selector).dispatchEvent(event);
          }
          const opened = PudgeCoverPreview.isOpen(); PudgeCoverPreview.close(); await __tick(400); return opened;
        };
        const out = {};
        for (const [name, selector] of Object.entries(covers)) out[name] = {drag: await drag(selector), pinch: await pinch(selector)};
        return out;
        """,
        state=HOME,
    )
    out = result.value
    for card in ("continue", "ready"):
        assert out[card] == {"drag": {"pressPrevented": False, "opened": False, "peeks": 0}, "pinch": True}, (
            card
        )
    assert out["waiting"] == {"drag": {"pressPrevented": True, "opened": True, "peeks": 0}, "pinch": True}
    # The press on the episode to continue still started it.
    assert [call[0] for call in result.calls_of("play")] == ["/v/continue.mkv"]


AUDIOBOOKS = {
    "audiobook_state": {
        "books": [
            {"id": 4, "title": "Audio", "duration": 600, "cover_url": "/covers/a.jpg"},
            {"id": 5, "title": "Other", "duration": 600},
        ]
    }
}
ESCAPE = r"""
setPage('audiobooks', true); await __tick(300);
const selected = () => PudgeAudiobookSelection.selectedBookIds().length;
const surfaces = () => ({cards: selected(), menu: document.querySelectorAll('.pudge-select-menu.open').length, preview: PudgeCoverPreview.isOpen()});
const escape = type => window.dispatchEvent(new KeyboardEvent(type, {key: 'Escape', code: 'Escape', bubbles: true, cancelable: true}));
const openSurfaces = async () => {
  await __key('a', {metaKey: true}, 'body');                                  // Cmd+A: every card selected
  await __fire('[data-audiobook-id="4"] .pudge-select:has([data-audio-speed]) .pudge-select-button', 'pointerdown', {button: 0}, 'PointerEvent');
  PudgeCoverPreview.openSource('/covers/a.jpg'); await __tick(50);
};
const logged = () => __apiCalls.filter(call => call.name === 'log_ui_event' && call.args[0] === 'escape.selection_clear').map(call => call.args[1]);
"""


def test_escape_leaves_card_selection_before_closing_menus_and_previews(page) -> None:
    # Cmd+A then Escape sometimes did nothing visible: an open dropdown or cover preview took
    # the Escape and the cards stayed selected.
    result = page.run(
        ESCAPE
        + r"""
        await openSurfaces();
        const steps = [surfaces()];
        for (let k = 0; k < 3; k++) { escape('keydown'); escape('keyup'); await __tick(50); steps.push(surfaces()); }
        return {steps, logged: logged()};
        """,
        api=AUDIOBOOKS,
    )
    assert result.value["steps"] == [
        {"cards": 2, "menu": 1, "preview": True},
        {"cards": 0, "menu": 1, "preview": True},  # first: the card selection
        {"cards": 0, "menu": 0, "preview": True},  # then the dropdown
        {"cards": 0, "menu": 0, "preview": False},  # then the preview
    ]
    assert result.value["logged"] == [{"page": "audiobooks", "count": 2, "source": "keydown"}]


def test_escape_release_clears_card_selection_when_webkit_swallowed_the_press(page) -> None:
    result = page.run(
        ESCAPE
        + r"""
        await openSurfaces();
        escape('keyup'); await __tick(50);                                          // no keydown reached the page
        return {after: surfaces(), logged: logged()};
        """,
        api=AUDIOBOOKS,
    )
    assert result.value == {
        "after": {"cards": 0, "menu": 1, "preview": True},
        "logged": [{"page": "audiobooks", "count": 2, "source": "keyup"}],
    }
