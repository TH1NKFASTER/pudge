"""Review round 6 of the phone link (Mac side).

1. A PermissionError of the file system (a file without read permission, a volume macOS does not
   let Pudge read: EACCES/EPERM) was answered 401 like a revoked device: the phone said "Access
   was revoked on the Mac. Connect the Mac again." and stopped a streamed episode for good. After
   a file's headers were sent, the 401 answer was even written into the body of the 200.
2. One manga volume's card (GET media/manga/<id>, the package worker) read the OCR state of every
   volume in the library to find its own.
3. A reading package's state worked out the volume's revisions twice per request.
4. mpv: the final playback save ran at end-file, when mpv has already closed the file (no
   time-pos, duration or subtitle tracks): it found no position and saved nothing, so the
   watching since the last periodic save, the place and a z/x delay set meanwhile were lost.
   It runs in the on_unload hook now, while the file is still open.
5. Revoking a paired device in Settings while phone access was turned off started the HTTPS
   server (and its Bonjour advertisement) again; Settings then showed access as on.
"""

from __future__ import annotations

import errno
import http.client
import json
import logging
import socket
import threading
from types import SimpleNamespace

import pytest

from pudge.database import Database
from pudge.iphone_http import IPhoneHTTPServer, access_refused
from pudge.iphone_media import MediaError
from pudge.iphone_reading import ReadingPackages
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario

RESOURCE = "6f1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SHA = "a" * 64


class _Media:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def handle(self, command, route, query, body, device=""):
        raise self.error


class _Service:
    server_id = "server"

    def __init__(self, *, media_error: BaseException | None = None, resource=None) -> None:
        self.media = _Media(media_error or RuntimeError("unused"))
        self._resource = resource

    def authenticate(self, token):
        if token != "token":
            raise PermissionError("Device access revoked or invalid")
        return "phone"

    def still_authorized(self, device):
        return True

    def resource(self, resource, sha):
        if isinstance(self._resource, BaseException):
            raise self._resource
        return self._resource


class _UnreadablePath:
    """A verified resource whose file the system no longer lets Pudge open."""

    def stat(self):
        return SimpleNamespace(st_size=4096)

    def open(self, mode="r"):
        raise PermissionError(errno.EACCES, "Permission denied", "/Volumes/Library/episode.mp4")


@pytest.fixture
def serve():
    servers = []

    def start(service) -> IPhoneHTTPServer:
        server = IPhoneHTTPServer(("127.0.0.1", 0), service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server

    yield start
    for server, thread in servers:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def _get(server, path: str, token: str = "token") -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request("GET", path, headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def test_only_a_permission_error_without_errno_is_about_the_device() -> None:
    assert access_refused(PermissionError("Device access revoked or invalid"))
    assert not access_refused(PermissionError(errno.EACCES, "Permission denied", "/x"))
    assert not access_refused(PermissionError(errno.EPERM, "Operation not permitted"))
    assert not access_refused(OSError(errno.EACCES, "Permission denied"))
    assert not access_refused(MediaError("No such illustration"))


def test_a_file_the_mac_may_not_read_is_no_revoked_device_for_media(serve) -> None:
    denied = PermissionError(errno.EPERM, "Operation not permitted", "/Volumes/Manga/volume.cbz")
    server = serve(_Service(media_error=denied))
    status, body = _get(server, "/api/v2/media/manga/5/pages/0")
    # Temporary (the phone asks again later) with the reason, never "Access was revoked".
    assert status == 503
    assert "Operation not permitted" in body["error"]
    # The device's own access is still refused as before.
    server = serve(_Service(media_error=PermissionError("Device access revoked or invalid")))
    assert _get(server, "/api/v2/media/manga/5/pages/0")[0] == 401
    assert _get(server, "/api/v2/media/ln", token="wrong")[0] == 401


def test_a_resource_the_mac_may_not_read_is_answered_503(serve) -> None:
    server = serve(_Service(resource=PermissionError(errno.EACCES, "Permission denied", "/x.m4a")))
    status, body = _get(server, f"/api/v2/resources/{RESOURCE}/content?sha256={SHA}")
    assert status == 503
    assert body == {"error": "Storage or network unavailable"}


def test_a_refused_file_after_its_headers_adds_no_second_answer(serve) -> None:
    server = serve(_Service(resource=_UnreadablePath()))
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=10) as client:
        client.sendall(
            (
                f"GET /api/v2/resources/{RESOURCE}/content?sha256={SHA} HTTP/1.1\r\n"
                "Host: mac\r\nAuthorization: Bearer token\r\n\r\n"
            ).encode()
        )
        received = b""
        while chunk := client.recv(65536):
            received += chunk
    head, _, body = received.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    assert b"Content-Length: 4096" in head
    # The phone sees a cut transfer (it resumes or starts over), not JSON inside the file.
    assert body == b""
    assert b"HTTP/1.1 401" not in received and b"revoked" not in received


def _manga_service(tmp_path):
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    with service.db.connect() as conn:
        for book in range(1, 6):
            conn.execute(
                "INSERT INTO manga_books(id,title,path,page_count,position,read_pages,reading_direction,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,0,?)",
                (
                    book,
                    f"Invented manga {book}",
                    str(tmp_path / f"m{book}.cbz"),
                    20 + book,
                    book,
                    book,
                    "rtl",
                    book,
                ),
            )
    asked: list[int] = []

    def ocr_cache_status(book_id):
        asked.append(int(book_id))
        return {"cached_pages": int(book_id) * 2}

    def shelf_metadata(book_id, title, path):
        return {"series_key": "invented", "series_title": "Invented manga", "volume": int(book_id)}

    service.api = SimpleNamespace(
        manga=SimpleNamespace(ocr_cache_status=ocr_cache_status, shelf_metadata=shelf_metadata)
    )
    return service, asked


def test_one_manga_volume_reads_only_its_own_ocr_state(tmp_path) -> None:
    service, asked = _manga_service(tmp_path)
    media = service.media
    whole = {item["id"]: item for item in media.manga_list()["items"]}
    assert sorted(asked) == [1, 2, 3, 4, 5]
    asked.clear()
    item = media.handle("GET", "/api/v2/media/manga/3", {}, dict)
    assert asked == [3]
    assert item == whole[3]
    assert item["ocrPages"] == 6 and item["pages"] == 23 and item["volume"] == 3
    with pytest.raises(MediaError):
        media.manga_book(99)
    with pytest.raises(MediaError):
        media.handle("GET", "/api/v2/media/manga/x", {}, dict)


def test_a_package_state_works_out_the_revisions_once() -> None:
    reading = object.__new__(ReadingPackages)
    reading.jobs = {"manga:3": {"state": "preparing", "progress": 0.25, "message": "", "revision": "r1"}}
    reading._prepared = lambda key, revision: None
    calls: list[tuple[str, int]] = []

    def revisions(kind, item):
        calls.append((kind, item))
        return {"revision": "r1", "pagesRevision": "p1", "ocrRevision": "", "ocrComplete": False}

    reading.revisions = revisions
    value = reading.state("manga", 3)
    assert calls == [("manga", 3)]
    assert value == {
        "revision": "r1",
        "pagesRevision": "p1",
        "ocrRevision": "",
        "ocrComplete": False,
        "kind": "manga",
        "id": 3,
        "state": "preparing",
        "progress": 0.25,
        "message": "",
    }


@needs_lua
def test_the_last_watching_is_saved_while_mpv_still_has_the_file_open(tmp_path) -> None:
    run_mpv_scenario(
        tmp_path,
        """
        sim.set('duration', 1440)
        sim.set('time-pos', 100)
        sim.set('current-tracks/sub/external-filename', '/subs/Show - 05.srt')
        sim.set('sub-delay', 0)
        sim.fire('file-loaded')
        sim.play(25)                         -- within the save interval: no periodic save yet
        sim.set('sub-delay', 0.75)           -- z/x just before closing
        assert(#sim.commands('%-%-playback%-save') == 0, 'no periodic save yet')
        sim.hook('on_unload')                -- mpv closes the file: it is still open here
        -- end-file and shutdown: mpv has closed the file (no time-pos, duration or tracks)
        sim.set('time-pos', nil)
        sim.set('duration', nil)
        sim.set('current-tracks/sub/external-filename', nil)
        sim.fire('end-file', {reason = 'quit'})
        sim.fire('shutdown')
        local saves = sim.commands('%-%-playback%-save')
        assert(#saves == 1, 'one final save: ' .. #saves)
        assert(saves[1]:find('--playback-position 125.000 --playback-duration 1440.000', 1, true), saves[1])
        assert(saves[1]:find('--playback-sub-delay 0.750 --playback-subtitle /subs/Show - 05.srt', 1, true), saves[1])
        local active = tonumber(saves[1]:match('%-%-playback%-active%-seconds ([%d%.]+)'))
        assert(active > 24 and active < 26, 'active seconds ' .. tostring(active))
        """,
        env={
            "PUDGE_PLAYBACK_ENABLED": "1",
            "PUDGE_PLAYBACK_VIDEO": "/media/Show - 05.mkv",
            "PUDGE_PLAYBACK_INTERVAL": "30",
            "PUDGE_SUBTITLE_PATH": "/subs/Show - 05.srt",
        },
        scripts=[ANILIST],
    )


def test_revoking_a_device_with_access_off_keeps_the_server_off(tmp_path, monkeypatch) -> None:
    import pudge.iphone_http as iphone_http
    from pudge.iphone_service import IPhoneService
    from pudge.web_app import WebAppApi

    started: list[object] = []

    def start(service, **_options):
        started.append(service)
        return SimpleNamespace(server_port=8766), SimpleNamespace(), "pin"

    monkeypatch.setattr(iphone_http, "start_iphone_server", start)
    db = Database(tmp_path / "db.sqlite")
    service = IPhoneService(db, tmp_path / "mobile")
    device = "0b7c9e3a-1d2f-4c5b-8a6e-7f9d0c1b2a3e"
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO iphone_devices(id,name,token_hash,revoked,paired_at) VALUES(?,?,?,0,1)",
            (device, "Invented iPhone", "0" * 64),
        )
    api = WebAppApi.__new__(WebAppApi)
    api.manager = SimpleNamespace(db=db)
    api.logger = logging.getLogger("test")
    api.audiobooks = SimpleNamespace()
    # Access turned off in Settings: the server is gone, the protocol service stays attached.
    api._iphone_service, api._iphone_server, api._iphone_invitation = service, None, None
    db.set_state("iphone_enabled", "0")
    status = api.iphone_revoke_device(device)
    assert started == [] and api._iphone_server is None
    assert status["enabled"] is False
    assert [row["revoked"] for row in service.devices()] == [1]


def test_an_archived_part_is_hashed_once_and_never_under_the_service_lock(tmp_path, monkeypatch) -> None:
    import hashlib
    import os

    import pudge.iphone_service as iphone_service
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    device = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
    data = b"invented audio bytes " * 200
    resource = "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a"
    manifest = {
        "protocolMajor": 2,
        "assetId": "3e4f5a6b-7c8d-4e9f-8a1b-2c3d4e5f6a7b",
        "editionId": "4f5a6b7c-8d9e-4f0a-9b2c-3d4e5f6a7b8c",
        "timelineRevision": "timeline-1",
        "title": "Invented book",
        "durationMs": 60000,
        "parts": [
            {
                "partId": "5a6b7c8d-9e0f-4a1b-8c3d-4e5f6a7b8c9d",
                "resourceId": resource,
                "sizeBytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "fileName": "book.mp3",
                "offsetMs": 0,
                "durationMs": 60000,
            }
        ],
        "chapters": [],
    }
    transfer = service.begin_archive(device, manifest)["transferId"]
    service.archive_chunk(device, transfer, resource, 0, data)
    hashed: list[object] = []
    lock_free: list[bool] = []
    real = iphone_service.digest

    def counting(path):
        hashed.append(path)

        # Another thread (the Mac player saving its place) can take the service lock meanwhile.
        def take() -> None:
            taken = service.lock.acquire(timeout=0.5)
            lock_free.append(taken)
            if taken:
                service.lock.release()

        probe = threading.Thread(target=take)
        probe.start()
        probe.join()
        return real(path)

    monkeypatch.setattr(iphone_service, "digest", counting)
    service.commit_archive(device, transfer)
    assert len(hashed) == 1  # the staged bytes
    receipt = service.verify_archive(device, resource, manifest["editionId"])
    assert receipt["retained"] is True and receipt["sha256"] == manifest["parts"][0]["sha256"]
    assert len(hashed) == 1  # the moved part is known: not hashed again
    # A part whose stored time changed is hashed again, without the service lock held.
    final = next((tmp_path / "mobile" / "objects").glob("*.mp3"))
    os.utime(final, ns=(1_000_000_000, 1_000_000_000))
    service._verified_resources.clear()
    assert service.verify_archive(device, resource, manifest["editionId"])["retained"] is True
    assert len(hashed) == 2 and all(lock_free)
