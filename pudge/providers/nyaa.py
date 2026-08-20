from __future__ import annotations

import base64
import binascii

import json
import math
import re
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

from ..filename import fold_search_title, normalize_title, title_similarity
from ..branding import APP_SLUG
from ..manager_models import LibraryAnime, NyaaRelease
from .base import CircuitBreaker


NYAA_NS = "https://nyaa.si/xmlns/nyaa"
GROUP_RE = re.compile(r"^\s*\[([^\]]+)\]")
SUFFIX_GROUP_RE = re.compile(
    r"-(?P<group>[A-Za-z][A-Za-z0-9._-]{1,31})(?:\s+\([^)]*\))?\s*$"
)
FRESH_TRUSTED_ZERO_SEEDER_MAX_HOURS = 24.0
EPISODE_PATTERNS = (
    re.compile(r"(?i)\bS\d{1,2}E(?P<ep>\d{1,4})\b"),
    re.compile(r"(?i)\bE(?:P(?:ISODE)?)?[ ._-]?(?P<ep>\d{1,4})\b"),
    re.compile(r"(?:^|[\s._-])-(?:[\s._-])*(?P<ep>\d{1,4})(?:v\d+)?(?:[\s._-]|$)"),
    re.compile(r"(?:^|[\s._-])(?P<ep>\d{2,4})(?:v\d+)?(?:[\s._-]|$)"),
)
BATCH_RE = re.compile(
    r"(?i)\b(batch|complete|全集|全話|season\s*pack|\d{1,3}\s*[-~]\s*\d{1,3})\b"
)
VOLUME_MARKER_RE = re.compile(r"(?i)\bvol(?:ume)?[ ._-]*0*\d{1,3}\b")
EPISODE_RANGE_PATTERNS = (
    re.compile(
        r"(?i)\bS\d{1,2}E(?P<start>\d{1,3})\s*[-~]\s*(?:S\d{1,2}E)?(?P<end>\d{1,3})\b"
    ),
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
    re.compile(r"(?i)\bS(?:eason)?[ ._-]*0*(?P<season>\d{1,2})(?:\b|(?=E\d))"),
    re.compile(r"(?i)\b0*(?P<season>\d{1,2})(?:st|nd|rd|th)[ ._-]+Season\b"),
    re.compile(r"(?i)\bSeason[ ._-]*0*(?P<season>\d{1,2})\b"),
    re.compile(r"第\s*0*(?P<season>\d{1,2})\s*期"),
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


class NyaaError(RuntimeError):
    pass


SUBSPLEASE_RSS_BASE = "https://subsplease.org/rss/"
SHANA_PUBLIC_FEED = "https://www.shanaproject.com/feeds/site/"


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
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(path)
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
            info_hash = xt[len(prefix):].strip().lower()
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
        raise NyaaError(f"SubsPlease returned invalid RSS: {exc}") from exc

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
                is_batch=bool(BATCH_RE.search(title)),
                group="SubsPlease",
            )
        )
    return releases


class SubsPleaseClient:
    def __init__(
        self,
        *,
        timeout: float = 8.0,
        cache_ttl: float = 300.0,
        history_path: Path | None = None,
    ) -> None:
        self.timeout = timeout
        self.cache_ttl = max(0.0, cache_ttl)
        self.history_path = history_path
        self._cache: dict[str, tuple[float, list[NyaaRelease]]] = {}

    @staticmethod
    def feed_url(preferred_resolution: str) -> str:
        resolution = preferred_resolution.casefold().strip()
        if "720" in resolution:
            value = "720"
        elif "1080" in resolution or resolution in {"highest", "best", "max"} or "1440" in resolution or "2160" in resolution:
            # SubsPlease RSS currently exposes 1080p as its highest standard
            # feed. Higher/best preferences are still applied to Nyaa scoring.
            value = "1080"
        else:
            value = "sd"
        # Official magnet RSS. Magnet entries include the info hash, which lets
        # qBittorrent verify and tag the torrent immediately after adding it.
        return f"{SUBSPLEASE_RSS_BASE}?r={value}"

    def releases(self, preferred_resolution: str) -> list[NyaaRelease]:
        url = self.feed_url(preferred_resolution)
        cached = self._cache.get(url)
        now = time.monotonic()
        if cached is not None and now - cached[0] < self.cache_ttl:
            return list(cached[1])
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
                return list(releases)
        except httpx.HTTPError as exc:
            cached = _load_release_history(self.history_path)
            if cached:
                return cached
            raise NyaaError(f"SubsPlease RSS unavailable: {exc}") from exc




def parse_shana_rss(xml_text: str) -> list[NyaaRelease]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NyaaError(f"Shana Project returned invalid RSS: {exc}") from exc

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
                info_hash=_magnet_info_hash(download_url) or (hash_match.group(1).lower() if hash_match else ""),
                size_text=size_text,
                size_bytes=parse_size(size_text),
                seeders=0,
                leechers=0,
                downloads=0,
                trusted=group.casefold() == "subsplease",
                remake=False,
                category_id="shana-rss",
                published=_text(item, "pubDate"),
                is_batch=bool(BATCH_RE.search(title)),
                group=group,
            )
        )
    return releases


class ShanaProjectClient:
    def __init__(
        self,
        *,
        timeout: float = 8.0,
        cache_ttl: float = 600.0,
        history_path: Path | None = None,
    ) -> None:
        self.timeout = timeout
        self.cache_ttl = max(0.0, cache_ttl)
        self.history_path = history_path
        self._cache: tuple[float, list[NyaaRelease]] | None = None

    def releases(self) -> list[NyaaRelease]:
        now = time.monotonic()
        if self._cache is not None and now - self._cache[0] < self.cache_ttl:
            return list(self._cache[1])
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": APP_SLUG},
            ) as client:
                response = client.get(SHANA_PUBLIC_FEED)
                response.raise_for_status()
                releases = _merge_release_history(
                    self.history_path,
                    parse_shana_rss(response.text),
                    source="shana",
                )
                self._cache = (time.monotonic(), releases)
                return list(releases)
        except httpx.HTTPError as exc:
            cached = _load_release_history(self.history_path)
            if cached:
                return cached
            raise NyaaError(f"Shana Project RSS unavailable: {exc}") from exc

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
    return int(amount * (1024 ** powers[unit]))


def _release_episode_match(title: str) -> tuple[int | None, bool]:
    """Return the parsed episode and whether the marker is explicit.

    SxxExx, E/EP markers and the common `` - 05 `` form are explicit enough
    to reject a different requested episode. A bare two-to-four digit token is
    kept as a low-confidence fallback because numbers can be part of an anime
    title (for example ``86``).
    """
    clean = re.sub(r"\[[^\]]*\]", " ", title)
    clean = VOLUME_MARKER_RE.sub(" ", clean)
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
    return episode


def _season_episode_pair_range(
    title: str,
    anime: LibraryAnime,
    requested_episode: int,
) -> bool:
    """Return true when a generic ``2 - 05`` match is season + episode.

    Bare sequel titles such as ``Otomege ... 2 - 05`` otherwise look like an
    episode range to the generic pack parser.  Require agreement between the
    AniList-derived expected season, the release season marker and the explicit
    episode marker before suppressing that false range.
    """
    return bool(
        _expected_season(anime) > 1
        and _season_number(title) == _expected_season(anime)
        and release_episode_range(title) == (_expected_season(anime), requested_episode)
        and _release_episode_match(title) == (requested_episode, True)
    )


def _release_is_eligible_for_episode(
    title: str,
    requested_episode: int,
    anime: LibraryAnime | None = None,
    alternative_episodes: tuple[int, ...] = (),
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
    episode_range = release_episode_range(title)
    if episode_range is not None and not (anime and _season_episode_pair_range(title, anime, requested_episode)):
        return False
    if BATCH_RE.search(title) and not (anime and _season_episode_pair_range(title, anime, requested_episode)):
        return False
    found_episode, explicit = _release_episode_match(title)
    allowed_episodes = {int(requested_episode), *(int(value) for value in alternative_episodes if int(value) > 0)}
    if explicit and found_episode not in allowed_episodes:
        return False
    return True


def release_episode_range(title: str) -> tuple[int, int] | None:
    clean = re.sub(r"\[(?!\s*\d{1,3}\s*[-~]\s*\d{1,3}\s*\])[^\]]*\]", " ", title)
    for pattern in EPISODE_RANGE_PATTERNS:
        match = pattern.search(clean)
        if not match:
            continue
        try:
            start = int(match.group("start"))
            end = int(match.group("end"))
        except (TypeError, ValueError):
            continue
        if 1 <= start < end <= 500:
            return start, end
    return None


def _text(item: ET.Element, name: str, default: str = "") -> str:
    node = item.find(name)
    return (node.text or "").strip() if node is not None else default


def _nyaa_text(item: ET.Element, name: str, default: str = "") -> str:
    return _text(item, f"{{{NYAA_NS}}}{name}", default)


def parse_rss(xml_text: str) -> list[NyaaRelease]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NyaaError(f"Nyaa вернул некорректный RSS: {exc}") from exc

    releases: list[NyaaRelease] = []
    for item in root.findall("./channel/item"):
        title = _text(item, "title")
        link = _text(item, "guid") or _text(item, "link")
        torrent_url = _text(item, "link")
        info_hash = _nyaa_text(item, "infoHash")
        size_text = _nyaa_text(item, "size")
        group = release_group(title)
        try:
            seeders = int(_nyaa_text(item, "seeders", "0") or 0)
            leechers = int(_nyaa_text(item, "leechers", "0") or 0)
            downloads = int(_nyaa_text(item, "downloads", "0") or 0)
        except ValueError:
            seeders = leechers = downloads = 0
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
                is_batch=bool(BATCH_RE.search(title)),
                group=group,
            )
        )
    return releases


class NyaaClient:
    def __init__(
        self,
        base_url: str = "https://nyaa.si",
        *,
        proxy_mode: str = "direct_then_proxy",
        proxy_url: str = "",
        pre_search_command: str = "",
        category: str = "1_2",
        timeout: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.proxy_mode = proxy_mode
        self.proxy_url = self._normalize_proxy_url(proxy_url)
        self.pre_search_command = pre_search_command.strip()
        self.category = category
        self.timeout = timeout
        self._breaker = CircuitBreaker(failure_threshold=3, recovery_seconds=30.0)
        self._clients: dict[str, httpx.Client] = {}
        self._client_lock = threading.Lock()

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

    def _run_hook(self) -> None:
        if not self.pre_search_command:
            return
        completed = subprocess.run(
            self.pre_search_command,
            shell=True,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise NyaaError(f"Команда перед поиском Nyaa завершилась с ошибкой: {detail}")

    def _get(self, url: str, proxy: str | None) -> str:
        if not self._breaker.allow():
            raise NyaaError("Nyaa temporarily paused after repeated network failures")
        try:
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
            response = client.get(url)
            response.raise_for_status()
            self._breaker.success()
            return response.text
        except ImportError as exc:
            self._breaker.failure()
            raise NyaaError(
                "Для SOCKS-прокси не установлена зависимость socksio. "
                "Повторно запустите ./install.sh."
            ) from exc
        except httpx.HTTPError as exc:
            self._breaker.failure()
            route = f" через {proxy}" if proxy else " напрямую"
            raise NyaaError(f"Nyaa недоступен{route}: {exc}") from exc

    def close(self) -> None:
        with self._client_lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            client.close()

    def search(self, query: str, *, category: str | None = None, filter_id: int = 0) -> list[NyaaRelease]:
        self._run_hook()
        selected_category = category or self.category
        url = (
            f"{self.base_url}/?page=rss&q={quote_plus(query)}"
            f"&c={quote_plus(selected_category)}&f={int(filter_id)}"
        )
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
        for proxy in attempts:
            if proxy is None and mode == "proxy_only":
                continue
            try:
                return parse_rss(self._get(url, proxy))
            except NyaaError as exc:
                errors.append(str(exc))
        raise NyaaError("; ".join(dict.fromkeys(errors)) or "Nyaa недоступен")


def release_group(title: str) -> str:
    prefix = GROUP_RE.match(title)
    if prefix:
        return prefix.group(1).strip()

    suffix = SUFFIX_GROUP_RE.search(title)
    if not suffix:
        return ""
    group = suffix.group("group").strip()
    normalized = _normalized_group(group)
    reserved = {
        "webdl", "webrip", "bluray", "bdrip", "remux",
        "h264", "x264", "h265", "x265", "hevc", "av1",
        "aac", "flac", "ddp", "ddp20", "ddp51",
    }
    return "" if normalized in reserved else group


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
    roman_match = re.search(r"(?i)(?:^|[ ._:-])(?P<roman>II|III|IV|V|VI|VII|VIII|IX|X)(?=\s*(?:$|[-._]\s*\d{1,3}\b|\[|\(|S\d{1,2}E))", title)
    if roman_match:
        return ROMAN_SEASONS.get(roman_match.group("roman").casefold())
    numeric_match = re.search(r"(?:^|[ ._:-])(?P<season>[2-9])(?=\s*(?:$|[-._]\s*\d{1,3}\b|\[|\(|S\d{1,2}E))", title)
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
            or re.search(r"(?i)\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)[ ._-]+Season\b", name)
        )
    ]
    if explicit:
        return max(explicit)
    if has_prequel and seasons:
        return max(seasons)
    return 1


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
    return max(0.0, (current - published.astimezone(timezone.utc)).total_seconds() / 86400.0)


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


def _quality_score(title: str, preferred_resolution: str) -> tuple[float, list[str]]:
    match = RESOLUTION_RE.search(title)
    if not match:
        return -8.0, ["resolution-unknown"]

    resolution = match.group(1).casefold()
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
    return any(release_tokens[index:index + width] == alias_tokens for index in range(len(release_tokens) - width + 1))


def _title_match_score(
    anime: LibraryAnime,
    release_title: str,
    alternative_titles: tuple[str, ...] = (),
) -> tuple[float, list[str]]:
    aliases = list(dict.fromkeys(
        value.strip()
        for value in [anime.title, *alternative_titles, *anime.titles, *anime.synonyms]
        if value.strip()
    ))
    if not aliases:
        return -200.0, ["title-missing"]

    similarities = [(alias, title_similarity(alias, release_title)) for alias in aliases]
    best_alias, best_similarity = max(similarities, key=lambda item: item[1])
    exact_aliases = [alias for alias in aliases if _token_phrase_present(alias, release_title)]
    if exact_aliases:
        exact_similarity = max(title_similarity(alias, release_title) for alias in exact_aliases)
        return exact_similarity * 0.45 + 62.0, [f"title={exact_similarity:.0f}", "exact-title-phrase"]

    alias_norm = normalize_title(best_alias)
    alias_tokens = alias_norm.split()
    release_tokens = set(normalize_title(release_title).split())
    coverage = (
        len(set(alias_tokens) & release_tokens) / len(set(alias_tokens))
        if alias_tokens
        else 0.0
    )

    # Short titles must appear as complete tokens. This blocks e.g. "Akira"
    # matching the unrelated word "Akiraka" while still accepting "AKIRA (1988)".
    if len(alias_tokens) == 1 and len(alias_norm) <= 12:
        return best_similarity * 0.20 - 185.0, [f"title={best_similarity:.0f}", "short-title-token-mismatch"]
    if coverage < 0.60:
        return best_similarity * 0.25 - 125.0, [f"title={best_similarity:.0f}", f"title-token-coverage={coverage:.2f}"]
    if coverage < 0.85:
        return best_similarity * 0.35 - 45.0, [f"title={best_similarity:.0f}", f"partial-title-coverage={coverage:.2f}"]
    return best_similarity * 0.40 - 15.0, [f"title={best_similarity:.0f}", "fuzzy-title-match"]


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
        and not re.search(r"(?i)\b(?:Dual[ ._-]?Audio|Multi[ ._-]?Audio|Japanese|JPN|JA[ ._-]?Audio)\b", title)
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
    elif release_season is not None and release_season != 1:
        score -= 110
        reasons.append(f"wrong-season={release_season}")

    found_episode, explicit_episode = _release_episode_match(release.title)
    if batch:
        episode_range = release_episode_range(release.title)
        range_count = episode_range[1] - episode_range[0] + 1 if episode_range else 0
        plausible_single_episode = (
            found_episode
            if found_episode is not None
            and (anime.episodes is None or found_episode <= max(3, anime.episodes + 3))
            else None
        )
        likely_pack = bool(
            release.is_batch
            or range_count >= 3
            or (release.size_bytes >= 4 * 1024**3 and plausible_single_episode is None)
        )

        if release.is_batch:
            score += 18
            reasons.append("batch")
        elif likely_pack:
            score += 18
            reasons.append("large-pack-candidate")
        else:
            score -= 55
            reasons.append("not-a-pack")

        if episode_range:
            start, end = episode_range
            reasons.append(f"range={start}-{end}")
            if anime.episodes and start <= 1 and end >= anime.episodes:
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
        elif plausible_single_episode is not None and not release.is_batch:
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
        if episode_range is not None and _season_episode_pair_range(release.title, anime, episode):
            episode_range = None
        if release.is_batch or episode_range is not None:
            score -= 500
            reasons.append("episode-pack")
            if episode_range is not None:
                reasons.append(f"range={episode_range[0]}-{episode_range[1]}")
        elif found_episode == episode:
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
    fresh_trusted_zero_seeders = fresh_trusted_zero_seeders_allowed(
        release, trusted_groups
    )
    if official_subsplease:
        score += 18
        reasons.extend(["official-subsplease-rss", "seeders-unknown"])
    elif fresh_trusted_zero_seeders:
        reasons.append("fresh-trusted-zero-seeders")
    elif release.seeders <= 0:
        score -= 60
        reasons.append("no-seeders")
    elif release.seeders < min_seeders:
        score -= 48
        reasons.append("few-seeders")
    if not official_subsplease:
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
        elif release.size_bytes and release.size_bytes < min(target_episode_min_bytes, 800 * 1024 * 1024) // 2:
            score -= 8
            reasons.append("below-configured-minimum")

    return replace(release, score=round(score, 3), reasons=reasons)


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
    max_queries: int = 5,
    query_budget_seconds: float | None = None,
) -> list[NyaaRelease]:
    raw_aliases = [anime.title, *alternative_titles, *anime.titles, *anime.synonyms]
    aliases: list[str] = []
    for value in raw_aliases:
        value = value.strip()
        if not value:
            continue
        # Nyaa titles frequently use plain ASCII even when AniList stores
        # decorative Latin characters (é, ō, œ, ø, ...). Search both forms.
        folded = fold_search_title(value).strip()
        for variant in (folded, value):
            if variant and variant not in aliases:
                aliases.append(variant)
    expected_season = _expected_season(anime)

    # Search the canonical AniList title first. A longer English alias is not
    # necessarily the title used by release groups (for example Youjo Senki II
    # releases are not usually named Saga of Tanya the Evil Season 2).
    canonical_variants: list[str] = []
    canonical_folded = fold_search_title(anime.title).strip()
    for value in (canonical_folded, anime.title.strip()):
        if value and value in aliases and value not in canonical_variants:
            canonical_variants.append(value)
    remaining_aliases = [value for value in aliases if value not in canonical_variants]
    remaining_aliases.sort(
        key=lambda value: (_season_number(value) == expected_season, len(value)),
        reverse=True,
    )
    aliases = canonical_variants + remaining_aliases

    # Search every title alias in its plain form before adding season suffixes.
    # The previous nested ordering let the first long alias consume most of the
    # query budget and could prevent a release-group alias from being searched.
    selected_aliases = aliases[:max_queries]
    bases: list[str] = list(selected_aliases)
    if expected_season > 1:
        for alias in selected_aliases:
            if _season_number(alias) is not None:
                continue
            bases.extend(
                [f"{alias} S{expected_season:02d}", f"{alias} {_ordinal(expected_season)} Season"]
            )

    queries: list[str] = []
    for base in dict.fromkeys(value.strip() for value in bases if value.strip()):
        if batch:
            queries.extend([base, f"{base} batch", f"{base} complete"])
            if anime.episodes and anime.episodes > 1:
                queries.extend(
                    [
                        f"{base} 01-{anime.episodes:02d}",
                        f"{base} 1-{anime.episodes}",
                    ]
                )
        elif episode is not None:
            episode_numbers = [episode, *(value for value in alternative_episodes if value > 0)]
            for episode_number in dict.fromkeys(episode_numbers):
                number = int(episode_number)
                # Nyaa tokenizes S01E43/E43 differently from a standalone 43.
                # Include the common scene/streaming forms explicitly; this is
                # essential for absolute-numbered split-cour releases.
                queries.extend(
                    [
                        f"{base} {number:02d}",
                        f"{base} {number}",
                        f"{base} E{number:02d}",
                        f"{base} S01E{number:02d}",
                    ]
                )
                if number == int(episode) and expected_season > 1:
                    queries.append(f"{base} S{expected_season:02d}E{number:02d}")
    query_limit = max_queries * (12 if batch else 18)
    queries = list(dict.fromkeys(queries))[:query_limit]

    by_hash: dict[str, NyaaRelease] = {}
    search_errors: list[str] = []
    deadline = (
        time.monotonic() + max(0.0, query_budget_seconds)
        if query_budget_seconds is not None
        else None
    )
    budget_exhausted = False
    for query in queries:
        if deadline is not None and time.monotonic() >= deadline:
            budget_exhausted = True
            break
        try:
            found = client.search(query)
        except NyaaError as exc:
            # One Nyaa query may time out while a shorter alias succeeds. Do
            # not abort the whole search on the first 5xx/timeout. Automatic
            # background checks additionally have a wall-clock budget so a
            # broken Nyaa route cannot block startup for several minutes.
            search_errors.append(f"{query}: {exc}")
            continue
        for release in found:
            key = release.info_hash or release.torrent_url or release.link
            if key not in by_hash:
                by_hash[key] = release

    if not by_hash and search_errors:
        suffix = "; automatic search budget exhausted" if budget_exhausted else ""
        raise NyaaError("; ".join(dict.fromkeys(search_errors)) + suffix)

    eligible_releases = [
        release
        for release in by_hash.values()
        if not (
            episode is not None
            and not batch
            and not _release_is_eligible_for_episode(
                release.title, episode, anime, alternative_episodes
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
        )
        for release in eligible_releases
    ]
    ranked.sort(key=lambda item: (item.score, item.seeders, item.downloads), reverse=True)
    return ranked



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
        )
        for release in client.releases()
        if (release.is_batch if batch else not release.is_batch)
    ]
    ranked = [
        item
        for item in ranked
        if any(reason.startswith("exact-title") or reason.startswith("alias-title") or reason.startswith("fuzzy-title") for reason in item.reasons)
    ]
    ranked.sort(key=lambda item: (item.score, item.published), reverse=True)
    return ranked

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
) -> list[NyaaRelease]:
    releases = client.releases(preferred_resolution)
    eligible: list[NyaaRelease] = []
    for release in releases:
        if batch:
            if not (release.is_batch or release_episode_range(release.title) is not None):
                continue
        elif episode is not None and not _release_is_eligible_for_episode(
            release.title, episode, anime, alternative_episodes
        ):
            continue
        title_score, _title_reasons = _title_match_score(
            anime, release.title, alternative_titles
        )
        if title_score < 25.0:
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
        )
        for release in eligible
    ]
    ranked.sort(key=lambda item: (item.score, item.published), reverse=True)
    return ranked
