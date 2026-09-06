from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers.nyaa import _seed_availability_bonus, score_release


ANSATSU = LibraryAnime(
    media_id=20755,
    title="Ansatsu Kyoushitsu",
    titles=["Ansatsu Kyoushitsu", "Assassination Classroom"],
    episodes=22,
    format="TV",
)


def _release(
    title: str,
    *,
    seeders: int,
    size_gib: float,
    is_batch: bool,
    info_hash: str,
) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link=f"https://nyaa.si/view/{info_hash}",
        torrent_url=f"https://nyaa.si/download/{info_hash}.torrent",
        info_hash=info_hash,
        size_text=f"{size_gib:.1f} GiB",
        size_bytes=int(size_gib * 1024**3),
        seeders=seeders,
        leechers=0,
        downloads=1000,
        trusted=True,
        remake=False,
        category_id="1_2",
        published="Thu, 01 Aug 2024 00:00:00 +0000",
        is_batch=is_batch,
        group="",
    )


def _score_batch(item: NyaaRelease) -> NyaaRelease:
    return score_release(
        item,
        ANSATSU,
        episode=None,
        batch=True,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
        preferred_sources=["BluRay", "WEB-DL", "WEBRip"],
    )


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.nyaa.min_release_score = 72.0
    cfg.nyaa.min_seeders = 1
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(ANSATSU)
    return manager


def test_seed_bonus_saturates_after_twenty() -> None:
    assert _seed_availability_bonus(0) == 0.0
    assert _seed_availability_bonus(6) < _seed_availability_bonus(20) - 10.0
    assert _seed_availability_bonus(20) == 30.0
    assert _seed_availability_bonus(200) - _seed_availability_bonus(20) <= 4.0
    assert _seed_availability_bonus(2000) == 34.0


def test_bd_marker_counts_as_bluray_preference() -> None:
    okay = _score_batch(
        _release(
            "[Okay-Subs] Assassination Classroom S1 (BD 1080p) | "
            "Ansatsu Kyoushitsu",
            seeders=31,
            size_gib=48.7,
            is_batch=False,
            info_hash="okay",
        )
    )

    assert "BD" in okay.reasons
    assert "source-preferred=BluRay" in okay.reasons


def test_batch_ranking_prefers_healthy_bluray_season_pack_over_old_range_label() -> None:
    horrible = _score_batch(
        _release(
            "[HorribleSubs] Assassination Classroom (01-22) [1080p] (Batch)",
            seeders=6,
            size_gib=15.1,
            is_batch=True,
            info_hash="horrible",
        )
    )
    okay = _score_batch(
        _release(
            "[Okay-Subs] Assassination Classroom S1 (BD 1080p) | "
            "Ansatsu Kyoushitsu",
            seeders=31,
            size_gib=48.7,
            is_batch=False,
            info_hash="okay",
        )
    )

    assert okay.score > horrible.score


def test_batch_first_accepts_season_pack_without_batch_word(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    horrible = _score_batch(
        _release(
            "[HorribleSubs] Assassination Classroom (01-22) [1080p] (Batch)",
            seeders=6,
            size_gib=15.1,
            is_batch=True,
            info_hash="horrible",
        )
    )
    okay = _score_batch(
        _release(
            "[Okay-Subs] Assassination Classroom S1 (BD 1080p) | "
            "Ansatsu Kyoushitsu",
            seeders=31,
            size_gib=48.7,
            is_batch=False,
            info_hash="okay",
        )
    )
    manager.search_releases = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: [okay, horrible]
    )
    added: list[NyaaRelease] = []
    manager.add_release = (  # type: ignore[method-assign]
        lambda _media_id, item, **_kwargs: added.append(item) or True
    )

    selected = manager.search_and_add_best(
        ANSATSU.media_id,
        episode=None,
        batch=True,
        automatic=False,
        require_explicit_batch=True,
    )

    assert selected is okay
    assert added == [okay]


def test_batch_first_rejects_movies_multi_season_and_partial_ranges(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    base = _score_batch(
        _release(
            "[Okay-Subs] Assassination Classroom S1 (BD 1080p) | "
            "Ansatsu Kyoushitsu",
            seeders=31,
            size_gib=48.7,
            is_batch=False,
            info_hash="okay",
        )
    )
    movie = replace(
        base,
        title="Assassination Classroom Movie 1080p",
        score=999.0,
        reasons=["exact-title-phrase", "large-pack-candidate"],
        info_hash="movie",
    )
    multi = replace(
        base,
        title="Assassination Classroom S01+S02 [Batch] 1080p",
        score=998.0,
        reasons=["exact-title-phrase", "batch", "large-pack-candidate"],
        is_batch=True,
        info_hash="multi",
    )
    partial = replace(
        base,
        title="Assassination Classroom 01-12 [Batch] 1080p",
        score=997.0,
        reasons=[
            "exact-title-phrase",
            "batch",
            "range=1-12",
            "partial-series-range",
        ],
        is_batch=True,
        info_hash="partial",
    )
    manager.search_releases = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: [movie, multi, partial, base]
    )
    manager.add_release = lambda *_args, **_kwargs: True  # type: ignore[method-assign]

    selected = manager.search_and_add_best(
        ANSATSU.media_id,
        episode=None,
        batch=True,
        automatic=False,
        require_explicit_batch=True,
    )

    assert selected is base


def test_production_search_rejects_explicit_wrong_season_before_ranking() -> None:
    from pudge.providers.nyaa import search_ranked

    anime = LibraryAnime(
        media_id=16498,
        title="Shingeki no Kyojin",
        titles=["Attack on Titan"],
        episodes=25,
        format="TV",
    )
    wrong = _release(
        "[Erai-raws] Shingeki no Kyojin Season 2 - 01 ~ 12 "
        "[720p][BATCH][Multiple Subtitle]",
        seeders=20,
        size_gib=8.5,
        is_batch=True,
        info_hash="aot-s2",
    )

    class FakeClient:
        def search(self, _query: str) -> list[NyaaRelease]:
            return [wrong]

    ranked = search_ranked(
        FakeClient(),
        anime,
        episode=11,
        batch=True,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="720p",
        min_seeders=1,
        target_episode_min_bytes=40 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )
    assert ranked == []


def test_shared_production_batch_guard_rejects_multi_season_pack(tmp_path: Path) -> None:
    from pudge.providers.nyaa import release_is_safe_batch_candidate, score_release

    anime = LibraryAnime(
        media_id=16498,
        title="Shingeki no Kyojin",
        titles=["Attack on Titan"],
        episodes=25,
        format="TV",
    )
    item = _release(
        "[HorribleSubs] Shingeki no Kyojin S1 - S3 Complete [720p] (Unofficial Batch)",
        seeders=19,
        size_gib=22.3,
        is_batch=True,
        info_hash="aot-s1-s3",
    )
    scored = score_release(
        item,
        anime,
        episode=11,
        batch=True,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="720p",
        min_seeders=1,
        target_episode_min_bytes=40 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )
    assert release_is_safe_batch_candidate(anime, scored) is False

    manager = _manager(tmp_path)
    manager.db.upsert_anime(anime)
    assert manager._release_is_safe_batch_candidate(anime.media_id, scored) is False


def test_production_search_rejects_unrelated_title_even_if_provider_returns_it() -> None:
    from pudge.providers.nyaa import search_ranked

    anime = LibraryAnime(
        media_id=16498,
        title="Shingeki no Kyojin",
        titles=["Attack on Titan"],
        episodes=25,
        format="TV",
    )
    wrong = _release(
        "[NoobSubs] Kimetsu no Yaiba - 02 [720p]",
        seeders=100,
        size_gib=0.7,
        is_batch=False,
        info_hash="kimetsu-for-aot",
    )

    class FakeClient:
        def search(self, _query: str) -> list[NyaaRelease]:
            return [wrong]

    ranked = search_ranked(
        FakeClient(),
        anime,
        episode=2,
        batch=False,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="720p",
        min_seeders=1,
        target_episode_min_bytes=40 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )
    assert ranked == []


def test_shared_identity_guard_rejects_compact_multi_season_ranges(tmp_path: Path) -> None:
    from pudge.providers.nyaa import (
        release_identity_mismatch_reason,
        release_is_safe_batch_candidate,
    )

    anime = LibraryAnime(
        media_id=16498,
        title="Shingeki no Kyojin",
        titles=["Attack on Titan"],
        episodes=25,
        format="TV",
    )
    titles = [
        "[Trix] Shingeki no Kyojin S01-04 + OADs (Complete Series) [720p]",
        "[Trix] Attack on Titan S1-S4 Complete [720p]",
        "[Trix] Attack on Titan Season 1-4 Complete [720p]",
        "[Trix] Attack on Titan S01~S04 Complete [720p]",
    ]
    for index, title in enumerate(titles):
        item = _release(
            title,
            seeders=19,
            size_gib=17.2,
            is_batch=True,
            info_hash=f"multi-season-{index}",
        )
        assert release_identity_mismatch_reason(anime, title) == "cross-season-multi"
        assert release_is_safe_batch_candidate(anime, item) is False


def test_episode_range_is_not_mistaken_for_season_range() -> None:
    from pudge.providers.nyaa import release_identity_mismatch_reason

    anime = LibraryAnime(
        media_id=20755,
        title="Ansatsu Kyoushitsu",
        titles=["Assassination Classroom"],
        episodes=22,
        format="TV",
    )
    assert (
        release_identity_mismatch_reason(
            anime, "[HorribleSubs] Assassination Classroom (01-22) [1080p] (Batch)"
        )
        is None
    )


def test_production_search_rejects_named_related_kimetsu_sequel_arcs() -> None:
    from pudge.providers.nyaa import search_ranked

    anime = LibraryAnime(
        media_id=101922,
        title="Kimetsu no Yaiba",
        titles=["Demon Slayer: Kimetsu no Yaiba"],
        episodes=26,
        format="TV",
    )
    related_titles = (
        "Kimetsu no Yaiba: Mugen Ressha-hen (TV)",
        "Kimetsu no Yaiba: Yuukaku-hen",
        "Kimetsu no Yaiba: Katanakaji no Sato-hen",
        "Kimetsu no Yaiba: Hashira Geiko-hen",
    )
    wrong_titles = [
        "[Erai-raws] Kimetsu no Yaiba - Mugen Ressha Hen (TV) - 05 [720p][Multiple Subtitle]",
        "[SubsPlease] Kimetsu no Yaiba - Yuukaku-hen - 05 (720p)",
        "[Erai-raws] Kimetsu no Yaiba - Katanakaji no Sato Hen - 05 [720p]",
        "[SubsPlease] Kimetsu no Yaiba - Hashira Geiko-hen - 05 (480p)",
    ]

    class FakeClient:
        def search(self, _query: str) -> list[NyaaRelease]:
            return [
                _release(
                    title,
                    seeders=20,
                    size_gib=0.5,
                    is_batch=False,
                    info_hash=f"wrong-{index}",
                )
                for index, title in enumerate(wrong_titles)
            ]

    ranked = search_ranked(
        FakeClient(),
        anime,
        episode=5,
        batch=False,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="720p",
        min_seeders=1,
        target_episode_min_bytes=40 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
        negative_titles=related_titles,
    )
    assert ranked == []


def test_manager_cached_relation_graph_supplies_full_negative_franchise_titles(
    tmp_path: Path, monkeypatch
) -> None:
    anime = LibraryAnime(
        media_id=101922,
        title="Kimetsu no Yaiba",
        titles=["Demon Slayer: Kimetsu no Yaiba"],
        episodes=26,
        format="TV",
    )
    manager = _manager(tmp_path)
    manager.db.upsert_anime(anime)
    monkeypatch.setattr(
        manager.db,
        "relation_graph_for_media",
        lambda _media_id: {
            "graph": {
                "nodes": [
                    {"media_id": 101922, "title": "Kimetsu no Yaiba"},
                    {"media_id": 129874, "title": "Kimetsu no Yaiba: Mugen Ressha-hen (TV)"},
                    {"media_id": 142329, "title": "Kimetsu no Yaiba: Yuukaku-hen"},
                    {"media_id": 145139, "title": "Kimetsu no Yaiba: Katanakaji no Sato-hen"},
                    {"media_id": 166240, "title": "Kimetsu no Yaiba: Hashira Geiko-hen"},
                ]
            }
        },
    )

    negatives = manager._release_negative_titles_from_graph(anime)
    assert "Kimetsu no Yaiba: Hashira Geiko-hen" in negatives
    assert "Kimetsu no Yaiba" not in negatives
