"""Watching an episode on the iPhone/iPad straight from the Mac (Watch, no download first).

The phone's player (AVPlayer through a resource loader) asks the Mac for byte ranges of the
prepared MP4 over the paired HTTPS connection:

- every edge range is served exactly (first bytes, last byte, open-ended, suffix, clipped end) and
  unsatisfiable ones get 416 without breaking the kept-alive connection;
- a stream holds the prepared copy while the player is open (one hold per stream), and lets it
  go when the player closes, when the phone stops renewing it (Wi-Fi gone, app closed) or when
  the device is revoked; its byte ranges renew it and never count as a download;
- the copy is streamable: ffmpeg writes it with ``-movflags +faststart`` (moov before mdat) in
  every path (remux of H.264/HEVC, re-encode).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import ssl
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.database import Database
from test_iphone_audio_repository import allowed


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, text: str, *args) -> None:
        self.lines.append(text % args if args else text)

    warning = info


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    value = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    value.api = SimpleNamespace(logger=_Log())
    return value


def _episode(service, tmp_path: Path, number: int = 3) -> int:
    video = tmp_path / f"[SubsPlease] Dorohedoro - {number:02d} (1080p).mkv"
    video.write_bytes(b"x" * (10 + number))
    with service.db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO anime(media_id,title,status,progress,updated_at) "
            "VALUES(105228,'Dorohedoro','CURRENT',2,0)"
        )
        return conn.execute(
            "INSERT INTO episodes(media_id,title,episode,video_path,state,updated_at) VALUES(105228,'Ep',?,?,'ready',0)",
            (number, str(video)),
        ).lastrowid


def _prepared(service, tmp_path: Path, monkeypatch, data: bytes, number: int = 3) -> tuple[int, dict]:
    """An episode whose phone copy (``data``) the Mac has prepared."""
    episode = _episode(service, tmp_path, number)

    def fake_prepare(source, folder, **_kwargs):
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (hashlib.sha256(str(source).encode()).hexdigest()[:32] + ".mp4")
        target.write_bytes(data)
        return {"path": target, "durationMs": 1440000}

    monkeypatch.setattr("pudge.iphone_media.prepare_video", fake_prepare)
    monkeypatch.setattr(
        service.media, "episode_subtitles", lambda _id: {"schema": 1, "tokenized": True, "cues": []}
    )
    service.media._prepare_worker(episode)
    state = service.media.episode(episode)
    assert state["state"] == "ready"
    return episode, state


def _pair(service) -> tuple[str, str]:
    invite = service.invite("https://127.0.0.1:1", "0" * 64)
    request = {
        "invitation": invite["invitation"],
        "deviceId": str(uuid.uuid4()),
        "attemptId": str(uuid.uuid4()),
        "name": "iPad",
    }
    return request["deviceId"], allowed(service, request)["token"]


@pytest.fixture
def server(service):
    from pudge.iphone_http import start_iphone_server

    value, thread, _pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        yield value
    finally:
        value.shutdown()
        value.wait_for_requests(timeout=5)
        value.server_close()
        thread.join(5)


def _connection(service, server) -> http.client.HTTPSConnection:
    context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
    context.check_hostname = False
    return http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=20)


def _get(connection, route: str, token: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    connection.request("GET", route, headers={"Authorization": "Bearer " + token, **(headers or {})})
    response = connection.getresponse()
    body = response.read()
    return response.status, {key.lower(): value for key, value in response.getheaders()}, body


def _post(connection, route: str, token: str, value: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(value or {})
    connection.request(
        "POST",
        route,
        body,
        {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    response = connection.getresponse()
    return response.status, json.loads(response.read() or b"{}")


def _downloads(service) -> int:
    with service.db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM iphone_downloads").fetchone()[0]


# ----------------------------------------------------------------- byte ranges


def test_parse_range_edges() -> None:
    from pudge.iphone_http import parse_range

    size = 1000
    assert parse_range(None, size) == (0, 999)
    assert parse_range("bytes=0-1", size) == (0, 1)  # AVPlayer's first probe
    assert parse_range("bytes=0-0", size) == (0, 0)
    assert parse_range("bytes=999-999", size) == (999, 999)  # the last byte
    assert parse_range("bytes=999-", size) == (999, 999)
    assert parse_range("bytes=0-", size) == (0, 999)
    assert parse_range("bytes=500-99999999", size) == (500, 999)  # clipped to the end
    assert parse_range("bytes=-1", size) == (999, 999)
    assert parse_range("bytes=-1000", size) == (0, 999)
    assert parse_range("bytes=-5000", size) == (0, 999)  # a suffix longer than the file
    for value in (
        "bytes=1000-",
        "bytes=1000-1001",
        "bytes=5-4",
        "bytes=-0",
        "bytes=-",
        "bytes=0-1,5-6",
        "items=0-1",
        "",
    ):
        with pytest.raises(ValueError):
            parse_range(value, size)


def test_the_prepared_episode_is_served_by_range_on_one_kept_alive_connection(
    service, server, tmp_path, monkeypatch
) -> None:
    data = os.urandom(3 * 1024 * 1024 + 7)
    size = len(data)
    _episode_id, state = _prepared(service, tmp_path, monkeypatch, data)
    video = state["video"]
    assert video["sizeBytes"] == size and video["sha256"] == hashlib.sha256(data).hexdigest()
    _device, token = _pair(service)
    route = f"/api/v2/resources/{video['resourceId']}/content?sha256={video['sha256']}&stream=play"
    connection = _connection(service, server)
    try:
        cases = [
            ("bytes=0-1", 0, 1),
            ("bytes=0-0", 0, 0),
            (f"bytes={size - 1}-{size - 1}", size - 1, size - 1),
            (f"bytes={size - 10}-", size - 10, size - 1),
            ("bytes=-5", size - 5, size - 1),
            (f"bytes=1048576-{size * 4}", 1048576, size - 1),
            ("bytes=0-", 0, size - 1),
        ]
        for header, start, end in cases:
            status, headers, body = _get(connection, route, token, {"Range": header})
            assert status == 206, header
            assert headers["content-range"] == f"bytes {start}-{end}/{size}", header
            assert headers["content-length"] == str(end - start + 1), header
            assert headers["accept-ranges"] == "bytes"
            assert body == data[start : end + 1], header
        # Unsatisfiable: 416 with the size, and the connection goes on.
        for header in (
            f"bytes={size}-",
            f"bytes={size + 5}-{size + 9}",
            "bytes=9-3",
            "bytes=0-1,4-5",
            "bytes=-0",
        ):
            status, headers, body = _get(connection, route, token, {"Range": header})
            assert status == 416 and headers["content-range"] == f"bytes */{size}" and body == b"", header
        # Chunks the way the phone's loader asks for them (a small first one, then larger),
        # read ahead out of order, give the file back exactly.
        chunks = [(0, 256 * 1024 - 1)]
        while chunks[-1][1] < size - 1:
            start = chunks[-1][1] + 1
            chunks.append((start, min(size - 1, start + 1024 * 1024 - 1)))
        parts: dict[int, bytes] = {}
        for start, end in chunks[1:] + chunks[:1]:
            status, _headers, body = _get(connection, route, token, {"Range": f"bytes={start}-{end}"})
            assert status == 206
            parts[start] = body
        assert b"".join(parts[start] for start, _end in chunks) == data
        # Without a Range header: the whole file (a download).
        status, headers, body = _get(connection, route, token)
        assert status == 200 and "content-range" not in headers and body == data
        # Another revision of the file: refused (the phone prepares it again).
        status, _headers, _body = _get(
            connection, route.replace(video["sha256"], "0" * 64), token, {"Range": "bytes=0-1"}
        )
        assert status == 409
    finally:
        connection.close()


def test_stream_ranges_renew_the_stream_and_are_no_download(service, server, tmp_path, monkeypatch) -> None:
    data = os.urandom(512 * 1024)
    episode, state = _prepared(service, tmp_path, monkeypatch, data)
    video = state["video"]
    _device, token = _pair(service)
    connection = _connection(service, server)
    try:
        status, opened = _post(connection, f"/api/v2/media/episodes/{episode}/stream", token)
        assert status == 200 and opened["state"] == "ready" and opened["video"] == video
        stream = opened["stream"]["id"]
        assert opened["stream"]["leaseSeconds"] == 90
        held = service.media._streams[stream]
        held["renewed"] -= 60  # a minute without renewal
        before = held["renewed"]
        base = f"/api/v2/resources/{video['resourceId']}/content?sha256={video['sha256']}"
        last = f"bytes={len(data) - 100}-{len(data) - 1}"
        status, _headers, body = _get(connection, base + f"&stream={stream}", token, {"Range": last})
        assert status == 206 and body == data[-100:]
        # The Mac counts a range after its bytes went out, so the phone may see them first.
        assert _eventually(
            lambda: held["renewed"] > before and held["requests"] == 1 and held["bytes"] == 100
        )
        # The last byte of a stream is no download; the same range of a download is one.
        assert _downloads(service) == 0
        status, _headers, _body = _get(connection, base, token, {"Range": last})
        assert status == 206 and _eventually(lambda: _downloads(service) == 1)
        lines = service.api.logger.lines
        # Stream ranges are not logged one by one (an episode is hundreds of them).
        assert sum("EVENT iphone.transfer start" in line for line in lines) == 1
        # Renewed and closed by the phone.
        status, answer = _post(connection, f"/api/v2/media/streams/{stream}/keepalive", token)
        assert status == 200 and answer["ok"] is True
        status, answer = _post(connection, f"/api/v2/media/streams/{stream}/close", token)
        assert status == 200 and stream not in service.media._streams
        # Ended: the phone is told so (and opens a new one).
        status, answer = _post(connection, f"/api/v2/media/streams/{stream}/keepalive", token)
        assert status == 422 and "ended" in answer["error"]
        assert any(
            line.startswith(
                f"EVENT iphone.video_stream end episode={episode} stream={stream[:8]} reason=closed"
            )
            and "requests=1 bytes=100" in line
            for line in lines
        )
    finally:
        connection.close()


# ----------------------------------------------------------------- keeping the copy


def _holds(service, path: str) -> int:
    return service.media._video_streams.get(path, 0)


def test_a_stream_holds_the_copy_until_the_player_closes(service, tmp_path, monkeypatch) -> None:
    episode, state = _prepared(service, tmp_path, monkeypatch, b"m" * 4096)
    media = service.media
    path = media._prepared_video(episode)["path"]
    first = media.handle("POST", f"/api/v2/media/episodes/{episode}/stream", {}, lambda: {}, device="ipad")
    second = media.handle("POST", f"/api/v2/media/episodes/{episode}/stream", {}, lambda: {}, device="iphone")
    assert first["stream"]["id"] != second["stream"]["id"]
    assert _holds(service, path) == 2  # one hold per stream
    # Over the size budget and not asked for in hours: kept while streamed.
    monkeypatch.setattr("pudge.iphone_media._VIDEO_BUDGET_BYTES", 1)
    monkeypatch.setattr("pudge.iphone_media._VIDEO_PIN_SECONDS", 0)
    media._prune_videos()
    assert Path(path).exists()
    # Only the device that opened a stream renews or closes it.
    with pytest.raises(Exception, match="ended"):
        media.handle(
            "POST",
            f"/api/v2/media/streams/{first['stream']['id']}/keepalive",
            {},
            lambda: {},
            device="iphone",
        )
    media.handle(
        "POST", f"/api/v2/media/streams/{first['stream']['id']}/close", {}, lambda: {}, device="iphone"
    )
    assert _holds(service, path) == 2
    media.handle(
        "POST", f"/api/v2/media/streams/{first['stream']['id']}/close", {}, lambda: {}, device="ipad"
    )
    assert _holds(service, path) == 1
    media._prune_videos()
    assert Path(path).exists()  # the iPhone still plays it
    media.close_stream(second["stream"]["id"], "iphone")
    media.close_stream(second["stream"]["id"], "iphone")  # closing twice changes nothing
    assert _holds(service, path) == 0
    media._prune_videos()
    assert not Path(path).exists()  # let go: removed within the budget as before
    assert state["video"]["sizeBytes"] == 4096


def test_a_phone_that_went_away_lets_the_copy_go_after_the_lease(service, tmp_path, monkeypatch) -> None:
    episode, _state = _prepared(service, tmp_path, monkeypatch, b"m" * 4096)
    media = service.media
    path = media._prepared_video(episode)["path"]
    stream = media.open_stream(episode, "ipad")["stream"]["id"]
    media._streams[stream]["renewed"] -= 60
    media.renew_stream(stream, "ipad")  # renewed within the lease
    media._streams[stream]["renewed"] -= 60
    assert media.stream_used(stream, "ipad", 2048)  # a byte range renews it too
    assert _holds(service, path) == 1
    media._streams[stream]["renewed"] -= 91  # no renewal for longer than the lease (Wi-Fi gone)
    monkeypatch.setattr("pudge.iphone_media._VIDEO_BUDGET_BYTES", 1)
    monkeypatch.setattr("pudge.iphone_media._VIDEO_PIN_SECONDS", 0)
    media._prune_videos()
    assert stream not in media._streams and _holds(service, path) == 0
    assert not Path(path).exists()
    assert any(
        "EVENT iphone.video_stream end" in line and "reason=expired" in line
        for line in service.api.logger.lines
    )
    with pytest.raises(Exception, match="ended"):
        media.renew_stream(stream, "ipad")
    assert not media.stream_used(stream, "ipad", 10)


def test_a_revoked_device_and_an_older_stream_of_the_device_let_go(service, tmp_path, monkeypatch) -> None:
    episode, _state = _prepared(service, tmp_path, monkeypatch, b"m" * 4096)
    other = _episode(service, tmp_path, 4)
    service.media._prepare_worker(other)
    media = service.media
    path = media._prepared_video(episode)["path"]
    device, _token = _pair(service)
    streams = [media.open_stream(episode, device)["stream"]["id"] for _ in range(3)]
    assert _holds(service, path) == 3
    # A fourth stream of the same device (players that never closed): the oldest goes.
    newest = media.open_stream(other, device)["stream"]["id"]
    assert streams[0] not in media._streams and _holds(service, path) == 2
    assert any("reason=replaced" in line for line in service.api.logger.lines)
    service.revoke(device)
    media._expire_streams()
    assert not media._streams and _holds(service, path) == 0
    assert newest not in media._streams
    assert any("reason=revoked" in line for line in service.api.logger.lines)


class _WatchedLock:
    """The media lock, with a hook run when pruning checks a copy (after it listed them)."""

    def __init__(self, inner, on_check) -> None:
        self.inner, self.on_check = inner, on_check

    def __enter__(self):
        frame = sys._getframe(1)
        if self.on_check and frame.f_code.co_name == "_prune_videos" and "total" in frame.f_locals:
            hook, self.on_check = self.on_check, None
            hook()
        self.inner.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.inner.release()

    def locked(self) -> bool:
        return self.inner.locked()


def test_pruning_checks_and_removes_a_copy_under_one_lock(service, monkeypatch) -> None:
    folder = service.root / "video"
    folder.mkdir(parents=True, exist_ok=True)
    old, wanted = folder / ("a" * 32 + ".mp4"), folder / ("b" * 32 + ".mp4")
    for path in (old, wanted):
        path.write_bytes(b"v" * 1000)
        os.utime(path, (1, 1))
    media = service.media
    monkeypatch.setattr("pudge.iphone_media._VIDEO_BUDGET_BYTES", 1)
    # A phone asks for one copy after pruning listed them, before it gets to that copy.
    media._video_lock = _WatchedLock(media._video_lock, lambda: media._touch_video(wanted))
    held: list[bool] = []
    original = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.suffix == ".mp4":
            held.append(media._video_lock.locked())
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    media._prune_videos()
    assert wanted.exists() and not old.exists()
    assert held == [True]  # removed while no transfer or stream can start on it


def test_streaming_an_episode_not_prepared_yet_prepares_it_first(service, tmp_path, monkeypatch) -> None:
    episode = _episode(service, tmp_path)
    started: list[int] = []
    monkeypatch.setattr(service.media, "_prepare_worker", started.append)
    answer = service.media.open_stream(episode, "ipad")
    deadline = time.monotonic() + 5
    while not started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started == [episode]
    assert answer["state"] == "preparing" and "stream" not in answer
    assert not service.media._streams
    with pytest.raises(Exception, match="ended"):
        service.media.renew_stream("not-a-stream", "ipad")
    assert service.media.close_stream("../../x", "ipad") == {"ok": True}


def test_the_mac_tells_phones_it_can_stream(service, server) -> None:
    connection = _connection(service, server)
    try:
        connection.request("GET", "/api/v2/capabilities")
        answer = json.loads(connection.getresponse().read())
    finally:
        connection.close()
    assert "video-stream" in answer["capabilities"] and "range" in answer["capabilities"]


# ----------------------------------------------------------------- streamable MP4


FAKE_PROBE = """#!{python}
import json, sys
print(json.dumps({{"streams": [{{"index": 0, "codec_type": "video", "codec_name": "{codec}", "pix_fmt": "{pix}"}},
                              {{"index": 1, "codec_type": "audio", "codec_name": "aac"}}],
                  "format": {{"duration": "60"}}}}))
"""

FAKE_FFMPEG = """#!{python}
import json, sys
open({log!r}, "w").write(json.dumps(sys.argv[1:]))
open(sys.argv[-1], "wb").write(b"x")
"""


@pytest.mark.parametrize(
    ("codec", "pix", "video"),
    [
        ("h264", "yuv420p", ["-c:v", "copy", "-tag:v", "avc1"]),
        ("hevc", "yuv420p10le", ["-c:v", "copy", "-tag:v", "hvc1"]),
        ("h264", "yuv420p10le", ["-tag:v", "avc1"]),  # 10-bit H.264: re-encoded
        ("vp9", "yuv420p", ["-tag:v", "avc1"]),
    ],
)
def test_every_prepared_copy_is_written_with_moov_first(tmp_path, monkeypatch, codec, pix, video) -> None:
    from pudge.iphone_media import prepare_video
    from pudge.subtitles import stt

    monkeypatch.setattr(stt, "_select_japanese_audio_stream", lambda _source, _ffprobe: (1, {}))
    log = tmp_path / "argv.json"
    probe = tmp_path / "ffprobe"
    probe.write_text(FAKE_PROBE.format(python=sys.executable, codec=codec, pix=pix))
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text(FAKE_FFMPEG.format(python=sys.executable, log=str(log)))
    for tool in (probe, ffmpeg):
        tool.chmod(0o755)
    source = tmp_path / "Show - 01.mkv"
    source.write_bytes(b"source")
    result = prepare_video(source, tmp_path / "video", ffmpeg=str(ffmpeg), ffprobe=str(probe))
    argv = json.loads(log.read_text())
    flags = argv.index("-movflags")
    assert argv[flags + 1] == "+faststart"
    assert flags < len(argv) - 1 and argv[-1].endswith(".part.mp4")
    joined = " ".join(argv)
    assert " ".join(video) in joined
    # The cache key is unchanged: copies prepared before stay valid (they had faststart too).
    assert (
        result["path"].name
        == hashlib.sha256(
            f"{source.resolve()}:{source.stat().st_size}:{source.stat().st_mtime_ns}:iphone-mp4-v1".encode()
        ).hexdigest()[:32]
        + ".mp4"
    )


def _top_level_boxes(path: Path) -> list[str]:
    names = []
    with path.open("rb") as handle:
        while True:
            head = handle.read(8)
            if len(head) < 8:
                return names
            size, kind = struct.unpack(">I4s", head)
            if size == 1:
                size = struct.unpack(">Q", handle.read(8))[0]
                handle.seek(size - 16, os.SEEK_CUR)
            elif size == 0:
                names.append(kind.decode("latin-1"))
                return names
            else:
                handle.seek(size - 8, os.SEEK_CUR)
            names.append(kind.decode("latin-1"))


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
@pytest.mark.parametrize("codec", ["libx264", "mpeg4"])
def test_a_real_prepared_copy_has_its_index_before_the_media(tmp_path, codec) -> None:
    from pudge.iphone_media import prepare_video

    source = tmp_path / f"Show - {codec}.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=2",
            "-c:v",
            codec,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-metadata:s:a:0",
            "language=jpn",
            str(source),
        ],
        check=True,
    )
    result = prepare_video(source, tmp_path / "video", ffmpeg="ffmpeg", ffprobe="ffprobe")
    boxes = _top_level_boxes(result["path"])
    assert "moov" in boxes and "mdat" in boxes
    assert boxes.index("moov") < boxes.index("mdat"), boxes


def _eventually(check, timeout: float = 30.0) -> bool:  # ends as soon as it holds
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True
