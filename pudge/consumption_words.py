"""Words met while watching and reading, for Statistics.

The parse of each piece of text consumed is kept as a *word unit*:

* ``anime:<media_uuid>`` – the episode's prepared subtitles: cues with their time and words;
* ``manga:<media_uuid>:<page_id>`` – the OCR text boxes of one manga page;
* ``vn:<text hash>`` – one line captured from a visual novel;
* ``ln:<chapter text hash>`` – a light-novel chapter (its words live in the LN parse cache;
  the row only records that the background job parsed it).

Statistics only reads units (fast, never parses). Units it needs and does not have are
queued as ``pending``; a background job (web_app) fills them with the app's Jiten parsing
and its caches, slowly and only when background work is allowed. A unit without text
(no subtitles, no OCR yet, Jiten unavailable) is ``missing`` and tried again later.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from typing import Any, Iterable

from .consumption_volume import MANGA_DWELL_SECONDS, _merged, _position_range

READY = "ready"
PENDING = "pending"
MISSING = "missing"
MISSING_RETRY_SECONDS = 6 * 3600.0
WORD_KINDS = ("anime", "light_novel", "manga", "visual_novel")

Pair = tuple[int, int]


def vn_line_key(text: str) -> str:
    normalized = " ".join(str(text or "").split())
    return "vn:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def pairs_of(tokens: Iterable[Any]) -> list[Pair]:
    """(wordId, readingIndex) of parsed tokens (one paragraph's token list, or several)."""
    result: list[Pair] = []
    for token in tokens or []:
        if isinstance(token, list):
            result.extend(pairs_of(token))
            continue
        if not isinstance(token, dict):
            continue
        try:
            pair = (int(token.get("wordId")), int(token.get("readingIndex")))
        except (TypeError, ValueError):
            continue
        if pair[0] > 0 and pair[1] >= 0:
            result.append(pair)
    return result


class WordUnits:
    def __init__(self, database: Any) -> None:
        self.database = database
        self._ready: dict[str, Any] = {}

    # ------------------------------------------------------------------ store
    def lookup(self, keys: Iterable[str]) -> dict[str, tuple[str, Any]]:
        """status and decoded words of known units (cached in memory once ready)."""
        wanted = [key for key in dict.fromkeys(keys) if key]
        result: dict[str, tuple[str, Any]] = {}
        missing = []
        for key in wanted:
            if key in self._ready:
                result[key] = (READY, self._ready[key])
            else:
                missing.append(key)
        with self.database.connect() as conn:
            for start in range(0, len(missing), 400):
                chunk = missing[start : start + 400]
                rows = conn.execute(
                    f"SELECT unit_key,status,words_json FROM consumption_word_units "
                    f"WHERE unit_key IN ({','.join('?' * len(chunk))})",
                    chunk,
                ).fetchall()
                for row in rows:
                    key, status = str(row["unit_key"]), str(row["status"])
                    words: Any = None
                    if status == READY:
                        try:
                            words = json.loads(str(row["words_json"] or "null"))
                        except (TypeError, ValueError):
                            status, words = PENDING, None
                        else:
                            if len(self._ready) > 5000:
                                self._ready.clear()
                            self._ready[key] = words
                    result[key] = (status, words)
        return result

    def enqueue(self, units: dict[str, tuple[str, dict[str, Any]]]) -> int:
        """Queue units not known yet: {key: (kind, source)}. Returns how many were added."""
        if not units:
            return 0
        now = time.time()
        added = 0
        with self.database.connect() as conn:
            for key, (kind, source) in units.items():
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO consumption_word_units(unit_key,kind,status,source_json,updated_at) "
                    "VALUES(?,?,?,?,?)",
                    (key, kind, PENDING, json.dumps(source, ensure_ascii=False), now),
                )
                added += int(cursor.rowcount or 0)
        return added

    def due(self, limit: int = 20, *, now: float | None = None) -> list[dict[str, Any]]:
        """Units for the background job: pending first, then missing ones whose retry time came."""
        current = float(now if now is not None else time.time())
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT unit_key,kind,status,source_json,attempts FROM consumption_word_units "
                "WHERE status=? OR (status=? AND retry_after<=?) "
                "ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END, updated_at LIMIT ?",
                (PENDING, MISSING, current, PENDING, int(limit)),
            ).fetchall()
        result = []
        for row in rows:
            try:
                source = json.loads(str(row["source_json"] or "{}"))
            except (TypeError, ValueError):
                source = {}
            result.append(
                {
                    "key": str(row["unit_key"]),
                    "kind": str(row["kind"]),
                    "status": str(row["status"]),
                    "attempts": int(row["attempts"] or 0),
                    "source": source if isinstance(source, dict) else {},
                }
            )
        return result

    def counts(self) -> dict[str, int]:
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT status,COUNT(*) AS n FROM consumption_word_units GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def store(self, key: str, words: Any) -> None:
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE consumption_word_units SET status=?,words_json=?,reason='',updated_at=? WHERE unit_key=?",
                (READY, json.dumps(words, separators=(",", ":")), time.time(), key),
            )
        self._ready.pop(key, None)

    def mark_missing(self, key: str, reason: str, *, attempts: int = 0) -> None:
        """No text (yet) or Jiten unavailable: try again in 6 h, then less often (up to 30 days)."""
        now = time.time()
        wait = min(MISSING_RETRY_SECONDS * (2 ** max(0, min(int(attempts), 8))), 30 * 86400.0)
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE consumption_word_units SET status=?,reason=?,attempts=attempts+1,retry_after=?,updated_at=? "
                "WHERE unit_key=?",
                (MISSING, str(reason)[:200], now + wait, now, key),
            )

    # ------------------------------------------------------------------ VN lines
    def record_vn_lines(self, session_id: str, lines: Iterable[dict[str, Any]]) -> int:
        """Lines a VN capture session read (the capture keeps only the last ones in memory)."""
        rows = []
        for line in lines or []:
            try:
                rows.append(
                    (
                        str(session_id),
                        int(line["id"]),
                        float(line.get("created_at") or time.time()),
                        str(line.get("text") or ""),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        rows = [row for row in rows if row[0] and row[3].strip()]
        if not rows:
            return 0
        with self.database.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO vn_capture_lines(session_id,line_id,captured_at_utc,text) VALUES(?,?,?,?)",
                rows,
            )
            return int(conn.total_changes - before)

    def vn_lines(
        self,
        *,
        session_ids: Iterable[str] | None = None,
        start: float | None = None,
        end: float | None = None,
    ) -> list[tuple[str, float, str]]:
        conditions, params = [], []
        if start is not None:
            conditions.append("captured_at_utc>=?")
            params.append(float(start))
        if end is not None:
            conditions.append("captured_at_utc<?")
            params.append(float(end))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self.database.connect() as conn:
            rows = conn.execute(
                f"SELECT session_id,captured_at_utc,text FROM vn_capture_lines {where} ORDER BY captured_at_utc",
                params,
            ).fetchall()
        allowed = set(session_ids) if session_ids is not None else None
        return [
            (str(row["session_id"]), float(row["captured_at_utc"]), str(row["text"]))
            for row in rows
            if allowed is None or str(row["session_id"]) in allowed
        ]

    # ------------------------------------------------------------------ history
    def backfill_units(self) -> dict[str, tuple[str, dict[str, Any]]]:
        """Every unit the recorded history refers to (for the background job)."""
        units: dict[str, tuple[str, dict[str, Any]]] = {}
        with self.database.connect() as conn:
            anime = conn.execute(
                "SELECT DISTINCT em.media_uuid FROM consumption_events e "
                "JOIN consumption_event_media em ON em.event_id=e.event_id WHERE e.kind='anime'"
            ).fetchall()
            located = conn.execute(
                "SELECT DISTINCT e.kind,em.media_uuid,"
                "json_extract(em.locator_end_json,'$.page_id') AS page_id,"
                "json_extract(em.locator_end_json,'$.page_index') AS page_index,"
                "json_extract(em.locator_end_json,'$.chapter_key') AS chapter_key "
                "FROM consumption_events e JOIN consumption_event_media em ON em.event_id=e.event_id "
                "WHERE e.kind IN ('manga','light_novel')"
            ).fetchall()
        for row in anime:
            units.update(segment_units({"kind": "anime", "media": [{"media_uuid": row["media_uuid"]}]}))
        for row in located:
            locator = {
                name: row[name] for name in ("page_id", "page_index", "chapter_key") if row[name] is not None
            }
            units.update(
                segment_units(
                    {
                        "kind": str(row["kind"]),
                        "media": [{"media_uuid": row["media_uuid"], "locator_end": locator}],
                    }
                )
            )
        for _session, _at, text in self.vn_lines():
            units[vn_line_key(text)] = ("visual_novel", {"text": text})
        return units


def _locator_page(media: dict[str, Any]) -> tuple[str, int] | None:
    locator = media.get("locator_end") or media.get("locator_start") or {}
    if not isinstance(locator, dict):
        return None
    try:
        index = int(locator.get("page_index"))
    except (TypeError, ValueError):
        return None
    page_id = str(locator.get("page_id") or f"page:{index}")
    return page_id, index


def segment_units(segment: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    kind = str(segment.get("kind") or "")
    units: dict[str, tuple[str, dict[str, Any]]] = {}
    for media in segment.get("media") or []:
        media_uuid = str(media.get("media_uuid") or "")
        if not media_uuid:
            continue
        if kind == "anime":
            units[f"anime:{media_uuid}"] = ("anime", {"media_uuid": media_uuid})
        elif kind == "manga":
            page = _locator_page(media)
            if page is not None:
                units[f"manga:{media_uuid}:{page[0]}"] = (
                    "manga",
                    {"media_uuid": media_uuid, "page_id": page[0], "page_index": page[1]},
                )
        elif kind == "light_novel":
            locator = media.get("locator_end") or {}
            chapter = str(locator.get("chapter_key") or "") if isinstance(locator, dict) else ""
            if chapter and not chapter.startswith("chapter:"):
                units[f"ln:{chapter}"] = ("light_novel", {"chapter_key": chapter})
    return units


class WordTally:
    """Word occurrences per kind plus distinct words, built up from units."""

    def __init__(self) -> None:
        self.total: dict[str, int] = defaultdict(int)
        self.pairs: dict[str, set[Pair]] = defaultdict(set)
        self.pending: set[str] = set()
        self.missing: set[str] = set()

    def add(self, kind: str, pairs: Iterable[Pair]) -> None:
        pairs = list(pairs)
        self.total[kind] += len(pairs)
        self.pairs[kind].update(pairs)

    def unit_state(self, key: str, status: str | None) -> bool:
        """True when the unit can be counted; otherwise remember why not."""
        if status == READY:
            return True
        if status == MISSING:
            self.missing.add(key)
        else:
            self.pending.add(key)
        return False

    def everything(self) -> set[Pair]:
        result: set[Pair] = set()
        for pairs in self.pairs.values():
            result |= pairs
        return result


def _cue_pairs(cues: Any, intervals: list[tuple[float, float]]) -> list[Pair]:
    """Words of the subtitle lines shown during the watched intervals (each line once)."""
    result: list[Pair] = []
    for cue in cues or []:
        try:
            start, end, words = float(cue[0]), float(cue[1]), cue[2]
        except (TypeError, ValueError, IndexError):
            continue
        if any(start < b and end > a for a, b in intervals):
            result.extend((int(w), int(r)) for w, r in words)
    return result


def _anime_intervals(segments: list[dict[str, Any]]) -> dict[tuple[str, str], list[tuple[float, float]]]:
    """Watched episode positions per (session, episode)."""
    rows: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for segment in segments:
        if str(segment.get("kind") or "") != "anime":
            continue
        session = str(segment.get("session_id") or segment.get("event_id") or "")
        for media in segment.get("media") or []:
            media_uuid = str(media.get("media_uuid") or "")
            interval = _position_range(segment, media) if media_uuid else None
            if interval is not None:
                rows[(session, media_uuid)].append(interval)
    return {key: _merged(value) for key, value in rows.items()}


def _manga_pages(segments: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Unit keys of manga pages looked at long enough, per session."""
    dwell: dict[tuple[str, str], float] = defaultdict(float)
    for segment in segments:
        if str(segment.get("kind") or "") != "manga":
            continue
        session = str(segment.get("session_id") or segment.get("event_id") or "")
        seconds = max(0.0, float(segment.get("end") or 0.0) - float(segment.get("start") or 0.0))
        for key in segment_units(segment):
            dwell[(session, key)] += seconds
    result: dict[str, set[str]] = defaultdict(set)
    for (session, key), seconds in dwell.items():
        if seconds >= MANGA_DWELL_SECONDS:
            result[session].add(key)
    return result


def tally_period(
    store: WordUnits,
    segments: list[dict[str, Any]],
    *,
    start: float | None,
    end: float | None,
) -> tuple[WordTally, dict[str, tuple[str, dict[str, Any]]]]:
    """Anime, manga and VN words of the period: each line/page once per session.

    Returns the tally and the units it needed but did not have (to queue)."""
    tally = WordTally()
    anime = _anime_intervals(segments)
    manga = _manga_pages(segments)
    vn_sessions = {
        str(segment.get("session_id") or "")
        for segment in segments
        if str(segment.get("kind") or "") == "visual_novel" and segment.get("session_id")
    }
    vn_lines: dict[str, set[str]] = defaultdict(set)
    if vn_sessions:
        for session, _at, text in store.vn_lines(session_ids=vn_sessions, start=start, end=end):
            vn_lines[session].add(" ".join(text.split()))
    needed: dict[str, tuple[str, dict[str, Any]]] = {}
    for _session, media_uuid in anime:
        needed[f"anime:{media_uuid}"] = ("anime", {"media_uuid": media_uuid})
    for segment in segments:
        if str(segment.get("kind") or "") == "manga":
            needed.update(segment_units(segment))
    for texts in vn_lines.values():
        for text in texts:
            needed[vn_line_key(text)] = ("visual_novel", {"text": text})
    known = store.lookup(needed)
    for (_session, media_uuid), intervals in anime.items():
        key = f"anime:{media_uuid}"
        status, words = known.get(key, (None, None))
        if tally.unit_state(key, status):
            tally.add("anime", _cue_pairs((words or {}).get("cues"), intervals))
    for keys in manga.values():
        for key in keys:
            status, words = known.get(key, (None, None))
            if tally.unit_state(key, status):
                tally.add("manga", [tuple(pair) for pair in (words or {}).get("words") or []])
    for texts in vn_lines.values():
        for text in texts:
            key = vn_line_key(text)
            status, words = known.get(key, (None, None))
            if tally.unit_state(key, status):
                tally.add("visual_novel", [tuple(pair) for pair in (words or {}).get("words") or []])
    unknown = {key: value for key, value in needed.items() if key not in known}
    return tally, unknown


def earlier_pairs(store: WordUnits, prior_state: dict[str, Any] | None, *, before: float | None) -> set[Pair]:
    """Distinct anime/manga/VN words met before a period (from the prior volume summary)."""
    if before is None:
        return set()
    result: set[Pair] = set()
    state = prior_state or {}
    timelines: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for key, rows in (state.get("numeric") or {}).items():
        if len(key) == 4 and key[2] == "anime":
            timelines[str(key[0])].extend(rows)
    pages = [
        f"manga:{media_uuid}:{page}"
        for (media_uuid, _revision), visited in (state.get("manga") or {}).items()
        for page in visited
    ]
    texts = {" ".join(text.split()) for _s, _a, text in store.vn_lines(end=before)}
    keys = [f"anime:{media_uuid}" for media_uuid in timelines] + pages + [vn_line_key(text) for text in texts]
    known = store.lookup(keys)
    for media_uuid, intervals in timelines.items():
        status, words = known.get(f"anime:{media_uuid}", (None, None))
        if status == READY:
            result.update(_cue_pairs((words or {}).get("cues"), _merged(intervals)))
    for key in keys[len(timelines) :]:
        status, words = known.get(key, (None, None))
        if status == READY:
            result.update(tuple(pair) for pair in (words or {}).get("words") or [])
    return result
