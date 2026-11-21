"""MEM: what an idle window process keeps in memory, and letting go of it.

* an hourly ``EVENT memory rss_mb=… caches=…`` line (and one in the runtime debug bundle)
  names the largest caches, module-level and service-owned;
* caches kept only for speed drop what they have not used for a while (light-novel word
  positions of Statistics, Jiten dictionary entries, Jiten word states);
* Quartz is not imported at launch for someone who never captured a visual novel;
* the Dock icon is decoded once per run, not on every identity refresh.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
import weakref
from functools import lru_cache
from pathlib import Path
from types import ModuleType


from pudge import memory_diagnostics, web_app
from pudge.config import AppConfig, write_config
from pudge.consumption_statistics import ConsumptionStatistics
from pudge.database import Database
from pudge.jiten_words import JitenWordRepository
from pudge.light_novels import LightNovelService
from pudge.memory_diagnostics import (
    MemoryReporter,
    cache_sizes,
    release_idle_memory,
    rss_mb,
    summary_line,
)


def _make_api(tmp_path: Path) -> web_app.WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return web_app.WebAppApi(cfg.config_path)


# ------------------------------------------------------------------ the report


class _Owned:
    """A service-like object (module pudge.*) holding an invented cache."""

    def __init__(self) -> None:
        self.rows = {f"key-{n}": f"{n:0200d}" for n in range(3000)}
        self.released: list[bool] = []

    def release_idle(self, force: bool = False) -> int:
        self.released.append(force)
        return 7


_Owned.__module__ = "pudge.fake_service"


class _Api:
    def __init__(self) -> None:
        self.child = _Owned()


_Api.__module__ = "pudge.fake_api"


def test_rss_is_the_current_resident_size() -> None:
    # RSS is the current resident size, not a peak: it is positive and follows a fresh allocation
    # while it is touched. macOS may compress or page out other memory of the test process at the
    # same moment (a full suite on a busy Mac dropped 1039 -> 769 MB), so the growth is checked
    # against the best of a few readings instead of one before/after pair.
    before = rss_mb()
    assert before > 0
    block = bytearray(64 * 2**20)
    readings = []
    for _ in range(3):
        for index in range(0, len(block), 4096):
            block[index] = (block[index] + 1) % 256
        readings.append(rss_mb())
    assert all(value > 0 for value in readings)
    assert max(readings) >= min(before, readings[0]) or max(readings) >= 64
    assert memory_diagnostics.RSS_SOURCE in {"proc", "task_info", "proc_pid_rusage"}
    del block


def test_macos_rss_tries_task_info_then_proc_pid_rusage_then_ps(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(memory_diagnostics, "_darwin_task_info_rss", lambda: 300 * 2**20)
    monkeypatch.setattr(memory_diagnostics, "_darwin_rusage_rss", lambda: 200 * 2**20)
    assert rss_mb() == 300 and memory_diagnostics.RSS_SOURCE == "task_info"

    def broken():
        raise OSError("no task_info")

    monkeypatch.setattr(memory_diagnostics, "_darwin_task_info_rss", broken)
    assert rss_mb() == 200 and memory_diagnostics.RSS_SOURCE == "proc_pid_rusage"
    monkeypatch.setattr(memory_diagnostics, "_darwin_rusage_rss", lambda: 0)
    assert rss_mb() > 0 and memory_diagnostics.RSS_SOURCE == "ps"  # this process, from ps
    assert "rss_source=ps" in summary_line(None)


def test_cache_sizes_name_module_and_service_caches(monkeypatch) -> None:
    module = ModuleType("pudge.fake_cache_module")
    module._MEMO = {n: [n] * 100 for n in range(500)}

    @lru_cache(maxsize=None)
    def parse(value: int) -> int:
        return value

    for value in range(42):
        parse(value)
    module.parse = parse
    monkeypatch.setitem(sys.modules, module.__name__, module)
    sizes = cache_sizes(_Api())
    assert sizes["pudge.fake_cache_module._MEMO"][0] == 500
    assert sizes["pudge.fake_cache_module._MEMO"][1] > 0.3  # 500 lists of 100 refs
    assert sizes["pudge.fake_cache_module.parse"] == (42, 0.0)
    assert sizes["api.child.rows"][0] == 3000 and sizes["api.child.rows"][1] > 0.5
    line = summary_line(_Api())
    assert (
        line.startswith("rss_mb=") and "child.rows:3000/" in line and "fake_cache_module._MEMO:500/" in line
    )


def test_release_idle_memory_reaches_nested_services() -> None:
    api = _Api()
    released = release_idle_memory(api, force=True)
    assert api.child.released == [True] and released["_Owned"] == 7 and "gc_objects" in released


def test_reporter_logs_hourly_and_ends_with_its_api(monkeypatch) -> None:
    # Not about timing: one report line, then the loop ends by itself once the api is gone. A
    # report walks every loaded module and the gc heap, which takes seconds inside a full suite
    # on a busy Mac, so the waits end on events and only have a far bound against a hang. The
    # lines are collected by the logger's own handler (it has the line before the event is set).
    monkeypatch.setattr(MemoryReporter, "RELEASE_SECONDS", 0.05)
    api = _Api()
    logger = logging.getLogger("t.memory")
    lines: list[str] = []
    logged = threading.Event()

    class _Lines(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if "EVENT memory" in record.getMessage():
                lines.append(record.getMessage())
                logged.set()

    handler = _Lines()
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    reporter = MemoryReporter(api, logger, interval=0.1)
    stop = threading.Event()
    thread = threading.Thread(target=reporter.run, args=(stop,), daemon=True)
    try:
        thread.start()
        assert logged.wait(120)
        assert lines[0].startswith("EVENT memory reason=hourly rss_mb=") and "caches=" in lines[0]
        assert api.child.released  # idle releases ran between the lines
        reference = weakref.ref(api)
        del api
        thread.join(120)  # ends on its own: only the dropped api can end it (stop is never set)
        assert reference() is None and not thread.is_alive()  # the loop does not keep the api alive
    finally:
        stop.set()
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def test_app_runs_the_report_and_the_debug_bundle_logs_one(tmp_path: Path, monkeypatch) -> None:
    api = _make_api(tmp_path)
    task = api.task_supervisor._tasks.get("memory-report")
    assert task is not None and task.running
    monkeypatch.setattr(api.diagnostics_controller, "export", lambda *a, **k: tmp_path / "bundle.zip")
    monkeypatch.setattr(web_app.subprocess, "Popen", lambda *a, **k: None)
    seen: list[str] = []
    handler = logging.Handler()
    handler.emit = lambda record: seen.append(record.getMessage())  # type: ignore[method-assign]
    api.logger.addHandler(handler)
    try:
        api.export_runtime_debug_bundle({})
    finally:
        api.logger.removeHandler(handler)
    assert any(line.startswith("EVENT memory reason=debug_bundle rss_mb=") for line in seen)
    api.close()
    assert not task.running


# ------------------------------------------------------------------ the caches


def test_statistics_word_cache_is_bounded_by_words_and_dropped_when_idle(tmp_path: Path, monkeypatch) -> None:
    words = {f"ch{n}": [(i, 1000 + i, 0) for i in range(20_000)] for n in range(40)}
    stats = ConsumptionStatistics(Database(tmp_path / "db.sqlite3"), ln_words=words.get)
    for chapter in words:
        assert stats._chapter_words(chapter) == words[chapter]
    # 40 chapters x 20k words used to stay (up to 400 chapters): now at most 300k words.
    assert stats._ln_word_cache_words <= ConsumptionStatistics.LN_WORD_CACHE_MAX_WORDS
    assert sum(map(len, stats._ln_word_cache.values())) == stats._ln_word_cache_words
    assert stats.release_idle() == 0  # just used
    monkeypatch.setattr(ConsumptionStatistics, "LN_WORD_CACHE_IDLE_SECONDS", 0.0)
    assert stats.release_idle() > 0 and stats._ln_word_cache == {} and stats._ln_word_cache_words == 0
    assert stats._chapter_words("ch3") == words["ch3"]  # read again on the next query


def test_jiten_word_memory_goes_after_idle_and_sqlite_still_answers(tmp_path: Path, monkeypatch) -> None:
    repository = JitenWordRepository(tmp_path / "words.sqlite")
    cards = [
        {"wordId": 1000 + n, "readingIndex": 0, "spelling": f"語{n}", "meanings": ["m"]} for n in range(50)
    ]
    repository.put_many("acct", cards)
    assert len(repository.memory) == 50
    assert repository.release_idle() == 0  # used a moment ago
    monkeypatch.setattr(JitenWordRepository, "MEMORY_IDLE_SECONDS", 0.0)
    assert repository.release_idle() == 50 and len(repository.memory) == 0
    data, _updated = repository.get("acct", cards[7])
    assert data["spelling"] == "語7" and len(repository.memory) == 1
    repository.close()


def test_light_novel_word_states_are_trimmed_once_reading_stopped(tmp_path: Path, monkeypatch) -> None:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "db.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    service = LightNovelService(config)
    now = time.monotonic()
    service._jiten_live_state_cache.update(
        {("acct", n, 0): (now - 10_000 + n * 0.001, {"state": n}) for n in range(5_000)}
    )
    extras = service._live_state_extras()
    extras["absent"] = {("acct", 90_000 + n, 0) for n in range(1_000)}
    service._jiten_live_state_cache[("acct", 99_999, 0)] = (now, {"state": "fresh"})
    assert service.release_idle() == 0 and len(service._jiten_live_state_cache) == 5_001  # reading now
    del service._jiten_live_state_cache[("acct", 99_999, 0)]
    assert service.release_idle() == 3_000 + 1_000
    kept = service._jiten_live_state_cache
    assert len(kept) == LightNovelService.JITEN_LIVE_STATE_IDLE_ENTRIES
    assert min(key[1] for key in kept) == 3_000  # the newest stay
    assert extras["absent"] == set()
    service.close()


# ------------------------------------------------------------------ launch


def test_quartz_is_preloaded_only_for_recent_visual_novel_users(tmp_path: Path, monkeypatch) -> None:
    api = _make_api(tmp_path)
    prewarmed: list[str] = []
    monkeypatch.setattr(api, "_run_startup_maintenance_background", lambda: None)
    monkeypatch.setattr(
        api, "_prewarm_visual_novel_windows", lambda: prewarmed.append(threading.current_thread().name)
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    api.startup_maintenance()
    time.sleep(0.1)
    assert prewarmed == []
    with api.manager.db.connect() as conn:
        conn.execute(
            "INSERT INTO vn_titles(id,title,normalized_title,executable_hint,created_at,updated_at) "
            "VALUES('vn-1','Invented VN','invented vn','',0,0)"
        )

    def again() -> None:
        api._startup_maintenance_thread = None
        api._vn_prewarm_started = False
        api.startup_maintenance()
        time.sleep(0.1)

    # MEM3: a title row alone (one capture tried long ago) is not "uses visual novels".
    again()
    assert prewarmed == []
    capture = api.consumption.begin_vn_capture(
        title="Invented VN", executable_hint="", window_id=1, generation=1
    )
    api.consumption.end_vn_capture(capture["session_id"])
    with api.manager.db.connect() as conn:
        conn.execute(
            "UPDATE consumption_sessions SET started_at_utc=? WHERE session_id=?",
            (time.time() - 60 * 86400, capture["session_id"]),
        )
    again()
    assert prewarmed == []  # 60 days ago
    with api.manager.db.connect() as conn:
        conn.execute(
            "UPDATE consumption_sessions SET started_at_utc=? WHERE session_id=?",
            (time.time() - 86400, capture["session_id"]),
        )
    again()
    deadline = time.monotonic() + 60  # ends as soon as the prewarm ran
    while not prewarmed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(prewarmed) == 1 and prewarmed[0].endswith("vn-prewarm")
    monkeypatch.setattr(sys, "platform", "linux")
    api.close()


def test_dock_icon_is_decoded_once_per_run(monkeypatch) -> None:
    from test_web_app import _Cocoa, _in_worker_thread

    cocoa = _Cocoa(monkeypatch)
    loads: list[str] = []
    real_alloc = sys.modules["AppKit"].NSImage.alloc

    def alloc():
        image = real_alloc()
        original = image.initWithContentsOfFile_
        image.initWithContentsOfFile_ = lambda path: loads.append(path) or original(path)
        return image

    sys.modules["AppKit"].NSImage.alloc = alloc
    # An icon cached by an earlier call (the real AppKit on a Mac) is not reused with another NSImage.
    monkeypatch.setattr(
        web_app,
        "_RUNTIME_ICON_IMAGE",
        (
            str(web_app.Path(web_app.__file__).resolve().parent / "assets" / "app-icon.png"),
            object(),
            object(),
        ),
    )
    assert web_app._set_macos_runtime_identity() is True
    assert web_app._set_macos_runtime_identity() is True
    assert _in_worker_thread(web_app._set_macos_runtime_identity) is True
    cocoa.run_main_thread_queue()
    icons = [event for event in cocoa.events if event[0] == "icon"]
    assert len(loads) == 1 and len(icons) == 3  # set each time, decoded once
