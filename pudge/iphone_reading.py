"""Light novels and manga volumes packed for reading on the iPhone without the Mac.

A volume is prepared in the background into one package file, registered as an iPhone
resource (like a prepared episode) and downloaded through ``/api/v2/resources/<id>/content``
with its SHA-256, so the phone verifies it and can resume the transfer.

Package format (version 1)::

    b"PUDGEPK1" | header length (8 bytes, big-endian) | header JSON | file data

The header is ``{"format": 1, "files": [{"name", "offset", "size"}, ...]}``; offsets count
from the first byte after the header. ``package.json`` describes the volume:

* light novel: ``book`` (the reader's volume: chapters, linked audiobook), ``item`` (its
  library row), one ``chapter-<index>.json`` per chapter (paragraphs with Jiten tokens, as
  the phone reader gets them online), ``image-<name>`` illustrations and ``cover.jpg``;
* manga: ``item``, ``pages`` (file names in reading order), ``page-<n>.<ext>`` and, only when
  the Mac's OCR of the whole volume is complete, ``text-<n>.json`` (the OCR text boxes) and
  ``parsed.json`` (each text box split into words, for word cards without the Mac);
* manga-ocr: the OCR files alone, for a volume downloaded before its OCR was complete.

A manga volume has two revisions: ``pagesRevision`` (the archive) and ``ocrRevision`` (empty
until the OCR is complete), so finished OCR comes as a small update, not as the pages again.
``POST media/downloads/check`` answers the revisions of many downloaded volumes at once
(the phone's update check).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

MAGIC = b"PUDGEPK1"
FORMAT = 1
# Prepared packages kept on the Mac (older ones are prepared again when asked); a package
# younger than an hour stays too: another iPhone or iPad may still be downloading it.
_KEEP = 6
_KEEP_SECONDS = 3600
_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def _safe_name(name: str) -> str:
    if not name or len(name) > 120 or name.startswith(".") or not set(name) <= _NAME_CHARS:
        raise ValueError(f"Invalid package file name: {name!r}")
    return name


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class PackageWriter:
    """Writes a package without holding a whole manga volume in memory."""

    def __init__(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        self._data = tempfile.NamedTemporaryFile(dir=folder, prefix="data-", suffix=".tmp", delete=False)
        self._files: list[dict[str, Any]] = []
        self._names: set[str] = set()
        self._offset = 0

    def add(self, name: str, data: bytes) -> None:
        name = _safe_name(name)
        if name in self._names:
            raise ValueError(f"Duplicate package file: {name}")
        self._names.add(name)
        self._data.write(data)
        self._files.append({"name": name, "offset": self._offset, "size": len(data)})
        self._offset += len(data)

    def finish(self, target: Path) -> None:
        self._data.close()
        header = _json({"format": FORMAT, "files": self._files})
        temporary = target.with_name(target.name + ".tmp")
        try:
            with open(temporary, "wb") as output, open(self._data.name, "rb") as data:
                output.write(MAGIC + struct.pack(">Q", len(header)) + header)
                shutil.copyfileobj(data, output, 1024 * 1024)
            os.replace(temporary, target)
        finally:
            Path(self._data.name).unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)

    def discard(self) -> None:
        self._data.close()
        Path(self._data.name).unlink(missing_ok=True)


def read_package(path: Path) -> dict[str, bytes]:
    """{name: data} of a package (the phone's unpacking, for checks and tests)."""
    raw = Path(path).read_bytes()
    if raw[:8] != MAGIC:
        raise ValueError("Not a Pudge package")
    (length,) = struct.unpack(">Q", raw[8:16])
    header = json.loads(raw[16 : 16 + length])
    base = 16 + length
    files = {}
    for entry in header["files"]:
        start = base + int(entry["offset"])
        files[_safe_name(entry["name"])] = raw[start : start + int(entry["size"])]
    return files


class ReadingPackages:
    """Background preparation of light-novel and manga packages for the phone."""

    KINDS = ("ln", "manga", "manga-ocr")

    def __init__(self, media: Any) -> None:
        self.media = media
        self.lock = threading.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}

    @property
    def folder(self) -> Path:
        return Path(self.media.service.root) / "reading"

    # ------------------------------------------------------------------ state
    def state(self, kind: str, item: int) -> dict[str, Any]:
        revision = self.revision(kind, item)
        key = f"{kind}:{item}"
        prepared = self._prepared(key, revision)
        job = dict(self.jobs.get(key) or {})
        if job.get("state") != "preparing" and job.get("revision") != revision:
            job = {}  # a finished job for an older version of the volume
        value: dict[str, Any] = {
            **self.revisions(kind, item),
            "kind": kind,
            "id": item,
            "revision": revision,
            "state": "ready" if prepared else str(job.get("state") or "idle"),
            "progress": 1.0 if prepared else float(job.get("progress") or 0),
            "message": "" if prepared else str(job.get("message") or ""),
        }
        if prepared:
            value["package"] = {k: prepared[k] for k in ("resourceId", "sha256", "sizeBytes", "fileName")}
        return value

    def prepare(self, kind: str, item: int) -> dict[str, Any]:
        if kind == "manga-ocr" and not self.ocr_status(item)["complete"]:
            from .iphone_media import MediaError

            raise MediaError("The Mac has not finished the OCR of this volume yet")
        revision = self.revision(kind, item)
        key = f"{kind}:{item}"
        self._watch_deletions()
        with self.lock:
            job = self.jobs.get(key)
            # A running job may see the revision change (it parses chapters): never a second one.
            running = job and job.get("state") == "preparing"
            if self._prepared(key, revision) is None and not running:
                self.jobs[key] = {"state": "preparing", "progress": 0.0, "message": "", "revision": revision}
                threading.Thread(
                    target=self._worker,
                    args=(kind, item, revision),
                    name=f"iphone-reading-{kind}-{item}",
                    daemon=True,
                ).start()
        return self.state(kind, item)

    def revision(self, kind: str, item: int) -> str:
        """Changes when the volume's text, words, pages or OCR change (a new package is needed)."""
        return self.revisions(kind, item)["revision"]

    def revisions(self, kind: str, item: int) -> dict[str, Any]:
        if kind not in self.KINDS:
            raise KeyError("Unknown media operation")

        def digest(*parts: Any, of: str = kind) -> str:
            text = f"pk{FORMAT}:{of}:{item}:" + "|".join(map(str, parts))
            return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]

        from .iphone_media import MediaError

        service = self.media.service
        with service.db.connect() as conn:
            if kind == "ln":
                if conn.execute("SELECT 1 FROM ln_books WHERE id=?", (item,)).fetchone() is None:
                    raise MediaError("This light novel is no longer on the Mac")
                rows = conn.execute(
                    "SELECT chapter_index,title,text_hash FROM ln_chapters WHERE book_id=? ORDER BY chapter_index",
                    (item,),
                ).fetchall()
                parts = [f"{r['chapter_index']}:{r['text_hash']}:{r['title']}" for r in rows]
                try:
                    # Words appear once chapters are parsed (Jiten parse cache), as for read-along.
                    parsed = conn.execute(
                        "SELECT COUNT(*) FROM ln_parse_cache WHERE text_hash IN "
                        "(SELECT text_hash FROM ln_chapters WHERE book_id=?)",
                        (item,),
                    ).fetchone()[0]
                except Exception:  # noqa: BLE001 - no reader cache yet
                    parsed = 0
                return {"revision": digest(*parts, f"parsed:{parsed}")}
            row = conn.execute("SELECT path FROM manga_books WHERE id=?", (item,)).fetchone()
        if row is None:
            raise MediaError("This manga is no longer on the Mac")
        path = Path(str(row["path"]))
        stat = path.stat() if path.is_file() else None
        # Both are the volume's (the same for its full and its OCR-only package).
        pages = digest(
            "pages", path, stat.st_size if stat else 0, stat.st_mtime_ns if stat else 0, of="manga"
        )
        ocr = self.ocr_status(item)
        ocr_revision = digest("ocr", *ocr["fingerprint"], of="manga") if ocr["complete"] else ""
        revision = (
            (ocr_revision or digest("ocr-missing")) if kind == "manga-ocr" else digest(pages, ocr_revision)
        )
        return {
            "revision": revision,
            "pagesRevision": pages,
            "ocrRevision": ocr_revision,
            "ocrComplete": ocr["complete"],
        }

    # -------------------------------------------------------------------- OCR
    def ocr_status(self, item: int) -> dict[str, Any]:
        """The Mac's Mokuro OCR of a whole manga volume (complete only when nothing still runs)."""
        api = self.media.api
        book_status = getattr(api, "manga_ocr_book_status", None)
        status = dict((book_status(item) if book_status else api.manga.ocr_cache_status(item)) or {})
        total = max(0, int(status.get("total_pages") or 0))
        done = max(0, min(total, int(status.get("completed_pages") or 0)))
        running = bool(status.get("running"))
        complete = bool(status.get("complete")) and not running
        fingerprint: tuple[Any, ...] = (done,)
        try:
            # The OCR's own generation/revision change when pages are redone or corrected.
            fingerprint = (*api.manga._ocr_context(item), done)
        except Exception:  # noqa: BLE001 - older OCR store: the page count is enough
            pass
        return {
            "complete": complete,
            "running": running,
            "state": str(status.get("state") or ("ready" if complete else "idle")),
            "totalPages": total,
            "completedPages": done,
            "failedPages": max(0, int(status.get("failed") or status.get("failed_pages") or 0)),
            "progress": 1.0 if complete else (done / total if total else 0.0),
            "message": str(status.get("pause_reason") or status.get("wait_reason") or ""),
            "fingerprint": fingerprint,
        }

    def ocr(self, item: int, *, start: bool = False) -> dict[str, Any]:
        """OCR state for the phone; `start` asks the Mac to OCR the whole volume (Mokuro)."""
        from .iphone_media import MediaError

        self.revisions("manga", item)  # the volume exists
        if start and not self.ocr_status(item)["complete"]:
            try:
                self.media.api.start_manga_ocr_book(item)
            except Exception as exc:  # noqa: BLE001 - e.g. Mokuro not installed: shown on the phone
                raise MediaError(str(exc) or "The Mac could not start the OCR") from exc
        value = self.ocr_status(item)
        value.pop("fingerprint")
        value["ocrRevision"] = self.revisions("manga", item)["ocrRevision"]
        return value

    def check(self, value: dict[str, Any]) -> dict[str, Any]:
        """Current revisions of the phone's downloaded volumes.

        A volume deleted on the Mac is ``missing`` with ``deletedAt`` (seconds): the phone
        keeps it readable, marked, and removes it a week later unless it was opened since.
        """
        from .iphone_media import MediaError

        self._watch_deletions()
        items = []
        for row in (value.get("items") or [])[:200]:
            if not isinstance(row, dict) or row.get("kind") not in ("ln", "manga"):
                continue
            try:
                item = int(row.get("id"))
            except (TypeError, ValueError):
                continue
            try:
                items.append({"kind": row["kind"], "id": item, **self.revisions(row["kind"], item)})
            except MediaError:
                items.append(
                    {
                        "kind": row["kind"],
                        "id": item,
                        "missing": True,
                        "deletedAt": self._deleted_at(row["kind"], item),
                    }
                )
        return {"items": items}

    def _watch_deletions(self) -> None:
        """Deleting a light novel or manga volume on the Mac records when (for the phones)."""
        with self.media.service.db.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS iphone_deleted_volumes(kind TEXT NOT NULL,id INTEGER NOT NULL,"
                "deleted_at REAL NOT NULL,PRIMARY KEY(kind,id))"
            )
            for kind, table in (("ln", "ln_books"), ("manga", "manga_books")):
                if conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone():
                    conn.execute(
                        f"CREATE TRIGGER IF NOT EXISTS iphone_deleted_{kind} AFTER DELETE ON {table} BEGIN "
                        f"INSERT OR IGNORE INTO iphone_deleted_volumes(kind,id,deleted_at) "
                        f"VALUES('{kind}',OLD.id,CAST(strftime('%s','now') AS REAL)); END"
                    )

    def _deleted_at(self, kind: str, item: int) -> float:
        """When the volume was deleted; one deleted before this was recorded counts from now."""
        with self.media.service.db.connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO iphone_deleted_volumes(kind,id,deleted_at) VALUES(?,?,?)",
                (kind, item, time.time()),
            )
            row = conn.execute(
                "SELECT deleted_at FROM iphone_deleted_volumes WHERE kind=? AND id=?", (kind, item)
            ).fetchone()
        return float(row["deleted_at"])

    def _prepared(self, key: str, revision: str) -> dict[str, Any] | None:
        with self.media.service.db.connect() as conn:
            row = conn.execute(
                "SELECT result FROM iphone_prepared WHERE source=?", (f"reading:{key}",)
            ).fetchone()
        if row is None:
            return None
        result = json.loads(row["result"])
        target = Path(result["path"])
        if (
            result.get("revision") != revision
            or not target.is_file()
            or target.stat().st_size != result["sizeBytes"]
        ):
            return None
        return result

    # ----------------------------------------------------------------- worker
    def _worker(self, kind: str, item: int, revision: str) -> None:
        key = f"{kind}:{item}"
        writer = PackageWriter(self.folder)

        def progress(value: float) -> None:
            self.jobs.setdefault(key, {}).update(progress=max(0.0, min(0.99, value)))

        try:
            if kind == "ln":
                description = self._light_novel(item, writer, progress)
            elif kind == "manga":
                description = self._manga(item, writer, progress)
            else:
                description = self._manga_ocr(item, writer, progress)
            # Packing parses chapters (the parse cache grows): the package is of the volume now.
            revisions = self.revisions(kind, item)
            revision = revisions["revision"]
            description = {**{k: v for k, v in revisions.items() if k != "revision"}, **description}
            writer.add(
                "package.json",
                _json({"format": FORMAT, "kind": kind, "id": item, "revision": revision, **description}),
            )
            target = self.folder / f"{kind}-{item}-{revision}.pudgepkg"
            writer.finish(target)
            service = self.media.service
            sha = service.file_sha256(target)
            resource = str(uuid.uuid5(uuid.UUID(service.server_id), "reading:" + sha))
            stored = {
                "resourceId": resource,
                "sha256": sha,
                "sizeBytes": target.stat().st_size,
                "fileName": f"{kind}.pudgepkg",
                "revision": revision,
                "path": str(target),
            }
            with service.db.connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO iphone_resources(id,sha,size,path) VALUES(?,?,?,?)",
                    (resource, sha, stored["sizeBytes"], stored["path"]),
                )
                conn.execute(
                    "INSERT INTO iphone_prepared(source,size,mtime_ns,result) VALUES(?,?,?,?) "
                    "ON CONFLICT(source) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,result=excluded.result",
                    (f"reading:{key}", stored["sizeBytes"], 0, json.dumps(stored)),
                )
            self.jobs[key] = {"state": "ready", "progress": 1.0, "message": "", "revision": revision}
            self._prune()
        except Exception as exc:  # noqa: BLE001 - reported to the phone
            writer.discard()
            logger = getattr(getattr(self.media.service, "api", None), "logger", None)
            if logger is not None:
                logger.warning("FAIL iphone.reading_package key=%s error=%s", key, exc)
            self.jobs[key] = {
                "state": "failed",
                "progress": 0.0,
                "message": str(exc)[-300:] or "Preparation failed",
                "revision": revision,
            }

    def _prune(self) -> None:
        files = sorted(self.folder.glob("*.pudgepkg"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in files[_KEEP:]:
            if time.time() - path.stat().st_mtime > _KEEP_SECONDS:
                path.unlink(missing_ok=True)

    def _light_novel(self, book_id: int, writer: PackageWriter, progress: Any) -> dict[str, Any]:
        media = self.media
        book = media.light_novel(book_id)
        item = next((row for row in media.light_novels()["items"] if row["id"] == book_id), None)
        reader = media.api.light_novels
        settings = media._reader_settings()
        images: list[str] = []
        chapters = book["chapters"]
        for number, chapter in enumerate(chapters):
            index = int(chapter["index"])
            try:
                payload = reader.chapter(book_id, index)  # parsed now (Jiten), as the reader would
            except Exception:  # noqa: BLE001 - Jiten unavailable: the text without word marks
                payload = reader.chapter_fast(book_id, index)
            view = media._chapter_view(payload, settings)
            media.live_word_states(view["paragraphs"])  # word colours as the Mac reader shows them now
            view["parsing"] = False  # nothing will finish parsing on the phone
            writer.add(f"chapter-{index}.json", _json(view))
            for paragraph in view["paragraphs"]:
                name = paragraph.get("imageId")
                if name and name not in images:
                    images.append(name)
            progress((number + 1) / max(1, len(chapters)) * 0.85)
        kept = []
        for name in images:
            try:
                data, _kind = media.ln_image(name)
            except Exception:  # noqa: BLE001 - a missing illustration shows its placeholder
                continue
            writer.add(f"image-{name}", data)
            kept.append(name)
        try:
            writer.add("cover.jpg", media.cover("ln", book_id)[0])
        except Exception:  # noqa: BLE001 - no cover
            pass
        return {"title": book["title"], "book": book, "item": item, "images": kept}

    def _manga(self, book_id: int, writer: PackageWriter, progress: Any) -> dict[str, Any]:
        media = self.media
        item = media.manga_book(book_id)
        total = int(item["pages"] or 0)
        # OCR goes along only when the Mac finished the whole volume (never part of it).
        complete = self.ocr_status(book_id)["complete"]
        names: list[str] = []
        for page in range(total):
            data, kind = media.manga_page(book_id, page)
            extension = {"image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(kind, "jpg")
            name = f"page-{page}.{extension}"
            writer.add(name, data)
            names.append(name)
            progress((page + 1) / max(1, total) * (0.7 if complete else 0.95))
        if complete:
            self._ocr_files(book_id, total, writer, progress)
        try:
            writer.add("cover.jpg", media.cover("manga", book_id)[0])
        except Exception:  # noqa: BLE001 - no cover
            pass
        return {"title": item["title"], "item": item, "pages": names, "ocr": complete}

    def _manga_ocr(self, book_id: int, writer: PackageWriter, progress: Any) -> dict[str, Any]:
        from .iphone_media import MediaError

        item = self.media.manga_book(book_id)
        if not self.ocr_status(book_id)["complete"]:
            raise MediaError("The Mac has not finished the OCR of this volume yet")
        self._ocr_files(book_id, int(item["pages"] or 0), writer, progress)
        return {"title": item["title"], "ocr": True}

    def _ocr_files(self, book_id: int, total: int, writer: PackageWriter, progress: Any) -> None:
        """text-<n>.json for every page and parsed.json (each text box split into words once)."""
        media = self.media
        texts: list[str] = []
        seen: set[str] = set()
        for page in range(total):
            text = media.manga_text(book_id, page)
            writer.add(f"text-{page}.json", _json(text))
            for region in text["regions"]:
                if region["text"] not in seen:
                    seen.add(region["text"])
                    texts.append(region["text"])
            progress(0.7 + (page + 1) / max(1, total) * 0.2)
        views = media.parse_lines(texts) or []
        parsed = [{"text": text, "paragraphs": [view]} for text, view in zip(texts, views)]
        writer.add("parsed.json", _json({"items": parsed}))
