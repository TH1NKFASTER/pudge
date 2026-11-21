"""Watched credit summed across devices: the union of the parts of an episode seen anywhere.

The iPhone/iPad sends the parts it played (seconds, seeks excluded) with its place; the Mac's
mpv heartbeats add the part played since the previous heartbeat. Overlaps count once, so a
part seen on both devices (or sent again by a retry) is never counted twice. The usual rule
(65 % of the episode, at least 3 minutes) is then checked against the union: half watched on
the iPad and the rest in mpv counts like a full watch on one device.

Kept per episode in the ``state`` table (``watch_intervals:v1:<video path>``), bounded, cleared
once the watch counted and ignored for an episode already watched (a rewatch counts on its own).
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Iterable

STATE_PREFIX = "watch_intervals:v1:"
# More parts than this keeps the longest ones (never credits a gap that was not watched).
MAX_INTERVALS = 200
# Parts a device sent in one message (a longer list is cut to the longest parts).
MAX_INPUT_INTERVALS = 2000
# Parts not added to for this long belong to an abandoned watch: they start over.
EXPIRE_SECONDS = 30 * 24 * 3600.0
# A device reached the end: its place or a part played is past this share of the episode.
END_FRACTION = 0.85
# Expired parts are deleted at most this often per library (heartbeats come every few seconds;
# load() already ignores expired parts).
PRUNE_INTERVAL_SECONDS = 3600.0
_LAST_PRUNE: dict[str, float] = {}

Interval = tuple[float, float]


def required_seconds(duration: float) -> float:
    """The usual watched rule (as mpv's tracker and the iPhone): 65 % of the episode, ≥ 3 min."""
    duration = float(duration or 0.0)
    return max(180.0, duration * 0.65) if duration > 0 else 300.0


def merge(
    intervals: Iterable[Interval], *, duration: float = 0.0, cap: int = MAX_INTERVALS
) -> list[Interval]:
    """Sorted union of the parts (clipped to the episode), at most ``cap`` parts."""
    limit = float(duration or 0.0)
    clean: list[Interval] = []
    for start, end in intervals:
        start, end = float(start), float(end)
        if not (math.isfinite(start) and math.isfinite(end)):
            continue
        start = max(0.0, start)
        if limit > 0:
            end = min(end, limit)
        if end - start >= 0.5:
            clean.append((start, end))
    clean.sort()
    merged: list[Interval] = []
    for start, end in clean:
        # Touching parts (whole seconds from the phone, 15 s mpv steps) join.
        if merged and start <= merged[-1][1] + 0.5:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    if len(merged) > cap:
        merged = sorted(sorted(merged, key=lambda part: part[1] - part[0], reverse=True)[:cap])
    return merged


def covered_seconds(intervals: Iterable[Interval]) -> float:
    return sum(max(0.0, end - start) for start, end in merge(intervals, cap=10**9))


def parse_ranges(value: Any) -> list[Interval]:
    """``[[start, end], ...]`` (seconds) from a device; anything malformed is skipped."""
    if not isinstance(value, list):
        return []
    parts: list[Interval] = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            start, end = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and end > start:
            parts.append((start, end))
    if len(parts) > MAX_INPUT_INTERVALS:
        parts = sorted(parts, key=lambda part: part[1] - part[0], reverse=True)[:MAX_INPUT_INTERVALS]
    return parts


def mpv_interval(previous: float | None, position: float, active: float) -> Interval | None:
    """The part an mpv heartbeat played: ``active`` seconds up to ``position``, never before
    the previous heartbeat's place when playback went straight on from it."""
    active = max(0.0, float(active or 0.0))
    position = max(0.0, float(position or 0.0))
    if active < 0.5 or not math.isfinite(position):
        return None
    start = position - active
    if previous is not None and math.isfinite(float(previous)) and float(previous) <= position:
        start = max(start, float(previous))
    start = max(0.0, start)
    return (start, position) if position - start >= 0.5 else None


def _key(video_path: str | Path) -> str:
    return STATE_PREFIX + str(video_path)


def load(db: Any, video_path: str | Path, *, now: float | None = None) -> dict[str, Any]:
    """``{"intervals": [...], "duration": s, "end_reached": bool}`` (empty when none or expired)."""
    now = time.time() if now is None else now
    try:
        stored = json.loads(db.get_state(_key(video_path), "") or "{}")
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict) or now - float(stored.get("u") or 0.0) > EXPIRE_SECONDS:
        stored = {}
    duration = float(stored.get("d") or 0.0)
    return {
        "intervals": merge(parse_ranges(stored.get("i")), duration=duration),
        "duration": duration,
        "end_reached": bool(stored.get("r")),
    }


def record(
    db: Any,
    video_path: str | Path,
    intervals: Iterable[Interval] = (),
    *,
    duration: float = 0.0,
    end_reached: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
    """Adds parts seen on a device (and whether it reached the end); returns the stored union.

    Read, merge and write are one transaction: the iPhone's send and an mpv heartbeat
    arriving together each read the old union, and the later write dropped the other's part.
    """
    scope = getattr(db, "connection_scope", None)
    if scope is None:
        return _record(db, video_path, intervals, duration=duration, end_reached=end_reached, now=now)
    with scope() as conn:
        if not conn.in_transaction:  # inside a caller's transaction (mpv's save) it joins that
            conn.execute("BEGIN IMMEDIATE")
        return _record(db, video_path, intervals, duration=duration, end_reached=end_reached, now=now)


def _record(
    db: Any,
    video_path: str | Path,
    intervals: Iterable[Interval],
    *,
    duration: float,
    end_reached: bool,
    now: float | None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    current = load(db, video_path, now=now)
    duration = float(duration or 0.0) or current["duration"]
    merged = merge([*current["intervals"], *intervals], duration=duration)
    reached = bool(end_reached or current["end_reached"])
    if not merged and not reached:
        return current
    db.set_state(
        _key(video_path),
        json.dumps(
            {"i": [[round(a, 3), round(b, 3)] for a, b in merged], "d": duration, "r": reached, "u": now},
            separators=(",", ":"),
        ),
    )
    _prune(db, now)
    return {"intervals": merged, "duration": duration, "end_reached": reached}


def _prune(db: Any, now: float) -> None:
    from .database import STATE_PREFIX_SQL, state_prefix_bounds

    library = str(getattr(db, "path", "") or id(db))
    last = _LAST_PRUNE.get(library)
    if last is not None and abs(now - last) < PRUNE_INTERVAL_SECONDS:
        return
    _LAST_PRUNE[library] = now
    try:
        with db.connect() as conn:
            conn.execute(
                # The key index, not a scan of the whole table.
                f"DELETE FROM state WHERE {STATE_PREFIX_SQL} AND updated_at<?",
                (*state_prefix_bounds(STATE_PREFIX), now - EXPIRE_SECONDS),
            )
    except Exception:  # noqa: BLE001 - housekeeping only
        pass


def coverage(db: Any, video_path: str | Path) -> float:
    return covered_seconds(load(db, video_path)["intervals"])


def clear(db: Any, video_path: str | Path) -> None:
    """The watch counted: the next watch (a rewatch) starts from nothing."""
    try:
        db.delete_state(_key(video_path))
    except Exception:  # noqa: BLE001 - never blocks counting
        pass


def reaches_end(position: float, intervals: Iterable[Interval], duration: float) -> bool:
    duration = float(duration or 0.0)
    if duration <= 0:
        return False
    mark = duration * END_FRACTION
    return float(position or 0.0) >= mark or any(end >= mark for _start, end in intervals)


def record_mpv_heartbeat(
    db: Any, episode: Any, *, position: float, duration: float, active_seconds: float
) -> None:
    """Called before the heartbeat replaces the stored place (``episode`` is the row before it)."""
    if episode is None or str(getattr(episode, "state", "") or "") == "watched":
        return
    part = mpv_interval(getattr(episode, "playback_position", None), position, active_seconds)
    if part is None:
        return
    record(db, episode.video_path, [part], duration=duration)
