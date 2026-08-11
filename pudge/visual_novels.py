from __future__ import annotations

import hashlib
import platform
import re
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image

from .ocr import _vision_recognize


class VisualNovelError(RuntimeError):
    pass


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


class VisualNovelService:
    """Explicit, bounded macOS window capture for the optional VN reader."""

    def __init__(self, *, logger: Any = None) -> None:
        self.logger = logger
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._window_id = 0
        self._window_title = ""
        self._state: dict[str, Any] = {"running": False, "status": "idle", "detail": ""}
        self._transcript: deque[dict[str, Any]] = deque(maxlen=200)

    def windows(self) -> list[dict[str, Any]]:
        if platform.system() != "Darwin":
            return []
        try:
            import Quartz  # type: ignore
        except ImportError as exc:
            raise VisualNovelError("Quartz is unavailable") from exc
        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        rows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []
        result: list[dict[str, Any]] = []
        for row in rows:
            window_id = int(row.get(Quartz.kCGWindowNumber, 0) or 0)
            owner = str(row.get(Quartz.kCGWindowOwnerName, "") or "").strip()
            title = str(row.get(Quartz.kCGWindowName, "") or "").strip()
            layer = int(row.get(Quartz.kCGWindowLayer, 0) or 0)
            bounds = row.get(Quartz.kCGWindowBounds) or {}
            if not window_id or layer != 0 or not owner or float(bounds.get("Width", 0) or 0) < 240:
                continue
            result.append({"id": window_id, "owner": owner, "title": title, "label": f"{owner} — {title}" if title else owner})
        return result

    def state(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["transcript"] = list(self._transcript)
            state["window_id"] = self._window_id
            state["window_title"] = self._window_title
            state["running"] = bool(self._thread and self._thread.is_alive())
        return state

    def start(self, window_id: int, title: str = "") -> dict[str, Any]:
        if platform.system() != "Darwin":
            raise VisualNovelError("The Visual Novel reader requires macOS")
        self.stop()
        self._window_id = int(window_id)
        self._window_title = str(title or "Visual Novel")
        self._stop.clear()
        with self._lock:
            self._state = {"running": True, "status": "starting", "detail": ""}
            self._transcript.clear()
        self._thread = threading.Thread(target=self._capture_loop, name="visual-novel-reader", daemon=True)
        self._thread.start()
        return self.state()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._lock:
            self._thread = None
            self._state.update({"running": False, "status": "idle"})
        return self.state()

    def _capture_loop(self) -> None:
        last_hash = ""
        last_change = time.monotonic()
        candidate = ""
        candidate_count = 0
        try:
            with tempfile.TemporaryDirectory(prefix="pudge-vn-") as temp_dir:
                frame_path = Path(temp_dir) / "frame.png"
                while not self._stop.is_set():
                    completed = subprocess.run(
                        ["/usr/sbin/screencapture", "-x", "-l", str(self._window_id), str(frame_path)],
                        check=False, capture_output=True, text=True, timeout=8,
                    )
                    if completed.returncode != 0 or not frame_path.is_file():
                        detail = (completed.stderr or completed.stdout or "Screen capture failed").strip()
                        raise VisualNovelError(f"Screen Recording permission or the selected window is unavailable: {detail}")
                    fingerprint = hashlib.blake2b(frame_path.read_bytes(), digest_size=16).hexdigest()
                    if fingerprint == last_hash:
                        interval = 3.0 if time.monotonic() - last_change >= 30 else 0.5
                        self._stop.wait(interval)
                        continue
                    last_hash = fingerprint
                    last_change = time.monotonic()
                    with Image.open(frame_path) as image:
                        text = _vision_recognize(image.convert("RGBA")).strip()
                    normalized = _normalized_text(text)
                    if normalized and normalized == candidate:
                        candidate_count += 1
                    else:
                        candidate, candidate_count = normalized, 1
                    with self._lock:
                        self._state.update({"status": "reading", "detail": "", "current_text": text})
                    if candidate_count >= 2 and text and (not self._transcript or self._transcript[-1]["normalized"] != normalized):
                        with self._lock:
                            self._transcript.append({"id": int(time.time() * 1000), "text": text, "normalized": normalized, "created_at": time.time()})
                    self._stop.wait(0.2)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            if self.logger:
                self.logger.warning("VN reader stopped: %s", exc)
            with self._lock:
                self._state.update({"running": False, "status": "error", "detail": str(exc)})
