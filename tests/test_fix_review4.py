"""Review 4: aria2 ownership records change as one step; the phone API refuses NaN/Infinity."""

from __future__ import annotations

import http.client
import threading
from pathlib import Path

from pudge.iphone_http import IPhoneHTTPServer
from pudge.manager_models import NyaaRelease
from pudge.providers.aria2 import Aria2Client

LISTED_GID = "aaaaaaaaaaaaaaaa"
LISTED_HASH = "a" * 40
NEW_HASH = "b" * 40


def _release(info_hash: str) -> NyaaRelease:
    return NyaaRelease(
        title="[Group] Invented Show - 02 [1080p]",
        link="https://releases.invalid/view/2",
        torrent_url="",
        info_hash=info_hash,
        size_text="1 GiB",
        size_bytes=1024**3,
        seeders=5,
        leechers=1,
        downloads=10,
        trusted=True,
        remake=False,
        score=90.0,
    )


def _listing_client(tmp_path: Path, monkeypatch, meanwhile) -> Aria2Client:
    """A client whose listing finds one finished seeding task; ``meanwhile`` runs while it
    pauses that seeder (an RPC call between reading and saving the records)."""
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False, seed_mode="off")
    client._save_metadata(
        {LISTED_GID: {"info_hash": LISTED_HASH, "title": "Invented Show - 01", "save_path": str(tmp_path)}}
    )
    monkeypatch.setattr(
        client,
        "_all_statuses",
        lambda: [
            {
                "gid": LISTED_GID,
                "status": "active",
                "totalLength": "100",
                "completedLength": "100",
                "infoHash": LISTED_HASH,
                "files": [{"path": str(tmp_path / "Invented Show - 01.mkv"), "length": "100"}],
            }
        ],
    )

    def rpc(method: str, params=None):
        if method == "aria2.forcePause":
            meanwhile()
            return LISTED_GID
        raise AssertionError(method)

    monkeypatch.setattr(client, "_rpc_raw", rpc)
    monkeypatch.setattr(client, "_orphaned_completed_item", lambda *_args: None)
    return client


def test_listing_keeps_a_release_added_while_it_ran(tmp_path: Path, monkeypatch) -> None:
    adder = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    monkeypatch.setattr(adder, "_rpc_raw", lambda method, params=None: "bbbbbbbbbbbbbbbb")
    monkeypatch.setattr(adder, "ensure_running", lambda: None)
    monkeypatch.setattr(adder, "_torrent_payload", lambda _url: None)
    lister = _listing_client(
        tmp_path,
        monkeypatch,
        lambda: adder.add_release(_release(NEW_HASH), save_path=tmp_path, category="", tags=["anilist:7"]),
    )

    items = lister.torrents()

    assert [(item.torrent_hash, item.state) for item in items] == [(LISTED_HASH, "complete")]
    records = lister._load_metadata()
    # The listing's own changes and the release added meanwhile are both kept.
    assert records[LISTED_GID]["video_count"] == 1
    assert records[LISTED_GID]["completed_on"] > 0
    assert records["bbbbbbbbbbbbbbbb"]["info_hash"] == NEW_HASH


def test_listing_does_not_bring_back_a_record_deleted_while_it_ran(tmp_path: Path, monkeypatch) -> None:
    other = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    lister = _listing_client(tmp_path, monkeypatch, lambda: other._save_metadata({}))

    lister.torrents()

    assert lister._load_metadata() == {}


# --- the phone API refuses NaN / Infinity in request bodies ---------------------------------


class _Media:
    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def handle(self, command, route, query, body, device=""):
        self.bodies.append(body())
        return {"ok": True}


class _Service:
    server_id = "server"

    def __init__(self) -> None:
        self.media = _Media()
        self.checkpoints: list[dict] = []

    def authenticate(self, token):
        return "phone"

    def still_authorized(self, device):
        return True

    def record_checkpoint(self, device, payload):
        self.checkpoints.append(payload)
        return {"ok": True}


def _post(port: int, path: str, body: bytes) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request(
        "POST", path, body, {"Authorization": "Bearer token", "Content-Type": "application/json"}
    )
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status


def test_phone_api_refuses_non_finite_numbers():
    service = _Service()
    server = IPhoneHTTPServer(("127.0.0.1", 0), service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        route = "/api/v2/media/episodes/1/position"
        assert _post(port, route, b'{"seconds": Infinity, "duration": 1440}') == 422
        assert _post(port, "/api/v2/media/episodes/1/watched", b'{"activeSeconds": NaN}') == 422
        assert _post(port, "/api/v2/checkpoints", b'{"positionMs": -Infinity}') == 409
        assert service.media.bodies == [] and service.checkpoints == []
        # Ordinary numbers are untouched.
        assert _post(port, route, b'{"seconds": 12.5, "duration": 1440}') == 200
        assert service.media.bodies == [{"seconds": 12.5, "duration": 1440}]
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)
