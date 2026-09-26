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
from pathlib import Path
from typing import Any, Callable

from .branding import APP_NAME, BACKUP_APP_ID, APP_SLUG


BACKUP_FORMAT = 2

_CONFIG_SECRET_KEYS = {
    ("jimaku", "api_key"),
    ("anilist", "access_token"),
    ("llm", "api_key"),
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


def create_backup(*, config_path: Path, database_path: Path, cache_dir: Path, output: Path, version: str) -> dict[str, Any]:
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
        }
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            if config_path.exists():
                redacted_config, config_secret_count = _redact_config(
                    config_path.read_text(encoding="utf-8")
                )
                redacted_secret_count += config_secret_count
                manifest["redacted_secret_count"] = redacted_secret_count
                archive.writestr("config.toml", redacted_config)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.write(db_copy, "library.sqlite3")
            for item in cached_files:
                archive.write(Path(item["original"]), item["archive"])
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
        raise ValueError("Invalid backup configuration")
    for section_name, keys in _LOCAL_PATH_FIELDS.items():
        section_values = restored.get(section_name, {})
        if not isinstance(section_values, dict):
            raise ValueError(f"Invalid backup [{section_name}] section")
        for key, value in section_values.items():
            if key in keys:
                valid = (
                    isinstance(value, list) and all(isinstance(item, str) for item in value)
                    if key in {"subtitle_dirs", "watched_media_dirs", "download_dirs"}
                    else isinstance(value, str)
                )
                if not valid:
                    raise ValueError(f"Invalid backup path {section_name}.{key}")

    if not isinstance(current.get("paths", {}), dict) or not isinstance(current.get("library", {}), dict):
        raise ValueError("Invalid existing configuration paths")

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
                raise ValueError(f"Invalid existing paths.{key}")
            local[("paths", key)] = value
    # Old installations may still use download_dirs rather than watched_media_dirs.
    if "watched_media_dirs" not in current_paths and "download_dirs" in current_paths:
        value = current_paths["download_dirs"]
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError("Invalid existing paths.download_dirs")
        local[("paths", "download_dirs")] = value

    def literal(value: Any) -> str:
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in value) + "]"
        raise ValueError("Invalid destination path setting")

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
                raise ValueError("Duplicate location setting in backup")
            seen.add(identity)
            # After removing this one line, the document must still be complete.
            # A multiline value would fail the final tomllib validation.
            if identity in local:
                result.append(f"{key} = {literal(local[identity])}\n")
            continue
        result.append(line)

    text = "".join(result)
    for sec in ("library", "paths"):
        missing = [(key, value) for (name, key), value in local.items() if name == sec and (name, key) not in seen]
        if not missing:
            continue
        lines = text.splitlines(keepends=True)
        start = next((i for i, line in enumerate(lines)
                      if line.strip() == f"[{sec}]"), None)
        additions = [f"{key} = {literal(value)}\n" for key, value in missing]
        if start is None:
            text = text.rstrip("\n") + f"\n\n[{sec}]\n" + "".join(additions)
        else:
            lines[start + 1:start + 1] = additions
            text = "".join(lines)

    checked = tomllib.loads(text)
    for sec, key in (("library", "database_path"), ("library", "root_dir"),
                     ("library", "cover_cache_dir"), ("paths", "cache_dir")):
        value = checked[sec][key]
        if not isinstance(value, str):
            raise ValueError(f"Invalid path type: {sec}.{key}")
    for key in ("subtitle_dirs", "watched_media_dirs", "download_dirs"):
        value = checked.get("paths", {}).get(key, [])
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError(f"Invalid paths.{key}")
    return text


def _validate_staged_config(staged_config: Path, *, database_path: Path, cache_dir: Path) -> None:
    # Full application parser, not merely syntactic TOML validation. This runs
    # before live database/config replacement, so malformed settings cannot
    # strand the application in an unbootable state.
    from .config import load_config

    parsed = load_config(staged_config)
    if parsed.library.database_path.expanduser().resolve() != database_path.expanduser().resolve():
        raise ValueError("Restored configuration points to a different database")
    if parsed.paths.cache_dir.expanduser().resolve() != cache_dir.expanduser().resolve():
        raise ValueError("Restored configuration points to a different cache directory")


def restore_backup(*, archive_path: Path, config_path: Path, database_path: Path, cache_dir: Path, post_commit: Callable[[], None] | None = None) -> dict[str, Any]:
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
                raise ValueError(f"This is not an {APP_NAME} backup")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            backup_format = int(manifest.get("format", 0))
            if manifest.get("app") != BACKUP_APP_ID or backup_format not in {1, BACKUP_FORMAT}:
                raise ValueError(f"Unsupported {APP_NAME} backup format")

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

        # Everything below this line works only with staged files until all
        # validation and path rewrites have succeeded.
        existing_database_secrets = _read_database_secrets(database_path)
        with sqlite3.connect(restored_db) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).casefold() != "ok":
                raise ValueError("Backup database failed integrity check")
            for old_path, new_path in restored_paths.items():
                conn.execute(
                    "UPDATE episodes SET subtitle_path=? WHERE subtitle_path=?",
                    (new_path, old_path),
                )
            if existing_database_secrets:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ln_settings'"
                ).fetchone()
                if table:
                    for key, value in existing_database_secrets.items():
                        conn.execute(
                            "INSERT INTO ln_settings(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, value),
                        )
            conn.commit()
            final_integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if final_integrity is None or str(final_integrity[0]).casefold() != "ok":
                raise ValueError("Prepared backup database failed integrity check")

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

        rollback_dir = tmp / "rollback"
        rollback_dir.mkdir(parents=True, exist_ok=True)
        rollback_db = rollback_dir / "library.sqlite3"
        database_existed = database_path.is_file()
        if database_existed:
            _sqlite_snapshot(database_path, rollback_db)

        rollback_config = rollback_dir / "config.toml"
        config_existed = config_path.is_file()
        if config_existed:
            shutil.copy2(config_path, rollback_config)

        cache_rollbacks: list[tuple[Path, Path | None]] = []
        for index, (_original, _staged, target) in enumerate(staged_cached, start=1):
            if target.is_file():
                backup_target = rollback_dir / f"cached-{index:04d}-{target.name}"
                shutil.copy2(target, backup_target)
                cache_rollbacks.append((target, backup_target))
            else:
                cache_rollbacks.append((target, None))

        try:
            # No writer may be active while WAL/SHM are removed. WebApp owns
            # that lifecycle guarantee before calling restore_backup().
            for suffix in ("-wal", "-shm"):
                Path(str(database_path) + suffix).unlink(missing_ok=True)
            _atomic_copy(restored_db, database_path)
            if staged_config is not None:
                _atomic_copy(staged_config, config_path)
                config_path.chmod(0o600)
            for _original, staged, target in staged_cached:
                _atomic_copy(staged, target)
            # Keep rollback snapshots alive through service rebind. The caller
            # must not declare success until the new runtime is operational.
            if post_commit is not None:
                post_commit()
        except Exception:
            for target, backup_target in reversed(cache_rollbacks):
                if backup_target is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_copy(backup_target, target)
            if staged_config is not None:
                if config_existed:
                    _atomic_copy(rollback_config, config_path)
                    config_path.chmod(0o600)
                else:
                    config_path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(database_path) + suffix).unlink(missing_ok=True)
            if database_existed:
                _atomic_copy(rollback_db, database_path)
            else:
                database_path.unlink(missing_ok=True)
            raise

    return {
        "path": str(archive_path),
        "restored_cached_files": len(restored_paths),
        "restart_required": True,
    }
