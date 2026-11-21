"""MEM3: what the Mac's memory lines named after oct31b.

* Low RAM mode: the AniList list (anilist.library +105 MB) and OP/ED detection (numpy, audio
  fingerprints) run in short-lived workers; the window keeps only the compact rows. A worker
  that fails falls back to the same work here; an AniList refusal is not asked twice.
* everyone: the visual-novel prewarm (Quartz) needs a capture in the last 30 days (see
  test_fix_memory_diagnostics); freed heap goes back right after the big steps; the hourly
  line shows RSS before and after its release.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from pudge import low_ram, low_ram_worker, memory_diagnostics
from pudge.config import write_config
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime
from pudge.memory_diagnostics import MemoryReporter, memory_step
from pudge.providers import anilist as al
from pudge.providers.anilist import AniListClient, AniListHTTPError
from test_fix_anilist_list_sync_and_local_changes import _entry, _list_handler
from test_fix_anilist_sync_and_movie_stats import _config


def _mock_client_factory(handler):
    def factory(*_args, **_kwargs):
        client = AniListClient("https://graphql.anilist.co", "tok")
        headers = client.client.headers
        client.client.close()
        client.client = httpx.Client(transport=httpx.MockTransport(handler), headers=headers)
        return client

    return factory


@pytest.fixture(autouse=True)
def workers_on(monkeypatch):
    # These tests are about the workers (the suite runs with PUDGE_WORKERS=0).
    monkeypatch.delenv(low_ram.WORKERS_DISABLE_ENV, raising=False)


@pytest.fixture
def anilist(tmp_path, monkeypatch):
    al.configure_rate_limit_dir(tmp_path / "rl")
    monkeypatch.setattr(al.time, "sleep", lambda _seconds: None)
    yield
    al.configure_rate_limit_dir(None)


LISTS = [
    {
        "status": "CURRENT",
        "entries": [_entry(5, "CURRENT", 3, "Invented Five"), _entry(6, "CURRENT", 1, "Invented Six")],
    },
    {"status": "PLANNING", "entries": [_entry(8, "PLANNING", 0, "Invented Plan")]},
    {"status": "COMPLETED", "entries": [_entry(9, "COMPLETED", 12, "Invented Done")]},
]


def _snapshot(manager: AnimeManager) -> dict[int, tuple]:
    return {
        item.media_id: (item.title, item.status, item.progress, item.episodes)
        for item in manager.db.anime_list()
    }


def _in_process_worker(calls: list[str], handler):
    """low_ram.run_worker stand-in: the worker's task in this process, JSON both ways."""

    def run(task, payload, **_kwargs):
        calls.append(task)
        payload = json.loads(json.dumps(payload))
        original = al.AniListClient
        al.AniListClient = _mock_client_factory(handler)
        try:
            return json.loads(json.dumps(low_ram_worker.TASKS[task](payload)))
        finally:
            al.AniListClient = original

    return run


def _low_ram_manager(tmp_path: Path, *, low: bool) -> AnimeManager:
    cfg = _config(tmp_path)
    cfg.power.low_ram_mode = low
    write_config(cfg, cfg.config_path)
    Database(cfg.library.database_path)
    return AnimeManager(cfg)


def test_low_ram_anilist_list_comes_from_the_worker_with_the_same_result(tmp_path, monkeypatch, anilist):
    handler = _list_handler(LISTS)
    monkeypatch.setattr("pudge.manager.AniListClient", _mock_client_factory(handler))
    inline = _low_ram_manager(tmp_path / "inline", low=False)
    with monkeypatch.context() as no_workers:
        no_workers.setenv(low_ram.WORKERS_DISABLE_ENV, "0")  # the in-process answer
        inline.sync_anilist()
    expected = _snapshot(inline)

    calls: list[str] = []
    monkeypatch.setattr(low_ram, "run_worker", _in_process_worker(calls, handler))

    def window_client(*args, **kwargs):
        client = _mock_client_factory(handler)(*args, **kwargs)

        def no_list(*_args, **_kwargs):
            raise AssertionError("the window must not fetch the list itself")

        client.library = no_list  # the worker's own client is not this one
        return client

    monkeypatch.setattr("pudge.manager.AniListClient", window_client)
    worker = _low_ram_manager(tmp_path / "worker", low=True)
    result = worker.sync_anilist()
    assert calls == ["anilist_library"]
    assert _snapshot(worker) == expected and len(expected) == 3
    assert {item.media_id for item in result} == {5, 6, 8}
    assert isinstance(result[0], LibraryAnime)


def test_a_failed_anilist_worker_falls_back_and_a_refusal_is_not_asked_twice(tmp_path, monkeypatch, anilist):
    handler = _list_handler(LISTS)
    monkeypatch.setattr("pudge.manager.AniListClient", _mock_client_factory(handler))

    def broken(task, payload, **_kwargs):
        raise low_ram.WorkerFailed("exit=1 no python")

    monkeypatch.setattr(low_ram, "run_worker", broken)
    manager = _low_ram_manager(tmp_path / "fallback", low=True)
    manager.sync_anilist()
    assert {item.media_id for item in manager.db.anime_list(("CURRENT", "PLANNING"))} == {5, 6, 8}

    asked: list[str] = []
    monkeypatch.setattr(
        low_ram,
        "run_worker",
        lambda task, payload, **_kwargs: {
            "anilist_error": {
                "message": "AniList refused",
                "status_code": 403,
                "detail": "x",
                "refusal": True,
            }
        },
    )
    real_library = AniListClient.library
    monkeypatch.setattr(AniListClient, "library", lambda self: asked.append("inline") or real_library(self))
    refused = _low_ram_manager(tmp_path / "refused", low=True)
    with pytest.raises(AniListHTTPError) as caught:  # nothing saved yet: the error stands
        refused._anilist_library(_mock_client_factory(handler)())
    assert caught.value.status_code == 403 and caught.value.refusal is True
    assert asked == []


def test_low_ram_intro_detection_runs_in_a_worker_process(tmp_path, monkeypatch) -> None:
    manager = _low_ram_manager(tmp_path, low=True)
    seen: list[dict] = []
    real = low_ram.run_worker

    def spy(task, payload, **kwargs):
        seen.append({"task": task, **payload})
        return real(task, payload, **kwargs)  # a real `python -m pudge.low_ram_worker intro_pass`

    monkeypatch.setattr(low_ram, "run_worker", spy)
    monkeypatch.setattr(
        "pudge.intro_skipper.run_background_pass",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("ran in the window")),
    )
    # The test run disables detection itself (PUDGE_INTRO_DETECTION=0): the worker says so.
    assert manager.run_intro_detection() == 0
    assert [row["task"] for row in seen] == ["intro_pass"]
    assert seen[0]["config_path"] == str(manager.config.config_path)


def test_intro_worker_answer_and_fallback(tmp_path, monkeypatch) -> None:
    manager = _low_ram_manager(tmp_path, low=True)
    inline: list[bool] = []
    monkeypatch.setattr(
        "pudge.intro_skipper.run_background_pass",
        lambda **kwargs: inline.append(kwargs["paused_ok"]) or {"status": "done", "analysed": 2},
    )
    monkeypatch.setattr(low_ram, "run_worker", lambda task, payload, **_kw: {"status": "done", "analysed": 3})
    assert manager.run_intro_detection() == 3 and inline == []
    monkeypatch.setattr(
        low_ram, "run_worker", lambda *a, **k: (_ for _ in ()).throw(low_ram.WorkerFailed("timeout"))
    )
    assert manager.run_intro_detection() == 2 and inline == [False]
    # The agent's step beside a paused player stays in its own process as before.
    monkeypatch.setattr(manager.work_scheduler, "foreground_video", lambda: object())
    manager.run_paused_intro_detection()
    assert inline == [False, True]
    # Normal mode uses the same worker (here: failing → in-process); PUDGE_WORKERS=0 never asks it.
    off = _low_ram_manager(tmp_path / "off", low=False)
    off.run_intro_detection()
    assert inline == [False, True, False]
    monkeypatch.setattr(low_ram, "run_worker", lambda *a, **k: pytest.fail("worker started"))
    monkeypatch.setenv(low_ram.WORKERS_DISABLE_ENV, "0")
    off.run_intro_detection()
    assert inline == [False, True, False, False]


def test_the_worker_task_itself_answers_disabled(tmp_path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    write_config(cfg, cfg.config_path)
    Database(cfg.library.database_path)
    monkeypatch.setenv("PUDGE_INTRO_DETECTION", "0")
    assert low_ram_worker.intro_pass({"config_path": str(cfg.config_path), "incomplete_paths": []}) == {
        "status": "disabled"
    }


# ------------------------------------------------------------------ heap release


def test_big_steps_hand_freed_heap_back(monkeypatch) -> None:
    released: list[bool] = []
    monkeypatch.setattr(
        memory_diagnostics, "release_heap", lambda force=False: released.append(force) or (300.0, 200.0)
    )
    monkeypatch.setattr(memory_diagnostics, "_rss_bytes", lambda: 100 * 2**20)
    with memory_step(None, "home.small"):
        pass
    assert released == []
    with memory_step(None, "anilist.library"):  # known big step: always, even without growth
        pass
    assert released == [True]
    readings = iter([100 * 2**20, 260 * 2**20])
    monkeypatch.setattr(memory_diagnostics, "_rss_bytes", lambda: next(readings))
    lines: list[str] = []
    logger = logging.getLogger("t.mem3.step")
    logger.handlers[:] = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())  # type: ignore[method-assign]
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    with memory_step(logger, "library.scan"):
        pass
    assert released == [True, False]
    assert lines and "delta_mb=160.0 rss_mb=260.0 after_release_mb=200.0" in lines[0]


def test_release_heap_is_throttled_unless_forced(monkeypatch) -> None:
    collected: list[int] = []
    monkeypatch.setattr(memory_diagnostics.gc, "collect", lambda: collected.append(1) or 0)
    monkeypatch.setattr(memory_diagnostics, "_LAST_RELEASE", [0.0])
    assert memory_diagnostics.release_heap() is not None
    assert memory_diagnostics.release_heap() is None  # a second within 10 s
    assert memory_diagnostics.release_heap(force=True) is not None
    assert len(collected) == 2


def test_hourly_line_shows_rss_before_and_after_its_release(monkeypatch) -> None:
    class _Api:
        pass

    values = iter([500.0, 380.0])
    monkeypatch.setattr(memory_diagnostics, "rss_mb", lambda: next(values, 380.0))
    line = MemoryReporter(_Api(), None).log_once("hourly")
    assert line.startswith("rss_mb=380.0 ") and line.endswith(
        " rss_before_release_mb=500.0 rss_after_release_mb=380.0"
    )
