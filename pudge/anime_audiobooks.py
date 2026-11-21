"""Audiobooks made from anime episodes ("watched it, now listen to it again").

The Japanese audio track of a downloaded episode is copied (AAC: no re-encode) or
converted to AAC into an ``.m4a`` under ``<library>/Anime Audiobooks/<title> [<id>]/``
and added to the audiobook library: one book per episode, and one book for the whole
title (one chapter per episode, the same files). Every book of one title carries the
title as its series (a manual series correction), so they stand on one shelf, ordered
by episode, on the Mac and on the iPhone.

* The opening and ending found by the intro skipper can be left out; the subtitle
  times and the chapter marks follow the cut.
* The episode's prepared Japanese subtitles become the book's read-along text (the
  ``readalong.json`` the iPhone player already shows for light novels).
* The files live apart from the videos: deleting an episode keeps its audiobook, and
  deleting an audiobook never touches a video (only its own unreferenced ``.m4a``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from . import library_overrides
from .fsutil import atomic_write_json
from .i18n import err, t

LOGGER = logging.getLogger(__name__)

ROOT_NAME = "Anime Audiobooks"
MARKER = ".pudge-anime-audiobook.json"
SCHEMA = 1
# Changing how a file is made (stream choice, cut, codec) makes older files be made again.
EXTRACT_VERSION = 1
AAC_BITRATE = "128k"
FREE_SPACE_MARGIN_BYTES = 256 * 1024 * 1024
_JAPANESE_LANGUAGES = {"jpn", "ja", "jp", "jap", "japanese"}


@dataclass
class EpisodeSource:
    """One downloaded episode file, as the library knows it."""

    episode: int
    video: Path
    episode_end: int | None = None
    subtitle: Path | None = None
    subtitle_delay: float = 0.0
    # Opening/ending (seconds in the video) found by the intro skipper.
    segments: list[tuple[float, float]] = field(default_factory=list)


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------- pure helpers


def safe_name(value: str, fallback: str = "Anime") -> str:
    text = re.sub(r"[^\wぁ-ゟ゠-ヿ一-鿿 .()\[\]&,'!-]+", "_", str(value or "")).strip(" ._")
    return text[:120] or fallback


def title_folder(root: Path, media_id: int, title: str) -> Path:
    return Path(root) / f"{safe_name(title)} [{int(media_id)}]"


def episode_file_name(episode: int) -> str:
    return f"Episode {int(episode):03d}.m4a"


def read_along_sidecar(audio: Path) -> Path:
    return Path(audio).with_name(f".{Path(audio).stem}.readalong.json")


def is_anime_audiobook_path(path: Path | str) -> bool:
    """A file or folder made here (its folder holds the marker)."""
    item = Path(str(path or ""))
    if not str(path or ""):
        return False
    try:
        return (item / MARKER).is_file() if item.is_dir() else (item.parent / MARKER).is_file()
    except OSError:
        return False


def pick_audio_stream(streams: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The Japanese audio stream (language tag, then title, then default, then first).

    Returns the stream with ``audio_index`` (its place among the audio streams: ``0:a:N``).
    """
    audio = [dict(stream) for stream in streams if str(stream.get("codec_type") or "audio") == "audio"]
    if not audio:
        return None

    def score(item: tuple[int, dict[str, Any]]) -> tuple[int, int]:
        index, stream = item
        tags = {str(k).casefold(): str(v) for k, v in (stream.get("tags") or {}).items()}
        language = tags.get("language", "").strip().casefold()
        title = tags.get("title", "").casefold()
        value = 0
        if language in _JAPANESE_LANGUAGES:
            value = 4
        elif "japanese" in title or "日本語" in title or re.search(r"\bjp(?:n)?\b", title):
            value = 3
        elif (stream.get("disposition") or {}).get("default"):
            value = 1
        if "commentary" in title or "コメンタリー" in title:
            value -= 3
        return (value, -index)

    index, best = max(enumerate(audio), key=score)
    return {**best, "audio_index": index}


def merge_spans(spans: Iterable[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """Sorted, merged spans inside ``0..duration`` (spans shorter than a second are ignored)."""
    limit = float(duration) if duration and duration > 0 else float("inf")
    cleaned = sorted(
        (max(0.0, float(start)), min(limit, float(end)))
        for start, end in spans
        if float(end) - float(start) >= 1.0
    )
    merged: list[tuple[float, float]] = []
    for start, end in cleaned:
        if end - start < 1.0:
            continue
        if merged and start <= merged[-1][1] + 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def remap_time(value: float, spans: list[tuple[float, float]]) -> float:
    """A video time on the cut audio's clock (a time inside a cut lands on its start)."""
    removed = 0.0
    for start, end in spans:
        if value >= end:
            removed += end - start
        elif value > start:
            return start - removed
        else:
            break
    return value - removed


def unmap_time(value: float, spans: list[tuple[float, float]]) -> float:
    """A time of the cut audio back on the video's clock (the reverse of ``remap_time``)."""
    removed = 0.0
    for start, end in spans:
        if value >= start - removed:
            removed += end - start
        else:
            break
    return value + removed


def audio_offset(container: dict[str, Any] | None, stream: dict[str, Any] | None) -> float:
    """Seconds the audio stream starts after the file does (0 for an ordinary file).

    FFmpeg (the ``t`` of the cut filter), mpv and the subtitles count from the file's start;
    the made audio counts from its own first sample.
    """
    try:
        value = float((stream or {}).get("start_time") or 0.0) - float(
            (container or {}).get("start_time") or 0.0
        )
    except (TypeError, ValueError):
        return 0.0
    # A few milliseconds (encoder priming, Matroska rounding) are no shift worth a seek.
    return round(value, 3) if 0.05 <= value < 6 * 3600 else 0.0


def remap_cues(
    cues: list[tuple[float, float, str]],
    spans: list[tuple[float, float]],
    *,
    delay: float = 0.0,
    audio_start: float = 0.0,
) -> list[tuple[float, float, str]]:
    """Subtitle cues on the cut audio's clock; cues inside a cut (song lyrics) go.

    ``audio_start``: the video time of the audio's first sample (the made audio starts there).
    """
    result = []
    lead = remap_time(max(0.0, float(audio_start)), spans) if audio_start else 0.0
    for start, end, text in cues:
        start, end = float(start) + delay, float(end) + delay
        if end <= 0:
            continue
        start = max(0.0, start)
        if any(cut_start <= start and end <= cut_end for cut_start, cut_end in spans):
            continue
        new_start = max(0.0, remap_time(start, spans) - lead)
        new_end = max(0.0, remap_time(end, spans) - lead)
        if new_end - new_start >= 0.05:
            result.append((new_start, new_end, text))
    result.sort(key=lambda cue: (cue[0], cue[1]))
    return result


def read_along_chapter(
    cues: list[tuple[float, float, str]], *, index: int, title: str, duration: float
) -> dict[str, Any] | None:
    """One read-along chapter (the ``readalong.json`` schema 1 the iPhone reads).

    Each cue is a sentence (and its own paragraph). Offsets count the spoken characters;
    one silent character between cues keeps the last cue lit until the next one starts.
    ``t`` is the cue's own ``[startMs, endMs]``: two lines shown at once (usual in ASS) share
    one moment, which the monotonic ``anchors`` cannot say (the second line's anchor start
    would be the first line's end).
    """
    from .reading_audio_alignment import normalize_reading_text

    sentences: list[dict[str, Any]] = []
    anchors: list[list[int]] = []
    cursor = 0
    for start, end, raw in cues:
        text = " ".join(line.strip() for line in str(raw or "").splitlines() if line.strip())
        if not text:
            continue
        length = max(1, len(normalize_reading_text(text)))
        sentence = {
            "p": len(sentences),
            "text": text,
            "start": cursor,
            "end": cursor + length,
            "t": [round(start * 1000), max(round(start * 1000), round(end * 1000))],
        }
        for point in ([round(start * 1000), cursor], [round(end * 1000), cursor + length]):
            if anchors and (point[0] < anchors[-1][0] or point[1] < anchors[-1][1]):
                continue  # overlapping cues: the phone needs a monotonic clock
            anchors.append(point)
        sentences.append(sentence)
        cursor += length + 1
    if not sentences or len(anchors) < 2:
        return None
    return {
        "index": int(index),
        "title": str(title),
        "startMs": 0,
        "endMs": max(round(float(duration) * 1000), anchors[-1][0]),
        "sentences": sentences,
        "anchors": anchors,
    }


def shift_chapter(chapter: dict[str, Any], *, index: int, title: str, offset_ms: int) -> dict[str, Any]:
    sentences = chapter.get("sentences") or []
    if offset_ms and any(isinstance(item, dict) and item.get("t") for item in sentences):
        sentences = [
            {**item, "t": [int(value) + offset_ms for value in item["t"]]}
            if isinstance(item.get("t"), list) and len(item["t"]) == 2
            else item
            for item in sentences
            if isinstance(item, dict)
        ]
    return {
        **chapter,
        "sentences": sentences,
        "index": int(index),
        "title": str(title),
        "startMs": int(chapter.get("startMs") or 0) + offset_ms,
        "endMs": int(chapter.get("endMs") or 0) + offset_ms,
        "anchors": [[int(a) + offset_ms, int(b)] for a, b in chapter.get("anchors") or []],
    }


def read_along_signature(db: Any, book_id: int) -> str | None:
    """What ``read_along_document`` is built from (None: not a book made here).

    The same signature means the same document, so a caller may keep the one it built.
    """
    with db.connect() as conn:
        book = conn.execute("SELECT title FROM audiobooks WHERE id=?", (int(book_id),)).fetchone()
        rows = conn.execute(
            "SELECT file_index,path,start FROM audiobook_files WHERE book_id=? ORDER BY file_index",
            (int(book_id),),
        ).fetchall()
        titles = [
            (int(row["chapter_index"]), str(row["title"]))
            for row in conn.execute(
                "SELECT chapter_index,title FROM audiobook_chapters WHERE book_id=? ORDER BY chapter_index",
                (int(book_id),),
            )
        ]
    if book is None or not rows:
        return None
    parts: list[Any] = [SCHEMA, str(book["title"] or ""), titles]
    folders: set[Path] = set()
    for row in rows:
        audio = Path(str(row["path"]))
        folders.add(audio.parent)
        try:
            stat = read_along_sidecar(audio).stat()
            mark: tuple[int, int] | None = (int(stat.st_size), int(stat.st_mtime_ns))
        except OSError:
            mark = None
        parts.append((int(row["file_index"]), str(audio), round(float(row["start"] or 0.0), 3), mark))
    if not all((folder / MARKER).is_file() for folder in folders):
        return None
    return hashlib.sha256(repr(parts).encode("utf-8")).hexdigest()


def read_along_document(db: Any, book_id: int) -> dict[str, Any] | None:
    """The read-along text of an audiobook made from anime (None for any other book)."""
    with db.connect() as conn:
        book = conn.execute("SELECT title FROM audiobooks WHERE id=?", (int(book_id),)).fetchone()
        rows = conn.execute(
            "SELECT file_index,path,start FROM audiobook_files WHERE book_id=? ORDER BY file_index",
            (int(book_id),),
        ).fetchall()
        chapter_titles = {
            int(row["chapter_index"]): str(row["title"])
            for row in conn.execute(
                "SELECT chapter_index,title FROM audiobook_chapters WHERE book_id=?", (int(book_id),)
            )
        }
    if book is None or not rows:
        return None
    chapters = []
    for row in rows:
        audio = Path(str(row["path"]))
        if not is_anime_audiobook_path(audio):
            return None
        try:
            chapter = json.loads(read_along_sidecar(audio).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(chapter, dict) or not chapter.get("sentences"):
            continue
        index = int(row["file_index"])
        chapters.append(
            shift_chapter(
                chapter,
                index=index,
                title=chapter_titles.get(index, str(chapter.get("title") or "")),
                offset_ms=round(float(row["start"] or 0.0) * 1000),
            )
        )
    if not chapters:
        return None
    return {"schema": 1, "title": str(book["title"] or ""), "chapters": chapters}


def _marked_folder(folder: Path) -> bool:
    """A folder made here: it carries the marker, and the marker is Pudge's manifest."""
    try:
        data = json.loads((Path(folder) / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("episodes"), dict) and "schema" in data


def _cuts(entry: dict[str, Any] | None) -> list[tuple[float, float]]:
    try:
        return [(float(a), float(b)) for a, b in (entry or {}).get("cuts") or []]
    except (TypeError, ValueError):
        return []


def _cut_change(
    earlier: dict[str, Any] | None, manifest: dict[str, Any], episode: int
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]] | None:
    """``(cuts before, cuts now)`` of an episode made again with another cut."""
    now = (manifest.get("episodes") or {}).get(str(episode))
    if not isinstance(earlier, dict) or not isinstance(now, dict):
        return None
    old, new = _cuts(earlier), _cuts(now)
    return (old, new) if old != new else None


# ---------------------------------------------------------------- the maker


class AnimeAudiobookMaker:
    """Background jobs that make anime audiobooks (one job per title at a time)."""

    def __init__(
        self,
        *,
        audiobooks: Any,
        root: Path,
        cache_dir: Path,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        job_center: Any | None = None,
    ) -> None:
        self.audiobooks = audiobooks
        self.root = Path(root)
        self.cache_dir = Path(cache_dir)
        self.ffmpeg = str(ffmpeg or "ffmpeg")
        self.ffprobe = str(ffprobe or "ffprobe")
        self.job_center = job_center
        self._lock = threading.Lock()
        self._jobs: dict[int, dict[str, Any]] = {}
        self._cancel: dict[int, threading.Event] = {}
        self._threads: dict[int, threading.Thread] = {}
        self._processes: dict[int, subprocess.Popen[Any]] = {}

    # -- state

    @property
    def db(self) -> Any:
        return self.audiobooks.db

    def folder(self, media_id: int, title: str) -> Path:
        """The title's folder. One made under an earlier title (AniList renamed the anime, the
        title language changed) stays the title's folder: its books point into it, and a
        second folder made every episode and every book again."""
        expected = title_folder(self.root, media_id, title)
        if (expected / MARKER).is_file():
            return expected
        suffix = f" [{int(media_id)}]"
        try:
            earlier = sorted(
                item
                for item in self.root.iterdir()
                if item.name.endswith(suffix) and item != expected and (item / MARKER).is_file()
            )
        except OSError:
            return expected
        for item in earlier:
            try:
                if int(self.manifest(item).get("media_id") or media_id) == int(media_id):
                    return item
            except (TypeError, ValueError):
                continue
        return expected

    def manifest(self, folder: Path) -> dict[str, Any]:
        try:
            data = json.loads((folder / MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema": SCHEMA, "episodes": {}}
        if not isinstance(data, dict) or not isinstance(data.get("episodes"), dict):
            return {"schema": SCHEMA, "episodes": {}}
        return data

    def _save_manifest(self, folder: Path, data: dict[str, Any]) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write_json(folder / MARKER, data, ensure_ascii=False, indent=1)

    def job(self, media_id: int) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(int(media_id))
            return dict(job) if job else None

    def busy(self, media_id: int) -> bool:
        with self._lock:
            thread = self._threads.get(int(media_id))
            return bool(thread is not None and thread.is_alive())

    def busy_any(self) -> bool:
        with self._lock:
            return any(thread.is_alive() for thread in self._threads.values())

    def book_for_path(self, path: Path) -> int | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT id FROM audiobooks WHERE path=?", (str(path),)).fetchone()
        return int(row["id"]) if row is not None else None

    def _set_job(self, media_id: int, **values: Any) -> None:
        with self._lock:
            job = self._jobs.setdefault(int(media_id), {"media_id": int(media_id)})
            job.update(values, updated_at=time.time())
            job_id = str(job.get("job_id") or "")
            snapshot = dict(job)
        if self.job_center is not None and job_id and snapshot.get("state") == "running":
            try:
                self.job_center.update(
                    job_id,
                    state="running",
                    current=float(snapshot.get("progress") or 0.0) * 100.0,
                    total=100.0,
                    message=str(snapshot.get("message") or ""),
                )
            except Exception:  # noqa: BLE001 - the journal is informational
                pass

    # -- jobs

    def start(
        self,
        *,
        media_id: int,
        title: str,
        sources: list[EpisodeSource],
        whole: bool,
        cover: Path | None = None,
        skip_op_ed: bool = True,
    ) -> dict[str, Any]:
        media_id = int(media_id)
        if not sources:
            raise ValueError(err("animeAudiobook.noEpisodes"))
        with self._lock:
            thread = self._threads.get(media_id)
            if thread is not None and thread.is_alive():
                raise ValueError(err("animeAudiobook.busy"))
            cancel = threading.Event()
            self._cancel[media_id] = cancel
            job_id = ""
            if self.job_center is not None:
                try:
                    job_id = self.job_center.start(
                        "anime_audiobook",
                        title,
                        payload={"media_id": media_id, "whole": bool(whole)},
                        total=100,
                    )
                except Exception:  # noqa: BLE001
                    job_id = ""
            self._jobs[media_id] = {
                "media_id": media_id,
                "job_id": job_id,
                "state": "running",
                "whole": bool(whole),
                "episodes": [source.episode for source in sources],
                "progress": 0.0,
                "message": t("animeAudiobook.preparing"),
                "error": "",
                "audiobook_id": None,
                "started_at": time.time(),
                "updated_at": time.time(),
            }
            thread = threading.Thread(
                target=self._run,
                kwargs={
                    "media_id": media_id,
                    "title": title,
                    "sources": list(sources),
                    "whole": bool(whole),
                    "cover": cover,
                    "skip_op_ed": bool(skip_op_ed),
                    "cancel": cancel,
                },
                name=f"anime-audiobook-{media_id}",
                daemon=True,
            )
            self._threads[media_id] = thread
        thread.start()
        return self.job(media_id) or {}

    def cancel(self, media_id: int) -> bool:
        with self._lock:
            event = self._cancel.get(int(media_id))
            process = self._processes.get(int(media_id))
            alive = bool((thread := self._threads.get(int(media_id))) is not None and thread.is_alive())
        if event is None or not alive:
            return False
        event.set()
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        self._set_job(int(media_id), message=t("animeAudiobook.cancelling"))
        return True

    def wait(self, media_id: int, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._threads.get(int(media_id))
        if thread is not None:
            thread.join(timeout)

    def _run(
        self,
        *,
        media_id: int,
        title: str,
        sources: list[EpisodeSource],
        whole: bool,
        cover: Path | None,
        skip_op_ed: bool,
        cancel: threading.Event,
    ) -> None:
        job_id = str((self.job(media_id) or {}).get("job_id") or "")
        try:
            result = self.make(
                media_id=media_id,
                title=title,
                sources=sources,
                whole=whole,
                cover=cover,
                skip_op_ed=skip_op_ed,
                cancel=cancel,
                progress=lambda value, message: self._set_job(media_id, progress=value, message=message),
            )
            self._set_job(
                media_id,
                state="succeeded",
                progress=1.0,
                message=t("animeAudiobook.done"),
                audiobook_id=result["audiobook_id"],
                audiobook_ids=result["audiobook_ids"],
            )
            if self.job_center is not None and job_id:
                self.job_center.finish(job_id, message=t("animeAudiobook.done"), result=result)
        except Cancelled:
            self._set_job(media_id, state="cancelled", message=t("animeAudiobook.cancelled"))
            if self.job_center is not None and job_id:
                self.job_center.cancelled(job_id, message=t("animeAudiobook.cancelled"))
        except Exception as exc:  # noqa: BLE001 - shown to the person, logged
            LOGGER.warning("FAIL step=anime_audiobook media_id=%s error=%r", media_id, str(exc)[:500])
            self._set_job(media_id, state="failed", message=t("animeAudiobook.failed"), error=str(exc))
            if self.job_center is not None and job_id:
                self.job_center.fail(job_id, exc, message=t("animeAudiobook.failed"))

    def make(
        self,
        *,
        media_id: int,
        title: str,
        sources: list[EpisodeSource],
        whole: bool,
        cover: Path | None = None,
        skip_op_ed: bool = True,
        cancel: threading.Event | None = None,
        progress: Callable[[float, str], None] | None = None,
    ) -> dict[str, Any]:
        """Make (or reuse) the episodes' files and add their audiobooks (blocking)."""
        cancel = cancel or threading.Event()
        report = progress or (lambda _value, _message: None)
        folder = self.folder(media_id, title)
        sources = sorted(sources, key=lambda item: item.episode)
        manifest = self.manifest(folder)
        # The episodes as they were made before this run (a remade one may be cut otherwise).
        before = {
            str(key): dict(entry)
            for key, entry in (manifest.get("episodes") or {}).items()
            if isinstance(entry, dict)
        }
        earlier_title = str(manifest.get("title") or title)
        renamed = earlier_title != str(title)
        manifest.update({"schema": SCHEMA, "media_id": int(media_id), "title": str(title)})
        folder.mkdir(parents=True, exist_ok=True)
        if renamed or not (folder / MARKER).is_file():
            self._save_manifest(folder, manifest)
        if renamed:
            self._rename_shelf(folder, earlier_title, title)
        for stale in folder.glob(".partial-*"):
            stale.unlink(missing_ok=True)
        cover_jpg = self._cover(folder, cover)
        made: list[tuple[EpisodeSource, Path, float]] = []
        changes: dict[str, tuple[list[tuple[float, float]], list[tuple[float, float]]]] = {}
        probes: dict[str, tuple[float, list[dict[str, Any]], float]] = {}
        book_ids: list[int] = []
        try:
            # The whole run must fit before the first episode is made: a season that fills
            # the disk at episode 9 leaves eight files and no book.
            self._check_run_space(
                folder, manifest, sources, skip_op_ed=skip_op_ed, cancel=cancel, probes=probes
            )
            for position, source in enumerate(sources):
                if cancel.is_set():
                    raise Cancelled()

                def step(value: float, position: int = position, source: EpisodeSource = source) -> None:
                    report(
                        (position + max(0.0, min(1.0, value))) / len(sources) * 0.97,
                        t("animeAudiobook.extracting", episode=source.episode),
                    )

                step(0.0)
                output, duration, written = self._episode_audio(
                    folder,
                    manifest,
                    source,
                    cover=cover_jpg,
                    skip_op_ed=skip_op_ed,
                    cancel=cancel,
                    progress=step,
                    probes=probes,
                )
                if written:
                    change = _cut_change(before.get(str(source.episode)), manifest, source.episode)
                    if change is not None:
                        changes[str(output)] = change
                made.append((source, output, duration))
                self._save_manifest(folder, manifest)
                if not whole or self.book_for_path(output) is not None:
                    # The whole title is one book; its episodes stay single files of it
                    # (an episode book made before is refreshed with its file).
                    book_ids.append(
                        self._add_episode_book(media_id, title, source, output, duration, changes)
                    )
            whole_id = None
            if whole:
                report(0.98, t("animeAudiobook.assembling"))
                whole_id = self._add_whole_book(folder, title, made, changes)
            else:
                self._refresh_whole_book(folder, title, manifest, changes)
        except BaseException as exc:
            for stale in folder.glob(".partial-*"):
                stale.unlink(missing_ok=True)
            if isinstance(exc, Cancelled):
                # Cancelled (the whole anime, half way): the episodes made so far belong to no
                # book; they must not stay on the disk (nor the ones an earlier failed run
                # left). A failure keeps them: the dialog lists them as made, "Make" again
                # continues where it stopped, and closing the dialog removes them
                # (discard_unfinished).
                try:
                    self._remove_unreferenced(self._folder_files(folder))
                except Exception as error:  # noqa: BLE001 - the cancel itself is the answer
                    LOGGER.warning("SKIP step=anime_audiobook.cancel_cleanup error=%r", str(error)[:300])
            if (
                folder.is_dir()
                and not self.manifest(folder).get("episodes")
                and not any(folder.glob("*.m4a"))
            ):
                shutil.rmtree(folder, ignore_errors=True)
            raise
        self._forget_overrides()
        return {
            "media_id": int(media_id),
            "audiobook_id": whole_id if whole_id is not None else (book_ids[-1] if book_ids else None),
            "audiobook_ids": book_ids + ([whole_id] if whole_id is not None else []),
            "folder": str(folder),
        }

    # -- one episode

    def _cover(self, folder: Path, cover: Path | None) -> Path | None:
        target = folder / ".cover.jpg"
        if cover is None or not Path(cover).is_file():
            return target if target.is_file() else None
        try:
            if target.is_file() and target.stat().st_mtime >= Path(cover).stat().st_mtime:
                return target
            partial = folder / ".partial-cover.jpg"
            subprocess.run(
                [
                    self.ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(cover),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='min(600,iw)':-2",
                    "-q:v",
                    "3",
                    "-f",
                    "image2",
                    str(partial),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            partial.replace(target)
            return target
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.info("SKIP step=anime_audiobook.cover error=%r", str(exc)[:200])
            return target if target.is_file() else None

    def probe(self, video: Path) -> tuple[float, list[dict[str, Any]]]:
        duration, streams, _start = self._probe(video)
        return duration, streams

    def _probe(self, video: Path) -> tuple[float, list[dict[str, Any]], float]:
        """``(seconds, streams, start)``: ``start`` is the file's own first timestamp."""
        completed = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode != 0:
            raise ValueError(err("animeAudiobook.unreadable", error=completed.stderr.strip()[-300:]))
        payload = json.loads(completed.stdout or "{}")
        try:
            duration = float((payload.get("format") or {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        try:
            start = float((payload.get("format") or {}).get("start_time") or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        return max(0.0, duration), list(payload.get("streams") or []), start

    @staticmethod
    def _source_identity(video: Path) -> dict[str, Any]:
        stat = video.stat()
        return {"path": str(video), "size": int(stat.st_size), "mtime_ns": int(stat.st_mtime_ns)}

    def _subtitle_identity(self, source: EpisodeSource) -> str:
        if source.subtitle is None:
            return ""
        try:
            stat = Path(source.subtitle).stat()
        except OSError:
            return ""
        return f"{source.subtitle}:{stat.st_size}:{stat.st_mtime_ns}:{round(source.subtitle_delay, 3)}"

    def _episode_audio(
        self,
        folder: Path,
        manifest: dict[str, Any],
        source: EpisodeSource,
        *,
        cover: Path | None,
        skip_op_ed: bool,
        cancel: threading.Event,
        progress: Callable[[float], None],
        probes: dict[str, tuple[float, list[dict[str, Any]], float]] | None = None,
    ) -> tuple[Path, float, bool]:
        """``(file, seconds, written)``: ``written`` is False for a file reused as it was."""
        output = folder / episode_file_name(source.episode)
        entries = manifest.setdefault("episodes", {})
        video = Path(source.video)
        video_present = video.is_file()
        entry = self._reusable(folder, manifest, source, skip_op_ed=skip_op_ed)
        if entry is not None:
            if video_present and entry.get("subtitle") != self._subtitle_identity(source):
                self._write_read_along(output, source, entry)  # subtitles prepared later
            return output, float(entry.get("duration") or 0.0), False
        if not video_present:
            raise ValueError(err("animeAudiobook.videoMissing", episode=source.episode))
        duration, streams, container_start = (probes or {}).get(str(video)) or self._probe(video)
        stream = pick_audio_stream(streams)
        if stream is None:
            raise ValueError(err("animeAudiobook.noAudio", episode=source.episode))
        spans = merge_spans(source.segments, duration) if skip_op_ed else []
        kept = max(1.0, duration - sum(end - start for start, end in spans))
        self._check_space(folder, stream, kept)
        codec = str(stream.get("codec_name") or "").casefold()
        copy = codec == "aac" and not spans
        # An audio stream that starts later than the file (a delayed track): the made audio
        # starts at its own first sample, so the subtitle times move by that much.
        offset = audio_offset({"start_time": container_start}, stream)
        partial = folder / f".partial-{output.name}"
        command = [self.ffmpeg, "-nostdin", "-v", "error", "-y"]
        if offset and not spans:
            # Without the cut filter (which restarts the clock itself) the delay would stay
            # in the file as an empty edit that players count differently.
            command += ["-itsoffset", f"{-offset:.3f}"]
        command += ["-i", str(video)]
        if cover is not None:
            command += ["-i", str(cover)]
        command += ["-map", f"0:a:{int(stream['audio_index'])}"]
        if cover is not None:
            command += ["-map", "1:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
        if spans:
            keep = "+".join(f"between(t\\,{start:.3f}\\,{end:.3f})" for start, end in spans)
            command += ["-af", f"aselect='not({keep})',asetpts=N/SR/TB"]
        if copy:
            command += ["-c:a", "copy"]
        else:
            command += ["-c:a", "aac", "-b:a", AAC_BITRATE]
            if int(stream.get("channels") or 2) > 2:
                command += ["-ac", "2"]
        command += [
            "-vn" if cover is None else "-sn",
            "-dn",
            "-map_chapters",
            "-1",
            "-map_metadata",
            "-1",
            "-metadata",
            f"title={t('animeAudiobook.episodeChapter', episode=source.episode)}",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            "-f",
            "ipod",
            str(partial),
        ]
        self._run_ffmpeg(command, cancel=cancel, total=kept, progress=progress)
        partial.replace(output)
        result_duration, _ = self.probe(output)
        entry = {
            "file": output.name,
            "episode": int(source.episode),
            "episode_end": source.episode_end,
            "duration": result_duration or kept,
            "source": self._source_identity(video),
            "stream": int(stream["audio_index"]),
            "language": str((stream.get("tags") or {}).get("language") or ""),
            "codec": "copy" if copy else "aac",
            "cuts": [[round(a, 3), round(b, 3)] for a, b in spans],
            "audio_offset": offset,
            "skip_op_ed": bool(skip_op_ed),
            "version": EXTRACT_VERSION,
            "made_at": time.time(),
        }
        self._write_read_along(output, source, entry)
        entries[str(source.episode)] = entry
        return output, float(entry["duration"]), True

    def _reusable(
        self, folder: Path, manifest: dict[str, Any], source: EpisodeSource, *, skip_op_ed: bool
    ) -> dict[str, Any] | None:
        """The episode's manifest entry when its made file is used as it is."""
        entry = (manifest.get("episodes") or {}).get(str(source.episode))
        if not isinstance(entry, dict) or not (folder / episode_file_name(source.episode)).is_file():
            return None
        video = Path(source.video)
        if not video.is_file():
            return entry  # the video may be gone (deleted after watching): its audio is kept
        try:
            same = (
                entry.get("source") == self._source_identity(video)
                and bool(entry.get("skip_op_ed")) == bool(skip_op_ed)
                and int(entry.get("version") or 0) == EXTRACT_VERSION
            )
        except (OSError, TypeError, ValueError):
            return None
        return entry if same else None

    @staticmethod
    def _expected_bytes(stream: dict[str, Any], seconds: float) -> int:
        try:
            bitrate = float(stream.get("bit_rate") or 0.0)
        except (TypeError, ValueError):
            bitrate = 0.0
        bitrate = max(bitrate, 192_000.0)
        return int(seconds * bitrate / 8 * 1.1)

    def _check_run_space(
        self,
        folder: Path,
        manifest: dict[str, Any],
        sources: list[EpisodeSource],
        *,
        skip_op_ed: bool,
        cancel: threading.Event,
        probes: dict[str, tuple[float, list[dict[str, Any]], float]],
    ) -> None:
        """Refuse a run of several episodes that cannot fit (each episode is checked again)."""
        pending = [
            source
            for source in sources
            if self._reusable(folder, manifest, source, skip_op_ed=skip_op_ed) is None
        ]
        if len(pending) < 2:
            return  # one episode: its own check says it
        needed = 0
        for source in pending:
            if cancel.is_set():
                raise Cancelled()
            video = Path(source.video)
            try:
                probed = self._probe(video) if video.is_file() else None
            except (OSError, ValueError, subprocess.SubprocessError):
                probed = None
            stream = pick_audio_stream(probed[1]) if probed else None
            if probed is None or stream is None:
                # Unreadable here: the episode's own turn reports it. Counted by its size
                # (audio is a small part of a video) so the sum is not short by it.
                try:
                    needed += int(video.stat().st_size * 0.15)
                except OSError:
                    pass
                continue
            probes[str(video)] = probed
            duration = probed[0]
            spans = merge_spans(source.segments, duration) if skip_op_ed else []
            kept = max(1.0, duration - sum(end - start for start, end in spans))
            estimate = self._expected_bytes(stream, kept)
            try:
                estimate = min(estimate, int(video.stat().st_size))  # never more than the video
            except OSError:
                pass
            needed += estimate
        needed += FREE_SPACE_MARGIN_BYTES
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            return
        if free < needed:
            raise ValueError(
                err(
                    "animeAudiobook.noSpaceRun",
                    count=len(pending),
                    needed=f"{needed / 1024 / 1024:.0f}",
                    free=f"{free / 1024 / 1024:.0f}",
                )
            )

    def _check_space(self, folder: Path, stream: dict[str, Any], seconds: float) -> None:
        needed = self._expected_bytes(stream, seconds) + FREE_SPACE_MARGIN_BYTES
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            return
        if free < needed:
            raise ValueError(
                err(
                    "animeAudiobook.noSpace",
                    needed=f"{needed / 1024 / 1024:.0f}",
                    free=f"{free / 1024 / 1024:.0f}",
                )
            )

    def _run_ffmpeg(
        self,
        command: list[str],
        *,
        cancel: threading.Event,
        total: float,
        progress: Callable[[float], None],
    ) -> None:
        media_id = next(
            (key for key, event in list(self._cancel.items()) if event is cancel),
            None,
        )
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        if media_id is not None:
            with self._lock:
                self._processes[media_id] = process
        stderr: list[str] = []
        reader = threading.Thread(
            target=lambda: stderr.extend(process.stderr or []), name="anime-audiobook-ffmpeg-err", daemon=True
        )
        reader.start()
        try:
            for line in process.stdout or []:
                if cancel.is_set():
                    process.terminate()
                    break
                key, _, value = line.strip().partition("=")
                if (
                    key in {"out_time_us", "out_time_ms"}
                    and value.strip().lstrip("-").isdigit()
                    and total > 0
                ):
                    progress(min(1.0, int(value) / 1_000_000 / total))
            process.wait(timeout=60 if cancel.is_set() else None)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        finally:
            if media_id is not None:
                with self._lock:
                    self._processes.pop(media_id, None)
        reader.join(timeout=5)
        if cancel.is_set():
            raise Cancelled()
        if process.returncode != 0:
            raise ValueError(err("animeAudiobook.ffmpegFailed", error="".join(stderr).strip()[-400:]))

    def _cues(self, source: EpisodeSource) -> list[tuple[float, float, str]]:
        from .subtitle_formats import convert_to_plain_srt, parse_srt

        subtitle = source.subtitle
        if subtitle is None or not Path(subtitle).is_file():
            return []
        subtitle = Path(subtitle)
        suffix = subtitle.suffix.casefold()
        if suffix in {".ass", ".ssa"}:
            subtitle, _ = convert_to_plain_srt(subtitle, self.cache_dir, ffmpeg_path=self.ffmpeg)
        if subtitle.suffix.casefold() != ".srt":
            return []
        return parse_srt(subtitle)

    def _write_read_along(self, output: Path, source: EpisodeSource, entry: dict[str, Any]) -> None:
        sidecar = read_along_sidecar(output)
        try:
            cues = self._cues(source)
        except Exception as exc:  # noqa: BLE001 - the audio is the book; text is extra
            LOGGER.info("SKIP step=anime_audiobook.subtitles error=%r", str(exc)[:200])
            cues = []
        spans = [(float(a), float(b)) for a, b in entry.get("cuts") or []]
        try:
            lead = float(entry.get("audio_offset") or 0.0)
        except (TypeError, ValueError):
            lead = 0.0
        chapter = read_along_chapter(
            remap_cues(cues, spans, delay=float(source.subtitle_delay or 0.0), audio_start=lead),
            index=0,
            title=t("animeAudiobook.episodeChapter", episode=source.episode),
            duration=float(entry.get("duration") or 0.0),
        )
        entry["subtitle"] = self._subtitle_identity(source)
        entry["read_along"] = chapter is not None
        if chapter is None:
            sidecar.unlink(missing_ok=True)
            return
        atomic_write_json(sidecar, chapter, ensure_ascii=False, separators=(",", ":"))

    # -- library rows

    def _chapter_title(self, source: EpisodeSource) -> str:
        if source.episode_end and source.episode_end > source.episode:
            return t("animeAudiobook.episodesChapter", first=source.episode, last=source.episode_end)
        return t("animeAudiobook.episodeChapter", episode=source.episode)

    def _add_episode_book(
        self,
        media_id: int,
        title: str,
        source: EpisodeSource,
        output: Path,
        duration: float,
        changes: dict[str, Any] | None = None,
    ) -> int:
        chapter = self._chapter_title(source)
        place = self._place(output)
        book = self.audiobooks._upsert(
            path=output,
            title=f"{title} — {chapter}",
            duration=duration,
            files=[
                {
                    "index": 0,
                    "path": str(output),
                    "title": chapter,
                    "duration": duration,
                    "start": 0.0,
                    "end": duration,
                }
            ],
            chapters=[{"index": 0, "title": chapter, "start": 0.0, "end": duration}],
        )
        book_id = int(book["id"])
        self._keep_place(book_id, place, changes)
        volume = int(source.episode) if 1 <= int(source.episode) <= 999 else None
        with self.db.connect() as conn:
            library_overrides.store(conn, "audiobook", book_id, volume=volume, series_title=title)
        self._forget_overrides()
        return book_id

    def _place(self, path: Path) -> tuple[float, list[tuple[str, float, float]]] | None:
        """Where the listener is in the book of ``path`` (before its files are replaced)."""
        with self.db.connect() as conn:
            row = conn.execute("SELECT id,position FROM audiobooks WHERE path=?", (str(path),)).fetchone()
            if row is None or float(row["position"] or 0.0) <= 0:
                return None
            files = [
                (str(item["path"]), float(item["start"] or 0.0), float(item["duration"] or 0.0))
                for item in conn.execute(
                    "SELECT path,start,duration FROM audiobook_files WHERE book_id=? ORDER BY file_index",
                    (int(row["id"]),),
                )
            ]
        return float(row["position"]), files

    def _keep_place(
        self,
        book_id: int,
        place: tuple[float, list[tuple[str, float, float]]] | None,
        changes: dict[str, Any] | None,
    ) -> None:
        """The saved position follows its content: the same moment of the same episode, after
        an episode before it became shorter or longer, or was itself cut otherwise."""
        if place is None or not place[1]:
            return
        position, earlier = place
        path, start, length = earlier[0]
        for item in earlier:
            if item[1] <= position + 1e-6:
                path, start, length = item
        offset = min(max(0.0, position - start), length if length > 0 else position)
        change = (changes or {}).get(path)
        if change is not None:
            offset = remap_time(unmap_time(offset, change[0]), change[1])
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT start,duration FROM audiobook_files WHERE book_id=? AND path=?", (int(book_id), path)
            ).fetchone()
            if row is None:
                return  # the episode is no part of the book any more
            target = float(row["start"] or 0.0) + min(max(0.0, offset), float(row["duration"] or 0.0))
            if abs(target - position) < 0.05:
                return
            conn.execute("UPDATE audiobooks SET position=? WHERE id=?", (target, int(book_id)))
        LOGGER.info(
            "Anime audiobook position kept book=%s from=%.1f to=%.1f recut=%s",
            book_id,
            position,
            target,
            change is not None,
        )

    def _add_whole_book(
        self,
        folder: Path,
        title: str,
        made: list[tuple[EpisodeSource, Path, float]],
        changes: dict[str, Any] | None = None,
    ) -> int:
        place = self._place(folder)
        files, chapters, cursor = [], [], 0.0
        for index, (source, output, duration) in enumerate(made):
            chapter = self._chapter_title(source)
            files.append(
                {
                    "index": index,
                    "path": str(output),
                    "title": chapter,
                    "duration": duration,
                    "start": cursor,
                    "end": cursor + duration,
                }
            )
            chapters.append({"index": index, "title": chapter, "start": cursor, "end": cursor + duration})
            cursor += duration
        book = self.audiobooks._upsert(
            path=folder,
            title=t("animeAudiobook.wholeTitle", title=title),
            duration=cursor,
            files=files,
            chapters=chapters,
        )
        book_id = int(book["id"])
        self._keep_place(book_id, place, changes)
        with self.db.connect() as conn:
            library_overrides.store(conn, "audiobook", book_id, volume=None, series_title=title)
        self._forget_overrides()
        return book_id

    def _refresh_whole_book(
        self, folder: Path, title: str, manifest: dict[str, Any], changes: dict[str, Any] | None = None
    ) -> None:
        """An episode made again (another cut, a better release): the whole book follows."""
        book_id = self.book_for_path(folder)
        if book_id is None:
            return
        by_file = {
            str(entry.get("file")): entry
            for entry in (manifest.get("episodes") or {}).values()
            if isinstance(entry, dict)
        }
        made = []
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT path FROM audiobook_files WHERE book_id=? ORDER BY file_index", (book_id,)
            ).fetchall()
        for row in rows:
            path = Path(str(row["path"]))
            entry = by_file.get(path.name)
            if entry is None or path.parent != folder or not path.is_file():
                return  # not one of ours as expected: left as it is
            source = EpisodeSource(
                episode=int(entry.get("episode") or 0),
                video=Path(str((entry.get("source") or {}).get("path") or "")),
                episode_end=entry.get("episode_end"),
            )
            made.append((source, path, float(entry.get("duration") or 0.0)))
        if made:
            self._add_whole_book(folder, title, made, changes)

    def _rename_shelf(self, folder: Path, earlier: str, title: str) -> None:
        """The anime has another title now: the books made under the earlier one move to the
        new shelf name with the book made next (their own titles stay as they were made; a
        series name the user typed is kept)."""
        # As library_overrides.store keeps a series name.
        earlier = unicodedata.normalize("NFC", " ".join(str(earlier or "").split()))[:200]
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id,path FROM audiobooks").fetchall()
            known = library_overrides.load(conn, "audiobook")
            for row in rows:
                path = Path(str(row["path"]))
                if path != folder and path.parent != folder:
                    continue
                override = known.get(int(row["id"])) or {}
                if str(override.get("series_title") or "") != earlier:
                    continue
                volume = override.get("volume")
                library_overrides.store(conn, "audiobook", int(row["id"]), volume=volume, series_title=title)
        self._forget_overrides()

    def _forget_overrides(self) -> None:
        try:
            self.audiobooks._overrides = None
        except AttributeError:
            pass

    # -- deletion

    def prune_unreferenced(self, paths: Iterable[Path | str]) -> list[Path]:
        """Remove made files that no audiobook uses any more (never a video, never elsewhere).

        With them go the title's other made files that no book uses: episodes a failed
        "whole anime" run left behind have no book of their own to be deleted with.
        """
        candidates = [Path(str(path)) for path in paths if str(path or "")]
        seen = {str(path) for path in candidates}
        for folder in {path.parent for path in candidates}:
            for sibling in self._folder_files(folder):
                if str(sibling) not in seen:
                    seen.add(str(sibling))
                    candidates.append(sibling)
        return self._remove_unreferenced(candidates, skip_busy=True)

    def _folder_files(self, folder: Path) -> list[Path]:
        """Every made file of a title's folder (nothing for a folder that is not Pudge's)."""
        if not _marked_folder(folder):
            return []
        try:
            return sorted(item for item in Path(folder).glob("*.m4a") if item.is_file())
        except OSError:
            return []

    def _used_paths(self) -> set[str]:
        with self.db.connect() as conn:
            used = {str(row["path"]) for row in conn.execute("SELECT path FROM audiobook_files")}
            used |= {str(row["path"]) for row in conn.execute("SELECT path FROM audiobooks")}
        return used

    def unfinished_episodes(self, media_id: int, title: str) -> dict[int, Path]:
        """Episode -> made file that no audiobook uses (a "whole anime" run failed half way)."""
        folder = self.folder(media_id, title)
        entries = self.manifest(folder).get("episodes") or {}
        if not entries:
            return {}
        used = self._used_paths()
        result: dict[int, Path] = {}
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            output = folder / str(entry.get("file") or "")
            try:
                if output.suffix.casefold() == ".m4a" and output.is_file() and str(output) not in used:
                    result[int(key)] = output
            except (OSError, ValueError):
                continue
        return result

    def discard_unfinished(self, media_id: int, title: str) -> list[Path]:
        """Remove the made files no book uses (the dialog was closed without "Make" again)."""
        if self.busy(int(media_id)):
            return []
        folder = self.folder(media_id, title)
        removed = self._remove_unreferenced(self._folder_files(folder), skip_busy=True)
        with self._lock:
            job = self._jobs.get(int(media_id))
            if removed and job is not None and job.get("state") == "failed":
                self._jobs.pop(int(media_id), None)  # its "failed" line described those files
        return removed

    def _busy_title(self, folder: Path) -> bool:
        """A job is making this folder's title right now (it may be about to use the file)."""
        try:
            media_id = int(self.manifest(folder).get("media_id") or 0)
        except (TypeError, ValueError):
            media_id = 0
        if media_id:
            return self.busy(media_id)
        with self._lock:
            jobs = list(self._jobs)
        return any(self.busy(int(job)) for job in jobs)  # an unnamed folder: any job

    def _remove_unreferenced(self, paths: Iterable[Path | str], *, skip_busy: bool = False) -> list[Path]:
        removed: list[Path] = []
        candidates = [Path(str(path)) for path in paths if str(path or "")]
        if not candidates:
            return removed
        used = self._used_paths()
        for path in candidates:
            if path.suffix.casefold() != ".m4a" or str(path) in used:
                continue
            try:
                resolved = path.resolve()
                plain_file = path.is_file() and not path.is_symlink()
            except OSError:
                continue
            # The folder's marker says the file is Pudge's, not where the library is now: after
            # the library folder was changed in Settings the books still play (and are deleted)
            # from the old one. A link, or a folder without a readable marker, is never touched.
            if not plain_file or not _marked_folder(path.parent) or not _marked_folder(resolved.parent):
                continue
            folder = path.parent
            if skip_busy and self._busy_title(folder):
                # Only this title's job: another title being made kept every deleted
                # book's files on the disk for good.
                continue
            manifest = self.manifest(folder)
            episodes = manifest.get("episodes") or {}
            for key, entry in list(episodes.items()):
                if isinstance(entry, dict) and entry.get("file") == path.name:
                    episodes.pop(key)
            path.unlink(missing_ok=True)
            read_along_sidecar(path).unlink(missing_ok=True)
            removed.append(path)
            if episodes or any(folder.glob("*.m4a")):
                self._save_manifest(folder, manifest)
            else:
                shutil.rmtree(folder, ignore_errors=True)
        return removed

    # -- what exists

    def made_episodes(self, media_id: int, title: str) -> dict[int, int]:
        """Episode -> audiobook id of the episode books already in the library."""
        folder = self.folder(media_id, title)
        result: dict[int, int] = {}
        for key, entry in (self.manifest(folder).get("episodes") or {}).items():
            if not isinstance(entry, dict):
                continue
            output = folder / str(entry.get("file") or "")
            if not output.is_file():
                continue
            book_id = self.book_for_path(output)
            if book_id is not None:
                result[int(key)] = book_id
        return result

    def whole_book(self, media_id: int, title: str) -> int | None:
        return self.book_for_path(self.folder(media_id, title))
