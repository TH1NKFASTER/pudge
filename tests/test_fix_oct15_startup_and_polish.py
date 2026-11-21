"""Fixes after the 2026-10-15 debug report.

- A sync that fails (aria2 idle, qBittorrent closed) waits the normal 15/60 s interval like a
  successful one. It was retried on every 1-2 s window poll, each time with a traceback: about
  60 log entries a minute while the window was open.
- Weekly load and the reading-speed bars use the app's own hover tooltip (data-tooltip); their
  title attribute showed nothing on hover.
- The second row of the Statistics page shows amounts in their own units (characters, pages,
  audio) and no second anime time next to the anime time of the first row.
"""

from __future__ import annotations

from pathlib import Path

from test_v229_r12_poll_efficiency import make_api, make_poll_idle


def test_a_failed_sync_waits_the_normal_interval(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    make_poll_idle(api, monkeypatch)
    calls: list[str] = []

    def failing_sync() -> int:
        calls.append("sync")
        raise RuntimeError("aria2: aria2 RPC is not running")

    clock = [1000.0]
    monkeypatch.setattr(api.manager, "sync_downloads", failing_sync)
    monkeypatch.setattr("pudge.web_app.time.monotonic", lambda: clock[0])
    version = api.manager.db.get_state("ui_state_version", "")
    api._poll_downloads_and_subtitles_sync(version, True)
    for _second in range(10):
        clock[0] += 1.0
        api._poll_downloads_and_subtitles_sync(version, True)
    assert calls == ["sync"]
    clock[0] += 15.0
    api._poll_downloads_and_subtitles_sync(version, True)
    assert calls == ["sync", "sync"]


WEEKLY = {
    "total": 3,
    "items": [
        {"media_id": 1, "title": "Dorohedoro", "count": 2, "source": "schedule"},
        {"media_id": 2, "title": "Frieren", "count": 1, "source": "airing"},
    ],
}


def test_weekly_load_hover_uses_the_app_tooltip(page) -> None:
    result = page.run(
        r"""
        setPage('current'); await __tick(20);
        const node = $('pageWeeklyLoad');
        await __fire('#pageWeeklyLoad', 'mouseover', {bubbles: true}, 'MouseEvent');
        await __tick(20);
        return {title: node.getAttribute('title'), tooltip: node.dataset.tooltip || '',
                shown: $('titleTooltip').classList.contains('show'), tip: $('titleTooltip').textContent,
                rows: [PudgeI18n.t('home.weeklyLoadHint'),
                       PudgeI18n.t('home.weeklyLoadSchedule', {title: 'Dorohedoro', episodes: PudgeI18n.tn('uiB.episodeCount', 2)}),
                       PudgeI18n.t('home.weeklyLoadAiring', {title: 'Frieren', episodes: PudgeI18n.tn('uiB.episodeCount', 1)})]};
        """,
        state={"weekly_load": WEEKLY},
    )
    out = result.value
    assert out["title"] is None
    assert out["tooltip"] == "\n".join(out["rows"])
    assert out["shown"] is True and out["tip"] == out["tooltip"]


def test_tooltips_keep_their_lines() -> None:
    from css_support import css_rules

    assert css_rules("index.html").get(".title-tooltip", "white-space") == "pre-line"


def test_statistics_second_row_has_no_second_anime_time(page) -> None:
    payload = {
        "schema": 1,
        "scope": {"period": "week"},
        "timezone": {"mode": "system", "label": "UTC"},
        "summary": {
            "total_seconds": 86040,
            "active_days": 3,
            "sessions": 9,
            "works": 4,
            "manual_unplaced_seconds": 0,
        },
        "breakdown": {"anime": 86040.0, "light_novel": 10260.0},
        "volume": {
            "by_kind": {
                "anime": {"kind": "anime", "consumed": 82440.0, "unit": "seconds", "exact": True},
                "light_novel": {
                    "kind": "light_novel",
                    "consumed": 31427,
                    "unit": "characters",
                    "exact": True,
                },
                "manga": {"kind": "manga", "consumed": 46, "unit": "pages", "exact": True},
            },
            "by_media": {},
        },
        "highlights": {},
        "days": [],
        "works": [],
        "journal": [],
        "journal_total": 0,
        "media_options": [],
    }
    import json

    result = page.run(
        "__api.consumption_statistics_query = async () => ("
        + json.dumps(payload)
        + ");"
        + r"""
        setPage('statistics', true); for (let i = 0; i < 6; i++) await __tick(50);
        const rows = [...document.querySelectorAll('#statsBody .stats-breakdown')];
        const second = rows.find(row => !row.classList.contains('stats-legend'));
        return {second: second ? [...second.querySelectorAll('.stats-chip')].map(chip => chip.innerText) : [],
                anime: PudgeI18n.t('stats.kind.anime')};
        """
    )
    chips = result.value["second"]
    assert len(chips) == 2
    assert not any(chip.startswith(result.value["anime"]) for chip in chips)
