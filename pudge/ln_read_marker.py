"""The light-novel "read up to here" marker (one per volume, the row of ``ln_bookmarks``).

It is the reading-progress marker the Mac and every paired iPhone/iPad show in the text; it is
not a list of user bookmarks (audiobooks have those: ``audiobook_bookmarks``, ``/api/v2/bookmarks``).

Two kinds move it:

- ``manual``: "Read up to here" on a word. It sits exactly at that character. The newest manual
  marker wins; one sent late (read offline) still counts when it is further ahead, like the
  reading place (``IPhoneMedia._stale_position``).
- ``auto``: the reader's own progress (the Mac's 20-second dwell, a phone's saved place) and the
  sentence the paired audiobook reads. It only moves forward, and only once it is at least
  ``AUTO_MIN_CHARS`` characters past the marker (counted through the chapters in between). It
  never takes a newer manual marker of another device unless it is further ahead and newer.
  It is snapped to the start of its sentence; when that start is more than
  ``SENTENCE_BACK_CHARS`` back, the place the reader sent stays (the readers send the start of
  the line there). It is not character-precise.

Positions are counted in characters (Unicode code points) from the start of the chapter's stored
text (``ln_chapters.text``), the same count the Mac reader's offsets use (``offset`` = characters
before / chapter length, so the character is exact). The phone counts by its paragraphs: the
reader paragraph and a UTF-16 offset in its text (``phone_view`` / ``char_from_phone``).
"""

from __future__ import annotations

import re
from typing import Any

# About a third of a Mac screen at the default size and a few phone screens of lines: reading on
# moves the marker, scrolling around a page does not.
AUTO_MIN_CHARS = 300
SENTENCE_BACK_CHARS = 80

_SENTENCE_ENDS = frozenset("。！？!?．…‥")
_SPACES = frozenset(" \t　")
_OPENING = frozenset("「『（(［[【〈《〔｛{‘“")
_CLOSING = frozenset("」』）)］]】〉》〕｝}’”")
# Closing quotes and brackets after a sentence end still belong to that sentence.
_SENTENCE_TRAIL = _CLOSING | frozenset("\"'") | _SPACES
_IMAGE = re.compile(r"\[\[PUDGE_LN_IMAGE_URL:(.+?)\]\]")


def kind_of(source: Any) -> str:
    return "auto" if str(source or "").casefold() == "auto" else "manual"


def sentence_start(text: str, char: int, back: int = SENTENCE_BACK_CHARS) -> int:
    """Start of the sentence holding ``char``; ``char`` itself when that start is farther back
    than ``back`` characters. Line breaks and a paragraph's leading spaces are skipped forward.

    A sentence end inside a quote the place is not in (「待って！」と叫んだ) ends nothing."""
    size = len(text)
    char = max(0, min(size, int(char)))
    while char < size and text[char] in "\r\n":
        char += 1
    line = max(text.rfind("\n", 0, char), text.rfind("\r", 0, char)) + 1
    if char == line:
        while char < size and text[char] in _SPACES:
            char += 1
        return char
    low = max(line, char - max(0, int(back)))
    depth = 0
    for index in range(char - 1, low - 1, -1):
        symbol = text[index]
        if symbol in _CLOSING:
            depth += 1
        elif symbol in _OPENING:
            depth = max(0, depth - 1)
        elif symbol in _SENTENCE_ENDS and depth == 0:
            start = index + 1
            while start < size and text[start] in _SENTENCE_TRAIL:
                start += 1
            if start <= char:
                return start
    if line >= low:
        start = line
        while start < char and text[start] in _SPACES:
            start += 1
        return start
    return char


def distance(lengths: dict[int, int], start: tuple[int, int], end: tuple[int, int]) -> int:
    """Characters from ``start`` to ``end`` (chapter, character) through the chapters between;
    negative when ``end`` comes first."""
    if start[0] == end[0]:
        return end[1] - start[1]
    sign = 1 if end[0] > start[0] else -1
    first, last = (start, end) if sign > 0 else (end, start)
    between = sum(size for chapter, size in lengths.items() if first[0] < chapter < last[0])
    span = max(0, lengths.get(first[0], 0) - first[1]) + between + last[1]
    return sign * max(1, span)


def accepts(
    current: dict[str, Any] | None,
    kind: str,
    chapter: int,
    char: int,
    at: float,
    lengths: dict[int, int],
) -> bool:
    """Whether a new marker replaces ``current`` (``chapter_index``, ``char_offset``, ``source``,
    ``updated_at``)."""
    if not current:
        return True
    ahead = distance(
        lengths,
        (int(current.get("chapter_index") or 0), int(current.get("char_offset") or 0)),
        (int(chapter), int(char)),
    )
    stored_at = float(current.get("updated_at") or 0.0)
    if kind == "manual":
        return at >= stored_at or ahead > 0
    if ahead < AUTO_MIN_CHARS:
        return False
    return kind_of(current.get("source")) == "auto" or at > stored_at


# ------------------------------------------------------------------ the phone's paragraphs
def paragraphs(text: str) -> list[tuple[int, str]]:
    """The reader's paragraphs (``LightNovels.chapter``: stripped non-empty lines) with the
    character where each starts in the chapter text."""
    rows: list[tuple[int, str]] = []
    cursor = 0
    for value in (line.strip() for line in text.split("\n")):
        if not value:
            continue
        found = text.find(value, cursor)  # as the Mac reader finds them (lnReaderCharacterOffset)
        start = found if found >= 0 else cursor
        rows.append((start, value))
        cursor = max(cursor, start + len(value))
    return rows


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def phone_place(text: str, char: int) -> tuple[int, int]:
    """(reader paragraph, UTF-16 offset in its text) of a character; a character between
    paragraphs (a line break, stripped spaces) is the next paragraph's start."""
    rows = paragraphs(text)
    if not rows:
        return 0, 0
    for index, (start, value) in enumerate(rows):
        if char < start:
            return _next_text(rows, index), 0
        if char < start + len(value):
            if _IMAGE.fullmatch(value):
                return _next_text(rows, index + 1), 0
            return index, _utf16(value[: char - start])
    last = len(rows) - 1
    return last, _utf16(rows[last][1]) if not _IMAGE.fullmatch(rows[last][1]) else 0


def _next_text(rows: list[tuple[int, str]], index: int) -> int:
    for position in range(index, len(rows)):
        if not _IMAGE.fullmatch(rows[position][1]):
            return position
    return min(index, len(rows) - 1)


def char_from_phone(text: str, paragraph: int, utf16: int = 0) -> int:
    """The character at a phone place (reader paragraph, UTF-16 offset in its text)."""
    rows = paragraphs(text)
    if not rows:
        return 0
    start, value = rows[max(0, min(len(rows) - 1, int(paragraph)))]
    if _IMAGE.fullmatch(value):
        return start
    remaining, local = max(0, int(utf16)), 0
    for symbol in value:
        width = 2 if ord(symbol) > 0xFFFF else 1
        if remaining < width:
            break
        remaining -= width
        local += 1
    return start + local


def phone_paragraph_at_fraction(text: str, fraction: float) -> int:
    """The paragraph a phone's saved fraction names (``ChapterText.paragraph(atFraction:)``:
    sizes by characters, an illustration counts 1)."""
    sizes = [1 if _IMAGE.fullmatch(value) else max(1, len(value)) for _start, value in paragraphs(text)]
    total = sum(sizes)
    if not total:
        return 0
    remaining = int(total * max(0.0, min(1.0, float(fraction))))
    for index, size in enumerate(sizes):
        if remaining < size:
            return index
        remaining -= size
    return max(0, len(sizes) - 1)


def phone_view(marker: dict[str, Any] | None, text: str, device: str = "") -> dict[str, Any] | None:
    """The marker as the phone decodes it (``ReadMarker`` in PudgeCore)."""
    if not marker:
        return None
    paragraph, offset = phone_place(text, int(marker.get("char_offset") or 0))
    owner = str(marker.get("device_id") or "mac")
    name = str(marker.get("device_name") or "") or ("Mac" if owner == "mac" else "iPhone/iPad")
    return {
        "chapter": int(marker.get("chapter_index") or 0),
        "paragraph": paragraph,
        "paragraphOffset": offset,
        "char": int(marker.get("char_offset") or 0),
        "kind": kind_of(marker.get("source")),
        "at": float(marker.get("updated_at") or 0.0),
        "device": owner,
        "deviceName": name,
        "here": bool(device) and owner == device,
    }
