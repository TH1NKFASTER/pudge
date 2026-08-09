from __future__ import annotations

from pathlib import Path

from anime_mpv.config import AppConfig, load_config, write_config
from anime_mpv.manager import AnimeManager
from anime_mpv.manager_models import NyaaRelease
from anime_mpv.web_app import WebAppApi


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.nyaa.auto_require_trusted = True
    cfg.nyaa.trusted_groups = ["Erai-raws", "SubsPlease"]
    return AnimeManager(cfg, log=lambda _message: None)


def _strong_untrusted() -> NyaaRelease:
    return NyaaRelease(
        title="[AnoZu] Bleach S17E43 REPACK 1080p WEB-DL",
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash="a" * 40,
        size_text="1006.8 MiB",
        size_bytes=1007 * 1024 * 1024,
        seeders=482,
        leechers=1,
        downloads=729,
        trusted=False,
        remake=False,
        group="AnoZu",
        score=176.2,
        reasons=[
            "title=100",
            "exact-title-phrase",
            "wrong-season=17",
            "absolute-ep=43",
            "relative-ep=3",
            "size-floor-ok",
        ],
    )


def test_only_trusted_groups_blocks_strong_untrusted_fallback(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    release = _strong_untrusted()
    assert manager._release_is_allowed_for_auto(release) is True

    manager.config.nyaa.only_trusted_groups = True
    assert manager._release_is_allowed_for_auto(release) is False


def test_only_trusted_groups_still_allows_trusted_release(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    release = _strong_untrusted()
    release.group = "Erai-raws"
    manager.config.nyaa.only_trusted_groups = True
    assert manager._release_is_allowed_for_auto(release) is True



def test_only_trusted_groups_uses_configured_group_list_not_nyaa_trusted_flag(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    release = _strong_untrusted()
    release.trusted = True
    manager.config.nyaa.only_trusted_groups = True
    assert manager._release_is_trusted(release) is True
    assert manager._release_is_allowed_for_auto(release) is False

def test_only_trusted_groups_defaults_off_and_round_trips_config(tmp_path: Path) -> None:
    cfg = AppConfig()
    assert cfg.nyaa.only_trusted_groups is False
    cfg.nyaa.only_trusted_groups = True
    cfg.config_path = tmp_path / "config.toml"
    write_config(cfg, cfg.config_path)
    loaded = load_config(cfg.config_path)
    assert loaded.nyaa.only_trusted_groups is True


def test_only_trusted_groups_is_exposed_and_saved_by_web_settings(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)

    assert api._settings_payload()["only_trusted_groups"] is False
    result = api.save_settings({"only_trusted_groups": True})
    assert result["settings"]["only_trusted_groups"] is True
    assert api.config.nyaa.only_trusted_groups is True
    assert load_config(cfg.config_path).nyaa.only_trusted_groups is True

    html = Path("anime_mpv/web/index.html").read_text(encoding="utf-8")
    assert "s_only_trusted_groups" in html
    assert "Только доверенные группы для автоскачивания" in html
    assert "Only trusted groups for automatic downloads" in html
    assert "only_trusted_groups:c('s_only_trusted_groups')" in html


def test_auto_tracker_has_no_startup_future_completion_osd() -> None:
    lua = Path("anime_mpv/mpv_scripts/anime_mpv_anilist.lua").read_text(encoding="utf-8")
    assert "after %.1f%% and within %.1f min of the end" not in lua
    assert "после %.1f%% и не раньше чем за %.1f мин до конца" not in lua
    assert "AniList tracker loaded:" in lua
