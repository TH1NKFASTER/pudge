"""WEBFLOW-7: per-poll download speed/peer stats and progress no longer bump
ui_state_version (each bump rebuilt and re-sent the whole Home snapshot)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pudge.database import Database
from pudge.manager_models import DownloadItem


def _version(db: Database) -> int:
    return int(db.get_state("ui_state_version", "0"))


def _item(**changes) -> DownloadItem:
    base = DownloadItem(
        torrent_hash="a" * 40,
        name="[Grp] Show - 01 (1080p).mkv",
        state="downloading",
        progress=0.1,
        save_path="/library/Show",
        content_path="/library/Show/[Grp] Show - 01 (1080p).mkv",
        media_id=None,
        episode=1,
        raw={"dlspeed": 1000, "num_seeds": 3, "size": 100, "backend": "qbittorrent", "tags": "pudge"},
    )
    return replace(base, **changes)


def test_speed_and_progress_changes_do_not_bump(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_download(_item())
    inserted = _version(db)
    db.upsert_download(
        _item(raw={"dlspeed": 5000, "num_seeds": 9, "size": 100, "backend": "qbittorrent", "tags": "pudge"})
    )
    assert _version(db) == inserted
    db.upsert_download(
        _item(
            progress=0.5,
            raw={"dlspeed": 4000, "num_seeds": 9, "size": 100, "backend": "qbittorrent", "tags": "pudge"},
        )
    )
    assert _version(db) == inserted
    aria2 = {
        "backend": "aria2",
        "total_size": 100,
        "downloaded": 10,
        "download_speed": 1,
        "num_connections": 2,
    }
    db.upsert_download(_item(torrent_hash="b" * 40, raw=aria2))
    second = _version(db)
    db.upsert_download(
        _item(torrent_hash="b" * 40, progress=0.7, raw={**aria2, "downloaded": 70, "download_speed": 9})
    )
    assert _version(db) == second


def test_ui_relevant_changes_still_bump(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    raw = {"dlspeed": 1000, "num_seeds": 3, "size": 100, "backend": "qbittorrent", "tags": "pudge"}
    db.upsert_download(_item(raw=raw))
    before = _version(db)
    db.upsert_download(_item(raw=raw, state="stalledDL"))
    assert _version(db) == before + 1
    db.upsert_download(_item(raw={**raw, "error_message": "tracker down"}, state="stalledDL"))
    assert _version(db) == before + 2
    db.upsert_download(_item(raw={**raw, "error_message": "tracker down"}, state="stalledDL", progress=1.0))
    assert _version(db) == before + 3  # finished
    db.upsert_download(_item(raw={**raw, "error_message": "tracker down"}, state="stalledDL", progress=0.4))
    assert _version(db) == before + 4  # a recheck took it back below 100 %
    db.upsert_download(
        _item(raw={**raw, "error_message": "tracker down"}, state="stalledDL", progress=0.4, name="Renamed")
    )
    assert _version(db) == before + 5


def test_damaged_stored_raw_still_compares(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_download(_item())
    with db.connect() as conn:
        conn.execute("UPDATE downloads SET raw_json='not json'")
    before = _version(db)
    db.upsert_download(_item())  # stored raw unreadable: counts as a change, no error
    assert _version(db) == before + 1
