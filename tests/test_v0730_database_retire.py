import threading

import pytest

from pudge import web_app
from pudge.backup import restore_backup
from pudge.database import Database
from test_v0730_companion_restore_barrier import _restore_api


def test_retired_database_rejects_new_connection_and_scope(tmp_path):
    db = Database(tmp_path / 'database.sqlite3')
    getattr(db, 'retire', lambda **kwargs: None)(timeout=1)
    for factory in (db.connect, db.connection_scope):
        with pytest.raises(RuntimeError, match='retired'):
            with factory() as conn:
                conn.execute('SELECT 1')


def test_restore_drains_direct_companion_bridge_writer(tmp_path, monkeypatch):
    api = _restore_api(tmp_path, monkeypatch)
    old_db = api.manager.db
    entered, release, finished, replacing = (threading.Event() for _ in range(4))
    original_pairing = api.mobile_sync.start_pairing
    results, errors = [], []

    def pairing():
        with old_db.connection_scope() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old_db.set_state('writer', 'pending')
            entered.set()
            assert release.wait(5)
            result = original_pairing()
        finished.set()
        return result

    def checked_restore(**kwargs):
        replacing.set()
        assert finished.is_set(), 'Restore started while a direct bridge writer was active'
        return restore_backup(**kwargs)

    api.mobile_sync.start_pairing = pairing
    monkeypatch.setattr(web_app, 'restore_backup', checked_restore)

    def writer():
        try:
            api.companion_start_pairing()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=writer)
    restorer = threading.Thread(target=lambda: results.append(api.restore_full_backup()))
    try:
        worker.start()
        assert entered.wait(2)
        restorer.start()
        assert not replacing.wait(.7), 'Direct bridge writes bypassed the restore barrier'
        release.set()
        worker.join(3)
        restorer.join(5)
        assert not errors
        assert finished.is_set()
        assert results[0]['ok']
        assert api.manager.db.get_state('marker') == 'restored'
        assert old_db is not api.manager.db
        with pytest.raises(RuntimeError, match='retired'):
            old_db.set_state('late', 'cannot resurrect old runtime')
    finally:
        release.set()
        worker.join(3)
        restorer.join(5)
        api.close()
