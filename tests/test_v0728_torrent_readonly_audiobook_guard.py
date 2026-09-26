"""Network-off regressions for aria2 observation and audiobook downloads."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.light_novels import LightNovelError
from pudge.providers.aria2 import Aria2Client, Aria2Error
from pudge.torrent_admission import TorrentAdmission
from pudge.web_app import WebAppApi


@pytest.mark.parametrize(
    "operation",
    [
        lambda client: client.traffic_stats(),
        lambda client: client.version(),
        lambda client: client.torrents(),
        lambda client: client.torrent_status("deadbeef"),
        lambda client: client.files("deadbeef"),
        lambda client: client.pause("deadbeef"),
        lambda client: client.delete("deadbeef", delete_files=False),
    ],
)
def test_missing_aria2_read_or_stop_never_spawns_sidecar(tmp_path: Path, monkeypatch, operation) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=True)
    starts = []
    monkeypatch.setattr(client, "_probe", lambda: False)
    monkeypatch.setattr(client, "_start", lambda: starts.append("unexpected spawn"))
    try:
        with pytest.raises(Aria2Error, match="RPC не запущен"):
            operation(client)
        assert not starts
    finally:
        client.close()


def test_running_aria2_status_does_not_reconfigure_or_restart(tmp_path: Path, monkeypatch) -> None:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=True)
    starts = []
    monkeypatch.setattr(client, "_probe", lambda: True)
    monkeypatch.setattr(client, "ensure_running", lambda: starts.append("ensure"))
    monkeypatch.setattr(client, "_start", lambda: starts.append("spawn"))

    def rpc(method, params=None):
        if method == "aria2.getGlobalStat":
            return {"downloadSpeed": "3", "uploadSpeed": "0", "numActive": "1", "numWaiting": "0"}
        if method in {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"}:
            return []
        raise AssertionError(method)

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    try:
        assert client.traffic_stats()["download_speed"] == 3
        assert client.torrents() == []
        assert starts == []
    finally:
        client.close()


class _FakeTorrent:
    def __init__(self, after_files=None):
        self.added = self.started = self.deleted = 0
        self.after_files = after_files
        self.closed = False

    def add_release(self, *_args, **_kwargs):
        self.added += 1
        return "managed-audiobook"

    def files(self, _torrent_hash):
        if self.after_files is not None:
            cb, self.after_files = self.after_files, None
            cb()
        return [{"index": 0, "name": "book.m4b", "size": 123, "progress": 0, "priority": 1}]

    def set_file_priority(self, *_args, **_kwargs):
        pass

    def start(self, _torrent_hash):
        self.started += 1

    def delete(self, *_args, **_kwargs):
        self.deleted += 1

    def close(self):
        self.closed = True


def _audiobook_api(tmp_path: Path, fake: _FakeTorrent) -> WebAppApi:
    api = object.__new__(WebAppApi)
    api.config = SimpleNamespace(
        config_path=tmp_path / "config.toml",
        nyaa=SimpleNamespace(torrents_enabled=True),
        qbittorrent=SimpleNamespace(enabled=False),
        aria2=SimpleNamespace(enabled=True),
        paths=SimpleNamespace(download_dirs=[str(tmp_path)]),
        library=SimpleNamespace(root_dir=str(tmp_path)),
    )
    api.light_novels = SimpleNamespace(book=lambda book_id: {
        "id": book_id, "title": "Book", "series_title": "Book", "volume": 1,
    })
    api.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    api._audiobook_torrent_client = lambda: ("aria2", fake)
    return api


def _release():
    return {"title": "Book volume 1", "info_hash": "a" * 40,
            "target_volume": 1, "selected_files": ["book.m4b"]}


def test_audiobook_add_blocked_by_shared_off_before_start(tmp_path: Path) -> None:
    fake = _FakeTorrent()
    api = _audiobook_api(tmp_path, fake)
    shared = TorrentAdmission(api.config.config_path)
    with shared.locked():
        shared.publish(False)
    with pytest.raises(LightNovelError, match="Enable torrents"):
        api.light_novel_download_audiobook_nyaa(1, _release())
    assert fake.added == fake.started == fake.deleted == 0
    assert fake.closed


def test_audiobook_late_start_blocked_when_off_during_metadata(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    shared = TorrentAdmission(config_path)
    with shared.locked():
        shared.publish(True)

    def disable_during_metadata():
        with shared.locked():
            shared.publish(False)

    fake = _FakeTorrent(after_files=disable_during_metadata)
    api = _audiobook_api(tmp_path, fake)
    with pytest.raises(LightNovelError, match="Enable torrents"):
        api.light_novel_download_audiobook_nyaa(1, _release())
    assert fake.added == 1
    assert fake.started == 0
    assert fake.deleted == 1
    assert fake.closed
