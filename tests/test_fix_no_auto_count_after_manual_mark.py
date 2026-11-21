"""After Ctrl+A counted the episode, mpv does not try to count it again.

User: "while watching SBR I marked the episode finished; after that the app tried twice more
to mark it finished and could not". Ctrl+A counted episode 4 (the log has one AniList
update), but only the automatic path's own flag stopped automatic attempts. Past the watched
point the tracker asked again; the watch-time check (Ctrl+A skips it) answered "not counted"
and was asked again every 30 s, each time with "AniList: not counted" on screen.
"""

from __future__ import annotations

from pathlib import Path

from tests.lua_harness import needs_lua
from tests.test_anilist_lua import of, tracker, tracking_env

DEFERRED = r"'ANILIST_DEFERRED:1\nOSD: not counted yet\n'"


@needs_lua
def test_ctrl_a_before_the_watched_point_ends_automatic_attempts(tmp_path: Path) -> None:
    log = tracker(
        tmp_path,
        f"""
        sim.set('duration', 1440); sim.set('time-pos', 1000)
        sim.fire('file-loaded')
        sim.press('Ctrl+a')                    -- counted by hand at 69 %
        sim_stdout = {DEFERRED}                -- what an automatic attempt would get now
        sim.play(400)                          -- past the watched point to the end
        """,
        tracking_env(tmp_path, auto=True),
    )
    updates = of(log, "subprocess", "--anilist-action update")
    assert len(updates) == 1 and updates[0].endswith("--manual"), updates
    assert of(log, "osd", "not counted") == []


@needs_lua
def test_ctrl_a_after_a_deferred_attempt_ends_the_retries(tmp_path: Path) -> None:
    log = tracker(
        tmp_path,
        f"""
        sim.set('duration', 1440); sim.set('time-pos', 1250)
        sim_stdout = {DEFERRED}
        sim.fire('file-loaded')
        sim.play(5)                            -- automatic attempt: not counted, retry in 30 s
        sim_stdout = ''
        sim.press('Ctrl+a')
        sim_stdout = {DEFERRED}
        sim.play(150)
        """,
        tracking_env(tmp_path, auto=True),
    )
    updates = of(log, "subprocess", "--anilist-action update")
    assert len(updates) == 2 and "--manual" not in updates[0] and updates[1].endswith("--manual")
    assert len(of(log, "osd", "not counted")) == 1
