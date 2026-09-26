from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

import pytest

from pudge import web_app
from pudge.torrent_observation import normalize_torrent_state


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pausedDL", "paused"), ("stoppedDL", "paused"),
        ("queuedDL", "queued"), ("stalledDL", "stalled"),
        ("pausedUP", "paused"), ("stalledUP", "stalled"),
        ("checkingDL", "checking"), ("forcedDL", "downloading"),
        ("uploading", "seeding"), ("unknownFutureState", "unknown"),
    ],
)
def test_normalization(raw, expected):
    assert normalize_torrent_state(raw) == expected


def _api(clients, *, enabled=True):
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.config = SimpleNamespace(
        nyaa=SimpleNamespace(torrents_enabled=enabled),
        qbittorrent=SimpleNamespace(category="pudge"),
    )
    api.manager = SimpleNamespace(
        config=api.config,
        torrent_clients=lambda: clients,
        db=SimpleNamespace(downloads=lambda: []),
    )
    api.logger = logging.getLogger("test-torrent-observation")
    api._torrent_state_lock = threading.RLock()
    api._torrent_session_enabled = enabled
    api._torrent_session_authoritative = True
    api._torrent_traffic_lock = threading.Lock()
    api._last_torrent_traffic = {}
    api._downloads_configured = lambda: True
    return api


class Client:
    def __init__(self, states=(), *, error=None):
        self.states, self.error, self.closed = states, error, False

    def torrents(self, *, category):
        if self.error:
            raise self.error
        return [SimpleNamespace(state=state, raw={"dlspeed": 0, "upspeed": 0})
                for state in self.states]

    def close(self):
        self.closed = True


def test_queued_paused_and_stalled_not_counted_as_active():
    client = Client(["pausedDL", "stoppedDL", "queuedDL", "stalledDL", "downloading", "uploading"])
    result = _api([("qbittorrent", client)]).torrent_traffic_status()
    assert result["active"] == 2
    assert result["paused"] == 2
    assert result["waiting"] == 2
    assert result["stale"] is False
    assert client.closed is True


def test_failed_poll_reports_unknown_instead_of_fresh_zero():
    client = Client(error=TimeoutError("unavailable"))
    result = _api([("qbittorrent", client)]).torrent_traffic_status()
    assert result["enabled"] is True
    assert result["stale"] is True
    assert result["download_speed"] is None
    assert result["upload_speed"] is None
    assert result["updated_at"] is None
    assert result["backend_errors"] == ["qbittorrent"]
    assert client.closed is True


def test_partial_failure_does_not_publish_partial_rate_as_total():
    good = Client(["downloading"])
    bad = Client(error=TimeoutError("offline"))
    result = _api([("qbittorrent", good), ("another", bad)]).torrent_traffic_status()
    assert result["stale"] is True
    assert result["download_speed"] is None
    assert result["active"] is None
    assert result["backend_errors"] == ["another"]


def test_off_not_misrepresented_as_confirmed_backend_stop():
    result = _api([], enabled=False).torrent_traffic_status()
    assert result["enabled"] is False
    assert result["observed_state"] == "unknown"
    assert result["stale"] is True
    assert result["observed_at"] is None
