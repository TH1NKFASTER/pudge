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
