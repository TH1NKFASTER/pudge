from __future__ import annotations

import pytest

from pudge import release_parser as rp
from pudge.filename import parse_anime_filename
from pudge.providers.nyaa import release_episode, release_episode_range, release_group

pytestmark = pytest.mark.skipif(not rp.anitomy_available(), reason="anitopy not installed")


@pytest.mark.parametrize(
    "name, season, episode, span",
    [
        ("Show.S02E03.1080p.WEB.mkv", 2, 3, None),
        ("[Grp] Show [03] [1080p].mkv", None, 3, None),
        ("[Grp] Show - 01v2 [720p].mkv", None, 1, None),
        ("[SubsPlease] Aharen-san wa Hakarenai (01-12) (1080p) [Batch]", None, None, (1, 12)),
        ("[Erai-raws] Kuroko no Basket 2nd Season - 01 ~ 25 [1080p][Multiple Subtitle]", 2, None, (1, 25)),
        ("[Grp] Movie Name (2019) [BD 1080p].mkv", None, None, None),
        ("[Grp] Show - NCOP1 [1080p].mkv", None, None, None),
        ("[Grp] Show - SP1 [1080p].mkv", None, None, None),
        ("[Grp] Show - 12.5 [720p].mkv", None, None, None),
        ("からかい上手の高木さん 第03話.mkv", None, 3, None),
        ("[Grp] 86 - 05 [1080p].mkv", None, 5, None),
        ("[SubsPlease] Love Live! Superstar!! - 04 (1080p) [5D9E6A8D].mkv", None, 4, None),
    ],
)
def test_parse_release_name_safety(name, season, episode, span):
    parsed = rp.parse_release_name(name)
    assert parsed.parser_source == "anitomy"
    assert (parsed.season, parsed.episode, parsed.episode_range) == (season, episode, span)


def test_structured_fields():
    parsed = rp.parse_release_name("[SubsPlease] Love Live! Superstar!! - 04v2 (1080p) [5D9E6A8D].mkv")
    assert parsed.group == "SubsPlease" and parsed.title == "Love Live! Superstar!!"
    assert parsed.version == 2 and parsed.episode == 4, "v2 is a version, not episode 2"
    assert parsed.resolution == "1080p" and parsed.checksum == "5D9E6A8D"
    movie = rp.parse_release_name("[Grp] Movie Name (2019) [BD 1080p].mkv")
    assert movie.anime_type == "Movie" and movie.year == 2019 and movie.source == "BD"
    extra = rp.parse_release_name("[Grp] Show - NCOP1 [1080p].mkv")
    assert extra.extra and extra.unsafe_single_episode
    frac = rp.parse_release_name("[Grp] Show - 12.5 [720p].mkv")
    assert frac.fractional and frac.raw_episodes == ("12.5",)


def test_bare_numbers_are_not_explicit_episodes():
    assert rp.parse_release_name("Series 02 BDSUP.mkv").explicit_episode is False
    assert parse_anime_filename("Series 02 BDSUP.mkv").episode is None


def test_filename_and_release_parsers_agree_and_are_safe():
    assert parse_anime_filename("[Grp] Show - 12.5 [720p].mkv").episode is None  # was 12
    assert release_episode("[Grp] Show - 12.5 [720p].mkv") is None
    assert parse_anime_filename("[Erai-raws] Kuroko no Basket 2nd Season - 01 ~ 25 [1080p]").episode is None  # was 1
    assert release_episode("[Erai-raws] Kuroko no Basket 2nd Season - 01 ~ 25 [1080p]") is None
    assert release_episode_range("[Erai-raws] Kuroko no Basket 2nd Season - 01 ~ 25 [1080p]") == (1, 25)
    assert release_episode("[Grp] Show [03] [1080p].mkv") == 3  # was None
    assert parse_anime_filename("からかい上手の高木さん 第03話.mkv").episode == 3  # was None
    assert parse_anime_filename("[Judas] Hoshiai No Sora S1 - 01.mkv").season == 1
    assert release_episode("[Grp] Show - SP1 [1080p].mkv") is None
    # Parent-directory season fallback still wins over nothing and is kept.
    assert parse_anime_filename("Anime/Season 02/Episode 05.mkv").season == 2
    assert release_group("[Grp] Show - 05 [1080p]") == "Grp"


def test_legacy_fallback_when_anitomy_is_missing(monkeypatch):
    monkeypatch.setattr(rp, "_anitopy", None)
    rp.parse_release_name.cache_clear()
    try:
        parsed = rp.parse_release_name("[Grp] Show - 01-12 [1080p]")
        assert parsed.parser_source == "legacy" and "anitomy_unavailable" in parsed.warnings
        assert parsed.episode_range == (1, 12) and parsed.episode is None
        assert parse_anime_filename("[Grp] Show - 05 [1080p].mkv").episode == 5
    finally:
        rp.parse_release_name.cache_clear()


def test_parser_crash_falls_back(monkeypatch):
    class Broken:
        @staticmethod
        def parse(_name):
            raise ValueError("boom")

    monkeypatch.setattr(rp, "_anitopy", Broken)
    rp.parse_release_name.cache_clear()
    try:
        parsed = rp.parse_release_name("[Grp] Show - 07 [1080p].mkv")
        assert parsed.parser_source == "legacy" and "anitomy_error:ValueError" in parsed.warnings
        assert release_episode("[Grp] Show - 07 [1080p].mkv") == 7
    finally:
        rp.parse_release_name.cache_clear()
