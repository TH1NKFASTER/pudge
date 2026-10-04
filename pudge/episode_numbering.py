from __future__ import annotations

import json
import hashlib
import re
import time
from dataclasses import dataclass, replace
from typing import Any

from .config import AppConfig
from .filename import normalize_title, title_similarity
from .models import AniListAnime
from .release_parser import parse_release_name
from .providers.anilist import AniListClient, AniListError
from . import external_episode_numbering as external


RESOLVER_VERSION = 4  # scoped release continuity and reversible cached rules
_CACHE_TTL_SECONDS = 7 * 24 * 3600
_COUNTED_FORMATS = {"TV", "TV_SHORT", "ONA", ""}
_BRIDGE_FORMATS = {"OVA", "SPECIAL", "MOVIE"}
_STAGE_SUFFIX_RE = re.compile(r"(?i)\s*[-:]?\s*\d{1,2}(?:st|nd|rd|th)(?:\s*(?:&|＆|and|[-–—])\s*\d{1,2}(?:st|nd|rd|th))?\s+STAGE\s*$")


def release_series_identity(title: str) -> str:
    return normalize_title(_STAGE_SUFFIX_RE.sub("", str(title)))


def is_split_stage(anime: Any) -> bool:
    return bool(_STAGE_SUFFIX_RE.search(_title(anime)))


@dataclass(frozen=True, slots=True)
class EpisodeNumbering:
    media_episode: int
    release_episode: int
    offset: int
    aliases: tuple[int, ...]
    prequel_titles: tuple[str, ...]
    chain: tuple[int, ...]
    source: str
    resolver_version: int = RESOLVER_VERSION
    # v3, backward compatible: "resolved" | "conflict" (external rule and the
    # AniList chain disagree: no numeric alias is trusted) and the rule used.
    status: str = "resolved"
    rule: str = ""
    # Season-scoped context, e.g. (("tvdb", 2, 5),): only meaningful together
    # with a season marker in a release name; never a global numeric alias.
    season_aliases: tuple[tuple[str, int, int], ...] = ()
    series_identity: str = ""
    alias_offsets: tuple[int, ...] = ()
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReleaseEpisodeMatch:
    status: str
    media_id: int
    requested_media_episode: int | None
    mapped_media_episode: int | None
    raw_release_episode: int | None
    raw_release_season: int | None
    scheme_id: str
    rule_id: str
    rule_revision: int = RESOLVER_VERSION
    evidence: tuple[str, ...] = ()
    rejection_reason: str = ""


def match_release_episode(
    anime: Any,
    parsed_release: Any,
    requested_media_episode: int | None,
    numbering_context: EpisodeNumbering | None = None,
    *,
    alternative_episodes: tuple[int, ...] = (),
    trusted_source: bool = True,
) -> ReleaseEpisodeMatch:
    """Map a release once, then validate the requested local identity.

    Split-stage overlap uses a bounded continuity rule for this storyline.
    Unknown sources retain an ambiguous result for manual inspection.
    Numeric aliases remain discovery hints for other season conventions.
    """
    raw = parsed_release.episode
    season = parsed_release.season
    requested = int(requested_media_episode) if requested_media_episode is not None else None
    scheme, rule, evidence = "local", "explicit-local", ()
    mapped = raw
    status = "resolved"
    if parsed_release.unsafe_single_episode or season == 0:
        mapped, status = None, "rejected"
    elif raw is None or not parsed_release.explicit_episode:
        status = "ambiguous"
    elif is_split_stage(anime):
        offset = numbering_context.offset if numbering_context is not None else 0
        if not offset and requested is not None:
            offsets = [int(value) - requested for value in alternative_episodes if 0 < int(value) - requested <= 3]
            offset = offsets[0] if offsets else 0
        if offset:
            mapped = int(raw) - offset
            group = str(parsed_release.group or "").casefold()
            # The confirmed continuing scale belongs to this release storyline,
            # uploader and notation. Catalog adjacency alone is a proposal.
            bare_scope = _media_id(anime) == 210482 and group == "erai-raws" and season is None
            scene_scope = _media_id(anime) == 210482 and group == "toonshub" and season == 6
            names = [_title(anime), *_value(anime, "titles", [])]
            release_text = normalize_title(parsed_release.raw_name)
            same_story = any(release_series_identity(name) in release_text for name in names if len(release_series_identity(name)) >= 12)
            scheme = "continuous-stage:" + release_series_identity(_title(anime)) + ":" + group + (":bare" if season is None else f":s{season}")
            rule, evidence = "continuous-stage", ("same-storyline", "adjacent-stage-count", "release-source")
            total = _episodes(anime)
            if not same_story:
                mapped, status = None, "rejected"
            elif not trusted_source or not (bare_scope or scene_scope):
                mapped, status = None, "ambiguous"
            elif mapped < 1 or (total and mapped > total):
                mapped, status = None, "rejected"
        elif not re.search(r"(?i)\b1st\s+STAGE\s*$", _title(anime)):
            status = "ambiguous"
    elif requested is not None and raw in alternative_episodes and raw != requested:
        mapped, scheme, rule = requested, "absolute-alias", "discovery-alias"
    if numbering_context is not None and numbering_context.status == "conflict":
        status = "conflict"
    reason = ""
    if mapped is not None and requested is not None and mapped != requested:
        status = "rejected"
        reason = f"mapped-local={mapped} requested-local={requested}"
    revision = RESOLVER_VERSION
    if rule == "continuous-stage":
        scope = json.dumps([RESOLVER_VERSION, _media_id(anime), scheme, offset, _episodes(anime)], separators=(",", ":"))
        revision = int(hashlib.sha256(scope.encode()).hexdigest()[:8], 16)
    return ReleaseEpisodeMatch(status, _media_id(anime), requested, mapped, raw, season, scheme, rule, revision, evidence=evidence, rejection_reason=reason)


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _media_id(anime: Any) -> int:
    raw = _value(anime, "media_id", _value(anime, "id", 0))
    return int(raw or 0)


def _title(anime: Any) -> str:
    direct = str(_value(anime, "title", "") or "").strip()
    if direct:
        return direct
    titles = list(_value(anime, "titles", []) or [])
    return str(titles[0] if titles else "").strip()


def _titles(anime: Any) -> list[str]:
    result: list[str] = []
    for value in [_title(anime), *list(_value(anime, "titles", []) or []), *list(_value(anime, "synonyms", []) or [])]:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _episodes(anime: Any) -> int:
    try:
        return max(0, int(_value(anime, "episodes", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _format(anime: Any) -> str:
    return str(_value(anime, "format", "") or "").upper()


def _airing_year(node: dict[str, Any]) -> int | None:
    try:
        season_year = int(node.get("season_year") or 0)
    except (TypeError, ValueError):
        season_year = 0
    if season_year > 0:
        return season_year
    start = str(node.get("start_date") or "")
    try:
        return int(start[:4]) if len(start) >= 4 else None
    except ValueError:
        return None


def _short_stage_alias(
    current: dict[str, Any],
    candidate: dict[str, Any],
    *,
    media_episode: int,
    counted: int,
) -> int | None:
    """Return a near release alias for an adjacent short split-stage entry."""
    if counted < 1 or counted > 3:
        return None
    if title_similarity(
        str(current.get("title") or ""),
        str(candidate.get("title") or ""),
    ) < 80.0:
        return None
    current_year = _airing_year(current)
    candidate_year = _airing_year(candidate)
    if (
        current_year is None
        or candidate_year is None
        or abs(current_year - candidate_year) > 1
    ):
        return None
    return int(media_episode) + int(counted)


def _as_anilist(anime: Any) -> AniListAnime:
    return AniListAnime(
        id=_media_id(anime),
        titles=_titles(anime),
        synonyms=[str(value) for value in list(_value(anime, "synonyms", []) or [])],
        season_year=_value(anime, "season_year"),
        episodes=_value(anime, "episodes"),
        format=_value(anime, "format"),
    )


def _graph_from_db(config: AppConfig, media_id: int, db: Any | None) -> dict[str, Any] | None:
    try:
        if db is None:
            from .database import Database

            db = Database(config.library.database_path)
        loader = getattr(db, "relation_graph_for_media", None)
        if not callable(loader):
            return None
        cached = loader(int(media_id))
    except Exception:
        return None
    graph = cached.get("graph") if isinstance(cached, dict) else None
    return graph if isinstance(graph, dict) else None


def episode_numbering_from_graph(
    graph: dict[str, Any],
    anime: Any,
    media_episode: int,
) -> EpisodeNumbering | None:
    """Resolve release numbering from one cached AniList relation component.

    OVA/SPECIAL/MOVIE nodes may bridge two TV entries but do not contribute to
    the ordinary release episode counter. This mirrors how release groups keep
    TV episode numbering continuous while AniList can insert side works.
    """

    media_episode = int(media_episode)
    if media_episode < 1:
        return None
    media_id = _media_id(anime)
    nodes = {
        int(node["media_id"]): dict(node)
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("media_id") is not None
    }
    if media_id not in nodes:
        return None

    incoming: dict[int, list[int]] = {}
    for edge in graph.get("edges", []):
        if (
            not isinstance(edge, dict)
            or str(edge.get("relation_type") or "").upper() != "SEQUEL"
        ):
            continue
        try:
            source = int(edge.get("source"))
            target = int(edge.get("target"))
        except (TypeError, ValueError):
            continue
        if source in nodes and target in nodes and source != target:
            incoming.setdefault(target, []).append(source)

    episode_cap = max(100, _episodes(anime) * 4)
    current_id = media_id
    visited = {current_id}
    offset = 0
    predecessors: list[dict[str, Any]] = []
    chain: list[int] = [media_id]
    local_stage_aliases: list[int] = []

    for _ in range(20):
        current = nodes[current_id]
        current_title = str(current.get("title") or _title(anime))
        candidates: list[tuple[float, bool, int, dict[str, Any]]] = []
        for source_id in incoming.get(current_id, []):
            if source_id in visited:
                continue
            candidate = nodes[source_id]
            candidate_title = str(candidate.get("title") or "")
            if (_STAGE_SUFFIX_RE.search(current_title) or _STAGE_SUFFIX_RE.search(candidate_title)) and release_series_identity(current_title) != release_series_identity(candidate_title):
                continue
            fmt = str(candidate.get("format") or "").upper()
            try:
                count = int(candidate.get("episodes") or 0)
            except (TypeError, ValueError):
                count = 0
            if fmt in _COUNTED_FORMATS:
                if count < 1 or count > episode_cap:
                    continue
                counted = count
            elif fmt in _BRIDGE_FORMATS:
                counted = 0
            else:
                continue
            continuity = title_similarity(
                current_title,
                str(candidate.get("title") or ""),
            )
            if continuity < 35.0:
                continue
            candidates.append((continuity, counted > 0, counted, candidate))
        if not candidates:
            break

        _score, _counted_first, counted, candidate = max(
            candidates,
            key=lambda item: (
                item[0],
                item[1],
                str(item[3].get("start_date") or ""),
                int(item[3].get("media_id") or 0),
            ),
        )
        # Some streaming releases split one release-season into separate AniList
        # entries.  A very short, adjacent direct prequel is then numbered as the
        # first episode(s) of the same scene season even though the wider franchise
        # graph may continue for hundreds of episodes.  Preserve that near alias in
        # addition to the full absolute number.  This is intentionally narrow so a
        # normal 12/24-episode previous season cannot become an accepted alias.
        if not predecessors:
            short_stage_alias = _short_stage_alias(
                current,
                candidate,
                media_episode=media_episode,
                counted=counted,
            )
            if short_stage_alias is not None:
                local_stage_aliases.append(short_stage_alias)

        offset += counted
        current_id = int(candidate["media_id"])
        visited.add(current_id)
        predecessors.insert(0, candidate)
        chain.insert(0, current_id)

    release_episode = media_episode + offset
    titles = tuple(
        dict.fromkeys(
            str(item.get("title") or "").strip()
            for item in predecessors
            if str(item.get("format") or "").upper() in _COUNTED_FORMATS
            and str(item.get("title") or "").strip()
        )
    )
    aliases = tuple(
        dict.fromkeys(
            [
                *local_stage_aliases,
                *([release_episode] if offset else []),
            ]
        )
    )
    return EpisodeNumbering(
        media_episode=media_episode,
        release_episode=release_episode,
        offset=offset,
        aliases=aliases,
        prequel_titles=titles,
        chain=tuple(chain),
        source="relation_graph",
        rule="continuous-stage" if is_split_stage(anime) and offset else "graph-offset",
        series_identity=release_series_identity(_title(anime)),
        alias_offsets=tuple(value - media_episode for value in aliases),
        evidence=("adjacent-stage-count",) if is_split_stage(anime) and offset else (),
    )


def _cache_paths(config: AppConfig, media_id: int) -> tuple[Any, Any]:
    return (
        config.paths.cache_dir / "anilist-release-numbering" / f"{media_id}.json",
        config.paths.cache_dir / "anilist-episode-offset" / f"{media_id}.json",
    )


def _read_cache(
    path: Any, *, media_episode: int, allow_legacy: bool = True
) -> EpisodeNumbering | None:
    try:
        if not path.is_file() or time.time() - path.stat().st_mtime >= _CACHE_TTL_SECONDS:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or "offset" not in payload:
            return None
        version = int(payload.get("resolver_version", 0))
        if version < RESOLVER_VERSION and not allow_legacy:
            return None
        # v1 caches did not write resolver_version. They are still useful as an
        # offline basis for absolute -> season-local conversion, but live
        # relative-number lookups should refresh them to the current resolver.
        offset = max(0, int(payload.get("offset", 0)))
        chain = tuple(int(value) for value in payload.get("chain", []) if int(value) > 0)
        titles = tuple(
            str(value).strip()
            for value in payload.get("prequel_titles", [])
            if str(value).strip()
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    absolute = int(media_episode) + offset
    return EpisodeNumbering(
        media_episode=int(media_episode),
        release_episode=absolute,
        offset=offset,
        aliases=tuple(int(media_episode) + int(value) for value in payload.get("alias_offsets", [offset] if offset else [])),
        prequel_titles=titles,
        chain=chain,
        source=path.parent.name,
        resolver_version=max(2, int(payload.get("resolver_version", 2))),
        status=str(payload.get("status") or "resolved"),
        rule=str(payload.get("rule") or ""),
        series_identity=str(payload.get("series_identity") or ""),
        alias_offsets=tuple(int(value) for value in payload.get("alias_offsets", [])),
        evidence=tuple(str(value) for value in payload.get("evidence", [])),
    )


def _write_caches(
    config: AppConfig,
    anime: Any,
    result: EpisodeNumbering,
) -> None:
    media_id = _media_id(anime)
    release_path, jimaku_path = _cache_paths(config, media_id)
    payload = {
        "media_id": media_id,
        "offset": int(result.offset),
        "chain": list(result.chain),
        "prequel_titles": list(result.prequel_titles),
        "resolver_version": RESOLVER_VERSION,
        "alias_offsets": list(result.alias_offsets or tuple(value - result.media_episode for value in result.aliases)),
        "rule": result.rule,
        "status": result.status,
        "series_identity": result.series_identity,
        "evidence": list(result.evidence),
        "updated_at": time.time(),
    }
    for path in (release_path, jimaku_path):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            continue


def _external_rules(anime: Any, config: AppConfig) -> tuple[external.EpisodeMappingRule, ...]:
    try:
        return external.rules_for_media(_media_id(anime), config.paths.cache_dir)
    except Exception:  # noqa: BLE001 - optional index: never block numbering
        return ()


def has_external_rules(anime: Any, config: AppConfig) -> bool:
    return bool(_external_rules(anime, config))


def resolve_episode_numbering(
    anime: Any,
    media_episode: int,
    config: AppConfig,
    logger: Any,
    *,
    db: Any | None = None,
    allow_network: bool = True,
) -> EpisodeNumbering:
    """AniList-local episode → release numbering.

    An exact external rule (Anime-Lists, via the local index) is applied
    first. It never overwrites the offset caches. When it contradicts the
    AniList relation chain, the result is marked ``status="conflict"`` and
    carries no numeric alias instead of silently picking one.
    """
    base = _resolve_base(anime, media_episode, config, logger, db=db, allow_network=allow_network)
    if _format(anime) not in _COUNTED_FORMATS:
        return base
    rules = _external_rules(anime, config)
    if not rules:
        return base
    target = external.to_target(rules, int(media_episode))
    if target is None:
        return base
    season, target_episode = target
    season_alias = (("tvdb", int(season), int(target_episode)),)
    rule_text = f"tvdb:{rules[0].tvdb_id} S{season:02d}E{target_episode:02d}"
    if season != 1:
        # A later TVDB season is season-local: "S02E05" needs the season marker.
        return replace(base, season_aliases=season_alias, rule=rule_text)
    external_offset = int(target_episode) - int(media_episode)
    if base.offset and external_offset != base.offset:
        if logger is not None:
            logger.info(
                "CONFLICT step=episode_numbering media_id=%s episode=%s graph_offset=%s external=%s",
                _media_id(anime), media_episode, base.offset, rule_text,
            )
        return replace(base, aliases=(), status="conflict", rule=rule_text, season_aliases=season_alias)
    if external_offset <= 0:
        return replace(base, season_aliases=season_alias, rule=rule_text)
    return replace(
        base,
        release_episode=int(target_episode),
        offset=external_offset,
        aliases=(int(target_episode),),
        source=base.source if base.offset else "external-tvdb",
        rule=rule_text,
        season_aliases=season_alias,
    )


def _resolve_base(
    anime: Any,
    media_episode: int,
    config: AppConfig,
    logger: Any,
    *,
    db: Any | None = None,
    allow_network: bool = True,
) -> EpisodeNumbering:
    media_episode = int(media_episode)
    if media_episode < 1:
        raise ValueError("media_episode must be >= 1")
    media_id = _media_id(anime)
    if not media_id or _format(anime) not in _COUNTED_FORMATS:
        return EpisodeNumbering(
            media_episode=media_episode,
            release_episode=media_episode,
            offset=0,
            aliases=(),
            prequel_titles=(),
            chain=(media_id,) if media_id else (),
            source="local",
        )

    graph = _graph_from_db(config, media_id, db)
    if graph is not None:
        graph_result = episode_numbering_from_graph(graph, anime, media_episode)
        has_incoming = any(
            isinstance(edge, dict)
            and str(edge.get("relation_type") or "").upper() == "SEQUEL"
            and str(edge.get("target") or "") == str(media_id)
            for edge in graph.get("edges", [])
        )
        # Zero is authoritative for an actual root entry. For a node that does
        # have a prequel edge, a zero result means the cached graph was partial
        # or continuity filtering could not prove the chain; allow cache/live
        # fallback instead of freezing a false season-local numbering.
        if graph_result is not None and (graph_result.offset > 0 or not has_incoming):
            _write_caches(config, anime, graph_result)
            if logger is not None:
                logger.info(
                    "RESULT step=episode_numbering media_id=%s relative=%s absolute=%s "
                    "offset=%s source=relation_graph",
                    media_id,
                    media_episode,
                    graph_result.release_episode,
                    graph_result.offset,
                )
            return graph_result

    release_path, jimaku_path = _cache_paths(config, media_id)
    cached = [
        item
        for item in (
            _read_cache(
                release_path,
                media_episode=media_episode,
                allow_legacy=(not is_split_stage(anime) and (not allow_network or media_episode == 1)),
            ),
            _read_cache(
                jimaku_path,
                media_episode=media_episode,
                allow_legacy=(not is_split_stage(anime) and (not allow_network or media_episode == 1)),
            ),
        )
        if item is not None and item.offset > 0
        and (not is_split_stage(anime) or item.rule == "continuous-stage")
    ]
    if cached:
        best = max(
            cached,
            key=lambda item: (
                item.resolver_version,
                item.source == "anilist-release-numbering",
            ),
        )
        if logger is not None:
            logger.info(
                "RESULT step=episode_numbering media_id=%s relative=%s absolute=%s "
                "offset=%s source=%s",
                media_id,
                media_episode,
                best.release_episode,
                best.offset,
                best.source,
            )
        return best

    if allow_network and bool(config.anilist.enabled):
        client = AniListClient(
            config.anilist.endpoint,
            access_token=config.anilist.access_token,
        )
        try:
            absolute, chain = client.absolute_episode_number(
                _as_anilist(anime),
                media_episode,
            )
        except (AniListError, OSError, ValueError) as exc:
            if logger is not None:
                logger.info(
                    "SKIP step=episode_numbering media_id=%s episode=%s reason=%r",
                    media_id,
                    media_episode,
                    exc,
                )
        else:
            offset = max(0, int(absolute) - media_episode)
            stage_rule = ""
            if is_split_stage(anime):
                # A split-stage entry continues only its own storyline: the live
                # franchise chain (e.g. every earlier JoJo part) is not its scale.
                story = release_series_identity(_title(anime))
                offset = 0
                kept = [chain[-1]] if chain else []
                for item in reversed(chain[:-1]):
                    if str(item.format or "").upper() in _BRIDGE_FORMATS:
                        continue
                    if not any(release_series_identity(name) == story for name in [*item.titles, *item.synonyms] if name):
                        break
                    offset += max(0, int(item.episodes or 0))
                    kept.insert(0, item)
                chain = kept
                absolute = media_episode + offset
                stage_rule = "continuous-stage"
            predecessor_titles: list[str] = []
            for item in chain[:-1]:
                if str(item.format or "").upper() in _BRIDGE_FORMATS:
                    continue
                for value in [*item.titles, *item.synonyms]:
                    text = str(value or "").strip()
                    if text and text not in predecessor_titles:
                        predecessor_titles.append(text)
            result = EpisodeNumbering(
                media_episode=media_episode,
                release_episode=int(absolute),
                offset=offset,
                aliases=(int(absolute),) if offset else (),
                prequel_titles=tuple(predecessor_titles),
                chain=tuple(int(item.id) for item in chain),
                source="anilist-live-v2",
                rule=stage_rule if offset else "",
                evidence=("same-storyline", "adjacent-stage-count", "live-chain") if stage_rule and offset else (),
            )
            _write_caches(config, anime, result)
            if logger is not None:
                logger.info(
                    "RESULT step=episode_numbering media_id=%s relative=%s absolute=%s "
                    "offset=%s source=anilist-live-v2",
                    media_id,
                    media_episode,
                    absolute,
                    offset,
                )
            return result
        finally:
            client.close()

    return EpisodeNumbering(
        media_episode=media_episode,
        release_episode=media_episode,
        offset=0,
        aliases=(),
        prequel_titles=(),
        chain=(media_id,),
        source="local",
    )



def aliases_from_offset(anime: Any, episode_hint: int, offset: int) -> tuple[int, ...]:
    hint = int(episode_hint)
    offset = max(0, int(offset))
    if hint < 1 or not offset:
        return ()
    total = _episodes(anime)
    if total and hint > total:
        relative = hint - offset
        return (relative,) if 1 <= relative <= total else ()
    absolute = hint + offset
    return (absolute,) if absolute != hint else ()

def episode_aliases_for_hint(
    anime: Any,
    episode_hint: int,
    config: AppConfig,
    logger: Any,
    *,
    db: Any | None = None,
    allow_network: bool = True,
) -> tuple[int, ...]:
    """Return the opposite numbering form for Jimaku/release matching."""

    hint = int(episode_hint)
    if hint < 1 or _format(anime) not in _COUNTED_FORMATS:
        return ()
    total = _episodes(anime)
    if total and hint > total:
        rules = _external_rules(anime, config)
        if rules:
            # Piecewise inverse: the rule that actually covers this number,
            # not the offset of episode 1.
            local = external.from_target(rules, 1, hint, total_episodes=total)
            if local is not None:
                return (local,)
        basis = resolve_episode_numbering(
            anime,
            1,
            config,
            logger,
            db=db,
            allow_network=allow_network,
        )
        if basis.offset:
            relative = hint - basis.offset
            if 1 <= relative <= total:
                return (relative,)
        return ()
    result = resolve_episode_numbering(
        anime,
        hint,
        config,
        logger,
        db=db,
        allow_network=allow_network,
    )
    return result.aliases


def media_episode_from_release(
    anime: Any | None,
    release_episode: int | None,
    config: AppConfig,
    logger: Any,
    *,
    requested_media_episode: int | None = None,
    db: Any | None = None,
    allow_network: bool = False,
    release_season: int | None = None,
    release_name: str = "",
) -> int | None:
    if release_name:
        parsed = parse_release_name(release_name)
        release_season = parsed.season
        if parsed.unsafe_single_episode:
            return None
    if release_episode is None or release_season == 0:
        return None
    value = int(release_episode)
    if anime is None:
        return value if requested_media_episode in (None, value) else None
    total = _episodes(anime)
    rules = _external_rules(anime, config) if _format(anime) in _COUNTED_FORMATS else ()
    basis = _resolve_base(anime, 1, config, logger, db=db, allow_network=allow_network)
    if is_split_stage(anime) and release_name:
        match = match_release_episode(anime, parsed, requested_media_episode, basis, trusted_source=True)
        return match.mapped_media_episode if match.status == "resolved" else None
    if is_split_stage(anime) and basis.offset:
        local = value - basis.offset
        if local < 1 or (total and local > total) or requested_media_episode not in (None, local):
            return None
        return local
    if release_season is not None and rules:
        if int(release_season) == 0:
            return None  # S00Exx is a special, never a regular episode
        local = external.from_target(rules, int(release_season), value, total_episodes=total or None)
        if local is not None:
            return local
    if value >= 1 and (not total or value <= total):
        return value if requested_media_episode in (None, value) else None
    external_local = external.from_target(rules, 1, value, total_episodes=total or None) if rules else None
    basis = _resolve_base(
        anime,
        1,
        config,
        logger,
        db=db,
        allow_network=allow_network,
    )
    graph_local = None
    if basis.offset:
        local = value - basis.offset
        if local >= 1 and (not total or local <= total):
            graph_local = local
    if external_local is not None and graph_local is not None and external_local != graph_local:
        # Ambiguous: never import/track progress on a guess.
        if logger is not None:
            logger.info(
                "CONFLICT step=episode_numbering.inverse media_id=%s release=%s external=%s graph=%s",
                _media_id(anime), value, external_local, graph_local,
            )
        return None
    resolved = external_local if external_local is not None else graph_local
    if requested_media_episode is not None:
        requested = int(requested_media_episode)
        if resolved is not None:
            return resolved if resolved == requested else None
        # Managed completed downloads retain their explicit season-local owner
        # when an offline catalog lacks the rule for an out-of-range filename.
        # Split-stage overlaps took the guarded path above; an unresolved stage
        # must never inherit the historical request as identity evidence.
        if not is_split_stage(anime) and requested >= 1 and (not total or requested <= total):
            return requested
    return resolved
