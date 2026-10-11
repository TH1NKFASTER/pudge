from pathlib import Path

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, NyaaRelease
from pudge.providers.aria2 import Aria2Client


def test_richest_magnet_prefers_tracker_rich_source() -> None:
    plain = "magnet:?xt=urn:btih:abc&dn=Episode"
    rich = plain + "&tr=udp%3A%2F%2Ftracker.one&tr=https%3A%2F%2Ftracker.two"
    release = NyaaRelease(
        title="Episode",
        link="",
        torrent_url=rich,
        info_hash="abc",
        size_text="1 GiB",
        size_bytes=1024,
        seeders=0,
        leechers=0,
        downloads=0,
        trusted=True,
        remake=False,
        score=1.0,
    )
    assert Aria2Client._preferred_release_source(release) == rich


def test_metadata_only_reconnect_upgrades_saved_tracker_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = Aria2Client(state_dir=tmp_path, auto_start=False)
    gid = "0123456789abcdef"
    plain = "magnet:?xt=urn:btih:abc&dn=Episode"
    rich = plain + "&tr=udp%3A%2F%2Ftracker.one"
    client._save_metadata(
        {
            gid: {
                "magnet": plain,
                "source_url": rich,
                "save_path": str(tmp_path),
            }
        }
    )
    calls = []

    def rpc(method: str, params=None):
        calls.append((method, params))
        if method == "aria2.addUri":
            return gid
        return "OK"

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    try:
        repaired = client._upgrade_metadata_only_source(
            gid,
            {
                "status": "waiting",
                "totalLength": "0",
                "completedLength": "0",
                "dir": str(tmp_path),
            },
        )
    finally:
        client.close()

    assert repaired
    assert client._load_metadata()[gid]["magnet"] == rich
    assert any(method == "aria2.forceRemove" for method, _ in calls)
    assert any(method == "aria2.addUri" for method, _ in calls)


class _Reconnects:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def reconnect(self, torrent_hash: str) -> bool:
        self.calls.append(torrent_hash)
        return True


def _metadata_only(added_on: int, *, state: str = "waiting") -> DownloadItem:
    # A magnet that never fetched its metadata: aria2 reports 0 of 0 bytes.
    return DownloadItem(
        torrent_hash="abc",
        name="Episode",
        state=state,
        progress=0.0,
        save_path="/library",
        content_path="/library/Episode",
        added_on=added_on,
        raw={"backend": "aria2", "downloaded": 0, "total_size": 0, "download_speed": 0, "num_connections": 0},
    )


def test_manager_counts_zero_length_metadata_as_stall_candidate(tmp_path: Path) -> None:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    manager = AnimeManager(config, log=lambda _message: None)
    client = _Reconnects()

    # Added 20 minutes before Pudge first sees it: the stall counts from the add time, so
    # the very first check already reconnects it.
    assert (
        manager._recover_stalled_aria2_downloads(client, [_metadata_only(added_on=1000)], now=1000 + 20 * 60)
        == 1
    )
    assert client.calls == ["abc"]

    # A fresh metadata-only magnet gets its 15 minutes first.
    fresh = _Reconnects()
    item = _metadata_only(added_on=5000)
    item.torrent_hash = "fresh"
    assert manager._recover_stalled_aria2_downloads(fresh, [item], now=5000 + 60) == 0
    assert manager._recover_stalled_aria2_downloads(fresh, [item], now=5000 + 16 * 60) == 1
    assert fresh.calls == ["fresh"]
