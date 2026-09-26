"""Backend-observed Torrent Off must be distinct from the user's Off intent."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from types import SimpleNamespace

from pudge import web_app
from pudge.config import AppConfig


def _api(tmp_path: Path, monkeypatch, *, qbt_enabled: bool = True) -> web_app.WebAppApi:
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path)
    api = object.__new__(web_app.WebAppApi)
    api.config = AppConfig(config_path=tmp_path / "config.toml")
    api.config.nyaa.torrents_enabled = False
    api.config.qbittorrent.enabled = qbt_enabled
    api.config.aria2.enabled = False
    api.manager = SimpleNamespace(
        config=api.config, db=SimpleNamespace(downloads=lambda: []),
        download_intents=SimpleNamespace(waiting_count=lambda: 0),
        torrent_backend_name=lambda: "qBittorrent" if qbt_enabled else "disabled",
        torrent_clients=lambda: [],
    )
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_authoritative = True
    api._torrent_session_enabled = False
    api._torrent_toggle_generation = 1
    api._torrent_traffic_lock = threading.Lock()
    api._last_torrent_traffic = {}
    api._torrent_off_evidence = {
        "generation": 1, "aria2_shutdown_confirmed": False, "checked_at": 0,
    }
    api._downloads_configured = lambda: qbt_enabled
    api.logger = logging.getLogger("test-verified-off-e2")
    return api


def _row(state: str, *, category: str = "pudge", tags: str = "", speed: int = 0):
    return SimpleNamespace(
        torrent_hash=f"hash-{category}", state=state,
        raw={"category": category, "tags": tags, "dlspeed": speed, "upspeed": 0},
    )


def test_observed_pause_confirms_off_without_starting_qbt(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch)
    events: list[tuple] = []

    class SafeClient:
        def __init__(self, *_args, **kwargs):
            events.append(("init", kwargs["auto_start_app"], kwargs["timeout"]))

        def torrents(self, *, category):
            events.append(("read", category))
            return [_row("pausedDL"), _row("downloading", category="foreign", speed=9999)]

        def close(self):
            events.append(("close",))

    monkeypatch.setattr(web_app, "QBittorrentClient", SafeClient)
    result = api.torrent_traffic_status()
    assert result["transition"] == "off_confirmed"
    assert result["enabled"] is False
    assert result["observed_state"] == "stopped"
    assert result["download_speed"] == 0 and result["upload_speed"] == 0
    assert result["waiting"] == 0 and result["paused"] == 1
    assert result["stale"] is False and result["observed_at"] is not None
    assert events == [("init", False, 2.0), ("read", ""), ("close",)]
    assert api.torrent_traffic_status()["transition"] == "off_confirmed"
    assert len(events) == 3  # Read-only inspection is throttled.


def test_active_owned_download_revokes_previous_confirmation(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch)
    current = [_row("pausedDL")]

    class SafeClient:
        def __init__(self, *_args, **kwargs):
            assert kwargs["auto_start_app"] is False

        def torrents(self, *, category):
            assert category == ""
            return current

        def close(self):
            pass

    monkeypatch.setattr(web_app, "QBittorrentClient", SafeClient)
    assert api.torrent_traffic_status()["transition"] == "off_confirmed"
    current[:] = [_row("downloading", speed=10)]
    api._torrent_off_evidence["checked_at"] = 0  # Force the next safe inspection.
    result = api.torrent_traffic_status()
    assert result["transition"] == "unconfirmed"
    assert result["observed_state"] == "unknown"
    assert result["stale"] is True and result["download_speed"] is None
    assert result["backend_errors"] == ["qbittorrent_unconfirmed"]


def test_errored_or_unknown_qbt_observation_never_claims_zero(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch)

    class FailingClient:
        def __init__(self, *_args, **kwargs):
            assert kwargs["auto_start_app"] is False

        def torrents(self, *, category):
            raise TimeoutError("unavailable")

        def close(self):
            pass

    monkeypatch.setattr(web_app, "QBittorrentClient", FailingClient)
    first = api.torrent_traffic_status()
    assert first["transition"] == "unconfirmed" and first["download_speed"] is None
    assert first["backend_errors"] == ["qbittorrent_unconfirmed"]
    # Unknown backend states are not synonymous with paused.
    api._torrent_off_evidence["checked_at"] = 0
    monkeypatch.setattr(FailingClient, "torrents", lambda self, *, category: [_row("futureState")])
    second = api.torrent_traffic_status()
    assert second["transition"] == "unconfirmed" and second["download_speed"] is None


def test_aria2_absence_confirms_off_without_previous_shutdown_ack(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch, qbt_enabled=False)
    (tmp_path / "aria2").mkdir()

    class SafeAria:
        active = False
        managed = False

        def __init__(self, **kwargs):
            assert kwargs["auto_start"] is False

        def _probe(self):
            return self.active

        def managed_sidecar_running(self):
            return self.managed

        def close(self):
            pass

    monkeypatch.setattr(web_app, "Aria2Client", SafeAria)

    def no_listener(*_args, **_kwargs):
        raise ConnectionRefusedError("sidecar stopped")

    monkeypatch.setattr(web_app.socket, "create_connection", no_listener)
    assert api.torrent_traffic_status()["transition"] == "off_confirmed"
    SafeAria.active = True
    api._torrent_off_evidence["checked_at"] = 0
    assert api.torrent_traffic_status()["transition"] == "unconfirmed"


def test_aria2_unreachable_rpc_but_occupied_port_is_not_confirmed(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch, qbt_enabled=False)
    (tmp_path / "aria2").mkdir()
    api._torrent_off_evidence["aria2_shutdown_confirmed"] = True

    class UnauthenticatedAria:
        def __init__(self, **kwargs):
            assert kwargs["auto_start"] is False

        def _probe(self):
            return False

        def managed_sidecar_running(self):
            return False

        def close(self):
            pass

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(web_app, "Aria2Client", UnauthenticatedAria)
    monkeypatch.setattr(web_app.socket, "create_connection", lambda *_a, **_k: Socket())
    result = api.torrent_traffic_status()
    assert result["transition"] == "unconfirmed" and result["download_speed"] is None


def test_superseded_off_poll_does_not_publish_after_on(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    class DelayedClient:
        def __init__(self, *_args, **kwargs):
            assert kwargs["auto_start_app"] is False

        def torrents(self, *, category):
            entered.set()
            assert release.wait(timeout=5)
            return [_row("pausedDL")]

        def close(self):
            pass

    monkeypatch.setattr(web_app, "QBittorrentClient", DelayedClient)
    outcomes: list[dict] = []
    thread = threading.Thread(target=lambda: outcomes.append(api.torrent_traffic_status()))
    thread.start()
    try:
        assert entered.wait(timeout=5)
        with api._torrent_state_lock:
            api._torrent_session_enabled = True
            api.config.nyaa.torrents_enabled = True
            api._torrent_toggle_generation = 2
            api._torrent_off_evidence = {}
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive() and outcomes
    assert outcomes[0]["enabled"] is True
    assert api._last_torrent_traffic.get("enabled") is not False
    assert api._torrent_off_evidence == {}


def test_failed_config_persist_cannot_leave_off_evidence(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch, qbt_enabled=False)
    api._torrent_session_enabled = True
    api.config.nyaa.torrents_enabled = True
    api._torrent_shared_admission = lambda: None
    api._quiesce_torrent_backends = lambda *, reason: {
        "aria2_stopped": True, "qbittorrent_paused": 0,
    }
    api._ui_state_cache = None
    api._bump_ui_state_version = lambda: ""
    api._downloads_configured = lambda: False

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(web_app, "write_config", fail_write)
    try:
        api.set_torrents_enabled(False)
    except OSError:
        pass
    else:
        raise AssertionError("expected failed persistence")
    assert api._torrent_off_evidence == {}
    assert api.torrent_traffic_status()["transition"] == "unconfirmed"


def test_off_pauses_untracked_audiobook_but_not_foreign_torrent(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch)
    api.manager.db.downloads = lambda: [SimpleNamespace(torrent_hash="anime-hash")]
    paused: list[str] = []

    class SafeClient:
        def __init__(self, *_args, **kwargs):
            assert kwargs["auto_start_app"] is False

        def torrents(self, *, category):
            assert category == ""
            return [
                SimpleNamespace(torrent_hash="anime-hash", state="downloading", raw={"category": "other"}),
                SimpleNamespace(torrent_hash="audio-hash", state="downloading", raw={
                    "category": "pudge-audiobooks", "tags": "pudge,audiobook",
                }),
                SimpleNamespace(torrent_hash="foreign-hash", state="downloading", raw={
                    "category": "other", "tags": "private",
                }),
            ]

        def pause(self, torrent_hash):
            paused.append(torrent_hash)

        def close(self):
            pass

    monkeypatch.setattr(web_app, "QBittorrentClient", SafeClient)
    assert api._quiesce_qbittorrent(reason="test") == 2
    assert paused == ["anime-hash", "audio-hash"]


def test_live_listing_failure_still_pauses_all_persisted_hashes(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch)
    api.manager.db.downloads = lambda: [
        SimpleNamespace(torrent_hash="healthy"), SimpleNamespace(torrent_hash="broken"),
    ]
    paused: list[str] = []

    class SafeClient:
        def __init__(self, *_args, **kwargs):
            assert kwargs["auto_start_app"] is False

        def torrents(self, *, category):
            raise TimeoutError("offline")

        def pause(self, torrent_hash):
            paused.append(torrent_hash)
            if torrent_hash == "broken":
                raise TimeoutError("pause failed")

        def close(self):
            pass

    monkeypatch.setattr(web_app, "QBittorrentClient", SafeClient)
    assert api._quiesce_qbittorrent(reason="test") == 1
    assert paused == ["broken", "healthy"]


def test_shutdown_constructor_error_is_unconfirmed_not_an_off_toggle_failure(tmp_path, monkeypatch) -> None:
    api = _api(tmp_path, monkeypatch, qbt_enabled=False)
    (tmp_path / "aria2").mkdir()

    def fail_constructor(**_kwargs):
        raise OSError("aria2 unavailable")

    monkeypatch.setattr(web_app, "Aria2Client", fail_constructor)
    assert api._shutdown_aria2_from_config(reason="test") is False
    assert api.torrent_traffic_status()["transition"] == "unconfirmed"
