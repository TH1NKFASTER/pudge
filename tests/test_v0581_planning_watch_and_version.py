from __future__ import annotations

import json
from pathlib import Path

import httpx

from pudge import __version__, cli
from pudge.anilist_tracking import TrackingPayload, create_tracking_file
from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.providers.anilist import AniListClient
from pudge.web_app import WebAppApi
from tests.i18n_support import en, ru


def _client(handler) -> AniListClient:
    client = AniListClient("https://graphql.anilist.co", access_token="secret")
    client.client.close()
    client.client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer secret"},
    )
    return client


def test_watching_planned_episode_moves_entry_to_current_even_with_existing_progress() -> None:
    mutations: list[dict[str, object]] = []

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
                                "progress": 1,
                                "status": "PLANNING",
                            },
                        }
                    }
                },
            )
        mutations.append(payload["variables"])
        return httpx.Response(
            200,
            json={
                "data": {
                    "SaveMediaListEntry": {
                        "id": 99,
                        "progress": payload["variables"]["progress"],
                        "status": payload["variables"]["status"],
                    }
                }
            },
        )

    client = _client(handler)
    try:
        result = client.update_progress(10, 1, 12)
    finally:
        client.close()

    assert mutations == [{"mediaId": 10, "progress": 1, "status": "CURRENT"}]
    assert result == {
        "updated": True,
        "progress": 1,
        "status": "CURRENT",
        "reason": "started_watching",
    }


def test_watching_planned_final_episode_or_movie_moves_entry_to_completed() -> None:
    mutations: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if "mediaListEntry" in payload["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "Media": {
                            "id": 20,
                            "episodes": 1,
                            "format": "MOVIE",
                            "mediaListEntry": {
                                "id": 199,
                                "progress": 0,
                                "status": "PLANNING",
                            },
                        }
                    }
                },
            )
        mutations.append(payload["variables"])
        return httpx.Response(
            200,
            json={
                "data": {
                    "SaveMediaListEntry": {
                        "id": 199,
                        "progress": 1,
                        "status": "COMPLETED",
                    }
                }
            },
        )

    client = _client(handler)
    try:
        result = client.update_progress(20, 1, 1)
    finally:
        client.close()

    assert mutations == [{"mediaId": 20, "progress": 1, "status": "COMPLETED"}]
    assert result["status"] == "COMPLETED"
    assert result["reason"] == "completed_from_planning"


def test_version_is_exposed_in_regular_and_advanced_settings(tmp_path: Path, page) -> None:
    api = WebAppApi(tmp_path / "config.toml")
    settings = json.loads(json.dumps(api._settings_payload(), default=str))
    assert settings["version"] == __version__
    assert en("settings.version") == "Version"
    assert ru("settings.version") == "Версия"
    code = "return {line: __text('#settingsContent .settings-header p'), label: t('settings.version')};"
    for lang in ("en", "ru"):
        shown = page.run(code, lang=lang, state={"settings": settings}).value
        assert shown["line"].startswith(f"{shown['label']}: {__version__}")
    unknown = page.run(code, state={"settings": {**settings, "version": ""}}).value
    assert unknown["line"].startswith(f"{unknown['label']}: —")
    # The Tk advanced settings window (pudge/settings_ui.py) was removed in 68Б; no second surface to check.


def test_confirmed_watch_updates_planned_local_entry_and_episode(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.access_token = "token"
    cfg.playback.enabled = True

    video = tmp_path / "Planned Show - 01.mkv"
    video.write_bytes(b"video")
    db = Database(cfg.library.database_path)
    db.upsert_anime(LibraryAnime(media_id=30, title="Planned Show", status="PLANNING", episodes=12))
    db.upsert_episode(LibraryEpisode(30, "Planned Show", 1, video, state="ready"))
    db.record_playback(video, 1000, 1400, active_seconds=1000)
    tracking = create_tracking_file(
        cfg.paths.cache_dir,
        TrackingPayload(
            video=str(video),
            title="Planned Show",
            media_id=30,
            episode=1,
            total_episodes=12,
            threshold=5 / 6,
            mapping_key="planned-show",
        ),
    )

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def update_progress(self, media_id, progress, total_episodes, **_kwargs):
            assert (media_id, progress, total_episodes) == (30, 1, 12)
            return {
                "updated": True,
                "progress": 1,
                "status": "CURRENT",
                "reason": "started_watching",
            }

        def close(self):
            pass

    monkeypatch.setattr(cli, "AniListClient", Client)
    args = type(
        "Args",
        (),
        {
            "tracking_file": tracking,
            "anilist_action": "update",
            "anilist_id": None,
            "manual": False,
        },
    )()

    assert cli._run_anilist_action(args, cfg) == 0
    assert db.get_anime(30).status == "CURRENT"
    assert db.get_anime(30).progress == 1
    episode = db.episode_by_path(video)
    assert episode is not None
    assert episode.state == "watched"
    assert episode.delete_after is not None
