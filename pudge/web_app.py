from __future__ import annotations

import html
import http.server
import json
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import __version__
from .audiobooks import AudiobookService
from .backup import create_backup, restore_backup
from .branding import APP_BUNDLE_ID, APP_NAME, APP_SLUG
from .config import load_config, write_config
from .debug_snapshot import DebugSnapshotService
from .energy_diagnostics import ENERGY_LOG_PATH, EnergyDiagnosticsMonitor
from .episode_numbering import resolve_episode_numbering
from .presentation_state import derive_episode_presentation
from .first_experience import (
    configure_mpv_study_keys,
    dependency_status,
    install_jiten_mpv,
    install_media_tools,
    mpv_study_status,
)
from .jimaku_trial import apply_jimaku_trial
from .job_center import JobCenter
from .language import IMAGE_SUBTITLE_EXTENSIONS
from .light_novels import LightNovelError, LightNovelService
from .logging_utils import DEFAULT_LOG_PATH, configure_logging, tail_log, timed_step
from .maintenance_lock import maintenance_lock
from .manager import AnimeManager
from .manager_models import LibraryAnime, NyaaRelease
from .manga import MangaService
from .metadata_cache import MetadataCache
from .notifications import send_native_notification
from .permissions import request_folder_access, request_notification_permission
from .providers.anilist import AniListClient
from .providers.aria2 import Aria2Client
from .providers.nyaa import NyaaClient
from .providers.qbittorrent import QBittorrentClient
from .runtime import python_executable
from .updater import AppUpdater
from .visual_novels import VisualNovelService


def _plain_anilist_description(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)<br\s*/?>|</p\s*>|</li\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


class WebAppApi:
    """Local bridge used by the WKWebView application.

    Read methods only access SQLite and the filesystem. AniList is synchronized
    once after the window opens and can also be refreshed explicitly in Settings.
    """

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.expanduser()
        self.config = load_config(self.config_path)
        self.logger = configure_logging()
        self.manager = AnimeManager(self.config, log=self.logger.info)
        self.job_center = JobCenter(self.manager.db)
        self.light_novels = LightNovelService(self.config, logger=self.logger)
        self.manga = MangaService(
            self.manager.db,
            cache_dir=self.config.paths.cache_dir,
            python=python_executable(),
            work_scheduler=self.manager.work_scheduler,
        )
        self.audiobooks = AudiobookService(
            self.manager.db,
            ffprobe=self.config.tools.ffprobe,
            mpv=self.config.tools.mpv,
            cache_dir=self.config.paths.cache_dir,
            ffmpeg=self.config.tools.ffmpeg,
            python=python_executable(),
            stt_model=self.config.sync.japanese_stt_model,
            job_center=self.job_center,
            work_scheduler=self.manager.work_scheduler,
        )
        threading.Thread(
            target=self.audiobooks.resume_pending_transcriptions,
            name="audiobook-stt-resume",
            daemon=True,
        ).start()
        self.visual_novels = VisualNovelService(logger=self.logger)
        self._planning_search_cache = MetadataCache(
            self.config.paths.cache_dir,
            "anilist-planning-search",
            schema="v2",
        )
        self.app_updater = AppUpdater(logger=self.logger)
        self.debug_snapshots = DebugSnapshotService(
            self.manager,
            cache_dir=self.config.paths.cache_dir,
            runtime_log_path=DEFAULT_LOG_PATH,
        )
        self.logger.info("APP session_start version=%s platform=%s", __version__, platform.platform())
        self.window: Any | None = None
        self.asset_base = ""
        self._play_processes: dict[str, subprocess.Popen[Any]] = {}
        self._play_started_at: dict[str, float] = {}
        self._play_exit_codes: dict[str, tuple[int, float]] = {}
        self._play_lock = threading.Lock()
        self._anilist_sync_lock = threading.Lock()
        self._local_refresh_lock = threading.Lock()
        self._startup_maintenance_lock = threading.Lock()
        self._download_poll_lock = threading.Lock()
        self._fullscreen_exit_lock = threading.Lock()
        self._fullscreen_exit_pending = False
        self._manga_ocr_install_lock = threading.Lock()
        self._manga_ocr_install_thread: threading.Thread | None = None
        self._manga_ocr_install_state: dict[str, Any] = {
            "state": "idle",
            "detail": "",
            "started_at": 0.0,
            "finished_at": 0.0,
        }
        self._manga_book_ocr_lock = threading.Lock()
        self._manga_ocr_run_lock = threading.Lock()
        self._manga_book_ocr_threads: dict[int, threading.Thread] = {}
        self._manga_book_ocr_state: dict[int, dict[str, Any]] = {}
        self._manga_ocr_cancel_events: dict[int, threading.Event] = {}
        self._manga_ocr_job_ids: dict[int, str] = {}
        self._planning_episode_download_lock = threading.Lock()
        self._planning_episode_download_thread: threading.Thread | None = None
        self._planning_episode_cancel_event: threading.Event | None = None
        self._planning_episode_job_id = ""
        self._import_cancel_events: dict[str, threading.Event] = {}
        self._planning_episode_download_state: dict[str, Any] = {
            "status": "idle",
            "running": False,
            "media_id": None,
            "title": "",
            "total": 0,
            "current": 0,
            "episodes": [],
            "started_at": 0.0,
            "finished_at": 0.0,
        }
        self._startup_anilist_sync_done = False
        self._startup_maintenance_done = False
        self._startup_maintenance_thread: threading.Thread | None = None
        self._startup_maintenance_stats: dict[str, Any] = {}
        self._startup_maintenance_error = ""
        self._last_storage_status: dict[str, int | float | bool] | None = None
        self._episode_offset_cache: dict[int, int] = {}
        self.energy_monitor = EnergyDiagnosticsMonitor(
            interval_seconds=self.config.diagnostics.energy_sample_seconds,
            logger=self.logger,
        )
        if self.config.diagnostics.energy_monitoring_enabled:
            self.energy_monitor.start()

    def close(self) -> None:
        self.energy_monitor.stop()
        self.audiobooks.stop_all()
        self.visual_novels.stop()

    def visual_novel_windows(self) -> list[dict[str, Any]]:
        return self.visual_novels.windows()

    def visual_novel_state(self) -> dict[str, Any]:
        return self.visual_novels.state()

    def visual_novel_start(self, window_id: int, title: str = "") -> dict[str, Any]:
        return self.visual_novels.start(int(window_id), str(title or ""))

    def visual_novel_stop(self) -> dict[str, Any]:
        return self.visual_novels.stop()

    def visual_novel_parse(self, text: str) -> dict[str, Any]:
        return self.light_novels.parse_study_text(str(text or ""))

    def open_screen_recording_settings(self) -> dict[str, Any]:
        subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"])
        return {"ok": True}

    def open_developer_tools_settings(self) -> dict[str, Any]:
        if sys.platform != "darwin":
            return {"ok": False, "supported": False}
        subprocess.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_DeveloperTool",
        ])
        mpv = dependency_status(
            mpv=self.config.tools.mpv,
            ffmpeg=self.config.tools.ffmpeg,
        ).get("mpv", {})
        return {
            "ok": True,
            "supported": True,
            "pudge_app": str(Path.home() / "Applications" / f"{APP_SLUG}.app"),
            "mpv": str(mpv.get("path") or self.config.tools.mpv),
        }

    def set_window(self, window: Any) -> None:
        self.window = window

    def _downloads_enabled(self) -> bool:
        checker = getattr(self.manager, "downloads_enabled", None)
        if callable(checker):
            return bool(checker())
        qbt = bool(getattr(getattr(self.config, "qbittorrent", None), "enabled", False))
        aria2 = bool(getattr(getattr(self.config, "aria2", None), "enabled", False))
        return qbt or aria2

    @staticmethod
    def _remaining(target: int | None) -> int | None:
        return max(0, int(target) - int(time.time())) if target else None

    def _cover_uri(self, anime: LibraryAnime) -> str:
        path = self.manager.cached_cover_path(anime)
        if path is None or not self.asset_base:
            return ""
        return f"{self.asset_base}/covers/{quote(path.name)}"

    @staticmethod
    def _relation_level(
        items: list[dict[str, Any]],
        relation_type: str,
        *,
        excluded: set[int],
        limit: int | None = 2,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen = set(excluded)
        for item in items:
            if item.get("relation_type") != relation_type:
                continue
            try:
                media_id = int(item.get("media_id"))
            except (TypeError, ValueError):
                continue
            if media_id in seen:
                continue
            seen.add(media_id)
            result.append(item)
            if limit is not None and len(result) >= limit:
                break
        return result

    @staticmethod
    def _release_bounds(
        item: LibraryAnime | dict[str, Any],
    ) -> tuple[tuple[int, int, int], tuple[int, int, int], int] | None:
        if isinstance(item, LibraryAnime):
            start_date = item.start_date
            season_year = item.season_year
            media_id = item.media_id
        else:
            start_date = item.get("start_date")
            season_year = item.get("season_year")
            media_id = item.get("media_id")
        if start_date:
            try:
                parts = [int(value) for value in str(start_date).split("-")]
                year = parts[0]
                if len(parts) >= 3:
                    point = (year, parts[1], parts[2])
                    return point, point, int(media_id or 0)
                if len(parts) == 2:
                    month = parts[1]
                    return (year, month, 1), (year, month, 31), int(media_id or 0)
                return (year, 1, 1), (year, 12, 31), int(media_id or 0)
            except (TypeError, ValueError, IndexError):
                pass
        if season_year:
            try:
                year = int(season_year)
                return (year, 1, 1), (year, 12, 31), int(media_id or 0)
            except (TypeError, ValueError):
                pass
        return None

    @classmethod
    def _release_key(
        cls, item: LibraryAnime | dict[str, Any]
    ) -> tuple[int, int, int, int] | None:
        bounds = cls._release_bounds(item)
        if bounds is None:
            return None
        earliest, latest, media_id = bounds
        # Midpoint is only a stable sorting key. Classification uses interval
        # comparison below, so uncertain dates do not override PREQUEL/SEQUEL.
        if earliest == latest:
            return (*earliest, media_id)
        if earliest[0] == latest[0] and earliest[1] == latest[1]:
            return (earliest[0], earliest[1], 15, media_id)
        return (earliest[0], 7, 1, media_id)

    @classmethod
    def _release_direction(
        cls, root: LibraryAnime, item: dict[str, Any]
    ) -> int | None:
        root_bounds = cls._release_bounds(root)
        item_bounds = cls._release_bounds(item)
        if root_bounds is None or item_bounds is None:
            return None
        root_earliest, root_latest, _ = root_bounds
        item_earliest, item_latest, _ = item_bounds
        if item_latest < root_earliest:
            return -1
        if item_earliest > root_latest:
            return 1
        return None

    @staticmethod
    def _relation_nodes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[int] = set()

        def visit(nodes: list[dict[str, Any]]) -> None:
            for node in nodes:
                try:
                    media_id = int(node.get("media_id"))
                except (TypeError, ValueError):
                    continue
                if media_id not in seen:
                    seen.add(media_id)
                    result.append(node)
                children = node.get("relations")
                if isinstance(children, list):
                    visit(children)

        visit(items)
        return result

    @staticmethod
    def _relation_stages(items: list[dict[str, Any]], *, before: bool) -> list[list[dict[str, Any]]]:
        if before:
            selected = items[-4:]
            stages = [selected[max(0, len(selected) - 4):max(0, len(selected) - 2)], selected[-2:]]
        else:
            selected = items[:4]
            stages = [selected[:2], selected[2:4]]
        return [stage for stage in stages if stage]

    def _relation_payload(self, anime: LibraryAnime) -> dict[str, Any]:
        current = {
            "media_id": anime.media_id,
            "title": anime.title,
            "site_url": anime.site_url,
            "format": anime.format,
            "season_year": anime.season_year,
            "start_date": anime.start_date,
            "studio": anime.studio,
            "episodes": anime.episodes,
            "cover_url": self._cover_uri(anime),
            "media_status": anime.media_status or "",
            "list_status": anime.status,
            "progress": anime.progress,
            "watched": anime.status in {"COMPLETED", "REPEATING"}
            or bool(anime.episodes and anime.progress >= anime.episodes),
        }

        if self.config.anilist.relations_by_release_date:
            root_key = self._release_key(anime)
            if root_key is not None:
                before: list[dict[str, Any]] = []
                after: list[dict[str, Any]] = []
                for item in self._relation_nodes(anime.relations):
                    try:
                        media_id = int(item.get("media_id"))
                    except (TypeError, ValueError):
                        continue
                    if media_id == int(anime.media_id):
                        continue
                    relation_type = str(item.get("relation_type") or "").upper()
                    direction = self._release_direction(anime, item)
                    if direction is not None:
                        (before if direction < 0 else after).append(item)
                        continue
                    # Partial dates can overlap (for example a year-only root and
                    # an exact summer sequel). In that uncertainty window, AniList
                    # PREQUEL/SEQUEL is stronger than an invented midpoint.
                    if relation_type == "PREQUEL":
                        before.append(item)
                    elif relation_type == "SEQUEL":
                        after.append(item)
                    else:
                        item_key = self._release_key(item)
                        if item_key is not None:
                            (before if item_key < root_key else after).append(item)
                before.sort(key=lambda item: self._release_key(item) or (0, 0, 0, int(item.get("media_id") or 0)))
                after.sort(key=lambda item: self._release_key(item) or (9999, 12, 31, int(item.get("media_id") or 0)))
                if before or after:
                    return {
                        "current": current,
                        "prequel_levels": self._relation_stages(before, before=True),
                        "sequel_levels": self._relation_stages(after, before=False),
                        "full_prequel_levels": [[item] for item in before],
                        "full_sequel_levels": [[item] for item in after],
                        "order_mode": "release",
                    }

        root_id = int(anime.media_id)
        direct_prequels_full = self._relation_level(
            anime.relations, "PREQUEL", excluded={root_id}, limit=None
        )
        direct_sequels_full = self._relation_level(
            anime.relations, "SEQUEL", excluded={root_id}, limit=None
        )

        prequel_children = [
            child
            for item in direct_prequels_full
            for child in (item.get("relations") or [])
        ]
        sequel_children = [
            child
            for item in direct_sequels_full
            for child in (item.get("relations") or [])
        ]
        prequel_ids = {root_id, *(int(item["media_id"]) for item in direct_prequels_full)}
        sequel_ids = {root_id, *(int(item["media_id"]) for item in direct_sequels_full)}
        second_prequels_full = self._relation_level(
            prequel_children, "PREQUEL", excluded=prequel_ids, limit=None
        )
        second_sequels_full = self._relation_level(
            sequel_children, "SEQUEL", excluded=sequel_ids, limit=None
        )
        direct_prequels = direct_prequels_full[:2]
        direct_sequels = direct_sequels_full[:2]
        second_prequels = second_prequels_full[:2]
        second_sequels = second_sequels_full[:2]

        return {
            "current": current,
            "prequel_levels": [second_prequels, direct_prequels],
            "sequel_levels": [direct_sequels, second_sequels],
            "full_prequel_levels": [second_prequels_full, direct_prequels_full],
            "full_sequel_levels": [direct_sequels_full, second_sequels_full],
            "order_mode": "anilist",
        }

    def _episode_numbering_offset(self, anime: LibraryAnime | None) -> int:
        if anime is None or not anime.media_id:
            return 0
        cached = self._episode_offset_cache.get(int(anime.media_id))
        if cached is not None:
            return cached
        try:
            result = resolve_episode_numbering(
                anime,
                1,
                self.config,
                self.logger,
                db=self.manager.db,
                allow_network=False,
            )
            offset = max(0, int(result.offset))
        except Exception:
            offset = 0
        if offset:
            self._episode_offset_cache[int(anime.media_id)] = offset
        return offset

    def _display_episode_number(
        self, anime: LibraryAnime | None, episode: int | None
    ) -> int | None:
        if episode is None:
            return None
        value = int(episode)
        if anime is None:
            return value
        total = int(anime.episodes or 0)
        if 1 <= value and (not total or value <= total):
            return value
        offset = self._episode_numbering_offset(anime)
        relative = value - offset if offset else value
        if relative >= 1 and (not total or relative <= total):
            return relative
        return value

    def _local_episode_for_relative(
        self, anime: LibraryAnime, relative_episode: int | None
    ):
        if relative_episode is None:
            return self.manager.db.ready_episode(anime.media_id, None)
        exact = self.manager.db.ready_episode(anime.media_id, relative_episode)
        if exact is not None:
            return exact
        for item in self.manager.db.episodes(anime.media_id):
            if item.state not in {"ready", "local", "watched", "waiting_subtitles", "waiting_text_subtitles"}:
                continue
            if self._display_episode_number(anime, item.episode) == int(relative_episode):
                return item
        return None

    def _planning_download_button_hidden(
        self,
        anime: LibraryAnime,
        *,
        downloads: list[Any] | None = None,
    ) -> bool:
        # Hide Planning auto-download once this title is already local or managed.
        job = getattr(self, "_planning_episode_download_state", {}) or {}
        if bool(job.get("running")) and int(job.get("media_id") or 0) == int(anime.media_id):
            return True

        invalid_states = {"", "error", "missingfiles", "unknown", "stalleddl"}
        rows = downloads if downloads is not None else self.manager.db.downloads()
        for item in rows:
            if item.media_id is None or int(item.media_id) != int(anime.media_id):
                continue
            if AnimeManager._download_is_complete(item):
                return True
            state = str(item.state or "").strip().casefold()
            if state not in invalid_states:
                return True

        if str(anime.format or "").upper() == "MOVIE":
            return False
        if str(anime.media_status or "").upper() == "NOT_YET_RELEASED":
            return False

        if str(anime.media_status or "").upper() == "FINISHED":
            target = int(anime.episodes or 0)
        else:
            target = int(anime.released_episodes or 0)
        if target < 1:
            return False

        incomplete = self.manager.incomplete_download_paths()
        local_numbers: set[int] = set()
        for item in self.manager.db.episodes(anime.media_id):
            if not item.video_path.is_file():
                continue
            if self.manager._path_within(item.video_path, incomplete):
                continue
            display_episode = self._display_episode_number(anime, item.episode)
            if display_episode is not None and 1 <= int(display_episode) <= target:
                local_numbers.add(int(display_episode))
        return all(number in local_numbers for number in range(1, target + 1))

    def _anime_payload(self, anime: LibraryAnime) -> dict[str, Any]:
        released = anime.released_episodes
        outdated = bool(released and released > anime.progress)
        local = self._local_episode_for_relative(anime, anime.next_episode)
        next_queue_count = 0
        franchise_queue_available = False
        if anime.media_status != "NOT_YET_RELEASED":
            next_queue_count = len(self._ready_queue_items([anime.media_id], limit=5))
            relation_ids = sorted(self._relation_ids(anime))
            franchise_queue_available = bool(
                self._ready_queue_items([anime.media_id, *relation_ids], limit=1)
            )
        return {
            "media_id": anime.media_id,
            "title": anime.title,
            "titles": list(anime.titles),
            "synonyms": list(anime.synonyms),
            "cover": self._cover_uri(anime),
            "site_url": anime.site_url,
            "list_status": anime.status,
            "media_status": anime.media_status or "",
            "finished": anime.media_status == "FINISHED",
            "end_date": anime.end_date or "",
            "progress": anime.progress,
            "episodes": anime.episodes,
            "mean_score": anime.mean_score,
            "user_score": anime.user_score,
            "duration": anime.duration,
            "format": anime.format or "",
            "season_year": anime.season_year,
            "start_date": anime.start_date,
            "studio": anime.studio,
            "relations": self._relation_payload(anime),
            "next_episode": anime.next_episode,
            "is_final_episode": bool(anime.episodes and anime.next_episode >= anime.episodes),
            "released_episodes": released,
            "outdated": outdated,
            "missing_count": max(0, (released or anime.progress) - anime.progress),
            "next_airing_episode": anime.next_airing_episode,
            "next_airing_at": anime.next_airing_at,
            "remaining_seconds": self._remaining(anime.next_airing_at),
            "queue_next_count": next_queue_count,
            "queue_franchise_available": franchise_queue_available,
            "local": (
                {
                    "episode": self._display_episode_number(anime, local.episode),
                    "video_path": str(local.video_path),
                    "subtitle_path": str(local.subtitle_path) if local.subtitle_path else "",
                    "subtitle_source": (
                        "image"
                        if (
                            local.state == "waiting_text_subtitles"
                            and (local.subtitle_path is not None or local.embedded_subtitle_id is not None)
                        )
                        else "external"
                        if local.subtitle_path
                        else "embedded"
                        if local.state in {"ready", "watched"}
                        else "none"
                    ),
                    "state": local.state,
                }
                if local
                else None
            ),
        }

    def _continue_payloads(
        self,
        anime_by_id: dict[int, LibraryAnime],
    ) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        incomplete = self.manager.incomplete_download_paths()
        for item in self.manager.db.resumable_episodes(limit=8):
            if (
                not item.video_path.is_file()
                or self.manager._path_within(item.video_path, incomplete)
            ):
                continue
            anime = anime_by_id.get(item.media_id) if item.media_id is not None else None
            position = float(item.playback_position or 0.0)
            duration = float(item.playback_duration or 0.0)
            payloads.append(
                {
                    "media_id": item.media_id,
                    "title": anime.title if anime is not None else item.title,
                    "site_url": anime.site_url if anime is not None else "",
                    "media_status": anime.media_status if anime is not None else "",
                    "cover": self._cover_uri(anime) if anime is not None else "",
                    "episode": self._display_episode_number(anime, item.episode),
                    "is_movie": bool(anime is not None and str(anime.format or "").upper() == "MOVIE"),
                    "video_path": str(item.video_path),
                    "position": position,
                    "duration": duration,
                    "resume_start": max(0.0, position - self.config.playback.rewind_seconds),
                    "progress_percent": (position / duration * 100.0) if duration > 0 else None,
                    "updated_at": item.playback_updated_at,
                }
            )
        return payloads

    def _library_payloads(
        self,
        anime_by_id: dict[int, LibraryAnime],
    ) -> list[dict[str, Any]]:
        """Group local episodes by anime for the compact Library page."""
        groups: dict[tuple[str, object], dict[str, Any]] = {}
        incomplete = self.manager.incomplete_download_paths()
        for item in self.manager.db.episodes():
            if self.manager._path_within(item.video_path, incomplete):
                continue
            key: tuple[str, object]
            if item.media_id is not None:
                key = ("media", int(item.media_id))
            else:
                key = ("title", item.title.casefold())

            anime = anime_by_id.get(item.media_id) if item.media_id is not None else None
            group = groups.setdefault(
                key,
                {
                    "media_id": item.media_id,
                    "title": anime.title if anime is not None else item.title,
                    "cover": self._cover_uri(anime) if anime is not None else "",
                    "site_url": anime.site_url if anime is not None else "",
                    "total_episodes": anime.episodes if anime is not None else None,
                    "episode_duration_minutes": anime.duration if anime is not None else None,
                    "watched_folder": False,
                    "episodes": [],
                },
            )
            try:
                size_bytes = item.video_path.stat().st_size if item.video_path.is_file() else 0
            except OSError:
                size_bytes = 0
            if self.manager._path_within(
                item.video_path,
                tuple(path.expanduser().resolve() for path in self.config.paths.download_dirs),
            ):
                group["watched_folder"] = True
            effective_state = item.state
            if effective_state == "ready" and (
                str(item.subtitle_origin or "").casefold() == "bitmap"
                or (
                    item.subtitle_path is not None
                    and item.subtitle_path.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS
                )
            ):
                effective_state = "waiting_text_subtitles"
            group["episodes"].append(
                {
                    "episode": self._display_episode_number(anime, item.episode),
                    "stored_episode": item.episode,
                    "video_path": str(item.video_path),
                    "filename": item.video_path.name,
                    "subtitle_path": str(item.subtitle_path) if item.subtitle_path else "",
                    "subtitle_filename": item.subtitle_path.name if item.subtitle_path else "",
                    "subtitle_source": (
                        "image"
                        if (
                            effective_state == "waiting_text_subtitles"
                            and (item.subtitle_path is not None or item.embedded_subtitle_id is not None)
                        )
                        else "external"
                        if item.subtitle_path
                        else "embedded"
                        if effective_state in {"ready", "watched"}
                        else "none"
                    ),
                    "state": effective_state,
                    "size_bytes": size_bytes,
                    "playback_position": item.playback_position,
                    "playback_duration": item.playback_duration,
                }
            )

        payloads = list(groups.values())
        for group in payloads:
            group["episodes"].sort(
                key=lambda item: (
                    item["episode"] is None,
                    item["episode"] if item["episode"] is not None else 10**9,
                    str(item["filename"]).casefold(),
                )
            )
            group["episode_count"] = len(group["episodes"])
            group["ready_count"] = sum(
                1 for item in group["episodes"] if item["state"] in {"ready", "watched"}
            )
            group["watched_count"] = sum(
                1 for item in group["episodes"] if item["state"] == "watched"
            )
            group["waiting_count"] = sum(
                1
                for item in group["episodes"]
                if item["state"] in {"waiting_subtitles", "waiting_text_subtitles"}
            )
            group["size_bytes"] = sum(int(item["size_bytes"] or 0) for item in group["episodes"])
            fallback_duration = max(0.0, float(group.get("episode_duration_minutes") or 0.0) * 60.0)
            group["duration_seconds"] = sum(
                max(0.0, float(item.get("playback_duration") or 0.0)) or fallback_duration
                for item in group["episodes"]
            )

        return sorted(payloads, key=lambda item: str(item["title"]).casefold())

    def _downloaded_payloads(
        self,
        anime_by_id: dict[int, LibraryAnime],
    ) -> list[dict[str, Any]]:
        """Return one home-page card per anime that is ready to watch.

        The library table remains the complete low-level view. This collection
        is intentionally narrower: only existing, non-watched files whose
        Japanese subtitle preparation finished successfully are exposed.
        """
        groups: dict[tuple[str, object], dict[int | None, Any]] = {}
        titles: dict[tuple[str, object], str] = {}
        media_ids: dict[tuple[str, object], int | None] = {}

        incomplete = self.manager.incomplete_download_paths()
        for item in self.manager.db.episodes():
            if (
                item.state != "ready"
                or not item.video_path.is_file()
                or self.manager._path_within(item.video_path, incomplete)
            ):
                continue
            key: tuple[str, object]
            if item.media_id is not None:
                key = ("media", int(item.media_id))
            else:
                key = ("title", item.title.casefold())
            groups.setdefault(key, {}).setdefault(item.episode, item)
            titles[key] = item.title
            media_ids[key] = item.media_id

        payloads: list[dict[str, Any]] = []
        for key, episode_map in groups.items():
            stored_episodes = sorted(
                episode_map,
                key=lambda value: (value is None, value if value is not None else 10**9),
            )
            first = episode_map[stored_episodes[0]]
            media_id = media_ids[key]
            anime = anime_by_id.get(media_id) if media_id is not None else None
            title = anime.title if anime is not None else titles[key]
            episodes = sorted(
                {self._display_episode_number(anime, value) for value in stored_episodes},
                key=lambda value: (value is None, value if value is not None else 10**9),
            )
            display_first_episode = self._display_episode_number(anime, first.episode)
            payloads.append(
                {
                    "media_id": media_id,
                    "title": title,
                    "cover": self._cover_uri(anime) if anime is not None else "",
                    "site_url": anime.site_url if anime is not None else "",
                    "ready_episodes": episodes,
                    "ready_count": len(episodes),
                    "total_episodes": anime.episodes if anime is not None else None,
                    "episodes": anime.episodes if anime is not None else None,
                    "format": anime.format if anime is not None else "",
                    "season_year": anime.season_year if anime is not None else None,
                    "start_date": anime.start_date if anime is not None else None,
                    "relations": self._relation_payload(anime) if anime is not None else {},
                    "user_score": anime.user_score if anime is not None else None,
                    "is_final_episode": bool(
                        anime is not None
                        and anime.episodes is not None
                        and display_first_episode is not None
                        and display_first_episode >= anime.episodes
                    ),
                    "all_episodes_ready": bool(
                        anime is not None
                        and anime.episodes is not None
                        and anime.episodes > 0
                        and episodes == list(range(1, anime.episodes + 1))
                    ),
                    "local": {
                        "episode": display_first_episode,
                        "video_path": str(first.video_path),
                        "subtitle_path": str(first.subtitle_path) if first.subtitle_path else "",
                        "subtitle_source": "external" if first.subtitle_path else "embedded",
                        "state": first.state,
                    },
                }
            )

        return sorted(
            payloads,
            key=lambda item: (
                item["ready_episodes"][0] is None,
                str(item["title"]).casefold(),
            ),
        )

    def _pending_local_payloads(
        self,
        anime_by_id: dict[int, LibraryAnime],
    ) -> list[dict[str, Any]]:
        """Expose complete local files that are still waiting for preparation."""
        incomplete = self.manager.incomplete_download_paths()
        groups: dict[tuple[str, object], Any] = {}
        for item in self.manager.db.episodes():
            if (
                item.state in {"ready", "watched"}
                or not item.video_path.is_file()
                or self.manager._path_within(item.video_path, incomplete)
            ):
                continue
            key = (
                ("media", int(item.media_id))
                if item.media_id is not None
                else ("title", item.title.casefold())
            )
            groups.setdefault(key, item)

        result: list[dict[str, Any]] = []
        for item in groups.values():
            anime = anime_by_id.get(item.media_id) if item.media_id is not None else None
            if anime is not None and anime.status == "DROPPED":
                continue
            episode = self._display_episode_number(anime, item.episode)
            result.append(
                {
                    "media_id": item.media_id,
                    "title": anime.title if anime is not None else item.title,
                    "cover": self._cover_uri(anime) if anime is not None else "",
                    "site_url": anime.site_url if anime is not None else "",
                    "next_episode": episode,
                    "episodes": anime.episodes if anime is not None else None,
                    "is_final_episode": bool(
                        anime is not None
                        and anime.episodes
                        and episode is not None
                        and episode >= anime.episodes
                    ),
                    "local": {
                        "episode": episode,
                        "video_path": str(item.video_path),
                        "subtitle_path": str(item.subtitle_path) if item.subtitle_path else "",
                        "subtitle_source": (
                            "image"
                            if item.state == "waiting_text_subtitles"
                            and (item.subtitle_path is not None or item.embedded_subtitle_id is not None)
                            else "external" if item.subtitle_path else "none"
                        ),
                        "state": item.state,
                    },
                }
            )
        return sorted(result, key=lambda value: str(value["title"]).casefold())

    @staticmethod
    def _relation_ids(anime: LibraryAnime) -> set[int]:
        ids: set[int] = set()

        def visit(items: list[dict[str, Any]]) -> None:
            for item in items:
                try:
                    ids.add(int(item.get("media_id")))
                except (TypeError, ValueError):
                    pass
                children = item.get("relations")
                if isinstance(children, list):
                    visit(children)

        visit(anime.relations)
        return ids

    def _order_single_episode_component(
        self,
        media_ids: set[int],
        anime_by_id: dict[int, LibraryAnime],
    ) -> list[int]:
        release_sorted = sorted(
            media_ids,
            key=lambda media_id: self._release_key(anime_by_id[media_id])
            or (9999, 12, 31, media_id),
        )
        if self.config.anilist.relations_by_release_date:
            return release_sorted

        outgoing: dict[int, set[int]] = {media_id: set() for media_id in media_ids}
        indegree: dict[int, int] = {media_id: 0 for media_id in media_ids}
        for source_id in media_ids:
            anime = anime_by_id[source_id]
            for relation in anime.relations:
                try:
                    target_id = int(relation.get("media_id"))
                except (TypeError, ValueError):
                    continue
                if target_id not in media_ids:
                    continue
                relation_type = str(relation.get("relation_type") or "").upper()
                edge = None
                if relation_type == "SEQUEL":
                    edge = (source_id, target_id)
                elif relation_type == "PREQUEL":
                    edge = (target_id, source_id)
                if edge is None or edge[1] in outgoing[edge[0]]:
                    continue
                outgoing[edge[0]].add(edge[1])
                indegree[edge[1]] += 1

        rank = {media_id: index for index, media_id in enumerate(release_sorted)}
        queue = sorted((media_id for media_id, value in indegree.items() if value == 0), key=rank.get)
        ordered: list[int] = []
        while queue:
            media_id = queue.pop(0)
            ordered.append(media_id)
            for target in sorted(outgoing[media_id], key=rank.get):
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
                    queue.sort(key=rank.get)
        return ordered if len(ordered) == len(media_ids) else release_sorted

    @staticmethod
    def _series_group_title(
        media_ids: list[int],
        anime_by_id: dict[int, LibraryAnime],
    ) -> str:
        titles = [
            str(anime_by_id[media_id].title or "").strip()
            for media_id in media_ids
            if media_id in anime_by_id and str(anime_by_id[media_id].title or "").strip()
        ]
        if not titles:
            return "Series"

        tokenized = [re.findall(r"[^\W_]+", title, flags=re.UNICODE) for title in titles]
        prefix_len = 0
        for index in range(min(len(tokens) for tokens in tokenized)):
            token = tokenized[0][index]
            if all(tokens[index].casefold() == token.casefold() for tokens in tokenized[1:]):
                prefix_len += 1
            else:
                break
        candidate = " ".join(tokenized[0][:prefix_len]).strip()
        candidate = re.sub(
            r"(?i)\b(?:season|movie|film|part|episode|ova|ona|special)\b.*$",
            "",
            candidate,
        ).strip(" :-–—")
        generic = {"part", "season", "movie", "film", "special", "ova", "ona"}
        if candidate.casefold() in generic:
            candidate = ""

        if not candidate:
            compact = [
                re.sub(
                    r"[^0-9a-zа-яёぁ-んァ-ヶ一-龯]+",
                    "",
                    unicodedata.normalize("NFKC", title).casefold(),
                    flags=re.UNICODE,
                )
                for title in titles
            ]
            shortest = min(compact, key=len)
            common = ""
            for length in range(min(len(shortest), 40), 4, -1):
                match = next(
                    (
                        shortest[start : start + length]
                        for start in range(0, len(shortest) - length + 1)
                        if all(shortest[start : start + length] in value for value in compact)
                    ),
                    "",
                )
                if match:
                    common = match
                    break
            if common:
                candidate = common[:1].upper() + common[1:]

        if not candidate:
            candidate = re.split(r"\s*[:：|]\s*|\s+[–—-]\s+", titles[0], maxsplit=1)[0].strip()
            candidate = re.sub(
                r"(?i)\b(?:season|movie|film|part|episode|ova|ona|special)\b.*$",
                "",
                candidate,
            ).strip(" :-–—") or titles[0]

        if len(candidate.split()) == 1 and not candidate.casefold().endswith("series"):
            candidate = f"{candidate} Series"
        return candidate

    def _group_completed_ready(
        self,
        items: list[dict[str, Any]],
        anime_by_id: dict[int, LibraryAnime],
    ) -> list[dict[str, Any]]:
        candidates = {
            int(item["media_id"]): item
            for item in items
            if item.get("media_id") is not None
            and int(item.get("total_episodes") or item.get("episodes") or 0) == 1
            and int(item["media_id"]) in anime_by_id
        }
        if not candidates:
            return items

        # Build the franchise component from every known one-episode work, not
        # only from the entries that are currently ready.  This keeps the compact
        # franchise card alive when the first entry becomes watched and only one
        # ready entry remains: the card immediately advances to the next work.
        all_single_ids = {
            media_id
            for media_id, anime in anime_by_id.items()
            if int(anime.episodes or 0) == 1
        }
        adjacency: dict[int, set[int]] = {media_id: set() for media_id in all_single_ids}
        for media_id in all_single_ids:
            for related_id in self._relation_ids(anime_by_id[media_id]):
                if related_id in all_single_ids:
                    adjacency[media_id].add(related_id)
                    adjacency[related_id].add(media_id)

        grouped_ids: set[int] = set()
        groups_by_first: dict[int, dict[str, Any]] = {}
        visited: set[int] = set()
        for media_id in candidates:
            if media_id in visited:
                continue
            stack = [media_id]
            full_component: set[int] = set()
            while stack:
                current = stack.pop()
                if current in full_component:
                    continue
                full_component.add(current)
                stack.extend(adjacency.get(current, set()) - full_component)
            visited.update(full_component)
            visible_component = full_component & candidates.keys()
            # A watch-order card is useful only when at least two entries are
            # actually local and ready. Relations that exist only on AniList must
            # not turn a single movie/special into a one-item sequence card.
            if len(visible_component) < 2:
                continue

            ordered_full_ids = self._order_single_episode_component(
                full_component, anime_by_id
            )
            ordered_visible_ids = [
                item_id for item_id in ordered_full_ids if item_id in visible_component
            ]
            if not ordered_visible_ids:
                continue
            grouped_ids.update(visible_component)
            first_visible = ordered_visible_ids[0]
            groups_by_first[first_visible] = {
                "kind": "watch_sequence",
                "items": [candidates[item_id] for item_id in ordered_visible_ids],
                "media_ids": ordered_visible_ids,
                "series_media_ids": ordered_full_ids,
                "title": self._series_group_title(ordered_full_ids, anime_by_id),
                "order_mode": (
                    "release" if self.config.anilist.relations_by_release_date else "anilist"
                ),
            }

        result: list[dict[str, Any]] = []
        emitted_groups: set[int] = set()
        for item in items:
            media_id = int(item["media_id"]) if item.get("media_id") is not None else None
            if media_id not in grouped_ids:
                result.append(item)
                continue
            for first_id, group in groups_by_first.items():
                if media_id in group["media_ids"] and first_id not in emitted_groups:
                    result.append(group)
                    emitted_groups.add(first_id)
                    break
        return result

    @staticmethod
    def _home_item_identity(item: dict[str, Any]) -> tuple[str, object]:
        media_id = item.get("media_id")
        if media_id is not None:
            return ("media", int(media_id))
        return ("title", str(item.get("title") or "").strip().casefold())

    @staticmethod
    def _download_shadowed_by_local(
        card: dict[str, Any], download: dict[str, Any]
    ) -> bool:
        # A completed local episode wins over stale transport metadata.
        if bool(download.get("is_batch")):
            return False
        local = card.get("local")
        if not isinstance(local, dict):
            return False
        video_path = str(local.get("video_path") or "").strip()
        if not video_path or not Path(video_path).is_file():
            return False
        local_episode = local.get("episode")
        download_episode = download.get("episode")
        if local_episode is None or download_episode is None:
            return local_episode is None and download_episode is None
        try:
            return int(local_episode) == int(download_episode)
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _pending_local_action(
        local: dict[str, Any],
        action_job: Any | None,
        *,
        ocr_enabled: bool,
    ) -> tuple[str, str] | None:
        del local, ocr_enabled
        if action_job is None:
            return None
        return (
            str(action_job["action_code"] or ""),
            str(action_job["last_error"] or ""),
        )

    @classmethod
    def _deduplicate_home_sections(
        cls,
        sections: dict[str, list[dict[str, Any]]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Keep every anime only in its highest-priority home section."""
        # A saved mid-episode position must win over every ready grouping.
        # Otherwise the ready card hides Continue Watching and its ordinary
        # play action launches from 00:00 instead of using the resume point.
        priority = (
            "continue_watching",
            "needs_action",
            "new_ready",
            "completed_ready",
            "waiting",
            "download_available",
            "caught_up",
            "dropped",
        )
        seen: set[tuple[str, object]] = set()
        for key in priority:
            unique: list[dict[str, Any]] = []
            for item in sections.get(key, []):
                identity = cls._home_item_identity(item)
                if identity in seen:
                    continue
                seen.add(identity)
                unique.append(item)
            sections[key] = unique
        return sections

    @staticmethod
    def _is_future_unreleased(
        anime: LibraryAnime | None,
        *,
        today: date | None = None,
    ) -> bool:
        if anime is None:
            return False
        if str(anime.media_status or "").strip().upper() == "NOT_YET_RELEASED":
            return True
        if anime.start_date:
            try:
                return date.fromisoformat(str(anime.start_date)) > (today or date.today())
            except ValueError:
                pass
        return False

    @staticmethod
    def _ended_within_days(
        anime: LibraryAnime,
        days: int = 7,
        *,
        today: date | None = None,
    ) -> bool:
        if not anime.end_date:
            return False
        try:
            ended = date.fromisoformat(anime.end_date)
        except ValueError:
            return False
        age = (today or date.today()) - ended
        return 0 <= age.days <= days

    def _home_sections(
        self,
        current_anime: list[LibraryAnime],
        anime_by_id: dict[int, LibraryAnime],
    ) -> dict[str, list[dict[str, Any]]]:
        """Build the actionable groups shown on the main page.

        A recently finished title is treated like an airing title only when the
        viewer is exactly one episode behind and the finale aired in the last
        seven calendar days. Older finished titles appear only when a prepared
        local episode is available.
        """
        downloaded = self._downloaded_payloads(anime_by_id)
        pending_local = self._pending_local_payloads(anime_by_id)
        needs_action_by_path = {
            str(row["video_path"]): row
            for row in self.manager.db.subtitle_jobs()
            if (
                str(row["state"] or "") == "needs_action"
                or str(row["action_code"] or "") == "enable_subtitle_ocr"
            )
        }
        ready_by_media = {
            int(item["media_id"]): item
            for item in downloaded
            if item.get("media_id") is not None
        }
        handled_media: set[int] = set()
        downloads_by_media = {}
        for item in self.manager.db.downloads():
            if item.media_id is not None:
                # downloads() is newest-first; retain the newest matching job.
                downloads_by_media.setdefault(int(item.media_id), item)
        sections: dict[str, list[dict[str, Any]]] = {
            "continue_watching": self._continue_payloads(anime_by_id),
            "needs_action": [],
            "new_ready": [],
            "completed_ready": [],
            "waiting": [],
            "download_available": [],
            "caught_up": [],
            "dropped": [],
        }

        for anime in current_anime:
            handled_media.add(anime.media_id)
            if self._is_future_unreleased(anime):
                # A local file cannot make an AniList title available before its
                # release date. Keep the file visible in Library, but never expose
                # it as a Ready/Waiting actionable home card. This also contains
                # damage from a stale false auto-import association.
                continue
            base = self._anime_payload(anime)
            ready = ready_by_media.get(anime.media_id)
            ready_episodes = set(ready.get("ready_episodes", [])) if ready else set()
            next_is_ready = anime.next_episode in ready_episodes
            if anime.format == "MOVIE" and None in ready_episodes:
                next_is_ready = True

            card = dict(base)
            if ready is not None:
                card.update(ready)
                card["local"] = ready["local"]

            currently_airing = anime.media_status == "RELEASING"
            recent_finale_pending = bool(
                anime.media_status == "FINISHED"
                and anime.episodes
                and anime.progress == anime.episodes - 1
                and self._ended_within_days(anime)
            )

            if currently_airing or recent_finale_pending:
                if next_is_ready:
                    sections["new_ready"].append(card)
                elif base["outdated"] or recent_finale_pending:
                    sections["waiting"].append(base)
                else:
                    sections["caught_up"].append(base)
            elif next_is_ready:
                sections["completed_ready"].append(card)
            elif (
                anime.media_status not in {"RELEASING", "NOT_YET_RELEASED"}
                and (anime.episodes is None or anime.progress < anime.episodes)
            ):
                # Finished/paused non-airing titles in Watching otherwise had no
                # actionable home card. Offer a full batch download without
                # changing their AniList list status. Existing qBittorrent work
                # is attached so the UI cannot offer another duplicate download.
                download = downloads_by_media.get(anime.media_id)
                if download is not None:
                    base["download"] = {
                        "torrent_hash": download.torrent_hash,
                        "name": download.name,
                        "state": download.state,
                        "progress": download.progress,
                        "is_batch": download.is_batch,
                    }
                sections["download_available"].append(base)

        # Keep downloaded movies, PLANNING titles and locally matched files that
        # are not in the CURRENT AniList list accessible on the main page.
        for item in downloaded:
            media_id = item.get("media_id")
            if media_id is not None and int(media_id) in handled_media:
                continue
            anime = anime_by_id.get(int(media_id)) if media_id is not None else None
            if self._is_future_unreleased(anime):
                continue
            section = (
                "new_ready"
                if anime is not None and anime.media_status == "RELEASING"
                else "completed_ready"
            )
            sections[section].append(item)

        # A fully downloaded Planning/removed title used to disappear from the
        # home page until subtitle preparation finished. Keep it visible in the
        # waiting section so specials such as "I am a hero too" are actionable.
        for item in pending_local:
            media_id = item.get("media_id")
            anime = anime_by_id.get(int(media_id)) if media_id is not None else None
            if self._is_future_unreleased(anime):
                continue
            local = item.get("local") if isinstance(item.get("local"), dict) else {}
            action_job = needs_action_by_path.get(str(local.get("video_path") or ""))
            action = self._pending_local_action(
                local,
                action_job,
                ocr_enabled=bool(self.config.matching.ocr_image_subtitles),
            )
            if action is not None:
                item["action_code"], item["action_error"] = action
                sections["needs_action"].append(item)
            else:
                sections["waiting"].append(item)

        for anime in self.manager.db.anime_list(("DROPPED",)):
            local_items = [item for item in self.manager.db.episodes(anime.media_id) if item.video_path.is_file()]
            if not local_items:
                continue
            card = self._anime_payload(anime)
            delete_at = max((item.delete_after or 0.0) for item in local_items) or None
            card["delete_at"] = delete_at
            card["delete_remaining_seconds"] = self._remaining(int(delete_at)) if delete_at else None
            card["local_count"] = len(local_items)
            sections["dropped"].append(card)

        sections["new_ready"].sort(
            key=lambda item: (
                item["local"]["episode"] is None,
                item["local"]["episode"] or 0,
                str(item["title"]).casefold(),
            )
        )
        sections["completed_ready"].sort(key=lambda item: str(item["title"]).casefold())
        sections["needs_action"].sort(key=lambda item: str(item["title"]).casefold())
        sections["waiting"].sort(
            key=lambda item: (
                item.get("next_airing_at") or 10**15,
                str(item["title"]).casefold(),
            )
        )
        sections["download_available"].sort(key=lambda item: str(item["title"]).casefold())
        sections["caught_up"].sort(
            key=lambda item: (
                item.get("next_airing_at") or 10**15,
                str(item["title"]).casefold(),
            )
        )
        sections["dropped"].sort(key=lambda item: str(item["title"]).casefold())
        sections = self._deduplicate_home_sections(sections)
        sections["completed_ready"] = self._group_completed_ready(
            sections["completed_ready"], anime_by_id
        )
        for rows in sections.values():
            for card in rows:
                try:
                    media_id = int(card.get("media_id") or 0)
                except (TypeError, ValueError):
                    media_id = 0
                local = card.get("local") if isinstance(card.get("local"), dict) else None
                local_for_state = local
                if local_for_state is None and card.get("video_path"):
                    local_for_state = {
                        "state": "ready",
                        "video_path": card.get("video_path"),
                    }
                download = downloads_by_media.get(media_id) if media_id else None
                action_job = None
                if local is not None and local.get("video_path"):
                    action_job = needs_action_by_path.get(str(local.get("video_path")))
                card["presentation"] = derive_episode_presentation(
                    local=local_for_state,
                    download=download,
                    action_job=action_job,
                )
        return sections

    def _settings_payload(self) -> dict[str, Any]:
        apply_jimaku_trial(self.config)
        cfg = self.config
        ln_settings = self.light_novels.settings() if hasattr(self, "light_novels") else None
        study_plugins = mpv_study_status(
            jiten_api_key=str(getattr(ln_settings, "jiten_api_key", "") or ""),
            jpdb_api_token=str(getattr(ln_settings, "jpdb_api_token", "") or ""),
            selected_plugin=cfg.tools.mpv_study_plugin,
        )
        return {
            "version": __version__,
            "language": cfg.ui.language,
            "onboarding_completed": cfg.ui.onboarding_completed,
            "escape_exits_fullscreen": cfg.ui.escape_exits_fullscreen,
            "notifications_enabled": cfg.ui.notifications_enabled,
            "permissions_requested": cfg.ui.permissions_requested,
            "jiten_developer_tools_confirmed": cfg.ui.jiten_developer_tools_confirmed,
            "library_root": str(cfg.library.root_dir),
            "watched_folders": "\n".join(str(path) for path in cfg.paths.download_dirs),
            "subtitle_folders": "\n".join(str(path) for path in cfg.paths.subtitle_dirs),
            "subtitle_folder": str(cfg.paths.subtitle_dirs[0]) if cfg.paths.subtitle_dirs else "",
            "default_subtitle_folder": str(Path.home() / "Downloads"),
            "disk_limit_enabled": cfg.library.disk_limit_enabled,
            "disk_limit_gb": cfg.library.disk_limit_gb,
            "playback_enabled": cfg.playback.enabled,
            "playback_rewind": cfg.playback.rewind_seconds,
            "nyaa_enabled": cfg.nyaa.enabled,
            "nyaa_auto": cfg.nyaa.auto_download_current,
            "subsplease_rss_enabled": cfg.nyaa.subsplease_rss_enabled,
            "subsplease_rss_preferred": cfg.nyaa.subsplease_rss_preferred,
            "nyaa_url": cfg.nyaa.base_url,
            "proxy_mode": cfg.nyaa.proxy_mode,
            "proxy_url": cfg.nyaa.proxy_url,
            "search_hook": cfg.nyaa.pre_search_command,
            "min_score": cfg.nyaa.min_release_score,
            "preferred_resolution": cfg.nyaa.preferred_resolution,
            "preferred_video_codecs": ", ".join(cfg.nyaa.preferred_video_codecs),
            "preferred_sources": ", ".join(cfg.nyaa.preferred_sources),
            "require_japanese_audio": cfg.nyaa.require_japanese_audio,
            "avoid_upscaled": cfg.nyaa.avoid_upscaled,
            "only_trusted_groups": cfg.nyaa.only_trusted_groups,
            "trusted_groups": ", ".join(cfg.nyaa.trusted_groups),
            "preferred_groups": ", ".join(cfg.nyaa.preferred_groups),
            "blocked_groups": ", ".join(cfg.nyaa.blocked_groups),
            "auto_upgrade_downloaded": cfg.nyaa.auto_upgrade_downloaded,
            "upgrade_min_score_gain": cfg.nyaa.upgrade_min_score_gain,
            "upgrade_check_hours": cfg.nyaa.upgrade_check_hours,
            "max_upgrade_checks_per_run": cfg.nyaa.max_upgrade_checks_per_run,
            "qbt_enabled": cfg.qbittorrent.enabled,
            "qbt_url": cfg.qbittorrent.base_url,
            "qbt_user": cfg.qbittorrent.username,
            "qbt_password": cfg.qbittorrent.password,
            "qbt_api_key": cfg.qbittorrent.api_key,
            "aria2_enabled": cfg.aria2.enabled,
            "aria2_binary": cfg.aria2.binary,
            "aria2_rpc_port": cfg.aria2.rpc_port,
            "aria2_seed_mode": cfg.aria2.seed_mode,
            "aria2_seed_ratio": cfg.aria2.seed_ratio,
            "aria2_seed_time_minutes": cfg.aria2.seed_time_minutes,
            "aria2_upload_limit_kib": cfg.aria2.upload_limit_kib,
            "aria2_vpn_interface": cfg.aria2.vpn_interface,
            "aria2_vpn_kill_switch": cfg.aria2.vpn_kill_switch,
            "torrent_backend": (self.manager.torrent_backend_name() if callable(getattr(self.manager, "torrent_backend_name", None)) else ("qBittorrent" if cfg.qbittorrent.enabled else "aria2")) if self._downloads_enabled() else "disabled",
            "download_hook": cfg.qbittorrent.pre_download_command,
            "agent_enabled": cfg.agent.enabled,
            "agent_poll": cfg.agent.poll_minutes,
            "anilist_refresh_poll": cfg.agent.anilist_refresh_minutes,
            "subtitle_poll": cfg.agent.subtitle_poll_minutes,
            "delete_hours": cfg.agent.delete_after_watched_hours,
            "anilist_enabled": cfg.anilist.enabled,
            "anilist_client_id": cfg.anilist.client_id,
            "anilist_token": cfg.anilist.access_token,
            "anilist_auto_progress": cfg.anilist.auto_update_progress,
            "anilist_add_if_missing": cfg.anilist.add_if_missing,
            "anilist_threshold": round(cfg.anilist.watched_threshold * 100, 2),
            "anilist_max_remaining_minutes": cfg.anilist.watched_max_remaining_minutes,
            "relations_by_release_date": cfg.anilist.relations_by_release_date,
            "jimaku_api_key": cfg.jimaku.personal_api_key,
            "jimaku_trial_active": cfg.jimaku.trial_active,
            "jimaku_trial_expires_at": cfg.jimaku.trial_expires_at,
            "jimaku_trial_remaining_seconds": max(
                0, int(cfg.jimaku.trial_expires_at - time.time())
            ) if cfg.jimaku.trial_active else 0,
            "ocr_image_subtitles": cfg.matching.ocr_image_subtitles,
            "auto_upgrade_subtitles": cfg.matching.auto_upgrade_subtitles,
            "subtitle_upgrade_min_score_gain": cfg.matching.subtitle_upgrade_min_score_gain,
            "subtitle_upgrade_check_hours": cfg.matching.subtitle_upgrade_check_hours,
            "max_subtitle_upgrade_checks_per_run": cfg.matching.max_subtitle_upgrade_checks_per_run,
            "llm_enabled": cfg.llm.enabled,
            "llm_url": cfg.llm.base_url,
            "llm_api_key": cfg.llm.api_key,
            "llm_model": cfg.llm.model,
            "subtitle_semantic_checks": cfg.llm.validate_embedded_reference,
            "use_container_chapters": cfg.sync.use_container_chapters,
            "japanese_stt_fallback": cfg.sync.japanese_stt_fallback,
            "japanese_stt_model": cfg.sync.japanese_stt_model,
            "shortcut_mpv_mark_watched": cfg.shortcuts.mpv_mark_watched,
            "shortcut_mpv_open_anilist": cfg.shortcuts.mpv_open_anilist,
            "shortcut_mpv_correct_match": cfg.shortcuts.mpv_correct_match,
            "shortcut_mpv_translate_subtitle": cfg.shortcuts.mpv_translate_subtitle,
            "mpv_study_plugin": cfg.tools.mpv_study_plugin,
            "mpv_study_plugins": study_plugins,
            "energy_monitoring_enabled": cfg.diagnostics.energy_monitoring_enabled,
            "energy_sample_seconds": cfg.diagnostics.energy_sample_seconds,
            "energy_log_path": str(ENERGY_LOG_PATH),
            "light_novels": self.light_novels.settings_payload() if hasattr(self, "light_novels") else {},
        }

    def _storage_payload(self, *, refresh: bool) -> dict[str, int | float | bool]:
        if refresh or self._last_storage_status is None:
            self._last_storage_status = self.manager.storage_status()
        return dict(self._last_storage_status)

    @staticmethod
    def _download_number(raw: dict[str, Any], *keys: str) -> int:
        for key in keys:
            try:
                value = int(float(raw.get(key) or 0))
            except (TypeError, ValueError):
                continue
            if value:
                return max(0, value)
        return 0

    def _download_payload(
        self,
        item: Any,
        anime_by_id: dict[int, Any],
    ) -> dict[str, Any]:
        raw = dict(getattr(item, "raw", {}) or {})
        anime = (
            anime_by_id.get(int(item.media_id))
            if item.media_id is not None
            else None
        )
        total = self._download_number(raw, "total_size", "size", "totalLength")
        downloaded = self._download_number(
            raw, "downloaded", "completed_length", "completedLength"
        )
        if total and not downloaded:
            downloaded = min(total, round(total * float(item.progress or 0)))
        speed = self._download_number(raw, "dlspeed", "download_speed", "downloadSpeed")
        upload_speed = self._download_number(raw, "upspeed", "upload_speed", "uploadSpeed")
        eta = self._download_number(raw, "eta")
        if eta >= 8_640_000:
            eta = 0
        if not eta and speed > 0 and total > downloaded:
            eta = max(1, round((total - downloaded) / speed))
        seeders = self._download_number(raw, "num_seeds", "num_seeders", "numSeeders")
        listed_seeders = self._download_number(raw, "listed_seeders")
        listed_leechers = self._download_number(raw, "listed_leechers")
        leechers = self._download_number(raw, "num_leechs", "num_leechers")
        connections = self._download_number(
            raw, "num_connections", "connections", "num_incomplete"
        )
        peers = max(leechers, max(0, connections - seeders))
        backends = [
            str(value) for value in raw.get("_backends", []) if str(value).strip()
        ]
        primary_backend = str(raw.get("backend") or self.manager.torrent_backend_name())
        return {
            "hash": item.torrent_hash,
            "torrent_hash": item.torrent_hash,
            "name": item.name,
            "anime_title": str(anime.title) if anime is not None else "",
            "state": item.state,
            "progress": item.progress,
            "media_id": item.media_id,
            "episode": item.episode,
            "is_batch": item.is_batch,
            "backend": " + ".join(backends) if len(backends) > 1 else primary_backend,
            "backend_id": primary_backend,
            "backends": backends or [primary_backend],
            "total_bytes": total,
            "downloaded_bytes": downloaded,
            "download_speed": speed,
            "upload_speed": upload_speed,
            "seeders": seeders,
            "peers": peers,
            "listed_seeders": listed_seeders,
            "listed_peers": listed_leechers,
            "eta_seconds": eta,
            "ratio": float(raw.get("ratio") or 0),
            "save_path": item.save_path,
            "added_on": item.added_on,
            "completed_on": item.completed_on,
            "error": str(raw.get("error_message") or raw.get("error") or ""),
        }

    def _get_state(self, *, refresh_storage: bool) -> dict[str, Any]:
        current_anime = self.manager.db.anime_list(("CURRENT",))
        planned_anime = self.manager.db.anime_list(("PLANNING",))
        anime_by_id = {anime.media_id: anime for anime in self.manager.db.anime_list()}
        download_rows = self.manager.db.downloads()
        downloads = [
            self._download_payload(item, anime_by_id) for item in download_rows
        ]
        download_by_media: dict[int, dict[str, Any]] = {}
        for download in downloads:
            media_id = download.get("media_id")
            if media_id is None:
                continue
            key = int(media_id)
            previous = download_by_media.get(key)
            # Prefer live work over old completed rows, then the newest row.
            rank = (
                0 if str(download.get("state") or "").casefold() in {"active", "waiting", "paused"} else 1,
                -float(download.get("added_on") or 0),
            )
            previous_rank = (
                0 if previous and str(previous.get("state") or "").casefold() in {"active", "waiting", "paused"} else 1,
                -float(previous.get("added_on") or 0) if previous else 0,
            )
            if previous is None or rank < previous_rank:
                download_by_media[key] = download

        current = [self._anime_payload(a) for a in current_anime]
        for payload in current:
            download = download_by_media.get(int(payload["media_id"]))
            if (
                download is not None
                and not self._download_shadowed_by_local(payload, download)
            ):
                payload["download"] = download
        planned = []
        for anime in planned_anime:
            payload = self._anime_payload(anime)
            download = download_by_media.get(int(anime.media_id))
            if (
                download is not None
                and not self._download_shadowed_by_local(payload, download)
            ):
                payload["download"] = download
            payload["planning_download_hidden"] = self._planning_download_button_hidden(
                anime,
                downloads=download_rows,
            )
            planned.append(payload)
        episodes = [
            {
                "media_id": item.media_id,
                "title": item.title,
                "episode": item.episode,
                "video_path": str(item.video_path),
                "subtitle": bool(item.subtitle_path) or item.state in {"ready", "watched"},
                "subtitle_source": (
                    "external"
                    if item.subtitle_path
                    else "embedded"
                    if item.state in {"ready", "watched"}
                    else "none"
                ),
                "state": item.state,
                "playback_position": item.playback_position,
                "playback_duration": item.playback_duration,
            }
            for item in self.manager.db.episodes()
        ]
        home = self._home_sections(current_anime, anime_by_id)

        def _attach_download(card: dict[str, Any]) -> None:
            if card.get("kind") == "watch_sequence":
                for child in card.get("items") or []:
                    if isinstance(child, dict):
                        _attach_download(child)
                return
            media_id = card.get("media_id")
            if media_id is not None and int(media_id) in download_by_media:
                download = download_by_media[int(media_id)]
                if not self._download_shadowed_by_local(card, download):
                    card["download"] = download

        represented: set[int] = set()
        for cards in home.values():
            if not isinstance(cards, list):
                continue
            for card in cards:
                if not isinstance(card, dict):
                    continue
                _attach_download(card)
                children = card.get("items") if card.get("kind") == "watch_sequence" else [card]
                for child in children or []:
                    if isinstance(child, dict) and child.get("media_id") is not None:
                        represented.add(int(child["media_id"]))

        # A Planning batch has no local episode yet, so historically it vanished
        # from Home until the entire batch finished. Surface it immediately in
        # Waiting for preparation and keep the live torrent status attached.
        for media_id, download in download_by_media.items():
            anime = anime_by_id.get(media_id)
            if anime is None or media_id in represented:
                continue
            if str(anime.status or "").upper() not in {"CURRENT", "PLANNING"}:
                continue
            card = self._anime_payload(anime)
            card["download"] = download
            home.setdefault("waiting", []).append(card)
            represented.add(media_id)
        episode_title_by_path = {
            str(item.video_path): str(item.title or "") for item in self.manager.db.episodes()
        }
        jobs = []
        for row in self.manager.db.subtitle_jobs():
            media_id = int(row["media_id"]) if row["media_id"] is not None else None
            anime = anime_by_id.get(media_id) if media_id is not None else None
            video_path = str(row["video_path"])
            try:
                progress = json.loads(str(row["progress_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                progress = {}
            jobs.append({
                "video": Path(video_path).name,
                "video_path": video_path,
                "media_id": media_id,
                "anime_title": (
                    str(anime.title)
                    if anime is not None
                    else episode_title_by_path.get(video_path, "")
                ),
                "episode": int(row["episode"]) if row["episode"] is not None else None,
                "state": str(row["state"] or "pending"),
                "stage": str(row["stage"] or "queued"),
                "action_code": str(row["action_code"] or ""),
                "heartbeat_at": float(row["heartbeat_at"] or 0),
                "progress": progress if isinstance(progress, dict) else {},
                "attempts": int(row["attempts"] or 0),
                "priority": int(row["priority"] or 0),
                "next_check": float(row["next_check"] or 0),
                "error": str(row["last_error"] or ""),
            })
        return {
            "branding": {"name": APP_NAME, "slug": APP_SLUG, "bundle_id": APP_BUNDLE_ID},
            "current": current,
            "planned": planned,
            "downloaded": self._downloaded_payloads(anime_by_id),
            "home": home,
            "episodes": episodes,
            "library": self._library_payloads(anime_by_id),
            "downloads": downloads,
            "subtitle_jobs": jobs,
            "release_upgrades": self.manager.db.upgrade_jobs(limit=50),
            "subtitle_history": self.manager.db.subtitle_history(limit=80),
            "playlists": self._playlist_payloads(),
            "integrity_last_run": self.manager.db.get_state("integrity_last_run", ""),
            "settings": self._settings_payload(),
            "synced_at": self.manager.db.get_state("anilist_synced_at", ""),
            "ready_state_version": self.manager.db.get_state("ready_state_version", ""),
            "ui_state_version": self.manager.db.get_state("ui_state_version", ""),
            "storage": self._storage_payload(refresh=refresh_storage),
        }

    def ready_state_version(self) -> str:
        """Cheap cross-process marker for newly prepared Ready episodes."""
        return self.manager.db.get_state("ready_state_version", "")

    def ui_state_versions(self) -> dict[str, str]:
        """Return tiny cross-process invalidation markers without rebuilding UI state."""
        values = self.manager.db.get_states(("ready_state_version", "ui_state_version"))
        return {
            "ready": values.get("ready_state_version", ""),
            "ui": values.get("ui_state_version", ""),
        }

    def get_state(self) -> dict[str, Any]:
        return self._get_state(refresh_storage=True)

    def get_state_fast(self) -> dict[str, Any]:
        """Return UI state without recursively scanning the video library.

        Startup maintenance polls this every second so newly prepared subtitles
        can move to the Ready section immediately. Disk usage is reused from the
        most recent full state and is refreshed when maintenance finishes.
        """
        return self._get_state(refresh_storage=False)

    def torrent_downloads(
        self,
        media_id: int | None = None,
        refresh: bool = True,
    ) -> dict[str, Any]:
        warning = ""
        if refresh and self._downloads_enabled():
            try:
                self.manager.sync_downloads()
            except Exception as exc:
                warning = str(exc)
        anime_by_id = {
            anime.media_id: anime for anime in self.manager.db.anime_list()
        }
        rows = self.manager.db.downloads()
        if media_id is not None:
            rows = [
                item
                for item in rows
                if item.media_id is not None and int(item.media_id) == int(media_id)
            ]
        network_guard: dict[str, Any] = {}
        if self.config.aria2.enabled:
            for backend, client in self.manager.torrent_clients():
                try:
                    if backend == "aria2" and isinstance(client, Aria2Client):
                        network_guard = client.network_guard_status()
                        break
                finally:
                    client.close()
        return {
            "backend": (
                self.manager.torrent_backend_name()
                if self._downloads_enabled()
                else "disabled"
            ),
            "downloads": [
                self._download_payload(item, anime_by_id) for item in rows
            ],
            "storage": self._storage_payload(refresh=False),
            "network_guard": network_guard,
            "warning": warning,
        }

    def torrent_download_action(
        self,
        torrent_hash: str,
        action: str,
        delete_files: bool = False,
        backend: str = "",
    ) -> dict[str, Any]:
        value = str(torrent_hash or "").strip()
        command = str(action or "").strip().casefold()
        backend_hint = str(backend or "").strip().casefold()
        if command == "stop-all":
            stopped = 0
            errors: list[str] = []
            for backend_name, client in self.manager.torrent_clients():
                try:
                    rows = client.torrents(
                        category=(self.config.qbittorrent.category if backend_name == "qbittorrent" else "")
                    )
                    for item in rows:
                        try:
                            client.pause(item.torrent_hash)
                            stopped += 1
                        except Exception as exc:
                            errors.append(f"{backend_name}: {exc}")
                finally:
                    client.close()
            result = self.torrent_downloads(refresh=True)
            result["stopped"] = stopped
            if errors:
                result["warning"] = "; ".join(errors)
            return result
        known = next(
            (
                item
                for item in self.manager.db.downloads()
                if item.torrent_hash.casefold() == value.casefold()
            ),
            None,
        )
        if known is None:
            raise ValueError("Download is not managed by Pudge")
        if command not in {"pause", "resume", "reconnect", "remove"}:
            raise ValueError("Unknown download action")
        known_backends = [
            str(name).casefold()
            for name in (known.raw or {}).get("_backends", [])
            if str(name).strip()
        ]
        if len(known_backends) > 1:
            backend_hint = ""
        elif not backend_hint:
            backend_hint = str((known.raw or {}).get("backend") or "").casefold()
        clients = self.manager.torrent_clients()
        selected = [
            (name, client) for name, client in clients
            if (
                (known_backends and name in known_backends)
                or (not known_backends and (not backend_hint or name == backend_hint))
            )
        ]
        if not selected:
            for _name, client in clients:
                client.close()
            raise ValueError("Torrent backend is no longer enabled")
        try:
            for name, client in selected:
                if command == "pause":
                    client.pause(value)
                elif command == "resume":
                    client.start(value)
                elif command == "reconnect":
                    reconnect = getattr(client, "reconnect", None)
                    if not callable(reconnect):
                        raise ValueError("Reconnect is available for the built-in torrent client")
                    reconnect(value)
                else:
                    client.delete(value, delete_files=bool(delete_files))
        finally:
            for _name, client in clients:
                client.close()
        if command == "remove":
            self.manager.db.delete_torrent_records(value)
        else:
            try:
                self.manager.sync_downloads()
            except Exception:
                pass
        return self.torrent_downloads(refresh=False)

    def _sync_anilist(self) -> dict[str, Any]:
        with self._anilist_sync_lock:
            with timed_step(self.logger, "web.anilist_sync"):
                stats = self.manager.refresh_anilist_cache()
        return {"skipped": False, "stats": stats, "state": self.get_state()}

    def startup_sync_anilist(self) -> dict[str, Any]:
        with self._anilist_sync_lock:
            if self._startup_anilist_sync_done:
                return {"skipped": True, "stats": {"anime": 0, "covers": 0}, "state": self.get_state()}
            self._startup_anilist_sync_done = True
            try:
                last_sync = float(self.manager.db.get_state("anilist_synced_at", "0") or 0)
            except ValueError:
                last_sync = 0.0
            # run_startup_once may already have performed the periodic refresh.
            # Avoid contacting AniList twice within the same startup sequence.
            if time.time() - last_sync < 60:
                return {"skipped": True, "stats": {"anime": 0, "covers": 0}, "state": self.get_state()}
            with timed_step(self.logger, "startup.phase.anilist", order=2):
                stats = self.manager.refresh_anilist_cache()
        return {"skipped": False, "stats": stats, "state": self.get_state()}

    def sync_anilist(self) -> dict[str, Any]:
        return self._sync_anilist()

    def refresh_local(self) -> dict[str, Any]:
        if not self._local_refresh_lock.acquire(blocking=False):
            return {"skipped": True, "stats": {}, "state": self.get_state()}
        try:
            with timed_step(self.logger, "web.refresh_local"):
                # Manual Refresh must remain interactive even if the launch agent
                # is currently aligning subtitles. Release discovery runs now;
                # expensive subtitle preparation is queued separately.
                stats = self.manager.run_interactive_refresh()
                try:
                    ln_added = self.light_novels.auto_download_missing()
                    if ln_added:
                        stats["light_novels_auto"] = len(ln_added)
                except Exception as exc:
                    self.logger.warning("LN auto-download skipped: %s", exc)
                # A newly added magnet can need a brief qBittorrent metadata
                # round-trip before its state is visible. Reconcile once in the
                # same Refresh so the user never has to press the button twice.
                if stats.get("auto", 0) and self._downloads_enabled():
                    try:
                        with timed_step(self.logger, "web.qbittorrent_after_auto"):
                            stats["downloads_after_auto"] = self.manager.sync_downloads()
                    except Exception as exc:
                        self.manager.log(str(exc))
                        stats["downloads_after_auto"] = 0
            return {"skipped": False, "stats": stats, "state": self.get_state()}
        finally:
            self._local_refresh_lock.release()

    def refresh_all(self) -> dict[str, Any]:
        # Backwards-compatible alias. This no longer contacts AniList.
        return self.refresh_local()

    def _run_startup_maintenance_background(self) -> None:
        stats: dict[str, Any] = {}
        error = ""
        try:
            with timed_step(self.logger, "startup.phase.local_nyaa_subtitles", order=1):
                stats = self.manager.run_startup_once()
                # Refresh once more so the UI can immediately monitor newly added torrents.
                if stats.get("auto", 0) and self._downloads_enabled():
                    try:
                        with timed_step(self.logger, "startup.qbittorrent_after_auto"):
                            stats["downloads_after_auto"] = self.manager.sync_downloads()
                    except Exception as exc:
                        self.manager.log(str(exc))
                        stats["downloads_after_auto"] = 0
        except Exception as exc:
            error = str(exc)
            self.logger.exception("FAIL step=startup.phase.local_nyaa_subtitles")
        finally:
            with self._startup_maintenance_lock:
                self._startup_maintenance_stats = stats
                self._startup_maintenance_error = error
                self._startup_maintenance_done = True

    def startup_maintenance(self) -> dict[str, Any]:
        """Start the complete launch maintenance pass without blocking the UI."""
        with self._startup_maintenance_lock:
            thread = self._startup_maintenance_thread
            if self._startup_maintenance_done:
                return {
                    "skipped": True,
                    "running": False,
                    "done": True,
                    "stats": self._startup_maintenance_stats,
                    "error": self._startup_maintenance_error,
                    "state": self.get_state(),
                }
            if thread is None or not thread.is_alive():
                thread = threading.Thread(
                    target=self._run_startup_maintenance_background,
                    name=f"{APP_SLUG}-startup-maintenance",
                    daemon=True,
                )
                self._startup_maintenance_thread = thread
                thread.start()
        return {
            "skipped": False,
            "running": True,
            "done": False,
            "stats": {},
            "error": "",
            "state": self.get_state_fast(),
        }

    def startup_maintenance_status(self) -> dict[str, Any]:
        with self._startup_maintenance_lock:
            thread = self._startup_maintenance_thread
            running = bool(thread is not None and thread.is_alive())
            done = bool(self._startup_maintenance_done)
            stats = dict(self._startup_maintenance_stats)
            error = self._startup_maintenance_error
        return {
            "running": running,
            "done": done,
            "stats": stats,
            "error": error,
            "state": self.get_state_fast() if running else self.get_state(),
        }

    def poll_downloads_and_subtitles(self) -> dict[str, Any]:
        """Energy-efficient foreground poll.

        Ordinary polling remains lightweight. When a torrent has just completed,
        only that video's high-priority subtitle job is started immediately if no
        full maintenance refresh currently owns the shared lock.
        """
        if not self._download_poll_lock.acquire(blocking=False):
            return {"skipped": True, "stats": {}, "state": self.get_state()}
        stats = {"downloads": 0, "subs": 0}
        try:
            with timed_step(self.logger, "foreground.poll", mode="downloads-only"):
                try:
                    completed_paths: tuple[Path, ...] = ()
                    try:
                        with timed_step(self.logger, "foreground.qbittorrent"):
                            stats["downloads"] = self.manager.sync_downloads()
                        completed_paths = self.manager.last_completed_video_paths
                    except Exception as exc:
                        # Subtitle preparation is independent from the torrent Web
                        # API. If qBittorrent is closed, keep draining due/manual
                        # subtitle jobs instead of leaving Checking stuck forever.
                        self.manager.log(str(exc))
                        self.logger.warning(
                            "FALLBACK step=foreground.qbittorrent reason=unavailable continue=subtitles error=%r",
                            str(exc),
                        )
                    with maintenance_lock(
                        self.config.paths.cache_dir,
                        blocking=False,
                    ) as maintenance_acquired:
                        inbox = self.manager.scan_subtitle_inbox()
                        if int(inbox.get("requeued", 0) or 0):
                            stats["inbox_requeued"] = int(inbox.get("requeued", 0) or 0)
                        if completed_paths and maintenance_acquired:
                            with timed_step(
                                self.logger,
                                "foreground.subtitle_new_completion",
                                videos=len(completed_paths),
                            ):
                                stats["subs"] = self.manager.process_subtitle_jobs(
                                    limit=max(1, len(completed_paths)),
                                    preferred_paths=completed_paths,
                                )
                        elif maintenance_acquired and int(inbox.get("requeued", 0) or 0):
                            with timed_step(self.logger, "foreground.subtitle_inbox"):
                                stats["subs"] = self.manager.process_subtitle_jobs(
                                    limit=min(12, max(1, int(inbox.get("requeued", 0) or 0)))
                                )
                        elif completed_paths:
                            self.logger.info(
                                "QUEUE step=foreground.subtitle_new_completion videos=%s "
                                "reason=maintenance_active priority=100",
                                len(completed_paths),
                            )
                        elif maintenance_acquired and self.manager.db.priority_subtitle_job_count(min_priority=200):
                            manual_count = self.manager.db.priority_subtitle_job_count(min_priority=200)
                            with timed_step(
                                self.logger,
                                "foreground.subtitle_manual_refresh",
                                jobs=manual_count,
                            ):
                                stats["subs"] = self.manager.process_subtitle_jobs(
                                    limit=min(8, max(1, manual_count))
                                )
                        elif maintenance_acquired and self.manager.db.subtitle_jobs():
                            with timed_step(self.logger, "foreground.subtitle_due_jobs"):
                                stats["subs"] = self.manager.process_subtitle_jobs(limit=2)
                        queued_manual = self.manager.db.priority_subtitle_job_count(min_priority=200)
                        if queued_manual:
                            stats["subtitle_check_queued"] = queued_manual
                        if maintenance_acquired:
                            with timed_step(self.logger, "foreground.qbittorrent_tag_cleanup"):
                                tag_stats = self.manager.cleanup_qbittorrent_tags()
                                stats.update(
                                    {
                                        key: value
                                        for key, value in tag_stats.items()
                                        if int(value or 0) > 0
                                    }
                                )
                    missing_rows = int(self.manager._last_missing_episode_rows or 0)
                    if missing_rows:
                        # An externally deleted torrent/file used to leave a stale
                        # episode row that blocked the missing-episode search. Run
                        # exactly one targeted auto-search after reconciliation;
                        # ordinary foreground polls remain downloads-only.
                        with timed_step(
                            self.logger,
                            "foreground.nyaa_after_missing_video",
                            removed_rows=missing_rows,
                        ):
                            stats["auto"] = self.manager.auto_search_current()
                        if stats["auto"]:
                            stats["downloads_after_auto"] = self.manager.sync_downloads()
                except Exception as exc:
                    self.manager.log(str(exc))
            return {"skipped": False, "stats": stats, "state": self.get_state()}
        finally:
            self._download_poll_lock.release()

    def light_novel_state(self) -> dict[str, Any]:
        state = self.light_novels.state()
        for book in state.get("books", []):
            book["paired_audio"] = self.audiobooks.link_for_light_novel(int(book["id"]))
        return state

    def manga_state(self) -> dict[str, Any]:
        return self.manga.state()

    @staticmethod
    def _manga_anilist_search_text(value: str) -> str:
        # Keep AniList lookup and local series grouping on exactly the same
        # filename normalization rules.
        return MangaService.series_title(value)

    def manga_search_anilist(self, query: str) -> list[dict[str, Any]]:
        if not self.config.anilist.enabled or not self.config.anilist.access_token:
            return []
        cleaned = self._manga_anilist_search_text(query)
        if not cleaned:
            return []
        gql = """
        query($search:String!){Page(page:1,perPage:12){media(search:$search,type:MANGA,sort:SEARCH_MATCH){id format chapters volumes status title{userPreferred romaji english native}coverImage{large}siteUrl mediaListEntry{status progress progressVolumes score(format:POINT_10)}}}}
        """
        data = self.light_novels._anilist_post(gql, {"search": cleaned})
        rows: list[dict[str, Any]] = []
        for media in (data.get("Page") or {}).get("media") or []:
            media_format = str(media.get("format") or "").upper()
            if media_format not in {"MANGA", "ONE_SHOT"}:
                continue
            titles = media.get("title") or {}
            entry = media.get("mediaListEntry") or {}
            rows.append(
                {
                    "media_id": int(media.get("id")),
                    "title": titles.get("userPreferred") or titles.get("romaji") or titles.get("native") or "",
                    "format": media_format,
                    "chapters": media.get("chapters"),
                    "volumes": media.get("volumes"),
                    "media_status": media.get("status") or "",
                    "list_status": entry.get("status") or "",
                    "progress": entry.get("progress") or 0,
                    "user_score": float(entry.get("score")) if entry.get("score") is not None else None,
                    "cover": (media.get("coverImage") or {}).get("large") or "",
                    "site_url": media.get("siteUrl") or f"https://anilist.co/manga/{int(media.get('id'))}",
                }
            )
        return rows

    def manga_bind_anilist(
        self, book_id: int, media_id: int, selection: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        item = dict(selection or {})
        if int(item.get("media_id") or 0) != int(media_id):
            rows = self.manga_search_anilist(str(self.manga._book(int(book_id))["title"]))
            item = next((row for row in rows if int(row.get("media_id") or 0) == int(media_id)), {})
        book = self.manga.bind_anilist(
            int(book_id),
            int(media_id),
            cover_url=str(item.get("cover") or ""),
            site_url=str(item.get("site_url") or f"https://anilist.co/manga/{int(media_id)}"),
            user_score=item.get("user_score"),
        )
        return {"book": book, "state": self.manga.state()}

    def manga_unbind_anilist(self, book_id: int) -> dict[str, Any]:
        book = self.manga.unbind_anilist(int(book_id))
        return {"book": book, "state": self.manga.state()}

    def _manga_ocr_log_path(self) -> Path:
        return DEFAULT_LOG_PATH.with_name(f"{APP_SLUG}-manga-ocr-install.log")

    def _manga_ocr_marker_path(self) -> Path:
        return self.config.paths.cache_dir / "manga-ocr" / "model-ready.json"

    def manga_ocr_status(self) -> dict[str, Any]:
        with self._manga_ocr_install_lock:
            state = dict(self._manga_ocr_install_state)
            thread = self._manga_ocr_install_thread
            running = bool(thread is not None and thread.is_alive())
        installed = self.manga.ocr_available(refresh=not running)
        marker = self._manga_ocr_marker_path()
        model_ready = marker.is_file()
        if running:
            state["running"] = True
        elif installed and model_ready:
            state.update({"state": "ready", "running": False})
        elif installed:
            state.update({"state": "package_installed", "running": False})
        else:
            state.update({"state": "not_installed", "running": False})
        state["installed"] = installed
        state["model_ready"] = model_ready
        state["log_path"] = str(self._manga_ocr_log_path())
        return state

    def _set_manga_ocr_install_state(self, state: str, detail: str = "") -> None:
        with self._manga_ocr_install_lock:
            self._manga_ocr_install_state["state"] = state
            self._manga_ocr_install_state["detail"] = detail
            if state in {"ready", "failed"}:
                self._manga_ocr_install_state["finished_at"] = time.time()

    def _run_manga_ocr_install(self) -> None:
        log_path = self._manga_ocr_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        python = python_executable()
        try:
            with log_path.open("w", encoding="utf-8") as log:
                self._set_manga_ocr_install_state("installing_package", "Installing manga-ocr and dependencies")
                log.write(f"Python: {python}\n")
                log.flush()
                completed = subprocess.run(
                    [python, "-m", "pip", "install", "manga-ocr>=0.1.14,<1"],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=30 * 60,
                )
                if completed.returncode != 0:
                    raise RuntimeError(f"pip exited with code {completed.returncode}")
                self.manga.invalidate_ocr_availability()
                if not self.manga.ocr_available(refresh=True):
                    raise RuntimeError("manga_ocr package is still unavailable after pip install")
                self._set_manga_ocr_install_state("downloading_model", "Downloading and warming MangaOCR model")
                log.write("\nPackage installed. Loading MangaOCR model...\n")
                log.flush()
                completed = subprocess.run(
                    [
                        python,
                        "-c",
                        "from manga_ocr import MangaOcr; MangaOcr(); print('MangaOCR model ready')",
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=45 * 60,
                )
                if completed.returncode != 0:
                    raise RuntimeError(f"model preload exited with code {completed.returncode}")
                marker = self._manga_ocr_marker_path()
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(
                    json.dumps({"ready_at": time.time(), "python": python}, ensure_ascii=False),
                    encoding="utf-8",
                )
                self._set_manga_ocr_install_state("ready", "MangaOCR and model are ready")
        except Exception as exc:
            self.logger.exception("FAIL step=manga_ocr.install error=%r", str(exc))
            self._set_manga_ocr_install_state("failed", str(exc))

    def install_manga_ocr(self) -> dict[str, Any]:
        with self._manga_ocr_install_lock:
            thread = self._manga_ocr_install_thread
            already_running = bool(thread is not None and thread.is_alive())
            if not already_running:
                self._manga_ocr_install_state = {
                "state": "starting",
                "detail": "Starting MangaOCR installation",
                "started_at": time.time(),
                    "finished_at": 0.0,
                }
                thread = threading.Thread(
                    target=self._run_manga_ocr_install,
                    name=f"{APP_SLUG}-manga-ocr-install",
                    daemon=True,
                )
                self._manga_ocr_install_thread = thread
                thread.start()
        return self.manga_ocr_status()

    def reveal_manga_ocr_install_log(self) -> dict[str, Any]:
        path = self._manga_ocr_log_path()
        if not path.exists():
            return {"ok": False, "path": str(path)}
        subprocess.run(["open", "-R", str(path)], check=False)
        return {"ok": True, "path": str(path)}

    def manga_ocr_book_status(self, book_id: int) -> dict[str, Any]:
        book_id = int(book_id)
        cache = self.manga.ocr_cache_status(book_id)
        with self._manga_book_ocr_lock:
            state = dict(self._manga_book_ocr_state.get(book_id, {}))
            thread = self._manga_book_ocr_threads.get(book_id)
            running = bool(thread is not None and thread.is_alive())
        if not state:
            state = {"state": "ready" if cache["complete"] else "idle", "errors": []}
        state.update(cache)
        state["running"] = running
        if cache["complete"] and not running:
            state["state"] = "ready"
        return state

    def _run_manga_book_ocr(self, book_id: int) -> None:
        def on_progress(done: int, total: int, page_index: int | None) -> None:
            with self._manga_book_ocr_lock:
                self._manga_book_ocr_state[book_id] = {
                    "state": "running",
                    "running": True,
                    "cached_pages": int(done),
                    "total_pages": int(total),
                    "page_index": page_index,
                    "errors": [],
                }
            job_id = self._manga_ocr_job_ids.get(book_id, "")
            if job_id:
                self.job_center.update(
                    job_id,
                    state="running",
                    current=done,
                    total=total,
                    message=f"OCR page {min(total, done + 1)}/{total}",
                )

        try:
            cancel_event = self._manga_ocr_cancel_events.get(book_id)
            result = self.manga.ocr_book(
                book_id,
                progress=on_progress,
                cancelled=cancel_event.is_set if cancel_event is not None else None,
            )
            if result.get("cancelled"):
                with self._manga_book_ocr_lock:
                    self._manga_book_ocr_state[book_id] = {
                        **result,
                        "state": "cancelled",
                        "running": False,
                        "errors": [],
                    }
                job_id = self._manga_ocr_job_ids.get(book_id, "")
                if job_id:
                    self.job_center.cancelled(job_id)
                return
            region_texts = self.manga.cached_region_texts(book_id)
            parse_errors: list[str] = []
            parsed_count = 0
            for page_index, text in region_texts:
                if cancel_event is not None and cancel_event.is_set():
                    with self._manga_book_ocr_lock:
                        self._manga_book_ocr_state[book_id] = {
                            **result,
                            "state": "cancelled",
                            "running": False,
                            "parsed_regions": parsed_count,
                            "total_regions": len(region_texts),
                            "errors": parse_errors,
                        }
                    job_id = self._manga_ocr_job_ids.get(book_id, "")
                    if job_id:
                        self.job_center.cancelled(job_id)
                    return
                with self._manga_book_ocr_lock:
                    self._manga_book_ocr_state[book_id] = {
                        **result,
                        "state": "parsing",
                        "running": True,
                        "parsed_regions": parsed_count,
                        "total_regions": len(region_texts),
                        "page_index": page_index,
                        "errors": parse_errors,
                    }
                try:
                    self.light_novels.parse_study_text(text)
                    parsed_count += 1
                except LightNovelError as exc:
                    if not parse_errors:
                        parse_errors.append(str(exc))
                    break
            with self._manga_book_ocr_lock:
                self._manga_book_ocr_state[book_id] = {
                    **result,
                    "state": "ready" if result.get("complete") else "partial",
                    "running": False,
                    "parsed_regions": parsed_count,
                    "total_regions": len(region_texts),
                    "errors": [*result.get("errors", []), *parse_errors],
                }
            job_id = self._manga_ocr_job_ids.get(book_id, "")
            if job_id:
                self.job_center.finish(
                    job_id,
                    message="Manga OCR ready",
                    result={"book_id": book_id, "cached_pages": result.get("cached_pages", 0)},
                )
        except Exception as exc:
            self.logger.exception("FAIL step=manga_ocr.book book_id=%s error=%r", book_id, str(exc))
            cache = self.manga.ocr_cache_status(book_id)
            with self._manga_book_ocr_lock:
                self._manga_book_ocr_state[book_id] = {
                    **cache,
                    "state": "failed",
                    "running": False,
                    "errors": [str(exc)],
                }
            job_id = self._manga_ocr_job_ids.get(book_id, "")
            if job_id:
                self.job_center.fail(job_id, exc)
        finally:
            self._manga_ocr_cancel_events.pop(book_id, None)

    def _run_serialized_manga_book_ocr(self, book_id: int) -> None:
        # MangaOCR is a large model. Serializing volumes prevents two imports
        # from holding separate model processes in memory at the same time.
        with self._manga_ocr_run_lock:
            self._run_manga_book_ocr(int(book_id))

    def start_manga_ocr_book(
        self,
        book_id: int,
        refresh: bool = False,
        attempt_of: str = "",
    ) -> dict[str, Any]:
        book_id = int(book_id)
        if not self.manga.ocr_available():
            raise RuntimeError("MangaOCR is not installed. Install it from Settings → Reading.")
        with self._manga_book_ocr_lock:
            thread = self._manga_book_ocr_threads.get(book_id)
            if thread is None or not thread.is_alive():
                if bool(refresh):
                    self.manga.invalidate_region_cache(book_id)
                self._manga_book_ocr_state[book_id] = {
                    **self.manga.ocr_cache_status(book_id),
                    "state": "queued",
                    "running": True,
                    "errors": [],
                }
                cancel_event = threading.Event()
                self._manga_ocr_cancel_events[book_id] = cancel_event
                try:
                    title = str(self.manga._book(book_id)["title"] or f"Manga {book_id}")
                except Exception:
                    title = f"Manga {book_id}"
                self._manga_ocr_job_ids[book_id] = self.job_center.start(
                    "ocr",
                    f"OCR · {title}",
                    payload={"book_id": book_id},
                    total=float(self.manga.ocr_cache_status(book_id).get("total_pages") or 0),
                    attempt_of=str(attempt_of or ""),
                )
                thread = threading.Thread(
                    target=self._run_serialized_manga_book_ocr,
                    args=(book_id,),
                    name=f"{APP_SLUG}-manga-ocr-book-{book_id}",
                    daemon=True,
                )
                self._manga_book_ocr_threads[book_id] = thread
                thread.start()
        return self.manga_ocr_book_status(book_id)

    def choose_manga_file(self) -> dict[str, Any]:
        if self.window is None:
            return {"cancelled": True}
        try:
            import webview

            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=("Manga archives (*.cbz;*.zip)",),
            )
        except Exception as exc:
            return {"cancelled": True, "error": str(exc)}
        if not result:
            return {"cancelled": True}
        selected = list(result) if isinstance(result, (list, tuple)) else [result]
        paths = [str(value) for value in selected]
        job_id = self.job_center.start(
            "import",
            "Import manga",
            payload={"media_kind": "manga", "paths": paths},
            total=len(paths),
        )
        cancel_event = threading.Event()
        self._import_cancel_events[job_id] = cancel_event
        books: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, value in enumerate(selected, 1):
            if cancel_event.is_set():
                break
            try:
                book = self.manga.import_file(Path(str(value)))
                books.append(book)
                if self.manga.ocr_available():
                    self.start_manga_ocr_book(int(book["id"]))
            except Exception as exc:
                errors.append(f"{Path(str(value)).name}: {exc}")
            self.job_center.update(
                job_id,
                state="running",
                current=index,
                total=len(selected),
                message=f"Imported {len(books)}/{len(selected)}",
            )
        self._import_cancel_events.pop(job_id, None)
        if cancel_event.is_set():
            self.job_center.cancelled(job_id)
        elif books:
            self.job_center.finish(
                job_id,
                message=f"Imported {len(books)}; errors {len(errors)}",
                result={"book_ids": [int(book["id"]) for book in books], "errors": errors},
            )
        else:
            self.job_center.fail(job_id, " • ".join(errors) or "Import failed")
        return {"cancelled": False, "books": books, "errors": errors, "state": self.manga.state()}

    def manga_page(self, book_id: int, page_index: int) -> dict[str, Any]:
        return self.manga.page(int(book_id), int(page_index))

    def manga_ocr_page(self, book_id: int, page_index: int) -> dict[str, Any]:
        return self.manga.ocr_page(int(book_id), int(page_index))

    def manga_text_regions(
        self,
        book_id: int,
        page_index: int,
        refresh: bool = False,
        cached_only: bool = False,
    ) -> dict[str, Any]:
        return self.manga.text_regions(
            int(book_id),
            int(page_index),
            refresh=bool(refresh),
            cached_only=bool(cached_only),
        )

    def manga_ocr_cached_page(self, book_id: int, page_index: int) -> dict[str, Any]:
        return self.manga.cached_ocr_page(int(book_id), int(page_index))

    def audiobook_state(self) -> dict[str, Any]:
        return self.audiobooks.state()

    def job_center_state(self) -> dict[str, Any]:
        jobs = self.job_center.jobs()
        return {
            "jobs": jobs,
            "active_count": sum(1 for job in jobs if job.get("can_cancel")),
        }

    def job_center_cancel(self, job_id: str) -> dict[str, Any]:
        job = self.job_center.get(str(job_id))
        if job is None:
            raise KeyError(f"Unknown job {job_id}")
        kind = str(job.get("kind") or "")
        payload = job.get("payload") or {}
        requested = self.job_center.request_cancel(str(job_id))
        if not requested:
            return self.job_center_state()
        if kind == "nyaa" and str(job_id) == self._planning_episode_job_id:
            if self._planning_episode_cancel_event is not None:
                self._planning_episode_cancel_event.set()
        elif kind == "ocr":
            event = self._manga_ocr_cancel_events.get(int(payload.get("book_id") or 0))
            if event is not None:
                event.set()
        elif kind == "stt":
            self.audiobooks.cancel_transcription(int(payload.get("audiobook_id") or 0))
        elif kind == "import":
            event = self._import_cancel_events.get(str(job_id))
            if event is not None:
                event.set()
        if kind == "import" and str(job_id) not in self._import_cancel_events:
            self.job_center.cancelled(str(job_id))
        return self.job_center_state()

    def _retry_import_worker(
        self,
        job_id: str,
        media_kind: str,
        paths: list[str],
        cancel_event: threading.Event,
    ) -> None:
        imported = 0
        errors: list[str] = []
        try:
            for index, raw in enumerate(paths, 1):
                if cancel_event.is_set():
                    self.job_center.cancelled(job_id)
                    return
                path = Path(str(raw))
                try:
                    if media_kind == "manga":
                        self.manga.import_file(path)
                    elif media_kind == "audiobook_folder":
                        self.audiobooks.import_folder(path)
                    elif media_kind == "audiobook":
                        self.audiobooks.import_file(path)
                    else:
                        book = self.light_novels.import_file(path)
                        self.audiobooks.auto_link_light_novel(int(book["id"]))
                    imported += 1
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
                self.job_center.update(
                    job_id,
                    state="running",
                    current=index,
                    total=len(paths),
                    message=f"Imported {imported}/{len(paths)}",
                )
            if cancel_event.is_set():
                self.job_center.cancelled(job_id)
                return
            if imported:
                self.job_center.finish(
                    job_id,
                    message=f"Imported {imported}; errors {len(errors)}",
                    result={"imported": imported, "errors": errors},
                )
            else:
                self.job_center.fail(job_id, " • ".join(errors) or "Import failed")
        finally:
            self._import_cancel_events.pop(job_id, None)

    def job_center_retry(self, job_id: str) -> dict[str, Any]:
        previous = self.job_center.get(str(job_id))
        if previous is None:
            raise KeyError(f"Unknown job {job_id}")
        if not bool(previous.get("can_retry")):
            raise ValueError("Only failed or cancelled jobs can be retried")
        kind = str(previous.get("kind") or "")
        payload = previous.get("payload") or {}
        if kind == "nyaa":
            self.start_planning_episode_download(
                int(payload.get("media_id") or 0), attempt_of=str(job_id)
            )
        elif kind == "ocr":
            self.start_manga_ocr_book(
                int(payload.get("book_id") or 0),
                refresh=True,
                attempt_of=str(job_id),
            )
        elif kind == "stt":
            self.audiobooks.prepare_transcription(
                int(payload.get("audiobook_id") or 0),
                force=True,
                attempt_of=str(job_id),
            )
        elif kind == "import":
            paths = [str(value) for value in payload.get("paths") or []]
            media_kind = str(payload.get("media_kind") or "light_novel")
            retry_id = self.job_center.start(
                "import",
                str(previous.get("title") or "Import"),
                payload={"media_kind": media_kind, "paths": paths},
                total=len(paths),
                attempt_of=str(job_id),
            )
            event = threading.Event()
            self._import_cancel_events[retry_id] = event
            threading.Thread(
                target=self._retry_import_worker,
                args=(retry_id, media_kind, paths, event),
                name=f"{APP_SLUG}-import-retry",
                daemon=True,
            ).start()
        else:
            raise ValueError(f"Job type {kind} cannot be retried")
        return self.job_center_state()

    def choose_audiobook_file(self) -> dict[str, Any]:
        if self.window is None:
            return {"cancelled": True}
        try:
            import webview

            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=("Audiobooks (*.m4b;*.m4a;*.mp3;*.aac;*.opus;*.ogg;*.flac;*.wav)",),
            )
        except Exception as exc:
            return {"cancelled": True, "error": str(exc)}
        if not result:
            return {"cancelled": True}
        selected = list(result) if isinstance(result, (list, tuple)) else [result]
        paths = [str(value) for value in selected]
        job_id = self.job_center.start(
            "import",
            "Import audiobooks",
            payload={"media_kind": "audiobook", "paths": paths},
            total=len(paths),
        )
        cancel_event = threading.Event()
        self._import_cancel_events[job_id] = cancel_event
        books: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, value in enumerate(selected, 1):
            if cancel_event.is_set():
                break
            try:
                books.append(self.audiobooks.import_file(Path(str(value))))
            except Exception as exc:
                errors.append(f"{Path(str(value)).name}: {exc}")
            self.job_center.update(
                job_id,
                state="running",
                current=index,
                total=len(selected),
                message=f"Imported {len(books)}/{len(selected)}",
            )
        self._import_cancel_events.pop(job_id, None)
        if cancel_event.is_set():
            self.job_center.cancelled(job_id)
        elif books:
            self.job_center.finish(
                job_id,
                message=f"Imported {len(books)}; errors {len(errors)}",
                result={"book_ids": [int(book["id"]) for book in books], "errors": errors},
            )
        else:
            self.job_center.fail(job_id, " • ".join(errors) or "Import failed")
        return {"cancelled": False, "books": books, "errors": errors, "state": self.audiobooks.state()}

    def choose_audiobook_folder(self) -> dict[str, Any]:
        if self.window is None:
            return {"cancelled": True}
        try:
            import webview
            result = self.window.create_file_dialog(webview.FOLDER_DIALOG, directory=str(Path.home()), allow_multiple=False)
        except Exception as exc:
            return {"cancelled": True, "error": str(exc)}
        if not result:
            return {"cancelled": True}
        raw = result[0] if isinstance(result, (list, tuple)) else result
        job_id = self.job_center.start(
            "import",
            "Import audiobook folder",
            payload={"media_kind": "audiobook_folder", "paths": [str(raw)]},
            total=1,
        )
        cancel_event = threading.Event()
        self._import_cancel_events[job_id] = cancel_event
        try:
            book = self.audiobooks.import_folder(Path(str(raw)))
        except Exception as exc:
            self._import_cancel_events.pop(job_id, None)
            if cancel_event.is_set():
                self.job_center.cancelled(job_id)
                return {"cancelled": True, "state": self.audiobooks.state()}
            self.job_center.fail(job_id, exc)
            return {"cancelled": False, "error": str(exc), "state": self.audiobooks.state()}
        self._import_cancel_events.pop(job_id, None)
        if cancel_event.is_set():
            self.job_center.cancelled(job_id)
            return {"cancelled": True, "state": self.audiobooks.state()}
        self.job_center.finish(
            job_id,
            message="Audiobook folder imported",
            result={"book_ids": [int(book["id"])]},
        )
        return {"cancelled": False, "book": book, "state": self.audiobooks.state()}

    def audiobook_play(self, book_id: int, start: float | None = None, speed: float = 1.0) -> dict[str, Any]:
        return self.audiobooks.play(int(book_id), None if start is None else float(start), float(speed or 1.0))

    def audiobook_set_speed(self, book_id: int, speed: float) -> dict[str, Any]:
        result = self.audiobooks.set_speed(int(book_id), float(speed or 1.0))
        result["state"] = self.audiobooks.state()
        return result

    def audiobook_seek(self, book_id: int, seconds: float) -> dict[str, Any]:
        result = self.audiobooks.seek(int(book_id), float(seconds))
        result["state"] = self.audiobooks.state()
        return result

    def audiobook_seek_to(self, book_id: int, position: float) -> dict[str, Any]:
        result = self.audiobooks.seek_to(int(book_id), float(position))
        result["state"] = self.audiobooks.state()
        return result

    def audiobook_stop(self, book_id: int) -> dict[str, Any]:
        result = self.audiobooks.stop(int(book_id))
        result["state"] = self.audiobooks.state()
        return result

    def audiobook_sleep_timer(self, book_id: int, mode: str) -> dict[str, Any]:
        value = str(mode or "off").strip().lower()
        if value == "chapter":
            result = self.audiobooks.set_sleep_timer(int(book_id), end_of_chapter=True)
        elif value in {"15", "30", "45", "60"}:
            result = self.audiobooks.set_sleep_timer(int(book_id), seconds=int(value) * 60)
        else:
            result = self.audiobooks.set_sleep_timer(int(book_id))
        result["state"] = self.audiobooks.state()
        return result

    def audiobook_add_bookmark(self, book_id: int, title: str = "") -> dict[str, Any]:
        result = self.audiobooks.add_bookmark(int(book_id), str(title or ""))
        result["state"] = self.audiobooks.state()
        return result

    def audiobook_delete_bookmark(self, bookmark_id: int) -> dict[str, Any]:
        result = self.audiobooks.delete_bookmark(int(bookmark_id))
        result["state"] = self.audiobooks.state()
        return result

    def audiobook_mark_finished(self, book_id: int, finished: bool = True) -> dict[str, Any]:
        result = self.audiobooks.mark_finished(int(book_id), bool(finished))
        result["state"] = self.audiobooks.state()
        return result

    def audiobook_delete(self, book_id: int) -> dict[str, Any]:
        result = self.audiobooks.delete(int(book_id), delete_files=False)
        result["state"] = self.audiobooks.state()
        return result

    def light_novel_link_audiobook(self, book_id: int, audiobook_id: int) -> dict[str, Any]:
        result = self.audiobooks.link_light_novel(int(book_id), int(audiobook_id))
        result["state"] = self.audiobooks.state()
        return result

    def light_novel_unlink_audiobook(self, book_id: int) -> dict[str, Any]:
        return self.audiobooks.unlink_light_novel(int(book_id))

    def light_novel_play_paired(
        self,
        book_id: int,
        chapter_index: int,
        chapter_progress: float,
        speed: float | None = None,
    ) -> dict[str, Any]:
        return self.audiobooks.play_paired(
            int(book_id),
            int(chapter_index),
            float(chapter_progress),
            None if speed is None else float(speed),
        )

    def light_novel_play_paired_at_offset(
        self,
        book_id: int,
        chapter_index: int,
        character_offset: int,
        speed: float | None = None,
    ) -> dict[str, Any]:
        return self.audiobooks.play_paired_at_offset(
            int(book_id),
            int(chapter_index),
            int(character_offset),
            None if speed is None else float(speed),
        )

    def light_novel_paired_state(self, book_id: int) -> dict[str, Any]:
        return self.audiobooks.paired_state(int(book_id))

    def light_novel_prepare_audio_alignment(
        self, book_id: int, force: bool = False
    ) -> dict[str, Any]:
        return self.audiobooks.prepare_alignment(int(book_id), force=bool(force))

    def light_novel_refresh(self) -> dict[str, Any]:
        return self.light_novels.refresh_state()

    def study_parse_text(self, text: str) -> dict[str, Any]:
        return self.light_novels.parse_study_text(str(text or ""))

    def study_decks(self, backend: str) -> list[dict[str, Any]]:
        return self.light_novels.decks(str(backend or "jiten"))

    def study_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = payload or {}
        return self.light_novels.study_action(
            str(payload.get("backend") or self.light_novels.settings().study_backend),
            str(payload.get("action") or "review"),
            int(payload.get("word_id") or 0),
            int(payload.get("reading_index") or 0),
            grade=str(payload.get("grade") or "good"),
            sentence=str(payload.get("sentence") or ""),
            deck_id=payload.get("deck_id"),
        )

    def translate_text(
        self,
        text: str,
        context: str = "",
        target_language: str = "",
        media_id: int | None = None,
    ) -> dict[str, Any]:
        return self.light_novels.translate_selection(
            str(text or ""),
            str(context or ""),
            str(target_language or "") or None,
            int(media_id) if media_id else None,
        )

    def light_novel_translate(
        self,
        text: str,
        context: str = "",
        target_language: str = "",
        media_id: int | None = None,
    ) -> dict[str, Any]:
        return self.light_novels.translate_selection(
            str(text or ""),
            str(context or ""),
            str(target_language or "") or None,
            int(media_id) if media_id else None,
        )

    def choose_light_novel_file(self) -> dict[str, Any]:
        if self.window is None:
            return {"cancelled": True}
        try:
            import webview
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                allow_multiple=True,
                file_types=("Light novels (*.epub;*.txt)", "EPUB (*.epub)", "Text (*.txt)"),
            )
        except Exception as exc:
            return {"cancelled": True, "error": str(exc)}
        if not result:
            return {"cancelled": True}
        selected = list(result) if isinstance(result, (list, tuple)) else [result]
        paths = [str(value) for value in selected]
        job_id = self.job_center.start(
            "import",
            "Import Light Novels",
            payload={"media_kind": "light_novel", "paths": paths},
            total=len(paths),
        )
        cancel_event = threading.Event()
        self._import_cancel_events[job_id] = cancel_event
        books: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, value in enumerate(selected, 1):
            if cancel_event.is_set():
                break
            try:
                book = self.light_novels.import_file(Path(str(value)))
                books.append(book)
                self.audiobooks.auto_link_light_novel(int(book["id"]))
            except Exception as exc:
                errors.append(f"{Path(str(value)).name}: {exc}")
            self.job_center.update(
                job_id,
                state="running",
                current=index,
                total=len(selected),
                message=f"Imported {len(books)}/{len(selected)}",
            )
        self._import_cancel_events.pop(job_id, None)
        if cancel_event.is_set():
            self.job_center.cancelled(job_id)
        elif books:
            self.job_center.finish(
                job_id,
                message=f"Imported {len(books)}; errors {len(errors)}",
                result={"book_ids": [int(book["id"]) for book in books], "errors": errors},
            )
        else:
            self.job_center.fail(job_id, " • ".join(errors) or "Import failed")
        state = self.light_novels.state()
        return {"cancelled": False, "books": books, "book": books[0] if books else None, "errors": errors, "state": state}

    def light_novel_open(self, book_id: int) -> dict[str, Any]:
        book = self.light_novels.open_book(int(book_id))
        book["paired_audio"] = self.audiobooks.link_for_light_novel(int(book_id))
        return book

    def light_novel_chapter(self, book_id: int, chapter_index: int) -> dict[str, Any]:
        return self.light_novels.chapter_fast(int(book_id), int(chapter_index))

    def light_novel_chapter_parse_status(self, book_id: int, chapter_index: int) -> dict[str, Any]:
        return self.light_novels.chapter_parse_status(int(book_id), int(chapter_index))

    def light_novel_cancel_reader_background(self) -> dict[str, Any]:
        self.light_novels.cancel_reader_background()
        return {"ok": True}

    def light_novel_position(self, book_id: int, chapter_index: int, offset: float) -> dict[str, Any]:
        self.light_novels.update_position(int(book_id), int(chapter_index), float(offset))
        return {"ok": True}

    def light_novel_bookmark(
        self,
        book_id: int,
        chapter_index: int,
        offset: float,
        source: str = "manual",
    ) -> dict[str, Any]:
        bookmark = self.light_novels.save_bookmark(
            int(book_id),
            int(chapter_index),
            float(offset),
            source=str(source or "manual"),
        )
        return {
            "ok": True,
            "bookmark": bookmark,
            "current_chapter": bookmark["chapter_index"],
            "current_offset": bookmark["offset"],
            "bookmark_source": bookmark["source"],
            "bookmark_updated_at": bookmark["updated_at"],
        }

    def light_novel_reset_position(self, book_id: int) -> dict[str, Any]:
        book = self.light_novels.reset_position(int(book_id))
        return {"ok": True, "book": book, "state": self.light_novel_state()}

    def light_novel_finish_volume(self, book_id: int) -> dict[str, Any]:
        return self.light_novels.finish_volume(int(book_id))

    def light_novel_set_finished(self, book_id: int, finished: bool) -> dict[str, Any]:
        return self.light_novels.set_finished(int(book_id), bool(finished))

    def light_novel_delete(self, book_id: int) -> dict[str, Any]:
        self.audiobooks.unlink_light_novel(int(book_id))
        result = self.light_novels.delete_book(int(book_id), delete_file=False)
        result["state"] = self.light_novel_state()
        return result

    def light_novel_bind_anilist(self, book_id: int, media_id: int, selection: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.light_novels.bind_anilist(
            int(book_id), int(media_id), selection if isinstance(selection, dict) else None
        )
        self.audiobooks.auto_link_light_novel(int(book_id))
        return result

    def light_novel_unbind_anilist(self, book_id: int) -> dict[str, Any]:
        return self.light_novels.unbind_anilist(int(book_id))

    def light_novel_search_anilist(self, query: str) -> list[dict[str, Any]]:
        return self.light_novels.search_anilist_novels(str(query or "").strip())

    def character_glossary(self, media_id: int) -> list[dict[str, str]]:
        return self.light_novels.character_glossary(int(media_id))

    def save_character_glossary_override(self, media_id: int, source: str, preferred: str) -> list[dict[str, str]]:
        return self.light_novels.save_character_glossary_override(int(media_id), source, preferred)

    def delete_character_glossary_override(self, media_id: int, source: str) -> list[dict[str, str]]:
        return self.light_novels.delete_character_glossary_override(int(media_id), source)

    def media_identity_search(self, kind: str, query: str) -> list[dict[str, Any]]:
        normalized = str(kind or "").strip().lower()
        if normalized in {"novel", "light_novel", "ln", "audiobook"}:
            return self.light_novels.search_anilist_novels(str(query or "").strip())
        if normalized == "manga":
            return self.manga_search_anilist(str(query or "").strip())
        return self.planning_search_anilist(str(query or "").strip())

    def media_identity_current(self, kind: str, local_id: int) -> dict[str, Any]:
        normalized = str(kind or "").strip().lower()
        if normalized in {"novel", "light_novel", "ln"}:
            return self.light_novels.book(int(local_id))
        if normalized == "manga":
            return self.manga._book(int(local_id))
        with self.manager.db.connect() as conn:
            row = conn.execute("SELECT * FROM media_identities WHERE kind=? AND local_id=?", (normalized, int(local_id))).fetchone()
        return dict(row) if row is not None else {"kind": normalized, "local_id": int(local_id)}

    def media_identity_bind(self, kind: str, local_id: int, media_id: int, selection: dict[str, Any] | None = None) -> dict[str, Any]:
        normalized = str(kind or "").strip().lower()
        item = dict(selection or {})
        if normalized in {"novel", "light_novel", "ln"}:
            result = self.light_novels.bind_anilist(int(local_id), int(media_id), item)
            self.audiobooks.auto_link_light_novel(int(local_id))
            return result
        if normalized == "manga":
            return self.manga_bind_anilist(int(local_id), int(media_id), item)
        with self.manager.db.connect() as conn:
            conn.execute(
                "INSERT INTO media_identities(kind,local_id,anilist_id,anilist_type,title,cover_url,site_url,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(kind,local_id) DO UPDATE SET anilist_id=excluded.anilist_id,anilist_type=excluded.anilist_type,title=excluded.title,cover_url=excluded.cover_url,site_url=excluded.site_url,updated_at=excluded.updated_at",
                (normalized, int(local_id), int(media_id), "ANIME" if item.get("media_kind") == "anime" else "MANGA", str(item.get("title") or ""), str(item.get("cover") or ""), str(item.get("site_url") or ""), time.time()),
            )
        if normalized == "audiobook":
            self.audiobooks.auto_link_audiobook(int(local_id))
        return self.media_identity_current(normalized, int(local_id))

    def media_identity_unbind(self, kind: str, local_id: int) -> dict[str, Any]:
        normalized = str(kind or "").strip().lower()
        if normalized in {"novel", "light_novel", "ln"}:
            return self.light_novels.unbind_anilist(int(local_id))
        if normalized == "manga":
            return self.manga_unbind_anilist(int(local_id))
        with self.manager.db.connect() as conn:
            conn.execute("DELETE FROM media_identities WHERE kind=? AND local_id=?", (normalized, int(local_id)))
        return {"kind": normalized, "local_id": int(local_id)}

    def set_literature_score(
        self,
        kind: str,
        book_id: int,
        media_id: int,
        score: float,
    ) -> dict[str, Any]:
        normalized = str(kind or "").strip().lower()
        if normalized not in {"manga", "novel", "light_novel"}:
            raise ValueError("Literature kind must be manga or novel")
        value = max(1.0, min(10.0, float(score)))
        client = self._anilist_client()
        try:
            client.set_score(int(media_id), value)
        finally:
            client.close()
        if normalized == "manga":
            book = self.manga.set_score(int(book_id), value)
            return {"ok": True, "book": book, "state": self.manga.state()}
        if normalized in {"novel", "light_novel"}:
            book = self.light_novels.set_score(int(book_id), value)
            return {"ok": True, "book": book, "state": self.light_novel_state()}
        raise AssertionError("unreachable literature kind")

    def light_novel_save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        return self.light_novels.save_settings(values or {})

    def light_novel_generate_reader_css(
        self,
        request: str,
        current_css: str = "",
    ) -> dict[str, str]:
        return self.light_novels.generate_reader_css(str(request or ""), str(current_css or ""))

    def light_novel_test_study(self, backend: str) -> dict[str, Any]:
        return self.light_novels.test_study(str(backend or "jiten"))

    def light_novel_decks(self, backend: str) -> list[dict[str, Any]]:
        return self.light_novels.decks(str(backend or "jiten"))

    def light_novel_study_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = payload or {}
        return self.light_novels.study_action(
            str(payload.get("backend") or self.light_novels.settings().study_backend),
            str(payload.get("action") or "review"),
            int(payload.get("word_id") or 0),
            int(payload.get("reading_index") or 0),
            grade=str(payload.get("grade") or "good"),
            sentence=str(payload.get("sentence") or ""),
            deck_id=payload.get("deck_id"),
        )

    def light_novel_search_nyaa(
        self,
        query: str,
        target_volume: int | None = None,
    ) -> list[dict[str, Any]]:
        return self.light_novels.search_nyaa(
            str(query or "").strip(),
            target_volume=int(target_volume or 0) or None,
        )

    def audiobook_search_light_novel_nyaa(self, audiobook_id: int) -> dict[str, Any]:
        book = self.audiobooks.book(int(audiobook_id))
        linked = book.get("linked_light_novel") or {}
        title = str(
            linked.get("title")
            or book.get("anilist_title")
            or book.get("title")
            or ""
        ).strip()
        volume = int(linked.get("volume") or 0)
        query = self.light_novels._nyaa_title(title)
        return {
            "query": query,
            "category": "3_3",
            "target_volume": volume or None,
            "releases": self.light_novels.search_nyaa(
                query, target_volume=volume or None
            ),
        }

    def light_novel_download_nyaa(self, release: dict[str, Any]) -> dict[str, Any]:
        return self.light_novels.download_nyaa_release(release or {})

    def light_novel_auto_download(self) -> list[dict[str, Any]]:
        return self.light_novels.auto_download_missing()

    def scan_library(self) -> dict[str, Any]:
        if self._downloads_enabled():
            try:
                self.manager.sync_downloads()
            except Exception as exc:
                self.manager.log(str(exc))
        self.manager.scan_library()
        return self.get_state()

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        cfg = self.config
        ln_current = self.light_novels.settings()
        ln_values = {
            "jiten_api_key": values.get("ln_jiten_api_key", ln_current.jiten_api_key),
            "jpdb_api_token": values.get("ln_jpdb_api_token", ln_current.jpdb_api_token),
            "study_backend": values.get("ln_study_backend", ln_current.study_backend),
            "show_furigana": values.get("ln_show_furigana", ln_current.show_furigana),
            "show_pitch_accent": values.get(
                "ln_show_pitch_accent", ln_current.show_pitch_accent
            ),
            "custom_css": values.get("ln_custom_css", ln_current.custom_css),
            "parse_ahead": values.get("ln_parse_ahead", ln_current.parse_ahead),
            "auto_download_nyaa": values.get("ln_auto_download_nyaa", ln_current.auto_download_nyaa),
            "nyaa_category": values.get("ln_nyaa_category", ln_current.nyaa_category),
            "reader_font": values.get("ln_reader_font", ln_current.reader_font),
            "reader_theme": values.get("ln_reader_theme", ln_current.reader_theme),
            "reader_font_size": values.get("ln_reader_font_size", ln_current.reader_font_size),
            "reader_text_color": values.get("ln_reader_text_color", ln_current.reader_text_color),
            "reader_background_color": values.get("ln_reader_background_color", ln_current.reader_background_color),
            "reader_width": values.get("ln_reader_width", ln_current.reader_width),
            "reader_line_height": values.get("ln_reader_line_height", ln_current.reader_line_height),
            "reader_indent": values.get("ln_reader_indent", ln_current.reader_indent),
            "reader_vertical": values.get("ln_reader_vertical", ln_current.reader_vertical),
            "reader_mode": values.get("ln_reader_mode", ln_current.reader_mode),
            "auto_bookmarks": values.get(
                "ln_auto_bookmarks", ln_current.auto_bookmarks
            ),
        }
        old_ocr_enabled = bool(cfg.matching.ocr_image_subtitles)
        old_jimaku_key = str(cfg.jimaku.api_key or "")
        old_subtitle_dirs = tuple(str(path.expanduser()) for path in cfg.paths.subtitle_dirs)
        old_watched_dirs = tuple(str(path.expanduser()) for path in cfg.paths.download_dirs)
        old_anilist_connection = (
            bool(cfg.anilist.enabled),
            str(cfg.anilist.client_id or ""),
            str(cfg.anilist.access_token or ""),
        )
        language = str(values.get("language", cfg.ui.language)).strip().lower()
        cfg.ui.language = language if language in {"en", "ru"} else "en"
        cfg.ui.escape_exits_fullscreen = bool(
            values.get("escape_exits_fullscreen", cfg.ui.escape_exits_fullscreen)
        )
        cfg.ui.notifications_enabled = bool(
            values.get("notifications_enabled", cfg.ui.notifications_enabled)
        )
        cfg.ui.jiten_developer_tools_confirmed = bool(
            values.get(
                "jiten_developer_tools_confirmed",
                cfg.ui.jiten_developer_tools_confirmed,
            )
        )
        cfg.library.root_dir = Path(str(values.get("library_root", cfg.library.root_dir))).expanduser()
        def _folder_list(value: object) -> list[Path]:
            raw = str(value or "").replace(";", "\n")
            result: list[Path] = []
            seen: set[str] = set()
            for part in raw.splitlines():
                text = part.strip()
                if not text:
                    continue
                path = Path(text).expanduser()
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                result.append(path)
            return result

        current_subtitles = "\n".join(str(path) for path in cfg.paths.subtitle_dirs)
        cfg.paths.subtitle_dirs = _folder_list(
            values.get("subtitle_folders", values.get("subtitle_folder", current_subtitles))
        )
        current_watched = "\n".join(str(path) for path in cfg.paths.download_dirs)
        cfg.paths.download_dirs = _folder_list(values.get("watched_folders", current_watched))
        cfg.library.disk_limit_enabled = bool(values.get("disk_limit_enabled", cfg.library.disk_limit_enabled))
        cfg.library.disk_limit_gb = max(0.0, float(values.get("disk_limit_gb", cfg.library.disk_limit_gb)))
        cfg.playback.enabled = True
        cfg.playback.rewind_seconds = 10.0
        cfg.nyaa.enabled = bool(values.get("nyaa_enabled", cfg.nyaa.enabled))
        cfg.nyaa.auto_download_current = bool(values.get("nyaa_auto", cfg.nyaa.auto_download_current))
        cfg.nyaa.subsplease_rss_enabled = bool(
            values.get("subsplease_rss_enabled", cfg.nyaa.subsplease_rss_enabled)
        )
        cfg.nyaa.subsplease_rss_preferred = bool(
            values.get("subsplease_rss_preferred", cfg.nyaa.subsplease_rss_preferred)
        )
        cfg.nyaa.base_url = str(values.get("nyaa_url", cfg.nyaa.base_url)).strip().rstrip("/")
        cfg.nyaa.proxy_mode = str(values.get("proxy_mode", cfg.nyaa.proxy_mode)).strip()
        cfg.nyaa.proxy_url = str(values.get("proxy_url", cfg.nyaa.proxy_url)).strip()
        cfg.nyaa.pre_search_command = str(values.get("search_hook", cfg.nyaa.pre_search_command)).strip()
        cfg.nyaa.min_release_score = float(values.get("min_score", cfg.nyaa.min_release_score))
        resolution = str(values.get("preferred_resolution", cfg.nyaa.preferred_resolution)).strip().casefold() or "1080p"
        resolution_aliases = {"4k": "2160p", "best": "highest", "max": "highest", "higher": "highest"}
        resolution = resolution_aliases.get(resolution, resolution)
        if resolution not in {"480p", "720p", "1080p", "1440p", "2160p", "highest"}:
            resolution = "1080p"
        cfg.nyaa.preferred_resolution = resolution
        cfg.nyaa.preferred_video_codecs = [
            part.strip()
            for part in str(
                values.get(
                    "preferred_video_codecs",
                    ", ".join(cfg.nyaa.preferred_video_codecs),
                )
            ).split(",")
            if part.strip()
        ]
        cfg.nyaa.preferred_sources = [
            part.strip()
            for part in str(
                values.get("preferred_sources", ", ".join(cfg.nyaa.preferred_sources))
            ).split(",")
            if part.strip()
        ]
        cfg.nyaa.require_japanese_audio = True
        cfg.nyaa.avoid_upscaled = True
        cfg.nyaa.only_trusted_groups = bool(
            values.get("only_trusted_groups", cfg.nyaa.only_trusted_groups)
        )
        cfg.nyaa.trusted_groups = [
            part.strip()
            for part in str(
                values.get("trusted_groups", ", ".join(cfg.nyaa.trusted_groups))
            ).split(",")
            if part.strip()
        ]
        cfg.nyaa.preferred_groups = [
            part.strip()
            for part in str(
                values.get("preferred_groups", ", ".join(cfg.nyaa.preferred_groups))
            ).split(",")
            if part.strip()
        ]
        cfg.nyaa.blocked_groups = [
            part.strip()
            for part in str(
                values.get("blocked_groups", ", ".join(cfg.nyaa.blocked_groups))
            ).split(",")
            if part.strip()
        ]
        cfg.nyaa.auto_upgrade_downloaded = bool(
            values.get("auto_upgrade_downloaded", cfg.nyaa.auto_upgrade_downloaded)
        )
        cfg.nyaa.upgrade_min_score_gain = 30.0
        cfg.nyaa.upgrade_check_hours = max(
            0.0, float(values.get("upgrade_check_hours", cfg.nyaa.upgrade_check_hours))
        )
        cfg.nyaa.max_upgrade_checks_per_run = max(
            0, int(values.get("max_upgrade_checks_per_run", cfg.nyaa.max_upgrade_checks_per_run))
        )
        cfg.qbittorrent.enabled = bool(values.get("qbt_enabled", cfg.qbittorrent.enabled))
        cfg.qbittorrent.base_url = str(values.get("qbt_url", cfg.qbittorrent.base_url)).strip().rstrip("/")
        cfg.qbittorrent.username = str(values.get("qbt_user", cfg.qbittorrent.username)).strip()
        cfg.qbittorrent.password = str(values.get("qbt_password", cfg.qbittorrent.password))
        cfg.qbittorrent.api_key = str(values.get("qbt_api_key", cfg.qbittorrent.api_key)).strip()
        cfg.qbittorrent.pre_download_command = str(values.get("download_hook", cfg.qbittorrent.pre_download_command)).strip()
        cfg.aria2.enabled = bool(values.get("aria2_enabled", cfg.aria2.enabled))
        cfg.aria2.binary = "aria2c"
        cfg.aria2.rpc_port = max(1024, min(65535, int(values.get("aria2_rpc_port", cfg.aria2.rpc_port))))
        seed_mode = str(values.get("aria2_seed_mode", cfg.aria2.seed_mode)).strip().casefold()
        cfg.aria2.seed_mode = seed_mode if seed_mode in {"off", "ratio", "ratio_or_time", "unlimited"} else "off"
        cfg.aria2.seed_ratio = max(0.0, float(values.get("aria2_seed_ratio", cfg.aria2.seed_ratio)))
        cfg.aria2.seed_time_minutes = max(0.0, float(values.get("aria2_seed_time_minutes", cfg.aria2.seed_time_minutes)))
        cfg.aria2.upload_limit_kib = max(0, int(values.get("aria2_upload_limit_kib", cfg.aria2.upload_limit_kib)))
        cfg.aria2.vpn_interface = str(values.get("aria2_vpn_interface", cfg.aria2.vpn_interface)).strip()
        cfg.aria2.vpn_kill_switch = bool(values.get("aria2_vpn_kill_switch", cfg.aria2.vpn_kill_switch))
        cfg.agent.enabled = bool(values.get("agent_enabled", cfg.agent.enabled))
        cfg.agent.poll_minutes = max(5, int(values.get("agent_poll", cfg.agent.poll_minutes)))
        cfg.agent.anilist_refresh_minutes = max(5, int(values.get("anilist_refresh_poll", cfg.agent.anilist_refresh_minutes)))
        cfg.agent.subtitle_poll_minutes = max(5, int(values.get("subtitle_poll", cfg.agent.subtitle_poll_minutes)))
        cfg.agent.delete_after_watched_hours = max(0.0, float(values.get("delete_hours", cfg.agent.delete_after_watched_hours)))
        cfg.anilist.enabled = bool(values.get("anilist_enabled", cfg.anilist.enabled))
        cfg.anilist.client_id = str(values.get("anilist_client_id", cfg.anilist.client_id)).strip()
        cfg.anilist.access_token = str(values.get("anilist_token", cfg.anilist.access_token)).strip()
        cfg.anilist.auto_update_progress = bool(values.get("anilist_auto_progress", cfg.anilist.auto_update_progress))
        cfg.anilist.add_if_missing = bool(values.get("anilist_add_if_missing", cfg.anilist.add_if_missing))
        cfg.anilist.watched_threshold = 0.85
        cfg.anilist.watched_max_remaining_minutes = 10.0
        cfg.anilist.relations_by_release_date = bool(
            values.get(
                "relations_by_release_date",
                cfg.anilist.relations_by_release_date,
            )
        )
        cfg.shortcuts.mpv_mark_watched = str(values.get("shortcut_mpv_mark_watched", cfg.shortcuts.mpv_mark_watched)).strip()
        cfg.shortcuts.mpv_open_anilist = str(values.get("shortcut_mpv_open_anilist", cfg.shortcuts.mpv_open_anilist)).strip()
        cfg.shortcuts.mpv_correct_match = str(values.get("shortcut_mpv_correct_match", cfg.shortcuts.mpv_correct_match)).strip()
        cfg.shortcuts.mpv_translate_subtitle = str(values.get("shortcut_mpv_translate_subtitle", cfg.shortcuts.mpv_translate_subtitle)).strip()
        requested_study_plugin = str(
            values.get("mpv_study_plugin", cfg.tools.mpv_study_plugin)
        ).strip().casefold()
        cfg.tools.mpv_study_plugin = (
            requested_study_plugin
            if requested_study_plugin in {"auto", "jiten", "jpdb"}
            else "auto"
        )
        cfg.diagnostics.energy_monitoring_enabled = bool(
            values.get("energy_monitoring_enabled", cfg.diagnostics.energy_monitoring_enabled)
        )
        cfg.diagnostics.energy_sample_seconds = max(
            10.0, float(values.get("energy_sample_seconds", cfg.diagnostics.energy_sample_seconds))
        )
        personal_jimaku_key = str(
            values.get("jimaku_api_key", cfg.jimaku.personal_api_key)
        ).strip()
        cfg.jimaku.personal_api_key = personal_jimaku_key
        cfg.jimaku.api_key = personal_jimaku_key
        apply_jimaku_trial(cfg)
        cfg.matching.ocr_image_subtitles = bool(
            values.get("ocr_image_subtitles", cfg.matching.ocr_image_subtitles)
        )
        cfg.matching.auto_upgrade_subtitles = True
        cfg.matching.subtitle_upgrade_min_score_gain = 25.0
        cfg.matching.subtitle_upgrade_check_hours = 6.0
        cfg.matching.max_subtitle_upgrade_checks_per_run = 2
        cfg.llm.enabled = bool(values.get("llm_enabled", cfg.llm.enabled))
        cfg.llm.base_url = str(values.get("llm_url", cfg.llm.base_url)).strip().rstrip("/")
        cfg.llm.api_key = str(values.get("llm_api_key", cfg.llm.api_key)).strip()
        cfg.llm.model = str(values.get("llm_model", cfg.llm.model)).strip()
        cfg.llm.validate_embedded_reference = bool(
            values.get("subtitle_semantic_checks", cfg.llm.validate_embedded_reference)
        )
        cfg.sync.use_container_chapters = True
        cfg.sync.japanese_stt_fallback = True
        cfg.sync.japanese_stt_model = str(
            values.get("japanese_stt_model", cfg.sync.japanese_stt_model)
        ).strip() or "mlx-community/whisper-tiny"
        write_config(cfg, self.config_path)
        self.audiobooks.stop_all()
        self.config = load_config(self.config_path)
        self.manager = AnimeManager(self.config, log=self.logger.info)
        self.light_novels = LightNovelService(self.config, logger=self.logger)
        self.manga = MangaService(
            self.manager.db,
            cache_dir=self.config.paths.cache_dir,
            python=python_executable(),
            work_scheduler=self.manager.work_scheduler,
        )
        self.audiobooks = AudiobookService(
            self.manager.db,
            ffprobe=self.config.tools.ffprobe,
            mpv=self.config.tools.mpv,
            cache_dir=self.config.paths.cache_dir,
            ffmpeg=self.config.tools.ffmpeg,
            python=python_executable(),
            stt_model=self.config.sync.japanese_stt_model,
            work_scheduler=self.manager.work_scheduler,
        )
        threading.Thread(
            target=self.audiobooks.resume_pending_transcriptions,
            name="audiobook-stt-resume",
            daemon=True,
        ).start()
        self._planning_search_cache = MetadataCache(
            self.config.paths.cache_dir,
            "anilist-planning-search",
            schema="v2",
        )
        self.debug_snapshots = DebugSnapshotService(
            self.manager,
            cache_dir=self.config.paths.cache_dir,
            runtime_log_path=DEFAULT_LOG_PATH,
        )
        self.light_novels.save_settings(ln_values)
        configure_mpv_study_keys(
            jiten_api_key=str(ln_values.get("jiten_api_key") or ""),
            jpdb_api_token=str(ln_values.get("jpdb_api_token") or ""),
        )
        self.energy_monitor.update_interval(self.config.diagnostics.energy_sample_seconds)
        if self.config.diagnostics.energy_monitoring_enabled:
            self.energy_monitor.start()
        else:
            self.energy_monitor.stop()

        # Settings that affect video readiness are reconciled immediately. The
        # returned state is rendered by the UI before any manual Refresh.
        reconcile_stats: dict[str, int] = {}
        new_ocr_enabled = bool(self.config.matching.ocr_image_subtitles)
        if old_ocr_enabled and not new_ocr_enabled:
            invalidated = self.manager.invalidate_disabled_ocr_subtitles()
            reconcile_stats["ocr_invalidated"] = len(invalidated)

        new_subtitle_dirs = tuple(str(path.expanduser()) for path in self.config.paths.subtitle_dirs)
        new_watched_dirs = tuple(str(path.expanduser()) for path in self.config.paths.download_dirs)
        resolver_settings_changed = (
            old_jimaku_key != str(self.config.jimaku.api_key or "")
            or old_subtitle_dirs != new_subtitle_dirs
        )
        if resolver_settings_changed:
            reconcile_stats["subtitle_requeued"] = self.manager.db.force_requeue_unresolved_subtitle_jobs()
        if old_watched_dirs != new_watched_dirs:
            try:
                reconcile_stats["library"] = len(self.manager.scan_library())
            except Exception as exc:
                self.logger.warning("FALLBACK step=settings.instant_library_scan error=%r", str(exc))

        new_anilist_connection = (
            bool(self.config.anilist.enabled),
            str(self.config.anilist.client_id or ""),
            str(self.config.anilist.access_token or ""),
        )
        anilist_refresh_error = ""
        if (
            new_anilist_connection != old_anilist_connection
            and new_anilist_connection[0]
            and new_anilist_connection[1]
            and new_anilist_connection[2]
        ):
            try:
                with self._anilist_sync_lock:
                    anilist_stats = self.manager.refresh_anilist_cache()
                reconcile_stats["anilist"] = int(anilist_stats.get("anime") or 0)
            except Exception as exc:
                anilist_refresh_error = str(exc)
                self.logger.warning(
                    "RETRY step=settings.auto_anilist_refresh error=%r",
                    anilist_refresh_error,
                )

        folder_access = request_folder_access(
            [self.config.library.root_dir, *self.config.paths.download_dirs, *self.config.paths.subtitle_dirs]
        )
        self.logger.info(
            "EVENT settings.saved language=%s watched_dirs=%s subtitle_dirs=%s folder_access=%s",
            self.config.ui.language,
            [str(path) for path in self.config.paths.download_dirs],
            [str(path) for path in self.config.paths.subtitle_dirs],
            folder_access,
        )
        state = self.get_state_fast()
        return {
            "ok": True,
            "settings": self._settings_payload(),
            "folder_access": folder_access,
            "reconcile": reconcile_stats,
            "recheck_subtitles": bool(
                reconcile_stats.get("ocr_invalidated")
                or reconcile_stats.get("subtitle_requeued")
            ),
            "anilist_refresh_error": anilist_refresh_error,
            "state": state,
        }

    def complete_onboarding(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.save_settings(values or {})
        self.config.ui.onboarding_completed = True
        write_config(self.config, self.config_path)
        result["settings"] = self._settings_payload()
        self.logger.info("EVENT onboarding.completed")
        return result

    def first_experience_dependencies(self) -> dict[str, Any]:
        ln = self.light_novels.settings()
        status = dependency_status(
            mpv=self.config.tools.mpv,
            ffmpeg=self.config.tools.ffmpeg,
            jiten_api_key=ln.jiten_api_key,
            jpdb_api_token=ln.jpdb_api_token,
            selected_plugin=self.config.tools.mpv_study_plugin,
        )
        manga_ocr = dict(self.manga_ocr_status())
        package_installed = bool(manga_ocr.get("installed"))
        ready = bool(package_installed and manga_ocr.get("model_ready"))
        manga_ocr["package_installed"] = package_installed
        manga_ocr["installed"] = ready
        manga_ocr["version"] = "Package + model ready" if ready else (
            "Model not ready" if package_installed else "Not installed"
        )
        status["manga_ocr"] = manga_ocr
        return status

    def install_first_experience_dependencies(
        self, values: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        options = values or {}
        status = install_media_tools(
            mpv=self.config.tools.mpv,
            ffmpeg=self.config.tools.ffmpeg,
        )
        self.config.tools.mpv = str(status["mpv"]["path"])
        self.config.tools.ffmpeg = str(status["ffmpeg"]["path"])
        ffprobe = shutil.which("ffprobe")
        ffmpeg_path = Path(self.config.tools.ffmpeg)
        sibling_ffprobe = ffmpeg_path.with_name("ffprobe")
        if sibling_ffprobe.is_file():
            ffprobe = str(sibling_ffprobe)
        if ffprobe:
            self.config.tools.ffprobe = str(ffprobe)
        key = str(
            options.get("jiten_api_key")
            or self.light_novels.settings().jiten_api_key
            or ""
        ).strip()
        jpdb_key = str(
            options.get("jpdb_api_token")
            or self.light_novels.settings().jpdb_api_token
            or ""
        ).strip()
        if bool(options.get("install_jiten_mpv")):
            status = install_jiten_mpv(
                key,
                mpv=self.config.tools.mpv,
                ffmpeg=self.config.tools.ffmpeg,
            )
        if key:
            self.light_novels.save_settings({"jiten_api_key": key})
        if jpdb_key:
            self.light_novels.save_settings({"jpdb_api_token": jpdb_key})
        selected_plugin = str(
            options.get("mpv_study_plugin") or self.config.tools.mpv_study_plugin
        ).strip().casefold()
        if selected_plugin in {"auto", "jiten", "jpdb"}:
            self.config.tools.mpv_study_plugin = selected_plugin
        configure_mpv_study_keys(jiten_api_key=key, jpdb_api_token=jpdb_key)
        manga_ocr = self.manga_ocr_status()
        if not (bool(manga_ocr.get("installed")) and bool(manga_ocr.get("model_ready"))):
            self._run_manga_ocr_install()
            manga_ocr = self.manga_ocr_status()
        if not (bool(manga_ocr.get("installed")) and bool(manga_ocr.get("model_ready"))):
            raise RuntimeError("MangaOCR installation did not complete")

        status = dependency_status(
            mpv=self.config.tools.mpv,
            ffmpeg=self.config.tools.ffmpeg,
            jiten_api_key=key,
            jpdb_api_token=jpdb_key,
            selected_plugin=self.config.tools.mpv_study_plugin,
        )
        manga_ocr = dict(manga_ocr)
        manga_ocr["package_installed"] = True
        manga_ocr["installed"] = True
        manga_ocr["version"] = "Package + model ready"
        status["manga_ocr"] = manga_ocr
        write_config(self.config, self.config_path)
        for service, name, value in (
            (self.audiobooks, "mpv", self.config.tools.mpv),
            (self.audiobooks, "ffmpeg", self.config.tools.ffmpeg),
            (self.audiobooks, "ffprobe", self.config.tools.ffprobe),
        ):
            if hasattr(service, name):
                setattr(service, name, value)
        self.logger.info(
            "EVENT onboarding.dependencies mpv=%s ffmpeg=%s jiten_mpv=%s",
            status["mpv"]["installed"],
            status["ffmpeg"]["installed"],
            status["jiten_mpv"]["installed"],
        )
        return status

    def skip_onboarding(self) -> dict[str, Any]:
        self.config.ui.onboarding_completed = True
        # Skipping setup also skips the optional external subtitle folder.
        self.config.paths.subtitle_dirs = []
        write_config(self.config, self.config_path)
        self.logger.info("EVENT onboarding.skipped")
        return {"ok": True, "settings": self._settings_payload()}

    def _anilist_client(self) -> AniListClient:
        if not self.config.anilist.enabled or not self.config.anilist.access_token.strip():
            raise RuntimeError("AniList integration is not configured")
        return AniListClient(
            self.config.anilist.endpoint,
            access_token=self.config.anilist.access_token,
        )

    @staticmethod
    def _find_relation_metadata(
        relations: list[dict[str, Any]],
        media_id: int,
    ) -> dict[str, Any] | None:
        for item in relations:
            try:
                item_id = int(item.get("media_id"))
            except (TypeError, ValueError):
                item_id = 0
            if item_id == int(media_id):
                return item
            children = item.get("relations")
            if isinstance(children, list):
                found = WebAppApi._find_relation_metadata(children, media_id)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _overlay_relation_status(
        relations: list[dict[str, Any]],
        media_id: int,
        status: str,
    ) -> bool:
        changed = False
        normalized = str(status or "").upper()
        for item in relations:
            try:
                item_id = int(item.get("media_id"))
            except (TypeError, ValueError):
                item_id = 0
            if item_id == int(media_id):
                item["list_status"] = "" if normalized == "REMOVED" else normalized
                if normalized == "REMOVED":
                    item["progress"] = 0
                    item["watched"] = False
                changed = True
            children = item.get("relations")
            if isinstance(children, list):
                changed = (
                    WebAppApi._overlay_relation_status(children, media_id, status)
                    or changed
                )
        return changed

    def _set_local_anilist_status(self, media_id: int, status: str) -> None:
        media_id = int(media_id)
        status = str(status or "").upper()
        anime = self.manager.db.get_anime(media_id)
        if anime is not None:
            self.manager.db.set_anime_status(media_id, status)
        elif status in {"CURRENT", "PLANNING"}:
            metadata = None
            for parent in self.manager.db.anime_list():
                metadata = self._find_relation_metadata(parent.relations, media_id)
                if metadata is not None:
                    break
            self.manager.db.upsert_anime(
                LibraryAnime(
                    media_id=media_id,
                    title=str((metadata or {}).get("title") or f"AniList #{media_id}"),
                    titles=[str((metadata or {}).get("title") or f"AniList #{media_id}")],
                    cover_url=str((metadata or {}).get("cover_url") or ""),
                    site_url=str(
                        (metadata or {}).get("site_url")
                        or f"https://anilist.co/anime/{media_id}"
                    ),
                    status=status,
                    episodes=(
                        int(metadata["episodes"])
                        if (metadata or {}).get("episodes")
                        else None
                    ),
                    format=str((metadata or {}).get("format") or "") or None,
                    season_year=(
                        int(metadata["season_year"])
                        if (metadata or {}).get("season_year")
                        else None
                    ),
                    start_date=str((metadata or {}).get("start_date") or "") or None,
                    studio=str((metadata or {}).get("studio") or ""),
                    media_status=str((metadata or {}).get("media_status") or "") or None,
                )
            )

        # Nested relation nodes have their own list status in the cached graph.
        # Update those optimistically too, so the UI reflects a successful
        # mutation even when AniList's following read request returns HTTP 500.
        for parent in self.manager.db.anime_list():
            if self._overlay_relation_status(parent.relations, media_id, status):
                self.manager.db.upsert_anime(parent)

    def _refresh_anilist_after_mutation(self) -> tuple[dict[str, object], str]:
        try:
            stats = self.manager.refresh_anilist_cache()
        except Exception as exc:
            warning = (
                "AniList сохранил изменение, но не отдал обновлённый список; "
                "используются локальные данные"
            )
            self.logger.warning(
                "FALLBACK step=anilist.post_mutation_refresh error=%r",
                exc,
            )
            return {"anime": 0, "covers": 0, "cached": True}, warning
        warning = str(stats.get("warning") or "")
        return stats, warning

    def set_anilist_score(self, media_id: int, score: float, episode: int | None = None) -> dict[str, Any]:
        client = self._anilist_client()
        try:
            saved = client.set_score(int(media_id), float(score))
        finally:
            client.close()
        self.manager.db.set_anime_score(int(media_id), float(score))
        if episode is not None:
            self.manager.db.mark_rating_prompted(int(media_id), int(episode))
        return {"ok": True, "score": float(saved.get("score") or score), "state": self.get_state()}

    def skip_rating(self, media_id: int, episode: int) -> dict[str, Any]:
        self.manager.db.mark_rating_prompted(int(media_id), int(episode))
        return {"ok": True}

    def reset_anime_progress(self, media_id: int) -> dict[str, Any]:
        client = self._anilist_client()
        try:
            saved = client.set_progress(int(media_id), 0, "CURRENT")
        finally:
            client.close()
        reset = self.manager.db.reset_anime_progress(int(media_id))
        self.logger.info(
            "EVENT step=anime.reset_progress media_id=%s local_episodes=%s",
            media_id, reset,
        )
        return {
            "ok": True,
            "progress": int(saved.get("progress") or 0),
            "local_episodes": reset,
            "state": self.get_state(),
        }

    def drop_anime(self, media_id: int) -> dict[str, Any]:
        client = self._anilist_client()
        try:
            client.set_list_status(int(media_id), "DROPPED")
        finally:
            client.close()
        self._set_local_anilist_status(int(media_id), "DROPPED")
        queued = self.manager.db.schedule_anime_cleanup(
            int(media_id),
            self.config.agent.delete_after_watched_hours,
        )
        self.logger.info(
            "SCHEDULE step=anime.drop media_id=%s local_files=%s delete_after_hours=%s",
            media_id, queued, self.config.agent.delete_after_watched_hours,
        )
        return {"ok": True, "queued": queued, "state": self.get_state()}

    def move_planned_to_watching(self, media_id: int) -> dict[str, Any]:
        client = self._anilist_client()
        try:
            client.set_list_status(int(media_id), "CURRENT")
        finally:
            client.close()
        self._set_local_anilist_status(int(media_id), "CURRENT")
        release = None
        if self.config.nyaa.enabled and self._downloads_enabled():
            release = self.manager.download_planned(int(media_id))
        return {
            "ok": True,
            "download_started": release is not None,
            "release": release.title if release is not None else "",
            "state": self.get_state(),
        }

    def add_to_planning(self, media_id: int) -> dict[str, Any]:
        client = self._anilist_client()
        try:
            client.set_list_status(int(media_id), "PLANNING")
        finally:
            client.close()
        self._set_local_anilist_status(int(media_id), "PLANNING")
        stats, warning = self._refresh_anilist_after_mutation()
        return {
            "ok": True,
            "refresh_pending": bool(warning),
            "warning": warning,
            "stats": stats,
            "state": self.get_state(),
        }

    def planning_search_anilist(self, query: str) -> list[dict[str, Any]]:
        """Search AniList beyond the user's existing Planning collection."""

        cleaned = re.sub(r"\s+", " ", str(query or "")).strip()[:120]
        if (
            len(cleaned) < 3
            or not self.config.anilist.enabled
            or not self.config.anilist.access_token
        ):
            return []
        cache_key = {
            "query": cleaned.casefold(),
            # The token itself is only hashed into the cache filename by
            # MetadataCache; it is never written to the cache payload.
            "account": self.config.anilist.access_token,
        }
        cached = self._planning_search_cache.get(cache_key, ttl_seconds=24 * 3600)
        if isinstance(cached, list):
            rows = [dict(item) for item in cached if isinstance(item, dict)]
            for item in rows:
                item["description"] = _plain_anilist_description(item.get("description"))
            return rows
        gql = """
        query($search:String!){
          anime:Page(page:1,perPage:8){media(search:$search,type:ANIME,sort:SEARCH_MATCH){
            id format status meanScore seasonYear episodes duration genres description(asHtml:false)
            title{userPreferred romaji english native} coverImage{large} siteUrl
            studios(isMain:true){nodes{name}}
            mediaListEntry{status score(format:POINT_10)}
          }}
          literature:Page(page:1,perPage:8){media(search:$search,type:MANGA,sort:SEARCH_MATCH){
            id format status meanScore seasonYear chapters volumes genres description(asHtml:false)
            title{userPreferred romaji english native} coverImage{large} siteUrl
            mediaListEntry{status score(format:POINT_10)}
          }}
        }
        """
        data = self.light_novels._anilist_post(gql, {"search": cleaned})
        rows: list[dict[str, Any]] = []
        for bucket in ("anime", "literature"):
            for media in (data.get(bucket) or {}).get("media") or []:
                titles = media.get("title") or {}
                entry = media.get("mediaListEntry") or {}
                media_format = str(media.get("format") or "").upper()
                kind = (
                    "anime"
                    if bucket == "anime"
                    else "novel"
                    if media_format == "NOVEL"
                    else "manga"
                )
                rows.append(
                    {
                        "media_id": int(media["id"]),
                        "media_kind": kind,
                        "format": media_format,
                        "title": titles.get("userPreferred")
                        or titles.get("romaji")
                        or titles.get("native")
                        or "",
                        "native_title": titles.get("native") or "",
                        "description": _plain_anilist_description(media.get("description")),
                        "year": media.get("seasonYear"),
                        "episodes": media.get("episodes"),
                        "duration": media.get("duration"),
                        "chapters": media.get("chapters"),
                        "volumes": media.get("volumes"),
                        "genres": list(media.get("genres") or [])[:4],
                        "studio": next(
                            (
                                str(node.get("name") or "")
                                for node in ((media.get("studios") or {}).get("nodes") or [])
                                if node.get("name")
                            ),
                            "",
                        ),
                        "cover": (media.get("coverImage") or {}).get("large") or "",
                        "site_url": media.get("siteUrl") or "",
                        "media_status": media.get("status") or "",
                        "list_status": entry.get("status") or "",
                        "mean_score": media.get("meanScore"),
                        "user_score": entry.get("score"),
                    }
                )
        rows.sort(
            key=lambda item: (
                bool(item.get("list_status")),
                -(float(item.get("mean_score") or 0)),
            )
        )
        rows = rows[:12]
        self._planning_search_cache.put(cache_key, rows)
        self._planning_search_cache.prune(older_than_seconds=30 * 24 * 3600, max_entries=300)
        return rows

    def planning_jiten_stats(
        self,
        media_id: int,
        media_kind: str,
        media_format: str,
        titles: list[str] | None = None,
    ) -> dict[str, Any]:
        return self.light_novels.jiten_media_stats(
            int(media_id),
            str(media_kind or "anime"),
            str(media_format or ""),
            [str(value) for value in (titles or []) if str(value or "").strip()],
        )

    def add_media_to_planning(self, media_id: int, media_kind: str = "anime") -> dict[str, Any]:
        if str(media_kind or "anime").lower() == "anime":
            return self.add_to_planning(int(media_id))
        mutation = """
        mutation($mediaId:Int!){SaveMediaListEntry(mediaId:$mediaId,status:PLANNING){status}}
        """
        entry = self.light_novels._anilist_post(mutation, {"mediaId": int(media_id)}).get(
            "SaveMediaListEntry"
        ) or {}
        return {
            "ok": True,
            "entry": entry,
            "light_novel_state": self.light_novels.refresh_state(),
        }

    def remove_from_planning(self, media_id: int) -> dict[str, Any]:
        client = self._anilist_client()
        try:
            client.delete_list_entry(int(media_id))
        finally:
            client.close()
        self._set_local_anilist_status(int(media_id), "REMOVED")
        stats, warning = self._refresh_anilist_after_mutation()
        return {
            "ok": True,
            "refresh_pending": bool(warning),
            "warning": warning,
            "stats": stats,
            "state": self.get_state(),
        }

    def test_nyaa_proxy(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        values = values or {}
        cfg = self.config.nyaa
        client = NyaaClient(
            str(values.get("nyaa_url", cfg.base_url)).strip(),
            proxy_mode=str(values.get("proxy_mode", cfg.proxy_mode)).strip(),
            proxy_url=str(values.get("proxy_url", cfg.proxy_url)).strip(),
            pre_search_command=str(values.get("search_hook", cfg.pre_search_command)).strip(),
            category=cfg.category,
            timeout=20.0,
        )
        releases = client.search("anime")
        return {
            "ok": True,
            "count": len(releases),
            "proxy_url": client.proxy_url,
            "proxy_mode": client.proxy_mode,
        }

    def test_qbittorrent(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        values = values or {}
        cfg = self.config.qbittorrent
        client = QBittorrentClient(
            str(values.get("qbt_url", cfg.base_url)).strip(),
            str(values.get("qbt_user", cfg.username)).strip(),
            str(values.get("qbt_password", cfg.password)),
            str(values.get("qbt_api_key", cfg.api_key)).strip(),
            verify_tls=cfg.verify_tls,
            pre_download_command=str(values.get("download_hook", cfg.pre_download_command)).strip(),
            auto_start_app=cfg.auto_start_app,
        )
        try:
            version = client.version()
            return {"ok": True, "version": version, "url": client.base_url}
        finally:
            client.close()

    def test_aria2(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        values = values or {}
        cfg = self.config.aria2
        client = Aria2Client(
            enabled=True,
            binary=str(values.get("aria2_binary", cfg.binary)).strip() or "aria2c",
            rpc_port=max(1024, min(65535, int(values.get("aria2_rpc_port", cfg.rpc_port)))),
            pre_download_command=str(values.get("download_hook", self.config.qbittorrent.pre_download_command)).strip(),
            auto_start=True,
            seed_mode=str(values.get("aria2_seed_mode", cfg.seed_mode)),
            seed_ratio=float(values.get("aria2_seed_ratio", cfg.seed_ratio)),
            seed_time_minutes=float(values.get("aria2_seed_time_minutes", cfg.seed_time_minutes)),
            upload_limit_kib=int(values.get("aria2_upload_limit_kib", cfg.upload_limit_kib)),
            vpn_interface=str(values.get("aria2_vpn_interface", cfg.vpn_interface)).strip(),
            vpn_kill_switch=bool(values.get("aria2_vpn_kill_switch", cfg.vpn_kill_switch)),
        )
        try:
            version = client.version()
            return {"ok": True, "version": version, "url": client.base_url, "backend": "aria2", "network_guard": client.network_guard_status()}
        finally:
            client.close()

    @staticmethod
    def _play_key(video_path: str) -> str:
        return str(Path(video_path).expanduser().resolve())

    def _play_state_locked(self, key: str) -> dict[str, Any]:
        process = self._play_processes.get(key)
        if process is not None:
            code = process.poll()
            if code is None:
                age = max(0.0, time.time() - self._play_started_at.get(key, time.time()))
                return {
                    "status": "starting" if age < 6.0 else "running",
                    "pid": process.pid,
                    "age": age,
                }
            self._play_processes.pop(key, None)
            self._play_started_at.pop(key, None)
            self._play_exit_codes[key] = (int(code), time.time())

        last = self._play_exit_codes.get(key)
        if last is not None:
            code, finished_at = last
            if time.time() - finished_at < 8.0 and code != 0:
                return {"status": "failed", "exit_code": code}
            self._play_exit_codes.pop(key, None)
        return {"status": "idle"}

    def play(
        self,
        video_path: str,
        resume: bool = False,
        allow_image_subtitles: bool = False,
    ) -> dict[str, Any]:
        key = self._play_key(video_path)
        path = Path(key)
        if not path.is_file():
            raise FileNotFoundError(f"Видео не найдено: {path}")

        with self._play_lock:
            state = self._play_state_locked(key)
            if state["status"] in {"starting", "running"}:
                return {"ok": True, "duplicate": True, **state}
            try:
                episode = self.manager.db.episode_by_path(path)
                command = [
                    python_executable(),
                    "-m",
                    "pudge.cli",
                    "--fast-play",
                    "--no-sync",
                    "--fullscreen",
                ]
                if episode is not None:
                    if resume and episode.playback_position is not None:
                        start_at = max(
                            0.0,
                            float(episode.playback_position) - self.config.playback.rewind_seconds,
                        )
                        command.extend(["--start-at", f"{start_at:.3f}"])
                    if (
                        episode.state in {"ready", "watched"}
                        and episode.subtitle_path is not None
                        and episode.subtitle_path.is_file()
                    ):
                        command.extend(["--sub", str(episode.subtitle_path)])
                    elif (
                        allow_image_subtitles
                        and episode.state == "waiting_text_subtitles"
                        and episode.subtitle_path is not None
                        and episode.subtitle_path.is_file()
                    ):
                        command.extend(["--sub", str(episode.subtitle_path)])
                    elif (
                        episode.embedded_subtitle_id is not None
                        and (
                            episode.state in {"ready", "watched"}
                            or (
                                allow_image_subtitles
                                and episode.state == "waiting_text_subtitles"
                            )
                        )
                    ):
                        command.extend(
                            ["--embedded-sid", str(episode.embedded_subtitle_id)]
                        )
                    if episode.episode is not None:
                        command.extend(["--episode-hint", str(episode.episode)])
                    if episode.media_id is not None:
                        anime = self.manager.db.get_anime(episode.media_id)
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
                                command.extend(
                                    ["--media-episodes", str(anime.episodes)]
                                )
                            if anime.format:
                                command.extend(["--media-format", anime.format])
                command.append(key)
                self.logger.info(
                    "EVENT play.fast video=%s prepared_subtitle=%s embedded_sid=%s media_id=%s episode=%s allow_image_subtitles=%s",
                    path.name,
                    bool(
                        episode
                        and episode.state in {"ready", "watched"}
                        and episode.subtitle_path
                        and episode.subtitle_path.is_file()
                    ),
                    episode.embedded_subtitle_id if episode else None,
                    episode.media_id if episode else None,
                    episode.episode if episode else None,
                    bool(allow_image_subtitles),
                )
                process = subprocess.Popen(
                    command,
                    start_new_session=True,
                )
            except OSError as exc:
                raise RuntimeError(f"Не удалось запустить {APP_NAME}: {exc}") from exc
            self._play_processes[key] = process
            self._play_started_at[key] = time.time()
            self._play_exit_codes.pop(key, None)
            return {"ok": True, "duplicate": False, "status": "starting", "pid": process.pid}

    def play_status(self, video_path: str) -> dict[str, Any]:
        key = self._play_key(video_path)
        with self._play_lock:
            result = {"ok": True, **self._play_state_locked(key)}
        episode = self.manager.db.episode_by_path(Path(key))
        if episode is not None:
            result.update(
                {
                    "episode_state": episode.state,
                    "watched": episode.state == "watched",
                    "episode": episode.media_episode,
                    "media_episode": episode.media_episode,
                    "release_episode": episode.release_episode,
                    "media_id": episode.media_id,
                }
            )
            if episode.media_id is not None:
                anime = self.manager.db.get_anime(episode.media_id)
                if anime is not None:
                    result["anime_progress"] = anime.progress
                    result["list_status"] = anime.status
                    result["title"] = anime.title
                    result["site_url"] = anime.site_url
                    result["user_score"] = anime.user_score
                    numbered_final = bool(
                        episode.media_episode is not None
                        and anime.episodes is not None
                        and int(episode.media_episode) == int(anime.episodes)
                        and anime.progress >= anime.episodes
                    )
                    single_entry_final = bool(
                        episode.episode is None
                        and anime.status in {"COMPLETED", "REPEATING"}
                        and (
                            anime.episodes == 1
                            or str(anime.format or "").strip().upper() == "MOVIE"
                        )
                    )
                    result["final_episode"] = numbered_final or single_entry_final
                    rating_episode = (
                        int(anime.episodes)
                        if numbered_final and anime.episodes is not None
                        else (1 if single_entry_final else None)
                    )
                    result["rating_episode"] = rating_episode
                    result["rating_prompted"] = bool(
                        rating_episode is not None
                        and self.manager.db.rating_prompted(anime.media_id, rating_episode)
                    )
        return result

    def open_library_folder(self) -> dict[str, Any]:
        self.config.library.root_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(self.config.library.root_dir)])
        return {"ok": True}

    def reveal_subtitle_file(self, path: str) -> dict[str, Any]:
        """Open Finder and select a text/image subtitle or its video container."""
        source_path = Path(str(path)).expanduser()
        if not source_path.is_file():
            return {"ok": False, "error": "Subtitle source does not exist"}
        allowed = {
            ".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".sup", ".pgs",
            ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm",
        }
        if source_path.suffix.casefold() not in allowed:
            return {"ok": False, "error": "Unsupported subtitle source"}
        subprocess.Popen(["open", "-R", str(source_path.resolve())])
        return {"ok": True, "path": str(source_path.resolve())}

    def get_recent_logs(self, limit: int = 300) -> dict[str, Any]:
        return {
            "path": str(DEFAULT_LOG_PATH),
            "lines": tail_log(limit=max(20, min(int(limit), 1000))),
        }

    def open_log_folder(self) -> dict[str, Any]:
        DEFAULT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(DEFAULT_LOG_PATH.parent)])
        return {"ok": True, "path": str(DEFAULT_LOG_PATH)}

    def open_energy_log(self) -> dict[str, Any]:
        ENERGY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            if ENERGY_LOG_PATH.exists():
                subprocess.Popen(["open", "-R", str(ENERGY_LOG_PATH)])
            else:
                subprocess.Popen(["open", str(ENERGY_LOG_PATH.parent)])
        return {"ok": True, "path": str(ENERGY_LOG_PATH), "exists": ENERGY_LOG_PATH.exists()}

    def _clear_fullscreen_exit_pending(self) -> None:
        with self._fullscreen_exit_lock:
            self._fullscreen_exit_pending = False

    def _exit_fullscreen_on_main_thread(self) -> None:
        """Perform the AppKit fullscreen transition only on Cocoa's main thread."""
        try:
            from AppKit import NSApplication, NSWindowStyleMaskFullScreen

            application = NSApplication.sharedApplication()
            native_window = application.keyWindow() or application.mainWindow()
            if native_window is not None and int(native_window.styleMask()) & int(NSWindowStyleMaskFullScreen):
                native_window.toggleFullScreen_(None)
        except Exception:
            self.logger.exception("FAIL step=app.exit_fullscreen.main_thread")
        finally:
            # AppKit's fullscreen transition is asynchronous. Keep the guard for
            # a moment so repeated Escape presses cannot start a second transition.
            timer = threading.Timer(1.5, self._clear_fullscreen_exit_pending)
            timer.daemon = True
            timer.start()

    def _toggle_fullscreen_on_main_thread(self) -> None:
        try:
            from AppKit import NSApplication

            application = NSApplication.sharedApplication()
            native_window = application.keyWindow() or application.mainWindow()
            if native_window is not None:
                native_window.toggleFullScreen_(None)
        except Exception:
            self.logger.exception("FAIL step=app.toggle_fullscreen.main_thread")
        finally:
            timer = threading.Timer(1.5, self._clear_fullscreen_exit_pending)
            timer.daemon = True
            timer.start()

    def toggle_fullscreen(self) -> dict[str, Any]:
        if sys.platform != "darwin":
            return {"ok": True, "scheduled": False}
        with self._fullscreen_exit_lock:
            if self._fullscreen_exit_pending:
                return {"ok": True, "scheduled": False, "pending": True}
            self._fullscreen_exit_pending = True
        try:
            if threading.current_thread() is threading.main_thread():
                self._toggle_fullscreen_on_main_thread()
            else:
                from PyObjCTools import AppHelper

                AppHelper.callAfter(self._toggle_fullscreen_on_main_thread)
            return {"ok": True, "scheduled": True}
        except Exception:
            self._clear_fullscreen_exit_pending()
            self.logger.exception("FAIL step=app.toggle_fullscreen.schedule")
            return {"ok": False, "scheduled": False}

    def exit_fullscreen(self) -> dict[str, Any]:
        """Schedule fullscreen exit on Cocoa's main thread.

        pywebview exposes JS API methods from worker threads. Calling AppKit's
        toggleFullScreen_ there crashes macOS with "Must only be used from the
        main thread", so dispatch the complete Cocoa operation via AppHelper.
        """
        if sys.platform != "darwin":
            return {"ok": True, "exited": False}
        with self._fullscreen_exit_lock:
            if self._fullscreen_exit_pending:
                return {"ok": True, "exited": False, "pending": True}
            self._fullscreen_exit_pending = True
        try:
            if threading.current_thread() is threading.main_thread():
                self._exit_fullscreen_on_main_thread()
            else:
                from PyObjCTools import AppHelper

                AppHelper.callAfter(self._exit_fullscreen_on_main_thread)
            return {"ok": True, "exited": True, "scheduled": True}
        except Exception:
            self._clear_fullscreen_exit_pending()
            self.logger.exception("FAIL step=app.exit_fullscreen.schedule")
            return {"ok": False, "exited": False}


    def test_notification(self) -> dict[str, Any]:
        permission = request_notification_permission(timeout=4.0)
        language = self.config.ui.language
        subtitle = "Проверка уведомлений" if language == "ru" else "Notification test"
        message = (
            f"Уведомления {APP_NAME} работают."
            if language == "ru"
            else f"{APP_NAME} notifications are working."
        )
        delivered = bool(permission.get("granted")) and send_native_notification(subtitle, message)
        self.logger.info(
            "EVENT notification.test granted=%s delivered=%s",
            permission.get("granted"), delivered,
        )
        return {"ok": delivered, "delivered": delivered, "permission": permission}

    def request_permissions(self) -> dict[str, Any]:
        """Probe configured folders on every launch; request notifications once."""
        folder_paths = [self.config.library.root_dir, *self.config.paths.download_dirs, *self.config.paths.subtitle_dirs]
        folders = request_folder_access(folder_paths)
        if self.config.ui.permissions_requested:
            notifications: dict[str, object] = {
                "supported": True,
                "granted": True,
                "error": "",
                "skipped": True,
            }
        else:
            notifications = request_notification_permission()
            self.config.ui.permissions_requested = True
            write_config(self.config, self.config_path)
        self.logger.info(
            "EVENT permissions.requested folders=%s subtitle_dirs=%s "
            "notification_supported=%s notification_granted=%s notification_skipped=%s",
            folders,
            [str(path) for path in self.config.paths.subtitle_dirs],
            notifications.get("supported"),
            notifications.get("granted"),
            notifications.get("skipped", False),
        )
        return {
            "ok": True,
            "folders": folders,
            "notifications": notifications,
            "settings": self._settings_payload(),
        }

    def cleanup_duplicate_torrents(self) -> dict[str, Any]:
        removed = self.manager.cleanup_duplicate_torrents()
        return {"ok": True, "removed": removed, "state": self.get_state()}

    def log_ui_event(self, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        safe_event = str(event or "unknown")[:120]
        safe_payload = dict(payload or {})
        self.logger.info("EVENT ui.%s payload=%s", safe_event, safe_payload)
        return {"ok": True}

    def get_relation_graph_cache(self) -> dict[str, Any]:
        media_ids = [
            int(item.media_id)
            for item in self.manager.db.anime_list(("CURRENT", "PLANNING"))
        ]
        return self.manager.cached_relation_graphs(media_ids)

    def get_relation_graph(self, media_id: int) -> dict[str, Any]:
        return self.manager.relation_graph(int(media_id), force_refresh=False)

    def refresh_relation_graph(self, media_id: int) -> dict[str, Any]:
        return self.manager.relation_graph(int(media_id), force_refresh=True)

    def open_url(self, url: str) -> dict[str, Any]:
        webbrowser.open(str(url))
        return {"ok": True}

    def app_update_status(self, force: bool = True) -> dict[str, Any]:
        return self.app_updater.check(force=bool(force))

    def app_update_install(self) -> dict[str, Any]:
        return self.app_updater.start()

    def app_update_progress(self) -> dict[str, Any]:
        return self.app_updater.state()

    def reveal_app_update_log(self) -> dict[str, Any]:
        path = self.app_updater.log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform == "darwin":
            command = ["open", "-R", str(path)] if path.exists() else ["open", str(path.parent)]
            subprocess.Popen(command)
        return {"ok": True, "path": str(path), "exists": path.exists()}

    def diagnose_episode(self, media_id: int, episode: int | None = None) -> dict[str, Any]:
        return self.manager.diagnose_episode(int(media_id), episode)

    def anime_debug_snapshot(self, media_id: int, episode: int | None = None) -> dict[str, Any]:
        return DebugSnapshotService(self.manager, cache_dir=self.config.paths.cache_dir, runtime_log_path=DEFAULT_LOG_PATH).snapshot(
            int(media_id), None if episode is None else int(episode)
        )

    def debug_reselect_subtitles(self, video_path: str) -> dict[str, Any]:
        video = Path(str(video_path)).expanduser().resolve()
        result = self.manager.force_fresh_subtitle_selection(video)
        def worker() -> None:
            try:
                with maintenance_lock(self.config.paths.cache_dir, blocking=True) as acquired:
                    if acquired:
                        self.manager.process_subtitle_jobs(limit=1, preferred_paths=[video])
            except Exception as exc:
                self.logger.exception("FAIL step=subtitle.debug_fresh_background video=%r error=%r", str(video), str(exc))
        threading.Thread(target=worker, name=f"{APP_SLUG}-debug-fresh-subtitles", daemon=True).start()
        return result

    def export_anime_debug_snapshot(self, media_id: int, episode: int | None = None) -> dict[str, Any]:
        payload = self.anime_debug_snapshot(media_id, episode)
        downloads = Path.home() / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        episode_part = (
            f"-ep{int(payload.get('selected_episode'))}"
            if payload.get("selected_episode") is not None
            else ""
        )
        target = downloads / f"pudge-debug-{int(media_id)}{episode_part}-{int(time.time())}.json"
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        subprocess.run(["open", "-R", str(target)], check=False)
        return {"ok": True, "path": str(target)}

    def retry_episode_preparation(self, video_path: str) -> dict[str, Any]:
        video = Path(str(video_path)).expanduser()
        item = self.manager.db.episode_by_path(video)
        if item is None:
            raise RuntimeError("Episode is not present in the local library")
        self.manager.db.clear_subtitle_selection(video)
        self.manager.db.queue_subtitle_job(
            video, item.media_id, item.episode, priority=220, error="Manual retry"
        )
        prepared = 0
        with maintenance_lock(self.config.paths.cache_dir, blocking=False) as acquired:
            if acquired:
                prepared = self.manager.process_subtitle_jobs(limit=1, preferred_paths=[video])
        return {"ok": True, "prepared": prepared, "state": self.get_state()}

    def cancel_subtitle_job(self, video_path: str) -> dict[str, Any]:
        video = Path(str(video_path)).expanduser()
        self.manager.db.delete_subtitle_job(video)
        self.logger.info("EVENT subtitle_job.cancelled video=%r", str(video))
        return {"ok": True, "state": self.get_state()}

    def choose_manual_subtitle(self, video_path: str) -> dict[str, Any]:
        if self.window is None:
            return {"ok": False, "cancelled": True}
        video = Path(str(video_path)).expanduser()
        try:
            import webview
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                directory=str(Path.home() / "Downloads"),
                allow_multiple=False,
                file_types=(
                    "Subtitles (*.srt;*.ass;*.ssa;*.vtt;*.sup;*.pgs)",
                    "All files (*.*)",
                ),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = Path(str(result[0] if isinstance(result, (list, tuple)) else result))
            target = self.manager.set_manual_subtitle(video, selected)
            prepared = 0
            with maintenance_lock(self.config.paths.cache_dir, blocking=False) as acquired:
                if acquired:
                    prepared = self.manager.process_subtitle_jobs(limit=1, preferred_paths=[video])
            return {"ok": True, "path": str(target), "prepared": prepared, "state": self.get_state()}
        except Exception as exc:
            self.logger.warning("FAIL step=subtitle.manual_choose video=%r error=%r", str(video), str(exc))
            return {"ok": False, "cancelled": False, "error": str(exc)}

    def repair_library(self) -> dict[str, Any]:
        with maintenance_lock(self.config.paths.cache_dir, blocking=False) as acquired:
            if not acquired:
                return {"ok": False, "busy": True, "result": {}, "state": self.get_state()}
            result = self.manager.repair_library_integrity(automatic=False, scan=True)
        return {"ok": True, "busy": False, "result": result, "state": self.get_state()}

    def _playlist_payloads(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for queue in self.manager.db.playlists():
            payload = dict(queue)
            items = []
            for item in queue.get("items", []):
                row = dict(item)
                row["available"] = Path(str(row.get("video_path") or "")).is_file()
                items.append(row)
            payload["items"] = items
            payload["remaining"] = sum(
                1 for item in items if item.get("state") == "pending" and item.get("available")
            )
            result.append(payload)
        return result

    def _ready_queue_items(self, media_ids: list[int], *, limit: int | None = None) -> list[dict[str, object]]:
        anime_by_id = {anime.media_id: anime for anime in self.manager.db.anime_list()}
        items: list[dict[str, object]] = []
        seen_paths: set[str] = set()
        for media_id in media_ids:
            anime = anime_by_id.get(int(media_id))
            episodes = sorted(
                self.manager.db.episodes(int(media_id)),
                key=lambda episode: (
                    episode.episode is None,
                    int(episode.episode or 0),
                    str(episode.video_path),
                ),
            )
            for episode in episodes:
                if episode.state == "watched" or episode.state != "ready":
                    continue
                if not episode.video_path.is_file():
                    continue
                path = str(episode.video_path)
                if path in seen_paths:
                    continue
                seen_paths.add(path)
                ep_label = "Movie" if episode.episode is None else f"Episode {episode.episode}"
                items.append({
                    "media_id": episode.media_id,
                    "episode": episode.episode,
                    "video_path": path,
                    "title": f"{anime.title if anime else episode.title} — {ep_label}",
                })
                if limit is not None and len(items) >= max(1, int(limit)):
                    return items
        return items

    def create_next_episodes_queue(self, media_id: int, count: int = 5) -> dict[str, Any]:
        anime = self.manager.db.get_anime(int(media_id))
        if anime is None:
            return {"ok": False, "error": "Anime not found", "state": self.get_state_fast()}
        if anime.media_status == "NOT_YET_RELEASED":
            return {"ok": False, "error": "Anime has not aired yet", "state": self.get_state_fast()}
        items = self._ready_queue_items([anime.media_id], limit=max(1, min(50, int(count))))
        if not items:
            return {"ok": False, "error": "No ready unwatched episodes", "state": self.get_state_fast()}
        queue_id = self.manager.db.create_playlist(
            name=f"{anime.title} — next {len(items)}",
            kind="next_episodes",
            media_id=anime.media_id,
            items=items,
        )
        return {"ok": True, "playlist_id": queue_id, "state": self.get_state_fast()}

    def create_franchise_queue(self, media_id: int) -> dict[str, Any]:
        anime = self.manager.db.get_anime(int(media_id))
        if anime is None:
            return {"ok": False, "error": "Anime not found", "state": self.get_state_fast()}
        if anime.media_status == "NOT_YET_RELEASED":
            return {"ok": False, "error": "Anime has not aired yet", "state": self.get_state_fast()}
        graph = self.manager.relation_graph(int(media_id), force_refresh=False)
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        ordered_ids: list[int] = []
        for node in sorted(
            [node for node in nodes if isinstance(node, dict)],
            key=lambda node: (
                str(node.get("start_date") or f"{int(node.get('season_year') or 9999):04d}"),
                int(node.get("media_id") or 0),
            ),
        ):
            try:
                value = int(node.get("media_id"))
            except (TypeError, ValueError):
                continue
            if value not in ordered_ids:
                ordered_ids.append(value)
        if anime.media_id not in ordered_ids:
            ordered_ids.append(anime.media_id)
        items = self._ready_queue_items(ordered_ids)
        if not items:
            return {"ok": False, "error": "No ready unwatched franchise episodes", "state": self.get_state_fast()}
        queue_id = self.manager.db.create_playlist(
            name=f"{anime.title} — watch order",
            kind="franchise",
            media_id=anime.media_id,
            items=items,
        )
        return {"ok": True, "playlist_id": queue_id, "state": self.get_state_fast()}

    def advance_playlist(self, playlist_id: int, item_id: int) -> dict[str, Any]:
        self.manager.db.mark_playlist_item(int(item_id), "completed")
        queue = self.manager.db.playlist(int(playlist_id))
        next_item = None
        if queue:
            for item in queue.get("items", []):
                if item.get("state") == "pending" and Path(str(item.get("video_path") or "")).is_file():
                    next_item = dict(item)
                    break
        return {"ok": True, "next": next_item, "state": self.get_state_fast()}

    def skip_playlist_item(self, item_id: int) -> dict[str, Any]:
        self.manager.db.mark_playlist_item(int(item_id), "skipped")
        return {"ok": True, "state": self.get_state_fast()}

    def delete_playlist(self, playlist_id: int) -> dict[str, Any]:
        self.manager.db.delete_playlist(int(playlist_id))
        return {"ok": True, "state": self.get_state_fast()}

    def check_release_upgrades(self) -> dict[str, Any]:
        with maintenance_lock(self.config.paths.cache_dir, blocking=False) as acquired:
            if not acquired:
                return {"ok": False, "busy": True, "state": self.get_state_fast()}
            scheduled = self.manager.auto_upgrade_downloaded(force=True, limit=20)
        return {"ok": True, "scheduled": scheduled, "state": self.get_state_fast()}

    def check_subtitle_upgrades(self) -> dict[str, Any]:
        with maintenance_lock(self.config.paths.cache_dir, blocking=False) as acquired:
            if not acquired:
                return {"ok": False, "busy": True, "state": self.get_state_fast()}
            scheduled = self.manager.schedule_subtitle_upgrades(force=True, limit=20)
        return {"ok": True, "scheduled": scheduled, "state": self.get_state_fast()}

    def create_full_backup(self) -> dict[str, Any]:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        output = Path.home() / "Downloads" / f"{APP_SLUG}-backup-{stamp}.zip"
        result = create_backup(
            config_path=self.config_path,
            database_path=self.config.library.database_path,
            cache_dir=self.config.paths.cache_dir,
            output=output,
            version=__version__,
        )
        self.logger.info("DONE step=backup.create path=%r cached_files=%s", result["path"], result["cached_files"])
        return {"ok": True, **result}

    def restore_full_backup(self) -> dict[str, Any]:
        if self.window is None:
            return {"ok": False, "cancelled": True}
        try:
            import webview
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                directory=str(Path.home() / "Downloads"),
                allow_multiple=False,
                file_types=(f"{APP_NAME} backup (*.zip)",),
            )
            if not result:
                return {"ok": False, "cancelled": True}
            selected = Path(str(result[0] if isinstance(result, (list, tuple)) else result))
            restored = restore_backup(
                archive_path=selected,
                config_path=self.config_path,
                database_path=self.config.library.database_path,
                cache_dir=self.config.paths.cache_dir,
            )
            self.audiobooks.stop_all()
            self.config = load_config(self.config_path)
            self.manager = AnimeManager(self.config, log=self.logger.info)
            self.light_novels = LightNovelService(self.config, logger=self.logger)
            self.manga = MangaService(
            self.manager.db,
            cache_dir=self.config.paths.cache_dir,
            python=python_executable(),
        )
            self.audiobooks = AudiobookService(
                self.manager.db,
                ffprobe=self.config.tools.ffprobe,
                mpv=self.config.tools.mpv,
                cache_dir=self.config.paths.cache_dir,
                ffmpeg=self.config.tools.ffmpeg,
                python=python_executable(),
                stt_model=self.config.sync.japanese_stt_model,
                work_scheduler=self.manager.work_scheduler,

            )
            threading.Thread(
                target=self.audiobooks.resume_pending_transcriptions,
                name="audiobook-stt-resume",
                daemon=True,
            ).start()
            self._planning_search_cache = MetadataCache(
                self.config.paths.cache_dir,
                "anilist-planning-search",
                schema="v2",
            )
            self.debug_snapshots = DebugSnapshotService(
                self.manager,
                cache_dir=self.config.paths.cache_dir,
                runtime_log_path=DEFAULT_LOG_PATH,
            )
            self.logger.info("DONE step=backup.restore path=%r", str(selected))
            return {"ok": True, **restored, "state": self.get_state()}
        except Exception as exc:
            self.logger.exception("FAIL step=backup.restore")
            return {"ok": False, "cancelled": False, "error": str(exc)}

    def choose_library_folder(self) -> str:
        if self.window is None:
            return ""
        try:
            import webview

            result = self.window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=str(self.config.library.root_dir),
                allow_multiple=False,
            )
            if not result:
                return ""
            return str(result[0] if isinstance(result, (list, tuple)) else result)
        except Exception:
            return ""

    def choose_watch_folder(self) -> dict[str, Any]:
        if self.window is None:
            return {"ok": False, "path": "", "access": False}
        try:
            import webview
            initial = (
                self.config.paths.download_dirs[0]
                if self.config.paths.download_dirs
                else Path.home() / "Downloads"
            )
            result = self.window.create_file_dialog(
                webview.FOLDER_DIALOG, directory=str(initial), allow_multiple=False
            )
            if not result:
                return {"ok": False, "path": "", "access": False}
            selected = Path(str(result[0] if isinstance(result, (list, tuple)) else result)).expanduser()
            access = request_folder_access([selected]).get(str(selected), False)
            return {"ok": True, "path": str(selected), "access": access}
        except Exception as exc:
            self.logger.warning("FAIL step=watch_folder.choose error=%s", exc)
            return {"ok": False, "path": "", "access": False, "error": str(exc)}

    def choose_subtitle_folder(self) -> dict[str, Any]:
        if self.window is None:
            return {"ok": False, "path": "", "access": False}
        initial = (
            self.config.paths.subtitle_dirs[0]
            if self.config.paths.subtitle_dirs
            else Path.home() / "Downloads"
        )
        try:
            import webview

            result = self.window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=str(initial),
                allow_multiple=False,
            )
            if not result:
                return {"ok": False, "path": "", "access": False}
            selected = Path(
                str(result[0] if isinstance(result, (list, tuple)) else result)
            ).expanduser()
            access = request_folder_access([selected]).get(str(selected), False)
            self.logger.info(
                "EVENT subtitle_folder.chosen path=%s access=%s", selected, access
            )
            return {"ok": True, "path": str(selected), "access": bool(access)}
        except Exception as exc:
            self.logger.warning("FAIL step=subtitle_folder.choose error=%s", exc)
            return {"ok": False, "path": "", "access": False, "error": str(exc)}

    def search_releases(self, media_id: int, episode: int | None, batch: bool) -> list[dict[str, Any]]:
        releases = self.manager.search_releases(int(media_id), episode=episode, batch=bool(batch))
        return [asdict(item) for item in releases[:80]]

    def add_release(
        self,
        media_id: int,
        episode: int | None,
        batch: bool,
        release: dict[str, Any],
    ) -> dict[str, Any]:
        allowed = set(NyaaRelease.__dataclass_fields__)
        payload = {key: value for key, value in release.items() if key in allowed}
        item = NyaaRelease(**payload)
        added = self.manager.add_release(
            int(media_id), item, episode=episode, batch=bool(batch)
        )
        return {"ok": True, "added": bool(added), "already_downloading": not added}

    def add_best_planning_episode(self, media_id: int, episode: int) -> dict[str, Any]:
        anime = self.manager.db.get_anime(int(media_id))
        if anime is None:
            raise KeyError(f"Unknown AniList id={media_id}")
        finished = str(anime.media_status or "").upper() == "FINISHED"
        available = int(
            (anime.episodes if finished else anime.released_episodes)
            or max(0, int(anime.next_airing_episode or 1) - 1)
            or 0
        )
        episode = int(episode)
        if episode < 1 or (available and episode > available):
            raise ValueError(f"Episode {episode} is not released")
        local = self._local_episode_for_relative(anime, episode)
        if local is not None and local.video_path.is_file():
            return {
                "ok": True,
                "episode": episode,
                "local": True,
                "release": None,
            }
        release = self.manager.search_and_add_best(
            int(media_id),
            episode=episode,
            batch=False,
            # This is a user-requested background job, not the short periodic
            # agent pass. Use the complete alias search so later title aliases
            # such as "Goodbye Lara" are not lost to the automatic time budget.
            automatic=False,
        )
        return {
            "ok": release is not None,
            "episode": episode,
            "local": False,
            "release": asdict(release) if release is not None else None,
        }

    @staticmethod
    def _planning_released_episode_count(anime: LibraryAnime) -> int:
        finished = str(anime.media_status or "").upper() == "FINISHED"
        if finished:
            return max(0, int(anime.episodes or anime.released_episodes or 0))
        before_next = max(0, int(anime.next_airing_episode or 1) - 1)
        return max(0, int(anime.released_episodes or 0), before_next, int(anime.progress or 0))

    def _planning_local_episodes(
        self,
        anime: LibraryAnime,
        total: int,
        *,
        manager: AnimeManager | None = None,
    ) -> list[int]:
        active_manager = manager or self.manager
        local: list[int] = []
        rows = active_manager.db.episodes(anime.media_id)
        for episode in range(1, int(total) + 1):
            match = next(
                (
                    item
                    for item in rows
                    if item.video_path.is_file()
                    and self._display_episode_number(anime, item.episode) == episode
                ),
                None,
            )
            if match is not None:
                local.append(episode)
        return local

    def planning_episode_download_preview(self, media_id: int) -> dict[str, Any]:
        media_id = int(media_id)
        anime = self.manager.db.get_anime(media_id)
        if anime is None:
            raise KeyError(f"Unknown AniList id={media_id}")
        if self._downloads_enabled():
            try:
                self.manager.sync_downloads()
            except Exception as exc:
                self.logger.warning(
                    "FALLBACK step=planning_episode.preview_downloads media_id=%s error=%r",
                    media_id,
                    str(exc),
                )
        self.manager.scan_library()
        total = self._planning_released_episode_count(anime)
        return {
            "media_id": media_id,
            "title": anime.title,
            "total": total,
            "local_episodes": self._planning_local_episodes(anime, total),
        }

    def _planning_episode_download_payload(self) -> dict[str, Any]:
        with self._planning_episode_download_lock:
            payload = dict(self._planning_episode_download_state)
            payload["episodes"] = [
                dict(item) for item in self._planning_episode_download_state.get("episodes", [])
            ]
            thread = self._planning_episode_download_thread
            payload["running"] = bool(thread is not None and thread.is_alive())
        return payload

    def planning_episode_download_status(self) -> dict[str, Any]:
        return self._planning_episode_download_payload()

    def _run_planning_episode_download(
        self,
        manager: AnimeManager,
        anime: LibraryAnime,
        total: int,
    ) -> None:
        media_id = int(anime.media_id)
        results: list[dict[str, Any]] = []
        cancelled = False
        try:
            if manager.downloads_enabled():
                try:
                    manager.sync_downloads()
                except Exception as exc:
                    self.logger.warning(
                        "FALLBACK step=planning_episode.downloads media_id=%s error=%r",
                        media_id,
                        str(exc),
                    )
            manager.scan_library()
            local_episodes = set(
                self._planning_local_episodes(anime, total, manager=manager)
            )
            missing_episodes = [
                episode for episode in range(1, total + 1)
                if episode not in local_episodes
            ]

            # Prefer one real season pack over N independent episode searches.
            # Size alone is deliberately not enough evidence for this path.
            if len(missing_episodes) >= 2:
                cancel_event = getattr(self, "_planning_episode_cancel_event", None)
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                else:
                    try:
                        batch_release = manager.search_and_add_best(
                            media_id,
                            episode=None,
                            batch=True,
                            automatic=False,
                            require_explicit_batch=True,
                        )
                    except Exception as exc:
                        batch_release = None
                        self.logger.warning(
                            "FALLBACK step=planning_episode.batch_first "
                            "media_id=%s error=%r",
                            media_id,
                            str(exc),
                        )
                    if batch_release is not None:
                        results = [
                            {
                                "episode": episode,
                                "status": "local" if episode in local_episodes else "added",
                                "source": "" if episode in local_episodes else "batch",
                                "release": "" if episode in local_episodes else batch_release.title,
                                "error": "",
                            }
                            for episode in range(1, total + 1)
                        ]
                        with self._planning_episode_download_lock:
                            self._planning_episode_download_state["current"] = total
                            self._planning_episode_download_state["episodes"] = [
                                dict(item) for item in results
                            ]
                        job_id = str(getattr(self, "_planning_episode_job_id", "") or "")
                        if job_id and getattr(self, "job_center", None) is not None:
                            self.job_center.update(
                                job_id,
                                state="running",
                                current=total,
                                total=total,
                                message="Series pack added",
                            )
                        status = "done"
                        return

            for episode in range(1, total + 1):
                cancel_event = getattr(self, "_planning_episode_cancel_event", None)
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                with self._planning_episode_download_lock:
                    self._planning_episode_download_state["current"] = episode
                if episode in local_episodes:
                    results.append({"episode": episode, "status": "local", "error": ""})
                else:
                    try:
                        release = manager.search_and_add_best(
                            media_id,
                            episode=episode,
                            batch=False,
                            automatic=False,
                        )
                        results.append(
                            {
                                "episode": episode,
                                "status": "added" if release is not None else "missing",
                                "release": release.title if release is not None else "",
                                "error": "",
                            }
                        )
                    except Exception as exc:
                        self.logger.exception(
                            "FAIL step=planning_episode.download media_id=%s episode=%s",
                            media_id,
                            episode,
                        )
                        results.append(
                            {"episode": episode, "status": "error", "error": str(exc)}
                        )
                with self._planning_episode_download_lock:
                    self._planning_episode_download_state["episodes"] = [
                        dict(item) for item in results
                    ]
                job_id = str(getattr(self, "_planning_episode_job_id", "") or "")
                if job_id and getattr(self, "job_center", None) is not None:
                    self.job_center.update(
                        job_id,
                        state="running",
                        current=episode,
                        total=total,
                        message=f"Episode {episode}/{total}",
                    )
            status = "cancelled" if cancelled else "done"
        except Exception as exc:
            self.logger.exception(
                "FAIL step=planning_episode.batch media_id=%s", media_id
            )
            results.append({"episode": None, "status": "error", "error": str(exc)})
            status = "failed"
        finally:
            with self._planning_episode_download_lock:
                self._planning_episode_download_state.update(
                    {
                        "status": status,
                        "running": False,
                        "current": total,
                        "episodes": [dict(item) for item in results],
                        "finished_at": time.time(),
                    }
                )
            job_id = str(getattr(self, "_planning_episode_job_id", "") or "")
            if job_id and getattr(self, "job_center", None) is not None:
                if status == "done":
                    self.job_center.finish(
                        job_id,
                        message="Episode search complete",
                        result={"media_id": media_id, "episodes": results},
                    )
                elif status == "cancelled":
                    self.job_center.cancelled(job_id)
                else:
                    error = next(
                        (row.get("error") for row in reversed(results) if row.get("error")),
                        "Episode search failed",
                    )
                    self.job_center.fail(job_id, error)

    def start_planning_episode_download(
        self, media_id: int, attempt_of: str = ""
    ) -> dict[str, Any]:
        media_id = int(media_id)
        anime = self.manager.db.get_anime(media_id)
        if anime is None:
            raise KeyError(f"Unknown AniList id={media_id}")
        total = self._planning_released_episode_count(anime)
        if total < 1:
            raise ValueError("No released episodes")
        with self._planning_episode_download_lock:
            running = self._planning_episode_download_thread
            if running is not None and running.is_alive():
                return self._planning_episode_download_payload_unlocked()
            self._planning_episode_download_state = {
                "status": "running",
                "running": True,
                "media_id": media_id,
                "title": anime.title,
                "total": total,
                "current": 0,
                "episodes": [],
                "started_at": time.time(),
                "finished_at": 0.0,
            }
            self._planning_episode_cancel_event = threading.Event()
            self._planning_episode_job_id = self.job_center.start(
                "nyaa",
                f"Nyaa · {anime.title}",
                payload={"media_id": media_id},
                total=total,
                attempt_of=str(attempt_of or ""),
            )
            manager = self.manager
            thread = threading.Thread(
                target=self._run_planning_episode_download,
                args=(manager, anime, total),
                name=f"{APP_SLUG}-planning-episodes-{media_id}",
                daemon=True,
            )
            self._planning_episode_download_thread = thread
            thread.start()
            return self._planning_episode_download_payload_unlocked()

    def _planning_episode_download_payload_unlocked(self) -> dict[str, Any]:
        payload = dict(self._planning_episode_download_state)
        payload["episodes"] = [
            dict(item) for item in self._planning_episode_download_state.get("episodes", [])
        ]
        thread = self._planning_episode_download_thread
        payload["running"] = bool(thread is not None and thread.is_alive())
        return payload


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _start_asset_server(api: WebAppApi) -> tuple[http.server.ThreadingHTTPServer, str]:
    web_root = api.config.paths.cache_dir / "web-ui"
    web_root.mkdir(parents=True, exist_ok=True)
    source_dir = Path(__file__).resolve().parent / "web"
    for source in source_dir.iterdir():
        if not source.is_file():
            continue
        target = web_root / source.name
        if source.name == "index.html":
            target.write_text(
                source.read_text(encoding="utf-8").replace("__APP_NAME__", APP_NAME),
                encoding="utf-8",
            )
        else:
            shutil.copy2(source, target)
    covers_link = web_root / "covers"
    api.config.library.cover_cache_dir.mkdir(parents=True, exist_ok=True)
    if covers_link.is_symlink() or covers_link.exists():
        if covers_link.is_symlink() or covers_link.is_file():
            covers_link.unlink()
        elif covers_link.resolve() != api.config.library.cover_cache_dir.resolve():
            shutil.rmtree(covers_link)
    if not covers_link.exists():
        covers_link.symlink_to(api.config.library.cover_cache_dir, target_is_directory=True)

    handler = lambda *args, **kwargs: _QuietHandler(
        *args, directory=str(web_root), **kwargs
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def _set_macos_runtime_identity() -> bool:
    """Set the real Cocoa process identity used by Dock and Cmd+Tab.

    The launcher starts Homebrew Python, whose default process name is
    ``Python``. NSProcessInfo.processName is writable on macOS, so set it
    before and after pywebview creates NSApplication. The app bundle still
    supplies the icon and display name as a second source of truth.
    """
    if sys.platform != "darwin":
        return False
    icon_path = Path(__file__).resolve().parent / "assets" / "app-icon.png"
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyRegular, NSImage
        from Foundation import NSBundle, NSProcessInfo

        process_info = NSProcessInfo.processInfo()
        process_info.setProcessName_(APP_NAME)

        bundle_info = NSBundle.mainBundle().infoDictionary()
        if bundle_info is not None:
            bundle_info.setObject_forKey_(APP_NAME, "CFBundleName")
            bundle_info.setObject_forKey_(APP_NAME, "CFBundleDisplayName")
            bundle_info.setObject_forKey_(APP_BUNDLE_ID, "CFBundleIdentifier")

        application = NSApplication.sharedApplication()
        application.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        if icon_path.is_file():
            image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
            if image is not None:
                if threading.current_thread() is threading.main_thread():
                    application.setApplicationIconImage_(image)
                else:
                    from PyObjCTools import AppHelper

                    AppHelper.callAfter(application.setApplicationIconImage_, image)
        return True
    except Exception:
        configure_logging().exception("FAIL step=app.runtime_identity")
        return False


def _set_macos_runtime_icon() -> bool:
    # Backwards-compatible private alias used by older tests/installations.
    return _set_macos_runtime_identity()


def _request_notification_permission_after_launch(api: WebAppApi) -> None:
    """Request notification access as soon as the regular app has opened."""
    result = request_notification_permission(timeout=12.0)
    api.logger.info(
        "EVENT notification.permission_startup supported=%s granted=%s error=%r",
        result.get("supported"),
        result.get("granted"),
        result.get("error", ""),
    )


def launch_web_app(config_path: Path) -> int:
    # Set the process name before Cocoa/pywebview initializes. Calling this a
    # second time from on_started handles backends that recreate NSApplication.
    _set_macos_runtime_identity()
    try:
        import webview
    except ImportError as exc:
        raise RuntimeError("pywebview is not installed. Run ./install.sh again") from exc

    api = WebAppApi(config_path)
    _set_macos_runtime_identity()
    with timed_step(api.logger, "app.asset_server"):
        server, base_url = _start_asset_server(api)
    api.asset_base = base_url
    window = webview.create_window(
        APP_NAME,
        url=f"{base_url}/index.html",
        js_api=api,
        width=1280,
        height=820,
        min_size=(980, 660),
        resizable=True,
        text_select=True,
        background_color="#0b1320",
    )
    api.set_window(window)

    def on_started() -> None:
        # pywebview initializes NSApplication during start(). Set the icon again
        # so the actual WebKit process has the same icon in Dock and Cmd+Tab.
        icon_set = _set_macos_runtime_identity()
        api.logger.info("EVENT app.webview_started runtime_identity=%s", icon_set)
        permission_thread = threading.Thread(
            target=_request_notification_permission_after_launch,
            args=(api,),
            name="AnimeMPVNotificationPermission",
            daemon=True,
        )
        permission_thread.start()

    try:
        with timed_step(api.logger, "app.webview_lifetime"):
            runtime_icon = Path(__file__).resolve().parent / "assets" / "app-icon.png"
            webview.start(
                on_started,
                gui="cocoa",
                debug=False,
                icon=str(runtime_icon) if runtime_icon.is_file() else None,
            )
    finally:
        api.close()
        api.logger.info("APP session_stop")
        server.shutdown()
        server.server_close()
    return 0
