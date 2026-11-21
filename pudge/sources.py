"""Release sources the user adds in Settings → Sources (Pudge ships with none).

They live in ``sources.json`` next to config.toml::

    {"schema": 1,
     "release_search": {"url": "", "mirrors": []},
     "weekly_rss": "",
     "release_feed": "",
     "recommendations": ""}

* ``release_search.url``: a torrent site with an RSS search; ``mirrors`` are
  extra base URLs of the same search, tried when it fails.
* ``weekly_rss``: base of a weekly releases RSS (``/?r=<resolution>`` is added).
* ``release_feed``: a public release feed, read as given.
* ``recommendations``: base URL of a release recommendations API.

An empty value turns that part off. Unknown keys are ignored.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .i18n import err

_LOGGER = logging.getLogger(__name__)

SCHEMA = 1
FILE_NAME = "sources.json"
MAX_URL_LENGTH = 512
MAX_MIRRORS = 10
_FIELDS = ("release_search", "mirrors", "weekly_rss", "release_feed", "recommendations")
_CACHE: dict[str, tuple[tuple[int, int], "Sources"]] = {}
_CACHE_LOCK = threading.Lock()


class SourcesError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Sources:
    release_search: str = ""
    mirrors: tuple[str, ...] = ()
    weekly_rss: str = ""
    release_feed: str = ""
    recommendations: str = ""

    @property
    def empty(self) -> bool:
        return not any(getattr(self, name) for name in _FIELDS)

    @property
    def release_host(self) -> str:
        return (urlparse(self.release_search).hostname or "") if self.release_search else ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "release_search": {"url": self.release_search, "mirrors": list(self.mirrors)},
            "weekly_rss": self.weekly_rss,
            "release_feed": self.release_feed,
            "recommendations": self.recommendations,
        }


def sources_path(config: Any) -> Path:
    from .config import DEFAULT_CONFIG_PATH

    path = getattr(config, "config_path", None) or DEFAULT_CONFIG_PATH
    return Path(path).expanduser().with_name(FILE_NAME)


def _clean_url(value: Any, field: str, *, keep_trailing_slash: bool = False) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourcesError(err("sources.invalidUrl", field=field))
    text = value.strip()
    if not text:
        return ""
    if len(text) > MAX_URL_LENGTH:
        raise SourcesError(err("sources.tooLong", field=field, max=MAX_URL_LENGTH))
    try:
        parsed = urlparse(text)
    except ValueError as exc:
        # "http://[::1" or a host with characters NFKC changes: urlparse raises a plain
        # ValueError, which load_sources and a backup restore (both catch SourcesError) let
        # through, so one bad address broke every search instead of turning the sources off.
        raise SourcesError(err("sources.invalidUrl", field=field)) from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or any(char.isspace() for char in text)
    ):
        raise SourcesError(err("sources.invalidUrl", field=field))
    # A feed URL is requested exactly as given; base URLs get paths appended.
    return text if keep_trailing_slash else text.rstrip("/")


def parse_sources(value: str | bytes | dict[str, Any]) -> Sources:
    """Validated sources from sources.json text or an already parsed object."""
    data: Any = value
    if isinstance(value, (str, bytes)):
        try:
            data = json.loads(value)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourcesError(err("sources.invalidJson", error=exc)) from exc
    if not isinstance(data, dict):
        raise SourcesError(err("sources.invalidFormat"))
    schema = data.get("schema", SCHEMA)
    if schema != SCHEMA:
        raise SourcesError(err("sources.unsupportedSchema", schema=schema))
    search = data.get("release_search") or {}
    if isinstance(search, str):
        search = {"url": search}
    if not isinstance(search, dict):
        raise SourcesError(err("sources.invalidFormat"))
    url = _clean_url(search.get("url"), "release_search.url")
    raw_mirrors = search.get("mirrors") or []
    if isinstance(raw_mirrors, str):
        raw_mirrors = raw_mirrors.splitlines()
    if not isinstance(raw_mirrors, (list, tuple)):
        raise SourcesError(err("sources.invalidFormat"))
    mirrors: list[str] = []
    for item in raw_mirrors:
        mirror = _clean_url(item, "release_search.mirrors")
        if mirror and mirror != url and mirror not in mirrors:
            mirrors.append(mirror)
    if len(mirrors) > MAX_MIRRORS:
        raise SourcesError(err("sources.tooManyMirrors", max=MAX_MIRRORS))
    return Sources(
        release_search=url,
        mirrors=tuple(mirrors),
        weekly_rss=_clean_url(data.get("weekly_rss"), "weekly_rss"),
        release_feed=_clean_url(data.get("release_feed"), "release_feed", keep_trailing_slash=True),
        recommendations=_clean_url(data.get("recommendations"), "recommendations"),
    )


def save_sources(config: Any, sources: Sources) -> Path:
    path = sources_path(config)
    text = json.dumps(sources.to_dict(), ensure_ascii=False, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{FILE_NAME}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except OSError as exc:
        raise SourcesError(err("sources.writeFailed", error=exc)) from exc
    with _CACHE_LOCK:
        _CACHE.pop(str(path), None)
    return path


def _migrate(config: Any) -> Sources:
    # An existing config.toml names its release search explicitly (older
    # versions wrote nyaa.base_url); carry it over once.
    legacy = str(getattr(getattr(config, "nyaa", None), "base_url", "") or "").strip()
    if not legacy:
        return Sources()
    try:
        sources = parse_sources({"release_search": {"url": legacy}})
        path = save_sources(config, sources)
    except SourcesError as exc:
        _LOGGER.warning("FAIL step=sources.migrate error=%r", str(exc))
        return Sources()
    _LOGGER.info("MIGRATE step=sources.release_search path=%s", path)
    return sources


def load_sources(config: Any) -> Sources:
    path = sources_path(config)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return _migrate(config)
    except OSError:
        return Sources()
    stamp = (stat.st_mtime_ns, stat.st_size)
    key = str(path)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        sources = parse_sources(path.read_bytes())
    except (OSError, SourcesError) as exc:
        # A broken file must not break searches: everything is off until fixed.
        _LOGGER.warning("FAIL step=sources.load path=%s error=%r", path, str(exc))
        sources = Sources()
    with _CACHE_LOCK:
        _CACHE[key] = (stamp, sources)
    return sources


def import_sources(config: Any, text: str | bytes | dict[str, Any], *, only_missing: bool) -> Sources:
    """Save sources from a file; ``only_missing`` keeps the fields already filled in."""
    incoming = parse_sources(text)
    if only_missing:
        current = load_sources(config)
        incoming = Sources(**{name: getattr(current, name) or getattr(incoming, name) for name in _FIELDS})
    save_sources(config, incoming)
    return incoming
