from __future__ import annotations

from collections import defaultdict
from typing import Any


def _media_id(item: dict[str, Any]) -> int | None:
    try:
        value = int(item.get("media_id"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _sort_key(item: dict[str, Any], media_id: int) -> tuple[int, int]:
    try:
        year = int(item.get("year") or 0)
    except (TypeError, ValueError):
        year = 0
    return (year if year > 0 else 9999, media_id)


def literature_franchise_metadata(items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Build stable shelf metadata from the existing PREQUEL/SEQUEL graph.

    This function does not infer relations from titles. Only media already present in
    ``items`` and explicit PREQUEL/SEQUEL edges participate. Cycles and incomplete
    graphs are retained and ordered deterministically rather than dropping nodes.
    """

    by_media: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        media_id = _media_id(item)
        if media_id is not None:
            by_media[media_id] = item

    undirected: dict[int, set[int]] = {media_id: set() for media_id in by_media}
    outgoing: dict[int, set[int]] = defaultdict(set)
    incoming: dict[int, set[int]] = defaultdict(set)

    for media_id, item in by_media.items():
        for relation in item.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            relation_type = str(relation.get("relation_type") or "").upper()
            if relation_type not in {"PREQUEL", "SEQUEL"}:
                continue
            if str(relation.get("format") or "NOVEL").upper() != "NOVEL":
                continue
            try:
                other = int(relation.get("media_id"))
            except (TypeError, ValueError):
                continue
            if other not in by_media or other == media_id:
                continue
            undirected[media_id].add(other)
            undirected[other].add(media_id)
            before, after = (media_id, other) if relation_type == "SEQUEL" else (other, media_id)
            outgoing[before].add(after)
            incoming[after].add(before)

    result: dict[int, dict[str, Any]] = {}
    seen: set[int] = set()
    for root in sorted(by_media):
        if root in seen:
            continue
        component: set[int] = set()
        stack = [root]
        while stack:
            current = stack.pop()
            if current in component:
                continue
            component.add(current)
            stack.extend(sorted(undirected.get(current, ()), reverse=True))
        seen.update(component)
        if len(component) <= 1:
            continue

        indegree = {
            media_id: len(incoming.get(media_id, set()) & component)
            for media_id in component
        }
        ready = sorted(
            (media_id for media_id, degree in indegree.items() if degree == 0),
            key=lambda media_id: _sort_key(by_media[media_id], media_id),
        )
        ordered: list[int] = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for other in sorted(
                outgoing.get(current, set()) & component,
                key=lambda media_id: _sort_key(by_media[media_id], media_id),
            ):
                indegree[other] -= 1
                if indegree[other] == 0:
                    ready.append(other)
                    ready.sort(key=lambda media_id: _sort_key(by_media[media_id], media_id))

        ambiguous = len(ordered) != len(component)
        if ambiguous:
            remaining = sorted(
                component - set(ordered),
                key=lambda media_id: _sort_key(by_media[media_id], media_id),
            )
            ordered.extend(remaining)

        key = f"anilist-franchise:{min(component)}"
        first_item = by_media[ordered[0]]
        title = str(first_item.get("title") or "").strip() or f"Franchise {min(component)}"
        order_by_id = {media_id: index for index, media_id in enumerate(ordered)}
        for media_id in ordered:
            has_before = bool(incoming.get(media_id, set()) & component)
            has_after = bool(outgoing.get(media_id, set()) & component)
            if not has_before and has_after:
                relation_kind = "root"
            elif has_before:
                relation_kind = "sequel"
            else:
                relation_kind = "related"
            result[media_id] = {
                "franchise_key": key,
                "franchise_title": title,
                "franchise_order": order_by_id[media_id],
                "franchise_series_count": len(component),
                "franchise_ambiguous": ambiguous,
                "relation_kind": relation_kind,
            }
    return result
