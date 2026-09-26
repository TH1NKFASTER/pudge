from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.companion_streaming import CompanionStreamingService, _Job
from pudge.web_app import WebAppApi


def _stream(tmp_path: Path) -> CompanionStreamingService:
    return CompanionStreamingService(SimpleNamespace(), cache_dir=tmp_path)


def _old(path: Path) -> None:
    os.utime(path, (1, 1))


def test_active_ticket_and_pending_prepare_are_not_evicted(tmp_path: Path) -> None:
    service = _stream(tmp_path)
    active = service.cache_root / "active"
    preparing = service.cache_root / "preparing"
    expired = service.cache_root / "expired"
    orphan = service.cache_root / "orphan"
    for path in (active, preparing, expired, orphan):
        path.mkdir()
        (path / "segment-00000.ts").write_bytes(b"video")
        _old(path)
    service._issue_ticket(cache_key="active", entity_id="episode", output_dir=active)
    service._issue_ticket(cache_key="expired", entity_id="episode", output_dir=expired)
    with service._lock:
        service._preparing_cache_keys.add("preparing")
        next(item for item in service._tickets.values() if item.cache_key == "expired").expires_at = 0
    service.cleanup_cache()
    assert active.exists()
    assert preparing.exists()
    assert not expired.exists()
    assert not orphan.exists()
    service.close()


def test_media_ticket_cannot_be_used_after_close(tmp_path: Path) -> None:
    service = _stream(tmp_path)
    directory = service.cache_root / "media"
    directory.mkdir()
    (directory / "segment-00000.ts").write_bytes(b"video")
    ticket = service._issue_ticket(cache_key="media", entity_id="episode", output_dir=directory)
    assert service.media_path(ticket.ticket, "segment-00000.ts")[0].is_file()
    service.close()
    with pytest.raises(ValueError, match="expired"):
        service.media_path(ticket.ticket, "segment-00000.ts")
    with pytest.raises(ValueError, match="shutting down"):
        service._issue_ticket(cache_key="media", entity_id="episode", output_dir=directory)


def test_transcoder_spawn_and_shutdown_do_not_leave_orphan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from pudge import companion_streaming as module

    service = _stream(tmp_path)
    source = tmp_path / "episode.mkv"
    source.write_bytes(b"video")
    output_dir = service.cache_root / "media"
    job = _Job("media", "episode", source, output_dir)
    service._jobs["media"] = job
    spawn_started = threading.Event()
    allow_spawn = threading.Event()
    process_stopped = threading.Event()

    class Process:
        returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15
            process_stopped.set()

        def kill(self) -> None:
            self.returncode = -9
            process_stopped.set()

        def wait(self, timeout: int) -> int:
            assert process_stopped.wait(timeout)
            return self.returncode or 0

        def communicate(self) -> tuple[bytes, bytes]:
            assert process_stopped.wait(3)
            return b"", b"terminated"

    def start(_command: list[str], **_kwargs: object) -> Process:
        spawn_started.set()
        assert allow_spawn.wait(3)
        return Process()

    monkeypatch.setattr(module.subprocess, "Popen", start)
    monkeypatch.setattr(service, "_stream_copy_compatible", lambda _: False)
    monkeypatch.setattr(service, "_command", lambda *args: ["dummy"])
    worker = threading.Thread(target=service._run_job, args=(job, {}))
    worker.start()
    assert spawn_started.wait(3)
    shutdown = threading.Thread(target=service.close)
    shutdown.start()
    allow_spawn.set()
    worker.join(timeout=4)
    shutdown.join(timeout=4)
    assert not worker.is_alive() and not shutdown.is_alive()
    assert process_stopped.is_set()
    assert job.state == "cancelled"


def test_install_status_preserves_model_download_failure(tmp_path: Path) -> None:
    api = WebAppApi.__new__(WebAppApi)
    api._manga_ocr_install_lock = threading.Lock()
    api._manga_ocr_install_state = {"state": "failed", "detail": "model warm-up failed"}
    api._manga_ocr_install_thread = None
    api.manga = SimpleNamespace(ocr_available=lambda **kwargs: True)
    api._manga_ocr_marker_path = lambda: tmp_path / "missing-marker"
    api._manga_ocr_log_path = lambda: tmp_path / "install.log"
    status = api.manga_ocr_status()
    assert status["state"] == "failed"
    assert status["detail"] == "model warm-up failed"
    assert status["installed"] is True and status["model_ready"] is False


def test_completed_cache_does_not_mask_failed_or_partial_ocr_job() -> None:
    api = WebAppApi.__new__(WebAppApi)
    api._manga_book_ocr_lock = threading.Lock()
    api._manga_book_ocr_threads = {}
    api._manga_book_ocr_state = {1: {"state": "failed", "errors": ["worker error"]}}
    api.manga = SimpleNamespace(
        ocr_cache_status=lambda _: {
            "complete": True, "total_pages": 2, "completed_pages": 2,
            "failed_pages": 0, "cached_pages": 2,
        }
    )
    status = api.manga_ocr_book_status(1)
    assert status["state"] == "failed"
    assert status["errors"] == ["worker error"]
    api._manga_book_ocr_state[1] = {"state": "partial", "errors": ["Jiten parser error"]}
    assert api.manga_ocr_book_status(1)["state"] == "partial"


def test_partial_ocr_run_does_not_mark_job_succeeded() -> None:
    api = WebAppApi.__new__(WebAppApi)
    api._manga_book_ocr_lock = threading.Lock()
    api._manga_book_ocr_state = {}
    api._manga_ocr_cancel_events = {}
    api._manga_ocr_job_ids = {1: "job"}
    api.logger = SimpleNamespace(info=lambda *args: None, exception=lambda *args: None)
    results: list[tuple[str, str]] = []
    api.job_center = SimpleNamespace(
        fail=lambda _id, exc, **kwargs: results.append(("failed", str(exc))),
        finish=lambda _id, **kwargs: results.append(("succeeded", "")),
    )
    api.manga = SimpleNamespace(
        ocr_book=lambda *args, **kwargs: {"complete": False, "cached_pages": 1, "errors": ["worker error"]},
        cached_region_texts=lambda _book_id: [],
    )
    api._run_manga_book_ocr(1)
    assert results == [("failed", "worker error")]
    assert api._manga_book_ocr_state[1]["state"] == "partial"


def test_cache_registry_respects_active_companion_stream(tmp_path: Path) -> None:
    from pudge.cache_registry import CachePolicy, CacheRegistry
    from pudge.database import Database

    db = Database(tmp_path / "db.sqlite3")
    registry = CacheRegistry(db, tmp_path / "cache")
    active = tmp_path / "cache" / "companion-hls" / "active"
    orphan = tmp_path / "cache" / "companion-hls" / "orphan"
    for path in (active, orphan):
        path.mkdir(parents=True)
        (path / "segment-00000.ts").write_bytes(b"video" * 30)
        registry.register("hls", path)
    result = registry.enforce(
        {"hls": CachePolicy(max_bytes=10)}, protected_paths={active}
    )
    assert result["removed"] == 1
    assert active.exists() and not orphan.exists()
