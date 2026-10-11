"""The Anime state is built without re-reading the whole library for every card, and its
build time is logged (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import logging
from pathlib import Path

from pudge.manager_models import LibraryAnime, LibraryEpisode
from test_web_app import make_api


def _api_with_ready_anime(tmp_path: Path, count: int):
    api = make_api(tmp_path)
    folder = tmp_path / "library"
    folder.mkdir(exist_ok=True)
    for media_id in range(1, count + 1):
        api.manager.db.upsert_anime(
            LibraryAnime(
                media_id=media_id,
                title=f"Show {media_id}",
                status="CURRENT",
                progress=0,
                episodes=12,
                media_status="RELEASING",
            )
        )
        for episode in (1, 2):
            video = folder / f"Show {media_id} - {episode:02d}.mkv"
            video.write_bytes(b"video")
            subtitle = video.with_suffix(".ja.srt")
            subtitle.write_text("日本語", encoding="utf-8")
            api.manager.db.upsert_episode(
                LibraryEpisode(
                    media_id=media_id,
                    title=f"Show {media_id}",
                    episode=episode,
                    video_path=video,
                    subtitle_path=subtitle,
                    state="ready",
                )
            )
    return api


def _count_reads(api, monkeypatch) -> dict[str, list]:
    reads: dict[str, list] = {"anime_list": [], "get_anime": []}
    db = api.manager.db
    anime_list, get_anime = db.anime_list, db.get_anime
    monkeypatch.setattr(
        db, "anime_list", lambda *a, **k: reads["anime_list"].append(1) or anime_list(*a, **k)
    )
    monkeypatch.setattr(
        db,
        "get_anime",
        lambda media_id, *a, **k: reads["get_anime"].append(int(media_id)) or get_anime(media_id, *a, **k),
    )
    return reads


def test_ready_queue_builder_does_not_reload_entire_anime_library_per_card(
    tmp_path: Path, monkeypatch
) -> None:
    # get_state() reads the library a fixed number of times, however many cards there are.
    counts = []
    for cards in (1, 6):
        api = _api_with_ready_anime(tmp_path / str(cards), cards)
        reads = _count_reads(api, monkeypatch)
        state = api.get_state()
        assert len(state["current"]) == cards
        counts.append(len(reads["anime_list"]))
        api.close()
    assert counts[0] == counts[1]


def test_anime_payload_reuses_current_anime_for_queue_badges(tmp_path: Path, monkeypatch) -> None:
    api = _api_with_ready_anime(tmp_path, 2)
    anime = api.manager.db.get_anime(1)
    reads = _count_reads(api, monkeypatch)
    payload = api._anime_payload(anime)
    assert payload["queue_next_count"] == 2  # both ready episodes are queued
    assert reads["anime_list"] == [] and 1 not in reads["get_anime"]
    api.close()


def test_full_state_timing_is_logged_for_runtime_regressions(tmp_path: Path, caplog) -> None:
    api = _api_with_ready_anime(tmp_path, 1)
    api.logger = logging.getLogger("t71g13.web")
    with caplog.at_level(logging.INFO, logger="t71g13.web"):
        api.get_state()
    messages = [record.getMessage() for record in caplog.records if record.name == "t71g13.web"]
    assert "START step=web.get_state" in messages
    assert any(message.startswith("DONE step=web.get_state duration_ms=") for message in messages)
    api.close()
