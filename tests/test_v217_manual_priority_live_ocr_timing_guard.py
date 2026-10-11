"""Manual work goes first: a user request makes background heavy work yield, Debug fresh waits for its slot even during maintenance; a weaker speech
match cannot replace a strong embedded winner (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from pudge.foreground import mark_foreground
from pudge.maintenance_lock import maintenance_lock
from pudge.models import SubtitleCandidate
from pudge.syncing import _embedded_rank_regression_guard
from pudge.task_supervisor import TaskSupervisor
from pudge.web_app import WebAppApi
from pudge.work_scheduler import WorkPriority, WorkScheduler
from t71g08_support import subtitle_library


def test_user_priority_request_makes_running_background_cooperatively_yield(tmp_path: Path) -> None:
    scheduler = WorkScheduler(tmp_path)
    background = scheduler.acquire_heavy(
        "background-test",
        blocking=True,
        foreground_sensitive=False,
        priority=WorkPriority.BACKGROUND,
    )
    assert background is not None
    token = scheduler.begin_priority_request(WorkPriority.USER, name="manual-test")
    try:
        assert scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND)
        assert not scheduler.should_yield_to_higher_priority(WorkPriority.USER)
    finally:
        scheduler.end_priority_request(token)
        background.release()


def test_user_waiter_gets_heavy_slot_before_background_reacquire(tmp_path: Path) -> None:
    scheduler = WorkScheduler(tmp_path)
    background = scheduler.acquire_heavy(
        "background-first",
        blocking=True,
        foreground_sensitive=False,
        priority=WorkPriority.BACKGROUND,
    )
    assert background is not None
    token = scheduler.begin_priority_request(WorkPriority.USER, name="manual-test")
    acquired = threading.Event()
    release_user = threading.Event()

    def user() -> None:
        lease = scheduler.acquire_heavy(
            "manual-test",
            blocking=True,
            foreground_sensitive=False,
            priority=WorkPriority.USER,
            poll_seconds=0.02,
        )
        assert lease is not None
        acquired.set()
        release_user.wait(2)
        lease.release()

    thread = threading.Thread(target=user, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not scheduler.should_yield_to_higher_priority(
        WorkPriority.BACKGROUND
    ):
        time.sleep(0.01)
    background.release()
    assert acquired.wait(2)
    # A background retry must not jump ahead while the manual request/lease lives.
    assert (
        scheduler.acquire_heavy(
            "background-retry",
            blocking=False,
            foreground_sensitive=False,
            priority=WorkPriority.BACKGROUND,
        )
        is None
    )
    release_user.set()
    thread.join(2)
    scheduler.end_priority_request(token)


def _candidate(tmp_path: Path, name: str, *, score: float = 80.0) -> SubtitleCandidate:
    return SubtitleCandidate(
        path=tmp_path / name,
        source="jimaku",
        score=score,
        name=name,
        episode=10,
        details={
            "episode_match": "exact",
            "entry_anilist_id": 200637,
            "requested_anilist_id": 200637,
        },
    )


def test_weaker_speech_cannot_replace_different_strong_exact_embedded_winner(tmp_path: Path) -> None:
    protected = _candidate(tmp_path, "protected.srt")
    risky = _candidate(tmp_path, "risky.srt")
    item = (
        (1.0,),
        protected,
        tmp_path / "protected-aligned.srt",
        {"sync_was_successful": True},
        {"available": True, "weighted": 0.9617},
        {"reason": "ok"},
    )
    chosen, meta = _embedded_rank_regression_guard(
        [item],
        risky,
        {"available": True, "weighted": 0.7837},
    )
    assert chosen is item
    assert meta["accepted"] is True
    assert meta["activity_regression"] > 0.17


def test_small_activity_difference_does_not_disable_speech_verification(tmp_path: Path) -> None:
    protected = _candidate(tmp_path, "protected.srt")
    risky = _candidate(tmp_path, "risky.srt")
    item = (
        (1.0,),
        protected,
        tmp_path / "protected-aligned.srt",
        {"sync_was_successful": True},
        {"available": True, "weighted": 0.95},
        {"reason": "ok"},
    )
    chosen, meta = _embedded_rank_regression_guard(
        [item],
        risky,
        {"available": True, "weighted": 0.91},
    )
    assert chosen is None
    assert meta["accepted"] is False


def test_debug_fresh_waits_for_its_slot_as_user_work_even_during_maintenance(
    tmp_path: Path, monkeypatch
) -> None:
    manager, video, commands = subtitle_library(tmp_path, monkeypatch)
    scheduler = manager.work_scheduler
    supervisor = TaskSupervisor()
    window = SimpleNamespace(
        manager=manager,
        task_supervisor=supervisor,
        logger=SimpleNamespace(info=lambda *_a, **_k: None, exception=lambda *_a, **_k: None),
    )
    # An episode plays, a volume OCR holds the heavy-work slot and maintenance runs.
    playback = mark_foreground(scheduler.cache_dir)
    ocr = scheduler.acquire_heavy("manga-ocr-book", foreground_sensitive=False)
    assert ocr is not None
    with maintenance_lock(manager.config.paths.cache_dir) as maintenance:
        assert maintenance is True
        answer = WebAppApi.debug_reselect_subtitles(window, str(video))
        time.sleep(0.3)
        # From the click on, the running volume OCR is asked to yield to the user's request.
        waiting = [(scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND), list(commands))]
        playback.unlink()
        time.sleep(0.7)
        waiting.append((scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND), list(commands)))
        ocr.release()
        deadline = time.monotonic() + 5
        while not commands and time.monotonic() < deadline:
            time.sleep(0.05)
    assert supervisor.shutdown(timeout=5) == []
    assert answer["ok"] is True
    assert waiting == [(True, []), (True, [])]
    [command] = commands
    assert "--force-search" in command and "--resync" in command
    assert manager.db.episode_by_path(video).state == "ready"
    # The request ends with the job: background work no longer yields.
    assert not scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND)
