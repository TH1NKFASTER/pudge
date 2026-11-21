"""Watching an episode from the Mac on the iPad stalled: AVPlayer asks for the prepared MP4 in
many byte ranges (a 256 KB first chunk, then 2 MB chunks, two at a time), and every answer of
the Mac's HTTPS server came late:

- Nagle: an answer is written in parts (headers, then the body) and the later part waited for
  the phone's delayed ACK (40 ms on Linux, up to ~200 ms on Apple devices) on every range;
- each range looked the file up and checked its hash in SQLite (three connections, each
  parsing the library's schema: milliseconds per range).

Measured locally (TLS over loopback, one kept-alive connection): 16 KB ranges 20 -> 430 per
second, 2 MB ranges 108 -> 536 MB/s.
"""

from __future__ import annotations

import os
import socket
import time

from test_fix_iphone_video_stream import _connection, _get, _pair, _prepared, server, service  # noqa: F401


def _route(state: dict) -> str:
    video = state["video"]
    return f"/api/v2/resources/{video['resourceId']}/content?sha256={video['sha256']}&stream=play"


def test_connections_send_without_nagle_delay(service, server) -> None:  # noqa: F811
    seen: list[int] = []
    original = type(server).finish_request

    def finish_request(self, request, client_address):
        seen.append(request.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
        return original(self, request, client_address)

    type(server).finish_request = finish_request
    try:
        connection = _connection(service, server)
        try:
            connection.request("GET", "/api/v2/capabilities")
            assert connection.getresponse().read()
        finally:
            connection.close()
    finally:
        type(server).finish_request = original
    assert seen and all(value != 0 for value in seen)


def test_repeated_stream_ranges_do_not_query_the_database_for_the_file(
    service,
    server,
    tmp_path,
    monkeypatch,  # noqa: F811
) -> None:
    data = os.urandom(1024 * 1024)
    _episode, state = _prepared(service, tmp_path, monkeypatch, data)
    _device, token = _pair(service)
    route = _route(state)
    opened = []
    original = type(service.db)._open_connection

    def counting(db):
        opened.append(1)
        return original(db)

    connection = _connection(service, server)
    try:
        assert _get(connection, route, token, {"Range": "bytes=0-1"})[0] == 206
        monkeypatch.setattr(type(service.db), "_open_connection", counting)
        for index in range(10):
            start = index * 65536
            status, _headers, body = _get(
                connection, route, token, {"Range": f"bytes={start}-{start + 65535}"}
            )
            assert status == 206 and body == data[start : start + 65536]
    finally:
        connection.close()
    # Authentication only (one connection per request); the file's row and hash are known.
    assert len(opened) <= 10


def test_a_changed_file_is_checked_again(service, server, tmp_path, monkeypatch) -> None:  # noqa: F811
    data = os.urandom(256 * 1024)
    _episode, state = _prepared(service, tmp_path, monkeypatch, data)
    _device, token = _pair(service)
    route = _route(state)
    connection = _connection(service, server)
    try:
        assert _get(connection, route, token, {"Range": "bytes=0-1"})[0] == 206
        path = service.resource(state["video"]["resourceId"], state["video"]["sha256"])
        path.write_bytes(os.urandom(len(data)))  # same size, other bytes, new mtime
        stamp = path.stat().st_mtime_ns + 5_000_000_000
        os.utime(path, ns=(stamp, stamp))
        assert _get(connection, route, token, {"Range": "bytes=0-1"})[0] == 409
    finally:
        connection.close()


def test_small_ranges_on_a_kept_alive_connection_answer_quickly(
    service,
    server,
    tmp_path,
    monkeypatch,  # noqa: F811
) -> None:
    data = os.urandom(2 * 1024 * 1024)
    _episode, state = _prepared(service, tmp_path, monkeypatch, data)
    _device, token = _pair(service)
    route = _route(state)
    connection = _connection(service, server)
    try:
        assert _get(connection, route, token, {"Range": "bytes=0-1"})[0] == 206
        began = time.monotonic()
        for index in range(20):
            start = index * 16384
            assert _get(connection, route, token, {"Range": f"bytes={start}-{start + 16383}"})[0] == 206
        elapsed = time.monotonic() - began
    finally:
        connection.close()
    # Before: ~45 ms each (delayed ACK + database); now a few ms. Generous for a busy machine.
    assert elapsed < 0.5, elapsed
