from __future__ import annotations

import shlex
from pathlib import Path

_BRAND_PATH = Path(__file__).with_name("brand.env")


def _load_brand() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = _BRAND_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError:
            parsed = []
        values[key.strip()] = parsed[0] if parsed else value.strip().strip('"\'')
    return values


_BRAND = _load_brand()
APP_NAME = _BRAND.get("APP_NAME", "Anime MPV")
APP_SLUG = _BRAND.get("APP_SLUG", "anime-mpv")
APP_BUNDLE_ID = _BRAND.get("APP_BUNDLE_ID", "com.anime-mpv.app")
APP_CLI = _BRAND.get("APP_CLI", "anime-mpv")
APP_AGENT_CLI = _BRAND.get("APP_AGENT_CLI", "anime-mpv-agent")
APP_ENV_PREFIX = _BRAND.get("APP_ENV_PREFIX", "ANIME_MPV")


def _legacy_values(key: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in _BRAND.get(key, "").split("|") if value.strip())


LEGACY_APP_NAMES = _legacy_values("APP_LEGACY_NAMES")
LEGACY_APP_SLUGS = _legacy_values("APP_LEGACY_SLUGS")
LEGACY_BUNDLE_IDS = _legacy_values("APP_LEGACY_BUNDLE_IDS")
LEGACY_APP_CLIS = _legacy_values("APP_LEGACY_CLIS")
LEGACY_AGENT_CLIS = _legacy_values("APP_LEGACY_AGENT_CLIS")
APP_BUNDLE_NAME = f"{APP_NAME}.app"
APP_EXECUTABLE_NAME = APP_NAME

CONFIG_DIR = Path.home() / ".config" / APP_SLUG
CACHE_DIR = Path.home() / "Library" / "Caches" / APP_SLUG
DATA_DIR = Path.home() / ".local" / "share" / APP_SLUG
LOG_DIR = Path.home() / "Library" / "Logs"
DEFAULT_LIBRARY_DIR = Path.home() / "Movies" / APP_NAME
DEFAULT_DATABASE_PATH = DATA_DIR / "library.sqlite3"
DEFAULT_ENERGY_LOG_PATH = LOG_DIR / f"{APP_SLUG}-energy.jsonl"
DEFAULT_RUNTIME_LOG_PATH = LOG_DIR / f"{APP_SLUG}-runtime.log"
QBITTORRENT_CATEGORY = APP_SLUG
BACKUP_APP_ID = APP_SLUG
