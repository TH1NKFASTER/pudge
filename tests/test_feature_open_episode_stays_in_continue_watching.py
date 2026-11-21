"""An episode open in mpv stays under "Continue watching" until mpv closes.

User: "let an anime I have open but already counted stay in the continue-watching group (but
move to Recently watched the moment I quit mpv)". Counting marked the file watched, so it left
"Continue watching" for "Recently watched" while still playing; and when mpv closed after a
count the page did not ask for the new state at all.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


def _api(tmp_path: Path):
    db = Database(tmp_path / "library.sqlite3")
    anime = LibraryAnime(
        media_id=205909, title="Hotaru no Yomeiri", status="CURRENT", progress=2, episodes=12
    )
    db.upsert_anime(anime)
    paths = {}
    for number in (1, 2):
        video = tmp_path / f"Firefly.Wedding.S01E0{number}.mkv"
        video.write_bytes(b"video")
        paths[number] = video
        db.upsert_episode(
            LibraryEpisode(
                media_id=anime.media_id, title=anime.title, episode=number, video_path=video, state="ready"
            )
        )
    db.record_playback(paths[1], 1380.0, 1420.0, 1300.0)
    db.record_playback(paths[2], 1390.0, 1420.0, 1300.0)
    db.schedule_cleanup(paths[1], 24.0)
    db.schedule_cleanup(paths[2], 24.0)  # counted, mpv still open
    db.record_playback(paths[2], 1395.0, 1420.0, 5.0)  # mpv goes on saving its position
    api = object.__new__(WebAppApi)
    api.manager = SimpleNamespace(
        db=db, incomplete_download_paths=lambda: set(), _path_within=lambda _path, _roots: False
    )
    api.config = SimpleNamespace(
        playback=SimpleNamespace(rewind_seconds=10.0),
        agent=SimpleNamespace(delete_after_watched_hours=24.0),
    )
    api._episode_offset_cache = {}
    api._cover_uri = lambda _anime: ""
    api._anime_payload = lambda item, **_options: {"media_id": item.media_id, "title": item.title}
    api._play_lock = threading.Lock()
    api._active_playbacks_payload = lambda: []
    return api, {anime.media_id: anime}, paths


def test_a_counted_episode_stays_while_open_and_goes_to_recently_watched_after(tmp_path: Path) -> None:
    api, anime_by_id, paths = _api(tmp_path)
    episode_two = str(paths[2])
    api._active_playbacks_payload = lambda: [{"video_path": episode_two, "status": "running"}]
    [card] = api._continue_payloads(anime_by_id)
    assert card["video_path"] == episode_two and card["episode"] == 2
    # Not twice: "Recently watched" keeps the episode before it.
    assert [row["video_path"] for row in api._recently_watched_payloads(anime_by_id)] == [str(paths[1])]

    api._active_playbacks_payload = lambda: []  # mpv closed
    assert api._continue_payloads(anime_by_id) == []
    assert [row["video_path"] for row in api._recently_watched_payloads(anime_by_id)] == [episode_two]


def test_an_episode_opened_a_moment_ago_does_not_move(tmp_path: Path) -> None:
    api, anime_by_id, paths = _api(tmp_path)
    fresh = tmp_path / "Firefly.Wedding.S01E03.mkv"
    fresh.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(media_id=205909, title="Hotaru no Yomeiri", episode=3, video_path=fresh, state="ready")
    )
    api._active_playbacks_payload = lambda: [{"video_path": str(fresh), "status": "starting"}]
    assert api._continue_payloads(anime_by_id) == []  # stays where it was (its "ready" card)

    api.manager.db.record_playback(fresh, 1400.0, 1420.0, 1300.0)  # to the end, not counted yet
    api._active_playbacks_payload = lambda: [{"video_path": str(fresh), "status": "running"}]
    assert [card["episode"] for card in api._continue_payloads(anime_by_id)] == [3]


def test_without_a_player_registry_home_still_builds(tmp_path: Path) -> None:
    api, anime_by_id, _paths = _api(tmp_path)
    del api._play_lock
    assert api._open_video_paths() == set()


VIDEO = "/media/Hotaru no Yomeiri/Firefly.Wedding.S01E02.mkv"
CARD = {
    "media_id": 205909,
    "title": "Hotaru no Yomeiri",
    "episode": 2,
    "video_path": VIDEO,
    "progress_percent": 98,
}

MONITOR = r"""
setPage('current', true); await __tick(20);
const answers = [
  {status: 'running', watched: true, episode: 2, media_id: 205909},
  {status: 'running', watched: true, episode: 2, media_id: 205909},
  {status: 'idle', watched: true, episode: 2, media_id: 205909},
];
__api.play_status = async () => answers.length > 1 ? answers.shift() : answers[0];
const states = [STATE_OPEN, STATE_CLOSED];
let asked = 0;
__api.get_state = async () => { asked += 1; return {...ui.state, ...(states.length > 1 ? states.shift() : states[0])}; };
const where = () => [...document.querySelectorAll('#currentContent [data-home-section]')]
  .filter(section => section.querySelector(`[data-play-path="${CSS.escape(VIDEO)}"]`))
  .map(section => section.dataset.homeSection);
const out = {};
monitorPlay(VIDEO);
await __tick(1500); out.whileOpen = where();
await __tick(10000);
out.afterClose = where(); out.asked = asked;
return out;
"""


def test_the_page_asks_for_the_new_state_when_mpv_closes_after_a_count(page) -> None:
    open_home = {"home": {"continue_watching": [CARD], "recently_watched": []}}
    closed_home = {"home": {"continue_watching": [], "recently_watched": [CARD]}}
    script = (
        MONITOR.replace("STATE_OPEN", json.dumps(open_home))
        .replace("STATE_CLOSED", json.dumps(closed_home))
        .replace("VIDEO", json.dumps(VIDEO))
    )
    out = page.run(script).value
    assert out["whileOpen"] == ["section.continueWatching"], out
    assert out["afterClose"] == ["ui.recentlyWatched2"], out
    assert out["asked"] == 2  # on the count and on close
