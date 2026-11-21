"""An episode on a personal schedule counts as watched by the same rule as the AniList
count in mpv: past the watched threshold (85%) AND at most 10 minutes left.

The schedule used "or": a 24-minute Dorohedoro episode was marked watched with 10 minutes
still to go (13:41 of 23:40), the series moved from Continue watching to Up to date, while
AniList (which needs both) had not counted it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from pudge.manager_models import LibraryAnime, LibraryEpisode
from test_web_app import make_api

MEDIA = 105228


def _watching(tmp_path: Path, position: float, duration: float) -> dict:
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(
        LibraryAnime(
            media_id=MEDIA, title="Dorohedoro", status="CURRENT", progress=2, episodes=12, format="TV"
        )
    )
    video = (tmp_path / "[G] Dorohedoro - 03 (1080p).mkv").resolve()
    video.write_bytes(b"video")
    db.upsert_episode(
        LibraryEpisode(media_id=MEDIA, title="Dorohedoro", episode=3, video_path=video, state="ready")
    )
    key = api._play_key(str(video))
    api._schedule_play_sessions = {
        key: {
            "media_id": MEDIA,
            "episode": 3,
            "rewatch": False,
            "started_at": time.time() - 60,
            "baseline_watched_at": 0.0,
            "baseline_playback_updated_at": 0.0,
        }
    }
    db.record_playback(video, position, duration, 60.0)
    return api.play_status(str(video))


@pytest.mark.parametrize(
    ("position", "duration"),
    [
        (821.0, 1420.1),  # 58%, 10 minutes left: the Dorohedoro case
        (5200.0, 6000.0),  # a movie at 87% with 13 minutes left
    ],
)
def test_one_of_the_two_conditions_is_not_watched(tmp_path: Path, position: float, duration: float) -> None:
    result = _watching(tmp_path, position, duration)
    assert not result.get("personal_schedule_watched")


def test_past_the_threshold_with_little_left_is_watched(tmp_path: Path) -> None:
    result = _watching(tmp_path, 1240.0, 1420.1)  # 87%, 3 minutes left
    assert result.get("personal_schedule_watched") is True
