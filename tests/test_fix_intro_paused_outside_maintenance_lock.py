"""Reviewer repro (review of d45e772), kept as a regression test."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from pudge import foreground, intro_skipper
from pudge.foreground import clear_foreground, mark_foreground
from pudge.maintenance_lock import maintenance_lock
from pudge.intro_skipper import run_background_pass
from tests.test_fix_intro_detection_paused_mpv import FakeMpv, _scheduler


def test_paused_mpv_holds_maintenance_lock_for_the_settle_wait(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PUDGE_INTRO_DETECTION", "1")
    monkeypatch.setattr(intro_skipper, "tools_missing", lambda *_a: [])
    monkeypatch.setattr(intro_skipper, "_register_and_enforce", lambda *a: None)
    foreground._playback_cache.clear()
    cache_dir = tmp_path / "cache"
    mpv = FakeMpv(pause=True)
    mark_foreground(cache_dir, video=tmp_path / "x.mkv", mpv_ipc=mpv.path)
    scheduler = _scheduler(cache_dir)
    db = SimpleNamespace(anime_list=lambda *a: [], episodes=lambda *a: [])
    seen: list[bool] = []

    def other_user_action() -> None:
        time.sleep(0.5)  # a user action while the pass runs
        with maintenance_lock(cache_dir, blocking=False) as acquired:
            seen.append(bool(acquired))

    try:
        with maintenance_lock(cache_dir, blocking=False) as acquired:  # what run_once holds
            assert acquired
            t = threading.Thread(target=other_user_action)
            t.start()
            started = time.monotonic()
            result = run_background_pass(
                db=db, cache_dir=cache_dir, ffmpeg="ffmpeg", ffprobe="ffprobe", scheduler=scheduler
            )
            elapsed = time.monotonic() - started
            t.join()
        print("result", result, "elapsed %.1f" % elapsed, "other action got lock:", seen)
        assert elapsed < 5, elapsed
    finally:
        clear_foreground(cache_dir)
        mpv.close()
