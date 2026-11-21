"""Review 5 (media, statistics, visual novels). Invented data only.

* A VN capture counts as reading only while it is not paused and text keeps coming
  (a game left open overnight added hours to Statistics).
* The lines a VN capture read since the last poll are kept when it stops.
* The reader/player heartbeat names its book once a minute, not every 5 s (each naming
  re-read every audio file's samples / every chapter's text).
* Word counts ("met often") do not load listening rows.
* The phone's day streak counts back from today, not from the last active day.
* A long Statistics period builds the same numbers with less memory.
"""

from __future__ import annotations

import time
from pathlib import Path

from pudge import web_app
from pudge.consumption import ConsumptionLedger, VnReadingGate
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.database import Database
from pudge.iphone_media import streak


def _state(**values):
    return {"running": True, "generation": 1, "current_text_id": 1, "transcript": [{"id": 10}], **values}


def test_vn_capture_counts_only_while_read() -> None:
    gate = VnReadingGate()
    assert gate.reading("s", _state(), 0.0)
    assert gate.reading("s", _state(), 200.0)
    # The game left open: no new text for more than five minutes.
    assert not gate.reading("s", _state(), 301.0)
    # A new line: reading again at once.
    assert gate.reading("s", _state(current_text_id=2, transcript=[{"id": 11}]), 302.0)
    # Paused (the game in the background) never counts; back in front it counts again.
    assert not gate.reading("s", _state(current_text_id=2, transcript=[{"id": 11}], paused=True), 303.0)
    assert gate.reading("s", _state(current_text_id=2, transcript=[{"id": 11}]), 900.0)
    assert not gate.reading("", _state(), 901.0)
    assert not gate.reading("s", {"running": False}, 902.0)


def test_runtime_loop_does_not_bill_a_paused_capture(monkeypatch) -> None:
    calls: list[float] = []

    class Cancel:
        polls = 0

        def is_set(self) -> bool:
            return self.polls >= 2

        def wait(self, _seconds: float) -> bool:
            self.polls += 1
            return self.is_set()

    def run(paused: bool) -> list[float]:
        calls.clear()
        api = web_app.WebAppApi.__new__(web_app.WebAppApi)
        api.audiobooks = type("A", (), {"consumption_playback_observations": lambda self: []})()
        api.visual_novels = type("V", (), {"state": lambda self: _state(paused=paused)})()
        api.consumption = type(
            "C", (), {"record_vn_runtime": lambda self, _s, **kw: calls.append(kw["active_seconds"]) or []}
        )()
        api.logger = type("L", (), {"debug": lambda *a, **k: None, "warning": lambda *a, **k: None})()
        api._consumption_runtime_last_audio = {}
        api._consumption_runtime_last_vn = {}
        api._vn_consumption_session_id = "session-1"
        api._statistics_record_vn_lines = lambda *_args: None
        api._statistics_words_checked = 100.0
        mono, wall = iter([100.0, 105.0]), iter([1000.0, 1005.0])
        monkeypatch.setattr(web_app.time, "monotonic", lambda: next(mono))
        monkeypatch.setattr(web_app.time, "time", lambda: next(wall))
        api._consumption_runtime_loop(Cancel())
        return list(calls)

    assert run(False) == [5.0]
    assert run(True) == []


def test_stopping_a_capture_keeps_its_last_lines(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    stats = ConsumptionStatistics(db)
    ledger = ConsumptionLedger(db)
    capture = ledger.begin_vn_capture(title="Game", executable_hint="", window_id=3, generation=1)
    lines = [{"id": 1, "text": "最初の行", "created_at": time.time()}, {"id": 2, "text": "最後の行"}]

    class VN:
        def state(self):
            return {"running": True, "transcript": lines}

        def stop(self):
            return {"running": False}

    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.visual_novels = VN()
    api.consumption = ledger
    api.consumption_statistics = stats
    api.logger = type("L", (), {"info": lambda *a, **k: None, "warning": lambda *a, **k: None})()
    api._vn_consumption_session_id = capture["session_id"]
    api._vn_title_id = capture["vn_title_id"]
    api._statistics_vn_last_line = (capture["session_id"], 1)  # line 1 was kept by the last poll
    stats.word_units.record_vn_lines(capture["session_id"], lines[:1])
    api.visual_novel_stop()
    kept = [text for _s, _at, text in stats.word_units.vn_lines(session_ids={capture["session_id"]})]
    assert kept == ["最初の行", "最後の行"]


def test_heartbeats_name_their_book_once_a_minute(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    with db.connect() as conn:
        book = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,?,?,?)",
            (str(tmp_path / "book.mp3"), "Book", 600.0, 1.0, 1.0),
        ).lastrowid
    named: list[int] = []
    original = ledger.audiobook_media
    monkeypatch.setattr(ledger, "audiobook_media", lambda book_id: named.append(book_id) or original(book_id))
    for index in range(4):
        ledger.record_audiobook_activity(
            book_id=book, active_seconds=5.0, position_start=index * 5.0, position_end=index * 5.0 + 5
        )
    assert named == [book]
    ledger.detach_library_media(kind="audiobook", local_id=str(book))
    ledger.record_audiobook_activity(book_id=book, active_seconds=5.0, position_start=20.0, position_end=25.0)
    assert named == [book, book]
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM consumption_events").fetchone()[0] == 5


def _interval(ledger, media, kind, start, seconds, session=None, **locators):
    session = session or ledger.start_session(kind=kind, media_uuid=media, started_at_utc=start)
    ledger.append_interval(
        session_id=session,
        kind=kind,
        interval_start_utc=start,
        interval_end_utc=start + seconds,
        elapsed_monotonic_ms=seconds * 1000,
        media=[{"media_uuid": media, "role": "primary", **locators}],
        payload={"speed": 1.0},
    )
    return session


def test_word_counts_skip_listening_and_totals_are_unchanged(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    stats = ConsumptionStatistics(db)
    audio = ledger.ensure_media(kind="audiobook", title="Audio", aliases=[("local", "a")]).media_uuid
    show = ledger.ensure_media(kind="anime_episode", title="Show · 1", aliases=[("local", "s")]).media_uuid
    start = time.time() - 3600
    session = None
    for index in range(10):  # 5 minutes of listening, one session
        session = _interval(
            ledger,
            audio,
            "audiobook",
            start + index * 30,
            30.0,
            session,
            locator_start={"position_seconds": index * 30.0},
            locator_end={"position_seconds": index * 30.0 + 30},
        )
    _interval(ledger, show, "anime", start + 1000, 25.0)
    scope = stats._scope({"period": "7d"})
    words_only, _journal, _c = stats._effective_segments(scope, without_kinds=("audiobook",))
    assert {row["kind"] for row in words_only} == {"anime"}
    everything, journal, _c = stats._effective_segments(scope)
    assert sum(row["end"] - row["start"] for row in everything) == 325.0
    # Segments of one session share their parsed payloads (memory); nothing writes to them.
    audio_rows = [row for row in everything if row["kind"] == "audiobook"]
    assert len({id(row["payload"]) for row in audio_rows}) == 1
    result = stats.query({"period": "7d"})
    assert result["summary"]["total_seconds"] == 325.0
    assert result["volume"]["by_media"][audio]["consumed"] == 300.0


def test_phone_streak_counts_back_from_today() -> None:
    days = [
        {"day": "2026-10-01", "seconds": 60},
        {"day": "2026-10-02", "seconds": 60},
        {"day": "2026-10-03", "seconds": 60},
    ]
    # "All time" lists only active days: a run that ended last month is no current streak.
    assert streak(days, "2026-11-13") == 0
    assert streak(days, "2026-10-04") == 3  # today still empty: the run up to yesterday
    assert streak(days, "2026-10-03") == 3
    assert streak(days) == 3
    assert streak([], "2026-11-13") == 0
