"""Jimaku names such as "Roshidere-08-JPN v1.ass" carry the episode as a dash-joined
number before a language or version tag; resolutions, years, title numbers and version
numbers stay no episode.  Invented names."""

from __future__ import annotations

import pytest

from pudge.filename import parse_anime_filename


@pytest.mark.parametrize(
    ("name", "title", "episode"),
    [
        ("Roshidere-08-JPN v1.ass", "Roshidere", 8),
        ("Roshidere-08-JPN.ass", "Roshidere", 8),
        ("Roshidere-08v2-ja.srt", "Roshidere", 8),
        ("Roshidere-08-v2.ass", "Roshidere", 8),
        (
            "Tokidoki Bosotto Russia-go de Dereru Tonari no Alya-san-12-Japanese.srt",
            "Tokidoki Bosotto Russia-go de Dereru Tonari no Alya-san",
            12,
        ),
        ("Roshidere - 08 - JPN v1.ass", "Roshidere", 8),
    ],
)
def test_dash_number_before_a_language_or_version_tag_is_the_episode(name, title, episode):
    parsed = parse_anime_filename(name)
    assert (parsed.title, parsed.episode) == (title, episode)


@pytest.mark.parametrize(
    "name",
    [
        "Show-2024-JPN.ass",  # year
        "Show-1080-JPN.ass",  # resolution
        "Kaiju No. 8-JPN.ass",  # the title's own number
        "Steins-Gate-0-JPN.ass",  # 0 is never an episode
        "Show-JPN-v2.ass",  # version only
        "Show [ABCD1234]-JPN.ass",  # CRC
        "Show-08x-JPN.ass",
    ],
)
def test_no_false_episode(name):
    assert parse_anime_filename(name).episode is None
