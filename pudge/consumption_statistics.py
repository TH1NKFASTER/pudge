from __future__ import annotations

import csv
import json
import math
import os
import platform
import time
import uuid
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .consumption import ConsumptionConflictError, ConsumptionLedger, ConsumptionLedgerError
from .database import Database
from .consumption_volume import (
    VOLUME_POLICY_VERSION,
    aggregate_volume,
    dump_prior_volume_state,
    load_prior_volume_state,
    merge_prior_volume_states,
    prior_volume_state,
)
from .consumption_words import MISSING, READY, WORD_KINDS, WordUnits, earlier_pairs, tally_period
from .i18n import err, t

STATISTICS_POLICY_VERSION = "r14-v1"
MIN_AUTOMATIC_SESSION_SECONDS = 60.0
_SHORT_SESSION_METHODS = {
    "reader_visible_heartbeat",
    "audiobook_active_playback",
    "vn_capture_session",
    "companion_observation",
}
_VALID_KINDS = {"anime", "light_novel", "manga", "audiobook", "visual_novel", "review", "manual"}
# History before a report period, summarized up to a local midnight (state table).
VOLUME_ROLLUP_PREFIX = "consumption_volume_rollup:"
_VOLUME_ROLLUPS_KEPT = 6
# Events are at most 30 s long: starting a newer span this much earlier means every
# event crossing the stored boundary is seen whole once (union, so overlap is free).
_ROLLUP_OVERLAP_SECONDS = 120.0


def _local_datetime(timestamp: float) -> datetime:
    # ``astimezone()`` without an explicit zone asks the operating system every
    # time.  That is deliberate: R8 has no report-timezone preference. Changing
    # the computer timezone changes the view, never the stored UTC facts.
    return datetime.fromtimestamp(float(timestamp)).astimezone()


def system_local_timezone_label() -> str:
    configured = str(os.environ.get("TZ") or "").strip()
    if configured and not configured.startswith(":"):
        return configured
    for path in (Path("/etc/localtime"), Path("/var/db/timezone/zoneinfo")):
        try:
            resolved = path.resolve()
            text = str(resolved)
            marker = "/zoneinfo/"
            if marker in text:
                return text.split(marker, 1)[1]
        except OSError:
            pass
    try:
        zone_file = Path("/etc/timezone")
        if zone_file.is_file():
            value = zone_file.read_text(encoding="utf-8").strip()
            if value:
                return value
    except OSError:
        pass
    return str(_local_datetime(time.time()).tzname() or "local")


def _local_day(timestamp: float) -> date:
    return _local_datetime(timestamp).date()


def _local_midnight_epoch(day: date) -> float:
    # A naive datetime is interpreted by ``mktime`` in the computer's current
    # local zone, including historical DST rules. Midnight boundaries therefore
    # naturally produce 23/25-hour days where appropriate.
    return float(time.mktime(datetime.combine(day, datetime.min.time()).timetuple()))


def _format_local(timestamp: float | None) -> str:
    if timestamp is None:
        return ""
    return _local_datetime(float(timestamp)).isoformat(timespec="seconds")


def _finite(value: Any, *, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _normalize_kind(value: str) -> str:
    kind = str(value or "").strip().casefold()
    aliases = {
        "anime_episode": "anime",
        "novel": "light_novel",
        "ln": "light_novel",
        "visualnovel": "visual_novel",
        "vn": "visual_novel",
    }
    return aliases.get(kind, kind or "unknown")


def _merge_intervals(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    rows = [(float(a), float(b)) for a, b in intervals if float(b) > float(a)]
    if len(rows) > 1 and any(rows[index][0] < rows[index - 1][0] for index in range(1, len(rows))):
        rows.sort()
    merged: list[list[float]] = []
    for start, end in rows:
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(row[0], row[1]) for row in merged]


def _union_seconds(intervals: Iterable[tuple[float, float]]) -> float:
    return sum(end - start for start, end in _merge_intervals(intervals))


def _overlap_bucket(segments: list[dict[str, Any]], active: set[int]) -> str:
    kinds = {str(segments[index].get("kind") or "unknown") for index in active}
    if len(kinds) == 1:
        return next(iter(kinds))
    if kinds == {"light_novel", "audiobook"}:
        ln_groups = {
            str(segments[index].get("activity_group_id") or "")
            for index in active
            if str(segments[index].get("kind") or "") == "light_novel"
            and str(segments[index].get("activity_group_id") or "")
        }
        audio_groups = {
            str(segments[index].get("activity_group_id") or "")
            for index in active
            if str(segments[index].get("kind") or "") == "audiobook"
            and str(segments[index].get("activity_group_id") or "")
        }
        if ln_groups & audio_groups:
            return "light_novel+audiobook"
    return "simultaneous"


def _sweep_breakdown(segments: list[dict[str, Any]]) -> tuple[float, dict[str, float]]:
    # Recorder output is normally chronological and non-overlapping. Avoid the
    # O(n log n) sweep in that overwhelmingly common case; overlapping linked
    # LN+audio/multi-device activity still falls through to the exact sweep.
    total = 0.0
    breakdown: dict[str, float] = defaultdict(float)
    previous_end: float | None = None
    simple = True
    valid = 0
    for segment in segments:
        start = float(segment["start"])
        end = float(segment["end"])
        if end <= start:
            continue
        valid += 1
        if previous_end is not None and start < previous_end - 1e-6:
            simple = False
            break
        previous_end = end
    if simple:
        for segment in segments:
            start = float(segment["start"])
            end = float(segment["end"])
            if end <= start:
                continue
            seconds = end - start
            bucket = str(segment.get("kind") or "unknown")
            total += seconds
            breakdown[bucket] += seconds
        return total, dict(breakdown)
    if valid == 0:
        return 0.0, {}

    points: dict[float, list[tuple[int, int]]] = defaultdict(list)
    for index, segment in enumerate(segments):
        start = float(segment["start"])
        end = float(segment["end"])
        if end <= start:
            continue
        points[start].append((index, 1))
        points[end].append((index, -1))
    active: set[int] = set()
    total = 0.0
    breakdown = defaultdict(float)
    previous: float | None = None
    for point in sorted(points):
        if previous is not None and point > previous and active:
            seconds = point - previous
            bucket = _overlap_bucket(segments, active)
            total += seconds
            breakdown[bucket] += seconds
        for index, delta in points[point]:
            if delta > 0:
                active.add(index)
            else:
                active.discard(index)
        previous = point
    return total, dict(breakdown)


def _split_by_local_day(segment: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    start = float(segment["start"])
    end = float(segment["end"])
    result: list[tuple[str, dict[str, Any]]] = []
    cursor = start
    guard = 0
    while cursor < end - 1e-6 and guard < 3700:
        day = _local_day(cursor)
        next_midnight = _local_midnight_epoch(day + timedelta(days=1))
        if next_midnight <= cursor + 1e-6:
            next_midnight = cursor + 3600.0
        piece_end = min(end, next_midnight)
        piece = dict(segment)
        piece["start"] = cursor
        piece["end"] = piece_end
        result.append((day.isoformat(), piece))
        cursor = piece_end
        guard += 1
    return result


def _segments_by_local_day(segments: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    if not segments:
        return {}
    first_timestamp = min(float(row["start"]) for row in segments)
    last_timestamp = max(float(row["end"]) for row in segments)
    first_day = _local_day(first_timestamp)
    last_day = _local_day(max(first_timestamp, last_timestamp - 1e-6))
    span = max(0, (last_day - first_day).days)
    days = [first_day + timedelta(days=offset) for offset in range(span + 1)]
    # One extra boundary closes the last day. These calls are O(number of days),
    # not O(number of events), and therefore keep historical DST semantics.
    boundaries = [_local_midnight_epoch(day) for day in days]
    boundaries.append(_local_midnight_epoch(last_day + timedelta(days=1)))
    labels = [day.isoformat() for day in days]
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for segment in segments:
        start = float(segment["start"])
        end = float(segment["end"])
        if end <= start:
            continue
        index = max(0, min(len(labels) - 1, bisect_right(boundaries, start) - 1))
        cursor = start
        while cursor < end - 1e-6 and index < len(labels):
            piece_end = min(end, boundaries[index + 1])
            if cursor == start and piece_end == end:
                piece = segment
            else:
                piece = dict(segment)
                piece["start"] = cursor
                piece["end"] = piece_end
            result[labels[index]].append(piece)
            cursor = piece_end
            index += 1
    return dict(result)


def _manual_title(value: object) -> str:
    """Stored titles of manual entries are English; show them in the interface language."""
    title = str(value or "")
    return t("web.manualEntry") if not title or title == t("web.manualEntry", lang="en") else title


def _ln_chapter_ranges(numeric: dict[tuple[str, ...], list[tuple[float, float]]]) -> dict[str, list]:
    """Merged character ranges read per LN chapter (the reader's chapter key is its text hash)."""
    ranges: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for key, rows in numeric.items():
        if len(key) == 4 and key[2] == "light_novel" and not str(key[3]).startswith("chapter:"):
            ranges[str(key[3])].extend(rows)
    return {chapter: _merge_intervals(rows) for chapter, rows in ranges.items()}


def _words_in_ranges(
    positions: list[tuple[int, int, int]], ranges: list[tuple[float, float]]
) -> tuple[int, set[tuple[int, int]]]:
    offsets = [row[0] for row in positions]
    total = 0
    pairs: set[tuple[int, int]] = set()
    for start, end in ranges:
        first = bisect_right(offsets, start - 1e-9)
        last = bisect_right(offsets, end - 1e-9)
        total += max(0, last - first)
        pairs.update((row[1], row[2]) for row in positions[first:last])
    return total, pairs


def _longest_streak(days: list[dict[str, Any]]) -> int:
    active = sorted(date.fromisoformat(str(row["day"])) for row in days if float(row.get("seconds") or 0) > 0)
    best = run = 0
    previous: date | None = None
    for day in active:
        run = run + 1 if previous is not None and (day - previous).days == 1 else 1
        best = max(best, run)
        previous = day
    return best


class ConsumptionStatistics:
    def __init__(
        self,
        database: Database,
        *,
        logger: Any = None,
        ln_words: Any = None,
        count_words: bool | None = None,
        on_words_pending: Any = None,
    ) -> None:
        self.database = database
        self.logger = logger
        self.ledger = ConsumptionLedger(database, logger=logger)
        self.word_units = WordUnits(database)
        # Word stats need parsed text; the app passes its LN word source and a way to wake
        # the background job that parses the rest (anime subtitles, manga OCR, VN lines).
        self.count_words = bool(ln_words is not None if count_words is None else count_words)
        self.on_words_pending = on_words_pending
        # ln_words(text_hash) -> [(character offset, wordId, readingIndex)] or None:
        # the words of a light-novel chapter from its cached parse.
        self.ln_words = ln_words
        self._ln_word_cache: dict[str, list[tuple[int, int, int]]] = {}

    def _chapter_words(self, chapter: str) -> list[tuple[int, int, int]] | None:
        cached = self._ln_word_cache.get(chapter)
        if cached is not None:
            return cached
        try:
            positions = self.ln_words(chapter) if self.ln_words is not None else None
        except Exception as exc:  # noqa: BLE001 - a chapter that cannot be read only lacks a word count
            if self.logger is not None:
                self.logger.warning("SKIP step=statistics.ln_words error=%r", exc)
            positions = None
        if positions is None:
            return None
        if len(self._ln_word_cache) > 400:
            self._ln_word_cache.clear()
        self._ln_word_cache[chapter] = list(positions)
        return self._ln_word_cache[chapter]

    def _words(
        self,
        segments: list[dict[str, Any]],
        normalized: dict[str, Any],
        prior_state: dict[str, Any] | None,
        *,
        now: float | None,
    ) -> dict[str, Any] | None:
        """Words met in the period, all kinds and per kind (estimates).

        Anime: the subtitle lines shown while watching, each line once per session. Manga:
        the text boxes of the pages looked at, once per session. Visual novels: the lines
        the capture read. Light novels: the words in the character ranges read in the
        period (a re-read passage counts once). "New" words were not met in any earlier
        watching or reading. Only parses already made are read; what is still to parse is
        queued for the background job and reported as pending."""
        if not self.count_words:
            return None
        tally, unknown = tally_period(
            self.word_units, segments, start=normalized["start_utc"], end=normalized["end_utc"]
        )
        selected_chapters = (
            _ln_chapter_ranges(prior_volume_state(self.database, segments)["numeric"])
            if self.ln_words is not None
            else {}
        )
        if not (tally.total or tally.pending or tally.missing or selected_chapters):
            return None
        earlier_state = prior_state
        if normalized["kind"] != "all" or normalized["media_uuid"]:
            # "New words" are new to all earlier watching and reading, not only the filtered work.
            earlier_state = None
            if normalized["start_utc"] is not None and float(normalized["start_utc"]) > 0:
                everything = {**normalized, "kind": "all", "media_uuid": ""}
                earlier_state = self._prior_volume(everything, now=now)
        known = self.word_units.lookup(f"ln:{chapter}" for chapter in selected_chapters)
        for chapter, ranges in selected_chapters.items():
            positions = self._chapter_words(chapter)
            key = f"ln:{chapter}"
            if positions is None:
                status = known.get(key, (None, None))[0]
                if status == MISSING or status == READY:
                    tally.missing.add(key)
                else:
                    tally.pending.add(key)
                    unknown.setdefault(key, ("light_novel", {"chapter_key": chapter}))
                continue
            words, pairs = _words_in_ranges(positions, ranges)
            tally.total["light_novel"] += words
            tally.pairs["light_novel"] |= pairs
        if unknown:
            self.word_units.enqueue(unknown)
        if tally.pending and self.on_words_pending is not None:
            try:
                self.on_words_pending()
            except Exception as exc:  # noqa: BLE001 - counting continues on the next look
                if self.logger is not None:
                    self.logger.warning("SKIP step=statistics.words_job error=%r", exc)
        start = normalized["start_utc"]
        earlier: set[tuple[int, int]] = set()
        if start is not None and float(start) > 0:
            earlier = earlier_pairs(self.word_units, earlier_state, before=float(start))
            for chapter, ranges in _ln_chapter_ranges((earlier_state or {}).get("numeric") or {}).items():
                positions = self._chapter_words(chapter)
                if positions is not None:
                    earlier |= _words_in_ranges(positions, ranges)[1]
        kinds = [kind for kind in WORD_KINDS if kind in tally.total or kind in tally.pairs]
        counted = bool(kinds)
        everything = tally.everything()
        return {
            "total": sum(tally.total.values()) if counted else None,
            "unique": len(everything) if counted else None,
            "new": len(everything - earlier) if counted else None,
            "by_kind": {
                kind: {
                    "total": tally.total.get(kind, 0),
                    "unique": len(tally.pairs.get(kind, set())),
                    "new": len(tally.pairs.get(kind, set()) - earlier),
                }
                for kind in kinds
            },
            "pending": len(tally.pending),
            "missing": len(tally.missing),
        }

    @staticmethod
    def _highlights(
        days: list[dict[str, Any]],
        journal: list[dict[str, Any]],
        works: list[dict[str, Any]],
        breakdown: dict[str, float],
        volume: dict[str, Any],
        words: dict[str, Any] | None,
    ) -> dict[str, Any]:
        sessions = [row for row in journal if not row.get("excluded") and float(row.get("seconds") or 0) > 0]
        longest = max(sessions, key=lambda row: float(row["seconds"]), default=None)
        busiest = max(days, key=lambda row: float(row.get("seconds") or 0), default=None)
        if busiest is not None and float(busiest.get("seconds") or 0) <= 0:
            busiest = None
        by_kind = volume.get("by_kind") or {}

        def rate(kind: str, seconds: float, per: float) -> float | None:
            row = by_kind.get(kind) or {}
            amount = row.get("consumed")
            if not row.get("exact") or amount is None or float(amount) <= 0 or seconds < 60:
                return None
            return float(amount) / seconds * per

        ln_seconds = float(breakdown.get("light_novel", 0.0)) + float(
            breakdown.get("light_novel+audiobook", 0.0)
        )
        episodes = {
            str(media_uuid)
            for media_uuid, row in (volume.get("by_media") or {}).items()
            if str(row.get("kind")) == "anime" and float(row.get("consumed") or 0) > 0
        }
        return {
            "longest_streak_days": _longest_streak(days),
            "busiest_day": {"day": busiest["day"], "seconds": float(busiest["seconds"])} if busiest else None,
            "longest_session": {
                "seconds": float(longest["seconds"]),
                "title": str(longest.get("title") or ""),
                "kind": str(longest.get("kind") or ""),
                "start_utc": longest.get("start_utc"),
            }
            if longest
            else None,
            "top_work": {"title": works[0]["title"], "kind": works[0]["kind"], "seconds": works[0]["seconds"]}
            if works
            else None,
            "anime_episodes": len(episodes),
            "ln_characters_per_minute": rate("light_novel", ln_seconds, 60.0),
            "manga_pages_per_hour": rate("manga", float(breakdown.get("manga", 0.0)), 3600.0),
            "words": words,
        }

    def _device_name(self, device_id: str) -> str:
        with self.database.connect() as conn:
            row = conn.execute(
                "SELECT device_name FROM consumption_devices WHERE device_id=?", (str(device_id),)
            ).fetchone()
        if row is not None and str(row["device_name"] or "").strip():
            return str(row["device_name"])
        return str(device_id)[:8]

    def _scope(self, scope: dict[str, Any] | None, *, now: float | None = None) -> dict[str, Any]:
        raw = dict(scope or {})
        current = float(now if now is not None else time.time())
        period = str(raw.get("period") or "30d").strip().casefold()
        today = _local_day(current)
        start: float | None
        end: float | None = current
        if period == "7d":
            start = _local_midnight_epoch(today - timedelta(days=6))
        elif period == "30d":
            start = _local_midnight_epoch(today - timedelta(days=29))
        elif period == "year":
            start = _local_midnight_epoch(date(today.year, 1, 1))
        elif period == "all":
            start = None
        elif period == "custom":
            try:
                start_day = date.fromisoformat(str(raw.get("start_date") or ""))
                end_day = date.fromisoformat(str(raw.get("end_date") or raw.get("start_date") or ""))
            except ValueError as exc:
                raise ConsumptionLedgerError(err("consumption.invalidPeriod")) from exc
            if end_day < start_day:
                raise ConsumptionLedgerError(err("consumption.endBeforeStart"))
            start = _local_midnight_epoch(start_day)
            end = min(current, _local_midnight_epoch(end_day + timedelta(days=1)))
        else:
            period = "30d"
            start = _local_midnight_epoch(today - timedelta(days=29))
        kind = _normalize_kind(str(raw.get("kind") or "all"))
        if kind == "unknown" or str(raw.get("kind") or "all").casefold() == "all":
            kind = "all"
        media_uuid = str(raw.get("media_uuid") or "").strip()
        return {
            "period": period,
            "start_utc": start,
            "end_utc": end,
            "start_date": str(raw.get("start_date") or ""),
            "end_date": str(raw.get("end_date") or ""),
            "kind": kind,
            "media_uuid": media_uuid,
        }

    def _latest_corrections(self, conn: Any) -> dict[tuple[str, str], dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT c.* FROM consumption_corrections c
            JOIN (
              SELECT target_type,target_id,MAX(revision) AS revision
              FROM consumption_corrections GROUP BY target_type,target_id
            ) latest ON latest.target_type=c.target_type
                 AND latest.target_id=c.target_id AND latest.revision=c.revision
            """
        ).fetchall()
        return {(str(row["target_type"]), str(row["target_id"])): dict(row) for row in rows}

    def _raw_events(
        self, normalized: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
        start = normalized["start_utc"]
        end = normalized["end_utc"]
        conditions = ["e.interval_start_utc < ?"]
        params: list[Any] = [float(end)]
        if start is not None:
            conditions.append("e.interval_end_utc > ?")
            params.append(float(start))
        if normalized["kind"] != "all":
            requested_kind = normalized["kind"]
            if requested_kind in {"light_novel", "audiobook"}:
                # A format filter keeps the linked half of an LN+audio activity,
                # but does not pull in unrelated simultaneous playback.
                conditions.append(
                    "(e.kind=? OR (e.kind IN ('light_novel','audiobook') "
                    "AND s.activity_group_id<>'' AND EXISTS("
                    "SELECT 1 FROM consumption_sessions sx "
                    "WHERE sx.activity_group_id=s.activity_group_id AND sx.kind=?)))"
                )
                params.extend([requested_kind, requested_kind])
            else:
                conditions.append("e.kind=?")
                params.append(requested_kind)
        if normalized["media_uuid"]:
            # Work filters also keep explicitly linked companion sessions.
            conditions.append(
                "(EXISTS(SELECT 1 FROM consumption_event_media fx WHERE fx.event_id=e.event_id AND fx.media_uuid=?) "
                "OR (s.activity_group_id<>'' AND EXISTS("
                "SELECT 1 FROM consumption_sessions sx "
                "WHERE sx.activity_group_id=s.activity_group_id AND sx.primary_media_uuid=?)))"
            )
            params.extend([normalized["media_uuid"], normalized["media_uuid"]])
        where = " AND ".join(conditions)
        with self.database.connect() as conn:
            corrections = self._latest_corrections(conn)
            # Pull event + media links in one ordered scan. The previous R8 draft
            # queried the same event scope twice, which became visible at 100k
            # events. Multi-media events are reconstructed from duplicate join
            # rows without changing ledger semantics.
            rows = conn.execute(
                f"""
                SELECT e.event_id,e.device_id,e.session_id,e.kind,
                       e.interval_start_utc,e.interval_end_utc,e.elapsed_monotonic_ms,e.payload_json,
                       s.primary_media_uuid,s.activity_group_id,s.origin,s.measurement_method,d.device_name,
                       em.media_uuid AS linked_media_uuid,em.role AS linked_media_role,
                       em.source_revision AS linked_source_revision,
                       em.locator_start_json AS linked_locator_start_json,
                       em.locator_end_json AS linked_locator_end_json,
                       m.kind AS linked_media_kind,m.title_snapshot AS linked_title_snapshot
                FROM consumption_events e
                JOIN consumption_sessions s ON s.session_id=e.session_id
                LEFT JOIN consumption_devices d ON d.device_id=e.device_id
                LEFT JOIN consumption_event_media em ON em.event_id=e.event_id
                LEFT JOIN consumption_media m ON m.media_uuid=em.media_uuid
                WHERE {where}
                ORDER BY e.interval_start_utc,e.event_id,
                         CASE em.role WHEN 'primary' THEN 0 ELSE 1 END,m.title_snapshot
                """,
                tuple(params),
            ).fetchall()
            sessions: dict[str, dict[str, Any]] = {}
            payload: list[dict[str, Any]] = []
            current_event_id = ""
            current: dict[str, Any] | None = None
            for raw in rows:
                event_id = str(raw["event_id"])
                if event_id != current_event_id:
                    current = {
                        "event_id": event_id,
                        "device_id": str(raw["device_id"] or ""),
                        "session_id": str(raw["session_id"]),
                        "kind": str(raw["kind"] or "unknown"),
                        "interval_start_utc": float(raw["interval_start_utc"]),
                        "interval_end_utc": float(raw["interval_end_utc"]),
                        "elapsed_monotonic_ms": float(raw["elapsed_monotonic_ms"] or 0.0),
                        "primary_media_uuid": str(raw["primary_media_uuid"] or ""),
                        "activity_group_id": str(raw["activity_group_id"] or ""),
                        "origin": str(raw["origin"] or "automatic"),
                        "measurement_method": str(raw["measurement_method"] or "observation"),
                        "device_name": str(raw["device_name"] or ""),
                        "payload": json.loads(str(raw["payload_json"] or "{}")),
                        "media": [],
                    }
                    payload.append(current)
                    sessions.setdefault(str(raw["session_id"]), current)
                    current_event_id = event_id
                assert current is not None
                media_uuid = str(raw["linked_media_uuid"] or "")
                if media_uuid:
                    current["media"].append(
                        {
                            "event_id": event_id,
                            "media_uuid": media_uuid,
                            "role": str(raw["linked_media_role"] or "primary"),
                            "source_revision": str(raw["linked_source_revision"] or ""),
                            "media_kind": str(raw["linked_media_kind"] or ""),
                            "title_snapshot": str(raw["linked_title_snapshot"] or ""),
                            "locator_start": json.loads(str(raw["linked_locator_start_json"] or "{}")),
                            "locator_end": json.loads(str(raw["linked_locator_end_json"] or "{}")),
                        }
                    )
            if sessions:
                # The 60-second rule judges a whole session, not the part inside
                # the period: a session crossing the 7/30-day boundary counts in
                # both periods exactly like in "all time".
                totals = conn.execute(
                    f"""
                    SELECT ev.session_id AS session_id,
                           SUM(CASE WHEN ev.elapsed_monotonic_ms>0
                                     AND ABS((ev.interval_end_utc-ev.interval_start_utc)-ev.elapsed_monotonic_ms/1000.0)>2.0
                                    THEN ev.elapsed_monotonic_ms/1000.0
                                    ELSE MAX(0.0,ev.interval_end_utc-ev.interval_start_utc) END) AS seconds
                    FROM consumption_events ev
                    WHERE ev.session_id IN (
                        SELECT DISTINCT e.session_id FROM consumption_events e
                        JOIN consumption_sessions s ON s.session_id=e.session_id
                        WHERE {where})
                    GROUP BY ev.session_id
                    """,
                    tuple(params),
                ).fetchall()
                for total in totals:
                    first_event = sessions.get(str(total["session_id"]))
                    if first_event is not None:
                        first_event["session_total_seconds"] = float(total["seconds"] or 0.0)
        return payload, sessions, corrections

    @staticmethod
    def _corrected_interval(
        row: dict[str, Any], correction: dict[str, Any] | None
    ) -> tuple[float, float] | None:
        if correction and bool(correction.get("excluded")):
            return None
        raw_start = float(row["interval_start_utc"])
        raw_end = float(row["interval_end_utc"])
        elapsed = max(0.0, float(row.get("elapsed_monotonic_ms") or 0.0) / 1000.0)
        if correction:
            start = _finite(correction.get("replacement_start_utc"), default=raw_start)
            end = _finite(correction.get("replacement_end_utc"), default=None)
            duration = _finite(correction.get("replacement_duration_seconds"), default=None)
            assert start is not None
            if end is None and duration is not None:
                end = start + max(0.0, duration)
            if end is None:
                end = raw_end
            return (start, max(start, end))
        wall = max(0.0, raw_end - raw_start)
        # Calendar clocks can jump. Monotonic elapsed is the duration source of
        # truth; when wall and monotonic diverge materially, keep the UTC start
        # but place the bounded chunk using monotonic elapsed.
        if elapsed > 0.0 and abs(wall - elapsed) > 2.0:
            return (raw_start, raw_start + elapsed)
        return (raw_start, raw_end)

    @staticmethod
    def _ignore_short_automatic_session(
        *, kind: str, measurement_method: str, seconds: float, corrected: bool
    ) -> bool:
        if corrected or _normalize_kind(kind) == "anime":
            return False
        if str(measurement_method or "") not in _SHORT_SESSION_METHODS:
            return False
        return float(seconds) < MIN_AUTOMATIC_SESSION_SECONDS - 1e-6

    def _effective_segments(
        self, normalized: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
        events, session_rows, corrections = self._raw_events(normalized)
        by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            by_session[str(event["session_id"])].append(event)
        segments: list[dict[str, Any]] = []
        journal: list[dict[str, Any]] = []
        lower = normalized["start_utc"]
        upper = float(normalized["end_utc"])
        for session_id, session_events in by_session.items():
            first = session_rows[session_id]
            session_correction = corrections.get(("session", session_id))
            if len(session_events) == 1 and session_correction is None:
                # The recorder normally emits one bounded chunk per short
                # session. Keep that hot path allocation-light while preserving
                # event-level correction semantics.
                event = session_events[0]
                event_media = list(event.get("media") or [])
                interval = self._corrected_interval(event, corrections.get(("event", str(event["event_id"]))))
                raw_start = float(event["interval_start_utc"])
                raw_end = float(event["interval_end_utc"])
                session_segment: dict[str, Any] | None = None
                if interval is not None:
                    start, end = interval
                    if lower is not None:
                        start = max(float(lower), start)
                    end = min(upper, end)
                    if end > start:
                        session_segment = {
                            "start": start,
                            "end": end,
                            "kind": _normalize_kind(str(event.get("kind") or "unknown")),
                            "session_id": session_id,
                            "event_id": str(event["event_id"]),
                            "media": event_media,
                            "origin": str(event.get("origin") or "automatic"),
                            "measurement_method": str(event.get("measurement_method") or "observation"),
                            "device_id": str(event.get("device_id") or ""),
                            "activity_group_id": str(event.get("activity_group_id") or ""),
                            "payload": dict(event.get("payload") or {}),
                        }
                if session_segment is not None:
                    display_start = float(session_segment["start"])
                    display_end = float(session_segment["end"])
                    seconds = display_end - display_start
                else:
                    display_start, display_end, seconds = raw_start, raw_end, 0.0
                method = str(first.get("measurement_method") or "observation")
                event_corrected = ("event", str(event["event_id"])) in corrections
                if self._ignore_short_automatic_session(
                    kind=str(first.get("kind") or "unknown"),
                    measurement_method=method,
                    seconds=max(seconds, float(first.get("session_total_seconds") or 0.0)),
                    corrected=event_corrected,
                ):
                    continue
                if session_segment is not None:
                    segments.append(session_segment)
                primary_media = next(
                    (row for row in event_media if str(row.get("role")) == "primary"),
                    event_media[0] if event_media else {},
                )
                journal.append(
                    {
                        "id": session_id,
                        "type": "session",
                        "session_id": session_id,
                        "start_utc": display_start,
                        "end_utc": display_end,
                        "seconds": seconds,
                        "kind": _normalize_kind(str(first.get("kind") or "unknown")),
                        "media_uuid": str(
                            primary_media.get("media_uuid") or first.get("primary_media_uuid") or ""
                        ),
                        "title": str(primary_media.get("title_snapshot") or "Untitled"),
                        "origin": str(first.get("origin") or "automatic"),
                        "measurement_method": method,
                        "estimated": "estimated" in method,
                        "device_id": str(first.get("device_id") or ""),
                        "device": str(first.get("device_name") or "")
                        or str(first.get("device_id") or "")[:8],
                        "excluded": False,
                        "correction_revision": 0,
                        "correction_reason": "",
                        "event_count": 1,
                    }
                )
                continue

            media_by_uuid: dict[str, dict[str, Any]] = {}
            for event in session_events:
                for media in event.get("media") or []:
                    media_by_uuid[str(media["media_uuid"])] = media
            excluded = bool(session_correction and session_correction.get("excluded"))
            session_segments: list[dict[str, Any]] = []
            replacement_start = (
                _finite(session_correction.get("replacement_start_utc"), default=None)
                if session_correction
                else None
            )
            replacement_end = (
                _finite(session_correction.get("replacement_end_utc"), default=None)
                if session_correction
                else None
            )
            replacement_duration = (
                _finite(session_correction.get("replacement_duration_seconds"), default=None)
                if session_correction
                else None
            )
            if (
                not excluded
                and session_correction
                and (
                    replacement_start is not None
                    or replacement_end is not None
                    or replacement_duration is not None
                )
            ):
                raw_starts = [float(event["interval_start_utc"]) for event in session_events]
                raw_ends = [float(event["interval_end_utc"]) for event in session_events]
                start = replacement_start if replacement_start is not None else min(raw_starts)
                end = replacement_end
                if end is None and replacement_duration is not None:
                    end = start + max(0.0, replacement_duration)
                if end is None:
                    end = max(raw_ends)
                start = max(float(lower), start) if lower is not None else start
                end = min(upper, max(start, end))
                if end > start:
                    session_segments.append(
                        {
                            "start": start,
                            "end": end,
                            "kind": _normalize_kind(str(first.get("kind") or "unknown")),
                            "session_id": session_id,
                            "event_id": "",
                            "media": list(media_by_uuid.values()),
                            "origin": str(first.get("origin") or "automatic"),
                            "measurement_method": "user_correction",
                            "device_id": str(first.get("device_id") or ""),
                            "activity_group_id": str(first.get("activity_group_id") or ""),
                        }
                    )
            elif not excluded:
                for event in session_events:
                    interval = self._corrected_interval(
                        event, corrections.get(("event", str(event["event_id"])))
                    )
                    if interval is None:
                        continue
                    start, end = interval
                    if lower is not None:
                        start = max(float(lower), start)
                    end = min(upper, end)
                    if end <= start:
                        continue
                    session_segments.append(
                        {
                            "start": start,
                            "end": end,
                            "kind": _normalize_kind(str(event.get("kind") or "unknown")),
                            "session_id": session_id,
                            "event_id": str(event["event_id"]),
                            "media": list(event.get("media") or []),
                            "origin": str(event.get("origin") or "automatic"),
                            "measurement_method": str(event.get("measurement_method") or "observation"),
                            "device_id": str(event.get("device_id") or ""),
                            "activity_group_id": str(event.get("activity_group_id") or ""),
                            "payload": dict(event.get("payload") or {}),
                        }
                    )
            raw_session_start = min(float(event["interval_start_utc"]) for event in session_events)
            raw_session_end = max(float(event["interval_end_utc"]) for event in session_events)
            if session_segments:
                display_start = min(float(row["start"]) for row in session_segments)
                display_end = max(float(row["end"]) for row in session_segments)
                seconds = _union_seconds((float(row["start"]), float(row["end"])) for row in session_segments)
            else:
                display_start, display_end, seconds = raw_session_start, raw_session_end, 0.0
            method = str(first.get("measurement_method") or "observation")
            event_corrected = any(
                ("event", str(event["event_id"])) in corrections for event in session_events
            )
            if self._ignore_short_automatic_session(
                kind=str(first.get("kind") or "unknown"),
                measurement_method=method,
                seconds=max(seconds, float(first.get("session_total_seconds") or 0.0)),
                corrected=bool(session_correction) or event_corrected,
            ):
                continue
            segments.extend(session_segments)
            primary_media = next(
                (row for row in media_by_uuid.values() if str(row.get("role")) == "primary"), None
            )
            primary_media = primary_media or next(iter(media_by_uuid.values()), {})
            journal.append(
                {
                    "id": session_id,
                    "type": "session",
                    "session_id": session_id,
                    "start_utc": display_start,
                    "end_utc": display_end,
                    "seconds": seconds,
                    "kind": _normalize_kind(str(first.get("kind") or "unknown")),
                    "media_uuid": str(
                        primary_media.get("media_uuid") or first.get("primary_media_uuid") or ""
                    ),
                    "title": str(primary_media.get("title_snapshot") or "Untitled"),
                    "origin": str(first.get("origin") or "automatic"),
                    "measurement_method": method,
                    "estimated": "estimated" in method,
                    "device_id": str(first.get("device_id") or ""),
                    "device": str(first.get("device_name") or "") or str(first.get("device_id") or "")[:8],
                    "excluded": excluded,
                    "correction_revision": int(session_correction.get("revision") or 0)
                    if session_correction
                    else 0,
                    "correction_reason": str(session_correction.get("reason") or "")
                    if session_correction
                    else "",
                    "event_count": len(session_events),
                }
            )
        return segments, journal, corrections

    def _manual_rows(
        self, normalized: dict[str, Any], corrections: dict[tuple[str, str], dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
        conditions: list[str] = []
        params: list[Any] = []
        if normalized["kind"] != "all":
            conditions.append("kind=?")
            params.append(normalized["kind"])
        if normalized["media_uuid"]:
            conditions.append("media_uuid=?")
            params.append(normalized["media_uuid"])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM consumption_manual_entries" + where + " ORDER BY created_at_utc",
                tuple(params),
            ).fetchall()
        segments: list[dict[str, Any]] = []
        journal: list[dict[str, Any]] = []
        unplaced = 0.0
        lower = normalized["start_utc"]
        upper = float(normalized["end_utc"])
        for row_raw in rows:
            row = dict(row_raw)
            manual_id = str(row["manual_id"])
            correction = corrections.get(("manual", manual_id))
            excluded = bool(correction and correction.get("excluded"))
            duration = max(0.0, float(row["duration_seconds"] or 0.0))
            if correction and correction.get("replacement_duration_seconds") is not None:
                duration = max(0.0, float(correction["replacement_duration_seconds"]))
            start = _finite(row.get("started_at_utc"), default=None)
            if correction and correction.get("replacement_start_utc") is not None:
                start = float(correction["replacement_start_utc"])
            end: float | None = None
            if start is not None:
                end = start + duration
                if correction and correction.get("replacement_end_utc") is not None:
                    end = max(start, float(correction["replacement_end_utc"]))
            included_in_period = start is None or (
                end is not None and end > (float(lower) if lower is not None else -1.0) and start < upper
            )
            if start is None and not excluded:
                unplaced += duration
            if start is not None and end is not None and included_in_period and not excluded:
                clipped_start = max(float(lower), start) if lower is not None else start
                clipped_end = min(upper, end)
                if clipped_end > clipped_start:
                    segments.append(
                        {
                            "start": clipped_start,
                            "end": clipped_end,
                            "kind": _normalize_kind(str(row["kind"])),
                            "session_id": f"manual:{manual_id}",
                            "event_id": "",
                            "media": [
                                {
                                    "media_uuid": str(row.get("media_uuid") or ""),
                                    "title_snapshot": _manual_title(row.get("title_snapshot")),
                                    "media_kind": str(row["kind"]),
                                    "role": "primary",
                                }
                            ],
                            "origin": "manual",
                            "measurement_method": "manual_exact_time",
                            "device_id": "",
                            "activity_group_id": "",
                        }
                    )
            if included_in_period:
                journal.append(
                    {
                        "id": manual_id,
                        "type": "manual",
                        "session_id": "",
                        "start_utc": start,
                        "end_utc": end,
                        "seconds": 0.0 if excluded else duration,
                        "kind": _normalize_kind(str(row["kind"])),
                        "media_uuid": str(row.get("media_uuid") or ""),
                        "title": _manual_title(row.get("title_snapshot")),
                        "origin": "manual",
                        "measurement_method": "manual_exact_time"
                        if start is not None
                        else "manual_duration_only",
                        "estimated": start is None,
                        "device_id": "",
                        "device": platform.node() or t("web.thisComputer"),
                        "excluded": excluded,
                        "correction_revision": int(correction.get("revision") or 0) if correction else 0,
                        "correction_reason": str(correction.get("reason") or "") if correction else "",
                        "event_count": 0,
                        "note": str(row.get("note") or ""),
                    }
                )
        return segments, journal, unplaced

    def query(
        self,
        scope: dict[str, Any] | None = None,
        *,
        now: float | None = None,
        journal_limit: int | None = 200,
        journal_offset: int = 0,
    ) -> dict[str, Any]:
        normalized = self._scope(scope, now=now)
        segments, journal, corrections = self._effective_segments(normalized)
        native_segments = list(segments)
        prior_native_segments: list[dict[str, Any]] = []
        prior_state: dict[str, Any] | None = None
        if normalized["start_utc"] is not None and float(normalized["start_utc"]) > 0:
            prior_state = self._prior_volume(normalized, now=now)
            if prior_state is None:
                prior_scope = dict(normalized)
                prior_scope["start_utc"] = None
                prior_scope["end_utc"] = float(normalized["start_utc"])
                prior_native_segments, _prior_journal, _prior_corrections = self._effective_segments(
                    prior_scope
                )
        if prior_state is None and prior_native_segments:
            prior_state = prior_volume_state(self.database, prior_native_segments)
        volume = aggregate_volume(
            self.database, native_segments, prior_segments=prior_native_segments, prior_state=prior_state
        )
        words = self._words(native_segments, normalized, prior_state, now=now)
        manual_segments, manual_journal, manual_unplaced = self._manual_rows(normalized, corrections)
        segments.extend(manual_segments)
        journal.extend(manual_journal)

        total_seconds, breakdown = _sweep_breakdown(segments)
        by_day = _segments_by_local_day(segments)
        day_payload: dict[str, dict[str, Any]] = {}
        for day in sorted(by_day):
            day_segments = by_day[day]
            day_total, day_breakdown = _sweep_breakdown(day_segments)
            event_keys = {
                str(segment.get("event_id") or segment.get("session_id") or "")
                for segment in day_segments
                if segment.get("event_id") or segment.get("session_id")
            }
            day_payload[day] = {
                "day": day,
                "seconds": day_total,
                "breakdown": day_breakdown,
                "event_count": len(event_keys),
            }
        if normalized["period"] != "all" and normalized["start_utc"] is not None:
            first_day = _local_day(float(normalized["start_utc"]))
            last_probe = max(float(normalized["start_utc"]), float(normalized["end_utc"]) - 1e-6)
            last_day = _local_day(last_probe)
            span = (last_day - first_day).days
            if 0 <= span <= 370:
                for offset in range(span + 1):
                    key = (first_day + timedelta(days=offset)).isoformat()
                    day_payload.setdefault(
                        key, {"day": key, "seconds": 0.0, "breakdown": {}, "event_count": 0}
                    )
        days = [day_payload[key] for key in sorted(day_payload)]

        # Works are logical consumption targets, not a raw count of source files.
        # A linked LN + audiobook pair shares one activity_group_id and therefore
        # appears once with unioned physical time. Unrelated simultaneous media
        # remain separate works.
        linked_group_kinds: dict[str, set[str]] = defaultdict(set)
        for segment in segments:
            group_id = str(segment.get("activity_group_id") or "")
            kind = _normalize_kind(str(segment.get("kind") or "unknown"))
            if group_id and kind in {"light_novel", "audiobook"}:
                linked_group_kinds[group_id].add(kind)

        work_segments: dict[str, list[dict[str, Any]]] = defaultdict(list)
        work_media: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for segment in segments:
            group_id = str(segment.get("activity_group_id") or "")
            kind = _normalize_kind(str(segment.get("kind") or "unknown"))
            linked_pair = bool(
                group_id
                and kind in {"light_novel", "audiobook"}
                and linked_group_kinds.get(group_id) == {"light_novel", "audiobook"}
            )
            media_rows = [row for row in (segment.get("media") or []) if str(row.get("media_uuid") or "")]
            if linked_pair:
                key = f"activity-group:{group_id}"
                work_segments[key].append(segment)
                for media in media_rows:
                    work_media[key][str(media["media_uuid"])] = media
                continue
            for media in media_rows:
                media_uuid = str(media["media_uuid"])
                key = f"media:{media_uuid}"
                work_segments[key].append(segment)
                work_media[key][media_uuid] = media

        works: list[dict[str, Any]] = []
        for key, rows in work_segments.items():
            media_rows = list(work_media[key].values())
            if not media_rows:
                continue
            seconds = _union_seconds((float(row["start"]), float(row["end"])) for row in rows)
            kinds = {
                _normalize_kind(str(media.get("media_kind") or ""))
                for media in media_rows
                if str(media.get("media_kind") or "")
            }
            linked_pair = kinds == {"light_novel", "audiobook"}
            preferred = next(
                (
                    media
                    for media in media_rows
                    if _normalize_kind(str(media.get("media_kind") or "")) == "light_novel"
                ),
                media_rows[0],
            )
            media_uuids = sorted(
                {str(media.get("media_uuid") or "") for media in media_rows if media.get("media_uuid")}
            )
            work_volume = [
                volume["by_media"][media_uuid]
                for media_uuid in media_uuids
                if media_uuid in volume["by_media"]
            ]
            works.append(
                {
                    "media_uuid": str(preferred.get("media_uuid") or ""),
                    "media_uuids": media_uuids,
                    "volume": work_volume,
                    "activity_group_id": key.removeprefix("activity-group:")
                    if key.startswith("activity-group:")
                    else "",
                    "title": str(preferred.get("title_snapshot") or "Untitled"),
                    "kind": "light_novel+audiobook"
                    if linked_pair
                    else _normalize_kind(
                        str(preferred.get("media_kind") or rows[0].get("kind") or "unknown")
                    ),
                    "seconds": seconds,
                    "last_at_utc": max(float(row["end"]) for row in rows),
                    "session_count": len({str(row.get("session_id") or "") for row in rows}),
                }
            )
        works.sort(key=lambda row: (-float(row["seconds"]), str(row["title"]).casefold()))

        journal.sort(key=lambda row: (float(row["start_utc"] or 0.0), str(row["id"])), reverse=True)
        journal_total = len(journal)
        start_index = max(0, int(journal_offset))
        if journal_limit is None:
            journal_page = journal[start_index:]
        else:
            limit = max(1, min(100000, int(journal_limit)))
            journal_page = journal[start_index : start_index + limit]
        for row in journal_page:
            row["start_local"] = _format_local(row.get("start_utc"))
            row["end_local"] = _format_local(row.get("end_utc"))

        with self.database.connect() as conn:
            media_options = [
                {
                    "media_uuid": str(row["media_uuid"]),
                    "kind": _normalize_kind(str(row["kind"])),
                    "title": str(row["title_snapshot"] or "Untitled"),
                }
                for row in conn.execute(
                    "SELECT media_uuid,kind,title_snapshot FROM consumption_media ORDER BY title_snapshot COLLATE NOCASE"
                ).fetchall()
            ]
        active_session_ids = {str(row.get("session_id") or "") for row in segments if row.get("session_id")}
        highlights = self._highlights(days, journal, works, breakdown, volume, words)
        return {
            "schema": 1,
            "policy_version": STATISTICS_POLICY_VERSION,
            "timezone": {"mode": "system", "label": system_local_timezone_label()},
            "scope": normalized,
            "summary": {
                "total_seconds": total_seconds,
                "active_days": sum(1 for row in days if float(row["seconds"]) > 0.0),
                "sessions": len(active_session_ids),
                "works": len(works),
                "manual_unplaced_seconds": manual_unplaced,
            },
            "breakdown": breakdown,
            "volume": volume,
            "highlights": highlights,
            "days": days,
            "works": works,
            "journal": journal_page,
            "journal_total": journal_total,
            "media_options": media_options,
        }

    def _history_stamp(self, conn: Any, before: float) -> str:
        """Changes when anything that a summary up to ``before`` was built from changes."""
        events = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS last FROM consumption_events WHERE interval_start_utc < ?",
            (float(before),),
        ).fetchone()
        corrections = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS last FROM consumption_corrections"
        ).fetchone()
        return (
            f"{STATISTICS_POLICY_VERSION}|{VOLUME_POLICY_VERSION}|{int(events['n'])}|{int(events['last'])}"
            f"|{int(corrections['n'])}|{int(corrections['last'])}"
        )

    def _prior_volume(self, normalized: dict[str, Any], *, now: float | None = None) -> dict[str, Any] | None:
        """Volume history before the period from a stored summary plus only the newer events.

        Summaries end at a local midnight and are rebuilt when an earlier event
        arrives late (an offline iPhone), a correction is made, or history is reset.
        Filtered reports (one kind or one work) still read the whole history.
        """
        if normalized["kind"] != "all" or normalized["media_uuid"]:
            return None
        boundary = float(normalized["start_utc"])
        current = float(now if now is not None else time.time())
        with self.database.connect() as conn:
            rows = conn.execute(
                "SELECT key,value FROM state WHERE substr(key,1,?)=?",
                (len(VOLUME_ROLLUP_PREFIX), VOLUME_ROLLUP_PREFIX),
            ).fetchall()
            stored: list[tuple[float, str, str]] = []
            for row in rows:
                try:
                    covered = float(str(row["key"])[len(VOLUME_ROLLUP_PREFIX) :])
                    record = json.loads(str(row["value"] or "{}"))
                    stored.append((covered, str(record["stamp"]), str(record["state"])))
                except (TypeError, ValueError, KeyError):
                    continue
            stored.sort(reverse=True)
            base: tuple[float, str] | None = None
            for covered, stamp, text in stored:
                if covered <= boundary and stamp == self._history_stamp(conn, covered):
                    base = (covered, text)
                    break
            boundary_stamp = self._history_stamp(conn, boundary)
        state: dict[str, Any] = {"numeric": {}, "manga": {}, "denominators": {}}
        span_start: float | None = None
        if base is not None:
            try:
                state = load_prior_volume_state(base[1])
                span_start = base[0]
            except (TypeError, ValueError, KeyError):
                span_start = None
        if span_start is None or span_start < boundary:
            span = dict(normalized)
            span["start_utc"] = None if span_start is None else max(0.0, span_start - _ROLLUP_OVERLAP_SECONDS)
            span["end_utc"] = boundary
            span_segments, _journal, _corrections = self._effective_segments(span)
            state = merge_prior_volume_states(state, prior_volume_state(self.database, span_segments))
        if boundary <= current and (base is None or base[0] != boundary):
            try:
                with self.database.connect() as conn:
                    conn.execute(
                        "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                        (
                            f"{VOLUME_ROLLUP_PREFIX}{boundary:.3f}",
                            json.dumps({"stamp": boundary_stamp, "state": dump_prior_volume_state(state)}),
                            time.time(),
                        ),
                    )
                    keys = [
                        str(row["key"])
                        for row in conn.execute(
                            "SELECT key FROM state WHERE substr(key,1,?)=? ORDER BY updated_at DESC",
                            (len(VOLUME_ROLLUP_PREFIX), VOLUME_ROLLUP_PREFIX),
                        ).fetchall()
                    ]
                    for key in keys[_VOLUME_ROLLUPS_KEPT:]:
                        conn.execute("DELETE FROM state WHERE key=?", (key,))
            except Exception as exc:  # noqa: BLE001 - a summary that cannot be stored is only slower next time
                if self.logger is not None:
                    self.logger.warning("SKIP step=statistics.volume_rollup error=%r", exc)
        return state

    def correction(
        self,
        *,
        target_type: str,
        target_id: str,
        expected_revision: int = 0,
        excluded: bool = False,
        start_utc: float | None = None,
        end_utc: float | None = None,
        duration_seconds: float | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        target_type = str(target_type or "").strip().casefold()
        target_id = str(target_id or "").strip()
        if target_type not in {"session", "event", "manual"} or not target_id:
            raise ConsumptionLedgerError(err("consumption.invalidCorrectionTarget"))
        start = _finite(start_utc, default=None)
        end = _finite(end_utc, default=None)
        duration = _finite(duration_seconds, default=None)
        if duration is not None and (duration < 0 or duration > 31 * 86400):
            raise ConsumptionLedgerError(err("consumption.invalidCorrectionDuration"))
        if start is not None and end is not None and end < start:
            raise ConsumptionLedgerError(err("consumption.correctionEndBeforeStart"))
        now = time.time()
        correction_id = str(uuid.uuid4())
        with self.database.connect() as conn:
            table, column = {
                "session": ("consumption_sessions", "session_id"),
                "event": ("consumption_events", "event_id"),
                "manual": ("consumption_manual_entries", "manual_id"),
            }[target_type]
            if conn.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (target_id,)).fetchone() is None:
                raise ConsumptionLedgerError(err("consumption.correctionTargetMissing"))
            row = conn.execute(
                "SELECT COALESCE(MAX(revision),0) AS revision FROM consumption_corrections WHERE target_type=? AND target_id=?",
                (target_type, target_id),
            ).fetchone()
            current = int(row["revision"] if row else 0)
            if int(expected_revision) != current:
                raise ConsumptionConflictError(
                    err("consumption.correctionConflict", expected=expected_revision, current=current)
                )
            device_row = conn.execute("SELECT value FROM state WHERE key='consumption_device_id'").fetchone()
            device_id = str(device_row["value"] or "") if device_row is not None else ""
            revision = current + 1
            conn.execute(
                """
                INSERT INTO consumption_corrections(
                    correction_id,target_type,target_id,revision,parent_revision,excluded,
                    replacement_start_utc,replacement_end_utc,replacement_duration_seconds,
                    reason,device_id,created_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    correction_id,
                    target_type,
                    target_id,
                    revision,
                    current,
                    int(bool(excluded)),
                    start,
                    end,
                    duration,
                    str(reason or ""),
                    device_id,
                    now,
                ),
            )
        return {"ok": True, "correction_id": correction_id, "revision": revision}

    def add_manual(
        self,
        *,
        kind: str,
        duration_seconds: float,
        started_at_utc: float | None = None,
        media_uuid: str = "",
        title: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        normalized_kind = _normalize_kind(kind)
        if normalized_kind not in _VALID_KINDS - {"manual"}:
            raise ConsumptionLedgerError(err("consumption.invalidManualKind"))
        duration = _finite(duration_seconds, default=None)
        if duration is None or duration <= 0 or duration > 31 * 86400:
            raise ConsumptionLedgerError(err("consumption.invalidManualDuration"))
        start = _finite(started_at_utc, default=None)
        manual_id = str(uuid.uuid4())
        media_uuid = str(media_uuid or "").strip()
        title_snapshot = str(title or "").strip()
        with self.database.connect() as conn:
            if media_uuid:
                row = conn.execute(
                    "SELECT title_snapshot FROM consumption_media WHERE media_uuid=?", (media_uuid,)
                ).fetchone()
                if row is None:
                    raise ConsumptionLedgerError(err("consumption.manualMediaMissing"))
                title_snapshot = str(row["title_snapshot"] or title_snapshot)
        if not media_uuid:
            title_snapshot = title_snapshot or t("web.manualEntry", lang="en")  # stored; shown translated
            media = self.ledger.ensure_media(
                kind=normalized_kind,
                title=title_snapshot,
                aliases=[("manual", manual_id)],
            )
            media_uuid = media.media_uuid
        now = time.time()
        with self.database.connect() as conn:
            conn.execute(
                """
                INSERT INTO consumption_manual_entries(
                    manual_id,media_uuid,kind,title_snapshot,started_at_utc,duration_seconds,
                    note,origin,created_at_utc,updated_at_utc
                ) VALUES(?,?,?,?,?,?,?,'manual',?,?)
                """,
                (
                    manual_id,
                    media_uuid,
                    normalized_kind,
                    title_snapshot,
                    start,
                    duration,
                    str(note or ""),
                    now,
                    now,
                ),
            )
        return {"ok": True, "manual_id": manual_id, "media_uuid": media_uuid}

    def delete_history(self) -> dict[str, Any]:
        return self.ledger.delete_history()

    def rebuild_daily_rollups(self) -> dict[str, Any]:
        payload = self.query({"period": "all"}, journal_limit=1)
        now = time.time()
        policy = f"{STATISTICS_POLICY_VERSION}:{system_local_timezone_label()}"
        with self.database.connect() as conn:
            conn.execute("DELETE FROM consumption_daily_rollups")
            for row in payload["days"]:
                conn.execute(
                    """
                    INSERT INTO consumption_daily_rollups(
                        local_day,total_seconds,breakdown_json,event_count,policy_version,generated_at_utc
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (
                        str(row["day"]),
                        float(row["seconds"]),
                        json.dumps(row["breakdown"], sort_keys=True),
                        int(row.get("event_count") or 0),
                        policy,
                        now,
                    ),
                )
        return {"ok": True, "days": len(payload["days"]), "policy_version": policy}

    def export_csv(
        self, scope: dict[str, Any] | None = None, *, output_dir: Path | None = None
    ) -> dict[str, Any]:
        payload = self.query(scope, journal_limit=None)
        root = Path(output_dir or (Path.home() / "Downloads")).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"pudge-statistics-{time.strftime('%Y%m%d-%H%M%S')}.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "start_local",
                    "end_local",
                    "duration_seconds",
                    "title",
                    "format",
                    "origin",
                    "measurement",
                    "device",
                    "excluded",
                    "correction_reason",
                    "id",
                ]
            )
            for row in payload["journal"]:
                writer.writerow(
                    [
                        row.get("start_local") or "",
                        row.get("end_local") or "",
                        round(float(row.get("seconds") or 0.0), 3),
                        row.get("title") or "",
                        row.get("kind") or "",
                        row.get("origin") or "",
                        row.get("measurement_method") or "",
                        row.get("device") or "",
                        "1" if row.get("excluded") else "0",
                        row.get("correction_reason") or "",
                        row.get("id") or "",
                    ]
                )
        return {
            "ok": True,
            "path": str(path),
            "rows": len(payload["journal"]),
            "summary": payload["summary"],
            "scope": payload["scope"],
        }
