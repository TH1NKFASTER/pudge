"""An unfinished download's bytes count once against the library disk limit.

A sparse 10 GB batch file at 10 % (qBittorrent sparse files, aria2 after the tail
piece) counted its full st_size as "used" plus its 9 GB still to download as
"pending": 19 GB committed, so with an 18 GB limit automatic downloads paused early.
"""

from __future__ import annotations

from pathlib import Path

from test_web_app import make_api

from pudge.manager_models import DownloadItem, LibraryAnime

GB = 1024**3


def _sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.truncate(size)


def test_in_progress_bytes_are_not_counted_twice(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    manager, db = api.manager, api.manager.db
    monkeypatch.setattr("pudge.manager.disk_space.volume_usage", lambda _path: (2000 * GB, 1000 * GB))
    db.upsert_anime(LibraryAnime(media_id=1, title="Show", status="CURRENT", episodes=12, format="TV"))
    root = manager.config.library.root_dir.expanduser()
    batch = root / "Show" / "[Grp] Show (01-12) [Batch]"
    _sparse(batch / "Show - 01.mkv", 10 * GB)
    _sparse(root / "Other" / "Other - 01.mkv", 2 * GB)  # a finished file counts in full
    db.upsert_download(
        DownloadItem(
            torrent_hash="aa" * 20,
            name=batch.name,
            state="downloading",
            progress=0.1,
            save_path=str(batch.parent),
            content_path=str(batch),
            media_id=1,
            is_batch=True,
            raw={"backend": "qbittorrent", "total_size": 10 * GB, "downloaded": GB},
        )
    )
    manager.config.library.disk_limit_enabled = True
    manager.config.library.disk_limit_gb = 18.0
    manager.invalidate_storage_snapshot()
    snapshot = manager._storage_snapshot()
    assert snapshot["used_bytes"] == 3 * GB
    assert snapshot["pending_bytes"] == 9 * GB
    assert manager._storage_can_accept(int(1.4 * GB))

    # Finished: the whole file counts as used and nothing is pending.
    db.upsert_download(
        DownloadItem(
            torrent_hash="aa" * 20,
            name=batch.name,
            state="uploading",
            progress=1.0,
            save_path=str(batch.parent),
            content_path=str(batch),
            media_id=1,
            is_batch=True,
            raw={"backend": "qbittorrent", "total_size": 10 * GB, "downloaded": 10 * GB, "amount_left": 0},
        )
    )
    manager.invalidate_storage_snapshot()
    snapshot = manager._storage_snapshot()
    assert snapshot["used_bytes"] == 12 * GB and snapshot["pending_bytes"] == 0
