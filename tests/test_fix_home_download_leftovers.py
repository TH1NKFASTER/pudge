"""Leftover and finished downloads bring no Home card for good (review of the Suzume fix).

Since unfinished downloads of titles without a card show on Home: a Completed movie's 0 %
paused aria2 shell, an error row at 20 % or an "unknown" row stayed there forever, and the
shell cleanup never forgot a movie's shell (no episode number). A Planning/Current movie whose
file was deleted while its torrent still seeds showed "Waiting for subs" for good (or hid the
Download button). The last Home pass also skipped the not-yet-released guard and showed titles
removed from the list.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fix_home_movie_download import (
    MOVIE,
    MOVIE_FILE,
    MOVIE_HASH,
    _api,
    _cards,
    _home,
    _movie,
    _movie_download,
    _ready_file,
)

from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime
from pudge.web_app import WebAppApi


def _leftover(tmp_path: Path, state: str, progress: float, **raw) -> DownloadItem:
    item = _movie_download(tmp_path, state=state, progress=progress)
    item.raw.update(raw)
    return item


@pytest.mark.parametrize(
    ("state", "progress"),
    [("paused", 0.0), ("error", 0.2), ("unknown", 0.3), ("stopped", 0.5), ("missingfiles", 0.4)],
)
def test_a_watched_movies_idle_leftover_brings_no_card(tmp_path: Path, state: str, progress: float) -> None:
    api = _api(tmp_path)
    _movie(api, "COMPLETED")
    api.manager.db.upsert_download(_leftover(tmp_path, state, progress))
    assert _cards(_home(api), MOVIE) == []


@pytest.mark.parametrize("status", ["", "PAUSED", "REPEATING"])
@pytest.mark.parametrize("state", ["paused", "unknown", "error"])
def test_an_empty_idle_leftover_brings_no_card(tmp_path: Path, status: str, state: str) -> None:
    api = _api(tmp_path)
    _movie(api, status)
    api.manager.db.upsert_download(_leftover(tmp_path, state, 0.0005, download_speed=0))
    assert _cards(_home(api), MOVIE) == []


@pytest.mark.parametrize("state", ["error", "paused", "unknown"])
def test_an_idle_download_of_an_unwatched_movie_with_data_still_shows(tmp_path: Path, state: str) -> None:
    # Not on the list, 20 % downloaded and then failed or paused: the person sees where it stands.
    api = _api(tmp_path)
    _movie(api, "")
    api.manager.db.upsert_download(_leftover(tmp_path, state, 0.2))
    cards = _cards(_home(api), MOVIE)
    assert [key for key, _ in cards] == ["waiting"]
    assert cards[0][1]["download"]["state"] == state


@pytest.mark.parametrize("state", ["active", "waiting"])
def test_an_active_or_queued_rewatch_download_still_shows(tmp_path: Path, state: str) -> None:
    api = _api(tmp_path)
    _movie(api, "COMPLETED")
    api.manager.db.upsert_download(_leftover(tmp_path, state, 0.0))
    assert [key for key, _ in _cards(_home(api), MOVIE)] == ["waiting"]


@pytest.mark.parametrize("status", ["PLANNING", "CURRENT"])
def test_a_finished_movie_whose_file_was_deleted_is_no_download_any_more(tmp_path: Path, status: str) -> None:
    api = _api(tmp_path)
    _movie(api, status)
    api.manager.db.upsert_download(_movie_download(tmp_path, state="complete", progress=1.0))
    state = api.get_state()
    cards = _cards(json.loads(json.dumps(state["home"], default=str)), MOVIE)
    if status == "CURRENT":
        # Offered for download again, like a title without one.
        assert [key for key, _ in cards] == ["download_available"]
        assert "download" not in cards[0][1]
        assert cards[0][1]["presentation"]["status"] == "missing"
    else:
        assert cards == []
        planned = next(card for card in state["planned"] if card["media_id"] == MOVIE)
        assert "download" not in planned


def test_a_finished_movie_whose_file_is_there_waits_for_its_scan_then_is_ready(tmp_path: Path) -> None:
    api = _api(tmp_path)
    _movie(api, "PLANNING")
    video = tmp_path / "library" / "Suzume no Tojimari" / MOVIE_FILE
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    api.manager.db.upsert_download(_movie_download(tmp_path, state="complete", progress=1.0))
    cards = _cards(_home(api), MOVIE)
    assert [key for key, _ in cards] == ["waiting"]
    assert cards[0][1]["download"]["torrent_hash"] == MOVIE_HASH
    video.unlink()
    _ready_file(api, tmp_path, MOVIE, "Suzume no Tojimari", None, MOVIE_FILE)
    assert [key for key, _ in _cards(_home(api), MOVIE)] == ["completed_ready"]


def test_a_title_not_released_yet_gets_no_card_from_its_download(tmp_path: Path) -> None:
    api = _api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=MOVIE,
            title="Suzume no Tojimari",
            status="",
            episodes=1,
            format="MOVIE",
            media_status="NOT_YET_RELEASED",
        )
    )
    api.manager.db.upsert_download(_movie_download(tmp_path))
    assert _cards(_home(api), MOVIE) == []


def test_a_title_removed_from_the_list_keeps_its_download_in_downloads_only(tmp_path: Path) -> None:
    api = _api(tmp_path)
    _movie(api, "REMOVED")
    api.manager.db.upsert_download(_movie_download(tmp_path))
    state = api.get_state()
    assert _cards(json.loads(json.dumps(state["home"], default=str)), MOVIE) == []
    assert [row["torrent_hash"] for row in state["downloads"]] == [MOVIE_HASH]


@pytest.mark.parametrize(("local", "download"), [(None, 1), (1, None), (None, None), (1, 1)])
def test_a_movies_file_shadows_its_download_with_or_without_the_number(
    tmp_path: Path, local: int | None, download: int | None
) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"v")
    card = {"format": "MOVIE", "episodes": 1, "local": {"episode": local, "video_path": str(video)}}
    assert WebAppApi._download_shadowed_by_local(card, {"episode": download, "is_batch": False})
    ova = {"format": "OVA", "episodes": 1, "local": {"episode": local, "video_path": str(video)}}
    assert WebAppApi._download_shadowed_by_local(ova, {"episode": download, "is_batch": False})


def test_a_series_file_without_a_number_does_not_shadow_episode_one(tmp_path: Path) -> None:
    video = tmp_path / "show.mkv"
    video.write_bytes(b"v")
    card = {"format": "TV", "episodes": 12, "local": {"episode": None, "video_path": str(video)}}
    assert not WebAppApi._download_shadowed_by_local(card, {"episode": 1, "is_batch": False})


def test_a_ready_movie_hides_its_numbered_download(tmp_path: Path) -> None:
    # The file has no number, the finished download was recorded as episode 1.
    api = _api(tmp_path)
    _movie(api, "CURRENT")
    item = _movie_download(tmp_path, state="complete", progress=1.0)
    item.episode = item.media_episode = 1
    api.manager.db.upsert_download(item)
    _ready_file(api, tmp_path, MOVIE, "Suzume no Tojimari", None, MOVIE_FILE)
    cards = _cards(_home(api), MOVIE)
    assert [key for key, _ in cards] == ["completed_ready"]
    assert "download" not in cards[0][1]


# The aria2 shell cleanup: a movie's shell has no episode number.


def _shell_manager(anime: LibraryAnime):
    deleted: list[str] = []
    cleared: list[tuple] = []
    manager = object.__new__(AnimeManager)
    manager.db = SimpleNamespace(
        get_anime=lambda media_id: anime if media_id == anime.media_id else None,
        delete_torrent_records=deleted.append,
    )
    manager.download_intents = SimpleNamespace(clear=lambda *args: cleared.append(args))
    manager.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    client_deleted: list[tuple] = []
    client = SimpleNamespace(
        delete=lambda torrent_hash, *, delete_files=True: client_deleted.append((torrent_hash, delete_files))
    )
    return manager, client, deleted, cleared, client_deleted


def _shell(tmp_path: Path, media_id: int = MOVIE) -> DownloadItem:
    return DownloadItem(
        torrent_hash="shell",
        name=MOVIE_FILE,
        state="paused",
        progress=0.0,
        save_path=str(tmp_path),
        content_path=str(tmp_path / MOVIE_FILE),
        media_id=media_id,
        episode=None,
        is_batch=False,
        raw={"backend": "aria2", "total_size": 0, "downloaded": 0},
    )


@pytest.mark.parametrize("media_format", ["MOVIE", "OVA"])
def test_a_watched_movies_empty_aria2_shell_is_forgotten(tmp_path: Path, media_format: str) -> None:
    anime = LibraryAnime(
        media_id=MOVIE, title="Suzume", status="COMPLETED", progress=1, episodes=1, format=media_format
    )
    manager, client, deleted, cleared, client_deleted = _shell_manager(anime)
    assert manager._discard_watched_empty_aria2_shells(client, [_shell(tmp_path)]) == []
    assert client_deleted == [("shell", False)] and deleted == ["shell"]
    assert cleared == [(MOVIE, 1, False), (MOVIE, None, True)]


def test_an_unwatched_movies_shell_and_an_unnumbered_series_shell_are_kept(tmp_path: Path) -> None:
    movie = LibraryAnime(
        media_id=MOVIE, title="Suzume", status="PLANNING", progress=0, episodes=1, format="MOVIE"
    )
    manager, client, deleted, _, _ = _shell_manager(movie)
    item = _shell(tmp_path)
    assert manager._discard_watched_empty_aria2_shells(client, [item]) == [item]
    series = LibraryAnime(media_id=5, title="Show", status="COMPLETED", progress=12, episodes=12, format="TV")
    manager, client, deleted, _, _ = _shell_manager(series)
    item = _shell(tmp_path, media_id=5)
    assert manager._discard_watched_empty_aria2_shells(client, [item]) == [item]
    assert deleted == []
