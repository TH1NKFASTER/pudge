from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from pudge.notifications import send_native_notification
from pudge.providers.jimaku import JimakuClient, JimakuError
from pudge.providers.nyaa import NyaaError, search_ranked
from pudge.manager_models import LibraryAnime


def _anime() -> LibraryAnime:
    return LibraryAnime(
        media_id=1,
        title="Example Show",
        titles=["Example Show", "Example Show Alternate"],
        synonyms=[],
        episodes=12,
        format="TV",
    )


def _search_kwargs() -> dict[str, object]:
    return {
        "episode": 1,
        "batch": False,
        "trusted_groups": [],
        "preferred_groups": [],
        "blocked_groups": [],
        "preferred_resolution": "1080p",
        "min_seeders": 0,
        "target_episode_min_bytes": 0,
        "target_episode_max_bytes": 4 * 1024 * 1024 * 1024,
    }


def test_schedule_home_cards_use_normal_compact_card_state() -> None:
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert "personalScheduleBadge" not in html
    assert "personal-schedule-badge" not in html
    assert "function personalScheduleCardDateText(value)" not in html
    caught = html[html.index("function caughtUpHomeCard(a)"):html.index("function droppedHomeCard(a)")]
    assert "t('label.airingIn',{episode:Number(schedule.next_episode),time:formatRemaining(scheduledRemaining)})" in caught
    assert "next_unlock_local" not in caught
    ready = html[html.index("function readyHomeCard(a)"):html.index("function readySequenceCard(group)")]
    assert "personal_schedule" not in ready
    assert "personalSchedule" not in ready


def test_notification_helper_typeerror_is_best_effort_failure(monkeypatch, tmp_path: Path) -> None:
    helper = tmp_path / "pudge"
    helper.write_bytes(b"binary")
    helper.chmod(0o755)
    monkeypatch.setattr("pudge.notifications.platform.system", lambda: "Darwin")
    monkeypatch.setattr("pudge.notifications._notification_helper_path", lambda: helper)
    monkeypatch.setattr(
        "pudge.notifications.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TypeError("fake Popen lacks context manager")),
    )
    assert send_native_notification("Episode ready", "Example") is False


def test_update_unloads_agent_before_runtime_package_is_removed() -> None:
    source = Path("install.sh").read_text(encoding="utf-8")
    early = 'launchctl bootout "gui/$(id -u)" "$AGENT_PLIST"'
    destructive = 'rm -rf "$INSTALL_SITE_PACKAGES/pudge"'
    assert source.index(early) < source.index(destructive)


def test_automatic_nyaa_stops_after_first_failed_route_query() -> None:
    class Client:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def search(self, query: str):
            self.queries.append(query)
            raise NyaaError("The read operation timed out")

    client = Client()
    with pytest.raises(NyaaError, match="budget exhausted"):
        search_ranked(client, _anime(), query_budget_seconds=18.0, **_search_kwargs())
    assert len(client.queries) == 1


def test_manual_nyaa_still_tries_aliases_after_one_failure() -> None:
    class Client:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def search(self, query: str):
            self.queries.append(query)
            if len(self.queries) == 1:
                raise NyaaError("temporary failure")
            return []

    client = Client()
    with pytest.raises(NyaaError):
        search_ranked(client, _anime(), query_budget_seconds=None, max_queries=2, **_search_kwargs())
    assert len(client.queries) > 1


def _stale_cache_path(client: JimakuClient, path: str, params: dict[str, object]) -> Path:
    raw = json.dumps(
        {"base_url": client.base_url, "path": path, "params": params},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    assert client.cache_dir is not None
    return client.cache_dir / "jimaku-api" / f"{digest}.json"


def test_jimaku_transport_failure_uses_stale_cache_without_retry(monkeypatch, tmp_path: Path) -> None:
    client = JimakuClient("https://jimaku.cc", "key", cache_dir=tmp_path, cache_ttl_seconds=0.01)
    path = "/api/entries/search"
    params = {"anime": "true", "anilist_id": 123}
    cached = _stale_cache_path(client, path, params)
    cached.parent.mkdir(parents=True)
    payload = [{"id": 9, "name": "Cached anime"}]
    cached.write_text(json.dumps(payload), encoding="utf-8")
    old = time.time() - 60
    os.utime(cached, (old, old))
    attempts = 0

    def fail(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        request = httpx.Request("GET", "https://jimaku.cc/api/entries/search")
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(client.client, "get", fail)
    try:
        assert client._get_json(path, params) == payload
        assert attempts == 1
    finally:
        client.close()


def test_jimaku_transport_failure_without_cache_retries_only_once(monkeypatch, tmp_path: Path) -> None:
    client = JimakuClient("https://jimaku.cc", "key", cache_dir=tmp_path)
    attempts = 0

    def fail(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        request = httpx.Request("GET", "https://jimaku.cc/api/entries/search")
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(client.client, "get", fail)
    monkeypatch.setattr("pudge.providers.jimaku.time.sleep", lambda _seconds: None)
    try:
        with pytest.raises(JimakuError, match="timed out"):
            client.search_entries(anilist_id=123)
        assert attempts == 2
    finally:
        client.close()



def test_ln_audiobook_recovery_is_safe_for_partial_webapp_instances() -> None:
    from pudge.web_app import WebAppApi

    api = WebAppApi.__new__(WebAppApi)
    result = api._recover_light_novel_audiobook_downloads()
    assert result == {"skipped": True, "reason": "light_novels_unavailable", "recovered": 0}


def test_notification_helper_mode_restores_active_environment(monkeypatch) -> None:
    monkeypatch.delenv("PUDGE_NOTIFICATION_HELPER_ACTIVE", raising=False)
    monkeypatch.setattr(
        "pudge.notifications.send_native_notification_direct",
        lambda _subtitle, _message: True,
    )

    from pudge.notifications import maybe_handle_notification_helper

    assert maybe_handle_notification_helper(
        ["--pudge-native-notification", "Episode ready", "Example"]
    ) == 0
    assert "PUDGE_NOTIFICATION_HELPER_ACTIVE" not in __import__("os").environ
