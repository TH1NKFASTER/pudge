from __future__ import annotations

import time
from pathlib import Path

from anime_mpv.config import AppConfig
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import LibraryAnime


def make_manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"
    return AnimeManager(cfg, log=lambda _message: None)


def graph_payload(root_id: int) -> dict:
    return {
        "root_id": root_id,
        "nodes": [
            {
                "media_id": 10,
                "title": "Season 1",
                "format": "TV",
                "episodes": 12,
                "season_year": 2024,
                "list_status": "COMPLETED",
                "progress": 12,
                "watched": True,
            },
            {
                "media_id": 20,
                "title": "Season 2",
                "format": "TV",
                "episodes": 12,
                "season_year": 2025,
                "list_status": "PLANNING",
                "progress": 0,
                "watched": False,
            },
        ],
        "edges": [{"source": 10, "target": 20, "relation_type": "SEQUEL"}],
        "truncated": False,
        "partial": False,
    }


def test_full_graph_is_shared_between_all_component_entries(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(media_id=10, title="Season 1", status="CURRENT", episodes=12)
    )
    manager.db.upsert_anime(
        LibraryAnime(media_id=20, title="Season 2", status="PLANNING", episodes=12)
    )
    calls: list[int] = []

    class FakeClient:
        def __init__(self, endpoint, access_token=""):
            pass

        def full_relation_graph(self, media_id: int):
            calls.append(media_id)
            return graph_payload(media_id)

        def close(self):
            pass

    monkeypatch.setattr("anime_mpv.manager.AniListClient", FakeClient)

    first = manager.relation_graph(10)
    second = manager.relation_graph(20)

    assert calls == [10]
    assert first["root_id"] == 10
    assert second["root_id"] == 20
    cached_10 = manager.db.relation_graph_for_media(10)
    cached_20 = manager.db.relation_graph_for_media(20)
    assert cached_10 is not None and cached_20 is not None
    assert cached_10["graph_id"] == cached_20["graph_id"]
    interval = float(cached_10["next_refresh_at"]) - float(cached_10["refreshed_at"])
    assert 3 * 86400 <= interval < 5 * 86400

    planned = manager.db.get_anime(20)
    assert planned is not None
    assert planned.relations[0]["media_id"] == 10
    assert planned.relations[0]["relation_type"] == "PREQUEL"


def test_due_refresh_updates_only_one_component_per_pass(tmp_path: Path, monkeypatch) -> None:
    manager = make_manager(tmp_path)
    calls: list[int] = []

    class FakeClient:
        def __init__(self, endpoint, access_token=""):
            pass

        def full_relation_graph(self, media_id: int):
            calls.append(media_id)
            return graph_payload(media_id)

        def close(self):
            pass

    monkeypatch.setattr("anime_mpv.manager.AniListClient", FakeClient)
    manager.relation_graph(10)
    cached = manager.db.relation_graph_for_media(10)
    assert cached is not None
    manager.db.defer_relation_graph(str(cached["graph_id"]), time.time() - 1)

    assert manager.refresh_due_relation_graphs(limit=1) == 1
    assert calls == [10, 10]


def test_startup_anilist_sync_uses_compact_list_and_preserves_relations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = make_manager(tmp_path)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=20,
            title="Season 2",
            status="PLANNING",
            episodes=12,
            relations=[
                {
                    "media_id": 10,
                    "title": "Season 1",
                    "relation_type": "PREQUEL",
                    "relations": [],
                }
            ],
        )
    )
    compact_calls = 0

    class FakeClient:
        last_library_warning = ""
        last_library_used_fallback = False

        def __init__(self, endpoint, access_token=""):
            pass

        def library_compact(self):
            nonlocal compact_calls
            compact_calls += 1
            return [
                LibraryAnime(
                    media_id=20,
                    title="Season 2",
                    status="PLANNING",
                    episodes=12,
                    relations=[],
                )
            ]

        def library(self):
            raise AssertionError("expanded relation query must not run on startup")

        def close(self):
            pass

    monkeypatch.setattr("anime_mpv.manager.AniListClient", FakeClient)

    manager.sync_anilist()

    assert compact_calls == 1
    stored = manager.db.get_anime(20)
    assert stored is not None
    assert stored.relations[0]["media_id"] == 10


def test_watch_order_modal_has_component_refresh_button() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert "refresh_relation_graph" in html
    assert 'data-action="refresh-watch-order"' in html
    assert "action.refreshWatchOrder" in html
