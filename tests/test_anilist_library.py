from __future__ import annotations

from anime_mpv.providers.anilist import AniListClient


def test_anilist_library_parses_current_and_planning(monkeypatch) -> None:
    client = AniListClient("https://example.invalid", access_token="x")
    monkeypatch.setattr(client, "viewer", lambda: {"id": 7, "name": "tester"})

    def fake_post(query, variables=None):
        return {
            "MediaListCollection": {
                "lists": [
                    {
                        "status": "CURRENT",
                        "entries": [
                            {
                                "progress": 5,
                                "status": "CURRENT",
                                "media": {
                                    "id": 100,
                                    "title": {"romaji": "Anime A", "english": "Anime A"},
                                    "synonyms": [],
                                    "episodes": 12,
                                    "format": "TV",
                                    "endDate": {"year": 2026, "month": 8, "day": 1},
                                    "siteUrl": "https://anilist.co/anime/100",
                                    "coverImage": {"large": "https://img/a.jpg"},
                                    "nextAiringEpisode": {"episode": 7, "airingAt": 123456},
                                },
                            }
                        ],
                    },
                    {
                        "status": "COMPLETED",
                        "entries": [],
                    },
                ]
            }
        }

    monkeypatch.setattr(client, "_post", fake_post)
    try:
        items = client.library()
    finally:
        client.close()
    assert len(items) == 1
    assert items[0].media_id == 100
    assert items[0].progress == 5
    assert items[0].next_airing_episode == 7
    assert items[0].end_date == "2026-08-01"


def test_episode_airing_at_uses_exact_schedule_query(monkeypatch) -> None:
    client = AniListClient("https://example.invalid", access_token="x")
    seen = {}

    def fake_post(query, variables=None):
        seen["query"] = query
        seen["variables"] = variables
        return {"AiringSchedule": {"episode": 5, "airingAt": 1785600000}}

    monkeypatch.setattr(client, "_post", fake_post)
    try:
        result = client.episode_airing_at(123, 5)
    finally:
        client.close()

    assert result == 1785600000
    assert seen["variables"] == {"mediaId": 123, "episode": 5}


def test_library_maps_episode_duration(monkeypatch) -> None:
    from anime_mpv.providers.anilist import AniListClient

    client = AniListClient("https://example.invalid")
    responses = iter(
        [
            {"Viewer": {"id": 1, "name": "test"}},
            {
                "MediaListCollection": {
                    "lists": [
                        {
                            "status": "CURRENT",
                            "entries": [
                                {
                                    "progress": 0,
                                    "status": "CURRENT",
                                    "media": {
                                        "id": 42,
                                        "title": {"romaji": "Example"},
                                        "synonyms": [],
                                        "episodes": 12,
                                        "duration": 24,
                                        "format": "TV",
                                        "status": "RELEASING",
                                        "meanScore": 70,
                                        "siteUrl": "",
                                        "coverImage": {},
                                        "nextAiringEpisode": None,
                                    },
                                }
                            ],
                        }
                    ]
                }
            },
        ]
    )
    monkeypatch.setattr(client, "_post", lambda *args, **kwargs: next(responses))
    try:
        result = client.library()
    finally:
        client.close()
    assert result[0].duration == 24
