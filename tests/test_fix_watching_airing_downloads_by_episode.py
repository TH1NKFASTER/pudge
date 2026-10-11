"""Moving an airing show from Planning to Watching downloads its next episode, not a "season
pack": a pack search for an airing show picked a single-episode release and recorded it as a
batch, which later blocked every new episode as "already downloading" (Kusuriya S3)."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime


def _manager(anime: LibraryAnime) -> tuple[AnimeManager, list[dict]]:
    manager = AnimeManager.__new__(AnimeManager)
    manager.db = SimpleNamespace(get_anime=lambda media_id: anime if media_id == anime.media_id else None)
    manager.logger = logging.getLogger("test")
    calls: list[dict] = []
    manager.search_and_add_best = lambda media_id, **kwargs: calls.append({"media_id": media_id, **kwargs})
    return manager, calls


def test_an_airing_show_starts_with_its_next_episode() -> None:
    manager, calls = _manager(LibraryAnime(195516, "Kusuriya 3", progress=0, media_status="RELEASING"))
    manager.download_planned(195516)
    assert calls == [{"media_id": 195516, "episode": 1, "batch": False}]


def test_a_finished_show_still_downloads_as_a_season_pack() -> None:
    manager, calls = _manager(LibraryAnime(105228, "Dorohedoro", progress=0, media_status="FINISHED"))
    manager.download_planned(105228)
    assert calls == [{"media_id": 105228, "episode": None, "batch": True}]
