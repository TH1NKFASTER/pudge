from __future__ import annotations

import hashlib
import http.server
import json
import re
import socket
import ssl
import subprocess
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import iphone_network
from .iphone_service import IPhoneService, PairingRefused, encoded
from .i18n import err

# A connection waiting for a request (or the TLS handshake) is closed after this.
_IDLE_TIMEOUT = 5.0
# While a file goes to the phone: Wi-Fi stalls and a busy phone pause reading for longer
# than the idle timeout without the transfer being dead (the phone itself waits 120 s).
_TRANSFER_TIMEOUT = 120.0


def _logger(service: Any) -> Any:
    return getattr(getattr(service, "api", None), "logger", None)


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

    def json(self, code: int, value: dict) -> None:
        if code >= 400:
            # Unread request bodies must never be parsed as the next request.
            self.close_connection = True
        body = encoded(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if code >= 400:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit: int = 2 * 1024 * 1024) -> bytes:
        raw = self.headers.get("Content-Length", "")
        if not raw.isdigit() or not 0 < int(raw) <= limit:
            raise ValueError("Invalid request size")
        size = int(raw)
        self._body_read = True
        data = self.rfile.read(size)
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
        if size > 4 * 1024 * 1024:
            self.close_connection = True
            return
        if size and len(self.rfile.read(size)) != size:
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
                    ],
                },
            )
            return
        if self.command == "POST" and route == "/api/v2/pair":
            payload = json.loads(self.body())
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
        self.server.attach_device(self.request, device)
        if route.startswith("/api/v2/media/"):
            self.media_request(service, route, parse_qs(request.query))
            return
        if self.command == "GET" and route == "/api/v2/commands":
            self.json(200, service.commands(device))
            return
        if self.command == "GET" and route == "/api/v2/catalog":
            scan = service.refresh_catalog()
            self.json(200, {**service.catalog(), **scan})
            return
        resource_match = re.fullmatch("/api/v2/resources/([^/]+)/content", route)
        if self.command == "GET" and resource_match:
            sha = parse_qs(request.query).get("sha256", [""])[0]
            path = service.resource(resource_match[1], sha)
            size = path.stat().st_size
            try:
                start, end = parse_range(self.headers.get("Range"), size)
            except ValueError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206 if self.headers.get("Range") else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", '"' + sha + '"')
            self.send_header("Cache-Control", "private, no-store")
            if self.headers.get("Range"):
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            # From here on a failure must not append JSON to a half-sent body.
            self._streaming = True
            self.stream_resource(service, device, resource_match[1], path, start, end, size)
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
        if self.command == "POST":
            payload = json.loads(self.body())
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
        self, service: IPhoneService, device: str, resource: str, path: Any, start: int, end: int, size: int
    ) -> None:
        """Send bytes start..end of a resource; every transfer and cut is logged (EVENT iphone.transfer)."""
        logger = _logger(service)
        media = getattr(service, "media", None)
        pin = getattr(media, "video_streaming", None)
        length, sent, began = end - start + 1, 0, time.monotonic()
        reason = "done"
        if logger is not None:
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
            if logger is not None:
                logger.info(
                    "EVENT iphone.transfer %s resource=%s bytes=%s/%s seconds=%.1f%s",
                    "done" if reason == "done" else "cut",
                    resource[:8],
                    sent,
                    length,
                    time.monotonic() - began,
                    "" if reason == "done" else f" reason={reason}",
                )

    def media_request(self, service: IPhoneService, route: str, query: dict) -> None:
        def body() -> dict:
            payload = json.loads(self.body())
            if not isinstance(payload, dict):
                raise ValueError("Invalid body")
            return payload

        try:
            result = service.media.handle(self.command, route, query, body)
        except PermissionError:
            raise
        except KeyError as exc:
            if "Unknown media operation" in str(exc):
                self.json(404, {"error": "Unknown operation"})
            else:
                self.json(422, {"error": "This item is no longer on the Mac"})
            return
        except Exception as exc:  # noqa: BLE001 - readable reason for the phone
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
            self.json(422, {"error": message[:400]})
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
        self.json(200, result)

    def handle_request(self) -> None:
        self._streaming = False
        self._body_read = False
        try:
            try:
                self.run_request()
                self.drain_unread_body()
            except PermissionError:
                self.close_connection = True
                self.json(401, {"error": "Device access revoked or invalid"})
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

    def __init__(self, address, service):
        self.service = service
        self._request_condition = threading.Condition()
        self._active_requests: set[Any] = set()
        self._request_devices: dict[Any, str] = {}
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
        request.settimeout(_IDLE_TIMEOUT)
        with self._request_condition:
            self._active_requests.add(request)
        try:
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
            self._active_requests.discard(request)
            self._request_devices.pop(request, None)
            self._request_condition.notify_all()

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
