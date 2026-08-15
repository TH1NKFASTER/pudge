from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryEpisode
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


def test_aria2_exposes_recovery_marker_on_metadata_only_task(
    tmp_path: Path, monkeypatch
) -> None:
    client = Aria2Client(state_dir=tmp_path, seed_mode="off")
    gid = "feb989d6d59f9869"
    info_hash = "feb989d6d59f98698b918c1f96061a0d3e638c74"
    client._save_metadata(
        {
            gid: {
                "info_hash": info_hash,
                "title": "episode.mkv",
                "save_path": str(tmp_path / "downloads"),
                "media_id": 182205,
                "episode": 18,
                "recovery_started_at": 123,
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
                "status": "waiting",
                "totalLength": "0",
                "completedLength": "0",
                "downloadSpeed": "0",
                "uploadSpeed": "0",
                "connections": "0",
                "numSeeders": "0",
                "seeder": "false",
                "dir": str(tmp_path / "downloads"),
                "files": [],
                "bittorrent": {},
                "infoHash": "",
                "errorCode": "",
                "errorMessage": "",
                "verifiedLength": "0",
                "verifyIntegrityPending": "false",
            }
        ],
    )
    try:
        rows = client.torrents()
    finally:
        client.close()

    assert len(rows) == 1
    assert rows[0].torrent_hash == info_hash
    assert rows[0].state == "waiting"
    assert rows[0].raw["recovery_started_at"] == 123


def test_manager_discards_zero_length_recovery_shell_when_exact_file_is_local(
    tmp_path: Path,
) -> None:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(config, log=lambda _message: None)
    info_hash = "feb989d6d59f98698b918c1f96061a0d3e638c74"
    video = tmp_path / "library" / "Slime" / "Slime S04E18.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"complete video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=182205,
            title="Slime",
            episode=18,
            video_path=video.resolve(),
            state="waiting_subtitles",
            torrent_hash=info_hash,
        )
    )

    item = SimpleNamespace(
        torrent_hash=info_hash,
        state="waiting",
        raw={
            "backend": "aria2",
            "total_size": 0,
            "downloaded": 0,
            "recovery_started_at": 123,
        },
    )

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, bool]] = []

        def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
            self.calls.append((torrent_hash, delete_files))

    client = Client()
    assert manager._discard_completed_aria2_recovery_tasks(client, [item]) == 1
    assert client.calls == [(info_hash, False)]
    assert video.is_file()


def test_manager_keeps_zero_length_recovery_without_exact_local_file(
    tmp_path: Path,
) -> None:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    manager = AnimeManager(config, log=lambda _message: None)
    item = SimpleNamespace(
        torrent_hash="a" * 40,
        state="waiting",
        raw={"total_size": 0, "downloaded": 0, "recovery_started_at": 123},
    )

    class Client:
        def delete(self, *_args, **_kwargs) -> None:
            raise AssertionError("must not delete an unresolved recovery")

    assert manager._discard_completed_aria2_recovery_tasks(Client(), [item]) == 0
