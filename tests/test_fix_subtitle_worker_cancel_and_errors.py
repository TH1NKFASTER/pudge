"""Wave-4 follow-ups to WEBFLOW-12: the background subtitle worker stops for "Update now"
(cancel event, lock polled, checked between jobs), its failures reach the job card, and
a paused supervisor leaves the jobs queued instead of failing the action."""

from __future__ import annotations

import threading
from pathlib import Path

from pudge.maintenance_lock import maintenance_lock
from tests.test_fix_subtitle_actions_off_the_bridge import SlowPreparation, _join, _video, make_api


def _job(api, video: Path):
    return next(row for row in api.manager.db.subtitle_jobs() if row["video_path"] == str(video))


def test_update_preflight_succeeds_while_find_new_waits_for_maintenance(tmp_path: Path, monkeypatch) -> None:
    # Reviewer repro (rev7): the worker sat in maintenance_lock(blocking=True) behind an agent
    # pass, had no cancel event, and "Update now" failed its quiesce.
    api = make_api(tmp_path)
    _video(tmp_path, api, "Show - 01.mkv")
    slow = SlowPreparation()
    slow.release.set()
    monkeypatch.setattr(api.manager, "process_subtitle_jobs", slow)
    monkeypatch.setattr(api, "_stop_scheduled_agent", lambda: None)
    monkeypatch.setattr(api, "_start_scheduled_agent", lambda: None)
    held, done = threading.Event(), threading.Event()

    def agent_pass() -> None:
        with maintenance_lock(api.config.paths.cache_dir, blocking=True):
            held.set()
            done.wait(30)

    agent = threading.Thread(target=agent_pass)
    agent.start()
    try:
        assert held.wait(10)
        api.search_new_subtitles()
        api._prepare_update_shutdown()  # before: RuntimeError (quiesce found the worker)
        assert slow.calls == []  # it stopped instead of preparing during the update
    finally:
        done.set()
        agent.join()
        api.task_supervisor.resume()
        api.close()


def test_worker_stops_between_jobs_when_cancelled(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    first = _video(tmp_path, api, "Show - 01.mkv")
    _video(tmp_path, api, "Show - 02.mkv")
    with api.manager.db.connect() as conn:
        conn.execute("UPDATE subtitle_jobs SET attempts=0,next_check=0")
    slow = SlowPreparation()
    monkeypatch.setattr(api.manager, "process_subtitle_jobs", slow)
    api.search_new_subtitles()
    try:
        assert slow.started.wait(10)
        threading.Timer(0.5, slow.release.set).start()  # the running job ends soon after
        lingering = api.task_supervisor.quiesce(timeout=10)
        assert lingering == []
        assert len(slow.calls) == 1 and slow.calls[0] == [first]  # the second job never started
    finally:
        slow.release.set()
        api.task_supervisor.resume()
        api.close()


def test_a_worker_failure_is_shown_on_the_job(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    video = _video(tmp_path, api, "Show - 03.mkv")

    def broken(limit: int = 4, *, preferred_paths=(), wait_for_slot: bool = False) -> int:
        raise RuntimeError("Jimaku said no")

    monkeypatch.setattr(api.manager, "process_subtitle_jobs", broken)
    before = int(api.manager.db.get_state("ui_state_version", "0"))
    result = api.retry_episode_preparation(str(video))
    assert result["ok"] is True
    _join(api, "manual-subtitle-retry")
    job = _job(api, video)
    assert "Jimaku said no" in str(job["last_error"])
    assert str(job["last_error"]).startswith("Subtitle preparation stopped with an error")
    assert int(api.manager.db.get_state("ui_state_version", "0")) > before  # the card redraws
    api.close()


def test_a_paused_supervisor_leaves_the_jobs_queued(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    video = _video(tmp_path, api, "Show - 04.mkv")
    api.task_supervisor.suspend()  # an update preflight is running
    try:
        result = api.search_new_subtitles()  # before: RuntimeError("task supervisor is suspended")
        assert result["queued"] is True and result["eligible"] == 1
        retry = api.retry_episode_preparation(str(video))
        assert retry["ok"] is True
        assert str(_job(api, video)["state"]) == "pending"
    finally:
        api.task_supervisor.resume()
        api.close()


def test_failure_text_exists_in_both_languages() -> None:
    from tests.i18n_support import en, ru

    assert en("error.jobs.backgroundFailed", error="x") != "error.jobs.backgroundFailed"
    assert ru("error.jobs.backgroundFailed", error="x") not in (
        "error.jobs.backgroundFailed",
        en("error.jobs.backgroundFailed", error="x"),
    )
