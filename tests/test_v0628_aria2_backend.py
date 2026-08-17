from __future__ import annotations

from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, NyaaRelease
from pudge.providers.aria2 import Aria2Client


def release() -> NyaaRelease:
    return NyaaRelease(
        title="[Group] Example - 01 [1080p]",
        link="https://nyaa.si/view/1",
        torrent_url="https://nyaa.si/download/1.torrent",
        info_hash="0123456789abcdef0123456789abcdef01234567",
        size_text="1 GiB",
        size_bytes=1024**3,
        seeders=20,
        leechers=2,
        downloads=100,
        trusted=True,
        remake=False,
        score=96.0,
    )


def test_aria2_config_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = AppConfig()
    config.aria2.enabled = True
    config.aria2.binary = "/opt/homebrew/bin/aria2c"
    config.aria2.rpc_port = 6812
    write_config(config, path)

    loaded = load_config(path)
    assert loaded.aria2.enabled is True
    assert loaded.aria2.binary == "aria2c"
    assert loaded.aria2.rpc_port == 6812


def test_manager_uses_aria2_when_qbittorrent_is_disabled(tmp_path: Path) -> None:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.qbittorrent.enabled = False
    config.aria2.enabled = True
    config.aria2.binary = "/tmp/aria2c"
    manager = AnimeManager(config)

    client = manager.qbt_client()
    try:
        assert isinstance(client, Aria2Client)
        assert manager.downloads_enabled() is True
        assert manager.torrent_backend_name() == "aria2"
    finally:
        client.close()


def test_aria2_add_and_list_preserves_anime_metadata(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    calls: list[tuple[str, list[object]]] = []
    gid = "0123456789abcdef"

    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_torrent_payload", lambda _url: None)

    def rpc(method: str, params=None):
        params = list(params or [])
        calls.append((method, params))
        if method == "aria2.addUri":
            return gid
        if method == "aria2.tellActive":
            return [
                {
                    "gid": gid,
                    "status": "active",
                    "totalLength": "1000",
                    "completedLength": "250",
                    "downloadSpeed": "100",
                    "dir": str(tmp_path / "downloads"),
                    "infoHash": release().info_hash,
                    "files": [
                        {
                            "path": str(tmp_path / "downloads" / "Example 01.mkv"),
                            "length": "1000",
                            "completedLength": "250",
                            "selected": "true",
                        }
                    ],
                    "bittorrent": {"info": {"name": "Example 01.mkv"}},
                }
            ]
        if method in {"aria2.tellWaiting", "aria2.tellStopped"}:
            return []
        raise AssertionError((method, params))

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    client.add_release(
        release(),
        save_path=tmp_path / "downloads",
        category="pudge",
        tags=["anime: Example", "anilist: 123", "episode: 1", "score: 96"],
    )
    items = client.torrents()

    assert len(items) == 1
    item = items[0]
    assert item.torrent_hash == release().info_hash
    assert item.progress == 0.25
    assert item.media_id == 123
    assert item.episode == 1
    assert item.raw["backend"] == "aria2"
    assert item.raw["_release_score_tag"] == 96.0
    assert any(method == "aria2.addUri" for method, _ in calls)
    client.close()


def test_aria2_prefers_tracker_rich_torrent_payload(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    calls: list[tuple[str, list[object]]] = []
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_torrent_payload", lambda _url: b"d4:infode")

    def rpc(method: str, params=None):
        values = list(params or [])
        calls.append((method, values))
        if method == "aria2.addTorrent":
            return "0123456789abcdef"
        raise AssertionError((method, values))

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    client.add_release(
        release(),
        save_path=tmp_path / "downloads",
        category="pudge",
        tags=["anime: Example", "anilist: 123"],
    )

    assert [method for method, _params in calls] == ["aria2.addTorrent"]
    metadata = client._load_metadata()["0123456789abcdef"]
    assert metadata["listed_seeders"] == 20
    assert metadata["listed_leechers"] == 2
    client.close()


def test_aria2_large_torrent_avoids_default_rpc_request_limit(
    tmp_path: Path, monkeypatch
) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    calls: list[tuple[str, list[object]]] = []
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(
        client,
        "_torrent_payload",
        lambda _url: b"d" + b"x" * (client._SAFE_RPC_TORRENT_BYTES + 1),
    )

    def rpc(method: str, params=None):
        values = list(params or [])
        calls.append((method, values))
        if method == "aria2.addUri":
            return "0123456789abcdef"
        raise AssertionError((method, values))

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    client.add_release(
        release(),
        save_path=tmp_path / "downloads",
        category="pudge",
        tags=["anime: Example", "anilist: 123"],
    )

    assert [method for method, _params in calls] == ["aria2.addUri"]
    assert str(calls[0][1][0][0]).startswith("magnet:?")
    client.close()


def test_aria2_start_and_pause_are_idempotent(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    calls: list[str] = []
    states = iter(("active", "paused", "paused", "active"))
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_resolve_gid", lambda _value: "0123456789abcdef")

    def rpc(method: str, params=None):
        calls.append(method)
        if method == "aria2.tellStatus":
            return {"status": next(states)}
        if method in {"aria2.unpause", "aria2.pause"}:
            return "OK"
        raise AssertionError((method, params))

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    client.start("hash")
    client.start("hash")
    client.pause("hash")
    client.pause("hash")

    assert calls == [
        "aria2.tellStatus",
        "aria2.tellStatus",
        "aria2.unpause",
        "aria2.tellStatus",
        "aria2.tellStatus",
        "aria2.pause",
    ]
    client.close()


def test_aria2_reconnect_force_pauses_and_unpauses_without_removing_data(
    tmp_path: Path, monkeypatch
) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    calls: list[str] = []
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_resolve_gid", lambda _value: "0123456789abcdef")

    def rpc(method: str, params=None):
        calls.append(method)
        if method == "aria2.tellStatus":
            return {"status": "active", "totalLength": "1000", "completedLength": "532"}
        if method in {"aria2.forcePause", "aria2.unpause"}:
            return "OK"
        raise AssertionError((method, params))

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    assert client.reconnect("hash") is True
    assert calls == ["aria2.tellStatus", "aria2.forcePause", "aria2.unpause"]
    assert "aria2.forceRemove" not in calls
    client.close()


def test_manager_reconnects_aria2_after_progress_is_stalled(
    tmp_path: Path,
) -> None:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    manager = AnimeManager(config)

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def reconnect(self, torrent_hash: str) -> bool:
            self.calls.append(torrent_hash)
            return True

    client = Client()
    item = DownloadItem(
        torrent_hash="abc",
        name="Akiba",
        state="active",
        progress=0.532,
        save_path=str(tmp_path),
        content_path=str(tmp_path / "Akiba"),
        raw={
            "backend": "aria2",
            "downloaded": 532,
            "total_size": 1000,
            "download_speed": 0,
            "num_connections": 0,
            "listed_seeders": 22,
        },
    )
    assert manager._recover_stalled_aria2_downloads(client, [item], now=1000) == 0
    assert manager._recover_stalled_aria2_downloads(client, [item], now=1901) == 1
    assert client.calls == ["abc"]


def test_manager_does_not_reconnect_aria2_during_piece_verification(
    tmp_path: Path,
) -> None:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    manager = AnimeManager(config)

    class Client:
        def reconnect(self, torrent_hash: str) -> bool:
            raise AssertionError(torrent_hash)

    item = DownloadItem(
        torrent_hash="abc",
        name="Akiba",
        state="verifying",
        progress=1.0,
        save_path=str(tmp_path),
        content_path=str(tmp_path / "Akiba"),
        raw={
            "backend": "aria2",
            "downloaded": 1000,
            "total_size": 1000,
            "download_speed": 0,
            "num_connections": 0,
            "verifying": True,
        },
    )

    assert manager._recover_stalled_aria2_downloads(Client(), [item], now=1000) == 0
    assert manager._recover_stalled_aria2_downloads(Client(), [item], now=5000) == 0
    assert manager._download_is_complete(item) is False


def test_manager_eventually_reconnects_zero_progress_aria2_download(
    tmp_path: Path,
) -> None:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    manager = AnimeManager(config)

    class Client:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def reconnect(self, torrent_hash: str) -> bool:
            self.calls.append(torrent_hash)
            return True

    client = Client()
    item = DownloadItem(
        torrent_hash="abc",
        name="Akiba",
        state="active",
        progress=0.0,
        save_path=str(tmp_path),
        content_path=str(tmp_path / "Akiba"),
        raw={
            "backend": "aria2",
            "downloaded": 0,
            "total_size": 1000,
            "download_speed": 0,
            "num_connections": 0,
        },
    )

    assert manager._recover_stalled_aria2_downloads(client, [item], now=1000) == 0
    assert manager._recover_stalled_aria2_downloads(client, [item], now=1901) == 1
    assert client.calls == ["abc"]


def test_aria2_exposes_verification_without_publishing_completion(
    tmp_path: Path, monkeypatch
) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    gid = "0123456789abcdef"
    client._save_metadata(
        {
            gid: {
                "info_hash": release().info_hash,
                "title": release().title,
                "save_path": str(tmp_path / "downloads"),
                "media_id": 123,
            }
        }
    )
    monkeypatch.setattr(client, "ensure_running", lambda: None)

    def rpc(method: str, params=None):
        if method == "aria2.tellActive":
            return [
                {
                    "gid": gid,
                    "status": "active",
                    "totalLength": "1000",
                    "completedLength": "1000",
                    "verifiedLength": "750",
                    "verifyIntegrityPending": "true",
                    "dir": str(tmp_path / "downloads"),
                    "infoHash": release().info_hash,
                    "files": [],
                }
            ]
        if method in {"aria2.tellWaiting", "aria2.tellStopped"}:
            return []
        raise AssertionError((method, params))

    monkeypatch.setattr(client, "_rpc_raw", rpc)

    item = client.torrents()[0]

    assert item.state == "verifying"
    assert item.progress == 1.0
    assert item.completed_on == 0
    assert item.raw["verification_progress"] == 0.75
    client.close()


def test_aria2_repairs_only_metadata_only_magnet(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    gid = "0123456789abcdef"
    client._save_metadata(
        {
            gid: {
                "info_hash": release().info_hash,
                "title": release().title,
                "save_path": str(tmp_path / "downloads"),
                "media_id": 123,
            }
        }
    )
    calls: list[str] = []
    monkeypatch.setattr(client, "ensure_running", lambda: None)
    monkeypatch.setattr(client, "_resolve_gid", lambda _value: gid)
    monkeypatch.setattr(client, "_torrent_payload", lambda _url: b"d4:infode")

    def rpc(method: str, params=None):
        calls.append(method)
        if method == "aria2.tellStatus":
            return {
                "status": "active",
                "totalLength": "0",
                "completedLength": "0",
                "files": [],
            }
        if method in {"aria2.forceRemove", "aria2.removeDownloadResult"}:
            return "OK"
        if method == "aria2.addTorrent":
            return gid
        raise AssertionError((method, params))

    monkeypatch.setattr(client, "_rpc_raw", rpc)

    assert client.repair_stalled_release(release().info_hash, release()) is True
    assert calls == [
        "aria2.tellStatus",
        "aria2.forceRemove",
        "aria2.removeDownloadResult",
        "aria2.addTorrent",
    ]
    metadata = client._load_metadata()[gid]
    assert metadata["media_id"] == 123
    assert metadata["listed_seeders"] == 20
    client.close()


def test_aria2_delete_removes_owned_metadata(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    gid = "0123456789abcdef"
    metadata = {
        gid: {
            "info_hash": release().info_hash,
            "title": release().title,
            "save_path": str(tmp_path),
            "added_on": 1,
        }
    }
    client._save_metadata(metadata)
    monkeypatch.setattr(client, "ensure_running", lambda: None)

    def rpc(method: str, params=None):
        if method in {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"}:
            return []
        if method == "aria2.tellStatus":
            return {"status": "paused", "files": [], "dir": str(tmp_path)}
        if method in {"aria2.forceRemove", "aria2.removeDownloadResult", "aria2.saveSession"}:
            return "OK"
        raise AssertionError(method)

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    client.delete(release().info_hash, delete_files=False)
    assert client._load_metadata() == {}
    client.close()


def test_release_contains_aria2_ui_and_installer() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    installer = (root / "install.sh").read_text(encoding="utf-8")
    assert "s_aria2_enabled" in html
    assert "pywebview.api.test_aria2" in html
    assert "brew --prefix aria2" in installer
    assert "PUDGE_ARIA2C" in installer


def test_aria2_hides_magnet_metadata_parent_and_keeps_payload(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    parent = "0123456789abcdef"
    child = "fedcba9876543210"
    client._save_metadata(
        {
            parent: {
                "info_hash": release().info_hash,
                "title": release().title,
                "save_path": str(tmp_path),
                "media_id": 123,
                "episode": 1,
                "is_batch": False,
                "added_on": 1,
                "release_score": 96.0,
            }
        }
    )
    monkeypatch.setattr(client, "ensure_running", lambda: None)

    def rpc(method: str, params=None):
        if method == "aria2.tellActive":
            return [
                {
                    "gid": parent,
                    "status": "complete",
                    "totalLength": "0",
                    "completedLength": "0",
                    "followedBy": [child],
                    "files": [],
                },
                {
                    "gid": child,
                    "status": "active",
                    "totalLength": "100",
                    "completedLength": "50",
                    "infoHash": release().info_hash,
                    "dir": str(tmp_path),
                    "files": [{"path": str(tmp_path / "episode.mkv"), "length": "100", "completedLength": "50"}],
                    "bittorrent": {"info": {"name": "episode.mkv"}},
                },
            ]
        if method in {"aria2.tellWaiting", "aria2.tellStopped"}:
            return []
        raise AssertionError(method)

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    items = client.torrents()
    assert len(items) == 1
    assert items[0].raw["gid"] == child
    assert items[0].media_id == 123
    assert client._resolve_gid(release().info_hash) == child
    client.close()


def test_first_experience_explains_qbittorrent_is_optional() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert "автоматические загрузки выполнит управляемый aria2c" in html
    assert "с меньшим набором функций" in html
    assert "Torrent: ${d.qbt_enabled?'qBittorrent':'aria2'}" in html
