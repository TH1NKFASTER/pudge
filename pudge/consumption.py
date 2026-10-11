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

# Lower is stronger: aliases that identify this exact file/library item.
_ALIAS_PRIORITY = {"content": 0, "path": 1, "video_path": 1, "legacy_fingerprint": 2, "local": 3}
# Identities several library items can share (AniList volume/episode).
_SHARED_ALIAS_TYPES = frozenset({"external"})

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
            existing: dict[tuple[str, str], str] = {}
            for alias_type, alias_value in clean_aliases:
                row = conn.execute(
                    "SELECT media_uuid FROM consumption_media_aliases "
                    "WHERE kind=? AND alias_type=? AND alias_value=?",
                    (kind, alias_type, alias_value),
                ).fetchone()
                if row is not None:
                    existing[(alias_type, alias_value)] = str(row["media_uuid"])
            media_uuid = self._resolve_media(
                conn,
                existing,
                current_library_kind=str(current_library_kind or ""),
                current_library_id=str(current_library_id or ""),
            ) or str(preferred_uuid or uuid.uuid4())
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
                        str(title or ""),
                        str(title or ""),
                        str(current_library_kind or ""),
                        str(current_library_id or ""),
                        str(source_revision or ""),
                        str(source_revision or ""),
                        payload,
                        now,
                        media_uuid,
                    ),
                )
            for alias_type, alias_value in clean_aliases:
                owner = existing.get((alias_type, alias_value))
                if alias_type in _SHARED_ALIAS_TYPES and owner is not None and owner != media_uuid:
                    # A shared identity (AniList volume or episode) keeps pointing
                    # at the item that owns it; a second book never takes it over.
                    continue
                if owner == media_uuid:
                    continue
                conn.execute(
                    """
                    INSERT INTO consumption_media_aliases(kind,alias_type,alias_value,media_uuid,created_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(kind,alias_type,alias_value) DO UPDATE SET media_uuid=excluded.media_uuid
                    """,
                    (kind, alias_type, alias_value, media_uuid, now),
                )
        return ConsumptionMediaRef(media_uuid, kind, str(title or ""), str(source_revision or ""))

    @staticmethod
    def _resolve_media(
        conn: Any,
        existing: dict[tuple[str, str], str],
        *,
        current_library_kind: str,
        current_library_id: str,
    ) -> str:
        """Pick the media an item's aliases point to, strongest alias first.

        Content/path/local aliases identify this very item. A shared alias
        (AniList volume or episode) only merges into media no other library
        item owns — a re-import or re-download — never into a different book
        that happens to carry the same AniList volume.
        """
        own = sorted(
            (
                (_ALIAS_PRIORITY.get(alias_type, 50), media)
                for (alias_type, _value), media in existing.items()
                if alias_type not in _SHARED_ALIAS_TYPES
            ),
        )
        if own:
            if len({media for _rank, media in own}) > 1:
                from .logging_utils import configure_logging

                configure_logging().warning(
                    "CONFLICT step=consumption.media_aliases chosen=%s others=%s",
                    own[0][1],
                    sorted({media for _rank, media in own[1:]} - {own[0][1]}),
                )
            return own[0][1]
        for (alias_type, _value), media in existing.items():
            if alias_type not in _SHARED_ALIAS_TYPES:
                continue
            row = conn.execute(
                "SELECT current_library_kind,current_library_id FROM consumption_media WHERE media_uuid=?",
                (media,),
            ).fetchone()
            if row is None:
                return media
            owner = (str(row["current_library_kind"] or ""), str(row["current_library_id"] or ""))
            if not owner[1] or owner == (current_library_kind, current_library_id):
                return media
        return ""

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
                SELECT e.media_id,COALESCE(e.media_episode,e.episode) AS episode,e.media_episode_end AS episode_end,
                       e.title AS episode_title,a.title AS anime_title
                FROM episodes e LEFT JOIN anime a ON a.media_id=e.media_id
                WHERE e.video_path=? ORDER BY e.updated_at DESC LIMIT 1
                """,
                (path,),
            ).fetchone()
        if row is None or row["media_id"] is None:
            return None
        media_id = int(row["media_id"])
        # Movies and one-part specials are stored without an episode number.
        # AniList counts them as episode 1, and their watch time must reach
        # statistics like any episode's (a two-hour movie used to be dropped).
        single_part = row["episode"] is None
        episode = 1 if single_part else int(row["episode"])
        title = str(row["anime_title"] or media_id)
        if not single_part:
            title += f" · {episode}"
        if row["episode_end"] is not None:
            title += f"–{int(row['episode_end'])}"  # two episodes in one file
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
            metadata={
                "anilist_id": row["anilist_id"],
                "volume": row["volume"],
                "character_count": total_characters,
            },
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
            source_parts.append(f"{sampled or file_path.name}|{float(item['duration'] or 0):.3f}")
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
                    conn.execute("UPDATE vn_titles SET title=?,updated_at=? WHERE id=?", (title, now, vn_id))
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
                    return ConsumptionAppendResult(str(seq_existing["event_id"]), device_id, seq, True)
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
                    identifier,
                    device_id,
                    seq,
                    str(session_id),
                    str(kind),
                    start,
                    end,
                    elapsed,
                    received,
                    resolved_timezone,
                    resolved_offset,
                    payload_json,
                    content_hash,
                    LEDGER_SCHEMA_VERSION,
                ),
            )
            for row in media_rows:
                conn.execute(
                    """
                    INSERT INTO consumption_event_media(
                        event_id,media_uuid,source_revision,role,locator_start_json,locator_end_json
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        identifier,
                        row["media_uuid"],
                        row["source_revision"],
                        row["role"],
                        _json(row["locator_start"]),
                        _json(row["locator_end"]),
                    ),
                )
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
        previous_position = (
            float(previous["playback_position"] or position) if previous is not None else float(position)
        )
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
                    media=[
                        {
                            "media_uuid": media_ref.media_uuid,
                            "source_revision": media_ref.source_revision,
                            "role": "primary",
                            "locator_start": {"position_seconds": max(0.0, locator0)},
                            "locator_end": {"position_seconds": max(0.0, locator1)},
                        }
                    ],
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
                media_ref.media_uuid,
                len(results),
                active,
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
        normalized_kind = (
            "light_novel" if str(kind).casefold() in {"light_novel", "novel", "ln"} else str(kind).casefold()
        )
        resolved_group = str(activity_group_id or "")
        if not resolved_group and normalized_kind == "light_novel":
            paired_audio_id = int((payload or {}).get("paired_audio_id") or 0)
            resolved_group = self._ln_audio_group(int(local_id), paired_audio_id)
        now = time.time()
        session_id = self._active_session_for_media(
            media_ref.media_uuid,
            normalized_kind,
            now=now,
            measurement_method=measurement_method,
            activity_group_id=resolved_group,
        )
        results: list[ConsumptionAppendResult] = []
        remaining = active
        end = now
        while remaining > 0.001:
            chunk = min(15.0, remaining)
            start = end - chunk
            results.append(
                self.append_interval(
                    session_id=session_id,
                    kind=normalized_kind,
                    interval_start_utc=start,
                    interval_end_utc=end,
                    elapsed_monotonic_ms=chunk * 1000.0,
                    media=[
                        {
                            "media_uuid": media_ref.media_uuid,
                            "source_revision": media_ref.source_revision,
                            "role": "primary",
                            "locator_start": locator_start or {},
                            "locator_end": locator_end or {},
                        }
                    ],
                    payload={"measurement": measurement_method, **(payload or {})},
                )
            )
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
        return self.record_reader_activity(
            kind="audiobook",
            local_id=int(book_id),
            active_seconds=active_seconds,
            locator_start={"position_seconds": max(0.0, float(position_start or 0.0))},
            locator_end={"position_seconds": max(0.0, float(position_end or 0.0))},
            payload={
                "speed": max(0.1, float(speed or 1.0)),
                "seek_or_discontinuity": abs(float(position_end) - float(position_start))
                > max(10.0, active_seconds * max(1.0, float(speed or 1.0)) * 4.0),
            },
            activity_group_id=self.audiobook_activity_group(int(book_id)),
            measurement_method="audiobook_active_playback",
        )

    def audiobook_activity_group(self, book_id: int) -> str:
        """The LN<->audiobook group of a linked audiobook ("" when it is not linked)."""
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT ln_book_id FROM reading_audio_links WHERE audiobook_id=? "
                "ORDER BY updated_at DESC,ln_book_id LIMIT 1",
                (int(book_id),),
            ).fetchone()
        return self._ln_audio_group(int(row["ln_book_id"] or 0) if row is not None else 0, int(book_id))

    def light_novel_activity_group(self, ln_book_id: int) -> str:
        """The LN<->audiobook group of a light novel with a linked audiobook ("" otherwise)."""
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT audiobook_id FROM reading_audio_links WHERE ln_book_id=?", (int(ln_book_id),)
            ).fetchone()
        return self._ln_audio_group(int(ln_book_id), int(row["audiobook_id"] or 0) if row is not None else 0)

    def record_device_interval(
        self,
        *,
        device_id: str,
        device_name: str,
        event_id: str,
        kind: str,
        media_ref: ConsumptionMediaRef,
        start_utc: float,
        end_utc: float,
        active_seconds: float,
        locator_start: dict[str, Any] | None = None,
        locator_end: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        measurement_method: str = "device_observation",
        activity_group_id: str = "",
        timezone_iana: str = "",
        utc_offset: int | None = None,
        clock_skew_seconds: float = 0.0,
    ) -> str:
        """Store one interval measured on a paired device (the iPhone) at its own clock times.

        Returns "applied", or "duplicate" for a retry of the same event. The same
        event id with other content raises ConsumptionConflictError. Time spent on
        one work on the Mac and on the phone at once is counted once in reports,
        because reports take the union of overlapping intervals.

        A phone clock ahead of the Mac's never loses the time: the interval moves back by
        `clock_skew_seconds` (the device's measured lead), and one still ending more than
        5 minutes in the future moves back to end now. The shift is kept in the payload;
        retries match on the times the device sent.
        """
        start, end, active = float(start_utc), float(end_utc), float(active_seconds)
        now = time.time()
        sent_start, sent_end = start, end
        skew = float(clock_skew_seconds or 0.0)
        if not math.isfinite(skew) or skew < 0:
            skew = 0.0
        if (
            not all(math.isfinite(value) for value in (start, end, active))
            or start < 0
            or end < start
            or end - start > MAX_CHUNK_SECONDS + 0.001
            or active < 0
            or active > (end - start) + 2.5
        ):
            raise ConsumptionLedgerError("invalid device interval")
        if end - skew > now + 300.0:
            skew = end - now
        start, end = start - skew, end - skew
        if start < 0:
            raise ConsumptionLedgerError("invalid device interval")
        device = str(device_id or "").strip()
        identifier = str(event_id or "").strip()
        if not device or not identifier or len(identifier) > 160:
            raise ConsumptionLedgerError("device interval needs a device and an event id")
        normalized_kind = str(kind or "").strip().casefold()
        if normalized_kind not in {"anime", "light_novel", "manga", "audiobook"}:
            raise ConsumptionLedgerError("unsupported device interval kind")
        event_payload = {"measurement": str(measurement_method), **(payload or {})}
        stored_payload = (
            {**event_payload, "clock_skew_seconds": round(skew, 3)} if skew > 0 else event_payload
        )
        payload_json = _json(stored_payload)
        if len(payload_json.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ConsumptionLedgerError("consumption payload is too large")
        media_row = {
            "media_uuid": media_ref.media_uuid,
            "source_revision": media_ref.source_revision,
            "role": "primary",
            "locator_start": locator_start or {},
            "locator_end": locator_end or {},
        }
        automatic_zone, automatic_offset = _system_timezone_metadata(end)
        zone = str(timezone_iana or "") or automatic_zone
        offset = automatic_offset if utc_offset is None else int(utc_offset)
        content_hash = _sha(
            _json(
                {
                    "device": device,
                    "kind": normalized_kind,
                    "start": round(sent_start, 6),
                    "end": round(sent_end, 6),
                    "elapsed": round(active * 1000.0, 3),
                    "payload": event_payload,
                    "timezone": zone,
                    "utc_offset": offset,
                    "media": [media_row],
                }
            )
        )
        with self.database.connect() as conn:
            existing = conn.execute(
                "SELECT content_hash FROM consumption_events WHERE event_id=?", (identifier,)
            ).fetchone()
            if existing is not None:
                if str(existing["content_hash"]) != content_hash:
                    raise ConsumptionConflictError("event_id was retried with different content")
                return "duplicate"
            self._ensure_remote_device(conn, device, device_name)
            session = conn.execute(
                """
                SELECT s.session_id,s.started_at_utc,COALESCE(MAX(e.interval_end_utc),s.started_at_utc) AS last_at
                FROM consumption_sessions s LEFT JOIN consumption_events e ON e.session_id=s.session_id
                WHERE s.device_id=? AND s.kind=? AND s.primary_media_uuid=? AND s.status='active'
                GROUP BY s.session_id ORDER BY last_at DESC LIMIT 1
                """,
                (device, normalized_kind, media_ref.media_uuid),
            ).fetchone()
            if (
                session is not None
                and start - float(session["last_at"] or 0.0) <= 90.0
                and start >= float(session["started_at_utc"] or 0.0) - 90.0
            ):
                session_id = str(session["session_id"])
            else:
                if session is not None:
                    conn.execute(
                        "UPDATE consumption_sessions SET ended_at_utc=?,status='closed' WHERE session_id=?",
                        (float(session["last_at"] or start), str(session["session_id"])),
                    )
                session_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO consumption_sessions(
                        session_id,profile_id,device_id,activity_group_id,kind,primary_media_uuid,started_at_utc,
                        ended_at_utc,origin,status,measurement_method,policy_version,schema_version,metadata_json
                    ) VALUES(?,?,?,?,?,?,?,NULL,'automatic','active',?,'r7-v1',?,'{}')
                    """,
                    (
                        session_id,
                        "default",
                        device,
                        str(activity_group_id or "") or str(uuid.uuid4()),
                        normalized_kind,
                        media_ref.media_uuid,
                        start,
                        str(measurement_method),
                        LEDGER_SCHEMA_VERSION,
                    ),
                )
            seq = self._next_seq(conn, device)
            conn.execute(
                """
                INSERT INTO consumption_events(
                    event_id,device_id,device_seq,session_id,kind,interval_start_utc,
                    interval_end_utc,elapsed_monotonic_ms,received_at_utc,timezone_iana,
                    utc_offset,payload_json,content_hash,schema_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    identifier,
                    device,
                    seq,
                    session_id,
                    normalized_kind,
                    start,
                    end,
                    active * 1000.0,
                    now,
                    zone,
                    offset,
                    payload_json,
                    content_hash,
                    LEDGER_SCHEMA_VERSION,
                ),
            )
            conn.execute(
                """
                INSERT INTO consumption_event_media(
                    event_id,media_uuid,source_revision,role,locator_start_json,locator_end_json
                ) VALUES(?,?,?,?,?,?)
                """,
                (
                    identifier,
                    media_ref.media_uuid,
                    media_ref.source_revision,
                    "primary",
                    _json(locator_start or {}),
                    _json(locator_end or {}),
                ),
            )
        return "applied"

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
            results.append(
                self.append_interval(
                    session_id=str(session_id),
                    kind="visual_novel",
                    interval_start_utc=start,
                    interval_end_utc=end,
                    elapsed_monotonic_ms=chunk * 1000.0,
                    media=[
                        {
                            "media_uuid": media_uuid,
                            "source_revision": str(media["source_revision"] if media is not None else ""),
                            "role": "primary",
                        }
                    ],
                    payload={
                        "measurement": "capture_runtime_estimated",
                        "capture_count": max(0, int(capture_count or 0)),
                        "focus_verified": False,
                    },
                )
            )
            end = start
            remaining -= chunk
        return list(reversed(results))

    def _delete_history_conn(self, conn: Any, *, new_epoch: int, reset_at: float) -> None:
        conn.execute("DELETE FROM consumption_sync_outbox")
        conn.execute("DELETE FROM consumption_sync_receipts")
        conn.execute("DELETE FROM vn_capture_sessions")
        conn.execute("DELETE FROM vn_capture_lines")
        conn.execute("DELETE FROM consumption_event_media")
        conn.execute("DELETE FROM consumption_events")
        conn.execute("DELETE FROM consumption_sessions")
        conn.execute("DELETE FROM consumption_corrections")
        conn.execute("DELETE FROM consumption_manual_entries")
        conn.execute("DELETE FROM consumption_daily_rollups")
        # Statistics volume summaries (consumption_statistics.VOLUME_ROLLUP_PREFIX).
        conn.execute("DELETE FROM state WHERE substr(key,1,26)='consumption_volume_rollup:'")
        conn.execute(
            """INSERT INTO consumption_history_state(profile_id,reset_epoch,reset_at_utc) VALUES('default',?,?)
               ON CONFLICT(profile_id) DO UPDATE SET reset_epoch=excluded.reset_epoch,reset_at_utc=excluded.reset_at_utc""",
            (int(new_epoch), float(reset_at)),
        )

    def delete_history(self) -> dict[str, Any]:
        now = time.time()
        with self.database.connect() as conn:
            epoch = self._history_epoch_conn(conn) + 1
            self._delete_history_conn(conn, new_epoch=epoch, reset_at=now)
        return {"ok": True, "reset_epoch": epoch, "reset_at_utc": now}

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
