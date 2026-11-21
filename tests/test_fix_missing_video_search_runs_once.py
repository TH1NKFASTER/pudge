"""The search after a forgotten missing video runs once, not on every foreground poll.

_last_missing_episode_rows was reset only inside sync_downloads, which the foreground
poll throttles to every 15/60 s; until then every poll (1-5 s apart) ran the whole
auto_search_current again (Nyaa searches for every CURRENT title).
"""

from __future__ import annotations

from test_web_app import make_api


def test_missing_rows_trigger_one_auto_search(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    manager = api.manager
    calls = {"sync": 0, "auto": 0}

    def fake_sync():
        calls["sync"] += 1
        manager._last_missing_episode_rows = 1 if calls["sync"] == 1 else 0
        return 0

    def fake_auto():
        calls["auto"] += 1
        return 0

    monkeypatch.setattr(manager, "sync_downloads", fake_sync)
    monkeypatch.setattr(manager, "auto_search_current", fake_auto)
    monkeypatch.setattr(manager, "reconcile_completed_download_rows", lambda: 0)
    monkeypatch.setattr(manager, "reconcile_prepared_subtitle_rows", lambda: 0)
    monkeypatch.setattr(manager, "downloads_configured", lambda: True)
    for _poll in range(5):
        api._poll_downloads_and_subtitles_sync("", True, True)
    assert calls["sync"] >= 1
    assert calls["auto"] == 1
