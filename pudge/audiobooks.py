from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from .database import Database

AUDIOBOOK_EXTENSIONS = {".m4b", ".m4a", ".mp3", ".aac", ".opus", ".ogg", ".flac", ".wav"}

def _natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]

class AudiobookService:
    """Silent mpv audiobook playback with resumable file/folder books."""

    def __init__(self, database: Database, *, ffprobe: str, mpv: str, cache_dir: Path) -> None:
        self.db = database
        self.ffprobe = ffprobe
        self.mpv = mpv
        self.cache_dir = cache_dir
        self._players: dict[int, subprocess.Popen[Any]] = {}
        self._ipc_paths: dict[int, Path] = {}
        self._speeds: dict[int, float] = {}
        self._lock = threading.Lock()

    def _probe(self, path: Path) -> tuple[float, list[dict[str, Any]]]:
        completed = subprocess.run([self.ffprobe,"-v","error","-show_format","-show_chapters","-of","json",str(path)], text=True, capture_output=True, timeout=30)
        if completed.returncode != 0:
            raise ValueError(completed.stderr.strip() or "ffprobe could not read this audiobook")
        payload = json.loads(completed.stdout or "{}")
        try: duration = max(0.0, float(payload.get("format", {}).get("duration") or 0.0))
        except (TypeError, ValueError): duration = 0.0
        chapters=[]
        for index, chapter in enumerate(payload.get("chapters") or []):
            try: start=float(chapter.get("start_time") or 0.0); end=float(chapter.get("end_time") or start)
            except (TypeError, ValueError): continue
            tags=chapter.get("tags") if isinstance(chapter.get("tags"),dict) else {}
            chapters.append({"index":index,"title":str(tags.get("title") or f"Chapter {index+1}"),"start":start,"end":end})
        return duration, chapters

    def _folder_files(self, folder: Path) -> list[Path]:
        rows=[p.resolve() for p in folder.rglob("*") if p.is_file() and p.suffix.casefold() in AUDIOBOOK_EXTENSIONS]
        rows.sort(key=lambda p:_natural_key(str(p.relative_to(folder))))
        return rows

    def _upsert(self, *, path: Path, title: str, duration: float, files: list[dict[str, Any]], chapters: list[dict[str, Any]]) -> dict[str, Any]:
        now=time.time()
        with self.db.connect() as conn:
            conn.execute("""INSERT INTO audiobooks(path,title,duration,position,finished,created_at,updated_at) VALUES(?,?,?,0,0,?,?) ON CONFLICT(path) DO UPDATE SET title=excluded.title,duration=excluded.duration,updated_at=excluded.updated_at""", (str(path),title,float(duration),now,now))
            row=conn.execute("SELECT * FROM audiobooks WHERE path=?",(str(path),)).fetchone(); assert row is not None; book_id=int(row["id"])
            conn.execute("DELETE FROM audiobook_chapters WHERE book_id=?",(book_id,)); conn.execute("DELETE FROM audiobook_files WHERE book_id=?",(book_id,))
            for item in files:
                conn.execute("INSERT INTO audiobook_files(book_id,file_index,path,title,duration,start,end) VALUES(?,?,?,?,?,?,?)",(book_id,int(item["index"]),str(item["path"]),str(item["title"]),float(item["duration"]),float(item["start"]),float(item["end"])))
            for chapter in chapters:
                conn.execute("INSERT INTO audiobook_chapters(book_id,chapter_index,title,start,end) VALUES(?,?,?,?,?)",(book_id,int(chapter["index"]),str(chapter["title"]),float(chapter["start"]),float(chapter["end"])))
        return self.book(book_id)

    def import_file(self, path: Path) -> dict[str, Any]:
        path=path.expanduser().resolve()
        if not path.is_file() or path.suffix.casefold() not in AUDIOBOOK_EXTENSIONS: raise ValueError("Unsupported audiobook format")
        duration,embedded=self._probe(path)
        files=[{"index":0,"path":str(path),"title":path.stem,"duration":duration,"start":0.0,"end":duration}]
        chapters=embedded or [{"index":0,"title":path.stem,"start":0.0,"end":duration}]
        return self._upsert(path=path,title=path.stem,duration=duration,files=files,chapters=chapters)

    def import_folder(self, folder: Path) -> dict[str, Any]:
        folder=folder.expanduser().resolve()
        if not folder.is_dir(): raise ValueError("Audiobook folder does not exist")
        paths=self._folder_files(folder)
        if not paths: raise ValueError("No supported audio files found in this folder")
        files=[]; chapters=[]; cursor=0.0
        for index,path in enumerate(paths):
            duration,_=self._probe(path); start=cursor; end=start+max(0.0,duration)
            files.append({"index":index,"path":str(path),"title":path.stem,"duration":duration,"start":start,"end":end})
            chapters.append({"index":index,"title":path.stem,"start":start,"end":end}); cursor=end
        return self._upsert(path=folder,title=folder.name,duration=cursor,files=files,chapters=chapters)

    def _file_rows(self, book_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn: rows=conn.execute("SELECT * FROM audiobook_files WHERE book_id=? ORDER BY file_index",(int(book_id),)).fetchall()
        return [dict(row) for row in rows]

    def is_playing(self, book_id: int) -> bool:
        with self._lock: process=self._players.get(int(book_id)); return bool(process is not None and process.poll() is None)

    def book(self, book_id: int) -> dict[str, Any]:
        with self.db.connect() as conn:
            row=conn.execute("SELECT * FROM audiobooks WHERE id=?",(int(book_id),)).fetchone()
            chapters=conn.execute("SELECT * FROM audiobook_chapters WHERE book_id=? ORDER BY chapter_index",(int(book_id),)).fetchall()
            file_count=int(conn.execute("SELECT COUNT(*) FROM audiobook_files WHERE book_id=?",(int(book_id),)).fetchone()[0])
        if row is None: raise KeyError(f"Unknown audiobook id={book_id}")
        path=Path(str(row["path"]))
        return {"id":int(row["id"]),"path":str(row["path"]),"title":str(row["title"]),"duration":float(row["duration"] or 0.0),"position":float(row["position"] or 0.0),"finished":bool(row["finished"]),"playing":self.is_playing(int(book_id)),"speed":float(self._speeds.get(int(book_id),1.0)),"multi_file":path.is_dir() or file_count>1,"file_count":file_count,"chapters":[{"index":int(ch["chapter_index"]),"title":str(ch["title"]),"start":float(ch["start"] or 0.0),"end":float(ch["end"] or 0.0)} for ch in chapters]}

    def state(self) -> dict[str, Any]:
        with self.db.connect() as conn: rows=conn.execute("SELECT id FROM audiobooks ORDER BY updated_at DESC,id DESC").fetchall()
        return {"books":[self.book(int(row["id"])) for row in rows]}

    def set_position(self, book_id: int, position: float) -> None:
        book=self.book(book_id); duration=float(book["duration"] or 0.0); value=max(0.0,min(float(position),duration or float(position))); finished=bool(duration>0 and value>=duration*0.98)
        with self.db.connect() as conn: conn.execute("UPDATE audiobooks SET position=?,finished=?,updated_at=? WHERE id=?",(value,int(finished),time.time(),int(book_id)))

    @staticmethod
    def _ipc_command(ipc_path: Path, command: list[Any]) -> dict[str, Any] | None:
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
                client.settimeout(1.0)
                client.connect(str(ipc_path))
                client.sendall(json.dumps({"command":command}).encode("utf-8")+b"\n")
                payload=json.loads(client.recv(4096).decode("utf-8"))
            return payload if isinstance(payload,dict) else None
        except (OSError,ValueError,TypeError,json.JSONDecodeError):
            return None

    @classmethod
    def _ipc_get(cls, ipc_path: Path, property_name: str) -> Any:
        payload=cls._ipc_command(ipc_path,["get_property",property_name])
        return payload.get("data") if payload else None

    def _global_position(self, book_id: int, ipc_path: Path) -> float | None:
        raw=self._ipc_get(ipc_path,"time-pos")
        try: local=float(raw) if raw is not None else None
        except (TypeError,ValueError): local=None
        if local is None:return None
        files=self._file_rows(book_id)
        if len(files)<=1:return local
        raw_index=self._ipc_get(ipc_path,"playlist-pos")
        try:index=int(raw_index) if raw_index is not None else 0
        except (TypeError,ValueError):index=0
        match=next((row for row in files if int(row["file_index"])==index),None)
        return float(match["start"] if match else 0.0)+local

    def _monitor(self, book_id: int, process: subprocess.Popen[Any], ipc_path: Path) -> None:
        try:
            while process.poll() is None:
                position=self._global_position(book_id,ipc_path)
                if position is not None:self.set_position(book_id,position)
                time.sleep(2)
        finally:
            ipc_path.unlink(missing_ok=True)
            with self._lock:
                if self._players.get(int(book_id)) is process:self._players.pop(int(book_id),None);self._ipc_paths.pop(int(book_id),None)

    def stop(self, book_id: int) -> dict[str, Any]:
        book_id=int(book_id)
        with self._lock: process=self._players.get(book_id);ipc_path=self._ipc_paths.get(book_id)
        if process is None or process.poll() is not None:return {"ok":True,"book":self.book(book_id),"stopped":False}
        if ipc_path is not None:
            position=self._global_position(book_id,ipc_path)
            if position is not None:self.set_position(book_id,position)
        process.terminate()
        try:process.wait(timeout=4)
        except subprocess.TimeoutExpired:process.kill()
        with self._lock:
            if self._players.get(book_id) is process:self._players.pop(book_id,None);self._ipc_paths.pop(book_id,None)
        if ipc_path is not None:ipc_path.unlink(missing_ok=True)
        return {"ok":True,"book":self.book(book_id),"stopped":True}

    def stop_all(self) -> None:
        with self._lock: ids=list(self._players)
        for book_id in ids:
            try:self.stop(book_id)
            except Exception:pass

    def play(self, book_id: int, start: float | None = None, speed: float = 1.0) -> dict[str, Any]:
        book_id=int(book_id)
        speed=max(0.5,min(3.0,float(speed or 1.0)))
        if self.is_playing(book_id):self.stop(book_id)
        book=self.book(book_id);position=float(book["position"] if start is None else start);files=self._file_rows(book_id)
        if not files:
            path=Path(str(book["path"]));
            if not path.is_file():raise FileNotFoundError(path)
            files=[{"file_index":0,"path":str(path),"start":0.0,"end":float(book["duration"] or 0.0)}]
        selected_index=0;local_start=position
        for row in files:
            start_at=float(row.get("start") or 0.0);end_at=float(row.get("end") or start_at)
            if position>=start_at:selected_index=int(row.get("file_index") or 0);local_start=max(0.0,position-start_at)
            if position<end_at:break
        ipc_dir=self.cache_dir/"audiobook-ipc";ipc_dir.mkdir(parents=True,exist_ok=True);ipc_path=ipc_dir/f"book-{book_id}-{os.getpid()}.sock";ipc_path.unlink(missing_ok=True)
        command=[self.mpv,"--no-video","--force-window=no",f"--start={max(0.0,local_start):.3f}",f"--speed={speed:.3f}",f"--input-ipc-server={ipc_path}"]
        if len(files)>1:command.append(f"--playlist-start={selected_index}")
        command.extend(str(row["path"]) for row in files);process=subprocess.Popen(command)
        with self._lock:self._players[book_id]=process;self._ipc_paths[book_id]=ipc_path;self._speeds[book_id]=speed
        threading.Thread(target=self._monitor,args=(book_id,process,ipc_path),name=f"audiobook-{book_id}",daemon=True).start()
        return {"ok":True,"book_id":book_id,"position":position,"playing":True,"speed":speed}

    def set_speed(self, book_id: int, speed: float) -> dict[str, Any]:
        book_id=int(book_id);value=max(0.5,min(3.0,float(speed or 1.0)))
        with self._lock:
            ipc_path=self._ipc_paths.get(book_id)
            self._speeds[book_id]=value
        if ipc_path is not None:
            self._ipc_command(ipc_path,["set_property","speed",value])
        return {"ok":True,"book_id":book_id,"speed":value,"book":self.book(book_id)}

    def seek(self, book_id: int, seconds: float) -> dict[str, Any]:
        book_id=int(book_id);delta=float(seconds)
        with self._lock: ipc_path=self._ipc_paths.get(book_id)
        if ipc_path is not None and self.is_playing(book_id):
            self._ipc_command(ipc_path,["seek",delta,"relative","exact"])
            time.sleep(0.03)
            position=self._global_position(book_id,ipc_path)
            if position is not None:self.set_position(book_id,position)
        else:
            book=self.book(book_id);self.set_position(book_id,float(book["position"] or 0.0)+delta)
        return {"ok":True,"book":self.book(book_id)}

    def delete(self, book_id: int, *, delete_files: bool = False) -> dict[str, Any]:
        book_id=int(book_id);book=self.book(book_id);self.stop(book_id);source=Path(str(book["path"])).expanduser()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM audiobook_files WHERE book_id=?",(book_id,));conn.execute("DELETE FROM audiobook_chapters WHERE book_id=?",(book_id,));conn.execute("DELETE FROM audiobooks WHERE id=?",(book_id,))
        if delete_files:
            try:
                if source.is_dir():shutil.rmtree(source)
                elif source.is_file():source.unlink(missing_ok=True)
            except OSError:pass
        return {"ok":True,"book_id":book_id,"files_kept":not bool(delete_files)}
