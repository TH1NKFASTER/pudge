"""Changing the interval of a running personal schedule counts from the last watched
episode and opens at most one new episode right away (shorter and longer alike)."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from pudge.database import Database
from pudge.manager_models import LibraryAnime
from pudge.personal_schedule import materialize_schedule_items, normalize_schedule_rule
from pudge.web_app import WebAppApi

ALMATY = "Asia/Almaty"  # UTC+5, no DST
BERLIN = "Europe/Berlin"  # DST ends 2026-10-25 03:00 CEST -> 02:00 CET
EPISODES = [1, 2, 3, 4, 5, 6]


def _at(local: str, zone: str = ALMATY) -> float:
    return datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(zone)).timestamp()


def _local(value: float, zone: str = ALMATY) -> str:
    return datetime.fromtimestamp(value, UTC).astimezone(ZoneInfo(zone)).strftime("%Y-%m-%dT%H:%M")


def _anime() -> LibraryAnime:
    return LibraryAnime(
        media_id=42,
        title="Schedule Test",
        status="CURRENT",
        progress=0,
        episodes=6,
        format="TV",
        media_status="FINISHED",
    )


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_anime(_anime())
    return db


def _save(db: Database, *, start: str, unit: str, every: int, now: float, zone: str = ALMATY):
    current = db.personal_release_schedule(42, advance_unlocks=False)
    return db.save_personal_release_schedule(
        42,
        episodes=EPISODES,
        start_local_datetime=start,
        timezone_iana=zone,
        rewatch=False,
        rule_unit=unit,
        rule_every=every,
        expected_revision=current.revision if current is not None else 0,
        now=now,
    )


def _plan(schedule, zone: str = ALMATY) -> dict[int, tuple[str, bool, bool]]:
    return {
        item.episode: (_local(item.unlock_at_utc, zone), item.unlocked, item.watched_at is not None)
        for item in schedule.items
    }


def _open_unwatched(schedule) -> list[int]:
    return [item.episode for item in schedule.items if item.unlocked and item.watched_at is None]


def test_shorten_with_a_ready_episode_keeps_it_and_next_opens_one_interval_from_now(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _save(db, start="2026-09-01T20:00", unit="weeks", every=1, now=_at("2026-08-31T00:00"))
    db.personal_release_schedule(42, now=_at("2026-09-01T20:00"))
    db.mark_personal_release_watched(42, 1, watched_at=_at("2026-09-02T21:00"))
    db.personal_release_schedule(42, now=_at("2026-09-09T12:00"))  # episode 2 opened on Sep 8

    edited = _save(db, start="2026-09-01T20:00", unit="days", every=1, now=_at("2026-09-09T12:00"))

    plan = _plan(edited)
    assert plan[2] == ("2026-09-08T20:00", True, False)
    assert plan[3] == ("2026-09-10T12:00", False, False)
    assert plan[4] == ("2026-09-11T12:00", False, False)
    assert _open_unwatched(edited) == [2]
    assert _open_unwatched(db.personal_release_schedule(42, now=_at("2026-09-10T11:59"))) == [2]
    assert _open_unwatched(db.personal_release_schedule(42, now=_at("2026-09-10T12:00"))) == [2, 3]


def test_shorten_without_a_ready_episode_opens_exactly_one_now_when_the_new_interval_has_passed(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    _save(db, start="2026-09-01T20:00", unit="weeks", every=1, now=_at("2026-08-31T00:00"))
    db.personal_release_schedule(42, now=_at("2026-09-08T20:00"))
    db.mark_personal_release_watched(42, 1, watched_at=_at("2026-09-02T21:00"))
    db.mark_personal_release_watched(42, 2, watched_at=_at("2026-09-08T22:00"))

    now = _at("2026-09-10T10:00")
    edited = _save(db, start="2026-09-01T20:00", unit="days", every=1, now=now)

    plan = _plan(edited)
    assert plan[3] == ("2026-09-10T10:00", True, False)  # Sep 9 22:00 already passed -> now
    assert plan[4] == ("2026-09-11T10:00", False, False)
    assert plan[5] == ("2026-09-12T10:00", False, False)
    assert _open_unwatched(edited) == [3]


def test_shorten_without_a_ready_episode_counts_from_when_the_last_one_was_watched(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _save(db, start="2026-09-01T20:00", unit="weeks", every=1, now=_at("2026-08-31T00:00"))
    db.personal_release_schedule(42, now=_at("2026-09-08T20:00"))
    db.mark_personal_release_watched(42, 1, watched_at=_at("2026-09-02T21:00"))
    db.mark_personal_release_watched(42, 2, watched_at=_at("2026-09-08T22:00"))

    edited = _save(db, start="2026-09-01T20:00", unit="days", every=3, now=_at("2026-09-10T10:00"))

    plan = _plan(edited)
    assert plan[3] == ("2026-09-11T22:00", False, False)
    assert plan[4] == ("2026-09-14T22:00", False, False)
    assert _open_unwatched(edited) == []


def test_lengthen_counts_from_the_last_watched_episode(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _save(db, start="2026-09-01T20:00", unit="days", every=1, now=_at("2026-08-31T00:00"))
    db.personal_release_schedule(42, now=_at("2026-09-03T20:00"))
    for episode, watched in ((1, "2026-09-01T21:00"), (2, "2026-09-02T21:00"), (3, "2026-09-03T21:00")):
        db.mark_personal_release_watched(42, episode, watched_at=_at(watched))

    edited = _save(db, start="2026-09-01T20:00", unit="weeks", every=1, now=_at("2026-09-03T23:00"))

    plan = _plan(edited)
    assert plan[3] == ("2026-09-03T20:00", True, True)
    assert plan[4] == ("2026-09-10T21:00", False, False)
    assert plan[5] == ("2026-09-17T21:00", False, False)


def test_lengthen_with_a_ready_episode_keeps_it_and_next_opens_one_interval_from_now(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _save(db, start="2026-09-01T20:00", unit="days", every=1, now=_at("2026-08-31T00:00"))
    db.personal_release_schedule(42, now=_at("2026-09-04T21:00"))
    db.mark_personal_release_watched(42, 1, watched_at=_at("2026-09-01T21:00"))
    db.mark_personal_release_watched(42, 2, watched_at=_at("2026-09-02T21:00"))
    db.mark_personal_release_watched(42, 3, watched_at=_at("2026-09-03T21:00"))

    edited = _save(db, start="2026-09-01T20:00", unit="weeks", every=1, now=_at("2026-09-04T21:00"))

    plan = _plan(edited)
    assert plan[4] == ("2026-09-04T20:00", True, False)
    assert plan[5] == ("2026-09-11T21:00", False, False)
    assert plan[6] == ("2026-09-18T21:00", False, False)


def test_saving_again_unchanged_keeps_the_reanchored_plan(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _save(db, start="2026-09-01T20:00", unit="weeks", every=1, now=_at("2026-08-31T00:00"))
    db.personal_release_schedule(42, now=_at("2026-09-08T20:00"))
    db.mark_personal_release_watched(42, 1, watched_at=_at("2026-09-02T21:00"))
    db.mark_personal_release_watched(42, 2, watched_at=_at("2026-09-08T22:00"))
    edited = _save(db, start="2026-09-01T20:00", unit="days", every=1, now=_at("2026-09-10T10:00"))

    again = _save(db, start=edited.start_local_datetime, unit="days", every=1, now=_at("2026-09-10T10:05"))

    assert _plan(again) == _plan(edited)
    assert _open_unwatched(again) == [3]


def test_nothing_watched_keeps_the_start_date_plan(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _save(db, start="2026-09-01T20:00", unit="weeks", every=1, now=_at("2026-08-31T00:00"))

    edited = _save(db, start="2026-09-01T20:00", unit="days", every=2, now=_at("2026-09-02T00:00"))

    expected = materialize_schedule_items(
        EPISODES,
        start_local_datetime="2026-09-01T20:00",
        timezone_iana=ALMATY,
        rule=normalize_schedule_rule("days", 2),
    )
    assert [item.unlock_at_utc for item in edited.items] == [item.unlock_at_utc for item in expected]
    assert edited.start_local_datetime == "2026-09-01T20:00"


def test_days_keep_the_wall_clock_and_hours_keep_elapsed_time_across_dst(tmp_path: Path) -> None:
    for unit, every, expected in (
        ("days", 5, ["2026-10-26T20:30", "2026-10-31T20:30"]),
        ("hours", 120, ["2026-10-26T19:30", "2026-10-31T19:30"]),
    ):
        db = _db(tmp_path / unit)
        _save(
            db,
            start="2026-10-20T20:00",
            unit="weeks",
            every=1,
            now=_at("2026-10-19T00:00", BERLIN),
            zone=BERLIN,
        )
        db.personal_release_schedule(42, now=_at("2026-10-20T20:00", BERLIN))
        db.mark_personal_release_watched(42, 1, watched_at=_at("2026-10-21T20:30", BERLIN))

        edited = _save(
            db,
            start="2026-10-20T20:00",
            unit=unit,
            every=every,
            now=_at("2026-10-22T12:00", BERLIN),
            zone=BERLIN,
        )

        plan = _plan(edited, BERLIN)
        assert [plan[2][0], plan[3][0]] == expected, unit
        assert _open_unwatched(edited) == []


def _api(db: Database) -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=db, japanese_subtitles_required=lambda _media_id: True)
    api.config = SimpleNamespace(matching=SimpleNamespace(ocr_counts_as_ready=True))
    api.logger = SimpleNamespace(info=lambda *_a, **_k: None)
    api._episode_offset_cache = {}
    api._get_state = lambda **_kwargs: {}
    return api


def test_dialog_preview_save_and_home_card_show_the_reanchored_next_episode(
    tmp_path: Path, monkeypatch
) -> None:
    db = _db(tmp_path)
    api = _api(db)
    clock = {"now": _at("2026-08-31T00:00")}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    assert api.personal_schedule_save(42, "2026-09-01T20:00", ALMATY, "weeks", 1, 0)["ok"] is True
    clock["now"] = _at("2026-09-08T20:00")
    db.personal_release_schedule(42)
    db.mark_personal_release_watched(42, 1, watched_at=_at("2026-09-02T21:00"))
    db.mark_personal_release_watched(42, 2, watched_at=_at("2026-09-08T22:00"))
    clock["now"] = _at("2026-09-10T10:00")
    revision = api.personal_schedule_get(42)["schedule"]["revision"]

    preview = api.personal_schedule_preview(42, "2026-09-01T20:00", ALMATY, "days", 3, revision)
    assert [row["local"] for row in preview["preview"][2:4]] == ["2026-09-11T22:00", "2026-09-14T22:00"]

    assert api.personal_schedule_save(42, "2026-09-01T20:00", ALMATY, "days", 3, revision)["ok"] is True
    card = api.personal_schedule_get(42)["schedule"]
    assert card["status"] == "caught_up"
    assert card["next_episode"] == 3
    assert card["next_unlock_local"] == "2026-09-11T22:00"
    assert [row["local"] for row in card["preview"][2:4]] == ["2026-09-11T22:00", "2026-09-14T22:00"]


def test_watched_and_released_episodes_keep_their_real_dates_everywhere(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    api = _api(db)
    clock = {"now": _at("2026-08-31T00:00")}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    assert api.personal_schedule_save(42, "2026-09-01T20:00", ALMATY, "weeks", 1, 0)["ok"] is True
    db.personal_release_schedule(42, now=_at("2026-09-01T20:00"))
    db.mark_personal_release_watched(42, 1, watched_at=_at("2026-09-02T21:00"))
    # Episode 2 came out on Sep 8 while the app was closed: nothing marked it open yet.
    clock["now"] = _at("2026-09-09T12:00")
    revision = db.personal_release_schedule(42, advance_unlocks=False).revision
    real = {1: "2026-09-01T20:00", 2: "2026-09-08T20:00"}

    preview = api.personal_schedule_preview(42, "2026-09-01T20:00", ALMATY, "days", 1, revision)["preview"]
    assert {row["episode"]: row["local"] for row in preview[:2]} == real
    assert [row["preserved"] for row in preview[:3]] == [True, True, False]
    assert preview[2]["local"] == "2026-09-10T12:00"

    assert api.personal_schedule_save(42, "2026-09-01T20:00", ALMATY, "days", 1, revision)["ok"] is True
    stored = db.personal_release_schedule(42)
    assert {item.episode: _local(item.unlock_at_utc) for item in stored.items[:2]} == real
    assert stored.items[0].watched_at == _at("2026-09-02T21:00")
    assert _open_unwatched(stored) == [2]
    dialog = api.personal_schedule_get(42)["schedule"]["preview"]
    assert {row["episode"]: row["local"] for row in dialog[:2]} == real
    assert [row["local"] for row in dialog[2:4]] == ["2026-09-10T12:00", "2026-09-11T12:00"]
    card = api._personal_schedule_snapshot(db.get_anime(42))
    assert card["next_episode"] == 2 and _local(card["next_unlock_at"]) == real[2]

    # Watching episode 2 and changing the interval again still keeps both real dates.
    db.mark_personal_release_watched(42, 2, watched_at=_at("2026-09-09T13:00"))
    clock["now"] = _at("2026-09-09T14:00")
    revision = db.personal_release_schedule(42).revision
    assert api.personal_schedule_save(42, "2026-09-01T20:00", ALMATY, "weeks", 2, revision)["ok"] is True
    again = api.personal_schedule_get(42)["schedule"]["preview"]
    assert {row["episode"]: row["local"] for row in again[:2]} == real
    assert again[2]["local"] == "2026-09-23T13:00"
