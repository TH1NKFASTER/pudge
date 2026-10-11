"""Reading, watching and study endpoints for the native iPhone client.

The Mac stays the single sender for every SRS action (limited mode): the
phone shows words and definitions and asks the Mac to add or review them.
All routes live under ``/api/v2/media/`` and require the paired bearer token.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable

from .word_states import DEFAULT_WORD_COLORS, WORD_STATES, canonical_state, normalized_word_state, word_color

# Compact knowledge state shared with the phone (Tokens: [start, end, wordId, readingIndex, state]):
# 0 new, 1 learning, 2 due, 3 known, 4 ignored. The nine word states go in the flags.
STATE_CODES = {
    "young": 1,
    "mature": 3,
    "due": 2,
    "mastered": 3,
    "redundant": 3,
    "blacklisted": 4,
    "suspended": 4,
}
_IMAGE_RE = re.compile(r"^\[\[PUDGE_LN_IMAGE_URL:(.+?)\]\]$")
# Stored LN illustrations are content-addressed files in the cover cache.
_LN_IMAGE_NAME = re.compile(r"ln-[0-9a-f]{24}\.(?:jpg|png|webp|gif)")
# The Mac Home page's section order (the iPhone's Anime tab follows it).
_HOME_ORDER = (
    "continue_watching",
    "new_ready",
    "completed_ready",
    "needs_action",
    "waiting",
    "download_available",
    "caught_up",
    "recently_watched",
    "dropped",
)
_SUBTITLE_BATCH_CHARS = 16000
# Words per iPhone request for offline dictionary entries.
_WORDS_PER_REQUEST = 200
# Longest wait for Jiten's live word states before a text goes to the phone (then as parsed).
_LIVE_STATE_SECONDS = 3.0
# Prepared episode copies (MP4) for the phones: past this total the least recently used go,
# never one a phone asked for in the last _VIDEO_PIN_SECONDS or is downloading now.
_VIDEO_BUDGET_BYTES = 16 * 1024**3
_VIDEO_PIN_SECONDS = 2 * 3600
# Media kinds a volume of which the phone can download for reading offline.
_READING_KINDS = ("ln", "manga")


class MediaError(Exception):
    """A user-facing failure (HTTP 422 with the message)."""


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf16_to_index(text: str) -> list[int]:
    """Map every UTF-16 offset (JS/Swift string index) to a Python index."""
    mapping: list[int] = []
    for index, character in enumerate(text):
        mapping.append(index)
        if ord(character) > 0xFFFF:
            mapping.append(index)
    mapping.append(len(text))
    return mapping


def word_state(card: dict[str, Any] | None) -> str:
    """One of WORD_STATES, picked like the Mac reader (reading_tools normalizeState); "" when unknown."""
    if not isinstance(card, dict):
        return ""
    explicit = str(card.get("normalizedState") or "").strip().casefold()
    if explicit in WORD_STATES:
        return explicit
    if explicit == "unknown":
        return ""  # another provider's IDs
    # No name, or an older cache's learning/known: the raw states decide (due first).
    states = list(card.get("cardState") or card.get("states") or card.get("knownState") or [])
    state = normalized_word_state(states)
    if state != "new":
        return state
    if explicit:
        state = canonical_state(explicit)
        return state if state in WORD_STATES else ""
    decks = card.get("studyDeckIds")
    return normalized_word_state(states, in_deck=bool(decks) if isinstance(decks, list) else None)


def state_code(card: dict[str, Any] | None) -> int:
    return STATE_CODES.get(word_state(card), 0)


_RAW_STATES = (
    "new",
    "learning",
    "young",
    "mature",
    "due",
    "failed",
    "known",
    "mastered",
    "never-forget",
    "blacklisted",
)
FURIGANA_FLAG = 1  # the Mac reader shows furigana for this word (furigana_states)
MARK_FLAG = 2  # the Mac reader marks this word (underline_states)
MATURE_FLAG = 4  # mature/mastered on Jiten (plain text in Jiten's colours)
WORD_STATE_SHIFT = 7  # bits 7-10: WORD_STATES index + 1 (0 = unknown)


def raw_states(card: dict[str, Any] | None) -> list[str]:
    """Jiten states as the Mac reader matches them (lnCardRawStates)."""
    card = card if isinstance(card, dict) else {}
    values = [
        str(x).strip().casefold()
        for x in (card.get("cardState") or card.get("states") or card.get("knownState") or [])
    ]
    raw = sorted({x for x in values if x in _RAW_STATES})
    if raw:
        return raw
    normalized = str(card.get("normalizedState") or "").casefold()
    return [normalized] if normalized in _RAW_STATES else ["new"]


def _hiragana(text: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def _is_kana(text: str) -> bool:
    return bool(text) and all("\u3040" <= c <= "\u30ff" or c == "ー" for c in text)


def _has_kanji(text: str) -> bool:
    return any("\u3400" <= c <= "\u9fff" or c in "々〆ヵヶ" for c in text)


def token_ruby(
    token: dict[str, Any], text: str, start: int, end: int, card: dict[str, Any] | None
) -> list[list[Any]]:
    """Furigana as [start, end, reading] in paragraph UTF-16 units (Jiten rubies, else a reading fallback)."""
    rows: list[list[Any]] = []
    for ruby in token.get("rubies") or []:
        if not isinstance(ruby, dict):
            continue
        try:
            low = int(ruby.get("start"))
            high = int(ruby.get("end") if ruby.get("end") is not None else low + int(ruby.get("length") or 0))
        except (TypeError, ValueError):
            continue
        reading = str(ruby.get("text") or "")
        if reading and start <= low < high <= end:
            rows.append([low, high, reading])
    if rows:
        return rows
    mapping = utf16_to_index(text)
    surface = text[mapping[start] : mapping[min(end, len(mapping) - 1)]]
    if not _has_kanji(surface):
        return []
    card = card if isinstance(card, dict) else {}
    reading = re.sub(r"[^\u3040-\u30ffー]", "", str(token.get("reading") or ""))
    if not reading and str(card.get("spelling") or "") == surface:
        reading = re.sub(r"[^\u3040-\u30ffー]", "", str(card.get("reading") or ""))
    reading = _hiragana(reading)
    if not reading:
        return []
    # Okurigana stays outside the ruby (食べる/たべる -> 食/た).
    head, tail = 0, 0
    while (
        head < len(surface)
        and head < len(reading)
        and _is_kana(surface[head])
        and _hiragana(surface[head]) == reading[head]
    ):
        head += 1
    while (
        tail < len(surface) - head
        and tail < len(reading) - head
        and _is_kana(surface[-1 - tail])
        and _hiragana(surface[-1 - tail]) == reading[-1 - tail]
    ):
        tail += 1
    base, kana = surface[head : len(surface) - tail], reading[head : len(reading) - tail]
    if not base or not kana or not _has_kanji(base):
        return []
    low = start + utf16_length(surface[:head])
    return [[low, low + utf16_length(base), kana]]


def paragraph_view(
    text: str, group: Any, vocabulary: list[Any] | None, settings: Any = None
) -> dict[str, Any]:
    """One paragraph for the phone: tokens with state flags and furigana."""
    furigana, marked = _state_sets(settings)
    vocab = _vocabulary(vocabulary)
    tokens, ruby = [], []
    for row, token in _token_rows(group, vocab, text):
        card = token.get("card") if isinstance(token.get("card"), dict) else vocab.get((row[2], row[3]))
        tokens.append([*row, token_flags(card, furigana, marked)])
        ruby.extend(token_ruby(token, text, row[0], row[1], card))
    return {"text": text, "image": False, "tokens": tokens, "ruby": ruby}


# JitenMPV's KnownState order, and its collapse priority for words with several states.
JITEN_STATES = ("new", "young", "mature", "blacklisted", "due", "mastered", "redundant", "suspended")
_JITEN_PRIORITY = ("blacklisted", "mastered", "redundant", "suspended", "due", "mature", "young", "new")
_JITEN_ALIASES = {"learning": "young", "failed": "due", "known": "mastered", "never-forget": "mastered"}


def jiten_state(card: dict[str, Any] | None) -> int:
    """JitenMPV KnownState of a word (collapsed like KnownStates.Collapse)."""
    card = card if isinstance(card, dict) else {}
    values = {
        str(x).strip().casefold()
        for x in (card.get("cardState") or card.get("states") or card.get("knownState") or [])
    }
    values = {_JITEN_ALIASES.get(x, x) for x in values}
    if not values:
        normalized = str(card.get("normalizedState") or "").casefold()
        values = {{"learning": "young", "known": "mastered"}.get(normalized, normalized)}
    for name in _JITEN_PRIORITY:
        if name in values:
            return JITEN_STATES.index(name)
    return 0


def token_flags(card: dict[str, Any] | None, furigana: set[str], marked: set[str]) -> int:
    """Bits 0-2: furigana/mark/mature; bits 3-6: JitenMPV state + 1 (0 = unknown);
    bits 7-10: the reader's word state (WORD_STATES index + 1, 0 = unknown)."""
    states = set(raw_states(card))
    reader_state = word_state(card)
    return (
        (FURIGANA_FLAG if furigana & states else 0)
        | (MARK_FLAG if marked & states else 0)
        | (MATURE_FLAG if states & {"mature", "mastered", "never-forget", "known"} else 0)
        | ((jiten_state(card) + 1) << 3)
        | ((WORD_STATES.index(reader_state) + 1 if reader_state else 0) << WORD_STATE_SHIFT)
    )


def _state_sets(settings: Any) -> tuple[set[str], set[str]]:
    furigana = {
        x.strip()
        for x in str(getattr(settings, "furigana_states", "new,learning,young,mature,due,failed")).split(",")
        if x.strip()
    }
    marked = {
        x.strip()
        for x in str(getattr(settings, "underline_states", ",".join(_RAW_STATES))).split(",")
        if x.strip()
    }
    return furigana, marked


def _vocabulary(vocabulary: list[Any] | None) -> dict[tuple[int, int], dict[str, Any]]:
    vocab: dict[tuple[int, int], dict[str, Any]] = {}
    for item in vocabulary or []:
        if isinstance(item, dict):
            try:
                vocab[(int(item.get("wordId")), int(item.get("readingIndex")))] = item
            except (TypeError, ValueError):
                continue
    return vocab


def compact_tokens(group: Any, vocabulary: list[Any] | None, text: str) -> list[list[int]]:
    """Reader tokens as [start, end, wordId, readingIndex, state] in UTF-16 units."""
    return [row for row, _token in _token_rows(group, _vocabulary(vocabulary), text)]


def _token_rows(
    group: Any, vocab: dict[tuple[int, int], dict[str, Any]], text: str
) -> list[tuple[list[int], dict[str, Any]]]:
    limit = utf16_length(text)
    rows: list[tuple[list[int], dict[str, Any]]] = []
    for token in group if isinstance(group, list) else []:
        if not isinstance(token, dict):
            continue
        try:
            start = int(token.get("start") or 0)
            end = int(
                token.get("end") if token.get("end") is not None else start + int(token.get("length") or 0)
            )
        except (TypeError, ValueError):
            continue
        end = min(end, limit)
        if start < 0 or end <= start:
            continue
        try:
            word, reading = int(token.get("wordId") or 0), int(token.get("readingIndex") or 0)
        except (TypeError, ValueError):
            word, reading = 0, 0
        card = token.get("card") if isinstance(token.get("card"), dict) else vocab.get((word, reading))
        rows.append(([start, end, max(0, word), max(0, reading), state_code(card)], token))
    rows.sort(key=lambda item: (item[0][0], item[0][1]))
    return rows


def _pairs(rows: Any, limit: int) -> list[tuple[int, int]]:
    """[[wordId, readingIndex], ...] from a request, invalid rows skipped."""
    pairs: list[tuple[int, int]] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, list) and len(row) >= 2:
            try:
                pairs.append((int(row[0]), int(row[1])))
            except (TypeError, ValueError):
                continue
    return pairs[:limit]


def word_offsets(
    paragraph: str, tokens: list[list[int]], base: int, normalize: Callable[[str], str]
) -> list[int]:
    """Normalized read-along offset where each token starts (as the Mac reader counts it)."""
    mapping = utf16_to_index(paragraph)
    result = []
    for start, *_rest in tokens:
        index = mapping[min(start, len(mapping) - 1)]
        result.append(base + len(normalize(paragraph[:index])))
    return result


class IPhoneMedia:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.lock = threading.Lock()
        self.video_jobs: dict[int, dict[str, Any]] = {}
        # Prepared videos a phone asked for (path -> time) or is downloading (path -> open transfers).
        self._video_lock = threading.Lock()
        self._video_used: dict[str, float] = {}
        self._video_streams: dict[str, int] = {}
        from .iphone_reading import ReadingPackages

        # Light novels and manga volumes downloaded to the phone for reading offline.
        self.reading = ReadingPackages(self)

    # ---------------------------------------------------------------- plumbing
    @property
    def api(self) -> Any:
        api = getattr(self.service, "api", None)
        if api is None:
            raise MediaError("The Mac app is still starting")
        return api

    def handle(
        self, method: str, route: str, query: dict[str, list[str]], body: Callable[[], dict[str, Any]]
    ) -> Any:
        parts = route.removeprefix("/api/v2/media/").strip("/").split("/")
        head, rest = parts[0], parts[1:]
        number = lambda text: int(text) if text.isdigit() else -1  # noqa: E731
        if method == "GET" and head == "ln" and not rest:
            return self.light_novels()
        if head == "ln" and len(rest) == 1 and method == "GET":
            return self.light_novel(number(rest[0]))
        if head == "ln" and len(rest) == 3 and rest[1] == "chapters" and method == "GET":
            return self.chapter(number(rest[0]), number(rest[2]), poll=query.get("poll", ["0"])[0] == "1")
        if head == "ln" and len(rest) == 2 and rest[1] == "position" and method == "POST":
            return self.ln_position(number(rest[0]), body())
        if (
            head == "manga"
            and len(rest) == 2
            and rest[1] in {"ocr", "ocr-package"}
            and method in {"GET", "POST"}
        ):
            if rest[1] == "ocr":
                return self.reading.ocr(number(rest[0]), start=method == "POST")
            if method == "POST":
                return self.reading.prepare("manga-ocr", number(rest[0]))
            return self.reading.state("manga-ocr", number(rest[0]))
        if head == "downloads" and rest == ["check"] and method == "POST":
            return self.reading.check(body())
        if head in _READING_KINDS and len(rest) == 2 and rest[1] == "package":
            if method == "POST":
                return self.reading.prepare(head, number(rest[0]))
            if method == "GET":
                return self.reading.state(head, number(rest[0]))
        if head == "ln-image" and len(rest) == 1 and method == "GET":
            return self.ln_image(rest[0])
        if head == "manga" and not rest and method == "GET":
            return self.manga_list()
        if head == "manga" and len(rest) == 1 and method == "GET":
            return self.manga_book(number(rest[0]))
        if head == "manga" and len(rest) == 3 and rest[1] == "pages" and method == "GET":
            return self.manga_page(number(rest[0]), number(rest[2]))
        if (
            head == "manga"
            and len(rest) == 4
            and rest[1] == "pages"
            and rest[3] == "text"
            and method == "GET"
        ):
            return self.manga_text(number(rest[0]), number(rest[2]))
        if head == "manga" and len(rest) == 2 and rest[1] == "position" and method == "POST":
            return self.manga_position(number(rest[0]), body())
        if head == "episodes" and not rest and method == "GET":
            return self.episodes()
        if head == "anime" and not rest and method == "GET":
            return self.anime()
        if head == "study" and rest == ["states"] and method == "POST":
            return self.study_states(body())
        if head == "episodes" and len(rest) == 1 and method == "GET":
            return self.episode(number(rest[0]))
        if head == "episodes" and len(rest) == 2 and rest[1] == "prepare" and method == "POST":
            return self.prepare_episode(number(rest[0]))
        if head == "episodes" and len(rest) == 2 and rest[1] == "subtitles" and method == "GET":
            return self.episode_subtitles(number(rest[0]))
        if head == "episodes" and len(rest) == 2 and rest[1] == "position" and method == "POST":
            return self.episode_position(number(rest[0]), body())
        if head == "cover" and len(rest) == 2 and method == "GET":
            return self.cover(rest[0], number(rest[1]))
        if head == "study" and rest == ["options"] and method == "GET":
            return self.study_options()
        if head == "study" and rest == ["word"] and method == "POST":
            return self.study_word(body())
        if head == "study" and rest == ["words"] and method == "POST":
            return self.study_words(body())
        if head == "study" and rest == ["parse"] and method == "POST":
            return self.study_parse(body())
        if head == "study" and rest == ["action"] and method == "POST":
            return self.study_action(body())
        if head == "study" and rest == ["deck"] and method == "POST":
            return self.study_deck(body())
        if (
            head == "episodes"
            and len(rest) == 3
            and rest[1] == "gate"
            and rest[2] == "begin"
            and method == "POST"
        ):
            return self.gate_begin(number(rest[0]))
        if (
            head == "episodes"
            and len(rest) == 3
            and rest[1] == "gate"
            and rest[2] == "review"
            and method == "POST"
        ):
            return self.gate_review(number(rest[0]), body())
        if head == "reader" and not rest and method == "GET":
            return self.reader_settings()
        if head == "subtitle-look" and not rest and method == "GET":
            return subtitle_look()
        if head == "episodes" and len(rest) == 2 and rest[1] == "watched" and method == "POST":
            return self.episode_watched(number(rest[0]), body())
        if head == "stats" and not rest and method == "GET":
            return self.stats(query.get("period", ["30d"])[0])
        if head == "planning" and not rest and method == "GET":
            return self.planning()
        raise KeyError("Unknown media operation")

    @staticmethod
    def _rows(conn: Any, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        try:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]
        except Exception:  # noqa: BLE001 - a library type may not exist yet
            return []

    # ------------------------------------------------------------ light novels
    def light_novels(self) -> dict[str, Any]:
        with self.service.db.connect() as conn:
            books = self._rows(
                conn,
                "SELECT id,title,volume,current_chapter,current_offset,finished,cover_url,updated_at FROM ln_books ORDER BY updated_at DESC",
            )
            counts = {
                int(r["book_id"]): int(r["n"])
                for r in self._rows(conn, "SELECT book_id,COUNT(*) AS n FROM ln_chapters GROUP BY book_id")
            }
            linked = {
                int(r["ln_book_id"]) for r in self._rows(conn, "SELECT ln_book_id FROM reading_audio_links")
            }
        # Series and franchise grouping exactly as the Mac library computes it.
        grouping: dict[int, dict[str, Any]] = {}
        light_novels = getattr(getattr(self.service, "api", None), "light_novels", None)
        if light_novels is not None:
            try:
                grouping = {
                    int(b["id"]): b for b in light_novels.state().get("books") or [] if isinstance(b, dict)
                }
            except Exception:  # noqa: BLE001 - plain list without grouping
                grouping = {}
        from .light_novels import _series_key, _series_title

        items = []
        for book in books:
            book_id = int(book["id"])
            title = str(book["title"] or f"Light novel {book_id}")
            group = grouping.get(book_id, {})
            items.append(
                {
                    "id": book_id,
                    "title": title,
                    "volume": int(book["volume"]) if book.get("volume") else None,
                    "chapters": counts.get(book_id, 0),
                    "chapter": int(book["current_chapter"] or 0),
                    "offset": float(book["current_offset"] or 0),
                    "finished": bool(book["finished"]),
                    "hasCover": bool(book.get("cover_url")),
                    "hasAudio": book_id in linked,
                    "updatedAt": float(book["updated_at"] or 0),
                    "seriesKey": str(group.get("series_key") or _series_key(title) or f"book:{book_id}"),
                    "seriesTitle": str(group.get("series_title") or _series_title(title) or title),
                    "franchiseKey": str(group.get("franchise_key") or ""),
                    "franchiseTitle": str(group.get("franchise_title") or ""),
                    "franchiseOrder": int(group.get("franchise_order") or 0),
                    "progress": float(group.get("reading_progress") or (1.0 if book["finished"] else 0.0)),
                }
            )
        return {"items": items}

    def light_novel(self, book_id: int) -> dict[str, Any]:
        with self.service.db.connect() as conn:
            rows = self._rows(
                conn,
                "SELECT id,title,current_chapter,current_offset,finished FROM ln_books WHERE id=?",
                (book_id,),
            )
            chapters = self._rows(
                conn,
                "SELECT chapter_index,title,LENGTH(text) AS size FROM ln_chapters WHERE book_id=? ORDER BY chapter_index",
                (book_id,),
            )
        if not rows:
            raise MediaError("This light novel is no longer on the Mac")
        book = rows[0]
        audio_asset = None
        with self.service.db.connect() as conn:
            link = self._rows(
                conn,
                "SELECT audiobook_id FROM reading_audio_links WHERE ln_book_id=? ORDER BY updated_at DESC LIMIT 1",
                (book_id,),
            )
        if link:
            try:
                audio_asset = self.service.asset_for_book(int(link[0]["audiobook_id"]))
            except Exception:  # noqa: BLE001
                audio_asset = None
        return {
            "id": book_id,
            "title": str(book["title"]),
            "audioAssetId": audio_asset,
            "chapter": int(book["current_chapter"] or 0),
            "offset": float(book["current_offset"] or 0),
            "finished": bool(book["finished"]),
            "chapters": [
                {
                    "index": int(c["chapter_index"]),
                    "title": str(c["title"] or ""),
                    "characters": int(c["size"] or 0),
                }
                for c in chapters
            ],
        }

    def chapter(self, book_id: int, index: int, *, poll: bool = False) -> dict[str, Any]:
        api = self.api
        if poll:
            status = api.light_novel_chapter_parse_status(book_id, index)
            payload = status.get("payload") if status.get("ready") else None
            if payload is None:
                return {"parsing": True, "index": index, "title": "", "paragraphs": []}
        else:
            payload = api.light_novel_chapter(book_id, index)
        view = self._chapter_view(payload, self._reader_settings())
        self.live_word_states(view["paragraphs"])
        return view

    def _reader_settings(self) -> Any:
        try:
            return self.service.api.light_novels.settings()
        except Exception:  # noqa: BLE001 - defaults match the Mac reader's
            return None

    def live_word_states(self, views: list[Any]) -> None:
        """Words of texts for the phone take the Jiten states the Mac reader colours them by.

        The Mac window asks Jiten for the states of a chapter's words after it opens (the
        parse keeps the states of its parse time, without deck membership); the same batched
        lookup and its 45 s cache are used here, for at most _LIVE_STATE_SECONDS. Words left
        without an answer keep the states they were parsed with.
        """
        pairs = [
            (token[2], token[3])
            for view in views
            if isinstance(view, dict)
            for token in view.get("tokens") or []
            if isinstance(token, list) and len(token) >= 6 and token[2] > 0
        ]
        states = self._live_states(pairs) if pairs else {}
        if not states:
            return
        furigana, marked = _state_sets(self._reader_settings())
        for view in views:
            if not isinstance(view, dict):
                continue
            for token in view.get("tokens") or []:
                row = (
                    states.get((token[2], token[3])) if isinstance(token, list) and len(token) >= 6 else None
                )
                if row is not None:
                    token[4], token[5] = state_code(row), token_flags(row, furigana, marked)

    def _live_states(self, pairs: list[tuple[int, int]]) -> dict[tuple[int, int], dict[str, Any]]:
        api = getattr(self.service, "api", None)
        reader = getattr(api, "light_novels", None)
        try:
            settings = reader.settings()
            if str(getattr(settings, "study_backend", "") or "jiten").casefold() != "jiten":
                return {}  # the Mac reader asks Jiten only with Jiten as the study provider
            if not str(getattr(settings, "jiten_api_key", "") or "").strip():
                return {}
            account = str(reader.study_provider_capabilities("jiten").get("account_key") or "")
        except Exception:  # noqa: BLE001 - no reader service (starting) or no Jiten account
            return {}
        if not account:
            return {}
        ordered = list(dict.fromkeys(pairs))
        found: dict[tuple[int, int], dict[str, Any]] = {}
        lock = threading.Lock()
        done = threading.Event()
        started = time.monotonic()
        deadline = started + _LIVE_STATE_SECONDS

        def lookup() -> None:
            try:
                for offset in range(0, len(ordered), 500):  # the window's batch size
                    if time.monotonic() >= deadline:
                        break
                    try:
                        rows = reader.jiten_live_states(ordered[offset : offset + 500], force=False)
                    except Exception:  # noqa: BLE001 - Jiten unavailable: these words stay as parsed
                        continue
                    with lock:
                        for row in rows or []:
                            if isinstance(row, dict):
                                found[(int(row.get("wordId") or 0), int(row.get("readingIndex") or 0))] = row
            finally:
                done.set()

        # A slow Jiten never holds the phone longer than the budget; a late answer still
        # fills the Mac's cache for the phone's next request.
        threading.Thread(target=lookup, name="iphone-live-states", daemon=True).start()
        done.wait(max(0.0, deadline - time.monotonic()))
        with lock:
            result = dict(found)
        try:
            if str(reader.study_provider_capabilities("jiten").get("account_key") or "") != account:
                return {}  # the Jiten account changed meanwhile: these states are the other account's
        except Exception:  # noqa: BLE001
            return {}
        if len(result) < len(ordered):
            logger = getattr(api, "logger", None)
            if logger is not None:
                logger.info(
                    "iPhone live word states: %s of %s words in %.1fs, the rest as parsed",
                    len(result),
                    len(ordered),
                    time.monotonic() - started,
                )
        return result

    @staticmethod
    def _chapter_view(payload: dict[str, Any], settings: Any = None) -> dict[str, Any]:
        paragraphs = payload.get("paragraphs") or []
        tokens = payload.get("tokens") or []
        vocabulary = payload.get("vocabulary") or []
        rows = []
        for index, text in enumerate(paragraphs):
            text = str(text or "")
            image = _IMAGE_RE.fullmatch(text)
            if image:
                name = Path(image.group(1)).name
                rows.append(
                    {
                        "text": "",
                        "image": True,
                        "tokens": [],
                        "ruby": [],
                        "imageId": name if _LN_IMAGE_NAME.fullmatch(name) else None,
                    }
                )
                continue
            group = tokens[index] if index < len(tokens) else []
            rows.append(paragraph_view(text, group, vocabulary, settings))
        return {
            "parsing": bool(payload.get("parsing")),
            "index": int(payload.get("chapter_index") or 0),
            "title": str(payload.get("title") or ""),
            "paragraphs": rows,
        }

    def ln_image(self, name: str, *, width: int = 1600) -> tuple[bytes, str]:
        """An illustration of a light novel (the Mac reader's inline image), sized for the phone."""
        if not _LN_IMAGE_NAME.fullmatch(str(name or "")):
            raise MediaError("No such illustration")
        folders = [
            getattr(getattr(self.service.api, "light_novels", None), "cover_cache_dir", None),
            *getattr(self.service, "cover_dirs", []),
        ]
        source = next(
            (Path(folder) / name for folder in folders if folder and (Path(folder) / name).is_file()), None
        )
        if source is None:
            raise MediaError("No such illustration")
        data = source.read_bytes()
        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width <= width and source.suffix.casefold() in {".jpg", ".png"}:
                    return data, "image/png" if source.suffix.casefold() == ".png" else "image/jpeg"
        except OSError as exc:
            raise MediaError("No such illustration") from exc
        return self._thumbnail(data, width=width), "image/jpeg"

    @staticmethod
    def _observed(value: dict[str, Any]) -> float | None:
        """When the phone read there (seconds); None from older apps."""
        try:
            observed = float(value.get("observedAt"))
        except (TypeError, ValueError):
            return None
        return observed if observed > 0 else None

    def _stale_position(
        self, sql: str, item: int, observed: float | None, behind: Callable[[Any], bool]
    ) -> bool:
        """A place read earlier on one device (sent late, e.g. after reading offline) never takes
        a newer place from another device or the Mac back; a place further ahead always counts."""
        if observed is None:
            return False
        with self.service.db.connect() as conn:
            row = conn.execute(sql, (item,)).fetchone()
        return bool(row is not None and float(row[-1] or 0) > observed and behind(row))

    def ln_position(self, book_id: int, value: dict[str, Any]) -> dict[str, Any]:
        chapter = int(value.get("chapter") or 0)
        offset = max(0.0, min(1.0, float(value.get("offset") or 0)))
        stale = self._stale_position(
            "SELECT current_chapter,current_offset,updated_at FROM ln_books WHERE id=?",
            book_id,
            self._observed(value),
            lambda row: (chapter, offset) <= (int(row[0] or 0), float(row[1] or 0)),
        )
        if stale and value.get("finished") is not True:
            return {"ok": True, "kept": "newer"}
        if not stale:
            self.api.light_novel_position(book_id, chapter, offset)
        if value.get("finished") is True:
            try:
                self.api.light_novel_set_finished(book_id, True)
            except Exception as exc:  # noqa: BLE001 - AniList may be offline; the position is saved
                return {"ok": True, "warning": str(exc)}
        return {"ok": True}

    # ------------------------------------------------------------------- manga
    def manga_list(self) -> dict[str, Any]:
        with self.service.db.connect() as conn:
            rows = self._rows(
                conn,
                "SELECT id,title,path,page_count,position,read_pages,reading_direction,cover_url,updated_at FROM manga_books ORDER BY updated_at DESC",
            )
        manga = getattr(self.service.api, "manga", None) if getattr(self.service, "api", None) else None
        items = []
        for row in rows:
            book_id = int(row["id"])
            ocr = 0
            if manga is not None:
                try:
                    ocr = int(manga.ocr_cache_status(book_id).get("cached_pages") or 0)
                except Exception:  # noqa: BLE001
                    ocr = 0
            title = str(row["title"] or "")
            if manga is not None and hasattr(manga, "shelf_metadata"):
                # Same shelves as the Mac library, hand-made corrections included.
                shelf = manga.shelf_metadata(book_id, title, str(row.get("path") or ""))
            else:
                from .manga import _manga_series_key, _manga_series_title, _manga_volume

                series = _manga_series_title(title) or title
                shelf = {
                    "series_key": _manga_series_key(series) or f"book:{book_id}",
                    "series_title": series,
                    "volume": _manga_volume(title) or 1,
                }
            items.append(
                {
                    "id": book_id,
                    "title": title,
                    "pages": int(row["page_count"] or 0),
                    "position": int(row["position"] or 0),
                    "readPages": int(row["read_pages"] or 0),
                    "rightToLeft": str(row["reading_direction"] or "rtl") != "ltr",
                    "ocrPages": ocr,
                    "updatedAt": float(row["updated_at"] or 0),
                    "seriesKey": str(shelf["series_key"]),
                    "seriesTitle": str(shelf["series_title"]),
                    "volume": int(shelf["volume"] or 1),
                }
            )
        return {"items": items}

    def manga_book(self, book_id: int) -> dict[str, Any]:
        for item in self.manga_list()["items"]:
            if item["id"] == book_id:
                return item
        raise MediaError("This manga is no longer on the Mac")

    def manga_page(self, book_id: int, page: int) -> tuple[bytes, str]:
        manga = self.api.manga
        row = manga._book(book_id)
        path = Path(str(row["path"]))
        pages = manga._pages(path)
        if not pages:
            raise MediaError("This manga has no pages")
        name = pages[max(0, min(page, len(pages) - 1))]
        with zipfile.ZipFile(path) as archive:
            data = archive.read(name)
        suffix = Path(name).suffix.casefold()
        kind = {".png": "image/png", ".webp": "image/webp", ".gif": "image/gif", ".avif": "image/avif"}.get(
            suffix, "image/jpeg"
        )
        if kind in {"image/avif"}:
            data, kind = self._as_jpeg(data), "image/jpeg"
        return data, kind

    @staticmethod
    def _as_jpeg(data: bytes) -> bytes:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            output = io.BytesIO()
            image.convert("RGB").save(output, "JPEG", quality=88)
            return output.getvalue()

    def manga_text(self, book_id: int, page: int) -> dict[str, Any]:
        payload = self.api.manga.text_regions(book_id, page, cached_only=True)
        regions = []
        for region in payload.get("regions") or []:
            if not isinstance(region, dict):
                continue
            text = str(region.get("text") or "").strip()
            if not text:
                continue
            x = max(0.0, min(1.0, float(region.get("x") or 0)))
            y = max(0.0, min(1.0, float(region.get("y") or 0)))
            width = max(0.0, min(1.0 - x, float(region.get("width") or 0)))
            height = max(0.0, min(1.0 - y, float(region.get("height") or 0)))
            # Vision coordinates have a bottom-left origin; the phone uses top-left.
            regions.append(
                {
                    "x": x,
                    "y": max(0.0, 1.0 - y - height),
                    "width": width,
                    "height": height,
                    "text": text,
                    "vertical": str(region.get("orientation") or "") == "vertical",
                }
            )
        return {
            "page": int(payload.get("page_index") or page),
            "status": str(payload.get("status") or ""),
            "regions": regions,
        }

    def manga_position(self, book_id: int, value: dict[str, Any]) -> dict[str, Any]:
        page = max(0, int(value.get("page") or 0))
        if self._stale_position(
            "SELECT position,updated_at FROM manga_books WHERE id=?",
            book_id,
            self._observed(value),
            lambda row: page <= int(row[0] or 0),
        ):
            return {"ok": True, "kept": "newer"}
        self.api.manga_set_position(book_id, page)
        if value.get("read") is True:
            self.api.manga_mark_read(book_id, page)
        return {"ok": True}

    # ----------------------------------------------------------------- episodes
    def episodes(self) -> dict[str, Any]:
        return {"items": [item for group in self.anime()["items"] for item in group["episodes"]]}

    def anime(self) -> dict[str, Any]:
        """Ready episodes grouped by anime, as the Mac's Anime page offers them.

        Unwatched episodes ahead of the AniList progress; with a personal weekly
        schedule only the unlocked, unwatched releases (caught-up titles keep
        their card with the next unlock time).
        """
        with self.service.db.connect() as conn:
            rows = self._rows(
                conn,
                """
                SELECT e.id,e.media_id,e.episode,COALESCE(e.media_episode,e.episode) AS number,e.media_episode_end AS number_end,e.part_boundary,e.title,e.state,e.video_path,
                       e.subtitle_path,e.embedded_subtitle_id,e.playback_position,e.playback_duration,e.updated_at,
                       a.title AS anime_title,a.status AS anime_status,a.progress AS anime_progress,a.episodes AS anime_episodes
                FROM episodes e LEFT JOIN anime a ON a.media_id=e.media_id
                WHERE e.state='ready' AND e.watched_at IS NULL AND e.delete_after IS NULL
                  AND (e.subtitle_path IS NOT NULL OR e.embedded_subtitle_id IS NOT NULL)
                ORDER BY a.title COLLATE NOCASE, number""",
            )
        groups: dict[Any, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(
                row["media_id"] if row["media_id"] is not None else f"video:{row['id']}", []
            ).append(row)
        api = getattr(self.service, "api", None)
        library: dict[int, Any] = {}
        if api is not None and getattr(api, "manager", None) is not None:
            try:
                library = {int(item.media_id): item for item in api.manager.db.anime_list()}
            except Exception:  # noqa: BLE001 - grouping still works without schedules
                library = {}
        scheduled_ids: set[int] = set()
        result = []
        for key, members in groups.items():
            first = members[0]
            media_id = int(first["media_id"] or 0)
            schedule = self._schedule(api, library.get(media_id)) if media_id else None
            if schedule is not None:
                scheduled_ids.add(media_id)
                allowed = schedule.pop("paths")
                members = [row for row in members if str(row["video_path"]) in allowed]
            else:
                if str(first.get("anime_status") or "") in {"COMPLETED", "DROPPED"}:
                    continue
                if first.get("anime_status") is not None:
                    members = [
                        row
                        for row in members
                        if int(row.get("number_end") or row["number"] or 0)
                        > int(first.get("anime_progress") or 0)
                    ]
            episodes = [self._episode_item(row) for row in members if Path(str(row["video_path"])).is_file()]
            if not episodes and schedule is None:
                continue
            result.append(
                {
                    "mediaId": media_id,
                    "title": str(first["anime_title"] or first["title"] or ""),
                    "progress": int(first.get("anime_progress") or 0),
                    "total": int(first["anime_episodes"]) if first.get("anime_episodes") else None,
                    "schedule": schedule,
                    "episodes": episodes,
                }
            )
        # Scheduled titles whose next release is not unlocked yet have no ready rows.
        for media_id, anime in library.items():
            if media_id in scheduled_ids or media_id in groups:
                continue
            schedule = self._schedule(api, anime, quick=True)
            if schedule is not None and schedule.get("status") == "caught_up":
                schedule.pop("paths", None)
                result.append(
                    {
                        "mediaId": media_id,
                        "title": str(getattr(anime, "title", "") or ""),
                        "progress": int(getattr(anime, "progress", 0) or 0),
                        "total": int(anime.episodes) if getattr(anime, "episodes", None) else None,
                        "schedule": schedule,
                        "episodes": [],
                    }
                )
        order = self._home_order(api)
        result.sort(
            key=lambda group: (
                *order.get(int(group["mediaId"] or 0), (len(_HOME_ORDER), 0)),
                group["title"].casefold(),
            )
        )
        return {"items": result}

    @staticmethod
    def _home_order(api: Any) -> dict[int, tuple[int, int]]:
        """media id -> (section, position) on the Mac Home page; usually its cached snapshot."""
        try:
            home = (api.get_state_fast() or {}).get("home") or {}
        except Exception:  # noqa: BLE001 - without it the list stays alphabetical
            return {}
        order: dict[int, tuple[int, int]] = {}
        for rank, name in enumerate(_HOME_ORDER):
            position = 0
            for card in home.get(name) or []:
                if not isinstance(card, dict):
                    continue
                members = card.get("items") if card.get("kind") == "watch_sequence" else None
                for item in members if isinstance(members, list) else [card]:
                    try:
                        media_id = int((item or {}).get("media_id") or 0)
                    except (TypeError, ValueError, AttributeError):
                        continue
                    if media_id and media_id not in order:
                        order[media_id] = (rank, position)
                        position += 1
        return order

    @staticmethod
    def _schedule(api: Any, anime: Any, *, quick: bool = False) -> dict[str, Any] | None:
        """Personal weekly schedule of a title (None when it has none)."""
        if api is None or anime is None or not callable(getattr(api, "_personal_schedule_snapshot", None)):
            return None
        try:
            if quick and not api.manager.db.personal_release_schedule(int(anime.media_id)):
                return None
            snapshot = api._personal_schedule_snapshot(anime)
        except Exception:  # noqa: BLE001 - schedules are optional
            return None
        if not snapshot.get("enabled") or snapshot.get("status") == "completed":
            return None
        paths: set[str] = set()
        for number in snapshot.get("ready_episodes") or []:
            try:
                local = api._local_episode_for_relative(anime, int(number))
            except Exception:  # noqa: BLE001
                local = None
            if local is not None:
                paths.add(str(local.video_path))
        return {
            "status": str(snapshot.get("status") or ""),
            "nextEpisode": int(snapshot["next_episode"])
            if snapshot.get("next_episode") is not None
            else None,
            "nextUnlockAt": float(snapshot["next_unlock_at"]) if snapshot.get("next_unlock_at") else None,
            "paths": paths,
        }

    def _episode_item(self, row: dict[str, Any]) -> dict[str, Any]:
        episode_id = int(row["id"])
        job = self.video_jobs.get(episode_id) or {}
        item_end = (
            {"numberEnd": int(row["number_end"]), "partBoundary": float(row.get("part_boundary") or 0)}
            if row.get("number_end")
            else {}
        )
        return {
            **item_end,
            "id": episode_id,
            "mediaId": int(row["media_id"] or 0),
            "number": int(row["number"] or 0),
            "title": str(row["anime_title"] or row["title"] or ""),
            "episodeTitle": str(row["title"] or ""),
            "watched": str(row["state"]) == "watched",
            "position": float(row["playback_position"] or 0),
            "duration": float(row["playback_duration"] or 0),
            "prepared": self._prepared_video(episode_id) is not None,
            "preparing": job.get("state") == "preparing",
            "subtitleRevision": self._subtitle_revision(row),
        }

    @staticmethod
    def _subtitle_revision(row: dict[str, Any]) -> str:
        """Changes when the Mac's subtitle for the episode changes (the phone re-fetches cues)."""
        path = Path(str(row["subtitle_path"])) if row.get("subtitle_path") else None
        try:
            stamp = (
                f"{path}:{path.stat().st_mtime_ns}"
                if path is not None
                else f"sid:{row.get('embedded_subtitle_id')}"
            )
        except OSError:
            stamp = str(path)
        return hashlib.sha256(f"{stamp}:cues-v2".encode()).hexdigest()[:16]

    def _episode_row(self, episode_id: int) -> dict[str, Any]:
        with self.service.db.connect() as conn:
            rows = self._rows(
                conn,
                "SELECT e.*,a.title AS anime_title FROM episodes e LEFT JOIN anime a ON a.media_id=e.media_id WHERE e.id=?",
                (episode_id,),
            )
        if not rows or not Path(str(rows[0]["video_path"])).is_file():
            raise MediaError("This episode is no longer on the Mac")
        return rows[0]

    def _log(self, text: str, *args: Any) -> None:
        logger = getattr(getattr(self.service, "api", None), "logger", None)
        if logger is not None:
            logger.info(text, *args)

    def _touch_video(self, path: str | Path) -> None:
        with self._video_lock:
            self._video_used[str(path)] = time.time()

    def video_streaming(self, path: str | Path, active: bool) -> None:
        """A phone started (or ended) downloading this file: it must not be pruned meanwhile."""
        key = str(path)
        with self._video_lock:
            count = self._video_streams.get(key, 0) + (1 if active else -1)
            if count > 0:
                self._video_streams[key] = count
            else:
                self._video_streams.pop(key, None)
            self._video_used[key] = time.time()

    def _prepared_video(self, episode_id: int, *, touch: bool = False) -> dict[str, Any] | None:
        try:
            row = self._episode_row(episode_id)
        except MediaError:
            return None
        source = Path(str(row["video_path"]))
        stat = source.stat()
        with self.service.db.connect() as conn:
            cached = conn.execute(
                "SELECT size,mtime_ns,result FROM iphone_prepared WHERE source=?", (f"video:{episode_id}",)
            ).fetchone()
        if cached is None or cached["size"] != stat.st_size or cached["mtime_ns"] != stat.st_mtime_ns:
            return None
        result = json.loads(cached["result"])
        target = Path(result["path"])
        if not target.is_file() or target.stat().st_size != result["sizeBytes"]:
            return None
        if touch:
            self._touch_video(target)
        return result

    def episode(self, episode_id: int) -> dict[str, Any]:
        row = self._episode_row(episode_id)
        # A phone polls this while it waits to download the episode: keep its copy.
        prepared = self._prepared_video(episode_id, touch=True)
        job = dict(self.video_jobs.get(episode_id) or {})
        value: dict[str, Any] = {
            "id": episode_id,
            "title": str(row["anime_title"] or row["title"] or ""),
            "number": int(row["media_episode"] or row["episode"] or 0),
            "state": "ready" if prepared else str(job.get("state") or "idle"),
            "progress": float(job.get("progress") or 0),
            "message": str(job.get("message") or ""),
        }
        if prepared:
            value["video"] = {k: prepared[k] for k in ("resourceId", "sha256", "sizeBytes", "fileName")}
            value["durationMs"] = int(prepared.get("durationMs") or 0)
        return value

    def prepare_episode(self, episode_id: int) -> dict[str, Any]:
        self._episode_row(episode_id)
        with self.lock:
            job = self.video_jobs.get(episode_id)
            # Missing (pruned, or the Mac restarted mid-way): prepared again on demand.
            if self._prepared_video(episode_id, touch=True) is None and not (
                job and job.get("state") == "preparing"
            ):
                self.video_jobs[episode_id] = {"state": "preparing", "progress": 0.0, "message": ""}
                threading.Thread(
                    target=self._prepare_worker,
                    args=(episode_id,),
                    name=f"iphone-video-{episode_id}",
                    daemon=True,
                ).start()
        return self.episode(episode_id)

    def _tools(self) -> tuple[str, str]:
        tools = (
            getattr(getattr(self.service.api, "config", None), "tools", None)
            if getattr(self.service, "api", None)
            else None
        )
        return str(getattr(tools, "ffmpeg", None) or "ffmpeg"), str(
            getattr(tools, "ffprobe", None) or "ffprobe"
        )

    def _prepare_worker(self, episode_id: int) -> None:
        started = time.monotonic()
        try:
            row = self._episode_row(episode_id)
            source = Path(str(row["video_path"]))
            stat = source.stat()
            self._log(
                "EVENT iphone.video_prepare start episode=%s source_size=%s video=%r",
                episode_id,
                stat.st_size,
                source.name,
            )
            ffmpeg, ffprobe = self._tools()
            result = prepare_video(
                source,
                self.service.root / "video",
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                progress=lambda value: self.video_jobs.get(episode_id, {}).update(progress=value),
            )
            self._touch_video(result["path"])
            sha = self.service.file_sha256(result["path"])
            resource = str(uuid.uuid5(uuid.UUID(self.service.server_id), "video:" + sha))
            stored = {
                "resourceId": resource,
                "sha256": sha,
                "sizeBytes": result["path"].stat().st_size,
                "fileName": "episode.mp4",
                "durationMs": result["durationMs"],
                "path": str(result["path"]),
            }
            with self.service.db.connect() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO iphone_resources(id,sha,size,path) VALUES(?,?,?,?)",
                    (resource, sha, stored["sizeBytes"], stored["path"]),
                )
                conn.execute(
                    "INSERT INTO iphone_prepared(source,size,mtime_ns,result) VALUES(?,?,?,?) "
                    "ON CONFLICT(source) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,result=excluded.result",
                    (f"video:{episode_id}", stat.st_size, stat.st_mtime_ns, json.dumps(stored)),
                )
            try:
                # Tokenized before "ready": the phone's subtitle request right after it is then
                # answered from the cache instead of parsing again (and timing out on a busy Mac).
                self.episode_subtitles(episode_id)
            except Exception:  # noqa: BLE001 - subtitles are fetched again by the phone
                pass
            self.video_jobs[episode_id] = {"state": "ready", "progress": 1.0, "message": ""}
            self._log(
                "EVENT iphone.video_prepare done episode=%s size=%s seconds=%.1f",
                episode_id,
                stored["sizeBytes"],
                time.monotonic() - started,
            )
            self._prune_videos()
        except Exception as exc:  # noqa: BLE001 - reported to the phone
            self.video_jobs[episode_id] = {
                "state": "failed",
                "progress": 0.0,
                "message": str(exc)[-300:] or "Preparation failed",
            }
            self._log(
                "EVENT iphone.video_prepare failed episode=%s seconds=%.1f error=%r",
                episode_id,
                time.monotonic() - started,
                str(exc)[-300:],
            )

    def _prune_videos(self) -> None:
        """Keep the prepared copies within the size budget, least recently used first.

        A copy a phone asked for recently or is downloading now is never removed (a phone
        queue of a whole season prepares one episode after the other); a removed copy is
        prepared again when a phone asks for it.
        """
        folder = self.service.root / "video"
        now = time.time()
        files: list[tuple[float, int, Path]] = []
        for path in folder.glob("*.mp4"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if path.name.endswith(".part.mp4"):
                # A preparation still being written; one a day old was left by an interrupted run.
                if now - stat.st_mtime > 86400:
                    path.unlink(missing_ok=True)
                continue
            with self._video_lock:
                used = max(stat.st_mtime, self._video_used.get(str(path), 0.0))
            files.append((used, stat.st_size, path))
        files.sort(key=lambda item: item[0], reverse=True)
        total = 0
        for used, size, path in files:
            total += size
            if total <= _VIDEO_BUDGET_BYTES:
                continue
            with self._video_lock:
                pinned = str(path) in self._video_streams or now - used < _VIDEO_PIN_SECONDS
            if pinned:
                continue
            path.unlink(missing_ok=True)
            total -= size
            self._log("EVENT iphone.video_prune file=%s size=%s idle_s=%.0f", path.name, size, now - used)

    def _subtitle_file(self, row: dict[str, Any]) -> Path | None:
        from .subtitle_formats import clean_srt_for_playback, convert_to_plain_srt

        api = self.api
        cache = Path(
            str(getattr(getattr(api.config, "paths", None), "cache_dir", self.service.root / "cache"))
        ).expanduser()
        ffmpeg, _ffprobe = self._tools()
        from .subtitle_runtime import resolve_episode_subtitle

        ffprobe = self._tools()[1]
        video = Path(str(row["video_path"]))
        # The same Japanese track the Mac plays: embedded_subtitle_id is mpv's sid,
        # resolved to the real stream index (never "0:<sid>").
        selection = resolve_episode_subtitle(
            self.service.db,
            video_path=video,
            media_id=int(row.get("media_id") or 0) or None,
            episode=int(row.get("episode") or 0) or None,
            stored_path=row.get("subtitle_path"),
            stored_embedded_id=row.get("embedded_subtitle_id"),
            stored_origin=str(row.get("subtitle_origin") or ""),
            ffprobe=ffprobe,
            ffmpeg=ffmpeg,
            allow_bitmap=False,
        )
        if not selection.found or not selection.is_text:
            return None
        path = selection.external_path
        if path is None and selection.embedded_stream_index is not None:
            stat = video.stat()
            target = (
                self.service.root
                / "video"
                / (
                    f"embedded-{int(row['id'])}-s{int(selection.embedded_stream_index)}-"
                    f"{stat.st_size}-{stat.st_mtime_ns}.srt"
                )
            )
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.stem + ".part.srt")
                subprocess.run(
                    [
                        ffmpeg,
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-i",
                        str(video),
                        "-map",
                        f"0:{int(selection.embedded_stream_index)}",
                        "-f",
                        "srt",
                        str(temporary),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=300,
                )
                os.replace(temporary, target)
            path = target
        if path is None or not path.is_file():
            return None
        if path.suffix.casefold() in {".ass", ".ssa"}:
            path, _ = convert_to_plain_srt(path, cache, ffmpeg_path=ffmpeg)
        if path.suffix.casefold() != ".srt":
            return None
        cleaned, _ = clean_srt_for_playback(path, cache)
        return cleaned

    def episode_subtitles(self, episode_id: int) -> dict[str, Any]:
        from .subtitle_formats import parse_srt

        row = self._episode_row(episode_id)
        path = self._subtitle_file(row)
        if path is None:
            raise MediaError("This episode has no Japanese subtitles")
        stat = path.stat()
        key = hashlib.sha256(f"{path}:{stat.st_size}:{stat.st_mtime_ns}:cues-v2".encode()).hexdigest()[:24]
        cache = self.service.root / "video" / f"cues-{key}.json"
        if cache.is_file():
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("tokenized"):
                self.live_word_states(cached["cues"])  # the states of the parse time are old
                return cached
        cues = [
            {"start": round(start * 1000), "end": round(end * 1000), "text": text.strip(), "tokens": []}
            for start, end, text in parse_srt(path)
            if text.strip()
        ]
        tokenized = self._tokenize_cues(cues)
        value = {"schema": 1, "tokenized": tokenized, "cues": cues}
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache.with_name(cache.name + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, cache)
        return value

    def _tokenize_cues(self, cues: list[dict[str, Any]]) -> bool:
        """Jiten tokens for each cue."""
        views = self.parse_lines([cue["text"] for cue in cues])
        if views is None:
            return False
        for cue, view in zip(cues, views):
            cue["tokens"], cue["ruby"] = view["tokens"], view["ruby"]
        return True

    def parse_lines(self, texts: list[str]) -> list[dict[str, Any]] | None:
        """Reader paragraphs (tokens, furigana) of many short texts in a few Jiten parses.

        A line break inside a text is parsed as a space (same length). None when Jiten
        could not parse them (offline): the texts stay plain.
        """
        light_novels = (
            getattr(self.service.api, "light_novels", None) if getattr(self.service, "api", None) else None
        )
        if light_novels is None or not texts:
            return None
        batches: list[list[str]] = [[]]
        size = 0
        for text in texts:
            length = len(text) + 1
            if batches[-1] and size + length > _SUBTITLE_BATCH_CHARS:
                batches.append([])
                size = 0
            batches[-1].append(text)
            size += length
        views: list[dict[str, Any]] = []
        try:
            settings = self._reader_settings()
            for batch in batches:
                parsed = light_novels.parse_study_text("\n".join(text.replace("\n", " ") for text in batch))
                groups = parsed.get("tokens") or []
                if len(groups) != len(batch):
                    return None
                for text, group in zip(batch, groups):
                    views.append(paragraph_view(text, group, parsed.get("vocabulary") or [], settings))
        except Exception:  # noqa: BLE001 - Jiten offline: plain text still shows
            return None
        self.live_word_states(views)
        return views

    def episode_watched(self, episode_id: int, value: dict[str, Any]) -> dict[str, Any]:
        """An episode watched on the iPhone counts like one watched in mpv.

        The phone reports the seconds it actually played (seeks excluded); the
        Mac applies the same rule as its mpv tracker (65% of the episode, at
        least 3 minutes), then updates AniList, the personal schedule and the
        cleanup queue.
        """
        row = self._episode_row(episode_id)
        api = self.api
        video = Path(str(row["video_path"]))
        duration = max(0.0, float(value.get("duration") or row.get("playback_duration") or 0))
        active = max(0.0, float(value.get("activeSeconds") or 0))
        required = max(180.0, duration * 0.65) if duration > 0 else 300.0
        first_episode = int(row.get("media_episode") or row.get("episode") or 0)
        last_episode = int(row.get("media_episode_end") or 0) or first_episode
        if last_episode > first_episode and duration > 0 and row.get("media_id"):
            # Two episodes in one file (audit 17А): when the first one is
            # already counted, the second needs 65 % of its own half.
            with self.service.db.connect() as conn:
                progress_rows = self._rows(
                    conn, "SELECT progress FROM anime WHERE media_id=?", (int(row["media_id"]),)
                )
            boundary = float(row.get("part_boundary") or 0) or duration / 2
            if (
                progress_rows
                and int(progress_rows[0].get("progress") or 0) >= first_episode
                and 0 < boundary < duration
            ):
                required = max(180.0, (duration - boundary) * 0.65)
        if active + 0.5 < required:
            return {
                "ok": False,
                "outcome": "not_enough",
                "message": f"Watched {active / 60:.1f} of the required {required / 60:.1f} min",
            }
        db = api.manager.db
        config = api.config
        db.record_playback(video, duration, duration, active)
        media_id = int(row.get("media_id") or 0)
        episode = last_episode
        result: dict[str, Any] = {"ok": True, "outcome": "counted", "message": ""}
        list_status = None
        if media_id and episode and getattr(config.anilist, "access_token", ""):
            from .providers.anilist import AniListClient

            total = None
            with self.service.db.connect() as conn:
                anime = self._rows(conn, "SELECT episodes FROM anime WHERE media_id=?", (media_id,))
            if anime and anime[0].get("episodes"):
                total = int(anime[0]["episodes"])
            client = AniListClient(config.anilist.endpoint, access_token=config.anilist.access_token)
            try:
                update = client.update_progress(
                    media_id,
                    episode,
                    total,
                    add_if_missing=config.anilist.add_if_missing,
                    update_when_rewatching=config.anilist.update_when_rewatching,
                    completed_to_rewatching_on_episode_one=config.anilist.completed_to_rewatching_on_episode_one,
                    complete_current_final=config.anilist.complete_current_final,
                    complete_rewatching_final=config.anilist.complete_rewatching_final,
                )
            finally:
                client.close()
            reason = str(update.get("reason") or "")
            list_status = str(update.get("status") or "") or None
            result["anilist"] = "updated" if update.get("updated") else reason
            if not (update.get("updated") or reason == "already_at_or_above"):
                result.update(
                    ok=False,
                    outcome="anilist_" + (reason or "unchanged"),
                    message="AniList did not count the episode" + (f" ({reason})" if reason else ""),
                )
                return result
            with self.service.db.connect() as conn:
                conn.execute(
                    "UPDATE anime SET progress=MAX(progress,?),updated_at=? WHERE media_id=?",
                    (int(update.get("progress") or episode), time.time(), media_id),
                )
        db.schedule_cleanup(
            video,
            config.agent.delete_after_watched_hours,
            list_status=list_status,
            media_id=media_id or None,
            episode=episode or None,
        )
        if media_id and episode:
            try:
                for number in range(first_episode or episode, episode + 1):
                    if db.mark_personal_release_watched(media_id, number, watched_at=time.time()):
                        result["schedule"] = "watched"
            except Exception:  # noqa: BLE001 - schedules are optional
                pass
        logger = getattr(api, "logger", None)
        if logger is not None:
            logger.info(
                "EVENT iphone.episode_watched media_id=%s episode=%s active=%.0f duration=%.0f",
                media_id,
                episode,
                active,
                duration,
            )
        return result

    def episode_position(self, episode_id: int, value: dict[str, Any]) -> dict[str, Any]:
        seconds = max(0.0, float(value.get("seconds") or 0))
        duration = max(0.0, float(value.get("duration") or 0))
        if self._stale_position(
            "SELECT playback_position,playback_updated_at FROM episodes WHERE id=?",
            episode_id,
            self._observed(value),
            lambda row: seconds <= float(row[0] or 0),
        ):
            return {"ok": True, "kept": "newer"}
        with self.service.db.connect() as conn:
            conn.execute(
                "UPDATE episodes SET playback_position=?,playback_duration=CASE WHEN ?>0 THEN ? ELSE playback_duration END,"
                "playback_updated_at=? WHERE id=?",
                (seconds, duration, duration, time.time(), episode_id),
            )
        return {"ok": True}

    # ------------------------------------------------------------------- covers
    def cover(self, kind: str, item: int) -> tuple[bytes, str]:
        url = ""
        with self.service.db.connect() as conn:
            if kind == "ln":
                rows = self._rows(conn, "SELECT cover_url FROM ln_books WHERE id=?", (item,))
            elif kind == "manga":
                rows = self._rows(conn, "SELECT cover_url FROM manga_books WHERE id=?", (item,))
            elif kind == "anime":
                rows = self._rows(conn, "SELECT cover_url FROM anime WHERE media_id=?", (item,))
            else:
                rows = []
        if rows:
            url = str(rows[0].get("cover_url") or "")
        source: Path | None = None
        if url.startswith(("http://", "https://")):
            source = self.service._remote_cover(url)
        elif url.startswith("covers/"):
            for folder in getattr(self.service, "cover_dirs", []):
                if (Path(folder) / Path(url).name).is_file():
                    source = Path(folder) / Path(url).name
        if source is None and kind == "manga":
            data, _kind = self.manga_page(item, 0)
            return self._thumbnail(data), "image/jpeg"
        if source is None:
            raise MediaError("No cover")
        return self._thumbnail(source.read_bytes()), "image/jpeg"

    @staticmethod
    def _thumbnail(data: bytes, width: int = 360) -> bytes:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            if image.width > width:
                image = image.resize((width, max(1, round(image.height * width / image.width))))
            output = io.BytesIO()
            image.save(output, "JPEG", quality=82)
            return output.getvalue()

    # -------------------------------------------------------------------- study
    def study_options(self) -> dict[str, Any]:
        from .review_actions import actions_for

        api = self.api
        settings = api.light_novels.settings()
        backend = str(settings.study_backend or "jiten").casefold()
        decks: list[dict[str, Any]] = []
        message = ""
        try:
            decks = [
                {"id": str(d.get("id")), "name": str(d.get("name") or d.get("id"))}
                for d in api.study_decks(backend)
                if d.get("id") is not None
            ]
        except Exception as exc:  # noqa: BLE001 - grading without a deck still works
            message = str(exc)
        grades = [
            {"id": a.id, "label": a.label_en, "tone": a.tone}
            for a in actions_for(backend, settings.review_mode)
        ]
        return {
            "backend": backend,
            "decks": decks,
            "deckId": str(api.study_deck_preference(backend).get("deck_id") or ""),
            "grades": grades,
            "message": message,
        }

    def study_deck(self, value: dict[str, Any]) -> dict[str, Any]:
        api = self.api
        backend = str(api.light_novels.settings().study_backend or "jiten")
        return api.set_study_deck_preference(backend, str(value.get("deckId") or ""))

    def study_word(self, value: dict[str, Any]) -> dict[str, Any]:
        api = self.api
        word, reading = int(value.get("wordId") or 0), int(value.get("readingIndex") or 0)
        if word <= 0:
            raise MediaError("No dictionary entry for this word")
        detail = dict(api.study_word({"wordId": word, "readingIndex": reading}) or {})
        try:
            live = api.study_state({"word_id": word, "reading_index": reading}) or {}
        except Exception:  # noqa: BLE001 - state is optional
            live = {}
        return self._word_view(word, reading, detail, live, str(value.get("surface") or ""))

    def study_words(self, value: dict[str, Any]) -> dict[str, Any]:
        """Dictionary entries of many words, kept by the iPhone for its offline word card.

        Answered from the Mac's word cache at once; some words not cached yet are fetched in
        the background and come on the phone's next request. Only complete entries are sent.
        """
        pairs = _pairs(value.get("pairs"), _WORDS_PER_REQUEST)
        if not pairs:
            return {"items": []}
        cards = self.api.study_words([{"wordId": word, "readingIndex": reading} for word, reading in pairs])
        items = []
        for (word, reading), card in zip(pairs, cards or []):
            detail = dict(card or {})
            if word > 0 and detail.get("dictionary_complete"):
                items.append(self._word_view(word, reading, detail, {}, ""))
        return {"items": items}

    @staticmethod
    def _word_view(
        word: int, reading: int, detail: dict[str, Any], live: dict[str, Any], surface: str
    ) -> dict[str, Any]:
        meanings: list[str] = []
        raw = detail.get("meanings") or detail.get("meaningsChunks") or detail.get("definitions") or []
        for item in raw if isinstance(raw, list) else []:
            for entry in item if isinstance(item, list) else [item]:
                if isinstance(entry, str):
                    meanings.append(entry)
                elif isinstance(entry, dict):
                    text = entry.get("meanings") or entry.get("text") or entry.get("gloss")
                    meanings.append(", ".join(map(str, text)) if isinstance(text, list) else str(text or ""))
        readings: list[str] = []
        rows = ([detail["mainReading"]] if isinstance(detail.get("mainReading"), dict) else []) + [
            r
            for r in (detail.get("readings") or []) + (detail.get("alternativeReadings") or [])
            if isinstance(r, (dict, str))
        ]
        for row in rows:
            text = row if isinstance(row, str) else str(row.get("reading") or row.get("text") or "")
            if text and text not in readings:
                readings.append(text)
        spelling = str(detail.get("spelling") or detail.get("wordTextPlain") or surface or "")
        card = {**detail, **{k: live[k] for k in ("states", "normalizedState") if k in live}}
        decks = (
            live.get("studyDeckIds")
            if isinstance(live.get("studyDeckIds"), list)
            else detail.get("studyDeckIds")
        )
        return {
            "wordId": word,
            "readingIndex": reading,
            "spelling": spelling,
            "reading": re.sub(
                r"\[[^\]]*\]", "", str(detail.get("reading") or (readings[0] if readings else ""))
            ),
            "readings": readings[:6],
            "meanings": [m for m in meanings if m][:12],
            "state": state_code(card),
            "stateLabel": str(live.get("rawStateLabel") or detail.get("rawStateLabel") or ""),
            "frequencyRank": int(detail.get("frequencyRank") or 0) or None,
            "inDeck": bool(decks) if isinstance(decks, list) else None,
            "complete": bool(detail.get("dictionary_complete")),
        }

    def study_states(self, value: dict[str, Any]) -> dict[str, Any]:
        """Live Jiten states for words already on screen: rows [wordId, readingIndex, state, flags]."""
        pairs = _pairs(value.get("pairs"), 500)
        if not pairs:
            return {"rows": []}
        result = self.api.study_states({"pairs": [list(pair) for pair in pairs]}) or {}
        if not result.get("ok", True):
            raise MediaError(str(result.get("message") or "Jiten states are unavailable"))
        furigana, marked = _state_sets(self._reader_settings())
        rows = []
        for item in result.get("states") or []:
            if not isinstance(item, dict):
                continue
            try:
                pair = [int(item.get("wordId")), int(item.get("readingIndex"))]
            except (TypeError, ValueError):
                continue
            rows.append([*pair, state_code(item), token_flags(item, furigana, marked)])
        return {"rows": rows}

    def study_parse(self, value: dict[str, Any]) -> dict[str, Any]:
        text = str(value.get("text") or "").strip()[:4000]
        if not text:
            raise MediaError("No text to look up")
        parsed = self.api.study_parse_text(text)
        groups = parsed.get("tokens") or []
        settings = self._reader_settings()
        rows = [
            paragraph_view(
                str(paragraph),
                groups[index] if index < len(groups) else [],
                parsed.get("vocabulary") or [],
                settings,
            )
            for index, paragraph in enumerate(parsed.get("paragraphs") or [])
        ]
        self.live_word_states(rows)
        return {"paragraphs": rows}

    def study_action(self, value: dict[str, Any]) -> dict[str, Any]:
        attempt = str(value.get("attemptId") or "")
        if not 8 <= len(attempt) <= 64:
            raise MediaError("Invalid attempt")
        action = str(value.get("action") or "")
        if action not in {"add", "review"}:
            raise MediaError("Unknown study action")
        api = self.api
        payload = {
            "backend": str(api.light_novels.settings().study_backend or "jiten"),
            "action": action,
            "word_id": int(value.get("wordId") or 0),
            "reading_index": int(value.get("readingIndex") or 0),
            "deck_id": str(value.get("deckId") or ""),
            "sentence": str(value.get("sentence") or "")[:1000],
            "attempt_id": attempt,
            "id_namespace": "jiten",
        }
        if action == "review":
            payload["grade"] = str(value.get("grade") or "")
        try:
            result = api.study_action(payload) or {}
        except Exception as exc:  # noqa: BLE001 - shown on the phone, never retried silently
            return {"ok": False, "outcome": "failed", "message": str(exc)}
        if result.get("outcome") == "unknown":
            return {
                "ok": False,
                "outcome": "unknown",
                "message": str(result.get("message") or "The outcome is unknown; check on the Mac"),
            }
        if result.get("ok") is False:
            return {
                "ok": False,
                "outcome": "failed",
                "message": str(result.get("message") or "The Mac could not save it"),
            }
        return {"ok": True, "outcome": "saved", "message": ""}

    def reader_settings(self) -> dict[str, Any]:
        """The Mac reader's look: word colours, mark style, furigana and text colours."""
        settings = self._reader_settings()
        get = lambda name, default: getattr(settings, name, default) if settings is not None else default  # noqa: E731
        return {
            "markStyle": "color"
            if str(get("word_mark_style", "underline")) in {"color", "none"}
            else "underline",
            "showFurigana": bool(get("show_furigana", True)),
            "font": str(get("reader_font", "mincho") or "mincho"),
            "lineHeight": float(get("reader_line_height", 1.9) or 1.9),
            "textColor": str(get("reader_text_color", "#c9c7c2") or "#c9c7c2"),
            "backgroundColor": str(get("reader_background_color", "#000000") or "#000000"),
            "blurImages": bool(get("blur_images", False)),
            # Always "#rrggbb". "learning"/"known" for older iPhone apps.
            "colors": {
                **{
                    state: word_color(get(f"word_color_{state}", None), DEFAULT_WORD_COLORS[state])
                    for state in WORD_STATES
                },
                "learning": word_color(get("word_color_young", None), DEFAULT_WORD_COLORS["young"]),
                "known": word_color(get("word_color_mastered", None), DEFAULT_WORD_COLORS["mastered"]),
            },
        }

    # ------------------------------------------------------------- review gate
    @staticmethod
    def _kana(text: str) -> str:
        def replace(match: re.Match) -> str:
            base, reading = match.group(1), match.group(2)
            prefix = re.match(r"^[^\u3400-\u9fff々〆ヵヶ]*", base).group(0)
            return prefix + reading

        return re.sub(r"([^\[\]]+)\[([^\]]+)\]", replace, str(text or ""))

    @classmethod
    def _gate_card(cls, card: dict[str, Any]) -> dict[str, Any]:
        readings = [r for r in card.get("readings") or [] if isinstance(r, dict)]
        wanted = int(card.get("readingIndex") or 0)
        selected = next(
            (r for r in readings if int(r.get("readingIndex", -2)) == wanted), readings[0] if readings else {}
        )
        word = re.sub(
            r"\[[^\]]*\]",
            "",
            str(card.get("wordTextPlain") or card.get("wordText") or selected.get("text") or ""),
        )
        reading = _hiragana(cls._kana(str(selected.get("text") or "")))
        meanings: list[str] = []
        for definition in card.get("definitions") or []:
            for meaning in (definition.get("meanings") if isinstance(definition, dict) else None) or []:
                text = str(meaning or "").strip()
                if text and text not in meanings:
                    meanings.append(text)
        example = card.get("exampleSentence") if isinstance(card.get("exampleSentence"), dict) else {}
        sentence = (
            str(example.get("text") or card.get("pudgeContextSentence") or "").replace("**", "").strip()
        )
        return {
            "wordId": int(card.get("wordId") or 0),
            "readingIndex": wanted,
            "word": word,
            "reading": "" if reading == word else reading,
            "meanings": meanings[:8],
            "sentence": sentence,
            "frequencyRank": int(card.get("frequencyRank") or 0) or None,
        }

    def _gate_view(self, status: dict[str, Any]) -> dict[str, Any]:
        actions = (
            ((status.get("review_actions") or {}).get("actions") or [])
            if isinstance(status.get("review_actions"), dict)
            else []
        )
        return {
            "enabled": bool(status.get("enabled")),
            "blocking": bool(status.get("blocking")),
            "granted": bool(status.get("granted", not status.get("blocking"))),
            "required": int(status.get("required") or 0),
            "completed": int(status.get("completed") or 0),
            "remaining": int(status.get("remaining") or 0),
            "reason": str(status.get("reason") or ""),
            "outcome": str(status.get("outcome") or ""),
            "message": str(status.get("message_en") or status.get("message") or ""),
            "grades": [
                {
                    "id": str(a.get("id")),
                    "label": str(a.get("label_en") or a.get("id")),
                    "tone": str(a.get("tone") or ""),
                }
                for a in actions
                if isinstance(a, dict) and a.get("id")
            ],
            "cards": [
                self._gate_card(c)
                for c in status.get("cards") or []
                if isinstance(c, dict) and c.get("wordId")
            ],
        }

    def gate_begin(self, episode_id: int) -> dict[str, Any]:
        row = self._episode_row(episode_id)
        return self._gate_view(self.api.review_gate_begin(str(row["video_path"])))

    def gate_review(self, episode_id: int, value: dict[str, Any]) -> dict[str, Any]:
        row = self._episode_row(episode_id)
        attempt = str(value.get("attemptId") or "")
        if not 8 <= len(attempt) <= 64:
            raise MediaError("Invalid attempt")
        result = self.api.review_gate_review(
            str(row["video_path"]),
            int(value.get("wordId") or 0),
            int(value.get("readingIndex") or 0),
            str(value.get("grade") or ""),
            attempt,
        )
        return self._gate_view(result)

    # ---------------------------------------------------------- stats/planning
    def stats(self, period: str) -> dict[str, Any]:
        period = period if period in {"7d", "30d", "year", "all"} else "30d"
        data = self.api.consumption_statistics_query({"period": period}, limit=1, offset=0)
        days = [
            {
                "day": str(d["day"]),
                "seconds": float(d.get("seconds") or 0),
                "breakdown": {
                    str(k): float(v)
                    for k, v in (d.get("breakdown") or {}).items()
                    if isinstance(v, (int, float))
                },
            }
            for d in data.get("days") or []
        ]
        works = [
            {
                "title": str(w.get("title") or ""),
                "kind": str(w.get("kind") or ""),
                "seconds": float(w.get("seconds") or 0),
            }
            for w in (data.get("works") or [])[:20]
        ]
        summary = data.get("summary") or {}
        breakdown = {
            str(k): float(v) for k, v in (data.get("breakdown") or {}).items() if isinstance(v, (int, float))
        }
        return {
            "highlights": _phone_highlights(data.get("highlights")),
            "period": period,
            "totalSeconds": float(summary.get("total_seconds") or 0),
            "activeDays": int(summary.get("active_days") or 0),
            "sessions": int(summary.get("sessions") or 0),
            "streak": streak(days),
            "breakdown": breakdown,
            "days": days[-60:],
            "works": works,
        }

    # --------------------------------------------------------------- activity
    def record_activity(
        self, device: str, device_name: str, items: Any, *, sent_at: Any = None
    ) -> dict[str, Any]:
        """Statistics: reading and watching intervals measured on the phone (sent in batches, offline too).

        Each item gets a final answer: "applied", "duplicate" (a retry) or
        "rejected" with a reason (malformed, or the book/episode is no longer on the Mac);
        the phone keeps rejected items with their reason for its diagnostics.

        A phone clock ahead of the Mac's (`sent_at`, the phone's time when sending, or else the
        batch's latest end) moves the whole batch back by that lead, keeping its spacing.
        """
        from .consumption import ConsumptionConflictError, ConsumptionLedgerError

        if not isinstance(items, list) or len(items) > 500:
            raise ValueError("Invalid activity batch")
        ledger = self.api.consumption
        cache: dict[tuple[str, int], Any] = {}
        results: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        now = time.time()
        try:
            phone_now = float(sent_at) if isinstance(sent_at, (int, float)) else None
        except (TypeError, ValueError):
            phone_now = None
        if phone_now is None or not math.isfinite(phone_now):
            ends = []
            for raw in items:
                try:
                    ends.append(float(raw["end"]))
                except (TypeError, ValueError, KeyError):
                    continue
            phone_now = max((end for end in ends if math.isfinite(end)), default=now)
        lead = phone_now - now
        skew = lead if lead > 300.0 else 0.0
        for raw in items:
            item_id = str(raw.get("id") or "")[:64] if isinstance(raw, dict) else ""
            reason = ""
            try:
                status = self._activity_item(ledger, device, device_name, raw, cache, skew)
            except (
                ConsumptionConflictError,
                ConsumptionLedgerError,
                MediaError,
                ValueError,
                TypeError,
                KeyError,
            ) as exc:
                status = "rejected"
                reason = (
                    "No longer on the Mac"
                    if isinstance(exc, MediaError)
                    else "Retried with different data"
                    if isinstance(exc, ConsumptionConflictError)
                    else str(exc).strip()[:200] or "Invalid activity"
                )
            counts[status] = counts.get(status, 0) + 1
            results.append({"id": item_id, "status": status, **({"reason": reason} if reason else {})})
        logger = getattr(self.api, "logger", None)
        if logger is not None and items:
            logger.info(
                "EVENT iphone.activity device=%s applied=%s duplicate=%s rejected=%s clock_skew=%.0f",
                device[:8],
                counts.get("applied", 0),
                counts.get("duplicate", 0),
                counts.get("rejected", 0),
                skew,
            )
        return {"results": results}

    def _activity_item(
        self,
        ledger: Any,
        device: str,
        device_name: str,
        raw: Any,
        cache: dict[tuple[str, int], Any],
        skew: float = 0.0,
    ) -> str:
        if not isinstance(raw, dict):
            raise ValueError("Invalid activity")
        from .iphone_service import identifier

        event = identifier(raw.get("id"))
        kind = str(raw.get("kind") or "")
        item = int(raw.get("item"))
        start, end, active = float(raw["start"]), float(raw["end"]), float(raw["active"])
        first = raw.get("from") if isinstance(raw.get("from"), dict) else {}
        last = raw.get("to") if isinstance(raw.get("to"), dict) else {}
        zone = str(raw.get("timezone") or "")[:64]
        offset = int(raw["offset"]) if isinstance(raw.get("offset"), int) else None

        def media(key: str, load: Callable[[], Any]) -> Any:
            if (key, item) not in cache:
                cache[(key, item)] = load()
            if cache[(key, item)] is None:
                raise MediaError("No longer on the Mac")
            return cache[(key, item)]

        group = ""
        payload: dict[str, Any] = {}
        if kind == "light_novel":
            ref = media("ln", lambda: ledger.light_novel_media(item))
            if ("ln-chapters", item) not in cache:
                # Same chapter keys as the Mac reader (text hash), so Mac and phone ranges merge.
                with self.service.db.connect() as conn:
                    cache[("ln-chapters", item)] = {
                        int(r["chapter_index"]): (str(r["text_hash"] or ""), int(r["size"] or 0))
                        for r in conn.execute(
                            "SELECT chapter_index,text_hash,COALESCE(char_count,LENGTH(text)) AS size FROM ln_chapters WHERE book_id=?",
                            (item,),
                        )
                    }
            chapters = cache[("ln-chapters", item)]

            def locator(value: dict[str, Any]) -> dict[str, Any]:
                chapter = int(value.get("chapter") or 0)
                fraction = max(0.0, min(1.0, float(value.get("fraction") or 0)))
                key, size = chapters.get(chapter, ("", 0))
                return {
                    "chapter_index": chapter,
                    "chapter_key": key or f"chapter:{chapter}",
                    "character_offset": round(size * fraction),
                }

            first, last = locator(first), locator(last)
            payload = {"locator_policy": "iphone-fraction-v1"}
            group = ledger.light_novel_activity_group(item)
            method = "iphone_reader_visible"
        elif kind == "manga":
            ref = media("manga", lambda: ledger.manga_media(item))
            if ("manga-pages", item) not in cache:
                cache[("manga-pages", item)] = self._manga_pages(item)
            pages = cache[("manga-pages", item)]

            def locator(value: dict[str, Any]) -> dict[str, Any]:
                page = max(0, int(value.get("page") or 0))
                return {"page_index": page, "page_id": f"page:{page}"}

            first, last = locator(first), locator(last)
            payload = {"page_count": pages, "dwell_policy": "page-dwell-v1"}
            method = "iphone_reader_visible"
        elif kind == "anime":

            def episode_media() -> Any:
                # The file may already be cleaned up after watching; its row still names the episode.
                with self.service.db.connect() as conn:
                    row = conn.execute("SELECT video_path FROM episodes WHERE id=?", (item,)).fetchone()
                return ledger.anime_episode_media(Path(str(row["video_path"]))) if row else None

            ref = media("anime", episode_media)
            first = {"position_seconds": max(0.0, float(first.get("seconds") or 0))}
            last = {"position_seconds": max(0.0, float(last.get("seconds") or 0))}
            moved = last["position_seconds"] - first["position_seconds"]
            payload = {
                "measurement": "iphone_video_playing",
                "seek_or_discontinuity": moved < 0 or moved > active * 4 + 10,
            }
            method = "iphone_video_playing"
        else:
            raise ValueError("Unsupported activity")
        return ledger.record_device_interval(
            device_id=device,
            device_name=device_name,
            event_id="iphone:" + event,
            kind=kind,
            media_ref=ref,
            start_utc=start,
            end_utc=end,
            active_seconds=active,
            locator_start=first,
            locator_end=last,
            payload=payload,
            measurement_method=method,
            activity_group_id=group,
            timezone_iana=zone,
            utc_offset=offset,
            clock_skew_seconds=skew,
        )

    def _manga_pages(self, book_id: int) -> int:
        with self.service.db.connect() as conn:
            row = conn.execute("SELECT page_count FROM manga_books WHERE id=?", (book_id,)).fetchone()
        return int(row["page_count"] or 0) if row else 0

    def planning(self) -> dict[str, Any]:
        with self.service.db.connect() as conn:
            anime = self._rows(
                conn,
                """SELECT media_id,title,status,progress,episodes,format,next_airing_episode,next_airing_at,mean_score
                                         FROM anime WHERE status IN ('CURRENT','PLANNING','REPEATING') ORDER BY title COLLATE NOCASE""",
            )
            ready = {
                int(r["media_id"]): int(r["n"])
                for r in self._rows(
                    conn,
                    "SELECT media_id,COUNT(*) AS n FROM episodes WHERE state='ready' AND media_id IS NOT NULL GROUP BY media_id",
                )
            }
        items = []
        for row in anime:
            media_id = int(row["media_id"])
            items.append(
                {
                    "mediaId": media_id,
                    "title": str(row["title"] or ""),
                    "status": str(row["status"] or ""),
                    "progress": int(row["progress"] or 0),
                    "episodes": int(row["episodes"]) if row.get("episodes") else None,
                    "nextEpisode": int(row["next_airing_episode"])
                    if row.get("next_airing_episode")
                    else None,
                    "nextAiringAt": int(row["next_airing_at"]) if row.get("next_airing_at") else None,
                    "readyEpisodes": ready.get(media_id, 0),
                    "score": int(row["mean_score"]) if row.get("mean_score") else None,
                }
            )
        return {
            "current": [i for i in items if i["status"] in {"CURRENT", "REPEATING"}],
            "planned": [i for i in items if i["status"] == "PLANNING"],
        }


def _phone_highlights(value: Any) -> dict[str, Any] | None:
    """The Statistics fun stats for the phone (camelCase, only plain values)."""
    if not isinstance(value, dict):
        return None

    def number(raw: Any) -> float | None:
        return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None

    def entry(raw: Any, *fields: str) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        out: dict[str, Any] = {}
        for name in fields:
            item = raw.get(name)
            out[name] = number(item) if name == "seconds" else str(item or "")
        return out

    words = value.get("words") if isinstance(value.get("words"), dict) else None
    phone_words = None
    if words is not None:
        phone_words = {
            "total": number(words.get("total")),
            "unique": number(words.get("unique")),
            "new": number(words.get("new")),
            "pending": int(words.get("pending") or 0),
            "missing": int(words.get("missing") or 0),
            "byKind": {
                str(kind): {
                    "total": float(row.get("total") or 0),
                    "unique": float(row.get("unique") or 0),
                    "new": float(row.get("new") or 0),
                }
                for kind, row in (words.get("by_kind") or {}).items()
                if isinstance(row, dict)
            },
        }
    return {
        "longestStreakDays": int(value.get("longest_streak_days") or 0),
        "busiestDay": entry(value.get("busiest_day"), "day", "seconds"),
        "longestSession": entry(value.get("longest_session"), "title", "kind", "seconds"),
        "topWork": entry(value.get("top_work"), "title", "kind", "seconds"),
        "animeEpisodes": int(value.get("anime_episodes") or 0),
        "lnCharactersPerMinute": number(value.get("ln_characters_per_minute")),
        "mangaPagesPerHour": number(value.get("manga_pages_per_hour")),
        "words": phone_words,
    }


def streak(days: list[dict[str, Any]]) -> int:
    """Consecutive active days ending today (or yesterday when today is still empty)."""
    active = [float(d.get("seconds") or 0) > 0 for d in sorted(days, key=lambda d: d["day"])]
    if active and not active[-1]:
        active = active[:-1]
    count = 0
    for value in reversed(active):
        if not value:
            break
        count += 1
    return count


def prepare_video(
    source: Path, folder: Path, *, ffmpeg: str, ffprobe: str, progress: Callable[[float], Any] | None = None
) -> dict[str, Any]:
    """MP4 that AVPlayer plays offline: video copied when the iPhone decodes it, Japanese audio as AAC."""
    from .subtitles.stt import _select_japanese_audio_stream

    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    stat = source.stat()
    key = hashlib.sha256(
        f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:iphone-mp4-v1".encode()
    ).hexdigest()[:32]
    target = folder / f"{key}.mp4"
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,pix_fmt:format=duration",
            "-of",
            "json",
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    info = json.loads(probe.stdout or "{}")
    duration = float((info.get("format") or {}).get("duration") or 0)
    if target.is_file() and target.stat().st_size > 0:
        os.utime(target)
        return {"path": target, "durationMs": round(duration * 1000)}
    streams = [s for s in info.get("streams") or [] if isinstance(s, dict)]
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and s.get("codec_name") not in {"mjpeg", "png"}
        ),
        None,
    )
    if video is None:
        raise MediaError("The episode has no video stream")
    audio_index, _details = _select_japanese_audio_stream(source, ffprobe)
    if audio_index is None:
        audio_index = next((int(s["index"]) for s in streams if s.get("codec_type") == "audio"), None)
    audio = next((s for s in streams if s.get("index") == audio_index), None)
    codec = str(video.get("codec_name") or "")
    command = [ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(source), "-map", f"0:{int(video['index'])}"]
    if audio_index is not None:
        command += ["-map", f"0:{int(audio_index)}"]
    if codec == "h264" and str(video.get("pix_fmt") or "yuv420p") == "yuv420p":
        command += ["-c:v", "copy", "-tag:v", "avc1"]
    elif codec == "hevc":
        command += ["-c:v", "copy", "-tag:v", "hvc1"]
    else:
        encoder = "h264_videotoolbox" if os.uname().sysname == "Darwin" else "libx264"
        command += [
            "-c:v",
            encoder,
            "-b:v",
            "3500k",
            "-pix_fmt",
            "yuv420p",
            "-vf",
            "scale='min(1920,iw)':-2",
            "-tag:v",
            "avc1",
        ]
    if audio is not None:
        command += (
            ["-c:a", "copy"]
            if audio.get("codec_name") == "aac"
            else ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
        )
    temporary = target.with_name(target.stem + ".part.mp4")
    command += [
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        str(temporary),
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for line in process.stdout or []:
            if line.startswith("out_time_us=") and duration > 0 and progress is not None:
                try:
                    progress(max(0.0, min(0.99, int(line.split("=", 1)[1]) / 1e6 / duration)))
                except ValueError:
                    pass
        error = (process.stderr.read() if process.stderr else "")[-500:]
        if process.wait() != 0:
            raise MediaError("ffmpeg could not prepare the episode: " + error.strip())
        os.replace(temporary, target)
    finally:
        if process.poll() is None:
            process.kill()
        temporary.unlink(missing_ok=True)
    return {"path": target, "durationMs": round(duration * 1000)}


# ----------------------------------------------------------------- JitenMPV look
_DIM = {"text": "#969696", "outline": "#505050", "outlineSize": 3, "opacity": 150}


def _style(text: str = "#eeeeee", outline: str = "#000000", size: float = 3, **extra: Any) -> dict[str, Any]:
    return {"text": text, "outline": outline, "outlineSize": size, **extra}


def _underlined(color: str) -> dict[str, Any]:
    return _style(underline=True, underlineColor=color, underlineThickness=3)


# Ported from JitenMPV Theming/ThemePresets.cs (state order: JITEN_STATES).
JITEN_THEMES: dict[str, list[dict[str, Any]]] = {
    "Default": [
        _style("#a566ef"),
        _style("#eeeeee", "#d08700"),
        _style("#eeeeee", "#70c000"),
        dict(_DIM),
        _style("#eeeeee", "#ff4500"),
        _style("#c8c8c8", "#006400", opacity=200),
        dict(_DIM),
        dict(_DIM),
    ],
    "High Contrast": [
        _style("#00ffff", size=4, bold=True),
        _style("#ffff00", size=4, underline=True),
        _style("#00ff00", size=4),
        _style("#808080", opacity=120),
        _style("#ff4444", size=4, bold=True),
        _style("#88ff88"),
        _style("#808080", opacity=120),
        _style("#808080", opacity=120),
    ],
    "Monochrome": [
        _style("#cccccc"),
        _style("#999999"),
        _style("#666666"),
        _style("#eeeeee", opacity=102),
        _style("#ffffff", underline=True),
        _style("#eeeeee"),
        _style("#eeeeee", opacity=102),
        _style("#eeeeee", opacity=102),
    ],
    "Subtle": [
        _style("#e0e0e0", "#3a2060", 2),
        _style("#e0e0e0", "#4a3500", 2),
        _style("#e0e0e0", "#2a4a00", 2),
        _style("#c0c0c0", "#202020", 2, opacity=180),
        _style("#e0e0e0", "#4a1500", 2),
        _style("#c8c8c8", "#1a3a00", 2),
        _style("#c0c0c0", "#202020", 2, opacity=180),
        _style("#c0c0c0", "#202020", 2, opacity=180),
    ],
    "Underline": [
        _underlined("#a566ef"),
        _underlined("#e8a020"),
        _style(),
        _style(),
        _underlined("#e03030"),
        _style(),
        _underlined("#4b9fff"),
        _style(),
    ],
    "Toy Box": [
        _style("#4b8dff"),
        _style("#55b87a", underline=True),
        _style(),
        _style("#888888", opacity=130),
        _style("#d08700", underline=True),
        _style("#aaaaaa"),
        _style("#888888", opacity=130),
        _style("#888888", opacity=130),
    ],
}
_JITEN_STATE_NAMES = ("New", "Young", "Mature", "Blacklisted", "Due", "Mastered", "Redundant", "Suspended")


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _opacity(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else max(0, min(255, int(round(number))))


def jiten_mpv_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base) if base and Path(base).is_absolute() else Path.home() / ".config"
    return root / "jiten-mpv" / "config.json"


def subtitle_look(path: Path | None = None) -> dict[str, Any]:
    """Word styles of the user's JitenMPV setup (theme or custom colours), for iPhone subtitles."""
    path = path or jiten_mpv_config_path()
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        source = "jiten-mpv"
    except (OSError, ValueError):
        settings, source = {}, "default"
    if not isinstance(settings, dict):
        settings, source = {}, "default"
    theme = str(settings.get("Theme") or "Default")
    states = [dict(item) for item in JITEN_THEMES.get(theme, JITEN_THEMES["Default"])]
    custom = settings.get("CustomThemeColors")
    if theme == "Custom" and isinstance(custom, dict):
        for index, name in enumerate(_JITEN_STATE_NAMES):
            row = custom.get(name)
            if not isinstance(row, dict):
                continue
            states[index] = {
                key: value
                for key, value in {
                    "text": str(row.get("text_color") or "#ffffff"),
                    "outline": str(row.get("outline_color") or "#000000"),
                    "outlineSize": _number(row.get("outline_size"))
                    if _number(row.get("outline_size")) is not None
                    else 3.0,
                    "opacity": _opacity(row.get("text_opacity")),
                    "bold": bool(row.get("bold")),
                    "italic": bool(row.get("italic")),
                    "underline": bool(row.get("underline")),
                    "underlineColor": str(row["underline_color"]) if row.get("underline_color") else None,
                    "underlineThickness": _number(row.get("underline_thickness")),
                    "shadow": str(row["shadow_color"]) if row.get("shadow_color") else None,
                }.items()
                if value is not None
            }

    def number(key: str, default: float) -> float:
        try:
            return float(settings.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "source": source,
        "theme": theme,
        "fontFamily": str(settings.get("FontFamily") or "Hiragino Sans"),
        "fontSize": number("FontSize", 48),
        "borderSize": number("BorderSize", 3),
        "unparsed": _style(),
        "states": states,
    }
