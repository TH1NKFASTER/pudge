from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.energy_diagnostics import EnergyDiagnosticsMonitor
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.providers.nyaa import SubsPleaseClient, _quality_score
from pudge.web_app import WebAppApi

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
LUA = ROOT / "pudge" / "mpv_scripts" / "pudge_anilist.lua"


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def bleach_graph() -> dict:
    return {
        "root_id": 5,
        "nodes": [
            {"media_id": 1, "title": "BLEACH", "format": "TV", "episodes": 366, "start_date": "2004-10-05"},
            {"media_id": 2, "title": "BLEACH: Sennen Kessen-hen", "format": "TV", "episodes": 13, "start_date": "2022-10-11"},
            {"media_id": 3, "title": "BLEACH: Sennen Kessen-hen - Ketsubetsu-tan", "format": "TV", "episodes": 13, "start_date": "2023-07-08"},
            {"media_id": 4, "title": "BLEACH: Sennen Kessen-hen - Soukoku-tan", "format": "TV", "episodes": 14, "start_date": "2024-10-05"},
            {"media_id": 5, "title": "BLEACH: Sennen Kessen-hen - Kashin-tan", "format": "TV", "episodes": 13, "start_date": "2026-07-25"},
        ],
        "edges": [
            {"source": 1, "target": 2, "relation_type": "SEQUEL"},
            {"source": 2, "target": 3, "relation_type": "SEQUEL"},
            {"source": 3, "target": 4, "relation_type": "SEQUEL"},
            {"source": 4, "target": 5, "relation_type": "SEQUEL"},
        ],
    }


def test_polychrome_is_50_percent_slower_and_does_not_double_restart() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "polychrome-flow 1.875s ease-out 1 both" in html
    assert "polychrome-glint 1.425s ease-out 1 both" in html
    assert "if(cover.classList.contains('polychrome-wake')||cover.classList.contains('polychrome-hover-wake'))return false" in html
    assert "setTimeout(wakePolychromeAnimations,180)" not in html
    assert "requestAnimationFrame(()=>" in html


def test_polychrome_hover_has_delegated_rerender_safe_fallback() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert "function startPolychromeHover(cover)" in html
    assert "document.addEventListener('pointerover'" in html
    assert "document.addEventListener('pointermove'" in html
    assert "document.addEventListener('pointerout'" in html
    assert "polychromeHoverActive" in html


def test_auto_watch_is_enabled_by_default_and_groups_threshold_fields(tmp_path: Path) -> None:
    cfg = AppConfig()
    assert cfg.anilist.auto_update_progress is True
    path = tmp_path / "config.toml"
    write_config(cfg, path)
    assert load_config(path).anilist.auto_update_progress is True

    html = HTML.read_text(encoding="utf-8")
    assert "settings.autoProgress':'Automatically mark episode watched'" in html
    assert "settings.autoProgress':'Автоматически засчитывать серию'" in html
    assert 'id="settings-anilist-auto-fields"' in html
    assert "s_anilist_threshold" in html
    assert "s_anilist_max_remaining" in html


def test_only_mpv_shortcuts_are_configurable_and_app_shortcuts_are_standard(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.shortcuts.mpv_mark_watched = "Ctrl+w"
    path = tmp_path / "config.toml"
    write_config(cfg, path)
    loaded = load_config(path)
    assert loaded.shortcuts.mpv_mark_watched == "Ctrl+w"
    assert not hasattr(loaded.shortcuts, "app_watching")

    html = HTML.read_text(encoding="utf-8")
    assert "settings.shortcuts':'Shortcuts'" in html
    assert "shortcut-recorder" in html
    assert "capturedShortcut(event)" in html
    assert "shortcut_app_watching" not in html
    assert "shortcut_app_planning_search" not in html
    assert "document.querySelectorAll('.nav button[data-page]')" in html
    assert "String(event.key||'').toLowerCase()==='f'" in html
    assert "symbols={cmd:'⌘',command:'⌘',meta:'⌘',ctrl:'⌃',control:'⌃',alt:'⌥',option:'⌥',opt:'⌥',shift:'⇧'}" in html
    assert "shortcut_mpv_mark_watched" in html

    lua = LUA.read_text(encoding="utf-8")
    assert "PUDGE_SHORTCUT_MARK_WATCHED" in lua
    assert "PUDGE_SHORTCUT_OPEN_ANILIST" in lua
    assert "PUDGE_SHORTCUT_CORRECT_MATCH" in lua
    assert "add_reliable_binding(shortcut_mark_watched" in lua


def test_library_uses_relative_episode_number_and_singular_label(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    anime = LibraryAnime(
        media_id=5,
        title="BLEACH: Sennen Kessen-hen - Kashin-tan",
        status="CURRENT",
        progress=2,
        episodes=13,
        format="TV",
        media_status="RELEASING",
    )
    api.manager.db.upsert_anime(anime)
    api.manager.db.store_relation_graph(bleach_graph(), refreshed_at=1.0, next_refresh_at=9999999999.0)
    video = tmp_path / "bleach-43.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(media_id=5, title=anime.title, episode=43, video_path=video, state="waiting_subtitles")
    )

    library = api._library_payloads({5: anime})
    assert library[0]["episodes"][0]["episode"] == 3
    assert library[0]["episodes"][0]["stored_episode"] == 43

    payload = api._anime_payload(anime)
    assert payload["local"] is not None
    assert payload["local"]["episode"] == 3
    assert payload["local"]["state"] == "waiting_subtitles"

    html = HTML.read_text(encoding="utf-8")
    assert "label.libraryRangeSingle':'Episode: {episode}'" in html
    assert "numbered.length===1?t('label.libraryRangeSingle'" in html


def test_highest_resolution_prefers_higher_standard_resolution() -> None:
    score_2160, _ = _quality_score("Anime 2160p WEB-DL", "highest")
    score_1440, _ = _quality_score("Anime 1440p WEB-DL", "highest")
    score_1080, _ = _quality_score("Anime 1080p WEB-DL", "highest")
    score_720, _ = _quality_score("Anime 720p WEB-DL", "highest")
    assert score_2160 > score_1440 > score_1080 > score_720
    assert SubsPleaseClient.feed_url("highest").endswith("?r=1080")

    html = HTML.read_text(encoding="utf-8")
    for value in ("480p", "720p", "1080p", "1440p", "2160p", "highest"):
        assert f'value="{value}"' in html


def test_energy_diagnostics_is_opt_in_and_collects_related_process_activity(monkeypatch) -> None:
    rows = [
        {"pid": 100, "ppid": 1, "cpu_percent": 4.0, "memory_percent": 1.0, "rss_mb": 50.0, "elapsed": "1:00", "command": "pudge"},
        {"pid": 101, "ppid": 100, "cpu_percent": 6.0, "memory_percent": 2.0, "rss_mb": 70.0, "elapsed": "0:40", "command": "WebKit WebContent"},
        {"pid": 202, "ppid": 1, "cpu_percent": 10.0, "memory_percent": 3.0, "rss_mb": 100.0, "elapsed": "4:00", "command": "/opt/homebrew/bin/mpv /tmp/a.mkv"},
    ]
    monkeypatch.setattr("pudge.energy_diagnostics.os.getpid", lambda: 100)
    monkeypatch.setattr(EnergyDiagnosticsMonitor, "_process_rows", staticmethod(lambda: rows))
    monitor = EnergyDiagnosticsMonitor(interval_seconds=30)
    sample = monitor.sample()
    assert sample["energy_metric"] == "cpu_activity_proxy"
    assert sample["related_cpu_percent"] == 20.0
    assert {row["pid"] for row in sample["processes"]} == {100, 101, 202}

    html = HTML.read_text(encoding="utf-8")
    assert "settings.energyDiagnostics':'Energy diagnostics'" in html
    assert "openEnergyLog" in html
