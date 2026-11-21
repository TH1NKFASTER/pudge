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
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .word_states import DEFAULT_WORD_COLORS, WORD_STATES, canonical_state, normalized_word_state, word_color
from .fsutil import atomic_write_json
from . import ln_read_marker

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
# A review-gate grade held back by the Jiten budget is sent again up to twice when the
# wait is short (the phone's request waits meanwhile).
GATE_RETRY_ATTEMPTS = 3
GATE_RETRY_MAX_WAIT_SECONDS = 15.0
# Words per iPhone request for offline dictionary entries.
_WORDS_PER_REQUEST = 200
# Longest wait for Jiten's live word states before a text goes to the phone (then as parsed).
_LIVE_STATE_SECONDS = 3.0
# Prepared episode copies (MP4) for the phones: past this total the least recently used go,
# never one a phone asked for in the last _VIDEO_PIN_SECONDS or is downloading now.
_VIDEO_BUDGET_BYTES = 16 * 1024**3
_VIDEO_PIN_SECONDS = 2 * 3600
# An episode watched on a phone straight from the Mac (Watch, no download) holds its prepared
# copy through a stream: the phone renews it every 30 s while the player is open and closes it
# with the player; a phone that went away (Wi-Fi gone, app closed) lets it go this long after
# its last renewal or byte range.
_STREAM_LEASE_SECONDS = 90.0
# Streams one device holds at most (opening another lets its oldest go).
_STREAMS_PER_DEVICE = 3
_STREAM_ID = re.compile(r"[0-9a-f]{32}")
# Media kinds a volume of which the phone can download for reading offline.
_READING_KINDS = ("ln", "manga")


def _positive_int(value: Any) -> int | None:
    """``value`` as a positive int (None for anything else)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _isolated_from_library(video_path: object) -> bool:
    """A video in a folder marked ``.pudge-ignore-library-scan`` (a benchmark corpus): the
    Mac's Home hides it, so the iPhone is never offered it either."""
    from .manager import AnimeManager

    return AnimeManager._library_path_is_ignored(Path(str(video_path)))


class MediaError(Exception):
    """A user-facing failure (HTTP 422 with the message): the phone takes it as final."""


class MediaTemporaryError(MediaError):
    """A user-facing failure that passes ("try again later": HTTP 503 with the message); the
    phone keeps what it sent and sends it again."""


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


def frequency_rank(detail: dict[str, Any], reading_index: int) -> int | None:
    """Jiten's frequency rank of a word (1: the most frequent), None when unknown.

    Where Jiten puts it is not certain: the words of a parse carry ``frequencyRank`` at the top,
    and its vocabulary info (a word looked up on the phone) may keep it on the reading instead
    (``mainReading`` / ``alternativeReadings``; the one fixture in its real shape,
    ``powder_dto`` in tests/test_v27_ui_and_workers.py, has none there). Both places are read;
    the rank of another reading of the word is never taken."""

    def number(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def rank(value: Any) -> int | None:
        found = number(value)
        return found if found is not None and found > 0 else None

    found = rank(detail.get("frequencyRank"))
    if found:
        return found
    rows = (
        [detail.get("mainReading")]
        + list(detail.get("alternativeReadings") or [])
        + list(detail.get("readings") or [])
    )
    for row in rows:
        if isinstance(row, dict) and number(row.get("readingIndex")) == reading_index:
            found = rank(row.get("frequencyRank"))
            if found:
                return found
    return None


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


# JitenMPV's KnownState order, and the collapse priority for words with several states:
# JitenMPV's, except that Due comes first as everywhere on the Mac (word_states.WORD_STATE_PRIORITY):
# a word due for review shows as due, whatever else it is, until it is graded.
JITEN_STATES = ("new", "young", "mature", "blacklisted", "due", "mastered", "redundant", "suspended")
_JITEN_PRIORITY = ("due", "blacklisted", "mastered", "redundant", "suspended", "mature", "young", "new")


def jiten_state(card: dict[str, Any] | None) -> int:
    """JitenMPV KnownState of a word (collapsed like KnownStates.Collapse)."""
    card = card if isinstance(card, dict) else {}
    values = {
        str(x).strip().casefold()
        for x in (card.get("cardState") or card.get("states") or card.get("knownState") or [])
    }
    # Other names as the Mac reader reads them (word_states.STATE_ALIASES): "known" (jpdb, older
    # caches) is mature there; taken as mastered it got the themes' dimmed Mastered look on the
    # phone while the Mac showed it green. never_forget / not-in-deck spellings likewise.
    values = {canonical_state(x) for x in values}
    if not values:
        normalized = str(card.get("normalizedState") or "").casefold()
        values = {canonical_state(normalized)}
    for name in _JITEN_PRIORITY:
        if name in values:
            return JITEN_STATES.index(name)
    return 0


def _state_label(card: dict[str, Any] | None, label: str) -> str:
    """The word card's state label: Jiten's, except "Due" for a due word (its colour is Due:
    a mature-but-due word was labelled "Mature" in a Due-coloured badge)."""
    return "Due" if word_state(card) == "due" else label


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


_STATS_PERIODS = ("7d", "30d", "year", "all")
# What the phone's Statistics are built from: rows added (count, last rowid) or changed in
# place (updated_at, ended sessions, parsed words).
_STATS_STAMP_QUERIES = (
    "SELECT COUNT(*),COALESCE(MAX(rowid),0) FROM consumption_events",
    "SELECT COUNT(*),COALESCE(MAX(rowid),0) FROM consumption_event_media",
    "SELECT COUNT(*),COALESCE(MAX(rowid),0),COUNT(ended_at_utc),TOTAL(ended_at_utc),"
    "TOTAL(LENGTH(status)),TOTAL(LENGTH(metadata_json)) FROM consumption_sessions",
    "SELECT COUNT(*),COALESCE(MAX(rowid),0) FROM consumption_corrections",
    "SELECT COUNT(*),COALESCE(MAX(updated_at_utc),0),TOTAL(duration_seconds) FROM consumption_manual_entries",
    "SELECT COUNT(*),COALESCE(MAX(updated_at),0) FROM consumption_media",
    "SELECT COUNT(*),COALESCE(MAX(updated_at),0) FROM consumption_word_units",
    "SELECT COALESCE(MAX(reset_epoch),0),COALESCE(MAX(reset_at_utc),0) FROM consumption_history_state",
    "SELECT COUNT(*),COALESCE(MAX(rowid),0) FROM vn_capture_lines",
)


def _job_state_without_copy(job: dict[str, Any]) -> str:
    state = str(job.get("state") or "idle")
    return "idle" if state == "ready" else state


class IPhoneMedia:
    def __init__(self, service: Any) -> None:
        self.service = service
        self.lock = threading.Lock()
        # When AniList last refused a watch counted from the parts of several devices (episode id).
        self._union_refused: dict[int, float] = {}
        self._union_counting: set[int] = set()  # episodes whose union count is being sent
        # Preparations by episode (the original copy) or (episode, quality) for a smaller one.
        self.video_jobs: dict[Any, dict[str, Any]] = {}
        # ffprobe facts of episode files for the size of each quality (key: path:size:mtime).
        self._source_facts: dict[str, dict[str, Any]] = {}
        self._facts_waiting: dict[str, Path] = {}
        self._facts_worker = False
        # Prepared videos a phone asked for (path -> time) or is downloading (path -> open transfers).
        self._video_lock = threading.Lock()
        self._video_used: dict[str, float] = {}
        self._video_streams: dict[str, int] = {}
        # Episodes played on a phone from the Mac now (stream id -> stream), see open_stream.
        self._streams: dict[str, dict[str, Any]] = {}
        from .iphone_reading import ReadingPackages

        # Light novels and manga volumes downloaded to the phone for reading offline.
        self.reading = ReadingPackages(self)
        from .iphone_grammar import IPhoneGrammar

        # "Explain grammar" and follow-up questions through the Mac's LLM.
        self.grammar = IPhoneGrammar(self)
        from .iphone_finale import IPhoneFinale

        # The end of an anime watched here: its score (wheel) and the next entry, as on the Mac.
        self.finale = IPhoneFinale(self)
        from .iphone_screen_cache import ScreenCache

        # Statistics answered at once from a copy built in the background (iphone_screen_cache).
        self.screens = ScreenCache(
            Path(service.root) / "screen-cache",
            idle_ok=self._mac_idle,
            logger=lambda: getattr(getattr(self.service, "api", None), "logger", None),
        )
        for period in _STATS_PERIODS:
            self.screens.register(
                "stats-" + period, self._stats_stamp, lambda period=period: self.stats(period)
            )
        if self.screens.wanted():
            # A phone looked at Statistics before: keep its copies current from now on.
            self.screens._ensure_thread()

    # ---------------------------------------------------------------- plumbing
    @property
    def api(self) -> Any:
        api = getattr(self.service, "api", None)
        if api is None:
            raise MediaTemporaryError("The Mac app is still starting")
        return api

    def handle(
        self,
        method: str,
        route: str,
        query: dict[str, list[str]],
        body: Callable[[], dict[str, Any]],
        device: str = "",
    ) -> Any:
        parts = route.removeprefix("/api/v2/media/").strip("/").split("/")
        head, rest = parts[0], parts[1:]
        number = lambda text: int(text) if text.isdigit() else -1  # noqa: E731
        if method == "GET" and head == "ln" and not rest:
            return self.light_novels()
        if head == "ln" and len(rest) == 1 and method == "GET":
            return self.light_novel(number(rest[0]), device=device)
        if head == "ln" and len(rest) == 3 and rest[1] == "chapters" and method == "GET":
            return self.chapter(number(rest[0]), number(rest[2]), poll=query.get("poll", ["0"])[0] == "1")
        if head == "ln" and len(rest) == 2 and rest[1] == "position" and method == "POST":
            return self.ln_position(number(rest[0]), body(), device=device)
        if head == "ln" and len(rest) == 2 and rest[1] == "marker" and method == "POST":
            return self.ln_marker(number(rest[0]), body(), device=device)
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
        if head == "manga" and len(rest) == 2 and rest[1] == "spreads" and method in {"GET", "POST"}:
            return self.manga_spreads(number(rest[0]), body() if method == "POST" else None)
        if head == "episodes" and not rest and method == "GET":
            return self.episodes()
        if head == "anime" and not rest and method == "GET":
            return self.anime()
        if head == "anime" and len(rest) == 2 and rest[1] in {"score", "next-entry"}:
            return self.finale.handle(method, number(rest[0]), rest[1], body)
        if head == "study" and rest == ["states"] and method == "POST":
            return self.study_states(body())
        if head == "episodes" and len(rest) == 1 and method == "GET":
            return self.episode(number(rest[0]), **self._asked_quality(query, None))
        if head == "episodes" and len(rest) == 2 and rest[1] == "prepare" and method == "POST":
            return self.prepare_episode(number(rest[0]), **self._asked_quality(query, body))
        if head == "episodes" and len(rest) == 2 and rest[1] == "stream" and method == "POST":
            return self.open_stream(number(rest[0]), device, **self._asked_quality(query, body))
        if head == "streams" and len(rest) == 2 and rest[1] == "keepalive" and method == "POST":
            return self.renew_stream(rest[0], device)
        if head == "streams" and len(rest) == 2 and rest[1] == "close" and method == "POST":
            return self.close_stream(rest[0], device)
        if head == "episodes" and len(rest) == 2 and rest[1] == "subtitles" and method == "GET":
            return self.episode_subtitles(number(rest[0]))
        if head == "episodes" and len(rest) == 2 and rest[1] == "position" and method == "POST":
            return self.episode_position(number(rest[0]), body())
        if head == "episodes" and len(rest) == 2 and rest[1] == "subtitle-delay" and method == "POST":
            return self.episode_subtitle_delay(number(rest[0]), body())
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
            period = query.get("period", ["30d"])[0]
            return self.screens.serve("stats-" + (period if period in _STATS_PERIODS else "30d"))
        if head == "planning" and not rest and method == "GET":
            return self.planning()
        if head == "grammar":
            return self.grammar.handle(method, rest, query, body, device)
        raise KeyError("Unknown media operation")

    @staticmethod
    def _asked_quality(
        query: dict[str, list[str]], body: Callable[[], dict[str, Any]] | None
    ) -> dict[str, str]:
        """The copy a phone asks for (``?quality=`` or ``{"quality": ...}``) as the call's
        keyword; an older phone sends nothing (or an empty body) and gets the original (no
        keyword: the call is the one of before)."""
        asked: Any = (query.get("quality") or [None])[0]
        if asked is None and body is not None:
            try:
                asked = body().get("quality")
            except ValueError:
                asked = None
        quality = video_quality(asked)
        return {} if quality == "original" else {"quality": quality}

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
            # The phone's assets of the linked audiobooks: a finished audiobook offers to mark
            # its light novel finished.
            audio_assets: dict[int, list[str]] = {}
            for r in self._rows(
                conn,
                "SELECT l.ln_book_id,a.id AS asset FROM reading_audio_links l "
                "JOIN iphone_assets a ON a.book_id=l.audiobook_id ORDER BY l.updated_at DESC",
            ):
                assets = audio_assets.setdefault(int(r["ln_book_id"]), [])
                if str(r["asset"]) not in assets:
                    assets.append(str(r["asset"]))
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
                    "audioAssetIds": audio_assets.get(book_id, []),
                    "updatedAt": float(book["updated_at"] or 0),
                    "seriesKey": str(group.get("series_key") or _series_key(title) or f"book:{book_id}"),
                    "seriesTitle": str(group.get("series_title") or _series_title(title) or title),
                    "franchiseKey": str(group.get("franchise_key") or ""),
                    "franchiseTitle": str(group.get("franchise_title") or ""),
                    "franchiseOrder": int(group.get("franchise_order") or 0),
                    "progress": float(group.get("reading_progress") or (1.0 if book["finished"] else 0.0)),
                    # The Mac card's chips ("123k chars", the AniList year).
                    "characters": _positive_int(group.get("character_count")),
                    "year": _positive_int(group.get("anilist_year")),
                }
            )
        return {"items": items}

    def light_novel(self, book_id: int, device: str = "") -> dict[str, Any]:
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
            # The "read up to here" marker (ln_read_marker); older phones ignore it.
            "marker": self._phone_marker(book_id, device=device),
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
        from PIL import Image

        from .light_novels import LN_ILLUSTRATION_LIMIT

        try:
            # A huge file or one over Pillow's pixel limit (DecompressionBombError is no
            # OSError) failed the request and the phone's whole package with it.
            if source.stat().st_size > LN_ILLUSTRATION_LIMIT:
                raise MediaError("This illustration is too large")
            data = source.read_bytes()
            with Image.open(io.BytesIO(data)) as image:
                if image.width <= width and source.suffix.casefold() in {".jpg", ".png"}:
                    return data, "image/png" if source.suffix.casefold() == ".png" else "image/jpeg"
            return self._thumbnail(data, width=width), "image/jpeg"
        except (OSError, ValueError, Image.DecompressionBombError) as exc:
            raise MediaError("No such illustration") from exc

    @staticmethod
    def _observed(value: dict[str, Any]) -> float | None:
        """When the phone read there (seconds, on the Mac clock); None from older apps.

        A phone clock ahead of the Mac's moves the time back by that lead, as for activity
        (`sentAt`, the phone's time when sending, or else the place's own time).
        """
        try:
            observed = float(value.get("observedAt"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(observed) or observed <= 0:
            return None
        sent = value.get("sentAt")
        phone_now = float(sent) if isinstance(sent, (int, float)) and math.isfinite(sent) else observed
        lead = phone_now - time.time()
        return observed - lead if lead > 300.0 else observed

    @staticmethod
    def _marker_time(value: dict[str, Any]) -> float | None:
        """When a marker was set (seconds, Mac clock); None from older apps.

        Always moved by the phone's clock difference (``sentAt`` against now), slow or fast: a
        marker set a minute ago on a phone running ten minutes behind is a minute old, not ten
        and older than the Mac's (the 300 s tolerance of places would drop it)."""
        try:
            observed = float(value.get("observedAt"))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(observed) or observed <= 0:
            return None
        sent = value.get("sentAt")
        if isinstance(sent, (int, float)) and not isinstance(sent, bool) and math.isfinite(sent) and sent > 0:
            observed += time.time() - float(sent)
        return min(observed, time.time())

    def _require_light_novel(self, book_id: int) -> None:
        """A volume deleted on the Mac is a final answer (HTTP 422), so a phone's queue moves on."""
        with self.service.db.connect() as conn:
            if conn.execute("SELECT 1 FROM ln_books WHERE id=?", (int(book_id),)).fetchone() is None:
                raise MediaError("This light novel is no longer on the Mac")

    def _stale_position(
        self, sql: str, item: int, observed: float | None, behind: Callable[[Any], bool]
    ) -> tuple[bool, float]:
        """A place read earlier on one device (sent late, e.g. after reading offline) never takes
        a newer place from another device or the Mac back; a place further ahead always counts.

        Also returns the stored place's time (the query's last column).
        """
        with self.service.db.connect() as conn:
            row = conn.execute(sql, (item,)).fetchone()
        stored = float(row[-1] or 0) if row is not None else 0.0
        return bool(observed is not None and row is not None and stored > observed and behind(row)), stored

    def _stamp_position(self, table: str, item: int, observed: float | None, stored: float) -> None:
        """The place taken from the phone gets the time it was read there, never earlier than
        the place it replaced: with the time it arrived, a place read offline and sent late kept
        out another device's later reading. Older apps (no time): the Mac's save time stays.
        Always after the replaced place's time: the Mac reader keeps out its own older saves by
        it (a place read earlier but further ahead had exactly the time the Mac knew)."""
        if observed is None:
            return
        with self.service.db.connect() as conn:
            conn.execute(
                f"UPDATE {table} SET position_updated_at=? WHERE id=?",
                (max(stored + 0.001, min(observed, time.time())), item),
            )

    # ------------------------------------------------------------ read marker
    def _marker_reader(self) -> Any:
        """The Mac's light-novel service (None in a bare test service or while starting)."""
        return getattr(getattr(self.service, "api", None), "light_novels", None)

    def _chapter_text(self, book_id: int, chapter: int) -> str:
        with self.service.db.connect() as conn:
            row = conn.execute(
                "SELECT text FROM ln_chapters WHERE book_id=? AND chapter_index=?",
                (int(book_id), int(chapter)),
            ).fetchone()
        return str(row[0] or "") if row is not None else ""

    def _phone_marker(self, book_id: int, marker: Any = False, device: str = "") -> dict[str, Any] | None:
        reader = self._marker_reader()
        if marker is False:
            if reader is None or not callable(getattr(reader, "read_marker", None)):
                return None
            try:
                marker = reader.read_marker(int(book_id))
            except Exception:  # noqa: BLE001 - the volume's place still opens without its marker
                return None
        if not marker:
            return None
        text = self._chapter_text(book_id, int(marker.get("chapter_index") or 0))
        return ln_read_marker.phone_view(marker, text, device)

    def _move_phone_marker(
        self,
        book_id: int,
        value: dict[str, Any],
        kind: str,
        observed: float | None,
        device: str,
        event_id: str | None = None,
    ) -> dict[str, Any] | None:
        """The phone's place moves the marker by its rules (ln_read_marker): ``paragraph`` and
        ``paragraphOffset`` (UTF-16) from newer apps, else the paragraph of ``offset``."""
        reader = self._marker_reader()
        if reader is None or not callable(getattr(reader, "move_read_marker", None)):
            return None
        chapter = int(value.get("chapter") or 0)
        text = self._chapter_text(book_id, chapter)
        if isinstance(value.get("paragraph"), int) and not isinstance(value.get("paragraph"), bool):
            paragraph = int(value["paragraph"])
            utf16 = value.get("paragraphOffset")
            utf16 = int(utf16) if isinstance(utf16, int) and not isinstance(utf16, bool) else 0
        else:
            paragraph = ln_read_marker.phone_paragraph_at_fraction(text, float(value.get("offset") or 0))
            utf16 = 0
        name_of = getattr(self.service, "device_name", None)
        try:
            name = str(name_of(device)) if device and callable(name_of) else ""
        except Exception:  # noqa: BLE001 - the name is only shown
            name = ""
        moved = reader.move_read_marker(
            int(book_id),
            chapter,
            source=kind,
            char_offset=ln_read_marker.char_from_phone(text, paragraph, utf16),
            device_id=device or "iphone",
            device_name=name or "iPhone/iPad",
            at=observed,
            **({"event_id": event_id} if event_id else {}),
        )
        answer = {
            "moved": bool(moved.get("moved")),
            "marker": self._phone_marker(book_id, moved.get("marker"), device),
        }
        if moved.get("duplicate"):
            answer["duplicate"] = True
        return answer

    def ln_marker(self, book_id: int, value: dict[str, Any], device: str = "") -> dict[str, Any]:
        """ "Read up to here" on the phone (``kind`` manual) or the sentence its audiobook reads
        (auto): ``{chapter, paragraph, paragraphOffset, kind, markerId?, observedAt?, sentAt?}``.

        ``markerId``: the phone's id of a marker (one it queued without the Mac is sent with
        it later); the same id again is answered as the first time and changes nothing."""
        kind = ln_read_marker.kind_of(value.get("kind") or "manual")
        marker_id = value.get("markerId")
        event = (
            f"{device or 'iphone'}:{marker_id}"
            if isinstance(marker_id, str) and 0 < len(marker_id) <= 64
            else None
        )
        self._require_light_novel(book_id)
        moved = self._move_phone_marker(book_id, value, kind, self._marker_time(value), device, event)
        if moved is None:
            raise MediaTemporaryError("The Mac app is still starting")
        return {"ok": True, **moved}

    def ln_position(self, book_id: int, value: dict[str, Any], device: str = "") -> dict[str, Any]:
        self._require_light_novel(book_id)
        if value.get("finishOnly") is True:
            # "Mark light novel read" from a finished audiobook (capability "ln-finish"): the
            # volume is finished, its place and read marker stay as they are. Phones before it
            # sent the list's place with observedAt 1 and relied on the stale-place rule; a
            # volume never opened got that place, stamped at 1 s after 1970, and an automatic
            # marker of 1970 that a later reading place could not move back past.
            try:
                self.api.light_novel_set_finished(book_id, True)
            except Exception as exc:  # noqa: BLE001 - AniList may be offline; Finished is saved
                return {"ok": True, "finished": True, "warning": str(exc)}
            return {"ok": True, "finished": True}
        result = self._ln_position(book_id, value)
        try:
            moved = self._move_phone_marker(book_id, value, "auto", self._marker_time(value), device)
        except Exception as exc:  # noqa: BLE001 - the place is saved; the marker follows next time
            logger = getattr(getattr(self.service, "api", None), "logger", None)
            if logger is not None:
                logger.warning("iPhone LN marker book=%s failed: %s", book_id, exc)
            moved = None
        # Newer apps (they send `paragraph`) show the marker the place moved.
        if moved is not None and "paragraph" in value:
            result["marker"] = moved["marker"]
        return result

    def _ln_position(self, book_id: int, value: dict[str, Any]) -> dict[str, Any]:
        chapter = int(value.get("chapter") or 0)
        offset = max(0.0, min(1.0, float(value.get("offset") or 0)))
        observed = self._observed(value)
        # position_updated_at, not updated_at: AniList refreshes, scores and covers change
        # updated_at too, and a newer place from the phone was dropped as older.
        stale, stored = self._stale_position(
            "SELECT current_chapter,current_offset,position_updated_at FROM ln_books WHERE id=?",
            book_id,
            observed,
            lambda row: (chapter, offset) <= (int(row[0] or 0), float(row[1] or 0)),
        )
        if stale and value.get("finished") is not True:
            return {"ok": True, "kept": "newer"}
        if not stale:
            self.api.light_novel_position(book_id, chapter, offset)
            self._stamp_position("ln_books", book_id, observed, stored)
        if value.get("finished") is True:
            try:
                self.api.light_novel_set_finished(book_id, True)
            except Exception as exc:  # noqa: BLE001 - AniList may be offline; the position is saved
                return {"ok": True, "warning": str(exc)}
        return {"ok": True}

    # ------------------------------------------------------------------- manga
    def manga_list(self, book_id: int | None = None) -> dict[str, Any]:
        """The manga volumes for the phone; ``book_id``: that volume only (manga_book: one
        volume's card read the OCR state of every volume in the library to find its own)."""
        only = " WHERE id=?" if book_id is not None else ""
        with self.service.db.connect() as conn:
            rows = self._rows(
                conn,
                "SELECT id,title,path,page_count,position,read_pages,reading_direction,cover_url,updated_at FROM manga_books"
                + only
                + " ORDER BY updated_at DESC",
                (int(book_id),) if book_id is not None else (),
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
        for item in self.manga_list(book_id)["items"]:
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
        from .manga import MangaService, read_page_member

        # A CBZ/ZIP archive, or an image folder read in place.
        with MangaService._open_book(path) as archive:
            try:
                data = read_page_member(archive, name)
            except ValueError as exc:
                raise MediaError(str(exc)) from exc
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

    def manga_spreads(self, book_id: int, change: dict[str, Any] | None = None) -> dict[str, Any]:
        """What the iPhone/iPad landscape view shows together (the Mac's two-page table): the
        wide pages kept alone, the volume's "Shift pairs by one page", the spreads, the usual
        page's height / width, "Align scanned pages" and the pages' transforms. A POST `{shift}`
        and/or `{align}` changes the settings (as on the Mac)."""
        import zipfile

        try:
            if change is not None:
                shift, align = change.get("shift"), change.get("align")
                if shift is None and align is None:
                    raise MediaError("shift or align must be given")
                if shift is not None and not isinstance(shift, bool):
                    raise MediaError("shift must be true or false")
                if align is not None and not isinstance(align, bool):
                    raise MediaError("align must be true or false")
                answer: dict[str, Any] = {}
                if shift is not None:
                    answer = self.api.manga.set_pair_shift(int(book_id), shift)
                if align is not None:
                    answer = self.api.manga.set_page_align(int(book_id), align)
                return answer
            return self.api.manga.spread_pages(int(book_id))
        except KeyError as exc:
            raise MediaError("This manga is no longer on the Mac") from exc
        except (OSError, zipfile.BadZipFile) as exc:
            raise MediaError("The Mac cannot read this volume's pages") from exc

    def manga_position(self, book_id: int, value: dict[str, Any]) -> dict[str, Any]:
        page = max(0, int(value.get("page") or 0))
        # Two pages side by side (iPhone/iPad landscape): the place is the first one, both were read.
        second = value.get("readThrough")
        through = (
            page + 1
            if isinstance(second, int) and not isinstance(second, bool) and second == page + 1
            else page
        )
        # A package made while junk pages counted sends their numbering (the phone sends no
        # pages revision): mapped back, so "read" never marks the whole volume.
        convert = getattr(getattr(self.api, "manga", None), "page_from_junk_listing", None)
        if callable(convert):
            try:
                page, through = int(convert(int(book_id), page)), int(convert(int(book_id), through))
            except (KeyError, OSError, ValueError):
                pass
        observed = self._observed(value)
        stale, stored = self._stale_position(
            "SELECT position,position_updated_at FROM manga_books WHERE id=?",
            book_id,
            observed,
            lambda row: page <= int(row[0] or 0),
        )
        if stale:
            return {"ok": True, "kept": "newer"}
        self.api.manga_set_position(book_id, page)
        self._stamp_position("manga_books", book_id, observed, stored)
        if value.get("read") is True:
            self.api.manga_mark_read(book_id, max(page, through))
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
        from .manager import AnimeManager

        with AnimeManager.library_ignore_memo():
            return self._anime_groups()

    def _anime_groups(self) -> dict[str, Any]:
        with self.service.db.connect() as conn:
            rows = self._rows(
                conn,
                """
                SELECT e.id,e.media_id,e.episode,COALESCE(e.media_episode,e.episode) AS number,e.media_episode_end AS number_end,e.part_boundary,e.title,e.state,e.video_path,
                       e.subtitle_path,e.embedded_subtitle_id,e.playback_position,e.playback_duration,e.updated_at,
                       a.title AS anime_title,a.status AS anime_status,a.progress AS anime_progress,a.episodes AS anime_episodes
                FROM episodes e LEFT JOIN anime a ON a.media_id=e.media_id
                WHERE e.state IN ('ready','couldnt_sync') AND e.watched_at IS NULL AND e.delete_after IS NULL
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
            present = [
                row
                for row in members
                if Path(str(row["video_path"])).is_file() and not _isolated_from_library(row["video_path"])
            ]
            episodes = [self._episode_item(row) for row in self._one_copy_per_episode(present, media_id)]
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
        home = self._home_snapshot(api)
        order = self._home_order(api, home)
        new_rank = _HOME_ORDER.index("new_ready")
        for group in result:
            # Ongoing (airing, a personal weekly schedule, or under the Mac's New episodes ready):
            # the phone shows it under New episodes ready; finished titles under Completed & ready.
            media_id = int(group["mediaId"] or 0)
            anime = library.get(media_id)
            status = str(getattr(anime, "media_status", "") or "").upper()
            group["airing"] = bool(
                status == "RELEASING"
                or group.get("schedule") is not None
                or order.get(media_id, (None,))[0] == new_rank
            )
            # The Mac's Continue watching card says "Movie • resume at …" for a movie.
            group["movie"] = str(getattr(anime, "format", "") or "").upper() == "MOVIE"
        result.sort(
            key=lambda group: (
                *order.get(int(group["mediaId"] or 0), (len(_HOME_ORDER), 0)),
                group["title"].casefold(),
            )
        )
        answer: dict[str, Any] = {"items": result}
        if home is not None:
            # The Mac Home's Caught up and Waiting/Available to download cards (titles with no
            # ready episode, which ``items`` does not list); older phones ignore the key.
            answer["home"] = self._home_shelves(home, torrents_enabled=self._torrents_enabled(api))
        hours = self._delete_watched_hours(api)
        if hours is not None:
            # The phone deletes its watched downloads after the same delay (its Settings
            # can choose otherwise); older phones ignore the key.
            answer["deleteWatchedAfterHours"] = hours
        return answer

    def _one_copy_per_episode(self, rows: list[dict[str, Any]], media_id: int) -> list[dict[str, Any]]:
        """One row per episode number: two files of the same episode (an older single download
        and a later batch) were listed, and downloaded, twice. The copy the Mac plays stays
        (database.preferred_copy, as ready_episode); the other one is not offered."""
        if not media_id:
            return rows
        by_number: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            by_number.setdefault(int(row["number"] or 0), []).append(row)
        if all(len(copies) == 1 for copies in by_number.values()):
            return rows
        from .database import Database, preferred_copy

        with self.service.db.connect() as conn:
            counts = Database._folder_counts(conn, media_id)
        chosen = {
            id(preferred_copy(copies, counts) if len(copies) > 1 else copies[0])
            for copies in by_number.values()
        }
        return [row for row in rows if id(row) in chosen]

    @staticmethod
    def _delete_watched_hours(api: Any) -> float | None:
        """agent.delete_after_watched_hours of the Mac (0: right after the watch); None: unknown."""
        try:
            value = float(api.config.agent.delete_after_watched_hours)
        except (AttributeError, TypeError, ValueError):
            return None
        return max(0.0, value) if math.isfinite(value) else None

    @staticmethod
    def _home_snapshot(api: Any) -> dict[str, Any] | None:
        """The Mac Home page's sections (usually its cached snapshot); None when unknown."""
        try:
            home = (api.get_state_fast() or {}).get("home")
        except Exception:  # noqa: BLE001 - without it the list stays alphabetical
            return None
        return home if isinstance(home, dict) else None

    @classmethod
    def _home_order(cls, api: Any, home: dict[str, Any] | None = None) -> dict[int, tuple[int, int]]:
        """media id -> (section, position) on the Mac Home page; usually its cached snapshot."""
        if home is None:
            home = cls._home_snapshot(api) or {}
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
    def _torrents_enabled(api: Any) -> bool:
        """Torrents switched on in the Mac window (its cards call a download paused otherwise)."""
        try:
            settings = (api.get_state_fast() or {}).get("settings") or {}
            return bool(settings.get("torrents_enabled", True))
        except Exception:  # noqa: BLE001 - unknown: as the Mac shows a running download
            return True

    @staticmethod
    def _download_kind(download: dict[str, Any], torrents_enabled: bool) -> str:
        """The Mac card's name for a torrent's state (torrentUiState in torrent_status_ui.js)."""
        raw = str(download.get("state") or "").strip().lower()
        effective = str(download.get("effective_state") or download.get("state") or "").lower()
        try:
            progress = float(download.get("progress") or 0)
        except (TypeError, ValueError):
            progress = 0.0
        if progress >= 1 or re.search(r"complete|completed|seeding|uploading", effective):
            return "finished"
        if re.search(r"error|missingfiles", raw):
            return "error"
        if re.search(r"forcedup|pausedup|stoppedup|queuedup|stalledup", raw):
            return "finished"
        if not torrents_enabled:
            return "paused"
        for kind, pattern in (
            ("paused", r"paused|stopped"),
            ("waiting", r"queued|waiting"),
            ("stalled", r"stalled"),
            ("checking", r"checking|moving|allocating"),
            ("downloading", r"downloading|active|forceddl|metadl"),
        ):
            if re.search(pattern, raw):
                return kind
        return "unknown"

    @classmethod
    def _home_shelves(
        cls, home: dict[str, Any], *, torrents_enabled: bool = True
    ) -> dict[str, list[dict[str, Any]]]:
        """The Mac Home cards of titles with nothing to play yet, in the Mac's order.

        ``caughtUp``: its Caught up; ``waiting``: its Needs action, Waiting for preparation and
        Available to download (the phone shows them read-only, with the Mac card's texts).
        """

        def number(value: Any) -> int | None:
            try:
                result = int(value)
            except (TypeError, ValueError):
                return None
            return result if result > 0 else None

        def moment(value: Any) -> float | None:
            try:
                result = float(value)
            except (TypeError, ValueError):
                return None
            return result if math.isfinite(result) and result > 0 else None

        shelves: dict[str, list[dict[str, Any]]] = {"caughtUp": [], "waiting": []}
        seen: set[int] = set()
        for shelf, names in (
            ("waiting", ("needs_action", "waiting", "download_available")),
            ("caughtUp", ("caught_up",)),
        ):
            for name in names:
                for card in home.get(name) or []:
                    if not isinstance(card, dict) or card.get("kind") == "watch_sequence":
                        continue
                    media_id = number(card.get("media_id"))
                    if media_id is None or media_id in seen:
                        continue
                    seen.add(media_id)
                    schedule = (
                        card.get("personal_schedule")
                        if isinstance(card.get("personal_schedule"), dict)
                        else {}
                    )
                    presentation = (
                        card.get("presentation") if isinstance(card.get("presentation"), dict) else {}
                    )
                    local = card.get("local") if isinstance(card.get("local"), dict) else {}
                    if name == "caught_up":
                        # The Mac's own rule: the personal schedule's next unlock, else AniList's
                        # next airing (_apply_personal_schedule_to_payload put it in next_airing_at).
                        next_episode = number(
                            schedule.get("next_episode")
                            if schedule.get("enabled")
                            else card.get("next_airing_episode")
                        )
                        next_at = moment(card.get("next_airing_at"))
                    else:
                        next_episode = number(local.get("episode")) or number(card.get("next_episode"))
                        next_at = None
                    # Without a presentation the Mac card reads the episode's own state.
                    status = str(presentation.get("status") or local.get("state") or "")
                    if name == "needs_action":
                        status = "needs_action"
                    fraction = None
                    if status in {"downloading", "paused_download", "waiting_download"}:
                        percent = moment(presentation.get("progress_percent"))
                        fraction = min(1.0, percent / 100) if percent is not None else None
                    download = card.get("download") if isinstance(card.get("download"), dict) else None
                    kind = None
                    if download is not None:
                        kind = cls._download_kind(download, torrents_enabled)
                        try:
                            fraction = max(0.0, min(1.0, float(download.get("progress") or 0)))
                        except (TypeError, ValueError):
                            pass
                    extras = card.get("extra_files") if isinstance(card.get("extra_files"), list) else []
                    if not extras and isinstance(card.get("extra_file"), dict):
                        extras = [card["extra_file"]]
                    shelves[shelf].append(
                        {
                            "mediaId": media_id,
                            "title": str(card.get("title") or ""),
                            "progress": number(card.get("progress")) or 0,
                            "total": number(card.get("episodes")),
                            "section": name,
                            "status": status,
                            "nextEpisode": next_episode,
                            "nextAt": next_at,
                            "downloadFraction": fraction,
                            "downloadKind": kind,
                            "queuedRelease": bool(card.get("queued_release")),
                            "actionCode": str(card.get("action_code") or "") or None,
                            "extraKinds": [
                                str(extra.get("kind") or "OVA") for extra in extras if isinstance(extra, dict)
                            ],
                        }
                    )
        return shelves

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
            # Download / stream choices with their sizes (estimates once the file was probed).
            "qualities": self._qualities(episode_id, Path(str(row["video_path"])), wait=False)
            if row.get("video_path")
            else [],
            "subtitleRevision": self._subtitle_revision(row),
            # Raw subtitles shown as published ("couldn't sync": no trustworthy time map).
            "subtitleNotAligned": str(row["state"]) == "couldnt_sync",
            # mpv's z/x delay for this subtitle (ms; + later), shared with the phone's timing
            # wheel; and the detected OP/ED for the phone's Skip button. Older phones ignore both.
            "subtitleDelayMs": self._subtitle_delay_ms(row),
            "skipSegments": self._skip_segments(row),
        }

    @staticmethod
    def _subtitle_source(row: dict[str, Any]) -> str:
        """subtitle_identity of the episode's library subtitle (the one the phone shows)."""
        from .subtitle_delay import subtitle_identity

        return subtitle_identity(row.get("subtitle_path") or None, row.get("embedded_subtitle_id"))

    def _subtitle_delay_ms(self, row: dict[str, Any]) -> int:
        from .subtitle_delay import remembered_subtitle_delay

        source = self._subtitle_source(row)
        db = getattr(getattr(self, "service", None), "db", None)
        if not source or db is None:
            return 0
        try:
            seconds = remembered_subtitle_delay(
                db, Path(str(row["video_path"])).expanduser().resolve(), source=source
            )
        except Exception:  # noqa: BLE001 - the list is shown without it
            return 0
        return round(seconds * 1000) if seconds is not None else 0

    def _skip_segments(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """Detected opening/ending of the file (cache only, never analyses), as mpv's Skip
        button gets them; empty with "Skip opening/ending" off on the Mac."""
        api = getattr(getattr(self, "service", None), "api", None)
        config = getattr(api, "config", None)
        if config is None or not getattr(getattr(config, "playback", None), "skip_segments_enabled", True):
            return []
        cache_dir = getattr(getattr(config, "paths", None), "cache_dir", None)
        if cache_dir is None:
            return []
        from .intro_skipper import segments_for_video

        try:
            segments = segments_for_video(Path(cache_dir), Path(str(row["video_path"])))
        except Exception:  # noqa: BLE001 - skipping is optional
            return []
        return [
            {
                "kind": segment.kind,
                "start": round(segment.start_seconds, 3),
                "end": round(segment.end_seconds, 3),
            }
            for segment in segments
        ]

    def episode_subtitle_delay(self, episode_id: int, value: dict[str, Any]) -> dict[str, Any]:
        """The phone's subtitle timing for the episode (ms, + later): kept as mpv's delay for
        the same subtitle, so the next mpv launch and the other devices start with it."""
        from .subtitle_delay import MAX_SUBTITLE_DELAY_SECONDS, remember_subtitle_delay

        try:
            ms = float(value.get("ms"))
        except (TypeError, ValueError):
            raise MediaError("Subtitle timing must be a number") from None
        if not math.isfinite(ms):
            raise MediaError("Subtitle timing must be a number")
        row = self._episode_row(episode_id, require_file=False)
        source = self._subtitle_source(row)
        if not source:
            raise MediaError("This episode has no Japanese subtitles")
        seconds = max(-MAX_SUBTITLE_DELAY_SECONDS, min(MAX_SUBTITLE_DELAY_SECONDS, ms / 1000))
        remember_subtitle_delay(
            self.service.db,
            Path(str(row["video_path"])).expanduser().resolve(),
            seconds,
            subtitle=row.get("subtitle_path") or None,
            embedded_sid=None if row.get("subtitle_path") else row.get("embedded_subtitle_id"),
            source=source,
        )
        return {"ok": True, "ms": round(seconds * 1000)}

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

    def _episode_row(self, episode_id: int, *, require_file: bool = True) -> dict[str, Any]:
        """The episode's row; ``require_file=False`` for marks about a watch that already
        happened on the phone (the Mac's copy may be gone meanwhile)."""
        with self.service.db.connect() as conn:
            rows = self._rows(
                conn,
                "SELECT e.*,a.title AS anime_title FROM episodes e LEFT JOIN anime a ON a.media_id=e.media_id WHERE e.id=?",
                (episode_id,),
            )
        if not rows or (require_file and not Path(str(rows[0]["video_path"])).is_file()):
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
        with self._video_lock:
            self._count_video_hold(str(path), 1 if active else -1)

    def _count_video_hold(self, key: str, change: int) -> None:
        """Open transfers and streams of a prepared copy (called with ``_video_lock`` held)."""
        count = self._video_streams.get(key, 0) + change
        if count > 0:
            self._video_streams[key] = count
        else:
            self._video_streams.pop(key, None)
        self._video_used[key] = time.time()

    # ---------------------------------------------------------------- streams
    def open_stream(self, episode_id: int, device: str, quality: str = "original") -> dict[str, Any]:
        """A phone plays the episode from the Mac now (Watch): the episode's state as for
        prepare, plus ``stream`` ({id, leaseSeconds}) once its copy is ready. The copy is kept
        until the phone closes the stream or stops renewing it. Not ready yet: the preparation
        runs and the answer has no ``stream`` (the phone asks again when it is ready)."""
        quality = video_quality(quality)
        value = self.prepare_episode(episode_id, quality=quality)
        prepared = (
            self._prepared_video(episode_id, touch=True, quality=quality)
            if value.get("state") == "ready"
            else None
        )
        if prepared is None:
            return value
        self._expire_streams()
        stream_id = uuid.uuid4().hex
        now = time.monotonic()
        stream = {
            "id": stream_id,
            "device": device,
            "episode": episode_id,
            "path": str(prepared["path"]),
            "opened": now,
            "renewed": now,
            "requests": 0,
            "bytes": 0,
        }
        with self._video_lock:
            mine = sorted(
                (item for item in self._streams.values() if item["device"] == device),
                key=lambda item: item["renewed"],
            )
            replaced = mine[: max(0, len(mine) + 1 - _STREAMS_PER_DEVICE)]
            for item in replaced:
                self._streams.pop(item["id"], None)
                self._count_video_hold(item["path"], -1)
            self._streams[stream_id] = stream
            self._count_video_hold(stream["path"], 1)
        for item in replaced:
            self._log_stream_end(item, "replaced")
        self._log(
            "EVENT iphone.video_stream open episode=%s stream=%s size=%s quality=%s",
            episode_id,
            stream_id[:8],
            prepared.get("sizeBytes"),
            quality,
        )
        return {**value, "stream": {"id": stream_id, "leaseSeconds": _STREAM_LEASE_SECONDS}}

    def renew_stream(self, stream_id: str, device: str) -> dict[str, Any]:
        """The phone's player is still open (every 30 s). An unknown or ended stream is refused:
        the phone opens a new one."""
        self._expire_streams()
        if not self._touch_stream(stream_id, device):
            raise MediaError("This stream ended on the Mac")
        return {"ok": True, "leaseSeconds": _STREAM_LEASE_SECONDS}

    def close_stream(self, stream_id: str, device: str) -> dict[str, Any]:
        """The phone's player closed: the copy may go (within the usual limits)."""
        stream = None
        if isinstance(stream_id, str) and _STREAM_ID.fullmatch(stream_id):
            with self._video_lock:
                stream = self._streams.get(stream_id)
                if stream is not None and stream["device"] == device:
                    self._streams.pop(stream_id, None)
                    self._count_video_hold(stream["path"], -1)
                else:
                    stream = None
        if stream is not None:
            self._log_stream_end(stream, "closed")
        self._expire_streams()
        return {"ok": True}

    def stream_used(self, stream_id: str, device: str, sent: int, *, cut: bool = False) -> bool:
        """A byte range of the stream went to the phone (renews it). False: no such stream."""
        return self._touch_stream(stream_id, device, sent=sent, cut=cut)

    def _touch_stream(
        self, stream_id: str, device: str, *, sent: int | None = None, cut: bool = False
    ) -> bool:
        if not isinstance(stream_id, str) or not _STREAM_ID.fullmatch(stream_id):
            return False
        with self._video_lock:
            stream = self._streams.get(stream_id)
            if stream is None or stream["device"] != device:
                return False
            stream["renewed"] = time.monotonic()
            self._video_used[stream["path"]] = time.time()
            if sent is not None:
                stream["requests"] += 1
                stream["bytes"] += max(0, int(sent))
                stream["cuts"] = stream.get("cuts", 0) + (1 if cut else 0)
        return True

    def _expire_streams(self) -> None:
        """Streams not renewed within the lease (the phone went away) or of a revoked device end."""
        authorized = getattr(self.service, "still_authorized", None)
        with self._video_lock:
            devices = {stream["device"] for stream in self._streams.values()}
        revoked = {device for device in devices if callable(authorized) and not authorized(device)}
        now = time.monotonic()
        released: list[tuple[dict[str, Any], str]] = []
        with self._video_lock:
            for stream in list(self._streams.values()):
                if stream["device"] in revoked:
                    reason = "revoked"
                elif now - stream["renewed"] > _STREAM_LEASE_SECONDS:
                    reason = "expired"
                else:
                    continue
                self._streams.pop(stream["id"], None)
                self._count_video_hold(stream["path"], -1)
                released.append((stream, reason))
        for stream, reason in released:
            self._log_stream_end(stream, reason)

    def _log_stream_end(self, stream: dict[str, Any], reason: str) -> None:
        self._log(
            "EVENT iphone.video_stream end episode=%s stream=%s reason=%s seconds=%.0f requests=%s bytes=%s cuts=%s",
            stream["episode"],
            stream["id"][:8],
            reason,
            time.monotonic() - stream["opened"],
            stream["requests"],
            stream["bytes"],
            stream.get("cuts", 0),
        )

    @staticmethod
    def _job_key(episode_id: int, quality: str) -> Any:
        """The preparation's key: the episode for the original (as before), else (episode, quality)."""
        return episode_id if quality == "original" else (episode_id, quality)

    @staticmethod
    def _prepared_key(episode_id: int, quality: str) -> str:
        return f"video:{episode_id}" if quality == "original" else f"video:{episode_id}:{quality}"

    def _prepared_video(
        self, episode_id: int, *, touch: bool = False, quality: str = "original"
    ) -> dict[str, Any] | None:
        try:
            row = self._episode_row(episode_id)
        except MediaError:
            return None
        source = Path(str(row["video_path"]))
        try:
            stat = source.stat()
        except OSError:  # deleted since (cleanup of a watched file): no copy, the list goes on
            return None
        with self.service.db.connect() as conn:
            cached = conn.execute(
                "SELECT size,mtime_ns,result FROM iphone_prepared WHERE source=?",
                (self._prepared_key(episode_id, video_quality(quality)),),
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

    def _facts(self, source: Path, *, wait: bool) -> dict[str, Any] | None:
        """Duration, bit rate and whether the original copies the video, of an episode file
        (ffprobe once per file version). ``wait=False``: only when already known; the probe
        then runs in the background (the episode list must not wait for ffprobe)."""
        try:
            stat = source.stat()
        except OSError:
            return None
        key = f"{source}:{stat.st_size}:{stat.st_mtime_ns}"
        known = self._source_facts.get(key)
        if known is not None or not wait:
            if known is None:
                with self.lock:
                    self._facts_waiting.setdefault(key, source)
                    start = not self._facts_worker
                    self._facts_worker = True
                if start:
                    threading.Thread(
                        target=self._probe_waiting, name="iphone-video-probe", daemon=True
                    ).start()
            return known or None
        return self._probe_facts(source, key)

    def _probe_waiting(self) -> None:
        """The files the episode list named without known sizes, one after the other."""
        while True:
            with self.lock:
                if not self._facts_waiting:
                    self._facts_worker = False
                    return
                key, source = next(iter(self._facts_waiting.items()))
                self._facts_waiting.pop(key, None)
            if key not in self._source_facts:
                self._probe_facts(source, key)

    def _probe_facts(self, source: Path, key: str) -> dict[str, Any] | None:
        try:
            _ffmpeg, ffprobe = self._tools()
            probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name,pix_fmt:format=duration,bit_rate",
                    "-of",
                    "json",
                    str(source),
                ],
                capture_output=True,
                text=True,
                timeout=20,
                check=True,
            )
            info = json.loads(probe.stdout or "{}")
            fmt = info.get("format") or {}
            video = next(iter(info.get("streams") or []), {}) or {}
            facts = {
                "duration": float(fmt.get("duration") or 0),
                "kbps": int(float(fmt.get("bit_rate") or 0) / 1000),
                "copies": _copies_video(
                    str(video.get("codec_name") or ""), str(video.get("pix_fmt") or "yuv420p")
                ),
                "bytes": source.stat().st_size,
            }
        except (OSError, ValueError, subprocess.SubprocessError, TypeError):
            facts = None
        with self.lock:
            if len(self._source_facts) > 2000:
                self._source_facts.clear()
            # A file ffprobe cannot read is not probed again (until it changes): no sizes then.
            self._source_facts[key] = facts or {}
        return facts

    def _qualities(self, episode_id: int, source: Path, *, wait: bool) -> list[dict[str, Any]]:
        """Each copy the phone may download or stream, with its size: exact once prepared, else
        estimated from bit rate × duration (``sizeBytes`` absent when not known yet)."""
        facts = self._facts(source, wait=wait)
        result = []
        for quality in VIDEO_QUALITIES:
            prepared = self._prepared_video(episode_id, quality=quality)
            option: dict[str, Any] = {"id": quality, "prepared": prepared is not None}
            if prepared is not None:
                option["sizeBytes"] = int(prepared["sizeBytes"])
            elif facts is not None:
                estimate = video_quality_bytes(
                    quality,
                    facts["duration"],
                    source_bytes=facts["bytes"],
                    copies_video=facts["copies"],
                    source_kbps=facts["kbps"],
                )
                if estimate:
                    option["sizeBytes"] = estimate
                    option["estimated"] = True
            result.append(option)
        return result

    def episode(self, episode_id: int, quality: str = "original") -> dict[str, Any]:
        quality = video_quality(quality)
        row = self._episode_row(episode_id)
        # A phone polls this while it waits to download the episode: keep its copy.
        prepared = self._prepared_video(episode_id, touch=True, quality=quality)
        job = dict(self.video_jobs.get(self._job_key(episode_id, quality)) or {})
        value: dict[str, Any] = {
            "id": episode_id,
            "title": str(row["anime_title"] or row["title"] or ""),
            "number": int(row["media_episode"] or row["episode"] or 0),
            # A job that finished once but whose copy is gone (pruned, the file replaced): not
            # "ready" without a video, prepared again on the next request.
            "state": "ready" if prepared else _job_state_without_copy(job),
            "progress": float(job.get("progress") or 0),
            "message": str(job.get("message") or ""),
            "quality": quality,
            "qualities": self._qualities(episode_id, Path(str(row["video_path"])), wait=True),
        }
        if prepared:
            value["video"] = {k: prepared[k] for k in ("resourceId", "sha256", "sizeBytes", "fileName")}
            value["durationMs"] = int(prepared.get("durationMs") or 0)
        return value

    def prepare_episode(self, episode_id: int, quality: str = "original") -> dict[str, Any]:
        quality = video_quality(quality)
        row = self._episode_row(episode_id)
        # Downloaded or streamed: the final episode's next entry is ready when it ends.
        self._warm_finale(episode_id, row)
        key = self._job_key(episode_id, quality)
        with self.lock:
            job = self.video_jobs.get(key)
            # Missing (pruned, or the Mac restarted mid-way): prepared again on demand.
            if self._prepared_video(episode_id, touch=True, quality=quality) is None and not (
                job and job.get("state") == "preparing"
            ):
                self.video_jobs[key] = {"state": "preparing", "progress": 0.0, "message": ""}
                threading.Thread(
                    target=self._prepare_worker,
                    # The original's call as before (one argument).
                    args=(episode_id,) if quality == "original" else (episode_id, quality),
                    name=f"iphone-video-{episode_id}" + ("" if quality == "original" else f"-{quality}"),
                    daemon=True,
                ).start()
        return self.episode(episode_id, quality=quality)

    def _tools(self) -> tuple[str, str]:
        tools = (
            getattr(getattr(self.service.api, "config", None), "tools", None)
            if getattr(self.service, "api", None)
            else None
        )
        return str(getattr(tools, "ffmpeg", None) or "ffmpeg"), str(
            getattr(tools, "ffprobe", None) or "ffprobe"
        )

    def _prepare_worker(self, episode_id: int, quality: str = "original") -> None:
        started = time.monotonic()
        key = self._job_key(episode_id, quality)
        try:
            row = self._episode_row(episode_id)
            source = Path(str(row["video_path"]))
            stat = source.stat()
            self._log(
                "EVENT iphone.video_prepare start episode=%s source_size=%s video=%r quality=%s",
                episode_id,
                stat.st_size,
                source.name,
                quality,
            )
            ffmpeg, ffprobe = self._tools()
            result = prepare_video(
                source,
                self.service.root / "video",
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                progress=lambda value: self.video_jobs.get(key, {}).update(progress=value),
                quality=quality,
            )
            self._touch_video(result["path"])
            sha = self.service.file_sha256(result["path"])
            try:
                # Tokenized before the copy counts as prepared (the row below): the phone sees
                # "ready" from that row and asks for the subtitles at once; they are then
                # answered from the cache instead of parsed a second time in parallel.
                self.episode_subtitles(episode_id)
            except Exception:  # noqa: BLE001 - subtitles are fetched again by the phone
                pass
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
                self.service.register_resource(conn, resource, sha, stored["sizeBytes"], stored["path"])
                conn.execute(
                    "INSERT INTO iphone_prepared(source,size,mtime_ns,result) VALUES(?,?,?,?) "
                    "ON CONFLICT(source) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,result=excluded.result",
                    (
                        self._prepared_key(episode_id, quality),
                        stat.st_size,
                        stat.st_mtime_ns,
                        json.dumps(stored),
                    ),
                )
            self.video_jobs[key] = {"state": "ready", "progress": 1.0, "message": ""}
            self._log(
                "EVENT iphone.video_prepare done episode=%s size=%s seconds=%.1f quality=%s",
                episode_id,
                stored["sizeBytes"],
                time.monotonic() - started,
                quality,
            )
        except Exception as exc:  # noqa: BLE001 - reported to the phone
            self.video_jobs[key] = {
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
            return
        try:
            # Outside the preparation: a copy that could not be pruned (an external volume, a
            # permission) never turns this finished preparation into "failed".
            self._prune_videos()
        except Exception as exc:  # noqa: BLE001
            self._log("FAIL step=iphone.video_prune error=%r", str(exc)[:200])

    def _prune_videos(self) -> None:
        """Keep the prepared copies within the size budget, least recently used first.

        A copy a phone asked for recently or is downloading now is never removed (a phone
        queue of a whole season prepares one episode after the other); a removed copy is
        prepared again when a phone asks for it.
        """
        folder = self.service.root / "video"
        # A phone that went away while streaming no longer holds its copy.
        self._expire_streams()
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
                # Checked and removed under one lock: a phone that asks for the copy or starts a
                # transfer or stream meanwhile keeps it (its time read again here).
                used = max(used, self._video_used.get(str(path), 0.0))
                if str(path) in self._video_streams or now - used < _VIDEO_PIN_SECONDS:
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
            with _embedded_subtitle_lock(target):
                if not target.is_file():
                    _extract_embedded_subtitle(ffmpeg, video, int(selection.embedded_stream_index), target)
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
        atomic_write_json(cache, value, ensure_ascii=False, separators=(",", ":"))
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

    # A watched mark arriving again this soon after the first is the phone retrying.
    WATCH_RETRY_WINDOW_SECONDS = 24 * 3600.0

    def _watch_just_counted(self, row: dict[str, Any]) -> bool:
        if str(row.get("state") or "") != "watched":
            return False
        try:
            watched_at = float(row.get("watched_at") or 0)
        except (TypeError, ValueError):
            watched_at = 0.0
        if watched_at and time.time() - watched_at < self.WATCH_RETRY_WINDOW_SECONDS:
            return True
        media_id = int(row.get("media_id") or 0)
        episode = int(row.get("media_episode_end") or 0) or int(
            row.get("media_episode") or row.get("episode") or 0
        )
        if not media_id or not episode:
            return False
        from .anilist_tracking import pending_progress

        return any(
            int(item["media_id"]) == media_id and int(item["episode"]) == episode
            for item in pending_progress(
                getattr(getattr(self.api, "manager", None), "db", None) or self.service.db
            )
        )

    def episode_watched(self, episode_id: int, value: dict[str, Any]) -> dict[str, Any]:
        """An episode watched on the iPhone counts like one watched in mpv.

        The phone reports the seconds it actually played (seeks excluded); the
        Mac applies the same rule as its mpv tracker (65% of the episode, at
        least 3 minutes), then updates AniList, the personal schedule and the
        cleanup queue.
        """
        # The watch happened on the phone: counting it does not need the Mac's
        # copy of the file any more (cleanup, a replaced release, a moved folder).
        row = self._episode_row(episode_id, require_file=False)
        api = self.api
        video = Path(str(row["video_path"]))
        if self._watch_just_counted(row):
            # Counted already (this is the phone retrying a lost answer): adding
            # its seconds again or re-queueing would count the watch twice. A
            # rewatch later on is a new watch and reaches AniList as one.
            return self._with_finale(
                {"ok": True, "outcome": "counted", "message": "", "anilist": "already_counted"}, row
            )
        duration = max(0.0, float(value.get("duration") or row.get("playback_duration") or 0))
        active = max(0.0, float(value.get("activeSeconds") or 0))
        # Parts seen on the Mac (mpv) or another device count too: the union, overlaps once.
        from . import watch_coverage

        active = max(active, watch_coverage.coverage(self.service.db, str(row["video_path"])))
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
        # The phone reports its total for this watch; keep the larger of it and
        # what the Mac has (a retry must not add the same seconds again).
        recorded = float(db.playback_evidence(video).get("active_seconds") or 0.0)
        db.record_playback(video, duration, duration, max(0.0, active - recorded))
        media_id = int(row.get("media_id") or 0)
        episode = last_episode
        result: dict[str, Any] = {"ok": True, "outcome": "counted", "message": ""}
        list_status = None
        if media_id and episode and getattr(config.anilist, "access_token", ""):
            from .anilist_tracking import queue_pending_progress
            from .providers.anilist import AniListClient, AniListError

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
            except AniListError as exc:
                # AniList refuses or is unreachable: the episode is counted here now
                # and sent with the next AniList refresh, as one watched in mpv.
                queue_pending_progress(
                    db, media_id=media_id, episode=episode, total_episodes=total, video=str(video)
                )
                update = {"updated": False, "reason": "queued", "progress": episode}
                result["anilist"] = "queued"
                result["message"] = str(exc)
                logger = getattr(api, "logger", None)
                if logger is not None:
                    logger.warning(
                        "FALLBACK step=iphone.episode_watched media_id=%s episode=%s queued=true error=%r",
                        media_id,
                        episode,
                        str(exc),
                    )
            finally:
                client.close()
            reason = str(update.get("reason") or "")
            list_status = str(update.get("status") or "") or None
            if reason != "queued":
                result["anilist"] = "updated" if update.get("updated") else reason
            if not (update.get("updated") or reason in {"already_at_or_above", "queued"}):
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
        watch_coverage.clear(self.service.db, str(row["video_path"]))
        logger = getattr(api, "logger", None)
        if logger is not None:
            logger.info(
                "EVENT iphone.episode_watched media_id=%s episode=%s active=%.0f duration=%.0f",
                media_id,
                episode,
                active,
                duration,
            )
        return self._with_finale(result, row)

    def _warm_finale(self, episode_id: int, row: dict[str, Any] | None = None) -> None:
        """The title's next entry computed ahead when ``episode_id`` is its final episode
        (checked once per episode every few minutes, iphone_finale.warm)."""
        finale = getattr(self, "finale", None)
        if finale is None or not finale.warm_due(episode_id):
            return
        try:
            finale.warm(row if row is not None else self._episode_row(episode_id, require_file=False))
        except Exception as exc:  # noqa: BLE001 - only a warm-up
            self._log("FAIL step=iphone.finale_warm episode=%s error=%r", episode_id, str(exc)[:200])

    def _with_finale(self, result: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        """A counted watch of the final episode: ``finale`` tells the phone to
        show the score wheel and the next entry (iphone_finale); nothing when there is none."""
        from .iphone_finale import finale

        try:
            number = int(row.get("media_episode_end") or 0) or int(
                row.get("media_episode") or row.get("episode") or 0
            )
            value = finale(self.api.manager.db, int(row.get("media_id") or 0), number)
        except Exception as exc:  # noqa: BLE001 - the watch counts anyway
            self._log("FAIL step=iphone.finale episode=%s error=%r", row.get("id"), str(exc)[:200])
            value = None
        if value is not None:
            result["finale"] = value
        return result

    def episode_position(self, episode_id: int, value: dict[str, Any]) -> dict[str, Any]:
        seconds = max(0.0, float(value.get("seconds") or 0))
        duration = max(0.0, float(value.get("duration") or 0))
        observed = self._observed(value)
        # The parts played there count for watched credit even when the place itself is older
        # than the stored one (sent late after watching offline).
        counted = self._add_watched_parts(episode_id, seconds, duration, value.get("ranges"))
        stale, stored = self._stale_position(
            "SELECT playback_position,playback_updated_at FROM episodes WHERE id=?",
            episode_id,
            observed,
            lambda row: seconds <= float(row[0] or 0),
        )
        if counted:
            # Counted as watched (its place was cleared as for a finished watch).
            try:
                row = self._episode_row(episode_id, require_file=False)
            except MediaError:
                return {"ok": True, "counted": True}
            return self._with_finale({"ok": True, "counted": True}, row)
        if stale:
            return {"ok": True, "kept": "newer"}
        # Played there (a download, offline too): the final episode's next entry is ready.
        self._warm_finale(episode_id)
        # The time it was watched there (as for reading places), never earlier than the place it
        # replaces: with the arrival time, a place sent late kept out a later one of another device.
        now = time.time()
        stamp = now if observed is None else max(stored, min(observed, now))
        from .database import _UI_STATE_BUMP_SQL, _resumable

        with self.service.db.connect() as conn:
            before = conn.execute(
                "SELECT state,playback_position,playback_duration FROM episodes WHERE id=?", (episode_id,)
            ).fetchone()
            conn.execute(
                "UPDATE episodes SET playback_position=?,playback_duration=CASE WHEN ?>0 THEN ? ELSE playback_duration END,"
                "playback_updated_at=? WHERE id=?",
                (seconds, duration, duration, stamp, episode_id),
            )
            # Entering or leaving Continue Watching changes the Mac's Home (as an mpv heartbeat
            # does): a cached Home kept the episode under "ready" and played it from the start.
            if before is not None and _resumable(
                before["state"], before["playback_position"], before["playback_duration"]
            ) != _resumable(before["state"], seconds, duration or before["playback_duration"]):
                conn.execute(_UI_STATE_BUMP_SQL)
        return {"ok": True}

    # A union count AniList refused is tried again after this long (the place is sent every 15 s).
    UNION_RETRY_SECONDS = 600.0

    def _add_watched_parts(self, episode_id: int, seconds: float, duration: float, ranges: Any) -> bool:
        """Adds the parts the phone played to the episode's watched parts (with mpv's); True when
        the union now counts the episode (the end reached on some device), counted like a watch."""
        from . import watch_coverage

        parts = watch_coverage.parse_ranges(ranges)
        if not parts:
            return False
        try:
            row = self._episode_row(episode_id, require_file=False)
        except MediaError:
            return False
        if str(row.get("state") or "") == "watched":
            return False
        duration = duration or float(row.get("playback_duration") or 0)
        stored = watch_coverage.record(
            self.service.db,
            str(row["video_path"]),
            parts,
            duration=duration,
            end_reached=watch_coverage.reaches_end(seconds, parts, duration),
        )
        covered = watch_coverage.covered_seconds(stored["intervals"])
        if (
            not stored["end_reached"]
            or duration <= 0
            or covered + 0.5 < watch_coverage.required_seconds(duration)
        ):
            return False
        attempts = self._union_refused
        if time.time() - attempts.get(episode_id, 0.0) < self.UNION_RETRY_SECONDS:
            # AniList refused a moment ago: not asked again on every periodic send.
            return False
        with self.lock:
            if episode_id in self._union_counting:
                return False  # the same place sent twice at once (backgrounding): counted once
            self._union_counting.add(episode_id)
        try:
            result = self.episode_watched(episode_id, {"duration": duration, "activeSeconds": covered})
        except Exception as exc:  # noqa: BLE001 - the place is still saved; mpv or a retry counts it
            self._log("FAIL step=iphone.watch_union episode=%s error=%r", episode_id, str(exc)[:200])
            attempts[episode_id] = time.time()  # AniList unreachable: not again on every send
            return False
        finally:
            with self.lock:
                self._union_counting.discard(episode_id)
        self._log(
            "EVENT iphone.watch_union episode=%s covered=%.0f duration=%.0f outcome=%s",
            episode_id,
            covered,
            duration,
            result.get("outcome"),
        )
        if not result.get("ok"):
            attempts[episode_id] = time.time()
        return bool(result.get("ok"))

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
        # The library screen asks for every cover; decoding and scaling one again per
        # request is the slow part. Kept by the source file's identity (a replaced cover
        # has another size or time).
        stat = source.stat()
        key = (str(kind), int(item))
        mark = (str(source), int(stat.st_size), int(stat.st_mtime_ns))
        with self.lock:
            cache = self.__dict__.setdefault("_cover_thumbnails", {})
            kept = cache.get(key)
            if kept is not None and kept[0] == mark:
                cache[key] = cache.pop(key)  # most recently used last
                return kept[1], "image/jpeg"
        data = self._thumbnail(source.read_bytes())
        with self.lock:
            cache.pop(key, None)
            cache[key] = (mark, data)
            while len(cache) > self.COVER_CACHE_ITEMS:
                cache.pop(next(iter(cache)))
        return data, "image/jpeg"

    COVER_CACHE_ITEMS = 256

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
            "stateLabel": _state_label(
                card, str(live.get("rawStateLabel") or detail.get("rawStateLabel") or "")
            ),
            "frequencyRank": frequency_rank(detail, reading),
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
            raise MediaTemporaryError(str(result.get("message") or "Jiten states are unavailable"))
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
        answer: dict[str, Any] = {"ok": True, "outcome": "saved", "message": ""}
        row = self._graded_state_row(payload["word_id"], payload["reading_index"])
        if row is not None:
            answer["row"] = row
        return answer

    def _graded_state_row(self, word: int, reading: int) -> list[int] | None:
        """The word's state after a grade or an add, [wordId, readingIndex, state, flags], for the
        phone to recolour every occurrence at once; None when Jiten's answer is not fresh (an old
        cached state would put the pre-review state back)."""
        if word <= 0:
            return None
        try:
            result = self.api.study_states({"pairs": [[word, reading]]}) or {}
        except Exception:  # noqa: BLE001 - the phone asks for the state itself
            return None
        if not result.get("ok", True):
            return None
        for item in result.get("states") or []:
            if not isinstance(item, dict) or item.get("stale"):
                continue
            try:
                pair = [int(item.get("wordId")), int(item.get("readingIndex"))]
            except (TypeError, ValueError):
                continue
            if pair == [word, reading]:
                furigana, marked = _state_sets(self._reader_settings())
                return [*pair, state_code(item), token_flags(item, furigana, marked)]
        return None

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
            # "Auto-bookmark after 20 s" off: the phone sends no automatic read markers.
            "autoBookmarks": bool(get("auto_bookmarks", True)),
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
        for _attempt in range(GATE_RETRY_ATTEMPTS):
            result = self.api.review_gate_review(
                str(row["video_path"]),
                int(value.get("wordId") or 0),
                int(value.get("readingIndex") or 0),
                str(value.get("grade") or ""),
                attempt,
            )
            # Held back by the Mac's Jiten budget (nothing sent): the phone waits for its
            # answer while the same grade goes again after a short wait.
            wait = float(result.get("retry_after") or 0.0) if result.get("outcome") == "retry" else 0.0
            if not 0.0 < wait <= GATE_RETRY_MAX_WAIT_SECONDS:
                break
            time.sleep(wait)
        return self._gate_view(result)

    # ---------------------------------------------------------- stats/planning
    def _mac_idle(self) -> bool:
        """Background rebuilds wait for energy saving, heat and the player in front."""
        api = getattr(self.service, "api", None)
        if api is None:
            return False
        scheduler = getattr(getattr(api, "manager", None), "work_scheduler", None)
        wait = getattr(scheduler, "background_wait_reason", None)
        return not (callable(wait) and wait())

    def _stats_stamp(self) -> str:
        """Changes when anything the phone's Statistics are built from changes (and each day)."""
        from .consumption_statistics import (
            STATISTICS_POLICY_VERSION,
            VOLUME_POLICY_VERSION,
            system_local_timezone_label,
        )

        parts = [
            STATISTICS_POLICY_VERSION,
            VOLUME_POLICY_VERSION,
            time.strftime("%Y-%m-%d"),
            system_local_timezone_label(),
        ]
        with self.service.db.connect() as conn:
            for sql in _STATS_STAMP_QUERIES:
                try:
                    row = conn.execute(sql).fetchone()
                    parts.append("/".join(str(value) for value in tuple(row)))
                except Exception:  # noqa: BLE001 - a table missing in an older database
                    parts.append("-")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]

    def stats(self, period: str) -> dict[str, Any]:
        period = period if period in _STATS_PERIODS else "30d"
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
            "streak": streak(days, time.strftime("%Y-%m-%d")),
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
        # New activity: the Statistics copies are rebuilt once it settles (iphone_screen_cache).
        self.screens.wake()
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
                result: dict[str, Any] = {"page_index": page, "page_id": f"page:{page}"}
                end = value.get("pageEnd")
                if isinstance(end, (int, float)) and not isinstance(end, bool) and int(end) == page + 1:
                    # Two pages side by side (landscape): both were on screen.
                    result["page_ids"] = [f"page:{page}", f"page:{page + 1}"]
                return result

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


def streak(days: list[dict[str, Any]], today: str | None = None) -> int:
    """Consecutive active days ending today (or yesterday when today is still empty).

    Counted by date: "all time" lists only days with activity, so a run that ended a week
    ago used to count as the current streak. ``today`` (ISO date) defaults to the last day listed.
    """
    from datetime import date, timedelta

    active = set()
    for row in days:
        try:
            if float(row.get("seconds") or 0) > 0:
                active.add(date.fromisoformat(str(row["day"])))
        except (KeyError, TypeError, ValueError):
            continue
    listed = [str(row.get("day") or "") for row in days if row.get("day")]
    anchor_text = today or (max(listed) if listed else "")
    if not anchor_text:
        return 0
    cursor = date.fromisoformat(anchor_text)
    if cursor not in active:
        cursor -= timedelta(days=1)
    count = 0
    while cursor in active:
        count += 1
        cursor -= timedelta(days=1)
    return count


# Phone copies of an episode. "original": as before (video copied when the iPhone decodes it,
# else H.264 3.5 Mbit/s). The smaller ones are HEVC re-encodes (VideoToolbox on the Mac: the
# hardware encoder, several times real time) for keeping more episodes on the device: about
# 270 MB / 140 MB per 24 min instead of 1.5 GB. Timing is the source's either way (no seek, no
# offset): the Mac's subtitles fit every copy.
VIDEO_QUALITIES: dict[str, dict[str, int]] = {
    "original": {},
    "smaller": {"height": 720, "videoKbps": 1400, "audioKbps": 96},
    "saver": {"height": 540, "videoKbps": 750, "audioKbps": 64},
}
# Container overhead of an MP4 over its streams' bit rates.
_MP4_OVERHEAD = 1.015
# Bit rates of an original copy that is re-encoded (not H.264 4:2:0 or HEVC).
_ORIGINAL_ENCODE_KBPS = 3500 + 160


def video_quality(value: Any) -> str:
    """A quality the phone asked for; anything unknown (an older phone: nothing) is the original."""
    return value if isinstance(value, str) and value in VIDEO_QUALITIES else "original"


def _copies_video(codec: str, pix_fmt: str) -> bool:
    return (codec == "h264" and (pix_fmt or "yuv420p") == "yuv420p") or codec == "hevc"


def video_quality_bytes(
    quality: str, duration: float, *, source_bytes: int = 0, copies_video: bool = True, source_kbps: int = 0
) -> int | None:
    """Estimated size of the episode's copy in ``quality`` (bit rate × duration); None: not known.

    An original that copies the video is about the source's size; a smaller copy is never
    estimated above it (its bit rate follows a lower source's, see ``_reencode_kbps``)."""
    settings = VIDEO_QUALITIES[video_quality(quality)]
    if not settings:
        if copies_video and source_bytes > 0:
            return int(source_bytes)
        kbps = _ORIGINAL_ENCODE_KBPS
    else:
        kbps = _reencode_kbps(settings["videoKbps"], source_kbps) + settings["audioKbps"]
    if not duration or duration <= 0 or not math.isfinite(duration):
        return None
    estimate = int(kbps * 1000 / 8 * duration * _MP4_OVERHEAD)
    return min(estimate, int(source_bytes)) if source_bytes > 0 and settings else estimate


def _reencode_kbps(target: int, source_kbps: int) -> int:
    """The video bit rate of a smaller copy: the quality's, or 60 % of a source that is already
    lower (a 700 kbit/s source would otherwise come out bigger)."""
    if source_kbps > 0:
        return max(250, min(target, int(source_kbps * 0.6)))
    return target


def _video_key(source: Path, stat: os.stat_result, quality: str) -> str:
    # The original's key is the one of before: copies prepared earlier stay valid.
    suffix = "iphone-mp4-v1" if quality == "original" else f"iphone-mp4-v1:{quality}-v1"
    return hashlib.sha256(
        f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{suffix}".encode()
    ).hexdigest()[:32]


def _smaller_video_options(quality: str, source_kbps: int, *, darwin: bool) -> list[list[str]]:
    """ffmpeg's video options of a smaller copy, best first: VideoToolbox HEVC on the Mac, H.264
    from VideoToolbox when the Mac's GPU has no HEVC encoder; libx265 elsewhere (tests, Linux)."""
    settings = VIDEO_QUALITIES[quality]
    kbps = _reencode_kbps(settings["videoKbps"], source_kbps)
    # Scaled down only (a 480p source stays 480p); the display aspect is kept (scale keeps it
    # through the sample aspect ratio). A keyframe every 4-5 s: a seek lands quickly.
    # The height rounded down to even as well: yuv420p encoders refuse an odd one (a cropped
    # 405p/531p source kept its odd height; -2 only rounds the width).
    common = [
        "-vf",
        f"scale=-2:'2*trunc(min({settings['height']},ih)/2)'",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "120",
    ]
    if darwin:
        return [
            [
                "-c:v",
                "hevc_videotoolbox",
                "-allow_sw",
                "1",
                "-b:v",
                f"{kbps}k",
                "-profile:v",
                "main",
                *common,
                "-tag:v",
                "hvc1",
            ],
            ["-c:v", "h264_videotoolbox", "-b:v", f"{int(kbps * 1.5)}k", *common, "-tag:v", "avc1"],
        ]
    return [
        [
            "-c:v",
            "libx265",
            "-preset",
            "veryfast",
            "-b:v",
            f"{kbps}k",
            "-x265-params",
            "log-level=error",
            *common,
            "-tag:v",
            "hvc1",
        ]
    ]


def prepare_video(
    source: Path,
    folder: Path,
    *,
    ffmpeg: str,
    ffprobe: str,
    progress: Callable[[float], Any] | None = None,
    quality: str = "original",
) -> dict[str, Any]:
    """MP4 that AVPlayer plays offline: video copied when the iPhone decodes it, Japanese audio as AAC.

    ``quality`` "smaller" / "saver" (VIDEO_QUALITIES): a smaller HEVC copy, kept separately."""
    from .subtitles.stt import _select_japanese_audio_stream

    quality = video_quality(quality)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    stat = source.stat()
    target = folder / f"{_video_key(source, stat, quality)}.mp4"
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,pix_fmt:format=duration,bit_rate",
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
    darwin = os.uname().sysname == "Darwin"
    settings = VIDEO_QUALITIES[quality]
    if settings:
        try:
            source_kbps = int(float((info.get("format") or {}).get("bit_rate") or 0) / 1000)
        except (TypeError, ValueError):
            source_kbps = 0
        attempts = _smaller_video_options(quality, source_kbps, darwin=darwin)
    elif _copies_video(codec, str(video.get("pix_fmt") or "yuv420p")):
        attempts = [["-c:v", "copy", "-tag:v", "hvc1" if codec == "hevc" else "avc1"]]
    else:
        encoder = "h264_videotoolbox" if darwin else "libx264"
        attempts = [
            [
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
        ]
    tail: list[str] = []
    if audio is not None:
        if settings:
            tail += ["-c:a", "aac", "-b:a", f"{settings['audioKbps']}k", "-ac", "2"]
        elif audio.get("codec_name") == "aac":
            tail += ["-c:a", "copy"]
        else:
            tail += ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
    temporary = target.with_name(target.stem + ".part.mp4")
    tail += [
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
    for number, options in enumerate(attempts):
        try:
            _run_prepare(command + options + tail, temporary, target, duration, progress)
            break
        except MediaTemporaryError:
            raise
        except MediaError:
            # VideoToolbox without an HEVC encoder (an older Mac): its H.264 next.
            if number == len(attempts) - 1:
                raise
    return {"path": target, "durationMs": round(duration * 1000)}


def _run_prepare(
    command: list[str],
    temporary: Path,
    target: Path,
    duration: float,
    progress: Callable[[float], Any] | None,
) -> None:
    # ffmpeg's messages go to a file: through a second pipe read only after stdout ended, a
    # damaged file's decoder errors (over ~64 KB) blocked ffmpeg and Pudge for good and the
    # episode stayed "preparing". The whole run also has a time limit.
    limit = video_prepare_timeout(duration)
    timed_out = threading.Event()
    with tempfile.TemporaryFile() as messages:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=messages, text=True)

        def stop() -> None:
            timed_out.set()
            process.kill()

        watchdog = threading.Timer(limit, stop)
        watchdog.daemon = True
        watchdog.start()
        try:
            for line in process.stdout or []:
                # out_time_ms: ffmpeg 4's name for the same microseconds.
                if (
                    line.startswith(("out_time_us=", "out_time_ms="))
                    and duration > 0
                    and progress is not None
                ):
                    try:
                        progress(max(0.0, min(0.99, int(line.split("=", 1)[1]) / 1e6 / duration)))
                    except ValueError:
                        pass
            code = process.wait()
            watchdog.cancel()
            if timed_out.is_set():
                raise MediaTemporaryError(
                    f"ffmpeg did not finish preparing the episode within {round(limit / 60)} min"
                )
            if code != 0:
                messages.seek(0, os.SEEK_END)
                messages.seek(max(0, messages.tell() - 4000))
                error = messages.read().decode("utf-8", "replace")[-500:]
                raise MediaError("ffmpeg could not prepare the episode: " + error.strip())
            os.replace(temporary, target)
        finally:
            watchdog.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
            temporary.unlink(missing_ok=True)


def video_prepare_timeout(duration: float) -> float:
    """Longest an episode's preparation may take: a re-encode of slow hardware runs at about
    real time, a copy takes seconds. Unknown duration: six hours."""
    if not duration or duration <= 0 or not math.isfinite(duration):
        return 6 * 3600.0
    return max(30 * 60.0, duration * 3 + 600.0)


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


# JitenMPV / mpv font names (often Windows or Linux fonts) and the Japanese gothic the phone
# draws them with; the phone's Japanese Hiragino families pass through (unknown: Hiragino Sans).
_IOS_GOTHIC = "Hiragino Sans"
_IOS_FONT_NAMES = {
    "gothic": _IOS_GOTHIC,
    "ms gothic": _IOS_GOTHIC,
    "ms pgothic": _IOS_GOTHIC,
    "ms ui gothic": _IOS_GOTHIC,
    "yu gothic": _IOS_GOTHIC,
    "yu gothic ui": _IOS_GOTHIC,
    "meiryo": _IOS_GOTHIC,
    "meiryo ui": _IOS_GOTHIC,
    "noto sans jp": _IOS_GOTHIC,
    "noto sans cjk jp": _IOS_GOTHIC,
    "noto sans cjk": _IOS_GOTHIC,
    "source han sans": _IOS_GOTHIC,
    "source han sans jp": _IOS_GOTHIC,
    "sans-serif": _IOS_GOTHIC,
    "sans": _IOS_GOTHIC,
    "hiragino sans": "Hiragino Sans",
    "hiragino kaku gothic pron": "Hiragino Kaku Gothic ProN",
    "hiragino mincho pron": "Hiragino Mincho ProN",
}


def ios_subtitle_font(name: Any) -> str:
    """The iOS font family for a JitenMPV/mpv font name (unknown names: Hiragino Sans)."""
    wanted = " ".join(str(name or "").split())
    key = wanted.lower()
    return _IOS_FONT_NAMES.get(key, _IOS_GOTHIC)


def _setting(settings: dict[str, Any], snake: str, pascal: str) -> Any:
    """JitenMPV's config.json uses snake_case keys; older notes used PascalCase (fallback)."""
    value = settings.get(snake)
    return settings.get(pascal) if value is None else value


def _row_value(row: dict[str, Any], snake: str) -> Any:
    if row.get(snake) is not None:
        return row.get(snake)
    return row.get("".join(part.capitalize() for part in snake.split("_")))


def subtitle_look(path: Path | None = None) -> dict[str, Any]:
    """Word styles of the user's JitenMPV setup (theme or custom colours), for iPhone subtitles.

    Sizes stay in mpv units (scaled pixels of a 720-line picture): the phone draws fontSize/720
    of the picture height. A custom state row's outline_size is that word's outline; border_size
    (mpv's sub-border-size) outlines the text that is not a word."""
    path = path or jiten_mpv_config_path()
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        source = "jiten-mpv"
    except (OSError, ValueError):
        settings, source = {}, "default"
    if not isinstance(settings, dict):
        settings, source = {}, "default"
    theme = str(_setting(settings, "theme", "Theme") or "Default")
    preset = next((name for name in JITEN_THEMES if name.lower() == theme.lower()), None)
    states = [dict(item) for item in JITEN_THEMES.get(preset or "Default", JITEN_THEMES["Default"])]
    custom = _setting(settings, "custom_theme_colors", "CustomThemeColors")
    if theme.lower() == "custom" and isinstance(custom, dict):
        theme = "Custom"
        rows = {str(key).lower(): value for key, value in custom.items()}
        for index, name in enumerate(_JITEN_STATE_NAMES):
            row = rows.get(name.lower())
            if not isinstance(row, dict):
                continue
            size = _number(_row_value(row, "outline_size"))
            underline_color = _row_value(row, "underline_color")
            shadow = _row_value(row, "shadow_color")
            states[index] = {
                key: value
                for key, value in {
                    "text": str(_row_value(row, "text_color") or "#ffffff"),
                    "outline": str(_row_value(row, "outline_color") or "#000000"),
                    "outlineSize": size if size is not None else 3.0,
                    "opacity": _opacity(_row_value(row, "text_opacity")),
                    "bold": bool(_row_value(row, "bold")),
                    "italic": bool(_row_value(row, "italic")),
                    "underline": bool(_row_value(row, "underline")),
                    "underlineColor": str(underline_color) if underline_color else None,
                    "underlineThickness": _number(_row_value(row, "underline_thickness")),
                    "shadow": str(shadow) if shadow else None,
                }.items()
                if value is not None
            }
    elif preset:
        theme = preset

    def number(snake: str, pascal: str, default: float) -> float:
        value = _number(_setting(settings, snake, pascal))
        return value if value is not None and value >= 0 else default

    border = number("border_size", "BorderSize", 3)
    look: dict[str, Any] = {
        "source": source,
        "theme": theme,
        "fontFamily": ios_subtitle_font(_setting(settings, "font_family", "FontFamily")),
        "fontSize": number("font_size", "FontSize", 48) or 48,
        "borderSize": border,
        "unparsed": _style(size=border),
        "states": states,
    }
    margin = _number(_setting(settings, "subtitle_margin_y", "SubtitleMarginY"))
    if margin is not None:
        look["marginY"] = margin
    alignment = _number(_setting(settings, "subtitle_alignment", "SubtitleAlignment"))
    if alignment is not None:
        look["alignment"] = int(alignment)
    return look


_EMBEDDED_SUBTITLE_LOCKS: dict[str, threading.Lock] = {}
_EMBEDDED_SUBTITLE_LOCKS_GUARD = threading.Lock()


def _embedded_subtitle_lock(target: Path) -> threading.Lock:
    """One extraction per target file: the phone asks for an episode's subtitles while its
    download prepares them, and two ffmpeg runs used to share one .part file (the second
    rename then failed with "No such file or directory")."""
    with _EMBEDDED_SUBTITLE_LOCKS_GUARD:
        return _EMBEDDED_SUBTITLE_LOCKS.setdefault(str(target), threading.Lock())


def _extract_embedded_subtitle(ffmpeg: str, video: Path, stream_index: int, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}.{os.getpid()}-{uuid.uuid4().hex[:8]}.part.srt")
    try:
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
                f"0:{stream_index}",
                "-f",
                "srt",
                str(temporary),
            ],
            check=True,
            capture_output=True,
            timeout=300,
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
