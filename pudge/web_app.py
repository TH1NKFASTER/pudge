from __future__ import annotations

import base64
import html
import hashlib
import http.server
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
import zipfile
import httpx
from rapidfuzz import fuzz
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from . import __version__
from .app_session import mark_app_running, mark_app_stopped
from .cache_management import cleanup_segment_audio_cache
from .audiobooks import (
    AUDIOBOOK_EXTENSIONS,
    AudiobookService,
    audiobook_torrent_files_from_payload,
    audiobook_torrent_pack_plan,
    audiobook_series_path_matches,
)
from .backup import create_backup, restore_backup
from .branding import APP_BUNDLE_ID, APP_NAME, APP_SLUG, DATA_DIR
from .cache_registry import CacheRegistry
from .config import load_config, write_config, write_torrents_enabled
from .companion_streaming import CompanionStreamingService
from .debug_snapshot import DebugSnapshotService
from .diagnostics import DebugBundleBuilder, DiagnosticRecorder
from .energy_diagnostics import ENERGY_LOG_PATH, EnergyDiagnosticsMonitor
from .episode_state import watched_by_anilist_progress
from .episode_numbering import resolve_episode_numbering
from .presentation_state import derive_episode_presentation, download_complete
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
from .llm import OllamaClient, list_models
from .library import VIDEO_EXTENSIONS
from .logging_utils import DEFAULT_LOG_PATH, cleanup_debug_logs, configure_logging, debug_log_dir, tail_log, timed_step
from .maintenance_lock import maintenance_lock
from .manager import AnimeManager
from .manager_models import LibraryAnime, NyaaRelease
from .manga import MangaService
from .metadata_cache import MetadataCache
from .mobile_sync import MobileSyncService
from .mobile_sync_http import start_mobile_sync_server
from .notifications import send_native_notification
from .permissions import request_folder_access, request_notification_permission
from .providers.anilist import AniListClient
from .providers.aria2 import Aria2Client
from .providers.nyaa import NyaaClient
from .providers.qbittorrent import QBittorrentClient
from .runtime import python_executable
from .safe_mode import SafeModeController
from .secrets_store import keep_masked_secret, masked_secret
from .subtitle_runtime import repair_episode_subtitle, resolve_episode_subtitle
from .task_supervisor import TaskSupervisor
from .uninstall import build_uninstall_plan, launch_uninstaller
from .updater import AppUpdater
from .visual_novels import VisualNovelService
from .work_scheduler import WorkPriority
from .web_controllers import CompanionController, DiagnosticsController
from .web_state import UIStateSnapshotCache




def _disable_torrents_for_startup(config: Any) -> bool:
    """Make every real GUI session start with explicit torrent intent off."""
    previous = bool(config.nyaa.torrents_enabled)
    config.nyaa.torrents_enabled = False
    return previous

def _plain_anilist_description(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)<br\s*/?>|</p\s*>|</li\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)



# pudge-ui-v0.7.23-global-search-recent-v1
def _global_search_normalize(value: Any) -> str:
    text = unicodedata.normalize(
        "NFKC",
        html.unescape(str(value or "")),
    ).casefold()
    return "".join(char for char in text if char.isalnum())


_KANA_ROMAJI = {
    "きゃ":"kya","きゅ":"kyu","きょ":"kyo","しゃ":"sha","しゅ":"shu","しょ":"sho",
    "ちゃ":"cha","ちゅ":"chu","ちょ":"cho","にゃ":"nya","にゅ":"nyu","にょ":"nyo",
    "ひゃ":"hya","ひゅ":"hyu","ひょ":"hyo","みゃ":"mya","みゅ":"myu","みょ":"myo",
    "りゃ":"rya","りゅ":"ryu","りょ":"ryo","ぎゃ":"gya","ぎゅ":"gyu","ぎょ":"gyo",
    "じゃ":"ja","じゅ":"ju","じょ":"jo","びゃ":"bya","びゅ":"byu","びょ":"byo",
    "ぴゃ":"pya","ぴゅ":"pyu","ぴょ":"pyo","てぃ":"ti","でぃ":"di","ふぁ":"fa",
    "ふぃ":"fi","ふぇ":"fe","ふぉ":"fo","うぃ":"wi","うぇ":"we","うぉ":"wo",
    "あ":"a","い":"i","う":"u","え":"e","お":"o","か":"ka","き":"ki","く":"ku","け":"ke","こ":"ko",
    "さ":"sa","し":"shi","す":"su","せ":"se","そ":"so","た":"ta","ち":"chi","つ":"tsu","て":"te","と":"to",
    "な":"na","に":"ni","ぬ":"nu","ね":"ne","の":"no","は":"ha","ひ":"hi","ふ":"fu","へ":"he","ほ":"ho",
    "ま":"ma","み":"mi","む":"mu","め":"me","も":"mo","や":"ya","ゆ":"yu","よ":"yo",
    "ら":"ra","り":"ri","る":"ru","れ":"re","ろ":"ro","わ":"wa","を":"o","ん":"n",
    "が":"ga","ぎ":"gi","ぐ":"gu","げ":"ge","ご":"go","ざ":"za","じ":"ji","ず":"zu","ぜ":"ze","ぞ":"zo",
    "だ":"da","ぢ":"ji","づ":"zu","で":"de","ど":"do","ば":"ba","び":"bi","ぶ":"bu","べ":"be","ぼ":"bo",
    "ぱ":"pa","ぴ":"pi","ぷ":"pu","ぺ":"pe","ぽ":"po","ゔ":"vu",
}


def _kana_to_romaji(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    # Katakana -> hiragana while leaving kanji/latin untouched.
    text = "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in text)
    out: list[str] = []
    geminate = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "っ":
            geminate = True; i += 1; continue
        pair = text[i:i+2]
        roma = _KANA_ROMAJI.get(pair)
        if roma is not None:
            i += 2
        else:
            roma = _KANA_ROMAJI.get(ch)
            i += 1
        if roma is None:
            if ch == "ー" and out:
                last = out[-1]
                vowel = next((v for v in reversed(last) if v in "aeiou"), "")
                if vowel: out.append(vowel)
            elif ch.isalnum():
                out.append(ch.casefold())
            continue
        if geminate and roma:
            consonant = "t" if roma.startswith("ch") else "s" if roma.startswith("sh") else roma[0]
            if consonant not in "aeioun": out.append(consonant)
        geminate = False
        out.append(roma)
    return "".join(out)


def _global_search_keys(value: Any) -> list[str]:
    native = _global_search_normalize(value)
    romaji = _global_search_normalize(_kana_to_romaji(value))
    return list(dict.fromkeys(key for key in (native, romaji) if key))


def _global_search_score(
    query: str,
    names: list[str],
) -> tuple[float, str]:
    cleaned = re.sub(r"\s+", " ", str(query or "")).strip()
    query_keys = _global_search_keys(cleaned)
    query_key = query_keys[0] if query_keys else ""
    if not query_key:
        return 0.0, ""

    best_score = 0.0
    best_name = ""
    for raw_name in names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        name_keys = _global_search_keys(name)
        if not name_keys:
            continue
        score = 0.0
        for qkey in query_keys:
            for name_key in name_keys:
                if name_key == qkey:
                    candidate = 100.0
                elif name_key.startswith(qkey):
                    candidate = 97.0
                elif qkey in name_key:
                    candidate = 93.0
                else:
                    candidate = float(fuzz.ratio(qkey, name_key))
                score = max(score, candidate)
        score = max(score, float(fuzz.WRatio(cleaned.casefold(), name.casefold())))
        if score > best_score:
            best_score = score
            best_name = name
    return best_score, best_name


class WebAppApi:
    """Local bridge used by the WKWebView application.

    Read methods only access SQLite and the filesystem. AniList is synchronized
    once after the window opens and can also be refreshed explicitly in Settings.
    """

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.expanduser()
        self.config = load_config(self.config_path)
        self.logger = configure_logging()
        startup_was_enabled = _disable_torrents_for_startup(self.config)
        write_torrents_enabled(self.config, self.config.config_path)
        startup_aria2_stopped = self._shutdown_aria2_from_config(reason="startup")
        self.logger.info(
            "EVENT torrent.startup_off previous_enabled=%s aria2_stopped=%s",
            startup_was_enabled,
            startup_aria2_stopped,
        )
        self.safe_mode = SafeModeController(
            self.config.paths.cache_dir,
            self.config.library.database_path,
        )
        self.safe_mode.begin()
        mark_app_running()
        self.task_supervisor = TaskSupervisor(logger=self.logger)
        cleanup_debug_logs()
        cache_cleanup = cleanup_segment_audio_cache(self.config.paths.cache_dir, force=True)
        if int(cache_cleanup.get("removed_files") or 0):
            self.logger.info(
                "RESULT step=cache.segment_audio_cleanup removed_files=%s removed_mb=%.1f remaining_mb=%.1f",
                cache_cleanup.get("removed_files", 0),
                float(cache_cleanup.get("removed_bytes") or 0) / (1024 * 1024),
                float(cache_cleanup.get("remaining_bytes") or 0) / (1024 * 1024),
            )
        self.manager = AnimeManager(self.config, log=self.logger.info)
        self._background_quiet_lock = threading.Lock()
        self._background_quiet_done = False
        self._quiesce_qbittorrent(reason="startup")
        self.companion_base_url = ""
        self._companion_server: Any | None = None
        self._companion_thread: threading.Thread | None = None
        self._configure_database_services()
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
            cover_cache_dir=self.config.library.cover_cache_dir,
            ffmpeg=self.config.tools.ffmpeg,
            python=python_executable(),
            stt_model=self.config.sync.japanese_stt_model,
            job_center=self.job_center,
            work_scheduler=self.manager.work_scheduler,
        )
        if not self.safe_mode.active:
            self.task_supervisor.start(
                name="audiobook-stt-resume",
                target=self.audiobooks.resume_pending_transcriptions,
            )
        self._ui_state_cache = UIStateSnapshotCache()
        # Torrent traffic is a dedicated session state.  Generic settings
        # snapshots must never be able to roll a user's explicit On/Off click
        # back a few seconds later.
        self._torrent_state_lock = threading.RLock()
        self._torrent_session_enabled = bool(self.config.nyaa.torrents_enabled)
        # Until the user explicitly toggles Torrent in this process, the
        # loaded config remains authoritative. This preserves startup/config
        # mutation semantics while still preventing stale snapshots from
        # overriding an explicit click later in the session.
        self._torrent_session_authoritative = False
        self.visual_novels = VisualNovelService(logger=self.logger)
        self._planning_search_cache = MetadataCache(
            self.config.paths.cache_dir,
            "anilist-planning-search",
            schema="v2",
        )
        self._ln_audiobook_search_cache = MetadataCache(
            self.config.paths.cache_dir,
            "ln-audiobook-nyaa-search",
            schema="v159",
        )
        self._ln_audiobook_torrent_cache = MetadataCache(
            self.config.paths.cache_dir,
            "ln-audiobook-torrent-files",
            schema="v159",
        )
        self._ln_audiobook_download_lock = threading.Lock()
        self._ln_audiobook_download_jobs: dict[int, dict[str, Any]] = {}
        self._ln_audiobook_recovery_lock = threading.Lock()
        self._ln_audiobook_last_recovery_at = 0.0
        self.app_updater = AppUpdater(logger=self.logger)
        self.debug_snapshots = DebugSnapshotService(
            self.manager,
            cache_dir=self.config.paths.cache_dir,
            runtime_log_path=DEFAULT_LOG_PATH,
        )
        self.logger.info("APP session_start version=%s platform=%s", __version__, platform.platform())
        self.window: Any | None = None
        self._macos_window_lifecycle: _MacWindowLifecycle | None = None
        self._macos_app_delegate_proxy: Any | None = None
        self.asset_base = ""
        self._play_processes: dict[str, subprocess.Popen[Any]] = {}
        self._play_started_at: dict[str, float] = {}
        self._play_exit_codes: dict[str, tuple[int, float]] = {}
        self._play_registry_path = self.config.paths.cache_dir / "active-playbacks.json"
        self._play_registry: dict[str, dict[str, Any]] = self._load_play_registry()
        self._play_lock = threading.Lock()
        self._anilist_sync_lock = threading.Lock()
        self._local_refresh_lock = threading.Lock()
        self._restore_lock = threading.Lock()
        self._startup_maintenance_lock = threading.Lock()
        self._download_poll_lock = threading.Lock()
        self._torrent_traffic_lock = threading.Lock()
        self._last_torrent_traffic: dict[str, Any] = {
            "download_speed": 0,
            "upload_speed": 0,
            "active": 0,
            "waiting": 0,
        }
        self._last_network_guard: dict[str, Any] = {}
        self._fullscreen_exit_lock = threading.Lock()
        self._fullscreen_exit_pending = False
        self._drop_import_bound = False
        self._drop_import_element: Any | None = None
        self._drop_import_handlers: list[Any] = []
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
        self._jiten_refresh_lock = threading.Lock()
        self._jiten_refresh_thread: threading.Thread | None = None
        self._jiten_refresh_job_id = ""
        self._irodori_tts_lock = threading.RLock()
        self._irodori_tts_threads: dict[int, threading.Thread] = {}
        self._irodori_tts_job_ids: dict[int, str] = {}
        self._audiobook_generation_controls: dict[int, str] = {}
        self._audiobook_generation_start_params: dict[int, dict[str, Any]] = {}
        self._irodori_install_lock = threading.Lock()
        self._irodori_install_thread: threading.Thread | None = None
        self._irodori_install_state: dict[str, Any] = {
            "state": "idle",
            "detail": "",
            "started_at": 0.0,
            "finished_at": 0.0,
        }
        self._irodori_server_lock = threading.Lock()
        self._irodori_server_process: subprocess.Popen[Any] | None = None
        # Managed Irodori belongs to this Pudge process and must not survive it.
        import atexit
        atexit.register(self._stop_managed_irodori_server)
        self._irodori_remote_revision_cache = ""
        self._irodori_remote_revision_checked_at = 0.0
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
        self._ensure_energy_monitor(reason="startup")
        if not self.safe_mode.active:
            self._start_scheduled_agent()

    def _configure_database_services(self) -> None:
        """Rebind small controllers whenever configuration replaces the database."""

        previous_streaming = getattr(self, "companion_streaming", None)
        if previous_streaming is not None:
            previous_streaming.close()
        self.cache_registry = CacheRegistry(self.manager.db, self.config.paths.cache_dir)
        self.diagnostics = DiagnosticRecorder(self.manager.db)
        self.debug_bundle_builder = DebugBundleBuilder(self.manager.db, self.diagnostics)
        self.mobile_sync = MobileSyncService(
            self.manager.db,
            pairing_ttl_seconds=self.config.companion.pairing_ttl_seconds,
            max_events_per_request=self.config.companion.max_events_per_request,
        )
        self.companion_streaming = CompanionStreamingService(
            self.manager.db,
            cache_dir=self.config.paths.cache_dir,
            ffmpeg=self.config.tools.ffmpeg,
            ffprobe=self.config.tools.ffprobe,
            logger=self.logger,
            work_scheduler=self.manager.work_scheduler,
            task_supervisor=self.task_supervisor,
            cache_registry=self.cache_registry,
        )
        self.companion_controller = CompanionController(
            self.mobile_sync,
            self.companion_streaming,
        )
        self.diagnostics_controller = DiagnosticsController(
            self.debug_bundle_builder,
            lambda media_id, episode: self.debug_snapshots.snapshot(media_id, episode),
        )
        self.job_center = JobCenter(self.manager.db)

    def _ensure_energy_monitor(self, *, reason: str) -> None:
        """Keep low-overhead energy diagnostics alive, including Safe Mode.

        Safe Mode is exactly when diagnostics are most useful, so it must not
        suppress the monitor. The monitor itself is resilient to a failed
        sample and ``start`` is idempotent.
        """

        safe_mode_active = bool(getattr(getattr(self, "safe_mode", None), "active", False))
        if sys.platform != "darwin":
            self.logger.info(
                "SKIP step=energy_diagnostics.ensure reason=%s platform=%s",
                reason,
                sys.platform,
            )
            return
        if not bool(self.config.diagnostics.energy_monitoring_enabled):
            self.logger.info(
                "SKIP step=energy_diagnostics.ensure reason=%s disabled=true",
                reason,
            )
            self.energy_monitor.stop()
            return
        try:
            already_running = bool(self.energy_monitor.running)
            self.energy_monitor.start()
            self.logger.info(
                "EVENT energy_diagnostics.ensure reason=%s running=%s already_running=%s safe_mode=%s",
                reason,
                bool(self.energy_monitor.running),
                already_running,
                safe_mode_active,
            )
        except Exception as exc:
            self.logger.exception(
                "FAIL step=energy_diagnostics.ensure reason=%s error=%r",
                reason,
                str(exc),
            )

    @staticmethod
    def _scheduled_agent_plist() -> Path:
        label = f"{APP_BUNDLE_ID.removesuffix('.app')}.agent"
        return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

    def _start_scheduled_agent(self) -> None:
        if sys.platform != "darwin":
            return
        plist = self._scheduled_agent_plist()
        if not plist.is_file():
            return
        try:
            subprocess.run(
                ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _stop_scheduled_agent(self) -> None:
        if sys.platform != "darwin":
            return
        plist = self._scheduled_agent_plist()
        if not plist.is_file():
            return
        try:
            subprocess.run(
                ["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _shutdown_aria2_from_config(self, *, reason: str) -> bool:
        """Stop an already-running Pudge aria2 sidecar without ever starting it."""
        aria = self.config.aria2
        state_dir = DATA_DIR / "aria2"
        if not aria.enabled and not state_dir.exists():
            return False
        client = Aria2Client(
            enabled=True,
            binary=aria.binary,
            rpc_port=aria.rpc_port,
            state_dir=state_dir,
            pre_download_command=self.config.qbittorrent.pre_download_command,
            paused_on_add=aria.paused_on_add,
            auto_start=False,
            source_proxy_mode=self.config.nyaa.proxy_mode,
            source_proxy_url=self.config.nyaa.proxy_url,
            seed_mode=aria.seed_mode,
            seed_ratio=aria.seed_ratio,
            seed_time_minutes=aria.seed_time_minutes,
            upload_limit_kib=aria.upload_limit_kib,
            vpn_interface=aria.vpn_interface,
            vpn_kill_switch=aria.vpn_kill_switch,
        )
        try:
            stopped = bool(client.shutdown(save_session=True))
            self.logger.info(
                "EVENT torrent.aria2_quiet reason=%s stopped=%s", reason, stopped
            )
            return stopped
        except Exception as exc:
            self.logger.warning(
                "FALLBACK step=torrent.aria2_quiet reason=%s error=%r",
                reason,
                str(exc),
            )
            return False
        finally:
            client.close()

    def _quiesce_qbittorrent(self, *, reason: str) -> int:
        """Pause only Pudge-owned qBittorrent hashes, without launching qBittorrent."""
        qbt = self.config.qbittorrent
        if not qbt.enabled or not hasattr(self, "manager"):
            return 0
        client = QBittorrentClient(
            qbt.base_url,
            qbt.username,
            qbt.password,
            qbt.api_key,
            verify_tls=qbt.verify_tls,
            pre_download_command=qbt.pre_download_command,
            auto_start_app=False,
        )
        paused = 0
        try:
            for row in self.manager.db.downloads():
                torrent_hash = str(getattr(row, "torrent_hash", "") or "").strip().lower()
                if not torrent_hash:
                    continue
                client.pause(torrent_hash)
                paused += 1
        except Exception as exc:
            self.logger.debug(
                "Torrent qBittorrent quiet skipped reason=%s error=%s", reason, exc
            )
        finally:
            client.close()
        if paused:
            self.logger.info(
                "EVENT torrent.qbittorrent_quiet reason=%s paused=%s", reason, paused
            )
        return paused

    def _quiesce_torrent_backends(self, *, reason: str) -> dict[str, Any]:
        return {
            "aria2_stopped": self._shutdown_aria2_from_config(reason=reason),
            "qbittorrent_paused": self._quiesce_qbittorrent(reason=reason),
        }

    def _enter_background_quiet(self, *, reason: str) -> dict[str, Any]:
        lock = getattr(self, "_background_quiet_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._background_quiet_lock = lock
        with lock:
            if bool(getattr(self, "_background_quiet_done", False)):
                return {"already_quiet": True}
            self._background_quiet_done = True
        mark_app_stopped()
        self._stop_scheduled_agent()
        torrent_result = self._quiesce_torrent_backends(reason=reason)
        self.logger.info(
            "EVENT app.background_quiet reason=%s aria2_stopped=%s qbittorrent_paused=%s",
            reason,
            torrent_result.get("aria2_stopped", False),
            torrent_result.get("qbittorrent_paused", 0),
        )
        return torrent_result

    def _quiesce_manga_ocr(self, *, timeout: float = 5.0) -> list[str]:
        lock = getattr(self, "_manga_book_ocr_lock", None)
        if lock is None:
            return []
        with lock:
            for event in getattr(self, "_manga_ocr_cancel_events", {}).values():
                event.set()
            threads = [
                thread for thread in getattr(self, "_manga_book_ocr_threads", {}).values()
                if thread.is_alive() and thread is not threading.current_thread()
            ]
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return sorted(thread.name for thread in threads if thread.is_alive())

    def _restore_background_blockers(self) -> list[str]:
        blockers: list[threading.Thread] = []
        for name in (
            "_startup_maintenance_thread",
            "_planning_episode_download_thread",
            "_jiten_refresh_thread",
            "_irodori_install_thread",
        ):
            thread = getattr(self, name, None)
            if isinstance(thread, threading.Thread) and thread.is_alive():
                blockers.append(thread)
        with self._irodori_tts_lock:
            blockers.extend(
                thread for thread in self._irodori_tts_threads.values()
                if thread.is_alive()
            )
        return sorted({thread.name for thread in blockers})

    def _reload_runtime_services_after_restore(self) -> None:
        self.config = load_config(self.config_path)
        self.manager = AnimeManager(self.config, log=self.logger.info)
        self._configure_database_services()
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
            cover_cache_dir=self.config.library.cover_cache_dir,
            ffmpeg=self.config.tools.ffmpeg,
            python=python_executable(),
            stt_model=self.config.sync.japanese_stt_model,
            job_center=self.job_center,
            work_scheduler=self.manager.work_scheduler,
        )
        self._ui_state_cache.invalidate()
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

    def close(self) -> None:
        # Cmd+Q means Pudge goes fully idle: no scheduled maintenance and no
        # detached torrent sidecar continuing to download or seed in the background.
        self._enter_background_quiet(reason="close")
        cache_cleanup = {"removed_files": 0}
        config = getattr(self, "config", None)
        cache_dir = getattr(getattr(config, "paths", None), "cache_dir", None)
        if cache_dir is not None:
            cache_cleanup = cleanup_segment_audio_cache(cache_dir, force=True)
        if int(cache_cleanup.get("removed_files") or 0):
            self.logger.info(
                "EVENT cache.segment_audio_quiet removed=%s",
                cache_cleanup.get("removed_files", 0),
            )
        self._stop_companion_server()
        streaming = getattr(self, "companion_streaming", None)
        if streaming is not None:
            streaming.close()
        self.energy_monitor.stop()
        manga_lingering = self._quiesce_manga_ocr(timeout=5.0)
        audiobook_close = getattr(self.audiobooks, "close", None)
        if callable(audiobook_close):
            audiobook_lingering = list(audiobook_close(timeout=5.0) or [])
        else:
            self.audiobooks.stop_all()
            audiobook_lingering = []
        self.visual_novels.stop()
        supervisor = getattr(self, "task_supervisor", None)
        supervisor_lingering = list(
            (supervisor.shutdown(timeout=5.0) if supervisor is not None else []) or []
        )
        lingering = [*manga_lingering, *audiobook_lingering, *supervisor_lingering]
        if lingering:
            self.logger.warning(
                "WAIT step=app.shutdown lingering=%s",
                ",".join(sorted(set(lingering))),
            )
        safe_mode = getattr(self, "safe_mode", None)
        if safe_mode is not None and not lingering:
            safe_mode.finish_cleanly()

    def _close_window_for_uninstall(self) -> None:
        lifecycle = self._macos_window_lifecycle
        if lifecycle is not None:
            lifecycle.request_quit("uninstall")
        window = self.window
        if window is None:
            return
        try:
            window.destroy()
        except Exception as exc:
            self.logger.warning("FALLBACK step=app.uninstall_close error=%r", str(exc))

    def uninstall_pudge(self) -> dict[str, Any]:
        """Start a detached cleanup process, then close the application."""

        if sys.platform != "darwin":
            raise RuntimeError("The built-in uninstaller is available only on macOS")
        plan = build_uninstall_plan(
            config_path=self.config_path,
            cache_dir=self.config.paths.cache_dir,
            database_path=self.config.library.database_path,
            library_root=self.config.library.root_dir,
        )
        launch_uninstaller(plan)
        self.logger.info("EVENT app.uninstall_started targets=%s", len(plan.targets))
        close_timer = threading.Timer(0.2, self._close_window_for_uninstall)
        close_timer.daemon = True
        close_timer.start()
        return {"ok": True, "target_count": len(plan.targets)}

    @staticmethod
    def _companion_lan_host() -> str:
        candidates: list[str] = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect(("1.1.1.1", 80))
                candidates.append(str(sock.getsockname()[0]))
            finally:
                sock.close()
        except OSError:
            pass
        try:
            candidates.extend(
                str(item[4][0])
                for item in socket.getaddrinfo(
                    socket.gethostname(),
                    None,
                    family=socket.AF_INET,
                    type=socket.SOCK_STREAM,
                )
            )
        except OSError:
            pass
        for host in candidates:
            if host and host != "0.0.0.0" and not host.startswith("127."):
                return host
        return "127.0.0.1"

    def _companion_effective_base_url(self) -> str:
        server = getattr(self, "_companion_server", None)
        if server is not None:
            host, port = server.server_address
            host = str(host)
            if host in {"0.0.0.0", "::"}:
                host = self._companion_lan_host()
            return f"http://{host}:{int(port)}"
        raw = str(self.companion_base_url or "")
        if raw and "0.0.0.0" not in raw:
            return raw
        return ""

    def _start_companion_server(self) -> None:
        if self._companion_server is not None:
            return
        server, thread = start_mobile_sync_server(
            self.mobile_sync,
            host=self.config.companion.bind_host,
            port=self.config.companion.port,
            streaming=self.companion_streaming,
            study_parser=self.light_novels.parse_study_text,
            logger=self.logger,
        )
        self._companion_server = server
        self._companion_thread = thread
        host, port = server.server_address
        self.companion_base_url = f"http://{host}:{int(port)}"

    def _stop_companion_server(self) -> None:
        server = getattr(self, "_companion_server", None)
        self._companion_server = None
        self._companion_thread = None
        self.companion_base_url = ""
        if server is None:
            return
        try:
            server.shutdown()
        finally:
            server.server_close()

    def companion_enable_lan(self) -> dict[str, Any]:
        cfg = self.config.companion
        previous_enabled = bool(cfg.enabled)
        previous_host = str(cfg.bind_host)
        if self._companion_server is not None:
            bound_host = str(self._companion_server.server_address[0])
            if bound_host not in {"0.0.0.0", "::"}:
                self._stop_companion_server()
        cfg.enabled = True
        cfg.bind_host = "0.0.0.0"
        write_config(self.config, self.config_path)
        try:
            self._start_companion_server()
        except OSError:
            cfg.enabled = previous_enabled
            cfg.bind_host = previous_host
            write_config(self.config, self.config_path)
            raise
        return self.companion_status()

    def companion_disable(self) -> dict[str, Any]:
        self._stop_companion_server()
        self.config.companion.enabled = False
        write_config(self.config, self.config_path)
        return self.companion_status()

    def companion_status(self) -> dict[str, Any]:
        return self.companion_controller.status(
            self.config.companion,
            self._companion_effective_base_url(),
        )

    def companion_start_pairing(self) -> dict[str, Any]:
        if not self.config.companion.enabled or self._companion_server is None:
            self.companion_enable_lan()
        payload = self.mobile_sync.start_pairing()
        base_url = self._companion_effective_base_url()
        payload["base_url"] = base_url
        payload["companion_url"] = (
            f"{base_url}/companion/?pair={quote(str(payload['pairing_token']))}"
            if base_url
            else ""
        )
        return payload

    def companion_revoke_device(self, device_id: str) -> dict[str, Any]:
        return self.companion_controller.revoke(str(device_id or ""))

    def companion_sync_conflicts(self, limit: int = 100) -> dict[str, Any]:
        return self.companion_controller.conflicts(limit=int(limit))

    def companion_resolve_sync_conflict(
        self, conflict_id: int, resolution: str = "local"
    ) -> dict[str, Any]:
        return self.companion_controller.resolve_conflict(
            int(conflict_id), str(resolution)
        )

    def companion_library_snapshot(self) -> dict[str, Any]:
        return self.mobile_sync.library_snapshot()

    def visual_novel_windows(self) -> list[dict[str, Any]]:
        return self.visual_novels.windows()

    def visual_novel_state(self) -> dict[str, Any]:
        return self.visual_novels.state()

    def visual_novel_start(self, window_id: int, title: str = "") -> dict[str, Any]:
        return self.visual_novels.start(int(window_id), str(title or ""))

    def visual_novel_stop(self) -> dict[str, Any]:
        return self.visual_novels.stop()

    def visual_novel_set_dialogue_region(
        self, x: float, y: float, width: float, height: float
    ) -> dict[str, Any]:
        return self.visual_novels.set_dialogue_region(x, y, width, height)

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

    def bind_drop_import(self) -> dict[str, Any]:
        """Bind Finder drops only after JS reports that the DOM is ready.

        Dragover stays entirely inside WebKit. Crossing every dragover event into
        Python makes Cocoa visibly stall, while binding during webview.start can
        attach to a document that is replaced by the real page a moment later.
        """
        if self._drop_import_bound:
            return {"ok": True, "bound": True, "already_bound": True}
        if self.window is None:
            return {"ok": False, "bound": False, "error": "window not ready"}

        try:
            from webview.dom import DOMEventHandler

            element = self.window.dom.get_element("#app")
            if element is None:
                return {"ok": False, "bound": False, "error": "#app not ready"}

            def dropped(event: dict[str, Any]) -> None:
                transfer = event.get("dataTransfer") or event.get("domTransfer") or {}
                files = transfer.get("files") or []
                paths = [
                    str(item.get("pywebviewFullPath") or "")
                    for item in files
                    if isinstance(item, dict) and item.get("pywebviewFullPath")
                ]
                if not paths:
                    self.logger.warning("SKIP step=app.drop_import reason=no_absolute_paths")
                    return
                self.logger.info("START step=app.drop_import paths=%r", paths)

                def import_drop() -> None:
                    try:
                        result = self.import_dropped_paths(paths)
                    except Exception as exc:
                        result = {
                            "ok": False,
                            "imports": [],
                            "focus": None,
                            "errors": [str(exc)],
                        }
                    window = self.window
                    if window is None:
                        return
                    try:
                        payload = json.dumps(result, ensure_ascii=False).replace("</", "<\\/")
                        script = (
                            "window.dispatchEvent(new CustomEvent('pudge-files-imported',{detail:"
                            + payload
                            + "}));"
                        )
                        runner = getattr(window, "run_js", None)
                        if callable(runner):
                            runner(script)
                        else:
                            window.evaluate_js(script)
                    except Exception as exc:
                        self.logger.warning("FAIL step=drop.focus error=%r", str(exc))

                threading.Thread(
                    target=import_drop,
                    name=f"{APP_SLUG}-drop-import",
                    daemon=True,
                ).start()

            # HTML handles dragenter/dragover locally. Only the single drop event
            # crosses the JS/Python bridge, which keeps the Cocoa UI responsive.
            drop_handler = DOMEventHandler(dropped, True, True)
            element.events.drop += drop_handler
            self._drop_import_element = element
            self._drop_import_handlers = [drop_handler]
            self._drop_import_bound = True
            self.logger.info("EVENT app.drop_import_bound target=#app timing=pywebviewready")
            return {"ok": True, "bound": True, "already_bound": False}
        except Exception as exc:
            self.logger.warning("FALLBACK step=app.drop_import_bind error=%r", str(exc))
            return {"ok": False, "bound": False, "error": str(exc)}

    def _downloads_enabled(self) -> bool:
        # Session intent is the single UI/API owner. Manager config is mirrored
        # for workers, but a stale config object must never repaint Torrent Off.
        return bool(self._torrent_enabled_state() and self._downloads_configured())

    def _downloads_configured(self) -> bool:
        checker = getattr(self.manager, "downloads_configured", None)
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
            "japanese_subtitles_required": self.manager.japanese_subtitles_required(anime.media_id),
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

    def _watched_on_anilist(
        self, anime: LibraryAnime | None, episode: int | None
    ) -> bool:
        if anime is None:
            return False
        return watched_by_anilist_progress(
            self._display_episode_number(anime, episode),
            anime.progress,
            total_episodes=anime.episodes,
            media_format=anime.format,
        )

    def _local_episode_for_relative(
        self, anime: LibraryAnime, relative_episode: int | None
    ):
        if relative_episode is None:
            return self.manager.db.ready_episode(anime.media_id, None)
        exact = self.manager.db.ready_episode(anime.media_id, relative_episode)
        if exact is not None:
            return exact
        for item in self.manager.db.episodes(anime.media_id):
            if item.state not in {"ready", "local", "watched", "waiting_subtitles", "waiting_text_subtitles", "couldnt_sync"}:
                continue
            if self._display_episode_number(anime, item.episode) == int(relative_episode):
                return item
        return None

    def _ready_subtitle_is_usable(self, item: Any) -> bool:
        if item is None:
            return False
        if item.embedded_subtitle_id is not None:
            return True
        if item.subtitle_path is None:
            # Legacy/tests can contain a pathless Ready row. The corruption we
            # need to catch is stricter: SQLite points at an external subtitle
            # file that no longer exists.
            return str(item.state or "") in {"ready", "watched"}
        try:
            return item.subtitle_path.is_file() and item.subtitle_path.stat().st_size > 0
        except OSError:
            return False

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
        japanese_subtitles_required = self.manager.japanese_subtitles_required(anime.media_id)
        released = anime.released_episodes
        outdated = bool(released and released > anime.progress)
        local_target = None if str(anime.format or "").upper() == "MOVIE" else anime.next_episode
        local = self._local_episode_for_relative(anime, local_target)
        next_queue_count = 0
        franchise_queue_available = False
        if anime.media_status != "NOT_YET_RELEASED":
            next_queue_count = len(self._ready_queue_items([anime.media_id], limit=5))
            relation_ids = sorted(self._relation_ids(anime))
            franchise_queue_available = bool(
                self._ready_queue_items([anime.media_id, *relation_ids], limit=1)
            )
        local_state = str(local.state or "") if local is not None else ""
        if local is not None and not japanese_subtitles_required and local_state != "watched":
            local_state = "ready"
        if (
            local is not None
            and local_state == "ready"
            and japanese_subtitles_required
            and not self._ready_subtitle_is_usable(local)
        ):
            local_state = "waiting_subtitles"
        if (
            local is not None
            and local_state == "ready"
            and str(local.subtitle_origin or "").casefold() == "ocr"
            and not self.config.matching.ocr_counts_as_ready
        ):
            local_state = "waiting_text_subtitles"
        return {
            "media_id": anime.media_id,
            "japanese_subtitles_required": japanese_subtitles_required,
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
                            local_state == "waiting_text_subtitles"
                            and (local.subtitle_path is not None or local.embedded_subtitle_id is not None)
                        )
                        else "external"
                        if local.subtitle_path
                        else "embedded"
                        if (
                            local_state in {"ready", "watched"}
                            and local.embedded_subtitle_id is not None
                        )
                        else "none"
                    ),
                    "subtitle_origin": str(local.subtitle_origin or ""),
                    "original_state": str(local.state or ""),
                    "state": local_state,
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
            if self._watched_on_anilist(anime, item.episode):
                continue
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
            if self._watched_on_anilist(anime, item.episode):
                effective_state = "watched"
            if effective_state == "ready" and (
                str(item.subtitle_origin or "").casefold() == "bitmap"
                or (
                    item.subtitle_path is not None
                    and item.subtitle_path.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS
                )
                or (
                    str(item.subtitle_origin or "").casefold() == "ocr"
                    and not self.config.matching.ocr_counts_as_ready
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
                1 for item in group["episodes"] if item["state"] == "ready"
            )
            group["watched_count"] = sum(
                1 for item in group["episodes"] if item["state"] == "watched"
            )
            group["waiting_count"] = sum(
                1
                for item in group["episodes"]
                if item["state"] in {"waiting_subtitles", "waiting_text_subtitles", "couldnt_sync"}
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
            anime = anime_by_id.get(item.media_id) if item.media_id is not None else None
            subtitle_optional = bool(
                item.media_id is not None
                and not self.manager.japanese_subtitles_required(item.media_id)
            )
            if (
                (item.state != "ready" and not subtitle_optional)
                or (
                    not subtitle_optional
                    and item.state == "ready"
                    and not self._ready_subtitle_is_usable(item)
                )
                or (
                    not subtitle_optional
                    and str(item.subtitle_origin or "").casefold() == "ocr"
                    and not self.config.matching.ocr_counts_as_ready
                )
                or not item.video_path.is_file()
                or self.manager._path_within(item.video_path, incomplete)
                or self._watched_on_anilist(anime, item.episode)
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
                    "japanese_subtitles_required": (
                        self.manager.japanese_subtitles_required(media_id)
                        if media_id is not None else True
                    ),
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
                        "subtitle_source": (
                            "external" if first.subtitle_path
                            else "embedded" if first.embedded_subtitle_id is not None
                            else "none"
                        ),
                        "original_state": str(first.state or ""),
                        "state": (
                            "ready"
                            if media_id is not None
                            and not self.manager.japanese_subtitles_required(media_id)
                            and first.state != "watched"
                            else first.state
                        ),
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
            anime = anime_by_id.get(item.media_id) if item.media_id is not None else None
            if item.media_id is not None and not self.manager.japanese_subtitles_required(item.media_id):
                continue
            effective_ocr_waiting = bool(
                item.state == "ready"
                and str(item.subtitle_origin or "").casefold() == "ocr"
                and not self.config.matching.ocr_counts_as_ready
            )
            if (
                (item.state in {"ready", "watched"} and not effective_ocr_waiting)
                or not item.video_path.is_file()
                or self.manager._path_within(item.video_path, incomplete)
                or self._watched_on_anilist(anime, item.episode)
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
                        "state": (
                            "waiting_text_subtitles"
                            if item.state == "ready"
                            and str(item.subtitle_origin or "").casefold() == "ocr"
                            and not self.config.matching.ocr_counts_as_ready
                            else item.state
                        ),
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

    def _recently_watched_payloads(
        self,
        anime_by_id: dict[int, LibraryAnime],
    ) -> list[dict[str, Any]]:
        # Latest watched local episode per anime inside the cleanup TTL.
        try:
            ttl_hours = max(
                0.0,
                float(self.config.agent.delete_after_watched_hours),
            )
        except (TypeError, ValueError):
            ttl_hours = 0.0
        if ttl_hours <= 0.0:
            return []

        cutoff = time.time() - ttl_hours * 3600.0
        incomplete = self.manager.incomplete_download_paths()
        latest: dict[int, Any] = {}

        for item in self.manager.db.episodes():
            if item.media_id is None or item.watched_at is None:
                continue
            try:
                watched_at = float(item.watched_at)
            except (TypeError, ValueError):
                continue
            if watched_at < cutoff:
                continue
            if not item.video_path.is_file():
                continue
            if self.manager._path_within(item.video_path, incomplete):
                continue

            media_id = int(item.media_id)
            previous = latest.get(media_id)
            if (
                previous is None
                or watched_at > float(previous.watched_at or 0.0)
            ):
                latest[media_id] = item

        rows: list[dict[str, Any]] = []
        rewind = max(0.0, float(self.config.playback.rewind_seconds or 0.0))
        for media_id, item in latest.items():
            anime = anime_by_id.get(media_id)
            if anime is None:
                continue

            position = max(0.0, float(item.playback_position or 0.0))
            duration = max(0.0, float(item.playback_duration or 0.0))
            progress = (
                round(max(0.0, min(100.0, position / duration * 100.0)), 2)
                if duration > 0.0
                else None
            )
            episode = item.media_episode if item.media_episode is not None else item.episode
            payload = self._anime_payload(anime)
            payload.update(
                {
                    "episode": episode,
                    "next_episode": episode,
                    "video_path": str(item.video_path),
                    "watched_at": float(item.watched_at or 0.0),
                    "resume_start": max(0.0, position - rewind),
                    "progress_percent": progress,
                    "is_movie": str(anime.format or "").upper() == "MOVIE",
                    "local": {
                        "episode": episode,
                        "state": "watched",
                        "video_path": str(item.video_path),
                        "subtitle_path": (
                            str(item.subtitle_path)
                            if item.subtitle_path is not None
                            else ""
                        ),
                        "subtitle_source": (
                            "external"
                            if item.subtitle_path is not None
                            else "embedded"
                            if item.embedded_subtitle_id is not None
                            else "none"
                        ),
                        "playback_position": item.playback_position,
                        "playback_duration": item.playback_duration,
                    },
                }
            )
            rows.append(payload)

        rows.sort(
            key=lambda item: (
                -float(item.get("watched_at") or 0.0),
                str(item.get("title") or "").casefold(),
            )
        )
        return rows

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
        downloads_by_media: dict[int, Any] = {}
        downloads_by_episode: dict[tuple[int, int], Any] = {}
        for item in self.manager.db.downloads():
            if item.media_id is None:
                continue
            media_id = int(item.media_id)
            # downloads() is newest-first; retain the newest matching job.
            downloads_by_media.setdefault(media_id, item)
            item_episode = item.media_episode if item.media_episode is not None else item.episode
            if item_episode is not None:
                downloads_by_episode.setdefault((media_id, int(item_episode)), item)

        def home_download(media_id: int, episode: int | None) -> Any | None:
            if episode is not None:
                exact = downloads_by_episode.get((int(media_id), int(episode)))
                if exact is not None:
                    return exact
            fallback = downloads_by_media.get(int(media_id))
            return fallback if fallback is not None and fallback.is_batch else None
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
                local_state = str(
                    (base.get("local") or {}).get("state")
                    if isinstance(base.get("local"), dict)
                    else ""
                )
                if local_state in {
                    "local",
                    "waiting_subtitles",
                    "waiting_text_subtitles",
                    "couldnt_sync",
                }:
                    # The nearest unwatched episode is already local but not
                    # playable yet. Never replace that with a misleading batch
                    # download action just because later episodes are ready.
                    sections["waiting"].append(base)
                else:
                    # Finished/paused non-airing titles in Watching otherwise had no
                    # actionable home card. Offer a full batch download without
                    # changing their AniList list status. Existing qBittorrent work
                    # is attached so the UI cannot offer another duplicate download.
                    download = home_download(anime.media_id, anime.next_episode)
                    if download is not None:
                        raw_state = str(download.state or "")
                        effective_state = raw_state
                        if not download_complete(download) and not self._torrent_enabled_state():
                            effective_state = "paused"
                        base["download"] = {
                            "torrent_hash": download.torrent_hash,
                            "name": download.name,
                            "state": raw_state,
                            "effective_state": effective_state,
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
            if anime is not None:
                ready_episodes = set(item.get("ready_episodes") or [])
                next_is_ready = anime.next_episode in ready_episodes
                if anime.format == "MOVIE" and None in ready_episodes:
                    next_is_ready = True
                if next_is_ready:
                    if anime.media_status == "RELEASING":
                        sections["new_ready"].append(item)
                    else:
                        sections["completed_ready"].append(item)
                else:
                    # Never skip the nearest unwatched episode merely because a
                    # later local episode is Ready. This applies to FINISHED /
                    # PLANNING titles too, not only currently airing anime.
                    sections["waiting"].append(self._anime_payload(anime))
            else:
                sections["completed_ready"].append(item)

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
                target_episode = card.get("next_episode")
                if local is not None and local.get("episode") is not None:
                    target_episode = local.get("episode")
                download = home_download(media_id, target_episode) if media_id else None
                action_job = None
                if local is not None and local.get("video_path"):
                    action_job = needs_action_by_path.get(str(local.get("video_path")))
                card["presentation"] = derive_episode_presentation(
                    local=local_for_state,
                    download=download,
                    action_job=action_job,
                    allow_ocr_ready=bool(self.config.matching.ocr_counts_as_ready),
                    downloads_enabled=self._torrent_enabled_state(),
                )
        return sections

    def _torrent_state_guard(self) -> threading.RLock:
        lock = getattr(self, "_torrent_state_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._torrent_state_lock = lock
        if not hasattr(self, "_torrent_session_enabled"):
            # Real AppConfig always has ``nyaa``. A few long-lived unit-test
            # stubs intentionally provide only qBittorrent/aria2 config, though.
            # Preserve their pre-session semantics instead of crashing while
            # still making the explicit Nyaa toggle authoritative in runtime.
            config = getattr(self, "config", None)
            nyaa = getattr(config, "nyaa", None)
            if nyaa is not None and hasattr(nyaa, "torrents_enabled"):
                enabled = bool(nyaa.torrents_enabled)
            else:
                checker = getattr(getattr(self, "manager", None), "downloads_enabled", None)
                if callable(checker):
                    try:
                        enabled = bool(checker())
                    except Exception:
                        enabled = True
                else:
                    # Lightweight callers may construct WebAppApi without a
                    # config object only to serialize an already-running job.
                    # Missing configuration is not an explicit Torrent Off.
                    enabled = True
            self._torrent_session_enabled = enabled
        if not hasattr(self, "_torrent_session_authoritative"):
            self._torrent_session_authoritative = False
        return lock

    def _torrent_enabled_state(self) -> bool:
        lock = self._torrent_state_guard()
        with lock:
            if not bool(self._torrent_session_authoritative):
                config = getattr(self, "config", None)
                nyaa = getattr(config, "nyaa", None)
                if nyaa is not None and hasattr(nyaa, "torrents_enabled"):
                    self._torrent_session_enabled = bool(nyaa.torrents_enabled)
            return bool(self._torrent_session_enabled)

    def _bump_ui_state_version(self) -> str:
        manager = getattr(self, "manager", None)
        db = getattr(manager, "db", None)
        if db is None:
            return ""
        try:
            version = int(db.get_state("ui_state_version", "0") or 0)
        except (TypeError, ValueError):
            version = 0
        value = str(version + 1)
        db.set_state("ui_state_version", value)
        return value

    def _store_ui_state_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Serialize the final torrent stamp and cache store with the toggle.
        # If an old get_state() started before the click, it either stores first
        # and is then invalidated by the toggle, or stores afterwards with the
        # new session value.  It can never resurrect an old Off snapshot.
        lock = self._torrent_state_guard()
        with lock:
            settings = payload.get("settings")
            if isinstance(settings, dict):
                settings["torrents_enabled"] = bool(self._torrent_session_enabled)
            manager = getattr(self, "manager", None)
            db = getattr(manager, "db", None)
            version = str(payload.get("ui_state_version") or "")
            pending = bool(getattr(self, "_torrent_ui_version_pending", False))
            if pending:
                try:
                    version = str(int(version or "0") + 1)
                except (TypeError, ValueError):
                    version = str(time.time_ns())
                payload["ui_state_version"] = version
                self._torrent_ui_version_pending = False
            elif db is not None:
                version = str(db.get_state("ui_state_version", version) or version)
                payload["ui_state_version"] = version
            return self._ui_state_cache.store(version, payload)

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
            "subsplease_rss_enabled": True,
            "subsplease_rss_preferred": True,
            "torrents_enabled": self._torrent_enabled_state(),
            "torrents_configured": self._downloads_configured(),
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
            "qbt_password": masked_secret(cfg.qbittorrent.password),
            "qbt_api_key": masked_secret(cfg.qbittorrent.api_key),
            "aria2_enabled": cfg.aria2.enabled,
            "aria2_binary": cfg.aria2.binary,
            "aria2_rpc_port": cfg.aria2.rpc_port,
            "aria2_seed_mode": cfg.aria2.seed_mode,
            "aria2_seed_ratio": cfg.aria2.seed_ratio,
            "aria2_seed_time_minutes": cfg.aria2.seed_time_minutes,
            "aria2_upload_limit_kib": cfg.aria2.upload_limit_kib,
            "aria2_vpn_interface": cfg.aria2.vpn_interface,
            "aria2_vpn_kill_switch": cfg.aria2.vpn_kill_switch,
            "torrent_backend": self.manager.torrent_backend_name() if callable(getattr(self.manager, "torrent_backend_name", None)) and self._downloads_configured() else "disabled",
            "download_hook": cfg.qbittorrent.pre_download_command,
            "agent_enabled": cfg.agent.enabled,
            "agent_poll": cfg.agent.poll_minutes,
            "anilist_refresh_poll": cfg.agent.anilist_refresh_minutes,
            "subtitle_poll": cfg.agent.subtitle_poll_minutes,
            "delete_hours": cfg.agent.delete_after_watched_hours,
            "anilist_enabled": cfg.anilist.enabled,
            "anilist_client_id": cfg.anilist.client_id,
            "anilist_token": masked_secret(cfg.anilist.access_token),
            "anilist_auto_progress": cfg.anilist.auto_update_progress,
            "anilist_add_if_missing": cfg.anilist.add_if_missing,
            "anilist_threshold": round(cfg.anilist.watched_threshold * 100, 2),
            "anilist_max_remaining_minutes": cfg.anilist.watched_max_remaining_minutes,
            "relations_by_release_date": cfg.anilist.relations_by_release_date,
            "jimaku_api_key": masked_secret(cfg.jimaku.personal_api_key),
            "jimaku_trial_active": cfg.jimaku.trial_active,
            "jimaku_trial_expires_at": cfg.jimaku.trial_expires_at,
            "jimaku_trial_remaining_seconds": max(
                0, int(cfg.jimaku.trial_expires_at - time.time())
            ) if cfg.jimaku.trial_active else 0,
            "ocr_image_subtitles": cfg.matching.ocr_image_subtitles,
            "ocr_counts_as_ready": cfg.matching.ocr_counts_as_ready,
            "auto_upgrade_subtitles": cfg.matching.auto_upgrade_subtitles,
            "subtitle_upgrade_min_score_gain": cfg.matching.subtitle_upgrade_min_score_gain,
            "subtitle_upgrade_check_hours": cfg.matching.subtitle_upgrade_check_hours,
            "max_subtitle_upgrade_checks_per_run": cfg.matching.max_subtitle_upgrade_checks_per_run,
            "llm_enabled": cfg.llm.enabled,
            "llm_provider": cfg.llm.provider,
            "llm_url": cfg.llm.base_url,
            "llm_api_key": masked_secret(cfg.llm.api_key),
            "llm_model": cfg.llm.model,
            "llm_reasoning_effort": cfg.llm.reasoning_effort,
            "subtitle_semantic_checks": cfg.llm.validate_embedded_reference,
            "use_container_chapters": cfg.sync.use_container_chapters,
            "japanese_stt_fallback": cfg.sync.japanese_stt_fallback,
            "japanese_stt_model": cfg.sync.japanese_stt_model,
            "shortcut_mpv_mark_watched": cfg.shortcuts.mpv_mark_watched,
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
        state = str(getattr(item, "state", "") or "")
        effective_state = state
        if (
            not download_complete(item)
            and state.casefold() not in {"error", "missingfiles", "unknown"}
            and not self._torrent_enabled_state()
        ):
            effective_state = "paused"
        return {
            "hash": item.torrent_hash,
            "torrent_hash": item.torrent_hash,
            "name": item.name,
            "anime_title": str(anime.title) if anime is not None else "",
            "state": state,
            "effective_state": effective_state,
            "progress": item.progress,
            "media_id": item.media_id,
            "episode": item.episode,
            "media_episode": getattr(item, "media_episode", None),
            "release_episode": getattr(item, "release_episode", None),
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
        all_anime = self.manager.db.anime_list()
        current_anime = [anime for anime in all_anime if anime.status == "CURRENT"]
        planned_anime = [anime for anime in all_anime if anime.status == "PLANNING"]
        anime_by_id = {anime.media_id: anime for anime in all_anime}
        download_rows = self.manager.db.downloads()
        downloads = [
            self._download_payload(item, anime_by_id) for item in download_rows
        ]
        download_by_media: dict[int, dict[str, Any]] = {}
        download_by_episode: dict[tuple[int, int], dict[str, Any]] = {}

        def download_rank(download: dict[str, Any]) -> tuple[int, float]:
            return (
                0 if str(download.get("state") or "").casefold() in {"active", "waiting", "paused"} else 1,
                -float(download.get("added_on") or 0),
            )

        for download in downloads:
            media_id = download.get("media_id")
            if media_id is None:
                continue
            key = int(media_id)
            previous = download_by_media.get(key)
            if previous is None or download_rank(download) < download_rank(previous):
                download_by_media[key] = download
            raw_episode = download.get("media_episode")
            if raw_episode is None:
                raw_episode = download.get("episode")
            if raw_episode is not None:
                episode_key = (key, int(raw_episode))
                previous_episode = download_by_episode.get(episode_key)
                if previous_episode is None or download_rank(download) < download_rank(previous_episode):
                    download_by_episode[episode_key] = download

        def download_for_episode(media_id: int, episode: int | None) -> dict[str, Any] | None:
            if episode is not None:
                exact = download_by_episode.get((int(media_id), int(episode)))
                if exact is not None:
                    return exact
            fallback = download_by_media.get(int(media_id))
            return fallback if fallback is not None and bool(fallback.get("is_batch")) else None

        ready_by_media = {
            int(item["media_id"]): item
            for item in self._downloaded_payloads(anime_by_id)
            if item.get("media_id") is not None
        }
        current = [self._anime_payload(a) for a in current_anime]
        for payload in current:
            download = download_for_episode(int(payload["media_id"]), payload.get("next_episode"))
            if (
                download is not None
                and not self._download_shadowed_by_local(payload, download)
            ):
                payload["download"] = download
        planned = []
        for anime in planned_anime:
            payload = self._anime_payload(anime)
            ready = ready_by_media.get(int(anime.media_id))
            if ready is not None:
                payload["ready_episodes"] = list(ready.get("ready_episodes") or [])
                payload["ready_count"] = int(ready.get("ready_count") or 0)
                payload["all_episodes_ready"] = bool(ready.get("all_episodes_ready"))
                if payload.get("local") is None:
                    payload["local"] = ready.get("local")
            download = download_for_episode(int(anime.media_id), payload.get("next_episode"))
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
        home["recently_watched"] = self._recently_watched_payloads(anime_by_id)

        def _attach_download(card: dict[str, Any]) -> None:
            if card.get("kind") == "watch_sequence":
                for child in card.get("items") or []:
                    if isinstance(child, dict):
                        _attach_download(child)
                return
            media_id = card.get("media_id")
            if media_id is not None:
                target_episode = card.get("next_episode")
                local = card.get("local") if isinstance(card.get("local"), dict) else None
                if local is not None and local.get("episode") is not None:
                    target_episode = local.get("episode")
                download = download_for_episode(int(media_id), target_episode)
                if download is not None and not self._download_shadowed_by_local(card, download):
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
        for media_id in download_by_media:
            anime = anime_by_id.get(media_id)
            if anime is None or media_id in represented:
                continue
            if str(anime.status or "").upper() not in {"CURRENT", "PLANNING"}:
                continue
            download = download_for_episode(media_id, anime.next_episode)
            if download is None:
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
            "torrent_waiting_count": sum(
                1
                for item in downloads
                if str(item.get("state") or "").casefold()
                in {"waiting", "queued", "stalled"}
            ),
            "subtitle_jobs": jobs,
            "release_upgrades": self.manager.db.upgrade_jobs(limit=50),
            "subtitle_history": self.manager.db.subtitle_history(limit=80),
            "playlists": self._playlist_payloads(),
            "active_playbacks": self._active_playbacks_payload(),
            "integrity_last_run": self.manager.db.get_state("integrity_last_run", ""),
            "settings": self._settings_payload(),
            "synced_at": self.manager.db.get_state("anilist_synced_at", ""),
            "ready_state_version": self.manager.db.get_state("ready_state_version", ""),
            "ui_state_version": self.manager.db.get_state("ui_state_version", ""),
            "storage": self._storage_payload(refresh=refresh_storage),
            "safe_mode": self.safe_mode.status(run_checks=False),
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
        # Reconciliation is a write operation, so it only runs on explicit full
        # refreshes and never in the one-second UI polling path.
        self.manager.reconcile_completed_download_rows()
        self.manager.reconcile_prepared_subtitle_rows()
        payload = self._get_state(refresh_storage=True)
        return self._store_ui_state_snapshot(payload)

    def get_state_fast(self) -> dict[str, Any]:
        """Return UI state without recursively scanning the video library.

        Startup maintenance polls this every second so newly prepared subtitles
        can move to the Ready section immediately. Disk usage is reused from the
        most recent full state and is refreshed when maintenance finishes.
        """
        lock = self._torrent_state_guard()
        with lock:
            version = self.manager.db.get_state("ui_state_version", "")
            cached = self._ui_state_cache.get(version)
            if cached is not None:
                settings = cached.get("settings")
                if isinstance(settings, dict):
                    settings["torrents_enabled"] = bool(self._torrent_session_enabled)
                return cached
        payload = self._get_state(refresh_storage=False)
        return self._store_ui_state_snapshot(payload)

    def get_state_delta(self, ui_state_version: str = "") -> dict[str, Any]:
        version = self.manager.db.get_state("ui_state_version", "")
        if str(ui_state_version) == version:
            return {"changed": False, "ui_state_version": version}
        state = self.get_state_fast()
        return {"changed": True, "ui_state_version": version, "state": state}

    def torrent_downloads(
        self,
        media_id: int | None = None,
        refresh: bool = True,
    ) -> dict[str, Any]:
        warning = ""
        if refresh and self._downloads_configured():
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
        network_guard: dict[str, Any] = dict(getattr(self, "_last_network_guard", {}) or {})
        if refresh and self.config.aria2.enabled:
            for backend, client in self.manager.torrent_clients():
                try:
                    if backend == "aria2" and isinstance(client, Aria2Client):
                        network_guard = client.network_guard_status()
                        self._last_network_guard = dict(network_guard)
                        break
                finally:
                    client.close()
        return {
            "backend": self.manager.torrent_backend_name() if self._downloads_configured() else "disabled",
            "enabled": self._downloads_enabled(),
            "configured": self._downloads_configured(),
            "downloads": [
                self._download_payload(item, anime_by_id) for item in rows
            ],
            "storage": self._storage_payload(refresh=False),
            "network_guard": network_guard,
            "warning": warning,
        }

    def torrent_traffic_status(self) -> dict[str, Any]:
        """Return authoritative torrent state/rates without rebuilding full UI state."""
        enabled = bool(self._torrent_enabled_state() and self._downloads_configured())
        manager_config = getattr(getattr(self, "manager", None), "config", None)
        if manager_config is not None and getattr(manager_config, "nyaa", None) is not None:
            manager_config.nyaa.torrents_enabled = bool(enabled)
        if not enabled:
            paused = self._paused_torrent_count()
            # Torrent traffic status is about backend jobs, not logical
            # download intents. A stale intent can legitimately survive after
            # discovery concludes that there is nothing to start; surfacing it
            # here as `waiting` makes Torrent Off claim traffic is pending when
            # no backend job exists.
            result = {
                "enabled": False,
                "download_speed": 0,
                "upload_speed": 0,
                "active": 0,
                "waiting": 0,
                "paused": paused,
                "updated_at": time.time(),
            }
            self._last_torrent_traffic = result
            return dict(result)

        if not self._torrent_traffic_lock.acquire(blocking=False):
            return dict(self._last_torrent_traffic)
        try:
            down = 0
            up = 0
            active = 0
            waiting = 0
            for backend, client in self.manager.torrent_clients():
                try:
                    if backend == "aria2" and isinstance(client, Aria2Client):
                        stats = client.traffic_stats()
                        down += max(0, int(stats.get("download_speed") or 0))
                        up += max(0, int(stats.get("upload_speed") or 0))
                        active += max(0, int(stats.get("active") or 0))
                        waiting = max(waiting, max(0, int(stats.get("waiting") or 0)))
                        continue

                    rows = client.torrents(category=self.config.qbittorrent.category)
                    for row in rows:
                        raw = dict(getattr(row, "raw", {}) or {})
                        row_down = self._download_number(
                            raw, "dlspeed", "download_speed", "downloadSpeed"
                        )
                        row_up = self._download_number(
                            raw, "upspeed", "upload_speed", "uploadSpeed"
                        )
                        down += row_down
                        up += row_up
                        state = str(getattr(row, "state", "") or "").casefold()
                        if state not in {"paused", "stopped", "complete", "completed"}:
                            active += 1
                        if state in {"waiting", "queued", "stalled"}:
                            waiting += 1
                except Exception as exc:
                    self.logger.debug(
                        "Torrent traffic snapshot skipped backend=%s error=%s",
                        backend,
                        exc,
                    )
                finally:
                    client.close()
            result = {
                "enabled": True,
                "download_speed": down,
                "upload_speed": up,
                "active": active,
                "waiting": waiting,
                "paused": 0,
                "updated_at": time.time(),
            }
            self._last_torrent_traffic = result
            return dict(result)
        finally:
            self._torrent_traffic_lock.release()

    def _paused_torrent_count(self) -> int:
        db = getattr(getattr(self, "manager", None), "db", None)
        loader = getattr(db, "downloads", None)
        if not callable(loader):
            return 0
        try:
            return sum(1 for item in loader() if not download_complete(item))
        except Exception:
            return 0

    def _resume_incomplete_torrent_jobs(self) -> int:
        db = getattr(getattr(self, "manager", None), "db", None)
        loader = getattr(db, "downloads", None)
        clients = getattr(getattr(self, "manager", None), "torrent_clients", None)
        if not callable(loader) or not callable(clients):
            return 0
        try:
            rows = [item for item in loader() if not download_complete(item)]
        except Exception:
            return 0
        if not rows:
            return 0

        resumed = 0
        for backend, client in clients():
            try:
                for item in rows:
                    torrent_hash = str(getattr(item, "torrent_hash", "") or "").strip()
                    if not torrent_hash:
                        continue
                    raw = dict(getattr(item, "raw", {}) or {})
                    declared = {
                        str(value).casefold()
                        for value in raw.get("_backends", [])
                        if str(value).strip()
                    }
                    primary = str(raw.get("backend") or "").casefold()
                    if declared and str(backend).casefold() not in declared:
                        continue
                    if not declared and primary and str(backend).casefold() != primary:
                        continue
                    try:
                        client.start(torrent_hash)
                        resumed += 1
                    except Exception as exc:
                        self.logger.debug(
                            "Torrent resume skipped backend=%s hash=%s error=%s",
                            backend,
                            torrent_hash,
                            exc,
                        )
            finally:
                client.close()
        return resumed

    def _torrent_toggle_auto_search(self) -> None:
        started = 0
        resumed = 0
        try:
            # UI-state versioning is cache bookkeeping, not part of enabling
            # torrent traffic. Keep SQLite entirely off the acknowledgement
            # path so a long maintenance transaction cannot block the button.
            try:
                self._bump_ui_state_version()
            except Exception as exc:
                self.logger.warning(
                    "RETRY step=torrent.toggle_ui_version error=%r", str(exc)
                )
            # Resume retained jobs first. This never submits a new torrent and
            # keeps an off -> on transition from duplicating unfinished work.
            resumed = int(self._resume_incomplete_torrent_jobs() or 0)
            # Discovery is follow-up work as well.
            started = int(self.manager.auto_search_current() or 0)
        except Exception as exc:
            self.logger.warning("RETRY step=torrent.toggle_start error=%r", str(exc))
        finally:
            ui_state_cache = getattr(self, "_ui_state_cache", None)
            if ui_state_cache is not None:
                ui_state_cache.invalidate()
            self.logger.info(
                "EVENT torrent.toggle_search finished resumed=%s started=%s",
                resumed,
                started,
            )

    def set_torrents_enabled(self, enabled: bool) -> dict[str, Any]:
        requested = bool(enabled)
        lock = self._torrent_state_guard()
        with lock:
            previous = bool(self._torrent_session_enabled)
            previous_authoritative = bool(getattr(self, "_torrent_session_authoritative", False))
            self._torrent_session_enabled = requested
            self._torrent_session_authoritative = True
            self.config.nyaa.torrents_enabled = requested
            manager_config = getattr(getattr(self, "manager", None), "config", None)
            if manager_config is not None:
                manager_config.nyaa.torrents_enabled = requested
            # Do not touch SQLite in the toggle RPC. Startup/maintenance can
            # hold the database for tens of seconds, which used to make
            # Torrent On look frozen. Cache invalidation is sufficient for the
            # current authoritative session state; the DB version stamp is
            # bumped in the background follow-up.
            ui_version = ""
            ui_state_cache = getattr(self, "_ui_state_cache", None)
            if ui_state_cache is not None:
                ui_state_cache.invalidate()
            self._torrent_ui_version_pending = True
        logger = getattr(self, "logger", None)
        if logger is not None:
            logger.info(
                "EVENT torrent.toggle_transition source=request previous=%s desired=%s effective=%s ui_version=%s",
                previous, requested, requested, ui_version,
            )

        quiet_result: dict[str, Any] = {}
        if not requested:
            quiet_result = self._quiesce_torrent_backends(reason="toggle_off")

        try:
            with lock:
                # Rapid clicks may supersede this invocation while Off is
                # quiescing. Persist the latest session intent, not stale input.
                persisted = bool(self._torrent_session_enabled)
                self.config.nyaa.torrents_enabled = persisted
                manager_config = getattr(getattr(self, "manager", None), "config", None)
                if manager_config is not None:
                    manager_config.nyaa.torrents_enabled = persisted
                config_path = Path(self.config.config_path).expanduser()
                if config_path.exists():
                    write_torrents_enabled(self.config, config_path)
                else:
                    # First-run/test fallback: there is no document to patch yet.
                    write_config(self.config, config_path)
        except Exception as exc:
            with lock:
                # Roll back only if no newer click has superseded this request.
                if bool(self._torrent_session_enabled) == requested:
                    self._torrent_session_enabled = previous
                    self._torrent_session_authoritative = previous_authoritative
                    self.config.nyaa.torrents_enabled = previous
                    manager_config = getattr(getattr(self, "manager", None), "config", None)
                    if manager_config is not None:
                        manager_config.nyaa.torrents_enabled = previous
                    self._torrent_ui_version_pending = True
                    ui_state_cache = getattr(self, "_ui_state_cache", None)
                    if ui_state_cache is not None:
                        ui_state_cache.invalidate()
            if logger is not None:
                logger.error(
                    "FAIL torrent.toggle_transition source=persist desired=%s restored=%s error=%r",
                    requested, previous, str(exc),
                )
            raise

        if logger is not None:
            logger.info(
                "EVENT torrent.toggle_transition source=persist desired=%s effective=%s",
                requested, persisted,
            )
        scheduled = False
        if requested and persisted and self._downloads_configured():
            supervisor = getattr(self, "task_supervisor", None)
            if supervisor is not None:
                try:
                    supervisor.start(
                        name="torrent-toggle-auto-search",
                        target=self._torrent_toggle_auto_search,
                        replace=True,
                    )
                    scheduled = True
                except Exception as exc:
                    # Discovery is follow-up work. It must not turn a persisted
                    # On acknowledgement into an RPC error/UI rollback.
                    if logger is not None:
                        logger.warning(
                            "RETRY step=torrent.toggle_schedule desired=%s error=%r",
                            requested, str(exc),
                        )
        if logger is not None:
            logger.info(
                "EVENT torrent.toggle requested=%s persisted=%s ui_version=%s search_scheduled=%s",
                requested, persisted, ui_version, scheduled,
            )
        return {
            "enabled": persisted,
            "configured": self._downloads_configured(),
            "backend": self.manager.torrent_backend_name() if self._downloads_configured() else "disabled",
            "started": 0,
            "search_scheduled": scheduled,
            "waiting": self.manager.download_intents.waiting_count(),
            "aria2_stopped": bool(quiet_result.get("aria2_stopped", False)),
            "qbittorrent_paused": int(quiet_result.get("qbittorrent_paused", 0) or 0),
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

    def _watched_import_signature(self, paths: list[Path]) -> str:
        rows: list[tuple[str, int, int]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append((str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size)))
        return hashlib.sha1(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()

    def _watched_import_changed(self, kind: str, path: Path, signature: str) -> bool:
        digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()
        return self.manager.db.get_state(f"watched_media:{kind}:{digest}", "") != signature

    def _mark_watched_import(self, kind: str, path: Path, signature: str) -> None:
        digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()
        self.manager.db.set_state(f"watched_media:{kind}:{digest}", signature)

    def scan_watched_media_folders(self) -> dict[str, int]:
        """Cheap first-pass import of mixed watched folders before Refresh work."""
        stats = {"light_novels": 0, "manga": 0, "audiobooks": 0, "files_seen": 0, "light_novels_skipped_non_japanese": 0}
        roots: list[Path] = []
        limit = max(1, int(self.config.paths.max_scanned_files or 8000))
        for configured in self.config.paths.download_dirs:
            root = Path(configured).expanduser()
            if not root.is_dir():
                continue
            resolved = root.resolve()
            if any(resolved == old or old in resolved.parents for old in roots):
                continue
            roots.append(resolved)

        remaining = limit
        for root in roots:
            if remaining <= 0:
                break
            candidates: list[Path] = []
            try:
                iterator = root.rglob("*") if self.config.library.recursive else root.iterdir()
                for path in iterator:
                    if remaining <= 0:
                        break
                    if not path.is_file():
                        continue
                    candidates.append(path)
                    remaining -= 1
            except (OSError, PermissionError):
                continue
            stats["files_seen"] += len(candidates)

            for path in candidates:
                suffix = path.suffix.casefold()
                if suffix not in {".epub", ".txt", ".cbz", ".zip"}:
                    continue
                kind = "light_novels" if suffix in {".epub", ".txt"} else "manga"
                signature = self._watched_import_signature([path])
                if not signature or not self._watched_import_changed(kind, path, signature):
                    continue
                try:
                    if kind == "light_novels":
                        if not self.light_novels.is_probably_japanese_source(path):
                            self._mark_watched_import(kind, path, signature)
                            stats["light_novels_skipped_non_japanese"] += 1
                            continue
                        self.light_novels.import_file(path, explicit=False)
                    else:
                        self.manga.import_file(path)
                    self._mark_watched_import(kind, path, signature)
                    stats[kind] += 1
                except Exception as exc:
                    self.logger.debug("Watched media import skipped kind=%s path=%s error=%s", kind, path, exc)

            audio_by_parent: dict[Path, list[Path]] = {}
            for path in candidates:
                if path.suffix.casefold() in AUDIOBOOK_EXTENSIONS:
                    audio_by_parent.setdefault(path.parent.resolve(), []).append(path.resolve())
            for parent, audio_files in audio_by_parent.items():
                audio_files.sort(key=lambda item: item.name.casefold())
                grouped = parent != root and len(audio_files) > 1
                targets = [(parent, audio_files)] if grouped else [(item, [item]) for item in audio_files]
                for target, files in targets:
                    signature = self._watched_import_signature(files)
                    if not signature or not self._watched_import_changed("audiobooks", target, signature):
                        continue
                    try:
                        if grouped:
                            self.audiobooks.import_folder(target)
                        else:
                            self.audiobooks.import_file(target)
                        self._mark_watched_import("audiobooks", target, signature)
                        stats["audiobooks"] += 1
                    except Exception as exc:
                        self.logger.debug("Watched audiobook import skipped path=%s error=%s", target, exc)
        return stats

    def refresh_local(self) -> dict[str, Any]:
        if not self._local_refresh_lock.acquire(blocking=False):
            return {"skipped": True, "stats": {}, "state": self.get_state()}
        try:
            with timed_step(self.logger, "web.refresh_local"):
                watched_stats: dict[str, int] = {}
                try:
                    with timed_step(self.logger, "watched_media.scan_local", priority="first"):
                        watched_stats = self.scan_watched_media_folders()
                except Exception as exc:
                    self.logger.warning("Watched media scan skipped: %s", exc)

                # Register watched/local anime video before release feeds, torrent
                # reconciliation and subtitle work. This intentionally happens in
                # the same cheap-first phase as LN/manga/audiobook discovery.
                local_video_rows: int | None = None
                try:
                    with timed_step(self.logger, "library.scan_local", priority="first"):
                        local_video_rows = len(self.manager.scan_library(reuse_unchanged=True, user_requested=True))
                except Exception as exc:
                    self.logger.warning("Watched video scan skipped: %s", exc)

                # The managed LN directory is also cheap to scan and follows the
                # mixed watched-folder pass before torrent/subtitle/network work.
                ln_imported = 0
                try:
                    with timed_step(self.logger, "light_novels.scan_local", priority="first"):
                        ln_imported = int(self.light_novels.scan_downloaded() or 0)
                except Exception as exc:
                    self.logger.warning("LN local scan skipped: %s", exc)
                self._schedule_light_novel_audiobook_recovery("refresh", min_interval=0.0)

                # Manual Refresh must remain interactive even if the launch agent
                # is currently aligning subtitles. Release discovery runs now;
                # expensive subtitle preparation is queued separately.
                stats = self.manager.run_interactive_refresh(
                    pre_scanned_library_count=local_video_rows
                )
                reconcile_ready = getattr(self.manager, "reconcile_prepared_subtitle_rows", None)
                repaired_ready = int(reconcile_ready() or 0) if callable(reconcile_ready) else 0
                if repaired_ready:
                    stats["prepared_state_repaired"] = repaired_ready
                for key, value in watched_stats.items():
                    if value:
                        stats[f"watched_{key}"] = int(value)
                if local_video_rows is not None:
                    stats["library"] = local_video_rows
                if ln_imported:
                    stats["light_novels_imported"] = ln_imported
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
                manager_db = getattr(self.manager, "db", None)
                queued_manual = (
                    int(manager_db.priority_subtitle_job_count(min_priority=200) or 0)
                    if manager_db is not None
                    else 0
                )
                scheduler = getattr(self.manager, "work_scheduler", None)
                if (
                    queued_manual
                    and scheduler is not None
                    and not scheduler.background_allowed()
                ):
                    stats["subtitle_waiting_for_foreground"] = queued_manual
            return {"skipped": False, "stats": stats, "state": self.get_state()}
        finally:
            self._local_refresh_lock.release()

    def refresh_all(self) -> dict[str, Any]:
        # Backwards-compatible alias. This no longer contacts AniList.
        return self.refresh_local()

    def set_anime_japanese_subtitles_required(
        self, media_id: int, required: bool
    ) -> dict[str, Any]:
        value = self.manager.set_japanese_subtitles_required(int(media_id), bool(required))
        return {
            "media_id": int(media_id),
            "japanese_subtitles_required": value,
            # The WebView applies this tiny mutation optimistically.  A full
            # state rebuild is deliberately NOT on the click path; on large
            # libraries that used to make this toggle take several seconds.
            "ui_state_version": self.manager.db.get_state("ui_state_version", ""),
        }

    # pudge-v0.7.23-sidebar-context-menus-v1
    def search_new_subtitles(self) -> dict[str, Any]:
        # Only process subtitle jobs that have never been attempted. Do not
        # reset backoff, requeue old jobs, or touch ready/watched episodes.
        now = time.time()
        fresh_jobs = []
        for row in self.manager.db.subtitle_jobs():
            try:
                attempts = int(row["attempts"] or 0)
                next_check = float(row["next_check"] or 0)
            except (TypeError, ValueError):
                continue
            if attempts != 0 or str(row["state"] or "") != "pending" or next_check > now:
                continue
            media_id = row["media_id"]
            if media_id is not None and not self.manager.japanese_subtitles_required(int(media_id)):
                continue
            path = Path(str(row["video_path"] or "")).expanduser()
            if path.is_file():
                fresh_jobs.append(row)

        preferred_paths = tuple(
            Path(str(row["video_path"])).expanduser().resolve()
            for row in fresh_jobs
        )
        processed = 0
        queued = False
        if preferred_paths:
            with maintenance_lock(
                self.config.paths.cache_dir,
                blocking=False,
            ) as maintenance_acquired:
                if maintenance_acquired:
                    with timed_step(
                        self.logger,
                        "web.subtitle_new_only",
                        videos=len(preferred_paths),
                    ):
                        processed = int(
                            self.manager.process_subtitle_jobs(
                                limit=max(1, len(preferred_paths)),
                                preferred_paths=preferred_paths,
                            )
                            or 0
                        )
                else:
                    queued = True

        return {
            "eligible": len(preferred_paths),
            "processed": processed,
            "queued": queued,
            "state": self.get_state(),
        }

    def search_new_releases(self) -> dict[str, Any]:
        # Focused manual missing-episode discovery; unlike Refresh this does not
        # scan the library or touch AniList/subtitle retry state.
        started = 0
        synced = 0
        warning = ""
        with timed_step(self.logger, "web.release_search_manual"):
            try:
                started = int(self.manager.auto_search_current() or 0)
            except Exception as exc:
                warning = str(exc)
                self.logger.warning(
                    "FAIL step=web.release_search_manual error=%r",
                    warning,
                )
            if started and self._downloads_enabled():
                try:
                    synced = int(self.manager.sync_downloads() or 0)
                except Exception as exc:
                    warning = str(exc)
                    self.logger.warning(
                        "FALLBACK step=web.release_search_sync error=%r",
                        warning,
                    )
        return {
            "started": started,
            "downloads": synced,
            "warning": warning,
            "state": self.get_state(),
        }

    def _run_startup_maintenance_background(self) -> None:
        stats: dict[str, Any] = {}
        error = ""
        try:
            try:
                with timed_step(self.logger, "startup.light_novel_audiobook_recovery", order=0):
                    recovery = self._recover_light_novel_audiobook_downloads()
                    if int(recovery.get("recovered") or 0):
                        stats["light_novel_audiobooks_recovered"] = int(recovery["recovered"])
            except Exception as exc:
                self.logger.warning("Startup LN audiobook recovery skipped: %s", exc)
            with timed_step(self.logger, "startup.phase.local_nyaa_subtitles", order=1):
                startup_stats = self.manager.run_startup_once()
                stats.update(startup_stats)
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
        if bool(getattr(getattr(self, "safe_mode", None), "active", False)):
            return {
                "skipped": True,
                "running": False,
                "done": True,
                "stats": {},
                "error": "",
                "safe_mode": True,
                "state": self.get_state_fast(),
            }
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
        if bool(getattr(getattr(self, "safe_mode", None), "active", False)):
            return {
                "skipped": True,
                "safe_mode": True,
                "stats": {},
                "state": self.get_state_fast(),
            }
        if not self._download_poll_lock.acquire(blocking=False):
            return {"skipped": True, "stats": {}, "state": self.get_state()}
        stats = {"downloads": 0, "subs": 0}
        try:
            with timed_step(self.logger, "foreground.poll", mode="downloads-only"):
                try:
                    completed_paths: tuple[Path, ...] = ()
                    try:
                        now_mono = time.monotonic()
                        last_qbt = float(getattr(self, "_last_foreground_qbt_sync", 0.0) or 0.0)
                        if now_mono - last_qbt >= 15.0:
                            with timed_step(self.logger, "foreground.qbittorrent"):
                                stats["downloads"] = self.manager.sync_downloads()
                            self._last_foreground_qbt_sync = time.monotonic()
                            completed_paths = self.manager.last_completed_video_paths
                        else:
                            stats["qbittorrent_throttled"] = 1
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
                            if not self.manager.work_scheduler.background_allowed():
                                stats["subtitle_waiting_for_foreground"] = manual_count
                                self.logger.info(
                                    "WAIT step=foreground.subtitle_manual_refresh reason=foreground_active jobs=%s",
                                    manual_count,
                                )
                            else:
                                with timed_step(
                                    self.logger,
                                    "foreground.subtitle_manual_refresh",
                                    jobs=manual_count,
                                ):
                                    stats["subs"] = self.manager.process_subtitle_jobs(
                                        limit=min(8, max(1, manual_count))
                                    )
                        elif maintenance_acquired and self.manager.db.subtitle_jobs():
                            if not self.manager.work_scheduler.background_allowed():
                                stats["subtitle_due_waiting_for_foreground"] = len(
                                    self.manager.db.subtitle_jobs()
                                )
                            else:
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

    def import_dropped_paths(self, values: list[str]) -> dict[str, Any]:
        """Route Finder drops without doing post-import network/audio work inline."""
        started_at = time.monotonic()
        paths: list[Path] = []
        for raw in values or []:
            try:
                path = Path(str(raw)).expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if path.exists() and path not in paths:
                paths.append(path)

        imports: list[dict[str, Any]] = []
        errors: list[str] = []
        image_paths: list[Path] = []
        image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

        def record(kind: str, page: str, payload: dict[str, Any], source: Path) -> None:
            row = {"kind": kind, "page": page, "source": str(source), **payload}
            imports.append(row)

        for path in paths:
            if path.is_dir():
                try:
                    audio_files = [
                        item for item in path.iterdir()
                        if item.is_file() and item.suffix.casefold() in AUDIOBOOK_EXTENSIONS
                    ]
                    # pudge-v0.7.23-nested-manga-drop-v1
                    # Manga torrents are often unpacked as pack/volume/page trees.
                    images = self.manga.discover_image_files(path, limit=50000)
                except OSError as exc:
                    errors.append(f"{path.name}: {exc}")
                    continue
                if audio_files:
                    try:
                        book = self.audiobooks.import_folder(path)
                        record("audiobook", "audiobooks", {"id": int(book["id"]), "title": str(book.get("title") or path.name), "book": book}, path)
                    except Exception as exc:
                        errors.append(f"{path.name}: {exc}")
                if images:
                    image_paths.extend(images)
                continue

            suffix = path.suffix.casefold()
            try:
                if suffix in {".epub", ".txt"}:
                    imported = self.light_novels.import_file(path, explicit=True)
                    book = self.light_novels.drop_card(int(imported["id"]))
                    book["paired_audio"] = self.audiobooks.link_for_light_novel(int(book["id"]))
                    record(
                        "light_novel",
                        "lightnovels",
                        {
                            "id": int(book["id"]),
                            "title": str(book.get("title") or path.stem),
                            "book": book,
                        },
                        path,
                    )
                    def link_later(book_id: int = int(book["id"])) -> None:
                        try:
                            self.audiobooks.auto_link_light_novel(book_id)
                        except Exception as exc:
                            self.logger.debug("Dropped LN audio auto-link skipped book_id=%s error=%s", book_id, exc)
                        irodori = self.light_novels.settings()
                        if (
                            irodori.irodori_tts_enabled
                            and irodori.irodori_tts_auto_generate
                            and self._audiobook_link_kind(book_id) == "none"
                        ):
                            try:
                                self.generate_light_novel_tts(book_id)
                            except Exception as exc:
                                self.logger.debug(
                                    "Dropped LN audiobook auto-generation skipped book_id=%s error=%s",
                                    book_id,
                                    exc,
                                )
                    timer = threading.Timer(1.5, link_later)
                    timer.daemon = True
                    timer.start()
                elif suffix in {".cbz", ".zip"}:
                    book = self.manga.import_file(path)
                    record("manga", "manga", {"id": int(book["id"]), "title": str(book.get("title") or path.stem), "book": book}, path)
                elif suffix in image_extensions:
                    image_paths.append(path)
                elif suffix in AUDIOBOOK_EXTENSIONS:
                    book = self.audiobooks.import_file(path)
                    record("audiobook", "audiobooks", {"id": int(book["id"]), "title": str(book.get("title") or path.stem), "book": book}, path)
                elif suffix in VIDEO_EXTENSIONS:
                    episode = self.manager.import_local_video(path)
                    record(
                        "anime",
                        "current",
                        {
                            "media_id": episode.media_id,
                            "episode": episode.episode,
                            "title": episode.title,
                            "video_path": str(episode.video_path),
                        },
                        path,
                    )
                else:
                    errors.append(f"{path.name}: unsupported file type")
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")

        if image_paths:
            try:
                books = self.manga.import_image_groups(image_paths)
                for book in books:
                    record(
                        "manga", "manga",
                        {
                            "id": int(book["id"]),
                            "title": str(book.get("title") or "Dropped manga"),
                            "book": book,
                        },
                        image_paths[0],
                    )
            except Exception as exc:
                errors.append(f"images: {exc}")

        result = {
            "ok": bool(imports),
            "imports": imports,
            "focus": imports[0] if imports else None,
            "errors": errors,
        }
        self.logger.info(
            "DONE step=app.drop_import duration_ms=%.1f paths=%s imports=%s errors=%s kinds=%s",
            (time.monotonic() - started_at) * 1000.0,
            len(paths),
            len(imports),
            len(errors),
            [item.get("kind") for item in imports],
        )
        return result

    def _audiobook_generation_root(self) -> Path:
        return Path(self.config.library.root_dir) / "Generated Audiobooks"

    @staticmethod
    def _audiobook_generation_manifest_path(output_dir: Path) -> Path:
        return output_dir / ".pudge-audiobook-generation.json"

    def _write_audiobook_generation_manifest(self, output_dir: Path, payload: dict[str, Any]) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        path = self._audiobook_generation_manifest_path(output_dir)
        data = {"schema": 1, **payload, "updated_at": time.time()}
        temporary = path.with_suffix(".tmp.json")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _audiobook_generation_manifest(self, book_id: int) -> dict[str, Any] | None:
        root = self._audiobook_generation_root()
        if not root.is_dir():
            return None
        best: tuple[float, dict[str, Any]] | None = None
        for path in root.glob("*/.pudge-audiobook-generation.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if not isinstance(payload, dict) or int(payload.get("book_id") or 0) != int(book_id):
                continue
            state = str(payload.get("state") or "")
            if state in {"succeeded", "cancelled"}:
                continue
            output_dir = Path(str(payload.get("output_dir") or path.parent)).expanduser()
            if not output_dir.is_dir():
                continue
            payload = dict(payload)
            payload["output_dir"] = str(output_dir)
            stamp = float(payload.get("updated_at") or 0.0)
            if best is None or stamp > best[0]:
                best = (stamp, payload)
        if best is not None:
            return best[1]

        # v92 compatibility: a process killed before v93 has no manifest yet.
        # A stale marker or chunk directory is enough to offer an explicit resume.
        suffixes = (f"[Generated {int(book_id)}]", f"[Irodori {int(book_id)}]")
        for output_dir in root.iterdir():
            if not output_dir.is_dir() or not output_dir.name.endswith(suffixes):
                continue
            if not (
                (output_dir / ".pudge-audiobook-generating").exists()
                or (output_dir / ".pudge-audiobook-parts").is_dir()
            ):
                continue
            return {
                "schema": 0,
                "book_id": int(book_id),
                "mode": "generate",
                "state": "interrupted",
                "current": 0.0,
                "total": 0.0,
                "output_dir": str(output_dir),
                "message": "Audiobook generation interrupted",
                "updated_at": output_dir.stat().st_mtime,
            }
        return None

    def _audiobook_link_kind(self, book_id: int, link: dict[str, Any] | None = None) -> str:
        link = link if link is not None else self.audiobooks.link_for_light_novel(int(book_id), include_alignment=False)
        if not link:
            return "none"
        book = link.get("book") or {}
        raw_path = str(book.get("path") or "").strip()
        if not raw_path:
            return "ordinary"
        path = Path(raw_path).expanduser()
        try:
            root = self._audiobook_generation_root().resolve()
            resolved = path.resolve()
            under_generated_root = resolved == root or root in resolved.parents
        except OSError:
            under_generated_root = self._audiobook_generation_root() in path.parents
        if under_generated_root and path.is_dir() and (path / ".pudge-audiobook-profile.json").is_file():
            return "generated"
        return "ordinary"

    def _irodori_tts_status(
        self,
        book_id: int,
        *,
        paired_audio: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        book_id = int(book_id)
        with self._irodori_tts_lock:
            job_id = str(self._irodori_tts_job_ids.get(book_id) or "")
            thread = self._irodori_tts_threads.get(book_id)
            thread_alive = bool(thread is not None and thread.is_alive())
            control = str(self._audiobook_generation_controls.get(book_id) or "")
        paired_kind = self._audiobook_link_kind(book_id, paired_audio)
        manifest = self._audiobook_generation_manifest(book_id)
        job = self.job_center.get(job_id) if job_id else None
        if thread_alive and job is not None:
            state = str(job.get("state") or "running")
            current = float(job.get("current") or 0.0)
            total = float(job.get("total") or 0.0)
            progress = max(0.0, min(1.0, current / total)) if total > 0 else 0.0
            mode = str((manifest or {}).get("mode") or job.get("payload", {}).get("mode") or "generate")
            return {
                "active": True,
                "state": "pause_requested" if control == "pause_requested" else state,
                "job_id": job_id,
                "current": current,
                "total": total,
                "progress": progress,
                "message": "Pausing…" if control == "pause_requested" else str(job.get("message") or ""),
                "error": str(job.get("error") or ""),
                "mode": mode,
                "resumable": False,
                "paired_kind": paired_kind,
            }
        if manifest is not None:
            state = str(manifest.get("state") or "interrupted")
            if state == "running":
                state = "interrupted"
            current = float(manifest.get("current") or 0.0)
            total = float(manifest.get("total") or 0.0)
            progress = max(0.0, min(1.0, current / total)) if total > 0 else 0.0
            return {
                "active": False,
                "state": state,
                "job_id": str(manifest.get("job_id") or job_id),
                "current": current,
                "total": total,
                "progress": progress,
                "message": str(manifest.get("message") or "Audiobook generation can be resumed"),
                "error": str(manifest.get("error") or ""),
                "mode": str(manifest.get("mode") or "generate"),
                "resumable": True,
                "paired_kind": paired_kind,
            }
        if job is not None and str(job.get("state") or "") == "failed":
            return {
                "active": False,
                "state": "failed",
                "job_id": job_id,
                "current": float(job.get("current") or 0.0),
                "total": float(job.get("total") or 0.0),
                "progress": float(job.get("progress") or 0.0),
                "message": str(job.get("message") or ""),
                "error": str(job.get("error") or ""),
                "mode": str(job.get("payload", {}).get("mode") or "generate"),
                "resumable": False,
                "paired_kind": paired_kind,
            }
        return {
            "active": False,
            "state": "idle",
            "progress": 0.0,
            "resumable": False,
            "paired_kind": paired_kind,
        }

    def light_novel_state(self) -> dict[str, Any]:
        self._schedule_light_novel_audiobook_recovery("state", min_interval=60.0)
        state = self.light_novels.state()
        active_irodori = False
        for book in state.get("books", []):
            book_id = int(book["id"])
            paired_audio = self.audiobooks.link_for_light_novel(book_id)
            book["paired_audio"] = paired_audio
            status = self._irodori_tts_status(book_id, paired_audio=paired_audio)
            book["irodori_tts"] = status
            active_irodori = active_irodori or bool(status.get("active"))
        state["irodori_tts_active"] = active_irodori
        return state

    def _backfill_manga_mean_scores(self, state: dict[str, Any]) -> dict[str, Any]:
        # pudge-v0.7.23-manga-mean-score-backfill-v1
        if not self.config.anilist.enabled or not self.config.anilist.access_token:
            return state
        attempted = set(getattr(self, "_manga_mean_score_attempted", set()))
        missing = sorted(
            {
                int(book["anilist_id"])
                for book in state.get("books", [])
                if book.get("anilist_id")
                and book.get("mean_score") is None
                and int(book["anilist_id"]) not in attempted
            }
        )
        if not missing:
            return state
        attempted.update(missing)
        self._manga_mean_score_attempted = attempted
        gql = "query($ids:[Int]){Page(page:1,perPage:50){media(id_in:$ids,type:MANGA){id meanScore}}}"
        try:
            data = self.light_novels._anilist_post(gql, {"ids": missing})
            scores = {
                int(media["id"]): float(media["meanScore"])
                for media in (data.get("Page") or {}).get("media") or []
                if media.get("id") is not None and media.get("meanScore") is not None
            }
            if scores and self.manga.set_mean_scores(scores):
                return self.manga.state()
        except Exception as exc:
            self.logger.warning("Manga AniList mean-score backfill skipped: %s", exc)
        return state

    def manga_state(self) -> dict[str, Any]:
        return self._backfill_manga_mean_scores(self.manga.state())

    def manga_remove_series(self, book_id: int) -> dict[str, Any]:
        removed = self.manga.remove_series(int(book_id))
        return {"removed": removed}

    def manga_remove_books(self, book_ids: list[int]) -> dict[str, Any]:
        removed = self.manga.remove_books([int(value) for value in (book_ids or [])])
        return {"removed": removed}

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
        query($search:String!){Page(page:1,perPage:12){media(search:$search,type:MANGA,sort:SEARCH_MATCH){id format chapters volumes status meanScore title{userPreferred romaji english native}coverImage{large}siteUrl mediaListEntry{status progress progressVolumes score(format:POINT_10)}}}}
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
                    "mean_score": float(media.get("meanScore")) if media.get("meanScore") is not None else None,
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
            mean_score=item.get("mean_score"),
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
        live_processed_raw = state.get("processed_pages", state.get("cached_pages"))
        state.update(cache)
        state["running"] = running
        if cache["complete"] and not running:
            state["state"] = "ready"

        total = max(0, int(cache.get("total_pages") or 0))
        completed = max(0, min(total, int(cache.get("completed_pages") or 0)))
        try:
            live_processed = (
                max(0, min(total, int(live_processed_raw)))
                if live_processed_raw is not None
                else completed
            )
        except (TypeError, ValueError):
            live_processed = completed
        processed = max(completed, live_processed) if running else completed
        failed = max(0, min(total - completed, int(cache.get("failed_pages") or 0)))
        pending = max(0, total - completed - failed)
        state_name = str(state.get("state") or "idle")
        current_raw = state.get("page_index")
        try:
            current_index = int(current_raw) if current_raw is not None else None
        except (TypeError, ValueError):
            current_index = None

        processing = 0
        queued = 0
        if running and state_name == "queued":
            queued = pending
        elif running and state_name == "running":
            processing = 1 if pending > 0 and current_index is not None else 0
            queued = max(0, pending - processing)
        # ``parsing`` is post-OCR Jiten preparation. It must not make a persisted
        # OCR page look queued/processing again.
        not_started = max(0, total - completed - failed - queued - processing)
        state.update(
            {
                "completed": completed,
                "processed_pages": processed,
                "queued": queued,
                "processing": processing,
                "failed": failed,
                "not_started": not_started,
                "current_page_index": current_index if processing else None,
                "current_page": (current_index + 1) if processing and current_index is not None else None,
                "page_state_consistent": (
                    completed + queued + processing + failed + not_started == total
                ),
            }
        )
        return state

    def _run_manga_book_ocr(self, book_id: int) -> None:
        def on_progress(
            done: int,
            total: int,
            page_index: int | None,
            phase: str = "ocr",
        ) -> None:
            phase_name = str(phase or "ocr")
            with self._manga_book_ocr_lock:
                previous = dict(self._manga_book_ocr_state.get(book_id, {}))
                previous_processed = max(
                    0, int(previous.get("processed_pages") or previous.get("cached_pages") or 0)
                )
                previous_prepared = max(
                    previous_processed,
                    int(previous.get("prepared_pages") or previous_processed),
                )
                if phase_name == "detecting":
                    processed = previous_processed
                    prepared = max(previous_prepared, int(done))
                    cached = max(0, int(previous.get("cached_pages") or 0))
                    state_name = "preparing"
                else:
                    processed = max(previous_processed, int(done))
                    prepared = max(previous_prepared, processed)
                    cached = int(done)
                    state_name = "running"
                self._manga_book_ocr_state[book_id] = {
                    "state": state_name,
                    "phase": phase_name,
                    "running": True,
                    "cached_pages": cached,
                    "processed_pages": processed,
                    "prepared_pages": prepared,
                    "total_pages": int(total),
                    "page_index": page_index,
                    "errors": [],
                }
            self.logger.info(
                "EVENT manga_ocr.progress book_id=%s phase=%s processed=%s prepared=%s total=%s current_page=%s",
                book_id,
                phase_name,
                processed,
                prepared,
                int(total),
                (int(page_index) + 1) if page_index is not None else None,
            )
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
            while True:
                result = self.manga.ocr_book(
                    book_id,
                    progress=on_progress,
                    cancelled=cancel_event.is_set if cancel_event is not None else None,
                )
                if not result.get("preempted"):
                    break
                self.logger.info(
                    "YIELD step=manga_ocr.book book_id=%s reason=higher_priority_user_work cached=%s",
                    book_id,
                    result.get("cached_pages", 0),
                )
                with self._manga_book_ocr_lock:
                    previous = dict(self._manga_book_ocr_state.get(book_id, {}))
                    self._manga_book_ocr_state[book_id] = {
                        **previous,
                        **result,
                        "state": "queued",
                        "phase": "yielded",
                        "running": True,
                    }
                if cancel_event is not None and cancel_event.is_set():
                    result = {**result, "cancelled": True, "preempted": False}
                    break
                time.sleep(0.05)
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
            raise RuntimeError("MangaOCR is not installed. Install it from Settings → Essential.")
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
                    resumable=True,
                    correlation_id=f"manga-ocr:{book_id}",
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
                directory=str(Path.home()),
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

    def choose_manga_folder(self) -> dict[str, Any]:
        # Import all manga content recursively under one selected folder.
        if self.window is None:
            return {"cancelled": True}
        try:
            import webview

            result = self.window.create_file_dialog(
                webview.FOLDER_DIALOG,
                directory=str(Path.home() / "Downloads"),
                allow_multiple=False,
            )
        except Exception as exc:
            return {"cancelled": True, "error": str(exc)}
        if not result:
            return {"cancelled": True}

        raw = result[0] if isinstance(result, (list, tuple)) else result
        source_root = Path(str(raw)).expanduser().resolve()
        if not source_root.is_dir():
            return {"cancelled": False, "error": "Selected manga folder does not exist"}

        # pudge-v0.7.23-manga-folder-picker-v1
        image_paths = self.manga.discover_image_files(source_root, limit=50000)
        archive_paths = sorted(
            [
                path.resolve()
                for path in source_root.rglob("*")
                if path.is_file() and path.suffix.casefold() in {".cbz", ".zip"}
            ],
            key=lambda path: str(path).casefold(),
        )

        job_id = self.job_center.start(
            "import",
            "Import manga folder",
            payload={
                "media_kind": "manga_folder",
                "paths": [str(source_root)],
                "image_count": len(image_paths),
                "archive_count": len(archive_paths),
            },
            total=max(1, len(archive_paths) + (1 if image_paths else 0)),
        )
        cancel_event = threading.Event()
        self._import_cancel_events[job_id] = cancel_event
        books: list[dict[str, Any]] = []
        errors: list[str] = []
        current = 0

        try:
            if image_paths and not cancel_event.is_set():
                try:
                    books.extend(
                        self.manga.import_image_groups(
                            image_paths,
                            source_root=source_root,
                        )
                    )
                except Exception as exc:
                    errors.append(f"images: {exc}")
                current += 1
                self.job_center.update(
                    job_id,
                    state="running",
                    current=current,
                    total=max(1, len(archive_paths) + 1),
                    message=f"Imported {len(books)} manga books",
                )

            for archive in archive_paths:
                if cancel_event.is_set():
                    break
                try:
                    books.append(self.manga.import_file(archive))
                except Exception as exc:
                    errors.append(f"{archive.name}: {exc}")
                current += 1
                self.job_center.update(
                    job_id,
                    state="running",
                    current=current,
                    total=max(1, len(archive_paths) + (1 if image_paths else 0)),
                    message=f"Imported {len(books)} manga books",
                )
        finally:
            self._import_cancel_events.pop(job_id, None)

        if cancel_event.is_set():
            self.job_center.cancelled(job_id)
            return {"cancelled": True, "books": books, "errors": errors, "state": self.manga.state()}

        if books:
            self.job_center.finish(
                job_id,
                message=f"Imported {len(books)} manga books; errors {len(errors)}",
                result={
                    "book_ids": [int(book["id"]) for book in books],
                    "errors": errors,
                    "folder": str(source_root),
                },
            )
        else:
            message = "No manga images or CBZ/ZIP archives found in the selected folder"
            if errors:
                message = " • ".join(errors)
            self.job_center.fail(job_id, message)
            if not errors:
                errors.append(message)

        return {
            "cancelled": False,
            "books": books,
            "errors": errors,
            "folder": str(source_root),
            "state": self.manga.state(),
        }

    def manga_page(self, book_id: int, page_index: int) -> dict[str, Any]:
        return self.manga.page(int(book_id), int(page_index))

    def manga_set_position(self, book_id: int, page_index: int) -> dict[str, Any]:
        self.manga.set_position(int(book_id), int(page_index))
        return {"book_id": int(book_id), "page_index": int(page_index)}

    def manga_reset_progress(self, book_id: int) -> dict[str, Any]:
        return self.manga.reset_progress(int(book_id))

    def manga_reset_progress_many(self, book_ids: list[int]) -> dict[str, Any]:
        books = [self.manga.reset_progress(int(book_id)) for book_id in book_ids]
        return {"ok": True, "books": books}

    def manga_mark_read(self, book_id: int, page_index: int) -> dict[str, Any]:
        return self.manga.mark_read(int(book_id), int(page_index))

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

    def manga_ocr_artifact(self, book_id: int) -> dict[str, Any]:
        return self.manga.ocr_artifact(int(book_id))

    def manga_export_ocr_debug(
        self, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        source = dict(payload) if isinstance(payload, dict) else {}
        book = source.get("book") if isinstance(source.get("book"), dict) else {}
        book_id = int(book.get("id") or 0)
        page_index = int(source.get("page_index") or 0)
        if book_id <= 0:
            raise ValueError("Missing manga book id")

        page = self.manga.page(book_id, page_index)
        backend = self.manga.text_regions(
            book_id,
            page_index,
            refresh=False,
            cached_only=True,
        )
        raw_events = source.get("events")
        source["events"] = (
            [dict(item) for item in raw_events[-600:] if isinstance(item, dict)]
            if isinstance(raw_events, list)
            else []
        )

        stamp = time.strftime("%Y%m%d-%H%M%S")
        suffix = int(time.time_ns() % 1_000_000)
        output_root = debug_log_dir() / f"{stamp}-{suffix:06d}-manga-ocr"
        output_root.mkdir(parents=True, exist_ok=True)

        data_uri = str(page.get("data_uri") or "")
        image_name = "page.bin"
        if data_uri.startswith("data:") and "," in data_uri:
            header, encoded = data_uri.split(",", 1)
            mime = header[5:].split(";", 1)[0].lower()
            extension = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/jpg": ".jpg",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }.get(mime, ".bin")
            image_name = f"page{extension}"
            image_bytes = (
                base64.b64decode(encoded)
                if ";base64" in header
                else encoded.encode("utf-8")
            )
            (output_root / image_name).write_bytes(image_bytes)

        page_meta = {key: value for key, value in page.items() if key != "data_uri"}
        snapshot = {
            "schema": 1,
            "generated_at": time.time(),
            "pudge_version": __version__,
            "page": page_meta,
            "backend_ocr": backend,
            "frontend": source,
        }
        (output_root / "snapshot.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

        backend_regions = [
            dict(item)
            for item in backend.get("regions", [])
            if isinstance(item, dict)
        ]
        frames = source.get("frames") if isinstance(source.get("frames"), list) else []
        current_frame = next(
            (
                item
                for item in frames
                if isinstance(item, dict)
                and int(item.get("page_index") or -1) == page_index
            ),
            frames[0] if frames and isinstance(frames[0], dict) else {},
        )
        frontend_regions = (
            current_frame.get("overlays")
            if isinstance(current_frame, dict)
            and isinstance(current_frame.get("overlays"), list)
            else []
        )

        def backend_boxes() -> str:
            rows: list[str] = []
            for index, region in enumerate(backend_regions):
                x = max(0.0, min(1.0, float(region.get("x") or 0.0)))
                y = max(0.0, min(1.0, float(region.get("y") or 0.0)))
                width = max(0.0, min(1.0 - x, float(region.get("width") or 0.0)))
                height = max(0.0, min(1.0 - y, float(region.get("height") or 0.0)))
                top = max(0.0, 1.0 - y - height)
                title = html.escape(
                    f"{index}: {region.get('orientation') or ''} "
                    f"{region.get('confidence') or ''} {region.get('text') or ''}"
                )
                rows.append(
                    f'<div class="box backend" title="{title}" '
                    f'style="left:{x * 100:.5f}%;top:{top * 100:.5f}%;'
                    f'width:{width * 100:.5f}%;height:{height * 100:.5f}%">'
                    f'<b>B{index}</b></div>'
                )
            return "".join(rows)

        def frontend_boxes() -> str:
            rows: list[str] = []
            for index, overlay in enumerate(frontend_regions):
                if not isinstance(overlay, dict):
                    continue
                normalized = overlay.get("normalized_to_image")
                if not isinstance(normalized, dict):
                    continue
                left = float(normalized.get("left") or 0.0)
                top = float(normalized.get("top") or 0.0)
                width = float(normalized.get("width") or 0.0)
                height = float(normalized.get("height") or 0.0)
                title = html.escape(
                    f"{overlay.get('region_index', index)}: "
                    f"{overlay.get('parsed_token_count', 0)} tokens "
                    f"{overlay.get('text') or ''}"
                )
                rows.append(
                    f'<div class="box frontend" title="{title}" '
                    f'style="left:{left * 100:.5f}%;top:{top * 100:.5f}%;'
                    f'width:{width * 100:.5f}%;height:{height * 100:.5f}%">'
                    f'<b>F{overlay.get("region_index", index)}</b></div>'
                )
            return "".join(rows)

        image_ref = html.escape(image_name)
        overlay_html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Pudge Manga OCR debug</title>
<style>
body{{margin:0;padding:20px;background:#10151d;color:#e8eef6;font:14px -apple-system,BlinkMacSystemFont,sans-serif}}
h1{{font-size:18px}}.grid{{display:grid;grid-template-columns:repeat(2,minmax(320px,1fr));gap:20px}}
.panel{{min-width:0}}.canvas{{position:relative;display:inline-block;max-width:100%}}
.canvas img{{display:block;max-width:100%;height:auto}}.box{{position:absolute;box-sizing:border-box;pointer-events:none}}
.box b{{position:absolute;left:0;top:0;padding:1px 3px;background:#111;color:#fff;font-size:11px}}
.backend{{border:2px solid #ff5b66;background:rgba(255,91,102,.08)}}
.frontend{{border:2px solid #57a5ff;background:rgba(87,165,255,.08)}}
code{{color:#a8d1ff}}@media(max-width:900px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body>
<h1>Pudge Manga OCR debug</h1>
<p>Book <code>{book_id}</code>, page <code>{page_index + 1}</code>. Red: backend OCR geometry. Blue: actual browser overlay.</p>
<div class="grid">
<section class="panel"><h2>Backend regions</h2><div class="canvas"><img src="{image_ref}">{backend_boxes()}</div></section>
<section class="panel"><h2>Rendered overlay</h2><div class="canvas"><img src="{image_ref}">{frontend_boxes()}</div></section>
</div>
<p>See <code>snapshot.json</code> for reading order, confidence, DOM sizes, selections and click hit-testing.</p>
</body></html>"""
        (output_root / "overlay.html").write_text(overlay_html, encoding="utf-8")

        archive = Path(
            shutil.make_archive(
                str(output_root),
                "zip",
                root_dir=output_root,
            )
        )
        try:
            subprocess.Popen(["open", "-R", str(archive)])
        except OSError:
            pass
        return {
            "ok": True,
            "path": str(archive),
            "folder": str(output_root),
            "event_count": len(source["events"]),
            "backend_region_count": len(backend_regions),
            "frontend_region_count": len(frontend_regions),
        }


    def audiobook_state(self) -> dict[str, Any]:
        return self.audiobooks.state()


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
                        self.audiobooks.import_folder_collection(path)
                    elif media_kind == "audiobook":
                        self.audiobooks.import_file(path)
                    else:
                        book = self.light_novels.import_file(path)
                        self.audiobooks.auto_link_light_novel(int(book["id"]))
                        irodori = self.light_novels.settings()
                        if (
                            irodori.irodori_tts_enabled
                            and irodori.irodori_tts_auto_generate
                            and self._audiobook_link_kind(int(book["id"])) == "none"
                        ):
                            self.generate_light_novel_tts(int(book["id"]))
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
        selected_folder = Path(str(raw))
        try:
            targets = self.audiobooks.folder_import_targets(selected_folder)
        except Exception as exc:
            return {"cancelled": False, "error": str(exc), "state": self.audiobooks.state()}
        job_id = self.job_center.start(
            "import",
            "Import audiobook folder",
            payload={"media_kind": "audiobook_folder", "paths": [str(raw)]},
            total=max(1, len(targets)),
        )
        cancel_event = threading.Event()
        self._import_cancel_events[job_id] = cancel_event
        books: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, target in enumerate(targets, 1):
            if cancel_event.is_set():
                break
            try:
                books.append(self.audiobooks.import_folder(target))
            except Exception as exc:
                errors.append(f"{target.name}: {exc}")
            self.job_center.update(
                job_id,
                state="running",
                current=index,
                total=len(targets),
                message=f"Imported {len(books)}/{len(targets)}",
            )
        self._import_cancel_events.pop(job_id, None)
        if cancel_event.is_set():
            self.job_center.cancelled(job_id)
            return {"cancelled": True, "books": books, "errors": errors, "state": self.audiobooks.state()}
        if not books:
            self.job_center.fail(job_id, " • ".join(errors) or "Import failed")
            return {"cancelled": False, "errors": errors, "error": " • ".join(errors) or "Import failed", "state": self.audiobooks.state()}
        self.job_center.finish(
            job_id,
            message=f"Imported {len(books)} audiobook{'s' if len(books) != 1 else ''}",
            result={"book_ids": [int(book["id"]) for book in books], "errors": errors},
        )
        payload = {"cancelled": False, "books": books, "errors": errors, "state": self.audiobooks.state()}
        if len(books) == 1:
            payload["book"] = books[0]
        return payload

    def audiobook_play(self, book_id: int, start: float | None = None, speed: float = 1.0) -> dict[str, Any]:
        return self.audiobooks.play(int(book_id), None if start is None else float(start), float(speed or 1.0))

    def audiobook_set_speed(self, book_id: int, speed: float) -> dict[str, Any]:
        # Point actions return only the changed book. Rebuilding a 200-book
        # library after every click made audiobook controls visibly stall.
        return self.audiobooks.set_speed(int(book_id), float(speed or 1.0))

    def audiobook_seek(self, book_id: int, seconds: float) -> dict[str, Any]:
        return self.audiobooks.seek(int(book_id), float(seconds))

    def audiobook_seek_to(self, book_id: int, position: float) -> dict[str, Any]:
        return self.audiobooks.seek_to(int(book_id), float(position))

    def audiobook_stop(self, book_id: int) -> dict[str, Any]:
        return self.audiobooks.stop(int(book_id))

    def audiobook_set_paused(self, book_id: int, paused: bool) -> dict[str, Any]:
        # Pause/resume is latency-sensitive. Building the complete audiobook
        # library state can take more than a second and lets mpv advance while
        # the reader UI is blocked. The paired-reader poll fetches its focused
        # state separately.
        return self.audiobooks.set_paused(int(book_id), bool(paused))

    def audiobook_sleep_timer(self, book_id: int, mode: str) -> dict[str, Any]:
        value = str(mode or "off").strip().lower()
        if value == "chapter":
            result = self.audiobooks.set_sleep_timer(int(book_id), end_of_chapter=True)
        elif value in {"15", "30", "45", "60"}:
            result = self.audiobooks.set_sleep_timer(int(book_id), seconds=int(value) * 60)
        else:
            result = self.audiobooks.set_sleep_timer(int(book_id))
        return result

    def audiobook_add_bookmark(self, book_id: int, title: str = "") -> dict[str, Any]:
        return self.audiobooks.add_bookmark(int(book_id), str(title or ""))

    def audiobook_delete_bookmark(self, bookmark_id: int) -> dict[str, Any]:
        return self.audiobooks.delete_bookmark(int(bookmark_id))

    def audiobook_restore_bookmark(
        self,
        book_id: int,
        position: float,
        title: str,
        sort_order: int,
        created_at: float,
        bookmark_id: int | None = None,
    ) -> dict[str, Any]:
        result = self.audiobooks.restore_bookmark(
            int(book_id),
            float(position),
            str(title or ""),
            int(sort_order),
            float(created_at),
            int(bookmark_id) if bookmark_id is not None else None,
        )
        return result

    def audiobook_rename_bookmark(self, bookmark_id: int, title: str) -> dict[str, Any]:
        return self.audiobooks.rename_bookmark(int(bookmark_id), str(title or ""))

    def audiobook_reorder_bookmarks(self, book_id: int, bookmark_ids: list[int]) -> dict[str, Any]:
        result = self.audiobooks.reorder_bookmarks(
            int(book_id), [int(value) for value in bookmark_ids]
        )
        return result

    def audiobook_reveal_source(self, book_id: int) -> dict[str, Any]:
        book = self.audiobooks.book(int(book_id))
        source = Path(str(book.get("path") or "")).expanduser()
        if not source.exists():
            return {"ok": False, "error": "Audiobook source does not exist"}
        if sys.platform == "darwin":
            if source.is_dir():
                subprocess.Popen(["open", str(source.resolve())])
            else:
                subprocess.Popen(["open", "-R", str(source.resolve())])
        return {"ok": True, "path": str(source.resolve())}

    def audiobook_mark_finished(self, book_id: int, finished: bool = True) -> dict[str, Any]:
        return self.audiobooks.mark_finished(int(book_id), bool(finished))

    def audiobook_delete(self, book_id: int) -> dict[str, Any]:
        return self.audiobooks.delete(int(book_id), delete_files=False)

    def audiobook_delete_many(self, book_ids: list[int]) -> dict[str, Any]:
        return self.audiobooks.delete_many([int(value) for value in book_ids], delete_files=False)

    def light_novel_audiobook_candidates(self, book_id: int, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        return self.audiobooks.link_candidates_for_light_novel(int(book_id), str(query or ""), int(limit or 50))

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

    def light_novel_audio_alignment_report(self, book_id: int) -> dict[str, Any]:
        return self.audiobooks.alignment_report(int(book_id))

    def light_novel_reprocess_audio_alignment(
        self, book_id: int, clear_transcription: bool = False
    ) -> dict[str, Any]:
        return self.audiobooks.reprocess_alignment(
            int(book_id),
            clear_transcription=bool(clear_transcription),
        )


    def light_novel_export_audio_sync_trace(
        self, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        source = dict(payload) if isinstance(payload, dict) else {}
        raw_events = source.get("events")
        events = (
            [dict(item) for item in raw_events[-3000:] if isinstance(item, dict)]
            if isinstance(raw_events, list)
            else []
        )
        source["events"] = events
        output_dir = debug_log_dir()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        suffix = int(time.time_ns() % 1_000_000)
        output = output_dir / f"pudge-ln-audio-sync-{stamp}-{suffix:06d}.json"
        snapshot = {
            "schema": 1,
            "generated_at": time.time(),
            "pudge_version": __version__,
            "trace": source,
        }
        output.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        try:
            subprocess.Popen(["open", "-R", str(output)])
        except OSError:
            pass
        return {"ok": True, "path": str(output), "event_count": len(events)}

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
        return self.translate_text(text, context, target_language, media_id)

    def choose_light_novel_file(self) -> dict[str, Any]:
        if self.window is None:
            return {"cancelled": True}
        try:
            import webview
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                directory=str(Path.home()),
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
                irodori = self.light_novels.settings()
                if (
                    irodori.irodori_tts_enabled
                    and irodori.irodori_tts_auto_generate
                    and self._audiobook_link_kind(int(book["id"])) == "none"
                ):
                    self.generate_light_novel_tts(int(book["id"]))
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
        started_at = time.monotonic()
        metadata_started = time.monotonic()
        book = self.light_novels.open_book(int(book_id))
        metadata_ms = (time.monotonic() - metadata_started) * 1000.0
        audio_started = time.monotonic()
        book["paired_audio"] = self.audiobooks.link_for_light_novel(
            int(book_id), include_alignment=False, include_transcription=False
        )
        audio_ms = (time.monotonic() - audio_started) * 1000.0
        total_ms = (time.monotonic() - started_at) * 1000.0
        # Preserve the long-standing first log record for existing tests/tools,
        # then emit the richer timing breakdown as a second record.
        self.logger.info(
            "LN open book=%s chapters=%s paired=%s elapsed=%.3fs",
            book_id, len(book.get("chapters") or []), bool(book.get("paired_audio")),
            total_ms / 1000.0,
        )
        self.logger.info(
            "TIMING step=ln.open book=%s chapters=%s paired=%s metadata_ms=%.1f paired_audio_ms=%.1f total_ms=%.1f",
            book_id, len(book.get("chapters") or []), bool(book.get("paired_audio")),
            metadata_ms, audio_ms, total_ms,
        )
        return book

    def _schedule_ln_reader_parse_alignment_refresh(self, book_id: int, chapter_index: int) -> None:
        supervisor = getattr(self, "task_supervisor", None)
        if supervisor is None:
            return
        supervisor.start(
            name=f"ln-reader-parse-align-{int(book_id)}-{int(chapter_index)}",
            target=self.audiobooks.maybe_refresh_alignment_after_reader_parse,
            args=(int(book_id), int(chapter_index)),
            replace=False,
        )

    def light_novel_chapter(self, book_id: int, chapter_index: int) -> dict[str, Any]:
        # maybe_refresh_alignment_after_reader_parse is deliberately scheduled,
        # never awaited on the reader's first-paint response path.
        started_at = time.monotonic()
        payload = self.light_novels.chapter_fast(int(book_id), int(chapter_index))
        chapter_ms = (time.monotonic() - started_at) * 1000.0
        if not bool(payload.get("parsing")) and (payload.get("tokens") or []):
            try:
                self._schedule_ln_reader_parse_alignment_refresh(int(book_id), int(chapter_index))
            except Exception as exc:
                self.logger.warning("LN audio reader-parse alignment refresh schedule failed: %s", exc)
        self.logger.info(
            "TIMING step=ln.chapter book=%s chapter=%s parsing=%s tokens=%s response_ms=%.1f",
            book_id, chapter_index, bool(payload.get("parsing")),
            sum(len(row or []) for row in (payload.get("tokens") or [])), chapter_ms,
        )
        return payload

    def light_novel_chapter_parse_status(self, book_id: int, chapter_index: int) -> dict[str, Any]:
        # maybe_refresh_alignment_after_reader_parse stays off the poll response path.
        started_at = time.monotonic()
        payload = self.light_novels.chapter_parse_status(int(book_id), int(chapter_index))
        ready_payload = payload.get("payload") if isinstance(payload, dict) else None
        if bool(payload.get("ready")) and isinstance(ready_payload, dict) and (ready_payload.get("tokens") or []):
            try:
                self._schedule_ln_reader_parse_alignment_refresh(int(book_id), int(chapter_index))
            except Exception as exc:
                self.logger.warning("LN audio reader-parse alignment refresh schedule failed: %s", exc)
        self.logger.info(
            "TIMING step=ln.chapter_parse_status book=%s chapter=%s ready=%s response_ms=%.1f",
            book_id, chapter_index, bool(payload.get("ready")),
            (time.monotonic() - started_at) * 1000.0,
        )
        return payload

    def light_novel_stop_paired(self, book_id: int) -> dict[str, Any]:
        return self.audiobooks.stop_for_light_novel(int(book_id))

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
        progress = self.light_novels.progress_summary(int(book_id))
        return {
            "ok": True,
            "bookmark": bookmark,
            **progress,
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
        # Keep the bridge call tiny. The frontend removes the card optimistically;
        # audiobook unlinking is allowed to finish after the durable LN tombstone
        # and row deletion are committed.
        result = self.light_novels.delete_book(int(book_id), delete_file=False)
        threading.Thread(
            target=self.audiobooks.unlink_light_novel,
            args=(int(book_id),),
            name=f"ln-unlink-delete-{int(book_id)}",
            daemon=True,
        ).start()
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

    def _speaker_markup_series_books(self, book_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        anchor = self.light_novels.book(int(book_id))
        series_key = str(anchor.get("series_key") or f"book:{int(book_id)}")
        books: list[dict[str, Any]] = []
        for row in self.light_novels.books():
            try:
                full = self.light_novels.book(int(row["id"]))
            except Exception:
                continue
            if str(full.get("series_key") or f"book:{int(full['id'])}") == series_key:
                books.append(full)
        if not books:
            books = [anchor]
        books.sort(key=lambda item: (int(item.get("volume") or 0), int(item.get("id") or 0)))
        return anchor, books

    @staticmethod
    def _speaker_markup_chapter_digest(text: str) -> str:
        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()

    def export_light_novel_speaker_markup(self, book_id: int) -> dict[str, Any]:
        """Export all currently imported volumes of one LN series for ChatGPT markup."""
        anchor, books = self._speaker_markup_series_books(int(book_id))
        series_key = str(anchor.get("series_key") or f"book:{int(book_id)}")
        series_title = str(anchor.get("series_title") or anchor.get("title") or "Light Novel")
        context = self._audiobook_speaker_cache_context(int(anchor["id"]))
        profiles = self._read_audiobook_speaker_profiles(Path(context["profiles_path"]))
        global_profiles = self._read_audiobook_character_profiles(self._audiobook_character_profiles_path())
        anilist_id = next((int(item["anilist_id"]) for item in books if item.get("anilist_id")), None)
        known_characters: list[dict[str, Any]] = []
        if anilist_id:
            try:
                known_characters = self.light_novels.character_glossary(anilist_id)
            except Exception as exc:
                self.logger.warning("Speaker markup glossary unavailable media_id=%s: %s", anilist_id, exc)

        known_character_ids: set[int] = set()
        canonical_by_id: dict[int, str] = {}
        for row in known_characters:
            try:
                character_id = int(row.get("character_id") or 0)
            except (TypeError, ValueError):
                continue
            if character_id <= 0:
                continue
            known_character_ids.add(character_id)
            canonical = re.sub(r"\s+", " ", str(row.get("preferred") or row.get("source") or "")).strip()
            if canonical:
                canonical_by_id.setdefault(character_id, canonical)
        exported_character_profiles = {
            str(character_id): {
                "name": str(global_profiles[character_id].get("name") or canonical_by_id.get(character_id, "")),
                "caption": str(global_profiles[character_id].get("caption") or ""),
            }
            for character_id in sorted(known_character_ids)
            if character_id in global_profiles and str(global_profiles[character_id].get("caption") or "").strip()
        }

        manifest_books: list[dict[str, Any]] = []
        output_books: list[dict[str, Any]] = []
        chapter_files: dict[str, bytes] = {}
        chapter_count = 0
        dialogue_count = 0
        for book in books:
            source = self.light_novels.tts_source(int(book["id"]))
            manifest_chapters: list[dict[str, Any]] = []
            output_chapters: list[dict[str, Any]] = []
            volume = int(book.get("volume") or source.get("volume") or 0)
            for chapter in source.get("chapters") or []:
                chapter_index = int(chapter.get("index") or 0)
                text = str(chapter.get("text") or "")
                units = self._audiobook_voice_units(text)
                digest = self._speaker_markup_chapter_digest(text)
                dialogue_ids = [int(unit["id"]) for unit in units if bool(unit.get("dialogue"))]
                narration_ids = [int(unit["id"]) for unit in units if not bool(unit.get("dialogue"))]
                dialogue_count += len(dialogue_ids)
                chapter_count += 1
                rel = f"books/volume-{volume:03d}-book-{int(book['id'])}/chapter-{chapter_index:04d}.json"
                chapter_payload = {
                    "schema": 1,
                    "book_id": int(book["id"]),
                    "book_title": str(book.get("title") or source.get("title") or ""),
                    "volume": volume,
                    "chapter_index": chapter_index,
                    "chapter_title": str(chapter.get("title") or ""),
                    "source_sha256": digest,
                    "unit_count": len(units),
                    "dialogue_ids": dialogue_ids,
                    "narration_ids": narration_ids,
                    "segments": [
                        {
                            "id": int(unit["id"]),
                            "kind": "dialogue" if bool(unit.get("dialogue")) else "narration",
                            "text": str(unit.get("text") or ""),
                        }
                        for unit in units
                    ],
                }
                chapter_files[rel] = json.dumps(chapter_payload, ensure_ascii=False, indent=2).encode("utf-8")
                manifest_chapters.append(
                    {
                        "chapter_index": chapter_index,
                        "title": str(chapter.get("title") or ""),
                        "source_sha256": digest,
                        "unit_count": len(units),
                        "dialogue_count": len(dialogue_ids),
                        "path": rel,
                    }
                )
                output_chapters.append(
                    {
                        "chapter_index": chapter_index,
                        "source_sha256": digest,
                        "assignments": [],
                    }
                )
            manifest_books.append(
                {
                    "book_id": int(book["id"]),
                    "title": str(book.get("title") or source.get("title") or ""),
                    "volume": volume,
                    "chapters": manifest_chapters,
                }
            )
            output_books.append(
                {
                    "book_id": int(book["id"]),
                    "title": str(book.get("title") or source.get("title") or ""),
                    "volume": volume,
                    "chapters": output_chapters,
                }
            )

        manifest = {
            "schema": 1,
            "kind": "pudge-speaker-markup-source",
            "series_key": series_key,
            "series_title": series_title,
            "anilist_id": anilist_id,
            "books": manifest_books,
        }
        output_template = {
            "schema": 1,
            "kind": "pudge-speaker-annotations",
            "series_key": series_key,
            "series_title": series_title,
            "profiles": profiles,
            "character_profiles": exported_character_profiles,
            "books": output_books,
        }
        instructions = f"""# Pudge character-voice markup\n\nYou are preparing speaker annotations for Irodori TTS.\n\nSeries: {series_title}\nSeries key: {series_key}\nVolumes in this archive: {len(books)}\nChapters: {chapter_count}\nDialogue spans: {dialogue_count}\n\n## Task\n\n1. Read the chapter JSON files in `books/`. They contain the full LN context split into exact numbered narration/dialogue spans.\n2. For every `dialogue` span, infer the most likely canonical speaker. Use `unknown` when the text does not support a reliable identification.\n3. Also annotate a `narration` span when it is clearly voiced by a specific character: first-person narration, internal monologue, thoughts, remembered speech, or another character-owned viewpoint. Leave neutral third-person narration unassigned so Pudge keeps the default narrator voice.\n4. NEVER return or rewrite the LN text. Output only ids, speaker identity, and voice captions.\n5. `known_characters.json` contains AniList aliases with stable `character_id` values. When a speaker matches one of them, return `anilist_character_id` in the assignment. This id is the PRIMARY voice identity and is shared across different LN series/sequels.\n6. Reuse every matching caption in `character_profiles.json` EXACTLY for the same AniList character id. Never redesign an existing AniList character voice.\n7. `profiles.json` contains series-scoped fallback profiles for speakers that cannot be resolved to AniList. Reuse those captions exactly by canonical speaker name.\n8. For a newly identified named speaker with no existing profile, create one short stable Japanese Irodori voice-design caption describing persistent identity (approximate age, gender presentation when supported, pitch/timbre, habitual speaking character). Do not encode temporary emotion or line content.\n9. Prefer canonical names/aliases from `known_characters.json` when they match. Do not invent an AniList id if the match is uncertain.\n10. Fill `OUTPUT_TEMPLATE.json`. Preserve `book_id`, `chapter_index`, and `source_sha256`, and add dialogue rows like `{{\"id\": 7, \"speaker\": \"Name\", \"anilist_character_id\": 12345, \"caption\": \"...\"}}`. Omit `anilist_character_id` for unresolved speakers.\n11. Put new/reused AniList-id profiles in top-level `character_profiles`; put unresolved series-only profiles in top-level `profiles`.\n12. Return either the completed JSON file named `pudge-speaker-annotations.json` or a ZIP containing that exact file.\n\nPudge validates source hashes and ids before importing, so do not renumber spans.\n"""

        downloads = Path.home() / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\wぁ-ゟ゠-ヿ一-鿿 .()\[\]-]+", "_", series_title).strip()[:90] or "light-novel"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        archive = downloads / f"Pudge-speaker-markup-{safe}-{stamp}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("INSTRUCTIONS.md", instructions.encode("utf-8"))
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            zf.writestr("profiles.json", json.dumps({"schema": 2, "scope": "series-fallback", "profiles": profiles}, ensure_ascii=False, indent=2).encode("utf-8"))
            zf.writestr("character_profiles.json", json.dumps({"schema": 1, "scope": "anilist-character", "characters": exported_character_profiles}, ensure_ascii=False, indent=2).encode("utf-8"))
            zf.writestr("known_characters.json", json.dumps(known_characters, ensure_ascii=False, indent=2).encode("utf-8"))
            zf.writestr("OUTPUT_TEMPLATE.json", json.dumps(output_template, ensure_ascii=False, indent=2).encode("utf-8"))
            for rel, content in chapter_files.items():
                zf.writestr(rel, content)
        if sys.platform == "darwin":
            try:
                subprocess.Popen(["open", "-R", str(archive)])
            except OSError:
                pass
        return {
            "ok": True,
            "path": str(archive),
            "series_key": series_key,
            "series_title": series_title,
            "book_count": len(books),
            "chapter_count": chapter_count,
            "dialogue_count": dialogue_count,
            "profile_count": len(profiles),
        }

    @staticmethod
    def _read_speaker_annotations_file(path: Path) -> dict[str, Any]:
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise LightNovelError("Speaker annotation JSON must contain an object")
            return payload
        if path.suffix.lower() != ".zip":
            raise LightNovelError("Choose a Pudge speaker annotation .json or .zip file")
        with zipfile.ZipFile(path, "r") as zf:
            candidates = [
                name for name in zf.namelist()
                if Path(name).name.casefold() == "pudge-speaker-annotations.json"
            ]
            for name in candidates:
                try:
                    payload = json.loads(zf.read(name).decode("utf-8"))
                except (KeyError, UnicodeDecodeError, ValueError):
                    continue
                if isinstance(payload, dict) and payload.get("kind") == "pudge-speaker-annotations":
                    return payload
        raise LightNovelError("ZIP does not contain pudge-speaker-annotations.json")

    def import_light_novel_speaker_markup(self, book_id: int, path: str) -> dict[str, Any]:
        anchor, local_books = self._speaker_markup_series_books(int(book_id))
        expected_series_key = str(anchor.get("series_key") or f"book:{int(book_id)}")
        payload = self._read_speaker_annotations_file(Path(str(path)).expanduser())
        if int(payload.get("schema") or 0) != 1 or payload.get("kind") != "pudge-speaker-annotations":
            raise LightNovelError("Unsupported Pudge speaker annotation schema")
        if str(payload.get("series_key") or "") != expected_series_key:
            raise LightNovelError("Speaker annotation archive belongs to a different LN series")

        local_by_id = {int(item["id"]): item for item in local_books}
        context = self._audiobook_speaker_cache_context(int(anchor["id"]))
        profiles_path = Path(context["profiles_path"])
        profiles = self._read_audiobook_speaker_profiles(profiles_path)
        character_profiles_path = self._audiobook_character_profiles_path()
        character_profiles = self._read_audiobook_character_profiles(character_profiles_path)

        known_characters: list[dict[str, Any]] = []
        seen_media: set[int] = set()
        for local_book in local_books:
            try:
                media_id = int(local_book.get("anilist_id") or 0)
            except (TypeError, ValueError):
                media_id = 0
            if media_id <= 0 or media_id in seen_media:
                continue
            seen_media.add(media_id)
            try:
                known_characters.extend(self.light_novels.character_glossary(media_id))
            except Exception:
                continue
        identity_index = self._audiobook_character_identity_index(known_characters)
        canonical_by_id: dict[int, str] = {}
        for character_id, canonical in identity_index.values():
            if canonical:
                canonical_by_id.setdefault(character_id, canonical)

        def normalize_speaker(value: Any) -> str:
            return re.sub(r"\s+", " ", str(value or "")).strip()[:80]

        def resolve_identity(speaker: str, explicit_id: Any = None) -> tuple[str, int]:
            try:
                character_id = int(explicit_id or 0)
            except (TypeError, ValueError):
                character_id = 0
            indexed = identity_index.get(speaker.casefold()) if speaker else None
            if character_id <= 0 and indexed:
                character_id = int(indexed[0])
            canonical = canonical_by_id.get(character_id, "") if character_id > 0 else ""
            if not canonical and indexed:
                canonical = indexed[1]
            return (canonical or speaker), max(0, character_id)

        # v96 archives carry global AniList-id profiles explicitly. Existing global
        # voices are authoritative so importing a later sequel cannot redesign them.
        imported_character_profiles = payload.get("character_profiles", {})
        if isinstance(imported_character_profiles, dict):
            for raw_id, raw_value in imported_character_profiles.items():
                try:
                    character_id = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if character_id <= 0 or character_id in character_profiles:
                    continue
                if isinstance(raw_value, dict):
                    caption = re.sub(r"\s+", " ", str(raw_value.get("caption") or "")).strip()[:240]
                    name = normalize_speaker(raw_value.get("name") or canonical_by_id.get(character_id, ""))
                else:
                    caption = re.sub(r"\s+", " ", str(raw_value or "")).strip()[:240]
                    name = canonical_by_id.get(character_id, "")
                if caption:
                    character_profiles[character_id] = {"name": name, "caption": caption}

        # Backward-compatible v95 series profiles are promoted lazily when their
        # speaker name resolves to an AniList character. Unresolved names stay
        # series-scoped and therefore cannot leak between unrelated characters.
        imported_profiles = payload.get("profiles", {})
        if isinstance(imported_profiles, dict):
            for raw_speaker, raw_caption in imported_profiles.items():
                speaker = normalize_speaker(raw_speaker)
                caption = re.sub(r"\s+", " ", str(raw_caption or "")).strip()[:240]
                if not speaker or not caption or speaker.casefold() in {"unknown", "不明"}:
                    continue
                speaker, character_id = resolve_identity(speaker)
                if character_id > 0:
                    if character_id not in character_profiles:
                        character_profiles[character_id] = {"name": speaker, "caption": caption}
                else:
                    profiles.setdefault(speaker, caption)

        imported_chapters = 0
        imported_assignments = 0
        skipped_unknown = 0
        for book_row in payload.get("books", []) if isinstance(payload.get("books"), list) else []:
            if not isinstance(book_row, dict):
                continue
            try:
                target_book_id = int(book_row.get("book_id"))
            except (TypeError, ValueError):
                continue
            if target_book_id not in local_by_id:
                raise LightNovelError(f"Speaker markup references unavailable local volume book_id={target_book_id}")
            source = self.light_novels.tts_source(target_book_id)
            chapters = {int(ch.get("index") or 0): ch for ch in source.get("chapters") or []}
            book_context = self._audiobook_speaker_cache_context(target_book_id)
            book_dir = Path(book_context["book_dir"])
            for chapter_row in book_row.get("chapters", []) if isinstance(book_row.get("chapters"), list) else []:
                if not isinstance(chapter_row, dict):
                    continue
                try:
                    chapter_index = int(chapter_row.get("chapter_index"))
                except (TypeError, ValueError):
                    continue
                chapter = chapters.get(chapter_index)
                if chapter is None:
                    raise LightNovelError(
                        f"Speaker markup chapter is not present locally: book_id={target_book_id} chapter={chapter_index}"
                    )
                chapter_text = str(chapter.get("text") or "")
                digest = self._speaker_markup_chapter_digest(chapter_text)
                if str(chapter_row.get("source_sha256") or "") != digest:
                    raise LightNovelError(
                        f"LN text changed since export: book_id={target_book_id} chapter={chapter_index}. Export a fresh archive."
                    )
                units = self._audiobook_voice_units(chapter_text)
                assignable_ids = {int(unit["id"]) for unit in units}
                assignments: list[dict[str, Any]] = []
                seen_ids: set[int] = set()
                for raw in chapter_row.get("assignments", []) if isinstance(chapter_row.get("assignments"), list) else []:
                    if not isinstance(raw, dict):
                        continue
                    try:
                        unit_id = int(raw.get("id"))
                    except (TypeError, ValueError):
                        continue
                    if unit_id not in assignable_ids or unit_id in seen_ids:
                        continue
                    seen_ids.add(unit_id)
                    speaker = normalize_speaker(raw.get("speaker") or "unknown") or "unknown"
                    speaker, character_id = resolve_identity(speaker, raw.get("anilist_character_id"))
                    caption = re.sub(r"\s+", " ", str(raw.get("caption") or "")).strip()[:240]
                    if speaker.casefold() in {"unknown", "不明"}:
                        caption = ""
                        character_id = 0
                        skipped_unknown += 1
                    elif character_id > 0:
                        existing_global = character_profiles.get(character_id)
                        if existing_global and existing_global.get("caption"):
                            caption = str(existing_global["caption"])
                        elif speaker in profiles and profiles[speaker]:
                            # Lazy v95 -> v96 migration: preserve the already used
                            # voice the first time this speaker resolves to AniList.
                            caption = profiles[speaker]
                            character_profiles[character_id] = {"name": speaker, "caption": caption}
                        elif caption:
                            character_profiles[character_id] = {"name": speaker, "caption": caption}
                    elif speaker in profiles:
                        caption = profiles[speaker]
                    elif caption:
                        profiles[speaker] = caption
                    assignment: dict[str, Any] = {"id": unit_id, "speaker": speaker, "caption": caption}
                    if character_id > 0:
                        assignment["anilist_character_id"] = character_id
                    assignments.append(assignment)
                target = self._audiobook_manual_chapter_cache(book_dir, chapter_index, chapter_text)
                target.write_text(
                    json.dumps(
                        {
                            "schema": 2,
                            "source": "chatgpt-import",
                            "source_sha256": digest,
                            "assignments": assignments,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                imported_chapters += 1
                imported_assignments += len(assignments)

        if imported_chapters <= 0:
            raise LightNovelError("Speaker annotation file did not contain any matching chapters")
        self._write_audiobook_speaker_profiles(
            profiles_path,
            profiles,
            expected_series_key,
            str(anchor.get("series_title") or anchor.get("title") or ""),
        )
        self._write_audiobook_character_profiles(character_profiles_path, character_profiles)
        # Imported manual markup should work without any API LLM configuration.
        self.light_novels.save_settings({"audiobook_character_voices": True})
        return {
            "ok": True,
            "series_key": expected_series_key,
            "series_title": str(anchor.get("series_title") or anchor.get("title") or ""),
            "chapters": imported_chapters,
            "assignments": imported_assignments,
            "profiles": len(profiles),
            "character_profiles": len(character_profiles),
            "unknown": skipped_unknown,
        }

    def choose_light_novel_speaker_markup(self, book_id: int) -> dict[str, Any]:
        if self.window is None:
            return {"cancelled": True}
        try:
            import webview
            result = self.window.create_file_dialog(
                webview.OPEN_DIALOG,
                directory=str(Path.home() / "Downloads"),
                allow_multiple=False,
                file_types=("Pudge speaker markup (*.zip;*.json)", "ZIP (*.zip)", "JSON (*.json)"),
            )
        except Exception as exc:
            return {"cancelled": True, "error": str(exc)}
        if not result:
            return {"cancelled": True}
        raw = result[0] if isinstance(result, (list, tuple)) else result
        return {"cancelled": False, **self.import_light_novel_speaker_markup(int(book_id), str(raw))}

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
        return self.study_action(payload)

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
            "category": "2_0",
            "target_volume": volume or None,
            "releases": self.light_novels.search_nyaa(
                query, target_volume=volume or None
            ),
        }

    @staticmethod
    def _audiobook_nyaa_series_match(value: str, series_title: str) -> bool:
        return audiobook_series_path_matches(value, series_title)

    @staticmethod
    def _audiobook_nyaa_title_looks_like_audiobook(value: str) -> bool:
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return bool(re.search(r"(?:オーディオブック|audiobook|unabridged)", text, re.I))

    def _audiobook_torrent_client(self) -> tuple[str, Any]:
        qbt_cfg = getattr(self.config, "qbittorrent", None)
        aria_cfg = getattr(self.config, "aria2", None)
        if bool(getattr(qbt_cfg, "enabled", True if qbt_cfg is not None and aria_cfg is None else False)):
            cfg = qbt_cfg
            return "qbittorrent", QBittorrentClient(
                cfg.base_url,
                cfg.username,
                cfg.password,
                cfg.api_key,
                verify_tls=cfg.verify_tls,
                pre_download_command=cfg.pre_download_command,
                auto_start_app=cfg.auto_start_app,
            )
        if bool(getattr(aria_cfg, "enabled", False)):
            cfg = aria_cfg
            return "aria2", Aria2Client(
                enabled=cfg.enabled,
                binary=cfg.binary,
                rpc_port=cfg.rpc_port,
                pre_download_command=self.config.qbittorrent.pre_download_command,
                paused_on_add=cfg.paused_on_add,
                auto_start=cfg.auto_start,
                source_proxy_mode=self.config.nyaa.proxy_mode,
                source_proxy_url=self.config.nyaa.proxy_url,
                seed_mode=cfg.seed_mode,
                seed_ratio=cfg.seed_ratio,
                seed_time_minutes=cfg.seed_time_minutes,
                upload_limit_kib=cfg.upload_limit_kib,
                vpn_interface=cfg.vpn_interface,
                vpn_kill_switch=cfg.vpn_kill_switch,
            )
        raise LightNovelError("Audiobook torrent download requires qBittorrent or aria2")

    def _audiobook_nyaa_volume_selection(
        self,
        files: list[dict[str, Any]],
        target_volume: int,
        release_title: str,
        *,
        series_title: str = "",
        require_series_match: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        release_name = str(release_title or "")
        looks_like_collection = bool(
            require_series_match
            or re.search(r"(?i)(?:collection|pack|batch|complete[ ._-]*series|全巻|全集|第?\d+[-~–—]\d+巻)", release_name)
        )
        plan = audiobook_torrent_pack_plan(
            files,
            series_title=series_title,
            allow_bare_numeric_volume_dirs=looks_like_collection,
        )
        target = int(target_volume or 0)
        for volume in plan.get("volumes") or []:
            if int(volume.get("volume") or 0) == target:
                selected_ids = {int(value) for value in volume.get("file_ids") or []}
                selected = [
                    row for row in (plan.get("files") or [])
                    if int(row.get("file_id") if row.get("file_id") is not None else -1) in selected_ids
                    and row.get("is_audio")
                ]
                if require_series_match:
                    selected = [
                        row for row in selected
                        if self._audiobook_nyaa_series_match(str(row.get("name") or ""), series_title)
                    ]
                if selected:
                    return selected, plan

        # A release explicitly labelled as exactly one requested volume may be
        # split into chapter MP3/M4A files without repeating the volume number in
        # every filename.  In that case all audio files belong to this volume.
        matches, volume_range, exact = self.light_novels._release_volume_match(
            str(release_title or ""), target
        )
        audio = [row for row in (plan.get("files") or []) if row.get("is_audio")]
        if require_series_match:
            audio = [
                row for row in audio
                if self._audiobook_nyaa_series_match(str(row.get("name") or ""), series_title)
            ]
        if matches and exact and audio and not (plan.get("archive_file_ids") or []):
            return audio, plan
        return [], plan

    def _ln_audiobook_search_cache_key(self, book: dict[str, Any]) -> dict[str, Any]:
        return {
            "series": unicodedata.normalize(
                "NFKC", str(book.get("series_title") or book.get("title") or "")
            ).strip().casefold(),
            "volume": max(0, int(book.get("volume") or 0)),
        }

    def light_novel_cached_audiobook_nyaa(self, book_id: int) -> dict[str, Any]:
        """Return the last successful Nyaa result without touching the network."""
        book = self.light_novels.book(int(book_id))
        target_volume = max(0, int(book.get("volume") or 0))
        cache = getattr(self, "_ln_audiobook_search_cache", None)
        cached = None
        if cache is not None:
            cached = cache.get(
                self._ln_audiobook_search_cache_key(book),
                ttl_seconds=30 * 24 * 3600,
            )
        if not isinstance(cached, dict):
            return {
                "cached": False,
                "target_volume": target_volume,
                "matches": [],
                "inspected_releases": 0,
            }
        result = dict(cached)
        result["cached"] = True
        result["cache_age_seconds"] = max(
            0.0, time.time() - float(result.get("cached_at") or time.time())
        )
        return result

    def _ln_audiobook_cached_torrent_files(self, release: NyaaRelease) -> list[dict[str, Any]]:
        cache = getattr(self, "_ln_audiobook_torrent_cache", None)
        if cache is None:
            return []
        key = str(release.info_hash or release.torrent_url or release.link or "").strip()
        if not key:
            return []
        rows = cache.get(key, ttl_seconds=60 * 24 * 3600)
        return [dict(row) for row in rows] if isinstance(rows, list) else []

    def _ln_audiobook_cache_torrent_files(
        self, release: NyaaRelease, files: list[dict[str, Any]]
    ) -> None:
        cache = getattr(self, "_ln_audiobook_torrent_cache", None)
        if cache is None or not files:
            return
        key = str(release.info_hash or release.torrent_url or release.link or "").strip()
        if not key:
            return
        cache.put(key, [dict(row) for row in files])

    def light_novel_search_audiobook_nyaa(self, book_id: int) -> dict[str, Any]:
        book = self.light_novels.book(int(book_id))
        target_volume = max(0, int(book.get("volume") or 0))
        if target_volume <= 0:
            raise LightNovelError("This light novel does not have a detectable volume number")
        series_title = str(book.get("series_title") or book.get("title") or "").strip()
        query = self.light_novels._nyaa_title(series_title)
        if not query:
            raise LightNovelError("Could not derive a Nyaa search title for this series")
        client = NyaaClient(
            self.config.nyaa.base_url,
            category="2_0",
            proxy_mode=self.config.nyaa.proxy_mode,
            proxy_url=self.config.nyaa.proxy_url,
            pre_search_command=self.config.nyaa.pre_search_command,
            timeout=8.0,
        )
        errors: list[str] = []
        # Nyaa search indexes release titles, not the file table inside a torrent.
        # Japanese audiobooks are commonly buried in generic TMW/collection packs,
        # so direct title search alone cannot discover them.  Search a small set of
        # collection feeds too, then inspect their .torrent metadata and require
        # the requested series name inside the selected file path.
        direct_queries = tuple(dict.fromkeys((
            query,
            f"{query} audiobook",
            f"{query} オーディオブック",
            f"{query} {target_volume}",
            f"{query} 第{target_volume}巻",
        )))
        collection_queries = (
            "TMW Japanese Audiobooks Collection",
            "Japanese Audiobooks Collection",
            "Japanese Audiobook Collection",
            "Light Novel Audiobooks Collection",
            "Light Novel Audiobook Collection",
            "オーディオブック コレクション",
        )
        discovered: dict[str, tuple[NyaaRelease, bool]] = {}
        self.logger.info(
            "LN audiobook Nyaa search start book_id=%s series=%r volume=%s query=%r",
            int(book_id), series_title, target_volume, query,
        )
        try:
            for search_query in direct_queries:
                try:
                    found = client.search(search_query, category="2_0")
                    self.logger.info(
                        "LN audiobook Nyaa direct query=%r releases=%s", search_query, len(found)
                    )
                    for release in found:
                        key = str(release.info_hash or release.torrent_url or release.link)
                        if key:
                            discovered[key] = (release, False)
                except Exception as exc:
                    errors.append(f"{search_query}: {exc}")
            for search_query in collection_queries:
                try:
                    found = client.search(search_query, category="2_0")
                    self.logger.info(
                        "LN audiobook Nyaa collection query=%r releases=%s", search_query, len(found)
                    )
                    for release in found:
                        key = str(release.info_hash or release.torrent_url or release.link)
                        if key and key not in discovered:
                            discovered[key] = (release, True)
                except Exception as exc:
                    errors.append(f"{search_query}: {exc}")

            direct = [row for row in discovered.values() if not row[1]]
            collections = [row for row in discovered.values() if row[1]]
            rank = lambda item: (
                bool(item[0].trusted), int(item[0].seeders), int(item[0].downloads),
                int(item[0].size_bytes),
            )
            direct = sorted(direct, key=rank, reverse=True)[:36]
            collections = sorted(collections, key=rank, reverse=True)[:64]
            releases = [*direct, *collections]

            def inspect(item: tuple[NyaaRelease, bool]) -> dict[str, Any] | None:
                release, generic_collection = item
                try:
                    # Direct single-volume audiobook releases already carry the
                    # complete volume identity in the Nyaa title. Do not hammer
                    # /download/*.torrent just to rediscover that fact: Nyaa can
                    # rate-limit those endpoints independently from search.
                    if not generic_collection:
                        title_is_audiobook = self._audiobook_nyaa_title_looks_like_audiobook(release.title)
                        if not title_is_audiobook:
                            return None
                        title_matches, _title_range, title_exact = self.light_novels._release_volume_match(
                            str(release.title or ""), target_volume
                        )
                        if (
                            title_matches
                            and title_exact
                            and self._audiobook_nyaa_series_match(str(release.title or ""), series_title)
                        ):
                            volume_title = str(book.get("title") or "").strip() or f"Volume {target_volume}"
                            self.logger.info(
                                "LN audiobook Nyaa title match title=%r volume_title=%r without metadata fetch",
                                release.title, volume_title,
                            )
                            return {
                                "title": release.title,
                                "torrent_url": release.torrent_url,
                                "link": release.link,
                                "info_hash": release.info_hash,
                                "seeders": int(release.seeders),
                                "size": release.size_text,
                                "trusted": bool(release.trusted),
                                "target_volume": target_volume,
                                "volume_title": volume_title,
                                "selected_files": [],
                                "selected_track_count": None,
                                "selected_size_bytes": 0,
                                "pack_volumes": [target_volume],
                                "collection": False,
                                "generic_collection": False,
                                "metadata_source": "title",
                            }
                        # An explicit audiobook release for another volume cannot
                        # become the requested one because of chapter numbers.
                        # Skip it before any torrent metadata request.
                        if re.search(r"(?i)(?:vol(?:ume)?[ ._-]*\d+|第?\d+巻|[IVXLCDM]+～?完全版オーディオブック)", str(release.title or "")):
                            return None

                    files = self._ln_audiobook_cached_torrent_files(release)
                    metadata_source = "cache" if files else "torrent"
                    if not files:
                        try:
                            payload = client.fetch_torrent_payload(release.torrent_url)
                            files = audiobook_torrent_files_from_payload(payload)
                        except Exception as torrent_exc:
                            if not self.config.qbittorrent.enabled:
                                raise
                            metadata_source = "magnet"
                            self.logger.info(
                                "LN audiobook Nyaa torrent metadata fallback title=%r error=%s",
                                release.title, torrent_exc,
                            )
                            qbt = QBittorrentClient(
                                self.config.qbittorrent.base_url,
                                self.config.qbittorrent.username,
                                self.config.qbittorrent.password,
                                self.config.qbittorrent.api_key,
                                verify_tls=self.config.qbittorrent.verify_tls,
                                pre_download_command=self.config.qbittorrent.pre_download_command,
                                auto_start_app=self.config.qbittorrent.auto_start_app,
                            )
                            try:
                                files = qbt.inspect_release_files(
                                    release,
                                    save_path=DATA_DIR / "cache" / "nyaa-audiobook-metadata",
                                    metadata_timeout=20.0,
                                )
                            finally:
                                qbt.close()
                            if not files:
                                raise LightNovelError(
                                    "Could not read torrent metadata through Nyaa or qBittorrent"
                                )
                        self._ln_audiobook_cache_torrent_files(release, files)
                    selected, plan = self._audiobook_nyaa_volume_selection(
                        files, target_volume, release.title,
                        series_title=series_title,
                        require_series_match=generic_collection,
                    )
                    if not selected:
                        return None
                    selected_names = [str(row.get("name") or "") for row in selected]
                    volume_title = str(book.get("title") or "").strip() or f"Volume {target_volume}"
                    self.logger.info(
                        "LN audiobook Nyaa match title=%r volume_title=%r tracks=%s sample=%r",
                        release.title, volume_title, len(selected_names), selected_names[:2],
                    )
                    return {
                        "title": release.title,
                        "torrent_url": release.torrent_url,
                        "link": release.link,
                        "info_hash": release.info_hash,
                        "seeders": int(release.seeders),
                        "size": release.size_text,
                        "trusted": bool(release.trusted),
                        "target_volume": target_volume,
                        "volume_title": volume_title,
                        "selected_files": selected_names,
                        "selected_track_count": len(selected_names),
                        "selected_size_bytes": sum(int(row.get("size_bytes") or 0) for row in selected),
                        "pack_volumes": [int(row.get("volume") or 0) for row in plan.get("volumes") or []],
                        "collection": bool(generic_collection or len(plan.get("volumes") or []) > 1),
                        "generic_collection": bool(generic_collection),
                        "metadata_source": metadata_source,
                    }
                except Exception as exc:
                    self.logger.info(
                        "LN audiobook Nyaa inspect skipped title=%r generic=%s error=%s",
                        release.title, generic_collection, exc,
                    )
                    return None

            rows: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=min(8, max(1, len(releases)))) as pool:
                futures = [pool.submit(inspect, release) for release in releases]
                for future in as_completed(futures):
                    row = future.result()
                    if row:
                        rows.append(row)
            rows.sort(
                key=lambda row: (
                    bool(row.get("generic_collection")),
                    bool(row.get("trusted")),
                    int(row.get("seeders") or 0),
                ),
                reverse=True,
            )
            self.logger.info(
                "LN audiobook Nyaa search done book_id=%s volume=%s direct=%s collections=%s inspected=%s matches=%s",
                int(book_id), target_volume, len(direct), len(collections), len(releases), len(rows),
            )
            result = {
                "query": query,
                "category": "2_0",
                "target_volume": target_volume,
                "inspected_releases": len(releases),
                "direct_releases": len(direct),
                "collection_releases": len(collections),
                "matches": rows,
                "errors": errors[:5],
                "cached_at": time.time(),
            }
            cache = getattr(self, "_ln_audiobook_search_cache", None)
            if cache is not None:
                cache.put(self._ln_audiobook_search_cache_key(book), result)
            return result
        finally:
            client.close()

    def _light_novel_audiobook_download_destination(self, book: dict[str, Any]) -> Path:
        base_root = (
            Path(self.config.paths.download_dirs[0]).expanduser()
            if self.config.paths.download_dirs
            else Path(self.config.library.root_dir).expanduser()
        )
        safe_series = re.sub(
            r"[^\wぁ-ゟ゠-ヿ一-鿿 ._-]+",
            " ",
            str(book.get("series_title") or book.get("title") or "Audiobook"),
        )
        safe_series = re.sub(r"\s+", " ", safe_series).strip(" ._")[:120] or "Audiobook"
        target_volume = max(0, int(book.get("volume") or 0))
        return base_root / "Pudge Audiobooks" / safe_series / f"Volume {target_volume:02d}"

    @staticmethod
    def _ln_audiobook_selection_manifest_path(destination: Path) -> Path:
        return destination / ".pudge-audiobook-selection.json"

    def _write_ln_audiobook_selection_manifest(
        self,
        destination: Path,
        *,
        book: dict[str, Any],
        torrent_hash: str,
        selected: list[dict[str, Any]],
        collection: bool,
    ) -> None:
        payload = {
            "schema": 1,
            "book_id": int(book.get("id") or 0),
            "series_title": str(book.get("series_title") or book.get("title") or ""),
            "volume": max(0, int(book.get("volume") or 0)),
            "torrent_hash": str(torrent_hash or ""),
            "collection": bool(collection),
            "selected_files": [str(row.get("name") or "") for row in selected if str(row.get("name") or "").strip()],
            "selected_entries": [
                {
                    "name": str(row.get("name") or ""),
                    "size_bytes": max(0, int(row.get("size_bytes", row.get("size", 0)) or 0)),
                }
                for row in selected
                if str(row.get("name") or "").strip()
            ],
            "created_at": time.time(),
        }
        path = self._ln_audiobook_selection_manifest_path(destination)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _normalized_audiobook_relative_path(value: object) -> str:
        return unicodedata.normalize("NFKC", str(value or "")).replace("\\", "/").strip("/")

    def _recoverable_light_novel_audiobook_paths(
        self,
        book: dict[str, Any],
        destination: Path,
    ) -> list[Path]:
        """Resolve only the requested LN volume from a completed managed download.

        The destination can contain the directory tree of a huge audiobook
        collection.  The outer managed ``Volume XX`` directory is not evidence
        that every nested file belongs to that volume; series and volume are
        therefore revalidated from torrent-relative paths (or the persisted
        selection manifest) before import.
        """
        destination = destination.expanduser().resolve()
        if not destination.is_dir():
            return []
        target_volume = max(0, int(book.get("volume") or 0))
        series_title = str(book.get("series_title") or book.get("title") or "").strip()
        if target_volume <= 0 or not series_title:
            return []

        disk_rows: list[dict[str, Any]] = []
        path_by_name: dict[str, Path] = {}
        for path in destination.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in AUDIOBOOK_EXTENSIONS:
                continue
            try:
                relative = path.relative_to(destination).as_posix()
                # Never feed our own hard-link staging directory back into recovery.
                if relative.startswith(".pudge-volume-import/"):
                    continue
                size = max(0, int(path.stat().st_size))
            except (OSError, ValueError):
                continue
            normalized = self._normalized_audiobook_relative_path(relative)
            row = {"index": len(disk_rows), "name": relative, "size": size, "priority": 0}
            disk_rows.append(row)
            path_by_name[normalized] = path.resolve()
        if not disk_rows:
            return []

        manifest_path = self._ln_audiobook_selection_manifest_path(destination)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            manifest = {}
        if (
            isinstance(manifest, dict)
            and int(manifest.get("book_id") or 0) == int(book.get("id") or 0)
            and int(manifest.get("volume") or 0) == target_volume
        ):
            resolved: list[Path] = []
            seen: set[Path] = set()
            for expected_raw in manifest.get("selected_files") or []:
                expected = self._normalized_audiobook_relative_path(expected_raw)
                matches = [
                    path
                    for actual, path in path_by_name.items()
                    if actual == expected or actual.endswith("/" + expected) or expected.endswith("/" + actual)
                ]
                if len(matches) == 1 and matches[0] not in seen:
                    if bool(manifest.get("collection")):
                        try:
                            relative = matches[0].relative_to(destination).as_posix()
                        except ValueError:
                            continue
                        if not self._audiobook_nyaa_series_match(relative, series_title):
                            continue
                    seen.add(matches[0])
                    resolved.append(matches[0])
            expected_count = len({
                self._normalized_audiobook_relative_path(value)
                for value in (manifest.get("selected_files") or [])
                if str(value or "").strip()
            })
            if resolved and len(resolved) == expected_count:
                expected_sizes = {
                    self._normalized_audiobook_relative_path(row.get("name")): max(0, int(row.get("size_bytes") or 0))
                    for row in (manifest.get("selected_entries") or [])
                    if isinstance(row, dict) and str(row.get("name") or "").strip()
                }
                complete = True
                for path in resolved:
                    try:
                        relative = self._normalized_audiobook_relative_path(path.relative_to(destination).as_posix())
                        expected = next((size for name, size in expected_sizes.items() if relative == name or relative.endswith("/" + name) or name.endswith("/" + relative)), 0)
                        if expected > 0 and int(path.stat().st_size) < max(1, int(expected * 0.999)):
                            complete = False
                            break
                    except (OSError, ValueError):
                        complete = False
                        break
                if complete:
                    return sorted(resolved, key=lambda path: str(path).casefold())
                return []

        # Recovery of pre-v160 downloads has no manifest.  First isolate the
        # exact series subtree so ``新説 狼と香辛料 / 狼と羊皮紙`` cannot be
        # imported merely because the managed outer folder says 狼と香辛料.
        exact_series_rows = [
            row for row in disk_rows
            if self._audiobook_nyaa_series_match(str(row.get("name") or ""), series_title)
        ]
        if not exact_series_rows:
            return []
        selected, _plan = self._audiobook_nyaa_volume_selection(
            exact_series_rows,
            target_volume,
            "Audiobook Collection",
            series_title=series_title,
            require_series_match=True,
        )
        if not selected and len(exact_series_rows) == 1:
            # A single exact-series audio file inside Pudge's target-volume
            # destination is safe even when the filename is opaque.
            selected = list(exact_series_rows)

        resolved = []
        seen: set[Path] = set()
        for row in selected:
            normalized = self._normalized_audiobook_relative_path(row.get("name"))
            path = path_by_name.get(normalized)
            if path is not None and path not in seen:
                seen.add(path)
                resolved.append(path)
        # Old downloads have no persisted expected sizes.  Do not import a file
        # that aria2 may still be extending after a restart; the throttled state
        # recovery will try again once the payload has been stable for a minute.
        now = time.time()
        for path in resolved:
            try:
                if now - float(path.stat().st_mtime) < 60.0:
                    return []
            except OSError:
                return []
        return sorted(resolved, key=lambda path: str(path).casefold())

    def _import_recovered_light_novel_audiobook(
        self,
        book: dict[str, Any],
        destination: Path,
        audio_paths: list[Path],
    ) -> dict[str, Any] | None:
        if not audio_paths:
            return None
        volume_title = str(book.get("title") or "").strip() or f"Volume {int(book.get('volume') or 0):02d}"
        audio_paths = sorted({path.resolve() for path in audio_paths}, key=lambda path: str(path).casefold())
        if len(audio_paths) == 1:
            return self.audiobooks.import_file(
                audio_paths[0], auto_link=False, prepare_transcription=False, title_override=volume_title
            )

        common_root = Path(os.path.commonpath([str(path) for path in audio_paths]))
        if common_root.is_file():
            common_root = common_root.parent
        selected_set = set(audio_paths)
        all_under_common = {
            path.resolve()
            for path in common_root.rglob("*")
            if path.is_file() and path.suffix.casefold() in AUDIOBOOK_EXTENSIONS
        }
        if all_under_common == selected_set:
            import_root = common_root
        else:
            # The common root also contains other volumes/books.  Build a
            # hidden hard-link view containing only the selected tracks; this
            # costs no duplicate media storage and prevents import_folder from
            # swallowing the rest of a giant collection.
            import_root = destination / ".pudge-volume-import"
            if import_root.exists():
                shutil.rmtree(import_root)
            import_root.mkdir(parents=True, exist_ok=True)
            for index, source in enumerate(audio_paths):
                target = import_root / f"{index:04d}-{source.name}"
                os.link(source, target)
        return self.audiobooks.import_folder(
            import_root, auto_link=False, prepare_transcription=False, title_override=volume_title
        )

    def _recover_light_novel_audiobook_downloads(self) -> dict[str, Any]:
        lock = getattr(self, "_ln_audiobook_recovery_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._ln_audiobook_recovery_lock = lock
        if not lock.acquire(blocking=False):
            return {"skipped": True, "recovered": 0}
        recovered = 0
        inspected = 0
        try:
            state = self.light_novels.state()
            for book in state.get("books", []):
                try:
                    book_id = int(book.get("id") or 0)
                    if book_id <= 0 or int(book.get("volume") or 0) <= 0:
                        continue
                    if self.audiobooks.link_for_light_novel(book_id, include_alignment=False, include_transcription=False):
                        continue
                    destination = self._light_novel_audiobook_download_destination(book)
                    if not destination.is_dir():
                        continue
                    inspected += 1
                    audio_paths = self._recoverable_light_novel_audiobook_paths(book, destination)
                    if not audio_paths:
                        self.logger.info(
                            "LN audiobook recovery no exact volume book_id=%s destination=%s",
                            book_id, destination,
                        )
                        continue
                    self.logger.info(
                        "LN audiobook recovery candidate book_id=%s volume=%s files=%s sample=%r",
                        book_id, int(book.get("volume") or 0), len(audio_paths),
                        [str(path) for path in audio_paths[:3]],
                    )
                    imported = self._import_recovered_light_novel_audiobook(book, destination, audio_paths)
                    if not imported or not imported.get("id"):
                        continue
                    self.audiobooks.link_light_novel(book_id, int(imported["id"]), prepare_alignment=True)
                    recovered += 1
                    self.logger.info(
                        "LN audiobook recovery linked book_id=%s audiobook_id=%s files=%s",
                        book_id, int(imported["id"]), len(audio_paths),
                    )
                except Exception as exc:
                    self.logger.warning(
                        "LN audiobook recovery skipped book_id=%s error=%s",
                        int(book.get("id") or 0), exc,
                    )
            return {"skipped": False, "inspected": inspected, "recovered": recovered}
        finally:
            lock.release()

    def _schedule_light_novel_audiobook_recovery(
        self,
        reason: str,
        *,
        min_interval: float = 15.0,
    ) -> bool:
        if getattr(getattr(self, "safe_mode", None), "active", False):
            return False
        now = time.monotonic()
        last = float(getattr(self, "_ln_audiobook_last_recovery_at", 0.0) or 0.0)
        if min_interval > 0 and now - last < float(min_interval):
            return False
        lock = getattr(self, "_ln_audiobook_recovery_lock", None)
        if lock is not None and lock.locked():
            return False
        self._ln_audiobook_last_recovery_at = now
        thread = threading.Thread(
            target=self._recover_light_novel_audiobook_downloads,
            name=f"ln-audiobook-recovery-{reason}",
            daemon=True,
        )
        thread.start()
        return True

    def _finish_light_novel_audiobook_torrent(
        self,
        torrent_hash: str,
        book_id: int,
        destination: Path,
        selected_ids: set[int],
    ) -> None:
        backend_name, torrent = self._audiobook_torrent_client()
        try:
            deadline = time.monotonic() + 14 * 24 * 3600
            selected_rows: list[dict[str, Any]] = []
            while time.monotonic() < deadline:
                files = torrent.files(torrent_hash)
                selected_rows = [
                    row for row in files
                    if int(row.get("index") if row.get("index") is not None else -1) in selected_ids
                ]
                if selected_rows and all(float(row.get("progress") or 0.0) >= 0.999 for row in selected_rows):
                    break
                time.sleep(20.0)
            else:
                self.logger.warning("Audiobook torrent timed out hash=%s", torrent_hash)
                return
            torrent.delete(torrent_hash, delete_files=False)

            audio_paths: list[Path] = []
            root = destination.resolve()
            for row in selected_rows:
                candidate = (destination / str(row.get("name") or "")).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    continue
                if candidate.is_file() and candidate.suffix.casefold() in AUDIOBOOK_EXTENSIONS:
                    audio_paths.append(candidate)
            if not audio_paths:
                self.logger.warning("Downloaded audiobook files were not found hash=%s", torrent_hash)
                return
            ln_book = self.light_novels.book(int(book_id))
            imported = self._import_recovered_light_novel_audiobook(ln_book, destination, audio_paths)
            if imported and imported.get("id"):
                self.audiobooks.link_light_novel(
                    int(book_id), int(imported["id"]), prepare_alignment=True
                )
            else:
                self.audiobooks.auto_link_light_novel(int(book_id))
            self.logger.info(
                "Audiobook volume download complete ln_book_id=%s files=%s",
                int(book_id), len(audio_paths),
            )
        except Exception as exc:
            self.logger.warning(
                "Audiobook volume download monitor failed ln_book_id=%s error=%s",
                int(book_id), exc,
            )
        finally:
            torrent.close()

    def light_novel_download_audiobook_nyaa(
        self,
        book_id: int,
        release: dict[str, Any],
    ) -> dict[str, Any]:
        qbt_enabled = bool(getattr(getattr(self.config, "qbittorrent", None), "enabled", False))
        aria_enabled = bool(getattr(getattr(self.config, "aria2", None), "enabled", False))
        if not (qbt_enabled or aria_enabled):
            raise LightNovelError("Selective audiobook downloads require qBittorrent or aria2")
        book = self.light_novels.book(int(book_id))
        target_volume = max(0, int(book.get("volume") or release.get("target_volume") or 0))
        if target_volume <= 0:
            raise LightNovelError("Could not determine the target audiobook volume")
        item = NyaaRelease(
            title=str(release.get("title") or ""),
            link=str(release.get("link") or ""),
            torrent_url=str(release.get("torrent_url") or ""),
            info_hash=str(release.get("info_hash") or ""),
            size_text=str(release.get("size") or ""),
            size_bytes=0,
            seeders=int(release.get("seeders") or 0),
            leechers=0,
            downloads=0,
            trusted=bool(release.get("trusted")),
            remake=False,
            category_id="2_0",
            published="",
            is_batch=True,
            group="",
        )
        destination = self._light_novel_audiobook_download_destination(book)
        destination.mkdir(parents=True, exist_ok=True)
        backend_name, torrent = self._audiobook_torrent_client()
        torrent_hash = ""
        try:
            torrent_hash = torrent.add_release(
                item,
                save_path=destination,
                category=f"{APP_SLUG}-audiobooks",
                tags=[APP_SLUG, "audiobook", f"ln-book:{int(book_id)}", f"volume:{target_volume}"],
                paused=True,
                stop_at_metadata=True,
            )
            deadline = time.monotonic() + 35.0
            files: list[dict[str, Any]] = []
            while time.monotonic() < deadline:
                files = torrent.files(torrent_hash)
                if files:
                    break
                time.sleep(0.5)
            if not files:
                raise LightNovelError("Could not read the audiobook torrent file list")
            series_title = str(book.get("series_title") or book.get("title") or "").strip()
            plan = audiobook_torrent_pack_plan(
                files,
                series_title=series_title,
                allow_bare_numeric_volume_dirs=False,
            )
            def normalized_torrent_name(value: object) -> str:
                return unicodedata.normalize("NFKC", str(value or "")).replace("\\", "/").strip("/")

            expected_names = {
                normalized_torrent_name(value)
                for value in (release.get("selected_files") or [])
                if str(value or "").strip()
            }
            selected: list[dict[str, Any]] = []
            if expected_names:
                available = [row for row in (plan.get("files") or []) if row.get("is_audio")]
                matched_expected: set[str] = set()
                matched_ids: set[int] = set()
                for expected in expected_names:
                    candidates = []
                    for row in available:
                        actual = normalized_torrent_name(row.get("name"))
                        if actual == expected or actual.endswith("/" + expected) or expected.endswith("/" + actual):
                            candidates.append(row)
                    if len(candidates) == 1:
                        row = candidates[0]
                        matched_expected.add(expected)
                        matched_ids.add(int(row.get("file_id") or -1))
                selected = [
                    row for row in available
                    if int(row.get("file_id") or -1) in matched_ids
                ]
                if matched_expected != expected_names:
                    missing = sorted(expected_names - matched_expected)
                    raise LightNovelError(
                        "Audiobook torrent metadata changed since search; missing selected file(s): "
                        + ", ".join(missing[:3])
                    )
            else:
                selected, _plan = self._audiobook_nyaa_volume_selection(
                    files,
                    target_volume,
                    item.title,
                    series_title=series_title,
                    require_series_match=bool(release.get("collection")),
                )
            if not selected:
                raise LightNovelError(
                    f"Volume {target_volume} is not separable in this audiobook torrent"
                )
            all_ids = [int(row.get("index")) for row in files if row.get("index") is not None]
            selected_ids = {int(row.get("file_id")) for row in selected}
            self.logger.info(
                "LN audiobook Nyaa download selection book_id=%s volume=%s tracks=%s names=%r",
                int(book_id), target_volume, len(selected), [str(row.get("name") or "") for row in selected[:3]],
            )
            self._write_ln_audiobook_selection_manifest(
                destination,
                book=book,
                torrent_hash=str(torrent_hash),
                selected=selected,
                collection=bool(release.get("collection")),
            )
            if backend_name == "aria2":
                not_selected = [index for index in all_ids if index not in selected_ids]
                if not_selected:
                    torrent.set_file_priority(torrent_hash, not_selected, 0)
            else:
                torrent.set_file_priority(torrent_hash, all_ids, 0)
                torrent.set_file_priority(torrent_hash, sorted(selected_ids), 6)
            torrent.start(torrent_hash)
        except Exception:
            if torrent_hash:
                try:
                    torrent.delete(torrent_hash, delete_files=True)
                except Exception:
                    pass
            raise
        finally:
            torrent.close()
        threading.Thread(
            target=self._finish_light_novel_audiobook_torrent,
            args=(str(torrent_hash), int(book_id), destination, selected_ids),
            name=f"audiobook-volume-{int(book_id)}-{target_volume}",
            daemon=True,
        ).start()
        return {
            "ok": True,
            "torrent_hash": torrent_hash,
            "target_volume": target_volume,
            "selected_files": [str(row.get("name") or "") for row in selected],
            "destination": str(destination),
        }

    def start_light_novel_download_audiobook_nyaa(
        self,
        book_id: int,
        release: dict[str, Any],
    ) -> dict[str, Any]:
        """Start selective audiobook metadata/download work without blocking the modal."""
        book_id = int(book_id)
        lock = getattr(self, "_ln_audiobook_download_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._ln_audiobook_download_lock = lock
        jobs = getattr(self, "_ln_audiobook_download_jobs", None)
        if jobs is None:
            jobs = {}
            self._ln_audiobook_download_jobs = jobs
        with lock:
            current = dict(jobs.get(book_id) or {})
            if current.get("running"):
                return {**current, "started": False, "already_running": True}
            job_id = f"ln-audiobook-{book_id}-{time.time_ns()}"
            job = {
                "job_id": job_id,
                "book_id": book_id,
                "running": True,
                "started": True,
                "state": "starting",
                "error": "",
                "started_at": time.time(),
                "finished_at": 0.0,
            }
            jobs[book_id] = job

        payload = dict(release or {})

        def run() -> None:
            with lock:
                jobs[book_id] = {**jobs.get(book_id, {}), "state": "preparing"}
            try:
                result = self.light_novel_download_audiobook_nyaa(book_id, payload)
            except Exception as exc:
                self.logger.exception(
                    "LN audiobook Nyaa background download failed book_id=%s", book_id
                )
                with lock:
                    jobs[book_id] = {
                        **jobs.get(book_id, {}),
                        "running": False,
                        "state": "failed",
                        "error": str(exc),
                        "finished_at": time.time(),
                    }
                return
            with lock:
                jobs[book_id] = {
                    **jobs.get(book_id, {}),
                    "running": False,
                    "state": "downloading",
                    "result": result,
                    "finished_at": time.time(),
                }

        threading.Thread(
            target=run,
            name=f"ln-audiobook-download-{book_id}",
            daemon=True,
        ).start()
        return dict(job)

    def light_novel_audiobook_download_status(self, book_id: int) -> dict[str, Any]:
        jobs = getattr(self, "_ln_audiobook_download_jobs", {})
        lock = getattr(self, "_ln_audiobook_download_lock", None)
        if lock is None:
            return {}
        with lock:
            return dict(jobs.get(int(book_id)) or {})

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
        self.manager.scan_library(reuse_unchanged=True, user_requested=True)
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
            "audiobook_generation_provider": values.get(
                "ln_audiobook_generation_provider", ln_current.audiobook_generation_provider
            ),
            "audiobook_tts_model": values.get(
                "ln_audiobook_tts_model", ln_current.audiobook_tts_model
            ),
            "audiobook_character_voices": values.get(
                "ln_audiobook_character_voices", ln_current.audiobook_character_voices
            ),
            "irodori_tts_enabled": str(values.get(
                "ln_audiobook_generation_provider", ln_current.audiobook_generation_provider
            ) or "off").strip().lower() != "off",
            "irodori_tts_auto_generate": values.get(
                "ln_irodori_tts_auto_generate", ln_current.irodori_tts_auto_generate
            ),
            "irodori_tts_url": values.get(
                "ln_irodori_tts_url", ln_current.irodori_tts_url
            ),
            "irodori_tts_api_key": values.get(
                "ln_irodori_tts_api_key", ln_current.irodori_tts_api_key
            ),
            "irodori_tts_voice": values.get(
                "ln_irodori_tts_voice", ln_current.irodori_tts_voice
            ),
            "irodori_tts_caption": values.get(
                "ln_irodori_tts_caption", ln_current.irodori_tts_caption
            ),
            "irodori_tts_speed": values.get(
                "ln_irodori_tts_speed", ln_current.irodori_tts_speed
            ),
        }
        old_ocr_enabled = bool(cfg.matching.ocr_image_subtitles)
        old_ocr_counts_as_ready = bool(cfg.matching.ocr_counts_as_ready)
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
        cfg.nyaa.auto_download_current = True
        # Dedicated Torrent On/Off owns this value.  A stale Settings form must
        # never overwrite a click performed while save_settings is in flight.
        cfg.nyaa.torrents_enabled = self._torrent_enabled_state()
        cfg.nyaa.subsplease_rss_enabled = True
        cfg.nyaa.subsplease_rss_preferred = True
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
        cfg.qbittorrent.password = keep_masked_secret(
            values.get("qbt_password", cfg.qbittorrent.password), cfg.qbittorrent.password
        )
        cfg.qbittorrent.api_key = keep_masked_secret(
            values.get("qbt_api_key", cfg.qbittorrent.api_key), cfg.qbittorrent.api_key
        ).strip()
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
        cfg.anilist.access_token = keep_masked_secret(
            values.get("anilist_token", cfg.anilist.access_token), cfg.anilist.access_token
        ).strip()
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
        cfg.shortcuts.mpv_translate_subtitle = str(values.get("shortcut_mpv_translate_subtitle", cfg.shortcuts.mpv_translate_subtitle)).strip()
        requested_study_plugin = str(
            values.get("mpv_study_plugin", cfg.tools.mpv_study_plugin)
        ).strip().casefold()
        cfg.tools.mpv_study_plugin = (
            requested_study_plugin
            if requested_study_plugin in {"auto", "jiten", "jpdb"}
            else "auto"
        )
        cfg.diagnostics.energy_monitoring_enabled = True
        cfg.diagnostics.energy_sample_seconds = 30.0
        personal_jimaku_key = keep_masked_secret(
            values.get("jimaku_api_key", cfg.jimaku.personal_api_key),
            cfg.jimaku.personal_api_key,
        ).strip()
        cfg.jimaku.personal_api_key = personal_jimaku_key
        cfg.jimaku.api_key = personal_jimaku_key
        apply_jimaku_trial(cfg)
        cfg.matching.ocr_image_subtitles = bool(
            values.get("ocr_image_subtitles", cfg.matching.ocr_image_subtitles)
        )
        cfg.matching.ocr_image_subtitles_disabled_by_user = (
            not cfg.matching.ocr_image_subtitles
        )
        cfg.matching.ocr_counts_as_ready = bool(
            values.get("ocr_counts_as_ready", cfg.matching.ocr_counts_as_ready)
        )
        cfg.matching.auto_upgrade_subtitles = True
        cfg.matching.subtitle_upgrade_min_score_gain = 25.0
        cfg.matching.subtitle_upgrade_check_hours = 6.0
        cfg.matching.max_subtitle_upgrade_checks_per_run = 2
        cfg.llm.enabled = bool(values.get("llm_enabled", cfg.llm.enabled))
        cfg.llm.provider = str(values.get("llm_provider", cfg.llm.provider)).strip().lower() or "ollama"
        if cfg.llm.provider not in {"ollama", "openai"}:
            cfg.llm.provider = "ollama"
        cfg.llm.base_url = str(values.get("llm_url", cfg.llm.base_url)).strip().rstrip("/")
        cfg.llm.api_key = keep_masked_secret(
            values.get("llm_api_key", cfg.llm.api_key), cfg.llm.api_key
        ).strip()
        cfg.llm.model = str(values.get("llm_model", cfg.llm.model)).strip()
        reasoning_effort = str(values.get("llm_reasoning_effort", cfg.llm.reasoning_effort)).strip().lower() or "low"
        cfg.llm.reasoning_effort = (
            reasoning_effort
            if reasoning_effort in {"none", "low", "medium", "high", "xhigh", "max"}
            else "low"
        )
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
        self._configure_database_services()
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
            cover_cache_dir=self.config.library.cover_cache_dir,
            ffmpeg=self.config.tools.ffmpeg,
            python=python_executable(),
            stt_model=self.config.sync.japanese_stt_model,
            job_center=self.job_center,
            work_scheduler=self.manager.work_scheduler,
        )
        if not self.safe_mode.active:
            self.task_supervisor.start(
            name="audiobook-stt-resume",
                target=self.audiobooks.resume_pending_transcriptions,
                replace=True,
            )
        self._ui_state_cache.invalidate()
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
        self._ensure_energy_monitor(reason="settings_saved")

        # Settings that affect video readiness are reconciled immediately. The
        # returned state is rendered by the UI before any manual Refresh.
        reconcile_stats: dict[str, int] = {}
        new_ocr_enabled = bool(self.config.matching.ocr_image_subtitles)
        if old_ocr_enabled and not new_ocr_enabled:
            invalidated = self.manager.invalidate_disabled_ocr_subtitles()
            reconcile_stats["ocr_invalidated"] = len(invalidated)
        if old_ocr_counts_as_ready and not self.config.matching.ocr_counts_as_ready:
            reconcile_stats["ocr_ready_demoted"] = self.manager._reconcile_ocr_readiness_policy()

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
        media_id = int(media_id)
        anime = self.manager.db.get_anime(media_id)
        list_status = str(anime.status or "").strip().upper() if anime else ""
        saved_progress = 0
        remote_status = ""
        # A relation/local title with no AniList list status is shown as New.
        # Sending SaveMediaListEntry(progress=0) for it creates a CURRENT entry,
        # so a purely local reset must not contact AniList in that state.
        if list_status:
            remote_status = (
                "CURRENT"
                if list_status in {"COMPLETED", "REPEATING"}
                else list_status
            )
            client = self._anilist_client()
            try:
                saved = client.set_progress(media_id, 0, remote_status)
            finally:
                client.close()
            saved_progress = int(saved.get("progress") or 0)
        reset = self.manager.db.reset_anime_progress(media_id)
        self.logger.info(
            "EVENT step=anime.reset_progress media_id=%s local_episodes=%s "
            "previous_status=%s remote_status=%s",
            media_id,
            reset,
            list_status or "none",
            remote_status or "skipped",
        )
        return {
            "ok": True,
            "progress": saved_progress,
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

    def _global_anilist_aliases(
        self,
        media_ids: set[int],
        *,
        allow_network: bool = True,
    ) -> dict[int, list[str]]:
        cache = getattr(self, "_global_search_anilist_alias_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._global_search_anilist_alias_cache = cache

        missing = sorted(
            int(media_id)
            for media_id in media_ids
            if int(media_id) > 0 and int(media_id) not in cache
        )
        if not missing or not allow_network:
            return cache
        if (
            not self.config.anilist.enabled
            or not self.config.anilist.access_token
        ):
            return cache

        gql = '''
        query($ids:[Int]){
          Page(page:1,perPage:50){
            media(id_in:$ids){
              id
              title{userPreferred romaji english native}
              synonyms
            }
          }
        }
        '''
        for offset in range(0, len(missing), 50):
            chunk = missing[offset : offset + 50]
            try:
                data = self.light_novels._anilist_post(gql, {"ids": chunk})
                returned: set[int] = set()
                for media in (data.get("Page") or {}).get("media") or []:
                    try:
                        media_id = int(media.get("id"))
                    except (TypeError, ValueError):
                        continue
                    returned.add(media_id)
                    titles = media.get("title") or {}
                    names = [
                        titles.get("userPreferred"),
                        titles.get("romaji"),
                        titles.get("english"),
                        titles.get("native"),
                        *(media.get("synonyms") or []),
                    ]
                    cache[media_id] = list(
                        dict.fromkeys(
                            str(value or "").strip()
                            for value in names
                            if str(value or "").strip()
                        )
                    )
                for media_id in chunk:
                    if media_id not in returned:
                        cache.setdefault(media_id, [])
            except Exception as exc:
                self.logger.debug(
                    "Global AniList alias lookup unavailable ids=%s error=%s",
                    chunk,
                    exc,
                )
                break
        return cache

    def global_media_search(
        self,
        query: str,
        limit: int = 40,
    ) -> list[dict[str, Any]]:
        cleaned = re.sub(r"\s+", " ", str(query or "")).strip()[:120]
        if not cleaned:
            return []

        anime_rows = self.manager.db.anime_list()
        ln_state = self.light_novels.state()
        manga_state = self.manga.state()
        audiobook_books = self.audiobooks.search_catalog()
        ln_books = [
            dict(item)
            for item in (ln_state.get("books") or [])
            if isinstance(item, dict)
        ]
        manga_books = [
            dict(item)
            for item in (manga_state.get("books") or [])
            if isinstance(item, dict)
        ]

        linked_ids = {
            int(anime.media_id)
            for anime in anime_rows
            if anime.media_id
        }
        linked_ids.update(
            int(item["anilist_id"])
            for item in [*ln_books, *manga_books, *audiobook_books]
            if item.get("anilist_id")
        )
        # Local search must never wait for AniList.  Cached aliases are useful,
        # while remote discovery is already performed asynchronously by the UI
        # after local results have been rendered.
        aliases = self._global_anilist_aliases(linked_ids, allow_network=False)

        incomplete = self.manager.incomplete_download_paths()
        episodes_by_media: dict[int, list[Any]] = {}
        for item in self.manager.db.episodes():
            if item.media_id is None or not item.video_path.is_file():
                continue
            if self.manager._path_within(item.video_path, incomplete):
                continue
            episodes_by_media.setdefault(int(item.media_id), []).append(item)

        results: list[dict[str, Any]] = []
        episode_hint = re.search(
            r"(?:\b(?:ep(?:isode)?|e|сер(?:ия)?)\s*)?(\d{1,3})\s*$",
            cleaned,
            flags=re.IGNORECASE,
        )
        episode_number = int(episode_hint.group(1)) if episode_hint else None
        episode_prefix = (
            cleaned[: episode_hint.start(1)].rstrip(" .#-_")
            if episode_hint
            else ""
        )

        for anime in anime_rows:
            names = list(
                dict.fromkeys(
                    [
                        anime.title,
                        *anime.titles,
                        *anime.synonyms,
                        *aliases.get(int(anime.media_id), []),
                    ]
                )
            )
            score, matched = _global_search_score(cleaned, names)
            local_eps = episodes_by_media.get(int(anime.media_id), [])

            chosen = next(
                (
                    item
                    for item in local_eps
                    if item.episode is not None
                    and int(item.episode) == int(anime.next_episode)
                ),
                None,
            )
            if chosen is None and local_eps:
                chosen = max(
                    local_eps,
                    key=lambda item: (
                        float(
                            item.playback_updated_at
                            or item.watched_at
                            or 0.0
                        ),
                        int(item.episode or 0),
                    ),
                )

            if score >= 52.0:
                results.append(
                    {
                        "kind": "anime",
                        "media_id": int(anime.media_id),
                        "title": anime.title,
                        "matched_title": matched,
                        "cover": self._cover_uri(anime),
                        "site_url": anime.site_url,
                        "status": anime.status,
                        "page": (
                            "planned"
                            if str(anime.status or "").upper() == "PLANNING"
                            else "current"
                        ),
                        "video_path": str(chosen.video_path) if chosen else "",
                        "episode": chosen.episode if chosen else None,
                        "score": round(score, 3),
                        "priority_tier": (
                            0
                            if chosen is not None
                            and str(chosen.state or "").lower() in {"ready", "watched"}
                            else 1
                            if chosen is not None
                            or str(anime.status or "").upper() != "PLANNING"
                            else 2
                        ),
                    }
                )

            if (
                episode_number is not None
                and len(_global_search_normalize(episode_prefix)) >= 2
            ):
                prefix_score, prefix_match = _global_search_score(
                    episode_prefix,
                    names,
                )
                if prefix_score >= 52.0:
                    exact = next(
                        (
                            item
                            for item in local_eps
                            if item.episode is not None
                            and int(item.episode) == episode_number
                        ),
                        None,
                    )
                    if exact is not None:
                        results.append(
                            {
                                "kind": "episode",
                                "media_id": int(anime.media_id),
                                "title": anime.title,
                                "matched_title": prefix_match,
                                "cover": self._cover_uri(anime),
                                "site_url": anime.site_url,
                                "episode": episode_number,
                                "video_path": str(exact.video_path),
                                "page": "current",
                                "score": round(min(100.0, prefix_score + 6.0), 3),
                                "priority_tier": (
                                    0
                                    if str(exact.state or "").lower()
                                    in {"ready", "watched"}
                                    else 1
                                ),
                            }
                        )

        for book in ln_books:
            names = [
                str(book.get("title") or ""),
                str(book.get("series_title") or ""),
                *aliases.get(int(book.get("anilist_id") or 0), []),
            ]
            score, matched = _global_search_score(cleaned, names)
            if score < 52.0:
                continue
            results.append(
                {
                    "kind": "light_novel",
                    "local_id": int(book.get("id") or 0),
                    "media_id": (
                        int(book["anilist_id"])
                        if book.get("anilist_id")
                        else None
                    ),
                    "title": str(
                        book.get("title")
                        or book.get("series_title")
                        or "Light Novel"
                    ),
                    "matched_title": matched,
                    "cover": str(book.get("cover_url") or ""),
                    "page": "lightnovels",
                    "volume": book.get("volume"),
                    "score": round(score, 3),
                    "priority_tier": 0,
                }
            )

        for book in manga_books:
            names = [
                str(book.get("title") or ""),
                str(book.get("series_title") or ""),
                str(book.get("anilist_title") or ""),
                *aliases.get(int(book.get("anilist_id") or 0), []),
            ]
            score, matched = _global_search_score(cleaned, names)
            if score < 52.0:
                continue
            results.append(
                {
                    "kind": "manga",
                    "local_id": int(book.get("id") or 0),
                    "media_id": (
                        int(book["anilist_id"])
                        if book.get("anilist_id")
                        else None
                    ),
                    "title": str(
                        book.get("title")
                        or book.get("series_title")
                        or "Manga"
                    ),
                    "matched_title": matched,
                    "cover": str(
                        book.get("cover_url")
                        or book.get("local_cover_url")
                        or ""
                    ),
                    "page": "manga",
                    "volume": book.get("volume"),
                    "score": round(score, 3),
                    "priority_tier": 0,
                }
            )

        for book in audiobook_books:
            linked = book.get("linked_light_novel") or {}
            names = [
                str(book.get("title") or ""),
                str(book.get("series_title") or ""),
                str(book.get("anilist_title") or ""),
                str(linked.get("title") or ""),
                *aliases.get(int(book.get("anilist_id") or 0), []),
            ]
            score, matched = _global_search_score(cleaned, names)
            if score < 52.0:
                continue
            results.append(
                {
                    "kind": "audiobook",
                    "local_id": int(book.get("id") or 0),
                    "media_id": int(book["anilist_id"]) if book.get("anilist_id") else None,
                    "title": str(book.get("title") or "Audiobook"),
                    "matched_title": matched,
                    "cover": str(book.get("cover_url") or ""),
                    "page": "audiobooks",
                    "volume": book.get("volume"),
                    "score": round(score, 3),
                    "priority_tier": 0,
                }
            )

        kind_rank = {
            "episode": 0,
            "anime": 1,
            "light_novel": 2,
            "manga": 3,
            "audiobook": 4,
        }
        # pudge-ui-v0.7.23-global-search-priority-v4
        # Search priority:
        #   0 = ready/imported/local
        #   1 = current/local but not fully ready
        #   2 = Planning
        #   3 = remote/random AniList
        # Text score only orders items inside the same bucket.
        results.sort(
            key=lambda item: (
                int(item.get("priority_tier", 3)),
                -float(item.get("score") or 0.0),
                kind_rank.get(str(item.get("kind") or ""), 9),
                str(item.get("title") or "").casefold(),
            )
        )

        return results[: max(1, min(100, int(limit or 40)))]

    def test_saved_credentials(
        self,
        kinds: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        requested = {
            str(value or "").strip().lower()
            for value in (kinds or [])
            if str(value or "").strip()
        }
        checks: list[dict[str, Any]] = []

        if "jimaku" in requested:
            key = str(self.config.jimaku.personal_api_key or "").strip()
            if not key:
                checks.append({"service": "Jimaku", "status": "missing"})
            else:
                try:
                    response = httpx.get(
                        f"{self.config.jimaku.base_url.rstrip('/')}/api/entries/search",
                        headers={
                            "Authorization": key,
                            "Accept": "application/json",
                            "User-Agent": APP_SLUG,
                        },
                        params={"anime": "true", "query": "Frieren"},
                        timeout=8.0,
                        follow_redirects=True,
                    )
                    if response.status_code in {401, 403}:
                        checks.append({"service": "Jimaku", "status": "invalid"})
                    elif response.status_code == 429:
                        checks.append({"service": "Jimaku", "status": "rate_limited"})
                    else:
                        response.raise_for_status()
                        payload = response.json()
                        count = len(payload) if isinstance(payload, list) else 0
                        checks.append(
                            {
                                "service": "Jimaku",
                                "status": "ok" if count > 0 else "empty",
                                "count": count,
                            }
                        )
                except httpx.HTTPError as exc:
                    checks.append(
                        {
                            "service": "Jimaku",
                            "status": "unreachable",
                            "detail": str(exc),
                        }
                    )
                except ValueError as exc:
                    checks.append(
                        {
                            "service": "Jimaku",
                            "status": "error",
                            "detail": str(exc),
                        }
                    )

        if "anilist" in requested:
            token = str(self.config.anilist.access_token or "").strip()
            if not token:
                checks.append({"service": "AniList", "status": "missing"})
            else:
                try:
                    response = httpx.post(
                        self.config.anilist.endpoint,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        },
                        json={"query": "query{Viewer{id name}}"},
                        timeout=8.0,
                    )
                    if response.status_code in {401, 403}:
                        checks.append({"service": "AniList", "status": "invalid"})
                    else:
                        response.raise_for_status()
                        payload = response.json()
                        viewer = (
                            (payload.get("data") or {}).get("Viewer")
                            if isinstance(payload, dict)
                            else None
                        )
                        checks.append(
                            {
                                "service": "AniList",
                                "status": "ok" if viewer else "empty",
                            }
                        )
                except httpx.HTTPError as exc:
                    checks.append(
                        {
                            "service": "AniList",
                            "status": "unreachable",
                            "detail": str(exc),
                        }
                    )
                except ValueError as exc:
                    checks.append(
                        {
                            "service": "AniList",
                            "status": "error",
                            "detail": str(exc),
                        }
                    )

        for backend, service in (("jiten", "Jiten"), ("jpdb", "JPDB")):
            if backend not in requested:
                continue
            settings = self.light_novels.settings()
            token = (
                settings.jiten_api_key
                if backend == "jiten"
                else settings.jpdb_api_token
            )
            if not str(token or "").strip():
                checks.append({"service": service, "status": "missing"})
                continue
            try:
                self.light_novels.test_study(backend)
                checks.append({"service": service, "status": "ok"})
            except Exception as exc:
                detail = str(exc)
                lowered = detail.casefold()
                status = (
                    "invalid"
                    if "401" in lowered
                    or "403" in lowered
                    or "unauthorized" in lowered
                    or "forbidden" in lowered
                    else "unreachable"
                    if "timeout" in lowered
                    or "connect" in lowered
                    or "network" in lowered
                    else "error"
                )
                checks.append(
                    {
                        "service": service,
                        "status": status,
                        "detail": detail,
                    }
                )

        return checks

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

    def _jiten_refresh_worker(
        self,
        job_id: str,
        ln_pending: list[tuple[str, str]],
        manga_pending: list[str],
    ) -> None:
        parsed_ln = 0
        parsed_manga = 0
        errors: list[str] = []
        total = len(ln_pending) + len(manga_pending)
        try:
            self.job_center.update(job_id, state="running", total=total, message="Refreshing Jiten parses")
            for index, (text, digest) in enumerate(ln_pending, 1):
                state = self.job_center.get(job_id) or {}
                if str(state.get("state") or "") == "cancel_requested":
                    self.job_center.cancelled(job_id)
                    return
                try:
                    self.light_novels.jiten_preparse(text, digest)
                    parsed_ln += 1
                except Exception as exc:
                    errors.append(f"LN: {exc}")
                self.job_center.update(
                    job_id,
                    state="running",
                    current=index,
                    total=total,
                    message=f"Jiten LN {index}/{len(ln_pending)}",
                )
            offset = len(ln_pending)
            for index, text in enumerate(manga_pending, 1):
                state = self.job_center.get(job_id) or {}
                if str(state.get("state") or "") == "cancel_requested":
                    self.job_center.cancelled(job_id)
                    return
                try:
                    self.light_novels.jiten_preparse(text)
                    parsed_manga += 1
                except Exception as exc:
                    errors.append(f"Manga: {exc}")
                self.job_center.update(
                    job_id,
                    state="running",
                    current=offset + index,
                    total=total,
                    message=f"Jiten manga OCR {index}/{len(manga_pending)}",
                )
            self.job_center.finish(
                job_id,
                message=f"Jiten refreshed: LN {parsed_ln}, manga {parsed_manga}",
                result={"light_novel_parses": parsed_ln, "manga_parses": parsed_manga, "errors": errors[:50]},
            )
        except Exception as exc:
            self.logger.exception("FAIL step=jiten.manual_refresh error=%r", str(exc))
            self.job_center.fail(job_id, exc)
        finally:
            with self._jiten_refresh_lock:
                if self._jiten_refresh_job_id == job_id:
                    self._jiten_refresh_thread = None
                    self._jiten_refresh_job_id = ""

    def refresh_jiten_data(self) -> dict[str, Any]:
        """Invalidate Jiten metadata immediately and lazily parse local reading material."""
        if not str(self.light_novels.settings().jiten_api_key or "").strip():
            raise LightNovelError("Jiten API key is not configured")
        invalidated = self.light_novels.invalidate_jiten_media_stats()
        with self._jiten_refresh_lock:
            if self._jiten_refresh_thread is not None and self._jiten_refresh_thread.is_alive():
                return {"started": False, "running": True, "job_id": self._jiten_refresh_job_id, "invalidated": invalidated}
            ln_pending = self.light_novels.jiten_unparsed_chapters()
            manga_pending: list[str] = []
            try:
                with self.manager.db.connect() as conn:
                    rows = conn.execute(
                        "SELECT DISTINCT text FROM manga_ocr_cache "
                        "WHERE TRIM(COALESCE(text,''))<>'' AND region_key<>'full' ORDER BY book_id,page_index"
                    ).fetchall()
                seen: set[str] = set()
                for row in rows:
                    text = str(row["text"] or "").strip()[:20000]
                    if text and text not in seen:
                        seen.add(text)
                        digest = hashlib.sha256(("study-v1\0" + text).encode("utf-8")).hexdigest()
                        if self.light_novels._cached_parse(digest) is None:
                            manga_pending.append(text)
            except Exception as exc:
                self.logger.info("Jiten manga preparse discovery skipped: %s", exc)
            job_id = self.job_center.start(
                "jiten_refresh",
                "Refresh Jiten data",
                payload={"light_novels": len(ln_pending), "manga_regions": len(manga_pending)},
                total=len(ln_pending) + len(manga_pending),
            )
            thread = threading.Thread(
                target=self._jiten_refresh_worker,
                args=(job_id, ln_pending, manga_pending),
                name=f"{APP_SLUG}-jiten-refresh",
                daemon=True,
            )
            self._jiten_refresh_job_id = job_id
            self._jiten_refresh_thread = thread
            thread.start()
        return {
            "started": True,
            "running": True,
            "job_id": job_id,
            "invalidated": invalidated,
            "light_novels": len(ln_pending),
            "manga_regions": len(manga_pending),
        }

    def _irodori_install_root(self) -> Path:
        return DATA_DIR / "optional" / "irodori-tts-server"

    def _irodori_repo_dir(self) -> Path:
        return self._irodori_install_root() / "repo"

    def _irodori_install_log_path(self) -> Path:
        return DEFAULT_LOG_PATH.with_name(f"{APP_SLUG}-irodori-install.log")

    def _irodori_server_log_path(self) -> Path:
        return DEFAULT_LOG_PATH.with_name(f"{APP_SLUG}-irodori-server.log")

    def _irodori_managed_python(self) -> Path:
        return self._irodori_repo_dir() / ".venv" / "bin" / "python"

    @staticmethod
    def _irodori_local_url(url: str) -> bool:
        try:
            parsed = urlparse(str(url or ""))
        except ValueError:
            return False
        return parsed.scheme in {"http", "https"} and (parsed.hostname or "").lower() in {
            "127.0.0.1", "localhost", "::1"
        }

    def _irodori_git_revision(self) -> str:
        repo = self._irodori_repo_dir()
        if not (repo / ".git").is_dir():
            return ""
        try:
            return subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            ).strip()
        except Exception:
            return ""

    def _irodori_remote_revision(self, *, refresh: bool = False) -> str:
        now = time.time()
        if (
            not refresh
            and self._irodori_remote_revision_cache
            and now - self._irodori_remote_revision_checked_at < 900
        ):
            return self._irodori_remote_revision_cache
        repo = self._irodori_repo_dir()
        if not (repo / ".git").is_dir():
            return ""
        git = shutil.which("git") or ("/usr/bin/git" if Path("/usr/bin/git").is_file() else "")
        if not git:
            return ""
        try:
            output = subprocess.check_output(
                [git, "-C", str(repo), "ls-remote", "origin", "HEAD"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=8,
            ).strip()
            revision = output.split()[0] if output else ""
        except Exception:
            revision = ""
        self._irodori_remote_revision_checked_at = now
        if revision:
            self._irodori_remote_revision_cache = revision
        return revision

    def irodori_install_status(self, refresh: bool = False) -> dict[str, Any]:
        with self._irodori_install_lock:
            state = dict(self._irodori_install_state)
            thread = self._irodori_install_thread
            running = bool(thread is not None and thread.is_alive())
        installed = self._irodori_managed_python().is_file()
        if running:
            state["running"] = True
        elif installed and state.get("state") != "failed":
            state.update({"state": "ready", "running": False, "detail": ""})
        elif not installed and state.get("state") not in {"failed"}:
            state.update({"state": "not_installed", "running": False, "detail": ""})
        local_full = ""
        if installed:
            try:
                local_full = subprocess.check_output(
                    ["git", "-C", str(self._irodori_repo_dir()), "rev-parse", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=3,
                ).strip()
            except Exception:
                local_full = ""
        remote_full = "" if running or not installed else self._irodori_remote_revision(refresh=refresh)
        state["installed"] = installed
        state["revision"] = local_full[:8] if local_full else ""
        state["remote_revision"] = remote_full[:8] if remote_full else ""
        state["update_available"] = bool(local_full and remote_full and local_full != remote_full)
        state["install_dir"] = str(self._irodori_install_root())
        state["log_path"] = str(self._irodori_install_log_path())
        return state

    def _set_irodori_install_state(self, state: str, detail: str = "") -> None:
        with self._irodori_install_lock:
            self._irodori_install_state["state"] = state
            self._irodori_install_state["detail"] = detail
            if state in {"ready", "failed"}:
                self._irodori_install_state["finished_at"] = time.time()

    def _irodori_uv(self, log: Any) -> Path:
        candidates = [
            shutil.which("uv"),
            str(Path.home() / ".local" / "bin" / "uv"),
            str(Path.home() / ".cargo" / "bin" / "uv"),
        ]
        for raw in candidates:
            if raw and Path(raw).is_file():
                return Path(raw)
        root = self._irodori_install_root() / "uv-bootstrap"
        python = root / "bin" / "python"
        uv = root / "bin" / "uv"
        if uv.is_file():
            return uv
        root.parent.mkdir(parents=True, exist_ok=True)
        self._set_irodori_install_state("installing_uv", "Installing uv in a private bootstrap environment")
        completed = subprocess.run(
            [python_executable(), "-m", "venv", str(root)],
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=10 * 60,
        )
        if completed.returncode != 0 or not python.is_file():
            raise RuntimeError(f"failed to create uv bootstrap environment (exit {completed.returncode})")
        completed = subprocess.run(
            [str(python), "-m", "pip", "install", "--upgrade", "uv"],
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=15 * 60,
        )
        if completed.returncode != 0 or not uv.is_file():
            raise RuntimeError(f"failed to install uv (exit {completed.returncode})")
        return uv

    def _run_irodori_install(self) -> None:
        root = self._irodori_install_root()
        repo = self._irodori_repo_dir()
        log_path = self._irodori_install_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("w", encoding="utf-8") as log:
                git = shutil.which("git") or ("/usr/bin/git" if Path("/usr/bin/git").is_file() else "")
                if not git:
                    raise RuntimeError("git is required to install Irodori")
                uv = self._irodori_uv(log)
                if (repo / ".git").is_dir():
                    self._set_irodori_install_state("updating_source", "Updating Irodori-TTS-Server")
                    commands = [
                        [git, "-C", str(repo), "fetch", "--depth", "1", "origin", "main"],
                        [git, "-C", str(repo), "reset", "--hard", "origin/main"],
                    ]
                else:
                    if repo.exists():
                        shutil.rmtree(repo)
                    self._set_irodori_install_state("cloning", "Downloading Irodori-TTS-Server")
                    commands = [[git, "clone", "--depth", "1", "--branch", "main", "https://github.com/Aratako/Irodori-TTS-Server.git", str(repo)]]
                for command in commands:
                    log.write("$ " + " ".join(map(str, command)) + "\n")
                    log.flush()
                    completed = subprocess.run(
                        command,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        check=False,
                        timeout=15 * 60,
                    )
                    if completed.returncode != 0:
                        raise RuntimeError(f"Irodori source command failed with exit {completed.returncode}")
                example = repo / ".env.example"
                env_file = repo / ".env"
                if example.is_file() and not env_file.exists():
                    shutil.copy2(example, env_file)
                self._set_irodori_install_state("installing_dependencies", "Installing Irodori dependencies (macOS CPU/MPS backend)")
                command = [str(uv), "sync", "--locked", "--no-dev", "--extra", "cpu"]
                log.write("$ " + " ".join(command) + "\n")
                log.flush()
                completed = subprocess.run(
                    command,
                    cwd=repo,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=90 * 60,
                    env={**os.environ, "UV_LINK_MODE": "copy"},
                )
                if completed.returncode != 0:
                    raise RuntimeError(f"uv sync exited with code {completed.returncode}")
                if not self._irodori_managed_python().is_file():
                    raise RuntimeError("Irodori environment was not created")
                self._irodori_remote_revision_cache = ""
                self._irodori_remote_revision_checked_at = 0.0
                self._set_irodori_install_state("ready", "")
        except Exception as exc:
            self.logger.exception("FAIL step=irodori.install error=%r", str(exc))
            self._set_irodori_install_state("failed", str(exc))
            if platform.system() == "Darwin" and log_path.exists():
                subprocess.run(["open", "-R", str(log_path)], check=False)

    def install_irodori_tts(self) -> dict[str, Any]:
        with self._irodori_install_lock:
            thread = self._irodori_install_thread
            if thread is None or not thread.is_alive():
                self._irodori_install_state = {
                    "state": "starting",
                    "detail": "Starting Irodori installation",
                    "started_at": time.time(),
                    "finished_at": 0.0,
                }
                thread = threading.Thread(
                    target=self._run_irodori_install,
                    name=f"{APP_SLUG}-irodori-install",
                    daemon=True,
                )
                self._irodori_install_thread = thread
                thread.start()
        return self.irodori_install_status()

    def reveal_irodori_install_log(self) -> dict[str, Any]:
        path = self._irodori_install_log_path()
        if not path.exists():
            return {"ok": False, "path": str(path)}
        if platform.system() == "Darwin":
            subprocess.run(["open", "-R", str(path)], check=False)
        return {"ok": True, "path": str(path)}

    def _start_managed_irodori_server(self, url: str) -> bool:
        if not self._irodori_local_url(url) or not self._irodori_managed_python().is_file():
            return False
        parsed = urlparse(url)
        port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
        if parsed.scheme == "http" and port == 80 and not parsed.port:
            port = 8088
        with self._irodori_server_lock:
            process = self._irodori_server_process
            if process is not None and process.poll() is None:
                return True
            log_path = self._irodori_server_log_path()
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("a", encoding="utf-8")
            try:
                managed_env = {
                    **os.environ,
                    "IRODORI_ALLOW_NO_REF_VOICE": "true",
                    "IRODORI_MODEL_NAME": "irodori-tts",
                }
                self._irodori_server_process = subprocess.Popen(
                    [
                        str(self._irodori_managed_python()),
                        "-m",
                        "irodori_openai_tts",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    cwd=self._irodori_repo_dir(),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    start_new_session=True,
                    env=managed_env,
                )
                self.logger.info(
                    "START step=irodori.managed_server pid=%s reason=generation_or_explicit_test",
                    self._irodori_server_process.pid,
                )
            finally:
                log.close()
        return True

    def _stop_managed_irodori_server(self) -> None:
        # Stop only the process group created by this Pudge instance.
        with self._irodori_server_lock:
            process = self._irodori_server_process
            self._irodori_server_process = None
        if process is None or process.poll() is not None:
            return

        import signal

        pid = int(process.pid)
        try:
            pgid = os.getpgid(pid)
        except (ProcessLookupError, OSError):
            pgid = None

        try:
            if pgid is not None:
                os.killpg(pgid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                if pgid is not None:
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    process.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, ChildProcessError):
                pass
        except (ProcessLookupError, ChildProcessError, OSError):
            pass
        finally:
            self.logger.info("STOP step=irodori.managed_server pid=%s", pid)

    def _irodori_server_failure_detail(self) -> str:
        process = self._irodori_server_process
        if process is None:
            return ""
        code = process.poll()
        if code is None:
            return ""
        tail = ""
        log_path = self._irodori_server_log_path()
        if log_path.is_file():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                tail = "\n".join(lines[-20:]).strip()
            except OSError:
                tail = ""
        detail = f"Managed Irodori server exited with code {code}"
        if tail:
            detail += f"\nServer log tail:\n{tail}"
        return detail

    def _irodori_health(self, url: str, key: str, timeout: float = 5.0) -> httpx.Response:
        response = httpx.get(f"{url}/health", headers=self._irodori_headers(key), timeout=timeout)
        response.raise_for_status()
        return response

    @staticmethod
    def _irodori_headers(api_key: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if str(api_key or "").strip():
            headers["Authorization"] = f"Bearer {str(api_key).strip()}"
        return headers

    def test_irodori_tts(
        self,
        values: dict[str, Any] | None = None,
        *,
        keep_managed_alive: bool = False,
    ) -> dict[str, Any]:
        settings = self.light_novels.settings()
        values = values or {}
        url = str(values.get("url") or settings.irodori_tts_url or "http://127.0.0.1:8088").rstrip("/")
        key = str(values.get("api_key") or settings.irodori_tts_api_key or "")
        started_here = False
        try:
            try:
                response = self._irodori_health(url, key, timeout=2.0)
            except Exception:
                if not self._start_managed_irodori_server(url):
                    raise
                started_here = True
                deadline = time.monotonic() + 120.0
                last_error: Exception | None = None
                while time.monotonic() < deadline:
                    process_error = self._irodori_server_failure_detail()
                    if process_error:
                        raise RuntimeError(process_error)
                    try:
                        response = self._irodori_health(url, key, timeout=2.0)
                        break
                    except Exception as exc:
                        last_error = exc
                        time.sleep(0.5)
                else:
                    log_path = self._irodori_server_log_path()
                    raise RuntimeError(
                        f"Managed Irodori server did not become ready after 120s: {last_error}. "
                        f"Server log: {log_path}"
                    )
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            return {
                "ok": True,
                "url": url,
                "health": payload if isinstance(payload, dict) else {},
                "managed": self._irodori_local_url(url) and self._irodori_managed_python().is_file(),
            }
        finally:
            if started_here and not keep_managed_alive:
                self._stop_managed_irodori_server()

    def test_llm_provider(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        values = values or {}
        provider = str(values.get("provider") or self.config.llm.provider or "ollama").strip().lower()
        if provider not in {"ollama", "openai"}:
            raise RuntimeError(f"Unsupported LLM provider: {provider}")
        base_url = str(values.get("url") or self.config.llm.base_url or "").strip().rstrip("/")
        api_key = str(values.get("api_key") or "")
        if api_key == "••••••••":
            api_key = self.config.llm.api_key
        model = str(values.get("model") or self.config.llm.model or "").strip()
        reasoning_effort = str(
            values.get("reasoning_effort") or self.config.llm.reasoning_effort or "low"
        ).strip().lower()
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            reasoning_effort = "low"
        if not base_url:
            raise RuntimeError("LLM URL is empty")

        models: list[str] = []
        model_list_error: Exception | None = None
        try:
            models = list_models(base_url, api_key, timeout=8.0, provider=provider)
        except Exception as exc:
            model_list_error = exc

        # Some OpenAI-compatible gateways intentionally do not expose /v1/models.
        # When a model was supplied, verify the endpoint that Pudge actually uses.
        if model:
            cfg = replace(
                self.config.llm,
                enabled=True,
                provider=provider,
                base_url=base_url,
                api_key=api_key,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            client = OllamaClient(cfg)
            try:
                reply = client.json_chat(
                    "Return strict JSON only.",
                    '{"task":"connectivity_check","reply":{"ok":true}}',
                )
            finally:
                client.close()
            if not isinstance(reply, dict):
                chat_detail = str(getattr(client, "last_error", "") or "").strip()
                if chat_detail:
                    raise RuntimeError(f"LLM chat completion failed: {chat_detail}")
                detail = f": {model_list_error}" if model_list_error else ""
                raise RuntimeError(f"LLM chat completion failed{detail}")
            return {"ok": True, "provider": provider, "model": model, "models": models}

        if model_list_error is not None:
            raise RuntimeError(f"LLM model listing failed: {model_list_error}")
        if not models:
            raise RuntimeError("LLM server returned no models")
        return {"ok": True, "provider": provider, "model": "", "models": models}

    def _audiobook_generation_runtime(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        settings = self.light_novels.settings()
        values = values or {}
        provider = str(values.get("provider") or settings.audiobook_generation_provider or "off").strip().lower()
        if provider == "irodori":
            return {
                "provider": "irodori",
                "base_url": "http://127.0.0.1:8088",
                "api_key": "",
                "model": "irodori-tts",
                "voice": "none",
                "caption": str(values.get("caption", settings.irodori_tts_caption) or "").strip(),
                "speed": float(values.get("speed", settings.irodori_tts_speed) or 1.0),
                "managed": True,
            }
        if provider == "external":
            return {
                "provider": "external",
                "base_url": str(values.get("url") or settings.irodori_tts_url or "").strip().rstrip("/"),
                "api_key": str(values.get("api_key") or settings.irodori_tts_api_key or ""),
                "model": str(values.get("model") or settings.audiobook_tts_model or "tts-1").strip() or "tts-1",
                "voice": str(values.get("voice") or settings.irodori_tts_voice or "alloy").strip() or "alloy",
                "caption": "",
                "speed": float(values.get("speed", settings.irodori_tts_speed) or 1.0),
                "managed": False,
            }
        raise LightNovelError("Audiobook generation is disabled in Settings")

    def test_audiobook_generation(self, values: dict[str, Any] | None = None) -> dict[str, Any]:
        values = values or {}
        runtime = self._audiobook_generation_runtime(values)
        provider = runtime["provider"]
        if provider == "irodori":
            result = self.test_irodori_tts({"url": "http://127.0.0.1:8088", "api_key": ""})
            return {**result, "provider": "irodori"}
        url = str(runtime["base_url"]).strip().rstrip("/")
        key = str(runtime["api_key"] or "")
        if not url:
            raise LightNovelError("External audiobook API URL is empty")
        headers = self._irodori_headers(key)
        last_error: Exception | None = None
        for endpoint in ("/v1/models", "/health"):
            try:
                response = httpx.get(f"{url}{endpoint}", headers=headers, timeout=5.0)
                response.raise_for_status()
                return {"ok": True, "provider": "external", "url": url}
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"External audiobook API is not reachable: {last_error}")

    @staticmethod
    def _tts_http_error(response: httpx.Response, chapter_index: int) -> RuntimeError:
        detail = ""
        try:
            payload = response.json()
            detail = json.dumps(payload, ensure_ascii=False)
        except Exception:
            detail = response.text.strip()
        detail = detail[:1600]
        return RuntimeError(
            f"Audiobook API rejected chapter {chapter_index}: HTTP {response.status_code}"
            + (f" — {detail}" if detail else "")
        )

    @staticmethod
    def _split_audiobook_tts_text(text: str, *, max_chars: int = 3600) -> list[tuple[str, int]]:
        """Split long chapter text below OpenAI/Irodori request limits.

        The second tuple item is the number of original source characters consumed,
        so progress remains character-based even when whitespace is trimmed for TTS.
        """
        source = str(text or "")
        if not source.strip():
            return []
        max_chars = max(256, min(4000, int(max_chars)))
        boundaries = "\n\r。！？!?．.、，,;；:："
        chunks: list[tuple[str, int]] = []
        cursor = 0
        while cursor < len(source):
            hard_end = min(len(source), cursor + max_chars)
            end = hard_end
            if hard_end < len(source):
                window = source[cursor:hard_end]
                candidates = [window.rfind(mark) for mark in boundaries]
                boundary = max(candidates, default=-1)
                # Avoid pathological tiny chunks when the only punctuation is near
                # the beginning of a long paragraph.
                if boundary >= max(160, max_chars // 3):
                    end = cursor + boundary + 1
            raw = source[cursor:end]
            spoken = raw.strip()
            consumed = end - cursor
            if spoken:
                chunks.append((spoken, consumed))
            elif chunks:
                previous, previous_consumed = chunks[-1]
                chunks[-1] = (previous, previous_consumed + consumed)
            cursor = end
        # Preserve trailing whitespace in the progress denominator.
        consumed_total = sum(item[1] for item in chunks)
        if chunks and consumed_total < len(source):
            spoken, consumed = chunks[-1]
            chunks[-1] = (spoken, consumed + len(source) - consumed_total)
        return chunks

    def _merge_audiobook_parts(self, parts: list[Path], output: Path) -> None:
        if not parts:
            raise RuntimeError("Audiobook generation returned no audio parts")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".merge.part.mp3")
        temporary.unlink(missing_ok=True)
        if len(parts) == 1:
            shutil.copy2(parts[0], temporary)
        else:
            concat_list = output.with_suffix(".concat.txt")
            lines = []
            for part in parts:
                escaped = str(part.resolve()).replace("\\", "\\\\").replace("'", "'\\''")
                lines.append(f"file '{escaped}'")
            concat_list.write_text("\n".join(lines) + "\n", encoding="utf-8")
            try:
                completed = subprocess.run(
                    [
                        str(self.config.tools.ffmpeg), "-hide_banner", "-loglevel", "error",
                        "-nostdin", "-y", "-f", "concat", "-safe", "0", "-i",
                        str(concat_list), "-c", "copy", str(temporary),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                    timeout=180,
                )
                if completed.returncode != 0:
                    raise RuntimeError(
                        "ffmpeg could not join audiobook chunks: "
                        + str(completed.stderr or "").strip()[-1600:]
                    )
            finally:
                concat_list.unlink(missing_ok=True)
        if not temporary.is_file() or temporary.stat().st_size < 1024:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("Joined audiobook chapter is unexpectedly small")
        temporary.replace(output)

    @staticmethod
    def _irodori_voice_seed(speaker: str, caption: str) -> int:
        """Stable positive seed for one logical voice identity.

        The line/chapter/volume is deliberately absent: the same character and
        canonical Voice Design caption must start from the same sampling state.
        """
        identity = re.sub(r"\s+", " ", str(speaker or "narrator")).strip().casefold() or "narrator"
        voice = re.sub(r"\s+", " ", str(caption or "")).strip()
        digest = hashlib.sha256(f"pudge-irodori-voice-v1\0{identity}\0{voice}".encode("utf-8")).digest()
        return 1 + (int.from_bytes(digest[:8], "big") % 2_147_483_646)

    def _post_audiobook_speech(
        self,
        client: httpx.Client,
        runtime: dict[str, Any],
        text: str,
        chapter_index: int,
        *,
        caption: str | None = None,
        seed: int | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {
            "model": runtime["model"],
            "input": text,
            "voice": runtime["voice"],
            "response_format": "mp3",
            "speed": runtime["speed"],
        }
        effective_caption = str(runtime.get("caption") if caption is None else caption or "").strip()
        if runtime["provider"] == "irodori":
            irodori: dict[str, Any] = {}
            if effective_caption:
                irodori["caption"] = effective_caption
                # Current Irodori-TTS-Server exposes Voice Design at the top level;
                # retain the nested extension for older Pudge-compatible servers.
                body["caption"] = effective_caption
            if seed is not None:
                stable_seed = max(1, int(seed))
                irodori["seed"] = stable_seed
                body["seed"] = stable_seed
            if irodori:
                body["irodori"] = irodori
        response = client.post(
            f"{runtime['base_url']}/v1/audio/speech",
            headers=self._irodori_headers(str(runtime.get("api_key") or "")),
            json=body,
        )
        status_code = int(getattr(response, "status_code", 200) or 200)
        if status_code == 422 and runtime["provider"] == "irodori" and seed is not None:
            # Compatibility retry for servers that know caption conditioning but
            # predate deterministic request seeds.
            self.logger.warning(
                "FALLBACK step=audiobook_generation.seed_compat chapter=%s detail=%r",
                chapter_index,
                str(getattr(response, "text", ""))[:1200],
            )
            fallback = dict(body)
            fallback.pop("seed", None)
            nested = dict(fallback.get("irodori") or {})
            nested.pop("seed", None)
            if nested:
                fallback["irodori"] = nested
            else:
                fallback.pop("irodori", None)
            response = client.post(
                f"{runtime['base_url']}/v1/audio/speech",
                headers=self._irodori_headers(str(runtime.get("api_key") or "")),
                json=fallback,
            )
            body = fallback
            status_code = int(getattr(response, "status_code", 200) or 200)
        if status_code == 422 and runtime["provider"] == "irodori" and ("irodori" in body or "caption" in body):
            # Older managed-server schemas did not expose Voice Design yet. Keep
            # generation usable, but only after preserving the exact rejection in logs.
            self.logger.warning(
                "FALLBACK step=audiobook_generation.caption_compat chapter=%s detail=%r",
                chapter_index,
                str(getattr(response, "text", ""))[:1200],
            )
            fallback = dict(body)
            fallback.pop("irodori", None)
            fallback.pop("caption", None)
            response = client.post(
                f"{runtime['base_url']}/v1/audio/speech",
                headers=self._irodori_headers(str(runtime.get("api_key") or "")),
                json=fallback,
            )
            status_code = int(getattr(response, "status_code", 200) or 200)
        if runtime["provider"] == "irodori" and not bool(getattr(response, "is_error", status_code >= 400)):
            response_seed = str(getattr(response, "headers", {}).get("X-Irodori-Seed") or getattr(response, "headers", {}).get("X-Seed") or "").strip()
            if response_seed:
                self.logger.debug("RESULT step=audiobook_generation.seed chapter=%s seed=%s", chapter_index, response_seed)
        if bool(getattr(response, "is_error", status_code >= 400)):
            raise self._tts_http_error(response, chapter_index)
        return response

    @staticmethod
    def _audiobook_voice_units(text: str) -> list[dict[str, Any]]:
        """Split LN text into exact narration/dialogue spans without rewriting it."""
        source = str(text or "")
        if not source:
            return []
        openers = {"「": "」", "『": "』"}
        stack: list[str] = []
        start = 0
        dialogue_start: int | None = None
        result: list[dict[str, Any]] = []

        def append_span(left: int, right: int, dialogue: bool) -> None:
            if right <= left:
                return
            raw = source[left:right]
            result.append(
                {
                    "id": len(result),
                    "text": raw,
                    "dialogue": bool(dialogue),
                    "consumed": len(raw),
                }
            )

        for index, char in enumerate(source):
            if char in openers:
                if not stack:
                    append_span(start, index, False)
                    dialogue_start = index
                stack.append(openers[char])
                continue
            if stack and char == stack[-1]:
                stack.pop()
                if not stack and dialogue_start is not None:
                    append_span(dialogue_start, index + 1, True)
                    start = index + 1
                    dialogue_start = None
        if stack and dialogue_start is not None:
            append_span(dialogue_start, len(source), True)
        else:
            append_span(start, len(source), False)
        return result

    def _audiobook_speaker_cache_context(self, book_id: int) -> dict[str, Any]:
        """Return series-scoped speaker cache paths, migrating old per-volume data.

        Voice identity belongs to a light-novel series, not one imported volume.
        Chapter assignments remain volume-specific because their numeric span ids are
        tied to the exact source text of that volume.
        """
        book_id = int(book_id)
        try:
            book = self.light_novels.book(book_id)
            series_key = str(book.get("series_key") or f"book:{book_id}")
            series_title = str(book.get("series_title") or book.get("title") or f"Light Novel {book_id}")
        except Exception:
            book = {"id": book_id, "title": f"Light Novel {book_id}"}
            series_key = f"book:{book_id}"
            series_title = str(book["title"])
        root = Path(self.config.paths.cache_dir) / "audiobook-speakers"
        series_digest = hashlib.sha256(series_key.encode("utf-8")).hexdigest()[:24]
        series_dir = root / "series" / series_digest
        book_dir = series_dir / "books" / str(book_id)
        series_dir.mkdir(parents=True, exist_ok=True)
        book_dir.mkdir(parents=True, exist_ok=True)
        profiles_path = series_dir / "profiles.json"

        # v92-v94 stored profiles and chapter caches under the individual volume.
        # Merge once so characters already learned in an older volume immediately
        # become reusable by every other volume in the same series.
        legacy_dir = root / str(book_id)
        migrated_marker = book_dir / ".legacy-migrated-v1"
        if legacy_dir.is_dir() and not migrated_marker.exists():
            existing = self._read_audiobook_speaker_profiles(profiles_path)
            legacy_profiles = self._read_audiobook_speaker_profiles(legacy_dir / "profiles.json")
            changed = False
            for speaker, caption in legacy_profiles.items():
                if speaker not in existing and caption:
                    existing[speaker] = caption
                    changed = True
            if changed:
                self._write_audiobook_speaker_profiles(profiles_path, existing, series_key, series_title)
            for source in legacy_dir.glob("chapter-*.json"):
                target = book_dir / source.name
                if not target.exists():
                    try:
                        shutil.copy2(source, target)
                    except OSError:
                        pass
            try:
                migrated_marker.touch(exist_ok=True)
            except OSError:
                pass
        return {
            "book": book,
            "series_key": series_key,
            "series_title": series_title,
            "series_dir": series_dir,
            "book_dir": book_dir,
            "profiles_path": profiles_path,
        }

    def _audiobook_speaker_cache_dir(self, book_id: int) -> Path:
        # Kept as a compatibility helper for existing tests/callers. New data is
        # volume-specific under a series-scoped root.
        return Path(self._audiobook_speaker_cache_context(book_id)["book_dir"])

    def _audiobook_character_profiles_path(self) -> Path:
        """Global AniList-character voice registry shared by every LN series."""
        return Path(self.config.paths.cache_dir) / "audiobook-speakers" / "characters" / "profiles.json"

    @staticmethod
    def _read_audiobook_character_profiles(path: Path) -> dict[int, dict[str, str]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        raw = payload.get("characters", {})
        if not isinstance(raw, dict):
            return {}
        result: dict[int, dict[str, str]] = {}
        for raw_id, raw_value in raw.items():
            try:
                character_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if character_id <= 0:
                continue
            if isinstance(raw_value, str):
                caption = raw_value.strip()
                name = ""
            elif isinstance(raw_value, dict):
                caption = str(raw_value.get("caption") or "").strip()
                name = str(raw_value.get("name") or "").strip()
            else:
                continue
            if caption:
                result[character_id] = {"name": name, "caption": caption}
        return result

    @staticmethod
    def _write_audiobook_character_profiles(path: Path, profiles: dict[int, dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "scope": "anilist-character",
            "characters": {
                str(character_id): {
                    "name": str(value.get("name") or ""),
                    "caption": str(value.get("caption") or ""),
                }
                for character_id, value in sorted(profiles.items())
                if int(character_id) > 0 and str(value.get("caption") or "").strip()
            },
        }
        temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _audiobook_character_identity_index(
        known_characters: list[dict[str, Any]] | None,
    ) -> dict[str, tuple[int, str]]:
        """Map AniList aliases/canonical names to stable character ids."""
        index: dict[str, tuple[int, str]] = {}
        for row in known_characters or []:
            if not isinstance(row, dict):
                continue
            try:
                character_id = int(row.get("character_id") or 0)
            except (TypeError, ValueError):
                continue
            if character_id <= 0:
                continue
            source = re.sub(r"\s+", " ", str(row.get("source") or "")).strip()
            preferred = re.sub(r"\s+", " ", str(row.get("preferred") or source)).strip()
            canonical = preferred or source
            for value in (source, preferred):
                if value:
                    index[value.casefold()] = (character_id, canonical)
        return index

    @staticmethod
    def _audiobook_apply_character_readings(
        text: str, known_characters: list[dict[str, Any]] | None
    ) -> str:
        """Replace canonical AniList names with kana in the TTS request only."""
        result = str(text or "")
        replacements: dict[str, str] = {}
        for row in known_characters or []:
            if not isinstance(row, dict):
                continue
            source = str(row.get("source") or "").strip()
            reading = str(row.get("reading") or "").strip()
            if source and reading and source != reading:
                replacements.setdefault(source, reading)
        for source in sorted(replacements, key=len, reverse=True):
            result = result.replace(source, replacements[source])
        return result

    @staticmethod
    def _read_audiobook_speaker_profiles(path: Path) -> dict[str, str]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        raw = payload.get("profiles", {})
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in raw.items()
            if str(key).strip() and str(value).strip()
        }

    @staticmethod
    def _write_audiobook_speaker_profiles(
        path: Path,
        profiles: dict[str, str],
        series_key: str = "",
        series_title: str = "",
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 2,
            "series_key": str(series_key or ""),
            "series_title": str(series_title or ""),
            "profiles": dict(sorted(profiles.items())),
        }
        temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        try:
            temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _audiobook_manual_chapter_cache(book_dir: Path, chapter_index: int, chapter_text: str) -> Path:
        digest = hashlib.sha256(str(chapter_text).encode("utf-8")).hexdigest()[:32]
        return book_dir / f"manual-chapter-{int(chapter_index):04d}-{digest}.json"

    @staticmethod
    def _load_audiobook_speaker_assignments(path: Path, units: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        assignments: dict[int, dict[str, Any]] = {}
        if not path.is_file():
            return assignments
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload.get("assignments", []) if isinstance(payload, dict) else []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                unit_id = int(row.get("id", -1))
                if 0 <= unit_id < len(units):
                    value: dict[str, Any] = {
                        "speaker": str(row.get("speaker") or "unknown"),
                        "caption": str(row.get("caption") or ""),
                    }
                    try:
                        character_id = int(row.get("anilist_character_id") or 0)
                    except (TypeError, ValueError):
                        character_id = 0
                    if character_id > 0:
                        value["anilist_character_id"] = character_id
                    assignments[unit_id] = value
        except (OSError, ValueError, TypeError, IndexError):
            return {}
        return assignments

    def _audiobook_speaker_parts(
        self,
        *,
        book_id: int,
        book_title: str,
        chapter_index: int,
        chapter_text: str,
        base_caption: str,
        known_characters: list[dict[str, Any]] | None = None,
    ) -> list[tuple[str, int, str, str]]:
        """Return exact TTS parts with stable character voices.

        AniList Character ID is the primary identity and its caption is global across
        sequels/series. Names that cannot be resolved to AniList retain the older
        series-scoped fallback profile. Narration remains book/series scoped.
        """
        units = self._audiobook_voice_units(chapter_text)
        if not units:
            return []
        context = self._audiobook_speaker_cache_context(book_id)
        book_dir = Path(context["book_dir"])
        profiles_path = Path(context["profiles_path"])
        profiles = self._read_audiobook_speaker_profiles(profiles_path)
        character_profiles_path = self._audiobook_character_profiles_path()
        character_profiles = self._read_audiobook_character_profiles(character_profiles_path)
        identity_index = self._audiobook_character_identity_index(known_characters)
        canonical_by_id: dict[int, str] = {}
        for character_id, canonical in identity_index.values():
            if canonical:
                canonical_by_id.setdefault(character_id, canonical)
        global_changed = False
        series_changed = False

        def normalize_assignment(value: dict[str, Any]) -> dict[str, Any]:
            nonlocal global_changed
            speaker = re.sub(r"\s+", " ", str(value.get("speaker") or "unknown")).strip()[:80] or "unknown"
            try:
                character_id = int(value.get("anilist_character_id") or 0)
            except (TypeError, ValueError):
                character_id = 0
            indexed = identity_index.get(speaker.casefold())
            if character_id <= 0 and indexed:
                character_id = int(indexed[0])
            if character_id > 0:
                speaker = canonical_by_id.get(character_id) or (indexed[1] if indexed else speaker)
            caption = re.sub(r"\s+", " ", str(value.get("caption") or "")).strip()[:240]
            if speaker.casefold() in {"unknown", "不明"}:
                return {"speaker": "unknown", "caption": ""}
            if character_id > 0:
                global_profile = character_profiles.get(character_id)
                if global_profile and global_profile.get("caption"):
                    caption = str(global_profile["caption"])
                    if global_profile.get("name"):
                        speaker = str(global_profile["name"])
                elif speaker in profiles and profiles[speaker]:
                    # Lazy migration of v95 series profiles. Preserve the old voice
                    # exactly when the same named speaker first resolves to AniList.
                    caption = profiles[speaker]
                    character_profiles[character_id] = {"name": speaker, "caption": caption}
                    global_changed = True
                elif caption:
                    character_profiles[character_id] = {"name": speaker, "caption": caption}
                    global_changed = True
                result: dict[str, Any] = {
                    "speaker": speaker,
                    "caption": caption,
                    "anilist_character_id": character_id,
                }
                return result
            if speaker in profiles:
                caption = profiles[speaker]
            return {"speaker": speaker, "caption": caption}

        manual_cache = self._audiobook_manual_chapter_cache(book_dir, chapter_index, chapter_text)
        raw_assignments = self._load_audiobook_speaker_assignments(manual_cache, units)
        assignments: dict[int, dict[str, Any]] = {
            unit_id: normalize_assignment(value) for unit_id, value in raw_assignments.items()
        }
        cfg = self.config.llm
        llm_available = bool(cfg.enabled and str(cfg.model or "").strip())
        model_key = f"{getattr(cfg, 'provider', 'ollama')}:{cfg.base_url}:{cfg.model}"
        character_rows = []
        for item in known_characters or []:
            if not isinstance(item, dict):
                continue
            row: dict[str, Any] = {
                "source": str(item.get("source") or ""),
                "preferred": str(item.get("preferred") or ""),
                "reading": str(item.get("reading") or ""),
            }
            try:
                character_id = int(item.get("character_id") or 0)
            except (TypeError, ValueError):
                character_id = 0
            if character_id > 0:
                row["character_id"] = character_id
            if row["source"] or row["preferred"]:
                character_rows.append(row)
        character_rows = character_rows[:80]
        character_key = json.dumps(character_rows, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(
            (f"speaker-v3\0{model_key}\0{character_key}\0{chapter_text}").encode("utf-8")
        ).hexdigest()[:32]
        chapter_cache = book_dir / f"chapter-{int(chapter_index):04d}-{digest}.json"
        cached_llm = self._load_audiobook_speaker_assignments(chapter_cache, units)
        for unit_id, value in cached_llm.items():
            assignments.setdefault(unit_id, normalize_assignment(value))

        dialogue_ids = [int(unit["id"]) for unit in units if unit["dialogue"]]
        missing = [unit_id for unit_id in dialogue_ids if unit_id not in assignments]
        if missing and llm_available:
            client = OllamaClient(cfg)
            try:
                batches: list[list[dict[str, Any]]] = []
                current: list[dict[str, Any]] = []
                chars = 0
                for unit in units:
                    row = {
                        "id": int(unit["id"]),
                        "kind": "dialogue" if unit["dialogue"] else "narration",
                        "text": str(unit["text"]).strip()[:1800],
                    }
                    cost = len(row["text"]) + 80
                    if current and chars + cost > 7000:
                        batches.append(current)
                        current = []
                        chars = 0
                    current.append(row)
                    chars += cost
                if current:
                    batches.append(current)

                visible_global_profiles = {
                    str(character_id): {
                        "name": str(value.get("name") or canonical_by_id.get(character_id, "")),
                        "caption": str(value.get("caption") or ""),
                    }
                    for character_id, value in character_profiles.items()
                    if character_id in canonical_by_id
                }
                system = (
                    "You annotate Japanese light-novel dialogue for text-to-speech. "
                    "Never rewrite, translate, summarize, merge, or split the supplied text. "
                    "For every dialogue row infer the most likely canonical speaker from context; use 'unknown' when uncertain. "
                    "KNOWN_CHARACTERS contains AniList aliases and stable character_id values. When a speaker matches, return that exact id as anilist_character_id. "
                    "A character id is global across sequels, so CHARACTER_PROFILES is authoritative by id and its caption must be reused exactly. "
                    "SERIES_PROFILES is only a fallback for speakers that cannot be resolved to AniList. "
                    "For a new speaker create one short Japanese voice-design caption describing persistent voice identity, never temporary emotion. "
                    "Return strict JSON only: {\"assignments\":[{\"id\":integer,\"speaker\":string,\"anilist_character_id\":integer|null,\"caption\":string}]}. "
                    "Return dialogue assignments only and preserve numeric ids."
                )
                for batch in batches:
                    if not any(row["kind"] == "dialogue" and int(row["id"]) in missing for row in batch):
                        continue
                    response = client.json_chat(
                        system,
                        json.dumps(
                            {
                                "book": book_title,
                                "known_characters": character_rows,
                                "character_profiles": visible_global_profiles,
                                "series_profiles": profiles,
                                "segments": batch,
                            },
                            ensure_ascii=False,
                        ),
                    )
                    if not isinstance(response, dict):
                        continue
                    for row in response.get("assignments", []) if isinstance(response.get("assignments"), list) else []:
                        if not isinstance(row, dict):
                            continue
                        try:
                            unit_id = int(row.get("id", -1))
                        except (TypeError, ValueError):
                            continue
                        if unit_id not in dialogue_ids or unit_id in assignments:
                            continue
                        value = normalize_assignment(
                            {
                                "speaker": row.get("speaker") or "unknown",
                                "caption": row.get("caption") or "",
                                "anilist_character_id": row.get("anilist_character_id"),
                            }
                        )
                        speaker = str(value.get("speaker") or "unknown")
                        character_id = int(value.get("anilist_character_id") or 0)
                        caption = str(value.get("caption") or "")
                        if speaker.casefold() not in {"unknown", "不明"} and character_id <= 0 and caption:
                            if speaker not in profiles:
                                profiles[speaker] = caption
                                series_changed = True
                            value["caption"] = profiles[speaker]
                        assignments[unit_id] = value
            finally:
                client.close()

        # Re-apply profile authority after mixing manual and cached/API assignments.
        for unit_id, value in list(assignments.items()):
            assignments[unit_id] = normalize_assignment(value)

        try:
            if series_changed:
                self._write_audiobook_speaker_profiles(
                    profiles_path,
                    profiles,
                    str(context["series_key"]),
                    str(context["series_title"]),
                )
            if global_changed:
                self._write_audiobook_character_profiles(character_profiles_path, character_profiles)
            if llm_available and missing:
                chapter_cache.write_text(
                    json.dumps(
                        {
                            "schema": 3,
                            "model": model_key,
                            "assignments": [
                                {"id": unit_id, **value}
                                for unit_id, value in sorted(assignments.items())
                                if unit_id in dialogue_ids
                            ],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        except OSError:
            pass

        narrator_caption = base_caption or "落ち着いた自然なナレーター。聞き取りやすく抑制された朗読調。"
        raw_parts: list[tuple[str, int, str, str]] = []
        pending_consumed = 0
        for unit in units:
            unit_id = int(unit["id"])
            assignment = assignments.get(unit_id, {})
            default_speaker = "unknown" if unit["dialogue"] else "narrator"
            speaker = str(assignment.get("speaker") or default_speaker)
            if not unit["dialogue"] and speaker.casefold() in {"unknown", "不明"}:
                speaker = "narrator"
                assignment = {}
            caption = str(assignment.get("caption") or "").strip() if assignment else narrator_caption
            if not caption:
                caption = base_caption or narrator_caption
            split = self._split_audiobook_tts_text(str(unit["text"]))
            if not split:
                if raw_parts:
                    text_value, consumed, old_caption, old_speaker = raw_parts[-1]
                    raw_parts[-1] = (text_value, consumed + int(unit["consumed"]), old_caption, old_speaker)
                else:
                    pending_consumed += int(unit["consumed"])
                continue
            for part_index, (part_text, consumed) in enumerate(split):
                if part_index == 0 and pending_consumed:
                    consumed += pending_consumed
                    pending_consumed = 0
                raw_parts.append((part_text, consumed, caption, speaker))
        if raw_parts and pending_consumed:
            text_value, consumed, old_caption, old_speaker = raw_parts[-1]
            raw_parts[-1] = (text_value, consumed + pending_consumed, old_caption, old_speaker)

        merged: list[tuple[str, int, str, str]] = []
        for text_value, consumed, caption, speaker in raw_parts:
            if merged:
                prev_text, prev_consumed, prev_caption, prev_speaker = merged[-1]
                if prev_caption == caption and prev_speaker == speaker and len(prev_text) + len(text_value) + 1 <= 3600:
                    merged[-1] = (
                        f"{prev_text}{text_value}",
                        prev_consumed + consumed,
                        caption,
                        speaker,
                    )
                    continue
            merged.append((text_value, consumed, caption, speaker))
        return merged

    def _audiobook_generation_control(self, book_id: int) -> str:
        with self._irodori_tts_lock:
            return str(self._audiobook_generation_controls.get(int(book_id)) or "")

    def _audiobook_generation_stop_if_requested(
        self,
        *,
        book_id: int,
        job_id: str,
        output_dir: Path,
        manifest: dict[str, Any],
        current: float,
        total: float,
    ) -> bool:
        control = self._audiobook_generation_control(book_id)
        if control == "pause_requested":
            self.job_center.update(
                job_id,
                state="paused",
                current=current,
                total=total,
                message="Audiobook generation paused",
            )
            self._write_audiobook_generation_manifest(
                output_dir,
                {
                    **manifest,
                    "job_id": job_id,
                    "state": "paused",
                    "current": current,
                    "total": total,
                    "message": "Audiobook generation paused",
                    "error": "",
                },
            )
            return True
        if control == "cancel_requested":
            self.job_center.cancelled(job_id, message="Audiobook generation cancelled")
            shutil.rmtree(output_dir, ignore_errors=True)
            return True
        return False

    def _irodori_tts_worker(self, book_id: int, job_id: str) -> None:
        marker: Path | None = None
        output_dir: Path | None = None
        completed_characters = 0
        total_characters = 0
        manifest_base: dict[str, Any] = {}
        try:
            with self._irodori_tts_lock:
                start_params = dict(self._audiobook_generation_start_params.get(int(book_id)) or {})
            mode = str(start_params.get("mode") or "generate")
            old_audiobook_id = int(start_params.get("old_audiobook_id") or 0) or None
            runtime = self._audiobook_generation_runtime()
            ln_settings = self.light_novels.settings()
            character_voices = bool(
                runtime["provider"] == "irodori"
                and ln_settings.audiobook_character_voices
            )
            source = self.light_novels.tts_source(book_id)
            known_characters: list[dict[str, str]] = []
            if character_voices and source.get("anilist_id"):
                try:
                    known_characters = self.light_novels.character_glossary(int(source["anilist_id"]))
                except Exception as exc:
                    self.logger.warning(
                        "Audiobook character glossary unavailable book_id=%s: %s", book_id, exc
                    )
            chapters = list(source.get("chapters") or [])
            if not chapters:
                raise LightNovelError("No text chapters found")
            chapter_sizes = [len(str(chapter.get("text") or "")) for chapter in chapters]
            total_characters = max(1, sum(chapter_sizes))
            if runtime["provider"] == "irodori":
                self.test_irodori_tts(
                    {"url": runtime["base_url"], "api_key": ""},
                    keep_managed_alive=True,
                )
            safe_title = re.sub(
                r"[^\wぁ-ゟ゠-ヿ一-鿿 .()\[\]-]+",
                "_",
                str(source.get("title") or "Light Novel"),
            ).strip()[:120] or "Light Novel"
            generated_root = self._audiobook_generation_root()
            requested_output = str(start_params.get("output_dir") or "").strip()
            if requested_output:
                output_dir = Path(requested_output).expanduser()
            else:
                output_dir = generated_root / f"{safe_title} [Generated {book_id}]"
            legacy_dir = generated_root / f"{safe_title} [Irodori {book_id}]"
            if mode == "generate" and legacy_dir.is_dir() and not output_dir.exists():
                try:
                    legacy_dir.rename(output_dir)
                except OSError:
                    pass
            output_dir.mkdir(parents=True, exist_ok=True)
            marker = output_dir / ".pudge-audiobook-generating"
            marker.touch(exist_ok=True)
            generation_profile = {
                "schema": 1,
                "provider": runtime["provider"],
                "model": runtime["model"],
                "speed": runtime["speed"],
                "caption": runtime.get("caption", ""),
                "character_voices": character_voices,
                "voice_seed_policy": "deterministic-character-caption-v1" if runtime["provider"] == "irodori" else "",
                "pronunciation_policy": "anilist-character-reading-v1" if runtime["provider"] == "irodori" else "",
                "llm_provider": getattr(self.config.llm, "provider", "ollama") if character_voices else "",
                "llm_model": self.config.llm.model if character_voices else "",
            }
            profile_signature = hashlib.sha256(
                json.dumps(generation_profile, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            profile_path = output_dir / ".pudge-audiobook-profile.json"
            previous_signature = ""
            try:
                previous = json.loads(profile_path.read_text(encoding="utf-8"))
                if isinstance(previous, dict):
                    previous_signature = str(previous.get("signature") or "")
            except (OSError, ValueError, TypeError):
                previous_signature = ""
            reuse_final_chapters = bool(
                previous_signature == profile_signature
                or (mode == "generate" and not character_voices and not previous_signature)
            )
            try:
                profile_path.write_text(
                    json.dumps(
                        {"signature": profile_signature, "profile": generation_profile},
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass
            manifest_base = {
                "book_id": int(book_id),
                "mode": mode,
                "output_dir": str(output_dir),
                "old_audiobook_id": old_audiobook_id or 0,
                "profile_signature": profile_signature,
            }
            self.job_center.update(
                job_id,
                state="running",
                current=0,
                total=total_characters,
                message="Preparing audiobook generation",
            )
            self._write_audiobook_generation_manifest(
                output_dir,
                {
                    **manifest_base,
                    "job_id": job_id,
                    "state": "running",
                    "current": 0,
                    "total": total_characters,
                    "message": "Preparing audiobook generation",
                    "error": "",
                },
            )
            with httpx.Client(timeout=httpx.Timeout(connect=5.0, read=3600.0, write=30.0, pool=5.0)) as client:
                for index, (chapter, chapter_characters) in enumerate(zip(chapters, chapter_sizes), 1):
                    if self._audiobook_generation_stop_if_requested(
                        book_id=book_id,
                        job_id=job_id,
                        output_dir=output_dir,
                        manifest=manifest_base,
                        current=completed_characters,
                        total=total_characters,
                    ):
                        return
                    title = re.sub(r'[\\/:*?"<>|]+', "_", str(chapter.get("title") or f"Chapter {index}"))[:100]
                    output = output_dir / f"{index:04d} - {title}.mp3"
                    if reuse_final_chapters and output.exists() and output.stat().st_size > 1024:
                        completed_characters += chapter_characters
                        message = f"Reusing chapter {index}/{len(chapters)}"
                        self.job_center.update(
                            job_id,
                            state="running",
                            current=completed_characters,
                            total=total_characters,
                            message=message,
                        )
                        self._write_audiobook_generation_manifest(
                            output_dir,
                            {
                                **manifest_base,
                                "job_id": job_id,
                                "state": "running",
                                "current": completed_characters,
                                "total": total_characters,
                                "message": message,
                                "error": "",
                            },
                        )
                        continue
                    message = f"Generating chapter {index}/{len(chapters)}"
                    self.job_center.update(
                        job_id,
                        state="running",
                        current=completed_characters,
                        total=total_characters,
                        message=message,
                    )
                    self._write_audiobook_generation_manifest(
                        output_dir,
                        {
                            **manifest_base,
                            "job_id": job_id,
                            "state": "running",
                            "current": completed_characters,
                            "total": total_characters,
                            "message": message,
                            "error": "",
                        },
                    )
                    chapter_text = str(chapter.get("text") or "")
                    if character_voices:
                        text_parts = self._audiobook_speaker_parts(
                            book_id=book_id,
                            book_title=str(source.get("title") or "Light Novel"),
                            chapter_index=index,
                            chapter_text=chapter_text,
                            base_caption=str(runtime.get("caption") or ""),
                            known_characters=known_characters,
                        )
                    else:
                        text_parts = [
                            (text_value, consumed, str(runtime.get("caption") or ""), "narrator")
                            for text_value, consumed in self._split_audiobook_tts_text(chapter_text)
                        ]
                    if not text_parts:
                        completed_characters += chapter_characters
                        continue
                    chapter_parts_dir = output_dir / ".pudge-audiobook-parts" / f"{index:04d}-{profile_signature[:10]}"
                    chapter_parts_dir.mkdir(parents=True, exist_ok=True)
                    audio_parts: list[Path] = []
                    chapter_progress = 0
                    for part_index, (part_text, part_characters, part_caption, speaker) in enumerate(text_parts, 1):
                        if self._audiobook_generation_stop_if_requested(
                            book_id=book_id,
                            job_id=job_id,
                            output_dir=output_dir,
                            manifest=manifest_base,
                            current=completed_characters + chapter_progress,
                            total=total_characters,
                        ):
                            return
                        part_output = chapter_parts_dir / f"{part_index:04d}.mp3"
                        if not (part_output.exists() and part_output.stat().st_size > 1024):
                            message = (
                                f"Generating chapter {index}/{len(chapters)} · {speaker}"
                                if character_voices and speaker not in {"", "narrator", "unknown"}
                                else f"Generating chapter {index}/{len(chapters)}"
                            )
                            self.job_center.update(
                                job_id,
                                state="running",
                                current=completed_characters + chapter_progress,
                                total=total_characters,
                                message=message,
                            )
                            voice_seed = (
                                self._irodori_voice_seed(speaker, part_caption)
                                if runtime["provider"] == "irodori"
                                else None
                            )
                            speech_text = (
                                self._audiobook_apply_character_readings(part_text, known_characters)
                                if runtime["provider"] == "irodori"
                                else part_text
                            )
                            response = self._post_audiobook_speech(
                                client,
                                runtime,
                                speech_text,
                                index,
                                caption=part_caption,
                                seed=voice_seed,
                            )
                            if len(response.content) < 1024:
                                raise RuntimeError(
                                    f"Audiobook API returned an unexpectedly small audio response for chapter {index}"
                                )
                            output_dir.mkdir(parents=True, exist_ok=True)
                            chapter_parts_dir.mkdir(parents=True, exist_ok=True)
                            marker.touch(exist_ok=True)
                            part_temporary = part_output.with_suffix(".part.mp3")
                            part_temporary.write_bytes(response.content)
                            part_temporary.replace(part_output)
                        audio_parts.append(part_output)
                        chapter_progress += part_characters
                        current = min(total_characters, completed_characters + chapter_progress)
                        self.job_center.update(
                            job_id,
                            state="running",
                            current=current,
                            total=total_characters,
                            message=f"Generating chapter {index}/{len(chapters)}",
                        )
                        self._write_audiobook_generation_manifest(
                            output_dir,
                            {
                                **manifest_base,
                                "job_id": job_id,
                                "state": "running",
                                "current": current,
                                "total": total_characters,
                                "message": f"Generating chapter {index}/{len(chapters)}",
                                "error": "",
                            },
                        )
                    if self._audiobook_generation_stop_if_requested(
                        book_id=book_id,
                        job_id=job_id,
                        output_dir=output_dir,
                        manifest=manifest_base,
                        current=completed_characters + chapter_progress,
                        total=total_characters,
                    ):
                        return
                    self._merge_audiobook_parts(audio_parts, output)
                    shutil.rmtree(chapter_parts_dir, ignore_errors=True)
                    parts_root = output_dir / ".pudge-audiobook-parts"
                    try:
                        parts_root.rmdir()
                    except OSError:
                        pass
                    completed_characters += chapter_characters
                    self.job_center.update(
                        job_id,
                        state="running",
                        current=completed_characters,
                        total=total_characters,
                        message=f"Generated chapter {index}/{len(chapters)}",
                    )
                    self._write_audiobook_generation_manifest(
                        output_dir,
                        {
                            **manifest_base,
                            "job_id": job_id,
                            "state": "running",
                            "current": completed_characters,
                            "total": total_characters,
                            "message": f"Generated chapter {index}/{len(chapters)}",
                            "error": "",
                        },
                    )
            if self._audiobook_generation_stop_if_requested(
                book_id=book_id,
                job_id=job_id,
                output_dir=output_dir,
                manifest=manifest_base,
                current=completed_characters,
                total=total_characters,
            ):
                return
            audiobook = self.audiobooks.import_folder(
                output_dir,
                auto_link=False,
                prepare_transcription=False,
            )
            self.audiobooks.link_light_novel(
                int(book_id),
                int(audiobook["id"]),
                prepare_alignment=False,
            )
            if mode == "regenerate" and old_audiobook_id and int(audiobook["id"]) != old_audiobook_id:
                try:
                    self.audiobooks.delete(old_audiobook_id, delete_files=True)
                except Exception as exc:
                    self.logger.warning(
                        "Could not remove superseded generated audiobook id=%s: %s",
                        old_audiobook_id,
                        exc,
                    )
            self._write_audiobook_generation_manifest(
                output_dir,
                {
                    **manifest_base,
                    "job_id": job_id,
                    "state": "succeeded",
                    "current": total_characters,
                    "total": total_characters,
                    "message": "Audiobook generated",
                    "error": "",
                    "audiobook_id": int(audiobook["id"]),
                },
            )
            self.job_center.finish(
                job_id,
                message="Audiobook generated",
                result={"book_id": int(book_id), "audiobook_id": int(audiobook["id"]), "output_dir": str(output_dir)},
            )
        except Exception as exc:
            self.logger.exception("FAIL step=audiobook_generation.generate book_id=%s error=%r", book_id, str(exc))
            self.job_center.fail(job_id, exc)
            if output_dir is not None and output_dir.is_dir():
                try:
                    self._write_audiobook_generation_manifest(
                        output_dir,
                        {
                            **manifest_base,
                            "book_id": int(book_id),
                            "job_id": job_id,
                            "state": "failed",
                            "current": completed_characters,
                            "total": total_characters,
                            "message": "Audiobook generation failed",
                            "error": str(exc)[-2000:],
                            "output_dir": str(output_dir),
                        },
                    )
                except OSError:
                    pass
        finally:
            if marker is not None:
                marker.unlink(missing_ok=True)
            with self._irodori_tts_lock:
                self._irodori_tts_threads.pop(int(book_id), None)
                self._audiobook_generation_controls.pop(int(book_id), None)
                self._audiobook_generation_start_params.pop(int(book_id), None)
                other_generation_running = any(
                    thread.is_alive() for thread in self._irodori_tts_threads.values()
                )
                # Keep the latest job id so the card can surface success/failure.
            if not other_generation_running:
                self._stop_managed_irodori_server()

    def _start_light_novel_tts(
        self,
        book_id: int,
        *,
        mode: str,
        output_dir: Path | None = None,
        old_audiobook_id: int | None = None,
    ) -> dict[str, Any]:
        book_id = int(book_id)
        runtime = self._audiobook_generation_runtime()
        paired = self.audiobooks.link_for_light_novel(book_id, include_alignment=False)
        paired_kind = self._audiobook_link_kind(book_id, paired)
        if paired_kind == "ordinary":
            raise LightNovelError("This light novel already has a linked audiobook")
        if mode == "generate" and paired_kind == "generated":
            raise LightNovelError("A generated audiobook already exists; use Regenerate audiobook")
        if mode == "regenerate" and paired_kind != "generated":
            raise LightNovelError("Regenerate audiobook is only available for a generated audiobook")
        with self._irodori_tts_lock:
            running = self._irodori_tts_threads.get(book_id)
            if running is not None and running.is_alive():
                job_id = str(self._irodori_tts_job_ids.get(book_id, "") or "")
                return {
                    "started": False,
                    "running": True,
                    "job_id": job_id,
                    "job": self._irodori_tts_status(book_id, paired_audio=paired),
                }
            source = self.light_novels.tts_source(book_id)
            chapters = list(source.get("chapters") or [])
            total_characters = sum(len(str(chapter.get("text") or "")) for chapter in chapters)
            if mode == "generate" and output_dir is None and self._audiobook_generation_manifest(book_id) is not None:
                raise LightNovelError("Audiobook generation is partially complete; use Resume generation")
            safe_title = re.sub(
                r"[^\wぁ-ゟ゠-ヿ一-鿿 .()\[\]-]+",
                "_",
                str(source.get("title") or "Light Novel"),
            ).strip()[:120] or "Light Novel"
            if output_dir is None:
                if mode == "regenerate":
                    output_dir = self._audiobook_generation_root() / (
                        f"{safe_title} [Regenerating {book_id}-{time.time_ns()}]"
                    )
                else:
                    output_dir = self._audiobook_generation_root() / f"{safe_title} [Generated {book_id}]"
            job_id = self.job_center.start(
                "audiobook_generation",
                f"{'Regenerate' if mode == 'regenerate' else 'Generate'} audiobook: {source.get('title') or book_id}",
                payload={"book_id": book_id, "provider": runtime["provider"], "mode": mode},
                total=total_characters,
            )
            self._audiobook_generation_controls.pop(book_id, None)
            self._audiobook_generation_start_params[book_id] = {
                "mode": mode,
                "output_dir": str(output_dir),
                "old_audiobook_id": int(old_audiobook_id or 0),
            }
            self._write_audiobook_generation_manifest(
                output_dir,
                {
                    "book_id": book_id,
                    "job_id": job_id,
                    "mode": mode,
                    "state": "queued",
                    "current": 0,
                    "total": total_characters,
                    "output_dir": str(output_dir),
                    "old_audiobook_id": int(old_audiobook_id or 0),
                    "message": "Queued",
                    "error": "",
                },
            )
            thread = threading.Thread(
                target=self._irodori_tts_worker,
                args=(book_id, job_id),
                name=f"{APP_SLUG}-audiobook-{book_id}",
                daemon=True,
            )
            self._irodori_tts_threads[book_id] = thread
            self._irodori_tts_job_ids[book_id] = job_id
            thread.start()
        return {
            "started": True,
            "running": True,
            "job_id": job_id,
            "job": self._irodori_tts_status(book_id, paired_audio=paired),
        }

    def generate_light_novel_tts(self, book_id: int) -> dict[str, Any]:
        return self._start_light_novel_tts(int(book_id), mode="generate")

    def resume_light_novel_tts(self, book_id: int) -> dict[str, Any]:
        book_id = int(book_id)
        manifest = self._audiobook_generation_manifest(book_id)
        if manifest is None:
            raise LightNovelError("No partial audiobook generation was found")
        paired = self.audiobooks.link_for_light_novel(book_id, include_alignment=False)
        if self._audiobook_link_kind(book_id, paired) == "ordinary":
            raise LightNovelError("This light novel already has a linked audiobook")
        return self._start_light_novel_tts(
            book_id,
            mode=str(manifest.get("mode") or "generate"),
            output_dir=Path(str(manifest.get("output_dir") or "")).expanduser(),
            old_audiobook_id=int(manifest.get("old_audiobook_id") or 0) or None,
        )

    def regenerate_light_novel_tts(self, book_id: int) -> dict[str, Any]:
        book_id = int(book_id)
        paired = self.audiobooks.link_for_light_novel(book_id, include_alignment=False)
        if self._audiobook_link_kind(book_id, paired) != "generated":
            raise LightNovelError("Regenerate audiobook is only available for a generated audiobook")
        old_id = int((paired or {}).get("book", {}).get("id") or 0)
        return self._start_light_novel_tts(
            book_id,
            mode="regenerate",
            old_audiobook_id=old_id or None,
        )

    def pause_light_novel_tts(self, book_id: int) -> dict[str, Any]:
        book_id = int(book_id)
        with self._irodori_tts_lock:
            thread = self._irodori_tts_threads.get(book_id)
            if thread is None or not thread.is_alive():
                return {"ok": False, "reason": "not_running", "job": self._irodori_tts_status(book_id)}
            self._audiobook_generation_controls[book_id] = "pause_requested"
            job_id = str(self._irodori_tts_job_ids.get(book_id) or "")
        if job_id:
            job = self.job_center.get(job_id) or {}
            self.job_center.update(
                job_id,
                state="running",
                current=float(job.get("current") or 0.0),
                total=float(job.get("total") or 0.0),
                message="Pausing…",
            )
        return {"ok": True, "job": self._irodori_tts_status(book_id)}

    def cancel_light_novel_tts(self, book_id: int) -> dict[str, Any]:
        book_id = int(book_id)
        with self._irodori_tts_lock:
            thread = self._irodori_tts_threads.get(book_id)
            job_id = str(self._irodori_tts_job_ids.get(book_id) or "")
            if thread is not None and thread.is_alive():
                self._audiobook_generation_controls[book_id] = "cancel_requested"
                if job_id:
                    self.job_center.request_cancel(job_id)
                return {"ok": True, "pending": True, "job": self._irodori_tts_status(book_id)}
        manifest = self._audiobook_generation_manifest(book_id)
        if manifest is None:
            return {"ok": True, "pending": False, "deleted": False, "job": self._irodori_tts_status(book_id)}
        output_dir = Path(str(manifest.get("output_dir") or "")).expanduser()
        shutil.rmtree(output_dir, ignore_errors=True)
        manifest_job_id = str(manifest.get("job_id") or job_id)
        if manifest_job_id:
            self.job_center.cancelled(manifest_job_id, message="Audiobook generation cancelled")
        return {"ok": True, "pending": False, "deleted": True, "job": self._irodori_tts_status(book_id)}

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

    def _load_play_registry(self) -> dict[str, dict[str, Any]]:
        path = self.config.paths.cache_dir / "active-playbacks.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in payload.items()
            if isinstance(value, dict)
        }

    def _save_play_registry(self) -> None:
        path = self._play_registry_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._play_registry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    def _restored_play_state_locked(self, key: str) -> dict[str, Any] | None:
        record = self._play_registry.get(key)
        if not isinstance(record, dict):
            return None
        try:
            pid = int(record.get("pid") or 0)
            started_at = float(record.get("started_at") or 0.0)
        except (TypeError, ValueError):
            pid = 0
            started_at = 0.0
        if not self._pid_is_alive(pid):
            self._play_registry.pop(key, None)
            self._save_play_registry()
            return None
        age = max(0.0, time.time() - started_at) if started_at else 6.0
        return {
            "status": "starting" if age < 6.0 else "running",
            "pid": pid,
            "age": age,
            "restored": True,
        }

    def _active_playbacks_payload(self) -> list[dict[str, Any]]:
        active: list[dict[str, Any]] = []
        with self._play_lock:
            keys = set(self._play_registry) | set(self._play_processes)
            for key in list(keys):
                state = self._play_state_locked(key)
                if state.get("status") not in {"starting", "running"}:
                    continue
                active.append({"video_path": key, **state})
        return active

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
            self._play_registry.pop(key, None)
            self._save_play_registry()
            self._play_exit_codes[key] = (int(code), time.time())

        restored = self._restored_play_state_locked(key)
        if restored is not None:
            return restored

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
        allow_unsynced_subtitles: bool = False,
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
                explicit_library_embedded_sid = (
                    int(episode.embedded_subtitle_id)
                    if (
                        episode is not None
                        and allow_image_subtitles
                        and episode.state == "waiting_text_subtitles"
                        and episode.embedded_subtitle_id is not None
                    )
                    else None
                )
                legacy_bitmap_only = (
                    episode is not None
                    and episode.state == "waiting_text_subtitles"
                    and episode.embedded_subtitle_id is not None
                    and (
                        episode.subtitle_path is None
                        or not episode.subtitle_path.is_file()
                    )
                )
                explicit_raw_unsynced = bool(
                    episode is not None
                    and allow_unsynced_subtitles
                    and episode.state == "couldnt_sync"
                    and episode.subtitle_path is not None
                    and episode.subtitle_path.is_file()
                )
                if episode is not None and not legacy_bitmap_only and not explicit_raw_unsynced:
                    selection = resolve_episode_subtitle(
                        self.manager.db,
                        video_path=path,
                        media_id=episode.media_id,
                        episode=episode.episode,
                        stored_path=episode.subtitle_path,
                        stored_embedded_id=episode.embedded_subtitle_id,
                        stored_origin=episode.subtitle_origin,
                        ffprobe=self.config.tools.ffprobe,
                        ffmpeg=self.config.tools.ffmpeg,
                        allow_bitmap=bool(allow_image_subtitles),
                    )
                    if selection.found:
                        repaired = repair_episode_subtitle(
                            self.manager.db,
                            video_path=path,
                            selection=selection,
                        )
                        # repair_episode_subtitle() can promote a stale Waiting
                        # row back to Ready. Refresh the in-memory episode before
                        # building the fast-play command; otherwise this launch
                        # still sees the old state and omits --sub even though the
                        # recovered subtitle is already persisted in SQLite.
                        refreshed_episode = (
                            self.manager.db.episode_by_path(path)
                            if selection.recovered
                            else None
                        )
                        if refreshed_episode is not None:
                            episode = refreshed_episode
                        else:
                            # Preserve the existing safety rule for a non-ready
                            # row that merely contains some old/stale subtitle
                            # path: it may be intentionally invalidated and must
                            # not be passed to mpv until the resolver actually
                            # recovers a trusted selection.
                            episode.subtitle_path = selection.external_path
                            episode.embedded_subtitle_id = (
                                None if selection.external_path is not None
                                else selection.embedded_subtitle_id
                            )
                            episode.subtitle_origin = selection.source or episode.subtitle_origin
                        if repaired or selection.recovered:
                            self.logger.info(
                                "RESULT step=play.subtitle_recover video=%s source=%s reason=%s external=%s embedded_sid=%s",
                                path.name,
                                selection.source,
                                selection.reason,
                                bool(selection.external_path),
                                selection.embedded_subtitle_id,
                            )
                    if (
                        explicit_library_embedded_sid is not None
                        and (
                            episode.subtitle_path is None
                            or not episode.subtitle_path.is_file()
                        )
                    ):
                        episode.embedded_subtitle_id = explicit_library_embedded_sid
                    usable_external = bool(
                        episode.subtitle_path is not None
                        and episode.subtitle_path.is_file()
                    )
                    usable_embedded = episode.embedded_subtitle_id is not None
                    if (
                        episode.state == "ready"
                        and not usable_external
                        and not usable_embedded
                    ):
                        # The DB can outlive cache cleanup.  A stale Ready row must
                        # not launch mpv without subtitles; invalidate it, enqueue a
                        # fresh resolver job and tell the UI that preparation resumed.
                        series_result = self.manager.revalidate_subtitle_series(
                            episode.media_id,
                            reason="Prepared subtitle cache file is missing",
                            priority=260,
                        )
                        if episode.media_id is None:
                            self.manager.db.clear_subtitle_selection(path)
                            self.manager.db.queue_subtitle_job(
                                path,
                                episode.media_id,
                                episode.episode,
                                error="Prepared subtitle cache file is missing",
                                priority=260,
                            )
                        self.logger.warning(
                            "REPAIR step=play.subtitle_missing_requeue video=%s "
                            "media_id=%s episode=%s stale_path=%r series=%s",
                            path.name, episode.media_id, episode.episode,
                            str(episode.subtitle_path or ""),
                            series_result,
                        )
                        raise RuntimeError(
                            "Prepared subtitles are missing; Pudge queued them for repair."
                        )
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
                        recently_closed = bool(
                            episode.playback_updated_at is not None
                            and 0.0
                            <= time.time() - float(episode.playback_updated_at)
                            < 120.0
                        )
                        rewind_seconds = (
                            0.0
                            if recently_closed
                            else float(self.config.playback.rewind_seconds)
                        )
                        start_at = max(
                            0.0,
                            float(episode.playback_position) - rewind_seconds,
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
                        allow_unsynced_subtitles
                        and episode.state == "couldnt_sync"
                        and episode.subtitle_path is not None
                        and episode.subtitle_path.is_file()
                    ):
                        # The user explicitly requested the original source.
                        # Fast-play + --no-sync prevents any transformation.
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
                    if (
                        explicit_library_embedded_sid is not None
                        and "--sub" not in command
                        and "--embedded-sid" not in command
                    ):
                        command.extend(
                            ["--embedded-sid", str(explicit_library_embedded_sid)]
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
            self._play_registry[key] = {
                "pid": int(process.pid),
                "started_at": float(self._play_started_at[key]),
            }
            self._save_play_registry()
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

    def export_runtime_debug_bundle(
        self, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        output_dir = debug_log_dir()
        frontend = dict(payload) if isinstance(payload, dict) else {}
        target = self.diagnostics_controller.export(
            output_dir,
            version=__version__,
            frontend=frontend,
            logs={
                "runtime": DEFAULT_LOG_PATH,
                "energy": ENERGY_LOG_PATH,
                "agent": DEFAULT_LOG_PATH.parent / f"{APP_SLUG}-agent.log",
                "agent-error": DEFAULT_LOG_PATH.parent / f"{APP_SLUG}-agent-error.log",
            },
        )
        try:
            subprocess.Popen(["open", "-R", str(target)])
        except OSError:
            pass
        return {"ok": True, "path": str(target)}

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
        """Request protected-folder/notification access only during first-run preflight."""
        if self.config.ui.permissions_requested:
            folders: dict[str, bool] = {}
        else:
            folder_paths = [
                self.config.library.root_dir,
                *self.config.paths.download_dirs,
                *self.config.paths.subtitle_dirs,
            ]
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
        try:
            self.diagnostics.record(
                str(safe_payload.get("correlation_id") or ""),
                "ui",
                safe_event,
                media_id=(
                    int(safe_payload["media_id"])
                    if safe_payload.get("media_id") is not None
                    else None
                ),
                media_episode=(
                    int(safe_payload["episode"])
                    if safe_payload.get("episode") is not None
                    else None
                ),
                payload=safe_payload,
            )
        except (OSError, TypeError, ValueError):
            self.logger.warning("FALLBACK step=diagnostics.ui_event event=%s", safe_event)
        return {"ok": True}

    def safe_mode_status(self) -> dict[str, Any]:
        return self.safe_mode.status(run_checks=True)

    def safe_mode_restart_normally(self) -> dict[str, Any]:
        self.safe_mode.leave_on_restart()
        self._ui_state_cache.invalidate()
        self.task_supervisor.start(
            name="audiobook-stt-resume",
            target=self.audiobooks.resume_pending_transcriptions,
            replace=True,
        )
        self._ensure_energy_monitor(reason="safe_mode_resumed")
        self._start_scheduled_agent()
        self.logger.info("EVENT app.safe_mode_resumed")
        return {
            "ok": True,
            "restart_required": False,
            "state": self.get_state(),
        }

    def reveal_safe_mode_backup(self) -> dict[str, Any]:
        backup = self.safe_mode.latest_backup()
        if backup is None:
            return {"ok": False, "error": "No migration backup is available"}
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(backup)])
        return {"ok": True, "path": str(backup)}

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

    def media_identity_lock(self, canonical_id: str, locked: bool = True) -> dict[str, Any]:
        return {
            "ok": self.manager.identity_resolver.lock(str(canonical_id), bool(locked)),
            "canonical_id": str(canonical_id),
            "locked": bool(locked),
        }

    def anime_debug_snapshot(self, media_id: int, episode: int | None = None) -> dict[str, Any]:
        return DebugSnapshotService(self.manager, cache_dir=self.config.paths.cache_dir, runtime_log_path=DEFAULT_LOG_PATH).snapshot(
            int(media_id), None if episode is None else int(episode)
        )

    def debug_reselect_subtitles(self, video_path: str) -> dict[str, Any]:
        video = Path(str(video_path)).expanduser().resolve()
        result = self.manager.force_fresh_subtitle_selection(video)
        priority_token = self.manager.work_scheduler.begin_priority_request(
            WorkPriority.USER,
            name="subtitle-debug-fresh",
        )
        def worker() -> None:
            try:
                # Manual UI work must enter the priority queue immediately.
                # The heavy scheduler, not maintenance.lock, serializes the
                # expensive preparation against background OCR.
                self.manager.process_subtitle_jobs(
                    limit=1,
                    preferred_paths=[video],
                    wait_for_slot=True,
                )
            except Exception as exc:
                self.logger.exception(
                    "FAIL step=subtitle.debug_fresh_background video=%r error=%r",
                    str(video),
                    str(exc),
                )
            finally:
                self.manager.work_scheduler.end_priority_request(priority_token)
        try:
            self.task_supervisor.start(
                name=f"{APP_SLUG}-debug-fresh-subtitles",
                target=worker,
                replace=True,
            )
        except Exception:
            self.manager.work_scheduler.end_priority_request(priority_token)
            raise
        return result

    def export_anime_debug_snapshot(self, media_id: int, episode: int | None = None) -> dict[str, Any]:
        payload = self.anime_debug_snapshot(media_id, episode)
        downloads = debug_log_dir()
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
        video = Path(str(video_path)).expanduser().resolve()
        item = self.manager.db.episode_by_path(video)
        if item is None:
            raise RuntimeError("Episode is not present in the local library")

        # A retry must never destroy the last known-good subtitle before the new
        # attempt succeeds. Recover a valid prepared history path first when an
        # older retry already cleared the episode row (the Hyakkano S03E09 case).
        if item.subtitle_path is None and item.embedded_subtitle_id is None:
            self.manager._recover_selected_text_subtitle_from_history(
                video, media_id=item.media_id, episode=item.episode
            )
            item = self.manager.db.episode_by_path(video) or item

        if item.subtitle_path is not None or item.embedded_subtitle_id is not None:
            latest = self.manager.db.latest_selected_subtitle(video)
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
                "requested_at": time.time(),
                "forced": True,
                "manual_retry": True,
            }
            self.manager.db.set_state(
                self.manager._subtitle_upgrade_state_key(video),
                json.dumps(request, ensure_ascii=False),
            )
        else:
            self.manager.db.clear_subtitle_selection(video)

        self.manager.db.queue_subtitle_job(
            video, item.media_id, item.episode, priority=220, error="Manual retry"
        )
        prepared = 0
        acquired_now = False
        with maintenance_lock(self.config.paths.cache_dir, blocking=False) as acquired:
            acquired_now = bool(acquired)
            if acquired_now:
                prepared = self.manager.process_subtitle_jobs(limit=1, preferred_paths=[video])
        if not acquired_now:
            def worker() -> None:
                try:
                    with maintenance_lock(self.config.paths.cache_dir, blocking=True) as acquired:
                        if acquired:
                            self.manager.process_subtitle_jobs(limit=1, preferred_paths=[video])
                except Exception as exc:
                    self.logger.exception(
                        "FAIL step=subtitle.manual_retry_background video=%r error=%r",
                        str(video), str(exc),
                    )
            self.task_supervisor.start(
                name=f"{APP_SLUG}-manual-subtitle-retry",
                target=worker,
                replace=True,
            )
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
                if self._watched_on_anilist(anime, episode.episode):
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

            with self._restore_lock:
                blockers = self._restore_background_blockers()
                if blockers:
                    return {
                        "ok": False,
                        "busy": True,
                        "error": "Background work is still active",
                        "workers": blockers,
                    }
                with maintenance_lock(self.config.paths.cache_dir, blocking=False) as acquired:
                    if not acquired:
                        return {
                            "ok": False,
                            "busy": True,
                            "error": "Maintenance is active; retry restore after it finishes",
                        }

                    self._stop_scheduled_agent()
                    supervisor = self.task_supervisor
                    supervisor_lingering = supervisor.quiesce(timeout=5.0)
                    if supervisor_lingering:
                        supervisor.resume()
                        self._start_scheduled_agent()
                        return {
                            "ok": False,
                            "busy": True,
                            "error": "Background tasks did not quiesce",
                            "workers": supervisor_lingering,
                        }

                    manga_lingering = self._quiesce_manga_ocr(timeout=5.0)
                    if manga_lingering:
                        supervisor.resume()
                        self._start_scheduled_agent()
                        return {
                            "ok": False,
                            "busy": True,
                            "error": "Manga OCR is still stopping",
                            "workers": manga_lingering,
                        }

                    audiobook_lingering = self.audiobooks.close(timeout=5.0)
                    if audiobook_lingering:
                        supervisor.resume()
                        self._start_scheduled_agent()
                        return {
                            "ok": False,
                            "busy": True,
                            "error": "Audiobook workers did not stop",
                            "workers": audiobook_lingering,
                            "restart_required": True,
                        }

                    try:
                        restored = restore_backup(
                            archive_path=selected,
                            config_path=self.config_path,
                            database_path=self.config.library.database_path,
                            cache_dir=self.config.paths.cache_dir,
                        )
                        self._reload_runtime_services_after_restore()
                    except Exception:
                        # restore_backup rolls live files back atomically. Rebind
                        # services to that rolled-back state before surfacing error.
                        self._reload_runtime_services_after_restore()
                        raise
                    finally:
                        supervisor.resume()
                        self._start_scheduled_agent()

                    if not self.safe_mode.active:
                        self.task_supervisor.start(
                            name="audiobook-stt-resume",
                            target=self.audiobooks.resume_pending_transcriptions,
                            replace=True,
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


def _asset_handler_for_api(api: WebAppApi, web_root: Path) -> type[_QuietHandler]:
    class _AppAssetHandler(_QuietHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(web_root), **kwargs)

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/torrents/status":
                try:
                    result = dict(api.torrent_traffic_status())
                    self._send_json(result)
                except Exception as exc:
                    api.logger.warning("FAIL torrent.http_status error=%r", str(exc))
                    self._send_json({"ok": False, "error": str(exc)}, status=500)
                return
            super().do_GET()

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path != "/api/torrents/enabled":
                self._send_json({"ok": False, "error": "not_found"}, status=404)
                return
            try:
                size = min(max(int(self.headers.get("Content-Length", "0") or 0), 0), 4096)
                raw = self.rfile.read(size) if size else b"{}"
                payload = json.loads(raw.decode("utf-8"))
                desired = bool(payload.get("enabled"))
                api.logger.info("EVENT torrent.http_toggle desired=%s", desired)
                result = dict(api.set_torrents_enabled(desired))
                result["ok"] = True
                self._send_json(result)
            except Exception as exc:
                api.logger.error("FAIL torrent.http_toggle error=%r", str(exc))
                self._send_json({"ok": False, "error": str(exc)}, status=500)

    return _AppAssetHandler


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

    handler = _asset_handler_for_api(api, web_root)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


class _MacWindowLifecycle:
    """Keep a macOS window alive when the user closes it.

    The red close button and Cmd+W should hide the window while leaving the
    Python/backend process running.  A real application quit (Cmd+Q / Quit)
    flips ``quit_requested`` first, allowing pywebview's normal close path.
    """

    def __init__(self, window: Any, logger: Any, on_quit: Any | None = None) -> None:
        self.window = window
        self.logger = logger
        self.on_quit = on_quit
        self.quit_requested = False

    def request_quit(self, source: str = "macos") -> None:
        first_request = not self.quit_requested
        self.quit_requested = True
        self.logger.info("EVENT app.quit_requested source=%s", source)
        if first_request and callable(self.on_quit):
            try:
                self.on_quit(source)
            except Exception as exc:
                self.logger.warning(
                    "FALLBACK step=app.background_quiet_on_quit error=%r", str(exc)
                )

    def handle_closing(self) -> bool:
        if self.quit_requested:
            self.logger.info("EVENT app.window_close action=quit")
            return True
        try:
            self.window.hide()
            self.logger.info("EVENT app.window_close action=hide")
        except Exception as exc:
            self.logger.warning("FALLBACK step=app.window_hide error=%r", str(exc))
        return False

    def reopen(self) -> bool:
        try:
            self.window.show()
            self.logger.info("EVENT app.window_reopen action=show")
            return True
        except Exception as exc:
            self.logger.warning("FALLBACK step=app.window_reopen error=%r", str(exc))
            return False


def _install_macos_app_delegate_proxy(api: "WebAppApi", lifecycle: _MacWindowLifecycle) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import NSApplication
        from Foundation import NSObject, YES

        application = NSApplication.sharedApplication()
        original_delegate = application.delegate()
        if original_delegate is None:
            api.logger.warning("FALLBACK step=app.macos_delegate reason=no_original_delegate")
            return False

        class PudgeApplicationDelegateV41(NSObject):
            def applicationShouldTerminate_(self, app):
                lifecycle.request_quit("macos_quit")
                handler = getattr(original_delegate, "applicationShouldTerminate_", None)
                return handler(app) if callable(handler) else YES

            def applicationSupportsSecureRestorableState_(self, app):
                handler = getattr(original_delegate, "applicationSupportsSecureRestorableState_", None)
                return handler(app) if callable(handler) else YES

            def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_visible_windows):
                if not bool(has_visible_windows):
                    lifecycle.reopen()
                handler = getattr(original_delegate, "applicationShouldHandleReopen_hasVisibleWindows_", None)
                if callable(handler):
                    try:
                        return handler(app, has_visible_windows)
                    except Exception:
                        pass
                return YES

        proxy = PudgeApplicationDelegateV41.alloc().init()
        # Retain both delegates for the complete Cocoa lifetime.
        proxy._pudge_original_delegate = original_delegate
        proxy._pudge_lifecycle = lifecycle
        api._macos_app_delegate_proxy = proxy
        application.setDelegate_(proxy)
        api.logger.info("EVENT app.macos_close_to_hide installed=true")
        return True
    except Exception:
        api.logger.exception("FAIL step=app.macos_close_to_hide")
        return False


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
    companion_server = None
    if api.config.companion.enabled:
        try:
            companion_server, _companion_thread = start_mobile_sync_server(
                api.mobile_sync,
                host=api.config.companion.bind_host,
                port=api.config.companion.port,
                streaming=api.companion_streaming,
                study_parser=api.light_novels.parse_study_text,
                logger=api.logger,
            )
            api._companion_server = companion_server
            api._companion_thread = _companion_thread
            companion_host, companion_port = companion_server.server_address
            api.companion_base_url = f"http://{companion_host}:{companion_port}"
            api.logger.info(
                "EVENT companion.started host=%s port=%s",
                companion_host,
                companion_port,
            )
        except OSError as exc:
            api.logger.warning(
                "FALLBACK step=companion.start host=%s port=%s error=%r",
                api.config.companion.bind_host,
                api.config.companion.port,
                str(exc),
            )
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

    if sys.platform == "darwin":
        lifecycle = _MacWindowLifecycle(
            window, api.logger, lambda source: api._enter_background_quiet(reason=source)
        )
        api._macos_window_lifecycle = lifecycle
        window.events.closing += lifecycle.handle_closing

        def on_before_show() -> None:
            _install_macos_app_delegate_proxy(api, lifecycle)

        window.events.before_show += on_before_show

    def on_started() -> None:
        # pywebview initializes NSApplication during start(). Set the icon again
        # so the actual WebKit process has the same icon in Dock and Cmd+Tab.
        icon_set = _set_macos_runtime_identity()
        api.logger.info("EVENT app.webview_started runtime_identity=%s", icon_set)

        # Finder drop binding is intentionally deferred until the page fires
        # ``pywebviewready``. Binding DOM drag events here used to attach too
        # early and also routed high-frequency dragover traffic through Python.
        api.logger.info("EVENT app.drop_import_waiting_for_dom")

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
        if companion_server is not None:
            companion_server.shutdown()
            companion_server.server_close()
    return 0
