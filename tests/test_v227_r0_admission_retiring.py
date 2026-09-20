from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from pudge.task_supervisor import TaskSupervisor
from pudge.web_app import WebAppApi
from pudge.work_scheduler import WorkPriority, WorkScheduler


def _logger() -> SimpleNamespace:
    return SimpleNamespace(
        info=lambda *_args, **_kwargs: None,
        exception=lambda *_args, **_kwargs: None,
    )


def test_debug_fresh_coalesces_same_video_and_queues_distinct_videos(tmp_path: Path) -> None:
    scheduler = WorkScheduler(tmp_path / "scheduler")
    supervisor = TaskSupervisor()
    first_inside = threading.Event()
    second_inside = threading.Event()
    release_first = threading.Event()
    force_calls: list[Path] = []
    process_calls: list[Path] = []
    call_lock = threading.Lock()

    one = (tmp_path / "one.mkv").resolve()
    two = (tmp_path / "two.mkv").resolve()

    def force_fresh(video: Path) -> dict[str, object]:
        force_calls.append(video)
        return {"ok": True, "video_path": str(video)}

    def process_subtitle_jobs(**kwargs) -> int:
        video = Path(kwargs["preferred_paths"][0]).resolve()
        lease = scheduler.acquire_heavy(
            f"test-debug-fresh:{video.name}",
            blocking=True,
            foreground_sensitive=False,
            priority=WorkPriority.USER,
            poll_seconds=0.01,
        )
        assert lease is not None
        with lease:
            with call_lock:
                process_calls.append(video)
            if video == one:
                first_inside.set()
                assert release_first.wait(2)
            else:
                second_inside.set()
        return 1

    fake = SimpleNamespace(
        manager=SimpleNamespace(
            work_scheduler=scheduler,
            force_fresh_subtitle_selection=force_fresh,
            process_subtitle_jobs=process_subtitle_jobs,
        ),
        task_supervisor=supervisor,
        logger=_logger(),
    )

    first = WebAppApi.debug_reselect_subtitles(fake, str(one))
    assert first["ok"] is True
    assert first_inside.wait(1)

    duplicate = WebAppApi.debug_reselect_subtitles(fake, str(one))
    assert duplicate["already_running"] is True
    assert force_calls == [one]

    second = WebAppApi.debug_reselect_subtitles(fake, str(two))
    assert second["ok"] is True
    time.sleep(0.05)
    assert not second_inside.is_set()

    release_first.set()
    assert second_inside.wait(1)
    assert supervisor.shutdown(timeout=2) == []

    assert force_calls == [one, two]
    assert process_calls == [one, two]
    assert scheduler._priority_requests == []
    assert not scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND)


def test_debug_fresh_worker_failure_releases_priority_request(tmp_path: Path) -> None:
    scheduler = WorkScheduler(tmp_path / "scheduler")
    supervisor = TaskSupervisor()
    entered = threading.Event()

    def process_subtitle_jobs(**_kwargs) -> int:
        entered.set()
        raise RuntimeError("synthetic worker failure")

    fake = SimpleNamespace(
        manager=SimpleNamespace(
            work_scheduler=scheduler,
            force_fresh_subtitle_selection=lambda video: {"ok": True, "video_path": str(video)},
            process_subtitle_jobs=process_subtitle_jobs,
        ),
        task_supervisor=supervisor,
        logger=_logger(),
    )

    result = WebAppApi.debug_reselect_subtitles(fake, str(tmp_path / "failure.mkv"))
    assert result["ok"] is True
    assert entered.wait(1)
    assert supervisor.shutdown(timeout=2) == []
    assert scheduler._priority_requests == []
    assert not scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND)


def test_supervisor_tracks_retiring_cooperative_task_until_actual_finish() -> None:
    supervisor = TaskSupervisor()
    old_entered = threading.Event()
    release_old = threading.Event()
    replacement_finished = threading.Event()

    def old_worker(_cancel_event: threading.Event) -> None:
        old_entered.set()
        release_old.wait(2)

    old = supervisor.start(
        "same-name",
        old_worker,
        pass_cancel_event=True,
    )
    assert old_entered.wait(1)

    replacement = supervisor.start_with_admission(
        "same-name",
        lambda _cancel_event: replacement_finished.set(),
        pass_cancel_event=True,
        replace=True,
    )
    assert replacement.admission == "started"
    replacement.task.thread.join(1)
    assert replacement_finished.is_set()

    lingering = supervisor.shutdown(timeout=0.02)
    assert "same-name" in lingering
    assert old.running
    assert any(
        row["name"] == "same-name" and row["retiring"] and row["running"]
        for row in supervisor.status()
    )

    release_old.set()
    old.thread.join(1)
    assert not old.running
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and any(row["retiring"] for row in supervisor.status()):
        time.sleep(0.01)
    assert not any(row["retiring"] for row in supervisor.status())
