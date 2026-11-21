"""Reading speed on the Statistics page and the weekly load on the Anime page.

Reading speed: the page only had one "Reading speed, light novels" number for the whole
period, and it mixed read-along (a light novel together with its linked audiobook) into
it: the read-along time and its characters counted as reading. Now the Statistics query
returns ``reading_speed`` per local day and per local week (Monday first) from light-novel
reading without audio only, the highlight follows the same rule, and the page shows a
"Reading speed" card with a Days / Weeks toggle, a tooltip per bar and an empty state.

Weekly load: the Anime page did not say how many episodes the watched titles bring in a
week. Its header now shows "Weekly load: N episodes" for the next 7 days: personal schedule
items unlocking in that time (the schedule replaces the title's airing schedule), plus the
next episode of airing titles when it airs in that time; the hover lists the titles.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pudge.consumption import ConsumptionLedger
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.database import Database
from pudge.manager_models import LibraryAnime
from test_web_app import make_api


@pytest.fixture(autouse=True)
def _one_minute_rate_floor(monkeypatch):
    # These cases were written for the old 1-minute floor; the 5-minute floor is covered in
    # test_fix_reading_speed_five_minutes.py.
    monkeypatch.setattr("pudge.consumption_statistics._MIN_RATE_SECONDS", 60.0)


# ---------------------------------------------------------------- backend: reading speed


def _local(year: int, month: int, day: int, hour: int = 12, minute: int = 0, second: int = 0) -> float:
    return datetime(year, month, day, hour, minute, second).timestamp()


def _reading(ledger, media, start: float, chunks, *, group: str = "") -> None:
    """chunks: [(seconds, characters)] read back to back in one chapter."""
    session = ledger.start_session(
        kind="light_novel", media_uuid=media.media_uuid, started_at_utc=start, activity_group_id=group
    )
    at, offset = start, 0
    for seconds, characters in chunks:
        ledger.append_interval(
            session_id=session,
            kind="light_novel",
            interval_start_utc=at,
            interval_end_utc=at + seconds,
            elapsed_monotonic_ms=seconds * 1000.0,
            media=[
                {
                    "media_uuid": media.media_uuid,
                    "source_revision": "r",
                    "role": "primary",
                    "locator_start": {"chapter_key": "c", "character_offset": offset},
                    "locator_end": {"chapter_key": "c", "character_offset": offset + characters},
                }
            ],
        )
        at += seconds
        offset += characters
    ledger.end_session(session, ended_at_utc=at)


def _listening(ledger, media, start: float, seconds: float, *, group: str) -> None:
    session = ledger.start_session(
        kind="audiobook", media_uuid=media.media_uuid, started_at_utc=start, activity_group_id=group
    )
    done = 0.0
    while done < seconds:  # the ledger takes chunks of at most 30 s
        chunk = min(30.0, seconds - done)
        ledger.append_interval(
            session_id=session,
            kind="audiobook",
            interval_start_utc=start + done,
            interval_end_utc=start + done + chunk,
            elapsed_monotonic_ms=chunk * 1000.0,
            media=[
                {
                    "media_uuid": media.media_uuid,
                    "source_revision": "r",
                    "role": "primary",
                    "locator_start": {"position_seconds": done},
                    "locator_end": {"position_seconds": done + chunk},
                }
            ],
        )
        done += chunk
    ledger.end_session(session, ended_at_utc=start + seconds)


def _library(tmp_path: Path):
    db = Database(tmp_path / "db.sqlite3")
    ledger = ConsumptionLedger(db)
    book = ledger.ensure_media(
        kind="light_novel", title="Book", aliases=[("stable", "b")], source_revision="r"
    )
    other = ledger.ensure_media(
        kind="light_novel", title="Other", aliases=[("stable", "o")], source_revision="r"
    )
    audio = ledger.ensure_media(
        kind="audiobook", title="Audio", aliases=[("stable", "a")], source_revision="r"
    )
    return db, ledger, book, other, audio


def test_read_along_time_and_its_characters_do_not_count(tmp_path: Path) -> None:
    db, ledger, book, other, audio = _library(tmp_path)
    start = _local(2026, 9, 30, 12)
    # Linked read-along: six 30 s chunks; the audiobook of the same activity group plays
    # over the first 135 s (four fast chunks and half of the fifth).
    _reading(ledger, book, start, [(30, 600)] * 4 + [(30, 300), (30, 300)], group="ln-audio:1")
    _listening(ledger, audio, start, 135, group="ln-audio:1")
    # Plain reading of another book the same day: 2 min, 600 characters.
    _reading(ledger, other, start + 3600, [(30, 150)] * 4)

    result = ConsumptionStatistics(db).query({"period": "7d"}, now=_local(2026, 10, 2, 9))

    day = next(row for row in result["reading_speed"]["days"] if row["day"] == "2026-09-30")
    breakdown = next(row for row in result["days"] if row["day"] == "2026-09-30")["breakdown"]
    # 15 s of the fifth chunk (150 characters) + the sixth chunk (300) + the other book (600).
    assert day["seconds"] == pytest.approx(165.0)
    assert breakdown["light_novel"] == pytest.approx(day["seconds"])  # same split as the time chart
    assert breakdown["light_novel+audiobook"] == pytest.approx(135.0)
    assert day["characters"] == 1050
    assert day["cpm"] == pytest.approx(1050 / (165 / 60))
    # The highlight used to be all LN characters (3600) over LN + read-along time (300 s): 720.
    assert result["highlights"]["ln_characters_per_minute"] == pytest.approx(1050 / (165 / 60))
    assert result["reading_speed"]["total"] == {
        "seconds": pytest.approx(165.0),
        "characters": 1050,
        "cpm": pytest.approx(1050 / (165 / 60)),
    }
    # Days without reading are in the list (the time chart's days) with no rate.
    assert len(result["reading_speed"]["days"]) == 7
    assert next(row for row in result["reading_speed"]["days"] if row["day"] == "2026-10-01") == {
        "day": "2026-10-01",
        "seconds": 0.0,
        "characters": 0,
        "cpm": None,
    }


def test_only_read_along_reading_gives_no_speed_and_the_old_highlight_is_gone(tmp_path: Path) -> None:
    db, ledger, book, _other, audio = _library(tmp_path)
    start = _local(2026, 9, 30, 12)
    _reading(ledger, book, start, [(30, 600)] * 6, group="ln-audio:2")
    _listening(ledger, audio, start - 5, 200, group="ln-audio:2")

    result = ConsumptionStatistics(db).query({"period": "7d"}, now=_local(2026, 10, 2, 9))

    assert result["breakdown"]["light_novel+audiobook"] == pytest.approx(180.0)
    assert all(row["seconds"] == 0 for row in result["reading_speed"]["days"])
    assert result["reading_speed"]["total"]["cpm"] is None
    assert result["highlights"]["ln_characters_per_minute"] is None


def test_days_and_monday_weeks_with_sum_over_sum_rates(tmp_path: Path) -> None:
    db, ledger, book, other, _audio = _library(tmp_path)
    # Sunday 27 Sep: 30 s only (too short for a rate). Monday 28 Sep: 2 min, 600 characters
    # (300/min). Sunday 4 Oct: 4 min, 600 characters (150/min). Monday 5 Oct: a new week.
    _reading(ledger, book, _local(2026, 9, 27, 20), [(30, 200)])
    _reading(ledger, book, _local(2026, 9, 28, 8), [(30, 150)] * 4)
    _reading(ledger, other, _local(2026, 10, 4, 21), [(30, 75)] * 8)
    _reading(ledger, other, _local(2026, 10, 5, 7), [(30, 150)] * 4)

    result = ConsumptionStatistics(db).query(
        {"period": "custom", "start_date": "2026-09-27", "end_date": "2026-10-05"},
        now=_local(2026, 10, 9, 12),
    )
    speed = result["reading_speed"]
    days = {row["day"]: row for row in speed["days"]}
    assert sorted(days) == [f"2026-09-{d}" for d in (27, 28, 29, 30)] + [f"2026-10-0{d}" for d in range(1, 6)]
    assert days["2026-09-27"] == {"day": "2026-09-27", "seconds": 30.0, "characters": 200, "cpm": None}
    assert days["2026-09-28"]["cpm"] == pytest.approx(300.0)
    assert days["2026-10-04"]["cpm"] == pytest.approx(150.0)
    assert days["2026-10-05"]["cpm"] == pytest.approx(300.0)

    weeks = {row["week_start"]: row for row in speed["weeks"]}
    assert list(weeks) == ["2026-09-21", "2026-09-28", "2026-10-05"]
    assert weeks["2026-09-21"] == {
        "week_start": "2026-09-21",
        "week_end": "2026-09-27",
        "seconds": 30.0,
        "characters": 200,
        "cpm": None,  # under 60 s of reading
    }
    middle = weeks["2026-09-28"]
    assert (middle["week_end"], middle["seconds"], middle["characters"]) == ("2026-10-04", 360.0, 1200)
    # 1200 characters in 6 minutes, not the average of 300 and 150.
    assert middle["cpm"] == pytest.approx(200.0)
    assert weeks["2026-10-05"]["cpm"] == pytest.approx(300.0)
    assert result["highlights"]["ln_characters_per_minute"] == pytest.approx(2000 / 8.5)


def test_reading_across_midnight_splits_time_and_characters(tmp_path: Path) -> None:
    db, ledger, book, _other, _audio = _library(tmp_path)
    # The first 30 s chunk (300 characters) is half before midnight, half after.
    _reading(ledger, book, _local(2026, 9, 29, 23, 59, 45), [(30, 300)] * 3)

    days = {
        row["day"]: row
        for row in ConsumptionStatistics(db).query({"period": "all"}, now=_local(2026, 10, 2, 9))[
            "reading_speed"
        ]["days"]
    }
    assert days["2026-09-29"] == {
        "day": "2026-09-29",
        "seconds": pytest.approx(15.0),
        "characters": 150,
        "cpm": None,
    }
    assert days["2026-09-30"]["seconds"] == pytest.approx(75.0)
    assert days["2026-09-30"]["characters"] == 750
    assert days["2026-09-30"]["cpm"] == pytest.approx(600.0)


# ---------------------------------------------------------------- Statistics page


def _payload(speed):
    return {
        "schema": 1,
        "timezone": {"label": "UTC"},
        "summary": {"total_seconds": 3600, "active_days": 2, "sessions": 2, "works": 1},
        "breakdown": {"light_novel": 3600.0},
        "volume": {"by_kind": {}, "by_media": {}},
        "days": [
            {"day": "2026-09-28", "seconds": 1800.0, "breakdown": {"light_novel": 1800.0}},
            {"day": "2026-09-29", "seconds": 0.0, "breakdown": {}},
            {"day": "2026-10-05", "seconds": 1800.0, "breakdown": {"light_novel": 1800.0}},
        ],
        "reading_speed": speed,
        "works": [],
        "journal": [],
        "journal_total": 0,
        "media_options": [],
        "highlights": None,
    }


SPEED = {
    "days": [
        {"day": "2026-09-28", "seconds": 1500.0, "characters": 9000, "cpm": 360.0},
        {"day": "2026-09-29", "seconds": 0.0, "characters": 0, "cpm": None},
        {"day": "2026-10-05", "seconds": 45.0, "characters": 200, "cpm": None},
    ],
    "weeks": [
        {
            "week_start": "2026-09-28",
            "week_end": "2026-10-04",
            "seconds": 1500.0,
            "characters": 9000,
            "cpm": 360.0,
        },
        {
            "week_start": "2026-10-05",
            "week_end": "2026-10-11",
            "seconds": 45.0,
            "characters": 200,
            "cpm": None,
        },
    ],
    "total": {"seconds": 1545.0, "characters": 9200, "cpm": 357.3},
}

OPEN = r"""
__api.consumption_statistics_query = async () => (PAYLOAD);
setPage('statistics', true); for (let i = 0; i < 6; i++) await __tick(50);
const card = () => document.querySelector('#statsBody .stats-speed');
const columns = () => [...document.querySelectorAll('#statsBody .stats-speed-col')].map(col => ({
  title: col.dataset.tooltip || null, label: col.querySelector('small').innerText,
  bar: col.querySelector('.stats-speed-bar') ? (col.querySelector('.stats-speed-bar.stats-speed-unmeasured') ? 'stub' : col.querySelector('.stats-speed-bar').style.height) : ''}));
const pressed = () => [...document.querySelectorAll('#statsBody [data-stats-speed-mode]')]
  .filter(b => b.getAttribute('aria-pressed') === 'true').map(b => b.dataset.statsSpeedMode);
"""


def _run(page, speed, code, **options):
    return page.run(OPEN.replace("PAYLOAD", json.dumps(_payload(speed))) + code, **options).value


def test_reading_speed_card_shows_days_and_weeks_with_tooltips(page) -> None:
    out = _run(
        page,
        SPEED,
        r"""
        const head = card().querySelector('.stats-speed-head').innerText;
        const days = {columns: columns(), pressed: pressed()};
        // The legend chips of the time chart do not touch the speed bars.
        await __fire('#statsBody [data-stats-legend="light_novel"]', 'pointerover', {}, 'PointerEvent');
        const timeBars = document.querySelectorAll('#statsBody .stats-seg').length;
        await __click('#statsBody [data-stats-speed-mode="weeks"]');
        const weeks = {columns: columns(), pressed: pressed()};
        await __click('#statsBody [data-stats-speed-mode="days"]');
        return {head, days, weeks, timeBars, back: columns().length,
          order: [...document.querySelector('#statsBody .stats-shell').children].map(node => [...node.classList].pop())};
        """,
    )
    assert out["head"].startswith("Reading speed")
    assert "light novels read without audio · average 357 chars/min" in out["head"]
    assert out["days"]["pressed"] == ["days"]
    assert out["days"]["columns"] == [
        {"title": "2026-09-28 · 360 chars/min · 25m 0s of reading", "label": "09-28", "bar": "100.00%"},
        {"title": "2026-09-29 · no reading", "label": "09-29", "bar": ""},
        {
            "title": "2026-10-05 · too little reading to measure · 45s of reading",
            "label": "10-05",
            "bar": "stub",
        },
    ]
    assert out["timeBars"] == 2
    assert out["weeks"]["pressed"] == ["weeks"]
    assert out["weeks"]["columns"] == [
        {
            "title": "2026-09-28 – 2026-10-04 · 360 chars/min · 25m 0s of reading",
            "label": "09-28",
            "bar": "100.00%",
        },
        {
            "title": "2026-10-05 – 2026-10-11 · too little reading to measure · 45s of reading",
            "label": "10-05",
            "bar": "stub",
        },
    ]
    assert out["back"] == 3
    # Below the time chart and its legend, above the stats cards.
    order = out["order"]
    assert order.index("stats-chart") < order.index("stats-legend") < order.index("stats-speed")


def test_reading_speed_card_in_russian(page) -> None:
    out = _run(
        page,
        SPEED,
        r"""
        await __click('#statsBody [data-stats-speed-mode="weeks"]');
        return {head: card().querySelector('.stats-speed-head').innerText, first: columns()[0].title};
        """,
        lang="ru",
    )
    assert "Скорость чтения" in out["head"] and "Дни" in out["head"] and "Недели" in out["head"]
    assert out["first"] == "2026-09-28 – 2026-10-04 · 360 знаков/мин · чтение: 25 мин 0 с"


def test_reading_speed_card_empty_state(page) -> None:
    empty = {
        "days": [{"day": "2026-09-28", "seconds": 0.0, "characters": 0, "cpm": None}],
        "weeks": [
            {
                "week_start": "2026-09-28",
                "week_end": "2026-10-04",
                "seconds": 0.0,
                "characters": 0,
                "cpm": None,
            }
        ],
        "total": {"seconds": 0.0, "characters": 0, "cpm": None},
    }
    out = _run(
        page,
        empty,
        r"""
        const days = __text('#statsBody .stats-speed .stats-empty');
        await __click('#statsBody [data-stats-speed-mode="weeks"]');
        return {days, weeks: __text('#statsBody .stats-speed .stats-empty'), bars: columns().length};
        """,
    )
    assert out == {
        "days": "No light-novel reading without audio in this period",
        "weeks": "No light-novel reading without audio in this period",
        "bars": 0,
    }


# ---------------------------------------------------------------- weekly load


def _anime(
    media_id: int, title: str, *, status: str = "CURRENT", media_status: str = "FINISHED", airs_in=None
):
    return LibraryAnime(
        media_id=media_id,
        title=title,
        status=status,
        progress=0,
        episodes=12,
        format="TV",
        media_status=media_status,
        next_airing_episode=3 if airs_in is not None else None,
        next_airing_at=int(time.time() + airs_in) if airs_in is not None else None,
    )


def _schedule(db, media_id: int, *, unit: str, every: int, starts_in: float) -> None:
    start = datetime.fromtimestamp(time.time() + starts_in, UTC).strftime("%Y-%m-%dT%H:%M")
    db.save_personal_release_schedule(
        media_id,
        episodes=list(range(1, 13)),
        start_local_datetime=start,
        timezone_iana="UTC",
        rewatch=False,
        rule_unit=unit,
        rule_every=every,
    )


DAY = 86400


def test_weekly_load_counts_schedules_and_airing_episodes(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    for anime in (
        _anime(1, "Every Two Days"),
        _anime(2, "Airs In Three Days", media_status="RELEASING", airs_in=3 * DAY),
        _anime(3, "Airs In Nine Days", media_status="RELEASING", airs_in=9 * DAY),
        _anime(4, "Scheduled Airing", media_status="RELEASING", airs_in=2 * DAY),
        _anime(5, "Finished Unscheduled"),
        _anime(6, "Planned Airing", status="PLANNING", media_status="RELEASING", airs_in=DAY),
        _anime(7, "Planned Scheduled", status="PLANNING"),
        _anime(8, "Scheduled Later", media_status="RELEASING", airs_in=DAY),
    ):
        db.upsert_anime(anime)
    # Unlocks at +1 h, +2 d, +4 d, +6 d (then +8 d): four in the next 7 days, one already watched.
    _schedule(db, 1, unit="days", every=2, starts_in=3600)
    db.mark_personal_release_watched(1, 2)
    # Daily from +1 h: seven items; the airing episode of the title is not added on top.
    _schedule(db, 4, unit="days", every=1, starts_in=3600)
    _schedule(db, 7, unit="days", every=1, starts_in=3600)
    # The schedule starts in 10 days: it wins over airing, so the title brings nothing this week.
    _schedule(db, 8, unit="weeks", every=1, starts_in=10 * DAY)

    load = api.get_state()["weekly_load"]

    assert load == {
        "total": 11,
        "items": [
            {"media_id": 4, "title": "Scheduled Airing", "count": 7, "source": "schedule"},
            {"media_id": 1, "title": "Every Two Days", "count": 3, "source": "schedule"},
            {"media_id": 2, "title": "Airs In Three Days", "count": 1, "source": "airing"},
        ],
    }


def test_weekly_load_is_empty_without_watched_titles(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(_anime(5, "Finished Unscheduled"))
    assert api.get_state()["weekly_load"] == {"total": 0, "items": []}


LOAD = {
    "total": 11,
    "items": [
        {"media_id": 4, "title": "Scheduled Airing", "count": 7, "source": "schedule"},
        {"media_id": 1, "title": "Every Two Days", "count": 3, "source": "schedule"},
        {"media_id": 2, "title": "Airs In Three Days", "count": 1, "source": "airing"},
    ],
}

HEADER = r"""
setPage('current', true); await __tick(50);
const shown = () => ({visible: __visible('#pageWeeklyLoad'), text: __text('#pageWeeklyLoad'),
  title: document.getElementById('pageWeeklyLoad').dataset.tooltip || ''});
"""


def test_anime_page_header_shows_the_weekly_load_with_titles_on_hover(page) -> None:
    out = page.run(
        HEADER
        + r"""
        const current = shown();
        setPage('statistics', true); await __tick(50);
        const statistics = shown();
        setPage('current', true); await __tick(50);
        // A state update redraws it.
        ui.state = {...ui.state, weekly_load: {total: 1, items: [{media_id: 2, title: 'Airs In Three Days', count: 1, source: 'airing'}]}};
        renderDataPages(true);
        const updated = shown();
        ui.state = {...ui.state, weekly_load: {total: 0, items: []}};
        renderDataPages(true);
        return {current, statistics, updated, zero: shown()};
        """,
        state={"weekly_load": LOAD},
    ).value
    assert out["current"] == {
        "visible": True,
        "text": "Weekly load: 11 episodes",
        "title": "Episodes in the next 7 days of the anime you are watching\n"
        "Scheduled Airing — 7 episodes, personal schedule\n"
        "Every Two Days — 3 episodes, personal schedule\n"
        "Airs In Three Days — 1 episode, airing",
    }
    assert out["statistics"]["visible"] is False
    assert out["updated"]["visible"] is True and out["updated"]["text"] == "Weekly load: 1 episode"
    assert out["zero"]["visible"] is False


def test_weekly_load_in_russian_and_hidden_without_data(page) -> None:
    ru = page.run(HEADER + "return shown();", state={"weekly_load": LOAD}, lang="ru").value
    assert ru["text"] == "Нагрузка в неделю: 11 серий"
    assert "Scheduled Airing — 7 серий, личное расписание" in ru["title"]
    assert "Airs In Three Days — 1 серия, выходит в эфир" in ru["title"]
    one = page.run(
        HEADER + "return shown();",
        state={
            "weekly_load": {
                "total": 21,
                "items": [{"media_id": 1, "title": "A", "count": 21, "source": "schedule"}],
            }
        },
        lang="ru",
    ).value
    assert one["text"] == "Нагрузка в неделю: 21 серия"
    assert page.run(HEADER + "return shown();").value["visible"] is False


def test_weekly_load_counts_only_the_next_seven_days(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(_anime(1, "Weekly"))
    _schedule(db, 1, unit="weeks", every=1, starts_in=-DAY)  # -1 d (already out), +6 d, +13 d
    now = time.time()
    anime = db.anime_list()
    assert api._weekly_load_payload(anime, now=now)["total"] == 1
    assert api._weekly_load_payload(anime, now=now + 7 * DAY)["total"] == 1  # +13 d
    assert api._weekly_load_payload(anime, now=now + 6 * DAY + 3600)["total"] == 1
    assert api._weekly_load_payload(anime, now=now - 2 * DAY)["total"] == 1  # -1 d, not +6 d yet
