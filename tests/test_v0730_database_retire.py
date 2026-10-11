import sys
import threading
from types import SimpleNamespace

import pytest

from pudge import web_app
from pudge.backup import create_backup, restore_backup
from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.work_scheduler import WorkScheduler
from test_v0730_lifecycle_regressions import _shutdown_api


def _restore_api(tmp_path, monkeypatch):
    api = _shutdown_api(tmp_path)
    api.config.library.root_dir = tmp_path / "library"
    api.config.library.cover_cache_dir = tmp_path / "covers"
    api.config_path = tmp_path / "config.toml"
    write_config(api.config, api.config_path)
    api.manager.work_scheduler = WorkScheduler(api.config.paths.cache_dir)
    api._configure_database_services()
    api.light_novels = SimpleNamespace(parse_study_text=lambda text: {"text": text})
    api._restore_lock = threading.Lock()
    api._review_gate_lock = threading.RLock()
    api._ui_state_cache = SimpleNamespace(invalidate=lambda: None)
    api.safe_mode = SimpleNamespace(active=True, finish_cleanly=lambda: None)
    api.get_state = lambda: {"marker": api.manager.db.get_state("marker")}
    api.manager.db.set_state("marker", "original")

    source_dir = tmp_path / "source"
    source_dir.mkdir()
    source_cfg = AppConfig()
    source_cfg.library.database_path = source_dir / "source.sqlite3"
    source_cfg.library.root_dir = api.config.library.root_dir
    source_cfg.paths.cache_dir = source_dir / "cache"
    source_cfg.paths.cache_dir.mkdir()
    source_config = source_dir / "config.toml"
    write_config(source_cfg, source_config)
    Database(source_cfg.library.database_path).set_state("marker", "restored")
    archive = tmp_path / "backup.zip"
    create_backup(
        config_path=source_config,
        database_path=source_cfg.library.database_path,
        cache_dir=source_cfg.paths.cache_dir,
        output=archive,
        version="test",
    )
    api.window = SimpleNamespace(create_file_dialog=lambda *a, **k: [str(archive)])
    monkeypatch.setitem(sys.modules, "webview", SimpleNamespace(OPEN_DIALOG=1))
    # Manager construction can scan user media and native workers, which this test does not need.
    monkeypatch.setattr(
        web_app,
        "AnimeManager",
        lambda cfg, **kwargs: SimpleNamespace(
            db=Database(cfg.library.database_path), work_scheduler=WorkScheduler(cfg.paths.cache_dir)
        ),
    )
    return api


def test_retired_database_rejects_new_connection_and_scope(tmp_path):
    db = Database(tmp_path / "database.sqlite3")
    getattr(db, "retire", lambda **kwargs: None)(timeout=1)
    for factory in (db.connect, db.connection_scope):
        with pytest.raises(RuntimeError, match="retired"):
            with factory() as conn:
                conn.execute("SELECT 1")


def test_restore_drains_direct_bridge_writer(tmp_path, monkeypatch):
    api = _restore_api(tmp_path, monkeypatch)
    old_db = api.manager.db
    entered, release, finished, replacing = (threading.Event() for _ in range(4))
    results, errors = [], []

    def bridge_write():
        # A window call writing through the shared repository (outside HTTP request tracking).
        with old_db.connection_scope() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old_db.set_state("writer", "pending")
            entered.set()
            assert release.wait(5)
            old_db.set_state("writer", "done")
        finished.set()

    def checked_restore(**kwargs):
        replacing.set()
        assert finished.is_set(), "Restore started while a direct bridge writer was active"
        return restore_backup(**kwargs)

    monkeypatch.setattr(web_app, "restore_backup", checked_restore)

    def writer():
        try:
            bridge_write()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=writer)
    restorer = threading.Thread(target=lambda: results.append(api.restore_full_backup()))
    try:
        worker.start()
        assert entered.wait(2)
        restorer.start()
        assert not replacing.wait(0.7), "Direct bridge writes bypassed the restore barrier"
        release.set()
        worker.join(3)
        restorer.join(5)
        assert not errors
        assert finished.is_set()
        assert results[0]["ok"]
        assert api.manager.db.get_state("marker") == "restored"
        assert old_db is not api.manager.db
        with pytest.raises(RuntimeError, match="retired"):
            old_db.set_state("late", "cannot resurrect old runtime")
    finally:
        release.set()
        worker.join(3)
        restorer.join(5)
        api.close()
