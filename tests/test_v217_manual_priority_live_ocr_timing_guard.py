from __future__ import annotations

import threading
import time
from pathlib import Path

from pudge.models import SubtitleCandidate
from pudge.syncing import _embedded_rank_regression_guard
from pudge.work_scheduler import WorkPriority, WorkScheduler


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
    while time.monotonic() < deadline and not scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND):
        time.sleep(0.01)
    background.release()
    assert acquired.wait(2)
    # A background retry must not jump ahead while the manual request/lease lives.
    assert scheduler.acquire_heavy(
        "background-retry",
        blocking=False,
        foreground_sensitive=False,
        priority=WorkPriority.BACKGROUND,
    ) is None
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


def test_v84_source_contracts_are_wired_to_real_paths() -> None:
    root = Path(__file__).resolve().parents[1]
    manga = (root / "pudge/manga.py").read_text(encoding="utf-8")
    web = (root / "pudge/web_app.py").read_text(encoding="utf-8")
    js = (root / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    syncing = (root / "pudge/syncing.py").read_text(encoding="utf-8")
    assert '"preempted": True' in manga
    assert "should_yield(WorkPriority.BACKGROUND)" in manga
    assert "begin_priority_request(" in web and "WorkPriority.USER" in web
    debug = web[web.index("def debug_reselect_subtitles"):]
    debug = debug[: debug.find("\n    def ", 10)]
    assert "with maintenance_lock" not in debug
    assert "wait_for_slot=True" in debug
    assert "status?.prepared_pages" in js
    assert "embedded_rank_over_weaker_speech" in syncing
