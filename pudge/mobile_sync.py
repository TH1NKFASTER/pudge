from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from .database import Database


PROTOCOL_VERSION = 1
SERVER_DEVICE_ID = "server"
_MAX_CLOCK_SKEW_SECONDS = 30 * 24 * 60 * 60


class MobileSyncError(RuntimeError):
    pass


class MobileSyncAuthenticationError(MobileSyncError):
    pass


class MobileSyncValidationError(MobileSyncError):
    pass


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_timestamp(value: object, *, now: float | None = None) -> float:
    current = float(now if now is not None else time.time())
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return current
    return max(current - _MAX_CLOCK_SKEW_SECONDS, min(current + 300.0, timestamp))


def _clamp_int(value: object, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def _clamp_float(value: object, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True, slots=True)
class LocalSyncEntity:
    kind: str
    local_key: str
    external_key: str
    title: str
    metadata: dict[str, Any]
    position: dict[str, Any]
    status: str
    occurred_at: float


class MobileSyncService:
    """Stable event protocol between Pudge and companion clients.

    The public protocol only exposes opaque entity UUIDs and canonical positions.
    Local numeric IDs and table layouts remain implementation details, so an iOS
    client does not need to change when Pudge's internal schema evolves.
    """

    def __init__(
        self,
        database: Database,
        *,
        pairing_ttl_seconds: float = 300.0,
        max_events_per_request: int = 500,
    ) -> None:
        self.database = database
        self.pairing_ttl_seconds = max(30.0, float(pairing_ttl_seconds))
        self.max_events_per_request = max(1, min(2000, int(max_events_per_request)))
        self._ensure_server_device()

    def _ensure_server_device(self) -> None:
        now = time.time()
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO sync_devices(
                    device_id,name,platform,token_hash,created_at,last_seen_at,revoked_at
                ) VALUES(?,?,?,?,?,?,0)
                ON CONFLICT(device_id) DO UPDATE SET
                    name=excluded.name,
                    platform=excluded.platform,
                    last_seen_at=excluded.last_seen_at,
                    revoked_at=0
                """,
                (SERVER_DEVICE_ID, "Pudge", "server", "", now, now),
            )

    def protocol_info(self) -> dict[str, Any]:
        return {
            "protocol": "pudge-sync",
            "version": PROTOCOL_VERSION,
            "cursor": "monotonic-event-id",
            "entities": ["anime_episode", "manga", "light_novel", "audiobook"],
            "events": ["progress.updated"],
        }

    def start_pairing(self) -> dict[str, Any]:
        pairing_token = secrets.token_urlsafe(24)
        now = time.time()
        expires_at = now + self.pairing_ttl_seconds
        with self.database.connect() as conn:
            conn.execute(
                "DELETE FROM sync_pairing_codes WHERE expires_at<? OR used_at>0",
                (now,),
            )
            conn.execute(
                """
                INSERT INTO sync_pairing_codes(token_hash,created_at,expires_at,used_at)
                VALUES(?,?,?,0)
                """,
                (_token_hash(pairing_token), now, expires_at),
            )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "pairing_token": pairing_token,
            "expires_at": expires_at,
        }

    def complete_pairing(
        self,
        pairing_token: str,
        *,
        name: str,
        platform: str,
    ) -> dict[str, Any]:
        token_value = str(pairing_token or "").strip()
        if len(token_value) < 16:
            raise MobileSyncValidationError("Invalid pairing token")
        device_name = str(name or "Companion").strip()[:80] or "Companion"
        device_platform = str(platform or "unknown").strip().lower()[:40] or "unknown"
        now = time.time()
        device_id = str(uuid.uuid4())
        access_token = secrets.token_urlsafe(32)
        with self.database.connect() as conn:
            row = conn.execute(
                """
                SELECT token_hash,expires_at,used_at
                FROM sync_pairing_codes WHERE token_hash=?
                """,
                (_token_hash(token_value),),
            ).fetchone()
            if row is None or float(row["expires_at"]) < now or float(row["used_at"]) > 0:
                raise MobileSyncAuthenticationError("Pairing token expired or already used")
            updated = conn.execute(
                """
                UPDATE sync_pairing_codes SET used_at=?
                WHERE token_hash=? AND used_at=0 AND expires_at>=?
                """,
                (now, row["token_hash"], now),
            ).rowcount
            if updated != 1:
                raise MobileSyncAuthenticationError("Pairing token expired or already used")
            conn.execute(
                """
                INSERT INTO sync_devices(
                    device_id,name,platform,token_hash,created_at,last_seen_at,revoked_at
                ) VALUES(?,?,?,?,?,?,0)
                """,
                (
                    device_id,
                    device_name,
                    device_platform,
                    _token_hash(access_token),
                    now,
                    now,
                ),
            )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "device_id": device_id,
            "access_token": access_token,
            "server_time": now,
        }

    def authenticate(self, access_token: str) -> str:
        token_value = str(access_token or "").strip()
        if not token_value:
            raise MobileSyncAuthenticationError("Missing bearer token")
        now = time.time()
        with self.database.connect() as conn:
            row = conn.execute(
                """
                SELECT device_id FROM sync_devices
                WHERE token_hash=? AND revoked_at=0
                """,
                (_token_hash(token_value),),
            ).fetchone()
            if row is None:
                raise MobileSyncAuthenticationError("Invalid bearer token")
            device_id = str(row["device_id"])
            conn.execute(
                "UPDATE sync_devices SET last_seen_at=? WHERE device_id=?",
                (now, device_id),
            )
        return device_id

    def devices(self) -> list[dict[str, Any]]:
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT device_id,name,platform,created_at,last_seen_at,revoked_at
                FROM sync_devices WHERE device_id!=? ORDER BY created_at
                """,
                (SERVER_DEVICE_ID,),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_device(self, device_id: str) -> bool:
        identifier = str(device_id or "").strip()
        if not identifier or identifier == SERVER_DEVICE_ID:
            return False
        with self.database.connect() as conn:
            changed = conn.execute(
                "UPDATE sync_devices SET revoked_at=? WHERE device_id=? AND revoked_at=0",
                (time.time(), identifier),
            ).rowcount
        return changed == 1

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None

    def _local_entities(self, conn: sqlite3.Connection) -> list[LocalSyncEntity]:
        result: list[LocalSyncEntity] = []
        result.extend(self._anime_entities(conn))
        result.extend(self._manga_entities(conn))
        result.extend(self._light_novel_entities(conn))
        result.extend(self._audiobook_entities(conn))
        return result

    def _anime_entities(self, conn: sqlite3.Connection) -> list[LocalSyncEntity]:
        if not self._table_exists(conn, "episodes"):
            return []
        rows = conn.execute(
            """
            SELECT e.media_id,COALESCE(e.media_episode,e.episode) AS logical_episode,
                   e.title AS episode_title,e.playback_position,e.playback_duration,
                   e.playback_updated_at,e.watched_at,e.state,e.updated_at,
                   a.title AS anime_title,a.cover_url,a.site_url
            FROM episodes e
            LEFT JOIN anime a ON a.media_id=e.media_id
            WHERE e.media_id IS NOT NULL
              AND COALESCE(e.media_episode,e.episode) IS NOT NULL
            ORDER BY e.media_id,logical_episode,e.updated_at DESC
            """
        ).fetchall()
        seen: set[str] = set()
        entities: list[LocalSyncEntity] = []
        for row in rows:
            media_id = int(row["media_id"])
            episode = int(row["logical_episode"])
            local_key = f"{media_id}:{episode}"
            if local_key in seen:
                continue
            seen.add(local_key)
            seconds = max(0.0, float(row["playback_position"] or 0.0))
            duration = max(0.0, float(row["playback_duration"] or 0.0))
            completed = bool(row["watched_at"]) or str(row["state"] or "") == "watched"
            status = "completed" if completed else ("in_progress" if seconds > 0 else "not_started")
            entities.append(
                LocalSyncEntity(
                    kind="anime_episode",
                    local_key=local_key,
                    external_key=f"anilist:{media_id}:episode:{episode}",
                    title=f"{row['anime_title'] or media_id} · {episode}",
                    metadata={
                        "media_id": media_id,
                        "episode": episode,
                        "anime_title": str(row["anime_title"] or ""),
                        "episode_title": str(row["episode_title"] or ""),
                        "cover_url": str(row["cover_url"] or ""),
                        "site_url": str(row["site_url"] or ""),
                    },
                    position={
                        "episode": episode,
                        "position_ms": int(round(seconds * 1000.0)),
                        "duration_ms": int(round(duration * 1000.0)),
                    },
                    status=status,
                    occurred_at=float(row["playback_updated_at"] or row["updated_at"] or 0.0),
                )
            )
        return entities

    def _manga_entities(self, conn: sqlite3.Connection) -> list[LocalSyncEntity]:
        if not self._table_exists(conn, "manga_books"):
            return []
        rows = conn.execute(
            """
            SELECT id,path,title,page_count,position,reading_direction,anilist_id,
                   cover_url,site_url,source_fingerprint,updated_at
            FROM manga_books ORDER BY id
            """
        ).fetchall()
        entities: list[LocalSyncEntity] = []
        for row in rows:
            book_id = int(row["id"])
            page_count = max(0, int(row["page_count"] or 0))
            page_index = _clamp_int(row["position"], 0, max(0, page_count - 1))
            external = (
                f"anilist:{int(row['anilist_id'])}:manga"
                if row["anilist_id"] is not None
                else f"fingerprint:{row['source_fingerprint'] or book_id}"
            )
            entities.append(
                LocalSyncEntity(
                    kind="manga",
                    local_key=str(book_id),
                    external_key=external,
                    title=str(row["title"] or ""),
                    metadata={
                        "book_id": book_id,
                        "anilist_id": row["anilist_id"],
                        "page_count": page_count,
                        "reading_direction": str(row["reading_direction"] or "rtl"),
                        "cover_url": str(row["cover_url"] or ""),
                        "site_url": str(row["site_url"] or ""),
                    },
                    position={"page_index": page_index, "page_count": page_count},
                    status=(
                        "completed"
                        if page_count > 0 and page_index >= page_count - 1
                        else ("in_progress" if page_index > 0 else "not_started")
                    ),
                    occurred_at=float(row["updated_at"] or 0.0),
                )
            )
        return entities

    def _light_novel_entities(self, conn: sqlite3.Connection) -> list[LocalSyncEntity]:
        if not self._table_exists(conn, "ln_books"):
            return []
        rows = conn.execute(
            """
            SELECT b.id,b.title,b.volume,b.anilist_id,b.cover_url,b.current_chapter,
                   b.current_offset,b.finished,b.updated_at,
                   c.title AS chapter_title,c.text,c.text_hash
            FROM ln_books b
            LEFT JOIN ln_chapters c
              ON c.book_id=b.id AND c.chapter_index=b.current_chapter
            ORDER BY b.id
            """
        ).fetchall()
        entities: list[LocalSyncEntity] = []
        for row in rows:
            book_id = int(row["id"])
            chapter_index = max(0, int(row["current_chapter"] or 0))
            chapter_text = str(row["text"] or "")
            chapter_length = len(chapter_text)
            fraction = _clamp_float(row["current_offset"], 0.0, 1.0)
            character_offset = int(round(fraction * chapter_length)) if chapter_length else 0
            external = (
                f"anilist:{int(row['anilist_id'])}:volume:{int(row['volume'] or 0)}"
                if row["anilist_id"] is not None
                else f"local-ln:{book_id}"
            )
            entities.append(
                LocalSyncEntity(
                    kind="light_novel",
                    local_key=str(book_id),
                    external_key=external,
                    title=str(row["title"] or ""),
                    metadata={
                        "book_id": book_id,
                        "anilist_id": row["anilist_id"],
                        "volume": row["volume"],
                        "cover_url": str(row["cover_url"] or ""),
                        "chapter_title": str(row["chapter_title"] or ""),
                    },
                    position={
                        "chapter_index": chapter_index,
                        "character_offset": character_offset,
                        "chapter_length": chapter_length,
                        "chapter_hash": str(row["text_hash"] or ""),
                        "fraction": fraction,
                    },
                    status=(
                        "completed"
                        if bool(row["finished"])
                        else ("in_progress" if chapter_index > 0 or fraction > 0 else "not_started")
                    ),
                    occurred_at=float(row["updated_at"] or 0.0),
                )
            )
        return entities

    def _audiobook_entities(self, conn: sqlite3.Connection) -> list[LocalSyncEntity]:
        if not self._table_exists(conn, "audiobooks"):
            return []
        rows = conn.execute(
            """
            SELECT id,title,duration,position,finished,speed,updated_at
            FROM audiobooks ORDER BY id
            """
        ).fetchall()
        entities: list[LocalSyncEntity] = []
        for row in rows:
            book_id = int(row["id"])
            duration = max(0.0, float(row["duration"] or 0.0))
            position = _clamp_float(row["position"], 0.0, duration or float("inf"))
            entities.append(
                LocalSyncEntity(
                    kind="audiobook",
                    local_key=str(book_id),
                    external_key=f"local-audiobook:{book_id}",
                    title=str(row["title"] or ""),
                    metadata={"book_id": book_id, "duration_ms": int(round(duration * 1000.0))},
                    position={
                        "position_ms": int(round(position * 1000.0)),
                        "duration_ms": int(round(duration * 1000.0)),
                        "speed": float(row["speed"] or 1.0),
                    },
                    status=(
                        "completed"
                        if bool(row["finished"])
                        else ("in_progress" if position > 0 else "not_started")
                    ),
                    occurred_at=float(row["updated_at"] or 0.0),
                )
            )
        return entities

    @staticmethod
    def _ensure_entity(conn: sqlite3.Connection, entity: LocalSyncEntity) -> str:
        row = conn.execute(
            "SELECT entity_id FROM sync_entities WHERE kind=? AND local_key=?",
            (entity.kind, entity.local_key),
        ).fetchone()
        now = time.time()
        if row is not None:
            entity_id = str(row["entity_id"])
            conn.execute(
                """
                UPDATE sync_entities SET external_key=?,title=?,metadata_json=?,updated_at=?
                WHERE entity_id=?
                """,
                (
                    entity.external_key,
                    entity.title,
                    _json_dumps(entity.metadata),
                    now,
                    entity_id,
                ),
            )
            return entity_id
        entity_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO sync_entities(
                entity_id,kind,local_key,external_key,title,metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                entity_id,
                entity.kind,
                entity.local_key,
                entity.external_key,
                entity.title,
                _json_dumps(entity.metadata),
                now,
                now,
            ),
        )
        return entity_id

    def _record_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_uuid: str,
        device_id: str,
        entity_id: str,
        event_type: str,
        payload: dict[str, Any],
        occurred_at: float,
        received_at: float,
    ) -> int | None:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO sync_events(
                event_uuid,device_id,entity_id,event_type,payload_json,occurred_at,received_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                event_uuid,
                device_id,
                entity_id,
                event_type,
                _json_dumps(payload),
                occurred_at,
                received_at,
            ),
        )
        if cursor.rowcount != 1:
            return None
        return int(cursor.lastrowid)

    def capture_local_changes(self) -> int:
        now = time.time()
        last_event_id = 0
        with self.database.connect() as conn:
            for entity in self._local_entities(conn):
                entity_id = self._ensure_entity(conn, entity)
                position_json = _json_dumps(entity.position)
                snapshot = conn.execute(
                    """
                    SELECT position_json,status,occurred_at,event_id
                    FROM sync_snapshots WHERE entity_id=?
                    """,
                    (entity_id,),
                ).fetchone()
                if snapshot is not None:
                    unchanged = (
                        str(snapshot["position_json"]) == position_json
                        and str(snapshot["status"]) == entity.status
                    )
                    if unchanged:
                        last_event_id = max(last_event_id, int(snapshot["event_id"] or 0))
                        continue
                    if float(snapshot["occurred_at"] or 0.0) > entity.occurred_at + 0.001:
                        continue
                digest = hashlib.sha1(
                    f"{entity_id}:{position_json}:{entity.status}:{entity.occurred_at:.6f}".encode(
                        "utf-8"
                    )
                ).hexdigest()
                event_id = self._record_event(
                    conn,
                    event_uuid=f"local-{digest}",
                    device_id=SERVER_DEVICE_ID,
                    entity_id=entity_id,
                    event_type="progress.updated",
                    payload={"position": entity.position, "status": entity.status},
                    occurred_at=max(0.0, entity.occurred_at),
                    received_at=now,
                )
                if event_id is None:
                    existing = conn.execute(
                        "SELECT id FROM sync_events WHERE event_uuid=?", (f"local-{digest}",)
                    ).fetchone()
                    event_id = int(existing["id"]) if existing else 0
                conn.execute(
                    """
                    INSERT INTO sync_snapshots(
                        entity_id,position_json,status,source_device_id,event_id,occurred_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(entity_id) DO UPDATE SET
                        position_json=excluded.position_json,
                        status=excluded.status,
                        source_device_id=excluded.source_device_id,
                        event_id=excluded.event_id,
                        occurred_at=excluded.occurred_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        entity_id,
                        position_json,
                        entity.status,
                        SERVER_DEVICE_ID,
                        event_id,
                        max(0.0, entity.occurred_at),
                        now,
                    ),
                )
                last_event_id = max(last_event_id, int(event_id or 0))
        return last_event_id

    def library_snapshot(self) -> dict[str, Any]:
        self.capture_local_changes()
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT e.entity_id,e.kind,e.external_key,e.title,e.metadata_json,
                       s.position_json,s.status,s.event_id AS revision,s.occurred_at
                FROM sync_entities e
                LEFT JOIN sync_snapshots s ON s.entity_id=e.entity_id
                ORDER BY e.kind,e.title,e.entity_id
                """
            ).fetchall()
            relations = self._paired_relations(conn)
            cursor_row = conn.execute("SELECT COALESCE(MAX(id),0) AS cursor FROM sync_events").fetchone()
        return {
            "protocol_version": PROTOCOL_VERSION,
            "cursor": int(cursor_row["cursor"] if cursor_row else 0),
            "entities": [
                {
                    "entity_id": str(row["entity_id"]),
                    "kind": str(row["kind"]),
                    "external_key": str(row["external_key"] or ""),
                    "title": str(row["title"] or ""),
                    "metadata": _json_loads(str(row["metadata_json"] or "{}"), {}),
                    "position": _json_loads(str(row["position_json"] or "{}"), {}),
                    "status": str(row["status"] or "not_started"),
                    "revision": int(row["revision"] or 0),
                    "occurred_at": float(row["occurred_at"] or 0.0),
                }
                for row in rows
            ],
            "relations": relations,
        }

    @staticmethod
    def _paired_relations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        if not MobileSyncService._table_exists(conn, "reading_audio_links"):
            return []
        rows = conn.execute(
            "SELECT ln_book_id,audiobook_id,alignment_mode,updated_at FROM reading_audio_links"
        ).fetchall()
        relations: list[dict[str, Any]] = []
        for row in rows:
            ln = conn.execute(
                "SELECT entity_id FROM sync_entities WHERE kind='light_novel' AND local_key=?",
                (str(int(row["ln_book_id"])),),
            ).fetchone()
            audio = conn.execute(
                "SELECT entity_id FROM sync_entities WHERE kind='audiobook' AND local_key=?",
                (str(int(row["audiobook_id"])),),
            ).fetchone()
            if ln is None or audio is None:
                continue
            relations.append(
                {
                    "type": "read_with_audio",
                    "from_entity_id": str(ln["entity_id"]),
                    "to_entity_id": str(audio["entity_id"]),
                    "alignment_mode": str(row["alignment_mode"] or "chapter"),
                    "updated_at": float(row["updated_at"] or 0.0),
                }
            )
        return relations

    def changes(self, *, cursor: int = 0, limit: int = 200) -> dict[str, Any]:
        self.capture_local_changes()
        start = max(0, int(cursor))
        page_size = max(1, min(self.max_events_per_request, int(limit)))
        with self.database.connect() as conn:
            rows = conn.execute(
                """
                SELECT id,event_uuid,device_id,entity_id,event_type,payload_json,
                       occurred_at,received_at
                FROM sync_events WHERE id>? ORDER BY id LIMIT ?
                """,
                (start, page_size + 1),
            ).fetchall()
        has_more = len(rows) > page_size
        visible = rows[:page_size]
        next_cursor = int(visible[-1]["id"]) if visible else start
        return {
            "protocol_version": PROTOCOL_VERSION,
            "cursor": next_cursor,
            "has_more": has_more,
            "events": [
                {
                    "cursor": int(row["id"]),
                    "event_id": str(row["event_uuid"]),
                    "device_id": str(row["device_id"]),
                    "entity_id": str(row["entity_id"]),
                    "type": str(row["event_type"]),
                    "payload": _json_loads(str(row["payload_json"]), {}),
                    "occurred_at": float(row["occurred_at"]),
                    "received_at": float(row["received_at"]),
                }
                for row in visible
            ],
        }

    def push_events(
        self,
        device_id: str,
        events: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        event_list = list(events)
        if len(event_list) > self.max_events_per_request:
            raise MobileSyncValidationError("Too many events in one request")
        now = time.time()
        results: list[dict[str, Any]] = []
        max_cursor = 0
        with self.database.connect() as conn:
            device = conn.execute(
                "SELECT 1 FROM sync_devices WHERE device_id=? AND revoked_at=0",
                (str(device_id),),
            ).fetchone()
            if device is None:
                raise MobileSyncAuthenticationError("Device is not active")
            for raw in event_list:
                result = self._push_one(conn, str(device_id), raw, now=now)
                results.append(result)
                max_cursor = max(max_cursor, int(result.get("cursor") or 0))
            latest = conn.execute("SELECT COALESCE(MAX(id),0) AS cursor FROM sync_events").fetchone()
            max_cursor = max(max_cursor, int(latest["cursor"] if latest else 0))
        return {
            "protocol_version": PROTOCOL_VERSION,
            "cursor": max_cursor,
            "results": results,
        }

    def _push_one(
        self,
        conn: sqlite3.Connection,
        device_id: str,
        raw: dict[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise MobileSyncValidationError("Each event must be an object")
        event_uuid = str(raw.get("event_id") or "").strip()
        entity_id = str(raw.get("entity_id") or "").strip()
        event_type = str(raw.get("type") or "progress.updated").strip()
        if not event_uuid or len(event_uuid) > 160:
            raise MobileSyncValidationError("event_id is required")
        if event_type != "progress.updated":
            raise MobileSyncValidationError(f"Unsupported event type: {event_type}")
        entity = conn.execute(
            "SELECT entity_id,kind,local_key FROM sync_entities WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        if entity is None:
            raise MobileSyncValidationError("Unknown entity_id")
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            payload = {
                "position": raw.get("position") if isinstance(raw.get("position"), dict) else {},
                "status": raw.get("status") or "in_progress",
            }
        position = payload.get("position")
        if not isinstance(position, dict):
            raise MobileSyncValidationError("payload.position must be an object")
        status = str(payload.get("status") or "in_progress").strip().lower()
        if status not in {"not_started", "in_progress", "completed"}:
            raise MobileSyncValidationError("Unsupported progress status")
        occurred_at = _bounded_timestamp(raw.get("occurred_at"), now=now)
        event_id = self._record_event(
            conn,
            event_uuid=event_uuid,
            device_id=device_id,
            entity_id=entity_id,
            event_type=event_type,
            payload={"position": position, "status": status},
            occurred_at=occurred_at,
            received_at=now,
        )
        if event_id is None:
            existing = conn.execute(
                "SELECT id FROM sync_events WHERE event_uuid=?", (event_uuid,)
            ).fetchone()
            return {
                "event_id": event_uuid,
                "status": "duplicate",
                "cursor": int(existing["id"] if existing else 0),
            }
        snapshot = conn.execute(
            "SELECT occurred_at,event_id FROM sync_snapshots WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        applies = (
            snapshot is None
            or occurred_at > float(snapshot["occurred_at"] or 0.0) + 0.001
            or (
                abs(occurred_at - float(snapshot["occurred_at"] or 0.0)) <= 0.001
                and event_id > int(snapshot["event_id"] or 0)
            )
        )
        if applies:
            normalized = self._normalize_position(str(entity["kind"]), position)
            self._apply_to_local(
                conn,
                kind=str(entity["kind"]),
                local_key=str(entity["local_key"]),
                position=normalized,
                status=status,
                occurred_at=occurred_at,
            )
            conn.execute(
                """
                INSERT INTO sync_snapshots(
                    entity_id,position_json,status,source_device_id,event_id,occurred_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    position_json=excluded.position_json,
                    status=excluded.status,
                    source_device_id=excluded.source_device_id,
                    event_id=excluded.event_id,
                    occurred_at=excluded.occurred_at,
                    updated_at=excluded.updated_at
                """,
                (
                    entity_id,
                    _json_dumps(normalized),
                    status,
                    device_id,
                    event_id,
                    occurred_at,
                    now,
                ),
            )
        return {
            "event_id": event_uuid,
            "status": "applied" if applies else "recorded_stale",
            "cursor": event_id,
        }

    @staticmethod
    def _normalize_position(kind: str, position: dict[str, Any]) -> dict[str, Any]:
        if kind == "anime_episode":
            return {
                "episode": max(1, int(position.get("episode") or 1)),
                "position_ms": max(0, int(position.get("position_ms") or 0)),
                "duration_ms": max(0, int(position.get("duration_ms") or 0)),
            }
        if kind == "manga":
            page_count = max(0, int(position.get("page_count") or 0))
            return {
                "page_index": _clamp_int(
                    position.get("page_index"), 0, max(0, page_count - 1)
                ),
                "page_count": page_count,
            }
        if kind == "light_novel":
            chapter_length = max(0, int(position.get("chapter_length") or 0))
            character_offset = _clamp_int(
                position.get("character_offset"), 0, chapter_length
            )
            fraction = (
                character_offset / float(chapter_length)
                if chapter_length > 0
                else _clamp_float(position.get("fraction"), 0.0, 1.0)
            )
            return {
                "chapter_index": max(0, int(position.get("chapter_index") or 0)),
                "character_offset": character_offset,
                "chapter_length": chapter_length,
                "chapter_hash": str(position.get("chapter_hash") or ""),
                "fraction": fraction,
            }
        if kind == "audiobook":
            duration_ms = max(0, int(position.get("duration_ms") or 0))
            maximum = duration_ms if duration_ms > 0 else 2**63 - 1
            return {
                "position_ms": _clamp_int(position.get("position_ms"), 0, maximum),
                "duration_ms": duration_ms,
                "speed": _clamp_float(position.get("speed", 1.0), 0.25, 4.0),
            }
        raise MobileSyncValidationError(f"Unsupported entity kind: {kind}")

    def _apply_to_local(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        local_key: str,
        position: dict[str, Any],
        status: str,
        occurred_at: float,
    ) -> None:
        if kind == "anime_episode":
            media_id, episode = (int(value) for value in local_key.split(":", 1))
            conn.execute(
                """
                UPDATE episodes SET playback_position=?,playback_duration=?,
                    playback_updated_at=?,watched_at=CASE
                        WHEN ?='completed' THEN COALESCE(watched_at,?)
                        ELSE NULL END,
                    state=CASE
                        WHEN ?='completed' THEN 'watched'
                        WHEN state='watched' THEN 'ready'
                        ELSE state END,
                    updated_at=MAX(updated_at,?)
                WHERE media_id=? AND COALESCE(media_episode,episode)=?
                """,
                (
                    float(position["position_ms"]) / 1000.0,
                    float(position["duration_ms"]) / 1000.0,
                    occurred_at,
                    status,
                    occurred_at,
                    status,
                    occurred_at,
                    media_id,
                    episode,
                ),
            )
            return
        if kind == "manga":
            conn.execute(
                "UPDATE manga_books SET position=?,updated_at=? WHERE id=?",
                (int(position["page_index"]), occurred_at, int(local_key)),
            )
            return
        if kind == "light_novel":
            book_id = int(local_key)
            chapter_index = int(position["chapter_index"])
            chapter = conn.execute(
                "SELECT text,text_hash FROM ln_chapters WHERE book_id=? AND chapter_index=?",
                (book_id, chapter_index),
            ).fetchone()
            fraction = float(position["fraction"])
            if chapter is not None:
                text_length = len(str(chapter["text"] or ""))
                incoming_hash = str(position.get("chapter_hash") or "")
                if incoming_hash and incoming_hash == str(chapter["text_hash"] or "") and text_length:
                    fraction = _clamp_int(
                        position.get("character_offset"), 0, text_length
                    ) / float(text_length)
            conn.execute(
                """
                UPDATE ln_books SET current_chapter=?,current_offset=?,
                    finished=?,updated_at=? WHERE id=?
                """,
                (
                    chapter_index,
                    _clamp_float(fraction, 0.0, 1.0),
                    int(status == "completed"),
                    occurred_at,
                    book_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO ln_bookmarks(book_id,chapter_index,offset,source,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET
                    chapter_index=excluded.chapter_index,
                    offset=excluded.offset,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (
                    book_id,
                    chapter_index,
                    _clamp_float(fraction, 0.0, 1.0),
                    "sync",
                    occurred_at,
                ),
            )
            return
        if kind == "audiobook":
            conn.execute(
                """
                UPDATE audiobooks SET position=?,speed=?,finished=?,updated_at=? WHERE id=?
                """,
                (
                    float(position["position_ms"]) / 1000.0,
                    float(position["speed"]),
                    int(status == "completed"),
                    occurred_at,
                    int(local_key),
                ),
            )
            return
        raise MobileSyncValidationError(f"Unsupported entity kind: {kind}")
