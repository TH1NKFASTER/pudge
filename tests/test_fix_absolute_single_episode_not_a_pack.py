"""A season-pack search does not mistake a single episode numbered across seasons
(S03E49 = season 3 episode 1) for a pack: it is penalized like any single episode."""

from __future__ import annotations

from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers.nyaa import score_release


def _release(title: str, size_gib: float) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash="b" * 40,
        size_text=f"{size_gib} GiB",
        size_bytes=int(size_gib * 1024**3),
        seeders=600,
        leechers=10,
        downloads=2000,
        trusted=False,
        remake=False,
        group="VARYG",
    )


def _score(release: NyaaRelease) -> NyaaRelease:
    anime = LibraryAnime(
        media_id=195516,
        title="Kusuriya no Hitorigoto 3rd Season",
        titles=["Kusuriya no Hitorigoto 3rd Season", "The Apothecary Diaries Season 3"],
        episodes=24,
    )
    return score_release(
        release,
        anime,
        episode=None,
        batch=True,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )


def test_an_absolute_numbered_single_episode_is_not_a_pack() -> None:
    single = _score(_release("The Apothecary Diaries S03E49 1080p CR WEB-DL AAC2.0 H.264-VARYG", 1.4))
    assert any(reason.startswith("single-episode=") for reason in single.reasons)
    assert "not-a-pack" in single.reasons


def test_a_real_season_pack_still_counts_as_one() -> None:
    pack = _score(_release("The Apothecary Diaries S03 1080p CR WEB-DL AAC2.0 H.264-VARYG", 30.0))
    assert not any(reason.startswith("single-episode=") for reason in pack.reasons)
