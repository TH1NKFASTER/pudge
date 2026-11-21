"""Which episode of one anime downloads first: the one nearest to the next one to watch.

The next to watch is the first unwatched episode after the last watched one (AniList progress
+ 1); without a watched one, the lowest number. Episodes from it onwards come first in
ascending order, already watched ones (a re-download or a rewatch) after them, also ascending:
when every queued episode is behind it (a rewatch from 1 with progress 13), 1, 2, 3...
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def next_to_watch(progress: int | None, episodes: Iterable[int]) -> int | None:
    """The first unwatched episode after the last watched (``progress``); else the lowest number."""
    numbers = sorted({int(value) for value in episodes})
    if not numbers:
        return None
    if progress is not None and int(progress) > 0:
        return int(progress) + 1
    return numbers[0]


def nearest_first(episodes: Iterable[int], next_episode: int | None) -> list[int]:
    """Episodes in download order: from ``next_episode`` on ascending, then the ones behind it
    ascending (a rewatch starts at its first episode, not at the last watched one)."""
    numbers = sorted({int(value) for value in episodes})
    if next_episode is None or not numbers:
        return numbers
    target = int(next_episode)
    return sorted(numbers, key=lambda number: (number < target, number))


def download_order(items: Iterable[Any], progress: int | None) -> list[Any]:
    """Unfinished single-episode downloads of one anime in priority order (nearest first).

    ``items`` are DownloadItem-like (``episode``, ``progress``, ``is_batch``); finished and
    batch downloads and ones without an episode number are left out."""
    pending = [
        item
        for item in items
        if getattr(item, "episode", None) is not None
        and not getattr(item, "is_batch", False)
        and float(getattr(item, "progress", 0) or 0) < 1.0
    ]
    target = next_to_watch(progress, (int(item.episode) for item in pending))
    rank = {
        number: index for index, number in enumerate(nearest_first((int(i.episode) for i in pending), target))
    }
    return sorted(pending, key=lambda item: (rank.get(int(item.episode), len(rank)), str(item.torrent_hash)))
