from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import tomllib
import time
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from .branding import APP_NAME, BACKUP_APP_ID, APP_SLUG
from .i18n import err
from .restore_guard import DatabaseInUse, exclusive_database


BACKUP_FORMAT = 2

_CONFIG_SECRET_KEYS = {
    ("jimaku", "api_key"),
    ("anilist", "access_token"),
    ("llm", "api_key"),
    ("llm.profiles.ollama", "api_key"),
    ("llm.profiles.openai", "api_key"),
    ("qbittorrent", "password"),
    ("qbittorrent", "api_key"),
    ("nyaa", "proxy_url"),
}
_DATABASE_SECRET_KEYS = {"jiten_api_key", "jpdb_api_token"}


def _config_secret_values(text: str) -> dict[tuple[str, str], str]:
    section = ""
    values: dict[tuple[str, str], str] = {}
    for line in text.splitlines():
        section_match = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line)
        if section_match:
            section = section_match.group(1).strip().casefold()
            continue
        assignment = re.match(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$", line)
        if assignment:
            key = assignment.group(2).casefold()
            if (section, key) in _CONFIG_SECRET_KEYS:
                values[(section, key)] = assignment.group(4)
    return values


def _redact_config(text: str) -> tuple[str, int]:
    section = ""
    lines: list[str] = []
    redacted = 0
    for line in text.splitlines():
        section_match = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line)
        if section_match:
            section = section_match.group(1).strip().casefold()
            lines.append(line)
            continue
        assignment = re.match(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$", line)
        if assignment and (section, assignment.group(2).casefold()) in _CONFIG_SECRET_KEYS:
            lines.append(f'{assignment.group(1)}{assignment.group(2)}{assignment.group(3)}""')
            redacted += 1
        else:
            lines.append(line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), redacted


def _merge_config_secrets(restored: str, current: str) -> str:
    current_values = _config_secret_values(current)
    section = ""
    lines: list[str] = []
    for line in restored.splitlines():
        section_match = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line)
        if section_match:
            section = section_match.group(1).strip().casefold()
            lines.append(line)
            continue
        assignment = re.match(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*)(.*)$", line)
        identity = (section, assignment.group(2).casefold()) if assignment else None
        if assignment and identity in current_values:
            lines.append(
                f"{assignment.group(1)}{assignment.group(2)}{assignment.group(3)}{current_values[identity]}"
            )
        else:
            lines.append(line)
    return "\n".join(lines) + ("\n" if restored.endswith("\n") else "")


def _read_database_secrets(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        with sqlite3.connect(path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ln_settings'"
            ).fetchone()
            if not exists:
                return {}
            rows = conn.execute(
                "SELECT key,value FROM ln_settings WHERE key IN (?,?)",
                tuple(sorted(_DATABASE_SECRET_KEYS)),
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(key): str(value) for key, value in rows if str(value)}


def _sqlite_snapshot(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


# Local copies of the library beside the database: one before every restore
# (so a restore can be undone) and one a day. ``pre-v<N>`` copies are made by
# schema migrations (Database._backup_before_migration).
SNAPSHOT_KEEP = {"pre-restore": 3, "daily": 7}
# How long a restore waits for mpv helper processes to finish a database write.
RESTORE_LOCK_TIMEOUT = 10.0
_SNAPSHOT_NAME = re.compile(r"^(?:(pre-restore|daily)-(\d{8}-\d{6})(?:-(\d+))?|pre-v\d+)$")


def _snapshot_identity(database_path: Path, candidate: Path) -> tuple[str, float | None, int]:
    """(kind, time from the name, same-second counter) of a local copy; kind "" for other files."""
    prefix, suffix = database_path.name + ".", ".backup"
    name = candidate.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        return "", None, 0
    match = _SNAPSHOT_NAME.match(name[len(prefix) : -len(suffix)])
    if match is None:
        return "", None, 0
    if match.group(1) is None:
        return "migration", None, 0
    try:
        created = time.mktime(time.strptime(match.group(2), "%Y%m%d-%H%M%S"))
    except (OverflowError, ValueError):
        return "", None, 0
    return match.group(1), created, int(match.group(3) or 1)


def list_database_snapshots(database_path: Path) -> list[dict[str, Any]]:
    """Local copies of the library database, newest first."""
    database_path = Path(database_path).expanduser()
    found: list[tuple[float, int, dict[str, Any]]] = []
    try:
        candidates = list(database_path.parent.glob(database_path.name + ".*.backup"))
    except OSError:
        return []
    for candidate in candidates:
        kind, created_at, counter = _snapshot_identity(database_path, candidate)
        if not kind:
            continue
        try:
            stat = candidate.stat()
        except OSError:
            continue
        if not candidate.is_file() or stat.st_size <= 0:
            continue
        created = stat.st_mtime if created_at is None else created_at
        found.append(
            (
                created,
                counter,
                {"path": str(candidate), "kind": kind, "created_at": created, "size": stat.st_size},
            )
        )
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item for _created, _counter, item in found]


def _check_database_file(path: Path) -> None:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as conn:
        row = conn.execute("PRAGMA quick_check").fetchone()
    if row is None or str(row[0]).casefold() != "ok":
        raise ValueError(err("backup.integrityFailed"))


def make_database_snapshot(
    database_path: Path, kind: str, *, now: float | None = None, prune: bool = True
) -> Path:
    """A checked copy of the live database (SQLite backup API), then rotation of that kind."""
    database_path = Path(database_path).expanduser()
    if kind not in SNAPSHOT_KEEP:
        raise ValueError(f"unknown snapshot kind {kind!r}")
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() if now is None else now))
    # Several copies in one second count up from the highest one there: a counter freed by
    # pruning must not be reused, or the newest copy would sort as the oldest.
    counters = [0] + [
        _snapshot_identity(database_path, existing)[2]
        for existing in database_path.parent.glob(f"{database_path.name}.{kind}-{stamp}*.backup")
        if _snapshot_identity(database_path, existing)[0] == kind
    ]
    counter = max(counters) + 1
    target = database_path.with_name(
        f"{database_path.name}.{kind}-{stamp}.backup"
        if counter == 1
        else f"{database_path.name}.{kind}-{stamp}-{counter}.backup"
    )
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with closing(sqlite3.connect(database_path, timeout=10)) as source:
            with closing(sqlite3.connect(temporary)) as destination:
                # One step: a single read transaction, so the copy is consistent
                # while WAL writers go on.
                source.backup(destination)
                destination.execute("PRAGMA journal_mode=DELETE")
        _check_database_file(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    if prune:
        _prune_snapshots(database_path, kind, keep_path=target)
    return target


def _prune_snapshots(database_path: Path, kind: str, *, keep_path: Path | None = None) -> list[str]:
    same_kind = [item for item in list_database_snapshots(database_path) if item["kind"] == kind]
    removed: list[str] = []
    # Copies interrupted by a quit or crash.
    try:
        for leftover in database_path.parent.glob(f".{database_path.name}.{kind}-*.tmp"):
            if leftover.stat().st_mtime < time.time() - 3600:
                leftover.unlink(missing_ok=True)
    except OSError:
        pass
    for item in same_kind[SNAPSHOT_KEEP[kind] :]:
        if keep_path is not None and Path(item["path"]) == keep_path:
            continue
        try:
            Path(item["path"]).unlink()
            removed.append(item["path"])
        except OSError:
            continue
    return removed


def make_daily_snapshot(database_path: Path, *, now: float | None = None) -> Path | None:
    """Today's copy of the library, unless one was already made today."""
    database_path = Path(database_path).expanduser()
    if not database_path.is_file():
        return None
    current = time.time() if now is None else float(now)
    today = time.strftime("%Y%m%d", time.localtime(current))
    for item in list_database_snapshots(database_path):
        if item["kind"] == "daily" and time.strftime("%Y%m%d", time.localtime(item["created_at"])) == today:
            return None
    return make_database_snapshot(database_path, "daily", now=current)


# The Mac's iPhone/iPad identity (server ID and TLS pin) lives beside the
# database, outside it. A full backup carries it, so a restore on a new Mac is
# still the Mac the phones were paired with.
_IPHONE_IDENTITY_NAME = re.compile(r"^(server-secret|tls-[A-Za-z0-9_-]+\.pem)$")


def _iphone_identity_files(database_path: Path) -> list[Path]:
    root = database_path.parent / "iphone"
    try:
        return sorted(
            path for path in root.iterdir() if path.is_file() and _IPHONE_IDENTITY_NAME.match(path.name)
        )
    except OSError:
        return []


def _table_columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _merge_iphone_access(staged_db: Path, live_db: Path) -> None:
    """Pairings and revocations are today's, not the backup's.

    Devices and invitations of the live database are kept (a revoked iPhone
    stays revoked, a re-paired one keeps its new key); devices known only to the
    restored copy come back (a backup moved to a new Mac). The restore time lets
    the iPhone service accept a phone position newer than the restored one.
    """
    with closing(sqlite3.connect(staged_db)) as conn:
        if live_db.is_file():
            try:
                conn.execute("ATTACH DATABASE ? AS live", (str(live_db),))
            except sqlite3.Error:
                pass  # an unreadable live library has no pairings to keep
            else:
                try:
                    _copy_live_iphone_access(conn)
                    conn.commit()
                except BaseException as exc:
                    conn.rollback()
                    # Only a damaged live library ("malformed", "not a database")
                    # is skipped: the restored pairings stay.
                    if type(exc) is not sqlite3.DatabaseError:
                        raise
                finally:
                    conn.execute("DETACH DATABASE live")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state'").fetchone():
            conn.execute(
                "INSERT INTO state(key,value,updated_at) VALUES('iphone_restored_at',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (str(int(time.time() * 1000)), time.time()),
            )
        conn.commit()


def _copy_live_iphone_access(conn: sqlite3.Connection) -> None:
    for table in ("iphone_devices", "iphone_invites"):
        live_sql = conn.execute(
            "SELECT sql FROM live.sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if live_sql is None:
            continue
        if not conn.execute(
            "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            conn.execute(str(live_sql[0]))
        live_columns = set(_table_columns(conn, table, "live"))
        columns = [name for name in _table_columns(conn, table) if name in live_columns]
        key = "id" if table == "iphone_devices" else "hash"
        if key not in columns:
            continue
        revoked_before: set[tuple[str, str]] = set()
        if table == "iphone_devices" and {"token_hash", "revoked"} <= set(columns):
            revoked_before = {
                (str(row[0]), str(row[1]))
                for row in conn.execute("SELECT id,token_hash FROM main.iphone_devices WHERE revoked<>0")
            }
        names = ",".join(columns)
        updates = ",".join(f"{name}=excluded.{name}" for name in columns if name != key)
        conn.execute(
            f"INSERT INTO main.{table}({names}) SELECT {names} FROM live.{table} WHERE true "
            f"ON CONFLICT({key}) DO " + (f"UPDATE SET {updates}" if updates else "NOTHING")
        )
        # The same key revoked in the restored copy stays revoked.
        for device, token_hash in revoked_before:
            conn.execute(
                "UPDATE main.iphone_devices SET revoked=1 WHERE id=? AND token_hash=?",
                (device, token_hash),
            )


def create_backup(
    *, config_path: Path, database_path: Path, cache_dir: Path, output: Path, version: str
) -> dict[str, Any]:
    config_path = config_path.expanduser()
    database_path = database_path.expanduser()
    cache_dir = cache_dir.expanduser()
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"{APP_SLUG}-backup-") as raw_tmp:
        tmp = Path(raw_tmp)
        db_copy = tmp / "library.sqlite3"
        if database_path.exists():
            _sqlite_snapshot(database_path, db_copy)
        else:
            sqlite3.connect(db_copy).close()

        cached_files: list[dict[str, str]] = []
        redacted_secret_count = 0
        with sqlite3.connect(db_copy) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT DISTINCT subtitle_path FROM episodes WHERE subtitle_path IS NOT NULL AND subtitle_path<>''"
                ).fetchall()
            except sqlite3.Error:
                rows = []
            try:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ln_settings'"
                ).fetchone()
                if exists:
                    cursor = conn.execute(
                        "UPDATE ln_settings SET value='' WHERE key IN (?,?) AND value<>''",
                        tuple(sorted(_DATABASE_SECRET_KEYS)),
                    )
                    redacted_secret_count += int(cursor.rowcount or 0)
                    conn.commit()
                    # Rebuild pages so removed tokens cannot survive in SQLite
                    # freelist space inside the supposedly sanitized archive.
                    conn.execute("VACUUM")
                    conn.execute("PRAGMA journal_mode=DELETE")
            except sqlite3.Error:
                pass
        durable_subtitle_dir = database_path.parent / "prepared-subtitles"
        for index, row in enumerate(rows, start=1):
            original = Path(str(row["subtitle_path"])).expanduser()
            try:
                original_resolved = original.resolve()
            except OSError:
                continue
            storage = ""
            try:
                original_resolved.relative_to(cache_dir.resolve())
                storage = "cache"
            except (OSError, ValueError):
                try:
                    original_resolved.relative_to(durable_subtitle_dir.resolve())
                    storage = "durable"
                except (OSError, ValueError):
                    continue
            if not original.is_file():
                continue
            prefix = "prepared-subtitles" if storage == "durable" else "cache/subtitles"
            archive_name = f"{prefix}/{index:04d}-{original.name}"
            cached_files.append(
                {
                    "original": str(original),
                    "archive": archive_name,
                    "storage": storage,
                }
            )

        identity_files = _iphone_identity_files(database_path)
        manifest = {
            "app": BACKUP_APP_ID,
            "format": BACKUP_FORMAT,
            "version": version,
            "created_at": time.time(),
            "config_path": str(config_path),
            "database_path": str(database_path),
            "cache_dir": str(cache_dir),
            "cached_files": cached_files,
            "includes_media": False,
            "secrets_included": False,
            "redacted_secret_count": redacted_secret_count,
            "iphone_identity": [path.name for path in identity_files],
        }
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            if config_path.exists():
                redacted_config, config_secret_count = _redact_config(config_path.read_text(encoding="utf-8"))
                redacted_secret_count += config_secret_count
                manifest["redacted_secret_count"] = redacted_secret_count
                archive.writestr("config.toml", redacted_config)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.write(db_copy, "library.sqlite3")
            for item in cached_files:
                archive.write(Path(item["original"]), item["archive"])
            for path in identity_files:
                archive.write(path, f"iphone/{path.name}")
    return {
        "path": str(output),
        "cached_files": len(cached_files),
        "includes_media": False,
    }


def _atomic_copy(source: Path, target: Path) -> None:
    target = target.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{target.name}.restore-",
        dir=str(target.parent),
    )
    os.close(fd)
    temporary = Path(raw_temp)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


# These locations belong to the receiving machine, not to the backup producer.
# Preserve the existing effective paths and never copy absolute paths from an
# unrelated home directory into a restored runtime configuration.
_LOCAL_PATH_FIELDS = {
    "library": {"database_path", "root_dir", "cover_cache_dir"},
    "paths": {"cache_dir", "subtitle_dirs", "watched_media_dirs", "download_dirs"},
}


def _restore_config_for_destination(
    restored_text: str, current_text: str, *, database_path: Path, cache_dir: Path
) -> str:
    from .branding import DEFAULT_LIBRARY_DIR
    from .config import DEFAULT_CACHE_DIR

    restored = tomllib.loads(restored_text)
    try:
        current = tomllib.loads(current_text) if current_text.strip() else {}
    except tomllib.TOMLDecodeError:
        # A damaged current config must not prevent recovery from a valid backup.
        # In that case retain only the explicitly supplied destination DB/cache.
        current = {}
    if not isinstance(restored, dict) or not isinstance(current, dict):
        raise ValueError(err("backup.invalidConfig"))
    for section_name, keys in _LOCAL_PATH_FIELDS.items():
        section_values = restored.get(section_name, {})
        if not isinstance(section_values, dict):
            raise ValueError(err("backup.invalidSection", section=section_name))
        for key, value in section_values.items():
            if key in keys:
                valid = (
                    isinstance(value, list) and all(isinstance(item, str) for item in value)
                    if key in {"subtitle_dirs", "watched_media_dirs", "download_dirs"}
                    else isinstance(value, str)
                )
                if not valid:
                    raise ValueError(err("backup.invalidPath", section=section_name, key=key))

    if not isinstance(current.get("paths", {}), dict) or not isinstance(current.get("library", {}), dict):
        raise ValueError(err("backup.invalidExistingPaths"))

    local: dict[tuple[str, str], Any] = {
        ("library", "database_path"): str(database_path),
        ("library", "root_dir"): str(current.get("library", {}).get("root_dir", DEFAULT_LIBRARY_DIR)),
        ("library", "cover_cache_dir"): str(
            current.get("library", {}).get("cover_cache_dir", DEFAULT_CACHE_DIR / "covers")
        ),
        ("paths", "cache_dir"): str(cache_dir),
    }
    current_paths = current.get("paths", {})
    for key in ("subtitle_dirs", "watched_media_dirs"):
        if key in current_paths:
            value = current_paths[key]
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                raise ValueError(err("backup.invalidExistingPath", key=key))
            local[("paths", key)] = value
    # Old installations may still use download_dirs rather than watched_media_dirs.
    if "watched_media_dirs" not in current_paths and "download_dirs" in current_paths:
        value = current_paths["download_dirs"]
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError(err("backup.invalidExistingPath", key="download_dirs"))
        local[("paths", "download_dirs")] = value

    def literal(value: Any) -> str:
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in value) + "]"
        raise ValueError(err("backup.invalidDestination"))

    # Preserve comments, unknown settings and non-path settings. TOML arrays for
    # the operational paths are expected to use Pudge's one-line writer format.
    # Reject nonstandard multiline assignments instead of risking silent data loss.
    lines = restored_text.splitlines(keepends=True)
    result: list[str] = []
    section = ""
    seen: set[tuple[str, str]] = set()
    for line in lines:
        section_match = re.match(r"^\s*\[([^]\n]+)]\s*(?:#.*)?$", line.rstrip("\r\n"))
        if section_match:
            section = section_match.group(1).strip()
        assignment = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
        key = assignment.group(1) if assignment else ""
        identity = (section, key)
        if section in _LOCAL_PATH_FIELDS and key in _LOCAL_PATH_FIELDS[section]:
            if identity in seen:
                raise ValueError(err("backup.duplicateLocation"))
            seen.add(identity)
            # After removing this one line, the document must still be complete.
            # A multiline value would fail the final tomllib validation.
            if identity in local:
                result.append(f"{key} = {literal(local[identity])}\n")
            continue
        result.append(line)

    text = "".join(result)
    for sec in ("library", "paths"):
        missing = [
            (key, value) for (name, key), value in local.items() if name == sec and (name, key) not in seen
        ]
        if not missing:
            continue
        lines = text.splitlines(keepends=True)
        start = next((i for i, line in enumerate(lines) if line.strip() == f"[{sec}]"), None)
        additions = [f"{key} = {literal(value)}\n" for key, value in missing]
        if start is None:
            text = text.rstrip("\n") + f"\n\n[{sec}]\n" + "".join(additions)
        else:
            lines[start + 1 : start + 1] = additions
            text = "".join(lines)

    checked = tomllib.loads(text)
    for sec, key in (
        ("library", "database_path"),
        ("library", "root_dir"),
        ("library", "cover_cache_dir"),
        ("paths", "cache_dir"),
    ):
        value = checked[sec][key]
        if not isinstance(value, str):
            raise ValueError(err("backup.invalidPathType", section=sec, key=key))
    for key in ("subtitle_dirs", "watched_media_dirs", "download_dirs"):
        value = checked.get("paths", {}).get(key, [])
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError(err("backup.invalidBackupPath", key=key))
    return text


def _validate_staged_config(staged_config: Path, *, database_path: Path, cache_dir: Path) -> None:
    # Full application parser, not merely syntactic TOML validation. This runs
    # before live database/config replacement, so malformed settings cannot
    # strand the application in an unbootable state.
    from .config import CONFIG_WARNINGS, load_config

    parsed = load_config(  # only validates the backup
        staged_config, apply_language=False, recover_invalid=False
    )
    if CONFIG_WARNINGS:
        # The app itself starts with defaults; a backup must be valid as written.
        raise ValueError(err("backup.invalidValues", details="; ".join(CONFIG_WARNINGS[:3])))
    if parsed.library.database_path.expanduser().resolve() != database_path.expanduser().resolve():
        raise ValueError(err("backup.otherDatabase"))
    if parsed.paths.cache_dir.expanduser().resolve() != cache_dir.expanduser().resolve():
        raise ValueError(err("backup.otherCache"))


def restore_backup(
    *,
    archive_path: Path,
    config_path: Path,
    database_path: Path,
    cache_dir: Path,
    post_commit: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Validate completely in staging, then replace live files with rollback.

    Callers that own long-lived services should quiesce their writers before this
    function enters its commit phase.  The function itself guarantees that an
    invalid archive never touches live files and that a commit failure restores
    the previous database/config/cached subtitle set.
    """

    archive_path = archive_path.expanduser()
    config_path = config_path.expanduser()
    database_path = database_path.expanduser()
    cache_dir = cache_dir.expanduser()
    if not archive_path.is_file():
        raise FileNotFoundError(archive_path)

    with tempfile.TemporaryDirectory(prefix=f"{APP_SLUG}-restore-") as raw_tmp:
        tmp = Path(raw_tmp)
        restored_db = tmp / "library.sqlite3"
        restored_config = tmp / "config.toml"
        staged_cached_dir = tmp / "cached"
        staged_cached_dir.mkdir(parents=True, exist_ok=True)

        staged_cached: list[tuple[str, Path, Path]] = []
        restored_paths: dict[str, str] = {}
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            if "manifest.json" not in names or "library.sqlite3" not in names:
                raise ValueError(err("backup.notBackup", app=APP_NAME))
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            backup_format = int(manifest.get("format", 0))
            if manifest.get("app") != BACKUP_APP_ID or backup_format not in {1, BACKUP_FORMAT}:
                raise ValueError(err("backup.unsupportedFormat", app=APP_NAME))

            with archive.open("library.sqlite3") as source, restored_db.open("wb") as destination:
                shutil.copyfileobj(source, destination)
            if "config.toml" in names:
                with archive.open("config.toml") as source, restored_config.open("wb") as destination:
                    shutil.copyfileobj(source, destination)

            for index, item in enumerate(manifest.get("cached_files", []), start=1):
                if not isinstance(item, dict):
                    continue
                member = str(item.get("archive") or "")
                original = str(item.get("original") or "")
                if not member or member not in names or not original:
                    continue
                storage = str(item.get("storage") or "")
                if storage == "durable" or member.startswith("prepared-subtitles/"):
                    target = database_path.parent / "prepared-subtitles" / Path(member).name
                else:
                    target = cache_dir / "restored-subtitles" / Path(member).name
                staged = staged_cached_dir / f"{index:04d}-{Path(member).name}"
                with archive.open(member) as source, staged.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                staged_cached.append((original, staged, target))
                restored_paths[original] = str(target)

            staged_identity: list[tuple[Path, Path, int | None]] = []
            identity_dir = database_path.parent / "iphone"
            for name in manifest.get("iphone_identity", []) if backup_format >= 2 else []:
                name = str(name)
                member = f"iphone/{name}"
                if not _IPHONE_IDENTITY_NAME.match(name) or member not in names:
                    continue
                data = archive.read(member)
                if name == "server-secret" and len(data) != 32:
                    raise ValueError(err("backup.invalidIphoneIdentity"))
                staged = tmp / f"iphone-{name}"
                staged.write_bytes(data)
                staged_identity.append((staged, identity_dir / name, 0o600))

        # Everything below this line works only with staged files until all
        # validation and path rewrites have succeeded.
        existing_database_secrets = _read_database_secrets(database_path)
        with closing(sqlite3.connect(restored_db)) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).casefold() != "ok":
                raise ValueError(err("backup.integrityFailed"))
            for old_path, new_path in restored_paths.items():
                conn.execute(
                    "UPDATE episodes SET subtitle_path=? WHERE subtitle_path=?",
                    (new_path, old_path),
                )
            conn.commit()
        _keep_database_secrets(restored_db, existing_database_secrets)
        _merge_iphone_access(restored_db, database_path)
        with closing(sqlite3.connect(restored_db)) as conn:
            final_integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if final_integrity is None or str(final_integrity[0]).casefold() != "ok":
                raise ValueError(err("backup.preparedIntegrityFailed"))

        staged_config: Path | None = None
        if restored_config.exists():
            staged_config = tmp / "config.final.toml"
            current_text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
            incoming_text = restored_config.read_text(encoding="utf-8")
            merged = (
                _merge_config_secrets(incoming_text, current_text)
                if backup_format >= 2 and current_text
                else incoming_text
            )
            staged_config.write_text(
                _restore_config_for_destination(
                    merged, current_text, database_path=database_path, cache_dir=cache_dir
                ),
                encoding="utf-8",
            )
            _validate_staged_config(staged_config, database_path=database_path, cache_dir=cache_dir)

        files: list[tuple[Path, Path, int | None]] = []
        if staged_config is not None:
            files.append((staged_config, config_path, 0o600))
        files.extend((staged, target, None) for _original, staged, target in staged_cached)
        if staged_identity:
            identity_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            files.extend(staged_identity)
        undo = _commit_restore(
            database_path=database_path,
            restored_db=restored_db,
            files=files,
            rollback_dir=tmp / "rollback",
            post_commit=post_commit,
        )

    return {
        "path": str(archive_path),
        "restored_cached_files": len(restored_paths),
        "restored_iphone_identity": len(staged_identity),
        "undo_snapshot": str(undo) if undo is not None else "",
        "restart_required": True,
    }


def _keep_database_secrets(staged_db: Path, secrets: dict[str, str]) -> None:
    """API keys of this Mac win over the (redacted or older) ones of the restored copy."""
    if not secrets:
        return
    with closing(sqlite3.connect(staged_db)) as conn:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ln_settings'"
        ).fetchone():
            return
        for key, value in secrets.items():
            conn.execute(
                "INSERT INTO ln_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        conn.commit()


def _commit_restore(
    *,
    database_path: Path,
    restored_db: Path,
    files: list[tuple[Path, Path, int | None]],
    rollback_dir: Path,
    post_commit: Callable[[], None] | None,
) -> Path | None:
    """Swap the live database (and files) with rollback; returns the undo copy.

    The current library is first copied to ``<db>.pre-restore-<time>.backup``:
    the rollback source if anything fails, and afterwards the copy that undoes
    this restore. Helper processes (mpv) cannot open the database meanwhile.
    """
    rollback_dir.mkdir(parents=True, exist_ok=True)
    file_rollbacks: list[tuple[Path, Path | None, int | None]] = []
    for index, (_staged, target, mode) in enumerate(files, start=1):
        if target.is_file():
            saved = rollback_dir / f"file-{index:04d}-{target.name}"
            shutil.copy2(target, saved)
            file_rollbacks.append((target, saved, mode))
        else:
            file_rollbacks.append((target, None, mode))
    try:
        with exclusive_database(database_path, timeout=RESTORE_LOCK_TIMEOUT):
            undo: Path | None = None
            damaged: Path | None = None
            if database_path.is_file():
                try:
                    undo = make_database_snapshot(database_path, "pre-restore", prune=False)
                except (sqlite3.Error, ValueError):
                    # The live library is damaged (often the reason to restore):
                    # keep it as it is for the rollback, never offered as a copy.
                    damaged = database_path.with_name(
                        f"{database_path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}.backup"
                    )
                    shutil.copy2(database_path, damaged)
                    for suffix in ("-wal", "-shm"):
                        side = Path(str(database_path) + suffix)
                        if side.is_file():
                            shutil.copy2(side, Path(str(damaged) + suffix))
            try:
                # No writer may be active while WAL/SHM are removed. WebApp owns
                # that lifecycle guarantee before calling restore; helper
                # processes wait on the restore lock.
                for suffix in ("-wal", "-shm"):
                    Path(str(database_path) + suffix).unlink(missing_ok=True)
                _atomic_copy(restored_db, database_path)
                for staged, target, mode in files:
                    _atomic_copy(staged, target)
                    if mode is not None:
                        target.chmod(mode)
                # Keep rollback copies alive through service rebind. The caller
                # must not declare success until the new runtime is operational.
                if post_commit is not None:
                    post_commit()
            except Exception:
                for target, saved, mode in reversed(file_rollbacks):
                    if saved is None:
                        target.unlink(missing_ok=True)
                    else:
                        _atomic_copy(saved, target)
                        if mode is not None:
                            target.chmod(mode)
                for suffix in ("-wal", "-shm"):
                    Path(str(database_path) + suffix).unlink(missing_ok=True)
                if undo is not None:
                    _atomic_copy(undo, database_path)
                    undo.unlink(missing_ok=True)
                elif damaged is not None:
                    for suffix in ("-wal", "-shm"):
                        side = Path(str(damaged) + suffix)
                        if side.is_file():
                            _atomic_copy(side, Path(str(database_path) + suffix))
                    _atomic_copy(damaged, database_path)
                else:
                    database_path.unlink(missing_ok=True)
                raise
    except DatabaseInUse as exc:
        raise RuntimeError(err("backup.helpersBusy")) from exc
    _prune_snapshots(database_path, "pre-restore", keep_path=undo)
    return undo


def restore_database_snapshot(
    *,
    snapshot_path: Path,
    database_path: Path,
    post_commit: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Put a local copy of the library back (before a restore, daily, before an update).

    Settings and files stay as they are; the current library is kept as a
    pre-restore copy first, so this can be undone the same way.
    """
    database_path = Path(database_path).expanduser()
    snapshot_path = Path(snapshot_path).expanduser()
    if not any(Path(item["path"]) == snapshot_path for item in list_database_snapshots(database_path)):
        raise ValueError(err("backup.unknownSnapshot"))
    with tempfile.TemporaryDirectory(prefix=f"{APP_SLUG}-restore-") as raw_tmp:
        tmp = Path(raw_tmp)
        staged = tmp / "library.sqlite3"
        shutil.copy2(snapshot_path, staged)
        with closing(sqlite3.connect(staged)) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).casefold() != "ok":
                raise ValueError(err("backup.integrityFailed"))
        _keep_database_secrets(staged, _read_database_secrets(database_path))
        _merge_iphone_access(staged, database_path)
        undo = _commit_restore(
            database_path=database_path,
            restored_db=staged,
            files=[],
            rollback_dir=tmp / "rollback",
            post_commit=post_commit,
        )
    return {
        "path": str(snapshot_path),
        "restored_cached_files": 0,
        "undo_snapshot": str(undo) if undo is not None else "",
        "restart_required": True,
    }
