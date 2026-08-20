from __future__ import annotations

import tempfile
from pathlib import Path

from hypothesis import given, settings, strategies as st

from pudge.filename import normalize_title, parse_anime_filename
from pudge.mobile_sync import MobileSyncService
from pudge.subtitle_formats import parse_srt, write_srt


@given(st.text(max_size=180))
def test_filename_normalization_is_idempotent(value: str) -> None:
    normalized = normalize_title(value)
    assert normalize_title(normalized) == normalized


@given(
    title=st.text(
        alphabet=list("abcdefghijklmnopqrstuvwxyz     -_."),
        min_size=1,
        max_size=80,
    ),
    season=st.integers(min_value=1, max_value=99),
    episode=st.integers(min_value=1, max_value=999),
)
def test_filename_parser_preserves_explicit_season_episode(title: str, season: int, episode: int) -> None:
    identity = parse_anime_filename(f"{title} S{season:02d}E{episode:03d}.mkv")
    assert identity.season == season
    assert identity.episode == episode


@settings(max_examples=40)
@given(
    starts=st.lists(
        st.integers(min_value=0, max_value=100_000),
        min_size=1,
        max_size=20,
        unique=True,
    )
)
def test_srt_write_parse_round_trip_keeps_valid_monotonic_cues(
    starts: list[int],
) -> None:
    cues = [
        (start / 1000.0, start / 1000.0 + 0.4, f"日本語 {index}")
        for index, start in enumerate(sorted(starts))
    ]
    with tempfile.TemporaryDirectory() as temporary:
        path = write_srt(cues, Path(temporary) / "property.srt")
        parsed = parse_srt(path)
    assert parsed
    assert all(start < end for start, end, _text in parsed)
    assert [start for start, _end, _text in parsed] == sorted(start for start, _end, _text in parsed)


@given(
    episode=st.integers(min_value=-1000, max_value=1000),
    position=st.integers(min_value=-(10**9), max_value=10**9),
    duration=st.integers(min_value=-(10**9), max_value=10**9),
)
def test_mobile_sync_anime_position_is_always_bounded(episode: int, position: int, duration: int) -> None:
    normalized = MobileSyncService._normalize_position(
        "anime_episode",
        {"episode": episode, "position_ms": position, "duration_ms": duration},
    )
    assert normalized["episode"] >= 1
    assert normalized["position_ms"] >= 0
    assert normalized["duration_ms"] >= 0
