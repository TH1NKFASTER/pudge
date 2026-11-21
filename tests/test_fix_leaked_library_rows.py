"""Benchmark data that leaked into the real library, and anime rows titled with a file name.

Maintainer's library: a benchmark harness ran Pudge's code against the real state. It left a
ready "Jujutsu Kaisen" episode (a video in ~/Downloads/pudge-benchmark-production/corpus) that
the iPhone offered, and 78 anime rows whose title was a release file name
("[HorribleSubs] Youjo Senki - 03 [1080p].mkv", titles_json "[]"), 13 of them Completed
list entries that the AniList list sync never filled ("Youjo Senki COMPLETED 12").
The parent row of an episode, download, release choice or torrent observation was created
with whatever name the caller had (Database._ensure_anime_parent).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pudge.filename import looks_like_release_name, release_name_display_title
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.providers.anilist import AniListClient, AniListHTTPError, AniListNotFoundError
from test_web_app import make_api


@pytest.mark.parametrize(
    ("name", "title"),
    [
        ("[Erai-raws] Jujutsu Kaisen - 05 [480p][Multiple Subtitle].mkv", "Jujutsu Kaisen"),
        ("[HorribleSubs] Youjo Senki - 03 [1080p].mkv", "Youjo Senki"),
        ("Solo.Leveling.S01E11.1080p.WEB.H264-VARYG.mkv", "Solo Leveling"),
        ("[SubsPlease] Gekidol (01-12) (720p) [Batch]", "Gekidol"),
        ("[1080p].mkv", "AniList 7"),
    ],
)
def test_a_release_name_becomes_its_series_title(name, title):
    assert looks_like_release_name(name)
    assert release_name_display_title(name, 7) == title


@pytest.mark.parametrize(
    "title",
    [
        "[Oshi no Ko]",
        "[Oshi no Ko] Final Season",
        "Steins;Gate 0",
        "Mob Psycho 100",
        "86 Eighty-Six",
        "Dr. Stone",
        "2.5 Dimensional Seduction",
        "AniList 5",
    ],
)
def test_genuine_titles_are_no_release_names(title):
    assert not looks_like_release_name(title)
    assert release_name_display_title(title, 5) == title


def test_placeholder_parent_rows_never_carry_a_file_name(tmp_path):
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_download(
        DownloadItem(
            torrent_hash="aa" * 20,
            name="[HorribleSubs] Youjo Senki - 03 [1080p].mkv",
            state="active",
            progress=0.5,
            save_path=str(tmp_path),
            content_path=str(tmp_path / "x.mkv"),
            media_id=21613,
        )
    )
    video = tmp_path / "Solo.Leveling.S01E11.1080p.WEB.H264-VARYG.mkv"
    video.write_bytes(b"v")
    db.upsert_episode(LibraryEpisode(151807, video.stem, 11, video))
    db.record_release("bb" * 20, 97940, 170, "[SubsPlease] Black Clover - 170 (1080p) [ABCDEF01].mkv", 1.0)
    assert db.get_anime(21613).title == "Youjo Senki"
    assert db.get_anime(97940).title == "Black Clover"
    assert db.get_anime(151807).title == "Solo Leveling"
    # An existing row keeps its title (INSERT OR IGNORE).
    db.upsert_anime(LibraryAnime(1, "[Oshi no Ko]", titles=["[Oshi no Ko]"], status="CURRENT"))
    db.upsert_download(
        DownloadItem(
            torrent_hash="cc" * 20,
            name="[SubsPlease] Oshi no Ko - 01 (1080p).mkv",
            state="active",
            progress=0.1,
            save_path=str(tmp_path),
            content_path=str(tmp_path / "y.mkv"),
            media_id=1,
        )
    )
    assert db.get_anime(1).title == "[Oshi no Ko]"


def test_torrent_observation_parent_is_not_titled_with_the_release(tmp_path):
    from types import SimpleNamespace

    from pudge.manager_services import ReleaseTelemetryService

    api = make_api(tmp_path)
    release = SimpleNamespace(
        info_hash="DD" * 20, title="[Erai-raws] Jujutsu Kaisen - 05 [480p][Multiple Subtitle].mkv"
    )
    ReleaseTelemetryService(api.manager.db).record(
        release, media_id=113415, media_episode=5, provider="nyaa", outcome="added"
    )
    with api.manager.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM torrent_observations").fetchone()[0] == 1
    assert api.manager.db.get_anime(113415).title == "Jujutsu Kaisen"


def test_anilist_list_answer_keeps_metadata_of_every_entry(monkeypatch):
    def media(media_id, romaji):
        return {
            "id": media_id,
            "title": {"romaji": romaji, "english": None, "native": None, "userPreferred": romaji},
            "episodes": 12,
            "status": "FINISHED",
            "format": "TV",
        }

    payload = {
        "MediaListCollection": {
            "lists": [
                {
                    "status": "CURRENT",
                    "entries": [{"status": "CURRENT", "progress": 1, "media": media(1, "A")}],
                },
                {
                    "status": "COMPLETED",
                    "entries": [
                        {"status": "COMPLETED", "progress": 12, "media": media(21613, "Youjo Senki")}
                    ],
                },
            ]
        }
    }
    client = AniListClient("https://example.invalid/graphql", "token")
    monkeypatch.setattr(client, "viewer_id", lambda: 1)
    monkeypatch.setattr(client, "_post", lambda query, variables=None: payload)
    try:
        items = client.library()
    finally:
        client.close()
    assert [item.media_id for item in items] == [1]
    assert set(client.last_list_media) == {1, 21613}
    assert client.last_list_media[21613].titles == ["Youjo Senki"]
    assert client.last_list_media[21613].media_status == "FINISHED"


def _list_client(other_media: dict[int, LibraryAnime], entries: dict[int, dict]):
    class Client:
        def __init__(self, *_args, **_kwargs):
            self.last_list_entries = dict(entries)
            self.last_list_media = dict(other_media)
            self.last_list_incomplete = False

        def library_compact(self):
            return []

        def close(self):
            pass

    return Client


def test_list_sync_fills_completed_placeholder_rows(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    db = api.manager.db
    # As the maintainer's row was: a list entry whose metadata was never fetched.
    db.upsert_anime(
        LibraryAnime(21613, "[HorribleSubs] Youjo Senki - 03 [1080p].mkv", status="COMPLETED", progress=12)
    )
    db.upsert_anime(
        LibraryAnime(5, "Real", titles=["Real"], status="COMPLETED", progress=3, cover_url="keep")
    )
    metadata = {
        21613: LibraryAnime(
            21613,
            "Youjo Senki",
            titles=["Youjo Senki", "Saga of Tanya the Evil"],
            status="COMPLETED",
            progress=12,
            episodes=12,
            media_status="FINISHED",
            cover_url="https://img/tanya.jpg",
        ),
        5: LibraryAnime(5, "Other", titles=["Other"], status="COMPLETED", progress=3, cover_url="new"),
    }
    entries = {
        21613: {"status": "COMPLETED", "progress": 12, "episodes": 12},
        5: {"status": "COMPLETED", "progress": 3, "episodes": 3},
    }
    monkeypatch.setattr("pudge.manager.AniListClient", _list_client(metadata, entries))
    api.manager.config.anilist.enabled = True
    api.manager.config.anilist.access_token = "token"
    api.manager.sync_anilist()
    row = db.get_anime(21613)
    assert row.title == "Youjo Senki"
    assert row.titles == ["Youjo Senki", "Saga of Tanya the Evil"]
    assert row.media_status == "FINISHED" and row.episodes == 12
    assert row.status == "COMPLETED" and row.progress == 12
    # A row that already has AniList titles is left to the regular rules.
    assert db.get_anime(5).title == "Real" and db.get_anime(5).cover_url == "keep"


def _seed_leaked_rows(db):
    with db.connect() as conn:
        for media_id, title, status, progress in (
            (21613, "[HorribleSubs] Youjo Senki - 03 [1080p].mkv", "COMPLETED", 12),
            (113415, "[Erai-raws] Jujutsu Kaisen - 05 [480p][Multiple Subtitle].mkv", "", 0),
            (999001, "[SubsPlease] Gekidol (01-12) (720p) [Batch]", "", 0),
        ):
            conn.execute(
                "INSERT INTO anime(media_id,title,titles_json,status,progress,updated_at) VALUES(?,?,?,?,?,0)",
                (media_id, title, "[]", status, progress),
            )
        conn.execute(
            "INSERT INTO anime(media_id,title,titles_json,status,updated_at) VALUES(?,?,?,?,0)",
            (150672, "[Oshi no Ko]", json.dumps(["[Oshi no Ko]", "Oshi no Ko"]), "CURRENT"),
        )


class _MediaClient:
    calls: list[int] = []
    fail_after: int | None = None

    def __init__(self, *_args, **_kwargs):
        pass

    def library_anime(self, media_id):
        _MediaClient.calls.append(int(media_id))
        if _MediaClient.fail_after is not None and len(_MediaClient.calls) > _MediaClient.fail_after:
            raise AniListHTTPError("rate limited", status_code=429)
        if media_id == 999001:
            raise AniListNotFoundError("gone")
        names = {21613: "Youjo Senki", 113415: "Jujutsu Kaisen"}
        return LibraryAnime(
            int(media_id),
            names[int(media_id)],
            titles=[names[int(media_id)]],
            status="",
            media_status="FINISHED",
            episodes=12 if media_id == 21613 else 24,
        )

    def close(self):
        pass


def test_startup_repair_renames_then_fetches_metadata_once(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    db = api.manager.db
    _seed_leaked_rows(db)
    _MediaClient.calls, _MediaClient.fail_after = [], None
    monkeypatch.setattr("pudge.manager.AniListClient", _MediaClient)

    # Without a token: names are cleaned at once, nothing is fetched.
    api.manager.config.anilist.access_token = ""
    first = api.manager.repair_placeholder_anime_titles()
    assert first["renamed"] == 3 and first["fetched"] == 0
    assert db.get_anime(21613).title == "Youjo Senki"
    assert db.get_anime(113415).title == "Jujutsu Kaisen"
    assert db.get_anime(999001).title == "Gekidol"
    assert db.get_anime(150672).title == "[Oshi no Ko]"
    assert _MediaClient.calls == []

    api.manager.config.anilist.enabled = True
    api.manager.config.anilist.access_token = "token"
    lines: list[str] = []
    monkeypatch.setattr(api.manager.logger, "info", lambda msg, *args: lines.append(msg % args))
    second = api.manager.repair_placeholder_anime_titles()
    assert second == {"renamed": 0, "fetched": 2, "missing": 1, "pending": 3}
    assert _MediaClient.calls[0] == 21613  # on the list first
    row = db.get_anime(21613)
    assert row.titles == ["Youjo Senki"] and row.media_status == "FINISHED"
    assert row.status == "COMPLETED" and row.progress == 12  # list fields stay local
    assert db.get_anime(113415).titles == ["Jujutsu Kaisen"]
    assert any(
        line.startswith("RESULT step=anime.placeholder_titles fetched") or "placeholder_titles" in line
        for line in lines
    )
    assert not any("release_name_title" in line for line in lines)  # already renamed

    # Idempotent: nothing left to ask, the unknown id is not asked again; no row deleted.
    _MediaClient.calls = []
    third = api.manager.repair_placeholder_anime_titles()
    assert third["fetched"] == 0 and third["renamed"] == 0 and _MediaClient.calls == []
    assert db.get_anime(999001) is not None


def test_startup_repair_stops_at_a_rate_limit_and_continues_next_time(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    _seed_leaked_rows(api.manager.db)
    _MediaClient.calls, _MediaClient.fail_after = [], 1
    monkeypatch.setattr("pudge.manager.AniListClient", _MediaClient)
    api.manager.config.anilist.enabled = True
    api.manager.config.anilist.access_token = "token"
    result = api.manager.repair_placeholder_anime_titles()
    assert result["fetched"] == 1 and len(_MediaClient.calls) == 2
    _MediaClient.calls, _MediaClient.fail_after = [], None
    again = api.manager.repair_placeholder_anime_titles()
    assert again["fetched"] == 1 and again["missing"] == 1


def test_relation_graph_replaces_a_parsed_placeholder_title(tmp_path):
    from pudge import extra_files

    anime = LibraryAnime(3901, "Baccano", status="")
    node = {"media_id": 3901, "title": "Baccano! Bangai-hen", "format": "SPECIAL"}
    assert extra_files.refresh_own_title_metadata(anime, node, None)
    assert anime.title == "Baccano! Bangai-hen" and anime.titles == ["Baccano! Bangai-hen"]


# Episodes of a benchmark corpus (a folder with the ignore marker)


def _isolated(tmp_path, api):
    corpus = tmp_path / "pudge-benchmark-production" / "corpus"
    downloads = corpus / "downloads"
    downloads.mkdir(parents=True)
    (tmp_path / "pudge-benchmark-production" / AnimeManager._LIBRARY_SCAN_IGNORE_MARKER).write_text("")
    return downloads


def _ready(api, folder: Path, media_id: int, number: int) -> LibraryEpisode:
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"[Erai-raws] Jujutsu Kaisen - Shimetsu Kaiyuu - Zenpen - {number:02d} [1080p].mkv"
    video.write_bytes(b"video")
    sub = video.with_suffix(".ja.srt")
    sub.write_text("1\n00:00:01,000 --> 00:00:02,000\nあ\n", encoding="utf-8")
    item = LibraryEpisode(media_id, "Jujutsu Kaisen", number, video, state="ready", subtitle_path=sub)
    api.manager.db.upsert_episode(item)
    return item


def test_isolated_episode_is_never_offered_to_the_iphone_and_is_forgotten(tmp_path):
    from pudge.iphone_service import IPhoneService

    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(
        LibraryAnime(113415, "Jujutsu Kaisen", titles=["Jujutsu Kaisen"], status="CURRENT", episodes=24)
    )
    corpus = _isolated(tmp_path, api)
    leaked = _ready(api, corpus, 113415, 11)
    watched = _ready(api, corpus / "played", 113415, 12)
    db.record_playback(watched.video_path, 300.0, 1440.0)
    own = _ready(api, api.config.library.root_dir / "Jujutsu Kaisen", 113415, 13)
    db.queue_subtitle_job(leaked.video_path, 113415, 11)

    service = IPhoneService(db, tmp_path / "mobile")
    service.api = api
    media = service.media
    offered = [item for group in media.anime()["items"] for item in group["episodes"]]
    assert len(offered) == 1  # only the library's own copy

    assert api.manager.forget_isolated_library_episodes() == 1
    paths = {str(row.video_path) for row in db.episodes()}
    assert str(leaked.video_path) not in paths
    assert {str(watched.video_path), str(own.video_path)} <= paths  # watch history stays
    assert all(str(row["video_path"]) != str(leaked.video_path) for row in db.subtitle_jobs())
    assert leaked.video_path.read_bytes() == b"video"  # the file is never deleted
    assert api.manager.forget_isolated_library_episodes() == 0


def test_startup_runs_both_repairs(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    _seed_leaked_rows(api.manager.db)
    api.manager.db.upsert_anime(LibraryAnime(7, "X", titles=["X"], status="CURRENT"))
    corpus = _isolated(tmp_path, api)
    leaked = _ready(api, corpus, 7, 1)
    api.manager.config.anilist.access_token = ""
    api._repair_off_list_entries()
    assert all(row.video_path != leaked.video_path for row in api.manager.db.episodes())
    assert api.manager.db.get_anime(113415).title == "Jujutsu Kaisen"
