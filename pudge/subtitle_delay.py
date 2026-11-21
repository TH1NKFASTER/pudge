"""The subtitle delay set in mpv (z/x), remembered per video and subtitle.

mpv forgets ``sub-delay`` when it closes, so a correction made during one
episode was needed again on every launch. The mpv script sends the delay with
each playback save; it is stored per video together with the identity of the
subtitle it was set for (the file mpv played and its size, or the embedded
track). The next launch passes ``--sub-delay`` only while that same subtitle is
played: a re-prepared subtitle is another file, so its delay starts at zero.
The playback copy mpv gets is named after its source's stat, so the same
prepared subtitle keeps one name even when the copy is made again.

Carried delay: a release is usually off by the same amount in every episode.
When the viewer set a delay by hand on two or more other episodes of the same
anime and release (same video release group, same subtitle family) and those
delays agree, an episode without a stored delay starts with that delay. The
record is marked ``origin: carried`` (never evidence for further episodes);
setting it back to zero stores ``origin: carry_reset`` so it is not suggested
again. The delay is relative to the subtitles Pudge prepared.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# mpv accepts any value; anything beyond a few minutes is a typo or a stuck key.
MAX_SUBTITLE_DELAY_SECONDS = 600.0


# Carried delay: how many other episodes must agree, and how closely.
CARRY_MIN_EPISODES = 2
CARRY_TOLERANCE_SECONDS = 0.15
# A carried delay still unchanged this long after the launch was kept by the viewer.
CARRY_KEPT_AFTER_SECONDS = 120.0
ORIGIN_CARRIED = "carried"
ORIGIN_CARRY_RESET = "carry_reset"


def _state_key(video: Path) -> str:
    return "subtitle_delay:v1:" + hashlib.sha256(str(Path(video)).encode("utf-8")).hexdigest()


state_key = _state_key


def subtitle_identity(subtitle: Path | str | None, embedded_sid: int | None = None) -> str:
    """What the delay belongs to: an external file (path and size), or an embedded track number."""
    text = str(subtitle or "").strip()
    if text:
        path = Path(text).expanduser()
        try:
            return f"file:{path.resolve()}:{int(path.stat().st_size)}"
        except OSError:
            return ""
    if embedded_sid is not None:
        try:
            return f"sid:{int(embedded_sid)}"
        except (TypeError, ValueError):
            return ""
    return ""


def remember_subtitle_delay(
    db: Any,
    video: Path,
    seconds: float | None,
    *,
    subtitle: Path | str | None = None,
    embedded_sid: int | None = None,
    source: str = "",
) -> None:
    """Store (or, at zero, forget) the delay mpv reported for ``video``.

    ``source``: ``subtitle_identity`` of the library row's subtitle the played one was made
    from (mpv plays a cleaned copy): the iPhone/iPad player shows that subtitle, so it takes
    the same delay, and a delay set there reaches the next mpv launch of the same subtitle.
    """
    if seconds is None:
        return
    value = float(seconds)
    if not math.isfinite(value):
        return
    key = _state_key(video)
    identity = subtitle_identity(subtitle, embedded_sid)
    if not identity:
        return
    try:
        previous = json.loads(db.get_state(key, "") or "{}")
    except (TypeError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    same_subtitle = previous.get("subtitle") == identity or bool(source and previous.get("source") == source)
    origin = str(previous.get("origin") or "") if same_subtitle else ""
    if abs(value) < 0.0005:
        # Zero for this subtitle forgets its delay; a delay stored for another
        # subtitle (the next launch may play it again) stays.
        if origin == ORIGIN_CARRY_RESET:
            return
        if origin == ORIGIN_CARRIED:
            # The carried delay was declined: zero is remembered, so it is not suggested again.
            payload: dict[str, Any] = {
                "seconds": 0.0,
                "subtitle": identity,
                "origin": ORIGIN_CARRY_RESET,
                "carried_seconds": previous.get("seconds"),
                "updated_at": time.time(),
            }
            if source:
                payload["source"] = source
            db.set_state(key, json.dumps(payload, ensure_ascii=False, sort_keys=True))
            _log_result(video, "reset", previous=_number(previous.get("seconds")), seconds=0.0)
        elif same_subtitle:
            db.delete_state(key)
        return
    value = round(max(-MAX_SUBTITLE_DELAY_SECONDS, min(MAX_SUBTITLE_DELAY_SECONDS, value)), 3)
    if origin == ORIGIN_CARRIED and previous.get("seconds") == value:
        # Heartbeats repeat the carried delay: it stays "carried" (not the viewer's own).
        carried_at = _number(previous.get("carried_at"))
        if not previous.get("kept") and time.time() - carried_at >= CARRY_KEPT_AFTER_SECONDS:
            previous["kept"] = True
            db.set_state(key, json.dumps(previous, ensure_ascii=False, sort_keys=True))
            _log_result(video, "kept", seconds=value)
        return
    if (
        previous.get("subtitle") == identity
        and previous.get("seconds") == value
        and str(previous.get("source") or "") == source
    ):
        return  # every heartbeat repeats it
    payload = {"seconds": value, "subtitle": identity, "updated_at": time.time()}
    if source:
        payload["source"] = source
    db.set_state(key, json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if origin == ORIGIN_CARRIED:
        _log_result(video, "adjusted", previous=_number(previous.get("seconds")), seconds=value)


def remembered_subtitle_delay(
    db: Any,
    video: Path,
    *,
    subtitle: Path | str | None = None,
    embedded_sid: int | None = None,
    source: str = "",
) -> float | None:
    """The stored delay when it was set for this exact subtitle (or for a subtitle made from
    the same library subtitle ``source``, see remember_subtitle_delay), else None."""
    payload = _stored(db, video)
    if payload is None:
        return None
    identity = subtitle_identity(subtitle, embedded_sid)
    matches = bool(identity) and payload.get("subtitle") == identity
    if not matches and source:
        matches = source in {payload.get("source"), payload.get("subtitle")}
    if not matches:
        return None
    try:
        value = float(payload.get("seconds"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or abs(value) < 0.0005:
        return None
    return max(-MAX_SUBTITLE_DELAY_SECONDS, min(MAX_SUBTITLE_DELAY_SECONDS, value))


def _stored(db: Any, video: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(db.get_state(_state_key(video), "") or "{}")
    except (TypeError, ValueError, AttributeError):
        return None
    return payload if isinstance(payload, dict) else None


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _log_result(video: Path, outcome: str, **fields: Any) -> None:
    """One RESULT line per carried delay and what became of it (kept, adjusted, reset)."""
    try:
        from .logging_utils import configure_logging

        details = " ".join(
            f"{name}={value:.3f}" if isinstance(value, float) else f"{name}={value!r}"
            for name, value in fields.items()
        )
        configure_logging().info(
            "RESULT step=mpv.sub_delay_carry video=%s outcome=%s %s", Path(video).name, outcome, details
        )
    except Exception:  # noqa: BLE001 - a log line never blocks playback or a save
        pass


_CRC_RE = re.compile(r"[\[(][0-9a-f]{8}[\])]", re.IGNORECASE)


def name_family(name: str) -> str:
    """A file name without what changes per episode: "[Grp] Show - 03 [1A2B3C4D].srt" and
    "[Grp] Show - 04 [99887766].srt" are one family."""
    text = _CRC_RE.sub("", str(name or "").casefold())
    text = re.sub(r"\d+", "#", text)
    return re.sub(r"\s+", " ", text).strip()


def release_identity(
    video_name: str,
    *,
    release_group: str = "",
    subtitle_source: str = "",
    raw_subtitle_name: str = "",
    embedded_sid: int | None = None,
) -> str:
    """Video release + subtitle family, or "" when either is unknown (then nothing is carried).

    ``subtitle_source``: what the prepared subtitle was made from ("external", "embedded",
    "ocr"); ``raw_subtitle_name``: the name of the downloaded file (it carries the provider's
    or group's naming).
    """
    group = str(release_group or "").strip().casefold()
    video_part = f"group:{group}" if group else f"name:{name_family(Path(str(video_name)).name)}"
    if video_part == "name:":
        return ""
    kind = str(subtitle_source or "").strip().casefold()
    if kind == "embedded" and embedded_sid is not None:
        subtitle_part = f"embedded:{int(embedded_sid)}"
    elif kind == "ocr":
        subtitle_part = "ocr"
    elif kind == "external" and name_family(raw_subtitle_name):
        subtitle_part = "external:" + name_family(raw_subtitle_name)
    else:
        return ""
    return f"{video_part}|{subtitle_part}"


def agreed_delay(values: Iterable[float]) -> float | None:
    """The delay to carry: at least two episodes, all within the tolerance, not zero."""
    delays = [float(value) for value in values if math.isfinite(float(value))]
    if len(delays) < CARRY_MIN_EPISODES:
        return None
    if max(delays) - min(delays) > CARRY_TOLERANCE_SECONDS + 1e-9:
        return None
    value = round(statistics.median(delays), 3)
    return None if abs(value) < 0.0005 else value


def _row_subtitle_identity(row: Any) -> str:
    subtitle = getattr(row, "subtitle_path", None)
    if subtitle is not None:
        return subtitle_identity(subtitle)
    return subtitle_identity(None, getattr(row, "embedded_subtitle_id", None))


def carried_delay_suggestion(
    db: Any, episode: Any, rows: Iterable[Any], *, release_of: Callable[[Any], str]
) -> tuple[float, int, str] | None:
    """(seconds, agreeing episodes, release) for ``episode``, or None.

    ``rows``: the library rows of the same anime. ``release_of(row)``: its release_identity.
    Evidence is a delay the viewer set by hand (or an explicit zero after a declined carry)
    that still belongs to the row's current subtitle; a carried delay is not evidence.
    """
    video = Path(episode.video_path)
    if _stored(db, video):
        # A delay for this video (even for another subtitle), or a declined carry: never overridden.
        return None
    release = release_of(episode)
    if not release:
        return None
    by_key = {_state_key(Path(row.video_path)): row for row in rows if Path(row.video_path) != video}
    if len(by_key) < CARRY_MIN_EPISODES:
        return None
    states: dict[str, str] = {}
    keys = tuple(by_key)
    for index in range(0, len(keys), 400):  # SQLite caps the variables of one query
        states.update(db.get_states(keys[index : index + 400]))
    delays: dict[object, float] = {}
    for key, raw in states.items():
        row = by_key.get(key)
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, ValueError):
            continue
        if row is None or not isinstance(payload, dict) or not payload:
            continue
        origin = str(payload.get("origin") or "")
        if origin == ORIGIN_CARRIED:
            continue
        identity = _row_subtitle_identity(row)
        if not identity or identity not in {payload.get("source"), payload.get("subtitle")}:
            continue  # set for a subtitle the episode no longer has
        try:
            seconds = float(payload.get("seconds"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(seconds) or release_of(row) != release:
            continue
        number = getattr(row, "media_episode", None)
        delays[number if number is not None else str(row.video_path)] = seconds
    value = agreed_delay(delays.values())
    if value is None:
        return None
    return value, len(delays), release


def remember_carried_delay(
    db: Any,
    video: Path,
    seconds: float,
    *,
    subtitle: Path | str | None = None,
    embedded_sid: int | None = None,
    source: str = "",
    episodes: int = 0,
    release: str = "",
) -> bool:
    """Store the delay this launch carries; False when the subtitle has no identity."""
    identity = subtitle_identity(subtitle, embedded_sid)
    if not identity:
        return False
    now = time.time()
    payload: dict[str, Any] = {
        "seconds": round(float(seconds), 3),
        "subtitle": identity,
        "origin": ORIGIN_CARRIED,
        "carried_at": now,
        "updated_at": now,
    }
    if source:
        payload["source"] = source
    db.set_state(_state_key(video), json.dumps(payload, ensure_ascii=False, sort_keys=True))
    _log_result(video, "carried", seconds=float(payload["seconds"]), episodes=int(episodes), release=release)
    return True
