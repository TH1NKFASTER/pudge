from __future__ import annotations

import inspect
import threading
from pathlib import Path
from types import SimpleNamespace

from pudge import agent, app_session
from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.review_gate import ReviewGateStore
from pudge.web_app import WebAppApi


ROOT = Path(__file__).resolve().parents[1]


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_wal_is_persistent_database_configuration_not_per_connection(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    with db.connect() as conn:
        assert str(conn.execute("PRAGMA journal_mode").fetchone()[0]).casefold() == "wal"

    # WAL persists in the database file. Reissuing journal_mode on every tiny
    # state lookup was visible in the R10 macOS stack sample as repeated schema
    # parsing and made connection-heavy UI state assembly unnecessarily costly.
    assert "journal_mode" not in inspect.getsource(Database.connect)
    assert "PRAGMA journal_mode=WAL" in inspect.getsource(Database.__init__)


def _make_foreground_poll_idle(api: WebAppApi, monkeypatch) -> None:
    monkeypatch.setattr(api.manager, "sync_downloads", lambda: 0)
    monkeypatch.setattr(api.manager, "scan_subtitle_inbox", lambda: {"requeued": 0})
    monkeypatch.setattr(api.manager, "cleanup_qbittorrent_tags", lambda: {})
    api.manager._last_missing_episode_rows = 0


def test_foreground_noop_poll_reuses_versioned_fast_state(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    _make_foreground_poll_idle(api, monkeypatch)
    marker = {"fast": True}
    monkeypatch.setattr(api, "get_state_fast", lambda: marker)
    monkeypatch.setattr(
        api,
        "get_state",
        lambda: (_ for _ in ()).throw(AssertionError("no-op foreground poll must not rebuild full state")),
    )

    result = api.poll_downloads_and_subtitles()

    assert result["skipped"] is False
    assert result["state"] is marker


def test_foreground_busy_poll_also_uses_fast_state(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    marker = {"fast": True}
    monkeypatch.setattr(api, "get_state_fast", lambda: marker)
    monkeypatch.setattr(
        api,
        "get_state",
        lambda: (_ for _ in ()).throw(AssertionError("busy foreground poll must not rebuild full state")),
    )
    api._download_poll_lock.acquire()
    try:
        result = api.poll_downloads_and_subtitles()
    finally:
        api._download_poll_lock.release()

    assert result == {"skipped": True, "stats": {}, "state": marker}


class _NoCapPoolService:
    def __init__(self, available: int = 30) -> None:
        self.available = available
        self.calls = 0

    def settings(self):
        return SimpleNamespace(study_backend="jiten")

    def study_provider_capabilities(self, _backend: str):
        return {
            "configured": True,
            "strict_gate_supported": True,
            "strict_gate_reason": "safe",
            "account_key": "jiten:no-cap",
        }

    def strict_review_candidates(
        self,
        *,
        required: int,
        exclude_keys: set[str] | None = None,
        trusted_previous_keys: set[str] | None = None,
    ):
        self.calls += 1
        excluded = set(exclude_keys or ())
        cards = []
        for word_id in range(1, self.available + 1):
            key = f"{word_id}:0"
            if key in excluded:
                continue
            cards.append({"wordId": word_id, "readingIndex": 0, "pudgeCardKey": key})
            if len(cards) >= required:
                break
        # Deliberately no provider_available_estimate: this mirrors the real R10
        # trace where a 30/35 pool retried every minute but found no new cards.
        return {"session_id": "s1", "cards": cards, "batch_rounds": 2}


def test_review_prefetch_backs_off_when_provider_yields_no_new_cards(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.ui.review_gate_enabled = True
    cfg.ui.review_gate_count = 5
    db = Database(cfg.library.database_path)
    service = _NoCapPoolService(available=30)

    api = object.__new__(WebAppApi)
    api.config = cfg
    api.manager = SimpleNamespace(db=db)
    api.light_novels = service
    api.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    api._review_gate_lock = threading.RLock()
    api._review_gate_store = ReviewGateStore(db)
    api._review_gate_candidates = {}
    api._review_gate_pending = set()
    api._review_gate_prefetch_target = lambda: 35
    api._review_gate_prefetch_target_count = 35
    api._review_gate_ready_anime_count = 7

    first = api.review_gate_prefetch()
    assert first["started"] is True
    api._review_gate_prefetch_thread.join(timeout=3)
    assert len(api._review_gate_prefetch_cards) == 30

    # First old-cache retry discovers that randomized provider batches cannot
    # add anything to the 30-card pool and starts exponential backoff.
    api._review_gate_prefetch_fetched_at -= 600
    retry = api.review_gate_prefetch()
    assert retry["started"] is True
    api._review_gate_prefetch_thread.join(timeout=3)
    calls_after_no_progress = service.calls
    assert api._review_gate_prefetch_retry_at > 0

    # The next minute tick must not wake Jiten again. Explicit force=True remains
    # available to refill immediately after a card is consumed/completed.
    api._review_gate_prefetch_fetched_at -= 600
    backed_off = api.review_gate_prefetch()
    assert backed_off["started"] is False
    assert backed_off["reason"] == "backoff"
    assert backed_off["retry_after_seconds"] > 0
    assert service.calls == calls_after_no_progress


def test_ln_reader_close_releases_heavy_chapter_state() -> None:
    source = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    start = source.index("if(target.id==='lnReaderClose')")
    branch = source[start : start + 1400]

    assert "ui.lnChapterCacheGeneration++" in branch
    assert "ui.lnChapterPayloadCache.clear()" in branch
    assert "ui.lnTokenMap.clear()" in branch
    assert "ui.lnChapter=null" in branch
    assert "$('lnReader').replaceChildren()" in branch
    assert "while(ui.lnChapterPayloadCache.size>3)" in source
    assert "generation===Number(ui.lnChapterCacheGeneration||0)?cacheLnChapter" in source


def test_snapshot_and_cold_fast_state_reuse_one_sqlite_connection(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    original = db._open_connection
    opened = 0
    caller_thread = threading.get_ident()

    def counted_open():
        nonlocal opened
        if threading.get_ident() == caller_thread:
            opened += 1
        return original()

    monkeypatch.setattr(db, "_open_connection", counted_open)

    # Reconciliation intentionally keeps its normal write transactions. The
    # read-heavy state snapshot itself must fan out through only one DB handle.
    with db.connection_scope():
        payload = api._get_state(refresh_storage=False)
        api._store_ui_state_snapshot(payload)
    assert opened == 1

    opened = 0
    api._ui_state_cache.invalidate()
    api.get_state_fast()
    assert opened == 1



def test_app_session_tracks_interactive_window_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(app_session, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_session, "SESSION_PATH", tmp_path / "app-session.json")
    monkeypatch.setattr(app_session.os, "kill", lambda _pid, _signal: None)

    app_session.mark_app_running(pid=1234)
    assert app_session.app_session_window_active() is True

    assert app_session.set_app_window_active(False, pid=1234) is True
    assert app_session.app_session_active() is True
    assert app_session.app_session_window_active() is False

    assert app_session.set_app_window_active(True, pid=9999) is False
    assert app_session.app_session_window_active() is False


def test_scheduled_agent_defers_before_manager_when_window_is_active(monkeypatch, tmp_path: Path) -> None:
    config = SimpleNamespace(agent=SimpleNamespace(enabled=True))
    monkeypatch.setattr(agent, "load_config", lambda _path: config)
    monkeypatch.setattr(agent, "app_session_active", lambda: True)
    monkeypatch.setattr(agent, "app_session_window_active", lambda: True)

    def should_not_construct_manager(_config):
        raise AssertionError("interactive scheduled agent must defer before heavy manager setup")

    monkeypatch.setattr(agent, "AnimeManager", should_not_construct_manager)

    assert agent.main(["--scheduled", "--config", str(tmp_path / "config.toml")]) == 0


def test_scheduled_agent_can_work_when_live_window_is_inactive(monkeypatch, tmp_path: Path) -> None:
    now = 1_000_000.0
    calls: list[str] = []

    class FakeDb:
        def get_state(self, key, default=""):
            assert key == "agent_last_run"
            return "0"

        def subtitle_jobs(self):
            return []

    class FakeManager:
        def __init__(self, _config):
            self.db = FakeDb()

        def anilist_refresh_due(self, *, now):
            return False

        def run_once(self):
            calls.append("run_once")
            return {"ok": 1}

    config = SimpleNamespace(agent=SimpleNamespace(enabled=True, poll_minutes=5))
    monkeypatch.setattr(agent, "load_config", lambda _path: config)
    monkeypatch.setattr(agent, "app_session_active", lambda: True)
    monkeypatch.setattr(agent, "app_session_window_active", lambda: False)
    monkeypatch.setattr(agent, "AnimeManager", FakeManager)
    monkeypatch.setattr(agent.time, "time", lambda: now)
    monkeypatch.setattr(agent, "configure_logging", lambda: SimpleNamespace())

    class _Timed:
        def __enter__(self):
            return None
        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(agent, "timed_step", lambda *_args, **_kwargs: _Timed())

    assert agent.main(["--scheduled", "--config", str(tmp_path / "config.toml")]) == 0
    assert calls == ["run_once"]


def test_window_activity_is_published_to_backend_only_on_state_changes() -> None:
    source = (ROOT / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    start = source.index("function setWindowActivity(active)")
    branch = source[start : start + 1100]

    assert "if(changed&&window.pywebview?.api?.set_window_activity)" in branch
    assert "set_window_activity(next)" in branch


def test_webapp_window_activity_updates_session_marker(tmp_path: Path, monkeypatch) -> None:
    api = object.__new__(WebAppApi)
    calls: list[bool] = []
    monkeypatch.setattr("pudge.web_app.set_app_window_active", lambda active: calls.append(bool(active)) or True)

    assert api.set_window_activity(False) == {"ok": True, "active": False, "updated": True}
    assert api.set_window_activity(True) == {"ok": True, "active": True, "updated": True}
    assert calls == [False, True]
