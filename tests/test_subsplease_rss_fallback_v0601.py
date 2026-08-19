from __future__ import annotations

import logging
from pathlib import Path

import pytest

from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers.nyaa import (
    NyaaError,
    SubsPleaseClient,
    parse_subsplease_rss,
    search_subsplease_ranked,
)


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


def _rss() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title><![CDATA[[SubsPlease] Youjo Senki II - 05 (1080p) [CA0C3A4F].mkv]]></title>
    <link>magnet:?xt=urn:btih:0123456789ABCDEF0123456789ABCDEF01234567&amp;dn=Youjo</link>
    <pubDate>Wed, 05 Aug 2026 12:00:00 +0000</pubDate>
  </item>
  <item>
    <title><![CDATA[[SubsPlease] Unrelated Anime - 05 (1080p).mkv]]></title>
    <link>magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA</link>
    <pubDate>Wed, 05 Aug 2026 12:00:00 +0000</pubDate>
  </item>
</channel></rss>"""


def test_subsplease_parser_reads_magnet_and_marks_official_feed():
    releases = parse_subsplease_rss(_rss())

    assert len(releases) == 2
    first = releases[0]
    assert first.info_hash == "0123456789abcdef0123456789abcdef01234567"
    assert first.magnet.startswith("magnet:?xt=urn:btih:")
    assert first.group == "SubsPlease"
    assert first.trusted is True
    assert first.category_id == "subsplease-rss"
    assert first.seeders == 0


def test_subsplease_uses_official_magnet_feed_for_selected_resolution():
    assert SubsPleaseClient.feed_url("1080p") == "https://subsplease.org/rss/?r=1080"
    assert SubsPleaseClient.feed_url("720p") == "https://subsplease.org/rss/?r=720"
    assert SubsPleaseClient.feed_url("480p") == "https://subsplease.org/rss/?r=sd"


def test_subsplease_ranking_keeps_matching_episode_and_drops_unrelated_titles():
    class Client:
        def releases(self, preferred_resolution: str):
            assert preferred_resolution == "1080p"
            return parse_subsplease_rss(_rss())

    ranked = search_subsplease_ranked(
        Client(),
        _anime(),
        episode=5,
        batch=False,
        trusted_groups=["SubsPlease"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
    )

    assert len(ranked) == 1
    assert "Youjo Senki II - 05" in ranked[0].title
    assert ranked[0].score > 72
    assert "official-subsplease-rss" in ranked[0].reasons
    assert "seeders-unknown" in ranked[0].reasons


def test_manager_falls_back_to_subsplease_after_nyaa_error(monkeypatch):
    fallback_release = NyaaRelease(
        title="[SubsPlease] Youjo Senki II - 05 (1080p).mkv",
        link="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        torrent_url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        info_hash="0123456789abcdef0123456789abcdef01234567",
        size_text="",
        size_bytes=0,
        seeders=0,
        leechers=0,
        downloads=0,
        trusted=True,
        remake=False,
        category_id="subsplease-rss",
        score=150.0,
        reasons=["official-subsplease-rss", "seeders-unknown", "ep=5"],
        group="SubsPlease",
    )

    def fail_nyaa(*args, **kwargs):
        raise NyaaError("504 Gateway Timeout")

    def fallback(*args, **kwargs):
        return [fallback_release]

    monkeypatch.setattr("pudge.manager.search_ranked", fail_nyaa)
    monkeypatch.setattr("pudge.manager.search_subsplease_ranked", fallback)

    manager = AnimeManager.__new__(AnimeManager)
    manager.config = AppConfig()
    manager.config.nyaa.subsplease_rss_enabled = True
    manager.db = type("DB", (), {"get_anime": lambda self, media_id: _anime()})()
    manager.logger = logging.getLogger("test-subsplease-fallback")
    manager.log = lambda message: None
    manager._storage_can_accept = lambda size_bytes: True

    releases = manager.search_releases(135865, episode=5, batch=False)

    assert releases == [fallback_release]
    assert manager._release_is_allowed_for_auto(fallback_release) is True


def test_subsplease_setting_round_trips(tmp_path: Path):
    path = tmp_path / "config.toml"
    config = AppConfig(config_path=path)
    config.nyaa.subsplease_rss_enabled = True

    write_config(config, path)
    loaded = load_config(path)

    assert loaded.nyaa.subsplease_rss_enabled is True
    assert "subsplease_rss_enabled = true" in path.read_text()


def test_settings_ui_hides_obsolete_subsplease_checkbox():
    html = Path("pudge/web/index.html").read_text()

    assert "checkbox('s_subsplease_rss',t('settings.useSubsPleaseRss'))" not in html
    assert "subsplease_rss_enabled:c('s_subsplease_rss')" not in html
