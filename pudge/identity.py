from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .database import Database


@dataclass(slots=True)
class MediaIdentity:
    media_id: int | None
    media_episode: int | None
    release_episode: int | None = None
    video_path: Path | None = None
    torrent_hash: str = ""
    source: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)
    locked: bool = False

    @property
    def canonical_id(self) -> str:
        if self.media_id is not None and self.media_episode is not None:
            return f"anime:{int(self.media_id)}:episode:{int(self.media_episode)}"
        raw = str(self.video_path or self.torrent_hash or self.provenance)
        return "local:" + hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()

    @property
    def fingerprint(self) -> str:
        path = self.video_path
        if path is None:
            return ""
        try:
            stat = path.expanduser().stat()
        except OSError:
            return ""
        raw = f"{path.expanduser().resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
        return hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()


class IdentityResolver:
    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, identity: MediaIdentity) -> dict[str, Any]:
        now = time.time()
        canonical_id = identity.canonical_id
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO media_identity_ledger(
                    canonical_id,media_id,media_episode,release_episode,video_path,
                    fingerprint,torrent_hash,source,provenance_json,locked,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(canonical_id) DO UPDATE SET
                    media_id=CASE WHEN media_identity_ledger.locked THEN media_identity_ledger.media_id ELSE COALESCE(excluded.media_id,media_identity_ledger.media_id) END,
                    media_episode=CASE WHEN media_identity_ledger.locked THEN media_identity_ledger.media_episode ELSE COALESCE(excluded.media_episode,media_identity_ledger.media_episode) END,
                    release_episode=CASE WHEN media_identity_ledger.locked THEN media_identity_ledger.release_episode ELSE COALESCE(excluded.release_episode,media_identity_ledger.release_episode) END,
                    video_path=CASE WHEN media_identity_ledger.locked THEN media_identity_ledger.video_path ELSE COALESCE(NULLIF(excluded.video_path,''),media_identity_ledger.video_path) END,
                    fingerprint=COALESCE(NULLIF(excluded.fingerprint,''),media_identity_ledger.fingerprint),
                    torrent_hash=COALESCE(NULLIF(excluded.torrent_hash,''),media_identity_ledger.torrent_hash),
                    source=COALESCE(NULLIF(excluded.source,''),media_identity_ledger.source),
                    provenance_json=excluded.provenance_json,
                    locked=MAX(media_identity_ledger.locked,excluded.locked),
                    updated_at=excluded.updated_at
                """,
                (
                    canonical_id,
                    identity.media_id,
                    identity.media_episode,
                    identity.release_episode,
                    str(identity.video_path or ""),
                    identity.fingerprint,
                    str(identity.torrent_hash or "").casefold(),
                    str(identity.source or ""),
                    json.dumps(identity.provenance, ensure_ascii=False, sort_keys=True),
                    int(identity.locked),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM media_identity_ledger WHERE canonical_id=?", (canonical_id,)
            ).fetchone()
        return dict(row) if row is not None else asdict(identity)

    def lock(self, canonical_id: str, locked: bool = True) -> bool:
        with self.database.connect() as conn:
            changed = conn.execute(
                "UPDATE media_identity_ledger SET locked=?,updated_at=? WHERE canonical_id=?",
                (int(locked), time.time(), str(canonical_id)),
            ).rowcount
        return changed == 1

    def lookup(self, *, video_path: Path | None = None, torrent_hash: str = "") -> dict[str, Any] | None:
        path_value = str(video_path) if video_path is not None else ""
        hash_value = str(torrent_hash or "").strip().casefold()
        if not path_value and not hash_value:
            return None
        with self.database.connect() as conn:
            # Exact path is the strongest local identity and must never be
            # diluted by an empty torrent hash matching unrelated rows.
            if path_value:
                row = conn.execute(
                    "SELECT * FROM media_identity_ledger WHERE video_path=? "
                    "ORDER BY locked DESC,updated_at DESC LIMIT 1",
                    (path_value,),
                ).fetchone()
                if row is not None:
                    return dict(row)
            if not hash_value:
                return None
            rows = conn.execute(
                "SELECT * FROM media_identity_ledger WHERE torrent_hash=? "
                "ORDER BY locked DESC,updated_at DESC LIMIT 2",
                (hash_value,),
            ).fetchall()
        if len(rows) != 1:
            # A hash collision/duplicate ledger entry is ambiguous. Do not pick
            # whichever happened to be updated last.
            return None
        return dict(rows[0])
