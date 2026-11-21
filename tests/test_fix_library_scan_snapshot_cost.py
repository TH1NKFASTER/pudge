"""MGR-9: in-progress downloads do not change the scan fingerprint, and a scan lists
each folder once for sidecar subtitles."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pudge import library
from pudge.config import AppConfig
from pudge.library import sidecar_subtitle
from pudge.manager import AnimeManager

JAPANESE = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは、元気ですか。今日はいい天気ですね。\n"


def _manager(tmp_path: Path) -> AnimeManager:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.anilist.enabled = False
    return AnimeManager(cfg)


def _grow(path: Path) -> None:
    with path.open("ab") as handle:
        handle.write(b"y" * 1024)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))


def test_a_download_being_written_keeps_the_fingerprint(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / "Show"
    folder.mkdir()
    (folder / "Show - 01.mkv").write_bytes(b"x" * 100)
    downloading = folder / "Show - 02.mkv"
    downloading.write_bytes(b"x" * 10)
    monkeypatch.setattr(manager, "incomplete_download_paths", lambda: (downloading,))
    before = manager._library_scan_snapshot()
    _grow(downloading)
    assert manager._library_scan_snapshot() == before
    monkeypatch.setattr(manager, "incomplete_download_paths", lambda: ())  # finished
    assert manager._library_scan_snapshot()["fingerprint"] != before["fingerprint"]


def test_aria2_control_files_and_their_videos_count_by_path(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / "Show"
    batch = folder / "[Grp] Show (Batch)"
    batch.mkdir(parents=True)
    single = folder / "Show - 03.mkv"
    single.write_bytes(b"x")
    control = folder / "Show - 03.mkv.aria2"
    control.write_bytes(b"c")
    in_batch = batch / "Show - 04.mkv"
    in_batch.write_bytes(b"x")
    folder_control = folder / "[Grp] Show (Batch).aria2"
    folder_control.write_bytes(b"c")
    before = manager._library_scan_snapshot()
    for path in (single, control, in_batch, folder_control):
        _grow(path)
    assert manager._library_scan_snapshot() == before
    control.unlink()  # aria2 finished this file
    assert manager._library_scan_snapshot()["fingerprint"] != before["fingerprint"]


def test_a_finished_video_still_changes_the_fingerprint(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    folder = manager.config.library.root_dir / "Show"
    folder.mkdir()
    video = folder / "Show - 01.mkv"
    video.write_bytes(b"x")
    before = manager._library_scan_snapshot()
    _grow(video)
    assert manager._library_scan_snapshot()["fingerprint"] != before["fingerprint"]


@pytest.fixture
def scandirs(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = os.scandir

    def counting(path=".", *args, **kwargs):
        seen.append(str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(library.os, "scandir", counting)
    return seen


def test_one_scan_lists_each_folder_once(tmp_path: Path, monkeypatch, scandirs) -> None:
    folder = tmp_path / "Show"
    folder.mkdir()
    videos = []
    for number in range(1, 41):
        video = folder / f"[Grp] Show - {number:02d} (1080p).mkv"
        video.write_bytes(b"x")
        videos.append(video)
    with library.directory_listing_cache():
        for video in videos:
            sidecar_subtitle(video)
    assert sorted(scandirs) == sorted({str(folder), str(folder / "subs")})


def test_sidecar_choice_is_unchanged(tmp_path: Path) -> None:
    folder = tmp_path / "Show"
    (folder / "subs").mkdir(parents=True)
    video = folder / "[SubsPlease] Show - 05 (1080p).mkv"
    video.write_bytes(b"x")
    stem = video.stem
    (folder / f"{stem}.en.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello there.\n", encoding="utf-8"
    )
    assert sidecar_subtitle(video) is None
    point_five = folder / "[SubsPlease] Show - 05.5 (1080p).ja.srt"
    point_five.write_text(JAPANESE, encoding="utf-8")  # another episode's file
    assert sidecar_subtitle(video) is None
    in_subs = folder / "subs" / f"{stem}.ja.ass"
    in_subs.write_text(
        "[Script Info]\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,こんにちは、元気ですか。\n",
        encoding="utf-8",
    )
    assert sidecar_subtitle(video) == in_subs.resolve()
    beside = folder / f"{stem}.ja.srt"
    beside.write_text(JAPANESE, encoding="utf-8")
    assert sidecar_subtitle(video) == beside.resolve()
    exact = folder / f"{stem}.srt"
    exact.write_text(JAPANESE, encoding="utf-8")
    assert sidecar_subtitle(video) == exact.resolve()
    with library.directory_listing_cache():
        assert sidecar_subtitle(video) == exact.resolve()
