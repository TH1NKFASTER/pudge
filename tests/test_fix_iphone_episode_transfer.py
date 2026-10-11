"""Episode downloads to the iPhone (Dorohedoro: 12 episodes + OVA failed with a network error).

- A phone that stops reading for a few seconds (Wi-Fi stall, busy phone) used to have its
  download cut by the Mac's 5-second socket timeout.
- Preparing a 9th episode deleted older prepared copies, also ones a phone was about to download.
- Neither preparing nor transferring left any trace in the Mac's log.
"""

from __future__ import annotations

import hashlib
import os
import socket
import ssl
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


def _copies(service, count: int, *, age: float, size: int = 1000, prefix: str = "a") -> list[Path]:
    folder = service.root / "video"
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        path = folder / f"{prefix}{index:031x}.mp4"
        path.write_bytes(b"v" * size)
        stamp = time.time() - age - index
        os.utime(path, (stamp, stamp))
        paths.append(path)
    return paths


def test_a_season_queue_keeps_every_recently_prepared_episode(service) -> None:
    # The phone's "Download all" prepares 13 episodes one after the other.
    copies = _copies(service, 13, age=60)
    service.media._prune_videos()
    assert all(path.exists() for path in copies)


def test_old_copies_go_past_the_budget_but_not_recent_or_downloading_ones(service, monkeypatch) -> None:
    monkeypatch.setattr("pudge.iphone_media._VIDEO_BUDGET_BYTES", 2500)
    old = _copies(service, 3, age=3 * 86400, prefix="a")
    recent = _copies(service, 3, age=600, prefix="b")
    (streamed,) = _copies(service, 1, age=4 * 86400, size=5000, prefix="c")
    preparing = service.root / "video" / "next.part.mp4"
    preparing.write_bytes(b"p" * 5000)
    os.utime(preparing, (time.time() - 60, time.time() - 60))
    leftover = service.root / "video" / "crashed.part.mp4"
    leftover.write_bytes(b"p" * 10)
    os.utime(leftover, (1, 1))
    service.media.video_streaming(streamed, True)  # an old copy on its way to a phone right now
    service.media._prune_videos()
    assert not any(path.exists() for path in old)
    assert all(path.exists() for path in recent)  # asked for in the last hours: kept past the budget
    assert streamed.exists()
    assert preparing.exists()  # a preparation still being written is never removed
    assert not leftover.exists()  # left by an interrupted run long ago
    assert any("EVENT iphone.video_prune" in line for line in service.api.logger.lines)
    service.media.video_streaming(streamed, False)


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


def test_prepared_episode_has_its_subtitles_ready_and_is_logged(service, tmp_path, monkeypatch) -> None:
    episode = _episode(service, tmp_path)

    def fake_prepare(source, folder, **_kwargs):
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "copy.mp4"
        target.write_bytes(b"m" * 4096)
        return {"path": target, "durationMs": 1000}

    states_when_subtitles_asked: list[str] = []

    def subtitles(episode_id):
        states_when_subtitles_asked.append(service.media.video_jobs[episode_id]["state"])
        return {"schema": 1, "tokenized": True, "cues": []}

    monkeypatch.setattr("pudge.iphone_media.prepare_video", fake_prepare)
    monkeypatch.setattr(service.media, "episode_subtitles", subtitles)
    service.media.video_jobs[episode] = {"state": "preparing", "progress": 0.0, "message": ""}
    service.media._prepare_worker(episode)

    assert service.media.episode(episode)["state"] == "ready"
    # The phone asks for the subtitles right after "ready": they were tokenized before it.
    assert states_when_subtitles_asked == ["preparing"]
    lines = service.api.logger.lines
    assert any(line.startswith("EVENT iphone.video_prepare start episode=%s" % episode) for line in lines)
    assert any(
        line.startswith("EVENT iphone.video_prepare done episode=%s size=4096" % episode) for line in lines
    )


def test_failed_preparation_is_logged(service, tmp_path, monkeypatch) -> None:
    episode = _episode(service, tmp_path)

    def broken(*_args, **_kwargs):
        raise RuntimeError("ffmpeg could not prepare the episode: moov atom not found")

    monkeypatch.setattr("pudge.iphone_media.prepare_video", broken)
    service.media._prepare_worker(episode)
    assert service.media.video_jobs[episode]["state"] == "failed"
    assert any(
        "EVENT iphone.video_prepare failed episode=%s" % episode in line and "moov atom" in line
        for line in service.api.logger.lines
    )


def _resource(service, size: int) -> tuple[str, str, Path]:
    folder = service.root / "video"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("%032x.mp4" % 7)
    block = os.urandom(1024 * 1024)
    with path.open("wb") as handle:
        for _ in range(size // len(block)):
            handle.write(block)
    sha = service.file_sha256(path)
    resource = str(uuid.uuid4())
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO iphone_resources(id,sha,size,path) VALUES(?,?,?,?)", (resource, sha, size, str(path))
        )
    return resource, sha, path


def test_a_phone_that_pauses_reading_still_gets_the_whole_episode(service, monkeypatch) -> None:
    from pudge.iphone_http import start_iphone_server

    size = 24 * 1024 * 1024
    resource, sha, path = _resource(service, size)
    server, thread, pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        invite = service.invite(f"https://127.0.0.1:{server.server_port}", pin)
        token = allowed(
            service,
            {
                "invitation": invite["invitation"],
                "deviceId": str(uuid.uuid4()),
                "attemptId": str(uuid.uuid4()),
                "name": "Phone",
            },
        )["token"]
        context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
        context.check_hostname = False
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)  # a small phone-side window
        raw.connect(("127.0.0.1", server.server_port))
        connection = context.wrap_socket(raw)
        connection.sendall(
            (
                f"GET /api/v2/resources/{resource}/content?sha256={sha} HTTP/1.1\r\n"
                f"Host: 127.0.0.1\r\nAuthorization: Bearer {token}\r\n\r\n"
            ).encode()
        )
        received = bytearray()
        while b"\r\n\r\n" not in received:
            received += connection.recv(65536)
        head, _, body = bytes(received).partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 200")
        digest = hashlib.sha256(body)
        count = len(body)
        # The phone stops reading for longer than the old 5-second socket timeout.
        time.sleep(6.5)
        # Meanwhile the Mac makes room: the copy being downloaded stays.
        monkeypatch.setattr("pudge.iphone_media._VIDEO_BUDGET_BYTES", 1)
        os.utime(path, (1, 1))
        service.media._prune_videos()
        assert path.exists()
        connection.settimeout(30)
        while count < size:
            chunk = connection.recv(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            count += len(chunk)
        connection.close()
        assert count == size and digest.hexdigest() == sha
        deadline = time.monotonic() + 5
        lines = service.api.logger.lines
        while not any("EVENT iphone.transfer done" in line for line in lines) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert any(
            line.startswith(f"EVENT iphone.transfer start resource={resource[:8]} bytes={size}")
            for line in lines
        )
        assert any(
            line.startswith(f"EVENT iphone.transfer done resource={resource[:8]} bytes={size}/{size}")
            for line in lines
        )
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def test_a_cut_transfer_is_logged_with_its_reason(service) -> None:
    from pudge.iphone_http import start_iphone_server

    size = 24 * 1024 * 1024
    resource, sha, _path = _resource(service, size)
    server, thread, pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        invite = service.invite(f"https://127.0.0.1:{server.server_port}", pin)
        request = {
            "invitation": invite["invitation"],
            "deviceId": str(uuid.uuid4()),
            "attemptId": str(uuid.uuid4()),
            "name": "Phone",
        }
        token = allowed(service, request)["token"]
        context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
        context.check_hostname = False
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
        raw.connect(("127.0.0.1", server.server_port))
        connection = context.wrap_socket(raw)
        connection.sendall(
            (
                f"GET /api/v2/resources/{resource}/content?sha256={sha} HTTP/1.1\r\n"
                f"Host: 127.0.0.1\r\nAuthorization: Bearer {token}\r\n\r\n"
            ).encode()
        )
        connection.recv(65536)
        service.revoke(request["deviceId"])  # access removed mid-download
        connection.close()
        deadline = time.monotonic() + 10
        lines = service.api.logger.lines
        while not any("EVENT iphone.transfer cut" in line for line in lines) and time.monotonic() < deadline:
            time.sleep(0.05)
        cut = [
            line for line in lines if line.startswith(f"EVENT iphone.transfer cut resource={resource[:8]}")
        ]
        assert cut and " reason=" in cut[0]
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)
