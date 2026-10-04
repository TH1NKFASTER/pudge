"""Exact episode rules from the external-ID index (PUDGE_IMPLEMENTATION task 3).

Turns an AniList entry's Anime-Lists mapping into explicit, piecewise rules
AniList/AniDB episode ↔ TheTVDB (season, episode), usable in both directions:

* ``kind="pair"``   one episode → one target episode (``;5-7;``)
* ``kind="range"``  ``start..end`` with its own offset (``start=13 end=24 offset=-12``)
* ``kind="offset"`` the entry's default season + ``episodeoffset`` for every
  other regular episode

Pairs beat ranges beat the default offset — so an exception for one episode
never becomes a global offset, and a range never borrows the offset of
episode 1.  Specials (AniDB season 0) are kept as ``episode_type="special"``
and never turn into regular episodes.

Rules are only produced when the identity is unambiguous: exactly one AniDB
link for the AniList id, a real TVDB series id (not a ``movie``/``OVA``
sentinel) and a concrete default season (not ``a`` = absolute).  A TVDB
number is *not* automatically a release number: callers decide which scheme
a release uses (season-local ``S02E05`` vs. continuing absolute numbering).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from .providers.anime_mappings import AnimeMappings, ExternalMapping


@dataclass(frozen=True)
class EpisodeMappingRule:
    source_scheme: str  # "anilist" (numbering of the AniList/AniDB entry)
    target_scheme: str  # "tvdb"
    anilist_id: int
    anidb_id: int
    tvdb_id: int
    target_season: int
    kind: str  # "pair" | "range" | "offset"
    episode_type: str = "regular"  # "regular" | "special"
    start: int | None = None
    end: int | None = None
    offset: int = 0
    source_episode: int | None = None
    target_episode: int | None = None  # 0 = no equivalent on the target side
    provenance: str = "Anime-Lists/anime-lists via Fribb/anime-lists"


_mappings_lock = threading.Lock()
_mappings_by_root: dict[str, AnimeMappings] = {}
_rules_cache: dict[tuple[str, int, float], tuple[EpisodeMappingRule, ...]] = {}


def _mappings(cache_dir: Path) -> AnimeMappings:
    key = str(Path(cache_dir).expanduser())
    with _mappings_lock:
        instance = _mappings_by_root.get(key)
        if instance is None:
            instance = _mappings_by_root[key] = AnimeMappings(Path(key))
        return instance


def rules_from_mapping(mapping: ExternalMapping) -> tuple[EpisodeMappingRule, ...]:
    if (
        mapping.anidb_id is None
        or mapping.tvdb_id is None
        or mapping.tvdb_sentinel
        or mapping.tvdb_absolute
        or mapping.tvdb_season is None
    ):
        return ()
    base = {
        "source_scheme": "anilist",
        "target_scheme": "tvdb",
        "anilist_id": int(mapping.anilist_id),
        "anidb_id": int(mapping.anidb_id),
        "tvdb_id": int(mapping.tvdb_id),
    }
    rules: list[EpisodeMappingRule] = []
    for rule in mapping.rules:
        if rule.target != "tvdb" or rule.target_season is None:
            continue
        episode_type = "special" if rule.anidb_season == 0 else "regular"
        if rule.anidb_season not in (0, 1):
            continue
        for source, targets in rule.pairs:
            if len(targets) != 1:
                continue  # one source episode split over several target episodes: not a 1:1 rule
            rules.append(
                EpisodeMappingRule(
                    **base, target_season=int(rule.target_season), kind="pair",
                    episode_type=episode_type, source_episode=int(source), target_episode=int(targets[0]),
                )
            )
        if rule.start is not None and rule.end is not None and rule.offset is not None and rule.start <= rule.end:
            rules.append(
                EpisodeMappingRule(
                    **base, target_season=int(rule.target_season), kind="range", episode_type=episode_type,
                    start=int(rule.start), end=int(rule.end), offset=int(rule.offset),
                )
            )
    rules.append(
        EpisodeMappingRule(
            **base, target_season=int(mapping.tvdb_season), kind="offset",
            offset=int(mapping.tvdb_episode_offset or 0),
        )
    )
    return tuple(rules)


def rules_for_media(media_id: int, cache_dir: Path) -> tuple[EpisodeMappingRule, ...]:
    media_id = int(media_id or 0)
    if media_id <= 0:
        return ()
    mappings = _mappings(cache_dir)
    try:
        stamp = mappings.index_path.stat().st_mtime if mappings.available else 0.0
    except OSError:
        stamp = 0.0
    if not stamp:
        return ()
    key = (str(mappings.index_path), media_id, stamp)
    cached = _rules_cache.get(key)
    if cached is not None:
        return cached
    found = mappings.lookup_anilist(media_id)
    # Several AniDB entries for one AniList id is an identity question, not a rule.
    result = rules_from_mapping(found[0]) if len(found) == 1 else ()
    if len(_rules_cache) > 4096:
        _rules_cache.clear()
    _rules_cache[key] = result
    return result


def to_target(rules: tuple[EpisodeMappingRule, ...], episode: int) -> tuple[int, int] | None:
    """Regular AniList episode → (tvdb season, tvdb episode); None when unknown/none."""
    regular = [rule for rule in rules if rule.episode_type == "regular"]
    for rule in regular:
        if rule.kind == "pair" and rule.source_episode == int(episode):
            return (rule.target_season, int(rule.target_episode)) if rule.target_episode else None
    for rule in regular:
        if rule.kind == "range" and rule.start <= int(episode) <= rule.end:  # type: ignore[operator]
            target = int(episode) + rule.offset
            return (rule.target_season, target) if target >= 1 else None
    for rule in regular:
        if rule.kind == "offset":
            target = int(episode) + rule.offset
            return (rule.target_season, target) if target >= 1 else None
    return None


def from_target(
    rules: tuple[EpisodeMappingRule, ...],
    season: int,
    episode: int,
    *,
    total_episodes: int | None = None,
) -> int | None:
    """(tvdb season, episode) → regular AniList episode, only when exactly one maps there."""
    if not rules or int(season) == 0:
        return None  # season 0 is specials: never a regular episode
    limit = int(total_episodes) if total_episodes else max(
        [rule.end or 0 for rule in rules] + [rule.source_episode or 0 for rule in rules] + [0]
    ) + 400
    candidates = {
        local
        for local in range(1, limit + 1)
        if to_target(rules, local) == (int(season), int(episode))
    }
    return candidates.pop() if len(candidates) == 1 else None
