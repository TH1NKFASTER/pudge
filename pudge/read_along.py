"""Read-along text of an audiobook: the words that follow the audio.

Two sources, one document (``readalong.json`` schema 1, the file the iPhone reads):

* a light novel linked to the audiobook, on the audiobook clock of its alignment;
* an audiobook made from anime episodes (``anime_audiobooks``): the episode's prepared
  Japanese subtitles, OP/ED cuts already applied.

A chapter holds ``sentences`` (``start``/``end`` count spoken characters) and
``anchors`` (``[ms, offset]`` pairs, monotonic, on the book clock). The Mac player reads
the same data as a list of timed lines (``transcript``).
"""

from __future__ import annotations

from bisect import bisect_left
from pathlib import Path
from typing import Any

SENTENCE_END = frozenset("。！？!?…")
SENTENCE_CLOSE = frozenset("」』）)】〉》\"'")


def split_sentences(line: str) -> list[str]:
    pieces, current = [], ""
    for index, character in enumerate(line):
        current += character
        following = line[index + 1] if index + 1 < len(line) else ""
        if character in SENTENCE_END and following not in SENTENCE_END and following not in SENTENCE_CLOSE:
            pieces.append(current)
            current = ""
        elif (
            character in SENTENCE_CLOSE
            and current
            and len(current) > 1
            and current[-2] in SENTENCE_END
            and following not in SENTENCE_CLOSE
        ):
            pieces.append(current)
            current = ""
    if current:
        pieces.append(current)
    return pieces


def light_novel_document(db: Any, reader: Any, ln_id: int, alignment: dict[str, Any]) -> dict[str, Any]:
    """The linked light novel's text on the audiobook clock (``reader``: words, optional)."""
    from .iphone_media import compact_tokens, utf16_to_index
    from .reading_audio_alignment import (
        _IMAGE_PARAGRAPH_RE,
        _runtime_audio_position_index,
        normalize_reading_text,
    )

    with db.connect() as conn:
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
            group = compact_tokens(groups[parse_index], vocabulary, line) if parse_index < len(groups) else []
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
            for piece in split_sentences(line):
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


def linked_light_novel(db: Any, book_id: int) -> int | None:
    try:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT ln_book_id FROM reading_audio_links WHERE audiobook_id=? ORDER BY updated_at DESC LIMIT 1",
                (int(book_id),),
            ).fetchone()
    except Exception:  # noqa: BLE001 - no light-novel tables yet
        return None
    return int(row["ln_book_id"]) if row is not None else None


def audiobook_document(db: Any, audiobooks: Any, book_id: int) -> tuple[str, dict[str, Any] | None]:
    """``(source, document)`` of one audiobook: ``light_novel``, ``anime`` or ``("", None)``."""
    from .anime_audiobooks import read_along_document

    ln_id = linked_light_novel(db, int(book_id))
    if ln_id is None:
        return (
            ("anime", read_along_document(db, int(book_id))) if _anime_book(db, int(book_id)) else ("", None)
        )
    if audiobooks is None or not callable(getattr(audiobooks, "_load_alignment", None)):
        return "", None
    path = getattr(audiobooks, "_alignment_path", None)
    if callable(path) and not Path(path(ln_id, int(book_id))).is_file():
        return "", None  # not aligned yet (loading would only miss)
    alignment = audiobooks._load_alignment(ln_id, int(book_id))
    if not alignment:
        return "", None
    document = light_novel_document(db, None, ln_id, alignment)
    return ("light_novel", document) if document["chapters"] else ("", None)


def _anime_book(db: Any, book_id: int) -> bool:
    from .anime_audiobooks import is_anime_audiobook_path

    with db.connect() as conn:
        row = conn.execute("SELECT path FROM audiobooks WHERE id=?", (int(book_id),)).fetchone()
    return row is not None and is_anime_audiobook_path(str(row["path"]))


def _time_at(anchors: list[list[int]], offsets: list[int], offset: float) -> float:
    """Milliseconds at which ``offset`` is first reached (linear between anchors)."""
    if offset <= offsets[0]:
        return float(anchors[0][0])
    if offset >= offsets[-1]:
        return float(anchors[bisect_left(offsets, offsets[-1])][0])
    index = bisect_left(offsets, offset)
    (t0, o0), (t1, o1) = anchors[index - 1], anchors[index]
    if o1 == offset:
        return float(t1)
    return t0 + (t1 - t0) * (offset - o0) / (o1 - o0)


def transcript(document: dict[str, Any] | None) -> dict[str, Any]:
    """A read-along document as timed lines for the Mac player (seconds, sorted by start).

    ``lines``: ``{"start", "end", "text", "chapter"}``; ``chapter`` indexes ``chapters``.
    """
    lines: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    for chapter in (document or {}).get("chapters") or []:
        anchors = [
            [int(a), int(b)]
            for a, b in (chapter.get("anchors") or [])
            if isinstance(a, (int, float)) and isinstance(b, (int, float))
        ]
        sentences = chapter.get("sentences") or []
        if len(anchors) < 2 or not sentences:
            continue
        offsets = [anchor[1] for anchor in anchors]
        number = len(chapters)
        chapters.append(
            {"title": str(chapter.get("title") or ""), "start": int(chapter.get("startMs") or 0) / 1000}
        )
        for sentence in sentences:
            text = str(sentence.get("text") or "").strip()
            if not text:
                continue
            own = sentence.get("t")
            if (
                isinstance(own, list)
                and len(own) == 2
                and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in own)
            ):
                # The line's own times (subtitles): two lines shown at once start together,
                # which the monotonic anchors cannot say.
                start, end = float(own[0]), max(float(own[0]), float(own[1]))
            else:
                start = _time_at(anchors, offsets, float(sentence.get("start") or 0))
                end = max(start, _time_at(anchors, offsets, float(sentence.get("end") or 0)))
            lines.append(
                {
                    "start": round(start / 1000, 3),
                    "end": round(end / 1000, 3),
                    "text": text,
                    "chapter": number,
                }
            )
    lines.sort(key=lambda line: (line["start"], line["end"]))
    return {"chapters": chapters, "lines": lines}
