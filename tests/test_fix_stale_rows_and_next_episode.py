"""Sora yori mo Tooi Basho after the one-time aria2 leftover cleanup (runtime log 2026-10-15 23:19).

1. The cleanup deleted the 26 empty files of the two removed race packs ([MegaMTBB], [MTBB])
   but not their library rows: each row waited out the missing-file delay ("Delete hours
   after watching", 2 hours here) like a file deleted in Finder, so the episode list showed
   every episode two or three times ("Episode 1 · waiting_subtitles" twice...). Now the
   cleanup, and the removal of an empty race or stalled candidate, drop the rows and
   subtitle jobs of the files they delete at once, and the next scan walks the library.
2. A gone file's row that the scan keeps for its wait was not looked at again once the wait
   ended: the file list no longer changed, so "unchanged library" skipped the scan for up to
   6 hours. The scan now runs when such a wait ends. Installs where the old cleanup already
   ran forget, once, the rows of files that went missing since then and hold no viewing
   history (watched, played, deletion planned); a drive that is away, a torrent the client
   still has and older missing files keep the usual wait.
3. Home ("Completed & ready"), the Planning card and its Watch button and "Play next" offered
   episode 2 of a Planning title (progress 0) whose episode 1 was not downloaded: any ready
   file counted. Now the next unwatched episode decides everywhere: when it is not ready the
   card shows its real state (its download, waiting for subtitles, or a download action) and
   "Play next" starts at it and stops at the first episode that is not ready. A title watched
   to the end (a rewatch), movies and files without an episode number are unchanged; an
   episode opened from the episode list still plays.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import pytest

from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from test_fix_planning_downloads_oct15 import _aria2_manager
from test_fix_race_leftovers_and_empty_files import MEDIA_ID, MTBB, TITLE, _leftovers, _library_manager
from test_web_app import make_api

SUBTITLE = "1\n00:00:01,000 --> 00:00:03,000\nこんにちは。\n"
MEGA = "[MegaMTBB] Sora yori mo Tooi Basho S1 (BD 1080p)"


def _row(manager: AnimeManager, video: Path, episode: int, state: str, **fields) -> Path:
    manager.db.upsert_episode(LibraryEpisode(MEDIA_ID, TITLE, episode, video, state=state, **fields))
    manager.db.queue_subtitle_job(video, MEDIA_ID, episode)
    return video


def _jobs(manager: AnimeManager) -> set[str]:
    return {str(row["video_path"]) for row in manager.db.subtitle_jobs()}


# --- 1: Pudge's own deletions forget their rows -------------------------------------------


def test_leftover_cleanup_forgets_the_rows_and_jobs_of_the_files_it_deletes(tmp_path, monkeypatch) -> None:
    manager, _backend, _clock = _aria2_manager(tmp_path, monkeypatch, {})
    series = manager.config.library.root_dir / TITLE
    files = _leftovers(series)
    old = time.time() - 3600
    for control in series.glob("*.aria2"):
        os.utime(control, (old, old))
    gone_one = _row(manager, files["empty"].resolve(), 1, "waiting_subtitles")
    gone_nc = _row(manager, files["nc"].resolve(), 0, "local")
    kept = _row(manager, files["done"].resolve(), 3, "ready")
    manager.db.set_state(manager._LIBRARY_SCAN_CACHE_KEY, json.dumps({"generation": 3, "fingerprint": "x"}))

    assert manager.cleanup_aria2_leftovers() >= 2

    assert not gone_one.exists() and not gone_nc.exists()
    # Before: both rows stayed ("waiting for subtitles" next to the real copy) for hours.
    assert manager.db.episode_by_path(gone_one) is None
    assert manager.db.episode_by_path(gone_nc) is None
    assert _jobs(manager) == {str(kept)}
    assert manager.db.episode_by_path(kept) is not None
    assert not manager.db.get_state(manager._LIBRARY_SCAN_CACHE_KEY, "")


def test_removed_empty_candidate_forgets_its_rows(tmp_path) -> None:
    manager = _library_manager(tmp_path)
    root = manager.config.library.root_dir / TITLE
    pack = root / MEGA
    pack.mkdir(parents=True)
    empty = (pack / "[MegaMTBB] Sora yori mo Tooi Basho - 01 (BD 1080p) [2CE7AAFD].mkv").resolve()
    empty.touch()
    (root / f"{MEGA}.aria2").write_bytes(b"control")
    _row(manager, empty, 1, "waiting_subtitles")

    assert manager._remove_empty_candidate_files("c" * 40, [empty], root) >= 1

    assert not empty.exists()
    assert manager.db.episode_by_path(empty) is None and _jobs(manager) == set()


# --- 2: the scan looks at a gone file again when its wait ends; one-time repair -----------


@pytest.fixture
def clock(monkeypatch):
    real = time.time
    state = {"offset": 0.0}
    monkeypatch.setattr(time, "time", lambda: real() + state["offset"])
    return state


def test_unchanged_library_is_scanned_again_when_a_gone_file_may_be_forgotten(tmp_path, clock) -> None:
    manager = _library_manager(tmp_path)
    manager.config.agent.delete_after_watched_hours = 2.0
    video = (manager.config.library.root_dir / TITLE / f"{TITLE} - 01.mkv").resolve()
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")
    _row(manager, video, 1, "waiting_subtitles")
    manager.scan_library(reuse_unchanged=True)
    video.unlink()

    manager.scan_library(reuse_unchanged=True)  # the file list changed: a full scan starts the wait
    assert manager.db.episode_by_path(video) is not None
    clock["offset"] = 1800.0
    manager.scan_library(reuse_unchanged=True)  # unchanged and still waiting
    assert manager.db.episode_by_path(video) is not None

    clock["offset"] = 3 * 3600.0
    manager.scan_library(reuse_unchanged=True)
    # Before: "unchanged" for 6 hours, so the row stayed 4 hours past its wait.
    assert manager.db.episode_by_path(video) is None
    assert _jobs(manager) == set()


def test_rows_the_old_leftover_cleanup_left_are_forgotten_once(tmp_path, caplog) -> None:
    manager = _library_manager(tmp_path)  # aria2 is off by now: the repair still runs
    db = manager.db
    series = (manager.config.library.root_dir / TITLE).resolve()
    series.mkdir(parents=True)
    real = series / "[HorribleSubs] Sora yori mo Tooi Basho - 02 [1080p].mkv"
    real.write_bytes(b"video")
    cleaned_at = time.time() - 600
    leftovers = [
        _row(manager, series / MEGA / f"[MegaMTBB] Sora yori mo Tooi Basho - 0{n} (BD 1080p).mkv", n, state)
        for n, state in ((1, "waiting_subtitles"), (2, "waiting_subtitles"), (3, "couldnt_sync"))
    ]
    leftovers.append(_row(manager, series / MTBB / "[MTBB] Sora yori mo Tooi Basho - 01.mkv", 1, "local"))
    watched = _row(manager, series / MTBB / "[MTBB] Sora yori mo Tooi Basho - 04.mkv", 4, "ready")
    db.schedule_cleanup(watched, 2.0)  # watched: its row keeps the usual wait
    older = _row(manager, series / "Sora yori mo Tooi Basho - 05 (old).mkv", 5, "waiting_subtitles")
    still_listed = _row(
        manager,
        series / "[SubsPlease] Sora yori mo Tooi Basho - 06.mkv",
        6,
        "waiting_subtitles",
        torrent_hash="e" * 40,
    )
    db.upsert_download(
        DownloadItem("e" * 40, "six", "moving", 1.0, str(series), str(still_listed), MEDIA_ID, 6)
    )
    kept_real = _row(manager, real, 2, "ready")
    # The state the old code left: its done marker, and the waits it started 3 s later.
    db.set_state(manager._ARIA2_LEFTOVER_CLEANUP_KEY, str(int(cleaned_at)))
    since = {str(path): cleaned_at + 3 for path in [*leftovers, watched]}
    since[str(older)] = cleaned_at - 86400
    db.set_state(manager._MISSING_VIDEO_STATE, json.dumps(since))
    logging.getLogger("pudge").propagate = True  # AnimeManager() detached the app logger
    caplog.set_level(logging.INFO)

    manager.cleanup_aria2_leftovers()

    for path in leftovers:
        assert db.episode_by_path(path) is None
    assert _jobs(manager) == {str(watched), str(older), str(still_listed), str(kept_real)}
    for path in (watched, older, still_listed, kept_real):
        assert db.episode_by_path(path) is not None
    assert (
        sum("REPAIR step=library.aria2_leftover_row" in record.getMessage() for record in caplog.records) == 4
    )
    # Once only.
    again = _row(
        manager, series / MEGA / "[MegaMTBB] Sora yori mo Tooi Basho - 07 (BD 1080p).mkv", 7, "local"
    )
    manager.cleanup_aria2_leftovers()
    assert db.episode_by_path(again) is not None


# --- 3: the next unwatched episode, in order ----------------------------------------------


def _sora_api(tmp_path: Path, *, status: str = "PLANNING", progress: int = 0, ready=(2,), gone=(1,)):
    api = make_api(tmp_path)
    db = api.manager.db
    anime = LibraryAnime(
        media_id=MEDIA_ID,
        title=TITLE,
        status=status,
        media_status="FINISHED",
        progress=progress,
        episodes=13,
        format="TV",
    )
    db.upsert_anime(anime)
    series = (api.config.library.root_dir / TITLE).resolve()
    series.mkdir(parents=True, exist_ok=True)
    for number in ready:
        video = series / f"[HorribleSubs] {TITLE} - {number:02d} [1080p].mkv"
        video.write_bytes(b"video")
        subtitle = video.with_suffix(".ja.srt")
        subtitle.write_text(SUBTITLE, encoding="utf-8")
        db.upsert_episode(
            LibraryEpisode(MEDIA_ID, TITLE, number, video, subtitle_path=subtitle, state="ready")
        )
    for number in gone:  # the rows of a deleted duplicate pack
        video = series / MEGA / f"[MegaMTBB] {TITLE} - {number:02d} (BD 1080p).mkv"
        db.upsert_episode(LibraryEpisode(MEDIA_ID, TITLE, number, video, state="waiting_subtitles"))
    return api, anime


def _home_card(api) -> tuple[str, dict]:
    for section, rows in api.get_state()["home"].items():
        for card in rows or []:
            for item in card.get("items") or [card]:
                if item.get("media_id") == MEDIA_ID:
                    return section, item
    raise AssertionError("no Home card")


def _queue_episodes(api) -> list[int]:
    return [int(item["episode"]) for item in api._ready_queue_items([MEDIA_ID])]


def test_planning_title_missing_episode_one_never_offers_episode_two(tmp_path) -> None:
    api, anime = _sora_api(tmp_path)

    # The anime card: before, the gone copy of episode 1 "waiting for subtitles".
    assert api._anime_payload(anime)["local"] is None
    planned = next(row for row in api.get_state()["planned"] if row["media_id"] == MEDIA_ID)
    # Before: "Episode 2: ready" with a Watch button for episode 2.
    assert planned["local"] is None
    assert planned["ready_episodes"] == [2] and planned["next_episode"] == 1
    # Home: before, "Completed & ready" playing episode 2.
    section, card = _home_card(api)
    assert section == "download_available"
    assert card["next_episode"] == 1 and card.get("local") is None
    # Play next: before, a queue starting at episode 2.
    assert _queue_episodes(api) == []


def test_next_episode_downloading_shows_its_download(tmp_path) -> None:
    api, _anime = _sora_api(tmp_path, gone=())
    series = api.config.library.root_dir / TITLE
    api.manager.db.upsert_download(
        DownloadItem(
            "f" * 40, f"{TITLE} - 01", "downloading", 0.42, str(series), str(series / "01.mkv"), MEDIA_ID, 1
        )
    )
    section, card = _home_card(api)
    assert section == "waiting"  # a download under way is preparation
    assert card["download"]["progress"] == pytest.approx(0.42)
    assert card.get("local") is None


def test_next_episode_ready_is_the_one_offered(tmp_path) -> None:
    api, anime = _sora_api(tmp_path, ready=(1, 2, 4), gone=(1, 3))

    assert api._anime_payload(anime)["local"]["episode"] == 1
    planned = next(row for row in api.get_state()["planned"] if row["media_id"] == MEDIA_ID)
    assert planned["local"]["episode"] == 1
    section, card = _home_card(api)
    assert section == "completed_ready" and card["local"]["episode"] == 1
    # In order, up to the first episode that is not ready (3).
    assert _queue_episodes(api) == [1, 2]


def test_watching_title_queue_starts_at_the_next_episode(tmp_path) -> None:
    api, anime = _sora_api(tmp_path, status="CURRENT", progress=1, ready=(3, 4), gone=(2,))
    assert api._anime_payload(anime)["local"] is None
    assert _queue_episodes(api) == []
    section, card = _home_card(api)
    assert section == "download_available" and card["next_episode"] == 2

    api, anime = _sora_api(tmp_path / "b", status="CURRENT", progress=2, ready=(3, 4, 6), gone=())
    assert _queue_episodes(api) == [3, 4]
    assert _home_card(api)[1]["local"]["episode"] == 3


def test_a_title_watched_to_the_end_still_opens_its_first_file(tmp_path) -> None:
    api, _anime = _sora_api(tmp_path, status="COMPLETED", progress=13, ready=(2, 3), gone=(1,))
    section, card = _home_card(api)
    assert section == "completed_ready" and card["local"]["episode"] == 2


def test_planning_card_names_the_next_episode_not_a_later_ready_one(page) -> None:
    for lang, expected in (
        ("en", "Next is episode 1 · already ready: 2,5"),
        ("ru", "Следующая — серия 1 · уже готовы: 2,5"),
    ):
        result = page.run(
            """
            const card = animeCard({media_id: 7, title: 'Sora', progress: 0, episodes: 13, next_episode: 1,
                                    local: null, ready_episodes: [2, 5], media_status: 'FINISHED'}, true);
            const box = document.createElement('div');
            box.innerHTML = card;
            return box.querySelector('.anime-status').innerText;
            """,
            lang=lang,
        )
        # Before: nothing (the card said "Episode 2: ready" while it took episode 2 as local).
        assert result.value == expected
