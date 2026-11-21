"""SMALL1 (review intro #12): when every queued episode of a title is behind the next one to
watch (a rewatch: progress 12, episodes 1-3 queued), they download 1, 2, 3, not 3, 2, 1. Ahead
ones still come first from the next to watch on; behind ones after them, now also ascending.
The iPhone rule (PudgeCore ``EpisodeDownloadOrder``) is the same (PlayerPolishTests)."""

from __future__ import annotations

from types import SimpleNamespace

from pudge.download_priority import download_order, nearest_first


def _item(hash_: str, episode: int):
    return SimpleNamespace(torrent_hash=hash_, episode=episode, is_batch=False, progress=0.1)


def test_a_rewatch_downloads_from_its_first_episode() -> None:
    assert nearest_first([3, 1, 2], 13) == [1, 2, 3]  # was [3, 2, 1]
    items = [_item("e3", 3), _item("e1", 1), _item("e2", 2)]
    assert [item.torrent_hash for item in download_order(items, 12)] == ["e1", "e2", "e3"]


def test_ahead_episodes_still_come_first() -> None:
    assert nearest_first([4, 9, 2, 5, 6], 5) == [5, 6, 9, 2, 4]
    assert nearest_first([7, 8], 5) == [7, 8]
    assert nearest_first([3, 1], None) == [1, 3]
