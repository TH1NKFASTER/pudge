from __future__ import annotations

import time
from pathlib import Path

import pytest

from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from test_web_app import make_api


@pytest.fixture
def isolated_library(tmp_path):
    api = make_api(tmp_path)
    corpus = tmp_path / "private-corpus"
    corpus.mkdir()
    marker = corpus / api.manager._LIBRARY_SCAN_IGNORE_MARKER
    marker.write_text("Synthetic isolated test corpus\n")
    return api, corpus, marker


def episode(api, folder: Path, *, media_id=1, state="waiting_subtitles", number=1):
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"Synthetic {media_id} - {number}.mkv"
    video.write_bytes(b"synthetic video")
    item = LibraryEpisode(media_id, "Synthetic anime", number, video, state=state)
    api.manager.db.upsert_episode(item)
    return item


def download(api, folder: Path, *, media_id=1, torrent_hash="isolated", added_on=1):
    item = DownloadItem(
        torrent_hash=torrent_hash, name="Synthetic batch", state="active", progress=0.3,
        save_path=str(folder), content_path=str(folder / "Synthetic batch"),
        media_id=media_id, is_batch=True, added_on=added_on,
    )
    api.manager.db.upsert_download(item)
    return item


def cards(home):
    for section in home.values():
        for card in section:
            yield from card.get("items", [card]) if card.get("kind") == "watch_sequence" else [card]


@pytest.mark.parametrize("state", ["local", "waiting_subtitles", "waiting_text_subtitles", "couldnt_sync", "ready"])
@pytest.mark.parametrize("status", ["", "PLANNING"])
def test_isolated_stale_rows_never_surface_on_home(isolated_library, state, status):
    api, corpus, _marker = isolated_library
    api.manager.db.upsert_anime(LibraryAnime(1, "Synthetic corpus anime", status=status, episodes=12, media_status="FINISHED"))
    item = episode(api, corpus / "downloads", state=state)
    api.manager.db.queue_subtitle_job(item.video_path, 1, 1)
    if state == "waiting_text_subtitles":
        api.manager.db.mark_subtitle_job_needs_action(item.video_path, "Synthetic bitmap subs", "enable_subtitle_ocr")
    before = (api.manager.db.episodes(), api.manager.db.downloads(), [dict(row) for row in api.manager.db.subtitle_jobs()])

    for _ in range(3):
        result = api.get_state()
        assert list(cards(result["home"])) == []
        assert result["downloaded"] == []
        # Explicit low-level library and worker views remain available.
        assert result["library"][0]["episodes"][0]["video_path"] == str(item.video_path)
        assert result["subtitle_jobs"][0]["video_path"] == str(item.video_path)

    assert before == (api.manager.db.episodes(), api.manager.db.downloads(), [dict(row) for row in api.manager.db.subtitle_jobs()])
    assert item.video_path.read_bytes() == b"synthetic video"


@pytest.mark.parametrize("state,section", [("waiting_subtitles", "waiting"), ("ready", "completed_ready")])
def test_ordinary_copy_of_same_episode_wins_over_isolated_row(isolated_library, state, section):
    api, corpus, _marker = isolated_library
    anime = LibraryAnime(1, "Synthetic shared anime", status="CURRENT", episodes=12, media_status="FINISHED")
    api.manager.db.upsert_anime(anime)
    hidden = episode(api, corpus / "downloads", state=state)
    visible = episode(api, api.config.library.root_dir, state=state)
    assert api.manager.db.ready_episode(1, 1).video_path == hidden.video_path

    result = api.get_state()

    assert result["current"][0]["local"]["video_path"] == str(visible.video_path)
    assert result["home"][section][0]["local"]["video_path"] == str(visible.video_path)
    assert all((card.get("local") or {}).get("video_path") != str(hidden.video_path) for card in cards(result["home"]))
    assert [row["video_path"] for row in api._ready_queue_items([1])] == ([str(visible.video_path)] if state == "ready" else [])


def test_isolated_download_cannot_reinsert_planning_card(isolated_library):
    api, corpus, _marker = isolated_library
    for media_id in (1, 2):
        api.manager.db.upsert_anime(LibraryAnime(media_id, f"Synthetic {media_id}", status="PLANNING", episodes=12, media_status="FINISHED"))
    hidden = download(api, corpus / "downloads")
    ordinary = download(api, api.config.library.root_dir, media_id=2, torrent_hash="ordinary")

    result = api.get_state()

    assert [card["media_id"] for card in result["home"]["waiting"]] == [2]
    assert result["home"]["waiting"][0]["download"]["hash"] == ordinary.torrent_hash
    assert {row["hash"] for row in result["downloads"]} == {hidden.torrent_hash, ordinary.torrent_hash}
    assert result["planned"][0]["planning_download_hidden"] is False


def test_newer_isolated_download_does_not_shadow_ordinary_download(isolated_library):
    api, corpus, _marker = isolated_library
    api.manager.db.upsert_anime(LibraryAnime(1, "Synthetic shared anime", status="PLANNING", episodes=12, media_status="FINISHED"))
    download(api, api.config.library.root_dir, torrent_hash="ordinary", added_on=1)
    download(api, corpus / "downloads", added_on=2)

    result = api.get_state()

    assert result["home"]["waiting"][0]["download"]["hash"] == "ordinary"
    assert result["planned"][0]["download"]["hash"] == "ordinary"


def test_exclusion_uses_marker_and_is_reversible(isolated_library):
    api, corpus, marker = isolated_library
    api.manager.db.upsert_anime(LibraryAnime(1, "Synthetic anime", episodes=12, media_status="FINISHED"))
    hidden = episode(api, corpus / "downloads")
    ordinary = episode(api, corpus.parent / "benchmark-in-name-only", media_id=2)

    assert [card["local"]["video_path"] for card in api.get_state()["home"]["waiting"]] == [str(ordinary.video_path)]
    marker.unlink()
    assert {card["local"]["video_path"] for card in api.get_state()["home"]["waiting"]} == {str(hidden.video_path), str(ordinary.video_path)}
    marker.write_text("Synthetic isolated test corpus\n")
    assert [card["local"]["video_path"] for card in api.get_state()["home"]["waiting"]] == [str(ordinary.video_path)]


def test_content_path_is_checked_when_save_path_is_outside_corpus(isolated_library):
    api, corpus, _marker = isolated_library
    api.manager.db.upsert_anime(LibraryAnime(1, "Synthetic anime", status="PLANNING", episodes=12, media_status="FINISHED"))
    item = download(api, corpus / "downloads")
    item.save_path = str(corpus.parent)
    api.manager.db.upsert_download(item)

    assert api.get_state()["home"]["waiting"] == []


def test_symlink_to_corpus_does_not_leak_into_waiting(isolated_library):
    api, corpus, _marker = isolated_library
    hidden = episode(api, corpus / "downloads")
    linked = corpus.parent / "shortcut.mkv"
    linked.symlink_to(hidden.video_path)
    api.manager.db.upsert_episode(LibraryEpisode(2, "Synthetic shortcut", 1, linked, state="waiting_subtitles"))

    assert api.get_state()["home"]["waiting"] == []


def test_isolated_resume_history_and_dropped_are_hidden(isolated_library):
    api, corpus, _marker = isolated_library
    api.config.agent.delete_after_watched_hours = 24
    for media_id, status in ((1, ""), (2, "COMPLETED"), (3, "DROPPED")):
        api.manager.db.upsert_anime(LibraryAnime(media_id, f"Synthetic {media_id}", status=status, episodes=1, media_status="FINISHED"))
        item = episode(api, corpus / "downloads", media_id=media_id, state="watched" if media_id == 2 else "ready")
        if media_id == 1:
            api.manager.db.record_playback(item.video_path, 120, 1200)
        if media_id == 2:
            item.watched_at = time.time()
            api.manager.db.upsert_episode(item)

    assert list(cards(api.get_state()["home"])) == []


def test_movie_falls_back_to_ordinary_copy(isolated_library):
    api, corpus, _marker = isolated_library
    anime = LibraryAnime(1, "Synthetic movie", status="CURRENT", format="MOVIE", media_status="FINISHED")
    api.manager.db.upsert_anime(anime)
    episode(api, corpus / "downloads", number=None)
    ordinary = episode(api, api.config.library.root_dir, number=None)

    assert api.get_state()["current"][0]["local"]["video_path"] == str(ordinary.video_path)
