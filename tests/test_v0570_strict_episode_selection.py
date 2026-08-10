from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers.nyaa import release_episode, score_release, search_ranked


WRONG_TITLE = (
    "[Erai-raws] Kimi ga Shinu made Koi wo Shitai - 01 "
    "[1080p CR WEB-DL AVC AAC][MultiSub][0846EAB5]"
)


def release(title: str, info_hash: str = "hash") -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash=info_hash,
        size_text="1.2 GiB",
        size_bytes=int(1.2 * 1024**3),
        seeders=500,
        leechers=2,
        downloads=1000,
        trusted=True,
        remake=False,
        group="Erai-raws",
    )


def scoring_kwargs(anime: LibraryAnime) -> dict:
    return dict(
        anime=anime,
        episode=5,
        batch=False,
        trusted_groups=["Erai-raws"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )


def test_explicit_episode_01_is_never_valid_for_episode_05() -> None:
    anime = LibraryAnime(
        media_id=1,
        title="Kimi ga Shinu made Koi wo Shitai",
        titles=["Kimi ga Shinu made Koi wo Shitai"],
        episodes=12,
    )
    item = release(WRONG_TITLE)

    assert release_episode(item.title) == 1
    scored = score_release(item, **scoring_kwargs(anime))

    assert "wrong-episode" in scored.reasons
    assert "found-episode=1" in scored.reasons
    assert scored.score < 0


def test_search_ranked_drops_explicit_wrong_episode_before_ranking() -> None:
    anime = LibraryAnime(
        media_id=1,
        title="Kimi ga Shinu made Koi wo Shitai",
        titles=["Kimi ga Shinu made Koi wo Shitai"],
        episodes=12,
    )

    class FakeClient:
        def search(self, _query: str) -> list[NyaaRelease]:
            return [release(WRONG_TITLE)]

    kwargs = scoring_kwargs(anime)
    kwargs.pop("anime")
    ranked = search_ranked(FakeClient(), anime, **kwargs)

    assert ranked == []


def test_exact_episode_05_still_survives_search() -> None:
    anime = LibraryAnime(
        media_id=1,
        title="Kimi ga Shinu made Koi wo Shitai",
        titles=["Kimi ga Shinu made Koi wo Shitai"],
        episodes=12,
    )
    correct = release(
        "[Erai-raws] Kimi ga Shinu made Koi wo Shitai - 05 "
        "[1080p CR WEB-DL AVC AAC][MultiSub][ABCDEF12]",
        "correct",
    )

    class FakeClient:
        def search(self, _query: str) -> list[NyaaRelease]:
            return [correct]

    kwargs = scoring_kwargs(anime)
    kwargs.pop("anime")
    ranked = search_ranked(FakeClient(), anime, **kwargs)

    assert len(ranked) == 1
    assert ranked[0].info_hash == "correct"
    assert "ep=5" in ranked[0].reasons


def test_automatic_download_rejects_release_without_episode_number(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.nyaa.min_release_score = -1000
    cfg.nyaa.min_seeders = 1
    manager = AnimeManager(cfg, log=lambda _message: None)

    item = release("[Erai-raws] Kimi ga Shinu made Koi wo Shitai [1080p][MultiSub]")
    scored = score_release(
        item,
        anime=LibraryAnime(media_id=1, title="Kimi ga Shinu made Koi wo Shitai"),
        episode=5,
        batch=False,
        trusted_groups=["Erai-raws"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024 * 1024,
        target_episode_max_bytes=3500 * 1024 * 1024,
    )

    assert "episode-not-specified" in scored.reasons
    assert manager._release_is_allowed_for_auto(scored) is False
