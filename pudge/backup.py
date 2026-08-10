from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

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
        for index, row in enumerate(rows, start=1):
            original = Path(str(row["subtitle_path"])).expanduser()
            try:
                original_resolved = original.resolve()
                cache_resolved = cache_dir.resolve()
                original_resolved.relative_to(cache_resolved)
            except (OSError, ValueError):
                continue
            if not original.is_file():
                continue
            archive_name = f"cache/subtitles/{index:04d}-{original.name}"
            cached_files.append({"original": str(original), "archive": archive_name})

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


def restore_backup(*, archive_path: Path, config_path: Path, database_path: Path, cache_dir: Path) -> dict[str, Any]:
    archive_path = archive_path.expanduser()
    if not archive_path.is_file():
        raise FileNotFoundError(archive_path)
    with tempfile.TemporaryDirectory(prefix=f"{APP_SLUG}-restore-") as raw_tmp:
        tmp = Path(raw_tmp)
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            if "manifest.json" not in names or "library.sqlite3" not in names:
                raise ValueError(f"This is not an {APP_NAME} backup")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            backup_format = int(manifest.get("format", 0))
            if manifest.get("app") != BACKUP_APP_ID or backup_format not in {1, BACKUP_FORMAT}:
                raise ValueError(f"Unsupported {APP_NAME} backup format")
            archive.extract("library.sqlite3", tmp)
            if "config.toml" in names:
                archive.extract("config.toml", tmp)
            restored_paths: dict[str, str] = {}
            for item in manifest.get("cached_files", []):
                if not isinstance(item, dict):
                    continue
                member = str(item.get("archive") or "")
                original = str(item.get("original") or "")
                if not member or member not in names or not original:
                    continue
                target = cache_dir.expanduser() / "restored-subtitles" / Path(member).name
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                restored_paths[original] = str(target)

        existing_database_secrets = _read_database_secrets(database_path.expanduser())
        restored_db = tmp / "library.sqlite3"
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

        database_path = database_path.expanduser()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(database_path) + suffix).unlink(missing_ok=True)
        shutil.copy2(restored_db, database_path)
        restored_config = tmp / "config.toml"
        if restored_config.exists():
            config_path = config_path.expanduser()
            config_path.parent.mkdir(parents=True, exist_ok=True)
            if backup_format >= 2 and config_path.is_file():
                merged = _merge_config_secrets(
                    restored_config.read_text(encoding="utf-8"),
                    config_path.read_text(encoding="utf-8"),
                )
                config_path.write_text(merged, encoding="utf-8")
            else:
                shutil.copy2(restored_config, config_path)
            config_path.chmod(0o600)
    return {
        "path": str(archive_path),
        "restored_cached_files": len(restored_paths),
        "restart_required": True,
    }
