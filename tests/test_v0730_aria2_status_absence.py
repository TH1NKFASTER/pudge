from __future__ import annotations

import json

import httpx
import pytest

from pudge.manager import AnimeManager
from pudge.providers.aria2 import Aria2Client, Aria2Error


GID = "1234567890abcdef"
INFO_HASH = "a" * 40


@pytest.fixture
def rpc_client(tmp_path):
    clients = []

    def create(respond):
        client = Aria2Client(state_dir=tmp_path / f"aria2-{len(clients)}", auto_start=False)
        client._http.close()

        def handle(request):
            payload = json.loads(request.content)
            method, params = payload["method"], payload["params"][1:]
            if method == "aria2.getVersion":
                body = {"result": {"version": "1.37.0", "enabledFeatures": ["BitTorrent"]}}
            else:
                body = respond(method, params)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], **body})

        client._http = httpx.Client(transport=httpx.MockTransport(handle))
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.close()


@pytest.mark.parametrize("message", [
    f"GID #{GID}# is not found",
    f"GID {GID} is not found",
    f"Invalid GID {GID}",
])
def test_missing_gid_status_returns_none(rpc_client, message):
    def respond(method, _params):
        if method in {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"}:
            return {"result": []}
        assert method == "aria2.tellStatus"
        return {"error": {"code": 1, "message": message}}

    assert rpc_client(respond).torrent_status(GID) is None


@pytest.mark.parametrize("message", [
    "permission denied",
    "File not found",
    "Invalid GID unrelated-value",
    "GID #fedcba0987654321# is not found",
    f"permission denied; GID #{GID}# is not found",
])
def test_other_rpc_status_errors_are_not_absence(rpc_client, message):
    def respond(method, _params):
        if method in {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"}:
            return {"result": []}
        assert method == "aria2.tellStatus"
        return {"error": {"code": 1, "message": message}}

    with pytest.raises(Aria2Error):
        rpc_client(respond).torrent_status(GID)


def test_network_error_containing_missing_gid_text_is_not_absence(rpc_client):
    def respond(method, _params):
        if method in {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"}:
            return {"result": []}
        assert method == "aria2.tellStatus"
        raise httpx.ConnectError(f"GID #{GID}# is not found")

    with pytest.raises(Aria2Error, match="RPC недоступен"):
        rpc_client(respond).torrent_status(GID)


def test_gid_resolution_failure_is_not_absence(rpc_client):
    def respond(method, _params):
        assert method == "aria2.tellActive"
        return {"error": {"code": 1, "message": f"GID #{GID}# is not found"}}

    with pytest.raises(Aria2Error):
        rpc_client(respond).torrent_status(GID)


def test_manager_confirms_real_aria2_removal_after_metadata_is_deleted(tmp_path, rpc_client):
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"owned video")
    item = {"gid": GID, "infoHash": INFO_HASH, "status": "active",
            "files": [{"path": str(video), "length": "11", "completedLength": "11", "selected": "true"}]}
    tasks = {GID: item}

    def respond(method, params):
        if method == "aria2.tellActive":
            return {"result": list(tasks.values())}
        if method in {"aria2.tellWaiting", "aria2.tellStopped"}:
            return {"result": []}
        if method == "aria2.tellStatus":
            if params[0] in tasks:
                return {"result": tasks[params[0]]}
            return {"error": {"code": 1, "message": f"Invalid GID {params[0]}"}}
        if method == "aria2.forceRemove":
            tasks.pop(params[0])
            return {"result": params[0]}
        assert method in {"aria2.removeDownloadResult", "aria2.saveSession"}
        return {"result": "OK"}

    client = rpc_client(respond)
    client._save_metadata({GID: {"info_hash": INFO_HASH}})
    AnimeManager._delete_confirmed_torrent(client, INFO_HASH, delete_files=True)

    assert client.torrent_status(INFO_HASH) is None
    assert client._load_metadata() == {}
    assert not video.exists()
