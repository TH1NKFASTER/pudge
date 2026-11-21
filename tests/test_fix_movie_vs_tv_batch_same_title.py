"""A movie never takes a TV batch that shares its title (fx118 review).

AniList has the movie "Kimetsu no Yaiba: Mugen Ressha-hen" and a TV version of the same name. Nyaa lists
"[SubsPlease] Kimetsu no Yaiba - Mugen Ressha-hen (01-07) (1080p) [Batch]": for a one-unit entry a 1-7
range used to count as "the whole series" (+24) and beat the movie's BD release, so Planning Auto-download
and the final-episode prompt added the TV batch under the movie's id. A range past a one-unit entry is
another entry."""

from __future__ import annotations

from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers.nyaa import score_release

MOVIE = LibraryAnime(
    media_id=112151,
    title="Kimetsu no Yaiba: Mugen Ressha-hen",
    titles=["Kimetsu no Yaiba: Mugen Ressha-hen", "Demon Slayer: Kimetsu no Yaiba - The Movie: Mugen Train"],
    episodes=1,
    format="MOVIE",
)


def _release(
    title: str, *, size_gib: float, is_batch: bool, info_hash: str, seeders: int = 120
) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link=f"https://releases.invalid/view/{info_hash}",
        torrent_url=f"https://releases.invalid/download/{info_hash}.torrent",
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


def _score(item: NyaaRelease, anime: LibraryAnime = MOVIE) -> NyaaRelease:
    return score_release(
        item,
        anime,
        episode=None,
        batch=True,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=12000 * 1024 * 1024,
        preferred_sources=["BluRay", "WEB-DL", "WEBRip"],
    )


def test_the_movie_release_beats_a_tv_batch_with_the_same_title() -> None:
    tv = _score(
        _release(
            "[SubsPlease] Kimetsu no Yaiba - Mugen Ressha-hen (01-07) (1080p) [Batch]",
            size_gib=9.5,
            is_batch=True,
            info_hash="a" * 40,
            seeders=300,
        )
    )
    movie = _score(
        _release(
            "[Judas] Kimetsu no Yaiba Movie - Mugen Ressha-hen (BD 1080p) [HEVC]",
            size_gib=4.2,
            is_batch=False,
            info_hash="b" * 40,
            seeders=80,
        )
    )
    assert "range-exceeds-entry" in tv.reasons
    assert movie.score > tv.score, (movie.score, movie.reasons, tv.score, tv.reasons)


def test_a_series_full_range_batch_keeps_its_bonus() -> None:
    series = LibraryAnime(
        media_id=1, title="Bocchi the Rock!", titles=["Bocchi the Rock!"], episodes=12, format="TV"
    )
    batch = _score(
        _release(
            "[Judas] Bocchi the Rock! (01-12) (BD 1080p) [Batch]",
            size_gib=14.0,
            is_batch=True,
            info_hash="c" * 40,
        ),
        series,
    )
    assert "full-series-range" in batch.reasons and "range-exceeds-entry" not in batch.reasons


def test_a_one_unit_range_still_counts_for_a_movie() -> None:
    single = _score(
        _release(
            "[Group] Kimetsu no Yaiba - Mugen Ressha-hen (01) (BD 1080p)",
            size_gib=4.0,
            is_batch=False,
            info_hash="d" * 40,
        )
    )
    assert "range-exceeds-entry" not in single.reasons
