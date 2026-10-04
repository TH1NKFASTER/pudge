from __future__ import annotations

import json
import logging
import subprocess
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import pudge.config as config_module
from pudge.config import AppConfig, load_config, write_config
from pudge.database import Database
from pudge.diagnostics import DebugBundleBuilder, DiagnosticRecorder
from pudge.manager import AnimeManager
from pudge.mobile_sync import MobileSyncService
from pudge.mobile_sync_http import start_mobile_sync_server
from pudge.power_policy import PowerPolicy
from pudge.review_providers import ReviewProviderError
from pudge.secrets_store import SecretStore
from pudge.web_app import WebAppApi
from test_v0730_content_power_lifecycle import _gate


@pytest.mark.parametrize(('grade', 'wire'), [('pass', 'good'), ('fail', 'again')])
def test_content_pass_fail_reviews_commit_with_jiten_wire_grades(tmp_path, monkeypatch, grade, wire):
    gate, _, _ = _gate(tmp_path, monkeypatch)
    gate.api.light_novels.settings = lambda: SimpleNamespace(
        study_backend='jiten', jiten_api_key='synthetic-key', review_mode='binary'
    )

    def submit(word_id, reading_index, sent_grade, **kwargs):
        if sent_grade not in {'again', 'hard', 'good', 'easy'}:
            raise ReviewProviderError('Unsupported Jiten review grade')
        assert sent_grade == wire
        return {'ok': True, 'outcome': 'saved'}

    gate.api.light_novels.strict_review_submit = submit
    status = gate.begin('ln', 1, 0)
    assert gate.review(status['token'], 3, 0, grade, 'synthetic-attempt')['completed'] == 1


def test_clearing_saved_credentials_survives_config_reload(tmp_path, monkeypatch):
    accounts = {}

    def security(args, **kwargs):
        account = args[args.index('-a') + 1]
        operation = args[1]
        if operation == 'add-generic-password':
            accounts[account] = args[args.index('-w') + 1]
            return SimpleNamespace(returncode=0)
        if operation == 'find-generic-password':
            return SimpleNamespace(returncode=0 if account in accounts else 44, stdout=accounts.get(account, ''))
        accounts.pop(account, None)
        return SimpleNamespace(returncode=0)

    store = SecretStore()
    monkeypatch.setattr(SecretStore, 'available', property(lambda self: True))
    monkeypatch.setattr('pudge.secrets_store.subprocess.run', security)
    monkeypatch.setattr(config_module, '_SECRET_STORE', store)
    destination = tmp_path / 'config.toml'
    monkeypatch.setattr(config_module, 'DEFAULT_CONFIG_PATH', destination)
    cfg = AppConfig()
    cfg.qbittorrent.password = 'synthetic-password'
    cfg.qbittorrent.api_key = 'synthetic-qbt-key'
    cfg.jimaku.api_key = 'synthetic-jimaku-key'
    cfg.anilist.access_token = 'synthetic-anilist-token'
    cfg.llm.api_key = 'synthetic-llm-key'
    write_config(cfg, destination)
    assert load_config(destination).qbittorrent.password == 'synthetic-password'
    cfg.qbittorrent.password = cfg.qbittorrent.api_key = cfg.jimaku.api_key = ''
    cfg.anilist.access_token = cfg.llm.api_key = ''
    write_config(cfg, destination)
    actual = load_config(destination)
    assert [actual.qbittorrent.password, actual.qbittorrent.api_key, actual.jimaku.api_key,
            actual.anilist.access_token, actual.llm.api_key] == ['', '', '', '', '']


def test_failed_keychain_clear_reports_error(monkeypatch):
    monkeypatch.setattr(SecretStore, 'available', property(lambda self: True))
    monkeypatch.setattr('pudge.secrets_store.subprocess.run', lambda *a, **k: SimpleNamespace(returncode=42))
    with pytest.raises(RuntimeError, match='Keychain'):
        SecretStore().persisted_config_value('synthetic-account', '')


@pytest.mark.parametrize('manual', [False, True])
def test_system_low_power_respects_disabled_auto_setting(manual):
    run = lambda *a, **k: SimpleNamespace(stdout="Now drawing from 'AC Power'\n100%; charged")
    policy = PowerPolicy(manual_enabled=manual, auto_enabled=False, platform='darwin',
                         run_command=run, native_probe=lambda: (True, False))
    state = policy.snapshot()
    assert state.mode == ('energy_saving' if manual else 'normal')
    assert 'system_low_power' not in state.reasons
    assert policy.background_block_reason(resource='io') == ('energy_saving' if manual else None)


def test_companion_request_logs_omit_pairing_and_media_credentials(tmp_path, caplog):
    service = MobileSyncService(Database(tmp_path / 'db.sqlite3'))
    # Other application tests reconfigure the logging hierarchy. Use an owned
    # logger so route redaction is checked independently of their log handlers.
    logger = logging.Logger('pudge.test.companion.credentials', logging.INFO)
    logger.addHandler(caplog.handler)
    server, thread = start_mobile_sync_server(service, host='127.0.0.1', port=0, logger=logger)
    base = f'http://127.0.0.1:{server.server_address[1]}'
    caplog.set_level(logging.INFO, logger=logger.name)
    try:
        for path in ['/companion/?pair=SYNTHETIC_PAIR_SECRET&name=phone',
                     '/api/v1/media/SYNTHETIC_MEDIA_SECRET/index.m3u8?token=SYNTHETIC_QUERY_SECRET']:
            try:
                urllib.request.urlopen(base + path, timeout=2).close()
            except urllib.error.HTTPError:
                pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert 'SYNTHETIC_PAIR_SECRET' not in caplog.text
    assert 'SYNTHETIC_MEDIA_SECRET' not in caplog.text
    assert 'SYNTHETIC_QUERY_SECRET' not in caplog.text
    assert '/companion/' in caplog.text and '/api/v1/media/' in caplog.text


def test_debug_export_scrubs_old_companion_url_credentials(tmp_path):
    log = tmp_path / 'runtime.log'
    log.write_text('GET /companion/?pair=SYNTHETIC_PAIR_SECRET&foo=ok HTTP/1.1\n'
                   'GET http://host/api/v1/media/SYNTHETIC_MEDIA_SECRET/index.m3u8?token=SYNTHETIC_QUERY_SECRET HTTP/1.1\n')
    db = Database(tmp_path / 'db.sqlite3')
    output = tmp_path / 'bundle.zip'
    DebugBundleBuilder(db, DiagnosticRecorder(db)).build(output, version='test', logs={'runtime': log})
    with zipfile.ZipFile(output) as archive:
        exported = b'\n'.join(archive.read(name) for name in archive.namelist())
    assert b'SYNTHETIC_PAIR_SECRET' not in exported
    assert b'SYNTHETIC_MEDIA_SECRET' not in exported
    assert b'SYNTHETIC_QUERY_SECRET' not in exported


def test_storage_poll_recomputes_real_usage_after_cache_expires(tmp_path, monkeypatch):
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / 'library'
    cfg.library.root_dir.mkdir()
    cfg.paths.download_dirs = []
    manager = AnimeManager.__new__(AnimeManager)
    manager.config = cfg
    api = WebAppApi.__new__(WebAppApi)
    api.manager = manager
    api._last_storage_status = None
    now = [1000.0]
    monkeypatch.setattr('pudge.web_app.time.monotonic', lambda: now[0])
    assert api._storage_payload(refresh=False)['used_bytes'] == 0
    (cfg.library.root_dir / 'episode.mkv').write_bytes(b'x' * 1234)
    now[0] += 1
    assert api._storage_payload(refresh=False)['used_bytes'] == 0
    now[0] += 60
    assert api._storage_payload(refresh=False)['used_bytes'] == 1234


def _shutdown_api(tmp_path):
    from pudge.audiobooks import AudiobookService
    from pudge.task_supervisor import TaskSupervisor

    api = WebAppApi.__new__(WebAppApi)
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / 'cache'
    cfg.paths.cache_dir.mkdir(exist_ok=True)
    cfg.library.database_path = tmp_path / 'db.sqlite3'
    api.config = cfg
    api.manager = SimpleNamespace(db=Database(cfg.library.database_path))
    api.logger = logging.getLogger('pudge.test.shutdown')
    api.task_supervisor = TaskSupervisor()
    api.audiobooks = AudiobookService(api.manager.db, cache_dir=cfg.paths.cache_dir, ffprobe='ffprobe', mpv='mpv')
    api.energy_monitor = SimpleNamespace(stop=lambda: None)
    api._enter_background_quiet = lambda **kwargs: {}
    api.visual_novel_stop = lambda: None
    api._stop_scheduled_agent = lambda: None
    api._start_scheduled_agent = lambda: None
    api._restore_background_blockers = lambda: []
    api._play_processes = {}
    api._play_registry = {}
    return api


def test_refused_update_leaves_task_admission_and_runtime_usable(tmp_path):
    api = _shutdown_api(tmp_path)
    release = threading.Event()
    worker = api.task_supervisor.start('blocked-writer', lambda: release.wait(10))
    try:
        with pytest.raises(RuntimeError, match='incomplete|quiesce|active'):
            api._prepare_update_shutdown()
        assert not getattr(api, '_closing', False)
        task = api.task_supervisor.start('still-usable', lambda: api.manager.db.set_state('runtime', 'alive'))
        task.thread.join(1)
        assert api.manager.db.get_state('runtime') == 'alive'
        assert not api.audiobooks._closed_event.is_set()
    finally:
        release.set()
        worker.thread.join(1)
        api.close()


def test_successful_update_preflight_keeps_runtime_live_until_quit(tmp_path):
    api = _shutdown_api(tmp_path)
    try:
        api._prepare_update_shutdown()
        assert not getattr(api, '_closing', False)
        task = api.task_supervisor.start('after-preflight', lambda: api.manager.db.set_state('spawn-failed', 'recoverable'))
        task.thread.join(1)
        assert api.manager.db.get_state('spawn-failed') == 'recoverable'
    finally:
        api.close()


def test_app_quit_preserves_detached_player_tree_and_final_progress(tmp_path):
    import os
    import sys
    import time

    from pudge.manager_models import LibraryAnime, LibraryEpisode
    from pudge.process_cleanup import OwnedProcessTree

    api = _shutdown_api(tmp_path)
    api._owning_pid = os.getpid()
    video = tmp_path / 'synthetic.mkv'
    video.write_bytes(b'synthetic')
    api.manager.db.upsert_anime(LibraryAnime(1, 'Synthetic'))
    api.manager.db.upsert_episode(LibraryEpisode(1, 'Synthetic', 1, video))
    ready, release = tmp_path / 'ready', tmp_path / 'release'
    child_code = '''import sys,time
from pathlib import Path
from pudge.database import Database
from pudge.playback_save import save_playback
ready,release,db,video=map(Path,sys.argv[1:])
ready.write_text('ready')
while not release.exists(): time.sleep(.02)
save_playback(Database(db,initialize=False),video,position=47,duration=100,active_seconds=47)
'''
    launcher_code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-c',sys.argv[1],*sys.argv[2:]]); p.wait()"
    launcher = subprocess.Popen([sys.executable, '-c', launcher_code, child_code,
                                 str(ready), str(release), str(api.manager.db.path), str(video)])
    worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])
    api._play_processes[str(video)] = launcher
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            threading.Event().wait(.02)
        assert ready.exists()
        api.close()
        assert launcher.poll() is None
        assert worker.wait(timeout=2) != 0
        release.write_text('finish')
        assert launcher.wait(timeout=5) == 0
        assert api.manager.db.playback_evidence(video)['position'] == 47
    finally:
        release.write_text('finish')
        if launcher.poll() is None:
            OwnedProcessTree(launcher.pid).stop()
            launcher.terminate()
        launcher.wait(timeout=3)
        if worker.poll() is None:
            worker.terminate()
        worker.wait(timeout=3)
        api.close()
