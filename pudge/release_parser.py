"""One release/file-name parser for Pudge, built on Anitomy (via ``anitopy``).

``parse_release_name`` returns a :class:`ParsedRelease` with everything the
name says (title, group, season, episode(s), version, resolution, source,
codecs, checksum, type) plus *safety* decisions made once for every caller:

* ``episode`` is an ``int`` only when the name identifies exactly one regular
  episode.  A range (``01-12``, ``01 ~ 25``), a fractional number (``12.5``),
  an extra (NCOP/NCED/PV/...) or a special (SP) gives ``episode=None`` — never
  ``12.5 -> 12``, ``01-12 -> 1`` or ``v2 -> 2``.
* ``episode_range`` keeps an explicit span; ``raw_episodes`` keeps the text.

``anitopy`` is the pure-Python port of the original C++ Anitomy (MPL-2.0, no
compiled code, so it installs identically on arm64 and x86_64).  When it is
missing or raises, a small legacy fallback fills only the safety fields and
``parser_source`` says ``"legacy"``; callers keep their own historical parsers
as the primary result in that case.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

try:  # optional at import time: the app must start without it
    import anitopy as _anitopy
except Exception:  # noqa: BLE001 - any import failure means "not available"
    _anitopy = None

PARSER_VERSION = "anitomy-adapter-1"

# Anitomy ``anime_type`` values that are not numbered story episodes.
EXTRA_TYPES = {
    "ncop", "nced", "op", "ed", "opening", "ending", "pv", "preview", "cm",
    "menu", "trailer", "teaser", "commercial", "promo", "cd",
}
SPECIAL_TYPES = {"sp", "special", "specials", "sps", "omake", "picture drama", "recap"}
_RANGE_TAIL_RE = re.compile(r"^\s*[~\-–—]\s*0*(\d{1,4})\b")
_JP_EPISODE_RE = re.compile(r"第\s*0*(\d{1,4})\s*[話回]")
_LEGACY_RANGE_RE = re.compile(
    r"(?i)(?:^|[\s(\[_])(?:ep?\s*)?0*(\d{1,3})\s*(?:-|~|–|—|to)\s*(?:ep?\s*)?0*(\d{1,3})(?=$|[\s)\]_])"
)


@dataclass(frozen=True)
class ParsedRelease:
    raw_name: str
    title: str = ""
    group: str = ""
    season: int | None = None
    episode: int | None = None
    episode_range: tuple[int, int] | None = None
    raw_episodes: tuple[str, ...] = ()
    fractional: bool = False
    special: bool = False
    extra: bool = False
    anime_type: str = ""
    version: int | None = None
    resolution: str = ""
    source: str = ""
    video_codecs: tuple[str, ...] = ()
    audio: tuple[str, ...] = ()
    checksum: str = ""
    year: int | None = None
    episode_title: str = ""
    parser_source: str = "legacy"
    warnings: tuple[str, ...] = field(default=())
    # The episode number sits behind an explicit marker ([03], - 03, E03,
    # Episode 3, S01E03, 第3話), not a bare number that may be part of a title.
    explicit_episode: bool = False

    @property
    def unsafe_single_episode(self) -> bool:
        """True when the name must not be read as one regular episode."""
        return bool(self.episode_range or self.fractional or self.special or self.extra)


def anitomy_available() -> bool:
    return _anitopy is not None


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value).strip() else []


def _int_or_none(value: Any) -> int | None:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _legacy_only(name: str, warnings: list[str]) -> ParsedRelease:
    match = _LEGACY_RANGE_RE.search(re.sub(r"\[[^\]]*\]", " ", name))
    span = None
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if 1 <= start < end <= 500:
            span = (start, end)
    jp = _JP_EPISODE_RE.search(name)
    return ParsedRelease(
        raw_name=name,
        episode=int(jp.group(1)) if jp and span is None else None,
        episode_range=span,
        explicit_episode=bool(jp and span is None),
        parser_source="legacy",
        warnings=tuple(warnings),
    )


@lru_cache(maxsize=8192)
def parse_release_name(name: str) -> ParsedRelease:
    text = unicodedata.normalize("NFC", str(name or "")).strip()
    warnings: list[str] = []
    if _anitopy is None:
        warnings.append("anitomy_unavailable")
        return _legacy_only(text, warnings)
    try:
        data = _anitopy.parse(text) or {}
    except Exception as exc:  # noqa: BLE001 - a parser crash must not break search
        warnings.append(f"anitomy_error:{type(exc).__name__}")
        return _legacy_only(text, warnings)

    raw_episodes = tuple(_as_list(data.get("episode_number")))
    anime_type = " ".join(_as_list(data.get("anime_type")))
    type_key = anime_type.casefold().strip()
    episode_title = " ".join(_as_list(data.get("episode_title")))

    episode: int | None = None
    span: tuple[int, int] | None = None
    fractional = False
    if len(raw_episodes) >= 2:
        numbers = [_int_or_none(value) for value in raw_episodes]
        if all(number is not None for number in numbers):
            start, end = min(numbers), max(numbers)  # type: ignore[type-var]
            if start != end:
                span = (int(start), int(end))
            else:
                episode = int(start)
        else:
            fractional = any("." in value for value in raw_episodes)
            warnings.append("unparsed_episode_list")
    elif raw_episodes:
        value = raw_episodes[0]
        if value.isdigit():
            episode = int(value)
        elif re.fullmatch(r"\d+\.\d+", value):
            fractional = True
            warnings.append("fractional_episode")
        else:
            warnings.append("non_numeric_episode")

    # Anitomy misses "01 ~ 25": it reports episode 01 and title "~ 25".
    tail = _RANGE_TAIL_RE.match(episode_title) if episode is not None else None
    if tail is not None and int(tail.group(1)) > episode:
        span = (episode, int(tail.group(1)))
        episode = None
        warnings.append("range_from_episode_title")

    if episode is None and span is None and not fractional:
        jp = _JP_EPISODE_RE.search(text)
        if jp:
            episode = int(jp.group(1))
            warnings.append("japanese_episode_marker")

    # Anitomy can consume a numeric title ("Ranma 1-2") as an episode
    # range. A year between that title and a separate final episode marker
    # proves the range belongs to the title, rather than to this file.
    final_marker = re.search(r"\((?:19|20)\d{2}\)\s+[-–—]\s*(\d{1,4})(?:v\d+)?(?=$|[\s\[(.])", text)
    if span is not None and final_marker is not None and not fractional:
        episode = int(final_marker.group(1))
        span = None
        raw_episodes = (final_marker.group(1),)
        warnings.append("explicit_episode_after_title_year")

    extra = any(part in EXTRA_TYPES for part in re.split(r"[\s/]+", type_key) if part)
    special = any(part in SPECIAL_TYPES for part in re.split(r"[\s/]+", type_key) if part)
    if type_key in {"ova", "ona", "oad"}:
        warnings.append("ova_numbering_may_differ")
    if span is not None or fractional or extra or special:
        episode = None

    explicit = False
    if episode is not None:
        number = rf"0*{episode}(?:v\d+)?"
        explicit = bool(
            re.search(rf"\[\s*(?:E|EP)?\s*{number}\s*\]", text, re.IGNORECASE)
            or re.search(rf"(?:^|\s)[-–—]\s*{number}(?=$|[\s\[(._])", text)
            or re.search(rf"(?i)(?:\bS\d{{1,2}}E|\bEP?\.?\s*|\bEpisode\s*|\d+x){number}(?!\d)", text)
            or re.search(rf"第\s*{number}\s*[話回]", text)
        )

    season_values = [_int_or_none(value) for value in _as_list(data.get("anime_season"))]
    seasons = sorted({value for value in season_values if value is not None})
    season = seasons[0] if len(seasons) == 1 else None
    if len(seasons) > 1:
        warnings.append("multiple_seasons")

    return ParsedRelease(
        raw_name=text,
        title=str(data.get("anime_title") or "").strip(),
        group=str(data.get("release_group") or "").strip(),
        season=season,
        episode=episode,
        episode_range=span,
        raw_episodes=raw_episodes,
        fractional=fractional,
        special=special,
        extra=extra,
        anime_type=anime_type,
        version=_int_or_none(data.get("release_version")),
        resolution=str(data.get("video_resolution") or "").strip(),
        source=" ".join(_as_list(data.get("source"))),
        video_codecs=tuple(_as_list(data.get("video_term"))),
        audio=tuple(_as_list(data.get("audio_term"))),
        checksum=str(data.get("file_checksum") or "").strip(),
        year=_int_or_none(data.get("anime_year")),
        episode_title=episode_title,
        parser_source="anitomy",
        warnings=tuple(warnings),
        explicit_episode=explicit,
    )
