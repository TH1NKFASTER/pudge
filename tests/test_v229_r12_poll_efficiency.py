from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.web_app import WebAppApi


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def make_poll_idle(api: WebAppApi, monkeypatch) -> None:
    monkeypatch.setattr(api.manager, "scan_subtitle_inbox", lambda: {"requeued": 0})
    monkeypatch.setattr(api.manager, "cleanup_qbittorrent_tags", lambda: {})
    monkeypatch.setattr(api.manager.db, "subtitle_jobs", lambda: [])
    monkeypatch.setattr(api.manager.db, "priority_subtitle_job_count", lambda **_kwargs: 0)
    api.manager._last_missing_episode_rows = 0


def test_known_ui_version_omits_large_unchanged_state(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    version = api.manager.db.get_state("ui_state_version", "")
    monkeypatch.setattr(
        api,
        "get_state_fast",
        lambda: (_ for _ in ()).throw(
            AssertionError("unchanged poll must not serialize the full Home state")
        ),
    )

    payload = api._foreground_poll_state_payload(version)

    assert payload == {"ui_state_version": version, "state": None}


def test_stale_ui_version_returns_full_state(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    marker = {"ui_state_version": "9", "downloads": []}
    monkeypatch.setattr(api, "get_state_fast", lambda: marker)

    payload = api._foreground_poll_state_payload("8")

    assert payload["state"] is marker
    assert payload["ui_state_version"] == "9"


def test_legacy_poll_caller_still_receives_state(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    make_poll_idle(api, monkeypatch)
    marker = {"ui_state_version": "legacy", "downloads": []}
    monkeypatch.setattr(api, "get_state_fast", lambda: marker)
    monkeypatch.setattr(api.manager, "sync_downloads", lambda: 0)

    result = api._poll_downloads_and_subtitles_sync()

    assert result["state"] is marker


def test_idle_download_poll_uses_60_second_qbt_network_cadence(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    make_poll_idle(api, monkeypatch)
    calls = []
    monkeypatch.setattr(api.manager, "sync_downloads", lambda: calls.append("sync") or 0)
    api._last_foreground_qbt_sync = 100.0
    monkeypatch.setattr("pudge.web_app.time.monotonic", lambda: 120.0)
    version = api.manager.db.get_state("ui_state_version", "")

    result = api._poll_downloads_and_subtitles_sync(version, False)

    assert calls == []
    assert result["state"] is None


def test_active_download_keeps_15_second_qbt_network_cadence(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    make_poll_idle(api, monkeypatch)
    calls = []
    monkeypatch.setattr(api.manager, "sync_downloads", lambda: calls.append("sync") or 0)
    api._last_foreground_qbt_sync = 100.0
    monkeypatch.setattr("pudge.web_app.time.monotonic", lambda: 120.0)
    version = api.manager.db.get_state("ui_state_version", "")

    api._poll_downloads_and_subtitles_sync(version, True)

    assert calls == ["sync"]


def test_window_draws_the_state_a_background_check_sends_back(page) -> None:
    # The check answers with the whole state only when its version changed: an episode that
    # became ready meanwhile is drawn at once (not on the next full reload). The cadence in front, behind and with due
    # subtitles is in test_v0560_inactive_window_energy.py and test_v203_refresh_foreground_queue.py.
    result = page.run(
        r"""
        setPage('current'); await __tick(1500);
        return __text('#currentContent');
        """,
        before=r"""
        const fresh = {media_id: 9, title: 'Fresh Show', next_episode: 1, site_url: 'https://anilist.co/anime/9',
                       local: {state: 'ready', episode: 1, video_path: '/v/fresh-01.mkv'}};
        __api.poll_downloads_and_subtitles = async () => ({state: {...(await __api.get_state()), ui_state_version: '2', current: [fresh],
          library: [{media_id: 9, title: 'Fresh Show', episodes: [{episode: 1, state: 'ready', video_path: '/v/fresh-01.mkv'}]}],
          home: {new_ready: [fresh]}}});
        """,
        api={"startup_maintenance": {"running": False}},
    )
    assert "Fresh Show" in result.value
    assert result.calls_of("poll_downloads_and_subtitles")[0][:2] == ["1", False]
