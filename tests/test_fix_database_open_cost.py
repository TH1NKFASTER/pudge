"""DB-6: helpers, the agent and CLI runs open the library without the write lock,
the schema script and a full-file quick_check; the window checks only when due."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import __version__, cli, web_app
from pudge.config import AppConfig
from pudge.database import SCHEMA_DIGEST_KEY, Database
from pudge.episode_numbering import _graph_from_db
from pudge.manager import AnimeManager


@pytest.fixture
def statements(monkeypatch) -> list[str]:
    seen: list[str] = []
    real_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    return seen


def _heavy(seen: list[str]) -> list[str]:
    return [s for s in seen if "quick_check" in s or "BEGIN IMMEDIATE" in s or "CREATE TABLE" in s]


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    return cfg


def test_helper_open_of_a_current_library_skips_schema_and_check(tmp_path: Path, statements) -> None:
    path = tmp_path / "library.sqlite3"
    Database(path)
    statements.clear()
    db = Database(path, initialize=False)
    assert _heavy(statements) == []
    assert db.get_state("missing", "ok") == "ok"


def test_unknown_schema_digest_initializes_once_without_quick_check(tmp_path: Path, statements) -> None:
    # Same user_version, but the file was last initialized by other schema code (a
    # table added without a version bump): the helper initializes it once.
    path = tmp_path / "library.sqlite3"
    Database(path).set_state(SCHEMA_DIGEST_KEY, "13:0.0.1:older")
    statements.clear()
    Database(path, initialize=False)
    assert any("BEGIN IMMEDIATE" in s for s in statements)
    assert not [s for s in statements if "quick_check" in s]
    statements.clear()
    Database(path, initialize=False)
    assert _heavy(statements) == []


def test_owning_open_still_checks_by_default(tmp_path: Path, statements) -> None:
    path = tmp_path / "library.sqlite3"
    Database(path)
    statements.clear()
    Database(path)
    assert [s for s in statements if "quick_check" in s]
    statements.clear()
    Database(path, integrity_check=False)
    assert not [s for s in statements if "quick_check" in s]


def test_agent_and_cli_manager_do_not_initialize_or_check(tmp_path: Path, statements) -> None:
    cfg = _config(tmp_path)
    Database(cfg.library.database_path)
    statements.clear()
    AnimeManager(cfg)
    assert _heavy(statements) == []


def test_cli_helpers_open_without_initializing(tmp_path: Path, statements) -> None:
    cfg = _config(tmp_path)
    Database(cfg.library.database_path)
    statements.clear()
    assert cli._library_anilist_hint(tmp_path / "missing.mkv", cfg) is None
    _graph_from_db(cfg, 1, None)
    assert _heavy(statements) == []


def _gui(db: Database, *, interrupted: bool) -> SimpleNamespace:
    return SimpleNamespace(
        manager=SimpleNamespace(db=db),
        safe_mode=SimpleNamespace(interrupted=interrupted),
        logger=logging.getLogger("test.integrity"),
        INTEGRITY_CHECKED_VERSION_KEY=web_app.WebAppApi.INTEGRITY_CHECKED_VERSION_KEY,
    )


def test_window_checks_after_unclean_exit_or_version_change_only(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "library.sqlite3"
    Database(path)
    checks: list[int] = []
    monkeypatch.setattr(Database, "check_integrity", lambda self: checks.append(1))

    def launch(*, interrupted: bool) -> int:
        checks.clear()
        db = Database(path, integrity_check=False)
        web_app.WebAppApi._startup_integrity_check(_gui(db, interrupted=interrupted))
        return len(checks)

    assert launch(interrupted=False) == 1  # first launch of this version
    assert Database(path).get_state(web_app.WebAppApi.INTEGRITY_CHECKED_VERSION_KEY) == __version__
    assert launch(interrupted=False) == 0  # clean exit, same version
    assert launch(interrupted=True) == 1  # the previous window crashed
    Database(path).set_state(web_app.WebAppApi.INTEGRITY_CHECKED_VERSION_KEY, "0.0.1")
    assert launch(interrupted=False) == 1  # app updated


def test_window_check_raises_for_a_damaged_file(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    Database(path)
    db = Database(path, integrity_check=False)

    def damaged(self) -> None:
        raise sqlite3.DatabaseError("database integrity check failed: Tree 61 page 61")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Database, "check_integrity", damaged)
        with pytest.raises(sqlite3.DatabaseError):
            web_app.WebAppApi._startup_integrity_check(_gui(db, interrupted=True))


def test_safe_mode_reports_an_interrupted_previous_window(tmp_path: Path, monkeypatch) -> None:
    from pudge import safe_mode

    session = tmp_path / "session.json"
    session.write_text('{"pid": 999999, "started_at": 1}', encoding="utf-8")
    monkeypatch.setattr(safe_mode, "SESSION_PATH", session)
    monkeypatch.setattr(safe_mode, "_pid_alive", lambda _pid: False)
    controller = safe_mode.SafeModeController(tmp_path / "cache", tmp_path / "library.sqlite3")
    controller.begin()
    assert controller.interrupted is True
    session.write_text("{}", encoding="utf-8")
    controller = safe_mode.SafeModeController(tmp_path / "cache", tmp_path / "library.sqlite3")
    controller.begin()
    assert controller.interrupted is False


def test_helper_open_of_a_bare_current_version_file_initializes_it(tmp_path: Path) -> None:
    # A file at the current user_version without the state table (made by another tool).
    path = tmp_path / "library.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE other(x)")
        conn.execute(f"PRAGMA user_version={__import__('pudge.database').database.LATEST_SCHEMA_VERSION}")
    db = Database(path, initialize=False)
    assert db.get_state("missing", "ok") == "ok"
