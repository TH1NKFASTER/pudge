"""An inactive window saves energy (behaviour, page harness; audit 71Б rewrite): animations
pause, data changes are drawn when the window comes back, background checks slow down."""

from __future__ import annotations

import json

from css_support import css_rules

IDLE = {"startup_maintenance": {"running": False}, "ui_state_versions": {"ready": "1", "ui": "1"}}
SHOW = {"media_id": 7, "title": "Show", "next_episode": 3, "site_url": "https://anilist.co/anime/7"}


def test_inactive_window_pauses_all_css_animations(page) -> None:
    result = page.run(
        """
        const inactive = () => document.documentElement.classList.contains('ui-inactive');
        const out = {start: inactive()};
        window.dispatchEvent(new Event('blur')); out.blurred = inactive();
        window.dispatchEvent(new Event('focus')); out.focused = inactive();
        document.hidden = true; document.visibilityState = 'hidden';
        document.dispatchEvent(new Event('visibilitychange')); out.hidden = inactive();
        return out;
        """,
        api=IDLE,
    )
    assert result.value == {"start": False, "blurred": True, "focused": False, "hidden": True}
    rules = css_rules("index.html")
    for selector in ("html.ui-inactive *", "html.ui-inactive *::before", "html.ui-inactive *::after"):
        assert rules.get(selector, "animation-play-state") == "paused !important", selector


# The backend's state changes when the startup maintenance finishes (Show appears).
STARTUP = """
const afterStartup = async () => ({...(await __api.get_state()), home: {waiting: [SHOW]}});
let started = false;
__api.startup_maintenance = async () => { started = true; return {running: false, state: await afterStartup()}; };
__api.get_state_fast = async () => started ? afterStartup() : __api.get_state();
""".replace("SHOW", json.dumps(SHOW))


def test_background_window_draws_data_changes_only_when_it_comes_back(page) -> None:
    # Pudge started behind other windows: the startup result is kept, not drawn, and
    # appears at once when the user switches to the window.
    result = page.run(
        """
        window.dispatchEvent(new Event('blur'));
        window.dispatchEvent(new Event('pywebviewready'));
        await __tick(3000);
        const background = __text('#currentContent');
        window.dispatchEvent(new Event('focus'));
        const focused = __text('#currentContent');           // before any new state could arrive
        return {background, focused, empty: t('home.emptyExplain')};
        """,
        ready=False,
        before=STARTUP,
        api={"ui_state_versions": {"ready": "1", "ui": "1"}},
    )
    out = result.value
    assert out["empty"] in out["background"] and "Show" not in out["background"]
    assert "Show" in out["focused"]


POLLS = "return __apiCalls.filter(call => call.name === 'poll_downloads_and_subtitles').map(call => call.at);"
HIDDEN = "document.hidden = true; document.visibilityState = 'hidden';"


def test_background_window_checks_downloads_and_subtitles_less_often(page) -> None:
    # Nothing to do: two checks after the start, 15 s apart in front, 2 min apart behind.
    front = page.run(f"await __tick(200000); {POLLS}", api=IDLE)
    behind = page.run(f"await __tick(200000); {POLLS}", api=IDLE, before=HIDDEN)
    assert front.value == [1000, 16000]
    assert behind.value == [1000, 121000]
    assert front.calls_of("poll_downloads_and_subtitles")[0] == [
        "1",
        False,
        True,
    ]  # (version, downloads, visible)
    assert behind.calls_of("poll_downloads_and_subtitles")[0] == ["1", False, False]

    # Subtitles waiting for a check in an hour (the backend's hint): once a minute in front,
    # every 2 min behind; hiding the window reschedules the pending check.
    hinted = page.run(
        """
        __api.poll_downloads_and_subtitles = async () => ({subtitle_poll_hint:
          {active_count: 1, due_count: 0, priority_due_count: 0, next_check: Date.now() / 1000 + 3600}});
        await __tick(200000);
        document.hidden = true; document.visibilityState = 'hidden';
        document.dispatchEvent(new Event('visibilitychange'));
        await __tick(400000);
        """
        + POLLS,
        api=IDLE,
    )
    assert hinted.value == [1000, 61000, 121000, 181000, 320000, 440000, 560000]

    # A download that does not move is still checked once a minute behind.
    download = {"name": "Show 03", "torrent_hash": "abc", "state": "downloading", "progress": 0.5}
    stalled = page.run(
        f"await __tick(200000); {POLLS}", api=IDLE, before=HIDDEN, state={"downloads": [download]}
    )
    gaps = [later - earlier for earlier, later in zip(stalled.value, stalled.value[1:])]
    assert len(gaps) == 3 and max(gaps) == 60000
