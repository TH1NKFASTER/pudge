from __future__ import annotations

import contextvars
import functools
import datetime
import email.utils
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator

import httpx
from rapidfuzz import fuzz


if TYPE_CHECKING:
    from .providers.seadex import SeaDexRecommendation
from .anilist_tracking import (
    flush_pending_progress,
    flush_pending_scores,
    flush_pending_statuses,
    pending_progress_by_media,
    pending_scores,
    pending_statuses,
)
from . import disk_space
from .fsutil import atomic_write_bytes
from .personal_schedule import READY_BEFORE_PLAYBACK, ready_marker_is_stale
from .backup import prune_one_off_copies
from .cache_management import enforce_derived_caches, purge_episode_derived_files
from .cache_registry import CacheRegistry
from .config import AppConfig
from .branding import APP_SLUG, LEGACY_APP_NAMES, LEGACY_APP_SLUGS
from .database import (
    EXTRA_FILE_ACTION_CODES,
    STATE_PREFIX_SQL,
    Database,
    reused_connection,
    state_prefix_bounds,
)
from .database_storage import maintain_database_storage
from .filename import (
    looks_like_release_name,
    parse_anime_filename,
    release_name_display_title,
    title_similarity,
    with_title_memo,
)
from .language import IMAGE_SUBTITLE_EXTENSIONS, TEXT_SUBTITLE_EXTENSIONS
from .jimaku_trial import apply_jimaku_trial
from .identity import IdentityResolver, MediaIdentity
from .library import (
    VIDEO_EXTENSIONS,
    aria2_control_file,
    aria2_control_infohash,
    japanese_subtitle_details,
    japanese_subtitle_source,
    scan_library,
    sidecar_subtitle,
    strict_title_similarity,
    unfinished_video,
    user_episode_identity,
    user_identity_state_key,
)
from .runtime import python_executable
from .download_intents import DownloadIntentStore, UnconfirmedCandidatesError
from .download_priority import download_order
from .queued_releases import QueuedReleaseStore
from .episode_numbering import (
    automatic_lookups as automatic_numbering_lookups,
    clear_lookup_failures as clear_numbering_lookup_failures,
    episode_numbering_from_graph,
    media_episode_from_release as shared_media_episode_from_release,
    resolve_episode_numbering,
    match_release_episode,
    is_split_stage,
)
from . import extra_files
from .release_parser import double_episode_span, parse_release_name
from .episode_state import single_unit_title, watched_by_anilist_progress
from .presentation_state import derive_episode_presentation, download_complete
from .work_scheduler import WorkPriority, WorkScheduler
from .torrent_admission import TorrentAdmission
from .logging_utils import configure_logging, timed_step
from .maintenance_lock import maintenance_lock
from .manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease
from .planned_release_discovery import PlannedReleaseDiscovery
from .manager_services import ReleaseTelemetryService
from .notifications import send_native_notification
from .i18n import err, message as i18n_message, t
from .pipeline_cache import invalidate_final_pipeline_result
from .providers.anilist import (
    ANILIST_USER_AGENT,
    AniListClient,
    AniListError,
    AniListHTTPError,
    AniListNotFoundError,
    anilist_refusal,
    bulk_requests,
)
from .providers.nyaa import (
    NoReleaseSourceError,
    NyaaClient,
    NyaaError,
    ReleaseSearchUnavailableError,
    SubsPleaseClient,
    ShanaProjectClient,
    fresh_trusted_zero_seeders_allowed,
    is_camrip_release,
    search_ranked,
    search_shana_ranked,
    search_subsplease_ranked,
    same_web_encode,
    seadex_batch_candidates,
    _season_number,
    release_is_safe_batch_candidate,
    release_episode as parsed_release_episode,
    release_episode_range,
    SCORE_FORMULA_VERSION,
    score_context_key,
)
from .providers.qbittorrent import QBittorrentClient, QBittorrentError
from .providers.aria2 import Aria2Client
from .sources import load_sources
from .relation_graphs import (
    compact_relations_from_graph,
    graph_for_root,
    next_relation_refresh_at,
    relation_retry_at,
)
from .subtitles.jobs import read_job_report
from .subtitles.selection import upgrade_is_better
from .subtitle_formats import subtitle_has_genuine_overlaps
from . import subtitle_migrations
from .debug_snapshot import (
    append_debug_trace,
    prune_subtitle_debug,
    record_video_selection_debug,
    subtitle_debug_paths,
    write_prepare_debug_result,
)
from .energy_diagnostics import EnergyDiagnosticsMonitor
from .process_priority import mark_background


LogFn = Callable[[str], None]

# The release search failure of the last search_releases() call in this thread.
# The Planning batch step must tell "the release search is unavailable" from
# "nothing found": search_releases() falls back to the other feeds and returns
# a list either way (it runs in the caller's thread, so this is per call).
_RELEASE_SEARCH_OUTCOME = threading.local()
# Inside AnimeManager.release_search_cancellable(): the check that says whether this
# thread's release search may go on (Torrent Off cancels the On toggle's search).
_RELEASE_SEARCH_CANCEL = threading.local()
# Inside AnimeManager.ignore_auto_search_backoff(): the user asked for a search now (Refresh,
# "search new releases", Torrents On), so episodes that keep missing are searched too.
_AUTO_SEARCH_BYPASS = threading.local()


# WEBFLOW-8: reconcile_completed_download_rows called by the foreground poll.
_RECONCILE_FROM_POLL: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "pudge_reconcile_from_poll", default=False
)
# WEBFLOW-6: ignore-marker answers per folder for one Home build (library_ignore_memo).
_IGNORE_ROOT_MEMO: contextvars.ContextVar[dict[str, Path | None] | None] = contextvars.ContextVar(
    "pudge_ignore_root_memo", default=None
)


@functools.lru_cache(maxsize=16)
def _root_parts(roots: tuple[Any, ...]) -> frozenset[tuple[str, ...]]:
    """Components of each root for AnimeManager._path_within (one build reuses one roots tuple)."""
    return frozenset(root.parts for root in roots if isinstance(root, PurePath))


def _reusing_db_connection(method: Any) -> Any:
    """Run a pass of many small DB calls on one connection (Database.reuse_connection)."""

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        with reused_connection(self.db):
            return method(self, *args, **kwargs)

    return wrapper


class ManagerError(RuntimeError):
    pass


def _identified_anime(media_id: int, entry: dict[str, Any] | None) -> LibraryAnime:
    """A local row for an AniList entry the person picked for a file (not on their list)."""
    entry = entry if isinstance(entry, dict) else {}

    def text(key: str, limit: int = 300) -> str:
        value = entry.get(key)
        return str(value).strip()[:limit] if isinstance(value, (str, int, float)) else ""

    def number(key: str) -> int | None:
        try:
            value = int(entry.get(key))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def web_url(key: str) -> str:
        value = text(key, 2000)
        return value if value.startswith(("https://", "http://")) else ""

    title = text("title") or f"AniList {int(media_id)}"
    native = text("native_title")
    return LibraryAnime(
        media_id=int(media_id),
        title=title,
        titles=[name for name in dict.fromkeys((title, native)) if name],
        cover_url=web_url("cover"),
        site_url=web_url("site_url") or f"https://anilist.co/anime/{int(media_id)}",
        status="",
        episodes=number("episodes"),
        format=re.sub(r"[^A-Z_]", "", text("format", 40).upper()) or None,
        season_year=number("year"),
        media_status=re.sub(r"[^A-Z_]", "", text("media_status", 40).upper()) or None,
        duration=number("duration"),
    )


class NotEnoughDiskSpaceError(ManagerError):
    """A download that would eat into the free-space reserve of the library volume."""


def _process_rss_mb(pid: int) -> float | None:
    """Return resident memory for one child process using the native ps tool."""
    try:
        completed = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(int(pid))],
            text=True,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    try:
        rss_kb = float((completed.stdout or "").strip().splitlines()[0])
    except (IndexError, TypeError, ValueError):
        return None
    return max(0.0, rss_kb / 1024.0)


_NETWORK_RETRY_MARKERS = (
    "httpx.connecterror",
    "httpx.connecttimeout",
    "httpx.readtimeout",
    "httpx.writetimeout",
    "httpx.pooltimeout",
    "httpx.remoteprotocolerror",
    "httpcore.connecterror",
    "httpcore.connecttimeout",
    "httpcore.readtimeout",
    "httpcore.writetimeout",
    "httpcore.pooltimeout",
    "httpcore.remoteprotocolerror",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname provided",
    "network is unreachable",
    "connection refused",
    "connection reset by peer",
    "server disconnected",
    "dns lookup",
)


def _subtitle_retry_is_rate_limit(detail: str) -> bool:
    lowered = str(detail or "").casefold()
    return bool(
        "jimaku rate limited" in lowered
        or "429 too many requests" in lowered
        or re.search(r"\b429\b.*\btoo many requests\b", lowered)
    )


def _subtitle_retry_is_network_error(detail: str) -> bool:
    """Return whether preparation failed because an external service was unreachable.

    A bare ``timeout`` is intentionally not enough: local ALASS/ffsubsync/OCR
    timeouts must continue using the user-selected subtitle interval.
    """
    lowered = str(detail or "").casefold()
    if _subtitle_retry_is_rate_limit(lowered):
        return True
    if any(marker in lowered for marker in _NETWORK_RETRY_MARKERS):
        return True
    return bool(
        re.search(
            r"\bhttp(?: status)?[ :=]+(?:408|429|5\d\d)\b",
            lowered,
        )
    )


_DETERMINISTIC_VALIDATION_MARKERS = (
    "all subtitle candidates failed synchronization/validation",
    "too_many_large_jumps",
    "full_range_oscillation",
    "no_stable_offset_cluster",
    "timeline_validation_failed",
    "timeline_segment_support_too_low",
    "timeline_unstable_segments",
    "timeline_weak_path",
)

# "Nothing on Jimaku yet" is waiting for an upload, not a failure that repeats.
_SUBTITLES_NOT_FOUND_MARKERS = (
    "jimaku: no matching anime found",
    "jimaku: no sufficiently confident file found",
    "japanese subtitles not found yet",
    "subtitles not found yet",
)

# The same failure this many times in a row (a 20-minute timeout, one error) ends the
# automatic retries: the job asks the person (Action required) instead.
SUBTITLE_FAILURE_STREAK_CAP = 5
SUBTITLE_RETRY_EXHAUSTED = "subtitle_retry_exhausted"


def _subtitle_failure_signature(detail: str, prepare_status: str = "") -> str:
    """Name a failed preparation so equal failures in a row can be counted."""
    lowered = str(detail or "").casefold()
    if prepare_status == "waiting_subtitles" or any(
        marker in lowered for marker in _SUBTITLES_NOT_FOUND_MARKERS
    ):
        return "not_found"
    if any(marker in lowered for marker in _DETERMINISTIC_VALIDATION_MARKERS):
        return "validation"
    lines = [line.strip() for line in lowered.splitlines() if line.strip()]
    last = re.sub(r"\d+", "#", lines[-1] if lines else "")[:240]
    return "error:" + hashlib.sha1(last.encode("utf-8")).hexdigest()[:12]


def _jimaku_lookup_incomplete(detail: str) -> bool:
    """The Jimaku lookup stopped on an error (unreachable, rate limited): it kept only the
    candidates found before, so its result says nothing final about the episode."""
    lowered = str(detail or "").casefold()
    return "jimaku api error" in lowered or _subtitle_retry_is_rate_limit(lowered)


def _upgrade_request_priority(request: dict[str, Any], default: int) -> int:
    """Queue priority stored with an upgrade check or rebuild, ``default`` without one."""
    try:
        return max(0, int(request.get("requeue_priority", default)))
    except (TypeError, ValueError):
        return default


def _subtitle_failure_backoff_seconds(*, poll_minutes: float, streak: int) -> float:
    """The configured interval, doubled for each identical failure before, at most a day."""
    configured = max(60.0, float(poll_minutes) * 60.0)
    return min(24 * 3600.0, configured * (2 ** max(0, min(int(streak) - 1, 16))))


_REAL_POPEN = subprocess.Popen


def _signal_prepare_child(process: Any, sig: int, *, fallback: bool = True) -> None:
    """Signal a prepare worker together with what it started (ffmpeg, alass, its pool).

    The worker runs in its own process group (``start_new_session``), so the group
    id is its pid. Stand-ins without a real process get terminate()/kill().
    """
    pid = getattr(process, "pid", None)
    if isinstance(process, _REAL_POPEN) and isinstance(pid, int) and pid > 0 and pid != os.getpgrp():
        try:
            os.killpg(pid, sig)
            return
        except OSError:
            pass
    if not fallback:
        return
    if sig == signal.SIGKILL:
        process.kill()
    else:
        process.terminate()


def _stop_prepare_child(process: Any) -> None:
    """SIGTERM to the worker's group, SIGKILL after 5 s; then nothing of it is left."""
    _signal_prepare_child(process, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _signal_prepare_child(process, signal.SIGKILL)
        process.wait(timeout=5)
    else:
        # The worker stopped; a child of it that ignored SIGTERM must not run on.
        _signal_prepare_child(process, signal.SIGKILL, fallback=False)


def _subtitle_retry_delay_seconds(
    *,
    poll_minutes: float,
    attempts: int,
    detail: str,
) -> float:
    """Use the configured interval unless this is a network/service failure."""
    configured = max(1.0, float(poll_minutes) * 60.0)
    deterministic_validation = any(
        marker in str(detail or "").casefold() for marker in _DETERMINISTIC_VALIDATION_MARKERS
    )
    if deterministic_validation:
        return max(configured, 6 * 3600.0)
    if _subtitle_retry_is_rate_limit(detail):
        return configured
    if not _subtitle_retry_is_network_error(detail):
        return configured
    if int(attempts) <= 12:
        return configured
    if int(attempts) <= 36:
        return max(configured, 3600.0)
    return max(configured, 6 * 3600.0)


def _subtitle_source_retry_delay_seconds(
    *,
    poll_minutes: float,
    media_status: str | None,
    minimum_seconds: float = 5 * 60.0,
) -> float:
    """Poll airing titles promptly, but stop hammering finished archives.

    A finished show's Jimaku candidate set normally changes rarely.  Airing
    titles, unknown metadata, and not-yet-released titles retain the configured
    short poll so a newly published subtitle is still discovered promptly.
    """
    configured = max(float(minimum_seconds), float(poll_minutes) * 60.0)
    if str(media_status or "").strip().upper() == "FINISHED":
        return max(configured, 24 * 3600.0)
    return configured


_PREPARATION_DETAIL_EN_REPLACEMENTS = (
    (
        "AniList: обновление прогресса отключено для этого запуска",
        "AniList: progress updates disabled for this run",
    ),
    ("AniList: отключён для этого запуска (--offline)", "AniList: disabled for this run (--offline)"),
    ("AniList: интеграция отключена в настройках", "AniList: integration is disabled in settings"),
    (
        "AniList: access token не задан; трекер и горячие клавиши отключены",
        "AniList: access token is not set; tracker and hotkeys are disabled",
    ),
    (
        "Jimaku: API key не задан, интернет-поиск пропущен",
        "Jimaku: API key is not set; online search skipped",
    ),
    ("Jimaku: подходящее аниме не найдено", "Jimaku: no matching anime found"),
    (
        "Jimaku: файл нужной серии не найден с достаточной уверенностью",
        "Jimaku: no sufficiently confident file found for the requested episode",
    ),
    ("Японские субтитры пока не найдены", "Japanese subtitles not found yet"),
    (
        "Японские субтитры не найдены; mpv будет запущен без добавленного файла",
        "Japanese subtitles not found; mpv will start without an added subtitle file",
    ),
    ("Субтитры пока не найдены", "Subtitles not found yet"),
    (
        "Субтитры отклонены проверкой качества: все проверенные варианты отклонены",
        "Subtitles rejected by quality validation: all checked candidates were rejected",
    ),
    ("Проверка constant-offset эталона:", "Constant-offset reference check:"),
    ("Выбран лучший constant-offset эталон:", "Best constant-offset reference selected:"),
    ("LLM-проверка встроенного эталона:", "Embedded-reference LLM check:"),
    ("Прямое сопоставление субтитров через ALASS:", "Direct subtitle matching via ALASS:"),
    ("Проверка прямого ALASS по английскому эталону:", "Direct ALASS English-reference validation:"),
    ("Эталон тайминга: встроенная дорожка", "Timing reference: embedded track"),
    ("Выравнивание по английскому эталону не выполнено", "Alignment against English reference failed"),
    ("локальная проверка отклонена", "local validation rejected"),
    ("структура=", "structure="),
    ("отклонён", "rejected"),
    ("принят", "accepted"),
)


def _localize_preparation_detail(detail: str, *, language: str) -> str:
    """Translate user-facing prepare-job diagnostics without altering stored job data.

    Preparation runs are subprocesses and older versions wrote several status
    lines in Russian unconditionally.  Translating at presentation time fixes
    both new and already persisted jobs and keeps language switching reversible.
    """
    text = str(detail or "")
    if str(language or "en").casefold() == "ru":
        return text
    for source, target in _PREPARATION_DETAIL_EN_REPLACEMENTS:
        text = text.replace(source, target)
    return text


def _subtitle_upgrade_backoff_hours(completed_checks: int) -> float:
    checks = max(0, int(completed_checks))
    if checks <= 1:
        return 6.0
    return min(24.0, 6.0 * (2 ** (checks - 1)))


_ARIA2_METADATA_RE = re.compile(r"[0-9a-f]{40}\.torrent")

# A quality upgrade that has not become Ready within a week is given up; the
# old file stays and the episode can be checked for upgrades again.
UPGRADE_JOB_TTL_SECONDS = 7 * 24 * 3600.0

# Torrent states whose payload no longer grows on disk.
_SETTLED_TORRENT_STATES = {
    "complete",
    "uploading",
    "stalledup",
    "pausedup",
    "stoppedup",
    "queuedup",
    "forcedup",
    "checkingup",
    "error",
    "missingfiles",
    "removed",
    "missing",
}


def _download_remaining_bytes(item: Any) -> int | None:
    """Bytes an unfinished download will still write; ``None`` while its size is unknown."""
    state = str(getattr(item, "state", "") or "").casefold()
    raw = getattr(item, "raw", None) or {}
    try:
        progress = float(getattr(item, "progress", 0.0) or 0.0)
        if progress >= 1.0 or state in _SETTLED_TORRENT_STATES:
            return 0
        if raw.get("amount_left") is not None:
            return max(0, int(raw["amount_left"]))
        total = int(raw.get("total_size") or raw.get("size") or 0)
        if total <= 0:
            return None
        if raw.get("downloaded") is not None:
            return max(0, total - int(raw.get("downloaded") or 0))
        return max(0, int(total * (1.0 - max(0.0, progress))))
    except (TypeError, ValueError):
        return None


def _download_written_bytes(item: Any) -> int:
    """Bytes an unfinished download has written so far (0 while unknown)."""
    raw = getattr(item, "raw", None) or {}
    try:
        if raw.get("completed") is not None:  # qBittorrent: done bytes of the wanted files
            return max(0, int(raw.get("completed") or 0))
        total = int(raw.get("size") or raw.get("total_size") or 0)
        if total > 0 and raw.get("amount_left") is not None:
            return max(0, total - int(raw["amount_left"]))
        if raw.get("downloaded") is not None:
            return max(0, int(raw.get("downloaded") or 0))
        if total > 0:
            return max(0, int(total * max(0.0, min(1.0, float(getattr(item, "progress", 0.0) or 0.0)))))
    except (TypeError, ValueError):
        pass
    return 0


def _torrent_app_may_start(method):
    """Adding a torrent may launch a closed qBittorrent; reads and polls never do."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.allow_torrent_app_start():
            return method(self, *args, **kwargs)

    return wrapper


class AniListSyncResult(list):
    """The Watching/Planning list of one AniList refresh, and where it came from.

    ``used_cache`` is True when AniList did not answer and the saved list was
    returned; ``error`` is then AniList's reason. A list subclass, so callers
    that only want the titles keep working.
    """

    def __init__(
        self,
        items: Iterable[LibraryAnime] = (),
        *,
        used_cache: bool = False,
        warning: str = "",
        error: str = "",
    ) -> None:
        super().__init__(items)
        self.used_cache = bool(used_cache)
        self.warning = str(warning or "")
        self.error = str(error or "")


class AnimeManager:
    _acquisition_keys_lock = threading.Lock()

    def __init__(self, config: AppConfig, log: LogFn | None = None, *, owns_database: bool = False) -> None:
        self.config = config
        # Only the window owns the library schema (DB-6): the agent and CLI runs open
        # it without the write lock, schema script and full-file check; the window
        # decides when quick_check is due (WebAppApi._startup_integrity_check).
        self.db = Database(
            config.library.database_path,
            initialize=owns_database,
            integrity_check=False,
            # The window's repository calls (each phone request, every poll) keep one
            # connection per thread; retire() before a restore closes them.
            thread_connections=owns_database,
        )
        self._owns_database = bool(owns_database)  # the window's manager (not the agent / CLI)
        self.logger = configure_logging()
        self.log = log or self.logger.info
        self.work_scheduler = WorkScheduler(
            config.paths.cache_dir,
            logger=self.logger,
            power_manual_enabled=config.power.manual_energy_saving,
            power_auto_enabled=config.power.auto_battery_energy_saving,
        )
        self.identity_resolver = IdentityResolver(self.db)
        self.release_telemetry = ReleaseTelemetryService(self.db)
        self.download_intents = DownloadIntentStore(self.db)
        self.queued_releases = QueuedReleaseStore(self.db)
        # Two threads (a manual download and auto_search_current) must not race
        # the same episode: check-and-add under the lock.
        self._active_acquisition_keys: set[str] = set()
        self._last_missing_episode_rows = 0
        self._last_completed_video_paths: tuple[Path, ...] = ()
        # Completed downloads waiting for their OP/ED detection step (request_intro_detection).
        self._intro_requested: dict[Path, None] = {}
        self._intro_request_lock = threading.Lock()
        self._intro_request_thread: threading.Thread | None = None
        feed_cache = config.paths.cache_dir / "release-feeds"
        self._subsplease_client = SubsPleaseClient(
            base_url=self._weekly_rss_url,
            timeout=8.0,
            cache_ttl=300.0,
            history_path=feed_cache / "subsplease.json",
        )
        self._shana_client = ShanaProjectClient(
            feed_url=self._release_feed_url,
            timeout=8.0,
            cache_ttl=600.0,
            history_path=feed_cache / "shana.json",
        )
        missing_schedule_repaired = self.db.repair_missing_cleanup_schedule(
            config.agent.delete_after_watched_hours, library_root=config.library.root_dir
        )
        repaired = self.db.reconcile_watched_cleanup(config.agent.delete_after_watched_hours)
        if missing_schedule_repaired or repaired:
            self.logger.info(
                "EVENT cleanup_schedule.reconciled missing=%s rescheduled=%s delete_after_hours=%s",
                missing_schedule_repaired,
                repaired,
                config.agent.delete_after_watched_hours,
            )
        retry_generation = "0.6.23-sevenzip-app-path"
        if self.db.get_state("subtitle_retry_generation", "") != retry_generation:
            retried = self.db.reset_pending_subtitle_jobs()
            self.db.set_state("subtitle_retry_generation", retry_generation)
            self.logger.info(
                "EVENT subtitle_jobs.retry_generation generation=%s rows=%s",
                retry_generation,
                retried,
            )

    def _record_media_identity(
        self,
        item: LibraryEpisode,
        *,
        source: str,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.identity_resolver.record(
                MediaIdentity(
                    media_id=item.media_id,
                    media_episode=item.media_episode,
                    release_episode=item.release_episode,
                    video_path=item.video_path,
                    torrent_hash=item.torrent_hash,
                    source=source,
                    provenance=dict(provenance or {}),
                )
            )
        except Exception as exc:
            self.logger.warning(
                "FALLBACK step=identity.record media_id=%s episode=%s path=%r error=%r",
                item.media_id,
                item.media_episode,
                str(item.video_path),
                str(exc),
            )

    def _anime_is_fully_ready(self, anime: LibraryAnime) -> bool:
        episodes = self.db.episodes(anime.media_id)
        ready_states = {"ready", "watched"}
        if anime.format == "MOVIE":
            return any(item.state in ready_states for item in episodes)
        if not anime.episodes or anime.episodes < 1:
            return False
        ready_numbers = {
            int(item.episode) for item in episodes if item.episode is not None and item.state in ready_states
        }
        return all(number in ready_numbers for number in range(1, int(anime.episodes) + 1))

    def remember_ready_before_playback(self, video: Path) -> None:
        """Suppress the ready notification for a next episode the user can already play.

        On a personal schedule only an already unlocked next episode counts; a
        locked one still gets its unlock notification later.
        """
        current = self.db.episode_by_path(video)
        if current is None or current.media_id is None or current.media_episode is None:
            return
        next_number = int(current.media_episode) + 1
        ready = next(
            (
                row
                for row in self.db.episodes(current.media_id)
                if row.media_episode == next_number
                and row.state in {"ready", "watched"}
                and row.video_path.is_file()
                and (
                    (row.subtitle_path is not None and row.subtitle_path.is_file())
                    or row.embedded_subtitle_id is not None
                )
            ),
            None,
        )
        if ready is None:
            return
        schedule = self.db.personal_release_schedule(current.media_id)
        if schedule is not None and schedule.enabled:
            item = next((row for row in schedule.items if int(row.episode) == next_number), None)
            if item is not None and not item.unlocked:
                # A batch on a personal schedule has every file on disk, but the
                # next episode is still locked: its unlock is news and must notify.
                return
        key = (
            f"personal_schedule_notification:{schedule.schedule_id}:{schedule.cycle_id}:{next_number}:ready"
            if schedule is not None and schedule.enabled
            else f"ready_notification:episode:{current.media_id}:{next_number}"
        )
        if not self.db.get_state(key, ""):
            self.db.set_state(key, READY_BEFORE_PLAYBACK)

    def _notify_ready_episode(
        self,
        *,
        video: Path,
        media_id: int | None,
        episode: int | None,
    ) -> None:
        episode_key = (
            f"ready_notification:episode:{media_id}:{episode}"
            if media_id is not None and episode is not None
            else f"ready_notification:file:{hashlib.sha256(str(video).encode()).hexdigest()}"
        )

        anime = self.db.get_anime(media_id) if media_id is not None else None
        schedule_loader = getattr(self.db, "personal_release_schedule", None)
        personal_schedule = (
            schedule_loader(int(media_id)) if media_id is not None and callable(schedule_loader) else None
        )
        if anime is not None and episode is not None:
            if personal_schedule is not None and personal_schedule.enabled:
                next_item = next((item for item in personal_schedule.items if item.watched_at is None), None)
                if next_item is None or int(episode) != int(next_item.episode) or not next_item.unlocked:
                    self.logger.info(
                        "SKIP step=notification.ready media_id=%s episode=%s reason=personal_schedule_locked next_episode=%s unlock_at=%s",
                        media_id,
                        episode,
                        int(next_item.episode) if next_item is not None else None,
                        float(next_item.unlock_at_utc) if next_item is not None else None,
                    )
                    return
                episode_key = f"personal_schedule_notification:{personal_schedule.schedule_id}:{personal_schedule.cycle_id}:{int(episode)}:ready"
                marker = self.db.get_state(episode_key, "")
                if marker and ready_marker_is_stale(personal_schedule, int(episode), marker):
                    self.db.delete_state(episode_key)
                    marker = ""
                if marker:
                    return
            else:
                next_episode = int(anime.next_episode)
                if int(episode) != next_episode:
                    self.logger.info(
                        "SKIP step=notification.ready media_id=%s episode=%s "
                        "reason=not_next_unwatched next_episode=%s",
                        media_id,
                        episode,
                        next_episode,
                    )
                    return
        # Choose the dedupe key only after personal-schedule routing. A title may
        # already have an old normal ready-notification key from its first watch;
        # that must not suppress a later scheduled rewatch unlock.
        if self.db.get_state(episode_key, ""):
            return
        scheduled_release = bool(personal_schedule is not None and personal_schedule.enabled)
        # A completed rewatch may already have every file/subtitle locally ready.
        # The personal overlay unlocks one episode at a time, so never collapse it
        # into the legacy "all episodes ready" notification.
        full_ready = bool(not scheduled_release and anime is not None and self._anime_is_fully_ready(anime))
        full_key = (
            f"ready_notification:anime:{anime.media_id}:{anime.episodes or anime.format or 'unknown'}"
            if anime is not None
            else ""
        )
        notify_full = bool(full_ready and full_key and not self.db.get_state(full_key, ""))

        if not self.config.ui.notifications_enabled:
            # A deliberately disabled notification must not appear later merely
            # because the episode is reprocessed after notifications are enabled.
            self.db.set_state(episode_key, "disabled")
            if notify_full and full_key:
                self.db.set_state(full_key, "disabled")
            return

        language = self.config.ui.language
        title = anime.title if anime is not None else video.stem
        if notify_full:
            subtitle = t("notify.animeReady", lang=language)
            if anime is not None and anime.format == "MOVIE":
                message = t("notify.movieReadyMessage", lang=language, title=title)
            else:
                message = t("notify.allEpisodesReadyMessage", lang=language, title=title)
        else:
            subtitle = t("notify.episodeReady", lang=language)
            message = (
                t("notify.episodeReadyMessage", lang=language, title=title, episode=episode)
                if episode is not None
                else t("notify.movieReadyMessage", lang=language, title=title)
            )
        try:
            delivered = send_native_notification(subtitle, message)
        except Exception as exc:
            # Notifications are best-effort UI. A macOS notification backend
            # failure must never roll back an otherwise successful subtitle job.
            delivered = False
            self.logger.warning(
                "FALLBACK step=notification.ready media_id=%s episode=%s error=%r",
                media_id,
                episode,
                str(exc),
            )
        if delivered:
            self.db.set_state(episode_key, "delivered")
            if scheduled_release and personal_schedule is not None and episode is not None:
                # Compatibility mirror for older diagnostics/tests. Routing and
                # dedupe use the cycle-aware key above, so a new cycle is not
                # suppressed by this legacy marker.
                self.db.set_state(
                    f"personal_schedule_notification:{personal_schedule.schedule_id}:{int(episode)}:ready",
                    "delivered",
                )
            if notify_full and full_key:
                self.db.set_state(full_key, "delivered")
        self.logger.info(
            "EVENT notification.ready media_id=%s episode=%s full=%s delivered=%s backend=app_bundle",
            media_id,
            episode,
            notify_full,
            delivered,
        )

    def sync_anilist(self) -> AniListSyncResult:
        """Refresh the Watching/Planning list from AniList (or keep the saved one).

        The result says itself whether AniList answered (``used_cache``), so a
        caller never reads a flag another thread's refresh just changed.
        """
        self._last_missing_episode_rows = 0
        if not self.config.anilist.enabled or not self.config.anilist.access_token:
            self.log("AniList: token is not set; list not refreshed")
            return AniListSyncResult(self.db.anime_list())
        self.db.set_state("anilist_last_attempt_at", str(time.time()))
        client = AniListClient(
            self.config.anilist.endpoint,
            access_token=self.config.anilist.access_token,
        )
        full_list = False
        try:
            try:
                # Watched episodes that could not reach AniList earlier go
                # first, so the refreshed list already contains them.
                flush_pending_statuses(self.db, client, self.logger)
                flush_pending_scores(self.db, client, self.logger)
                flush_pending_progress(self.db, client, self.config, self.logger)
            except Exception as exc:  # noqa: BLE001 - the list refresh still runs
                self.logger.warning("FAIL step=anilist.pending_progress error=%r", exc)
            # Episodes watched (mpv, iPhone) while the list is on its way are not in it.
            list_requested_at = time.time()
            try:
                with timed_step(self.logger, "anilist.library", mode="compact"):
                    items = self._anilist_library(client)
                entries = getattr(client, "last_list_entries", None)
                # Every entry of every list (Completed, Paused, custom lists ...):
                # only then is "missing from the list" a real removal.
                full_list = isinstance(entries, dict)
                if full_list and getattr(client, "last_list_incomplete", False):
                    # An entry came back without its media: "missing" proves nothing.
                    full_list = False
                    self.logger.warning("SKIP step=anilist.list_removal reason=entry_without_media")
                other_entries = dict(entries or {})
                media_of_entries = getattr(client, "last_list_media", None)
                other_media = dict(media_of_entries) if isinstance(media_of_entries, dict) else {}
            except AniListError as exc:
                cached = self.db.anime_list(("CURRENT", "PLANNING"))
                if not cached:
                    raise
                warning = err("anilist.listFromCache")
                self.db.set_state("anilist_last_error", str(exc))
                self.log(f"AniList: {warning}: {exc}")
                self.logger.warning(
                    "FALLBACK step=anilist.library cached=%s error=%r",
                    len(cached),
                    exc,
                )
                return AniListSyncResult(cached, used_cache=True, warning=warning, error=str(exc))

            cached_by_id = {item.media_id: item for item in self.db.anime_list(("CURRENT", "PLANNING"))}
            for item in items:
                previous = cached_by_id.get(item.media_id)
                if previous is not None:
                    # Startup list synchronization is deliberately compact.
                    # Relation trees have a separate multi-day component cache.
                    if not item.relations:
                        item.relations = previous.relations
                    if not item.studio:
                        item.studio = previous.studio
                if item.relations:
                    continue
                cached_graph = self.db.relation_graph_for_media(item.media_id)
                if cached_graph is not None:
                    item.relations = compact_relations_from_graph(
                        cached_graph["graph"],
                        item.media_id,
                    )
        finally:
            client.close()

        # A status set in the window that AniList has not received yet stays local.
        unsent = pending_statuses(self.db)
        for item in items:
            if int(item.media_id) in unsent:
                item.status = unsent[int(item.media_id)]
        unsent_scores = pending_scores(self.db)
        for item in items:
            if int(item.media_id) in unsent_scores:
                item.user_score = unsent_scores[int(item.media_id)]
        # So does an episode watched while AniList refused it: without this the
        # older AniList progress would put the watched episode back to ready.
        unsent_progress = pending_progress_by_media(self.db)
        # An episode counted here after the list was requested (the iPhone's or mpv's
        # watch reached AniList meanwhile): the older list would take it back to ready.
        for media_id, number in self.db.episodes_watched_since(list_requested_at).items():
            if number > unsent_progress.get(media_id, 0):
                unsent_progress[media_id] = number
        for item in items:
            queued = unsent_progress.get(int(item.media_id), 0)
            if queued > int(item.progress or 0):
                item.progress = queued
        for media_id, entry in other_entries.items():
            if int(media_id) in unsent:
                # Titles outside Watching/Planning keep an unsent status too.
                entry["status"] = unsent[int(media_id)]
            queued = unsent_progress.get(int(media_id), 0)
            if queued > int(entry.get("progress") or 0):
                entry["progress"] = queued
        list_entries = {
            int(item.media_id): {
                "status": str(item.status or "").upper(),
                "progress": int(item.progress or 0),
                "episodes": item.episodes,
            }
            for item in items
        }

        def overlay_cached_relations(relations: list[dict[str, Any]]) -> None:
            for relation in relations:
                try:
                    relation_id = int(relation.get("media_id"))
                except (TypeError, ValueError):
                    relation_id = 0
                entry = list_entries.get(relation_id)
                if entry is not None:
                    relation["list_status"] = entry["status"]
                    relation["progress"] = entry["progress"]
                    episodes = entry["episodes"] or relation.get("episodes")
                    relation["watched"] = entry["status"] in {"COMPLETED", "REPEATING"} or bool(
                        episodes and int(entry["progress"] or 0) >= int(episodes)
                    )
                children = relation.get("relations")
                if isinstance(children, list):
                    overlay_cached_relations(children)

        for item in items:
            overlay_cached_relations(item.relations)

        restored = 0
        for item in items:
            reset = self.db.reconcile_anilist_progress(item.media_id, item.progress)
            if reset:
                restored += reset
                self.logger.info(
                    "EVENT anilist.progress_rollback media_id=%s progress=%s episodes_reset=%s",
                    item.media_id,
                    item.progress,
                    reset,
                )
            self.db.upsert_anime(item)
        restored += self._sync_other_list_entries(
            other_entries, {int(item.media_id) for item in items}, media=other_media
        )
        removed = (
            self._mark_removed_on_anilist({int(item.media_id) for item in items} | set(other_entries), unsent)
            if full_list
            else 0
        )
        self.db.set_state("anilist_synced_at", str(time.time()))
        self.db.set_state("anilist_last_error", "")
        # AniList answers again: automatic searches may look episode numbering up again.
        clear_numbering_lookup_failures(self.config)
        self.db.set_state("anilist_last_warning", "")
        self.log(f"AniList: refreshed cards: {len(items)}")
        if removed:
            self.log(f"AniList: titles removed from the AniList list: {removed}")
        if restored:
            self.log(f"AniList: local episodes unwatched: {restored}; returned to ready")
        return AniListSyncResult(items)

    _ANILIST_LIST_STATUSES = frozenset({"CURRENT", "PLANNING", "COMPLETED", "PAUSED", "DROPPED", "REPEATING"})

    _ANILIST_LIST_IDS_KEY = "anilist_list_ids_v1"
    # More removals than this in one refresh look like a glitch, not the user.
    ANILIST_REMOVAL_LIMIT = 5

    def _mark_removed_on_anilist(self, present: set[int], unsent: dict[int, str]) -> int:
        """Titles deleted from the AniList list (website, phone app) become REMOVED here too.

        Watching/Planning entries removed on anilist.co used to stay CURRENT /
        PLANNING locally forever: their episodes kept downloading and Planning
        kept offering premieres. Only a title that was in the previous full list
        of the same account and is missing now counts as removed, so titles
        followed locally before AniList was connected stay. Local-only titles
        (status ""), changes waiting to be sent, an empty list and more than
        ANILIST_REMOVAL_LIMIT removals at once are never applied.
        """
        account = hashlib.sha256(str(self.config.anilist.access_token or "").strip().encode()).hexdigest()
        if not present:
            self.logger.warning("SKIP step=anilist.list_removal reason=empty_list")
            return 0
        try:
            stored = json.loads(self.db.get_state(self._ANILIST_LIST_IDS_KEY, "") or "{}")
        except (TypeError, ValueError):
            stored = {}
        previous: set[int] | None = None
        if (
            isinstance(stored, dict)
            and stored.get("account") == account
            and isinstance(stored.get("ids"), list)
        ):
            previous = {int(value) for value in stored["ids"] if isinstance(value, int)}
        self.db.set_state(
            self._ANILIST_LIST_IDS_KEY,
            json.dumps({"account": account, "ids": sorted(int(value) for value in present)}),
        )
        if previous is None:
            self.logger.info("DONE step=anilist.list_removal reason=first_list entries=%s", len(present))
            return 0
        gone = previous - present
        rows = [
            anime
            for anime in self.db.anime_list()
            if int(anime.media_id) in gone
            and int(anime.media_id) not in unsent
            and (anime.status or "").upper() in self._ANILIST_LIST_STATUSES
        ]
        self._forget_returned_anilist_removals(present, account)
        if len(rows) > self.ANILIST_REMOVAL_LIMIT:
            self.logger.warning(
                "SKIP step=anilist.list_removal reason=too_many count=%s media_ids=%s",
                len(rows),
                sorted(int(anime.media_id) for anime in rows),
            )
            self._hold_anilist_removals(rows, account)
            return 0
        for anime in rows:
            self.db.set_anime_status(int(anime.media_id), "REMOVED")
            self.logger.info(
                "EVENT anilist.removed_on_anilist media_id=%s previous_status=%s title=%r",
                anime.media_id,
                anime.status,
                anime.title,
            )
        return len(rows)

    _ANILIST_REMOVAL_HELD_KEY = "anilist_removal_held_v1"
    _ANILIST_REMOVAL_KEPT_KEY = "anilist_removal_kept_v1"

    def _anilist_removal_json(self, key: str) -> dict[str, Any]:
        try:
            value = json.loads(self.db.get_state(key, "") or "{}")
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def _hold_anilist_removals(self, rows: list[LibraryAnime], account: str) -> None:
        """Too many titles left the list at once: they stay, and the window asks the user.

        Titles the user chose to keep before are not asked about again."""
        kept_state = self._anilist_removal_json(self._ANILIST_REMOVAL_KEPT_KEY)
        kept = (
            {int(value) for value in kept_state.get("ids") or [] if isinstance(value, int)}
            if kept_state.get("account") == account
            else set()
        )
        held = self._anilist_removal_json(self._ANILIST_REMOVAL_HELD_KEY)
        titles = {
            int(item["media_id"]): str(item.get("title") or "")
            for item in (held.get("titles") or [] if held.get("account") == account else [])
            if isinstance(item, dict) and isinstance(item.get("media_id"), int)
        }
        for anime in rows:
            if int(anime.media_id) not in kept:
                titles[int(anime.media_id)] = str(anime.title or "")
        if not titles:
            return
        self.db.set_state(
            self._ANILIST_REMOVAL_HELD_KEY,
            json.dumps(
                {
                    "account": account,
                    "titles": [{"media_id": key, "title": value} for key, value in sorted(titles.items())],
                    "at": time.time(),
                },
                ensure_ascii=False,
            ),
        )
        self.logger.warning(
            "EVENT anilist.list_removal_held count=%s titles=%s (kept until the user chooses Remove from Pudge or Keep)",
            len(titles),
            "; ".join(f"{key} {value!r}" for key, value in sorted(titles.items())),
        )
        self.log(
            "AniList: "
            + str(len(titles))
            + " titles left the AniList list at once; Pudge keeps them until you choose: "
            + ", ".join(value or str(key) for key, value in sorted(titles.items()))
        )

    def _forget_returned_anilist_removals(self, present: set[int], account: str) -> None:
        """Held titles back on the list (or whose account changed) are not asked about any more."""
        held = self._anilist_removal_json(self._ANILIST_REMOVAL_HELD_KEY)
        if not held:
            return
        titles = held.get("titles") or [] if held.get("account") == account else []
        remaining = [
            item
            for item in titles
            if isinstance(item, dict)
            and isinstance(item.get("media_id"), int)
            and int(item["media_id"]) not in present
        ]
        if remaining != titles or held.get("account") != account:
            self.db.set_state(
                self._ANILIST_REMOVAL_HELD_KEY,
                json.dumps({**held, "titles": remaining}, ensure_ascii=False) if remaining else "",
            )

    def anilist_removal_notice(self) -> dict[str, Any]:
        """The titles held back from a mass removal, for the window ({} when none)."""
        held = self._anilist_removal_json(self._ANILIST_REMOVAL_HELD_KEY)
        account = hashlib.sha256(str(self.config.anilist.access_token or "").strip().encode()).hexdigest()
        if held.get("account") != account:
            return {}
        titles = []
        for item in held.get("titles") or []:
            if not isinstance(item, dict) or not isinstance(item.get("media_id"), int):
                continue
            anime = self.db.get_anime(int(item["media_id"]))
            if anime is None or (anime.status or "").upper() not in self._ANILIST_LIST_STATUSES:
                continue  # removed or deleted meanwhile
            titles.append(
                {"media_id": int(anime.media_id), "title": str(anime.title or item.get("title") or "")}
            )
        if not titles:
            return {}
        notice_id = hashlib.sha256(json.dumps([item["media_id"] for item in titles]).encode()).hexdigest()[
            :16
        ]
        return {"id": notice_id, "titles": titles}

    def resolve_anilist_removal_notice(self, action: str, notice_id: str = "") -> dict[str, Any]:
        """``remove``: the held titles become removed (as a removal on AniList does); ``keep``:
        they stay and are never asked about again."""
        action = str(action or "").strip().casefold()
        if action not in {"remove", "keep"}:
            raise ManagerError(err("manager.anilistRemovalUnknownAction"))
        notice = self.anilist_removal_notice()
        if not notice:
            self.db.set_state(self._ANILIST_REMOVAL_HELD_KEY, "")
            return {"ok": True, "changed": 0}
        if notice_id and str(notice_id) != notice["id"]:
            # The list changed since the window asked: it asks again about what is held now.
            return {"ok": False, "changed": 0, "stale": True}
        ids = [int(item["media_id"]) for item in notice["titles"]]
        account = hashlib.sha256(str(self.config.anilist.access_token or "").strip().encode()).hexdigest()
        if action == "remove":
            for item in notice["titles"]:
                anime = self.db.get_anime(int(item["media_id"]))
                self.db.set_anime_status(int(item["media_id"]), "REMOVED")
                self.logger.info(
                    "EVENT anilist.removed_on_anilist media_id=%s previous_status=%s title=%r source=user_confirmed",
                    item["media_id"],
                    anime.status if anime is not None else "",
                    item["title"],
                )
        else:
            kept_state = self._anilist_removal_json(self._ANILIST_REMOVAL_KEPT_KEY)
            kept = (
                {int(value) for value in kept_state.get("ids") or [] if isinstance(value, int)}
                if kept_state.get("account") == account
                else set()
            )
            self.db.set_state(
                self._ANILIST_REMOVAL_KEPT_KEY,
                json.dumps({"account": account, "ids": sorted(kept | set(ids))}),
            )
            self.logger.info("EVENT anilist.list_removal_kept media_ids=%s", ids)
        self.db.set_state(self._ANILIST_REMOVAL_HELD_KEY, "")
        return {"ok": True, "changed": len(ids), "action": action}

    def _sync_other_list_entries(
        self,
        entries: dict[int, dict[str, Any]],
        refreshed: set[int],
        *,
        media: dict[int, LibraryAnime] | None = None,
    ) -> int:
        """Keep list status/progress of titles outside Watching/Planning current.

        Completed, Repeating, Paused or Dropped titles can still have local
        episodes. Their cleanup depends on the AniList list state, so a title
        moved to Repeating (or whose progress was lowered) must not keep an
        old Completed row locally. A local row made before its title was known
        (a placeholder: no AniList titles) gets its metadata from ``media``, the
        same list answer. Returns how many watched episodes were reset.
        """
        restored = 0
        now = time.time()
        for media_id, entry in entries.items():
            media_id = int(media_id)
            if media_id in refreshed:
                continue
            local = self.db.get_anime(media_id)
            if local is None:
                continue
            metadata = (media or {}).get(media_id)
            if metadata is not None and not local.titles and metadata.titles:
                local = self._fill_placeholder_metadata(local, metadata, source="anilist.list")
            status = str(entry.get("status") or "").upper()
            progress = int(entry.get("progress") or 0)
            if status == (local.status or "").upper() and progress == int(local.progress or 0):
                continue
            reset = self.db.reconcile_anilist_progress(media_id, progress)
            restored += reset
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE anime SET status=?,progress=?,updated_at=? WHERE media_id=?",
                    (status or local.status, progress, now, media_id),
                )
                self.db.note_anilist_progress(media_id, progress, now=now, conn=conn)
            self.logger.info(
                "EVENT anilist.list_entry_sync media_id=%s status=%s progress=%s previous_status=%s previous_progress=%s episodes_reset=%s",
                media_id,
                status,
                progress,
                local.status,
                local.progress,
                reset,
            )
        return restored

    ANILIST_WORKER_TIMEOUT_SECONDS = 180.0

    def _anilist_library(self, client: Any) -> list[LibraryAnime]:
        """The AniList list. A short-lived worker asks AniList and parses the
        answer (its peak, ~100 MB for a long list, goes with the process); the window gets
        the compact rows and the same ``last_list_*`` facts on ``client``. A worker that fails
        (not an AniList refusal) falls back to asking here."""

        def inline() -> list[LibraryAnime]:
            compact_library = getattr(client, "library_compact", None)
            return compact_library() if callable(compact_library) else client.library()

        from . import low_ram

        if not self._low_ram_worker_allowed():
            return inline()
        answer = low_ram.worker_or_inline(
            "anilist_library",
            {
                "config_path": str(self.config.config_path),
                "endpoint": str(self.config.anilist.endpoint),
                "access_token": str(self.config.anilist.access_token),
                # Only rows made before their title was known use the list's media (every
                # entry of every list as full rows was MBs of JSON parsed here).
                "media_for": self._placeholder_media_ids(),
            },
            lambda: None,
            use_worker=True,
            logger=self.logger,
            timeout=self.ANILIST_WORKER_TIMEOUT_SECONDS,
        )
        if not isinstance(answer, dict):
            return inline()
        refused = answer.get("anilist_error")
        if isinstance(refused, dict):
            raise self._anilist_error_from_worker(refused)
        try:
            items = [LibraryAnime(**row) for row in answer.get("items") or []]
            entries = answer.get("entries")
            media = answer.get("media")
            client.last_list_entries = (
                {int(key): value for key, value in entries.items()} if isinstance(entries, dict) else None
            )
            client.last_list_media = (
                {int(key): LibraryAnime(**value) for key, value in media.items()}
                if isinstance(media, dict)
                else {}
            )
            client.last_list_incomplete = bool(answer.get("incomplete"))
        except (TypeError, ValueError) as exc:
            self.logger.warning("FALLBACK step=anilist.library_worker error=%r", str(exc)[:200])
            return inline()
        return items

    def _placeholder_media_ids(self) -> list[int] | None:
        """Local rows without AniList titles: _sync_other_list_entries fills them from the list.

        None when they could not be read: the worker then sends every entry's media (an
        empty list would have kept none of it)."""
        try:
            return sorted(int(anime.media_id) for anime in self.db.anime_list() if not anime.titles)
        except Exception:  # noqa: BLE001 - unknown: the worker sends every entry's media
            return None

    @staticmethod
    def _anilist_error_from_worker(refused: dict[str, Any]) -> AniListError:
        """The AniList refusal a worker met, as the error asking here would have raised."""
        message = str(refused.get("message") or "AniList error")
        if int(refused.get("status_code") or 0):
            return AniListHTTPError(
                message,
                status_code=int(refused["status_code"]),
                detail=str(refused.get("detail") or ""),
                refusal=bool(refused.get("refusal")),
            )
        return AniListError(message)

    def _low_ram_worker_allowed(self) -> bool:
        """Not already a worker (both modes) and the worker can read the config."""
        from . import low_ram

        if not low_ram.worker_allowed(self.config):
            return False
        try:
            return Path(self.config.config_path).is_file()
        except (TypeError, OSError):
            return False

    def _anilist_worker(self, task: str, extra: dict[str, Any] | None = None) -> Any:
        """``task`` of pudge.low_ram_worker with the window's AniList request mode; None when
        the worker failed (the caller does the work here). A refusal AniList gave the worker
        is raised here, so it is not asked a second time."""
        from . import low_ram
        from .providers.anilist import request_modes

        payload: dict[str, Any] = {
            "config_path": str(self.config.config_path),
            "anilist": {
                "enabled": bool(self.config.anilist.enabled),
                "endpoint": str(self.config.anilist.endpoint),
                "access_token": str(self.config.anilist.access_token),
            },
            **request_modes(),
            **(extra or {}),
        }
        answer = low_ram.worker_or_inline(
            task,
            payload,
            lambda: None,
            use_worker=True,
            logger=self.logger,
            timeout=self.ANILIST_REFRESH_WORKER_TIMEOUT_SECONDS,
        )
        if not isinstance(answer, dict):
            return None
        refused = answer.get("anilist_error")
        if isinstance(refused, dict):
            raise self._anilist_error_from_worker(refused)
        return answer

    # The list, one relation graph (paced bulk requests) and the cover files.
    ANILIST_REFRESH_WORKER_TIMEOUT_SECONDS = 300.0

    def refresh_anilist_cache(self) -> dict[str, object]:
        """The AniList list, covers, one due relation graph and off-list metadata.

        All of it runs in a short-lived worker (``anilist_refresh``): the
        GraphQL answers, the parsed rows and the transient Python objects (≈ 100–200 MB at
        launch) go with that process instead of leaving the window's heap fragmented. The
        worker writes the same database; a failed worker falls back to the work here."""
        if self._low_ram_worker_allowed():
            with timed_step(self.logger, "anilist.refresh_cache", via="worker"):
                answer = self._anilist_worker("anilist_refresh")
            stats = answer.get("stats") if isinstance(answer, dict) else None
            if isinstance(stats, dict):
                return stats
        return self.refresh_anilist_cache_inline()

    def refresh_anilist_cache_inline(self, *, step: str = "anilist.refresh_cache") -> dict[str, object]:
        with timed_step(self.logger, step):
            items = self.sync_anilist()
            used_cache = bool(getattr(items, "used_cache", False))
            with timed_step(self.logger, "covers.prefetch", anime_count=len(items)):
                covers = self.prefetch_covers(("CURRENT", "PLANNING"))
            # AniList did not answer the list: its relation graphs would fail too.
            relation_graphs = 0 if used_cache else self.refresh_due_relation_graphs(limit=1)
            try:
                self.refresh_off_list_entry_metadata()
            except Exception as exc:  # noqa: BLE001 - covers of split entries never block the sync
                self.logger.warning("FAIL step=anime.off_list_metadata error=%r", str(exc)[:200])
        return {
            "anime": len(items),
            "covers": covers,
            "relation_graphs": relation_graphs,
            "cached": used_cache,
            "warning": str(getattr(items, "warning", "") or ""),
            # AniList's own reason (HTTP status and message) when the saved list is shown.
            "error": str(getattr(items, "error", "") or ""),
        }

    def anilist_refresh_due(self, *, now: float | None = None) -> bool:
        if not self.config.anilist.enabled or not self.config.anilist.access_token.strip():
            return False
        current = time.time() if now is None else float(now)
        try:
            last_sync = float(self.db.get_state("anilist_synced_at", "0") or 0)
        except ValueError:
            last_sync = 0.0
        try:
            last_attempt = float(self.db.get_state("anilist_last_attempt_at", "0") or 0)
        except ValueError:
            last_attempt = 0.0
        interval = max(5, self.config.agent.anilist_refresh_minutes) * 60
        return current - max(last_sync, last_attempt) >= interval

    def refresh_anilist_if_due(self, *, now: float | None = None) -> int:
        if not self.anilist_refresh_due(now=now):
            return 0
        if getattr(self, "_owns_database", False) and self._low_ram_worker_allowed():
            # The window (both modes): the periodic list refresh and its relation graph in a
            # worker (the agent is a short-lived process of its own already).
            try:
                with timed_step(self.logger, "anilist.periodic_refresh", via="worker"):
                    answer = self._anilist_worker("anilist_periodic", {"now": now})
            except Exception as exc:  # an AniList refusal: the same handling as here
                self.log(f"AniList: background refresh failed: {exc}")
                self.logger.warning("RETRY step=anilist.periodic_refresh error=%r", exc)
                return 0
            if isinstance(answer, dict) and "count" in answer:
                return int(answer.get("count") or 0)
        return self.refresh_anilist_if_due_inline(now=now)

    def refresh_anilist_if_due_inline(self, *, now: float | None = None) -> int:
        """The periodic refresh here (due already checked by the caller)."""
        try:
            with timed_step(
                self.logger,
                "anilist.periodic_refresh",
                interval_minutes=self.config.agent.anilist_refresh_minutes,
            ):
                items = self.sync_anilist()
        except Exception as exc:
            # A temporary AniList failure must not block torrent/subtitle work.
            self.log(f"AniList: background refresh failed: {exc}")
            self.logger.warning("RETRY step=anilist.periodic_refresh error=%r", exc)
            return 0
        if not getattr(items, "used_cache", False):
            self.refresh_due_relation_graphs(limit=1, now=now)
        return len(items)

    @staticmethod
    def _fallback_relation_graph(anime: LibraryAnime) -> dict[str, Any]:
        root_id = int(anime.media_id)
        root = {
            "media_id": root_id,
            "title": anime.title,
            "site_url": anime.site_url,
            "format": anime.format,
            "season_year": anime.season_year,
            "start_date": anime.start_date,
            "studio": anime.studio,
            "episodes": anime.episodes,
            "cover_url": anime.cover_url,
            "media_status": anime.media_status or "",
            "list_status": anime.status,
            "progress": anime.progress,
            "watched": anime.status in {"COMPLETED", "REPEATING"}
            or bool(anime.episodes and anime.progress >= anime.episodes),
        }
        nodes: dict[int, dict[str, Any]] = {root_id: root}
        edges: list[dict[str, Any]] = []
        edge_keys: set[tuple[int, int, str]] = set()

        def visit(parent_id: int, items: list[dict[str, Any]]) -> None:
            for item in items:
                try:
                    child_id = int(item.get("media_id"))
                except (TypeError, ValueError):
                    continue
                node = dict(item)
                children = node.pop("relations", None)
                relation_type = str(node.pop("relation_type", "OTHER") or "OTHER").upper()
                if relation_type in {"RELATED", "OTHER", "CHARACTER", "SHARED_CHARACTERS"}:
                    continue
                nodes.setdefault(child_id, node)
                if relation_type == "PREQUEL":
                    source_id, target_id, normalized = child_id, parent_id, "SEQUEL"
                elif relation_type == "SEQUEL":
                    source_id, target_id, normalized = parent_id, child_id, "SEQUEL"
                else:
                    source_id, target_id = sorted((parent_id, child_id))
                    normalized = relation_type
                key = (source_id, target_id, normalized)
                if key not in edge_keys:
                    edge_keys.add(key)
                    edges.append(
                        {
                            "source": source_id,
                            "target": target_id,
                            "relation_type": normalized,
                        }
                    )
                if isinstance(children, list):
                    visit(child_id, children)

        visit(root_id, anime.relations)
        return {
            "root_id": root_id,
            "nodes": list(nodes.values()),
            "edges": edges,
            "truncated": False,
            "partial": True,
        }

    def _overlay_relation_graph_local_state(self, graph: dict[str, Any]) -> None:
        anime_by_id = {item.media_id: item for item in self.db.anime_list()}
        for node in graph.get("nodes", []):
            if not isinstance(node, dict):
                continue
            try:
                media_id = int(node.get("media_id"))
            except (TypeError, ValueError):
                continue
            anime = anime_by_id.get(media_id)
            if anime is None:
                continue
            node["list_status"] = anime.status
            node["progress"] = anime.progress
            node["watched"] = anime.status in {"COMPLETED", "REPEATING"} or bool(
                anime.episodes and anime.progress >= anime.episodes
            )

    def _save_relation_graph(
        self,
        graph: dict[str, Any],
        *,
        refreshed_at: float,
        preferred_graph_id: str = "",
        retry_soon: bool = False,
    ) -> tuple[str, float]:
        node_ids = sorted(
            {
                int(node["media_id"])
                for node in graph.get("nodes", [])
                if isinstance(node, dict) and node.get("media_id") is not None
            }
        )
        if not node_ids:
            raise ManagerError(err("manager.relationGraphEmpty"))
        graph_id = str(preferred_graph_id or f"component-{node_ids[0]}")
        next_refresh_at = (
            relation_retry_at(refreshed_at)
            if retry_soon
            else next_relation_refresh_at(graph_id, refreshed_at)
        )
        graph_id = self.db.store_relation_graph(
            graph,
            refreshed_at=refreshed_at,
            next_refresh_at=next_refresh_at,
            preferred_graph_id=graph_id,
        )
        relation_updates = {media_id: compact_relations_from_graph(graph, media_id) for media_id in node_ids}
        updated = self.db.update_anime_relations(relation_updates)
        self.logger.info(
            "CACHE step=anilist.relation_graph graph_id=%s nodes=%s anime_relations=%s "
            "next_refresh_at=%s partial=%s",
            graph_id,
            len(node_ids),
            updated,
            int(next_refresh_at),
            bool(graph.get("partial")),
        )
        return graph_id, next_refresh_at

    def relation_graph(self, media_id: int, *, force_refresh: bool = False) -> dict[str, Any]:
        media_id = int(media_id)
        cached = self.db.relation_graph_for_media(media_id)
        if cached is not None and not force_refresh:
            graph = dict(cached["graph"])
            self._overlay_relation_graph_local_state(graph)
            return graph_for_root(
                graph,
                media_id,
                refreshed_at=float(cached["refreshed_at"]),
                next_refresh_at=float(cached["next_refresh_at"]),
                graph_id=str(cached["graph_id"]),
            )

        client: AniListClient | None = None
        now = time.time()
        preferred_graph_id = str(cached["graph_id"]) if cached is not None else ""
        try:
            client = AniListClient(
                self.config.anilist.endpoint,
                access_token=self.config.anilist.access_token,
            )
            with timed_step(
                self.logger,
                "anilist.relation_graph",
                media_id=media_id,
                force=force_refresh,
            ):
                graph = client.full_relation_graph(media_id)
        except Exception as exc:
            if cached is not None:
                self.db.defer_relation_graph(preferred_graph_id, relation_retry_at(now))
                graph = dict(cached["graph"])
                graph["warning"] = str(exc)
                graph["refresh_failed"] = True
                self._overlay_relation_graph_local_state(graph)
                self.logger.warning(
                    "FALLBACK step=anilist.relation_graph_cache media_id=%s graph_id=%s error=%r",
                    media_id,
                    preferred_graph_id,
                    exc,
                )
                return graph_for_root(
                    graph,
                    media_id,
                    refreshed_at=float(cached["refreshed_at"]),
                    next_refresh_at=relation_retry_at(now),
                    graph_id=preferred_graph_id,
                )
            anime = self.db.get_anime(media_id)
            if anime is None:
                raise
            graph = self._fallback_relation_graph(anime)
            graph["warning"] = str(exc)
            graph["refresh_failed"] = True
            graph_id, next_refresh_at = self._save_relation_graph(
                graph,
                refreshed_at=now,
                retry_soon=True,
            )
            self.logger.warning(
                "FALLBACK step=anilist.relation_graph media_id=%s graph_id=%s error=%r",
                media_id,
                graph_id,
                exc,
            )
            return graph_for_root(
                graph,
                media_id,
                refreshed_at=now,
                next_refresh_at=next_refresh_at,
                graph_id=graph_id,
            )
        finally:
            if client is not None:
                client.close()

        graph.pop("warning", None)
        graph.pop("refresh_failed", None)
        self._overlay_relation_graph_local_state(graph)
        graph_id, next_refresh_at = self._save_relation_graph(
            graph,
            refreshed_at=now,
            preferred_graph_id=preferred_graph_id,
        )
        return graph_for_root(
            graph,
            media_id,
            refreshed_at=now,
            next_refresh_at=next_refresh_at,
            graph_id=graph_id,
        )

    def cached_relation_graphs(self, media_ids: list[int]) -> dict[str, Any]:
        """Return one shared cached graph per franchise for immediate UI use."""
        graphs: dict[str, dict[str, Any]] = {}
        media_to_graph: dict[str, str] = {}
        for item in self.db.relation_graph_cache(media_ids):
            graph_id = str(item["graph_id"])
            graph = dict(item["graph"])
            self._overlay_relation_graph_local_state(graph)
            graphs[graph_id] = {
                "graph": graph,
                "refreshed_at": float(item["refreshed_at"]),
                "next_refresh_at": float(item["next_refresh_at"]),
            }
            for media_id in item["members"]:
                media_to_graph[str(int(media_id))] = graph_id
        return {"graphs": graphs, "media_to_graph": media_to_graph}

    def refresh_due_relation_graphs(
        self,
        *,
        limit: int = 1,
        now: float | None = None,
    ) -> int:
        if not self.config.anilist.enabled or not self.config.anilist.access_token.strip():
            return 0
        due = self.db.due_relation_graphs(now=now, limit=limit)
        refreshed = 0
        # Background bulk work: paced under AniList's degraded-mode limit.
        with bulk_requests():
            for item in due:
                graph = self.relation_graph(int(item["media_id"]), force_refresh=True)
                if not graph.get("refresh_failed"):
                    refreshed += 1
        return refreshed

    @staticmethod
    def _download_content_path(item) -> Path | None:
        value = str(item.content_path or "").strip()
        if value:
            return Path(value).expanduser().resolve()
        save_path = str(item.save_path or "").strip()
        name = str(item.name or "").strip()
        if save_path and name:
            return (Path(save_path).expanduser() / name).resolve()
        return None

    @staticmethod
    def _download_is_complete(item: DownloadItem) -> bool:
        # state=complete is the durable terminal state. It must win over stale
        # aria2 counters/flags left in raw. Active aria2 seeding still uses the
        # stricter verifier/error/byte-count gates.
        if float(item.progress or 0.0) < 0.999:
            return False

        state = str(item.state or "").casefold()
        if state == "complete":
            return True

        raw = getattr(item, "raw", {}) or {}
        if str(raw.get("backend") or "").casefold() != "aria2":
            # qBittorrent rounds: a 56 GB batch at 99.91 % (52 MiB left) is still
            # downloading. Bytes left decide; without them only a settled state.
            try:
                left = int(raw["amount_left"]) if raw.get("amount_left") is not None else None
            except (TypeError, ValueError):
                left = None
            if left is not None:
                return left <= 0
            return (
                float(item.progress or 0.0) >= 1.0
                or state.endswith("up")
                or state in {"uploading", "seeding"}
            )
        if bool(raw.get("verifying")):
            return False
        if str(raw.get("error_code") or "").strip():
            return False

        total = max(0, int(raw.get("total_size") or 0))
        downloaded = max(0, int(raw.get("downloaded") or 0))
        return total > 0 and downloaded >= total

    def _repair_unreadable_orphaned_download(
        self,
        video: Path,
        *,
        media_id: int | None,
        episode: int | None,
        attempts: int,
        probe_error: str,
    ) -> bool:
        """Quarantine a false aria2 orphan completion and restart the download.

        aria2 can forget a task row while our ownership metadata survives.  The
        orphan recovery path used to trust any matching file on disk and mark it
        100% verified by file size alone.  A sparse/preallocated or abandoned file
        could therefore become a permanent ``complete`` download even though
        ffprobe rejects byte zero as an invalid Matroska header.

        Only managed aria2 orphan rows are repaired here, and only after repeated
        probe failures.  The bad payload is renamed in-place instead of deleted,
        then normal release search starts a clean replacement.
        """
        if int(attempts or 0) < 2:
            return False
        error = str(probe_error or "")
        low_error = error.casefold()
        if not (
            "ebml header parsing failed" in low_error
            or "invalid data found when processing input" in low_error
        ):
            return False

        resolved = Path(video).expanduser().resolve()
        target: DownloadItem | None = None
        for item in self.db.downloads():
            if media_id is not None and item.media_id != int(media_id):
                continue
            if episode is not None:
                item_episode = item.media_episode if item.media_episode is not None else item.episode
                if item_episode not in {None, int(episode)} and not item.is_batch:
                    continue
            content = self._download_content_path(item)
            if content is None or content != resolved:
                continue
            raw = getattr(item, "raw", {}) or {}
            if (
                self._download_is_complete(item)
                and str(raw.get("backend") or "").casefold() == "aria2"
                and bool(raw.get("orphaned_metadata"))
            ):
                target = item
                break
        if target is None:
            return False

        stamp = int(time.time())
        quarantine = resolved.with_name(resolved.name + f".pudge-corrupt-{stamp}")
        try:
            resolved.rename(quarantine)
        except OSError as exc:
            self.logger.warning(
                "WARN step=download.corrupt_orphan_quarantine media_id=%s episode=%s "
                "hash=%s video=%s error=%r",
                media_id,
                episode,
                target.torrent_hash,
                resolved.name,
                str(exc),
            )
            return False

        try:
            client = self.qbt_client()
            try:
                client.delete(target.torrent_hash, delete_files=False)
            finally:
                client.close()
        except Exception as exc:
            self.logger.warning(
                "WARN step=download.corrupt_orphan_forget hash=%s error=%r",
                target.torrent_hash,
                str(exc),
            )

        self.db.delete_subtitle_job(resolved)
        self.db.delete_episode_record(resolved)
        self.db.delete_torrent_records(target.torrent_hash)
        # A quarantined payload is no longer a completed logical request.  The
        # old intent used to survive as state=complete, so Retry saw neither a
        # local file nor an active torrent but still refused to start selection.
        try:
            self.download_intents.clear(int(media_id), episode, False)
        except (AttributeError, TypeError, ValueError):
            pass
        self.logger.warning(
            "REPAIR step=download.corrupt_orphan media_id=%s episode=%s hash=%s "
            "old=%s quarantine=%s attempts=%s",
            media_id,
            episode,
            target.torrent_hash,
            resolved.name,
            quarantine.name,
            attempts,
        )

        if media_id is None:
            return True
        if not self.downloads_enabled():
            try:
                self.download_intents.update(
                    int(media_id),
                    episode,
                    False,
                    state="waiting",
                    backend="aria2",
                    detail="Corrupt completed download removed; waiting for torrents",
                )
            except (AttributeError, TypeError, ValueError):
                pass
            return True
        try:
            replacement = self.search_and_add_best(
                int(media_id),
                episode=(int(episode) if episode is not None else None),
                batch=False,
                automatic=True,
            )
        except Exception as exc:
            self.logger.warning(
                "RETRY step=download.corrupt_orphan_redownload media_id=%s episode=%s error=%r",
                media_id,
                episode,
                str(exc),
            )
            return True
        self.logger.info(
            "START step=download.corrupt_orphan_redownload media_id=%s episode=%s selected=%r",
            media_id,
            episode,
            replacement.title if replacement is not None else "",
        )
        return True

    @classmethod
    def _completed_download_video_files(
        cls, item: DownloadItem, *, walk_save_path: bool = True
    ) -> list[Path]:
        """Resolve videos owned by one completed download without stealing siblings.

        A historical single-episode aria2 row can lose ``content_path`` while its
        ``save_path`` still points at the whole series directory.  Scanning every
        video below that directory lets an old episode claim a newer sibling.
        Batch rows may own many files; single-episode rows must resolve to one
        unambiguous episode only. ``walk_save_path=False`` (the foreground poll,
        WEBFLOW-8) looks only at the download's own entry, never the whole folder.
        """
        content_value = str(item.content_path or "").strip()
        content = Path(content_value).expanduser() if content_value else None
        if not walk_save_path:
            if content is None and str(item.save_path or "").strip() and str(item.name or "").strip():
                content = Path(str(item.save_path).strip()).expanduser() / str(item.name).strip()
            if content is None or not (content.is_file() or content.is_dir()):
                return []
        if content is not None and content.is_file():
            resolved = content.resolve()
            if item.is_batch:
                return [resolved]
            wanted_release = item.release_episode
            if wanted_release is None:
                wanted_release = parsed_release_episode(str(item.name or ""))
            if wanted_release is not None and not cls._file_matches_release(
                resolved.name, int(wanted_release), item
            ):
                return []
            return [resolved]

        root: Path | None = None
        if content is not None and content.is_dir():
            root = content
        else:
            save_value = str(item.save_path or "").strip()
            save = Path(save_value).expanduser() if save_value else None
            if save is not None and save.is_dir():
                root = save
        if root is None:
            return []

        try:
            candidates = [
                path.resolve()
                for path in root.rglob("*")
                if path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS
            ]
        except OSError:
            return []
        if item.is_batch:
            return candidates

        wanted_release = item.release_episode
        if wanted_release is None:
            wanted_release = parsed_release_episode(str(item.name or ""))
        if wanted_release is not None:
            matching = [
                path for path in candidates if cls._file_matches_release(path.name, int(wanted_release), item)
            ]
            # "01-02" may be one file or two files (audit 17А).
            return matching if 1 <= len(matching) <= 1 + cls._download_span_length(item) else []

        # With no episode identity at all, one exact candidate is the only
        # unambiguous fallback. Never let "there is only one file left" make a
        # known episode claim a newer sibling after its own watched file was
        # deleted.
        return candidates if len(candidates) == 1 else []

    @staticmethod
    def _download_span_length(item: DownloadItem) -> int:
        end, first = getattr(item, "media_episode_end", None), item.media_episode
        if (
            end is None
            and first is not None
            and not item.is_batch
            and double_episode_span(str(item.name or ""))
        ):
            return 1
        return max(0, int(end) - int(first)) if end is not None and first is not None else 0

    @classmethod
    def _file_matches_release(cls, name: str, wanted_release: int, item: DownloadItem) -> bool:
        """The file is the requested episode, or part of a two-episode download (audit 17А)."""
        single = parsed_release_episode(name)
        if single is not None:
            return wanted_release <= int(single) <= wanted_release + cls._download_span_length(item)
        double = double_episode_span(name)
        return double is not None and double[0] == wanted_release

    @classmethod
    def _stale_aria2_zero_shell_local_file(cls, item: DownloadItem) -> Path | None:
        """Return an authoritative local file for an already-corrupted aria2 row.

        Older syncs could overwrite a completed DB row with aria2's metadata-only
        ``paused, 0/0`` shell.  At that point there is no durable ``complete``
        flag left to preserve. Recover only a single-file download whose exact
        content path still exists, whose filename matches the managed episode,
        and which has no aria2 control sidecar (an actual in-progress transfer
        keeps that sidecar).
        """
        if item.is_batch or cls._download_is_complete(item):
            return None
        raw = dict(item.raw or {})
        if str(raw.get("backend") or "").casefold() != "aria2":
            return None
        if str(item.state or "").casefold() not in {"paused", "stopped", "waiting"}:
            return None
        if float(item.progress or 0.0) > 0.001:
            return None
        if max(0, int(raw.get("total_size") or 0)) != 0:
            return None
        if max(0, int(raw.get("downloaded") or 0)) != 0:
            return None

        content_value = str(item.content_path or "").strip()
        if not content_value:
            return None
        content = Path(content_value).expanduser()
        if not content.is_file() or content.suffix.casefold() not in VIDEO_EXTENSIONS:
            return None
        try:
            if int(content.stat().st_size) <= 0:
                return None
        except OSError:
            return None
        if Path(str(content) + ".aria2").exists():
            return None

        name = str(item.name or "").strip()
        if name and Path(name).name.casefold() != content.name.casefold():
            return None
        wanted_release = item.release_episode
        if wanted_release is None:
            wanted_release = parsed_release_episode(name)
        if wanted_release is not None and not cls._file_matches_release(
            content.name, int(wanted_release), item
        ):
            return None
        return content.resolve()

    @classmethod
    def _recover_stale_aria2_zero_shell(cls, item: DownloadItem) -> DownloadItem | None:
        content = cls._stale_aria2_zero_shell_local_file(item)
        if content is None:
            return None
        try:
            size = max(1, int(content.stat().st_size))
        except OSError:
            return None
        raw = dict(item.raw or {})
        raw.update(
            {
                "total_size": size,
                "downloaded": size,
                "download_speed": 0,
                "aria2_status": "complete",
                "verifying": False,
                "recovered_stale_zero_local": True,
            }
        )
        return DownloadItem(
            torrent_hash=item.torrent_hash,
            name=item.name or content.name,
            state="complete",
            progress=1.0,
            save_path=item.save_path or str(content.parent),
            content_path=str(content),
            media_id=item.media_id,
            episode=item.media_episode,
            media_episode=item.media_episode,
            release_episode=item.release_episode,
            is_batch=False,
            added_on=item.added_on,
            completed_on=item.completed_on or int(time.time()),
            raw=raw,
        )

    @classmethod
    def _stale_aria2_row_shadowed_by_completed_local(
        cls, live: DownloadItem, stored: DownloadItem | None
    ) -> bool:
        """Keep durable completion when aria2 resurrects a zero-byte stale shell."""
        if stored is None or not cls._download_is_complete(stored):
            return False
        stored_path = cls._download_content_path(stored)
        if stored_path is None or not stored_path.is_file():
            return False
        raw = dict(live.raw or {})
        if str(raw.get("backend") or "").casefold() != "aria2":
            return False
        if cls._download_is_complete(live):
            return False
        state = str(live.state or "").casefold()
        total = max(0, int(raw.get("total_size") or 0))
        downloaded = max(0, int(raw.get("downloaded") or 0))
        return bool(
            state in {"active", "waiting", "paused", "stopped"}
            and float(live.progress or 0.0) <= 0.001
            and total == 0
            and downloaded == 0
        )

    def incomplete_download_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        shared: set[Path] = set()  # folders other files live in too: never "incomplete content"
        for root in self._library_scan_roots():
            shared.add(root)
            shared.update(root.parents)
        for item in self.db.downloads():
            if self._download_is_complete(item):
                continue
            path = self._download_content_path(item)
            if path is None:
                continue
            save = str(item.save_path or "").strip()
            try:
                save_path = Path(save).expanduser().resolve() if save else None
            except (OSError, RuntimeError):
                save_path = None
            if path in shared or (save_path is not None and path == save_path):
                # A season pack without its own folder reports the title's folder (where every
                # other episode lives) as its content: every row there was deleted and the title
                # hidden until the pack finished, watched episodes included.
                continue
            paths.append(path)
        return tuple(dict.fromkeys(paths))

    @staticmethod
    def _path_within(path: Path, roots: tuple[Path, ...]) -> bool:
        # Compare path components, not ``root in resolved.parents``: that built a Path for every
        # ancestor of every root on every call (a Home build asks once per episode with every
        # unfinished download as a root, ~3 s of a 5 s build on a 120-title probe library).
        # No roots (nothing downloading): no realpath walk at all.
        root_parts = _root_parts(tuple(roots))
        if not root_parts:
            return False
        parts = path.expanduser().resolve().parts
        return any(parts[:depth] in root_parts for depth in range(1, len(parts) + 1))

    def _repair_legacy_library_episode_paths(self) -> int:
        """Collapse stale pre-rename episode paths onto the current library root."""
        current_root = self.config.library.root_dir.expanduser().resolve()
        repaired = 0
        rows = list(self.db.episodes())
        for item in rows:
            old_path = item.video_path.expanduser()
            if old_path.is_file():
                continue
            for legacy_name in LEGACY_APP_NAMES:
                legacy_root = current_root.parent / legacy_name
                try:
                    relative = old_path.relative_to(legacy_root)
                except ValueError:
                    continue
                candidate = current_root / relative
                if not candidate.is_file():
                    continue
                if self.db.merge_episode_path(old_path, candidate):
                    repaired += 1
                    self.logger.info(
                        "REPAIR step=library.legacy_path old=%r new=%r",
                        str(old_path),
                        str(candidate),
                    )
                break
        return repaired

    _LIBRARY_SCAN_CACHE_KEY = "library_scan_cache_v39"
    # 3: empty and unfinished aria2 files are no episodes (and .aria2 files count).
    # 4: S00Exx / "Specials" files of a series are specials, never its episodes.
    _LIBRARY_SCAN_CACHE_GENERATION = 4
    _LIBRARY_SCAN_CACHE_TTL_SECONDS = 6 * 60 * 60
    _LIBRARY_SCAN_IGNORE_MARKER = ".pudge-ignore-library-scan"

    @classmethod
    @contextmanager
    def library_ignore_memo(cls) -> Iterator[None]:
        """Answer ignore-marker lookups from one walk per folder inside this block.

        A Home build asks for every episode and download several times, and each
        question stat'ed every folder up to "/" (54,700 stats of 64,254 per build in
        the WEBFLOW-6 probe). Inside the block each folder is checked once.
        """
        if _IGNORE_ROOT_MEMO.get() is not None:
            yield
            return
        token = _IGNORE_ROOT_MEMO.set({})
        try:
            yield
        finally:
            _IGNORE_ROOT_MEMO.reset(token)

    @classmethod
    def _library_scan_ignore_root_for_path(cls, path: Path) -> Path | None:
        """Return nearest ancestor explicitly isolated from Library discovery."""
        memo = _IGNORE_ROOT_MEMO.get()
        key = str(path)
        if memo is not None and key in memo:
            return memo[key]
        try:
            current = Path(path).expanduser().resolve()
        except (OSError, RuntimeError):
            return None
        if current.is_file():
            current = current.parent
        walked: list[str] = []
        result: Path | None = None
        while True:
            folder_key = "\0" + str(current)
            if memo is not None and folder_key in memo:
                result = memo[folder_key]
                break
            walked.append(folder_key)
            if (current / cls._LIBRARY_SCAN_IGNORE_MARKER).is_file():
                result = current
                break
            parent = current.parent
            if parent == current:
                break
            current = parent
        if memo is not None:
            for folder_key in walked:
                memo[folder_key] = result
            memo[key] = result
        return result

    # An extra file a batch download brings along: an OVA, special or movie inside the batch
    # of a TV series, or an NCOP/NCED/PV. It is not an episode of the title: Home asks
    # whether to delete or keep it instead of searching subtitles for it forever.
    EXTRA_FILE_REVIEW = "review_extra_file"
    EXTRA_FILE_KEPT = "extra_file_kept"

    @staticmethod
    def _extra_file_kind(anime: LibraryAnime | None, name: str) -> str:
        """ "OVA", "Movie", "NCOP"... when ``name`` is an extra file of ``anime``, else ""."""
        return extra_files.extra_file_kind(anime, name)

    def _downloaded_extra_file_kind(self, video: Path, media_id: int | None, episode: int | None) -> str:
        """The kind of an extra file that came with a download, or "".

        Of the person's own files only a special (S00Exx) the library scan took out of a TV
        series' episodes counts (it had the number of a regular episode).
        """
        if media_id is None or episode is not None:
            return ""
        row = self.db.episode_by_path(video)
        if row is None:
            return ""
        if self._special_set_aside(video):
            return "Special"  # the library scan set it aside: a second file of a regular episode
        anime = self.db.get_anime(int(media_id))
        if not str(row.torrent_hash or "").strip():
            special = extra_files.season_zero_series(anime) and parse_release_name(video.name).season == 0
            return "Special" if special else ""
        return self._extra_file_kind(anime, video.name)

    def _own_extra_file(self, video: Path) -> bool:
        """An NCOP/NCED/PV in the person's own folder (not downloaded by Pudge)."""
        row = self.db.episode_by_path(video)
        if row is None or str(row.torrent_hash or "").strip():
            return False
        return bool(parse_release_name(video.name).extra)

    @staticmethod
    def _extra_file_kept_state_key(video: Path) -> str:
        return extra_files.kept_state_key(video)

    def _extra_file_kept(self, video: Path) -> bool:
        return bool(self.db.get_state(self._extra_file_kept_state_key(video), ""))

    def _special_set_aside(self, video: Path) -> bool:
        """A season-0 file the library scan set aside; it waits for the person (Keep/Delete)."""
        return bool(self.db.get_state(extra_files.set_aside_state_key(video), ""))

    def keep_extra_download_file(self, video: Path, episode: int | None = None) -> bool:
        """Keep an extra file. An NCOP/NCED/PV stays in the Library only; an OVA or movie
        belongs to its own AniList entry (the series' only fitting related entry), otherwise
        it is a title of its own. Its subtitles are then prepared as for any episode.

        A season-0 file the library scan set aside becomes episode ``episode`` of its series
        when the person chose one, or of a TV_SHORT/ONA series its S00 number (TVDB numbers such
        series in season 0), unless another file is that episode. Of a TV series it stays an
        unnumbered extra of the title: its S00E01 is no episode 1, also once that one is gone."""
        from .library import _regular_episode_exists

        video = Path(video).expanduser().resolve()
        row = self.db.episode_by_path(video)
        if row is None:
            return False
        series = self.db.get_anime(int(row.media_id)) if row.media_id is not None else None
        set_aside = self._special_set_aside(video)
        self.db.set_state(self._extra_file_kept_state_key(video), "1")
        self.db.delete_state(extra_files.set_aside_state_key(video))  # the person decided
        number = episode
        if number is None and series is not None and not extra_files.season_zero_series(series):
            number = parse_anime_filename(video).episode  # TV_SHORT/ONA: its S00 number
        if set_aside and series is not None and episode is None and extra_files.season_zero_series(series):
            # A TV series' special: kept as an extra of the title, without an episode number.
            self.db.mark_subtitle_job_needs_action(video, err("jobs.extraFileKept"), self.EXTRA_FILE_KEPT)
            self.logger.info(
                "EVENT extra_file.kept media_id=%s episode=- reason=season_zero_extra video=%r",
                row.media_id,
                str(video),
            )
            return True
        if (
            set_aside
            and series is not None
            and number is not None
            and int(number) >= 1
            and not _regular_episode_exists(self.db, series, video, int(number))
        ):
            self.db.set_episode_identity(video, series.media_id, int(number), series.title)
            self.db.queue_subtitle_job(
                video, series.media_id, int(number), priority=100, error=err("jobs.newDownload")
            )
            self.logger.info(
                "EVENT extra_file.kept media_id=%s episode=%s reason=season_zero_episode video=%r",
                series.media_id,
                number,
                str(video),
            )
            return True
        if not extra_files.is_story_extra(series, video.name):
            self.db.mark_subtitle_job_needs_action(video, err("jobs.extraFileKept"), self.EXTRA_FILE_KEPT)
            self.logger.info("EVENT extra_file.kept media_id=%s video=%r", row.media_id, str(video))
            return True
        node = extra_files.own_title_for_extra(self.db, series, video.name)
        if node is not None:
            anime = extra_files.own_title_anime(self.db, node, series)
            media_id, episode, title = (
                anime.media_id,
                extra_files.own_title_episode(anime, video.name),
                anime.title,
            )
        else:
            identity = parse_anime_filename(video)
            media_id, episode, title = None, identity.episode, identity.title or video.stem
        self.db.set_episode_identity(video, media_id, episode, title)
        candidates = self._subtitle_candidate_fingerprint_state_key(video)
        for key in (candidates, candidates + ":outcome", candidates + ":verification_failures"):
            self.db.delete_state(key)
        self.db.queue_subtitle_job(video, media_id, episode, priority=100, error=err("jobs.newDownload"))
        self.logger.info(
            "EVENT extra_file.kept media_id=%s episode=%s from_media_id=%s video=%r",
            media_id,
            episode,
            row.media_id,
            str(video),
        )
        return True

    # ------------------------------------------------------------------ Identify…
    # A file Pudge could not match (an OVA in the person's own folder, an unmatched or
    # misnumbered video) is given its AniList entry and episode by the person. The choice is
    # stored per file and wins in every later scan (library.user_episode_identity).

    @staticmethod
    def identify_allowed(row: LibraryEpisode | None) -> bool:
        return bool(row is not None and row.state not in {"ready", "watched"} and row.video_path.is_file())

    def identify_search_local(self, query: str, limit: int = 8) -> list[LibraryAnime]:
        """Titles of the local list that fit ``query`` (works while AniList is unreachable)."""
        cleaned = re.sub(r"\s+", " ", str(query or "")).strip().casefold()
        if len(cleaned) < 2:
            return []
        scored: list[tuple[float, str, LibraryAnime]] = []
        for anime in self.db.anime_list():
            names = [str(name or "") for name in (anime.title, *anime.titles, *anime.synonyms) if name]
            score = max(
                (
                    100.0
                    if cleaned in name.casefold()
                    else float(fuzz.token_set_ratio(cleaned, name.casefold()))
                    for name in names
                ),
                default=0.0,
            )
            if score >= 80:
                scored.append((score, anime.title.casefold(), anime))
        scored.sort(key=lambda row: (-row[0], row[1]))
        return [anime for _score, _title, anime in scored[: max(1, int(limit))]]

    def identify_episode_file(
        self,
        video: Path,
        media_id: Any,
        episode: Any = None,
        entry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Give a local file the AniList entry and episode the person chose (Identify…)."""
        video = Path(video).expanduser().resolve()
        row = self.db.episode_by_path(video)
        if row is None or not video.is_file():
            raise ManagerError(err("identify.fileNotFound"))
        if not self.identify_allowed(row):
            raise ManagerError(err("identify.fileReady"))
        try:
            media_id = int(media_id)
        except (TypeError, ValueError):
            media_id = 0
        if media_id <= 0:
            raise ManagerError(err("identify.entryRequired"))
        anime = self.db.get_anime(media_id)
        new_entry = anime is None
        if anime is None:
            anime = _identified_anime(media_id, entry)
        total = int(anime.episodes or 0)
        number: int | None
        if episode is None or str(episode).strip() == "":
            number = None
        else:
            try:
                number = int(str(episode).strip())
            except ValueError:
                raise ManagerError(err("identify.episodeRequired")) from None
        if str(anime.format or "").upper() == "MOVIE":
            number = None  # a movie has no episode in the library (as when matched by a scan)
        elif number is None:
            if total != 1:
                raise ManagerError(err("identify.episodeRequired"))
            number = 1
        elif number <= 0 or (total and number > total):
            raise ManagerError(err("identify.episodeOutOfRange", title=anime.title, episode=number))
        if number is not None and any(
            other.video_path != video and other.video_path.is_file()
            for other in self.db.episode_rows(media_id, number)
        ):
            # Another file is that episode already: one episode never gets two files (a
            # set-aside S00E02 named TV episode 2 while S01E02 is there). Another number can be picked.
            raise ManagerError(err("identify.episodeTaken", title=anime.title, episode=number))
        if new_entry:
            self.db.upsert_anime(anime)
            anime = self.db.get_anime(media_id) or anime
        self.db.set_episode_identity(video, media_id, number, anime.title)
        self.db.set_state(
            user_identity_state_key(video),
            json.dumps({"media_id": media_id, "episode": number}, separators=(",", ":")),
        )
        candidates = self._subtitle_candidate_fingerprint_state_key(video)
        for key in (
            candidates,
            candidates + ":outcome",
            candidates + ":verification_failures",
            self._subtitle_failure_streak_state_key(video),
            extra_files.set_aside_state_key(video),  # a set-aside special has an identity now
        ):
            self.db.delete_state(key)
        self.db.queue_subtitle_job(video, media_id, number, priority=100, error=err("jobs.identityChosen"))
        self.logger.info(
            "EVENT episode.identified media_id=%s episode=%s from_media_id=%s from_episode=%s video=%r",
            media_id,
            number,
            row.media_id,
            row.episode,
            str(video),
        )
        return {"media_id": media_id, "episode": number, "title": anime.title}

    def diagnose_video(self, video: Path) -> dict[str, Any]:
        """ "Why not ready?" for one file, also for a file without an AniList entry."""
        video = Path(video).expanduser().resolve()
        row = self.db.episode_by_path(video)
        if row is None:
            raise ManagerError(err("identify.fileNotFound"))
        if row.media_id is not None and self.db.get_anime(int(row.media_id)) is not None:
            return self.diagnose_episode(int(row.media_id), row.episode)
        language = self.config.ui.language
        jobs = [job for job in self.db.subtitle_jobs() if str(job["video_path"]) == str(video)]
        exists = video.is_file()
        external_ready = False
        if row.subtitle_path is not None:
            try:
                external_ready = row.subtitle_path.is_file() and row.subtitle_path.stat().st_size > 0
            except OSError:
                external_ready = False
        subtitle_ready = bool(
            row.state in {"ready", "watched"} and (external_ready or row.embedded_subtitle_id is not None)
        )
        last_error = (
            i18n_message(
                _localize_preparation_detail(str(jobs[0]["last_error"] or ""), language=language), language
            )
            if jobs
            else ""
        )
        label = lambda key: t(f"manager.{key}", lang=language)  # noqa: E731
        checks = [
            {"key": "anilist", "ok": False, "label": label("diagAnilistNotMatched"), "detail": video.name},
            {
                "key": "file",
                "ok": exists,
                "label": label("diagLocalVideo"),
                "detail": str(video) if exists else label("diagNoVideo"),
            },
            {
                "key": "subtitle",
                "ok": subtitle_ready,
                "label": label("diagSubtitlesPrepared"),
                "detail": str(row.subtitle_path) if row.subtitle_path else label("diagNoSubtitles"),
            },
            {
                "key": "job",
                "ok": subtitle_ready or bool(jobs),
                "label": label("diagPreparationJob"),
                "detail": (
                    f"{jobs[0]['state']}; attempts={jobs[0]['attempts']}; {last_error}"
                    if jobs
                    else label("diagCompleted" if subtitle_ready else "diagMissingJob")
                ),
            },
        ]
        return {
            "media_id": None,
            "title": row.title,
            "episode": row.episode,
            "ready": subtitle_ready,
            "video_path": str(video),
            "state": row.state,
            "watched_on_anilist": False,
            "presentation": derive_episode_presentation(
                local=row,
                download=None,
                action_job=jobs[0] if jobs else None,
                watched_externally=False,
                allow_ocr_ready=bool(self.config.matching.ocr_counts_as_ready),
            ),
            "checks": checks,
            "downloads": [],
            "download_intent": None,
            "job": (
                {
                    "state": str(jobs[0]["state"]),
                    "attempts": int(jobs[0]["attempts"] or 0),
                    "next_check": float(jobs[0]["next_check"] or 0),
                    "error": last_error,
                }
                if jobs
                else None
            ),
            "identify_allowed": self.identify_allowed(row),
        }

    def _deselect_torrent_file(self, torrent_hash: str, video: Path) -> None:
        """Stop the torrent client from fetching a deleted file again (best effort)."""
        if not str(torrent_hash or "").strip() or not self.downloads_enabled():
            return
        try:
            clients = self.torrent_clients()
        except Exception as exc:  # noqa: BLE001 - deleting the file must not depend on the client
            self.logger.warning("SKIP step=extra_file.deselect reason=%r", str(exc))
            return
        for name, client in clients:
            try:
                rows = client.files(torrent_hash)
                indices = [
                    int(row.get("index"))
                    for row in rows
                    if row.get("index") is not None and Path(str(row.get("name") or "")).name == video.name
                ]
                if indices:
                    client.set_file_priority(torrent_hash, indices, 0)
                    self.logger.info(
                        "DONE step=extra_file.deselect backend=%s hash=%s files=%s",
                        name,
                        torrent_hash,
                        indices,
                    )
            except Exception as exc:  # noqa: BLE001 - a stopped or unreachable client keeps the file selected
                self.logger.info("SKIP step=extra_file.deselect backend=%s reason=%r", name, str(exc))
            finally:
                client.close()

    def delete_extra_download_file(self, video: Path) -> bool:
        """Delete an extra file that came with a download (only one Pudge flagged as extra)."""
        video = Path(video).expanduser().resolve()
        row = self.db.episode_by_path(video)
        if row is None or not self._downloaded_extra_file_kind(video, row.media_id, row.episode):
            return False
        self._deselect_torrent_file(str(row.torrent_hash or ""), video)
        video.unlink(missing_ok=True)
        if row.subtitle_path is not None:
            Path(row.subtitle_path).unlink(missing_ok=True)
        self.db.delete_episode_record(video)
        self.db.delete_state(self._extra_file_kept_state_key(video))
        self.db.delete_state(extra_files.set_aside_state_key(video))
        self._forget_video_state([video])
        self._remove_empty_episode_parent(video)
        self.logger.info(
            "DONE step=extra_file.delete media_id=%s video=%r torrent_hash=%s",
            row.media_id,
            str(video),
            row.torrent_hash,
        )
        return True

    @classmethod
    def _library_path_is_ignored(cls, path: Path) -> bool:
        return cls._library_scan_ignore_root_for_path(path) is not None

    def _library_scan_ignore_roots(self, roots: tuple[Path, ...] | None = None) -> tuple[Path, ...]:
        """Discover marker roots once per Library scan."""
        found: list[Path] = []
        for root in roots or self._library_scan_roots():
            if not root.is_dir():
                continue
            try:
                for marker in root.rglob(self._LIBRARY_SCAN_IGNORE_MARKER):
                    if not marker.is_file():
                        continue
                    ignored = marker.parent.resolve()
                    if any(ignored == old or old in ignored.parents for old in found):
                        continue
                    found.append(ignored)
            except (OSError, PermissionError, RuntimeError):
                continue
        return tuple(found)

    def forget_isolated_library_episodes(self) -> int:
        """Forget episode rows of videos in a folder marked ``.pudge-ignore-library-scan``.

        The scan never adds such a video, so a row there was written by something
        else that ran Pudge's code against this library (a benchmark or test harness
        whose corpus carries the marker). Home hides those rows, but they stayed in
        the database and reached the iPhone as ready episodes. Only the database
        record and its subtitle job go: the video file stays. A row with watch
        history (watched, a playback position) is kept (and still never offered).
        Idempotent; returns rows forgotten.
        """
        forgotten = 0
        with self.library_ignore_memo():
            for item in list(self.db.episodes()):
                marker_root = self._library_scan_ignore_root_for_path(item.video_path)
                if marker_root is None:
                    continue
                if item.state == "watched" or item.watched_at or float(item.playback_position or 0.0) > 0.0:
                    self.logger.info(
                        "SKIP step=library.isolated_episode reason=watch_history video=%r",
                        str(item.video_path),
                    )
                    continue
                self.db.delete_episode_record(item.video_path)
                forgotten += 1
                self.logger.info(
                    "RESULT step=library.isolated_episode action=forget media_id=%s episode=%s state=%s "
                    "marker=%r video=%r",
                    item.media_id,
                    item.episode,
                    item.state,
                    str(marker_root),
                    str(item.video_path),
                )
        return forgotten

    _DROPPED_MEDIA_FOLDERS_KEY = "library_dropped_media_folders_v1"

    def dropped_media_folders(self) -> tuple[Path, ...]:
        """Anime folders dropped on the window: scanned like watched folders, not listed in Settings."""
        try:
            values = json.loads(self.db.get_state(self._DROPPED_MEDIA_FOLDERS_KEY, "[]") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        if not isinstance(values, list):
            return ()
        return tuple(Path(value) for value in values if isinstance(value, str) and value.strip())

    def _save_dropped_media_folders(self, folders: Iterable[Path]) -> None:
        values = list(dict.fromkeys(str(folder) for folder in folders))
        self.db.set_state(self._DROPPED_MEDIA_FOLDERS_KEY, json.dumps(values, ensure_ascii=False))

    def _watched_media_folders(self) -> tuple[Path, ...]:
        """Settings' watched folders, then the folders dropped on the window."""
        return (*self.config.paths.download_dirs, *self.dropped_media_folders())

    _DROPPED_MEDIA_FILES_KEY = "library_dropped_media_files_v1"

    def dropped_media_files(self) -> tuple[Path, ...]:
        """Single videos dropped on the window: kept in the library, never scanned as a folder."""
        try:
            values = json.loads(self.db.get_state(self._DROPPED_MEDIA_FILES_KEY, "[]") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return ()
        if not isinstance(values, list):
            return ()
        return tuple(Path(value) for value in values if isinstance(value, str) and value.strip())

    def _remember_dropped_media_file(self, video: Path) -> None:
        """Remember one dropped video (only the file, not its folder) for the library scan.

        A file inside the library root, a watched folder or a dropped folder is
        found by the scan anyway. Entries whose file is gone are dropped here.
        """
        resolved = Path(video).expanduser().resolve()
        roots: list[Path] = []
        for root in (self.config.library.root_dir, *self._watched_media_folders()):
            try:
                roots.append(Path(root).expanduser().resolve())
            except OSError:
                continue
        if self._path_within(resolved, tuple(roots)):
            return
        kept = [value for value in self.dropped_media_files() if value.is_file() and value != resolved]
        values = list(dict.fromkeys(str(value) for value in (*kept, resolved)))
        self.db.set_state(self._DROPPED_MEDIA_FILES_KEY, json.dumps(values, ensure_ascii=False))

    def import_dropped_media_folder(self, folder: Path) -> list[LibraryEpisode]:
        """A folder of anime dropped on the window, from anywhere: its episodes, matched like a watched folder's.

        The folder is remembered (only it, not its parent) so its episodes stay in the
        library and files added to it later are found; a folder in which no anime was
        recognized is forgotten again. Nothing is moved or copied.
        """
        resolved = Path(folder).expanduser().resolve()
        if not resolved.is_dir():
            raise ManagerError(err("manager.unsupportedVideo", name=resolved.name))
        previous = self.dropped_media_folders()
        covered = any(resolved == root or root in resolved.parents for root in self._library_scan_roots())
        if not covered:
            # A dropped folder that holds earlier dropped folders replaces them.
            kept = [value for value in previous if resolved not in Path(value).parents]
            self._save_dropped_media_folders([*kept, resolved])
        self.logger.info("START step=library.dropped_folder path=%r new_root=%s", str(resolved), not covered)
        report: dict[str, Any] = {}
        self._scan_library_uncached(report=report)
        found = [
            item
            for item in self.db.episodes()
            if item.media_id is not None
            and self._path_within(item.video_path.expanduser().resolve(), (resolved,))
        ]
        if not found and report.get("resolver_incomplete"):
            # AniList refused or the lookup budget ran out: "nothing recognized" is
            # not an answer yet. The folder stays and later scans match it.
            self.logger.warning(
                "DEFER step=library.dropped_folder path=%r reason=anilist_unavailable", str(resolved)
            )
            raise ManagerError(err("web.dropFolderAniListUnavailable"))
        if not found and not covered:
            self._save_dropped_media_folders(previous)
        self.logger.info(
            "DONE step=library.dropped_folder path=%r episodes=%s anime=%s",
            str(resolved),
            len(found),
            sorted({int(item.media_id) for item in found if item.media_id is not None}),
        )
        return sorted(
            found, key=lambda item: (int(item.media_id or 0), item.episode or 0, str(item.video_path))
        )

    def _library_scan_roots(self) -> tuple[Path, ...]:
        roots: list[Path] = []
        main_root = self.config.library.root_dir.expanduser()
        try:
            roots.append(main_root.resolve())
        except (OSError, RuntimeError):
            roots.append(main_root)
        for configured in self._watched_media_folders():
            root = Path(configured).expanduser()
            if not root.is_dir():
                continue
            try:
                resolved = root.resolve()
            except (OSError, RuntimeError):
                continue
            if any(resolved == old or old in resolved.parents for old in roots):
                continue
            roots.append(resolved)
        return tuple(roots)

    def _finished_torrent_hashes(self) -> frozenset[str]:
        """Info hashes of the downloads that are complete (seeding or paused after finishing)."""
        hashes: set[str] = set()
        for item in self.db.downloads():
            if not self._download_is_complete(item):
                continue
            raw = getattr(item, "raw", {}) or {}
            for value in (item.torrent_hash, raw.get("infohash_v1")):
                text = str(value or "").strip().casefold()
                if text:
                    hashes.add(text)
        return frozenset(hashes)

    def _finished_torrent_check(self) -> Callable[[str], bool]:
        """finished_torrent() for unfinished_video(); reads the downloads once, on first use."""
        cache: list[frozenset[str]] = []

        def finished(info_hash: str) -> bool:
            if not cache:
                cache.append(self._finished_torrent_hashes())
            return str(info_hash or "").casefold() in cache[0]

        return finished

    def _missing_video_was_watched(self, video: Path, media_id: int | None, episode: int | None) -> bool:
        """A gone file's episode is watched: locally, on a phone, or by AniList progress."""
        row = self.db.episode_by_path(video)
        if row is not None and str(row.state or "") == "watched":
            return True
        if media_id is None:
            return False
        anime = self.db.get_anime(int(media_id))
        if anime is None:
            return False
        return watched_by_anilist_progress(
            episode,
            anime.progress,
            total_episodes=anime.episodes,
            media_format=anime.format,
        )

    def unfinished_library_video(
        self,
        video: Path,
        *,
        roots: tuple[Path, ...] | None = None,
        finished: Callable[[str], bool] | None = None,
    ) -> bool:
        """unfinished_video(), looking for control files up to the library folder holding the file.

        The control file of a finished torrent (aria2 keeps it while the pack seeds)
        does not make its files unfinished: they were "unfinished_video" and lost their
        episode rows every second while the reconcile restored them.
        """
        try:
            resolved = Path(video).expanduser().resolve()
        except (OSError, RuntimeError):
            return False
        folders = self._library_scan_roots() if roots is None else roots
        return unfinished_video(
            resolved,
            next((root for root in folders if root in resolved.parents), None),
            finished_torrent=finished or self._finished_torrent_check(),
        )

    def _library_scan_snapshot(self) -> dict[str, Any] | None:
        """Build a cheap filesystem signature for files relevant to library import.

        Full library reconciliation probes containers and can take ~45 seconds on
        a modest library. Walking paths + stat is orders of magnitude cheaper and
        lets the GUI and LaunchAgent share a short-lived scan result safely.
        """
        digest = hashlib.sha256()
        digest.update(
            f"generation={self._LIBRARY_SCAN_CACHE_GENERATION};recursive={int(self.config.library.recursive)}\n".encode()
        )
        relevant = (
            set(VIDEO_EXTENSIONS)
            | set(TEXT_SUBTITLE_EXTENSIONS)
            | set(IMAGE_SUBTITLE_EXTENSIONS)
            | {".pgs", ".aria2"}
        )
        file_count = 0
        roots = self._library_scan_roots()
        # A download still being written changed size and mtime on every pass, so
        # the fingerprint never matched and each pass ran the full scan (MGR-9).
        # The scan skips those files anyway: an unfinished download counts by its
        # path only, and so do aria2 control files and the videos they belong to.
        try:
            incomplete = tuple(path.expanduser().resolve() for path in self.incomplete_download_paths())
        except (OSError, RuntimeError, sqlite3.Error):
            incomplete = ()
        try:
            for root in roots:
                digest.update(f"root={root}\n".encode("utf-8", errors="surrogateescape"))
                if not root.exists():
                    continue
                iterator = root.rglob("*") if self.config.library.recursive else root.glob("*")
                files: list[tuple[Path, str]] = []
                controls: set[Path] = set()
                for path in iterator:
                    suffix = path.suffix.casefold()
                    if suffix not in relevant or not path.is_file():
                        continue
                    resolved = path.resolve()
                    files.append((resolved, suffix))
                    if suffix == ".aria2":
                        controls.add(resolved.with_name(resolved.stem))
                for resolved, suffix in files:
                    if (
                        suffix == ".aria2"
                        or (
                            controls
                            and (resolved in controls or any(p in controls for p in resolved.parents))
                        )
                        or any(resolved == item or item in resolved.parents for item in incomplete)
                    ):
                        line = f"{resolved}\0unfinished\n"
                    else:
                        stat = resolved.stat()
                        line = f"{resolved}\0{stat.st_size}\0{stat.st_mtime_ns}\n"
                    digest.update(line.encode("utf-8", errors="surrogateescape"))
                    file_count += 1
        except (OSError, PermissionError, RuntimeError):
            return None
        return {"fingerprint": digest.hexdigest(), "file_count": file_count}

    def _cached_library_items(self) -> list[LibraryEpisode]:
        roots = self._library_scan_roots()
        result: list[LibraryEpisode] = []
        for item in self.db.episodes():
            try:
                path = item.video_path.expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if not path.is_file() or not self._path_within(path, roots):
                continue
            result.append(item)
        return result

    def _load_library_scan_cache(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.db.get_state(self._LIBRARY_SCAN_CACHE_KEY, "{}") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _store_library_scan_cache(self, snapshot: dict[str, Any], rows: int) -> None:
        payload = {
            "generation": self._LIBRARY_SCAN_CACHE_GENERATION,
            "fingerprint": str(snapshot.get("fingerprint") or ""),
            "file_count": int(snapshot.get("file_count") or 0),
            "rows": max(0, int(rows)),
            "completed_at": time.time(),
        }
        # A row of a gone file that the scan keeps for now: the file list does not
        # change when its wait ends, so an "unchanged" library kept it for hours.
        due_at = getattr(self, "_library_scan_forget_due_at", None)
        if due_at:
            payload["forget_due_at"] = float(due_at)
        self.db.set_state(
            self._LIBRARY_SCAN_CACHE_KEY,
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
        )

    def scan_library(
        self,
        *,
        reuse_unchanged: bool = False,
        user_requested: bool = False,
        outcome: dict[str, Any] | None = None,
    ) -> list[LibraryEpisode]:
        """Scan the library roots. ``outcome`` (optional) receives ``skipped="busy"`` when the
        heavy-work lease was taken (subtitle alignment, OCR, the agent) and the cached rows
        were returned instead of a scan."""
        if not reuse_unchanged:
            return self._scan_library_uncached()

        snapshot = self._library_scan_snapshot()
        cached = self._load_library_scan_cache()
        now = time.time()
        try:
            completed_at = float(cached.get("completed_at") or 0.0)
        except (TypeError, ValueError):
            completed_at = 0.0
        age = max(0.0, now - completed_at) if completed_at else float("inf")
        try:
            forget_due_at = float(cached.get("forget_due_at") or 0.0)
        except (TypeError, ValueError):
            forget_due_at = 0.0
        cache_hit = bool(
            snapshot is not None
            and int(cached.get("generation") or 0) == self._LIBRARY_SCAN_CACHE_GENERATION
            and str(cached.get("fingerprint") or "") == str(snapshot.get("fingerprint") or "")
            and age <= self._LIBRARY_SCAN_CACHE_TTL_SECONDS
            and not (forget_due_at and now >= forget_due_at)
        )
        if cache_hit:
            items = self._cached_library_items()
            self.logger.info(
                "CACHE step=library.scan_fingerprint age_s=%.1f files=%s rows=%s",
                age,
                snapshot.get("file_count", 0) if snapshot else 0,
                len(items),
            )
            return items

        lease = self.work_scheduler.acquire_heavy(
            "library-scan",
            blocking=False,
            foreground_sensitive=True,
            priority=WorkPriority.USER if user_requested else WorkPriority.BACKGROUND,
            resource="cpu",
        )
        if lease is None:
            items = self._cached_library_items()
            if outcome is not None:
                outcome["skipped"] = "busy"
            self.logger.info(
                "SKIP step=library.scan reason=heavy_work_active files=%s rows=%s",
                snapshot.get("file_count", 0) if snapshot else -1,
                len(items),
            )
            return items

        with lease:
            return self._scan_library_under_lease()

    def _scan_library_under_lease(self) -> list[LibraryEpisode]:
        items = self._scan_library_uncached()
        final_snapshot = self._library_scan_snapshot()
        if final_snapshot is not None:
            self._store_library_scan_cache(final_snapshot, len(items))
            self.logger.info(
                "CACHE step=library.scan_fingerprint_write files=%s rows=%s",
                final_snapshot.get("file_count", 0),
                len(items),
            )
        return items

    def scan_library_when_free(self, cancel_event: threading.Event | None = None) -> bool:
        """A rescan the person asked for while other heavy work held the lease: wait for the
        lease at user priority (ahead of background work), then scan. False if cancelled."""
        lease = self.work_scheduler.acquire_heavy(
            "library-scan",
            blocking=True,
            foreground_sensitive=True,
            wait_for_foreground=True,
            cancel_event=cancel_event,
            poll_seconds=2.0,
            priority=WorkPriority.USER,
            resource="cpu",
        )
        if lease is None:
            self.logger.info("SKIP step=library.scan_queued reason=cancelled")
            return False
        with lease:
            items = self._scan_library_under_lease()
        self.logger.info("RESULT step=library.scan_queued rows=%s", len(items))
        return True

    @_reusing_db_connection
    def _scan_library_uncached(self, report: dict[str, Any] | None = None) -> list[LibraryEpisode]:
        """Scan every library root. ``report`` (optional) receives ``resolver_incomplete``:
        True when AniList failed or the lookup budget ran out, so "no match" is not final."""
        self._repair_legacy_library_episode_paths()
        incomplete = self.incomplete_download_paths()
        ignored = self._library_scan_ignore_roots()
        excluded = tuple(dict.fromkeys((*incomplete, *ignored)))
        if ignored:
            self.logger.info(
                "SKIP step=library.scan_marked_roots count=%s roots=%r",
                len(ignored),
                [str(path) for path in ignored],
            )
        # Remove rows created by older versions that scanned qBittorrent's
        # partially allocated video files as if they were finished local media.
        # Also forget manually deleted local-only files. Torrent-backed rows are
        # reconciled against qBittorrent in ``sync_downloads`` so an active move
        # or recheck does not erase them prematurely. A missing file is
        # forgotten only when its drive is there and it stayed missing.
        missing_local: list[Path] = []
        scan_roots = self._library_scan_roots()
        finished_torrent = self._finished_torrent_check()
        for item in self.db.episodes():
            if self._path_within(item.video_path, incomplete):
                if item.state == "watched" or item.watched_at or float(item.playback_position or 0.0) > 0.0:
                    # A finished torrent its client checks again (Force recheck, its disk
                    # plugged back in) is unfinished until the check ends: its rows keep the
                    # watched mark, place and deletion date instead of coming back unwatched.
                    continue
                self.db.delete_episode_record(item.video_path)
            elif not item.video_path.is_file():
                if not item.torrent_hash:
                    missing_local.append(item.video_path)
            elif self.unfinished_library_video(item.video_path, roots=scan_roots, finished=finished_torrent):
                # An empty file, or one whose aria2 control file is still there (and no
                # finished download's), is no episode (left over by a removed race candidate).
                self.db.delete_episode_record(item.video_path)
                self.logger.info("SKIP step=library.unfinished_video video=%r", str(item.video_path))
        for value in sorted(self._forgettable_missing_videos(missing_local)):
            self.db.delete_episode_record(Path(value))
            self.logger.info("REPAIR step=library.missing_local_video video=%r", value)
        self._library_scan_forget_due_at = self._missing_video_due_at
        items = scan_library(
            self.config.library.root_dir,
            self.db,
            recursive=self.config.library.recursive,
            ffprobe=self.config.tools.ffprobe,
            ffmpeg=self.config.tools.ffmpeg,
            excluded_paths=excluded,
            pipeline_cache_config=self.config,
            media_episode_resolver=lambda anime, release: self._media_episode_from_release(anime, release),
            file_episode_resolver=self._scoped_file_media_episode,
            finished_torrent=finished_torrent,
        )

        # Additional watched folders (Downloads, an external drive, etc.) are
        # intentionally stricter than the managed Library root: arbitrary video
        # files are ignored unless they can be mapped confidently to a known or
        # public AniList anime entry.
        main_root = self.config.library.root_dir.expanduser().resolve()
        extra_roots: list[Path] = []
        for configured in self._watched_media_folders():
            root = configured.expanduser()
            if not root.is_dir():
                continue
            resolved_root = root.resolve()
            if resolved_root == main_root or main_root in resolved_root.parents:
                continue
            if any(
                resolved_root == existing or existing in resolved_root.parents for existing in extra_roots
            ):
                continue
            extra_roots.append(resolved_root)

        # v0.6.37 briefly reused the old download_dirs config key for media
        # watching. On upgraded installations that key could still point to a
        # subtitle/Downloads folder, causing unrelated batch files to be
        # imported into Waiting for preparation. Once the active watched roots
        # are known, remove only unresolved external rows that are no longer
        # inside any managed root. Ready/watched/manual-progress rows and
        # torrent-managed files are deliberately preserved.
        # A single dropped video is remembered by itself (not its folder: that
        # would make all of ~/Downloads a watched root) and stays in the library.
        dropped_files = tuple(
            value.expanduser().resolve() for value in self.dropped_media_files() if value.is_file()
        )
        active_media_roots = (main_root, *extra_roots, *dropped_files)
        for stale in list(self.db.episodes()):
            if self._path_within(stale.video_path, ignored):
                continue
            if stale.torrent_hash or self._path_within(stale.video_path, active_media_roots):
                continue
            if not stale.video_path.is_file():
                continue
            if stale.state in {"ready", "watched"} or stale.watched_at:
                continue
            if float(stale.playback_position or 0.0) > 0.0:
                continue
            self.logger.info(
                "CLEANUP step=library.external_orphan path=%r media_id=%s state=%s",
                str(stale.video_path),
                stale.media_id,
                stale.state,
            )
            self.db.delete_episode_record(stale.video_path)

        # Detach obviously impossible historical watched-folder matches without
        # contacting AniList. This is intentionally stronger than the network
        # revalidation below so a rate limit (HTTP 429) cannot keep e.g.
        # catmahjong.mp4 attached to the old Mahoutsukai/Mahoyo false match.
        for stale in list(self.db.episodes()):
            resolved = stale.video_path.expanduser().resolve()
            if self._path_within(resolved, ignored):
                continue
            if not self._path_within(resolved, tuple(extra_roots)):
                continue
            if stale.media_id is None or stale.state == "watched" or stale.watched_at:
                continue
            if float(stale.playback_position or 0.0) > 0.0:
                continue
            managed_download = self.db.download_by_hash(stale.torrent_hash) if stale.torrent_hash else None
            if managed_download is not None and managed_download.media_id == stale.media_id:
                continue
            associated = self.db.get_anime(stale.media_id)
            if associated is None:
                continue
            identity = parse_anime_filename(resolved)
            names = [associated.title, *associated.titles, *associated.synonyms]
            strict = strict_title_similarity(identity.title, names)
            threshold = 72.0 if identity.episode is not None else 86.0
            if strict >= threshold:
                continue
            self.logger.info(
                "CLEANUP step=library.external_false_match_local path=%r media_id=%s strict=%.1f",
                str(resolved),
                stale.media_id,
                strict,
            )
            self.db.delete_episode_record(resolved)

        client: AniListClient | None = None
        resolver_cache: dict[tuple[str, int | None, int | None, int | None], LibraryAnime | None] = {}
        resolver_had_error = False
        resolver_rate_limited = False
        resolver_budget_exhausted = False
        resolver_started_at = time.monotonic()
        resolver_network_seconds = 0.0
        resolver_network_queries = 0
        # Watched folders can contain thousands of unrelated videos. Never let
        # their public-AniList matching turn an ordinary Refresh into a many-minute
        # crawl. Known/local matches remain unlimited; remote discovery gets a
        # small query budget (time spent in AniList requests, not in ffprobe of
        # other files) and a short per-request timeout. One search per title
        # serves every episode file of that series and is remembered for a few
        # hours, so the next scan continues with titles not looked up yet.
        resolver_budget_seconds = 10.0
        resolver_query_limit = 8
        search_cache_ttl = 6 * 3600.0
        search_cache: dict[str, tuple[float, list]] = getattr(self, "_external_search_cache", None) or {}
        self._external_search_cache = search_cache
        now_wall = time.time()
        for cached_title in [
            name for name, (at, _m) in search_cache.items() if now_wall - at > search_cache_ttl
        ]:
            search_cache.pop(cached_title, None)
        if extra_roots and self.config.anilist.enabled:
            client = AniListClient(
                self.config.anilist.endpoint, self.config.anilist.access_token, timeout=3.0
            )

        def strict_external_title_score(query: str, names: list[str]) -> float:
            # Do not use partial/WRatio matching for arbitrary files discovered in
            # watched folders.  A random local video can share a tiny substring
            # with an anime title and WRatio may still score it surprisingly high.
            query_norm = re.sub(r"[^\w]+", " ", str(query).casefold(), flags=re.UNICODE).strip()
            if len(query_norm.replace(" ", "")) < 4:
                return 0.0
            best = 0.0
            for raw_name in names:
                name_norm = re.sub(r"[^\w]+", " ", str(raw_name).casefold(), flags=re.UNICODE).strip()
                if not name_norm:
                    continue
                best = max(
                    best,
                    float(fuzz.ratio(query_norm, name_norm)),
                    float(fuzz.token_sort_ratio(query_norm, name_norm)),
                )
            return best

        def local_external_score(identity, anime: LibraryAnime, *, known: bool) -> float | None:
            """Strict title score of a local anime, or None when it cannot be this file.

            ``known``: this title was already imported as ``anime``. Any other
            entry of the local list needs a near-literal title and season.
            """
            names = [anime.title, *anime.titles, *anime.synonyms]
            strict = strict_external_title_score(identity.title, names)
            if strict < ((72.0 if identity.episode is not None else 86.0) if known else 90.0):
                return None
            if identity.season is not None or not known:
                seasons = [
                    value for value in (_season_number(name) for name in names if name) if value is not None
                ]
                # "Cyberpunk - Edgerunners 2 - 01": the 2 belongs to the title, as in the entry's.
                file_season = identity.season or _season_number(str(identity.title or "")) or 1
                if (seasons[0] if seasons else 1) != int(file_season):
                    return None
            if identity.year and anime.season_year and abs(int(identity.year) - int(anime.season_year)) > 1:
                return None
            return strict

        # Files of a series already imported from a watched folder map the same
        # title to the same anime without asking AniList again.
        known_external: dict[tuple[str, int | None, bool], int | None] = {}
        if extra_roots:
            for row in self.db.episodes():
                if row.media_id is None:
                    continue
                row_path = row.video_path.expanduser().resolve()
                if self._path_within(row_path, ignored) or not self._path_within(
                    row_path, tuple(extra_roots)
                ):
                    continue
                row_identity = parse_anime_filename(row_path)
                row_key = (
                    str(row_identity.title).casefold(),
                    row_identity.season,
                    row_identity.episode is None,
                )
                if known_external.setdefault(row_key, int(row.media_id)) != int(row.media_id):
                    known_external[row_key] = None
        local_anime: list[LibraryAnime] | None = None
        local_cache: dict[tuple[str, int | None, bool], LibraryAnime | None] = {}

        def resolve_local(identity) -> LibraryAnime | None:
            nonlocal local_anime
            series_key = (str(identity.title).casefold(), identity.season, identity.episode is None)
            if series_key in local_cache:
                return local_cache[series_key]
            found: LibraryAnime | None = None
            known_id = known_external.get(series_key)
            if known_id is not None:
                known = self.db.get_anime(known_id)
                if known is not None and local_external_score(identity, known, known=True) is not None:
                    found = known
            # Episode-less files (movies/OVAs) keep the stricter AniList check.
            if found is None and identity.episode is not None:
                if local_anime is None:
                    local_anime = self.db.anime_list()
                scored = [
                    (score, anime)
                    for anime in local_anime
                    if (score := local_external_score(identity, anime, known=False)) is not None
                ]
                if len(scored) == 1:
                    # Only an unambiguous entry; e.g. a remake or sequel with a
                    # near-identical title is left to the AniList check.
                    found = scored[0][1]
            local_cache[series_key] = found
            if found is not None:
                self.logger.info(
                    "MATCH step=library.external_match title=%r media_id=%s source=%s",
                    identity.title,
                    found.media_id,
                    "known" if known_id == found.media_id else "local",
                )
            return found

        def resolve_external(identity) -> LibraryAnime | None:
            nonlocal resolver_had_error, resolver_rate_limited, resolver_network_queries
            nonlocal resolver_budget_exhausted, resolver_network_seconds
            key = (str(identity.title).casefold(), identity.season, identity.episode, identity.year)
            if key in resolver_cache:
                return resolver_cache[key]
            local = resolve_local(identity)
            if local is not None:
                resolver_cache[key] = local
                return local
            if client is None or resolver_rate_limited:
                resolver_cache[key] = None
                return None
            # Reject obvious camera/export/random filenames before any network I/O.
            title_text = str(identity.title or "").strip()
            meaningful = re.sub(r"[\W\d_]+", "", title_text, flags=re.UNICODE)
            if len(meaningful) < 4 or (
                identity.episode is None and len(title_text.split()) <= 1 and len(meaningful) < 7
            ):
                resolver_cache[key] = None
                return None
            search_key = title_text.casefold()
            cached_search = search_cache.get(search_key)
            if cached_search is not None:
                matches = cached_search[1]
            elif (
                resolver_network_queries >= resolver_query_limit
                or resolver_network_seconds >= resolver_budget_seconds
            ):
                # Not "no match": this file is simply looked up on a later scan.
                resolver_budget_exhausted = True
                resolver_cache[key] = None
                return None
            else:
                resolver_network_queries += 1
                request_started = time.monotonic()
                try:
                    # The query depends only on the title; episode/year are scored
                    # per file below so one search serves the whole series.
                    matches = client.search(replace(identity, episode=None, year=None))
                except AniListError as exc:
                    resolver_had_error = True
                    if isinstance(exc, AniListHTTPError) and exc.status_code in {403, 429}:
                        # Rate limited or refused: every further search this scan would fail too.
                        resolver_rate_limited = True
                        self.logger.warning(
                            "BACKOFF step=library.external_match reason=anilist_%s remaining_files_skipped=1 title=%r",
                            exc.status_code,
                            identity.title,
                        )
                    else:
                        self.logger.warning(
                            "FALLBACK step=library.external_match title=%r error=%r", identity.title, str(exc)
                        )
                    resolver_cache[key] = None
                    return None
                finally:
                    resolver_network_seconds += time.monotonic() - request_started
                matches = list(matches or [])
                search_cache[search_key] = (time.time(), matches)
            if not matches:
                resolver_cache[key] = None
                return None

            def identity_score(candidate) -> float:
                score = float(candidate.score)
                season_year = getattr(candidate, "season_year", None)
                episodes = getattr(candidate, "episodes", None)
                if identity.year and season_year:
                    score += 8 if identity.year == int(season_year) else -3
                if identity.episode and episodes:
                    score += 4 if identity.episode <= int(episodes) else -20
                if identity.season is not None:
                    candidate_seasons = [
                        value
                        for value in (
                            _season_number(name) for name in [*candidate.titles, *candidate.synonyms]
                        )
                        if value is not None
                    ]
                    candidate_season = candidate_seasons[0] if candidate_seasons else 1
                    score += 20.0 if candidate_season == int(identity.season) else -20.0
                return score

            ranked = sorted(matches, key=identity_score, reverse=True)
            top = ranked[0]
            top_score = identity_score(top)
            runner_up_score = identity_score(ranked[1]) if len(ranked) > 1 else -999.0
            top_names = [*top.titles, *top.synonyms]
            strict_score = strict_external_title_score(identity.title, top_names)
            # Episode-tagged release files have extra structural evidence, so a
            # conservative 72 is enough.  Episode-less files (movies/OVAs) need a
            # much closer literal title match because arbitrary MP4s are common in
            # Downloads.
            strict_threshold = 72.0 if identity.episode is not None else 86.0
            if top_score < 82.0 or top_score - runner_up_score < 8.0 or strict_score < strict_threshold:
                self.logger.info(
                    "SKIP step=library.external_match title=%r top=%s score=%.1f strict=%.1f margin=%.1f reason=ambiguous",
                    identity.title,
                    top.id,
                    top_score,
                    strict_score,
                    top_score - runner_up_score,
                )
                resolver_cache[key] = None
                return None
            anime = LibraryAnime(
                media_id=int(top.id),
                title=(top.titles[0] if top.titles else identity.title),
                titles=list(top.titles),
                synonyms=list(top.synonyms),
                site_url=f"https://anilist.co/anime/{int(top.id)}",
                status="",
                progress=0,
                episodes=top.episodes,
                format=top.format,
                season_year=top.season_year,
            )
            resolver_cache[key] = anime
            self.logger.info(
                "MATCH step=library.external_match title=%r media_id=%s score=%.1f",
                identity.title,
                anime.media_id,
                top_score,
            )
            return anime

        matched_external_paths: set[Path] = set()
        try:
            for root in extra_roots:
                external_items = scan_library(
                    root,
                    self.db,
                    recursive=self.config.library.recursive,
                    ffprobe=self.config.tools.ffprobe,
                    ffmpeg=self.config.tools.ffmpeg,
                    excluded_paths=excluded,
                    pipeline_cache_config=self.config,
                    anime_resolver=resolve_external,
                    media_episode_resolver=lambda anime, release: self._media_episode_from_release(
                        anime, release
                    ),
                    file_episode_resolver=self._scoped_file_media_episode,
                    require_anime_match=True,
                    finished_torrent=finished_torrent,
                )
                items.extend(external_items)
                matched_external_paths.update(item.video_path.resolve() for item in external_items)

            if report is not None:
                report["resolver_incomplete"] = bool(resolver_had_error or resolver_budget_exhausted)
            if resolver_network_queries or resolver_budget_exhausted:
                self.logger.info(
                    "RESULT step=library.external_match_budget queries=%s network_ms=%.1f elapsed_ms=%.1f "
                    "rate_limited=%s budget_exhausted=%s",
                    resolver_network_queries,
                    resolver_network_seconds * 1000.0,
                    (time.monotonic() - resolver_started_at) * 1000.0,
                    resolver_rate_limited,
                    resolver_budget_exhausted,
                )

            # Revalidate auto-imports created by older fuzzy matching, including
            # rows that already reached ``ready``.  The file itself is untouched;
            # only the incorrect DB association/subtitle job is removed. Preserve
            # anything the user actually watched/resumed and never purge on an
            # AniList network failure or when the lookup budget ran out, because
            # absence of a match is not then trustworthy.
            if not resolver_had_error and not resolver_budget_exhausted:
                for stale in list(self.db.episodes()):
                    resolved = stale.video_path.expanduser().resolve()
                    if self._path_within(resolved, ignored):
                        continue
                    if not self._path_within(resolved, tuple(extra_roots)):
                        continue
                    if resolved in matched_external_paths:
                        continue
                    managed_download = (
                        self.db.download_by_hash(stale.torrent_hash) if stale.torrent_hash else None
                    )
                    if managed_download is not None and managed_download.media_id == stale.media_id:
                        continue
                    if stale.state == "watched" or stale.watched_at:
                        continue
                    if float(stale.playback_position or 0.0) > 0.0:
                        continue
                    self.logger.info(
                        "CLEANUP step=library.external_false_match path=%r media_id=%s state=%s",
                        str(resolved),
                        stale.media_id,
                        stale.state,
                    )
                    self.db.delete_episode_record(resolved)
        finally:
            if client is not None:
                client.close()

        self._prune_empty_library_dirs()

        # scan_library may clean or remap historical rows.  Reconcile completed
        # managed downloads *after* the scan as the final authority, so a real
        # downloaded file can never leave the UI as "Episode record missing".
        repaired_completed = self.reconcile_completed_download_rows()
        if repaired_completed:
            self.logger.info(
                "REPAIR step=library.post_scan_completed_downloads rows=%s",
                repaired_completed,
            )

        # A special or OVA the scan split into its own entry: its cover and AniList page
        # now, not after the next AniList refresh (Baccano! Bangai-hen).
        try:
            self.refresh_off_list_entry_metadata(cache_covers=True)
        except Exception as exc:  # noqa: BLE001 - covers never fail a scan
            self.logger.warning("FAIL step=anime.off_list_metadata error=%r", str(exc)[:200])

        self.db.set_state("library_scanned_at", str(time.time()))
        self.log(f"Library: videos found: {len(items)}")
        return items

    # Feed clients outlive a Settings change: they read the address on use.
    def _weekly_rss_url(self) -> str:
        return load_sources(self.config).weekly_rss

    def _release_feed_url(self) -> str:
        return load_sources(self.config).release_feed

    def _recommendations_url(self) -> str:
        return load_sources(self.config).recommendations

    def _release_feeds_configured(self) -> bool:
        """A weekly or public release feed can still find releases without a search source."""
        return any(
            bool(getattr(client, "configured", True))
            for client in (getattr(self, "_subsplease_client", None), getattr(self, "_shana_client", None))
            if client is not None
        )

    def _log_no_release_source(self, step: str) -> None:
        self.logger.info("SKIP step=%s reason=no_release_source", step)

    def nyaa_client(self, *, timeout: float = 20.0) -> NyaaClient:
        sources = load_sources(self.config)
        return NyaaClient(
            sources.release_search,
            mirrors=sources.mirrors,
            proxy_mode=self.config.nyaa.proxy_mode,
            proxy_url=self.config.nyaa.proxy_url,
            pre_search_command=self.config.nyaa.pre_search_command,
            category=self.config.nyaa.category,
            timeout=timeout,
        )

    def downloads_configured(self) -> bool:
        return bool(self.config.qbittorrent.enabled or self.config.aria2.enabled)

    def _torrent_admission(self) -> TorrentAdmission | None:
        path = getattr(self.config, "config_path", None)
        return TorrentAdmission(Path(path)) if path is not None else None

    def _torrent_network_start(self, client: Any, action: str, *args: Any, **kwargs: Any) -> Any:
        """Fence each traffic-starting RPC against the GUI's shared Off transition."""
        gate = self._torrent_admission()
        if gate is None:
            if not self.downloads_enabled():
                raise ManagerError(err("manager.torrentTrafficOff"))
            return getattr(client, action)(*args, **kwargs)
        if not gate.enabled(fallback=bool(self.config.nyaa.torrents_enabled)):
            raise ManagerError(err("manager.torrentTrafficOff"))
        # The pre-download hook (up to 90 s) and reaching or launching
        # qBittorrent run before the cross-process lock: Torrent Off must not
        # wait for them. The gate is checked again under the lock right before
        # the RPC that starts traffic.
        prepare = getattr(client, "prepare_network_start", None)
        if callable(prepare):
            prepare(hook=action == "add_release")
        with gate.locked():
            if not gate.enabled(fallback=bool(self.config.nyaa.torrents_enabled)):
                raise ManagerError(err("manager.torrentTrafficOff"))
            return getattr(client, action)(*args, **kwargs)

    def downloads_enabled(self) -> bool:
        if not self.downloads_configured():
            return False
        gate = self._torrent_admission()
        return bool(
            gate.enabled(fallback=bool(self.config.nyaa.torrents_enabled))
            if gate
            else self.config.nyaa.torrents_enabled
        )

    def torrent_backend_name(self) -> str:
        if self.config.qbittorrent.enabled and self.config.aria2.enabled:
            return "qBittorrent + aria2"
        return "qBittorrent" if self.config.qbittorrent.enabled else "aria2"

    def torrent_paused_on_add(self) -> bool:
        if self.config.qbittorrent.enabled:
            return bool(self.config.qbittorrent.paused_on_add)
        return bool(self.config.aria2.paused_on_add)

    _torrent_start_state = threading.local()

    @contextmanager
    def allow_torrent_app_start(self):
        """Within this block a closed qBittorrent may be launched (adding a torrent, startup)."""
        state = AnimeManager._torrent_start_state
        previous = getattr(state, "depth", 0)
        state.depth = previous + 1
        try:
            yield
        finally:
            state.depth = previous

    @staticmethod
    def _torrent_app_start_allowed() -> bool:
        return getattr(AnimeManager._torrent_start_state, "depth", 0) > 0

    def _shared_qbittorrent_client(self) -> QBittorrentClient:
        """One long-lived qBittorrent client: its session cookie survives between polls.

        Status reads no longer log in every 2 seconds, and they never launch a
        qBittorrent the user closed (see allow_torrent_app_start).
        """
        qbt = self.config.qbittorrent
        key = (
            id(QBittorrentClient),
            qbt.base_url,
            qbt.username,
            qbt.password,
            qbt.api_key,
            bool(qbt.verify_tls),
            qbt.pre_download_command,
            bool(qbt.auto_start_app),
        )
        lock = self.__dict__.setdefault("_shared_qbt_lock", threading.Lock())
        with lock:
            client = self.__dict__.get("_shared_qbt")
            if client is not None and self.__dict__.get("_shared_qbt_key") == key:
                client.category_save_path = self.config.library.root_dir
                return client
            previous = client
            client = QBittorrentClient(
                qbt.base_url,
                qbt.username,
                qbt.password,
                qbt.api_key,
                verify_tls=qbt.verify_tls,
                pre_download_command=qbt.pre_download_command,
                auto_start_app=qbt.auto_start_app,
            )
            try:
                client._shared = True
                client.start_policy = self._torrent_app_start_allowed
                client.category_save_path = self.config.library.root_dir
            except AttributeError:
                return client  # a replacement client class keeps per-call instances
            self._shared_qbt, self._shared_qbt_key = client, key
        if previous is not None:
            closer = getattr(previous, "close_shared", None)
            if callable(closer):
                closer()
        return client

    def qbt_client(self) -> QBittorrentClient | Aria2Client:
        # Keep the historical method name so older integrations/tests remain
        # compatible. The returned object implements the common torrent client
        # surface used by pudge.
        if self.config.qbittorrent.enabled:
            return self._shared_qbittorrent_client()
        if self.config.aria2.enabled:
            aria = self.config.aria2
            return Aria2Client(
                enabled=aria.enabled,
                binary=aria.binary,
                rpc_port=aria.rpc_port,
                pre_download_command=self.config.qbittorrent.pre_download_command,
                paused_on_add=aria.paused_on_add,
                auto_start=aria.auto_start,
                source_proxy_mode=self.config.nyaa.proxy_mode,
                source_proxy_url=self.config.nyaa.proxy_url,
                seed_mode=aria.seed_mode,
                seed_ratio=aria.seed_ratio,
                seed_time_minutes=aria.seed_time_minutes,
                upload_limit_kib=aria.upload_limit_kib,
                vpn_interface=aria.vpn_interface,
                vpn_kill_switch=aria.vpn_kill_switch,
            )
        raise ManagerError(err("manager.allTorrentBackendsDisabled"))

    def ensure_torrent_backend_ready(self) -> str:
        """Prepare the preferred write backend before any write-path reads.

        aria2 is a Pudge-managed sidecar, so a duplicate probe is already a
        backend RPC and must not run before the sidecar has been started or
        recovered. qBittorrent has no equivalent explicit readiness hook.
        """
        if not self.downloads_enabled():
            raise ManagerError(err("manager.torrentTrafficOff"))
        client = self.qbt_client()
        backend = str(getattr(client, "backend_name", "qbittorrent") or "qbittorrent")
        try:
            ensure_running = getattr(client, "ensure_running", None)
            if callable(ensure_running):
                self._torrent_network_start(client, "ensure_running")
            return backend
        finally:
            client.close()

    def torrent_clients(self) -> list[tuple[str, QBittorrentClient | Aria2Client]]:
        """Return every enabled backend; qBittorrent remains the add preference."""
        if self.config.qbittorrent.enabled != self.config.aria2.enabled:
            return [
                (
                    "qbittorrent" if self.config.qbittorrent.enabled else "aria2",
                    self.qbt_client(),
                )
            ]
        result: list[tuple[str, QBittorrentClient | Aria2Client]] = []
        if self.config.qbittorrent.enabled:
            result.append(("qbittorrent", self._shared_qbittorrent_client()))
        if self.config.aria2.enabled:
            aria = self.config.aria2
            result.append(
                (
                    "aria2",
                    Aria2Client(
                        enabled=aria.enabled,
                        binary=aria.binary,
                        rpc_port=aria.rpc_port,
                        pre_download_command=self.config.qbittorrent.pre_download_command,
                        paused_on_add=aria.paused_on_add,
                        auto_start=aria.auto_start,
                        source_proxy_mode=self.config.nyaa.proxy_mode,
                        source_proxy_url=self.config.nyaa.proxy_url,
                        seed_mode=aria.seed_mode,
                        seed_ratio=aria.seed_ratio,
                        seed_time_minutes=aria.seed_time_minutes,
                        upload_limit_kib=aria.upload_limit_kib,
                        vpn_interface=aria.vpn_interface,
                        vpn_kill_switch=aria.vpn_kill_switch,
                    ),
                )
            )
        return result

    def _release_negative_titles_from_graph(
        self,
        anime: LibraryAnime,
    ) -> tuple[str, ...]:
        """Return other media titles from the cached AniList franchise graph.

        The graph is stronger negative identity evidence than season regexes for
        named sequel arcs such as Kimetsu no Yaiba: Yuukaku-hen.  The provider
        owns the matching rule; the manager only supplies the full cached graph.
        """
        lookup = getattr(self.db, "relation_graph_for_media", None)
        if not callable(lookup):
            return ()
        cached = lookup(anime.media_id)
        graph = cached.get("graph") if isinstance(cached, dict) else None
        if not isinstance(graph, dict):
            return ()
        result: list[str] = []
        for node in graph.get("nodes", []):
            if not isinstance(node, dict):
                continue
            try:
                media_id = int(node.get("media_id"))
            except (TypeError, ValueError):
                continue
            if media_id == int(anime.media_id):
                continue
            title = str(node.get("title") or "").strip()
            if title and title not in result:
                result.append(title)
        return tuple(result)

    def _release_episode_context_from_graph(
        self,
        anime: LibraryAnime,
        episode: int,
    ) -> tuple[tuple[int, ...], tuple[str, ...]]:
        cached = self.db.relation_graph_for_media(anime.media_id)
        graph = cached.get("graph") if isinstance(cached, dict) else None
        if not isinstance(graph, dict):
            return (), ()
        result = episode_numbering_from_graph(graph, anime, int(episode))
        if result is None:
            return (), ()
        return result.aliases, result.prequel_titles

    def _media_episode_from_release(
        self,
        anime: LibraryAnime | None,
        release_episode: int | None,
        *,
        requested_media_episode: int | None = None,
        release_name: str = "",
    ) -> int | None:
        if release_name and anime is not None and is_split_stage(anime):
            row = self.db.episode_by_path(Path(release_name).expanduser().resolve())
            if row is not None and row.torrent_hash:
                try:
                    anchor = json.loads(
                        self.db.get_state("release_identity:" + row.torrent_hash.casefold(), "") or "{}"
                    )
                except (TypeError, ValueError):
                    anchor = {}
                if (
                    anchor.get("source") == "user-confirmed-file"
                    and anchor.get("video_path") == str(row.video_path)
                    and anchor.get("media_id") == anime.media_id
                ):
                    return int(anchor["media_episode"])
        return shared_media_episode_from_release(
            anime,
            release_episode,
            self.config,
            self.logger,
            requested_media_episode=requested_media_episode,
            db=self.db,
            allow_network=False,
            release_name=Path(release_name).name if release_name else "",
        )

    def _download_file_media_episode(
        self, anime: LibraryAnime | None, item: Any, release_number: int | None, path: Path
    ) -> int | None:
        """The local episode of one file of a completed download.

        A download named "01-02" (audit 17А) is either one file with both
        episodes or two files; each file must land inside that span.
        """
        span = self._download_span_length(item)
        span_end = int(item.media_episode) + span if span and item.media_episode is not None else None
        # One double file keeps the requested first episode; files of a
        # two-file "01-02" download are each checked against the span.
        file_is_double = double_episode_span(path.name) is not None
        requested = (
            None if item.is_batch or (span_end is not None and not file_is_double) else item.media_episode
        )
        number = self._media_episode_from_release(
            anime, release_number, requested_media_episode=requested, release_name=str(path)
        )
        if number is None and requested is not None and span_end is None:
            number = self._download_name_media_episode(anime, item, release_number, path, requested)
        if span_end is not None and number is not None and item.media_episode is not None:
            if not int(item.media_episode) <= int(number) <= int(span_end):
                return None
        return number

    def _download_name_media_episode(
        self, anime: LibraryAnime | None, item: Any, release_number: int | None, path: Path, requested: int
    ) -> int | None:
        """Map a single-episode download's file through its torrent name.

        A file inside a single-episode torrent often drops what the torrent name
        carries: "Show.S06E07...-Group.mkv" has no storyline title and no
        bracketed uploader, while the torrent name the search matched
        ("[Group] Show S06E07 (Storyline)") maps absolute 7 to local 6. When the
        file carries the same season/episode as the torrent name, the torrent
        name's mapping (still validated against the requested episode) is the file's.
        """
        name = str(getattr(item, "name", "") or "").strip()
        if anime is None or not name or release_number is None or getattr(item, "is_batch", False):
            return None
        file_release = parse_release_name(path.name)
        torrent_release = parse_release_name(name)
        if (
            file_release.unsafe_single_episode
            or torrent_release.unsafe_single_episode
            or not torrent_release.explicit_episode
            or torrent_release.episode is None
            or int(torrent_release.episode) != int(release_number)
            or (file_release.episode is not None and int(file_release.episode) != int(release_number))
            or (file_release.season is not None and file_release.season != torrent_release.season)
        ):
            return None
        number = shared_media_episode_from_release(
            anime,
            int(release_number),
            self.config,
            self.logger,
            requested_media_episode=int(requested),
            db=self.db,
            allow_network=False,
            release_name=name,
        )
        source = "torrent_name"
        if number is None and self._saved_numbering_episode(
            anime, item, int(release_number), int(requested), release_name=name
        ):
            number, source = int(requested), "saved_release_numbering"
        if number is not None and int(number) == int(requested):
            self.logger.debug(
                "RESULT step=download.file_episode media_id=%s release=%s episode=%s source=%s",
                getattr(item, "media_id", None),
                release_number,
                number,
                source,
            )
            return int(number)
        return None

    def _saved_numbering_episode(
        self,
        anime: LibraryAnime | None,
        item: Any,
        release_number: int | None,
        requested: int | None,
        *,
        release_name: str,
    ) -> bool:
        """The download's own resolved numbering still stands when today's is unknown.

        Offline split-stage numbering needs the AniList offset cache (7-day TTL)
        or a stored relation graph. Without either, release 7 of a later stage
        maps to nothing: that is "unknown", not a contradiction. The numbering
        resolved when the release was chosen (saved on the download) then keeps
        the single-episode download linked. A current mapping to another local
        episode still wins (returns False).
        """
        raw = getattr(item, "raw", None) or {}
        saved = raw.get("_release_numbering") if isinstance(raw, dict) else None
        if (
            anime is None
            or getattr(item, "is_batch", False)
            or release_number is None
            or requested is None
            or not isinstance(saved, dict)
            or str(saved.get("numbering_status") or "") != "resolved"
        ):
            return False
        try:
            if int(saved.get("mapped_media_episode")) != int(requested) or int(
                saved.get("raw_release_episode")
            ) != int(release_number):
                return False
        except (TypeError, ValueError):
            return False
        current = shared_media_episode_from_release(
            anime,
            int(release_number),
            self.config,
            self.logger,
            db=self.db,
            allow_network=False,
            release_name=release_name,
        )
        return current is None or int(current) == int(requested)

    def _scoped_file_media_episode(
        self, anime: LibraryAnime, path: Path, requested: int | None
    ) -> int | None:
        if not is_split_stage(anime):
            return requested
        parsed = parse_release_name(path.name)
        return self._media_episode_from_release(anime, parsed.episode, release_name=str(path))

    def _release_episode_context(
        self,
        anime: LibraryAnime,
        episode: int | None,
    ) -> tuple[tuple[int, ...], tuple[str, ...]]:
        if episode is None or int(episode) < 1:
            return (), ()
        result = resolve_episode_numbering(
            anime,
            int(episode),
            self.config,
            self.logger,
            db=self.db,
            allow_network=True,
        )
        return result.aliases, result.prequel_titles

    def planned_release_discovery(self) -> PlannedReleaseDiscovery:
        return PlannedReleaseDiscovery(self)

    def poll_release_feeds(self) -> int:
        """Cheaply retain short RSS windows so release discovery survives feed rotation."""
        if os.getenv("PYTEST_CURRENT_TEST"):
            return 0
        if not self._release_feeds_configured():
            self._log_no_release_source("release_feeds.poll")
            return 0
        saved = 0
        try:
            saved += len(self._subsplease_client.releases(self.config.nyaa.preferred_resolution))
        except NyaaError as exc:
            self.logger.info("SKIP step=subsplease.rss_poll error=%r", str(exc))
        try:
            saved += len(self._shana_client.releases())
        except NyaaError as exc:
            self.logger.info("SKIP step=shana.rss_poll error=%r", str(exc))
        return saved

    def run_intro_detection(self) -> int:
        """One background OP/ED detection step (analysis lives in intro_skipper).

        Part of the maintenance pass (inside the maintenance lock): it waits for the whole
        foreground session, a paused player too. See ``run_paused_intro_detection``.
        """
        return int(self._intro_detection_pass(paused_ok=False).get("analysed") or 0)

    INTRO_REQUEST_RETRY_SECONDS = 30.0
    INTRO_REQUEST_MAX_SECONDS = 30 * 60.0

    def _remember_completed_for_intro(self, paths: Iterable[Path]) -> None:
        """New downloads go first in the next OP/ED pass, whichever process runs it."""
        paths = tuple(paths)
        if not paths:
            return
        from .intro_skipper import detection_enabled, request_analysis

        if not detection_enabled(self.config):
            return
        try:
            request_analysis(self.config.paths.cache_dir, paths)
        except OSError as exc:
            self.logger.warning("FAIL step=intro_skipper.request error=%r", str(exc)[:200])

    def request_intro_detection(self, paths: Iterable[Path], *, reason: str = "download_complete") -> bool:
        """OP/ED detection for just completed videos, soon, on a background thread.

        Never blocks the caller (the foreground poll). The step itself keeps every rule of
        the background pass (foreground, power, the heavy-work lease, the low-RAM worker);
        a deferred step is retried every ``INTRO_REQUEST_RETRY_SECONDS`` for a while, and
        the maintenance pass still picks the files up first later on.
        """
        from .intro_skipper import detection_enabled

        wanted = tuple(Path(path) for path in paths if str(path or "").strip())
        if not wanted or not detection_enabled(self.config):
            return False
        self._remember_completed_for_intro(wanted)
        with self._intro_request_lock:
            for path in wanted:
                self._intro_requested[path] = None
            thread = self._intro_request_thread
            if thread is not None and thread.is_alive():
                return True
            thread = threading.Thread(
                target=self._intro_request_loop, name="pudge-intro-new-episodes", daemon=True
            )
            self._intro_request_thread = thread
        self.logger.info("QUEUE step=intro_skipper.new_episodes videos=%s reason=%s", len(wanted), reason)
        thread.start()
        return True

    def _intro_request_loop(self) -> None:
        from .intro_skipper import analysed

        started = time.monotonic()
        final = {"disabled", "tool_missing", "idle", "error"}
        try:
            while True:
                with self._intro_request_lock:
                    pending = [
                        path
                        for path in self._intro_requested
                        if path.is_file() and not analysed(self.config.paths.cache_dir, path)
                    ]
                    self._intro_requested = dict.fromkeys(pending)
                    if not pending:
                        return
                status = "deferred"
                # Cheap check first: no worker process is started while the pass would wait.
                if self.work_scheduler.background_wait_reason() is None:
                    result = self._intro_detection_pass(paused_ok=False, prefer_paths=pending)
                    status = str(result.get("status") or "error")
                    self.logger.info(
                        "DONE step=intro_skipper.new_episodes status=%s analysed=%s videos=%s",
                        status,
                        result.get("analysed", 0),
                        len(pending),
                    )
                if status in final or time.monotonic() - started >= self.INTRO_REQUEST_MAX_SECONDS:
                    with self._intro_request_lock:
                        for path in pending:
                            self._intro_requested.pop(path, None)
                    continue  # anything requested meanwhile still gets its step
                time.sleep(1.0 if status == "ran" else self.INTRO_REQUEST_RETRY_SECONDS)
        except Exception as exc:  # noqa: BLE001 - optional feature must not break the window
            self.logger.warning("FAIL step=intro_skipper.new_episodes error=%r", str(exc)[:300])
        finally:
            with self._intro_request_lock:
                if self._intro_request_thread is threading.current_thread():
                    self._intro_request_thread = None

    def run_paused_intro_detection(self) -> dict[str, Any]:
        """The agent's own step beside a paused video mpv, after the maintenance lock is gone.

        Holds only the heavy-work lease; never part of an interactive or startup refresh.
        Without a foreground player there is nothing to do here (the maintenance pass runs).
        """
        if self.work_scheduler.foreground_video() is None:
            return {"status": "no_player"}
        return self._intro_detection_pass(paused_ok=True)

    INTRO_WORKER_TIMEOUT_SECONDS = 1800.0

    def _intro_detection_pass(self, *, paused_ok: bool, prefer_paths: Iterable[Path] = ()) -> dict[str, Any]:
        from .intro_skipper import run_background_pass

        prefer = [Path(path) for path in prefer_paths]

        if not paused_ok:
            from . import low_ram

            if low_ram.worker_allowed(self.config):
                # Both modes: numpy and the audio fingerprints load in a worker that
                # exits after the step, not in the window for the rest of the run.
                answer = low_ram.worker_or_inline(
                    "intro_pass",
                    {
                        "config_path": str(self.config.config_path),
                        "incomplete_paths": [str(path) for path in self.incomplete_download_paths()],
                        "prefer_paths": [str(path) for path in prefer],
                    },
                    lambda: None,
                    use_worker=True,
                    logger=self.logger,
                    timeout=self.INTRO_WORKER_TIMEOUT_SECONDS,
                )
                if isinstance(answer, dict):
                    return answer
        try:
            return run_background_pass(
                db=self.db,
                cache_dir=self.config.paths.cache_dir,
                ffmpeg=self.config.tools.ffmpeg,
                ffprobe=self.config.tools.ffprobe,
                scheduler=self.work_scheduler,
                logger=self.logger,
                incomplete_paths=self.incomplete_download_paths(),
                # Detection only feeds the skip button: off with the button.
                enabled=bool(getattr(self.config.playback, "skip_segments_enabled", True)),
                paused_ok=paused_ok,
                **({"prefer_paths": prefer} if prefer else {}),
            )
        except Exception as exc:  # noqa: BLE001 - optional feature must not break the agent
            self.logger.warning("FAIL step=intro_skipper.pass error=%r", str(exc)[:300])
            return {"status": "error"}

    def _seadex_for_search(self, media_id: int, *, automatic: bool) -> SeaDexRecommendation | None:
        """One SeaDex lookup per title search (never per candidate/alias).

        Interactive searches may use the network.  Automatic searches and
        upgrade checks read the local cache only (filled by interactive
        searches): no request on a background path.  Scores carry a context
        key with the SeaDex snapshot, so an upgrade never compares a score
        with a bonus against one computed without it.
        """
        if not getattr(self.config.nyaa, "seadex_enabled", False):
            return None
        if os.environ.get("PUDGE_SEADEX", "1").strip() == "0":
            return None
        client = getattr(self, "_seadex_client", None)
        if client is None:
            from .providers.seadex import SeaDexClient

            client = self._seadex_client = SeaDexClient(
                self.config.paths.cache_dir,
                base_url=self._recommendations_url,
                found_ttl_seconds=max(1.0, float(getattr(self.config.nyaa, "seadex_cache_hours", 24.0)))
                * 3600,
            )
        if not getattr(client, "configured", True):
            return None  # no recommendations address in Settings → Sources
        try:
            with timed_step(self.logger, "seadex.lookup", media_id=media_id, automatic=automatic):
                rec = client.recommendations(
                    int(media_id),
                    allow_network=not automatic,
                    timeout=max(0.5, float(getattr(self.config.nyaa, "seadex_timeout_seconds", 3.0))),
                )
        except Exception as exc:  # noqa: BLE001 - enrichment must never break search
            self.logger.warning("FAIL step=seadex.lookup media_id=%s error=%r", media_id, str(exc)[:200])
            return None
        self.logger.info(
            "RESULT step=seadex.lookup media_id=%s status=%s stale=%s preferred=%s alternative=%s error=%r",
            media_id,
            rec.status,
            rec.stale,
            len(rec.preferred_hashes | rec.preferred_nyaa_ids),
            len(rec.alternative_hashes | rec.alternative_nyaa_ids),
            rec.error,
        )
        return rec if rec.usable else None

    @with_title_memo
    def search_releases(
        self,
        media_id: int,
        *,
        episode: int | None = None,
        batch: bool = False,
        automatic: bool = False,
        allow_anilist_network: bool = True,
    ) -> list[NyaaRelease]:
        _RELEASE_SEARCH_OUTCOME.unavailable = None
        anime = self.db.get_anime(media_id)
        if anime is None:
            raise ManagerError(err("manager.animeNotInLocalDb", mediaId=media_id))
        if not self.config.nyaa.enabled:
            raise ManagerError(err("manager.releaseSearchDisabled"))
        anime = self._with_chain_season(anime)
        if automatic and not batch and episode is not None and self._redownload_blocked(media_id, episode):
            return []

        if not allow_anilist_network and episode is not None and episode >= 1:
            # Planned-release polling is driven by cached premiere metadata.
            # Resolve title/absolute-number aliases locally: no AniList calls.
            context = resolve_episode_numbering(
                anime,
                int(episode),
                self.config,
                self.logger,
                db=self.db,
                allow_network=False,
            )
            alternative_episodes, alternative_titles = context.aliases, context.prequel_titles
        else:
            # A failed numbering lookup is not repeated by automatic searches for a while.
            with automatic_numbering_lookups(automatic):
                alternative_episodes, alternative_titles = self._release_episode_context(anime, episode)
        numbering_context = (
            resolve_episode_numbering(
                anime, int(episode), self.config, self.logger, db=self.db, allow_network=False
            )
            if episode is not None and episode >= 1 and is_split_stage(anime)
            else None
        )
        seadex = self._seadex_for_search(media_id, automatic=automatic)
        common_kwargs = {
            "alternative_episodes": alternative_episodes,
            "alternative_titles": alternative_titles,
            "negative_titles": self._release_negative_titles_from_graph(anime),
            "episode": episode,
            "batch": batch,
            "trusted_groups": self.config.nyaa.trusted_groups,
            "preferred_groups": self.config.nyaa.preferred_groups,
            "blocked_groups": self.config.nyaa.blocked_groups,
            "preferred_resolution": self.config.nyaa.preferred_resolution,
            "preferred_video_codecs": self.config.nyaa.preferred_video_codecs,
            "preferred_sources": self.config.nyaa.preferred_sources,
            "require_japanese_audio": self.config.nyaa.require_japanese_audio,
            "avoid_upscaled": self.config.nyaa.avoid_upscaled,
            "min_seeders": self.config.nyaa.min_seeders,
            "target_episode_min_bytes": self.config.nyaa.episode_min_size_mb * 1024 * 1024,
            "target_episode_max_bytes": self.config.nyaa.episode_max_size_mb * 1024 * 1024,
        }
        if numbering_context is not None:
            common_kwargs["numbering_context"] = numbering_context
        if seadex is not None:
            common_kwargs["seadex"] = seadex

        def search_nyaa_source() -> tuple[list[NyaaRelease], NyaaError | None]:
            client = self.nyaa_client(timeout=6.0 if automatic else 20.0)
            try:
                with timed_step(
                    self.logger,
                    "nyaa.search",
                    media_id=media_id,
                    episode=episode,
                    batch=batch,
                    title=anime.title,
                ):
                    cancellable = (
                        {"should_continue": lambda: not self._release_search_cancelled()}
                        if callable(getattr(_RELEASE_SEARCH_CANCEL, "check", None))
                        else {}
                    )
                    return search_ranked(
                        client,
                        anime,
                        # Automatic refresh should search the same title aliases as
                        # Find episode. The wall-clock budget below keeps background
                        # refresh bounded without silently dropping aliases 4-5.
                        max_queries=5,
                        query_budget_seconds=18.0 if automatic else None,
                        **cancellable,
                        **common_kwargs,
                    ), None
            except NoReleaseSourceError as exc:
                self.logger.info(
                    "SKIP step=nyaa.search media_id=%s episode=%s reason=no_release_source",
                    media_id,
                    episode,
                )
                return [], exc
            except NyaaError as exc:
                self.logger.warning(
                    "FALLBACK step=nyaa.search media_id=%s episode=%s reason=nyaa_error error=%r",
                    media_id,
                    episode,
                    str(exc),
                )
                return [], exc
            finally:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

        def search_subsplease_source(
            *,
            reason: str,
        ) -> tuple[list[NyaaRelease], NyaaError | None]:
            self.logger.info(
                "START step=subsplease.rss_source media_id=%s episode=%s batch=%s reason=%s",
                media_id,
                episode,
                batch,
                reason,
            )
            try:
                with timed_step(
                    self.logger,
                    "subsplease.rss_search",
                    media_id=media_id,
                    episode=episode,
                    batch=batch,
                    title=anime.title,
                ):
                    subsplease_client = getattr(self, "_subsplease_client", None)
                    if subsplease_client is None:
                        subsplease_client = SubsPleaseClient(
                            base_url=self._weekly_rss_url,
                            timeout=8.0,
                            cache_ttl=300.0,
                            history_path=self.config.paths.cache_dir / "release-feeds" / "subsplease.json",
                        )
                        self._subsplease_client = subsplease_client
                    releases = search_subsplease_ranked(
                        subsplease_client,
                        anime,
                        **common_kwargs,
                    )
            except NyaaError as exc:
                self.logger.warning(
                    "FAIL step=subsplease.rss_source media_id=%s episode=%s error=%r",
                    media_id,
                    episode,
                    str(exc),
                )
                return [], exc
            self.logger.info(
                "DONE step=subsplease.rss_source media_id=%s episode=%s candidates=%s",
                media_id,
                episode,
                len(releases),
            )
            return releases, None

        def search_shana_source(
            *,
            reason: str,
        ) -> tuple[list[NyaaRelease], NyaaError | None]:
            self.logger.info(
                "START step=shana.rss_source media_id=%s episode=%s batch=%s reason=%s",
                media_id,
                episode,
                batch,
                reason,
            )
            try:
                with timed_step(
                    self.logger,
                    "shana.rss_search",
                    media_id=media_id,
                    episode=episode,
                    batch=batch,
                    title=anime.title,
                ):
                    shana_client = getattr(self, "_shana_client", None)
                    if shana_client is None:
                        shana_client = ShanaProjectClient(
                            feed_url=self._release_feed_url,
                            timeout=8.0,
                            cache_ttl=600.0,
                            history_path=self.config.paths.cache_dir / "release-feeds" / "shana.json",
                        )
                        self._shana_client = shana_client
                    releases = search_shana_ranked(
                        shana_client,
                        anime,
                        **common_kwargs,
                    )
            except NyaaError as exc:
                self.logger.warning(
                    "FAIL step=shana.rss_source media_id=%s episode=%s error=%r",
                    media_id,
                    episode,
                    str(exc),
                )
                return [], exc
            self.logger.info(
                "DONE step=shana.rss_source media_id=%s episode=%s candidates=%s",
                media_id,
                episode,
                len(releases),
            )
            return releases, None

        def merge_releases(
            first: list[NyaaRelease],
            second: list[NyaaRelease],
        ) -> list[NyaaRelease]:
            by_key: dict[str, NyaaRelease] = {}
            for item in [*first, *second]:
                key = item.info_hash or item.torrent_url or item.link
                by_key.setdefault(key, item)
            return sorted(
                by_key.values(),
                key=lambda item: (item.score, item.seeders, item.downloads),
                reverse=True,
            )

        releases: list[NyaaRelease] = []
        nyaa_error: NyaaError | None = None
        subsplease_error: NyaaError | None = None
        shana_error: NyaaError | None = None

        self.logger.info(
            "SOURCE_ORDER step=release.search media_id=%s episode=%s order=subsplease_then_nyaa_then_shana",
            media_id,
            episode,
        )
        subsplease, subsplease_error = search_subsplease_source(reason="default_source")
        releases = merge_releases(releases, subsplease)
        suitable_rss = any(self._release_is_allowed_for_auto(item) for item in subsplease)
        # With a SeaDex entry, a manual/batch search also asks Nyaa: the
        # recommended release is usually not the SubsPlease one.
        # Automatic paths keep their request budget: no extra Nyaa query.
        seadex_wants_nyaa = bool(not automatic and seadex is not None and seadex.usable)
        if seadex_wants_nyaa and suitable_rss:
            self.logger.info("SOURCE step=release.search media_id=%s reason=seadex_entry_nyaa_too", media_id)
        elif not automatic and suitable_rss:
            # Find episode (the user's own search) always lists Nyaa too: the user may want
            # another group or source than the feed's release.
            self.logger.info("SOURCE step=release.search media_id=%s reason=manual_search_nyaa_too", media_id)
        if (not suitable_rss or not automatic) and self._release_search_cancelled():
            self._log_release_search_cancelled("nyaa.search", media_id=media_id, episode=episode)
        elif not suitable_rss or not automatic:
            nyaa_releases, nyaa_error = search_nyaa_source()
            releases = merge_releases(releases, nyaa_releases)
            suitable_nyaa = any(self._release_is_allowed_for_auto(item) for item in nyaa_releases)
            if not suitable_nyaa and not self._release_search_cancelled():
                shana_releases, shana_error = search_shana_source(reason="subsplease_and_nyaa_empty")
                releases = merge_releases(releases, shana_releases)
        if isinstance(nyaa_error, ReleaseSearchUnavailableError):
            _RELEASE_SEARCH_OUTCOME.unavailable = nyaa_error
        if batch and not automatic and seadex is not None:
            # SeaDex knew the season's preferred releases, but they only added a
            # bonus to search results: with the release search down (or not
            # listing them) the pack was never offered. Offer them as candidates;
            # scoring and the batch safety checks decide as for any other.
            candidate_kwargs = {
                key: value
                for key, value in common_kwargs.items()
                if key not in {"episode", "batch", "seadex", "numbering_context"}
            }
            seadex_releases = seadex_batch_candidates(seadex, anime, **candidate_kwargs)
            if seadex_releases:
                self.logger.info(
                    "SOURCE step=seadex.candidates media_id=%s offered=%s best_score=%.1f",
                    media_id,
                    len(seadex_releases),
                    max(item.score for item in seadex_releases),
                )
                releases = merge_releases(releases, seadex_releases)
        if (
            not releases
            and isinstance(nyaa_error, NoReleaseSourceError)
            # An automatic pass still uses the release feeds when only they are set.
            and (not automatic or not self._release_feeds_configured())
        ):
            raise nyaa_error
        # Every source failing keeps the "release search unavailable" kind.
        failed_type = (
            ReleaseSearchUnavailableError
            if isinstance(nyaa_error, ReleaseSearchUnavailableError)
            else NyaaError
        )
        if (
            not releases
            and subsplease_error is not None
            and nyaa_error is not None
            and shana_error is not None
        ):
            raise failed_type(f"{subsplease_error}; {nyaa_error}; {shana_error}") from shana_error
        if not releases and nyaa_error is not None and subsplease_error is not None:
            raise failed_type(f"{subsplease_error}; {nyaa_error}") from nyaa_error

        if not self.config.nyaa.allow_camrips:
            # Cinema recordings were offered and scored like any release
            # (Planning discovery accepted a movie CAMRIP). Off = never used.
            kept = [item for item in releases if not is_camrip_release(item.title)]
            if len(kept) != len(releases):
                self.logger.info(
                    "SKIP step=release.search media_id=%s episode=%s reason=camrip dropped=%s",
                    media_id,
                    episode,
                    len(releases) - len(kept),
                )
            releases = kept

        # Every candidate of one search was scored with the same inputs.
        score_context = score_context_key(common_kwargs)
        releases = [
            replace(item, score_formula=SCORE_FORMULA_VERSION, score_context=score_context)
            for item in releases
        ]

        if releases:
            best = releases[0]
            self.logger.info(
                "RESULT step=nyaa.search media_id=%s episode=%s candidates=%s best_score=%.1f seeds=%s title=%s source_order=subsplease_then_nyaa_then_shana",
                media_id,
                episode,
                len(releases),
                best.score,
                best.seeders,
                best.title,
            )
        else:
            self.logger.info(
                "RESULT step=nyaa.search media_id=%s episode=%s candidates=0 source_order=subsplease_then_nyaa_then_shana",
                media_id,
                episode,
            )
        return releases

    def _validated_release_identity(
        self,
        anime: LibraryAnime,
        release: NyaaRelease,
        episode: int | None,
        batch: bool,
        *,
        manual: bool = False,
    ) -> NyaaRelease:
        if batch or episode is None or not is_split_stage(anime):
            return release
        context = resolve_episode_numbering(
            anime, episode, self.config, self.logger, db=self.db, allow_network=False
        )
        trusted = release.trusted or release.group in self.config.nyaa.trusted_groups
        match = match_release_episode(
            anime, parse_release_name(release.title), episode, context, trusted_source=trusted
        )
        if (
            manual
            and match.status == "ambiguous"
            and match.rule_id == "continuous-stage"
            and match.proposed_media_episode == episode
            and match.proposed_offset > 0
            and match.scope_group
        ):
            # A manual choice confirms this uploader's continuous numbering for
            # the title; later automatic searches reuse it (forget it from the
            # title's menu).
            self.db.store_release_numbering_rule(
                anime.media_id,
                match.scope_group,
                match.scope_season,
                match.proposed_offset,
                example_title=release.title,
            )
            self.logger.info(
                "RULE step=release_numbering.learned media_id=%s group=%r season=%s offset=%s title=%r",
                anime.media_id,
                match.scope_group,
                match.scope_season,
                match.proposed_offset,
                release.title,
            )
            context = resolve_episode_numbering(
                anime, episode, self.config, self.logger, db=self.db, allow_network=False
            )
            match = match_release_episode(
                anime, parse_release_name(release.title), episode, context, trusted_source=trusted
            )
        if match.status != "resolved" or match.mapped_media_episode != episode:
            raise ManagerError(
                err("manager.releaseIdentityUnresolved", reason=match.rejection_reason or match.status)
            )
        if release.numbering_revision and release.numbering_revision != match.rule_revision:
            raise ManagerError(err("manager.releaseRevisionChanged"))
        return replace(
            release,
            mapped_media_episode=match.mapped_media_episode,
            raw_release_episode=match.raw_release_episode,
            numbering_status=match.status,
            numbering_scheme=match.scheme_id,
            numbering_rule=match.rule_id,
            numbering_revision=match.rule_revision,
        )

    def _queued_release_store(self) -> QueuedReleaseStore:
        store = getattr(self, "queued_releases", None)
        if store is None:
            store = QueuedReleaseStore(self.db)
            self.queued_releases = store
        return store

    def queue_release(
        self,
        media_id: int,
        release: NyaaRelease,
        *,
        episode: int | None,
        batch: bool,
    ) -> dict[str, Any]:
        """Keep a release chosen while Torrents are off; start_queued_releases adds it later."""
        anime = self.db.get_anime(media_id)
        if anime is None:
            raise ManagerError(err("manager.animeNotInDb", mediaId=media_id))
        wanted = str(release.info_hash or "").strip().casefold()
        if wanted and any(
            str(getattr(item, "torrent_hash", "") or "").casefold() == wanted for item in self.db.downloads()
        ):
            return {"queued": False, "already_downloading": True, "entry": None}
        entry, duplicate = self._queued_release_store().add(
            int(media_id), episode, bool(batch), asdict(release), anime_title=str(anime.title or "")
        )
        self.logger.info(
            "WAIT step=release.queued media_id=%s episode=%s batch=%s duplicate=%s title=%r",
            media_id,
            episode,
            bool(batch),
            duplicate,
            release.title,
        )
        return {"queued": True, "already_queued": duplicate, "already_downloading": False, "entry": entry}

    def _queued_release_covered(self, entry: dict[str, Any]) -> bool:
        """The queued episode (or title) got a download or a file meanwhile (picked by hand)."""
        media_id = int(entry.get("media_id") or 0)
        episode = entry.get("episode")
        batch = bool(entry.get("batch"))
        for item in self.db.downloads():
            if item.media_id is None or int(item.media_id) != media_id:
                continue
            if batch:
                if bool(item.is_batch):
                    return True
                continue
            number = item.media_episode if item.media_episode is not None else item.episode
            if episode is not None and number is not None and int(number) == int(episode):
                return True
        if not batch and episode is not None:
            has_episode = getattr(self.db, "has_episode", None)
            if callable(has_episode) and has_episode(media_id, int(episode)):
                return True
        return False

    def _queued_store_call(self, action: str, *args: Any, **kwargs: Any) -> None:
        try:
            getattr(self._queued_release_store(), action)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a locked database must not end the pass
            self.logger.warning("RETRY step=release.queue_%s error=%r", action, str(exc)[:200])

    def start_queued_releases(self) -> int:
        """Add the releases chosen while Torrents were off, through the normal add path.

        One entry is claimed at a time: an add can take a minute, and a claim held
        longer than CLAIM_STALE_SECONDS would be taken over by the other process.
        """
        if not self.downloads_enabled():
            return 0
        store = self._queued_release_store()
        started = 0
        tried: set[str] = set()
        allowed = set(NyaaRelease.__dataclass_fields__)
        while True:
            claimed = store.claim(limit=1, skip=tried)
            if not claimed:
                break
            entry = claimed[0]
            entry_id = str(entry["id"])
            tried.add(entry_id)
            payload = {
                key: value for key, value in dict(entry.get("release") or {}).items() if key in allowed
            }
            episode = entry.get("episode")
            batch = bool(entry.get("batch"))
            media_id = int(entry["media_id"])
            try:
                if self.db.get_anime(media_id) is None:
                    # The title left the library: its queued release never starts.
                    self.logger.info("SKIP step=release.queued media_id=%s reason=anime_removed", media_id)
                    self._queued_store_call("finish", entry)
                    continue
                if self._queued_release_covered(entry):
                    self.logger.info(
                        "SKIP step=release.queued media_id=%s episode=%s batch=%s reason=already_present",
                        media_id,
                        episode,
                        batch,
                    )
                    self._queued_store_call("finish", entry)
                    continue
                self.add_release(
                    media_id,
                    NyaaRelease(**payload),
                    episode=int(episode) if episode is not None else None,
                    batch=batch,
                    manual=True,
                )
            except Exception as exc:  # noqa: BLE001 - one failed entry never blocks the others
                off = not self.downloads_enabled()
                self._queued_store_call(
                    "unclaim", entry_id, error="" if off else str(exc), count_attempt=not off
                )
                self.logger.warning(
                    "FAIL step=release.queued_start media_id=%s episode=%s batch=%s error=%r",
                    media_id,
                    episode,
                    batch,
                    str(exc)[:300],
                )
                if off:
                    break  # Turned off again meanwhile: the rest waits for the next On.
                continue
            self._queued_store_call("finish", entry)
            started += 1
            self.logger.info(
                "EVENT step=release.queued_started media_id=%s episode=%s batch=%s title=%r",
                media_id,
                episode,
                batch,
                payload.get("title"),
            )
        return started

    def forget_queued_release(self, media_id: int, episode: int | None, batch: bool) -> None:
        """A release for this episode was added directly: the queued one must not start too."""
        self._queued_store_call("remove", QueuedReleaseStore.entry_id(media_id, episode, batch))

    @_torrent_app_may_start
    def add_release(
        self,
        media_id: int,
        release: NyaaRelease,
        *,
        episode: int | None,
        batch: bool,
        manual: bool = False,
        upgrade: bool = False,
    ) -> bool:
        if not self.downloads_enabled():
            raise ManagerError(err("manager.torrentsDisabled"))
        anime = self.db.get_anime(media_id)
        if anime is None:
            raise ManagerError(err("manager.animeNotInDb", mediaId=media_id))
        self._ensure_download_space(release.size_bytes)
        release = self._validated_release_identity(anime, release, episode, batch, manual=manual)
        # A quality upgrade or a release the user picked is a *different* copy of
        # an episode that may already be listed (seeding, or an orphaned aria2
        # row): only the same torrent counts as already downloading.
        hash_only = bool((manual or upgrade) and str(release.info_hash or "").strip())
        numbering_metadata = DownloadIntentStore.numbering_metadata(release)
        selected_release_episode = None
        if not batch:
            selected_release_episode = release.raw_release_episode
            if selected_release_episode is None:
                selected_release_episode = parsed_release_episode(release.title)
            if selected_release_episode is None:
                selected_release_episode = episode
        target = self.config.library.root_dir / self._safe_dir_name(anime.title)
        target.mkdir(parents=True, exist_ok=True)
        # Persist the AniList identity next to downloads as a filesystem-level
        # fallback. qBittorrent tags/history can be lost by imports, renames or
        # client-side edits, but a file downloaded from Planning must never
        # become an unrelated movie on the next Library scan.
        try:
            (target / ".anilist.id").write_text(str(anime.media_id), encoding="utf-8")
            self._remember_pudge_sidecar(target, anime.media_id)
        except OSError as exc:
            self.logger.warning(
                "WARN step=library.anilist_sidecar media_id=%s path=%r error=%r",
                anime.media_id,
                str(target),
                str(exc),
            )
        tags = [
            APP_SLUG,
            f"anime: {self._safe_qbt_tag(anime.title)}",
            f"anilist: {anime.media_id}",
        ]
        if episode is not None:
            tags.append(f"episode: {episode}")
        if selected_release_episode is not None and selected_release_episode != episode:
            tags.append(f"release episode: {selected_release_episode}")
        if batch:
            tags.append("series pack")

        # A managed aria2 duplicate probe is itself an RPC. Prepare the preferred
        # write backend before *any* duplicate lookup, otherwise a stopped
        # sidecar prevents us from ever reaching the later add/ensure path.
        self.ensure_torrent_backend_ready()

        # Do not add the same request to the preferred client when it already
        # exists in the other enabled backend. This also covers users migrating
        # gradually from qBittorrent to aria2 (or the other way around).
        for backend_name, candidate_client in self.torrent_clients():
            try:
                existing_any = self._existing_download_for_request(
                    candidate_client,
                    anime,
                    episode=episode,
                    batch=batch,
                    release_hash=release.info_hash,
                    hash_only=hash_only,
                )
                if existing_any is None:
                    continue
                existing_any.media_episode = episode
                existing_any.episode = episode
                existing_any.release_episode = selected_release_episode
                existing_any.raw["backend"] = backend_name
                existing_any.raw["_release_numbering"] = numbering_metadata
                self.db.upsert_download(existing_any)
                paused_on_add = (
                    self.config.qbittorrent.paused_on_add
                    if backend_name == "qbittorrent"
                    else self.config.aria2.paused_on_add
                )
                if not paused_on_add and str(existing_any.state or "").casefold().startswith(
                    ("paused", "stopped")
                ):
                    self._torrent_network_start(candidate_client, "start", existing_any.torrent_hash)
                self._reselect_wanted_files(
                    candidate_client,
                    existing_any.torrent_hash,
                    (episode, selected_release_episode),
                    batch=batch,
                )
                self._record_duplicate_block(media_id, episode, batch, existing_any, backend_name)
                self.log(f"{backend_name}: {anime.title} is already downloading: {existing_any.name}")
                return False
            finally:
                candidate_client.close()
        client = self.qbt_client()
        add_backend = str(getattr(client, "backend_name", "qbittorrent") or "qbittorrent")
        try:
            # The preferred write backend was prepared before the cross-backend
            # duplicate scan above. Reusing a fresh client here must remain a
            # side-effect-free probe before the actual add.
            existing = self._existing_download_for_request(
                client,
                anime,
                episode=episode,
                batch=batch,
                release_hash=release.info_hash,
                hash_only=hash_only,
            )
            if existing is not None:
                existing.media_episode = episode
                existing.episode = episode
                existing.release_episode = selected_release_episode
                existing.raw["_release_numbering"] = numbering_metadata
                repaired = False
                repair = getattr(client, "repair_stalled_release", None)
                if callable(repair):
                    try:
                        repaired = bool(
                            self._torrent_network_start(
                                client, "repair_stalled_release", existing.torrent_hash, release
                            )
                        )
                    except QBittorrentError as exc:
                        self.logger.warning(
                            "FALLBACK step=aria2.metadata_source hash=%s error=%r",
                            existing.torrent_hash,
                            str(exc),
                        )
                    if repaired:
                        self.logger.info(
                            "REPAIR step=aria2.metadata_source hash=%s media_id=%s episode=%s",
                            existing.torrent_hash,
                            media_id,
                            episode,
                        )
                        refreshed = self._existing_download_for_request(
                            client,
                            anime,
                            episode=episode,
                            batch=batch,
                            release_hash=release.info_hash,
                            hash_only=hash_only,
                        )
                        if refreshed is not None:
                            existing = refreshed
                existing.media_episode = episode
                existing.episode = episode
                existing.release_episode = selected_release_episode
                self.db.upsert_download(existing)
                state = str(existing.state or "").casefold()
                if (
                    not self.torrent_paused_on_add()
                    and state.startswith(("paused", "stopped"))
                    and hasattr(client, "start")
                ):
                    self._torrent_network_start(client, "start", existing.torrent_hash)
                    self.logger.info(
                        "REPAIR step=%s.start_existing hash=%s state=%s media_id=%s episode=%s",
                        add_backend,
                        existing.torrent_hash,
                        existing.state,
                        media_id,
                        episode,
                    )
                self._reselect_wanted_files(
                    client, existing.torrent_hash, (episode, selected_release_episode), batch=batch
                )
                self._record_duplicate_block(media_id, episode, batch, existing, add_backend)
                self.log(
                    f"{self.torrent_backend_name()}: {anime.title} is already downloading: {existing.name}"
                )
                return False
            with timed_step(
                self.logger,
                f"{add_backend}.add",
                media_id=media_id,
                episode=episode,
                batch=batch,
                title=release.title,
            ):
                self._torrent_network_start(
                    client,
                    "add_release",
                    release,
                    save_path=target,
                    category=self.config.qbittorrent.category,
                    tags=tags,
                    paused=self.torrent_paused_on_add(),
                )
            self._note_storage_admission(release.info_hash, release.size_bytes)
            if getattr(client, "torrents", None) is not None:
                verified = None
                for delay in (0.0, 0.25, 0.75, 1.5):
                    if delay:
                        time.sleep(delay)
                    verified = self._existing_download_for_request(
                        client,
                        anime,
                        episode=episode,
                        batch=batch,
                        release_hash=release.info_hash,
                        hash_only=hash_only,
                    )
                    if verified is not None:
                        break
                if verified is None:
                    self.logger.error(
                        "ERROR step=%s.add_verify media_id=%s episode=%s batch=%s "
                        "hash=%s title=%r reason=not_visible_after_add",
                        add_backend,
                        media_id,
                        episode,
                        batch,
                        release.info_hash,
                        release.title,
                    )
                    raise ManagerError(err("manager.torrentNotListed", backend=add_backend))
                verified.media_episode = episode
                verified.episode = episode
                verified.release_episode = selected_release_episode
                verified.raw["_release_numbering"] = numbering_metadata
                self.db.upsert_download(verified)
                if not self.torrent_paused_on_add() and hasattr(client, "start"):
                    self._torrent_network_start(client, "start", verified.torrent_hash)
                    self.logger.info(
                        "DONE step=%s.start_after_add hash=%s media_id=%s episode=%s",
                        add_backend,
                        verified.torrent_hash,
                        media_id,
                        episode,
                    )
        finally:
            client.close()
        self.db.record_release(
            release.info_hash or hashlib.sha1(release.magnet.encode()).hexdigest(),
            media_id,
            episode,
            release.title,
            release.score,
            release_episode=selected_release_episode,
            score_formula=getattr(release, "score_formula", ""),
            score_context=getattr(release, "score_context", ""),
            numbering_metadata=numbering_metadata,
        )
        self.log(
            f"{self.torrent_backend_name()}: added {release.title} "
            f"(score={release.score:.1f}, seeds={release.seeders})"
        )
        if manual:
            self._allow_redownload(media_id, None if batch else episode)
            if hash_only and not batch and episode is not None:
                self._record_manual_replacement(media_id, int(episode), release)
            # A release the user picked is never swapped by the stall fallback.
            intents = getattr(self, "download_intents", None)
            if intents is not None:
                intents.drop_stall_fallback(media_id, episode, batch)
        return True

    def _record_duplicate_block(
        self, media_id: int, episode: int | None, batch: bool, item: DownloadItem, backend: str
    ) -> None:
        """An existing torrent covers this request: say which one (log, race and auto-search status)."""
        self.logger.info(
            "SKIP step=torrent.add media_id=%s episode=%s batch=%s reason=already_downloading backend=%s "
            "torrent=%s state=%s progress=%.4f covers=%s orphan=%s name=%r",
            media_id,
            episode,
            batch,
            backend,
            item.torrent_hash,
            item.state,
            float(item.progress or 0.0),
            item.raw.get("_duplicate_covers") or "all",
            bool(item.raw.get("orphaned_metadata")),
            item.name,
        )
        blocks = getattr(self, "_duplicate_blocks", None)
        if blocks is None:
            self._duplicate_blocks = blocks = {}
        blocks[(int(media_id), episode, bool(batch))] = (str(item.torrent_hash or ""), str(item.name or ""))

    def _duplicate_block(
        self, media_id: int, episode: int | None, batch: bool, *, take: bool = False
    ) -> tuple[str, str] | None:
        blocks = getattr(self, "_duplicate_blocks", None) or {}
        key = (int(media_id), episode, bool(batch))
        return blocks.pop(key, None) if take else blocks.get(key)

    def _record_manual_replacement(self, media_id: int, episode: int, release: NyaaRelease) -> None:
        """A release picked for an owned episode replaces it once it is Ready."""
        new_hash = str(release.info_hash or "").strip().lower()
        for current in self.db.episodes(media_id):
            old_hash = str(current.torrent_hash or "").strip().lower()
            if current.episode != episode or not old_hash or old_hash == new_hash:
                continue
            download = self.db.download_by_hash(current.torrent_hash)
            if download is None or download.is_batch:
                continue
            if self.db.episode_count_for_torrent(current.torrent_hash) != 1:
                continue
            record = self.db.release_score_record(media_id, episode, old_hash)
            self.db.record_upgrade(
                new_info_hash=new_hash,
                old_torrent_hash=old_hash,
                media_id=media_id,
                episode=episode,
                old_score=float(record[0]) if record else 0.0,
                new_score=float(release.score),
            )
            self.logger.info(
                "SCHEDULE step=nyaa.manual_replace media_id=%s episode=%s old_hash=%s new_hash=%s",
                media_id,
                episode,
                old_hash,
                new_hash,
            )
            return

    def _redownload_block(self, media_id: int) -> set[int]:
        blocked = getattr(self.db, "redownload_blocked", None)
        return blocked(int(media_id)) if callable(blocked) else set()

    def _redownload_blocked(self, media_id: int, episode: int) -> bool:
        if int(episode) not in self._redownload_block(media_id):
            return False
        self.logger.info(
            "SKIP step=nyaa.auto_search media_id=%s episode=%s reason=deleted_by_user",
            media_id,
            episode,
        )
        return True

    def _allow_redownload(self, media_id: int, episode: int | None) -> None:
        """A download the user asked for lifts the deleted-episode block."""
        allow = getattr(self.db, "allow_redownload", None)
        lifted = allow(int(media_id), episode) if callable(allow) else 0
        # The user asked for it: older misses of these episodes no longer slow their search.
        self._reset_auto_search_backoff(media_id, episode)
        if lifted:
            self.logger.info(
                "EVENT step=anime.redownload_allowed media_id=%s episode=%s lifted=%s reason=manual_download",
                media_id,
                episode if episode is not None else "all",
                lifted,
            )

    def _remember_pudge_sidecar(self, folder: Path, media_id: int) -> None:
        """Pudge's own folder marker, not the person's pin (the library's season-0 rule)."""
        try:
            self.db.set_state(extra_files.pudge_sidecar_state_key(folder), str(int(media_id)))
        except Exception as exc:  # noqa: BLE001 - bookkeeping never blocks a download
            self.logger.warning("WARN step=library.anilist_sidecar_record error=%r", str(exc)[:200])

    @staticmethod
    def _safe_dir_name(value: str) -> str:
        return extra_files.title_folder_name(value)

    @staticmethod
    def _safe_qbt_tag(value: str) -> str:
        # qBittorrent separates tags with commas. Keep the visible title readable.
        clean = " ".join(value.replace(",", " ").split())
        return clean[:120] or "Unknown anime"

    @staticmethod
    def _normalized_title(value: str) -> str:
        return "".join(char for char in value.casefold() if char.isalnum())

    def _resolve_download_media(self, item: DownloadItem) -> None:
        anchor_raw = self.db.get_state("release_identity:" + item.torrent_hash.casefold(), "")
        try:
            anchor = json.loads(anchor_raw) if anchor_raw else {}
        except (TypeError, ValueError):
            anchor = {}
        if anchor.get("source") == "user-confirmed-file":
            item.media_id = int(anchor["media_id"])
            item.media_episode = item.episode = int(anchor["media_episode"])
            item.release_episode = int(anchor["release_episode"])
            item.raw["_release_identity"] = anchor
            item.raw.pop("_identity_conflict", None)
            return
        persisted = self.db.download_by_hash(item.torrent_hash)
        if persisted is not None:
            if persisted.raw.get("_release_numbering"):
                item.raw["_release_numbering"] = persisted.raw["_release_numbering"]
            for key in ("_intent_revision", "_acquisition_kind", "_acquisition_target"):
                if key in persisted.raw:
                    item.raw[key] = persisted.raw[key]
            if item.media_id is None:
                item.media_id = persisted.media_id
            if persisted.media_episode is not None:
                item.media_episode = persisted.media_episode
                item.episode = persisted.media_episode
            if persisted.release_episode is not None:
                item.release_episode = persisted.release_episode
            if persisted.media_episode_end is not None and item.media_episode == persisted.media_episode:
                item.media_episode_end = persisted.media_episode_end
        history = self.db.release_metadata_by_hash(item.torrent_hash)
        if history is not None:
            history_media_id, history_media_episode, history_release_episode, history_score = history
            if item.media_id is None:
                item.media_id = history_media_id
                item.raw["_media_id_source"] = "history"
            if item.media_episode is None and history_media_episode is not None:
                item.media_episode = history_media_episode
                item.episode = history_media_episode
            if history_release_episode is not None:
                item.release_episode = history_release_episode
            item.raw["_release_score_history"] = history_score
        if item.media_id is not None:
            # Derived on every resolve: a flag persisted in raw_json by an
            # earlier pass must not block this download forever.
            item.raw.pop("_identity_conflict", None)
            anime = self.db.get_anime(item.media_id)
            if anime is not None and is_split_stage(anime) and not item.is_batch:
                mapped = self._media_episode_from_release(
                    anime,
                    item.release_episode,
                    requested_media_episode=item.media_episode,
                    release_name=item.name,
                )
                if mapped is None and not self._saved_numbering_episode(
                    anime,
                    item,
                    item.release_episode,
                    item.media_episode,
                    release_name=Path(str(item.name or "")).name,
                ):
                    item.raw["_identity_conflict"] = "Saved release identity contradicts current numbering"
            item.raw.setdefault("_media_id_source", "tag")
            return
        tagged_title = str(item.raw.get("_anime_title_tag") or "").strip()
        parsed_title = parse_anime_filename(str(item.name or "")).title
        candidates = [value for value in (tagged_title, parsed_title) if value]
        if not candidates:
            return
        best_anime = None
        best_score = 0.0
        for anime in self.db.anime_list():
            names = [anime.title, *anime.titles, *anime.synonyms]
            for candidate in candidates:
                wanted = self._normalized_title(candidate)
                if wanted and any(self._normalized_title(name) == wanted for name in names if name):
                    item.media_id = anime.media_id
                    item.raw["_media_id_source"] = (
                        "anime_title_tag" if tagged_title and candidate == tagged_title else "exact_title"
                    )
                    return
                score = max(
                    (title_similarity(candidate, name) for name in names if name),
                    default=0.0,
                )
                if score > best_score:
                    best_anime = anime
                    best_score = score
        if best_anime is not None and best_score >= 82:
            item.media_id = best_anime.media_id
            item.raw["_media_id_source"] = "fuzzy_title"
            item.raw["_media_id_score"] = round(best_score, 2)

    def _existing_download_for_request(
        self,
        client: QBittorrentClient,
        anime: LibraryAnime,
        *,
        episode: int | None,
        batch: bool,
        release_hash: str = "",
        hash_only: bool = False,
    ):
        torrents = getattr(client, "torrents", None)
        if torrents is None:
            return None
        wanted_hash = str(release_hash or "").strip().casefold()
        hash_only = bool(hash_only and wanted_hash)
        invalid_states = {"error", "missingfiles", "unknown"}
        for item in torrents(category=""):
            raw_hashes = {
                str(item.torrent_hash or "").strip().casefold(),
                str(item.raw.get("infohash_v1") or "").strip().casefold(),
                str(item.raw.get("infohash_v2") or "").strip().casefold(),
            }
            same_hash = bool(wanted_hash and wanted_hash in raw_hashes)
            if hash_only and not same_hash:
                # Another copy of this episode (the release being upgraded, or
                # the one the user chose to replace) is not this request.
                continue
            self._resolve_download_media(item)
            source = str(item.raw.get("_media_id_source") or "unknown")
            state = str(item.state or "").strip().casefold()
            self.logger.info(
                "CANDIDATE step=qbittorrent.duplicate_check media_id=%s torrent=%s "
                "resolved_media_id=%s source=%s state=%s progress=%.4f episode=%s "
                "batch=%s same_hash=%s name=%r",
                anime.media_id,
                item.torrent_hash,
                item.media_id,
                source,
                item.state,
                float(item.progress or 0.0),
                item.episode,
                item.is_batch,
                same_hash,
                item.name,
            )
            recoverable_aria2_error = bool(
                state == "error" and (item.raw or {}).get("recoverable_missing_control")
            )
            if state in invalid_states and not recoverable_aria2_error:
                self.logger.info(
                    "SKIP step=qbittorrent.duplicate_check torrent=%s reason=invalid_state state=%s",
                    item.torrent_hash,
                    item.state,
                )
                continue
            if not same_hash and item.media_id != anime.media_id:
                continue

            # Fuzzy title inference is useful for housekeeping old torrents, but
            # it is not reliable enough to block a user-requested download. Exact
            # title inference is accepted only once data actually started moving.
            if not same_hash and source == "fuzzy_title":
                self.logger.info(
                    "SKIP step=qbittorrent.duplicate_check torrent=%s reason=fuzzy_only score=%s",
                    item.torrent_hash,
                    item.raw.get("_media_id_score"),
                )
                continue
            if not same_hash and source == "exact_title" and float(item.progress or 0.0) < 0.01:
                self.logger.info(
                    "SKIP step=qbittorrent.duplicate_check torrent=%s reason=unlinked_zero_progress",
                    item.torrent_hash,
                )
                continue
            # aria2 forgot this finished torrent; only its file on disk is left. It
            # holds back a download only when the requested episode is on disk.
            if (
                not batch
                and not same_hash
                and episode is not None
                and item.raw.get("orphaned_metadata")
                and not self._orphan_holds_episode(item, anime, int(episode))
            ):
                self.logger.info(
                    "SKIP step=qbittorrent.duplicate_check torrent=%s reason=orphan_episode_not_in_library "
                    "episode=%s orphan_episode=%s",
                    item.torrent_hash,
                    episode,
                    item.episode,
                )
                continue

            request_matches = False
            covers = "all"
            if batch:
                request_matches = bool(item.is_batch or anime.format == "MOVIE" or item.episode is None)
            elif item.is_batch:
                numbers = self._batch_release_numbers(client, item)
                # Unknown contents: a series pack may hold the episode (kept as before).
                request_matches = numbers is None or self._batch_covers_episode(anime, item, numbers, episode)
                if numbers is not None:
                    covers = self._episode_numbers_text(numbers)
                    if not request_matches:
                        self.logger.info(
                            "SKIP step=qbittorrent.duplicate_check torrent=%s reason=batch_does_not_cover "
                            "episode=%s covers=%s",
                            item.torrent_hash,
                            episode,
                            covers,
                        )
            else:
                request_matches = item.episode == episode
                covers = str(item.episode)
            if not request_matches:
                continue
            item.raw["_duplicate_covers"] = covers
            self.logger.info(
                "MATCH step=qbittorrent.duplicate_check media_id=%s torrent=%s source=%s "
                "state=%s progress=%.4f covers=%s",
                anime.media_id,
                item.torrent_hash,
                source,
                item.state,
                float(item.progress or 0.0),
                covers,
            )
            return item
        self.logger.info(
            "RESULT step=qbittorrent.duplicate_check media_id=%s episode=%s batch=%s hash_only=%s match=none",
            anime.media_id,
            episode,
            batch,
            hash_only,
        )
        return None

    def _orphan_holds_episode(self, item: DownloadItem, anime: LibraryAnime, episode: int) -> bool:
        """The episode's file is on disk: the orphan's own file, or one in the library."""
        if item.episode == episode and Path(str(item.content_path or "")).is_file():
            return True
        for row in self.db.episodes(anime.media_id):
            first = row.media_episode if row.media_episode is not None else row.episode
            if first is None:
                continue
            last = getattr(row, "media_episode_end", None) or first
            if int(first) <= episode <= int(last) and row.video_path.is_file():
                return True
        return False

    @staticmethod
    def _batch_release_numbers(client: Any, item: DownloadItem) -> set[int] | None:
        """Episode numbers of a series pack's video files as named in the torrent (None: unknown)."""
        numbers = {int(value) for value in item.raw.get("video_episodes") or []}
        files_of = getattr(client, "files", None)
        if not numbers and callable(files_of) and not item.raw.get("orphaned_metadata"):
            try:
                entries = files_of(item.torrent_hash) or []
            except (QBittorrentError, OSError, ValueError):
                entries = []
            for entry in entries:
                name = Path(str((entry or {}).get("name") or (entry or {}).get("path") or "")).name
                if Path(name).suffix.casefold() in VIDEO_EXTENSIONS:
                    number = parsed_release_episode(name)
                    if number is not None:
                        numbers.add(int(number))
        if not numbers:
            span = release_episode_range(str(item.name or ""))
            if span is not None:
                numbers = set(range(span[0], span[1] + 1))
        return numbers or None

    def _batch_covers_episode(
        self, anime: LibraryAnime, item: DownloadItem, numbers: set[int], episode: int | None
    ) -> bool:
        """The pack holds the episode, in season-local or absolute numbering (S03E49 = episode 1)."""
        if episode is None:
            return True
        wanted = {int(episode)}
        if item.release_episode is not None and item.media_episode is not None:
            wanted.add(int(episode) + int(item.release_episode) - int(item.media_episode))
        try:
            numbering = resolve_episode_numbering(
                anime, int(episode), self.config, self.logger, db=self.db, allow_network=False
            )
            wanted.update(int(value) for value in numbering.aliases)
        except Exception as exc:  # noqa: BLE001 - numbering is a hint here
            self.logger.info("FALLBACK step=qbittorrent.duplicate_numbering error=%r", str(exc)[:200])
        if wanted & numbers:
            return True
        total = int(anime.episodes or 0)
        # Absolute numbers (all past the season's end) that could not be mapped: unknown.
        return len(wanted) == 1 and bool(total) and min(numbers) > total

    @staticmethod
    def _episode_numbers_text(numbers: set[int]) -> str:
        ordered = sorted(numbers)
        if ordered[-1] - ordered[0] + 1 == len(ordered):
            return str(ordered[0]) if len(ordered) == 1 else f"{ordered[0]}-{ordered[-1]}"
        return ",".join(str(value) for value in ordered[:12]) + ("..." if len(ordered) > 12 else "")

    @staticmethod
    def _torrent_tags(item: DownloadItem) -> set[str]:
        raw_tags = item.raw.get("_tag_set")
        if isinstance(raw_tags, list):
            return {str(tag).strip() for tag in raw_tags if str(tag).strip()}
        return {value.strip() for value in str(item.raw.get("tags") or "").split(",") if value.strip()}

    def _download_is_managed(self, item: DownloadItem) -> bool:
        tags = {tag.casefold() for tag in self._torrent_tags(item)}
        category = str(item.raw.get("category") or "").strip().casefold()
        tagged = any(
            tag.startswith(("anime:", "anilist:", "anilist-", "episode:", "episode-", "score:"))
            or tag in {"batch", "series pack"}
            for tag in tags
        )
        return bool(
            tagged
            or category == self.config.qbittorrent.category.casefold()
            or self.db.download_by_hash(item.torrent_hash) is not None
            or self.db.release_metadata_by_hash(item.torrent_hash) is not None
        )

    def _duplicate_identity(self, item: DownloadItem) -> tuple[str, str] | None:
        if not self._download_is_managed(item):
            return None
        tags = self._torrent_tags(item)
        tagged_title = next(
            (tag.split(":", 1)[1].strip() for tag in tags if tag.casefold().startswith("anime:")),
            "",
        )
        anime = self.db.get_anime(item.media_id) if item.media_id is not None else None
        if item.media_id is not None:
            anime_key = f"anilist:{int(item.media_id)}"
        else:
            title = tagged_title or (anime.title if anime is not None else "")
            title_key = self._normalized_title(title)
            if not title_key:
                return None
            anime_key = f"title:{title_key}"

        # Movies are one logical instance even when an older torrent lacks the
        # modern "series pack" tag. For TV entries, batch and episode downloads
        # must never be mixed in the same duplicate group.
        if anime is not None and anime.format == "MOVIE":
            kind = "movie"
        elif item.episode is not None:
            kind = f"episode:{int(item.episode)}"
        elif item.is_batch or any(tag.casefold() in {"batch", "series pack"} for tag in tags):
            # Two packs of one entry ("01-12" and "13-24") are not duplicates: the second one
            # (with its files) was deleted as the "loser" of the first.
            span = release_episode_range(str(item.name or ""))
            kind = "batch" if span is None else f"batch:{int(span[0])}-{int(span[1])}"
        else:
            return None
        return anime_key, kind

    def _download_release_score(self, item: DownloadItem) -> float | None:
        for key in ("_release_score_history", "_release_score_tag"):
            raw = item.raw.get(key)
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
        if item.media_id is None:
            return None
        return self.db.release_score(
            item.media_id,
            item.episode,
            item.torrent_hash,
        )

    def _download_is_linked(self, item: DownloadItem) -> bool:
        return self.db.download_by_hash(item.torrent_hash) is not None

    def _choose_duplicate_winner(
        self, items: list[DownloadItem]
    ) -> tuple[DownloadItem | None, list[DownloadItem]]:
        completed = [item for item in items if self._download_is_complete(item)]
        if completed:
            # Completed data is immutable from automatic duplicate cleanup.
            # Keep every completed copy and only remove incomplete duplicates.
            winner = max(
                completed,
                key=lambda item: (
                    self._download_release_score(item) or float("-inf"),
                    int(self._download_is_linked(item)),
                    item.added_on,
                ),
            )
            return winner, [item for item in items if not self._download_is_complete(item)]

        if len(items) < 2:
            return None, []
        progress_sorted = sorted(items, key=lambda item: item.progress, reverse=True)
        progress_gap = progress_sorted[0].progress - progress_sorted[1].progress
        scores = {item.torrent_hash: self._download_release_score(item) for item in items}
        known_scores = [value for value in scores.values() if value is not None]

        # A torrent that is at least 20 percentage points further along wins;
        # this avoids throwing away a large partial download for a modest score gain.
        if progress_gap >= 0.20:
            winner = progress_sorted[0]
            return winner, [item for item in items if item is not winner]

        if len(known_scores) == len(items):
            ranked = sorted(
                items,
                key=lambda item: (
                    item.progress * 100.0
                    + float(scores[item.torrent_hash]) * 0.35
                    + (3.0 if self._download_is_linked(item) else 0.0),
                    item.progress,
                    float(scores[item.torrent_hash]),
                ),
                reverse=True,
            )
            top_value = (
                ranked[0].progress * 100.0
                + float(scores[ranked[0].torrent_hash]) * 0.35
                + (3.0 if self._download_is_linked(ranked[0]) else 0.0)
            )
            second_value = (
                ranked[1].progress * 100.0
                + float(scores[ranked[1].torrent_hash]) * 0.35
                + (3.0 if self._download_is_linked(ranked[1]) else 0.0)
            )
            if top_value - second_value >= 5.0:
                winner = ranked[0]
                return winner, [item for item in items if item is not winner]

        # When scores are missing, only a meaningful progress lead or one unique
        # pudge-linked item is enough evidence. Otherwise preserve both.
        if progress_gap >= 0.05:
            winner = progress_sorted[0]
            return winner, [item for item in items if item is not winner]
        linked = [item for item in items if self._download_is_linked(item)]
        if len(linked) == 1:
            winner = linked[0]
            return winner, [item for item in items if item is not winner]
        return None, []

    @staticmethod
    def _paths_overlap(first: str, second: str) -> bool:
        if not first.strip() or not second.strip():
            return True
        try:
            left = Path(first).expanduser().resolve(strict=False)
            right = Path(second).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return True
        return left == right or left in right.parents or right in left.parents

    def _can_delete_duplicate_files(
        self,
        loser: DownloadItem,
        preserved: list[DownloadItem],
    ) -> bool:
        if self._download_is_complete(loser) or not loser.content_path.strip():
            return False
        return all(
            not self._paths_overlap(loser.content_path, item.content_path)
            for item in preserved
            if item.torrent_hash != loser.torrent_hash
        )

    def _duplicate_loser_holds_library_files(self, client: Any, loser: DownloadItem) -> bool:
        """Finished files of the torrent on disk, or Library episodes inside its content."""
        if self._torrent_files_kept_on_disk(
            client,
            loser.torrent_hash,
            loser,
            set(),
            self._torrent_snapshot(client, loser.torrent_hash),
            complete_only=True,
        ):
            return True
        content = str(loser.content_path or "").strip()
        if not content:
            return False
        content_forms = self._path_forms(content)
        for row in self.db.episodes():
            video_forms = self._path_forms(row.video_path)
            for base in content_forms:
                if any(value == base or value.startswith(base.rstrip("/") + "/") for value in video_forms):
                    self.logger.info(
                        "KEEP step=qbittorrent.duplicate_cleanup reason=library_episode_inside hash=%s video=%r",
                        loser.torrent_hash,
                        str(row.video_path),
                    )
                    return True
        return False

    def _metadata_tags_for_download(self, item: DownloadItem, score: float | None) -> list[str]:
        anime = self.db.get_anime(item.media_id) if item.media_id is not None else None
        if anime is None:
            return []
        tags = [
            APP_SLUG,
            f"anime: {self._safe_qbt_tag(anime.title)}",
            f"anilist: {anime.media_id}",
        ]
        if item.episode is not None:
            tags.append(f"episode: {item.episode}")
        elif item.is_batch or anime.format == "MOVIE":
            tags.append("series pack")
        return tags

    def cleanup_duplicate_torrents(self) -> int:
        """Remove only clear incomplete duplicate torrents managed by pudge.

        Duplicate identity comes from pudge's anime/episode/batch tags. Every
        completed torrent is protected. Ambiguous partial pairs are left intact.
        """
        if not self.downloads_enabled():
            return 0
        upgrade_hashes = {
            str(job["new_info_hash"] or "").strip().lower() for job in self.db.pending_upgrades()
        }
        client = self.qbt_client()
        removed = 0
        try:
            items = client.torrents(category="")
            groups: dict[tuple[str, str], list[DownloadItem]] = {}
            for item in items:
                self._resolve_download_media(item)
                identity = self._duplicate_identity(item)
                if identity is None:
                    continue
                groups.setdefault(identity, []).append(item)

            for identity, duplicates in groups.items():
                if len(duplicates) < 2:
                    continue
                winner, losers = self._choose_duplicate_winner(duplicates)
                if winner is None or not losers:
                    self.logger.info(
                        "SKIP step=qbittorrent.duplicate_cleanup identity=%s reason=ambiguous hashes=%s",
                        identity,
                        [item.torrent_hash for item in duplicates],
                    )
                    continue

                # Make the retained torrent the canonical pudge-linked copy,
                # even when it was created by an older version without a category.
                winner_score = self._download_release_score(winner)
                metadata_tags = self._metadata_tags_for_download(winner, winner_score)
                if metadata_tags:
                    setter = getattr(client, "set_metadata", None)
                    if setter is not None:
                        try:
                            setter(
                                winner.torrent_hash,
                                category=self.config.qbittorrent.category,
                                tags=metadata_tags,
                            )
                        except QBittorrentError as exc:
                            self.logger.warning(
                                "WARN step=qbittorrent.duplicate_link hash=%s error=%s",
                                winner.torrent_hash,
                                exc,
                            )
                self.db.upsert_download(winner)

                completed_copies = [item for item in duplicates if self._download_is_complete(item)]
                preserved = completed_copies or [winner]
                for loser in losers:
                    if self._download_is_complete(loser):
                        continue
                    if loser.torrent_hash.lower() in upgrade_hashes:
                        # An upgrade or a manual replacement downloads next to
                        # the completed copy with the same tags. Deleting it
                        # lost the download and left its upgrade job blocking
                        # new upgrade checks.
                        self.logger.info(
                            "SKIP step=qbittorrent.duplicate_cleanup identity=%s hash=%s reason=pending_upgrade",
                            identity,
                            loser.torrent_hash,
                        )
                        continue
                    delete_files = self._can_delete_duplicate_files(loser, preserved)
                    if delete_files and self._duplicate_loser_holds_library_files(client, loser):
                        # An unfinished batch can hold finished, imported episodes: the
                        # torrent goes, its files stay.
                        delete_files = False
                    try:
                        client.delete(loser.torrent_hash, delete_files=delete_files)
                    except QBittorrentError as exc:
                        self.logger.warning(
                            "WARN step=qbittorrent.duplicate_cleanup hash=%s error=%s",
                            loser.torrent_hash,
                            exc,
                        )
                        continue
                    self.db.delete_torrent_records(loser.torrent_hash)
                    self.logger.info(
                        "DONE step=qbittorrent.duplicate_cleanup identity=%s kept_hash=%s removed_hash=%s kept_progress=%.3f removed_progress=%.3f kept_score=%s removed_score=%s delete_files=%s",
                        identity,
                        winner.torrent_hash,
                        loser.torrent_hash,
                        winner.progress,
                        loser.progress,
                        winner_score,
                        self._download_release_score(loser),
                        delete_files,
                    )
                    removed += 1
        finally:
            client.close()
        return removed

    # aria2 tries candidates one at a time. A magnet often needs well over ten
    # seconds just for its metadata, and metadata plus a peer connection is not
    # data yet: a 6-second window dropped a healthy 9-seed batch and then "won"
    # with a 2-seed pack whose peers never sent a byte, stuck at 0 % for good.
    ARIA2_RACE_METADATA_SECONDS = 40.0
    ARIA2_RACE_STARTING_SECONDS = 20.0
    # aria2 runs 3 downloads at once and queues the rest ("waiting": no peers can
    # be asked yet). A candidate still queued after this is kept as the choice
    # with its fallbacks recorded instead of being judged dead.
    ARIA2_RACE_QUEUED_SECONDS = 5.0
    # A chosen aria2 release that has not received one byte for this long is
    # swapped for the next candidate its race recorded.
    ARIA2_STALL_REPLACE_SECONDS = 600.0

    @staticmethod
    def _torrent_race_is_alive(snapshot: dict[str, Any] | None) -> bool:
        if not snapshot:
            return False
        try:
            speed = int(snapshot.get("dlspeed") or snapshot.get("download_speed") or 0)
            downloaded = int(snapshot.get("downloaded") or 0)
            progress = float(snapshot.get("progress") or 0.0)
        except (TypeError, ValueError):
            return False
        # Only data actually flowing proves a live swarm. Metadata plus a
        # connection used to count too (see _torrent_race_is_starting).
        return bool(speed > 0 or downloaded >= 256 * 1024 or progress > 0.001)

    @staticmethod
    def _torrent_race_is_starting(snapshot: dict[str, Any] | None) -> bool:
        """Metadata and a peer connection but no data yet: maybe alive, not proven."""
        if not snapshot:
            return False
        try:
            total = int(snapshot.get("total_size") or snapshot.get("totalLength") or 0)
            connections = int(snapshot.get("connections") or snapshot.get("num_connections") or 0)
        except (TypeError, ValueError):
            return False
        return total > 0 and connections > 0

    @staticmethod
    def _release_record(release: NyaaRelease) -> dict[str, Any]:
        return asdict(release)

    @staticmethod
    def _release_from_record(record: Any) -> NyaaRelease | None:
        if not isinstance(record, dict):
            return None
        allowed = set(NyaaRelease.__dataclass_fields__)
        try:
            return NyaaRelease(**{key: value for key, value in record.items() if key in allowed})
        except TypeError:
            return None

    @staticmethod
    def _race_pool(candidates: list[NyaaRelease], *, batch: bool) -> list[NyaaRelease]:
        """The best three, and for a season pack up to two well-seeded ones after them.

        Baccano!: the three best packs (an old 1080p one and two DVDs) gave no data, the
        1080p pack with 171 seeders was fifth and never tried, and a dead DVD was kept.
        """
        pool = list(candidates[:3])
        if batch:
            known = [int(item.seeders or 0) for item in pool if getattr(item, "seeders_known", True)]
            floor = max(10, 2 * max(known, default=0))
            pool.extend([item for item in candidates[3:] if int(item.seeders or 0) >= floor][:2])
        return pool

    def _race_add_candidates_aria2(
        self,
        media_id: int,
        candidates: list[NyaaRelease],
        *,
        episode: int | None,
        batch: bool,
        fast_seconds: float = 10.0,
        poll_seconds: float = 1.0,
        intent_revision: int | None = None,
        stalled_hashes: Iterable[str] = (),
    ) -> NyaaRelease | None:
        """Try aria2 candidates sequentially so they never write the same target concurrently."""

        stalled = {str(value or "").casefold() for value in stalled_hashes if value}
        # A release that already stalled for this request is tried last.
        pool = sorted(
            self._race_pool(candidates, batch=batch),
            key=lambda item: str(item.info_hash or "").casefold() in stalled,
        )
        metadata_window = max(1.0, float(fast_seconds), self.ARIA2_RACE_METADATA_SECONDS)
        starting_window = max(1.0, float(fast_seconds), self.ARIA2_RACE_STARTING_SECONDS)
        # Hashes that showed neither data nor a peer in their window.
        dead: set[str] = set()
        # Metadata and peers but no data: removed to free the shared target,
        # the best one comes back when nothing proved alive.
        starting: list[NyaaRelease] = []
        candidate_started = time.monotonic()
        race_anime = self.db.get_anime(media_id)
        race_target = self.config.library.root_dir / self._safe_dir_name(
            race_anime.title if race_anime is not None else str(media_id)
        )

        def fail(
            detail: str,
            release: NyaaRelease | None = None,
            *,
            reason: str = "status_error",
            candidate: NyaaRelease | None = None,
        ) -> None:
            # Every exit of the race says why. A removal that aria2 did not
            # confirm used to end it without a line, and Planning then went on
            # with one search per episode as if no pack existed.
            tried = candidate or release
            self.logger.warning(
                "FAIL step=nyaa.race backend=aria2 media_id=%s episode=%s candidate=%s reason=%s "
                "title=%r error=%r",
                media_id,
                episode,
                pool.index(tried) + 1 if tried in pool else "-",
                reason,
                tried.title if tried is not None else "",
                detail,
            )
            self.download_intents.update(
                media_id,
                episode,
                batch,
                state="recovery_required",
                selected=release,
                backend="aria2" if release is not None else None,
                detail=detail,
                expected_revision=intent_revision,
            )

        def fallback(chosen: NyaaRelease) -> dict[str, Any]:
            # The other candidates that are not proven dead, in ranked order: a
            # stall of the chosen one later moves on to them.
            chosen_hash = str(chosen.info_hash or "").casefold()
            others = [
                self._release_record(item)
                for item in pool
                if str(item.info_hash or "").casefold() not in {"", chosen_hash, *dead, *stalled}
            ]
            return {"selected": self._release_record(chosen), "candidates": others} if others else {}

        def best_starting() -> NyaaRelease:
            # Most listed seeders; one that already stalled only when it is the
            # only one with peers. max() keeps the higher-ranked one on ties.
            return max(
                starting,
                key=lambda item: (
                    str(item.info_hash or "").casefold() not in stalled,
                    int(item.seeders or 0),
                ),
            )

        def select(
            release: NyaaRelease,
            snapshot: dict[str, Any] | None,
            *,
            outcome: str = "winner",
            detail: str = "",
        ) -> NyaaRelease:
            self.release_telemetry.record(
                release,
                media_id=media_id,
                media_episode=episode,
                provider="aria2",
                outcome=outcome,
                snapshot=snapshot,
                elapsed_seconds=time.monotonic() - candidate_started,
            )
            self.download_intents.update(
                media_id,
                episode,
                batch,
                state="downloading",
                selected=release,
                backend="aria2",
                detail=detail,
                expected_revision=intent_revision,
                owned_candidates=[],
                stall_fallback=fallback(release),
            )
            return release

        def add(index: int, release: NyaaRelease) -> tuple[bool, NyaaRelease | None]:
            """Add one candidate: (True, None) to watch it, else (False, the race result)."""
            nonlocal candidate_started
            probe_client = self.qbt_client()
            try:
                if probe_client.backend_name == "aria2":
                    # The status probe below is read-only and refuses a stopped
                    # aria2; the add after it would start it, but the probe failed
                    # first and the episode was never added (aria2 gone after sleep
                    # or a crash, without the idle marker). Adding is a write, so
                    # starting the sidecar here is what the user asked for.
                    probe_client.ensure_running()
                existing = probe_client.torrent_status(release.info_hash) if release.info_hash else None
            except QBittorrentError as exc:
                fail(str(exc), candidate=release)
                return False, None
            finally:
                probe_client.close()
            owned = []
            if release.info_hash and existing is None:
                anime = self.db.get_anime(media_id)
                if anime is None:  # removed meanwhile: a ManagerError the pass handles, not AttributeError
                    raise ManagerError(err("manager.animeNotInDb", mediaId=media_id))
                target = self.config.library.root_dir / self._safe_dir_name(anime.title)
                owned = [
                    {
                        "info_hash": release.info_hash,
                        "title": release.title,
                        "backend": "aria2",
                        "save_path": str(target),
                        "stage": "adding",
                    }
                ]
                self.db.upsert_download(
                    DownloadItem(
                        release.info_hash,
                        release.title,
                        "selecting",
                        0,
                        str(target),
                        str(target),
                        media_id=media_id,
                        episode=episode,
                        is_batch=batch,
                        raw={
                            "backend": "aria2",
                            "_intent_revision": intent_revision,
                            "_release_numbering": DownloadIntentStore.numbering_metadata(release),
                        },
                    )
                )
            candidate_started = time.monotonic()
            self.release_telemetry.record(
                release,
                media_id=media_id,
                media_episode=episode,
                provider="aria2",
                outcome="candidate",
            )
            updated = self.download_intents.update(
                media_id,
                episode,
                batch,
                state="trying",
                selected=release,
                backend="aria2",
                detail=f"candidate {index + 1}/{len(pool)}",
                expected_revision=intent_revision,
                owned_candidates=owned,
            )
            if updated is None:
                self.logger.info(
                    "SKIP step=nyaa.race backend=aria2 media_id=%s episode=%s candidate=%s "
                    "reason=intent_superseded",
                    media_id,
                    episode,
                    index + 1,
                )
                return False, None
            try:
                added = self.add_release(media_id, release, episode=episode, batch=batch)
            except (QBittorrentError, ManagerError) as exc:
                fail(str(exc), reason="add_failed", candidate=release)
                return False, None
            blocker = self._duplicate_block(media_id, episode, batch) if added is False else None
            if blocker is not None:
                # Another torrent already covers this request; every other candidate
                # would be refused the same way, so the race ends here.
                blocker_hash, blocker_name = blocker
                if release.info_hash and release.info_hash.casefold() != blocker_hash.casefold():
                    self.db.delete_torrent_records(release.info_hash)
                self.download_intents.update(
                    media_id,
                    episode,
                    batch,
                    state="waiting",
                    backend="aria2",
                    detail=f"Covered by an existing torrent: {blocker_name}",
                    expected_revision=intent_revision,
                    owned_candidates=[],
                )
                self.logger.info(
                    "SKIP step=nyaa.race backend=aria2 media_id=%s episode=%s candidate=%s "
                    "reason=already_downloading torrent=%s",
                    media_id,
                    episode,
                    index + 1,
                    blocker_hash,
                )
                return False, None

            if not release.info_hash:
                self.download_intents.update(
                    media_id,
                    episode,
                    batch,
                    state="downloading",
                    selected=release,
                    backend="aria2",
                    expected_revision=intent_revision,
                )
                self.logger.info(
                    "DONE step=nyaa.race backend=aria2 media_id=%s episode=%s winner=%r candidate=%s "
                    "reason=no_info_hash",
                    media_id,
                    episode,
                    release.title,
                    index + 1,
                )
                return False, release
            return True, None

        for index, release in enumerate(pool):
            watch, result = add(index, release)
            if not watch:
                return result

            client = self.qbt_client()
            try:
                prioritize = getattr(client, "prioritize", None)
                if callable(prioritize):
                    try:
                        prioritize(release.info_hash)
                    except QBittorrentError:
                        pass

                # Without metadata the candidate gets the long window; once the
                # metadata is there, only whether peers send data is left.
                started = time.monotonic()
                deadline = started + metadata_window
                queued_since: float | None = None
                snapshot: dict[str, Any] | None = None
                while True:
                    try:
                        snapshot = client.torrent_status(release.info_hash)
                    except QBittorrentError as exc:
                        fail(str(exc), release)
                        return None
                    if snapshot and self._torrent_race_is_alive(snapshot):
                        self.logger.info(
                            "DONE step=nyaa.race backend=aria2 media_id=%s episode=%s "
                            "winner=%r candidate=%s elapsed=%.1f",
                            media_id,
                            episode,
                            release.title,
                            index + 1,
                            time.monotonic() - started,
                        )
                        return select(release, snapshot)
                    if snapshot and str(snapshot.get("status") or "").casefold() == "waiting":
                        # Queued behind aria2's active downloads: its window does not
                        # run, and every other candidate would queue the same way.
                        now_queued = time.monotonic()
                        queued_since = now_queued if queued_since is None else queued_since
                        if now_queued - queued_since >= self.ARIA2_RACE_QUEUED_SECONDS:
                            self.logger.info(
                                "FALLBACK step=nyaa.race backend=aria2 media_id=%s episode=%s "
                                "kept=%r candidate=%s reason=queued",
                                media_id,
                                episode,
                                release.title,
                                index + 1,
                            )
                            return select(
                                release,
                                snapshot,
                                outcome="queued",
                                detail="Queued behind other downloads",
                            )
                        started = now_queued
                        deadline = started + metadata_window
                        time.sleep(max(0.1, float(poll_seconds)))
                        continue
                    queued_since = None
                    if snapshot and int(snapshot.get("total_size") or snapshot.get("totalLength") or 0) > 0:
                        deadline = min(deadline, started + starting_window)
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(max(0.1, float(poll_seconds)))

                downloaded = int((snapshot or {}).get("downloaded") or 0)
                progress = float((snapshot or {}).get("progress") or 0.0)
                if downloaded > 0 or progress > 0:
                    # Once bytes exist, never destructively switch releases.
                    self.download_intents.update(
                        media_id,
                        episode,
                        batch,
                        state="downloading",
                        selected=release,
                        backend="aria2",
                        detail="candidate has partial data",
                        expected_revision=intent_revision,
                        stall_fallback=fallback(release),
                    )
                    self.logger.info(
                        "DONE step=nyaa.race backend=aria2 media_id=%s episode=%s winner=%r candidate=%s "
                        "reason=partial_data downloaded=%s",
                        media_id,
                        episode,
                        release.title,
                        index + 1,
                        downloaded,
                    )
                    return release

                peers_without_data = self._torrent_race_is_starting(snapshot)
                if peers_without_data:
                    starting.append(release)
                    if index == len(pool) - 1 and best_starting() is release:
                        # Nothing proved alive and the last try is the best one
                        # with peers: keep it instead of removing and re-adding.
                        self.logger.info(
                            "FALLBACK step=nyaa.race backend=aria2 media_id=%s episode=%s "
                            "kept=%r candidate=%s reason=peers_without_data",
                            media_id,
                            episode,
                            release.title,
                            index + 1,
                        )
                        return select(
                            release,
                            snapshot,
                            outcome="kept_without_data",
                            detail="Peers found, no data yet",
                        )
                else:
                    dead.add(release.info_hash.casefold())

                # Read before the removal: what aria2 created for it (0 bytes so far).
                created = self._aria2_candidate_files(client, release.info_hash)
                try:
                    self._delete_confirmed_torrent(client, release.info_hash, delete_files=False)
                except (QBittorrentError, ManagerError) as exc:
                    fail(str(exc), release, reason="removal_failed")
                    return None
                self.db.delete_torrent_records(release.info_hash)
                self._remove_empty_candidate_files(release.info_hash, created, race_target)
                self.download_intents.update(
                    media_id,
                    episode,
                    batch,
                    state="trying",
                    expected_revision=intent_revision,
                    owned_candidates=[],
                )
                self.release_telemetry.record(
                    release,
                    media_id=media_id,
                    media_episode=episode,
                    provider="aria2",
                    outcome="no_progress",
                    snapshot=snapshot,
                    elapsed_seconds=time.monotonic() - candidate_started,
                )
                self.logger.info(
                    "RETRY step=nyaa.race backend=aria2 media_id=%s episode=%s candidate=%s reason=%s",
                    media_id,
                    episode,
                    index + 1,
                    "peers_without_data" if peers_without_data else "no_progress",
                )
            finally:
                client.close()

        if starting:
            # No candidate sent data, but some had metadata and peers. Ending
            # with nothing left the request without any download; keep the one
            # with the most listed seeders (a stall moves on to the others).
            best = best_starting()
            watch, result = add(pool.index(best), best)
            if not watch:
                return result
            self.logger.info(
                "FALLBACK step=nyaa.race backend=aria2 media_id=%s episode=%s "
                "kept=%r candidate=%s reason=peers_without_data",
                media_id,
                episode,
                best.title,
                pool.index(best) + 1,
            )
            return select(best, None, outcome="kept_without_data", detail="Peers found, no data yet")

        self.download_intents.update(
            media_id,
            episode,
            batch,
            state="waiting",
            backend="aria2",
            detail="No candidate started downloading",
            expected_revision=intent_revision,
        )
        self.logger.info(
            "FAIL step=nyaa.race backend=aria2 media_id=%s episode=%s reason=no_candidate_started candidates=%s",
            media_id,
            episode,
            len(pool),
        )
        return None

    @staticmethod
    def _aria2_candidate_files(client: Any, torrent_hash: str) -> list[Path]:
        """The files aria2 created for a torrent (absolute paths), read before it is removed."""
        lister = getattr(client, "files", None)
        if not callable(lister) or str(getattr(client, "backend_name", "") or "") != "aria2":
            return []
        try:
            rows = lister(torrent_hash) or []
        except QBittorrentError:
            return []
        paths: list[Path] = []
        for row in rows:
            name = str((row or {}).get("name") or "") if isinstance(row, dict) else ""
            if name and not name.startswith("[METADATA]") and Path(name).is_absolute():
                paths.append(Path(name))
        return paths

    def _remove_empty_candidate_files(self, torrent_hash: str, files: list[Path], save_path: Path) -> int:
        """Remove what aria2 created for a removed torrent that never received a byte.

        Its empty files, control file and saved "<infohash>.torrent" stayed in
        the library and every scan listed the files as episodes. Only that
        torrent's own paths inside its save folder, and nothing at all once
        one of its files holds data.
        """
        if not files or not str(save_path or ""):
            return 0
        try:
            root = Path(save_path).expanduser().resolve()
            resolved = [path.expanduser().resolve() for path in files]
        except (OSError, RuntimeError):
            return 0
        if any(root not in path.parents for path in resolved):
            return 0
        try:
            if any(path.is_file() and path.stat().st_size > 0 for path in resolved):
                return 0
        except OSError:
            return 0
        tops = {root / path.relative_to(root).parts[0] for path in resolved}
        targets = [
            *resolved,
            *(path.with_name(path.name + ".aria2") for path in resolved),
            *(top.with_name(top.name + ".aria2") for top in tops),
            root / f"{str(torrent_hash or '').strip().lower()}.torrent",
        ]
        removed = 0
        deleted_videos: list[Path] = []
        for path in dict.fromkeys(targets):
            try:
                if path.is_file() and (
                    path.suffix.casefold() in {".aria2", ".torrent"} or path.stat().st_size == 0
                ):
                    path.unlink()
                    removed += 1
                    if path.suffix.casefold() in VIDEO_EXTENSIONS:
                        deleted_videos.append(path)
            except OSError:
                continue
        self._forget_deleted_videos(deleted_videos)
        folders = {parent for path in resolved for parent in path.parents if root in parent.parents}
        for folder in sorted(folders, key=lambda value: len(value.parts), reverse=True):
            try:
                folder.rmdir()
            except OSError:
                pass
        self.logger.info(
            "CLEANUP step=aria2.empty_candidate hash=%s files=%s removed=%s",
            torrent_hash,
            len(resolved),
            removed,
        )
        return removed

    def _video_state_keys(self, video: Path) -> list[str]:
        """The ``state`` rows kept per video file (subtitle choices, upgrade checks, markers)."""
        from .subtitle_delay import state_key as subtitle_delay_key

        forms = [video]
        try:
            resolved = video.expanduser().resolve()
        except (OSError, RuntimeError):
            resolved = video
        forms.append(resolved)
        keys: list[str] = []
        for value in dict.fromkeys(forms):
            keys += [
                self._subtitle_upgrade_state_key(value),
                self._subtitle_upgrade_checked_key(value),
                self._subtitle_upgrade_attempts_key(value),
                self._manual_subtitle_state_key(value),
                self._debug_force_subtitle_state_key(value),
                self._subtitle_candidate_fingerprint_state_key(value),
                self._subtitle_failure_streak_state_key(value),
                self._subtitle_force_rebuild_state_key(value),
                self._extra_file_kept_state_key(value),
                extra_files.set_aside_state_key(value),
                subtitle_delay_key(value),
                f"ready_notification:file:{hashlib.sha256(str(value).encode()).hexdigest()}",
                f"double_episode_part_one:{value}",
                self._embedded_subtitle_probe_state_key(value),
                *subtitle_migrations.video_marker_keys(value),
            ]
        return list(dict.fromkeys(keys))

    def _forget_video_state(self, videos: Iterable[Path]) -> int:
        """Drop the per-video state rows of videos Pudge itself deleted (DB-9).

        Only ``playback_save`` markers were ever pruned: every other per-video key
        stayed forever after the episode was watched and deleted. Only files that
        are really gone count (a kept file keeps its choices).
        """
        deleter = getattr(self.db, "delete_states", None)
        if not callable(deleter):
            return 0
        keys: list[str] = []
        for video in videos:
            try:
                if video.exists():
                    continue
            except OSError:
                continue
            keys += self._video_state_keys(video)
        if not keys:
            return 0
        try:
            return int(deleter(keys) or 0)
        except sqlite3.Error as exc:
            self.logger.warning("SKIP step=state.forget_video reason=%r", str(exc)[:200])
            return 0

    def _forget_deleted_videos(self, videos: list[Path]) -> int:
        """Drop the library rows and subtitle jobs of videos Pudge itself just deleted.

        They used to wait out the missing-file delay like a file deleted in
        Finder, so the episode list showed every episode twice (the deleted
        copy "waiting for subtitles") and Home took the gone copy as the next
        episode. The next scan walks the library again.
        """
        forgotten = 0
        for video in videos:
            try:
                resolved = video.expanduser().resolve()
            except (OSError, RuntimeError):
                resolved = video
            for value in dict.fromkeys((video, resolved)):
                row = self.db.episode_by_path(value)
                self.db.delete_episode_record(value)
                if row is None:
                    continue
                forgotten += 1
                self.logger.info(
                    "CLEANUP step=library.deleted_video_row media_id=%s episode=%s video=%r",
                    row.media_id,
                    row.episode,
                    str(value),
                )
        if videos:
            self._forget_video_state(videos)
            self.db.delete_state(self._LIBRARY_SCAN_CACHE_KEY)
            self.invalidate_storage_snapshot()
        return forgotten

    def _known_client_torrents(self, step: str) -> tuple[list[Path], set[str]] | None:
        """Paths and info hashes of every torrent the clients list, or None when one cannot be asked.

        None means nothing may be removed: aria2 may simply be stopped.
        """
        clients = self.torrent_clients()
        if not clients:
            return None
        known_paths: list[Path] = []
        known_hashes: set[str] = set()
        try:
            for name, client in clients:
                try:
                    listing = list(client.torrents(category=""))
                except Exception as exc:  # noqa: BLE001 - unknown torrents: try again later
                    self.logger.info(
                        "SKIP step=%s reason=client_unavailable backend=%s error=%r",
                        step,
                        name,
                        str(exc)[:200],
                    )
                    return None
                for item in listing:
                    raw = item.raw or {}
                    known_hashes.update(
                        str(value or "").strip().casefold()
                        for value in (item.torrent_hash, raw.get("infohash_v1"), raw.get("infohash_v2"))
                        if value
                    )
                    for value in (item.content_path, item.save_path if not item.content_path else ""):
                        if str(value or "").strip():
                            try:
                                known_paths.append(Path(str(value)).expanduser().resolve())
                            except (OSError, RuntimeError):
                                continue
        finally:
            for _name, client in clients:
                client.close()
        return known_paths, known_hashes

    _ARIA2_LEFTOVER_CLEANUP_KEY = "aria2_leftover_cleanup_v1"
    # aria2 rewrites the control file of a task it runs about every minute.
    _ARIA2_LEFTOVER_MIN_AGE_SECONDS = 10 * 60

    def cleanup_aria2_leftovers(self, *, now: float | None = None) -> int:
        """Once: remove the empty files that removed race candidates left in the library.

        Only a video of 0 bytes that no torrent of the client lists and that has
        an aria2 control file next to it (or next to its folder) is deleted, then
        that control file and folders left empty, and "<infohash>.torrent" files
        of torrents the client no longer has. A file with data is never touched.
        Runs when the client can be asked; the done-marker is set afterwards.
        """
        cleaned_at = self.db.get_state(self._ARIA2_LEFTOVER_CLEANUP_KEY, "")
        if cleaned_at:
            self._forget_aria2_leftover_rows_once(cleaned_at)
        if not self.config.aria2.enabled or cleaned_at:
            return 0
        root = self.config.library.root_dir.expanduser()
        known = self._known_client_torrents("library.aria2_leftovers")
        if known is None:
            return 0
        known_paths, known_hashes = known
        current = float(now if now is not None else time.time())
        removed = 0
        controls: set[Path] = set()
        deleted_videos: list[Path] = []
        videos: list[Path] = []
        torrents: list[Path] = []
        if root.is_dir():
            for path in root.rglob("*"):
                suffix = path.suffix.casefold()
                if suffix in VIDEO_EXTENSIONS:
                    videos.append(path)
                elif suffix == ".torrent":
                    torrents.append(path)
        for path in videos:
            try:
                if not path.is_file() or path.stat().st_size != 0:
                    continue
                resolved = path.resolve()
            except (OSError, RuntimeError):
                continue
            if self._path_within(resolved, tuple(known_paths)):
                continue
            control = aria2_control_file(path, root)
            try:
                if (
                    control is None
                    or current - control.stat().st_mtime < self._ARIA2_LEFTOVER_MIN_AGE_SECONDS
                ):
                    continue
                path.unlink()
            except OSError:
                continue
            removed += 1
            deleted_videos.append(path)
            controls.add(control)
            self.logger.info(
                "CLEANUP step=library.aria2_leftover video=%r control=%r", str(path), str(control)
            )
        for control in controls:
            target = control.with_name(control.name[: -len(".aria2")])
            if target.is_dir():
                folders = sorted(
                    (item for item in target.rglob("*") if item.is_dir()),
                    key=lambda value: len(value.parts),
                    reverse=True,
                )
                for folder in [*folders, target]:
                    try:
                        folder.rmdir()
                    except OSError:
                        pass
            if target.exists():
                continue  # files with data (or others) are still there: keep it
            try:
                control.unlink()
            except OSError:
                continue
            self.logger.info("CLEANUP step=library.aria2_leftover control=%r", str(control))
        for path in torrents:
            stem = path.stem.casefold()
            if not re.fullmatch(r"[0-9a-f]{40}", stem) or stem in known_hashes:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
            self.logger.info("CLEANUP step=library.aria2_leftover torrent=%r", str(path))
        self._forget_deleted_videos(deleted_videos)
        self.db.set_state(self._ARIA2_LEFTOVER_CLEANUP_KEY, str(int(current)))
        # This cleanup forgets its rows itself: nothing to repair afterwards.
        self.db.set_state(self._ARIA2_LEFTOVER_ROWS_REPAIR_KEY, str(int(current)))
        self.logger.info("DONE step=library.aria2_leftovers removed=%s", removed)
        return removed

    _ARIA2_ORPHAN_CONTROL_KEY = "aria2_orphan_controls_checked_at"
    _ARIA2_ORPHAN_CONTROL_INTERVAL_SECONDS = 6 * 60 * 60

    def cleanup_orphan_aria2_controls(self, *, now: float | None = None) -> int:
        """Remove aria2 control files left by torrents that are gone, a few times a day.

        A race candidate removed before it wrote anything can leave its
        "<name>.aria2" with no "<name>" beside it (seen: a 229-byte control of a
        21 GiB BD pack next to the batch that won). Only a control file that is
        older than ten minutes, names a torrent no client lists and has no data
        (its file or folder is missing, or the folder holds no file) goes, with
        that empty folder. Unreadable control files are kept.
        """
        if not self.config.aria2.enabled:
            return 0
        current = float(now if now is not None else time.time())
        try:
            checked = float(self.db.get_state(self._ARIA2_ORPHAN_CONTROL_KEY, "") or 0.0)
        except (TypeError, ValueError):
            checked = 0.0
        if 0.0 < current - checked < self._ARIA2_ORPHAN_CONTROL_INTERVAL_SECONDS:
            return 0
        root = self.config.library.root_dir.expanduser()
        if not root.is_dir():
            return 0
        known = self._known_client_torrents("library.aria2_orphan_controls")
        if known is None:
            return 0
        _known_paths, known_hashes = known
        # A download Pudge still tracks keeps its control file even if the client lost it.
        known_hashes = known_hashes | {
            str(item.torrent_hash or "").strip().casefold() for item in self.db.downloads()
        }
        removed = 0
        for control in root.rglob("*.aria2"):
            try:
                if (
                    not control.is_file()
                    or current - control.stat().st_mtime < self._ARIA2_LEFTOVER_MIN_AGE_SECONDS
                ):
                    continue
            except OSError:
                continue
            info_hash = aria2_control_infohash(control)
            if not info_hash or info_hash in known_hashes:
                continue
            target = control.with_name(control.name[: -len(".aria2")])
            folders: list[Path] = []
            try:
                if target.is_dir():
                    if any(item.is_file() or item.is_symlink() for item in target.rglob("*")):
                        continue  # data is there: not an orphan
                    folders = sorted(
                        (item for item in target.rglob("*") if item.is_dir()),
                        key=lambda value: len(value.parts),
                        reverse=True,
                    )
                    folders.append(target)
                elif target.exists() or target.is_symlink():
                    continue
                for folder in folders:
                    folder.rmdir()
                control.unlink()
            except OSError:
                continue
            removed += 1
            self.logger.info(
                "CLEANUP step=library.aria2_orphan_control control=%r hash=%s empty_folder=%s",
                str(control),
                info_hash,
                bool(folders),
            )
        self.db.set_state(self._ARIA2_ORPHAN_CONTROL_KEY, str(int(current)))
        if removed:
            self.logger.info("DONE step=library.aria2_orphan_controls removed=%s", removed)
        return removed

    _ARIA2_LEFTOVER_ROWS_REPAIR_KEY = "aria2_leftover_rows_repair_v1"

    def _forget_aria2_leftover_rows_once(self, cleaned_at: str) -> int:
        """Once: forget the rows that the first leftover cleanup left behind.

        It deleted the empty files of removed race candidates but kept their
        rows for the missing-file wait, so every episode was listed twice for
        hours. A row of a file that went missing since that cleanup goes unless
        it holds viewing history (watched, played, deletion planned), its drive
        is away or the client still has its torrent.
        """
        if self.db.get_state(self._ARIA2_LEFTOVER_ROWS_REPAIR_KEY, ""):
            return 0
        try:
            since_cleanup = float(cleaned_at) - 60.0
        except (TypeError, ValueError):
            since_cleanup = 0.0
        try:
            stored = json.loads(self.db.get_state(self._MISSING_VIDEO_STATE, "") or "{}")
        except (TypeError, ValueError):
            stored = {}
        missing_since = stored if isinstance(stored, dict) else {}
        active = {str(item.torrent_hash or "").strip().casefold() for item in self.db.downloads()}
        active.discard("")
        incomplete = self.incomplete_download_paths()
        rows: dict[str, LibraryEpisode] = {}
        for item in self.db.episodes():
            key = str(item.video_path)
            if item.video_path.is_file() or self._path_within(item.video_path, incomplete):
                continue
            if str(item.torrent_hash or "").strip().casefold() in active:
                continue
            if (
                item.watched_at
                or str(item.state or "") == "watched"
                or float(item.playback_position or 0.0) > 0.0
                or item.delete_after
            ):
                continue
            try:
                since = float(missing_since[key]) if key in missing_since else None
            except (TypeError, ValueError):
                since = None
            if since is not None and since < since_cleanup:
                continue  # missing since before the cleanup: not its leftover
            rows[key] = item
        forgotten = 0
        for value in sorted(self._forgettable_missing_videos([Path(key) for key in rows], immediate=True)):
            row = rows.get(value)
            self.db.delete_episode_record(Path(value))
            forgotten += 1
            self.logger.info(
                "REPAIR step=library.aria2_leftover_row media_id=%s episode=%s state=%s video=%r",
                row.media_id if row else None,
                row.episode if row else None,
                row.state if row else "",
                value,
            )
        self.db.set_state(self._ARIA2_LEFTOVER_ROWS_REPAIR_KEY, str(int(time.time())))
        self.logger.info("DONE step=library.aria2_leftover_rows forgotten=%s", forgotten)
        return forgotten

    def replace_stalled_aria2_downloads(self, *, now: float | None = None) -> int:
        """Swap a chosen aria2 release that never received data for the next recorded candidate.

        The race keeps a candidate with peers but no data when nothing better
        moved, and aria2's own stall recovery only reconnects that same torrent.
        Only a download still at 0 bytes is replaced, and only one that its race
        chose (a manual choice drops the recorded candidates).
        """
        intents = getattr(self, "download_intents", None)
        listing = getattr(intents, "stall_fallbacks", None)
        if not callable(listing) or not self.config.aria2.enabled or self.config.qbittorrent.enabled:
            return 0
        pending = listing()
        if not pending or not self.downloads_enabled() or self.torrent_paused_on_add():
            return 0
        current = float(now if now is not None else time.time())
        active = getattr(self, "_active_acquisition_keys", set())
        replaced = 0
        client = self.qbt_client()
        try:
            try:
                items = {
                    str(item.torrent_hash or "").casefold(): item
                    for item in client.torrents(category=self.config.qbittorrent.category)
                }
            except QBittorrentError as exc:
                self.logger.warning("RETRY step=aria2.stall_replace error=%r", str(exc))
                return 0
            for intent in pending:
                media_id, episode, batch = (
                    int(intent["media_id"]),
                    intent.get("episode"),
                    bool(intent.get("batch")),
                )
                if intents.key(media_id, episode, batch) in active:
                    continue
                if str(intent.get("backend") or "") != "aria2":
                    continue
                selected_hash = str(intent.get("selected_hash") or "").casefold()
                item = items.get(selected_hash)
                if item is None:
                    continue
                raw = item.raw or {}
                if str(item.state or "").casefold() == "waiting":
                    # Queued behind aria2's active downloads: no peer was asked yet,
                    # so the stall clock starts only once it runs.
                    restart = getattr(intents, "restart_stall_clock", None)
                    if callable(restart):
                        restart(media_id, episode, batch, now=current)
                    continue
                if (
                    str(item.state or "").casefold() != "active"
                    or bool(raw.get("verifying"))
                    or int(raw.get("downloaded") or 0) > 0
                    or float(item.progress or 0.0) > 0
                    or int(raw.get("download_speed") or 0) > 0
                ):
                    continue
                details = intent.get("stall_fallback") or {}
                since = max(float(details.get("since") or 0.0), float(item.added_on or 0))
                stalled_for = current - since
                if since <= 0 or stalled_for < self.ARIA2_STALL_REPLACE_SECONDS:
                    continue
                fallbacks = [
                    release
                    for release in map(self._release_from_record, details.get("candidates") or [])
                    if release is not None and release.info_hash
                ]
                previous = self._release_from_record(details.get("selected"))
                record = self.db.download_by_hash(item.torrent_hash)
                if not fallbacks or (
                    record is not None and (record.media_id != media_id or record.episode != episode)
                ):
                    continue
                try:
                    snapshot = client.torrent_status(item.torrent_hash)
                except QBittorrentError as exc:
                    self.logger.warning(
                        "RETRY step=aria2.stall_replace hash=%s error=%r", item.torrent_hash, str(exc)
                    )
                    continue
                # Same rule as the race: never switch once a byte exists.
                if (
                    snapshot is None
                    or int(snapshot.get("downloaded") or 0) > 0
                    or float(snapshot.get("progress") or 0.0) > 0
                    or int(snapshot.get("dlspeed") or 0) > 0
                ):
                    continue
                self.logger.info(
                    "RETRY step=aria2.stall_replace media_id=%s episode=%s batch=%s hash=%s "
                    "stalled_s=%.0f candidates=%s title=%r",
                    media_id,
                    episode,
                    batch,
                    item.torrent_hash,
                    stalled_for,
                    len(fallbacks),
                    item.name,
                )
                created = self._aria2_candidate_files(client, item.torrent_hash)
                try:
                    self._delete_confirmed_torrent(client, item.torrent_hash, delete_files=False)
                except (QBittorrentError, ManagerError) as exc:
                    self.logger.warning(
                        "RETRY step=aria2.stall_replace hash=%s error=%r", item.torrent_hash, str(exc)
                    )
                    continue
                self.db.delete_torrent_records(item.torrent_hash)
                self._remove_empty_candidate_files(item.torrent_hash, created, Path(item.save_path or ""))
                # The stalled release goes last: it comes back only when every
                # other candidate is dead, and it is never recorded again.
                pool = [*fallbacks, *([previous] if previous is not None else [])]
                try:
                    winner = self._race_add_candidates(
                        media_id,
                        pool,
                        episode=episode,
                        batch=batch,
                        stalled_hashes=frozenset({selected_hash}),
                    )
                except (UnconfirmedCandidatesError, QBittorrentError, ManagerError) as exc:
                    self.logger.warning(
                        "FAIL step=aria2.stall_replace media_id=%s episode=%s error=%r",
                        media_id,
                        episode,
                        str(exc),
                    )
                    self._stall_replace_left_nothing(media_id, episode, batch, selected_hash, str(exc))
                    continue
                if winner is None:
                    self._stall_replace_left_nothing(
                        media_id, episode, batch, selected_hash, "no_candidate_started"
                    )
                self.logger.info(
                    "REPLACE step=aria2.stall_replace media_id=%s episode=%s batch=%s old_hash=%s "
                    "new_hash=%s title=%r",
                    media_id,
                    episode,
                    batch,
                    item.torrent_hash,
                    winner.info_hash if winner is not None else "",
                    winner.title if winner is not None else "",
                )
                if winner is not None and str(winner.info_hash or "").casefold() != selected_hash:
                    replaced += 1
        finally:
            client.close()
        return replaced

    def _stall_replace_left_nothing(
        self, media_id: int, episode: int | None, batch: bool, removed_hash: str, reason: str
    ) -> None:
        """The stalled torrent is gone and no replacement started: the request still said
        "downloading" the removed hash, which nothing ever looked at again (a Planning pack
        stayed "downloading" with no torrent). It waits again instead."""
        intents = getattr(self, "download_intents", None)
        if intents is None:
            return
        try:
            intent = intents.get(media_id, episode, batch) or {}
            if str(intent.get("selected_hash") or "").casefold() != removed_hash:
                return  # a replacement (or something else) owns it now
            intents.update(
                media_id,
                episode,
                batch,
                state="waiting",
                backend=str(intent.get("backend") or "aria2"),
                detail=f"Stalled download removed; no replacement started ({reason[:120]})",
            )
        except Exception as exc:  # noqa: BLE001 - the replacement pass goes on
            self.logger.warning("FAIL step=aria2.stall_replace_waiting error=%r", str(exc)[:200])
            return
        self.logger.info(
            "WAIT step=aria2.stall_replace media_id=%s episode=%s reason=%s", media_id, episode, reason[:120]
        )

    @_torrent_app_may_start
    def recover_acquisition_attempts(self) -> int:
        """Replay only journaled, unfinished candidates through their recorded owner."""
        recovered = 0
        attempts = self.download_intents.recoverable()
        if not attempts:
            return 0
        clients = dict(self.torrent_clients())
        try:
            for intent in attempts:
                media, episode, batch = (
                    int(intent["media_id"]),
                    intent.get("episode"),
                    bool(intent.get("batch")),
                )
                key = self.download_intents.key(media, episode, batch)
                if key in getattr(self, "_active_acquisition_keys", set()):
                    continue
                revision = int(intent.get("revision") or 0)
                owned = list(intent["owned_candidates"])
                finalized = bool(intent.get("winner_finalized"))
                try:
                    for candidate in list(owned):
                        backend, hash_ = candidate["backend"], candidate["info_hash"]
                        client = clients.get(backend)
                        try:
                            if client is None:
                                raise ManagerError(
                                    err("manager.acquisitionOwnerUnavailable", backend=backend)
                                )
                            snapshot = client.torrent_status(hash_)
                        except (QBittorrentError, ManagerError) as exc:
                            if not self._unconfirmed_candidate_released(intent, candidate, client, exc):
                                raise
                            self.db.delete_torrent_records(hash_)
                            owned.remove(candidate)
                            self.download_intents.update(
                                media,
                                episode,
                                batch,
                                state="recovery_required",
                                expected_revision=revision,
                                owned_candidates=list(owned),
                            )
                            continue
                        if snapshot is not None and not self.downloads_enabled():
                            self.note_torrents_paused_by_pudge([hash_], reason="torrents_off")
                            client.pause(hash_)
                            continue
                        if snapshot is not None:
                            record = self.db.download_by_hash(hash_)
                            if record is None or record.media_id != media or record.episode != episode:
                                raise ManagerError(err("manager.acquisitionOwnershipChanged"))
                            if backend == "qbittorrent":
                                # Failed races own isolated temporary directories. Never
                                # delete a moved winner or a pre-existing download.
                                if candidate.get("is_winner"):
                                    target = Path(candidate["final_target"])
                                    anime = self.db.get_anime(media)
                                    if (
                                        anime is None
                                        or target
                                        != self.config.library.root_dir / self._safe_dir_name(anime.title)
                                    ):
                                        raise ManagerError(err("manager.winnerTargetChanged"))
                                    if (
                                        str(intent.get("selected_hash") or "").casefold() != hash_.casefold()
                                        or int(record.raw.get("_intent_revision") or 0) != revision
                                        or record.is_batch != batch
                                        or record.raw.get("backend") != backend
                                        or str(record.save_path)
                                        not in {candidate.get("save_path"), str(target)}
                                    ):
                                        raise ManagerError(err("manager.winnerOwnershipChanged"))
                                    if not record.raw.get("_race_id") and str(record.save_path) != str(
                                        target
                                    ):
                                        raise ManagerError(err("manager.winnerTargetLost"))
                                    client.set_location(hash_, target)
                                    tags = [
                                        APP_SLUG,
                                        f"anime: {self._safe_qbt_tag(anime.title)}",
                                        f"anilist: {media}",
                                    ]
                                    tags.append(
                                        f"episode: {episode}" if episode is not None else "series pack"
                                    )
                                    client.set_metadata(
                                        hash_, category=self.config.qbittorrent.category, tags=tags
                                    )
                                    if not self.torrent_paused_on_add():
                                        self._torrent_network_start(client, "start", hash_)
                                    record.save_path = record.content_path = str(target)
                                    record.raw.pop("_race_id", None)
                                    record.raw.pop("_race_stage", None)
                                    record.state = "downloading"
                                    self.db.upsert_download(record)
                                    finalized = True
                                    owned.remove(candidate)
                                    self.download_intents.update(
                                        media,
                                        episode,
                                        batch,
                                        state="recovery_required",
                                        expected_revision=revision,
                                        owned_candidates=list(owned),
                                        winner_finalized=True,
                                    )
                                    continue
                                else:
                                    if not record.raw.get("_race_id") or str(
                                        record.save_path
                                    ) != candidate.get("save_path"):
                                        raise ManagerError(err("manager.candidatePathChanged"))
                                    self._delete_confirmed_torrent(client, hash_, delete_files=True)
                            else:
                                if (
                                    int(snapshot.get("downloaded") or 0) > 0
                                    or float(snapshot.get("progress") or 0) > 0
                                ):
                                    # A partially downloaded single target stays owned; do
                                    # not switch releases after data has been written.
                                    self._torrent_network_start(client, "start", hash_)
                                    finalized = True
                                    owned.remove(candidate)
                                    self.download_intents.update(
                                        media,
                                        episode,
                                        batch,
                                        state="recovery_required",
                                        expected_revision=revision,
                                        owned_candidates=list(owned),
                                        winner_finalized=True,
                                    )
                                    continue
                                self._delete_confirmed_torrent(client, hash_, delete_files=False)
                        self.db.delete_torrent_records(hash_)
                        owned.remove(candidate)
                        self.download_intents.update(
                            media,
                            episode,
                            batch,
                            state="recovery_required",
                            expected_revision=revision,
                            owned_candidates=list(owned),
                        )
                    if owned:
                        continue
                    self.download_intents.update(
                        media,
                        episode,
                        batch,
                        state="downloading" if finalized else "waiting",
                        expected_revision=revision,
                        owned_candidates=[],
                        detail="Interrupted acquisition recovered",
                    )
                    recovered += 1
                except (QBittorrentError, ManagerError) as exc:
                    self.download_intents.update(
                        media,
                        episode,
                        batch,
                        state="recovery_required",
                        expected_revision=revision,
                        detail=str(exc),
                        owned_candidates=owned,
                    )
        finally:
            for client in clients.values():
                client.close()
        return recovered

    # An unconfirmed candidate whose torrent client cannot be asked (torrents
    # off, aria2 stopped, backend switched off) is released after this long,
    # but only when the client's own records show it never arrived there.
    UNCONFIRMED_RELEASE_SECONDS = 3600.0

    def _unconfirmed_candidate_released(
        self,
        intent: dict[str, Any],
        candidate: dict[str, Any],
        client: Any | None,
        error: Exception,
    ) -> bool:
        if candidate.get("is_winner") or intent.get("winner_finalized"):
            return False
        try:
            since = float(intent.get("unconfirmed_since") or intent.get("updated_at") or 0.0)
        except (TypeError, ValueError):
            since = 0.0
        age = time.time() - since
        if since <= 0 or age < self.UNCONFIRMED_RELEASE_SECONDS:
            return False
        hash_ = str(candidate.get("info_hash") or "")
        if client is not None:
            known_offline = getattr(client, "known_offline", None)
            if not callable(known_offline):
                return False  # qBittorrent keeps torrents we cannot see while it is down
            try:
                if known_offline(hash_):
                    return False
            except Exception:  # noqa: BLE001 - unknown means keep
                return False
        self.logger.info(
            "RELEASE step=download.unconfirmed_candidate media_id=%s episode=%s hash=%s backend=%s "
            "age_s=%.0f reason=%r",
            intent.get("media_id"),
            intent.get("episode"),
            hash_,
            candidate.get("backend"),
            age,
            str(error)[:200],
        )
        return True

    @_torrent_app_may_start
    def _add_journaled_single_candidate(
        self,
        media_id: int,
        release: NyaaRelease,
        *,
        episode: int | None,
        batch: bool,
        backend: str,
        intent_revision: int,
    ) -> NyaaRelease | None:
        """Journal a normal target before an add with an uncertain acknowledgement."""
        anime = self.db.get_anime(media_id)
        if anime is None:  # removed meanwhile: a ManagerError the pass handles, not AttributeError
            raise ManagerError(err("manager.animeNotInDb", mediaId=media_id))
        target = self.config.library.root_dir / self._safe_dir_name(anime.title)
        try:
            if not release.info_hash:
                raise ManagerError(err("manager.candidateNeedsHash"))
            self.ensure_torrent_backend_ready()
            borrowed = False
            clients = self.torrent_clients()
            try:
                for _name, client in clients:
                    if (
                        client.torrent_status(release.info_hash) is not None
                        or self._existing_download_for_request(client, anime, episode=episode, batch=batch)
                        is not None
                    ):
                        borrowed = True
            finally:
                for _name, client in clients:
                    client.close()
            if not borrowed:
                prior = self.db.download_by_hash(release.info_hash)
                if prior is not None and (
                    prior.media_id not in {None, media_id} or prior.episode not in {None, episode}
                ):
                    raise ManagerError(err("manager.candidateOtherRequest"))
                raw = {
                    "backend": backend,
                    "_intent_revision": intent_revision,
                    "_acquisition_kind": "single",
                    "_acquisition_target": str(target),
                    "_release_numbering": DownloadIntentStore.numbering_metadata(release),
                }
                self.db.upsert_download(
                    DownloadItem(
                        release.info_hash,
                        release.title,
                        "selecting",
                        0,
                        str(target),
                        str(target),
                        media_id=media_id,
                        episode=episode,
                        is_batch=batch,
                        raw=raw,
                    )
                )
                candidate = {
                    "info_hash": release.info_hash,
                    "title": release.title,
                    "backend": backend,
                    "save_path": str(target),
                    "stage": "single_adding",
                    "is_winner": True,
                    "final_target": str(target),
                }
                updated = self.download_intents.update(
                    media_id,
                    episode,
                    batch,
                    state="trying",
                    selected=release,
                    backend=backend,
                    expected_revision=intent_revision,
                    owned_candidates=[candidate],
                )
                if updated is None:
                    raise ManagerError(err("manager.acquisitionRevisionChanged"))
            self.add_release(media_id, release, episode=episode, batch=batch)
            self.download_intents.update(
                media_id,
                episode,
                batch,
                state="downloading",
                selected=release,
                backend=backend,
                expected_revision=intent_revision,
                owned_candidates=[],
            )
            return release
        except (QBittorrentError, ManagerError) as exc:
            self.download_intents.update(
                media_id,
                episode,
                batch,
                state="recovery_required",
                backend=backend,
                expected_revision=intent_revision,
                detail=str(exc),
            )
            self.logger.warning(
                "FAIL step=nyaa.race backend=%s media_id=%s episode=%s reason=add_failed title=%r error=%r",
                backend,
                media_id,
                episode,
                release.title,
                str(exc),
            )
            return None

    def _race_add_candidates(
        self, media_id: int, candidates: list[NyaaRelease], *, episode: int | None, batch: bool, **kwargs: Any
    ) -> NyaaRelease | None:
        if not hasattr(self, "download_intents"):
            self.download_intents = DownloadIntentStore(self.db)
        if candidates:
            self._ensure_download_space(
                max(int(release.size_bytes or 0) for release in self._race_pool(candidates, batch=batch))
            )
        anime = self.db.get_anime(media_id)
        validated = []
        for release in candidates:
            try:
                validated.append(self._validated_release_identity(anime, release, episode, batch))
            except ManagerError as exc:
                self.logger.info(
                    "SKIP step=torrent.candidate_identity hash=%s reason=%s", release.info_hash, str(exc)
                )
        if not validated:
            self.logger.info(
                "SKIP step=nyaa.race media_id=%s episode=%s reason=no_valid_candidate candidates=%s",
                media_id,
                episode,
                len(candidates),
            )
            return None
        intent = self.download_intents.get(media_id, episode, batch)
        if intent and intent.get("owned_candidates"):
            self.recover_acquisition_attempts()
            intent = self.download_intents.get(media_id, episode, batch)
            if intent and intent.get("owned_candidates"):
                self.logger.info(
                    "SKIP step=nyaa.race media_id=%s episode=%s reason=unconfirmed_candidates",
                    media_id,
                    episode,
                )
                return None
        key = self.download_intents.key(media_id, episode, batch)
        with self._acquisition_keys_lock:
            active = getattr(self, "_active_acquisition_keys", None)
            if active is None:
                self._active_acquisition_keys = active = set()
            running = key in active
            if not running:
                active.add(key)
        if running:
            self.logger.info(
                "SKIP step=nyaa.race media_id=%s episode=%s reason=already_running", media_id, episode
            )
            return None
        try:
            winner = self._race_add_candidates_impl(
                media_id, validated, episode=episode, batch=batch, **kwargs
            )
        finally:
            with self._acquisition_keys_lock:
                active.discard(key)
        if winner is not None:
            self._note_storage_admission(winner.info_hash, winner.size_bytes)
        return winner

    @_torrent_app_may_start
    def _race_add_candidates_impl(
        self,
        media_id: int,
        candidates: list[NyaaRelease],
        *,
        episode: int | None,
        batch: bool,
        fast_seconds: float = 10.0,
        total_seconds: float = 120.0,
        poll_seconds: float = 2.0,
        stalled_hashes: Iterable[str] = (),
    ) -> NyaaRelease | None:
        """Race up to three qBittorrent candidates and keep the best one that moves."""
        sequential = bool(self.config.aria2.enabled and not self.config.qbittorrent.enabled)
        # aria2 tries one after another: a season pack may try two well-seeded extras.
        pool = self._race_pool(candidates, batch=batch) if sequential else list(candidates[:3])
        if not pool:
            return None
        if not hasattr(self, "download_intents"):
            self.download_intents = DownloadIntentStore(self.db)
        if not hasattr(self, "release_telemetry"):
            self.release_telemetry = ReleaseTelemetryService(self.db)
        backend = "qbittorrent" if self.config.qbittorrent.enabled else "aria2"
        fast_seconds, total_seconds = self.release_telemetry.deadlines(
            provider=backend,
            default_fast=float(fast_seconds),
            default_total=float(total_seconds),
        )
        for release in pool:
            self.release_telemetry.record(
                release,
                media_id=media_id,
                media_episode=episode,
                provider=backend,
                outcome="candidate",
            )
        intent = self.download_intents.begin(media_id, episode, batch, pool, backend=backend)
        intent_revision = int(intent.get("revision") or 0)
        if self.config.aria2.enabled and not self.config.qbittorrent.enabled:
            return self._race_add_candidates_aria2(
                media_id,
                pool,
                episode=episode,
                batch=batch,
                fast_seconds=fast_seconds,
                poll_seconds=poll_seconds,
                intent_revision=intent_revision,
                stalled_hashes=stalled_hashes,
            )
        if not self.config.qbittorrent.enabled or len(pool) == 1 or self.torrent_paused_on_add():
            return self._add_journaled_single_candidate(
                media_id,
                pool[0],
                episode=episode,
                batch=batch,
                backend=backend,
                intent_revision=intent_revision,
            )

        anime = self.db.get_anime(media_id)
        if anime is None:
            raise ManagerError(err("manager.animeNotInDb", mediaId=media_id))

        target = self.config.library.root_dir / self._safe_dir_name(anime.title)
        target.mkdir(parents=True, exist_ok=True)
        try:
            (target / ".anilist.id").write_text(str(anime.media_id), encoding="utf-8")
            self._remember_pudge_sidecar(target, anime.media_id)
        except OSError as exc:
            self.logger.warning(
                "WARN step=torrent.race_sidecar media_id=%s path=%r error=%r",
                media_id,
                str(target),
                str(exc),
            )

        race_id = f"{media_id}-{episode if episode is not None else 'batch'}-{int(time.time() * 1000)}"
        race_root = self.config.paths.cache_dir / "torrent-race" / race_id
        race_root.mkdir(parents=True, exist_ok=True)
        race_tag = f"race: {race_id}"
        final_tags = [
            APP_SLUG,
            f"anime: {self._safe_qbt_tag(anime.title)}",
            f"anilist: {anime.media_id}",
        ]
        if episode is not None:
            final_tags.append(f"episode: {episode}")
        if batch:
            final_tags.append("series pack")

        client = self.qbt_client()
        entries: list[tuple[NyaaRelease, str, Path]] = []
        live_hashes: set[str] = set()
        latest_snapshots: dict[str, dict[str, Any]] = {}
        stale_existing_hash = ""
        started_at = time.monotonic()

        winner_hash_for_journal = ""

        def journal(stage: str) -> None:
            owned = [
                {
                    "info_hash": torrent_hash,
                    "title": release.title,
                    "backend": "qbittorrent",
                    "save_path": str(slot),
                    "stage": stage,
                    "is_winner": torrent_hash == winner_hash_for_journal,
                    "final_target": str(target),
                }
                for release, torrent_hash, slot in entries
            ]
            updated = self.download_intents.update(
                media_id,
                episode,
                batch,
                state="trying",
                backend="qbittorrent",
                expected_revision=intent_revision,
                owned_candidates=owned,
                selected=next(
                    (release for release, hash_, _slot in entries if hash_ == winner_hash_for_journal), None
                ),
            )
            if updated is None:
                raise ManagerError(err("manager.acquisitionStageChanged"))

        def add_one(index: int, release: NyaaRelease) -> bool:
            self._validated_release_identity(self.db.get_anime(media_id), release, episode, batch)
            # Never hijack/delete a torrent that was already present before this race.
            if release.info_hash:
                existing = client.torrent_status(release.info_hash)
                if existing is not None:
                    self.logger.info(
                        "SKIP step=torrent.race_add reason=already_present hash=%s title=%r",
                        release.info_hash,
                        release.title,
                    )
                    return False
            slot = race_root / f"candidate-{index + 1}"
            if not release.info_hash:
                raise ManagerError(err("manager.candidateNeedsHash"))
            entries.append((release, release.info_hash, slot))
            self.db.upsert_download(
                DownloadItem(
                    torrent_hash=release.info_hash,
                    name=release.title,
                    state="selecting",
                    progress=0,
                    save_path=str(slot),
                    content_path=str(slot),
                    media_id=media_id,
                    episode=episode,
                    is_batch=batch,
                    raw={
                        "backend": "qbittorrent",
                        "_race_id": race_id,
                        "_race_stage": "adding",
                        "_intent_revision": intent_revision,
                        "_release_numbering": DownloadIntentStore.numbering_metadata(release),
                    },
                )
            )
            journal("adding")
            torrent_hash = self._torrent_network_start(
                client,
                "add_release",
                release,
                save_path=slot,
                category=self.config.qbittorrent.category,
                tags=[APP_SLUG, race_tag],
                paused=False,
            )
            if not torrent_hash:
                return False
            entries[-1] = (release, str(torrent_hash), slot)
            self.db.upsert_download(
                DownloadItem(
                    torrent_hash=str(torrent_hash),
                    name=release.title,
                    state="selecting",
                    progress=0,
                    save_path=str(slot),
                    content_path=str(slot),
                    media_id=media_id,
                    episode=episode,
                    is_batch=batch,
                    raw={
                        "backend": "qbittorrent",
                        "_race_id": race_id,
                        "_race_stage": "added",
                        "_intent_revision": intent_revision,
                        "_release_numbering": DownloadIntentStore.numbering_metadata(release),
                    },
                )
            )
            journal("added")
            self._torrent_network_start(client, "start", torrent_hash)
            self.logger.info(
                "START step=torrent.race candidate=%s/%s hash=%s score=%.1f seeds=%s leechers=%s title=%r",
                index + 1,
                len(pool),
                torrent_hash,
                release.score,
                release.seeders,
                release.leechers,
                release.title,
            )
            return True

        def observe() -> None:
            for release, torrent_hash, _slot in entries:
                snapshot = client.torrent_status(torrent_hash)
                if snapshot is None:
                    continue
                latest_snapshots[torrent_hash.casefold()] = dict(snapshot)
                if self._torrent_race_is_alive(snapshot):
                    live_hashes.add(torrent_hash.casefold())
                self.logger.info(
                    "POLL step=torrent.race hash=%s live=%s speed=%s downloaded=%s progress=%s seeds=%s peers=%s score=%.1f",
                    torrent_hash,
                    torrent_hash.casefold() in live_hashes,
                    snapshot.get("dlspeed"),
                    snapshot.get("downloaded"),
                    snapshot.get("progress"),
                    snapshot.get("num_seeds"),
                    snapshot.get("num_leechs"),
                    release.score,
                )

        def remove_entry(entry: tuple[NyaaRelease, str, Path]) -> None:
            _release, torrent_hash, _slot = entry
            self._delete_confirmed_torrent(client, torrent_hash, delete_files=True)
            self.db.delete_torrent_records(torrent_hash)

        def cleanup_empty_dirs() -> None:
            for _release, _torrent_hash, slot in reversed(entries):
                try:
                    slot.rmdir()
                except OSError:
                    pass
            try:
                race_root.rmdir()
            except OSError:
                pass

        def finalize(winner_entry: tuple[NyaaRelease, str, Path]) -> NyaaRelease:
            nonlocal winner_hash_for_journal
            winner, winner_hash, _winner_slot = winner_entry
            winner_hash_for_journal = winner_hash
            journal("winner_selected")
            for entry in entries:
                if entry[1] != winner_hash:
                    remove_entry(entry)
                release, torrent_hash, _slot = entry
                self.release_telemetry.record(
                    release,
                    media_id=media_id,
                    media_episode=episode,
                    provider="qbittorrent",
                    outcome="winner" if torrent_hash == winner_hash else "loser",
                    snapshot=latest_snapshots.get(torrent_hash.casefold()),
                    elapsed_seconds=time.monotonic() - started_at,
                )
            if stale_existing_hash and stale_existing_hash.casefold() != winner_hash.casefold():
                try:
                    self._delete_confirmed_torrent(client, stale_existing_hash, delete_files=True)
                    delete_records = getattr(self.db, "delete_torrent_records", None)
                    if callable(delete_records):
                        delete_records(stale_existing_hash)
                    self.logger.info(
                        "DONE step=torrent.race_replace_stalled old_hash=%s winner_hash=%s",
                        stale_existing_hash,
                        winner_hash,
                    )
                except QBittorrentError as exc:
                    self.logger.warning(
                        "WARN step=torrent.race_replace_stalled old_hash=%s error=%r",
                        stale_existing_hash,
                        str(exc),
                    )
            client.set_location(winner_hash, target)
            journal("winner_moved")
            remover = getattr(client, "remove_tags", None)
            if callable(remover):
                remover(winner_hash, {race_tag})
            client.set_metadata(
                winner_hash,
                category=self.config.qbittorrent.category,
                tags=final_tags,
            )
            record = self.db.download_by_hash(winner_hash) if hasattr(self.db, "download_by_hash") else None
            if record is not None:
                record.raw.pop("_race_id", None)
                record.raw.pop("_race_stage", None)
                record.save_path = record.content_path = str(target)
                record.state = "downloading"
                self.db.upsert_download(record)
            self._torrent_network_start(client, "start", winner_hash)
            try:
                for item in client.torrents(category=""):
                    hashes = {
                        str(item.torrent_hash or "").casefold(),
                        str(item.raw.get("infohash_v1") or "").casefold(),
                        str(item.raw.get("infohash_v2") or "").casefold(),
                    }
                    if winner_hash.casefold() in hashes:
                        self._resolve_download_media(item)
                        self.db.upsert_download(item)
                        break
            except QBittorrentError as exc:
                if entries:
                    raise
                self.logger.warning("WARN step=torrent.race_db_sync hash=%s error=%r", winner_hash, str(exc))
            release_hash = winner.info_hash or hashlib.sha1(winner.magnet.encode()).hexdigest()
            try:
                self.db.record_release(
                    release_hash,
                    media_id,
                    episode,
                    winner.title,
                    winner.score,
                    release_episode=parsed_release_episode(winner.title),
                    score_formula=getattr(winner, "score_formula", ""),
                    score_context=getattr(winner, "score_context", ""),
                    numbering_metadata=DownloadIntentStore.numbering_metadata(winner),
                )
            except TypeError:
                # Compatibility for small third-party/fake DB adapters that
                # still implement the pre-v5 positional surface.
                self.db.record_release(release_hash, media_id, episode, winner.title, winner.score)
            cleaner = getattr(client, "delete_tags", None)
            if callable(cleaner):
                try:
                    cleaner({race_tag})
                except QBittorrentError:
                    pass
            cleanup_empty_dirs()
            self.download_intents.update(
                media_id,
                episode,
                batch,
                state="downloading",
                selected=winner,
                backend="qbittorrent",
                expected_revision=intent_revision,
                owned_candidates=[],
                winner_finalized=True,
            )
            self.log(
                f"qBittorrent: chose the live release {winner.title} "
                f"(score={winner.score:.1f}, seeds={winner.seeders}, leechers={winner.leechers})"
            )
            return winner

        try:
            existing_request = self._existing_download_for_request(
                client, anime, episode=episode, batch=batch
            )
            if existing_request is not None:
                snapshot = client.torrent_status(existing_request.torrent_hash)
                if self._torrent_race_is_alive(snapshot):
                    matched = next(
                        (
                            release
                            for release in pool
                            if release.info_hash
                            and release.info_hash.casefold() == existing_request.torrent_hash.casefold()
                        ),
                        None,
                    )
                    if matched is None:
                        matched = replace(
                            pool[0],
                            info_hash=existing_request.torrent_hash,
                            title=existing_request.name or pool[0].title,
                        )
                    self.logger.info(
                        "DONE step=torrent.race_existing_live hash=%s title=%r",
                        existing_request.torrent_hash,
                        existing_request.name,
                    )
                    self.download_intents.update(
                        media_id,
                        episode,
                        batch,
                        state="downloading",
                        selected=matched,
                        backend="qbittorrent",
                        expected_revision=intent_revision,
                    )
                    return matched
                if float(existing_request.progress or 0.0) < 0.01:
                    stale_existing_hash = existing_request.torrent_hash

            first_added = False
            try:
                first_added = add_one(0, pool[0])
            except QBittorrentError as exc:
                if entries:
                    raise
                self.logger.warning(
                    "FALLBACK step=torrent.race_first_add title=%r error=%r",
                    pool[0].title,
                    str(exc),
                )

            # Fast path: a healthy top-ranked release gets ten seconds alone.
            if first_added:
                fast_deadline = started_at + max(0.0, fast_seconds)
                while time.monotonic() < fast_deadline:
                    observe()
                    first_hash = entries[0][1].casefold()
                    if first_hash in live_hashes:
                        self.logger.info(
                            "DONE step=torrent.race_fast hash=%s elapsed=%.1f",
                            entries[0][1],
                            time.monotonic() - started_at,
                        )
                        return finalize(entries[0])
                    remaining = fast_deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(max(0.05, poll_seconds), remaining))
                observe()
                if entries and entries[0][1].casefold() in live_hashes:
                    return finalize(entries[0])

            # Top-1 did not move. Start at most two alternatives in parallel.
            for index, release in enumerate(pool[1:], start=1):
                try:
                    add_one(index, release)
                except QBittorrentError as exc:
                    if entries:
                        raise
                    self.logger.warning(
                        "WARN step=torrent.race_add candidate=%s title=%r error=%r",
                        index + 1,
                        release.title,
                        str(exc),
                    )

            if not entries:
                cleanup_empty_dirs()
                self.download_intents.update(
                    media_id,
                    episode,
                    batch,
                    state="waiting",
                    backend="qbittorrent",
                    detail="No candidate could be added",
                    expected_revision=intent_revision,
                )
                self.logger.info(
                    "FAIL step=nyaa.race backend=qbittorrent media_id=%s episode=%s reason=no_candidate_added",
                    media_id,
                    episode,
                )
                return None

            race_deadline = started_at + max(float(total_seconds), float(fast_seconds))
            while time.monotonic() < race_deadline:
                observe()
                remaining = race_deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(max(0.05, poll_seconds), remaining))
            observe()

            # `pool` is already ranked. Keep the highest-score release that actually moved.
            winner_entry = next(
                (entry for entry in entries if entry[1].casefold() in live_hashes),
                None,
            )
            if winner_entry is None:
                for entry in entries:
                    remove_entry(entry)
                    release, torrent_hash, _slot = entry
                    self.release_telemetry.record(
                        release,
                        media_id=media_id,
                        media_episode=episode,
                        provider="qbittorrent",
                        outcome="no_progress",
                        snapshot=latest_snapshots.get(torrent_hash.casefold()),
                        elapsed_seconds=time.monotonic() - started_at,
                    )
                cleanup_empty_dirs()
                self.logger.warning(
                    "FALLBACK step=torrent.race_none_live media_id=%s episode=%s batch=%s candidates=%s",
                    media_id,
                    episode,
                    batch,
                    len(entries),
                )
                self.download_intents.update(
                    media_id,
                    episode,
                    batch,
                    state="waiting",
                    backend="qbittorrent",
                    detail="No candidate started downloading",
                    expected_revision=intent_revision,
                )
                return None
            return finalize(winner_entry)
        except (QBittorrentError, ManagerError) as exc:
            self.download_intents.update(
                media_id,
                episode,
                batch,
                state="recovery_required",
                backend="qbittorrent",
                detail=str(exc),
                expected_revision=intent_revision,
            )
            self.logger.warning(
                "RETRY step=torrent.race_recovery media_id=%s episode=%s owned=%s error=%r",
                media_id,
                episode,
                [entry[1] for entry in entries],
                str(exc),
            )
            return None
        finally:
            client.close()

    def search_and_add_best(
        self,
        media_id: int,
        *,
        episode: int | None,
        batch: bool,
        require_score: float | None = None,
        automatic: bool = False,
        require_explicit_batch: bool = False,
        raise_when_unavailable: bool = False,
    ) -> NyaaRelease | None:
        """Search and add the best release; ``None`` when none qualifies.

        With ``raise_when_unavailable`` a search whose release search could not
        be reached at all raises ReleaseSearchUnavailableError instead of
        returning ``None`` ("nothing found") when nothing was added.
        """
        eligible = self.release_candidates(
            media_id,
            episode=episode,
            batch=batch,
            require_score=require_score,
            automatic=automatic,
            require_explicit_batch=require_explicit_batch,
            raise_when_unavailable=raise_when_unavailable,
        )
        if not eligible:
            return None
        return self.add_release_candidates(media_id, eligible, episode=episode, batch=batch)

    def release_candidates(
        self,
        media_id: int,
        *,
        episode: int | None,
        batch: bool,
        require_score: float | None = None,
        automatic: bool = False,
        require_explicit_batch: bool = False,
        raise_when_unavailable: bool = False,
    ) -> list[NyaaRelease]:
        """The releases search_and_add_best would race, best first; nothing is added.

        Planning searches every episode first and adds them only when all were found.
        """
        if not automatic:
            self._allow_redownload(media_id, None if batch else episode)
        _RELEASE_SEARCH_OUTCOME.unavailable = None
        releases = self.search_releases(
            media_id,
            episode=episode,
            batch=batch,
            automatic=bool(automatic),
        )
        unavailable = getattr(_RELEASE_SEARCH_OUTCOME, "unavailable", None)
        threshold = self.config.nyaa.min_release_score if require_score is None else require_score
        eligible = [
            item
            for item in releases
            if item.score >= threshold
            and self._release_seed_requirement_met(item)
            and (not require_explicit_batch or self._release_is_safe_batch_candidate(media_id, item))
            and (batch or episode is None or self._release_has_safe_episode_identity(item))
        ]
        record_video_selection_debug(
            self.config.paths.cache_dir,
            media_id=media_id,
            episode=episode,
            mode="manual_best",
            releases=releases,
            selected=eligible[0] if eligible else None,
            threshold=float(threshold),
        )
        if not eligible and raise_when_unavailable and unavailable is not None:
            raise unavailable
        return eligible

    def add_release_candidates(
        self,
        media_id: int,
        eligible: list[NyaaRelease],
        *,
        episode: int | None,
        batch: bool,
    ) -> NyaaRelease | None:
        """Add the best of ``eligible`` (a race when there are several); the release added or None."""
        if not eligible:
            return None
        best = eligible[0]
        if self.downloads_enabled() and not self.torrent_paused_on_add() and len(eligible) > 1:
            return self._race_add_candidates(media_id, eligible, episode=episode, batch=batch)
        single_backend = "qbittorrent" if self.config.qbittorrent.enabled else "aria2"
        intent = self.download_intents.begin(media_id, episode, batch, [best], backend=single_backend)
        intent_revision = int(intent.get("revision") or 0)
        self.add_release(media_id, best, episode=episode, batch=batch)
        self.download_intents.update(
            media_id,
            episode,
            batch,
            state="downloading",
            selected=best,
            backend=single_backend,
            expected_revision=intent_revision,
        )
        return best

    def planning_batch_status(self, media_id: int, *, since: float = 0.0) -> tuple[str, str]:
        """After a season-pack step that reported no pack: (pack, failure).

        ``pack`` names a season-pack torrent of this title that is still in the
        client and not finished (the race kept or reused it but said nothing);
        ``failure`` is the error that ended this step's race (state
        ``recovery_required`` written after ``since``). Each is "" when absent.
        """
        anime = self.db.get_anime(int(media_id))
        if anime is not None:
            clients = self.torrent_clients()
            try:
                for name, client in clients:
                    try:
                        item = self._existing_download_for_request(client, anime, episode=None, batch=True)
                    except (QBittorrentError, ManagerError, OSError) as exc:
                        self.logger.warning(
                            "SKIP step=planning_episode.batch_present backend=%s media_id=%s error=%r",
                            name,
                            media_id,
                            str(exc),
                        )
                        continue
                    if item is not None and not self._download_is_complete(item):
                        return str(item.name or ""), ""
            finally:
                for _name, client in clients:
                    client.close()
        intents = getattr(self, "download_intents", None)
        intent = intents.get(int(media_id), None, True) if intents is not None else None
        if (
            intent
            and str(intent.get("state") or "") == "recovery_required"
            and float(intent.get("updated_at") or 0.0) >= float(since)
        ):
            return "", str(intent.get("detail") or "")
        return "", ""

    @staticmethod
    def _recently_finished(end_date: str | None, *, days: int = 60) -> bool:
        raw = str(end_date or "").strip()
        if not raw:
            return False
        try:
            parts = [int(x) for x in raw.split("-")]
            finished = datetime.date(
                parts[0], parts[1] if len(parts) > 1 else 12, parts[2] if len(parts) > 2 else 28
            )
        except (ValueError, IndexError):
            return False
        return 0 <= (datetime.date.today() - finished).days <= days

    ANILIST_STALE_SYNC_SECONDS = 24 * 3600.0

    def _anilist_sync_stale(self, *, now: float | None = None) -> bool:
        """AniList has not answered a list refresh for a day (an outage, a block)."""
        if not self.config.anilist.enabled or not self.config.anilist.access_token.strip():
            return False
        try:
            synced = float(self.db.get_state("anilist_synced_at", "0") or 0)
        except ValueError:
            synced = 0.0
        moment = time.time() if now is None else float(now)
        return synced > 0 and moment - synced > self.ANILIST_STALE_SYNC_SECONDS

    @staticmethod
    def _released_episodes_for_search(
        anime: LibraryAnime, *, stale_sync: bool, now: float | None = None
    ) -> int | None:
        """``released_episodes``, estimated forward weekly while the AniList data is stale.

        The cached next airing episode stops moving when AniList cannot be
        reached, which used to freeze a weekly show after one more episode.
        Only during such an outage: one episode a week since the cached airing
        time, never beyond the episode count. Searching for an episode that has
        not aired (a break week) costs only a search.
        """
        released = anime.released_episodes
        if (
            not stale_sync
            or released is None
            or str(anime.media_status or "").upper() != "RELEASING"
            or not anime.next_airing_episode
            or not anime.next_airing_at
        ):
            return released
        moment = time.time() if now is None else float(now)
        weeks = int(max(0.0, moment - float(anime.next_airing_at)) // (7 * 86400))
        if weeks <= 0:
            return released
        estimate = released + weeks
        if anime.episodes:
            estimate = min(estimate, int(anime.episodes))
        return max(released, estimate)

    @contextmanager
    def release_search_cancellable(self, should_continue: Callable[[], bool]) -> Iterator[None]:
        """Release searches in this thread stop between requests once ``should_continue()``
        is False (cooperative cancel: nothing is added after that)."""
        previous = getattr(_RELEASE_SEARCH_CANCEL, "check", None)
        _RELEASE_SEARCH_CANCEL.check = should_continue
        try:
            yield
        finally:
            _RELEASE_SEARCH_CANCEL.check = previous

    @staticmethod
    def _release_search_cancelled() -> bool:
        check = getattr(_RELEASE_SEARCH_CANCEL, "check", None)
        if not callable(check):
            return False
        try:
            return not check()
        except Exception:  # noqa: BLE001 - a broken check never cancels
            return False

    def _log_release_search_cancelled(self, step: str, **fields: Any) -> None:
        self.logger.info(
            "SKIP step=%s reason=cancelled %s",
            step,
            " ".join(f"{key}={value}" for key, value in fields.items()),
        )

    # An older aired episode that no release search found keeps being searched every pass for
    # its first misses, then waits 20 min, 40, 80 ... up to 3 h between searches. The newest
    # aired episode is searched every pass (a fresh release may appear any minute).
    AUTO_SEARCH_MISS_PREFIX = "auto_search_miss:"
    AUTO_SEARCH_BACKOFF_AFTER_MISSES = 6
    AUTO_SEARCH_BACKOFF_BASE_SECONDS = 20 * 60.0
    AUTO_SEARCH_BACKOFF_MAX_SECONDS = 3 * 3600.0

    @contextmanager
    def ignore_auto_search_backoff(self) -> Iterator[None]:
        """Automatic searches in this thread search every missing episode (a user's request)."""
        previous = bool(getattr(_AUTO_SEARCH_BYPASS, "active", False))
        _AUTO_SEARCH_BYPASS.active = True
        try:
            yield
        finally:
            _AUTO_SEARCH_BYPASS.active = previous

    @classmethod
    def _auto_search_miss_key(cls, media_id: int, episode: int) -> str:
        return f"{cls.AUTO_SEARCH_MISS_PREFIX}{int(media_id)}:{int(episode)}"

    def _auto_search_misses(self) -> dict[str, dict[str, Any]]:
        """Every remembered miss (one query a pass): key -> {"misses", "next_at", ...}."""
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    f"SELECT key,value FROM state WHERE {STATE_PREFIX_SQL}",
                    state_prefix_bounds(self.AUTO_SEARCH_MISS_PREFIX),
                ).fetchall()
        except (AttributeError, sqlite3.Error) as exc:
            self.logger.warning("SKIP step=nyaa.auto_search_backoff reason=unreadable error=%r", str(exc))
            return {}
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                value = json.loads(str(row["value"] or "{}"))
            except (TypeError, ValueError):
                value = {}
            result[str(row["key"])] = value if isinstance(value, dict) else {}
        return result

    def _note_auto_search_miss(
        self, misses: dict[str, dict[str, Any]], media_id: int, episode: int, *, now: float
    ) -> None:
        key = self._auto_search_miss_key(media_id, episode)
        try:
            count = int((misses.get(key) or {}).get("misses") or 0) + 1
        except (TypeError, ValueError):
            count = 1
        entry: dict[str, Any] = {"misses": count, "last_miss_at": round(now, 3)}
        over = count - self.AUTO_SEARCH_BACKOFF_AFTER_MISSES
        if over >= 0:
            delay = min(
                self.AUTO_SEARCH_BACKOFF_MAX_SECONDS,
                self.AUTO_SEARCH_BACKOFF_BASE_SECONDS * 2 ** min(over, 16),
            )
            entry["next_at"] = round(now + delay, 3)
            if over == 0:
                self.logger.info(
                    "RESULT step=nyaa.auto_search_backoff media_id=%s episode=%s misses=%s retry_in_s=%.0f "
                    "reason=no_suitable_release",
                    media_id,
                    episode,
                    count,
                    delay,
                )
        misses[key] = entry
        try:
            self.db.set_state(key, json.dumps(entry, separators=(",", ":")))
        except sqlite3.Error as exc:
            self.logger.warning("SKIP step=nyaa.auto_search_backoff key=%s error=%r", key, str(exc))

    def _forget_auto_search_misses(self, keys: Iterable[str]) -> None:
        wanted = [key for key in dict.fromkeys(keys) if key]
        if not wanted:
            return
        try:
            self.db.delete_states(wanted)
        except sqlite3.Error as exc:
            self.logger.warning("SKIP step=nyaa.auto_search_backoff_reset error=%r", str(exc))

    def _reset_auto_search_backoff(self, media_id: int, episode: int | None = None) -> None:
        """Forget the misses of one episode (``None``: of every episode of the title)."""
        try:
            if episode is not None:
                self.db.delete_state(self._auto_search_miss_key(media_id, int(episode)))
                return
            with self.db.connect() as conn:
                conn.execute(
                    f"DELETE FROM state WHERE {STATE_PREFIX_SQL}",
                    state_prefix_bounds(f"{self.AUTO_SEARCH_MISS_PREFIX}{int(media_id)}:"),
                )
        except (AttributeError, sqlite3.Error) as exc:
            self.logger.warning(
                "SKIP step=nyaa.auto_search_backoff_reset media_id=%s error=%r", media_id, str(exc)
            )

    def auto_search_current(self) -> int:
        # Every periodic pass runs this, also with automatic downloads off: a
        # Planning season pack stuck at 0 bytes moves on to its next candidate.
        try:
            self.replace_stalled_aria2_downloads()
        except Exception as exc:  # noqa: BLE001 - never block the release search
            self.logger.warning("FAIL step=aria2.stall_replace error=%r", str(exc)[:200])
        try:
            self.cleanup_aria2_leftovers()
        except Exception as exc:  # noqa: BLE001 - never block the release search
            self.logger.warning("FAIL step=library.aria2_leftovers error=%r", str(exc)[:200])
        try:
            self.cleanup_orphan_aria2_controls()
        except Exception as exc:  # noqa: BLE001 - never block the release search
            self.logger.warning("FAIL step=library.aria2_orphan_controls error=%r", str(exc)[:200])
        try:
            # Releases the user chose while Torrents were off: before the automatic
            # search, so it finds them downloading instead of picking another copy.
            self.start_queued_releases()
        except Exception as exc:  # noqa: BLE001 - never block the release search
            self.logger.warning("FAIL step=release.queued_start error=%r", str(exc)[:200])
        if not (
            self.config.nyaa.enabled
            and self.config.nyaa.auto_download_current
            and self.downloads_configured()
        ):
            return 0
        if not self._storage_can_accept(0):
            # Not even a typical episode fits: no searches, a visible pause instead.
            self._log_storage_pause()
            return 0
        count = 0
        stale_sync = self._anilist_sync_stale()
        misses = self._auto_search_misses()
        backoff_bypassed = bool(getattr(_AUTO_SEARCH_BYPASS, "active", False))
        pass_started = time.time()
        # Where each title's search starts this pass (misses below it are stale), and
        # titles whose released count is unknown (their misses are kept as they are).
        search_starts: dict[int, int] = {}
        keep_misses_of: set[int] = set()
        stale_misses: set[str] = set()
        # CURRENT is the normal case.  Also keep following a RELEASING title in
        # PLANNING once it already has local video: progress=0 must not freeze
        # an airing download chain merely because the user has not watched the
        # first downloaded episode yet.  Do not auto-start arbitrary Planning
        # entries with no local ownership signal.
        candidates = list(self.db.anime_list(("CURRENT",)))
        current_ids = {item.media_id for item in candidates}
        # A PLANNING title followed while airing must also get its *final*
        # episodes: when the last one airs AniList flips it to FINISHED, which
        # used to drop it from the chain (Sayonara Lara: 1-11 local, 12 never
        # searched). Only the tail after the newest local episode is followed,
        # and only for a recently finished show.
        planning_tail_start: dict[int, int] = {}
        for anime in self.db.anime_list(("PLANNING",)):
            if anime.media_id in current_ids or anime.media_status not in {"RELEASING", "FINISHED"}:
                continue
            local = [item for item in self.db.episodes(anime.media_id) if item.video_path.is_file()]
            if not local:
                continue
            if anime.media_status == "FINISHED":
                if not self._recently_finished(anime.end_date):
                    continue
                newest = max((int(item.episode) for item in local if item.episode is not None), default=0)
                if newest <= 0:
                    continue
                planning_tail_start[int(anime.media_id)] = newest + 1
            candidates.append(anime)

        for anime in candidates:
            if self._release_search_cancelled():
                self._log_release_search_cancelled("nyaa.auto_search", started=count)
                return count
            tail_start = planning_tail_start.get(int(anime.media_id))
            if anime.media_status != "RELEASING" and anime.status == "PLANNING" and tail_start is None:
                continue
            start = anime.progress + 1
            if tail_start is not None:
                start = max(start, tail_start)
            released = self._released_episodes_for_search(anime, stale_sync=stale_sync)
            search_starts[int(anime.media_id)] = start

            # Never guess an episode beyond the locally cached release boundary.
            # A completed/up-to-date title must not trigger Nyaa requests. The
            # later AniList sync will advance released_episodes when a new episode
            # actually airs, and the next lightweight refresh/agent pass can then
            # search for it.
            if released is None:
                keep_misses_of.add(int(anime.media_id))
                self.log(f"{anime.title}: release search skipped — released episode count is unknown")
                self.logger.info(
                    "SKIP step=nyaa.auto_search media_id=%s title=%r reason=released_unknown progress=%s",
                    anime.media_id,
                    anime.title,
                    anime.progress,
                )
                continue
            if start > released:
                self.log(
                    f"{anime.title}: release search skipped — progress {anime.progress} >= released {released}"
                )
                self.logger.info(
                    "SKIP step=nyaa.auto_search media_id=%s title=%r reason=up_to_date progress=%s released=%s",
                    anime.media_id,
                    anime.title,
                    anime.progress,
                    released,
                )
                continue

            # Bound network work by the number of *missing local episodes*, not
            # by a short numeric window after AniList progress. A user can own
            # episodes 2..6 while their AniList progress is still 1; the old
            # window inspected only 2..3, saw both locally, and never reached a
            # newly aired episode 7. Local episodes are free to skip and must not
            # consume the automatic search budget.
            search_budget = max(1, self.config.nyaa.max_auto_download_per_anime)
            searched_missing = 0
            covered_by_double: set[int] = set()
            blocked = self._redownload_block(anime.media_id)
            for episode in range(start, released + 1):
                miss_key = self._auto_search_miss_key(anime.media_id, episode)
                if episode in covered_by_double or self.db.has_episode(anime.media_id, episode):
                    if miss_key in misses:
                        stale_misses.add(miss_key)  # it is here now: its misses are over
                    continue
                if episode in blocked:
                    # Deleted from the anime menu: not fetched again automatically.
                    continue
                if not backoff_bypassed and episode < released:
                    try:
                        next_at = float((misses.get(miss_key) or {}).get("next_at") or 0.0)
                    except (TypeError, ValueError):
                        next_at = 0.0
                    if next_at > pass_started:
                        # Missed many times: searched again later, without using the budget.
                        self.logger.debug(
                            "SKIP step=nyaa.auto_search media_id=%s episode=%s reason=miss_backoff retry_in_s=%.0f",
                            anime.media_id,
                            episode,
                            next_at - pass_started,
                        )
                        continue
                if searched_missing >= search_budget and episode < released:
                    # The budget is for older episodes: the newest aired one is searched every
                    # pass, also when older ones that keep missing used the budget up.
                    continue
                if self._auto_search_intent_unconfirmed(anime.media_id, episode):
                    continue
                if self._release_search_cancelled():
                    self._log_release_search_cancelled("nyaa.auto_search", started=count)
                    return count
                searched_missing += 1
                self._duplicate_block(anime.media_id, episode, False, take=True)
                try:
                    releases = self.search_releases(
                        anime.media_id, episode=episode, batch=False, automatic=True
                    )
                    if self._release_search_cancelled():
                        # Torrent Off while this search ran: nothing is added any more.
                        self._log_release_search_cancelled(
                            "nyaa.auto_search", media_id=anime.media_id, episode=episode, started=count
                        )
                        return count
                    storage_rejections = int(getattr(self, "_storage_rejections", 0))
                    eligible = self.automatic_release_candidates(releases)
                    eligible = self._prefer_single_episode_releases(anime, episode, eligible)
                    best = eligible[0] if eligible else None
                    if best is not None and self._release_double_span(best) is not None:
                        covered_by_double.add(episode + 1)
                    record_video_selection_debug(
                        self.config.paths.cache_dir,
                        media_id=anime.media_id,
                        episode=episode,
                        mode="automatic",
                        releases=releases,
                        selected=best,
                        threshold=float(self.config.nyaa.min_release_score),
                    )
                    if best is not None:
                        single_backend = "qbittorrent" if self.config.qbittorrent.enabled else "aria2"
                        if not self.downloads_enabled():
                            self.logger.info(
                                "WAIT step=nyaa.auto media_id=%s episode=%s reason=torrent_traffic_off title=%r",
                                anime.media_id,
                                episode,
                                best.title,
                            )
                            intent = self.download_intents.begin(
                                anime.media_id,
                                episode,
                                False,
                                eligible or [best],
                                backend=single_backend,
                            )
                            intent_revision = int(intent.get("revision") or 0)
                            self.download_intents.update(
                                anime.media_id,
                                episode,
                                False,
                                state="waiting",
                                selected=best,
                                backend=single_backend,
                                detail="Torrent traffic is off",
                                expected_revision=intent_revision,
                            )
                        elif not self.torrent_paused_on_add() and len(eligible) > 1:
                            best = self._race_add_candidates(
                                anime.media_id,
                                eligible,
                                episode=episode,
                                batch=False,
                            )
                        else:
                            intent = self.download_intents.begin(
                                anime.media_id,
                                episode,
                                False,
                                [best],
                                backend=single_backend,
                            )
                            intent_revision = int(intent.get("revision") or 0)
                            self.add_release(anime.media_id, best, episode=episode, batch=False)
                            self.download_intents.update(
                                anime.media_id,
                                episode,
                                False,
                                state="downloading",
                                selected=best,
                                backend=single_backend,
                                expected_revision=intent_revision,
                            )
                except UnconfirmedCandidatesError:
                    # Another pass journaled candidates meanwhile: never abort the
                    # whole pass (and the subtitles after it) for one title.
                    self._log_intent_unconfirmed(anime.media_id, episode)
                    continue
                except NoReleaseSourceError:
                    # No source in Settings → Sources: one quiet line, no error per title.
                    self._log_no_release_source("nyaa.auto_search")
                    return count
                except (NyaaError, QBittorrentError, ManagerError) as exc:
                    self.log(f"{anime.title} #{episode}: {exc}")
                    continue
                except Exception as exc:  # noqa: BLE001 - one title never ends the pass
                    # A folder that cannot be made (the library disk away), a locked database
                    # or a bug for one release used to leave this loop and the rest of the
                    # maintenance pass: no other title was searched, nothing was cleaned up,
                    # every pass again.
                    self.logger.warning(
                        "FAIL step=nyaa.auto_search media_id=%s episode=%s error=%r",
                        anime.media_id,
                        episode,
                        f"{type(exc).__name__}: {exc}"[:300],
                    )
                    self.log(f"{anime.title} #{episode}: {exc}")
                    continue
                if best is None:
                    if int(getattr(self, "_storage_rejections", 0)) > storage_rejections:
                        self.log(
                            f"{anime.title} #{episode}: automatic download paused — not enough disk space"
                        )
                        continue
                    blocker = self._duplicate_block(anime.media_id, episode, False, take=True)
                    if blocker is not None:
                        if miss_key in misses:
                            stale_misses.add(miss_key)
                        self.log(
                            f"{anime.title} #{episode}: not downloaded, an existing torrent covers it: {blocker[1]}"
                        )
                        continue
                    self.log(f"{anime.title} #{episode}: no suitable release found automatically yet")
                    if episode < released:
                        self._note_auto_search_miss(misses, anime.media_id, episode, now=time.time())
                    continue
                if miss_key in misses:
                    stale_misses.add(miss_key)
                count += 1
        for key in misses:
            # Titles no longer followed, and episodes now below the title's progress.
            try:
                media_text, _, episode_text = key[len(self.AUTO_SEARCH_MISS_PREFIX) :].partition(":")
                media_id, episode = int(media_text), int(episode_text)
            except ValueError:
                stale_misses.add(key)
                continue
            if media_id in keep_misses_of:
                continue
            if media_id not in search_starts or episode < search_starts[media_id]:
                stale_misses.add(key)
        self._forget_auto_search_misses(stale_misses)
        try:
            self.prioritize_nearest_episodes()
        except Exception as exc:  # noqa: BLE001 - queue order never blocks the release search
            self.logger.warning("FAIL step=downloads.nearest_first error=%r", str(exc)[:200])
        return count

    def prioritize_nearest_episodes(self) -> int:
        """Several episodes of one anime downloading: the one nearest to the next to watch goes
        first in the torrent client's queue (and takes a slot from a farther one in aria2)."""
        groups: dict[int, list[DownloadItem]] = {}
        for item in self.db.downloads():
            if (
                item.media_id is None
                or item.episode is None
                or item.is_batch
                or float(item.progress or 0) >= 1.0
            ):
                continue
            groups.setdefault(int(item.media_id), []).append(item)
        ordered: dict[str, list[list[str]]] = {}
        preferred = "qbittorrent" if self.config.qbittorrent.enabled else "aria2"
        for media_id, items in groups.items():
            if len(items) < 2:
                continue
            anime = self.db.get_anime(media_id)
            order = download_order(items, getattr(anime, "progress", None) if anime is not None else None)
            by_backend: dict[str, list[str]] = {}
            for item in order:
                backend = str(item.raw.get("backend") or preferred).casefold()
                by_backend.setdefault(backend, []).append(item.torrent_hash)
            for backend, hashes in by_backend.items():
                if len(hashes) > 1:
                    ordered.setdefault(backend, []).append(hashes)
        if not ordered:
            return 0
        changed = 0
        for backend_name, client in self.torrent_clients():
            try:
                arrange = getattr(client, "prioritize_queue", None)
                if not callable(arrange):
                    continue
                for hashes in ordered.get(backend_name, []):
                    try:
                        if arrange(hashes):
                            changed += 1
                            self.logger.info(
                                "DONE step=downloads.nearest_first backend=%s first=%s count=%s",
                                backend_name,
                                hashes[0],
                                len(hashes),
                            )
                    except (QBittorrentError, ManagerError) as exc:
                        self.logger.warning(
                            "WARN step=downloads.nearest_first backend=%s error=%r",
                            backend_name,
                            str(exc)[:200],
                        )
            finally:
                client.close()
        return changed

    def _log_intent_unconfirmed(self, media_id: int, episode: int | None) -> None:
        self.logger.info(
            "SKIP step=auto_search reason=intent_unconfirmed media_id=%s episode=%s", media_id, episode
        )

    def _auto_search_intent_unconfirmed(self, media_id: int, episode: int) -> bool:
        """An earlier attempt still owns unconfirmed candidates: reconcile, else skip."""
        intents = getattr(self, "download_intents", None)
        unconfirmed = getattr(intents, "unconfirmed", None)
        if not callable(unconfirmed) or not unconfirmed(media_id, episode, False):
            return False
        try:
            self.recover_acquisition_attempts()
        except Exception as exc:  # noqa: BLE001 - recovery is retried by the next sync
            self.logger.warning("FAIL step=download.recover media_id=%s error=%r", media_id, str(exc)[:200])
        if not unconfirmed(media_id, episode, False):
            return False
        self._log_intent_unconfirmed(media_id, episode)
        return True

    def _release_group_is_configured_trusted(self, item: NyaaRelease) -> bool:
        return any(
            group.casefold() in item.group.casefold()
            for group in self.config.nyaa.trusted_groups
            if group.strip()
        )

    def _release_is_trusted(self, item: NyaaRelease) -> bool:
        return bool(item.trusted or self._release_group_is_configured_trusted(item))

    def _release_seed_requirement_met(self, item: NyaaRelease) -> bool:
        fresh_trusted = fresh_trusted_zero_seeders_allowed(item, self.config.nyaa.trusted_groups)
        if fresh_trusted:
            self.logger.info(
                "ALLOW step=nyaa.auto reason=fresh_trusted_zero_seeders "
                "group=%r seeders=%s published=%r title=%r",
                item.group,
                item.seeders,
                item.published,
                item.title,
            )
        return bool(
            item.category_id == "subsplease-rss"
            or not getattr(item, "seeders_known", True)
            or item.seeders >= self.config.nyaa.min_seeders
            or fresh_trusted
        )

    @staticmethod
    def _release_uses_absolute_episode_alias(item: NyaaRelease) -> bool:
        reasons = [str(reason) for reason in item.reasons]
        return bool(
            any(reason.startswith("absolute-ep=") for reason in reasons)
            and any(reason.startswith("relative-ep=") for reason in reasons)
        )

    def _with_chain_season(self, anime: LibraryAnime) -> LibraryAnime:
        """The anime with its place in the chain of seasons, for release identity.

        Only for a sequel whose titles carry no season number, from the cached
        relation graph (no AniList call): "Shouwa Genroku Rakugo Shinjuu:
        Sukeroku Futatabi-hen" follows one season, so packs named "... S2" or
        "... Season 2" are this entry. Anything unresolved leaves it as it is.
        """
        # Planning rows often carry no relations: the resolver's cached graph decides.
        memo = self.__dict__.setdefault("_chain_season_memo", {})
        key = int(anime.media_id)
        cached = memo.get(key)
        if cached is not None and time.monotonic() - cached[0] < 120.0:
            season, titles = cached[1], cached[2]
        else:
            season, titles = 0, []
            try:
                context = resolve_episode_numbering(
                    anime, 1, self.config, self.logger, db=self.db, allow_network=False
                )
                chain = tuple(int(value) for value in context.chain or ())
                found = [str(value) for value in context.prequel_titles or () if str(value or "").strip()]
                if context.status == "resolved" and len(chain) >= 2 and chain[-1] == key and found:
                    season, titles = len(chain), found
            except Exception as exc:  # noqa: BLE001 - search goes on without it
                self.logger.debug("SKIP step=release.chain_season media_id=%s error=%r", key, str(exc))
            memo[key] = (time.monotonic(), season, titles)
            if season:
                self.logger.info(
                    "RESULT step=release.chain_season media_id=%s season=%s titles=%r",
                    key,
                    season,
                    titles[:3],
                )
        if not season:
            return anime
        return replace(anime, chain_season=season, chain_titles=list(titles))

    def _release_is_safe_batch_candidate(
        self,
        media_id: int,
        item: NyaaRelease,
    ) -> bool:
        """Use the shared Nyaa identity contract for one-entry batch downloads."""
        anime = self.db.get_anime(int(media_id))
        if anime is not None:
            anime = self._with_chain_season(anime)
        if anime is None:
            # Defensive fallback for transient/legacy callers. Real manager flows
            # normally have the AniList row, but batch safety must still reject
            # obvious movies/multi-season packs when metadata is temporarily absent.
            anime = LibraryAnime(media_id=int(media_id), title="")
        return release_is_safe_batch_candidate(
            anime,
            item,
            negative_titles=self._release_negative_titles_from_graph(anime),
        )

    def _release_has_safe_episode_identity(self, item: NyaaRelease) -> bool:
        """Require structural evidence before a release can be selected as an episode."""
        if item.numbering_status and item.numbering_status != "resolved":
            return False
        blocked_reasons = {
            "wrong-episode",
            "episode-not-specified",
            "episode-pack",
            "ambiguous-episode-mismatch",
        }
        if blocked_reasons.intersection(item.reasons):
            return False

        absolute_alias = self._release_uses_absolute_episode_alias(item)
        season_mismatch = "season-not-specified" in item.reasons or any(
            str(reason).startswith("wrong-season=") for reason in item.reasons
        )
        return not season_mismatch or absolute_alias

    def _release_is_strong_exact_untrusted_candidate(self, item: NyaaRelease) -> bool:
        """Allow an exceptionally clear Nyaa match even if the uploader is not trusted.

        This is deliberately much stricter than the normal score floor. It exists for
        releases such as split-cour/absolute-numbered episodes where a perfectly valid
        uploader may not be present in the user's trusted-groups list.
        """
        reasons = {str(reason) for reason in item.reasons}
        explicit_episode = any(reason.startswith("ep=") for reason in reasons)
        explicit_episode = explicit_episode or self._release_uses_absolute_episode_alias(item)
        score_floor = max(120.0, float(self.config.nyaa.min_release_score) + 50.0)
        seed_floor = max(10, int(self.config.nyaa.min_seeders))
        return bool(
            "exact-title-phrase" in reasons
            and explicit_episode
            and "size-floor-ok" in reasons
            and "very-low-bitrate-size" not in reasons
            and item.score >= score_floor
            and item.seeders >= seed_floor
        )

    DOUBLE_EPISODE_WAIT_SECONDS = 600.0

    @staticmethod
    def _release_double_span(item: NyaaRelease) -> tuple[int, int] | None:
        for reason in item.reasons:
            text = str(reason)
            if text.startswith("double-episode="):
                first, _, last = text.split("=", 1)[1].partition("-")
                if first.isdigit() and last.isdigit():
                    return int(first), int(last)
        return None

    def _release_first_seen(self, item: NyaaRelease) -> float:
        """When the release appeared: its publish time, else when Pudge first saw it."""
        now = time.time()
        published: float | None = None
        raw = str(item.published or "").strip()
        if raw:
            try:
                published = email.utils.parsedate_to_datetime(raw).timestamp()
            except (TypeError, ValueError, IndexError):
                try:
                    stamp = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    published = (
                        stamp if stamp.tzinfo else stamp.replace(tzinfo=datetime.timezone.utc)
                    ).timestamp()
                except ValueError:
                    published = None
        key = (
            "double_episode_seen:"
            + (item.info_hash or hashlib.sha1(item.title.encode()).hexdigest()).casefold()
        )
        try:
            seen = float(self.db.get_state(key, "") or 0.0)
        except (TypeError, ValueError):
            seen = 0.0
        if seen <= 0.0:
            seen = now
            self.db.set_state(key, f"{now:.3f}")
        return min(value for value in (published, seen) if value is not None)

    def _prefer_single_episode_releases(
        self, anime: LibraryAnime, episode: int, eligible: list[NyaaRelease]
    ) -> list[NyaaRelease]:
        """Two episodes in one file only when no single-episode release exists (audit 17А).

        A double is taken 10 minutes after it appeared at the earliest, so a
        group that posts singles shortly after a double premiere wins.
        """
        doubles = [item for item in eligible if self._release_double_span(item) is not None]
        if not doubles:
            return eligible
        singles = [item for item in eligible if self._release_double_span(item) is None]
        if singles:
            self.logger.info(
                "SKIP step=nyaa.auto reason=single_episode_preferred media_id=%s episode=%s doubles=%s",
                anime.media_id,
                episode,
                len(doubles),
            )
            return singles
        now = time.time()
        ready = [
            item
            for item in doubles
            if now - self._release_first_seen(item) >= self.DOUBLE_EPISODE_WAIT_SECONDS
        ]
        if not ready:
            remaining = min(
                self.DOUBLE_EPISODE_WAIT_SECONDS - (now - self._release_first_seen(item)) for item in doubles
            )
            self.logger.info(
                "WAIT step=nyaa.auto reason=double_episode_wait media_id=%s episode=%s remaining=%.0f title=%r",
                anime.media_id,
                episode,
                max(0.0, remaining),
                doubles[0].title,
            )
        return ready

    def automatic_release_candidates(self, releases: Iterable[NyaaRelease]) -> list[NyaaRelease]:
        """Return releases that the normal automatic downloader may actually choose.

        Kept as a shared decision boundary so diagnostics/benchmarks can exercise the
        exact production eligibility contract instead of copying score/trust rules.
        """
        return [item for item in releases if self._release_is_allowed_for_auto(item)]

    def _release_is_allowed_for_auto(self, item: NyaaRelease) -> bool:
        blocked_reasons = {
            "blocked-upscale",
            "english-dub-only",
            "blocked-group",
            "wrong-episode",
            "episode-not-specified",
            "episode-pack",
            "ambiguous-episode-mismatch",
        }
        if blocked_reasons.intersection(item.reasons):
            return False
        # Also for releases that did not come through search_releases.
        if not self.config.nyaa.allow_camrips and is_camrip_release(item.title):
            return False

        absolute_alias = self._release_uses_absolute_episode_alias(item)
        season_mismatch = "season-not-specified" in item.reasons or any(
            str(reason).startswith("wrong-season=") for reason in item.reasons
        )
        # For split-cour shows the torrent's Sxx value often follows a release-site
        # convention rather than AniList's entry season. A confirmed absolute episode
        # alias (e.g. local #3 == absolute #43) is stronger evidence than that Sxx tag.
        if season_mismatch and not absolute_alias:
            return False
        if season_mismatch and absolute_alias:
            self.logger.info(
                "ALLOW step=nyaa.auto reason=absolute_episode_overrides_season title=%r reasons=%r",
                item.title,
                item.reasons,
            )

        if not self._storage_can_accept(item.size_bytes):
            self._storage_rejections = int(getattr(self, "_storage_rejections", 0)) + 1
            status = self._storage_snapshot()
            self.logger.info(
                "SKIP step=nyaa.auto reason=%s size_bytes=%s used_bytes=%s pending_bytes=%s "
                "effective_limit_bytes=%s title=%r",
                "disk_space" if status["space_limited"] else "disk_limit",
                item.size_bytes,
                status["used_bytes"],
                status["pending_bytes"],
                status["effective_limit_bytes"],
                item.title,
            )
            return False

        if item.score < self.config.nyaa.min_release_score:
            return False
        if not self._release_seed_requirement_met(item):
            return False
        configured_trusted = self._release_group_is_configured_trusted(item)
        trusted = self._release_is_trusted(item)
        if self.config.nyaa.only_trusted_groups:
            if not configured_trusted:
                self.logger.info(
                    "SKIP step=nyaa.auto reason=only_trusted_groups group=%r title=%r",
                    item.group,
                    item.title,
                )
            return configured_trusted
        if not self.config.nyaa.auto_require_trusted or trusted:
            return True

        strong_exact = self._release_is_strong_exact_untrusted_candidate(item)
        if strong_exact:
            self.logger.info(
                "ALLOW step=nyaa.auto reason=strong_exact_untrusted score=%.1f seeders=%s group=%r title=%r",
                item.score,
                item.seeders,
                item.group,
                item.title,
            )
        return strong_exact

    def _library_usage_bytes(self) -> int:
        root = self.config.library.root_dir.expanduser()
        if not root.exists():
            return 0
        # An unfinished download's file can already have its full size (sparse,
        # or aria2 after the tail piece) while its remaining bytes count as
        # pending too: count what it really wrote instead.
        unfinished: dict[Path, int] = {}
        downloads = getattr(getattr(self, "db", None), "downloads", None)
        for item in downloads() if callable(downloads) else ():
            if self._download_is_complete(item):
                continue
            path = self._download_content_path(item)
            if path is not None:
                unfinished[path] = unfinished.get(path, 0) + _download_written_bytes(item)
        roots = tuple(unfinished)
        total = 0
        for path in root.rglob("*"):
            try:
                if path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS:
                    if roots and self._path_within(path, roots):
                        continue
                    total += path.stat().st_size
            except OSError:
                continue
        try:
            resolved_root = root.resolve()
        except OSError:
            return total
        for path, written in unfinished.items():
            if path == resolved_root or resolved_root in path.parents:
                total += written
        return total

    STORAGE_USAGE_TTL_SECONDS = 60.0
    STORAGE_ADMISSION_TTL_SECONDS = 30 * 60.0

    def invalidate_storage_snapshot(self) -> None:
        """The next storage check walks the library again (after adding or deleting files)."""
        self._storage_usage_cache = None

    def _storage_usage(self) -> tuple[int, int, set[str]]:
        """Library video bytes, bytes still to download and hashes with a known size.

        Cached briefly: one automatic pass checks every candidate release, and
        walking the whole library for each of them made a pass walk it
        hundreds of times.
        """
        root = str(self.config.library.root_dir.expanduser())
        now = time.monotonic()
        cached = getattr(self, "_storage_usage_cache", None)
        if cached is not None and cached[0] == root and now - cached[1] < self.STORAGE_USAGE_TTL_SECONDS:
            return cached[2], cached[3], cached[4]
        used = self._library_usage_bytes()
        downloading = 0
        known: set[str] = set()
        downloads = getattr(getattr(self, "db", None), "downloads", None)
        for item in downloads() if callable(downloads) else ():
            remaining = _download_remaining_bytes(item)
            if remaining is not None:
                known.add(str(item.torrent_hash or "").casefold())
                downloading += remaining
        self._storage_usage_cache = (root, now, used, downloading, known)
        return used, downloading, known

    def _note_storage_admission(self, info_hash: str, size_bytes: int) -> None:
        """Count a just-added download before its client reports a size."""
        admissions = getattr(self, "_storage_admissions", None)
        if admissions is None:
            self._storage_admissions = admissions = {}
        size = int(size_bytes or 0)
        key = str(info_hash or "").casefold() or f"unknown:{time.monotonic()}"
        admissions[key] = (size if size > 0 else disk_space.UNKNOWN_DOWNLOAD_BYTES, time.monotonic())

    def _admitted_bytes(self, known: set[str]) -> int:
        admissions = getattr(self, "_storage_admissions", None) or {}
        now = time.monotonic()
        total = 0
        for key, (size, at) in list(admissions.items()):
            if now - at > self.STORAGE_ADMISSION_TTL_SECONDS:
                admissions.pop(key, None)
            elif key not in known:
                total += size
        return total

    def _storage_snapshot(self) -> dict[str, Any]:
        """Effective limit = min(configured limit, used + free space - reserve)."""
        used, downloading, known = self._storage_usage()
        pending = downloading + self._admitted_bytes(known)
        library = self.config.library
        enabled = bool(library.disk_limit_enabled)
        limit = int(max(0.0, library.disk_limit_gb) * 1024**3)
        configured = limit if enabled and limit > 0 else None
        usage = disk_space.volume_usage(library.root_dir)
        total, free = usage if usage is not None else (0, 0)
        reserve = (
            disk_space.free_space_reserve(total, getattr(library, "disk_free_reserve_gb", 0.0))
            if usage is not None
            else 0
        )
        space_limit = max(0, used + free - reserve) if usage is not None else None
        bounds = [value for value in (configured, space_limit) if value is not None]
        effective = min(bounds) if bounds else 0
        space_limited = space_limit is not None and (configured is None or space_limit <= configured)
        paused = bool(bounds) and used + pending + disk_space.UNKNOWN_DOWNLOAD_BYTES > effective
        return {
            "enabled": enabled,
            "used_bytes": used,
            "limit_bytes": limit,
            "used_gb": round(used / 1024**3, 2),
            "limit_gb": round(limit / 1024**3, 2),
            "over_limit": bool(enabled and bounds and used > effective),
            "limited": bool(bounds),
            "effective_limit_bytes": effective,
            "effective_limit_gb": round(effective / 1024**3, 2),
            "space_limited": space_limited,
            "total_bytes": total,
            "free_bytes": free,
            "free_gb": round(free / 1024**3, 1),
            "reserve_bytes": reserve,
            "reserve_gb": round(reserve / 1024**3, 1),
            "pending_bytes": pending,
            # What the pause notice shows: free space left once unfinished downloads
            # complete, and the library size including them.
            "available_gb": round(max(0, free - pending) / 1024**3, 1),
            "committed_gb": round((used + pending) / 1024**3, 1),
            "downloads_paused": paused,
            "paused_reason": ("free_space" if space_limited else "limit") if paused else "",
        }

    def storage_status(self) -> dict[str, Any]:
        status = self._storage_snapshot()
        cache = 0
        try:
            cache = CacheRegistry(self.db, self.config.paths.cache_dir).usage_bytes()
        except (AttributeError, OSError, sqlite3.Error):
            cache = 0
        status["cache_bytes"] = cache
        status["cache_gb"] = round(cache / 1024**3, 2)
        return status

    def _storage_can_accept(self, size_bytes: int) -> bool:
        status = self._storage_snapshot()
        if not status["limited"]:
            return True
        # An unknown size must still leave room for a typical episode.
        needed = int(size_bytes) if int(size_bytes or 0) > 0 else disk_space.UNKNOWN_DOWNLOAD_BYTES
        return int(status["used_bytes"]) + int(status["pending_bytes"]) + needed <= int(
            status["effective_limit_bytes"]
        )

    def _ensure_download_space(self, size_bytes: int) -> None:
        """Refuse a download that would eat into the free-space reserve of the library volume."""
        library = self.config.library
        usage = disk_space.volume_usage(library.root_dir)
        if usage is None:
            return  # volume not readable: the client reports its own errors
        total, free = usage
        reserve = disk_space.free_space_reserve(total, getattr(library, "disk_free_reserve_gb", 0.0))
        _used, downloading, known = self._storage_usage()
        pending = downloading + self._admitted_bytes(known)
        needed = int(size_bytes) if int(size_bytes or 0) > 0 else disk_space.UNKNOWN_DOWNLOAD_BYTES
        if free - pending - needed >= reserve:
            return
        self.logger.warning(
            "SKIP step=storage.download reason=free_space_reserve free_bytes=%s pending_bytes=%s "
            "size_bytes=%s reserve_bytes=%s",
            free,
            pending,
            size_bytes,
            reserve,
        )
        # "139.5 GB free" alone read as plenty: unfinished downloads take their share first.
        raise NotEnoughDiskSpaceError(
            err(
                "manager.notEnoughDiskSpace",
                size=f"{needed / 1024**3:.1f}",
                free=f"{free / 1024**3:.1f}",
                pending=f"{pending / 1024**3:.1f}",
                reserve=f"{reserve / 1024**3:.1f}",
            )
        )

    def _log_storage_pause(self) -> None:
        status = self._storage_snapshot()
        reason = "disk_space" if status["space_limited"] else "disk_limit"
        self.logger.warning(
            "WAIT step=nyaa.auto reason=%s used_bytes=%s pending_bytes=%s free_bytes=%s reserve_bytes=%s "
            "limit_bytes=%s effective_limit_bytes=%s",
            reason,
            status["used_bytes"],
            status["pending_bytes"],
            status["free_bytes"],
            status["reserve_bytes"],
            status["limit_bytes"],
            status["effective_limit_bytes"],
        )
        if status["space_limited"]:
            self.log(
                f"Automatic downloads paused: {status['available_gb']} GB free on the download disk "
                f"(after unfinished downloads), Pudge keeps {status['reserve_gb']} GB free"
            )
        else:
            self.log(
                f"Automatic downloads paused: Pudge storage limit reached "
                f"({status['committed_gb']} / {status['limit_gb']} GB)"
            )

    @_reusing_db_connection
    def enforce_disk_limit(self) -> int:
        """Delete the oldest watched, managed single episodes until under cap.

        Unwatched episodes and batch torrents are deliberately protected. If
        they alone exceed the cap, automatic downloads are paused instead of
        destructively guessing what the user wants to keep.
        """
        status = self.storage_status()
        if not status["over_limit"]:
            return 0
        used_bytes = int(status["used_bytes"])
        # The free-space reserve can be stricter than the configured limit.
        limit_bytes = int(status.get("effective_limit_bytes", status["limit_bytes"]))
        if status.get("space_limited"):
            self.logger.warning(
                "EVENT storage.free_space_low free_bytes=%s reserve_bytes=%s used_bytes=%s",
                status.get("free_bytes"),
                status.get("reserve_bytes"),
                used_bytes,
            )

        downloads = {item.torrent_hash.lower(): item for item in self.db.downloads()}
        # The personal schedules are read once for the pass, not twice per episode.
        retained = self.db.personal_release_retained_items()
        candidates = []
        for item in self.db.episodes():
            if item.state != "watched" or not item.video_path.is_file():
                continue
            if not self._inside_library_root(item.video_path):
                # Only files in the library count toward the limit (_library_usage_bytes):
                # a watched file in a watched folder or on another drive went every pass,
                # early and without bringing the library under the limit.
                continue
            if (
                item.media_id is not None
                and item.episode is not None
                and (int(item.media_id), int(item.episode)) in retained
            ):
                self.logger.info(
                    "SKIP step=storage.limit_delete reason=personal_schedule_retention media_id=%s episode=%s video=%r",
                    item.media_id,
                    item.episode,
                    str(item.video_path),
                )
                continue
            candidates.append(item)
        candidates.sort(key=lambda item: (item.watched_at or 0.0, str(item.video_path)))
        deleted = 0
        for item in candidates:
            if used_bytes <= limit_bytes:
                break
            try:
                item_size = item.video_path.stat().st_size
            except OSError:
                item_size = 0
            torrent_hash = item.torrent_hash.lower().strip()
            if self._identity_repair_protected(torrent_hash, item.video_path):
                self.logger.info(
                    "SKIP step=storage.limit_delete reason=identity_repair_protection video=%r",
                    str(item.video_path),
                )
                continue
            if self.config.agent.delete_only_managed_files and not torrent_hash:
                continue
            download = downloads.get(torrent_hash) if torrent_hash else None
            if download is not None and download.is_batch:
                continue
            try:
                if torrent_hash:
                    if not self.downloads_enabled():
                        continue
                    if self.db.episode_count_for_torrent(torrent_hash) != 1:
                        continue
                    # Same as cleanup(): remove the torrent from the client that
                    # owns it, then the file itself. Only qbt_client() was asked
                    # before, and aria2 forgets completed tasks, so the file
                    # often stayed on disk while its row (and watched mark) was
                    # dropped; a later scan imported it again as unwatched.
                    removed = self._delete_owned_torrent(torrent_hash, download, deleting=[item.video_path])
                    item.video_path.unlink(missing_ok=True)
                    if item.subtitle_path:
                        item.subtitle_path.unlink(missing_ok=True)
                    if removed:
                        self.db.delete_torrent_records(torrent_hash)
                    else:
                        # The batch stays in the client: its row would keep the deleted
                        # episode "on disk" (an active torrent protects it) for good.
                        self.db.delete_episode_record(item.video_path)
                else:
                    item.video_path.unlink(missing_ok=True)
                    if item.subtitle_path:
                        item.subtitle_path.unlink(missing_ok=True)
                    self.db.delete_episode_record(item.video_path)
            except (OSError, QBittorrentError, ManagerError) as exc:
                # The next oldest watched file is tried: one locked file or a torrent
                # client refusing used to end the whole pass with the disk still full.
                self.logger.warning(
                    "FAIL step=storage.limit_delete video=%r torrent_hash=%s error=%r",
                    str(item.video_path),
                    torrent_hash,
                    str(exc)[:300],
                )
                self.log(f"Disk limit: could not delete an old watched file: {exc}")
                continue
            self._forget_video_state([item.video_path])
            deleted += 1
            used_bytes = max(0, used_bytes - item_size)
            self.logger.info(
                "DONE step=storage.limit_delete video=%r torrent_hash=%s watched_at=%s",
                str(item.video_path),
                torrent_hash,
                item.watched_at,
            )

        if deleted:
            self.invalidate_storage_snapshot()
        remaining = self.storage_status()
        if remaining["over_limit"] and used_bytes > limit_bytes:
            self.logger.warning(
                "EVENT storage.limit_unresolved used_bytes=%s limit_bytes=%s reason=no_safe_watched_candidates",
                remaining["used_bytes"],
                remaining.get("effective_limit_bytes", remaining["limit_bytes"]),
            )
        return deleted

    def enforce_derived_caches(self, *, force: bool = False) -> int:
        """Bound derived subtitle/alignment caches and iPhone copies (hourly)."""
        iphone_root = Path(self.config.library.database_path).expanduser().parent / "iphone"
        try:
            result = enforce_derived_caches(
                self.db, self.config.paths.cache_dir, iphone_root=iphone_root, force=force
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.logger.warning("FAIL step=cache.derived error=%r", str(exc)[:200])
            return 0
        if result.get("skipped"):
            return 0
        removed = int(result["removed"]) + int(result["purged_files"])
        self.logger.info(
            "RESULT step=cache.derived removed=%s purged_files=%s removed_mb=%.1f remaining_mb=%.1f",
            result["removed"],
            result["purged_files"],
            int(result["removed_bytes"]) / 1024**2,
            int(result["remaining_bytes"]) / 1024**2,
        )
        return removed

    def maintain_database_storage(self) -> int:
        """Compress the Jiten caches in short batches, then hourly give free pages back."""
        try:
            result = maintain_database_storage(
                Path(self.config.library.database_path).expanduser(), logger=self.logger
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.logger.warning("FAIL step=database.storage error=%r", str(exc)[:200])
            return 0
        return int(result.get("compacted") or 0)

    def purge_episode_derived_files(self, video_path: Path) -> int:
        """Delete the derived cache files of a deleted episode (call after its row is gone)."""
        try:
            removed = purge_episode_derived_files(self.db, self.config.paths.cache_dir, video_path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.logger.warning(
                "FAIL step=cache.episode_purge video=%r error=%r", str(video_path), str(exc)[:200]
            )
            return 0
        if removed:
            self.logger.info("DONE step=cache.episode_purge video=%r files=%s", str(video_path), removed)
        return removed

    @_reusing_db_connection
    def auto_upgrade_downloaded(self, *, force: bool = False, limit: int | None = None) -> int:
        """Check managed, unwatched episodes only after missing episodes were handled.

        An upgrade is downloaded alongside the current file. The current torrent is
        removed only after the replacement finishes and has usable Japanese subtitles.
        """
        if not (self.config.nyaa.enabled and self.downloads_enabled()):
            return 0
        if not force and not self.config.nyaa.auto_upgrade_downloaded:
            return 0

        self._expire_stale_upgrades()
        checks_left = max(0, int(limit) if limit is not None else self.config.nyaa.max_upgrade_checks_per_run)
        if checks_left == 0:
            return 0
        scheduled = 0
        now = time.time()
        cooldown = max(0.0, self.config.nyaa.upgrade_check_hours) * 3600

        for anime in self.db.anime_list(("CURRENT",)):
            if checks_left <= 0:
                break
            candidates = [
                item
                for item in self.db.episodes(anime.media_id)
                if item.episode is not None and item.torrent_hash and item.state != "watched"
            ]
            candidates.sort(key=lambda item: int(item.episode or 0), reverse=True)

            for current in candidates:
                if checks_left <= 0:
                    break
                episode = int(current.episode or 0)
                if episode <= 0 or self.db.has_pending_upgrade(anime.media_id, episode):
                    continue
                download = self.db.download_by_hash(current.torrent_hash)
                if download is None or download.is_batch:
                    continue
                if self.db.episode_count_for_torrent(current.torrent_hash) != 1:
                    continue

                state_key = f"upgrade_checked:{anime.media_id}:{episode}"
                try:
                    last_checked = float(self.db.get_state(state_key, "0") or 0)
                except ValueError:
                    last_checked = 0
                if not force and cooldown and now - last_checked < cooldown:
                    continue

                checks_left -= 1
                try:
                    releases = self.search_releases(
                        anime.media_id, episode=episode, batch=False, automatic=True
                    )
                except NoReleaseSourceError:
                    # Not "checked": the episode is checked once a source is added.
                    self._log_no_release_source("nyaa.upgrade")
                    return scheduled
                except NyaaError as exc:
                    self.log(f"{anime.title} #{episode}: upgrade check failed: {exc}")
                    self.db.set_state(state_key, str(time.time()))
                    continue
                except Exception as exc:  # noqa: BLE001 - one episode never ends the pass
                    self.logger.warning(
                        "FAIL step=nyaa.upgrade media_id=%s episode=%s error=%r",
                        anime.media_id,
                        episode,
                        f"{type(exc).__name__}: {exc}"[:300],
                    )
                    self.db.set_state(state_key, str(time.time()))
                    continue
                self.db.set_state(state_key, str(time.time()))

                current_hash = current.torrent_hash.lower()
                best = next(
                    (
                        item
                        for item in releases
                        if self._release_is_allowed_for_auto(item)
                        and item.info_hash
                        and item.info_hash.lower() != current_hash
                    ),
                    None,
                )
                if best is None:
                    continue
                listed = next(
                    (item for item in releases if item.info_hash and item.info_hash.lower() == current_hash),
                    None,
                )
                current_title = (
                    listed.title if listed is not None else str(download.name or current.video_path.name)
                )
                try:
                    current_size = (
                        listed.size_bytes
                        if listed is not None and listed.size_bytes > 0
                        else current.video_path.stat().st_size
                    )
                except OSError:
                    current_size = 0
                if same_web_encode(current_title, current_size, best.title, best.size_bytes):
                    self.logger.info(
                        "SKIP step=nyaa.upgrade media_id=%s episode=%s reason=same_web_encode current=%r best=%r",
                        anime.media_id,
                        episode,
                        current_title,
                        best.title,
                    )
                    continue
                current_score, basis = self._upgrade_current_score(
                    anime.media_id, episode, current_hash, releases, best
                )
                if current_score is None:
                    self.logger.info(
                        "SKIP step=nyaa.upgrade media_id=%s episode=%s reason=%s hash=%s best_context=%s",
                        anime.media_id,
                        episode,
                        basis,
                        current_hash,
                        getattr(best, "score_context", ""),
                    )
                    continue
                gain = best.score - current_score
                if gain < self.config.nyaa.upgrade_min_score_gain:
                    self.logger.info(
                        "SKIP step=nyaa.upgrade media_id=%s episode=%s reason=gain_too_small current_score=%.1f best_score=%.1f gain=%.1f required=%.1f",
                        anime.media_id,
                        episode,
                        current_score,
                        best.score,
                        gain,
                        self.config.nyaa.upgrade_min_score_gain,
                    )
                    continue
                try:
                    added = self.add_release(anime.media_id, best, episode=episode, batch=False, upgrade=True)
                except (QBittorrentError, ManagerError) as exc:
                    self.log(f"{anime.title} #{episode}: upgrade add failed: {exc}")
                    continue
                except Exception as exc:  # noqa: BLE001 - one episode never ends the pass
                    # The title folder cannot be made (the library disk away), a locked
                    # database: as in auto_search_current, the rest of the pass still runs
                    # (upgrades finished, cleanup, the disk limit).
                    self.logger.warning(
                        "FAIL step=nyaa.upgrade_add media_id=%s episode=%s error=%r",
                        anime.media_id,
                        episode,
                        f"{type(exc).__name__}: {exc}"[:300],
                    )
                    self.log(f"{anime.title} #{episode}: upgrade add failed: {exc}")
                    continue
                if not added:
                    # Nothing new was added (the same torrent is already listed):
                    # a job here would block this episode without a download.
                    self.logger.info(
                        "SKIP step=nyaa.upgrade media_id=%s episode=%s reason=not_added new_hash=%s",
                        anime.media_id,
                        episode,
                        best.info_hash.lower(),
                    )
                    continue
                self.db.record_upgrade(
                    new_info_hash=best.info_hash,
                    old_torrent_hash=current_hash,
                    media_id=anime.media_id,
                    episode=episode,
                    old_score=current_score,
                    new_score=best.score,
                )
                self.logger.info(
                    "SCHEDULE step=nyaa.upgrade media_id=%s episode=%s old_score=%.1f new_score=%.1f gain=%.1f old_hash=%s new_hash=%s basis=%s context=%s",
                    anime.media_id,
                    episode,
                    current_score,
                    best.score,
                    gain,
                    current_hash,
                    best.info_hash.lower(),
                    basis,
                    getattr(best, "score_context", ""),
                )
                self.log(
                    f"{anime.title} #{episode}: scheduled quality upgrade "
                    f"({current_score:.1f} → {best.score:.1f})"
                )
                scheduled += 1
        return scheduled

    def _upgrade_current_score(
        self,
        media_id: int,
        episode: int,
        current_hash: str,
        releases: list[NyaaRelease],
        best: NyaaRelease,
    ) -> tuple[float | None, str]:
        """Score of the current release in the *same* context as ``best``.

        1. The current torrent is among the fresh results → its fresh score.
        2. Its stored score was computed with the same formula and inputs.
        Otherwise the scores are not comparable: (None, diagnostic reason).
        """
        same = next(
            (item for item in releases if item.info_hash and item.info_hash.lower() == current_hash),
            None,
        )
        if same is not None:
            return float(same.score), "fresh"
        lookup = getattr(self.db, "release_score_record", None)
        record = lookup(media_id, episode, current_hash) if callable(lookup) else None
        if record is None:
            return None, "current_score_unknown"
        score, formula, context = record
        fresh_context = str(getattr(best, "score_context", "") or "")
        if context != fresh_context:
            # Also covers legacy rows without a context vs. stamped results.
            return None, "score_context_mismatch" if context else "score_context_missing"
        if formula and formula != str(getattr(best, "score_formula", "") or formula):
            return None, "score_formula_mismatch"
        return float(score), "stored"

    def _expire_stale_upgrades(self) -> int:
        expired = self.db.expire_upgrades(UPGRADE_JOB_TTL_SECONDS)
        for job in expired:
            self.logger.info(
                "EXPIRE step=nyaa.upgrade media_id=%s episode=%s new_hash=%s old_hash=%s age_hours=%.1f",
                job["media_id"],
                job["episode"],
                job["new_info_hash"],
                job["old_torrent_hash"],
                (time.time() - float(job["created_at"] or 0)) / 3600.0,
            )
        return len(expired)

    @staticmethod
    def _upgrade_replacement_ready(item: LibraryEpisode | None) -> bool:
        """The new copy is Ready: its video and Japanese text subtitles are usable."""
        if item is None or item.state != "ready" or not item.video_path.is_file():
            return False
        if item.embedded_subtitle_id is not None:
            return True
        return bool(item.subtitle_path is not None and item.subtitle_path.is_file())

    def _upgrade_may_replace(
        self, new_episode: LibraryEpisode | None, old_episode: LibraryEpisode | None
    ) -> bool:
        """The replacement is Ready, or the current copy is not Ready either.

        Kusuriya no Hitorigoto S3 #3: neither copy had Japanese subtitles yet (Jimaku had
        none), so the old one was "kept until the upgrade has subtitles" and both stayed on
        disk. A current copy without subtitles loses nothing to a complete better release.
        """
        if self._upgrade_replacement_ready(new_episode):
            return True
        if new_episode is None or not new_episode.video_path.is_file():
            return False
        if new_episode.state not in {"waiting_subtitles", "local"}:
            return False
        if self.unfinished_library_video(new_episode.video_path):
            return False
        download = self.db.download_by_hash(new_episode.torrent_hash) if new_episode.torrent_hash else None
        if download is None or not download_complete(download):
            return False
        return old_episode is None or old_episode.state not in {"ready", "watched"}

    def finalize_ready_upgrades(self) -> int:
        """Replace old managed files only after the new episode is fully ready."""
        jobs = self.db.pending_upgrades()
        if not jobs or not self.downloads_enabled():
            return 0
        replaced = 0
        clients = self._replacement_clients()
        try:
            for job in jobs:
                new_hash = str(job["new_info_hash"] or "").lower()
                old_hash = str(job["old_torrent_hash"] or "").lower()
                media_id = int(job["media_id"])
                episode = int(job["episode"])
                new_episode = self.db.episode_for_torrent(media_id, episode, new_hash)
                old_episode = self.db.episode_for_torrent(media_id, episode, old_hash)
                if not self._upgrade_may_replace(new_episode, old_episode):
                    continue
                old_video = (
                    old_episode.video_path
                    if old_episode is not None and old_episode.video_path != new_episode.video_path
                    else None
                )
                try:
                    # A batch's old copy: only that file goes, never the batch's other
                    # episodes with the torrent (Ansatsu Kyoushitsu).
                    removed = self._retire_torrent_file(
                        clients, old_hash, old_video, self.db.download_by_hash(old_hash)
                    )
                except (QBittorrentError, ManagerError) as exc:
                    self.log(
                        f"Upgrade #{episode}: replacement ready, but old torrent could not be removed: {exc}"
                    )
                    continue

                try:
                    if old_episode is not None:
                        if (
                            old_episode.subtitle_path
                            and old_episode.subtitle_path != new_episode.subtitle_path
                        ):
                            old_episode.subtitle_path.unlink(missing_ok=True)
                        if old_video is not None:
                            old_video.unlink(missing_ok=True)
                            if not removed:
                                self.db.delete_episode_record(old_video)
                except OSError as exc:
                    # A locked or read-only old file: this job waits for the next pass. Raised,
                    # it ended every maintenance pass here (no cleanup, no disk limit).
                    self.logger.warning(
                        "FAIL step=nyaa.upgrade_replace media_id=%s episode=%s video=%r error=%r",
                        media_id,
                        episode,
                        str(old_video or ""),
                        str(exc)[:300],
                    )
                    self.log(f"Upgrade #{episode}: the old file could not be deleted: {exc}")
                    continue
                if removed:
                    self.db.delete_torrent_records(old_hash)
                self.db.complete_upgrade(new_hash)
                self.logger.info(
                    "DONE step=nyaa.upgrade_replace media_id=%s episode=%s old_hash=%s new_hash=%s old_score=%.1f new_score=%.1f old_torrent_removed=%s",
                    media_id,
                    episode,
                    old_hash,
                    new_hash,
                    float(job["old_score"]),
                    float(job["new_score"]),
                    removed,
                )
                replaced += 1
        finally:
            for client in clients:
                client.close()
        return replaced

    @_reusing_db_connection
    def reconcile_duplicate_versions(self) -> int:
        """Remove stale single-episode torrents when a clear better copy exists.

        Normal upgrade jobs are authoritative. For legacy/orphaned duplicates,
        release-history scores are used only when they identify one unique
        winner. Batch torrents and ambiguous duplicates are never deleted.
        """
        if not self.downloads_enabled():
            return 0

        groups: dict[tuple[int, int], list[LibraryEpisode]] = {}
        for item in self.db.episodes():
            if item.media_id is None or item.episode is None or not item.torrent_hash:
                continue
            groups.setdefault((item.media_id, item.episode), []).append(item)

        pending_by_key = {
            (int(job["media_id"]), int(job["episode"])): str(job["new_info_hash"] or "").lower()
            for job in self.db.pending_upgrades()
        }

        plans: list[tuple[LibraryEpisode, list[LibraryEpisode]]] = []
        for key, items in groups.items():
            hashes = {item.torrent_hash.lower() for item in items}
            if len(hashes) < 2:
                continue

            winner: LibraryEpisode | None = None
            pending_hash = pending_by_key.get(key, "")
            if pending_hash:
                winner = next(
                    (item for item in items if item.torrent_hash.lower() == pending_hash),
                    None,
                )
                current = next((item for item in items if item is not winner), None)
                if winner is not None and not self._upgrade_may_replace(winner, current):
                    # Same rule as finalize_ready_upgrades: keep the current Ready
                    # copy until the replacement has Japanese subtitles.
                    continue

            if winner is None:
                scored: list[tuple[float, LibraryEpisode]] = []
                for item in items:
                    score = self.db.release_score(key[0], key[1], item.torrent_hash)
                    if score is not None:
                        scored.append((score, item))
                if len(scored) != len(items):
                    continue
                scored.sort(key=lambda pair: pair[0], reverse=True)
                if len(scored) > 1 and scored[0][0] == scored[1][0]:
                    continue
                winner = scored[0][1]

            if winner.state not in {
                "ready",
                "local",
                "watched",
                "waiting_subtitles",
            }:
                continue

            losers = [item for item in items if item.torrent_hash.lower() != winner.torrent_hash.lower()]
            if winner.state not in {"ready", "watched"}:
                # finalize_ready_upgrades' rule for every copy: one without subtitles yet never
                # replaces a Ready copy whose subtitles play. An upgrade given up after a week
                # keeps both copies, and the better-scored one then deleted the Ready episode.
                losers = [item for item in losers if not self._upgrade_replacement_ready(item)]
            safe_losers: list[LibraryEpisode] = []
            for loser in losers:
                if self.db.episode_count_for_torrent(loser.torrent_hash) != 1:
                    continue
                download = self.db.download_by_hash(loser.torrent_hash)
                if download is not None and download.is_batch:
                    continue
                safe_losers.append(loser)
            if safe_losers:
                plans.append((winner, safe_losers))

        if not plans:
            return 0

        removed = 0
        clients = self._replacement_clients()
        try:
            for winner, losers in plans:
                for loser in losers:
                    old_hash = loser.torrent_hash.lower()
                    old_video = loser.video_path if loser.video_path != winner.video_path else None
                    try:
                        # A loser that is part of a batch the rows under-count: only its
                        # file goes (deselected first), the torrent and its other files stay.
                        torrent_removed = self._retire_torrent_file(
                            clients, old_hash, old_video, self.db.download_by_hash(old_hash)
                        )
                    except (QBittorrentError, ManagerError) as exc:
                        self.log(f"Duplicate #{winner.episode}: old torrent could not be removed: {exc}")
                        continue

                    try:
                        if loser.subtitle_path and loser.subtitle_path != winner.subtitle_path:
                            loser.subtitle_path.unlink(missing_ok=True)
                        if old_video is not None:
                            old_video.unlink(missing_ok=True)
                    except OSError as exc:
                        # As in finalize_ready_upgrades: retried next pass, the pass goes on.
                        self.logger.warning(
                            "FAIL step=nyaa.duplicate_replace media_id=%s episode=%s video=%r error=%r",
                            winner.media_id,
                            winner.episode,
                            str(old_video or ""),
                            str(exc)[:300],
                        )
                        self.log(f"Duplicate #{winner.episode}: the old file could not be deleted: {exc}")
                        continue
                    if torrent_removed:
                        self.db.delete_torrent_records(old_hash)
                    elif old_video is not None:
                        self.db.delete_episode_record(old_video)
                    self.logger.info(
                        "DONE step=nyaa.duplicate_replace media_id=%s episode=%s old_hash=%s new_hash=%s old_torrent_removed=%s",
                        winner.media_id,
                        winner.episode,
                        old_hash,
                        winner.torrent_hash.lower(),
                        torrent_removed,
                    )
                    removed += 1
                self.db.complete_upgrade(winner.torrent_hash)
        finally:
            for client in clients:
                client.close()
        return removed

    def _remove_missing_episode_rows(self, active_hashes: set[str]) -> int:
        """Forget library rows whose video and owning torrent were both removed.

        qBittorrent deletion is often performed outside pudge. The downloads
        table was pruned correctly, but the episode row survived and continued to
        block ``auto_search_current`` through ``has_episode``. Only rows whose
        video is actually absent are removed; an active torrent still protects its
        row while qBittorrent is moving/rechecking data.
        """
        normalized_hashes = {value.strip().casefold() for value in active_hashes if value}
        missing: list[LibraryEpisode] = []
        for episode in self.db.episodes():
            if episode.video_path.is_file():
                continue
            torrent_hash = episode.torrent_hash.strip().casefold()
            if torrent_hash and torrent_hash in normalized_hashes:
                continue
            missing.append(episode)
        forgettable = self._forgettable_missing_videos([episode.video_path for episode in missing])
        removed = 0
        for episode in missing:
            if str(episode.video_path) not in forgettable:
                continue
            self.db.delete_episode_record(episode.video_path)
            self.logger.info(
                "REPAIR step=library.missing_video media_id=%s episode=%s video=%r torrent_hash=%s",
                episode.media_id,
                episode.episode,
                str(episode.video_path),
                episode.torrent_hash.strip().casefold(),
            )
            removed += 1
        return removed

    _MISSING_VIDEO_STATE = "missing_video_since"

    def _missing_video_forget_seconds(self) -> float:
        """How long a missing file on a reachable drive must stay missing (and in every
        check meanwhile) before Pudge forgets its library row: the "Delete hours after
        watching" interval of Settings (agent.delete_after_watched_hours)."""
        return max(0.0, float(self.config.agent.delete_after_watched_hours or 0.0)) * 3600.0

    def _missing_video_unreachable(self, video: Path, volumes: dict[Path, bool], roots: list[Any]) -> str:
        """Why a missing ``video`` may only be out of reach now ("" when it is not).

        An unplugged external drive leaves /Volumes/<name> unmounted (or gone),
        and a library or watched folder that is not there means its drive or
        share is away. Neither proves that the file was deleted. ``volumes``
        caches mount checks; ``roots`` holds (root, resolved root, present).
        """
        path = video.expanduser()
        parts = path.parts
        if len(parts) > 2 and parts[0] == os.sep and parts[1] == "Volumes":
            volume = Path(parts[0], parts[1], parts[2])
            if volume not in volumes:
                volumes[volume] = os.path.ismount(volume) or (volume.is_symlink() and volume.is_dir())
            if not volumes[volume]:
                return "volume_unmounted"
        for root, resolved_root, present in roots:
            if not present and (path.is_relative_to(root) or path.is_relative_to(resolved_root)):
                return "root_unavailable"
        return ""

    def _missing_video_roots(self) -> list[Any]:
        """(root, resolved root, present) of the library and watched folders, for
        ``_missing_video_unreachable``."""
        roots: list[Any] = []
        for configured in (self.config.library.root_dir, *self._watched_media_folders()):
            root = configured.expanduser()
            try:
                resolved_root = root.resolve()
            except (OSError, RuntimeError):
                resolved_root = root
            roots.append((root, resolved_root, root.is_dir()))
        return roots

    def _forgettable_missing_videos(self, videos: list[Path], *, immediate: bool = False) -> set[str]:
        """The missing videos whose library rows may be forgotten now.

        A row holds the watched mark, resume position, subtitle choice and
        schedules. A file that is only out of reach keeps it; any other file
        must stay missing for the "Delete hours after watching" interval (a file
        seen again starts over; 0 forgets it at once). ``immediate`` (an explicit
        repair) skips the wait only.
        """
        now = time.time()
        wait = self._missing_video_forget_seconds()
        try:
            stored = json.loads(self.db.get_state(self._MISSING_VIDEO_STATE, "") or "{}")
        except (TypeError, ValueError):
            stored = {}
        since_by_path: dict[str, float] = {}
        for key, value in (stored if isinstance(stored, dict) else {}).items():
            try:
                since = float(value)
            except (TypeError, ValueError):
                continue
            if now - since < max(30 * 86400.0, wait + 86400.0) and not Path(str(key)).is_file():
                since_by_path[str(key)] = since
        forget: set[str] = set()
        unreachable: dict[str, int] = {}
        volumes: dict[Path, bool] = {}
        roots: list[Any] = []
        # When the first of the videos still waiting may be forgotten (the
        # library scan must not be skipped as "unchanged" past it).
        self._missing_video_due_at: float | None = None
        if videos:
            roots = self._missing_video_roots()
        for video in videos:
            key = str(video)
            reason = self._missing_video_unreachable(video, volumes, roots)
            if reason:
                unreachable[reason] = unreachable.get(reason, 0) + 1
                continue
            since = since_by_path.get(key)
            missing_for = 0.0 if since is None else now - since
            if immediate or missing_for >= wait:
                forget.add(key)
                since_by_path.pop(key, None)
            else:
                if since is None:
                    since = since_by_path[key] = now
                    self.logger.info(
                        "DEFER step=library.missing_video video=%r forget_after_seconds=%.0f",
                        key,
                        wait,
                    )
                due = since + wait
                if self._missing_video_due_at is None or due < self._missing_video_due_at:
                    self._missing_video_due_at = due
        if unreachable != getattr(self, "_last_unreachable_missing_videos", {}):
            self._last_unreachable_missing_videos = dict(unreachable)
            if unreachable:
                self.logger.info("SKIP step=library.missing_video reason=unreachable rows=%s", unreachable)
        encoded = json.dumps(since_by_path, sort_keys=True) if since_by_path else ""
        if encoded != (self.db.get_state(self._MISSING_VIDEO_STATE, "") or ""):
            self.db.set_state(self._MISSING_VIDEO_STATE, encoded)
        return forget

    def _repair_legacy_qbittorrent_paths(self, client, items: list[DownloadItem]) -> int:
        """Repair qBittorrent save paths left behind by the pudge -> Pudge rename."""
        set_location = getattr(client, "set_location", None)
        if not callable(set_location):
            return 0
        recheck = getattr(client, "recheck", None)
        current_root = self.config.library.root_dir.expanduser().resolve()
        repaired = 0
        for item in items:
            if str(item.state or "").casefold() != "missingfiles":
                continue
            raw_save = str(item.save_path or "").strip()
            if not raw_save:
                continue
            old_save = Path(raw_save).expanduser()
            target: Path | None = None
            for legacy_name in LEGACY_APP_NAMES:
                legacy_root = current_root.parent / legacy_name
                try:
                    relative = old_save.relative_to(legacy_root)
                except ValueError:
                    continue
                candidate = current_root / relative
                if candidate.exists():
                    target = candidate.resolve()
                    break
            if target is None:
                continue
            try:
                set_location(item.torrent_hash, target)
                if callable(recheck):
                    recheck(item.torrent_hash)
            except QBittorrentError as exc:
                self.logger.warning(
                    "FAIL step=qbittorrent.legacy_path torrent=%s old=%r new=%r error=%r",
                    item.torrent_hash,
                    str(old_save),
                    str(target),
                    str(exc),
                )
                continue
            repaired += 1
            self.logger.info(
                "REPAIR step=qbittorrent.legacy_path torrent=%s old=%r new=%r recheck=%s",
                item.torrent_hash,
                str(old_save),
                str(target),
                callable(recheck),
            )
        return repaired

    def _discard_watched_empty_aria2_shells(
        self,
        client: Any,
        items: list[DownloadItem],
    ) -> list[DownloadItem]:
        """Forget zero-byte managed aria2 shells for episodes already watched.

        Historical aria2 metadata can outlive both the real task and a video that
        Pudge already deleted after watching.  Those 0% paused rows are not a
        download and should not live forever in Downloads.  Cleanup is deliberately
        narrow: single episode, no local payload, effectively zero bytes/progress,
        inactive state, and AniList progress already covers the episode.
        """
        kept: list[DownloadItem] = []
        inactive_states = {"paused", "stopped", "error", "removed", "missing", "unknown"}
        for item in items:
            raw = item.raw or {}
            if str(raw.get("backend") or "aria2").casefold() != "aria2":
                kept.append(item)
                continue
            episode = item.media_episode if item.media_episode is not None else item.episode
            if episode is not None and getattr(item, "media_episode_end", None) is not None:
                episode = int(item.media_episode_end)  # both episodes of a double must be watched
            try:
                progress = float(item.progress or 0.0)
                total_size = max(0, int(raw.get("total_size") or 0))
                downloaded = max(0, int(raw.get("downloaded") or 0))
            except (TypeError, ValueError):
                kept.append(item)
                continue
            content = self._download_content_path(item)
            payload_exists = bool(content is not None and content.is_file())
            state = str(item.state or "").casefold()
            get_anime = getattr(self.db, "get_anime", None)
            anime = (
                get_anime(int(item.media_id)) if item.media_id is not None and callable(get_anime) else None
            )
            unnumbered = episode is None
            if (
                unnumbered
                and not item.is_batch
                and anime is not None
                and single_unit_title(anime.format, anime.episodes)
            ):
                # A movie's shell has no episode number: it is episode 1 of its title.
                episode = 1
            watched = bool(
                anime is not None
                and watched_by_anilist_progress(
                    episode,
                    anime.progress,
                    total_episodes=anime.episodes,
                    media_format=anime.format,
                )
            )
            note_progress = getattr(self.db, "note_anilist_progress", None)
            if watched and episode is not None and int(item.added_on or 0) > 0 and callable(note_progress):
                # Added after the list already counted the episode (a rewatch): not a leftover.
                reached_at = Database.anilist_progress_reached_at(
                    note_progress(int(item.media_id), int(anime.progress or 0)), int(episode)
                )
                watched = reached_at is None or float(item.added_on) < reached_at
            stale = bool(
                not item.is_batch
                and item.media_id is not None
                and episode is not None
                and progress <= 0.001
                and total_size == 0
                and downloaded == 0
                and not payload_exists
                and state in inactive_states
                and watched
            )
            if not stale:
                kept.append(item)
                continue
            try:
                client.delete(item.torrent_hash, delete_files=False)
            except Exception as exc:
                self.logger.info(
                    "SKIP step=aria2.watched_empty_shell_backend_forget hash=%s error=%r",
                    item.torrent_hash,
                    str(exc),
                )
            self.db.delete_torrent_records(item.torrent_hash)
            try:
                self.download_intents.clear(int(item.media_id), int(episode), False)
                if unnumbered:
                    # A whole-title request (the movie's search ran as a batch).
                    self.download_intents.clear(int(item.media_id), None, True)
            except (AttributeError, TypeError, ValueError):
                pass
            self.logger.info(
                "REPAIR step=aria2.watched_empty_shell hash=%s media_id=%s episode=%s state=%s",
                item.torrent_hash,
                item.media_id,
                episode,
                state,
            )
        return kept

    def _repair_stale_completed_current_download_intents(
        self,
        active_items: list[DownloadItem],
    ) -> int:
        """Restart already-requested current episodes stranded after file repair.

        Older corrupt-orphan repair removed the bad file/torrent row but left its
        logical intent as ``complete``. That exact state has no local episode and
        no active torrent, so it is neither a completed download nor a new request.
        Clear the impossible terminal state once and resume the existing request.
        """
        anime_list = getattr(self.db, "anime_list", None)
        has_episode = getattr(self.db, "has_episode", None)
        intents = getattr(self, "download_intents", None)
        if not (callable(anime_list) and callable(has_episode) and intents is not None):
            return 0
        active_keys = {
            (int(item.media_id), int(item.media_episode if item.media_episode is not None else item.episode))
            for item in active_items
            if item.media_id is not None
            and (item.media_episode if item.media_episode is not None else item.episode) is not None
        }
        repaired = 0
        try:
            current = list(anime_list(("CURRENT",)))
        except Exception:
            return 0
        for anime in current:
            released = anime.released_episodes
            if released is None:
                continue
            start = max(1, int(anime.progress or 0) + 1)
            for episode in range(start, int(released) + 1):
                try:
                    intent = intents.get(int(anime.media_id), int(episode), False)
                except Exception:
                    intent = None
                if not isinstance(intent, dict) or str(intent.get("state") or "").casefold() != "complete":
                    continue
                if (int(anime.media_id), int(episode)) in active_keys:
                    continue
                try:
                    if bool(has_episode(int(anime.media_id), int(episode))):
                        continue
                except Exception:
                    continue
                intents.clear(int(anime.media_id), int(episode), False)
                repaired += 1
                if not self.downloads_enabled():
                    intents.update(
                        int(anime.media_id),
                        int(episode),
                        False,
                        state="waiting",
                        backend=str(intent.get("backend") or "aria2"),
                        detail="Completed download state had no file; waiting for torrents",
                    )
                    self.logger.info(
                        "REPAIR step=download.stale_complete_intent media_id=%s episode=%s action=waiting",
                        anime.media_id,
                        episode,
                    )
                    continue
                try:
                    replacement = self.search_and_add_best(
                        int(anime.media_id),
                        episode=int(episode),
                        batch=False,
                        automatic=True,
                    )
                except Exception as exc:
                    intents.update(
                        int(anime.media_id),
                        int(episode),
                        False,
                        state="waiting",
                        backend=str(intent.get("backend") or "aria2"),
                        detail="Retry after stale completed download failed",
                    )
                    self.logger.warning(
                        "RETRY step=download.stale_complete_intent media_id=%s episode=%s error=%r",
                        anime.media_id,
                        episode,
                        str(exc),
                    )
                    continue
                if replacement is None:
                    intents.update(
                        int(anime.media_id),
                        int(episode),
                        False,
                        state="waiting",
                        backend=str(intent.get("backend") or "aria2"),
                        detail="No replacement release started yet",
                    )
                self.logger.info(
                    "REPAIR step=download.stale_complete_intent media_id=%s episode=%s selected=%r",
                    anime.media_id,
                    episode,
                    replacement.title if replacement is not None else "",
                )
        return repaired

    ARIA2_IDLE_STOP_SECONDS = 10 * 60

    @staticmethod
    def _aria2_idle_stopped(client: Any) -> bool:
        check = getattr(client, "idle_stopped", None)
        try:
            return bool(check()) if callable(check) else False
        except Exception:  # noqa: BLE001 - unknown means "report the error"
            return False

    def _stop_aria2_when_idle(self, client: Any) -> bool:
        """Stop the aria2 sidecar after 10 minutes with nothing to download or seed."""
        is_idle = getattr(client, "is_idle", None)
        stop = getattr(client, "stop_when_idle", None)
        if not callable(is_idle) or not callable(stop):
            return False
        try:
            idle = bool(is_idle())
        except Exception:  # noqa: BLE001 - a failed probe is not idleness
            idle = False
        now = time.monotonic()
        if not idle:
            self._aria2_idle_since = None
            return False
        since = getattr(self, "_aria2_idle_since", None)
        if since is None:
            self._aria2_idle_since = now
            return False
        if now - since < self.ARIA2_IDLE_STOP_SECONDS:
            return False
        self._aria2_idle_since = None
        try:
            stopped = bool(stop())
        except Exception as exc:  # noqa: BLE001 - stays running; retried after the next idle window
            self.logger.warning("SKIP step=aria2.idle_stop error=%r", str(exc))
            return False
        self.logger.info("EVENT aria2.idle_stop stopped=%s idle_seconds=%.0f", stopped, now - since)
        return stopped

    @_reusing_db_connection
    def sync_downloads(self) -> int:
        # The Download Center refresh, the foreground worker and actions all sync:
        # one at a time in this process (the agent is covered by the row age below).
        lock = self.__dict__.setdefault("_sync_downloads_lock", threading.RLock())
        with lock:
            return self._sync_downloads_locked()

    def _sync_downloads_locked(self) -> int:
        self._last_missing_episode_rows = 0
        # Rows written after this (a release just added) are not in the listings below.
        listing_started = time.time()
        completed_paths: list[Path] = []
        if not self.downloads_configured():
            self._last_completed_video_paths = ()
            return 0
        self.recover_acquisition_attempts()
        traffic_enabled = self.downloads_enabled()
        items: list[DownloadItem] = []
        offline_items: list[DownloadItem] = []
        backend_errors: list[str] = []
        backend_snapshots: dict[str, set[str]] = {}
        configured_backends: set[str] = set()
        for backend, client in self.torrent_clients():
            configured_backends.add(str(backend))
            try:
                if backend == "qbittorrent":
                    accepted_categories = {
                        self.config.qbittorrent.category,
                        *LEGACY_APP_SLUGS,
                    }
                    all_items = client.torrents(category="")
                    repaired_paths = self._repair_legacy_qbittorrent_paths(client, all_items)
                    if repaired_paths:
                        all_items = client.torrents(category="")
                    backend_items = [
                        item
                        for item in all_items
                        if str(item.raw.get("category") or "") in accepted_categories
                    ]
                    # A product rename should not strand already-running torrents in
                    # the previous qBittorrent category or leave the legacy app tag.
                    ensure_category = getattr(client, "ensure_category", None)
                    set_metadata = getattr(client, "set_metadata", None)
                    if callable(ensure_category) and callable(set_metadata):
                        migrated_category = False
                        legacy_names = {value.casefold() for value in LEGACY_APP_SLUGS}
                        for item in backend_items:
                            old_category = str(item.raw.get("category") or "").strip()
                            raw_tags = self._torrent_tags(item)
                            legacy_brand_tags = {tag for tag in raw_tags if tag.casefold() in legacy_names}
                            has_current_brand = any(tag.casefold() == APP_SLUG.casefold() for tag in raw_tags)
                            needs_category = old_category in LEGACY_APP_SLUGS
                            needs_tags = bool(legacy_brand_tags or not has_current_brand)
                            if not needs_category and not needs_tags:
                                continue
                            if not migrated_category:
                                ensure_category(
                                    self.config.qbittorrent.category,
                                    self.config.library.root_dir,
                                )
                                migrated_category = True
                            desired_tags = sorted(
                                (raw_tags - legacy_brand_tags) | {APP_SLUG},
                                key=str.casefold,
                            )
                            set_metadata(
                                item.torrent_hash,
                                category=self.config.qbittorrent.category,
                                tags=desired_tags,
                            )
                            item.raw["category"] = self.config.qbittorrent.category
                            item.raw["_tag_set"] = desired_tags
                else:
                    backend_items = client.torrents(category=self.config.qbittorrent.category)
                    if traffic_enabled:
                        discarded_recovery = self._discard_completed_aria2_recovery_tasks(
                            client, backend_items
                        )
                        if discarded_recovery:
                            backend_items = client.torrents(category=self.config.qbittorrent.category)
                        recovered_aria2 = self._recover_stalled_aria2_downloads(client, backend_items)
                        if recovered_aria2:
                            # Recovery registers a fresh hash-check task. Refresh now
                            # so DB/UI and the subsequent auto-search see that task
                            # instead of the stale error row and never add a duplicate.
                            backend_items = client.torrents(category=self.config.qbittorrent.category)
                if not traffic_enabled:
                    pause = getattr(client, "pause", None)
                    if callable(pause):
                        for item in backend_items:
                            state = str(item.state or "").casefold()
                            # Already-paused/removed rows need no enforcement.
                            if state in {"paused", "stopped", "removed", "missing"}:
                                continue
                            # aria2 keeps historical metadata for completed/error
                            # tasks whose GIDs no longer exist. Re-pausing those
                            # expired GIDs produces HTTP 400 on every foreground
                            # poll. qBittorrent is intentionally *not* covered by
                            # this optimization: a completed qBittorrent item can
                            # still be seeding and Torrent Off must pause it.
                            raw = item.raw or {}
                            if backend == "aria2" and (
                                state in {"complete", "completed", "error"}
                                or bool(raw.get("orphaned_metadata"))
                                or bool(raw.get("recoverable_missing_control"))
                            ):
                                continue
                            try:
                                pause(item.torrent_hash)
                            except Exception as exc:
                                self.logger.warning(
                                    "FAIL step=torrent.off_enforce backend=%s hash=%s error=%r",
                                    backend,
                                    item.torrent_hash,
                                    str(exc),
                                )
                for item in backend_items:
                    item.raw["backend"] = backend
                if backend == "aria2":
                    backend_items = self._discard_watched_empty_aria2_shells(client, backend_items)
                    if traffic_enabled:
                        self._stop_aria2_when_idle(client)
                backend_snapshots[str(backend)] = {
                    str(item.torrent_hash).strip().casefold()
                    for item in backend_items
                    if str(item.torrent_hash or "").strip()
                }
                items.extend(backend_items)
            except Exception as exc:
                if backend == "aria2":
                    # Torrents off or stopped while idle: a task that finished
                    # just before aria2 stopped is gone from its session, its
                    # row stuck short of 100% (Rakugo Shinjuu S2, 2026-10-18).
                    offline_items.extend(self._aria2_completed_while_stopped(client))
                if backend == "aria2" and self._aria2_idle_stopped(client):
                    # Deliberately stopped while idle: its rows are kept as
                    # unknown, and the next add or resume starts it again.
                    continue
                backend_errors.append(f"{backend}: {exc}")
                self.logger.warning("FAIL step=torrent.sync_backend backend=%s error=%r", backend, str(exc))
            finally:
                client.close()
        for item in offline_items:
            self._resolve_download_media(item)
            self.db.upsert_download(item)
            registered = self._register_completed_download(item, completed_paths=completed_paths)
            self.logger.info(
                "REPAIR step=aria2.completed_while_stopped hash=%s media_id=%s registered=%s path=%r",
                item.torrent_hash,
                item.media_id,
                registered,
                item.content_path,
            )
        if backend_errors and not items:
            self._last_completed_video_paths = tuple(dict.fromkeys(completed_paths))
            self._remember_completed_for_intro(self._last_completed_video_paths)
            raise ManagerError("; ".join(backend_errors))

        # A torrent may exist in both clients. Keep a single database/UI row,
        # preferring the copy that has made more progress (then the active one).
        deduplicated: dict[str, DownloadItem] = {}
        for item in items:
            key = item.torrent_hash.casefold()
            previous = deduplicated.get(key)
            item.raw["_backends"] = [str(item.raw.get("backend") or "")]
            rank = (float(item.progress), int(item.raw.get("download_speed") or item.raw.get("dlspeed") or 0))
            previous_rank = (
                (-1.0, -1)
                if previous is None
                else (
                    float(previous.progress),
                    int(previous.raw.get("download_speed") or previous.raw.get("dlspeed") or 0),
                )
            )
            if previous is None or rank > previous_rank:
                if previous is not None:
                    item.raw["_backends"] = list(
                        dict.fromkeys(
                            [
                                *previous.raw.get("_backends", []),
                                *item.raw.get("_backends", []),
                            ]
                        )
                    )
                deduplicated[key] = item
            elif previous is not None:
                previous.raw["_backends"] = list(
                    dict.fromkeys(
                        [
                            *previous.raw.get("_backends", []),
                            *item.raw.get("_backends", []),
                        ]
                    )
                )
        items = list(deduplicated.values())
        completed = 0
        active_hashes = {
            str(item.torrent_hash).strip().casefold()
            for item in items
            if str(item.torrent_hash or "").strip()
        }
        successful_backends = set(backend_snapshots)
        failed_backends = configured_backends - successful_backends
        # Reconciliation is backend-scoped.  A failed backend is unknown, not an
        # authoritative empty snapshot: retain rows last observed there so a
        # healthy backend cannot prune another backend's downloads.
        stored_downloads = self.db.downloads()
        for stored in stored_downloads:
            torrent_hash = str(stored.torrent_hash or "").strip().casefold()
            if not torrent_hash:
                continue
            raw = stored.raw if isinstance(stored.raw, dict) else {}
            raw_backends = raw.get("_backends")
            recorded_backends = {
                str(value).strip()
                for value in (raw_backends if isinstance(raw_backends, list) else [])
                if str(value or "").strip()
            }
            fallback_backend = str(raw.get("backend") or "").strip()
            if fallback_backend:
                recorded_backends.add(fallback_backend)

            if failed_backends and (not recorded_backends or bool(recorded_backends & failed_backends)):
                active_hashes.add(torrent_hash)
                continue

            # aria2 may forget a completed task after its control files disappear.
            # Keep local completed metadata so a later Library scan can still map
            # absolute release numbering (for example release 19 -> season episode 7).
            if self._download_is_complete(stored):
                local_path = self._download_content_path(stored)
                if local_path is not None and local_path.is_file():
                    active_hashes.add(torrent_hash)

        active_hashes |= self.db.download_hashes_written_since(listing_started)
        self._last_missing_episode_rows = self._remove_missing_episode_rows(active_hashes)
        self.db.prune_downloads(active_hashes, written_before=listing_started)
        finished: list[DownloadItem] = []
        for item in items:
            stored = self.db.download_by_hash(item.torrent_hash) if item.torrent_hash else None
            if self._stale_aria2_row_shadowed_by_completed_local(item, stored):
                self.logger.info(
                    "REPAIR step=aria2.stale_zero_progress hash=%s "
                    "reason=completed_local_file_is_authoritative",
                    item.torrent_hash,
                )
                item = stored
            else:
                recovered = self._recover_stale_aria2_zero_shell(item)
                if recovered is not None:
                    self.logger.info(
                        "REPAIR step=aria2.stale_zero_local_file hash=%s path=%r "
                        "reason=exact_file_without_control_sidecar",
                        item.torrent_hash,
                        recovered.content_path,
                    )
                    item = recovered
            self._resolve_download_media(item)
            score_tag = item.raw.get("_release_score_tag")
            if (
                score_tag is not None
                and item.media_id is not None
                and self.db.release_metadata_by_hash(item.torrent_hash) is None
            ):
                try:
                    self.db.record_release(
                        item.torrent_hash,
                        int(item.media_id),
                        int(item.media_episode) if item.media_episode is not None else None,
                        item.name,
                        float(score_tag),
                        release_episode=parsed_release_episode(item.name),
                    )
                except (TypeError, ValueError):
                    pass
            self.db.upsert_download(item)
            if not self._download_is_complete(item):
                continue
            finished.append(item)
        # Which titles the completed downloads claim each file for, read once after every
        # row above is stored: each registration read every download row again and walked
        # every completed download's files (n² per sync, on every 15-60 s poll).
        claims = self._completed_download_claims() if finished else {}
        for item in finished:
            completed += self._register_completed_download(
                item,
                completed_paths=completed_paths,
                claims=claims,
            )
        self._last_completed_video_paths = tuple(dict.fromkeys(completed_paths))
        self._remember_completed_for_intro(self._last_completed_video_paths)
        self._repair_stale_completed_current_download_intents(items)
        self.db.set_state("downloads_synced_at", str(time.time()))
        return completed

    def _aria2_completed_while_stopped(self, client: Any) -> list[DownloadItem]:
        """Managed aria2 torrents finished on disk while aria2 cannot be asked."""
        recover = getattr(client, "completed_on_disk", None)
        if not callable(recover):
            return []
        try:
            done = {
                str(row.torrent_hash).strip().casefold()
                for row in self.db.downloads()
                if str(row.torrent_hash or "").strip() and self._download_is_complete(row)
            }
            return list(recover(skip_hashes=done))
        except Exception as exc:  # noqa: BLE001 - a local check never fails the sync
            self.logger.info("SKIP step=aria2.completed_while_stopped reason=%r", str(exc))
            return []

    def _discard_completed_aria2_recovery_tasks(self, client: Any, items: list[DownloadItem]) -> int:
        """Drop metadata-only recovery tasks after the exact torrent is already local."""

        delete = getattr(client, "delete", None)
        if not callable(delete):
            return 0
        local_hashes = {
            str(episode.torrent_hash or "").strip().casefold()
            for episode in self.db.episodes()
            if str(episode.torrent_hash or "").strip() and episode.video_path.is_file()
        }
        if not local_hashes:
            return 0

        removed = 0
        for item in items:
            raw = item.raw or {}
            torrent_hash = str(item.torrent_hash or "").strip().casefold()
            if not torrent_hash or torrent_hash not in local_hashes:
                continue
            if not int(raw.get("recovery_started_at") or 0):
                continue
            state = str(item.state or "").casefold()
            total = max(0, int(raw.get("total_size") or 0))
            downloaded = max(0, int(raw.get("downloaded") or 0))
            if state not in {"active", "waiting", "paused"} or total > 0 or downloaded > 0:
                continue
            try:
                # The exact torrent already produced a Library file. This is only
                # the metadata-only magnet/hash-check shell created after a lost
                # .aria2 control file, so never touch the downloaded video.
                delete(item.torrent_hash, delete_files=False)
            except QBittorrentError as exc:
                self.logger.warning(
                    "RETRY step=aria2.recovery_shell_cleanup hash=%s error=%r",
                    item.torrent_hash,
                    str(exc),
                )
                continue
            self.db.delete_state(f"aria2_stall:{torrent_hash}")
            self.logger.info(
                "REPAIR step=aria2.recovery_shell_cleanup hash=%s "
                "reason=exact_library_file_exists delete_files=false",
                item.torrent_hash,
            )
            removed += 1
        return removed

    def _recover_stalled_aria2_downloads(
        self, client: Any, items: list[DownloadItem], *, now: float | None = None
    ) -> int:
        """Reannounce an aria2 task that stopped moving while still active."""

        reconnect = getattr(client, "reconnect", None)
        if not callable(reconnect):
            return 0
        current = float(now if now is not None else time.time())
        recovered = 0
        for item in items:
            raw = item.raw or {}
            key = f"aria2_stall:{item.torrent_hash.casefold()}"
            state = str(item.state or "").casefold()
            downloaded = max(0, int(raw.get("downloaded") or 0))
            total = max(0, int(raw.get("total_size") or 0))
            speed = max(0, int(raw.get("download_speed") or 0))
            connections = max(0, int(raw.get("num_connections") or 0))
            recoverable_missing_control = bool(state == "error" and raw.get("recoverable_missing_control"))
            stale_zero_local_with_control = False
            if state == "paused" and total == 0 and downloaded == 0 and not item.is_batch:
                content_value = str(item.content_path or "").strip()
                content = Path(content_value).expanduser() if content_value else None
                if content is not None and content.is_file():
                    wanted_release = item.release_episode
                    if wanted_release is None:
                        wanted_release = parsed_release_episode(str(item.name or ""))
                    actual_release = parsed_release_episode(content.name)
                    identity_ok = bool(
                        wanted_release is None
                        or (actual_release is not None and int(actual_release) == int(wanted_release))
                    )
                    stale_zero_local_with_control = bool(
                        identity_ok and Path(str(content) + ".aria2").exists()
                    )

            stalled_candidate = (
                state in {"active", "waiting"}
                and not bool(raw.get("verifying"))
                and ((total > 0 and 0 <= downloaded < total) or (total == 0 and downloaded == 0))
                and speed == 0
                and connections == 0
            )
            if (
                not stalled_candidate
                and not recoverable_missing_control
                and not stale_zero_local_with_control
            ):
                if self.db.get_state(key, ""):
                    # Deleted, not emptied: an empty row per torrent ever stalled stayed forever.
                    self.db.delete_state(key)
                continue
            try:
                marker = json.loads(self.db.get_state(key, "") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                marker = {}
            previous = max(0, int(marker.get("downloaded") or 0))
            if previous != downloaded:
                marker = {
                    "downloaded": downloaded,
                    "stalled_since": current,
                    "last_reconnect": float(marker.get("last_reconnect") or 0),
                }
            else:
                marker.setdefault("stalled_since", current)
                marker.setdefault("last_reconnect", 0.0)
            if total == 0 and int(item.added_on or 0) > 0:
                marker["stalled_since"] = min(
                    float(marker["stalled_since"] or current),
                    float(item.added_on),
                )
            stalled_for = current - float(marker["stalled_since"] or current)
            since_reconnect = current - float(marker["last_reconnect"] or 0)

            if stale_zero_local_with_control:
                # A legacy aria2 task can be paused at 0/0 while the exact video
                # and its control sidecar are still present. Do not promote it to
                # complete blindly; resume/reconnect aria2 immediately so it can
                # reconstruct metadata and verify or continue the existing data.
                last_local_attempt = float(marker.get("last_local_zero_attempt") or 0)
                if current - last_local_attempt >= 2 * 60:
                    marker["last_local_zero_attempt"] = current
                    try:
                        if reconnect(item.torrent_hash):
                            recovered += 1
                            marker["last_reconnect"] = current
                            marker["stalled_since"] = current
                            self.logger.info(
                                "REPAIR step=aria2.stale_zero_local_control hash=%s mode=resume_verify",
                                item.torrent_hash,
                            )
                        else:
                            self.logger.info(
                                "RETRY step=aria2.stale_zero_local_control hash=%s "
                                "reason=no_recovery_source delay_s=120",
                                item.torrent_hash,
                            )
                    except QBittorrentError as exc:
                        self.logger.warning(
                            "RETRY step=aria2.stale_zero_local_control hash=%s delay_s=120 error=%r",
                            item.torrent_hash,
                            str(exc),
                        )
                self.db.set_state(key, json.dumps(marker, separators=(",", ":")))
                continue

            if recoverable_missing_control:
                # Missing-control recovery may need to fetch torrent metadata.
                # Retry failed attempts quickly, but never write the success
                # cooldown before reconnect() actually registered a hash-check.
                # Older builds did exactly that and left errorCode=13 stuck for
                # 30 minutes after a failed Nyaa .torrent fetch.
                last_control_attempt = float(marker.get("last_control_attempt") or 0)
                if current - last_control_attempt >= 2 * 60:
                    marker["last_control_attempt"] = current
                    try:
                        if reconnect(item.torrent_hash):
                            recovered += 1
                            marker["last_control_recovery"] = current
                            marker["stalled_since"] = current
                            self.logger.info(
                                "REPAIR step=aria2.missing_control hash=%s mode=hash_check total=%s",
                                item.torrent_hash,
                                total,
                            )
                        else:
                            self.logger.info(
                                "RETRY step=aria2.missing_control hash=%s "
                                "reason=no_recovery_source delay_s=120",
                                item.torrent_hash,
                            )
                    except QBittorrentError as exc:
                        self.logger.warning(
                            "RETRY step=aria2.missing_control hash=%s delay_s=120 error=%r",
                            item.torrent_hash,
                            str(exc),
                        )
                self.db.set_state(key, json.dumps(marker, separators=(",", ":")))
                continue

            # aria2 retries trackers and peers itself. A pause/unpause cycle is
            # reserved for a genuinely long stall: doing it every few minutes
            # interrupts piece verification and tracker announce backoff.
            if stalled_for >= 15 * 60 and since_reconnect >= 30 * 60:
                try:
                    if reconnect(item.torrent_hash):
                        recovered += 1
                        marker["last_reconnect"] = current
                        marker["stalled_since"] = current
                        self.logger.info(
                            "REPAIR step=aria2.reconnect hash=%s progress=%.3f downloaded=%s listed_seeders=%s",
                            item.torrent_hash,
                            float(item.progress or 0.0),
                            downloaded,
                            int(raw.get("listed_seeders") or 0),
                        )
                except QBittorrentError as exc:
                    self.logger.warning(
                        "RETRY step=aria2.reconnect hash=%s error=%r",
                        item.torrent_hash,
                        str(exc),
                    )
            self.db.set_state(key, json.dumps(marker, separators=(",", ":")))
        return recovered

    def _complete_download_intent(self, item: DownloadItem) -> bool:
        if item.media_id is None:
            return False
        target_episode = None
        if not item.is_batch:
            target_episode = item.media_episode if item.media_episode is not None else item.episode
        completed = self.download_intents.complete_if_present(
            int(item.media_id),
            int(target_episode) if target_episode is not None else None,
            bool(item.is_batch),
            expected_hash=str(item.torrent_hash or ""),
        )
        if completed:
            self.logger.info(
                "REPAIR step=download.intent_complete media_id=%s episode=%s batch=%s hash=%s",
                item.media_id,
                target_episode,
                bool(item.is_batch),
                item.torrent_hash,
            )
        return completed

    def _register_completed_download(
        self,
        item,
        *,
        completed_paths: list[Path] | None = None,
        claims: dict[Path, set[int]] | None = None,
    ) -> int:
        """``claims``: ``_completed_download_claims()`` of the current download rows when the
        caller has it already (read here otherwise)."""
        discovered_files = self._completed_download_video_files(item)
        files = [path for path in discovered_files if not self._library_path_is_ignored(path)]
        if files:
            # A file aria2 has not finished is no episode yet (preparation would drop it).
            roots = self._library_scan_roots()
            known_finished = self._finished_torrent_check()
            own_hash = str(getattr(item, "torrent_hash", "") or "").strip().casefold()

            def finished(info_hash: str) -> bool:
                # The download registered here is complete, whether or not its row says so yet
                # (its seeding control file stays while the pack seeds).
                if own_hash and str(info_hash or "").casefold() == own_hash:
                    return True
                return known_finished(info_hash)

            unfinished = [
                path for path in files if self.unfinished_library_video(path, roots=roots, finished=finished)
            ]
            if unfinished and len(unfinished) == len(files):
                # Still being written (another torrent's control file): registered by a later
                # pass, not completed now as if every file were excluded by an ignore marker.
                self.logger.info(
                    "WAIT step=download.completed_registration reason=unfinished_video hash=%s files=%s",
                    own_hash,
                    len(unfinished),
                )
                return 0
            files = [path for path in files if path not in unfinished]
        if discovered_files and not files:
            self.logger.info(
                "SKIP step=download.completed_registration reason=library_ignore_marker hash=%s files=%s",
                getattr(item, "torrent_hash", ""),
                len(discovered_files),
            )
            self._complete_download_intent(item)
            return 0
        count = 0
        new_paths: list[Path] = []
        if claims is None:
            claims = self._completed_download_claims()
        anime = self.db.get_anime(item.media_id) if item.media_id else None
        for path in files:
            resolved = path.resolve()
            if self._completed_download_owner_conflict(item, resolved, claims):
                continue
            if self._extra_file_kept(resolved) or self._special_set_aside(resolved):
                continue  # an extra file the person kept (or a special set aside): unchanged
            identity = parse_anime_filename(path)
            release_number = identity.episode or item.release_episode
            media_number = self._download_file_media_episode(anime, item, release_number, path)
            extra_kind = self._extra_file_kind(anime, path.name) if item.is_batch else ""
            if extra_kind:
                # "Show - OVA 02" in a TV batch is not episode 2 of the series.
                release_number = media_number = None
            existing = self.db.episode_by_path(resolved)
            other_hash = str(existing.torrent_hash or "").strip() if existing is not None else ""
            if (
                item.is_batch
                and other_hash
                and other_hash.casefold() != str(item.torrent_hash or "").casefold()
                and self.db.download_by_hash(other_hash) is not None
            ):
                # A batch whose content path is the series folder lists every
                # video in it. A file another torrent still in the client
                # downloaded was re-pointed to the batch, and cleanup of the
                # batch later deleted it.
                self.logger.info(
                    "SKIP step=download.completed_registration reason=owned_by_other_torrent hash=%s owner=%s video=%r",
                    item.torrent_hash,
                    other_hash,
                    str(resolved),
                )
                continue
            expected_episode = int(media_number) if media_number is not None else None
            expected_media = int(item.media_id) if item.media_id is not None else None
            existing_episode = (
                int(existing.media_episode if existing.media_episode is not None else existing.episode)
                if existing is not None
                and (existing.media_episode is not None or existing.episode is not None)
                else None
            )
            identity_matches = bool(
                existing is not None
                and existing.media_id == expected_media
                and existing_episode == expected_episode
                and (release_number is None or existing.release_episode == int(release_number))
                and (
                    not item.torrent_hash
                    or str(existing.torrent_hash or "").casefold() == item.torrent_hash.casefold()
                )
            )
            # Ready/Watched is terminal only for the *same managed episode*. A
            # path row left behind with an old/absolute identity must not block
            # the authoritative torrent metadata from repairing season-local
            # numbering (for example Seihantai S2 release 19 -> episode 7).
            if existing is not None and existing.state in {"ready", "watched"} and identity_matches:
                continue
            is_new_completion = not identity_matches
            subtitle_source, subtitle_path, embedded_subtitle_id = self._completed_subtitle_details(path)
            library_episode = LibraryEpisode(
                media_id=item.media_id,
                title=anime.title if anime else identity.title,
                episode=media_number,
                media_episode=media_number,
                release_episode=release_number,
                video_path=resolved,
                subtitle_path=subtitle_path,
                embedded_subtitle_id=embedded_subtitle_id,
                subtitle_origin=(
                    "bitmap"
                    if subtitle_source in {"external_bitmap", "embedded_bitmap"}
                    else subtitle_source
                    if subtitle_source in {"external", "embedded"}
                    else ""
                ),
                state=(
                    "waiting_subtitles"
                    if extra_kind  # not Ready under the series before the person decides
                    else "ready"
                    if subtitle_source in {"external", "embedded"}
                    else "waiting_text_subtitles"
                    if subtitle_source in {"external_bitmap", "embedded_bitmap"}
                    else "waiting_subtitles"
                ),
                torrent_hash=item.torrent_hash,
            )
            self.db.upsert_episode(
                library_episode,
                downloaded_at=float(item.completed_on or time.time()),
            )
            self._record_media_identity(
                library_episode,
                source="completed_download",
                provenance={"subtitle_source": subtitle_source},
            )
            if extra_kind:
                # Home asks about it right away (Action required), not after the queue.
                self.db.ensure_subtitle_job(resolved, item.media_id, None)
                self.db.mark_subtitle_job_needs_action(
                    resolved, err("jobs.extraDownloadFile", kind=extra_kind), self.EXTRA_FILE_REVIEW
                )
                if is_new_completion:
                    count += 1
                    new_paths.append(resolved)
                continue
            if subtitle_source in {"none", "external_bitmap", "embedded_bitmap"}:
                if is_new_completion:
                    self.db.queue_subtitle_job(
                        resolved,
                        item.media_id,
                        media_number,
                        priority=100,
                        error=err("jobs.newDownload"),
                    )
                else:
                    self.db.ensure_subtitle_job(resolved, item.media_id, media_number)
            if subtitle_source in {"external", "embedded"}:
                self._notify_ready_episode(
                    video=resolved,
                    media_id=(int(item.media_id) if item.media_id is not None else None),
                    episode=(int(media_number) if media_number is not None else None),
                )
            if is_new_completion:
                count += 1
                new_paths.append(resolved)
        if files:
            self._complete_download_intent(item)
        if completed_paths is not None:
            completed_paths.extend(new_paths)
        return count

    def _completed_subtitle_details(self, video: Path) -> tuple[str, Path | None, int | None]:
        """Japanese subtitles of a completed download's file: (source, file, embedded track).

        Every download check registers each completed download again, and a file still
        waiting for subtitles ran ffprobe on every check (twice with an embedded track). A
        subtitle file beside the video is looked for each time (a folder listing); the
        embedded-track probe is reused while the video's size and modification time stay.
        """
        sidecar = sidecar_subtitle(video)
        if sidecar is not None:
            kind = "external_bitmap" if sidecar.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS else "external"
            return kind, sidecar.resolve(), None
        try:
            return self._cached_embedded_subtitle_details(video)
        except Exception:  # noqa: BLE001 - a failed probe is never cached: checked as before
            pass
        source, path = japanese_subtitle_source(
            video,
            ffprobe=self.config.tools.ffprobe,
            ffmpeg=self.config.tools.ffmpeg,
        )
        embedded_id = None
        if source in {"embedded", "embedded_bitmap"}:
            _source, _path, embedded_id = japanese_subtitle_details(
                video,
                ffprobe=self.config.tools.ffprobe,
                ffmpeg=self.config.tools.ffmpeg,
            )
        return source, path, embedded_id

    @property
    def last_completed_video_paths(self) -> tuple[Path, ...]:
        return self._last_completed_video_paths

    @staticmethod
    def _prepared_bitmap_retry_path(item: LibraryEpisode | None) -> Path | None:
        """Reuse the synchronized bitmap stored by an earlier prepare attempt.

        Once a PGS/SUP candidate has been discovered and synchronized, retrying
        Jimaku discovery and archive extraction is pure duplicate work.  The
        background CLI can feed this exact path straight into OCR.
        """
        if item is None or item.state != "waiting_text_subtitles":
            return None
        path = item.subtitle_path
        if path is None or path.suffix.casefold() not in IMAGE_SUBTITLE_EXTENSIONS | {".pgs"}:
            return None
        try:
            return path if path.is_file() and path.stat().st_size > 0 else None
        except OSError:
            return None

    @staticmethod
    def _subtitle_upgrade_state_key(video: Path) -> str:
        return "subtitle_upgrade:" + hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()

    @staticmethod
    def _subtitle_upgrade_checked_key(video: Path) -> str:
        return "subtitle_upgrade_checked:" + hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()

    @staticmethod
    def _subtitle_upgrade_attempts_key(video: Path) -> str:
        return "subtitle_upgrade_checks:" + hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()

    def _is_legacy_ocr_prepared_subtitle(self, item: LibraryEpisode) -> bool:
        """Recognize OCR results created before subtitle provenance was stored in DB."""
        if str(item.subtitle_origin or "").casefold() == "ocr":
            return True
        subtitle = item.subtitle_path
        if subtitle is None or subtitle.suffix.casefold() != ".srt":
            return False
        latest = self.db.latest_selected_subtitle(item.video_path)
        if latest is not None:
            details = latest.get("details") if isinstance(latest.get("details"), dict) else {}
            if bool(details.get("generated_by_ocr")):
                return True
            if str(latest.get("source") or "").casefold() == "ocr":
                return True
            candidate_path = Path(str(latest.get("candidate_path") or ""))
            if candidate_path.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS | {".pgs"}:
                return True

        # v0.6.41 and older cleaned OCR output through playback-srt and did not
        # record its origin. Reconstruct the deterministic cleaned filename from
        # cached OCR SRTs so upgrades can still invalidate those rows safely.
        try:
            resolved_subtitle = subtitle.expanduser().resolve()
            ocr_root = (self.config.paths.cache_dir / "ocr").expanduser().resolve()
            playback_root = (self.config.paths.cache_dir / "playback-srt").expanduser().resolve()
            if ocr_root == resolved_subtitle.parent:
                return True
            if playback_root == resolved_subtitle.parent:
                # OCR existed before explicit subtitle_origin provenance.  The
                # playback cleaner changed generations over time, so rebuild the
                # deterministic filename for every generation that shipped with
                # OCR instead of assuming only the current v12 name.
                for generation in ("v10", "v11", "v12", "v13", "v14", "v15", "v16"):
                    if not resolved_subtitle.name.startswith(f"{generation}-"):
                        continue
                    for ocr_srt in ocr_root.glob("*.srt"):
                        try:
                            stat = ocr_srt.stat()
                        except OSError:
                            continue
                        digest = hashlib.sha1(
                            f"{ocr_srt.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:playback-srt-{generation}".encode()
                        ).hexdigest()[:20]
                        if resolved_subtitle.name == f"{generation}-{digest}.srt":
                            return True

                # Old final-pipeline manifests are an additional source of
                # provenance and may survive even if the raw OCR cache was
                # pruned.  Their schema/key changed over releases, therefore
                # inspect the tiny JSON payloads directly rather than loading
                # only the current schema.
                final_root = (self.config.paths.cache_dir / "final-pipeline").expanduser()
                for manifest in final_root.glob("*.json"):
                    try:
                        payload = json.loads(manifest.read_text(encoding="utf-8"))
                    except (OSError, ValueError, json.JSONDecodeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if str(payload.get("source") or "").casefold() != "ocr":
                        continue
                    try:
                        cached_subtitle = Path(str(payload.get("subtitle") or "")).expanduser().resolve()
                    except (OSError, RuntimeError):
                        continue
                    if cached_subtitle == resolved_subtitle:
                        return True
        except OSError:
            pass
        return False

    def invalidate_disabled_ocr_subtitles(self) -> list[Path]:
        """Immediately withdraw OCR-generated text when OCR is disabled.

        Bitmap sources remain available for Library-only playback, while a
        high-priority text-subtitle job is queued for the normal resolver.
        """
        if self.config.matching.ocr_image_subtitles:
            return []
        changed: list[Path] = []
        for item in self.db.episodes():
            if item.state == "watched" or not item.video_path.is_file():
                continue
            if not self._is_legacy_ocr_prepared_subtitle(item):
                continue
            video = item.video_path.expanduser().resolve()
            invalidate_final_pipeline_result(video, self.config)
            try:
                source, bitmap_path, bitmap_sid = japanese_subtitle_details(
                    video,
                    ffprobe=self.config.tools.ffprobe,
                    ffmpeg=self.config.tools.ffmpeg,
                )
            except Exception as exc:
                self.logger.warning(
                    "FALLBACK step=subtitle.ocr_disable_probe video=%r error=%r",
                    str(video),
                    str(exc),
                )
                source, bitmap_path, bitmap_sid = "none", None, None

            if source in {"external", "embedded"}:
                self.db.set_subtitle_ready(
                    video,
                    bitmap_path,
                    bitmap_sid,
                    origin=source,
                )
                self.logger.info(
                    "RESULT step=subtitle.ocr_disable video=%r state=ready replacement=%s",
                    str(video),
                    source,
                )
                changed.append(video)
                continue
            if source in {"external_bitmap", "embedded_bitmap"}:
                self.db.set_waiting_text_subtitles(video, bitmap_path, bitmap_sid)
            else:
                self.db.clear_subtitle_selection(video)
            self.db.queue_subtitle_job(
                video,
                item.media_id,
                item.episode,
                priority=240,
                error=err("manager.ocrDisabledWaitingText"),
            )
            self.logger.info(
                "REQUEUE step=subtitle.ocr_disabled video=%r media_id=%s episode=%s source=%s",
                str(video),
                item.media_id,
                item.episode,
                source,
            )
            changed.append(video)
        return changed

    def _subtitle_upgrade_marker_keys(self) -> set[str] | None:
        """State keys of the upgrade checks and rebuilds that hold a request, in one query
        (None when they could not be read: the caller asks per episode as before).

        Every maintenance pass resolved each library video and read its marker row, twice,
        while usually no check was waiting at all."""
        try:
            with self.db.connect() as conn:
                rows = conn.execute(
                    f"SELECT key,value FROM state WHERE {STATE_PREFIX_SQL}",
                    state_prefix_bounds("subtitle_upgrade:"),
                ).fetchall()
        except (AttributeError, TypeError, sqlite3.Error):
            return None
        return {str(row["key"]) for row in rows if str(row["value"] or "").strip()}

    @_reusing_db_connection
    def _active_subtitle_upgrade_paths(self) -> set[str]:
        paths: set[str] = set()
        markers = self._subtitle_upgrade_marker_keys()
        if markers is not None and not markers:
            return paths
        for item in self.db.episodes():
            key = self._subtitle_upgrade_state_key(item.video_path)
            if (key in markers) if markers is not None else self.db.get_state(key, "").strip():
                paths.add(str(item.video_path))
        return paths

    @_reusing_db_connection
    def _restore_interrupted_subtitle_upgrades(self) -> int:
        """Restore Ready selections hidden by legacy upgrade/repair races.

        v39 and older reused ordinary subtitle jobs for upgrade checks. A later
        repair pass could therefore demote the episode while the upgrade marker
        still preserved the previous valid selection. Rehydrate that selection
        and keep/recreate the upgrade job so checking remains transparent.
        """
        restored = 0
        markers = self._subtitle_upgrade_marker_keys()
        if markers is not None and not markers:
            return restored
        jobs = {str(row["video_path"]): row for row in self.db.subtitle_jobs()}
        for item in self.db.episodes():
            key = self._subtitle_upgrade_state_key(item.video_path)
            if markers is not None and key not in markers:
                continue
            raw = self.db.get_state(key, "").strip()
            if not raw:
                continue
            job = jobs.get(str(item.video_path))
            job_state = str(job["state"] or "") if job is not None else ""
            if job_state == "processing":
                # A worker is running this check: resetting the job to pending
                # took it away from that worker.
                continue
            try:
                request = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(request, dict):
                continue
            previous_value = str(request.get("previous_subtitle_path") or "").strip()
            previous_path = Path(previous_value) if previous_value else None
            previous_sid_raw = request.get("previous_embedded_sid")
            previous_sid = int(previous_sid_raw) if previous_sid_raw is not None else None
            valid_previous = bool(
                previous_sid is not None or (previous_path is not None and previous_path.is_file())
            )
            if not valid_previous:
                continue
            if item.state not in {"ready", "watched"}:
                if self.db.restore_ready_selection_for_upgrade(
                    item.video_path,
                    previous_path,
                    previous_sid,
                    origin=str(request.get("previous_origin") or ""),
                ):
                    restored += 1
                    self.logger.info(
                        "REPAIR step=subtitle.upgrade_restore media_id=%s episode=%s video=%r",
                        item.media_id,
                        item.episode,
                        str(item.video_path),
                    )
            if job is not None:
                # It has its job (pending, postponed or waiting for the user): its delay,
                # priority and reason stay. Queueing it again for "now" ran a failing
                # check on every tick and put rebuilds ahead of new downloads.
                continue
            self.db.queue_subtitle_job(
                item.video_path,
                item.media_id,
                item.episode,
                priority=_upgrade_request_priority(request, 0 if request.get("rebuild_rule") else 120),
                error=str(request.get("requeue_reason") or err("jobs.checkingUpgrade")),
            )
        return restored

    def _reconcile_ocr_readiness_policy(self) -> int:
        """Put OCR in the timing-uncertain group, or Ready when explicitly allowed."""
        changed = 0
        for item in self.db.episodes():
            if item.state == "watched" or not item.video_path.is_file():
                continue
            if not self._is_legacy_ocr_prepared_subtitle(item):
                continue
            if item.subtitle_path is None or not item.subtitle_path.is_file():
                continue

            if self.config.matching.ocr_counts_as_ready:
                if item.state == "couldnt_sync":
                    self.db.set_subtitle_ready(
                        item.video_path,
                        item.subtitle_path,
                        None,
                        origin="ocr",
                    )
                    changed += 1
                    self.logger.info(
                        "REPAIR step=subtitle.ocr_readiness media_id=%s episode=%s "
                        "video=%r state=ready policy=accepted",
                        item.media_id,
                        item.episode,
                        str(item.video_path),
                    )
                continue

            timing_state_changed = bool(
                item.state != "couldnt_sync" or str(item.subtitle_origin or "").casefold() != "ocr"
            )
            if timing_state_changed:
                self._set_ocr_timing_uncertain(item.video_path, item.subtitle_path)
                changed += 1
                self.logger.info(
                    "REPAIR step=subtitle.ocr_readiness media_id=%s episode=%s "
                    "video=%r state=couldnt_sync policy=timing_uncertain",
                    item.media_id,
                    item.episode,
                    str(item.video_path),
                )
            anime = self.db.get_anime(item.media_id) if item.media_id is not None else None
            if timing_state_changed:
                self.db.queue_subtitle_job(
                    item.video_path,
                    item.media_id,
                    item.episode,
                    priority=80,
                    delay_seconds=_subtitle_source_retry_delay_seconds(
                        poll_minutes=self.config.agent.subtitle_poll_minutes,
                        media_status=(anime.media_status if anime is not None else None),
                        minimum_seconds=60.0,
                    ),
                    error=err("manager.ocrTimingNeedsReview"),
                )
            else:
                # A normal refresh must not push an existing next_check another
                # 24 hours into the future.  Library scans use the same
                # preserve-existing-backoff primitive.
                self.db.ensure_subtitle_job(
                    item.video_path,
                    item.media_id,
                    item.episode,
                )
        return changed

    def _set_ocr_timing_uncertain(
        self,
        video_path: Path,
        subtitle_path: Path,
        *,
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> bool:
        """Retain an OCR SRT in the existing confident-content/uncertain-timing group."""
        current = self.db.episode_by_path(video_path)
        if current is not None and current.state == "ready":
            if not self.db.set_ocr_fallback_not_ready(
                video_path,
                subtitle_path,
                generation=generation,
                owner_token=owner_token,
            ):
                return False
        return self.db.set_couldnt_sync_subtitle(
            video_path,
            subtitle_path,
            origin="ocr",
            generation=generation,
            owner_token=owner_token,
        )

    @staticmethod
    def _japanese_subtitles_required_state_key(media_id: int) -> str:
        return f"japanese_subtitles_required:{int(media_id)}"

    def japanese_subtitles_required(self, media_id: int | None) -> bool:
        """Whether this AniList title should wait/search for Japanese subtitles."""
        if media_id is None:
            return True
        value = (
            self.db.get_state(
                self._japanese_subtitles_required_state_key(int(media_id)),
                "1",
            )
            .strip()
            .casefold()
        )
        return value not in {"0", "false", "no", "off"}

    def set_japanese_subtitles_required(self, media_id: int, required: bool) -> bool:
        """Persist per-anime subtitle policy without deleting downloaded subtitles."""
        media_id = int(media_id)
        required = bool(required)
        self.db.set_state(
            self._japanese_subtitles_required_state_key(media_id),
            "1" if required else "0",
        )
        for item in self.db.episodes(media_id):
            if not required:
                self.db.delete_subtitle_job(item.video_path)
                self.db.delete_state(self._subtitle_upgrade_state_key(item.video_path))
            elif item.state not in {"ready", "watched"} and item.video_path.is_file():
                self.db.ensure_subtitle_job(
                    item.video_path,
                    item.media_id,
                    item.episode,
                )
        # ``state`` rows have no UI trigger, so explicitly invalidate the WebView
        # (in one statement: a read-then-set lost concurrent bumps, DB-8).
        bump = getattr(self.db, "bump_ui_state_version", None)
        if callable(bump):
            bump()
        else:
            try:
                version = int(self.db.get_state("ui_state_version", "0") or 0)
            except ValueError:
                version = 0
            self.db.set_state("ui_state_version", str(version + 1))
        self.logger.info(
            "EVENT subtitle.requirement media_id=%s required=%s",
            media_id,
            required,
        )
        return required

    @staticmethod
    def _embedded_subtitle_probe_state_key(video: Path) -> str:
        """The cached same-container subtitle probe of a (resolved) video path."""
        digest = hashlib.sha1(str(video).encode("utf-8", errors="replace")).hexdigest()[:24]
        return f"embedded_subtitle_probe:v1:{digest}"

    def _cached_embedded_subtitle_details(
        self,
        video: Path,
        *,
        refresh: bool = False,
    ) -> tuple[str, Path | None, int | None]:
        """Cache successful same-container subtitle probes by immutable file stat.

        A probe error is never cached as a negative answer. Sidecars are ignored:
        this helper exists only for the authoritative same-container repair guard.
        """
        resolved = Path(video).expanduser().resolve()
        stat = resolved.stat()
        key = self._embedded_subtitle_probe_state_key(resolved)
        if not refresh:
            raw = self.db.get_state(key, "").strip()
            if raw:
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {}
                if (
                    isinstance(payload, dict)
                    and int(payload.get("version") or 0) == 1
                    and int(payload.get("size") or -1) == int(stat.st_size)
                    and int(payload.get("mtime_ns") or -1) == int(stat.st_mtime_ns)
                ):
                    sid_raw = payload.get("sid")
                    return (
                        str(payload.get("source") or "none"),
                        None,
                        int(sid_raw) if sid_raw is not None else None,
                    )

        source, _path, sid = japanese_subtitle_details(
            resolved,
            ffprobe=self.config.tools.ffprobe,
            ffmpeg=self.config.tools.ffmpeg,
            strict_probe_errors=True,
            ignore_sidecars=True,
        )
        if source not in {"embedded", "embedded_bitmap", "none"}:
            # ignore_sidecars=True means this is defensive, not an expected path.
            return source, None, sid
        self.db.set_state(
            key,
            json.dumps(
                {
                    "version": 1,
                    "size": int(stat.st_size),
                    "mtime_ns": int(stat.st_mtime_ns),
                    "source": source,
                    "sid": sid,
                    "checked_at": time.time(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        return source, None, sid

    @_reusing_db_connection
    def schedule_subtitle_upgrades(
        self,
        *,
        force: bool = False,
        limit: int | None = None,
    ) -> int:
        """Queue safe re-evaluation of selected Japanese subtitles.

        The current selection is preserved in state and restored unless the new
        candidate improves the recorded score by the configured threshold.
        """
        apply_jimaku_trial(self.config)
        if not self.config.matching.auto_upgrade_subtitles and not force:
            return 0
        if not self.config.jimaku.api_key.strip():
            return 0
        checks_left = (
            max(1, int(limit))
            if limit is not None
            else max(0, int(self.config.matching.max_subtitle_upgrade_checks_per_run))
        )
        if checks_left <= 0:
            return 0
        now = time.time()
        scheduled = 0
        episodes = sorted(
            self.db.episodes(),
            key=lambda item: (item.playback_updated_at or 0.0, str(item.video_path)),
        )
        for item in episodes:
            if not self.japanese_subtitles_required(item.media_id):
                continue
            if item.state == "watched" or not item.video_path.is_file():
                continue
            if item.subtitle_path is None and item.embedded_subtitle_id is None:
                continue
            upgrade_key = self._subtitle_upgrade_state_key(item.video_path)
            if self.db.get_state(upgrade_key, "").strip():
                continue
            latest = self.db.latest_selected_subtitle(item.video_path)
            if latest and str(latest.get("source") or "").casefold() == "manual":
                continue

            # A verified Japanese text track embedded in the exact video is the
            # strongest possible release/timing identity.  Automatic subtitle
            # upgrades must never replace it with Jimaku merely because embedded
            # tracks do not carry a historical numeric quality score.  Older
            # versions could do exactly that: upgrade mode used --force-search,
            # skipped the embedded probe, then treated the first measured Jimaku
            # quality as an improvement over the unmeasured embedded selection.
            # Probe here as a repair as well as a guard so already-affected rows
            # return to the same-container track on the next maintenance pass.
            current_origin = str(item.subtitle_origin or "").casefold()
            latest_source = str(latest.get("source") or "").casefold() if latest else ""
            if item.embedded_subtitle_id is not None and current_origin == "embedded":
                continue

            # Only probe selections that could have been produced/replaced by the
            # resolver. This keeps the repair independent of the per-run upgrade
            # scheduling budget without ffprobing every unrelated library row.
            should_probe_embedded = bool(
                current_origin in {"jimaku", "external", "legacy", "pipeline_cache"}
                or latest_source in {"jimaku", "external", "legacy", "upgraded"}
            )
            if should_probe_embedded:
                try:
                    embedded_source, _embedded_path, embedded_sid = self._cached_embedded_subtitle_details(
                        item.video_path,
                        refresh=force,
                    )
                except Exception as exc:
                    self.logger.info(
                        "SKIP step=subtitle.embedded_upgrade_guard video=%r reason=probe_failed error=%r",
                        str(item.video_path),
                        exc,
                    )
                else:
                    if embedded_source == "embedded" and embedded_sid is not None:
                        invalidate_final_pipeline_result(item.video_path, self.config)
                        self.db.set_subtitle_ready(
                            item.video_path, None, int(embedded_sid), origin="embedded"
                        )
                        self.db.record_subtitle_history(
                            video_path=item.video_path,
                            media_id=item.media_id,
                            episode=item.episode,
                            source="embedded",
                            candidate_name=f"embedded sid={int(embedded_sid)}",
                            candidate_path="",
                            score=None,
                            status="selected",
                            reason=(
                                "same-container Japanese text track is authoritative "
                                "over automatic external subtitle upgrades"
                            ),
                            details={
                                "embedded_subtitle_id": int(embedded_sid),
                                "auto_upgrade_guard": True,
                            },
                        )
                        self.db.delete_state(self._subtitle_upgrade_state_key(item.video_path))
                        self.db.delete_subtitle_job(item.video_path)
                        self.logger.info(
                            "REPAIR step=subtitle.embedded_authoritative media_id=%s episode=%s "
                            "video=%r embedded_sid=%s previous_origin=%s",
                            item.media_id,
                            item.episode,
                            str(item.video_path),
                            embedded_sid,
                            item.subtitle_origin,
                        )
                        continue

            if checks_left <= 0:
                continue
            checked_key = self._subtitle_upgrade_checked_key(item.video_path)
            attempts_key = self._subtitle_upgrade_attempts_key(item.video_path)
            try:
                last_checked = float(self.db.get_state(checked_key, "0") or 0)
            except ValueError:
                last_checked = 0.0
            try:
                completed_checks = max(0, int(self.db.get_state(attempts_key, "0") or 0))
            except ValueError:
                completed_checks = 0
            cooldown = _subtitle_upgrade_backoff_hours(completed_checks) * 3600.0
            if not force and last_checked and now - last_checked < cooldown:
                continue
            request = {
                "previous_subtitle_path": str(item.subtitle_path or ""),
                "previous_embedded_sid": item.embedded_subtitle_id,
                "previous_state": item.state,
                "previous_score": latest.get("score") if latest else None,
                "previous_quality": (
                    latest.get("details", {}).get("quality")
                    if latest and isinstance(latest.get("details"), dict)
                    else None
                ),
                "previous_source": latest.get("source") if latest else "legacy",
                "previous_origin": item.subtitle_origin,
                "requested_at": now,
                "forced": bool(force),
                "requeue_priority": 180 if force else 120,
                "requeue_reason": err("jobs.checkingUpgrade"),
            }
            self.db.set_state(upgrade_key, json.dumps(request, ensure_ascii=False))
            self.db.set_state(checked_key, str(now))
            if not force:
                self.db.set_state(attempts_key, str(completed_checks + 1))
            self.db.queue_subtitle_job(
                item.video_path,
                item.media_id,
                item.episode,
                priority=180 if force else 120,
                error=err("jobs.checkingUpgrade"),
            )
            checks_left -= 1
            scheduled += 1
            self.logger.info(
                "SCHEDULE step=subtitle.upgrade media_id=%s episode=%s video=%r force=%s",
                item.media_id,
                item.episode,
                str(item.video_path),
                force,
            )
        return scheduled

    @staticmethod
    def _manual_subtitle_state_key(video: Path) -> str:
        return "manual_subtitle:" + hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()

    @staticmethod
    def _debug_force_subtitle_state_key(video: Path) -> str:
        return "debug_force_subtitle:" + hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()

    @staticmethod
    def _subtitle_candidate_fingerprint_state_key(video: Path) -> str:
        return "subtitle_candidate_set:" + hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()

    @staticmethod
    def _subtitle_force_rebuild_state_key(video: Path) -> str:
        return "subtitle_force_rebuild:" + hashlib.sha1(str(video.resolve()).encode("utf-8")).hexdigest()

    def _legacy_missing_selection_needs_force_rebuild(self, video: Path) -> bool:
        """Recover pre-v204 invalidations that lost their force-rebuild marker.

        v204 stores a durable marker whenever a previously Ready prepared subtitle
        disappears.  Episodes invalidated before v204 already have
        ``waiting_subtitles`` + a queued job, but no marker, so an unchanged
        Jimaku candidate fingerprint can still skip alignment for a day.
        Detect only rows with a previously successful prepared selection whose
        prepared output is now genuinely absent; ordinary never-resolved Waiting
        episodes keep their normal candidate-set backoff.
        """
        try:
            item = self.db.episode_by_path(video)
        except Exception:
            return False
        if (
            item is None
            or item.state != "waiting_subtitles"
            or item.subtitle_path is not None
            or item.embedded_subtitle_id is not None
        ):
            return False
        try:
            history = self.db.latest_selected_subtitle_for_media_or_filename(
                video_path=video,
                media_id=item.media_id,
                episode=item.media_episode if item.media_episode is not None else item.episode,
            )
        except Exception:
            return False
        if not isinstance(history, dict):
            return False
        source = str(history.get("source") or "").casefold()
        details = history.get("details") if isinstance(history.get("details"), dict) else {}
        if source in {"ocr", "bitmap", "raw_unsynced"} or bool(details.get("generated_by_ocr")):
            return False

        prepared_refs: list[str] = []
        for key in ("final_path", "prepared_path", "subtitle_path"):
            raw = str(details.get(key) or "").strip()
            if raw:
                prepared_refs.append(raw)
        if source == "pipeline_cache":
            raw = str(details.get("candidate_path") or history.get("candidate_path") or "").strip()
            if raw:
                prepared_refs.append(raw)
        if not prepared_refs:
            return False
        for raw in prepared_refs:
            path = Path(raw).expanduser()
            try:
                if (
                    path.suffix.casefold() in TEXT_SUBTITLE_EXTENSIONS
                    and path.is_file()
                    and path.stat().st_size > 0
                ):
                    return False
            except OSError:
                pass
        return True

    def _keep_subtitles_during_rebuild(
        self,
        item: LibraryEpisode,
        *,
        fresh: bool,
        priority: int | None = None,
        reason: str = "",
        rebuild_rule: bool = False,
    ) -> bool:
        """Keep a Ready selection playable while its subtitles are prepared again.

        Uses the upgrade check: the new result replaces the old one once it is ready;
        a failed or interrupted run puts the old one back. ``fresh`` takes any ready
        result (the user asked for a rebuild, a score comparison does not apply).
        ``priority`` and ``reason`` are what its job is queued with again (after a
        pause or an interrupted run); ``rebuild_rule`` marks a rebuild rule's request.
        """
        if item.state not in {"ready", "watched"}:
            return False
        subtitle = item.subtitle_path
        has_file = False
        if subtitle is not None:
            try:
                has_file = subtitle.is_file() and subtitle.stat().st_size > 0
            except OSError:
                has_file = False
        if not has_file and item.embedded_subtitle_id is None:
            return False
        latest = self.db.latest_selected_subtitle(item.video_path)
        request = {
            "previous_subtitle_path": str(subtitle or "") if has_file else "",
            "previous_embedded_sid": item.embedded_subtitle_id,
            "previous_state": item.state,
            "previous_score": latest.get("score") if latest else None,
            "previous_quality": (
                latest.get("details", {}).get("quality")
                if latest and isinstance(latest.get("details"), dict)
                else None
            ),
            "previous_source": latest.get("source") if latest else "legacy",
            "previous_origin": item.subtitle_origin,
            "requested_at": time.time(),
            "forced": True,
            "manual_retry": True,
            "fresh_rebuild": bool(fresh),
        }
        if priority is not None:
            request["requeue_priority"] = max(0, int(priority))
        if reason:
            request["requeue_reason"] = str(reason)
        if rebuild_rule:
            request["rebuild_rule"] = True
        self.db.set_state(
            self._subtitle_upgrade_state_key(item.video_path),
            json.dumps(request, ensure_ascii=False),
        )
        return True

    def force_fresh_subtitle_selection(self, video: Path) -> dict[str, Any]:
        video = video.expanduser().resolve()
        item = self.db.episode_by_path(video)
        if item is None:
            raise ManagerError(err("manager.episodeNotInLibrary"))
        # FX Senshi Kurumi-chan E03: the rebuild dropped the working subtitles first, Jimaku
        # timed out, and the episode played without any. The current subtitles stay until
        # the rebuild has new ones; if it fails they are kept (as for a manual retry).
        kept = self._keep_subtitles_during_rebuild(
            item, fresh=True, priority=260, reason=err("jobs.debugFreshSelection")
        )
        subtitle = item.subtitle_path
        if subtitle is not None and not kept:
            try:
                cache_root = self.config.paths.cache_dir.expanduser().resolve()
                resolved = subtitle.expanduser().resolve()
                if resolved.is_file() and resolved.is_relative_to(cache_root):
                    resolved.unlink(missing_ok=True)
            except (OSError, RuntimeError, ValueError, AttributeError):
                pass
        invalidate_final_pipeline_result(video, self.config)
        self._clear_jimaku_api_cache()
        self.db.delete_state(self._manual_subtitle_state_key(video))
        self.db.set_state(self._subtitle_force_rebuild_state_key(video), "1")
        self.db.delete_state(self._subtitle_candidate_fingerprint_state_key(video))
        if not kept:
            self.db.clear_subtitle_selection(video)
        self.db.set_state(self._debug_force_subtitle_state_key(video), "1")
        self.db.queue_subtitle_job(
            video, item.media_id, item.episode, priority=260, error=err("jobs.debugFreshSelection")
        )
        self.logger.info(
            "REQUEUE step=subtitle.debug_fresh media_id=%s episode=%s video=%r force_search=true resync=true force_rebuild=true",
            item.media_id,
            item.episode,
            str(video),
        )
        return {"ok": True, "video_path": str(video), "media_id": item.media_id, "episode": item.episode}

    def _copy_manual_subtitle(
        self, video: Path, subtitle: Path, *, allowed: set[str] | frozenset[str]
    ) -> Path:
        """A private copy of a user-chosen subtitle (the original may be deleted later)."""
        if not video.is_file():
            raise ManagerError(err("manager.videoMissing", path=video))
        if subtitle.suffix.casefold() not in TEXT_SUBTITLE_EXTENSIONS | IMAGE_SUBTITLE_EXTENSIONS | {".pgs"}:
            raise ManagerError(err("manager.unsupportedSubtitle", suffix=subtitle.suffix))
        if subtitle.suffix.casefold() not in allowed:
            raise ManagerError(err("manager.subtitleAsIsNeedsText"))
        if not subtitle.is_file():
            raise ManagerError(err("manager.subtitleMissing", path=subtitle))
        digest = hashlib.sha1(
            f"{video}:{subtitle}:{subtitle.stat().st_mtime_ns}".encode("utf-8")
        ).hexdigest()[:16]
        target_dir = self.config.paths.cache_dir / "manual-subtitles" / digest
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{video.stem}.manual.ja{subtitle.suffix.casefold()}"
        target.write_bytes(subtitle.read_bytes())
        return target

    def use_manual_subtitle_as_is(self, video: Path, subtitle: Path) -> Path:
        """Play a user-chosen text subtitle exactly as it is: no alignment job.

        "Choose subtitle" re-aligns the file, which can shift a hand-fixed or
        already synced one, or reject it. This keeps its timing; like any manual
        choice, automatic upgrades leave it alone.
        """
        video = video.expanduser().resolve()
        subtitle = subtitle.expanduser().resolve()
        # What mpv shows as is (not VobSub .sub index pairs or plain .txt).
        target = self._copy_manual_subtitle(
            video, subtitle, allowed=frozenset({".srt", ".ass", ".ssa", ".vtt"})
        )
        episode = self.db.episode_by_path(video)
        if episode is None:
            raise ManagerError(err("manager.episodeNotInLibrary"))
        # An older prepared result must not come back through the fast path.
        invalidate_final_pipeline_result(video, self.config)
        self.db.set_subtitle_ready(
            video,
            target,
            None,
            origin="manual",
            clear_state_keys=(
                self._manual_subtitle_state_key(video),
                self._subtitle_force_rebuild_state_key(video),
                self._debug_force_subtitle_state_key(video),
                self._subtitle_upgrade_state_key(video),
            ),
        )
        self.db.record_subtitle_history(
            video_path=video,
            media_id=episode.media_id,
            episode=episode.episode,
            source="manual",
            candidate_name=subtitle.name,
            candidate_path=target,
            score=None,
            status="manual",
            reason=err("jobs.userSelectedSubtitleAsIs"),
            details={"original_path": str(subtitle), "final_path": str(target), "as_is": True},
        )
        self.logger.info("EVENT subtitle.manual_as_is video=%r subtitle=%r", str(video), str(target))
        return target

    def set_manual_subtitle(self, video: Path, subtitle: Path) -> Path:
        video = video.expanduser().resolve()
        subtitle = subtitle.expanduser().resolve()
        allowed = TEXT_SUBTITLE_EXTENSIONS | IMAGE_SUBTITLE_EXTENSIONS | {".pgs"}
        target = self._copy_manual_subtitle(video, subtitle, allowed=allowed)
        self.db.set_state(self._manual_subtitle_state_key(video), str(target))
        self.db.delete_state(self._subtitle_force_rebuild_state_key(video))
        episode = self.db.episode_by_path(video)
        self.db.clear_subtitle_selection(video)
        self.db.queue_subtitle_job(
            video,
            episode.media_id if episode else None,
            episode.episode if episode else None,
            priority=250,
            error=err("jobs.manualSubtitle"),
        )
        self.db.record_subtitle_history(
            video_path=video,
            media_id=episode.media_id if episode else None,
            episode=episode.episode if episode else None,
            source="manual",
            candidate_name=subtitle.name,
            candidate_path=target,
            score=None,
            status="manual",
            reason=err("jobs.userSelectedSubtitle"),
            details={"original_path": str(subtitle)},
        )
        self.logger.info("EVENT subtitle.manual_selected video=%r subtitle=%r", str(video), str(target))
        return target

    def scan_subtitle_inbox(self) -> dict[str, int | bool | str]:
        roots = [path.expanduser() for path in self.config.paths.subtitle_dirs]
        allowed = TEXT_SUBTITLE_EXTENSIONS | IMAGE_SUBTITLE_EXTENSIONS | {".pgs", ".zip", ".7z", ".rar"}
        rows: list[tuple[str, int, int]] = []
        for root in roots:
            if not root.is_dir():
                continue
            try:
                for path in root.rglob("*"):
                    if len(rows) >= min(5000, self.config.paths.max_scanned_files):
                        break
                    if not path.is_file() or path.suffix.casefold() not in allowed:
                        continue
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    rows.append((str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size)))
            except (OSError, PermissionError):
                continue
        rows.sort()
        signature = hashlib.sha1(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()
        previous = self.db.get_state("subtitle_inbox_signature", "")
        changed = signature != previous
        requeued = 0
        if changed:
            self.db.set_state("subtitle_inbox_signature", signature)
            if rows:
                # New subtitle files may be what a failing job lacked: only the extra
                # files of a download keep waiting for the person's answer.
                requeued = self.db.force_requeue_unresolved_subtitle_jobs(
                    keep_action_codes=EXTRA_FILE_ACTION_CODES
                )
            self.logger.info(
                "EVENT subtitle_inbox.changed files=%s requeued=%s initial=%s",
                len(rows),
                requeued,
                not bool(previous),
            )
        return {"changed": changed, "files": len(rows), "requeued": requeued, "signature": signature}

    @_reusing_db_connection
    def reconcile_prepared_subtitle_rows(self, media_id: int | None = None) -> int:
        """Repair stale Waiting rows that already point at a valid text subtitle."""
        repaired = 0
        for item in self.db.episodes(int(media_id)) if media_id is not None else self.db.episodes():
            if item.state not in {"local", "waiting_subtitles", "waiting_text_subtitles"}:
                continue
            subtitle = item.subtitle_path
            if subtitle is None or not subtitle.is_file():
                continue
            if subtitle.suffix.casefold() not in TEXT_SUBTITLE_EXTENSIONS:
                continue
            if not self.config.matching.ocr_counts_as_ready and self._is_legacy_ocr_prepared_subtitle(item):
                # OCR output is intentionally playable but not Ready unless the
                # user opted into that policy.  Without this guard the generic
                # prepared-text repair immediately undoes _reconcile_ocr_readiness_policy.
                continue
            self.db.set_subtitle_ready(
                item.video_path,
                subtitle,
                item.embedded_subtitle_id,
                origin=item.subtitle_origin or "prepared",
            )
            repaired += 1
            self.logger.info(
                "REPAIR step=subtitle.prepared_state media_id=%s episode=%s path=%r",
                item.media_id,
                item.episode,
                str(subtitle),
            )
        return repaired

    def _materialize_authoritative_download_row(self, item: DownloadItem, path: Path) -> None:
        """Repair one missing/misidentified row from durable download metadata."""
        resolved = Path(path).expanduser().resolve()
        identity = parse_anime_filename(resolved)
        anime = self.db.get_anime(item.media_id) if item.media_id else None

        # Completed managed-download metadata is authoritative.  A previous
        # scan may have left this exact path attached to another media/episode,
        # and Database.upsert_episode intentionally preserves some non-null
        # historical identity fields.  Remove only the conflicting DB row;
        # never touch the video file.
        existing = self.db.episode_by_path(resolved)
        if (
            existing is not None
            and item.media_id is not None
            and (
                existing.media_id != item.media_id
                or (item.media_episode is not None and existing.media_episode != item.media_episode)
            )
        ):
            self.db.delete_episode_record(resolved)

        release_number = (
            int(item.release_episode)
            if item.release_episode is not None
            else int(identity.episode)
            if identity.episode is not None
            else None
        )
        media_number = (
            int(item.media_episode)
            if item.media_episode is not None
            else int(item.episode)
            if item.episode is not None
            else self._media_episode_from_release(anime, release_number, release_name=str(resolved))
        )

        subtitle_source, subtitle_path = japanese_subtitle_source(
            resolved,
            ffprobe=self.config.tools.ffprobe,
            ffmpeg=self.config.tools.ffmpeg,
        )
        embedded_subtitle_id = None
        if subtitle_source in {"embedded", "embedded_bitmap"}:
            _source, _path, embedded_subtitle_id = japanese_subtitle_details(
                resolved,
                ffprobe=self.config.tools.ffprobe,
                ffmpeg=self.config.tools.ffmpeg,
            )

        repaired_episode = LibraryEpisode(
            media_id=item.media_id,
            title=anime.title if anime else identity.title,
            episode=media_number,
            media_episode=media_number,
            release_episode=release_number,
            video_path=resolved,
            subtitle_path=subtitle_path,
            embedded_subtitle_id=embedded_subtitle_id,
            subtitle_origin=(
                "bitmap"
                if subtitle_source in {"external_bitmap", "embedded_bitmap"}
                else subtitle_source
                if subtitle_source in {"external", "embedded"}
                else ""
            ),
            state=(
                "ready"
                if subtitle_source in {"external", "embedded"}
                else "waiting_text_subtitles"
                if subtitle_source in {"external_bitmap", "embedded_bitmap"}
                else "waiting_subtitles"
            ),
            torrent_hash=item.torrent_hash,
        )
        self.db.upsert_episode(
            repaired_episode,
            downloaded_at=float(item.completed_on or time.time()),
        )
        self._record_media_identity(
            repaired_episode,
            source="download_reconciliation",
            provenance={"subtitle_source": subtitle_source},
        )

        if subtitle_source in {"none", "external_bitmap", "embedded_bitmap"}:
            self.db.ensure_subtitle_job(resolved, item.media_id, media_number)

        self.logger.info(
            "REPAIR step=download.authoritative_episode_row media_id=%s episode=%s release_episode=%s hash=%s",
            item.media_id,
            media_number,
            release_number,
            item.torrent_hash,
        )

    def _completed_download_claims(self, downloads=None, files_by_hash=None) -> dict[Path, set[int]]:
        claims: dict[Path, set[int]] = {}
        for download in self.db.downloads() if downloads is None else downloads:
            if download.media_id is None or not self._download_is_complete(download):
                continue
            files = self._completed_download_video_files(download)
            if files_by_hash is not None:
                files_by_hash[download.torrent_hash] = files
            for path in files:
                claims.setdefault(path.resolve(), set()).add(int(download.media_id))
        return claims

    def _completed_download_owner_conflict(self, item, path: Path, claims: dict[Path, set[int]]) -> bool:
        """Keep an established owner when completed download metadata disagrees.

        A single stale episode row still gets repaired from its download. Two
        completed downloads claiming the same file for different anime require
        manual identity resolution; alternating the owner on every poll loses
        subtitle/playback state and makes the home cards flicker.
        """
        owners = claims.get(path.resolve(), set())
        row = self.db.episode_by_path(path.resolve())
        if (
            row is None
            or row.media_id not in owners
            or item.media_id not in owners
            or row.media_id == item.media_id
        ):
            return False
        key = (str(path.resolve()), row.media_id, item.media_id)
        reported = getattr(self, "_reported_download_owner_conflicts", set())
        if key not in reported:
            self.logger.warning(
                "SKIP step=download.owner_conflict media_id=%s owner_media_id=%s path=%r files_retained=True",
                item.media_id,
                row.media_id,
                str(path),
            )
            if len(reported) < 256:
                reported.add(key)
                self._reported_download_owner_conflicts = reported
        return True

    def _row_owned_by_other_title(self, item, path: Path) -> bool:
        row = self.db.episode_by_path(path.resolve())
        return bool(row is not None and row.media_id is not None and row.media_id != item.media_id)

    _RECONCILE_POLL_MAX_AGE_SECONDS = 300.0

    def _completed_downloads_poll_signature(self) -> tuple[Any, ...] | None:
        """What the poll's reconcile depends on: completed downloads, their entries on disk,
        and (through ui_state_version) the episode rows."""
        try:
            rows = []
            for item in self.db.downloads():
                if item.media_id is None or not self._download_is_complete(item):
                    continue
                content = self._download_content_path(item)
                try:
                    stat = content.stat() if content is not None else None
                except OSError:
                    stat = None
                raw = item.raw or {}
                rows.append(
                    (
                        item.torrent_hash,
                        item.media_id,
                        item.media_episode,
                        item.release_episode,
                        bool(item.is_batch),
                        str(content),
                        stat.st_mtime_ns if stat is not None else -1,
                        bool(raw.get("_identity_conflict") or raw.get("_race_id")),
                    )
                )
            return (self.db.get_state("ui_state_version", ""), tuple(rows))
        except (sqlite3.Error, OSError, RuntimeError):
            return None

    def reconcile_completed_download_rows_from_poll(self) -> int:
        """The foreground poll's reconcile (WEBFLOW-8): nothing to do while nothing changed.

        The poll runs every second while subtitle jobs are due; it rebuilt ownership
        claims for all completed downloads and walked the folder of any with a missing
        entry each time (0.2 s for 150 downloads, ~10 s with many missing entries).
        Now it reruns only when a completed download, its entry on disk or an episode
        row changed (or every 5 minutes), and never walks a save folder.
        """
        signature = self._completed_downloads_poll_signature()
        now = time.monotonic()
        last = getattr(self, "_reconcile_poll_last", None)
        if (
            signature is not None
            and last is not None
            and last[0] == signature
            and now - last[1] < self._RECONCILE_POLL_MAX_AGE_SECONDS
        ):
            return 0
        token = _RECONCILE_FROM_POLL.set(True)
        try:
            repaired = int(self.reconcile_completed_download_rows() or 0)
        finally:
            _RECONCILE_FROM_POLL.reset(token)
        # The signature read before the run: a completion recorded while it ran (sync in
        # another thread, the agent) changes the next poll's signature and is filed then.
        # The run's own repairs cost one more (empty) run.
        self._reconcile_poll_last = (signature, now)
        return repaired

    @_reusing_db_connection
    def reconcile_completed_download_rows(
        self,
        media_id: int | None = None,
        episode: int | None = None,
    ) -> int:
        """Materialize LibraryEpisode rows from completed managed downloads.

        Torrent metadata can already know the exact media/episode while the
        durable episode row is missing (notably after aria2 completed earlier).
        Keep this repair local/cheap: it uses persisted download rows and only
        probes a video when its episode row is actually absent.
        """
        repaired = 0
        wanted_media = int(media_id) if media_id is not None else None
        wanted_episode = int(episode) if episode is not None else None
        downloads = self.db.downloads()
        files_by_hash = {}
        from_poll = bool(_RECONCILE_FROM_POLL.get())
        # The poll changes no established owner (below), so it needs no ownership claims:
        # they walked every completed download's folder on each poll (WEBFLOW-8).
        claims = {} if from_poll else self._completed_download_claims(downloads, files_by_hash)
        scan_roots: tuple[Path, ...] | None = None
        finished_torrent = self._finished_torrent_check()
        for item in downloads:
            self._resolve_download_media(item)
            if item.raw.get("_identity_conflict") or item.raw.get("_race_id"):
                continue
            if wanted_media is not None and item.media_id != wanted_media:
                continue
            if not self._download_is_complete(item):
                continue
            content = self._download_content_path(item)
            if content is None or not content.exists():
                continue
            candidate_episode = item.media_episode if item.media_episode is not None else item.episode
            if (
                wanted_episode is not None
                and candidate_episode is not None
                and int(candidate_episode) != wanted_episode
                and not item.is_batch
            ):
                continue
            candidate_files = files_by_hash.get(item.torrent_hash)
            if candidate_files is None:
                candidate_files = self._completed_download_video_files(item, walk_save_path=not from_poll)
            if scan_roots is None:
                scan_roots = self._library_scan_roots()
            # Preparation drops the row of a file aria2 has not finished: restoring it
            # here repaired the same rows on every poll (REPAIR ... rows=2 each second).
            candidate_files = [
                path
                for path in candidate_files
                if not self.unfinished_library_video(path, roots=scan_roots, finished=finished_torrent)
            ]
            if from_poll:
                # A file filed under another title waits for the full reconcile (after a
                # library scan), which weighs every download's claim on it.
                candidate_files = [
                    path for path in candidate_files if not self._row_owned_by_other_title(item, path)
                ]
            else:
                candidate_files = [
                    path
                    for path in candidate_files
                    if not self._completed_download_owner_conflict(item, path, claims)
                ]
            if not candidate_files:
                continue
            if candidate_files and all(self._library_path_is_ignored(path) for path in candidate_files):
                self.logger.info(
                    "SKIP step=download.completed_episode_row reason=library_ignore_marker hash=%s files=%s",
                    item.torrent_hash,
                    len(candidate_files),
                )
                continue

            def row_matches_download(path: Path) -> bool:
                row = self.db.episode_by_path(path.resolve())
                if row is None:
                    return False
                if (
                    self._extra_file_kept(path.resolve())
                    or self._special_set_aside(path.resolve())
                    or user_episode_identity(self.db, path.resolve())
                ):
                    # Keep or Identify… gave it its own identity (or it waits for the person)
                    return True
                identity = parse_anime_filename(path)
                release_number = identity.episode or item.release_episode
                download_anime = self.db.get_anime(item.media_id) if item.media_id else None
                expected = self._download_file_media_episode(download_anime, item, release_number, path)
                if item.is_batch and self._extra_file_kind(download_anime, path.name):
                    release_number = expected = None  # as registered (an extra file)
                actual = row.media_episode if row.media_episode is not None else row.episode
                return bool(
                    row.media_id == item.media_id
                    and actual == expected
                    and (release_number is None or row.release_episode == int(release_number))
                    # Torrent hash is provenance, not episode identity.  Two
                    # completed torrent rows can legitimately point at the same
                    # local episode (for example after a replacement download).
                    # Requiring hash equality makes reconciliation alternate the
                    # episode row between the two hashes on every foreground poll,
                    # which in turn recreates subtitle jobs forever.
                )

            if candidate_files and all(row_matches_download(path) for path in candidate_files):
                self._complete_download_intent(item)
                # A matching row may still have lost its resolver job. Ensure the
                # job from the durable local episode, without re-probing video.
                for path in candidate_files:
                    row = self.db.episode_by_path(path.resolve())
                    if row is not None and row.state in {
                        "local",
                        "waiting_subtitles",
                        "waiting_text_subtitles",
                        "couldnt_sync",
                    }:
                        self.db.ensure_subtitle_job(
                            row.video_path,
                            row.media_id,
                            row.media_episode if row.media_episode is not None else row.episode,
                        )
                continue
            before = {
                (str(row.video_path.resolve()), row.media_id, row.media_episode, row.torrent_hash)
                for row in self.db.episodes()
                if row.video_path.is_file()
            }
            # The claims read above (the poll reads none: registration reads them itself).
            self._register_completed_download(item, claims=None if from_poll else claims)

            # A completed managed single-episode download is authoritative.
            # If the normal registration path did not materialize the expected
            # LibraryEpisode row, restore it directly from durable download
            # metadata instead of leaving the UI at "Episode record missing".
            # Batch downloads stay on the existing filename-based path.
            if not item.is_batch:
                for path in candidate_files:
                    if not row_matches_download(path):
                        self._materialize_authoritative_download_row(item, path)

            after = {
                (str(row.video_path.resolve()), row.media_id, row.media_episode, row.torrent_hash)
                for row in self.db.episodes()
                if row.video_path.is_file()
            }
            changed = after - before
            if changed:
                repaired += len(changed)
                self.logger.info(
                    "REPAIR step=download.completed_episode_row media_id=%s episode=%s hash=%s rows=%s",
                    item.media_id,
                    candidate_episode,
                    item.torrent_hash,
                    len(changed),
                )
        return repaired

    def import_local_video(self, path: Path) -> LibraryEpisode:
        """Register one user-dropped video without scanning its whole directory."""
        video = Path(path).expanduser().resolve()
        if not video.is_file() or video.suffix.casefold() not in VIDEO_EXTENSIONS:
            raise ManagerError(err("manager.unsupportedVideo", name=video.name))
        identity = parse_anime_filename(video)
        best_anime: LibraryAnime | None = None
        best_score = 0.0
        for anime in self.db.anime_list():
            names = [anime.title, *anime.titles, *anime.synonyms]
            score = strict_title_similarity(identity.title, names)
            threshold = 72.0 if identity.episode is not None else 86.0
            if score >= threshold and score > best_score:
                best_anime, best_score = anime, score

        release_number = identity.episode
        media_number = (
            self._media_episode_from_release(best_anime, release_number, release_name=str(video))
            if best_anime is not None
            else release_number
        )
        subtitle_source, subtitle_path = japanese_subtitle_source(
            video, ffprobe=self.config.tools.ffprobe, ffmpeg=self.config.tools.ffmpeg
        )
        embedded_subtitle_id = None
        if subtitle_source in {"embedded", "embedded_bitmap"}:
            _source, _path, embedded_subtitle_id = japanese_subtitle_details(
                video, ffprobe=self.config.tools.ffprobe, ffmpeg=self.config.tools.ffmpeg
            )
        item = LibraryEpisode(
            media_id=best_anime.media_id if best_anime is not None else None,
            title=best_anime.title if best_anime is not None else identity.title,
            episode=media_number,
            media_episode=media_number,
            release_episode=release_number,
            video_path=video,
            subtitle_path=subtitle_path,
            embedded_subtitle_id=embedded_subtitle_id,
            subtitle_origin=(
                "bitmap"
                if subtitle_source in {"external_bitmap", "embedded_bitmap"}
                else subtitle_source
                if subtitle_source in {"external", "embedded"}
                else ""
            ),
            state=(
                "ready"
                if subtitle_source in {"external", "embedded"}
                else "waiting_text_subtitles"
                if subtitle_source in {"external_bitmap", "embedded_bitmap"}
                else "waiting_subtitles"
            ),
            torrent_hash="",
        )
        # No downloaded_at: that marks a file Pudge downloaded (and may delete
        # after watching). A dropped video is the user's own file, wherever it is;
        # an upsert without it keeps the value of a row Pudge did download.
        self.db.upsert_episode(item)
        self._remember_dropped_media_file(video)
        self._record_media_identity(item, source="manual_import")
        if subtitle_source in {"none", "external_bitmap", "embedded_bitmap"}:
            self.db.ensure_subtitle_job(video, item.media_id, media_number)
        return self.db.episode_by_path(video) or item

    def diagnose_episode(self, media_id: int, episode: int | None = None) -> dict[str, Any]:
        anime = self.db.get_anime(int(media_id))
        if anime is None:
            raise ManagerError(err("manager.animeNotInLocalDbShort", mediaId=media_id))
        # Movies use a null episode identity in the local library. Some older
        # UI payloads still pass next_episode=1; normalize that here so Why not
        # ready never reports a long-downloaded movie as missing.
        if str(anime.format or "").upper() == "MOVIE":
            episode = None
        self.reconcile_prepared_subtitle_rows(int(media_id))
        self.reconcile_completed_download_rows(int(media_id), episode)
        local_items = self.db.episodes(int(media_id))
        selected_for_request = (
            next((item for item in local_items if item.episode == int(episode)), None)
            if episode is not None
            else None
        )
        if selected_for_request is None and self.downloads_configured():
            try:
                self.sync_downloads()
                self.reconcile_completed_download_rows(int(media_id), episode)
            except Exception as exc:
                self.logger.warning(
                    "RETRY step=diagnose.sync_downloads media_id=%s episode=%s error=%r",
                    media_id,
                    episode,
                    str(exc),
                )
            self.reconcile_prepared_subtitle_rows(int(media_id))
            local_items = self.db.episodes(int(media_id))
        selected = None
        if episode is not None:
            selected = next((item for item in local_items if item.episode == int(episode)), None)
        if selected is None and local_items and episode is None:
            selected = next(
                (item for item in local_items if item.state not in {"watched", "ready"}),
                local_items[0],
            )
        downloads = [
            item
            for item in self.db.downloads()
            if item.media_id == int(media_id)
            and (episode is None or item.episode in {None, int(episode)} or item.is_batch)
        ]
        jobs = [
            row
            for row in self.db.subtitle_jobs()
            if (selected is not None and str(row["video_path"]) == str(selected.video_path))
            or (
                row["media_id"] is not None
                and int(row["media_id"]) == int(media_id)
                and (episode is None or row["episode"] is None or int(row["episode"]) == int(episode))
            )
        ]
        language = self.config.ui.language
        labels = {
            name: t(f"manager.{key}", lang=language)
            for name, key in (
                ("anilist", "diagAnilistMatched"),
                ("downloaded", "diagVideoDownloaded"),
                ("downloading", "diagVideoDownloading"),
                ("file", "diagLocalVideo"),
                ("episode_record", "diagEpisodeRecordMissing"),
                ("not_downloaded", "diagNotDownloaded"),
                ("download_in_progress", "diagStillDownloading"),
                ("download_unlinked", "diagDownloadUnlinked"),
                ("subtitle", "diagSubtitlesPrepared"),
                ("job", "diagPreparationJob"),
                ("none", "diagNoSubtitles"),
                ("image", "diagImageSubtitlesOnly"),
                ("no_video", "diagNoVideo"),
                ("missing_job", "diagMissingJob"),
                ("completed", "diagCompleted"),
            )
        }
        action_job = jobs[0] if jobs else None
        primary_download = downloads[0] if downloads else None
        watched_externally = bool(
            selected is not None
            and watched_by_anilist_progress(
                selected.last_episode,
                anime.progress,
                total_episodes=anime.episodes,
                media_format=anime.format,
            )
        )
        presentation = derive_episode_presentation(
            local=selected,
            download=primary_download,
            action_job=action_job,
            watched_externally=watched_externally,
            allow_ocr_ready=bool(self.config.matching.ocr_counts_as_ready),
        )

        video_exists = bool(selected and selected.video_path.is_file())
        completed_download = next((item for item in downloads if download_complete(item)), None)
        downloaded = bool(video_exists or completed_download is not None)
        download_detail = labels["no_video"]
        if video_exists and selected is not None:
            download_detail = str(selected.video_path)
        elif primary_download is not None:
            download_detail = (
                f"{primary_download.state} · {int(round(float(primary_download.progress or 0.0) * 100))}%"
            )

        file_label = labels["file"]
        file_detail = str(selected.video_path) if selected is not None else labels["not_downloaded"]
        if selected is None and completed_download is not None:
            file_label = labels["episode_record"]
            file_detail = labels["download_unlinked"]
        elif selected is None and primary_download is not None:
            file_detail = labels["download_in_progress"]

        checks: list[dict[str, Any]] = [
            {
                "key": "anilist",
                "ok": True,
                "label": labels["anilist"],
                "detail": f"id={anime.media_id}",
            },
            {
                "key": "download",
                "ok": downloaded,
                "label": labels["downloaded"] if downloaded else labels["downloading"],
                "detail": download_detail,
            },
            {
                "key": "file",
                "ok": video_exists,
                "label": file_label,
                "detail": file_detail,
            },
        ]

        external_subtitle_ready = False
        if selected is not None and selected.subtitle_path is not None:
            try:
                external_subtitle_ready = (
                    selected.subtitle_path.is_file() and selected.subtitle_path.stat().st_size > 0
                )
            except OSError:
                external_subtitle_ready = False
        subtitle_ready = bool(
            selected
            and selected.state in {"ready", "watched"}
            and (external_subtitle_ready or selected.embedded_subtitle_id is not None)
        )
        subtitle_detail = labels["none"]
        if selected is not None:
            if selected.subtitle_path:
                subtitle_detail = str(selected.subtitle_path)
            elif selected.embedded_subtitle_id is not None:
                subtitle_detail = f"Embedded subtitle sid={selected.embedded_subtitle_id}"
            elif selected.state == "waiting_text_subtitles":
                subtitle_detail = labels["image"]
        checks.append(
            {
                "key": "subtitle",
                "ok": subtitle_ready,
                "label": labels["subtitle"],
                "detail": subtitle_detail,
            }
        )

        raw_last_error = (
            _localize_preparation_detail(
                str(jobs[0]["last_error"] or ""),
                language=self.config.ui.language,
            )
            if jobs
            else ""
        )
        # A whole catalog message (English, or Russian from older versions) is
        # shown in the interface language; markers are matched on the raw text.
        last_error = i18n_message(raw_last_error, self.config.ui.language)
        if (
            not subtitle_ready
            and checks
            and checks[-1].get("key") == "subtitle"
            and "video container is not readable" in raw_last_error.casefold()
        ):
            checks[-1]["detail"] = t("manager.diagVideoCorrupt", lang=self.config.ui.language)
        checks.append(
            {
                "key": "job",
                "ok": subtitle_ready or bool(jobs),
                "label": labels["job"],
                "detail": (
                    f"{jobs[0]['state']}; attempts={jobs[0]['attempts']}; {last_error}"
                    if jobs
                    else (labels["completed"] if subtitle_ready else labels["missing_job"])
                ),
            }
        )
        return {
            "media_id": anime.media_id,
            "title": anime.title,
            "episode": episode,
            "ready": subtitle_ready,
            "video_path": str(selected.video_path) if selected else "",
            "state": selected.state if selected else "missing",
            "identify_allowed": self.identify_allowed(selected),
            "watched_on_anilist": watched_externally,
            "presentation": presentation,
            "checks": checks,
            "downloads": [
                {
                    "name": item.name,
                    "state": item.state,
                    "progress": item.progress,
                    "torrent_hash": item.torrent_hash,
                }
                for item in downloads[:5]
            ],
            "download_intent": self.download_intents.get(
                int(media_id),
                int(episode) if episode is not None else None,
                False,
            ),
            "job": (
                {
                    "state": str(jobs[0]["state"]),
                    "attempts": int(jobs[0]["attempts"] or 0),
                    "next_check": float(jobs[0]["next_check"] or 0),
                    "error": last_error,
                }
                if jobs
                else None
            ),
        }

    def _durable_prepared_subtitle_root(self) -> Path:
        """Return non-evictable storage for subtitle files referenced by SQLite."""
        return self.config.library.database_path.expanduser().resolve().parent / "prepared-subtitles"

    def _persist_prepared_text_subtitle(
        self,
        video: Path,
        subtitle: Path | None,
    ) -> Path | None:
        """Copy a prepared text subtitle out of ~/Library/Caches before Ready.

        ``episodes.subtitle_path`` is durable application state. Pointing it at
        ``~/Library/Caches`` is unsafe because macOS or a cache cleaner may
        evict that file while SQLite still says the episode is Ready.
        """
        if subtitle is None:
            return None
        source = Path(subtitle).expanduser()
        try:
            if (
                source.suffix.casefold() not in TEXT_SUBTITLE_EXTENSIONS
                or not source.is_file()
                or source.stat().st_size <= 0
            ):
                return source
            source = source.resolve()
        except OSError:
            return source

        root = self._durable_prepared_subtitle_root()
        try:
            if source.is_relative_to(root):
                return source
            cache_root = self.config.paths.cache_dir.expanduser().resolve()
            if not source.is_relative_to(cache_root):
                # A manual/user-owned subtitle outside Pudge's cache is already
                # durable and must keep its original path.
                return source
        except (OSError, RuntimeError, ValueError):
            return source

        try:
            stat = source.stat()
            digest = hashlib.sha1(
                (f"{Path(video).expanduser().resolve()}:{source}:{stat.st_size}:{stat.st_mtime_ns}").encode(
                    "utf-8"
                )
            ).hexdigest()[:24]
            root.mkdir(parents=True, exist_ok=True)
            target = root / f"{digest}{source.suffix.casefold() or '.srt'}"
            if target.is_file() and target.stat().st_size == stat.st_size:
                return target.resolve()
            temporary = target.with_name(f".{target.name}.{time.time_ns()}.tmp")
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
            return target.resolve()
        except OSError as exc:
            self.logger.warning(
                "FAIL step=subtitle.persist_ready video=%s subtitle=%r error=%r",
                Path(video).name,
                str(source),
                str(exc),
            )
            return source

    @_reusing_db_connection
    def _persist_ready_subtitle_selections(
        self,
        *,
        preserve_paths: set[str] | None = None,
    ) -> int:
        """Move surviving Ready cache selections into durable storage."""
        preserve = {str(value) for value in (preserve_paths or set())}
        cache_root = self.config.paths.cache_dir.expanduser().resolve()
        migrated = 0
        for item in list(self.db.episodes()):
            if item.state not in {"ready", "watched"} or item.subtitle_path is None:
                continue
            if str(item.video_path) in preserve:
                continue
            try:
                source = item.subtitle_path.expanduser().resolve()
                if not source.is_file() or source.stat().st_size <= 0:
                    continue
                if source.suffix.casefold() not in TEXT_SUBTITLE_EXTENSIONS:
                    continue
                if not source.is_relative_to(cache_root):
                    continue
            except (OSError, RuntimeError, ValueError):
                continue
            durable = self._persist_prepared_text_subtitle(item.video_path, source)
            if durable is None or durable == source:
                continue
            self.db.set_subtitle_ready(
                item.video_path,
                durable,
                item.embedded_subtitle_id,
                origin=str(item.subtitle_origin or ""),
            )
            migrated += 1
        return migrated

    def revalidate_subtitle_series(
        self,
        media_id: int | None,
        *,
        reason: str = "",
        priority: int = 260,
    ) -> dict[str, int]:
        """Validate every local episode in a series after one subtitle failure.

        Valid Ready siblings stay Ready. Missing Ready selections are invalidated
        and queued immediately; unresolved siblings are guaranteed to have a job.
        """
        reason = reason or err("jobs.seriesRecheck")
        result = {"checked": 0, "requeued": 0, "jobs_created": 0, "migrated": 0}
        if media_id is None or not self.japanese_subtitles_required(media_id):
            return result
        existing_jobs = {str(row["video_path"]) for row in self.db.subtitle_jobs()}
        for item in list(self.db.episodes(int(media_id))):
            if not item.video_path.is_file():
                continue
            result["checked"] += 1
            if item.state == "ready":
                valid_external = False
                if item.subtitle_path is not None:
                    try:
                        valid_external = (
                            item.subtitle_path.is_file() and item.subtitle_path.stat().st_size > 0
                        )
                    except OSError:
                        valid_external = False
                if valid_external:
                    durable = self._persist_prepared_text_subtitle(item.video_path, item.subtitle_path)
                    if durable is not None and durable != item.subtitle_path:
                        self.db.set_subtitle_ready(
                            item.video_path,
                            durable,
                            item.embedded_subtitle_id,
                            origin=str(item.subtitle_origin or ""),
                        )
                        result["migrated"] += 1
                    continue
                if item.embedded_subtitle_id is not None:
                    continue
                self.db.invalidate_subtitle(
                    item.video_path,
                    item.media_id,
                    item.episode,
                    reason,
                )
                self.db.queue_subtitle_job(
                    item.video_path,
                    item.media_id,
                    item.episode,
                    error=reason,
                    priority=priority,
                )
                result["requeued"] += 1
                continue
            if item.state in {
                "local",
                "waiting_subtitles",
                "waiting_text_subtitles",
                "couldnt_sync",
            }:
                if str(item.video_path) not in existing_jobs:
                    self.db.queue_subtitle_job(
                        item.video_path,
                        item.media_id,
                        item.episode,
                        error=reason,
                        priority=priority,
                    )
                    existing_jobs.add(str(item.video_path))
                    result["jobs_created"] += 1
        self.logger.info(
            "REPAIR step=subtitle.series_revalidate media_id=%s result=%s reason=%r",
            media_id,
            result,
            reason,
        )
        return result

    def repair_library_integrity(self, *, automatic: bool = False, scan: bool = True) -> dict[str, int]:
        result = {
            "missing_episode_rows": 0,
            "stale_subtitles": 0,
            "spurious_jobs": 0,
            "bitmap_rows": 0,
            "jobs_created": 0,
            "library_rows": 0,
        }
        if self.downloads_configured():
            try:
                self.sync_downloads()
            except Exception as exc:
                self.logger.warning("FAIL step=repair.sync_downloads error=%r", str(exc))
        missing: list[Path] = []
        # Read once: every missing row read all download rows again (each library row when
        # the library disk is unplugged).
        download_hashes = {str(d.torrent_hash or "").casefold() for d in self.db.downloads()}
        for item in list(self.db.episodes()):
            if item.video_path.is_file():
                continue
            # Case-blind, as cleanup(): a row's hash can be in another case than the client's.
            active = bool(item.torrent_hash) and str(item.torrent_hash).casefold() in download_hashes
            if not active:
                missing.append(item.video_path)
        # Never for a file on an unplugged drive; the automatic pass also waits
        # until the file stayed missing (an explicit Repair does not).
        for value in sorted(self._forgettable_missing_videos(missing, immediate=not automatic)):
            self.db.delete_episode_record(Path(value))
            result["missing_episode_rows"] += 1
        result["bitmap_rows"] = self.db.repair_bitmap_ready_rows()
        # Upgrade checks and rebuilds that keep their subtitles own their jobs (priority,
        # throttle marker): only jobs of other Ready rows are spurious.
        upgrade_paths = self._active_subtitle_upgrade_paths()
        result["spurious_jobs"] = self.db.repair_spurious_ready_subtitle_jobs(preserve_paths=upgrade_paths)
        result["stale_subtitles"] = self.db.repair_stale_subtitle_selections(preserve_paths=upgrade_paths)
        result["library_rows"] = len(self.scan_library()) if scan else len(self.db.episodes())
        for item in self.db.episodes():
            if item.state in {"local", "waiting_subtitles", "waiting_text_subtitles", "couldnt_sync"}:
                if self.db.ensure_subtitle_job(item.video_path, item.media_id, item.episode):
                    result["jobs_created"] += 1
        self.db.set_state("integrity_last_run", str(time.time()))
        self.logger.info("DONE step=library.repair automatic=%s result=%s", automatic, result)
        return result

    def repair_library_if_due(self, *, now: float | None = None) -> dict[str, int]:
        now = float(now or time.time())
        try:
            last = float(self.db.get_state("integrity_last_run", "0") or 0)
        except ValueError:
            last = 0.0
        if now - last < 6 * 3600:
            return {}
        return self.repair_library_integrity(automatic=True, scan=False)

    def _recover_selected_text_subtitle_from_history(
        self,
        video: Path,
        *,
        media_id: int | None,
        episode: int | None,
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> Path | None:
        """Restore a still-valid prepared text subtitle lost from the episode row.

        ``clear_subtitle_selection`` intentionally keeps subtitle history.  If a
        later retry sees an unchanged candidate fingerprint, the prepare-only
        subprocess skips alignment, so a valid cached selection could otherwise
        stay in Waiting forever.  Recover only a previously *selected* prepared
        text path; raw Jimaku candidates and OCR fallbacks remain subject to their
        normal validation/readiness policies.
        """
        get_state = getattr(self.db, "get_state", None)
        explicit_rebuild = False
        if callable(get_state):
            explicit_rebuild = (
                get_state(self._subtitle_force_rebuild_state_key(video), "").strip() == "1"
                or get_state(self._debug_force_subtitle_state_key(video), "").strip() == "1"
            )
        if explicit_rebuild:
            self.logger.info(
                "SKIP step=subtitle.history_selection media_id=%s episode=%s "
                "video=%s reason=explicit_rebuild",
                media_id,
                episode,
                video.name,
            )
            return None

        try:
            item = self.db.episode_by_path(video)
        except Exception:
            return None
        if item is None or item.subtitle_path is not None or item.embedded_subtitle_id is not None:
            return None
        try:
            history = self.db.latest_selected_subtitle(video)
        except Exception:
            return None
        if not isinstance(history, dict):
            return None

        details = history.get("details") if isinstance(history.get("details"), dict) else {}
        source = str(history.get("source") or details.get("source") or "history")
        generated_by_ocr = bool(details.get("generated_by_ocr"))
        if not self.config.matching.ocr_counts_as_ready and (generated_by_ocr or source.casefold() == "ocr"):
            return None
        quality = details.get("quality") if isinstance(details.get("quality"), dict) else {}
        if quality.get("accepted") is False:
            return None

        candidates: list[object] = []
        for key in ("final_path", "prepared_path", "subtitle_path"):
            if details.get(key):
                candidates.append(details[key])
        # A pipeline-cache history row already points at the prepared output.
        # For Jimaku rows candidate_path is the raw source and must not be
        # resurrected when its aligned final_path disappeared.
        if source.casefold() == "pipeline_cache":
            if details.get("candidate_path"):
                candidates.append(details["candidate_path"])
            if history.get("candidate_path"):
                candidates.append(history["candidate_path"])

        for value in candidates:
            raw = str(value or "").strip()
            if not raw:
                continue
            path = Path(raw).expanduser()
            try:
                usable = (
                    path.suffix.casefold() in TEXT_SUBTITLE_EXTENSIONS
                    and path.is_file()
                    and path.stat().st_size > 0
                )
            except OSError:
                usable = False
            if not usable:
                continue
            resolved = path.resolve()
            if generation is None and owner_token is None:
                committed = self.db.set_subtitle_ready(video, resolved, None, origin=source)
                # Legacy/test database adapters historically returned None on success.
                if committed is False:
                    return None
            else:
                committed = self.db.set_subtitle_ready(
                    video,
                    resolved,
                    None,
                    origin=source,
                    generation=generation,
                    owner_token=owner_token,
                )
                if not committed:
                    return None
            self.logger.info(
                "REPAIR step=subtitle.history_selection media_id=%s episode=%s "
                "video=%s subtitle=%r source=%s",
                media_id,
                episode,
                video.name,
                str(resolved),
                source,
            )
            return resolved
        return None

    def _throttle_sync_update_requeues(self, jobs: list[Any]) -> list[Any]:
        """Claimed queue jobs that run now. Jobs re-queued by a sync update (marked with
        ``jobs.requeueSyncUpdate``) wait while regular jobs run, and run one per pass otherwise;
        the rest are deferred by at least five minutes."""
        migration_marker = err("jobs.requeueSyncUpdate")
        # Rows queued by older versions carry the Russian text of the same marker.
        migration_markers = (migration_marker, t("error.jobs.requeueSyncUpdate", lang="ru"))
        has_regular_jobs = any(
            not any(marker in str(row["last_error"] or "") for marker in migration_markers) for row in jobs
        )
        migration_budget = 0 if has_regular_jobs else 1
        runnable_jobs = []
        for queued in jobs:
            is_migration = any(marker in str(queued["last_error"] or "") for marker in migration_markers)
            if is_migration and migration_budget <= 0:
                delay = max(5 * 60, self.config.agent.subtitle_poll_minutes * 60)
                self.db.defer_subtitle_job(
                    Path(str(queued["video_path"])),
                    migration_marker,
                    delay,
                    generation=int(queued["generation"] or 0),
                    owner_token=str(queued["owner_token"] or ""),
                )
                self.logger.info(
                    "RETRY step=subtitle.prepare reason=energy_throttle media_id=%s episode=%s video=%s delay_s=%s",
                    queued["media_id"],
                    queued["episode"],
                    Path(str(queued["video_path"])).name,
                    delay,
                )
                continue
            if is_migration:
                migration_budget -= 1
            runnable_jobs.append(queued)
        return runnable_jobs

    @classmethod
    def _subtitle_failure_streak_state_key(cls, video: Path) -> str:
        return cls._subtitle_candidate_fingerprint_state_key(video) + ":failure_streak"

    def _count_subtitle_failure(
        self,
        video: Path,
        signature: str,
        *,
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> int:
        """Count this failure; return how many identical ones came in a row."""
        key = self._subtitle_failure_streak_state_key(video)
        try:
            previous = json.loads(self.db.get_state(key, "") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            previous = {}
        if not isinstance(previous, dict) or previous.get("signature") != signature:
            previous = {}
        try:
            streak = max(0, int(previous.get("count") or 0)) + 1
        except (TypeError, ValueError):
            streak = 1
        self.db.set_state_if_subtitle_job_owned(
            key,
            json.dumps({"signature": signature, "count": streak}),
            video_path=video,
            generation=generation,
            owner_token=owner_token,
        )
        return streak

    def _retry_unfinished_subtitle_run(
        self,
        video: Path,
        detail: str,
        *,
        signature: str,
        generation: int | None = None,
        owner_token: str | None = None,
    ) -> None:
        """A run that ended without a verdict (20-minute timeout, the worker failed to start).

        The episode keeps its playable fallback. Each identical failure in a row doubles
        the wait; after ``SUBTITLE_FAILURE_STREAK_CAP`` of them the job waits for the
        person (Action required) instead of burning another 20 minutes.
        """
        owner_kwargs = {"generation": generation, "owner_token": owner_token}
        streak = self._count_subtitle_failure(video, signature, **owner_kwargs)
        delay = _subtitle_failure_backoff_seconds(
            poll_minutes=self.config.agent.subtitle_poll_minutes, streak=streak
        )
        exhausted = streak >= SUBTITLE_FAILURE_STREAK_CAP
        self.db.reschedule_subtitle_job(
            video,
            detail,
            delay,
            count_attempt=True,
            action_code=SUBTITLE_RETRY_EXHAUSTED if exhausted else "",
            **owner_kwargs,
        )
        self.logger.info(
            "RETRY step=subtitle.prepare reason=%s video=%s streak=%s delay_s=%s exhausted=%s detail=%r",
            signature,
            video.name,
            streak,
            0 if exhausted else int(delay),
            exhausted,
            str(detail)[-300:],
        )

    def process_subtitle_jobs(
        self,
        limit: int = 4,
        *,
        preferred_paths: tuple[Path, ...] | list[Path] | None = None,
        wait_for_slot: bool = False,
    ) -> int:
        apply_jimaku_trial(self.config)
        if not wait_for_slot:
            block_reason = self.work_scheduler.background_wait_reason()
            if block_reason is not None:
                self.logger.info(
                    "SKIP step=subtitle.process reason=%s limit=%s",
                    block_reason,
                    limit,
                )
                return 0
        ready = 0
        if preferred_paths:
            jobs = self.db.claim_subtitle_jobs_for_paths(
                preferred_paths,
                limit=limit,
            )
        else:
            jobs = self.db.claim_due_subtitle_jobs(limit)

        if not preferred_paths and jobs:
            jobs = self._throttle_sync_update_requeues(jobs)

        def scheduler_wait_detail(reason: str) -> str:
            return {
                "energy_saving": err("jobs.pausedEnergySaving"),
                "thermal": err("jobs.pausedThermal"),
                "foreground": err("jobs.foregroundPlayback"),
            }.get(str(reason), err("jobs.backgroundDeferred"))

        def requeue_unstarted(start_index: int, reason: str = "foreground") -> None:
            # Never tried: no attempt is counted and the priority stays.
            for queued in jobs[start_index:]:
                queued_video = Path(str(queued["video_path"]))
                self.db.reschedule_subtitle_job(
                    queued_video,
                    scheduler_wait_detail(reason),
                    60,
                    generation=int(queued["generation"] or 0),
                    owner_token=str(queued["owner_token"] or ""),
                )

        # A job someone waits for ("fresh subtitles", a retry) runs at USER priority:
        # only playback in the foreground stops it, not energy saving.
        job_priority = WorkPriority.USER if wait_for_slot else WorkPriority.BACKGROUND

        for job_index, job in enumerate(jobs):
            video = Path(str(job["video_path"]))
            media_id = int(job["media_id"]) if job["media_id"] is not None else None
            episode = int(job["episode"]) if job["episode"] is not None else None
            job_generation = int(job["generation"] or 0)
            job_owner_token = str(job["owner_token"] or "")
            owner_kwargs = {
                "generation": job_generation,
                "owner_token": job_owner_token,
            }

            def job_still_owned() -> bool:
                return self.db.subtitle_job_owned(video, job_generation, job_owner_token)

            if media_id is not None and not self.japanese_subtitles_required(media_id):
                self.db.delete_subtitle_job(video, **owner_kwargs)
                self.logger.info(
                    "SKIP step=subtitle.prepare reason=japanese_subtitles_optional media_id=%s episode=%s video=%s",
                    media_id,
                    episode,
                    video.name,
                )
                continue
            if not wait_for_slot:
                block_reason = self.work_scheduler.background_wait_reason()
                if block_reason is not None:
                    self.db.reschedule_subtitle_job(
                        video, scheduler_wait_detail(block_reason), 60, **owner_kwargs
                    )
                    self.logger.info(
                        "RETRY step=subtitle.prepare reason=%s media_id=%s episode=%s video=%s delay_s=60",
                        block_reason,
                        job["media_id"],
                        job["episode"],
                        video.name,
                    )
                    requeue_unstarted(job_index + 1, block_reason)
                    break
            if not video.is_file() and self._missing_video_was_watched(video, media_id, episode):
                # Cleanup deleted a watched episode: nothing will ever need its subtitles.
                self.db.delete_subtitle_job(video, **owner_kwargs)
                self.logger.info(
                    "SKIP step=subtitle.prepare reason=watched_video_gone media_id=%s episode=%s video=%s",
                    media_id,
                    episode,
                    video.name,
                )
                continue
            if not video.is_file():
                self.logger.info(
                    "RETRY step=subtitle.prepare reason=video_missing media_id=%s episode=%s video=%s delay_s=%s",
                    job["media_id"],
                    job["episode"],
                    video.name,
                    15 * 60,
                )
                self.db.postpone_subtitle_job(
                    video, err("jobs.videoNotAvailableYet"), 15 * 60, **owner_kwargs
                )
                continue
            incomplete_roots = self.incomplete_download_paths()
            if any(video.resolve() == root or root in video.resolve().parents for root in incomplete_roots):
                self.db.postpone_subtitle_job(video, err("jobs.torrentWriting"), 15 * 60, **owner_kwargs)
                self.logger.info(
                    "RETRY step=subtitle.prepare reason=download_incomplete media_id=%s episode=%s video=%s delay_s=%s",
                    job["media_id"],
                    job["episode"],
                    video.name,
                    15 * 60,
                )
                continue
            if self.unfinished_library_video(video):
                # A removed candidate's empty file: no episode, nothing to retry for a day.
                self.db.delete_episode_record(video)
                self.logger.info(
                    "SKIP step=subtitle.prepare reason=unfinished_video media_id=%s episode=%s video=%s",
                    job["media_id"],
                    job["episode"],
                    video.name,
                )
                continue
            if episode is None and self._own_extra_file(video):
                # An NCOP/NCED/PV of the person's own folder: it stays in the Library and
                # never waits for subtitles.
                self.db.mark_subtitle_job_needs_action(
                    video, err("jobs.extraFileKept"), self.EXTRA_FILE_KEPT, **owner_kwargs
                )
                self.logger.info(
                    "SKIP step=subtitle.prepare reason=own_extra_file media_id=%s video=%s",
                    media_id,
                    video.name,
                )
                continue
            extra_kind = self._downloaded_extra_file_kind(video, media_id, episode)
            if extra_kind:
                kept = bool(self.db.get_state(self._extra_file_kept_state_key(video), ""))
                self.db.mark_subtitle_job_needs_action(
                    video,
                    err("jobs.extraFileKept") if kept else err("jobs.extraDownloadFile", kind=extra_kind),
                    self.EXTRA_FILE_KEPT if kept else self.EXTRA_FILE_REVIEW,
                    **owner_kwargs,
                )
                self.logger.info(
                    "SKIP step=subtitle.prepare reason=extra_download_file kind=%s kept=%s media_id=%s video=%s",
                    extra_kind,
                    kept,
                    media_id,
                    video.name,
                )
                continue
            probe = None
            if video.stat().st_size >= 1024 * 1024:
                try:
                    probe = subprocess.run(
                        [
                            self.config.tools.ffprobe,
                            "-v",
                            "error",
                            "-show_entries",
                            "format=duration",
                            "-of",
                            "default=noprint_wrappers=1:nokey=1",
                            str(video),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    probe = None
            if probe is not None and probe.returncode != 0:
                probe_error = str(probe.stderr or "")
                if self._repair_unreadable_orphaned_download(
                    video,
                    media_id=media_id,
                    episode=episode,
                    attempts=int(job["attempts"] or 0),
                    probe_error=probe_error,
                ):
                    # The stale episode/job row was removed and a clean managed
                    # download was started (or queued). Do not keep retrying
                    # subtitle preparation against the quarantined payload.
                    continue
                self.db.postpone_subtitle_job(video, err("jobs.containerUnreadable"), 60 * 60, **owner_kwargs)
                self.logger.info(
                    "RETRY step=subtitle.prepare reason=video_container_unreadable media_id=%s episode=%s video=%s delay_s=%s error=%r",
                    job["media_id"],
                    job["episode"],
                    video.name,
                    60 * 60,
                    probe_error[-500:],
                )
                continue
            command = [
                python_executable(),
                "-m",
                "pudge.cli",
                "--prepare-only",
                "--no-anilist-progress",
            ]
            upgrade_key = self._subtitle_upgrade_state_key(video)
            upgrade_raw = self.db.get_state(upgrade_key, "").strip()
            try:
                upgrade_request = json.loads(upgrade_raw) if upgrade_raw else None
            except (TypeError, ValueError, json.JSONDecodeError):
                upgrade_request = None
            if not isinstance(upgrade_request, dict):
                upgrade_request = None
            if upgrade_request is None:
                recovered = self._recover_selected_text_subtitle_from_history(
                    video, media_id=media_id, episode=episode, **owner_kwargs
                )
                if recovered is not None:
                    ready += 1
                    continue
            previous_backup: Path | None = None
            if upgrade_request is not None:
                command.append("--force-search")
                previous_value = str(upgrade_request.get("previous_subtitle_path") or "").strip()
                previous_path = Path(previous_value) if previous_value else None
                if previous_path is not None and previous_path.is_file():
                    backup_dir = self.config.paths.cache_dir / "subtitle-upgrade-backups"
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    previous_backup = backup_dir / (
                        hashlib.sha1(str(video).encode("utf-8")).hexdigest() + previous_path.suffix.casefold()
                    )
                    shutil.copy2(previous_path, previous_backup)

            def restore_upgrade_selection(reason: str, *, paused: bool = False) -> bool:
                if upgrade_request is None:
                    return False
                fresh_rebuild = bool(upgrade_request.get("fresh_rebuild"))
                previous_value = str(upgrade_request.get("previous_subtitle_path") or "").strip()
                previous_path = Path(previous_value) if previous_value else None
                previous_sid_raw = upgrade_request.get("previous_embedded_sid")
                previous_sid = int(previous_sid_raw) if previous_sid_raw is not None else None
                if previous_backup is not None and previous_path is not None and previous_backup.is_file():
                    previous_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(previous_backup, previous_path)
                if not self.db.set_subtitle_ready(
                    video,
                    previous_path,
                    previous_sid,
                    origin=str(upgrade_request.get("previous_origin") or ""),
                    clear_state_keys=(
                        (upgrade_key, self._subtitle_force_rebuild_state_key(video))
                        if fresh_rebuild and not paused
                        else (upgrade_key,)
                    ),
                    **owner_kwargs,
                ):
                    return False
                if fresh_rebuild and paused:
                    # Paused (playback, energy): the rebuild goes on later, at its own
                    # priority. A rebuild rule's job keeps its reason (and the throttle
                    # marker of a sync update) and stays below new downloads and the
                    # checks the person asked for.
                    rule_rebuild = bool(upgrade_request.get("rebuild_rule"))
                    self.db.set_state(upgrade_key, json.dumps(upgrade_request, ensure_ascii=False))
                    self.db.queue_subtitle_job(
                        video,
                        media_id,
                        int(job["episode"]) if job["episode"] is not None else None,
                        delay_seconds=60,
                        priority=_upgrade_request_priority(upgrade_request, 0 if rule_rebuild else 260),
                        error=(
                            str(upgrade_request.get("requeue_reason") or reason) if rule_rebuild else reason
                        ),
                    )
                    if previous_backup is not None:
                        previous_backup.unlink(missing_ok=True)
                    return True
                self.db.record_subtitle_history(
                    video_path=video,
                    media_id=media_id,
                    episode=int(job["episode"]) if job["episode"] is not None else None,
                    source="",
                    candidate_name="",
                    candidate_path="",
                    score=None,
                    status="rejected",
                    reason=reason,
                    details={"upgrade_interrupted": True},
                )
                if previous_backup is not None:
                    previous_backup.unlink(missing_ok=True)
                return True

            def retry_rule_rebuild_later(detail: str, signature: str) -> bool:
                """A rebuild rule's run that failed for a passing reason runs again later.

                Jimaku unreachable or rate limited, a lookup cut short by a Jimaku error,
                the 20-minute timeout, a worker that did not start: the old subtitles
                stay, and so do the request and the forced rebuild. Each identical
                failure doubles the wait; the ``SUBTITLE_FAILURE_STREAK_CAP``-th ends the
                rebuild like a definitive failure (False: the caller ends it).
                """
                if upgrade_request is None or not upgrade_request.get("rebuild_rule"):
                    return False
                streak = self._count_subtitle_failure(video, signature, **owner_kwargs)
                if streak >= SUBTITLE_FAILURE_STREAK_CAP:
                    self.logger.info(
                        "SKIP step=subtitle.rebuild reason=retries_exhausted video=%s signature=%s streak=%s",
                        video.name,
                        signature,
                        streak,
                    )
                    return False
                previous_value = str(upgrade_request.get("previous_subtitle_path") or "").strip()
                previous_path = Path(previous_value) if previous_value else None
                if previous_backup is not None and previous_path is not None and previous_backup.is_file():
                    previous_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(previous_backup, previous_path)
                delay = _subtitle_failure_backoff_seconds(
                    poll_minutes=self.config.agent.subtitle_poll_minutes, streak=streak
                )
                self.db.reschedule_subtitle_job(
                    video,
                    str(upgrade_request.get("requeue_reason") or detail),
                    delay,
                    count_attempt=True,
                    keep_priority=True,
                    **owner_kwargs,
                )
                if previous_backup is not None:
                    previous_backup.unlink(missing_ok=True)
                self.logger.info(
                    "RETRY step=subtitle.rebuild reason=transient video=%s signature=%s streak=%s "
                    "delay_s=%s detail=%r",
                    video.name,
                    signature,
                    streak,
                    int(delay),
                    str(detail)[-300:],
                )
                return True

            anime = self.db.get_anime(media_id) if media_id is not None else None
            # No AniList title (an OVA of the person's own folder, an unmatched file),
            # or no episode of a multi-episode title: retrying every half hour finds
            # nothing new. A matched movie or one-episode special has no episode either.
            unidentified = media_id is None or (
                episode is None
                and anime is not None
                and int(anime.episodes or 0) > 1
                and str(anime.format or "").upper() != "MOVIE"
            )
            if anime is not None:
                command.extend(
                    [
                        "--media-id",
                        str(anime.media_id),
                        "--media-title",
                        anime.title,
                        "--media-titles-json",
                        json.dumps(anime.titles, ensure_ascii=False),
                        "--media-synonyms-json",
                        json.dumps(anime.synonyms, ensure_ascii=False),
                    ]
                )
                if anime.episodes is not None:
                    command.extend(["--media-episodes", str(anime.episodes)])
                if anime.format:
                    command.extend(["--media-format", anime.format])
                if anime.media_status:
                    command.extend(["--media-status", anime.media_status])
                # The manager already has an authoritative AniList ID. Avoid a
                # second GraphQL lookup here; Jimaku can search by ID directly.
                command.append("--skip-airing-lookup")
            if job["episode"] is not None:
                command.extend(["--episode-hint", str(int(job["episode"]))])
                double_row = self.db.episode_by_path(video)
                if (
                    double_row is not None
                    and double_row.media_episode_end is not None
                    and double_row.media_episode == int(job["episode"])
                ):
                    # Two episodes in one file: subtitles per episode, joined (audit 17А).
                    command.extend(["--episode-end-hint", str(int(double_row.media_episode_end))])
            manual_key = self._manual_subtitle_state_key(video)
            manual_value = self.db.get_state(manual_key, "").strip()
            manual_path = Path(manual_value) if manual_value else None
            if manual_path is not None and manual_path.is_file():
                command.extend(["--sub", str(manual_path)])
            debug_force_key = self._debug_force_subtitle_state_key(video)
            debug_force = self.db.get_state(debug_force_key, "").strip() == "1"
            force_rebuild_key = self._subtitle_force_rebuild_state_key(video)
            force_rebuild = self.db.get_state(force_rebuild_key, "").strip() == "1"
            candidate_fingerprint_key = self._subtitle_candidate_fingerprint_state_key(video)
            if not force_rebuild and self._legacy_missing_selection_needs_force_rebuild(video):
                if not self.db.delete_state_if_subtitle_job_owned(
                    candidate_fingerprint_key, video_path=video, **owner_kwargs
                ):
                    continue
                if not self.db.set_state_if_subtitle_job_owned(
                    force_rebuild_key, "1", video_path=video, **owner_kwargs
                ):
                    continue
                force_rebuild = True
                self.logger.info(
                    "REPAIR step=subtitle.force_rebuild_legacy media_id=%s episode=%s "
                    "video=%s reason=missing_previous_prepared_selection",
                    job["media_id"],
                    job["episode"],
                    video.name,
                )
            candidate_outcome_key = candidate_fingerprint_key + ":outcome"
            command.extend(
                ["--previous-candidate-outcome", self.db.get_state(candidate_outcome_key, "unknown")]
            )
            previous_candidate_fingerprint = self.db.get_state(candidate_fingerprint_key, "").strip()
            if (
                previous_candidate_fingerprint
                and manual_path is None
                and not debug_force
                and not force_rebuild
                and upgrade_request is None
            ):
                command.extend(["--previous-candidate-fingerprint", previous_candidate_fingerprint])
            if debug_force or force_rebuild:
                if "--force-search" not in command:
                    command.append("--force-search")
                if "--resync" not in command:
                    command.append("--resync")
                if force_rebuild:
                    self.logger.info(
                        "REPAIR step=subtitle.force_rebuild media_id=%s episode=%s video=%s reason=invalidated_selection",
                        job["media_id"],
                        job["episode"],
                        video.name,
                    )
            prepared_bitmap: Path | None = None
            ocr_fallback_path: Path | None = None
            existing_episode = self.db.episode_by_path(video)
            if (
                manual_path is None
                and not debug_force
                and upgrade_request is None
                and existing_episode is not None
                and existing_episode.state in {"waiting_text_subtitles", "couldnt_sync"}
                and str(existing_episode.subtitle_origin or "").casefold() == "ocr"
                and existing_episode.subtitle_path is not None
                and existing_episode.subtitle_path.is_file()
                and not self.config.matching.ocr_counts_as_ready
            ):
                # OCR is retained as a playback fallback, but background retries
                # must search only for verified text. Otherwise the final OCR
                # cache would be accepted again on every poll and bounce the
                # episode back to Ready.
                ocr_fallback_path = existing_episode.subtitle_path
                if "--force-search" not in command:
                    command.append("--force-search")
                command.append("--text-only")
                self.logger.info(
                    "CACHE step=subtitle.ocr_fallback_text_retry media_id=%s episode=%s video=%s path=%s",
                    job["media_id"],
                    job["episode"],
                    video.name,
                    ocr_fallback_path,
                )
            elif manual_path is None and not debug_force and upgrade_request is None:
                prepared_bitmap = self._prepared_bitmap_retry_path(existing_episode)
                if prepared_bitmap is not None:
                    command.extend(["--prepared-bitmap-sub", str(prepared_bitmap)])
                    self.logger.info(
                        "CACHE step=subtitle.prepare_bitmap_source media_id=%s episode=%s video=%s path=%s",
                        job["media_id"],
                        job["episode"],
                        video.name,
                        prepared_bitmap,
                    )
            command.append(str(video))
            env = dict(os.environ)
            env["PUDGE_CONFIG"] = str(self.config.config_path)
            # Distinguish launch-agent preparation from a user invoking
            # ``pudge --prepare-only`` manually. Manual preparation must
            # preempt this background worker just like normal playback.
            env["PUDGE_BACKGROUND_PREPARE"] = "1"
            if not wait_for_slot and not preferred_paths:
                # Queue work nobody is waiting for runs at low CPU priority;
                # a manual retry or fresh-subtitle request keeps normal priority.
                mark_background(env)
            attempt_id = f"g{job_generation}-{job_owner_token[:12] or 'owner'}"
            video_digest = hashlib.sha1(str(video).encode("utf-8")).hexdigest()
            status_path = (
                self.config.paths.cache_dir / "subtitle-job-status" / (f"{video_digest}-{attempt_id}.json")
            )
            status_path.unlink(missing_ok=True)
            env["PUDGE_SUBTITLE_JOB_STATUS"] = str(status_path)
            canonical_debug_paths = subtitle_debug_paths(self.config.paths.cache_dir, video)
            debug_paths = {
                "root": canonical_debug_paths["root"],
                "trace": canonical_debug_paths["root"] / f"{video_digest}-{attempt_id}.jsonl",
                "result": canonical_debug_paths["root"] / f"{video_digest}-{attempt_id}.json",
            }
            debug_paths["trace"].unlink(missing_ok=True)
            debug_paths["result"].unlink(missing_ok=True)
            env["PUDGE_SUBTITLE_JOB_TRACE"] = str(debug_paths["trace"])
            stdout_path = (
                self.config.paths.cache_dir / "subtitle-job-status" / f"{video_digest}-{attempt_id}.stdout"
            )
            stderr_path = (
                self.config.paths.cache_dir / "subtitle-job-status" / f"{video_digest}-{attempt_id}.stderr"
            )
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_path.unlink(missing_ok=True)
            stderr_path.unlink(missing_ok=True)
            prepare_debug_started_at = time.time()
            energy_probe = EnergyDiagnosticsMonitor(interval_seconds=30.0, logger=None)
            append_debug_trace(
                debug_paths["trace"],
                {
                    "kind": "manager_stage",
                    "stage": "spawn",
                    "updated_at": prepare_debug_started_at,
                    "details": {
                        "media_id": media_id,
                        "episode": job["episode"],
                        "video": str(video),
                    },
                },
            )
            heavy_lease = self.work_scheduler.acquire_heavy(
                "subtitle-prepare",
                blocking=wait_for_slot,
                foreground_sensitive=True,
                wait_for_foreground=wait_for_slot,
                priority=WorkPriority.USER if wait_for_slot else WorkPriority.BACKGROUND,
            )
            if heavy_lease is None:
                self.db.reschedule_subtitle_job(video, err("jobs.heavyWorkBusy"), 60, **owner_kwargs)
                requeue_unstarted(job_index + 1, "heavy_busy")
                break
            # Several jobs share one 25-minute claim and each child may run for
            # 20 minutes, so a later job's lease could expire and another worker
            # claim it before its turn here. Renew it right before the child
            # starts, or leave the job to its new owner.
            if not self.db.update_subtitle_job_stage(video, "discovering", **owner_kwargs):
                heavy_lease.release()
                self.logger.info(
                    "SKIP step=subtitle.prepare_stale_owner media_id=%s episode=%s video=%s generation=%s owner=%s phase=start",
                    media_id,
                    episode,
                    video.name,
                    job_generation,
                    job_owner_token[:12],
                )
                continue
            try:
                with timed_step(
                    self.logger,
                    "subtitle.prepare",
                    media_id=job["media_id"],
                    episode=job["episode"],
                    video=video.name,
                ):
                    stdout_handle = stdout_path.open("w", encoding="utf-8")
                    stderr_handle = None
                    process = None
                    try:
                        stderr_handle = stderr_path.open("w", encoding="utf-8")
                        # Its own process group: a cancel stops ffmpeg, alass and the
                        # candidate pool it started, not only the worker.
                        process = subprocess.Popen(
                            command,
                            text=True,
                            stdout=stdout_handle,
                            stderr=stderr_handle,
                            env=env,
                            start_new_session=True,
                        )
                        deadline = time.monotonic() + 20 * 60
                        cancelled_for_policy: str | None = None
                        memory_guard_triggered = False
                        memory_guard_limit_mb = 4096.0
                        next_memory_check = time.monotonic() + 5.0
                        next_owner_check = time.monotonic() + 15.0
                        lost_ownership = False
                        last_report_stage = ""
                        while process.poll() is None:
                            report = read_job_report(status_path)
                            if report is not None:
                                report_stage = str(report.get("stage") or "")
                                if report_stage and report_stage != last_report_stage:
                                    details = report.get("details")
                                    if not self.db.update_subtitle_job_stage(
                                        video,
                                        report_stage,
                                        progress=details if isinstance(details, dict) else {},
                                        **owner_kwargs,
                                    ):
                                        lost_ownership = True
                                    try:
                                        sample = energy_probe.sample()
                                    except Exception:
                                        sample = {}
                                    append_debug_trace(
                                        debug_paths["trace"],
                                        {
                                            "kind": "energy",
                                            "stage": report_stage,
                                            "updated_at": time.time(),
                                            "sample": sample,
                                        },
                                    )
                                    last_report_stage = report_stage
                            now_monotonic = time.monotonic()
                            if (
                                prepared_bitmap is not None
                                and self.config.matching.ocr_image_subtitles
                                and now_monotonic >= next_memory_check
                            ):
                                next_memory_check = now_monotonic + 5.0
                                rss_mb = _process_rss_mb(process.pid)
                                if rss_mb is not None and rss_mb > memory_guard_limit_mb:
                                    memory_guard_triggered = True
                                    self.logger.warning(
                                        "STOP step=subtitle.prepare reason=ocr_memory_guard "
                                        "media_id=%s episode=%s video=%s rss_mb=%.1f limit_mb=%.1f",
                                        job["media_id"],
                                        job["episode"],
                                        video.name,
                                        rss_mb,
                                        memory_guard_limit_mb,
                                    )
                                    _stop_prepare_child(process)
                                    break
                            if not lost_ownership and now_monotonic >= next_owner_check:
                                next_owner_check = now_monotonic + 15.0
                                lost_ownership = not job_still_owned()
                            if lost_ownership:
                                # Another worker took the job over (its lease ran out, a
                                # recovery): this run's result would be thrown away.
                                self.logger.info(
                                    "STOP step=subtitle.prepare reason=lost_ownership media_id=%s episode=%s "
                                    "video=%s generation=%s owner=%s",
                                    media_id,
                                    episode,
                                    video.name,
                                    job_generation,
                                    job_owner_token[:12],
                                )
                                _stop_prepare_child(process)
                                break
                            policy_reason = self.work_scheduler.background_wait_reason(priority=job_priority)
                            if policy_reason is not None:
                                cancelled_for_policy = policy_reason
                                _stop_prepare_child(process)
                                break
                            if time.monotonic() >= deadline:
                                _signal_prepare_child(process, signal.SIGKILL)
                                process.wait(timeout=5)
                                raise subprocess.TimeoutExpired(command, 20 * 60)
                            # Quick feedback while the worker starts, then one wake-up a second
                            # (status file, foreground marker and power policy per tick).
                            time.sleep(0.25 if time.monotonic() - (deadline - 20 * 60) < 5 else 1.0)
                        if process.poll() is None:
                            process.wait(timeout=5)
                    finally:
                        # The 20-minute timeout raised inside the loop before the handles
                        # were closed, and any other error there (a locked database while
                        # saving the stage) left the child running with nobody waiting.
                        # The agent's SIGTERM at quit ends here as SystemExit: the worker's
                        # whole group gets SIGTERM (its cleanup runs), SIGKILL after 5 s.
                        if process is not None and process.poll() is None:
                            if isinstance(sys.exc_info()[1], (SystemExit, KeyboardInterrupt)):
                                try:
                                    _stop_prepare_child(process)
                                except subprocess.TimeoutExpired:
                                    pass
                            else:
                                _signal_prepare_child(process, signal.SIGKILL)
                                try:
                                    process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    pass
                        stdout_handle.close()
                        if stderr_handle is not None:
                            stderr_handle.close()
                    # Real subprocesses write directly to files, avoiding PIPE backpressure.
                    # Some lightweight test/process adapters still return captured text from
                    # communicate(); keep that as a compatibility fallback only.
                    fallback_stdout = ""
                    fallback_stderr = ""
                    communicate = getattr(process, "communicate", None)
                    if callable(communicate):
                        try:
                            communicated = communicate()
                            if isinstance(communicated, tuple) and len(communicated) == 2:
                                fallback_stdout = str(communicated[0] or "")
                                fallback_stderr = str(communicated[1] or "")
                        except Exception:
                            pass
                    stdout = (
                        stdout_path.read_text(encoding="utf-8", errors="replace")
                        if stdout_path.is_file()
                        else ""
                    )
                    stderr = (
                        stderr_path.read_text(encoding="utf-8", errors="replace")
                        if stderr_path.is_file()
                        else ""
                    )
                    if not stdout:
                        stdout = fallback_stdout
                    if not stderr:
                        stderr = fallback_stderr
                    completed = subprocess.CompletedProcess(
                        command,
                        process.returncode if process.returncode is not None else 1,
                        stdout,
                        stderr,
                    )
                    final_report = read_job_report(status_path)
                    if final_report is not None:
                        final_stage = str(final_report.get("stage") or "")
                        if final_stage and final_stage != last_report_stage:
                            details = final_report.get("details")
                            self.db.update_subtitle_job_stage(
                                video,
                                final_stage,
                                progress=details if isinstance(details, dict) else {},
                                **owner_kwargs,
                            )
                    if memory_guard_triggered:
                        self.db.postpone_bitmap_ocr_job(
                            video,
                            err("jobs.bitmapOcrMemory"),
                            30 * 60,
                            **owner_kwargs,
                        )
                        self.logger.info(
                            "RETRY step=subtitle.prepare reason=ocr_memory_guard "
                            "media_id=%s episode=%s video=%s delay_s=%s",
                            job["media_id"],
                            job["episode"],
                            video.name,
                            30 * 60,
                        )
                        requeue_unstarted(job_index + 1, "heavy_busy")
                        break
                    if cancelled_for_policy is not None:
                        wait_messages = {
                            "energy_saving": err("jobs.pausedEnergySaving"),
                            "thermal": err("jobs.pausedThermal"),
                            "foreground": err("jobs.foregroundPlayback"),
                        }
                        wait_message = wait_messages.get(
                            cancelled_for_policy,
                            err("jobs.paused", reason=cancelled_for_policy),
                        )
                        if not restore_upgrade_selection(wait_message, paused=True):
                            # Paused, not judged: the episode keeps its fallback (OCR,
                            # raw unsynced, the bitmap source) and the job its count.
                            self.db.reschedule_subtitle_job(video, wait_message, 60, **owner_kwargs)
                        self.logger.info(
                            "RETRY step=subtitle.prepare reason=%s media_id=%s episode=%s video=%s delay_s=60",
                            cancelled_for_policy,
                            job["media_id"],
                            job["episode"],
                            video.name,
                        )
                        requeue_unstarted(job_index + 1, cancelled_for_policy)
                        break
            except (OSError, subprocess.TimeoutExpired) as exc:
                unfinished_signature = (
                    "timeout"
                    if isinstance(exc, subprocess.TimeoutExpired)
                    else _subtitle_failure_signature(f"{type(exc).__name__}: {exc}")
                )
                if retry_rule_rebuild_later(str(exc), unfinished_signature):
                    continue
                if not restore_upgrade_selection(str(exc)):
                    self._retry_unfinished_subtitle_run(
                        video,
                        str(exc),
                        signature=unfinished_signature,
                        **owner_kwargs,
                    )
                continue
            finally:
                heavy_lease.release()
                status_path.unlink(missing_ok=True)
                stdout_path.unlink(missing_ok=True)
                stderr_path.unlink(missing_ok=True)
            if not job_still_owned():
                self.logger.info(
                    "SKIP step=subtitle.prepare_stale_owner media_id=%s episode=%s video=%s generation=%s owner=%s",
                    media_id,
                    episode,
                    video.name,
                    job_generation,
                    job_owner_token[:12],
                )
                continue
            subtitle: Path | None = None
            raw_unsynced_subtitle: Path | None = None
            embedded_subtitle_id: int | None = None
            prepare_status = ""
            candidate_fingerprint = ""
            part_boundary: float | None = None
            subtitle_meta: dict[str, Any] = {}
            for line in completed.stdout.splitlines():
                if line.startswith("PREPARED_PART_BOUNDARY="):
                    try:
                        part_boundary = float(line.split("=", 1)[1].strip())
                    except ValueError:
                        part_boundary = None
                elif line.startswith("PREPARED_SUBTITLE="):
                    value = line.split("=", 1)[1].strip()
                    subtitle = Path(value) if value else None
                elif line.startswith("PREPARED_RAW_SUBTITLE="):
                    value = line.split("=", 1)[1].strip()
                    raw_unsynced_subtitle = Path(value) if value else None
                elif line.startswith("PREPARED_EMBEDDED_SID="):
                    value = line.split("=", 1)[1].strip()
                    embedded_subtitle_id = int(value) if value else None
                elif line.startswith("PREPARE_STATUS="):
                    prepare_status = line.split("=", 1)[1].strip()
                elif line.startswith("SUBTITLE_CANDIDATE_FINGERPRINT="):
                    candidate_fingerprint = line.split("=", 1)[1].strip()
                elif line.startswith("PREPARED_SUBTITLE_META="):
                    raw_meta = line.split("=", 1)[1].strip()
                    try:
                        parsed_meta = json.loads(raw_meta)
                    except (TypeError, ValueError, json.JSONDecodeError):
                        parsed_meta = {}
                    subtitle_meta = parsed_meta if isinstance(parsed_meta, dict) else {}
            meta_final_path = str(subtitle_meta.get("final_path") or "").strip()
            if meta_final_path:
                prepared_from_meta = Path(meta_final_path).expanduser()
                if prepared_from_meta.is_file():
                    if subtitle is None or subtitle != prepared_from_meta:
                        self.logger.info(
                            "REPAIR step=subtitle.prepare_result_path media_id=%s "
                            "episode=%s video=%s old=%r new=%r source=meta_final_path",
                            job["media_id"],
                            job["episode"],
                            video.name,
                            str(subtitle) if subtitle is not None else "",
                            str(prepared_from_meta),
                        )
                    subtitle = prepared_from_meta
            write_prepare_debug_result(
                debug_paths["result"],
                command=command,
                returncode=int(completed.returncode or 0),
                started_at=prepare_debug_started_at,
                finished_at=time.time(),
                stdout=completed.stdout,
                stderr=completed.stderr,
                prepare_status=prepare_status,
                subtitle_meta=subtitle_meta,
            )
            if job_still_owned():
                try:
                    if debug_paths["trace"].is_file():
                        canonical_debug_paths["trace"].parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(debug_paths["trace"], canonical_debug_paths["trace"])
                    if debug_paths["result"].is_file():
                        shutil.copy2(debug_paths["result"], canonical_debug_paths["result"])
                except OSError:
                    pass
                try:
                    prune_subtitle_debug(self.config.paths.cache_dir)
                except Exception as exc:  # noqa: BLE001 - trace cleanup never blocks a prepare
                    self.logger.warning("SKIP step=subtitle_debug.prune reason=%r", exc)
            else:
                self.logger.info(
                    "SKIP step=subtitle.prepare_stale_owner media_id=%s episode=%s video=%s generation=%s owner=%s phase=publish",
                    media_id,
                    episode,
                    video.name,
                    job_generation,
                    job_owner_token[:12],
                )
                continue
            if part_boundary is not None and prepare_status == "ready":
                self.db.set_part_boundary(video, part_boundary)
                self.logger.info(
                    "RESULT step=subtitle.double_episode_boundary media_id=%s episode=%s video=%s boundary=%.1f",
                    job["media_id"],
                    job["episode"],
                    video.name,
                    part_boundary,
                )
            stdout_tail = completed.stdout.strip()[-3000:]
            stderr_tail = completed.stderr.strip()[-3000:]
            self.db.delete_state_if_subtitle_job_owned(
                self._manual_subtitle_state_key(video),
                video_path=video,
                **owner_kwargs,
            )
            if debug_force:
                self.db.delete_state_if_subtitle_job_owned(
                    debug_force_key,
                    video_path=video,
                    **owner_kwargs,
                )
            self.logger.info(
                "RESULT step=subtitle.prepare media_id=%s episode=%s video=%s returncode=%s prepared=%s stdout_tail=%r stderr_tail=%r",
                job["media_id"],
                job["episode"],
                video.name,
                completed.returncode,
                str(subtitle) if subtitle else "",
                stdout_tail,
                stderr_tail,
            )
            image_subtitle = bool(
                subtitle is not None and subtitle.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS
            )
            candidate_set_changed = bool(
                previous_candidate_fingerprint
                and candidate_fingerprint
                and candidate_fingerprint != previous_candidate_fingerprint
            )
            if candidate_fingerprint:
                if not self.db.set_state_if_subtitle_job_owned(
                    candidate_fingerprint_key,
                    candidate_fingerprint,
                    video_path=video,
                    **owner_kwargs,
                ):
                    self.logger.info(
                        "SKIP step=subtitle.prepare_stale_owner media_id=%s episode=%s video=%s generation=%s owner=%s phase=fingerprint",
                        media_id,
                        episode,
                        video.name,
                        job_generation,
                        job_owner_token[:12],
                    )
                    continue
            if candidate_set_changed:
                self.db.reset_subtitle_job_attempts(video, **owner_kwargs)
                self.logger.info(
                    "RESET step=subtitle.retry_backoff media_id=%s episode=%s video=%s reason=candidate_set_changed old=%s new=%s",
                    job["media_id"],
                    job["episode"],
                    video.name,
                    previous_candidate_fingerprint[:12],
                    candidate_fingerprint[:12],
                )
            prepare_detail = "\n".join(
                part for part in (completed.stdout.strip()[-5000:], completed.stderr.strip()[-1500:]) if part
            )
            # A fresh rebuild whose Jimaku lookup stopped on an error has only part of the
            # candidates: its result is no verdict on the episode (and ends no failure run).
            rebuild_cut_short = bool(
                upgrade_request is not None
                and upgrade_request.get("fresh_rebuild")
                and _jimaku_lookup_incomplete(prepare_detail)
            )
            failure_streak_key = self._subtitle_failure_streak_state_key(video)
            if (
                not rebuild_cut_short
                and (
                    candidate_set_changed
                    or prepare_status
                    in {"ready", "couldnt_sync", "waiting_text_subtitles", "waiting_verification"}
                )
                and self.db.get_state(failure_streak_key, "")
            ):
                # Another verdict or other candidates: the run of identical failures ended.
                self.db.delete_state_if_subtitle_job_owned(
                    failure_streak_key, video_path=video, **owner_kwargs
                )
            from .subtitles.verification import verification_failure

            verification = verification_failure(subtitle_meta)
            candidate_outcome = (
                "verification"
                if verification and verification["retryable"]
                else "terminal"
                if prepare_status
                in {
                    "couldnt_sync",
                    "waiting_subtitles",
                    "waiting_unchanged_candidates",
                    "waiting_verification",
                }
                else "unknown"
            )
            if not self.db.set_state_if_subtitle_job_owned(
                candidate_outcome_key, candidate_outcome, video_path=video, **owner_kwargs
            ):
                continue
            # Speech-verification failures in a row. The job's attempts also count every
            # earlier "no subtitles yet" retry of an airing episode, which made the first
            # STT failure of such an episode "Action required" at once.
            verification_failures_key = candidate_fingerprint_key + ":verification_failures"
            verification_failures = self.db.get_state(verification_failures_key, "").strip()
            if prepare_status != "waiting_verification" and verification_failures:
                self.db.delete_state_if_subtitle_job_owned(
                    verification_failures_key, video_path=video, **owner_kwargs
                )
            # An upgrade check ends in the upgrade branch below, which restores
            # the previous Ready subtitle unless a better one was accepted.
            # Handled here, a failed speech verification left the episode
            # waiting with the upgrade marker kept, and the restore pass
            # re-queued the check (and its STT run) on every tick.
            if prepare_status == "waiting_verification" and upgrade_request is None:
                reason = str((verification or {}).get("reason") or "stt_reference_unavailable")
                failures = (int(verification_failures) if verification_failures.isdigit() else 0) + 1
                exhausted = failures >= 3
                if (verification and not verification["retryable"]) or exhausted:
                    self.db.set_state_if_subtitle_job_owned(
                        candidate_outcome_key, "terminal", video_path=video, **owner_kwargs
                    )
                    # A retry the user starts from "Action required" gets three tries again.
                    self.db.delete_state_if_subtitle_job_owned(
                        verification_failures_key, video_path=video, **owner_kwargs
                    )
                    self.db.mark_subtitle_job_needs_action(
                        video, reason, "configure_speech_verification", **owner_kwargs
                    )
                else:
                    # Count operational verification attempts too: deferral
                    # without counting made a timeout retry forever.
                    self.db.set_state_if_subtitle_job_owned(
                        verification_failures_key, str(failures), video_path=video, **owner_kwargs
                    )
                    self.db.postpone_subtitle_job(
                        video,
                        reason,
                        max(30.0, self.config.agent.subtitle_poll_minutes * 60.0),
                        **owner_kwargs,
                    )
                self.logger.info(
                    "RETRY step=subtitle.verification video=%s reason=%s retryable=%s exhausted=%s",
                    video.name,
                    reason,
                    (verification or {}).get("retryable", True),
                    exhausted,
                )
                continue
            if prepare_status == "waiting_unchanged_candidates" and upgrade_request is None:
                # An unchanged provider candidate set is not a reason to demote
                # a still-usable prepared selection.  Previously defer_subtitle_job()
                # forced Ready -> Waiting, while the next reconcile pass restored
                # the same history SRT -> Ready, producing a visible Preparation /
                # Ready loop.  Explicit debug-force checks still keep their
                # observational retry semantics.
                if not debug_force:
                    current = self.db.episode_by_path(video)
                    current_external: Path | None = None
                    if current is not None and current.subtitle_path is not None:
                        try:
                            if (
                                current.subtitle_path.suffix.casefold() in TEXT_SUBTITLE_EXTENSIONS
                                and current.subtitle_path.is_file()
                                and current.subtitle_path.stat().st_size > 0
                                and str(current.subtitle_origin or "").casefold()
                                not in {"ocr", "bitmap", "raw_unsynced"}
                            ):
                                current_external = current.subtitle_path.resolve()
                        except OSError:
                            current_external = None
                    if (
                        current is not None
                        and current.state in {"ready", "watched", "waiting_subtitles"}
                        and (current_external is not None or current.embedded_subtitle_id is not None)
                    ):
                        if not self.db.set_subtitle_ready(
                            video,
                            current_external,
                            current.embedded_subtitle_id if current_external is None else None,
                            origin=str(current.subtitle_origin or ""),
                            **owner_kwargs,
                        ):
                            self.logger.info(
                                "SKIP step=subtitle.prepare_stale_owner media_id=%s episode=%s video=%s generation=%s owner=%s phase=unchanged_preserve",
                                media_id,
                                episode,
                                video.name,
                                job_generation,
                                job_owner_token[:12],
                            )
                            continue
                        self.logger.info(
                            "RESOLVE step=subtitle.unchanged_preserve media_id=%s episode=%s "
                            "video=%s source=current_selection external=%s embedded_sid=%s",
                            job["media_id"],
                            job["episode"],
                            video.name,
                            bool(current_external),
                            current.embedded_subtitle_id,
                        )
                        ready += 1
                        continue
                    recovered = self._recover_selected_text_subtitle_from_history(
                        video, media_id=media_id, episode=episode, **owner_kwargs
                    )
                    if recovered is not None:
                        self.logger.info(
                            "RESOLVE step=subtitle.unchanged_preserve media_id=%s episode=%s "
                            "video=%s source=history",
                            job["media_id"],
                            job["episode"],
                            video.name,
                        )
                        ready += 1
                        continue
                delay = _subtitle_source_retry_delay_seconds(
                    poll_minutes=self.config.agent.subtitle_poll_minutes,
                    media_status=(anime.media_status if anime is not None else None),
                )
                if unidentified:
                    # Nothing new for a file without a title or episode: once a day.
                    delay = max(delay, 24 * 3600.0)
                self.db.defer_subtitle_job(
                    video,
                    err("jobs.candidatesUnchanged"),
                    delay,
                    **owner_kwargs,
                )
                self.logger.info(
                    "RETRY step=subtitle.prepare media_id=%s episode=%s video=%s attempts=%s delay_s=%s reason=candidate_set_unchanged",
                    job["media_id"],
                    job["episode"],
                    video.name,
                    int(job["attempts"] or 0),
                    delay,
                )
                continue
            if prepare_status == "couldnt_sync" and upgrade_request is None:
                detail = err("jobs.syncFailed")
                delay = _subtitle_source_retry_delay_seconds(
                    poll_minutes=self.config.agent.subtitle_poll_minutes,
                    media_status=(anime.media_status if anime is not None else None),
                    minimum_seconds=6 * 3600.0,
                )
                self.db.defer_subtitle_job(video, detail, delay, **owner_kwargs)
                if raw_unsynced_subtitle is not None and raw_unsynced_subtitle.is_file():
                    self.db.set_couldnt_sync_subtitle(
                        video,
                        raw_unsynced_subtitle,
                        origin=str(subtitle_meta.get("source") or "raw_unsynced"),
                        **owner_kwargs,
                    )
                    self.db.record_subtitle_history(
                        video_path=video,
                        media_id=media_id,
                        episode=int(job["episode"]) if job["episode"] is not None else None,
                        source=str(subtitle_meta.get("source") or "unknown"),
                        candidate_name=str(subtitle_meta.get("name") or raw_unsynced_subtitle.name),
                        candidate_path=str(raw_unsynced_subtitle),
                        score=(
                            float(subtitle_meta["filename_score"])
                            if subtitle_meta.get("filename_score") is not None
                            else None
                        ),
                        status="couldnt_sync",
                        reason=detail,
                        details=subtitle_meta,
                    )
                self.logger.info(
                    "RETRY step=subtitle.prepare media_id=%s episode=%s video=%s delay_s=%s reason=couldnt_sync raw=%s",
                    job["media_id"],
                    job["episode"],
                    video.name,
                    delay,
                    str(raw_unsynced_subtitle or ""),
                )
                continue
            if upgrade_request is not None:
                previous_value = str(upgrade_request.get("previous_subtitle_path") or "").strip()
                previous_path = Path(previous_value) if previous_value else None
                previous_sid_raw = upgrade_request.get("previous_embedded_sid")
                previous_sid = int(previous_sid_raw) if previous_sid_raw is not None else None
                old_score_raw = upgrade_request.get("previous_score")
                try:
                    old_score = float(old_score_raw) if old_score_raw is not None else None
                except (TypeError, ValueError):
                    old_score = None
                previous_quality = upgrade_request.get("previous_quality")
                if not isinstance(previous_quality, dict):
                    previous_quality = None
                candidate_quality = subtitle_meta.get("quality")
                if not isinstance(candidate_quality, dict):
                    candidate_quality = None
                new_score_raw = (
                    candidate_quality.get("score")
                    if candidate_quality is not None
                    else subtitle_meta.get("score")
                )
                try:
                    new_score = float(new_score_raw) if new_score_raw is not None else None
                except (TypeError, ValueError):
                    new_score = None
                quality_better, quality_reason = upgrade_is_better(
                    previous_quality,
                    candidate_quality,
                    minimum_gain=float(self.config.matching.subtitle_upgrade_min_score_gain),
                )
                manual_retry = bool(upgrade_request.get("manual_retry"))
                fresh_rebuild = bool(upgrade_request.get("fresh_rebuild"))
                accepted_upgrade = bool(
                    completed.returncode == 0
                    and prepare_status == "ready"
                    and not image_subtitle
                    and (subtitle is not None or embedded_subtitle_id is not None)
                    and (fresh_rebuild or (new_score is not None and (quality_better or manual_retry)))
                    # A fresh rebuild never takes a result from a lookup cut short.
                    and not rebuild_cut_short
                )
                rebuild_transient = bool(
                    not accepted_upgrade
                    and fresh_rebuild
                    and (rebuild_cut_short or _subtitle_retry_is_network_error(prepare_detail))
                )
                if (
                    rebuild_transient
                    and job_still_owned()
                    and retry_rule_rebuild_later(
                        prepare_detail, _subtitle_failure_signature(prepare_detail, prepare_status)
                    )
                ):
                    continue
                if accepted_upgrade:
                    if not self.db.set_subtitle_ready(
                        video,
                        subtitle,
                        embedded_subtitle_id,
                        origin=(
                            "ocr"
                            if bool(subtitle_meta.get("generated_by_ocr"))
                            else str(
                                subtitle_meta.get("source")
                                or ("embedded" if embedded_subtitle_id is not None else "external")
                            )
                        ),
                        clear_state_keys=(
                            self._subtitle_force_rebuild_state_key(video),
                            upgrade_key,
                        ),
                        **owner_kwargs,
                    ):
                        self.logger.info(
                            "SKIP step=subtitle.prepare_stale_owner media_id=%s episode=%s video=%s generation=%s owner=%s phase=upgrade_commit",
                            media_id,
                            episode,
                            video.name,
                            job_generation,
                            job_owner_token[:12],
                        )
                        continue
                    self.db.record_subtitle_history(
                        video_path=video,
                        media_id=media_id,
                        episode=int(job["episode"]) if job["episode"] is not None else None,
                        source=str(subtitle_meta.get("source") or "unknown"),
                        candidate_name=str(
                            subtitle_meta.get("name") or (subtitle.name if subtitle else "embedded")
                        ),
                        candidate_path=str(subtitle_meta.get("candidate_path") or subtitle or ""),
                        score=new_score,
                        status="selected" if fresh_rebuild else "upgraded",
                        reason=err("jobs.preparationCompleted") if fresh_rebuild else quality_reason,
                        details=subtitle_meta,
                    )
                    if fresh_rebuild or new_score is None:
                        self.log(f"Subtitles ready: {video.name}")
                    else:
                        self.log(
                            f"Subtitle upgrade ready: {video.name} "
                            + (
                                f"({old_score:.1f} → {new_score:.1f})"
                                if old_score is not None
                                else f"(score={new_score:.1f})"
                            )
                        )
                    ready += 1
                else:
                    if (
                        previous_backup is not None
                        and previous_path is not None
                        and previous_backup.is_file()
                    ):
                        previous_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(previous_backup, previous_path)
                    if not self.db.set_subtitle_ready(
                        video,
                        previous_path,
                        previous_sid,
                        origin=str(upgrade_request.get("previous_origin") or ""),
                        # A failed rebuild keeps the old subtitles and ends; the next
                        # check must not be a forced rebuild again.
                        clear_state_keys=(
                            (upgrade_key, self._subtitle_force_rebuild_state_key(video))
                            if fresh_rebuild
                            else (upgrade_key,)
                        ),
                        **owner_kwargs,
                    ):
                        self.logger.info(
                            "SKIP step=subtitle.prepare_stale_owner media_id=%s episode=%s video=%s generation=%s owner=%s phase=upgrade_restore",
                            media_id,
                            episode,
                            video.name,
                            job_generation,
                            job_owner_token[:12],
                        )
                        continue
                    reason = quality_reason or err("jobs.noBetterCandidate")
                    if completed.returncode != 0:
                        reason = completed.stderr.strip() or completed.stdout.strip()[-1000:] or reason
                    self.db.record_subtitle_history(
                        video_path=video,
                        media_id=media_id,
                        episode=int(job["episode"]) if job["episode"] is not None else None,
                        source=str(subtitle_meta.get("source") or ""),
                        candidate_name=str(subtitle_meta.get("name") or ""),
                        candidate_path=str(subtitle_meta.get("candidate_path") or subtitle or ""),
                        score=new_score,
                        status="rejected",
                        reason=reason,
                        details=subtitle_meta,
                    )
                    self.logger.info(
                        "SKIP step=subtitle.upgrade media_id=%s episode=%s video=%r old_score=%s new_score=%s reason=%r",
                        media_id,
                        job["episode"],
                        str(video),
                        old_score,
                        new_score,
                        reason,
                    )
                if previous_backup is not None:
                    previous_backup.unlink(missing_ok=True)
                continue
            if prepare_status == "waiting_text_subtitles" or image_subtitle:
                # Defence in depth: an old/malformed CLI result must never make
                # a PGS/SUP path ready merely because the subprocess exited 0.
                attempts = (0 if candidate_set_changed else int(job["attempts"] or 0)) + 1
                delay = _subtitle_source_retry_delay_seconds(
                    poll_minutes=self.config.agent.subtitle_poll_minutes,
                    media_status=(anime.media_status if anime is not None else None),
                )
                if not self.config.matching.ocr_image_subtitles:
                    self.db.mark_subtitle_job_needs_action(
                        video,
                        err("jobs.imageSubtitlesOcrOff"),
                        "enable_subtitle_ocr",
                        **owner_kwargs,
                    )
                else:
                    self.db.postpone_subtitle_job(
                        video, err("jobs.waitingTextSubtitles"), delay, **owner_kwargs
                    )
                # postpone_subtitle_job uses the generic waiting state; restore
                # the more precise fallback state afterwards. An already cached
                # OCR SRT is retained as playback fallback while text-only
                # discovery continues in the background.
                if ocr_fallback_path is not None and ocr_fallback_path.is_file():
                    self._set_ocr_timing_uncertain(video, ocr_fallback_path, **owner_kwargs)
                else:
                    self.db.set_waiting_text_subtitles(video, subtitle, embedded_subtitle_id, **owner_kwargs)
                self.logger.info(
                    "RETRY step=subtitle.prepare media_id=%s episode=%s video=%s attempts=%s delay_s=%s reason=waiting_text_subtitles",
                    job["media_id"],
                    job["episode"],
                    video.name,
                    attempts,
                    delay,
                )
            elif completed.returncode == 0:
                previous = self.db.episode_by_path(video)
                was_ready = bool(previous is not None and previous.state in {"ready", "watched"})
                generated_ocr = bool(subtitle_meta.get("generated_by_ocr"))
                if generated_ocr and not self.config.matching.ocr_counts_as_ready:
                    # OCR is useful as a playback fallback/cache but does not
                    # satisfy the user's Ready policy. Keep the SRT in the
                    # timing-uncertain group, retry text-only, and never notify Ready.
                    self._set_ocr_timing_uncertain(video, subtitle, **owner_kwargs)
                    retry_delay = _subtitle_source_retry_delay_seconds(
                        poll_minutes=self.config.agent.subtitle_poll_minutes,
                        media_status=(anime.media_status if anime is not None else None),
                        minimum_seconds=6 * 3600.0,
                    )
                    self.db.postpone_subtitle_job(
                        video,
                        err("manager.ocrTimingNeedsReview"),
                        retry_delay,
                        **owner_kwargs,
                    )
                    self._set_ocr_timing_uncertain(video, subtitle, **owner_kwargs)
                    self.db.record_subtitle_history(
                        video_path=video,
                        media_id=media_id,
                        episode=int(job["episode"]) if job["episode"] is not None else None,
                        source="ocr",
                        candidate_name=str(
                            subtitle_meta.get("name") or (subtitle.name if subtitle else "ocr")
                        ),
                        candidate_path=str(subtitle_meta.get("candidate_path") or subtitle or ""),
                        score=(
                            float(subtitle_meta["score"]) if subtitle_meta.get("score") is not None else None
                        ),
                        status="ocr_fallback",
                        reason="OCR cached in the timing-uncertain group by Ready policy",
                        details=subtitle_meta,
                    )
                    self.logger.info(
                        "RESULT step=subtitle.ocr_readiness media_id=%s episode=%s video=%r ready=false retry_s=%s",
                        media_id,
                        job["episode"],
                        str(video),
                        int(retry_delay),
                    )
                    continue
                if subtitle is not None and not generated_ocr:
                    cache_final = subtitle
                    subtitle = self._persist_prepared_text_subtitle(video, subtitle)
                    if subtitle is not None and subtitle != cache_final:
                        subtitle_meta = dict(subtitle_meta)
                        subtitle_meta["cache_final_path"] = str(cache_final)
                        subtitle_meta["final_path"] = str(subtitle)
                        self.logger.info(
                            "RESULT step=subtitle.persist_ready video=%s cache=%r durable=%r",
                            video.name,
                            str(cache_final),
                            str(subtitle),
                        )
                if not self.db.set_subtitle_ready(
                    video,
                    subtitle,
                    embedded_subtitle_id,
                    origin=(
                        "ocr"
                        if generated_ocr
                        else str(
                            subtitle_meta.get("source")
                            or ("embedded" if embedded_subtitle_id is not None else "external")
                        )
                    ),
                    clear_state_keys=(self._subtitle_force_rebuild_state_key(video), failure_streak_key),
                    **owner_kwargs,
                ):
                    self.logger.info(
                        "SKIP step=subtitle.prepare_stale_owner media_id=%s episode=%s video=%s generation=%s owner=%s phase=ready_commit",
                        media_id,
                        episode,
                        video.name,
                        job_generation,
                        job_owner_token[:12],
                    )
                    continue
                self.db.record_subtitle_history(
                    video_path=video,
                    media_id=media_id,
                    episode=int(job["episode"]) if job["episode"] is not None else None,
                    source=str(
                        subtitle_meta.get("source")
                        or ("embedded" if embedded_subtitle_id is not None else "unknown")
                    ),
                    candidate_name=str(
                        subtitle_meta.get("name") or (subtitle.name if subtitle else "embedded")
                    ),
                    candidate_path=str(subtitle_meta.get("candidate_path") or subtitle or ""),
                    score=(float(subtitle_meta["score"]) if subtitle_meta.get("score") is not None else None),
                    status="selected",
                    reason=err("jobs.preparationCompleted"),
                    details=subtitle_meta,
                )
                self.log(f"Subtitles ready: {video.name}")
                if not was_ready:
                    self._notify_ready_episode(
                        video=video,
                        media_id=media_id,
                        episode=(int(job["episode"]) if job["episode"] is not None else None),
                    )
                ready += 1
            else:
                # ffsubsync writes ordinary INFO lines to stderr. Using stderr
                # alone hid the actual deterministic validation failure printed
                # to stdout and reduced a 6h backoff to the normal 10m retry.
                stdout_detail = completed.stdout.strip()[-5000:]
                stderr_detail = completed.stderr.strip()[-1500:]
                detail = "\n".join(part for part in (stdout_detail, stderr_detail) if part)
                rate_limited = _subtitle_retry_is_rate_limit(detail)
                network_backoff = _subtitle_retry_is_network_error(detail)
                # A network error or a 429 says nothing about the subtitles: the episode
                # keeps what it can play (OCR, raw unsynced, the bitmap source).
                transient = rate_limited or network_backoff
                if transient:
                    pass
                # A failed validation must never leave an older generated cache
                # path playable. This matters after validation rules become
                # stricter: a cache file can still exist while the current run
                # explicitly rejects it.
                elif ocr_fallback_path is not None and ocr_fallback_path.is_file():
                    self._set_ocr_timing_uncertain(video, ocr_fallback_path, **owner_kwargs)
                else:
                    self.db.clear_subtitle_selection(video, **owner_kwargs)
                base_attempts = 0 if candidate_set_changed else int(job["attempts"] or 0)
                attempts = base_attempts + (0 if rate_limited else 1)
                delay = _subtitle_retry_delay_seconds(
                    poll_minutes=self.config.agent.subtitle_poll_minutes,
                    attempts=attempts,
                    detail=detail,
                )
                failure_streak = 0
                failure_signature = ""
                if not transient:
                    delay = max(
                        delay,
                        _subtitle_source_retry_delay_seconds(
                            poll_minutes=self.config.agent.subtitle_poll_minutes,
                            media_status=(anime.media_status if anime is not None else None),
                        ),
                    )
                    failure_signature = _subtitle_failure_signature(detail, prepare_status)
                    failure_streak = self._count_subtitle_failure(video, failure_signature, **owner_kwargs)
                    if failure_signature != "not_found":
                        # The same error again: wait twice as long each time.
                        delay = max(
                            delay,
                            _subtitle_failure_backoff_seconds(
                                poll_minutes=self.config.agent.subtitle_poll_minutes,
                                streak=failure_streak,
                            ),
                        )
                    if unidentified and attempts >= 2:
                        # A file without a title or episode: Jimaku has nothing new for it
                        # every half hour, once a day is enough.
                        delay = max(delay, 24 * 3600.0)
                retries_exhausted = bool(
                    failure_signature != "not_found" and failure_streak >= SUBTITLE_FAILURE_STREAK_CAP
                )
                self.logger.info(
                    "RETRY step=subtitle.prepare media_id=%s episode=%s video=%s attempts=%s delay_s=%s network_backoff=%s rate_limited=%s streak=%s exhausted=%s reason=%r",
                    job["media_id"],
                    job["episode"],
                    video.name,
                    attempts,
                    delay,
                    network_backoff,
                    rate_limited,
                    failure_streak,
                    retries_exhausted,
                    detail or "Subtitles not found yet",
                )
                permission_error = any(
                    marker in detail.casefold()
                    for marker in ("permission denied", "operation not permitted", "access denied")
                )
                if permission_error:
                    self.db.mark_subtitle_job_needs_action(
                        video,
                        detail or err("jobs.folderAccessRequired"),
                        "grant_folder_access",
                        **owner_kwargs,
                    )
                elif (
                    not self.config.jimaku.api_key.strip()
                    and attempts >= 1
                    and any(
                        marker in detail.casefold()
                        for marker in (
                            "jimaku api key",
                            "jimaku key",
                            # Details written in Russian by older versions.
                            "api-ключ jimaku",
                            "jimaku не настроен",
                        )
                    )
                ):
                    self.db.mark_subtitle_job_needs_action(
                        video, detail or err("jobs.jimakuKeyRequired"), "configure_jimaku", **owner_kwargs
                    )
                elif rate_limited:
                    self.db.reschedule_subtitle_job(
                        video,
                        detail or err("jobs.jimakuRateLimited"),
                        delay,
                        keep_priority=False,
                        **owner_kwargs,
                    )
                elif network_backoff:
                    self.db.reschedule_subtitle_job(
                        video,
                        detail or err("jobs.subtitlesNotFoundYet"),
                        delay,
                        count_attempt=True,
                        **owner_kwargs,
                    )
                elif retries_exhausted:
                    self.db.mark_subtitle_job_needs_action(
                        video,
                        detail or err("jobs.subtitlesNotFoundYet"),
                        SUBTITLE_RETRY_EXHAUSTED,
                        **owner_kwargs,
                    )
                else:
                    self.db.postpone_subtitle_job(
                        video, detail or err("jobs.subtitlesNotFoundYet"), delay, **owner_kwargs
                    )
                if ocr_fallback_path is not None and ocr_fallback_path.is_file():
                    self._set_ocr_timing_uncertain(video, ocr_fallback_path, **owner_kwargs)
        return ready

    def _prune_empty_library_dirs(self) -> int:
        """Remove empty directories anywhere below the managed anime root.

        qBittorrent can remove nested content a moment after the episode row is
        cleaned up, so checking only the immediate parent once is racy. Running
        this cheap bottom-up pass on Refresh/cleanup makes empty anime folders
        disappear on the next maintenance cycle without touching watched roots
        outside the managed library.
        """
        root = self.config.library.root_dir.expanduser()
        try:
            resolved_root = root.resolve()
        except OSError:
            return 0
        if not resolved_root.is_dir():
            return 0
        try:
            candidates = sorted(
                (path for path in resolved_root.rglob("*") if path.is_dir() and not path.is_symlink()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
        except OSError:
            return 0
        removed = self._remove_leftover_torrent_files(resolved_root)
        for path in candidates:
            try:
                path.rmdir()
            except OSError:
                continue
            removed += 1
            self.logger.info("DONE step=cleanup.empty_folder path=%r", str(path))
        return removed

    _LEFTOVER_NAMES = {".anilist.id", ".DS_Store"}
    _LEFTOVER_SUFFIXES = (".torrent", ".aria2")
    _LEFTOVER_MIN_AGE_SECONDS = 6 * 3600

    def _is_download_leftover(self, path: Path) -> bool:
        name = path.name
        return (
            name in self._LEFTOVER_NAMES
            or name.lower().endswith(self._LEFTOVER_SUFFIXES)
            or ".pudge-corrupt-" in name
        )

    @staticmethod
    def _leftover_folder_owner(folder: Path) -> int | None:
        marker = folder / ".anilist.id"
        try:
            value = marker.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return int(value) if value.isdigit() and int(value) > 0 else None

    def _remove_leftover_torrent_files(self, root: Path) -> int:
        """Clear anime folders that hold nothing but download leftovers.

        After a download is removed or its episodes are cleaned up, an anime
        folder can keep only ``.torrent`` files, the ``.anilist.id`` marker,
        stale aria2 control files and ``.pudge-corrupt-*`` quarantine files.
        Such a folder is emptied (and then removed by the empty-folder pass)
        only when no video/other file remains, nothing in it was touched
        recently, and no unfinished download points into it.
        """
        try:
            active = tuple(path.expanduser().resolve() for path in self.incomplete_download_paths())
            # A magnet still fetching metadata has no content path yet; aria2
            # names its saved metadata ``<info-hash>.torrent``.
            active_hashes = {
                str(item.torrent_hash or "").casefold()
                for item in self.db.downloads()
                if not self._download_is_complete(item) and item.torrent_hash
            }
            known_media = {int(anime.media_id) for anime in self.db.anime_list()}
        except Exception:
            return 0  # unreadable downloader/library state never authorises deletion
        now = time.time()
        cleaned = 0
        try:
            folders = [path for path in root.iterdir() if path.is_dir() and not path.is_symlink()]
        except OSError:
            return 0
        for folder in folders:
            try:
                files = [path for path in folder.rglob("*") if path.is_file() or path.is_symlink()]
            except OSError:
                continue
            if not files or not all(self._is_download_leftover(path) for path in files):
                continue
            if not any(path.name.lower().endswith(".torrent") for path in files):
                continue  # leftovers of something else; not ours to judge
            # Ownership proof, not just "looks like leftovers": the folder was
            # created by Pudge for a library title (``.anilist.id`` naming a
            # known AniList id) and every .torrent is aria2's own saved
            # metadata (``<40-hex info hash>.torrent``). A user's own .torrent
            # files, unknown folders and quarantined ``.pudge-corrupt-*``
            # files (possibly the only remaining copy) are never deleted.
            if any(".pudge-corrupt-" in path.name for path in files):
                self.logger.info(
                    "SKIP step=cleanup.torrent_leftovers path=%r reason=quarantine_kept", str(folder)
                )
                continue
            if not all(
                _ARIA2_METADATA_RE.fullmatch(path.name.casefold())
                for path in files
                if path.name.lower().endswith(".torrent")
            ):
                self.logger.info(
                    "SKIP step=cleanup.torrent_leftovers path=%r reason=foreign_torrent_file", str(folder)
                )
                continue
            owner = self._leftover_folder_owner(folder)
            if owner is None or owner not in known_media:
                self.logger.info(
                    "SKIP step=cleanup.torrent_leftovers path=%r reason=ownership_unproven", str(folder)
                )
                continue
            if any(
                path.name.lower().endswith(".torrent") and path.stem.casefold() in active_hashes
                for path in files
            ):
                self.logger.info(
                    "SKIP step=cleanup.torrent_leftovers path=%r reason=active_hash", str(folder)
                )
                continue
            if any(folder == item or folder in item.parents or item in folder.parents for item in active):
                self.logger.info(
                    "SKIP step=cleanup.torrent_leftovers path=%r reason=active_download", str(folder)
                )
                continue
            try:
                newest = max(path.lstat().st_mtime for path in files)
            except OSError:
                continue
            if now - newest < self._LEFTOVER_MIN_AGE_SECONDS:
                continue
            for path in files:
                try:
                    path.unlink()
                except OSError:
                    pass
            cleaned += 1
            self.logger.info("DONE step=cleanup.torrent_leftovers path=%r files=%s", str(folder), len(files))
        return cleaned

    def _remove_empty_episode_parent(self, video: Path) -> bool:
        """Remove only the immediate anime folder when cleanup left it empty."""
        parent = video.expanduser().parent
        allowed_roots = [
            self.config.library.root_dir.expanduser(),
            *[path.expanduser() for path in self.config.paths.download_dirs],
        ]
        try:
            resolved_parent = parent.resolve()
            roots = [root.resolve() for root in allowed_roots if root.exists()]
        except OSError:
            return False
        if any(resolved_parent == root for root in roots):
            return False
        if not any(root in resolved_parent.parents for root in roots):
            return False
        try:
            if not parent.is_dir() or any(parent.iterdir()):
                return False
            parent.rmdir()
            self.logger.info("DONE step=cleanup.empty_folder path=%r", str(parent))
            return True
        except OSError as exc:
            self.logger.info("SKIP step=cleanup.empty_folder path=%r error=%r", str(parent), exc)
            return False

    def _library_folder_owner(self, video: Path) -> int | None:
        """AniList id of the Pudge anime folder holding ``video`` (its ``.anilist.id``).

        Pudge writes the marker into the folder it downloads a title into, so
        it proves that a file without a torrent record (the torrent was
        removed from the client, aria2 lost its session) still sits in Pudge's
        own library folder. Files directly in the library root have no owner.
        """
        try:
            root = self.config.library.root_dir.expanduser().resolve()
            current = video.expanduser().resolve(strict=False).parent
        except OSError:
            return None
        while current != root and root in current.parents:
            marker = current / ".anilist.id"
            if marker.is_file():
                try:
                    value = marker.read_text(encoding="utf-8").strip()
                except OSError:
                    return None
                return int(value) if value.isdigit() and int(value) > 0 else None
            current = current.parent
        return None

    def _inside_library_root(self, video: Path) -> bool:
        try:
            root = self.config.library.root_dir.expanduser().resolve()
            return root in video.expanduser().resolve(strict=False).parents
        except OSError:
            return False

    def _downloaded_row_owned(self, row: Any) -> bool:
        """A hash-less row Pudge recorded as downloaded, and its file is in the library root.

        Earlier versions also set ``downloaded_at`` for a video dropped on the
        window (the user's own file, e.g. in ~/Downloads): that alone is no proof.
        """
        if "downloaded_at" not in row.keys() or float(row["downloaded_at"] or 0) <= 0:
            return False
        return self._inside_library_root(Path(str(row["video_path"])))

    def _cleanup_row_managed(self, row: Any) -> bool:
        """Whether a due row is Pudge's to delete under ``delete_only_managed_files``."""
        if not self.config.agent.delete_only_managed_files:
            return True
        if str(row["torrent_hash"] or "").strip():
            return True
        if self._downloaded_row_owned(row):
            return True
        media_id = row["media_id"]
        return media_id is not None and self._library_folder_owner(Path(str(row["video_path"]))) == int(
            media_id
        )

    def _reconcile_managed_episodes_behind_anilist_progress(self) -> int:
        """Mark Pudge's local episodes that AniList already counts as watched.

        Episodes watched outside Pudge, counted on AniList by hand, or re-added
        after watching stay ``ready`` locally, so their folder was never
        cleaned up. A Completed title, or progress covering the episode, makes
        it watched for cleanup with the usual delay. Never for Repeating,
        Planning or Dropped titles, rewatch schedules, recently played
        episodes, or files Pudge does not own. Never for a file Pudge downloaded
        after the list already counted its episode (a rewatch, a better release):
        it stays until it is watched.
        """
        now = time.time()
        delay_seconds = max(
            0.0,
            float(self.config.agent.delete_after_watched_hours) * 3600.0,
        )
        # This is inferred cleanup evidence, not the real watch time. Keep it
        # just outside the Recently watched TTL.
        inferred_watched_at = now - delay_seconds - 1.0
        recently_played = now - max(delay_seconds, 24 * 3600.0)
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT e.*,a.status AS list_status,a.progress AS list_progress,
                       COALESCE(e.media_episode,e.episode) AS first_episode,
                       COALESCE(e.media_episode_end,e.media_episode,e.episode) AS last_episode
                FROM episodes e JOIN anime a ON a.media_id=e.media_id
                WHERE COALESCE(e.media_episode,e.episode) IS NOT NULL
                  AND e.state NOT IN ('watched','dropped')
                  AND e.watched_at IS NULL
                  AND e.delete_after IS NULL
                  AND a.status IN ('CURRENT','PAUSED','COMPLETED')
                  AND a.progress>=COALESCE(e.media_episode_end,e.media_episode,e.episode)
                  AND (e.playback_updated_at IS NULL OR e.playback_updated_at<?)
                  AND NOT EXISTS (
                      SELECT 1 FROM personal_release_schedules s
                      WHERE s.enabled=1 AND s.anime_id=e.media_id AND s.rewatch=1
                  )
                """,
                (recently_played,),
            ).fetchall()
        changed = 0
        marks_by_media: dict[int, list[list[float]]] = {}
        for row in rows:
            media_id = int(row["media_id"])
            if media_id not in marks_by_media:
                marks_by_media[media_id] = self.db.note_anilist_progress(
                    media_id, int(row["list_progress"] or 0), now=now
                )
            reached_at = self.db.anilist_progress_reached_at(
                marks_by_media[media_id], int(row["last_episode"])
            )
            downloaded_at = row["downloaded_at"]
            if reached_at is not None and downloaded_at is not None and float(downloaded_at) >= reached_at:
                self.logger.debug(
                    "SKIP step=cleanup.anilist_covered reason=downloaded_after_counted media_id=%s episode=%s video=%r downloaded_at=%.0f counted_at=%.0f",
                    media_id,
                    row["last_episode"],
                    str(row["video_path"]),
                    float(downloaded_at),
                    reached_at,
                )
                continue
            owned = (
                str(row["torrent_hash"] or "").strip()
                or self._downloaded_row_owned(row)
                or self._library_folder_owner(Path(str(row["video_path"]))) == int(row["media_id"])
            )
            if not owned:
                continue
            with self.db.connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE episodes
                    SET state='watched',
                        watched_at=?,
                        delete_after=?,
                        playback_position=NULL,
                        playback_duration=NULL,
                        playback_updated_at=NULL,
                        playback_active_seconds=0,
                        updated_at=?
                    WHERE video_path=? AND state NOT IN ('watched','dropped') AND delete_after IS NULL
                    """,
                    (inferred_watched_at, now + delay_seconds, now, str(row["video_path"])),
                )
            if not int(cursor.rowcount or 0):
                continue
            changed += 1
            # A (non-rewatch) personal schedule paces episodes not watched yet;
            # it must not keep an episode AniList already counts.
            for episode in range(int(row["first_episode"]), int(row["last_episode"]) + 1):
                self.db.mark_personal_release_watched(
                    int(row["media_id"]), episode, watched_at=inferred_watched_at
                )
            self.logger.info(
                "REPAIR step=cleanup.anilist_covered media_id=%s episode=%s video=%r list_status=%s progress=%s delete_after=%.0f",
                row["media_id"],
                row["last_episode"],
                str(row["video_path"]),
                row["list_status"],
                row["list_progress"],
                now + delay_seconds,
            )
        return changed

    @staticmethod
    def _delete_confirmed_torrent(client: Any, torrent_hash: str, *, delete_files: bool) -> None:
        client.delete(torrent_hash, delete_files=delete_files)
        status = getattr(client, "torrent_status", None)
        if not callable(status) or status(torrent_hash) is not None:
            raise ManagerError(err("manager.torrentRemovalUnconfirmed", hash=torrent_hash))

    # Returned when a torrent's own file list could not be read: every caller keeps it.
    _TORRENT_FILES_UNKNOWN = Path("<torrent file list unavailable>")

    @staticmethod
    def _path_forms(path: Path | str) -> set[str]:
        """The path as given and resolved (/var vs /private/var, a NAS link)."""
        value = Path(str(path)).expanduser()
        forms = {str(value)}
        try:
            forms.add(str(value.resolve()))
        except (OSError, RuntimeError):
            pass
        return forms

    def _torrent_file_rows(
        self,
        client: Any,
        torrent_hash: str,
        download: DownloadItem | None,
        snapshot: dict[str, Any] | None,
    ) -> list[tuple[dict[str, Any], list[Path]]] | None:
        """Each file of the torrent with the places it can be on disk; None: list unknown."""
        try:
            rows = list(client.files(torrent_hash) or [])
        except Exception:  # noqa: BLE001 - unknown means keep, never delete with files
            return None
        bases: list[Path] = []
        for value in (
            getattr(download, "save_path", "") if download is not None else "",
            (snapshot or {}).get("save_path"),
            (snapshot or {}).get("dir"),
        ):
            if value and str(value).strip() and Path(str(value)) not in bases:
                bases.append(Path(str(value)))
        content_values = [
            getattr(download, "content_path", "") if download is not None else "",
            (snapshot or {}).get("content_path"),
        ]
        for value in content_values:
            # A single file or the torrent's own top folder: its parent holds the names.
            if value and str(value).strip() and Path(str(value)).parent not in bases:
                bases.append(Path(str(value)).parent)
        result: list[tuple[dict[str, Any], list[Path]]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = Path(str(row.get("name") or ""))
            if not str(name) or str(name) == ".":
                continue
            places = [name] if name.is_absolute() else [base / name for base in bases]
            result.append((row, places))
        return result

    def _torrent_files_kept_on_disk(
        self,
        client: Any,
        torrent_hash: str,
        download: DownloadItem | None,
        deleting: set[str],
        snapshot: dict[str, Any] | None = None,
        *,
        complete_only: bool = False,
    ) -> list[Path]:
        """Wanted video files of the torrent on disk that are not being deleted now.

        Removing a torrent "with files" deletes every file of it. Pudge's rows can
        under-count a batch (no download row, the other episodes' rows without the hash),
        and Ansatsu Kyoushitsu lost its unwatched episodes 8-22 with its folder. The
        torrent's own file list decides, not the rows; a list that cannot be read keeps
        the torrent (``_TORRENT_FILES_UNKNOWN``). Deselected files (priority 0: aria2 may
        leave stubs of them) do not count; ``complete_only``: only finished files count.
        """
        doomed: set[str] = set()
        for value in deleting:
            doomed |= self._path_forms(value)
        names = {Path(value).name for value in deleting}
        listed = self._torrent_file_rows(client, torrent_hash, download, snapshot)
        if listed is None:
            return [self._TORRENT_FILES_UNKNOWN]
        kept: dict[str, Path] = {}
        for row, places in listed:
            name = Path(str(row.get("name") or ""))
            if name.suffix.casefold() not in VIDEO_EXTENSIONS:
                continue
            if row.get("priority") is not None and int(row.get("priority") or 0) == 0:
                continue
            if complete_only and row.get("progress") is not None and float(row.get("progress") or 0) < 0.999:
                continue
            if not places:
                if name.name not in names:
                    kept[str(name)] = name  # nowhere to look: another file of the torrent counts
                continue
            if any(self._path_forms(place) & doomed for place in places):
                continue
            for place in places:
                if place.is_file():
                    kept[str(place)] = place
                    break
        return sorted(kept.values())

    @staticmethod
    def _torrent_snapshot(client: Any, torrent_hash: str) -> dict[str, Any] | None:
        try:
            snapshot = client.torrent_status(torrent_hash)
        except Exception:  # noqa: BLE001 - the download row may still name the folder
            return None
        return snapshot if isinstance(snapshot, dict) else None

    def _deselect_deleted_files(
        self,
        client: Any,
        torrent_hash: str,
        deleting: set[str],
        download: DownloadItem | None = None,
        snapshot: dict[str, Any] | None = None,
    ) -> None:
        """A batch keeps seeding its other files: the deleted ones are not fetched again.

        Matched by full path (S1/01.mkv is not S2/01.mkv). Raises ManagerError when the
        client did not take it: the caller then keeps the file for a later pass.
        """
        doomed: set[str] = set()
        for value in deleting:
            doomed |= self._path_forms(value)
        names = {Path(value).name for value in deleting}
        listed = self._torrent_file_rows(client, torrent_hash, download, snapshot)
        if listed is None:
            raise ManagerError(err("manager.torrentRemovalUnconfirmed", hash=torrent_hash))
        indices: list[int] = []
        for row, places in listed:
            if row.get("index") is None:
                continue
            if places:
                hit = any(self._path_forms(place) & doomed for place in places)
            else:
                hit = Path(str(row.get("name") or "")).name in names
            if hit and int(row.get("priority", 1) or 0) != 0:
                indices.append(int(row["index"]))
        if not indices:
            return
        try:
            client.set_file_priority(torrent_hash, indices, 0)
        except Exception as exc:  # noqa: BLE001 - keep the file, try again later
            self.logger.warning(
                "FAIL step=cleanup.deselect hash=%s files=%s error=%r", torrent_hash, indices, str(exc)[:200]
            )
            raise ManagerError(err("manager.torrentRemovalUnconfirmed", hash=torrent_hash)) from exc
        self.logger.info("DONE step=cleanup.deselect hash=%s files=%s", torrent_hash, indices)

    def _reselect_wanted_files(
        self, client: Any, torrent_hash: str, episodes: Iterable[int | None], *, batch: bool
    ) -> None:
        """Asked again for an episode Pudge deselected in this torrent: fetch it again."""
        wanted = {int(value) for value in episodes if value is not None}
        try:
            rows = list(client.files(torrent_hash) or [])
        except Exception:  # noqa: BLE001 - nothing to turn back on
            return
        indices = []
        for row in rows:
            if not isinstance(row, dict) or row.get("index") is None:
                continue
            if row.get("priority") is None or int(row.get("priority") or 0) != 0:
                continue
            name = Path(str(row.get("name") or ""))
            if name.suffix.casefold() not in VIDEO_EXTENSIONS:
                continue
            if batch or parse_anime_filename(name).episode in wanted:
                indices.append(int(row["index"]))
        if not indices:
            return
        try:
            client.set_file_priority(torrent_hash, indices, 1)
            self.logger.info("REPAIR step=torrent.reselect hash=%s files=%s", torrent_hash, indices)
        except Exception as exc:  # noqa: BLE001 - the existing download still blocks a duplicate
            self.logger.warning("FAIL step=torrent.reselect hash=%s error=%r", torrent_hash, str(exc)[:200])

    def _deselect_owned_files(
        self, torrent_hash: str, download: DownloadItem | None, videos: list[Path]
    ) -> None:
        """Deselect ``videos`` in every client holding the torrent; raises when one failed."""
        doomed = {str(video) for video in videos}
        clients = self.torrent_clients()
        try:
            for _name, client in clients:
                try:
                    snapshot = self._torrent_snapshot(client, torrent_hash)
                except Exception:  # noqa: BLE001
                    snapshot = None
                if snapshot is None:
                    continue  # not in this client (or it is stopped): nothing to fetch again
                self._deselect_deleted_files(client, torrent_hash, doomed, download, snapshot)
        finally:
            for _name, client in clients:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    def _replacement_clients(self) -> list[Any]:
        """The add client, plus aria2 when both backends are on (upgrades of its torrents)."""
        clients = [self.qbt_client()]
        if self.config.qbittorrent.enabled and self.config.aria2.enabled:
            clients.extend(client for name, client in self.torrent_clients() if name == "aria2")
        return clients

    def _retire_torrent_file(
        self, clients: list[Any], torrent_hash: str, video: Path | None, download: DownloadItem | None
    ) -> bool:
        """Remove a replaced torrent with its files, or only ``video`` of a batch.

        True: the torrent is gone; False: it holds other files and only ``video`` was
        deselected (the caller deletes the file). Raises when the client failed.
        """
        doomed = {str(video)} if video is not None else set()
        holders = [client for client in clients if self._torrent_snapshot(client, torrent_hash) is not None]
        removed = True
        for client in holders or clients[:1]:
            snapshot = self._torrent_snapshot(client, torrent_hash)
            kept = (
                self._torrent_files_kept_on_disk(client, torrent_hash, download, doomed, snapshot)
                if holders
                else []
            )
            if kept == [self._TORRENT_FILES_UNKNOWN]:
                # File list unreadable: never "with files"; the caller deletes ``video``.
                client.delete(torrent_hash, delete_files=False)
                continue
            if kept:
                self._deselect_deleted_files(client, torrent_hash, doomed, download, snapshot)
                removed = False
                continue
            client.delete(torrent_hash, delete_files=True)
        return removed

    def _delete_owned_torrent(
        self,
        torrent_hash: str,
        download: DownloadItem | None,
        *,
        deleting: Iterable[Path | str] | None = None,
    ) -> bool:
        """Remove the torrent with its files; False when it holds other files on disk.

        ``deleting``: the files the caller deletes now. A torrent with any other video
        file on disk stays (its deleted files deselected): only those files go.
        """
        doomed: set[str] = set()
        for value in deleting or ():
            doomed.add(str(value))
            try:
                doomed.add(str(Path(value).expanduser().resolve()))
            except (OSError, RuntimeError):
                pass
        removed = True
        raw = download.raw if download is not None else {}
        owners = {str(value) for value in raw.get("_backends", []) if value}
        if raw.get("backend"):
            owners.add(str(raw["backend"]))
        clients = self.torrent_clients()
        available = {name for name, _client in clients}
        started: list[Any] = []
        try:
            if owners - available:
                raise ManagerError(
                    err("manager.torrentOwnerUnavailable", backends=", ".join(sorted(owners - available)))
                )
            # Observe every configured client: the same hash may exist in both.
            for name, client in clients:
                if name == "aria2" and self._aria2_idle_stopped(client):
                    # The sidecar stopped itself after idle minutes. Pudge's
                    # aria2 records say whether it can hold this torrent; if
                    # so, start it for the removal instead of failing forever.
                    known = getattr(client, "known_offline", None)
                    if not (callable(known) and known(torrent_hash)):
                        self.logger.info(
                            "SKIP step=cleanup.aria2 reason=idle_stopped_not_recorded hash=%s", torrent_hash
                        )
                        continue
                    self.logger.info("EVENT cleanup.aria2_start reason=idle_stopped hash=%s", torrent_hash)
                    self._torrent_network_start(client, "ensure_running")
                    started.append(client)
                snapshot = client.torrent_status(torrent_hash)
                if snapshot is not None or name in owners:
                    if deleting is not None:
                        kept = self._torrent_files_kept_on_disk(
                            client,
                            torrent_hash,
                            download,
                            doomed,
                            snapshot if isinstance(snapshot, dict) else None,
                        )
                        if kept == [self._TORRENT_FILES_UNKNOWN]:
                            # Its file list could not be read: the torrent goes, its files
                            # stay (the caller deletes only the due ones itself).
                            self.logger.warning(
                                "KEEP step=cleanup.torrent_files reason=file_list_unknown backend=%s hash=%s",
                                name,
                                torrent_hash,
                            )
                            self._delete_confirmed_torrent(client, torrent_hash, delete_files=False)
                            continue
                        if kept:
                            self.logger.warning(
                                "KEEP step=cleanup.torrent reason=other_files_on_disk backend=%s hash=%s "
                                "deleting=%s kept=%s first_kept=%r",
                                name,
                                torrent_hash,
                                len({Path(value).name for value in doomed}),
                                len(kept),
                                str(kept[0]),
                            )
                            # Raises when the client did not deselect them: the files stay
                            # for the next pass instead of being fetched again.
                            self._deselect_deleted_files(
                                client,
                                torrent_hash,
                                doomed,
                                download,
                                snapshot if isinstance(snapshot, dict) else None,
                            )
                            removed = False
                            continue
                    self._delete_confirmed_torrent(client, torrent_hash, delete_files=True)
        finally:
            for client in started:
                # Started only for this removal: stop it again while idle.
                try:
                    client.stop_when_idle()
                except Exception as exc:  # noqa: BLE001 - the regular idle stop retries
                    self.logger.warning("SKIP step=cleanup.aria2_idle_stop error=%r", str(exc))
            for _name, client in clients:
                client.close()
        return removed

    def _identity_repair_protected(
        self, torrent_hash: str, video: Path | str, *, now: float | None = None
    ) -> bool:
        """Whether a freshly remapped/repaired file is inside its protection window."""
        torrent_hash = str(torrent_hash or "").strip().casefold()
        try:
            anchor = json.loads(self.db.get_state("release_identity:" + torrent_hash, "") or "{}")
        except (TypeError, ValueError):
            anchor = {}
        if not isinstance(anchor, dict):
            return False
        try:
            protected_until = float(anchor.get("cleanup_protected_until") or 0)
        except (TypeError, ValueError):
            protected_until = 0.0
        current = time.time() if now is None else float(now)
        return anchor.get("video_path") == str(video) and protected_until > current

    # A due file whose deletion failed, or could not be checked against AniList,
    # waits this long; the scheduled agent must not ask AniList every tick.
    CLEANUP_RETRY_SECONDS = 30 * 60
    _CLEANUP_RETRY_STATE = "cleanup_retry_after"

    def _cleanup_retry_after(self) -> dict[str, float]:
        try:
            data = json.loads(self.db.get_state(self._CLEANUP_RETRY_STATE, "") or "{}")
        except (TypeError, ValueError):
            return {}
        result: dict[str, float] = {}
        for key, value in (data if isinstance(data, dict) else {}).items():
            try:
                result[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return result

    def _save_cleanup_retry_after(self, retry_after: dict[str, float], *, now: float) -> None:
        kept = {key: value for key, value in retry_after.items() if value > now}
        encoded = json.dumps(kept, sort_keys=True) if kept else ""
        if encoded != (self.db.get_state(self._CLEANUP_RETRY_STATE, "") or ""):
            self.db.set_state(self._CLEANUP_RETRY_STATE, encoded)

    def _postpone_cleanup_rows(self, rows: list[Any], retry_after: dict[str, float], *, now: float) -> None:
        for row in rows:
            retry_after[str(row["video_path"])] = now + self.CLEANUP_RETRY_SECONDS
        self._save_cleanup_retry_after(retry_after, now=now)

    @staticmethod
    def _batch_title_finished(anime: LibraryAnime | None) -> bool:
        """keep_batch_until_completed: the list counts the whole title as watched.

        An unknown total (an airing or long title, or no local list row) is
        not finished: watching episode 1 must not take a 100-episode batch.
        """
        if anime is None:
            return False
        if anime.episodes:
            return int(anime.progress or 0) >= int(anime.episodes)
        return str(anime.status or "").upper() == "COMPLETED"

    def _cleanup_skip_reason(
        self,
        row: Any,
        downloads: dict[str, DownloadItem],
        *,
        now: float,
        retry_after: dict[str, float],
    ) -> str | None:
        """Why this pass does not delete a due row (the log text), or None."""
        video = Path(str(row["video_path"]))
        torrent_hash = str(row["torrent_hash"] or "").strip().casefold()
        if self._identity_repair_protected(torrent_hash, video, now=now):
            return f"identity_repair_protection video={str(video)!r}"
        if not self._cleanup_row_managed(row):
            return f"unmanaged_no_hash video={str(video)!r}"
        out_of_reach = self._cleanup_out_of_reach(video)
        if out_of_reach:
            # Its disk is unplugged or its library folder renamed: the file was not
            # deleted. Removing the torrent and the row lost the watched mark, and the
            # file came back as a new unwatched episode once the disk was there again.
            return f"{out_of_reach} video={str(video)!r}"
        retry_at = retry_after.get(str(video), 0.0)
        if retry_at > now:
            return f"retry_later retry_at={retry_at:.0f} video={str(video)!r}"
        if (
            torrent_hash
            and self.config.agent.keep_batch_until_completed
            and str(row["state"] or "") != "dropped"
        ):
            download = downloads.get(torrent_hash)
            shared_episode_count = self.db.episode_count_for_torrent(torrent_hash)
            if (download and download.is_batch) or shared_episode_count > 1:
                if self._pack_seeding_state(torrent_hash, download) == "ended":
                    # Its seeding is over: the pack leaves the client (files kept) and
                    # its watched episodes go one by one, each after its own delay.
                    return None
                anime = self.db.get_anime(int(row["media_id"])) if row["media_id"] else None
                if not self._batch_title_finished(anime):
                    return (
                        f"batch_incomplete media_id={row['media_id']} "
                        f"progress={anime.progress if anime else None} "
                        f"episodes={anime.episodes if anime else None} torrent_hash={torrent_hash} "
                        f"shared_episodes={shared_episode_count} metadata_present={bool(download)}"
                    )
                # If qBittorrent/download metadata disappeared, never infer
                # that one due episode is a safe single-file cleanup.
                if download is None and shared_episode_count > 1:
                    return (
                        f"batch_metadata_missing torrent_hash={torrent_hash} "
                        f"shared_episodes={shared_episode_count} video={str(video)!r}"
                    )
        return None

    def _cleanup_out_of_reach(self, video: Path) -> str:
        """Why a due file that is not there may only be out of reach ("" when it is there,
        or its drive and folder are and the file itself is gone). Mount checks are kept for
        the cleanup pass (``_cleanup_reach``)."""
        try:
            if video.exists():
                return ""
        except OSError:
            pass
        reach = getattr(self, "_cleanup_reach", None)
        if reach is None:
            reach = self._cleanup_reach = ({}, self._missing_video_roots())
        volumes, roots = reach
        return self._missing_video_unreachable(video, volumes, roots)

    # A complete torrent that no longer uploads: its ratio/seeding-time goal was
    # reached, or it was stopped (qBittorrent "state", aria2 "status").
    _QBT_SEEDING_ENDED_STATES = frozenset({"pausedup", "stoppedup"})
    _ARIA2_SEEDING_ENDED_STATUSES = frozenset({"complete", "paused"})

    @classmethod
    def _snapshot_seeding_ended(cls, snapshot: Any) -> bool:
        """Whether a client's torrent snapshot is complete and no longer seeding."""
        if not isinstance(snapshot, dict):
            return False
        try:
            progress = float(snapshot.get("progress") or 0.0)
        except (TypeError, ValueError):
            return False
        if progress < 0.999:
            return False
        if "state" in snapshot:
            return str(snapshot.get("state") or "").casefold() in cls._QBT_SEEDING_ENDED_STATES
        if "status" in snapshot:
            return str(snapshot.get("status") or "").casefold() in cls._ARIA2_SEEDING_ENDED_STATUSES
        return False  # no state reported: it may still seed

    @staticmethod
    def _strict_torrent_status(client: Any, torrent_hash: str) -> Any:
        """The torrent's snapshot or None (the client has none); raises when it could not be asked."""
        lookup = getattr(client, "lookup_torrent", None)
        if callable(lookup):
            return lookup(torrent_hash)
        return client.torrent_status(torrent_hash)

    @staticmethod
    def _download_owners(download: DownloadItem | None) -> set[str]:
        raw = download.raw if download is not None else {}
        owners = {str(value) for value in raw.get("_backends", []) if value}
        if raw.get("backend"):
            owners.add(str(raw["backend"]))
        return owners

    def _pack_seeding_state(self, torrent_hash: str, download: DownloadItem | None) -> str:
        """ "ended" when no client seeds this multi-file torrent any more (asked once per cleanup).

        Anything else (the reason) keeps the pack whole: deleting files under a torrent
        that still seeds would break it.
        """
        cache = getattr(self, "_pack_seeding_cache", None)
        if cache is None:
            cache = self._pack_seeding_cache = {}
        if torrent_hash not in cache:
            cache[torrent_hash] = self._read_pack_seeding_state(torrent_hash, download)
            self.logger.info(
                "RESULT step=cleanup.pack_seeding torrent_hash=%s state=%s", torrent_hash, cache[torrent_hash]
            )
        return "ended" if cache[torrent_hash] in {"ended", "detached"} else cache[torrent_hash]

    # A torrent Pudge itself stopped (Torrent Off, background quiet, Stop all, the
    # window's Pause): a temporary stop, never "seeding ended". Cleared when Pudge
    # starts it again. One state row per hash, so processes never overwrite each other.
    _PUDGE_PAUSED_PREFIX = "torrent_paused_by_pudge:"

    def note_torrents_paused_by_pudge(self, hashes: Iterable[str], *, reason: str) -> None:
        for value in hashes:
            key = str(value or "").strip().casefold()
            if key:
                self.db.set_state(self._PUDGE_PAUSED_PREFIX + key, f"{reason}|{time.time():.0f}")

    def note_torrents_resumed_by_pudge(self, hashes: Iterable[str]) -> None:
        keys = [str(value or "").strip().casefold() for value in hashes]
        self.db.delete_states(self._PUDGE_PAUSED_PREFIX + key for key in keys if key)

    # Stops Torrent On undoes: Torrent Off, the startup/background quiet and the
    # add-time pause while torrents are off. The window's Pause and Stop all are the
    # user's own choice and stay paused.
    _TEMPORARY_PUDGE_PAUSES = ("quiet_", "torrents_off")

    def torrents_paused_by_pudge_temporarily(self) -> list[str]:
        """Hashes Pudge stopped only while torrents were off (Torrent On resumes them)."""
        prefix = self._PUDGE_PAUSED_PREFIX
        with self.db.connect() as conn:
            rows = conn.execute(
                f"SELECT key,value FROM state WHERE {STATE_PREFIX_SQL}", state_prefix_bounds(prefix)
            ).fetchall()
        result = []
        for row in rows:
            reason = str(row["value"] or "").split("|", 1)[0]
            if reason.startswith(self._TEMPORARY_PUDGE_PAUSES):
                result.append(str(row["key"])[len(prefix) :])
        return sorted(result)

    def torrent_paused_by_pudge(self, torrent_hash: str) -> str:
        """Why Pudge stopped this torrent ("" when it did not)."""
        key = str(torrent_hash or "").strip().casefold()
        return self.db.get_state(self._PUDGE_PAUSED_PREFIX + key, "") if key else ""

    def _read_pack_seeding_state(self, torrent_hash: str, download: DownloadItem | None) -> str:
        if not self.downloads_enabled():
            return "torrents_off"
        paused_by = self.torrent_paused_by_pudge(torrent_hash)
        if paused_by:
            return "paused_by_pudge_" + paused_by.split("|", 1)[0]
        if download is not None and float(download.progress or 0.0) < 0.999:
            return "downloading"
        owners = self._download_owners(download)
        clients = self.torrent_clients()
        try:
            if owners - {name for name, _client in clients}:
                return "owner_unavailable"
            for name, client in clients:
                if name == "aria2" and self._aria2_idle_stopped(client):
                    continue  # the sidecar stopped itself: nothing in it downloads or seeds
                try:
                    snapshot = self._strict_torrent_status(client, torrent_hash)
                except Exception:  # noqa: BLE001 - "could not ask" is never "not seeding"
                    return f"unknown_{name}"
                if snapshot is None:
                    continue
                if not self._snapshot_seeding_ended(snapshot):
                    state = (
                        snapshot.get("state", snapshot.get("status")) if isinstance(snapshot, dict) else None
                    )
                    return f"seeding_{name}_{state}"
            return "ended"
        finally:
            for _name, client in clients:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    def _detach_finished_pack(self, torrent_hash: str, download: DownloadItem | None) -> None:
        """Remove a pack whose seeding ended from every client, keeping all of its files.

        Its episode rows (and split-out OVA/special rows sharing the hash) stay tracked;
        each watched file is then deleted on its own schedule. Raises when a client
        still seeds it or did not confirm the removal: nothing is deleted this pass.
        """
        cache = getattr(self, "_pack_seeding_cache", None)
        if cache is None:
            cache = self._pack_seeding_cache = {}
        if cache.get(torrent_hash) == "detached":
            return
        owners = self._download_owners(download)
        clients = self.torrent_clients()
        started: list[Any] = []
        removed_from: list[str] = []
        try:
            if owners - {name for name, _client in clients}:
                raise ManagerError(
                    err(
                        "manager.torrentOwnerUnavailable",
                        backends=", ".join(sorted(owners - {name for name, _client in clients})),
                    )
                )
            for name, client in clients:
                if name == "aria2" and self._aria2_idle_stopped(client):
                    known = getattr(client, "known_offline", None)
                    if not (callable(known) and known(torrent_hash)):
                        continue
                    # Its session still holds the torrent: start it for the removal.
                    self._torrent_network_start(client, "ensure_running")
                    started.append(client)
                snapshot = self._strict_torrent_status(client, torrent_hash)
                if snapshot is None:
                    continue
                if not self._snapshot_seeding_ended(snapshot) or self.torrent_paused_by_pudge(torrent_hash):
                    cache[torrent_hash] = "seeding"
                    raise ManagerError(err("manager.packStillSeeding", hash=torrent_hash))
                self._delete_confirmed_torrent(client, torrent_hash, delete_files=False)
                removed_from.append(name)
        finally:
            for client in started:
                try:
                    client.stop_when_idle()
                except Exception as exc:  # noqa: BLE001 - the regular idle stop retries
                    self.logger.warning("SKIP step=cleanup.aria2_idle_stop error=%r", str(exc))
            for _name, client in clients:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
        self.db.delete_download_record(torrent_hash)
        cache[torrent_hash] = "detached"
        self.logger.info(
            "DONE step=cleanup.pack_detach torrent_hash=%s backends=%s files_kept=true",
            torrent_hash,
            removed_from,
        )

    def _remove_pack_leftovers(self, torrent_hash: str, videos: list[Path]) -> None:
        """Leftovers of a torrent removed from its client once its last file in a folder is gone.

        Only the deleted files' aria2 control files, the pack folder's control file and
        the pack's own ``<infohash>.torrent``; a folder that still holds any other
        file (an OVA kept as its own entry, an unwatched episode) keeps everything.
        """
        roots: list[Path] = []
        for value in (self.config.library.root_dir, *self.config.paths.download_dirs):
            try:
                roots.append(Path(value).expanduser().resolve())
            except OSError:
                continue

        def allowed(path: Path) -> bool:
            try:
                resolved = path.resolve(strict=False)
            except OSError:
                return False
            return any(root in resolved.parents for root in roots)

        def unlink(path: Path) -> None:
            try:
                if allowed(path) and path.is_file() and not path.is_symlink():
                    path.unlink()
                    self.logger.info("DONE step=cleanup.pack_leftover path=%r", str(path))
            except OSError as exc:
                self.logger.info("SKIP step=cleanup.pack_leftover path=%r error=%r", str(path), exc)

        metadata_name = f"{torrent_hash.casefold()}.torrent"
        for video in videos:
            unlink(video.with_name(video.name + ".aria2"))
        for folder in sorted(
            {video.parent for video in videos}, key=lambda path: len(path.parts), reverse=True
        ):
            try:
                rest = [path for path in folder.iterdir()] if folder.is_dir() else []
            except OSError:
                continue
            if any(
                path.is_dir()
                or not (
                    path.name.casefold() == metadata_name
                    or path.name == ".DS_Store"
                    or (
                        path.name.endswith(".aria2")
                        and not path.with_name(path.name[: -len(".aria2")]).exists()
                    )
                )
                for path in rest
            ):
                continue  # other files stay in this folder: so do its leftovers
            for path in rest:
                unlink(path)
            self._remove_empty_episode_parent(folder / "removed")
            if not folder.exists():
                unlink(folder.with_name(folder.name + ".aria2"))
                unlink(folder.parent / metadata_name)

    def _torrent_episode_rows(self, torrent_hash: str) -> list[Any]:
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM episodes WHERE torrent_hash=?", (torrent_hash,)).fetchall()

    @staticmethod
    def _cleanup_removes_whole_torrent(
        row: Any,
        shared: list[Any],
        download: DownloadItem | None,
        deletable_paths: set[str],
    ) -> bool:
        """Whether deleting ``row`` removes its torrent (with every file) too.

        A single-episode torrent goes with its episode. A batch goes only when
        every episode row sharing its hash is due now and nothing is still
        downloading; until then only the due files are deleted.
        """
        batch_like = bool((download is not None and download.is_batch) or len(shared) > 1)
        if not batch_like:
            return True
        if any(str(other["video_path"]) not in deletable_paths for other in shared):
            return False
        dropped = str(row["state"] or "") == "dropped"
        return dropped or download is None or float(download.progress or 0.0) >= 0.999

    @_reusing_db_connection
    def cleanup(self) -> int:
        repaired = self.db.repair_missing_cleanup_schedule(
            self.config.agent.delete_after_watched_hours, library_root=self.config.library.root_dir
        )
        if repaired:
            self.logger.info(
                "REPAIR step=cleanup.schedule_missing rows=%s delete_after_hours=%s",
                repaired,
                self.config.agent.delete_after_watched_hours,
            )
        hash_repairs = self._repair_cleanup_torrent_hashes()
        if hash_repairs:
            self.logger.info(
                "REPAIR step=cleanup.torrent_hash rows=%s",
                hash_repairs,
            )
        progress_repairs = self._reconcile_managed_episodes_behind_anilist_progress()
        if progress_repairs:
            self.logger.info(
                "REPAIR step=cleanup.anilist_progress rows=%s",
                progress_repairs,
            )
        self._pack_seeding_cache = {}
        self._cleanup_reach = None
        now = time.time()
        for item in self.db.episodes():
            if (
                item.delete_after is not None
                and float(item.delete_after) <= now
                and self.db.personal_release_item_retained(item.media_id, item.episode)
            ):
                self.logger.info(
                    "SKIP step=cleanup.item reason=personal_schedule_retention media_id=%s episode=%s video=%r",
                    item.media_id,
                    item.episode,
                    str(item.video_path),
                )
        due = [row for row in self.db.due_cleanup() if self._cleanup_row_managed(row)]
        downloads = {item.torrent_hash.casefold(): item for item in self.db.downloads()}
        retry_after = self._cleanup_retry_after()
        deletable = [
            row
            for row in due
            if self._cleanup_skip_reason(row, downloads, now=now, retry_after=retry_after) is None
        ]
        watched_due = [row for row in deletable if str(row["state"] or "") == "watched"]
        if watched_due and self.config.anilist.enabled and self.config.anilist.access_token.strip():
            # A user may undo watched progress directly on AniList after closing
            # mpv. Verify progress immediately before destructive cleanup so the
            # scheduled agent cannot delete a file whose watched mark was revoked.
            # Only rows this pass can delete ask AniList; a failed check waits
            # CLEANUP_RETRY_SECONDS before asking again.
            failure: Any = None
            try:
                verified = self.sync_anilist()
            except Exception as exc:
                failure = exc
            else:
                if getattr(verified, "used_cache", False):
                    # AniList was not reached: the list is the local cache.
                    failure = "anilist_unreachable_cached_list"
            if failure is not None:
                self.logger.warning(
                    "EVENT cleanup.anilist_verification_failed error=%r rows=%s retry_in=%.0f",
                    failure,
                    len(deletable),
                    self.CLEANUP_RETRY_SECONDS,
                )
                self.log("Deletion postponed: could not check the current AniList progress")
                self._postpone_cleanup_rows(deletable, retry_after, now=now)
                return 0
            due = [row for row in self.db.due_cleanup() if self._cleanup_row_managed(row)]

        skips = {
            str(row["video_path"]): self._cleanup_skip_reason(
                row, downloads, now=now, retry_after=retry_after
            )
            for row in due
        }
        deletable_paths = {path for path, reason in skips.items() if reason is None}
        deleted = 0
        deleted_torrents: set[str] = set()
        touched_media: set[int] = set()
        self.logger.info("START step=cleanup.items due=%s", len(due))
        for row in due:
            video = Path(str(row["video_path"]))
            torrent_hash = str(row["torrent_hash"] or "").strip().casefold()
            downloaded_at = float(row["downloaded_at"] or 0.0) if "downloaded_at" in row.keys() else 0.0
            managed_without_hash = bool(downloaded_at)
            self.logger.info(
                "CANDIDATE step=cleanup.item media_id=%s episode=%s video=%r state=%s torrent_hash=%s downloaded_at=%s delete_after=%s exists=%s",
                row["media_id"],
                row["episode"],
                str(video),
                row["state"],
                torrent_hash,
                downloaded_at or None,
                row["delete_after"],
                video.exists(),
            )
            if torrent_hash and torrent_hash in deleted_torrents:
                self.logger.info(
                    "SKIP step=cleanup.item reason=torrent_already_deleted torrent_hash=%s video=%r",
                    torrent_hash,
                    str(video),
                )
                continue
            skip_reason = skips.get(str(video))
            if skip_reason is not None:
                self.logger.info("SKIP step=cleanup.item reason=%s", skip_reason)
                continue
            if not torrent_hash and managed_without_hash:
                self.logger.info(
                    "REPAIR step=cleanup.item reason=managed_download_missing_hash video=%r downloaded_at=%s",
                    str(video),
                    downloaded_at,
                )
            download = downloads.get(torrent_hash) if torrent_hash else None
            targets = [row]
            remove_torrent = False
            pack_detach = False
            if torrent_hash and self.downloads_enabled():
                shared = self._torrent_episode_rows(torrent_hash)
                pack_detach = bool(
                    ((download is not None and download.is_batch) or len(shared) > 1)
                    and self._pack_seeding_state(torrent_hash, download) == "ended"
                )
                if pack_detach:
                    # Seeding is over: the torrent leaves the client with its files kept,
                    # and only this due episode goes (the others wait for their own delay).
                    self.logger.info(
                        "EVENT step=cleanup.pack_episode torrent_hash=%s rows=%s video=%r",
                        torrent_hash,
                        len(shared),
                        str(video),
                    )
                else:
                    remove_torrent = self._cleanup_removes_whole_torrent(
                        row, shared, download, deletable_paths
                    )
                if remove_torrent:
                    # Every episode row of this torrent is due now: remove the
                    # torrent and all of their files at once.
                    targets.extend(other for other in shared if str(other["video_path"]) != str(video))
                else:
                    self.logger.info(
                        "KEEP step=cleanup.torrent reason=batch_not_all_due torrent_hash=%s rows=%s due=%s video=%r",
                        torrent_hash,
                        len(shared),
                        sum(1 for other in shared if str(other["video_path"]) in deletable_paths),
                        str(video),
                    )
            try:
                if pack_detach:
                    self._detach_finished_pack(torrent_hash, download)
                elif remove_torrent:
                    # The torrent's own file list has the last word: a batch the rows
                    # under-count keeps its torrent and its other episodes.
                    remove_torrent = self._delete_owned_torrent(
                        torrent_hash,
                        download,
                        deleting=[Path(str(target["video_path"])) for target in targets],
                    )
                    if not remove_torrent:
                        targets = [
                            target for target in targets if str(target["video_path"]) in deletable_paths
                        ] or [row]
                elif torrent_hash and self.downloads_enabled() and not pack_detach:
                    # The batch keeps its torrent: the due files are deselected first, so
                    # the client does not fetch them again (a failure keeps them for later).
                    self._deselect_owned_files(
                        torrent_hash, download, [Path(str(target["video_path"])) for target in targets]
                    )
                # qBittorrent can acknowledge deletion before the file is
                # physically removed, or retain it after a stale content-path
                # mapping, and aria2 forgets completed tasks on restart. A due,
                # watched pudge episode is safe to remove directly. A batch whose
                # other episodes are not due keeps its torrent: only this file goes.
                for target in targets:
                    Path(str(target["video_path"])).unlink(missing_ok=True)
                    if target["subtitle_path"]:
                        Path(str(target["subtitle_path"])).unlink(missing_ok=True)
                if remove_torrent:
                    self.db.delete_torrent_records(torrent_hash)
                    deleted_torrents.add(torrent_hash)
                for target in targets:
                    self.db.delete_episode_record(Path(str(target["video_path"])))
                self._forget_video_state(Path(str(target["video_path"])) for target in targets)
                if pack_detach or remove_torrent:
                    # The torrent is gone: its control files and saved metadata go with the last file.
                    self._remove_pack_leftovers(
                        torrent_hash, [Path(str(target["video_path"])) for target in targets]
                    )
                for target in targets:
                    target_video = Path(str(target["video_path"]))
                    if target["media_id"] is not None:
                        touched_media.add(int(target["media_id"]))
                    retry_after.pop(str(target_video), None)
                    self._remove_empty_episode_parent(target_video)
                    self.logger.info(
                        "DONE step=cleanup.item media_id=%s episode=%s video=%r torrent_hash=%s torrent_removed=%s",
                        target["media_id"],
                        target["episode"],
                        str(target_video),
                        torrent_hash,
                        remove_torrent,
                    )
                deleted += len(targets)
            except (OSError, QBittorrentError, ManagerError) as exc:
                self.logger.warning(
                    "FAIL step=cleanup.item media_id=%s episode=%s video=%r torrent_hash=%s error=%r retry_in=%.0f",
                    row["media_id"],
                    row["episode"],
                    str(video),
                    torrent_hash,
                    exc,
                    self.CLEANUP_RETRY_SECONDS,
                )
                self.log(f"Could not delete {video}: {exc}")
                for target in targets:
                    retry_after[str(target["video_path"])] = now + self.CLEANUP_RETRY_SECONDS
        self._save_cleanup_retry_after(retry_after, now=now)
        for media_id in touched_media:
            anime = self.db.get_anime(media_id)
            if anime is not None and anime.status == "DROPPED" and not self.db.episodes(media_id):
                self.logger.info("DONE step=drop.cleanup media_id=%s", media_id)
        self._prune_empty_library_dirs()
        return deleted

    def _repair_cleanup_torrent_hashes(self) -> int:
        """Reconnect overdue episode rows to their completed qBittorrent item."""
        due = self.db.due_cleanup()
        if not due:
            return 0
        downloads = [item for item in self.db.downloads() if self._download_is_complete(item)]
        repaired = 0
        for row in due:
            if str(row["torrent_hash"] or "").strip():
                continue
            video = Path(str(row["video_path"])).expanduser().resolve(strict=False)
            media_id = int(row["media_id"]) if row["media_id"] is not None else None
            episode = int(row["episode"]) if row["episode"] is not None else None
            ranked: list[tuple[int, DownloadItem]] = []
            for item in downloads:
                if media_id is not None and item.media_id is not None and item.media_id != media_id:
                    continue
                if (
                    episode is not None
                    and item.episode is not None
                    and item.episode != episode
                    and not item.is_batch
                ):
                    continue
                score = 0
                try:
                    content = Path(item.content_path).expanduser().resolve(strict=False)
                    if content == video:
                        score += 100
                    elif content.is_dir() and content in video.parents:
                        score += 80
                except (OSError, RuntimeError):
                    pass
                try:
                    save = Path(item.save_path).expanduser().resolve(strict=False)
                    if save == video.parent or save in video.parents:
                        score += 35
                except (OSError, RuntimeError):
                    pass
                if media_id is not None and item.media_id == media_id:
                    score += 30
                if episode is not None and item.episode == episode:
                    score += 30
                elif item.is_batch:
                    score += 15
                if score >= 60:
                    ranked.append((score, item))
            if not ranked:
                continue
            ranked.sort(key=lambda pair: pair[0], reverse=True)
            if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
                self.logger.info(
                    "SKIP step=cleanup.torrent_hash reason=ambiguous video=%r candidates=%s",
                    str(video),
                    [item.torrent_hash for score, item in ranked if score == ranked[0][0]],
                )
                continue
            winner = ranked[0][1]
            repaired += self.db.set_episode_torrent_hash(video, winner.torrent_hash)
            self.logger.info(
                "REPAIR step=cleanup.torrent_hash video=%r torrent_hash=%s media_id=%s episode=%s score=%s",
                str(video),
                winner.torrent_hash,
                media_id,
                episode,
                ranked[0][0],
            )
        return repaired

    def cleanup_qbittorrent_tags(self) -> dict[str, int]:
        if not self.downloads_enabled():
            return {"score_tags_removed": 0, "unused_tags_deleted": 0}
        client = self.qbt_client()
        try:
            cleaner = getattr(client, "cleanup_tags", None)
            if cleaner is None:
                return {"score_tags_removed": 0, "unused_tags_deleted": 0}
            result = cleaner()
        finally:
            client.close()
        self.logger.info(
            "DONE step=qbittorrent.tag_cleanup score_tags_removed=%s unused_tags_deleted=%s",
            result.get("score_tags_removed", 0),
            result.get("unused_tags_deleted", 0),
        )
        return {
            "score_tags_removed": int(result.get("score_tags_removed", 0)),
            "unused_tags_deleted": int(result.get("unused_tags_deleted", 0)),
        }

    def download_planned(self, media_id: int) -> NyaaRelease | None:
        """Planning → Watching: a finished show as a season pack; an airing one episode by episode.

        A "pack" search for a show that is still airing finds single-episode releases
        (one released episode) and recorded them as a batch, which then blocked every
        later episode as "already downloading".
        """
        anime = self.db.get_anime(int(media_id))
        if anime is not None and str(anime.media_status or "").upper() in {"RELEASING", "NOT_YET_RELEASED"}:
            self.logger.info(
                "EVENT step=watching.download media_id=%s mode=episode episode=%s reason=airing",
                media_id,
                anime.next_episode,
            )
            return self.search_and_add_best(media_id, episode=anime.next_episode, batch=False)
        return self.search_and_add_best(media_id, episode=None, batch=True)

    def cached_cover_path(self, anime: LibraryAnime) -> Path | None:
        if not anime.cover_url:
            return None
        suffix = Path(anime.cover_url.split("?", 1)[0]).suffix or ".jpg"
        path = self.config.library.cover_cache_dir / f"{anime.media_id}{suffix}"
        return path if path.is_file() and path.stat().st_size > 0 else None

    def cover_path(self, anime: LibraryAnime, *, allow_refresh: bool = True) -> Path | None:
        if not anime.cover_url:
            return None
        suffix = Path(anime.cover_url.split("?", 1)[0]).suffix or ".jpg"
        path = self.config.library.cover_cache_dir / f"{anime.media_id}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > 0:
            if not allow_refresh:
                return path
            age = time.time() - path.stat().st_mtime
            refresh_after = 6.5 * 86400 + (anime.media_id % 86400)
            if age < refresh_after:
                return path
        if anilist_refusal() is not None:
            # AniList refuses this Mac (403 cool-down): its image server is not asked either.
            return path if path.is_file() and path.stat().st_size > 0 else None
        try:
            response = httpx.get(
                anime.cover_url,
                timeout=20,
                follow_redirects=True,
                headers={"User-Agent": ANILIST_USER_AGENT},
            )
            response.raise_for_status()
            # Atomic: the window may be loading this file while it is refreshed;
            # a truncated read left the card on its placeholder.
            atomic_write_bytes(path, response.content)
            return path
        except (httpx.HTTPError, OSError):
            return path if path.is_file() and path.stat().st_size > 0 else None

    _PLACEHOLDER_METADATA_FIELDS = (
        "synonyms",
        "cover_url",
        "site_url",
        "episodes",
        "format",
        "season_year",
        "start_date",
        "studio",
        "media_status",
        "end_date",
        "mean_score",
        "duration",
        "next_airing_episode",
        "next_airing_at",
    )

    def _fill_placeholder_metadata(
        self, local: LibraryAnime, metadata: LibraryAnime, *, source: str
    ) -> LibraryAnime:
        """Title, titles and media fields of AniList's answer into a placeholder row.

        The list fields stay local (status, progress, score, add time, relations):
        the list sync owns them. Returns the stored row.
        """
        previous = local.title
        local.title = metadata.title
        local.titles = list(metadata.titles)
        for name in self._PLACEHOLDER_METADATA_FIELDS:
            value = getattr(metadata, name)
            if value not in (None, "", []):
                setattr(local, name, value)
        self.db.upsert_anime(local)
        self.logger.info(
            "RESULT step=anime.placeholder_metadata media_id=%s source=%s title=%r previous=%r",
            local.media_id,
            source,
            local.title,
            previous,
        )
        return self.db.get_anime(int(local.media_id)) or local

    _PLACEHOLDER_METADATA_MISSING_KEY = "anime_placeholder_metadata_missing_v1"
    PLACEHOLDER_METADATA_LIMIT = 25

    def repair_placeholder_anime_titles(
        self, *, fetch: bool = True, limit: int | None = None
    ) -> dict[str, int]:
        """Rows that carry a release or file name as their title, and rows without AniList metadata.

        Older versions created the parent row of an episode, download, release choice or
        torrent observation with the release or file name as its title
        ("[HorribleSubs] Youjo Senki - 03 [1080p].mkv"); nothing filled it later when the
        title was outside Watching/Planning, so Completed, search and relations showed the
        file name. At once and without network: the series title parsed from the name.
        Then (``fetch``) AniList's metadata by id, through the paced client, at most
        ``limit`` rows per run; an id AniList does not know is not asked again. Idempotent:
        a row with AniList titles is never touched. Rows are never deleted.
        """
        result = {"renamed": 0, "fetched": 0, "missing": 0, "pending": 0}
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT media_id,title FROM anime WHERE COALESCE(titles_json,'') IN ('','[]') "
                # Titles on the list first: they show in Completed and search.
                "ORDER BY COALESCE(status,'')='', media_id"
            ).fetchall()
            for row in rows:
                title = str(row["title"] or "")
                if not looks_like_release_name(title):
                    continue
                cleaned = release_name_display_title(title, int(row["media_id"]))
                conn.execute(
                    "UPDATE anime SET title=?,updated_at=? WHERE media_id=?",
                    (cleaned, time.time(), int(row["media_id"])),
                )
                result["renamed"] += 1
                self.logger.info(
                    "RESULT step=anime.release_name_title media_id=%s title=%r previous=%r",
                    row["media_id"],
                    cleaned,
                    title,
                )
        try:
            missing = {
                int(value)
                for value in json.loads(
                    self.db.get_state(self._PLACEHOLDER_METADATA_MISSING_KEY, "[]") or "[]"
                )
            }
        except (TypeError, ValueError):
            missing = set()
        pending = [int(row["media_id"]) for row in rows if int(row["media_id"]) not in missing]
        result["pending"] = len(pending)
        if (
            not fetch
            or not pending
            or not self.config.anilist.enabled
            or not str(self.config.anilist.access_token or "").strip()
            or anilist_refusal() is not None
        ):
            if pending:
                self.logger.info(
                    "SKIP step=anime.placeholder_metadata pending=%s reason=%s",
                    len(pending),
                    "no_fetch" if not fetch else "anilist_unavailable",
                )
            return result
        client = AniListClient(self.config.anilist.endpoint, access_token=self.config.anilist.access_token)
        try:
            with bulk_requests():
                for media_id in pending[: max(1, int(limit or self.PLACEHOLDER_METADATA_LIMIT))]:
                    local = self.db.get_anime(media_id)
                    if local is None or local.titles:
                        continue
                    try:
                        metadata = client.library_anime(media_id)
                    except (AniListNotFoundError, AniListHTTPError) as exc:
                        if isinstance(exc, AniListHTTPError) and exc.status_code != 404:
                            # Rate limit, refusal, server error: the next launch goes on.
                            self.logger.warning(
                                "RETRY step=anime.placeholder_metadata media_id=%s error=%r",
                                media_id,
                                str(exc)[:200],
                            )
                            break
                        missing.add(media_id)
                        result["missing"] += 1
                        self.logger.info(
                            "SKIP step=anime.placeholder_metadata media_id=%s reason=not_found", media_id
                        )
                        continue
                    except AniListError as exc:
                        # Rate limit, network, refusal: the next launch or refresh goes on.
                        self.logger.warning(
                            "RETRY step=anime.placeholder_metadata media_id=%s error=%r",
                            media_id,
                            str(exc)[:200],
                        )
                        break
                    if not metadata.titles:
                        continue
                    self._fill_placeholder_metadata(local, metadata, source="anilist.media")
                    result["fetched"] += 1
        finally:
            client.close()
            self.db.set_state(self._PLACEHOLDER_METADATA_MISSING_KEY, json.dumps(sorted(missing)))
        self.logger.info(
            "RESULT step=anime.placeholder_titles renamed=%s fetched=%s missing=%s pending=%s",
            result["renamed"],
            result["fetched"],
            result["missing"],
            max(0, result["pending"] - result["fetched"] - result["missing"]),
        )
        return result

    def refresh_off_list_entry_metadata(self, *, cache_covers: bool = False) -> int:
        """Bring rows Pudge made off the AniList list up to date from the cached relation graphs.

        An S00 special or OVA split from its series gets a row of its own (extra_files.
        own_title_anime) that the list sync never touches: rows made before it copied the
        cover kept none, so Home showed the "Cover" placeholder for days. The relation graph
        of the series is refreshed regularly and carries the entry's cover, title and
        episodes; without an AniList cover the series' cover stands in. No network but the
        cover image: it runs at launch, after a library scan and after an AniList refresh.
        ``cache_covers``: also fetch the cover file of an off-list entry with local episodes
        that has none yet (once per entry and launch). Returns rows changed.
        """
        changed = 0
        tried: set[int] = self.__dict__.setdefault("_off_list_cover_tried", set())
        for anime in self.db.anime_list():
            if str(anime.status or "").strip():
                continue  # on the list: the list sync keeps it up to date
            cached = self.db.relation_graph_for_media(int(anime.media_id))
            graph = cached.get("graph") if isinstance(cached, dict) else None
            node = None
            parent: LibraryAnime | None = None
            if isinstance(graph, dict):
                nodes = {
                    int(item["media_id"]): item
                    for item in graph.get("nodes") or []
                    if isinstance(item, dict) and item.get("media_id") is not None
                }
                node = nodes.get(int(anime.media_id))
                neighbours: list[LibraryAnime] = []
                for edge in graph.get("edges") or []:
                    if not isinstance(edge, dict):
                        continue
                    ends = {edge.get("source"), edge.get("target")}
                    if int(anime.media_id) not in ends:
                        continue
                    other = next((value for value in ends if value != int(anime.media_id)), None)
                    row = self.db.get_anime(int(other)) if other is not None else None
                    if row is not None and row.cover_url:
                        neighbours.append(row)
                # The series a special belongs to: a TV neighbour first.
                neighbours.sort(key=lambda row: str(row.format or "").upper() != "TV")
                parent = neighbours[0] if neighbours else None
            repaired = node is not None and extra_files.refresh_own_title_metadata(anime, node, parent)
            if repaired:
                self.db.upsert_anime(anime)
                changed += 1
            fetch = bool(
                anime.cover_url
                and self.cached_cover_path(anime) is None
                and (
                    repaired
                    or (
                        cache_covers
                        and int(anime.media_id) not in tried
                        and self.db.episodes(int(anime.media_id))
                    )
                )
            )
            cover_file = None
            if fetch:
                tried.add(int(anime.media_id))
                cover_file = self.cover_path(anime)  # the card's own file (and the iPhone's)
            if repaired or fetch:
                source = (
                    "none"
                    if not anime.cover_url
                    else "series"
                    if parent is not None and anime.cover_url == parent.cover_url
                    else "own"
                )
                self.logger.info(
                    "RESULT step=anime.off_list_metadata media_id=%s title=%r repaired=%s cover=%s "
                    "cover_file=%s",
                    anime.media_id,
                    anime.title,
                    repaired,
                    source,
                    "cached" if (cover_file or self.cached_cover_path(anime)) else "missing",
                )
        return changed

    def prefetch_covers(self, statuses: tuple[str, ...] = ("CURRENT", "PLANNING"), limit: int = 0) -> int:
        items = self.db.anime_list(statuses)
        count = 0
        for anime in items[: limit or None]:
            if self.cover_path(anime):
                count += 1
        return count

    def _remap_legacy_cache_path(self, path: Path | str | None) -> Path | None:
        if not path:
            return None
        raw = Path(str(path)).expanduser()
        current_root = self.config.paths.cache_dir.expanduser().resolve()
        for old_slug in LEGACY_APP_SLUGS:
            legacy_root = (current_root.parent / old_slug).expanduser().resolve()
            try:
                relative = raw.resolve().relative_to(legacy_root)
            except (OSError, RuntimeError, ValueError):
                continue
            candidate = current_root / relative
            try:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate.resolve()
            except OSError:
                continue
        return None

    @_reusing_db_connection
    def _repair_brand_moved_subtitle_selections(self) -> int:
        """Recover prepared subtitles whose absolute cache path changed on rename.

        The 0.6.57 pudge -> pudge migration moved the cache directory but
        older SQLite rows and subtitle-history JSON still contained absolute
        ``~/Library/Caches/pudge/...`` paths. A later integrity pass could
        therefore clear an otherwise valid prepared subtitle. Only paths that
        provably map from a configured legacy cache root to an existing file in
        the current cache are restored here.
        """
        repaired = 0
        for item in list(self.db.episodes()):
            mapped: Path | None = None
            source = str(item.subtitle_origin or "")

            if item.subtitle_path is not None:
                try:
                    current_valid = item.subtitle_path.is_file() and item.subtitle_path.stat().st_size > 0
                except OSError:
                    current_valid = False
                if not current_valid:
                    mapped = self._remap_legacy_cache_path(item.subtitle_path)

            if mapped is None and item.subtitle_path is None and item.media_id is not None:
                history = self.db.latest_selected_subtitle_for_media_or_filename(
                    video_path=item.video_path,
                    media_id=item.media_id,
                    episode=item.episode,
                )
                if history is None:
                    # Pre-fingerprint history from the product rename is allowed
                    # only through an exact, deterministic old-root -> new-root
                    # mapping. This is a confirmed migration path, not a basename
                    # fallback, and remains safe for legacy 0.6.x rows.
                    current_root = self.config.library.root_dir.expanduser().resolve()
                    try:
                        relative_video = item.video_path.expanduser().resolve().relative_to(current_root)
                    except (OSError, RuntimeError, ValueError):
                        relative_video = None
                    if relative_video is not None:
                        for legacy_name in LEGACY_APP_NAMES:
                            legacy_video = current_root.parent / legacy_name / relative_video
                            history = self.db.latest_selected_subtitle(legacy_video)
                            if history is not None:
                                break
                if history is not None:
                    details = history.get("details") if isinstance(history.get("details"), dict) else {}
                    final_path = str(details.get("final_path") or "").strip()
                    mapped = self._remap_legacy_cache_path(final_path)
                    source = str(history.get("source") or source or "external")
                    if mapped is None:
                        # Very old history rows may not have final_path. Only use
                        # candidate_path when it clearly points at a generated,
                        # prepared cache artifact rather than a raw Jimaku file.
                        candidate_path = str(history.get("candidate_path") or "").strip()
                        raw_candidate = Path(candidate_path) if candidate_path else None
                        if raw_candidate is not None and any(
                            part in {"playback-srt", "alass", "piecewise", "reference-piecewise"}
                            for part in raw_candidate.parts
                        ):
                            mapped = self._remap_legacy_cache_path(raw_candidate)

            if mapped is None:
                continue

            if mapped.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS:
                self.db.set_waiting_text_subtitles(item.video_path, mapped, None)
            else:
                self.db.set_subtitle_ready(
                    item.video_path,
                    mapped,
                    None,
                    origin=source or "external",
                )
                self.db.delete_subtitle_job(item.video_path)
            repaired += 1
            self.logger.info(
                "REPAIR step=subtitle.brand_cache_path video=%r media_id=%s episode=%s subtitle=%r",
                str(item.video_path),
                item.media_id,
                item.episode,
                str(mapped),
            )
        return repaired

    @_reusing_db_connection
    def _requeue_legacy_generated_subtitles(self) -> int:
        generation = "17"
        previous_generation = self.db.get_state("subtitle_validation_generation", "")
        if previous_generation == generation:
            return 0
        cache_root = self.config.paths.cache_dir.expanduser().resolve()

        # v15 changes the alignment algorithm.  Do not rebuild every cleaned
        # playback SRT: only files whose playback copy can be traced back to
        # an alignment cache are affected. This keeps the one-time migration
        # small while invalidating stale ffsubsync/ALASS results such as Bleach.
        aligned_playback_names: set[str] = set()
        if previous_generation == "14":
            for folder in ("synced", "alass", "piecewise", "reference-piecewise"):
                root = cache_root / folder
                if not root.is_dir():
                    continue
                for source in root.glob("*.srt"):
                    try:
                        stat = source.stat()
                    except OSError:
                        continue
                    for playback_generation in ("v12", "v13", "v14", "v15", "v16"):
                        digest = hashlib.sha1(
                            f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:playback-srt-{playback_generation}".encode()
                        ).hexdigest()[:20]
                        aligned_playback_names.add(f"{playback_generation}-{digest}.srt")

        queued = 0
        for item in self.db.episodes():
            subtitle = item.subtitle_path
            if subtitle is None:
                continue
            try:
                relative = subtitle.expanduser().resolve().relative_to(cache_root)
                generated = True
            except (OSError, RuntimeError, ValueError):
                generated = False
                relative = None
            if not generated:
                continue

            latest = self.db.latest_selected_subtitle(item.video_path)
            if latest and str(latest.get("source") or "").casefold() == "manual":
                continue

            # v14 changes OCR text extraction only. Users already on v13 do not
            # need every ordinary generated subtitle rebuilt; requeue just
            # OCR-derived rows (including legacy OCR lineage).
            if previous_generation == "13" and not self._is_legacy_ocr_prepared_subtitle(item):
                continue
            folder = relative.parts[0] if relative is not None and relative.parts else ""
            if previous_generation == "14":
                if folder != "playback-srt" or subtitle.name not in aligned_playback_names:
                    continue
            if previous_generation == "16":
                # v100 changes playback SRT semantics: genuine overlapping cues
                # must remain simultaneous instead of being trimmed/serialized.
                # Re-run only selections whose recorded source actually contains
                # overlaps, and force resync so cached aligned SRTs are rebuilt.
                if folder != "playback-srt" or not latest:
                    continue
                candidate_value = str(latest.get("candidate_path") or "").strip()
                candidate = Path(candidate_value).expanduser() if candidate_value else None
                if candidate is None or not candidate.is_file():
                    continue
                if not subtitle_has_genuine_overlaps(candidate):
                    continue
            if previous_generation == "15":
                # v15 could miss stale aligned playback files when the old
                # synced/ALASS source had already been pruned. Reconstruct the
                # playback filename that a direct clean of the recorded Jimaku
                # candidate would have produced. A different current filename
                # proves that an intermediate transformed/aligned SRT was used.
                if folder != "playback-srt" or not latest:
                    continue
                if str(latest.get("source") or "").casefold() != "jimaku":
                    continue
                candidate_value = str(latest.get("candidate_path") or "").strip()
                candidate = Path(candidate_value).expanduser() if candidate_value else None
                if candidate is None or not candidate.is_file():
                    continue
                try:
                    candidate_stat = candidate.stat()
                    direct_playback_names = set()
                    for playback_generation in ("v12", "v13", "v14", "v15", "v16"):
                        direct_digest = hashlib.sha1(
                            f"{candidate.resolve()}:{candidate_stat.st_size}:{candidate_stat.st_mtime_ns}:playback-srt-{playback_generation}".encode()
                        ).hexdigest()[:20]
                        direct_playback_names.add(f"{playback_generation}-{direct_digest}.srt")
                except OSError:
                    continue
                if subtitle.name in direct_playback_names:
                    continue
            if previous_generation == "2" and folder != "alass":
                continue
            if previous_generation in {"3", "4"} and folder not in {"alass", "reference-piecewise"}:
                continue
            if previous_generation == "5" and folder != "reference-piecewise":
                continue
            if previous_generation == "6" and folder not in {
                "playback-srt",
                "piecewise",
                "reference-piecewise",
            }:
                continue
            if previous_generation in {"7", "8", "9", "10", "11", "12", "13"} and folder != "playback-srt":
                continue

            # The DB selection is not the only fast path. prepare-only can also
            # return an old final-pipeline manifest, so invalidate both layers.
            if not self._requeue_rebuild(
                item, err("jobs.requeueSyncUpdate"), debug_force=previous_generation == "16"
            ):
                continue
            queued += 1
        self.db.set_state("subtitle_validation_generation", generation)
        if queued:
            self.log(f"Subtitles: rechecking previously prepared files: {queued}")
        return queued

    def _requeue_rebuild(
        self, item: LibraryEpisode, error: str, *, priority: int = 0, debug_force: bool = False
    ) -> bool:
        """Prepare an episode's subtitles again after an aligner fix (the rebuild rules).

        A Ready or Watched episode keeps playing its subtitles while they are prepared
        again: the upgrade check takes any ready result and puts the old subtitles back
        when the rebuild fails or is interrupted. Its state does not change, so a Watched
        episode stays watched. Only an episode with nothing playable to keep loses its
        selection; a Dropped one is left alone. False when nothing was queued.
        """
        state = str(getattr(item, "state", "") or "")
        if state == "dropped":
            return False
        video = item.video_path
        kept = False
        if state in {"ready", "watched"}:
            upgrade_key = self._subtitle_upgrade_state_key(video)
            raw = self.db.get_state(upgrade_key, "").strip()
            try:
                request = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                request = None
            if isinstance(request, dict):
                if request.get("fresh_rebuild"):
                    return False  # already being prepared again from scratch
                # A pending upgrade check becomes the rebuild: any ready result replaces.
                request.update(
                    forced=True,
                    manual_retry=True,
                    fresh_rebuild=True,
                    rebuild_rule=True,
                    requeue_priority=max(priority, _upgrade_request_priority(request, 0)),
                    requeue_reason=error,
                )
                self.db.set_state(upgrade_key, json.dumps(request, ensure_ascii=False))
                kept = True
            else:
                kept = self._keep_subtitles_during_rebuild(
                    item, fresh=True, priority=priority, reason=error, rebuild_rule=True
                )
        invalidate_final_pipeline_result(video, self.config)
        if kept:
            # A new job generation: a check already running for it no longer commits.
            self.db.invalidate_subtitle(
                video, item.media_id, item.episode, error, keep_selection=True, priority=priority
            )
        else:
            self.db.invalidate_subtitle(video, item.media_id, item.episode, error)
            if priority:
                self.db.queue_subtitle_job(video, item.media_id, item.episode, priority=priority, error=error)
        if debug_force:
            self.db.set_state(self._debug_force_subtitle_state_key(video), "1")
        return True

    @_reusing_db_connection
    def _run_subtitle_rebuild_rules(self) -> dict[str, int]:
        """The rebuild rules that have work, in one pass (see ``subtitle_migrations``)."""
        return subtitle_migrations.run_pending_rules(self)

    # One method per rebuild rule (pudge/subtitle_migrations.py): each runs that rule
    # over every prepared episode now.

    @_reusing_db_connection
    def _requeue_false_positive_tail_clocks(self) -> int:
        """Rebuild Jimaku selections with an unsupported large late clock jump (once)."""
        return subtitle_migrations.run_rule(self, "tail_clock")

    @_reusing_db_connection
    def _requeue_shared_opening_timeline_upgrade(self) -> int:
        """Rebuild v6.19 fits that retained a weak post-opening clock (and unwatched older
        ones whose later clock already fit the minute after the opening better)."""
        return subtitle_migrations.run_rule(self, "shared_opening") + subtitle_migrations.run_rule(
            self, "shared_opening_before_v6_19"
        )

    @_reusing_db_connection
    def _requeue_early_crossover_timeline_upgrade(self) -> int:
        """Rebuild pre-v6.21 fits whose clock change came from the first local crossover."""
        return subtitle_migrations.run_rule(self, "early_crossover")

    @_reusing_db_connection
    def _requeue_bridged_singleton_tail_timeline_upgrade(self) -> int:
        """Rebuild pre-v6.22 fits whose last clock was one window reached through a bridge."""
        return subtitle_migrations.run_rule(self, "bridged_singleton_tail")

    @_reusing_db_connection
    def _requeue_speech_maps_without_late_cut(self) -> int:
        """Prepare again speech-verified fits that dropped a late cut of the timeline."""
        return subtitle_migrations.run_rule(self, "speech_late_cut")

    @_reusing_db_connection
    def _requeue_unverified_timeline_edits(self) -> int:
        """Rebuild unverified timeline edits once Japanese STT can verify them."""
        return subtitle_migrations.run_rule(self, "unverified_timeline_edit")

    @_reusing_db_connection
    def _requeue_local_edge_timeline_upgrade(self) -> int:
        """Rebuild pre-v6.19 timelines with a large unverified opening-edge mismatch (once)."""
        return subtitle_migrations.run_rule(self, "local_edge")

    @_reusing_db_connection
    def _requeue_decreasing_gap_timeline_upgrade(self) -> int:
        """Rebuild pre-v6.17 timelines whose monotonic repair dragged a clock switch (once)."""
        return subtitle_migrations.run_rule(self, "decreasing_gap")

    @_reusing_db_connection
    def _requeue_stt_timeline_clock_conflicts(self) -> int:
        """Rebuild selections where STT beat a strong same-video timeline clock (once)."""
        return subtitle_migrations.run_rule(self, "stt_timeline_conflict")

    @_reusing_db_connection
    def _requeue_stt_opening_plateau_semantic_upgrade(self) -> int:
        """Rebuild old STT opening plateaus that had onset-only local clocks (once)."""
        return subtitle_migrations.run_rule(self, "stt_opening_plateau")

    @_reusing_db_connection
    def _requeue_double_opening_scaffold(self) -> int:
        """Rebuild cold opens shifted twice: by ALASS at the OP and by the scaffold (once)."""
        return subtitle_migrations.run_rule(self, "double_opening_scaffold")

    @_reusing_db_connection
    def _requeue_unsafe_stt_transition_maps(self) -> int:
        """Rebuild STT selections containing a large unsupported clock jump (once)."""
        return subtitle_migrations.run_rule(self, "unsafe_stt_transition")

    @_reusing_db_connection
    def _requeue_opening_gap_timeline_upgrade(self) -> int:
        """Rebuild risky v5.9 opening-gap maps with the cut-aware clock reacquire (once)."""
        return subtitle_migrations.run_rule(self, "opening_gap")

    @_reusing_db_connection
    def _requeue_large_cold_open_subtitles(self) -> int:
        """Rebuild subtitles affected by cold-open or sparse-gap timeline fixes (once)."""
        return subtitle_migrations.run_rule(self, "large_cold_open")

    @_reusing_db_connection
    def _requeue_known_source_repairs(self) -> int:
        """Rebuild selected subtitles covered by newly added narrow source repairs (once)."""
        return subtitle_migrations.run_rule(self, "known_source_repair")

    @_reusing_db_connection
    def _requeue_late_audio_tracks(self) -> int:
        """Prepare again unwatched episodes whose audio track starts after the file start (once)."""
        return subtitle_migrations.run_rule(self, "late_audio_track")

    @_reusing_db_connection
    def _requeue_audio_gap_tracks(self) -> int:
        """Prepare again unwatched episodes whose audio track has a timestamp gap inside (once)."""
        return subtitle_migrations.run_rule(self, "audio_gap_track")

    @_reusing_db_connection
    def _requeue_imprecise_timeline_clocks(self) -> int:
        """Prepare again unwatched Jimaku episodes timed on the window grid or with a
        Japanese+English/Chinese file (once; manual selections are left alone)."""
        return subtitle_migrations.run_rule(self, "precise_clocks")

    @_reusing_db_connection
    def _requeue_non_dialogue_text(self) -> int:
        """Prepare again unwatched episodes whose subtitles show ASS drawings or release
        credits as text (once; manual selections are left alone)."""
        return subtitle_migrations.run_rule(self, "non_dialogue_text")

    @_reusing_db_connection
    def _requeue_small_cuts(self) -> int:
        """Prepare again unwatched episodes timed to an English (or other non-Japanese) track
        on clocks a few seconds apart, before the small-cut step (once)."""
        return subtitle_migrations.run_rule(self, "small_cuts")

    @_reusing_db_connection
    def _requeue_staircase_speech(self) -> int:
        """Prepare again unwatched episodes timed to an English (or other non-Japanese) track
        on clocks a few seconds apart, before the Japanese speech check of such maps (once)."""
        return subtitle_migrations.run_rule(self, "staircase_speech")

    @_reusing_db_connection
    def _requeue_after_resolver_upgrade(self) -> int:
        generation = "9"
        key = "subtitle_resolver_generation"
        previous_generation = self.db.get_state(key, "")
        if previous_generation == generation:
            return 0
        queued = self.db.reset_pending_subtitle_jobs()
        self.db.set_state(key, generation)
        self.logger.info(
            "REQUEUE step=subtitle.resolver_upgrade previous=%r current=%s jobs=%s",
            previous_generation,
            generation,
            queued,
        )
        if queued:
            self.log(f"Subtitles: searching again after the resolver update: {queued}")
        return queued

    def _maintenance_stats(self) -> dict[str, int]:
        return {
            "anime": 0,
            "covers": 0,
            "anilist": 0,
            "library": 0,
            "downloads": 0,
            "torrent_duplicates": 0,
            "auto": 0,
            "upgrades": 0,
            "replaced": 0,
            "subs": 0,
            "deleted": 0,
            "disk_deleted": 0,
            "score_tags_removed": 0,
            "unused_tags_deleted": 0,
        }

    def _sync_downloads_for_stats(self, stats: dict[str, int]) -> None:
        # The error is caught inside the timed step: raised through it, a stopped torrent
        # client wrote an ERROR line with a full traceback on every maintenance run.
        unavailable: QBittorrentError | ManagerError | None = None
        with timed_step(self.logger, "qbittorrent.sync"):
            try:
                stats["downloads"] = self.sync_downloads()
            except (QBittorrentError, ManagerError) as exc:
                unavailable = exc
        if unavailable is not None:
            exc = unavailable
            # Backend observation is advisory for maintenance. A temporarily
            # unavailable torrent client must not abort release-feed polling,
            # subtitle work, or the later automatic release search.
            self.log(str(exc))
            self.logger.warning(
                "FALLBACK step=torrent.sync_backend reason=unavailable continue=maintenance error=%r",
                str(exc),
            )

    def run_startup_once(self) -> dict[str, int]:
        """Run one startup maintenance pass unless recent user work already did it."""
        with maintenance_lock(self.config.paths.cache_dir, blocking=False) as acquired:
            if not acquired:
                self.logger.info("SKIP step=maintenance.total mode=startup reason=another_process_active")
                return self._maintenance_stats()
            raw_interactive = self.db.get_state("interactive_refresh_completed_at_v39", "")
            try:
                interactive = json.loads(raw_interactive) if raw_interactive else {}
            except (TypeError, ValueError, json.JSONDecodeError):
                interactive = {}
            try:
                last_interactive = float(interactive.get("completed_at") or 0.0)
                interactive_pid = int(interactive.get("pid") or 0)
            except (AttributeError, TypeError, ValueError):
                last_interactive = 0.0
                interactive_pid = 0
            age = time.time() - last_interactive if last_interactive else float("inf")
            if interactive_pid == os.getpid() and 0.0 <= age <= 5 * 60:
                stats = self._maintenance_stats()
                stats["library"] = len(self._cached_library_items())
                self.logger.info(
                    "SKIP step=maintenance.total mode=startup "
                    "reason=recent_interactive_refresh age_s=%.1f rows=%s",
                    age,
                    stats["library"],
                )
                return stats
            return self._run_startup_once_unlocked()

    def _run_startup_once_unlocked(self) -> dict[str, int]:
        """Run the full maintenance pipeline at launch, releases first.

        Startup used to skip the normal subtitle backlog whenever the background
        agent was enabled; it runs the whole pass now, and due subtitle jobs are
        left to the window's worker. It is a normal pass, not a forced Refresh:
        each launch made every waiting job due at once and dropped the Jimaku
        answers, so dozens of titles without Jimaku subtitles were prepared again
        on every launch, past their 6 h and daily backoffs.
        """
        self.logger.info("START step=maintenance.total mode=startup-fast-refresh")
        stats = self._run_once_unlocked(
            force_subtitle_retry=False,
            prioritize_release_search=True,
            defer_subtitle_processing=True,
        )
        self.logger.info("SUMMARY mode=startup-fast-refresh stats=%s", stats)
        return stats

    def run_interactive_refresh(
        self,
        *,
        pre_scanned_library_count: int | None = None,
    ) -> dict[str, int]:
        """Refresh user-visible availability without waiting on subtitle alignment.

        One click must be enough even if another process is already preparing
        subtitles.  Existing ``processing`` rows are never reset while the
        heavyweight maintenance lock is owned elsewhere; we only raise their
        priority so the UI can keep showing that the requested check is still
        active.  Pending rows are made due immediately and foreground polling
        drains any leftovers once the lock becomes available.
        """
        with maintenance_lock(self.config.paths.cache_dir, blocking=False) as acquired:
            if acquired:
                stats = self._run_once_unlocked(
                    force_subtitle_retry=True,
                    prioritize_release_search=True,
                    defer_subtitle_processing=True,
                    pre_scanned_library_count=pre_scanned_library_count,
                )
                stats["subtitle_check_queued"] = self.db.priority_subtitle_job_count(min_priority=200)
                self.db.set_state(
                    "interactive_refresh_completed_at_v39",
                    json.dumps(
                        {"pid": os.getpid(), "completed_at": time.time()},
                        separators=(",", ":"),
                    ),
                )
                return stats
        self.logger.info("FALLBACK step=maintenance.interactive reason=heavy_maintenance_active")
        stats = self._maintenance_stats()
        if pre_scanned_library_count is not None:
            stats["library"] = max(0, int(pre_scanned_library_count))
        forced_jobs = self.db.force_requeue_unresolved_subtitle_jobs(priority=200, recover_processing=False)
        remaining = self.db.priority_subtitle_job_count(min_priority=200)
        stats["subtitle_check_queued"] = remaining
        self.logger.info(
            "REQUEUE step=subtitle.interactive jobs=%s remaining=%s priority=200 preserve_processing=true",
            forced_jobs,
            remaining,
        )
        with (
            timed_step(self.logger, "nyaa.auto_search", priority="interactive_concurrent"),
            self.ignore_auto_search_backoff(),
        ):
            stats["auto"] = self.auto_search_current()
        return stats

    def _clear_jimaku_api_cache(self) -> int:
        """Expire short-lived API responses so a manual refresh is truly fresh.

        The responses stay as the offline copy the client falls back to when Jimaku
        does not answer (deleted, a timeout after a fresh rebuild left nothing).
        """
        cache_dir = self.config.paths.cache_dir / "jimaku-api"
        expired = 0
        if not cache_dir.is_dir():
            return 0
        for path in cache_dir.glob("*.json"):
            if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                continue  # rate-limit, request-budget and key-rejection notes are not responses
            try:
                os.utime(path, (0, 0))
                expired += 1
            except OSError:
                continue
        return expired

    def run_once(
        self,
        *,
        force_subtitle_retry: bool = False,
        wait_for_maintenance: bool = False,
    ) -> dict[str, int]:
        if wait_for_maintenance:
            self.logger.info(
                "WAIT step=maintenance.total mode=manual-refresh reason=another_process_may_be_active"
            )
        with maintenance_lock(
            self.config.paths.cache_dir,
            blocking=wait_for_maintenance,
        ) as acquired:
            if not acquired:
                self.logger.info("SKIP step=maintenance.total mode=regular reason=another_process_active")
                return self._maintenance_stats()
            return self._run_once_unlocked(
                force_subtitle_retry=force_subtitle_retry,
                prioritize_release_search=bool(force_subtitle_retry and wait_for_maintenance),
            )

    HISTORY_KEEP_SECONDS = 5 * 86400

    def prune_history_if_due(self, *, now: float | None = None) -> dict[str, int]:
        """Once a day: drop 5-day-old history rows, debug JSONL and 14-day-old one-off library copies."""
        current = time.time() if now is None else float(now)
        if not callable(getattr(self.db, "get_state", None)) or not callable(
            getattr(self.db, "prune_history", None)
        ):
            return {}
        try:
            last = float(self.db.get_state("history_pruned_at", "0") or 0)
        except (TypeError, ValueError):
            last = 0.0
        if current - last < 86400:
            return {}
        self.db.set_state("history_pruned_at", str(current))
        cutoff = current - self.HISTORY_KEEP_SECONDS
        try:
            removed = self.db.prune_history(now=current, keep_seconds=self.HISTORY_KEEP_SECONDS)
        except Exception as exc:  # noqa: BLE001 - pruning never blocks maintenance
            self.logger.warning("SKIP step=history.prune reason=%r", exc)
            removed = {}
        debug_root = Path(self.config.paths.cache_dir) / "subtitle-debug"
        removed_files = 0
        try:
            for path in debug_root.rglob("*.jsonl") if debug_root.is_dir() else ():
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed_files += 1
                except OSError:
                    continue
        except OSError:
            pass
        removed["subtitle_debug_files"] = removed_files
        # The two newest pre-v<N> copies used to stay forever and identity-repair
        # copies were never removed: ~1 GB each beside a library that is mostly
        # cache. They now go after 14 days (backup.prune_one_off_copies).
        removed["one_off_backups"] = len(
            prune_one_off_copies(Path(self.config.library.database_path), now=current)
        )
        self.logger.info(
            "RESULT step=history.prune %s",
            " ".join(f"{key}={value}" for key, value in sorted(removed.items())),
        )
        return removed

    def _kick_anime_mappings_refresh(self) -> None:
        """Daily, off-thread refresh of the external-ID index (never blocks maintenance)."""
        try:
            from .providers.anime_mappings import AnimeMappings, refresh_in_background

            mappings = getattr(self, "_anime_mappings", None)
            if mappings is None:
                mappings = self._anime_mappings = AnimeMappings(self.config.paths.cache_dir)
            refresh_in_background(mappings, logger=self.logger)
        except Exception as exc:  # noqa: BLE001 - optional metadata must never break maintenance
            self.logger.warning("FAIL step=anime_mappings.kick error=%r", str(exc)[:200])

    # One SQLite connection for the whole pass (DB-7): its per-episode loops made a
    # fresh connection (~90x the query) for every small call.
    @_reusing_db_connection
    def _run_once_unlocked(
        self,
        *,
        force_subtitle_retry: bool = False,
        prioritize_release_search: bool = False,
        defer_subtitle_processing: bool = False,
        pre_scanned_library_count: int | None = None,
    ) -> dict[str, int]:
        stats = self._maintenance_stats()
        self._kick_anime_mappings_refresh()
        self.prune_history_if_due()
        # AniList is intentionally not refreshed by the periodic/local refresh.
        # It is synchronized once when the app opens or explicitly from Settings.
        with timed_step(self.logger, "maintenance.total", mode="regular-subtitles-first"):
            with timed_step(self.logger, "release_feeds.poll"):
                stats["release_feed_items"] = self.poll_release_feeds()
            with timed_step(self.logger, "subtitle.requeue_legacy"):
                brand_path_repaired = self._repair_brand_moved_subtitle_selections()
                if brand_path_repaired:
                    self.logger.info(
                        "REPAIR step=subtitle.brand_cache_paths rows=%s",
                        brand_path_repaired,
                    )
                bitmap_repaired = self.db.repair_bitmap_ready_rows()
                if bitmap_repaired:
                    self.logger.info(
                        "REPAIR step=subtitle.bitmap_ready rows=%s",
                        bitmap_repaired,
                    )
                restored_upgrades = self._restore_interrupted_subtitle_upgrades()
                upgrade_paths = self._active_subtitle_upgrade_paths()
                preserved = self.db.repair_spurious_ready_subtitle_jobs(preserve_paths=upgrade_paths)
                if preserved:
                    self.logger.info(
                        "REPAIR step=subtitle.spurious_ready_jobs removed=%s",
                        preserved,
                    )
                durable_migrated = self._persist_ready_subtitle_selections(preserve_paths=upgrade_paths)
                if durable_migrated:
                    self.logger.info(
                        "REPAIR step=subtitle.persist_ready migrated=%s",
                        durable_migrated,
                    )
                repaired = self.db.repair_stale_subtitle_selections(preserve_paths=upgrade_paths)
                if repaired:
                    self.logger.info(
                        "REPAIR step=subtitle.stale_selection cleared=%s",
                        repaired,
                    )
                if restored_upgrades:
                    self.logger.info(
                        "REPAIR step=subtitle.upgrade_restore rows=%s",
                        restored_upgrades,
                    )
                if not self.config.matching.ocr_image_subtitles:
                    self.invalidate_disabled_ocr_subtitles()
                else:
                    self._reconcile_ocr_readiness_policy()
                # Rebuild rules: each records when it is done (and the newest selection it
                # has seen), so a pass with nothing new reads a few state rows only.
                self._requeue_legacy_generated_subtitles()
                self._run_subtitle_rebuild_rules()
                self._requeue_after_resolver_upgrade()
            self._sync_downloads_for_stats(stats)
            with timed_step(self.logger, "qbittorrent.duplicate_cleanup"):
                try:
                    stats["torrent_duplicates"] = self.cleanup_duplicate_torrents()
                except QBittorrentError as exc:
                    self.log(str(exc))
            if pre_scanned_library_count is None:
                with timed_step(self.logger, "library.scan", priority="before_subtitles"):
                    stats["library"] = len(self.scan_library(reuse_unchanged=True))
            else:
                stats["library"] = max(0, int(pre_scanned_library_count))
                self.logger.info(
                    "SKIP step=library.scan reason=already_scanned_in_refresh rows=%s",
                    stats["library"],
                )
            with timed_step(self.logger, "subtitle.inbox"):
                inbox = self.scan_subtitle_inbox()
                stats["inbox_requeued"] = int(inbox.get("requeued", 0) or 0)
            with timed_step(self.logger, "library.integrity_if_due"):
                integrity = self.repair_library_if_due()
                stats["integrity_repairs"] = (
                    sum(int(value or 0) for value in integrity.values()) if integrity else 0
                )
            with timed_step(self.logger, "subtitle.upgrade_schedule"):
                stats["subtitle_upgrades"] = self.schedule_subtitle_upgrades()
            subtitle_limit = 8
            if force_subtitle_retry:
                cleared_cache = self._clear_jimaku_api_cache()
                forced_jobs = self.db.force_requeue_unresolved_subtitle_jobs(
                    priority=200 if defer_subtitle_processing else 20
                )
                subtitle_limit = max(8, min(forced_jobs, 32))
                self.logger.info(
                    "REQUEUE step=subtitle.manual_refresh jobs=%s limit=%s jimaku_cache_cleared=%s",
                    forced_jobs,
                    subtitle_limit,
                    cleared_cache,
                )
            # Manual Refresh is an explicit request to discover newly available
            # releases. Do that before potentially expensive subtitle alignment
            # (which can take minutes for a movie) so Nyaa is never hidden behind
            # a long ALASS/LLM job. Periodic agent runs keep subtitles-first.
            if prioritize_release_search:
                with (
                    timed_step(self.logger, "nyaa.auto_search", priority="manual_before_subtitles"),
                    # Refresh is the user's request: episodes that keep missing are searched too.
                    self.ignore_auto_search_backoff() if force_subtitle_retry else nullcontext(),
                ):
                    stats["auto"] = self.auto_search_current()
            if defer_subtitle_processing:
                stats["subs"] = 0
                stats["subtitle_check_queued"] = self.db.priority_subtitle_job_count(min_priority=200)
                self.logger.info(
                    "QUEUE step=subtitle.process deferred=true limit=%s reason=interactive_refresh",
                    subtitle_limit,
                )
            else:
                with timed_step(
                    self.logger,
                    "subtitle.process",
                    priority="manual_after_nyaa" if prioritize_release_search else "before_nyaa",
                    limit=subtitle_limit,
                ):
                    stats["subs"] = self.process_subtitle_jobs(limit=subtitle_limit)
            stats["anilist"] = self.refresh_anilist_if_due()
            if not prioritize_release_search:
                with timed_step(self.logger, "nyaa.auto_search", priority="missing_after_subtitles"):
                    stats["auto"] = self.auto_search_current()
            with timed_step(self.logger, "nyaa.upgrade_search", priority="after_missing"):
                stats["upgrades"] = self.auto_upgrade_downloaded()
            with timed_step(self.logger, "nyaa.upgrade_finalize"):
                stats["replaced"] = self.finalize_ready_upgrades()
                stats["replaced"] += self.reconcile_duplicate_versions()
            with timed_step(self.logger, "cleanup"):
                stats["deleted"] = self.cleanup()
            with timed_step(self.logger, "storage.enforce"):
                stats["disk_deleted"] = self.enforce_disk_limit()
            with timed_step(self.logger, "cache.derived"):
                stats["cache_deleted"] = self.enforce_derived_caches()
            with timed_step(self.logger, "database.storage"):
                stats["db_compacted"] = self.maintain_database_storage()
            with timed_step(self.logger, "qbittorrent.tag_cleanup"):
                try:
                    stats.update(self.cleanup_qbittorrent_tags())
                except QBittorrentError as exc:
                    self.log(str(exc))
            # Lowest priority: only after everything the user waits for.
            with timed_step(self.logger, "intro_skipper.pass"):
                stats["intro_segments"] = self.run_intro_detection()
        self.db.set_state("agent_last_run", str(time.time()))
        self.logger.info("SUMMARY mode=regular-subtitles-first stats=%s", stats)
        return stats
