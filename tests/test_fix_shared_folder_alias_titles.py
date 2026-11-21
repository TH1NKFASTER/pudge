"""Wave-4 follow-up to WEBFLOW-10: a shared-folder subtitle named by the title's English
(or another AniList) name is accepted; a different show still is not."""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge.cli import _anime_title_names
from pudge.local_search import find_local_subtitles
from pudge.models import AniListAnime
from pudge.filename import parse_anime_filename

LINE = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは、元気ですか。今日はいい天気ですね。\n"
MIN_SCORE = 68.0

CASES = [
    (
        "[SubsPlease] Ore dake Level Up na Ken - 05 (1080p).mkv",
        "Solo.Leveling.S01E05.WEBRip.Netflix.ja[cc].srt",
        ["Ore dake Level Up na Ken", "Solo Leveling", "俺だけレベルアップな件"],
    ),
    (
        "[SubsPlease] Hikaru ga Shinda Natsu - 03 (1080p).mkv",
        "The Summer Hikaru Died - 03.ja.srt",
        ["Hikaru ga Shinda Natsu", "The Summer Hikaru Died", "光が死んだ夏"],
    ),
    (
        "[SubsPlease] Hibike! Euphonium S3 - 05 (1080p).mkv",
        "Sound! Euphonium 3 - 05.ja.srt",
        ["Hibike! Euphonium 3", "Sound! Euphonium 3", "響け！ユーフォニアム3"],
    ),
]


def _search(tmp_path: Path, video_name: str, subtitle_name: str, titles: list[str]):
    folder = tmp_path / "library" / "Show"
    folder.mkdir(parents=True)
    video = folder / video_name
    video.write_bytes(b"x")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / subtitle_name).write_text(LINE, encoding="utf-8")
    identity = parse_anime_filename(video)
    return find_local_subtitles(
        video, identity, [shared], tmp_path / "cache", 1000, min_score=MIN_SCORE, titles=titles
    )


@pytest.mark.parametrize(("video_name", "subtitle_name", "titles"), CASES)
def test_subtitle_named_by_another_anilist_title_is_accepted(
    tmp_path: Path, video_name: str, subtitle_name: str, titles: list[str]
) -> None:
    found = _search(tmp_path, video_name, subtitle_name, titles)
    assert [c.name for c in found if c.score >= MIN_SCORE] == [subtitle_name]
    # Without the AniList names (the old gate) it was dropped: 122.5 -> 2.5 for Solo Leveling.
    without = _search(tmp_path / "plain", video_name, subtitle_name, [])
    assert [c.name for c in without if c.score >= MIN_SCORE] == []


def test_a_different_show_with_the_same_episode_is_still_rejected(tmp_path: Path) -> None:
    found = _search(
        tmp_path,
        "[SubsPlease] Ore dake Level Up na Ken - 05 (1080p).mkv",
        "Kimi no Na wa - 05.ja.srt",
        ["Ore dake Level Up na Ken", "Solo Leveling"],
    )
    assert [c.name for c in found if c.score >= MIN_SCORE] == []


def test_cli_passes_every_anilist_name() -> None:
    anime = AniListAnime(
        id=1,
        titles=["Ore dake Level Up na Ken", "Solo Leveling", " "],
        synonyms=["Solo Leveling", "SL"],
        season_year=2024,
        episodes=12,
        format="TV",
    )
    assert _anime_title_names(anime) == ("Ore dake Level Up na Ken", "Solo Leveling", "SL")
    assert _anime_title_names(None) == ()


ONE_WORD_CASES = [
    (
        "[Erai-raws] Kaguya-sama wa Kokurasetai - Tensai-tachi no Renai Zunousen - 05 [1080p].mkv",
        "Kaguyasama wa Kokurasetai - 05.ja.srt",
        ["Kaguya-sama wa Kokurasetai: Tensai-tachi no Renai Zunousen", "Kaguya-sama: Love is War"],
    ),
    (
        "[SubsPlease] Oshi no Ko - 05 (1080p).mkv",
        "OshiNoKo - 05.ja.srt",
        ["Oshi no Ko", "【Oshi no Ko】", "【推しの子】"],
    ),
]


@pytest.mark.parametrize(("video_name", "subtitle_name", "titles"), ONE_WORD_CASES)
def test_a_title_written_as_one_word_is_still_accepted(
    tmp_path: Path, video_name: str, subtitle_name: str, titles: list[str]
) -> None:
    # Whole-word comparison alone rejected these (66.7 and 53.3 on the gate, needs 70).
    found = _search(tmp_path, video_name, subtitle_name, titles)
    assert [c.name for c in found if c.score >= MIN_SCORE] == [subtitle_name]


@pytest.mark.parametrize(
    ("video_name", "subtitle_name", "titles"),
    [
        ("[SubsPlease] Kimetsu no Yaiba - 05 (1080p).mkv", "Kimi no Na wa - 05.ja.srt", ["Kimetsu no Yaiba"]),
        (
            "[SubsPlease] Mushoku Tensei - 05 (1080p).mkv",
            "Tensei shitara Slime Datta Ken - 05.ja.srt",
            ["Mushoku Tensei: Isekai Ittara Honki Dasu"],
        ),
        ("[SubsPlease] Oshi no Ko - 05 (1080p).mkv", "Oshiete Galko-chan - 05.ja.srt", ["Oshi no Ko"]),
    ],
)
def test_spaceless_comparison_does_not_let_other_shows_in(
    tmp_path: Path, video_name: str, subtitle_name: str, titles: list[str]
) -> None:
    found = _search(tmp_path, video_name, subtitle_name, titles)
    assert [c.name for c in found if c.score >= MIN_SCORE] == []
