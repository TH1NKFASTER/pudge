from __future__ import annotations

import json
from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import NyaaRelease
from pudge.web_app import WebAppApi
from tests.i18n_support import en, ru
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario


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


TRUSTED = """
const box = $('s_only_trusted_groups');
const out = {label: box.closest('label').innerText.trim(), expected: t('settings.onlyTrustedGroups'), checked: box.checked};
await __input('#s_only_trusted_groups', true);
await __click('#saveSettings');
await __tick(10);
return out;
"""


def test_only_trusted_groups_is_exposed_and_saved_by_web_settings(tmp_path: Path, page) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    settings = json.loads(json.dumps(api._settings_payload(), default=str))
    assert settings["only_trusted_groups"] is False
    assert ru("settings.onlyTrustedGroups") == "Только доверенные группы для автоскачивания"
    assert en("settings.onlyTrustedGroups") == "Only trusted groups for automatic downloads"

    # The user ticks "Only trusted groups" in Settings and saves; the backend gets it.
    for lang in ("en", "ru"):
        result = page.run(
            TRUSTED,
            lang=lang,
            state={"settings": {**settings, "language": lang}},
            before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
        )
        assert result.value["label"] == result.value["expected"]
        assert result.value["checked"] is False
        [[values]] = result.calls_of("save_settings")
        assert values["only_trusted_groups"] is True
    saved = api.save_settings(values)
    assert saved["settings"]["only_trusted_groups"] is True
    assert api.config.nyaa.only_trusted_groups is True
    assert load_config(cfg.config_path).nyaa.only_trusted_groups is True


@needs_lua
def test_auto_tracker_has_no_startup_future_completion_osd(tmp_path: Path) -> None:
    # Opening a tracked episode used to announce "AniList: after 85.0% and within 10.0 min
    # of the end"; automatic counting is silent now and only logs its rule.
    out = run_mpv_scenario(
        tmp_path,
        """
        sim.set('duration', 1440); sim.fire('file-loaded'); sim.play(5)
        for _, row in ipairs(sim.log) do
          if row.kind == 'osd' then print('OSD:' .. row.value) end
          if row.kind == 'msg.info' then print('LOG:' .. row.value) end
        end
    """,
        scripts=[ANILIST],
        env={
            "PUDGE_ANILIST_TRACKING_FILE": str(tmp_path / "tracking.json"),
            "PUDGE_ANILIST_AUTO_UPDATE": "1",
            "PUDGE_ANILIST_MEDIA_ID": "7",
            "PUDGE_ANILIST_THRESHOLD": "0.85",
            "PUDGE_ANILIST_MAX_REMAINING_MINUTES": "10",
        },
    )
    assert [line for line in out.splitlines() if line.startswith("OSD:")] == []
    assert (
        "LOG:AniList tracker loaded: media_id=7 auto=true threshold=85.0% max_remaining=10.0min"
        in out.splitlines()
    )
