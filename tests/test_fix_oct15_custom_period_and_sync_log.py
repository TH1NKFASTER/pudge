"""Statistics "Custom" keeps the period on screen; a stopped torrent client logs no traceback.

User: "when picking Custom, Statistics falls over because of a bad period. Let Custom keep
the last chosen one and just let me change it." Pressing Custom sent empty dates, the
backend answered "invalid period" and the page showed an error before a date could be
picked. Now Custom starts from the range on screen (last 30 days, 7 days, this year, or all
time from the first day with data) and only shows the date pickers; dates picked in the
wrong order are one range instead of an error.

The startup maintenance pass raised "aria2 RPC is not running" through its timed step, so
every start wrote an ERROR line with a full traceback for an expected, handled state.
"""

from __future__ import annotations

import json
import logging

from pudge.manager import AnimeManager, ManagerError


def _payload(days: list[str]) -> dict:
    return {
        "schema": 1,
        "timezone": {"label": "UTC"},
        "summary": {"total_seconds": 60, "active_days": 1, "sessions": 1, "works": 1},
        "breakdown": {"anime": 60.0},
        "volume": {"by_kind": {}, "by_media": {}},
        "days": [{"day": day, "seconds": 60.0, "breakdown": {"anime": 60.0}} for day in days],
        "works": [],
        "journal": [],
        "journal_total": 0,
        "media_options": [],
        "highlights": None,
    }


OPEN = r"""
const calls = [];
__api.consumption_statistics_query = async (scope) => { calls.push({...scope}); return PAYLOAD; };
setPage('statistics', true); for (let i = 0; i < 6; i++) await __tick(50);
const iso = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
const now = new Date(), today = iso(now);
const back = n => iso(new Date(now.getFullYear(), now.getMonth(), now.getDate() - n));
const custom = async () => { await __click('[data-stats-period="custom"]'); for (let i = 0; i < 4; i++) await __tick(30); };
const shown = () => ({hidden: document.getElementById('statsCustomDates').hidden,
  start: document.getElementById('statsStartDate').value, end: document.getElementById('statsEndDate').value,
  error: !!document.querySelector('#statisticsContent [data-stats-retry]')});
"""


def _run(page, code: str, days: list[str] | None = None):
    payload = _payload(days or ["2026-03-04"])
    return page.run(OPEN.replace("PAYLOAD", json.dumps(payload)) + code).value


def test_custom_starts_from_the_30_days_on_screen(page) -> None:
    out = _run(
        page,
        r"""
        await custom();
        return {view: shown(), last: calls[calls.length - 1], want: {start: back(29), end: today}};
        """,
    )
    assert out["view"]["hidden"] is False and out["view"]["error"] is False
    assert out["view"]["start"] == out["want"]["start"] and out["view"]["end"] == out["want"]["end"]
    assert out["last"]["period"] == "custom"
    assert out["last"]["start_date"] == out["want"]["start"] and out["last"]["end_date"] == out["want"]["end"]


def test_custom_after_7_days_year_and_all_time(page) -> None:
    out = _run(
        page,
        r"""
        const picks = {};
        for (const period of ['7d', 'year', 'all']) {
          await __click(`[data-stats-period="${period}"]`); for (let i = 0; i < 4; i++) await __tick(30);
          await custom();
          picks[period] = [shown().start, shown().end];
        }
        return {picks, want: {'7d': [back(6), today], year: [`${now.getFullYear()}-01-01`, today]}, today};
        """,
        days=["2025-11-02", "2026-01-05"],
    )
    assert out["picks"]["7d"] == out["want"]["7d"]
    assert out["picks"]["year"] == out["want"]["year"]
    # All time starts at the first day with data.
    assert out["picks"]["all"] == ["2025-11-02", out["today"]]


def test_custom_again_keeps_the_dates_and_reversed_dates_are_one_range(page) -> None:
    out = _run(
        page,
        r"""
        await __click('[data-stats-day="2026-03-04"]'); for (let i = 0; i < 4; i++) await __tick(30);
        await custom();
        const kept = [shown().start, shown().end];
        const start = document.getElementById('statsStartDate'), end = document.getElementById('statsEndDate');
        start.value = '2026-03-10'; end.value = '2026-03-01';
        end.dispatchEvent(new Event('change', {bubbles: true})); for (let i = 0; i < 4; i++) await __tick(30);
        return {kept, last: calls[calls.length - 1], view: shown()};
        """,
    )
    assert out["kept"] == ["2026-03-04", "2026-03-04"]
    assert out["last"]["start_date"] == "2026-03-01" and out["last"]["end_date"] == "2026-03-10"
    assert out["view"]["error"] is False


def test_stopped_torrent_client_logs_no_traceback_at_startup(caplog) -> None:
    manager = object.__new__(AnimeManager)
    messages: list[str] = []
    manager.sync_downloads = lambda: (_ for _ in ()).throw(ManagerError("aria2: aria2 RPC is not running"))
    manager.log = messages.append
    manager.logger = logging.getLogger("test-startup-sync")
    stats = {"downloads": 0}
    with caplog.at_level(logging.DEBUG, logger="test-startup-sync"):
        AnimeManager._sync_downloads_for_stats(manager, stats)
    assert messages == ["aria2: aria2 RPC is not running"]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR or record.exc_info]
    assert any("continue=maintenance" in record.getMessage() for record in caplog.records)
