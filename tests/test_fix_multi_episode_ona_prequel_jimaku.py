"""Tensei Shitara Ken Deshita 2nd Season (AniList 159042, 12-episode ONA), episode 2.

The episode debug export showed the prepared subtitle came from Jimaku entry 807
(AniList 139587, the season-1 prequel): "[Nekomoe kissaten&VCB-Studio] Tensei
Shitara Ken Deshita [02]...JP.ass", i.e. S01E02 aligned onto S02E02. Every ONA
was treated as a one-episode "single special", which let the prequel entry pass
the explicit AniList mismatch guard.
"""

from __future__ import annotations

from pathlib import Path

from pudge.cli import _find_online_subtitles
from pudge.config import AppConfig
from pudge.models import AniListAnime, JimakuEntry, JimakuFile, SubtitleCandidate, VideoIdentity
from pudge.syncing import _candidate_explicit_anilist_mismatch

VIDEO_NAME = (
    "Reincarnated.as.a.Sword.S02E02.Reincarnated.as.a.Sword.2.REPACK.1080p.ADN.WEB-DL.AAC2.0.H.264-VARYG.mkv"
)
PREQUEL_FILE = "[Nekomoe kissaten&VCB-Studio] Tensei Shitara Ken Deshita [02][Ma10p_1080p][x265_flac].JP.ass"


def _anime(*, episodes: int | None, format: str = "ONA") -> AniListAnime:
    return AniListAnime(
        id=159042,
        titles=[
            "Tensei Shitara Ken Deshita 2nd Season",
            "Reincarnated as a Sword Season 2",
            "転生したら剣でした 第2期",
        ],
        synonyms=["Reincarnated as a Sword II", "TenKen 2"],
        season_year=2026,
        episodes=episodes,
        format=format,
    )


def _run(tmp_path: Path, monkeypatch, anime: AniListAnime, episode: int):
    cfg = AppConfig()
    cfg.jimaku.api_key = "token"
    cfg.paths.cache_dir = tmp_path / "cache"
    video = tmp_path / VIDEO_NAME
    video.write_bytes(b"video")
    prequel = JimakuEntry(
        id=807,
        name="Tensei Shitara Ken Deshita",
        english_name="Reincarnated as a Sword",
        japanese_name="転生したら剣でした",
        anilist_id=139587,
        flags={},
    )
    looked_up: list[int] = []

    class FakeJimaku:
        def __init__(self, *_args, **_kwargs):
            pass

        def search_entries(self, *, anilist_id=None, query=None):
            if anilist_id is not None:
                assert anilist_id == 159042
                return []
            return [prequel]

        def rank_entries(self, entries, _identity, _anilist_id):
            return list(entries)

        def files_for_episode(self, entry_id, _episode, alternative_episodes=()):
            looked_up.append(entry_id)
            return [
                JimakuFile(
                    url="https://jimaku.cc/entry/807/download/x.ass",
                    name=PREQUEL_FILE,
                    size=1,
                    last_modified="2024-04-30T19:17:32Z",
                    score=119.54,
                    details={"parsed_episode": 2, "episode_match": "exact"},
                )
            ]

        def rank_files(self, files, *_args, **_kwargs):
            return list(files)

        def close(self) -> None:
            pass

    monkeypatch.setattr("pudge.cli.JimakuClient", FakeJimaku)
    monkeypatch.setattr(
        "pudge.cli.materialize_jimaku_files",
        lambda _client, item, *_args, **_kwargs: [
            SubtitleCandidate(path=tmp_path / item.name, source="jimaku", score=item.score, name=item.name)
        ],
    )
    monkeypatch.setattr("pudge.cli._jimaku_episode_aliases", lambda *_args: (episode + 12,))
    result = _find_online_subtitles(
        video,
        VideoIdentity(title="Reincarnated as a Sword", episode=episode, season=2),
        cfg,
        None,
        False,
        anime_hint=anime,
        skip_airing_lookup=True,
    )
    return result, looked_up


def test_multi_episode_ona_rejects_prequel_entry_with_same_base_title(tmp_path: Path, monkeypatch) -> None:
    result, looked_up = _run(tmp_path, monkeypatch, _anime(episodes=12), 2)

    assert result == []
    assert looked_up == []


def test_airing_ona_with_unknown_episode_count_rejects_prequel_for_episode_two(
    tmp_path: Path, monkeypatch
) -> None:
    result, looked_up = _run(tmp_path, monkeypatch, _anime(episodes=None), 2)

    assert result == []
    assert looked_up == []


def test_one_episode_special_keeps_stale_parent_title_override(tmp_path: Path, monkeypatch) -> None:
    special = _anime(episodes=1, format="SPECIAL")
    result, looked_up = _run(tmp_path, monkeypatch, special, 1)

    assert looked_up == [807]
    assert result and result[0].details["single_special_exact_entry"] is True


def test_stored_export_candidate_with_corrected_flags_is_a_mismatch(tmp_path: Path) -> None:
    candidate = SubtitleCandidate(
        path=tmp_path / PREQUEL_FILE,
        source="jimaku",
        score=79.51,
        name=PREQUEL_FILE,
        details={
            "entry_id": 807,
            "entry_anilist_id": 139587,
            "requested_anilist_id": 159042,
            "entry_identity_exact_title_match": True,
            "single_special_exact_entry": False,
            "exact_anilist_movie_entry": False,
            "media_format": "ONA",
            "requested_episode": 2,
        },
    )
    assert _candidate_explicit_anilist_mismatch(candidate) is True
