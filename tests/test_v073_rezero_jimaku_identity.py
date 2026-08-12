from __future__ import annotations

from pathlib import Path

from pudge.cli import _jimaku_entry_anilist_conflicts
from pudge.models import AniListAnime, JimakuEntry, SubtitleCandidate
from pudge.providers.jimaku import explicit_episode_range
from pudge.syncing import _candidate_explicit_anilist_mismatch


def _anime(media_id: int) -> AniListAnime:
    return AniListAnime(
        id=media_id,
        titles=["Re:Zero kara Hajimeru Isekai Seikatsu 4th Season"],
        synonyms=[],
        season_year=2026,
        episodes=19,
        format="TV",
    )


def test_season_marker_is_not_an_episode_range() -> None:
    name = (
        "[NanakoRaws] Re Zero kara Hajimeru Isekai Seikatsu "
        "S3 - 13 (AT-X 1920x1080 x265 AAC).ass"
    )
    assert explicit_episode_range(name) is None


def test_real_episode_ranges_still_parse() -> None:
    assert explicit_episode_range("Anime - 01-13.srt") == (1, 13)
    assert explicit_episode_range("Anime S03E01-13.srt") == (1, 13)
    assert explicit_episode_range("Anime EP01-13.srt") == (1, 13)


def test_explicit_jimaku_anilist_mismatch_is_hard_conflict() -> None:
    wrong_season = JimakuEntry(
        id=7615,
        name="Re:Zero kara Hajimeru Isekai Seikatsu 3rd Season",
        english_name=None,
        japanese_name=None,
        anilist_id=163134,
        flags={},
    )
    assert _jimaku_entry_anilist_conflicts(wrong_season, _anime(189046)) is True


def test_unlinked_jimaku_entry_is_not_rejected_only_for_missing_id() -> None:
    unlinked = JimakuEntry(
        id=999,
        name="Re:Zero",
        english_name=None,
        japanese_name=None,
        anilist_id=None,
        flags={},
    )
    assert _jimaku_entry_anilist_conflicts(unlinked, _anime(189046)) is False


def test_optimizer_rejects_explicit_cross_season_candidate(tmp_path: Path) -> None:
    candidate = SubtitleCandidate(
        path=tmp_path / "wrong.ass",
        source="jimaku",
        score=76.05,
        name="[NanakoRaws] Re Zero S3 - 13.ass",
        details={
            "entry_anilist_id": 163134,
            "requested_anilist_id": 189046,
            "entry_anilist_match": False,
        },
    )
    assert _candidate_explicit_anilist_mismatch(candidate) is True


def test_optimizer_allows_exact_identity_title_stale_parent_override(tmp_path: Path) -> None:
    special = SubtitleCandidate(
        path=tmp_path / "cloudy-beach.srt",
        source="jimaku",
        score=90.0,
        name="死亡遊戯で飯を食う。.44：CLOUDY.BEACH.srt",
        details={
            "entry_anilist_id": 209961,
            "requested_anilist_id": 180746,
            "entry_identity_exact_title_match": True,
        },
    )
    assert _candidate_explicit_anilist_mismatch(special) is False


def test_optimizer_keeps_exact_or_unlinked_identity(tmp_path: Path) -> None:
    exact = SubtitleCandidate(
        path=tmp_path / "exact.srt",
        source="jimaku",
        score=50.0,
        name="exact.srt",
        details={"entry_anilist_id": 189046, "requested_anilist_id": 189046},
    )
    unlinked = SubtitleCandidate(
        path=tmp_path / "unlinked.srt",
        source="jimaku",
        score=50.0,
        name="unlinked.srt",
        details={"entry_anilist_id": None, "requested_anilist_id": 189046},
    )
    assert _candidate_explicit_anilist_mismatch(exact) is False
    assert _candidate_explicit_anilist_mismatch(unlinked) is False
