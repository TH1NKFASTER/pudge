from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .database import Database


AUDIOBOOK_EXTENSIONS = {".m4b", ".m4a", ".mp3", ".aac", ".opus", ".ogg", ".flac", ".wav"}


class AudiobookService:
    """First-pass audiobook library backed by mpv and ffprobe chapters."""

    def __init__(self, database: Database, *, ffprobe: str, mpv: str, cache_dir: Path) -> None:
        self.db = database
        self.ffprobe = ffprobe
        self.mpv = mpv
        self.cache_dir = cache_dir
        self._players: dict[int, subprocess.Popen[Any]] = {}
        self._lock = threading.Lock()

    def _probe(self, path: Path) -> tuple[float, list[dict[str, Any]]]:
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
                {"index": index, "title": str(tags.get("title") or f"Chapter {index + 1}"), "start": start, "end": end}
            )
        return duration, chapters

    def import_file(self, path: Path) -> dict[str, Any]:
        path = path.expanduser().resolve()
        if not path.is_file() or path.suffix.casefold() not in AUDIOBOOK_EXTENSIONS:
            raise ValueError("Unsupported audiobook format")
        duration, chapters = self._probe(path)
        now = time.time()
        with self.db.connect() as conn:
            conn.execute(
                """
                INSERT INTO audiobooks(path,title,duration,position,finished,created_at,updated_at)
                VALUES(?,?,?,0,0,?,?)
                ON CONFLICT(path) DO UPDATE SET title=excluded.title,duration=excluded.duration,
                    updated_at=excluded.updated_at
                """,
                (str(path), path.stem, duration, now, now),
            )
            row = conn.execute("SELECT * FROM audiobooks WHERE path=?", (str(path),)).fetchone()
            assert row is not None
            book_id = int(row["id"])
            conn.execute("DELETE FROM audiobook_chapters WHERE book_id=?", (book_id,))
            for chapter in chapters:
                conn.execute(
                    "INSERT INTO audiobook_chapters(book_id,chapter_index,title,start,end) VALUES(?,?,?,?,?)",
                    (book_id, chapter["index"], chapter["title"], chapter["start"], chapter["end"]),
                )
        return self.book(book_id)

    def book(self, book_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM audiobooks WHERE id=?", (int(book_id),)).fetchone()
            chapters = conn.execute(
                "SELECT * FROM audiobook_chapters WHERE book_id=? ORDER BY chapter_index", (int(book_id),)
            ).fetchall()
        if row is None:
            raise KeyError(f"Unknown audiobook id={book_id}")
        return {
            "id": int(row["id"]),
            "path": str(row["path"]),
            "title": str(row["title"]),
            "duration": float(row["duration"] or 0.0),
            "position": float(row["position"] or 0.0),
            "finished": bool(row["finished"]),
            "chapters": [
                {
                    "index": int(chapter["chapter_index"]),
                    "title": str(chapter["title"]),
                    "start": float(chapter["start"] or 0.0),
                    "end": float(chapter["end"] or 0.0),
                }
                for chapter in chapters
            ],
        }

    def state(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id FROM audiobooks ORDER BY updated_at DESC,id DESC").fetchall()
        return {"books": [self.book(int(row["id"])) for row in rows]}

    def set_position(self, book_id: int, position: float) -> None:
        book = self.book(book_id)
        duration = float(book["duration"] or 0.0)
        value = max(0.0, min(float(position), duration or float(position)))
        finished = bool(duration > 0 and value >= duration * 0.98)
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE audiobooks SET position=?,finished=?,updated_at=? WHERE id=?",
                (value, int(finished), time.time(), int(book_id)),
            )

    @staticmethod
    def _ipc_position(ipc_path: Path) -> float | None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1.0)
                client.connect(str(ipc_path))
                client.sendall(b'{"command":["get_property","time-pos"]}\n')
                payload = json.loads(client.recv(4096).decode("utf-8"))
            value = payload.get("data")
            return float(value) if value is not None else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _monitor(self, book_id: int, process: subprocess.Popen[Any], ipc_path: Path) -> None:
        try:
            while process.poll() is None:
                position = self._ipc_position(ipc_path)
                if position is not None:
                    self.set_position(book_id, position)
                time.sleep(2)
            position = self._ipc_position(ipc_path)
            if position is not None:
                self.set_position(book_id, position)
        finally:
            ipc_path.unlink(missing_ok=True)
            with self._lock:
                self._players.pop(int(book_id), None)

    def play(self, book_id: int, start: float | None = None) -> dict[str, Any]:
        book = self.book(book_id)
        path = Path(str(book["path"]))
        if not path.is_file():
            raise FileNotFoundError(path)
        position = float(book["position"] if start is None else start)
        ipc_dir = self.cache_dir / "audiobook-ipc"
        ipc_dir.mkdir(parents=True, exist_ok=True)
        ipc_path = ipc_dir / f"book-{int(book_id)}-{os.getpid()}.sock"
        ipc_path.unlink(missing_ok=True)
        process = subprocess.Popen(
            [
                self.mpv,
                "--no-video",
                "--force-window=no",
                f"--start={max(0.0, position):.3f}",
                f"--input-ipc-server={ipc_path}",
                str(path),
            ]
        )
        with self._lock:
            previous = self._players.get(int(book_id))
            if previous is not None and previous.poll() is None:
                previous.terminate()
            self._players[int(book_id)] = process
        threading.Thread(target=self._monitor, args=(int(book_id), process, ipc_path), daemon=True).start()
        return {"ok": True, "book_id": int(book_id), "position": position}
