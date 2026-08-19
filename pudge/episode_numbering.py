from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .config import AppConfig
from .filename import title_similarity
from .models import AniListAnime
from .providers.anilist import AniListClient, AniListError


RESOLVER_VERSION = 2
_CACHE_TTL_SECONDS = 7 * 24 * 3600
_COUNTED_FORMATS = {"TV", "TV_SHORT", "ONA", ""}
_BRIDGE_FORMATS = {"OVA", "SPECIAL", "MOVIE"}


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

    for _ in range(20):
        current = nodes[current_id]
        current_title = str(current.get("title") or _title(anime))
        candidates: list[tuple[float, bool, int, dict[str, Any]]] = []
        for source_id in incoming.get(current_id, []):
            if source_id in visited:
                continue
            candidate = nodes[source_id]
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
    return EpisodeNumbering(
        media_episode=media_episode,
        release_episode=release_episode,
        offset=offset,
        aliases=(release_episode,) if offset else (),
        prequel_titles=titles,
        chain=tuple(chain),
        source="relation_graph",
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
        if version < 2 and not allow_legacy:
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
        aliases=(absolute,) if offset else (),
        prequel_titles=titles,
        chain=chain,
        source=path.parent.name,
        resolver_version=max(2, int(payload.get("resolver_version", 2))),
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


def resolve_episode_numbering(
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
                allow_legacy=(not allow_network or media_episode == 1),
            ),
            _read_cache(
                jimaku_path,
                media_episode=media_episode,
                allow_legacy=(not allow_network or media_episode == 1),
            ),
        )
        if item is not None and item.offset > 0
    ]
    if cached:
        best = max(
            cached,
            key=lambda item: (
                item.resolver_version,
                item.offset,
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
) -> int | None:
    if requested_media_episode is not None:
        return int(requested_media_episode)
    if release_episode is None:
        return None
    value = int(release_episode)
    if anime is None:
        return value
    total = _episodes(anime)
    if value >= 1 and (not total or value <= total):
        return value
    basis = resolve_episode_numbering(
        anime,
        1,
        config,
        logger,
        db=db,
        allow_network=allow_network,
    )
    if basis.offset:
        local = value - basis.offset
        if local >= 1 and (not total or local <= total):
            return local
    return None
