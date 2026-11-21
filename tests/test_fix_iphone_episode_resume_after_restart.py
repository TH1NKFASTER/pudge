"""An episode download to the iPhone/iPad resumes after the Mac app restarts (an update).

The phone keeps the bytes it has and asks for the rest with ``Range`` + ``If-Range`` (the
ETag). That only works when, after the restart, the same URL serves the same bytes with the
same ETag (no new preparation, no per-process validator), and when a changed edition answers
with the whole file instead of a wrong tail.
"""

from __future__ import annotations

import hashlib
import http.client
import ssl
import uuid
from pathlib import Path
from types import SimpleNamespace

from pudge.database import Database
from test_iphone_audio_repository import allowed


class _Log:
    def info(self, *_args) -> None:
        pass

    warning = info


def _service(tmp_path: Path):
    from pudge.iphone_service import IPhoneService

    value = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    value.api = SimpleNamespace(logger=_Log())
    return value


def _episode(service, tmp_path: Path) -> int:
    video = tmp_path / "[SubsPlease] Dorohedoro - 03 (1080p).mkv"
    video.write_bytes(b"x" * 10)
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,updated_at) VALUES(105228,'Dorohedoro','CURRENT',2,0)"
        )
        return conn.execute(
            "INSERT INTO episodes(media_id,title,episode,video_path,state,updated_at) VALUES(105228,'Ep',3,?,'ready',0)",
            (str(video),),
        ).lastrowid


def _connect(service, server) -> http.client.HTTPSConnection:
    context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
    context.check_hostname = False
    return http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=10)


def _stop(server, thread) -> None:
    server.shutdown()
    server.wait_for_requests(timeout=5)
    server.server_close()
    thread.join(5)


def test_a_cut_episode_download_resumes_after_the_mac_restarts(tmp_path, monkeypatch) -> None:
    from pudge.iphone_http import start_iphone_server

    content = bytes(range(256)) * 4096  # 1 MiB
    prepared: list[Path] = []

    def fake_prepare(source, folder, **_kwargs):
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "copy.mp4"
        target.write_bytes(content)
        prepared.append(target)
        return {"path": target, "durationMs": 1000}

    monkeypatch.setattr("pudge.iphone_media.prepare_video", fake_prepare)
    first = _service(tmp_path)
    monkeypatch.setattr(first.media, "episode_subtitles", lambda _id: {"schema": 1, "cues": []})
    episode = _episode(first, tmp_path)
    first.media._prepare_worker(episode)
    video = first.media.episode(episode)["video"]
    route = f"/api/v2/resources/{video['resourceId']}/content?sha256={video['sha256']}"

    server, thread, pin = start_iphone_server(first, host="127.0.0.1", port=0)
    try:
        invite = first.invite(f"https://127.0.0.1:{server.server_port}", pin)
        device = {
            "invitation": invite["invitation"],
            "deviceId": str(uuid.uuid4()),
            "attemptId": str(uuid.uuid4()),
            "name": "iPad",
        }
        token = allowed(first, device)["token"]
        connection = _connect(first, server)
        connection.request("GET", route, headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        assert response.status == 200
        etag = response.getheader("ETag")
        received = response.read(300_000)  # the Mac quits for an update here
        connection.close()
    finally:
        _stop(server, thread)

    # The Mac app starts again: a new process, the same data folder.
    second = _service(tmp_path)
    state = second.media.prepare_episode(episode)
    assert state["state"] == "ready"
    assert state["video"]["resourceId"] == video["resourceId"]
    assert state["video"]["sha256"] == video["sha256"]
    assert len(prepared) == 1  # not prepared again: the same bytes

    server, thread, pin_after = start_iphone_server(second, host="127.0.0.1", port=0)
    try:
        assert pin_after == pin  # the paired phone still trusts it
        connection = _connect(second, server)
        connection.request(
            "GET",
            route,
            headers={
                "Authorization": "Bearer " + token,
                "Range": f"bytes={len(received)}-",
                "If-Range": etag,
            },
        )
        response = connection.getresponse()
        assert response.status == 206
        assert response.getheader("ETag") == etag
        assert (
            response.getheader("Content-Range") == f"bytes {len(received)}-{len(content) - 1}/{len(content)}"
        )
        rest = response.read()
        assert hashlib.sha256(received + rest).hexdigest() == video["sha256"]

        # Resume data of another edition (its ETag differs): the whole file, never a wrong tail.
        connection.request(
            "GET",
            route,
            headers={"Authorization": "Bearer " + token, "Range": "bytes=10-", "If-Range": '"other"'},
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Range") is None
        assert response.read() == content

        # A date (no Last-Modified is sent, so it cannot match): the whole file as well.
        connection.request(
            "GET",
            route,
            headers={
                "Authorization": "Bearer " + token,
                "Range": "bytes=10-",
                "If-Range": "Sat, 24 Oct 2026 10:00:00 GMT",
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        assert len(response.read()) == len(content)

        # A plain range (AVPlayer streaming, no If-Range) is unchanged.
        connection.request("GET", route, headers={"Authorization": "Bearer " + token, "Range": "bytes=2-5"})
        response = connection.getresponse()
        assert response.status == 206
        assert response.read() == content[2:6]
        connection.close()
    finally:
        _stop(server, thread)
