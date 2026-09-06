from __future__ import annotations

from pathlib import Path

from PIL import Image

from pudge import ocr
from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.cache_dir.mkdir()
    cfg.paths.download_dirs = []
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.nyaa.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    return AnimeManager(cfg)


class _TestHeavyLease:
    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None


def _allow_heavy_scan(manager: AnimeManager, monkeypatch) -> None:
    """Make scan-cache tests independent of host battery/thermal state."""
    monkeypatch.setattr(
        manager.work_scheduler,
        "acquire_heavy",
        lambda *_args, **_kwargs: _TestHeavyLease(),
    )


def test_ocr_streams_compositions_without_materializing_display_list(
    tmp_path: Path, monkeypatch
) -> None:
    video = tmp_path / "movie.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "movie.sup"
    subtitle.write_bytes(b"PGS placeholder")
    images = [Image.new("RGBA", (8, 4), (255, 255, 255, 255)) for _ in range(2)]

    def displays(_path: Path):
        yield 1.0, images[0]
        yield 2.0, None
        yield 3.0, images[1]
        yield 4.0, None

    monkeypatch.setattr(ocr, "iter_pgs_compositions", displays)
    monkeypatch.setattr(
        ocr,
        "decode_pgs_compositions",
        lambda _path: (_ for _ in ()).throw(AssertionError("OCR must not build a display list")),
    )
    monkeypatch.setattr(ocr, "_vision_recognize", lambda _image: "こんにちは")

    output, result = ocr.image_subtitle_to_srt(
        video,
        tmp_path / "cache",
        subtitle_path=subtitle,
    )

    assert output is not None
    assert result["display_count"] == 4
    assert result["cue_count"] == 2
    assert "こんにちは" in output.read_text(encoding="utf-8")


def test_pgs_segment_reader_streams_file_without_path_read_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "one.sup"
    payload = b"abc"
    path.write_bytes(
        b"PG"
        + (90_000).to_bytes(4, "big")
        + (0).to_bytes(4, "big")
        + bytes([0x80])
        + len(payload).to_bytes(2, "big")
        + payload
    )
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _self: (_ for _ in ()).throw(AssertionError("streaming reader used read_bytes")),
    )

    rows = list(ocr._iter_pgs_file_segments(path))

    assert len(rows) == 1
    assert rows[0].payload == payload
    assert rows[0].time_seconds == 1.0


def test_library_scan_fingerprint_reuses_recent_unchanged_scan(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    _allow_heavy_scan(manager, monkeypatch)
    video = manager.config.library.root_dir / "Example - 01.mkv"
    video.write_bytes(b"video")
    calls: list[str] = []

    def full_scan():
        calls.append("scan")
        row = LibraryEpisode(None, "Example", 1, video.resolve(), state="local")
        manager.db.upsert_episode(row)
        return [manager.db.episode_by_path(video.resolve()) or row]

    monkeypatch.setattr(manager, "_scan_library_uncached", full_scan)

    first = manager.scan_library(reuse_unchanged=True)
    second = manager.scan_library(reuse_unchanged=True)

    assert len(first) == len(second) == 1
    assert calls == ["scan"]


def test_library_scan_fingerprint_invalidates_when_sidecar_changes(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    _allow_heavy_scan(manager, monkeypatch)
    video = manager.config.library.root_dir / "Example - 01.mkv"
    video.write_bytes(b"video")
    calls: list[str] = []

    def full_scan():
        calls.append("scan")
        row = LibraryEpisode(None, "Example", 1, video.resolve(), state="local")
        manager.db.upsert_episode(row)
        return [manager.db.episode_by_path(video.resolve()) or row]

    monkeypatch.setattr(manager, "_scan_library_uncached", full_scan)
    manager.scan_library(reuse_unchanged=True)
    (manager.config.library.root_dir / "Example - 01.srt").write_text("subtitle", encoding="utf-8")
    manager.scan_library(reuse_unchanged=True)

    assert calls == ["scan", "scan"]


def test_library_scan_does_not_start_over_existing_heavy_ocr(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "Example - 01.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(LibraryEpisode(None, "Example", 1, video.resolve(), state="local"))
    monkeypatch.setattr(manager.work_scheduler, "acquire_heavy", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        manager,
        "_scan_library_uncached",
        lambda: (_ for _ in ()).throw(AssertionError("heavy scan must not start")),
    )

    rows = manager.scan_library(reuse_unchanged=True, user_requested=True)

    assert [row.video_path for row in rows] == [video.resolve()]


def test_recent_interactive_refresh_suppresses_delayed_startup_maintenance(
    tmp_path: Path, monkeypatch
) -> None:
    import time

    manager = _manager(tmp_path)
    import json
    import os
    manager.db.set_state(
        "interactive_refresh_completed_at_v39",
        json.dumps({"pid": os.getpid(), "completed_at": time.time()}),
    )
    monkeypatch.setattr(
        manager,
        "_run_startup_once_unlocked",
        lambda: (_ for _ in ()).throw(AssertionError("startup maintenance must be skipped")),
    )

    stats = manager.run_startup_once()

    assert stats["library"] == 0


def test_successful_interactive_refresh_marks_recent_completion(
    tmp_path: Path, monkeypatch
) -> None:
    manager = _manager(tmp_path)
    monkeypatch.setattr(
        manager,
        "_run_once_unlocked",
        lambda **_kwargs: manager._maintenance_stats(),
    )

    manager.run_interactive_refresh(pre_scanned_library_count=0)

    import json
    payload = json.loads(manager.db.get_state("interactive_refresh_completed_at_v39", "{}"))
    assert float(payload["completed_at"]) > 0


def test_process_rss_helper_reads_ps_kilobytes(monkeypatch) -> None:
    from types import SimpleNamespace

    import pudge.manager as manager_module

    monkeypatch.setattr(
        manager_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="4194304\n"),
    )

    assert manager_module._process_rss_mb(123) == 4096.0


def test_library_scan_fingerprint_cache_is_shared_between_manager_processes(
    tmp_path: Path, monkeypatch
) -> None:
    first = _manager(tmp_path)
    _allow_heavy_scan(first, monkeypatch)
    video = first.config.library.root_dir / "Shared - 01.mkv"
    video.write_bytes(b"video")
    calls: list[str] = []

    def full_scan():
        calls.append("scan")
        row = LibraryEpisode(None, "Shared", 1, video.resolve(), state="local")
        first.db.upsert_episode(row)
        return [first.db.episode_by_path(video.resolve()) or row]

    monkeypatch.setattr(first, "_scan_library_uncached", full_scan)
    assert len(first.scan_library(reuse_unchanged=True)) == 1

    second = AnimeManager(first.config)
    monkeypatch.setattr(
        second,
        "_scan_library_uncached",
        lambda: (_ for _ in ()).throw(AssertionError("cross-process cache missed")),
    )
    assert len(second.scan_library(reuse_unchanged=True)) == 1
    assert calls == ["scan"]


def test_bitmap_ocr_memory_guard_preserves_prepared_bitmap_for_retry(
    tmp_path: Path, monkeypatch
) -> None:
    import itertools

    import pudge.manager as manager_module

    manager = _manager(tmp_path)
    video = manager.config.library.root_dir / "Movie.mkv"
    bitmap = manager.config.paths.cache_dir / "aligned.sup"
    video.write_bytes(b"video")
    bitmap.write_bytes(b"PGS")
    manager.db.upsert_episode(
        LibraryEpisode(
            None,
            "Movie",
            None,
            video.resolve(),
            subtitle_path=bitmap.resolve(),
            state="waiting_text_subtitles",
            subtitle_origin="bitmap",
        )
    )
    manager.db.queue_subtitle_job(video.resolve(), None, None)
    monkeypatch.setattr(manager, "incomplete_download_paths", lambda: ())
    monkeypatch.setattr(manager.work_scheduler, "background_allowed", lambda **_kwargs: True)

    class Lease:
        def release(self) -> None:
            pass

    monkeypatch.setattr(manager.work_scheduler, "acquire_heavy", lambda *_a, **_kw: Lease())
    ticks = itertools.count(0, 10)
    monkeypatch.setattr(manager_module.time, "monotonic", lambda: float(next(ticks)))
    monkeypatch.setattr(manager_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(manager_module, "_process_rss_mb", lambda _pid: 5000.0)

    class FakeProcess:
        def __init__(self) -> None:
            self.pid = 4242
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

        def communicate(self):
            return "", ""

    monkeypatch.setattr(manager_module.subprocess, "Popen", lambda *_a, **_kw: FakeProcess())

    assert manager.process_subtitle_jobs(limit=1) == 0
    episode = manager.db.episode_by_path(video.resolve())
    job = manager.db.subtitle_jobs()[0]
    assert episode is not None
    assert episode.state == "waiting_text_subtitles"
    assert episode.subtitle_path == bitmap.resolve()
    assert "4096 MB" in str(job["last_error"])
    assert str(job["state"]) == "pending"


def test_library_scan_fingerprint_reuses_unchanged_cache_for_one_hour(
    tmp_path: Path, monkeypatch
) -> None:
    """Unchanged libraries must not trigger the old 15-minute full rescan."""
    import json
    import time

    manager = _manager(tmp_path)
    _allow_heavy_scan(manager, monkeypatch)
    video = manager.config.library.root_dir / "Example - 01.mkv"
    video.write_bytes(b"video")
    calls: list[str] = []

    def full_scan():
        calls.append("scan")
        row = LibraryEpisode(None, "Example", 1, video.resolve(), state="local")
        manager.db.upsert_episode(row)
        return [manager.db.episode_by_path(video.resolve()) or row]

    monkeypatch.setattr(manager, "_scan_library_uncached", full_scan)
    manager.scan_library(reuse_unchanged=True)
    cached = json.loads(manager.db.get_state(manager._LIBRARY_SCAN_CACHE_KEY, "{}"))
    cached["completed_at"] = time.time() - 3600
    manager.db.set_state(manager._LIBRARY_SCAN_CACHE_KEY, json.dumps(cached))

    rows = manager.scan_library(reuse_unchanged=True)

    assert len(rows) == 1
    assert calls == ["scan"]


def test_vision_ocr_uses_pyobjc_autorelease_pool_without_manual_drain(
    monkeypatch,
) -> None:
    import sys
    from types import SimpleNamespace

    events: list[str] = []

    class Pool:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, _exc_type, _exc, _tb):
            events.append("exit")
            return False

    class Request:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

        def setRecognitionLevel_(self, _value):
            return None

        def setRecognitionLanguages_(self, _value):
            return None

        def setUsesLanguageCorrection_(self, _value):
            return None

        def results(self):
            return []

    class Handler:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithURL_options_(self, _url, _options):
            return self

        def performRequests_error_(self, _requests, _error):
            return True, None

    class URL:
        @staticmethod
        def fileURLWithPath_(path):
            return path

    fake_vision = SimpleNamespace(
        VNRecognizeTextRequest=Request,
        VNImageRequestHandler=Handler,
        VNRequestTextRecognitionLevelAccurate=1,
    )
    fake_objc = SimpleNamespace(autorelease_pool=lambda: Pool())
    # Deliberately expose no NSAutoreleasePool: importing/using it would make
    # this regression fail instead of silently double-releasing it again.
    fake_foundation = SimpleNamespace(NSURL=URL)

    monkeypatch.setattr(ocr.platform, "system", lambda: "Darwin")
    monkeypatch.setitem(sys.modules, "Vision", fake_vision)
    monkeypatch.setitem(sys.modules, "objc", fake_objc)
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)

    image = Image.new("RGBA", (8, 4), (255, 255, 255, 255))
    try:
        assert ocr._vision_recognize(image) == ""
    finally:
        image.close()

    assert events == ["enter", "exit"]
