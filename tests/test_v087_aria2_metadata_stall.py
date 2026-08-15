from pathlib import Path

from pudge.manager_models import NyaaRelease
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


def test_manager_counts_zero_length_metadata_as_stall_candidate() -> None:
    source = Path("pudge/manager.py").read_text(encoding="utf-8")
    assert "(total == 0 and downloaded == 0)" in source
    assert "float(item.added_on)" in source
