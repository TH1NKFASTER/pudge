from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from pudge import manga as manga_module
from pudge.config import AppConfig
from pudge.database import Database
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.manga import MangaService
from pudge.task_supervisor import TaskSupervisor
from pudge.web_app import WebAppApi
from pudge.work_scheduler import WorkPriority, WorkScheduler


def test_debug_fresh_releases_exactly_one_priority_request(tmp_path: Path) -> None:
    scheduler = WorkScheduler(tmp_path / "scheduler")
    fake = SimpleNamespace(
        manager=SimpleNamespace(
            work_scheduler=scheduler,
            force_fresh_subtitle_selection=lambda _video: {"ok": True},
            process_subtitle_jobs=lambda **_kwargs: 0,
        ),
        task_supervisor=SimpleNamespace(start=lambda **kwargs: kwargs["target"]()),
        logger=SimpleNamespace(exception=lambda *_args: None),
    )

    WebAppApi.debug_reselect_subtitles(fake, str(tmp_path / "video.mkv"))

    assert not scheduler._priority_requests
    assert not scheduler.should_yield_to_higher_priority(WorkPriority.BACKGROUND)
    lease = scheduler.acquire_heavy("probe", foreground_sensitive=False)
    assert lease is not None
    lease.release()


def test_debug_fresh_releases_priority_when_supervisor_start_fails(tmp_path: Path) -> None:
    scheduler = WorkScheduler(tmp_path / "scheduler")

    def fail_start(**_kwargs):
        raise RuntimeError("boom")

    fake = SimpleNamespace(
        manager=SimpleNamespace(
            work_scheduler=scheduler,
            force_fresh_subtitle_selection=lambda _video: {"ok": True},
            process_subtitle_jobs=lambda **_kwargs: 0,
        ),
        task_supervisor=SimpleNamespace(start=fail_start),
        logger=SimpleNamespace(exception=lambda *_args: None),
    )

    try:
        WebAppApi.debug_reselect_subtitles(fake, str(tmp_path / "video.mkv"))
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:  # pragma: no cover
        raise AssertionError("expected supervisor failure")
    assert not scheduler._priority_requests


def test_stale_subtitle_owner_cannot_commit_over_fresh_generation(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    db.upsert_episode(LibraryEpisode(media_id=None, title="test", episode=1, video_path=video))
    db.queue_subtitle_job(video, None, 1)

    first = db.claim_due_subtitle_jobs(1)
    assert len(first) == 1
    generation_a = int(first[0]["generation"])
    owner_a = str(first[0]["owner_token"])
    assert owner_a

    db.invalidate_subtitle(video, None, 1, "Fresh B")
    current = db.subtitle_jobs()[0]
    assert int(current["generation"]) == generation_a + 1
    assert str(current["owner_token"] or "") == ""

    old_subtitle = tmp_path / "old.srt"
    old_subtitle.write_text("old", encoding="utf-8")
    assert not db.set_subtitle_ready(
        video,
        old_subtitle,
        None,
        origin="old-worker-A",
        generation=generation_a,
        owner_token=owner_a,
    )
    assert db.subtitle_jobs()
    assert db.episode_by_path(video).state == "waiting_subtitles"
    assert db.episode_by_path(video).subtitle_path is None

    second = db.claim_due_subtitle_jobs(1)
    assert len(second) == 1
    generation_b = int(second[0]["generation"])
    owner_b = str(second[0]["owner_token"])
    assert generation_b == generation_a + 1
    assert owner_b and owner_b != owner_a
    assert not db.postpone_subtitle_job(
        video,
        "stale A",
        60,
        generation=generation_a,
        owner_token=owner_a,
    )

    fresh_subtitle = tmp_path / "fresh.srt"
    fresh_subtitle.write_text("fresh", encoding="utf-8")
    assert db.set_subtitle_ready(
        video,
        fresh_subtitle,
        None,
        origin="fresh-worker-B",
        generation=generation_b,
        owner_token=owner_b,
    )
    assert not db.subtitle_jobs()
    assert db.episode_by_path(video).subtitle_path == fresh_subtitle


def test_manager_ready_commit_carries_claim_generation_and_owner(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    manager = AnimeManager(cfg, log=lambda _message: None)
    video = tmp_path / "Example - 01.mkv"
    prepared = tmp_path / "prepared.srt"
    video.write_bytes(b"video")
    prepared.write_text("subtitle", encoding="utf-8")
    manager.db.upsert_anime(
        LibraryAnime(media_id=42, title="Example", episodes=1, format="TV")
    )
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=42,
            title="Example",
            episode=1,
            video_path=video.resolve(),
            state="waiting_subtitles",
        )
    )
    manager.db.queue_subtitle_job(video.resolve(), 42, 1)

    class FakeProcess:
        def __init__(self, _command, **_kwargs):
            self.returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self):
            return f"PREPARED_SUBTITLE={prepared}\nPREPARE_STATUS=ready\n", ""

    monkeypatch.setattr("pudge.manager.subprocess.Popen", FakeProcess)
    commits: list[dict[str, object]] = []
    original = manager.db.set_subtitle_ready

    def capture_ready(*args, **kwargs):
        commits.append(dict(kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(manager.db, "set_subtitle_ready", capture_ready)
    assert manager.process_subtitle_jobs(limit=1) == 1
    assert commits
    owner_commit = commits[-1]
    assert isinstance(owner_commit.get("generation"), int)
    assert str(owner_commit.get("owner_token") or "")
    assert manager.db.episode_by_path(video.resolve()).subtitle_path == prepared


def test_scheduler_filesystem_failure_does_not_leak_local_lock(tmp_path: Path) -> None:
    cache = tmp_path / "scheduler-cache"
    cache.write_text("not a directory", encoding="utf-8")
    scheduler = WorkScheduler(cache)
    try:
        scheduler.acquire_heavy("broken", foreground_sensitive=False)
    except OSError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected filesystem failure")
    assert not scheduler._local_lock.locked()

    cache.unlink()
    lease = scheduler.acquire_heavy("recovered", foreground_sensitive=False)
    assert lease is not None
    lease.release()


def test_task_supervisor_drains_verbose_child_without_pipe_deadlock() -> None:
    supervisor = TaskSupervisor()
    try:
        code, stdout, stderr = supervisor.run_process(
            "verbose",
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x'*1048576); "
                "sys.stderr.buffer.write(b'y'*1048576); sys.stdout.flush(); sys.stderr.flush()",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        assert code == 0
        assert len(stdout) == 1048576
        assert len(stderr) == 1048576
        assert supervisor._processes == {}
    finally:
        supervisor.shutdown()


class _Lease:
    def release(self) -> None:
        return None


class _YieldScheduler:
    def __init__(self) -> None:
        self.yield_now = False
        self.yield_after_first = True

    def wait_until_background(self, **_kwargs) -> bool:
        return True

    def acquire_heavy(self, *_args, **_kwargs):
        return _Lease()

    def should_yield_to_higher_priority(self, _priority) -> bool:
        return self.yield_now


def test_volume_ocr_preemption_checkpoints_completed_pages_and_resumes(
    tmp_path: Path, monkeypatch
) -> None:
    db = Database(tmp_path / "library.sqlite3")
    archive = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive, "w") as zf:
        for index in range(2):
            image_path = tmp_path / f"{index}.png"
            Image.new("RGB", (24, 24), "white").save(image_path)
            zf.write(image_path, arcname=f"{index}.png")
    now = time.time()
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(archive), "Book", 2, 0, "rtl", now, now),
        )
        book_id = int(cursor.lastrowid)

    scheduler = _YieldScheduler()
    service = MangaService(db, cache_dir=tmp_path / "cache", work_scheduler=scheduler)
    monkeypatch.setattr(service, "ocr_available", lambda **_kwargs: True)
    monkeypatch.setattr(service, "_vision_text_regions", lambda _image: [])
    manifests: list[list[int]] = []

    class FakePopen:
        def __init__(self, command, **_kwargs):
            self.command = list(command)
            self.returncode = None
            manifest_path = Path(self.command[-4])
            output_path = Path(self.command[-3])
            progress_path = Path(self.command[-2])
            stop_path = Path(self.command[-1])
            pages = json.loads(manifest_path.read_text(encoding="utf-8"))["pages"]
            manifests.append([int(item["page_index"]) for item in pages])

            def run() -> None:
                output_path.write_text("", encoding="utf-8")
                with output_path.open("a", encoding="utf-8") as output:
                    for done, item in enumerate(pages, start=1):
                        page_index = int(item["page_index"])
                        output.write(json.dumps({"page_index": page_index, "regions": []}) + "\n")
                        output.flush()
                        progress_path.write_text(
                            json.dumps({"done": done, "total": len(pages), "page_index": page_index}),
                            encoding="utf-8",
                        )
                        if scheduler.yield_after_first and done == 1:
                            scheduler.yield_now = True
                        deadline = time.monotonic() + 2
                        while scheduler.yield_after_first and done == 1 and not stop_path.exists():
                            if time.monotonic() >= deadline:
                                self.returncode = 2
                                return
                            time.sleep(0.01)
                        if stop_path.exists():
                            self.returncode = 75
                            return
                self.returncode = 0

            self.thread = threading.Thread(target=run, daemon=True)
            self.thread.start()

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.thread.join(timeout)
            if self.thread.is_alive():
                raise subprocess.TimeoutExpired(self.command, timeout)
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(manga_module.subprocess, "Popen", FakePopen)

    first = service.ocr_book(book_id)
    assert first["preempted"] is True
    assert first["cached_pages"] == 1
    assert first["complete"] is False
    assert manifests == [[0, 1]]

    scheduler.yield_now = False
    scheduler.yield_after_first = False
    second = service.ocr_book(book_id)
    assert second["ok"] is True
    assert second["complete"] is True
    assert second["cached_pages"] == 2
    assert manifests == [[0, 1], [1]]
