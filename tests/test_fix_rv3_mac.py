"""Review round 3 (Mac core): fewer repeated reads, no stray temporaries, bounded walks."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from pudge import intro_skipper
from pudge.intro_skipper import IntroSkipperCache
from pudge.metadata_cache import MetadataCache
from pudge.providers import aria2 as aria2_module
from pudge.providers.aria2 import Aria2Client
from pudge.web_app import WebAppApi


def _bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, str):
        value = value.encode()
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    return b"d" + b"".join(_bencode(key) + _bencode(value[key]) for key in sorted(value)) + b"e"


def _saved_torrent(folder: Path, name: str, files: list[tuple[str, int]]) -> str:
    info = {
        "name": name,
        "piece length": 16384,
        "pieces": b"\0" * 20,
        "files": [{"length": length, "path": [relative]} for relative, length in files],
    }
    info_hash = hashlib.sha1(_bencode(info)).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{info_hash}.torrent").write_bytes(_bencode({"info": info}))
    for relative, length in files:
        path = folder / name / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"v" * length)
    return info_hash


def test_a_forgotten_tasks_saved_torrent_is_parsed_once_per_version(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "Invented Show"
    name = "[Group] Invented Show [1080p]"
    files = [("Invented Show - 01.mkv", 11), ("Invented Show - 02.mkv", 12)]
    info_hash = _saved_torrent(folder, name, files)
    parsed: list[int] = []
    real_layout = aria2_module.torrent_layout

    def counting(payload: bytes):
        parsed.append(len(payload))
        return real_layout(payload)

    monkeypatch.setattr(aria2_module, "torrent_layout", counting)
    monkeypatch.setattr(aria2_module, "_TORRENT_LAYOUT_CACHE", {})
    for _ in range(5):  # every listing checks every forgotten task
        assert Aria2Client._completed_payload_on_disk(folder, info_hash) == (folder / name, 23)
    assert len(parsed) == 1

    # The files themselves are still checked on every call.
    (folder / name / files[1][0]).write_bytes(b"short")
    assert Aria2Client._completed_payload_on_disk(folder, info_hash) is None
    (folder / name / files[1][0]).write_bytes(b"v" * 12)
    (folder / f"{name}.aria2").write_bytes(b"control")
    assert Aria2Client._completed_payload_on_disk(folder, info_hash) is None
    (folder / f"{name}.aria2").unlink()
    assert len(parsed) == 1

    # A rewritten .torrent (another size) is read again.
    torrent = folder / f"{info_hash}.torrent"
    torrent.write_bytes(torrent.read_bytes() + b"")
    os.utime(torrent, ns=(1_000_000_000, 1_000_000_000))
    assert Aria2Client._completed_payload_on_disk(folder, info_hash) is not None
    assert len(parsed) == 2

    torrent.unlink()
    assert Aria2Client._completed_payload_on_disk(folder, info_hash) is None


def test_hollow_uses_the_stat_it_is_given(tmp_path: Path) -> None:
    video = tmp_path / "e.mkv"
    video.write_bytes(b"v" * 10)
    info = video.stat()
    video.unlink()  # a second stat would fail: the given one is used
    assert aria2_module._hollow(video, 10, info) is False


def test_intro_result_write_leaves_no_temporary_when_the_dump_fails(tmp_path: Path) -> None:
    cache = IntroSkipperCache(tmp_path)
    with pytest.raises(TypeError):
        cache.write_result("abc123", {"status": "found", "bad": object()})
    results = cache.root / "results"
    assert list(results.iterdir()) == []
    cache.write_result("abc123", {"status": "found", "segments": []})
    assert [path.name for path in results.iterdir()] == ["abc123.json"]
    assert cache.read_result("abc123")["status"] == "found"


def test_intro_wanted_and_no_progress_files_are_written_whole(tmp_path: Path) -> None:
    video = tmp_path / "Invented - 01.mkv"
    video.write_bytes(b"v")
    assert intro_skipper.request_analysis(tmp_path, [video], now=100.0) == 1
    root = IntroSkipperCache(tmp_path).root
    leftovers = [path.name for path in root.iterdir() if path.name.startswith(".") and path.suffix != ".lock"]
    assert leftovers == []
    assert intro_skipper._wanted_paths(IntroSkipperCache(tmp_path), 100.0) == [video]


def test_metadata_cache_prune_can_be_rate_limited(tmp_path: Path) -> None:
    cache = MetadataCache(tmp_path, "rv3-prune", schema="v1")
    for index in range(5):
        cache.put({"k": index}, index)
    assert cache.prune(older_than_seconds=3600, max_entries=3, min_interval_seconds=600) == 2
    for index in range(5, 8):
        cache.put({"k": index}, index)
    # Pruned a moment ago: skipped (it ran after every put).
    assert cache.prune(older_than_seconds=3600, max_entries=3, min_interval_seconds=600) == 0
    assert len(list(cache.root.glob("*.json"))) == 6
    # Without an interval it prunes as before.
    assert cache.prune(older_than_seconds=3600, max_entries=3) == 3
    assert len(list(cache.root.glob("*.json"))) == 3


def test_dropped_folder_videos_skip_hidden_folders_and_stay_sorted(tmp_path: Path) -> None:
    folder = tmp_path / "Drop"
    for relative in (
        "b/Invented - 02.mkv",
        "a/Invented - 01.mkv",
        "Invented - 03.mp4",
        ".git/objects/x.mkv",
        "a/.hidden/Invented - 04.mkv",
        "._Invented - 05.mkv",
        "notes.txt",
    ):
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"v")
    found = WebAppApi._dropped_folder_videos(folder)
    assert [path.relative_to(folder).as_posix() for path in found] == [
        "Invented - 03.mp4",
        "a/Invented - 01.mkv",
        "b/Invented - 02.mkv",
    ]
    assert [p.name for p in WebAppApi._dropped_folder_videos(folder, limit=1)] == ["Invented - 03.mp4"]
    assert json.dumps([str(p) for p in found])  # plain paths
