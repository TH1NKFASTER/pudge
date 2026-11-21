"""A download sync does not delete the row of a release added while it listed the client.

sync_downloads lists the torrent client, then prunes every stored row the listing does
not have. A release picked meanwhile (by the window or by the agent process) had its
row, with _intent_revision and the acquisition metadata, deleted: the new download
vanished from Home and the Download Center until the next sync. Syncs in one process
also ran concurrently (Download Center refresh, foreground worker, actions).
"""

from __future__ import annotations

import dataclasses
import threading
import time

from test_web_app import make_api

from pudge.manager_models import DownloadItem, LibraryAnime


def _item(tmp_path, torrent_hash: str, episode: int, category: str, **raw) -> DownloadItem:
    return DownloadItem(
        torrent_hash=torrent_hash,
        name=f"Show - {episode:02d}",
        state="downloading",
        progress=0.0,
        save_path=str(tmp_path),
        content_path=str(tmp_path / f"x{episode}.mkv"),
        media_id=5,
        episode=episode,
        raw={"category": category, "backend": "qbittorrent", "_backends": ["qbittorrent"], **raw},
    )


def _manager(tmp_path, monkeypatch, client):
    api = make_api(tmp_path)
    manager = api.manager
    manager.db.upsert_anime(LibraryAnime(media_id=5, title="Show", status="CURRENT", episodes=12))
    monkeypatch.setattr(manager, "downloads_configured", lambda: True)
    monkeypatch.setattr(manager, "downloads_enabled", lambda: True)
    monkeypatch.setattr(manager, "torrent_clients", lambda *a, **k: [("qbittorrent", client)])
    monkeypatch.setattr(manager, "_repair_legacy_qbittorrent_paths", lambda _client, _items: 0, raising=False)
    return api, manager


def test_row_added_during_the_listing_survives_the_prune(tmp_path, monkeypatch):
    listing_started, release = threading.Event(), threading.Event()
    rows: list[DownloadItem] = []

    class SlowClient:
        def torrents(self, category=""):
            listing_started.set()
            release.wait(5)
            return [dataclasses.replace(row, raw=dict(row.raw)) for row in rows]

        def close(self):
            pass

    api, manager = _manager(tmp_path, monkeypatch, SlowClient())
    category = api.config.qbittorrent.category
    old = _item(tmp_path, "aa" * 20, 1, category)
    gone = _item(tmp_path, "cc" * 20, 3, category)
    manager.db.upsert_download(old)
    manager.db.upsert_download(gone)  # removed in the client before this sync
    rows.append(old)
    time.sleep(0.01)
    errors: list[str] = []
    worker = threading.Thread(target=lambda: _run(manager, errors))
    worker.start()
    assert listing_started.wait(5)
    fresh = _item(tmp_path, "bb" * 20, 2, category, _intent_revision=3)
    manager.db.upsert_download(fresh)
    release.set()
    worker.join(10)
    assert errors == []
    stored = {item.torrent_hash: item for item in manager.db.downloads()}
    assert set(stored) == {"aa" * 20, "bb" * 20}
    assert stored["bb" * 20].raw.get("_intent_revision") == 3

    # The next sync, whose listing has it, keeps it; one without it removes it.
    rows.append(fresh)
    release.set()
    manager.sync_downloads()
    assert {item.torrent_hash for item in manager.db.downloads()} == {"aa" * 20, "bb" * 20}
    rows.remove(fresh)
    manager.sync_downloads()
    assert {item.torrent_hash for item in manager.db.downloads()} == {"aa" * 20}


def _run(manager, errors):
    try:
        manager.sync_downloads()
    except Exception as exc:  # pragma: no cover - reported by the assertion
        errors.append(repr(exc))


def test_syncs_in_one_process_run_one_at_a_time(tmp_path, monkeypatch):
    inside = {"now": 0, "max": 0}
    lock = threading.Lock()

    class CountingClient:
        def torrents(self, category=""):
            with lock:
                inside["now"] += 1
                inside["max"] = max(inside["max"], inside["now"])
            time.sleep(0.05)
            with lock:
                inside["now"] -= 1
            return []

        def close(self):
            pass

    _api, manager = _manager(tmp_path, monkeypatch, CountingClient())
    threads = [threading.Thread(target=manager.sync_downloads) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert inside["max"] == 1
