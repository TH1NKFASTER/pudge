from __future__ import annotations

import json

import httpx

from anime_mpv.providers.anilist import AniListClient


def _client(handler) -> AniListClient:
    client = AniListClient("https://graphql.anilist.co", access_token="secret")
    client.client.close()
    client.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer secret"},
    )
    return client


def test_update_progress_mutates_and_completes_final_episode():
    seen_queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer secret"
        payload = json.loads(request.content)
        query = payload["query"]
        seen_queries.append(query)
        if "mediaListEntry" in query:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "Media": {
                            "id": 10,
                            "episodes": 12,
                            "format": "TV",
                            "mediaListEntry": {
                                "id": 99,
                                "progress": 11,
                                "status": "CURRENT",
                            },
                        }
                    }
                },
            )
        variables = payload["variables"]
        assert variables == {"mediaId": 10, "progress": 12, "status": "COMPLETED"}
        return httpx.Response(
            200,
            json={
                "data": {
                    "SaveMediaListEntry": {
                        "id": 99,
                        "progress": 12,
                        "status": "COMPLETED",
                    }
                }
            },
        )

    client = _client(handler)
    try:
        result = client.update_progress(10, 12, 12)
    finally:
        client.close()

    assert result["updated"] is True
    assert result["progress"] == 12
    assert result["status"] == "COMPLETED"
    assert len(seen_queries) == 2


def test_update_progress_never_decreases_existing_progress():
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "data": {
                    "Media": {
                        "id": 10,
                        "episodes": 12,
                        "format": "TV",
                        "mediaListEntry": {
                            "id": 99,
                            "progress": 8,
                            "status": "CURRENT",
                        },
                    }
                }
            },
        )

    client = _client(handler)
    try:
        result = client.update_progress(10, 7, 12)
    finally:
        client.close()

    assert result == {
        "updated": False,
        "progress": 8,
        "status": "CURRENT",
        "reason": "already_at_or_above",
    }
    assert request_count == 1


def test_update_progress_does_not_add_missing_entry_by_default():
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={
                "data": {
                    "Media": {
                        "id": 10,
                        "episodes": 12,
                        "format": "TV",
                        "mediaListEntry": None,
                    }
                }
            },
        )

    client = _client(handler)
    try:
        result = client.update_progress(10, 1, 12)
    finally:
        client.close()

    assert result["updated"] is False
    assert result["reason"] == "not_on_list"
    assert request_count == 1


def test_update_progress_starts_rewatching_in_two_mutations():
    mutations: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "mediaListEntry" in payload["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "Media": {
                            "id": 10,
                            "episodes": 12,
                            "format": "TV",
                            "mediaListEntry": {
                                "id": 99,
                                "progress": 12,
                                "status": "COMPLETED",
                            },
                        }
                    }
                },
            )
        mutations.append(payload["variables"])
        variables = payload["variables"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "SaveMediaListEntry": {
                        "id": 99,
                        "progress": variables["progress"],
                        "status": variables.get("status") or "REPEATING",
                    }
                }
            },
        )

    client = _client(handler)
    try:
        result = client.update_progress(
            10,
            1,
            12,
            completed_to_rewatching_on_episode_one=True,
        )
    finally:
        client.close()

    assert mutations == [
        {"mediaId": 10, "progress": 0, "status": "REPEATING"},
        {"mediaId": 10, "progress": 1},
    ]
    assert result["reason"] == "started_rewatching"
    assert result["status"] == "REPEATING"


def test_update_progress_does_not_revive_dropped_entry():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "Media": {
                        "id": 10,
                        "episodes": 12,
                        "format": "TV",
                        "mediaListEntry": {
                            "id": 99,
                            "progress": 4,
                            "status": "DROPPED",
                        },
                    }
                }
            },
        )

    client = _client(handler)
    try:
        result = client.update_progress(10, 5, 12)
    finally:
        client.close()

    assert result["updated"] is False
    assert result["reason"] == "status_not_modifiable"
    assert result["status"] == "DROPPED"


def test_resolve_absolute_episode_across_sequel_cours():
    relation_map = {
        101: {
            "id": 101,
            "title": {"romaji": "Bleach: Sennen Kessen-hen"},
            "synonyms": [],
            "seasonYear": 2022,
            "episodes": 13,
            "format": "TV",
            "relations": {
                "edges": [
                    {
                        "relationType": "SEQUEL",
                        "node": {
                            "id": 102,
                            "title": {"romaji": "Bleach: Sennen Kessen-hen - Ketsubetsu-tan"},
                            "synonyms": [],
                            "seasonYear": 2023,
                            "episodes": 13,
                            "format": "TV",
                        },
                    }
                ]
            },
        },
        102: {
            "id": 102,
            "title": {"romaji": "Bleach: Sennen Kessen-hen - Ketsubetsu-tan"},
            "synonyms": [],
            "seasonYear": 2023,
            "episodes": 13,
            "format": "TV",
            "relations": {
                "edges": [
                    {
                        "relationType": "SEQUEL",
                        "node": {
                            "id": 103,
                            "title": {"romaji": "Bleach: Sennen Kessen-hen - Soukoku-tan"},
                            "synonyms": [],
                            "seasonYear": 2024,
                            "episodes": 14,
                            "format": "TV",
                        },
                    }
                ]
            },
        },
        103: {
            "id": 103,
            "title": {"romaji": "Bleach: Sennen Kessen-hen - Soukoku-tan"},
            "synonyms": [],
            "seasonYear": 2024,
            "episodes": 14,
            "format": "TV",
            "relations": {
                "edges": [
                    {
                        "relationType": "SEQUEL",
                        "node": {
                            "id": 104,
                            "title": {"romaji": "Bleach: Sennen Kessen-hen - Kashin-tan"},
                            "synonyms": [],
                            "seasonYear": 2026,
                            "episodes": 13,
                            "format": "TV",
                        },
                    }
                ]
            },
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        media_id = payload["variables"]["mediaId"]
        return httpx.Response(200, json={"data": {"Media": relation_map[media_id]}})

    client = _client(handler)
    start = __import__("anime_mpv.models", fromlist=["AniListAnime"]).AniListAnime(
        id=101,
        titles=["Bleach: Thousand-Year Blood War"],
        synonyms=[],
        season_year=2022,
        episodes=13,
        format="TV",
    )
    try:
        resolved = client.resolve_absolute_episode(start, 42)
    finally:
        client.close()

    assert resolved is not None
    anime, episode, chain = resolved
    assert anime.id == 104
    assert episode == 2
    assert [item.id for item in chain] == [101, 102, 103, 104]


def test_resolve_absolute_episode_returns_none_without_main_sequel():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "Media": {
                        "id": 101,
                        "title": {"romaji": "Example"},
                        "synonyms": [],
                        "seasonYear": 2020,
                        "episodes": 12,
                        "format": "TV",
                        "relations": {
                            "edges": [
                                {
                                    "relationType": "SIDE_STORY",
                                    "node": {
                                        "id": 999,
                                        "title": {"romaji": "Example Special"},
                                        "synonyms": [],
                                        "seasonYear": 2020,
                                        "episodes": 1,
                                        "format": "SPECIAL",
                                    },
                                }
                            ]
                        },
                    }
                }
            },
        )

    client = _client(handler)
    start = __import__("anime_mpv.models", fromlist=["AniListAnime"]).AniListAnime(
        id=101,
        titles=["Example"],
        synonyms=[],
        season_year=2020,
        episodes=12,
        format="TV",
    )
    try:
        resolved = client.resolve_absolute_episode(start, 13)
    finally:
        client.close()

    assert resolved is None


def test_resolve_absolute_episode_recovers_root_from_cached_later_cour():
    media = {
        201: {
            "id": 201,
            "title": {"english": "Bleach: Thousand-Year Blood War"},
            "synonyms": [],
            "seasonYear": 2022,
            "episodes": 13,
            "format": "TV",
        },
        202: {
            "id": 202,
            "title": {"english": "Bleach: Thousand-Year Blood War Part 2"},
            "synonyms": [],
            "seasonYear": 2023,
            "episodes": 13,
            "format": "TV",
        },
        203: {
            "id": 203,
            "title": {"english": "Bleach: Thousand-Year Blood War Part 3"},
            "synonyms": [],
            "seasonYear": 2024,
            "episodes": 14,
            "format": "TV",
        },
        204: {
            "id": 204,
            "title": {"english": "Bleach: Thousand-Year Blood War Part 4"},
            "synonyms": [],
            "seasonYear": 2026,
            "episodes": 13,
            "format": "TV",
        },
        999: {
            "id": 999,
            "title": {"english": "Bleach"},
            "synonyms": [],
            "seasonYear": 2004,
            "episodes": 366,
            "format": "TV",
        },
    }
    relations = {
        201: [("PREQUEL", 999), ("SEQUEL", 202)],
        202: [("PREQUEL", 201), ("SEQUEL", 203)],
        203: [("PREQUEL", 202), ("SEQUEL", 204)],
        204: [("PREQUEL", 203)],
        999: [("SEQUEL", 201)],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        media_id = payload["variables"]["mediaId"]
        item = dict(media[media_id])
        item["relations"] = {
            "edges": [
                {"relationType": relation_type, "node": media[target]}
                for relation_type, target in relations[media_id]
            ]
        }
        return httpx.Response(200, json={"data": {"Media": item}})

    client = _client(handler)
    cached_fourth = __import__("anime_mpv.models", fromlist=["AniListAnime"]).AniListAnime(
        id=204,
        titles=["Bleach: Thousand-Year Blood War Part 4"],
        synonyms=[],
        season_year=2026,
        episodes=13,
        format="TV",
    )
    try:
        resolved = client.resolve_absolute_episode(cached_fourth, 42)
    finally:
        client.close()

    assert resolved is not None
    anime, episode, chain = resolved
    assert anime.id == 204
    assert episode == 2
    assert [item.id for item in chain] == [201, 202, 203, 204]


def test_set_score_uses_score_raw_on_ten_point_ui():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.update(payload["variables"])
        return httpx.Response(
            200,
            json={"data": {"SaveMediaListEntry": {"id": 9, "score": 8.0, "status": "COMPLETED"}}},
        )

    client = _client(handler)
    try:
        result = client.set_score(123, 8)
    finally:
        client.close()

    assert seen == {"mediaId": 123, "scoreRaw": 80}
    assert result["score"] == 8.0


def test_set_status_and_delete_list_entry():
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        query = payload["query"]
        if "mediaListEntry" in query:
            return httpx.Response(
                200,
                json={"data": {"Media": {"id": 123, "mediaListEntry": {"id": 456, "progress": 0, "status": "PLANNING"}}}},
            )
        if "DeleteMediaListEntry" in query:
            return httpx.Response(200, json={"data": {"DeleteMediaListEntry": {"deleted": True}}})
        return httpx.Response(
            200,
            json={"data": {"SaveMediaListEntry": {"id": 456, "progress": 0, "status": "DROPPED"}}},
        )

    client = _client(handler)
    try:
        saved = client.set_list_status(123, "DROPPED")
        deleted = client.delete_list_entry(123)
    finally:
        client.close()

    assert saved["status"] == "DROPPED"
    assert requests[0]["variables"] == {"mediaId": 123, "status": "DROPPED"}
    assert requests[-1]["variables"] == {"id": 456}
    assert deleted is True
