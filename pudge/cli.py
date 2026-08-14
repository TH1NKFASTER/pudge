from __future__ import annotations

from dataclasses import replace

import argparse
import hashlib
import re
import json
import os
import shutil
import subprocess
import sys
import time
from importlib.resources import files
from pathlib import Path

from .anilist_tracking import (
    TrackingPayload,
    create_tracking_file,
    load_mapping,
    mapping_key,
    parse_anilist_id,
    read_tracking_file,
    save_mapping,
    update_tracking_media,
)
from .config import DEFAULT_CONFIG_PATH, AppConfig, load_config, write_default_config
from .branding import APP_CLI, APP_NAME
from .database import Database
from .filename import fold_search_title, normalize_title, parse_anime_filename, title_similarity
from .language import IMAGE_SUBTITLE_EXTENSIONS, TEXT_SUBTITLE_EXTENSIONS, is_japanese_subtitle
from .llm import OllamaClient
from .local_search import find_local_subtitles
from .logging_utils import StageTimer, configure_logging, timed_step
from .media import MediaProbeError, TEXT_CODECS, find_embedded_japanese_subtitles
from .models import AniListAnime, JimakuEntry, SubtitleCandidate, VideoIdentity
from .ocr import OCRConversionError, OCRUnavailableError, image_subtitle_to_srt
from .foreground import clear_foreground, mark_foreground
from .pipeline_cache import (
    final_pipeline_cache_available,
    invalidate_final_pipeline_result,
    load_final_pipeline_result,
    save_final_pipeline_result,
)
from .player import build_mpv_command, run_mpv
from .providers.anilist import AniListClient, AniListError
from .providers.jimaku import JimakuClient, JimakuError, find_7zip, materialize_jimaku_files
from .subtitle_formats import clean_srt_for_playback, convert_to_plain_srt, parse_srt
from .subtitles.discovery import deduplicate_candidates
from .subtitles.jobs import SubtitleJobReporter
from .subtitles.models import SubtitleJobStage
from .subtitles.validation import quality_from_result
from .syncing import optimize_candidates, optimize_subtitle, subtitle_quality_accepted



VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".mts", ".wmv",
    ".flv", ".ogv", ".mpeg", ".mpg", ".mpv", ".3gp",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=APP_CLI,
        description="Запускает mpv и автоматически подбирает японские субтитры.",
    )
    parser.add_argument("videos", nargs="*", type=Path, help="Видео-файл или несколько файлов")
    parser.add_argument("--sub", type=Path, help="Явно заданный файл субтитров")
    parser.add_argument("--offline", action="store_true", help="Не использовать AniList и Jimaku")
    parser.add_argument("--no-sync", action="store_true", help="Не исправлять тайминг")
    parser.add_argument("--resync", action="store_true", help="Не использовать кэш ретайминга")
    parser.add_argument("--force-search", action="store_true", help="Искать внешние субтитры даже при встроенных японских")
    parser.add_argument("--dry-run", action="store_true", help="Показать итоговую команду mpv, но не запускать")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--no-anilist-progress",
        action="store_true",
        help="Не обновлять прогресс AniList для этого запуска",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("PUDGE_CONFIG", str(DEFAULT_CONFIG_PATH))),
    )
    parser.add_argument("--init-config", action="store_true", help="Создать конфиг и выйти")
    parser.add_argument("--doctor", action="store_true", help="Проверить зависимости и настройки")
    parser.add_argument("--settings", action="store_true", help="Открыть расширенные настройки")
    parser.add_argument("--app", action="store_true", help=f"Открыть приложение {APP_NAME}")
    parser.add_argument("--agent-once", action="store_true", help="Однократно запустить фоновый агент")
    parser.add_argument("--prepare-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fast-play", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--embedded-sid", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--media-id", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--media-title", default="", help=argparse.SUPPRESS)
    parser.add_argument("--media-titles-json", default="[]", help=argparse.SUPPRESS)
    parser.add_argument("--media-synonyms-json", default="[]", help=argparse.SUPPRESS)
    parser.add_argument("--media-episodes", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--media-format", default="", help=argparse.SUPPRESS)
    parser.add_argument("--episode-hint", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--skip-airing-lookup", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--anilist-correct",
        metavar="ID_OR_URL",
        help="Закрепить AniList ID за аниме из указанного видео",
    )
    parser.add_argument("--anilist-action", choices=["update", "correct"], help=argparse.SUPPRESS)
    parser.add_argument("--tracking-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--anilist-id", help=argparse.SUPPRESS)
    parser.add_argument("--manual", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--playback-save", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--playback-video", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--playback-position", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--playback-duration", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--playback-active-seconds", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--start-at", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--fullscreen", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--subtitle-translate", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--subtitle-prewarm-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--subtitle-prewarm-from", type=float, default=0.0, help=argparse.SUPPRESS)
    parser.add_argument("--subtitle-study-text", default="", help=argparse.SUPPRESS)
    parser.add_argument("--subtitle-study-context", default="", help=argparse.SUPPRESS)
    parser.add_argument("--subtitle-study-media-id", type=int, help=argparse.SUPPRESS)
    return parser


def _string_list(value: str) -> list[str]:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [str(item).strip() for item in payload if str(item).strip()]


def _anime_hint_from_args(args: argparse.Namespace) -> AniListAnime | None:
    if args.media_id is None:
        return None
    title = str(args.media_title or "").strip() or str(args.media_id)
    titles = _string_list(args.media_titles_json)
    if title not in titles:
        titles.insert(0, title)
    return AniListAnime(
        id=int(args.media_id),
        titles=titles,
        synonyms=_string_list(args.media_synonyms_json),
        season_year=None,
        episodes=args.media_episodes,
        format=str(args.media_format or "").strip() or None,
        score=1000.0,
    )


def _print_identity(identity: VideoIdentity) -> None:
    episode = f" episode={identity.episode}" if identity.episode is not None else ""
    season = f" season={identity.season}" if identity.season is not None else ""
    print(f"Anime: {identity.title!r}{season}{episode}")


def _choose_with_optional_llm(
    candidates: list[SubtitleCandidate],
    identity: VideoIdentity,
    llm: OllamaClient | None,
    ambiguity_margin: float,
    *,
    allow_llm: bool = True,
) -> SubtitleCandidate | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    margin = candidates[0].score - candidates[1].score
    if allow_llm and llm is not None and margin < ambiguity_margin:
        index = llm.select_subtitle(identity, candidates)
        if index is not None:
            return candidates[index]
    return candidates[0]


def _subtitle_content_fingerprint(
    candidate: SubtitleCandidate,
    cache_dir: Path,
    *,
    ffmpeg_path: str,
) -> str | None:
    path = candidate.path
    if path.suffix.casefold() in {".ass", ".ssa"}:
        path, _ = convert_to_plain_srt(
            path,
            cache_dir,
            ffmpeg_path=ffmpeg_path,
            force=False,
            verbose=False,
        )
    if path.suffix.casefold() != ".srt":
        return None
    try:
        cues = parse_srt(path)
    except OSError:
        return None
    if not cues:
        return None
    normalized = []
    for start, end, text in cues:
        clean_text = re.sub(r"\s+", "", text).casefold()
        clean_text = re.sub(r"<[^>]+>|\{\\[^}]+\}", "", clean_text)
        normalized.append(f"{round(start, 2):.2f}|{round(end, 2):.2f}|{clean_text}")
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def _deduplicate_subtitle_candidates(
    candidates: list[SubtitleCandidate],
    cache_dir: Path,
    *,
    ffmpeg_path: str,
) -> tuple[list[SubtitleCandidate], int]:
    # Compatibility wrapper for callers/tests from the pre-package pipeline.
    return deduplicate_candidates(candidates, cache_dir, ffmpeg_path=ffmpeg_path)


def _choose_anilist(
    candidates: list[AniListAnime],
    identity: VideoIdentity,
    llm: OllamaClient | None,
    ambiguity_margin: float,
) -> AniListAnime | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    margin = candidates[0].score - candidates[1].score
    if llm is not None and margin < ambiguity_margin:
        index = llm.select_anilist(identity, candidates)
        if index is not None:
            return candidates[index]
    return candidates[0]


def _resolve_anilist(
    identity: VideoIdentity,
    config: AppConfig,
    llm: OllamaClient | None,
    verbose: bool,
    *,
    access_token: str = "",
) -> AniListAnime | None:
    if not config.anilist.enabled:
        return None
    client = AniListClient(config.anilist.endpoint, access_token=access_token)
    try:
        candidates = client.search(identity)
        if verbose:
            for item in candidates[:8]:
                print(f"  AniList {item.score:6.1f} id={item.id} titles={item.titles}")
        return _choose_anilist(candidates, identity, llm, config.llm.ambiguity_margin)
    except AniListError as exc:
        print(exc)
        return None
    finally:
        client.close()




def _episode_airing_at(
    anime: AniListAnime | None,
    identity: VideoIdentity,
    config: AppConfig,
    verbose: bool,
) -> int | None:
    if anime is None or identity.episode is None or not config.anilist.enabled:
        return None
    cache_dir = config.paths.cache_dir / "anilist-airing"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{anime.id}-{identity.episode}.json"
    now = time.time()
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        airing_at = int(payload.get("airing_at") or 0)
        fetched_at = float(payload.get("fetched_at") or 0)
        if airing_at > 0:
            return airing_at
        if now - fetched_at < 6 * 3600:
            return None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    client = AniListClient(config.anilist.endpoint, access_token=config.anilist.access_token)
    try:
        airing_at = client.episode_airing_at(anime.id, identity.episode)
    except AniListError as exc:
        if verbose:
            print(f"AniList: дата выхода серии недоступна: {exc}")
        return None
    finally:
        client.close()

    try:
        cache_path.write_text(
            json.dumps({"airing_at": airing_at or 0, "fetched_at": now}),
            encoding="utf-8",
        )
    except OSError:
        pass
    if verbose and airing_at:
        print(
            f"AniList: серия {identity.episode} вышла "
            f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(airing_at))}"
        )
    return airing_at

def _library_anilist_hint(video: Path, config: AppConfig) -> AniListAnime | None:
    """Reuse the authoritative AniList identity already stored by the app.

    The standalone CLI historically re-guessed a title from the release filename.
    That is especially unsafe for absolute-numbered split cours (e.g. BLEACH
    S17E43 parsing as "Bleach 2004") even though the Library DB already knows
    the exact media id.  Prefer that local identity and avoid unnecessary AniList
    search traffic.
    """
    try:
        db = Database(config.library.database_path)
        episode = db.episode_by_path(video.expanduser().resolve())
        if episode is None or episode.media_id is None:
            return None
        anime = db.get_anime(int(episode.media_id))
        if anime is None:
            return None
        titles = list(dict.fromkeys([str(anime.title or "").strip(), *[str(v).strip() for v in anime.titles if str(v).strip()]]))
        titles = [value for value in titles if value]
        return AniListAnime(
            id=int(anime.media_id),
            titles=titles or [str(anime.media_id)],
            synonyms=[str(v).strip() for v in anime.synonyms if str(v).strip()],
            season_year=anime.season_year,
            episodes=anime.episodes,
            format=anime.format,
            score=1000.0,
        )
    except Exception:
        return None


def _resolve_tracking_anilist(
    video: Path,
    identity: VideoIdentity,
    config: AppConfig,
    llm: OllamaClient | None,
    verbose: bool,
) -> tuple[AniListAnime | None, str]:
    key = mapping_key(video, identity)
    library_hint = _library_anilist_hint(video, config)
    if library_hint is not None:
        if verbose:
            print(f"AniList: сопоставление из Library id={library_hint.id}, titles={library_hint.titles}")
        save_mapping(
            config.paths.cache_dir,
            key,
            library_hint,
            ttl_hours=config.anilist.mapping_cache_hours,
        )
        return library_hint, key
    cached = load_mapping(config.paths.cache_dir, key)
    if cached is not None:
        if verbose:
            print(f"AniList: сопоставление из кэша id={cached.id}, titles={cached.titles}")
        return cached, key

    anime = _resolve_anilist(
        identity,
        config,
        llm,
        verbose,
        access_token=config.anilist.access_token,
    )
    if anime is not None:
        save_mapping(
            config.paths.cache_dir,
            key,
            anime,
            ttl_hours=config.anilist.mapping_cache_hours,
        )
    return anime, key


def _run_playback_save(args: argparse.Namespace, config: AppConfig) -> int:
    if args.playback_video is None:
        return 2
    video = args.playback_video.expanduser().resolve()
    try:
        Database(config.library.database_path).record_playback(
            video,
            args.playback_position,
            args.playback_duration,
            args.playback_active_seconds,
        )
    except Exception as exc:
        print(f"Playback save warning: {exc}", file=sys.stderr)
        return 1
    return 0


def _osd(config: AppConfig, english: str, russian: str) -> str:
    return russian if config.ui.language == "ru" else english


def _run_anilist_action(args: argparse.Namespace, config: AppConfig) -> int:
    if args.tracking_file is None:
        print("Ошибка: tracking file не задан", file=sys.stderr)
        return 2
    try:
        payload = read_tracking_file(args.tracking_file.expanduser())
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"Ошибка чтения tracking file: {exc}", file=sys.stderr)
        return 2

    if not config.anilist.access_token:
        print("OSD:" + _osd(
            config,
            "AniList access token is not configured",
            "AniList access token не задан",
        ))
        return 1

    client = AniListClient(config.anilist.endpoint, access_token=config.anilist.access_token)
    try:
        if args.anilist_action == "correct":
            if not args.anilist_id:
                print("OSD:" + _osd(
                    config,
                    "Enter an AniList ID or URL",
                    "Нужен AniList ID или URL",
                ))
                return 2
            media_id = parse_anilist_id(args.anilist_id)
            anime = client.get_anime(media_id)
            save_mapping(
                config.paths.cache_dir,
                payload.mapping_key,
                anime,
                corrected=True,
                ttl_hours=config.anilist.mapping_cache_hours,
            )
            updated_payload = update_tracking_media(args.tracking_file, anime)
            title = anime.titles[0] if anime.titles else str(media_id)
            print(f"ANILIST_ID:{media_id}")
            print("OSD:" + _osd(
                config,
                f"AniList match corrected: {title} (id={media_id})",
                f"AniList исправлен: {title} (id={media_id})",
            ))
            if updated_payload.episode > (anime.episodes or updated_payload.episode):
                print("OSD:" + _osd(
                    config,
                    "Warning: episode number exceeds the AniList episode count",
                    "Предупреждение: номер серии больше числа серий AniList",
                ))
            return 0

        if not args.manual and config.playback.enabled:
            evidence = Database(config.library.database_path).playback_evidence(Path(payload.video))
            duration = float(evidence.get("duration") or 0.0)
            active = float(evidence.get("active_seconds") or 0.0)
            required = max(180.0, duration * 0.65) if duration > 0 else 300.0
            if active + 0.5 < required:
                print("ANILIST_DEFERRED:1")
                print("OSD:" + _osd(
                    config,
                    (
                        "AniList: not counted — actually watched "
                        f"{active / 60:.1f} min of the required {required / 60:.1f} min"
                    ),
                    (
                        "AniList: серия не засчитана — реально просмотрено "
                        f"{active / 60:.1f} мин из необходимых {required / 60:.1f}"
                    ),
                ))
                return 3

        result = client.update_progress(
            payload.media_id,
            payload.episode,
            payload.total_episodes,
            add_if_missing=config.anilist.add_if_missing,
            update_when_rewatching=config.anilist.update_when_rewatching,
            completed_to_rewatching_on_episode_one=(
                config.anilist.completed_to_rewatching_on_episode_one
            ),
            complete_current_final=config.anilist.complete_current_final,
            complete_rewatching_final=config.anilist.complete_rewatching_final,
        )
        reason = str(result.get("reason") or "")
        counted_locally = bool(result.get("updated")) or reason == "already_at_or_above"
        if counted_locally:
            try:
                scheduled = Database(config.library.database_path).schedule_cleanup(
                    Path(payload.video),
                    config.agent.delete_after_watched_hours,
                    list_status=str(result.get("status") or ""),
                    media_id=payload.media_id,
                    episode=payload.episode,
                )
                if not scheduled:
                    print(
                        "AniList cleanup queue warning: local episode row was not found",
                        file=sys.stderr,
                    )
            except Exception as cleanup_exc:
                print(f"AniList cleanup queue warning: {cleanup_exc}", file=sys.stderr)

        if result.get("updated"):
            print("OSD:" + _osd(
                config,
                f"AniList: entry {payload.episode} counted ({result.get('status') or '-'})",
                f"AniList: серия {payload.episode} засчитана ({result.get('status') or '-'})",
            ))
        elif reason == "already_at_or_above":
            print("OSD:" + _osd(
                config,
                f"AniList: progress is already {result.get('progress')}",
                f"AniList: прогресс уже {result.get('progress')}",
            ))
        elif reason == "not_on_list":
            print("OSD:" + _osd(
                config,
                "AniList: title is not on your list; automatic adding is disabled",
                "AniList: аниме не в списке; автодобавление отключено",
            ))
        elif reason == "rewatching_disabled":
            print("OSD:" + _osd(
                config,
                "AniList: rewatch progress updates are disabled",
                "AniList: обновление повторного просмотра отключено",
            ))
        elif reason == "status_not_modifiable":
            print("OSD:" + _osd(
                config,
                f"AniList: status {result.get('status') or '-'} was not changed",
                f"AniList: статус {result.get('status') or '-'} не изменён",
            ))
        else:
            print("OSD:" + _osd(
                config,
                "AniList: no update is needed",
                "AniList: обновление не требуется",
            ))
        return 0
    except (AniListError, ValueError) as exc:
        print("OSD:" + _osd(
            config,
            f"AniList error: {exc}",
            f"AniList: ошибка: {exc}",
        ))
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        client.close()


def _correct_anilist_mapping(
    video: Path,
    value: str,
    config: AppConfig,
) -> int:
    if not config.anilist.access_token:
        print("Ошибка: для проверки AniList ID нужен access token", file=sys.stderr)
        return 1
    video = video.expanduser().resolve()
    identity = parse_anime_filename(video.name)
    key = mapping_key(video, identity)
    client = AniListClient(config.anilist.endpoint, access_token=config.anilist.access_token)
    try:
        anime = client.get_anime(parse_anilist_id(value))
        save_mapping(
            config.paths.cache_dir,
            key,
            anime,
            corrected=True,
            ttl_hours=config.anilist.mapping_cache_hours,
        )
    except (AniListError, ValueError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    title = anime.titles[0] if anime.titles else str(anime.id)
    print(f"AniList-сопоставление сохранено: {title} (id={anime.id})")
    return 0

def _cached_relative_tracking_episode(
    anime: AniListAnime, absolute_episode: int, config: AppConfig
) -> int | None:
    """Convert a release absolute number to this AniList cour without network."""
    total = int(anime.episodes or 0)
    absolute_episode = int(absolute_episode)
    if absolute_episode < 1:
        return None
    if not total or absolute_episode <= total:
        return absolute_episode

    for folder in ("anilist-episode-offset", "anilist-release-numbering"):
        path = config.paths.cache_dir / folder / f"{anime.id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            offset = max(0, int(payload.get("offset", 0)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        relative = absolute_episode - offset
        if offset and 1 <= relative <= total:
            return relative

    try:
        cached = Database(config.library.database_path).relation_graph_for_media(anime.id)
    except Exception:
        cached = None
    graph = cached.get("graph") if isinstance(cached, dict) else None
    if not isinstance(graph, dict):
        return None

    nodes = {
        int(node["media_id"]): node
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("media_id") is not None
    }
    if anime.id not in nodes:
        return None
    incoming: dict[int, list[int]] = {}
    for edge in graph.get("edges", []):
        if not isinstance(edge, dict) or str(edge.get("relation_type") or "").upper() != "SEQUEL":
            continue
        try:
            source, target = int(edge.get("source")), int(edge.get("target"))
        except (TypeError, ValueError):
            continue
        if source in nodes and target in nodes and source != target:
            incoming.setdefault(target, []).append(source)

    valid_formats = {"TV", "TV_SHORT", "ONA", ""}
    episode_cap = max(100, total * 4)
    current_id = int(anime.id)
    visited = {current_id}
    offset = 0
    for _ in range(12):
        current = nodes[current_id]
        current_title = str(current.get("title") or (anime.titles[0] if anime.titles else ""))
        candidates: list[tuple[float, dict[str, object]]] = []
        for source_id in incoming.get(current_id, []):
            if source_id in visited:
                continue
            candidate = nodes[source_id]
            try:
                count = int(candidate.get("episodes") or 0)
            except (TypeError, ValueError):
                count = 0
            if str(candidate.get("format") or "").upper() not in valid_formats:
                continue
            if count < 1 or count > episode_cap:
                continue
            continuity = title_similarity(current_title, str(candidate.get("title") or ""))
            if continuity < 35.0:
                continue
            candidates.append((continuity, candidate))
        if not candidates:
            break
        _score, candidate = max(
            candidates,
            key=lambda item: (
                item[0],
                str(item[1].get("start_date") or ""),
                int(item[1].get("media_id") or 0),
            ),
        )
        offset += int(candidate.get("episodes") or 0)
        current_id = int(candidate["media_id"])
        visited.add(current_id)

    relative = absolute_episode - offset
    return relative if offset and 1 <= relative <= total else None


def _tracking_episode_from_hint(
    anime: AniListAnime, episode_hint: int, config: AppConfig, logger
) -> tuple[AniListAnime, int] | None:
    """Resolve a GUI/release episode hint to AniList-entry-local progress.

    Library filenames may intentionally retain release absolute numbering (for
    example BLEACH episode 43 for cour-local episode 3). AniList progress must
    never receive that absolute number.
    """
    episode_hint = int(episode_hint)
    total = int(anime.episodes or 0)
    if episode_hint < 1:
        return None
    if not total or episode_hint <= total:
        return anime, episode_hint

    relative = _cached_relative_tracking_episode(anime, episode_hint, config)
    if relative is not None:
        logger.info(
            "RESULT step=anilist.tracking_episode media_id=%s absolute=%s relative=%s source=cache",
            anime.id, episode_hint, relative,
        )
        return anime, relative

    client = AniListClient(config.anilist.endpoint, access_token=config.anilist.access_token)
    try:
        resolved = client.resolve_absolute_episode(anime, episode_hint)
    except (AniListError, OSError, ValueError) as exc:
        logger.info(
            "SKIP step=anilist.tracking_episode media_id=%s absolute=%s reason=%r",
            anime.id, episode_hint, exc,
        )
        return None
    finally:
        client.close()
    if resolved is None:
        return None
    target, relative, _chain = resolved
    if target.episodes is not None and not (1 <= int(relative) <= int(target.episodes)):
        return None
    logger.info(
        "RESULT step=anilist.tracking_episode media_id=%s target_media_id=%s absolute=%s relative=%s source=anilist",
        anime.id, target.id, episode_hint, relative,
    )
    return target, int(relative)


def _anilist_episode(identity: VideoIdentity, anime: AniListAnime) -> int | None:
    if identity.episode is not None and identity.episode > 0:
        if anime.episodes is not None and identity.episode > anime.episodes:
            return None
        return identity.episode
    # Movies and one-entry Specials/OVAs often have no episode number in the
    # filename. AniList still expects progress=1 for them. Previously only the
    # MOVIE format received a tracker, so one-entry SPECIAL titles such as
    # "Boku no Hero Academia: I am a hero too" silently launched without the
    # Ctrl+A/automatic progress environment.
    if anime.episodes == 1 or (anime.format or "").upper() == "MOVIE":
        return 1
    return None


def _jimaku_entry_anilist_conflicts(
    entry: JimakuEntry,
    anime: AniListAnime | None,
) -> bool:
    # Both IDs are authoritative. A different linked AniList work/season must
    # never become a timing/quality fallback for the requested anime.
    if anime is None or entry.anilist_id is None:
        return False
    try:
        return int(entry.anilist_id) != int(anime.id)
    except (TypeError, ValueError):
        return False


def _jimaku_episode_aliases(
    anime: AniListAnime | None,
    requested_episode: int | None,
    config: AppConfig,
    logger,
) -> tuple[int, ...]:
    """Return additional absolute-number aliases for a season episode."""
    if anime is None or requested_episode is None or requested_episode < 1:
        return ()
    if (anime.format or "").upper() not in {"TV", "TV_SHORT", "ONA", ""}:
        return ()

    cache_path = config.paths.cache_dir / "anilist-episode-offset" / f"{anime.id}.json"
    release_cache_path = config.paths.cache_dir / "anilist-release-numbering" / f"{anime.id}.json"
    offset: int | None = None
    for local_cache in (cache_path, release_cache_path):
        try:
            if local_cache.is_file() and time.time() - local_cache.stat().st_mtime < 7 * 86400:
                payload = json.loads(local_cache.read_text(encoding="utf-8"))
                candidate_offset = int(payload.get("offset", 0))
                if candidate_offset >= 0:
                    offset = candidate_offset
                    logger.info(
                        "RESULT step=jimaku.episode_offset media_id=%s offset=%s source=%s",
                        anime.id, offset, local_cache.parent.name,
                    )
                    break
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue

    if offset is None:
        client = AniListClient(
            config.anilist.endpoint,
            access_token=config.anilist.access_token,
        )
        try:
            absolute, chain = client.absolute_episode_number(anime, requested_episode)
            offset = max(0, int(absolute) - int(requested_episode))
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "media_id": anime.id,
                        "offset": offset,
                        "chain": [item.id for item in chain],
                        "updated_at": time.time(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except (AniListError, OSError, ValueError) as exc:
            logger.info(
                "SKIP step=jimaku.episode_aliases media_id=%s episode=%s reason=%r",
                anime.id,
                requested_episode,
                exc,
            )
            return ()
        finally:
            client.close()

    if not offset:
        return ()

    # The local video filename can use either cour-relative numbering (e.g. 3)
    # or franchise/streaming absolute numbering (e.g. 43).  The cached offset is
    # the number of episodes before this AniList entry.  Treat it bidirectionally:
    # relative -> absolute for ordinary releases, absolute -> relative when the
    # requested number is clearly outside the current entry's episode count.
    episode_count = int(anime.episodes or 0)
    aliases: list[int] = []
    if episode_count > 0 and requested_episode > episode_count:
        relative_episode = requested_episode - offset
        if 1 <= relative_episode <= episode_count:
            aliases.append(relative_episode)
            logger.info(
                "RESULT step=jimaku.episode_aliases media_id=%s absolute=%s relative=%s offset=%s",
                anime.id,
                requested_episode,
                relative_episode,
                offset,
            )
    else:
        absolute_episode = requested_episode + offset
        if absolute_episode != requested_episode:
            aliases.append(absolute_episode)
            logger.info(
                "RESULT step=jimaku.episode_aliases media_id=%s relative=%s absolute=%s offset=%s",
                anime.id,
                requested_episode,
                absolute_episode,
                offset,
            )
    return tuple(dict.fromkeys(aliases))


def _find_online_subtitles(
    video: Path,
    identity: VideoIdentity,
    config: AppConfig,
    llm: OllamaClient | None,
    verbose: bool,
    anime_hint: AniListAnime | None = None,
    *,
    skip_airing_lookup: bool = False,
) -> list[SubtitleCandidate]:
    if not config.jimaku.api_key:
        print("Jimaku: API key не задан, интернет-поиск пропущен")
        return []

    logger = configure_logging()
    anime = anime_hint or _resolve_anilist(identity, config, llm, verbose)
    is_movie = bool(anime is not None and (anime.format or "").upper() == "MOVIE")
    # Movie release names often contain values such as ``Movie 1`` or a year.
    # They are part of the title, not a Jimaku episode filter.  Query the full
    # entry and rank it as an episode-less work even if the filename parser
    # produced a spurious episode number.
    requested_episode = None if is_movie else identity.episode
    episode_aliases = _jimaku_episode_aliases(
        anime,
        requested_episode,
        config,
        logger,
    )
    jimaku_identity = (
        replace(identity, episode=None)
        if is_movie and identity.episode is not None
        else identity
    )
    airing_identity = identity
    if (
        anime is not None
        and requested_episode is not None
        and anime.episodes is not None
        and requested_episode > anime.episodes
    ):
        relative_alias = next(
            (value for value in episode_aliases if 1 <= value <= int(anime.episodes or 0)),
            None,
        )
        if relative_alias is not None:
            airing_identity = replace(identity, episode=int(relative_alias))
    expected_airing_at = (
        None
        if skip_airing_lookup or is_movie
        else _episode_airing_at(anime, airing_identity, config, verbose)
    )
    logger.info(
        "START step=jimaku.lookup video=%s title=%r episode=%s requested_episode=%s "
        "media_format=%s anilist_id=%s expected_airing_at=%s",
        video.name, identity.title, identity.episode, requested_episode,
        anime.format if anime else None, anime.id if anime else None, expected_airing_at,
    )

    jimaku = JimakuClient(
        config.jimaku.base_url,
        config.jimaku.api_key,
        cache_dir=config.paths.cache_dir,
    )
    try:
        entries_by_id = jimaku.search_entries(anilist_id=anime.id) if anime else []
        exact_id_entries = [
            entry
            for entry in entries_by_id
            if anime is not None and entry.anilist_id == anime.id
        ]

        def identity_exact_entry(entry: JimakuEntry) -> bool:
            wanted = normalize_title(identity.title)
            if not wanted:
                return False
            return any(
                normalize_title(name) == wanted
                for name in (entry.name, entry.english_name or "", entry.japanese_name or "")
                if normalize_title(name)
            )

        explicit_season_exact_id = bool(
            identity.season is not None
            and anime is not None
            and any(entry.anilist_id == anime.id for entry in exact_id_entries)
        )
        id_matches_identity = (
            any(identity_exact_entry(entry) for entry in exact_id_entries)
            or explicit_season_exact_id
        )
        if explicit_season_exact_id and exact_id_entries:
            logger.info(
                "ACCEPT step=jimaku.entry_identity video=%s reason=explicit_season_exact_anilist "
                "season=%s anilist_id=%s entries=%s",
                video.name,
                identity.season,
                anime.id if anime else None,
                [(entry.id, entry.anilist_id, entry.name) for entry in exact_id_entries],
            )
        single_work = bool(
            anime is not None
            and (
                anime.episodes == 1
                or (anime.format or "").upper() in {"OVA", "ONA", "SPECIAL", "TV_SHORT"}
            )
        )

        def identity_title_overrides_anilist_conflict(entry: JimakuEntry) -> bool:
            # An exact local title may legitimately point at a separately linked
            # movie/special whose parent AniList ID was inherited by the download.
            # For an ordinary numbered TV episode, however, explicit AniList IDs
            # are the season boundary: a generic franchise title such as Re:Zero
            # must not let S01E12 satisfy S04E12/absolute episode 78.
            return bool(
                identity_exact_entry(entry)
                and (requested_episode is None or is_movie or single_work)
            )

        entries_by_name = []
        # Newly created one-episode specials are often present on Jimaku before
        # their AniList ID is attached to the entry. Always perform a title lookup
        # for these works even when an ID lookup returned another/stale entry.
        if (
            not exact_id_entries
            or (single_work and not is_movie)
            or (not is_movie and not id_matches_identity)
        ):
            search_names = [identity.title]
            if anime is not None:
                search_names.extend(anime.titles)
                search_names.extend(anime.synonyms)
            # Jimaku entries may use the ASCII spelling while AniList keeps accents.
            # Query both forms, e.g. Caraméliser and Carameliser.
            search_names.extend(fold_search_title(name) for name in list(search_names))
            for search_name in dict.fromkeys(name.strip() for name in search_names if name.strip()):
                entries_by_name.extend(jimaku.search_entries(query=search_name))
        else:
            logger.info(
                "SKIP step=jimaku.name_search video=%s reason=exact_anilist_id entries=%s",
                video.name,
                len(exact_id_entries),
            )
        deduplicated = {entry.id: entry for entry in [*entries_by_id, *entries_by_name]}
        identity_exact_entries = [
            entry for entry in deduplicated.values() if identity_exact_entry(entry)
        ]
        if (
            any(identity_title_overrides_anilist_conflict(entry) for entry in identity_exact_entries)
            and exact_id_entries
            and not id_matches_identity
        ):
            # The authoritative local identity can be more specific than stale or
            # incorrectly inherited download metadata (for example a sequel movie
            # resolving to its parent TV entry). An exact Jimaku title result wins
            # over the conflicting ID result, without any per-anime hardcode.
            conflicting_ids = {entry.id for entry in exact_id_entries}
            deduplicated = {
                entry_id: entry
                for entry_id, entry in deduplicated.items()
                if entry_id not in conflicting_ids
            }
            logger.info(
                "OVERRIDE step=jimaku.entries video=%s reason=exact_identity_title_over_conflicting_anilist "
                "requested_anilist_id=%s removed_entries=%s exact_entries=%s",
                video.name, anime.id if anime else None,
                sorted(conflicting_ids),
                [(entry.id, entry.anilist_id, entry.name) for entry in identity_exact_entries],
            )
        entries = jimaku.rank_entries(list(deduplicated.values()), identity, anime.id if anime else None)
        logger.info(
            "RESULT step=jimaku.entries video=%s by_id=%s by_name=%s deduplicated=%s ranked=%s "
            "id_entries=%s name_entries=%s",
            video.name, len(entries_by_id), len(entries_by_name), len(deduplicated), len(entries),
            [(entry.id, entry.anilist_id, entry.name) for entry in entries_by_id],
            [(entry.id, entry.anilist_id, entry.name) for entry in entries_by_name],
        )
        if not entries:
            logger.info("REJECT step=jimaku.lookup video=%s reason=no_entries", video.name)
            print("Jimaku: подходящее аниме не найдено")
            return []

        candidates: list[SubtitleCandidate] = []
        seen_urls: set[str] = set()
        limit = max(0, config.matching.max_jimaku_candidates)
        for entry in entries[:4]:
            logger.info(
                "CANDIDATE step=jimaku.entry video=%s entry_id=%s anilist_id=%s name=%r",
                video.name, entry.id, entry.anilist_id, entry.name,
            )
            if (
                _jimaku_entry_anilist_conflicts(entry, anime)
                and not identity_title_overrides_anilist_conflict(entry)
            ):
                logger.info(
                    "REJECT step=jimaku.entry video=%s entry_id=%s reason=explicit_anilist_mismatch "
                    "entry_anilist_id=%s requested_anilist_id=%s name=%r",
                    video.name,
                    entry.id,
                    entry.anilist_id,
                    anime.id if anime else None,
                    entry.name,
                )
                continue
            if verbose:
                print(f"  Jimaku entry candidate: {entry.name} (id={entry.id})")
            entry_files = (
                jimaku.files_for_episode(
                    entry.id,
                    requested_episode,
                    alternative_episodes=episode_aliases,
                )
                if episode_aliases
                else jimaku.files_for_episode(entry.id, requested_episode)
            )
            files = jimaku.rank_files(
                entry_files,
                jimaku_identity,
                video,
                prefer_srt=config.matching.prefer_srt,
                expected_airing_at=expected_airing_at,
                alternative_episodes=episode_aliases,
            )
            if verbose:
                for item in files[:20]:
                    print(f"    Jimaku {item.score:6.1f} {item.name}")

            for item in files:
                logger.info(
                    "CANDIDATE step=jimaku.file video=%s entry_id=%s score=%.1f min_score=%.1f parsed_episode=%s episode_match=%s title_similarity=%s overlap=%s format_bonus=%s airing_sanity=%s name=%r",
                    video.name, entry.id, item.score, config.matching.jimaku_min_score,
                    item.details.get("parsed_episode"), item.details.get("episode_match"),
                    item.details.get("title_similarity"), item.details.get("release_token_overlap"),
                    item.details.get("format_bonus"), item.details.get("airing_sanity"), item.name,
                )
                if str(item.details.get("airing_sanity", "")).startswith("before_airing") and verbose:
                    print(
                        f"    Jimaku penalty: {item.name} загружен раньше выхода "
                        f"серии на AniList"
                    )
                exact_anilist_entry = bool(
                    anime is not None
                    and entry.anilist_id is not None
                    and entry.anilist_id == anime.id
                )
                reference_names = [identity.title]
                if anime is not None:
                    reference_names.extend(anime.titles)
                    reference_names.extend(anime.synonyms)
                entry_names = [entry.name, entry.english_name or "", entry.japanese_name or ""]
                identity_exact_title_entry = any(
                    normalize_title(identity.title) == normalize_title(right)
                    for right in entry_names
                    if normalize_title(identity.title) and normalize_title(right)
                )
                exact_title_entry = any(
                    normalize_title(left) == normalize_title(right)
                    for left in reference_names
                    for right in entry_names
                    if normalize_title(left) and normalize_title(right)
                )
                exact_single_work = bool(single_work and exact_title_entry)
                exact_episodeless_title_entry = bool(
                    requested_episode is None and identity_exact_title_entry
                )
                exact_work_entry = bool(
                    exact_anilist_entry or exact_single_work or exact_episodeless_title_entry
                )
                if (
                    (single_work and exact_work_entry and requested_episode in {None, 1})
                    or exact_episodeless_title_entry
                ):
                    item.details["episode_match"] = "exact"
                exact_anilist_episode = bool(
                    exact_work_entry
                    and item.details.get("episode_match") in {"exact", "absolute", "range"}
                )
                exact_anilist_movie = bool(exact_anilist_entry and is_movie)
                # An exact AniList-linked one-episode special is strong enough
                # even when the subtitle filename is generic and therefore has no
                # parseable episode/title.  Previously this override required an
                # exact textual title too, which rejected valid Jimaku entries such
                # as "I am a hero too" despite a correct AniList link.
                exact_single_special = bool(single_work and exact_work_entry and not is_movie)
                logger.info(
                    "DECISION step=jimaku.match video=%s entry_id=%s exact_anilist=%s "
                    "exact_title=%s identity_exact_title=%s single_work=%s exact_work=%s "
                    "single_special=%s requested_episode=%s parsed_episode=%s episode_match=%s score=%.1f",
                    video.name, entry.id, exact_anilist_entry, exact_title_entry,
                    identity_exact_title_entry, single_work, exact_work_entry, exact_single_special,
                    requested_episode, item.details.get("parsed_episode"),
                    item.details.get("episode_match"), item.score,
                )
                if (
                    item.score < config.matching.jimaku_min_score
                    and not exact_anilist_episode
                    and not exact_anilist_movie
                    and not exact_single_special
                ):
                    logger.info(
                        "REJECT step=jimaku.file video=%s entry_id=%s reason=score_below_threshold score=%.1f min_score=%.1f name=%r",
                        video.name, entry.id, item.score, config.matching.jimaku_min_score, item.name,
                    )
                    continue
                if item.score < config.matching.jimaku_min_score and exact_anilist_episode:
                    logger.info(
                        "OVERRIDE step=jimaku.file video=%s entry_id=%s reason=exact_anilist_episode score=%.1f min_score=%.1f name=%r",
                        video.name, entry.id, item.score, config.matching.jimaku_min_score, item.name,
                    )
                if item.score < config.matching.jimaku_min_score and exact_anilist_movie:
                    original_score = item.score
                    # An exact AniList movie entry is stronger evidence than the
                    # often unrelated release filename.  Promote it only enough
                    # to enter the normal timing/semantic validation pipeline.
                    item.score = config.matching.jimaku_min_score + 10.0
                    item.details["movie_exact_entry_override"] = True
                    item.details["original_score"] = original_score
                    logger.info(
                        "OVERRIDE step=jimaku.file video=%s entry_id=%s reason=exact_anilist_movie "
                        "score=%.1f promoted_score=%.1f min_score=%.1f name=%r",
                        video.name, entry.id, original_score, item.score,
                        config.matching.jimaku_min_score, item.name,
                    )
                if item.score < config.matching.jimaku_min_score and exact_single_special:
                    original_score = item.score
                    item.score = config.matching.jimaku_min_score + 10.0
                    item.details["single_special_exact_title_override"] = True
                    item.details["original_score"] = original_score
                    logger.info(
                        "OVERRIDE step=jimaku.file video=%s entry_id=%s reason=exact_single_special_title "
                        "score=%.1f promoted_score=%.1f min_score=%.1f name=%r",
                        video.name, entry.id, original_score, item.score,
                        config.matching.jimaku_min_score, item.name,
                    )
                if item.url in seen_urls:
                    logger.info(
                        "REJECT step=jimaku.file video=%s entry_id=%s reason=duplicate_url name=%r",
                        video.name, entry.id, item.name,
                    )
                    continue
                seen_urls.add(item.url)
                with timed_step(
                    logger, "jimaku.materialize", video=video.name, entry_id=entry.id, name=repr(item.name)
                ):
                    materialized = materialize_jimaku_files(
                        jimaku,
                        item,
                        jimaku_identity,
                        video,
                        config.paths.cache_dir,
                        prefer_srt=config.matching.prefer_srt,
                        allowed_episodes=episode_aliases,
                    )
                if not materialized:
                    logger.info(
                        "REJECT step=jimaku.file video=%s entry_id=%s reason=no_japanese_or_extract_failed name=%r",
                        video.name, entry.id, item.name,
                    )
                for candidate in materialized:
                    candidate.details.update(
                        {
                            "entry_id": entry.id,
                            "entry_name": entry.name,
                            "entry_anilist_id": entry.anilist_id,
                            "requested_anilist_id": anime.id if anime else None,
                            "entry_anilist_match": exact_work_entry,
                            "entry_exact_title_match": exact_title_entry,
                            "entry_identity_exact_title_match": identity_exact_title_entry,
                            "single_special_exact_title_match": exact_single_work,
                            "single_special_exact_entry": exact_single_special,
                            "exact_anilist_movie_entry": exact_anilist_movie,
                            "media_format": anime.format if anime else None,
                            "requested_episode": requested_episode,
                            "movie_exact_entry_override": bool(
                                item.details.get("movie_exact_entry_override")
                            ),
                        }
                    )
                    candidates.append(candidate)
                    logger.info(
                        "ACCEPT step=jimaku.candidate video=%s entry_id=%s score=%.1f episode=%s name=%r path=%r",
                        video.name, entry.id, candidate.score, candidate.episode,
                        candidate.name, str(candidate.path),
                    )
                    if not config.matching.evaluate_all_jimaku:
                        print(f"Jimaku entry: {entry.name} (id={entry.id})")
                        return [candidate]
                    if limit and len(candidates) >= limit:
                        break
                if limit and len(candidates) >= limit:
                    break
            if limit and len(candidates) >= limit:
                break

        candidates.sort(key=lambda item: item.score, reverse=True)
        if not candidates:
            logger.info("REJECT step=jimaku.lookup video=%s reason=no_accepted_candidates", video.name)
            print("Jimaku: файл нужной серии не найден с достаточной уверенностью")
        else:
            logger.info(
                "RESULT step=jimaku.lookup video=%s accepted=%s best_score=%.1f best_name=%r",
                video.name, len(candidates), candidates[0].score, candidates[0].name,
            )
        return candidates
    finally:
        jimaku.close()


def _validate_explicit_subtitle(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"Файл субтитров не найден: {path}")
    suffix = path.suffix.casefold()
    if suffix in TEXT_SUBTITLE_EXTENSIONS and not is_japanese_subtitle(path):
        print("Предупреждение: явно заданные субтитры не распознаны как японские")
    elif suffix not in TEXT_SUBTITLE_EXTENSIONS | IMAGE_SUBTITLE_EXTENSIONS:
        print("Предупреждение: необычный формат субтитров; решение оставлено mpv")
    return path


def _print_sync_result(path: Path, result: dict[str, object]) -> None:
    reason = str(result.get("reason", "unknown"))
    engine = result.get("engine")
    score = result.get("alignment_score")
    suffix = f", engine={engine}" if engine else ""
    if score is not None:
        suffix += f", score={score}"
    if reason == "cached":
        print(f"Синхронизация: использую кэшированный файл {path}{suffix}")
    elif reason == "applied":
        offset = result.get("offset_seconds")
        scale = result.get("framerate_scale_factor")
        timing_parts = []
        if offset is not None:
            timing_parts.append(f"offset={offset}s")
        if scale is not None:
            timing_parts.append(f"scale={scale}")
        timing = (", ".join(timing_parts) + ", ") if timing_parts else ""
        print(f"Синхронизация применена: {timing}{suffix.lstrip(', ')}")
        diagnostics = result.get("segment_diagnostics")
        if (
            isinstance(diagnostics, dict)
            and diagnostics.get("available")
            and diagnostics.get("reliable", True)
        ):
            offsets = []
            for item in diagnostics.get("segments", []):
                if (
                    isinstance(item, dict)
                    and item.get("successful")
                    and not item.get("boundary_hit")
                ):
                    offsets.append(f"{item.get('label')}={item.get('offset_seconds')}s")
            if offsets:
                print("Локальная проверка: " + ", ".join(offsets))
        elif isinstance(diagnostics, dict) and diagnostics.get("available"):
            print(
                "Локальная проверка проигнорирована как ненадёжная: "
                f"{diagnostics.get('quality_reason', 'неоднозначная корреляция')}"
            )
    elif reason == "quality_offset_exceeded":
        print(
            "Синхронизация отклонена: найденный offset "
            f"{result.get('offset_seconds')}s превышает безопасный порог "
            f"{result.get('quality_max_offset_seconds')}s"
        )
    elif reason == "ffsubsync_missing":
        print("Синхронизация пропущена: ffsubsync не установлен")
    elif reason == "alass_missing":
        print("ALASS не установлен; использую результат ffsubsync")
    elif reason == "unsupported_format":
        print("Синхронизация пропущена: формат не поддерживается")
    elif reason == "disabled":
        print("Синхронизация отключена в настройках")
    else:
        detail = f": {result.get('error')}" if result.get("error") else ""
        print(f"Синхронизация не выполнена ({reason}){detail}")

    embedded_failure = result.get("embedded_reference_failure_reason")
    if embedded_failure:
        detail = result.get("embedded_reference_failure_error")
        suffix = f": {detail}" if detail else ""
        print(
            "Английский эталон был принят LLM, но прямой ALASS не сработал "
            f"({embedded_failure}){suffix}"
        )

    validation = result.get("timing_reference_validation")
    if isinstance(validation, dict):
        state = "принят" if validation.get("accepted") else "отклонён"
        print(
            "LLM-проверка английского эталона: "
            f"{state}, similarity={validation.get('similarity', '-')}, "
            f"совпало {validation.get('matched_samples', '-')}/"
            f"{validation.get('total_samples', '-')} групп, "
            f"reason={validation.get('reason', '-')}"
        )
    if result.get("selection_reason") == "embedded_timing_reference":
        print(
            "Тайминг выровнен по встроенной субтитровой дорожке: "
            f"{result.get('timing_reference_language') or '-'} / "
            f"{result.get('timing_reference_title') or '-'}"
        )
    elif result.get("selection_reason") == "pgs_onset_reference":
        before = result.get("onset_before")
        after = result.get("onset_after")
        print(
            "SUP выровнен по появлениям картинок и встроенной дорожке: "
            f"{result.get('timing_reference_language') or '-'} / "
            f"{result.get('timing_reference_title') or '-'}"
        )
        if isinstance(before, dict) and isinstance(after, dict):
            print(
                "Совпадение начал реплик: "
                f"{before.get('coverage', '-')}→{after.get('coverage', '-')}, "
                f"совпало {after.get('matched', '-')}/"
                f"{min(int(after.get('candidate_count', 0)), int(after.get('reference_count', 0)))}"
            )
        baseline_activity = result.get("baseline_reference_activity")
        reference_activity = result.get("reference_activity")
        if isinstance(baseline_activity, dict) and isinstance(reference_activity, dict):
            print(
                "Сравнение с эталоном: "
                f"начало {baseline_activity.get('start', '-')}→{reference_activity.get('start', '-')}, "
                f"середина {baseline_activity.get('middle', '-')}→{reference_activity.get('middle', '-')}, "
                f"конец {baseline_activity.get('end', '-')}→{reference_activity.get('end', '-')}"
            )


def _sync_one(
    video: Path,
    subtitle: Path,
    args: argparse.Namespace,
    config: AppConfig,
    llm: OllamaClient | None,
) -> tuple[Path, dict[str, object]]:
    return optimize_subtitle(
        video,
        subtitle,
        config.paths.cache_dir,
        config.sync,
        ffmpeg_path=config.tools.ffmpeg,
        ffprobe_path=config.tools.ffprobe,
        alass_path=config.tools.alass,
        force=args.resync,
        verbose=args.verbose,
        llm=llm,
        validate_embedded_reference_with_llm=config.llm.validate_embedded_reference,
    )


def process_video(
    video: Path,
    args: argparse.Namespace,
    config: AppConfig,
    llm: OllamaClient | None,
) -> int:
    video = video.expanduser().resolve()
    if not video.is_file():
        raise RuntimeError(f"Видео не найдено: {video}")
    logger = configure_logging()
    pipeline_timer = StageTimer(logger, video=video.name)
    reporter = SubtitleJobReporter.from_environment()
    reporter.update(SubtitleJobStage.DISCOVERING, video=video.name)
    if not args.prepare_only:
        mark_foreground(config.paths.cache_dir, video=video)
    if args.resync:
        invalidate_final_pipeline_result(video, config)
    if video.suffix.casefold() not in VIDEO_EXTENSIONS:
        print(f"Предупреждение: расширение {video.suffix or '(нет)'} необычно, пробую открыть через mpv")

    identity = parse_anime_filename(video.name)
    if llm is not None and (identity.episode is None or len(identity.title) < 4):
        identity = llm.improve_identity(identity)
    _print_identity(identity)
    pipeline_timer.mark("identity")

    anime_hint = _anime_hint_from_args(args)

    tracking_anime: AniListAnime | None = None
    tracking_episode: int | None = None
    tracking_mapping_key = ""
    tracking_available = (
        config.anilist.enabled
        and bool(config.anilist.access_token)
        and not args.offline
        and not args.no_anilist_progress
    )
    tracking_auto = tracking_available and config.anilist.auto_update_progress

    if args.fast_play and anime_hint is not None and tracking_available:
        tracking_anime = anime_hint
        tracking_mapping_key = mapping_key(video, identity)
        if args.episode_hint is not None:
            resolved_hint = _tracking_episode_from_hint(
                anime_hint, args.episode_hint, config, logger
            )
            if resolved_hint is not None:
                tracking_anime, tracking_episode = resolved_hint
            else:
                tracking_episode = None
                print(
                    f"AniList: номер серии {args.episode_hint} не помещается в "
                    f"entry из {anime_hint.episodes or '?'} серий; трекер отключён "
                    "для защиты прогресса"
                )
        else:
            tracking_episode = _anilist_episode(identity, anime_hint)
        title = tracking_anime.titles[0] if tracking_anime.titles else str(tracking_anime.id)
        print(
            f"AniList: использую подготовленное сопоставление {title!r} "
            f"(id={anime_hint.id})"
        )
        if tracking_episode is not None:
            print(
                f"AniList: после {config.anilist.watched_threshold * 100:.1f}% "
                f"обновлю до серии {tracking_episode}"
            )
    elif args.offline:
        print("AniList: отключён для этого запуска (--offline)")
    elif args.no_anilist_progress:
        print("AniList: обновление прогресса отключено для этого запуска")
    elif not config.anilist.enabled:
        print("AniList: интеграция отключена в настройках")
    elif not config.anilist.access_token:
        print("AniList: access token не задан; трекер и горячие клавиши отключены")
    else:
        if not config.anilist.auto_update_progress:
            print(
                "AniList: автоматическое засчитывание выключено; "
                "загружаю ручные команды Ctrl+A / Ctrl+B / C"
            )

        tracking_anime, tracking_mapping_key = _resolve_tracking_anilist(
            video, identity, config, llm, args.verbose
        )
        if tracking_anime is not None:
            tracking_episode = _anilist_episode(identity, tracking_anime)
            if (
                tracking_episode is None
                and identity.episode is not None
                and tracking_anime.episodes is not None
                and identity.episode > tracking_anime.episodes
            ):
                local_aliases = _jimaku_episode_aliases(
                    tracking_anime, identity.episode, config, logger
                )
                local_relative = next(
                    (
                        value
                        for value in local_aliases
                        if 1 <= value <= int(tracking_anime.episodes or 0)
                    ),
                    None,
                )
                if local_relative is not None:
                    tracking_episode = int(local_relative)
                    title = (
                        tracking_anime.titles[0]
                        if tracking_anime.titles
                        else str(tracking_anime.id)
                    )
                    print(
                        f"AniList: абсолютная серия {identity.episode} → "
                        f"{title!r}, серия {tracking_episode} "
                        "(локальный кэш нумерации)"
                    )
                else:
                    print(
                        f"AniList: абсолютная серия {identity.episode} больше "
                        f"{tracking_anime.episodes}; ищу нужный cour по цепочке SEQUEL…"
                    )
                    client = AniListClient(
                        config.anilist.endpoint,
                        access_token=config.anilist.access_token,
                    )
                    try:
                        resolved = client.resolve_absolute_episode(
                            tracking_anime, identity.episode
                        )
                    except AniListError as exc:
                        print(f"AniList: не удалось разобрать абсолютную нумерацию: {exc}")
                        resolved = None
                    finally:
                        client.close()

                    if resolved is not None:
                        tracking_anime, tracking_episode, chain = resolved
                        chain_text = " → ".join(
                            (item.titles[0] if item.titles else str(item.id))
                            for item in chain
                        )
                        target_title = (
                            tracking_anime.titles[0]
                            if tracking_anime.titles
                            else str(tracking_anime.id)
                        )
                        print(
                            f"AniList: абсолютная серия {identity.episode} → "
                            f"{target_title!r}, серия {tracking_episode}"
                        )
                        if args.verbose:
                            print(f"AniList cour chain: {chain_text}")

            if tracking_episode is None:
                print(
                    "AniList: не удалось определить запись/cour и относительный "
                    "номер серии; трекер для этого запуска отключён"
                )
            else:
                title = (
                    tracking_anime.titles[0]
                    if tracking_anime.titles
                    else str(tracking_anime.id)
                )
                if tracking_auto:
                    percent = config.anilist.watched_threshold * 100
                    print(
                        f"AniList: после {percent:.1f}% обновлю {title!r} "
                        f"до серии {tracking_episode}"
                    )
                else:
                    print(
                        f"AniList: ручной режим для {title!r}, "
                        f"серия {tracking_episode}"
                    )
                print(
                    "AniList hotkeys: Ctrl+A — засчитать, "
                    "Ctrl+B — открыть, C — исправить"
                )
        else:
            print(
                "AniList: не удалось сопоставить файл с записью; "
                "трекер для этого запуска отключён"
            )

    pipeline_timer.mark("anilist_tracking")

    subtitle: Path | None = None
    subtitle_id: int | None = None
    embedded_bitmap_fallback = None
    bitmap_candidate_fallback: SubtitleCandidate | None = None
    selected_candidate: SubtitleCandidate | None = None
    selected_source = ""
    alignment_result: dict[str, object] = {}
    generated_by_ocr = False
    ocr_source_path: Path | None = None
    ocr_source_embedded_stream_index: int | None = None
    already_synced = False
    pipeline_cache_hit = False
    pipeline_cache_allowed = (
        not args.resync
        and args.sub is None
        and args.embedded_sid is None
        and not args.force_search
        and not args.fast_play
    )
    cached_pipeline = (
        load_final_pipeline_result(video, config)
        if pipeline_cache_allowed
        else None
    )

    if cached_pipeline is not None:
        cached_subtitle = str(cached_pipeline.get("subtitle") or "").strip()
        subtitle = Path(cached_subtitle) if cached_subtitle else None
        raw_subtitle_id = cached_pipeline.get("subtitle_id")
        subtitle_id = int(raw_subtitle_id) if raw_subtitle_id is not None else None
        already_synced = True
        pipeline_cache_hit = True
        selected_source = "pipeline_cache"
        generated_by_ocr = str(cached_pipeline.get("source") or "").casefold() == "ocr"
        print(f"Готовый результат из кэша: {subtitle or f'встроенный sid={subtitle_id}'}")
        logger.info(
            "RESULT step=pipeline.final_cache video=%s cache=hit subtitle=%s embedded_sid=%s",
            video.name,
            subtitle or "",
            subtitle_id,
        )
        pipeline_timer.mark("final_cache", cache="hit")
    elif args.sub:
        subtitle = _validate_explicit_subtitle(args.sub)
        selected_source = "manual"
        if args.fast_play:
            already_synced = True
            print(f"Подготовленные субтитры: {subtitle}")
    elif args.embedded_sid is not None:
        subtitle_id = int(args.embedded_sid)
        selected_source = "embedded"
        print(f"Подготовленная встроенная японская дорожка: sid={subtitle_id}")
    elif not args.force_search:
        try:
            embedded_candidates = find_embedded_japanese_subtitles(
                video,
                config.tools.ffprobe,
                config.tools.ffmpeg,
                verbose=args.verbose,
            )
        except MediaProbeError as exc:
            print(exc)
            if args.prepare_only:
                print("PREPARE_STATUS=waiting_video")
                print(f"Video container is not readable yet: {exc}", file=sys.stderr)
                return 4
            embedded_candidates = []

        embedded_text = next(
            (candidate for candidate in embedded_candidates if candidate.codec in TEXT_CODECS),
            None,
        )
        embedded_bitmap_fallback = next(
            (candidate for candidate in embedded_candidates if candidate.codec not in TEXT_CODECS),
            None,
        )

        if embedded_text is not None:
            subtitle_id = embedded_text.subtitle_id
            selected_source = "embedded"
            print(
                f"Встроенные японские текстовые субтитры: sid={embedded_text.subtitle_id}, "
                f"codec={embedded_text.codec}, lang={embedded_text.language or '-'}, "
                f"title={embedded_text.title or '-'}"
            )
        elif embedded_bitmap_fallback is not None:
            print(
                f"Найдены только встроенные японские графические субтитры: "
                f"sid={embedded_bitmap_fallback.subtitle_id}, "
                f"codec={embedded_bitmap_fallback.codec}. "
                "Сначала ищу текстовый вариант локально и на Jimaku…"
            )

    if not pipeline_cache_hit:
        pipeline_timer.mark("embedded_probe")

    if subtitle is None and subtitle_id is None and not args.fast_play:
        candidates: list[SubtitleCandidate] = []
        local_candidates = find_local_subtitles(
            video=video,
            identity=identity,
            subtitle_dirs=config.paths.subtitle_dirs,
            cache_dir=config.paths.cache_dir,
            max_files=config.paths.max_scanned_files,
            prefer_srt=config.matching.prefer_srt,
            verbose=args.verbose,
        )
        candidates.extend(
            candidate
            for candidate in local_candidates
            if candidate.score >= config.matching.local_min_score
        )

        # When full comparison is enabled, a merely acceptable local subtitle
        # should not block a substantially better Jimaku release.
        if not args.offline and (config.matching.evaluate_all_jimaku or not candidates):
            reporter.update(SubtitleJobStage.DOWNLOADING_CANDIDATES, video=video.name)
            candidates.extend(
                _find_online_subtitles(
                    video,
                    identity,
                    config,
                    llm,
                    args.verbose,
                    anime_hint=anime_hint or tracking_anime,
                    skip_airing_lookup=args.skip_airing_lookup,
                )
            )

        original_candidate_count = len(candidates)
        candidates, content_duplicates = _deduplicate_subtitle_candidates(
            candidates,
            config.paths.cache_dir,
            ffmpeg_path=config.tools.ffmpeg,
        )
        reporter.update(
            SubtitleJobStage.NORMALIZING,
            video=video.name,
            candidates=len(candidates),
        )
        logger.info(
            "RESULT step=subtitle.deduplicate video=%s before=%s after=%s removed=%s",
            video.name,
            original_candidate_count,
            len(candidates),
            content_duplicates,
        )
        text_candidates = [
            candidate
            for candidate in candidates
            if candidate.path.suffix.casefold() in TEXT_SUBTITLE_EXTENSIONS
        ]
        bitmap_candidates = [
            candidate
            for candidate in candidates
            if candidate.path.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS | {".pgs"}
        ]
        if text_candidates:
            # Text subtitles always win, even when a bitmap release has a
            # numerically higher filename/source score. Keep one bitmap only as
            # a final fallback if every text candidate fails validation.
            bitmap_candidate_fallback = _choose_with_optional_llm(
                bitmap_candidates,
                identity,
                llm,
                config.llm.ambiguity_margin,
                allow_llm=False,
            )
            candidates = text_candidates
            logger.info(
                "RESULT step=subtitle.prefer_text video=%s text=%s bitmap_deferred=%s",
                video.name,
                len(text_candidates),
                len(bitmap_candidates),
            )
        elif bitmap_candidates:
            candidates = bitmap_candidates
        if content_duplicates and args.verbose:
            print(f"  Удалены дубликаты SRT/ASS по содержимому: {content_duplicates}")
        pipeline_timer.mark(
            "candidate_discovery",
            candidates=len(candidates),
            duplicates=content_duplicates,
        )

        if (
            config.matching.evaluate_all_jimaku
            and len(candidates) > 1
            and config.sync.enabled
            and not args.no_sync
        ):
            reporter.update(
                SubtitleJobStage.ALIGNING,
                video=video.name,
                candidates=len(candidates),
            )
            print(f"Сравниваю тайминг {len(candidates)} доступных вариантов…")
            best, optimized_path, result = optimize_candidates(
                video,
                candidates,
                config.paths.cache_dir,
                config.sync,
                ffmpeg_path=config.tools.ffmpeg,
                ffprobe_path=config.tools.ffprobe,
                alass_path=config.tools.alass,
                force=args.resync,
                verbose=True,
                prefer_srt=config.matching.prefer_srt,
                srt_tolerance_ratio=config.matching.srt_alignment_tolerance_ratio,
                srt_tolerance_absolute=config.matching.srt_alignment_tolerance_absolute,
                llm=llm,
                validate_embedded_reference_with_llm=config.llm.validate_embedded_reference,
            )
            alignment_result = dict(result)
            reporter.update(SubtitleJobStage.VALIDATING, video=video.name)
            if best is not None and optimized_path is not None:
                selected_candidate = best
                selected_source = best.source
                subtitle = optimized_path
                already_synced = True
                print(f"Выбран вариант: {best.name} ({best.source})")
                if best.source == "jimaku":
                    print(
                        f"Jimaku entry: {best.details.get('entry_name', '-')} "
                        f"(id={best.details.get('entry_id', '-')})"
                    )
                _print_sync_result(subtitle, result)
            elif result.get("candidate_quality_accepted") is False:
                print(
                    "Субтитры отклонены проверкой качества: "
                    f"{result.get('candidate_quality_reason', 'низкая уверенность')}"
                )
            if subtitle is None and candidates and all(
                candidate.path.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS | {".pgs"}
                for candidate in candidates
            ):
                chosen = _choose_with_optional_llm(
                    candidates,
                    identity,
                    llm,
                    config.llm.ambiguity_margin,
                    allow_llm=False,
                )
                if chosen is not None:
                    selected_candidate = chosen
                    selected_source = chosen.source
                    subtitle = chosen.path
                    print(f"Графические субтитры-кандидат: {subtitle}")
        else:
            chosen = _choose_with_optional_llm(
                candidates,
                identity,
                llm,
                config.llm.ambiguity_margin,
                allow_llm=False,
            )
            if chosen is not None:
                selected_candidate = chosen
                selected_source = chosen.source
                subtitle = chosen.path
                if chosen.source == "jimaku":
                    print(
                        f"Jimaku entry: {chosen.details.get('entry_name', '-')} "
                        f"(id={chosen.details.get('entry_id', '-')})"
                    )
                    print(f"Скачаны субтитры: {subtitle}")
                else:
                    print(f"Локальные субтитры: {subtitle} (score={chosen.score:.1f})")

        pipeline_timer.mark("candidate_selection", selected=subtitle or "")

    if subtitle is not None and not args.no_sync and not already_synced:
        reporter.update(SubtitleJobStage.ALIGNING, video=video.name, candidates=1)
        print("Синхронизация: анализирую аудио и тайминги…")
        candidate_path = subtitle
        synchronized_path, result = _sync_one(video, subtitle, args, config, llm)
        alignment_result = dict(result)
        if selected_candidate is not None:
            alignment_result.setdefault(
                "candidate_context",
                {
                    "source": selected_candidate.source,
                    "filename_score": selected_candidate.score,
                    **selected_candidate.details,
                },
            )
        reporter.update(SubtitleJobStage.VALIDATING, video=video.name)
        _print_sync_result(synchronized_path, result)
        is_bitmap_candidate = candidate_path.suffix.casefold() in (
            IMAGE_SUBTITLE_EXTENSIONS | {".pgs"}
        )
        if is_bitmap_candidate:
            # OCR can still use the original PGS when optional onset-based
            # synchronization has no suitable embedded text reference.
            accepted = True
            subtitle = (
                synchronized_path
                if bool(result.get("sync_was_successful"))
                else candidate_path
            )
        else:
            accepted, quality_reason = subtitle_quality_accepted(result)
            subtitle = synchronized_path
            if not accepted:
                print(f"Субтитры {candidate_path.name} отклонены: {quality_reason}")
                subtitle = None
        pipeline_timer.mark("single_candidate_sync", accepted=accepted)

    if subtitle is None and bitmap_candidate_fallback is not None:
        selected_candidate = bitmap_candidate_fallback
        selected_source = bitmap_candidate_fallback.source
        subtitle = bitmap_candidate_fallback.path
        already_synced = False
        print(
            "Текстовые варианты не прошли проверку; "
            f"использую графический fallback: {subtitle}"
        )
        if not args.no_sync:
            reporter.update(SubtitleJobStage.ALIGNING, video=video.name, candidates=1)
            synchronized_path, result = _sync_one(video, subtitle, args, config, llm)
            alignment_result = dict(result)
            _print_sync_result(synchronized_path, result)
            if bool(result.get("sync_was_successful")):
                subtitle = synchronized_path

    if config.matching.ocr_image_subtitles and not args.fast_play:
        image_path = (
            subtitle
            if subtitle is not None
            and subtitle.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS | {".pgs"}
            else None
        )
        embedded_stream_index = (
            embedded_bitmap_fallback.stream_index
            if image_path is None
            and subtitle is None
            and subtitle_id is None
            and embedded_bitmap_fallback is not None
            else None
        )
        if image_path is not None or embedded_stream_index is not None:
            print("OCR: преобразую графические японские субтитры в SRT заранее…")
            try:
                ocr_subtitle, ocr_result = image_subtitle_to_srt(
                    video,
                    config.paths.cache_dir,
                    subtitle_path=image_path,
                    embedded_stream_index=embedded_stream_index,
                    embedded_codec=(
                        embedded_bitmap_fallback.codec
                        if embedded_stream_index is not None
                        and embedded_bitmap_fallback is not None
                        else None
                    ),
                    ffmpeg_path=config.tools.ffmpeg,
                    force=args.resync,
                )
            except (OCRUnavailableError, OCRConversionError, OSError, ValueError) as exc:
                ocr_subtitle = None
                ocr_result = {"reason": "ocr_error", "error": str(exc)}
            logger.info(
                "RESULT step=subtitle.ocr_prepare video=%s source=%s embedded_stream=%s result=%s",
                video.name,
                image_path or "",
                embedded_stream_index,
                ocr_result,
            )
            ocr_quality = ocr_result.get("quality", {}) if isinstance(ocr_result, dict) else {}
            ocr_accepted = bool(ocr_quality.get("accepted", True))
            if ocr_subtitle is not None and ocr_accepted:
                generated_by_ocr = True
                ocr_source_path = image_path
                ocr_source_embedded_stream_index = embedded_stream_index
                subtitle = ocr_subtitle
                subtitle_id = None
                embedded_bitmap_fallback = None
                already_synced = True
                print(
                    f"OCR завершён: {ocr_subtitle} "
                    f"({ocr_result.get('cue_count', 0)} реплик)"
                )
                pipeline_timer.mark(
                    "subtitle_ocr",
                    reason=ocr_result.get("reason"),
                    cues=ocr_result.get("cue_count", 0),
                )
            else:
                reason = ocr_result.get('error', ocr_result.get('reason', 'unknown'))
                if ocr_subtitle is not None and not ocr_accepted:
                    reason = "quality_review:" + ",".join(ocr_quality.get("warnings", []))
                print("OCR не создал надёжные текстовые субтитры: " + str(reason))

    if (
        subtitle is not None
        and not args.fast_play
        and config.matching.convert_ass_to_srt
        and subtitle.suffix.casefold() in {".ass", ".ssa"}
    ):
        converted, conversion_result = convert_to_plain_srt(
            subtitle,
            config.paths.cache_dir,
            ffmpeg_path=config.tools.ffmpeg,
            force=args.resync,
            verbose=args.verbose,
        )
        if converted != subtitle:
            subtitle = converted
            print(f"ASS/SSA преобразован в обычный SRT: {subtitle}")
        else:
            print(
                "Не удалось преобразовать ASS/SSA в SRT: "
                f"{conversion_result.get('error', conversion_result.get('reason'))}"
            )
        pipeline_timer.mark("subtitle_conversion", reason=conversion_result.get("reason"))

    if subtitle is not None and subtitle.suffix.casefold() == ".srt":
        reporter.update(SubtitleJobStage.SELECTING, video=video.name)
        source_subtitle = subtitle
        cleaned_subtitle, cleanup_result = clean_srt_for_playback(
            subtitle,
            config.paths.cache_dir,
            force=args.resync,
        )
        if cleaned_subtitle != subtitle:
            subtitle = cleaned_subtitle
            if not args.fast_play:
                print(f"SRT очищен для mpv: {subtitle}")
        elif args.verbose and cleanup_result.get("reason") not in {"already_clean", "cached"}:
            print(f"Очистка SRT пропущена: {cleanup_result.get('reason')}")
        configure_logging().info(
            "RESULT step=subtitle.playback_clean source=%s output=%s reason=%s cues=%s conflicts=%s",
            source_subtitle,
            subtitle,
            cleanup_result.get("reason"),
            cleanup_result.get("cue_count"),
            cleanup_result.get("conflict_count"),
        )
        pipeline_timer.mark(
            "playback_clean",
            reason=cleanup_result.get("reason"),
            conflicts=cleanup_result.get("conflict_count"),
        )

    if subtitle is None and subtitle_id is None and embedded_bitmap_fallback is not None:
        selected_source = "embedded_bitmap"
        subtitle_id = embedded_bitmap_fallback.subtitle_id
        print(
            "Текстовые японские субтитры не найдены; "
            f"использую встроенную графическую дорожку sid={subtitle_id}, "
            f"codec={embedded_bitmap_fallback.codec}"
        )

    if subtitle is None and subtitle_id is None:
        if args.prepare_only:
            reporter.update(SubtitleJobStage.WAITING_SOURCE, video=video.name)
            print("Японские субтитры пока не найдены")
            print("PREPARE_STATUS=waiting_subtitles")
            return 4
        print("Японские субтитры не найдены; mpv будет запущен без добавленного файла")

    selected_image_subtitle = bool(
        (subtitle is not None and subtitle.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS)
        or (
            subtitle is None
            and embedded_bitmap_fallback is not None
            and subtitle_id == embedded_bitmap_fallback.subtitle_id
        )
    )

    if (
        (subtitle is not None or subtitle_id is not None)
        and not selected_image_subtitle
        and args.sub is None
        and args.embedded_sid is None
        and not args.force_search
        and not args.fast_play
        and not pipeline_cache_hit
    ):
        manifest = save_final_pipeline_result(
            video,
            config,
            subtitle=subtitle,
            subtitle_id=subtitle_id,
            dependency=subtitle,
            source=("ocr" if generated_by_ocr else "external" if subtitle is not None else "embedded"),
        )
        logger.info(
            "RESULT step=pipeline.final_cache video=%s cache=write manifest=%s subtitle=%s embedded_sid=%s",
            video.name,
            manifest,
            subtitle or "",
            subtitle_id,
        )
        pipeline_timer.mark("final_cache", cache="write")

    if args.prepare_only:
        quality_input = dict(alignment_result)
        if subtitle_id is not None and not quality_input:
            quality_input = {
                "sync_was_successful": True,
                "reference_alignment_reliable": True,
                "engine": "embedded-exact",
                "reason": "embedded_exact_video_clock",
            }
        quality_accepted = bool(
            not selected_image_subtitle
            and (subtitle is not None or subtitle_id is not None)
        )
        quality_reason = str(
            quality_input.get("candidate_quality_reason")
            or quality_input.get("reason")
            or ("embedded exact video clock" if subtitle_id is not None else "prepared")
        )
        quality = quality_from_result(
            quality_input,
            accepted=quality_accepted,
            reason=quality_reason,
        )
        metadata = {
            "source": selected_source or (selected_candidate.source if selected_candidate else ""),
            "name": selected_candidate.name if selected_candidate else (subtitle.name if subtitle else "embedded" if subtitle_id is not None else ""),
            # The public score is final prepared quality. Filename ranking is
            # retained only as discovery evidence and never drives upgrades.
            "score": quality.score,
            "filename_score": selected_candidate.score if selected_candidate else None,
            "candidate_path": str(selected_candidate.path) if selected_candidate else str(subtitle or ""),
            "final_path": str(subtitle or ""),
            "embedded_sid": subtitle_id,
            "details": selected_candidate.details if selected_candidate else {},
            "generated_by_ocr": bool(generated_by_ocr),
            "ocr_source_path": str(ocr_source_path or ""),
            "ocr_source_embedded_stream_index": ocr_source_embedded_stream_index,
            "alignment": quality_input,
            "quality": quality.as_dict(),
        }
        print("PREPARED_SUBTITLE_META=" + json.dumps(metadata, ensure_ascii=False, default=str))
        if selected_image_subtitle:
            reporter.update(SubtitleJobStage.WAITING_SOURCE, video=video.name, bitmap=True)
            # Bitmap PGS/SUP is a Library-only fallback, never a completed
            # background preparation result. Remove a manifest written by an
            # older release so future retries continue searching for text.
            invalidate_final_pipeline_result(video, config)
            if subtitle is not None:
                print(f"PREPARED_SUBTITLE={subtitle}")
            elif embedded_bitmap_fallback is not None:
                print(
                    f"PREPARED_EMBEDDED_SID="
                    f"{subtitle_id or embedded_bitmap_fallback.subtitle_id}"
                )
            print("PREPARE_STATUS=waiting_text_subtitles")
            return 4
        print(f"PREPARED_SUBTITLE={subtitle or ''}")
        if subtitle_id is not None:
            print(f"PREPARED_EMBEDDED_SID={subtitle_id}")
        print("PREPARE_STATUS=ready")
        reporter.update(
            SubtitleJobStage.READY,
            video=video.name,
            confidence=quality.confidence.value,
            quality=quality.score,
        )
        return 0

    tracking_file: Path | None = None
    tracker_script: Path | None = Path(
        str(files("pudge").joinpath("mpv_scripts/pudge_anilist.lua"))
    )
    tracker_env: dict[str, str] = {
        "PUDGE_PYTHON": sys.executable,
        "PUDGE_CONFIG": str(config.config_path),
        "PUDGE_UI_LANGUAGE": config.ui.language,
        "PUDGE_APP_NAME": APP_NAME,
        "PUDGE_APP_CLI": APP_CLI,
        "PUDGE_PLAYBACK_ENABLED": "1" if config.playback.enabled else "0",
        "PUDGE_PLAYBACK_VIDEO": str(video),
        "PUDGE_PLAYBACK_INTERVAL": str(config.playback.save_interval_seconds),
        "PUDGE_SHORTCUT_MARK_WATCHED": config.shortcuts.mpv_mark_watched,
        "PUDGE_SHORTCUT_OPEN_ANILIST": config.shortcuts.mpv_open_anilist,
        "PUDGE_SHORTCUT_CORRECT_MATCH": config.shortcuts.mpv_correct_match,
        "PUDGE_SHORTCUT_TRANSLATE_SUBTITLE": config.shortcuts.mpv_translate_subtitle,
        "PUDGE_SUBTITLE_PATH": str(subtitle or ""),
    }
    if tracking_anime is not None and tracking_episode is not None and config.anilist.access_token:
        threshold = max(0.1, min(0.99, config.anilist.watched_threshold))
        title = tracking_anime.titles[0] if tracking_anime.titles else str(tracking_anime.id)
        tracking_file = create_tracking_file(
            config.paths.cache_dir,
            TrackingPayload(
                video=str(video),
                title=title,
                media_id=tracking_anime.id,
                episode=tracking_episode,
                total_episodes=tracking_anime.episodes,
                threshold=threshold,
                mapping_key=tracking_mapping_key,
            ),
        )
        tracker_env.update({
            "PUDGE_ANILIST_TRACKING_FILE": str(tracking_file),
            "PUDGE_ANILIST_THRESHOLD": str(threshold),
            "PUDGE_ANILIST_MAX_REMAINING_MINUTES": str(max(0.0, config.anilist.watched_max_remaining_minutes)),
            "PUDGE_ANILIST_MEDIA_ID": str(tracking_anime.id),
            "PUDGE_ANILIST_TITLE": title,
            "PUDGE_ANILIST_AUTO_UPDATE": "1" if tracking_auto else "0",
            "PUDGE_PYTHON": sys.executable,
            "PUDGE_CONFIG": str(config.config_path),
        })

    mpv_extra_args = list(config.tools.mpv_extra_args)
    try:
        from .first_experience import mpv_study_script_plan
        from .light_novels import LightNovelService

        study_settings = LightNovelService(config).settings()
        study_plan = mpv_study_script_plan(
            config.tools.mpv_study_plugin,
            jiten_api_key=study_settings.jiten_api_key,
            jpdb_api_token=study_settings.jpdb_api_token,
        )
        if study_plan["exclusive"]:
            # Explicit loading keeps all ordinary user scripts while ensuring
            # JitenMPV and jpdb-mpv-plugin never run together in Pudge's mpv.
            mpv_extra_args.append("--load-scripts=no")
            mpv_extra_args.extend(
                f"--script={path}" for path in study_plan["scripts"]
            )
    except (OSError, ValueError):
        # Playback remains available if a third-party plugin is being changed
        # on disk while the command is constructed.
        pass
    if args.start_at and args.start_at > 0:
        mpv_extra_args.append(f"--start={max(0.0, float(args.start_at)):.3f}")
    if args.fullscreen and not any(arg in {"--fs", "--fullscreen"} or arg.startswith("--fs=") for arg in mpv_extra_args):
        mpv_extra_args.append("--fs")

    command = build_mpv_command(
        config.tools.mpv,
        video,
        subtitle,
        subtitle_id,
        mpv_extra_args,
        script=tracker_script,
    )
    pipeline_timer.mark("ready_to_launch", subtitle=subtitle or "", embedded_sid=subtitle_id)
    # Keep the foreground marker alive for the full mpv lifetime. Background
    # subtitle workers use it as a preemption signal; clearing it here allowed
    # the scheduled agent to start ffmpeg/ffsubsync while the user was watching.
    configure_logging().info(
        "EVENT mpv.launch video=%s subtitle=%s embedded_sid=%s sub_fix_timing=%s",
        video,
        subtitle or "",
        subtitle_id,
        next((arg for arg in command if arg.startswith("--sub-fix-timing")), ""),
    )
    try:
        return run_mpv(
            command,
            dry_run=args.dry_run,
            env_overrides=tracker_env,
            focus=args.fullscreen,
        )
    finally:
        if tracking_file is not None and not args.dry_run:
            try:
                tracking_file.unlink(missing_ok=True)
            except OSError:
                pass


def doctor(config: AppConfig) -> int:
    print(f"Config: {config.config_path} ({'ok' if config.config_path.exists() else 'missing'})")
    status = 0
    for label, command, version_args in (
        ("mpv", config.tools.mpv, ["--version"]),
        ("ffmpeg", config.tools.ffmpeg, ["-version"]),
        ("ffprobe", config.tools.ffprobe, ["-version"]),
    ):
        resolved = shutil.which(command) if "/" not in command else command
        ok = False
        detail = "missing"
        if resolved and Path(resolved).is_file():
            try:
                completed = subprocess.run(
                    [resolved, *version_args],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                    check=False,
                )
                ok = completed.returncode == 0
                detail = "ok" if ok else f"broken, exit={completed.returncode}"
            except (OSError, subprocess.TimeoutExpired) as exc:
                detail = f"broken: {exc}"
        print(f"{label}: {resolved or command} ({detail})")
        status |= 0 if ok else 1

    try:
        import ffsubsync  # noqa: F401
        print("ffsubsync: ok")
    except ImportError:
        print("ffsubsync: missing")
        status |= 1

    alass = shutil.which(config.tools.alass) if "/" not in config.tools.alass else config.tools.alass
    if not alass and config.tools.alass in {"alass", "alass-cli"}:
        alass = shutil.which("alass-cli") or shutil.which("alass")
    print(f"alass: {alass or config.tools.alass} ({'ok' if alass else 'missing; auto fallback to ffsubsync'})")

    sevenzip = find_7zip()
    print(f"7-Zip: {sevenzip or 'missing'} ({'ok' if sevenzip else 'missing; .7z/.rar Jimaku archives unavailable'})")

    print(f"Jimaku API key: {'set' if config.jimaku.api_key else 'missing'}")
    print(
        "AniList: "
        f"enabled={'yes' if config.anilist.enabled else 'no'}, "
        f"token={'set' if config.anilist.access_token else 'missing'}, "
        f"auto_update={'yes' if config.anilist.auto_update_progress else 'no'}, "
        f"threshold={config.anilist.watched_threshold * 100:.1f}%"
    )
    print(f"Library: {config.library.root_dir} ({'ok' if config.library.root_dir.exists() else 'missing'})")
    try:
        from .database import Database
        Database(config.library.database_path)
        print(f"Database: {config.library.database_path} (ok)")
    except Exception as exc:
        print(f"Database: {config.library.database_path} (broken: {exc})")
        status |= 1
    print(
        "Nyaa: "
        f"enabled={'yes' if config.nyaa.enabled else 'no'}, "
        f"mode={config.nyaa.proxy_mode}, "
        f"proxy={'set' if config.nyaa.proxy_url else 'empty'}, "
        f"auto_download={'yes' if config.nyaa.auto_download_current else 'no'}"
    )
    print(
        "Agent: "
        f"enabled={'yes' if config.agent.enabled else 'no'}, "
        f"torrent_poll={config.agent.poll_minutes}m, "
        f"anilist_refresh={config.agent.anilist_refresh_minutes}m, "
        f"subtitle_poll={config.agent.subtitle_poll_minutes}m, "
        f"delete_after={config.agent.delete_after_watched_hours}h"
    )
    if config.qbittorrent.enabled or config.aria2.enabled:
        try:
            from .manager import AnimeManager
            manager = AnimeManager(config)
            client = manager.qbt_client()
            try:
                torrent_version = client.version()
            finally:
                client.close()
            print(f"Torrent backend: {manager.torrent_backend_name()} ({torrent_version})")
        except Exception as exc:
            print(f"Torrent backend: unavailable ({exc})")
            status |= 1
    else:
        print("Torrent backend: disabled")
    if config.llm.enabled:
        candidate = OllamaClient(config.llm, config.paths.cache_dir)
        try:
            print(f"LLM: {'ok' if candidate.available() else 'unavailable'} ({config.llm.model})")
        finally:
            candidate.close()
    else:
        print("LLM: disabled")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.init_config:
        path = write_default_config(args.config)
        print(path)
        return 0

    config = load_config(args.config)
    if args.subtitle_prewarm_file:
        from .mpv_study import SubtitleStudyApi

        try:
            if hasattr(os, "nice"):
                try:
                    os.nice(10)
                except OSError:
                    pass
            result = SubtitleStudyApi(
                config, media_id=args.subtitle_study_media_id
            ).prewarm_file(
                args.subtitle_prewarm_file,
                start_seconds=args.subtitle_prewarm_from,
            )
        except Exception as exc:
            print(f"Subtitle translation prewarm failed: {exc}", file=sys.stderr)
            return 1
        print("PUDGE_TRANSLATION_PREWARM_JSON=" + json.dumps(result, ensure_ascii=False))
        return 0
    if args.subtitle_translate:
        from .mpv_study import SubtitleStudyApi

        try:
            result = SubtitleStudyApi(
                config, media_id=args.subtitle_study_media_id
            ).translate(args.subtitle_study_text, args.subtitle_study_context)
        except Exception as exc:
            print(f"Subtitle translation failed: {exc}", file=sys.stderr)
            return 1
        print("PUDGE_TRANSLATION_JSON=" + json.dumps(result, ensure_ascii=False))
        return 0
    if args.playback_save:
        return _run_playback_save(args, config)
    if args.app:
        from .app_ui import launch_app
        return launch_app(args.config)
    if args.agent_once:
        from .manager import AnimeManager
        stats = AnimeManager(config).run_once()
        print(", ".join(f"{key}={value}" for key, value in stats.items()))
        return 0
    if args.anilist_action:
        return _run_anilist_action(args, config)
    if args.anilist_correct:
        if len(args.videos) != 1:
            parser.error("для --anilist-correct укажите ровно один видео-файл")
        return _correct_anilist_mapping(args.videos[0], args.anilist_correct, config)
    if args.settings:
        from .settings_ui import launch_settings
        return launch_settings(args.config)
    if args.doctor:
        return doctor(config)
    if not args.videos:
        from .app_ui import launch_app
        return launch_app(args.config)

    background_prepare = os.environ.get("PUDGE_BACKGROUND_PREPARE") == "1"
    # A manual ``--prepare-only`` is foreground work too: the user is waiting
    # for this exact episode. Only manager-spawned workers carry the explicit
    # background marker and are allowed to run without claiming priority.
    if not background_prepare:
        mark_foreground(config.paths.cache_dir, video=args.videos[0].expanduser())

    llm: OllamaClient | None = None
    all_videos_have_final_cache = bool(args.videos) and all(
        final_pipeline_cache_available(video.expanduser().resolve(), config)
        for video in args.videos
    )
    cache_shortcut_allowed = (
        not args.resync
        and args.sub is None
        and args.embedded_sid is None
        and not args.force_search
        and not args.fast_play
    )
    need_llm = (
        not args.fast_play
        and config.llm.enabled
        and bool(config.llm.model)
        and not (cache_shortcut_allowed and all_videos_have_final_cache)
    )
    if need_llm:
        candidate = OllamaClient(config.llm, config.paths.cache_dir)
        if candidate.available():
            llm = candidate
        else:
            candidate.close()
            print("LLM включена, но API или выбранная модель недоступны; продолжаю без LLM")
    elif cache_shortcut_allowed and all_videos_have_final_cache and config.llm.enabled:
        configure_logging().info("SKIP step=llm.available reason=all_videos_final_cache")

    exit_code = 0
    try:
        for video in args.videos:
            try:
                exit_code = process_video(video, args, config, llm)
            except (RuntimeError, JimakuError, KeyboardInterrupt) as exc:
                print(f"Ошибка: {exc}", file=sys.stderr)
                exit_code = 1
            if exit_code != 0:
                break
    finally:
        clear_foreground(config.paths.cache_dir, pid=os.getpid())
        if llm is not None:
            llm.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
