from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw

from pudge.database import Database
from pudge.visual_novels import VisualNovelService
from test_v0727_vn_tcc_audio_controls import AUDIO_HELPERS, AUDIO_PAGE, AUDIOBOOK
from tests.i18n_support import en, ru


def test_vn_retries_empty_ocr_on_unchanged_frame(monkeypatch, tmp_path: Path) -> None:
    # A static frame whose first OCR finds nothing is read again (the text may still be
    # fading in); the state reports the frames and empty attempts for the window.
    import pudge.visual_novels as module

    calls = 0

    def recognize(_image) -> str:
        nonlocal calls
        calls += 1
        return "" if calls <= 3 else "日本語です"

    monkeypatch.setattr(module, "_vision_recognize", recognize)
    service = VisualNovelService(logger=logging.getLogger("pudge.visual_novels"))
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
        frame = Image.new("RGB", (64, 48), "white")
        ImageDraw.Draw(frame).rectangle((8, 30, 40, 40), fill="black")  # a dialogue box
        frame.save(path)

    monkeypatch.setattr(service, "_capture_window", capture)
    thread = threading.Thread(target=service._capture_loop, args=(stop_event, 1, 123), daemon=True)
    service._thread = thread
    # Records of this capture thread only (another test's leftover loop may log too).
    records: list[logging.LogRecord] = []
    collector = logging.Handler(level=logging.INFO)
    collector.emit = lambda record: records.append(record) if record.thread == thread.ident else None  # type: ignore[method-assign]
    vn_logger = logging.getLogger("pudge.visual_novels")
    vn_logger.addHandler(collector)
    previous_level = vn_logger.level
    vn_logger.setLevel(logging.INFO)
    thread.start()
    deadline = time.monotonic() + 2.0
    state = service.state()
    while time.monotonic() < deadline and not state["transcript"]:
        time.sleep(0.03)
        state = service.state()
    stop_event.set()
    thread.join(timeout=1.0)
    vn_logger.removeHandler(collector)
    vn_logger.setLevel(previous_level)

    assert calls >= 4
    assert state["current_text"] == "日本語です"
    assert state["transcript"][-1]["text"] == "日本語です"
    assert state["empty_ocr_count"] >= 1
    assert state["capture_count"] >= 2 and state["ocr_count"] >= 2  # the same frame, read again
    assert (state["last_frame_width"], state["last_frame_height"]) == (64, 48)
    assert state["last_frame_contrast"] > 1.5
    first_frame = [
        r.getMessage() for r in records if r.getMessage().startswith("VN ScreenCaptureKit first frame")
    ]
    assert first_frame == [
        f"VN ScreenCaptureKit first frame window=123 size=64x48 contrast={state['last_frame_contrast']:.2f} ocr_chars=0"
    ]


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


# Audiobook 4 is being transcribed (not playing): the Audiobooks page re-reads it every
# 1.4 s. Speed changes wait until the test calls window.__saved(); until then the backend
# still reports the old speed.
TRANSCRIBING = AUDIO_PAGE.replace(
    "BOOK", json.dumps({**AUDIOBOOK, "transcription": {"status": "transcribing", "progress": 0.2}})
)


def test_chosen_speed_is_not_reverted_by_background_polls_while_it_is_saved(page) -> None:
    result = page.run(
        AUDIO_HELPERS
        + r"""
        setPage('audiobooks', true); await __tick(100);
        const label = () => document.querySelector(speedMenu + ' .pudge-select-label').textContent;
        await chooseSpeed('1.5×');
        await __tick(3000);                                       // two polls while the change is saved
        const saving = {label: label(), value: card().querySelector('[data-audio-speed]').value};
        window.__saved(); await __tick(10);
        const saved = label();
        window.__book = {...window.__book, title: 'Renamed'}; await __tick(1500);     // polls show changes again
        return {saving, saved, title: __text('[data-audiobook-id="4"] .audiobook-title-row strong')};
        """,
        before=TRANSCRIBING,
    )
    assert result.value == {"saving": {"label": "1.5×", "value": "1.5"}, "saved": "1.5×", "title": "Renamed"}
    assert result.calls_of("audiobook_set_speed") == [[4, 1.5]]


def test_focused_position_slider_is_not_replaced_by_background_polls(page) -> None:
    result = page.run(
        AUDIO_HELPERS
        + r"""
        setPage('audiobooks', true); await __tick(100);
        const slider = card().querySelector('[data-audio-position]');
        slider.focus();
        window.__book = {...window.__book, title: 'Renamed'}; await __tick(3000);
        const focused = {same: slider === card().querySelector('[data-audio-position]'), active: document.activeElement === slider};
        slider.blur(); await __tick(1500);
        return {focused, title: __text('[data-audiobook-id="4"] .audiobook-title-row strong')};
        """,
        before=TRANSCRIBING,
    )
    assert result.value == {"focused": {"same": True, "active": True}, "title": "Renamed"}


def test_open_speed_list_keeps_working_during_background_polls(page) -> None:
    result = page.run(
        AUDIO_HELPERS
        + r"""
        setPage('audiobooks', true); await __tick(100);
        await __fire(speedMenu, 'pointerdown', {button: 0}, 'PointerEvent');
        await __tick(3000);                                       // the user looks at the list; polls run
        [...document.querySelectorAll('.pudge-select-menu.open .pudge-select-option')].find(b => b.textContent === '1.5×').click();
        await __tick(10); window.__saved(); await __tick(50);
        """,
        before=TRANSCRIBING,
    )
    assert result.calls_of("audiobook_set_speed") == [[4, 1.5]]


def test_speed_change_while_the_player_starts_keeps_the_resume_point(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    # mpv starting at 100 s: the start-up check expects the clock to run at the current speed.
    # A speed change during the start-up used to be ignored there, so playback at 2× looked
    # like a missed start and was sought back to 100 s.
    import subprocess

    from pudge import audiobooks

    clock = [100.0]
    monkeypatch.setattr(audiobooks.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(audiobooks.time, "sleep", lambda _seconds: None)
    service = audiobooks.AudiobookService(
        Database(tmp_path / "db.sqlite3"), ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache"
    )
    service.book = lambda book: {
        "id": book,
        "position": 100,
        "duration": 600,
        "path": str(tmp_path / "audio.m4b"),
    }
    service._file_rows = lambda book: [
        {"file_index": 0, "path": str(tmp_path / "audio.m4b"), "start": 0, "end": 600}
    ]
    service._tracked_thread = lambda **_kwargs: None
    process = type(
        "Process",
        (),
        {
            "poll": lambda self: None,
            "terminate": lambda self: None,
            "kill": lambda self: None,
            "wait": lambda self, timeout=None: 0,
        },
    )()
    monkeypatch.setattr(subprocess, "Popen", lambda command, **_kwargs: process)
    sent: list[list] = []
    player = {"speed": 1.0}  # the running mpv

    def command(_path, payload: list) -> dict:
        sent.append(payload)
        if payload[:2] == ["set_property", "speed"]:
            player["speed"] = payload[2]
        return {"error": "success"}

    monkeypatch.setattr(service, "_ipc_command", command)
    monkeypatch.setattr(service, "_ipc_get", lambda _path, name: player.get(name))

    service.play(7, start=100, speed=1.0)
    clock[0] = 101.0
    with caplog.at_level(logging.INFO, logger="pudge.audiobooks"):
        service.set_speed(7, 2.0)
    clock[0] = 105.0  # 1 s at 1×, then 4 s at 2×
    assert service._reconcile_startup_position(7, service._ipc_paths[7], 109.0) == 109.0
    assert not [command for command in sent if command[0] == "seek"]
    messages = [record.getMessage() for record in caplog.records]
    assert "Audiobook speed request book=7 speed=2.000 player=True" in messages
    assert "Audiobook speed applied book=7 requested=2.000 live=2.0 restarted=False" in messages


# The VN page while the capture runs but OCR has found no text yet.
def test_vn_status_explains_why_no_text_appears_yet(page) -> None:
    assert "Повторяю OCR на неподвижном кадре" in ru("vn.retryingStaticFrame")
    assert "The frame looks nearly blank/black" in en("vn.frameBlank")
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const status = async state => {
              __api.visual_novel_state = async () => ({running: true, status: 'running', transcript: [], current_text: '', ...state});
              await window.PudgeVisualNovels.load(); await __tick(10); return __text('#vnStatus');
            };
            const frame = {capture_count: 4, ocr_count: 3, empty_ocr_count: 3, last_frame_width: 1280, last_frame_height: 720};
            const waiting = ` (1280×720)`, attempts = ' ' + PudgeI18n.tn('vn.emptyAttempts', 3);
            const noText = PudgeI18n.t('vn.noTextYet', {frame: waiting, attempts});
            return {
              blank: await status({...frame, last_frame_contrast: 0.4}), static: await status({...frame, last_frame_contrast: 42}),
              reading: await status({...frame, last_frame_contrast: 42, current_text: '猫だ。'}),
              expected: {blank: `${noText} ${PudgeI18n.t('vn.frameBlank')}`, static: `${noText} ${PudgeI18n.t('vn.retryingStaticFrame')}`,
                         reading: PudgeI18n.t('vn.readingThroughScreencapturekitFramesOcr', {captures: 4, ocrCount: 3})},
            };
            """,
            lang=lang,
            api={"visual_novel_parse": {"paragraphs": ["猫だ。"], "tokens": [[]]}},
        )
        out = result.value
        assert out["blank"] == out["expected"]["blank"].strip()
        assert out["static"] == out["expected"]["static"].strip()
        assert out["reading"] == out["expected"]["reading"]
