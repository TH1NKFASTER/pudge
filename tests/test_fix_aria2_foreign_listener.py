"""aria2: a foreign process on the RPC port is never trusted, and delete() never unlinks outside
the download folder Pudge recorded for that torrent (WEBSEC-2)."""

from __future__ import annotations

import http.server
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import pudge.providers.aria2 as aria2_module
from pudge.providers.aria2 import Aria2Client, Aria2Error

REAL_LISTENER_PIDS = Aria2Client._rpc_listener_pids  # conftest answers "cannot tell" by default
HASH = "c" * 40
GID = "0123456789abcdef"


def _client(tmp_path: Path, **kwargs) -> Aria2Client:
    return Aria2Client(state_dir=tmp_path / "aria2", auto_start=False, **kwargs)


def _record(client: Aria2Client, save_path: Path | None) -> None:
    meta = {"info_hash": HASH, "title": "Show - 01"}
    if save_path is not None:
        meta["save_path"] = str(save_path)
    (client.state_dir / "metadata.json").write_text(json.dumps({GID: meta}), encoding="utf-8")


def _status_rpc(files: list[Path], directory: Path):
    def rpc(method, params=None):
        if method == "aria2.tellStatus":
            return {"status": "complete", "dir": str(directory), "files": [{"path": str(p)} for p in files]}
        if method == "aria2.tellStopped":
            return []
        return "OK"

    return rpc


def test_delete_unlinks_only_files_inside_the_recorded_download_folder(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    downloads = tmp_path / "Library" / "Show"
    downloads.mkdir(parents=True)
    own = downloads / "Show - 01.mkv"
    own.write_bytes(b"video")
    (downloads / "Show - 01.mkv.aria2").write_bytes(b"ctl")
    victim = tmp_path / "Documents" / "thesis.docx"
    victim.parent.mkdir()
    victim.write_text("precious", encoding="utf-8")
    escape = downloads / "link.docx"
    escape.symlink_to(victim)
    traversal = downloads / ".." / ".." / "Documents" / "thesis.docx"
    _record(client, downloads)
    monkeypatch.setattr(client, "_require_observable", lambda: None)
    monkeypatch.setattr(client, "_resolve_gid", lambda _hash: GID)
    monkeypatch.setattr(client, "_rpc_raw", _status_rpc([own, victim, escape, traversal], tmp_path))
    try:
        client.delete(HASH, delete_files=True)
    finally:
        client.close()
    assert not own.exists() and not (downloads / "Show - 01.mkv.aria2").exists()
    assert victim.read_text(encoding="utf-8") == "precious"


def test_delete_of_a_torrent_without_pudges_record_unlinks_nothing(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    folder = tmp_path / "Downloads"
    folder.mkdir()
    video = folder / "x.mkv"
    video.write_bytes(b"video")
    _record(client, None)
    monkeypatch.setattr(client, "_require_observable", lambda: None)
    monkeypatch.setattr(client, "_resolve_gid", lambda _hash: GID)
    monkeypatch.setattr(client, "_rpc_raw", _status_rpc([video], folder))
    try:
        client.delete(HASH, delete_files=True)
    finally:
        client.close()
    assert video.exists()


class _FakeAria2Handler(http.server.BaseHTTPRequestHandler):
    """Any program on the port: answers "Unauthorized" to Pudge's wrong-token probe only."""

    tokens: list[str] = []
    victim = ""

    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        token = str((body.get("params") or [""])[0])
        type(self).tokens.append(token)
        if token.startswith("token:not-"):
            out = {"jsonrpc": "2.0", "id": body["id"], "error": {"code": 1, "message": "Unauthorized"}}
        else:
            result = {
                "aria2.getSessionInfo": {"sessionId": "abc"},
                "aria2.getVersion": {"version": "1.37.0"},
                "aria2.tellStatus": {
                    "status": "complete",
                    "dir": "/",
                    "files": [{"path": type(self).victim}],
                },
            }.get(body["method"], "OK")
            out = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def foreign_listener(tmp_path: Path):
    _FakeAria2Handler.tokens = []
    victim = tmp_path / "Documents" / "thesis.docx"
    victim.parent.mkdir()
    victim.write_text("precious", encoding="utf-8")
    _FakeAria2Handler.victim = str(victim)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeAria2Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], victim
    finally:
        server.shutdown()
        server.server_close()


def test_foreign_listener_never_gets_the_secret_or_a_delete(
    tmp_path: Path, monkeypatch, foreign_listener
) -> None:
    port, victim = foreign_listener
    monkeypatch.setattr(Aria2Client, "_rpc_listener_pids", REAL_LISTENER_PIDS)
    client = _client(tmp_path, rpc_port=port)
    try:
        pids = client._rpc_listener_pids()
        if pids is None:
            pytest.skip("lsof is not available")
        assert pids == [os.getpid()]  # this test process (python) listens, not aria2c
        assert client._rpc_listener_trusted() is False
        assert client._rpc_is_own_sidecar() is False
        assert client._probe() is False
        with pytest.raises(Aria2Error, match=str(port)):
            client.ensure_running()
        with pytest.raises(Aria2Error):
            client.delete(HASH, delete_files=True)
    finally:
        client.close()
    assert not any(client._secret in token for token in _FakeAria2Handler.tokens)
    assert victim.read_text(encoding="utf-8") == "precious"


def test_port_held_by_another_account_is_not_trusted(tmp_path: Path, monkeypatch, foreign_listener) -> None:
    port, _victim = foreign_listener
    client = _client(tmp_path, rpc_port=port)
    # lsof of an ordinary user does not list another account's processes.
    monkeypatch.setattr(client, "_rpc_listener_pids", lambda: [])
    try:
        assert client._rpc_is_own_sidecar() is False
        assert client._probe() is False
    finally:
        client.close()
    assert _FakeAria2Handler.tokens == []


def test_pudges_own_or_same_account_aria2_is_trusted(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    monkeypatch.setattr(Aria2Client, "_own_process_alive", staticmethod(lambda pid: True))
    monkeypatch.setattr(Aria2Client, "_process_start_time", staticmethod(lambda pid: f"start-{pid}"))
    try:
        client._managed_process = SimpleNamespace(pid=4242, poll=lambda: None)
        monkeypatch.setattr(client, "_process_is_own_aria2", lambda pid: False)
        monkeypatch.setattr(client, "_rpc_listener_pids", lambda: [4242])
        assert client._rpc_listener_trusted() is True
        # Proven once: while that process lives, no lsof again.
        monkeypatch.setattr(client, "_rpc_listener_pids", lambda: (_ for _ in ()).throw(AssertionError))
        assert client._rpc_listener_trusted() is True

        aria2_module._TRUSTED_RPC_LISTENERS.clear()
        client._managed_process = None  # a sidecar started by an earlier Pudge process
        monkeypatch.setattr(client, "_rpc_listener_pids", lambda: [5151])
        monkeypatch.setattr(client, "_process_is_own_aria2", lambda pid: pid == 5151)
        assert client._rpc_listener_trusted() is True
        aria2_module._TRUSTED_RPC_LISTENERS.clear()
        monkeypatch.setattr(client, "_process_is_own_aria2", lambda pid: False)
        assert client._rpc_listener_trusted() is False
        monkeypatch.setattr(client, "_rpc_listener_pids", lambda: None)
        assert client._rpc_listener_trusted() is None  # no lsof: only the token test decides
    finally:
        client.close()


def test_this_python_process_is_not_an_aria2c(tmp_path: Path) -> None:
    client = _client(tmp_path)
    try:
        assert client._process_is_own_aria2(os.getpid()) is False
    finally:
        client.close()


def test_many_status_polls_run_lsof_once(tmp_path: Path, monkeypatch, foreign_listener) -> None:
    """The manager makes a client per call: proof of the listener is shared and kept while it lives."""
    port, _victim = foreign_listener
    lsof_calls: list[int] = []
    start_checks: list[int] = []
    real_start_time = Aria2Client._process_start_time

    def lsof(self):
        lsof_calls.append(1)
        return [os.getpid()]

    def start_time(pid):
        start_checks.append(pid)
        return real_start_time(pid)

    monkeypatch.setattr(Aria2Client, "_rpc_listener_pids", lsof)
    monkeypatch.setattr(Aria2Client, "_process_start_time", staticmethod(start_time))
    monkeypatch.setattr(Aria2Client, "_process_is_own_aria2", lambda self, pid: True)  # pretend: aria2c

    def poll() -> bool:
        client = _client(tmp_path, rpc_port=port)
        try:
            return client._probe()
        finally:
            client.close()

    assert all(poll() for _ in range(12))
    assert len(lsof_calls) == 1 and start_checks == [os.getpid()]

    # Not seen for a while: one cheap start-time check, still no lsof.
    pid, started, _seen = aria2_module._TRUSTED_RPC_LISTENERS[port]
    aria2_module._TRUSTED_RPC_LISTENERS[port] = (pid, started, aria2_module.time.monotonic() - 60)
    assert poll()
    assert len(lsof_calls) == 1 and len(start_checks) == 2

    # The pid now names another process (reused): the port owner is checked again.
    aria2_module._TRUSTED_RPC_LISTENERS[port] = (pid, "another start", aria2_module.time.monotonic() - 60)
    assert poll()
    assert len(lsof_calls) == 2

    # A refused connection (aria2 stopped) forgets the proof as well.
    client = _client(tmp_path, rpc_port=port)
    client._http.close()
    client._http = httpx.Client(transport=_refusing_transport())
    try:
        with pytest.raises(Aria2Error):
            client._rpc_raw("aria2.getVersion")
    finally:
        client.close()
    assert port not in aria2_module._TRUSTED_RPC_LISTENERS
    assert poll()
    assert len(lsof_calls) == 3


def test_dead_or_foreign_pid_is_no_longer_trusted(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path)
    try:
        aria2_module._TRUSTED_RPC_LISTENERS[client.rpc_port] = (
            2**22 + 12345,
            "x",
            aria2_module.time.monotonic(),
        )
        assert client._trusted_listener_still_there() is False  # no such process
        assert Aria2Client._own_process_alive(os.getpid()) is True
        assert Aria2Client._own_process_alive(1) is (os.getuid() == 0)  # launchd/init: another account's
    finally:
        client.close()


def _refusing_transport():
    def handle(request):
        raise httpx.ConnectError("Connection refused", request=request)

    return httpx.MockTransport(handle)
