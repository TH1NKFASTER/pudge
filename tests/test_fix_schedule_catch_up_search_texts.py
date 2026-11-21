"""Ansatsu Kyoushitsu sat in "Waiting for preparation · All episodes ready"; searching "fable"
put "The Fable" eighth; the Settings texts the person found odd."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from css_support import css_rules
from pudge.i18n import t
from pudge.manager_models import LibraryAnime, LibraryEpisode
from test_web_app import make_api

WEEK = 7 * 86400


def _episode(tmp_path: Path, media_id: int, number: int, state: str) -> LibraryEpisode:
    video = tmp_path / "library" / f"Show {media_id} - {number:02d}.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    subtitle = video.with_suffix(".srt")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
    return LibraryEpisode(
        media_id=media_id,
        title="Ansatsu Kyoushitsu",
        episode=number,
        video_path=video.resolve(),
        subtitle_path=subtitle.resolve(),
        subtitle_origin="jimaku",
        state=state,
    )


@pytest.mark.parametrize("evidence", ["anilist", "library"])
def test_episode_watched_outside_the_schedule_check_counts_for_the_schedule(
    tmp_path: Path, evidence: str
) -> None:
    # A weekly schedule from episode 1; episode 6 was finished while Pudge restarted, so the
    # schedule's own playback check never saw it. Episode 7 opens tomorrow.
    api = make_api(tmp_path)
    db = api.manager.db
    anime = LibraryAnime(
        media_id=20755,
        title="Ansatsu Kyoushitsu",
        status="CURRENT",
        progress=0,
        episodes=22,
        format="TV",
        media_status="FINISHED",
    )
    db.upsert_anime(anime)
    start = time.strftime("%Y-%m-%dT%H:%M", time.gmtime(time.time() - 6 * WEEK + 86400))
    assert api.personal_schedule_save(20755, start, "UTC", "weeks", 1, 0)["ok"]
    for number in range(1, 6):
        db.mark_personal_release_watched(20755, number)
    anime.progress = 6 if evidence == "anilist" else 5
    db.upsert_anime(anime)
    for number in range(1, 7):
        db.upsert_episode(_episode(tmp_path, 20755, number, "watched"))

    home = api.get_state()["home"]

    assert [card["title"] for card in home.get("waiting") or []] == []
    [card] = home["caught_up"]
    assert card["personal_schedule"]["next_episode"] == 7
    # Episode 7 itself, once it opens, is not refused as "watch episode 6 first".
    episode_seven = _episode(tmp_path, 20755, 7, "ready")
    db.upsert_episode(episode_seven)
    admission = api._personal_schedule_play_admission(episode_seven.video_path)
    assert admission.get("reason") == "not_unlocked"


def test_a_title_containing_the_query_comes_before_loose_matches(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    # Ready on disk, matched only loosely ("fable" ~ "Gambler").
    db.upsert_anime(
        LibraryAnime(
            media_id=1,
            title="Kakegurui",
            synonyms=["Kakegurui - Compulsive Gambler"],
            status="CURRENT",
        )
    )
    db.upsert_episode(_episode(tmp_path, 1, 1, "ready"))
    db.upsert_anime(LibraryAnime(media_id=2, title="Grand Blue Season 3", status="CURRENT"))
    db.upsert_anime(LibraryAnime(media_id=3, title="The Fable", status="PLANNING"))

    rows = api.global_media_search("fable", 40)

    assert rows[0]["title"] == "The Fable"
    assert "Kakegurui" in [row["title"] for row in rows[1:]]


def test_settings_texts() -> None:
    assert t("update.devCurrent", lang="en") == t("update.current", lang="en") == "Pudge is up to date."
    assert t("web.irodoriUpToDate", lang="en", revision="61012c76") == "Irodori is up to date."
    for lang in ("en", "ru"):
        hint = t("ui.turnOffSpatialAudioHint", lang=lang)
        assert "next episode" not in hint and "следующей" not in hint
    # Shortcuts: the Visual novels group stands apart from the mpv rows above it.
    gap = css_rules("index.html").declarations(".form-grid + .setting-divider-title")
    assert gap["margin-top"] == "24px"
