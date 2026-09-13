from __future__ import annotations

import json
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem
from pudge.manga import MangaService
from pudge.manga_ocr_artifact import read_artifact, write_artifact


class _ExitedProcess:
    def poll(self):
        return 0


def _audiobooks(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "audio.sqlite3"),
        ffprobe="ffprobe",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )


def test_old_audiobook_monitor_cannot_cleanup_new_session(tmp_path: Path) -> None:
    service = _audiobooks(tmp_path)
    book_id = 7
    old_process = _ExitedProcess()
    new_process = SimpleNamespace(poll=lambda: None)
    ipc_dir = tmp_path / "cache" / "audiobook-ipc"
    ipc_dir.mkdir(parents=True, exist_ok=True)
    old_ipc = ipc_dir / "old.sock"
    new_ipc = ipc_dir / "new.sock"
    old_ipc.write_text("old", encoding="utf-8")
    new_ipc.write_text("new", encoding="utf-8")

    with service._lock:
        service._players[book_id] = new_process
        service._ipc_paths[book_id] = new_ipc
        service._playback_sessions[book_id] = "session-b"
        service._last_positions[book_id] = 41.0
        service._sleep_deadlines[book_id] = time.monotonic() + 60
        service._sleep_chapter_ends[book_id] = 90.0

    service._monitor(book_id, old_process, old_ipc, "session-a")

    with service._lock:
        assert service._players[book_id] is new_process
        assert service._ipc_paths[book_id] == new_ipc
        assert service._playback_sessions[book_id] == "session-b"
        assert service._last_positions[book_id] == 41.0
        assert book_id in service._sleep_deadlines
        assert book_id in service._sleep_chapter_ends
    assert not old_ipc.exists()
    assert new_ipc.exists()


class _FailingClient:
    def torrents(self, **_kwargs):
        raise RuntimeError("backend unavailable")

    def close(self):
        return None


class _StaticClient:
    def __init__(self, items: list[DownloadItem]):
        self.items = items

    def torrents(self, **_kwargs):
        return list(self.items)

    def close(self):
        return None


def _download(hash_value: str, *, backend: str, progress: float = 0.1) -> DownloadItem:
    return DownloadItem(
        torrent_hash=hash_value,
        name=f"{backend}-{hash_value}",
        state="downloading",
        progress=progress,
        save_path="",
        content_path="",
        media_id=None,
        episode=None,
        media_episode=None,
        release_episode=None,
        is_batch=False,
        added_on=1,
        completed_on=0,
        raw={"backend": backend, "_backends": [backend]},
    )


def test_partial_backend_failure_does_not_prune_other_backend_rows(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)

    qbt_item = _download("qbt-only", backend="qbittorrent")
    aria_item = _download("aria-live", backend="aria2")
    manager.db.upsert_download(qbt_item)

    monkeypatch.setattr(manager, "downloads_configured", lambda: True)
    monkeypatch.setattr(manager, "downloads_enabled", lambda: True)
    monkeypatch.setattr(
        manager,
        "torrent_clients",
        lambda: [
            ("qbittorrent", _FailingClient()),
            ("aria2", _StaticClient([aria_item])),
        ],
    )
    monkeypatch.setattr(
        manager, "_discard_completed_aria2_recovery_tasks", lambda *_args: 0
    )
    monkeypatch.setattr(manager, "_recover_stalled_aria2_downloads", lambda *_args: 0)
    monkeypatch.setattr(
        manager, "_discard_watched_empty_aria2_shells", lambda _client, items: items
    )
    monkeypatch.setattr(manager, "_remove_missing_episode_rows", lambda _hashes: 0)
    monkeypatch.setattr(manager, "_resolve_download_media", lambda _item: None)
    monkeypatch.setattr(manager, "_recover_stale_aria2_zero_shell", lambda _item: None)
    monkeypatch.setattr(
        manager, "_stale_aria2_row_shadowed_by_completed_local", lambda *_args: False
    )
    monkeypatch.setattr(
        manager, "_repair_stale_completed_current_download_intents", lambda _items: 0
    )

    manager.sync_downloads()

    assert manager.db.download_by_hash("qbt-only") is not None
    assert manager.db.download_by_hash("aria-live") is not None


def _write_cbz(path: Path, value: int) -> None:
    image_path = path.with_suffix(".png")
    Image.new("RGB", (24 + value, 24), "white").save(image_path)
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(image_path, arcname="001.png")
    image_path.unlink()


def test_changed_manga_source_invalidates_ready_status_and_stale_generation(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "manga.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    archive = tmp_path / "book.cbz"
    _write_cbz(archive, 0)
    first = service.import_file(archive)
    book_id = int(first["id"])
    fingerprint, generation, _revision = service._ocr_context(book_id)

    assert service._commit_ocr_page_updates(
        book_id,
        source_fingerprint=fingerprint,
        generation=generation,
        updates=[(0, [{"text": "old"}], "ready", "", False)],
    )
    assert service.ocr_cache_status(book_id)["complete"] is True

    time.sleep(0.002)
    _write_cbz(archive, 3)
    service.import_file(archive)

    status = service.ocr_cache_status(book_id)
    assert status["complete"] is False
    assert status["completed_pages"] == 0
    assert service.text_regions(book_id, 0, cached_only=True)["status"] == "missing"

    assert not service._commit_ocr_page_updates(
        book_id,
        source_fingerprint=fingerprint,
        generation=generation,
        updates=[(0, [{"text": "stale"}], "ready", "", False)],
    )
    assert service.text_regions(book_id, 0, cached_only=True)["status"] == "missing"


def test_concurrent_artifact_writers_use_independent_tempfiles(tmp_path: Path) -> None:
    path = tmp_path / "artifact.json"
    barrier = threading.Barrier(8)
    failures: list[BaseException] = []

    def publish(index: int) -> None:
        try:
            barrier.wait(timeout=3)
            write_artifact(path, {"writer": index, "pages": []})
        except BaseException as exc:  # pragma: no cover - failure is asserted below
            failures.append(exc)

    threads = [threading.Thread(target=publish, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["writer"] in range(8)
    assert not list(tmp_path.glob("*.tmp"))


def test_artifact_revision_matches_current_ocr_projection(tmp_path: Path) -> None:
    db = Database(tmp_path / "manga.sqlite3")
    service = MangaService(db, cache_dir=tmp_path / "cache")
    archive = tmp_path / "book.cbz"
    _write_cbz(archive, 0)
    book_id = int(service.import_file(archive)["id"])
    fingerprint, generation, _revision = service._ocr_context(book_id)

    assert service._commit_ocr_page_updates(
        book_id,
        source_fingerprint=fingerprint,
        generation=generation,
        updates=[(0, [{"text": "current"}], "ready", "", False)],
    )
    current_fingerprint, current_generation, current_revision = service._ocr_context(
        book_id
    )
    artifact = read_artifact(service._ocr_artifact_path(book_id))
    assert artifact is not None
    assert artifact["source"]["fingerprint"] == current_fingerprint
    assert int(artifact["generation"]) == current_generation
    assert int(artifact["revision"]) == current_revision


def test_subtitle_history_cross_path_recovery_requires_same_video_content(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "library.sqlite3")
    old_video = tmp_path / "old" / "01.mkv"
    moved_video = tmp_path / "moved" / "01.mkv"
    other_video = tmp_path / "other" / "01.mkv"
    different_encode = tmp_path / "encode" / "episode.mkv"
    subtitle = tmp_path / "selected.srt"
    for path in (old_video, moved_video, other_video, different_encode):
        path.parent.mkdir(parents=True, exist_ok=True)
    old_video.write_bytes(b"A" * 4096 + b"same-content")
    moved_video.write_bytes(old_video.read_bytes())
    other_video.write_bytes(b"B" * 4096 + b"different-anime")
    different_encode.write_bytes(b"C" * 4096 + b"different-encode")
    subtitle.write_text("subtitle", encoding="utf-8")

    db.record_subtitle_history(
        video_path=old_video,
        media_id=42,
        episode=1,
        source="jimaku",
        candidate_name=subtitle.name,
        candidate_path=subtitle,
        status="selected",
    )

    moved = db.latest_selected_subtitle_for_media_or_filename(
        video_path=moved_video,
        media_id=42,
        episode=1,
    )
    assert moved is not None
    assert moved["candidate_path"] == str(subtitle)

    assert (
        db.latest_selected_subtitle_for_media_or_filename(
            video_path=other_video,
            media_id=999,
            episode=1,
        )
        is None
    )
    assert (
        db.latest_selected_subtitle_for_media_or_filename(
            video_path=different_encode,
            media_id=42,
            episode=1,
        )
        is None
    )
