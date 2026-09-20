from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable

from .database import Database

VOLUME_POLICY_VERSION = "r9-v1"
MANGA_DWELL_SECONDS = 3.0


def _kind(value: Any) -> str:
    raw = str(value or "").casefold()
    return {"anime_episode": "anime", "ln": "light_novel", "novel": "light_novel"}.get(raw, raw)


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _merged(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    rows = sorted((float(a), float(b)) for a, b in intervals if float(b) > float(a))
    out: list[list[float]] = []
    for start, end in rows:
        if out and start <= out[-1][1] + 1e-6:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(a, b) for a, b in out]


def _length(intervals: Iterable[tuple[float, float]]) -> float:
    return sum(b - a for a, b in _merged(intervals))


def _new_length(selected: Iterable[tuple[float, float]], prior: Iterable[tuple[float, float]]) -> float:
    # Length of the selected union that was not covered before this report period.
    selected_rows = _merged(selected)
    prior_rows = _merged(prior)
    if not selected_rows:
        return 0.0
    if not prior_rows:
        return sum(b - a for a, b in selected_rows)
    total = 0.0
    j = 0
    for start, end in selected_rows:
        cursor = start
        while j < len(prior_rows) and prior_rows[j][1] <= cursor:
            j += 1
        k = j
        while k < len(prior_rows) and prior_rows[k][0] < end:
            p0, p1 = prior_rows[k]
            if p0 > cursor:
                total += min(end, p0) - cursor
            cursor = max(cursor, p1)
            if cursor >= end:
                break
            k += 1
        if cursor < end:
            total += end - cursor
    return max(0.0, total)


def _position_range(segment: dict[str, Any], media: dict[str, Any]) -> tuple[float, float] | None:
    if bool((segment.get("payload") or {}).get("seek_or_discontinuity")):
        return None
    start = _finite_float((media.get("locator_start") or {}).get("position_seconds"))
    end = _finite_float((media.get("locator_end") or {}).get("position_seconds"))
    if start is None or end is None or end <= start:
        return None
    elapsed = max(0.0, float(segment.get("end") or 0.0) - float(segment.get("start") or 0.0))
    speed = max(0.1, _finite_float((segment.get("payload") or {}).get("speed")) or 1.0)
    # Protect volume from a bad locator even when a producer forgot to mark a seek.
    if end - start > max(15.0, elapsed * speed * 4.0 + 10.0):
        return None
    return start, end


def _ln_range(media: dict[str, Any]) -> tuple[str, float, float] | None:
    first = media.get("locator_start") or {}
    last = media.get("locator_end") or {}
    chapter0 = str(first.get("chapter_key") or f"chapter:{int(first.get('chapter_index') or 0)}")
    chapter1 = str(last.get("chapter_key") or f"chapter:{int(last.get('chapter_index') or 0)}")
    if chapter0 != chapter1:
        return None
    start = _finite_float(first.get("character_offset"))
    end = _finite_float(last.get("character_offset"))
    if start is None or end is None or end <= start or end - start > 20000:
        return None
    return chapter0, start, end


def _manga_page(media: dict[str, Any]) -> str:
    locator = media.get("locator_end") or media.get("locator_start") or {}
    page = str(locator.get("page_id") or "").strip()
    if page:
        return page
    value = locator.get("page_index")
    return f"page:{int(value or 0)}"


def _media_metadata(database: Database) -> dict[str, dict[str, Any]]:
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT media_uuid,kind,source_revision,metadata_json FROM consumption_media"
        ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        result[str(row["media_uuid"])] = {
            "kind": _kind(row["kind"]),
            "source_revision": str(row["source_revision"] or ""),
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
    return result


def _manga_visits(segments: list[dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
    # Stable page visits, not heartbeats. A visit is accepted only after enough
    # dwell, and leaving + returning creates a repeat visit.
    observations: dict[tuple[str, str], list[tuple[float, float, str]]] = defaultdict(list)
    for segment in segments:
        if _kind(segment.get("kind")) != "manga":
            continue
        seconds = max(0.0, float(segment.get("end") or 0.0) - float(segment.get("start") or 0.0))
        for media in segment.get("media") or []:
            media_uuid = str(media.get("media_uuid") or "")
            if not media_uuid:
                continue
            revision = str(media.get("source_revision") or "")
            observations[(media_uuid, revision)].append(
                (float(segment.get("start") or 0.0), seconds, _manga_page(media))
            )
    result: dict[tuple[str, str], list[str]] = defaultdict(list)
    for key, rows in observations.items():
        rows.sort()
        current_page = ""
        dwell = 0.0
        last_start: float | None = None
        for start, seconds, page in rows:
            split = bool(current_page and (page != current_page or (last_start is not None and start - last_start > 30.0)))
            if split:
                if dwell >= MANGA_DWELL_SECONDS:
                    result[key].append(current_page)
                current_page, dwell = "", 0.0
            if not current_page:
                current_page = page
            dwell += seconds
            last_start = start
        if current_page and dwell >= MANGA_DWELL_SECONDS:
            result[key].append(current_page)
    return result


def aggregate_volume(
    database: Database,
    segments: list[dict[str, Any]],
    *,
    prior_segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compute native material consumption without deriving it from progress deltas."""
    prior_segments = prior_segments or []
    metadata = _media_metadata(database)

    numeric: dict[tuple[str, str, str, str], list[tuple[float, float]]] = defaultdict(list)
    prior_numeric: dict[tuple[str, str, str, str], list[tuple[float, float]]] = defaultdict(list)
    denominators: dict[tuple[str, str], float] = {}

    def collect(rows: list[dict[str, Any]], target: dict[tuple[str, str, str, str], list[tuple[float, float]]]) -> None:
        for segment in rows:
            kind = _kind(segment.get("kind"))
            if kind not in {"anime", "audiobook", "light_novel"}:
                continue
            payload = segment.get("payload") or {}
            for media in segment.get("media") or []:
                media_uuid = str(media.get("media_uuid") or "")
                if not media_uuid:
                    continue
                revision = str(media.get("source_revision") or metadata.get(media_uuid, {}).get("source_revision") or "")
                if kind in {"anime", "audiobook"}:
                    interval = _position_range(segment, media)
                    if interval is None:
                        continue
                    target[(media_uuid, revision, kind, "timeline")].append(interval)
                    if kind == "anime":
                        duration = _finite_float(payload.get("duration_seconds"))
                    else:
                        duration = _finite_float(metadata.get(media_uuid, {}).get("metadata", {}).get("duration_seconds"))
                    if duration is not None and duration > 0:
                        denominators[(media_uuid, kind)] = max(denominators.get((media_uuid, kind), 0.0), duration)
                else:
                    interval = _ln_range(media)
                    if interval is None:
                        continue
                    chapter, start, end = interval
                    target[(media_uuid, revision, kind, chapter)].append((start, end))
                    total = _finite_float(metadata.get(media_uuid, {}).get("metadata", {}).get("character_count"))
                    if total is not None and total > 0:
                        denominators[(media_uuid, kind)] = max(denominators.get((media_uuid, kind), 0.0), total)

    collect(segments, numeric)
    collect(prior_segments, prior_numeric)
    selected_manga = _manga_visits(segments)
    prior_manga = _manga_visits(prior_segments)

    # Manga denominator is supplied by the reader independently of OCR.
    for segment in segments + prior_segments:
        if _kind(segment.get("kind")) != "manga":
            continue
        page_count = _finite_float((segment.get("payload") or {}).get("page_count"))
        if page_count is None or page_count <= 0:
            continue
        for media in segment.get("media") or []:
            media_uuid = str(media.get("media_uuid") or "")
            if media_uuid:
                denominators[(media_uuid, "manga")] = max(denominators.get((media_uuid, "manga"), 0.0), page_count)

    by_media: dict[str, dict[str, Any]] = {}

    keys = set(numeric) | set(prior_numeric)
    grouped_keys: dict[tuple[str, str], list[tuple[str, str, str, str]]] = defaultdict(list)
    for key in keys:
        grouped_keys[(key[0], key[2])].append(key)

    for (media_uuid, kind), media_keys in grouped_keys.items():
        consumed = unique_scope = first = lifetime_unique = 0.0
        for key in media_keys:
            selected = numeric.get(key, [])
            prior = prior_numeric.get(key, [])
            consumed += sum(max(0.0, b - a) for a, b in selected)
            unique_scope += _length(selected)
            first += _new_length(selected, prior)
            lifetime_unique += _length([*prior, *selected])
        repeat = max(0.0, consumed - first)
        denominator = denominators.get((media_uuid, kind))
        by_media[media_uuid] = {
            "media_uuid": media_uuid,
            "kind": kind,
            "unit": "characters" if kind == "light_novel" else "seconds",
            "exact": True,
            "consumed": consumed,
            "unique_in_scope": unique_scope,
            "first_in_scope": first,
            "repeat_in_scope": repeat,
            "lifetime_unique": lifetime_unique,
            "total_available": denominator,
            "coverage": min(1.0, lifetime_unique / denominator) if denominator and denominator > 0 else None,
        }

    manga_keys = set(selected_manga) | set(prior_manga)
    manga_by_media: dict[str, list[str]] = defaultdict(list)
    for media_uuid, revision in manga_keys:
        manga_by_media[media_uuid].append(revision)
    for media_uuid, revisions in manga_by_media.items():
        selected_visits: list[tuple[str, str]] = []
        prior_pages: set[tuple[str, str]] = set()
        selected_pages: set[tuple[str, str]] = set()
        for revision in revisions:
            selected = selected_manga.get((media_uuid, revision), [])
            prior = prior_manga.get((media_uuid, revision), [])
            selected_visits.extend((revision, page) for page in selected)
            prior_pages.update((revision, page) for page in prior)
            selected_pages.update((revision, page) for page in selected)
        first_pages = len(selected_pages - prior_pages)
        consumed = len(selected_visits)
        lifetime_unique = len(prior_pages | selected_pages)
        denominator = denominators.get((media_uuid, "manga"))
        by_media[media_uuid] = {
            "media_uuid": media_uuid,
            "kind": "manga",
            "unit": "pages",
            "exact": True,
            "consumed": float(consumed),
            "unique_in_scope": float(len(selected_pages)),
            "first_in_scope": float(first_pages),
            "repeat_in_scope": float(max(0, consumed - first_pages)),
            "lifetime_unique": float(lifetime_unique),
            "total_available": denominator,
            "coverage": min(1.0, lifetime_unique / denominator) if denominator and denominator > 0 else None,
        }

    # VN time is exact enough for R8/R9, but native text/page volume is not.
    for segment in segments:
        if _kind(segment.get("kind")) != "visual_novel":
            continue
        for media in segment.get("media") or []:
            media_uuid = str(media.get("media_uuid") or "")
            if media_uuid and media_uuid not in by_media:
                by_media[media_uuid] = {
                    "media_uuid": media_uuid, "kind": "visual_novel", "unit": "unknown",
                    "exact": False, "consumed": None, "unique_in_scope": None,
                    "first_in_scope": None, "repeat_in_scope": None,
                    "lifetime_unique": None, "total_available": None, "coverage": None,
                }

    by_kind: dict[str, dict[str, Any]] = {}
    for row in by_media.values():
        kind = str(row["kind"])
        if kind not in by_kind:
            by_kind[kind] = {
                "kind": kind, "unit": row["unit"], "exact": bool(row["exact"]),
                "consumed": 0.0 if row["consumed"] is not None else None,
                "first_in_scope": 0.0 if row["first_in_scope"] is not None else None,
                "repeat_in_scope": 0.0 if row["repeat_in_scope"] is not None else None,
            }
        target = by_kind[kind]
        target["exact"] = bool(target["exact"] and row["exact"])
        for name in ("consumed", "first_in_scope", "repeat_in_scope"):
            if target[name] is not None and row[name] is not None:
                target[name] += float(row[name])
            elif row[name] is None:
                target[name] = None

    with database.connect() as conn:
        first_row = conn.execute("SELECT MIN(interval_start_utc) AS t FROM consumption_events").fetchone()
    coverage_started = float(first_row["t"]) if first_row is not None and first_row["t"] is not None else None
    return {
        "policy_version": VOLUME_POLICY_VERSION,
        "coverage_started_at_utc": coverage_started,
        "legacy_complete": False,
        "by_kind": by_kind,
        "by_media": by_media,
    }
