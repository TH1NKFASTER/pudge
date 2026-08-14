from __future__ import annotations

from pathlib import Path

from pudge.cli import _find_online_subtitles, _jimaku_entry_anilist_conflicts
from pudge.config import AppConfig
from pudge.models import AniListAnime, JimakuEntry, SubtitleCandidate, VideoIdentity
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
            "requested_episode": None,
        },
    )
    assert _candidate_explicit_anilist_mismatch(special) is False


def test_optimizer_rejects_cross_season_even_when_base_title_matches(tmp_path: Path) -> None:
    candidate = SubtitleCandidate(
        path=tmp_path / "wrong-s01e12.srt",
        source="jimaku",
        score=59.925,
        name="Re Life in a different world from zero.S01E12.WEBRip.ja[cc].srt",
        details={
            "entry_anilist_id": 21355,
            "requested_anilist_id": 189046,
            "entry_anilist_match": False,
            "entry_identity_exact_title_match": True,
            "single_special_exact_entry": False,
            "exact_anilist_movie_entry": False,
            "media_format": "TV",
            "requested_episode": 78,
        },
    )
    assert _candidate_explicit_anilist_mismatch(candidate) is True


def test_rezero_s4_absolute_episode_rejects_exact_title_s1_entry(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = AppConfig()
    cfg.jimaku.api_key = "token"
    cfg.paths.cache_dir = tmp_path / "cache"
    video = tmp_path / "[SubsPlease] Re Zero kara Hajimeru Isekai Seikatsu - 78.mkv"
    video.write_bytes(b"video")
    wrong_first_season = JimakuEntry(
        id=332,
        name="Re:Zero kara Hajimeru Isekai Seikatsu",
        english_name="Re:ZERO -Starting Life in Another World-",
        japanese_name=None,
        anilist_id=21355,
        flags={},
    )
    requested_entries: list[int] = []

    class FakeJimaku:
        def __init__(self, *_args, **_kwargs):
            pass

        def search_entries(self, *, anilist_id=None, query=None):
            if anilist_id is not None:
                assert anilist_id == 189046
                return []
            return [wrong_first_season]

        def rank_entries(self, entries, _identity, _anilist_id):
            return list(entries)

        def files_for_episode(self, entry_id, _episode, alternative_episodes=()):
            requested_entries.append(entry_id)
            raise AssertionError("cross-season Jimaku entry must be rejected before file lookup")

        def close(self) -> None:
            pass

    monkeypatch.setattr("pudge.cli.JimakuClient", FakeJimaku)
    monkeypatch.setattr("pudge.cli._jimaku_episode_aliases", lambda *_args: (12,))

    result = _find_online_subtitles(
        video,
        VideoIdentity(title="Re:Zero kara Hajimeru Isekai Seikatsu", episode=78),
        cfg,
        None,
        False,
        anime_hint=_anime(189046),
        skip_airing_lookup=True,
    )

    assert result == []
    assert requested_entries == []


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
