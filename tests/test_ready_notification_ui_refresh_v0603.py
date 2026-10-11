"""The open window keeps Home current cheaply (behaviour, page harness; audit 71Б rewrite).

A foreground poll redraws when an episode's state changes, not only download progress;
the window asks for the tiny state versions every second and fetches the state only when
they change; focus resynchronizes at once."""

from __future__ import annotations

import json

IDLE = {"startup_maintenance": {"running": False}}
CARD = {
    "media_id": 7,
    "title": "Show",
    "episodes": 12,
    "next_episode": 3,
    "site_url": "https://anilist.co/anime/7",
}


def _episode(state: str, subtitle_source: str = "", video_path: str = "") -> dict:
    return {"episode": 3, "state": state, "subtitle_source": subtitle_source, "video_path": video_path}


def test_foreground_poll_redraws_when_an_episode_changes_without_download_progress(page) -> None:
    # Subtitles found for a downloaded episode: no download moved, so Home used to stay
    # on "Waiting for subs" until something else redrew it.
    waiting = _episode("waiting_subtitles", "jimaku", "/lib/Show - 03.mkv")
    steps = [
        # (episode row of the library group, Home section the backend puts the card in); one field changes each time
        (_episode("ready", "jimaku", "/lib/Show - 03.mkv"), "new_ready"),  # state
        (_episode("ready", "embedded", "/lib/Show - 03.mkv"), "waiting"),  # subtitle source
        (_episode("ready", "embedded", "/lib/[Better] Show - 03.mkv"), "new_ready"),  # video file
    ]
    result = page.run(
        """
        await __tick(2000);
        const home = () => ({text: __text('#currentContent'), play: document.querySelector('#currentContent [data-play-path]')?.dataset.playPath ?? null});
        const seen = [home()];
        for (const [row, section] of STEPS) {
          const card = {...CARD, local: row};
          __api.poll_downloads_and_subtitles = async () => ({state: {...ui.state, home: {[section]: [card]},
                                                                     library: [{media_id: 7, episodes: [row]}]}});
          await pollForegroundWork();
          seen.push(home());
        }
        return {seen, ready: t('section.newReady'), waiting: t('section.waitingPreparation')};
        """.replace("STEPS", json.dumps(steps)).replace("CARD", json.dumps(CARD)),
        lang="ru",
        state={
            "home": {"waiting": [{**CARD, "local": waiting}]},
            "library": [{"media_id": 7, "episodes": [waiting]}],
        },
        api={**IDLE, "ui_state_versions": {"ready": "1", "ui": "1"}},
    )
    seen, ready, waiting_title = result.value["seen"], result.value["ready"], result.value["waiting"]
    assert seen[0]["text"].startswith(waiting_title) and seen[0]["play"] is None
    assert seen[1]["text"].startswith(ready) and seen[1]["play"] == "/lib/Show - 03.mkv"
    assert seen[2]["text"].startswith(waiting_title)
    assert seen[3]["text"].startswith(ready) and seen[3]["play"] == "/lib/[Better] Show - 03.mkv"


# ui_state_versions answers window.__versions; get_state_fast answers a state with Show waiting.
VERSIONS = """
window.__versions = {ready: '1', ui: '1'};
__api.ui_state_versions = async () => window.__versions;
__api.get_state_fast = async () => {
  const state = await __api.get_state();
  return {...state, ready_state_version: window.__versions.ready, ui_state_version: window.__versions.ui,
          home: {...state.home, waiting: [{media_id: 7, title: 'Show', next_episode: 3, site_url: 'https://anilist.co/anime/7'}]}};
};
"""
COUNTS = """
const counts = () => Object.fromEntries(['ui_state_versions', 'get_state_fast', 'get_state']
  .map(name => [name, __apiCalls.filter(call => call.name === name).length]));
const delta = (later, earlier) => Object.fromEntries(Object.keys(later).map(name => [name, later[name] - earlier[name]]));
"""


def test_open_window_checks_the_tiny_versions_and_fetches_state_only_when_they_change(page) -> None:
    result = page.run(
        COUNTS
        + """
        await __tick(2000);
        const start = counts();
        await __tick(10000);
        const unchanged = delta(counts(), start);
        const shownBefore = __text('#currentContent').includes('Show');
        window.__versions = {ready: '1', ui: '2'};          // a library change
        let mark = counts();
        await __tick(1000);
        const changed = delta(counts(), mark);
        const shownAfter = __text('#currentContent').includes('Show');
        window.__versions = {ready: '2', ui: '2'};          // a new ready episode
        mark = counts();
        await __tick(1000);
        const ready = delta(counts(), mark);
        const quiet = counts();
        await __tick(600000);                       // ten idle minutes
        return {unchanged, shownBefore, changed, shownAfter, ready, idle: delta(counts(), quiet)};
        """,
        before=VERSIONS,
        api=IDLE,
    )
    out = result.value
    assert out["unchanged"] == {"ui_state_versions": 10, "get_state_fast": 0, "get_state": 0}
    assert out["changed"] == out["ready"] == {"ui_state_versions": 1, "get_state_fast": 1, "get_state": 0}
    assert out["shownBefore"] is False and out["shownAfter"] is True
    # No periodic rebuild of the whole state while nothing changes.
    assert out["idle"] == {"ui_state_versions": 600, "get_state_fast": 0, "get_state": 0}


def test_permissions_are_requested_at_every_start_and_announced_only_the_first_time(page) -> None:
    seen = {}
    for requested in (False, True):
        result = page.run(
            "await __tick(100); return {toast: __text('#toast'), text: t('toast.permissionsRequested')};",
            state={"settings": {"permissions_requested": requested}},
            api={
                **IDLE,
                "ui_state_versions": {"ready": "1", "ui": "1"},
                "request_permissions": {"settings": {"permissions_requested": True}},
            },
        )
        assert result.calls_of("request_permissions") == [[]]
        seen[requested] = result.value
    assert seen[False]["toast"] == seen[False]["text"]
    assert seen[True]["toast"] == ""


def test_focus_resynchronizes_at_once_and_restores_the_one_second_version_check(page) -> None:
    # In the background the version check is skipped; the next focus fetches the state
    # immediately (versions changed or not) and the 1 s check resumes.
    result = page.run(
        COUNTS
        + """
        const setHidden = hidden => {
          document.hidden = hidden; document.visibilityState = hidden ? 'hidden' : 'visible';
          document.dispatchEvent(new Event('visibilitychange'));
        };
        await __tick(2000);
        setHidden(true);
        const hiddenAt = counts();
        await __tick(127000);
        const hidden = delta(counts(), hiddenAt);
        const shownAt = counts(), callsBefore = __apiCalls.length;
        setHidden(false);
        await __flush();
        const focus = delta(counts(), shownAt);
        await __tick(5000);
        const since = __apiCalls.slice(callsBefore).filter(call => call.name === 'ui_state_versions');
        const checks = since.map(call => call.at - since[0].at);       // ms after the focus
        return {hidden, focus, checks};
        """,
        before=VERSIONS,
        api=IDLE,
    )
    out = result.value
    assert out["hidden"] == {"ui_state_versions": 0, "get_state_fast": 0, "get_state": 0}
    assert out["focus"] == {"ui_state_versions": 1, "get_state_fast": 1, "get_state": 0}
    assert out["checks"] == [0, 1000, 2000, 3000, 4000, 5000]
