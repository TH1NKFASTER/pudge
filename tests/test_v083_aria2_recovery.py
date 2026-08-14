from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.manager import AnimeManager
from pudge.providers.aria2 import Aria2Client


def test_aria2_missing_control_recovery_uses_hash_check_without_overwrite(
    tmp_path: Path, monkeypatch
) -> None:
    client = Aria2Client(state_dir=tmp_path, seed_mode="off")
    gid = "1234567890abcdef"
    client._save_metadata(
        {
            gid: {
                "source_url": "https://example.invalid/release.torrent",
                "save_path": str(tmp_path / "downloads"),
                "title": "episode.mkv",
            }
        }
    )
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_resolve_gid", lambda _value: gid)
    monkeypatch.setattr(client, "_torrent_payload", lambda _url: b"torrent-bytes")

    calls: list[tuple[str, list[object]]] = []

    def rpc(method: str, params=None):
        params = list(params or [])
        calls.append((method, params))
        if method == "aria2.tellStatus":
            return {
                "status": "error",
                "errorCode": "13",
                "errorMessage": "управляющий файл (*.aria2) отсутствует",
                "dir": str(tmp_path / "downloads"),
            }
        if method == "aria2.addTorrent":
            return gid
        return "OK"

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    try:
        assert client.reconnect(gid) is True
    finally:
        client.close()

    add_call = next(params for method, params in calls if method == "aria2.addTorrent")
    options = add_call[2]
    assert options["check-integrity"] == "true"
    assert options["seed-time"] == "0"
    assert "allow-overwrite" not in options
    assert any(method == "aria2.removeDownloadResult" for method, _ in calls)



def test_aria2_missing_control_recovery_synthesizes_magnet_for_legacy_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    client = Aria2Client(state_dir=tmp_path, seed_mode="off")
    gid = "1234567890abcdef"
    info_hash = "97c4bbed62e9237e64098c8ed481b28e9f1293d2"
    client._save_metadata(
        {
            gid: {
                "source_url": "https://example.invalid/release.torrent",
                "info_hash": info_hash,
                "save_path": str(tmp_path / "downloads"),
                "title": "episode name.mkv",
            }
        }
    )
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_resolve_gid", lambda _value: gid)
    monkeypatch.setattr(client, "_torrent_payload", lambda _url: None)

    calls: list[tuple[str, list[object]]] = []

    def rpc(method: str, params=None):
        params = list(params or [])
        calls.append((method, params))
        if method == "aria2.tellStatus":
            return {
                "status": "error",
                "errorCode": "13",
                "errorMessage": "управляющий файл (*.aria2) отсутствует",
                "dir": str(tmp_path / "downloads"),
            }
        if method == "aria2.addUri":
            return gid
        return "OK"

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    try:
        assert client.reconnect(info_hash) is True
    finally:
        client.close()

    add_call = next(params for method, params in calls if method == "aria2.addUri")
    magnet = str(add_call[0][0])
    options = add_call[1]
    assert magnet.startswith(f"magnet:?xt=urn:btih:{info_hash}")
    assert "dn=episode%20name.mkv" in magnet
    assert options["check-integrity"] == "true"
    assert "allow-overwrite" not in options


def test_aria2_seed_off_pauses_legacy_completed_seeder_and_reports_complete(
    tmp_path: Path, monkeypatch
) -> None:
    client = Aria2Client(state_dir=tmp_path, seed_mode="off")
    gid = "abcdef1234567890"
    client._save_metadata(
        {
            gid: {
                "info_hash": "a" * 40,
                "title": "done.mkv",
                "save_path": str(tmp_path),
                "added_on": 1,
            }
        }
    )
    monkeypatch.setattr(
        client,
        "_all_statuses",
        lambda: [
            {
                "gid": gid,
                "status": "active",
                "totalLength": "100",
                "completedLength": "100",
                "downloadSpeed": "0",
                "uploadSpeed": "0",
                "connections": "3",
                "numSeeders": "0",
                "seeder": "true",
                "dir": str(tmp_path),
                "files": [],
                "bittorrent": {"info": {"name": "done.mkv"}},
                "infoHash": "a" * 40,
                "errorCode": "",
                "errorMessage": "",
                "verifiedLength": "0",
                "verifyIntegrityPending": "false",
            }
        ],
    )
    calls: list[tuple[str, list[object]]] = []
    monkeypatch.setattr(
        client,
        "_rpc_raw",
        lambda method, params=None: calls.append((method, list(params or []))) or "OK",
    )
    try:
        rows = client.torrents()
    finally:
        client.close()

    assert len(rows) == 1
    assert rows[0].state == "complete"
    assert rows[0].progress == 1.0
    assert rows[0].raw["legacy_seeding_stopped"] is True
    assert ("aria2.forcePause", [gid]) in calls


def test_aria2_seed_off_reports_fully_downloaded_active_nonseeder_as_complete(
    tmp_path: Path, monkeypatch
) -> None:
    client = Aria2Client(state_dir=tmp_path, seed_mode="off")
    gid = "fedcba0987654321"
    client._save_metadata(
        {
            gid: {
                "info_hash": "b" * 40,
                "title": "done-nonseeder.mkv",
                "save_path": str(tmp_path),
                "added_on": 1,
            }
        }
    )
    monkeypatch.setattr(
        client,
        "_all_statuses",
        lambda: [
            {
                "gid": gid,
                "status": "active",
                "totalLength": "100",
                "completedLength": "100",
                "downloadSpeed": "0",
                "uploadSpeed": "0",
                "connections": "49",
                "numSeeders": "0",
                "seeder": "false",
                "dir": str(tmp_path),
                "files": [],
                "bittorrent": {"info": {"name": "done-nonseeder.mkv"}},
                "infoHash": "b" * 40,
                "errorCode": "",
                "errorMessage": "",
                "verifiedLength": "0",
                "verifyIntegrityPending": "false",
            }
        ],
    )
    calls: list[tuple[str, list[object]]] = []
    monkeypatch.setattr(
        client,
        "_rpc_raw",
        lambda method, params=None: calls.append((method, list(params or []))) or "OK",
    )
    try:
        rows = client.torrents()
    finally:
        client.close()

    assert len(rows) == 1
    assert rows[0].state == "complete"
    assert rows[0].progress == 1.0
    assert rows[0].raw["legacy_seeding_stopped"] is True
    assert ("aria2.forcePause", [gid]) in calls

def test_aria2_runtime_poll_never_rewrites_task_options(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path, seed_mode="off")
    calls: list[str] = []

    def rpc(method: str, params=None):
        calls.append(method)
        if method == "aria2.getGlobalOption":
            return {"seed-time": "0", "max-upload-limit": "0"}
        return "OK"

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    try:
        client._apply_runtime_options()
    finally:
        client.close()

    assert "aria2.changeOption" not in calls
    assert "aria2.tellActive" not in calls
    assert "aria2.tellWaiting" not in calls


def test_manager_treats_fully_downloaded_active_aria2_as_complete() -> None:
    item = SimpleNamespace(
        progress=1.0,
        state="active",
        raw={
            "backend": "aria2",
            "total_size": 1_400_000_000,
            "downloaded": 1_400_000_000,
            "verifying": False,
            "error_code": "",
        },
    )

    assert AnimeManager._download_is_complete(item) is True


def test_manager_does_not_publish_aria2_while_verifying_or_on_error() -> None:
    verifying = SimpleNamespace(
        progress=1.0,
        state="active",
        raw={
            "backend": "aria2",
            "total_size": 100,
            "downloaded": 100,
            "verifying": True,
            "error_code": "",
        },
    )
    failed = SimpleNamespace(
        progress=1.0,
        state="error",
        raw={
            "backend": "aria2",
            "total_size": 100,
            "downloaded": 100,
            "verifying": False,
            "error_code": "13",
        },
    )

    assert AnimeManager._download_is_complete(verifying) is False
    assert AnimeManager._download_is_complete(failed) is False
