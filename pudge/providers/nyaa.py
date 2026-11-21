from __future__ import annotations

import base64
import binascii

import json
import logging
import math
import os
import re
import subprocess
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import asdict, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from ..filename import fold_search_title, normalize_title, title_similarity, with_title_memo
from ..fsutil import atomic_write_json
from ..release_parser import double_episode_span, parse_release_name
from ..episode_numbering import EpisodeNumbering, is_split_stage, match_release_episode
from .seadex import SeaDexRecommendation, SeaDexTorrent, bonus_for
from ..branding import APP_SLUG
from ..manager_models import LibraryAnime, NyaaRelease
from ..i18n import err
from ..url_credentials import url_for_log
from .base import CircuitBreaker, ResponseTooLarge, read_capped_body

_LOGGER = logging.getLogger(__name__)


# The RSS search format's extension elements live in a namespace ending with
# this path, on whatever host serves the feed (mirrors use their own host).
NYAA_NS_SUFFIX = "/xmlns/nyaa"
MIRROR_BACKOFF_SECONDS = 300.0
_MIRROR_BACKOFF: dict[str, float] = {}
# One breaker per host (scheme://host) and route (a proxy URL or "direct") for the
# whole process: every search makes a new client, so breakers kept per client never
# tripped across searches. Per route: a failing direct route must not pause the proxy.
_HOST_BREAKERS: dict[tuple[str, str], CircuitBreaker] = {}
# host -> monotonic time until which a 429 (or a 503 with Retry-After) asked for no request.
_HOST_RETRY_AFTER: dict[str, float] = {}
_HOST_STATE_LOCK = threading.Lock()
RATE_LIMIT_DEFAULT_SECONDS = 60.0
RATE_LIMIT_MAX_SECONDS = 900.0


def _host_key(url: str) -> str:
    parsed = urlparse(str(url or ""))
    return f"{parsed.scheme}://{parsed.netloc}".casefold()


def _retry_after_seconds(value: str | None) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _note_rate_limit(url: str, exc: BaseException) -> None:
    """Remember a 429/503 answer's Retry-After for the host (no request until then)."""
    response = getattr(exc, "response", None) if isinstance(exc, httpx.HTTPStatusError) else None
    status = int(getattr(response, "status_code", 0) or 0)
    if status not in {429, 503}:
        return
    seconds = _retry_after_seconds(response.headers.get("Retry-After") if response is not None else None)
    if seconds is None:
        if status != 429:
            return  # a plain 503 is an outage: the breaker handles it
        seconds = RATE_LIMIT_DEFAULT_SECONDS
    until = time.monotonic() + min(RATE_LIMIT_MAX_SECONDS, seconds)
    with _HOST_STATE_LOCK:
        _HOST_RETRY_AFTER[_host_key(url)] = max(_HOST_RETRY_AFTER.get(_host_key(url), 0.0), until)


def _rate_limit_wait(url: str) -> float:
    with _HOST_STATE_LOCK:
        until = _HOST_RETRY_AFTER.get(_host_key(url), 0.0)
    return max(0.0, until - time.monotonic())


GROUP_RE = re.compile(r"^\s*\[([^\]]+)\]")
SUFFIX_GROUP_RE = re.compile(r"-(?P<group>[A-Za-z][A-Za-z0-9._-]{1,31})(?:\s+\([^)]*\))?\s*$")
FRESH_TRUSTED_ZERO_SEEDER_MAX_HOURS = 24.0
# Manual searches: a few RSS queries in parallel, one overall deadline, and the
# same query answered from memory for 5 minutes (automatic searches keep their
# sequential, budgeted path and always read fresh results).
MANUAL_SEARCH_WORKERS = 3
MANUAL_SEARCH_DEADLINE_SECONDS = 30.0
MANUAL_QUERY_CACHE_SECONDS = 300.0
_MANUAL_QUERY_CACHE: dict[tuple[str, ...], tuple[float, list[NyaaRelease]]] = {}
_MANUAL_QUERY_CACHE_LOCK = threading.Lock()
EPISODE_PATTERNS = (
    re.compile(r"(?i)\bS\d{1,2}E(?P<ep>\d{1,4})\b"),
    re.compile(r"(?i)\bE(?:P(?:ISODE)?)?[ ._-]?(?P<ep>\d{1,4})\b"),
    re.compile(r"(?:^|[\s._-])-(?:[\s._-])*(?P<ep>\d{1,4})(?:v\d+)?(?:[\s._-]|$)"),
    re.compile(r"(?:^|[\s._-])(?P<ep>\d{2,4})(?:v\d+)?(?:[\s._-]|$)"),
)
BATCH_RE = re.compile(r"(?i)\b(batch|complete|全集|全話|season\s*pack)\b")
VOLUME_MARKER_RE = re.compile(r"(?i)\bvol(?:ume)?[ ._-]*0*\d{1,3}\b")
EPISODE_RANGE_PATTERNS = (
    re.compile(r"(?i)\bS\d{1,2}E(?P<start>\d{1,3})\s*[-~]\s*(?:S\d{1,2}E)?(?P<end>\d{1,3})\b"),
    re.compile(
        r"(?i)\bE(?:P(?:ISODES?)?)?[ ._-]*0*(?P<start>\d{1,3})\s*[-~]\s*"
        r"(?:E(?:P(?:ISODES?)?)?[ ._-]*)?0*(?P<end>\d{1,3})\b"
    ),
    re.compile(
        r"(?i)(?:^|[\s\[(._-])0*(?P<start>\d{1,3})\s*[-~]\s*0*(?P<end>\d{1,3})"
        r"(?=$|[\s\])._-])"
    ),
)
RESOLUTION_RE = re.compile(r"(?i)\b(2160p|1440p|1080p|720p|480p)\b")
SEASON_PATTERNS = (
    re.compile(r"(?i)\bS(?:eason)?(?:[ ._]*|-(?!\s))0*(?P<season>\d{1,2})(?:\b|(?=E\d))"),
    re.compile(r"(?i)\b0*(?P<season>\d{1,2})(?:st|nd|rd|th)[ ._-]+Season\b"),
    re.compile(r"(?i)\bSeason(?:[ ._]*|-(?!\s))0*(?P<season>\d{1,2})\b"),
    re.compile(r"第\s*0*(?P<season>\d{1,2})\s*期"),
)
SEASON_RANGE_PATTERNS = (
    # Compact forms where the first ``S`` makes the range unambiguous.
    # ``S01-04`` / ``S01~04``.
    re.compile(r"(?i)\bS0*(?P<start>\d{1,2})(?:-|~|–|—)0*(?P<end>\d{1,2})\b"),
    # Explicit marker on both sides permits whitespace around the separator.
    # ``S1 - S4`` / ``Season 1 - Season 4`` / ``Season 1 - S4``.
    re.compile(
        r"(?i)\bS(?:eason)?[ ._-]*0*(?P<start>\d{1,2})\s*"
        r"(?:-|~|–|—|\bto\b)\s*"
        r"S(?:eason)?[ ._-]*0*(?P<end>\d{1,2})\b"
    ),
    # ``Season 1-4`` is common, but only accept the no-second-marker form when
    # the range separator is compact. This deliberately does NOT match
    # ``Season 1 - 02``, which is a common season + episode spelling.
    re.compile(r"(?i)\bSeason[ ._-]*0*(?P<start>\d{1,2})(?:-|~|–|—)0*(?P<end>\d{1,2})\b"),
)
ROMAN_SEASONS = {
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
}
SEASON_WORDS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}
STAGE_SPLIT_SUFFIX_RE = re.compile(
    r"(?i)\s*[-:]\s*"
    r"\d{1,2}(?:st|nd|rd|th)"
    r"(?:\s*(?:&|＆|and|[-–—])\s*\d{1,2}(?:st|nd|rd|th))?"
    r"\s+STAGE\s*$"
)


class NyaaError(RuntimeError):
    pass


class ReleaseSearchUnavailableError(NyaaError):
    """Every query of one search failed (network, paused hosts): not "nothing found"."""


class NoReleaseSourceError(NyaaError):
    """No release search is set up in Settings → Sources (Pudge ships with none)."""


def _resolve_url(value: str | Callable[[], str]) -> str:
    # Feed clients live as long as the manager; the address can change in
    # Settings at any time, so it may be given as a getter.
    return str((value() if callable(value) else value) or "").strip()


def _release_history_key(item: NyaaRelease) -> str:
    return (
        str(item.info_hash or "").strip().lower()
        or str(item.torrent_url or "").strip()
        or str(item.link or "").strip()
        or f"{item.title}|{item.published}"
    )


def _load_release_history(path: Path | None) -> list[NyaaRelease]:
    if path is None or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []
    rows = payload.get("releases") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    allowed = set(NyaaRelease.__dataclass_fields__)
    result: list[NyaaRelease] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            result.append(NyaaRelease(**{key: row[key] for key in allowed if key in row}))
        except (TypeError, ValueError, KeyError):
            continue
    return result


def _merge_release_history(
    path: Path | None,
    current: list[NyaaRelease],
    *,
    source: str,
    limit: int = 5000,
) -> list[NyaaRelease]:
    previous = _load_release_history(path)
    merged: dict[str, NyaaRelease] = {}
    for item in [*current, *previous]:
        key = _release_history_key(item)
        if key and key not in merged:
            merged[key] = item
    rows = list(merged.values())[: max(100, int(limit))]
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": "pudge-release-feed-history-v1",
                "source": source,
                "updated_at": time.time(),
                "releases": [
                    {
                        **asdict(item),
                        "source": source,
                        "episode": release_episode(item.title),
                    }
                    for item in rows
                ],
            }
            atomic_write_json(path, payload, ensure_ascii=False, separators=(",", ":"))
        except OSError:
            pass
    return rows


def _magnet_info_hash(value: str) -> str:
    if not value.casefold().startswith("magnet:?"):
        return ""
    try:
        params = parse_qs(urlparse(value).query)
    except ValueError:
        return ""
    for xt in params.get("xt", []):
        prefix = "urn:btih:"
        if xt.casefold().startswith(prefix):
            info_hash = xt[len(prefix) :].strip().lower()
            # BEP 9 permits btih values as either 40 hexadecimal characters or
            # 32 base32 characters. aria2/qBittorrent expose the canonical hex
            # form through their APIs, so normalize the RSS value before it is
            # used for add verification and duplicate detection.
            if re.fullmatch(r"[0-9a-f]{40}", info_hash):
                return info_hash
            if re.fullmatch(r"[a-z2-7]{32}", info_hash):
                try:
                    return base64.b32decode(info_hash.upper()).hex()
                except (binascii.Error, ValueError):
                    pass
            return info_hash
    return ""


def parse_subsplease_rss(xml_text: str) -> list[NyaaRelease]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NyaaError(err("nyaa.subspleaseInvalidRss", error=exc)) from exc

    releases: list[NyaaRelease] = []
    for item in root.findall("./channel/item"):
        title = _text(item, "title")
        link = _text(item, "link")
        guid = _text(item, "guid")
        enclosure = item.find("enclosure")
        enclosure_url = (enclosure.attrib.get("url") or "").strip() if enclosure is not None else ""
        description = _text(item, "description")
        description_magnet = re.search(r"magnet:\?[^\s<\"]+", description)
        candidates = [link, guid, enclosure_url, description_magnet.group(0) if description_magnet else ""]
        download_url = next(
            (value for value in candidates if value.casefold().startswith("magnet:?")),
            enclosure_url or link or guid,
        )
        if not title or not download_url:
            continue
        size_text = ""
        size_match = re.search(
            r"(?i)([0-9]+(?:\.[0-9]+)?\s*(?:B|KiB|MiB|GiB|TiB|KB|MB|GB|TB))",
            description,
        )
        if size_match:
            size_text = size_match.group(1)
        releases.append(
            NyaaRelease(
                title=title,
                link=link or guid or download_url,
                torrent_url=download_url,
                info_hash=_magnet_info_hash(download_url),
                size_text=size_text,
                size_bytes=parse_size(size_text),
                seeders=0,
                leechers=0,
                downloads=0,
                trusted=True,
                remake=False,
                category_id="subsplease-rss",
                published=_text(item, "pubDate"),
                is_batch=release_title_is_pack(title),
                group="SubsPlease",
            )
        )
    return releases


# A release feed whose request failed is asked again only after this long. Every
# release search starts with the feeds, so while a feed's host was down or blocked each
# search (a background pass: title after title) requested it again and waited out the
# timeout; meanwhile the kept history answers, as it did for each failure.
FEED_RETRY_SECONDS = 120.0


def _feed_history_or_raise(
    history_path: Path | None, message: str, cause: BaseException | None = None
) -> list[NyaaRelease]:
    """The kept feed history after a failed (or recently failed) request, else NyaaError."""
    cached = _load_release_history(history_path)
    if cached:
        return cached
    raise NyaaError(message) from cause


class SubsPleaseClient:
    """The weekly releases feed (Settings → Sources); off without an address."""

    def __init__(
        self,
        *,
        base_url: str | Callable[[], str] = "",
        timeout: float = 8.0,
        cache_ttl: float = 300.0,
        history_path: Path | None = None,
    ) -> None:
        self._base_url = base_url
        self.timeout = timeout
        self.cache_ttl = max(0.0, cache_ttl)
        self.history_path = history_path
        self._cache: dict[str, tuple[float, list[NyaaRelease]]] = {}
        # url -> (monotonic time, message) of the last failed request (FEED_RETRY_SECONDS).
        self._failed: dict[str, tuple[float, str]] = {}

    @property
    def base_url(self) -> str:
        return _resolve_url(self._base_url).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def feed_url(self, preferred_resolution: str) -> str:
        resolution = preferred_resolution.casefold().strip()
        if "720" in resolution:
            value = "720"
        elif (
            "1080" in resolution
            or resolution in {"highest", "best", "max"}
            or "1440" in resolution
            or "2160" in resolution
        ):
            # The weekly feed exposes 1080p as its highest standard feed.
            # Higher/best preferences are still applied to release search scoring.
            value = "1080"
        else:
            value = "sd"
        # Magnet RSS. Magnet entries include the info hash, which lets
        # qBittorrent verify and tag the torrent immediately after adding it.
        return f"{self.base_url}/?r={value}"

    def releases(self, preferred_resolution: str) -> list[NyaaRelease]:
        if not self.configured:
            return []
        url = self.feed_url(preferred_resolution)
        cached = self._cache.get(url)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self.cache_ttl:
            return list(cached[1])
        failed = self._failed.get(url)
        if failed is not None and now - failed[0] < FEED_RETRY_SECONDS:
            return _feed_history_or_raise(self.history_path, failed[1])
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": APP_SLUG},
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                releases = _merge_release_history(
                    self.history_path,
                    parse_subsplease_rss(response.text),
                    source="subsplease",
                )
                self._cache[url] = (time.monotonic(), releases)
                self._failed.pop(url, None)
                return list(releases)
        except httpx.HTTPError as exc:
            message = err("nyaa.subspleaseUnavailable", error=exc)
            self._failed[url] = (time.monotonic(), message)
            return _feed_history_or_raise(self.history_path, message, exc)


def parse_shana_rss(xml_text: str) -> list[NyaaRelease]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NyaaError(err("nyaa.shanaInvalidRss", error=exc)) from exc

    releases: list[NyaaRelease] = []
    for item in root.findall("./channel/item"):
        title = _text(item, "title")
        link = _text(item, "link")
        guid = _text(item, "guid")
        enclosure = item.find("enclosure")
        enclosure_url = (enclosure.attrib.get("url") or "").strip() if enclosure is not None else ""
        description = _text(item, "description")
        description_magnet = re.search(r"magnet:\?[^\s<\"]+", description)
        candidates = [enclosure_url, link, guid, description_magnet.group(0) if description_magnet else ""]
        download_url = next(
            (value for value in candidates if value.casefold().startswith("magnet:?")),
            enclosure_url or link or guid,
        )
        if not title or not download_url:
            continue
        size_text = ""
        size_match = re.search(
            r"(?i)([0-9]+(?:\.[0-9]+)?\s*(?:B|KiB|MiB|GiB|TiB|KB|MB|GB|TB))",
            description,
        )
        if size_match:
            size_text = size_match.group(1)
        hash_match = re.search(r"(?i)\bhash:\s*([0-9a-f]{8,40})\b", description)
        group = release_group(title) or ""
        releases.append(
            NyaaRelease(
                title=title,
                link=link or guid or download_url,
                torrent_url=download_url,
                info_hash=_magnet_info_hash(download_url)
                or (hash_match.group(1).lower() if hash_match else ""),
                size_text=size_text,
                size_bytes=parse_size(size_text),
                seeders=0,
                leechers=0,
                downloads=0,
                trusted=group.casefold() == "subsplease",
                remake=False,
                category_id="shana-rss",
                published=_text(item, "pubDate"),
                is_batch=release_title_is_pack(title),
                group=group,
            )
        )
    return releases


class ShanaProjectClient:
    """The public release feed (Settings → Sources); off without an address."""

    def __init__(
        self,
        *,
        feed_url: str | Callable[[], str] = "",
        timeout: float = 8.0,
        cache_ttl: float = 600.0,
        history_path: Path | None = None,
    ) -> None:
        self._feed_url = feed_url
        self.timeout = timeout
        self.cache_ttl = max(0.0, cache_ttl)
        self.history_path = history_path
        self._cache: tuple[str, float, list[NyaaRelease]] | None = None
        # url -> (monotonic time, message) of the last failed request (FEED_RETRY_SECONDS).
        self._failed: dict[str, tuple[float, str]] = {}

    @property
    def feed_url(self) -> str:
        return _resolve_url(self._feed_url)

    @property
    def configured(self) -> bool:
        return bool(self.feed_url)

    def releases(self) -> list[NyaaRelease]:
        url = self.feed_url
        if not url:
            return []
        now = time.monotonic()
        # Keyed by address: a feed changed in Settings is read fresh.
        if self._cache is not None and self._cache[0] == url and now - self._cache[1] < self.cache_ttl:
            return list(self._cache[2])
        failed = self._failed.get(url)
        if failed is not None and now - failed[0] < FEED_RETRY_SECONDS:
            return _feed_history_or_raise(self.history_path, failed[1])
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": APP_SLUG},
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                releases = _merge_release_history(
                    self.history_path,
                    parse_shana_rss(response.text),
                    source="shana",
                )
                self._cache = (url, time.monotonic(), releases)
                self._failed.pop(url, None)
                return list(releases)
        except httpx.HTTPError as exc:
            message = err("nyaa.shanaUnavailable", error=exc)
            self._failed[url] = (time.monotonic(), message)
            return _feed_history_or_raise(self.history_path, message, exc)


def parse_size(value: str) -> int:
    match = re.search(r"(?i)([0-9]+(?:\.[0-9]+)?)\s*(B|KiB|MiB|GiB|TiB|KB|MB|GB|TB)", value)
    if not match:
        return 0
    amount = float(match.group(1))
    unit = match.group(2).upper()
    powers = {
        "B": 0,
        "KIB": 1,
        "MIB": 2,
        "GIB": 3,
        "TIB": 4,
        "KB": 1,
        "MB": 2,
        "GB": 3,
        "TB": 4,
    }
    size = amount * (1024 ** powers[unit])
    # A huge number ("9" * 400 GiB) is inf here and int() raised OverflowError,
    # which broke the whole search instead of this one item.
    if not math.isfinite(size):
        return 0
    return int(size)


def _release_episode_match(title: str) -> tuple[int | None, bool]:
    """Return the parsed episode and whether the marker is explicit.

    SxxExx, E/EP markers and the common `` - 05 `` form are explicit enough
    to reject a different requested episode. A bare two-to-four digit token is
    kept as a low-confidence fallback because numbers can be part of an anime
    title (for example ``86``).
    """
    clean = re.sub(r"\[[^\]]*\]", " ", title)
    clean = VOLUME_MARKER_RE.sub(" ", clean)
    # "(BD 1080p HEVC x265 10-bit FLAC)": the bit depth is not episode 10.
    clean = re.sub(r"(?i)(?<![\w.])(?:8|10|12)[ ._-]?bits?\b", " ", clean)
    for index, pattern in enumerate(EPISODE_PATTERNS):
        matches = list(pattern.finditer(clean))
        if not matches:
            continue
        try:
            return int(matches[-1].group("ep")), index < 3
        except (TypeError, ValueError):
            continue
    return None, False


def release_episode(title: str) -> int | None:
    episode, _explicit = _release_episode_match(title)
    parsed = parse_release_name(title)
    if parsed.unsafe_single_episode:
        return None  # a range, 12.5, an extra or a special is not one episode
    if episode is None and parsed.episode is not None and parsed.explicit_episode:
        return parsed.episode  # e.g. "[Grp] Show [03] [1080p]"
    return episode


_PACK_WORD_RE = re.compile(r"(?i)\b(?:batch|complete|全集|全話|season\s*pack)\b")


def _season_episode_pair_range(
    title: str,
    anime: LibraryAnime,
    requested_episode: int,
    alternative_episodes: tuple[int, ...] = (),
) -> bool:
    """Return true when a generic ``2 - 05`` match is season + episode.

    ``alternative_episodes``: the requested episode's absolute numbers, so
    "Show 2 - 13" is cour 2's episode 1 numbered from the first cour.

    Bare sequel titles such as ``Otomege ... 2 - 05`` otherwise look like an
    episode range to the generic pack parser.  Require agreement between the
    AniList-derived expected season, the release season marker and the explicit
    episode marker before suppressing that false range.
    """
    season = _expected_season(anime)
    if season <= 1:
        # "Cyberpunk: Edgerunners 2 - 01" without a PREQUEL relation: the title's own number.
        season = _own_title_season(anime, title) or 0
    if season <= 1 or _season_number(title) != season or _PACK_WORD_RE.search(title):
        return False
    found_episode, explicit = _release_episode_match(title)
    allowed = {int(requested_episode), *(int(value) for value in alternative_episodes if int(value) > 0)}
    return bool(
        explicit
        and found_episode in allowed
        # "2 - 01" is no range at all (it runs backwards), "2 - 05" is (2, 5).
        and release_episode_range(title) in {None, (season, found_episode)}
    )


def release_double_episode(
    title: str,
    requested_episode: int | None,
    anime: LibraryAnime | None = None,
    alternative_episodes: tuple[int, ...] = (),
) -> tuple[int, int] | None:
    """Two consecutive episodes in one release starting at the requested one (audit 17А).

    ``alternative_episodes`` holds the absolute numbers of the requested
    episode, so ``13-14`` can be cour 2 episodes 1-2. Split-stage titles keep
    their per-episode identity rules and never take a double.
    """
    if requested_episode is None or (anime is not None and is_split_stage(anime)):
        return None
    span = double_episode_span(title)
    if span is None:
        return None
    if span[0] != int(requested_episode) and span[0] not in {
        int(value) for value in alternative_episodes if int(value) > 0
    }:
        return None
    return span


def _release_is_eligible_for_episode(
    title: str,
    requested_episode: int,
    anime: LibraryAnime | None = None,
    alternative_episodes: tuple[int, ...] = (),
    numbering_context: EpisodeNumbering | None = None,
) -> bool:
    """Reject explicit wrong episodes and packs before ranking.

    Nyaa may return episode 1 for a query ending in episode 5. Such a result
    must never survive to the ranking stage, regardless of seeders or group.
    Unnumbered releases remain visible for manual inspection, but the automatic
    download guard rejects them later.
    """
    # S00 is conventionally specials/extras, never the numbered TV season.
    # Reject it before scoring so S00E02 cannot masquerade as normal episode 2.
    if anime is not None and _season_number(title) == 0:
        return False
    if release_double_episode(title, requested_episode, anime, alternative_episodes) is not None:
        return True
    episode_range = release_episode_range(title)
    if episode_range is not None and not (
        anime and _season_episode_pair_range(title, anime, requested_episode, alternative_episodes)
    ):
        return False
    if BATCH_RE.search(title) and not (
        anime and _season_episode_pair_range(title, anime, requested_episode, alternative_episodes)
    ):
        return False
    if anime is not None and is_split_stage(anime):
        match = match_release_episode(
            anime,
            parse_release_name(title),
            requested_episode,
            numbering_context,
            alternative_episodes=alternative_episodes,
        )
        return match.status == "ambiguous" or (
            match.status == "resolved" and match.mapped_media_episode == int(requested_episode)
        )
    found_episode, explicit = _release_episode_match(title)
    allowed_episodes = {
        int(requested_episode),
        *(int(value) for value in alternative_episodes if int(value) > 0),
    }
    if explicit and found_episode not in allowed_episodes:
        return False
    return True


# " - 05" after a bare "1-2": an episode marker (not a year such as " - 2024").
_EPISODE_MARKER_AFTER_RE = re.compile(r"\s-\s*(?!(?:19|20)\d{2}\b)\d{1,4}(?:v\d+)?(?:[\s._~-]|$)")


def release_episode_range(title: str) -> tuple[int, int] | None:
    clean = re.sub(r"\[(?!\s*\d{1,3}\s*[-~]\s*\d{1,3}\s*\])[^\]]*\]", " ", title)
    for pattern in EPISODE_RANGE_PATTERNS:
        position = 0
        while (match := pattern.search(clean, position)) is not None:
            try:
                start = int(match.group("start"))
                end = int(match.group("end"))
            except (TypeError, ValueError):
                break
            if not 1 <= start < end <= 500:
                break
            if pattern is EPISODE_RANGE_PATTERNS[-1] and _EPISODE_MARKER_AFTER_RE.search(clean, match.end()):
                # "[Erai-raws] Ranma 1-2 (2024) - 05": the pair is part of the name, " - 05" the episode.
                position = match.end()
                continue
            return start, end
    parsed = parse_release_name(title)
    if parsed.episode_range is not None and 1 <= parsed.episode_range[0] < parsed.episode_range[1] <= 500:
        return parsed.episode_range
    return None


def release_title_is_pack(title: str) -> bool:
    """A pack word or a real (ascending) episode range; "Mob Psycho 100 - 07" is no pack."""
    return bool(BATCH_RE.search(title) or release_episode_range(title) is not None)


def _text(item: ET.Element, name: str, default: str = "") -> str:
    node = item.find(name)
    return (node.text or "").strip() if node is not None else default


def _nyaa_text(item: ET.Element, name: str, default: str = "") -> str:
    # The format's extension namespace is "<host>/xmlns/nyaa" on whichever host
    # serves the feed (mirrors use their own). Prefer an element in such a
    # namespace, then accept the same local name in any namespace.
    fallback: str | None = None
    for child in item:
        tag = str(child.tag)
        namespace, _, local_name = tag[1:].rpartition("}") if tag.startswith("{") else ("", "", tag)
        if local_name != name or child.text is None:
            continue
        if namespace.rstrip("/").endswith(NYAA_NS_SUFFIX):
            return child.text.strip()
        if fallback is None:
            fallback = child.text.strip()
    return default if fallback is None else fallback


def parse_rss(xml_text: str) -> list[NyaaRelease]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NyaaError(err("nyaa.invalidRss", error=exc)) from exc

    releases: list[NyaaRelease] = []
    for item in root.findall("./channel/item"):
        try:
            title = _text(item, "title")
            link = _text(item, "guid") or _text(item, "link")
            torrent_url = _text(item, "link")
            info_hash = _nyaa_text(item, "infoHash")
            size_text = _nyaa_text(item, "size")
            group = release_group(title)
            seeders_text = _nyaa_text(item, "seeders", "")
            seeders_known = bool(seeders_text)
            try:
                seeders = int(seeders_text or 0)
                leechers = int(_nyaa_text(item, "leechers", "0") or 0)
                downloads = int(_nyaa_text(item, "downloads", "0") or 0)
            except ValueError:
                seeders = leechers = downloads = 0
                seeders_known = False
            releases.append(
                NyaaRelease(
                    title=title,
                    link=link,
                    torrent_url=torrent_url,
                    info_hash=info_hash.lower(),
                    size_text=size_text,
                    size_bytes=parse_size(size_text),
                    seeders=seeders,
                    leechers=leechers,
                    downloads=downloads,
                    trusted=_nyaa_text(item, "trusted").casefold() in {"yes", "true", "1"},
                    remake=_nyaa_text(item, "remake").casefold() in {"yes", "true", "1"},
                    category_id=_nyaa_text(item, "categoryId"),
                    published=_text(item, "pubDate"),
                    is_batch=release_title_is_pack(title),
                    group=group,
                    seeders_known=seeders_known,
                )
            )
        except Exception as exc:
            # One malformed item used to raise out of the parser and fail the
            # whole search (and skip the mirror fallback). Skip just that item.
            _LOGGER.warning(
                "SKIP step=rss.item source=nyaa title=%r error=%r",
                _text(item, "title")[:200],
                str(exc),
            )
    return releases


class _SearchDeadline(Exception):
    """Internal: a search's deadline passed before the next request."""


class NyaaClient:
    """RSS release search of the source set in Settings → Sources, with its mirrors."""

    def __init__(
        self,
        base_url: str = "",
        *,
        mirrors: Iterable[str] = (),
        proxy_mode: str = "direct_then_proxy",
        proxy_url: str = "",
        pre_search_command: str = "",
        category: str = "1_2",
        timeout: float = 20.0,
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.search_base_urls = [self.base_url] if self.base_url else []
        for mirror in mirrors:
            mirror = str(mirror or "").strip().rstrip("/")
            if self.base_url and mirror and mirror not in self.search_base_urls:
                self.search_base_urls.append(mirror)
        self.proxy_mode = proxy_mode
        self.proxy_url = self._normalize_proxy_url(proxy_url)
        self.pre_search_command = pre_search_command.strip()
        self.category = category
        self.timeout = timeout
        # One breaker per host (scheme://host): a single breaker for the client
        # let a burst of 504s from the primary pause every mirror too, so the
        # whole search failed with "temporarily paused" for all hosts.
        self._breakers = _HOST_BREAKERS
        # base URL -> monotonic time until which a failing mirror (504/timeout)
        # is skipped while another mirror answers.  Shared by every client of
        # the process: callers create a new client per search.
        self._mirror_backoff = _MIRROR_BACKOFF
        self._clients: dict[str, httpx.Client] = {}
        self._client_lock = threading.Lock()
        # Manual searches run a few queries in parallel on one client.
        self._order_lock = threading.Lock()
        # When the command before the release search last succeeded (see _run_hook).
        self._hook_lock = threading.Lock()
        self._hook_done_at = 0.0

    @staticmethod
    def _normalize_proxy_url(value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if "://" not in value:
            value = f"socks5://{value}"
        if value.casefold().startswith("socks://"):
            value = "socks5://" + value.split("://", 1)[1]
        return value

    # One search sends up to a dozen RSS queries (a manual one three at a time), each
    # through search(): the command before the release search (a VPN connect script) ran
    # before every one of them, several copies at once. It now runs once for them; the
    # same 60 s the torrent clients allow their prepared download command.
    HOOK_REUSE_SECONDS = 60.0

    def _run_hook(self, timeout: float = 60.0) -> None:
        if not self.pre_search_command:
            return
        deadline = time.monotonic() + max(0.1, min(60.0, timeout))
        lock = self.__dict__.setdefault("_hook_lock", threading.Lock())
        with lock:
            done_at = float(getattr(self, "_hook_done_at", 0.0) or 0.0)
            if done_at and time.monotonic() - done_at < self.HOOK_REUSE_SECONDS:
                return
            # A query that waited here for another one's run keeps its own deadline.
            left = deadline - time.monotonic()
            if left <= 0:
                raise NyaaError(err("nyaa.searchTimedOut"))
            try:
                completed = subprocess.run(
                    self.pre_search_command,
                    shell=True,
                    text=True,
                    capture_output=True,
                    timeout=max(0.1, left),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                # A hung hook (e.g. a VPN connect script) used to escape search() as a raw
                # TimeoutExpired and abort the whole manual search.
                raise NyaaError(
                    err("nyaa.preSearchCommandFailed", error=f"timed out after {exc.timeout:.0f} s")
                ) from exc
            except OSError as exc:
                raise NyaaError(err("nyaa.preSearchCommandFailed", error=exc)) from exc
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise NyaaError(err("nyaa.preSearchCommandFailed", error=detail))
            self._hook_done_at = time.monotonic()

    def _client_for_proxy(self, proxy: str | None) -> httpx.Client:
        key = str(proxy or "")
        with self._client_lock:
            client = self._clients.get(key)
            if client is None:
                client = httpx.Client(
                    timeout=self.timeout,
                    follow_redirects=True,
                    proxy=proxy,
                    headers={"User-Agent": APP_SLUG},
                )
                self._clients[key] = client
        return client

    def _breaker_for(self, url: str, proxy: str | None = None) -> CircuitBreaker:
        key = (_host_key(url), str(proxy or "direct"))
        with _HOST_STATE_LOCK:
            breaker = self._breakers.get(key)
            if breaker is None:
                breaker = self._breakers[key] = CircuitBreaker(failure_threshold=3, recovery_seconds=30.0)
        return breaker

    def _admit(self, url: str, proxy: str | None = None) -> CircuitBreaker:
        wait = _rate_limit_wait(url)
        if wait > 0:
            raise NyaaError(err("nyaa.rateLimited", seconds=int(math.ceil(wait))))
        breaker = self._breaker_for(url, proxy)
        if not breaker.allow():
            raise NyaaError(err("nyaa.paused"))
        return breaker

    def _get(self, url: str, proxy: str | None, timeout: float | None = None) -> str:
        """``timeout``: a per-request cap below the client's own (the rest of a search deadline)."""
        breaker = self._admit(url, proxy)
        shortened = timeout is not None and timeout < self.timeout
        try:
            if shortened:
                response = self._client_for_proxy(proxy).get(url, timeout=max(0.1, float(timeout)))
            else:
                response = self._client_for_proxy(proxy).get(url)
            response.raise_for_status()
            breaker.success()
            return response.text
        except ImportError as exc:
            breaker.failure()
            raise NyaaError(err("nyaa.socksioMissing")) from exc
        except httpx.InvalidURL as exc:
            raise NyaaError(err("nyaa.unavailable", route=err("nyaa.routeDirect"), error=exc)) from exc
        except httpx.HTTPError as exc:
            _note_rate_limit(url, exc)
            if not (shortened and isinstance(exc, httpx.TimeoutException)):
                # A timeout cut short by the search deadline says nothing about the host.
                breaker.failure()
            route = err("nyaa.routeVia", proxy=url_for_log(proxy)) if proxy else err("nyaa.routeDirect")
            raise NyaaError(err("nyaa.unavailable", route=route, error=exc)) from exc
        except RuntimeError as exc:
            # The client was closed (close()) while this request was still on its way.
            route = err("nyaa.routeVia", proxy=url_for_log(proxy)) if proxy else err("nyaa.routeDirect")
            raise NyaaError(err("nyaa.unavailable", route=route, error=exc)) from exc

    def _get_bytes(self, url: str, proxy: str | None) -> bytes:
        breaker = self._admit(url, proxy)
        try:
            # Streamed with a byte cap and an overall deadline: the whole body
            # used to be read before the 12 MB check.
            payload = read_capped_body(self._client_for_proxy(proxy), url, max_bytes=12 * 1024 * 1024)
            if not payload:
                raise NyaaError(err("nyaa.invalidTorrent"))
            breaker.success()
            return payload
        except NyaaError:
            raise
        except ResponseTooLarge as exc:
            raise NyaaError(err("nyaa.invalidTorrent")) from exc
        except ImportError as exc:
            breaker.failure()
            raise NyaaError(err("nyaa.socksioMissing")) from exc
        except httpx.HTTPError as exc:
            _note_rate_limit(url, exc)
            breaker.failure()
            route = err("nyaa.routeVia", proxy=url_for_log(proxy)) if proxy else err("nyaa.routeDirect")
            raise NyaaError(err("nyaa.unavailable", route=route, error=exc)) from exc
        except RuntimeError as exc:
            # The client was closed (close()) while this request was still on its way.
            route = err("nyaa.routeVia", proxy=url_for_log(proxy)) if proxy else err("nyaa.routeDirect")
            raise NyaaError(err("nyaa.unavailable", route=route, error=exc)) from exc

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def fetch_torrent_payload(self, url: str) -> bytes:
        """Fetch a small .torrent file through the same release search proxy route."""
        source = str(url or "").strip()
        if not source.casefold().startswith(("http://", "https://")):
            raise NyaaError(err("nyaa.noTorrentFile"))
        mode = self.proxy_mode.casefold()
        if mode == "proxy_only":
            attempts = [self.proxy_url or None]
        elif mode == "proxy_then_direct":
            attempts = [self.proxy_url or None, None]
        elif mode == "direct":
            attempts = [None]
        else:
            attempts = [None, self.proxy_url or None]
        attempts = list(dict.fromkeys(attempts))
        errors: list[str] = []
        for proxy in attempts:
            if proxy is None and mode == "proxy_only":
                continue
            try:
                return self._get_bytes(source, proxy)
            except NyaaError as exc:
                errors.append(str(exc))
        raise NyaaError("; ".join(dict.fromkeys(errors)) or err("nyaa.invalidTorrent"))

    def close(self) -> None:
        with self._client_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()

    def search(
        self,
        query: str,
        *,
        category: str | None = None,
        filter_id: int = 0,
        sort_by: str | None = None,
        order: str | None = None,
        deadline: float | None = None,
    ) -> list[NyaaRelease]:
        """``deadline``: a ``time.monotonic()`` instant the whole call ends by.

        Without it one query could take mirrors x routes x the 20 s timeout (and
        manual searches left those requests running after their 30 s deadline).
        """
        if not self.base_url:
            # No built-in source: say what to do instead of a network error.
            raise NoReleaseSourceError(err("releases.noSource"))

        def remaining() -> float | None:
            return None if deadline is None else deadline - time.monotonic()

        def fetch(url: str, proxy: str | None) -> str:
            left = remaining()
            if left is None:
                return self._get(url, proxy)
            if left <= 0:
                raise _SearchDeadline()
            return self._get(url, proxy, timeout=left)

        left = remaining()
        if left is not None and left <= 0:
            raise NyaaError(err("nyaa.searchTimedOut"))
        self._run_hook(60.0 if left is None else left)
        selected_category = category or self.category
        mode = self.proxy_mode.casefold()
        attempts: list[str | None]
        if mode == "proxy_only":
            attempts = [self.proxy_url or None]
        elif mode == "proxy_then_direct":
            attempts = [self.proxy_url or None, None]
        elif mode == "direct":
            attempts = [None]
        else:
            attempts = [None, self.proxy_url or None]

        # With an empty proxy URL the mixed modes used to retry the exact same
        # direct request twice, doubling every timeout during startup.
        attempts = list(dict.fromkeys(attempts))

        errors: list[str] = []
        had_successful_response = False
        timed_out = False
        now = time.monotonic()
        with self._order_lock:
            bases = list(self.search_base_urls)
        candidates = [base for base in bases if self._mirror_backoff.get(base, 0.0) <= now] or bases

        def search_url(base_url: str) -> str:
            url = (
                f"{base_url}/?page=rss&q={quote_plus(query)}"
                f"&c={quote_plus(selected_category)}&f={int(filter_id)}"
            )
            if sort_by:
                url += f"&s={quote_plus(str(sort_by))}"
            if order:
                url += f"&o={quote_plus(str(order))}"
            return url

        for base_url in candidates:
            base_failed = False
            url = search_url(base_url)
            for proxy in attempts:
                if proxy is None and mode == "proxy_only":
                    continue
                try:
                    releases = parse_rss(fetch(url, proxy))
                    had_successful_response = True
                    if not releases:
                        # Read-only mirrors can lag the primary (or each other).
                        # An empty but valid RSS response therefore cannot prove
                        # that a release is absent. Do not waste time retrying the
                        # same mirror through another proxy route; probe the next
                        # distinct mirror instead.
                        break
                    # A mirror that actually returned data should stay first for
                    # later alias queries in this search. This avoids repeatedly
                    # paying a dead-primary timeout while still letting an empty
                    # stale mirror fall through to a fresher one.
                    with self._order_lock:
                        if self.search_base_urls[0] != base_url and base_url in self.search_base_urls:
                            self.search_base_urls.remove(base_url)
                            self.search_base_urls.insert(0, base_url)
                    self._mirror_backoff.pop(base_url, None)
                    return releases
                except _SearchDeadline:
                    timed_out = True
                    break
                except NyaaError as exc:
                    errors.append(f"{base_url}: {exc}")
                    base_failed = True
                    left = remaining()
                    if left is not None and left <= 0:
                        # The deadline, not the mirror, ended this attempt.
                        timed_out = True
                        break
            else:
                # Every route to this mirror failed (e.g. a 504 after ~17 s):
                # skip it for a while when other mirrors exist, instead
                # of paying the same timeout on every alias query.
                if base_failed and len(self.search_base_urls) > 1:
                    self._mirror_backoff[base_url] = time.monotonic() + MIRROR_BACKOFF_SECONDS
                continue
            if timed_out:
                break
            self._mirror_backoff.pop(base_url, None)
        if had_successful_response:
            # An empty mirror is not evidence of absence while the primary is
            # in backoff. Probe it once after every available mirror was empty.
            primary = self.base_url.rstrip("/")
            if primary not in candidates and not timed_out:
                try:
                    releases = parse_rss(fetch(search_url(primary), attempts[0]))
                except (NyaaError, _SearchDeadline):
                    pass
                else:
                    self._mirror_backoff.pop(primary, None)
                    if releases:
                        return releases
            return []
        if timed_out:
            errors.append(err("nyaa.searchTimedOut"))
        raise NyaaError("; ".join(dict.fromkeys(errors)) or err("nyaa.unavailablePlain"))


def release_group(title: str) -> str:
    prefix = GROUP_RE.match(title)
    if prefix:
        return prefix.group(1).strip()

    suffix = SUFFIX_GROUP_RE.search(title)
    if not suffix:
        return parse_release_name(title).group
    group = suffix.group("group").strip()
    normalized = _normalized_group(group)
    reserved = {
        "webdl",
        "webrip",
        "bluray",
        "bdrip",
        "remux",
        "h264",
        "x264",
        "h265",
        "x265",
        "hevc",
        "av1",
        "aac",
        "flac",
        "ddp",
        "ddp20",
        "ddp51",
    }
    return "" if normalized in reserved else group


# Cinema recordings. Letters/digits on either side break the token, so "Camp",
# "WebCam", "Tsubasa" or "cats" do not match; "_" and "." separate like spaces.
_CAMRIP_TOKEN_RE = re.compile(
    r"(?i)(?<![^\W_])(?:cam(?:[-_. ]?rip)?|hd[-_. ]?cam|telesync|hd[-_. ]?ts|telecine|hd[-_. ]?tc)(?![^\W_])"
)
# Bare "TS"/"TC" is also a title word or a broadcast "MPEG2 TS" spec, so it counts
# only as a whole bracket tag: "[TS]", "(TC)".
_CAMRIP_TAG_RE = re.compile(r"(?i)[\[(]\s*t[sc]\s*[\])]")
_VIDEO_EXTENSION_RE = re.compile(r"(?i)\.(?:mkv|mp4|avi|ts|m2ts|webm|mov|m4v|wmv|flv)$")


def is_camrip_release(title: str) -> bool:
    """True for a release title that marks a cinema recording (CAM, TELESYNC ...)."""
    # A ".ts" container extension is not a TELESYNC mark ("Show HD.ts").
    text = _VIDEO_EXTENSION_RE.sub("", str(title or "").strip())
    return bool(_CAMRIP_TOKEN_RE.search(text) or _CAMRIP_TAG_RE.search(text))


def fresh_trusted_zero_seeders_allowed(
    release: NyaaRelease,
    trusted_groups: Iterable[str],
    *,
    max_age_hours: float = FRESH_TRUSTED_ZERO_SEEDER_MAX_HOURS,
    now: datetime | None = None,
) -> bool:
    if release.category_id == "subsplease-rss" or release.is_batch or release.seeders > 0:
        return False
    trusted = release.trusted or _contains_any(release.group, trusted_groups)
    if not trusted:
        return False
    age_days = _published_age_days(release.published, now=now)
    return age_days is not None and age_days * 24.0 <= max(0.0, max_age_hours)


def _normalized_group(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _contains_any(value: str, terms: Iterable[str]) -> bool:
    normalized = _normalized_group(value)
    return any(_normalized_group(term) in normalized for term in terms if term.strip())


def _season_number(title: str) -> int | None:
    for pattern in SEASON_PATTERNS:
        match = pattern.search(title)
        if match:
            try:
                return int(match.group("season"))
            except (TypeError, ValueError):
                pass
    word_match = re.search(
        r"(?i)\b(" + "|".join(SEASON_WORDS) + r")[ ._-]+Season\b",
        title,
    )
    if word_match:
        return SEASON_WORDS[word_match.group(1).casefold()]

    # AniList frequently encodes sequels as a bare suffix rather than the word
    # Season: ``Youjo Senki II`` or ``Otomege ... 2``. Treat only a final,
    # standalone suffix as a season marker; embedded numbers such as 86 or
    # titles ending in a year are intentionally ignored.
    roman_match = re.search(
        # "x.264" is the codec, not season X.
        r"(?i)(?:^|[ ._:-])(?P<roman>II|III|IV|V|VI|VII|VIII|IX|X)(?!\.26[45]\b)(?=\s*(?:$|[-._]\s*\d{1,3}\b|\[|\(|S\d{1,2}E))",
        title,
    )
    if roman_match:
        return ROMAN_SEASONS.get(roman_match.group("roman").casefold())
    numeric_match = re.search(
        # Not "1-2"/"22-7" (a fraction or pair in the name), not "2.0"/"5.1" (audio channels, "2.5-jigen").
        r"(?:^|(?<!\d)[ ._:-])(?P<season>[2-9])(?![.,]\d)(?=\s*(?:$|[-._]\s*(?:\d{1,3}\b|(?:OVA|OAD|SPECIAL)\b)|\[|\(|S\d{1,2}E))",
        title,
        flags=re.IGNORECASE,
    )
    if numeric_match:
        return int(numeric_match.group("season"))
    part_match = re.search(r"(?i)\bPart[ ._-]*(?P<season>[2-9])\s*$", title)
    if part_match:
        return int(part_match.group("season"))
    return None


def _expected_season(anime: LibraryAnime) -> int:
    seasons = [
        value
        for value in (_season_number(name) for name in [anime.title, *anime.titles, *anime.synonyms])
        if value is not None
    ]
    # A bare trailing number is only trustworthy for a real sequel. This also
    # protects unrelated numbered titles while preserving explicit S02/Season 2
    # markers, which are already unambiguous on their own.
    has_prequel = any(
        str(item.get("relation_type") or "").upper() == "PREQUEL"
        for item in anime.relations
        if isinstance(item, dict)
    )
    explicit = [
        value
        for name in [anime.title, *anime.titles, *anime.synonyms]
        for value in [_season_number(name)]
        if value is not None
        and (
            any(pattern.search(name) for pattern in SEASON_PATTERNS)
            or re.search(
                r"(?i)\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)[ ._-]+Season\b",
                name,
            )
        )
    ]
    if explicit:
        return max(explicit)
    if has_prequel and seasons:
        return max(seasons)
    return 1


def _chain_season(anime: LibraryAnime) -> int:
    """The season a named sequel holds in its chain, when none of its titles has a number.

    "Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen" is season 2, and its
    packs are "[sam] Shouwa Genroku Rakugo Shinjuu Season 2: ..." or "... S2": with
    only the titles they were all "another season". The manager fills
    ``chain_season`` from the relation graph; 0 when unknown or a title says.
    """
    value = int(getattr(anime, "chain_season", 0) or 0)
    return value if value >= 2 and _expected_season(anime) == 1 else 0


def _release_season_markers(title: str) -> set[int]:
    """Every season number a release title states (S2, Season 2, a bare final "2")."""
    value = _season_number(title)
    return _explicit_season_numbers(title) | ({value} if value is not None else set())


def _named_by_first_season(anime: LibraryAnime, title: str) -> bool:
    """A release naming this sequel by an earlier season's title plus this season ("... S2").

    Needs the chain's titles from the manager; the season is the titles' own
    (a "2nd Season" synonym) or the chain's.
    """
    if not getattr(anime, "chain_titles", None):
        return False
    seasons = _release_season_markers(title)
    return bool(seasons) and 1 not in seasons and seasons <= _accepted_seasons(anime)


def _first_season_query_season(anime: LibraryAnime) -> int:
    """The season to ask for with the first season's title, 0 when it is not a sequel."""
    if not getattr(anime, "chain_titles", None):
        return 0
    expected = _expected_season(anime)
    return expected if expected > 1 else _chain_season(anime)


def _accepted_seasons(anime: LibraryAnime) -> set[int]:
    """Season numbers a release of this entry may carry: the titles' and the chain's."""
    chain = _chain_season(anime)
    return {_expected_season(anime), chain} if chain else {_expected_season(anime)}


_TRAILING_TITLE_NUMBER_RE = re.compile(r"(?i)[ ._:-]+(?:[2-9]|II|III|IV|V|VI|VII|VIII|IX|X)\s*$")


def _own_numbered_titles(anime: LibraryAnime) -> list[tuple[int, str]]:
    """(number, title without it) for titles that end in a bare number: "Kaiju No. 8", "... 2".

    Only a bare final number counts (an explicit "Season 2" / "S2" is handled as a
    season everywhere already), and the rest must still name something.
    """
    result: list[tuple[int, str]] = []
    for name in [anime.title, *anime.titles, *anime.synonyms]:
        text = str(name or "").strip()
        number = _season_number(text)
        if number is None or number < 2 or _explicit_season_numbers(text):
            continue
        base = _TRAILING_TITLE_NUMBER_RE.sub("", text).strip()
        if base != text and len(normalize_title(base).replace(" ", "")) >= 3:
            result.append((number, base))
    return result


_NUMBER_NOUNS = {"no", "number", "nr", "vol", "volume", "part", "ep", "episode", "act", "chapter"}


def _own_numbered_title_seasons(anime: LibraryAnime) -> list[tuple[int, str]]:
    """Own numbered titles whose number may be a season, for extra "<title> S02" queries.

    "Kaiju No. 8" is a name, not season 8 of "Kaiju No.": no "Kaiju No S08E01" query.
    """
    return [
        (number, base)
        for number, base in _own_numbered_titles(anime)
        if (normalize_title(base).split() or [""])[-1] not in _NUMBER_NOUNS
    ]


def _own_title_season(anime: LibraryAnime, title: str) -> int | None:
    """The season number a release carries as part of this entry's own title, else None.

    "Cyberpunk: Edgerunners 2" has no PREQUEL relation on AniList, so its expected
    season is 1, and "[Erai-raws] Cyberpunk: Edgerunners 2 - 01" (or "Cyberpunk
    Edgerunners S02E01") looked like another season of it. A release that names
    this entry's title and states exactly the number the title ends in is this
    entry, whatever the relation graph knows. "Kaiju No. 8" is the same case.
    """
    markers = _release_season_markers(title)
    if len(markers) != 1:
        return None
    (marker,) = markers
    for number, base in _own_numbered_titles(anime):
        if number == marker and _token_phrase_present(base, title):
            return number
    return None


_SEASON_MARKER_RES = (
    *SEASON_PATTERNS[:3],
    re.compile(r"(?i)\b(?:" + "|".join(SEASON_WORDS) + r")[ ._-]+Season\b"),
)


def _season_marked_titles(anime: LibraryAnime) -> list[tuple[int, str]]:
    """(season, title without its marker) for titles with an explicit season marker.

    "The Detective Is Already Dead Season 2" -> (2, "The Detective Is Already Dead"):
    releases write the same season as "S2", "S02E03", "2nd Season" or "Season 2".
    """
    result: list[tuple[int, str]] = []
    for name in [anime.title, *anime.titles, *anime.synonyms]:
        text = str(name or "").strip()
        seasons = _explicit_season_numbers(text)
        if len(seasons) != 1:
            continue
        (season,) = seasons
        base = text
        for pattern in _SEASON_MARKER_RES:
            base = pattern.sub(" ", base)
        base = re.sub(r"\s+", " ", base).strip(" -:–—_")
        if season >= 2 and base != text and len(normalize_title(base).replace(" ", "")) >= 3:
            result.append((season, base))
    return result


def _same_season_titles(anime: LibraryAnime, release_title: str) -> list[str]:
    """This entry's season-marked titles without the marker, when the release states that season.

    Only a release whose only season marker is the title's season: a season 1
    release, a season pack range or an unnumbered release gets nothing.
    """
    markers = _release_season_markers(release_title)
    if len(markers) != 1:
        return []
    (marker,) = markers
    return [base for season, base in _season_marked_titles(anime) if season == marker]


def _own_title_number_missing(anime: LibraryAnime, title: str) -> bool:
    """A release with this entry's title but without the number it ends in (an earlier series).

    Only when AniList gave no relations, where nothing else tells the two apart.
    """
    if anime.relations or _release_season_markers(title):
        return False
    named = [
        (number, base) for number, base in _own_numbered_titles(anime) if _token_phrase_present(base, title)
    ]
    return bool(named) and not any(_token_phrase_present(f"{base} {number}", title) for number, base in named)


_FINAL_SEASON_IDENTITY_RE = re.compile(r"(?i)\b(?:the[ ._-]+)?final[ ._-]+season\b")


def _explicit_season_numbers(title: str) -> set[int]:
    """Return high-confidence explicit season numbers from a release title."""
    value = str(title or "")
    result: set[int] = set()
    for pattern in SEASON_RANGE_PATTERNS:
        for match in pattern.finditer(value):
            try:
                start = int(match.group("start"))
                end = int(match.group("end"))
            except (TypeError, ValueError):
                continue
            if start <= 0 or end <= 0:
                continue
            low, high = sorted((start, end))
            # Season counts above 20 are almost certainly not season ranges.
            # Keep the parser fail-safe and avoid interpreting episode spans.
            if high > 20 or high - low > 12:
                continue
            result.update(range(low, high + 1))
    for pattern in SEASON_PATTERNS:
        for match in pattern.finditer(value):
            try:
                result.add(int(match.group("season")))
            except (TypeError, ValueError):
                continue
    for word, number in SEASON_WORDS.items():
        if re.search(rf"(?i)\b{re.escape(word)}[ ._-]+Season\b", value):
            result.add(number)
    return result


def release_identity_mismatch_reason(anime: LibraryAnime, title: str) -> str | None:
    """Return a hard identity contradiction shared by search and final selection.

    This intentionally rejects only high-confidence contradictions. Ambiguous
    unnumbered releases remain rankable; explicit other seasons/final-season
    releases do not. Keeping this in the provider makes benchmark discovery and
    the application use the same identity contract.
    """
    value = str(title or "")
    targets = [anime.title, *anime.titles, *anime.synonyms]
    target_final = any(_FINAL_SEASON_IDENTITY_RE.search(str(item or "")) for item in targets)
    source_final = bool(_FINAL_SEASON_IDENTITY_RE.search(value))
    if source_final and not target_final:
        return "cross-season-final"

    target_format = str(getattr(anime, "format", "") or "").strip().upper()
    # A broadcast year directly after the title identifies a TV season.
    # Do not treat Blu-ray/encode dates or years elsewhere in metadata as identity.
    parsed = parse_release_name(value)
    # Named continuations are separate entries even without a numeric season.
    # A root-title prefix alone must not admit "Tokyo Ghoul:re" for S1 when
    # the relation graph is absent/incomplete. Respect a full positive alias.
    parsed_identity = normalize_title(parsed.title or "")
    positive_identities = {normalize_title(str(item or "")) for item in targets}
    if (
        parsed_identity.endswith(" re")
        and parsed_identity not in positive_identities
        and parsed_identity[:-3] in positive_identities
    ):
        return "cross-entry-named-continuation"
    target_year = int(getattr(anime, "season_year", 0) or 0)
    if target_format in {"TV", "TV_SHORT"} and target_year and parsed.title:
        year_match = re.search(
            re.escape(parsed.title) + r"\s*\(((?:19|20)\d{2})\)\s*-\s*\d",
            value,
            re.IGNORECASE,
        )
        is_web = bool(re.search(r"(?i)\b(?:WEB[ ._-]?(?:DL|Rip)|CR|HIDIVE)\b", value))
        is_disc = bool(re.search(r"(?i)\b(?:BD|BDRip|Blu[ ._-]?Ray)\b", value))
        if year_match and is_web and not is_disc and abs(int(year_match[1]) - target_year) > 1:
            return "cross-season-year"
    if re.search(r"(?i)(?:^|[\s._\-\[(])(?:OVA|OAD)(?=$|[\s._\-\])])", _without_pack_extras(value)):
        if target_format not in {"OVA", "SPECIAL"}:
            return "special-release-ova"

    accepted = _accepted_seasons(anime)
    explicit = _explicit_season_numbers(value)
    if len(explicit) > 1:
        return "cross-season-multi"
    own_season = _own_title_season(anime, value)
    if explicit and accepted.isdisjoint(explicit) and own_season is None:
        return "cross-season-explicit"

    release_season = _season_number(value)
    if release_season is not None and release_season not in accepted and own_season is None:
        return "cross-season-release"
    if release_season == 0:
        return "special-season-zero"
    return None


def _absolute_episode_alias_overrides_season_identity(
    reason: str | None,
    title: str,
    alternative_episodes: tuple[int, ...],
) -> bool:
    """Allow scene-season tags only when an absolute episode alias proves identity.

    Release groups sometimes number a continuous franchise as S06E02 while
    AniList represents that episode as #1 of a separate entry. Only explicit
    cross-season contradictions are overridable; S00, multi-season packs and
    Final Season conflicts remain hard failures.
    """
    if reason not in {"cross-season-explicit", "cross-season-release"}:
        return False
    if _season_number(title) == 0 or 0 in _explicit_season_numbers(title):
        return False
    found_episode, explicit = _release_episode_match(title)
    allowed = {int(value) for value in alternative_episodes if int(value) > 0}
    return bool(explicit and found_episode in allowed)


# Words that make an unmarked release something other than the season itself.
_NOT_A_SEASON_PACK_RE = re.compile(
    r"(?i)\b(?:movie|film|specials?|ova|oad|ona|recap|digest|preview|pv|ncop|nced|ost|soundtrack"
    r"|vol(?:ume)?[ ._-]*\d{1,3})\b"
)


def _season_pack_shape(anime: LibraryAnime, title: str) -> bool:
    """A finished entry and a release title without any episode number or extra marker."""
    episodes = int(anime.episodes or 0)
    if episodes < 2:
        return False
    finished = str(anime.media_status or "").upper() == "FINISHED"
    # released_episodes falls back to the episode count without airing data,
    # so "all released" counts only with a known next airing episode.
    aired = bool(anime.next_airing_episode) and int(anime.released_episodes or 0) >= episodes
    if not (finished or aired):
        return False
    if _NOT_A_SEASON_PACK_RE.search(title) or release_episode_range(title) is not None:
        return False
    found_episode, _explicit = _release_episode_match(title)
    return found_episode is None


def _season_sized_pack(release: NyaaRelease, anime: LibraryAnime, per_episode_min_bytes: int) -> bool:
    """An unmarked release large enough to hold every episode of a finished entry.

    "[Cleo] Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen | Descending
    Stories: ... [10bit 1080p][HEVC-x265]" (31 seeders, the only real pack) has
    no "Batch"/"01-12" marker and is under 4 GiB, so it counted as "not a pack"
    and was never offered; every episode search then found only that pack.
    Size alone is still not enough evidence: the caller also requires an exact
    title of this entry, and the related-title guard keeps other seasons out.
    """
    episodes = int(anime.episodes or 0)
    if not release.size_bytes or not _season_pack_shape(anime, release.title):
        return False
    floor = episodes * max(100 * 1024 * 1024, int(per_episode_min_bytes or 0))
    # Never below what the size rules call too small for a series.
    if episodes >= 8:
        floor = max(floor, 2 * 1024**3)
    elif episodes >= 4:
        floor = max(floor, 1024**3)
    return release.size_bytes >= floor


def release_is_safe_batch_candidate(
    anime: LibraryAnime,
    item: NyaaRelease,
    *,
    negative_titles: tuple[str, ...] = (),
) -> bool:
    """Shared production/benchmark guard for one AniList-entry batch download."""
    reasons = {str(reason) for reason in getattr(item, "reasons", [])}
    if any(reason.startswith("wrong-season=") for reason in reasons):
        return False
    if "single-episode" in reasons or any(reason.startswith("single-episode=") for reason in reasons):
        return False

    has_range = any(reason.startswith("range=") for reason in reasons)
    if has_range and "full-series-range" not in reasons:
        return False

    title = str(getattr(item, "title", "") or "")
    if re.search(r"(?i)\b(?:movie|film|special|ova|oad|ona)\b", _without_pack_extras(title)):
        return False
    if release_identity_mismatch_reason(anime, title) is not None:
        return False
    if release_related_title_conflict_reason(anime, title, negative_titles) is not None:
        return False

    explicit = _explicit_season_numbers(title)
    # A whole-entry batch must not silently span several seasons. Selective
    # benchmark logic deliberately follows the same production safety rule so
    # it can expose application search bugs instead of masking them.
    if len(explicit) > 1:
        return False
    if explicit and not explicit <= _accepted_seasons(anime):
        return False

    if bool(getattr(item, "is_batch", False)):
        return True
    if "exact-title-phrase" not in reasons:
        return False
    if "large-pack-candidate" in reasons:
        return True
    # score_release adds this only when the size covers every episode of a
    # finished entry; size, an exact title, no episode number and no special
    # marker must all agree (re-checked here where no settings are needed).
    return bool("season-sized-pack" in reasons and _season_pack_shape(anime, title))


def _ordinal(value: int) -> str:
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _published_age_days(value: str, *, now: datetime | None = None) -> float | None:
    if not value.strip():
        return None
    try:
        published = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    try:
        return max(0.0, (current - published.astimezone(timezone.utc)).total_seconds() / 86400.0)
    except (OverflowError, ValueError):
        # "31 Dec 9999 23:59:59 -1400" is past the last date once in UTC: one such feed
        # item raised out of scoring and failed the whole search of every title.
        return None


def _freshness_score(published: str, *, batch: bool) -> tuple[float, list[str]]:
    age = _published_age_days(published)
    if age is None:
        return 0.0, []
    scale = 0.55 if batch else 1.0
    if age <= 2:
        points = 36.0
    elif age <= 7:
        points = 30.0
    elif age <= 30:
        points = 20.0
    elif age <= 90:
        points = 10.0
    elif age > 730:
        points = -28.0
    elif age > 365:
        points = -20.0
    elif age > 180:
        points = -10.0
    else:
        points = 0.0
    return points * scale, [f"age={age:.0f}d"]


_WEB_DL_RE = re.compile(r"(?i)\bWEB[ ._-]?DL\b")
_WEB_SERVICE_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(CR|NF|AMZN|DSNP|HIDI(?:VE)?|ADN|B-Global|BILI|ABEMA|U-NEXT|HULU|DMM)(?![A-Za-z0-9])"
)


def same_web_encode(title_a: str, size_a: int, title_b: str, size_b: int) -> bool:
    """Two WEB-DLs of one streaming service with the same size carry the same video stream:
    groups only remux it (Kusuriya S3 #3: AnoZu and Erai-raws, both "CR WEB-DL", 1434 MiB),
    so downloading one to replace the other gains nothing."""
    if not (_WEB_DL_RE.search(title_a or "") and _WEB_DL_RE.search(title_b or "")):
        return False
    services_a = {match.casefold() for match in _WEB_SERVICE_RE.findall(title_a)}
    services_b = {match.casefold() for match in _WEB_SERVICE_RE.findall(title_b)}
    if not services_a or services_a != services_b:
        return False
    if int(size_a or 0) <= 0 or int(size_b or 0) <= 0:
        return False
    return abs(int(size_a) - int(size_b)) <= 0.02 * max(int(size_a), int(size_b))


def _seed_availability_bonus(seeders: int) -> float:
    """Reward healthy availability, then saturate once a torrent is well seeded."""
    count = max(0, int(seeders))
    if count <= 0:
        return 0.0
    if count <= 20:
        # 6 seeds is usable but clearly weaker than 15-20. After 20, popularity
        # should barely matter compared with source/codec/release correctness.
        return 30.0 * (count / 20.0) ** 0.8
    return 30.0 + min(4.0, math.log2(count / 20.0) * 1.5)


def _leecher_activity_bonus(leechers: int, seeders: int) -> float:
    """Small swarm-activity bonus; never lets leechers rescue a seedless torrent."""
    peers = max(0, int(leechers))
    seeds = max(0, int(seeders))
    if seeds <= 0 or peers <= 0:
        return 0.0
    if peers <= 10:
        return 6.0 * (peers / 10.0) ** 0.7
    return 6.0 + min(2.0, math.log2(peers / 10.0) * 0.75)


# "BD 1920x1080", "1920×1080": the frame size names the resolution as well.
_FRAME_SIZE_RE = re.compile(r"(?i)(?<!\d)\d{3,4}\s*[x×]\s*(2160|1440|1080|720|576|480)(?!\d)")


def _sd_release_against_preference(title: str, preferred_resolution: str) -> bool:
    """An SD release (480p/576p, DVD) while the preference is HD or the highest."""
    preferred = str(preferred_resolution or "").casefold().strip()
    if preferred not in {"highest", "best", "max", "higher", "1080p", "1440p", "2160p"}:
        return False
    match = RESOLUTION_RE.search(title)
    frame = None if match else _FRAME_SIZE_RE.search(title)
    resolution = match.group(1).casefold() if match else (f"{frame.group(1)}p" if frame else "")
    return resolution in {"480p", "576p"} or (not resolution and bool(re.search(r"(?i)\bDVD", title)))


def _quality_score(title: str, preferred_resolution: str) -> tuple[float, list[str]]:
    match = RESOLUTION_RE.search(title)
    if match:
        resolution = match.group(1).casefold()
    else:
        frame = _FRAME_SIZE_RE.search(title)
        if not frame:
            return -8.0, ["resolution-unknown"]
        resolution = "480p" if frame.group(1) == "576" else f"{frame.group(1)}p"
    preferred = preferred_resolution.casefold().strip()
    if preferred in {"highest", "best", "max", "higher"}:
        ranking = {"2160p": 52.0, "1440p": 44.0, "1080p": 34.0, "720p": 8.0, "480p": -24.0}
        score = ranking.get(resolution, -55.0)
        reasons = [resolution, "prefer-highest"]
        if resolution in {"720p", "480p"}:
            reasons.append("low-resolution")
        return score, reasons
    if resolution == preferred:
        return 52.0, [resolution]
    if resolution == "2160p":
        return 24.0, [resolution]
    if resolution == "1440p":
        return 18.0, [resolution]
    if resolution == "1080p":
        return 30.0 if preferred != "1080p" else 52.0, [resolution]
    if resolution == "720p":
        return -18.0, [resolution, "low-resolution"]
    return -70.0, [resolution, "very-low-resolution"]


def _token_phrase_present(alias: str, release_title: str) -> bool:
    alias_tokens = normalize_title(alias).split()
    release_tokens = normalize_title(release_title).split()
    if not alias_tokens or len(alias_tokens) > len(release_tokens):
        return False
    width = len(alias_tokens)
    return any(
        release_tokens[index : index + width] == alias_tokens
        for index in range(len(release_tokens) - width + 1)
    )


def _relation_title_candidates(relations: Iterable[dict[str, object]]) -> list[str]:
    result: list[str] = []
    stack = [item for item in relations if isinstance(item, dict)]
    while stack:
        item = stack.pop()
        title = str(item.get("title") or "").strip()
        if title:
            result.append(title)
        children = item.get("relations")
        if isinstance(children, list):
            stack.extend(child for child in children if isinstance(child, dict))
    return result


def relation_negative_titles(
    anime: LibraryAnime,
    extra_titles: Iterable[str] = (),
) -> tuple[str, ...]:
    """Return distinct titles of other AniList media that must not win identity.

    ``LibraryAnime.relations`` contains nearby franchise entries while the manager
    can additionally supply every title from its cached full relation graph.  The
    provider owns the actual conflict decision so Nyaa, RSS fallbacks, the final
    batch selector and the benchmark all share one identity contract.
    """
    positive_norms = {
        normalize_title(value)
        for value in [anime.title, *anime.titles, *anime.synonyms]
        if str(value).strip()
    }
    values = [*_relation_title_candidates(anime.relations), *extra_titles]
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        normalized = normalize_title(value)
        if not value or not normalized or normalized in positive_norms or normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return tuple(result)


_EXTRAS_WORDS = {"special", "specials", "sp", "sps", "ova", "ovas", "oad", "oads", "extra", "extras", "bonus"}
_WITH_EXTRAS_RE = re.compile(r"(?i)\s\+\s*(?:specials?|sps?|ovas?|oads?|extras?|bonus)\b")


def _without_pack_extras(title: str) -> str:
    """A season pack's title without its "+ OVA" / "+ Specials" part.

    "[Erai-raws] Dorohedoro - 01 ~ 12 + OVA [Batch]" is the TV season with its extras,
    not the OVA entry; a single "Show - OVA" release keeps its marker.
    """
    if _WITH_EXTRAS_RE.search(title) and release_title_is_pack(title):
        return _WITH_EXTRAS_RE.sub(" ", title)
    return title


def _extras_of_positive(negative: str, positives: list[str]) -> bool:
    """True for a related title that is this title plus only "Specials"/"OVA"-type words."""
    negative_tokens = normalize_title(negative).split()
    for positive in positives:
        tokens = normalize_title(positive).split()
        rest = negative_tokens[len(tokens) :]
        if tokens and negative_tokens[: len(tokens)] == tokens and rest and set(rest) <= _EXTRAS_WORDS:
            return True
    return False


def release_related_title_conflict_reason(
    anime: LibraryAnime,
    release_title: str,
    negative_titles: Iterable[str] = (),
    *,
    alternative_titles: Iterable[str] = (),
    alternative_episodes: Iterable[int] = (),
) -> str | None:
    """Reject a release that more specifically names another related AniList media.

    A franchise root such as ``Kimetsu no Yaiba`` is a literal prefix of sequel
    releases such as ``Kimetsu no Yaiba: Yuukaku-hen``.  Pure positive-title
    scoring therefore cannot distinguish them.  Related AniList titles are safe
    negative evidence when the release contains the related title as a complete
    token phrase and that title is either more specific than a positive alias or
    is the only exact franchise-title match.
    """
    positive_values: list[str] = []
    for value in [anime.title, *anime.titles, *anime.synonyms]:
        positive_values.extend(_release_search_title_variants(str(value or "")))
    positives = list(dict.fromkeys(value.strip() for value in positive_values if value.strip()))
    release = str(release_title or "")
    positive_exact = [value for value in positives if _token_phrase_present(value, release)]
    # "The Detective Is Already Dead S02E03" for "The Detective Is Already Dead Season 2":
    # the prequel's title plus this entry's season marker names this entry.
    same_season_norms = {normalize_title(value) for value in _same_season_titles(anime, release)}
    named_by_first_season = _named_by_first_season(anime, release)
    chain_titles = {normalize_title(str(value or "")) for value in getattr(anime, "chain_titles", ()) or ()}
    # "Baccano! + Specials": the TV season with its extras, not the Specials entry.
    with_extras = bool(_WITH_EXTRAS_RE.search(release))
    # "[SubsPlease] Boku no Hero Academia - 139" for season 7 episode 1: an earlier
    # season's title with this entry's absolute episode number names this entry.
    found_episode, explicit_episode = _release_episode_match(release)
    absolute_alias = bool(
        explicit_episode and found_episode in {int(value) for value in alternative_episodes if int(value) > 0}
    )
    alias_norms = (
        {normalize_title(str(value)) for value in alternative_titles if str(value or "").strip()}
        if absolute_alias
        else set()
    )
    for negative in relation_negative_titles(anime, negative_titles):
        if not _token_phrase_present(negative, release):
            continue
        if with_extras and _extras_of_positive(negative, positive_exact):
            continue
        if normalize_title(negative) in alias_norms:
            continue
        if normalize_title(negative) in same_season_norms:
            continue
        if named_by_first_season and normalize_title(negative) in chain_titles:
            # "<first season title> Season 2" names this entry, not the first season.
            continue
        if not positive_exact:
            return "related-media-title"
        negative_tokens = normalize_title(negative).split()
        for positive in positive_exact:
            positive_tokens = normalize_title(positive).split()
            if len(negative_tokens) > len(positive_tokens) and _token_phrase_present(positive, negative):
                return "related-media-title"
    return None


def _identity_alias_present(alias: str, release_title: str) -> bool:
    if not _token_phrase_present(alias, release_title):
        return False
    tokens = normalize_title(alias).split()
    if len(tokens) != 1 or len(tokens[0]) > 12:
        return True
    # "Another" inside "... in Another World" is not an exact title match.
    parsed = parse_release_name(release_title)
    if parsed.title:
        # "Baccano! + Specials" is still the title "Baccano!".
        return normalize_title(_WITH_EXTRAS_RE.split(parsed.title)[0]) == normalize_title(alias)
    clean = re.sub(r"\[[^\]]*\]", " ", release_title).strip()
    clean = re.split(r"\s+-\s*\d|\bS\d{1,2}E\d|\bE(?:P)?\d", clean, maxsplit=1, flags=re.I)[0]
    clean = re.sub(r"\((?:19|20)\d{2}\)", "", clean)
    return normalize_title(clean) == normalize_title(alias)


def _title_match_score(
    anime: LibraryAnime,
    release_title: str,
    alternative_titles: tuple[str, ...] = (),
) -> tuple[float, list[str]]:
    raw_aliases: list[str] = []
    chain_titles: list[str] = []
    if _named_by_first_season(anime, release_title):
        # "Shouwa Genroku Rakugo Shinjuu S2" names the second season by the first one's title.
        chain_titles = [str(value) for value in getattr(anime, "chain_titles", ()) or ()]
    if _own_title_season(anime, release_title) is not None:
        # "Cyberpunk Edgerunners S02E01" names "Cyberpunk: Edgerunners 2" by its title without the 2.
        chain_titles.extend(base for _number, base in _own_numbered_titles(anime))
    # "Tantei wa Mou, Shindeiru. S2 - 03" for the title "... Season 2".
    chain_titles.extend(_same_season_titles(anime, release_title))
    for value in [anime.title, *alternative_titles, *anime.titles, *anime.synonyms, *chain_titles]:
        raw_aliases.extend(_release_search_title_variants(value))
    aliases = list(dict.fromkeys(value.strip() for value in raw_aliases if value.strip()))
    if not aliases:
        return -200.0, ["title-missing"]

    similarities = [(alias, title_similarity(alias, release_title)) for alias in aliases]
    best_alias, best_similarity = max(similarities, key=lambda item: item[1])
    exact_aliases = [alias for alias in aliases if _identity_alias_present(alias, release_title)]
    if exact_aliases:
        exact_similarity = max(title_similarity(alias, release_title) for alias in exact_aliases)
        return exact_similarity * 0.45 + 62.0, [f"title={exact_similarity:.0f}", "exact-title-phrase"]

    alias_norm = normalize_title(best_alias)
    alias_tokens = alias_norm.split()
    release_tokens = set(normalize_title(release_title).split())
    coverage = len(set(alias_tokens) & release_tokens) / len(set(alias_tokens)) if alias_tokens else 0.0

    # Short titles must appear as complete tokens. This blocks e.g. "Akira"
    # matching the unrelated word "Akiraka" while still accepting "AKIRA (1988)".
    if len(alias_tokens) == 1 and len(alias_norm) <= 12:
        return best_similarity * 0.20 - 185.0, [f"title={best_similarity:.0f}", "short-title-token-mismatch"]
    if coverage < 0.60:
        return best_similarity * 0.25 - 125.0, [
            f"title={best_similarity:.0f}",
            f"title-token-coverage={coverage:.2f}",
        ]
    if coverage < 0.85:
        return best_similarity * 0.35 - 45.0, [
            f"title={best_similarity:.0f}",
            f"partial-title-coverage={coverage:.2f}",
        ]
    return best_similarity * 0.40 - 15.0, [f"title={best_similarity:.0f}", "fuzzy-title-match"]


def release_title_is_plausible(
    anime: LibraryAnime,
    release_title: str,
    alternative_titles: tuple[str, ...] = (),
    negative_titles: tuple[str, ...] = (),
    alternative_episodes: tuple[int, ...] = (),
) -> bool:
    """Shared fail-closed title identity check for provider results/resume.

    This uses the same production title scoring used by Nyaa ranking. The
    threshold mirrors the existing SubsPlease provider gate: weak accidental
    token overlap (for example Kimetsu returned for Shingeki) is rejected,
    while exact aliases and strong fuzzy aliases remain eligible.
    """
    identity_mismatch = release_identity_mismatch_reason(anime, release_title)
    if identity_mismatch is not None and not _absolute_episode_alias_overrides_season_identity(
        identity_mismatch, release_title, alternative_episodes
    ):
        return False
    if (
        release_related_title_conflict_reason(
            anime,
            release_title,
            negative_titles,
            alternative_titles=alternative_titles,
            alternative_episodes=alternative_episodes,
        )
        is not None
    ):
        return False
    score, _reasons = _title_match_score(anime, release_title, alternative_titles)
    return score >= 25.0


def _episode_size_floor_bytes(anime: LibraryAnime) -> tuple[int, str]:
    duration = int(anime.duration or 0)
    if duration > 0 and not 18 <= duration <= 35:
        return duration * 30 * 1024 * 1024, f"duration={duration}m"
    return 800 * 1024 * 1024, "standard-episode-floor=800MiB"


def _episode_size_quality_score(size_bytes: int, anime: LibraryAnime) -> tuple[float, list[str]]:
    if size_bytes <= 0:
        return -8.0, ["size-unknown"]
    floor, floor_reason = _episode_size_floor_bytes(anime)
    ratio = size_bytes / max(1, floor)
    size_mib = size_bytes / 1024**2
    reasons = [f"size={size_mib:.0f}MiB", floor_reason]
    if ratio >= 1.0:
        # Large files are not inherently better for a single-episode search.
        return 5.0, reasons + ["size-floor-ok"]
    if ratio >= 0.80:
        return -32.0, reasons + ["below-size-floor"]
    if ratio >= 0.55:
        return -68.0, reasons + ["low-bitrate-size"]
    return -112.0, reasons + ["very-low-bitrate-size"]


def _ordered_preference_score(
    title: str,
    preferences: list[str],
    patterns: dict[str, str],
    *,
    weights: tuple[float, ...],
) -> tuple[float, list[str]]:
    for index, preference in enumerate(preferences):
        key = preference.casefold().strip()
        pattern = patterns.get(key)
        if not pattern or not re.search(pattern, title, re.IGNORECASE):
            continue
        weight = weights[min(index, len(weights) - 1)]
        return weight, [f"preferred={preference}"]
    return 0.0, []


def _video_policy_score(
    title: str,
    *,
    preferred_video_codecs: list[str],
    preferred_sources: list[str],
    require_japanese_audio: bool,
    avoid_upscaled: bool,
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    codec_patterns = {
        "hevc": r"\b(?:HEVC|x265|H[ ._-]?265)\b",
        "x265": r"\b(?:HEVC|x265|H[ ._-]?265)\b",
        "av1": r"\bAV1\b",
        "avc": r"\b(?:AVC|x264|H[ ._-]?264)\b",
        "x264": r"\b(?:AVC|x264|H[ ._-]?264)\b",
    }
    source_patterns = {
        "bluray": r"\b(?:BluRay|BDRip|BDMV|REMUX|BD)\b",
        "bdrip": r"\b(?:BluRay|BDRip|BDMV|REMUX|BD)\b",
        "web-dl": r"\bWEB[ ._-]?DL\b",
        "webdl": r"\bWEB[ ._-]?DL\b",
        "webrip": r"\bWEBRip\b",
        "hdtv": r"\bHDTV\b",
    }
    value, detail = _ordered_preference_score(
        title, preferred_video_codecs, codec_patterns, weights=(14.0, 8.0, 3.0)
    )
    score += value
    reasons.extend([f"codec-{item}" for item in detail])
    value, detail = _ordered_preference_score(
        title, preferred_sources, source_patterns, weights=(18.0, 10.0, 4.0)
    )
    score += value
    reasons.extend([f"source-{item}" for item in detail])

    if avoid_upscaled and re.search(r"(?i)\b(?:AI[ ._-]?)?upscal(?:e|ed|ing)\b", title):
        score -= 180.0
        reasons.append("blocked-upscale")

    dub_only = bool(
        re.search(r"(?i)\b(?:English[ ._-]?Dub|ENG[ ._-]?Dub|Dubbed)\b", title)
        and not re.search(
            r"(?i)\b(?:Dual[ ._-]?Audio|Multi[ ._-]?Audio|Japanese|JPN|JA[ ._-]?Audio)\b", title
        )
    )
    if require_japanese_audio and dub_only:
        score -= 220.0
        reasons.append("english-dub-only")
    elif re.search(r"(?i)\b(?:Japanese|JPN|JA)[ ._-]?(?:Audio)?\b", title):
        score += 8.0
        reasons.append("japanese-audio")
    elif re.search(r"(?i)\bDual[ ._-]?Audio\b", title):
        score += 4.0
        reasons.append("dual-audio")
    return score, reasons


# Bump when score_release changes meaning: stored scores from another formula
# are never compared with fresh ones (upgrade decisions skip instead).
SCORE_FORMULA_VERSION = "nyaa-score-2"


def seadex_fingerprint(rec: "SeaDexRecommendation | None") -> str:
    """Stable id of the SeaDex facts that can change a score ("none" = no bonus possible)."""
    if rec is None or not rec.usable:
        return "none"
    parts = [
        ",".join(sorted(rec.preferred_hashes)),
        ",".join(sorted(rec.alternative_hashes)),
        ",".join(sorted(rec.preferred_nyaa_ids)),
        ",".join(sorted(rec.alternative_nyaa_ids)),
    ]
    import hashlib

    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


def score_context_key(score_kwargs: dict) -> str:
    """Formula version + hash of every score_release input except the release itself."""
    import hashlib

    def norm(value):
        if isinstance(value, (list, tuple, set, frozenset)):
            return [norm(item) for item in value]
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return value

    payload = {key: norm(value) for key, value in score_kwargs.items() if key != "seadex"}
    payload["seadex"] = seadex_fingerprint(score_kwargs.get("seadex"))
    digest = hashlib.sha1(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"{SCORE_FORMULA_VERSION}:{digest}"


def score_release(
    release: NyaaRelease,
    anime: LibraryAnime,
    *,
    episode: int | None,
    batch: bool,
    trusted_groups: list[str],
    preferred_groups: list[str],
    blocked_groups: list[str],
    preferred_resolution: str,
    min_seeders: int,
    target_episode_min_bytes: int,
    target_episode_max_bytes: int,
    preferred_video_codecs: list[str] | None = None,
    preferred_sources: list[str] | None = None,
    require_japanese_audio: bool = True,
    avoid_upscaled: bool = True,
    alternative_episodes: tuple[int, ...] = (),
    alternative_titles: tuple[str, ...] = (),
    seadex: "SeaDexRecommendation | None" = None,
    numbering_context: EpisodeNumbering | None = None,
) -> NyaaRelease:
    score, reasons = _title_match_score(anime, release.title, alternative_titles)

    expected_season = _expected_season(anime)
    release_season = _season_number(release.title)
    if expected_season > 1:
        if release_season == expected_season:
            score += 48
            reasons.append(f"season={expected_season}")
        elif release_season is not None:
            score -= 130
            reasons.append(f"wrong-season={release_season}")
        else:
            score -= 34
            reasons.append("season-not-specified")
    elif release_season is not None and release_season == _chain_season(anime):
        # "<first season title> S2" for a named second season.
        score += 48
        reasons.append(f"season={release_season}")
    elif release_season is not None and release_season == _own_title_season(anime, release.title):
        # The number of the entry's own title ("Edgerunners 2", "Kaiju No. 8").
        score += 48
        reasons.append(f"season={release_season}")
    elif release_season is not None and release_season != 1:
        score -= 110
        reasons.append(f"wrong-season={release_season}")
    elif release_season is None and _own_title_number_missing(anime, release.title):
        # "Cyberpunk Edgerunners - 01" is the 2022 series, not "Cyberpunk: Edgerunners 2".
        score -= 34
        reasons.append("season-not-specified")

    found_episode, explicit_episode = _release_episode_match(release.title)
    episode_match = (
        match_release_episode(
            anime,
            parse_release_name(release.title),
            episode,
            numbering_context,
            alternative_episodes=alternative_episodes,
            trusted_source=release.trusted or _contains_any(release.group, trusted_groups),
        )
        if not batch and episode is not None
        else None
    )
    if batch:
        episode_range = release_episode_range(release.title)
        # "Kaiju No. 8 - 12" / "KonoSuba 3 - 11": the title's number and one episode, no pack.
        pair_range = bool(
            episode_range is not None and _season_episode_pair_range(release.title, anime, episode_range[1])
        )
        if pair_range:
            episode_range = None
        is_pack = bool(release.is_batch and not pair_range)
        range_count = episode_range[1] - episode_range[0] + 1 if episode_range else 0
        # One episode number and no range is a single episode, also when the release counts
        # episodes across seasons (S03E49 for the first episode of season 3): only a
        # pack-sized file with such a number is left to the size rules below.
        plausible_single_episode = (
            found_episode
            if found_episode is not None
            and (
                anime.episodes is None
                or found_episode <= max(3, anime.episodes + 3)
                or (
                    episode_range is None
                    and not is_pack
                    and (not release.size_bytes or release.size_bytes < 4 * 1024**3)
                )
            )
            else None
        )
        likely_pack = bool(
            is_pack
            or range_count >= 3
            or (release.size_bytes >= 4 * 1024**3 and plausible_single_episode is None)
        )
        season_sized = bool(
            not likely_pack
            and "exact-title-phrase" in reasons
            and _season_sized_pack(release, anime, target_episode_min_bytes)
        )

        if is_pack:
            score += 18
            reasons.append("batch")
        elif likely_pack:
            score += 18
            reasons.append("large-pack-candidate")
        elif season_sized:
            score += 18
            reasons.append("season-sized-pack")
        else:
            score -= 55
            reasons.append("not-a-pack")

        if episode_range:
            start, end = episode_range
            reasons.append(f"range={start}-{end}")
            single_unit = str(anime.format or "").upper() == "MOVIE" or anime.episodes == 1
            if single_unit and end > max(1, int(anime.episodes or 1)):
                # "(01-07) [Batch]" is a TV version sharing the movie's title (Kimetsu no Yaiba:
                # Mugen Ressha-hen), not the movie: a range past a one-unit entry is another entry.
                score -= 200
                reasons.append("range-exceeds-entry")
            elif anime.episodes and start <= 1 and end >= anime.episodes:
                score += 24
                reasons.append("full-series-range")
            elif anime.episodes:
                coverage = min(1.0, range_count / max(1, anime.episodes))
                if start <= 1 and coverage >= 0.75:
                    score += 68
                    reasons.append("large-series-range")
                elif range_count >= 3:
                    score += 18
                    score -= max(0.0, (0.75 - coverage) * 80.0)
                    reasons.append("partial-series-range")
            elif range_count >= 3:
                score += min(55.0, range_count * 4.0)
                reasons.append("multi-episode-range")
        elif plausible_single_episode is not None and not is_pack:
            score -= 125
            reasons.append(f"single-episode={plausible_single_episode}")

        if release.size_bytes:
            size_gib = release.size_bytes / 1024**3
            reasons.append(f"size={size_gib:.1f}GiB")
            if size_gib >= 4:
                # Whole-season packs are normally several GiB. Make this strong enough
                # to beat deceptively well-seeded single episodes.
                score += min(115.0, 58.0 + math.log2(size_gib / 4.0 + 1.0) * 30.0)
                reasons.append("large-series-size")
            elif anime.episodes and anime.episodes >= 8 and size_gib < 2:
                score -= 75
                reasons.append("too-small-for-series")
            elif anime.episodes and anime.episodes >= 4 and size_gib < 1:
                score -= 55
                reasons.append("far-too-small-for-series")
    elif episode is not None:
        episode_range = release_episode_range(release.title)
        pair_range = episode_range is not None and _season_episode_pair_range(
            release.title, anime, episode, alternative_episodes
        )
        if pair_range:
            episode_range = None
        double = release_double_episode(release.title, episode, anime, alternative_episodes)
        if double is not None:
            # Two episodes in one file: usable, but a single-episode release
            # of the same quality ranks above it (audit 17А).
            score += 34 - 30
            if double[0] == episode:
                reasons.append(f"ep={episode}")
            else:
                reasons.append(f"absolute-ep={double[0]}")
                reasons.append(f"relative-ep={episode}")
            reasons.append(f"double-episode={double[0]}-{double[1]}")
        elif (release.is_batch and not pair_range) or episode_range is not None:
            score -= 500
            reasons.append("episode-pack")
            if episode_range is not None:
                reasons.append(f"range={episode_range[0]}-{episode_range[1]}")
        elif episode_match is not None and episode_match.status in {"rejected", "conflict"}:
            score -= 500
            reasons.append("wrong-episode")
            if found_episode is not None:
                reasons.append(f"found-episode={found_episode}")
            if episode_match.rejection_reason:
                reasons.append(episode_match.rejection_reason)
        elif found_episode == episode and (
            episode_match is None or episode_match.mapped_media_episode == found_episode
        ):
            score += 34
            reasons.append(f"ep={episode}")
        elif found_episode in {int(value) for value in alternative_episodes if int(value) > 0}:
            score += 34
            reasons.append(f"absolute-ep={found_episode}")
            reasons.append(f"relative-ep={episode}")
        elif found_episode is None:
            score -= 90
            reasons.append("episode-not-specified")
        elif explicit_episode:
            score -= 500
            reasons.append("wrong-episode")
            reasons.append(f"found-episode={found_episode}")
        else:
            score -= min(110, 55 + abs(found_episode - episode) * 6)
            reasons.append("ambiguous-episode-mismatch")
            reasons.append(f"found-episode={found_episode}")

    if release.trusted:
        score += 18
        reasons.append("trusted")
    if release.remake:
        score -= 35
        reasons.append("remake")
    if _contains_any(release.group, blocked_groups):
        score -= 100
        reasons.append("blocked-group")
    if _contains_any(release.group, trusted_groups):
        score += 18
        reasons.append(f"group={release.group}")
    elif _contains_any(release.group, preferred_groups):
        score += 10
        reasons.append(f"group={release.group}")

    quality, quality_reasons = _quality_score(release.title, preferred_resolution)
    score += quality
    reasons.extend(quality_reasons)
    if re.search(r"(?i)\b(WEB[- .]?DL|WEBRip)\b", release.title):
        score += 6
        reasons.append("WEB")
    if re.search(r"(?i)\b(BluRay|BDRip|BDMV|REMUX|BD)\b", release.title):
        score += 7
        reasons.append("BD")
    if re.search(r"(?i)\b(HEVC|x265|AV1|10bit|10-bit)\b", release.title):
        score += 3

    policy_score, policy_reasons = _video_policy_score(
        release.title,
        preferred_video_codecs=preferred_video_codecs or ["HEVC", "AV1", "AVC"],
        preferred_sources=preferred_sources or ["BluRay", "WEB-DL", "WEBRip"],
        require_japanese_audio=require_japanese_audio,
        avoid_upscaled=avoid_upscaled,
    )
    score += policy_score
    reasons.extend(policy_reasons)

    if re.search(r"(?i)\b(MultiSub|Multiple Subtitle|Dual Subtitle|ENG SUB|English)\b", release.title):
        score += 9
        reasons.append("eng/multisub")
    if re.search(r"(?i)\b(JPN? SUB|Japanese Sub|jpn[ ._-]?sub)\b", release.title):
        score += 13
        reasons.append("jp-subs")

    official_subsplease = release.category_id == "subsplease-rss"
    fresh_trusted_zero_seeders = fresh_trusted_zero_seeders_allowed(release, trusted_groups)
    if official_subsplease:
        score += 18
        reasons.extend(["official-subsplease-rss", "seeders-unknown"])
    elif fresh_trusted_zero_seeders:
        reasons.append("fresh-trusted-zero-seeders")
    elif not release.seeders_known:
        # A mirror without swarm counts is not a dead torrent: no penalty, but a
        # release with known seeders still wins a close call.
        score -= 8
        reasons.append("seeders-unknown")
    elif release.seeders <= 0:
        score -= 60
        reasons.append("no-seeders")
    elif release.seeders < min_seeders:
        score -= 48
        reasons.append("few-seeders")
    if not official_subsplease and release.seeders_known:
        seed_bonus = _seed_availability_bonus(release.seeders)
        score += seed_bonus
        reasons.append(f"seeds={release.seeders}")
        leecher_bonus = _leecher_activity_bonus(release.leechers, release.seeders)
        score += leecher_bonus
        if release.leechers > 0:
            reasons.append(f"leechers={release.leechers}")
    score += min(10.0, math.log10(max(1, release.downloads + 1)) * 2.5)

    freshness, freshness_reasons = _freshness_score(release.published, batch=batch)
    score += freshness
    reasons.extend(freshness_reasons)

    if not batch:
        size_quality, size_reasons = _episode_size_quality_score(release.size_bytes, anime)
        score += size_quality
        reasons.extend(size_reasons)
        if release.size_bytes > target_episode_max_bytes * 2:
            score -= 8
            reasons.append("too-large")
        elif (
            release.size_bytes and release.size_bytes < min(target_episode_min_bytes, 800 * 1024 * 1024) // 2
        ):
            score -= 8
            reasons.append("below-configured-minimum")

    # SeaDex: exact hash / Nyaa id only; computed from scratch on every call,
    # so re-scoring never doubles it. It does not make a release "trusted".
    seadex_bonus, seadex_reasons = bonus_for(seadex, release.info_hash, release.link)
    if seadex_bonus > 0 and _sd_release_against_preference(release.title, preferred_resolution):
        # SeaDex picks a DVD encode when the Blu-ray is an upscale (Baccano!); a set
        # "highest"/1080p preference says the user wants the HD release, so the SD pick
        # gets no bonus over it.
        seadex_bonus, seadex_reasons = 0.0, ["seadex-sd-pick-no-bonus"]
    score += seadex_bonus
    reasons.extend(seadex_reasons)

    identity = (
        {}
        if episode_match is None
        else {
            "mapped_media_episode": episode_match.mapped_media_episode,
            "raw_release_episode": episode_match.raw_release_episode,
            "numbering_status": episode_match.status,
            "numbering_scheme": episode_match.scheme_id,
            "numbering_rule": episode_match.rule_id,
            "numbering_revision": episode_match.rule_revision,
        }
    )
    if episode_match is not None and not batch and episode is not None:
        double = release_double_episode(release.title, episode, anime, alternative_episodes)
        if double is not None:
            identity.update(
                mapped_media_episode=int(episode),
                raw_release_episode=double[0],
                numbering_status="resolved",
                numbering_scheme="double-episode",
                numbering_rule="double-episode",
            )
    return replace(release, score=round(score, 3), reasons=reasons, **identity)


def _release_search_title_variants(value: str) -> tuple[str, ...]:
    """Return narrow search/identity aliases for split-stage AniList titles.

    Release groups commonly omit AniList's split-stage suffix entirely.  Put
    the bare series title first so a bounded automatic search does not spend
    its whole wall-clock budget probing a verbose title that no release uses.
    The original title remains as a fallback and identity alias.
    """
    original = str(value or "").strip()
    if not original:
        return ()
    stripped = STAGE_SPLIT_SUFFIX_RE.sub("", original).strip(" -:–—")
    if stripped and stripped != original:
        return (stripped, original)
    return (original,)


# Query plan of one release search. The old plan crossed every title alias with
# every suffix (S02, 2nd Season, batch, complete, 01-12, 1-12 / 01, 1, E01,
# S01E01, S02E01): ~50 queries for a season pack and 90 for one episode, so the
# 30 s deadline expired with most of them unsent.
BATCH_QUERY_LIMIT = 12
EPISODE_QUERY_LIMIT = 8


def _query_key(value: str) -> str:
    """Case, punctuation and spacing do not make a different RSS query."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(value or "")).casefold())


def _season_episode_query(title: str, season: int, episode: int) -> str:
    """``<title> SxxEyy``; a title's own "Season 2" / "2nd Season" becomes the Sxx part."""
    base = title
    if _explicit_season_numbers(title) == {season}:
        for pattern in _SEASON_MARKER_RES:
            base = pattern.sub(" ", base)
        base = re.sub(r"\s+", " ", base).strip(" -:–—_") or title
    return f"{base} S{season:02d}E{episode:02d}"


def _scene_named_title(title: str) -> bool:
    """Latin-script titles: scene names (SxxEyy) never use a Japanese-script title."""
    return bool(re.search(r"[A-Za-z]", title)) and not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", title)


def _sequel_short_title(anime: LibraryAnime, main_titles: list[str]) -> tuple[int, str] | None:
    """(season, title) for a sequel's "<title> S2 - 06" episode releases, None when not a sequel.

    The first season's title from the relation graph, else this entry's own
    "<title> Season 2" / "2nd Season" title without the marker. None when that
    title is already a main query title (it would only repeat a query).
    """
    root_season = _first_season_query_season(anime)
    candidates: list[tuple[int, str]] = []
    if root_season:
        candidates.extend((root_season, str(title)) for title in getattr(anime, "chain_titles", ()) or ())
    season = _expected_season(anime)
    if season > 1:
        candidates.extend((value, base) for value, base in _season_marked_titles(anime) if value == season)
    main_keys = {_query_key(value) for value in main_titles}
    for number, title in candidates:
        value = fold_search_title(title).strip() or title.strip()
        if value and _query_key(value) not in main_keys:
            return number, value
    return None


def release_search_queries(
    anime: LibraryAnime,
    *,
    episode: int | None,
    batch: bool,
    alternative_episodes: tuple[int, ...] = (),
    alternative_titles: tuple[str, ...] = (),
    max_queries: int = 5,
) -> list[str]:
    """The RSS queries of one search: distinct titles first, then a few suffixes.

    An RSS title query already lists that title's batches and episodes, so a
    suffix only narrows a full result page: season variants for the main two
    titles, "batch" for the main title, and episode numbers for the main title
    (absolute numbers also for a prequel title).  ``max_queries`` is the number
    of distinct titles searched.
    """
    keys: set[str] = set()

    def distinct(titles: Iterable[str]) -> list[str]:
        result: list[str] = []
        for title in titles:
            for variant in _release_search_title_variants(title):
                # Nyaa titles frequently use plain ASCII even when AniList stores
                # decorative Latin characters (é, ō, œ, ø, ...). Search both forms.
                for value in (fold_search_title(variant).strip(), variant.strip()):
                    key = _query_key(value)
                    if value and key and key not in keys:
                        keys.add(key)
                        result.append(value)
        return result

    # Romaji (without a split-stage suffix first), English, native; then the
    # prequel titles of absolute numbering; synonyms last.
    main = distinct([anime.title, *anime.titles])
    prequel = distinct(alternative_titles)
    titles = [*main, *prequel, *distinct(anime.synonyms)][: max(1, int(max_queries))]
    if not titles or (not batch and episode is None):
        return []
    primary = titles[0]
    season = _expected_season(anime)
    plan: list[str] = []
    if batch:
        plan.extend(titles)
        if season > 1:
            for title in [value for value in main[:2] if value in titles]:
                if _season_number(title) is None:
                    plan.extend([f"{title} S{season:02d}", f"{title} {_ordinal(season)} Season"])
        for own_number, base in _own_numbered_title_seasons(anime)[:1]:
            if own_number != season:
                # "Cyberpunk Edgerunners S02" names "Cyberpunk: Edgerunners 2" without its number.
                plan.append(f"{base} S{own_number:02d}")
        root_season = _first_season_query_season(anime)
        for title in distinct(getattr(anime, "chain_titles", ()) or ())[:1] if root_season else ():
            # A named sequel's packs carry the first season's title: "... S2", "... Season 2".
            plan.extend([f"{title} S{root_season}", f"{title} {root_season}"])
        plan.append(f"{primary} batch")
        limit = BATCH_QUERY_LIMIT
    else:
        number = int(episode)
        absolute = [int(value) for value in dict.fromkeys(alternative_episodes) if int(value) > 0]
        absolute = [value for value in absolute if value != number]
        prequel_titles = [value for value in prequel if value in titles]
        others = [value for value in titles[1:] if value not in prequel_titles]
        if absolute:
            # A title-only probe first: the scene season of a split-cour release
            # (S06E02 for local episode 1) cannot be guessed.
            plan.append(primary)
        plan.extend([f"{primary} {number:02d}", f"{primary} {number}"])
        for own_number, base in _own_numbered_title_seasons(anime)[:1]:
            if own_number != season:
                # "Cyberpunk Edgerunners S02E01" names "Cyberpunk: Edgerunners 2" without its number.
                plan.append(f"{base} S{own_number:02d}E{number:02d}")
        for value in absolute:
            # Nyaa tokenizes S01E43 differently from a standalone 43.
            plan.extend([f"{primary} {value:02d}", f"{primary} S01E{value:02d}"])
        for title in prequel_titles[:1]:
            plan.append(title)
            plan.extend(f"{title} {value:02d}" for value in absolute)
        plan.append(_season_episode_query(primary, season, number))
        sequel = _sequel_short_title(anime, main)
        if sequel is not None:
            # "[HorribleSubs] Shouwa Genroku Rakugo Shinjuu S2 - 06" names "...: Sukeroku
            # Futatabi-hen" by the first season's title: no query above has its words.
            plan.append(f"{sequel[1]} S{sequel[0]} {number:02d}")
        # Every other title's plain query first, the SxxEyy forms after them: most releases are
        # named "- 06", and a per-title pair pushed the last synonyms' plain queries past the cap.
        plan.extend(title if absolute else f"{title} {number:02d}" for title in others)
        for title in others:
            if _scene_named_title(title):
                # "Assassination Classroom S01E11" is one Nyaa token, never found by "... 11".
                plan.append(_season_episode_query(title, season, number))
        limit = EPISODE_QUERY_LIMIT
    queries: list[str] = []
    seen: set[str] = set()
    for query in plan:
        key = _query_key(query)
        if key not in seen:
            seen.add(key)
            queries.append(query)
    return queries[:limit]


def _manual_query_key(client: NyaaClient, query: str) -> tuple[str, ...]:
    return (client.base_url, str(client.category), str(client.proxy_mode), str(client.proxy_url), query)


def _manual_parallel_search(
    client: NyaaClient, queries: list[str]
) -> tuple[dict[str, list[NyaaRelease]], list[str], bool]:
    """Run a manual search's RSS queries 3 at a time under one deadline.

    Results are merged in the original query order by the caller, so the
    outcome does not depend on which request finished first.
    """
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    found: dict[str, list[NyaaRelease]] = {}
    errors: list[str] = []
    now = time.monotonic()
    pending_queries: list[str] = []
    with _MANUAL_QUERY_CACHE_LOCK:
        for key in [
            key
            for key, (stamp, _rows) in _MANUAL_QUERY_CACHE.items()
            if now - stamp >= MANUAL_QUERY_CACHE_SECONDS
        ]:
            _MANUAL_QUERY_CACHE.pop(key, None)
        for query in queries:
            cached = _MANUAL_QUERY_CACHE.get(_manual_query_key(client, query))
            if cached is not None:
                found[query] = [replace(release) for release in cached[1]]
            else:
                pending_queries.append(query)
    if not pending_queries:
        return found, errors, False
    deadline = now + MANUAL_SEARCH_DEADLINE_SECONDS
    executor = ThreadPoolExecutor(max_workers=MANUAL_SEARCH_WORKERS, thread_name_prefix="nyaa-search")
    exhausted = False
    try:
        if isinstance(client, NyaaClient):
            # The same deadline inside each query: an abandoned request used to keep
            # its worker thread busy for another minute after the search returned.
            futures = {
                executor.submit(client.search, query, deadline=deadline): query for query in pending_queries
            }
        else:
            futures = {executor.submit(client.search, query): query for query in pending_queries}
        remaining = set(futures)
        while remaining:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                exhausted = True
                # An expired deadline used to be silent: only the ~30 s duration showed it.
                _LOGGER.warning(
                    "WARN step=nyaa.search_deadline queries=%s cached=%s done=%s pending=%s deadline_s=%.0f",
                    len(queries),
                    len(queries) - len(pending_queries),
                    len(pending_queries) - len(remaining),
                    len(remaining),
                    MANUAL_SEARCH_DEADLINE_SECONDS,
                )
                break
            done, remaining = wait(remaining, timeout=timeout, return_when=FIRST_COMPLETED)
            for future in done:
                query = futures[future]
                try:
                    rows = future.result()
                except NyaaError as exc:
                    errors.append(f"{query}: {exc}")
                    continue
                found[query] = rows
                with _MANUAL_QUERY_CACHE_LOCK:
                    _MANUAL_QUERY_CACHE[_manual_query_key(client, query)] = (
                        time.monotonic(),
                        [replace(release) for release in rows],
                    )
    finally:
        # Queries not started before the deadline are dropped; a request already
        # in flight finishes in the background and is discarded.
        executor.shutdown(wait=False, cancel_futures=True)
    return found, errors, exhausted


def _trusted_explicit_episode_match(
    release: NyaaRelease,
    anime: LibraryAnime,
    episode: int,
    *,
    trusted_groups: list[str],
    preferred_groups: list[str],
    blocked_groups: list[str],
    preferred_resolution: str,
    alternative_titles: tuple[str, ...],
    negative_titles: tuple[str, ...],
    alternative_episodes: tuple[int, ...],
    numbering_context: EpisodeNumbering | None,
) -> bool:
    """A trusted group's release of exactly this episode at the preferred resolution.

    Never a blocked group; with preferred groups set, only one of them (a later alias
    query may be the one that finds it).
    """
    group = release.group or release_group(release.title)
    if blocked_groups and _contains_any(group, blocked_groups):
        return False
    if preferred_groups and not _contains_any(group, preferred_groups):
        return False
    if not (release.trusted or _contains_any(group, trusted_groups)):
        return False
    resolution = RESOLUTION_RE.search(release.title)
    if not resolution or resolution.group(1).casefold() != str(preferred_resolution or "").casefold():
        return False
    found_episode, explicit = _release_episode_match(release.title)
    if not explicit or found_episode not in {int(episode), *(int(value) for value in alternative_episodes)}:
        return False
    if release_title_is_pack(release.title) and not _season_episode_pair_range(
        release.title, anime, episode, alternative_episodes
    ):
        return False
    return _release_is_eligible_for_episode(
        release.title, episode, anime, alternative_episodes, numbering_context
    ) and release_title_is_plausible(
        anime, release.title, alternative_titles, negative_titles, alternative_episodes
    )


@with_title_memo
def search_ranked(
    client: NyaaClient,
    anime: LibraryAnime,
    *,
    episode: int | None,
    batch: bool,
    trusted_groups: list[str],
    preferred_groups: list[str],
    blocked_groups: list[str],
    preferred_resolution: str,
    min_seeders: int,
    target_episode_min_bytes: int,
    target_episode_max_bytes: int,
    preferred_video_codecs: list[str] | None = None,
    preferred_sources: list[str] | None = None,
    require_japanese_audio: bool = True,
    avoid_upscaled: bool = True,
    alternative_episodes: tuple[int, ...] = (),
    alternative_titles: tuple[str, ...] = (),
    negative_titles: tuple[str, ...] = (),
    seadex: SeaDexRecommendation | None = None,
    max_queries: int = 5,
    query_budget_seconds: float | None = None,
    numbering_context: EpisodeNumbering | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> list[NyaaRelease]:
    """``should_continue``: checked before each query; False ends the search with what it has
    (a cooperative cancel, e.g. Torrent Off during the On toggle's search)."""
    if isinstance(client, NyaaClient) and not client.base_url:
        # One clear message instead of the same error for every alias query.
        raise NoReleaseSourceError(err("releases.noSource"))
    queries = release_search_queries(
        anime,
        episode=episode,
        batch=batch,
        alternative_episodes=alternative_episodes,
        alternative_titles=alternative_titles,
        max_queries=max_queries,
    )

    by_hash: dict[str, NyaaRelease] = {}
    search_errors: list[str] = []
    deadline = time.monotonic() + max(0.0, query_budget_seconds) if query_budget_seconds is not None else None
    budget_exhausted = False
    # True once any query got an answer (even an empty one).
    answered = False
    if query_budget_seconds is None and isinstance(client, NyaaClient):
        found_by_query, search_errors, budget_exhausted = _manual_parallel_search(client, queries)
        answered = bool(found_by_query)
        for query in queries:
            for release in found_by_query.get(query, []):
                key = release.info_hash or release.torrent_url or release.link
                if key not in by_hash:
                    by_hash[key] = release
        queries = []
    for query in queries:
        if should_continue is not None and not should_continue():
            break
        if deadline is not None and time.monotonic() >= deadline:
            budget_exhausted = True
            break
        try:
            if deadline is not None and isinstance(client, NyaaClient):
                # The budget used to be checked only between queries, so one query
                # (mirrors x routes x 20 s) could overrun it several times.
                found = client.search(query, deadline=deadline)
            else:
                found = client.search(query)
        except NyaaError as exc:
            search_errors.append(f"{query}: {exc}")
            if query_budget_seconds is not None:
                # Automatic/background refresh has RSS fallbacks and must not
                # spend the whole budget retrying aliases over the same broken
                # Nyaa route. Manual searches keep the exhaustive alias behavior.
                budget_exhausted = True
                break
            continue
        answered = True
        for release in found:
            key = release.info_hash or release.torrent_url or release.link
            if key not in by_hash:
                by_hash[key] = release
        if (
            query_budget_seconds is not None
            and episode is not None
            and not batch
            and any(
                # A dead (0-seed) or remade upload is no reason to skip the other aliases:
                # they may find the same group's seeded release.
                not release.remake
                and not (release.seeders_known and release.seeders < max(1, int(min_seeders)))
                and _trusted_explicit_episode_match(
                    release,
                    anime,
                    episode,
                    trusted_groups=trusted_groups,
                    preferred_groups=preferred_groups,
                    blocked_groups=blocked_groups,
                    preferred_resolution=preferred_resolution,
                    alternative_titles=alternative_titles,
                    negative_titles=negative_titles,
                    alternative_episodes=alternative_episodes,
                    numbering_context=numbering_context,
                )
                for release in found
            )
        ):
            # An automatic search has what it needs: the other aliases would only
            # repeat requests (and spend the site's rate limit) for the same pick.
            break

    if not by_hash and not answered and budget_exhausted and not search_errors:
        # A manual search whose deadline passed before any query answered used to
        # return [] — shown as "nothing found" although nothing was searched.
        search_errors.append(err("nyaa.searchTimedOut"))
    if not by_hash and search_errors:
        suffix = "; automatic search budget exhausted" if budget_exhausted else ""
        message = "; ".join(dict.fromkeys(search_errors)) + suffix
        if not answered:
            # Nothing answered at all: the search is unavailable, which callers
            # must not treat as "no release exists".
            raise ReleaseSearchUnavailableError(message)
        raise NyaaError(message)

    eligible_releases = [
        release
        for release in by_hash.values()
        if release_title_is_plausible(
            anime, release.title, alternative_titles, negative_titles, alternative_episodes
        )
        and not (
            episode is not None
            and not batch
            and not _release_is_eligible_for_episode(
                release.title, episode, anime, alternative_episodes, numbering_context
            )
        )
    ]

    ranked = [
        score_release(
            release,
            anime,
            episode=episode,
            batch=batch,
            trusted_groups=trusted_groups,
            preferred_groups=preferred_groups,
            blocked_groups=blocked_groups,
            preferred_resolution=preferred_resolution,
            min_seeders=min_seeders,
            target_episode_min_bytes=target_episode_min_bytes,
            target_episode_max_bytes=target_episode_max_bytes,
            preferred_video_codecs=preferred_video_codecs,
            preferred_sources=preferred_sources,
            require_japanese_audio=require_japanese_audio,
            avoid_upscaled=avoid_upscaled,
            alternative_episodes=alternative_episodes,
            alternative_titles=alternative_titles,
            seadex=seadex,
            numbering_context=numbering_context,
        )
        for release in eligible_releases
    ]
    ranked.sort(key=lambda item: (item.score, item.seeders, item.downloads), reverse=True)
    return ranked


_SEADEX_VIDEO_RE = re.compile(r"(?i)\.(?:mkv|mp4|m4v|avi|webm|ts|m2ts)$")


def _seadex_release_title(torrent: SeaDexTorrent) -> str:
    """A release title from SeaDex's own data: what the episode file names share.

    "[Cleo] Show - 01 [1080p].mkv" ... "[Cleo] Show - 12 [1080p].mkv" give
    "[Cleo] Show [1080p]"; one file gives its name. Without file names the
    "grouped" search link's query is used. Nothing is made up from the AniList
    title, so the usual identity checks still mean something.
    """
    names = [name.replace("\\", "/").rsplit("/", 1)[-1] for name, _size in torrent.files]
    stems = [re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name) for name in names if _SEADEX_VIDEO_RE.search(name)]
    stems = [stem for stem in stems if stem.strip()]
    if len(stems) == 1:
        return stems[0].strip()
    if stems:
        prefix = os.path.commonprefix(stems)
        # Drop the part of the episode token the names share ("- 0" of 01-09).
        prefix = re.sub(r"[^\s._\-\[\]()]*$", "", prefix).rstrip(" ._-[(")
        suffix = os.path.commonprefix([stem[::-1] for stem in stems])[::-1]
        starts = [index for index in (suffix.find("["), suffix.find("(")) if index >= 0]
        suffix = suffix[min(starts) :].strip() if starts else ""
        title = f"{prefix} {suffix}".strip()
        if len(_query_key(prefix)) >= 3:
            return title
    query = parse_qs(urlparse(torrent.grouped_url).query).get("q") if torrent.grouped_url else None
    return str(query[0]).strip() if query else ""


@with_title_memo
def seadex_batch_candidates(
    seadex: SeaDexRecommendation | None,
    anime: LibraryAnime,
    *,
    alternative_titles: tuple[str, ...] = (),
    negative_titles: tuple[str, ...] = (),
    alternative_episodes: tuple[int, ...] = (),
    **score_kwargs,
) -> list[NyaaRelease]:
    """SeaDex's releases of this entry as scored season-pack candidates.

    Only torrents with an info hash (a magnet is the download route) and a
    title from SeaDex's data are offered. The size is the sum of the listed
    files (0 when SeaDex lists none: never guessed). A file per episode makes
    it a pack; without a file list SeaDex's "best" flag is that evidence
    (its entries are whole-season releases). The title identity check, the
    related-title/season guards and the usual scoring still decide.
    """
    if seadex is None or not seadex.usable:
        return []
    episodes = int(anime.episodes or 0)
    candidates: list[NyaaRelease] = []
    for torrent in getattr(seadex, "torrents", ()) or ():
        if not torrent.info_hash:
            continue
        title = _seadex_release_title(torrent)
        if not title:
            continue
        videos = [name for name, _size in torrent.files if _SEADEX_VIDEO_RE.search(name)]
        # A file named for another season ("S01E05" for season 2) is the wrong season.
        seasons = set().union(*(_explicit_season_numbers(name) for name in videos)) if videos else set()
        if seasons and (len(seasons) > 1 or not seasons <= _accepted_seasons(anime)):
            continue
        if not release_title_is_plausible(
            anime, title, alternative_titles, negative_titles, alternative_episodes
        ):
            continue
        size = sum(length for _name, length in torrent.files if length > 0)
        pack = (episodes > 1 and len(videos) >= episodes) or (not torrent.files and torrent.best)
        release = NyaaRelease(
            title=title,
            link=torrent.url,
            torrent_url="",
            info_hash=torrent.info_hash,
            size_text=f"{size / 1024**3:.1f} GiB" if size else "",
            size_bytes=size,
            seeders=0,
            leechers=0,
            downloads=0,
            trusted=False,
            remake=False,
            category_id="seadex",
            is_batch=bool(pack or release_title_is_pack(title)),
            group=torrent.release_group or release_group(title),
            seeders_known=False,
        )
        candidates.append(
            score_release(
                release,
                anime,
                episode=None,
                batch=True,
                alternative_titles=alternative_titles,
                alternative_episodes=alternative_episodes,
                seadex=seadex,
                **score_kwargs,
            )
        )
    candidates.sort(key=lambda item: item.score, reverse=True)
    return candidates


_TITLE_MATCH_REASONS = ("exact-title", "alias-title", "fuzzy-title")


def _named_like_title(anime: LibraryAnime, title: str, alternative_titles: tuple[str, ...]) -> bool:
    """The title score's own verdict that a release names this title (see search_shana_ranked)."""
    _score, reasons = _title_match_score(anime, title, alternative_titles)
    return any(reason.startswith(_TITLE_MATCH_REASONS) for reason in reasons)


@with_title_memo
def search_shana_ranked(
    client: ShanaProjectClient,
    anime: LibraryAnime,
    *,
    episode: int | None,
    batch: bool,
    trusted_groups: list[str],
    preferred_groups: list[str],
    blocked_groups: list[str],
    preferred_resolution: str,
    min_seeders: int,
    target_episode_min_bytes: int,
    target_episode_max_bytes: int,
    preferred_video_codecs: list[str] | None = None,
    preferred_sources: list[str] | None = None,
    require_japanese_audio: bool = True,
    avoid_upscaled: bool = True,
    alternative_episodes: tuple[int, ...] = (),
    alternative_titles: tuple[str, ...] = (),
    negative_titles: tuple[str, ...] = (),
    seadex: SeaDexRecommendation | None = None,
    numbering_context: EpisodeNumbering | None = None,
) -> list[NyaaRelease]:
    ranked = [
        score_release(
            release,
            anime,
            episode=episode,
            batch=batch,
            trusted_groups=trusted_groups,
            preferred_groups=preferred_groups,
            blocked_groups=blocked_groups,
            preferred_resolution=preferred_resolution,
            min_seeders=min_seeders,
            target_episode_min_bytes=target_episode_min_bytes,
            target_episode_max_bytes=target_episode_max_bytes,
            preferred_video_codecs=preferred_video_codecs,
            preferred_sources=preferred_sources,
            require_japanese_audio=require_japanese_audio,
            avoid_upscaled=avoid_upscaled,
            alternative_episodes=alternative_episodes,
            alternative_titles=alternative_titles,
            seadex=seadex,
            numbering_context=numbering_context,
        )
        for release in client.releases()
        # Named like this title first (the same test as the filter below, which only sees
        # what scoring kept): the episode checks parse every kept feed item with Anitomy.
        if _named_like_title(anime, release.title, alternative_titles)
        # "01-02" looks like a pack but is two episodes in one file (audit 17А), and
        # "KonoSuba 3 - 05" is the title's number and the episode.
        and (
            release.is_batch
            if batch
            else (
                not release.is_batch
                or double_episode_span(release.title) is not None
                or (
                    episode is not None
                    and _season_episode_pair_range(release.title, anime, episode, alternative_episodes)
                )
            )
        )
        and (
            batch
            or episode is None
            or _release_is_eligible_for_episode(
                release.title, episode, anime, alternative_episodes, numbering_context
            )
        )
    ]
    ranked = [
        item
        for item in ranked
        if any(reason.startswith(_TITLE_MATCH_REASONS) for reason in item.reasons)
        and release_title_is_plausible(
            anime, item.title, alternative_titles, negative_titles, alternative_episodes
        )
    ]
    ranked.sort(key=lambda item: (item.score, item.published), reverse=True)
    return ranked


@with_title_memo
def search_subsplease_ranked(
    client: SubsPleaseClient,
    anime: LibraryAnime,
    *,
    episode: int | None,
    batch: bool,
    trusted_groups: list[str],
    preferred_groups: list[str],
    blocked_groups: list[str],
    preferred_resolution: str,
    min_seeders: int,
    target_episode_min_bytes: int,
    target_episode_max_bytes: int,
    preferred_video_codecs: list[str] | None = None,
    preferred_sources: list[str] | None = None,
    require_japanese_audio: bool = True,
    avoid_upscaled: bool = True,
    alternative_episodes: tuple[int, ...] = (),
    alternative_titles: tuple[str, ...] = (),
    negative_titles: tuple[str, ...] = (),
    seadex: SeaDexRecommendation | None = None,
    numbering_context: EpisodeNumbering | None = None,
) -> list[NyaaRelease]:
    releases = client.releases(preferred_resolution)
    eligible: list[NyaaRelease] = []
    for release in releases:
        # The title first: the kept feed history holds up to 5000 items of every show,
        # and the episode checks below parse each name with Anitomy (~0.4 ms, more
        # names than its cache keeps). Both checks must pass; only the order changed.
        title_score, _title_reasons = _title_match_score(anime, release.title, alternative_titles)
        if title_score < 25.0:
            continue
        if batch:
            if not (release.is_batch or release_episode_range(release.title) is not None):
                continue
        elif episode is not None and not _release_is_eligible_for_episode(
            release.title, episode, anime, alternative_episodes, numbering_context
        ):
            continue
        if not release_title_is_plausible(
            anime, release.title, alternative_titles, negative_titles, alternative_episodes
        ):
            continue
        eligible.append(release)

    ranked = [
        score_release(
            release,
            anime,
            episode=episode,
            batch=batch,
            trusted_groups=trusted_groups,
            preferred_groups=preferred_groups,
            blocked_groups=blocked_groups,
            preferred_resolution=preferred_resolution,
            min_seeders=min_seeders,
            target_episode_min_bytes=target_episode_min_bytes,
            target_episode_max_bytes=target_episode_max_bytes,
            preferred_video_codecs=preferred_video_codecs,
            preferred_sources=preferred_sources,
            require_japanese_audio=require_japanese_audio,
            avoid_upscaled=avoid_upscaled,
            alternative_episodes=alternative_episodes,
            alternative_titles=alternative_titles,
            seadex=seadex,
            numbering_context=numbering_context,
        )
        for release in eligible
    ]
    ranked.sort(key=lambda item: (item.score, item.published), reverse=True)
    return ranked
