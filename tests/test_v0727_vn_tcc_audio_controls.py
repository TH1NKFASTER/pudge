from __future__ import annotations

import threading
import time
from pathlib import Path

from pudge.visual_novels import VisualNovelService

ROOT = Path(__file__).parents[1]


def test_vn_false_preflight_is_diagnostic_not_capture_gate(monkeypatch) -> None:
    import pudge.visual_novels as module

    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    service = VisualNovelService()
    monkeypatch.setattr(service, "_screen_recording_access", lambda: False)
    entered = threading.Event()

    def fake_loop(_stop_event, _generation, window_id):
        assert window_id == 123
        entered.set()

    monkeypatch.setattr(service, "_capture_loop", fake_loop)
    state = service.start(123, "VN")
    assert entered.wait(1.0)
    state = service.state()
    assert state.get("error_code") != "permission_required"
    assert state.get("screen_recording_preflight") is False


def test_vn_actual_screencapturekit_permission_error_is_authoritative() -> None:
    class Error:
        def localizedDescription(self):
            return "The user declined Screen Recording permission"

        def code(self):
            return -1

    assert VisualNovelService._screen_capture_error_code(Error()) == "permission_required"


def test_audiobook_live_poll_never_rebuilds_playing_cards() -> None:
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    assert "const updateAudioLiveFields = () =>" in media
    assert "const livePlayback=(audioState.books||[]).some(book=>book.playing);" in media
    assert "if(livePlayback)updateAudioLiveFields();" in media
    assert 'data-audio-live-summary="${Number(book.id)}"' in media
    assert "audioControlEngagedUntil=Date.now()+15000" in media


def test_audiobook_speed_handler_reaches_backend_after_native_select_closes() -> None:
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    start = media.index("const id=Number(control.dataset.id),speed=Number(control.value||1);")
    block = media[start : start + 700]
    assert "audioControlEngagedUntil=0" in block
    assert "await pywebview.api.audiobook_set_speed(id,speed)" in block
    assert "audioControlMutation+=1" in block


def test_audiobook_backend_speed_diagnostics_are_present() -> None:
    audio = (ROOT / "pudge/audiobooks.py").read_text(encoding="utf-8")
    start = audio.index("    def set_speed(self, book_id: int, speed: float)")
    end = audio.index("    def seek(self, book_id: int, seconds: float)", start)
    block = audio[start:end]
    assert "Audiobook speed request" in block
    assert "Audiobook speed applied" in block
    assert 'startup_target["speed"] = value' in block
