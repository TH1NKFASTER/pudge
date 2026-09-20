from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import time
import uuid
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .database import Database

LEDGER_SCHEMA_VERSION = 2
MAX_CHUNK_SECONDS = 30.0
MAX_PAYLOAD_BYTES = 64 * 1024


class ConsumptionLedgerError(RuntimeError):
    pass


class ConsumptionConflictError(ConsumptionLedgerError):
    pass


@dataclass(frozen=True, slots=True)
class ConsumptionMediaRef:
    media_uuid: str
    kind: str
    title: str
    source_revision: str = ""


@dataclass(frozen=True, slots=True)
class ConsumptionAppendResult:
    event_id: str
    device_id: str
    device_seq: int
    duplicate: bool


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _norm(value: str) -> str:
    return " ".join(str(value or "").split()).strip().casefold()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()




def _system_timezone_metadata(timestamp: float) -> tuple[str, int]:
    moment = datetime.fromtimestamp(float(timestamp)).astimezone()
    offset = int((moment.utcoffset().total_seconds() if moment.utcoffset() else 0.0))
    configured = str(os.environ.get("TZ") or "").strip()
    if configured and not configured.startswith(":"):
        return configured, offset
    for candidate in (Path("/etc/localtime"), Path("/var/db/timezone/zoneinfo")):
        try:
            resolved = str(candidate.resolve())
            if "/zoneinfo/" in resolved:
                return resolved.split("/zoneinfo/", 1)[1], offset
        except OSError:
            pass
    return str(moment.tzname() or "local"), offset

def _file_sample_fingerprint(path: Path, *, sample_bytes: int = 65536) -> str:
    """Cheap content fingerprint stable across renames.

    Full media files can be many gigabytes; R7 only needs a conservative alias
    candidate. Size + first/last samples avoids path/mtime identity while keeping
    the cost bounded. It is not used as a cryptographic proof: conflicting aliases
    still fail closed in ``ensure_media``.
    """
    target = Path(path).expanduser()
    try:
        stat = target.stat()
        size = max(0, int(stat.st_size))
        digest = hashlib.sha256()
        digest.update(f"size:{size}:".encode("ascii"))
        with target.open("rb") as handle:
            digest.update(handle.read(max(1024, int(sample_bytes))))
            if size > sample_bytes:
                handle.seek(max(0, size - max(1024, int(sample_bytes))))
                digest.update(handle.read(max(1024, int(sample_bytes))))
        return digest.hexdigest()
    except OSError:
        return ""


class ConsumptionLedger:
    """Durable, append-only foundation for immersion statistics.

    R7 intentionally stores facts and stable identities only. Aggregation/UI is
    R8. Events are small, idempotent chunks; current progress remains owned by
    the existing media tables and is never reconstructed from this ledger.
    """

    def __init__(self, database: Database, *, logger: Any = None) -> None:
        self.database = database
        self.logger = logger

    @staticmethod
    def _queue_sync_record(
        conn: Any, record_type: str, record_key: str, payload: dict[str, Any], *, created_at: float | None = None
    ) -> None:
        record_type = str(record_type)
        record_key = str(record_key)
        payload_json = _json(payload)
        existing = conn.execute(
            "SELECT payload_json FROM consumption_sync_outbox WHERE record_type=? AND record_key=?",
            (record_type, record_key),
        ).fetchone()
        if existing is not None and str(existing["payload_json"] or "") == payload_json:
            return
        # A changed logical record needs a new monotonic cursor. Updating the old
        # row in place would make clients that already passed that cursor miss it.
        if existing is not None:
            conn.execute(
                "DELETE FROM consumption_sync_outbox WHERE record_type=? AND record_key=?",
                (record_type, record_key),
            )
        conn.execute(
            "INSERT INTO consumption_sync_outbox(record_type,record_key,payload_json,created_at_utc) VALUES(?,?,?,?)",
            (record_type, record_key, payload_json, float(created_at or time.time())),
        )

    @staticmethod
    def _history_epoch_conn(conn: Any) -> int:
        row = conn.execute(
            "SELECT reset_epoch FROM consumption_history_state WHERE profile_id='default'"
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO consumption_history_state(profile_id,reset_epoch,reset_at_utc) VALUES('default',0,0)"
            )
            return 0
        return int(row["reset_epoch"] or 0)

    def history_epoch(self) -> int:
        with self.database.connect() as conn:
            return self._history_epoch_conn(conn)

    @staticmethod
    def _ensure_remote_device(conn: Any, device_id: str, device_name: str = "") -> None:
        now = time.time()
        row = conn.execute(
            "SELECT next_seq FROM consumption_devices WHERE device_id=?", (str(device_id),)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO consumption_devices(device_id,device_name,next_seq,created_at,updated_at) VALUES(?,?,1,?,?)",
                (str(device_id), str(device_name or ""), now, now),
            )
        elif device_name:
            conn.execute(
                "UPDATE consumption_devices SET device_name=?,updated_at=? WHERE device_id=?",
                (str(device_name), now, str(device_id)),
            )

    # ------------------------------------------------------------------
    # Stable media identities
    # ------------------------------------------------------------------
    def ensure_media(
        self,
        *,
        kind: str,
        title: str,
        aliases: Iterable[tuple[str, str]],
        current_library_kind: str = "",
        current_library_id: str = "",
        source_revision: str = "",
        metadata: dict[str, Any] | None = None,
        preferred_uuid: str = "",
    ) -> ConsumptionMediaRef:
        kind = str(kind or "").strip().casefold()
        if not kind:
            raise ConsumptionLedgerError("media kind is required")
        clean_aliases = []
        seen: set[tuple[str, str]] = set()
        for alias_type, alias_value in aliases:
            pair = (str(alias_type or "").strip().casefold(), str(alias_value or "").strip())
            if not pair[0] or not pair[1] or pair in seen:
                continue
            seen.add(pair)
            clean_aliases.append(pair)
        if not clean_aliases:
            raise ConsumptionLedgerError("at least one stable media alias is required")
        now = time.time()
        with self.database.connect() as conn:
            media_ids: set[str] = set()
            for alias_type, alias_value in clean_aliases:
                row = conn.execute(
                    "SELECT media_uuid FROM consumption_media_aliases "
                    "WHERE kind=? AND alias_type=? AND alias_value=?",
                    (kind, alias_type, alias_value),
                ).fetchone()
                if row is not None:
                    media_ids.add(str(row["media_uuid"]))
            if len(media_ids) > 1:
                raise ConsumptionConflictError(
                    f"stable aliases resolve to multiple media IDs: {sorted(media_ids)}"
                )
            media_uuid = next(iter(media_ids), "") or str(preferred_uuid or uuid.uuid4())
            row = conn.execute(
                "SELECT media_uuid FROM consumption_media WHERE media_uuid=?", (media_uuid,)
            ).fetchone()
            payload = _json(metadata or {})
            if row is None:
                conn.execute(
                    """
                    INSERT INTO consumption_media(
                        media_uuid,kind,title_snapshot,current_library_kind,current_library_id,
                        source_revision,metadata_json,deleted_at,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        media_uuid,
                        kind,
                        str(title or ""),
                        str(current_library_kind or ""),
                        str(current_library_id or ""),
                        str(source_revision or ""),
                        payload,
                        None,
                        now,
                        now,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE consumption_media SET
                        title_snapshot=CASE WHEN ?!='' THEN ? ELSE title_snapshot END,
                        current_library_kind=?,current_library_id=?,
                        source_revision=CASE WHEN ?!='' THEN ? ELSE source_revision END,
                        metadata_json=?,deleted_at=NULL,updated_at=?
                    WHERE media_uuid=?
                    """,
                    (
                        str(title or ""), str(title or ""),
                        str(current_library_kind or ""), str(current_library_id or ""),
                        str(source_revision or ""), str(source_revision or ""),
                        payload, now, media_uuid,
                    ),
                )
            for alias_type, alias_value in clean_aliases:
                conn.execute(
                    """
                    INSERT INTO consumption_media_aliases(kind,alias_type,alias_value,media_uuid,created_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(kind,alias_type,alias_value) DO UPDATE SET media_uuid=excluded.media_uuid
                    """,
                    (kind, alias_type, alias_value, media_uuid, now),
                )
                alias_key = _sha(_json([kind, alias_type, alias_value, media_uuid]))
                self._queue_sync_record(conn, "alias", alias_key, {
                    "kind": kind, "alias_type": alias_type, "alias_value": alias_value,
                    "media": {
                        "media_uuid": media_uuid, "kind": kind, "title": str(title or ""),
                        "source_revision": str(source_revision or ""),
                    },
                }, created_at=now)
        return ConsumptionMediaRef(media_uuid, kind, str(title or ""), str(source_revision or ""))

    def detach_library_media(self, *, kind: str, local_id: str) -> bool:
        """Detach a deleted library row without deleting its history."""
        now = time.time()
        with self.database.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE consumption_media SET current_library_kind='',current_library_id='',
                    deleted_at=COALESCE(deleted_at,?),updated_at=?
                WHERE current_library_kind=? AND current_library_id=?
                """,
                (now, now, str(kind or "").casefold(), str(local_id)),
            )
        return bool(cursor.rowcount)

    def anime_episode_media(self, video_path: Path) -> ConsumptionMediaRef | None:
        path = str(Path(video_path).expanduser().resolve())
        with self.database.connect() as conn:
            row = conn.execute(
                """
                SELECT e.media_id,COALESCE(e.media_episode,e.episode) AS episode,
                       e.title AS episode_title,a.title AS anime_title
                FROM episodes e LEFT JOIN anime a ON a.media_id=e.media_id
                WHERE e.video_path=? ORDER BY e.updated_at DESC LIMIT 1
                """,
                (path,),
            ).fetchone()
        if row is None or row["media_id"] is None or row["episode"] is None:
            return None
        media_id = int(row["media_id"])
        episode = int(row["episode"])
        title = f"{row['anime_title'] or media_id} · {episode}"
        return self.ensure_media(
            kind="anime_episode",
            title=title,
            aliases=[
                ("external", f"anilist:{media_id}:episode:{episode}"),
                ("video_path", path),
            ],
            current_library_kind="anime_episode",
            current_library_id=f"{media_id}:{episode}",
            metadata={
                "media_id": media_id,
                "episode": episode,
                "episode_title": str(row["episode_title"] or ""),
            },
        )

    def manga_media(self, book_id: int) -> ConsumptionMediaRef | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT id,path,title,source_fingerprint,anilist_id FROM manga_books WHERE id=?",
                (int(book_id),),
            ).fetchone()
        if row is None:
            return None
        legacy_fingerprint = str(row["source_fingerprint"] or "").strip()
        path = Path(str(row["path"] or "")).expanduser()
        content_fingerprint = _file_sample_fingerprint(path)
        aliases: list[tuple[str, str]] = [("local", str(int(book_id)))]
        if legacy_fingerprint:
            aliases.append(("legacy_fingerprint", legacy_fingerprint))
        if content_fingerprint:
            aliases.insert(0, ("content", content_fingerprint))
        else:
            aliases.insert(0, ("path", str(path)))
        return self.ensure_media(
            kind="manga",
            title=str(row["title"] or ""),
            aliases=aliases,
            current_library_kind="manga",
            current_library_id=str(int(book_id)),
            source_revision=content_fingerprint or legacy_fingerprint,
            metadata={"anilist_id": row["anilist_id"]},
        )

    def light_novel_media(self, book_id: int) -> ConsumptionMediaRef | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT id,title,volume,anilist_id,file_path FROM ln_books WHERE id=?", (int(book_id),)
            ).fetchone()
            if row is None:
                return None
            chapter_rows = conn.execute(
                "SELECT text_hash,text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index",
                (int(book_id),),
            ).fetchall()
            hashes = [str(item["text_hash"] or "") for item in chapter_rows]
            total_characters = sum(len(str(item["text"] or "")) for item in chapter_rows)
        revision = _sha("\n".join(hashes)) if hashes else ""
        aliases: list[tuple[str, str]] = [("local", str(int(book_id)))]
        if revision:
            aliases.insert(0, ("content", revision))
        if row["anilist_id"] is not None and row["volume"] is not None:
            aliases.insert(0, ("external", f"anilist:{int(row['anilist_id'])}:volume:{int(row['volume'])}"))
        return self.ensure_media(
            kind="light_novel",
            title=str(row["title"] or ""),
            aliases=aliases,
            current_library_kind="light_novel",
            current_library_id=str(int(book_id)),
            source_revision=revision,
            metadata={"anilist_id": row["anilist_id"], "volume": row["volume"], "character_count": total_characters},
        )

    def audiobook_media(self, book_id: int) -> ConsumptionMediaRef | None:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT id,path,title,duration FROM audiobooks WHERE id=?", (int(book_id),)
            ).fetchone()
            if row is None:
                return None
            files = conn.execute(
                "SELECT path,duration FROM audiobook_files WHERE book_id=? ORDER BY file_index",
                (int(book_id),),
            ).fetchall()
        source_parts: list[str] = []
        for item in files:
            file_path = Path(str(item["path"] or "")).expanduser()
            sampled = _file_sample_fingerprint(file_path, sample_bytes=16384)
            source_parts.append(
                f"{sampled or file_path.name}|{float(item['duration'] or 0):.3f}"
            )
        if source_parts:
            revision = _sha("\n".join(source_parts))
        else:
            root_path = Path(str(row["path"] or "")).expanduser()
            sampled = _file_sample_fingerprint(root_path, sample_bytes=16384) if root_path.is_file() else ""
            revision = _sha(f"{sampled or root_path.name}|{float(row['duration'] or 0):.3f}")
        return self.ensure_media(
            kind="audiobook",
            title=str(row["title"] or ""),
            aliases=[("content", revision), ("local", str(int(book_id)))],
            current_library_kind="audiobook",
            current_library_id=str(int(book_id)),
            source_revision=revision,
            metadata={"duration_seconds": max(0.0, float(row["duration"] or 0.0))},
        )

    # ------------------------------------------------------------------
    # Visual novel stable identity. Window IDs are capture-session locators.
    # ------------------------------------------------------------------
    def ensure_vn_title(self, title: str, executable_hint: str = "") -> ConsumptionMediaRef:
        title = " ".join(str(title or "Visual Novel").split()).strip() or "Visual Novel"
        hint = _norm(executable_hint)
        normalized = _norm(title)
        now = time.time()
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT id,title FROM vn_titles WHERE normalized_title=? AND executable_hint=?",
                (normalized, hint),
            ).fetchone()
            if row is None:
                vn_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO vn_titles(id,title,normalized_title,executable_hint,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (vn_id, title, normalized, hint, now, now),
                )
            else:
                vn_id = str(row["id"])
                if str(row["title"] or "") != title:
                    conn.execute(
                        "UPDATE vn_titles SET title=?,updated_at=? WHERE id=?", (title, now, vn_id)
                    )
        return self.ensure_media(
            kind="visual_novel",
            title=title,
            aliases=[("vn_title", vn_id), ("title_executable", f"{normalized}|{hint}")],
            current_library_kind="visual_novel",
            current_library_id=vn_id,
            preferred_uuid=vn_id,
            metadata={"executable_hint": hint},
        )

    def begin_vn_capture(
        self,
        *,
        title: str,
        executable_hint: str,
        window_id: int,
        generation: int,
    ) -> dict[str, Any]:
        media = self.ensure_vn_title(title, executable_hint)
        session_id = self.start_session(
            kind="visual_novel",
            media_uuid=media.media_uuid,
            origin="automatic",
            measurement_method="vn_capture_session",
            metadata={"window_id": int(window_id), "generation": int(generation)},
        )
        now = time.time()
        try:
            with self.database.connect() as conn:
                conn.execute(
                    "INSERT INTO vn_capture_sessions(session_id,vn_title_id,window_id,generation,started_at,ended_at) "
                    "VALUES(?,?,?,?,?,NULL)",
                    (session_id, media.media_uuid, int(window_id), int(generation), now),
                )
        except Exception:
            # Do not leave an orphan active consumption session if the capture
            # locator row cannot be persisted. The stable VN identity remains
            # valid and can be reused by a later successful capture.
            self.end_session(session_id, ended_at_utc=now)
            raise
        return {"session_id": session_id, "vn_title_id": media.media_uuid, "media_uuid": media.media_uuid}

    def end_vn_capture(self, session_id: str) -> None:
        now = time.time()
        self.end_session(session_id, ended_at_utc=now)
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE vn_capture_sessions SET ended_at=COALESCE(ended_at,?) WHERE session_id=?",
                (now, str(session_id)),
            )

    # ------------------------------------------------------------------
    # Sessions / idempotent bounded events
    # ------------------------------------------------------------------
    def start_session(
        self,
        *,
        kind: str,
        media_uuid: str | None = None,
        activity_group_id: str = "",
        origin: str = "automatic",
        measurement_method: str = "observation",
        started_at_utc: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        started = float(started_at_utc if started_at_utc is not None else time.time())
        if not math.isfinite(started) or started < 0:
            raise ConsumptionLedgerError("invalid session start")
        session_id = str(uuid.uuid4())
        group_id = str(activity_group_id or uuid.uuid4())
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO consumption_sessions(
                    session_id,profile_id,device_id,activity_group_id,kind,primary_media_uuid,started_at_utc,
                    ended_at_utc,origin,status,measurement_method,policy_version,schema_version,metadata_json
                ) VALUES(?,?,?,?,?,?,?,NULL,?,'active',?,'r7-v1',?,?)
                """,
                (
                    session_id,
                    "default",
                    self._device_id(conn),
                    group_id,
                    str(kind or "unknown"),
                    str(media_uuid) if media_uuid else None,
                    started,
                    str(origin or "automatic"),
                    str(measurement_method or "observation"),
                    LEDGER_SCHEMA_VERSION,
                    _json(metadata or {}),
                ),
            )
        return session_id

    def end_session(self, session_id: str, *, ended_at_utc: float | None = None) -> bool:
        ended = float(ended_at_utc if ended_at_utc is not None else time.time())
        with self.database.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE consumption_sessions SET ended_at_utc=?,status='closed'
                WHERE session_id=? AND status='active'
                """,
                (ended, str(session_id)),
            )
        return cursor.rowcount == 1

    def _device_id(self, conn: Any) -> str:
        row = conn.execute("SELECT value FROM state WHERE key='consumption_device_id'").fetchone()
        device_id = str(row["value"] or "").strip() if row is not None else ""
        if not device_id:
            device_id = str(uuid.uuid4())
            now = time.time()
            conn.execute(
                "INSERT INTO state(key,value,updated_at) VALUES('consumption_device_id',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (device_id, now),
            )
        device_name = str(platform.node() or "").strip()
        now = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO consumption_devices(device_id,device_name,next_seq,created_at,updated_at) VALUES(?,?,1,?,?)",
            (device_id, device_name, now, now),
        )
        if device_name:
            conn.execute(
                "UPDATE consumption_devices SET device_name=?,updated_at=? WHERE device_id=? AND device_name!=?",
                (device_name, now, device_id, device_name),
            )
        return device_id

    @staticmethod
    def _next_seq(conn: Any, device_id: str) -> int:
        row = conn.execute(
            "SELECT next_seq FROM consumption_devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if row is None:
            raise ConsumptionLedgerError("consumption device cursor is missing")
        seq = int(row["next_seq"])
        conn.execute(
            "UPDATE consumption_devices SET next_seq=?,updated_at=? WHERE device_id=?",
            (seq + 1, time.time(), device_id),
        )
        return seq

    def append_interval(
        self,
        *,
        session_id: str,
        kind: str,
        interval_start_utc: float,
        interval_end_utc: float,
        elapsed_monotonic_ms: float,
        media: Iterable[dict[str, Any]],
        payload: dict[str, Any] | None = None,
        timezone_iana: str | None = None,
        utc_offset: int | None = None,
        event_id: str = "",
        device_seq: int | None = None,
    ) -> ConsumptionAppendResult:
        start = float(interval_start_utc)
        end = float(interval_end_utc)
        elapsed = float(elapsed_monotonic_ms)
        values = (start, end, elapsed)
        if not all(math.isfinite(value) for value in values) or start < 0 or end < start or elapsed < 0:
            raise ConsumptionLedgerError("invalid consumption interval")
        duration = end - start
        if duration > MAX_CHUNK_SECONDS + 0.001 or elapsed > (MAX_CHUNK_SECONDS + 0.001) * 1000:
            raise ConsumptionLedgerError("consumption chunk exceeds bounded duration")
        payload_json = _json(payload or {})
        if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ConsumptionLedgerError("consumption payload is too large")
        media_rows = []
        for item in media:
            media_uuid = str(item.get("media_uuid") or "").strip()
            if not media_uuid:
                continue
            media_rows.append(
                {
                    "media_uuid": media_uuid,
                    "source_revision": str(item.get("source_revision") or ""),
                    "role": str(item.get("role") or "primary"),
                    "locator_start": item.get("locator_start") or {},
                    "locator_end": item.get("locator_end") or {},
                }
            )
        if not media_rows:
            raise ConsumptionLedgerError("consumption event requires at least one media link")
        media_rows.sort(key=lambda row: (row["media_uuid"], row["role"]))
        automatic_zone, automatic_offset = _system_timezone_metadata(end)
        resolved_timezone = automatic_zone if timezone_iana in (None, "") else str(timezone_iana)
        resolved_offset = automatic_offset if utc_offset is None else int(utc_offset)
        content_hash = _sha(
            _json(
                {
                    "session_id": str(session_id),
                    "kind": str(kind),
                    "start": round(start, 6),
                    "end": round(end, 6),
                    "elapsed": round(elapsed, 3),
                    "payload": payload or {},
                    "timezone": resolved_timezone,
                    "utc_offset": resolved_offset,
                    "media": media_rows,
                }
            )
        )
        identifier = str(event_id or uuid.uuid4())
        received = time.time()
        with self.database.connect() as conn:
            existing = conn.execute(
                "SELECT event_id,device_id,device_seq,content_hash FROM consumption_events WHERE event_id=?",
                (identifier,),
            ).fetchone()
            if existing is not None:
                if str(existing["content_hash"]) != content_hash:
                    raise ConsumptionConflictError("event_id was retried with different content")
                return ConsumptionAppendResult(
                    identifier, str(existing["device_id"]), int(existing["device_seq"]), True
                )
            session_row = conn.execute(
                "SELECT * FROM consumption_sessions WHERE session_id=?", (str(session_id),)
            ).fetchone()
            if session_row is None:
                raise ConsumptionLedgerError("consumption session does not exist")
            device_id = self._device_id(conn)
            seq = int(device_seq) if device_seq is not None else self._next_seq(conn, device_id)
            seq_existing = conn.execute(
                "SELECT event_id,content_hash FROM consumption_events WHERE device_id=? AND device_seq=?",
                (device_id, seq),
            ).fetchone()
            if seq_existing is not None:
                if str(seq_existing["content_hash"]) == content_hash:
                    return ConsumptionAppendResult(
                        str(seq_existing["event_id"]), device_id, seq, True
                    )
                raise ConsumptionConflictError("device sequence was reused with different content")
            conn.execute(
                """
                INSERT INTO consumption_events(
                    event_id,device_id,device_seq,session_id,kind,interval_start_utc,
                    interval_end_utc,elapsed_monotonic_ms,received_at_utc,timezone_iana,
                    utc_offset,payload_json,content_hash,schema_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    identifier, device_id, seq, str(session_id), str(kind), start, end,
                    elapsed, received, resolved_timezone, resolved_offset,
                    payload_json, content_hash, LEDGER_SCHEMA_VERSION,
                ),
            )
            sync_media: list[dict[str, Any]] = []
            for row in media_rows:
                conn.execute(
                    """
                    INSERT INTO consumption_event_media(
                        event_id,media_uuid,source_revision,role,locator_start_json,locator_end_json
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        identifier, row["media_uuid"], row["source_revision"], row["role"],
                        _json(row["locator_start"]), _json(row["locator_end"]),
                    ),
                )
                media_info = conn.execute(
                    "SELECT kind,title_snapshot,source_revision FROM consumption_media WHERE media_uuid=?",
                    (row["media_uuid"],),
                ).fetchone()
                sync_media.append({
                    "media_uuid": row["media_uuid"],
                    "kind": str(media_info["kind"] if media_info is not None else kind),
                    "title": str(media_info["title_snapshot"] if media_info is not None else ""),
                    "source_revision": row["source_revision"] or str(media_info["source_revision"] if media_info is not None else ""),
                    "role": row["role"],
                    "locator_start": row["locator_start"],
                    "locator_end": row["locator_end"],
                })
            self._queue_sync_record(conn, "event", identifier, {
                "event_id": identifier, "device_id": device_id, "device_seq": seq,
                "session": {
                    "session_id": str(session_id),
                    "kind": str(session_row["kind"] or kind),
                    "primary_media_uuid": str(session_row["primary_media_uuid"] or ""),
                    "activity_group_id": str(session_row["activity_group_id"] or ""),
                    "started_at_utc": float(session_row["started_at_utc"]),
                    "ended_at_utc": session_row["ended_at_utc"],
                    "origin": str(session_row["origin"] or "automatic"),
                    "measurement_method": str(session_row["measurement_method"] or "observation"),
                    "policy_version": str(session_row["policy_version"] or "r7-v1"),
                },
                "kind": str(kind), "interval_start_utc": start, "interval_end_utc": end,
                "elapsed_monotonic_ms": elapsed, "timezone_iana": resolved_timezone,
                "utc_offset": resolved_offset, "payload": payload or {}, "media": sync_media,
            }, created_at=received)
        return ConsumptionAppendResult(identifier, device_id, seq, False)

    def _active_session_for_media(
        self,
        media_uuid: str,
        kind: str,
        *,
        now: float,
        measurement_method: str = "bounded_active_chunks",
        activity_group_id: str = "",
    ) -> str:
        requested_group = str(activity_group_id or "")
        with self.database.connect() as conn:
            params: list[Any] = [str(kind), str(media_uuid)]
            group_clause = ""
            if requested_group:
                group_clause = " AND s.activity_group_id=?"
                params.append(requested_group)
            rows = conn.execute(
                f"""
                SELECT s.session_id,s.activity_group_id,COALESCE(MAX(e.interval_end_utc),s.started_at_utc) AS last_at
                FROM consumption_sessions s
                LEFT JOIN consumption_events e ON e.session_id=s.session_id
                WHERE s.kind=? AND s.status='active' AND s.primary_media_uuid=?{group_clause}
                GROUP BY s.session_id ORDER BY last_at DESC LIMIT 1
                """,
                tuple(params),
            ).fetchone()
            stale_other = None
            if requested_group and rows is None:
                stale_other = conn.execute(
                    """
                    SELECT s.session_id,COALESCE(MAX(e.interval_end_utc),s.started_at_utc) AS last_at
                    FROM consumption_sessions s
                    LEFT JOIN consumption_events e ON e.session_id=s.session_id
                    WHERE s.kind=? AND s.status='active' AND s.primary_media_uuid=?
                    GROUP BY s.session_id ORDER BY last_at DESC LIMIT 1
                    """,
                    (str(kind), str(media_uuid)),
                ).fetchone()
        if rows is not None and now - float(rows["last_at"] or 0.0) <= 90.0:
            return str(rows["session_id"])
        if rows is not None:
            self.end_session(str(rows["session_id"]), ended_at_utc=float(rows["last_at"] or now))
        elif stale_other is not None:
            self.end_session(
                str(stale_other["session_id"]), ended_at_utc=float(stale_other["last_at"] or now)
            )
        return self.start_session(
            kind=kind,
            media_uuid=media_uuid,
            measurement_method=str(measurement_method or "bounded_active_chunks"),
            activity_group_id=requested_group,
        )

    @staticmethod
    def _ln_audio_group(ln_book_id: int, audiobook_id: int) -> str:
        if int(ln_book_id or 0) <= 0 or int(audiobook_id or 0) <= 0:
            return ""
        return f"ln-audio:{int(ln_book_id)}:{int(audiobook_id)}"


    def record_anime_playback(
        self,
        video_path: Path,
        *,
        position: float,
        duration: float,
        active_seconds: float,
    ) -> list[ConsumptionAppendResult]:
        active = max(0.0, float(active_seconds or 0.0))
        if active <= 0.001:
            return []
        media_ref = self.anime_episode_media(video_path)
        if media_ref is None:
            return []
        now = time.time()
        session_id = self._active_session_for_media(media_ref.media_uuid, "anime", now=now)
        with self.database.connect() as conn:
            previous = conn.execute(
                "SELECT playback_position FROM episodes WHERE video_path=? ORDER BY updated_at DESC LIMIT 1",
                (str(Path(video_path).expanduser().resolve()),),
            ).fetchone()
        previous_position = float(previous["playback_position"] or position) if previous is not None else float(position)
        total = active
        results: list[ConsumptionAppendResult] = []
        chunks = max(1, int(math.ceil(total / 15.0)))
        for index in range(chunks):
            chunk = min(15.0, total - (15.0 * index))
            if chunk <= 0:
                break
            chunk_end = now - max(0.0, total - 15.0 * (index + 1))
            chunk_start = chunk_end - chunk
            ratio0 = index / chunks
            ratio1 = (index + 1) / chunks
            locator0 = previous_position + (float(position) - previous_position) * ratio0
            locator1 = previous_position + (float(position) - previous_position) * ratio1
            results.append(
                self.append_interval(
                    session_id=session_id,
                    kind="anime",
                    interval_start_utc=chunk_start,
                    interval_end_utc=chunk_end,
                    elapsed_monotonic_ms=chunk * 1000.0,
                    media=[{
                        "media_uuid": media_ref.media_uuid,
                        "source_revision": media_ref.source_revision,
                        "role": "primary",
                        "locator_start": {"position_seconds": max(0.0, locator0)},
                        "locator_end": {"position_seconds": max(0.0, locator1)},
                    }],
                    payload={
                        "duration_seconds": max(0.0, float(duration or 0.0)),
                        "measurement": "mpv_active_seconds",
                        "seek_or_discontinuity": abs(float(position) - previous_position) > active * 4 + 10,
                    },
                )
            )
        if self.logger:
            self.logger.debug(
                "EVENT consumption.anime_chunk media=%s chunks=%s active_s=%.3f",
                media_ref.media_uuid, len(results), active,
            )
        return results

    def _media_for_local(self, kind: str, local_id: int) -> ConsumptionMediaRef | None:
        normalized = str(kind or "").strip().casefold()
        if normalized in {"light_novel", "novel", "ln"}:
            return self.light_novel_media(int(local_id))
        if normalized == "manga":
            return self.manga_media(int(local_id))
        if normalized == "audiobook":
            return self.audiobook_media(int(local_id))
        return None

    def record_reader_activity(
        self,
        *,
        kind: str,
        local_id: int,
        active_seconds: float,
        locator_start: dict[str, Any] | None = None,
        locator_end: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        activity_group_id: str = "",
        measurement_method: str = "reader_visible_heartbeat",
    ) -> list[ConsumptionAppendResult]:
        active = max(0.0, float(active_seconds or 0.0))
        if active <= 0.001:
            return []
        media_ref = self._media_for_local(kind, int(local_id))
        if media_ref is None:
            return []
        normalized_kind = "light_novel" if str(kind).casefold() in {"light_novel", "novel", "ln"} else str(kind).casefold()
        resolved_group = str(activity_group_id or "")
        if not resolved_group and normalized_kind == "light_novel":
            paired_audio_id = int((payload or {}).get("paired_audio_id") or 0)
            resolved_group = self._ln_audio_group(int(local_id), paired_audio_id)
        now = time.time()
        session_id = self._active_session_for_media(
            media_ref.media_uuid, normalized_kind, now=now,
            measurement_method=measurement_method, activity_group_id=resolved_group,
        )
        results: list[ConsumptionAppendResult] = []
        remaining = active
        end = now
        while remaining > 0.001:
            chunk = min(15.0, remaining)
            start = end - chunk
            results.append(self.append_interval(
                session_id=session_id, kind=normalized_kind,
                interval_start_utc=start, interval_end_utc=end, elapsed_monotonic_ms=chunk * 1000.0,
                media=[{
                    "media_uuid": media_ref.media_uuid,
                    "source_revision": media_ref.source_revision,
                    "role": "primary",
                    "locator_start": locator_start or {},
                    "locator_end": locator_end or {},
                }],
                payload={"measurement": measurement_method, **(payload or {})},
            ))
            end = start
            remaining -= chunk
        return list(reversed(results))

    def record_audiobook_activity(
        self,
        *,
        book_id: int,
        active_seconds: float,
        position_start: float,
        position_end: float,
        speed: float = 1.0,
    ) -> list[ConsumptionAppendResult]:
        linked_ln_id = 0
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT ln_book_id FROM reading_audio_links WHERE audiobook_id=? "
                "ORDER BY updated_at DESC,ln_book_id LIMIT 1",
                (int(book_id),),
            ).fetchone()
            if row is not None:
                linked_ln_id = int(row["ln_book_id"] or 0)
        return self.record_reader_activity(
            kind="audiobook", local_id=int(book_id), active_seconds=active_seconds,
            locator_start={"position_seconds": max(0.0, float(position_start or 0.0))},
            locator_end={"position_seconds": max(0.0, float(position_end or 0.0))},
            payload={
                "speed": max(0.1, float(speed or 1.0)),
                "seek_or_discontinuity": abs(float(position_end) - float(position_start)) > max(10.0, active_seconds * max(1.0, float(speed or 1.0)) * 4.0),
            },
            activity_group_id=self._ln_audio_group(linked_ln_id, int(book_id)),
            measurement_method="audiobook_active_playback",
        )

    def record_vn_runtime(
        self,
        session_id: str,
        *,
        active_seconds: float,
        capture_count: int = 0,
    ) -> list[ConsumptionAppendResult]:
        active = max(0.0, float(active_seconds or 0.0))
        if active <= 0.001 or not str(session_id or ""):
            return []
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT primary_media_uuid FROM consumption_sessions WHERE session_id=? AND status='active'",
                (str(session_id),),
            ).fetchone()
            if row is None or not row["primary_media_uuid"]:
                return []
            media_uuid = str(row["primary_media_uuid"])
            media = conn.execute(
                "SELECT source_revision FROM consumption_media WHERE media_uuid=?", (media_uuid,)
            ).fetchone()
        now = time.time()
        results: list[ConsumptionAppendResult] = []
        remaining = active
        end = now
        while remaining > 0.001:
            chunk = min(15.0, remaining)
            start = end - chunk
            results.append(self.append_interval(
                session_id=str(session_id), kind="visual_novel",
                interval_start_utc=start, interval_end_utc=end, elapsed_monotonic_ms=chunk * 1000.0,
                media=[{
                    "media_uuid": media_uuid,
                    "source_revision": str(media["source_revision"] if media is not None else ""),
                    "role": "primary",
                }],
                payload={
                    "measurement": "capture_runtime_estimated",
                    "capture_count": max(0, int(capture_count or 0)),
                    "focus_verified": False,
                },
            ))
            end = start
            remaining -= chunk
        return list(reversed(results))

    def _seed_sync_outbox(self) -> None:
        """Seed R9 transport from retained pre-R9 history once, without changing facts."""
        with self.database.connect() as conn:
            done = conn.execute("SELECT value FROM state WHERE key='consumption_sync_seeded_v2'").fetchone()
            if done is not None and str(done["value"] or "") == "1":
                return
            aliases = conn.execute(
                """SELECT a.kind,a.alias_type,a.alias_value,a.media_uuid,m.title_snapshot,m.source_revision
                   FROM consumption_media_aliases a JOIN consumption_media m ON m.media_uuid=a.media_uuid"""
            ).fetchall()
            for row in aliases:
                key = _sha(_json([row["kind"],row["alias_type"],row["alias_value"],row["media_uuid"]]))
                self._queue_sync_record(conn,"alias",key,{
                    "kind":str(row["kind"]),"alias_type":str(row["alias_type"]),"alias_value":str(row["alias_value"]),
                    "media":{"media_uuid":str(row["media_uuid"]),"kind":str(row["kind"]),"title":str(row["title_snapshot"] or ""),"source_revision":str(row["source_revision"] or "")},
                })
            # Existing events are materialized with the same wire shape used by new appends.
            event_ids = [str(row["event_id"]) for row in conn.execute("SELECT event_id FROM consumption_events ORDER BY received_at_utc,event_id")]
            for event_id in event_ids:
                payload = self._sync_event_payload_conn(conn,event_id)
                if payload is not None:
                    self._queue_sync_record(conn,"event",event_id,payload,created_at=float(payload.get("received_at_utc") or time.time()))
            for row in conn.execute("SELECT * FROM consumption_corrections ORDER BY created_at_utc,correction_id"):
                payload = {key: row[key] for key in row.keys()}
                self._queue_sync_record(conn,"correction",str(row["correction_id"]),payload,created_at=float(row["created_at_utc"]))
            for row in conn.execute("SELECT * FROM consumption_manual_entries ORDER BY created_at_utc,manual_id"):
                payload = {key: row[key] for key in row.keys()}
                self._queue_sync_record(conn,"manual",str(row["manual_id"]),payload,created_at=float(row["created_at_utc"]))
            conn.execute(
                "INSERT INTO state(key,value,updated_at) VALUES('consumption_sync_seeded_v2','1',?) "
                "ON CONFLICT(key) DO UPDATE SET value='1',updated_at=excluded.updated_at",
                (time.time(),),
            )

    def _sync_event_payload_conn(self, conn: Any, event_id: str) -> dict[str, Any] | None:
        row = conn.execute(
            """SELECT e.*,s.activity_group_id,s.primary_media_uuid,s.started_at_utc AS session_started_at,
                      s.ended_at_utc AS session_ended_at,s.origin,s.measurement_method,s.policy_version
               FROM consumption_events e JOIN consumption_sessions s ON s.session_id=e.session_id
               WHERE e.event_id=?""",
            (str(event_id),),
        ).fetchone()
        if row is None:
            return None
        media=[]
        for item in conn.execute(
            """SELECT em.*,m.kind AS media_kind,m.title_snapshot,m.source_revision AS media_revision
               FROM consumption_event_media em JOIN consumption_media m ON m.media_uuid=em.media_uuid
               WHERE em.event_id=? ORDER BY CASE em.role WHEN 'primary' THEN 0 ELSE 1 END,em.media_uuid""",
            (str(event_id),),
        ).fetchall():
            media.append({
                "media_uuid":str(item["media_uuid"]),"kind":str(item["media_kind"] or ""),
                "title":str(item["title_snapshot"] or ""),
                "source_revision":str(item["source_revision"] or item["media_revision"] or ""),
                "role":str(item["role"] or "primary"),
                "locator_start":json.loads(str(item["locator_start_json"] or "{}")),
                "locator_end":json.loads(str(item["locator_end_json"] or "{}")),
            })
        return {
            "event_id":str(row["event_id"]),"device_id":str(row["device_id"]),"device_seq":int(row["device_seq"]),
            "received_at_utc":float(row["received_at_utc"]),
            "session":{"session_id":str(row["session_id"]),"kind":str(row["kind"]),
                       "primary_media_uuid":str(row["primary_media_uuid"] or ""),
                       "activity_group_id":str(row["activity_group_id"] or ""),
                       "started_at_utc":float(row["session_started_at"]),"ended_at_utc":row["session_ended_at"],
                       "origin":str(row["origin"] or "automatic"),"measurement_method":str(row["measurement_method"] or "observation"),
                       "policy_version":str(row["policy_version"] or "r7-v1")},
            "kind":str(row["kind"]),"interval_start_utc":float(row["interval_start_utc"]),
            "interval_end_utc":float(row["interval_end_utc"]),"elapsed_monotonic_ms":float(row["elapsed_monotonic_ms"]),
            "timezone_iana":str(row["timezone_iana"] or ""),"utc_offset":int(row["utc_offset"] or 0),
            "payload":json.loads(str(row["payload_json"] or "{}")),"media":media,
        }

    def sync_changes(self, *, cursor: int = 0, limit: int = 200) -> dict[str, Any]:
        self._seed_sync_outbox()
        start=max(0,int(cursor)); page=max(1,min(2000,int(limit)))
        with self.database.connect() as conn:
            epoch=self._history_epoch_conn(conn)
            earliest=conn.execute("SELECT COALESCE(MIN(id),0) AS id FROM consumption_sync_outbox").fetchone()
            rows=conn.execute(
                "SELECT id,record_type,record_key,payload_json,created_at_utc FROM consumption_sync_outbox WHERE id>? ORDER BY id LIMIT ?",
                (start,page+1),
            ).fetchall()
        visible=rows[:page]
        earliest_id=int(earliest["id"] if earliest else 0)
        return {
            "schema":2,"reset_epoch":epoch,"cursor":int(visible[-1]["id"]) if visible else start,
            "has_more":len(rows)>page,"earliest_cursor":earliest_id,
            "reset_required":bool(start>0 and earliest_id>0 and start<earliest_id-1),
            "records":[{"cursor":int(row["id"]),"record_id":f"{row['record_type']}:{row['record_key']}",
                         "type":str(row["record_type"]),"payload":json.loads(str(row["payload_json"])),
                         "created_at_utc":float(row["created_at_utc"])} for row in visible],
        }

    def _ensure_wire_media_conn(self, conn: Any, item: dict[str, Any]) -> str:
        media_uuid=str(item.get("media_uuid") or "").strip()
        if not media_uuid:
            raise ConsumptionLedgerError("consumption sync media_uuid is required")
        kind=str(item.get("kind") or "unknown").strip().casefold() or "unknown"
        title=str(item.get("title") or "")
        revision=str(item.get("source_revision") or "")
        now=time.time()
        row=conn.execute("SELECT 1 FROM consumption_media WHERE media_uuid=?",(media_uuid,)).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO consumption_media(media_uuid,kind,title_snapshot,current_library_kind,current_library_id,
                   source_revision,metadata_json,deleted_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (media_uuid,kind,title,"","",revision,"{}",None,now,now),
            )
        else:
            conn.execute(
                "UPDATE consumption_media SET title_snapshot=CASE WHEN ?!='' THEN ? ELSE title_snapshot END,"
                "source_revision=CASE WHEN ?!='' THEN ? ELSE source_revision END,updated_at=? WHERE media_uuid=?",
                (title,title,revision,revision,now,media_uuid),
            )
        return media_uuid

    def _apply_sync_record_conn(self, conn: Any, device_id: str, record_type: str, payload: dict[str, Any], *, device_name: str = "") -> str:
        kind=str(record_type or "").strip().casefold()
        if kind=="alias":
            media=payload.get("media") if isinstance(payload.get("media"),dict) else {}
            media_uuid=self._ensure_wire_media_conn(conn,media)
            alias_kind=str(payload.get("kind") or media.get("kind") or "unknown").casefold()
            alias_type=str(payload.get("alias_type") or "").casefold(); alias_value=str(payload.get("alias_value") or "")
            if not alias_type or not alias_value: raise ConsumptionLedgerError("invalid synced alias")
            conn.execute(
                """INSERT INTO consumption_media_aliases(kind,alias_type,alias_value,media_uuid,created_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(kind,alias_type,alias_value) DO UPDATE SET media_uuid=excluded.media_uuid""",
                (alias_kind,alias_type,alias_value,media_uuid,time.time()),
            )
            return "applied"
        if kind=="correction":
            correction_id=str(payload.get("correction_id") or "").strip()
            if not correction_id: raise ConsumptionLedgerError("invalid synced correction")
            existing=conn.execute("SELECT * FROM consumption_corrections WHERE correction_id=?",(correction_id,)).fetchone()
            if existing is not None:
                return "duplicate"
            conn.execute(
                """INSERT INTO consumption_corrections(correction_id,target_type,target_id,revision,parent_revision,excluded,
                   replacement_start_utc,replacement_end_utc,replacement_duration_seconds,reason,device_id,created_at_utc)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (correction_id,str(payload.get("target_type") or ""),str(payload.get("target_id") or ""),int(payload.get("revision") or 0),
                 int(payload.get("parent_revision") or 0),int(bool(payload.get("excluded"))),payload.get("replacement_start_utc"),
                 payload.get("replacement_end_utc"),payload.get("replacement_duration_seconds"),str(payload.get("reason") or ""),
                 str(payload.get("device_id") or device_id),float(payload.get("created_at_utc") or time.time())),
            )
            return "applied"
        if kind=="manual":
            manual_id=str(payload.get("manual_id") or "").strip()
            if not manual_id: raise ConsumptionLedgerError("invalid synced manual entry")
            media_uuid=str(payload.get("media_uuid") or "")
            if media_uuid and conn.execute("SELECT 1 FROM consumption_media WHERE media_uuid=?",(media_uuid,)).fetchone() is None:
                self._ensure_wire_media_conn(conn,{"media_uuid":media_uuid,"kind":str(payload.get("kind") or "unknown"),"title":str(payload.get("title_snapshot") or "")})
            existing=conn.execute("SELECT 1 FROM consumption_manual_entries WHERE manual_id=?",(manual_id,)).fetchone()
            if existing is not None: return "duplicate"
            conn.execute(
                """INSERT INTO consumption_manual_entries(manual_id,media_uuid,kind,title_snapshot,started_at_utc,duration_seconds,
                   note,origin,created_at_utc,updated_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (manual_id,media_uuid or None,str(payload.get("kind") or "manual"),str(payload.get("title_snapshot") or ""),
                 payload.get("started_at_utc"),float(payload.get("duration_seconds") or 0.0),str(payload.get("note") or ""),
                 str(payload.get("origin") or "manual"),float(payload.get("created_at_utc") or time.time()),float(payload.get("updated_at_utc") or time.time())),
            )
            return "applied"
        if kind!="event":
            raise ConsumptionLedgerError(f"unsupported consumption sync record: {kind}")
        event_id=str(payload.get("event_id") or "").strip()
        session=payload.get("session") if isinstance(payload.get("session"),dict) else {}
        session_id=str(session.get("session_id") or "").strip()
        seq=int(payload.get("device_seq") or 0)
        if not event_id or not session_id or seq<=0: raise ConsumptionLedgerError("invalid synced consumption event")
        start=float(payload.get("interval_start_utc") or 0.0); end=float(payload.get("interval_end_utc") or 0.0); elapsed=float(payload.get("elapsed_monotonic_ms") or 0.0)
        if not all(math.isfinite(v) for v in (start,end,elapsed)) or start<0 or end<start or end-start>MAX_CHUNK_SECONDS+0.001 or elapsed>(MAX_CHUNK_SECONDS+0.001)*1000:
            raise ConsumptionLedgerError("invalid synced consumption interval")
        media_items=payload.get("media") if isinstance(payload.get("media"),list) else []
        if not media_items: raise ConsumptionLedgerError("synced consumption event has no media")
        primary=""
        for item in media_items:
            if not isinstance(item,dict): continue
            uid=self._ensure_wire_media_conn(conn,item)
            if not primary or str(item.get("role") or "primary")=="primary": primary=uid
        self._ensure_remote_device(conn,device_id,device_name)
        existing=conn.execute("SELECT content_hash FROM consumption_events WHERE event_id=?",(event_id,)).fetchone()
        event_payload=payload.get("payload") if isinstance(payload.get("payload"),dict) else {}
        media_hash_rows=[]
        for item in media_items:
            if not isinstance(item,dict): continue
            media_hash_rows.append({"media_uuid":str(item.get("media_uuid") or ""),"source_revision":str(item.get("source_revision") or ""),"role":str(item.get("role") or "primary"),"locator_start":item.get("locator_start") or {},"locator_end":item.get("locator_end") or {}})
        media_hash_rows.sort(key=lambda row:(row["media_uuid"],row["role"]))
        zone=str(payload.get("timezone_iana") or ""); offset=int(payload.get("utc_offset") or 0)
        content_hash=_sha(_json({"session_id":session_id,"kind":str(payload.get("kind") or session.get("kind") or "unknown"),"start":round(start,6),"end":round(end,6),"elapsed":round(elapsed,3),"payload":event_payload,"timezone":zone,"utc_offset":offset,"media":media_hash_rows}))
        if existing is not None:
            if str(existing["content_hash"])!=content_hash: raise ConsumptionConflictError("synced event_id content conflict")
            return "duplicate"
        seq_existing=conn.execute("SELECT event_id,content_hash FROM consumption_events WHERE device_id=? AND device_seq=?",(device_id,seq)).fetchone()
        if seq_existing is not None:
            if str(seq_existing["content_hash"])==content_hash: return "duplicate"
            raise ConsumptionConflictError("synced device sequence conflict")
        if conn.execute("SELECT 1 FROM consumption_sessions WHERE session_id=?",(session_id,)).fetchone() is None:
            conn.execute(
                """INSERT INTO consumption_sessions(session_id,profile_id,device_id,activity_group_id,kind,primary_media_uuid,started_at_utc,
                   ended_at_utc,origin,status,measurement_method,policy_version,schema_version,metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id,"default",device_id,str(session.get("activity_group_id") or ""),str(session.get("kind") or payload.get("kind") or "unknown"),
                 str(session.get("primary_media_uuid") or primary) or None,float(session.get("started_at_utc") or start),session.get("ended_at_utc"),
                 str(session.get("origin") or "companion"),"closed" if session.get("ended_at_utc") is not None else "active",
                 str(session.get("measurement_method") or "companion_observation"),str(session.get("policy_version") or "r9-sync-v1"),LEDGER_SCHEMA_VERSION,"{}"),
            )
        received=time.time()
        conn.execute(
            """INSERT INTO consumption_events(event_id,device_id,device_seq,session_id,kind,interval_start_utc,interval_end_utc,
               elapsed_monotonic_ms,received_at_utc,timezone_iana,utc_offset,payload_json,content_hash,schema_version)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_id,device_id,seq,session_id,str(payload.get("kind") or session.get("kind") or "unknown"),start,end,elapsed,received,zone,offset,_json(event_payload),content_hash,LEDGER_SCHEMA_VERSION),
        )
        for item in media_items:
            if not isinstance(item,dict): continue
            conn.execute(
                """INSERT INTO consumption_event_media(event_id,media_uuid,source_revision,role,locator_start_json,locator_end_json)
                   VALUES(?,?,?,?,?,?)""",
                (event_id,str(item.get("media_uuid") or ""),str(item.get("source_revision") or ""),str(item.get("role") or "primary"),_json(item.get("locator_start") or {}),_json(item.get("locator_end") or {})),
            )
        conn.execute("UPDATE consumption_devices SET next_seq=MAX(next_seq,?),updated_at=? WHERE device_id=?",(seq+1,received,device_id))
        return "applied"

    def push_sync_records(self, device_id: str, records: Iterable[dict[str, Any]], *, reset_epoch: int, device_name: str = "") -> dict[str, Any]:
        rows=list(records)
        if len(rows)>2000: raise ConsumptionLedgerError("too many consumption sync records")
        with self.database.connect() as conn:
            current_epoch=self._history_epoch_conn(conn)
            # A newer epoch is accepted only as an explicit reset record. An older
            # offline device must rebase before any event can resurrect history.
            if int(reset_epoch)<current_epoch:
                return {"schema":2,"reset_epoch":current_epoch,"reset_required":True,"results":[]}
            if int(reset_epoch)>current_epoch:
                reset_rows=[row for row in rows if isinstance(row,dict) and str(row.get("type") or "").casefold()=="reset"]
                if int(reset_epoch)!=current_epoch+1 or len(reset_rows)!=1 or len(rows)!=1:
                    raise ConsumptionConflictError("consumption reset epoch conflict")
                reset_payload=reset_rows[0].get("payload") if isinstance(reset_rows[0].get("payload"),dict) else {}
                if int(reset_payload.get("reset_epoch") or reset_epoch)!=int(reset_epoch):
                    raise ConsumptionConflictError("consumption reset payload epoch conflict")
                self._delete_history_conn(conn,new_epoch=int(reset_epoch),reset_at=float(reset_payload.get("reset_at_utc") or time.time()))
                record_id=str(reset_rows[0].get("record_id") or f"reset:{int(reset_epoch)}")
                digest=_sha(_json({"type":"reset","payload":reset_payload}))
                conn.execute(
                    "INSERT OR REPLACE INTO consumption_sync_receipts(device_id,record_id,record_hash,received_at_utc) VALUES(?,?,?,?)",
                    (str(device_id),record_id,digest,time.time()),
                )
                return {"schema":2,"reset_epoch":int(reset_epoch),"reset_required":False,"results":[{"record_id":record_id,"status":"applied"}]}
            results=[]
            for raw in rows:
                if not isinstance(raw,dict): raise ConsumptionLedgerError("consumption sync record must be an object")
                record_type=str(raw.get("type") or "").casefold(); payload=raw.get("payload") if isinstance(raw.get("payload"),dict) else {}
                record_id=str(raw.get("record_id") or f"{record_type}:{_sha(_json(payload))}")
                digest=_sha(_json({"type":record_type,"payload":payload}))
                receipt=conn.execute("SELECT record_hash FROM consumption_sync_receipts WHERE device_id=? AND record_id=?",(str(device_id),record_id)).fetchone()
                if receipt is not None:
                    if str(receipt["record_hash"])!=digest: raise ConsumptionConflictError("consumption sync retry changed content")
                    results.append({"record_id":record_id,"status":"duplicate"}); continue
                if record_type=="reset":
                    payload_epoch=int(payload.get("reset_epoch") or current_epoch)
                    if payload_epoch!=current_epoch:
                        raise ConsumptionConflictError("reset record did not match current epoch")
                    conn.execute("INSERT INTO consumption_sync_receipts(device_id,record_id,record_hash,received_at_utc) VALUES(?,?,?,?)",(str(device_id),record_id,digest,time.time()))
                    results.append({"record_id":record_id,"status":"duplicate"})
                    continue
                status=self._apply_sync_record_conn(conn,str(device_id),record_type,payload,device_name=device_name)
                conn.execute("INSERT INTO consumption_sync_receipts(device_id,record_id,record_hash,received_at_utc) VALUES(?,?,?,?)",(str(device_id),record_id,digest,time.time()))
                key=record_id.split(":",1)[1] if record_id.startswith(record_type+":") else _sha(record_id)
                self._queue_sync_record(conn,record_type,key,payload)
                results.append({"record_id":record_id,"status":status})
            return {"schema":2,"reset_epoch":current_epoch,"reset_required":False,"results":results}

    def _delete_history_conn(self, conn: Any, *, new_epoch: int, reset_at: float) -> None:
        conn.execute("DELETE FROM consumption_sync_outbox")
        conn.execute("DELETE FROM consumption_sync_receipts")
        conn.execute("DELETE FROM vn_capture_sessions")
        conn.execute("DELETE FROM consumption_event_media")
        conn.execute("DELETE FROM consumption_events")
        conn.execute("DELETE FROM consumption_sessions")
        conn.execute("DELETE FROM consumption_corrections")
        conn.execute("DELETE FROM consumption_manual_entries")
        conn.execute("DELETE FROM consumption_daily_rollups")
        conn.execute(
            """INSERT INTO consumption_history_state(profile_id,reset_epoch,reset_at_utc) VALUES('default',?,?)
               ON CONFLICT(profile_id) DO UPDATE SET reset_epoch=excluded.reset_epoch,reset_at_utc=excluded.reset_at_utc""",
            (int(new_epoch),float(reset_at)),
        )
        self._queue_sync_record(conn,"reset",str(int(new_epoch)),{"reset_epoch":int(new_epoch),"reset_at_utc":float(reset_at)},created_at=float(reset_at))

    def delete_history(self) -> dict[str, Any]:
        now=time.time()
        with self.database.connect() as conn:
            epoch=self._history_epoch_conn(conn)+1
            self._delete_history_conn(conn,new_epoch=epoch,reset_at=now)
        return {"ok":True,"reset_epoch":epoch,"reset_at_utc":now}

    def ledger_counts(self) -> dict[str, int]:
        with self.database.connect() as conn:
            values = {}
            for key, table in (
                ("media", "consumption_media"),
                ("sessions", "consumption_sessions"),
                ("events", "consumption_events"),
                ("vn_titles", "vn_titles"),
            ):
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                values[key] = int(row["n"] if row else 0)
        return values
