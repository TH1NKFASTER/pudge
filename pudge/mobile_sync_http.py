from __future__ import annotations

import http.server
import json
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse

from .mobile_sync import (
    MobileSyncAuthenticationError,
    MobileSyncError,
    MobileSyncService,
    MobileSyncValidationError,
)


_MAX_REQUEST_BYTES = 2 * 1024 * 1024


class MobileSyncHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        service: MobileSyncService,
        *,
        logger: Any = None,
    ) -> None:
        self.service = service
        self.logger = logger
        super().__init__(server_address, MobileSyncRequestHandler)


class MobileSyncRequestHandler(http.server.BaseHTTPRequestHandler):
    server: MobileSyncHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        logger = getattr(self.server, "logger", None)
        if logger is not None:
            try:
                logger.info("COMPANION http " + format, *args)
            except Exception:
                pass

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Pudge-Sync-Version", "1")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise MobileSyncValidationError("Invalid Content-Length") from exc
        if length < 0 or length > _MAX_REQUEST_BYTES:
            raise MobileSyncValidationError("Request body is too large")
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MobileSyncValidationError("Request body must be JSON") from exc
        if not isinstance(payload, dict):
            raise MobileSyncValidationError("Request body must be an object")
        return payload

    def _bearer_token(self) -> str:
        value = str(self.headers.get("Authorization") or "").strip()
        scheme, separator, token = value.partition(" ")
        if not separator or scheme.casefold() != "bearer":
            raise MobileSyncAuthenticationError("Bearer token required")
        return token.strip()

    def _device_id(self) -> str:
        return self.server.service.authenticate(self._bearer_token())

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, MobileSyncAuthenticationError):
            status = 401
        elif isinstance(exc, MobileSyncValidationError):
            status = 400
        elif isinstance(exc, MobileSyncError):
            status = 409
        else:
            status = 500
            logger = getattr(self.server, "logger", None)
            if logger is not None:
                try:
                    logger.exception("FAIL step=companion.http")
                except Exception:
                    pass
        self._send_json(
            status,
            {
                "ok": False,
                "error": str(exc) if status != 500 else "Internal server error",
            },
        )

    def do_GET(self) -> None:
        try:
            request = urlparse(self.path)
            if request.path == "/api/v1/health":
                self._send_json(
                    200,
                    {
                        "ok": True,
                        **self.server.service.protocol_info(),
                    },
                )
                return
            self._device_id()
            if request.path == "/api/v1/library":
                self._send_json(200, {"ok": True, **self.server.service.library_snapshot()})
                return
            if request.path == "/api/v1/sync/changes":
                query = parse_qs(request.query)
                cursor = int((query.get("cursor") or ["0"])[0])
                limit = int((query.get("limit") or ["200"])[0])
                self._send_json(
                    200,
                    {
                        "ok": True,
                        **self.server.service.changes(cursor=cursor, limit=limit),
                    },
                )
                return
            self._send_json(404, {"ok": False, "error": "Not found"})
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:
        try:
            request = urlparse(self.path)
            payload = self._read_json()
            if request.path == "/api/v1/pair/complete":
                result = self.server.service.complete_pairing(
                    str(payload.get("pairing_token") or ""),
                    name=str(payload.get("name") or "Companion"),
                    platform=str(payload.get("platform") or "unknown"),
                )
                self._send_json(200, {"ok": True, **result})
                return
            device_id = self._device_id()
            if request.path == "/api/v1/sync/events":
                events = payload.get("events")
                if not isinstance(events, list):
                    raise MobileSyncValidationError("events must be an array")
                result = self.server.service.push_events(device_id, events)
                self._send_json(200, {"ok": True, **result})
                return
            self._send_json(404, {"ok": False, "error": "Not found"})
        except Exception as exc:
            self._handle_error(exc)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()


def start_mobile_sync_server(
    service: MobileSyncService,
    *,
    host: str,
    port: int,
    logger: Any = None,
) -> tuple[MobileSyncHTTPServer, threading.Thread]:
    server = MobileSyncHTTPServer((str(host), int(port)), service, logger=logger)
    thread = threading.Thread(
        target=server.serve_forever,
        name="pudge-companion-api",
        daemon=True,
    )
    thread.start()
    return server, thread
