"""aria2 control files of torrents that are gone are removed, data never is.

Seen in Rakugo's folder: "Shouwa Genroku Rakugo Shinjuu.aria2" (the BD candidate that lost
the race, 0 pieces, no file or folder of its own) stayed next to the batch that won. The
one-time leftover cleanup only looked at control files next to empty videos, so this one
was never visited. A regular pass now removes a control file older than ten minutes whose
torrent no client lists and Pudge does not track, if it has no data beside it.
"""

from __future__ import annotations

import os
import time

from pudge.library import aria2_control_infohash
from pudge.manager_models import DownloadItem
from pudge.providers.qbittorrent import QBittorrentError

from test_fix_planning_downloads_oct15 import _aria2_manager, _peers_without_data

TITLE = "Shouwa Genroku Rakugo Shinjuu"
GONE = "9f5f5cd2051520fa4c62cc214d559969e7fdd93b"
RUNNING = "ce4f4e811c69558d74f8d2ecbc81f820667f43e1"
TRACKED = "a" * 40


def _control(path, info_hash: str, *, version: int = 1, age: float = 3600.0) -> None:
    order = "big" if version == 1 else "little"
    body = (
        version.to_bytes(2, "big")
        + (1).to_bytes(4, order)
        + (20).to_bytes(4, order)
        + bytes.fromhex(info_hash)
    )
    path.write_bytes(body + b"\0" * 32)
    old = time.time() - age
    os.utime(path, (old, old))


def test_control_file_hash_is_read_in_both_byte_orders(tmp_path) -> None:
    for version in (0, 1):
        _control(tmp_path / "x.aria2", GONE, version=version)
        assert aria2_control_infohash(tmp_path / "x.aria2") == GONE
    (tmp_path / "bad.aria2").write_bytes(b"control")
    assert aria2_control_infohash(tmp_path / "bad.aria2") is None


def test_orphan_controls_go_and_everything_with_data_or_a_torrent_stays(tmp_path, monkeypatch) -> None:
    manager, backend, _clock = _aria2_manager(tmp_path, monkeypatch, {})
    series = manager.config.library.root_dir / TITLE
    batch = series / f"[HorribleSubs] {TITLE} (01-13) [1080p] (Batch)"
    batch.mkdir(parents=True)
    (batch / f"[HorribleSubs] {TITLE} - 01 [1080p].mkv").write_bytes(b"x" * 1024)
    backend.scripts[RUNNING] = _peers_without_data
    backend.added_at[RUNNING] = 0.0
    backend.items[RUNNING] = DownloadItem(
        RUNNING, batch.name, "active", 0.83, str(series), str(batch), raw={"backend": "aria2"}
    )
    _control(series / f"{TITLE}.aria2", GONE)  # the user's case: no file or folder beside it
    _control(series / f"{batch.name}.aria2", RUNNING)  # the download that runs
    empty = series / "[Other] candidate"
    (empty / "sub").mkdir(parents=True)
    _control(series / f"{empty.name}.aria2", "b" * 40)  # gone, only empty folders
    data = series / "[Other] with data.mkv"
    data.write_bytes(b"x" * 10)
    _control(series / f"{data.name}.aria2", "c" * 40)  # gone, but its file holds data
    _control(series / "fresh.aria2", "d" * 40, age=60)  # rewritten a minute ago
    _control(series / "tracked.aria2", TRACKED)  # aria2 lost it, Pudge still tracks it
    (series / "unreadable.aria2").write_bytes(b"control")
    old = time.time() - 3600
    os.utime(series / "unreadable.aria2", (old, old))
    manager.db.upsert_download(
        DownloadItem(TRACKED, "tracked", "downloading", 0.1, str(series), str(series / "tracked"))
    )

    listing = backend.torrents

    def unavailable(**_kwargs):
        raise QBittorrentError("aria2 RPC is not running")

    monkeypatch.setattr(backend, "torrents", unavailable)
    assert manager.cleanup_orphan_aria2_controls() == 0  # aria2 cannot be asked: nothing goes
    assert (series / f"{TITLE}.aria2").exists()
    monkeypatch.setattr(backend, "torrents", listing)

    assert manager.cleanup_orphan_aria2_controls() == 2
    assert not (series / f"{TITLE}.aria2").exists()
    assert not (series / f"{empty.name}.aria2").exists() and not empty.exists()
    for kept in (
        f"{batch.name}.aria2",
        f"{data.name}.aria2",
        "fresh.aria2",
        "tracked.aria2",
        "unreadable.aria2",
    ):
        assert (series / kept).exists(), kept
    assert (batch / f"[HorribleSubs] {TITLE} - 01 [1080p].mkv").stat().st_size == 1024 and data.exists()

    # A few times a day, not on every periodic pass.
    _control(series / "later.aria2", "e" * 40)
    assert manager.cleanup_orphan_aria2_controls() == 0 and (series / "later.aria2").exists()
    later = time.time() + manager._ARIA2_ORPHAN_CONTROL_INTERVAL_SECONDS + 60
    # Hours later the control rewritten a minute before the first pass is old as well.
    assert manager.cleanup_orphan_aria2_controls(now=later) == 2
    assert not (series / "later.aria2").exists() and not (series / "fresh.aria2").exists()
    assert (series / "tracked.aria2").exists() and (series / f"{batch.name}.aria2").exists()
