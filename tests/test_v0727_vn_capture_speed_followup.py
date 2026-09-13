from __future__ import annotations

import threading
import time
from pathlib import Path

from PIL import Image

from pudge.visual_novels import VisualNovelService


ROOT = Path(__file__).parents[1]


def test_vn_retries_empty_ocr_on_unchanged_frame(monkeypatch, tmp_path: Path) -> None:
    import pudge.visual_novels as module

    calls = 0

    def recognize(_image) -> str:
        nonlocal calls
        calls += 1
        return "" if calls <= 3 else "日本語です"

    monkeypatch.setattr(module, "_vision_recognize", recognize)
    service = VisualNovelService()
    service._generation = 1
    service._state = {
        "running": True,
        "status": "starting",
        "detail": "",
        "error_code": "",
        "capture_backend": "screencapturekit",
        "current_text": "",
        "current_text_id": 0,
        "generation": 1,
        "capture_count": 0,
        "ocr_count": 0,
        "empty_ocr_count": 0,
        "last_frame_width": 0,
        "last_frame_height": 0,
        "last_frame_contrast": 0.0,
        "last_capture_at": 0.0,
    }
    service._EMPTY_OCR_FAST_RETRY_SECONDS = 0.05
    service._COMMIT_AFTER_SECONDS = 0.05
    stop_event = threading.Event()
    service._stop_event = stop_event

    monkeypatch.setattr(service, "_capture_target", lambda *_args: (object(), object()))

    def capture(path: Path, *_args) -> None:
        Image.new("RGB", (64, 48), "white").save(path)

    monkeypatch.setattr(service, "_capture_window", capture)
    thread = threading.Thread(target=service._capture_loop, args=(stop_event, 1, 123), daemon=True)
    service._thread = thread
    thread.start()
    deadline = time.monotonic() + 2.0
    state = service.state()
    while time.monotonic() < deadline and not state["transcript"]:
        time.sleep(0.03)
        state = service.state()
    stop_event.set()
    thread.join(timeout=1.0)

    assert calls >= 4
    assert state["current_text"] == "日本語です"
    assert state["transcript"][-1]["text"] == "日本語です"
    assert state["empty_ocr_count"] >= 1


def test_vn_ocr_drops_capture_alpha_before_vision(monkeypatch) -> None:
    import pudge.visual_novels as module

    alpha_extrema = []

    def recognize(image) -> str:
        alpha_extrema.append(image.getchannel("A").getextrema())
        return "日本語"

    monkeypatch.setattr(module, "_vision_recognize", recognize)
    service = VisualNovelService()
    transparent = Image.new("RGBA", (32, 24), (255, 255, 255, 0))
    try:
        assert service._recognize_frame(transparent) == "日本語"
    finally:
        transparent.close()

    assert alpha_extrema == [(255, 255)]


def test_audio_speed_control_is_not_destroyed_by_live_polling() -> None:
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    assert "let audioControlMutation = 0;" in media
    assert "focusedAudioControl" in media
    assert "audioControlMutation===0&&!focusedAudioControl" in media
    assert "audioControlMutation+=1" in media
    assert "audioControlMutation=Math.max(0,audioControlMutation-1)" in media


def test_audiobook_speed_updates_startup_target_and_logs_application() -> None:
    audio = (ROOT / "pudge/audiobooks.py").read_text(encoding="utf-8")
    start = audio.index("    def set_speed(self, book_id: int, speed: float)")
    end = audio.index("    def seek(self, book_id: int, seconds: float)", start)
    block = audio[start:end]
    assert 'startup_target["speed"] = value' in block
    assert "Audiobook speed request" in block
    assert "Audiobook speed applied" in block


def test_vn_runtime_exposes_non_text_capture_diagnostics() -> None:
    backend = (ROOT / "pudge/visual_novels.py").read_text(encoding="utf-8")
    frontend = (ROOT / "pudge/web/visual_novels.js").read_text(encoding="utf-8")
    assert "_EMPTY_OCR_FAST_RETRY_SECONDS" in backend
    assert "empty_ocr_count" in backend
    assert "last_frame_contrast" in backend
    assert "VN ScreenCaptureKit first frame" in backend
    assert "Повторяю OCR на неподвижном кадре" in frontend
    assert "The frame looks nearly blank/black" in frontend
