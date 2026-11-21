"""The library database and config.toml stay safe: a damaged library is offered a local copy at
launch, a broken config.toml is never overwritten, migrations run one at a time, a backup that
cannot remove the API keys is not made, a drifted index is rebuilt (review DB-1, DB-2, DB-4,
DB-5, DB-11)."""

from __future__ import annotations

import fcntl
import os
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import closing
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import pudge.backup as backup_module
import pudge.config as config_module
from pudge import damaged_library, logging_utils, web_app
from pudge.backup import create_backup, make_database_snapshot
from pudge.config import AppConfig, ConfigUnreadableError, load_config, write_config, write_torrents_enabled
from pudge.database import LATEST_SCHEMA_VERSION, Database
from pudge.safe_mode import SafeModeController
from tests.i18n_support import en, ru

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _forget_config_errors():
    yield
    config_module.CONFIG_LOAD_ERROR.clear()


# ------------------------------------------------------------------ DB-1


def _damage(path: Path) -> None:
    """Overwrite part of one page in the middle of the file (as a bad disk sector would)."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page = int(conn.execute("PRAGMA page_size").fetchone()[0])
    data = bytearray(path.read_bytes())
    offset = (len(data) // page) // 2 * page
    data[offset + 8 : offset + 400] = os.urandom(392)
    path.write_bytes(bytes(data))


def _library(path: Path, marker: str) -> Path:
    db = Database(path)
    db.set_state("marker", marker)
    with db.connect() as conn:
        for index in range(3000):
            conn.execute(
                "INSERT INTO state(key,value,updated_at) VALUES(?,?,0)", (f"filler-{index}", "v" * 200)
            )
    return path


def _marker(path: Path) -> str:
    return Database(path).get_state("marker", "")


def test_safe_mode_counts_damage_found_by_quick_check_as_a_failed_check(tmp_path: Path, monkeypatch) -> None:
    # quick_check reports damage as text ("*** in database main *** ..."), which did not start
    # with "error:", so Safe Mode never turned on for a damaged library.
    database = _library(tmp_path / "library.sqlite3", "mine")
    controller = SafeModeController(tmp_path / "cache", database)
    assert controller._database_check() == "ok"
    _damage(database)
    assert controller._database_check().startswith("error:")
    session = tmp_path / "app-session.json"
    session.write_text('{"pid": 99999999}', encoding="utf-8")
    monkeypatch.setattr("pudge.safe_mode.SESSION_PATH", session)
    assert controller.begin() is True and controller.reason == "database_check_failed"


def test_a_damaged_library_is_put_aside_and_the_latest_copy_restored(tmp_path: Path) -> None:
    database = _library(tmp_path / "library.sqlite3", "from the daily copy")
    make_database_snapshot(database, "daily")
    Database(database).set_state("marker", "changed after the copy")
    _damage(database)
    with pytest.raises(sqlite3.DatabaseError):
        Database(database)
    damage = damaged_library.library_damage(database)
    assert damage
    asked: list[tuple[str, str, list[str]]] = []
    assert damaged_library.recover_damaged_library(
        database, damage, choose=lambda title, message, buttons: asked.append((title, message, buttons)) or 0
    )
    [(title, message, buttons)] = asked
    assert title == en("web.damagedLibraryTitle")
    assert str(database) in message and buttons == [
        en("web.damagedLibraryRestoreButton"),
        en("web.damagedLibraryQuit"),
    ]
    assert _marker(database) == "from the daily copy"
    # The damaged file is kept beside the library, never offered as a copy to restore.
    assert len(list(tmp_path.glob("library.sqlite3.damaged-*.backup"))) == 1


def _damage_pages_of(path: Path, name: str) -> None:
    """Overwrite part of a leaf page of one table or index (needs SQLite's dbstat)."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        page = int(conn.execute("PRAGMA page_size").fetchone()[0])
        try:
            leaves = [
                int(row[0])
                for row in conn.execute("SELECT pageno FROM dbstat WHERE name=? AND pagetype='leaf'", (name,))
            ]
        except sqlite3.OperationalError:
            pytest.skip("SQLite without the dbstat table")
    assert len(leaves) > 2, name
    data = bytearray(path.read_bytes())
    offset = (leaves[len(leaves) // 2] - 1) * page
    data[offset + 8 : offset + 400] = os.urandom(392)
    path.write_bytes(bytes(data))


def test_without_a_copy_or_a_repair_the_damaged_file_is_kept_for_good(tmp_path: Path, monkeypatch) -> None:
    # The file put aside was named like a one-off backup copy, which is pruned after 14
    # days: it can be the only copy of the user's library.
    database = _library(tmp_path / "library.sqlite3", "mine")
    _damage_pages_of(database, "state")  # rows lost: a table copy cannot repair it
    monkeypatch.setattr(damaged_library, "_recover_with_sqlite_cli", _no_sqlite_cli)
    damaged_bytes = database.read_bytes()
    damage = damaged_library.library_damage(database)
    assert damage and damaged_library.repaired_copy(database) is None
    # "Quit" changes nothing.
    assert not damaged_library.recover_damaged_library(database, damage, choose=lambda *_a: 1)
    assert database.read_bytes() == damaged_bytes
    asked: list[tuple[str, list[str]]] = []
    assert damaged_library.recover_damaged_library(
        database, damage, choose=lambda _title, message, buttons: asked.append((message, buttons)) or 0
    )
    [aside] = tmp_path.glob("library.sqlite3.damaged-*.keep")
    assert aside.read_bytes() == damaged_bytes
    [(message, buttons)] = asked
    assert str(aside) in message
    assert buttons == [en("web.damagedLibraryStartEmpty"), en("web.damagedLibraryQuit")]
    assert _marker(database) == ""  # a new, empty library opens
    # Weeks later, with newer daily copies of the new library, the damaged file is still kept.
    old = time.time() - 30 * 86400
    os.utime(aside, (old, old))
    make_database_snapshot(database, "daily")
    backup_module.prune_one_off_copies(database)
    assert aside.read_bytes() == damaged_bytes
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]


def _no_sqlite_cli(_source: Path, _target: Path) -> None:
    raise FileNotFoundError("sqlite3")


def test_a_damaged_index_is_repaired_and_offered(tmp_path: Path, monkeypatch) -> None:
    database = _library(tmp_path / "library.sqlite3", "mine")
    _damage_pages_of(database, "sqlite_autoindex_state_1")
    monkeypatch.setattr(damaged_library, "_recover_with_sqlite_cli", _no_sqlite_cli)
    damaged_bytes = database.read_bytes()
    damage = damaged_library.library_damage(database)
    assert damage
    asked: list[tuple[str, list[str]]] = []
    assert damaged_library.recover_damaged_library(
        database, damage, choose=lambda _title, message, buttons: asked.append((message, buttons)) or 0
    )
    [(message, buttons)] = asked
    assert buttons == [
        en("web.damagedLibraryUseRepaired"),
        en("web.damagedLibraryStartEmpty"),
        en("web.damagedLibraryQuit"),
    ]
    [aside] = tmp_path.glob("library.sqlite3.damaged-*.keep")
    assert str(aside) in message and aside.read_bytes() == damaged_bytes
    # Everything is back: the repaired library opens with every row.
    assert damaged_library.library_damage(database) == ""
    assert _marker(database) == "mine"
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM state WHERE key LIKE 'filler-%'").fetchone()[0] == 3000
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    assert not [path for path in tmp_path.iterdir() if path.name.endswith(".tmp")]


def test_sqlite_recover_is_tried_when_the_rows_themselves_are_damaged(tmp_path: Path, monkeypatch) -> None:
    # The sqlite3 shell (every Mac has /usr/bin/sqlite3): its .recover output goes into a new file.
    database = _library(tmp_path / "library.sqlite3", "mine")
    _damage_pages_of(database, "state")
    calls = tmp_path / "calls"
    shell = tmp_path / "bin/sqlite3"
    shell.parent.mkdir()
    shell.write_text(
        f"#!{sys.executable}\n"
        "import sqlite3, sys\n"
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[-1] == '.recover':\n"
        '    print("CREATE TABLE state(key TEXT PRIMARY KEY, value TEXT, updated_at REAL);")\n'
        "    print(\"INSERT INTO state VALUES('marker','recovered',0);\")\n"
        "else:\n"
        "    connection = sqlite3.connect(sys.argv[-1]); connection.executescript(sys.stdin.read()); connection.close()\n",
        encoding="utf-8",
    )
    shell.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shell.parent}{os.pathsep}{os.environ['PATH']}")
    repaired = damaged_library.repaired_copy(database)
    assert repaired is not None
    try:
        # The dump of the damaged file (read-only) is piped into the new file.
        assert sorted(calls.read_text(encoding="utf-8").splitlines()) == sorted(
            [f"-readonly {database} .recover", str(repaired)]
        )
        with closing(sqlite3.connect(repaired)) as conn:
            assert conn.execute("SELECT value FROM state WHERE key='marker'").fetchone()[0] == "recovered"
            assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION
    finally:
        repaired.unlink(missing_ok=True)


def test_a_full_disk_is_not_mistaken_for_a_damaged_library(tmp_path: Path) -> None:
    database = _library(tmp_path / "library.sqlite3", "mine")
    assert damaged_library.library_damage(database) == ""
    assert damaged_library.library_damage(tmp_path / "missing.sqlite3") == ""


class _StopLaunch(Exception):
    pass


def _launch_with(tmp_path: Path, monkeypatch, *, choice: int) -> tuple[list[str], Path]:
    database = _library(tmp_path / "library.sqlite3", "from the daily copy")
    make_database_snapshot(database, "daily")
    _damage(database)
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = database
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache/covers"
    write_config(config, config.config_path)
    attempts: list[str] = []

    class Api:
        def __init__(self, path):
            attempts.append(_marker(database) if not damaged_library.library_damage(database) else "damaged")
            if len(attempts) == 1:
                Database(database)  # what AnimeManager does: raises for the damaged file
            raise _StopLaunch  # the second start reached past the database

    monkeypatch.setattr(web_app, "WebAppApi", Api)
    monkeypatch.setattr(web_app, "_INSTANCE_LOCK", None)
    monkeypatch.setattr(logging_utils, "_ROTATION_OWNER", False)
    monkeypatch.setattr("pudge.single_instance.acquire", lambda *_a, **_k: SimpleNamespace())
    monkeypatch.setitem(sys.modules, "webview", ModuleType("webview"))
    monkeypatch.setattr(damaged_library, "native_choice", lambda *_a: choice)
    return attempts, config.config_path


def test_launch_offers_the_copy_instead_of_crashing(tmp_path: Path, monkeypatch) -> None:
    # A damaged library raised out of WebAppApi before any window or Safe Mode existed.
    attempts, config_path = _launch_with(tmp_path, monkeypatch, choice=0)
    with pytest.raises(_StopLaunch):
        web_app.launch_web_app(config_path)
    assert attempts == ["damaged", "from the daily copy"]


def test_launch_ends_quietly_when_the_user_quits(tmp_path: Path, monkeypatch) -> None:
    attempts, config_path = _launch_with(tmp_path / "quit", monkeypatch, choice=1)
    assert web_app.launch_web_app(config_path) == 1
    assert attempts == ["damaged"]


def test_damaged_library_texts_exist_in_both_languages() -> None:
    for key in (
        "web.damagedLibraryTitle",
        "web.damagedLibraryRestoreButton",
        "web.damagedLibraryStartEmpty",
        "web.damagedLibraryQuit",
    ):
        assert en(key) != key and ru(key) != key and en(key) != ru(key)
    assert "{date}" not in ru("web.damagedLibraryRestore", path="/x", error="e", date="2026-10-01 10:00")


# ------------------------------------------------------------------ DB-2

BROKEN = '[ui]\nlanguage = "ru"\n\n[library]\nroot_dir = "/Volumes/Anime"\ndatabase_path = \n'


def test_a_broken_config_is_never_overwritten_with_the_defaults(tmp_path: Path) -> None:
    # Every page load (request_permissions) wrote the default settings over a config.toml
    # with one broken line: watched folders and the library and database paths were lost.
    config_path = tmp_path / "config.toml"
    config_path.write_text(BROKEN, encoding="utf-8")
    cfg = load_config(config_path, apply_language=False)
    assert config_module.CONFIG_LOAD_ERROR
    with pytest.raises(ConfigUnreadableError) as refused:
        write_config(cfg, config_path)
    assert str(refused.value) == en("error.config.unreadableNotSaved", line=6)
    cfg.nyaa.torrents_enabled = True
    write_torrents_enabled(cfg, config_path)
    assert config_path.read_text(encoding="utf-8") == BROKEN
    # No first-run permission prompt (that write) on default settings either.
    assert cfg.ui.permissions_requested is True
    # Another file is written as usual.
    write_config(cfg, tmp_path / "other.toml")
    assert (tmp_path / "other.toml").is_file()


def test_the_window_leaves_a_broken_config_as_it_is(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("pudge.config.DEFAULT_DATABASE_PATH", tmp_path / "data/library.sqlite3")
    monkeypatch.setattr("pudge.config.DEFAULT_LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr("pudge.config.DEFAULT_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("pudge.safe_mode.SESSION_PATH", tmp_path / "app-session.json")
    monkeypatch.setattr("pudge.web_app.EnergyDiagnosticsMonitor.start", lambda self: None)
    (tmp_path / "data").mkdir()
    config_path = tmp_path / "config.toml"
    config_path.write_text(BROKEN, encoding="utf-8")
    api = web_app.WebAppApi(config_path)
    try:
        assert api.safe_mode.reason == "config_invalid"
        api.request_permissions()
        with pytest.raises(ConfigUnreadableError):
            api.save_settings({"language": "ru"})
        api.config.nyaa.torrents_enabled = False
        assert config_path.read_text(encoding="utf-8") == BROKEN
    finally:
        api.close()


# ------------------------------------------------------------------ DB-4


def _old_schema(path: Path) -> Path:
    _library(path, "before the update")
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION - 1}")
        conn.commit()
    return path


def test_one_process_at_a_time_backs_up_and_migrates(tmp_path: Path) -> None:
    # The GUI, the agent and mpv helpers open the old schema together after an update; each
    # copied to the same temp file, the losers crashed and the backup was often 0 bytes.
    database = _old_schema(tmp_path / "library.sqlite3")
    backup = database.with_name(f"{database.name}.pre-v{LATEST_SCHEMA_VERSION}.backup")
    lock = open(database.with_name(f"{database.name}.migrate.lock"), "a+b")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)  # another process is migrating
    errors: list[BaseException] = []
    opener = threading.Thread(target=lambda: _capture(errors, Database, database))
    opener.start()
    time.sleep(0.5)
    assert opener.is_alive() and not backup.exists()
    # That process finishes: the schema is current when this one gets the lock.
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION}")
        conn.commit()
    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    lock.close()
    opener.join(timeout=30)
    assert errors == [] and not opener.is_alive()
    assert not backup.exists()  # no second backup of an already migrated library
    assert not list(tmp_path.glob(".*.tmp"))


def _capture(errors: list[BaseException], function, *args) -> None:
    try:
        function(*args)
    except BaseException as exc:  # noqa: BLE001 - collected for the assertion
        errors.append(exc)


def test_processes_migrating_together_all_open_the_library(tmp_path: Path) -> None:
    database = _old_schema(tmp_path / "library.sqlite3")
    code = "import sys; from pathlib import Path; from pudge.database import Database; Database(Path(sys.argv[1]))"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])}
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(database)], env=env, stderr=subprocess.PIPE, text=True
        )
        for _ in range(3)
    ]
    results = [(process.wait(timeout=120), process.stderr.read()) for process in processes]
    assert [code for code, _err in results] == [0, 0, 0], results
    backup = database.with_name(f"{database.name}.pre-v{LATEST_SCHEMA_VERSION}.backup")
    with closing(sqlite3.connect(f"{backup.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION - 1
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT value FROM state WHERE key='marker'").fetchone()[0] == "before the update"
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION


# ------------------------------------------------------------------ DB-5

SECRET = "jiten-key-0123456789abcdef-SECRET"


def _library_with_keys(tmp_path: Path) -> Path:
    database = tmp_path / "library.sqlite3"
    Database(database)
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ln_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO ln_settings(key,value) VALUES('jiten_api_key',?)", (SECRET,))
        conn.commit()
    return database


def _make_backup(database: Path, output: Path) -> dict:
    return create_backup(
        config_path=database.parent / "config.toml",
        database_path=database,
        cache_dir=database.parent / "cache",
        output=output,
        version="test",
    )


def test_a_backup_whose_key_removal_fails_is_not_made(tmp_path: Path, monkeypatch) -> None:
    # The removal of the Jiten/JPDB keys failed silently (VACUUM on a full disk) and the
    # archive still held them while its manifest said they were removed.
    database = _library_with_keys(tmp_path)
    real_connect = sqlite3.connect

    class FullDisk:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            if str(sql).strip().upper() == "VACUUM":
                raise sqlite3.OperationalError("database or disk is full")
            return self._conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

    opened: list[str] = []

    def connect(path, *args, **kwargs):
        conn = real_connect(path, *args, **kwargs)
        if "pudge-backup-" not in str(path):
            return conn
        opened.append(str(path))
        return conn if len(opened) == 1 else FullDisk(conn)  # the copy is made, then redacted

    monkeypatch.setattr(backup_module.sqlite3, "connect", connect)
    output = tmp_path / "out/backup.zip"
    with pytest.raises(RuntimeError) as failed:
        _make_backup(database, output)
    assert str(failed.value) == en("error.backup.redactionFailed", error="database or disk is full")
    assert not output.exists() and not list(output.parent.glob("*"))


def test_a_backup_holds_no_api_key(tmp_path: Path) -> None:
    database = _library_with_keys(tmp_path)
    output = tmp_path / "backup.zip"
    _make_backup(database, output)
    with zipfile.ZipFile(output) as archive:
        data = archive.read("library.sqlite3")
        manifest = archive.read("manifest.json").decode()
    assert SECRET.encode() not in data
    assert '"redacted_secret_count": 1' in manifest
    # The live library keeps its key.
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT value FROM ln_settings").fetchone()[0] == SECRET


# ------------------------------------------------------------------ DB-11


def test_a_drifted_index_is_rebuilt(tmp_path: Path) -> None:
    # Libraries created at schema v3-v6 kept idx_audiobook_bookmarks_book(book_id,position):
    # CREATE INDEX IF NOT EXISTS never replaces an index.
    database = tmp_path / "library.sqlite3"
    Database(database)
    with closing(sqlite3.connect(database)) as conn:
        conn.execute("DROP INDEX idx_audiobook_bookmarks_book")
        conn.execute("CREATE INDEX idx_audiobook_bookmarks_book ON audiobook_bookmarks(book_id,position)")
        conn.commit()
    Database(database)
    with closing(sqlite3.connect(database)) as conn:
        columns = [row[2] for row in conn.execute("PRAGMA index_info(idx_audiobook_bookmarks_book)")]
    assert columns == ["book_id", "sort_order", "position"]
    fresh = tmp_path / "fresh.sqlite3"
    Database(fresh)
    with closing(sqlite3.connect(database)) as old, closing(sqlite3.connect(fresh)) as new:
        query = "SELECT name,sql FROM sqlite_master WHERE type='index' ORDER BY name"

        def indexes(conn):
            return [(name, "".join(str(sql or "").split())) for name, sql in conn.execute(query)]

        assert indexes(old) == indexes(new)
