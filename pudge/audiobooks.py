from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .database import Database
from .metadata_cache import MetadataCache
from .reading_audio_alignment import (
    align_light_novel_to_transcript,
    audio_position_for_light_novel,
    light_novel_position_for_audio,
)

AUDIOBOOK_EXTENSIONS = {".m4b", ".m4a", ".mp3", ".aac", ".opus", ".ogg", ".flac", ".wav"}
_POSITION_WRITE_INTERVAL = 10.0


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
        python: str | None = None,
        stt_model: str = "mlx-community/whisper-tiny",
    ) -> None:
        self.db = database
        self.ffprobe = ffprobe
        self.mpv = mpv
        self.python = str(python or os.getenv("PUDGE_PYTHON", "").strip() or sys.executable)
        self.stt_model = str(stt_model or "mlx-community/whisper-tiny")
        self.cache_dir = Path(cache_dir)
        self._probe_cache = MetadataCache(self.cache_dir, "audiobook-probe", schema="v2")
        self._players: dict[int, subprocess.Popen[Any]] = {}
        self._ipc_paths: dict[int, Path] = {}
        self._speeds: dict[int, float] = {}
        self._sleep_deadlines: dict[int, float] = {}
        self._sleep_chapter_ends: dict[int, float] = {}
        self._alignment_jobs: dict[int, dict[str, Any]] = {}
        self._alignment_processes: dict[int, subprocess.Popen[Any]] = {}
        self._lock = threading.Lock()

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
        return self._upsert(path=path, title=path.stem, duration=duration, files=files, chapters=chapters)

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
        return self._upsert(path=folder, title=folder.name, duration=cursor, files=files, chapters=chapters)

    def _file_rows(self, book_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audiobook_files WHERE book_id=? ORDER BY file_index", (int(book_id),)
            ).fetchall()
        return [dict(row) for row in rows]

    def is_playing(self, book_id: int) -> bool:
        with self._lock:
            process = self._players.get(int(book_id))
            return bool(process is not None and process.poll() is None)

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
            "playing": self.is_playing(int(book_id)),
            "speed": float(self._speeds.get(int(book_id), row["speed"] or 1.0)),
            "multi_file": Path(str(row["path"])).is_dir() or file_count > 1,
            "file_count": file_count,
            "chapters": chapter_payload,
            "current_chapter": current_chapter,
            "bookmarks": self._bookmarks(int(book_id)),
            "sleep_timer_seconds": max(0, round(deadline - time.monotonic())) if deadline else None,
            "sleep_at_chapter_end": chapter_end is not None,
        }

    def state(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id FROM audiobooks ORDER BY updated_at DESC,id DESC").fetchall()
        return {"books": [self.book(int(row["id"])) for row in rows]}

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
                    if time.monotonic() - last_saved >= _POSITION_WRITE_INTERVAL:
                        self.set_position(book_id, position)
                        last_saved = time.monotonic()
                if self._sleep_reached(book_id, position):
                    self._ipc_command(ipc_path, ["set_property", "pause", True])
                    if position is not None:
                        self.set_position(book_id, position)
                    process.terminate()
                    break
                time.sleep(1.0)
        finally:
            if last_position is not None:
                self.set_position(book_id, last_position)
            ipc_path.unlink(missing_ok=True)
            with self._lock:
                if self._players.get(int(book_id)) is process:
                    self._players.pop(int(book_id), None)
                    self._ipc_paths.pop(int(book_id), None)
                self._sleep_deadlines.pop(int(book_id), None)
                self._sleep_chapter_ends.pop(int(book_id), None)

    def stop(self, book_id: int) -> dict[str, Any]:
        book_id = int(book_id)
        final_position: float | None = None
        with self._lock:
            process = self._players.get(book_id)
            ipc_path = self._ipc_paths.get(book_id)
        if process is None or process.poll() is not None:
            return {"ok": True, "book": self.book(book_id), "stopped": False}
        if ipc_path is not None:
            final_position = self._global_position(book_id, ipc_path)
            if final_position is not None:
                self.set_position(book_id, final_position)
        process.terminate()
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
        if final_position is not None:
            # The monitor's final write can race with Stop; the newest IPC
            # position is authoritative after the process has exited.
            self.set_position(book_id, final_position)
        with self._lock:
            if self._players.get(book_id) is process:
                self._players.pop(book_id, None)
                self._ipc_paths.pop(book_id, None)
            self._sleep_deadlines.pop(book_id, None)
            self._sleep_chapter_ends.pop(book_id, None)
        if ipc_path is not None:
            ipc_path.unlink(missing_ok=True)
        return {"ok": True, "book": self.book(book_id), "stopped": True}

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._players)
            alignment_processes = list(self._alignment_processes.values())
        for book_id in ids:
            try:
                self.stop(book_id)
            except Exception:
                continue
        for process in alignment_processes:
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
            "--no-video",
            "--force-window=no",
            f"--start={max(0.0, local_start):.3f}",
            f"--speed={speed:.3f}",
            "--audio-display=no",
            f"--input-ipc-server={ipc_path}",
        ]
        if len(files) > 1:
            command.append(f"--playlist-start={selected_index}")
        command.extend(str(row["path"]) for row in files)
        process = subprocess.Popen(command)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE audiobooks SET speed=?,last_played_at=?,updated_at=? WHERE id=?",
                (speed, time.time(), time.time(), book_id),
            )
        with self._lock:
            self._players[book_id] = process
            self._ipc_paths[book_id] = ipc_path
            self._speeds[book_id] = speed
        threading.Thread(
            target=self._monitor,
            args=(book_id, process, ipc_path),
            name=f"audiobook-{book_id}",
            daemon=True,
        ).start()
        return {"ok": True, "book_id": book_id, "position": position, "playing": True, "speed": speed}

    def set_speed(self, book_id: int, speed: float) -> dict[str, Any]:
        book_id = int(book_id)
        value = max(0.5, min(3.0, float(speed or 1.0)))
        with self._lock:
            ipc_path = self._ipc_paths.get(book_id)
            self._speeds[book_id] = value
        with self.db.connect() as conn:
            conn.execute("UPDATE audiobooks SET speed=?,updated_at=? WHERE id=?", (value, time.time(), book_id))
        if ipc_path is not None:
            self._ipc_command(ipc_path, ["set_property", "speed", value])
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
            self.play(book_id, start=value, speed=float(book["speed"] or 1.0))
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

    def _alignment_fingerprint(self, ln_book_id: int, audiobook_id: int) -> str:
        digest = hashlib.sha256(f"reading-audio-v1\0{self.stt_model}\0".encode())
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

    def _load_alignment(self, ln_book_id: int, audiobook_id: int) -> dict[str, Any] | None:
        path = self._alignment_path(int(ln_book_id), int(audiobook_id))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) and payload.get("schema") == "reading-audio-v1" else None

    def alignment_status(self, ln_book_id: int) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False)
        if link is None:
            return {"status": "unlinked", "ready": False}
        audiobook_id = int(link["book"]["id"])
        alignment = self._load_alignment(int(ln_book_id), audiobook_id)
        if alignment is not None:
            return {
                "status": "ready",
                "ready": True,
                "model": str(alignment.get("model") or self.stt_model),
                "confidence": float(alignment.get("confidence") or 0.0),
                "matched_chapters": len(alignment.get("chapters") or []),
                "anchor_count": int(alignment.get("anchor_count") or 0),
            }
        with self._lock:
            job = dict(self._alignment_jobs.get(int(ln_book_id)) or {})
        if job and int(job.get("audiobook_id") or audiobook_id) != audiobook_id:
            job = {}
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
        work_dir = output.parent / f".{output.stem}-work"
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
            files = self._file_rows(int(audiobook_id))
            book = self.book(int(audiobook_id))
            if not chapters or not files:
                raise ValueError("Linked LN or audiobook has no chapters")
            work_dir.mkdir(parents=True, exist_ok=True)
            all_segments: list[dict[str, Any]] = []
            for file_number, row in enumerate(files, 1):
                source = Path(str(row["path"]))
                if not source.is_file():
                    raise FileNotFoundError(source)
                self._set_alignment_job(
                    ln_book_id,
                    audiobook_id,
                    {
                        "status": "transcribing",
                        "ready": False,
                        "file": file_number,
                        "file_count": len(files),
                    },
                )
                result_path = work_dir / f"file-{file_number:04d}.json"
                timeout = max(
                    30 * 60,
                    min(12 * 3600, float(row.get("duration") or 0.0) * 3.0),
                )
                process = subprocess.Popen(
                    [
                        self.python,
                        "-m",
                        "pudge.subtitles.stt_worker",
                        "--words",
                        str(source),
                        str(result_path),
                        self.stt_model,
                    ],
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                with self._lock:
                    self._alignment_processes[int(ln_book_id)] = process
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise
                finally:
                    with self._lock:
                        if self._alignment_processes.get(int(ln_book_id)) is process:
                            self._alignment_processes.pop(int(ln_book_id), None)
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
            self._set_alignment_job(
                ln_book_id,
                audiobook_id,
                {"status": "aligning", "ready": False},
            )
            alignment = align_light_novel_to_transcript(
                chapters,
                all_segments,
                duration=float(book["duration"] or 0.0),
                model=self.stt_model,
            )
            alignment["created_at"] = time.time()
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(alignment, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(output)
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE reading_audio_links SET alignment_mode='stt',updated_at=? "
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
        except subprocess.TimeoutExpired:
            self._set_alignment_job(
                ln_book_id,
                audiobook_id,
                {"status": "error", "ready": False, "error": "Japanese STT timed out"},
            )
        except Exception as exc:
            self._set_alignment_job(
                ln_book_id,
                audiobook_id,
                {"status": "error", "ready": False, "error": str(exc)},
            )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def prepare_alignment(self, ln_book_id: int, *, force: bool = False) -> dict[str, Any]:
        link = self.link_for_light_novel(int(ln_book_id), include_alignment=False)
        if link is None:
            raise KeyError(f"Light novel id={ln_book_id} has no linked audiobook")
        audiobook_id = int(link["book"]["id"])
        output = self._alignment_path(int(ln_book_id), audiobook_id)
        if force:
            output.unlink(missing_ok=True)
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
        return {
            "linked": True,
            "playing": self.is_playing(audiobook_id),
            "audiobook_id": audiobook_id,
            "title": book["title"],
            "position": position,
            "duration": float(book["duration"] or 0.0),
            "chapter_index": chapter_index,
            "ln_chapter_index": ln_chapter_index,
            "chapter_progress": chapter_progress,
            "chapter_char_offset": exact.get("chapter_char_offset") if exact else None,
            "chapter_char_count": exact.get("chapter_char_count") if exact else None,
            "alignment_mode": "stt" if exact is not None else "chapter",
            "alignment": self.alignment_status(int(ln_book_id)),
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
