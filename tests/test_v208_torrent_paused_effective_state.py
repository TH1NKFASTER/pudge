from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

from pudge import web_app
from pudge.presentation_state import derive_episode_presentation
from test_v197_torrent_http_bridge import SWITCH
from tests.i18n_support import en, ru


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
    # A local DB row is not proof that the backend completed its pause RPC.
    assert result["waiting"] is None
    assert result["paused"] is None
    assert result["observed_state"] == "unknown"


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


# A download at 12% that the backend reports paused because torrent traffic is off.
PAUSED_DOWNLOAD = {
    "hash": "deadbeef",
    "name": "Example 01",
    "media_id": 123,
    "episode": 1,
    "state": "active",
    "effective_state": "paused",
    "progress": 0.12,
}
PAUSED_ANIME = {
    "media_id": 123,
    "title": "Example",
    "episodes": 12,
    "next_episode": 1,
    "download": PAUSED_DOWNLOAD,
    "presentation": {"status": "paused_download", "episode": 1},
}


def test_paused_download_shows_paused_and_progress_on_home(page) -> None:
    assert [en("ui.paused"), ru("ui.paused")] == ["Paused", "Пауза"]
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            await __tick(1000);                                   // the switch reports torrents off (confirmed)
            setPage('current', true); renderCurrent();
            const card = () => __text('#currentContent [data-media-id="123"] .airing-state');
            const first = card();
            await __tick(7000);                                   // live status refreshes keep it
            return {first, later: card(), paused: t('ui.paused'), label: torrentUiLabel('paused', ui.lang),
                    // A card without a presentation (only the download row) while torrents are off.
                    bare: compactDownloadStatus({state: 'active', progress: 0.12})};
            """,
            lang=lang,
            before=SWITCH,
            state={
                "current": [PAUSED_ANIME],
                "home": {"waiting": [PAUSED_ANIME]},
                "downloads": [PAUSED_DOWNLOAD],
            },
        )
        out = result.value
        assert out["label"] == out["paused"] == (en if lang == "en" else ru)("ui.paused")
        assert out["first"] == out["later"] == out["bare"] == f"{out['paused']} · 12%"
