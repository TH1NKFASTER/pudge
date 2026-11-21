"""A movie (or batch) download under way shows on Home, whatever the title's list status.

User (2026-10-23): "не показывается скачивание suzume на странице аниме – только в загрузках".
Suzume no Tojimari (AniList 142770, a MOVIE, on the list as PLANNING) was downloading through
aria2 as one file, "[Lazy] Suzume (2022) v2 (BD 4k SDR) (2160p) [6297C976].mkv": aria2 keeps
such a download as episode None and not a batch (one video file). Home matched a title's
download to its card only by the episode number or as a batch, so the movie download matched no
card: a Planning (or Completed, Rewatching, not on the list) title had no card at all, a Current
one offered "Download" again. The Download Center listed it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_web_app import make_api

from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode

MOVIE = 142770
SERIES = 42
MOVIE_FILE = "[Lazy] Suzume (2022) v2 (BD 4k SDR) (2160p) [6297C976].mkv"
MOVIE_HASH = "62369ee076ff0f81188237f8706425c4dba9c704"
BATCH_HASH = "b" * 40
SECTIONS = (
    "continue_watching",
    "needs_action",
    "new_ready",
    "completed_ready",
    "waiting",
    "download_available",
    "caught_up",
    "recently_watched",
    "dropped",
)
# "" is a title not on the list (downloaded from the global search).
STATUSES = ["", "PLANNING", "CURRENT", "COMPLETED", "REPEATING"]


def _api(tmp_path: Path):
    api = make_api(tmp_path)
    api.config.nyaa.torrents_enabled = True
    return api


def _movie(api, status: str) -> None:
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=MOVIE,
            title="Suzume no Tojimari",
            status=status,
            progress=1 if status == "COMPLETED" else 0,
            episodes=1,
            format="MOVIE",
            media_status="FINISHED",
        )
    )


def _series(api, status: str) -> None:
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=SERIES,
            title="Show",
            status=status,
            progress=12 if status == "COMPLETED" else 0,
            episodes=12,
            format="TV",
            media_status="FINISHED",
        )
    )


def _movie_download(tmp_path: Path, *, state: str = "active", progress: float = 0.012) -> DownloadItem:
    folder = tmp_path / "library" / "Suzume no Tojimari"
    return DownloadItem(
        MOVIE_HASH,
        MOVIE_FILE,
        state,
        progress,
        str(folder),
        str(folder / MOVIE_FILE),
        media_id=MOVIE,
        episode=None,
        is_batch=False,
        raw={"backend": "aria2", "single_video_file": True, "_media_id_source": "aria2_metadata"},
    )


def _batch_download(tmp_path: Path, *, state: str = "active", progress: float = 0.3) -> DownloadItem:
    folder = tmp_path / "library" / "Show" / "[Group] Show (BD 1080p)"
    return DownloadItem(
        BATCH_HASH,
        "[Group] Show (BD 1080p)",
        state,
        progress,
        str(folder.parent),
        str(folder),
        media_id=SERIES,
        episode=None,
        is_batch=True,
        raw={"backend": "aria2", "_media_id_source": "aria2_metadata"},
    )


def _ready_file(api, tmp_path: Path, media_id: int, title: str, episode: int | None, name: str) -> Path:
    video = tmp_path / "library" / title / name
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    subtitle = video.with_suffix(".ja.srt")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=media_id,
            title=title,
            episode=episode,
            video_path=video,
            subtitle_path=subtitle,
            state="ready",
        )
    )
    return video


def _home(api) -> dict:
    return json.loads(json.dumps(api.get_state()["home"], default=str))


def _cards(home: dict, media_id: int) -> list[tuple[str, dict]]:
    found = []
    for key in SECTIONS:
        for card in home.get(key) or []:
            for item in card.get("items") if card.get("kind") == "watch_sequence" else [card]:
                if item.get("media_id") == media_id:
                    found.append((key, item))
    return found


@pytest.mark.parametrize("status", [*STATUSES, "PAUSED"])
def test_a_movie_downloading_as_one_file_shows_on_home_with_its_progress(tmp_path: Path, status: str) -> None:
    api = _api(tmp_path)
    _movie(api, status)
    api.manager.db.upsert_download(_movie_download(tmp_path))
    cards = _cards(_home(api), MOVIE)
    assert [key for key, _ in cards] == ["waiting"], cards
    download = cards[0][1]["download"]
    assert download["torrent_hash"] == MOVIE_HASH
    assert download["progress"] == pytest.approx(0.012)
    assert download["effective_state"] == "active"


def test_the_planning_page_card_of_the_movie_has_its_download(tmp_path: Path) -> None:
    api = _api(tmp_path)
    _movie(api, "PLANNING")
    api.manager.db.upsert_download(_movie_download(tmp_path))
    planned = next(card for card in api.get_state()["planned"] if card["media_id"] == MOVIE)
    assert planned["download"]["torrent_hash"] == MOVIE_HASH


def test_a_current_movie_downloading_is_not_offered_for_download_again(tmp_path: Path) -> None:
    api = _api(tmp_path)
    _movie(api, "CURRENT")
    api.manager.db.upsert_download(_movie_download(tmp_path))
    home = _home(api)
    assert home["download_available"] == []
    card = home["waiting"][0]
    assert card["presentation"]["status"] == "downloading"
    assert card["presentation"]["progress_percent"] == 1


@pytest.mark.parametrize("status", ["CURRENT", "PLANNING", ""])
def test_a_one_episode_ova_without_an_episode_number_shows_its_download(tmp_path: Path, status: str) -> None:
    api = _api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=7, title="Some OVA", status=status, episodes=1, format="OVA", media_status="FINISHED"
        )
    )
    item = _movie_download(tmp_path)
    item.media_id = 7
    api.manager.db.upsert_download(item)
    cards = _cards(_home(api), 7)
    assert [key for key, _ in cards] == ["waiting"]
    assert cards[0][1]["download"]["torrent_hash"] == MOVIE_HASH


@pytest.mark.parametrize("status", STATUSES)
def test_a_series_batch_downloading_shows_on_home_with_its_progress(tmp_path: Path, status: str) -> None:
    api = _api(tmp_path)
    _series(api, status)
    api.manager.db.upsert_download(_batch_download(tmp_path))
    cards = _cards(_home(api), SERIES)
    assert [key for key, _ in cards] == ["waiting"], cards
    assert cards[0][1]["download"]["torrent_hash"] == BATCH_HASH
    assert cards[0][1]["download"]["progress"] == pytest.approx(0.3)


def test_an_unnumbered_file_of_a_series_not_on_the_list_still_shows(tmp_path: Path) -> None:
    # No card matches it by episode, but the title has no other card: Home shows the download.
    api = _api(tmp_path)
    _series(api, "")
    item = _movie_download(tmp_path)
    item.media_id = SERIES
    api.manager.db.upsert_download(item)
    cards = _cards(_home(api), SERIES)
    assert [key for key, _ in cards] == ["waiting"]
    assert cards[0][1]["download"]["torrent_hash"] == MOVIE_HASH


@pytest.mark.parametrize("status", STATUSES)
def test_a_finished_movie_download_turns_into_a_ready_card(tmp_path: Path, status: str) -> None:
    api = _api(tmp_path)
    _movie(api, status)
    api.manager.db.upsert_download(_movie_download(tmp_path))
    assert [key for key, _ in _cards(_home(api), MOVIE)] == ["waiting"]
    api.manager.db.upsert_download(_movie_download(tmp_path, state="complete", progress=1.0))
    video = _ready_file(api, tmp_path, MOVIE, "Suzume no Tojimari", None, MOVIE_FILE)
    cards = _cards(_home(api), MOVIE)
    assert [key for key, _ in cards] == ["completed_ready"], cards
    assert cards[0][1]["local"]["video_path"] == str(video)
    assert "download" not in cards[0][1]


@pytest.mark.parametrize("status", ["", "COMPLETED", "REPEATING"])
def test_a_finished_movie_download_whose_file_is_gone_brings_no_card(tmp_path: Path, status: str) -> None:
    # Watched and deleted, the torrent still listed (seeding): nothing left to show.
    api = _api(tmp_path)
    _movie(api, status)
    api.manager.db.upsert_download(_movie_download(tmp_path, state="complete", progress=1.0))
    assert _cards(_home(api), MOVIE) == []


def test_a_dropped_titles_leftover_download_stays_in_downloads_only(tmp_path: Path) -> None:
    # Dropped while Torrents were off: its download was not cancelled, it is paused.
    api = _api(tmp_path)
    _movie(api, "DROPPED")
    api.manager.db.upsert_download(_movie_download(tmp_path, state="paused"))
    state = api.get_state()
    assert _cards(json.loads(json.dumps(state["home"], default=str)), MOVIE) == []
    assert [row["torrent_hash"] for row in state["downloads"]] == [MOVIE_HASH]


@pytest.mark.parametrize("status", ["", "PLANNING", "CURRENT"])
def test_a_finished_batch_turns_into_a_ready_card(tmp_path: Path, status: str) -> None:
    api = _api(tmp_path)
    _series(api, status)
    api.manager.db.upsert_download(_batch_download(tmp_path, state="complete", progress=1.0))
    for episode in range(1, 13):
        _ready_file(api, tmp_path, SERIES, "Show", episode, f"[Group] Show - {episode:02d}.mkv")
    cards = _cards(_home(api), SERIES)
    assert [key for key, _ in cards] == ["completed_ready"], cards
    assert cards[0][1]["local"]["episode"] == 1


# The page: the Anime page (Home) renders the snapshot's card with the live progress.

RENDER = r"""
setPage('current', true); renderCurrent();
const section = document.querySelector('[data-home-section="section.waitingPreparation"]');
const card = section ? section.querySelector('.waiting-card[data-media-id="142770"]') : null;
const ready = document.querySelector('[data-home-section="section.completedReady"] .downloaded-card[data-media-id="142770"]');
return {
  waiting: card ? card.querySelector('.airing-state').innerText : null,
  live: card ? (card.querySelector('[data-live-download]')?.getAttribute('data-live-download') || '') : null,
  action: card ? card.getAttribute('data-action') : null,
  ready: ready ? ready.getAttribute('data-play-path') : null,
  available: document.querySelectorAll('.download-available-card[data-media-id="142770"]').length,
};
"""


def _page_state(api) -> dict:
    state = json.loads(json.dumps(api.get_state(), default=str))
    return {"home": state["home"], "settings": {"torrents_enabled": True}}


@pytest.mark.parametrize("status", STATUSES)
def test_the_anime_page_shows_the_movie_download_and_then_the_ready_movie(
    page, tmp_path: Path, status: str
) -> None:
    api = _api(tmp_path)
    _movie(api, status)
    api.manager.db.upsert_download(_movie_download(tmp_path))
    for lang in ("ru", "en"):
        out = page.run(RENDER, lang=lang, state=_page_state(api)).value
        assert out["waiting"] is not None and out["waiting"].startswith("1%"), out
        assert out["live"] == MOVIE_HASH
        assert out["action"] == "open-downloads"
        assert out["available"] == 0 and out["ready"] is None
    api.manager.db.upsert_download(_movie_download(tmp_path, state="complete", progress=1.0))
    video = _ready_file(api, tmp_path, MOVIE, "Suzume no Tojimari", None, MOVIE_FILE)
    out = page.run(RENDER, lang="ru", state=_page_state(api)).value
    assert out["waiting"] is None and out["available"] == 0
    assert out["ready"] == str(video)
