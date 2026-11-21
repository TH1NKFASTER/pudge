"""A library folder with anime the person already has (not downloaded by Pudge): after a
scan every title lands in the right Home section.

Titles that are not being watched (Completed, Paused, Planning, not on AniList at all) are
under "Completed & ready" ("New episodes ready" while airing) when their next episode is ready,
or any local file is when every episode is watched (a rewatch); with no subtitles yet they wait
under "Waiting for preparation". A later ready episode never stands in for a next one that is
not downloaded: that title offers the download (test_fix_stale_rows_and_next_episode.py). AniList
is never changed. NCOP/NCED/PV next to the episodes stay in the Library only. Airing titles keep
the next-episode rule (test_v085_ten_issue_regressions.py), and a finished title whose next
episode is on disk but still being prepared waits for it
(test_v202_ready_series_subtitle_durability.py). The subtitles here are "<name>.ja.srt" next to
"[Group] ..." videos: the brackets used to be read as a glob set, so they were never found.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge.manager_models import LibraryAnime
from test_web_app import make_api

SUBTITLE = "1\n00:00:01,000 --> 00:00:03,000\nこんにちは、世界。\n"

# title, AniList status, airing status, progress, episodes, {episode: has subtitles}
TITLES = [
    ("Frieren", "COMPLETED", "FINISHED", 3, 3, {1: True, 2: True, 3: True}),
    ("Kaiju Girl", "COMPLETED", "FINISHED", 2, 2, {1: False, 2: False}),
    ("Monster", "PAUSED", "FINISHED", 1, 3, {1: True, 2: True, 3: True}),
    ("Mob Psycho", "PAUSED", "FINISHED", 3, 3, {1: True, 2: True, 3: True}),
    ("Mushishi", "PLANNING", "FINISHED", 0, 3, {2: True, 3: True}),
    ("Oshi no Ko", "PLANNING", "RELEASING", 0, 11, {1: True}),
    ("Haikyuu", "CURRENT", "FINISHED", 1, 3, {2: True, 3: True}),
    ("Hyouka", "", "FINISHED", 0, 2, {1: True, 2: True}),
]
EXPECTED = {
    "Frieren": "completed_ready",
    # Every episode watched: no "Waiting for preparation" card (Library only).
    "Kaiju Girl": None,
    "Monster": "completed_ready",
    "Mob Psycho": "completed_ready",
    # Episode 1 is not downloaded: episodes 2 and 3 are not offered in its place.
    "Mushishi": "download_available",
    "Oshi no Ko": "new_ready",
    "Haikyuu": "completed_ready",
    "Hyouka": "completed_ready",
    # Not on AniList and not in the local list: a title of its own.
    "Random Show": "completed_ready",
    "Unsubbed Show": "waiting",
}


def _video(folder: Path, name: str, subtitles: bool) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / f"{name}.mkv"
    video.write_bytes(b"video")
    if subtitles:
        video.with_suffix(".ja.srt").write_text(SUBTITLE, encoding="utf-8")
    return video


@pytest.fixture
def scanned(tmp_path: Path):
    api = make_api(tmp_path)
    db = api.manager.db
    root = api.config.library.root_dir
    for media_id, (title, status, airing, progress, episodes, files) in enumerate(TITLES, start=1):
        db.upsert_anime(
            LibraryAnime(
                media_id=media_id,
                title=title,
                status=status,
                media_status=airing,
                progress=progress,
                episodes=episodes,
                format="TV",
            )
        )
        for number, subtitles in files.items():
            _video(root / title, f"[Group] {title} - {number:02d} (1080p)", subtitles)
    _video(root / "Random Show", "[Group] Random Show - 01 (1080p)", True)
    _video(root / "Unsubbed Show", "[Group] Unsubbed Show - 01 (1080p)", False)
    # Extras next to the episodes of Frieren.
    _video(root / "Frieren", "[Group] Frieren - NCOP (1080p)", False)
    _video(root / "Frieren", "[Group] Frieren - NCED (1080p)", False)
    statuses = {row.media_id: row.status for row in db.anime_list()}
    api.manager.scan_library()
    return api, statuses


def _sections(api) -> dict[str, str]:
    home = api.get_state()["home"]
    found: dict[str, str] = {}
    for section, rows in home.items():
        for card in rows or []:
            for item in card.get("items") or [card]:
                found.setdefault(str(item.get("title")), section)
    return found


def test_each_title_of_an_existing_folder_lands_in_its_section(scanned) -> None:
    api, _statuses = scanned
    sections = _sections(api)
    assert {title: sections.get(title) for title in EXPECTED} == EXPECTED


def test_extras_next_to_the_episodes_stay_in_the_library_only(scanned) -> None:
    api, _statuses = scanned
    state = api.get_state()
    titles = {str(card.get("title")) for rows in state["home"].values() for card in rows or []}
    assert not any("NCOP" in title or "NCED" in title for title in titles)
    library_files = {
        Path(episode["video_path"]).name
        for group in state["library"]
        for episode in group.get("episodes") or []
    }
    assert {"[Group] Frieren - NCOP (1080p).mkv", "[Group] Frieren - NCED (1080p).mkv"} <= library_files
    # They do not wait for subtitles either.
    api.manager.process_subtitle_jobs(limit=20, wait_for_slot=True)
    waiting = {
        Path(row["video_path"]).name
        for row in api.manager.db.subtitle_jobs()
        if str(row["state"]) == "pending"
    }
    assert not any("NCOP" in name or "NCED" in name for name in waiting)


def test_a_scan_does_not_change_anilist_statuses(scanned) -> None:
    api, before = scanned
    after = {row.media_id: row.status for row in api.manager.db.anime_list() if row.media_id in before}
    assert after == before
