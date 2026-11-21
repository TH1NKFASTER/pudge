"""Planning → "search released episodes": live progress, no blinking, sidebar line.

What went wrong before, and what happens now:

- The search dialog showed only "Checking local files and searching releases 0 / 12"
  with an empty, static bar for a minute or more (each release search can take ~30 s),
  and rebuilt its whole body via innerHTML on every 900 ms poll. Python now reports the
  step (`phase`: local check, season pack, episodes), the episode being searched
  (`current_episode`), the local episodes as soon as the library scan knows them and
  how long the step has run (`step_elapsed`); episode results were already published
  one by one. The dialog is built once and updated in place: a step line, a bar that
  fills with finished episodes and sweeps while a step runs (still with reduced motion),
  and one row per episode (waiting, searching, added with the release, already local,
  not found, error).
- Pressing the button made Planning blink: every state change while the search ran
  (a push after sync_downloads/scan_library/an added torrent, the get_state when it
  finished, download polls) ran buildPlannedNow(), which emptied #plannedContent and
  re-created every card with a new cover <img> showing its "Cover" placeholder until it
  loaded again. Unchanged cards now stay as they are; a changed card is replaced but
  keeps its cover.
- With the dialog closed the search was invisible, and when it finished the result
  dialog popped open over whatever was on screen. Now the sidebar line under the status
  text (#statusTask, separate from setStatus) shows "Searching episodes: <title> 3/12";
  a click reopens the live dialog. A search that finishes while the dialog is closed
  leaves its summary there for 30 s (a click opens the result). It survives page
  switches and is picked up again after a window reload.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from css_support import css_rules
from test_web_app import make_api

from pudge.manager_models import LibraryAnime
from pudge.web_app import WebAppApi


class _AliveThread:
    @staticmethod
    def is_alive() -> bool:
        return True


def _bare_api() -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.logger = SimpleNamespace(warning=lambda *_a, **_k: None, exception=lambda *_a, **_k: None)
    api._planning_episode_download_lock = threading.Lock()
    api._planning_episode_download_state = {"status": "running", "running": True, "episodes": []}
    api._planning_episode_download_thread = _AliveThread()
    api._planning_episode_cancel_event = threading.Event()
    api._planning_episode_job_id = ""
    api._planning_local_episodes = lambda *_a, **_k: [1]
    return api


def test_worker_reports_step_current_episode_and_each_result_as_it_goes() -> None:
    api = _bare_api()
    seen: list[dict] = []

    def snapshot(label: str) -> None:
        job = api.planning_episode_download_status()
        seen.append(
            {
                "at": label,
                "phase": job.get("phase"),
                "episode": job.get("current_episode"),
                "local": job.get("local_episodes"),
                "results": [
                    (row["episode"], row["status"], row.get("release", "")) for row in job["episodes"]
                ],
                "elapsed": job.get("step_elapsed"),
            }
        )

    def batch(_media_id: int, **kwargs):
        snapshot("batch")
        return None

    def candidates(_media_id: int, **kwargs):
        snapshot(f"episode {kwargs['episode']}")
        return [SimpleNamespace(title=f"[Group] Show - {kwargs['episode']:02d}", size_bytes=0)]

    def add(_media_id: int, eligible, **kwargs):
        snapshot(f"add {kwargs['episode']}")
        return eligible[0]

    # Every episode is searched first, then they are started (all or nothing).
    manager = SimpleNamespace(
        downloads_enabled=lambda: False,
        scan_library=lambda **_k: snapshot("scan") or [],
        search_and_add_best=batch,
        release_candidates=candidates,
        add_release_candidates=add,
    )
    anime = LibraryAnime(media_id=9, title="Show", episodes=3, media_status="FINISHED")

    api._run_planning_episode_download(manager, anime, 3)

    for row in seen:
        assert row["elapsed"] is not None and 0 <= row["elapsed"] < 5
    assert [(row["at"], row["phase"], row["episode"], row["local"], row["results"]) for row in seen] == [
        ("scan", "local", None, [], []),
        # Local episodes are published right after the scan, before any release search.
        ("batch", "batch", None, [1], []),
        ("episode 2", "episodes", 2, [1], [(1, "local", "")]),
        # Episode 2's result is visible while episode 3 is still being searched.
        ("episode 3", "episodes", 3, [1], [(1, "local", ""), (2, "found", "[Group] Show - 02")]),
        (
            "add 2",
            "adding",
            2,
            [1],
            [(1, "local", ""), (2, "found", "[Group] Show - 02"), (3, "found", "[Group] Show - 03")],
        ),
        (
            "add 3",
            "adding",
            3,
            [1],
            [(1, "local", ""), (2, "added", "[Group] Show - 02"), (3, "found", "[Group] Show - 03")],
        ),
    ]
    api._planning_episode_download_thread = None
    final = api.planning_episode_download_status()
    assert final["phase"] == "done" and final["current_episode"] is None and final["status"] == "done"
    assert final["running"] is False and final["step_elapsed"] == 0.0
    # The result rows keep their old shape.
    assert final["episodes"] == [
        {"episode": 1, "status": "local", "error": ""},
        {"episode": 2, "status": "added", "release": "[Group] Show - 02", "error": ""},
        {"episode": 3, "status": "added", "release": "[Group] Show - 03", "error": ""},
    ]


def test_status_reports_how_long_the_current_step_has_run() -> None:
    api = _bare_api()
    api._planning_episode_download_state.update(
        {"phase": "episodes", "current_episode": 4, "phase_started_at": time.time() - 12}
    )
    job = api.planning_episode_download_status()
    assert job["running"] is True and job["phase"] == "episodes" and job["current_episode"] == 4
    assert 11.5 <= job["step_elapsed"] < 20


def test_a_started_search_reports_the_local_check_step_at_once(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=9, title="Show", status="PLANNING", episodes=3, media_status="FINISHED", format="TV"
        )
    )
    gate = threading.Event()
    api._run_planning_episode_download = lambda *_args: gate.wait(10)  # type: ignore[method-assign]
    try:
        job = api.start_planning_episode_download(9)
    finally:
        gate.set()
    api._planning_episode_download_thread.join(10)
    assert job["running"] is True and job["phase"] == "local" and job["current_episode"] is None
    assert job["local_episodes"] == [] and job["total"] == 3 and job["step_elapsed"] >= 0


# The fake search in Python: tests change window.__job between polls.
SEARCH = r"""
window.__job = {running: false, episodes: []};
window.__startJob = {running: true, media_id: 9, title: 'Shouwa Genroku Rakugo Shinjuu', total: 3, current: 0, episodes: [],
                     phase: 'local', current_episode: null, local_episodes: [], step_elapsed: 0};
__api.start_planning_episode_download = async () => { window.__job = JSON.parse(JSON.stringify(window.__startJob)); return JSON.parse(JSON.stringify(window.__job)); };
__api.planning_episode_download_status = async () => JSON.parse(JSON.stringify(window.__job));
window.__step = patch => { window.__job = {...window.__job, ...patch}; };
window.__finish = rows => window.__step({running: false, status: 'done', phase: 'done', current: 3, current_episode: null, finished_at: 1790000000, episodes: rows});
"""
ROWS = [
    {"episode": 1, "status": "local", "error": ""},
    {"episode": 2, "status": "added", "release": "[Group] Show - 02", "error": ""},
    {"episode": 3, "status": "missing", "release": "", "error": ""},
]

SEARCH_WITH_ROWS = SEARCH + f"window.__rows = {json.dumps(ROWS)};"


def _anime(media_id: int, title: str) -> dict:
    return {
        "media_id": media_id,
        "title": title,
        "episodes": 3,
        "media_status": "FINISHED",
        "status": "PLANNING",
        "progress": 0,
        "format": "TV",
        "cover": f"https://covers.invalid/{media_id}.jpg",
    }


PLANNED = {"planned": [_anime(9, "Shouwa Genroku Rakugo Shinjuu"), _anime(10, "Other Show")]}


def test_search_dialog_shows_the_step_and_fills_episode_rows_in_place(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('planned', true); await __tick(20);
            await __click('#plannedContent [data-action="planning-episodes-auto-card"][data-id="9"]'); await __flush();
            const root = $('modalBody').firstElementChild, bar = root.querySelector('progress'), first = root.querySelector('[data-episode-row="1"]');
            const snap = () => ({
              step: __text('[data-job-step]'),
              rows: [...document.querySelectorAll('#modalBody [data-episode-row]')].filter(n => !n.hidden).map(n => [n.dataset.episodeRow, n.className.replace('planning-episode-result ', ''), n.innerText]),
              value: Number(bar.value), max: Number(bar.max), busy: root.querySelector('[data-job-meter]').classList.contains('busy'),
              same: $('modalBody').firstElementChild === root && root.querySelector('progress') === bar && root.querySelector('[data-episode-row="1"]') === first,
            });
            const out = {local: snap()};
            __step({phase: 'batch', local_episodes: [1], step_elapsed: 4.2}); await __tick(950); out.batch = snap();
            __step({phase: 'episodes', current: 2, current_episode: 2, step_elapsed: 12.7, episodes: [{episode: 1, status: 'local', error: ''}]});
            await __tick(950); out.episode2 = snap();
            __step({current: 3, current_episode: 3, step_elapsed: 0.4, episodes: [{episode: 1, status: 'local', error: ''}, {episode: 2, status: 'added', release: '[Group] Show - 02', error: ''}]});
            await __tick(950); out.episode3 = snap();
            __finish(window.__rows); await __tick(950);
            out.done = {...snap(), title: __text('#modalTitle'), summary: __text('[data-job-summary]'), progressShown: __visible('[data-job-progress]')};
            out.t = {local: PudgeI18n.t('planned.searchStepLocal'), batch: PudgeI18n.t('planned.searchStepBatch'),
                     ep2: PudgeI18n.t('planned.searchStepEpisode', {episode: 2, total: 3}), ep3: PudgeI18n.t('planned.searchStepEpisode', {episode: 3, total: 3}),
                     s4: PudgeI18n.t('planned.searchStepElapsed', {seconds: 4}), s12: PudgeI18n.t('planned.searchStepElapsed', {seconds: 12}),
                     waiting: PudgeI18n.t('planned.searchEpisodePending'), searching: PudgeI18n.t('planned.searchEpisodeSearching'),
                     local_: PudgeI18n.t('ui.alreadyLocal'), added: PudgeI18n.t('ui.added'), missing: PudgeI18n.t('ui.notFound'), ep: t('table.episode'),
                     complete: PudgeI18n.t('ui.searchComplete'), summary: PudgeI18n.t('ui.addedAlreadyLocalNotFound', {added: 1, local: 1, missing: 1, error: 0})};
            return out;
            """,
            lang=lang,
            state=PLANNED,
            before=SEARCH_WITH_ROWS,
        )
        out, t = result.value, result.value["t"]
        row = lambda n, status, *text: [str(n), status, "\n".join([f"{t['ep']} {n}", *text])]  # noqa: E731
        assert out["local"] == {
            "step": t["local"],
            "rows": [
                row(1, "pending", t["waiting"]),
                row(2, "pending", t["waiting"]),
                row(3, "pending", t["waiting"]),
            ],
            "value": 0,
            "max": 3,
            "busy": True,
            "same": True,
        }
        assert out["batch"]["step"] == f"{t['batch']} · {t['s4']}"
        assert out["batch"]["rows"][0] == row(1, "local", t["local_"]) and out["batch"]["value"] == 1
        assert out["episode2"]["step"] == f"{t['ep2']} · {t['s12']}"
        assert out["episode2"]["rows"][1] == row(2, "searching", t["searching"])
        assert out["episode2"]["rows"][2] == row(3, "pending", t["waiting"]) and out["episode2"]["value"] == 1
        assert out["episode3"]["step"] == t["ep3"]  # under a second: no elapsed time yet
        assert out["episode3"]["rows"][1] == row(2, "added", t["added"], "[Group] Show - 02")
        assert (
            out["episode3"]["rows"][2] == row(3, "searching", t["searching"])
            and out["episode3"]["value"] == 2
        )
        for step in ("batch", "episode2", "episode3"):
            assert out[step]["same"] and out[step]["busy"], step
        done = out["done"]
        assert done["title"] == f"{t['complete']} — Shouwa Genroku Rakugo Shinjuu"
        assert done["summary"] == t["summary"] and done["progressShown"] is False
        assert done["rows"] == [
            row(1, "local", t["local_"]),
            row(2, "added", t["added"], "[Group] Show - 02"),
            row(3, "missing", t["missing"]),
        ]
        assert done["same"] is True and done["busy"] is False and done["value"] == 3
        assert result.calls_of("planning_episode_download_status")[:1] == [[]]


def test_starting_polling_and_finishing_the_search_do_not_rebuild_planning_cards(page) -> None:
    result = page.run(
        r"""
        setPage('planned', true); await __tick(20);
        const card = id => document.querySelector(`#plannedContent .anime-card[data-media-id="${id}"]`);
        const img = id => card(id)?.querySelector('img[data-pudge-cover-image]');
        const before = {card9: card(9), card10: card(10), img9: img(9), img10: img(10)};
        await __click('#plannedContent [data-action="planning-episodes-auto-card"][data-id="9"]'); await __flush();
        const out = {started: card(9) === before.card9 && card(10) === before.card10};
        // Python's library scan and added torrents bump ui_state_version: a push, then get_state_fast.
        const fast = __api.get_state_fast, full = __api.get_state;
        __api.get_state_fast = async () => ({...(await fast()), ui_state_version: '2'});
        __api.ui_state_versions = {ready: '1', ui: '2'};
        window.PudgePush('versions', {ready: '1', ui: '2'}); await __tick(50);
        out.pushSynced = __apiCalls.filter(call => call.name === 'get_state_fast').length;
        out.afterPush = card(9) === before.card9 && card(10) === before.card10 && img(9) === before.img9 && img(10) === before.img10;
        __step({phase: 'episodes', current_episode: 2, local_episodes: [1]}); await __tick(950);
        out.polling = card(9) === before.card9 && card(10) === before.card10;
        // When it is done Python hides the searched card's button (it now has downloads).
        __api.get_state = async () => { const state = await full(); state.ui_state_version = '2'; state.planned[0].planning_download_hidden = true; return state; };
        __finish(window.__rows); await __tick(950);
        out.finished = {reloaded: __apiCalls.filter(call => call.name === 'get_state').length,
                        card10: card(10) === before.card10, img10: img(10) === before.img10,
                        card9Replaced: card(9) !== before.card9, img9: img(9) === before.img9,
                        button9: Boolean(card(9).querySelector('[data-action="planning-episodes-auto-card"]')),
                        count: document.querySelectorAll('#plannedContent .anime-card').length};
        return out;
        """,
        state=PLANNED,
        before=SEARCH_WITH_ROWS,
    )
    out = result.value
    assert out["started"] is True
    assert out["pushSynced"] >= 1 and out["afterPush"] is True
    assert out["polling"] is True
    finished = out["finished"]
    assert finished["reloaded"] >= 2  # start-up, then the search result
    assert finished["card10"] and finished["img10"]  # untouched card: same node, same cover
    assert finished["card9Replaced"] and finished["button9"] is False
    assert finished["img9"] is True  # the changed card keeps its loaded cover
    assert finished["count"] == 2


def test_sidebar_line_follows_a_search_with_its_dialog_closed(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('planned', true); await __tick(20);
            await __click('#plannedContent [data-action="planning-episodes-auto-card"][data-id="9"]'); await __flush();
            const out = {whileOpen: __visible('#statusTask')};
            await __click('#modalClose'); await __flush();
            out.closed = __text('#statusTask');
            setStatus(t('status.refreshing'));
            __step({phase: 'episodes', current: 2, current_episode: 2, local_episodes: [1], episodes: [{episode: 1, status: 'local', error: ''}]});
            await __tick(950);
            out.running = {line: __text('#statusTask'), status: __text('#status'), tooltip: $('statusTask').title};
            setPage('current', true); await __tick(20);
            out.afterSwitch = __text('#statusTask');
            await __click('#statusTask'); await __tick(20);
            out.reopened = {open: $('modalBackdrop').classList.contains('open'), title: __text('#modalTitle'), step: __text('[data-job-step]'), line: __visible('#statusTask')};
            await __click('#modalClose'); await __flush();
            __finish(window.__rows); await __tick(950);
            out.finished = {line: __text('#statusTask'), open: $('modalBackdrop').classList.contains('open'), error: $('statusTask').classList.contains('error')};
            await __tick(STATUS_CLEAR_MS + 1000);
            out.cleared = __visible('#statusTask');
            out.t = {start: PudgeI18n.t('planned.searchStatusRunning', {title: 'Shouwa Genroku…', done: 0, total: 3}),
                     running: PudgeI18n.t('planned.searchStatusRunning', {title: 'Shouwa Genroku…', done: 2, total: 3}),
                     refreshing: t('status.refreshing'), searching: PudgeI18n.t('ui.searchingReleasedEpisodes'),
                     step: PudgeI18n.t('planned.searchStepEpisode', {episode: 2, total: 3}),
                     done: PudgeI18n.t('planned.searchStatusDone', {title: 'Shouwa Genroku…', added: 1, missing: 1})};
            return out;
            """,
            lang=lang,
            state=PLANNED,
            settle=30000,  # start-up's own status texts are over
            before=SEARCH_WITH_ROWS,
        )
        out, t = result.value, result.value["t"]
        assert out["whileOpen"] is False  # the dialog itself shows the search
        assert out["closed"] == t["start"]
        # Its own line: the status text above it is not overwritten, nor does it overwrite it.
        assert out["running"] == {
            "line": t["running"],
            "status": t["refreshing"],
            "tooltip": "Shouwa Genroku Rakugo Shinjuu",
        }
        assert out["afterSwitch"] == t["running"]
        assert out["reopened"]["open"] is True and out["reopened"]["line"] is False
        assert out["reopened"]["title"] == f"{t['searching']} — Shouwa Genroku Rakugo Shinjuu"
        assert out["reopened"]["step"] == t["step"]
        # Finished while closed: no dialog pops up, the line holds the summary for a while.
        assert out["finished"] == {"line": t["done"], "open": False, "error": False}
        assert out["cleared"] is False


def test_a_search_still_running_after_a_window_reload_shows_in_the_sidebar(page) -> None:
    result = page.run(
        r"""
        const out = {line: __text('#statusTask'), open: $('modalBackdrop').classList.contains('open')};
        await __tick(2000);
        out.polls = __apiCalls.filter(call => call.name === 'planning_episode_download_status').length;
        out.later = __text('#statusTask');
        out.t = {line: PudgeI18n.t('planned.searchStatusRunning', {title: 'Show', done: 0, total: 12}),
                 later: PudgeI18n.t('planned.searchStatusRunning', {title: 'Show', done: 5, total: 12})};
        return out;
        """,
        before=r"""
        let calls = 0;
        __api.planning_episode_download_status = async () => ({running: true, title: 'Show', total: 12, current: 0, episodes: [],
          phase: ++calls < 2 ? 'batch' : 'episodes', current_episode: calls < 2 ? null : 5, local_episodes: []});
        """,
    )
    out = result.value
    assert out["open"] is False and out["line"] == out["t"]["line"]
    assert out["polls"] >= 3 and out["later"] == out["t"]["later"]


def test_progress_motion_stops_with_reduced_motion() -> None:
    rules = css_rules("media.css")
    reduce = "(prefers-reduced-motion:reduce)"
    assert "planning-episode-sweep" in rules.get(".planning-episode-meter.busy::after", "animation")
    assert "planning-episode-pulse" in rules.get(".planning-episode-result.searching", "animation")
    assert rules.get(".planning-episode-meter.busy::after", "animation", media=reduce) == "none"
    assert rules.get(".planning-episode-result.searching", "animation", media=reduce) == "none"
    status = css_rules("index.html").declarations(".status-task")
    assert status["border"] == "0" and status["background"] == "transparent"
