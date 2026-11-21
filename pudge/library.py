from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator
import re
import sys

from rapidfuzz import fuzz

from .config import AppConfig
from . import extra_files
from .database import EXTRA_FILE_ACTION_CODES, Database
from .filename import parse_anime_filename, title_similarity
from .manager_models import LibraryAnime, LibraryEpisode
from .models import VideoIdentity
from .language import (
    IMAGE_SUBTITLE_EXTENSIONS,
    TEXT_SUBTITLE_EXTENSIONS,
    has_japanese_marker,
    has_negative_language_marker,
    is_japanese_subtitle,
)
from .i18n import err
from .pipeline_cache import invalidate_final_pipeline_result, load_final_pipeline_result
from .release_parser import parse_release_name
from .media import (
    TEXT_CODECS,
    MediaProbeError,
    find_embedded_japanese_subtitles,
)


VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".mov",
    ".webm",
    ".ts",
    ".m2ts",
    ".mts",
    ".wmv",
    ".flv",
    ".ogv",
    ".mpeg",
    ".mpg",
    ".mpv",
    ".3gp",
}
SUB_EXTENSIONS = (".srt", ".ass", ".ssa", ".vtt", ".sup")


_LOGGER = logging.getLogger(__name__)
# Per scan: a failure for every file (a locked database) logs a few lines and a count.
_SCAN_FILE_FAILURES_LOGGED = 20


def aria2_control_file(video: Path, root: Path | None = None) -> Path | None:
    """aria2's ``<name>.aria2`` next to the file or next to a folder above it (up to ``root``)."""
    current = Path(video).expanduser()
    stop = Path(root).expanduser() if root is not None else None
    # A torrent's top folder sits at most a few levels below its save folder.
    for _level in range(6):
        if (stop is not None and current == stop) or not current.name:
            return None  # the root folder ("/") has no name to put ".aria2" after
        control = current.with_name(current.name + ".aria2")
        try:
            if control.is_file():
                return control
        except OSError:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def aria2_control_infohash(control: Path) -> str | None:
    """The BitTorrent info hash an aria2 control file belongs to, or None (unreadable, not a torrent).

    Layout: version (2 bytes: 0 = host byte order, 1 = big endian), extension
    (4), info hash length (4), info hash.
    """
    try:
        with open(control, "rb") as handle:
            head = handle.read(10 + 64)
    except OSError:
        return None
    if len(head) < 10:
        return None
    version = int.from_bytes(head[:2], "big")
    if version not in (0, 1):
        return None
    order = "big" if version == 1 else sys.byteorder
    length = int.from_bytes(head[6:10], order)
    if length not in (20, 32) or len(head) < 10 + length:
        return None
    return head[10 : 10 + length].hex()


def unfinished_video(
    video: Path,
    root: Path | None = None,
    *,
    finished_torrent: Callable[[str], bool] | None = None,
) -> bool:
    """A 0-byte file, or one aria2 has not finished: not a local episode.

    A race candidate removed before any data left its empty files and control
    file behind, and every scan listed them as episodes whose subtitles then
    failed for a day each ("Video container is not readable yet").

    aria2 keeps a torrent's control file while it seeds (or is paused after
    finishing): ``finished_torrent(info_hash)`` tells a finished download's
    control file from an unfinished one, so the files of a finished pack stay
    episodes.
    """
    try:
        if Path(video).stat().st_size == 0:
            return True
    except OSError:
        return False
    control = aria2_control_file(video, root)
    if control is None:
        return False
    if finished_torrent is not None:
        info_hash = aria2_control_infohash(control)
        if info_hash and finished_torrent(info_hash):
            return False
    return True


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


_SEASON_FORMATS = {"", "TV", "TV_SHORT", "ONA"}


def _season_chain(db: Database | None, anime: LibraryAnime) -> list[tuple[int, str, int]]:
    """The franchise's season entries around ``anime``: (media_id, title, offset).

    Offsets follow SEQUEL (+1) and PREQUEL (-1) links between TV/ONA entries, from the
    cached relation graph, else from the entry's own relations; ``anime`` is offset 0.
    """
    graph = None
    if db is not None and hasattr(db, "relation_graph_for_media"):
        try:
            cached = db.relation_graph_for_media(int(anime.media_id))
        except Exception:  # noqa: BLE001 - no graph is the same as an unknown franchise
            cached = None
        graph = cached.get("graph") if isinstance(cached, dict) else None
    links: dict[int, list[tuple[int, int]]] = {}
    titles: dict[int, str] = {int(anime.media_id): anime.title}

    def season_node(node: dict) -> bool:
        return str(node.get("format") or "").upper() in _SEASON_FORMATS

    if isinstance(graph, dict) and graph.get("nodes"):
        nodes = {
            int(node["media_id"]): node
            for node in graph.get("nodes") or []
            if isinstance(node, dict) and node.get("media_id") is not None
        }
        for media_id, node in nodes.items():
            titles.setdefault(media_id, str(node.get("title") or ""))
        for edge in graph.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            kind = str(edge.get("relation_type") or "").upper()
            step = 1 if kind == "SEQUEL" else -1 if kind == "PREQUEL" else 0
            try:
                source, target = int(edge["source"]), int(edge["target"])
            except (KeyError, TypeError, ValueError):
                continue
            if not step or not all(season_node(nodes.get(value, {})) for value in (source, target)):
                continue
            links.setdefault(source, []).append((target, step))
            links.setdefault(target, []).append((source, -step))
    else:
        for item in anime.relations or []:
            if not isinstance(item, dict) or item.get("media_id") is None or not season_node(item):
                continue
            kind = str(item.get("relation_type") or "").upper()
            step = 1 if kind == "SEQUEL" else -1 if kind == "PREQUEL" else 0
            if step:
                other = int(item["media_id"])
                titles[other] = str(item.get("title") or "")
                links.setdefault(int(anime.media_id), []).append((other, step))
    offsets = {int(anime.media_id): 0}
    queue = [int(anime.media_id)]
    while queue:
        current = queue.pop(0)
        for other, step in links.get(current, []):
            if other not in offsets:
                offsets[other] = offsets[current] + step
                queue.append(other)
    return [(media_id, titles.get(media_id, ""), offset) for media_id, offset in offsets.items()]


def season_checked_anime(
    identity: VideoIdentity,
    anime: LibraryAnime | None,
    anime_list: list[LibraryAnime],
    db: Database | None = None,
) -> LibraryAnime | None:
    """The folder or title match, unless the file names a season another entry holds.

    "Jujutsu Kaisen/[SubsPlease] Jujutsu Kaisen S2 - 01.mkv" (also "2nd Season",
    "S02E03" or a "Season 2/" folder) is no season 1 episode because the folder is
    called "Jujutsu Kaisen": the library's entry of that season takes it, and it stays
    unmatched when the franchise has a separate entry that may be that season (one
    titled so, or a named sequel such as Bleach's "Sennen Kessen-hen" for "Season 17").
    A single-entry long show in a Sonarr/Plex layout ("One Piece/Season 21/One Piece -
    S21E1050") and a named sequel's own folder keep their match while the episode
    number fits the entry.
    """
    season = identity.season
    if anime is None or season is None or season < 2:
        return anime
    from .providers.nyaa import _accepted_seasons, _expected_season, _explicit_season_numbers, _season_number

    if season in _accepted_seasons(anime):
        return anime
    best: LibraryAnime | None = None
    best_score = 0.0
    for entry in anime_list:
        if entry.media_id == anime.media_id or season not in _accepted_seasons(entry):
            continue
        names = [entry.title, *entry.titles, *entry.synonyms]
        score = max((title_similarity(identity.title, name) for name in names if name), default=0.0)
        if score > best_score:
            best, best_score = entry, score
    if best is not None and best_score >= 58:
        return best

    chain = _season_chain(db, anime)
    root_offset = min(offset for _media, _title, offset in chain)

    def title_season(title: str, offset: int) -> int | None:
        if offset == root_offset:
            return 1
        explicit = _explicit_season_numbers(title)
        if len(explicit) == 1:
            return next(iter(explicit))
        bare = _season_number(title)
        return bare if bare is not None and bare >= 2 else None  # a named sequel: unknown

    own = _expected_season(anime)
    own_season = own if own >= 2 else (1 if root_offset == 0 else None)
    if own_season is not None:
        for media_id, title, offset in chain:
            if media_id == anime.media_id:
                continue
            other = title_season(title, offset)
            on_that_side = offset > 0 if season > own_season else offset < 0
            if other == season or (other is None and on_that_side):
                return None  # the franchise's own entry for that season
    episode = identity.episode
    total = int(anime.episodes or 0)
    releasing = str(anime.media_status or "").upper() in {"RELEASING", "NOT_YET_RELEASED"}
    if episode is not None and total and not releasing and int(episode) > total:
        return None  # no episode of this entry either
    return anime


# One scan lists each folder once (MGR-9): every video used to run five globs per
# folder, each listing the whole folder, which is O(n^2) in a 1000-episode folder.
_LISTINGS: contextvars.ContextVar[dict[Path, tuple[str, ...] | None] | None] = contextvars.ContextVar(
    "pudge_sidecar_listings", default=None
)


@contextmanager
def directory_listing_cache() -> Iterator[None]:
    """Reuse folder listings for sidecar lookups inside this block (one library scan)."""
    if _LISTINGS.get() is not None:
        yield
        return
    token = _LISTINGS.set({})
    try:
        yield
    finally:
        _LISTINGS.reset(token)


def _file_names(directory: Path) -> tuple[str, ...] | None:
    """Names of the regular files in ``directory`` (None: not a folder)."""
    cache = _LISTINGS.get()
    if cache is not None and directory in cache:
        return cache[directory]
    try:
        with os.scandir(directory) as entries:
            names: tuple[str, ...] | None = tuple(sorted(entry.name for entry in entries if entry.is_file()))
    except (NotADirectoryError, FileNotFoundError):
        names = None
    except OSError:
        names = None if not directory.is_dir() else ()
    if cache is not None:
        cache[directory] = names
    return names


def sidecar_subtitle(video: Path) -> Path | None:
    """Choose a local Japanese subtitle without letting English text mark Ready."""

    candidates: list[Path] = []
    stem = video.stem
    for directory in (video.parent, video.parent / "subs"):
        names = _file_names(directory)
        if names is None:
            continue
        # "[SubsPlease] Title - 01.ja.srt": the name is literal (no glob set); this is
        # the old glob "<stem>.*<ext>" over one listing.
        prefixed = [name for name in names if name.startswith(stem + ".")]
        for ext in SUB_EXTENSIONS:
            exact = directory / f"{stem}{ext}"
            if exact.is_file():
                candidates.append(exact)
            for name in prefixed:
                if not name.endswith(ext) or len(name) < len(stem) + 1 + len(ext):
                    continue
                # "Show - 05.5.ja.ass" is episode 05.5's subtitle, not one of "Show - 05.mkv".
                if re.match(r"\.\d", name[len(stem) :]):
                    continue
                candidates.append(directory / name)
    candidates = list(dict.fromkeys(path.resolve() for path in candidates))

    text_candidates = [
        path
        for path in candidates
        if path.suffix.casefold() in TEXT_SUBTITLE_EXTENSIONS and is_japanese_subtitle(path)
    ]
    if text_candidates:
        return text_candidates[0]

    bitmap_candidates = [
        path
        for path in candidates
        if path.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS
        and not (has_negative_language_marker(path.name) and not has_japanese_marker(path.name))
    ]
    return bitmap_candidates[0] if bitmap_candidates else None


def japanese_subtitle_details(
    video: Path,
    *,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
    strict_probe_errors: bool = False,
    ignore_sidecars: bool = False,
) -> tuple[str, Path | None, int | None]:
    """Return subtitle source, path and embedded mpv subtitle id.

    Text tracks are always preferred. Bitmap PGS/SUP tracks are reported with
    an explicit ``*_bitmap`` source so callers can keep them available for
    Library-only playback without marking the anime ready.
    """
    if not ignore_sidecars:
        sidecar = sidecar_subtitle(video)
        if sidecar is not None:
            source = (
                "external_bitmap" if sidecar.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS else "external"
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
        if strict_probe_errors:
            raise
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


def anilist_sidecar_for_video(video_path: Path, root: Path) -> tuple[int, Path] | None:
    """(AniList id, folder) of the nearest ``.anilist.id`` sidecar."""
    root_resolved = root.expanduser().resolve()
    current = video_path.expanduser().resolve().parent
    while True:
        marker = current / ".anilist.id"
        if marker.is_file():
            try:
                value = int(marker.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                return None
            return (value, current) if value > 0 else None
        if current == root_resolved or root_resolved not in current.parents:
            break
        current = current.parent
    return None


class _PudgeDownloads:
    """Where Pudge itself put downloads (content/save paths of ``downloads`` rows): read once per
    scan. A file with a torrent hash the person moved elsewhere proves nothing about its folder."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._places: list[tuple[Path, int]] | None = None

    def places(self) -> list[tuple[Path, int]]:
        if self._places is None:
            places: list[tuple[Path, int]] = []
            try:
                for download in self.db.downloads():
                    if download.media_id is None:
                        continue
                    for raw in (download.content_path, download.save_path):
                        text = str(raw or "").strip()
                        if text:
                            places.append((Path(text).expanduser(), int(download.media_id)))
            except Exception:  # noqa: BLE001 - a minimal database adapter: no downloads known
                places = []
            self._places = places
        return self._places

    def inside(self, folder: Path, media_id: int) -> bool:
        return any(
            mid == media_id and (place == folder or folder in place.parents) for place, mid in self.places()
        )


def _pudge_sidecar(db: Database, folder: Path, media_id: int, downloads: _PudgeDownloads) -> bool:
    """Pudge wrote this ``.anilist.id`` (it marks every title folder it downloads into), so it
    is no pin of the person: recorded when written (or by the one-time migration), or Pudge's
    download of that title is in the folder right now (never stored: once the download is
    gone, an unrecorded sidecar is the person's again). A title-named folder, a title Pudge
    once chose a release for or a moved file with a torrent hash is no proof."""
    try:
        if str(db.get_state(extra_files.pudge_sidecar_state_key(folder), "") or "") == str(media_id):
            return True
    except Exception:  # noqa: BLE001 - a minimal database adapter
        return False
    return downloads.inside(folder.resolve(), int(media_id))


def _record_pudge_sidecars_once(db: Database, root: Path) -> None:
    """One-time: sidecars Pudge wrote before it recorded them are those of folders Pudge put a
    download of the same title into (content/save path of a ``downloads`` row)."""
    done_key = (
        "anilist_sidecar_by_pudge_migrated:" + hashlib.sha1(str(root.expanduser()).encode()).hexdigest()
    )
    try:
        if db.get_state(done_key, ""):
            return
        downloads = _PudgeDownloads(db)
        recorded = 0
        for place, media_id in downloads.places():
            folder_like = place.is_dir() or place.suffix.casefold() not in VIDEO_EXTENSIONS
            pin = anilist_sidecar_for_video(place / "_" if folder_like else place, root)
            if pin is not None and pin[0] == media_id:
                key = extra_files.pudge_sidecar_state_key(pin[1])
                if not db.get_state(key, ""):
                    db.set_state(key, str(media_id))
                    recorded += 1
        db.set_state(done_key, str(recorded))
    except Exception:  # noqa: BLE001 - bookkeeping; the live check still applies
        return


def moved_episode_row(db: Database, video: Path, media_id: int, episode: int, root: Path) -> Path | None:
    """The row of a video that was renamed or moved to ``video``, else None.

    "[G] Frieren - 01.mkv" tidied into "Frieren 01.mkv" used to start over as a new
    "local" row (new subtitle job, full preparation) while the old row with its
    position, watched mark and subtitle waited to be forgotten. The old row must be
    the only missing file of this title and episode on a present drive (its folder
    still exists, or it lies under the scanned root). A row whose subtitle selection
    recorded a content fingerprint moves only to a file with that fingerprint; a row
    with a subtitle but no fingerprint does not move (nothing ties it to this file).
    """
    try:
        root_resolved = root.expanduser().resolve()
    except OSError:
        return None
    candidates = []
    for row in db.episode_rows(media_id, episode):
        old = row.video_path
        if old == video:
            continue
        try:
            if old.exists() or not (old.parent.is_dir() or root_resolved in old.parents):
                continue
        except OSError:
            continue
        candidates.append(row)
    if not candidates:
        return None
    current = None
    matched = []
    for row in candidates:
        recorded = db.selected_subtitle_fingerprint(row.video_path)
        if recorded:
            if current is None:
                current = Database._sampled_video_fingerprint(video) or ""
            if recorded == current:
                matched.append(row)
    if len(matched) == 1:
        return matched[0].video_path
    if matched or len(candidates) != 1:
        return None
    (row,) = candidates
    if db.selected_subtitle_fingerprint(row.video_path):
        return None  # a selection made for another file
    if row.subtitle_path is not None or row.embedded_subtitle_id is not None:
        return None  # nothing proves the subtitle belongs to this file
    return row.video_path


# Subtitles that belong to the file itself, whatever title/episode it is filed under.
_FILE_OWN_SUBTITLE_ORIGINS = frozenset({"embedded", "external", "bitmap", "ocr", "manual"})


def _forget_subtitles_of_old_identity(
    db: Database,
    row: LibraryEpisode,
    video: Path,
    media_id: int | None,
    episode: int | None,
    config: AppConfig | None,
) -> None:
    """A special filed as a series episode got subtitles looked up for that episode (Baccano's
    S00E01 had Jimaku's TV episode 1): prepare them again for its own identity. Embedded,
    sidecar, OCR and manually chosen subtitles belong to the file and stay."""
    origin = str(row.subtitle_origin or "").casefold()
    if origin in _FILE_OWN_SUBTITLE_ORIGINS or (
        row.subtitle_path is None and row.embedded_subtitle_id is None
    ):
        return
    if config is not None:
        invalidate_final_pipeline_result(video, config)
    db.invalidate_subtitle(video, media_id, episode, err("jobs.specialRefiled"))


def _season_zero_file(
    path: Path,
    identity: VideoIdentity,
    series: LibraryAnime | None,
    root: Path,
    anime_list: list[LibraryAnime],
) -> bool:
    """A special of the series ``series`` (TV, TV_SHORT, ONA): "S00E01" (or "Season 0") in its
    name or folders, or a file in a "Specials"/"SP" folder inside the series' own folder."""
    if not extra_files.episodic_series(series):
        return False
    if identity.season == 0 or parse_release_name(path.name).season == 0:
        return True
    if identity.season is None and extra_files.specials_folder(path):
        owner = parent_folder_anime(path.parent, root, anime_list)
        return owner is not None and series is not None and owner.media_id == series.media_id
    return False


def _pudge_batch_file(db: Database, row: LibraryEpisode | None) -> bool:
    """A file of a batch Pudge downloaded (the download may be gone from the client)."""
    torrent = str(getattr(row, "torrent_hash", "") or "").strip()
    if not torrent:
        return False
    download = db.download_by_hash(torrent)
    if download is not None:
        return bool(download.is_batch)
    finder = getattr(db, "release_metadata_by_hash", None)
    metadata = finder(torrent) if callable(finder) else None
    return metadata is not None and metadata[1] is None  # chosen for the title, not one episode


def _regular_episode_exists(db: Database, series: LibraryAnime, video: Path, number: int | None) -> bool:
    """Another, regular (not season-0) file is episode ``number`` of the series."""
    if number is None:
        return False

    def regular(path: Path) -> bool:
        return parse_anime_filename(path).season != 0 and parse_release_name(path.name).season != 0

    for row in db.episodes(series.media_id):
        current = row.media_episode if row.media_episode is not None else row.episode
        if (
            row.video_path != video
            and current == number
            and regular(row.video_path)
            and row.video_path.is_file()
        ):
            return True
    try:
        siblings = [child for child in video.parent.iterdir() if child != video]
    except OSError:
        siblings = []
    for child in siblings:
        if child.suffix.casefold() not in VIDEO_EXTENSIONS or not child.is_file():
            continue
        parsed = parse_anime_filename(child)
        if parsed.episode == number and parsed.season != 0 and regular(child):
            return True
    return False


def _season_zero_route(
    db: Database, series: LibraryAnime, path: Path, number: int | None, *, reroute: bool
) -> tuple[str, LibraryAnime | None, int | None]:
    """("reroute", entry, episode) to a TV series' one related OVA/SPECIAL/ONA entry
    (``reroute``: not pinned by the person) when that episode of it is free; ("extra", None,
    None) when it would be a second file of a regular episode (any series, pinned too) or of
    the entry's episode; else ("keep", None, None): an entry numbered in season 0 itself."""
    video = path.resolve()
    if reroute and extra_files.season_zero_series(series):
        node = extra_files.own_title_for_extra(db, series, path.name, season=0)
        if node is not None:
            target = extra_files.own_title_anime(db, node, series)
            episode = extra_files.own_title_episode(target, path.name)
            taken = episode is not None and any(
                row.video_path != video
                and (row.media_episode if row.media_episode is not None else row.episode) == episode
                and row.video_path.is_file()
                for row in db.episodes(target.media_id)
            )
            if not taken:
                return "reroute", target, episode
            return "extra", None, None
    if _regular_episode_exists(db, series, video, number):
        return "extra", None, None
    return "keep", None, None


def _refile(
    db: Database,
    existing: LibraryEpisode | None,
    video: Path,
    series: LibraryAnime,
    media_id: int | None,
    episode: int | None,
    title: str,
    config: AppConfig | None,
) -> LibraryEpisode | None:
    """Give a file filed as a series episode its own identity. Subtitles looked up for the old
    episode go; a row that keeps its title and episode is never touched."""
    if existing is None or (existing.media_id == media_id and existing.episode == episode):
        return existing
    was_series_episode = existing.media_id == series.media_id and existing.episode is not None
    db.set_episode_identity(video, media_id, episode, title)
    if was_series_episode:
        _forget_subtitles_of_old_identity(db, existing, video, media_id, episode, config)
    return db.episode_by_path(video)


def _ask_about_special(db: Database, video: Path, media_id: int) -> None:
    """Action required (Delete / Keep) for a special taken out of the series' episodes.

    The decision stays with the file (see ``extra_files.set_aside_state_key``)."""
    db.set_state(extra_files.set_aside_state_key(video), str(int(media_id)))
    row = db.episode_by_path(video)
    if row is None or row.state in {"ready", "watched"}:
        return  # playable with its own subtitles; it is simply no numbered episode
    db.ensure_subtitle_job(video, media_id, None)
    db.mark_subtitle_job_needs_action(video, err("jobs.seasonZeroExtra"), EXTRA_FILE_ACTION_CODES[0])


def user_identity_state_key(video: Path) -> str:
    """State key of the AniList entry and episode the person chose for a file (Identify…)."""
    return "episode_identity_user:" + hashlib.sha1(str(Path(video).resolve()).encode("utf-8")).hexdigest()


def user_episode_identity(db: Database, video: Path) -> tuple[int, int | None] | None:
    """(media_id, episode) the person gave this file, or None. Scans keep it."""
    return _parse_user_identity(db.get_state(user_identity_state_key(video), ""))


def _parse_user_identity(raw: str) -> tuple[int, int | None] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
        media_id = int(value["media_id"])
        episode = value.get("episode")
        episode = int(episode) if episode is not None else None
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if media_id <= 0 or (episode is not None and episode <= 0):
        return None
    return media_id, episode


def _user_identity_claims(db: Database) -> dict[tuple[int, int], Path]:
    """(media_id, episode) -> the file the person gave that episode with Identify…."""
    keys: dict[str, Path] = {}
    for row in db.episodes():
        try:
            keys[user_identity_state_key(row.video_path)] = row.video_path
        except OSError:
            continue
    claims: dict[tuple[int, int], Path] = {}
    names = list(keys)
    for start in range(0, len(names), 400):
        for key, raw in db.get_states(tuple(names[start : start + 400])).items():
            parsed = _parse_user_identity(raw)
            if parsed is not None and parsed[1] is not None:
                claims[(parsed[0], parsed[1])] = keys[key]
    return claims


def _volume_not_mounted(path: Path) -> bool:
    """``path`` lies on /Volumes/<name> and that volume is not there."""
    parts = Path(path).parts
    if len(parts) < 3 or parts[0] != "/" or parts[1] != "Volumes":
        return False
    return not Path("/Volumes", parts[2]).is_dir()


@directory_listing_cache()
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
    media_episode_resolver: Callable[[LibraryAnime, int | None], int | None] | None = None,
    file_episode_resolver: Callable[[LibraryAnime, Path, int | None], int | None] | None = None,
    require_anime_match: bool = False,
    finished_torrent: Callable[[str], bool] | None = None,
) -> list[LibraryEpisode]:
    root = root.expanduser()
    if not root.exists():
        if _volume_not_mounted(root):
            # An external disk not there (unplugged, not mounted yet): a folder made now sits on
            # the system disk, the real one then mounts as "/Volumes/<name> 1" and every later
            # download lands on the system disk.
            return []
        root.mkdir(parents=True, exist_ok=True)
    anime_list = db.anime_list()
    downloads_by_path = {}
    for download in db.downloads():
        raw_path = str(download.content_path or "").strip()
        if not raw_path and str(download.save_path or "").strip() and str(download.name or "").strip():
            raw_path = str(Path(download.save_path).expanduser() / download.name)
        if not raw_path:
            continue
        try:
            downloads_by_path[Path(raw_path).expanduser().resolve()] = download
        except (OSError, RuntimeError):
            continue
    iterator = root.rglob("*") if recursive else root.glob("*")
    excluded = tuple(path.expanduser().resolve() for path in excluded_paths)

    def is_excluded(path: Path) -> bool:
        resolved = path.resolve()
        for blocked in excluded:
            if resolved == blocked or blocked in resolved.parents:
                return True
        return False

    result: list[LibraryEpisode] = []
    folder_matches: dict[tuple[Path, int], LibraryAnime | None] = {}
    _record_pudge_sidecars_once(db, root)
    pudge_downloads = _PudgeDownloads(db)
    identity_claims = _user_identity_claims(db)

    def scan_file(path: Path) -> None:
        """One file of the scan: its row is written and appended to ``result``."""
        if not path.is_file() or path.suffix.casefold() not in VIDEO_EXTENSIONS:
            return
        if is_excluded(path) or unfinished_video(path, root, finished_torrent=finished_torrent):
            return
        identity = parse_anime_filename(path)
        resolved = path.resolve()
        # The person identified this file (Identify…): their entry and episode win over
        # every match below, in every later scan.
        chosen = user_episode_identity(db, resolved)
        chosen_anime = db.get_anime(chosen[0]) if chosen is not None else None
        managed_download = downloads_by_path.get(resolved)
        # Managed torrent metadata is authoritative for identity. A fuzzy parent
        # folder/title match must never relabel a completed Season 2 download as
        # Season 1 merely because the release filename uses absolute numbering.
        managed_anime = (
            db.get_anime(managed_download.media_id)
            if managed_download is not None and managed_download.media_id is not None
            else None
        )
        anilist_pin = anilist_sidecar_for_video(path, root)
        sidecar_anilist_id = anilist_pin[0] if anilist_pin is not None else None
        anime = (
            chosen_anime
            or managed_anime
            or (db.get_anime(sidecar_anilist_id) if sidecar_anilist_id else None)
        )
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
            # The folder match depends only on the folder chain and the anime
            # list (which only grows during the scan): fuzzy-match each folder once.
            folder_key = (resolved.parent, len(anime_list))
            if folder_key not in folder_matches:
                folder_matches[folder_key] = parent_folder_anime(path, root, anime_list)
            anime = season_checked_anime(identity, folder_matches[folder_key], anime_list, db)
        if anime is None and not require_anime_match:
            anime = season_checked_anime(identity, match_anime(identity.title, anime_list), anime_list, db)
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
            return
        existing = db.episode_by_path(resolved)
        own_title_episode: int | None = None
        own_title_file = False
        # A batch is keyed by its folder, not by the file: find the batch that holds
        # this file (only to leave its extras alone; identity stays as decided above).
        batch_download = (
            managed_download if managed_download is not None and managed_download.is_batch else None
        )
        if batch_download is None and existing is not None:
            for parent in resolved.parents:
                candidate = downloads_by_path.get(parent)
                if candidate is not None:
                    batch_download = candidate if candidate.is_batch else None
                    break
            if batch_download is None and existing.torrent_hash:
                candidate = db.download_by_hash(existing.torrent_hash)
                batch_download = candidate if candidate is not None and candidate.is_batch else None
        # Season 0 of a TV series ("S00E01", a "Specials" folder of the series) is a special:
        # its one related OVA/SPECIAL/ONA entry, or an extra file the person decides about when
        # it would be a second file of a regular episode, never a duplicate episode. An AniList
        # pin (.anilist.id the person wrote) or a single-episode download decides as before.
        single_download = managed_download is not None and not managed_download.is_batch
        special_number = identity.episode
        ask_about_special = False
        set_aside_key = extra_files.set_aside_state_key(resolved)
        set_aside = bool(db.get_state(set_aside_key, ""))
        if set_aside and (existing is None or existing.episode is not None or existing.media_id is None):
            db.delete_state(set_aside_key)  # the person gave it an identity since
            set_aside = False
        if set_aside and existing is not None:
            owner = db.get_anime(existing.media_id) if existing.media_id is not None else None
            if not extra_files.season_zero_series(owner) and (
                owner is None or not _regular_episode_exists(db, owner, resolved, special_number)
            ):
                # Sticky only for a TV series. A TV_SHORT/ONA numbered in season 0 ("Re ZERO -
                # S00E01 - Break Time 1"): once the other copy of the episode is gone, it is it.
                db.delete_state(set_aside_key)
                set_aside = False
        if chosen_anime is not None and chosen is not None:
            own_title_file = True
            own_title_episode = chosen[1]
        elif db.get_state(extra_files.kept_state_key(resolved), ""):
            # An extra file the person kept: it keeps the title and episode Keep gave it (a
            # kept "S00E01" is no episode 1 of the series).
            anime = db.get_anime(existing.media_id) if existing is not None and existing.media_id else None
            if existing is not None:
                own_title_file = True
                own_title_episode = existing.episode
        elif set_aside and existing is not None:
            # A special set aside earlier stays so, also once the regular episode with its
            # number is watched and gone, until the person keeps or deletes it.
            anime = db.get_anime(existing.media_id) or anime
            own_title_file = True
            own_title_episode = None
        elif batch_download is not None and existing is not None:
            batch_anime = (
                managed_anime
                if batch_download is managed_download
                else db.get_anime(batch_download.media_id)
                if batch_download.media_id is not None
                else None
            )
            zero = batch_anime is not None and _season_zero_file(
                path, identity, batch_anime, root, anime_list
            )
            route = "keep"
            if zero and existing.episode is not None and existing.media_id == batch_anime.media_id:
                # An older scan filed this special of the batch as a series episode.
                route, target, target_episode = _season_zero_route(
                    db, batch_anime, path, special_number or existing.episode, reroute=True
                )
                if route == "reroute" and target is not None:
                    anime = target
                    if anime not in anime_list:
                        anime_list.append(anime)
                    own_title_file = True
                    own_title_episode = target_episode
                    existing = _refile(
                        db,
                        existing,
                        resolved,
                        batch_anime,
                        anime.media_id,
                        own_title_episode,
                        anime.title,
                        pipeline_cache_config,
                    )
                elif route == "extra":
                    existing = _refile(
                        db,
                        existing,
                        resolved,
                        batch_anime,
                        batch_anime.media_id,
                        None,
                        batch_anime.title,
                        pipeline_cache_config,
                    )
                    _ask_about_special(db, resolved, batch_anime.media_id)
                    result.append(existing or db.episode_by_path(resolved))
                    return
            if route != "reroute" and extra_files.extra_file_kind(
                batch_anime, path.name, season=0 if zero else None
            ):
                # A downloaded extra waits for the person (Action required): unchanged.
                result.append(existing)
                return
        elif (
            anime is not None
            and not single_download
            and _season_zero_file(path, identity, anime, root, anime_list)
        ):
            series = anime
            # The person's own .anilist.id decides the title (no move to the related entry);
            # Pudge's folder marker (recorded, or a Pudge download inside) or a Pudge batch does not.
            person_pin = bool(
                anilist_pin is not None
                and not _pudge_sidecar(db, anilist_pin[1], anilist_pin[0], pudge_downloads)
                and not _pudge_batch_file(db, existing)
            )
            route, target, target_episode = _season_zero_route(
                db,
                series,
                path,
                special_number if special_number is not None else getattr(existing, "episode", None),
                reroute=not person_pin,
            )
            if route == "reroute" and target is not None:
                anime = target
                if anime not in anime_list:
                    anime_list.append(anime)
                own_title_file = True
                own_title_episode = target_episode
                existing = _refile(
                    db,
                    existing,
                    resolved,
                    series,
                    anime.media_id,
                    own_title_episode,
                    anime.title,
                    pipeline_cache_config,
                )
            elif route == "extra":
                own_title_file = True  # no episode number: Action required
                own_title_episode = None
                ask_about_special = existing is None or existing.episode is not None
                existing = _refile(
                    db, existing, resolved, series, series.media_id, None, series.title, pipeline_cache_config
                )
            # "keep": an entry numbered in season 0 itself (Hanamonogatari as S00E01-05).
        elif anime is not None and extra_files.is_story_extra(anime, path.name, season_zero=False):
            # An OVA/movie in the person's own folder of a series: its own AniList entry
            # when the series has exactly one fitting related entry, else a title of its own.
            series = anime
            node = extra_files.own_title_for_extra(db, anime, path.name)
            anime = extra_files.own_title_anime(db, node, series) if node is not None else None
            if anime is not None and anime not in anime_list:
                anime_list.append(anime)
            own_title_file = True
            own_title_episode = (
                extra_files.own_title_episode(anime, path.name) if anime is not None else identity.episode
            )
            target_id = anime.media_id if anime is not None else None
            existing = _refile(
                db,
                existing,
                resolved,
                series,
                target_id,
                own_title_episode,
                anime.title if anime is not None else identity.title,
                pipeline_cache_config,
            )
        sidecar = sidecar_subtitle(path)
        existing_download = managed_download
        if existing_download is None and existing is not None and existing.torrent_hash:
            existing_download = db.download_by_hash(existing.torrent_hash)
        existing_has_managed_download = bool(
            existing_download is not None
            and anime is not None
            and existing_download.media_id == anime.media_id
        )
        release_number = identity.episode
        mapped_media_number = None
        if anime is not None and media_episode_resolver is not None:
            try:
                mapped_media_number = media_episode_resolver(anime, release_number)
            except (TypeError, ValueError):
                mapped_media_number = None
        media_number = (
            existing_download.media_episode
            if existing_has_managed_download
            and existing_download is not None
            and existing_download.media_episode is not None
            else mapped_media_number
            if mapped_media_number is not None
            else existing.media_episode
            if existing is not None
            else identity.episode
        )
        if own_title_file:
            media_number = own_title_episode
            release_number = own_title_episode
        elif anime is not None and file_episode_resolver is not None:
            media_number = file_episode_resolver(anime, resolved, media_number)
            if media_number is None and release_number is not None:
                # An ambiguous file stays available for manual identity review;
                # it must not acquire a Ready episode from a numeric fallback.
                return
        holder = (
            identity_claims.get((int(anime.media_id), int(media_number)))
            if chosen_anime is None and anime is not None and media_number is not None
            else None
        )
        if holder is not None and holder != resolved and holder.is_file():
            # The person gave another file this episode (Identify…): this copy is set aside
            # (Action required: Delete / Keep), never a second file of the episode.
            existing = _refile(
                db, existing, resolved, anime, anime.media_id, None, anime.title, pipeline_cache_config
            )
            media_number = release_number = None
            own_title_file = True
            ask_about_special = True
        if existing is None and anime is not None and media_number is not None and not own_title_file:
            moved = moved_episode_row(db, resolved, anime.media_id, media_number, root)
            if moved is not None and db.merge_episode_path(moved, resolved):
                existing = db.episode_by_path(resolved)
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
                    existing.subtitle_path.is_file() and existing.subtitle_path.stat().st_size > 0
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
            and existing.state in {"ready", "watched", "waiting_text_subtitles", "couldnt_sync"}
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
                subtitle_source = "external_bitmap" if subtitle is not None else "embedded_bitmap"
            elif existing.state == "couldnt_sync":
                subtitle_source = "raw_unsynced"
            else:
                subtitle_source = "external" if subtitle is not None else "embedded"
        elif sidecar is not None:
            subtitle = sidecar.resolve()
            embedded_subtitle_id = None
            subtitle_source = (
                "external_bitmap" if sidecar.suffix.casefold() in IMAGE_SUBTITLE_EXTENSIONS else "external"
            )
        elif cached_pipeline is not None:
            cached_subtitle = str(cached_pipeline.get("subtitle") or "").strip()
            subtitle = Path(cached_subtitle).resolve() if cached_subtitle else None
            raw_subtitle_id = cached_pipeline.get("subtitle_id")
            embedded_subtitle_id = int(raw_subtitle_id) if raw_subtitle_id is not None else None
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
        elif existing is not None and existing.state == "couldnt_sync" and subtitle_source == "raw_unsynced":
            state = "couldnt_sync"
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
            episode=media_number,
            media_episode=media_number,
            release_episode=release_number,
            video_path=resolved,
            subtitle_path=subtitle,
            embedded_subtitle_id=embedded_subtitle_id,
            state=state,
            subtitle_origin=subtitle_origin,
            torrent_hash=(
                existing_download.torrent_hash
                if existing_has_managed_download and existing_download is not None
                else existing.torrent_hash
                if existing is not None
                else ""
            ),
        )
        db.upsert_episode(item)
        if ask_about_special and anime is not None:
            _ask_about_special(db, resolved, anime.media_id)
        persisted = db.episode_by_path(resolved) or item
        if persisted.state in {"local", "waiting_subtitles", "waiting_text_subtitles", "couldnt_sync"}:
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

    failed = 0
    for path in iterator:
        try:
            scan_file(path)
        except sqlite3.Error:
            # The database itself: every other file would fail (or wait out the lock) too.
            raise
        except Exception as exc:  # noqa: BLE001 - one file never ends the scan
            if getattr(db, "_retired", False):
                raise  # a restore took the database away: stop, as before
            # A name no parser expected, a resolver or probe bug for one file: the scan
            # used to stop there (and the maintenance pass with it), every pass again.
            failed += 1
            if failed <= _SCAN_FILE_FAILURES_LOGGED:
                _LOGGER.warning(
                    "SKIP step=library.scan_file video=%r error=%r",
                    str(path),
                    f"{type(exc).__name__}: {exc}"[:300],
                )
    if failed > _SCAN_FILE_FAILURES_LOGGED:
        _LOGGER.warning(
            "SKIP step=library.scan_file failed=%s (first %s logged)", failed, _SCAN_FILE_FAILURES_LOGGED
        )
    return result
