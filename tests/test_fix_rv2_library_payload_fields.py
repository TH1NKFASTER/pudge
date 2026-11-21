"""The Library list in the window's snapshot carries only what the window reads.

One entry per local episode goes out with every changed snapshot (~360 bytes each): the
sizes (a stat of every file per snapshot), the playback place/duration, the subtitle file
name and the summed durations were never read by the page.
"""

from __future__ import annotations

from pathlib import Path

from pudge.manager_models import LibraryAnime, LibraryEpisode
from test_web_app import make_api

GROUP_FIELDS = {
    "media_id",
    "title",
    "cover",
    "cover_remote",
    "site_url",
    "media_status",
    "list_status",
    "personal_schedule",
    "watched_folder",
    "episodes",
    "episode_count",
    "ready_count",
    "watched_count",
    "waiting_count",
}
EPISODE_FIELDS = {
    "episode",
    "stored_episode",
    "video_path",
    "filename",
    "subtitle_path",
    "subtitle_source",
    "state",
}


def test_library_entries_have_only_the_fields_the_window_reads(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(LibraryAnime(media_id=7, title="Invented Show", status="CURRENT", episodes=2))
    for episode in (1, 2):
        video = tmp_path / f"Invented Show - {episode:02d}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(LibraryEpisode(7, "Invented Show", episode, video, state="ready"))

    (group,) = api.get_state()["library"]

    assert set(group) == GROUP_FIELDS
    assert [set(item) for item in group["episodes"]] == [EPISODE_FIELDS, EPISODE_FIELDS]
    assert group["episode_count"] == 2 and group["ready_count"] == 2
