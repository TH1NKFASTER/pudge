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

from ..filename import parse_anime_filename, release_tokens, title_similarity
from ..language import IMAGE_SUBTITLE_EXTENSIONS, TEXT_SUBTITLE_EXTENSIONS, is_japanese_subtitle
from ..logging_utils import configure_logging, timed_step
from ..branding import APP_SLUG
from ..models import JimakuEntry, JimakuFile, SubtitleCandidate, VideoIdentity
from ..subtitle_formats import (
    format_preference_bonus,
    subtitle_bilingual_cjk_profile,
    subtitle_filename_language_profile,
)


SUBTITLE_EXTENSIONS = TEXT_SUBTITLE_EXTENSIONS | IMAGE_SUBTITLE_EXTENSIONS
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar"}

# jimaku.cc documents a 25 requests/minute limit per API key. Keep a
# deliberate safety margin while still allowing a small interactive burst.
# Four initial tokens + 20/min sustained traffic stays below 25 requests in
# the first minute and then settles at one network request every ~3 seconds.
JIMAKU_LOCAL_REQUESTS_PER_MINUTE = 20.0
JIMAKU_LOCAL_BURST_CAPACITY = 4.0


def find_7zip() -> str | None:
    """Resolve 7-Zip even inside a Finder-launched macOS app.

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


def explicit_episode_range(name: str) -> tuple[int, int] | None:
    """Return an explicit episode span from a release filename.

    This intentionally ignores dimensions such as ``1440x1080`` and only
    accepts realistic episode numbers. It is used as a hard sanity check so a
    pack labelled ``01-02`` cannot be considered for episode 6 merely because
    the generic filename parser returned no single episode.
    """
    for pattern in _EPISODE_RANGE_PATTERNS:
        for match in pattern.finditer(name):
            start = int(match.group("start"))
            end = int(match.group("end"))
            if not (0 <= start <= 500 and 0 <= end <= 500):
                continue
            if start > end:
                start, end = end, start
            return start, end
    return None


class JimakuError(RuntimeError):
    pass




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


class JimakuClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        cache_dir: Path | None = None,
        cache_ttl_seconds: float = 2 * 60,
    ) -> None:
        if not api_key:
            raise JimakuError("Не задан JIMAKU_API_KEY")
        self.base_url = base_url.rstrip("/")
        self.cache_dir = cache_dir
        self.cache_ttl_seconds = max(0.0, float(cache_ttl_seconds))
        self._api_key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
        self.logger = configure_logging()
        self.client = httpx.Client(
            timeout=45,
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
        if self.cache_dir is None:
            return None
        return self.cache_dir / "jimaku-api" / "request-budget.json"

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
                    temporary = marker.with_name(
                        f"{marker.name}.{os.getpid()}.tmp"
                    )
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
        if self.cache_dir is None:
            return None
        return self.cache_dir / "jimaku-api" / "rate-limit.json"

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
        return f"Jimaku rate limited (429); retry in {minutes} min"

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
                    if time.time() - cache_path.stat().st_mtime <= self.cache_ttl_seconds:
                        count = len(cached_payload) if isinstance(cached_payload, list) else 1
                        if empty_dynamic_result:
                            self.logger.info(
                                "SKIP step=jimaku.http path=%s count=0 cache=empty_dynamic", path
                            )
                        else:
                            self.logger.info(
                                "RESULT step=jimaku.http path=%s count=%s cache=hit", path, count
                            )
                            return cached_payload
            except (OSError, ValueError, json.JSONDecodeError):
                stale_payload = None

        rate_limit_remaining = self._rate_limit_remaining()
        if rate_limit_remaining > 0:
            if stale_payload is not None:
                count = len(stale_payload) if isinstance(stale_payload, list) else 1
                self.logger.warning(
                    "FALLBACK step=jimaku.http path=%s cache=stale count=%s reason=rate_limited remaining_s=%.0f",
                    path, count, rate_limit_remaining,
                )
                return stale_payload
            raise JimakuError(self._rate_limit_message(rate_limit_remaining))

        last_network_error: httpx.HTTPError | None = None
        for attempt in range(3):
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
                                path, count, cooldown,
                            )
                            return stale_payload
                        raise JimakuError(self._rate_limit_message(cooldown))
                    response.raise_for_status()
                    payload = response.json()
                count = len(payload) if isinstance(payload, list) else 1
                self.logger.info("RESULT step=jimaku.http path=%s count=%s cache=miss", path, count)
                empty_dynamic_result = payload == [] and (
                    path == "/api/entries/search" or path.endswith("/files")
                )
                if cache_path is not None and not empty_dynamic_result:
                    try:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = cache_path.with_suffix(".tmp")
                        temporary.write_text(
                            json.dumps(payload, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        temporary.replace(cache_path)
                    except OSError:
                        pass
                return payload
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_network_error = exc
                if attempt < 2:
                    self.logger.warning(
                        "RETRY step=jimaku.http path=%s attempt=%s error=%s",
                        path,
                        attempt + 1,
                        exc,
                    )
                    time.sleep(0.25 * (2 ** attempt))
                    continue
                break
            except httpx.HTTPError as exc:
                # HTTP status errors are deterministic enough that retrying them
                # immediately only creates extra load. DNS/connect failures are
                # handled by the retry branch above.
                raise JimakuError(f"Ошибка Jimaku API: {exc}") from exc
            except ValueError as exc:
                raise JimakuError(f"Ошибка Jimaku API: {exc}") from exc

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
            raise JimakuError(f"Ошибка Jimaku API: {last_network_error}") from last_network_error
        raise JimakuError("Ошибка Jimaku API: неизвестная сетевая ошибка")

    def search_entries(self, *, anilist_id: int | None = None, query: str | None = None) -> list[JimakuEntry]:
        params: dict[str, object] = {"anime": "true"}
        if anilist_id is not None:
            params["anilist_id"] = anilist_id
        if query:
            params["query"] = query
        payload = self._get_json("/api/entries/search", params=params)
        result: list[JimakuEntry] = []
        for item in payload:
            result.append(
                JimakuEntry(
                    id=int(item["id"]),
                    name=str(item["name"]),
                    english_name=item.get("english_name"),
                    japanese_name=item.get("japanese_name"),
                    anilist_id=int(item["anilist_id"]) if item.get("anilist_id") else None,
                    flags=dict(item.get("flags") or {}),
                )
            )
        return result

    def files(self, entry_id: int, episode: int | None) -> list[JimakuFile]:
        params = {"episode": episode} if episode is not None else None
        payload = self._get_json(f"/api/entries/{entry_id}/files", params=params)
        return [
            JimakuFile(
                url=str(item["url"]),
                name=str(item["name"]),
                size=int(item.get("size", 0)),
                last_modified=str(item.get("last_modified", "")),
            )
            for item in payload
        ]

    def files_for_episode(
        self,
        entry_id: int,
        episode: int | None,
        alternative_episodes: tuple[int, ...] = (),
    ) -> list[JimakuFile]:
        """Return episode-filtered files, with a local-filter fallback.

        Jimaku's server-side episode index can occasionally miss files whose names
        still contain an unambiguous episode number, especially source-specific
        releases such as ``AT-X 1440x1080``. It can also return only one source for
        an episode even though the entry contains several independent variants.
        A single badly timed source must not prevent the remaining sources from
        being evaluated, so sparse server results are supplemented from the full
        entry and filtered locally.
        """
        aliases = {int(value) for value in alternative_episodes if int(value) > 0}
        if episode is not None:
            aliases.add(int(episode))
        filtered = self.files(entry_id, episode)
        for alias in sorted(aliases):
            if episode is not None and alias == episode:
                continue
            for item in self.files(entry_id, alias):
                if all(existing.url != item.url for existing in filtered):
                    filtered.append(item)
        if episode is None:
            getattr(self, "logger", configure_logging()).info(
                "RESULT step=jimaku.files entry_id=%s episode=none filtered=%s fallback=false",
                entry_id, len(filtered),
            )
            return filtered

        exact_filtered = [
            item for item in filtered
            if parse_anime_filename(item.name).episode in aliases
        ]
        # Three independent files are normally enough diversity. With fewer than
        # that, inspect the whole entry because Jimaku's episode index is known to
        # omit otherwise valid Netflix/Amazon/BD variants on some entries.
        if len(exact_filtered) >= 3:
            getattr(self, "logger", configure_logging()).info(
                "RESULT step=jimaku.files entry_id=%s episode=%s filtered=%s exact=%s fallback=false",
                entry_id, episode, len(filtered), len(exact_filtered),
            )
            return filtered

        unfiltered = self.files(entry_id, None)
        merged: dict[str, JimakuFile] = {item.url: item for item in filtered}
        for item in unfiltered:
            parsed_episode = parse_anime_filename(item.name).episode
            episode_range = explicit_episode_range(item.name)
            if parsed_episode in aliases:
                merged.setdefault(item.url, item)
            elif episode_range is not None:
                start, end = episode_range
                if any(start <= alias <= end for alias in aliases):
                    merged.setdefault(item.url, item)
            elif parsed_episode is None:
                merged.setdefault(item.url, item)
        result = list(merged.values())
        getattr(self, "logger", configure_logging()).info(
            "RESULT step=jimaku.files entry_id=%s episode=%s filtered=%s exact=%s unfiltered=%s merged=%s fallback=true",
            entry_id, episode, len(filtered), len(exact_filtered), len(unfiltered), len(result),
        )
        return result

    def rank_entries(self, entries: list[JimakuEntry], identity: VideoIdentity, anilist_id: int | None) -> list[JimakuEntry]:
        def score(entry: JimakuEntry) -> float:
            if anilist_id and entry.anilist_id == anilist_id:
                return 1000.0
            names = [entry.name, entry.english_name or "", entry.japanese_name or ""]
            return max(title_similarity(identity.title, name) for name in names if name)

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
            item.details = {
                "parsed_title": parsed.title,
                "parsed_episode": parsed.episode,
                "explicit_episode_range": list(episode_range) if episode_range else None,
                "title_similarity": round(similarity, 2),
            }
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
                "mixed_japanese_chinese": -120.0,
                "chinese_only": -180.0,
            }.get(language_purity, 0.0)
            score += overlap_bonus
            score += format_bonus
            score += language_purity_bonus
            if japanese_marker:
                score += 10
            item.details.update({
                "release_token_overlap": sorted(overlap),
                "overlap_bonus": overlap_bonus,
                "format_bonus": format_bonus,
                "japanese_marker": japanese_marker,
                "chinese_marker": bool(language_profile.get("chinese_marker")),
                "language_purity": language_purity,
                "language_purity_priority": int(language_profile.get("priority") or 0),
                "language_purity_bonus": language_purity_bonus,
            })
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
        digest = hashlib.sha1(item.url.encode()).hexdigest()[:16]
        target_dir = cache_dir / "jimaku" / digest
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / _safe_name(Path(item.name).name)
        if target.exists() and target.stat().st_size > 0:
            return target
        try:
            response = self.client.get(item.url)
            response.raise_for_status()
            target.write_bytes(response.content)
            return target
        except (httpx.HTTPError, OSError) as exc:
            raise JimakuError(f"Не удалось скачать {item.name}: {exc}") from exc


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
            names = [name for name in zf.namelist() if Path(name).suffix.casefold() in SUBTITLE_EXTENSIONS]
            ranked = sorted(
                ((_archive_member_score(name, identity, video, prefer_srt, allowed_episodes), name) for name in names),
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
                digest = hashlib.sha1(name.encode("utf-8", errors="ignore")).hexdigest()[:8]
                output = output_dir / f"{digest}_{_safe_name(Path(name).name)}"
                if not output.exists() or output.stat().st_size == 0:
                    with zf.open(name) as src, output.open("wb") as dst:
                        dst.write(src.read())
                suffix = output.suffix.casefold()
                if suffix in IMAGE_SUBTITLE_EXTENSIONS or is_japanese_subtitle(output):
                    extracted.append((output, name, score))
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return []
    return extracted



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
        if not source.is_file() or source.suffix.casefold() not in SUBTITLE_EXTENSIONS:
            continue
        display_name = str(source.relative_to(root))
        parsed = parse_anime_filename(display_name)
        if (
            identity.episode is not None
            and parsed.episode is not None
            and parsed.episode not in aliases
        ):
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


def _extract_7z_all(
    path: Path,
    identity: VideoIdentity,
    video: Path,
    output_dir: Path,
    prefer_srt: bool,
    allowed_episodes: tuple[int, ...] = (),
) -> list[tuple[Path, str, float]]:
    tool = find_7zip()
    if tool is None:
        print("Jimaku: найден архив .7z/.rar, но 7-Zip не установлен")
        return []
    try:
        with tempfile.TemporaryDirectory(prefix=f"{APP_SLUG}-archive-") as temp_dir:
            root = Path(temp_dir)
            completed = subprocess.run(
                [tool, "x", "-y", f"-o{root}", str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=120,
                check=False,
            )
            if completed.returncode != 0:
                print(f"Jimaku: не удалось распаковать {path.name}: {completed.stdout[-500:]}")
                return []
            return _copy_extracted_subtitles(
                root, identity, video, output_dir, prefer_srt, allowed_episodes
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Jimaku: не удалось распаковать {path.name}: {exc}")
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
        paths = _extract_7z_all(
            downloaded, identity, video, downloaded.parent, prefer_srt, allowed_episodes
        )
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
            identity.episode
            if identity.episode is not None and parsed_episode in aliases
            else parsed_episode
        )
        filename_language = subtitle_filename_language_profile(display_name)
        language_purity = str(filename_language.get("purity") or "unknown")
        bilingual_profile = subtitle_bilingual_cjk_profile(path)
        filename_bilingual = language_purity == "mixed_japanese_chinese"
        bilingual_detected = bool(
            filename_bilingual or bilingual_profile.get("suspected_bilingual_cjk")
        )
        bilingual_penalty = -85.0 if bilingual_detected else 0.0
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
                    "bilingual_penalty": bilingual_penalty,
                    "bilingual_profile": bilingual_profile,
                },
            )
        )
    return candidates


def materialize_jimaku_file(
    client: JimakuClient,
    item: JimakuFile,
    identity: VideoIdentity,
    video: Path,
    cache_dir: Path,
    prefer_srt: bool = True,
    allowed_episodes: tuple[int, ...] = (),
) -> SubtitleCandidate | None:
    """Backward-compatible helper returning the highest-ranked extracted file."""
    candidates = materialize_jimaku_files(
        client,
        item,
        identity,
        video,
        cache_dir,
        prefer_srt=prefer_srt,
        allowed_episodes=allowed_episodes,
    )
    return max(candidates, key=lambda candidate: candidate.score, default=None)
