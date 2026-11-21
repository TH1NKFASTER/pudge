"""MEM4: the Mac's Low RAM bundle after an update restart.

* Low RAM mode: the whole AniList refresh (startup phase, Sync, settings, after a change:
  ``refresh_anilist_cache`` with covers, a relation graph, off-list metadata) and the periodic
  one run in the worker; only the small stats come back. A failed worker falls back to the same
  work here, an AniList refusal is not asked twice, and a worker never starts another worker.
* the hourly / debug-bundle memory line says where RSS lives: phys_footprint, anonymous vs
  file-backed, compressed, resident by region kind, Python's arenas, SQLite's allocator.
* every launch phase hands freed heap back.
"""

from __future__ import annotations

import ctypes
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pudge import low_ram, low_ram_worker, memory_diagnostics
from pudge.config import write_config
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.memory_diagnostics import MemoryReporter, memory_step
from pudge.providers import anilist as al
from pudge.providers.anilist import AniListClient, AniListHTTPError
from pudge.web_app import WebAppApi
from test_fix_anilist_list_sync_and_local_changes import _entry, _list_handler
from test_fix_anilist_sync_and_movie_stats import _config

LISTS = [
    {
        "status": "CURRENT",
        "entries": [
            _entry(15, "CURRENT", 2, "Invented Fifteen"),
            _entry(16, "CURRENT", 0, "Invented Sixteen"),
        ],
    },
    {"status": "PLANNING", "entries": [_entry(18, "PLANNING", 0, "Invented Later")]},
    {"status": "COMPLETED", "entries": [_entry(19, "COMPLETED", 12, "Invented Finished")]},
]


def _client_factory(handler, asked: list[str] | None = None):
    def factory(*_args, **_kwargs):
        client = AniListClient("https://graphql.anilist.co", "tok")
        headers = client.client.headers
        client.client.close()

        def counted(request):
            if asked is not None:
                asked.append("request")
            return handler(request)

        client.client = httpx.Client(transport=httpx.MockTransport(counted), headers=headers)
        return client

    return factory


@pytest.fixture
def anilist(tmp_path, monkeypatch):
    al.configure_rate_limit_dir(tmp_path / "rl")
    monkeypatch.setattr(al.time, "sleep", lambda _seconds: None)
    monkeypatch.delenv(low_ram.WORKER_ENV, raising=False)
    monkeypatch.delenv(low_ram.WORKERS_DISABLE_ENV, raising=False)  # the suite runs without workers
    yield
    al.configure_rate_limit_dir(None)


def _manager(tmp_path: Path, *, low: bool, window: bool = False) -> AnimeManager:
    cfg = _config(tmp_path)
    cfg.power.low_ram_mode = low
    write_config(cfg, cfg.config_path)
    Database(cfg.library.database_path)
    return AnimeManager(cfg, owns_database=window)


def _snapshot(manager: AnimeManager) -> dict[int, tuple]:
    return {item.media_id: (item.title, item.status, item.progress) for item in manager.db.anime_list()}


def _in_process_worker(calls: list[dict], handler, monkeypatch, worker_asked: list[str]):
    """low_ram.run_worker stand-in: the worker's task in this process, JSON both ways, with
    AniList reachable only from "inside the worker"."""

    def run(task, payload, **_kwargs):
        payload = json.loads(json.dumps(payload))
        calls.append({"task": task, **payload})
        with monkeypatch.context() as inside:
            inside.setattr("pudge.manager.AniListClient", _client_factory(handler, worker_asked))
            inside.setattr(al, "AniListClient", _client_factory(handler, worker_asked))
            return json.loads(json.dumps(low_ram_worker.TASKS[task](payload)))

    return run


def _window_must_not_ask(*_args, **_kwargs):
    raise AssertionError("the window asked AniList itself")


def test_low_ram_refresh_cache_runs_whole_in_the_worker(tmp_path, monkeypatch, anilist) -> None:
    handler = _list_handler(LISTS)
    monkeypatch.setattr("pudge.manager.AniListClient", _client_factory(handler))
    inline = _manager(tmp_path / "inline", low=False)
    with monkeypatch.context() as no_workers:
        no_workers.setenv(low_ram.WORKERS_DISABLE_ENV, "0")  # the in-process answer
        expected_stats = inline.refresh_anilist_cache()
    expected = _snapshot(inline)
    assert len(expected) == 3  # Completed without a local row is not added

    calls: list[dict] = []
    worker_asked: list[str] = []
    monkeypatch.setattr(low_ram, "run_worker", _in_process_worker(calls, handler, monkeypatch, worker_asked))
    monkeypatch.setattr("pudge.manager.AniListClient", _window_must_not_ask)
    manager = _manager(tmp_path / "worker", low=True)
    with al.interactive_requests():
        stats = manager.refresh_anilist_cache()
    # One worker for everything: the list inside it did not start a second one.
    assert [call["task"] for call in calls] == ["anilist_refresh"]
    assert calls[0]["interactive"] is True and calls[0]["probe"] is False
    assert calls[0]["anilist"] == {
        "enabled": True,
        "endpoint": manager.config.anilist.endpoint,
        "access_token": "token",
    }
    assert worker_asked  # AniList was asked from the worker
    assert stats == expected_stats
    assert _snapshot(manager) == expected


def test_startup_phase_goes_through_the_routed_refresh(tmp_path, monkeypatch, anilist) -> None:
    handler = _list_handler(LISTS)
    calls: list[dict] = []
    monkeypatch.setattr(low_ram, "run_worker", _in_process_worker(calls, handler, monkeypatch, []))
    monkeypatch.setattr("pudge.manager.AniListClient", _window_must_not_ask)
    manager = _manager(tmp_path, low=True)
    api = SimpleNamespace(
        _anilist_sync_lock=threading.Lock(),
        _startup_anilist_sync_done=False,
        manager=manager,
        logger=manager.logger,
        get_state=lambda: {"ok": True},
    )
    answer = WebAppApi.startup_sync_anilist(api)
    assert answer["skipped"] is False and answer["stats"]["anime"] == 3
    assert [call["task"] for call in calls] == ["anilist_refresh"]
    assert calls[0]["interactive"] is True  # the startup phase's request mode reached the worker


def test_failed_refresh_worker_falls_back_and_refusal_is_not_asked_twice(
    tmp_path, monkeypatch, anilist
) -> None:
    handler = _list_handler(LISTS)
    asked: list[str] = []
    monkeypatch.setattr("pudge.manager.AniListClient", _client_factory(handler, asked))
    monkeypatch.setattr(
        low_ram, "run_worker", lambda *_a, **_k: (_ for _ in ()).throw(low_ram.WorkerFailed("exit=1"))
    )
    manager = _manager(tmp_path / "fallback", low=True)
    stats = manager.refresh_anilist_cache()
    assert stats["anime"] == 3 and asked  # done here instead
    assert {item.media_id for item in manager.db.anime_list(("CURRENT", "PLANNING"))} == {15, 16, 18}

    asked.clear()
    monkeypatch.setattr(
        low_ram,
        "run_worker",
        lambda *_a, **_k: {
            "anilist_error": {"message": "AniList refused", "status_code": 403, "detail": "", "refusal": True}
        },
    )
    refused = _manager(tmp_path / "refused", low=True)
    with pytest.raises(AniListHTTPError) as caught:
        refused.refresh_anilist_cache()
    assert caught.value.status_code == 403 and asked == []


def test_worker_task_reports_a_refusal_without_a_saved_list(tmp_path, monkeypatch, anilist) -> None:
    def refuse(_request):
        return httpx.Response(403, json={"errors": [{"message": "invented refusal"}]})

    monkeypatch.setattr("pudge.manager.AniListClient", _client_factory(refuse))
    manager = _manager(tmp_path, low=True)
    answer = low_ram_worker.anilist_refresh(
        {"config_path": str(manager.config.config_path), "anilist": {"access_token": "token"}}
    )
    assert answer["anilist_error"]["status_code"] == 403


def test_low_ram_periodic_refresh_in_the_worker_only_when_due(tmp_path, monkeypatch, anilist) -> None:
    handler = _list_handler(LISTS)
    calls: list[dict] = []
    monkeypatch.setattr(low_ram, "run_worker", _in_process_worker(calls, handler, monkeypatch, []))
    monkeypatch.setattr("pudge.manager.AniListClient", _window_must_not_ask)
    agent = _manager(tmp_path / "agent", low=True)  # the agent: its own process already
    monkeypatch.setattr("pudge.manager.AniListClient", _client_factory(handler))
    assert agent.refresh_anilist_if_due() == 3
    assert "anilist_periodic" not in [call["task"] for call in calls]
    calls.clear()
    monkeypatch.setattr("pudge.manager.AniListClient", _window_must_not_ask)
    manager = _manager(tmp_path / "window", low=True, window=True)
    assert manager.refresh_anilist_if_due() == 3
    assert [call["task"] for call in calls] == ["anilist_periodic"]
    assert manager.refresh_anilist_if_due() == 0  # just refreshed: not due, no worker
    assert len(calls) == 1
    assert {item.media_id for item in manager.db.anime_list(("CURRENT", "PLANNING"))} == {15, 16, 18}


def test_with_workers_switched_off_nothing_goes_to_a_worker(tmp_path, monkeypatch, anilist) -> None:
    # Normal mode uses the workers too now (test_fix_normal_mode_processes); PUDGE_WORKERS=0
    # keeps everything in-process.
    handler = _list_handler(LISTS)
    monkeypatch.setattr("pudge.manager.AniListClient", _client_factory(handler))
    monkeypatch.setattr(low_ram, "run_worker", lambda *_a, **_k: pytest.fail("worker started"))
    monkeypatch.setenv(low_ram.WORKERS_DISABLE_ENV, "0")
    manager = _manager(tmp_path, low=False)
    assert manager.refresh_anilist_cache()["anime"] == 3
    assert manager.refresh_anilist_if_due() == 0


def test_a_worker_never_starts_another_worker(tmp_path, monkeypatch) -> None:
    cfg = SimpleNamespace(power=SimpleNamespace(low_ram_mode=True))
    monkeypatch.delenv(low_ram.WORKER_ENV, raising=False)
    monkeypatch.delenv(low_ram.WORKERS_DISABLE_ENV, raising=False)
    assert low_ram.worker_allowed(cfg)
    with low_ram.inline_only():
        assert not low_ram.worker_allowed(cfg)
    assert low_ram.worker_allowed(cfg)
    monkeypatch.setenv(low_ram.WORKER_ENV, "1")
    assert not low_ram.worker_allowed(cfg)
    monkeypatch.delenv(low_ram.WORKER_ENV)

    seen: dict = {}

    def runner(command, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"result":1}\n', stderr="")

    assert low_ram.run_worker("anilist_refresh", {}, runner=runner) == 1
    assert seen["env"][low_ram.WORKER_ENV] == "1"


def test_real_refresh_worker_process_answers_stats(tmp_path, monkeypatch, anilist) -> None:
    cfg = _config(tmp_path)
    cfg.anilist.enabled = False  # no network: the saved (empty) list
    cfg.power.low_ram_mode = True
    write_config(cfg, cfg.config_path)
    Database(cfg.library.database_path)
    answer = low_ram.run_worker(
        "anilist_refresh",
        {"config_path": str(cfg.config_path), "anilist": {"enabled": False, "access_token": ""}},
        timeout=120,
    )
    assert answer["stats"]["anime"] == 0 and answer["stats"]["cached"] is False


def test_request_modes_cross_processes() -> None:
    assert al.request_modes() == {"interactive": False, "bulk": False, "probe": False}
    with al.interactive_requests(), al.probe_requests():
        modes = al.request_modes()
    assert modes == {"interactive": True, "bulk": False, "probe": True}
    with al.requests_in_modes({"bulk": True, "interactive": True}):
        assert al.request_modes() == {"interactive": True, "bulk": True, "probe": False}
    assert al.request_modes()["bulk"] is False


# ------------------------------------------------------------------ diagnostics

INVENTED_MALLOCSTATS = """Small block threshold = 512, in 32 size classes.

class   size   num pools   blocks in use  avail blocks
-----   ----   ---------   -------------  ------------
    0     16           1             100           924

# arenas allocated total           =                  400
# arenas reclaimed                 =                   10
# arenas highwater mark            =                  390
# arenas allocated current         =                  300
300 arenas * 1048576 bytes/arena   =          314,572,800

# bytes in allocated blocks        =           62,914,560
# bytes in available blocks        =           20,971,520
12800 unused pools * 16384 bytes   =          209,715,200
# bytes lost to pool headers       =              921,600
Total                              =          314,572,800

Total                              =              655,360
"""


def test_debugmallocstats_text_is_read() -> None:
    stats = memory_diagnostics.parse_debugmallocstats(INVENTED_MALLOCSTATS)
    assert stats == {
        "arenas": 300.0,
        "arena_mb": 300.0,
        "used_mb": 60.0,
        "free_mb": 20.0,
        "empty_pools_mb": 200.0,
    }
    assert memory_diagnostics.parse_debugmallocstats("nothing here = 3") == {}


def test_obmalloc_stats_of_this_process_and_stderr_restored(capfd) -> None:
    stats = memory_diagnostics.obmalloc_stats()
    assert stats["arenas"] >= 1 and stats["used_mb"] > 0
    import os

    os.write(2, b"still-stderr\n")
    assert "still-stderr" in capfd.readouterr().err


def test_regions_are_grouped_by_kind() -> None:
    page = 16384
    pages = 2**20 // page  # 1 MB
    grouped = memory_diagnostics.group_regions(
        [
            (0, False, 300 * pages, 290 * pages),
            (2, False, 50 * pages, 40 * pages),
            (7, False, 10 * pages, 10 * pages),
            (0, True, 120 * pages, 0),
            (30, False, 8 * pages, 8 * pages),
            (99, False, pages, pages),
        ],
        page,
    )
    assert grouped == {
        "untagged": (300.0, 290.0),
        "malloc": (60.0, 50.0),
        "file": (120.0, 0.0),
        "stack": (8.0, 8.0),
        "tag99": (1.0, 1.0),
    }


def test_darwin_task_vm_info_fields(monkeypatch) -> None:
    mb = 2**20

    def fake_task_info(task, flavor, info_ref, count_ref):
        assert flavor == memory_diagnostics.TASK_VM_INFO
        info = info_ref._obj
        info.phys_footprint, info.internal, info.external = 410 * mb, 380 * mb, 190 * mb
        info.compressed, info.reusable, info.purgeable_volatile_resident = 30 * mb, 5 * mb, 2 * mb
        return 0

    monkeypatch.setattr(memory_diagnostics, "_TASK_VM", (fake_task_info, 0))
    monkeypatch.setattr(memory_diagnostics.sys, "platform", "darwin")
    assert memory_diagnostics.vm_info() == {
        "footprint": 410.0,
        "internal": 380.0,
        "external": 190.0,
        "compressed": 30.0,
        "reusable": 5.0,
        "purgeable": 2.0,
    }
    monkeypatch.setattr(memory_diagnostics, "_TASK_VM", (lambda *a: 5, 0))  # KERN_FAILURE
    assert memory_diagnostics.vm_info() == {}


def test_darwin_region_walk_descends_into_submaps(monkeypatch) -> None:
    regions = [  # (is_submap, size, tag, external_pager, resident, dirtied)
        (False, 4096, 0, 0, 3, 2),
        (True, 8192, 0, 0, 0, 0),
        (False, 8192, 2, 0, 1, 1),
        (False, 4096, 0, 1, 5, 0),
    ]
    state = {"index": 0}

    def fake_recurse(task, address_ref, size_ref, depth_ref, info_ref, count_ref):
        if state["index"] >= len(regions):
            return 1  # KERN_INVALID_ADDRESS
        submap, size, tag, pager, resident, dirtied = regions[state["index"]]
        state["index"] += 1
        info = info_ref._obj
        info.is_submap, info.user_tag, info.external_pager = int(submap), tag, pager
        info.pages_resident, info.pages_dirtied = resident, dirtied
        size_ref._obj.value = size
        return 0

    monkeypatch.setattr(memory_diagnostics, "_REGION_RECURSE", (fake_recurse, 0))
    assert list(memory_diagnostics._darwin_regions()) == [(0, False, 3, 2), (2, False, 1, 1), (0, True, 5, 0)]


def test_hourly_line_says_where_rss_lives(monkeypatch) -> None:
    class _Api:
        pass

    monkeypatch.setattr(
        memory_diagnostics, "vm_info", lambda: {"footprint": 410.0, "internal": 380.0, "external": 190.0}
    )
    monkeypatch.setattr(
        memory_diagnostics, "vm_regions", lambda: {"untagged": (300.0, 290.0), "file": (120.0, 0.0)}
    )
    monkeypatch.setattr(
        memory_diagnostics,
        "obmalloc_stats",
        lambda: {
            "arenas": 300.0,
            "arena_mb": 300.0,
            "used_mb": 60.0,
            "free_mb": 20.0,
            "empty_pools_mb": 200.0,
        },
    )
    monkeypatch.setattr(memory_diagnostics, "sqlite_memory", lambda: {"used": 1.5, "highwater": 6.0})
    line = MemoryReporter(_Api(), None).log_once("debug_bundle")
    assert "vm=footprint:410.0,internal:380.0,external:190.0" in line
    assert "regions=untagged:300.0/290.0,file:120.0/0.0" in line
    assert "obmalloc=arenas:300/300.0,used:60.0,free:20.0,empty:200.0" in line
    assert "sqlite_mb=1.5/6.0" in line
    assert " rss_before_release_mb=" in line


def test_vm_line_on_this_platform_is_safe() -> None:
    line = memory_diagnostics.vm_line()
    assert isinstance(line, str)
    assert isinstance(memory_diagnostics.sqlite_memory(), dict)


def test_memory_step_names_footprint_and_launch_phases_release(monkeypatch) -> None:
    released: list[bool] = []
    monkeypatch.setattr(
        memory_diagnostics, "release_heap", lambda force=False: released.append(force) or (700.0, 600.0)
    )
    readings = iter([100 * 2**20, 260 * 2**20])
    monkeypatch.setattr(memory_diagnostics, "_rss_bytes", lambda: next(readings, 260 * 2**20))
    monkeypatch.setattr(memory_diagnostics, "footprint_mb", lambda: 321.0)
    lines: list[str] = []
    logger = logging.getLogger("t.mem4.step")
    logger.handlers[:] = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())  # type: ignore[method-assign]
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    with memory_step(logger, "startup.phase.anilist"):
        pass
    assert released == [True]  # a launch phase: forced release
    assert "after_release_mb=600.0 footprint_mb=321.0" in lines[0]
    assert memory_diagnostics.releases_after("startup.phase.local_nyaa_subtitles")
    assert not memory_diagnostics.releases_after("home.small")


def test_task_vm_info_struct_matches_the_kernel_layout() -> None:
    # phys_footprint is the 19th 64-bit word after two 32-bit ones (task_info.h, pack 4).
    assert memory_diagnostics._TaskVmInfo.phys_footprint.offset == 144
    assert ctypes.sizeof(memory_diagnostics._SubmapInfo64) == 19 * 4
    assert memory_diagnostics._SubmapInfo64.pages_reusable.offset == 64
