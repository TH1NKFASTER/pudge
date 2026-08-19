from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from .alignment_quality import build_alignment_report
from .audio_activity import (
    analyze_audio_activity,
    gate_activity_regions,
    merge_activity_regions,
)
from .database import Database
from .metadata_cache import MetadataCache
from .reading_audio_alignment import (
    align_light_novel_to_transcript,
    audio_position_for_light_novel,
    audio_position_for_light_novel_offset,
    light_novel_position_for_audio,
)

AUDIOBOOK_EXTENSIONS = {".m4b", ".m4a", ".mp3", ".aac", ".opus", ".ogg", ".flac", ".wav"}
_POSITION_WRITE_INTERVAL = 10.0
_MONITOR_POLL_INTERVAL = 0.25
_STOP_GRACE_SECONDS = 0.12
_STOP_IPC_TIMEOUT = 0.05
_PLAYBACK_STALL_SECONDS = 1.1
_PLAYBACK_MOTION_EPSILON = 0.015


def _audiobook_volume(value: str) -> int | None:
    text = unicodedata.normalize("NFKC", str(value or ""))
    for pattern in (
        r"(?i)\b(?:vol(?:ume)?|v)\s*[._ -]*0*(\d{1,3})\b",
        r"第\s*0*(\d{1,3})\s*巻",
        r"0*(\d{1,3})\s*巻",
    ):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def _audiobook_title_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", html.unescape(str(value or "")))
    text = re.sub(r"^\s*\[[^\]]{1,80}\]\s*", " ", text)
    text = re.sub(
        r"(?i)\b(?:audio\s*book|audiobook|light[ ._-]*novel|novel|vol(?:ume)?|v)\s*[._ -]*0*\d{1,3}\b",
        " ",
        text,
    )
    text = re.sub(r"第\s*0*\d{1,3}\s*巻|0*\d{1,3}\s*巻", " ", text)
    text = re.sub(r"\.(?:m4b|m4a|mp3|aac|opus|ogg|flac|wav)$", "", text, flags=re.I)
    return re.sub(r"[^\wぁ-ゟ゠-ヿ一-鿿]+", "", text).casefold()


def _natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


class AudiobookService:
    """Resumable mpv audiobook playback with chapters, bookmarks and paired reading."""

    def __init__(
        self,
        database: Database,
        *,
        ffprobe: str,
        mpv: str,
        cache_dir: Path,
        ffmpeg: str = "ffmpeg",
        python: str | None = None,
        stt_model: str = "mlx-community/whisper-tiny",
        job_center: Any | None = None,
        work_scheduler: Any | None = None,
    ) -> None:
        self.db = database
        self.ffprobe = ffprobe
        self.ffmpeg = str(ffmpeg or "ffmpeg")
        self.mpv = mpv
        self.python = str(python or os.getenv("PUDGE_PYTHON", "").strip() or sys.executable)
        self.stt_model = str(stt_model or "mlx-community/whisper-tiny")
        self.cache_dir = Path(cache_dir)
        self.job_center = job_center
        self.work_scheduler = work_scheduler
        self._probe_cache = MetadataCache(self.cache_dir, "audiobook-probe", schema="v2")
        self._players: dict[int, subprocess.Popen[Any]] = {}
        self._ipc_paths: dict[int, Path] = {}
        self._last_positions: dict[int, float] = {}
        self._last_motion_at: dict[int, float] = {}
        self._speeds: dict[int, float] = {}
        self._sleep_deadlines: dict[int, float] = {}
        self._sleep_chapter_ends: dict[int, float] = {}
        self._alignment_jobs: dict[int, dict[str, Any]] = {}
        self._alignment_processes: dict[int, subprocess.Popen[Any]] = {}
        self._transcription_jobs: dict[int, dict[str, Any]] = {}
        self._transcription_processes: dict[int, subprocess.Popen[Any]] = {}
        self._transcription_events: dict[int, threading.Event] = {}
        self._transcription_cancel_events: dict[int, threading.Event] = {}
        self._lock = threading.Lock()
        self._tempo_filter_args_cache: tuple[str, ...] | None = None

    def _tempo_filter_args(self) -> list[str]:
        """Use mpv's Chromium-derived pitch-preserving tempo path.

        mpv enables ``scaletempo2`` automatically when speed changes while
        audio pitch correction is on. Its default ``scaletempo2`` parameters
        are the Chromium defaults, which gives a more browser/YouTube-like
        result than forcing a custom WSOLA window here.
        """
        with self._lock:
            cached = self._tempo_filter_args_cache
        if cached is not None:
            return list(cached)

        selected = ("--audio-pitch-correction=yes",)
        with self._lock:
            self._tempo_filter_args_cache = selected
        return list(selected)

    def _probe(self, path: Path) -> tuple[float, list[dict[str, Any]]]:
        stat = path.stat()
        key = {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        cached = self._probe_cache.get(key, ttl_seconds=180 * 24 * 3600)
        if isinstance(cached, dict):
            return float(cached.get("duration") or 0.0), [
                dict(item) for item in cached.get("chapters") or [] if isinstance(item, dict)
            ]

        completed = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_format", "-show_chapters", "-of", "json", str(path)],
            text=True,
            capture_output=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or "ffprobe could not read this audiobook")
        payload = json.loads(completed.stdout or "{}")
        try:
            duration = max(0.0, float(payload.get("format", {}).get("duration") or 0.0))
        except (TypeError, ValueError):
            duration = 0.0
        chapters: list[dict[str, Any]] = []
        for index, chapter in enumerate(payload.get("chapters") or []):
            try:
                start = float(chapter.get("start_time") or 0.0)
                end = float(chapter.get("end_time") or start)
            except (TypeError, ValueError):
                continue
            tags = chapter.get("tags") if isinstance(chapter.get("tags"), dict) else {}
            chapters.append(
                {
                    "index": index,
                    "title": str(tags.get("title") or f"Chapter {index + 1}"),
                    "start": start,
                    "end": end,
                }
            )
        self._probe_cache.put(key, {"duration": duration, "chapters": chapters})
        self._probe_cache.prune(older_than_seconds=365 * 24 * 3600, max_entries=2000)
        return duration, chapters

    def _folder_files(self, folder: Path) -> list[Path]:
        rows = [
            path.resolve()
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.casefold() in AUDIOBOOK_EXTENSIONS
        ]
        rows.sort(key=lambda path: _natural_key(str(path.relative_to(folder))))
        return rows

    def _upsert(
        self,
        *,
        path: Path,
        title: str,
        duration: float,
        files: list[dict[str, Any]],
        chapters: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO audiobooks(path,title,duration,position,finished,created_at,updated_at)
                VALUES(?,?,?,0,0,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    title=excluded.title,duration=excluded.duration,updated_at=excluded.updated_at
                """,
                (str(path), title, float(duration), now, now),
            )
            row = conn.execute("SELECT * FROM audiobooks WHERE path=?", (str(path),)).fetchone()
            assert row is not None
            book_id = int(row["id"])
            conn.execute("DELETE FROM audiobook_chapters WHERE book_id=?", (book_id,))
            conn.execute("DELETE FROM audiobook_files WHERE book_id=?", (book_id,))
            conn.executemany(
                """
                INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end)
                VALUES(?,?,?,?,?,?,?)
                """,
                [
                    (
                        book_id,
                        int(item["index"]),
                        str(item["path"]),
                        str(item["title"]),
                        float(item["duration"]),
                        float(item["start"]),
                        float(item["end"]),
                    )
                    for item in files
                ],
            )
            conn.executemany(
                """
                INSERT INTO audiobook_chapters(book_id,chapter_index,title,start,end)
                VALUES(?,?,?,?,?)
                """,
                [
                    (
                        book_id,
                        int(chapter["index"]),
                        str(chapter["title"]),
                        float(chapter["start"]),
                        float(chapter["end"]),
                    )
                    for chapter in chapters
                ],
            )
        return self.book(book_id)

    def import_file(self, path: Path) -> dict[str, Any]:
        path = path.expanduser().resolve()
        if not path.is_file() or path.suffix.casefold() not in AUDIOBOOK_EXTENSIONS:
            raise ValueError("Unsupported audiobook format")
        duration, embedded = self._probe(path)
        files = [
            {
                "index": 0,
                "path": str(path),
                "title": path.stem,
                "duration": duration,
                "start": 0.0,
                "end": duration,
            }
        ]
        chapters = embedded or [{"index": 0, "title": path.stem, "start": 0.0, "end": duration}]
        book = self._upsert(
            path=path,
            title=path.stem,
            duration=duration,
            files=files,
            chapters=chapters,
        )
        self.auto_link_audiobook(int(book["id"]))
        self.prepare_transcription(int(book["id"]))
        return self.book(int(book["id"]))

    def import_folder(self, folder: Path) -> dict[str, Any]:
        folder = folder.expanduser().resolve()
        if not folder.is_dir():
            raise ValueError("Audiobook folder does not exist")
        paths = self._folder_files(folder)
        if not paths:
            raise ValueError("No supported audio files found in this folder")
        files: list[dict[str, Any]] = []
        chapters: list[dict[str, Any]] = []
        cursor = 0.0
        for index, path in enumerate(paths):
            duration, _embedded = self._probe(path)
            start = cursor
            end = start + max(0.0, duration)
            files.append(
                {
                    "index": index,
                    "path": str(path),
                    "title": path.stem,
                    "duration": duration,
                    "start": start,
                    "end": end,
                }
            )
            chapters.append({"index": index, "title": path.stem, "start": start, "end": end})
            cursor = end
        book = self._upsert(
            path=folder,
            title=folder.name,
            duration=cursor,
            files=files,
            chapters=chapters,
        )
        self.auto_link_audiobook(int(book["id"]))
        self.prepare_transcription(int(book["id"]))
        return self.book(int(book["id"]))

    def _file_rows(self, book_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audiobook_files WHERE book_id=? ORDER BY file_index", (int(book_id),)
            ).fetchall()
        return [dict(row) for row in rows]

    def _record_playback_position(self, book_id: int, position: float) -> None:
        book_id = int(book_id)
        value = float(position)
        now = time.monotonic()
        with self._lock:
            previous = self._last_positions.get(book_id)
            self._last_positions[book_id] = value
            if (
                previous is None
                or abs(value - float(previous)) >= _PLAYBACK_MOTION_EPSILON
            ):
                self._last_motion_at[book_id] = now

    def is_playing(self, book_id: int) -> bool:
        with self._lock:
            process = self._players.get(int(book_id))
            return bool(process is not None and process.poll() is None)

    def is_paused(self, book_id: int) -> bool:
        """Return mpv's explicit pause state.

        A short period without position movement is normal while mpv switches
        files in an audiobook playlist.  It must not be treated as a user pause.
        """

        book_id = int(book_id)
        with self._lock:
            process = self._players.get(book_id)
            ipc_path = self._ipc_paths.get(book_id)
        if process is None or process.poll() is not None or ipc_path is None:
            return False
        return self._ipc_get(ipc_path, "pause") is True

    def is_playback_active(self, book_id: int) -> bool:
        """Return whether mpv is running and actively advancing audio."""

        book_id = int(book_id)
        with self._lock:
            process = self._players.get(book_id)
            ipc_path = self._ipc_paths.get(book_id)
        if process is None or process.poll() is not None:
            return False
        if ipc_path is None:
            return True

        if self.is_paused(book_id):
            return False
        idle = self._ipc_get(ipc_path, "idle-active")
        if idle is True:
            return False

        with self._lock:
            last_motion_at = getattr(self, "_last_motion_at", {}).get(book_id)
        if (
            last_motion_at is not None
            and time.monotonic() - float(last_motion_at)
            > _PLAYBACK_STALL_SECONDS
        ):
            return False
        return True

    @staticmethod
    def _chapter_for_position(chapters: list[dict[str, Any]], position: float) -> dict[str, Any] | None:
        if not chapters:
            return None
        return next(
            (
                chapter
                for chapter in chapters
                if float(chapter["start"]) <= position < float(chapter["end"])
            ),
            chapters[-1],
        )

    def _bookmarks(self, book_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audiobook_bookmarks WHERE book_id=? ORDER BY position,id", (int(book_id),)
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "position": float(row["position"]),
                "title": str(row["title"] or ""),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    def book(self, book_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM audiobooks WHERE id=?", (int(book_id),)).fetchone()
            chapters = conn.execute(
                "SELECT * FROM audiobook_chapters WHERE book_id=? ORDER BY chapter_index", (int(book_id),)
            ).fetchall()
            file_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM audiobook_files WHERE book_id=?", (int(book_id),)
                ).fetchone()[0]
            )
            identity = conn.execute(
                "SELECT * FROM media_identities WHERE kind='audiobook' AND local_id=?",
                (int(book_id),),
            ).fetchone()
            has_novels = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ln_books'"
            ).fetchone()
            linked_novel = (
                conn.execute(
                    "SELECT b.id,b.title,b.volume,b.anilist_id,b.cover_url FROM reading_audio_links l "
                    "JOIN ln_books b ON b.id=l.ln_book_id WHERE l.audiobook_id=? "
                    "ORDER BY l.updated_at DESC LIMIT 1",
                    (int(book_id),),
                ).fetchone()
                if has_novels is not None
                else None
            )
        if row is None:
            raise KeyError(f"Unknown audiobook id={book_id}")
        chapter_payload = [
            {
                "index": int(chapter["chapter_index"]),
                "title": str(chapter["title"]),
                "start": float(chapter["start"] or 0.0),
                "end": float(chapter["end"] or 0.0),
            }
            for chapter in chapters
        ]
        position = float(row["position"] or 0.0)
        current_chapter = self._chapter_for_position(chapter_payload, position)
        with self._lock:
            deadline = self._sleep_deadlines.get(int(book_id))
            chapter_end = self._sleep_chapter_ends.get(int(book_id))
        return {
            "id": int(row["id"]),
            "path": str(row["path"]),
            "title": str(row["title"]),
            "duration": float(row["duration"] or 0.0),
            "position": position,
            "finished": bool(row["finished"]),
            "playing": self.is_playback_active(int(book_id)),
            "speed": float(self._speeds.get(int(book_id), row["speed"] or 1.0)),
            "multi_file": Path(str(row["path"])).is_dir() or file_count > 1,
            "file_count": file_count,
            "chapters": chapter_payload,
            "current_chapter": current_chapter,
            "bookmarks": self._bookmarks(int(book_id)),
            "sleep_timer_seconds": max(0, round(deadline - time.monotonic())) if deadline else None,
            "sleep_at_chapter_end": chapter_end is not None,
            "transcription": self.transcription_status(int(book_id)),
            "anilist_id": int(identity["anilist_id"]) if identity is not None else (
                int(linked_novel["anilist_id"]) if linked_novel is not None and linked_novel["anilist_id"] is not None else None
            ),
            "anilist_title": str(identity["title"] or "") if identity is not None else (
                str(linked_novel["title"] or "") if linked_novel is not None else ""
            ),
            "anilist_site_url": str(identity["site_url"] or "") if identity is not None else (
                f"https://anilist.co/manga/{int(linked_novel['anilist_id'])}"
                if linked_novel is not None and linked_novel["anilist_id"] is not None else ""
            ),
            "linked_light_novel": dict(linked_novel) if linked_novel is not None else None,
            "cover_url": str(
                (linked_novel["cover_url"] if linked_novel is not None else "")
                or (identity["cover_url"] if identity is not None else "")
                or ""
            ),
        }

    def auto_link_audiobook(self, audiobook_id: int) -> dict[str, Any] | None:
        """Link an unambiguous local LN/audiobook pair and share its AniList identity."""

        audiobook_id = int(audiobook_id)
        with self.db.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ln_books'"
            ).fetchone() is None:
                return None
            audio = conn.execute(
                "SELECT id,path,title FROM audiobooks WHERE id=?", (audiobook_id,)
            ).fetchone()
            if audio is None:
                return None
            existing = conn.execute(
                "SELECT ln_book_id FROM reading_audio_links WHERE audiobook_id=? LIMIT 1",
                (audiobook_id,),
            ).fetchone()
            if existing is not None:
                return {"ln_book_id": int(existing["ln_book_id"]), "audiobook_id": audiobook_id}
            identity = conn.execute(
                "SELECT * FROM media_identities WHERE kind='audiobook' AND local_id=?",
                (audiobook_id,),
            ).fetchone()
            novels = conn.execute(
                "SELECT id,title,file_path,volume,anilist_id,cover_url FROM ln_books ORDER BY updated_at DESC"
            ).fetchall()
        audio_title = str(audio["title"] or Path(str(audio["path"])).stem)
        audio_key = _audiobook_title_key(audio_title)
        audio_volume = _audiobook_volume(audio_title) or _audiobook_volume(str(audio["path"]))
        identity_id = int(identity["anilist_id"]) if identity is not None else None
        ranked: list[tuple[float, Any]] = []
        for novel in novels:
            novel_volume = int(novel["volume"] or 0) or None
            if audio_volume and novel_volume and audio_volume != novel_volume:
                continue
            score = float(fuzz.ratio(audio_key, _audiobook_title_key(str(novel["title"]))))
            if identity_id and novel["anilist_id"] is not None and int(novel["anilist_id"]) == identity_id:
                score = max(score, 120.0)
            if audio_volume and novel_volume == audio_volume:
                score += 8.0
            ranked.append((score, novel))
        ranked.sort(key=lambda item: item[0], reverse=True)
        if not ranked:
            return None
        best_score, novel = ranked[0]
        margin = best_score - (ranked[1][0] if len(ranked) > 1 else 0.0)
        if best_score < 90.0 or margin < 8.0:
            return None
        ln_book_id = int(novel["id"])
        with self.db.connect() as conn:
            occupied = conn.execute(
                "SELECT audiobook_id FROM reading_audio_links WHERE ln_book_id=?",
                (ln_book_id,),
            ).fetchone()
            if occupied is not None and int(occupied["audiobook_id"]) != audiobook_id:
                return None
            if identity is None and novel["anilist_id"] is not None:
                media_id = int(novel["anilist_id"])
                conn.execute(
                    "INSERT OR REPLACE INTO media_identities(kind,local_id,anilist_id,anilist_type,title,cover_url,site_url,updated_at) "
                    "VALUES('audiobook',?,?,?,?,?,?,?)",
                    (
                        audiobook_id,
                        media_id,
                        "MANGA",
                        str(novel["title"] or audio_title),
                        str(novel["cover_url"] or ""),
                        f"https://anilist.co/manga/{media_id}",
                        time.time(),
                    ),
                )
            elif identity_id and novel["anilist_id"] is None:
                conn.execute(
                    "UPDATE ln_books SET anilist_id=?,cover_url=CASE WHEN cover_url='' THEN ? ELSE cover_url END,updated_at=? WHERE id=?",
                    (
                        identity_id,
                        str(identity["cover_url"] or ""),
                        time.time(),
                        ln_book_id,
                    ),
                )
        self.link_light_novel(ln_book_id, audiobook_id)
        return {"ln_book_id": ln_book_id, "audiobook_id": audiobook_id, "score": best_score}

    def auto_link_light_novel(self, ln_book_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ln_books'"
            ).fetchone() is None:
                return None
            linked = conn.execute(
                "SELECT audiobook_id FROM reading_audio_links WHERE ln_book_id=?",
                (int(ln_book_id),),
            ).fetchone()
            audiobook_ids = [
                int(row["id"])
                for row in conn.execute("SELECT id FROM audiobooks ORDER BY updated_at DESC")
            ]
        if linked is not None:
            return {"ln_book_id": int(ln_book_id), "audiobook_id": int(linked["audiobook_id"])}
        for audiobook_id in audiobook_ids:
            result = self.auto_link_audiobook(audiobook_id)
            if result and int(result["ln_book_id"]) == int(ln_book_id):
                return result
        return None

    def state(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id FROM audiobooks ORDER BY updated_at DESC,id DESC").fetchall()
        books = [self.book(int(row["id"])) for row in rows]
        # Import starts STT immediately. If Pudge was closed before it finished,
        # opening the audiobook library resumes the missing cached job without
        # requiring the Light Novel reader to be open.
        for book in books:
            transcription = book.get("transcription") or {}
            if str(transcription.get("status") or "") == "queued" and not transcription.get("ready"):
                book["transcription"] = self.prepare_transcription(int(book["id"]))
        return {"books": books}

    def resume_pending_transcriptions(self) -> int:
        """Restart uncached audiobook analysis without waiting for the UI."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id FROM audiobooks ORDER BY updated_at DESC,id DESC").fetchall()
            links = conn.execute(
                "SELECT ln_book_id FROM reading_audio_links ORDER BY ln_book_id"
            ).fetchall()
        resumed = 0
        for row in rows:
            audiobook_id = int(row["id"])
            if self._load_transcript(audiobook_id) is not None:
                continue
            self.prepare_transcription(audiobook_id)
            resumed += 1
        for row in links:
            try:
                self.prepare_alignment(int(row["ln_book_id"]))
            except Exception:
                continue
        return resumed

    def set_position(self, book_id: int, position: float) -> None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT duration FROM audiobooks WHERE id=?", (int(book_id),)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown audiobook id={book_id}")
            duration = float(row["duration"] or 0.0)
            value = max(0.0, min(float(position), duration or float(position)))
            finished = bool(duration > 0 and value >= duration * 0.98)
            conn.execute(
                "UPDATE audiobooks SET position=?,finished=?,updated_at=? WHERE id=?",
                (value, int(finished), time.time(), int(book_id)),
            )

    @staticmethod
    def _ipc_command(ipc_path: Path, command: list[Any]) -> dict[str, Any] | None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1.0)
                client.connect(str(ipc_path))
                client.sendall(json.dumps({"command": command}).encode("utf-8") + b"\n")
                payload = json.loads(client.recv(4096).decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None


    @staticmethod
    def _ipc_commands_no_wait(
        ipc_path: Path,
        commands: list[list[Any]],
        *,
        timeout: float = _STOP_IPC_TIMEOUT,
    ) -> bool:
        # Queue mpv IPC commands without waiting for replies.
        try:
            payload = b"".join(
                json.dumps({"command": command}).encode("utf-8") + b"\n"
                for command in commands
            )
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(max(0.01, float(timeout)))
                client.connect(str(ipc_path))
                client.sendall(payload)
            return True
        except (OSError, ValueError, TypeError):
            return False

    @staticmethod
    def _wait_or_kill(
        process: subprocess.Popen[Any],
        *,
        timeout: float = _STOP_GRACE_SECONDS,
    ) -> None:
        try:
            process.wait(timeout=max(0.01, float(timeout)))
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except OSError:
            return
        try:
            process.wait(timeout=max(0.01, float(timeout)))
        except subprocess.TimeoutExpired:
            pass

    @classmethod
    def _ipc_get(cls, ipc_path: Path, property_name: str) -> Any:
        payload = cls._ipc_command(ipc_path, ["get_property", property_name])
        return payload.get("data") if payload else None

    def _global_position(self, book_id: int, ipc_path: Path) -> float | None:
        raw = self._ipc_get(ipc_path, "time-pos")
        try:
            local = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            local = None
        if local is None:
            return None
        files = self._file_rows(book_id)
        if len(files) <= 1:
            return local
        raw_index = self._ipc_get(ipc_path, "playlist-pos")
        try:
            index = int(raw_index) if raw_index is not None else 0
        except (TypeError, ValueError):
            index = 0
        match = next((row for row in files if int(row["file_index"]) == index), None)
        return float(match["start"] if match else 0.0) + local

    def _sleep_reached(self, book_id: int, position: float | None) -> bool:
        with self._lock:
            deadline = self._sleep_deadlines.get(book_id)
            chapter_end = self._sleep_chapter_ends.get(book_id)
        return bool(
            (deadline is not None and time.monotonic() >= deadline)
            or (chapter_end is not None and position is not None and position >= chapter_end - 0.25)
        )


    def _monitor(self, book_id: int, process: subprocess.Popen[Any], ipc_path: Path) -> None:
        last_saved = 0.0
        last_position: float | None = None
        try:
            while process.poll() is None:
                position = self._global_position(book_id, ipc_path)
                if position is not None:
                    last_position = position
                    with self._lock:
                        current_process = self._players.get(int(book_id))
                    if current_process is process:
                        self._record_playback_position(book_id, position)
                    if time.monotonic() - last_saved >= _POSITION_WRITE_INTERVAL:
                        self.set_position(book_id, position)
                        last_saved = time.monotonic()
                if self._sleep_reached(book_id, position):
                    self._ipc_commands_no_wait(
                        ipc_path,
                        [
                            ["set_property", "mute", True],
                            ["set_property", "pause", True],
                            ["quit"],
                        ],
                    )
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    if position is not None:
                        self.set_position(book_id, position)
                    self._wait_or_kill(process)
                    break
                time.sleep(_MONITOR_POLL_INTERVAL)
        finally:
            if last_position is not None:
                self.set_position(book_id, last_position)
            ipc_path.unlink(missing_ok=True)
            with self._lock:
                if self._players.get(int(book_id)) is process:
                    self._players.pop(int(book_id), None)
                    self._ipc_paths.pop(int(book_id), None)
                    self._last_positions.pop(int(book_id), None)
                    getattr(self, "_last_motion_at", {}).pop(int(book_id), None)
                self._sleep_deadlines.pop(int(book_id), None)
                self._sleep_chapter_ends.pop(int(book_id), None)


    def stop(self, book_id: int) -> dict[str, Any]:
        book_id = int(book_id)
        with self._lock:
            process = self._players.get(book_id)
            ipc_path = self._ipc_paths.get(book_id)
            final_position = self._last_positions.get(book_id)
        if process is None or process.poll() is not None:
            return {"ok": True, "book": self.book(book_id), "stopped": False}

        if ipc_path is not None:
            # Do not wait for mpv replies here. Muting and quitting are queued in
            # one short IPC write, while process termination is the hard fallback.
            self._ipc_commands_no_wait(
                ipc_path,
                [
                    ["set_property", "mute", True],
                    ["set_property", "pause", True],
                    ["quit"],
                ],
            )

        try:
            process.terminate()
        except OSError:
            pass

        # Position is continuously cached by the monitor, so Stop never performs
        # the former sequence of blocking IPC reads before silencing playback.
        if final_position is not None:
            self.set_position(book_id, final_position)

        self._wait_or_kill(process)

        with self._lock:
            if self._players.get(book_id) is process:
                self._players.pop(book_id, None)
                self._ipc_paths.pop(book_id, None)
                self._last_positions.pop(book_id, None)
                getattr(self, "_last_motion_at", {}).pop(book_id, None)
            self._sleep_deadlines.pop(book_id, None)
            self._sleep_chapter_ends.pop(book_id, None)
        if ipc_path is not None:
            ipc_path.unlink(missing_ok=True)
        return {"ok": True, "book": self.book(book_id), "stopped": True}

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._players)
            alignment_processes = list(self._alignment_processes.values())
            transcription_processes = list(self._transcription_processes.values())
        for book_id in ids:
            try:
                self.stop(book_id)
            except Exception:
                continue
        for process in [*alignment_processes, *transcription_processes]:
            if process.poll() is None:
                process.terminate()

    def play(self, book_id: int, start: float | None = None, speed: float = 1.0) -> dict[str, Any]:
        book_id = int(book_id)
        speed = max(0.5, min(3.0, float(speed or 1.0)))
        if self.is_playing(book_id):
            self.stop(book_id)
        book = self.book(book_id)
        position = float(book["position"] if start is None else start)
        # A small smart rewind makes returning after a break less disorienting.
        if start is None and position > 12:
            with self.db.connect() as conn:
                row = conn.execute("SELECT last_played_at FROM audiobooks WHERE id=?", (book_id,)).fetchone()
            if row is not None and time.time() - float(row["last_played_at"] or 0.0) > 5 * 60:
                position = max(0.0, position - 10.0)
        files = self._file_rows(book_id)
        if not files:
            path = Path(str(book["path"]))
            if not path.is_file():
                raise FileNotFoundError(path)
            files = [
                {
                    "file_index": 0,
                    "path": str(path),
                    "start": 0.0,
                    "end": float(book["duration"] or 0.0),
                }
            ]
        selected_index = 0
        local_start = position
        for row in files:
            start_at = float(row.get("start") or 0.0)
            end_at = float(row.get("end") or start_at)
            if position >= start_at:
                selected_index = int(row.get("file_index") or 0)
                local_start = max(0.0, position - start_at)
            if position < end_at:
                break

        ipc_dir = self.cache_dir / "audiobook-ipc"
        ipc_dir.mkdir(parents=True, exist_ok=True)
        ipc_path = ipc_dir / f"book-{book_id}-{os.getpid()}.sock"
        ipc_path.unlink(missing_ok=True)
        command = [
            self.mpv,
            # Audiobooks and paired LN reading must never load the user's
            # normal mpv configuration. In particular, JitenMPV and
            # jpdb-mpv-plugin are video study integrations and can spawn
            # their own GUI processes even though audiobook playback has no
            # video window.
            "--no-config",
            "--load-scripts=no",
            "--no-video",
            "--force-window=no",
        ]
        command.extend(self._tempo_filter_args())
        command.extend(
            [
                f"--speed={speed:.3f}",
                "--audio-display=no",
                f"--input-ipc-server={ipc_path}",
            ]
        )
        if len(files) > 1:
            command.append(f"--playlist-start={selected_index}")

        # mpv command-line options normally apply to every file in a playlist.
        # Keep the resume offset local to the initially selected file, or the
        # same offset is applied again when mpv advances to the next chapter.
        for row in files:
            path_value = str(row["path"])
            if int(row.get("file_index") or 0) == selected_index and local_start > 0:
                command.extend(
                    [
                        "--{",
                        f"--start={max(0.0, local_start):.3f}",
                        path_value,
                        "--}",
                    ]
                )
            else:
                command.append(path_value)
        process = subprocess.Popen(command)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE audiobooks SET speed=?,last_played_at=?,updated_at=? WHERE id=?",
                (speed, time.time(), time.time(), book_id),
            )
        with self._lock:
            self._players[book_id] = process
            self._ipc_paths[book_id] = ipc_path
            self._last_positions[book_id] = position
            self._last_motion_at[book_id] = time.monotonic()
            self._speeds[book_id] = speed
        threading.Thread(
            target=self._monitor,
            args=(book_id, process, ipc_path),
            name=f"audiobook-{book_id}",
            daemon=True,
        ).start()
        return {"ok": True, "book_id": book_id, "position": position, "playing": True, "speed": speed}

    def set_paused(self, book_id: int, paused: bool) -> dict[str, Any]:
        book_id = int(book_id)
        with self._lock:
            process = self._players.get(book_id)
            ipc_path = self._ipc_paths.get(book_id)
        if process is None or process.poll() is not None or ipc_path is None:
            return {
                "ok": False,
                "book_id": book_id,
                "playing": False,
                "player_running": False,
            }

        value = bool(paused)
        # Transport controls are latency-sensitive: writing pause/resume to the
        # local mpv socket is enough. Waiting for mpv's JSON reply made a space
        # press visibly lag behind the reader highlight. The paired-state poll
        # verifies the resulting state immediately afterwards.
        sent = self._ipc_commands_no_wait(
            ipc_path,
            [["set_property", "pause", value]],
        )
        if sent and not value:
            with self._lock:
                self._last_motion_at[book_id] = time.monotonic()
        return {
            "ok": bool(sent),
            "book_id": book_id,
            "playing": not value,
            "player_running": True,
            "paused": value,
        }

    def set_speed(self, book_id: int, speed: float) -> dict[str, Any]:
        book_id = int(book_id)
        value = max(0.5, min(3.0, float(speed or 1.0)))
        with self._lock:
            ipc_path = self._ipc_paths.get(book_id)
            self._speeds[book_id] = value
        with self.db.connect() as conn:
            conn.execute("UPDATE audiobooks SET speed=?,updated_at=? WHERE id=?", (value, time.time(), book_id))
        if ipc_path is not None and self.is_playing(book_id):
            applied = False
            for _attempt in range(3):
                response = self._ipc_command(ipc_path, ["set_property", "speed", value])
                live_speed = self._ipc_get(ipc_path, "speed")
                try:
                    applied = bool(
                        response is not None
                        and response.get("error") == "success"
                        and live_speed is not None
                        and abs(float(live_speed) - value) < 0.01
                    )
                except (TypeError, ValueError):
                    applied = False
                if applied:
                    break
                time.sleep(0.04)
            if not applied:
                position = self._global_position(book_id, ipc_path)
                self.play(
                    book_id,
                    start=float(position if position is not None else self.book(book_id)["position"]),
                    speed=value,
                )
        return {"ok": True, "book_id": book_id, "speed": value, "book": self.book(book_id)}

    def seek(self, book_id: int, seconds: float) -> dict[str, Any]:
        book_id = int(book_id)
        delta = float(seconds)
        with self._lock:
            ipc_path = self._ipc_paths.get(book_id)
        if ipc_path is not None and self.is_playing(book_id):
            self._ipc_command(ipc_path, ["seek", delta, "relative", "exact"])
            time.sleep(0.03)
            position = self._global_position(book_id, ipc_path)
            if position is not None:
                self.set_position(book_id, position)
        else:
            book = self.book(book_id)
            self.set_position(book_id, float(book["position"] or 0.0) + delta)
        return {"ok": True, "book": self.book(book_id)}

    def seek_to(self, book_id: int, position: float) -> dict[str, Any]:
        book_id = int(book_id)
        book = self.book(book_id)
        value = max(0.0, min(float(position), float(book["duration"] or position)))
        if self.is_playing(book_id):
            files = self._file_rows(book_id)
            selected = next(
                (
                    row
                    for row in files
                    if float(row.get("start") or 0.0)
                    <= value
                    < float(row.get("end") or value + 0.001)
                ),
                files[-1] if files else None,
            )
            with self._lock:
                ipc_path = self._ipc_paths.get(book_id)
            current_index = self._ipc_get(ipc_path, "playlist-pos") if ipc_path else None
            selected_index = int(selected.get("file_index") or 0) if selected else 0
            same_file = len(files) <= 1 or (
                current_index is not None and int(current_index) == selected_index
            )
            local_position = value - float(selected.get("start") or 0.0) if selected else value
            response = (
                self._ipc_command(
                    ipc_path,
                    ["seek", max(0.0, local_position), "absolute", "exact"],
                )
                if ipc_path is not None and same_file
                else None
            )
            if response is None or response.get("error") != "success":
                self.play(book_id, start=value, speed=float(book["speed"] or 1.0))
            self.set_position(book_id, value)
        else:
            self.set_position(book_id, value)
        return {"ok": True, "book": self.book(book_id)}

    def set_sleep_timer(
        self,
        book_id: int,
        *,
        seconds: float | None = None,
        end_of_chapter: bool = False,
    ) -> dict[str, Any]:
        book_id = int(book_id)
        book = self.book(book_id)
        position = float(book["position"] or 0.0)
        with self._lock:
            ipc_path = self._ipc_paths.get(book_id)
        if ipc_path is not None:
            live = self._global_position(book_id, ipc_path)
            if live is not None:
                position = live
        with self._lock:
            self._sleep_deadlines.pop(book_id, None)
            self._sleep_chapter_ends.pop(book_id, None)
            if end_of_chapter:
                chapter = self._chapter_for_position(book["chapters"], position)
                if chapter is not None:
                    self._sleep_chapter_ends[book_id] = float(chapter["end"])
            elif seconds is not None and float(seconds) > 0:
                self._sleep_deadlines[book_id] = time.monotonic() + float(seconds)
        return {"ok": True, "book": self.book(book_id)}

    def add_bookmark(self, book_id: int, title: str = "") -> dict[str, Any]:
        book_id = int(book_id)
        book = self.book(book_id)
        position = float(book["position"] or 0.0)
        with self._lock:
            ipc_path = self._ipc_paths.get(book_id)
        if ipc_path is not None:
            live = self._global_position(book_id, ipc_path)
            if live is not None:
                position = live
        label = str(title or "").strip() or f"{int(position // 60)}:{int(position % 60):02d}"
        with self.db.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO audiobook_bookmarks(book_id,position,title,created_at) VALUES(?,?,?,?)",
                (book_id, position, label, time.time()),
            )
            bookmark_id = int(cursor.lastrowid)
        return {"ok": True, "bookmark_id": bookmark_id, "book": self.book(book_id)}

    def delete_bookmark(self, bookmark_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT book_id FROM audiobook_bookmarks WHERE id=?", (int(bookmark_id),)
            ).fetchone()
            if row is None:
                return {"ok": False}
            book_id = int(row["book_id"])
            conn.execute("DELETE FROM audiobook_bookmarks WHERE id=?", (int(bookmark_id),))
        return {"ok": True, "book": self.book(book_id)}

    def mark_finished(self, book_id: int, finished: bool = True) -> dict[str, Any]:
        if self.is_playing(int(book_id)):
            self.stop(int(book_id))
        book = self.book(int(book_id))
        position = float(book["duration"] or 0.0) if finished else 0.0
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE audiobooks SET position=?,finished=?,updated_at=? WHERE id=?",
                (position, int(bool(finished)), time.time(), int(book_id)),
            )
        return {"ok": True, "book": self.book(int(book_id))}

    def _transcript_fingerprint(self, audiobook_id: int) -> str:
        digest = hashlib.sha256(f"audiobook-stt-v2\0{self.stt_model}\0".encode())
        for row in self._file_rows(int(audiobook_id)):
            path = Path(str(row["path"]))
            try:
                stat = path.stat()
                identity = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
            except OSError:
                identity = str(path)
            digest.update(
                f"{int(row['file_index'])}:{identity}:{row['start']}:{row['end']}\0".encode(
                    "utf-8", errors="replace"
                )
            )
        return digest.hexdigest()[:28]

    def _transcript_path(self, audiobook_id: int) -> Path:
        return (
            self.cache_dir
            / "audiobook-transcripts"
            / f"{self._transcript_fingerprint(int(audiobook_id))}.json"
        )

    def _activity_fingerprint(self, audiobook_id: int) -> str:
        digest = hashlib.sha256(b"audiobook-activity-v1\0")
        for row in self._file_rows(int(audiobook_id)):
            path = Path(str(row["path"]))
            try:
                stat = path.stat()
                identity = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
            except OSError:
                identity = str(path)
            digest.update(
                f"{int(row['file_index'])}:{identity}:{row['start']}:{row['end']}\0".encode(
                    "utf-8", errors="replace"
                )
            )
        return digest.hexdigest()[:28]

    def _activity_path(self, audiobook_id: int) -> Path:
        return (
            self.cache_dir
            / "audiobook-activity"
            / f"{self._activity_fingerprint(int(audiobook_id))}.json"
        )

    def _resolved_ffmpeg(self) -> str:
        resolved = (
            shutil.which(self.ffmpeg)
            if os.sep not in self.ffmpeg
            else str(Path(self.ffmpeg).expanduser())
        )
        if not resolved or not Path(resolved).is_file():
            raise FileNotFoundError(
                f"Configured ffmpeg was not found: {self.ffmpeg}. Re-run install.sh."
            )
        return str(Path(resolved).resolve())

    @staticmethod
    def _transcript_activity_regions(
        segments: list[dict[str, Any]],
    ) -> list[dict[str, float]]:
        regions: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            words = segment.get("words")
            rows = words if isinstance(words, list) and words else [segment]
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    start = float(row.get("start") or segment.get("start") or 0.0)
                    end = float(row.get("end") or segment.get("end") or start)
                except (TypeError, ValueError):
                    continue
                regions.append({"start": start, "end": end})
        return merge_activity_regions(regions, bridge_seconds=0.10)

    def _load_or_analyze_activity(
        self,
        audiobook_id: int,
        transcript_segments: list[dict[str, Any]],
    ) -> tuple[list[dict[str, float]], str]:
        output = self._activity_path(int(audiobook_id))
        try:
            cached = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            cached = None
        if isinstance(cached, dict) and cached.get("schema") == "audiobook-activity-v1":
            regions = merge_activity_regions(cached.get("regions") or [], bridge_seconds=0.0)
            if regions:
                return regions, "fft"

        rows = self._file_rows(int(audiobook_id))
        regions: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        try:
            ffmpeg = self._resolved_ffmpeg()
            for row in rows:
                source = Path(str(row["path"]))
                payload = analyze_audio_activity(source, ffmpeg=ffmpeg)
                offset = float(row.get("start") or 0.0)
                for region in payload.get("regions") or []:
                    regions.append(
                        {
                            "start": float(region["start"]) + offset,
                            "end": float(region["end"]) + offset,
                        }
                    )
                diagnostics.append(
                    {
                        key: value
                        for key, value in payload.items()
                        if key not in {"regions", "schema"}
                    }
                )
            merged = gate_activity_regions(
                merge_activity_regions(regions, bridge_seconds=0.0),
                self._transcript_activity_regions(transcript_segments),
            )
            if not merged:
                raise ValueError("FFT analysis found no speech activity")
            payload = {
                "schema": "audiobook-activity-v1",
                "created_at": time.time(),
                "regions": merged,
                "files": diagnostics,
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(output)
            return merged, "fft"
        except Exception:
            return self._transcript_activity_regions(transcript_segments), "stt"

    def _load_transcript(self, audiobook_id: int) -> dict[str, Any] | None:
        path = self._transcript_path(int(audiobook_id))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return (
            payload
            if isinstance(payload, dict)
            and payload.get("schema") == "audiobook-stt-v2"
            and isinstance(payload.get("segments"), list)
            else None
        )

    def transcription_status(self, audiobook_id: int) -> dict[str, Any]:
        audiobook_id = int(audiobook_id)
        cached = self._load_transcript(audiobook_id)
        if cached is not None:
            return {
                "status": "ready",
                "ready": True,
                "model": str(cached.get("model") or self.stt_model),
                "segment_count": len(cached.get("segments") or []),
            }
        with self._lock:
            job = dict(self._transcription_jobs.get(audiobook_id) or {})
        if job and str(job.get("status") or "") in {"queued", "transcribing"}:
            job["background"] = True
            job["elapsed_seconds"] = max(
                0.0,
                time.time() - float(job.get("started_at") or time.time()),
            )
        return job or {"status": "queued", "ready": False, "background": True}

    def _set_transcription_job(self, audiobook_id: int, payload: dict[str, Any]) -> None:
        with self._lock:
            current = dict(self._transcription_jobs.get(int(audiobook_id)) or {})
            now = time.time()
            updated = {
                **current,
                **payload,
                "audiobook_id": int(audiobook_id),
                "started_at": float(payload.get("started_at") or current.get("started_at") or now),
                "updated_at": now,
            }
            self._transcription_jobs[int(audiobook_id)] = updated
        job_id = str(updated.get("job_id") or "")
        if self.job_center is None or not job_id:
            return
        status = str(updated.get("status") or "queued")
        if status == "ready":
            self.job_center.finish(
                job_id,
                message="Transcription ready",
                result={"audiobook_id": int(audiobook_id), "segment_count": updated.get("segment_count", 0)},
            )
        elif status == "error":
            self.job_center.fail(job_id, updated.get("error") or "Japanese STT failed")
        elif status == "cancelled":
            self.job_center.cancelled(job_id)
        else:
            self.job_center.update(
                job_id,
                state="running" if status == "transcribing" else "queued",
                current=float(updated.get("processed_audio_seconds") or 0.0),
                total=float(updated.get("total_duration") or 0.0),
                message=(
                    f"STT file {int(updated.get('file') or 1)}/{int(updated.get('file_count') or 1)}"
                    if status == "transcribing"
                    else "Queued"
                ),
            )

    def _transcribe_worker(self, audiobook_id: int, output: Path, event: threading.Event) -> None:
        with self._lock:
            cancel_event = self._transcription_cancel_events.get(audiobook_id)
        heavy_lease = None
        if self.work_scheduler is not None:
            heavy_lease = self.work_scheduler.acquire_heavy(
                "audiobook-stt",
                blocking=True,
                foreground_sensitive=True,
                wait_for_foreground=True,
                cancel_event=cancel_event,
            )
            if heavy_lease is None:
                self._set_transcription_job(
                    audiobook_id,
                    {"status": "cancelled", "ready": False, "error": ""},
                )
                event.set()
                return
        output.parent.mkdir(parents=True, exist_ok=True)
        work_dir = Path(tempfile.mkdtemp(prefix=f".{output.stem}-work-", dir=output.parent))
        try:
            files = self._file_rows(int(audiobook_id))
            if not files:
                raise ValueError("Audiobook has no readable files")
            all_segments: list[dict[str, Any]] = []
            started_at = time.time()
            total_duration = sum(max(0.0, float(row.get("duration") or 0.0)) for row in files)
            completed_duration = 0.0
            for file_number, row in enumerate(files, 1):
                with self._lock:
                    cancel_event = self._transcription_cancel_events.get(audiobook_id)
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Transcription cancelled")
                source = Path(str(row["path"]))
                if not source.is_file():
                    raise FileNotFoundError(source)
                file_duration = max(0.0, float(row.get("duration") or 0.0))
                self._set_transcription_job(
                    audiobook_id,
                    {
                        "status": "transcribing",
                        "ready": False,
                        "file": file_number,
                        "file_count": len(files),
                        "file_progress_percent": 0,
                        "progress_percent": (
                            completed_duration / total_duration * 100.0 if total_duration else 0.0
                        ),
                        "processed_audio_seconds": completed_duration,
                        "remaining_audio_seconds": max(0.0, total_duration - completed_duration),
                        "total_duration": total_duration,
                        "started_at": started_at,
                    },
                )
                result_path = work_dir / f"file-{file_number:04d}.json"
                progress_path = work_dir / f"file-{file_number:04d}.progress.json"
                stdout_path = work_dir / f"file-{file_number:04d}.stdout.log"
                stderr_path = work_dir / f"file-{file_number:04d}.stderr.log"
                timeout = max(
                    30 * 60,
                    min(12 * 3600, float(row.get("duration") or 0.0) * 3.0),
                )
                command = [
                    self.python,
                    "-m",
                    "pudge.subtitles.stt_worker",
                    "--words",
                    str(source),
                    str(result_path),
                    self.stt_model,
                    str(progress_path),
                ]
                environment = os.environ.copy()
                ffmpeg = self._resolved_ffmpeg()
                environment["PATH"] = os.pathsep.join(
                    part
                    for part in (str(Path(ffmpeg).resolve().parent), environment.get("PATH", ""))
                    if part
                )
                with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
                    "w", encoding="utf-8"
                ) as stderr_file:
                    process = subprocess.Popen(
                        command,
                        env=environment,
                        text=True,
                        stdout=stdout_file,
                        stderr=stderr_file,
                    )
                    with self._lock:
                        self._transcription_processes[audiobook_id] = process
                    file_started = time.monotonic()
                    reported_percent = -1
                    try:
                        while process.poll() is None:
                            if cancel_event is not None and cancel_event.is_set():
                                process.terminate()
                                try:
                                    process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    process.wait(timeout=5)
                                raise InterruptedError("Transcription cancelled")
                            if time.monotonic() - file_started >= timeout:
                                process.terminate()
                                try:
                                    process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    process.kill()
                                    process.wait(timeout=5)
                                raise subprocess.TimeoutExpired(command, timeout)
                            try:
                                progress = json.loads(progress_path.read_text(encoding="utf-8"))
                                current_percent = max(
                                    0,
                                    min(100, int(progress.get("percent") or 0)),
                                )
                            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                                current_percent = reported_percent
                            if current_percent >= 0 and current_percent != reported_percent:
                                reported_percent = current_percent
                                processed = completed_duration + file_duration * current_percent / 100.0
                                self._set_transcription_job(
                                    audiobook_id,
                                    {
                                        "status": "transcribing",
                                        "ready": False,
                                        "file": file_number,
                                        "file_count": len(files),
                                        "file_progress_percent": current_percent,
                                        "progress_percent": (
                                            processed / total_duration * 100.0
                                            if total_duration
                                            else float(current_percent)
                                        ),
                                        "processed_audio_seconds": processed,
                                        "remaining_audio_seconds": max(0.0, total_duration - processed),
                                        "total_duration": total_duration,
                                        "started_at": started_at,
                                    },
                                )
                            time.sleep(0.35)
                    finally:
                        with self._lock:
                            if self._transcription_processes.get(audiobook_id) is process:
                                self._transcription_processes.pop(audiobook_id, None)
                stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("Transcription cancelled")
                if process.returncode != 0 or not result_path.is_file():
                    error = (stderr or stdout).strip()[-1200:]
                    raise RuntimeError(error or "Japanese STT failed")
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                segments = payload.get("segments") if isinstance(payload, dict) else None
                if not isinstance(segments, list):
                    raise ValueError("STT returned no timestamped segments")
                all_segments.extend(
                    self._shift_transcription_segments(
                        [item for item in segments if isinstance(item, dict)],
                        float(row.get("start") or 0.0),
                    )
                )
                completed_duration += file_duration
            payload = {
                "schema": "audiobook-stt-v2",
                "model": self.stt_model,
                "created_at": time.time(),
                "segments": all_segments,
            }
            temporary = work_dir / f"{output.name}.tmp"
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(output)
            self._set_transcription_job(
                audiobook_id,
                {"status": "ready", "ready": True, "segment_count": len(all_segments)},
            )
            # A link may have been created while this audiobook was being
            # transcribed.  Start its cheap text alignment immediately.
            with self.db.connect() as conn:
                links = conn.execute(
                    "SELECT ln_book_id FROM reading_audio_links WHERE audiobook_id=?",
                    (audiobook_id,),
                ).fetchall()
            for row in links:
                try:
                    self.prepare_alignment(int(row["ln_book_id"]))
                except Exception:
                    continue
        except InterruptedError:
            self._set_transcription_job(
                audiobook_id,
                {"status": "cancelled", "ready": False, "error": ""},
            )
        except subprocess.TimeoutExpired:
            self._set_transcription_job(
                audiobook_id,
                {"status": "error", "ready": False, "error": "Japanese STT timed out"},
            )
        except Exception as exc:
            self._set_transcription_job(
                audiobook_id,
                {"status": "error", "ready": False, "error": str(exc)},
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            with self._lock:
                self._transcription_cancel_events.pop(audiobook_id, None)
            if heavy_lease is not None:
                heavy_lease.release()
            event.set()

    def prepare_transcription(
        self,
        audiobook_id: int,
        *,
        force: bool = False,
        attempt_of: str = "",
    ) -> dict[str, Any]:
        audiobook_id = int(audiobook_id)
        book = self.book(audiobook_id)
        output = self._transcript_path(audiobook_id)
        if force:
            output.unlink(missing_ok=True)
        if output.is_file():
            return self.transcription_status(audiobook_id)
        with self._lock:
            current = self._transcription_jobs.get(audiobook_id) or {}
            if current.get("status") in {"queued", "transcribing"}:
                return dict(current)
            event = threading.Event()
            cancel_event = threading.Event()
            started_at = time.time()
            total_duration = sum(
                max(0.0, float(row.get("duration") or 0.0))
                for row in self._file_rows(audiobook_id)
            )
            self._transcription_events[audiobook_id] = event
            self._transcription_cancel_events[audiobook_id] = cancel_event
            job_id = (
                self.job_center.start(
                    "stt",
                    f"STT · {book['title']}",
                    payload={"audiobook_id": audiobook_id},
                    total=total_duration,
                    attempt_of=str(attempt_of or ""),
                )
                if self.job_center is not None
                else ""
            )
            self._transcription_jobs[audiobook_id] = {
                "status": "queued",
                "ready": False,
                "audiobook_id": audiobook_id,
                "background": True,
                "progress_percent": 0.0,
                "processed_audio_seconds": 0.0,
                "remaining_audio_seconds": total_duration,
                "total_duration": total_duration,
                "started_at": started_at,
                "updated_at": started_at,
                "job_id": job_id,
            }
        threading.Thread(
            target=self._transcribe_worker,
            args=(audiobook_id, output, event),
            name=f"audiobook-stt-{audiobook_id}",
            daemon=True,
        ).start()
        return self.transcription_status(audiobook_id)

    def cancel_transcription(self, audiobook_id: int) -> dict[str, Any]:
        audiobook_id = int(audiobook_id)
        with self._lock:
            cancel_event = self._transcription_cancel_events.get(audiobook_id)
            process = self._transcription_processes.get(audiobook_id)
            job = dict(self._transcription_jobs.get(audiobook_id) or {})
        if cancel_event is None:
            return self.transcription_status(audiobook_id)
        cancel_event.set()
        job_id = str(job.get("job_id") or "")
        if self.job_center is not None and job_id:
            self.job_center.request_cancel(job_id)
        if process is not None and process.poll() is None:
            process.terminate()
        return {**self.transcription_status(audiobook_id), "cancel_requested": True}

    def _alignment_fingerprint(self, ln_book_id: int, audiobook_id: int) -> str:
        digest = hashlib.sha256(f"reading-audio-v3-punctuation-clock-v2\0{self.stt_model}\0".encode())
        with self.db.connect() as conn:
            chapters = conn.execute(
                "SELECT chapter_index,title,text_hash FROM ln_chapters "
                "WHERE book_id=? ORDER BY chapter_index",
                (int(ln_book_id),),
            ).fetchall()
            files = conn.execute(
                "SELECT file_index,path,start,end FROM audiobook_files "
                "WHERE book_id=? ORDER BY file_index",
                (int(audiobook_id),),
            ).fetchall()
        for row in chapters:
            digest.update(
                f"c:{int(row['chapter_index'])}:{row['title']}:{row['text_hash']}\0".encode(
                    "utf-8", errors="replace"
                )
            )
        for row in files:
            path = Path(str(row["path"]))
            try:
                stat = path.stat()
                identity = f"{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
            except OSError:
                identity = str(path)
            digest.update(
                f"a:{int(row['file_index'])}:{identity}:{row['start']}:{row['end']}\0".encode(
                    "utf-8", errors="replace"
                )
            )
        return digest.hexdigest()[:28]

    def _alignment_path(self, ln_book_id: int, audiobook_id: int) -> Path:
        fingerprint = self._alignment_fingerprint(int(ln_book_id), int(audiobook_id))
        return self.cache_dir / "reading-audio-alignment" / f"{fingerprint}.json"

    def _alignment_report_path(self, ln_book_id: int, audiobook_id: int) -> Path:
        return self._alignment_path(int(ln_book_id), int(audiobook_id)).with_suffix(".report.json")

    def _load_alignment_report(self, ln_book_id: int, audiobook_id: int) -> dict[str, Any] | None:
        path = self._alignment_report_path(int(ln_book_id), int(audiobook_id))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) and payload.get("schema") == "pudge-alignment-report-v1" else None

    def _load_alignment(self, ln_book_id: int, audiobook_id: int) -> dict[str, Any] | None:
        path = self._alignment_path(int(ln_book_id), int(audiobook_id))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return (
            payload
            if isinstance(payload, dict) and payload.get("schema") == "reading-audio-v3"
            else None
        )

    def alignment_status(self, ln_book_id: int) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False)
        if link is None:
            return {"status": "unlinked", "ready": False}
        audiobook_id = int(link["book"]["id"])
        alignment = self._load_alignment(int(ln_book_id), audiobook_id)
        if alignment is not None:
            report = self._load_alignment_report(int(ln_book_id), audiobook_id)
            summary = dict(report.get("summary") or {}) if isinstance(report, dict) else {}
            return {
                "status": "ready",
                "ready": True,
                "model": str(alignment.get("model") or self.stt_model),
                "confidence": float(alignment.get("confidence") or 0.0),
                "matched_chapters": len(alignment.get("chapters") or []),
                "anchor_count": int(alignment.get("anchor_count") or 0),
                "timing_source": str(alignment.get("timing_source") or "stt"),
                "quality_grade": str(report.get("grade") or "unknown") if isinstance(report, dict) else "unknown",
                "coverage": float(summary.get("coverage") or 0.0),
                "warning_count": int(summary.get("warning_count") or 0),
            }
        with self._lock:
            job = dict(self._alignment_jobs.get(int(ln_book_id)) or {})
        if job and int(job.get("audiobook_id") or audiobook_id) != audiobook_id:
            job = {}
        transcription = self.transcription_status(audiobook_id)
        if not transcription.get("ready") and str(transcription.get("status") or "") in {
            "queued",
            "transcribing",
            "error",
        }:
            return {
                **transcription,
                "phase": "transcription",
                "audiobook_id": audiobook_id,
            }
        return job or {"status": "not_prepared", "ready": False}

    @staticmethod
    def _shift_transcription_segments(
        segments: list[dict[str, Any]], offset: float
    ) -> list[dict[str, Any]]:
        shifted: list[dict[str, Any]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            clone = dict(segment)
            for key in ("start", "end"):
                try:
                    clone[key] = float(clone.get(key) or 0.0) + float(offset)
                except (TypeError, ValueError):
                    pass
            words = clone.get("words")
            if isinstance(words, list):
                clone["words"] = []
                for word in words:
                    if not isinstance(word, dict):
                        continue
                    word_clone = dict(word)
                    for key in ("start", "end"):
                        try:
                            word_clone[key] = float(word_clone.get(key) or 0.0) + float(offset)
                        except (TypeError, ValueError):
                            pass
                    clone["words"].append(word_clone)
            shifted.append(clone)
        return shifted

    def _set_alignment_job(
        self, ln_book_id: int, audiobook_id: int, payload: dict[str, Any]
    ) -> None:
        with self._lock:
            current = self._alignment_jobs.get(int(ln_book_id)) or {}
            if int(current.get("audiobook_id") or -1) != int(audiobook_id):
                return
            self._alignment_jobs[int(ln_book_id)] = {
                **payload,
                "audiobook_id": int(audiobook_id),
            }

    def _prepare_alignment_worker(
        self,
        ln_book_id: int,
        audiobook_id: int,
        output: Path,
    ) -> None:
        heavy_lease = None
        try:
            with self.db.connect() as conn:
                chapters = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT chapter_index,title,text FROM ln_chapters "
                        "WHERE book_id=? ORDER BY chapter_index",
                        (int(ln_book_id),),
                    ).fetchall()
                ]
            book = self.book(int(audiobook_id))
            if not chapters:
                raise ValueError("Linked LN or audiobook has no chapters")
            transcript = self._load_transcript(int(audiobook_id))
            if transcript is None:
                self.prepare_transcription(int(audiobook_id))
                with self._lock:
                    event = self._transcription_events.get(int(audiobook_id))
                if event is not None:
                    event.wait(timeout=12 * 3600)
                transcript = self._load_transcript(int(audiobook_id))
            if transcript is None:
                status = self.transcription_status(int(audiobook_id))
                raise RuntimeError(str(status.get("error") or "Audiobook transcription is unavailable"))
            all_segments = [
                dict(item)
                for item in transcript.get("segments") or []
                if isinstance(item, dict)
            ]
            self._set_alignment_job(
                ln_book_id,
                audiobook_id,
                {"status": "aligning", "ready": False},
            )
            speech_regions, timing_source = self._load_or_analyze_activity(
                int(audiobook_id),
                all_segments,
            )
            alignment = align_light_novel_to_transcript(
                chapters,
                all_segments,
                duration=float(book["duration"] or 0.0),
                model=self.stt_model,
                speech_regions=speech_regions,
            )
            alignment["timing_source"] = timing_source
            alignment["created_at"] = time.time()
            alignment["processing"] = {
                "schema": "pudge-alignment-pipeline-v1",
                "algorithm": str(alignment.get("schema") or "reading-audio-v3"),
                "input_fingerprint": output.stem,
                "transcript_fingerprint": self._transcript_fingerprint(int(audiobook_id)),
                "transcription_reusable": True,
            }
            report_sources = [
                {
                    **chapter,
                    "normalized_length": len(
                        re.sub(
                            r"[^0-9a-zぁ-ゟ゠-ヿ一-鿿々〆ヶ]",
                            "",
                            unicodedata.normalize("NFKC", str(chapter.get("text") or "")).casefold(),
                        )
                    ),
                }
                for chapter in chapters
            ]
            report = build_alignment_report(alignment, report_sources)
            alignment["quality"] = dict(report.get("summary") or {})
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(alignment, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(output)
            report_output = self._alignment_report_path(int(ln_book_id), int(audiobook_id))
            report_temporary = report_output.with_suffix(".tmp")
            report_temporary.write_text(
                json.dumps(report, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            report_temporary.replace(report_output)
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE reading_audio_links SET alignment_mode='stt_acoustic',updated_at=? "
                    "WHERE ln_book_id=? AND audiobook_id=?",
                    (time.time(), int(ln_book_id), int(audiobook_id)),
                )
            self._set_alignment_job(
                ln_book_id,
                audiobook_id,
                {
                    "status": "ready",
                    "ready": True,
                    "confidence": float(alignment.get("confidence") or 0.0),
                    "matched_chapters": len(alignment.get("chapters") or []),
                },
            )
        except Exception as exc:
            self._set_alignment_job(
                ln_book_id,
                audiobook_id,
                {"status": "error", "ready": False, "error": str(exc)},
            )
        finally:
            if heavy_lease is not None:
                heavy_lease.release()

    def alignment_report(self, ln_book_id: int) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False)
        if link is None:
            return {"status": "unlinked", "ready": False}
        audiobook_id = int(link["book"]["id"])
        report = self._load_alignment_report(int(ln_book_id), audiobook_id)
        if report is None:
            alignment = self._load_alignment(int(ln_book_id), audiobook_id)
            if alignment is None:
                return {"status": "not_prepared", "ready": False}
            with self.db.connect() as conn:
                chapters = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT chapter_index,title,text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index",
                        (int(ln_book_id),),
                    ).fetchall()
                ]
            report = build_alignment_report(alignment, chapters)
        return {"status": "ready", "ready": True, **report}

    def reprocess_alignment(
        self,
        ln_book_id: int,
        *,
        clear_transcription: bool = False,
    ) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False)
        if link is None:
            raise KeyError(f"Light novel id={ln_book_id} has no linked audiobook")
        audiobook_id = int(link["book"]["id"])
        self._alignment_path(int(ln_book_id), audiobook_id).unlink(missing_ok=True)
        self._alignment_report_path(int(ln_book_id), audiobook_id).unlink(missing_ok=True)
        if clear_transcription:
            self._transcript_path(audiobook_id).unlink(missing_ok=True)
        with self._lock:
            self._alignment_jobs.pop(int(ln_book_id), None)
        return self.prepare_alignment(int(ln_book_id), force=False)

    def prepare_alignment(self, ln_book_id: int, *, force: bool = False) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False)
        if link is None:
            raise KeyError(f"Light novel id={ln_book_id} has no linked audiobook")
        audiobook_id = int(link["book"]["id"])
        output = self._alignment_path(int(ln_book_id), audiobook_id)
        if force:
            output.unlink(missing_ok=True)
            self._alignment_report_path(int(ln_book_id), audiobook_id).unlink(missing_ok=True)
        if output.is_file():
            return self.alignment_status(int(ln_book_id))
        with self._lock:
            current = self._alignment_jobs.get(int(ln_book_id)) or {}
            if current.get("status") in {"queued", "transcribing", "aligning"}:
                return dict(current)
            self._alignment_jobs[int(ln_book_id)] = {
                "status": "queued",
                "ready": False,
                "audiobook_id": audiobook_id,
            }
        threading.Thread(
            target=self._prepare_alignment_worker,
            args=(int(ln_book_id), audiobook_id, output),
            name=f"reading-audio-align-{int(ln_book_id)}",
            daemon=True,
        ).start()
        return {"status": "queued", "ready": False}

    def link_light_novel(self, ln_book_id: int, audiobook_id: int) -> dict[str, Any]:
        self.book(int(audiobook_id))
        with self._lock:
            self._alignment_jobs.pop(int(ln_book_id), None)
            old_process = self._alignment_processes.pop(int(ln_book_id), None)
        if old_process is not None and old_process.poll() is None:
            old_process.terminate()
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO reading_audio_links(ln_book_id,audiobook_id,alignment_mode,created_at,updated_at)
                VALUES(?,?,'chapter',?,?)
                ON CONFLICT(ln_book_id) DO UPDATE SET
                    audiobook_id=excluded.audiobook_id,alignment_mode='chapter',updated_at=excluded.updated_at
                """,
                (int(ln_book_id), int(audiobook_id), now, now),
            )
        self.prepare_transcription(int(audiobook_id))
        self.prepare_alignment(int(ln_book_id))
        return {"ok": True, "link": self.link_for_light_novel(int(ln_book_id))}

    def unlink_light_novel(self, ln_book_id: int) -> dict[str, Any]:
        with self._lock:
            self._alignment_jobs.pop(int(ln_book_id), None)
            process = self._alignment_processes.pop(int(ln_book_id), None)
        if process is not None and process.poll() is None:
            process.terminate()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM reading_audio_links WHERE ln_book_id=?", (int(ln_book_id),))
        return {"ok": True}

    def stop_for_light_novel(self, ln_book_id: int) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False)
        if link is None:
            return {"ok": True, "stopped": False, "audiobook_id": None}
        audiobook_id = int(link["book"]["id"])
        result = self.stop(audiobook_id)
        return {
            "ok": True,
            "stopped": bool(result.get("stopped")),
            "audiobook_id": audiobook_id,
        }

    def link_for_light_novel(
        self, ln_book_id: int, *, include_alignment: bool = True
    ) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT audiobook_id,alignment_mode FROM reading_audio_links WHERE ln_book_id=?",
                (int(ln_book_id),),
            ).fetchone()
        if row is None:
            return None
        try:
            book = self.book(int(row["audiobook_id"]))
        except KeyError:
            self.unlink_light_novel(int(ln_book_id))
            return None
        result = {
            "ln_book_id": int(ln_book_id),
            "alignment_mode": str(row["alignment_mode"]),
            "book": book,
        }
        if include_alignment:
            result["alignment"] = self.alignment_status(int(ln_book_id))
        return result

    def _paired_audio_position(self, ln_book_id: int, chapter_index: int, chapter_progress: float) -> tuple[int, float]:
        link = self.link_for_light_novel(int(ln_book_id))
        if link is None:
            raise KeyError(f"Light novel id={ln_book_id} has no linked audiobook")
        book = link["book"]
        alignment = self._load_alignment(int(ln_book_id), int(book["id"]))
        if alignment is not None:
            exact = audio_position_for_light_novel(
                alignment,
                int(chapter_index),
                float(chapter_progress),
            )
            if exact is not None:
                return int(book["id"]), exact
        chapters = book["chapters"] or [{"start": 0.0, "end": float(book["duration"] or 0.0)}]
        with self.db.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM ln_chapters WHERE book_id=?", (int(ln_book_id),)).fetchone()
        ln_count = max(1, int(row[0] if row else 1))
        if ln_count <= 1 or len(chapters) <= 1:
            audio_index = 0
        else:
            audio_index = round(max(0, min(ln_count - 1, int(chapter_index))) / (ln_count - 1) * (len(chapters) - 1))
        chapter = chapters[max(0, min(len(chapters) - 1, audio_index))]
        progress = max(0.0, min(1.0, float(chapter_progress)))
        position = float(chapter["start"]) + progress * max(0.0, float(chapter["end"]) - float(chapter["start"]))
        return int(book["id"]), position

    def play_paired(
        self,
        ln_book_id: int,
        chapter_index: int,
        chapter_progress: float,
        speed: float | None = None,
    ) -> dict[str, Any]:
        audiobook_id, position = self._paired_audio_position(ln_book_id, chapter_index, chapter_progress)
        book = self.book(audiobook_id)
        self.play(audiobook_id, start=position, speed=float(speed or book["speed"] or 1.0))
        return self.paired_state(int(ln_book_id))

    def play_paired_at_offset(
        self,
        ln_book_id: int,
        chapter_index: int,
        character_offset: int,
        speed: float | None = None,
    ) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id))
        if link is None:
            raise KeyError(f"Light novel id={ln_book_id} has no linked audiobook")
        audiobook_id = int(link["book"]["id"])
        alignment = self._load_alignment(int(ln_book_id), audiobook_id)
        position = (
            audio_position_for_light_novel_offset(
                alignment,
                int(chapter_index),
                int(character_offset),
            )
            if alignment is not None
            else None
        )
        if position is None:
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT text FROM ln_chapters WHERE book_id=? AND chapter_index=?",
                    (int(ln_book_id), int(chapter_index)),
                ).fetchone()
            length = max(1, len(str(row["text"] or "")) if row is not None else 1)
            _book_id, position = self._paired_audio_position(
                int(ln_book_id),
                int(chapter_index),
                max(0.0, min(1.0, int(character_offset) / length)),
            )
        book = self.book(audiobook_id)
        if self.is_playing(audiobook_id):
            self.seek_to(audiobook_id, max(0.0, float(position)))
            if speed is not None:
                self.set_speed(audiobook_id, float(speed))
        else:
            self.play(
                audiobook_id,
                start=max(0.0, float(position)),
                speed=float(speed or book["speed"] or 1.0),
            )
            self.set_position(audiobook_id, max(0.0, float(position)))
        return self.paired_state(int(ln_book_id))

    def _paired_light_novel_chapter_ranges(
        self, ln_book_id: int, audiobook_id: int, duration: float
    ) -> list[dict[str, Any]]:
        alignment = self._load_alignment(int(ln_book_id), int(audiobook_id))
        if alignment is None:
            return []
        ranges: list[dict[str, Any]] = []
        for chapter in alignment.get("chapters") or []:
            if not isinstance(chapter, dict):
                continue
            try:
                chapter_index = int(chapter.get("chapter_index") or 0)
                start = max(0.0, min(float(duration), float(chapter.get("start") or 0.0)))
                end = max(start, min(float(duration), float(chapter.get("end") or start)))
            except (TypeError, ValueError):
                continue
            if end <= start:
                anchors = [
                    item
                    for item in chapter.get("anchors") or []
                    if isinstance(item, dict) and item.get("time") is not None
                ]
                if anchors:
                    try:
                        start = max(0.0, min(float(duration), float(anchors[0]["time"])))
                        end = max(start, min(float(duration), float(anchors[-1]["time"])))
                    except (TypeError, ValueError, KeyError):
                        continue
            if end <= start:
                continue
            ranges.append(
                {
                    "chapter_index": chapter_index,
                    "title": str(chapter.get("title") or ""),
                    "start": start,
                    "end": end,
                }
            )
        ranges.sort(key=lambda item: (int(item["chapter_index"]), float(item["start"])))
        return ranges

    def paired_state(self, ln_book_id: int) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id))
        if link is None:
            return {"linked": False, "playing": False}
        book = link["book"]
        audiobook_id = int(book["id"])
        position = float(book["position"] or 0.0)
        with self._lock:
            ipc_path = self._ipc_paths.get(audiobook_id)
        if ipc_path is not None:
            live = self._global_position(audiobook_id, ipc_path)
            if live is not None:
                position = live
                self._record_playback_position(audiobook_id, position)
        alignment = self._load_alignment(int(ln_book_id), audiobook_id)
        exact = light_novel_position_for_audio(alignment, position) if alignment is not None else None
        chapter = self._chapter_for_position(book["chapters"], position)
        if chapter is None:
            chapter_progress = 0.0
            chapter_index = 0
        else:
            span = max(0.001, float(chapter["end"]) - float(chapter["start"]))
            chapter_progress = max(0.0, min(1.0, (position - float(chapter["start"])) / span))
            chapter_index = int(chapter["index"])
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM ln_chapters WHERE book_id=?", (int(ln_book_id),)
            ).fetchone()
        ln_count = max(1, int(row[0] if row else 1))
        audio_count = max(1, len(book["chapters"]))
        ln_chapter_index = (
            int(exact["chapter_index"])
            if exact is not None
            else (
                0
                if ln_count <= 1 or audio_count <= 1
                else round(chapter_index / (audio_count - 1) * (ln_count - 1))
            )
        )
        if exact is not None:
            chapter_progress = float(exact["chapter_progress"])
        player_running = self.is_playing(audiobook_id)
        paused = self.is_paused(audiobook_id) if player_running else False
        # "playing" describes the user-visible transport state.  A silent
        # interval or an mpv playlist transition is still playback, not Pause.
        playing = player_running and not paused
        playback_active = self.is_playback_active(audiobook_id) if playing else False
        return {
            "linked": True,
            "playing": playing,
            "playback_active": playback_active,
            "player_running": player_running,
            "paused": paused,
            "audiobook_id": audiobook_id,
            "title": book["title"],
            "position": position,
            "duration": float(book["duration"] or 0.0),
            "chapter_index": chapter_index,
            "ln_chapter_index": ln_chapter_index,
            "chapter_progress": chapter_progress,
            "chapter_char_offset": exact.get("chapter_char_offset") if exact else None,
            "chapter_char_offset_exact": exact.get("chapter_char_offset_exact") if exact else None,
            "chapter_char_count": exact.get("chapter_char_count") if exact else None,
            "anchor_window": exact.get("anchor_window") if exact else None,
            "alignment_mode": "stt" if exact is not None else "chapter",
            "alignment": self.alignment_status(int(ln_book_id)),
            "ln_chapter_ranges": self._paired_light_novel_chapter_ranges(
                int(ln_book_id), audiobook_id, float(book["duration"] or 0.0)
            ),
            "speed": float(book["speed"] or 1.0),
        }

    def delete(self, book_id: int, *, delete_files: bool = False) -> dict[str, Any]:
        book_id = int(book_id)
        book = self.book(book_id)
        self.stop(book_id)
        source = Path(str(book["path"])).expanduser()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM reading_audio_links WHERE audiobook_id=?", (book_id,))
            conn.execute("DELETE FROM audiobook_bookmarks WHERE book_id=?", (book_id,))
            conn.execute("DELETE FROM audiobook_files WHERE book_id=?", (book_id,))
            conn.execute("DELETE FROM audiobook_chapters WHERE book_id=?", (book_id,))
            conn.execute("DELETE FROM audiobooks WHERE id=?", (book_id,))
        if delete_files:
            try:
                if source.is_dir():
                    shutil.rmtree(source)
                elif source.is_file():
                    source.unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": True, "book_id": book_id, "files_kept": not bool(delete_files)}
