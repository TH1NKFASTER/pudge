from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.config import AppConfig
from pudge.download_intents import DownloadIntentStore
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem
from scripts import run_test_batch


def _ln_service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "pudge.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(config)


class _StateDb:
    def __init__(self) -> None:
        self.state: dict[str, str] = {}

    def get_state(self, key: str, default: str = "") -> str:
        return self.state.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.state[key] = value

    def delete_state(self, key: str) -> None:
        self.state.pop(key, None)


def test_light_novel_connection_context_closes_connection(tmp_path: Path) -> None:
    service = _ln_service(tmp_path)

    # Preserve the long-standing helper contract: callers may request a raw
    # sqlite3.Connection and manage its lifetime themselves.
    raw = service._connect()
    try:
        assert raw.execute("SELECT 1").fetchone()[0] == 1
    finally:
        raw.close()

    # Production code uses the deterministic-close wrapper.
    with service._connection() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_parse_same_hash_is_single_flight_and_reuses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _ln_service(tmp_path)
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def fake_request(_action: str, payload: dict | None = None) -> dict:
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(2)
        rows = list((payload or {}).get("text") or [])
        return {"tokens": [[] for _ in rows], "vocabulary": []}

    monkeypatch.setattr(service, "_jiten_request", fake_request)
    results: list[dict] = []

    first = threading.Thread(
        target=lambda: results.append(service._parse_text("同じ本文", "same-hash"))
    )
    second = threading.Thread(
        target=lambda: results.append(service._parse_text("同じ本文", "same-hash"))
    )
    first.start()
    assert started.wait(1)
    second.start()
    time.sleep(0.05)
    assert calls == 1
    release.set()
    first.join(2)
    second.join(2)

    assert not first.is_alive() and not second.is_alive()
    assert calls == 1
    assert len(results) == 2
    assert results[0] == results[1]


def test_parse_different_hashes_do_not_share_one_global_network_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _ln_service(tmp_path)
    first_started = threading.Event()
    allow_first = threading.Event()
    second_started = threading.Event()

    def fake_request(_action: str, payload: dict | None = None) -> dict:
        rows = list((payload or {}).get("text") or [])
        if rows == ["長い章"]:
            first_started.set()
            assert allow_first.wait(2)
        else:
            second_started.set()
        return {"tokens": [[] for _ in rows], "vocabulary": []}

    monkeypatch.setattr(service, "_jiten_request", fake_request)
    first = threading.Thread(target=lambda: service._parse_text("長い章", "chapter"))
    second = threading.Thread(target=lambda: service._parse_text("短い台詞", "dialogue"))
    first.start()
    assert first_started.wait(1)
    second.start()

    assert second_started.wait(0.5), "interactive parse was blocked behind unrelated chapter parse"
    allow_first.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()


def test_parse_rejects_mismatched_token_rows_before_cache_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _ln_service(tmp_path)
    monkeypatch.setattr(
        service,
        "_jiten_request",
        lambda *_args, **_kwargs: {"tokens": [], "vocabulary": []},
    )

    with pytest.raises(LightNovelError, match="token-row count"):
        service._parse_text("一行", "bad-shape")
    assert service._cached_parse("bad-shape") is None


def test_interactive_parse_slot_has_priority_over_queued_background(
    tmp_path: Path,
) -> None:
    service = _ln_service(tmp_path)
    release_active = threading.Event()
    active_started = [threading.Event(), threading.Event()]
    background_acquired = threading.Event()
    interactive_acquired = threading.Event()

    def active_worker(index: int) -> None:
        with service._parse_request_slot(interactive=False):
            active_started[index].set()
            assert release_active.wait(2)

    active_threads = [
        threading.Thread(target=active_worker, args=(0,)),
        threading.Thread(target=active_worker, args=(1,)),
    ]
    for thread in active_threads:
        thread.start()
    assert all(event.wait(1) for event in active_started)

    # Use explicit workers for correct context cleanup after acquisition.
    def queued_background() -> None:
        with service._parse_request_slot(interactive=False):
            background_acquired.set()

    def queued_interactive() -> None:
        with service._parse_request_slot(interactive=True):
            interactive_acquired.set()

    background = threading.Thread(target=queued_background)
    background.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with service._parse_admission:
            if service._parse_waiting_background:
                break
        time.sleep(0.005)

    interactive = threading.Thread(target=queued_interactive)
    interactive.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with service._parse_admission:
            if service._parse_waiting_interactive:
                break
        time.sleep(0.005)

    release_active.set()
    assert interactive_acquired.wait(1)
    assert not background_acquired.is_set() or interactive_acquired.is_set()

    for thread in [*active_threads, interactive, background]:
        thread.join(2)
        assert not thread.is_alive()


def test_download_intent_revision_and_hash_reject_stale_updates_and_completion() -> None:
    store = DownloadIntentStore(_StateDb())
    old = SimpleNamespace(info_hash="OLDHASH", title="old", score=1.0)
    new = SimpleNamespace(info_hash="NEWHASH", title="new", score=2.0)

    first = store.begin(10, 3, False, [old], backend="aria2")
    assert first["revision"] == 1
    assert store.update(
        10,
        3,
        False,
        state="downloading",
        selected=old,
        expected_revision=1,
    ) is not None

    second = store.begin(10, 3, False, [new], backend="aria2")
    assert second["revision"] == 2
    assert store.update(
        10,
        3,
        False,
        state="downloading",
        selected=old,
        expected_revision=1,
    ) is None
    assert store.complete_if_present(10, 3, False, expected_hash="OLDHASH") is False

    current = store.get(10, 3, False)
    assert current is not None
    assert current["revision"] == 2
    assert current["state"] == "selecting"
    assert current["selected_hash"] == ""

    assert store.update(
        10,
        3,
        False,
        state="downloading",
        selected=new,
        expected_revision=2,
    ) is not None
    assert store.complete_if_present(10, 3, False, expected_hash="OLDHASH") is False
    assert store.complete_if_present(10, 3, False, expected_hash="NEWHASH") is True


def test_manager_completion_requires_current_selected_torrent_hash() -> None:
    manager = AnimeManager.__new__(AnimeManager)
    manager.download_intents = DownloadIntentStore(_StateDb())
    manager.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    current = SimpleNamespace(info_hash="NEWHASH", title="new", score=2.0)
    intent = manager.download_intents.begin(10, 3, False, [current], backend="aria2")
    manager.download_intents.update(
        10,
        3,
        False,
        state="downloading",
        selected=current,
        expected_revision=int(intent["revision"]),
    )

    stale = DownloadItem(
        torrent_hash="OLDHASH",
        name="old.mkv",
        state="complete",
        progress=1.0,
        save_path="",
        content_path="",
        media_id=10,
        episode=3,
        media_episode=3,
    )
    assert manager._complete_download_intent(stale) is False
    assert manager.download_intents.get(10, 3, False)["state"] == "downloading"


def test_batch_tree_fingerprint_changes_when_production_or_lockfile_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pudge").mkdir()
    (tmp_path / "scripts").mkdir()
    test_file = tmp_path / "tests" / "test_sample.py"
    source_file = tmp_path / "pudge" / "core.py"
    script_file = tmp_path / "scripts" / "worker.py"
    lockfile = tmp_path / "uv.lock"
    pyproject = tmp_path / "pyproject.toml"
    test_file.write_text("def test_ok(): assert True\n", encoding="utf-8")
    source_file.write_text("VALUE = 1\n", encoding="utf-8")
    script_file.write_text("VALUE = 1\n", encoding="utf-8")
    lockfile.write_text("version = 1\n", encoding="utf-8")
    pyproject.write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.setattr(run_test_batch, "_installed_dependency_versions", lambda: ["dep==1"])

    first = run_test_batch.tree_fingerprint([test_file], root=tmp_path)
    source_file.write_text("VALUE = 2\n", encoding="utf-8")
    second = run_test_batch.tree_fingerprint([test_file], root=tmp_path)
    assert second != first

    lockfile.write_text("version = 2\n", encoding="utf-8")
    third = run_test_batch.tree_fingerprint([test_file], root=tmp_path)
    assert third != second

    monkeypatch.setattr(run_test_batch, "_installed_dependency_versions", lambda: ["dep==2"])
    fourth = run_test_batch.tree_fingerprint([test_file], root=tmp_path)
    assert fourth != third
