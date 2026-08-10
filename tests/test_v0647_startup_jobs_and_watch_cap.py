from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig
from pudge.web_app import WebAppApi


def test_startup_uses_same_forced_subtitle_retry_pipeline_as_refresh() -> None:
    source = Path("pudge/manager.py").read_text(encoding="utf-8")
    start = source.index("def _run_startup_once_unlocked")
    end = source.index("def _clear_jimaku_api_cache", start)
    block = source[start:end]

    assert "force_subtitle_retry=True" in block
    assert "prioritize_release_search=True" in block
    assert "defer_subtitle_processing=True" in block
    assert "background_agent_enabled mode=startup" not in block


def test_watch_cap_is_exposed_next_to_percentage_in_web_settings() -> None:
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")

    threshold = html.index("s_anilist_threshold")
    cap = html.index("s_anilist_max_remaining")
    assert threshold < cap
    assert "Макс. минут до конца" in html
    assert "Max minutes before end" in html
    assert "anilist_max_remaining_minutes:Number(v('s_anilist_max_remaining'))" in html


def test_watch_cap_is_sent_to_mpv_tracker() -> None:
    source = Path("pudge/cli.py").read_text(encoding="utf-8")
    assert '"PUDGE_ANILIST_MAX_REMAINING_MINUTES"' in source
    assert "config.anilist.watched_max_remaining_minutes" in source


def test_lua_requires_both_percent_and_remaining_time() -> None:
    source = Path("pudge/mpv_scripts/pudge_anilist.lua").read_text(encoding="utf-8")

    assert "if not percent or percent < threshold * 100 then return end" in source
    assert "local remaining_seconds = math.max(0, duration - position)" in source
    assert "if remaining_seconds <= max_remaining_minutes * 60 then" in source


def test_web_settings_round_trip_watch_cap(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    api = WebAppApi.__new__(WebAppApi)
    api.config = cfg
    api.manager = type("Manager", (), {"config": cfg})()

    # _settings_payload only needs self.config.
    payload = WebAppApi._settings_payload(api)
    assert payload["anilist_max_remaining_minutes"] == 10.0
