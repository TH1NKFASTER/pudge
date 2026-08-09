from __future__ import annotations

from pathlib import Path

from anime_mpv.config import AppConfig
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import NyaaRelease


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.nyaa.auto_require_trusted = True
    cfg.nyaa.trusted_groups = ["Erai-raws", "SubsPlease", "NanakoRaws", "shincaps"]
    cfg.nyaa.min_release_score = 72.0
    cfg.nyaa.min_seeders = 1
    return AnimeManager(cfg, log=lambda _message: None)


def _release(*, score: float = 176.2, seeders: int = 482, reasons: list[str] | None = None) -> NyaaRelease:
    return NyaaRelease(
        title=(
            "[AnoZu] Bleach S17E43 REPACK 1080p DSNP WEB-DL AAC 2.0 H.264 | "
            "Bleach: Thousand-Year Blood War - The Calamity | "
            "Bleach: Sennen Kessen-hen - Kashin-tan"
        ),
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash="a" * 40,
        size_text="1006.8 MiB",
        size_bytes=1007 * 1024 * 1024,
        seeders=seeders,
        leechers=1,
        downloads=729,
        trusted=False,
        remake=False,
        group="AnoZu",
        score=score,
        reasons=reasons or [
            "title=100",
            "exact-title-phrase",
            "wrong-season=17",
            "absolute-ep=43",
            "relative-ep=3",
            "1080p",
            "WEB",
            "codec-preferred=AVC",
            "source-preferred=WEB-DL",
            "seeds=482",
            "age=0d",
            "size=1007MiB",
            "standard-episode-floor=800MiB",
            "size-floor-ok",
        ],
    )


def test_bleach_absolute_episode_can_auto_download_despite_release_season_number(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    release = _release()

    assert manager._release_uses_absolute_episode_alias(release) is True
    assert manager._release_is_allowed_for_auto(release) is True


def test_wrong_season_without_absolute_alias_is_still_blocked(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    release = _release(
        reasons=[
            "title=100",
            "exact-title-phrase",
            "wrong-season=17",
            "ep=3",
            "size-floor-ok",
        ]
    )

    assert manager._release_is_allowed_for_auto(release) is False


def test_untrusted_exception_requires_exceptionally_strong_exact_release(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    release = _release(score=79.8, seeders=89)

    assert manager._release_is_allowed_for_auto(release) is False


def test_untrusted_exception_requires_size_floor(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    release = _release()
    release.reasons = [reason for reason in release.reasons if reason != "size-floor-ok"] + [
        "very-low-bitrate-size"
    ]

    assert manager._release_is_allowed_for_auto(release) is False


def test_auto_search_current_actually_schedules_bleach_absolute_release(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    manager.config.nyaa.enabled = True
    manager.config.nyaa.auto_download_current = True
    manager.config.qbittorrent.enabled = True
    manager.db.upsert_anime(
        __import__("anime_mpv.manager_models", fromlist=["LibraryAnime"]).LibraryAnime(
            media_id=191562,
            title="BLEACH: Sennen Kessen-hen - Kashin-tan",
            status="CURRENT",
            progress=2,
            episodes=3,
        )
    )
    release = _release()
    searched: list[tuple[int, int | None, bool]] = []
    added: list[tuple[int, str, int | None, bool]] = []

    def fake_search(media_id: int, *, episode: int | None, batch: bool, automatic: bool = False):
        searched.append((media_id, episode, automatic))
        return [release]

    def fake_add(media_id: int, item: NyaaRelease, *, episode: int | None, batch: bool):
        added.append((media_id, item.info_hash, episode, batch))
        return item

    monkeypatch.setattr(manager, "search_releases", fake_search)
    monkeypatch.setattr(manager, "add_release", fake_add)

    assert manager.auto_search_current() == 1
    assert searched == [(191562, 3, True)]
    assert added == [(191562, "a" * 40, 3, False)]


def test_automatic_release_search_keeps_same_alias_count_as_find_episode(tmp_path: Path, monkeypatch) -> None:
    import anime_mpv.manager as manager_module
    from anime_mpv.manager_models import LibraryAnime

    manager = _manager(tmp_path)
    manager.config.nyaa.enabled = True
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=191562,
            title="BLEACH: Sennen Kessen-hen - Kashin-tan",
            status="CURRENT",
            progress=2,
            episodes=3,
        )
    )
    captured: dict[str, object] = {}

    class DummyClient:
        def close(self) -> None:
            pass

    monkeypatch.setattr(manager, "nyaa_client", lambda timeout=20.0: DummyClient())
    monkeypatch.setattr(manager, "_release_episode_context", lambda anime, episode: ((43,), ("BLEACH: Sennen Kessen-hen",)))

    def fake_search_ranked(client, anime, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(manager_module, "search_ranked", fake_search_ranked)

    manager.search_releases(191562, episode=3, batch=False, automatic=True)

    assert captured["max_queries"] == 5
    assert captured["query_budget_seconds"] == 18.0
