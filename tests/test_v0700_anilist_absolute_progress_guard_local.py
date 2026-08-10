from __future__ import annotations

import json
import logging

import httpx

from pudge.cli import _tracking_episode_from_hint
from pudge.config import AppConfig
from pudge.database import Database
from pudge.models import AniListAnime
from pudge.providers.anilist import AniListClient


def _client(handler) -> AniListClient:
    client = AniListClient("https://graphql.anilist.co", access_token="secret")
    client.client.close()
    client.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer secret"},
    )
    return client


def test_absolute_bleach_43_becomes_cour_episode_3_from_cached_graph(tmp_path) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    db = Database(cfg.library.database_path)
    db.store_relation_graph(
        {
            "root_id": 5,
            "nodes": [
                {"media_id": 1, "title": "BLEACH", "format": "TV", "episodes": 366, "start_date": "2004-10-05"},
                {"media_id": 2, "title": "BLEACH: Sennen Kessen-hen", "format": "TV", "episodes": 13, "start_date": "2022-10-11"},
                {"media_id": 3, "title": "BLEACH: Sennen Kessen-hen - Ketsubetsu-tan", "format": "TV", "episodes": 13, "start_date": "2023-07-08"},
                {"media_id": 4, "title": "BLEACH: Sennen Kessen-hen - Soukoku-tan", "format": "TV", "episodes": 14, "start_date": "2024-10-05"},
                {"media_id": 5, "title": "BLEACH: Sennen Kessen-hen - Kashin-tan", "format": "TV", "episodes": 10, "start_date": "2026-07-25"},
            ],
            "edges": [
                {"source": 1, "target": 2, "relation_type": "SEQUEL"},
                {"source": 2, "target": 3, "relation_type": "SEQUEL"},
                {"source": 3, "target": 4, "relation_type": "SEQUEL"},
                {"source": 4, "target": 5, "relation_type": "SEQUEL"},
            ],
        },
        refreshed_at=1.0,
        next_refresh_at=9999999999.0,
    )
    anime = AniListAnime(
        id=5,
        titles=["BLEACH: Sennen Kessen-hen - Kashin-tan"],
        synonyms=["BLEACH: Thousand-Year Blood War - The Calamity"],
        season_year=2026,
        episodes=10,
        format="TV",
    )

    resolved = _tracking_episode_from_hint(anime, 43, cfg, logging.getLogger("test"))

    assert resolved is not None
    target, episode = resolved
    assert target.id == 5
    assert episode == 3


def test_update_progress_rejects_absolute_number_above_known_total() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if "SaveMediaListEntry" in payload["query"]:
            raise AssertionError("invalid absolute progress must never reach AniList mutation")
        return httpx.Response(
            200,
            json={
                "data": {
                    "Media": {
                        "id": 5,
                        "episodes": 10,
                        "format": "TV",
                        "mediaListEntry": {"id": 99, "progress": 2, "status": "CURRENT"},
                    }
                }
            },
        )

    client = _client(handler)
    try:
        result = client.update_progress(5, 43, 10)
    finally:
        client.close()

    assert len(requests) == 1
    assert result == {
        "updated": False,
        "progress": 2,
        "status": "CURRENT",
        "reason": "progress_exceeds_total",
        "requested_progress": 43,
        "total_episodes": 10,
    }


def test_update_progress_prefers_anilist_total_over_stale_local_total() -> None:
    seen_mutation: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "SaveMediaListEntry" not in payload["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "Media": {
                            "id": 5,
                            "episodes": 13,
                            "format": "TV",
                            "mediaListEntry": {"id": 99, "progress": 10, "status": "CURRENT"},
                        }
                    }
                },
            )
        seen_mutation.update(payload["variables"])
        return httpx.Response(
            200,
            json={"data": {"SaveMediaListEntry": {"id": 99, "progress": 11, "status": "CURRENT"}}},
        )

    client = _client(handler)
    try:
        result = client.update_progress(5, 11, 10)
    finally:
        client.close()

    assert seen_mutation == {"mediaId": 5, "progress": 11, "status": "CURRENT"}
    assert result["updated"] is True
    assert result["status"] == "CURRENT"
