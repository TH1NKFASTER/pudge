"""User: "Loading statistics and heavy screens in general: better precompute it when possible;
now it feels like everything is computed only when the screen opens. Ideally every screen
opens instantly. But don't go too far into the user's RAM."

The Mac keeps the phone's Statistics built (memory + a small disk cache keyed by a stamp of
their inputs), answers from that copy at once ("stale" when the inputs changed, rebuilt in the
background) and rebuilds idle copies once activity settles. The phone keeps the last answer of
each heavy screen on disk (bounded) and shows it at once. Invented data only.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.database import Database
from pudge.iphone_screen_cache import CACHE_VERSION, ScreenCache
from source_policy import source


class Source:
    def __init__(self) -> None:
        self.stamp = "a"
        self.builds = 0

    def build(self) -> dict:
        self.builds += 1
        return {"value": self.builds, "stamp": self.stamp}


def _cache(folder: Path, source: Source, **kwargs) -> ScreenCache:
    cache = ScreenCache(folder, background=False, **kwargs)
    cache.register("stats-30d", lambda: source.stamp, source.build)
    return cache


def test_a_cached_copy_is_served_at_once_and_a_changed_one_as_stale_then_rebuilt(tmp_path):
    source = Source()
    cache = _cache(tmp_path / "screens", source)
    first = cache.serve("stats-30d")
    assert first["value"] == 1 and first["stale"] is False and source.builds == 1
    assert cache.serve("stats-30d")["value"] == 1 and source.builds == 1  # no rebuild
    source.stamp = "b"  # an episode watched, a page read ...
    stale = cache.serve("stats-30d")
    # The request never waits for the rebuild: the old copy, marked stale.
    assert stale["value"] == 1 and stale["stale"] is True and source.builds == 1
    assert cache.run_urgent() == ["stats-30d"] and source.builds == 2
    fresh = cache.serve("stats-30d")
    assert fresh["value"] == 2 and fresh["stale"] is False and fresh["stamp"] == "b"
    assert cache.run_urgent() == []


def test_an_old_copy_is_stale_even_with_the_same_stamp(tmp_path):
    now = [1000.0]
    source = Source()
    cache = _cache(tmp_path / "screens", source, max_age=600, clock=lambda: now[0])
    cache.serve("stats-30d")
    now[0] += 601
    assert cache.serve("stats-30d")["stale"] is True
    cache.run_urgent()
    assert source.builds == 2 and cache.serve("stats-30d")["stale"] is False


def test_copies_survive_a_restart_and_other_versions_are_dropped(tmp_path):
    folder = tmp_path / "screens"
    source = Source()
    _cache(folder, source).serve("stats-30d")
    old = folder / f"stats-7d.v{CACHE_VERSION - 1}.json"
    old.write_text(json.dumps({"stamp": "x", "builtAt": 1, "payload": {}}))
    (folder / "stats-year.v1.json.tmp123").write_text("half written")
    restarted = _cache(folder, source)
    served = restarted.serve("stats-30d")
    assert served["value"] == 1 and served["stale"] is False and source.builds == 1
    assert not old.exists() and sorted(p.name for p in folder.iterdir()) == [
        f"stats-30d.v{CACHE_VERSION}.json"
    ]


def test_a_broken_cache_file_is_rebuilt(tmp_path):
    folder = tmp_path / "screens"
    folder.mkdir()
    (folder / f"stats-30d.v{CACHE_VERSION}.json").write_text("{not json")
    source = Source()
    assert _cache(folder, source).serve("stats-30d")["value"] == 1 and source.builds == 1


def test_the_disk_cache_stays_small(tmp_path):
    folder = tmp_path / "screens"
    cache = ScreenCache(folder, background=False, max_bytes=2500, max_entry_bytes=2000)
    big = {"rows": ["x" * 900]}
    for index, key in enumerate(["stats-7d", "stats-30d", "stats-year"]):
        cache.register(key, lambda: "s", lambda: dict(big))
        cache.serve(key)
        # Distinct modification times: the oldest copy goes first.
        path = folder / f"{key}.v{CACHE_VERSION}.json"
        if path.exists():
            stamp = time.time() - 100 + index
            import os

            os.utime(path, (stamp, stamp))
    total = sum(p.stat().st_size for p in folder.iterdir())
    assert total <= 2500
    assert not (folder / f"stats-7d.v{CACHE_VERSION}.json").exists()
    assert (folder / f"stats-year.v{CACHE_VERSION}.json").exists()
    # Too big for the disk cache: kept in memory only.
    cache.register("stats-all", lambda: "s", lambda: {"rows": ["y" * 5000]})
    assert cache.serve("stats-all")["stale"] is False
    assert not (folder / f"stats-all.v{CACHE_VERSION}.json").exists()


def test_idle_rebuilds_wait_for_activity_to_settle_and_for_an_idle_mac(tmp_path):
    idle = [True]
    source = Source()
    cache = _cache(tmp_path / "screens", source, idle_ok=lambda: idle[0])
    cache.register("stats-7d", lambda: "never-asked", lambda: {"x": 1})
    cache.serve("stats-30d")
    assert cache.wanted() == ["stats-30d"]  # only screens a phone asked for
    assert cache.idle_pass() == []  # nothing changed
    source.stamp = "b"
    assert cache.idle_pass() == []  # changed just now: maybe still watching
    source.stamp = "c"
    assert cache.idle_pass() == []  # still changing
    idle[0] = False
    assert cache.idle_pass() == []  # energy saving / player in front
    idle[0] = True
    assert cache.idle_pass() == ["stats-30d"] and source.builds == 2
    assert cache.serve("stats-30d")["stale"] is False


def test_a_failed_rebuild_keeps_the_copy(tmp_path):
    source = Source()
    cache = _cache(tmp_path / "screens", source)
    cache.serve("stats-30d")
    source.stamp = "b"
    source.build = lambda: (_ for _ in ()).throw(RuntimeError("Mac busy"))  # type: ignore[method-assign]
    cache.register("stats-30d", lambda: source.stamp, source.build)
    assert cache.serve("stats-30d")["stale"] is True
    assert cache.run_urgent() == []
    assert cache.serve("stats-30d")["value"] == 1


def _service(tmp_path):
    from pudge.consumption_statistics import ConsumptionStatistics
    from pudge.iphone_service import IPhoneService

    db = Database(tmp_path / "db.sqlite")
    service = IPhoneService(db, tmp_path / "mobile")
    service.media.screens.background = False
    statistics = ConsumptionStatistics(db)
    calls = []

    def query(scope, limit, offset):
        calls.append(scope["period"])
        return statistics.query(scope, journal_limit=limit, journal_offset=offset)

    service.api = SimpleNamespace(consumption_statistics_query=query)
    return service, statistics, calls


def test_the_phone_statistics_endpoint_serves_the_kept_copy(tmp_path):
    service, statistics, calls = _service(tmp_path)

    def get(period):
        return service.media.handle("GET", "/api/v2/media/stats", {"period": [period]}, dict)

    first = get("7d")
    assert first["stale"] is False and first["totalSeconds"] == 0 and calls == ["7d"]
    assert get("7d")["stale"] is False and calls == ["7d"]
    assert get("bogus")["period"] == "30d" and calls == ["7d", "30d"]
    statistics.add_manual(kind="anime", duration_seconds=600, started_at_utc=time.time() - 3600, title="Show")
    stale = get("7d")
    assert stale["stale"] is True and stale["totalSeconds"] == 0 and calls == ["7d", "30d"]
    service.media.screens.run_urgent()
    fresh = get("7d")
    assert fresh["stale"] is False and fresh["totalSeconds"] == pytest.approx(600)
    # The JSON the phone decodes (StatsSummary: stale/builtAt are optional fields).
    json.dumps(fresh)


def test_a_restarted_mac_answers_from_disk_before_the_app_is_ready(tmp_path):
    from pudge.iphone_media import MediaTemporaryError
    from pudge.iphone_service import IPhoneService

    service, _statistics, _calls = _service(tmp_path)
    service.media.handle("GET", "/api/v2/media/stats", {"period": ["year"]}, dict)
    restarted = IPhoneService(service.db, tmp_path / "mobile")
    restarted.media.screens.stop()
    assert restarted.media.screens.wanted() == ["stats-year"]
    # service.api not attached yet: the kept copy instead of "The Mac app is still starting".
    served = restarted.media.handle("GET", "/api/v2/media/stats", {"period": ["year"]}, dict)
    assert served["period"] == "year"
    with pytest.raises(MediaTemporaryError):
        restarted.media.handle("GET", "/api/v2/media/stats", {"period": ["all"]}, dict)


def test_the_statistics_stamp_sees_new_and_changed_rows(tmp_path):
    from pudge.consumption import ConsumptionLedger

    service, statistics, _calls = _service(tmp_path)
    stamps = [service.media._stats_stamp()]
    assert service.media._stats_stamp() == stamps[0]
    ledger = ConsumptionLedger(service.db)
    media = ledger.ensure_media(kind="anime_episode", title="Show · 1", aliases=[("video_path", "/x/1.mkv")])
    stamps.append(service.media._stats_stamp())
    session = ledger.start_session(kind="anime", media_uuid=media.media_uuid, started_at_utc=time.time() - 60)
    stamps.append(service.media._stats_stamp())
    ledger.append_interval(
        session_id=session,
        kind="anime",
        interval_start_utc=time.time() - 60,
        interval_end_utc=time.time() - 40,
        elapsed_monotonic_ms=20000,
        media=[{"media_uuid": media.media_uuid, "source_revision": "r", "role": "primary"}],
        payload={},
    )
    stamps.append(service.media._stats_stamp())
    ledger.end_session(session, ended_at_utc=time.time() - 40)
    stamps.append(service.media._stats_stamp())
    statistics.add_manual(kind="manga", duration_seconds=60, started_at_utc=time.time() - 30)
    stamps.append(service.media._stats_stamp())
    assert len(set(stamps)) == len(stamps)


SWIFT_CORE = "ios/PudgeCore/Sources/PudgeCore/ScreenCache.swift"
SWIFT_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/ScreenCacheTests.swift"
MODEL = "ios/Pudge/Features/Media/MediaModel.swift"
STATS = "ios/Pudge/UI/StatsScreen.swift"
CONTRACTS = "ios/PudgeCore/Sources/PudgeCore/MediaContracts.swift"


def test_phone_keeps_heavy_screens_on_disk_and_shows_them_at_once():
    core = source(SWIFT_CORE)
    assert "public struct ScreenCache" in core and "maxTotalBytes" in core and "version" in core
    assert "func testScreenCache" in source(SWIFT_TESTS)
    model = source(MODEL)
    refresh = model.split("func refreshStats(period: String) async {", 1)[1].split("\n    func ", 1)[0]
    # The saved copy of the period first, then the Mac quietly; asked again while stale.
    assert "screenCache.load" in refresh and "screenCache.save" in refresh and "stale" in refresh
    assert "func prefetchHeavyScreens()" in model and "priority: .background" in model
    contracts = source(CONTRACTS)
    summary = contracts.split("public struct StatsSummary: Codable {", 1)[1].split("}", 1)[0]
    assert "public let stale: Bool?" in summary and "public let builtAt: Double?" in summary


def test_statistics_spinner_is_centered_not_a_list_row():
    # User: "While statistics load in the mobile app the loading indicator must be centered, not top-left."
    screen = source(STATS)
    body = screen.split("var body: some View {", 1)[1].split("struct HighlightRow", 1)[0]
    assert "} else if media.loadingStats {\n                    ProgressView()" not in body
    assert ".overlay {" in body and "ProgressView()" in body.split(".overlay {", 1)[1].split("}", 1)[0]
