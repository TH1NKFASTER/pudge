"""iPad: a downloaded episode that played sound without a picture (clock at 0:00) — the Mac side
of the download check the phone does before it lists an episode as downloaded.

The phone verifies the whole file against the SHA-256 the Mac computed for the prepared copy
(FileIntegrity) and, resuming a cut download, appends only a 206 whose Content-Range starts at
its offset and runs to the end of that size (RangedResume.decide; anything else starts from 0).
These tests hold the Mac to that contract: the hash is the copy's own, and a resumed request is
answered with exactly the rest of the file, or with the whole file when its If-Range is another
edition.
"""

from __future__ import annotations

import hashlib

import test_fix_iphone_video_stream as stream
from test_fix_iphone_video_stream import _connection, _get, _pair, _prepared

# The Mac and its HTTPS server, as the stream tests make them.
service = stream.service
server = stream.server


def test_the_hash_the_phone_checks_is_the_prepared_copys_own(service, tmp_path, monkeypatch) -> None:
    data = bytes(range(256)) * 4099
    _episode, state = _prepared(service, tmp_path, monkeypatch, data)
    video = state["video"]
    assert video["sha256"] == hashlib.sha256(data).hexdigest()
    assert video["sizeBytes"] == len(data)
    assert state["durationMs"] == 1440000  # the phone's duration check compares with this


def test_a_resumed_download_gets_exactly_the_rest_or_the_whole_file(
    service, server, tmp_path, monkeypatch
) -> None:
    data = bytes((index * 7) % 251 for index in range(2 * 1024 * 1024 + 13))
    size = len(data)
    _episode, state = _prepared(service, tmp_path, monkeypatch, data)
    video = state["video"]
    _device, token = _pair(service)
    # The download's route (no stream name): what MediaModel.transfer asks for.
    route = f"/api/v2/resources/{video['resourceId']}/content?sha256={video['sha256']}"
    connection = _connection(service, server)
    try:
        offset = 1_000_003
        etag = '"' + video["sha256"] + '"'
        status, headers, body = _get(
            connection, route, token, {"Range": f"bytes={offset}-", "If-Range": etag}
        )
        # RangedResume.decide → .append only for this exact answer.
        assert status == 206
        assert headers["content-range"] == f"bytes {offset}-{size - 1}/{size}"
        assert headers["etag"] == etag
        assert data[:offset] + body == data
        assert hashlib.sha256(data[:offset] + body).hexdigest() == video["sha256"]
        # Another edition's partial: the whole file (200 → .restart from 0), never a tail.
        status, headers, body = _get(
            connection, route, token, {"Range": f"bytes={offset}-", "If-Range": '"' + "0" * 64 + '"'}
        )
        assert status == 200 and "content-range" not in headers and body == data
        # Cut right at the end: 416 with the size (→ .complete, then verified).
        status, headers, _body = _get(connection, route, token, {"Range": f"bytes={size}-", "If-Range": etag})
        assert status == 416 and headers["content-range"] == f"bytes */{size}"
    finally:
        connection.close()
