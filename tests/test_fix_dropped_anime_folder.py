"""An anime folder dropped on the window from anywhere; season packs of a whole-season drop.

The Cyberpunk: Edgerunners 2 season pack ("[Erai-raws] Cyberpunk - Edgerunners 2 - 01 ~ 10
[...]") was dropped on the window from ~/Downloads/torrents, which was not a watched folder
then: a dropped folder was only checked for audiobook files and manga pages, so nothing was
imported. And Netflix released the season at once with only a season pack on the tracker:
the Planning premiere search asked for episode 1, which drops packs, so no offer came.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pudge.manager as manager_module
from pudge.config import write_config
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.planned_release_discovery import _SEARCH_REVISION, PlannedReleaseDiscovery
from test_web_app import make_api

PACK = "[Erai-raws] Cyberpunk - Edgerunners 2 - 01 ~ 10 [1080p NF WEB-DL AVC EAC3][MultiSub]"


def _edgerunners(**values) -> LibraryAnime:
    fields = {
        "media_id": 195539,
        "title": "Cyberpunk: Edgerunners 2",
        "titles": ["Cyberpunk: Edgerunners 2"],
        "synonyms": ["Cyberpunk: Edgerunners II"],
        "episodes": 10,
        "format": "ONA",
        "status": "PLANNING",
        "media_status": "FINISHED",
        "start_date": "2026-10-20",
        "end_date": "2026-10-20",
    }
    fields.update(values)
    return LibraryAnime(**fields)


def _no_anilist(monkeypatch) -> list[str]:
    searched: list[str] = []

    class Client:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def search(self, identity):
            searched.append(identity.title)
            return []

        def close(self) -> None:
            pass

    monkeypatch.setattr(manager_module, "AniListClient", Client)
    monkeypatch.setattr("pudge.library.japanese_subtitle_details", lambda *_a, **_k: ("none", None, None))
    return searched


def _pack_folder(root: Path) -> tuple[Path, list[Path]]:
    folder = root / "torrents" / PACK
    folder.mkdir(parents=True)
    videos = []
    for number in range(1, 11):
        video = folder / (
            f"[Erai-raws] Cyberpunk - Edgerunners 2 - {number:02d} [1080p NF WEB-DL AVC EAC3][MultiSub][ABCD12{number:02d}].mkv"
        )
        video.write_bytes(b"video")
        videos.append(video)
    (folder / ".DS_Store").write_bytes(b"x")
    return folder, videos


def test_dropped_anime_folder_outside_watched_folders_is_imported_and_kept(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    searched = _no_anilist(monkeypatch)
    api.manager.db.upsert_anime(_edgerunners())
    folder, videos = _pack_folder(tmp_path / "Downloads")
    assert not any(folder.is_relative_to(Path(p)) for p in api.config.paths.download_dirs)

    result = api.import_dropped_paths([str(folder)])

    assert result["ok"] is True and result["errors"] == []
    assert [row["kind"] for row in result["imports"]] == ["anime"] * 10
    assert {row["media_id"] for row in result["imports"]} == {195539}
    assert sorted(row["episode"] for row in result["imports"]) == list(range(1, 11))
    assert {api.manager.db.episode_by_path(video).media_id for video in videos} == {195539}
    assert searched == []  # matched to the Planning entry without asking AniList
    # Only the dropped folder is remembered, not ~/Downloads/torrents; Settings are unchanged.
    assert api.manager.dropped_media_folders() == (folder.resolve(),)
    assert api.config.paths.download_dirs == []
    # Later scans keep the episodes (they used to be removed as outside every library folder).
    api.manager.scan_library()
    assert all(api.manager.db.episode_by_path(video) is not None for video in videos)


def test_folder_without_a_recognized_anime_is_reported_and_forgotten(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    _no_anilist(monkeypatch)
    folder = tmp_path / "Home videos"
    folder.mkdir()
    (folder / "IMG_0001.mov").write_bytes(b"video")

    result = api.import_dropped_paths([str(folder)])

    assert result["ok"] is False and result["imports"] == []
    assert result["failed"][0]["name"] == "Home videos"
    assert api.manager.dropped_media_folders() == ()


def test_folder_inside_a_watched_folder_is_not_remembered_twice(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    _no_anilist(monkeypatch)
    api.manager.db.upsert_anime(_edgerunners())
    watched = tmp_path / "Downloads"
    folder, videos = _pack_folder(watched)
    api.config.paths.download_dirs = [watched]
    write_config(api.config, api.config_path)

    result = api.import_dropped_paths([str(folder)])

    assert len(result["imports"]) == 10
    assert api.manager.dropped_media_folders() == ()


def _release(title: str, *, size: int, batch: bool = False) -> NyaaRelease:
    return NyaaRelease(
        title,
        "https://example.org/" + title,
        "https://example.org/t.torrent",
        "e" * 40,
        f"{size} GiB",
        size * 1024**3,
        800,
        10,
        50,
        True,
        False,
        score=470,
        is_batch=batch,
    )


class DB:
    def __init__(self, anime):
        self.anime = {item.media_id: item for item in anime}
        self.state = {}

    def anime_list(self, statuses=None):
        return [item for item in self.anime.values() if statuses is None or item.status in statuses]

    def get_anime(self, media_id):
        return self.anime.get(media_id)

    def get_state(self, key, default=""):
        return self.state.get(key, default)

    def set_state(self, key, value):
        self.state[key] = value


def _discovery(tmp_path, anime, *, episode_results, pack_results):
    db = DB([anime])
    db.state["planned_release_discovery:v1:195539"] = json.dumps(
        {
            "phase": "armed",
            "airing_at": 1792479600.0,
            "airing_source": "next_episode_1",
            "next_check": 1792479600.0 + 900,
            "attempts": 1,
            "search_revision": _SEARCH_REVISION,
        }
    )

    def search(media_id, *, episode, batch, automatic, allow_anilist_network):
        return list(pack_results if batch else episode_results)

    manager = SimpleNamespace(
        db=db,
        config=SimpleNamespace(paths=SimpleNamespace(cache_dir=tmp_path), nyaa=SimpleNamespace(enabled=True)),
        search_releases=Mock(side_effect=search),
        _release_is_allowed_for_auto=Mock(return_value=True),
        _release_has_safe_episode_identity=Mock(return_value=False),  # a pack is no episode
        _release_is_safe_batch_candidate=Mock(return_value=True),
        logger=logging.getLogger("test-season-pack-discovery"),
    )
    return PlannedReleaseDiscovery(manager), manager


def test_finished_season_with_only_a_pack_is_offered(tmp_path):
    pack = _release(PACK, size=12, batch=True)
    service, manager = _discovery(tmp_path, _edgerunners(), episode_results=[], pack_results=[pack])

    result = service.tick(now=1792573000.0)

    assert result["found"] == 1
    assert [call.kwargs["batch"] for call in manager.search_releases.call_args_list] == [False, True]
    assert [offer["media_id"] for offer in service.offers()] == [195539]


def test_airing_show_does_not_search_packs(tmp_path):
    service, manager = _discovery(
        tmp_path, _edgerunners(media_status="RELEASING", end_date=None), episode_results=[], pack_results=[]
    )

    result = service.tick(now=1792573000.0)

    assert result["found"] == 0
    assert [call.kwargs["batch"] for call in manager.search_releases.call_args_list] == [False]
