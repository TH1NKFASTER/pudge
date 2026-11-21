from __future__ import annotations

import hashlib
import http.server
import json
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import iphone_network
from .iphone_grammar import BODY_LIMIT as _GRAMMAR_BODY_LIMIT
from .iphone_service import IPhoneService, PairingRefused, encoded
from .i18n import err
from .memory_diagnostics import memory_step

# A connection waiting for a request (or the TLS handshake) is closed after this.
_IDLE_TIMEOUT = 5.0
# While a file goes to the phone: Wi-Fi stalls and a busy phone pause reading for longer
# than the idle timeout without the transfer being dead (the phone itself waits 120 s).
_TRANSFER_TIMEOUT = 120.0
# A pairing request is four short fields (well under 2 KB); it is read before any
# authentication, so a stranger on the Wi-Fi must not make the Mac buffer megabytes.
_PAIR_BODY_LIMIT = 16 * 1024


def _no_constant(name: str) -> Any:
    raise ValueError(f"Invalid number {name}")


def request_json(data: bytes) -> Any:
    """A request body as JSON. NaN and Infinity (Python's extension of JSON, never sent by the
    phone's encoder) are refused: an infinite "activeSeconds" counted a watch, an infinite place
    was stored and every later answer carrying it was no JSON a browser or phone could read."""
    return json.loads(data, parse_constant=_no_constant)


def media_refusal(exc: BaseException) -> bool:
    """A media request refused for good (HTTP 422: the phone shows the reason and does not send
    it again): the item is gone or the request is invalid (MediaError, ValueError, KeyError), or
    something is not set up on the Mac (errors marked ``setup_required``, e.g. "Jiten API token
    is not configured"). Everything else is temporary (HTTP 503, sent again later):
    MediaTemporaryError ("the Mac app is still starting"), a Jiten rate limit or outage, the
    release search, a locked database, a full disk, a RuntimeError while Pudge shuts down."""
    from .iphone_media import MediaError, MediaTemporaryError

    if isinstance(exc, MediaTemporaryError):
        return False
    if getattr(exc, "setup_required", False) is True:
        return True
    return isinstance(exc, (MediaError, ValueError, KeyError))


def access_refused(exc: BaseException) -> bool:
    """A PermissionError about the device's access (HTTP 401): raised by the phone API itself,
    without an errno (a bad or revoked token, an archive of another device). One with an errno
    is the file system refusing a file (EACCES/EPERM: no read permission, a volume macOS does
    not let Pudge read). Answered 401 too, the phone said "Access was revoked on the Mac.
    Connect the Mac again." and stopped a streamed episode for good, while pairing was fine."""
    return isinstance(exc, PermissionError) and exc.errno is None


def _logger(service: Any) -> Any:
    return getattr(getattr(service, "api", None), "logger", None)


def _no_delay(sock: Any) -> None:
    """Every write goes out at once (TCP_NODELAY). An answer is written in parts (headers,
    then the body), and Nagle held each later part until the phone acknowledged the first,
    which the phone delays (delayed ACK): 40-200 ms added to every answer, so AVPlayer's
    stream of byte ranges stalled on a fast Wi-Fi."""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (OSError, AttributeError):
        pass


def parse_range(value: str | None, size: int) -> tuple[int, int]:
    if value is None:
        return 0, size - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
    if not match or not any(match.groups()):
        raise ValueError("Invalid Range")
    left, right = match.groups()
    if not left:
        count = int(right)
        if count <= 0:
            raise ValueError("Invalid Range")
        if size <= 0:
            # An empty file has no last bytes: "bytes 0--1/0" is not a valid answer (RFC 9110).
            raise ValueError("Range not satisfiable")
        return max(0, size - count), size - 1
    start = int(left)
    end = min(size - 1, int(right)) if right else size - 1
    if start > end or start >= size:
        raise ValueError("Range not satisfiable")
    return start, end


class IPhoneHandler(http.server.BaseHTTPRequestHandler):
    server: "IPhoneHTTPServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:
        # Request paths, bearer credentials and pairing payloads are never logged.
        pass

    def json(self, code: int, value: dict, etag: str | None = None, *, body: bytes | None = None) -> None:
        """``body``: ``value`` already encoded (the caller hashed it for its ETag)."""
        if code >= 400:
            # Unread request bodies must never be parsed as the next request.
            self.close_connection = True
        if body is None:
            body = encoded(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if etag is not None:
            self.send_header("ETag", etag)
        if code >= 400:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def handle_one_request(self) -> None:
        # The idle timeout is per read: a client sending one byte every few seconds held the
        # connection (and its thread) without end. TLS handshake, request line and headers
        # now share one deadline; parse_request cancels it, so answers (streams) are not cut.
        self.server.set_deadline(self.request, self.server.header_deadline)
        super().handle_one_request()

    def parse_request(self) -> bool:
        parsed = super().parse_request()
        if parsed:
            self.server.set_deadline(self.request, None)
        return parsed

    def read_body(self, size: int, deadline: float | None = None) -> bytes:
        # A total deadline for the body too, for the same reason as for the headers.
        self.server.set_deadline(self.request, deadline or self.server.body_deadline)
        try:
            return self.rfile.read(size)
        finally:
            self.server.set_deadline(self.request, None)

    def body(self, limit: int = 2 * 1024 * 1024, deadline: float | None = None) -> bytes:
        raw = self.headers.get("Content-Length", "")
        if not raw.isdigit() or not 0 < int(raw) <= limit:
            raise ValueError("Invalid request size")
        size = int(raw)
        self._body_read = True
        data = self.read_body(size, deadline)
        if len(data) != size:
            raise ValueError("Incomplete request")
        return data

    def drain_unread_body(self) -> None:
        """Consume a body the route did not read, so a kept-alive connection stays in sync.

        Otherwise the phone's next request on the connection is parsed after the
        leftover bytes (e.g. ``{}GET /api/v2/media/reader``) and fails with 501.
        """
        if self._body_read or self.close_connection or self.headers is None:
            return
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return
        raw = self.headers.get("Content-Length", "0") or "0"
        if not raw.isdigit():
            self.close_connection = True
            return
        size = int(raw)
        # Before authentication (capabilities) only a small body is read, as for pairing.
        if size > (4 * 1024 * 1024 if self._authenticated else _PAIR_BODY_LIMIT):
            self.close_connection = True
            return
        if size and len(self.read_body(size)) != size:
            self.close_connection = True

    def run_request(self) -> None:
        service = self.server.service
        request = urlparse(self.path)
        route = request.path
        if self.command == "GET" and route == "/api/v2/capabilities":
            self.json(
                200,
                {
                    "protocolMajor": 2,
                    "serverId": service.server_id,
                    "capabilities": [
                        "audio",
                        "archive",
                        "range",
                        "progress",
                        "bookmarks",
                        "handoff",
                        "reader",
                        "manga",
                        "video",
                        "study",
                        "stats",
                        "activity",
                        # Checkpoint reason "unfinish" (Mark unfinished); "reset" keeps Finished.
                        "progress-unfinish",
                        # POST /api/v2/sync/state: progress and bookmarks of many books at once.
                        "state-batch",
                        # Merged periodic checkpoints ("continuousSince") count as listening.
                        "checkpoint-runs",
                        # GET /api/v2/catalog?lazy=1 + POST /api/v2/assets/prepare.
                        "lazy-catalog",
                        # POST /api/v2/inventory: what the device has downloaded (Mac library icons).
                        "inventory",
                        # Watch an episode from the Mac: POST media/episodes/<id>/stream holds the
                        # copy (media/streams/<id>/keepalive|close), byte ranges with ?stream=<id>.
                        "video-stream",
                        # Episode copies in smaller sizes: "quality" (original | smaller | saver) on
                        # media/episodes/<id> (?quality=), .../prepare and .../stream (body);
                        # "qualities" with each one's size on episodes and the episode list.
                        "video-quality",
                        # GET /api/v2/catalog answers with an ETag and 304 to If-None-Match.
                        "catalog-etag",
                        # POST media/ln/<id>/position {finished: true, finishOnly: true}: the
                        # volume is marked read without touching its place or read marker.
                        "ln-finish",
                    ],
                },
            )
            return
        if self.command == "POST" and route == "/api/v2/pair":
            # Unauthenticated: a small body within the header deadline.
            payload = request_json(self.body(_PAIR_BODY_LIMIT, self.server.header_deadline))
            if not isinstance(payload, dict):
                raise ValueError("Invalid body")
            try:
                result = service.pair(payload)
            except PairingRefused as exc:
                # Expired, used or declined: the phone shows this reason.
                self.json(422, {"error": str(exc)})
                return
            self.json(200, result)
            return
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise PermissionError("Authentication required")
        device = service.authenticate(auth[7:])
        self._authenticated = True
        self.server.attach_device(self.request, device)
        if (
            self.headers.get("X-Pudge-Discovery") == "any-name"
            or route == "/api/v2/sync/state"
            or (route == "/api/v2/catalog" and "lazy=1" in request.query)
        ) and callable(getattr(service, "note_new_discovery", None)):
            # Only newer phone builds send these: the legacy Bonjour name can go once all do.
            service.note_new_discovery(device)
        if route.startswith("/api/v2/media/"):
            self.media_request(service, route, parse_qs(request.query), device)
            return
        if self.command == "GET" and route == "/api/v2/commands":
            self.json(200, service.commands(device))
            return
        if self.command == "GET" and route == "/api/v2/catalog":
            lazy = parse_qs(request.query).get("lazy", [""])[0] == "1"
            scan = service.refresh_catalog(lazy=lazy)
            # ETag + If-None-Match → 304: a foreground or network-change refresh of an unchanged
            # library no longer sends (and the phone no longer saves) every manifest again.
            self.json_if_changed({**service.catalog(lazy=lazy), **scan})
            return
        resource_match = re.fullmatch("/api/v2/resources/([^/]+)/content", route)
        if self.command == "GET" and resource_match:
            query = parse_qs(request.query)
            sha = query.get("sha256", [""])[0]
            # Played from the Mac (AVPlayer's byte ranges), not downloaded: names the stream.
            stream = query.get("stream", [""])[0][:64]
            path = service.resource(resource_match[1], sha)
            size = path.stat().st_size
            etag = '"' + sha + '"'
            requested = self.headers.get("Range")
            if_range = self.headers.get("If-Range")
            if requested and if_range is not None and if_range.strip() != etag:
                # A resumed download whose bytes are another edition (RFC 9110 If-Range; there is
                # no Last-Modified, so a date never matches): the whole file, not the rest.
                requested = None
            try:
                start, end = parse_range(requested, size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206 if requested else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            # The content's SHA-256: the same after a Mac restart, so a cut download resumes.
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "private, no-store")
            if requested:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            # From here on a failure must not append JSON to a half-sent body.
            self._streaming = True
            self.stream_resource(service, device, resource_match[1], path, start, end, size, stream=stream)
            return
        asset_match = re.fullmatch("/api/v2/assets/([^/]+)/(manifest|progress|bookmarks)", route)
        if self.command == "GET" and asset_match:
            asset, method = asset_match.groups()
            value = (
                service.manifest(asset)
                if method == "manifest"
                else service.progress_view(asset)
                if method == "progress"
                else service.bookmarks(asset)
            )
            self.json(200, value)
            return
        chunk_match = re.fullmatch("/api/v2/archives/([^/]+)/resources/([^/]+)", route)
        if self.command == "PUT" and chunk_match:
            offset = int(parse_qs(request.query).get("offset", ["-1"])[0])
            self.json(
                200,
                service.archive_chunk(
                    device, chunk_match[1], chunk_match[2], offset, self.body(4 * 1024 * 1024)
                ),
            )
            return
        if self.command == "POST" and route == "/api/v2/inventory":
            from .iphone_inventory import BODY_LIMIT

            payload = request_json(self.body(BODY_LIMIT))
            if not isinstance(payload, dict):
                raise ValueError("Invalid body")
            self.json(200, service.record_inventory(device, payload))
            return
        if self.command == "POST":
            payload = request_json(self.body())
            if not isinstance(payload, dict):
                raise ValueError("Invalid body")
            if route == "/api/v2/archives":
                result = service.begin_archive(device, payload)
            elif route == "/api/v2/archives/commit":
                result = service.commit_archive(device, payload["transferId"])
            elif route == "/api/v2/archives/verify":
                result = service.verify_archive(device, payload["resourceId"], payload["editionId"])
            elif route == "/api/v2/checkpoints":
                result = service.record_checkpoint(device, payload)
            elif route == "/api/v2/progress/resolve":
                result = service.resolve_progress(
                    payload["assetId"], payload["expectedRevision"], payload["checkpointId"]
                )
            elif route == "/api/v2/bookmarks":
                result = service.record_bookmark(device, payload)
            elif route == "/api/v2/handoff":
                result = service.handoff(device, payload)
            elif route == "/api/v2/handoff/ack":
                result = service.acknowledge_handoff(device, payload)
            elif route == "/api/v2/sync/state":
                result = service.sync_state(payload.get("assets"), payload.get("since"))
            elif route == "/api/v2/assets/prepare":
                result = service.prepare_asset(payload.get("assetId"))
            elif route == "/api/v2/activity":
                result = service.media.record_activity(
                    device, service.device_name(device), payload.get("items"), sent_at=payload.get("sentAt")
                )
            else:
                self.json(404, {"error": "Unknown operation"})
                return
            self.json(200, result)
            return
        self.json(404, {"error": "Unknown operation"})

    def stream_resource(
        self,
        service: IPhoneService,
        device: str,
        resource: str,
        path: Any,
        start: int,
        end: int,
        size: int,
        stream: str = "",
    ) -> None:
        """Send bytes start..end of a resource; every transfer and cut is logged (EVENT iphone.transfer).

        ``stream``: a byte range of an episode played from the Mac (many short ranges, cancelled
        by every seek): it renews the stream, only real failures are logged, and reaching the
        last byte is no download."""
        logger = _logger(service)
        media = getattr(service, "media", None)
        pin = getattr(media, "video_streaming", None)
        length, sent, began = end - start + 1, 0, time.monotonic()
        reason = "done"
        if logger is not None and not stream:
            logger.info(
                "EVENT iphone.transfer start resource=%s bytes=%s offset=%s size=%s",
                resource[:8],
                length,
                start,
                size,
            )
        if callable(pin):
            pin(path, True)
        self.connection.settimeout(_TRANSFER_TIMEOUT)
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                while sent < length:
                    if not service.still_authorized(device) or self.server.closing:
                        # Revoked or access turned off mid-download: the phone sees a cut transfer.
                        reason = "revoked" if not service.still_authorized(device) else "server_closing"
                        self.close_connection = True
                        return
                    chunk = handle.read(min(256 * 1024, length - sent))
                    if not chunk:
                        reason = "truncated"
                        raise OSError("Resource truncated")
                    self.wfile.write(chunk)
                    sent += len(chunk)
            if not stream and end == size - 1 and callable(getattr(service, "record_download", None)):
                # The phone has the file up to its last byte (a resumed download sends the rest).
                try:
                    service.record_download(device, resource)
                except Exception:  # noqa: BLE001 - bookkeeping never breaks a finished transfer
                    pass
        except (TimeoutError, socket.timeout):
            reason = "phone_stopped_reading"
            raise
        except (BrokenPipeError, ConnectionResetError):
            reason = "phone_closed"
            raise
        except OSError as exc:
            reason = reason if reason != "done" else f"error:{type(exc).__name__}"
            raise
        finally:
            if callable(pin):
                pin(path, False)
            try:
                self.connection.settimeout(_IDLE_TIMEOUT)
            except OSError:
                pass
            used = getattr(media, "stream_used", None)
            if stream and callable(used):
                used(stream, device, sent, cut=reason != "done")
            # A stream's range cut by the phone (a seek, the player closed) is no failure.
            quiet = bool(stream) and reason in {"done", "phone_closed"}
            if logger is not None and not quiet:
                logger.info(
                    "EVENT iphone.transfer %s resource=%s bytes=%s/%s seconds=%.1f%s",
                    "done" if reason == "done" else "cut",
                    resource[:8],
                    sent,
                    length,
                    time.monotonic() - began,
                    ("" if reason == "done" else f" reason={reason}") + (" stream=1" if stream else ""),
                )

    def media_request(self, service: IPhoneService, route: str, query: dict, device: str = "") -> None:
        # A grammar question carries its chat: well under the general 2 MB.
        limit = _GRAMMAR_BODY_LIMIT if route.startswith("/api/v2/media/grammar/") else 2 * 1024 * 1024

        def body() -> dict:
            payload = request_json(self.body(limit))
            if not isinstance(payload, dict):
                raise ValueError("Invalid body")
            return payload

        try:
            result = service.media.handle(self.command, route, query, body, device=device)
        except KeyError as exc:
            if "Unknown media operation" in str(exc):
                self.json(404, {"error": "Unknown operation"})
            else:
                self.json(422, {"error": "This item is no longer on the Mac"})
            return
        except Exception as exc:  # noqa: BLE001 - readable reason for the phone
            if access_refused(exc):
                raise  # 401; a file the Mac may not read is a failure like any other (503)
            message = str(exc).strip() or type(exc).__name__
            logger = getattr(getattr(service, "api", None), "logger", None)
            if logger is not None:
                logger.warning(
                    "IPHONE media %s %s failed: %s: %s",
                    self.command,
                    route,
                    type(exc).__name__,
                    message[:400],
                )
            # 422 is final for the phone (a watched mark is not sent again): only a refusal
            # about the item itself. Anything else (a locked database, a full disk, a service
            # down) is 503 and the phone sends it again later; a watched mark answered 422 for
            # "database is locked" never reached AniList.
            self.json(422 if media_refusal(exc) else 503, {"error": message[:400]})
            return
        if isinstance(result, tuple):
            data, kind = result
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return
        if self.command == "GET" and isinstance(result, dict):
            # A list the phone asks for again and again (the anime list while the app is in
            # front): unchanged since its last answer, only "304" goes back.
            self.json_if_changed(result)
            return
        self.json(200, result)

    def json_if_changed(self, value: dict) -> None:
        """A GET answer with an ETag: unchanged since the phone's last one (If-None-Match),
        only "304" goes back, without a body the phone would decode and apply again. Older
        phones never send If-None-Match. Encoded once: hashed for the ETag and sent as it is
        (subtitle tracks and catalogs are MBs; hashing is cheap next to building them)."""
        body = encoded(value).encode()
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        if (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.json(200, value, etag=etag, body=body)

    def handle_request(self) -> None:
        self._streaming = False
        self._body_read = False
        self._authenticated = False
        try:
            try:
                with memory_step(
                    _logger(self.server.service),
                    "iphone.request",
                    route=f"{self.command}:{urlparse(self.path).path}"[:120],
                ):
                    self.run_request()
                self.drain_unread_body()
            except PermissionError as exc:
                self.close_connection = True
                if access_refused(exc):
                    self.json(401, {"error": "Device access revoked or invalid"})
                elif not self._streaming:
                    # The file system refused a file (access_refused): sent again later. Once a
                    # file's bytes are on their way nothing is added (an "HTTP/1.1 401" answer
                    # written into the body of a 200 broke the phone's copy).
                    self.json(503, {"error": "Storage or network unavailable"})
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                self.close_connection = True
                self.json(409, {"error": "Invalid request or resource revision; refresh and retry"})
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except OSError:
                self.close_connection = True
                if not self._streaming:
                    self.json(503, {"error": "Storage or network unavailable"})
            except Exception:  # noqa: BLE001 - never drop a request without an answer
                self.close_connection = True
                if not self._streaming:
                    self.json(500, {"error": "Internal error"})
        except OSError:
            # The connection was closed under the answer (revoked, access turned off).
            self.close_connection = True

    do_GET = handle_request
    do_POST = handle_request
    do_PUT = handle_request


class IPhoneHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # Every connection is a thread, and the TLS handshake and headers come before any
    # authentication: an unpaired device on the Wi-Fi opened hundreds of dripping
    # connections and threads. Connections over these caps are closed at once (a phone
    # uses a few URL sessions of 2-4 connections each, plus the video player).
    max_connections = 128
    max_client_connections = 32
    # Connections from addresses that have not authenticated lately share this part of
    # max_connections: devices on the Wi-Fi that never paired (4 hosts x 32 sockets) filled
    # every place and kept the paired phones out. A phone's address counts as known for
    # KNOWN_CLIENT_SECONDS after its last authenticated request.
    max_unknown_connections = 64
    known_client_seconds = 3600.0
    # Total time for TLS handshake + request line + headers, and for reading a body.
    header_deadline = 15.0
    body_deadline = _TRANSFER_TIMEOUT

    def __init__(self, address, service):
        self.service = service
        self._request_condition = threading.Condition()
        self._active_requests: set[Any] = set()
        self._request_devices: dict[Any, str] = {}
        self._request_clients: dict[Any, str] = {}
        self._client_connections: dict[str, int] = {}
        # Addresses that authenticated (address -> monotonic time) and the admitted
        # connections that count as unknown.
        self._known_clients: dict[str, float] = {}
        self._unknown_requests: set[Any] = set()
        self._deadlines: dict[Any, float] = {}
        self._deadline_thread: threading.Thread | None = None
        self._deadlines_stopped = False
        self.closing = False
        # Which connections are served (iphone_network.accepts): private LAN addresses only.
        self.accepts = iphone_network.accepts
        super().__init__(address, IPhoneHandler)

    def verify_request(self, request: Any, client_address: Any) -> bool:
        """Connections that reached this Mac through a VPN tunnel or a public address are closed."""
        try:
            local = request.getsockname()[0]
        except OSError:
            return False
        return bool(self.accepts(local, client_address[0] if client_address else ""))

    def attach_device(self, request: Any, device: str) -> None:
        with self._request_condition:
            if request in self._active_requests:
                self._request_devices[request] = device
                client = self._request_clients.get(request, "")
                if client:
                    self._known_clients[client] = time.monotonic()
                self._unknown_requests.discard(request)

    def _known(self, client: str) -> bool:
        seen = self._known_clients.get(client)
        if seen is None:
            return False
        if time.monotonic() - seen > self.known_client_seconds:
            self._known_clients.pop(client, None)
            return False
        return True

    def close_requests(self, device: str | None = None) -> int:
        """Cut the connections of one device (revoked), or all of them (access turned off)."""
        with self._request_condition:
            targets = [
                request
                for request in self._active_requests
                if device is None or self._request_devices.get(request) == device
            ]
        for request in targets:
            try:
                # The plain socket: a blocked read or write in the handler fails at once.
                socket.socket.shutdown(request, socket.SHUT_RDWR)
            except OSError:
                pass
        return len(targets)

    def process_request(self, request: Any, client_address: Any) -> None:
        # Register before starting the daemon thread: shutdown must also see
        # accepted requests whose handler has not begun running yet.
        client = str(client_address[0]) if client_address else ""
        with self._request_condition:
            known = self._known(client)
            admitted = (
                len(self._active_requests) < self.max_connections
                and self._client_connections.get(client, 0) < self.max_client_connections
                and (
                    known
                    or len(self._unknown_requests) < min(self.max_unknown_connections, self.max_connections)
                )
            )
            if admitted:
                self._active_requests.add(request)
                self._request_clients[request] = client
                self._client_connections[client] = self._client_connections.get(client, 0) + 1
                if not known:
                    self._unknown_requests.add(request)
        if not admitted:
            self.shutdown_request(request)
            return
        try:
            request.settimeout(_IDLE_TIMEOUT)
            _no_delay(request)
            super().process_request(request, client_address)
        except BaseException:
            self._request_finished(request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_finished(request)

    def _request_finished(self, request: Any) -> None:
        with self._request_condition:
            if request in self._active_requests:
                client = self._request_clients.pop(request, "")
                count = self._client_connections.get(client, 0) - 1
                if count > 0:
                    self._client_connections[client] = count
                else:
                    self._client_connections.pop(client, None)
            self._active_requests.discard(request)
            self._unknown_requests.discard(request)
            self._request_devices.pop(request, None)
            self._deadlines.pop(request, None)
            self._request_condition.notify_all()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # A connection cut by a deadline, a revocation or the phone itself is expected.
        if isinstance(sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)

    def set_deadline(self, request: Any, seconds: float | None) -> None:
        """Shut the connection down unless the deadline is cancelled (None) within `seconds`."""
        with self._request_condition:
            if seconds is None:
                self._deadlines.pop(request, None)
                return
            if request not in self._active_requests:
                return
            self._deadlines[request] = time.monotonic() + seconds
            if self._deadline_thread is None:
                # One timer thread for all connections, not one per request.
                self._deadline_thread = threading.Thread(
                    target=self._enforce_deadlines, name="pudge-iphone-deadlines", daemon=True
                )
                self._deadline_thread.start()
            self._request_condition.notify_all()

    def _enforce_deadlines(self) -> None:
        with self._request_condition:
            while not self._deadlines_stopped:
                now = time.monotonic()
                for request, deadline in list(self._deadlines.items()):
                    if deadline <= now:
                        del self._deadlines[request]
                        try:
                            # The plain socket, as in close_requests: the blocked read fails at once.
                            socket.socket.shutdown(request, socket.SHUT_RDWR)
                        except OSError:
                            pass
                upcoming = min(self._deadlines.values(), default=None)
                self._request_condition.wait(None if upcoming is None else max(0.01, upcoming - now))

    def server_close(self) -> None:
        with self._request_condition:
            self._deadlines_stopped = True
            self._request_condition.notify_all()
        super().server_close()

    def wait_for_requests(self, *, timeout: float = 5.0) -> bool:
        """Drain admitted requests after shutdown has stopped new admission."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._request_condition:
            while self._active_requests:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._request_condition.wait(remaining)
            return True


def start_iphone_server(service: IPhoneService, *, host: str = "0.0.0.0", port: int = 8766):
    """HTTPS for paired phones. Listens on the port of every interface but serves only
    connections to a private LAN address (or loopback): interfaces and their addresses change
    with the network, and a VPN tunnel's or public address is refused before TLS."""
    cert = service.root / "tls-cert.pem"
    key = service.root / "tls-key.pem"
    if not cert.exists() or not key.exists():
        # Never silently rotate an established identity: loss of only one half
        # requires explicit recovery, so a saved phone cannot trust another key.
        if cert.exists() != key.exists():
            raise ValueError(err("iphone.tlsIncomplete"))
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-sha256",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "3650",
                "-subj",
                "/CN=Pudge Local Audio",
            ],
            check=True,
            capture_output=True,
        )
        key.chmod(0o600)
        cert.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    server = IPhoneHTTPServer((host, port), service)
    try:
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    except BaseException:
        server.server_close()
        raise
    thread = threading.Thread(target=server.serve_forever, name="pudge-iphone-https", daemon=True)
    thread.start()
    service.http_server = server
    pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
    return server, thread, pin
