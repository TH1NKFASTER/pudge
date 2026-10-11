from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.database import Database, LATEST_SCHEMA_VERSION
from pudge.manager_models import LibraryAnime
from pudge.personal_schedule import (
    PersonalScheduleRevisionConflict,
    materialize_schedule_items,
    normalize_schedule_rule,
)
from pudge.web_app import WebAppApi
from tests.i18n_support import en, ru


def _anime(media_id: int = 42, *, progress: int = 4) -> LibraryAnime:
    return LibraryAnime(
        media_id=media_id,
        title="Schedule Test",
        status="CURRENT",
        progress=progress,
        episodes=12,
        format="TV",
        media_status="FINISHED",
    )


def _api(db: Database) -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=db, japanese_subtitles_required=lambda _media_id: True)
    api.config = SimpleNamespace(matching=SimpleNamespace(ocr_counts_as_ready=True))
    api.logger = SimpleNamespace(info=lambda *_a, **_k: None)
    api._episode_offset_cache = {}
    api._get_state = lambda **_kwargs: {}
    return api


def test_hours_are_elapsed_seconds_while_days_preserve_wall_clock_across_dst() -> None:
    hourly = materialize_schedule_items(
        [1, 2, 3],
        start_local_datetime="2026-03-08T00:30",
        timezone_iana="America/New_York",
        rule=normalize_schedule_rule("hours", 6),
    )
    assert hourly[1].unlock_at_utc - hourly[0].unlock_at_utc == pytest.approx(6 * 3600)
    assert hourly[2].unlock_at_utc - hourly[1].unlock_at_utc == pytest.approx(6 * 3600)
    local_second = datetime.fromtimestamp(hourly[1].unlock_at_utc, UTC).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    assert (local_second.hour, local_second.minute) == (7, 30)

    daily = materialize_schedule_items(
        [1, 2, 3],
        start_local_datetime="2026-03-07T02:30",
        timezone_iana="America/New_York",
        rule=normalize_schedule_rule("days", 1),
    )
    assert daily[1].nominal_local_datetime == "2026-03-08T02:30"
    assert daily[1].dst_adjusted is True
    adjusted = datetime.fromtimestamp(daily[1].unlock_at_utc, UTC).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    assert (adjusted.hour, adjusted.minute) == (3, 0)
    next_day = datetime.fromtimestamp(daily[2].unlock_at_utc, UTC).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    )
    assert (next_day.hour, next_day.minute) == (2, 30)


def test_rule_validation_rejects_nonpositive_fractional_unknown_and_overflow() -> None:
    for value in (0, -1, 1.5, "1.5", "nope"):
        with pytest.raises(ValueError, match="positive integer"):
            normalize_schedule_rule("hours", value)
    with pytest.raises(ValueError, match="hours, days, or weeks"):
        normalize_schedule_rule("months", 1)
    with pytest.raises(ValueError, match="too large"):
        materialize_schedule_items(
            [1, 2],
            start_local_datetime="2026-01-01T00:00",
            timezone_iana="UTC",
            rule=normalize_schedule_rule("weeks", 10**18),
        )


def test_v11_database_migrates_weekly_rule_to_v12(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE personal_release_schedules (
            schedule_id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id TEXT NOT NULL DEFAULT 'default',
            anime_id INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            start_local_datetime TEXT NOT NULL,
            timezone_iana TEXT NOT NULL,
            cadence TEXT NOT NULL DEFAULT 'weekly',
            first_episode_id INTEGER NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            rewatch INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            UNIQUE(profile_id, anime_id)
        );
        INSERT INTO personal_release_schedules(
            profile_id,anime_id,enabled,start_local_datetime,timezone_iana,cadence,
            first_episode_id,revision,rewatch,created_at,updated_at
        ) VALUES('default',42,1,'2026-09-01T20:00','Asia/Almaty','weekly',1,3,0,1,1);
        PRAGMA user_version=11;
        """
    )
    conn.commit()
    conn.close()

    db = Database(path)
    with db.connect() as migrated:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION >= 12
        row = migrated.execute("SELECT * FROM personal_release_schedules WHERE anime_id=42").fetchone()
        assert row["rule_version"] == 1
        assert row["interval_unit"] == "weeks"
        assert row["interval_every"] == 1
        assert row["interval_clock"] == "wall"
        assert row["cycle_id"] == 1
    assert path.with_name(f"{path.name}.pre-v{LATEST_SCHEMA_VERSION}.backup").exists()


def test_custom_rule_persists_and_revision_prevents_lost_update(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_anime(_anime())
    first = db.save_personal_release_schedule(
        42,
        episodes=[5, 6, 7],
        start_local_datetime="2026-09-23T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=False,
        rule_unit="hours",
        rule_every=6,
        expected_revision=0,
        now=0,
    )
    assert first.rule.as_dict() == {
        "version": 1,
        "unit": "hours",
        "every": 6,
        "clock": "elapsed",
        "custom": True,
    }
    loaded = db.personal_release_schedule(42, advance_unlocks=False)
    assert loaded is not None and loaded.rule == first.rule
    with pytest.raises(PersonalScheduleRevisionConflict) as conflict:
        db.save_personal_release_schedule(
            42,
            episodes=[5, 6, 7],
            start_local_datetime="2026-09-24T20:00",
            timezone_iana="Asia/Almaty",
            rewatch=False,
            rule_unit="days",
            rule_every=2,
            expected_revision=0,
            now=0,
        )
    assert conflict.value.actual_revision == first.revision


def test_edit_preserves_unlocked_but_disabled_restart_is_new_cycle(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_anime(_anime(progress=12))
    initial = db.save_personal_release_schedule(
        42,
        episodes=[1, 2],
        start_local_datetime="2000-01-01T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        expected_revision=0,
        now=0,
    )
    db.mark_personal_release_watched(42, 1, watched_at=initial.items[0].unlock_at_utc + 1)
    current = db.personal_release_schedule(42)
    assert current is not None and current.items[0].watched_at is not None

    edited = db.save_personal_release_schedule(
        42,
        episodes=[1, 2],
        start_local_datetime="2001-01-01T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        rule_unit="days",
        rule_every=2,
        expected_revision=current.revision,
        now=current.items[0].unlock_at_utc + 2,
    )
    assert edited.cycle_id == initial.cycle_id
    assert edited.items[0].watched_at is not None

    assert db.disable_personal_release_schedule(42) is True
    disabled = db.personal_release_schedule(42, advance_unlocks=False)
    assert disabled is not None
    restarted = db.save_personal_release_schedule(
        42,
        episodes=[1, 2],
        start_local_datetime="2099-01-01T20:00",
        timezone_iana="Asia/Almaty",
        rewatch=True,
        expected_revision=disabled.revision,
        new_cycle=True,
        now=0,
    )
    assert restarted.cycle_id == edited.cycle_id + 1
    assert restarted.items[0].watched_at is None
    assert restarted.items[0].unlocked is False


def test_api_preview_and_save_share_actual_episode_ids_rule_and_revision(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    anime = _anime(progress=4)
    db.upsert_anime(anime)
    api = _api(db)

    preview = api.personal_schedule_preview(
        anime.media_id,
        "2026-09-23T20:00",
        "Asia/Almaty",
        "days",
        2,
        0,
    )
    assert preview["ok"] is True
    assert preview["first_episode"] == 5
    assert [row["episode"] for row in preview["preview"][:3]] == [5, 6, 7]
    assert preview["rule"]["unit"] == "days"
    assert preview["rule"]["every"] == 2

    saved = api.personal_schedule_save(
        anime.media_id,
        "2026-09-23T20:00",
        "Asia/Almaty",
        "days",
        2,
        0,
    )
    assert saved["ok"] is True
    got = api.personal_schedule_get(anime.media_id)["schedule"]
    assert got["rule"]["unit"] == "days"
    assert got["rule"]["every"] == 2
    assert [row["episode"] for row in got["preview"][:3]] == [5, 6, 7]

    stale = api.personal_schedule_save(
        anime.media_id,
        "2026-10-01T20:00",
        "Asia/Almaty",
        "weeks",
        1,
        0,
    )
    assert stale["ok"] is False
    assert stale["conflict"] is True
    assert stale["revision"] == got["revision"]


SCHEDULED = {
    "media_id": 42,
    "title": "Schedule Test",
    "site_url": "https://anilist.co/anime/42",
    "media_status": "FINISHED",
    "personal_schedule": {"enabled": False, "eligible": True},
}

# The backend's answers: personal_schedule_get (revision 3, weekly, no preview yet); a
# preview of hours is slow (answers after 500 ms), other units at once; saving answers
# window.SAVE_ANSWER.
SCHEDULE_API = r"""
window.SAVE_ANSWER = null;
__api.personal_schedule_get = async id => ({ok: true, schedule: {eligible: true, enabled: false, revision: 3, timezone_iana: 'Asia/Almaty',
  start_local_datetime: '2026-10-02T20:00', rule: {unit: 'weeks', every: 1, custom: false}, preview: []}});
__api.personal_schedule_preview = (id, start, tz, unit, every, revision) => new Promise(resolve => setTimeout(() => resolve({ok: true,
  preview: [{episode: 5, local: `${unit}-${every}`}]}), unit === 'hours' ? 500 : 0));
__api.personal_schedule_save = async () => window.SAVE_ANSWER;
"""


def test_schedule_dialog_previews_and_saves_a_custom_interval_against_its_revision(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('current'); renderCurrent(); await __tick(50);
            await __click('.caught-up-card [data-action="card-menu"]'); await __tick(10);
            await __click('#contextMenu [data-context-action="personal-schedule"]'); await __tick(20);
            const preview = () => __text('#personalSchedulePreview');
            const labels = [...document.querySelectorAll('#modalBody label')].map(l => l.firstChild.textContent);
            const out = {title: __text('#modalTitle'), labels, advancedOpen: $('personalScheduleAdvanced').open, weekly: preview(),
                         units: [...$('personalScheduleUnit').options].map(o => [o.value, o.textContent])};
            await __click('#personalScheduleAdvanced summary');
            await __input('#personalScheduleEvery', '2');
            await __input('#personalScheduleUnit', 'hours');            // slow preview ...
            await __input('#personalScheduleUnit', 'days'); await __tick(50); // ... overtaken by days
            out.days = {summary: __text('#personalScheduleRuleSummary'), preview: preview()};
            await __tick(600); out.afterSlowAnswer = preview();
            window.SAVE_ANSWER = {ok: false, conflict: true, revision: 4};
            await __click('#modalBody [data-action="personal-schedule-save"]'); await __tick(20);
            out.conflict = {text: __text('#personalScheduleConflict'), open: $('modalBackdrop').classList.contains('open')};
            window.SAVE_ANSWER = {ok: true, state: ui.state};
            await __click('#modalBody [data-action="personal-schedule-save"]'); await __tick(20);
            out.saved = {open: $('modalBackdrop').classList.contains('open'), toasts: __toasts()};
            out.expected = {title: PudgeI18n.t('ui.watchOnASchedule2'), start: PudgeI18n.t('ui.cycleStart'), every: PudgeI18n.t('ui.every2'),
                            units: ['hours', 'days', 'weeks'].map(u => [u, PudgeI18n.t(`ui.${u}`)]),
                            days: personalScheduleRuleLabel({unit: 'days', every: 2}), conflict: PudgeI18n.t('ui.scheduleChangedInAnotherWindow'),
                            saved: PudgeI18n.t('ui.scheduleSaved')};
            return out;
            """,
            lang=lang,
            state={"current": [SCHEDULED], "home": {"caught_up": [SCHEDULED]}},
            before=SCHEDULE_API,
        )
        out, expected = result.value, result.value["expected"]
        assert out["title"] == expected["title"]
        assert expected["start"] in out["labels"] and expected["every"] in out["labels"]
        assert out["advancedOpen"] is False and out["units"] == expected["units"]
        assert expected["days"] in out["days"]["summary"]
        # The preview shows the last interval asked for, not a slower earlier answer.
        assert "days-2" in out["days"]["preview"] and out["afterSlowAnswer"] == out["days"]["preview"]
        start = ["2026-10-02T20:00", "Asia/Almaty"]
        assert result.calls_of("personal_schedule_preview") == [
            [42, *start, "weeks", 1, 3],
            [42, *start, "weeks", 2, 3],
            [42, *start, "hours", 2, 3],
            [42, *start, "days", 2, 3],
        ]
        # Saving sends the revision it was opened with; a newer one elsewhere is reported, not overwritten.
        assert result.calls_of("personal_schedule_save") == [[42, *start, "days", 2, 3]] * 2
        assert out["conflict"] == {"text": expected["conflict"], "open": True}
        assert out["saved"]["open"] is False and expected["saved"] in out["saved"]["toasts"]
    assert ru("ui.cycleStart") == "Начало цикла" and en("ui.cycleStart") == "Cycle start"


def test_schedule_dialog_of_a_custom_rule_opens_with_its_interval_shown(page) -> None:
    result = page.run(
        r"""
        await openPersonalScheduleDialog({media_id: 42, title: 'Schedule Test'}); await __tick(20);
        return {open: $('personalScheduleAdvanced').open, every: $('personalScheduleEvery').value, unit: $('personalScheduleUnit').value};
        """,
        before=SCHEDULE_API.replace(
            "rule: {unit: 'weeks', every: 1, custom: false}", "rule: {unit: 'days', every: 3, custom: true}"
        ),
    )
    assert result.value == {"open": True, "every": "3", "unit": "days"}
