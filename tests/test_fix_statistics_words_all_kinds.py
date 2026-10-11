"""Statistics words for anime, manga and visual novels too, and the fun stats on the iPhone.

User: "Of course, what's the problem? We have all the data — just put it into the Jiten parser."
Anime counts the subtitle lines shown while watching (each line once per watch), manga the
text boxes of the pages looked at, visual novels the lines the capture read. Parsing runs in
a background job (only when background work is allowed); Statistics only reads its results
and says how many items are still being counted.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import MethodType, SimpleNamespace

from pudge.consumption import ConsumptionLedger
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.consumption_words import WordUnits
from pudge.database import Database
from pudge.web_app import WebAppApi


def _noon(days_ago: int, now: float) -> float:
    local = datetime.fromtimestamp(now).replace(hour=12, minute=0, second=0, microsecond=0)
    return (local - timedelta(days=days_ago)).timestamp()


def _session(ledger, media, kind: str, start: float, chunks, payload=None) -> str:
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
    return session


def _watch(ledger, episode, start: float, positions: tuple[float, float]) -> None:
    a, b = positions
    chunks = []
    while a < b:
        chunks.append((10.0, {"position_seconds": a}, {"position_seconds": min(b, a + 10.0)}))
        a += 10.0
    _session(ledger, episode, "anime", start, chunks, {"duration_seconds": 1440})


def _look(ledger, volume, start: float, pages) -> None:
    chunks = [
        (
            20.0,
            {"page_index": index, "page_id": f"{index:03}.jpg"},
            {"page_index": index, "page_id": f"{index:03}.jpg"},
        )
        for index in pages
    ]
    _session(ledger, volume, "manga", start, chunks, {"page_count": 10})


# Subtitle lines of the episode: (start, end, words)
CUES = [
    [0.0, 20.0, [[1, 0], [2, 0]]],
    [30.0, 50.0, [[3, 0]]],
    [70.0, 90.0, [[4, 0], [1, 0]]],
    [200.0, 210.0, [[9, 0]]],
]


def _db(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    episode = ledger.ensure_media(
        kind="anime_episode", title="Odd Taxi · 1", aliases=[("video_path", str(tmp_path / "ep1.mkv"))]
    )
    volume = ledger.ensure_media(
        kind="manga",
        title="Vol 1",
        aliases=[("local", "5")],
        current_library_kind="manga",
        current_library_id="5",
    )
    return db, ledger, episode, volume


def test_anime_manga_and_vn_words_are_counted_per_watch_and_per_kind(tmp_path: Path) -> None:
    now = time.time()
    db, ledger, episode, volume = _db(tmp_path)
    units = WordUnits(db)
    # Last month: the first line of the episode (words 1 and 2).
    _watch(ledger, episode, _noon(30, now), (0.0, 20.0))
    # This week: watched 0–60 s (lines 1 and 2), then again 40–80 s (lines 2 and 3).
    _watch(ledger, episode, _noon(3, now), (0.0, 60.0))
    _watch(ledger, episode, _noon(2, now), (40.0, 80.0))
    # Manga: pages 1 and 2 (page 1 twice in one session counts once), page 2 again next day.
    _look(ledger, volume, _noon(2, now), [1, 2, 1])
    _look(ledger, volume, _noon(1, now), [2])
    # VN: a capture session that read two lines (one shown twice).
    vn = ledger.begin_vn_capture(title="Manosaba", executable_hint="", window_id=1, generation=1)
    ledger.record_vn_runtime(vn["session_id"], active_seconds=90)
    units.record_vn_lines(
        vn["session_id"],
        [
            {"id": 1, "text": "こんにちは", "created_at": now - 60},
            {"id": 2, "text": "猫だ", "created_at": now - 50},
            {"id": 3, "text": "猫だ", "created_at": now - 40},
        ],
    )
    ledger.end_vn_capture(vn["session_id"])
    pokes: list[int] = []
    stats = ConsumptionStatistics(db, count_words=True, on_words_pending=lambda: pokes.append(1))

    # Nothing parsed yet: everything is queued for the background job and reported.
    first = stats.query({"period": "7d"}, now=now + 5)["highlights"]["words"]
    assert first["total"] is None and first["pending"] == 5 and pokes == [1]
    assert sorted(unit["key"].split(":")[0] for unit in units.due(50)) == [
        "anime",
        "manga",
        "manga",
        "vn",
        "vn",
    ]

    units.store(f"anime:{episode.media_uuid}", {"cues": CUES})
    units.store(f"manga:{volume.media_uuid}:001.jpg", {"words": [[1, 0], [5, 0]]})
    units.store(f"manga:{volume.media_uuid}:002.jpg", {"words": [[6, 0]]})
    for unit in units.due(50):
        if unit["kind"] == "visual_novel":
            words = [[7, 0]] if unit["source"]["text"] == "こんにちは" else [[8, 0], [1, 0]]
            units.store(unit["key"], {"words": words})

    week = stats.query({"period": "7d"}, now=now + 5)["highlights"]["words"]
    # Anime: session 1 lines 1+2 (3 words), session 2 lines 2+3 (3 words); words 1,2,3,4.
    # Manga: session 1 pages 1+2 (3 words), session 2 page 2 (1 word); words 1,5,6.
    # VN: two different lines (3 words); words 7,8,1.
    assert week["by_kind"] == {
        "anime": {"total": 6, "unique": 4, "new": 2},
        "manga": {"total": 4, "unique": 3, "new": 2},
        "visual_novel": {"total": 3, "unique": 3, "new": 2},
    }
    assert week["total"] == 13 and week["unique"] == 8 and week["pending"] == 0
    # Words 1 and 2 were met last month; everything else is new this week.
    assert week["new"] == 6
    everything = stats.query({"period": "all"}, now=now + 5)["highlights"]["words"]
    assert everything["by_kind"]["anime"] == {"total": 8, "unique": 4, "new": 4}
    assert everything["new"] == everything["unique"] == 8
    # Filters apply; "new" still compares with everything met before.
    manga = stats.query({"period": "7d", "kind": "manga"}, now=now + 5)["highlights"]["words"]
    assert set(manga["by_kind"]) == {"manga"} and manga["new"] == 2


def test_deleting_history_forgets_the_captured_vn_lines(tmp_path: Path) -> None:
    db, ledger, _episode, _volume = _db(tmp_path)
    vn = ledger.begin_vn_capture(title="VN", executable_hint="", window_id=1, generation=1)
    units = WordUnits(db)
    assert units.record_vn_lines(vn["session_id"], [{"id": 1, "text": "猫", "created_at": 1.0}]) == 1
    assert units.record_vn_lines(vn["session_id"], [{"id": 1, "text": "猫", "created_at": 1.0}]) == 0
    ledger.delete_history()
    assert units.vn_lines() == []


# ---------------------------------------------------------------- the background job


class _Parser:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str | None, bool]] = []
        self.fail = fail

    def jiten_preparse(self, text, digest=None, *, interactive=True):
        self.calls.append((text, digest, interactive))
        if self.fail:
            raise RuntimeError("Jiten API token is not configured")
        # One word per line, told apart by the line's text.
        return {
            "tokens": [
                [{"wordId": 100 + sum(map(ord, line)) % 997, "readingIndex": 0, "start": 0, "end": 1}]
                for line in text.split("\n")
            ]
        }


def _app(tmp_path: Path, db: Database, parser: _Parser, *, blocked: str | None = None, ocr=None):
    srt = tmp_path / "ep1.ja.srt"
    srt.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n猫だ\n\n2\n00:00:05,000 --> 00:00:06,500\n犬だ\n", encoding="utf-8"
    )
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO episodes(title,episode,video_path,subtitle_path,state,updated_at) VALUES('Ep',1,?,?,'ready',0)",
            (str(tmp_path / "ep1.mkv"), str(srt)),
        )
    pages, regions = ocr or ({1}, [(1, "あれ"), (1, "これ")])
    fake = SimpleNamespace(
        manager=SimpleNamespace(
            db=db, work_scheduler=SimpleNamespace(background_wait_reason=lambda: blocked)
        ),
        light_novels=parser,
        manga=SimpleNamespace(
            cached_region_texts=lambda book_id, include_unreliable=True: regions,
            _completed_ocr_pages=lambda book_id: pages,
        ),
        config=SimpleNamespace(
            paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
            tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        ),
        logger=SimpleNamespace(info=lambda *a: None, warning=lambda *a: None, debug=lambda *a: None),
        STATISTICS_WORDS_PAUSE_SECONDS=0.0,
    )
    for name in (
        "_statistics_words_worker",
        "_statistics_word_unit",
        "_statistics_episode_cues",
        "_statistics_manga_page_words",
    ):
        setattr(fake, name, MethodType(getattr(WebAppApi, name), fake))
    return fake


def test_the_background_job_parses_subtitles_ocr_and_vn_lines_then_stats_count_them(tmp_path: Path) -> None:
    now = time.time()
    db, ledger, episode, volume = _db(tmp_path)
    _watch(ledger, episode, _noon(1, now), (0.0, 3.0))
    _look(ledger, volume, _noon(1, now), [1, 2])
    vn = ledger.begin_vn_capture(title="VN", executable_hint="", window_id=1, generation=1)
    ledger.record_vn_runtime(vn["session_id"], active_seconds=60)
    WordUnits(db).record_vn_lines(vn["session_id"], [{"id": 1, "text": "やあ", "created_at": now - 30}])
    ledger.end_vn_capture(vn["session_id"])
    parser = _Parser()
    app = _app(tmp_path, db, parser)

    app._statistics_words_worker(threading.Event())

    # History was queued by the job itself (no Statistics visit needed); one parse per
    # episode (the review gate's cache entry), per OCR box and per VN line, all background.
    texts = sorted(call[0] for call in parser.calls)
    assert texts == sorted(["猫だ\n犬だ", "あれ", "これ", "やあ"])
    assert all(call[2] is False for call in parser.calls)
    assert app._statistics_words_state == {"running": False, "paused": ""}
    rows = {row["key"]: row for row in WordUnits(db).due(50, now=now + 7 * 3600)}
    # Page 2 has no OCR yet: kept as "without text" and tried again later, not now.
    assert list(rows) == [f"manga:{volume.media_uuid}:002.jpg"] and WordUnits(db).due(50) == []

    words = ConsumptionStatistics(db, count_words=True).query({"period": "7d"}, now=now)["highlights"][
        "words"
    ]
    assert words["by_kind"]["anime"] == {
        "total": 1,
        "unique": 1,
        "new": 1,
    }  # only the line at 1–2 s was shown
    assert words["by_kind"]["manga"] == {"total": 2, "unique": 2, "new": 2}
    assert words["by_kind"]["visual_novel"] == {"total": 1, "unique": 1, "new": 1}
    assert words["missing"] == 1 and words["pending"] == 0


def test_the_job_waits_while_energy_saving_and_keeps_failed_items_for_later(tmp_path: Path) -> None:
    now = time.time()
    db, ledger, episode, _volume = _db(tmp_path)
    _watch(ledger, episode, _noon(1, now), (0.0, 3.0))
    parser = _Parser()
    paused = _app(tmp_path, db, parser, blocked="energy_saving")
    paused._statistics_words_worker(threading.Event())
    assert parser.calls == [] and paused._statistics_words_state == {
        "running": False,
        "paused": "energy_saving",
    }
    assert [unit["status"] for unit in WordUnits(db).due(50)] == ["pending"]

    failing = _Parser(fail=True)
    app = paused  # energy saving is off again
    app.manager.work_scheduler.background_wait_reason = lambda: None
    app.light_novels = failing
    app._statistics_words_worker(threading.Event())
    assert len(failing.calls) == 1
    assert WordUnits(db).due(50) == []  # Jiten unavailable: retried in a few hours, not in a loop
    later = WordUnits(db).due(50, now=now + 7 * 3600)
    assert [unit["status"] for unit in later] == ["missing"]


def test_vn_capture_lines_are_kept_while_the_capture_runs(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    stats = ConsumptionStatistics(db)
    fake = SimpleNamespace(consumption_statistics=stats, logger=SimpleNamespace(warning=lambda *a: None))
    record = MethodType(WebAppApi._statistics_record_vn_lines, fake)
    record("s1", [{"id": 10, "text": "一", "created_at": 1.0}, {"id": 11, "text": "二", "created_at": 2.0}])
    record(
        "s1",
        [
            {"id": 10, "text": "一", "created_at": 1.0},
            {"id": 11, "text": "二", "created_at": 2.0},
            {"id": 12, "text": "三", "created_at": 3.0},
        ],
    )
    assert [text for _s, _at, text in stats.word_units.vn_lines()] == ["一", "二", "三"]


# ---------------------------------------------------------------- iPhone


def test_the_phone_statistics_carry_the_fun_stats(tmp_path: Path) -> None:
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "phone.sqlite"), tmp_path / "mobile")
    highlights = {
        "longest_streak_days": 3,
        "busiest_day": {"day": "2026-10-01", "seconds": 600.0},
        "longest_session": {"seconds": 600.0, "title": "Odd Taxi · 1", "kind": "anime", "start_utc": 1.0},
        "top_work": None,
        "anime_episodes": 2,
        "ln_characters_per_minute": 212.5,
        "manga_pages_per_hour": None,
        "words": {
            "total": 120,
            "unique": 80,
            "new": 12,
            "by_kind": {"anime": {"total": 120, "unique": 80, "new": 12}},
            "pending": 2,
            "missing": 0,
            "job": {"running": True, "paused": ""},
        },
    }
    service.api = SimpleNamespace(
        consumption_statistics_query=lambda scope, limit, offset: {
            "summary": {"total_seconds": 60, "active_days": 1, "sessions": 1},
            "days": [],
            "highlights": highlights,
        }
    )
    payload = json.loads(json.dumps(service.media.stats("7d")))
    assert payload["highlights"] == {
        "longestStreakDays": 3,
        "busiestDay": {"day": "2026-10-01", "seconds": 600.0},
        "longestSession": {"title": "Odd Taxi · 1", "kind": "anime", "seconds": 600.0},
        "topWork": None,
        "animeEpisodes": 2,
        "lnCharactersPerMinute": 212.5,
        "mangaPagesPerHour": None,
        "words": {
            "total": 120.0,
            "unique": 80.0,
            "new": 12.0,
            "pending": 2,
            "missing": 0,
            "byKind": {"anime": {"total": 120.0, "unique": 80.0, "new": 12.0}},
        },
    }
    service.api.consumption_statistics_query = lambda scope, limit, offset: {"summary": {}, "days": []}
    assert service.media.stats("7d")["highlights"] is None  # an older Mac payload


def test_a_stopped_job_ends_quietly_even_when_its_database_is_gone(tmp_path: Path) -> None:
    from pudge import web_app

    db, _ledger, _episode, _volume = _db(tmp_path)
    app = _app(tmp_path, db, _Parser())
    warnings: list[tuple] = []
    app.logger = SimpleNamespace(
        info=lambda *a: None, warning=lambda *a: warnings.append(a), debug=lambda *a: None
    )
    for name in ("statistics_words_start", "statistics_words_stop", "statistics_words_state"):
        setattr(app, name, MethodType(getattr(WebAppApi, name), app))
    app.statistics_words_start(delay=60.0)
    assert app.statistics_words_state()["running"] is True

    # The app (or test) closes; the database folder disappears afterwards.
    web_app.stop_statistics_word_jobs()
    app._statistics_words_thread.join(5.0)
    app.manager.db = Database(tmp_path / "gone" / "db.sqlite3")
    assert not app._statistics_words_thread.is_alive()
    assert app.statistics_words_state()["running"] is False and warnings == []

    # Close stops a job that is already failing without logging into a removed log.
    app.statistics_words_start(delay=60.0)
    app.statistics_words_stop(timeout=5.0)
    assert not app._statistics_words_thread.is_alive() and warnings == []
