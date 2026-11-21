from __future__ import annotations

import hashlib
import os
import json
import fcntl
import time
import re
from datetime import datetime, timezone
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urljoin

import httpx

from ..archive_limits import (
    IMAGE_MEMBER_LIMIT,
    SUBTITLE_ARCHIVE_LIMIT,
    TEXT_MEMBER_LIMIT,
    ArchiveBudget,
    ArchiveTooLargeError,
    extract_member,
    run_extraction,
)
from ..filename import normalize_title, parse_anime_filename, release_tokens, title_similarity
from ..fsutil import atomic_write_json
from ..language import (
    IMAGE_SUBTITLE_EXTENSIONS,
    TEXT_SUBTITLE_EXTENSIONS,
    is_japanese_subtitle,
    is_macos_metadata_path,
)
from ..logging_utils import configure_logging, timed_step
from ..branding import APP_SLUG
from ..models import JimakuEntry, JimakuFile, SubtitleCandidate, VideoIdentity
from ..i18n import err, t
from ..jimaku_trial import (
    clear_jimaku_key_rejection,
    jimaku_key_fingerprint,
    jimaku_rejects_key,
    record_jimaku_key_rejection,
)
from ..subtitle_formats import (
    format_preference_bonus,
    subtitle_filename_language_profile,
    subtitle_language_profiles,
)


SUBTITLE_EXTENSIONS = TEXT_SUBTITLE_EXTENSIONS | IMAGE_SUBTITLE_EXTENSIONS
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}

# jimaku.cc documents a 25 requests/minute limit per API key. Keep a
# deliberate safety margin while still allowing a small interactive burst.
# Four initial tokens + 20/min sustained traffic stays below 25 requests in
# the first minute and then settles at one network request every ~3 seconds.
JIMAKU_LOCAL_REQUESTS_PER_MINUTE = 20.0
JIMAKU_LOCAL_BURST_CAPACITY = 4.0
ARCHIVE_EXTRACT_CACHE_SCHEMA = 1
# A download larger than this stops (an archive is never unpacked past this size either).
JIMAKU_DOWNLOAD_LIMIT = SUBTITLE_ARCHIVE_LIMIT


def find_7zip() -> str | None:
    """Resolve 7-Zip even inside a Finder-launched macOS app (only a fallback for bsdtar now).

    GUI applications do not inherit the interactive shell PATH, so Homebrew's
    /opt/homebrew/bin is commonly absent despite sevenzip being installed.
    """
    candidates = [
        os.environ.get("PUDGE_7ZIP", "").strip(),
        shutil.which("7zz") or "",
        shutil.which("7z") or "",
        "/opt/homebrew/bin/7zz",
        "/opt/homebrew/bin/7z",
        "/usr/local/bin/7zz",
        "/usr/local/bin/7z",
        "/opt/homebrew/opt/sevenzip/bin/7zz",
        "/usr/local/opt/sevenzip/bin/7zz",
    ]
    for value in candidates:
        if not value:
            continue
        path = Path(value).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def find_bsdtar() -> str | None:
    """macOS's own bsdtar (libarchive): it reads zip, 7z, rar4 and rar5.

    install.sh no longer installs the Homebrew 7-Zip just for .7z/.rar archives.
    PUDGE_BSDTAR names another bsdtar (tests).
    """
    for value in (os.environ.get("PUDGE_BSDTAR", "").strip(), "/usr/bin/bsdtar"):
        if not value:
            continue
        path = Path(value).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


class ArchiveToolMissingError(RuntimeError):
    """Neither bsdtar nor 7-Zip is there to unpack a .7z/.rar archive."""


class ArchiveUnreadableError(RuntimeError):
    """No archive tool could unpack the archive; ``detail`` is the end of their output."""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


_ENCRYPTED_ARCHIVE_RE = re.compile(r"(?i)encrypt|passphrase|password")


def _empty_folder(root: Path) -> None:
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


def _bsdtar_pattern(name: str) -> str:
    # bsdtar reads member arguments as patterns: "[Group] Title 01.epub" must match itself.
    return re.sub(r"([*?\[\]\\])", r"\\\1", name)


def list_archive(path: Path, *, timeout: float = 120.0) -> list[str]:
    """Member names of a .7z/.rar archive (files and folders): bsdtar first, then 7-Zip.

    Raises ArchiveToolMissingError, ArchiveUnreadableError (encrypted names or an
    unreadable archive) or subprocess.TimeoutExpired.
    """
    env = {**os.environ, "LC_ALL": "en_US.UTF-8"}
    outputs: list[str] = []
    tried = False
    bsdtar = find_bsdtar()
    if bsdtar:
        tried = True
        completed = subprocess.run(
            [bsdtar, "-tf", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        if completed.returncode == 0:
            text = completed.stdout.decode("utf-8", errors="replace")
            return [line for line in text.splitlines() if line.strip()]
        outputs.append(completed.stderr.decode("utf-8", errors="replace").strip())
    sevenzip = find_7zip()
    if sevenzip:
        tried = True
        completed = subprocess.run(
            [sevenzip, "l", "-ba", "-slt", "--", str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
        if completed.returncode == 0:
            text = completed.stdout.decode("utf-8", errors="replace")
            return [line[7:] for line in text.splitlines() if line.startswith("Path = ") and line[7:].strip()]
        outputs.append((completed.stdout + completed.stderr).decode("utf-8", errors="replace").strip())
    if not tried:
        raise ArchiveToolMissingError(path.name)
    detail = "\n".join(outputs)
    key = "archive.encrypted" if _ENCRYPTED_ARCHIVE_RE.search(detail) else "archive.unreadable"
    raise ArchiveUnreadableError(err(key), detail[-1000:])


def unpack_archive(
    path: Path,
    root: Path,
    *,
    limit: int | None,
    timeout: float,
    members: list[str] | None = None,
) -> None:
    """Unpack a .7z/.rar archive into the empty folder ``root``: bsdtar first, then 7-Zip.

    7zz is tried only when bsdtar cannot read the archive and 7zz is installed.
    Without -P, bsdtar refuses absolute paths, ".." and writing through a link,
    like 7-Zip; the size limit and timeout apply to both. ``members`` (names from
    ``list_archive``) unpacks only those. Raises ArchiveToolMissingError,
    ArchiveUnreadableError (encrypted or unreadable), ArchiveTooLargeError or
    subprocess.TimeoutExpired.
    """
    commands: list[list[str]] = []
    bsdtar = find_bsdtar()
    if bsdtar:
        selected = [_bsdtar_pattern(name) for name in members] if members is not None else []
        commands.append([bsdtar, "-xf", str(path), "-C", str(root), *selected])
    sevenzip = find_7zip()
    if sevenzip:
        if members is None:
            commands.append([sevenzip, "x", "-y", f"-o{root}", str(path)])
        else:
            # -spd: member names are names, not wildcards.
            commands.append([sevenzip, "x", "-y", "-spd", f"-o{root}", "--", str(path), *members])
    if not commands:
        raise ArchiveToolMissingError(path.name)
    # A Finder-launched app has no LANG: in the C locale libarchive cannot write the
    # Japanese member names of a .7z/.rar.
    env = {**os.environ, "LC_ALL": "en_US.UTF-8"}
    outputs: list[str] = []
    for command in commands:
        _empty_folder(root)
        returncode, output = run_extraction(command, root, limit=limit, timeout=timeout, env=env)
        if returncode == 0:
            return
        outputs.append(output.strip())
    _empty_folder(root)
    detail = "\n".join(outputs)
    key = "archive.encrypted" if _ENCRYPTED_ARCHIVE_RE.search(detail) else "archive.unreadable"
    raise ArchiveUnreadableError(err(key), detail[-1000:])


_SEASON_TOKEN_RE = re.compile(r"(?i)(?:^|[\s._-])s(?:eason\s*)?0*(?P<season>\d{1,2})(?=$|[\s._-])")
_ORDINAL_SEASON_RE = re.compile(r"(?i)\b(?P<season>\d{1,2})(?:st|nd|rd|th)\s+season\b")
_ROMAN_SEASONS = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9}


def explicit_season_hint(name: str, identity_title: str = "") -> int | None:
    """Return a season explicitly encoded by a subtitle release name.

    This is deliberately conservative: unknown/untagged files stay eligible.
    Besides S02/Season 2/2nd Season, recognize sequel suffixes such as
    ``Ansatsu Kyoushitsu II`` only when the base title exactly matches the
    requested identity.
    """
    parsed = parse_anime_filename(name)
    # "2nd Season 05": the ordinal is the season and "Season 05" right after it is
    # the episode. Hints that still disagree make the season unknown, not a reject.
    hints: set[int] = set()
    masked = name
    for match in _ORDINAL_SEASON_RE.finditer(name):
        hints.add(int(match.group("season")))
        masked = masked[: match.start()] + " " * (match.end() - match.start()) + masked[match.end() :]
    for match in _SEASON_TOKEN_RE.finditer(masked):
        hints.add(int(match.group("season")))
    if parsed.season is not None and not hints:
        hints.add(int(parsed.season))
    if len(hints) > 1:
        return None
    if hints:
        return hints.pop()
    base = normalize_title(identity_title)
    parsed_title = normalize_title(parsed.title)
    if not base or not parsed_title.startswith(base + " "):
        return None
    suffix = parsed_title[len(base) :].strip()
    if suffix in _ROMAN_SEASONS:
        return _ROMAN_SEASONS[suffix]
    if suffix.isdigit():
        value = int(suffix)
        if 2 <= value <= 9:
            return value
    return None


_EPISODE_RANGE_PATTERNS = (
    re.compile(r"(?i)\bS\d{1,2}E(?P<start>\d{1,3})\s*[-–—~]\s*(?:E)?(?P<end>\d{1,3})\b"),
    re.compile(r"(?i)\bEP?(?P<start>\d{1,3})\s*[-–—~]\s*(?:EP?)?(?P<end>\d{1,3})\b"),
    re.compile(
        # Do not start a generic episode range inside season/volume markers
        # such as "S3 - 13" or "V2 - 05".
        r"(?<![A-Za-z0-9])(?P<start>\d{1,3})\s*[-–—~]\s*(?P<end>\d{1,3})"
        r"(?=$|[\s._\[\](){}])"
    ),
)


_DATE_RE = re.compile(r"(?<!\d)(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[-./]\d{1,2}[-./]\d{4})(?!\d)")
_RANGE_BRACKET_RE = re.compile(r"\[[^\]]*\]")


def _range_starts_in_title(name: str, match: re.Match[str], title: str) -> bool:
    """``Kaiju No. 8 - 02``: the 8 ends the title, so "8 - 02" is no episode range."""
    wanted = normalize_title(title)
    if not wanted:
        return False
    prefix = _RANGE_BRACKET_RE.sub(" ", name[: match.start("start")])
    return normalize_title(prefix + " " + match.group("start")) == wanted


def explicit_episode_range(name: str) -> tuple[int, int] | None:
    """Return an explicit episode span from a release filename.

    This intentionally ignores dimensions such as ``1440x1080`` and only
    accepts realistic episode numbers. It is used as a hard sanity check so a
    pack labelled ``01-02`` cannot be considered for episode 6 merely because
    the generic filename parser returned no single episode.

    A span is never read backwards (``Mob Psycho 100 - 03``), from a date
    (``[2023-10-06]``) or from a number that ends the title (``Steins;Gate 0 - 05``).
    """
    masked = _DATE_RE.sub(lambda match: " " * len(match.group(0)), name)
    title = ""
    for index, pattern in enumerate(_EPISODE_RANGE_PATTERNS):
        position = 0
        while (match := pattern.search(masked, position)) is not None:
            position = match.start() + 1
            start = int(match.group("start"))
            end = int(match.group("end"))
            if not (0 <= start <= 500 and 0 <= end <= 500) or start > end:
                continue
            if index == len(_EPISODE_RANGE_PATTERNS) - 1:
                title = title or parse_anime_filename(name).title
                if _range_starts_in_title(masked, match, title):
                    continue
            return start, end
    return None


class JimakuError(RuntimeError):
    pass


class JimakuRateLimited(JimakuError):
    """Jimaku answered 429, or its cooldown from an earlier 429 is still running."""


class JimakuKeyRejected(JimakuError):
    """Jimaku refused the API key (invalid, revoked or an ended shared key)."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


def _parse_timestamp(value: str) -> int | None:
    raw = value.strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return None


def _safe_name(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]", "_", value)
    return value[:220] or "subtitle"


def jimaku_download_dir(cache_dir: Path, item: JimakuFile) -> Path:
    """The cache folder of one Jimaku file.

    The URL comes from the file name: a file replaced under the same name is a new
    upload only by its date and size, so both are part of the key.
    """
    key = "\0".join(
        (item.url, str(getattr(item, "last_modified", "") or ""), str(getattr(item, "size", 0) or 0))
    )
    return cache_dir / "jimaku" / hashlib.sha1(key.encode()).hexdigest()[:16]


def _api_rows(payload: object, path: str) -> list[dict]:
    """The rows of a list answer; another answer is a Jimaku error, not a crash.

    An error object, a string or a row without its fields raised TypeError/KeyError,
    which the subtitle search does not catch (only JimakuError): the whole job failed.
    """
    if not isinstance(payload, list):
        raise JimakuError(err("jimaku.api", error=f"{path}: unexpected answer {str(payload)[:120]!r}"))
    return [item for item in payload if isinstance(item, dict)]


class JimakuClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        cache_dir: Path | None = None,
        cache_ttl_seconds: float = 2 * 60,
        *,
        search_cache_ttl_seconds: float | None = None,
        empty_search_cache_ttl_seconds: float = 0.0,
    ) -> None:
        if not api_key:
            raise JimakuError(err("jimaku.apiKeyMissing"))
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        # Entry searches change rarely (a new title, a new AniList link); file lists
        # change with every upload and keep the short TTL. An empty search is kept
        # only briefly, so a newly created entry is still found soon.
        self.search_cache_ttl_seconds = (
            self.cache_ttl_seconds
            if search_cache_ttl_seconds is None
            else max(0.0, float(search_cache_ttl_seconds))
        )
        self.empty_search_cache_ttl_seconds = max(0.0, float(empty_search_cache_ttl_seconds))
        self._api_key_hash = jimaku_key_fingerprint(api_key)
        self.logger = configure_logging()
        self.client = httpx.Client(
            timeout=httpx.Timeout(15.0, connect=6.0),
            follow_redirects=True,
            headers={
                "Authorization": api_key,
                "Accept": "application/json",
                "User-Agent": APP_SLUG,
            },
        )

    def close(self) -> None:
        self.client.close()

    def _request_budget_file(self) -> Path | None:
        cache_dir = getattr(self, "cache_dir", None)  # bare instances (tests) have none
        if cache_dir is None:
            return None
        return cache_dir / "jimaku-api" / "request-budget.json"

    def _reserve_request_slot(self, *, now: float | None = None) -> float:
        """Reserve one shared Jimaku request slot and return required wait seconds.

        All prepare-only subprocesses share this small on-disk token bucket. We
        reserve future tokens while holding a filesystem lock so simultaneous
        processes queue behind each other instead of all waking for the same slot.
        """
        marker = self._request_budget_file()
        if marker is None:
            return 0.0

        moment = time.time() if now is None else float(now)
        refill_per_second = JIMAKU_LOCAL_REQUESTS_PER_MINUTE / 60.0
        capacity = JIMAKU_LOCAL_BURST_CAPACITY
        if refill_per_second <= 0.0 or capacity <= 0.0:
            return 0.0

        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            lock_path = marker.with_name("request-budget.lock")
            with lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    payload: dict[str, object] = {}
                    if marker.is_file():
                        try:
                            loaded = json.loads(marker.read_text(encoding="utf-8"))
                            if isinstance(loaded, dict):
                                payload = loaded
                        except (OSError, ValueError, TypeError, json.JSONDecodeError):
                            payload = {}

                    same_key = str(payload.get("key_hash") or "") == self._api_key_hash
                    try:
                        tokens = float(payload.get("tokens")) if same_key else capacity
                        updated_at = float(payload.get("updated_at")) if same_key else moment
                    except (TypeError, ValueError):
                        tokens = capacity
                        updated_at = moment

                    elapsed = max(0.0, moment - updated_at)
                    tokens = min(capacity, tokens + elapsed * refill_per_second)

                    if tokens >= 1.0:
                        wait_seconds = 0.0
                    else:
                        wait_seconds = (1.0 - tokens) / refill_per_second

                    # Reserving even a future token is intentional. Negative token
                    # balances serialize several concurrent processes into
                    # distinct future slots: 3s, 6s, 9s, ... instead of a thundering herd.
                    tokens -= 1.0
                    temporary = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
                    temporary.write_text(
                        json.dumps(
                            {
                                "key_hash": self._api_key_hash,
                                "tokens": tokens,
                                "updated_at": moment,
                                "requests_per_minute": JIMAKU_LOCAL_REQUESTS_PER_MINUTE,
                                "burst_capacity": JIMAKU_LOCAL_BURST_CAPACITY,
                            }
                        ),
                        encoding="utf-8",
                    )
                    temporary.replace(marker)
                    return max(0.0, wait_seconds)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            # Pacing is protective rather than correctness-critical. A damaged or
            # read-only cache directory must not make Jimaku unusable.
            self.logger.warning(
                "FALLBACK step=jimaku.local_rate_limit reason=lock_error error=%r",
                str(exc),
            )
            return 0.0

    def _acquire_request_slot(self, path: str) -> None:
        wait_seconds = self._reserve_request_slot()
        if wait_seconds <= 0.0:
            return
        self.logger.info(
            "WAIT step=jimaku.local_rate_limit path=%s wait_s=%.2f rate_per_minute=%.1f burst=%.1f",
            path,
            wait_seconds,
            JIMAKU_LOCAL_REQUESTS_PER_MINUTE,
            JIMAKU_LOCAL_BURST_CAPACITY,
        )
        time.sleep(wait_seconds)

    def _rate_limit_file(self) -> Path | None:
        cache_dir = getattr(self, "cache_dir", None)
        if cache_dir is None:
            return None
        return cache_dir / "jimaku-api" / "rate-limit.json"

    def _rate_limit_remaining(self) -> float:
        marker = self._rate_limit_file()
        if marker is None:
            return 0.0
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            until = float(payload.get("until") or 0.0) if isinstance(payload, dict) else 0.0
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0.0
        return max(0.0, until - time.time())

    @staticmethod
    def _retry_after_seconds(value: str) -> float:
        try:
            seconds = float(str(value or "").strip())
        except ValueError:
            seconds = 600.0
        return max(30.0, min(3600.0, seconds))

    @staticmethod
    def _rate_limit_message(seconds: float) -> str:
        minutes = max(1, int((max(0.0, seconds) + 59) // 60))
        return err("jimaku.rateLimited", minutes=minutes)

    def _set_rate_limit(self, seconds: float) -> float:
        seconds = max(30.0, float(seconds))
        marker = self._rate_limit_file()
        if marker is not None:
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                temporary = marker.with_name(f"{marker.name}.{os.getpid()}.tmp")
                temporary.write_text(
                    json.dumps({"until": time.time() + seconds, "status": 429}),
                    encoding="utf-8",
                )
                temporary.replace(marker)
            except OSError:
                pass
        return seconds

    def _key_rejected(self, path: str, status_code: int) -> None:
        self.logger.warning(
            "FAIL step=jimaku.http path=%s reason=key_rejected status=%s key=%s",
            path,
            status_code,
            self._api_key_hash,
        )
        if self.cache_dir is not None:
            record_jimaku_key_rejection(self.cache_dir, self._api_key_hash, status_code)
        raise JimakuKeyRejected(err("jimaku.keyRejected", status=status_code), status_code)

    def _cache_ttl(self, path: str, payload: object) -> float:
        if path == "/api/entries/search":
            return self.empty_search_cache_ttl_seconds if payload == [] else self.search_cache_ttl_seconds
        if payload == [] and path.endswith("/files"):
            return 0.0
        return self.cache_ttl_seconds

    def _get_json(self, path: str, params: dict[str, object] | None = None):
        safe_params = dict(params or {})
        cache_path: Path | None = None
        stale_payload = None
        if self.cache_dir is not None and self.cache_ttl_seconds > 0:
            raw = json.dumps(
                {"base_url": self.base_url, "path": path, "params": safe_params},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
            cache_path = self.cache_dir / "jimaku-api" / f"{digest}.json"
            try:
                if cache_path.is_file():
                    cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
                    empty_dynamic_result = cached_payload == [] and (
                        path == "/api/entries/search" or path.endswith("/files")
                    )
                    if not empty_dynamic_result:
                        stale_payload = cached_payload
                    ttl = self._cache_ttl(path, cached_payload)
                    if time.time() - cache_path.stat().st_mtime <= ttl:
                        count = len(cached_payload) if isinstance(cached_payload, list) else 1
                        self.logger.info("RESULT step=jimaku.http path=%s count=%s cache=hit", path, count)
                        return cached_payload
                    if empty_dynamic_result:
                        self.logger.info("SKIP step=jimaku.http path=%s count=0 cache=empty_dynamic", path)
            except (OSError, ValueError, json.JSONDecodeError):
                stale_payload = None

        rate_limit_remaining = self._rate_limit_remaining()
        if rate_limit_remaining > 0:
            if stale_payload is not None:
                count = len(stale_payload) if isinstance(stale_payload, list) else 1
                self.logger.warning(
                    "FALLBACK step=jimaku.http path=%s cache=stale count=%s reason=rate_limited remaining_s=%.0f",
                    path,
                    count,
                    rate_limit_remaining,
                )
                return stale_payload
            raise JimakuRateLimited(self._rate_limit_message(rate_limit_remaining))

        last_network_error: httpx.HTTPError | None = None
        max_attempts = 2
        for attempt in range(max_attempts):
            try:
                self._acquire_request_slot(path)
                with timed_step(
                    self.logger,
                    "jimaku.http",
                    path=path,
                    params=safe_params,
                    attempt=attempt + 1,
                ):
                    response = self.client.get(urljoin(self.base_url, path), params=params)
                    status_code = int(getattr(response, "status_code", 200) or 200)
                    if status_code == 429:
                        cooldown = self._set_rate_limit(
                            self._retry_after_seconds(response.headers.get("Retry-After", ""))
                        )
                        if stale_payload is not None:
                            count = len(stale_payload) if isinstance(stale_payload, list) else 1
                            self.logger.warning(
                                "FALLBACK step=jimaku.http path=%s cache=stale count=%s reason=429 cooldown_s=%.0f",
                                path,
                                count,
                                cooldown,
                            )
                            return stale_payload
                        raise JimakuRateLimited(self._rate_limit_message(cooldown))
                    if status_code in {401, 403} and jimaku_rejects_key(
                        status_code, response.headers.get("content-type", "")
                    ):
                        self._key_rejected(path, status_code)
                    response.raise_for_status()
                    payload = response.json()
                count = len(payload) if isinstance(payload, list) else 1
                self.logger.info("RESULT step=jimaku.http path=%s count=%s cache=miss", path, count)
                if self.cache_dir is not None:
                    clear_jimaku_key_rejection(self.cache_dir, self._api_key_hash)
                # Both endpoints answer a list: anything else (an error object sent with
                # HTTP 200) is not kept for the hours an entry search stays cached.
                if (
                    cache_path is not None
                    and isinstance(payload, list)
                    and self._cache_ttl(path, payload) > 0
                ):
                    try:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write_json(cache_path, payload, ensure_ascii=False)
                    except OSError:
                        pass
                return payload
            except httpx.TransportError as exc:
                last_network_error = exc
                if stale_payload is not None:
                    count = len(stale_payload) if isinstance(stale_payload, list) else 1
                    self.logger.warning(
                        "FALLBACK step=jimaku.http path=%s cache=stale count=%s error=%s",
                        path,
                        count,
                        exc,
                    )
                    return stale_payload
                if attempt < max_attempts - 1:
                    self.logger.warning(
                        "RETRY step=jimaku.http path=%s attempt=%s error=%s",
                        path,
                        attempt + 1,
                        exc,
                    )
                    time.sleep(0.25 * (2**attempt))
                    continue
                break
            except httpx.HTTPError as exc:
                # HTTP status errors are deterministic enough that retrying them
                # immediately only creates extra load. DNS/connect failures are
                # handled by the retry branch above.
                raise JimakuError(err("jimaku.api", error=exc)) from exc
            except ValueError as exc:
                raise JimakuError(err("jimaku.api", error=exc)) from exc

        if stale_payload is not None and last_network_error is not None:
            count = len(stale_payload) if isinstance(stale_payload, list) else 1
            self.logger.warning(
                "FALLBACK step=jimaku.http path=%s cache=stale count=%s error=%s",
                path,
                count,
                last_network_error,
            )
            return stale_payload
        if last_network_error is not None:
            raise JimakuError(err("jimaku.api", error=last_network_error)) from last_network_error
        raise JimakuError(err("jimaku.unknownNetwork"))

    def search_entries(self, *, anilist_id: int | None = None, query: str | None = None) -> list[JimakuEntry]:
        params: dict[str, object] = {"anime": "true"}
        if anilist_id is not None:
            params["anilist_id"] = anilist_id
        if query:
            params["query"] = query
        payload = self._get_json("/api/entries/search", params=params)
        result: list[JimakuEntry] = []
        for item in _api_rows(payload, "/api/entries/search"):
            try:
                entry = JimakuEntry(
                    id=int(item["id"]),
                    name=str(item.get("name") or ""),
                    english_name=item.get("english_name"),
                    japanese_name=item.get("japanese_name"),
                    anilist_id=int(item["anilist_id"]) if item.get("anilist_id") else None,
                    flags=dict(item.get("flags") or {}),
                )
            except (KeyError, TypeError, ValueError):
                continue  # one malformed entry, not the whole subtitle search
            result.append(entry)
        return result

    def files(self, entry_id: int, episode: int | None) -> list[JimakuFile]:
        params = {"episode": episode} if episode is not None else None
        payload = self._get_json(f"/api/entries/{entry_id}/files", params=params)
        result: list[JimakuFile] = []
        for item in _api_rows(payload, "/files"):
            try:
                result.append(
                    JimakuFile(
                        url=str(item["url"]),
                        name=str(item["name"]),
                        size=int(item.get("size") or 0),  # null when Jimaku does not know it
                        last_modified=str(item.get("last_modified", "")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue  # a file without an address or name cannot be downloaded
        return result

    def files_for_episode(
        self,
        entry_id: int,
        episode: int | None,
        alternative_episodes: tuple[int, ...] = (),
    ) -> list[JimakuFile]:
        # Called unbound with lightweight test doubles too: resolve helpers on the class.
        files = JimakuClient._files_for_episode_unchecked(self, entry_id, episode, alternative_episodes)
        if episode is None or not files:
            return files
        collision = JimakuClient._absolute_numbering_collision(
            self, entry_id, int(episode), alternative_episodes
        )
        if collision is None:
            return files
        kept: list[JimakuFile] = []
        for item in files:
            if parse_anime_filename(item.name).episode == int(episode):
                getattr(self, "logger", configure_logging()).info(
                    "REJECT step=jimaku.files entry_id=%s episode=%s reason=absolute_numbering_collision "
                    "offset=%s means_episode=%s name=%r",
                    entry_id,
                    episode,
                    collision,
                    int(episode) - collision,
                    item.name,
                )
                continue
            kept.append(item)
        return kept

    def _absolute_numbering_collision(
        self,
        entry_id: int,
        episode: int,
        alternative_episodes: tuple[int, ...],
    ) -> int | None:
        """Return the season offset when this entry numbers episodes absolutely.

        A sequel entry such as "2nd Season" may label its files 13, 14, ...
        (continuing the prequel). Then the file "13" is the sequel's first
        episode, not the requested season-local episode 13. The entry is
        absolute only when it has no file numbered within the prequel range
        and several distinct numbers above it; a lone "13" stays ambiguous.
        """
        absolute = [int(value) for value in alternative_episodes if int(value) > episode]
        if not absolute:
            return None
        offset = min(absolute) - episode
        if offset <= 0 or episode <= offset:
            return None
        numbers = {
            number
            for item in self.files(entry_id, None)
            if (number := parse_anime_filename(item.name).episode) is not None
        }
        if not numbers or min(numbers) <= offset:
            return None
        if len({number for number in numbers if number > offset}) < 2:
            return None
        return offset

    def _files_for_episode_unchecked(
        self,
        entry_id: int,
        episode: int | None,
        alternative_episodes: tuple[int, ...] = (),
    ) -> list[JimakuFile]:
        """Return episode files: the entry's full list first, the server filter only when sparse.

        One full-list request (cached for 2 minutes) answers most episodes: every
        file whose name carries the episode, or a range covering it. Jimaku's
        server-side episode index can know a file whose name hides the episode,
        so with fewer than three such files the per-episode filter is asked as
        well and both are merged (server results first, as before). Files whose
        name has no episode join only when neither list found anything.
        """
        aliases = {int(value) for value in alternative_episodes if int(value) > 0}
        if episode is not None:
            aliases.add(int(episode))
        unfiltered = self.files(entry_id, None)
        if episode is None:
            getattr(self, "logger", configure_logging()).info(
                "RESULT step=jimaku.files entry_id=%s episode=none filtered=%s fallback=false",
                entry_id,
                len(unfiltered),
            )
            return unfiltered

        local: dict[str, JimakuFile] = {}
        unknown: dict[str, JimakuFile] = {}
        exact = 0
        for item in unfiltered:
            parsed_episode = parse_anime_filename(item.name).episode
            episode_range = explicit_episode_range(item.name)
            if parsed_episode in aliases:
                local.setdefault(item.url, item)
                exact += 1
            elif episode_range is not None:
                start, end = episode_range
                if any(start <= alias <= end for alias in aliases):
                    local.setdefault(item.url, item)
            elif parsed_episode is None:
                unknown.setdefault(item.url, item)
        # Three independent files are normally enough diversity.
        if exact >= 3:
            result = list(local.values())
            getattr(self, "logger", configure_logging()).info(
                "RESULT step=jimaku.files entry_id=%s episode=%s unfiltered=%s exact=%s server_filter=false",
                entry_id,
                episode,
                len(unfiltered),
                exact,
            )
            return result

        filtered = list(self.files(entry_id, int(episode)))
        for alias in sorted(aliases):
            if alias == int(episode):
                continue
            for item in self.files(entry_id, alias):
                if all(existing.url != item.url for existing in filtered):
                    filtered.append(item)
        merged: dict[str, JimakuFile] = {item.url: item for item in filtered}
        for item in local.values():
            merged.setdefault(item.url, item)
        if not merged:
            # A name without a readable episode fits any episode: only a last resort,
            # or every file of the entry would be downloaded and aligned for each one.
            merged.update((url, item) for url, item in unknown.items())
        result = list(merged.values())
        getattr(self, "logger", configure_logging()).info(
            "RESULT step=jimaku.files entry_id=%s episode=%s filtered=%s exact=%s unfiltered=%s merged=%s server_filter=true",
            entry_id,
            episode,
            len(filtered),
            exact,
            len(unfiltered),
            len(result),
        )
        return result

    def rank_entries(
        self, entries: list[JimakuEntry], identity: VideoIdentity, anilist_id: int | None
    ) -> list[JimakuEntry]:
        def score(entry: JimakuEntry) -> float:
            if anilist_id and entry.anilist_id == anilist_id:
                return 1000.0
            names = [entry.name, entry.english_name or "", entry.japanese_name or ""]
            # An entry without any name ranks last instead of raising ValueError.
            return max((title_similarity(identity.title, name) for name in names if name), default=0.0)

        return sorted(entries, key=score, reverse=True)

    def rank_files(
        self,
        files: list[JimakuFile],
        identity: VideoIdentity,
        video: Path,
        prefer_srt: bool = True,
        expected_airing_at: int | None = None,
        alternative_episodes: tuple[int, ...] = (),
    ) -> list[JimakuFile]:
        aliases = {int(value) for value in alternative_episodes if int(value) > 0}
        if identity.episode is not None:
            aliases.add(int(identity.episode))
        video_tags = release_tokens(video.name)
        for item in files:
            parsed = parse_anime_filename(item.name)
            similarity = title_similarity(identity.title, parsed.title)
            score = similarity * 0.32
            episode_range = explicit_episode_range(item.name)
            season_hint = explicit_season_hint(item.name, identity.title)
            item.details = {
                "parsed_title": parsed.title,
                "parsed_episode": parsed.episode,
                "parsed_season": parsed.season,
                "explicit_season_hint": season_hint,
                "explicit_episode_range": list(episode_range) if episode_range else None,
                "title_similarity": round(similarity, 2),
            }
            if (
                identity.season is not None
                and season_hint is not None
                and int(season_hint) != int(identity.season)
            ):
                score -= 1000
                item.details["season_match"] = "mismatch"
                item.details["hard_reject_reason"] = "season_mismatch"
            elif identity.season is not None and season_hint is not None:
                score += 12
                item.details["season_match"] = "exact"
            else:
                item.details["season_match"] = "unknown"
            if identity.episode is not None:
                if parsed.episode == identity.episode:
                    score += 55
                    item.details["episode_match"] = "exact"
                elif parsed.episode in aliases:
                    score += 53
                    item.details["episode_match"] = "absolute"
                elif episode_range is not None:
                    start, end = episode_range
                    if any(start <= alias <= end for alias in aliases):
                        score += 48
                        item.details["episode_match"] = "range"
                    else:
                        score -= 500
                        item.details["episode_match"] = "range_mismatch"
                        item.details["hard_reject_reason"] = "episode_outside_explicit_range"
                elif parsed.episode is None:
                    score += 3
                    item.details["episode_match"] = "unknown"
                else:
                    score -= 140
                    item.details["episode_match"] = "mismatch"
            else:
                # Movies/OVAs do not have an episode number, so title agreement must
                # carry the confidence that episode matching normally provides.
                score += similarity * 0.35
                if similarity >= 70:
                    score += 8
            overlap = video_tags & release_tokens(item.name)
            overlap_bonus = min(20, len(overlap) * 5)
            format_bonus = format_preference_bonus(item.name, prefer_srt)
            language_profile = subtitle_filename_language_profile(item.name)
            japanese_marker = bool(language_profile.get("japanese_marker"))
            language_purity = str(language_profile.get("purity") or "unknown")
            language_purity_bonus = {
                "japanese_only": 35.0,
                "unknown": 0.0,
                "mixed_japanese_english": -60.0,
                "mixed_japanese_chinese": -120.0,
                "chinese_only": -180.0,
            }.get(language_purity, 0.0)
            score += overlap_bonus
            score += format_bonus
            score += language_purity_bonus
            if japanese_marker:
                score += 10
            item.details.update(
                {
                    "release_token_overlap": sorted(overlap),
                    "overlap_bonus": overlap_bonus,
                    "format_bonus": format_bonus,
                    "japanese_marker": japanese_marker,
                    "chinese_marker": bool(language_profile.get("chinese_marker")),
                    "language_purity": language_purity,
                    "language_purity_priority": int(language_profile.get("priority") or 0),
                    "language_purity_bonus": language_purity_bonus,
                }
            )
            lowered = item.name.casefold()
            if any(token in lowered for token in ("signs", "songs", "karaoke", "forced")):
                score -= 35

            uploaded_at = _parse_timestamp(item.last_modified)
            if uploaded_at is not None:
                item.details["uploaded_at"] = uploaded_at
            if expected_airing_at is not None:
                item.details["expected_airing_at"] = int(expected_airing_at)
                if uploaded_at is not None:
                    delta = uploaded_at - int(expected_airing_at)
                    item.details["upload_after_airing_seconds"] = delta
                    # Jimaku may receive a subtitle slightly before the official
                    # timestamp because of timezone metadata or early distribution.
                    # More than 36 hours early is not credible for the requested episode.
                    if delta < -(36 * 3600):
                        if parsed.episode in aliases:
                            # Broadcaster/source schedules can differ from AniList.
                            # An exact episode number and strong title match must not
                            # be discarded solely because the file predates the stored
                            # airing timestamp. The final timing/semantic gate remains
                            # responsible for rejecting a different episode.
                            days_early = max(1.0, abs(delta) / 86400)
                            score -= min(45.0, 15.0 + days_early * 3.0)
                            item.details["airing_sanity"] = "before_airing_exact_episode"
                        else:
                            score -= 220
                            item.details["airing_sanity"] = "before_airing"
                    elif delta < 0:
                        score -= 20
                        item.details["airing_sanity"] = "slightly_early"
                    elif delta <= 21 * 86400:
                        score += 8
                        item.details["airing_sanity"] = "plausible"
                    else:
                        item.details["airing_sanity"] = "late"

            item.score = score
        return sorted(files, key=lambda item: item.score, reverse=True)

    def download(self, item: JimakuFile, cache_dir: Path) -> Path:
        target_dir = jimaku_download_dir(cache_dir, item)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / _safe_name(Path(item.name).name)
        expected = int(getattr(item, "size", 0) or 0)
        if (
            target.exists()
            and target.stat().st_size > 0
            and (expected <= 0 or target.stat().st_size >= expected)
        ):
            # The cache keeps recently used downloads (cache_management): mark this one
            # used. Touch the folder, not the archive: the archive's mtime keys its
            # unpacked members.
            try:
                os.utime(target_dir)
            except OSError:
                pass
            return target
        # Downloads use the same shared request budget and 429 cooldown as the API.
        rate_limit_remaining = self._rate_limit_remaining()
        if rate_limit_remaining > 0:
            raise JimakuRateLimited(self._rate_limit_message(rate_limit_remaining))
        try:
            self._acquire_request_slot("download")
            # Atomic: an interrupted write never becomes a cached subtitle.
            # A per-call name: two workers fetching the same file never write into one.
            temporary = target.with_name(f".{target.name}.{os.getpid()}-{time.monotonic_ns():x}.part")
            try:
                # Streamed to disk with a cap: an archive (or a wrong answer) is never held
                # in memory whole, and a huge one is not downloaded to the end.
                with self.client.stream("GET", item.url) as response:
                    if int(getattr(response, "status_code", 200) or 200) == 429:
                        headers = getattr(response, "headers", None) or {}
                        cooldown = self._set_rate_limit(
                            self._retry_after_seconds(headers.get("Retry-After", ""))
                        )
                        raise JimakuRateLimited(self._rate_limit_message(cooldown))
                    response.raise_for_status()
                    received = 0
                    with temporary.open("wb") as handle:
                        for chunk in response.iter_bytes(1024 * 1024):
                            received += len(chunk)
                            if received > JIMAKU_DOWNLOAD_LIMIT:
                                raise JimakuError(
                                    err(
                                        "jimaku.downloadTooLarge",
                                        name=item.name,
                                        limit=JIMAKU_DOWNLOAD_LIMIT // (1024 * 1024),
                                    )
                                )
                            handle.write(chunk)
                if expected > 0 and received < expected:
                    raise OSError(f"incomplete download: {received} of {expected} bytes")
                os.replace(temporary, target)
            finally:
                # A failed write (a full disk) leaves no partial file in the download folder.
                temporary.unlink(missing_ok=True)
            return target
        except (httpx.HTTPError, OSError) as exc:
            raise JimakuError(err("jimaku.downloadFailed", name=item.name, error=exc)) from exc


def _archive_member_score(
    name: str,
    identity: VideoIdentity,
    video: Path,
    prefer_srt: bool,
    allowed_episodes: tuple[int, ...] = (),
) -> float:
    aliases = {int(value) for value in allowed_episodes if int(value) > 0}
    if identity.episode is not None:
        aliases.add(int(identity.episode))
    parsed = parse_anime_filename(name)
    score = title_similarity(identity.title, parsed.title) * 0.3
    score += format_preference_bonus(name, prefer_srt)
    language_profile = subtitle_filename_language_profile(name)
    score += {
        "japanese_only": 35.0,
        "unknown": 0.0,
        "mixed_japanese_english": -60.0,
        "mixed_japanese_chinese": -120.0,
        "chinese_only": -180.0,
    }.get(str(language_profile.get("purity") or "unknown"), 0.0)
    if identity.episode is not None:
        if parsed.episode in aliases:
            score += 60
        elif parsed.episode is not None:
            score -= 100
    score += len(release_tokens(video.name) & release_tokens(name)) * 5
    if language_profile.get("japanese_marker"):
        score += 10
    lowered = name.casefold()
    if any(token in lowered for token in ("signs", "songs", "karaoke", "forced")):
        score -= 35
    return score


def _extract_archive_all(
    path: Path,
    identity: VideoIdentity,
    video: Path,
    output_dir: Path,
    prefer_srt: bool,
    allowed_episodes: tuple[int, ...] = (),
) -> list[tuple[Path, str, float]]:
    aliases = {int(value) for value in allowed_episodes if int(value) > 0}
    if identity.episode is not None:
        aliases.add(int(identity.episode))
    extracted: list[tuple[Path, str, float]] = []
    try:
        with zipfile.ZipFile(path) as zf:
            budget = ArchiveBudget(SUBTITLE_ARCHIVE_LIMIT)
            # Unpacked members belong to this exact archive: a replaced archive unpacks anew.
            archive_size, archive_mtime_ns = _archive_signature(path)
            names = [
                name
                for name in zf.namelist()
                if Path(name).suffix.casefold() in SUBTITLE_EXTENSIONS and not is_macos_metadata_path(name)
            ]
            ranked = sorted(
                (
                    (_archive_member_score(name, identity, video, prefer_srt, allowed_episodes), name)
                    for name in names
                ),
                reverse=True,
            )
            for score, name in ranked:
                parsed = parse_anime_filename(name)
                if (
                    identity.episode is not None
                    and parsed.episode is not None
                    and parsed.episode not in aliases
                ):
                    continue
                member_key = f"{name}\0{archive_size}\0{archive_mtime_ns}"
                digest = hashlib.sha1(member_key.encode("utf-8", errors="ignore")).hexdigest()[:8]
                output = output_dir / f"{digest}_{_safe_name(Path(name).name)}"
                if not output.exists() or output.stat().st_size == 0:
                    # Members were read whole: a tiny archive could unpack to gigabytes.
                    extract_member(zf, name, output, limit=_subtitle_member_limit(name), budget=budget)
                suffix = output.suffix.casefold()
                if suffix in IMAGE_SUBTITLE_EXTENSIONS or is_japanese_subtitle(output):
                    extracted.append((output, name, score))
    except ArchiveTooLargeError as exc:
        print(t("error.jimaku.extractFailedDetail", file=path.name, error=exc))
        return []
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return []
    return extracted


def _subtitle_member_limit(name: str) -> int:
    # Bitmap subtitles (and VobSub .sub streams) are much larger than text ones.
    suffix = Path(name).suffix.casefold()
    return (
        IMAGE_MEMBER_LIMIT if suffix in IMAGE_SUBTITLE_EXTENSIONS or suffix == ".sub" else TEXT_MEMBER_LIMIT
    )


def _copy_extracted_subtitles(
    root: Path,
    identity: VideoIdentity,
    video: Path,
    output_dir: Path,
    prefer_srt: bool,
    allowed_episodes: tuple[int, ...] = (),
) -> list[tuple[Path, str, float]]:
    aliases = {int(value) for value in allowed_episodes if int(value) > 0}
    if identity.episode is not None:
        aliases.add(int(identity.episode))
    extracted: list[tuple[Path, str, float]] = []
    for source in root.rglob("*"):
        # A link in the archive could point at any file on the Mac: never copy through one.
        if source.is_symlink() or not source.is_file() or source.suffix.casefold() not in SUBTITLE_EXTENSIONS:
            continue
        display_name = str(source.relative_to(root))
        if is_macos_metadata_path(display_name):
            continue
        parsed = parse_anime_filename(display_name)
        if identity.episode is not None and parsed.episode is not None and parsed.episode not in aliases:
            continue
        score = _archive_member_score(display_name, identity, video, prefer_srt, allowed_episodes)
        digest = hashlib.sha1(display_name.encode("utf-8", errors="ignore")).hexdigest()[:8]
        output = output_dir / f"{digest}_{_safe_name(source.name)}"
        if not output.exists() or output.stat().st_size == 0:
            shutil.copyfile(source, output)
        suffix = output.suffix.casefold()
        if suffix in IMAGE_SUBTITLE_EXTENSIONS or is_japanese_subtitle(output):
            extracted.append((output, display_name, score))
    return sorted(extracted, key=lambda item: item[2], reverse=True)


def _archive_extract_manifest_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.extract-v{ARCHIVE_EXTRACT_CACHE_SCHEMA}.json")


def _archive_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return int(stat.st_size), int(stat.st_mtime_ns)


def _cached_archive_result_rows(
    path: Path,
    identity: VideoIdentity,
    video: Path,
    output_dir: Path,
    prefer_srt: bool,
    allowed_episodes: tuple[int, ...],
) -> list[tuple[Path, str, float]] | None:
    """Return cached archive members, or ``None`` when extraction is required.

    Jimaku downloads are cached in a directory unique to the download URL.  Keep
    the expensive external unpacking (bsdtar/7-Zip) persistent as well.  The
    archive stat is part of the manifest so a replaced archive invalidates the
    extracted members automatically.
    """
    manifest = _archive_extract_manifest_path(path)
    payload: dict[str, object] | None = None
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        payload = raw if isinstance(raw, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        payload = None

    try:
        size, mtime_ns = _archive_signature(path)
    except OSError:
        return None

    members: list[dict[str, str]] | None = None
    if (
        payload is not None
        and int(payload.get("schema") or 0) == ARCHIVE_EXTRACT_CACHE_SCHEMA
        and int(payload.get("archive_size") or -1) == size
        and int(payload.get("archive_mtime_ns") or -1) == mtime_ns
        and isinstance(payload.get("members"), list)
    ):
        members = [item for item in payload["members"] if isinstance(item, dict)]
    elif path.suffix.casefold() in {".7z", ".rar"}:
        # v37 and older already persisted extracted files, but had no manifest.
        # A very common Jimaku shape is ``foo.sup.7z`` containing ``foo.sup``.
        # Bootstrap that exact single-member cache without one final 7zz run.
        display_name = path.stem
        if Path(display_name).suffix.casefold() in SUBTITLE_EXTENSIONS:
            digest = hashlib.sha1(display_name.encode("utf-8", errors="ignore")).hexdigest()[:8]
            output = output_dir / f"{digest}_{_safe_name(Path(display_name).name)}"
            if output.is_file() and output.stat().st_size > 0:
                members = [{"display_name": display_name, "output_name": output.name}]
                _write_archive_extract_manifest(path, members)

    if members is None:
        return None

    aliases = {int(value) for value in allowed_episodes if int(value) > 0}
    if identity.episode is not None:
        aliases.add(int(identity.episode))
    results: list[tuple[Path, str, float]] = []
    for item in members:
        display_name = str(item.get("display_name") or "")
        output_name = str(item.get("output_name") or "")
        if not display_name or not output_name or Path(output_name).name != output_name:
            return None
        output = output_dir / output_name
        try:
            if not output.is_file() or output.stat().st_size <= 0:
                return None
        except OSError:
            return None
        parsed = parse_anime_filename(display_name)
        if identity.episode is not None and parsed.episode is not None and parsed.episode not in aliases:
            continue
        suffix = output.suffix.casefold()
        if suffix not in IMAGE_SUBTITLE_EXTENSIONS and not is_japanese_subtitle(output):
            continue
        results.append(
            (
                output,
                display_name,
                _archive_member_score(display_name, identity, video, prefer_srt, allowed_episodes),
            )
        )
    return sorted(results, key=lambda item: item[2], reverse=True)


def _write_archive_extract_manifest(path: Path, members: list[dict[str, str]]) -> None:
    try:
        size, mtime_ns = _archive_signature(path)
        manifest = _archive_extract_manifest_path(path)
        payload = {
            "schema": ARCHIVE_EXTRACT_CACHE_SCHEMA,
            "archive_size": size,
            "archive_mtime_ns": mtime_ns,
            "members": members,
        }
        atomic_write_json(manifest, payload, ensure_ascii=False, indent=2)
    except OSError:
        # Cache persistence must never make subtitle discovery fail.
        return


def _extract_7z_all(
    path: Path,
    identity: VideoIdentity,
    video: Path,
    output_dir: Path,
    prefer_srt: bool,
    allowed_episodes: tuple[int, ...] = (),
) -> list[tuple[Path, str, float]]:
    cached = _cached_archive_result_rows(path, identity, video, output_dir, prefer_srt, allowed_episodes)
    if cached is not None:
        configure_logging().info("CACHE step=jimaku.archive_extract path=%s members=%s", path, len(cached))
        return cached

    try:
        with tempfile.TemporaryDirectory(prefix=f"{APP_SLUG}-archive-") as temp_dir:
            root = Path(temp_dir)
            # The whole archive is unpacked here, so the tool is stopped once the
            # folder outgrows the limit; the temporary folder is removed either way.
            unpack_archive(path, root, limit=SUBTITLE_ARCHIVE_LIMIT, timeout=120)
            results = _copy_extracted_subtitles(
                root, identity, video, output_dir, prefer_srt, allowed_episodes
            )
            members = [
                {"display_name": display_name, "output_name": output.name}
                for output, display_name, _score in results
            ]
            _write_archive_extract_manifest(path, members)
            configure_logging().info(
                "RESULT step=jimaku.archive_extract path=%s cache=write members=%s",
                path,
                len(members),
            )
            return results
    except ArchiveToolMissingError:
        print(t("error.jimaku.sevenZipMissing"))
        return []
    except ArchiveUnreadableError as exc:
        configure_logging().warning(
            "FAIL step=jimaku.archive_extract path=%s error=%r detail=%r", path, str(exc), exc.detail[-500:]
        )
        print(t("error.jimaku.extractFailedDetail", file=path.name, error=exc))
        return []
    except (OSError, subprocess.TimeoutExpired, ArchiveTooLargeError) as exc:
        print(t("error.jimaku.extractFailedDetail", file=path.name, error=exc))
        return []


def materialize_jimaku_files(
    client: JimakuClient,
    item: JimakuFile,
    identity: VideoIdentity,
    video: Path,
    cache_dir: Path,
    prefer_srt: bool = True,
    allowed_episodes: tuple[int, ...] = (),
) -> list[SubtitleCandidate]:
    downloaded = client.download(item, cache_dir)
    paths: list[tuple[Path, str, float]]
    archive_suffix = downloaded.suffix.casefold()
    if archive_suffix == ".zip":
        paths = _extract_archive_all(
            downloaded, identity, video, downloaded.parent, prefer_srt, allowed_episodes
        )
    elif archive_suffix in {".7z", ".rar"}:
        paths = _extract_7z_all(downloaded, identity, video, downloaded.parent, prefer_srt, allowed_episodes)
    else:
        paths = [(downloaded, item.name, 0.0)]

    candidates: list[SubtitleCandidate] = []
    for path, display_name, archive_score in paths:
        suffix = path.suffix.casefold()
        verified = suffix in IMAGE_SUBTITLE_EXTENSIONS or is_japanese_subtitle(path)
        if not verified:
            continue
        parsed_episode = parse_anime_filename(display_name).episode
        aliases = {int(value) for value in allowed_episodes if int(value) > 0}
        source_episode = parsed_episode
        normalized_episode = (
            identity.episode if identity.episode is not None and parsed_episode in aliases else parsed_episode
        )
        filename_language = subtitle_filename_language_profile(display_name)
        language_purity = str(filename_language.get("purity") or "unknown")
        bilingual_profile, english_profile = subtitle_language_profiles(path)
        filename_bilingual = language_purity == "mixed_japanese_chinese"
        bilingual_detected = bool(filename_bilingual or bilingual_profile.get("suspected_bilingual_cjk"))
        # Japanese + English in one file (Dororo "[SFSM] ... 03.jp-en.ass" beside the same
        # group's "03.jp.ass"): its English lines also match an English timing track, so it
        # won on timing while the viewer read English. A language suffix or the lines tell
        # (a "[JPN, ENG]" release tag alone is dual audio).
        bilingual_english = bool(
            language_purity == "mixed_japanese_english" or english_profile.get("suspected_bilingual_english")
        )
        if bilingual_english and language_purity in {"japanese_only", "unknown"}:
            language_purity = "mixed_japanese_english"
        bilingual_penalty = -85.0 if bilingual_detected or bilingual_english else 0.0
        candidates.append(
            SubtitleCandidate(
                path=path,
                source="jimaku",
                score=item.score + archive_score + bilingual_penalty,
                name=display_name,
                episode=normalized_episode,
                verified_japanese=verified,
                details={
                    **item.details,
                    "url": item.url,
                    "container": item.name,
                    "source_episode_number": source_episode,
                    "language_purity": language_purity,
                    "language_purity_priority": int(filename_language.get("priority") or 0),
                    "filename_bilingual_cjk": filename_bilingual,
                    "bilingual_cjk": bilingual_detected,
                    "bilingual_english": bilingual_english,
                    "bilingual_penalty": bilingual_penalty,
                    "bilingual_profile": bilingual_profile,
                    "bilingual_english_profile": english_profile,
                },
            )
        )
    return candidates
