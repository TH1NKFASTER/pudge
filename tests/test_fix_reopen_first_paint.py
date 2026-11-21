"""A window reopened from the Dock paints the last known Home at once and builds the real one
once (the report: a reopen after a Low RAM unload ran three full builds, 1.7 s + 4.7 s + 2.9 s,
before the page was stable).

- get_state_snapshot answers from memory, or from the compact file saved when the page was let
  go, without building; the page paints it and get_state() replaces it (stale-while-revalidate);
- a skipped startup AniList sync and a finished launch maintenance no longer rebuild the state
  the page just loaded, and a focus during the first load asks for no second copy;
- an expired storage status (a walk over every library file) is checked in the background
  instead of inside the build;
- a slow build logs where its time went (TIMING step=ui.state_build sections=...).

Invented titles only. Page behaviour runs in the node harness (tests/page_harness.py).
"""

from __future__ import annotations

import gzip
import json
import logging
import stat
import threading
import time
from pathlib import Path

from pudge import web_app as web_app_module
from pudge.bridge_methods import PAGE_METHODS
from pudge.config import AppConfig, write_config
from pudge.manager_models import LibraryAnime
from pudge.web_app import WebAppApi
from pudge.web_state import UIStateSnapshot, UIStateSnapshotCache, read_snapshot_file, write_snapshot_file


def make_api(tmp_path: Path, *, low_ram: bool = True) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    cfg.power.low_ram_mode = low_ram
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    for media_id, title in ((501, "Hoshizora Teapot"), (502, "Kumo no Ressha")):
        api.manager.db.upsert_anime(
            LibraryAnime(media_id, title, status="CURRENT", progress=1, episodes=12, media_status="FINISHED")
        )
    api._bump_ui_state_version()
    return api


def count_builds(monkeypatch) -> list[int]:
    builds: list[int] = []
    real = WebAppApi._get_state_scoped

    def counting(self, *, refresh_storage):
        builds.append(1)
        return real(self, refresh_storage=refresh_storage)

    monkeypatch.setattr(WebAppApi, "_get_state_scoped", counting)
    return builds


def titles(state: dict) -> list[str]:
    return sorted(str(item.get("title")) for item in state.get("current") or [])


# ------------------------------------------------------------------ snapshot endpoint


def test_low_ram_unload_keeps_a_compact_private_snapshot_on_disk_and_frees_memory(
    tmp_path: Path, monkeypatch
) -> None:
    api = make_api(tmp_path, low_ram=True)
    state = api.get_state()
    assert titles(state) == ["Hoshizora Teapot", "Kumo no Ressha"]
    api._log_memory_window_unloaded()  # a minute after the page was let go
    path = api._ui_snapshot_path()
    assert path.is_file() and stat.S_IMODE(path.stat().st_mode) == 0o600
    raw = gzip.decompress(path.read_bytes())
    assert path.stat().st_size < len(raw) / 3  # compact: gzip of compact JSON
    assert api._ui_state_cache.latest() is None  # Low RAM mode: the copy in memory went
    api._bump_ui_state_version()  # the library changed while the window was closed

    builds = count_builds(monkeypatch)
    answer = api.get_state_snapshot()
    assert builds == []  # nothing is built for the first paint
    assert answer["source"] == "disk" and answer["fresh"] is False
    assert titles(answer["state"]) == ["Hoshizora Teapot", "Kumo no Ressha"]
    json.dumps(answer)  # crosses the bridge as it is
    # The real state follows from one build.
    assert titles(api.get_state()) == ["Hoshizora Teapot", "Kumo no Ressha"] and builds == [1]


def test_normal_mode_keeps_memory_and_answers_from_it(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path, low_ram=False)
    api.get_state()
    api._log_memory_window_unloaded()
    assert api._ui_snapshot_path().is_file()
    assert api._ui_state_cache.latest() is not None
    builds = count_builds(monkeypatch)
    fresh = api.get_state_snapshot()
    assert (fresh["source"], fresh["fresh"]) == ("memory", True)
    api._bump_ui_state_version()
    stale = api.get_state_snapshot()  # a stale one still paints
    assert (stale["source"], stale["fresh"]) == ("memory", False)
    assert titles(stale["state"]) == ["Hoshizora Teapot", "Kumo no Ressha"]
    assert builds == []


def test_no_snapshot_in_safe_mode_or_before_any_build(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    assert api.get_state_snapshot() == {"state": None, "source": "", "fresh": False}
    api.get_state()
    api.safe_mode.active = True
    assert api.get_state_snapshot()["state"] is None


def test_a_snapshot_on_disk_is_painted_only_for_the_same_code_and_library(tmp_path: Path) -> None:
    path = tmp_path / "window-state.json.gz"
    stamp = {"app": "1", "code": ["10:20", "30:40"], "db": "/x/library.sqlite3", "db_id": [1, 2]}
    snapshot = UIStateSnapshot("5", {"settings": {}, "current": [{"title": "Yoru no Ame"}]}, None, 1000.0)
    write_snapshot_file(path, snapshot, stamp)
    loaded = read_snapshot_file(path, stamp, now=1000.0 + 60)
    assert loaded is not None and loaded.payload["current"][0]["title"] == "Yoru no Ame"
    assert (loaded.version, loaded.stored_at) == ("5", 1000.0)
    assert read_snapshot_file(path, {**stamp, "code": ["11:20", "30:40"]}, now=1060.0) is None  # an update
    assert read_snapshot_file(path, {**stamp, "db_id": [1, 3]}, now=1060.0) is None  # a restored library
    assert read_snapshot_file(path, stamp, now=1000.0 + 8 * 86400) is None  # too old
    path.write_bytes(b"\x1f\x8b broken")
    assert read_snapshot_file(path, stamp, now=1060.0) is None
    assert read_snapshot_file(tmp_path / "missing.json.gz", stamp) is None


def test_discard_keeps_a_newer_snapshot() -> None:
    cache = UIStateSnapshotCache()
    cache.store("1", {"a": 1})
    old = cache.latest()
    cache.store("2", {"a": 2})
    assert cache.discard(old) is False and cache.latest().version == "2"
    assert cache.discard(cache.latest()) is True and cache.latest() is None


def test_quit_saves_the_snapshot_for_the_next_launch(tmp_path: Path) -> None:
    api = make_api(tmp_path, low_ram=False)
    api.get_state()

    class Unloader:
        def close(self) -> None:
            pass

    api._page_unloader = Unloader()
    api.close()
    second = WebAppApi(api.config_path)
    try:
        answer = second.get_state_snapshot()
        assert answer["source"] == "disk"
        assert titles(answer["state"]) == ["Hoshizora Teapot", "Kumo no Ressha"]
    finally:
        second.close()


def test_the_page_may_call_the_snapshot() -> None:
    assert "get_state_snapshot" in PAGE_METHODS


# ------------------------------------------------------------------ one build per reopen


def test_reopened_window_boot_calls_build_once(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    # The app ran: the launch sync and maintenance are done; the window comes back.
    api._startup_anilist_sync_done = True
    api._startup_maintenance_done = True
    builds = count_builds(monkeypatch)
    state = api.get_state()
    known = str(state["ui_state_version"])
    sync = api.startup_sync_anilist(known)
    maintenance = api.startup_maintenance(known)
    assert builds == [1]  # was 3: the skipped sync and the finished maintenance rebuilt it
    assert sync["skipped"] is True and sync["state"] is None
    assert maintenance["skipped"] is True and maintenance["state"] is None
    assert maintenance["ui_state_version"] == known
    # Something changed meanwhile: the finished maintenance hands over the newer state (one build).
    api._bump_ui_state_version()
    newer = api.startup_maintenance(known)
    assert newer["state"] is not None and newer["state"]["ui_state_version"] != known
    assert builds == [1, 1]
    # A skipped sync's state is ignored by the page: none even when the version moved on.
    assert api.startup_sync_anilist(known)["state"] is None and builds == [1, 1]


def test_python_callers_keep_the_answer_shape_from_the_cache(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    api._startup_anilist_sync_done = True
    api._startup_maintenance_done = True
    api.get_state()
    builds = count_builds(monkeypatch)
    assert titles(api.startup_sync_anilist()["state"]) == ["Hoshizora Teapot", "Kumo no Ressha"]
    assert titles(api.startup_maintenance()["state"]) == ["Hoshizora Teapot", "Kumo no Ressha"]
    assert builds == []


# ------------------------------------------------------------------ storage off the build


def test_a_stale_storage_status_is_checked_in_the_background(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    first = api.get_state()["storage"]
    assert first["downloads_paused"] is False
    walked = threading.Event()
    release = threading.Event()

    def slow_walk():  # the library on a disk that has to spin up
        walked.set()
        assert release.wait(5)
        return {**first, "downloads_paused": True, "paused_reason": "free_space", "free_gb": 0.1}

    monkeypatch.setattr(api.manager, "storage_status", slow_walk)
    api._last_storage_checked_at -= 31  # half a minute later
    version = api.ui_state_versions()["ui"]
    started = time.monotonic()
    state = api.get_state()
    assert time.monotonic() - started < 3  # did not wait for the walk
    assert state["storage"] == first  # the last known status
    assert walked.wait(5)
    api.get_state()  # a build meanwhile starts no second walk
    assert sum(t.name.endswith("-storage-check") for t in threading.enumerate()) == 1
    release.set()
    api._storage_check_thread.join(5)
    # The pause banner changed: the page learns it through a new version.
    assert api.ui_state_versions()["ui"] != version
    assert api.get_state_fast()["storage"]["downloads_paused"] is True


def test_storage_numbers_alone_wait_for_the_next_build(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    first = api.get_state()["storage"]
    monkeypatch.setattr(api.manager, "storage_status", lambda: {**first, "free_gb": 123.4})
    api._last_storage_checked_at -= 31
    version = api.ui_state_versions()["ui"]
    api.get_state()
    api._storage_check_thread.join(5)
    assert api.ui_state_versions()["ui"] == version  # no rebuild for a number
    assert api.get_state()["storage"]["free_gb"] == 123.4


# ------------------------------------------------------------------ timing breakdown


def test_a_slow_build_logs_its_sections(tmp_path: Path, monkeypatch, caplog) -> None:
    api = make_api(tmp_path)
    monkeypatch.setattr(api.logger, "propagate", True)  # the app's logging setup detaches it
    caplog.set_level(logging.INFO)
    api.get_state()
    assert "TIMING step=ui.state_build" not in caplog.text  # a usual build stays quiet
    monkeypatch.setattr(web_app_module, "STATE_BUILD_SLOW_MS", 0.0)
    api.get_state()
    lines = [record.getMessage() for record in caplog.records if "step=ui.state_build" in record.getMessage()]
    assert len(lines) == 1
    line = lines[0]
    sections = dict(part.split(":") for part in line.split("sections=")[1].split(","))
    assert list(sections) == [
        "anime_list",
        "downloads",
        "ready",
        "current_planned",
        "home",
        "waiting_jobs",
        "library",
        "settings",
        "storage",
        "rest",
    ]
    assert all(float(value) >= 0 for value in sections.values())
    assert "since_last_build_s=" in line and "since_last_build_s=first" not in line
    # Outside a build the section marks do nothing.
    web_app_module._build_lap("library")


# ------------------------------------------------------------------ the page


# As in the app (pywebview on macOS): the bridge appears when the page has loaded, together
# with pywebviewready; the page's own scripts ran without it.
PAGE = r"""
window.__bridge = window.pywebview; delete window.pywebview;
const base = __api.get_state;
const titled = (title, version) => { const state = base(); state.ui_state_version = version; state.home.new_ready = [{media_id: 9, title, local: {video_path: '/v/x.mkv'}, ready_episodes: [1]}]; return state; };
window.__shown = [];
const seen = () => {
  const titles = [...document.querySelectorAll('#currentContent .airing-meta strong')].map(el => el.textContent).join(',');
  if (titles && window.__shown[window.__shown.length - 1] !== titles) window.__shown.push(titles);
};
document.addEventListener('DOMContentLoaded', () => new MutationObserver(seen).observe(document.getElementById('currentContent'), {childList: true, subtree: true, characterData: true, attributes: true}));
window.__finishState = null;
__api.get_state = () => new Promise((resolve, reject) => { window.__finishState = () => resolve(titled('Fresh', '7')); window.__failState = () => reject(new Error('Database is retired')); });
__api.ui_state_versions = async () => ({ready: '1', ui: String(ui.state?.ui_state_version || '7')});
__api.startup_sync_anilist = async known => ({skipped: true, stats: {}, state: null, ui_state_version: '7'});
__api.startup_maintenance = async known => ({skipped: true, running: false, done: true, stats: {}, error: '', state: known === '7' ? null : titled('Rebuilt', '8')});
"""

SNAPSHOT = (
    PAGE
    + r"""
__api.get_state_snapshot = async () => ({state: titled('Last seen', '6'), source: 'disk', fresh: false});
"""
)

LATE_SNAPSHOT = (
    PAGE
    + r"""
__api.get_state_snapshot = () => new Promise(resolve => { window.__finishSnapshot = () => resolve({state: titled('Last seen', '6'), source: 'memory', fresh: false}); });
"""
)

READY = r"""
window.pywebview = window.__bridge; window.dispatchEvent(new Event('pywebviewready')); await __flush();
"""

BOOT = (
    READY
    + r"""
const names = () => __apiCalls.map(call => call.name);
const out = {};
await __flush(); await __tick(20);
window.dispatchEvent(new Event('focus')); await __flush();  // the window becomes key meanwhile
out.beforeState = {shown: [...window.__shown], status: __text('#status'), loading: t('status.loading'),
                   started: names().includes('startup_sync_anilist'), resync: names().filter(name => ['ui_state_versions', 'get_state_fast'].includes(name))};
window.__finishState(); await __flush(); await __tick(3000);
out.shown = window.__shown;
out.gets = names().filter(name => name === 'get_state').length;
out.fast = names().filter(name => name === 'get_state_fast').length;
out.snapshots = names().filter(name => name === 'get_state_snapshot').length;
out.sync = __apiCalls.filter(call => call.name === 'startup_sync_anilist').map(call => call.args);
out.maintenance = __apiCalls.filter(call => call.name === 'startup_maintenance').map(call => call.args);
out.version = ui.state.ui_state_version; out.fromSnapshot = ui.stateFromSnapshot;
return out;
"""
)


def test_the_window_paints_the_last_snapshot_then_the_real_state(page) -> None:
    out = page.run(BOOT, before=SNAPSHOT, ready=False).value
    # Painted before the build finished; still "Loading" and nothing started on it.
    assert out["beforeState"]["shown"] == ["Last seen"]
    assert out["beforeState"]["status"] == out["beforeState"]["loading"]
    assert out["beforeState"]["started"] is False
    # A focus while the first load runs asks for no second copy of the state.
    assert out["beforeState"]["resync"] == []
    assert out["shown"] == ["Last seen", "Fresh"]
    assert (out["version"], out["fromSnapshot"]) == ("7", False)
    # One build: the startup calls say which state the page holds and get none back.
    assert (out["gets"], out["fast"], out["snapshots"]) == (1, 0, 1)
    assert out["sync"] == [["7"]] and out["maintenance"] == [["7"]]


def test_a_snapshot_after_the_real_state_is_not_painted(page) -> None:
    code = (
        READY
        + r"""
    window.__finishState(); await __flush(); await __tick(50);
    window.__finishSnapshot(); await __flush(); await __tick(50);
    return {shown: window.__shown, version: ui.state.ui_state_version};
    """
    )
    out = page.run(code, before=LATE_SNAPSHOT, ready=False).value
    assert out == {"shown": ["Fresh"], "version": "7"}


def test_without_a_snapshot_the_boot_is_as_before(page) -> None:
    out = page.run(BOOT, before=PAGE, ready=False).value  # get_state_snapshot answers null
    assert out["beforeState"]["shown"] == []
    assert out["shown"] == ["Fresh"]
    assert (out["gets"], out["fast"]) == (1, 0)
    out = page.run(BOOT, before=PAGE, ready=False, missingApi=["get_state_snapshot"]).value  # older backend
    assert out["shown"] == ["Fresh"] and out["snapshots"] == 0


def test_a_failed_build_keeps_the_painted_snapshot_and_retries(page) -> None:
    code = (
        READY
        + r"""
    await __tick(20);
    window.__failState(); await __flush(); await __tick(20);
    const out = {shown: [...window.__shown], banner: !$('loadStateBanner').hidden};
    await __tick(2000); await __flush();
    window.__finishState(); await __flush(); await __tick(50);
    out.after = window.__shown; out.bannerAfter = !$('loadStateBanner').hidden;
    out.snapshots = __apiCalls.filter(call => call.name === 'get_state_snapshot').length;
    return out;
    """
    )
    out = page.run(code, before=SNAPSHOT, ready=False).value
    assert out["shown"] == ["Last seen"] and out["banner"] is True
    assert out["after"] == ["Last seen", "Fresh"] and out["bannerAfter"] is False
    assert out["snapshots"] == 1  # the retry does not paint the old snapshot over anything


def test_the_snapshot_answer_is_quick_while_a_build_runs(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    api.get_state()
    api._log_memory_window_unloaded()
    real = WebAppApi._get_state_scoped
    building = threading.Event()

    def slow(self, *, refresh_storage):
        building.set()
        time.sleep(0.5)
        return real(self, refresh_storage=refresh_storage)

    monkeypatch.setattr(WebAppApi, "_get_state_scoped", slow)
    worker = threading.Thread(target=api.get_state)
    worker.start()
    assert building.wait(5)
    started = time.monotonic()
    answer = api.get_state_snapshot()  # neither the build guard nor the DB scope is waited for
    elapsed = time.monotonic() - started
    worker.join()
    assert answer["state"] is not None and elapsed < 0.4
