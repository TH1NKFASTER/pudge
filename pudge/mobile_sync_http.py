from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .companion_streaming import CompanionStreamingService
from .mobile_sync import (
    MobileSyncAuthenticationError,
    MobileSyncError,
    MobileSyncService,
    MobileSyncValidationError,
)

_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_COMPANION_WEB_ROOT = Path(__file__).resolve().parent / "web" / "companion"
_COMPANION_ASSETS = {
    "/companion/": ("index.html", "text/html; charset=utf-8"),
    "/companion/index.html": ("index.html", "text/html; charset=utf-8"),
    "/companion/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/companion/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/companion/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/companion/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/companion/icon.svg": ("icon.svg", "image/svg+xml"),
}


class MobileSyncHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        service: MobileSyncService,
        *,
        streaming: CompanionStreamingService | None = None,
        study_parser: Callable[[str], dict[str, Any]] | None = None,
        logger: Any = None,
    ) -> None:
        self.service = service
        self.streaming = streaming
        self.study_parser = study_parser
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

    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Pudge-Sync-Version", "1")
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self._common_headers()
        self.end_headers()

    def _send_file_path(self, path: Path, content_type: str) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "private, no-store")
        self._common_headers()
        self.end_headers()
        try:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _media_request(self, request: Any) -> bool:
        prefix = "/api/v1/media/"
        if not request.path.startswith(prefix):
            return False
        streaming = self.server.streaming
        if streaming is None:
            raise MobileSyncError("Anime streaming is unavailable")
        suffix = request.path[len(prefix):]
        parts = [unquote(part) for part in suffix.split("/") if part]
        if len(parts) != 2:
            raise MobileSyncValidationError("Invalid stream media URL")
        path, content_type = streaming.media_path(parts[0], parts[1])
        self._send_file_path(path, content_type)
        return True

    def _send_companion_asset(self, request_path: str) -> bool:
        if request_path == "/companion":
            self._redirect("/companion/")
            return True
        asset = _COMPANION_ASSETS.get(request_path)
        if asset is None:
            if request_path.startswith("/companion/"):
                self._send_json(404, {"ok": False, "error": "Not found"})
                return True
            return False
        filename, content_type = asset
        path = (_COMPANION_WEB_ROOT / filename).resolve()
        root = _COMPANION_WEB_ROOT.resolve()
        if root not in path.parents or not path.is_file():
            self._send_json(404, {"ok": False, "error": "Companion asset missing"})
            return True
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Cache-Control",
            "no-cache"
            if filename in {"index.html", "app.js", "styles.css", "manifest.webmanifest", "sw.js"}
            else "public, max-age=3600",
        )
        if filename == "sw.js":
            self.send_header("Service-Worker-Allowed", "/companion/")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self' data: blob: https:; "
            "style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)
        return True

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
        elif isinstance(exc, (MobileSyncValidationError, ValueError, KeyError)):
            status = 400
        elif isinstance(exc, MobileSyncError):
            status = 409
        else:
            status = 500
        self._send_json(status, {"ok": False, "error": str(exc) if status != 500 else "Internal server error"})

    def _content_request(self, request: Any) -> bool:
        prefix = "/api/v1/content/"
        if not request.path.startswith(prefix):
            return False
        self._device_id()
        suffix = request.path[len(prefix):]
        parts = [unquote(part) for part in suffix.split("/") if part]
        if not parts:
            raise MobileSyncValidationError("Missing entity id")
        entity_id = parts[0]
        query = parse_qs(request.query)
        index = int((query.get("index") or ["-1"])[0])
        if len(parts) == 1:
            payload = self.server.service.companion_content(entity_id, index=None if index < 0 else index)
            self._send_json(200, {"ok": True, **payload})
            return True
        if len(parts) == 2 and parts[1] == "cover":
            body, content_type, redirect_url = self.server.service.companion_cover(entity_id)
            if redirect_url:
                self._redirect(redirect_url)
            else:
                self._send_bytes(200, body, content_type)
            return True
        if len(parts) == 2 and parts[1] == "page":
            body, content_type = self.server.service.companion_manga_page(entity_id, page_index=max(0, index))
            self._send_bytes(200, body, content_type)
            return True
        if len(parts) == 2 and parts[1] == "stream":
            streaming = self.server.streaming
            if streaming is None:
                raise MobileSyncError("Anime streaming is unavailable")
            self._send_json(200, {"ok": True, **streaming.prepare(entity_id)})
            return True
        raise MobileSyncValidationError("Unsupported companion content route")

    def do_GET(self) -> None:
        try:
            request = urlparse(self.path)
            if self._send_companion_asset(request.path):
                return
            if self._media_request(request):
                return
            if request.path == "/api/v1/health":
                self._send_json(200, {"ok": True, **self.server.service.protocol_info()})
                return
            if self._content_request(request):
                return
            self._device_id()
            if request.path == "/api/v1/library":
                self._send_json(200, {"ok": True, **self.server.service.library_snapshot()})
                return
            if request.path == "/api/v1/sync/changes":
                query = parse_qs(request.query)
                cursor = int((query.get("cursor") or ["0"])[0])
                limit = int((query.get("limit") or ["200"])[0])
                self._send_json(200, {"ok": True, **self.server.service.changes(cursor=cursor, limit=limit)})
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
            if request.path == "/api/v1/study/parse":
                parser = self.server.study_parser
                if parser is None:
                    raise MobileSyncValidationError("Study parser is unavailable")
                text = str(payload.get("text") or "").replace("\r", "\n").strip()[:1000]
                if not text:
                    raise MobileSyncValidationError("text is required")
                result = parser(text)
                if not isinstance(result, dict):
                    raise MobileSyncValidationError("Study parser returned invalid payload")
                self._send_json(200, {"ok": True, "study": result})
                return
            if request.path == "/api/v1/sync/events":
                events = payload.get("events")
                if not isinstance(events, list):
                    raise MobileSyncValidationError("events must be an array")
                self._send_json(200, {"ok": True, **self.server.service.push_events(device_id, events)})
                return
            self._send_json(404, {"ok": False, "error": "Not found"})
        except Exception as exc:
            self._handle_error(exc)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self._common_headers()
        self.end_headers()


def start_mobile_sync_server(
    service: MobileSyncService,
    *,
    host: str,
    port: int,
    streaming: CompanionStreamingService | None = None,
    study_parser: Callable[[str], dict[str, Any]] | None = None,
    logger: Any = None,
) -> tuple[MobileSyncHTTPServer, threading.Thread]:
    server = MobileSyncHTTPServer(
        (str(host), int(port)),
        service,
        streaming=streaming,
        study_parser=study_parser,
        logger=logger,
    )
    thread = threading.Thread(target=server.serve_forever, name="pudge-companion-api", daemon=True)
    thread.start()
    return server, thread