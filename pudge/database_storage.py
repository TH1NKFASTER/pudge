"""Keep the library database small: compressed Jiten caches, free pages given back.

Reading light novels grew library.sqlite3 by ~24 KB of Jiten parse JSON per
text segment, and the file was never vacuumed, so pages of deleted or replaced
rows stayed in it as free pages. Every daily copy carried both. The two Jiten
caches are now stored zlib-compressed (old TEXT rows stay readable and are
compressed in short background batches), the file switches once to incremental
auto-vacuum, and maintenance gives large freelists back to the disk.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
import zlib
from contextlib import closing
from pathlib import Path
from typing import Any

from .restore_guard import RestoreInProgress, database_use

# A stored JSON value that starts with these bytes is zlib-compressed UTF-8 JSON.
# JSON text never starts with a NUL byte, so old TEXT rows cannot be mistaken.
JSON_BLOB_MAGIC = b"\x00pz1"
JSON_COMPRESS_LEVEL = 6

# (table, column) of the caches written through pack_json.
COMPRESSED_JSON_COLUMNS = (("ln_parse_cache", "parsed_json"), ("jiten_word_cache", "payload"))
COMPACTION_DONE_KEY = "json_cache_compaction_v1"
COMPACTION_BATCH_ROWS = 200
COMPACTION_BUDGET_SECONDS = 20.0
# Pause between batches so waiting writers (mpv helpers, the agent) get the lock.
BATCH_PAUSE_SECONDS = 0.05
WRITE_BUSY_TIMEOUT_MS = 5000

VACUUM_CHECKED_KEY = "database_vacuum_checked_at"
VACUUM_CHECK_INTERVAL_SECONDS = 3600.0
# A VACUUM waits this long for other writers, then gives up until a later run.
VACUUM_BUSY_TIMEOUT_MS = 2000
FULL_VACUUM_FREE_FRACTION = 0.25
INCREMENTAL_VACUUM_FREE_BYTES = 16 * 1024**2
INCREMENTAL_VACUUM_FREE_FRACTION = 0.10
INCREMENTAL_VACUUM_CHUNK_PAGES = 2048
INCREMENTAL_VACUUM_BUDGET_SECONDS = 10.0
AUTO_VACUUM_INCREMENTAL = 2

_MB = 1024**2


def pack_json_text(text: str) -> bytes:
    return JSON_BLOB_MAGIC + zlib.compress(str(text).encode("utf-8"), JSON_COMPRESS_LEVEL)


def pack_json(value: Any) -> bytes:
    """The stored form of a cache value (a BLOB)."""
    return pack_json_text(json.dumps(value, ensure_ascii=False))


def unpack_json(stored: Any) -> Any:
    """Decode a stored cache value: new compressed BLOB or old TEXT JSON. Raises ValueError."""
    if isinstance(stored, (bytes, bytearray, memoryview)):
        data = bytes(stored)
        if data.startswith(JSON_BLOB_MAGIC):
            try:
                data = zlib.decompress(data[len(JSON_BLOB_MAGIC) :])
            except zlib.error as exc:
                raise ValueError(f"damaged compressed cache value: {exc}") from exc
        return json.loads(data)
    return json.loads(str(stored if stored is not None else ""))


def vacuum_into(source: sqlite3.Connection, target: Path) -> None:
    """A compact copy of ``source`` at ``target`` (one read transaction, no free pages)."""
    target = Path(target)
    target.unlink(missing_ok=True)  # VACUUM INTO refuses an existing file
    source.execute("VACUUM INTO ?", (str(target),))
    with closing(sqlite3.connect(target)) as copy:
        copy.execute("PRAGMA journal_mode=DELETE")


def _connect(database_path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    conn = sqlite3.connect(database_path, timeout=busy_timeout_ms / 1000.0, isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    return conn


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        is not None
    )


def _get_state(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else ""


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (key, value, time.time()),
    )


def _is_busy(exc: sqlite3.Error) -> bool:
    code = int(getattr(exc, "sqlite_errorcode", 0) or 0) & 0xFF
    text = str(exc).casefold()
    return code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or "locked" in text or "busy" in text


def compact_json_caches(
    database_path: Path,
    *,
    batch_rows: int = COMPACTION_BATCH_ROWS,
    budget_seconds: float = COMPACTION_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Compress old TEXT rows of the Jiten caches, a short transaction per batch.

    Resumable: the last compressed rowid of each table is kept in ``state``
    and the done marker is set once both tables are through. Rows written by
    current code are already compressed, so they are never revisited.
    """
    result: dict[str, Any] = {"rows": 0, "saved_bytes": 0, "done": False, "busy": False}
    deadline = time.monotonic() + max(0.0, float(budget_seconds))
    with closing(_connect(Path(database_path), WRITE_BUSY_TIMEOUT_MS)) as conn:
        if not _has_table(conn, "state"):
            return result
        if _get_state(conn, COMPACTION_DONE_KEY):
            result["done"] = True
            return result
        for table, column in COMPRESSED_JSON_COLUMNS:
            if not _has_table(conn, table):
                continue
            cursor_key = f"{COMPACTION_DONE_KEY}:{table}"
            try:
                last = int(_get_state(conn, cursor_key) or 0)
            except ValueError:
                last = 0
            while True:
                if time.monotonic() >= deadline:
                    return result
                try:
                    # Read and compress outside the write lock; the UPDATE only
                    # replaces a row that still holds the very text compressed here.
                    rows = conn.execute(
                        f"SELECT rowid,{column} FROM {table} WHERE rowid>? AND typeof({column})='text' "
                        "ORDER BY rowid LIMIT ?",
                        (last, max(1, int(batch_rows))),
                    ).fetchall()
                    if not rows:
                        break
                    packed = [(pack_json_text(text), rowid, text) for rowid, text in rows]
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        for blob, rowid, text in packed:
                            cursor = conn.execute(
                                f"UPDATE {table} SET {column}=? WHERE rowid=? AND {column}=?",
                                (blob, rowid, text),
                            )
                            if cursor.rowcount:
                                result["rows"] += 1
                                result["saved_bytes"] += len(text.encode("utf-8")) - len(blob)
                        _set_state(conn, cursor_key, str(int(rows[-1][0])))
                        conn.execute("COMMIT")
                    except BaseException:
                        conn.execute("ROLLBACK")
                        raise
                except sqlite3.OperationalError as exc:
                    if not _is_busy(exc):
                        raise
                    result["busy"] = True
                    return result
                last = int(rows[-1][0])
                time.sleep(BATCH_PAUSE_SECONDS)
        try:
            _set_state(conn, COMPACTION_DONE_KEY, str(time.time()))
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc):
                raise
            result["busy"] = True
            return result
        result["done"] = True
    return result


def _free_disk_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _page_stats(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "auto_vacuum": int(conn.execute("PRAGMA auto_vacuum").fetchone()[0]),
        "page_size": int(conn.execute("PRAGMA page_size").fetchone()[0]),
        "page_count": int(conn.execute("PRAGMA page_count").fetchone()[0]),
        "free_pages": int(conn.execute("PRAGMA freelist_count").fetchone()[0]),
    }


def _checkpoint(conn: sqlite3.Connection) -> None:
    # In WAL mode the shorter file only reaches the disk at a checkpoint.
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    except sqlite3.Error:
        pass


def vacuum_if_needed(database_path: Path, *, logger: Any) -> dict[str, Any]:
    """One full VACUUM (switching to incremental auto-vacuum), else an incremental one.

    Full: when the file is not yet INCREMENTAL or over 25 % is free pages; skipped
    when the disk has less than twice the file size free, and given up (for a
    later run) when another process holds the write lock. Incremental: when the
    freelist is over 16 MB or 10 %, in small chunks so writers wait only briefly.
    """
    database_path = Path(database_path)
    with closing(_connect(database_path, VACUUM_BUSY_TIMEOUT_MS)) as conn:
        stats = _page_stats(conn)
        size = stats["page_count"] * stats["page_size"]
        free_bytes = stats["free_pages"] * stats["page_size"]
        free_fraction = stats["free_pages"] / stats["page_count"] if stats["page_count"] else 0.0
        if stats["auto_vacuum"] != AUTO_VACUUM_INCREMENTAL or free_fraction > FULL_VACUUM_FREE_FRACTION:
            reason = "auto_vacuum" if stats["auto_vacuum"] != AUTO_VACUUM_INCREMENTAL else "free_pages"
            disk_free = _free_disk_bytes(database_path.parent)
            if disk_free < 2 * size:
                logger.info(
                    "SKIP step=database.vacuum reason=low_disk size_mb=%.1f disk_free_mb=%.1f",
                    size / _MB,
                    disk_free / _MB,
                )
                return {"vacuum": "low_disk"}
            logger.info(
                "START step=database.vacuum reason=%s size_mb=%.1f free_mb=%.1f",
                reason,
                size / _MB,
                free_bytes / _MB,
            )
            started = time.perf_counter()
            try:
                conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
                conn.execute("VACUUM")
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    raise
                logger.info("SKIP step=database.vacuum reason=busy retry=later_run")
                return {"vacuum": "busy"}
            _checkpoint(conn)
            after = _page_stats(conn)
            logger.info(
                "DONE step=database.vacuum duration_ms=%.0f size_mb=%.1f->%.1f auto_vacuum=%s",
                (time.perf_counter() - started) * 1000,
                size / _MB,
                after["page_count"] * after["page_size"] / _MB,
                after["auto_vacuum"],
            )
            return {"vacuum": "full", "freed_bytes": max(0, size - after["page_count"] * after["page_size"])}
        if free_bytes <= INCREMENTAL_VACUUM_FREE_BYTES and free_fraction <= INCREMENTAL_VACUUM_FREE_FRACTION:
            return {"vacuum": ""}
        started = time.perf_counter()
        deadline = time.monotonic() + INCREMENTAL_VACUUM_BUDGET_SECONDS
        remaining = stats["free_pages"]
        try:
            while remaining > 0 and time.monotonic() < deadline:
                # executescript steps the pragma to the end; execute() frees one page.
                conn.executescript(f"PRAGMA incremental_vacuum({INCREMENTAL_VACUUM_CHUNK_PAGES});")
                left = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
                if left >= remaining:
                    break
                remaining = left
                time.sleep(BATCH_PAUSE_SECONDS)
        except sqlite3.OperationalError as exc:
            if not _is_busy(exc):
                raise
            logger.info("SKIP step=database.incremental_vacuum reason=busy retry=later_run")
        _checkpoint(conn)
        freed = (stats["free_pages"] - int(conn.execute("PRAGMA freelist_count").fetchone()[0])) * stats[
            "page_size"
        ]
        logger.info(
            "DONE step=database.incremental_vacuum duration_ms=%.0f freed_mb=%.1f",
            (time.perf_counter() - started) * 1000,
            freed / _MB,
        )
        return {"vacuum": "incremental", "freed_bytes": max(0, freed)}


def maintain_database_storage(
    database_path: Path,
    *,
    logger: Any,
    now: float | None = None,
) -> dict[str, Any]:
    """Maintenance step: compress the Jiten caches, then hourly give free pages back.

    Runs under the maintenance lock (a restore holds it too) and also takes the
    helpers' shared restore lock, so it never touches a file being replaced.
    """
    database_path = Path(database_path).expanduser()
    if not database_path.is_file():
        return {}
    current = time.time() if now is None else float(now)
    try:
        with database_use(database_path, wait_seconds=0.0):
            with closing(_connect(database_path, WRITE_BUSY_TIMEOUT_MS)) as conn:
                if not _has_table(conn, "state"):
                    return {}
                pending = not _get_state(conn, COMPACTION_DONE_KEY)
            result: dict[str, Any] = {"compacted": 0}
            if pending:
                logger.info("START step=database.compact_json")
                started = time.perf_counter()
                compaction = compact_json_caches(database_path)
                result["compacted"] = int(compaction["rows"])
                logger.info(
                    "%s step=database.compact_json rows=%s saved_mb=%.1f finished=%s duration_ms=%.0f%s",
                    "SKIP" if compaction["busy"] else "DONE",
                    compaction["rows"],
                    compaction["saved_bytes"] / _MB,
                    compaction["done"],
                    (time.perf_counter() - started) * 1000,
                    " reason=busy retry=later_run" if compaction["busy"] else "",
                )
                if not compaction["done"]:
                    return result
            with closing(_connect(database_path, WRITE_BUSY_TIMEOUT_MS)) as conn:
                try:
                    last = float(_get_state(conn, VACUUM_CHECKED_KEY) or 0)
                except ValueError:
                    last = 0.0
                if 0 <= current - last < VACUUM_CHECK_INTERVAL_SECONDS:
                    return result
                _set_state(conn, VACUUM_CHECKED_KEY, str(current))
            result.update(vacuum_if_needed(database_path, logger=logger))
            return result
    except RestoreInProgress:
        logger.info("SKIP step=database.storage reason=restore_running")
        return {}
    except sqlite3.OperationalError as exc:
        # Another process held the write lock longer than this step waits.
        if not _is_busy(exc):
            raise
        logger.info("SKIP step=database.storage reason=busy retry=later_run")
        return {}
