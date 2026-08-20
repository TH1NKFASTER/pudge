from __future__ import annotations

import json
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.alignment_replay import evaluate_alignment_trace, replay_file
from pudge.cache_registry import CachePolicy, CacheRegistry
from pudge.database import Database, LATEST_SCHEMA_VERSION
from pudge.diagnostics import DebugBundleBuilder, DiagnosticRecorder
from pudge.identity import IdentityResolver, MediaIdentity
from pudge.job_center import JobCenter
from pudge.manager_models import LibraryAnime
from pudge.providers.base import CircuitBreaker, TorrentBackend
from pudge.providers.qbittorrent import QBittorrentClient
from pudge.safe_mode import SafeModeController
from pudge.task_supervisor import TaskSupervisor
from pudge.web_app import WebAppApi
from pudge.web_state import UIStateSnapshotCache


def test_database_enables_foreign_keys_on_every_connection(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.sqlite3")
    with database.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        try:
            connection.execute(
                "INSERT INTO episodes(media_id,title,episode,video_path,state,updated_at) "
                "VALUES(999,'orphan',1,'/tmp/orphan.mkv','waiting',0)"
            )
        except sqlite3.IntegrityError:
            pass
        else:  # pragma: no cover - protects the invariant if SQLite settings regress.
            raise AssertionError("orphan episode was accepted")


def test_migration_creates_consistent_pre_upgrade_backup(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    database = Database(path)
    database.set_state("sentinel", "before")
    with database.connect() as connection:
        connection.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION - 1}")

    migrated = Database(path)
    backup = path.with_name(f"{path.name}.pre-v{LATEST_SCHEMA_VERSION}.backup")

    assert backup.is_file()
    assert migrated.get_state("sentinel") == "before"
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION - 1


def test_failed_migration_rolls_back_all_schema_changes(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "library.sqlite3"
    database = Database(path)
    with database.connect() as connection:
        connection.execute(f"PRAGMA user_version={LATEST_SCHEMA_VERSION - 1}")

    def fail_migration(_self: Database, connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE should_rollback(value INTEGER)")
        raise sqlite3.OperationalError("simulated migration failure")

    monkeypatch.setattr(Database, "_migrate_v7", fail_migration)
    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        Database(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == LATEST_SCHEMA_VERSION - 1
        assert (
            connection.execute("SELECT 1 FROM sqlite_master WHERE name='should_rollback'").fetchone() is None
        )


def test_task_supervisor_cancels_owned_worker() -> None:
    supervisor = TaskSupervisor()
    stopped = threading.Event()

    def worker(cancel_event: threading.Event) -> None:
        cancel_event.wait(2)
        stopped.set()

    supervisor.start("worker", worker, pass_cancel_event=True)
    supervisor.shutdown(timeout=2)

    assert stopped.wait(0.2)
    assert supervisor.status()[0]["running"] is False


def test_resumable_job_keeps_checkpoint_after_interruption(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.sqlite3")
    center = JobCenter(database)
    job_id = center.start("stt", "Transcribe", resumable=True, correlation_id="audio:7")
    center.update(job_id, state="running", current=4, total=10)
    center.checkpoint(job_id, {"file": 2})

    recovered = JobCenter(database).resume_candidates(kind="stt")

    assert recovered[0]["id"] == job_id
    assert recovered[0]["state"] == "queued"
    assert recovered[0]["checkpoint"] == {"file": 2}


def test_locked_media_identity_cannot_be_reassigned(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.sqlite3")
    database.upsert_anime(LibraryAnime(media_id=1, title="One", status="CURRENT"))
    resolver = IdentityResolver(database)
    resolver.record(MediaIdentity(1, 3, release_episode=13, source="manual", locked=True))
    resolver.record(MediaIdentity(1, 3, release_episode=99, source="scan"))

    with database.connect() as connection:
        row = connection.execute(
            "SELECT release_episode,locked FROM media_identity_ledger WHERE canonical_id='anime:1:episode:3'"
        ).fetchone()
    assert dict(row) == {"release_episode": 13, "locked": 1}


def test_cache_registry_enforces_lru_quota_inside_cache_root(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.sqlite3")
    root = tmp_path / "cache"
    first = root / "first.bin"
    second = root / "second.bin"
    first.parent.mkdir()
    first.write_bytes(b"a" * 10)
    second.write_bytes(b"b" * 10)
    registry = CacheRegistry(database, root)
    registry.register("test", first)
    time.sleep(0.002)
    registry.register("test", second)

    result = registry.enforce({"test": CachePolicy(max_bytes=10)})

    assert result == {"removed": 1, "removed_bytes": 10}
    assert first.exists() is False
    assert second.is_file()


def test_circuit_breaker_opens_and_recovers() -> None:
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=5)
    breaker.failure(now=10)
    assert breaker.allow(now=11)
    breaker.failure(now=12)
    assert breaker.allow(now=13) is False
    assert breaker.allow(now=17) is True
    breaker.success()
    assert breaker.failures == 0


def test_alignment_replay_reports_jumps_and_error_percentiles() -> None:
    metrics = evaluate_alignment_trace(
        [
            {"sourceOffset": 0, "renderedOffset": 0},
            {"sourceOffset": 1, "renderedOffset": 1.2},
            {"sourceOffset": 2, "renderedOffset": 0.8},
            {"sourceOffset": 6, "renderedOffset": 6},
        ]
    )
    assert metrics.samples == 4
    assert metrics.backward_jumps == 1
    assert metrics.large_forward_jumps == 1
    assert metrics.maximum_error == 1.2


def test_alignment_golden_replay_stays_inside_error_budget() -> None:
    fixture = Path(__file__).parent / "fixtures" / "alignment_traces" / "silence_transition.json"

    metrics = replay_file(fixture)

    assert metrics.samples == 8
    assert metrics.median_error <= 0.08
    assert metrics.p95_error <= 0.15
    assert metrics.backward_jumps == 0
    assert metrics.large_forward_jumps == 0


def test_qbittorrent_implements_torrent_backend_protocol() -> None:
    backend = QBittorrentClient("http://127.0.0.1:8080", auto_start_app=False)
    try:
        assert isinstance(backend, TorrentBackend)
        assert backend.backend_name == "qbittorrent"
    finally:
        backend.close()


def test_debug_bundle_redacts_secrets_and_includes_final_subtitle(tmp_path: Path) -> None:
    database = Database(tmp_path / "library.sqlite3")
    recorder = DiagnosticRecorder(database)
    recorder.record("trace", "test", "prepared", payload={"api_key": "hidden"})
    subtitle = tmp_path / "final-ja.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
    target = tmp_path / "debug.zip"

    DebugBundleBuilder(database, recorder).build(
        target,
        version="0.7.22",
        frontend={"token": "hidden"},
        snapshots=[{"selected": {"final_path": str(subtitle)}}],
    )

    with zipfile.ZipFile(target) as archive:
        manifest = archive.read("manifest.json").decode()
        events = archive.read("diagnostic-events.json").decode()
        names = archive.namelist()
    assert "hidden" not in manifest
    assert "hidden" not in events
    assert any(name.endswith("final-ja.srt") for name in names)


def test_safe_mode_does_not_interrupt_launch_after_one_stale_session(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "app-session.json"
    session.write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
    monkeypatch.setattr("pudge.safe_mode.SESSION_PATH", session)
    database = tmp_path / "library.sqlite3"
    Database(database)
    controller = SafeModeController(tmp_path / "cache", database)

    assert controller.begin() is False
    status = controller.status()
    assert status["active"] is False
    assert status["crash_count"] == 1
    assert status["startup_database_check"] == "ok"


def test_safe_mode_detects_repeated_interruption_and_offers_backup(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "app-session.json"
    session.write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
    monkeypatch.setattr("pudge.safe_mode.SESSION_PATH", session)
    database = tmp_path / "library.sqlite3"
    Database(database)
    backup = database.with_name(database.name + f".pre-v{LATEST_SCHEMA_VERSION}.backup")
    backup.write_bytes(database.read_bytes())
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "safe-mode-state.json").write_text(
        json.dumps({"crash_count": 1}),
        encoding="utf-8",
    )
    controller = SafeModeController(cache, database)

    assert controller.begin() is True
    status = controller.status()
    assert status["reason"] == "repeated_interruption"
    assert status["crash_count"] == 2
    assert status["background_paused"] is True
    assert status["checks"]["database"] == "ok"
    assert status["migration_backup"] == str(backup)


def test_safe_mode_blocks_foreground_download_and_subtitle_poll() -> None:
    api = WebAppApi.__new__(WebAppApi)
    api.safe_mode = SimpleNamespace(active=True)
    api.get_state_fast = lambda: {"safe_mode": {"active": True}}
    api.manager = SimpleNamespace(sync_downloads=lambda: pytest.fail("safe mode must not touch downloads"))

    result = api.poll_downloads_and_subtitles()

    assert result["skipped"] is True
    assert result["safe_mode"] is True
    assert result["stats"] == {}


def test_safe_mode_frontend_never_shows_monitoring_banner() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")

    assert "function safeModeActive()" in html
    assert "visible=!!show&&!safeModeActive()" in html
    assert "if(safeModeActive()){showStartupBanner(false);setStatus(t('status.ready'));return;}" in html
    assert "if(safeModeActive()||ui.foregroundPolling||ui.foregroundPollTimer)return" in html


def test_ui_snapshot_cache_returns_only_matching_version() -> None:
    cache = UIStateSnapshotCache()
    payload = {"current": [1]}
    cache.store("7", payload)
    assert cache.get("7") is payload
    assert cache.get("8") is None
    assert cache.delta("7")["changed"] is False
