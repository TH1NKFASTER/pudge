from __future__ import annotations

import json
import logging
import sys
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import pudge.web_app as web_app_module
from pudge.backup import create_backup, restore_backup
from pudge.config import AppConfig, load_config, write_config
from pudge.database import Database
from pudge.mobile_sync import MobileSyncService
from pudge.web_app import WebAppApi
from pudge.work_scheduler import WorkScheduler
from test_v0730_lifecycle_regressions import _shutdown_api


def _restore_api(tmp_path, monkeypatch):
    api = _shutdown_api(tmp_path)
    api.config.companion.enabled = True
    api.config.companion.bind_host = '127.0.0.1'
    api.config.companion.port = 0
    api.config.library.root_dir = tmp_path / 'library'
    api.config.library.cover_cache_dir = tmp_path / 'covers'
    api.config_path = tmp_path / 'config.toml'
    write_config(api.config, api.config_path)
    api.manager.work_scheduler = WorkScheduler(api.config.paths.cache_dir)
    api._configure_database_services()
    api.light_novels = SimpleNamespace(parse_study_text=lambda text: {'text': text})
    api._companion_server = None
    api._companion_thread = None
    api.companion_base_url = ''
    api._start_companion_server()
    api._restore_lock = threading.Lock()
    api._review_gate_lock = threading.RLock()
    api._ui_state_cache = SimpleNamespace(invalidate=lambda: None)
    api.safe_mode = SimpleNamespace(active=True, finish_cleanly=lambda: None)
    api.get_state = lambda: {'marker': api.manager.db.get_state('marker')}
    api.manager.db.set_state('marker', 'original')

    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    source_cfg = AppConfig()
    source_cfg.library.database_path = source_dir / 'source.sqlite3'
    source_cfg.library.root_dir = api.config.library.root_dir
    source_cfg.paths.cache_dir = source_dir / 'cache'
    source_cfg.paths.cache_dir.mkdir()
    source_cfg.companion.enabled = True
    source_cfg.companion.bind_host = '127.0.0.1'
    source_cfg.companion.port = 0
    source_config = source_dir / 'config.toml'
    write_config(source_cfg, source_config)
    Database(source_cfg.library.database_path).set_state('marker', 'restored')
    archive = tmp_path / 'backup.zip'
    create_backup(config_path=source_config, database_path=source_cfg.library.database_path,
                  cache_dir=source_cfg.paths.cache_dir, output=archive, version='test')
    api.window = SimpleNamespace(create_file_dialog=lambda *a, **k: [str(archive)])
    monkeypatch.setitem(sys.modules, 'webview', SimpleNamespace(OPEN_DIALOG=1))

    # Keep real DB/cache/streaming/review rebinding. Manager construction can
    # scan user media and native workers, which this lifecycle test does not need.
    monkeypatch.setattr(web_app_module, 'AnimeManager', lambda cfg, **kwargs: SimpleNamespace(
        db=Database(cfg.library.database_path), work_scheduler=WorkScheduler(cfg.paths.cache_dir)))
    return api


def _get_json(server, path='/api/v1/health'):
    with urllib.request.urlopen(f'http://127.0.0.1:{server.server_address[1]}' + path, timeout=2) as response:
        return json.load(response)


@pytest.mark.parametrize('fail_rebind', [False, True])
def test_restore_restarts_companion_with_live_rebound_dependencies(tmp_path, monkeypatch, fail_rebind):
    api = _restore_api(tmp_path, monkeypatch)
    old_server = api._companion_server
    old_streaming = api.companion_streaming
    old_service = api.mobile_sync
    if fail_rebind:
        rebind = api._reload_runtime_services_after_restore
        calls = 0

        def fail_once():
            nonlocal calls
            calls += 1
            rebind()
            if calls == 1:
                raise RuntimeError('synthetic rebind failure')

        api._reload_runtime_services_after_restore = fail_once
    try:
        result = api.restore_full_backup()
        assert result['ok'] is (not fail_rebind)
        assert api.manager.db.get_state('marker') == ('original' if fail_rebind else 'restored')
        assert api._companion_server is not old_server
        assert api._companion_server.service is api.mobile_sync
        assert api._companion_server.streaming is api.companion_streaming
        assert api.mobile_sync is not old_service
        assert old_streaming._closed and not api.companion_streaming._closed
        assert _get_json(api._companion_server)['ok']
        # Serving media must reach the new live service. An invalid ticket is a
        # normal auth rejection, rather than dispatch into a closed dependency.
        assert api._companion_server.study_parser.__self__ is api.light_novels
    finally:
        api.close()


def test_restore_waits_for_admitted_companion_sqlite_writer_before_replacement(tmp_path, monkeypatch):
    api = _restore_api(tmp_path, monkeypatch)
    server = api._companion_server
    entered, release, finished, replacement = (threading.Event() for _ in range(4))
    real_complete = api.mobile_sync.complete_pairing
    token = api.mobile_sync.start_pairing()['pairing_token']

    def delayed_complete(*args, **kwargs):
        with api.mobile_sync.database.connection_scope() as conn:
            conn.execute('BEGIN IMMEDIATE')
            api.mobile_sync.database.set_state('writer', 'before-restore')
            entered.set()
            assert release.wait(5)
            result = real_complete(*args, **kwargs)
        finished.set()
        return result

    api.mobile_sync.complete_pairing = delayed_complete

    def checked_restore(**kwargs):
        replacement.set()
        assert finished.is_set(), 'database replacement started with an admitted SQLite writer'
        return restore_backup(**kwargs)

    monkeypatch.setattr(web_app_module, 'restore_backup', checked_restore)
    request = urllib.request.Request(
        f'http://127.0.0.1:{server.server_address[1]}/api/v1/pair/complete',
        data=json.dumps({'pairing_token': token, 'name': 'Synthetic', 'platform': 'test'}).encode(),
        headers={'Content-Type': 'application/json'},
    )
    replies, outcomes = [], []
    writer = threading.Thread(target=lambda: replies.append(json.load(urllib.request.urlopen(request, timeout=5))))
    restorer = threading.Thread(target=lambda: outcomes.append(api.restore_full_backup()))
    try:
        writer.start()
        assert entered.wait(2)
        restorer.start()
        assert not replacement.wait(.7), 'restore did not wait for its HTTP writer'
        release.set()
        writer.join(3)
        restorer.join(5)
        assert finished.is_set() and replies[0]['ok']
        assert outcomes[0]['ok']
        assert api.manager.db.get_state('marker') == 'restored'
        assert api.manager.db.get_state('writer', '') == ''
        assert _get_json(api._companion_server)['ok']
    finally:
        release.set()
        writer.join(3)
        if restorer.ident is not None:
            restorer.join(5)
        api.close()
