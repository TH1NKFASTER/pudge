from __future__ import annotations

import json
from pathlib import Path

import httpx

from anime_mpv.config import AppConfig, write_config
from anime_mpv.manager_models import LibraryAnime
from anime_mpv.providers.anilist import AniListClient
from anime_mpv.web_app import WebAppApi


def _client(handler) -> AniListClient:
    client = AniListClient("https://graphql.anilist.co", access_token="secret")
    client.client.close()
    client.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer secret"},
    )
    return client


def _api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_post_retries_transient_500_once() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text="temporary")
        return httpx.Response(200, json={"data": {"Viewer": {"id": 7, "name": "x"}}})

    client = _client(handler)
    try:
        assert client.viewer()["id"] == 7
    finally:
        client.close()
    assert calls == 2


def test_library_falls_back_after_extended_query_500() -> None:
    full_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal full_calls
        payload = json.loads(request.content)
        query = payload["query"]
        if "Viewer" in query:
            return httpx.Response(200, json={"data": {"Viewer": {"id": 7, "name": "x"}}})
        if "fragment RelationLeaf" in query:
            full_calls += 1
            return httpx.Response(500, text="query bug")
        return httpx.Response(
            200,
            json={
                "data": {
                    "MediaListCollection": {
                        "lists": [
                            {
                                "status": "CURRENT",
                                "entries": [
                                    {
                                        "status": "CURRENT",
                                        "progress": 3,
                                        "score": 8,
                                        "media": {
                                            "id": 100,
                                            "title": {"romaji": "Fallback Anime"},
                                            "synonyms": [],
                                            "episodes": 12,
                                            "format": "TV",
                                            "seasonYear": 2026,
                                            "status": "FINISHED",
                                            "siteUrl": "",
                                            "coverImage": {},
                                            "studios": {"nodes": []},
                                            "nextAiringEpisode": None,
                                        },
                                    }
                                ],
                            }
                        ]
                    }
                }
            },
        )

    client = _client(handler)
    try:
        items = client.library()
    finally:
        client.close()

    assert full_calls == 2
    assert items[0].title == "Fallback Anime"
    assert client.last_library_used_fallback is True
    assert "упрощённый" in client.last_library_warning


def test_status_mutation_500_is_verified_by_readback() -> None:
    mutation_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mutation_calls
        payload = json.loads(request.content)
        if "mutation" in payload["query"]:
            mutation_calls += 1
            return httpx.Response(500, text="response failed after save")
        return httpx.Response(
            200,
            json={
                "data": {
                    "Media": {
                        "id": 55,
                        "mediaListEntry": {
                            "id": 999,
                            "status": "PLANNING",
                            "progress": 0,
                            "score": 0,
                        },
                    }
                }
            },
        )

    client = _client(handler)
    try:
        saved = client.set_list_status(55, "PLANNING")
    finally:
        client.close()

    assert mutation_calls == 2
    assert saved["status"] == "PLANNING"


def test_planning_mutation_stays_successful_when_refresh_fails(tmp_path: Path, monkeypatch) -> None:
    api = _api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=1,
            title="Parent",
            status="CURRENT",
            relations=[
                {
                    "media_id": 55,
                    "title": "Related Anime",
                    "site_url": "https://anilist.co/anime/55",
                    "episodes": 12,
                    "format": "TV",
                    "season_year": 2024,
                    "studio": "Shaft",
                    "list_status": "",
                    "watched": False,
                    "relations": [],
                }
            ],
        )
    )

    class FakeClient:
        def set_list_status(self, media_id: int, status: str):
            assert (media_id, status) == (55, "PLANNING")
            return {"status": status}

        def close(self) -> None:
            pass

    monkeypatch.setattr(api, "_anilist_client", lambda: FakeClient())
    monkeypatch.setattr(
        api.manager,
        "refresh_anilist_cache",
        lambda: (_ for _ in ()).throw(RuntimeError("AniList 500")),
    )

    result = api.add_to_planning(55)

    assert result["ok"] is True
    assert result["refresh_pending"] is True
    cached = api.manager.db.get_anime(55)
    assert cached is not None
    assert cached.status == "PLANNING"
    assert cached.title == "Related Anime"
    assert result["state"]["planned"][0]["media_id"] == 55


def test_ui_explains_saved_mutation_with_delayed_refresh() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text()
    assert "toast.anilistRefreshPending" in html
    assert "r.refresh_pending?t('toast.anilistRefreshPending')" in html
