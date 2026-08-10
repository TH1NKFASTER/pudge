from __future__ import annotations

from pathlib import Path
from typing import Callable
import re

from rapidfuzz import fuzz

from .config import AppConfig
from .database import Database
from .filename import parse_anime_filename, title_similarity
from .manager_models import LibraryAnime, LibraryEpisode
from .models import VideoIdentity
from .language import IMAGE_SUBTITLE_EXTENSIONS
from .pipeline_cache import load_final_pipeline_result
from .media import (
    TEXT_CODECS,
    MediaProbeError,
    find_embedded_japanese_subtitles,
)


VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".mts",
    ".wmv", ".flv", ".ogv", ".mpeg", ".mpg", ".mpv", ".3gp",
}
SUB_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".sup")


def strict_title_similarity(title: str, names: list[str]) -> float:
    """Literal title score for arbitrary local files.

    Unlike ``title_similarity`` this deliberately avoids WRatio/partial matching,
    which can map unrelated short filenames (for example ``catmahjong``) to a
    short synonym such as ``Mahoyo``.
    """
    query = re.sub(r"[^\w]+", " ", str(title).casefold(), flags=re.UNICODE).strip()
    if len(query.replace(" ", "")) < 4:
        return 0.0
    best = 0.0
    for raw in names:
        name = re.sub(r"[^\w]+", " ", str(raw).casefold(), flags=re.UNICODE).strip()
        if not name:
            continue
        best = max(best, float(fuzz.ratio(query, name)), float(fuzz.token_sort_ratio(query, name)))
    return best


def confident_local_match(identity: VideoIdentity, anime: LibraryAnime) -> bool:
    names = [anime.title, *anime.titles, *anime.synonyms]
    threshold = 72.0 if identity.episode is not None else 86.0
    return strict_title_similarity(identity.title, names) >= threshold


def match_anime(title: str, anime_list: list[LibraryAnime]) -> LibraryAnime | None:
    best: LibraryAnime | None = None
    best_score = 0.0
    for anime in anime_list:
        names = [anime.title, *anime.titles, *anime.synonyms]
        score = max((title_similarity(title, name) for name in names if name), default=0.0)
        if score > best_score:
            best = anime
            best_score = score
    return best if best_score >= 58 else None


def parent_folder_anime(video_path: Path, root: Path, anime_list: list[LibraryAnime]) -> LibraryAnime | None:
    """Resolve a managed download by its containing anime folder.

    Files added from Planning are saved under ``root/<AniList title>/...``.
    Release filenames are often translated or scene-normalized, so the parent
    folder is stronger identity evidence than fuzzy matching the release name.
    Only near-literal folder matches are accepted.
    """
    try:
        root_resolved = root.expanduser().resolve()
        current = video_path.expanduser().resolve().parent
    except (OSError, RuntimeError):
        return None
    while current != root_resolved and root_resolved in current.parents:
        folder_name = current.name.strip()
        if folder_name:
            best = None
            best_score = 0.0
            for anime in anime_list:
                names = [anime.title, *anime.titles, *anime.synonyms]
                score = strict_title_similarity(folder_name, names)
                if score > best_score:
                    best, best_score = anime, score
            if best is not None and best_score >= 92.0:
                return best
        current = current.parent
    return None


def sidecar_subtitle(video: Path) -> Path | None:
    for ext in SUB_EXTENSIONS:
        candidate = video.with_suffix(ext)
        if candidate.is_file():
            return candidate
    # Prepared files may live in a sibling `subs` directory.
    subs_dir = video.parent / "subs"
    if subs_dir.is_dir():
        for ext in SUB_EXTENSIONS:
            candidates = sorted(subs_dir.glob(f"{video.stem}*{ext}"))
            if candidates:
                return candidates[0]
    return None


def japanese_subtitle_details(
    video: Path,
    *,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
) -> tuple[str, Path | None, int | None]:
    """Return subtitle source, path and embedded mpv subtitle id.

    Text tracks are always preferred. Bitmap PGS/SUP tracks are reported with
    an explicit ``*_bitmap`` source so callers can keep them available for
    Library-only playback without marking the anime ready.
    """
    sidecar = sidecar_subtitle(video)
    if sidecar is not None:
        source = (
            "external_bitmap"
            if sidecar.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS
            else "external"
        )
        return source, sidecar.resolve(), None
    try:
        candidates = find_embedded_japanese_subtitles(
            video,
            ffprobe,
            ffmpeg,
            verbose=False,
        )
    except MediaProbeError:
        candidates = []
    embedded = next(
        (candidate for candidate in candidates if candidate.codec in TEXT_CODECS),
        None,
    )
    if embedded is not None:
        return "embedded", None, embedded.subtitle_id
    embedded_bitmap = next(iter(candidates), None)
    if embedded_bitmap is not None:
        return "embedded_bitmap", None, embedded_bitmap.subtitle_id
    return "none", None, None

def japanese_subtitle_source(
    video: Path,
    *,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
) -> tuple[str, Path | None]:
    source, path, _subtitle_id = japanese_subtitle_details(
        video,
        ffprobe=ffprobe,
        ffmpeg=ffmpeg,
    )
    return source, path


def cached_anilist_id_for_video(video_path: Path, root: Path) -> int | None:
    """Read the nearest optional ``.anilist.id`` sidecar.

    The sidecar is useful for manually organized folders whose filenames are
    ambiguous. It is only a mapping hint; the AniList entry must already exist
    in the local database.
    """
    root_resolved = root.expanduser().resolve()
    current = video_path.expanduser().resolve().parent
    while True:
        marker = current / ".anilist.id"
        if marker.is_file():
            try:
                value = int(marker.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                return None
            return value if value > 0 else None
        if current == root_resolved or root_resolved not in current.parents:
            break
        current = current.parent
    return None


def scan_library(
    root: Path,
    db: Database,
    *,
    recursive: bool = True,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
    excluded_paths: tuple[Path, ...] = (),
    pipeline_cache_config: AppConfig | None = None,
    anime_resolver: Callable[[VideoIdentity], LibraryAnime | None] | None = None,
    require_anime_match: bool = False,
) -> list[LibraryEpisode]:
    root = root.expanduser()
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    anime_list = db.anime_list()
    iterator = root.rglob("*") if recursive else root.glob("*")
    excluded = tuple(path.expanduser().resolve() for path in excluded_paths)

    def is_excluded(path: Path) -> bool:
        resolved = path.resolve()
        for blocked in excluded:
            if resolved == blocked or blocked in resolved.parents:
                return True
        return False

    result: list[LibraryEpisode] = []
    for path in iterator:
        if not path.is_file() or path.suffix.casefold() not in VIDEO_EXTENSIONS:
            continue
        if is_excluded(path):
            continue
        identity = parse_anime_filename(path)
        sidecar_anilist_id = cached_anilist_id_for_video(path, root)
        anime = db.get_anime(sidecar_anilist_id) if sidecar_anilist_id else None
        # External watched folders are much more ambiguous than the managed
        # library. Resolve the full filename identity (including Sxx) before the
        # legacy title-only matcher so Season 3 cannot silently attach to Season 1.
        if anime is None and anime_resolver is not None and require_anime_match:
            anime = anime_resolver(identity)
        # External watched folders must never fall back to the permissive legacy
        # matcher.  Its fuzzy WRatio threshold is useful for an explicitly managed
        # anime library, but short arbitrary filenames such as ``uma.mp4`` can
        # otherwise partially match titles such as ``Azumanga``.
        if anime is None and not require_anime_match:
            anime = parent_folder_anime(path, root, anime_list)
        if anime is None and not require_anime_match:
            anime = match_anime(identity.title, anime_list)
            # Managed-library filenames are generally trustworthy, but movie/OVA
            # files without an episode token are still vulnerable to partial fuzzy
            # matches. Require a close literal title match before attaching them.
            if anime is not None and identity.episode is None and not confident_local_match(identity, anime):
                anime = None
        if anime is None and anime_resolver is not None and not require_anime_match:
            anime = anime_resolver(identity)
        if anime is not None and db.get_anime(anime.media_id) is None:
            db.upsert_anime(anime)
            anime_list.append(anime)
        if anime is None and require_anime_match:
            continue
        resolved = path.resolve()
        existing = db.episode_by_path(resolved)
        sidecar = sidecar_subtitle(path)
        existing_download = (
            db.download_by_hash(existing.torrent_hash)
            if existing is not None and existing.torrent_hash
            else None
        )
        existing_has_managed_download = bool(
            existing_download is not None
            and existing is not None
            and existing_download.media_id == existing.media_id
        )
        if (
            not require_anime_match
            and anime is None
            and existing is not None
            and existing.media_id is not None
            and not existing_has_managed_download
            and existing.state != "watched"
            and not existing.watched_at
            and float(existing.playback_position or 0.0) <= 0.0
            and sidecar_anilist_id is None
        ):
            associated = db.get_anime(existing.media_id)
            if associated is not None and not confident_local_match(identity, associated):
                # v0.6.55 and older preserve media_id on UPSERT via COALESCE, so
                # merely scanning the file again cannot detach a historical false
                # match. Remove only the DB row; the video itself is untouched.
                db.delete_episode_record(resolved)
                existing = None

        existing_external_valid = False
        if existing is not None and existing.subtitle_path is not None:
            try:
                existing_external_valid = (
                    existing.subtitle_path.is_file()
                    and existing.subtitle_path.stat().st_size > 0
                )
            except OSError:
                existing_external_valid = False
        existing_is_bitmap = bool(
            existing is not None
            and (
                str(existing.subtitle_origin or "").casefold() == "bitmap"
                or (
                    existing.subtitle_path is not None
                    and existing.subtitle_path.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS
                )
            )
        )
        existing_selection_valid = bool(
            existing is not None
            and existing.state in {"ready", "watched", "waiting_text_subtitles"}
            and (existing_external_valid or existing.embedded_subtitle_id is not None)
        )
        if existing is not None and existing.state == "ready" and existing_is_bitmap:
            # Never preserve a historical bitmap row as Ready. Bitmap subtitles
            # are playable as a Library fallback but still wait for text unless
            # OCR explicitly produced a text result.
            existing.state = "waiting_text_subtitles"
        cached_pipeline = None
        if not existing_selection_valid and sidecar is None and pipeline_cache_config is not None:
            try:
                # Library recovery is allowed to reuse an old manifest forever as
                # long as its video/dependency snapshots and prepared file remain
                # valid. This repairs rows accidentally reset by older scans
                # without recomputing subtitle synchronization.
                cached_pipeline = load_final_pipeline_result(
                    resolved,
                    pipeline_cache_config,
                    ttl_seconds=0,
                )
            except OSError:
                cached_pipeline = None

        if existing_selection_valid and existing is not None:
            subtitle = existing.subtitle_path
            embedded_subtitle_id = existing.embedded_subtitle_id
            if existing.state == "waiting_text_subtitles":
                subtitle_source = (
                    "external_bitmap" if subtitle is not None else "embedded_bitmap"
                )
            else:
                subtitle_source = "external" if subtitle is not None else "embedded"
        elif sidecar is not None:
            subtitle = sidecar.resolve()
            embedded_subtitle_id = None
            subtitle_source = (
                "external_bitmap"
                if sidecar.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS
                else "external"
            )
        elif cached_pipeline is not None:
            cached_subtitle = str(cached_pipeline.get("subtitle") or "").strip()
            subtitle = Path(cached_subtitle).resolve() if cached_subtitle else None
            raw_subtitle_id = cached_pipeline.get("subtitle_id")
            embedded_subtitle_id = (
                int(raw_subtitle_id) if raw_subtitle_id is not None else None
            )
            subtitle_source = "external" if subtitle is not None else "embedded"
        elif (
            existing is not None
            and existing.state in {"local", "waiting_subtitles"}
            and existing.subtitle_path is None
            and existing.embedded_subtitle_id is None
        ):
            # This exact path was already probed and is queued for external
            # subtitle resolution. Re-running ffprobe/ffmpeg on every refresh
            # wastes energy and cannot change the result unless the file changes.
            subtitle_source, subtitle, embedded_subtitle_id = "none", None, None
        else:
            # Re-probe old embedded rows when there is no validated prepared
            # selection or final-pipeline manifest to preserve.
            subtitle_source, subtitle, embedded_subtitle_id = japanese_subtitle_details(
                path,
                ffprobe=ffprobe,
                ffmpeg=ffmpeg,
            )
        if existing is not None and existing.state == "watched":
            state = "watched"
        elif subtitle_source in {"external", "embedded"}:
            state = "ready"
        elif subtitle_source in {"external_bitmap", "embedded_bitmap"}:
            state = "waiting_text_subtitles"
        else:
            state = "local"
        if existing_selection_valid and existing is not None:
            subtitle_origin = existing.subtitle_origin
        elif cached_pipeline is not None:
            subtitle_origin = str(cached_pipeline.get("source") or "")
        elif subtitle_source in {"external_bitmap", "embedded_bitmap"}:
            subtitle_origin = "bitmap"
        else:
            subtitle_origin = subtitle_source if subtitle_source in {"external", "embedded"} else ""
        item = LibraryEpisode(
            media_id=anime.media_id if anime else None,
            title=anime.title if anime else identity.title,
            episode=identity.episode,
            video_path=resolved,
            subtitle_path=subtitle,
            embedded_subtitle_id=embedded_subtitle_id,
            state=state,
            subtitle_origin=subtitle_origin,
        )
        db.upsert_episode(item)
        persisted = db.episode_by_path(resolved) or item
        if persisted.state in {"local", "waiting_subtitles", "waiting_text_subtitles"}:
            db.ensure_subtitle_job(
                resolved,
                persisted.media_id,
                persisted.episode,
            )
        elif persisted.state in {"ready", "watched"}:
            # A scan must never leave a resolver job attached to a validated
            # episode. Older versions did this by checking the temporary scan
            # state instead of the row actually preserved by ``upsert_episode``.
            db.delete_subtitle_job(resolved)
        result.append(persisted)
    return result
