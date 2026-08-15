from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from rapidfuzz import fuzz

from .config import AppConfig
from .branding import APP_SLUG, LEGACY_APP_NAMES, LEGACY_APP_SLUGS
from .database import Database
from .filename import parse_anime_filename, title_similarity
from .language import IMAGE_SUBTITLE_EXTENSIONS, TEXT_SUBTITLE_EXTENSIONS
from .jimaku_trial import apply_jimaku_trial
from .library import (
    VIDEO_EXTENSIONS,
    japanese_subtitle_details,
    japanese_subtitle_source,
    scan_library,
    strict_title_similarity,
)
from .runtime import python_executable
from .foreground import foreground_active
from .logging_utils import configure_logging, timed_step
from .maintenance_lock import maintenance_lock
from .manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease
from .models import AniListAnime
from .notifications import send_native_notification
from .pipeline_cache import invalidate_final_pipeline_result
from .providers.anilist import AniListClient, AniListError, AniListHTTPError
from .providers.nyaa import (
    NyaaClient,
    NyaaError,
    SubsPleaseClient,
    fresh_trusted_zero_seeders_allowed,
    search_ranked,
    search_subsplease_ranked,
    _season_number,
    _expected_season,
    release_episode as parsed_release_episode,
)
from .providers.qbittorrent import QBittorrentClient, QBittorrentError
from .providers.aria2 import Aria2Client
from .relation_graphs import (
    compact_relations_from_graph,
    graph_for_root,
    next_relation_refresh_at,
    relation_retry_at,
)
from .subtitles.jobs import read_job_report
from .subtitles.selection import upgrade_is_better
from .debug_snapshot import (
    append_debug_trace,
    record_video_selection_debug,
    subtitle_debug_paths,
    write_prepare_debug_result,
)
from .energy_diagnostics import EnergyDiagnosticsMonitor


LogFn = Callable[[str], None]


class ManagerError(RuntimeError):
    pass


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


def _subtitle_retry_delay_seconds(
    *,
    poll_minutes: float,
    attempts: int,
    detail: str,
) -> float:
    """Use the configured interval unless this is a network/service failure."""
    configured = max(1.0, float(poll_minutes) * 60.0)
    deterministic_validation = any(
        marker in str(detail or "").casefold()
        for marker in (
            "all subtitle candidates failed synchronization/validation",
            "too_many_large_jumps",
            "full_range_oscillation",
            "no_stable_offset_cluster",
            "timeline_validation_failed",
            "timeline_segment_support_too_low",
            "timeline_unstable_segments",
            "timeline_weak_path",
        )
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


_PREPARATION_DETAIL_EN_REPLACEMENTS = (
    ("AniList: обновление прогресса отключено для этого запуска",
     "AniList: progress updates disabled for this run"),
    ("AniList: отключён для этого запуска (--offline)",
     "AniList: disabled for this run (--offline)"),
    ("AniList: интеграция отключена в настройках",
     "AniList: integration is disabled in settings"),
    ("AniList: access token не задан; трекер и горячие клавиши отключены",
     "AniList: access token is not set; tracker and hotkeys are disabled"),
    ("Jimaku: API key не задан, интернет-поиск пропущен",
     "Jimaku: API key is not set; online search skipped"),
    ("Jimaku: подходящее аниме не найдено",
     "Jimaku: no matching anime found"),
    ("Jimaku: файл нужной серии не найден с достаточной уверенностью",
     "Jimaku: no sufficiently confident file found for the requested episode"),
    ("Японские субтитры пока не найдены",
     "Japanese subtitles not found yet"),
    ("Японские субтитры не найдены; mpv будет запущен без добавленного файла",
     "Japanese subtitles not found; mpv will start without an added subtitle file"),
    ("Субтитры пока не найдены",
     "Subtitles not found yet"),
    ("Субтитры отклонены проверкой качества: все проверенные варианты отклонены",
     "Subtitles rejected by quality validation: all checked candidates were rejected"),
    ("Проверка constant-offset эталона:",
     "Constant-offset reference check:"),
    ("Выбран лучший constant-offset эталон:",
     "Best constant-offset reference selected:"),
    ("LLM-проверка встроенного эталона:",
     "Embedded-reference LLM check:"),
    ("Прямое сопоставление субтитров через ALASS:",
     "Direct subtitle matching via ALASS:"),
    ("Проверка прямого ALASS по английскому эталону:",
     "Direct ALASS English-reference validation:"),
    ("Эталон тайминга: встроенная дорожка",
     "Timing reference: embedded track"),
    ("Выравнивание по английскому эталону не выполнено",
     "Alignment against English reference failed"),
    ("локальная проверка отклонена",
     "local validation rejected"),
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


class AnimeManager:
    def __init__(self, config: AppConfig, log: LogFn | None = None) -> None:
        self.config = config
        self.db = Database(config.library.database_path)
        self.logger = configure_logging()
        self.log = log or self.logger.info
        self._last_anilist_warning = ""
        self._last_anilist_used_cache = False
        self._last_missing_episode_rows = 0
        self._last_completed_video_paths: tuple[Path, ...] = ()
        self._subsplease_client = SubsPleaseClient(timeout=8.0, cache_ttl=60.0)
        missing_schedule_repaired = self.db.repair_missing_cleanup_schedule(
            config.agent.delete_after_watched_hours
        )
        repaired = self.db.reconcile_watched_cleanup(
            config.agent.delete_after_watched_hours
        )
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

    def _anime_is_fully_ready(self, anime: LibraryAnime) -> bool:
        episodes = self.db.episodes(anime.media_id)
        ready_states = {"ready", "watched"}
        if anime.format == "MOVIE":
            return any(item.state in ready_states for item in episodes)
        if not anime.episodes or anime.episodes < 1:
            return False
        ready_numbers = {
            int(item.episode)
            for item in episodes
            if item.episode is not None and item.state in ready_states
        }
        return all(number in ready_numbers for number in range(1, int(anime.episodes) + 1))

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
        if self.db.get_state(episode_key, ""):
            return

        anime = self.db.get_anime(media_id) if media_id is not None else None
        if anime is not None and episode is not None:
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
        full_ready = bool(anime is not None and self._anime_is_fully_ready(anime))
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
            if language == "ru":
                subtitle = "Аниме готово"
                if anime is not None and anime.format == "MOVIE":
                    message = f"{title} готово к просмотру с субтитрами."
                else:
                    message = f"{title}: все серии готовы с субтитрами."
            else:
                subtitle = "Anime ready"
                if anime is not None and anime.format == "MOVIE":
                    message = f"{title} is ready to watch with subtitles."
                else:
                    message = f"{title}: all episodes are ready with subtitles."
        else:
            if language == "ru":
                subtitle = "Серия готова"
                message = (
                    f"{title} — серия {episode} готова с субтитрами."
                    if episode is not None
                    else f"{title} готово к просмотру с субтитрами."
                )
            else:
                subtitle = "Episode ready"
                message = (
                    f"{title} — episode {episode} is ready with subtitles."
                    if episode is not None
                    else f"{title} is ready to watch with subtitles."
                )
        try:
            delivered = send_native_notification(subtitle, message)
        except Exception as exc:
            # Notifications are best-effort UI. A macOS notification backend
            # failure must never roll back an otherwise successful subtitle job.
            delivered = False
            self.logger.warning(
                "FALLBACK step=notification.ready media_id=%s episode=%s error=%r",
                media_id, episode, str(exc),
            )
        if delivered:
            self.db.set_state(episode_key, "delivered")
            if notify_full and full_key:
                self.db.set_state(full_key, "delivered")
        self.logger.info(
            "EVENT notification.ready media_id=%s episode=%s full=%s delivered=%s backend=app_bundle",
            media_id, episode, notify_full, delivered,
        )

    def sync_anilist(self) -> list[LibraryAnime]:
        self._last_anilist_warning = ""
        self._last_anilist_used_cache = False
        self._last_missing_episode_rows = 0
        if not self.config.anilist.enabled or not self.config.anilist.access_token:
            self.log("AniList: токен не задан, список не обновлён")
            return self.db.anime_list()
        self.db.set_state("anilist_last_attempt_at", str(time.time()))
        client = AniListClient(
            self.config.anilist.endpoint,
            access_token=self.config.anilist.access_token,
        )
        try:
            try:
                with timed_step(self.logger, "anilist.library", mode="compact"):
                    compact_library = getattr(client, "library_compact", None)
                    items = compact_library() if callable(compact_library) else client.library()
            except AniListError as exc:
                cached = self.db.anime_list(("CURRENT", "PLANNING"))
                if not cached:
                    raise
                self._last_anilist_used_cache = True
                self._last_anilist_warning = (
                    "AniList временно не отдал список; показаны последние сохранённые данные"
                )
                self.db.set_state("anilist_last_error", str(exc))
                self.log(f"AniList: {self._last_anilist_warning}: {exc}")
                self.logger.warning(
                    "FALLBACK step=anilist.library cached=%s error=%r",
                    len(cached),
                    exc,
                )
                return cached

            cached_by_id = {
                item.media_id: item
                for item in self.db.anime_list(("CURRENT", "PLANNING"))
            }
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
            self._last_anilist_warning = str(getattr(client, "last_library_warning", ""))
        finally:
            client.close()

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
        self.db.set_state("anilist_synced_at", str(time.time()))
        self.db.set_state("anilist_last_error", "")
        self.db.set_state("anilist_last_warning", self._last_anilist_warning)
        self.log(f"AniList: обновлено карточек — {len(items)}")
        if self._last_anilist_warning:
            self.log(f"AniList: {self._last_anilist_warning}")
        if restored:
            self.log(
                f"AniList: отменён просмотр локальных серий — {restored}; "
                "они возвращены в ready"
            )
        return items

    def refresh_anilist_cache(self) -> dict[str, object]:
        with timed_step(self.logger, "anilist.refresh_cache"):
            items = self.sync_anilist()
            with timed_step(self.logger, "covers.prefetch", anime_count=len(items)):
                covers = self.prefetch_covers(("CURRENT", "PLANNING"))
            relation_graphs = self.refresh_due_relation_graphs(limit=1)
        return {
            "anime": len(items),
            "covers": covers,
            "relation_graphs": relation_graphs,
            "cached": self._last_anilist_used_cache,
            "warning": self._last_anilist_warning,
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
        try:
            with timed_step(
                self.logger,
                "anilist.periodic_refresh",
                interval_minutes=self.config.agent.anilist_refresh_minutes,
            ):
                items = self.sync_anilist()
        except Exception as exc:
            # A temporary AniList failure must not block torrent/subtitle work.
            self.log(f"AniList: фоновое обновление не удалось: {exc}")
            self.logger.warning("RETRY step=anilist.periodic_refresh error=%r", exc)
            return 0
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
            raise ManagerError("AniList relation graph has no nodes")
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
        relation_updates = {
            media_id: compact_relations_from_graph(graph, media_id)
            for media_id in node_ids
        }
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
        """Treat fully downloaded aria2 payloads as usable once verification is done.

        aria2 keeps a BitTorrent task ``active`` while it is seeding, so requiring
        state=complete strands a fully downloaded video until seeding stops. At
        the same time a hash check may temporarily report completedLength equal
        to totalLength, therefore the explicit verifier/error flags remain the
        safety gate.
        """
        if float(item.progress or 0.0) < 0.999:
            return False
        raw = getattr(item, "raw", {}) or {}
        if str(raw.get("backend") or "").casefold() != "aria2":
            return True
        if bool(raw.get("verifying")):
            return False
        if str(raw.get("error_code") or "").strip():
            return False
        total = max(0, int(raw.get("total_size") or 0))
        downloaded = max(0, int(raw.get("downloaded") or 0))
        if total > 0:
            return downloaded >= total
        return str(item.state or "").casefold() == "complete"

    def incomplete_download_paths(self) -> tuple[Path, ...]:
        paths: list[Path] = []
        for item in self.db.downloads():
            if self._download_is_complete(item):
                continue
            path = self._download_content_path(item)
            if path is not None:
                paths.append(path)
        return tuple(dict.fromkeys(paths))

    @staticmethod
    def _path_within(path: Path, roots: tuple[Path, ...]) -> bool:
        resolved = path.expanduser().resolve()
        return any(resolved == root or root in resolved.parents for root in roots)

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

    def scan_library(self) -> list[LibraryEpisode]:
        self._repair_legacy_library_episode_paths()
        excluded = self.incomplete_download_paths()
        # Remove rows created by older versions that scanned qBittorrent's
        # partially allocated video files as if they were finished local media.
        # Also forget manually deleted local-only files. Torrent-backed rows are
        # reconciled against qBittorrent in ``sync_downloads`` so an active move
        # or recheck does not erase them prematurely.
        for item in self.db.episodes():
            if self._path_within(item.video_path, excluded) or (
                not item.video_path.is_file() and not item.torrent_hash
            ):
                self.db.delete_episode_record(item.video_path)
        items = scan_library(
            self.config.library.root_dir,
            self.db,
            recursive=self.config.library.recursive,
            ffprobe=self.config.tools.ffprobe,
            ffmpeg=self.config.tools.ffmpeg,
            excluded_paths=excluded,
            pipeline_cache_config=self.config,
        )

        # Additional watched folders (Downloads, an external drive, etc.) are
        # intentionally stricter than the managed Library root: arbitrary video
        # files are ignored unless they can be mapped confidently to a known or
        # public AniList anime entry.
        main_root = self.config.library.root_dir.expanduser().resolve()
        extra_roots: list[Path] = []
        for configured in self.config.paths.download_dirs:
            root = configured.expanduser()
            if not root.is_dir():
                continue
            resolved_root = root.resolve()
            if resolved_root == main_root or main_root in resolved_root.parents:
                continue
            if any(resolved_root == existing or existing in resolved_root.parents for existing in extra_roots):
                continue
            extra_roots.append(resolved_root)

        # v0.6.37 briefly reused the old download_dirs config key for media
        # watching. On upgraded installations that key could still point to a
        # subtitle/Downloads folder, causing unrelated batch files to be
        # imported into Waiting for preparation. Once the active watched roots
        # are known, remove only unresolved external rows that are no longer
        # inside any managed root. Ready/watched/manual-progress rows and
        # torrent-managed files are deliberately preserved.
        active_media_roots = (main_root, *extra_roots)
        for stale in list(self.db.episodes()):
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
                str(stale.video_path), stale.media_id, stale.state,
            )
            self.db.delete_episode_record(stale.video_path)

        # Detach obviously impossible historical watched-folder matches without
        # contacting AniList. This is intentionally stronger than the network
        # revalidation below so a rate limit (HTTP 429) cannot keep e.g.
        # catmahjong.mp4 attached to the old Mahoutsukai/Mahoyo false match.
        for stale in list(self.db.episodes()):
            resolved = stale.video_path.expanduser().resolve()
            if not self._path_within(resolved, tuple(extra_roots)):
                continue
            if stale.media_id is None or stale.state == "watched" or stale.watched_at:
                continue
            if float(stale.playback_position or 0.0) > 0.0:
                continue
            managed_download = (
                self.db.download_by_hash(stale.torrent_hash) if stale.torrent_hash else None
            )
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
                str(resolved), stale.media_id, strict,
            )
            self.db.delete_episode_record(resolved)

        client: AniListClient | None = None
        resolver_cache: dict[tuple[str, int | None, int | None], LibraryAnime | None] = {}
        resolver_had_error = False
        resolver_rate_limited = False
        resolver_started_at = time.monotonic()
        resolver_network_queries = 0
        # Watched folders can contain thousands of unrelated videos. Never let
        # their public-AniList matching turn an ordinary Refresh into a many-minute
        # crawl. Known/local matches remain unlimited; remote discovery gets a
        # small wall-clock/query budget and a short per-request timeout.
        resolver_budget_seconds = 10.0
        resolver_query_limit = 8
        if extra_roots and self.config.anilist.enabled:
            client = AniListClient(self.config.anilist.endpoint, self.config.anilist.access_token, timeout=3.0)

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

        def resolve_external(identity) -> LibraryAnime | None:
            nonlocal resolver_had_error, resolver_rate_limited, resolver_network_queries
            key = (str(identity.title).casefold(), identity.season, identity.episode)
            if key in resolver_cache:
                return resolver_cache[key]
            if client is None or resolver_rate_limited:
                resolver_cache[key] = None
                return None
            # Reject obvious camera/export/random filenames before any network I/O.
            title_text = str(identity.title or "").strip()
            meaningful = re.sub(r"[\W\d_]+", "", title_text, flags=re.UNICODE)
            if len(meaningful) < 4 or (identity.episode is None and len(title_text.split()) <= 1 and len(meaningful) < 7):
                resolver_cache[key] = None
                return None
            if resolver_network_queries >= resolver_query_limit or time.monotonic() - resolver_started_at >= resolver_budget_seconds:
                resolver_cache[key] = None
                return None
            resolver_network_queries += 1
            try:
                matches = client.search(identity)
            except AniListError as exc:
                resolver_had_error = True
                if isinstance(exc, AniListHTTPError) and exc.status_code == 429:
                    resolver_rate_limited = True
                    self.logger.warning("BACKOFF step=library.external_match reason=anilist_429 remaining_files_skipped=1 title=%r", identity.title)
                else:
                    self.logger.warning("FALLBACK step=library.external_match title=%r error=%r", identity.title, str(exc))
                resolver_cache[key] = None
                return None
            if not matches:
                resolver_cache[key] = None
                return None

            def identity_score(candidate) -> float:
                score = float(candidate.score)
                if identity.season is not None:
                    candidate_seasons = [
                        value
                        for value in (_season_number(name) for name in [*candidate.titles, *candidate.synonyms])
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
            if (
                top_score < 82.0
                or top_score - runner_up_score < 8.0
                or strict_score < strict_threshold
            ):
                self.logger.info(
                    "SKIP step=library.external_match title=%r top=%s score=%.1f strict=%.1f margin=%.1f reason=ambiguous",
                    identity.title, top.id, top_score, strict_score, top_score - runner_up_score,
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
                identity.title, anime.media_id, top_score,
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
                    require_anime_match=True,
                )
                items.extend(external_items)
                matched_external_paths.update(item.video_path.resolve() for item in external_items)

            if resolver_network_queries:
                self.logger.info(
                    "RESULT step=library.external_match_budget queries=%s elapsed_ms=%.1f rate_limited=%s",
                    resolver_network_queries, (time.monotonic() - resolver_started_at) * 1000.0, resolver_rate_limited,
                )

            # Revalidate auto-imports created by older fuzzy matching, including
            # rows that already reached ``ready``.  The file itself is untouched;
            # only the incorrect DB association/subtitle job is removed. Preserve
            # anything the user actually watched/resumed and never purge on an
            # AniList network failure, because absence of a match is not then
            # trustworthy.
            if not resolver_had_error:
                for stale in list(self.db.episodes()):
                    resolved = stale.video_path.expanduser().resolve()
                    if not self._path_within(resolved, tuple(extra_roots)):
                        continue
                    if resolved in matched_external_paths:
                        continue
                    managed_download = (
                        self.db.download_by_hash(stale.torrent_hash)
                        if stale.torrent_hash
                        else None
                    )
                    if (
                        managed_download is not None
                        and managed_download.media_id == stale.media_id
                    ):
                        continue
                    if stale.state == "watched" or stale.watched_at:
                        continue
                    if float(stale.playback_position or 0.0) > 0.0:
                        continue
                    self.logger.info(
                        "CLEANUP step=library.external_false_match path=%r media_id=%s state=%s",
                        str(resolved), stale.media_id, stale.state,
                    )
                    self.db.delete_episode_record(resolved)
        finally:
            if client is not None:
                client.close()

        self._prune_empty_library_dirs()
        self.db.set_state("library_scanned_at", str(time.time()))
        self.log(f"Библиотека: найдено видео — {len(items)}")
        return items

    def nyaa_client(self, *, timeout: float = 20.0) -> NyaaClient:
        return NyaaClient(
            self.config.nyaa.base_url,
            proxy_mode=self.config.nyaa.proxy_mode,
            proxy_url=self.config.nyaa.proxy_url,
            pre_search_command=self.config.nyaa.pre_search_command,
            category=self.config.nyaa.category,
            timeout=timeout,
        )

    def downloads_enabled(self) -> bool:
        return bool(self.config.qbittorrent.enabled or self.config.aria2.enabled)

    def torrent_backend_name(self) -> str:
        if self.config.qbittorrent.enabled and self.config.aria2.enabled:
            return "qBittorrent + aria2"
        return "qBittorrent" if self.config.qbittorrent.enabled else "aria2"

    def torrent_paused_on_add(self) -> bool:
        if self.config.qbittorrent.enabled:
            return bool(self.config.qbittorrent.paused_on_add)
        return bool(self.config.aria2.paused_on_add)

    def qbt_client(self) -> QBittorrentClient | Aria2Client:
        # Keep the historical method name so older integrations/tests remain
        # compatible. The returned object implements the common torrent client
        # surface used by pudge.
        if self.config.qbittorrent.enabled:
            qbt = self.config.qbittorrent
            return QBittorrentClient(
                qbt.base_url,
                qbt.username,
                qbt.password,
                qbt.api_key,
                verify_tls=qbt.verify_tls,
                pre_download_command=qbt.pre_download_command,
                auto_start_app=qbt.auto_start_app,
            )
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
        raise ManagerError("Все torrent backend отключены")

    def torrent_clients(self) -> list[tuple[str, QBittorrentClient | Aria2Client]]:
        """Return every enabled backend; qBittorrent remains the add preference."""
        if self.config.qbittorrent.enabled != self.config.aria2.enabled:
            return [(
                "qbittorrent" if self.config.qbittorrent.enabled else "aria2",
                self.qbt_client(),
            )]
        result: list[tuple[str, QBittorrentClient | Aria2Client]] = []
        if self.config.qbittorrent.enabled:
            qbt = self.config.qbittorrent
            result.append(("qbittorrent", QBittorrentClient(
                qbt.base_url, qbt.username, qbt.password, qbt.api_key,
                verify_tls=qbt.verify_tls,
                pre_download_command=qbt.pre_download_command,
                auto_start_app=qbt.auto_start_app,
            )))
        if self.config.aria2.enabled:
            aria = self.config.aria2
            result.append(("aria2", Aria2Client(
                enabled=aria.enabled, binary=aria.binary, rpc_port=aria.rpc_port,
                pre_download_command=self.config.qbittorrent.pre_download_command,
                paused_on_add=aria.paused_on_add, auto_start=aria.auto_start,
                source_proxy_mode=self.config.nyaa.proxy_mode,
                source_proxy_url=self.config.nyaa.proxy_url,
                seed_mode=aria.seed_mode, seed_ratio=aria.seed_ratio,
                seed_time_minutes=aria.seed_time_minutes,
                upload_limit_kib=aria.upload_limit_kib,
                vpn_interface=aria.vpn_interface,
                vpn_kill_switch=aria.vpn_kill_switch,
            )))
        return result

    def _release_episode_context_from_graph(
        self,
        anime: LibraryAnime,
        episode: int,
    ) -> tuple[tuple[int, ...], tuple[str, ...]]:
        """Resolve split-cour numbering from the already cached relation graph.

        This path is intentionally network-free.  Why-not-ready/manual Nyaa
        searches must not lose absolute numbering merely because AniList is
        temporarily unavailable.  Only chronological SEQUEL edges are followed
        and long-running parents are excluded with the same conservative guard
        used by the live AniList resolver.
        """
        graph_lookup = getattr(self.db, "relation_graph_for_media", None)
        if graph_lookup is None:
            return (), ()
        cached = graph_lookup(anime.media_id)
        graph = cached.get("graph") if isinstance(cached, dict) else None
        if not isinstance(graph, dict):
            return (), ()

        nodes = {
            int(node["media_id"]): node
            for node in graph.get("nodes", [])
            if isinstance(node, dict) and node.get("media_id") is not None
        }
        if anime.media_id not in nodes:
            return (), ()

        incoming: dict[int, list[int]] = {}
        for edge in graph.get("edges", []):
            if not isinstance(edge, dict) or str(edge.get("relation_type") or "").upper() != "SEQUEL":
                continue
            try:
                source = int(edge.get("source"))
                target = int(edge.get("target"))
            except (TypeError, ValueError):
                continue
            if source in nodes and target in nodes and source != target:
                incoming.setdefault(target, []).append(source)

        valid_formats = {"TV", "TV_SHORT", "ONA", ""}
        episode_cap = max(100, int(anime.episodes or 0) * 4)
        current_id = int(anime.media_id)
        visited = {current_id}
        predecessors: list[dict[str, Any]] = []
        total = int(episode)

        for _ in range(12):
            current = nodes[current_id]
            current_title = str(current.get("title") or anime.title)
            candidates: list[tuple[float, dict[str, Any]]] = []
            for source_id in incoming.get(current_id, []):
                if source_id in visited:
                    continue
                candidate = nodes[source_id]
                candidate_format = str(candidate.get("format") or "").upper()
                try:
                    count = int(candidate.get("episodes") or 0)
                except (TypeError, ValueError):
                    count = 0
                if candidate_format not in valid_formats or count < 1 or count > episode_cap:
                    continue
                continuity = title_similarity(
                    current_title, str(candidate.get("title") or "")
                )
                if continuity < 35.0:
                    continue
                candidates.append((continuity, candidate))
            if not candidates:
                break
            _continuity, candidate = max(
                candidates,
                key=lambda item: (
                    item[0],
                    str(item[1].get("start_date") or ""),
                    int(item[1].get("media_id") or 0),
                ),
            )
            total += int(candidate.get("episodes") or 0)
            current_id = int(candidate["media_id"])
            visited.add(current_id)
            predecessors.insert(0, candidate)

        if total <= episode:
            return (), ()
        titles = tuple(
            dict.fromkeys(
                str(item.get("title") or "").strip()
                for item in predecessors
                if str(item.get("title") or "").strip()
            )
        )
        self.logger.info(
            "RESULT step=nyaa.episode_aliases media_id=%s relative=%s absolute=%s offset=%s source=relation_graph",
            anime.media_id, episode, total, total - episode,
        )
        return (total,), titles

    def _media_episode_from_release(
        self,
        anime: LibraryAnime | None,
        release_episode: int | None,
        *,
        requested_media_episode: int | None = None,
    ) -> int | None:
        """Map a release filename number to this AniList entry's local number.

        Single-episode downloads carry the requested local number explicitly.
        Batch files are mapped with the cached relation graph, without a network
        request from the download worker.
        """
        if requested_media_episode is not None:
            return int(requested_media_episode)
        if release_episode is None:
            return None
        value = int(release_episode)
        if anime is None:
            return value
        total = int(anime.episodes or 0)
        if value >= 1 and (not total or value <= total):
            return value
        try:
            aliases, _titles = self._release_episode_context_from_graph(anime, 1)
        except (OSError, RuntimeError, TypeError, ValueError):
            aliases = ()
        if aliases:
            offset = int(aliases[0]) - 1
            local = value - offset
            if local >= 1 and (not total or local <= total):
                return local
        self.logger.warning(
            "WARN step=episode_identity media_id=%s release_episode=%s "
            "reason=no_safe_media_mapping",
            anime.media_id,
            value,
        )
        return None

    def _release_episode_context(
        self,
        anime: LibraryAnime,
        episode: int | None,
    ) -> tuple[tuple[int, ...], tuple[str, ...]]:
        """Return safe absolute-number and predecessor-title aliases for releases.

        Split-cour shows are often numbered relative to the AniList entry but
        absolute across the whole arc by release groups (e.g. BLEACH cour 4
        episode 3 is published as episode 43). These aliases are supplemental:
        failures here never block the normal Nyaa search.
        """
        if episode is None or episode < 1:
            return (), ()
        if (anime.format or "").upper() not in {"TV", "TV_SHORT", "ONA", ""}:
            return (), ()

        cache_path = (
            self.config.paths.cache_dir
            / "anilist-release-numbering"
            / f"{anime.media_id}.json"
        )
        try:
            if cache_path.is_file() and time.time() - cache_path.stat().st_mtime < 7 * 86400:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                offset = max(0, int(payload.get("offset", 0)))
                titles = tuple(
                    str(value).strip()
                    for value in payload.get("prequel_titles", [])
                    if str(value).strip()
                )
                if offset:
                    absolute = int(episode) + offset
                    self.logger.info(
                        "RESULT step=nyaa.episode_aliases media_id=%s relative=%s absolute=%s offset=%s source=cache",
                        anime.media_id, episode, absolute, offset,
                    )
                    return (absolute,), titles
                return (), ()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

        graph_episodes, graph_titles = self._release_episode_context_from_graph(
            anime, int(episode)
        )
        if graph_episodes:
            return graph_episodes, graph_titles

        if not self.config.anilist.enabled:
            return (), ()

        start = AniListAnime(
            id=anime.media_id,
            titles=list(dict.fromkeys([anime.title, *anime.titles])),
            synonyms=list(anime.synonyms),
            season_year=anime.season_year,
            episodes=anime.episodes,
            format=anime.format,
        )
        client = AniListClient(
            self.config.anilist.endpoint,
            access_token=self.config.anilist.access_token,
        )
        try:
            absolute, chain = client.absolute_episode_number(start, int(episode))
        except (AniListError, OSError, ValueError) as exc:
            self.logger.info(
                "SKIP step=nyaa.episode_aliases media_id=%s episode=%s reason=%r",
                anime.media_id, episode, exc,
            )
            return (), ()
        finally:
            client.close()

        offset = max(0, int(absolute) - int(episode))
        if not offset:
            return (), ()

        predecessor_titles: list[str] = []
        for item in chain[:-1]:
            for value in [*item.titles, *item.synonyms]:
                value = str(value).strip()
                if value and value not in predecessor_titles:
                    predecessor_titles.append(value)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "media_id": anime.media_id,
                        "offset": offset,
                        "chain": [item.id for item in chain],
                        "prequel_titles": predecessor_titles,
                        "updated_at": time.time(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
        self.logger.info(
            "RESULT step=nyaa.episode_aliases media_id=%s relative=%s absolute=%s offset=%s chain=%s",
            anime.media_id, episode, absolute, offset, [item.id for item in chain],
        )
        return (int(absolute),), tuple(predecessor_titles)

    def search_releases(
        self,
        media_id: int,
        *,
        episode: int | None = None,
        batch: bool = False,
        automatic: bool = False,
    ) -> list[NyaaRelease]:
        anime = self.db.get_anime(media_id)
        if anime is None:
            raise ManagerError(f"AniList id={media_id} отсутствует в локальной базе")
        if not self.config.nyaa.enabled:
            raise ManagerError("Поиск релизов отключён")

        alternative_episodes, alternative_titles = self._release_episode_context(
            anime, episode
        )
        common_kwargs = {
            "alternative_episodes": alternative_episodes,
            "alternative_titles": alternative_titles,
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

        def search_nyaa_source() -> tuple[list[NyaaRelease], NyaaError | None]:
            try:
                with timed_step(
                    self.logger,
                    "nyaa.search",
                    media_id=media_id,
                    episode=episode,
                    batch=batch,
                    title=anime.title,
                ):
                    return search_ranked(
                        self.nyaa_client(timeout=6.0 if automatic else 20.0),
                        anime,
                        # Automatic refresh should search the same title aliases as
                        # Find episode. The wall-clock budget below keeps background
                        # refresh bounded without silently dropping aliases 4-5.
                        max_queries=5,
                        query_budget_seconds=18.0 if automatic else None,
                        **common_kwargs,
                    ), None
            except NyaaError as exc:
                self.logger.warning(
                    "FALLBACK step=nyaa.search media_id=%s episode=%s reason=nyaa_error error=%r",
                    media_id, episode, str(exc),
                )
                return [], exc

        def search_subsplease_source(
            *,
            reason: str,
        ) -> tuple[list[NyaaRelease], NyaaError | None]:
            self.logger.info(
                "START step=subsplease.rss_source media_id=%s episode=%s batch=%s reason=%s",
                media_id, episode, batch, reason,
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
                        subsplease_client = SubsPleaseClient(timeout=8.0, cache_ttl=60.0)
                        self._subsplease_client = subsplease_client
                    releases = search_subsplease_ranked(
                        subsplease_client,
                        anime,
                        **common_kwargs,
                    )
            except NyaaError as exc:
                self.logger.warning(
                    "FAIL step=subsplease.rss_source media_id=%s episode=%s error=%r",
                    media_id, episode, str(exc),
                )
                return [], exc
            self.logger.info(
                "DONE step=subsplease.rss_source media_id=%s episode=%s candidates=%s",
                media_id, episode, len(releases),
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

        rss_enabled = bool(self.config.nyaa.subsplease_rss_enabled)
        prefer_rss = bool(
            rss_enabled and self.config.nyaa.subsplease_rss_preferred
        )
        releases: list[NyaaRelease] = []
        nyaa_error: NyaaError | None = None
        subsplease_error: NyaaError | None = None

        if prefer_rss:
            self.logger.info(
                "SOURCE_ORDER step=release.search media_id=%s episode=%s order=subsplease_then_nyaa",
                media_id, episode,
            )
            releases, subsplease_error = search_subsplease_source(
                reason="preferred_source",
            )
            suitable_rss = any(
                self._release_is_allowed_for_auto(item) for item in releases
            )
            if not suitable_rss:
                nyaa_releases, nyaa_error = search_nyaa_source()
                releases = merge_releases(releases, nyaa_releases)
                if subsplease_error is not None and nyaa_error is not None and not releases:
                    raise NyaaError(
                        f"{subsplease_error}; {nyaa_error}"
                    ) from nyaa_error
        else:
            self.logger.info(
                "SOURCE_ORDER step=release.search media_id=%s episode=%s order=nyaa_then_subsplease",
                media_id, episode,
            )
            releases, nyaa_error = search_nyaa_source()
            suitable_nyaa = any(
                self._release_is_allowed_for_auto(item) for item in releases
            )
            if rss_enabled and (nyaa_error is not None or not suitable_nyaa):
                reason = (
                    "nyaa_error"
                    if nyaa_error is not None
                    else "no_suitable_nyaa_release"
                )
                subsplease, subsplease_error = search_subsplease_source(reason=reason)
                releases = merge_releases(releases, subsplease)
                if nyaa_error is not None and subsplease_error is not None and not releases:
                    raise NyaaError(
                        f"{nyaa_error}; {subsplease_error}"
                    ) from subsplease_error
            elif nyaa_error is not None:
                raise nyaa_error

        if releases:
            best = releases[0]
            self.logger.info(
                "RESULT step=nyaa.search media_id=%s episode=%s candidates=%s best_score=%.1f seeds=%s title=%s source_order=%s",
                media_id,
                episode,
                len(releases),
                best.score,
                best.seeders,
                best.title,
                "subsplease_then_nyaa" if prefer_rss else "nyaa_then_subsplease",
            )
        else:
            self.logger.info(
                "RESULT step=nyaa.search media_id=%s episode=%s candidates=0 source_order=%s",
                media_id,
                episode,
                "subsplease_then_nyaa" if prefer_rss else "nyaa_then_subsplease",
            )
        return releases

    def add_release(
        self,
        media_id: int,
        release: NyaaRelease,
        *,
        episode: int | None,
        batch: bool,
    ) -> bool:
        if not self.downloads_enabled():
            raise ManagerError("Torrent-загрузки отключены в настройках")
        anime = self.db.get_anime(media_id)
        if anime is None:
            raise ManagerError(f"AniList id={media_id} отсутствует в базе")
        selected_release_episode = None
        if not batch:
            for reason in release.reasons:
                if str(reason).startswith("absolute-ep="):
                    try:
                        selected_release_episode = int(str(reason).split("=", 1)[1])
                    except ValueError:
                        pass
                    break
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
        except OSError as exc:
            self.logger.warning(
                "WARN step=library.anilist_sidecar media_id=%s path=%r error=%r",
                anime.media_id, str(target), str(exc),
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

        # Do not add the same request to the preferred client when it already
        # exists in the other enabled backend. This also covers users migrating
        # gradually from qBittorrent to aria2 (or the other way around).
        for backend_name, candidate_client in self.torrent_clients():
            try:
                existing_any = self._existing_download_for_request(
                    candidate_client, anime, episode=episode, batch=batch,
                    release_hash=release.info_hash,
                )
                if existing_any is None:
                    continue
                existing_any.media_episode = episode
                existing_any.episode = episode
                existing_any.release_episode = selected_release_episode
                existing_any.raw["backend"] = backend_name
                self.db.upsert_download(existing_any)
                paused_on_add = (
                    self.config.qbittorrent.paused_on_add
                    if backend_name == "qbittorrent"
                    else self.config.aria2.paused_on_add
                )
                if (
                    not paused_on_add
                    and str(existing_any.state or "").casefold().startswith(("paused", "stopped"))
                ):
                    candidate_client.start(existing_any.torrent_hash)
                self.log(
                    f"{backend_name}: {anime.title} уже скачивается — {existing_any.name}"
                )
                return False
            finally:
                candidate_client.close()
        client = self.qbt_client()
        add_backend = str(getattr(client, "backend_name", "qbittorrent") or "qbittorrent")
        try:
            existing = self._existing_download_for_request(
                client, anime, episode=episode, batch=batch,
                release_hash=release.info_hash,
            )
            if existing is not None:
                existing.media_episode = episode
                existing.episode = episode
                existing.release_episode = selected_release_episode
                repaired = False
                repair = getattr(client, "repair_stalled_release", None)
                if callable(repair):
                    try:
                        repaired = bool(repair(existing.torrent_hash, release))
                    except QBittorrentError as exc:
                        self.logger.warning(
                            "FALLBACK step=aria2.metadata_source hash=%s error=%r",
                            existing.torrent_hash, str(exc),
                        )
                    if repaired:
                        self.logger.info(
                            "REPAIR step=aria2.metadata_source hash=%s media_id=%s episode=%s",
                            existing.torrent_hash, media_id, episode,
                        )
                        refreshed = self._existing_download_for_request(
                            client, anime, episode=episode, batch=batch,
                            release_hash=release.info_hash,
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
                    client.start(existing.torrent_hash)
                    self.logger.info(
                        "REPAIR step=%s.start_existing hash=%s state=%s media_id=%s episode=%s",
                        add_backend, existing.torrent_hash, existing.state,
                        media_id, episode,
                    )
                self.log(
                    f"{self.torrent_backend_name()}: {anime.title} уже скачивается — {existing.name}"
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
                client.add_release(
                    release,
                    save_path=target,
                    category=self.config.qbittorrent.category,
                    tags=tags,
                    paused=self.torrent_paused_on_add(),
                )
            if getattr(client, "torrents", None) is not None:
                verified = None
                for delay in (0.0, 0.25, 0.75, 1.5):
                    if delay:
                        time.sleep(delay)
                    verified = self._existing_download_for_request(
                        client, anime, episode=episode, batch=batch,
                        release_hash=release.info_hash,
                    )
                    if verified is not None:
                        break
                if verified is None:
                    self.logger.error(
                        "ERROR step=%s.add_verify media_id=%s episode=%s batch=%s "
                        "hash=%s title=%r reason=not_visible_after_add",
                        add_backend, media_id, episode, batch,
                        release.info_hash, release.title,
                    )
                    raise ManagerError(
                        f"{add_backend} принял запрос, но торрент не появился в списке загрузок. "
                        "Повтори попытку; если ошибка сохранится, открой Diagnostics — там будет "
                        f"причина проверки {add_backend}."
                    )
                verified.media_episode = episode
                verified.episode = episode
                verified.release_episode = selected_release_episode
                self.db.upsert_download(verified)
                if not self.torrent_paused_on_add() and hasattr(client, "start"):
                    client.start(verified.torrent_hash)
                    self.logger.info(
                        "DONE step=%s.start_after_add hash=%s media_id=%s episode=%s",
                        add_backend, verified.torrent_hash, media_id, episode,
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
        )
        self.log(
            f"{self.torrent_backend_name()}: добавлен {release.title} "
            f"(score={release.score:.1f}, seeds={release.seeders})"
        )
        return True

    @staticmethod
    def _safe_dir_name(value: str) -> str:
        return "".join("_" if char in '/\\:*?\"<>|' else char for char in value).strip() or "Anime"

    @staticmethod
    def _safe_qbt_tag(value: str) -> str:
        # qBittorrent separates tags with commas. Keep the visible title readable.
        clean = " ".join(value.replace(",", " ").split())
        return clean[:120] or "Unknown anime"

    @staticmethod
    def _normalized_title(value: str) -> str:
        return "".join(char for char in value.casefold() if char.isalnum())

    def _resolve_download_media(self, item: DownloadItem) -> None:
        persisted = self.db.download_by_hash(item.torrent_hash)
        if persisted is not None:
            if item.media_id is None:
                item.media_id = persisted.media_id
            if persisted.media_episode is not None:
                item.media_episode = persisted.media_episode
                item.episode = persisted.media_episode
            if persisted.release_episode is not None:
                item.release_episode = persisted.release_episode
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
                if wanted and any(
                    self._normalized_title(name) == wanted for name in names if name
                ):
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
    ):
        torrents = getattr(client, "torrents", None)
        if torrents is None:
            return None
        wanted_hash = str(release_hash or "").strip().casefold()
        invalid_states = {"error", "missingfiles", "unknown"}
        for item in torrents(category=""):
            raw_hashes = {
                str(item.torrent_hash or "").strip().casefold(),
                str(item.raw.get("infohash_v1") or "").strip().casefold(),
                str(item.raw.get("infohash_v2") or "").strip().casefold(),
            }
            same_hash = bool(wanted_hash and wanted_hash in raw_hashes)
            self._resolve_download_media(item)
            source = str(item.raw.get("_media_id_source") or "unknown")
            state = str(item.state or "").strip().casefold()
            self.logger.info(
                "CANDIDATE step=qbittorrent.duplicate_check media_id=%s torrent=%s "
                "resolved_media_id=%s source=%s state=%s progress=%.4f episode=%s "
                "batch=%s same_hash=%s name=%r",
                anime.media_id, item.torrent_hash, item.media_id, source, item.state,
                float(item.progress or 0.0), item.episode, item.is_batch, same_hash, item.name,
            )
            recoverable_aria2_error = bool(
                state == "error"
                and (item.raw or {}).get("recoverable_missing_control")
            )
            if state in invalid_states and not recoverable_aria2_error:
                self.logger.info(
                    "SKIP step=qbittorrent.duplicate_check torrent=%s reason=invalid_state state=%s",
                    item.torrent_hash, item.state,
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
                    item.torrent_hash, item.raw.get("_media_id_score"),
                )
                continue
            if (
                not same_hash
                and source == "exact_title"
                and float(item.progress or 0.0) < 0.01
            ):
                self.logger.info(
                    "SKIP step=qbittorrent.duplicate_check torrent=%s reason=unlinked_zero_progress",
                    item.torrent_hash,
                )
                continue

            request_matches = False
            if batch:
                request_matches = bool(
                    item.is_batch or anime.format == "MOVIE" or item.episode is None
                )
            else:
                request_matches = bool(item.is_batch or item.episode == episode)
            if not request_matches:
                continue
            self.logger.info(
                "MATCH step=qbittorrent.duplicate_check media_id=%s torrent=%s source=%s "
                "state=%s progress=%.4f",
                anime.media_id, item.torrent_hash, source, item.state,
                float(item.progress or 0.0),
            )
            return item
        self.logger.info(
            "RESULT step=qbittorrent.duplicate_check media_id=%s episode=%s batch=%s match=none",
            anime.media_id, episode, batch,
        )
        return None

    @staticmethod
    def _torrent_tags(item: DownloadItem) -> set[str]:
        raw_tags = item.raw.get("_tag_set")
        if isinstance(raw_tags, list):
            return {str(tag).strip() for tag in raw_tags if str(tag).strip()}
        return {
            value.strip()
            for value in str(item.raw.get("tags") or "").split(",")
            if value.strip()
        }

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

    def _duplicate_identity(
        self, item: DownloadItem
    ) -> tuple[str, str] | None:
        if not self._download_is_managed(item):
            return None
        tags = self._torrent_tags(item)
        tagged_title = next(
            (
                tag.split(":", 1)[1].strip()
                for tag in tags
                if tag.casefold().startswith("anime:")
            ),
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
        elif item.is_batch or any(
            tag.casefold() in {"batch", "series pack"} for tag in tags
        ):
            kind = "batch"
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

    def _metadata_tags_for_download(
        self, item: DownloadItem, score: float | None
    ) -> list[str]:
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

                completed_copies = [
                    item for item in duplicates if self._download_is_complete(item)
                ]
                preserved = completed_copies or [winner]
                for loser in losers:
                    if self._download_is_complete(loser):
                        continue
                    delete_files = self._can_delete_duplicate_files(loser, preserved)
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

    @staticmethod
    def _torrent_race_is_alive(snapshot: dict[str, Any] | None) -> bool:
        if not snapshot:
            return False
        try:
            speed = int(snapshot.get("dlspeed") or 0)
            downloaded = int(snapshot.get("downloaded") or 0)
            progress = float(snapshot.get("progress") or 0.0)
        except (TypeError, ValueError):
            return False
        return bool(speed > 0 or downloaded >= 256 * 1024 or progress > 0.0)

    def _race_add_candidates(
        self,
        media_id: int,
        candidates: list[NyaaRelease],
        *,
        episode: int | None,
        batch: bool,
        fast_seconds: float = 10.0,
        total_seconds: float = 120.0,
        poll_seconds: float = 2.0,
    ) -> NyaaRelease | None:
        """Race up to three qBittorrent candidates and keep the best one that moves."""
        pool = list(candidates[:3])
        if not pool:
            return None
        if not self.config.qbittorrent.enabled or len(pool) == 1 or self.torrent_paused_on_add():
            self.add_release(media_id, pool[0], episode=episode, batch=batch)
            return pool[0]

        anime = self.db.get_anime(media_id)
        if anime is None:
            raise ManagerError(f"AniList id={media_id} отсутствует в базе")

        target = self.config.library.root_dir / self._safe_dir_name(anime.title)
        target.mkdir(parents=True, exist_ok=True)
        try:
            (target / ".anilist.id").write_text(str(anime.media_id), encoding="utf-8")
        except OSError as exc:
            self.logger.warning(
                "WARN step=torrent.race_sidecar media_id=%s path=%r error=%r",
                media_id, str(target), str(exc),
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
        stale_existing_hash = ""
        started_at = time.monotonic()

        def add_one(index: int, release: NyaaRelease) -> bool:
            # Never hijack/delete a torrent that was already present before this race.
            if release.info_hash:
                existing = client.torrent_status(release.info_hash)
                if existing is not None:
                    self.logger.info(
                        "SKIP step=torrent.race_add reason=already_present hash=%s title=%r",
                        release.info_hash, release.title,
                    )
                    return False
            slot = race_root / f"candidate-{index + 1}"
            torrent_hash = client.add_release(
                release,
                save_path=slot,
                category=self.config.qbittorrent.category,
                tags=[APP_SLUG, race_tag],
                paused=False,
            )
            if not torrent_hash:
                return False
            client.start(torrent_hash)
            entries.append((release, str(torrent_hash), slot))
            self.logger.info(
                "START step=torrent.race candidate=%s/%s hash=%s score=%.1f seeds=%s leechers=%s title=%r",
                index + 1, len(pool), torrent_hash, release.score,
                release.seeders, release.leechers, release.title,
            )
            return True

        def observe() -> None:
            for release, torrent_hash, _slot in entries:
                snapshot = client.torrent_status(torrent_hash)
                if snapshot is None:
                    continue
                if self._torrent_race_is_alive(snapshot):
                    live_hashes.add(torrent_hash.casefold())
                self.logger.info(
                    "POLL step=torrent.race hash=%s live=%s speed=%s downloaded=%s progress=%s seeds=%s peers=%s score=%.1f",
                    torrent_hash,
                    torrent_hash.casefold() in live_hashes,
                    snapshot.get("dlspeed"), snapshot.get("downloaded"), snapshot.get("progress"),
                    snapshot.get("num_seeds"), snapshot.get("num_leechs"), release.score,
                )

        def remove_entry(entry: tuple[NyaaRelease, str, Path]) -> None:
            _release, torrent_hash, _slot = entry
            try:
                client.delete(torrent_hash, delete_files=True)
            except QBittorrentError as exc:
                self.logger.warning(
                    "WARN step=torrent.race_delete hash=%s error=%r", torrent_hash, str(exc)
                )

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
            winner, winner_hash, _winner_slot = winner_entry
            for entry in entries:
                if entry[1] != winner_hash:
                    remove_entry(entry)
            if stale_existing_hash and stale_existing_hash.casefold() != winner_hash.casefold():
                try:
                    client.delete(stale_existing_hash, delete_files=True)
                    delete_records = getattr(self.db, "delete_torrent_records", None)
                    if callable(delete_records):
                        delete_records(stale_existing_hash)
                    self.logger.info(
                        "DONE step=torrent.race_replace_stalled old_hash=%s winner_hash=%s",
                        stale_existing_hash, winner_hash,
                    )
                except QBittorrentError as exc:
                    self.logger.warning(
                        "WARN step=torrent.race_replace_stalled old_hash=%s error=%r",
                        stale_existing_hash, str(exc),
                    )
            client.set_location(winner_hash, target)
            remover = getattr(client, "remove_tags", None)
            if callable(remover):
                remover(winner_hash, {race_tag})
            client.set_metadata(
                winner_hash,
                category=self.config.qbittorrent.category,
                tags=final_tags,
            )
            client.start(winner_hash)
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
                self.logger.warning(
                    "WARN step=torrent.race_db_sync hash=%s error=%r", winner_hash, str(exc)
                )
            release_hash = winner.info_hash or hashlib.sha1(winner.magnet.encode()).hexdigest()
            try:
                self.db.record_release(
                    release_hash,
                    media_id,
                    episode,
                    winner.title,
                    winner.score,
                    release_episode=parsed_release_episode(winner.title),
                )
            except TypeError:
                # Compatibility for small third-party/fake DB adapters that
                # still implement the pre-v5 positional surface.
                self.db.record_release(
                    release_hash, media_id, episode, winner.title, winner.score
                )
            cleaner = getattr(client, "delete_tags", None)
            if callable(cleaner):
                try:
                    cleaner({race_tag})
                except QBittorrentError:
                    pass
            cleanup_empty_dirs()
            self.log(
                f"qBittorrent: выбран живой релиз {winner.title} "
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
                        pool[0],
                    )
                    self.logger.info(
                        "DONE step=torrent.race_existing_live hash=%s title=%r",
                        existing_request.torrent_hash, existing_request.name,
                    )
                    return matched
                if float(existing_request.progress or 0.0) < 0.01:
                    stale_existing_hash = existing_request.torrent_hash

            first_added = False
            try:
                first_added = add_one(0, pool[0])
            except QBittorrentError as exc:
                self.logger.warning(
                    "FALLBACK step=torrent.race_first_add title=%r error=%r",
                    pool[0].title, str(exc),
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
                            entries[0][1], time.monotonic() - started_at,
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
                    self.logger.warning(
                        "WARN step=torrent.race_add candidate=%s title=%r error=%r",
                        index + 1, release.title, str(exc),
                    )

            if not entries:
                cleanup_empty_dirs()
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
                cleanup_empty_dirs()
                self.logger.warning(
                    "FALLBACK step=torrent.race_none_live media_id=%s episode=%s batch=%s candidates=%s",
                    media_id, episode, batch, len(entries),
                )
                return None
            return finalize(winner_entry)
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
    ) -> NyaaRelease | None:
        releases = self.search_releases(
            media_id,
            episode=episode,
            batch=batch,
            automatic=bool(automatic),
        )
        threshold = self.config.nyaa.min_release_score if require_score is None else require_score
        eligible = [
            item
            for item in releases
            if item.score >= threshold and self._release_seed_requirement_met(item)
            and (
                not require_explicit_batch
                or self._release_is_safe_batch_candidate(media_id, item)
            )
            and (
                batch
                or episode is None
                or self._release_has_safe_episode_identity(item)
            )
        ]
        best = eligible[0] if eligible else None
        record_video_selection_debug(
            self.config.paths.cache_dir,
            media_id=media_id,
            episode=episode,
            mode="manual_best",
            releases=releases,
            selected=best,
            threshold=float(threshold),
        )
        if best is None:
            return None
        if (
            self.config.qbittorrent.enabled
            and not self.torrent_paused_on_add()
            and len(eligible) > 1
        ):
            return self._race_add_candidates(
                media_id, eligible, episode=episode, batch=batch
            )
        self.add_release(media_id, best, episode=episode, batch=batch)
        return best

    def auto_search_current(self) -> int:
        if not (
            self.config.nyaa.enabled
            and self.config.nyaa.auto_download_current
            and self.downloads_enabled()
        ):
            return 0
        count = 0
        for anime in self.db.anime_list(("CURRENT",)):
            start = anime.progress + 1
            released = anime.released_episodes

            # Never guess an episode beyond the locally cached release boundary.
            # A completed/up-to-date title must not trigger Nyaa requests. The
            # later AniList sync will advance released_episodes when a new episode
            # actually airs, and the next lightweight refresh/agent pass can then
            # search for it.
            if released is None:
                self.log(
                    f"{anime.title}: Nyaa skip — released episode count is unknown"
                )
                self.logger.info(
                    "SKIP step=nyaa.auto_search media_id=%s title=%r reason=released_unknown progress=%s",
                    anime.media_id,
                    anime.title,
                    anime.progress,
                )
                continue
            if start > released:
                self.log(
                    f"{anime.title}: Nyaa skip — progress {anime.progress} >= released {released}"
                )
                self.logger.info(
                    "SKIP step=nyaa.auto_search media_id=%s title=%r reason=up_to_date progress=%s released=%s",
                    anime.media_id,
                    anime.title,
                    anime.progress,
                    released,
                )
                continue

            end = min(
                released,
                start + max(1, self.config.nyaa.max_auto_download_per_anime) - 1,
            )
            for episode in range(start, end + 1):
                if self.db.has_episode(anime.media_id, episode):
                    continue
                try:
                    releases = self.search_releases(
                        anime.media_id, episode=episode, batch=False, automatic=True
                    )
                    eligible = [
                        item for item in releases if self._release_is_allowed_for_auto(item)
                    ]
                    best = eligible[0] if eligible else None
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
                        if (
                            self.config.qbittorrent.enabled
                            and not self.torrent_paused_on_add()
                            and len(eligible) > 1
                        ):
                            best = self._race_add_candidates(
                                anime.media_id,
                                eligible,
                                episode=episode,
                                batch=False,
                            )
                        else:
                            self.add_release(
                                anime.media_id, best, episode=episode, batch=False
                            )
                except (NyaaError, QBittorrentError, ManagerError) as exc:
                    self.log(f"{anime.title} #{episode}: {exc}")
                    continue
                if best is None:
                    self.log(f"{anime.title} #{episode}: автоматически подходящий релиз пока не найден")
                    continue
                count += 1
        return count


    def _release_group_is_configured_trusted(self, item: NyaaRelease) -> bool:
        return any(
            group.casefold() in item.group.casefold()
            for group in self.config.nyaa.trusted_groups
            if group.strip()
        )

    def _release_is_trusted(self, item: NyaaRelease) -> bool:
        return bool(item.trusted or self._release_group_is_configured_trusted(item))

    def _release_seed_requirement_met(self, item: NyaaRelease) -> bool:
        fresh_trusted = fresh_trusted_zero_seeders_allowed(
            item, self.config.nyaa.trusted_groups
        )
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

    def _release_is_safe_batch_candidate(
        self,
        media_id: int,
        item: NyaaRelease,
    ) -> bool:
        """Accept a complete season pack even when its title omits 'Batch'."""
        reasons = {str(reason) for reason in item.reasons}
        if any(reason.startswith("wrong-season=") for reason in reasons):
            return False
        if "single-episode" in reasons or any(
            reason.startswith("single-episode=") for reason in reasons
        ):
            return False

        # A batch-first download must cover the whole requested AniList entry.
        # Never treat a partial explicit range as if every episode was queued.
        has_range = any(reason.startswith("range=") for reason in reasons)
        if has_range and "full-series-range" not in reasons:
            return False

        title = item.title
        if re.search(r"(?i)\b(?:movie|film|special|ova|oad|ona)\b", title):
            return False

        anime = self.db.get_anime(int(media_id))
        expected_season = _expected_season(anime) if anime is not None else 1
        season_markers = {
            int(value)
            for value in re.findall(
                r"(?i)\bS(?:eason)?[ ._-]*0*(\d{1,2})\b",
                title,
            )
        }
        if any(value != expected_season for value in season_markers):
            return False
        release_season = _season_number(title)
        if release_season is not None and release_season != expected_season:
            return False

        if item.is_batch:
            return True
        return bool(
            "large-pack-candidate" in reasons
            and "exact-title-phrase" in reasons
        )

    def _release_has_safe_episode_identity(self, item: NyaaRelease) -> bool:
        """Require structural evidence before a release can be selected as an episode."""
        blocked_reasons = {
            "wrong-episode",
            "episode-not-specified",
            "episode-pack",
            "ambiguous-episode-mismatch",
        }
        if blocked_reasons.intersection(item.reasons):
            return False

        absolute_alias = self._release_uses_absolute_episode_alias(item)
        season_mismatch = (
            "season-not-specified" in item.reasons
            or any(str(reason).startswith("wrong-season=") for reason in item.reasons)
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

        absolute_alias = self._release_uses_absolute_episode_alias(item)
        season_mismatch = (
            "season-not-specified" in item.reasons
            or any(str(reason).startswith("wrong-season=") for reason in item.reasons)
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
            status = self.storage_status()
            self.logger.info(
                "SKIP step=nyaa.auto reason=disk_limit size_bytes=%s used_bytes=%s limit_bytes=%s title=%r",
                item.size_bytes,
                status["used_bytes"],
                status["limit_bytes"],
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
        total = 0
        for path in root.rglob("*"):
            try:
                if path.is_file() and path.suffix.casefold() in VIDEO_EXTENSIONS:
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    def storage_status(self) -> dict[str, int | float | bool]:
        used = self._library_usage_bytes()
        enabled = bool(self.config.library.disk_limit_enabled)
        limit = int(max(0.0, self.config.library.disk_limit_gb) * 1024**3)
        return {
            "enabled": enabled,
            "used_bytes": used,
            "limit_bytes": limit,
            "used_gb": round(used / 1024**3, 2),
            "limit_gb": round(limit / 1024**3, 2),
            "over_limit": bool(enabled and limit > 0 and used > limit),
        }

    def _storage_can_accept(self, size_bytes: int) -> bool:
        status = self.storage_status()
        if not status["enabled"] or int(status["limit_bytes"]) <= 0:
            return True
        # Unknown sizes are not rejected: the cleanup pass will enforce the
        # configured cap once qBittorrent exposes the completed files.
        if size_bytes <= 0:
            return True
        return int(status["used_bytes"]) + int(size_bytes) <= int(status["limit_bytes"])

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
        limit_bytes = int(status["limit_bytes"])

        downloads = {item.torrent_hash.lower(): item for item in self.db.downloads()}
        candidates = sorted(
            (
                item
                for item in self.db.episodes()
                if item.state == "watched" and item.video_path.is_file()
            ),
            key=lambda item: (item.watched_at or 0.0, str(item.video_path)),
        )
        deleted = 0
        client: QBittorrentClient | None = None
        try:
            for item in candidates:
                if used_bytes <= limit_bytes:
                    break
                try:
                    item_size = item.video_path.stat().st_size
                except OSError:
                    item_size = 0
                torrent_hash = item.torrent_hash.lower().strip()
                if self.config.agent.delete_only_managed_files and not torrent_hash:
                    continue
                download = downloads.get(torrent_hash) if torrent_hash else None
                if download is not None and download.is_batch:
                    continue
                if torrent_hash:
                    if not self.downloads_enabled():
                        continue
                    if self.db.episode_count_for_torrent(torrent_hash) != 1:
                        continue
                    if client is None:
                        client = self.qbt_client()
                    client.delete(torrent_hash, delete_files=True)
                    self.db.delete_torrent_records(torrent_hash)
                else:
                    item.video_path.unlink(missing_ok=True)
                    if item.subtitle_path:
                        item.subtitle_path.unlink(missing_ok=True)
                    self.db.delete_episode_record(item.video_path)
                deleted += 1
                used_bytes = max(0, used_bytes - item_size)
                self.logger.info(
                    "DONE step=storage.limit_delete video=%r torrent_hash=%s watched_at=%s",
                    str(item.video_path), torrent_hash, item.watched_at,
                )
        except (OSError, QBittorrentError) as exc:
            self.log(f"Лимит диска: не удалось удалить старый просмотренный файл: {exc}")
        finally:
            if client is not None:
                client.close()

        remaining = self.storage_status()
        if remaining["over_limit"] and used_bytes > limit_bytes:
            self.logger.warning(
                "EVENT storage.limit_unresolved used_bytes=%s limit_bytes=%s reason=no_safe_watched_candidates",
                remaining["used_bytes"], remaining["limit_bytes"],
            )
        return deleted

    def auto_upgrade_downloaded(self, *, force: bool = False, limit: int | None = None) -> int:
        """Check managed, unwatched episodes only after missing episodes were handled.

        An upgrade is downloaded alongside the current file. The current torrent is
        removed only after the replacement finishes and has usable Japanese subtitles.
        """
        if not (self.config.nyaa.enabled and self.downloads_enabled()):
            return 0
        if not force and not self.config.nyaa.auto_upgrade_downloaded:
            return 0

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
                if item.episode is not None
                and item.torrent_hash
                and item.state != "watched"
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
                except NyaaError as exc:
                    self.log(f"{anime.title} #{episode}: upgrade check failed: {exc}")
                    self.db.set_state(state_key, str(time.time()))
                    continue
                self.db.set_state(state_key, str(time.time()))

                current_hash = current.torrent_hash.lower()
                current_score = self.db.release_score(
                    anime.media_id, episode, current_hash
                )
                if current_score is None:
                    same = next(
                        (
                            item for item in releases
                            if item.info_hash and item.info_hash.lower() == current_hash
                        ),
                        None,
                    )
                    current_score = same.score if same is not None else None
                if current_score is None:
                    self.logger.info(
                        "SKIP step=nyaa.upgrade media_id=%s episode=%s reason=current_score_unknown hash=%s",
                        anime.media_id, episode, current_hash,
                    )
                    continue

                best = next(
                    (
                        item for item in releases
                        if self._release_is_allowed_for_auto(item)
                        and item.info_hash
                        and item.info_hash.lower() != current_hash
                    ),
                    None,
                )
                if best is None:
                    continue
                gain = best.score - current_score
                if gain < self.config.nyaa.upgrade_min_score_gain:
                    self.logger.info(
                        "SKIP step=nyaa.upgrade media_id=%s episode=%s reason=gain_too_small current_score=%.1f best_score=%.1f gain=%.1f required=%.1f",
                        anime.media_id, episode, current_score, best.score, gain,
                        self.config.nyaa.upgrade_min_score_gain,
                    )
                    continue
                try:
                    self.add_release(
                        anime.media_id, best, episode=episode, batch=False
                    )
                except (QBittorrentError, ManagerError) as exc:
                    self.log(f"{anime.title} #{episode}: upgrade add failed: {exc}")
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
                    "SCHEDULE step=nyaa.upgrade media_id=%s episode=%s old_score=%.1f new_score=%.1f gain=%.1f old_hash=%s new_hash=%s",
                    anime.media_id, episode, current_score, best.score, gain,
                    current_hash, best.info_hash.lower(),
                )
                self.log(
                    f"{anime.title} #{episode}: scheduled quality upgrade "
                    f"({current_score:.1f} → {best.score:.1f})"
                )
                scheduled += 1
        return scheduled

    def finalize_ready_upgrades(self) -> int:
        """Replace old managed files only after the new episode is fully ready."""
        jobs = self.db.pending_upgrades()
        if not jobs or not self.downloads_enabled():
            return 0
        replaced = 0
        client = self.qbt_client()
        try:
            for job in jobs:
                new_hash = str(job["new_info_hash"] or "").lower()
                old_hash = str(job["old_torrent_hash"] or "").lower()
                media_id = int(job["media_id"])
                episode = int(job["episode"])
                new_episode = self.db.episode_for_torrent(
                    media_id, episode, new_hash
                )
                if new_episode is None or new_episode.state not in {"ready", "local", "waiting_subtitles"}:
                    continue
                old_episode = self.db.episode_for_torrent(
                    media_id, episode, old_hash
                )
                try:
                    client.delete(old_hash, delete_files=True)
                except QBittorrentError as exc:
                    self.log(
                        f"Upgrade #{episode}: replacement ready, but old torrent could not be removed: {exc}"
                    )
                    continue

                if old_episode is not None:
                    if old_episode.subtitle_path and old_episode.subtitle_path != new_episode.subtitle_path:
                        old_episode.subtitle_path.unlink(missing_ok=True)
                    if old_episode.video_path != new_episode.video_path:
                        old_episode.video_path.unlink(missing_ok=True)
                self.db.delete_torrent_records(old_hash)
                self.db.complete_upgrade(new_hash)
                self.logger.info(
                    "DONE step=nyaa.upgrade_replace media_id=%s episode=%s old_hash=%s new_hash=%s old_score=%.1f new_score=%.1f",
                    media_id, episode, old_hash, new_hash,
                    float(job["old_score"]), float(job["new_score"]),
                )
                replaced += 1
        finally:
            client.close()
        return replaced

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
            if (
                item.media_id is None
                or item.episode is None
                or not item.torrent_hash
            ):
                continue
            groups.setdefault((item.media_id, item.episode), []).append(item)

        pending_by_key = {
            (int(job["media_id"]), int(job["episode"])): str(
                job["new_info_hash"] or ""
            ).lower()
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
                    (
                        item
                        for item in items
                        if item.torrent_hash.lower() == pending_hash
                    ),
                    None,
                )

            if winner is None:
                scored: list[tuple[float, LibraryEpisode]] = []
                for item in items:
                    score = self.db.release_score(
                        key[0], key[1], item.torrent_hash
                    )
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

            losers = [
                item
                for item in items
                if item.torrent_hash.lower() != winner.torrent_hash.lower()
            ]
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
        client = self.qbt_client()
        try:
            for winner, losers in plans:
                for loser in losers:
                    old_hash = loser.torrent_hash.lower()
                    try:
                        client.delete(old_hash, delete_files=True)
                    except QBittorrentError as exc:
                        self.log(
                            f"Duplicate #{winner.episode}: old torrent could not be removed: {exc}"
                        )
                        continue

                    if (
                        loser.subtitle_path
                        and loser.subtitle_path != winner.subtitle_path
                    ):
                        loser.subtitle_path.unlink(missing_ok=True)
                    if loser.video_path != winner.video_path:
                        loser.video_path.unlink(missing_ok=True)
                    self.db.delete_torrent_records(old_hash)
                    self.logger.info(
                        "DONE step=nyaa.duplicate_replace media_id=%s episode=%s old_hash=%s new_hash=%s",
                        winner.media_id,
                        winner.episode,
                        old_hash,
                        winner.torrent_hash.lower(),
                    )
                    removed += 1
                self.db.complete_upgrade(winner.torrent_hash)
        finally:
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
        removed = 0
        for episode in self.db.episodes():
            if episode.video_path.is_file():
                continue
            torrent_hash = episode.torrent_hash.strip().casefold()
            if torrent_hash and torrent_hash in normalized_hashes:
                continue
            self.db.delete_episode_record(episode.video_path)
            self.logger.info(
                "REPAIR step=library.missing_video media_id=%s episode=%s video=%r torrent_hash=%s",
                episode.media_id, episode.episode, str(episode.video_path), torrent_hash,
            )
            removed += 1
        return removed

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
                    item.torrent_hash, str(old_save), str(target), str(exc),
                )
                continue
            repaired += 1
            self.logger.info(
                "REPAIR step=qbittorrent.legacy_path torrent=%s old=%r new=%r recheck=%s",
                item.torrent_hash, str(old_save), str(target), callable(recheck),
            )
        return repaired

    def sync_downloads(self) -> int:
        self._last_missing_episode_rows = 0
        completed_paths: list[Path] = []
        if not self.downloads_enabled():
            self._last_completed_video_paths = ()
            return 0
        items: list[DownloadItem] = []
        backend_errors: list[str] = []
        for backend, client in self.torrent_clients():
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
                            legacy_brand_tags = {
                                tag for tag in raw_tags if tag.casefold() in legacy_names
                            }
                            has_current_brand = any(
                                tag.casefold() == APP_SLUG.casefold() for tag in raw_tags
                            )
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
                    discarded_recovery = self._discard_completed_aria2_recovery_tasks(
                        client, backend_items
                    )
                    if discarded_recovery:
                        backend_items = client.torrents(
                            category=self.config.qbittorrent.category
                        )
                    recovered_aria2 = self._recover_stalled_aria2_downloads(
                        client, backend_items
                    )
                    if recovered_aria2:
                        # Recovery registers a fresh hash-check task. Refresh now
                        # so DB/UI and the subsequent auto-search see that task
                        # instead of the stale error row and never add a duplicate.
                        backend_items = client.torrents(
                            category=self.config.qbittorrent.category
                        )
                for item in backend_items:
                    item.raw["backend"] = backend
                items.extend(backend_items)
            except Exception as exc:
                backend_errors.append(f"{backend}: {exc}")
                self.logger.warning(
                    "FAIL step=torrent.sync_backend backend=%s error=%r", backend, str(exc)
                )
            finally:
                client.close()
        if backend_errors and not items:
            raise ManagerError("; ".join(backend_errors))

        # A torrent may exist in both clients. Keep a single database/UI row,
        # preferring the copy that has made more progress (then the active one).
        deduplicated: dict[str, DownloadItem] = {}
        for item in items:
            key = item.torrent_hash.casefold()
            previous = deduplicated.get(key)
            item.raw["_backends"] = [str(item.raw.get("backend") or "")]
            rank = (float(item.progress), int(item.raw.get("download_speed") or item.raw.get("dlspeed") or 0))
            previous_rank = (-1.0, -1) if previous is None else (
                float(previous.progress),
                int(previous.raw.get("download_speed") or previous.raw.get("dlspeed") or 0),
            )
            if previous is None or rank > previous_rank:
                if previous is not None:
                    item.raw["_backends"] = list(dict.fromkeys([
                        *previous.raw.get("_backends", []),
                        *item.raw.get("_backends", []),
                    ]))
                deduplicated[key] = item
            elif previous is not None:
                previous.raw["_backends"] = list(dict.fromkeys([
                    *previous.raw.get("_backends", []),
                    *item.raw.get("_backends", []),
                ]))
        items = list(deduplicated.values())
        completed = 0
        active_hashes = {item.torrent_hash for item in items if item.torrent_hash}
        self._last_missing_episode_rows = self._remove_missing_episode_rows(active_hashes)
        self.db.prune_downloads(active_hashes)
        for item in items:
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
            completed += self._register_completed_download(
                item,
                completed_paths=completed_paths,
            )
        self._last_completed_video_paths = tuple(dict.fromkeys(completed_paths))
        self.db.set_state("downloads_synced_at", str(time.time()))
        return completed

    def _discard_completed_aria2_recovery_tasks(
        self, client: Any, items: list[DownloadItem]
    ) -> int:
        """Drop metadata-only recovery tasks after the exact torrent is already local."""

        delete = getattr(client, "delete", None)
        if not callable(delete):
            return 0
        local_hashes = {
            str(episode.torrent_hash or "").strip().casefold()
            for episode in self.db.episodes()
            if str(episode.torrent_hash or "").strip()
            and episode.video_path.is_file()
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
            if (
                state not in {"active", "waiting", "paused"}
                or total > 0
                or downloaded > 0
            ):
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
            self.db.set_state(f"aria2_stall:{torrent_hash}", "")
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
            recoverable_missing_control = bool(
                state == "error" and raw.get("recoverable_missing_control")
            )
            stalled_candidate = (
                state in {"active", "waiting"}
                and not bool(raw.get("verifying"))
                and (
                    (total > 0 and 0 <= downloaded < total)
                    or (total == 0 and downloaded == 0)
                )
                and speed == 0
                and connections == 0
            )
            if not stalled_candidate and not recoverable_missing_control:
                if self.db.get_state(key, ""):
                    self.db.set_state(key, "")
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
                                "REPAIR step=aria2.missing_control hash=%s "
                                "mode=hash_check total=%s",
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
                            "RETRY step=aria2.missing_control hash=%s "
                            "delay_s=120 error=%r",
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

    def _register_completed_download(
        self,
        item,
        *,
        completed_paths: list[Path] | None = None,
    ) -> int:
        content = Path(item.content_path).expanduser()
        if content.is_file():
            files = [content]
        elif content.is_dir():
            files = [path for path in content.rglob("*") if path.suffix.casefold() in VIDEO_EXTENSIONS]
        else:
            save = Path(item.save_path).expanduser()
            files = [path for path in save.rglob("*") if path.suffix.casefold() in VIDEO_EXTENSIONS]
        count = 0
        new_paths: list[Path] = []
        anime = self.db.get_anime(item.media_id) if item.media_id else None
        for path in files:
            resolved = path.resolve()
            existing = self.db.episode_by_path(resolved)
            if existing is not None and existing.state in {"ready", "watched"}:
                continue
            is_new_completion = bool(
                existing is None
                or not existing.torrent_hash
                or existing.torrent_hash.casefold() != item.torrent_hash.casefold()
            )
            identity = parse_anime_filename(path)
            release_number = identity.episode or item.release_episode
            media_number = self._media_episode_from_release(
                anime,
                release_number,
                requested_media_episode=(item.media_episode if not item.is_batch else None),
            )
            subtitle_source, subtitle_path = japanese_subtitle_source(
                path,
                ffprobe=self.config.tools.ffprobe,
                ffmpeg=self.config.tools.ffmpeg,
            )
            embedded_subtitle_id = None
            if subtitle_source in {"embedded", "embedded_bitmap"}:
                _source, _path, embedded_subtitle_id = japanese_subtitle_details(
                    path,
                    ffprobe=self.config.tools.ffprobe,
                    ffmpeg=self.config.tools.ffmpeg,
                )
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
                    "bitmap" if subtitle_source in {"external_bitmap", "embedded_bitmap"}
                    else subtitle_source if subtitle_source in {"external", "embedded"}
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
                library_episode,
                downloaded_at=float(item.completed_on or time.time()),
            )
            if subtitle_source in {"none", "external_bitmap", "embedded_bitmap"}:
                if is_new_completion:
                    self.db.queue_subtitle_job(
                        resolved,
                        item.media_id,
                        media_number,
                        priority=100,
                        error="New completed download",
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
        if completed_paths is not None:
            completed_paths.extend(new_paths)
        return count

    @property
    def last_completed_video_paths(self) -> tuple[Path, ...]:
        return self._last_completed_video_paths

    @staticmethod
    def _subtitle_upgrade_state_key(video: Path) -> str:
        return "subtitle_upgrade:" + hashlib.sha1(
            str(video.resolve()).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _subtitle_upgrade_checked_key(video: Path) -> str:
        return "subtitle_upgrade_checked:" + hashlib.sha1(
            str(video.resolve()).encode("utf-8")
        ).hexdigest()

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
                for generation in ("v10", "v11", "v12"):
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
                    str(video), str(exc),
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
                    str(video), source,
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
                error="OCR disabled; waiting for Japanese text subtitles",
            )
            self.logger.info(
                "REQUEUE step=subtitle.ocr_disabled video=%r media_id=%s episode=%s source=%s",
                str(video), item.media_id, item.episode, source,
            )
            changed.append(video)
        return changed

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
        cooldown = max(0.0, float(self.config.matching.subtitle_upgrade_check_hours)) * 3600.0
        scheduled = 0
        episodes = sorted(
            self.db.episodes(),
            key=lambda item: (item.playback_updated_at or 0.0, str(item.video_path)),
        )
        for item in episodes:
            if checks_left <= 0:
                break
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
            checked_key = self._subtitle_upgrade_checked_key(item.video_path)
            try:
                last_checked = float(self.db.get_state(checked_key, "0") or 0)
            except ValueError:
                last_checked = 0.0
            if not force and cooldown and now - last_checked < cooldown:
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
            }
            self.db.set_state(upgrade_key, json.dumps(request, ensure_ascii=False))
            self.db.set_state(checked_key, str(now))
            self.db.queue_subtitle_job(
                item.video_path,
                item.media_id,
                item.episode,
                priority=180 if force else 120,
                error="Checking subtitle upgrade",
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

    def force_fresh_subtitle_selection(self, video: Path) -> dict[str, Any]:
        video = video.expanduser().resolve()
        item = self.db.episode_by_path(video)
        if item is None:
            raise ManagerError("Episode is not present in the local library")
        subtitle = item.subtitle_path
        if subtitle is not None:
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
        self.db.clear_subtitle_selection(video)
        self.db.set_state(self._debug_force_subtitle_state_key(video), "1")
        self.db.queue_subtitle_job(video, item.media_id, item.episode, priority=260, error="Debug fresh subtitle selection")
        self.logger.info("REQUEUE step=subtitle.debug_fresh media_id=%s episode=%s video=%r force_search=true resync=true", item.media_id, item.episode, str(video))
        return {"ok": True, "video_path": str(video), "media_id": item.media_id, "episode": item.episode}

    def set_manual_subtitle(self, video: Path, subtitle: Path) -> Path:
        video = video.expanduser().resolve()
        subtitle = subtitle.expanduser().resolve()
        if not video.is_file():
            raise ManagerError(f"Video file does not exist: {video}")
        allowed = TEXT_SUBTITLE_EXTENSIONS | IMAGE_SUBTITLE_EXTENSIONS | {".pgs"}
        if subtitle.suffix.casefold() not in allowed:
            raise ManagerError(f"Unsupported subtitle format: {subtitle.suffix}")
        if not subtitle.is_file():
            raise ManagerError(f"Subtitle file does not exist: {subtitle}")
        digest = hashlib.sha1(f"{video}:{subtitle}:{subtitle.stat().st_mtime_ns}".encode("utf-8")).hexdigest()[:16]
        target_dir = self.config.paths.cache_dir / "manual-subtitles" / digest
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{video.stem}.manual.ja{subtitle.suffix.casefold()}"
        target.write_bytes(subtitle.read_bytes())
        self.db.set_state(self._manual_subtitle_state_key(video), str(target))
        episode = self.db.episode_by_path(video)
        self.db.clear_subtitle_selection(video)
        self.db.queue_subtitle_job(
            video,
            episode.media_id if episode else None,
            episode.episode if episode else None,
            priority=250,
            error="Manual subtitle selected",
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
            reason="User selected subtitle file",
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
                requeued = self.db.force_requeue_unresolved_subtitle_jobs()
            self.logger.info(
                "EVENT subtitle_inbox.changed files=%s requeued=%s initial=%s",
                len(rows), requeued, not bool(previous),
            )
        return {"changed": changed, "files": len(rows), "requeued": requeued, "signature": signature}

    def diagnose_episode(self, media_id: int, episode: int | None = None) -> dict[str, Any]:
        anime = self.db.get_anime(int(media_id))
        if anime is None:
            raise ManagerError(f"AniList id={media_id} is not in the local database")
        local_items = self.db.episodes(int(media_id))
        selected = None
        if episode is not None:
            selected = next((item for item in local_items if item.episode == int(episode)), None)
        if selected is None and local_items:
            selected = next((item for item in local_items if item.state not in {"watched", "ready"}), local_items[0])
        downloads = [
            item for item in self.db.downloads()
            if item.media_id == int(media_id) and (episode is None or item.episode in {None, int(episode)} or item.is_batch)
        ]
        jobs = [
            row for row in self.db.subtitle_jobs()
            if (selected is not None and str(row["video_path"]) == str(selected.video_path))
            or (row["media_id"] is not None and int(row["media_id"]) == int(media_id)
                and (episode is None or row["episode"] is None or int(row["episode"]) == int(episode)))
        ]
        ru = self.config.ui.language == "ru"
        labels = {
            "anilist": "AniList сопоставлен" if ru else "AniList matched",
            "download": "Видео скачано" if ru else "Video downloaded",
            "file": "Видеофайл существует" if ru else "Video file exists",
            "subtitle": "Японские субтитры подготовлены" if ru else "Japanese subtitles prepared",
            "job": "Задание подготовки" if ru else "Preparation job",
            "none": "Субтитры не найдены" if ru else "No subtitles found",
            "image": "Найдены только субтитры-картинки; ожидается текст или OCR" if ru else "Only image subtitles found; waiting for text/OCR",
            "no_video": "Нет локального файла или активного торрента" if ru else "No local file or active torrent",
            "no_row": "Нет записи о серии" if ru else "No episode row",
            "missing_job": "Задание потеряно" if ru else "Missing job",
            "completed": "Завершено" if ru else "Completed",
        }
        checks: list[dict[str, Any]] = []
        checks.append({"key": "anilist", "ok": True, "label": labels["anilist"], "detail": f"id={anime.media_id}"})
        checks.append({
            "key": "download",
            "ok": bool(selected or downloads),
            "label": labels["download"],
            "detail": (str(selected.video_path) if selected else (downloads[0].state if downloads else labels["no_video"])),
        })
        video_exists = bool(selected and selected.video_path.is_file())
        checks.append({"key": "file", "ok": video_exists, "label": labels["file"], "detail": str(selected.video_path) if selected else labels["no_row"]})
        subtitle_ready = bool(selected and selected.state in {"ready", "watched"})
        subtitle_detail = labels["none"]
        if selected is not None:
            if selected.subtitle_path:
                subtitle_detail = str(selected.subtitle_path)
            elif selected.embedded_subtitle_id is not None:
                subtitle_detail = f"Embedded subtitle sid={selected.embedded_subtitle_id}"
            elif selected.state == "waiting_text_subtitles":
                subtitle_detail = labels["image"]
        checks.append({"key": "subtitle", "ok": subtitle_ready, "label": labels["subtitle"], "detail": subtitle_detail})
        last_error = (
            _localize_preparation_detail(
                str(jobs[0]["last_error"] or ""),
                language=self.config.ui.language,
            )
            if jobs
            else ""
        )
        checks.append({
            "key": "job",
            "ok": subtitle_ready or bool(jobs),
            "label": labels["job"],
            "detail": (f"{jobs[0]['state']}; attempts={jobs[0]['attempts']}; {last_error}" if jobs else (labels["completed"] if subtitle_ready else labels["missing_job"])),
        })
        return {
            "media_id": anime.media_id, "title": anime.title, "episode": episode,
            "ready": subtitle_ready,
            "video_path": str(selected.video_path) if selected else "",
            "state": selected.state if selected else "missing",
            "checks": checks,
            "downloads": [
                {"name": item.name, "state": item.state, "progress": item.progress, "torrent_hash": item.torrent_hash}
                for item in downloads[:5]
            ],
            "job": ({
                "state": str(jobs[0]["state"]), "attempts": int(jobs[0]["attempts"] or 0),
                "next_check": float(jobs[0]["next_check"] or 0), "error": last_error,
            } if jobs else None),
        }

    def repair_library_integrity(self, *, automatic: bool = False, scan: bool = True) -> dict[str, int]:
        result = {
            "missing_episode_rows": 0, "stale_subtitles": 0, "spurious_jobs": 0,
            "bitmap_rows": 0, "jobs_created": 0, "library_rows": 0,
        }
        if self.downloads_enabled():
            try:
                self.sync_downloads()
            except Exception as exc:
                self.logger.warning("FAIL step=repair.sync_downloads error=%r", str(exc))
        for item in list(self.db.episodes()):
            if item.video_path.is_file():
                continue
            active = any(d.torrent_hash == item.torrent_hash for d in self.db.downloads()) if item.torrent_hash else False
            if not active:
                self.db.delete_episode_record(item.video_path)
                result["missing_episode_rows"] += 1
        result["bitmap_rows"] = self.db.repair_bitmap_ready_rows()
        result["spurious_jobs"] = self.db.repair_spurious_ready_subtitle_jobs()
        result["stale_subtitles"] = self.db.repair_stale_subtitle_selections()
        result["library_rows"] = len(self.scan_library()) if scan else len(self.db.episodes())
        for item in self.db.episodes():
            if item.state in {"local", "waiting_subtitles", "waiting_text_subtitles"}:
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

    def process_subtitle_jobs(
        self,
        limit: int = 4,
        *,
        preferred_paths: tuple[Path, ...] | list[Path] | None = None,
    ) -> int:
        apply_jimaku_trial(self.config)
        if foreground_active(self.config.paths.cache_dir):
            self.logger.info(
                "SKIP step=subtitle.process reason=foreground_active limit=%s", limit
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
            migration_marker = "Повторная подготовка после обновления синхронизации субтитров"
            has_regular_jobs = any(
                migration_marker not in str(row["last_error"] or "") for row in jobs
            )
            migration_budget = 0 if has_regular_jobs else 1
            runnable_jobs = []
            for queued in jobs:
                is_migration = migration_marker in str(queued["last_error"] or "")
                if is_migration and migration_budget <= 0:
                    delay = max(5 * 60, self.config.agent.subtitle_poll_minutes * 60)
                    self.db.defer_subtitle_job(
                        Path(str(queued["video_path"])), migration_marker, delay
                    )
                    self.logger.info(
                        "RETRY step=subtitle.prepare reason=energy_throttle media_id=%s episode=%s video=%s delay_s=%s",
                        queued["media_id"], queued["episode"], Path(str(queued["video_path"])).name, delay,
                    )
                    continue
                if is_migration:
                    migration_budget -= 1
                runnable_jobs.append(queued)
            jobs = runnable_jobs

        def requeue_unstarted(start_index: int) -> None:
            for queued in jobs[start_index:]:
                queued_video = Path(str(queued["video_path"]))
                self.db.postpone_subtitle_job(
                    queued_video,
                    "Foreground playback requested",
                    60,
                )

        for job_index, job in enumerate(jobs):
            video = Path(str(job["video_path"]))
            if foreground_active(self.config.paths.cache_dir):
                self.db.postpone_subtitle_job(video, "Foreground playback requested", 60)
                self.logger.info(
                    "RETRY step=subtitle.prepare reason=foreground_active media_id=%s episode=%s video=%s delay_s=60",
                    job["media_id"], job["episode"], video.name,
                )
                requeue_unstarted(job_index + 1)
                break
            if not video.is_file():
                self.logger.info(
                    "RETRY step=subtitle.prepare reason=video_missing media_id=%s episode=%s video=%s delay_s=%s",
                    job["media_id"], job["episode"], video.name, 15 * 60,
                )
                self.db.postpone_subtitle_job(video, "Видео ещё недоступно", 15 * 60)
                continue
            incomplete_roots = self.incomplete_download_paths()
            if any(
                video.resolve() == root or root in video.resolve().parents
                for root in incomplete_roots
            ):
                self.db.postpone_subtitle_job(
                    video, "Torrent is still writing this video", 15 * 60
                )
                self.logger.info(
                    "RETRY step=subtitle.prepare reason=download_incomplete media_id=%s episode=%s video=%s delay_s=%s",
                    job["media_id"], job["episode"], video.name, 15 * 60,
                )
                continue
            probe = None
            if video.stat().st_size >= 1024 * 1024:
                try:
                    probe = subprocess.run(
                        [
                            self.config.tools.ffprobe,
                            "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", str(video),
                        ],
                        check=False, capture_output=True, text=True, timeout=15,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    probe = None
            if probe is not None and probe.returncode != 0:
                self.db.postpone_subtitle_job(
                    video, "Video container is not readable yet", 60 * 60
                )
                self.logger.info(
                    "RETRY step=subtitle.prepare reason=video_container_unreadable media_id=%s episode=%s video=%s delay_s=%s error=%r",
                    job["media_id"], job["episode"], video.name, 60 * 60,
                    (probe.stderr or "")[-500:],
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
            previous_backup: Path | None = None
            if upgrade_request is not None:
                command.append("--force-search")
                previous_value = str(upgrade_request.get("previous_subtitle_path") or "").strip()
                previous_path = Path(previous_value) if previous_value else None
                if previous_path is not None and previous_path.is_file():
                    backup_dir = self.config.paths.cache_dir / "subtitle-upgrade-backups"
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    previous_backup = backup_dir / (
                        hashlib.sha1(str(video).encode("utf-8")).hexdigest()
                        + previous_path.suffix.casefold()
                    )
                    shutil.copy2(previous_path, previous_backup)
            media_id = int(job["media_id"]) if job["media_id"] is not None else None

            def restore_upgrade_selection(reason: str) -> bool:
                if upgrade_request is None:
                    return False
                previous_value = str(
                    upgrade_request.get("previous_subtitle_path") or ""
                ).strip()
                previous_path = Path(previous_value) if previous_value else None
                previous_sid_raw = upgrade_request.get("previous_embedded_sid")
                previous_sid = int(previous_sid_raw) if previous_sid_raw is not None else None
                if (
                    previous_backup is not None
                    and previous_path is not None
                    and previous_backup.is_file()
                ):
                    previous_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(previous_backup, previous_path)
                self.db.set_subtitle_ready(
                    video, previous_path, previous_sid,
                    origin=str(upgrade_request.get("previous_origin") or ""),
                )
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
                self.db.delete_state(upgrade_key)
                if previous_backup is not None:
                    previous_backup.unlink(missing_ok=True)
                return True

            anime = self.db.get_anime(media_id) if media_id is not None else None
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
                # The manager already has an authoritative AniList ID. Avoid a
                # second GraphQL lookup here; Jimaku can search by ID directly.
                command.append("--skip-airing-lookup")
            if job["episode"] is not None:
                command.extend(["--episode-hint", str(int(job["episode"]))])
            manual_key = self._manual_subtitle_state_key(video)
            manual_value = self.db.get_state(manual_key, "").strip()
            manual_path = Path(manual_value) if manual_value else None
            if manual_path is not None and manual_path.is_file():
                command.extend(["--sub", str(manual_path)])
            debug_force_key = self._debug_force_subtitle_state_key(video)
            debug_force = self.db.get_state(debug_force_key, "").strip() == "1"
            if debug_force:
                if "--force-search" not in command:
                    command.append("--force-search")
                command.append("--resync")
            command.append(str(video))
            env = dict(os.environ)
            env["PUDGE_CONFIG"] = str(self.config.config_path)
            # Distinguish launch-agent preparation from a user invoking
            # ``pudge --prepare-only`` manually. Manual preparation must
            # preempt this background worker just like normal playback.
            env["PUDGE_BACKGROUND_PREPARE"] = "1"
            status_path = self.config.paths.cache_dir / "subtitle-job-status" / (
                hashlib.sha1(str(video).encode("utf-8")).hexdigest() + ".json"
            )
            status_path.unlink(missing_ok=True)
            env["PUDGE_SUBTITLE_JOB_STATUS"] = str(status_path)
            debug_paths = subtitle_debug_paths(self.config.paths.cache_dir, video)
            debug_paths["trace"].unlink(missing_ok=True)
            debug_paths["result"].unlink(missing_ok=True)
            env["PUDGE_SUBTITLE_JOB_TRACE"] = str(debug_paths["trace"])
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
            try:
                with timed_step(
                    self.logger,
                    "subtitle.prepare",
                    media_id=job["media_id"],
                    episode=job["episode"],
                    video=video.name,
                ):
                    process = subprocess.Popen(
                        command,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=env,
                    )
                    deadline = time.monotonic() + 20 * 60
                    cancelled_for_foreground = False
                    last_report_stage = ""
                    while process.poll() is None:
                        report = read_job_report(status_path)
                        if report is not None:
                            report_stage = str(report.get("stage") or "")
                            if report_stage and report_stage != last_report_stage:
                                details = report.get("details")
                                self.db.update_subtitle_job_stage(
                                    video,
                                    report_stage,
                                    progress=details if isinstance(details, dict) else {},
                                )
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
                        if foreground_active(self.config.paths.cache_dir):
                            cancelled_for_foreground = True
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                            break
                        if time.monotonic() >= deadline:
                            process.kill()
                            raise subprocess.TimeoutExpired(command, 20 * 60)
                        time.sleep(0.25)
                    stdout, stderr = process.communicate()
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
                            )
                    if cancelled_for_foreground:
                        if not restore_upgrade_selection("Foreground playback requested"):
                            self.db.clear_subtitle_selection(video)
                            self.db.postpone_subtitle_job(
                                video,
                                "Foreground playback requested",
                                60,
                            )
                        self.logger.info(
                            "RETRY step=subtitle.prepare reason=foreground_preempted media_id=%s episode=%s video=%s delay_s=60",
                            job["media_id"], job["episode"], video.name,
                        )
                        requeue_unstarted(job_index + 1)
                        break
            except (OSError, subprocess.TimeoutExpired) as exc:
                if not restore_upgrade_selection(str(exc)):
                    self.db.clear_subtitle_selection(video)
                    self.db.postpone_subtitle_job(
                        video,
                        str(exc),
                        self.config.agent.subtitle_poll_minutes * 60,
                    )
                continue
            finally:
                status_path.unlink(missing_ok=True)
            subtitle: Path | None = None
            embedded_subtitle_id: int | None = None
            prepare_status = ""
            subtitle_meta: dict[str, Any] = {}
            for line in completed.stdout.splitlines():
                if line.startswith("PREPARED_SUBTITLE="):
                    value = line.split("=", 1)[1].strip()
                    subtitle = Path(value) if value else None
                elif line.startswith("PREPARED_EMBEDDED_SID="):
                    value = line.split("=", 1)[1].strip()
                    embedded_subtitle_id = int(value) if value else None
                elif line.startswith("PREPARE_STATUS="):
                    prepare_status = line.split("=", 1)[1].strip()
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
            stdout_tail = completed.stdout.strip()[-3000:]
            stderr_tail = completed.stderr.strip()[-3000:]
            self.db.delete_state(self._manual_subtitle_state_key(video))
            if debug_force:
                self.db.delete_state(debug_force_key)
            self.logger.info(
                "RESULT step=subtitle.prepare media_id=%s episode=%s video=%s returncode=%s prepared=%s stdout_tail=%r stderr_tail=%r",
                job["media_id"], job["episode"], video.name, completed.returncode,
                str(subtitle) if subtitle else "", stdout_tail, stderr_tail,
            )
            image_subtitle = bool(
                subtitle is not None
                and subtitle.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS
            )
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
                accepted_upgrade = bool(
                    completed.returncode == 0
                    and prepare_status == "ready"
                    and not image_subtitle
                    and (subtitle is not None or embedded_subtitle_id is not None)
                    and new_score is not None
                    and quality_better
                )
                if accepted_upgrade:
                    self.db.set_subtitle_ready(
                        video, subtitle, embedded_subtitle_id,
                        origin=(
                            "ocr" if bool(subtitle_meta.get("generated_by_ocr"))
                            else str(subtitle_meta.get("source") or ("embedded" if embedded_subtitle_id is not None else "external"))
                        ),
                    )
                    self.db.record_subtitle_history(
                        video_path=video,
                        media_id=media_id,
                        episode=int(job["episode"]) if job["episode"] is not None else None,
                        source=str(subtitle_meta.get("source") or "unknown"),
                        candidate_name=str(subtitle_meta.get("name") or (subtitle.name if subtitle else "embedded")),
                        candidate_path=str(subtitle_meta.get("candidate_path") or subtitle or ""),
                        score=new_score,
                        status="upgraded",
                        reason=(
                            quality_reason
                        ),
                        details=subtitle_meta,
                    )
                    self.log(
                        f"Subtitle upgrade ready: {video.name} "
                        + (f"({old_score:.1f} → {new_score:.1f})" if old_score is not None else f"(score={new_score:.1f})")
                    )
                    ready += 1
                else:
                    if previous_backup is not None and previous_path is not None and previous_backup.is_file():
                        previous_path.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(previous_backup, previous_path)
                    self.db.set_subtitle_ready(
                        video, previous_path, previous_sid,
                        origin=str(upgrade_request.get("previous_origin") or ""),
                    )
                    reason = quality_reason or "No better subtitle candidate"
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
                self.db.delete_state(upgrade_key)
                continue
            if prepare_status == "waiting_text_subtitles" or image_subtitle:
                # Defence in depth: an old/malformed CLI result must never make
                # a PGS/SUP path ready merely because the subprocess exited 0.
                attempts = int(job["attempts"] or 0) + 1
                delay = self.config.agent.subtitle_poll_minutes * 60
                if not self.config.matching.ocr_image_subtitles:
                    self.db.mark_subtitle_job_needs_action(
                        video,
                        "Only image subtitles are available; OCR is disabled",
                        "enable_subtitle_ocr",
                    )
                else:
                    self.db.postpone_subtitle_job(
                        video, "Waiting for Japanese text subtitles", delay
                    )
                # postpone_subtitle_job uses the generic waiting state; restore
                # the more precise bitmap-fallback state afterwards.
                self.db.set_waiting_text_subtitles(
                    video, subtitle, embedded_subtitle_id
                )
                self.logger.info(
                    "RETRY step=subtitle.prepare media_id=%s episode=%s video=%s attempts=%s delay_s=%s reason=waiting_text_subtitles",
                    job["media_id"], job["episode"], video.name, attempts, delay,
                )
            elif completed.returncode == 0:
                previous = self.db.episode_by_path(video)
                was_ready = bool(previous is not None and previous.state in {"ready", "watched"})
                self.db.set_subtitle_ready(
                    video, subtitle, embedded_subtitle_id,
                    origin=(
                        "ocr" if bool(subtitle_meta.get("generated_by_ocr"))
                        else str(subtitle_meta.get("source") or ("embedded" if embedded_subtitle_id is not None else "external"))
                    ),
                )
                self.db.record_subtitle_history(
                    video_path=video,
                    media_id=media_id,
                    episode=int(job["episode"]) if job["episode"] is not None else None,
                    source=str(subtitle_meta.get("source") or ("embedded" if embedded_subtitle_id is not None else "unknown")),
                    candidate_name=str(subtitle_meta.get("name") or (subtitle.name if subtitle else "embedded")),
                    candidate_path=str(subtitle_meta.get("candidate_path") or subtitle or ""),
                    score=(float(subtitle_meta["score"]) if subtitle_meta.get("score") is not None else None),
                    status="selected",
                    reason="Preparation completed",
                    details=subtitle_meta,
                )
                self.log(f"Субтитры готовы: {video.name}")
                if not was_ready:
                    self._notify_ready_episode(
                        video=video,
                        media_id=media_id,
                        episode=(int(job["episode"]) if job["episode"] is not None else None),
                    )
                ready += 1
            else:
                # A failed validation must never leave an older generated cache
                # path playable. This matters after validation rules become
                # stricter: a cache file can still exist while the current run
                # explicitly rejects it.
                self.db.clear_subtitle_selection(video)
                # ffsubsync writes ordinary INFO lines to stderr. Using stderr
                # alone hid the actual deterministic validation failure printed
                # to stdout and reduced a 6h backoff to the normal 10m retry.
                stdout_detail = completed.stdout.strip()[-5000:]
                stderr_detail = completed.stderr.strip()[-1500:]
                detail = "\n".join(
                    part for part in (stdout_detail, stderr_detail) if part
                )
                rate_limited = _subtitle_retry_is_rate_limit(detail)
                attempts = int(job["attempts"] or 0) + (0 if rate_limited else 1)
                delay = _subtitle_retry_delay_seconds(
                    poll_minutes=self.config.agent.subtitle_poll_minutes,
                    attempts=attempts,
                    detail=detail,
                )
                network_backoff = _subtitle_retry_is_network_error(detail)
                self.logger.info(
                    "RETRY step=subtitle.prepare media_id=%s episode=%s video=%s attempts=%s delay_s=%s network_backoff=%s rate_limited=%s reason=%r",
                    job["media_id"], job["episode"], video.name, attempts, delay,
                    network_backoff, rate_limited, detail or "Субтитры пока не найдены",
                )
                permission_error = any(
                    marker in detail.casefold()
                    for marker in ("permission denied", "operation not permitted", "access denied")
                )
                if permission_error:
                    self.db.mark_subtitle_job_needs_action(
                        video, detail or "Folder access is required", "grant_folder_access"
                    )
                elif (
                    not self.config.jimaku.api_key.strip()
                    and attempts >= 1
                    and any(
                        marker in detail.casefold()
                        for marker in (
                            "jimaku api key",
                            "jimaku key",
                            "api-ключ jimaku",
                            "jimaku не настроен",
                        )
                    )
                ):
                    self.db.mark_subtitle_job_needs_action(
                        video, detail or "Jimaku API key is required", "configure_jimaku"
                    )
                elif rate_limited:
                    self.db.defer_subtitle_job(video, detail or "Jimaku rate limited (429)", delay)
                else:
                    self.db.postpone_subtitle_job(video, detail or "Субтитры пока не найдены", delay)
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
                (
                    path
                    for path in resolved_root.rglob("*")
                    if path.is_dir() and not path.is_symlink()
                ),
                key=lambda path: len(path.parts),
                reverse=True,
            )
        except OSError:
            return 0
        removed = 0
        for path in candidates:
            try:
                path.rmdir()
            except OSError:
                continue
            removed += 1
            self.logger.info("DONE step=cleanup.empty_folder path=%r", str(path))
        return removed

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

    def cleanup(self) -> int:
        repaired = self.db.repair_missing_cleanup_schedule(
            self.config.agent.delete_after_watched_hours
        )
        if repaired:
            self.logger.info(
                "REPAIR step=cleanup.schedule_missing rows=%s delete_after_hours=%s",
                repaired, self.config.agent.delete_after_watched_hours,
            )
        hash_repairs = self._repair_cleanup_torrent_hashes()
        if hash_repairs:
            self.logger.info(
                "REPAIR step=cleanup.torrent_hash rows=%s",
                hash_repairs,
            )
        due = self.db.due_cleanup()
        watched_due = [row for row in due if str(row["state"] or "") == "watched"]
        if (
            watched_due
            and self.config.anilist.enabled
            and self.config.anilist.access_token.strip()
        ):
            # A user may undo watched progress directly on AniList after closing
            # mpv. Verify progress immediately before destructive cleanup so the
            # scheduled agent cannot delete a file whose watched mark was revoked.
            try:
                self.sync_anilist()
            except Exception as exc:
                self.logger.warning(
                    "EVENT cleanup.anilist_verification_failed error=%r", exc
                )
                self.log(
                    "Удаление отложено: не удалось проверить актуальный прогресс AniList"
                )
                return 0
            due = self.db.due_cleanup()

        deleted = 0
        deleted_torrents: set[str] = set()
        touched_media: set[int] = set()
        downloads = {item.torrent_hash.casefold(): item for item in self.db.downloads()}
        self.logger.info("START step=cleanup.items due=%s", len(due))
        for row in due:
            video = Path(str(row["video_path"]))
            torrent_hash = str(row["torrent_hash"] or "").strip().casefold()
            downloaded_at = float(row["downloaded_at"] or 0.0) if "downloaded_at" in row.keys() else 0.0
            managed_without_hash = bool(downloaded_at)
            self.logger.info(
                "CANDIDATE step=cleanup.item media_id=%s episode=%s video=%r state=%s torrent_hash=%s downloaded_at=%s delete_after=%s exists=%s",
                row["media_id"], row["episode"], str(video), row["state"], torrent_hash,
                downloaded_at or None, row["delete_after"], video.exists(),
            )
            if (
                self.config.agent.delete_only_managed_files
                and not torrent_hash
                and not managed_without_hash
            ):
                self.logger.info(
                    "SKIP step=cleanup.item reason=unmanaged_no_hash video=%r", str(video)
                )
                continue
            if not torrent_hash and managed_without_hash:
                self.logger.info(
                    "REPAIR step=cleanup.item reason=managed_download_missing_hash video=%r downloaded_at=%s",
                    str(video), downloaded_at,
                )
            if torrent_hash and torrent_hash in deleted_torrents:
                self.logger.info(
                    "SKIP step=cleanup.item reason=torrent_already_deleted torrent_hash=%s video=%r",
                    torrent_hash, str(video),
                )
                continue
            if torrent_hash and self.config.agent.keep_batch_until_completed and str(row["state"] or "") != "dropped":
                download = downloads.get(torrent_hash)
                if download and download.is_batch:
                    anime = self.db.get_anime(int(row["media_id"])) if row["media_id"] else None
                    if anime and anime.episodes and anime.progress < anime.episodes:
                        self.logger.info(
                            "SKIP step=cleanup.item reason=batch_incomplete media_id=%s progress=%s episodes=%s torrent_hash=%s",
                            anime.media_id, anime.progress, anime.episodes, torrent_hash,
                        )
                        continue
            try:
                subtitle = row["subtitle_path"]
                if torrent_hash and self.downloads_enabled():
                    client = self.qbt_client()
                    try:
                        client.delete(torrent_hash, delete_files=True)
                    finally:
                        client.close()
                    # qBittorrent can acknowledge deletion before the file is
                    # physically removed, or retain it after a stale content-path
                    # mapping. A due, watched pudge episode is safe to remove
                    # directly after the torrent itself has been deleted.
                    if video.exists():
                        video.unlink(missing_ok=True)
                    if subtitle:
                        Path(str(subtitle)).unlink(missing_ok=True)
                else:
                    video.unlink(missing_ok=True)
                    if subtitle:
                        Path(str(subtitle)).unlink(missing_ok=True)
                if row["media_id"] is not None:
                    touched_media.add(int(row["media_id"]))
                if torrent_hash:
                    self.db.delete_torrent_records(torrent_hash)
                    deleted_torrents.add(torrent_hash)
                else:
                    self.db.delete_episode_record(video)
                self._remove_empty_episode_parent(video)
                self.logger.info(
                    "DONE step=cleanup.item media_id=%s episode=%s video=%r torrent_hash=%s",
                    row["media_id"], row["episode"], str(video), torrent_hash,
                )
                deleted += 1
            except (OSError, QBittorrentError) as exc:
                self.logger.warning(
                    "FAIL step=cleanup.item media_id=%s episode=%s video=%r torrent_hash=%s error=%r",
                    row["media_id"], row["episode"], str(video), torrent_hash, exc,
                )
                self.log(f"Не удалось удалить {video}: {exc}")
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
        downloads = [
            item for item in self.db.downloads() if self._download_is_complete(item)
        ]
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
                str(video), winner.torrent_hash, media_id, episode, ranked[0][0],
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
        try:
            response = httpx.get(anime.cover_url, timeout=20, follow_redirects=True)
            response.raise_for_status()
            path.write_bytes(response.content)
            return path
        except httpx.HTTPError:
            return path if path.is_file() and path.stat().st_size > 0 else None

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
                str(item.video_path), item.media_id, item.episode, str(mapped),
            )
        return repaired

    def _requeue_legacy_generated_subtitles(self) -> int:
        generation = "16"
        previous_generation = self.db.get_state("subtitle_validation_generation", "")
        if previous_generation == generation:
            return 0
        cache_root = self.config.paths.cache_dir.expanduser().resolve()

        # v15 changes the alignment algorithm.  Do not rebuild every cleaned
        # playback SRT: only files whose v12 playback copy can be traced back to
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
                    digest = hashlib.sha1(
                        f"{source.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:playback-srt-v12".encode()
                    ).hexdigest()[:20]
                    aligned_playback_names.add(f"v12-{digest}.srt")

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
                    direct_digest = hashlib.sha1(
                        f"{candidate.resolve()}:{candidate_stat.st_size}:{candidate_stat.st_mtime_ns}:playback-srt-v12".encode()
                    ).hexdigest()[:20]
                except OSError:
                    continue
                if subtitle.name == f"v12-{direct_digest}.srt":
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
            invalidate_final_pipeline_result(item.video_path, self.config)
            self.db.invalidate_subtitle(
                item.video_path,
                item.media_id,
                item.episode,
                "Повторная подготовка после обновления синхронизации субтитров",
            )
            queued += 1
        self.db.set_state("subtitle_validation_generation", generation)
        if queued:
            self.log(f"Субтитры: повторно проверяю ранее подготовленных файлов — {queued}")
        return queued

    def _requeue_large_cold_open_subtitles(self) -> int:
        """Rebuild only subtitles rejected by the former 2.5s cold-open limit."""

        generation = "1"
        key = "subtitle_large_cold_open_generation"
        if self.db.get_state(key, "") == generation:
            return 0
        queued = 0
        for item in self.db.episodes():
            subtitle = item.subtitle_path
            if subtitle is None:
                continue
            latest = self.db.latest_selected_subtitle(item.video_path)
            if not latest or str(latest.get("source") or "").casefold() == "manual":
                continue
            details = (
                latest.get("details")
                if isinstance(latest.get("details"), dict)
                else {}
            )
            alignment = (
                details.get("alignment")
                if isinstance(details.get("alignment"), dict)
                else {}
            )
            cold_start = (
                alignment.get("timeline_cold_start")
                if isinstance(alignment.get("timeline_cold_start"), dict)
                else {}
            )
            try:
                cold_delta = abs(float(cold_start.get("delta_seconds") or 0.0))
            except (TypeError, ValueError):
                cold_delta = 0.0
            if not (
                str(cold_start.get("reason") or "") == "edge_hint_not_local"
                and 2.5 < cold_delta <= 15.0
            ):
                continue
            invalidate_final_pipeline_result(item.video_path, self.config)
            self.db.invalidate_subtitle(
                item.video_path,
                item.media_id,
                item.episode,
                "Повторная подготовка после исправления cold-open синхронизации",
            )
            queued += 1
        self.db.set_state(key, generation)
        if queued:
            self.log(f"Субтитры: исправляю синхронизацию до опенинга — {queued}")
        return queued

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
            self.log(f"Субтитры: повторяю поиск после обновления resolver — {queued}")
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
        try:
            with timed_step(self.logger, "qbittorrent.sync"):
                stats["downloads"] = self.sync_downloads()
        except QBittorrentError as exc:
            self.log(str(exc))

    def run_startup_once(self) -> dict[str, int]:
        """Run one startup maintenance pass unless another process already owns it."""
        with maintenance_lock(self.config.paths.cache_dir, blocking=False) as acquired:
            if not acquired:
                self.logger.info(
                    "SKIP step=maintenance.total mode=startup reason=another_process_active"
                )
                return self._maintenance_stats()
            return self._run_startup_once_unlocked()

    def _run_startup_once_unlocked(self) -> dict[str, int]:
        """Run the same full maintenance pipeline as a manual Refresh.

        Startup used to skip the normal subtitle backlog whenever the background
        agent was enabled. That made opening the app much weaker than pressing
        Refresh: delayed/missing subtitle jobs could remain untouched until the
        next agent tick. A launch is now an explicit refresh point, so force the
        same subtitle retry/search work immediately.
        """
        self.logger.info("START step=maintenance.total mode=startup-fast-refresh")
        stats = self._run_once_unlocked(
            force_subtitle_retry=True,
            prioritize_release_search=True,
            defer_subtitle_processing=True,
        )
        self.logger.info("SUMMARY mode=startup-fast-refresh stats=%s", stats)
        return stats

    def run_interactive_refresh(self) -> dict[str, int]:
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
                )
                stats["subtitle_check_queued"] = self.db.priority_subtitle_job_count(
                    min_priority=200
                )
                return stats
        self.logger.info("FALLBACK step=maintenance.interactive reason=heavy_maintenance_active")
        stats = self._maintenance_stats()
        forced_jobs = self.db.force_requeue_unresolved_subtitle_jobs(
            priority=200, recover_processing=False
        )
        remaining = self.db.priority_subtitle_job_count(min_priority=200)
        stats["subtitle_check_queued"] = remaining
        self.logger.info(
            "REQUEUE step=subtitle.interactive jobs=%s remaining=%s priority=200 preserve_processing=true",
            forced_jobs, remaining,
        )
        with timed_step(self.logger, "nyaa.auto_search", priority="interactive_concurrent"):
            stats["auto"] = self.auto_search_current()
        return stats

    def _clear_jimaku_api_cache(self) -> int:
        """Remove short-lived API responses so a manual refresh is truly fresh."""
        cache_dir = self.config.paths.cache_dir / "jimaku-api"
        removed = 0
        if not cache_dir.is_dir():
            return 0
        for path in cache_dir.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

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
                self.logger.info(
                    "SKIP step=maintenance.total mode=regular reason=another_process_active"
                )
                return self._maintenance_stats()
            return self._run_once_unlocked(
                force_subtitle_retry=force_subtitle_retry,
                prioritize_release_search=bool(force_subtitle_retry and wait_for_maintenance),
            )

    def _run_once_unlocked(
        self,
        *,
        force_subtitle_retry: bool = False,
        prioritize_release_search: bool = False,
        defer_subtitle_processing: bool = False,
    ) -> dict[str, int]:
        stats = self._maintenance_stats()
        # AniList is intentionally not refreshed by the periodic/local refresh.
        # It is synchronized once when the app opens or explicitly from Settings.
        with timed_step(self.logger, "maintenance.total", mode="regular-subtitles-first"):
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
                preserved = self.db.repair_spurious_ready_subtitle_jobs()
                if preserved:
                    self.logger.info(
                        "REPAIR step=subtitle.spurious_ready_jobs removed=%s",
                        preserved,
                    )
                repaired = self.db.repair_stale_subtitle_selections()
                if repaired:
                    self.logger.info(
                        "REPAIR step=subtitle.stale_selection cleared=%s",
                        repaired,
                    )
                if not self.config.matching.ocr_image_subtitles:
                    self.invalidate_disabled_ocr_subtitles()
                self._requeue_legacy_generated_subtitles()
                self._requeue_large_cold_open_subtitles()
                self._requeue_after_resolver_upgrade()
            self._sync_downloads_for_stats(stats)
            with timed_step(self.logger, "qbittorrent.duplicate_cleanup"):
                try:
                    stats["torrent_duplicates"] = self.cleanup_duplicate_torrents()
                except QBittorrentError as exc:
                    self.log(str(exc))
            with timed_step(self.logger, "library.scan", priority="before_subtitles"):
                stats["library"] = len(self.scan_library())
            with timed_step(self.logger, "subtitle.inbox"):
                inbox = self.scan_subtitle_inbox()
                stats["inbox_requeued"] = int(inbox.get("requeued", 0) or 0)
            with timed_step(self.logger, "library.integrity_if_due"):
                integrity = self.repair_library_if_due()
                stats["integrity_repairs"] = sum(int(value or 0) for value in integrity.values()) if integrity else 0
            with timed_step(self.logger, "subtitle.upgrade_schedule"):
                stats["subtitle_upgrades"] = self.schedule_subtitle_upgrades()
            subtitle_limit = 8
            if force_subtitle_retry:
                cleared_cache = self._clear_jimaku_api_cache()
                forced_jobs = self.db.force_requeue_unresolved_subtitle_jobs(
                    priority=200 if defer_subtitle_processing else 20
                )
                subtitle_limit = max(8, min(forced_jobs, 32))
                if defer_subtitle_processing:
                    stats["subtitle_check_queued"] = self.db.priority_subtitle_job_count(
                        min_priority=200
                    )
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
                with timed_step(self.logger, "nyaa.auto_search", priority="manual_before_subtitles"):
                    stats["auto"] = self.auto_search_current()
            if defer_subtitle_processing:
                stats["subs"] = 0
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
            with timed_step(self.logger, "qbittorrent.tag_cleanup"):
                try:
                    stats.update(self.cleanup_qbittorrent_tags())
                except QBittorrentError as exc:
                    self.log(str(exc))
        self.db.set_state("agent_last_run", str(time.time()))
        self.logger.info("SUMMARY mode=regular-subtitles-first stats=%s", stats)
        return stats
