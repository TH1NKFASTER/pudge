from __future__ import annotations

import hashlib
import json
import platform
import time
import zipfile
from pathlib import Path
from typing import Any

from .database import Database


class DiagnosticRecorder:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(
        self,
        correlation_id: str,
        component: str,
        event: str,
        *,
        severity: str = "info",
        media_id: int | None = None,
        media_episode: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        with self.database.connect() as conn:
            self.database._ensure_anime_parent(conn, media_id)
            cursor = conn.execute(
                "INSERT INTO diagnostic_events(correlation_id,component,event,severity,media_id,"
                "media_episode,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    str(correlation_id),
                    str(component),
                    str(event),
                    str(severity),
                    media_id,
                    media_episode,
                    json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str),
                    time.time(),
                ),
            )
        return int(cursor.lastrowid or 0)

    def recent(
        self,
        *,
        correlation_id: str = "",
        media_id: int | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        values: list[Any] = []
        if correlation_id:
            where.append("correlation_id=?")
            values.append(str(correlation_id))
        if media_id is not None:
            where.append("media_id=?")
            values.append(int(media_id))
        sql = "SELECT * FROM diagnostic_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        values.append(max(1, min(5000, int(limit))))
        with self.database.connect() as conn:
            rows = conn.execute(sql, values).fetchall()
        result: list[dict[str, Any]] = []
        for row in reversed(rows):
            item = dict(row)
            raw_payload = item.pop("payload_json", "{}")
            try:
                payload = json.loads(str(raw_payload or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            item["payload"] = payload if isinstance(payload, dict) else {}
            result.append(item)
        return result


class DebugBundleBuilder:
    """Build one redacted support bundle with state, logs and final subtitles."""

    SUBTITLE_SUFFIXES = {".srt", ".ass", ".ssa", ".vtt", ".txt", ".json", ".jsonl"}

    def __init__(self, database: Database, recorder: DiagnosticRecorder) -> None:
        self.database = database
        self.recorder = recorder

    @classmethod
    def _redact(cls, value: Any, *, key: str = "") -> Any:
        sensitive = {"token", "password", "secret", "api_key", "authorization"}
        normalized = key.casefold().replace("-", "_")
        if any(part in normalized for part in sensitive):
            return "••••••••" if value else value
        if isinstance(value, dict):
            return {
                str(child_key): cls._redact(child, key=str(child_key)) for child_key, child in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(child) for child in value]
        return value

    def _database_summary(self) -> dict[str, Any]:
        with self.database.connect() as conn:
            tables = [
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            counts = {
                table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) for table in tables
            }
            integrity = conn.execute("PRAGMA quick_check").fetchone()
            version = conn.execute("PRAGMA user_version").fetchone()
        return {
            "integrity": str(integrity[0] if integrity else "unknown"),
            "schema_version": int(version[0] if version else 0),
            "row_counts": counts,
        }

    @classmethod
    def _paths_from_payload(cls, value: Any) -> set[Path]:
        result: set[Path] = set()
        if isinstance(value, dict):
            for child in value.values():
                result.update(cls._paths_from_payload(child))
        elif isinstance(value, list):
            for child in value:
                result.update(cls._paths_from_payload(child))
        elif isinstance(value, str):
            candidate = Path(value).expanduser()
            if candidate.suffix.casefold() in cls.SUBTITLE_SUFFIXES and candidate.is_file():
                result.add(candidate.resolve())
        return result

    def build(
        self,
        target: Path,
        *,
        version: str,
        frontend: dict[str, Any] | None = None,
        snapshots: list[dict[str, Any]] | None = None,
        logs: dict[str, Path] | None = None,
    ) -> Path:
        target = Path(target).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        snapshot_rows = list(snapshots or [])
        manifest = {
            "schema": 2,
            "generated_at": time.time(),
            "pudge_version": str(version),
            "platform": platform.platform(),
            "frontend": self._redact(dict(frontend or {})),
            "database": self._database_summary(),
        }
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
            archive.writestr(
                "diagnostic-events.json",
                json.dumps(
                    self._redact(self.recorder.recent(limit=2000)),
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
            )
            for index, snapshot in enumerate(snapshot_rows, start=1):
                archive.writestr(
                    f"episodes/snapshot-{index}.json",
                    json.dumps(
                        self._redact(snapshot),
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                )
            for label, path in (logs or {}).items():
                source = Path(path).expanduser()
                if source.is_file():
                    archive.write(source, f"logs/{label}{source.suffix or '.log'}")
            subtitle_paths: set[Path] = set()
            for snapshot in snapshot_rows:
                subtitle_paths.update(self._paths_from_payload(snapshot))
            for path in sorted(subtitle_paths):
                try:
                    if path.stat().st_size > 20 * 1024 * 1024:
                        continue
                    digest = hashlib.sha256(str(path).encode()).hexdigest()[:10]
                    archive.write(path, f"subtitles/{digest}-{path.name}")
                except OSError:
                    continue
        return target
