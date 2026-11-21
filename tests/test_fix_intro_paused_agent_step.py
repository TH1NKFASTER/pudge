"""Follow-ups to OP/ED detection beside a paused player (review of d45e772).

* The paused-player pass is the agent's own step after ``run_once`` released the maintenance
  lock: Refresh, Repair, Check upgrades and Backup are never kept waiting by it, and an
  interactive or startup refresh never runs beside a paused player.
* A pause first seen by this agent run gets one more look ~35 s later (launchd starts the
  agent every 5 minutes), without holding any lock.
* mpv open for more than 6 hours keeps its foreground marker (refreshed every 10 minutes).
* A no-progress memo never leaves a title alone for more than a day, also after a clock jump.
"""

from __future__ import annotations

import json
import logging
import time
from types import SimpleNamespace

import pytest

from pudge import agent, foreground, intro_skipper, process_priority
from pudge.database import Database
from pudge.foreground import clear_foreground, foreground_active, keep_foreground, mark_foreground
from pudge.maintenance_lock import maintenance_lock
from tests.test_fix_app_lifecycle_safety import _agent_config
from tests.test_fix_intro_detection_paused_mpv import FakeMpv


class AgentManager:
    """What the agent needs of AnimeManager; records when each step ran."""

    def __init__(self, config, events: list, answers: list[dict]) -> None:
        self.config = config
        self.db = Database(config.library.database_path)
        self.events = events
        self.answers = answers

    def anilist_refresh_due(self, now=None) -> bool:
        return False

    def run_once(self) -> dict:
        with maintenance_lock(self.config.paths.cache_dir, blocking=False) as acquired:
            self.events.append(("run_once", bool(acquired)))
        return {"library": 0}

    def run_paused_intro_detection(self) -> dict:
        with maintenance_lock(self.config.paths.cache_dir, blocking=False) as free:
            self.events.append(("paused_step", bool(free)))
        return self.answers.pop(0) if self.answers else {"status": "ran", "analysed": 1}


@pytest.fixture
def paused_player(tmp_path, monkeypatch):
    monkeypatch.setattr(foreground, "PAUSE_SETTLE_SECONDS", 0.0)
    foreground._playback_cache.clear()
    lowered: list[bool] = []
    monkeypatch.setattr(
        process_priority, "lower_current_process", lambda *a, **k: lowered.append(True) or True
    )
    monkeypatch.setattr(agent, "PAUSED_FOLLOW_UP_SECONDS", 0.01)
    monkeypatch.setattr(agent, "app_session_active", lambda: True)
    monkeypatch.setattr(agent, "app_session_window_active", lambda: False)
    monkeypatch.setattr(agent, "configure_logging", lambda: logging.getLogger("pudge.test.agent"))
    mpv = FakeMpv(pause=True)
    yield SimpleNamespace(mpv=mpv, lowered=lowered)
    mpv.close()
    foreground._playback_cache.clear()


def _run_agent(tmp_path, monkeypatch, *, last_run: float, answers: list[dict], mpv: FakeMpv) -> list:
    config = _agent_config(tmp_path, last_run=last_run)
    mark_foreground(config.paths.cache_dir, video=tmp_path / "x.mkv", mpv_ipc=mpv.path)
    events: list = []
    monkeypatch.setattr(agent, "load_config", lambda _path, **_kwargs: config)
    monkeypatch.setattr(agent, "AnimeManager", lambda cfg: AgentManager(cfg, events, answers))
    try:
        assert agent.main(["--scheduled", "--config", str(config.config_path)]) == 0
    finally:
        clear_foreground(config.paths.cache_dir)
    return events


def test_paused_step_runs_after_the_maintenance_lock_is_released(
    tmp_path, monkeypatch, paused_player
) -> None:
    events = _run_agent(tmp_path, monkeypatch, last_run=0, answers=[], mpv=paused_player.mpv)
    # run_once took the lock; the paused step found it free (Refresh/Repair/Backup are not blocked).
    assert events == [("run_once", True), ("paused_step", True)]
    assert paused_player.lowered == [True]  # the agent lowers itself first


def test_paused_player_wakes_the_agent_when_nothing_else_is_due(tmp_path, monkeypatch, paused_player) -> None:
    answers = [{"status": "deferred", "reason": "paused_recently"}, {"status": "ran", "analysed": 3}]
    events = _run_agent(tmp_path, monkeypatch, last_run=time.time(), answers=answers, mpv=paused_player.mpv)
    # No run_once (not due); one follow-up look after a pause too young to count.
    assert events == [("paused_step", True), ("paused_step", True)]


def test_no_paused_step_without_a_paused_player(tmp_path, monkeypatch, paused_player) -> None:
    paused_player.mpv.state["pause"] = False
    events = _run_agent(tmp_path, monkeypatch, last_run=0, answers=[], mpv=paused_player.mpv)
    assert events == [("run_once", True)]


def test_manager_maintenance_pass_never_opts_in(tmp_path, monkeypatch) -> None:
    from tests.test_manager import _upgrade_manager

    manager = _upgrade_manager(tmp_path)
    calls: list[bool] = []
    monkeypatch.setattr(
        intro_skipper,
        "run_background_pass",
        lambda **k: calls.append(k["paused_ok"]) or {"status": "ran", "analysed": 1},
    )
    assert manager.run_intro_detection() == 1
    assert manager.run_paused_intro_detection() == {"status": "no_player"}  # nothing to do
    mark_foreground(manager.config.paths.cache_dir, video=tmp_path / "x.mkv")
    try:
        assert manager.run_paused_intro_detection()["analysed"] == 1
    finally:
        clear_foreground(manager.config.paths.cache_dir)
    assert calls == [False, True]


def test_marker_of_a_long_playback_stays_while_refreshed(tmp_path, monkeypatch) -> None:
    cache_dir = tmp_path / "cache"
    marker = mark_foreground(cache_dir, video=tmp_path / "x.mkv")
    payload = json.loads(marker.read_text())
    payload["created_at"] = time.time() - 7 * 3600  # mpv opened 7 hours ago
    marker.write_text(json.dumps(payload))
    assert not foreground_active(cache_dir)  # never refreshed: stale, removed
    marker = mark_foreground(cache_dir, video=tmp_path / "x.mkv")
    payload = json.loads(marker.read_text())
    payload["created_at"] = time.time() - 7 * 3600
    marker.write_text(json.dumps(payload))
    stop = keep_foreground(cache_dir, interval=0.05)
    try:
        time.sleep(0.3)
        assert json.loads(marker.read_text())["refreshed_at"] > time.time() - 5
        assert foreground_active(cache_dir)
        # The identity (and the pause key) stays the one of the session.
        assert json.loads(marker.read_text())["created_at"] == payload["created_at"]
    finally:
        stop()
        clear_foreground(cache_dir)


def test_no_progress_memo_is_clamped_to_a_day_when_read(tmp_path, monkeypatch) -> None:
    np = pytest.importorskip("numpy")  # noqa: F841 - the pass imports it
    from tests.test_intro_skipper import FakeScheduler

    monkeypatch.setenv("PUDGE_INTRO_DETECTION", "1")
    monkeypatch.setattr(intro_skipper, "tools_missing", lambda *_a: [])
    cache = intro_skipper.IntroSkipperCache(tmp_path / "cache")
    cache.root.mkdir(parents=True)
    far = time.time() + 400 * 86400  # written before the clock was set back a year
    (cache.root / intro_skipper.NO_PROGRESS_FILE).write_text(
        json.dumps({"7": {"signature": "x", "until": far}})
    )
    db = SimpleNamespace(anime_list=lambda *a: [], episodes=lambda *a: [])
    intro_skipper.run_background_pass(
        db=db, cache_dir=tmp_path / "cache", ffmpeg="f", ffprobe="p", scheduler=FakeScheduler()
    )
    memo = json.loads((cache.root / intro_skipper.NO_PROGRESS_FILE).read_text())
    assert memo["7"]["until"] <= time.time() + intro_skipper.NO_PROGRESS_RECHECK_SECONDS + 1


def test_paused_player_on_battery_lets_the_agent_exit_early(tmp_path, monkeypatch, paused_player) -> None:
    from pudge.work_scheduler import WorkScheduler
    from tests.test_fix_intro_detection_paused_mpv import _power

    monkeypatch.setattr(
        agent,
        "_paused_scheduler",
        lambda config: WorkScheduler(config.paths.cache_dir, power_policy=_power("Battery Power")),
    )
    config = _agent_config(tmp_path, last_run=time.time())  # nothing else due
    mark_foreground(config.paths.cache_dir, video=tmp_path / "x.mkv", mpv_ipc=paused_player.mpv.path)
    monkeypatch.setattr(agent, "load_config", lambda _path, **_kwargs: config)
    monkeypatch.setattr(
        agent, "AnimeManager", lambda _cfg: (_ for _ in ()).throw(AssertionError("manager built on battery"))
    )
    try:
        assert agent.main(["--scheduled", "--config", str(config.config_path)]) == 0
    finally:
        clear_foreground(config.paths.cache_dir)
