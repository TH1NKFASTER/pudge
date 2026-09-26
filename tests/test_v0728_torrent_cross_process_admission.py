"""Scheduled-agent traffic starts must obey GUI's cross-process Torrent Off."""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.manager import AnimeManager, ManagerError
from pudge.torrent_admission import TorrentAdmission
from pudge.web_app import WebAppApi


class _Client:
    def __init__(self):
        self.calls = []

    def start(self, torrent_hash):
        self.calls.append(torrent_hash)
        return True

    def add_release(self, release, *, paused):
        self.calls.append((release, paused))
        return 'hash'


def _manager(config_path: Path, *, locally_enabled: bool):
    manager = AnimeManager.__new__(AnimeManager)
    manager.config = SimpleNamespace(
        config_path=config_path,
        nyaa=SimpleNamespace(torrents_enabled=locally_enabled),
    )
    manager.downloads_configured = lambda: True
    return manager


def test_old_agent_config_cannot_start_after_gui_off(tmp_path):
    path = tmp_path / 'config.toml'
    gate = TorrentAdmission(path)
    manager = _manager(path, locally_enabled=True)  # stale agent config
    client = _Client()
    with gate.locked():
        gate.publish(False)
    assert manager.downloads_enabled() is False
    with pytest.raises(ManagerError, match='off'):
        manager._torrent_network_start(client, 'start', 'old-task')
    with pytest.raises(ManagerError, match='off'):
        manager._torrent_network_start(client, 'add_release', 'new-task', paused=False)
    assert client.calls == []
    with gate.locked():
        gate.publish(True)
    assert manager.downloads_enabled() is True
    manager._torrent_network_start(client, 'start', 'new-task')
    assert client.calls == ['new-task']


def test_unreadable_intent_fails_closed(tmp_path):
    path = tmp_path / 'config.toml'
    gate = TorrentAdmission(path)
    with gate.locked():
        gate.publish(True)
    gate._state_path.write_text('not json')
    assert gate.enabled(fallback=True) is False
    assert _manager(path, locally_enabled=True).downloads_enabled() is False


def test_cross_process_start_waits_for_off_and_rechecks(tmp_path):
    path = tmp_path / 'config.toml'
    gate = TorrentAdmission(path)
    started = tmp_path / 'started'
    blocked = tmp_path / 'blocked'
    code = '''
import sys
from pathlib import Path
from types import SimpleNamespace
from pudge.manager import AnimeManager, ManagerError
path, started, blocked = [Path(value) for value in sys.argv[1:]]
manager = AnimeManager.__new__(AnimeManager)
manager.config = SimpleNamespace(config_path=path, nyaa=SimpleNamespace(torrents_enabled=True))
class Client:
    def start(self, _hash):
        started.write_text('started')
try:
    manager._torrent_network_start(Client(), 'start', 'delayed-task')
except ManagerError:
    blocked.write_text('blocked')
'''
    with gate.locked():
        gate.publish(True)
        child = subprocess.Popen(
            [sys.executable, '-c', code, str(path), str(started), str(blocked)],
            cwd=Path(__file__).resolve().parents[1],
        )
        try:
            time.sleep(0.25)
            assert child.poll() is None, 'start must wait for the cross-process lock'
            assert not started.exists()
            gate.publish(False)
        finally:
            if child.poll() is not None and child.returncode != 0:
                child.kill()
    try:
        assert child.wait(timeout=15) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
    assert blocked.read_text() == 'blocked'
    assert not started.exists()


def test_gui_off_waits_for_other_manager_before_quiescing(tmp_path, monkeypatch):
    from pudge import web_app

    path = tmp_path / 'config.toml'
    gate = TorrentAdmission(path)
    with gate.locked():
        gate.publish(True)
    manager = _manager(path, locally_enabled=True)
    entered, release, quiesced = threading.Event(), threading.Event(), threading.Event()

    class SlowClient:
        def start(self, _hash):
            entered.set()
            assert release.wait(timeout=5)

    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        config_path=path, nyaa=SimpleNamespace(torrents_enabled=True),
    )
    api.manager = SimpleNamespace(config=api.config, download_intents=SimpleNamespace(waiting_count=lambda: 0))
    api.logger = logging.getLogger('test-cross-process-off')
    api._torrent_session_enabled = True
    api._torrent_session_authoritative = True
    api._downloads_configured = lambda: False
    api._quiesce_torrent_backends = lambda *, reason: (quiesced.set() or {})
    api._bump_ui_state_version = lambda: None
    monkeypatch.setattr(web_app, 'write_torrents_enabled', lambda *_args: None)
    monkeypatch.setattr(web_app, 'write_config', lambda *_args: None)
    errors = []

    def run_start():
        try:
            manager._torrent_network_start(SlowClient(), 'start', 'abc')
        except Exception as exc:
            errors.append(exc)

    def run_off():
        try:
            api.set_torrents_enabled(False)
        except Exception as exc:
            errors.append(exc)

    one = threading.Thread(target=run_start)
    two = threading.Thread(target=run_off)
    one.start()
    try:
        assert entered.wait(timeout=5)
        two.start()
        assert not quiesced.wait(timeout=0.1)
    finally:
        release.set()
        one.join(timeout=5)
        if two.ident is not None:
            two.join(timeout=5)
    assert not one.is_alive() and not two.is_alive()
    assert not errors
    assert quiesced.is_set()
    assert gate.enabled(fallback=True) is False
