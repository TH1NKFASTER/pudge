from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

from pudge import web_app
from pudge.presentation_state import derive_episode_presentation


def _download(*, state: str = "active", progress: float = 0.12, backend: str = "aria2"):
    return SimpleNamespace(
        torrent_hash="deadbeef",
        name="Example",
        state=state,
        progress=progress,
        media_id=123,
        episode=1,
        media_episode=1,
        release_episode=1,
        is_batch=False,
        raw={"backend": backend, "total_size": 1000, "downloaded": int(1000 * progress)},
        save_path="/tmp/example",
        added_on=1,
        completed_on=0,
    )


def test_incomplete_download_is_paused_when_global_torrent_traffic_is_off() -> None:
    state = derive_episode_presentation(
        download={"state": "active", "progress": 0.12},
        downloads_enabled=False,
    )

    assert state["status"] == "paused_download"
    assert state["progress_percent"] == 12


def test_download_payload_reports_effective_paused_state_when_off() -> None:
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=False),
        qbittorrent=SimpleNamespace(enabled=False),
        aria2=SimpleNamespace(enabled=True),
    )
    api.manager = SimpleNamespace(
        downloads_configured=lambda: True,
        torrent_backend_name=lambda: "aria2",
    )
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = False
    api._torrent_session_authoritative = True

    payload = api._download_payload(_download(), {})

    assert payload["state"] == "active"
    assert payload["effective_state"] == "paused"
    assert payload["progress"] == 0.12


def test_torrent_status_off_counts_unfinished_jobs_as_paused() -> None:
    rows = [_download(progress=0.12), _download(progress=0.09)]
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=False),
        qbittorrent=SimpleNamespace(category="pudge"),
    )
    manager_config = SimpleNamespace(nyaa=SimpleNamespace(torrents_enabled=True))
    api.manager = SimpleNamespace(
        config=manager_config,
        db=SimpleNamespace(downloads=lambda: rows),
        download_intents=SimpleNamespace(waiting_count=lambda: 0),
    )
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = False
    api._torrent_session_authoritative = True
    api._last_torrent_traffic = {}
    api._downloads_configured = lambda: True

    result = api.torrent_traffic_status()

    assert result["enabled"] is False
    assert result["waiting"] == 0
    assert result["paused"] == 2


def test_enable_followup_resumes_existing_job_before_discovery() -> None:
    events: list[str] = []

    class FakeClient:
        def start(self, torrent_hash: str) -> None:
            events.append(f"resume:{torrent_hash}")

        def close(self) -> None:
            events.append("close")

    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.manager = SimpleNamespace(
        db=SimpleNamespace(downloads=lambda: [_download()]),
        torrent_clients=lambda: [("aria2", FakeClient())],
        auto_search_current=lambda: events.append("search") or 0,
    )
    api.logger = logging.getLogger("test-v208-torrent-paused")
    api._bump_ui_state_version = lambda: events.append("version")

    api._torrent_toggle_auto_search()

    assert events.index("resume:deadbeef") < events.index("search")
    assert events.count("resume:deadbeef") == 1



def test_download_payload_does_not_pause_just_because_backend_is_unconfigured() -> None:
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=True),
        qbittorrent=SimpleNamespace(enabled=False),
        aria2=SimpleNamespace(enabled=False),
    )
    api.manager = SimpleNamespace(torrent_backend_name=lambda: "aria2")

    payload = api._download_payload(_download(state="active", progress=0.5), {})

    assert payload["state"] == "active"
    assert payload["effective_state"] == "active"
    assert payload["progress"] == 0.5


def test_download_payload_without_config_keeps_observed_backend_state() -> None:
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.manager = SimpleNamespace(torrent_backend_name=lambda: "aria2")

    payload = api._download_payload(_download(state="active", progress=0.5), {})

    assert payload["state"] == "active"
    assert payload["effective_state"] == "active"

def test_frontend_exposes_paused_job_state_and_count() -> None:
    home = (web_app.Path(web_app.__file__).parent / "web" / "home_status.js").read_text(
        encoding="utf-8"
    )
    index = (web_app.Path(web_app.__file__).parent / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "download?.effective_state||download?.state" in home
    assert "p.status==='paused_download'" in home
    assert "Paused ${progress}%" in home
    assert "compactDownloadStatus(download)" in index
    assert "live.paused" in index
    assert "${paused} paused" in index
