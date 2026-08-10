from __future__ import annotations

from typing import Any
import time

import httpx

from ..filename import normalize_title, title_similarity
from ..branding import APP_SLUG
from ..models import AniListAnime, VideoIdentity
from ..manager_models import LibraryAnime


QUERY = """
query ($search: String!) {
  Page(page: 1, perPage: 12) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      title { romaji english native userPreferred }
      synonyms
      seasonYear
      episodes
      format
    }
  }
}
"""

VIEWER_QUERY = """
query {
  Viewer {
    id
    name
  }
}
"""

LIST_ENTRY_QUERY = """
query ($mediaId: Int!) {
  Media(id: $mediaId, type: ANIME) {
    id
    title { romaji english native userPreferred }
    synonyms
    seasonYear
    startDate { year month day }
    episodes
    format
    mediaListEntry {
      id
      progress
      status
      score(format: POINT_10)
    }
  }
}
"""


RELATIONS_QUERY = """
query ($mediaId: Int!) {
  Media(id: $mediaId, type: ANIME) {
    id
    title { romaji english native userPreferred }
    synonyms
    seasonYear
    startDate { year month day }
    episodes
    format
    relations {
      edges {
        relationType
        node {
          id
          title { romaji english native userPreferred }
          synonyms
          seasonYear
          startDate { year month day }
          episodes
          format
        }
      }
    }
  }
}
"""


DIRECT_RELATIONS_QUERY = """
fragment DirectRelationLeaf on Media {
  id
  type
  title { romaji english native userPreferred }
  episodes
  format
  seasonYear
  startDate { year month day }
  status
  siteUrl
  coverImage { extraLarge large medium }
  studios { nodes { name isAnimationStudio } }
  mediaListEntry { status progress }
}

query ($mediaId: Int!) {
  Media(id: $mediaId, type: ANIME) {
    id
    relations {
      edges {
        relationType
        node {
          ...DirectRelationLeaf
          relations {
            edges {
              relationType
              node { ...DirectRelationLeaf }
            }
          }
        }
      }
    }
  }
}
"""





FULL_RELATION_GRAPH_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      type
      title { romaji english native userPreferred }
      episodes
      format
      seasonYear
      startDate { year month day }
      status
      siteUrl
      coverImage { extraLarge large medium }
      studios { nodes { name isAnimationStudio } }
      mediaListEntry { status progress }
      relations {
        edges {
          relationType
          node {
            id
            type
            format
            title { romaji english native userPreferred }
          }
        }
      }
    }
  }
}
"""

LIBRARY_QUERY = """
fragment RelationLeaf on Media {
  id
  type
  title { romaji english native userPreferred }
  episodes
  format
  seasonYear
  startDate { year month day }
  status
  siteUrl
  coverImage { extraLarge large medium }
  studios { nodes { name isAnimationStudio } }
  mediaListEntry {
    status
    progress
  }
}

query ($userId: Int!) {
  MediaListCollection(userId: $userId, type: ANIME) {
    lists {
      status
      entries {
        progress
        status
        score(format: POINT_10)
        media {
          id
          title { romaji english native userPreferred }
          synonyms
          episodes
          format
          seasonYear
          startDate { year month day }
          status
          endDate { year month day }
          meanScore
          duration
          siteUrl
          coverImage { extraLarge large medium }
          studios { nodes { name isAnimationStudio } }
          nextAiringEpisode { episode airingAt }
          relations {
            edges {
              relationType
              node {
                ...RelationLeaf
                relations {
                  edges {
                    relationType
                    node { ...RelationLeaf }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""




LIBRARY_FALLBACK_QUERY = """
query ($userId: Int!) {
  MediaListCollection(userId: $userId, type: ANIME) {
    lists {
      status
      entries {
        progress
        status
        score(format: POINT_10)
        media {
          id
          title { romaji english native userPreferred }
          synonyms
          episodes
          format
          seasonYear
          startDate { year month day }
          status
          endDate { year month day }
          meanScore
          duration
          siteUrl
          coverImage { extraLarge large medium }
          studios { nodes { name isAnimationStudio } }
          nextAiringEpisode { episode airingAt }
        }
      }
    }
  }
}
"""

MINIMAL_LIST_ENTRY_QUERY = """
query ($mediaId: Int!) {
  Media(id: $mediaId, type: ANIME) {
    id
    mediaListEntry {
      id
      progress
      status
      score(format: POINT_10)
    }
  }
}
"""

AIRING_SCHEDULE_QUERY = """
query ($mediaId: Int!, $episode: Int!) {
  AiringSchedule(mediaId: $mediaId, episode: $episode) {
    episode
    airingAt
  }
}
"""


SAVE_STATUS_MUTATION = """
mutation ($mediaId: Int!, $status: MediaListStatus!) {
  SaveMediaListEntry(mediaId: $mediaId, status: $status) {
    id
    progress
    status
    score(format: POINT_10)
  }
}
"""

SAVE_SCORE_MUTATION = """
mutation ($mediaId: Int!, $scoreRaw: Int!) {
  SaveMediaListEntry(mediaId: $mediaId, scoreRaw: $scoreRaw) {
    id
    progress
    status
    score(format: POINT_10)
  }
}
"""

DELETE_LIST_ENTRY_MUTATION = """
mutation ($id: Int!) {
  DeleteMediaListEntry(id: $id) { deleted }
}
"""

SAVE_PROGRESS_MUTATION = """
mutation ($mediaId: Int!, $progress: Int!, $status: MediaListStatus) {
  SaveMediaListEntry(mediaId: $mediaId, progress: $progress, status: $status) {
    id
    progress
    status
  }
}
"""


class AniListError(RuntimeError):
    pass


class AniListHTTPError(AniListError):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = int(status_code)


def _as_title_list(media: dict[str, Any]) -> list[str]:
    title = media.get("title") or {}
    values = [title.get("romaji"), title.get("english"), title.get("native"), title.get("userPreferred")]
    return list(dict.fromkeys(str(v) for v in values if v))


_FRANCHISE_NOISE_TERMS = {
    "anime",
    "chapter",
    "episode",
    "episodes",
    "final",
    "movie",
    "part",
    "season",
    "series",
    "special",
    "the",
}


def _franchise_title_parts(media: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Return meaningful title terms and compact aliases for franchise checks."""
    terms: set[str] = set()
    compact: set[str] = set()
    for title in _as_title_list(media):
        normalized = normalize_title(title)
        if not normalized:
            continue
        tokens = [token for token in normalized.split() if token]
        joined = "".join(tokens)
        if len(joined) >= 5:
            compact.add(joined)
        for token in tokens:
            if len(token) >= 5 and token not in _FRANCHISE_NOISE_TERMS:
                terms.add(token)
    return terms, compact


def _parent_matches_root_franchise(
    root_media: dict[str, Any] | None,
    parent_node: dict[str, Any],
) -> bool:
    """Reject crossover parents that would pull an unrelated franchise.

    AniList can model joke crossovers with multiple PARENT edges.  Starting
    from Monogatari, for example, Nisekoimonogatari points both to
    Bakemonogatari and Nisekoi.  A parent is allowed to expand only when its
    titles share a meaningful franchise term with the graph root.
    """
    if not isinstance(root_media, dict):
        return True
    root_terms, root_compact = _franchise_title_parts(root_media)
    parent_terms, parent_compact = _franchise_title_parts(parent_node)
    if not root_terms or not parent_compact:
        return False
    if any(term in alias for term in root_terms for alias in parent_compact):
        return True
    return any(term in alias for term in parent_terms for alias in root_compact)



def _as_date_string(value: Any) -> str | None:
    """Preserve AniList partial dates (year or year-month).

    AniList commonly omits the exact day for upcoming or recently announced
    anime. Dropping the whole date made same-year relation ordering fall back to
    December 31 and could place a summer follow-up before a spring TV series.
    """
    if not isinstance(value, dict):
        return None
    try:
        year = int(value.get("year") or 0)
        month = int(value.get("month") or 0)
        day = int(value.get("day") or 0)
    except (TypeError, ValueError):
        return None
    if year <= 0:
        return None
    if month <= 0:
        return f"{year:04d}"
    if not 1 <= month <= 12:
        return None
    if day <= 0:
        return f"{year:04d}-{month:02d}"
    if not 1 <= day <= 31:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"



def _primary_studio(media: dict[str, Any]) -> str:
    nodes = ((media.get("studios") or {}).get("nodes") or [])
    valid = [node for node in nodes if isinstance(node, dict) and node.get("name")]
    for node in valid:
        if node.get("isAnimationStudio") is True:
            return str(node["name"])
    return str(valid[0]["name"]) if valid else ""


def _relation_watched(node: dict[str, Any]) -> tuple[str, int, bool]:
    entry = node.get("mediaListEntry") or {}
    list_status = str(entry.get("status") or "").upper()
    progress = int(entry.get("progress") or 0)
    episodes = int(node["episodes"]) if node.get("episodes") else None
    watched = list_status in {"COMPLETED", "REPEATING"} or bool(
        episodes and progress >= episodes
    )
    return list_status, progress, watched


def _relation_node_payload(
    node: dict[str, Any],
    relation_type: str,
    *,
    include_children: bool,
) -> dict[str, Any] | None:
    if str(node.get("type") or "ANIME").upper() != "ANIME" or not node.get("id"):
        return None
    media_id = int(node["id"])
    titles = _as_title_list(node)
    cover = node.get("coverImage") or {}
    list_status, progress, watched = _relation_watched(node)
    payload: dict[str, Any] = {
        "relation_type": relation_type,
        "media_id": media_id,
        "title": titles[0] if titles else str(media_id),
        "site_url": str(node.get("siteUrl") or f"https://anilist.co/anime/{media_id}"),
        "format": str(node.get("format")) if node.get("format") else None,
        "season_year": int(node["seasonYear"]) if node.get("seasonYear") else None,
        "start_date": _as_date_string(node.get("startDate")),
        "studio": _primary_studio(node),
        "episodes": int(node["episodes"]) if node.get("episodes") else None,
        "cover_url": str(cover.get("extraLarge") or cover.get("large") or cover.get("medium") or ""),
        "media_status": str(node.get("status") or ""),
        "list_status": list_status,
        "progress": progress,
        "watched": watched,
    }
    if include_children:
        payload["relations"] = _as_direct_relations(node, include_children=False)
    return payload


def _as_direct_relations(
    media: dict[str, Any],
    *,
    include_children: bool = True,
) -> list[dict[str, Any]]:
    """Return anime relation links with one additional relation depth."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    edges = ((media.get("relations") or {}).get("edges") or [])
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        relation_type = str(edge.get("relationType") or "").upper()
        node = edge.get("node")
        if not relation_type or not isinstance(node, dict):
            continue
        payload = _relation_node_payload(
            node,
            relation_type,
            include_children=include_children,
        )
        if payload is None:
            continue
        key = (relation_type, int(payload["media_id"]))
        if key in seen:
            continue
        seen.add(key)
        result.append(payload)

    result.sort(
        key=lambda item: (
            item["relation_type"] != "PREQUEL",
            -(item["season_year"] or 0)
            if item["relation_type"] == "PREQUEL"
            else (item["season_year"] or 9999),
            item["title"].casefold(),
            item["media_id"],
        )
    )
    return result

def _overlay_relation_list_entries(
    relations: list[dict[str, Any]],
    entries: dict[int, dict[str, Any]],
) -> None:
    """Apply the viewer's full AniList collection state to relation nodes.

    ``mediaListEntry`` inside nested relation fragments is not consistently
    populated by AniList.  The MediaListCollection response itself is
    authoritative, including COMPLETED/REPEATING entries that are not shown in
    pudge's CURRENT/PLANNING lists.
    """
    for item in relations:
        try:
            media_id = int(item.get("media_id"))
        except (TypeError, ValueError):
            continue
        entry = entries.get(media_id)
        if entry is not None:
            list_status = str(entry.get("status") or "").upper()
            progress = int(entry.get("progress") or 0)
            episodes_value = item.get("episodes") or entry.get("episodes")
            episodes = int(episodes_value) if episodes_value else None
            item["list_status"] = list_status
            item["progress"] = progress
            item["watched"] = list_status in {"COMPLETED", "REPEATING"} or bool(
                episodes and progress >= episodes
            )
        children = item.get("relations")
        if isinstance(children, list):
            _overlay_relation_list_entries(children, entries)


def _as_anime(media: dict[str, Any], *, score: float = 0.0) -> AniListAnime:
    episodes = media.get("episodes")
    return AniListAnime(
        id=int(media["id"]),
        titles=_as_title_list(media),
        synonyms=[str(value) for value in (media.get("synonyms") or []) if value],
        season_year=int(media["seasonYear"]) if media.get("seasonYear") else None,
        episodes=int(episodes) if episodes else None,
        format=str(media.get("format")) if media.get("format") else None,
        score=score,
    )


class AniListClient:
    def __init__(self, endpoint: str, access_token: str = "", *, timeout: float = 20.0) -> None:
        self.endpoint = endpoint
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": APP_SLUG,
        }
        if access_token.strip():
            headers["Authorization"] = f"Bearer {access_token.strip()}"
        self.client = httpx.Client(
            timeout=max(1.0, float(timeout)),
            follow_redirects=True,
            headers=headers,
        )
        self.last_library_warning = ""
        self.last_library_used_fallback = False

    def close(self) -> None:
        self.client.close()

    def _post(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        transient_statuses = {500, 502, 503, 504}
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self.client.post(
                    self.endpoint,
                    json={"query": query, "variables": variables or {}},
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = int(exc.response.status_code)
                if status in transient_statuses and attempt == 0:
                    time.sleep(0.25)
                    continue
                raise AniListHTTPError(
                    f"Ошибка AniList: HTTP {status} для {self.endpoint}",
                    status_code=status,
                ) from exc
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.25)
                    continue
                raise AniListError(f"Ошибка AniList: {exc}") from exc
            except ValueError as exc:
                raise AniListError(f"Ошибка AniList: некорректный JSON: {exc}") from exc

            if payload.get("errors"):
                raise AniListError(f"AniList вернул ошибку: {payload['errors']}")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise AniListError("AniList вернул ответ без data")
            return data

        raise AniListError(f"Ошибка AniList: {last_error}")

    def viewer(self) -> dict[str, Any]:
        viewer = self._post(VIEWER_QUERY).get("Viewer")
        if not isinstance(viewer, dict):
            raise AniListError("Не удалось определить пользователя AniList")
        return viewer


    def library(self, *, include_relations: bool = True) -> list[LibraryAnime]:
        viewer = self.viewer()
        self.last_library_warning = ""
        self.last_library_used_fallback = False
        if not include_relations:
            data = self._post(
                LIBRARY_FALLBACK_QUERY,
                {"userId": int(viewer["id"])},
            )
        else:
            try:
                data = self._post(LIBRARY_QUERY, {"userId": int(viewer["id"])})
            except AniListHTTPError as exc:
                if exc.status_code < 500:
                    raise
                self.last_library_used_fallback = True
                self.last_library_warning = (
                    "AniList вернул HTTP 500 на расширенный запрос; "
                    "использован упрощённый запрос, данные графа оставлены из кэша"
                )
                data = self._post(
                    LIBRARY_FALLBACK_QUERY,
                    {"userId": int(viewer["id"])},
                )
        collection = data.get("MediaListCollection") or {}
        groups = collection.get("lists") or []
        list_entries: dict[int, dict[str, Any]] = {}
        for group in groups:
            group_status = str(group.get("status") or "")
            for entry in group.get("entries") or []:
                media = entry.get("media") or {}
                if not media.get("id"):
                    continue
                list_entries[int(media["id"])] = {
                    "status": str(entry.get("status") or group_status or "").upper(),
                    "progress": int(entry.get("progress") or 0),
                    "episodes": int(media["episodes"]) if media.get("episodes") else None,
                }

        result: list[LibraryAnime] = []
        for group in groups:
            group_status = str(group.get("status") or "")
            if group_status not in {"CURRENT", "PLANNING"}:
                continue
            for entry in group.get("entries") or []:
                media = entry.get("media") or {}
                if not media.get("id"):
                    continue
                titles = _as_title_list(media)
                cover = media.get("coverImage") or {}
                airing = media.get("nextAiringEpisode") or {}
                entry_status = str(entry.get("status") or group.get("status") or "")
                relations = _as_direct_relations(media)
                _overlay_relation_list_entries(relations, list_entries)
                result.append(
                    LibraryAnime(
                        media_id=int(media["id"]),
                        title=(titles[0] if titles else str(media["id"])),
                        titles=titles,
                        synonyms=[str(x) for x in (media.get("synonyms") or []) if x],
                        cover_url=str(cover.get("extraLarge") or cover.get("large") or cover.get("medium") or ""),
                        site_url=str(media.get("siteUrl") or f"https://anilist.co/anime/{media['id']}"),
                        status=entry_status,
                        progress=int(entry.get("progress") or 0),
                        episodes=int(media["episodes"]) if media.get("episodes") else None,
                        format=str(media.get("format")) if media.get("format") else None,
                        season_year=int(media["seasonYear"]) if media.get("seasonYear") else None,
                        start_date=_as_date_string(media.get("startDate")),
                        studio=_primary_studio(media),
                        media_status=str(media.get("status")) if media.get("status") else None,
                        end_date=_as_date_string(media.get("endDate")),
                        mean_score=int(media["meanScore"]) if media.get("meanScore") is not None else None,
                        user_score=float(entry["score"]) if entry.get("score") is not None else None,
                        duration=int(media["duration"]) if media.get("duration") is not None else None,
                        next_airing_episode=(
                            int(airing["episode"]) if airing.get("episode") else None
                        ),
                        next_airing_at=int(airing["airingAt"]) if airing.get("airingAt") else None,
                        relations=relations,
                    )
                )
        result.sort(key=lambda item: (item.status != "CURRENT", item.title.casefold()))
        return result

    def library_compact(self) -> list[LibraryAnime]:
        """Fetch list/progress metadata without recalculating relation trees."""
        return self.library(include_relations=False)

    def search(self, identity: VideoIdentity) -> list[AniListAnime]:
        data = self._post(QUERY, {"search": identity.title})
        media_items = ((data.get("Page") or {}).get("media") or [])
        result: list[AniListAnime] = []
        for media in media_items:
            titles = _as_title_list(media)
            synonyms = [str(value) for value in (media.get("synonyms") or []) if value]
            all_names = [*titles, *synonyms]
            score = max((title_similarity(identity.title, name) for name in all_names), default=0.0)
            if identity.year and media.get("seasonYear"):
                score += 8 if identity.year == int(media["seasonYear"]) else -3
            episodes = media.get("episodes")
            if identity.episode and episodes:
                score += 4 if identity.episode <= int(episodes) else -20
            result.append(_as_anime(media, score=score))
        result.sort(key=lambda item: item.score, reverse=True)
        return result

    def get_anime(self, media_id: int) -> AniListAnime:
        media = self._post(LIST_ENTRY_QUERY, {"mediaId": media_id}).get("Media")
        if not isinstance(media, dict):
            raise AniListError(f"Аниме AniList id={media_id} не найдено")
        return _as_anime(media, score=1000.0)

    def episode_airing_at(self, media_id: int, episode: int) -> int | None:
        if media_id < 1 or episode < 1:
            return None
        schedule = self._post(
            AIRING_SCHEDULE_QUERY,
            {"mediaId": int(media_id), "episode": int(episode)},
        ).get("AiringSchedule")
        if not isinstance(schedule, dict) or not schedule.get("airingAt"):
            return None
        return int(schedule["airingAt"])


    def full_relation_graph(
        self,
        media_id: int,
        *,
        max_nodes: int = 250,
    ) -> dict[str, Any]:
        """Fetch every anime entry reachable through AniList relations.

        AniList does not expose recursive relations in one fixed GraphQL
        selection.  We therefore traverse the connected component in batches of
        up to 50 media IDs.  The safety cap prevents pathological franchise
        graphs from blocking the UI forever; callers receive ``truncated=True``
        when it is reached.
        """
        root_id = int(media_id)
        max_nodes = max(1, int(max_nodes))
        pending: list[int] = [root_id]
        requested: set[int] = set()
        nodes: dict[int, dict[str, Any]] = {}
        edge_keys: set[tuple[int, int, str]] = set()
        edges: list[dict[str, Any]] = []
        truncated = False
        root_media: dict[str, Any] | None = None
        terminal_alternative_ids: set[int] = set()
        expandable_ids: set[int] = {root_id}

        while pending:
            batch: list[int] = []
            while pending and len(batch) < 50:
                candidate = int(pending.pop(0))
                if candidate in requested:
                    continue
                requested.add(candidate)
                batch.append(candidate)
            if not batch:
                continue

            page = self._post(FULL_RELATION_GRAPH_QUERY, {"ids": batch}).get("Page")
            media_items = (page or {}).get("media") if isinstance(page, dict) else None
            if not isinstance(media_items, list):
                media_items = []

            for media in media_items:
                if not isinstance(media, dict) or not media.get("id"):
                    continue
                current_format = str(media.get("format") or "").upper()
                # Music videos are not useful watch-order entries and can pull
                # large unrelated character/song components into a franchise.
                if current_format == "MUSIC":
                    continue
                current_id = int(media["id"])
                if current_id == root_id:
                    root_media = media
                payload = _relation_node_payload(
                    media,
                    "SELF",
                    include_children=False,
                )
                if payload is None:
                    continue
                payload.pop("relation_type", None)
                nodes[current_id] = payload

                relation_edges = ((media.get("relations") or {}).get("edges") or [])
                # Alternative versions are useful as leaf nodes, but following
                # their own prequel/sequel trees merges a second watch order
                # into the selected franchise.  Only the selected root may
                # expand when it itself is an alternative entry.
                if current_id in terminal_alternative_ids and current_id != root_id:
                    relation_edges = []
                for relation in relation_edges:
                    if not isinstance(relation, dict):
                        continue
                    relation_type = str(relation.get("relationType") or "").upper()
                    target = relation.get("node")
                    if (
                        not relation_type
                        or not isinstance(target, dict)
                        or str(target.get("type") or "").upper() != "ANIME"
                        or not target.get("id")
                    ):
                        continue
                    target_id = int(target["id"])
                    target_format = str(target.get("format") or "").upper()
                    if target_id == current_id or target_format == "MUSIC":
                        continue
                    if relation_type in {"CHARACTER", "SHARED_CHARACTERS", "RELATED", "OTHER"}:
                        continue
                    if relation_type == "ALTERNATIVE" and target_id != root_id:
                        if target_id not in expandable_ids:
                            terminal_alternative_ids.add(target_id)
                    else:
                        expandable_ids.add(target_id)
                        terminal_alternative_ids.discard(target_id)
                    if (
                        relation_type == "PARENT"
                        and current_id != root_id
                        and not _parent_matches_root_franchise(root_media, target)
                    ):
                        continue

                    # Normalize PREQUEL/SEQUEL into a single chronological
                    # direction so the UI can build one horizontal backbone.
                    if relation_type == "PREQUEL":
                        source_id, destination_id, normalized_type = (
                            target_id,
                            current_id,
                            "SEQUEL",
                        )
                    elif relation_type == "SEQUEL":
                        source_id, destination_id, normalized_type = (
                            current_id,
                            target_id,
                            "SEQUEL",
                        )
                    else:
                        source_id, destination_id = sorted((current_id, target_id))
                        normalized_type = relation_type

                    key = (source_id, destination_id, normalized_type)
                    if key not in edge_keys:
                        edge_keys.add(key)
                        edges.append(
                            {
                                "source": source_id,
                                "target": destination_id,
                                "relation_type": normalized_type,
                            }
                        )

                    if target_id not in requested and target_id not in pending:
                        if len(requested) + len(pending) >= max_nodes:
                            truncated = True
                        else:
                            pending.append(target_id)

            if len(requested) >= max_nodes and pending:
                truncated = True
                pending.clear()

        if root_id not in nodes:
            raise AniListError(f"AniList relation graph has no anime #{root_id}")

        nodes_list = sorted(
            nodes.values(),
            key=lambda item: (
                item.get("start_date") or f"{int(item.get('season_year') or 9999):04d}",
                int(item.get("media_id") or 0),
            ),
        )
        edges.sort(
            key=lambda item: (
                item["relation_type"] != "SEQUEL",
                item["source"],
                item["target"],
                item["relation_type"],
            )
        )
        return {
            "root_id": root_id,
            "nodes": nodes_list,
            "edges": edges,
            "truncated": truncated,
            "partial": False,
        }

    def get_anime_with_relations(
        self, media_id: int
    ) -> tuple[AniListAnime, list[tuple[str, AniListAnime]]]:
        media = self._post(RELATIONS_QUERY, {"mediaId": media_id}).get("Media")
        if not isinstance(media, dict):
            raise AniListError(f"Аниме AniList id={media_id} не найдено")

        relations: list[tuple[str, AniListAnime]] = []
        edges = ((media.get("relations") or {}).get("edges") or [])
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else None
            relation_type = str((edge or {}).get("relationType") or "")
            if not isinstance(node, dict) or not relation_type:
                continue
            try:
                relations.append((relation_type, _as_anime(node)))
            except (KeyError, TypeError, ValueError):
                continue
        return _as_anime(media, score=1000.0), relations

    def direct_relation_payload(self, media_id: int) -> list[dict[str, Any]]:
        """Fetch the compact two-level relation payload for one anime.

        The normal library query can occasionally fall back to a compact
        response without relation data.  This focused query is deliberately
        small enough to restore the card graph for newly added entries.
        """
        media = self._post(DIRECT_RELATIONS_QUERY, {"mediaId": int(media_id)}).get(
            "Media"
        )
        if not isinstance(media, dict):
            raise AniListError(f"Аниме AniList id={media_id} не найдено")
        return _as_direct_relations(media)

    def resolve_absolute_episode(
        self,
        start: AniListAnime,
        absolute_episode: int,
        *,
        max_hops: int = 12,
    ) -> tuple[AniListAnime, int, list[AniListAnime]] | None:
        """Map absolute numbering across TV/ONA sequel cours.

        ``start`` may be any cour from the chain (including a stale cached
        mapping). We first follow eligible PREQUEL edges to the beginning of
        the segmented arc, then subtract cour lengths while following SEQUEL.
        """
        if absolute_episode < 1:
            return None

        valid_formats = {"TV", "TV_SHORT", "ONA"}

        def relation_rank(source: AniListAnime, candidate: AniListAnime) -> tuple[float, float, int]:
            source_names = [*source.titles, *source.synonyms]
            candidate_names = [*candidate.titles, *candidate.synonyms]
            continuity = max(
                (title_similarity(left, right) for left in source_names for right in candidate_names),
                default=0.0,
            )
            year_gap = abs(
                (candidate.season_year or 9999) - (source.season_year or 9999)
            )
            return continuity, -float(year_gap), -candidate.id

        # Recover the beginning of a split-cour arc. A predecessor with at
        # least as many episodes as the absolute number is normally the old
        # parent series (for example original BLEACH with 366 episodes), not
        # another cour of the current arc.
        current = start
        backward = [current]
        visited = {current.id}
        for _ in range(max_hops):
            _, relations = self.get_anime_with_relations(current.id)
            prequels = [
                anime
                for relation_type, anime in relations
                if relation_type == "PREQUEL"
                and anime.id not in visited
                and (anime.format or "").upper() in valid_formats
                and anime.episodes is not None
                and 0 < anime.episodes < absolute_episode
            ]
            if not prequels:
                break
            current = max(prequels, key=lambda anime: relation_rank(backward[0], anime))
            visited.add(current.id)
            backward.insert(0, current)

        remaining = absolute_episode
        chain = [backward[0]]
        current = backward[0]
        visited = {current.id}

        for _ in range(max_hops + 1):
            if current.episodes is None:
                # AniList may not publish the final episode count for an
                # airing cour yet. The remaining absolute offset is still the
                # correct relative episode for this latest known sequel.
                return current, remaining, chain
            if current.episodes < 1:
                return None
            if remaining <= current.episodes:
                return current, remaining, chain

            remaining -= current.episodes
            _, relations = self.get_anime_with_relations(current.id)
            sequels = [
                anime
                for relation_type, anime in relations
                if relation_type == "SEQUEL"
                and anime.id not in visited
                and (anime.format or "").upper() in valid_formats
                and (anime.episodes is None or anime.episodes > 0)
            ]
            if not sequels:
                return None

            current = max(sequels, key=lambda anime: relation_rank(chain[-1], anime))
            visited.add(current.id)
            chain.append(current)

        return None

    def absolute_episode_number(
        self,
        start: AniListAnime,
        relative_episode: int,
        *,
        max_hops: int = 12,
    ) -> tuple[int, list[AniListAnime]]:
        """Convert a cour-relative episode to franchise/cour absolute numbering.

        Some Jimaku uploads use ``S02E17`` for season two episode five after a
        twelve-episode first season. The normal relative number remains the
        primary identity; this method only supplies an additional safe alias.
        """
        if relative_episode < 1:
            return relative_episode, [start]

        valid_formats = {"TV", "TV_SHORT", "ONA"}

        def relation_rank(source: AniListAnime, candidate: AniListAnime) -> tuple[float, float, int]:
            source_names = [*source.titles, *source.synonyms]
            candidate_names = [*candidate.titles, *candidate.synonyms]
            continuity = max(
                (title_similarity(left, right) for left in source_names for right in candidate_names),
                default=0.0,
            )
            year_gap = abs((candidate.season_year or 9999) - (source.season_year or 9999))
            return continuity, -float(year_gap), -candidate.id

        total = int(relative_episode)
        current = start
        chain = [start]
        visited = {start.id}
        for _ in range(max_hops):
            _, relations = self.get_anime_with_relations(current.id)
            prequels = [
                anime
                for relation_type, anime in relations
                if relation_type == "PREQUEL"
                and anime.id not in visited
                and (anime.format or "").upper() in valid_formats
                and anime.episodes is not None
                and anime.episodes > 0
            ]
            if not prequels:
                break
            candidate = max(prequels, key=lambda anime: relation_rank(current, anime))
            continuity = relation_rank(current, candidate)[0]
            # Do not cross into a loosely related parent show or a long-running
            # predecessor that uses its own numbering. Split-cour / seasonal
            # chains are typically bounded entries, while e.g. original BLEACH
            # (366 episodes) must not be added to Thousand-Year Blood War.
            episode_cap = max(100, int(start.episodes or 0) * 4)
            if continuity < 35.0 or int(candidate.episodes or 0) > episode_cap:
                break
            total += int(candidate.episodes or 0)
            visited.add(candidate.id)
            chain.insert(0, candidate)
            current = candidate
        return total, chain

    def _minimal_list_entry(self, media_id: int) -> dict[str, Any] | None:
        media = self._post(
            MINIMAL_LIST_ENTRY_QUERY,
            {"mediaId": int(media_id)},
        ).get("Media")
        entry = media.get("mediaListEntry") if isinstance(media, dict) else None
        return entry if isinstance(entry, dict) else None

    def _save_progress(self, media_id: int, progress: int, status: str | None) -> dict[str, Any]:
        variables: dict[str, Any] = {"mediaId": media_id, "progress": progress}
        if status is not None:
            variables["status"] = status
        try:
            saved = self._post(SAVE_PROGRESS_MUTATION, variables).get("SaveMediaListEntry")
        except AniListHTTPError as exc:
            if exc.status_code < 500:
                raise
            entry = self._minimal_list_entry(media_id)
            expected_status = status or str((entry or {}).get("status") or "")
            if (
                isinstance(entry, dict)
                and int(entry.get("progress") or 0) == int(progress)
                and (status is None or str(entry.get("status") or "") == expected_status)
            ):
                return entry
            raise
        if not isinstance(saved, dict):
            raise AniListError("AniList не вернул обновлённую запись списка")
        return saved

    def set_progress(self, media_id: int, progress: int, status: str | None = None) -> dict[str, Any]:
        progress = max(0, int(progress))
        normalized_status = str(status).strip().upper() if status is not None else None
        return self._save_progress(int(media_id), progress, normalized_status)

    def set_list_status(self, media_id: int, status: str) -> dict[str, Any]:
        allowed = {"CURRENT", "PLANNING", "COMPLETED", "DROPPED", "PAUSED", "REPEATING"}
        status = str(status or "").strip().upper()
        if status not in allowed:
            raise AniListError(f"Недопустимый статус AniList: {status}")
        try:
            saved = self._post(
                SAVE_STATUS_MUTATION,
                {"mediaId": int(media_id), "status": status},
            ).get("SaveMediaListEntry")
        except AniListHTTPError as exc:
            if exc.status_code < 500:
                raise
            entry = self._minimal_list_entry(media_id)
            if isinstance(entry, dict) and str(entry.get("status") or "") == status:
                return entry
            raise
        if not isinstance(saved, dict):
            raise AniListError("AniList не вернул обновлённую запись списка")
        return saved

    def set_score(self, media_id: int, score: float) -> dict[str, Any]:
        value = float(score)
        if value < 1 or value > 10:
            raise AniListError("Оценка AniList должна быть от 1 до 10")
        try:
            saved = self._post(
                SAVE_SCORE_MUTATION,
                {"mediaId": int(media_id), "scoreRaw": int(round(value * 10))},
            ).get("SaveMediaListEntry")
        except AniListHTTPError as exc:
            if exc.status_code < 500:
                raise
            entry = self._minimal_list_entry(media_id)
            actual = float((entry or {}).get("score") or 0)
            if isinstance(entry, dict) and abs(actual - value) < 0.01:
                return entry
            raise
        if not isinstance(saved, dict):
            raise AniListError("AniList не вернул сохранённую оценку")
        return saved

    def delete_list_entry(self, media_id: int) -> bool:
        entry = self._minimal_list_entry(media_id)
        if not isinstance(entry, dict) or not entry.get("id"):
            return False
        try:
            deleted = self._post(
                DELETE_LIST_ENTRY_MUTATION,
                {"id": int(entry["id"])},
            ).get("DeleteMediaListEntry")
        except AniListHTTPError as exc:
            if exc.status_code < 500:
                raise
            return self._minimal_list_entry(media_id) is None
        return bool(isinstance(deleted, dict) and deleted.get("deleted"))

    def update_progress(
        self,
        media_id: int,
        progress: int,
        total_episodes: int | None = None,
        *,
        add_if_missing: bool = False,
        update_when_rewatching: bool = True,
        completed_to_rewatching_on_episode_one: bool = False,
        complete_current_final: bool = True,
        complete_rewatching_final: bool = True,
    ) -> dict[str, Any]:
        if progress < 1:
            raise AniListError("Номер серии для AniList должен быть положительным")

        media = self._post(LIST_ENTRY_QUERY, {"mediaId": media_id}).get("Media")
        if not isinstance(media, dict):
            raise AniListError(f"Аниме AniList id={media_id} не найдено")

        entry = media.get("mediaListEntry")
        current_progress = int((entry or {}).get("progress") or 0)
        current_status = str((entry or {}).get("status") or "")
        api_total = media.get("episodes")
        # AniList is authoritative when it publishes a total. A stale local
        # hint must not make an airing entry look shorter than it really is.
        total = (int(api_total) if api_total else None) or total_episodes
        if total and progress > int(total):
            # Release filenames can use absolute numbering across split cours
            # (e.g. BLEACH 43 == cour-local 3). Never send that absolute number
            # as AniList progress: it can incorrectly complete the whole entry.
            return {
                "updated": False,
                "progress": current_progress,
                "status": current_status or None,
                "reason": "progress_exceeds_total",
                "requested_progress": int(progress),
                "total_episodes": int(total),
            }
        is_final = bool(total and progress == int(total))

        if not entry:
            if not add_if_missing:
                return {
                    "updated": False,
                    "progress": 0,
                    "status": None,
                    "reason": "not_on_list",
                }
            status = "COMPLETED" if is_final and complete_current_final else "CURRENT"
            saved = self._save_progress(media_id, progress, status)
            return {
                "updated": True,
                "progress": int(saved.get("progress") or progress),
                "status": saved.get("status") or status,
                "reason": "added",
            }

        if current_status == "COMPLETED":
            if progress == 1 and completed_to_rewatching_on_episode_one:
                self._save_progress(media_id, 0, "REPEATING")
                saved = self._save_progress(media_id, 1, None)
                return {
                    "updated": True,
                    "progress": int(saved.get("progress") or 1),
                    "status": saved.get("status") or "REPEATING",
                    "reason": "started_rewatching",
                }
            return {
                "updated": False,
                "progress": current_progress,
                "status": current_status,
                "reason": "status_not_modifiable",
            }

        if current_status == "REPEATING":
            if not update_when_rewatching:
                return {
                    "updated": False,
                    "progress": current_progress,
                    "status": current_status,
                    "reason": "rewatching_disabled",
                }
            if current_progress >= progress:
                return {
                    "updated": False,
                    "progress": current_progress,
                    "status": current_status,
                    "reason": "already_at_or_above",
                }
            status = "COMPLETED" if is_final and complete_rewatching_final else None
            saved = self._save_progress(media_id, progress, status)
            return {
                "updated": True,
                "progress": int(saved.get("progress") or progress),
                "status": saved.get("status") or status or "REPEATING",
                "reason": "updated_rewatching",
            }

        # A confirmed watch must always move a PLANNING entry out of Planned.
        # Do this before the progress guard because AniList can contain an
        # inconsistent PLANNING entry with non-zero progress (for example after
        # an earlier client wrote progress without changing the list status).
        if current_status == "PLANNING":
            status = "COMPLETED" if is_final and complete_current_final else "CURRENT"
            target_progress = max(current_progress, progress)
            saved = self._save_progress(media_id, target_progress, status)
            return {
                "updated": True,
                "progress": int(saved.get("progress") or target_progress),
                "status": saved.get("status") or status,
                "reason": "completed_from_planning" if status == "COMPLETED" else "started_watching",
            }

        if current_status not in {"CURRENT", "PAUSED"}:
            return {
                "updated": False,
                "progress": current_progress,
                "status": current_status or None,
                "reason": "status_not_modifiable",
            }

        if current_progress >= progress:
            return {
                "updated": False,
                "progress": current_progress,
                "status": current_status or None,
                "reason": "already_at_or_above",
            }

        status = "COMPLETED" if is_final and complete_current_final else "CURRENT"
        saved = self._save_progress(media_id, progress, status)
        return {
            "updated": True,
            "progress": int(saved.get("progress") or progress),
            "status": saved.get("status") or status,
            "reason": "updated",
        }
