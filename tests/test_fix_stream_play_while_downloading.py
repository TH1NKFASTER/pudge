"""Play while downloading (iPhone/iPad): the phone plays an episode it is downloading from the
part already on the device and asks the Mac only for the rest, through the stream of the same
copy. The Mac's side of that contract:

- a stream opened while the episode downloads names the very copy being downloaded (same
  resource and SHA-256), so the phone's bytes and the Mac's bytes are of one file;
- the download's resumed range (``bytes=N-`` with If-Range) and the player's ranges of the
  stream ahead of it are served exactly side by side, and only the download counts as one.
"""

from __future__ import annotations

import os

import test_fix_iphone_video_stream as base
from test_fix_iphone_video_stream import _connection, _downloads, _eventually, _get, _pair, _post, _prepared

service = base.service
server = base.server


def test_a_stream_opened_while_downloading_serves_the_same_copy_beside_the_download(
    service,
    server,
    tmp_path,
    monkeypatch,
) -> None:
    data = os.urandom(300 * 1024)
    episode, state = _prepared(service, tmp_path, monkeypatch, data)
    video = state["video"]
    _device, token = _pair(service)
    download = _connection(service, server)
    player = _connection(service, server)
    try:
        base = f"/api/v2/resources/{video['resourceId']}/content?sha256={video['sha256']}"
        # The download has its first 100 KB (cut, resumed later).
        status, _headers, body = _get(download, base, token, {"Range": "bytes=0-102399"})
        assert status == 206 and body == data[:102400]
        # Play tapped: the stream of the same quality is the copy being downloaded.
        status, opened = _post(
            player, f"/api/v2/media/episodes/{episode}/stream", token, {"quality": "original"}
        )
        assert status == 200 and opened["video"] == video
        stream = opened["stream"]["id"]
        # The player seeks ahead of the download: those bytes come from the Mac.
        ahead = f"bytes={200 * 1024}-{250 * 1024 - 1}"
        status, headers, body = _get(player, base + f"&stream={stream}", token, {"Range": ahead})
        assert status == 206 and body == data[200 * 1024 : 250 * 1024]
        assert headers["content-range"] == f"bytes {200 * 1024}-{250 * 1024 - 1}/{len(data)}"
        # The download goes on in order from where it stopped, the same edition.
        status, headers, body = _get(
            download, base, token, {"Range": "bytes=102400-", "If-Range": '"' + video["sha256"] + '"'}
        )
        assert status == 206 and body == data[102400:]
        assert headers["content-range"] == f"bytes 102400-{len(data) - 1}/{len(data)}"
        # Only the download counts as one (the stream's last bytes never do).
        status, _headers, body = _get(
            player, base + f"&stream={stream}", token, {"Range": f"bytes={len(data) - 10}-"}
        )
        assert status == 206 and body == data[-10:]
        assert _eventually(lambda: _downloads(service) == 1)
        status, _answer = _post(player, f"/api/v2/media/streams/{stream}/close", token)
        assert status == 200
    finally:
        download.close()
        player.close()
