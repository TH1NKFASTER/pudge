from __future__ import annotations

import copy
import hashlib
from typing import Any


_DAY_SECONDS = 24 * 60 * 60
_RELATION_REFRESH_BASE_SECONDS = 3 * _DAY_SECONDS
_RELATION_REFRESH_JITTER_SECONDS = 2 * _DAY_SECONDS
_RELATION_RETRY_SECONDS = 6 * 60 * 60


def next_relation_refresh_at(graph_id: str, refreshed_at: float) -> float:
    """Schedule one component every 3–5 days at a stable scattered time."""
    digest = hashlib.sha256(str(graph_id).encode("utf-8")).digest()
    jitter = int.from_bytes(digest[:8], "big") % _RELATION_REFRESH_JITTER_SECONDS
    return float(refreshed_at) + _RELATION_REFRESH_BASE_SECONDS + jitter


def relation_retry_at(now: float) -> float:
    return float(now) + _RELATION_RETRY_SECONDS


def graph_for_root(
    graph: dict[str, Any],
    media_id: int,
    *,
    refreshed_at: float = 0.0,
    next_refresh_at: float = 0.0,
    graph_id: str = "",
) -> dict[str, Any]:
    result = copy.deepcopy(graph)
    result["root_id"] = int(media_id)
    result["refreshed_at"] = float(refreshed_at or 0.0)
    result["next_refresh_at"] = float(next_refresh_at or 0.0)
    if graph_id:
        result["graph_id"] = str(graph_id)
    return result


def _node_sort_key(node: dict[str, Any]) -> tuple[str, int]:
    start_date = str(node.get("start_date") or "")
    if not start_date:
        year = node.get("season_year")
        start_date = f"{int(year):04d}" if year else "9999"
    return start_date, int(node.get("media_id") or 0)


def compact_relations_from_graph(
    graph: dict[str, Any],
    root_id: int,
    *,
    depth: int = 2,
) -> list[dict[str, Any]]:
    """Build the compact Planning-card relation tree from one shared graph."""
    root_id = int(root_id)
    nodes = {
        int(node["media_id"]): dict(node)
        for node in graph.get("nodes", [])
        if isinstance(node, dict) and node.get("media_id") is not None
    }
    if root_id not in nodes:
        return []

    adjacency: dict[int, list[tuple[int, str]]] = {media_id: [] for media_id in nodes}
    for raw_edge in graph.get("edges", []):
        if not isinstance(raw_edge, dict):
            continue
        try:
            source = int(raw_edge.get("source"))
            target = int(raw_edge.get("target"))
        except (TypeError, ValueError):
            continue
        if source not in nodes or target not in nodes or source == target:
            continue
        relation_type = str(raw_edge.get("relation_type") or "OTHER").upper()
        if relation_type in {"RELATED", "OTHER"}:
            continue
        if relation_type == "SEQUEL":
            adjacency[source].append((target, "SEQUEL"))
            adjacency[target].append((source, "PREQUEL"))
        else:
            adjacency[source].append((target, relation_type))
            adjacency[target].append((source, relation_type))

    def build(current_id: int, parent_id: int | None, remaining: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[int, str]] = set()
        neighbours = sorted(
            adjacency.get(current_id, []),
            key=lambda pair: (_node_sort_key(nodes[pair[0]]), pair[1]),
        )
        for neighbour_id, relation_type in neighbours:
            if neighbour_id == parent_id:
                continue
            key = (neighbour_id, relation_type)
            if key in seen:
                continue
            seen.add(key)
            payload = dict(nodes[neighbour_id])
            payload["relation_type"] = relation_type
            payload["relations"] = (
                build(neighbour_id, current_id, remaining - 1) if remaining > 1 else []
            )
            result.append(payload)
        return result

    return build(root_id, None, max(1, int(depth)))
