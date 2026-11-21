"""A damaged library database at launch: offer to restore a local copy (DB-1).

``Database()`` refuses a library that fails ``PRAGMA quick_check``. That used to
end the launch before any window or Safe Mode existed, so the local copies
(daily, before a restore, before a schema update) could not be reached. The
launcher now asks with a native alert and puts the latest working copy back
through the regular restore code, which keeps the damaged file aside.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Sequence

from .i18n import t

_LOG = logging.getLogger("pudge.damaged_library")


def library_damage(database_path: Path) -> str:
    """Why the library file is damaged, or "" when it is fine (or cannot be judged)."""
    path = Path(database_path).expanduser()
    if not path.is_file():
        return ""
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.OperationalError:
        # Locked, busy or unreadable for now: not evidence of damage.
        return ""
    except sqlite3.DatabaseError as exc:  # "malformed", "file is not a database"
        return str(exc) or "damaged"
    result = str(row[0] if row else "no result")
    return "" if result.casefold() == "ok" else result


def damaged_copy_path(database_path: Path) -> Path:
    """Where the damaged file is kept: ``<db>.damaged-<time>.keep``.

    Not a ``*.backup`` name: backup.prune_one_off_copies deletes those after 14
    days, and this file can be the only copy of the user's library.
    """
    path = Path(database_path).expanduser()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.damaged-{stamp}.keep")
    counter = 1
    while target.exists():
        counter += 1
        target = path.with_name(f"{path.name}.damaged-{stamp}-{counter}.keep")
    return target


def move_damaged_aside(database_path: Path, target: Path | None = None) -> Path:
    """Rename the damaged file (and its -wal/-shm) to ``target`` (damaged_copy_path)."""
    path = Path(database_path).expanduser()
    target = target or damaged_copy_path(path)
    path.replace(target)
    for suffix in ("-wal", "-shm"):
        side = Path(str(path) + suffix)
        if side.is_file():
            side.replace(Path(str(target) + suffix))
    return target


def _quick_check_ok(path: Path) -> bool:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and str(row[0]).casefold() == "ok"


def _user_version(path: Path) -> int:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except (sqlite3.Error, TypeError, ValueError):
        return 0


def _salvage_tables(source: Path, target: Path) -> None:
    """Copy every table row by row into a new file, then rebuild indexes and triggers.

    Damage in an index (often the case) is left behind: rows are read from the
    tables only (NOT INDEXED; "WHERE true" turns off SQLite's page-copying
    transfer, which would copy the damaged index too).
    """
    with closing(sqlite3.connect(target, isolation_level=None)) as new:
        new.execute("ATTACH DATABASE ? AS damaged", (source.resolve().as_uri() + "?mode=ro",))
        objects = new.execute(
            "SELECT type,name,sql FROM damaged.sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY rowid"
        ).fetchall()
        new.execute("BEGIN")
        tables = [name for kind, name, _sql in objects if kind == "table"]
        for kind, _name, sql in objects:
            if kind == "table":
                new.execute(sql)
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            new.execute(f"INSERT INTO main.{quoted} SELECT * FROM damaged.{quoted} NOT INDEXED WHERE true")
        if new.execute("SELECT 1 FROM damaged.sqlite_master WHERE name='sqlite_sequence'").fetchone():
            new.execute("DELETE FROM main.sqlite_sequence")
            new.execute("INSERT INTO main.sqlite_sequence SELECT * FROM damaged.sqlite_sequence")
        for kind, _name, sql in objects:
            if kind in {"index", "view", "trigger"}:
                new.execute(sql)
        new.execute("COMMIT")
        new.execute("DETACH DATABASE damaged")


def _recover_with_sqlite_cli(source: Path, target: Path) -> None:
    """sqlite3's .recover (the command-line shell every Mac has) into a new file."""
    cli = shutil.which("sqlite3") or ("/usr/bin/sqlite3" if os.access("/usr/bin/sqlite3", os.X_OK) else "")
    if not cli:
        raise FileNotFoundError("sqlite3")
    dump = subprocess.Popen(
        [cli, "-readonly", str(source), ".recover"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    try:
        load = subprocess.run(
            [cli, str(target)],
            stdin=dump.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=900,
        )
    finally:
        if dump.stdout is not None:
            dump.stdout.close()
        dump.wait(timeout=900)
    if dump.returncode or load.returncode:
        raise RuntimeError(f"sqlite3 .recover failed ({dump.returncode}/{load.returncode})")


def repaired_copy(database_path: Path) -> Path | None:
    """A repaired copy of the damaged library beside it that passes quick_check, or None.

    The damaged file is never written. A table-by-table copy is tried first (cheap,
    enough for damaged indexes), then sqlite3's .recover.
    """
    path = Path(database_path).expanduser()
    version = _user_version(path)
    for attempt, repair in enumerate((_salvage_tables, _recover_with_sqlite_cli), start=1):
        candidate = path.with_name(f".{path.name}.repair-{os.getpid()}-{attempt}.tmp")
        candidate.unlink(missing_ok=True)
        try:
            repair(path, candidate)
            with closing(sqlite3.connect(candidate)) as conn:
                conn.execute(f"PRAGMA user_version={int(version)}")
                conn.commit()
            if _quick_check_ok(candidate):
                _LOG.warning("EVENT app.library_repaired method=%s", repair.__name__)
                return candidate
        except (sqlite3.Error, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            _LOG.warning("FAIL step=app.library_repair method=%s error=%r", repair.__name__, str(exc))
        candidate.unlink(missing_ok=True)
    return None


def native_choice(title: str, message: str, buttons: Sequence[str]) -> int:
    """A modal alert before the window exists; the index of the chosen button (-1: none)."""
    if sys.platform != "darwin":
        print(f"{title}\n{message}", file=sys.stderr)
        return -1
    try:
        from AppKit import NSAlert, NSAlertFirstButtonReturn, NSApplication

        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        for label in buttons:
            alert.addButtonWithTitle_(label)
        return int(alert.runModal()) - int(NSAlertFirstButtonReturn)
    except Exception as exc:  # noqa: BLE001 - no alert: the launch fails as before
        _LOG.error("FAIL step=app.damaged_library_alert error=%r", str(exc))
        return -1


def recover_damaged_library(
    database_path: Path,
    damage: str,
    *,
    choose: Callable[[str, str, Sequence[str]], int] | None = None,
) -> bool:
    """Ask the user and repair the library (``damage``: what library_damage found).

    True when the launch can be tried again; False when the user chose to quit
    or no copy could be restored.
    """
    from .backup import list_database_snapshots, restore_database_snapshot

    choose = choose or native_choice
    path = Path(database_path).expanduser()
    _LOG.error("EVENT app.library_damaged path=%s check=%r", path, damage[:300])
    title = t("web.damagedLibraryTitle")
    detail = (damage.strip().splitlines() or ["damaged"])[0][:200]
    snapshots = list_database_snapshots(path)
    if not snapshots:
        return _without_a_local_copy(path, title, detail, choose)
    created = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(snapshots[0]["created_at"])))
    message = t("web.damagedLibraryRestore", path=str(path), error=detail, date=created)
    if choose(title, message, [t("web.damagedLibraryRestoreButton"), t("web.damagedLibraryQuit")]) != 0:
        return False
    failure: Any = None
    for snapshot in snapshots:  # newest first; a copy that fails its own check is skipped
        try:
            result = restore_database_snapshot(snapshot_path=Path(snapshot["path"]), database_path=path)
        except (sqlite3.Error, ValueError, OSError, RuntimeError) as exc:
            failure = exc
            _LOG.warning("FAIL step=app.library_restore copy=%s error=%r", snapshot["path"], str(exc))
            continue
        _LOG.warning(
            "EVENT app.library_restored copy=%s undo=%s", snapshot["path"], result.get("undo_snapshot")
        )
        return True
    choose(title, t("web.damagedLibraryRestoreFailed", error=str(failure)), [t("web.damagedLibraryQuit")])
    return False


def _without_a_local_copy(
    path: Path, title: str, detail: str, choose: Callable[[str, str, Sequence[str]], int]
) -> bool:
    """No copy to restore: offer a repaired library if one can be made, or an empty one.

    The damaged file is kept in either case (damaged_copy_path), and the alert says where.
    """
    aside = damaged_copy_path(path)
    repaired = repaired_copy(path)
    try:
        if repaired is not None:
            message = t("web.damagedLibraryRepaired", path=str(path), error=detail, copy=str(aside))
            buttons = [
                t("web.damagedLibraryUseRepaired"),
                t("web.damagedLibraryStartEmpty"),
                t("web.damagedLibraryQuit"),
            ]
            choice = choose(title, message, buttons)
            if choice not in (0, 1):
                return False
            move_damaged_aside(path, aside)
            if choice == 0:
                os.replace(repaired, path)
                repaired = None
            _LOG.warning("EVENT app.library_damaged_moved_aside copy=%s repaired=%s", aside, choice == 0)
            return True
        message = t("web.damagedLibraryNoCopy", path=str(path), error=detail, copy=str(aside))
        if choose(title, message, [t("web.damagedLibraryStartEmpty"), t("web.damagedLibraryQuit")]) != 0:
            return False
        move_damaged_aside(path, aside)
        _LOG.warning("EVENT app.library_damaged_moved_aside copy=%s repaired=False", aside)
        return True
    finally:
        if repaired is not None:
            repaired.unlink(missing_ok=True)
