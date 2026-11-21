"""Files that are not episodes of the title they sit with: an OVA, special or movie in the
folder (or batch) of a TV series, an NCOP/NCED/PV.

Downloads ask the person (Home: Action required, Delete or Keep). The person's own folders
are handled quietly: an NCOP/NCED/PV stays in the Library only, an OVA or movie belongs to
its own AniList entry when the series has exactly one fitting related entry, otherwise it is
a title of its own. Keep does the same for a downloaded OVA or movie.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .manager_models import LibraryAnime
from .release_parser import parse_release_name

STORY_TYPES = frozenset({"ova", "oad", "oav", "special", "specials", "sp", "movie", "film"})
EPISODIC_FORMATS = frozenset({"TV", "TV_SHORT", "ONA"})
# Which AniList formats an extra of a given kind can belong to.
OWN_TITLE_FORMATS = {
    "ova": frozenset({"OVA", "SPECIAL", "ONA"}),
    "oad": frozenset({"OVA", "SPECIAL"}),
    "oav": frozenset({"OVA", "SPECIAL"}),
    "special": frozenset({"SPECIAL", "OVA"}),
    "specials": frozenset({"SPECIAL", "OVA"}),
    "sp": frozenset({"SPECIAL", "OVA"}),
    "movie": frozenset({"MOVIE"}),
    "film": frozenset({"MOVIE"}),
}


# Plex/Jellyfin/Sonarr keep season 0 in a "Specials" folder (BD rips: "SPs").
_SPECIALS_FOLDER_RE = re.compile(r"(?i)^(?:specials?|sps?)$")
# A season-0 file of a TV series belongs to its one related entry of these formats.
SEASON_ZERO_FORMATS = frozenset({"OVA", "SPECIAL", "ONA"})


def specials_folder(path: Path) -> bool:
    """The file sits directly in a "Specials"/"SP" folder (the caller checks that the folder
    is inside the series' own folder: a top-level "Specials" folder can hold any series)."""
    return bool(_SPECIALS_FOLDER_RE.match(Path(path).parent.name))


def season_zero_series(anime: LibraryAnime | None) -> bool:
    """Only a TV series has specials *beside* its episodes. TV_SHORT/ONA/OVA entries are
    often season 0 themselves (TVDB: "Re ZERO - S00E01 - Break Time 1")."""
    return anime is not None and str(anime.format or "").upper() == "TV"


def _episodic(anime: LibraryAnime | None) -> bool:
    return anime is not None and str(anime.format or "").upper() in EPISODIC_FORMATS


def _season_zero(anime: LibraryAnime | None, parsed: Any, season: int | None) -> bool:
    """Season 0 of a TV series: "S00E01" in the name, or ``season=0`` the scan decided."""
    return season_zero_series(anime) and (parsed.season == 0 or season == 0)


def extra_file_kind(
    anime: LibraryAnime | None, name: str, *, season: int | None = None, season_zero: bool = True
) -> str:
    """ "OVA", "Movie", "NCOP"... when ``name`` is an extra file of ``anime``, else "".

    ``season`` is the season the scan decided for the file; ``season_zero=False`` leaves the
    season-0 rule to the caller (the library scan has its own, see library.py).
    """
    parsed = parse_release_name(name)
    kind = str(parsed.anime_type or "").strip()
    if parsed.extra:
        return kind or "Extra"
    if _episodic(anime) and (
        parsed.special
        or kind.casefold() in STORY_TYPES
        or (season_zero and _season_zero(anime, parsed, season))
    ):
        return kind or "Special"
    return ""


def is_story_extra(
    anime: LibraryAnime | None, name: str, *, season: int | None = None, season_zero: bool = True
) -> bool:
    """An OVA, special or movie of an episodic series (not an NCOP/NCED/PV)."""
    parsed = parse_release_name(name)
    return bool(not parsed.extra and extra_file_kind(anime, name, season=season, season_zero=season_zero))


def kept_state_key(video: Path) -> str:
    return "extra_file_kept:" + hashlib.sha1(str(Path(video).resolve()).encode("utf-8")).hexdigest()


def set_aside_state_key(video: Path) -> str:
    """A season-0 file the library scan set aside (no episode, Action required). The decision
    stays with the file until the person keeps or deletes it: when the regular episode with its
    number is watched and cleaned up, the special must not become that episode (TV only)."""
    from .database import season_zero_set_aside_key

    return season_zero_set_aside_key(video)


def title_folder_name(title: str) -> str:
    """The folder Pudge downloads a title into (``<library>/<this>``)."""
    return "".join("_" if char in '/\\:*?"<>|' else char for char in str(title)).strip() or "Anime"


def pudge_sidecar_state_key(folder: Path) -> str:
    """Pudge wrote ``folder/.anilist.id`` itself (for its downloads), it is no pin of the person."""
    return "anilist_sidecar_by_pudge:" + hashlib.sha1(str(Path(folder).resolve()).encode("utf-8")).hexdigest()


def episodic_series(anime: LibraryAnime | None) -> bool:
    """TV, TV_SHORT or ONA: a series whose numbered episodes a special must not duplicate."""
    return _episodic(anime)


def own_title_for_extra(
    db: Any, series: LibraryAnime | None, name: str, *, season: int | None = None
) -> dict[str, Any] | None:
    """The related AniList entry an OVA/movie file of ``series`` belongs to, or None.

    Only a direct relation of the series counts, and only when exactly one related entry
    has a fitting format (several OVAs: Pudge cannot tell which one the file is).
    """
    if series is None:
        return None
    parsed = parse_release_name(name)
    zero = _season_zero(series, parsed, season)
    kind = str(parsed.anime_type or "").strip().casefold() or ("special" if parsed.special or zero else "")
    formats = OWN_TITLE_FORMATS.get(kind)
    if zero:
        formats = (formats or frozenset()) | SEASON_ZERO_FORMATS
    if not formats:
        return None
    series_id = int(series.media_id)
    candidates: dict[int, dict[str, Any]] = {}
    cached = db.relation_graph_for_media(series_id) if hasattr(db, "relation_graph_for_media") else None
    graph = cached.get("graph") if isinstance(cached, dict) else None
    if isinstance(graph, dict) and graph.get("nodes"):
        nodes = {
            int(node["media_id"]): node
            for node in graph.get("nodes") or []
            if isinstance(node, dict) and node.get("media_id") is not None
        }
        for edge in graph.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            ends = {edge.get("source"), edge.get("target")}
            if series_id not in ends:
                continue
            other = next((value for value in ends if value != series_id), None)
            node = nodes.get(int(other)) if other is not None else None
            if node is not None and str(node.get("format") or "").upper() in formats:
                candidates[int(other)] = node
    else:
        for item in series.relations or []:
            if isinstance(item, dict) and item.get("media_id") is not None:
                if str(item.get("format") or "").upper() in formats:
                    candidates[int(item["media_id"])] = item
    if len(candidates) != 1:
        return None
    return next(iter(candidates.values()))


def _node_cover(node: dict[str, Any], parent: LibraryAnime | None) -> str:
    """The entry's own AniList cover, else the series' (an entry AniList has no image for)."""
    own = str(node.get("cover_url") or "").strip()
    if own:
        return own
    return str(getattr(parent, "cover_url", "") or "").strip()


def refresh_own_title_metadata(
    anime: LibraryAnime, node: dict[str, Any], parent: LibraryAnime | None = None
) -> bool:
    """Fill what the related entry's row is missing from its AniList relation node.

    A row Pudge made off the list (an S00 special or OVA split from its series) is never
    refreshed by the list sync: rows from before kept no cover ("Cover" placeholder on Home
    for days). Its own cover replaces the series' fallback once AniList has one. Rows on the
    person's list keep what the list sync wrote; only empty fields are filled there.
    Returns whether something changed.
    """
    changed = False
    off_list = not str(anime.status or "").strip()
    cover = _node_cover(node, parent)
    own_cover = str(node.get("cover_url") or "").strip()
    parent_cover = str(getattr(parent, "cover_url", "") or "").strip()
    if cover and (
        not anime.cover_url
        or (off_list and own_cover and anime.cover_url == parent_cover and own_cover != parent_cover)
    ):
        anime.cover_url = cover
        changed = True
    media_id = int(anime.media_id)
    site = str(node.get("site_url") or "").strip() or f"https://anilist.co/anime/{media_id}"
    if not anime.site_url:
        anime.site_url = site
        changed = True
    for field_name, value in (
        ("episodes", int(node["episodes"]) if node.get("episodes") else None),
        ("format", str(node.get("format") or "") or None),
        ("season_year", int(node["season_year"]) if node.get("season_year") else None),
        ("start_date", str(node.get("start_date") or "") or None),
        ("studio", str(node.get("studio") or "")),
        ("media_status", str(node.get("media_status") or "") or None),
    ):
        if value in (None, ""):
            continue
        current = getattr(anime, field_name)
        if current in (None, "") or (off_list and current != value):
            setattr(anime, field_name, value)
            changed = True
    title = str(node.get("title") or "").strip()
    # No AniList titles yet: the row's title is a placeholder (a name parsed from a file).
    if title and off_list and (not anime.titles or anime.title in ("", f"AniList {media_id}", str(media_id))):
        anime.title = title
        changed = True
    if title and not anime.titles:
        anime.titles = [title]
        changed = True
    return changed


def own_title_anime(db: Any, node: dict[str, Any], parent: LibraryAnime | None = None) -> LibraryAnime:
    """The local row of the related entry: the existing one, or a new one off the list.

    ``parent`` is the series the file came with: its cover stands in when the entry has
    none on AniList.
    """
    media_id = int(node["media_id"])
    existing = db.get_anime(media_id)
    if existing is not None:
        if refresh_own_title_metadata(existing, node, parent):
            db.upsert_anime(existing)
            return db.get_anime(media_id) or existing
        return existing
    title = str(node.get("title") or f"AniList {media_id}")
    anime = LibraryAnime(
        media_id=media_id,
        title=title,
        titles=[title] if node.get("title") else [],
        cover_url=_node_cover(node, parent),
        site_url=str(node.get("site_url") or "") or f"https://anilist.co/anime/{media_id}",
        status="",
        episodes=int(node["episodes"]) if node.get("episodes") else None,
        format=str(node.get("format") or ""),
        season_year=int(node["season_year"]) if node.get("season_year") else None,
        start_date=str(node.get("start_date") or "") or None,
        studio=str(node.get("studio") or ""),
        media_status=str(node.get("media_status") or "") or None,
    )
    db.upsert_anime(anime)
    return db.get_anime(media_id) or anime


def own_title_episode(anime: LibraryAnime, name: str) -> int | None:
    """The episode of the related entry: the number in the name (``S00E02``: 2), or 1 of a
    one-episode entry."""
    parsed = parse_release_name(name)
    number = parsed.episode if parsed.episode is not None else None
    if number is None and parsed.raw_episodes and len(parsed.raw_episodes) == 1:
        try:
            number = int(str(parsed.raw_episodes[0]))
        except ValueError:
            number = None
    total = int(anime.episodes or 0)
    if number is not None and (not total or 1 <= number <= total):
        return number
    return 1 if total == 1 else None
