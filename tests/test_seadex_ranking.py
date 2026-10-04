from __future__ import annotations

import logging

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers.seadex import SeaDexRecommendation


def _anime():
    return LibraryAnime(media_id=135865, title="Youjo Senki II", titles=["Youjo Senki II"], episodes=12, format="TV", duration=24)


def _release(source, info_hash, score):
    return NyaaRelease(
        title=f"[{source}] Youjo Senki II - 05 (1080p).mkv", link="", torrent_url="", info_hash=info_hash, size_text="",
        size_bytes=0, seeders=0 if source == "SubsPlease" else 20, leechers=0, downloads=0, trusted=True, remake=False,
        category_id="subsplease-rss" if source == "SubsPlease" else "1_2", score=score,
        reasons=["official-subsplease-rss"] if source == "SubsPlease" else [], group=source,
    )


def _manager(rec):
    manager = AnimeManager.__new__(AnimeManager)
    manager.config = AppConfig()
    manager.db = type("DB", (), {"get_anime": lambda self, media_id: _anime()})()
    manager.logger = logging.getLogger("seadex")
    manager.log = lambda message: None
    manager._storage_can_accept = lambda size_bytes: True
    manager._seadex_for_search = lambda media_id, automatic: None if automatic else rec
    return manager


def test_recommended_nyaa_release_reaches_manual_results_despite_subsplease(monkeypatch):
    rec = SeaDexRecommendation(anilist_id=135865, status="found", preferred_hashes=frozenset({"a" * 40}))
    seen = {}

    def rss(*args, **kwargs):
        seen["rss"] = kwargs.get("seadex")
        return [_release("SubsPlease", "1" * 40, 150.0)]

    def nyaa(*args, **kwargs):
        seen["nyaa"] = kwargs.get("seadex")
        return [_release("Recommended", "a" * 40, 210.0)]

    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", rss)
    monkeypatch.setattr("pudge.manager.search_ranked", nyaa)
    releases = _manager(rec).search_releases(135865, episode=5)
    assert seen == {"rss": rec, "nyaa": rec}, "one recommendation object shared by every source"
    assert releases[0].group == "Recommended"


def test_without_seadex_entry_the_fast_rss_path_is_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", lambda *a, **k: calls.append(("rss", "seadex" in k)) or [_release("SubsPlease", "1" * 40, 150.0)])
    monkeypatch.setattr("pudge.manager.search_ranked", lambda *a, **k: calls.append(("nyaa", True)) or [])
    _manager(None).search_releases(135865, episode=5)
    assert calls == [("rss", False)]


def test_automatic_search_does_not_use_seadex_yet(monkeypatch):
    rec = SeaDexRecommendation(anilist_id=135865, status="found", preferred_hashes=frozenset({"a" * 40}))
    calls = []
    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", lambda *a, **k: calls.append(("rss", "seadex" in k)) or [_release("SubsPlease", "1" * 40, 150.0)])
    monkeypatch.setattr("pudge.manager.search_ranked", lambda *a, **k: calls.append(("nyaa", True)) or [])
    _manager(rec).search_releases(135865, episode=5, automatic=True)
    assert calls == [("rss", False)]


def test_real_lookup_is_skipped_when_disabled(monkeypatch, tmp_path):
    manager = AnimeManager.__new__(AnimeManager)
    manager.config = AppConfig()
    manager.config.paths.cache_dir = tmp_path
    manager.logger = logging.getLogger("seadex")
    manager.config.nyaa.seadex_enabled = False
    assert manager._seadex_for_search(1, automatic=False) is None
    manager.config.nyaa.seadex_enabled = True
    monkeypatch.setenv("PUDGE_SEADEX", "1")

    import pudge.providers.seadex as sd

    monkeypatch.setattr(sd, "_http_fetch", lambda url, params, timeout: (429, None))
    assert manager._seadex_for_search(1, automatic=False) is None, "429 keeps search working without SeaDex"


def test_seadex_settings_round_trip_and_old_config_defaults(tmp_path):
    from pudge.config import load_config, write_config

    path = tmp_path / "config.toml"
    config = AppConfig(config_path=path)
    config.nyaa.seadex_enabled = False
    config.nyaa.seadex_timeout_seconds = 2.5
    write_config(config, path)
    loaded = load_config(path)
    assert (loaded.nyaa.seadex_enabled, loaded.nyaa.seadex_timeout_seconds, loaded.nyaa.seadex_cache_hours) == (False, 2.5, 24.0)
    old = tmp_path / "old.toml"
    old.write_text("[nyaa]\nenabled = true\n", encoding="utf-8")
    assert load_config(old).nyaa.seadex_enabled is True
