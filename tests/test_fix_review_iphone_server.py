"""iPhone server and sync fixes from the review.

1. An unpaired device on the Wi-Fi could tie up the Mac's iPhone server: the 5 s timeout was
   per read, so a request sent one byte every few seconds (or a TLS handshake sent that way)
   kept its connection and thread forever, every connection got a thread with no limit
   (600 dripping connections -> 587 threads), and pairing buffered up to 2 MB before any
   authentication. Now the handshake + request line + headers and every body have a total
   deadline, connections over a total and a per-device cap are closed at once, and a pairing
   body is at most 16 KB. Long answers (streams, downloads) are never cut by these deadlines.
2. A newer reading place from the phone was dropped as "older": it was compared with the
   book's updated_at, which also changes when AniList refreshes, a score or a cover is saved.
   Going back from page 40 to 10 offline, then an AniList refresh: the phone got "kept: newer"
   and page 10 was lost. Light novels and manga now keep their own position time; a place taken
   from the phone keeps the time it was read there, and a phone clock ahead of the Mac's is
   corrected as for activity.
3. Choosing a conflict candidate ignored its finished state: picking the iPad's "finish" left
   the audiobook unfinished. Now it follows the same rule as an accepted checkpoint.
4. Two copies of one handoff request (a retry while the Mac was still pausing) left
   "revision_conflict" as the saved answer although the Mac had paused. Now the copies run one
   after the other and a saved answer is final.
5. Resuming on the Mac compared the Mac's time with the phone's clock: a phone running ahead
   hid the Mac's own newer listening. Now the Mac's receive time is used.
"""

from __future__ import annotations

import http.client
import json
import select
import socket
import sqlite3
import ssl
import threading
import time
import uuid
from contextlib import contextmanager

from pudge.database import Database
from pudge.iphone_service import IPhoneService, now_ms
from test_fix_iphone_multi_device import _second_device
from test_iphone_audio_repository import archived, checkpoint, service  # noqa: F401
from test_web_app import make_api


# ----------------------------------------------------------------------------- 1. server
class _Media:
    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        self.answers = 0

    def handle(self, command, route, query, body, device=""):
        if command == "POST":
            self.bodies.append(json.dumps(body()).encode())
            return {"ok": True}
        self.answers += 1
        if self.answers == 1:
            time.sleep(1.5)  # longer than the deadlines below: the answer must still arrive
        return b"x" * (32 << 20), "image/jpeg"  # far more than the socket buffers hold


class _Service:
    server_id = "server"

    def __init__(self) -> None:
        self.pairs: list[dict] = []
        self.media = _Media()

    def pair(self, payload):
        self.pairs.append(payload)
        return {"status": "pending"}

    def authenticate(self, token):
        if token != "token":
            raise PermissionError("bad token")
        return "device"


@contextmanager
def _server(fake=None, **limits):
    from pudge.iphone_http import IPhoneHTTPServer

    server = IPhoneHTTPServer(("127.0.0.1", 0), fake or _Service())
    for name, value in limits.items():
        setattr(server, name, value)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def _closed(sock, wait: float = 0.0) -> bool:
    readable, _, _ = select.select([sock], [], [], wait)
    if not readable:
        return False
    try:
        return sock.recv(65536) == b""
    except (ConnectionResetError, BrokenPipeError):
        return True


def _drip(port: int, data: bytes, *, limit: float = 12.0) -> float | None:
    """Send `data` one byte every 0.1 s (well within the 5 s idle timeout); when was it cut?

    Every dripped request is 160+ bytes (16+ s): without a total deadline it is never cut
    before ``limit``, so a late deadline thread on a busy Mac still passes the bound below.
    """
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    start = time.monotonic()
    try:
        for byte in data:
            if time.monotonic() - start > limit:
                return None
            try:
                sock.send(bytes([byte]))
            except OSError:
                return time.monotonic() - start
            if _closed(sock, 0.1):
                return time.monotonic() - start
        return None
    finally:
        sock.close()


def test_a_request_sent_byte_by_byte_is_cut_at_the_deadline():
    with _server(header_deadline=0.6) as server:
        cut = _drip(
            server.server_port, b"GET /api/v2/capabilities HTTP/1.1\r\nHost: x\r\n" + b"X: y\r\n" * 20
        )
        assert cut is not None and cut < 10.0
        assert server.wait_for_requests(timeout=30)


def test_a_body_sent_byte_by_byte_is_cut_at_its_deadline():
    with _server(header_deadline=5.0, body_deadline=0.6) as server:
        sock = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        head = b"POST /api/v2/media/ln/1/position HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer token\r\n"
        sock.sendall(head + b"Content-Length: 200\r\n\r\n")
        start, cut = time.monotonic(), None
        for byte in b"{" + b" " * 198 + b"}":
            if time.monotonic() - start > 12.0:  # 200 bytes: 20 s without the deadline
                break
            try:
                sock.send(bytes([byte]))
            except OSError:
                cut = time.monotonic() - start
                break
            if _closed(sock, 0.1):
                cut = time.monotonic() - start
                break
        sock.close()
        assert cut is not None and cut < 10.0


def test_a_tls_handshake_sent_byte_by_byte_is_cut_at_the_deadline(tmp_path, monkeypatch):
    from pudge.iphone_http import IPhoneHTTPServer, start_iphone_server

    monkeypatch.setattr(IPhoneHTTPServer, "header_deadline", 0.6)
    mobile = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    server, thread, _pin = start_iphone_server(mobile, host="127.0.0.1", port=0)
    try:
        # A TLS handshake record announcing 512 bytes, then its bytes one by one.
        cut = _drip(server.server_port, b"\x16\x03\x01\x02\x00" + b"\x01" * 200)
        assert cut is not None and cut < 10.0
        # A real phone still connects.
        context = ssl.create_default_context(cafile=str(mobile.root / "tls-cert.pem"))
        context.check_hostname = False
        connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=5)
        connection.request("GET", "/api/v2/capabilities")
        assert connection.getresponse().status == 200
        connection.close()
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def _capabilities(sock) -> bool:
    sock.sendall(b"GET /api/v2/capabilities HTTP/1.1\r\nHost: x\r\n\r\n")
    sock.settimeout(5)
    return sock.recv(64).startswith(b"HTTP/1.1 200")


def test_connections_over_the_caps_are_closed_at_once():
    # Per device (client address).
    with _server(max_connections=50, max_client_connections=2) as server:
        port = server.server_port
        held = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]
        extra = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert _closed(extra, 30.0)
        extra.close()
        assert not _closed(held[0], 0.2) and _capabilities(held[0])
        # A finished connection frees its place.
        held.pop().close()
        deadline = time.monotonic() + 30
        while len(server._active_requests) > 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        again = socket.create_connection(("127.0.0.1", port), timeout=5)
        assert not _closed(again, 0.3) and _capabilities(again)
        for sock in [*held, again]:
            sock.close()
    # In total.
    with _server(max_connections=3, max_client_connections=50) as server:
        held = [socket.create_connection(("127.0.0.1", server.server_port), timeout=5) for _ in range(3)]
        extra = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        assert _closed(extra, 30.0)
        assert len(server._active_requests) == 3
        for sock in [*held, extra]:
            sock.close()


def test_pairing_body_is_small_before_authentication():
    fake = _Service()
    with _server(fake) as server:
        body = json.dumps({"invitation": "a" * 20_000, "deviceId": str(uuid.uuid4())})
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", "/api/v2/pair", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 409
        response.read()
        connection.close()
        assert fake.pairs == []
        # A real pairing request (well under 2 KB) is read as before.
        request = {
            "invitation": "a" * 43,
            "deviceId": str(uuid.uuid4()),
            "attemptId": str(uuid.uuid4()),
            "name": "Телефон Максима" * 8,
        }
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("POST", "/api/v2/pair", json.dumps(request), {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        connection.close()
        assert fake.pairs == [request]


def test_long_answers_and_later_requests_are_not_cut_by_the_deadlines():
    fake = _Service()
    # 1 s deadlines: the first answer takes longer (1.5 s before its first byte). Between two
    # requests of a kept-alive connection the header deadline runs from the end of the Mac's
    # write while the phone still reads what the socket buffers hold: with 0.3 s a busy Mac
    # cut the next request there, which is no answer being cut.
    with _server(fake, header_deadline=1.0, body_deadline=1.0) as server:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"Authorization": "Bearer token"}
        for _ in range(2):  # the same kept-alive connection
            connection.request("GET", "/api/v2/media/manga/1/pages/0", headers=headers)
            response = connection.getresponse()
            assert response.status == 200
            received = 0
            while chunk := response.read(1 << 20):
                received += len(chunk)
                if received <= 8 << 20:
                    time.sleep(0.06)  # the phone reads slowly: the Mac still writes after the deadlines
            assert received == 32 << 20
        assert fake.media.answers == 2
        connection.request("POST", "/api/v2/media/ln/1/position", json.dumps({"chapter": 1}), headers)
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        connection.close()
        assert fake.media.bodies == [b'{"chapter": 1}']


# ------------------------------------------------------------------- 2. reading positions
def _reading(tmp_path):
    api = make_api(tmp_path)
    mobile = IPhoneService(api.manager.db, tmp_path / "iphone")
    mobile.api = api
    with api.manager.db.connect() as conn:
        book = conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,created_at,updated_at) VALUES('本',?,'epub',0,0)",
            (str(tmp_path / "book.epub"),),
        ).lastrowid
        manga = conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,created_at,updated_at) VALUES(?,'漫画',90,0,0,0)",
            (str(tmp_path / "manga.cbz"),),
        ).lastrowid
    return api, mobile, book, manga


def _ln_place(api, book):
    with api.manager.db.connect() as conn:
        row = conn.execute(
            "SELECT current_chapter,current_offset FROM ln_books WHERE id=?", (book,)
        ).fetchone()
    return int(row[0]), float(row[1])


def _manga_page(api, manga):
    with api.manager.db.connect() as conn:
        return int(conn.execute("SELECT position FROM manga_books WHERE id=?", (manga,)).fetchone()[0])


def test_newer_phone_place_survives_an_anilist_refresh_and_a_score(tmp_path):
    api, mobile, book, manga = _reading(tmp_path)
    api.light_novels.save_bookmark(book, 40, 0.5)  # read on the Mac
    api.manga_set_position(manga, 40)
    # Later, offline on the phone: back to chapter/page 10.
    read_back = time.time() + 1
    # Then the Mac refreshes AniList and a score: updated_at moves, the place does not.
    with api.manager.db.connect() as conn:
        conn.execute(
            "UPDATE ln_books SET anilist_status='CURRENT',updated_at=? WHERE id=?", (read_back + 1, book)
        )
        conn.execute("UPDATE manga_books SET user_score=8,updated_at=? WHERE id=?", (read_back + 1, manga))
    assert mobile.media.ln_position(book, {"chapter": 10, "offset": 0.0, "observedAt": read_back}) == {
        "ok": True
    }
    assert _ln_place(api, book) == (10, 0.0)
    page = mobile.media.manga_position(manga, {"page": 10, "read": False, "observedAt": read_back})
    assert page == {"ok": True}
    assert _manga_page(api, manga) == 10
    # A place really read earlier than the Mac's (sent late) is still kept out.
    api.light_novels.save_bookmark(book, 30, 0.0)
    api.manga_set_position(manga, 30)
    late = {"chapter": 20, "offset": 0.0, "observedAt": time.time() - 60}
    assert mobile.media.ln_position(book, late)["kept"] == "newer"
    late_page = {"page": 20, "read": False, "observedAt": time.time() - 60}
    assert mobile.media.manga_position(manga, late_page)["kept"] == "newer"
    assert _ln_place(api, book) == (30, 0.0) and _manga_page(api, manga) == 30


def test_phone_places_keep_the_time_they_were_read(tmp_path):
    api, mobile, book, manga = _reading(tmp_path)
    # A phone clock 5 minutes behind the Mac: forward to chapter 6, a minute later back to 2.
    # The first place used to get the Mac's receive time, which kept the second one out.
    now = time.time()
    mobile.media.ln_position(book, {"chapter": 6, "offset": 0.1, "observedAt": now - 300})
    assert mobile.media.ln_position(book, {"chapter": 2, "offset": 0.0, "observedAt": now - 240}) == {
        "ok": True
    }
    assert _ln_place(api, book) == (2, 0.0)
    mobile.media.manga_position(manga, {"page": 60, "read": False, "observedAt": now - 300})
    assert mobile.media.manga_position(manga, {"page": 20, "read": False, "observedAt": now - 240}) == {
        "ok": True
    }
    assert _manga_page(api, manga) == 20


def test_a_phone_clock_ahead_of_the_mac_is_corrected(tmp_path):
    api, mobile, book, manga = _reading(tmp_path)
    api.light_novels.save_bookmark(book, 40, 0.0)
    api.manga_set_position(manga, 40)
    # The phone's clock runs an hour ahead; it read chapter 10 a minute before the Mac's place
    # and sends it now with its own clock (sentAt).
    phone_now = time.time() + 3600
    late = {"chapter": 10, "offset": 0.0, "observedAt": phone_now - 60, "sentAt": phone_now}
    assert mobile.media.ln_position(book, late)["kept"] == "newer"
    late_page = {"page": 10, "read": False, "observedAt": phone_now - 60, "sentAt": phone_now}
    assert mobile.media.manga_position(manga, late_page)["kept"] == "newer"
    assert _ln_place(api, book) == (40, 0.0) and _manga_page(api, manga) == 40


def test_existing_books_get_a_position_time(tmp_path):
    api, mobile, book, manga = _reading(tmp_path)
    api.light_novels.save_bookmark(book, 4, 0.5)
    with api.manager.db.connect() as conn:
        saved = conn.execute("SELECT updated_at FROM ln_bookmarks WHERE book_id=?", (book,)).fetchone()[0]
    # A library from before this column: it is added on the next start (twice is harmless).
    path = api.config.library.database_path
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE ln_books DROP COLUMN position_updated_at")
        conn.execute("ALTER TABLE manga_books DROP COLUMN position_updated_at")
    for _ in range(2):
        again = make_api(tmp_path)
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute("SELECT position_updated_at FROM ln_books WHERE id=?", (book,)).fetchone()[0]
            == saved
        )
        assert (
            conn.execute("SELECT position_updated_at FROM manga_books WHERE id=?", (manga,)).fetchone()[0]
            == 0
        )
    assert again.light_novels.book(book)["current_chapter"] == 4


# ------------------------------------------------------------------- 3. conflict choice
def test_choosing_a_finished_candidate_marks_the_book_finished(service):  # noqa: F811
    phone, manifest = archived(service)
    pad = _second_device(service)
    service.record_checkpoint(phone, checkpoint(manifest, phone, position=10000))
    finish = checkpoint(manifest, pad, position=60000, reason="finish")
    assert service.record_checkpoint(pad, finish)["status"] == "conflict"
    progress = service.progress(manifest["assetId"])
    assert progress["finished"] is False
    service.resolve_progress(manifest["assetId"], progress["revision"], finish["checkpointId"])
    assert service.progress(manifest["assetId"])["finished"] is True
    with service.db.connect() as conn:
        row = conn.execute("SELECT position,finished FROM audiobooks").fetchone()
    assert (row["position"], row["finished"]) == (60.0, 1)
    # Choosing an "unfinish" candidate clears it again; another candidate keeps it.
    unfinish = checkpoint(manifest, phone, position=5000, reason="unfinish")
    service.record_checkpoint(phone, unfinish)
    progress = service.progress(manifest["assetId"])
    service.resolve_progress(manifest["assetId"], progress["revision"], unfinish["checkpointId"])
    assert service.progress(manifest["assetId"])["finished"] is False
    with service.db.connect() as conn:
        assert conn.execute("SELECT finished FROM audiobooks").fetchone()[0] == 0


# ------------------------------------------------------------------- 4. handoff copies
class _MacPlayer:
    def __init__(self, book, session):
        self._playback_lock = threading.Lock()
        self._playback_sessions = {book: session}
        self._ipc_paths = {book: "/tmp/mpv-socket"}
        self.paused = False

    def is_playing(self, book):
        return True

    def _ipc_command(self, path, command):
        time.sleep(0.3)  # mpv takes a moment to pause
        self.paused = True
        return {"error": "success"}

    def _ipc_get(self, path, name):
        return self.paused

    def _global_position(self, book, path):
        return 20.0


def test_two_copies_of_one_handoff_keep_the_right_answer(service):  # noqa: F811
    phone, manifest = archived(service)
    asset = manifest["assetId"]
    with service.db.connect() as conn:
        book = conn.execute("SELECT book_id FROM iphone_assets WHERE id=?", (asset,)).fetchone()["book_id"]
    service.audiobooks = _MacPlayer(book, "mac-session")
    service.begin_mac_session(asset, "mac-session")
    service.record_mac_position(asset, "mac-session", 10000)
    request = {
        "operationId": str(uuid.uuid4()),
        "assetId": asset,
        "expectedRevision": service.progress(asset)["revision"],
        "sourceSessionId": "mac-session",
        "targetReady": True,
    }
    answers: list[str] = []
    first = threading.Thread(target=lambda: answers.append(service.handoff(phone, dict(request))["status"]))
    first.start()
    time.sleep(0.1)
    second = threading.Thread(target=lambda: answers.append(service.handoff(phone, dict(request))["status"]))
    second.start()
    first.join(5)
    second.join(5)
    assert answers == ["source_paused", "source_paused"]
    later = service.handoff(phone, dict(request))
    assert later["status"] == "source_paused" and later["positionMs"] == 20000


# ------------------------------------------------------------------- 5. Mac resume clock
def test_mac_resume_compares_on_the_mac_clock(service):  # noqa: F811
    phone, manifest = archived(service)
    asset = manifest["assetId"]
    ahead = checkpoint(manifest, phone, position=1000)
    ahead["observedAt"] = now_ms() + 3_600_000  # the phone's clock runs an hour ahead
    service.record_checkpoint(phone, ahead)
    with service.db.connect() as conn:
        book = conn.execute("SELECT book_id FROM iphone_assets WHERE id=?", (asset,)).fetchone()[0]
        # The Mac played afterwards while this service was not attached.
        conn.execute("UPDATE audiobooks SET position=30,updated_at=? WHERE id=?", (time.time() + 5, book))
    assert service.mac_resume_position(book, 0.0) == 30.0
    assert service.progress(asset)["selected"]["positionMs"] == 30000
