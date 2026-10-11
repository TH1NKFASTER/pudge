"""Disk can fill silently; derived caches grow without limit (audit fix).

Automatic downloads keep a free-space reserve on the download volume, count
in-flight downloads, walk the library once per pass and show a distinct pause;
derived subtitle caches and iPhone copies are bounded and registered; OCR's
extracted bitmap track is deleted and its extraction cannot hang forever.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

from pudge import disk_space, ocr
from pudge.config import AppConfig, load_config, write_config
from pudge.database import Database
from pudge.manager import AnimeManager, ManagerError
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease
from tests.i18n_support import en, ru

GB = 1024**3
DAY = 24 * 3600.0


def _manager(tmp_path: Path, monkeypatch, *, total: int = 256 * GB, free: int = 21 * GB, **library):
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir()
    cfg.library.database_path = tmp_path / "data" / "library.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    for key, value in library.items():
        setattr(cfg.library, key, value)
    volume = {"usage": (total, free)}
    monkeypatch.setattr(disk_space, "volume_usage", lambda _path: volume["usage"])
    messages: list[str] = []
    manager = AnimeManager(cfg, log=messages.append)
    return manager, volume, messages


def _release(size_bytes: int, info_hash: str = "a" * 40) -> NyaaRelease:
    return NyaaRelease(
        title="[Group] Show - 01 (1080p).mkv",
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash=info_hash,
        size_text="",
        size_bytes=size_bytes,
        seeders=50,
        leechers=0,
        downloads=0,
        trusted=True,
        remake=False,
        category_id="1_2",
        score=200.0,
        reasons=["ep=1"],
        group="Group",
    )


# --- free-space reserve --------------------------------------------------------


def test_unknown_size_release_needs_reserve_plus_a_typical_episode(tmp_path, monkeypatch) -> None:
    # 256 GB SSD with 21 GB free: the 20 GB reserve leaves 1 GB for downloads.
    manager, volume, _ = _manager(tmp_path, monkeypatch)

    assert manager._storage_can_accept(0) is False
    assert manager._storage_can_accept(500 * 1024**2) is True
    assert manager._storage_can_accept(2 * GB) is False
    status = manager.storage_status()
    assert status["reserve_bytes"] == 20 * GB
    assert status["free_bytes"] == 21 * GB
    assert status["effective_limit_bytes"] == status["used_bytes"] + GB
    assert status["downloads_paused"] is True
    assert status["paused_reason"] == "free_space"

    # 512 GB SSD: the reserve is 5 % (25.6 GB); with 40 GB free an episode fits again.
    volume["usage"] = (512 * GB, 40 * GB)
    manager.invalidate_storage_snapshot()
    assert manager._storage_can_accept(0) is True
    status = manager.storage_status()
    assert status["reserve_bytes"] == int(512 * GB * 0.05)
    assert status["downloads_paused"] is False and status["paused_reason"] == ""


def test_free_space_reserve_is_configurable_and_round_trips(tmp_path, monkeypatch) -> None:
    manager, _, _ = _manager(tmp_path, monkeypatch, free=60 * GB, disk_free_reserve_gb=50.0)
    assert manager.storage_status()["reserve_bytes"] == 50 * GB
    assert manager._storage_can_accept(9 * GB) is True
    assert manager._storage_can_accept(11 * GB) is False

    path = tmp_path / "config.toml"
    config = AppConfig(config_path=path)
    config.library.disk_free_reserve_gb = 35.0
    write_config(config, path)
    assert load_config(path).library.disk_free_reserve_gb == 35.0
    assert AppConfig().library.disk_free_reserve_gb == 0.0  # automatic


def test_configured_limit_still_applies_and_unknown_sizes_respect_it(tmp_path, monkeypatch) -> None:
    manager, _, _ = _manager(
        tmp_path, monkeypatch, free=1000 * GB, total=2000 * GB, disk_limit_gb=3 * 1024**2 / GB
    )
    (manager.config.library.root_dir / "ep1.mkv").write_bytes(b"x" * (2 * 1024**2))

    status = manager.storage_status()
    assert status["limit_bytes"] == 3 * 1024**2
    assert status["effective_limit_bytes"] == 3 * 1024**2
    assert status["space_limited"] is False
    assert status["downloads_paused"] is True and status["paused_reason"] == "limit"
    assert manager._storage_can_accept(0) is False  # an unknown size was accepted before
    assert manager._storage_can_accept(512 * 1024) is True


def test_free_space_guard_applies_without_a_configured_limit(tmp_path, monkeypatch) -> None:
    manager, _, _ = _manager(tmp_path, monkeypatch, free=21 * GB, disk_limit_enabled=False)
    assert manager._storage_can_accept(3 * GB) is False
    assert manager.storage_status()["over_limit"] is False  # no deletions without the limit


def test_in_flight_downloads_count_against_the_free_space(tmp_path, monkeypatch) -> None:
    manager, _, _ = _manager(tmp_path, monkeypatch, free=30 * GB)
    assert manager._storage_can_accept(5 * GB) is True
    manager.db.upsert_download(
        DownloadItem(
            torrent_hash="b" * 40,
            name="Show - 02",
            state="downloading",
            progress=0.1,
            save_path=str(manager.config.library.root_dir),
            content_path="",
            raw={"backend": "qbittorrent", "amount_left": 7 * GB, "size": 8 * GB},
        )
    )
    manager.invalidate_storage_snapshot()
    status = manager.storage_status()
    assert status["pending_bytes"] == 7 * GB
    assert status["available_gb"] == 23.0
    assert manager._storage_can_accept(5 * GB) is False
    assert manager._storage_can_accept(2 * GB) is True


def test_library_is_walked_once_per_pass_not_per_candidate(tmp_path, monkeypatch) -> None:
    manager, _, _ = _manager(tmp_path, monkeypatch, free=500 * GB, total=1000 * GB)
    manager.config.nyaa.auto_require_trusted = False
    walks: list[int] = []
    walk = manager._library_usage_bytes

    def counted() -> int:
        walks.append(1)
        return walk()

    monkeypatch.setattr(manager, "_library_usage_bytes", counted)
    releases = [_release(GB, info_hash=f"{index:040x}") for index in range(40)]

    assert len(manager.automatic_release_candidates(releases)) == 40
    assert len(manager.automatic_release_candidates(releases)) == 40
    manager.storage_status()
    assert len(walks) == 1


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_full_disk_pauses_automatic_search_with_a_distinct_message(tmp_path, monkeypatch) -> None:
    manager, _, messages = _manager(tmp_path, monkeypatch, free=21 * GB)
    manager.config.nyaa.auto_download_current = True
    monkeypatch.setattr(manager, "downloads_configured", lambda: True)
    monkeypatch.setattr(
        manager, "search_releases", lambda *a, **k: pytest.fail("no search while downloads are paused")
    )
    manager.db.upsert_anime(LibraryAnime(media_id=1, title="Show", status="CURRENT", episodes=12))
    records = _Records()
    manager.logger.addHandler(records)
    try:
        assert manager.auto_search_current() == 0
    finally:
        manager.logger.removeHandler(records)

    assert any(message.startswith("Automatic downloads paused") for message in messages)
    assert not any("no suitable release" in message for message in messages)
    assert any("WAIT step=nyaa.auto reason=disk_space" in message for message in records.messages)


def test_release_too_big_for_the_free_space_is_reported_as_paused(tmp_path, monkeypatch) -> None:
    manager, _, messages = _manager(tmp_path, monkeypatch, free=24 * GB)  # 4 GB room
    manager.config.nyaa.auto_download_current = True
    manager.config.nyaa.auto_require_trusted = False
    manager.config.nyaa.max_auto_download_per_anime = 1
    monkeypatch.setattr(manager, "downloads_configured", lambda: True)
    monkeypatch.setattr(manager, "search_releases", lambda *a, **k: [_release(6 * GB)])
    monkeypatch.setattr(manager, "add_release", lambda *a, **k: pytest.fail("must not download"))
    manager.db.upsert_anime(LibraryAnime(media_id=1, title="Show", status="CURRENT", episodes=12))

    assert manager.auto_search_current() == 0

    assert "Show #1: automatic download paused — not enough disk space" in messages
    assert not any("no suitable release" in message for message in messages)


def test_manual_download_refuses_to_eat_into_the_reserve(tmp_path, monkeypatch) -> None:
    manager, _, _ = _manager(tmp_path, monkeypatch, free=22 * GB)
    monkeypatch.setattr(manager, "downloads_enabled", lambda: True)
    monkeypatch.setattr(manager, "qbt_client", lambda: pytest.fail("no torrent client call"))
    monkeypatch.setattr(manager, "ensure_torrent_backend_ready", lambda: pytest.fail("no backend start"))
    manager.db.upsert_anime(LibraryAnime(media_id=1, title="Show", status="CURRENT", episodes=12))

    with pytest.raises(ManagerError) as error:
        manager.add_release(1, _release(5 * GB), episode=1, batch=False, manual=True)

    assert str(error.value) == en("error.manager.notEnoughDiskSpace", free="22.0", reserve="20.0")
    assert ru("error.manager.notEnoughDiskSpace", free="22.0", reserve="20.0") != str(error.value)


def test_low_free_space_deletes_the_oldest_watched_episodes_under_the_limit(tmp_path, monkeypatch) -> None:
    manager, _, _ = _manager(tmp_path, monkeypatch, free=15 * GB)  # below the 20 GB reserve
    manager.config.agent.delete_only_managed_files = False
    root = manager.config.library.root_dir
    watched = root / "ep1.mkv"
    unwatched = root / "ep2.mkv"
    for episode, video in ((1, watched), (2, unwatched)):
        video.write_bytes(b"x" * 4096)
        manager.db.upsert_episode(LibraryEpisode(media_id=1, title="Show", episode=episode, video_path=video))
    with manager.db.connect() as conn:
        conn.execute(
            "UPDATE episodes SET state='watched', watched_at=? WHERE video_path=?",
            (time.time() - DAY, str(watched)),
        )

    status = manager.storage_status()
    assert status["over_limit"] is True and status["space_limited"] is True
    assert manager.enforce_disk_limit() == 1
    assert not watched.exists() and unwatched.exists()


# --- derived caches ------------------------------------------------------------


def _write(path: Path, data: bytes = b"x" * 100, *, age: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


def test_folder_policy_is_lru_keeps_sidecars_together_and_registers_size(tmp_path: Path) -> None:
    from pudge.cache_registry import CacheRegistry, FolderPolicy

    db = Database(tmp_path / "db.sqlite3")
    root = tmp_path / "cache"
    oldest = _write(root / "synced" / "a.srt", age=50)
    _write(root / "synced" / "b.srt", age=40)
    sidecar = _write(root / "synced" / "b.srt.json", age=40)
    newest = _write(root / "synced" / "c.srt", age=30)
    registry = CacheRegistry(db, root)

    report = registry.enforce_folders(
        {"synced": FolderPolicy("synced", 150, min_age_seconds=0)}, protected_paths={oldest}
    )

    assert oldest.exists() and not sidecar.exists() and not (root / "synced" / "b.srt").exists()
    assert not newest.exists()  # LRU: the protected entry stays, the others go until under the cap
    assert report["synced"]["remaining_bytes"] == 100
    assert registry.usage_bytes() == 100


def test_derived_caches_are_bounded_and_deleted_episodes_purged(tmp_path, monkeypatch) -> None:
    manager, _, _ = _manager(tmp_path, monkeypatch, free=500 * GB, total=1000 * GB)
    cache = manager.config.paths.cache_dir
    video = manager.config.library.root_dir / "ep1.mkv"
    video.write_bytes(b"video")
    live_subtitle = _write(cache / "synced" / "live.srt", age=400 * DAY)
    live_sidecar = _write(cache / "synced" / "live.srt.json", age=400 * DAY)
    manager.db.upsert_episode(
        LibraryEpisode(media_id=1, title="Show", episode=1, video_path=video, subtitle_path=live_subtitle)
    )
    gone = tmp_path / "gone" / "ep2.mkv"
    orphan_subtitle = _write(cache / "playback-srt" / "v16-orphan.srt")  # fresh: purged anyway
    orphan_manifest = _write(
        cache / "final-pipeline" / "orphan.json",
        json.dumps({"video": str(gone), "subtitle": str(orphan_subtitle)}).encode(),
    )
    live_manifest = _write(
        cache / "final-pipeline" / "live.json",
        json.dumps({"video": str(video.resolve()), "subtitle": str(live_subtitle)}).encode(),
        age=400 * DAY,
    )
    old_speech = _write(cache / "speech" / "old.npz", age=200 * DAY)
    new_speech = _write(cache / "speech" / "new.npz")
    leftover_sup = _write(cache / "ocr" / "x.sup", b"x" * 5000, age=2 * DAY)
    ocr_text = _write(cache / "ocr" / "x.srt", age=2 * DAY)

    assert manager.enforce_derived_caches(force=True) > 0

    assert live_subtitle.exists() and live_sidecar.exists() and live_manifest.exists()
    assert not orphan_manifest.exists() and not orphan_subtitle.exists()
    assert not old_speech.exists() and new_speech.exists()
    assert not leftover_sup.exists() and ocr_text.exists()
    status = manager.storage_status()
    remaining = sum(path.stat().st_size for path in cache.rglob("*") if path.is_file())
    assert status["cache_bytes"] == remaining > 0
    assert manager.enforce_derived_caches() == 0  # at most hourly


def test_purge_helper_deletes_a_deleted_episodes_derived_files(tmp_path, monkeypatch) -> None:
    manager, _, _ = _manager(tmp_path, monkeypatch, free=500 * GB, total=1000 * GB)
    cache = manager.config.paths.cache_dir
    video = manager.config.library.root_dir / "ep1.mkv"
    video.write_bytes(b"video")
    subtitle = _write(cache / "synced" / "ep1.srt")
    sidecar = _write(cache / "synced" / "ep1.srt.json")
    manifest = _write(
        cache / "final-pipeline" / "ep1.json",
        json.dumps({"video": str(video.resolve()), "subtitle": str(subtitle)}).encode(),
    )
    manager.db.upsert_episode(
        LibraryEpisode(media_id=1, title="Show", episode=1, video_path=video, subtitle_path=subtitle)
    )
    assert manager.purge_episode_derived_files(video) == 0  # still in the library

    video.unlink()
    manager.db.delete_episode_record(video)
    assert manager.purge_episode_derived_files(video) == 3
    assert not subtitle.exists() and not sidecar.exists() and not manifest.exists()


def test_iphone_staging_and_orphaned_copies_are_cleaned(tmp_path, monkeypatch) -> None:
    from pudge.iphone_service import IPhoneService

    manager, _, _ = _manager(tmp_path, monkeypatch, free=500 * GB, total=1000 * GB)
    root = manager.config.library.database_path.parent / "iphone"
    IPhoneService(manager.db, root)
    stale_transfer = _write(root / "staging" / "old-transfer" / "part", age=4 * DAY).parent
    os.utime(stale_transfer, (time.time() - 4 * DAY,) * 2)
    active_transfer = _write(root / "staging" / "new-transfer" / "part", age=3600).parent
    source = _write(tmp_path / "books" / "book.opus", b"audio")
    live_copy = _write(root / "prepared" / "live-aac-v1.m4a", age=40 * DAY)
    orphan_copy = _write(root / "prepared" / "orphan-aac-v1.m4a", age=40 * DAY)
    old_cover = _write(root / "cover-sources" / "cover", age=100 * DAY)
    stat = source.stat()
    with manager.db.connect() as conn:
        conn.execute(
            "INSERT INTO iphone_prepared(source,size,mtime_ns,result) VALUES(?,?,?,?)",
            (str(source), stat.st_size, stat.st_mtime_ns, json.dumps({"path": str(live_copy)})),
        )

    manager.enforce_derived_caches(force=True)

    assert not stale_transfer.exists() and active_transfer.exists()
    assert live_copy.exists() and not orphan_copy.exists()
    assert not old_cover.exists()


# --- OCR -----------------------------------------------------------------------


def _script(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _ocr_tools(tmp_path: Path, ffmpeg_body: str) -> tuple[str, str]:
    ffmpeg = _script(tmp_path / "ffmpeg", ffmpeg_body)
    ffprobe = _script(tmp_path / "ffprobe", "print('1440.0')")
    return ffmpeg, ffprobe


@pytest.mark.parametrize("text", ["こんにちは", ""])
def test_extracted_bitmap_track_is_deleted_after_ocr(tmp_path, monkeypatch, text) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    ffmpeg, ffprobe = _ocr_tools(tmp_path, "open(sys.argv[-1], 'wb').write(b'PGS' * 1000)")
    images = [Image.new("RGBA", (8, 4), (255, 255, 255, 255)) for _ in range(2)]

    def displays(path: Path):
        assert path.is_file()
        yield 1.0, images[0]
        yield 3.0, images[1]

    monkeypatch.setattr(ocr, "iter_pgs_compositions", displays)
    monkeypatch.setattr(ocr, "_vision_recognize", lambda _image: text)

    output, result = ocr.image_subtitle_to_srt(
        video,
        tmp_path / "cache",
        embedded_stream_index=3,
        embedded_codec="hdmv_pgs_subtitle",
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
    )

    assert result["reason"] == ("ocr_ready" if text else "ocr_no_text")
    assert (output is not None) == bool(text)
    assert list((tmp_path / "cache" / "ocr").glob("*.sup")) == []


def test_bitmap_extraction_times_out_proportionally_to_the_running_time(tmp_path, monkeypatch) -> None:
    assert ocr.pgs_extract_timeout_seconds(1440.0) == 2880.0
    assert ocr.pgs_extract_timeout_seconds(60.0) == 15 * 60.0
    assert ocr.pgs_extract_timeout_seconds(None) == 4 * 3600.0

    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    ffmpeg, ffprobe = _ocr_tools(
        tmp_path, "open(sys.argv[-1], 'wb').write(b'partial')\nsys.stdout.flush()\ntime.sleep(60)"
    )
    monkeypatch.setattr(ocr, "PGS_EXTRACT_MIN_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(ocr, "PGS_EXTRACT_TIMEOUT_PER_MEDIA_SECOND", 0.0001)
    started = time.monotonic()

    with pytest.raises(ocr.OCRConversionError, match="timed out"):
        ocr.image_subtitle_to_srt(
            video,
            tmp_path / "cache",
            embedded_stream_index=3,
            embedded_codec="hdmv_pgs_subtitle",
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
        )

    assert time.monotonic() - started < 30
    assert list((tmp_path / "cache" / "ocr").glob("*.sup")) == []


# --- window --------------------------------------------------------------------

PAUSED_FREE = {
    "downloads_paused": True,
    "paused_reason": "free_space",
    "free_gb": 12.3,
    "available_gb": 9.5,  # free once unfinished downloads complete
    "reserve_gb": 20.0,
    "total_bytes": 256 * GB,
    "used_gb": 80.0,
    "limit_gb": 500.0,
    "cache_bytes": 3 * GB,
    "cache_gb": 3.0,
}


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_window_shows_a_storage_pause_banner(page, lang) -> None:
    code = r"""
    renderDataPages(true);
    return {visible: __visible('#storageBanner'), text: __text('#storageBanner')};
    """
    tr = en if lang == "en" else ru
    paused = page.run(code, lang=lang, state={"storage": PAUSED_FREE}).value
    assert paused == {"visible": True, "text": tr("storage.pausedFreeSpace", free="9.5", reserve="20.0")}

    limit = {**PAUSED_FREE, "paused_reason": "limit", "committed_gb": 499.5}
    shown = page.run(code, lang=lang, state={"storage": limit}).value
    assert shown["text"] == tr("storage.pausedLimit", used="499.5", limit="500.0")

    calm = page.run(code, lang=lang, state={"storage": {**PAUSED_FREE, "downloads_paused": False}}).value
    assert calm["visible"] is False


def test_settings_show_free_space_and_cache_size(page) -> None:
    for lang, tr in (("en", en), ("ru", ru)):
        text = page.run("return __text('.storage-details')", lang=lang, state={"storage": PAUSED_FREE}).value
        assert tr("label.storageFree", free="12.3", reserve="20.0") in text
        assert tr("label.storageCache", cache="3.0") in text
