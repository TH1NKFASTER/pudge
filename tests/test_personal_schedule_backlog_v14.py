from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


def _case(tmp_path, monkeypatch, *, rewatch=False, japanese=True, local_states=None):
    now = datetime.fromisoformat("2026-09-30T17:36:00+05:00").timestamp()
    monkeypatch.setattr("pudge.web_app.time.time", lambda: now)
    db = Database(tmp_path / "library.sqlite3")
    anime = LibraryAnime(media_id=151379, title="Akiba Meido Sensou", status="CURRENT",
                         progress=12 if rewatch else 7, episodes=12, format="TV", media_status="FINISHED")
    db.upsert_anime(anime)
    for number in (8, 9, 10):
        video = tmp_path / f"episode-{number}.mkv"
        subtitle = tmp_path / f"episode-{number}.srt"
        video.write_bytes(b"video")
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n")
        db.upsert_episode(LibraryEpisode(media_id=anime.media_id, title=anime.title, episode=number,
                                        video_path=video, subtitle_path=subtitle,
                                        state=(local_states or {}).get(number, "watched" if rewatch else "ready")))
    db.save_personal_release_schedule(anime.media_id, episodes=[8, 9, 10, 11, 12],
                                     start_local_datetime="2026-09-22T20:00", timezone_iana="Asia/Almaty",
                                     rewatch=rewatch, now=0)
    api = object.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=db, japanese_subtitles_required=lambda _: japanese)
    api.config = SimpleNamespace(matching=SimpleNamespace(ocr_counts_as_ready=False))
    api._episode_offset_cache = {}
    return api, db, anime


@pytest.mark.parametrize("rewatch", [False, True])
def test_missed_releases_accumulate_and_home_card_keeps_episode_eight_target(tmp_path, monkeypatch, rewatch):
    api, db, anime = _case(tmp_path, monkeypatch, rewatch=rewatch)
    snapshot = api._personal_schedule_snapshot(anime)
    assert snapshot["status"] == "new_ready"
    assert snapshot["ready_episodes"] == [8, 9]
    assert snapshot["ready_count"] == 2
    assert snapshot["next_episode"] == 8
    api._continue_payloads = lambda _: []
    api._downloaded_payloads = lambda _: []
    api._pending_local_payloads = lambda _: []
    api._anime_payload = lambda item: api._apply_personal_schedule_to_payload(item, {
        "media_id": item.media_id, "title": item.title, "format": item.format,
    })
    card = api._home_sections([anime], {anime.media_id: anime})["new_ready"][0]
    assert card["ready_episodes"] == [8, 9]
    assert card["ready_count"] == 2
    assert card["local"]["episode"] == 8
    assert Path(card["local"]["video_path"]).name == "episode-8.mkv"
    db.mark_personal_release_watched(anime.media_id, 8)
    after = api._personal_schedule_snapshot(anime)
    assert after["next_episode"] == 9 and after["ready_episodes"] == [9]


@pytest.mark.parametrize("problem", ["missing_video", "missing_subtitle", "empty_subtitle", "ocr", "waiting", "watched"])
def test_backlog_does_not_count_unprepared_or_missing_episode(tmp_path, monkeypatch, problem):
    api, db, anime = _case(tmp_path, monkeypatch, local_states={9: "waiting_subtitles"} if problem == "waiting" else None)
    local = db.ready_episode(anime.media_id, 9)
    if problem == "missing_video":
        local.video_path.unlink()
    elif problem == "missing_subtitle":
        local.subtitle_path.unlink()
    elif problem == "empty_subtitle":
        local.subtitle_path.write_text("")
    else:
        if problem == "ocr":
            local.subtitle_origin = "ocr"
        else:
            local.state = "watched" if problem == "watched" else "waiting_subtitles"
        db.upsert_episode(local)
    snapshot = api._personal_schedule_snapshot(anime)
    assert snapshot["ready_episodes"] == [8] and snapshot["ready_count"] == 1
    assert snapshot["next_episode"] == 8


@pytest.mark.parametrize("problem", ["missing_video", "waiting"])
def test_later_ready_release_does_not_bypass_unprepared_nearest_episode(tmp_path, monkeypatch, problem):
    api, db, anime = _case(tmp_path, monkeypatch, local_states={8: "waiting_subtitles"} if problem == "waiting" else None)
    local = db.ready_episode(anime.media_id, 8)
    if problem == "missing_video":
        local.video_path.unlink()
    else:
        local.state = "waiting_subtitles"
        db.upsert_episode(local)
    snapshot = api._personal_schedule_snapshot(anime)
    assert snapshot["next_episode"] == 8
    assert snapshot["status"] == ("download_available" if problem == "missing_video" else "waiting")
    assert snapshot["ready_episodes"] == []


def test_optional_subtitles_and_ocr_readiness_use_same_rules_for_backlog(tmp_path, monkeypatch):
    api, db, anime = _case(tmp_path, monkeypatch, japanese=False, local_states={9: "waiting_subtitles"})
    local = db.ready_episode(anime.media_id, 9)
    local.state = "waiting_subtitles"
    local.subtitle_path.unlink()
    db.upsert_episode(local)
    assert api._personal_schedule_snapshot(anime)["ready_episodes"] == [8, 9]
