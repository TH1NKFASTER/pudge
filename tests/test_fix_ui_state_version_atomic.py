"""DB-8: ui_state_version bumps from Python are one SQL statement, like the triggers."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.web_app import WebAppApi


def _insert_episode(path: Path, name: str) -> None:
    with Database(path, initialize=False).connect() as conn:  # an episodes trigger bumps the version
        conn.execute("INSERT INTO episodes(title,video_path,updated_at) VALUES('t',?,0)", (name,))


def test_a_trigger_bump_between_read_and_write_is_not_lost(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "library.sqlite3"
    db = Database(path)
    start = int(db.get_state("ui_state_version", "0"))
    real_get = Database.get_state
    raced: list[str] = []

    def racing_get(self, key, default=""):
        value = real_get(self, key, default)
        if key == "ui_state_version" and not raced:
            raced.append(value)
            _insert_episode(path, "/v/race")  # the agent writes right after our read
        return value

    monkeypatch.setattr(Database, "get_state", racing_get)
    api = SimpleNamespace(manager=SimpleNamespace(db=db))
    value = WebAppApi._bump_ui_state_version(api)
    if not raced:  # one statement: the other writer can only come before or after it
        _insert_episode(path, "/v/race")
    assert int(real_get(db, "ui_state_version")) == start + 2
    assert int(value) >= start + 1


def test_concurrent_bumps_and_trigger_writes_all_count(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    Database(path)
    start = int(Database(path, initialize=False).get_state("ui_state_version", "0"))
    rounds = 60

    def bumper() -> None:
        db = Database(path, initialize=False)
        for _ in range(rounds):
            db.bump_ui_state_version()

    def writer() -> None:
        for index in range(rounds):
            _insert_episode(path, f"/w/{index}")

    threads = [
        threading.Thread(target=bumper),
        threading.Thread(target=bumper),
        threading.Thread(target=writer),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert int(Database(path, initialize=False).get_state("ui_state_version")) == start + 3 * rounds


def test_missing_or_damaged_version_row_restarts_at_one(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.delete_state("ui_state_version")
    assert db.bump_ui_state_version() == "1"
    db.set_state("ui_state_version", "garbage")
    assert db.bump_ui_state_version() == "1"


def test_subtitle_requirement_change_bumps_atomically(tmp_path: Path, monkeypatch) -> None:
    from pudge.config import AppConfig

    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg)
    calls: list[int] = []
    real = Database.bump_ui_state_version
    monkeypatch.setattr(Database, "bump_ui_state_version", lambda self: calls.append(1) or real(self))
    manager.set_japanese_subtitles_required(5, False)
    assert calls == [1]
