from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from pudge.manager_models import LibraryAnime
from pudge.providers.anilist import AniListClient
from pudge.web_app import WebAppApi


class _StateDb:
    def __init__(self) -> None:
        self.state: dict[str, str] = {}
        self.anime: dict[int, LibraryAnime] = {}

    def get_state(self, key: str, default: str = "") -> str:
        return self.state.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.state[key] = value

    def get_anime(self, media_id: int) -> LibraryAnime | None:
        return self.anime.get(int(media_id))

    def upsert_anime(self, anime: LibraryAnime) -> None:
        self.anime[int(anime.media_id)] = anime


class _Logger:
    def warning(self, *_args, **_kwargs) -> None:
        return


def _api_for_graph(graph: dict) -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.manager = SimpleNamespace(relation_graph=lambda *_args, **_kwargs: graph)
    return api


def test_final_sequel_option_uses_earliest_direct_sequel_by_start_date() -> None:
    graph = {
        "nodes": [
            {"media_id": 1, "title": "Root", "start_date": "2026-01-01"},
            {
                "media_id": 3,
                "title": "Later sequel",
                "start_date": "2027-04",
                "media_status": "NOT_YET_RELEASED",
                "list_status": "",
            },
            {
                "media_id": 2,
                "title": "Next sequel",
                "start_date": "2026-09-01",
                "media_status": "FINISHED",
                "list_status": "",
                "format": "TV",
                "episodes": 12,
            },
        ],
        "edges": [
            {"source": 1, "target": 3, "relation_type": "SEQUEL"},
            {"source": 1, "target": 2, "relation_type": "SEQUEL"},
        ],
    }

    sequel = _api_for_graph(graph).final_sequel_option(1)["sequel"]

    assert sequel["media_id"] == 2
    assert sequel["released"] is True
    assert sequel["can_planning"] is True
    assert sequel["can_download"] is True
    assert sequel["can_watching"] is False


def test_final_sequel_option_planning_released_replaces_planning_with_watching() -> None:
    graph = {
        "nodes": [
            {"media_id": 1, "title": "Root"},
            {
                "media_id": 2,
                "title": "Sequel",
                "start_date": "2026-09-01",
                "media_status": "RELEASING",
                "list_status": "PLANNING",
            },
        ],
        "edges": [{"source": 1, "target": 2, "relation_type": "SEQUEL"}],
    }

    sequel = _api_for_graph(graph).final_sequel_option(1)["sequel"]

    assert sequel["can_planning"] is False
    assert sequel["can_watching"] is True
    assert sequel["can_download"] is True


def test_final_sequel_option_upcoming_only_offers_planning() -> None:
    graph = {
        "nodes": [
            {"media_id": 1, "title": "Root"},
            {
                "media_id": 2,
                "title": "Future sequel",
                "start_date": "2099-01-01",
                "media_status": "NOT_YET_RELEASED",
                "list_status": "",
            },
        ],
        "edges": [{"source": 1, "target": 2, "relation_type": "SEQUEL"}],
    }

    sequel = _api_for_graph(graph).final_sequel_option(1)["sequel"]

    assert sequel["released"] is False
    assert sequel["can_planning"] is True
    assert sequel["can_watching"] is False
    assert sequel["can_download"] is False


def test_final_sequel_option_current_has_no_actions() -> None:
    graph = {
        "nodes": [
            {"media_id": 1, "title": "Root"},
            {
                "media_id": 2,
                "title": "Current sequel",
                "start_date": "2026-09-01",
                "media_status": "RELEASING",
                "list_status": "CURRENT",
            },
        ],
        "edges": [{"source": 1, "target": 2, "relation_type": "SEQUEL"}],
    }

    sequel = _api_for_graph(graph).final_sequel_option(1)["sequel"]

    assert sequel["can_planning"] is False
    assert sequel["can_watching"] is False
    assert sequel["can_download"] is False


def test_related_media_addition_notifications_are_parsed_without_resetting_anilist_count() -> None:
    client = object.__new__(AniListClient)
    captured: dict[str, object] = {}

    def fake_post(query: str, variables: dict) -> dict:
        captured["query"] = query
        captured["variables"] = variables
        return {
            "Page": {
                "notifications": [
                    {
                        "id": 77,
                        "type": "RELATED_MEDIA_ADDITION",
                        "mediaId": 123,
                        "context": "was recently added to the site.",
                        "createdAt": 1000,
                        "media": {
                            "id": 123,
                            "type": "ANIME",
                            "status": "NOT_YET_RELEASED",
                            "format": "TV",
                            "siteUrl": "https://anilist.co/anime/123",
                            "startDate": {"year": 2027, "month": 4, "day": None},
                            "mediaListEntry": {"status": "PLANNING"},
                            "title": {"userPreferred": "Example Sequel"},
                            "coverImage": {"large": "https://img.example/123.jpg"},
                        },
                    }
                ]
            }
        }

    client._post = fake_post  # type: ignore[method-assign]
    rows = client.related_media_addition_notifications(per_page=30)

    assert rows == [
        {
            "id": 77,
            "type": "RELATED_MEDIA_ADDITION",
            "media_id": 123,
            "media_type": "ANIME",
            "title": "Example Sequel",
            "context": "was recently added to the site.",
            "created_at": 1000,
            "cover": "https://img.example/123.jpg",
            "site_url": "https://anilist.co/anime/123",
            "media_status": "NOT_YET_RELEASED",
            "format": "TV",
            "start_date": "2027-04",
            "list_status": "PLANNING",
        }
    ]
    assert "resetNotificationCount: false" in str(captured["query"])
    assert "mediaListEntry { status }" in str(captured["query"])
    assert captured["variables"] == {"page": 1, "perPage": 30}


def test_pudge_related_notifications_filter_anime_recent_and_persist_seen_ids() -> None:
    now = int(time.time())
    db = _StateDb()
    manager = SimpleNamespace(db=db)
    api = object.__new__(WebAppApi)
    api.manager = manager
    api.config = SimpleNamespace(
        anilist=SimpleNamespace(enabled=True, access_token="token")
    )
    api.logger = _Logger()

    class FakeClient:
        def related_media_addition_notifications(self, *, per_page: int = 20):
            assert per_page == 30
            return [
                {
                    "id": 10,
                    "media_id": 100,
                    "media_type": "ANIME",
                    "title": "New anime",
                    "created_at": now - 60,
                },
                {
                    "id": 11,
                    "media_id": 101,
                    "media_type": "MANGA",
                    "title": "New manga",
                    "created_at": now - 60,
                },
                {
                    "id": 12,
                    "media_id": 102,
                    "media_type": "ANIME",
                    "title": "Old anime",
                    "created_at": now - 8 * 86400,
                },
            ]

        def close(self) -> None:
            return

    api._anilist_client = lambda: FakeClient()  # type: ignore[method-assign]

    first = api.poll_anilist_related_media_notifications()
    assert [row["id"] for row in first["notifications"]] == [10]

    api.mark_anilist_related_media_notifications_seen([10])
    second = api.poll_anilist_related_media_notifications()
    assert second["notifications"] == []
    assert "10" in db.get_state("anilist.related_media_additions.seen:v1")


def test_download_final_sequel_reports_missing_torrent_without_status_change() -> None:
    db = _StateDb()
    sequel = LibraryAnime(
        media_id=2,
        title="Movie sequel",
        status="",
        media_status="FINISHED",
        format="MOVIE",
        episodes=1,
    )
    db.upsert_anime(sequel)

    class FakeManager:
        def __init__(self) -> None:
            self.db = db
            self.calls: list[tuple[int, int | None, bool]] = []

        def downloads_enabled(self) -> bool:
            return True

        def search_and_add_best(self, media_id: int, *, episode, batch, automatic):
            self.calls.append((media_id, episode, batch))
            return None

    api = object.__new__(WebAppApi)
    api.manager = FakeManager()
    api.logger = _Logger()

    class FakeAniListClient:
        def library_anime(self, _media_id: int) -> LibraryAnime:
            return sequel

        def close(self) -> None:
            return

    api._anilist_client = lambda: FakeAniListClient()  # type: ignore[method-assign]
    api.final_sequel_option = lambda _media_id: {  # type: ignore[method-assign]
        "ok": True,
        "sequel": {
            "media_id": 2,
            "title": "Movie sequel",
            "released": True,
            "format": "MOVIE",
            "episodes": 1,
            "list_status": "",
        },
    }

    result = api.download_final_sequel(1)

    assert result["ok"] is False
    assert result["reason"] == "no_release"
    assert db.get_anime(2).status == ""
    assert api.manager.calls == [(2, None, False)]


def test_score_modal_and_notification_ui_hooks_exist() -> None:
    html = (Path(__file__).resolve().parents[1] / "pudge" / "web" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'data-action="final-sequel-planning"' in html
    assert 'data-action="final-sequel-watching"' in html
    assert 'data-action="final-sequel-download"' in html
    assert 'const siteUrl=sequel.site_url||`https://anilist.co/anime/${Number(sequel.media_id)}`' in html
    assert 'data-action="url" data-url="${escapeHtml(siteUrl)}"' in html
    assert "startAniListRelatedNotificationPolling()" in html
    assert 'id="anilistRelatedBackdrop"' in html


def test_download_final_airing_sequel_reuses_episode_auto_download_without_status_change() -> None:
    db = _StateDb()
    remote = LibraryAnime(
        media_id=2,
        title="Airing sequel",
        status="",
        media_status="RELEASING",
        format="TV",
        episodes=12,
        next_airing_episode=3,
        next_airing_at=int(time.time()) + 3600,
    )

    class FakeManager:
        def __init__(self) -> None:
            self.db = db

        def downloads_enabled(self) -> bool:
            return True

    api = object.__new__(WebAppApi)
    api.manager = FakeManager()
    api.logger = _Logger()
    api.get_state = lambda: {"ok": True}  # type: ignore[method-assign]
    api.final_sequel_option = lambda _media_id: {  # type: ignore[method-assign]
        "ok": True,
        "sequel": {
            "media_id": 2,
            "title": remote.title,
            "released": True,
            "format": "TV",
            "episodes": 12,
            "list_status": "",
        },
    }

    class FakeAniListClient:
        def library_anime(self, _media_id: int) -> LibraryAnime:
            return remote

        def close(self) -> None:
            return

    api._anilist_client = lambda: FakeAniListClient()  # type: ignore[method-assign]
    api.start_planning_episode_download = lambda media_id: {  # type: ignore[method-assign]
        "running": True,
        "media_id": media_id,
        "total": 2,
    }

    result = api.download_final_sequel(1)

    assert result["ok"] is True
    assert result["background"] is True
    assert result["job"]["total"] == 2
    assert db.get_anime(2).status == ""
    assert db.get_anime(2).next_airing_episode == 3


def test_download_final_sequel_does_not_attach_to_other_running_job() -> None:
    db = _StateDb()
    remote = LibraryAnime(
        media_id=2,
        title="Airing sequel",
        status="",
        media_status="RELEASING",
        format="TV",
        episodes=12,
        next_airing_episode=3,
        next_airing_at=int(time.time()) + 3600,
    )

    class FakeManager:
        def __init__(self) -> None:
            self.db = db

        def downloads_enabled(self) -> bool:
            return True

    api = object.__new__(WebAppApi)
    api.manager = FakeManager()
    api.logger = _Logger()
    api.final_sequel_option = lambda _media_id: {  # type: ignore[method-assign]
        "ok": True,
        "sequel": {
            "media_id": 2,
            "title": remote.title,
            "released": True,
            "format": "TV",
            "episodes": 12,
            "list_status": "",
        },
    }

    class FakeAniListClient:
        def library_anime(self, _media_id: int) -> LibraryAnime:
            return remote

        def close(self) -> None:
            return

    api._anilist_client = lambda: FakeAniListClient()  # type: ignore[method-assign]
    api.start_planning_episode_download = lambda _media_id: {  # type: ignore[method-assign]
        "running": True,
        "media_id": 999,
        "title": "Other anime",
    }

    result = api.download_final_sequel(1)

    assert result["ok"] is False
    assert result["reason"] == "download_busy"
    assert result["job"]["media_id"] == 999
    assert db.get_anime(2).status == ""


def test_related_notification_ui_has_planning_action_and_disables_existing_list_entries() -> None:
    html = (Path(__file__).resolve().parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")

    assert "data-anilist-related-planning" in html
    assert "pywebview.api.add_media_to_planning" in html
    assert "['PLANNING','CURRENT','REPEATING','COMPLETED'].includes(listStatus)" in html
    assert "planning.className='primary'" in html
