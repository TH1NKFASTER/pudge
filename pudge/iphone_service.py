"""Durable audiobook protocol for the native iPhone client (separate from v1)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .database import Database


def now_ms() -> int:
    return int(time.time() * 1000)


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise ValueError(f"Invalid {name}")
    return value


def identifier(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid identifier")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid identifier") from exc


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("protocolMajor") != 2:
        raise ValueError("Unsupported protocol")
    for key in ("assetId", "editionId"):
        identifier(value.get(key))
    if not isinstance(value.get("title"), str) or not 1 <= len(value["title"]) <= 1024:
        raise ValueError("Invalid title")
    revision = value.get("timelineRevision")
    if not isinstance(revision, str) or not 1 <= len(revision) <= 128:
        raise ValueError("Invalid timeline revision")
    total = integer(value.get("durationMs"), "duration", 1)
    parts = value.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 10000:
        raise ValueError("Invalid audio parts")
    seen: set[str] = set()
    offset = 0
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("Invalid part")
        for key in ("partId", "resourceId"):
            part_id = identifier(part.get(key))
            if key == "partId" and part_id in seen:
                raise ValueError("Duplicate part")
        seen.add(part["partId"])
        if integer(part.get("offsetMs"), "offset") != offset:
            raise ValueError("Discontinuous timeline")
        offset += integer(part.get("durationMs"), "part duration", 1)
        integer(part.get("sizeBytes"), "size", 1)
        sha = part.get("sha256")
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("Invalid SHA-256")
        name = part.get("fileName")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 240
            or any(c in name for c in "/\\\x00")
            or name in {".", ".."}
        ):
            raise ValueError("Invalid file name")
        if Path(name).suffix.lower() not in {
            ".mp3",
            ".m4a",
            ".m4b",
            ".aac",
            ".wav",
            ".flac",
            ".opus",
            ".ogg",
        }:
            raise ValueError("Unsupported audio file")
    if offset != total:
        raise ValueError("Duration does not match timeline")
    chapters = value.get("chapters", [])
    if not isinstance(chapters, list) or len(chapters) > 10000:
        raise ValueError("Invalid chapters")
    for chapter in chapters:
        if not isinstance(chapter, dict) or not isinstance(chapter.get("title"), str):
            raise ValueError("Invalid chapter")
        start = integer(chapter.get("startMs"), "chapter start")
        end = integer(chapter.get("endMs"), "chapter end")
        if not start <= end <= total:
            raise ValueError("Invalid chapter range")
    cover = value.get("cover")
    if cover is not None:
        if not isinstance(cover, dict):
            raise ValueError("Invalid cover")
        identifier(cover.get("resourceId"))
        integer(cover.get("sizeBytes"), "cover size", 1)
        sha = cover.get("sha256")
        name = cover.get("fileName")
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("Invalid cover hash")
        if (
            not isinstance(name, str)
            or len(name) > 240
            or any(c in name for c in "/\\\x00")
            or Path(name).suffix.lower() not in {".jpg", ".jpeg", ".png"}
        ):
            raise ValueError("Invalid cover filename")
        if cover["sizeBytes"] > 10 * 1024 * 1024 or any(
            p["resourceId"] == cover["resourceId"] for p in parts
        ):
            raise ValueError("Invalid cover resource")
    read_along = value.get("readAlong")
    if read_along is not None:
        if not isinstance(read_along, dict):
            raise ValueError("Invalid read-along")
        identifier(read_along.get("resourceId"))
        integer(read_along.get("sizeBytes"), "read-along size", 1)
        sha = read_along.get("sha256")
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("Invalid read-along hash")
        if read_along.get("fileName") != "readalong.json" or read_along["sizeBytes"] > 50 * 1024 * 1024:
            raise ValueError("Invalid read-along file")
        taken = {p["resourceId"] for p in parts} | ({cover["resourceId"]} if cover else set())
        if read_along["resourceId"] in taken:
            raise ValueError("Invalid read-along resource")
    if value.get("lightNovelId") is not None:
        integer(value.get("lightNovelId"), "light novel", 1)
    integer(sum(p["sizeBytes"] for p in resources(value)), "total size", 1)
    return value


INVITATION_MS = 600_000


class PairingRefused(ValueError):
    """A pairing request the phone shows with this reason (expired, used, declined)."""


def resources(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    extras = [manifest[key] for key in ("cover", "readAlong") if manifest.get(key)]
    return manifest["parts"] + extras


SCHEMA = """
CREATE TABLE IF NOT EXISTS iphone_devices(id TEXT PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS iphone_invites(hash TEXT PRIMARY KEY,expires INTEGER NOT NULL,device TEXT,attempt TEXT,cancelled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS iphone_assets(id TEXT PRIMARY KEY,book_id INTEGER,manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS iphone_versions(asset TEXT NOT NULL,edition TEXT NOT NULL,manifest TEXT NOT NULL,PRIMARY KEY(asset,edition));
CREATE TABLE IF NOT EXISTS iphone_resources(id TEXT PRIMARY KEY,sha TEXT NOT NULL,size INTEGER NOT NULL,path TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS iphone_archives(id TEXT PRIMARY KEY,device TEXT NOT NULL,manifest TEXT NOT NULL,receipt TEXT);
CREATE TABLE IF NOT EXISTS iphone_retention(resource TEXT NOT NULL,device TEXT NOT NULL,edition TEXT NOT NULL,receipt TEXT NOT NULL,PRIMARY KEY(resource,device,edition));
CREATE TABLE IF NOT EXISTS iphone_checkpoints(id TEXT PRIMARY KEY,device TEXT NOT NULL,body TEXT NOT NULL,receipt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS iphone_progress(asset TEXT PRIMARY KEY,revision INTEGER NOT NULL,selected TEXT,conflicts TEXT NOT NULL DEFAULT '[]',finished INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS iphone_bookmarks(id TEXT PRIMARY KEY,asset TEXT NOT NULL,edition TEXT NOT NULL,body TEXT NOT NULL,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS iphone_handoffs(id TEXT PRIMARY KEY,body TEXT NOT NULL,result TEXT);
CREATE TABLE IF NOT EXISTS iphone_resolution_fences(asset TEXT PRIMARY KEY,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS iphone_bookmark_links(id TEXT PRIMARY KEY,desktop_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS iphone_bookmark_events(id TEXT PRIMARY KEY,device TEXT NOT NULL,body TEXT NOT NULL,receipt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS iphone_mac_starts(operation TEXT PRIMARY KEY,started INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS iphone_hashes(path TEXT PRIMARY KEY,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS iphone_prepared(source TEXT PRIMARY KEY,size INTEGER NOT NULL,mtime_ns INTEGER NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS iphone_listen_marks(device TEXT NOT NULL,session TEXT NOT NULL,sequence INTEGER NOT NULL,position INTEGER NOT NULL,observed INTEGER NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(device,session));
"""


# After these the phone is not playing until its next checkpoint.
_NOT_PLAYING_AFTER = {"pause", "stop", "finish", "reset", "interruption", "handoff"}


def listening_interval(
    previous: Any, position_ms: int, observed_ms: int
) -> tuple[float, float, float, float] | None:
    """Listening between two checkpoints of one phone session: (start, end, from, to) in seconds.

    The phone saves a checkpoint every 5 s while it plays. A gap counts only
    when playback was running before it and the position moved like playback
    (0.4-3.2x the clock); a pause, a seek or a sleeping phone gives None.
    """
    if str(previous["reason"]) in _NOT_PLAYING_AFTER:
        return None
    wall = (int(observed_ms) - int(previous["observed"])) / 1000.0
    moved = (int(position_ms) - int(previous["position"])) / 1000.0
    if not 0.5 <= wall <= 20.0:
        return None
    if moved <= 0 or moved < wall * 0.4 or moved > wall * 3.2 + 1.5:
        return None
    return (
        int(previous["observed"]) / 1000.0,
        int(observed_ms) / 1000.0,
        int(previous["position"]) / 1000.0,
        int(position_ms) / 1000.0,
    )


class IPhoneService:
    def __init__(self, database: Database, root: Path, *, audiobooks: Any = None) -> None:
        self.db = database
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.audiobooks = audiobooks
        self.mac_sessions: dict[tuple[str, str], dict[str, Any]] = {}
        self._catalog_lock = threading.Lock()
        self._catalog_worker: threading.Thread | None = None
        self._catalog_errors: list[dict[str, Any]] = []
        self._transfer_locks: dict[str, threading.Lock] = {}
        # Mac sessions paused by a Mac -> iPhone handoff: their later saves
        # (Stop of the paused player) must never open a new branch.
        self.handed_off_sessions: set[tuple[str, str]] = set()
        # iPhone/iPad → iPhone/iPad handoffs waiting for the playing device (operation → start ms).
        self.device_handoffs: dict[str, int] = {}
        self.cover_dirs: list[Path] = []
        # The desktop app (WebAppApi): reading, study and statistics endpoints.
        self.api: Any = None
        from .iphone_media import IPhoneMedia

        self.media = IPhoneMedia(self)
        for folder in ("objects", "staging"):
            (self.root / folder).mkdir(exist_ok=True, mode=0o700)
        secret_path = self.root / "server-secret"
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(secrets.token_bytes(32))
                handle.flush()
                os.fsync(handle.fileno())
        self.secret = secret_path.read_bytes()
        if len(self.secret) != 32:
            raise ValueError("Invalid server identity")
        self.server_id = str(uuid.uuid5(uuid.NAMESPACE_URL, hashlib.sha256(self.secret).hexdigest()))
        self.ensure_schema()
        # Revoked during this run: streams in progress stop at their next chunk.
        self.revoked_now: set[str] = set()
        self._seen: dict[str, int] = {}
        # The running HTTPS server (iphone_http): revoking closes the device's connections.
        self.http_server: Any = None
        if audiobooks is not None:
            audiobooks.mobile_progress = self

    def ensure_schema(self) -> None:
        """Tables and later columns (also for a database restored from an older backup)."""
        with self.db.connect() as conn:
            conn.executescript(SCHEMA)
            for table, column, kind in (
                ("iphone_devices", "paired_at", "INTEGER"),
                ("iphone_devices", "last_seen", "INTEGER"),
                ("iphone_invites", "name", "TEXT"),
                ("iphone_invites", "token_hash", "TEXT"),
                # NULL for invitations used before confirmation existed (approved then).
                ("iphone_invites", "approval", "TEXT"),
            ):
                if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def invite(self, base_url: str | list[str], certificate_sha256: str) -> dict[str, Any]:
        """A pairing invitation for the QR: valid 10 minutes, used by one device after the Mac allows it.

        `base_url` may list several addresses (one per LAN interface); the phone tries them in order.
        """
        urls = [base_url] if isinstance(base_url, str) else [str(url) for url in base_url]
        if not urls or not all(url.startswith("https://") for url in urls):
            raise ValueError("HTTPS required")
        invitation = secrets.token_urlsafe(32)
        expires = now_ms() + INVITATION_MS
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO iphone_invites(hash,expires) VALUES(?,?)",
                (hashlib.sha256(invitation.encode()).hexdigest(), expires),
            )
        return {
            "protocolMajor": 2,
            "serverId": self.server_id,
            "serverName": "Pudge Mac",
            "baseURL": urls[0],
            "baseURLs": urls,
            "certificateSHA256": certificate_sha256,
            "invitation": invitation,
            "expiresAt": expires,
        }

    def cancel_invitation(self, invitation: str) -> None:
        # A device still waiting for Allow/Deny is refused with the code it shows.
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE iphone_invites SET cancelled=1 WHERE hash=? AND (device IS NULL OR approval='pending')",
                (hashlib.sha256(invitation.encode()).hexdigest(),),
            )

    def confirmation_code(self, invitation_hash: str, device: str, attempt: str) -> str:
        """Six digits shown on the phone and in the Mac's Allow/Deny prompt (same for retries)."""
        value = hmac.new(
            self.secret,
            ("confirm:" + invitation_hash + ":" + device + ":" + attempt).encode(),
            hashlib.sha256,
        ).digest()
        return "%06d" % (int.from_bytes(value[:8], "big") % 1_000_000)

    def pair(self, request: dict[str, Any]) -> dict[str, Any]:
        """Phone side of pairing. The first answer is "pending" with the confirmation code; the
        phone repeats the same request until the Mac's user allows it (then the token) or denies it."""
        device = identifier(request.get("deviceId"))
        attempt = identifier(request.get("attemptId"))
        invitation = request.get("invitation")
        if not isinstance(invitation, str) or not 20 <= len(invitation) <= 200:
            raise ValueError("Invalid invitation")
        name = request.get("name", "iPhone")
        if not isinstance(name, str) or not 1 <= len(name) <= 120:
            raise ValueError("Invalid device name")
        invitation_hash = hashlib.sha256(invitation.encode()).hexdigest()
        token = hmac.new(
            self.secret, (invitation + ":" + device + ":" + attempt).encode(), hashlib.sha256
        ).hexdigest()
        code = self.confirmation_code(invitation_hash, device, attempt)
        pending = {
            "status": "pending",
            "confirmationCode": code,
            "deviceId": device,
            "serverId": self.server_id,
        }
        with self.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM iphone_invites WHERE hash=?", (invitation_hash,)).fetchone()
            if row is None:
                raise PairingRefused("Unknown pairing code. Show a new code on the Mac.")
            previous = conn.execute("SELECT * FROM iphone_devices WHERE id=?", (device,)).fetchone()
            if row["device"]:
                if row["device"] != device or row["attempt"] != attempt:
                    raise PairingRefused("This pairing code was already used. Show a new code on the Mac.")
                if row["approval"] == "denied":
                    raise PairingRefused("Pairing was declined on the Mac.")
                if row["approval"] == "pending":
                    if row["cancelled"] or row["expires"] < now_ms():
                        raise PairingRefused("The pairing code expired. Show a new code on the Mac.")
                    return pending
                if previous is None or previous["revoked"]:
                    raise PermissionError("Device revoked")
            else:
                if row["cancelled"] or row["expires"] < now_ms():
                    raise PairingRefused("The pairing code expired. Show a new code on the Mac.")
                conn.execute(
                    "UPDATE iphone_invites SET device=?,attempt=?,name=?,token_hash=?,approval='pending' WHERE hash=?",
                    (device, attempt, name, hashlib.sha256(token.encode()).hexdigest(), invitation_hash),
                )
                return pending
        return {"status": "approved", "deviceId": device, "serverId": self.server_id, "token": token}

    def pending_pairings(self) -> list[dict[str, Any]]:
        """Devices waiting for Allow/Deny on the Mac (with the code they show)."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM iphone_invites WHERE approval='pending' AND cancelled=0 AND expires>=?",
                (now_ms(),),
            ).fetchall()
        return [
            {
                "deviceId": row["device"],
                "name": row["name"] or "iPhone/iPad",
                "code": self.confirmation_code(row["hash"], row["device"], row["attempt"]),
                "expiresAt": row["expires"],
            }
            for row in rows
        ]

    def approve_pairing(self, device: str) -> bool:
        """Allow on the Mac: the waiting device gets its token on its next request."""
        device = identifier(device)
        with self.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM iphone_invites WHERE device=? AND approval='pending' AND cancelled=0 AND expires>=?",
                (device, now_ms()),
            ).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE iphone_invites SET approval='approved' WHERE hash=?", (row["hash"],))
            conn.execute(
                "INSERT INTO iphone_devices(id,name,token_hash,revoked,paired_at) VALUES(?,?,?,0,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,token_hash=excluded.token_hash,revoked=0,paired_at=excluded.paired_at",
                (device, row["name"] or "iPhone", row["token_hash"], now_ms()),
            )
        self.revoked_now.discard(device)
        return True

    def deny_pairing(self, device: str) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE iphone_invites SET approval='denied' WHERE device=? AND approval='pending'",
                (identifier(device),),
            )

    def authenticate(self, token: str) -> str:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM iphone_devices WHERE token_hash=? AND revoked=0",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            if row is None:
                raise PermissionError("Device access revoked or invalid")
            device = str(row["id"])
            seen = now_ms()
            # "Last seen" in Settings: written at most once a minute per device.
            if seen - self._seen.get(device, 0) >= 60_000:
                self._seen[device] = seen
                conn.execute("UPDATE iphone_devices SET last_seen=? WHERE id=?", (seen, device))
        return device

    def still_authorized(self, device: str) -> bool:
        return device not in self.revoked_now

    def devices(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [
                dict(row)
                for row in conn.execute("SELECT id,name,revoked,paired_at,last_seen FROM iphone_devices")
            ]

    def revoke(self, device: str) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE iphone_devices SET revoked=1 WHERE id=?", (device,))
        self.revoked_now.add(str(device))
        server = self.http_server
        if server is not None:
            # Downloads and uploads in progress stop now, not when they finish.
            server.close_requests(str(device))

    def register_manifest(self, manifest: dict[str, Any], *, book_id: int | None = None) -> None:
        validate_manifest(manifest)
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO iphone_assets(id,book_id,manifest) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET manifest=excluded.manifest,book_id=COALESCE(excluded.book_id,iphone_assets.book_id)",
                (manifest["assetId"], book_id, encoded(manifest)),
            )
            conn.execute(
                "INSERT INTO iphone_versions(asset,edition,manifest) VALUES(?,?,?) ON CONFLICT(asset,edition) DO UPDATE SET manifest=excluded.manifest",
                (manifest["assetId"], manifest["editionId"], encoded(manifest)),
            )

    def manifest(self, asset: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT manifest FROM iphone_assets WHERE id=?", (asset,)).fetchone()
        if row is None:
            raise KeyError("Unknown asset")
        return json.loads(row["manifest"])

    def begin_archive(self, device: str, manifest: dict[str, Any]) -> dict[str, Any]:
        validate_manifest(manifest)
        transfer = str(uuid.uuid5(uuid.NAMESPACE_URL, device + manifest["assetId"] + manifest["editionId"]))
        with self.lock, self.db.connect() as conn:
            old = conn.execute("SELECT * FROM iphone_archives WHERE id=?", (transfer,)).fetchone()
            if old is not None and old["manifest"] != encoded(manifest):
                raise ValueError("Archive identity reused with different manifest")
            conn.execute(
                "INSERT OR IGNORE INTO iphone_archives(id,device,manifest) VALUES(?,?,?)",
                (transfer, device, encoded(manifest)),
            )
        folder = self.root / "staging" / transfer
        folder.mkdir(exist_ok=True, mode=0o700)
        offsets = {}
        for part in resources(manifest):
            stage = folder / part["resourceId"]
            final = self.root / "objects" / (part["sha256"] + Path(part["fileName"]).suffix.lower())
            offsets[part["resourceId"]] = (
                final.stat().st_size
                if final.exists() and self.file_sha256(final) == part["sha256"]
                else (stage.stat().st_size if stage.exists() else 0)
            )
        return {"transferId": transfer, "offsets": offsets}

    def _archive(self, device: str, transfer: str) -> dict[str, Any]:
        identifier(transfer)
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM iphone_archives WHERE id=? AND device=?", (transfer, device)
            ).fetchone()
        if row is None:
            raise PermissionError("Unknown archive")
        return dict(row)

    def archive_chunk(
        self, device: str, transfer: str, resource: str, offset: int, data: bytes
    ) -> dict[str, Any]:
        identifier(resource)
        integer(offset, "offset")
        if not 1 <= len(data) <= 4 * 1024 * 1024:
            raise ValueError("Invalid chunk size")
        # Disk writes/fsync of one transfer must not stall the Mac player,
        # whose position saves take the service lock.
        with self._transfer_lock(transfer):
            archive = self._archive(device, transfer)
            manifest = json.loads(archive["manifest"])
            part = next((p for p in resources(manifest) if p["resourceId"] == resource), None)
            if part is None or offset + len(data) > part["sizeBytes"]:
                raise ValueError("Chunk outside resource")
            path = self.root / "staging" / transfer / resource
            path.parent.mkdir(exist_ok=True, mode=0o700)
            if archive["receipt"] is not None:
                return {"offset": part["sizeBytes"]}
            with path.open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                current = handle.tell()
                if offset < current:
                    handle.seek(offset)
                    if handle.read(len(data)) != data:
                        raise ValueError("Conflicting repeated chunk")
                    return {"offset": current}
                if offset != current:
                    raise ValueError("Unexpected chunk offset")
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            return {"offset": offset + len(data)}

    def _transfer_lock(self, transfer: str) -> threading.Lock:
        with self.lock:
            return self._transfer_locks.setdefault(str(transfer), threading.Lock())

    def commit_archive(self, device: str, transfer: str) -> dict[str, Any]:
        with self._transfer_lock(transfer):
            archive = self._archive(device, transfer)
            if archive["receipt"] is not None:
                return json.loads(archive["receipt"])
            manifest = json.loads(archive["manifest"])
            ready = []
            # Hash (gigabytes) outside the service lock; the transfer lock
            # keeps chunks of this archive from changing the staged bytes.
            for part in resources(manifest):
                stage = self.root / "staging" / transfer / part["resourceId"]
                final = self.root / "objects" / (part["sha256"] + Path(part["fileName"]).suffix.lower())
                candidate = final if final.exists() else stage
                if (
                    not candidate.is_file()
                    or candidate.stat().st_size != part["sizeBytes"]
                    or self.file_sha256(candidate) != part["sha256"]
                ):
                    raise ValueError("Incomplete archive or SHA-256 mismatch")
                ready.append((part, stage, final))
            return self._publish_archive(device, transfer, manifest, ready)

    def _publish_archive(
        self, device: str, transfer: str, manifest: dict[str, Any], ready: list
    ) -> dict[str, Any]:
        with self.lock:
            for part, stage, final in ready:
                if not final.exists():
                    os.replace(stage, final)
                with final.open("rb") as handle:
                    os.fsync(handle.fileno())
            self._sync_directory(self.root / "objects")
            receipt = {
                "receiptId": str(uuid.uuid4()),
                "assetId": manifest["assetId"],
                "editionId": manifest["editionId"],
                "verifiedAt": now_ms(),
                "retained": True,
            }
            with self.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                for part, _, final in ready:
                    old = conn.execute(
                        "SELECT sha,size FROM iphone_resources WHERE id=?", (part["resourceId"],)
                    ).fetchone()
                    if old is not None and (old["sha"] != part["sha256"] or old["size"] != part["sizeBytes"]):
                        raise ValueError("Resource ID reused for different bytes")
                    conn.execute(
                        "INSERT OR IGNORE INTO iphone_resources(id,sha,size,path) VALUES(?,?,?,?)",
                        (part["resourceId"], part["sha256"], part["sizeBytes"], str(final)),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO iphone_retention(resource,device,edition,receipt) VALUES(?,?,?,?)",
                        (part["resourceId"], device, manifest["editionId"], receipt["receiptId"]),
                    )
                current = conn.execute(
                    "SELECT * FROM iphone_assets WHERE id=?", (manifest["assetId"],)
                ).fetchone()
                if current is None or (
                    current["book_id"] is None
                    and json.loads(current["manifest"])["editionId"] == manifest["editionId"]
                ):
                    folder = self.root / "books" / manifest["assetId"] / manifest["editionId"]
                    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                    cursor = conn.execute(
                        "INSERT INTO audiobooks(path,title,duration,created_at,updated_at) VALUES(?,?,?,?,?)",
                        (
                            str(folder),
                            manifest["title"],
                            manifest["durationMs"] / 1000,
                            time.time(),
                            time.time(),
                        ),
                    )
                    book_id = cursor.lastrowid
                    for index, part in enumerate(manifest["parts"]):
                        final = (
                            self.root / "objects" / (part["sha256"] + Path(part["fileName"]).suffix.lower())
                        )
                        conn.execute(
                            "INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end) VALUES(?,?,?,?,?,?,?)",
                            (
                                book_id,
                                index,
                                str(final),
                                part["fileName"],
                                part["durationMs"] / 1000,
                                part["offsetMs"] / 1000,
                                (part["offsetMs"] + part["durationMs"]) / 1000,
                            ),
                        )
                    for index, chapter in enumerate(manifest.get("chapters", [])):
                        conn.execute(
                            "INSERT INTO audiobook_chapters(book_id,chapter_index,title,start,end) VALUES(?,?,?,?,?)",
                            (
                                book_id,
                                index,
                                chapter["title"],
                                chapter["startMs"] / 1000,
                                chapter["endMs"] / 1000,
                            ),
                        )
                    conn.execute(
                        "INSERT INTO iphone_assets(id,book_id,manifest) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET book_id=excluded.book_id",
                        (manifest["assetId"], book_id, encoded(manifest)),
                    )
                # Archiving an older phone edition never replaces the current Mac edition.

                conn.execute(
                    "INSERT INTO iphone_versions(asset,edition,manifest) VALUES(?,?,?) ON CONFLICT(asset,edition) DO UPDATE SET manifest=excluded.manifest",
                    (manifest["assetId"], manifest["editionId"], encoded(manifest)),
                )
                conn.execute("UPDATE iphone_archives SET receipt=? WHERE id=?", (encoded(receipt), transfer))
            return receipt

    @staticmethod
    def _sync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def resource(self, resource: str, sha: str) -> Path:
        identifier(resource)
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM iphone_resources WHERE id=?", (resource,)).fetchone()
        if row is None or row["sha"] != sha:
            raise ValueError("Resource revision unavailable")
        path = Path(row["path"])
        if not path.is_file() or path.stat().st_size != row["size"] or self.file_sha256(path) != sha:
            raise ValueError("Resource changed or missing")
        return path

    def file_sha256(self, path: Path) -> str:
        """SHA-256 of a file, re-hashed only when its size or mtime changed.

        Range requests and catalog refreshes would otherwise hash whole
        audiobooks (gigabytes) on every call.
        """
        path = Path(path)
        stat = path.stat()
        key = str(path)
        with self.db.connect() as conn:
            row = conn.execute("SELECT size,mtime_ns,sha FROM iphone_hashes WHERE path=?", (key,)).fetchone()
        if row is not None and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
            return str(row["sha"])
        sha = digest(path)
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise ValueError("File changed while hashing")
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO iphone_hashes(path,size,mtime_ns,sha) VALUES(?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,sha=excluded.sha",
                (key, stat.st_size, stat.st_mtime_ns, sha),
            )
        return sha

    def _prepared_audio(self, source: Path) -> dict[str, Any]:
        from .iphone_audio import prepare_audio

        stat = source.stat()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT size,mtime_ns,result FROM iphone_prepared WHERE source=?", (str(source),)
            ).fetchone()
        if row is not None and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
            cached = json.loads(row["result"])
            target = Path(cached["path"])
            if target.is_file() and target.stat().st_size == cached["sizeBytes"]:
                return {**cached, "path": target}
        result = prepare_audio(
            source,
            self.root / "prepared",
            ffprobe=getattr(self.audiobooks, "ffprobe", "ffprobe"),
            ffmpeg=getattr(self.audiobooks, "ffmpeg", "ffmpeg"),
            original_sha=self.file_sha256(source),
        )
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO iphone_prepared(source,size,mtime_ns,result) VALUES(?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,result=excluded.result",
                (
                    str(source),
                    stat.st_size,
                    stat.st_mtime_ns,
                    encoded({**result, "path": str(result["path"])}),
                ),
            )
        return result

    def verify_archive(self, device: str, resource: str, edition: str) -> dict[str, Any]:
        identifier(edition)
        with self.lock, self.db.connect() as conn:
            row = conn.execute("SELECT * FROM iphone_resources WHERE id=?", (resource,)).fetchone()
            versions = conn.execute(
                "SELECT manifest FROM iphone_versions WHERE edition=?", (edition,)
            ).fetchall()
            part = next(
                (
                    p
                    for v in versions
                    for p in resources(json.loads(v["manifest"]))
                    if p["resourceId"] == resource
                ),
                None,
            )
            if row is None or part is None:
                raise ValueError("No verified archive for this edition")
            self.resource(resource, part["sha256"])
            retention = conn.execute(
                "SELECT receipt FROM iphone_retention WHERE resource=? AND device=? AND edition=?",
                (resource, device, edition),
            ).fetchone()
            receipt = str(retention["receipt"]) if retention else str(uuid.uuid4())
            conn.execute(
                "INSERT OR IGNORE INTO iphone_retention(resource,device,edition,receipt) VALUES(?,?,?,?)",
                (resource, device, edition, receipt),
            )
            return {
                "receiptId": receipt,
                "resourceId": resource,
                "editionId": edition,
                "sha256": row["sha"],
                "sizeBytes": row["size"],
                "verifiedAt": now_ms(),
                "retained": True,
            }

    def progress(self, asset: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM iphone_progress WHERE asset=?", (asset,)).fetchone()
        if row is None:
            return {"revision": 0, "selected": None, "conflicts": [], "finished": False}
        return {
            "revision": row["revision"],
            "selected": json.loads(row["selected"]) if row["selected"] else None,
            "conflicts": json.loads(row["conflicts"]),
            "finished": bool(row["finished"]),
        }

    def progress_view(self, asset: str) -> dict[str, Any]:
        """Progress for the phone, plus which device is playing this book now."""
        state = self.progress(asset)
        selected = state["selected"]
        playing = None
        if selected and selected.get("deviceId") == "mac":
            book = None
            with self.db.connect() as conn:
                row = conn.execute("SELECT book_id FROM iphone_assets WHERE id=?", (asset,)).fetchone()
                book = row["book_id"] if row else None
            audio = self.audiobooks
            try:
                if (
                    audio is not None
                    and book is not None
                    and audio.is_playing(int(book))
                    and not audio.is_paused(int(book))
                ):
                    playing = "mac"
            except Exception:  # noqa: BLE001 - liveness is advisory only
                playing = None
        elif self.playing_elsewhere(state, "mac"):
            playing = selected["deviceId"]
        # Names of the devices behind the positions (several iPhones and iPads can be paired).
        devices = {
            item["deviceId"]
            for item in [selected, *state["conflicts"]]
            if item and item.get("deviceId") not in {None, "", "mac"}
        }
        names = {device: self.device_name(device) for device in sorted(devices)}
        return {
            **state,
            "playingOn": playing,
            "playingOnName": "Mac" if playing == "mac" else names.get(playing) if playing else None,
            "deviceNames": names,
        }

    def record_checkpoint(self, device: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
        listened: list[dict[str, Any]] = []
        receipt = self._record_checkpoint(device, checkpoint, listened)
        for item in listened:
            self._record_listening(device, item)
        return receipt

    def device_name(self, device: str) -> str:
        with self.db.connect() as conn:
            row = conn.execute("SELECT name FROM iphone_devices WHERE id=?", (device,)).fetchone()
        return str(row["name"]) if row and row["name"] else "iPhone/iPad"

    def _record_listening(self, device: str, item: dict[str, Any]) -> None:
        """Statistics: audiobook time listened on the phone (after the checkpoint transaction)."""
        try:
            from .consumption import ConsumptionLedger

            ledger = getattr(self.api, "consumption", None) or ConsumptionLedger(self.db)
            book_id = int(item["book_id"])
            media = ledger.audiobook_media(book_id)
            if media is None:
                return
            start, end, first, last = item["interval"]
            ledger.record_device_interval(
                device_id=device,
                device_name=self.device_name(device),
                event_id="iphone-audio:" + item["checkpoint"],
                kind="audiobook",
                media_ref=media,
                start_utc=start,
                end_utc=end,
                active_seconds=end - start,
                locator_start={"position_seconds": first},
                locator_end={"position_seconds": last},
                payload={
                    "speed": round((last - first) / max(0.001, end - start), 3),
                    "seek_or_discontinuity": False,
                },
                measurement_method="iphone_audiobook_checkpoints",
                activity_group_id=ledger.audiobook_activity_group(book_id),
            )
        except Exception as exc:  # noqa: BLE001 - statistics never block the position receipt
            logger = getattr(self.api, "logger", None)
            if logger is not None:
                logger.warning("FAIL iphone.listening_statistics error=%r", str(exc)[:300])

    def _record_checkpoint(
        self, device: str, checkpoint: dict[str, Any], listened: list[dict[str, Any]]
    ) -> dict[str, Any]:
        event = identifier(checkpoint.get("checkpointId"))
        body = encoded(checkpoint)
        with self.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT * FROM iphone_checkpoints WHERE id=?", (event,)).fetchone()
            if previous:
                if previous["device"] != device or previous["body"] != body:
                    raise ValueError("Checkpoint ID reused")
                return json.loads(previous["receipt"])
            if checkpoint.get("deviceId") != device:
                raise ValueError("Checkpoint belongs to another device")

            def rejected(code: str, revision: int = 0) -> dict[str, Any]:
                # A well-formed but stale event (book removed, new edition, Mac
                # database restored) gets a final receipt. Answering 409 would
                # make the phone retry it forever and block every later sync.
                receipt = {"checkpointId": event, "revision": revision, "status": "rejected", "reason": code}
                conn.execute(
                    "INSERT INTO iphone_checkpoints(id,device,body,receipt) VALUES(?,?,?,?)",
                    (event, device, body, encoded(receipt)),
                )
                return receipt

            manifest_row = conn.execute(
                "SELECT * FROM iphone_assets WHERE id=?", (checkpoint.get("assetId"),)
            ).fetchone()
            if manifest_row is None:
                identifier(checkpoint.get("assetId"))
                return rejected("unknown_asset")
            manifest = json.loads(manifest_row["manifest"])
            current_revision_row = conn.execute(
                "SELECT revision FROM iphone_progress WHERE asset=?", (manifest["assetId"],)
            ).fetchone()
            current_revision = int(current_revision_row["revision"]) if current_revision_row else 0
            if (
                checkpoint.get("editionId") != manifest["editionId"]
                or checkpoint.get("timelineRevision") != manifest["timelineRevision"]
            ):
                return rejected("edition_changed", current_revision)
            position = integer(checkpoint.get("positionMs"), "position")
            base = integer(checkpoint.get("baseProgressRevision"), "base revision")
            sequence = integer(checkpoint.get("sequence"), "sequence", 1)
            observed = integer(checkpoint.get("observedAt"), "timestamp")
            session = checkpoint.get("playbackSessionId")
            if not isinstance(session, str) or not 1 <= len(session) <= 128:
                raise ValueError("Invalid playback session")
            reason = checkpoint.get("reason")
            if reason not in {
                "periodic",
                "pause",
                "seek",
                "chapter",
                "finish",
                "reset",
                "unfinish",
                "interruption",
                "background",
                "stop",
                "handoff",
            }:
                raise ValueError("Invalid checkpoint reason")
            part = next((p for p in manifest["parts"] if p["partId"] == checkpoint.get("partId")), None)
            local = integer(checkpoint.get("partPositionMs"), "part position")
            if (
                part is None
                or position > manifest["durationMs"]
                or local > part["durationMs"]
                or part["offsetMs"] + local != position
            ):
                raise ValueError("Position outside timeline")
            if device != "mac" and manifest_row["book_id"] is not None:
                mark = conn.execute(
                    "SELECT * FROM iphone_listen_marks WHERE device=? AND session=?", (device, session)
                ).fetchone()
                if mark is None or sequence > int(mark["sequence"]):
                    interval = listening_interval(mark, position, observed) if mark is not None else None
                    if interval is not None:
                        listened.append(
                            {
                                "book_id": int(manifest_row["book_id"]),
                                "interval": interval,
                                "checkpoint": event,
                            }
                        )
                    conn.execute(
                        "INSERT INTO iphone_listen_marks(device,session,sequence,position,observed,reason) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(device,session) DO UPDATE SET sequence=excluded.sequence,position=excluded.position,"
                        "observed=excluded.observed,reason=excluded.reason",
                        (device, session, sequence, position, observed, reason),
                    )
            row = conn.execute(
                "SELECT * FROM iphone_progress WHERE asset=?", (manifest["assetId"],)
            ).fetchone()
            revision = row["revision"] if row else 0
            selected = json.loads(row["selected"]) if row and row["selected"] else None
            conflicts = json.loads(row["conflicts"]) if row else []
            finished = bool(row["finished"]) if row else False
            if base > revision:
                # The device saw revisions this database no longer has: the Mac
                # library was restored from an older copy. Never answer with an
                # older revision (the device would take the older place):
                # continue from its revision. Its place is the newer one unless
                # another device or the Mac moved since the restore; then both
                # are offered as choices (a conflict).
                restored = conn.execute("SELECT value FROM state WHERE key='iphone_restored_at'").fetchone()
                try:
                    restored_at = int(restored["value"]) if restored else 0
                except (TypeError, ValueError):
                    restored_at = 0
                moved_elsewhere = bool(
                    selected
                    and selected.get("deviceId") != device
                    and (not restored_at or int(selected.get("receivedAt") or 0) > restored_at)
                )
                revision = base + 1 if moved_elsewhere else base
            fence = conn.execute(
                "SELECT revision FROM iphone_resolution_fences WHERE asset=?", (manifest["assetId"],)
            ).fetchone()
            same_session = bool(
                selected
                and selected["editionId"] == manifest["editionId"]
                and selected["deviceId"] == device
                and selected["playbackSessionId"] == session
            )
            causal = same_session and base >= (fence["revision"] if fence else 0)
            if same_session and sequence <= selected["sequence"]:
                status = "outdated"
            elif base == revision or (causal and sequence > selected["sequence"]):
                status = "accepted"
                revision += 1
                # Mac clock receive time: "still playing there?" must not depend
                # on the phone's clock.
                selected = {**checkpoint, "receivedAt": now_ms()}
                # Finished is set by "finish" and cleared only by an explicit "unfinish"
                # (Mark unfinished); "reset" (Start over) moves the place and keeps it.
                if reason == "finish":
                    finished = True
                elif reason == "unfinish":
                    finished = False
            else:
                status = "conflict"
                # One candidate per listening session: a long parallel session
                # replaces its own older entry instead of adding one per save.
                conflicts = [
                    item
                    for item in conflicts
                    if (item.get("deviceId"), item.get("playbackSessionId")) != (device, session)
                ]
                # Mac receive time, as for the selected one: candidates of several devices
                # are ordered by one clock.
                conflicts.append({**checkpoint, "receivedAt": now_ms()})
            receipt = {"checkpointId": event, "revision": revision, "status": status}
            conn.execute(
                "INSERT INTO iphone_checkpoints(id,device,body,receipt) VALUES(?,?,?,?)",
                (event, device, body, encoded(receipt)),
            )
            conn.execute(
                "INSERT INTO iphone_progress(asset,revision,selected,conflicts,finished) VALUES(?,?,?,?,?) ON CONFLICT(asset) DO UPDATE SET revision=excluded.revision,selected=excluded.selected,conflicts=excluded.conflicts,finished=excluded.finished",
                (
                    manifest["assetId"],
                    revision,
                    encoded(selected) if selected else None,
                    encoded(conflicts),
                    int(finished),
                ),
            )
            if status == "accepted" and manifest_row["book_id"] is not None:
                conn.execute(
                    "UPDATE audiobooks SET position=?,finished=?,updated_at=? WHERE id=?",
                    (position / 1000, int(finished), time.time(), manifest_row["book_id"]),
                )
            return receipt

    def resolve_progress(self, asset: str, expected_revision: int, chosen: str) -> dict[str, Any]:
        with self.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM iphone_progress WHERE asset=?", (asset,)).fetchone()
            if row is None or row["revision"] != expected_revision:
                raise ValueError("Progress changed; refresh choices")
            options = [json.loads(row["selected"])] + json.loads(row["conflicts"])
            selected = next((p for p in options if p and p["checkpointId"] == chosen), None)
            current = json.loads(
                conn.execute("SELECT manifest FROM iphone_assets WHERE id=?", (asset,)).fetchone()["manifest"]
            )
            if selected is None or selected["editionId"] != current["editionId"]:
                raise ValueError("Invalid resolution")
            revision = row["revision"] + 1
            if selected["deviceId"] == "mac":
                state = self.mac_sessions.get((asset, selected["playbackSessionId"]))
                if state is not None:
                    state["base"] = revision
            conn.execute(
                "INSERT OR REPLACE INTO iphone_resolution_fences(asset,revision) VALUES(?,?)",
                (asset, revision),
            )
            conn.execute(
                "UPDATE iphone_progress SET revision=?,selected=?,conflicts=? WHERE asset=?",
                (revision, encoded(selected), "[]", asset),
            )
            book = conn.execute("SELECT book_id FROM iphone_assets WHERE id=?", (asset,)).fetchone()
            if book and book["book_id"] is not None:
                conn.execute(
                    "UPDATE audiobooks SET position=?,updated_at=? WHERE id=?",
                    (selected["positionMs"] / 1000, time.time(), book["book_id"]),
                )
            return {"revision": revision, "status": "accepted"}

    def phone_position_choices(self, progress: dict[str, Any] | None) -> list[dict[str, Any]]:
        """While a book's positions conflict: each iPhone/iPad's newest candidate, newest first.

        Choosing any of them resolves the conflict for every device.
        """
        if not progress or not progress.get("conflicts"):
            return []
        options = [progress.get("selected"), *progress["conflicts"]]
        seen = lambda item: int(item.get("receivedAt") or item.get("observedAt") or 0)  # noqa: E731
        # Two saves can reach the Mac in the same millisecond: the one listed later arrived later.
        order = {id(item): index for index, item in enumerate(options) if item}
        rank = lambda item: (seen(item), order.get(id(item), -1))  # noqa: E731
        newest: dict[str, dict[str, Any]] = {}
        for item in options:
            device = str((item or {}).get("deviceId") or "")
            if device and device != "mac" and (device not in newest or rank(item) > rank(newest[device])):
                newest[device] = item
        return [
            {
                "checkpoint_id": choice["checkpointId"],
                "revision": int(progress.get("revision") or 0),
                "position": int(choice.get("positionMs") or 0) / 1000,
                "device_id": device,
                "device_name": self.device_name(device),
                "updated_at": seen(choice) / 1000 if seen(choice) else None,
            }
            for device, choice in sorted(newest.items(), key=lambda pair: rank(pair[1]), reverse=True)
        ]

    def phone_position_choice(self, progress: dict[str, Any] | None) -> dict[str, Any] | None:
        """The newest iPhone/iPad candidate while a book's positions conflict."""
        choices = self.phone_position_choices(progress)
        return choices[0] if choices else None

    def use_phone_position(self, book_id: int, checkpoint_id: str, expected_revision: int) -> float:
        """Resolve a conflict on the Mac in favour of the phone; returns the chosen position."""
        asset = self.asset_for_book(int(book_id))
        if asset is None:
            raise ValueError("Audiobook is not shared with the iPhone")
        result = self.resolve_progress(asset, int(expected_revision), str(checkpoint_id))
        # A Mac player that keeps playing follows the choice instead of reopening the conflict.
        for (session_asset, _session), state in self.mac_sessions.items():
            if session_asset == asset:
                state["base"] = result["revision"]
        return self.progress(asset)["selected"]["positionMs"] / 1000

    def catalog(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            items = [json.loads(r["manifest"]) for r in conn.execute("SELECT manifest FROM iphone_assets")]
        return {"protocolMajor": 2, "serverId": self.server_id, "items": items}

    def refresh_catalog(self, wait_seconds: float = 10.0) -> dict[str, Any]:
        """Refresh manifests in the background; wait briefly for quick results.

        Hashing a new library or converting an incompatible codec can take
        minutes. The phone's catalog request must not hang on it: what is ready
        within ``wait_seconds`` is returned, the rest appears on a later refresh.
        """
        with self._catalog_lock:
            worker = self._catalog_worker
            if worker is None or not worker.is_alive():
                worker = threading.Thread(
                    target=self._refresh_catalog_worker, name="pudge-iphone-catalog", daemon=True
                )
                self._catalog_worker = worker
                worker.start()
        worker.join(timeout=max(0.0, float(wait_seconds)))
        return {"errors": list(self._catalog_errors), "preparing": worker.is_alive()}

    def _refresh_catalog_worker(self) -> None:
        try:
            self._catalog_errors = self._refresh_catalog_now()["errors"]
        except Exception as exc:  # A failed scan must never break the HTTP server.
            self._catalog_errors = [{"title": "*", "error": type(exc).__name__}]

    def _refresh_catalog_now(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            books = [dict(r) for r in conn.execute("SELECT * FROM audiobooks ORDER BY id")]
        errors = []
        for book in books:
            try:
                with self.db.connect() as conn:
                    files = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT * FROM audiobook_files WHERE book_id=? ORDER BY file_index", (book["id"],)
                        )
                    ]
                    chapters = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT * FROM audiobook_chapters WHERE book_id=? ORDER BY chapter_index",
                            (book["id"],),
                        )
                    ]
                if not files:
                    files = [{"path": book["path"], "title": book["title"]}]
                with self.db.connect() as conn:
                    existing = conn.execute(
                        "SELECT id,manifest FROM iphone_assets WHERE book_id=?", (book["id"],)
                    ).fetchone()
                if existing and Path(book["path"]).is_relative_to(self.root / "books"):
                    continue  # Imported phone manifests already describe immutable archived bytes.
                asset = (
                    existing["id"]
                    if existing
                    else str(uuid.uuid5(uuid.UUID(self.server_id), "audiobook:" + str(book["id"])))
                )
                prepared = [self._prepared_audio(Path(f["path"])) for f in files]
                edition = str(
                    uuid.uuid5(
                        uuid.UUID(asset),
                        encoded([(p["originalSHA256"], p["canonicalDurationMs"]) for p in prepared]),
                    )
                )
                parts = []
                offset = 0
                for index, p in enumerate(prepared):
                    resource = str(uuid.uuid5(uuid.UUID(self.server_id), p["sha256"]))
                    parts.append(
                        {
                            "partId": str(uuid.uuid5(uuid.UUID(asset), "part:" + str(index))),
                            "resourceId": resource,
                            "sha256": p["sha256"],
                            "sizeBytes": p["sizeBytes"],
                            "fileName": f"part-{index + 1}{p['path'].suffix}",
                            "offsetMs": offset,
                            "durationMs": p["canonicalDurationMs"],
                            "playbackDurationMs": p["playbackDurationMs"],
                        }
                    )
                    offset += p["canonicalDurationMs"]
                    with self.db.connect() as conn:
                        conn.execute(
                            "INSERT OR IGNORE INTO iphone_resources(id,sha,size,path) VALUES(?,?,?,?)",
                            (resource, p["sha256"], p["sizeBytes"], str(p["path"])),
                        )
                manifest = {
                    "protocolMajor": 2,
                    "assetId": asset,
                    "editionId": edition,
                    "timelineRevision": edition,
                    "title": book["title"],
                    "durationMs": offset,
                    "parts": parts,
                    "chapters": [
                        {
                            "title": c["title"],
                            "startMs": min(offset, round(c["start"] * 1000)),
                            "endMs": min(offset, round(c["end"] * 1000)),
                        }
                        for c in chapters
                    ],
                }
                cover = self._book_cover(book["id"], Path(files[0]["path"]))
                if cover is not None:
                    manifest["cover"] = cover
                manifest.update(self._series_info(book["id"]))
                read_along = self._read_along(book["id"])
                if read_along is not None:
                    manifest["readAlong"] = read_along
                    linked = self._linked_light_novel(book["id"])
                    if linked is not None:
                        manifest["lightNovelId"] = linked
                self.register_manifest(manifest, book_id=book["id"])
                if self.progress(asset)["selected"] is None:
                    self._record_mac_action(
                        asset, round(book["position"] * 1000), "finish" if book["finished"] else "pause"
                    )
                session = getattr(self.audiobooks, "_playback_sessions", {}).get(book["id"])
                if session and (asset, session) not in self.mac_sessions:
                    self.begin_mac_session(asset, session)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                errors.append({"title": book["title"], "error": type(exc).__name__})
        self._forget_deleted_mac_books()
        return {"errors": errors}

    def _forget_deleted_mac_books(self) -> None:
        """Drop catalog entries of books removed on the Mac.

        Phone-archived editions stay (their bytes are retained objects); the
        phone's own downloaded copy is never affected by this.
        """
        with self.lock, self.db.connect() as conn:
            stale = [
                row["id"]
                for row in conn.execute(
                    "SELECT a.id FROM iphone_assets a LEFT JOIN audiobooks b ON b.id=a.book_id "
                    "WHERE a.book_id IS NOT NULL AND b.id IS NULL"
                )
            ]
            for asset in stale:
                manifest = json.loads(
                    conn.execute("SELECT manifest FROM iphone_assets WHERE id=?", (asset,)).fetchone()[
                        "manifest"
                    ]
                )
                ids = [part["resourceId"] for part in resources(manifest)]
                marks = ",".join("?" for _ in ids)
                if conn.execute(
                    f"SELECT 1 FROM iphone_retention WHERE resource IN ({marks}) LIMIT 1", ids
                ).fetchone():
                    conn.execute("UPDATE iphone_assets SET book_id=NULL WHERE id=?", (asset,))
                else:
                    conn.execute("DELETE FROM iphone_assets WHERE id=?", (asset,))

    _COVER_MAX_WIDTH = 600

    def _book_cover(self, book_id: int, first_file: Path) -> dict[str, Any] | None:
        """The same cover the Mac shows (linked LN → AniList → embedded), medium size."""
        source = self._cover_source(book_id, first_file)
        if source is None:
            return None
        try:
            source_sha = self.file_sha256(source)
            target = self.root / "objects" / f"cover-{source_sha}-w{self._COVER_MAX_WIDTH}.jpg"
            if not target.is_file() or target.stat().st_size <= 0:
                temporary = target.with_name(target.name + ".tmp.jpg")
                ffmpeg = getattr(self.audiobooks, "ffmpeg", "ffmpeg")
                try:
                    subprocess.run(
                        [
                            str(ffmpeg),
                            "-nostdin",
                            "-v",
                            "error",
                            "-y",
                            "-i",
                            str(source),
                            "-frames:v",
                            "1",
                            "-vf",
                            f"scale='min({self._COVER_MAX_WIDTH},iw)':-2",
                            "-q:v",
                            "3",
                            str(temporary),
                        ],
                        check=True,
                        capture_output=True,
                        timeout=60,
                    )
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            size = target.stat().st_size
            if not 0 < size <= 10 * 1024 * 1024:
                return None
            sha = self.file_sha256(target)
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
        cover_id = str(uuid.uuid5(uuid.UUID(self.server_id), "cover:" + sha))
        with self.db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO iphone_resources(id,sha,size,path) VALUES(?,?,?,?)",
                (cover_id, sha, size, str(target)),
            )
        return {"resourceId": cover_id, "sha256": sha, "sizeBytes": size, "fileName": "cover.jpg"}

    def _book_details(self, book_id: int) -> dict[str, Any]:
        audio = self.audiobooks
        if audio is None:
            return {}
        try:
            try:
                return dict(audio.book(int(book_id), include_transcription=False))
            except TypeError:
                return dict(audio.book(int(book_id)))
        except Exception:  # noqa: BLE001 - metadata never blocks the catalog
            return {}

    def _series_info(self, book_id: int) -> dict[str, Any]:
        """Series title and volume as the Mac groups them (optional manifest fields)."""
        details = self._book_details(book_id)
        series = str(details.get("series_title") or "").strip()[:1024]
        volume = details.get("volume")
        info: dict[str, Any] = {}
        if series:
            info["seriesTitle"] = series
        if type(volume) is int and 0 < volume < 10000:
            info["volume"] = volume
        return info

    def _linked_light_novel(self, book_id: int) -> int | None:
        """Light novel linked to an audiobook (Read and listen), for the iPhone reader."""
        try:
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT ln_book_id FROM reading_audio_links WHERE audiobook_id=? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (int(book_id),),
                ).fetchone()
        except Exception:  # noqa: BLE001
            return None
        return int(row["ln_book_id"]) if row is not None else None

    def _read_along(self, book_id: int) -> dict[str, Any] | None:
        """Text of the linked light novel on the audiobook clock, for read-along on iPhone."""
        audio = self.audiobooks
        if audio is None or not callable(getattr(audio, "_load_alignment", None)):
            return None
        try:
            with self.db.connect() as conn:
                link = conn.execute(
                    "SELECT ln_book_id FROM reading_audio_links WHERE audiobook_id=? "
                    "ORDER BY updated_at DESC LIMIT 1",
                    (int(book_id),),
                ).fetchone()
            if link is None:
                return None
            ln_id = int(link["ln_book_id"])
            path = Path(audio._alignment_path(ln_id, int(book_id)))
            if not path.is_file():
                return None
            stat = path.stat()
            try:
                with self.db.connect() as conn:
                    # Words appear once the reader parsed chapters (Jiten parse cache).
                    parsed = conn.execute(
                        "SELECT COUNT(*) FROM ln_parse_cache WHERE text_hash IN "
                        "(SELECT text_hash FROM ln_chapters WHERE book_id=?)",
                        (ln_id,),
                    ).fetchone()[0]
            except Exception:  # noqa: BLE001 - no reader cache yet
                parsed = 0
            key = f"readalong:{book_id}:{ln_id}:w{parsed}"
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT size,mtime_ns,result FROM iphone_prepared WHERE source=?", (key,)
                ).fetchone()
            if row is not None and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
                cached = json.loads(row["result"])
                target = Path(cached["path"])
                if target.is_file() and target.stat().st_size == cached["sizeBytes"]:
                    return {k: cached[k] for k in ("resourceId", "sha256", "sizeBytes", "fileName")}
            alignment = audio._load_alignment(ln_id, int(book_id))
            if not alignment:
                return None
            document = self._read_along_document(ln_id, alignment)
            if not document["chapters"]:
                return None
            data = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(data) > 50 * 1024 * 1024:
                return None
            sha = hashlib.sha256(data).hexdigest()
            target = self.root / "objects" / f"readalong-{sha}.json"
            if not target.is_file():
                temporary = target.with_name(target.name + ".tmp")
                temporary.write_bytes(data)
                os.replace(temporary, target)
            resource = str(uuid.uuid5(uuid.UUID(self.server_id), "readalong:" + sha))
            result = {
                "resourceId": resource,
                "sha256": sha,
                "sizeBytes": len(data),
                "fileName": "readalong.json",
            }
            with self.db.connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO iphone_resources(id,sha,size,path) VALUES(?,?,?,?)",
                    (resource, sha, len(data), str(target)),
                )
                conn.execute(
                    "INSERT INTO iphone_prepared(source,size,mtime_ns,result) VALUES(?,?,?,?) "
                    "ON CONFLICT(source) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,result=excluded.result",
                    (key, stat.st_size, stat.st_mtime_ns, encoded({**result, "path": str(target)})),
                )
            return result
        except Exception:  # noqa: BLE001 - read-along is optional; audio never depends on it
            return None

    _SENTENCE_END = frozenset("。！？!?…")
    _SENTENCE_CLOSE = frozenset("」』）)】〉》\"'")

    def _read_along_document(self, ln_id: int, alignment: dict[str, Any]) -> dict[str, Any]:
        from .reading_audio_alignment import (
            _IMAGE_PARAGRAPH_RE,
            _runtime_audio_position_index,
            normalize_reading_text,
        )
        from .iphone_media import compact_tokens, utf16_to_index

        with self.db.connect() as conn:
            texts = {
                int(r["chapter_index"]): (str(r["title"]), str(r["text"]))
                for r in conn.execute(
                    "SELECT chapter_index,title,text FROM ln_chapters WHERE book_id=?", (int(ln_id),)
                )
            }
            try:
                hashes = {
                    int(r["chapter_index"]): str(r["text_hash"])
                    for r in conn.execute(
                        "SELECT chapter_index,text_hash FROM ln_chapters WHERE book_id=?", (int(ln_id),)
                    )
                }
            except Exception:  # noqa: BLE001 - older schema without hashes: sentences only
                hashes = {}
            title_row = conn.execute("SELECT title FROM ln_books WHERE id=?", (int(ln_id),)).fetchone()
        reader = getattr(getattr(self, "api", None), "light_novels", None)
        chapters = []
        for row in _runtime_audio_position_index(alignment).get("chapters") or []:
            chapter = row["chapter"]
            index = int(chapter.get("chapter_index") or 0)
            if index not in texts:
                continue
            title, text = texts[index]
            parsed = None
            if reader is not None and index in hashes:
                try:
                    parsed = reader._cached_parse(hashes[index], provider="jiten")
                except Exception:  # noqa: BLE001 - words are optional
                    parsed = None
            groups = (parsed or {}).get("tokens") or []
            vocabulary = (parsed or {}).get("vocabulary") or []
            sentences = []
            cursor = 0
            paragraph = 0
            parse_index = -1
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                parse_index += 1
                if _IMAGE_PARAGRAPH_RE.fullmatch(line):
                    continue
                group = (
                    compact_tokens(groups[parse_index], vocabulary, line) if parse_index < len(groups) else []
                )
                to_python = utf16_to_index(line)
                to_utf16 = [0]
                for character in line:
                    to_utf16.append(to_utf16[-1] + (2 if ord(character) > 0xFFFF else 1))
                # Normalized offset of each word start, counted like the Mac reader (gaps + words).
                starts, running, previous = [], cursor, 0
                for token in group:
                    at = to_python[min(token[0], len(to_python) - 1)]
                    running += len(normalize_reading_text(line[previous:at])) if at > previous else 0
                    previous = max(previous, at)
                    starts.append(running)
                begin = 0
                for piece in self._split_sentences(line):
                    length = len(normalize_reading_text(piece))
                    item = {"p": paragraph, "text": piece, "start": cursor, "end": cursor + length}
                    low, high = to_utf16[begin], to_utf16[begin + len(piece)]
                    words = [
                        [token[0] - low, min(token[1], high) - low, start, token[2], token[3]]
                        for token, start in zip(group, starts)
                        if low <= token[0] < high
                    ]
                    if words:
                        item["w"] = words
                    sentences.append(item)
                    cursor += length
                    begin += len(piece)
                paragraph += 1
            anchors = []
            for anchor in row["anchors"]:
                point = [
                    max(0, round(float(anchor["time"]) * 1000)),
                    max(0, int(round(float(anchor["offset"])))),
                ]
                if anchors and (point[0] < anchors[-1][0] or point[1] < anchors[-1][1]):
                    continue  # the phone interpolates on a monotonic clock
                if anchors and point[0] - anchors[-1][0] < 400 and point[1] - anchors[-1][1] < 4:
                    continue
                anchors.append(point)
            if not sentences or len(anchors) < 2:
                continue
            chapters.append(
                {
                    "index": index,
                    "title": title,
                    "startMs": round(float(row["start"]) * 1000),
                    "endMs": round(float(row["end"]) * 1000),
                    "sentences": sentences,
                    "anchors": anchors,
                }
            )
        return {"schema": 1, "title": str(title_row["title"]) if title_row else "", "chapters": chapters}

    @classmethod
    def _split_sentences(cls, line: str) -> list[str]:
        pieces, current = [], ""
        for index, character in enumerate(line):
            current += character
            following = line[index + 1] if index + 1 < len(line) else ""
            if (
                character in cls._SENTENCE_END
                and following not in cls._SENTENCE_END
                and following not in cls._SENTENCE_CLOSE
            ):
                pieces.append(current)
                current = ""
            elif (
                character in cls._SENTENCE_CLOSE
                and current
                and len(current) > 1
                and current[-2] in cls._SENTENCE_END
                and following not in cls._SENTENCE_CLOSE
            ):
                pieces.append(current)
                current = ""
        if current:
            pieces.append(current)
        return pieces

    def _cover_source(self, book_id: int, first_file: Path) -> Path | None:
        audio = self.audiobooks
        url = str(self._book_details(book_id).get("cover_url") or "")
        if url.startswith(("http://", "https://")):
            return self._remote_cover(url)
        if url.startswith("covers/"):
            name = Path(url).name
            folders = [getattr(audio, "cover_cache_dir", None), *getattr(self, "cover_dirs", [])]
            for folder in folders:
                if folder and (Path(folder) / name).is_file():
                    return Path(folder) / name
        if audio is not None:
            paths = audio._embedded_cover_paths(first_file)
            if paths:
                audio._embedded_cover_url(first_file, extract=True)
                if paths[1].is_file() and paths[1].stat().st_size > 0:
                    return paths[1]
        return None

    def _remote_cover(self, url: str) -> Path | None:
        folder = self.root / "cover-sources"
        folder.mkdir(exist_ok=True, mode=0o700)
        target = folder / hashlib.sha256(url.encode("utf-8")).hexdigest()
        if target.is_file() and target.stat().st_size > 0:
            return target
        try:
            import httpx

            response = httpx.get(url, timeout=15.0, follow_redirects=True)
            response.raise_for_status()
            data = response.content
        except Exception:  # noqa: BLE001 - offline Mac: no cover, catalog still works
            return None
        if not 0 < len(data) <= 10 * 1024 * 1024:
            return None
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
        return target

    def bookmarks(self, asset: str) -> dict[str, Any]:
        # Deferred transaction: it only becomes a write when desktop bookmarks
        # actually changed (the phone reads this for every book on each sync).
        with self.lock, self.db.connect() as conn:
            self._sync_desktop_bookmarks(conn, asset)
            values = [
                json.loads(r["body"])
                for r in conn.execute("SELECT body FROM iphone_bookmarks WHERE asset=?", (asset,))
            ]
        return {"items": values}

    def _sync_desktop_bookmarks(self, conn: Any, asset: str) -> None:
        row = conn.execute("SELECT book_id,manifest FROM iphone_assets WHERE id=?", (asset,)).fetchone()
        if row is None or row["book_id"] is None:
            return
        manifest = json.loads(row["manifest"])
        desktop = {
            r["id"]: r
            for r in conn.execute("SELECT * FROM audiobook_bookmarks WHERE book_id=?", (row["book_id"],))
        }
        links = {
            r["desktop_id"]: r["id"]
            for r in conn.execute(
                "SELECT l.* FROM iphone_bookmark_links l JOIN iphone_bookmarks b ON b.id=l.id "
                "WHERE b.asset=? AND b.edition=?",
                (asset, manifest["editionId"]),
            )
        }
        for desktop_id in desktop.keys() | links.keys():
            key = links.get(desktop_id) or str(
                uuid.uuid5(uuid.UUID(asset), f"bookmark:{manifest['editionId']}:{desktop_id}")
            )
            old = conn.execute("SELECT * FROM iphone_bookmarks WHERE id=?", (key,)).fetchone()
            previous = json.loads(old["body"]) if old else None
            mark = desktop.get(desktop_id)
            deleted = mark is None
            position = (
                max(0, min(round(mark["position"] * 1000), manifest["durationMs"]))
                if mark
                else previous["positionMs"]
            )
            title = str(mark["title"])[:1024] if mark else previous["title"]
            if previous and (previous["positionMs"], previous["title"], previous["deleted"]) == (
                position,
                title,
                deleted,
            ):
                continue
            revision = old["revision"] if old else 0
            event = str(uuid.uuid4())
            body = {
                "bookmarkId": key,
                "eventId": event,
                "assetId": asset,
                "editionId": manifest["editionId"],
                "positionMs": position,
                "title": title,
                "deleted": deleted,
                "baseRevision": revision,
            }
            stored = {**body, "baseRevision": revision + 1}
            conn.execute(
                "INSERT INTO iphone_bookmarks(id,asset,edition,body,revision) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET body=excluded.body,revision=excluded.revision",
                (key, asset, manifest["editionId"], encoded(stored), revision + 1),
            )
            conn.execute(
                "INSERT OR REPLACE INTO iphone_bookmark_links(id,desktop_id) VALUES(?,?)", (key, desktop_id)
            )
            conn.execute(
                "INSERT INTO iphone_bookmark_events(id,device,body,receipt) VALUES(?,?,?,?)",
                (event, "mac", encoded(body), encoded({"eventId": event, "revision": revision + 1})),
            )

    def record_bookmark(self, device: str, bookmark: dict[str, Any]) -> dict[str, Any]:
        event = identifier(bookmark.get("eventId"))
        key = identifier(bookmark.get("bookmarkId"))
        body = encoded(bookmark)
        with self.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old_event = conn.execute("SELECT * FROM iphone_bookmark_events WHERE id=?", (event,)).fetchone()
            if old_event:
                if old_event["device"] != device or old_event["body"] != body:
                    raise ValueError("Bookmark event reused")
                return json.loads(old_event["receipt"])

            def rejected(code: str, revision: int = 0) -> dict[str, Any]:
                receipt = {"eventId": event, "revision": revision, "status": "rejected", "reason": code}
                conn.execute(
                    "INSERT INTO iphone_bookmark_events(id,device,body,receipt) VALUES(?,?,?,?)",
                    (event, device, body, encoded(receipt)),
                )
                return receipt

            row = conn.execute(
                "SELECT manifest,book_id FROM iphone_assets WHERE id=?", (bookmark.get("assetId"),)
            ).fetchone()
            if row is None:
                identifier(bookmark.get("assetId"))
                return rejected("unknown_asset")
            manifest = json.loads(row["manifest"])
            if bookmark.get("editionId") != manifest["editionId"]:
                return rejected("edition_changed")
            if integer(bookmark.get("positionMs"), "position") > manifest["durationMs"]:
                raise ValueError("Bookmark outside timeline")
            if (
                type(bookmark.get("deleted")) is not bool
                or not isinstance(bookmark.get("title"), str)
                or len(bookmark["title"]) > 1024
            ):
                raise ValueError("Invalid bookmark")
            self._sync_desktop_bookmarks(conn, manifest["assetId"])
            old = conn.execute("SELECT * FROM iphone_bookmarks WHERE id=?", (key,)).fetchone()
            revision = old["revision"] if old else 0
            if old and (old["asset"] != manifest["assetId"] or old["edition"] != manifest["editionId"]):
                raise ValueError("Bookmark belongs to another asset")
            if integer(bookmark.get("baseRevision"), "revision") != revision:
                # The Mac (or another device) changed this bookmark first; the
                # newer version is kept and the phone re-reads it.
                return rejected("bookmark_changed", revision)
            stored = {**bookmark, "baseRevision": revision + 1}
            conn.execute(
                "INSERT INTO iphone_bookmarks(id,asset,edition,body,revision) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,revision=excluded.revision",
                (key, manifest["assetId"], manifest["editionId"], encoded(stored), revision + 1),
            )
            receipt = {"eventId": event, "revision": revision + 1}
            conn.execute(
                "INSERT INTO iphone_bookmark_events(id,device,body,receipt) VALUES(?,?,?,?)",
                (event, device, body, encoded(receipt)),
            )
            if row["book_id"] is not None:
                link = conn.execute(
                    "SELECT desktop_id FROM iphone_bookmark_links WHERE id=?", (key,)
                ).fetchone()
                desktop_id = link["desktop_id"] if link else None
                if bookmark["deleted"]:
                    if desktop_id is not None:
                        conn.execute(
                            "DELETE FROM audiobook_bookmarks WHERE id=? AND book_id=?",
                            (desktop_id, row["book_id"]),
                        )
                else:
                    changed = conn.execute(
                        "UPDATE audiobook_bookmarks SET title=?,position=? WHERE id=? AND book_id=?",
                        (bookmark["title"], bookmark["positionMs"] / 1000, desktop_id, row["book_id"]),
                    ).rowcount
                    if not changed:
                        order = conn.execute(
                            "SELECT COALESCE(MAX(sort_order),-1)+1 FROM audiobook_bookmarks WHERE book_id=?",
                            (row["book_id"],),
                        ).fetchone()[0]
                        cursor = conn.execute(
                            "INSERT INTO audiobook_bookmarks(book_id,position,title,sort_order,created_at) VALUES(?,?,?,?,?)",
                            (
                                row["book_id"],
                                bookmark["positionMs"] / 1000,
                                bookmark["title"],
                                order,
                                time.time(),
                            ),
                        )
                        conn.execute(
                            "INSERT OR REPLACE INTO iphone_bookmark_links(id,desktop_id) VALUES(?,?)",
                            (key, cursor.lastrowid),
                        )
            return receipt

    def request_mac_handoff(self, book_id: int, operation: str) -> dict[str, Any]:
        operation = identifier(operation)
        asset = self.asset_for_book(book_id)
        with self.lock, self.db.connect() as conn:
            book = conn.execute("SELECT path FROM audiobooks WHERE id=?", (book_id,)).fetchone()
            files = [
                r["path"]
                for r in conn.execute("SELECT path FROM audiobook_files WHERE book_id=?", (book_id,))
            ]
            previous = conn.execute("SELECT body FROM iphone_handoffs WHERE id=?", (operation,)).fetchone()
        paths = files or ([book["path"]] if book else [])
        if not asset or not paths or not all(Path(p).is_file() and Path(p).stat().st_size > 0 for p in paths):
            return {"operationId": operation, "status": "target_not_ready"}
        if previous:
            request = json.loads(previous["body"])
            if request.get("targetDeviceId") != "mac" or request["assetId"] != asset:
                raise ValueError("Handoff ID reused")
            request.pop("targetDeviceId")
        else:
            state = self.progress(asset)
            source = state["selected"]
            if source is None or source["deviceId"] == "mac":
                return {"operationId": operation, "status": "source_unreachable"}
            request = {
                "operationId": operation,
                "assetId": asset,
                "expectedRevision": state["revision"],
                "sourceSessionId": source["playbackSessionId"],
                "sourceDeviceId": source["deviceId"],
                "targetReady": True,
                "requestedAt": now_ms(),
            }
            if source.get("reason") in self._PAUSED_REASONS:
                # Plan 2.1: a source that already confirmed a pause needs no
                # dialog; continue from that confirmed checkpoint.
                result = {
                    "operationId": operation,
                    "status": "source_paused",
                    "positionMs": source["positionMs"],
                    "revision": state["revision"],
                }
                with self.lock, self.db.connect() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO iphone_handoffs(id,body,result) VALUES(?,?,?)",
                        (operation, encoded({**request, "targetDeviceId": "mac"}), encoded(result)),
                    )
                return result
        return self.handoff("mac", request)

    _PAUSED_REASONS = frozenset({"pause", "stop", "background", "interruption", "handoff", "finish"})
    _LIVE_WINDOW_MS = 45000  # the phone reports a playing position at least every 15 s

    def playing_elsewhere(self, progress: dict[str, Any] | None, here: str) -> bool:
        """The latest position comes from another device that is still playing."""
        selected = (progress or {}).get("selected")
        if not selected or selected.get("deviceId") == here:
            return False
        if selected.get("reason") in self._PAUSED_REASONS or selected.get("reason") in {"reset", "unfinish"}:
            return False
        seen = int(selected.get("receivedAt") or selected.get("observedAt") or 0)
        return now_ms() - seen <= self._LIVE_WINDOW_MS

    def claim_mac_handoff(self, operation: str) -> bool:
        with self.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT result FROM iphone_handoffs WHERE id=?", (identifier(operation),)
            ).fetchone()
            if conn.execute("SELECT 1 FROM iphone_mac_starts WHERE operation=?", (operation,)).fetchone():
                return False
            if row is None or row["result"] is None or json.loads(row["result"])["status"] != "source_paused":
                raise ValueError("Source has not confirmed pause")
            body = json.loads(
                conn.execute("SELECT body FROM iphone_handoffs WHERE id=?", (operation,)).fetchone()["body"]
            )
            progress = conn.execute(
                "SELECT revision FROM iphone_progress WHERE asset=?", (body["assetId"],)
            ).fetchone()
            if (
                body.get("targetDeviceId") != "mac"
                or progress is None
                or progress["revision"] != json.loads(row["result"])["revision"]
            ):
                raise ValueError("Source changed after pause; request a new handoff")
            return (
                conn.execute(
                    "INSERT OR IGNORE INTO iphone_mac_starts(operation) VALUES(?)", (operation,)
                ).rowcount
                == 1
            )

    def cancel_mac_handoff(self, operation: str) -> None:
        with self.lock, self.db.connect() as conn:
            row = conn.execute(
                "SELECT body FROM iphone_handoffs WHERE id=?", (identifier(operation),)
            ).fetchone()
            if row is None or json.loads(row["body"]).get("targetDeviceId") != "mac":
                raise ValueError("Unknown Mac handoff")
            conn.execute(
                "UPDATE iphone_handoffs SET result=? WHERE id=? AND result IS NULL",
                (encoded({"operationId": operation, "status": "cancelled"}), operation),
            )

    def begin_mac_session(self, asset: str, session: str) -> None:
        self.handed_off_sessions.discard((asset, session))
        self.mac_sessions[(asset, session)] = {
            "base": self.progress(asset)["revision"],
            "sequence": 0,
            "last": None,
        }

    def _record_mac_action(self, asset: str, position_ms: int, reason: str) -> dict[str, Any] | None:
        session = "action:" + str(uuid.uuid4())
        self.begin_mac_session(asset, session)
        try:
            return self.record_mac_position(asset, session, position_ms, reason=reason)
        finally:
            self.mac_sessions.pop((asset, session), None)

    def mark_mac_finished(self, book_id: int, finished: bool, *, position: float | None = None) -> bool:
        asset = self.asset_for_book(book_id)
        if asset is None:
            return False
        manifest = self.manifest(asset)
        target = manifest["durationMs"] if finished else round(max(0.0, float(position or 0.0)) * 1000)
        self._record_mac_action(
            asset, min(target, manifest["durationMs"]), "finish" if finished else "unfinish"
        )
        return True

    def record_mac_position(
        self, asset: str, session: str | None, position_ms: int, *, reason: str = "periodic"
    ) -> dict[str, Any] | None:
        # An inactive player never creates a synthetic listening branch, and a
        # player paused by a handoff to the phone no longer owns the position.
        if (
            not session
            or (asset, session) not in self.mac_sessions
            or (asset, session) in self.handed_off_sessions
        ):
            return None
        state = self.mac_sessions[(asset, session)]
        if reason == "periodic" and state["last"] == position_ms:
            return None
        manifest = self.manifest(asset)
        position = max(0, min(position_ms, manifest["durationMs"]))
        part = next(
            (p for p in reversed(manifest["parts"]) if p["offsetMs"] <= position), manifest["parts"][0]
        )
        state["sequence"] += 1
        checkpoint = {
            "checkpointId": str(uuid.uuid4()),
            "assetId": asset,
            "editionId": manifest["editionId"],
            "timelineRevision": manifest["timelineRevision"],
            "playbackSessionId": session,
            "deviceId": "mac",
            "sequence": state["sequence"],
            "baseProgressRevision": state["base"],
            "positionMs": position,
            "partId": part["partId"],
            "partPositionMs": position - part["offsetMs"],
            "reason": reason,
            "observedAt": now_ms(),
        }
        receipt = self.record_checkpoint("mac", checkpoint)
        if receipt["status"] == "accepted":
            state["base"] = receipt["revision"]
        state["last"] = position_ms
        return receipt

    def asset_for_book(self, book_id: int) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT id FROM iphone_assets WHERE book_id=?", (book_id,)).fetchone()
        return str(row["id"]) if row else None

    def save_mac_position(self, book_id: int, position: float) -> bool:
        asset = self.asset_for_book(book_id)
        if asset is None:
            return False
        session = getattr(self.audiobooks, "_playback_sessions", {}).get(book_id)
        if (
            session
            and (asset, session) not in self.mac_sessions
            and (asset, session) not in self.handed_off_sessions
        ):
            # A live player started before this service instance existed
            # (iPhone access toggled during playback) is still a real session.
            self.begin_mac_session(asset, session)
        self.record_mac_position(asset, session, round(position * 1000))
        return True

    def record_mac_seek(self, book_id: int, position: float) -> bool:
        asset = self.asset_for_book(book_id)
        if asset is None:
            return False
        receipt = self._record_mac_action(asset, max(0, round(float(position) * 1000)), "seek")
        return bool(receipt and receipt.get("status") == "accepted")

    def mac_resume_position(self, book_id: int, fallback: float) -> float:
        asset = self.asset_for_book(book_id)
        if asset:
            selected = self.progress(asset)["selected"]
            if selected and selected["editionId"] == self.manifest(asset)["editionId"]:
                with self.db.connect() as conn:
                    row = conn.execute(
                        "SELECT position,finished,updated_at FROM audiobooks WHERE id=?", (book_id,)
                    ).fetchone()
                if (
                    row is not None
                    and float(row["updated_at"] or 0) * 1000 > selected["observedAt"]
                    and abs(float(row["position"] or 0) * 1000 - selected["positionMs"]) > 2000
                ):
                    # The Mac played while this service was not attached (iPhone
                    # access off in an earlier run): that newer local listening
                    # becomes the shared position instead of being skipped back.
                    receipt = self._record_mac_action(asset, round(float(row["position"]) * 1000), "stop")
                    if receipt and receipt.get("status") == "accepted":
                        return float(row["position"])
                return selected["positionMs"] / 1000
        return fallback

    def handoff(self, device: str, request: dict[str, Any]) -> dict[str, Any]:
        operation = identifier(request.get("operationId"))
        asset = identifier(request.get("assetId"))
        expected = integer(request.get("expectedRevision"), "revision")
        body = encoded({**request, "targetDeviceId": device})
        with self.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute("SELECT * FROM iphone_handoffs WHERE id=?", (operation,)).fetchone()
            if previous is not None:
                if previous["body"] != body:
                    raise ValueError("Handoff ID reused")
                if previous["result"]:
                    return json.loads(previous["result"])
                if device == "mac" and request.get("requestedAt", now_ms()) + 60000 < now_ms():
                    result = {"operationId": operation, "status": "source_unreachable"}
                    conn.execute(
                        "UPDATE iphone_handoffs SET result=? WHERE id=?", (encoded(result), operation)
                    )
                    return result
            else:
                conn.execute("INSERT INTO iphone_handoffs(id,body) VALUES(?,?)", (operation, body))
        progress = self.progress(asset)
        deterministic = str(uuid.uuid5(uuid.UUID(operation), "final-checkpoint"))
        with self.db.connect() as conn:
            committed = conn.execute(
                "SELECT body,receipt FROM iphone_checkpoints WHERE id=? AND device=?", (deterministic, "mac")
            ).fetchone()
        if committed:
            checkpoint = json.loads(committed["body"])
            result = {
                "operationId": operation,
                "status": "source_paused",
                "positionMs": checkpoint["positionMs"],
                "revision": json.loads(committed["receipt"])["revision"],
            }
        elif request.get("targetReady") is not True:
            result = {"operationId": operation, "status": "target_not_ready"}
        elif device != "mac" and self._other_device_playing(progress, device):
            # Another iPhone/iPad plays this book: it pauses on its next sync (commands) and
            # its final checkpoint answers this request (acknowledge_handoff).
            selected = progress["selected"]
            waiting = operation in self.device_handoffs
            if selected["playbackSessionId"] != request.get("sourceSessionId") or (
                not waiting and progress["revision"] != expected
            ):
                result = {"operationId": operation, "status": "revision_conflict"}
            elif now_ms() - self.device_handoffs.setdefault(operation, now_ms()) > self._DEVICE_HANDOFF_MS:
                result = {"operationId": operation, "status": "source_unreachable"}
            else:
                return {"operationId": operation, "status": "pending"}
        elif progress["revision"] != expected and not (device == "mac" and previous is not None):
            result = {"operationId": operation, "status": "revision_conflict"}
        elif device == "mac":
            # The phone polls this durable command and acknowledges its final
            # checkpoint. Without that acknowledgement the Mac cannot start.
            selected = progress["selected"]
            if (
                selected is not None
                and selected["playbackSessionId"] == request.get("sourceSessionId")
                and selected["deviceId"] == request.get("sourceDeviceId", selected["deviceId"])
            ):
                return {"operationId": operation, "status": "pending"}
            result = {"operationId": operation, "status": "revision_conflict"}
        else:
            with self.db.connect() as conn:
                row = conn.execute("SELECT book_id FROM iphone_assets WHERE id=?", (asset,)).fetchone()
            audio = self.audiobooks
            book_id = row["book_id"] if row else None
            if audio is None or book_id is None:
                result = {"operationId": operation, "status": "source_unreachable"}
            elif not audio.is_playing(book_id):
                selected = progress["selected"]
                if selected and selected["deviceId"] not in {"mac", device}:
                    result = {"operationId": operation, "status": "source_unreachable"}
                else:
                    result = {
                        "operationId": operation,
                        "status": "source_paused",
                        "positionMs": selected["positionMs"] if selected else 0,
                        "revision": progress["revision"],
                    }
            else:
                with audio._playback_lock:
                    session = audio._playback_sessions.get(book_id)
                    if (
                        session != request.get("sourceSessionId")
                        or self.progress(asset)["revision"] != expected
                    ):
                        result = {"operationId": operation, "status": "revision_conflict"}
                    else:
                        path = audio._ipc_paths.get(book_id)
                        reply = audio._ipc_command(path, ["set_property", "pause", True]) if path else None
                        if (
                            reply is None
                            or reply.get("error") != "success"
                            or audio._ipc_get(path, "pause") is not True
                        ):
                            result = {"operationId": operation, "status": "source_unreachable"}
                        else:
                            position = audio._global_position(book_id, path)
                            if position is None:
                                result = {"operationId": operation, "status": "source_unreachable"}
                            else:
                                manifest = self.manifest(asset)
                                value = max(0, min(round(position * 1000), manifest["durationMs"]))
                                part = next(p for p in reversed(manifest["parts"]) if p["offsetMs"] <= value)
                                checkpoint = {
                                    "checkpointId": deterministic,
                                    "assetId": asset,
                                    "editionId": manifest["editionId"],
                                    "timelineRevision": manifest["timelineRevision"],
                                    "playbackSessionId": session,
                                    "deviceId": "mac",
                                    "sequence": self.mac_sessions.get((asset, session), {}).get("sequence", 0)
                                    + 1,
                                    "baseProgressRevision": expected,
                                    "positionMs": value,
                                    "partId": part["partId"],
                                    "partPositionMs": value - part["offsetMs"],
                                    "reason": "handoff",
                                    "observedAt": now_ms(),
                                }
                                receipt = self.record_checkpoint("mac", checkpoint)
                                if receipt.get("status") != "accepted":
                                    result = {"operationId": operation, "status": "revision_conflict"}
                                else:
                                    self.handed_off_sessions.add((asset, session))
                                    if hasattr(audio, "_last_positions"):
                                        audio._last_positions[book_id] = value / 1000
                                    result = {
                                        "operationId": operation,
                                        "status": "source_paused",
                                        "positionMs": value,
                                        "revision": receipt["revision"],
                                    }
        with self.db.connect() as conn:
            conn.execute("UPDATE iphone_handoffs SET result=? WHERE id=?", (encoded(result), operation))
        return result

    _DEVICE_HANDOFF_MS = 60000

    def _other_device_playing(self, progress: dict[str, Any], device: str) -> bool:
        selected = progress.get("selected")
        return bool(
            selected
            and selected.get("deviceId") not in {"mac", device}
            and self.playing_elsewhere(progress, device)
        )

    def commands(self, device: str) -> dict[str, Any]:
        """Pause requests for this device: from the Mac or from another iPhone/iPad taking over."""
        with self.db.connect() as conn:
            pending = [
                dict(r) for r in conn.execute("SELECT id,body FROM iphone_handoffs WHERE result IS NULL")
            ]
        commands = []
        for row in pending:
            body = json.loads(row["body"])
            target = body.get("targetDeviceId")
            selected = self.progress(body["assetId"])["selected"]
            if target == device or not selected or selected["deviceId"] != device:
                continue
            if target == "mac":
                request = {k: v for k, v in body.items() if k != "targetDeviceId"}
                if self.handoff("mac", request)["status"] == "pending":
                    commands.append(body)
            elif (
                selected["playbackSessionId"] == body.get("sourceSessionId")
                and now_ms() - self.device_handoffs.get(row["id"], 0) <= self._DEVICE_HANDOFF_MS
            ):
                commands.append(body)
        return {"items": commands}

    def acknowledge_handoff(self, device: str, payload: dict[str, Any]) -> dict[str, Any]:
        operation = identifier(payload.get("operationId"))
        with self.lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM iphone_handoffs WHERE id=?", (operation,)).fetchone()
            if row is None:
                raise ValueError("Unknown handoff")
            body = json.loads(row["body"])
            if row["result"] is not None:
                return json.loads(row["result"])
            if body.get("requestedAt", now_ms()) + 60000 < now_ms():
                raise ValueError("Handoff expired; paused state must be requested again")
            event = conn.execute(
                "SELECT body,receipt FROM iphone_checkpoints WHERE id=? AND device=?",
                (payload.get("checkpointId"), device),
            ).fetchone()
            checkpoint = json.loads(event["body"]) if event else None
            selected = conn.execute(
                "SELECT selected FROM iphone_progress WHERE asset=?", (body["assetId"],)
            ).fetchone()
            current = json.loads(selected["selected"]) if selected and selected["selected"] else None
            if (
                not checkpoint
                or body.get("targetDeviceId") in {None, "", device}
                or body.get("sourceDeviceId", device) != device
                or checkpoint["assetId"] != body["assetId"]
                or checkpoint["playbackSessionId"] != body.get("sourceSessionId")
                or checkpoint["reason"] != "handoff"
                or json.loads(event["receipt"])["status"] != "accepted"
                or current is None
                or current["checkpointId"] != checkpoint["checkpointId"]
            ):
                raise ValueError("Final paused checkpoint required")
            result = {
                "operationId": operation,
                "status": "source_paused",
                "positionMs": checkpoint["positionMs"],
                "revision": json.loads(event["receipt"])["revision"],
            }
            conn.execute("UPDATE iphone_handoffs SET result=? WHERE id=?", (encoded(result), operation))
            return result
