from __future__ import annotations

import logging
from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease


def _anime() -> LibraryAnime:
    return LibraryAnime(
        media_id=135865,
        title="Youjo Senki II",
        titles=["Youjo Senki II", "Saga of Tanya the Evil Season 2"],
        synonyms=["Youjo Senki 2"],
        episodes=12,
        format="TV",
        duration=24,
    )


def _release(source: str, *, score: float = 150.0) -> NyaaRelease:
    is_rss = source == "rss"
    info_hash = ("1" if is_rss else "2") * 40
    return NyaaRelease(
        title=(
            "[SubsPlease] Youjo Senki II - 05 (1080p).mkv"
            if is_rss
            else "[Erai-raws] Youjo Senki II - 05 [1080p].mkv"
        ),
        link=f"magnet:?xt=urn:btih:{info_hash}",
        torrent_url=f"magnet:?xt=urn:btih:{info_hash}",
        info_hash=info_hash,
        size_text="",
        size_bytes=0,
        seeders=0 if is_rss else 20,
        leechers=0,
        downloads=0,
        trusted=True,
        remake=False,
        category_id="subsplease-rss" if is_rss else "1_2",
        score=score,
        reasons=["official-subsplease-rss", "ep=5"] if is_rss else ["ep=5"],
        group="SubsPlease" if is_rss else "Erai-raws",
    )


def _manager() -> AnimeManager:
    manager = AnimeManager.__new__(AnimeManager)
    manager.config = AppConfig()
    manager.config.nyaa.subsplease_rss_enabled = True
    manager.config.nyaa.subsplease_rss_preferred = True
    manager.db = type("DB", (), {"get_anime": lambda self, media_id: _anime()})()
    manager.logger = logging.getLogger("test-subsplease-preference")
    manager.log = lambda message: None
    manager._storage_can_accept = lambda size_bytes: True
    return manager


def test_preferred_subsplease_skips_nyaa_when_rss_has_suitable_release(monkeypatch):
    calls: list[str] = []
    rss_release = _release("rss")

    def rss(*args, **kwargs):
        calls.append("rss")
        return [rss_release]

    def nyaa(*args, **kwargs):
        calls.append("nyaa")
        return [_release("nyaa")]

    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", rss)
    monkeypatch.setattr("pudge.manager.search_ranked", nyaa)

    releases = _manager().search_releases(135865, episode=5)

    assert calls == ["rss"]
    assert releases == [rss_release]


def test_preferred_subsplease_falls_back_to_nyaa_when_rss_has_no_suitable_release(monkeypatch):
    calls: list[str] = []
    rss_release = _release("rss", score=10.0)
    nyaa_release = _release("nyaa", score=140.0)

    def rss(*args, **kwargs):
        calls.append("rss")
        return [rss_release]

    def nyaa(*args, **kwargs):
        calls.append("nyaa")
        return [nyaa_release]

    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", rss)
    monkeypatch.setattr("pudge.manager.search_ranked", nyaa)

    releases = _manager().search_releases(135865, episode=5)

    assert calls == ["rss", "nyaa"]
    assert releases[0] == nyaa_release
    assert rss_release in releases


def test_preference_setting_round_trips(tmp_path: Path):
    path = tmp_path / "config.toml"
    config = AppConfig(config_path=path)
    config.nyaa.subsplease_rss_enabled = True
    config.nyaa.subsplease_rss_preferred = True

    write_config(config, path)
    loaded = load_config(path)

    assert loaded.nyaa.subsplease_rss_preferred is True
    assert "subsplease_rss_preferred = true" in path.read_text()


def test_settings_ui_exposes_rss_first_checkbox():
    html = Path("pudge/web/index.html").read_text()

    assert "settings.preferSubsPleaseRss':'Prefer SubsPlease RSS before Nyaa'" in html
    assert "settings.preferSubsPleaseRss':'Сначала использовать RSS SubsPlease'" in html
    assert "checkbox('s_subsplease_preferred',t('settings.preferSubsPleaseRss'))" in html
    assert "subsplease_rss_preferred:c('s_subsplease_preferred')" in html
    assert "preferred.disabled=!rss.checked" in html


def test_default_order_keeps_nyaa_first_and_skips_rss_when_nyaa_is_suitable(monkeypatch):
    calls: list[str] = []
    nyaa_release = _release("nyaa")

    def rss(*args, **kwargs):
        calls.append("rss")
        return [_release("rss")]

    def nyaa(*args, **kwargs):
        calls.append("nyaa")
        return [nyaa_release]

    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", rss)
    monkeypatch.setattr("pudge.manager.search_ranked", nyaa)

    manager = _manager()
    manager.config.nyaa.subsplease_rss_preferred = False
    releases = manager.search_releases(135865, episode=5)

    assert calls == ["nyaa"]
    assert releases == [nyaa_release]


def test_preferred_rss_error_falls_back_to_nyaa(monkeypatch):
    from pudge.providers.nyaa import NyaaError

    calls: list[str] = []
    nyaa_release = _release("nyaa")

    def rss(*args, **kwargs):
        calls.append("rss")
        raise NyaaError("RSS unavailable")

    def nyaa(*args, **kwargs):
        calls.append("nyaa")
        return [nyaa_release]

    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", rss)
    monkeypatch.setattr("pudge.manager.search_ranked", nyaa)

    releases = _manager().search_releases(135865, episode=5)

    assert calls == ["rss", "nyaa"]
    assert releases == [nyaa_release]
