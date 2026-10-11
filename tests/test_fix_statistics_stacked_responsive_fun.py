"""Statistics: daily bars split by content type, a page that fits a narrow window, fun stats.

User requests: (1) the daily bars in Statistics split into blocks by content type, the same
on the phone; (2) a narrowed window cut the filter row and the journal table off at the right;
(4) fun stats such as words read and different words.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from pudge.config import AppConfig
from pudge.consumption import ConsumptionLedger
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.database import Database
from pudge.light_novels import LightNovelService
from tests.css_support import css_rules

DAY = 86400.0


def _noon(days_ago: int, now: float) -> float:
    local = datetime.fromtimestamp(now).replace(hour=12, minute=0, second=0, microsecond=0)
    return (local - timedelta(days=days_ago)).timestamp()


def _session(ledger: ConsumptionLedger, media, kind: str, start: float, chunks, payload=None) -> None:
    """chunks: [(seconds, locator_start, locator_end)] recorded back to back."""
    session = ledger.start_session(kind=kind, media_uuid=media.media_uuid, started_at_utc=start)
    at = start
    for seconds, locator0, locator1 in chunks:
        ledger.append_interval(
            session_id=session,
            kind=kind,
            interval_start_utc=at,
            interval_end_utc=at + seconds,
            elapsed_monotonic_ms=seconds * 1000.0,
            media=[
                {
                    "media_uuid": media.media_uuid,
                    "source_revision": "r",
                    "role": "primary",
                    "locator_start": locator0,
                    "locator_end": locator1,
                }
            ],
            payload=payload or {},
        )
        at += seconds
    ledger.end_session(session, ended_at_utc=at)


def _watch(ledger, media, start: float, minutes: int) -> None:
    chunks = [
        (20.0, {"position_seconds": k * 20.0}, {"position_seconds": k * 20.0 + 20.0})
        for k in range(minutes * 3)
    ]
    _session(ledger, media, "anime", start, chunks, {"duration_seconds": 1440})


# ---------------------------------------------------------------- backend: per-day kinds, highlights


def test_days_carry_time_per_kind_and_the_overview_gets_highlights(tmp_path: Path) -> None:
    now = time.time()
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    ep1 = ledger.ensure_media(
        kind="anime", title="Odd Taxi · 1", aliases=[("stable", "e1")], source_revision="r"
    )
    ep2 = ledger.ensure_media(
        kind="anime", title="Odd Taxi · 2", aliases=[("stable", "e2")], source_revision="r"
    )
    book = ledger.ensure_media(
        kind="light_novel",
        title="Book",
        aliases=[("stable", "b")],
        source_revision="r",
        metadata={"character_count": 5000},
    )
    # Active days: 6, 5, 4 days ago (a 3-day streak) and today; the longest session is 10 min.
    _watch(ledger, ep1, _noon(6, now), 4)
    _watch(ledger, ep1, _noon(5, now), 10)
    _watch(ledger, ep2, _noon(4, now), 3)
    reading = [
        (
            30.0,
            {"chapter_key": "c", "character_offset": k * 150},
            {"chapter_key": "c", "character_offset": k * 150 + 150},
        )
        for k in range(4)
    ]
    _session(ledger, book, "light_novel", _noon(4, now) + 3600, reading, {"character_count": 5000})
    _watch(ledger, ep2, _noon(0, now) - 7200, 2)

    result = ConsumptionStatistics(db).query({"period": "7d"}, now=now)

    by_day = {row["day"]: row for row in result["days"]}
    day4 = by_day[datetime.fromtimestamp(_noon(4, now)).date().isoformat()]
    assert day4["breakdown"] == {"anime": 180.0, "light_novel": 120.0}
    assert day4["seconds"] == 300.0
    h = result["highlights"]
    assert h["longest_streak_days"] == 3
    assert h["busiest_day"] == {
        "day": datetime.fromtimestamp(_noon(5, now)).date().isoformat(),
        "seconds": 600.0,
    }
    assert h["longest_session"]["seconds"] == 600.0 and h["longest_session"]["title"] == "Odd Taxi · 1"
    assert h["top_work"] == {"title": "Odd Taxi · 1", "kind": "anime", "seconds": 840.0}
    assert h["anime_episodes"] == 2
    assert h["ln_characters_per_minute"] == 600 / 120 * 60  # 600 characters in 2 minutes
    assert h["manga_pages_per_hour"] is None
    assert h["words"] is None  # no word sources: no word stats at all

    # The format filter applies to the highlights too.
    anime = ConsumptionStatistics(db).query({"period": "7d", "kind": "anime"}, now=now)["highlights"]
    assert anime["ln_characters_per_minute"] is None and anime["anime_episodes"] == 2


# ---------------------------------------------------------------- words read in light novels

TEXT = "猫が好き。\n犬も好き。\n鳥が飛ぶ。"
PARSED = {
    "paragraphs": ["猫が好き。", "犬も好き。", "鳥が飛ぶ。"],
    "tokens": [
        [
            {"wordId": 1, "readingIndex": 0, "start": 0, "end": 1},
            {"wordId": 2, "readingIndex": 0, "start": 1, "end": 2},
            {"wordId": 3, "readingIndex": 0, "start": 2, "end": 4},
        ],
        [
            {"wordId": 4, "readingIndex": 0, "start": 0, "end": 1},
            {"wordId": 5, "readingIndex": 0, "start": 1, "end": 2},
            {"wordId": 3, "readingIndex": 0, "start": 2, "end": 4},
        ],
        [
            {"wordId": 6, "readingIndex": 0, "start": 0, "end": 1},
            {"wordId": 2, "readingIndex": 0, "start": 1, "end": 2},
            {"wordId": 7, "readingIndex": 0, "start": 2, "end": 4},
        ],
    ],
    "vocabulary": [],
}


def _ln_service(tmp_path: Path, *, parsed: bool = True) -> tuple[LightNovelService, str]:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    ln = LightNovelService(cfg)
    digest = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
    now = time.time()
    with ln._connect() as conn:
        conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,current_chapter,current_offset,finished,created_at,updated_at)"
            " VALUES('Book',?,'txt',0,0,0,?,?)",
            (str(tmp_path / "book.txt"), now, now),
        )
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(1,0,'One',?,?)",
            (TEXT, digest),
        )
        if parsed:
            conn.execute(
                "INSERT INTO ln_parse_cache(text_hash,parsed_json,parser_schema,created_at) VALUES(?,?,?,?)",
                (digest, json.dumps(PARSED, ensure_ascii=False), "jiten-v1", now),
            )
    return ln, digest


def test_chapter_words_are_placed_at_their_offsets_in_the_chapter_text(tmp_path: Path) -> None:
    ln, digest = _ln_service(tmp_path)
    assert ln.chapter_word_positions(digest) == [
        (0, 1, 0),
        (1, 2, 0),
        (2, 3, 0),
        (6, 4, 0),
        (7, 5, 0),
        (8, 3, 0),
        (12, 6, 0),
        (13, 2, 0),
        (14, 7, 0),
    ]
    assert ln.chapter_word_positions("unknown-chapter") is None
    unparsed, other = _ln_service(tmp_path / "unparsed", parsed=False)
    assert unparsed.chapter_word_positions(other) is None  # never parsed here: no network call, no count


def _read(ledger, book, start: float, chapter: str, ranges) -> None:
    chunks = [
        (
            20.0,
            {"chapter_key": chapter, "character_offset": a},
            {"chapter_key": chapter, "character_offset": b},
        )
        for a, b in ranges
    ]
    _session(ledger, book, "light_novel", start, chunks, {"character_count": len(TEXT)})


def test_words_read_different_and_new_words_come_from_the_ranges_read(tmp_path: Path) -> None:
    ln, digest = _ln_service(tmp_path)
    now = time.time()
    db = Database(tmp_path / "stats.sqlite3")
    ledger = ConsumptionLedger(db)
    book = ledger.ensure_media(
        kind="light_novel", title="Book", aliases=[("stable", "b")], source_revision="r"
    )
    other = ledger.ensure_media(
        kind="light_novel", title="Other", aliases=[("stable", "o")], source_revision="r"
    )
    # 20 days ago: the first paragraph (words 1, 2, 3).
    _read(ledger, book, _noon(20, now), digest, [(0, 2), (2, 4), (4, 6), (6, 6)])
    # This week: up to the third paragraph's second word, then a passage re-read.
    _read(ledger, book, _noon(2, now), digest, [(0, 4), (4, 8), (8, 13), (6, 10)])
    stats = ConsumptionStatistics(db, ln_words=ln.chapter_word_positions)

    week = stats.query({"period": "7d"}, now=now)["highlights"]["words"]
    # Offsets 0,1,2,6,7,8,12 are inside what was read (the re-read counts once).
    assert week == {
        "total": 7,
        "unique": 6,
        "new": 3,
        "by_kind": {"light_novel": {"total": 7, "unique": 6, "new": 3}},
        "pending": 0,
        "missing": 0,
    }
    everything = stats.query({"period": "all"}, now=now)["highlights"]["words"]
    assert everything["total"] == 7 and everything["new"] == 6
    # Filtered to the book, "new" still means new to all earlier reading.
    filtered = stats.query({"period": "7d", "media_uuid": book.media_uuid}, now=now)["highlights"]["words"]
    assert filtered["new"] == 3
    assert (
        stats.query({"period": "7d", "media_uuid": other.media_uuid}, now=now)["highlights"]["words"] is None
    )

    # A chapter whose words were never parsed is queued for the background job, not guessed.
    _read(ledger, book, _noon(1, now), "f" * 64, [(0, 40), (40, 80), (80, 120)])
    pokes = []
    stats = ConsumptionStatistics(
        db, ln_words=ln.chapter_word_positions, on_words_pending=lambda: pokes.append(1)
    )
    week = stats.query({"period": "7d"}, now=now)["highlights"]["words"]
    assert week["pending"] == 1 and week["total"] == 7 and pokes == [1]
    assert stats.word_units.due() == [
        {
            "key": "ln:" + "f" * 64,
            "kind": "light_novel",
            "status": "pending",
            "attempts": 0,
            "source": {"chapter_key": "f" * 64},
        }
    ]


# ---------------------------------------------------------------- the page: stacked bars and legend


def _payload(**extra):
    days = [
        {"day": "2026-09-29", "seconds": 0.0, "breakdown": {}},
        {"day": "2026-09-30", "seconds": 3600.0, "breakdown": {"light_novel": 900.0, "anime": 2700.0}},
        {
            "day": "2026-10-01",
            "seconds": 1800.0,
            "breakdown": {"manga": 600.0, "simultaneous": 300.0, "visual_novel": 900.0},
        },
    ]
    payload = {
        "schema": 1,
        "timezone": {"label": "UTC"},
        "summary": {"total_seconds": 5400, "active_days": 2, "sessions": 3, "works": 2},
        "breakdown": {
            "anime": 2700.0,
            "light_novel": 900.0,
            "visual_novel": 900.0,
            "manga": 600.0,
            "simultaneous": 300.0,
        },
        "volume": {"by_kind": {}, "by_media": {}},
        "days": days,
        "works": [],
        "journal": [],
        "journal_total": 0,
        "media_options": [],
        "highlights": None,
    }
    payload.update(extra)
    return payload


OPEN = r"""
__api.consumption_statistics_query = async () => (PAYLOAD);
setPage('statistics', true); for (let i = 0; i < 6; i++) await __tick(50);
"""


def _open(page, payload, code, **options):
    return page.run(OPEN.replace("PAYLOAD", json.dumps(payload)) + code, **options)


def test_each_day_bar_is_split_into_blocks_by_content_type_with_a_matching_legend(page) -> None:
    result = _open(
        page,
        _payload(),
        r"""
        const bars = [...document.querySelectorAll('#statsBody .stats-bar')];
        const out = bars.map(bar => ({
          day: bar.dataset.statsDay, height: bar.style.height, label: bar.getAttribute('aria-label'),
          segments: [...bar.querySelectorAll('.stats-seg')].map(s => [s.dataset.statsKind, s.style.height, s.getAttribute('title')]),
        }));
        const legend = [...document.querySelectorAll('#statsBody .stats-legend .stats-chip')].map(c => [c.dataset.statsKind, c.innerText]);
        await __click('.stats-bar[data-stats-day="2026-10-01"] .stats-seg');
        for (let i = 0; i < 4; i++) await __tick(50);
        return {out, legend, tab: document.querySelector('[data-stats-tab].active')?.dataset.statsTab};
        """,
    )
    out = result.value
    assert out["out"][0]["segments"] == [] and out["out"][0]["height"] == "2%"
    # Bottom to top in one fixed order of types; each block has its share of the day.
    assert out["out"][1]["height"] == "100%"
    assert out["out"][1]["segments"] == [
        ["anime", "75.00%", "Anime · 45m 0s · 2026-09-30"],
        ["light_novel", "25.00%", "Light novel · 15m 0s · 2026-09-30"],
    ]
    assert [s[0] for s in out["out"][2]["segments"]] == ["manga", "visual_novel", "simultaneous"]
    assert out["out"][2]["height"] == "50%"
    assert (
        out["out"][2]["label"]
        == "2026-10-01 · 30m 0s, Manga: 10m 0s, Visual novel: 15m 0s, Simultaneous: 5m 0s"
    )
    # The time chips are the legend: same types, same order as the stacks.
    assert out["legend"] == [
        ["anime", "Anime: 45m 0s"],
        ["light_novel", "Light novel: 15m 0s"],
        ["manga", "Manga: 10m 0s"],
        ["visual_novel", "Visual novel: 15m 0s"],
        ["simultaneous", "Simultaneous: 5m 0s"],
    ]
    assert out["tab"] == "journal"  # a block opens that day's journal like the whole bar did
    calls = result.calls_of("consumption_statistics_query")
    assert calls[-1][0]["start_date"] == "2026-10-01"


def test_each_content_type_has_its_own_colour_shared_by_blocks_and_chips() -> None:
    rules = css_rules("statistics.css")
    kinds = [
        "anime",
        "light_novel",
        "manga",
        "audiobook",
        "visual_novel",
        "light_novel+audiobook",
        "simultaneous",
        "review",
    ]
    colours = [rules.get(f'[data-stats-kind="{kind}"]', "--stats-kind") for kind in kinds]
    assert all(colours) and len(set(colours)) == len(kinds)
    assert rules.get("[data-stats-kind]", "--stats-kind")  # anything else is grey, not invisible
    assert rules.get(".stats-seg", "background") == "var(--stats-kind,#8a9bb3)"
    assert rules.get(".stats-swatch", "background") == "var(--stats-kind,#8a9bb3)"


# ---------------------------------------------------------------- the page: fun stats


HIGHLIGHTS = {
    "longest_streak_days": 4,
    "busiest_day": {"day": "2026-09-30", "seconds": 3600.0},
    "longest_session": {"seconds": 2700.0, "title": "Odd Taxi · 1", "kind": "anime"},
    "top_work": {"title": "Odd Taxi", "kind": "anime", "seconds": 4000.0},
    "anime_episodes": 3,
    "ln_characters_per_minute": 212.4,
    "manga_pages_per_hour": None,
    "words": {
        "total": 12345,
        "unique": 2345,
        "new": 321,
        "by_kind": {
            "manga": {"total": 345, "unique": 300, "new": 21},
            "anime": {"total": 12000, "unique": 2200, "new": 300},
        },
        "pending": 4,
        "missing": 1,
        "job": {"running": False, "paused": "energy_saving"},
    },
}
FUN = r"""
return [...document.querySelectorAll('#statsBody .stats-highlight')].map(card =>
  [...card.children].map(child => child.innerText));
"""


def test_overview_shows_fun_stats_cards_and_labels_estimates(page) -> None:
    en = _open(page, _payload(highlights=HIGHLIGHTS), FUN).value
    assert en == [
        ["Longest streak", "4 days", "active days in a row"],
        ["Busiest day", "1h 0m", "2026-09-30"],
        ["Longest session", "45m 0s", "Odd Taxi · 1 · Anime"],
        ["Most time", "Odd Taxi", "Anime · 1h 6m"],
        ["Anime episodes", "3", "with watched time"],
        ["Reading speed, light novels", "≈ 212 chars/min", "estimate"],
        [
            "Words met",
            "≈ 12,345",
            "estimate: subtitles watched, pages and lines read · still counting 4 items"
            " · counting waits while energy saving is on · 1 without text yet (no subtitles or OCR)",
        ],
        ["Different words", "≈ 2,345", "estimate"],
        ["New words met", "≈ 321", "not met in earlier reading"],
        [
            "Words by type",
            "Anime: ≈ 12,000 · 2,200 different · 300 new",
            "Manga: ≈ 345 · 300 different · 21 new",
        ],
    ]
    ru = _open(page, _payload(highlights=HIGHLIGHTS), FUN, lang="ru").value
    assert ru[0] == ["Самая длинная серия", "4 дня", "активных дней подряд"]
    assert ru[6][0] == "Встречено слов" and "ещё считается 4 элемента" in ru[6][2]
    # Numbers follow the interface language, not the Mac's region (12,000 in English, 12 000 in Russian).
    assert ru[9][1] == "Аниме: ≈ 12\u00a0000 · разных 2\u00a0200 · новых 300"
    # Nothing parsed yet: a dash and why, no made-up numbers.
    words = {"total": None, "unique": None, "new": None, "by_kind": {}, "pending": 2, "missing": 0}
    none = _open(page, _payload(highlights={"longest_streak_days": 0, "words": words}), FUN).value
    assert none == [
        ["Words met", "—", "estimate: subtitles watched, pages and lines read · still counting 2 items"]
    ]
    assert _open(page, _payload(), FUN).value == []


# ---------------------------------------------------------------- the page: narrow window


JOURNAL = r"""
await __click('[data-stats-tab="journal"]'); await __tick(50);
return {
  head: __visible('#statsBody .stats-table thead'),
  labels: [...document.querySelectorAll('#statsBody .stats-table tbody td')].map(td => td.dataset.label || ''),
  title: document.querySelector('#statsBody td.stats-title')?.innerText,
};
"""


def test_a_narrow_window_shows_journal_rows_as_labelled_cards(page) -> None:
    row = {
        "id": "s1",
        "type": "session",
        "start_utc": 1_790_000_000,
        "end_utc": 1_790_000_600,
        "seconds": 600,
        "kind": "anime",
        "title": "Help me understand how a candidate cheated in a test",
        "origin": "automatic",
        "estimated": False,
        "device": "MacBook",
        "excluded": False,
    }
    payload = _payload(journal=[row], journal_total=1)
    wide = _open(page, payload, JOURNAL, viewport={"width": 1300, "height": 900}).value
    narrow = _open(page, payload, JOURNAL, viewport={"width": 900, "height": 900}).value
    assert wide["head"] is True and narrow["head"] is False
    assert narrow["labels"] == ["Start", "End", "Time", "", "Format", "Source", "Device", ""]
    assert narrow["title"] == row["title"]


def test_statistics_never_grows_wider_than_the_window() -> None:
    rules = css_rules("statistics.css")
    # One column that may shrink below its content: wide pieces scroll or wrap inside it.
    assert rules.get(".stats-shell", "grid-template-columns") == "minmax(0,1fr)"
    assert rules.get(".stats-shell>*", "min-width") == "0"
    for selector in (".stats-filterbar", ".stats-periods", ".stats-tabs", ".stats-breakdown"):
        assert rules.get(selector, "flex-wrap") == "wrap", selector
    assert (
        rules.get(".stats-cards", "grid-template-columns") == "repeat(auto-fit,minmax(min(170px,100%),1fr))"
    )
    assert rules.get(".stats-panel", "overflow") == "auto"
    assert rules.get(".stats-chart", "overflow-x") == "auto"
    # Table cells wrap (the window's tables keep cells on one line).
    assert rules.get(".stats-table td", "white-space") == "normal"
    narrow = "(max-width:1000px)"
    assert rules.get(".stats-table thead", "display", media=narrow) == "none"
    assert rules.get(".stats-table tr", "display", media=narrow) == "grid"
    assert rules.get(".stats-table td[data-label]::before", "content", media=narrow) == "attr(data-label)"
