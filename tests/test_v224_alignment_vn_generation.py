from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw

import pudge.audiobooks as audiobooks_module
import pudge.visual_novels as vn_module
from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.visual_novels import VisualNovelService


def _paired_audio(tmp_path: Path) -> tuple[AudiobookService, int, int]:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)

    novels = LightNovelService(cfg)
    source = tmp_path / "book.txt"
    source.write_text("第一章\n狼と商人の物語です。", encoding="utf-8")
    novel = novels.import_file(source)

    audio_path = tmp_path / "book.m4b"
    audio_path.write_bytes(b"audio")
    audio = AudiobookService(
        Database(cfg.library.database_path),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=cfg.paths.cache_dir,
    )
    book = audio._upsert(
        path=audio_path,
        title="Book",
        duration=120.0,
        files=[
            {
                "index": 0,
                "path": str(audio_path),
                "title": "audio",
                "duration": 120.0,
                "start": 0.0,
                "end": 120.0,
            }
        ],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": 120.0}],
    )
    ln_id, audio_id = int(novel["id"]), int(book["id"])
    audio.link_light_novel(ln_id, audio_id, prepare_alignment=False)
    return audio, ln_id, audio_id


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_alignment_reprocess_generation_blocks_late_old_publish(monkeypatch, tmp_path: Path) -> None:
    audio, ln_id, audio_id = _paired_audio(tmp_path)
    first_started = threading.Event()
    release_first = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    monkeypatch.setattr(
        audio,
        "_load_transcript",
        lambda _audio_id: {"segments": [{"start": 0.0, "end": 2.0, "text": "狼と商人"}]},
    )
    monkeypatch.setattr(audio, "_load_or_analyze_activity", lambda *_args, **_kwargs: ([], "test"))
    monkeypatch.setattr(audio, "_cached_chapter_start_reading_hints", lambda _ln_id: {})
    monkeypatch.setattr(audio, "_alignment_chapter_needs_precision", lambda _row: False)
    monkeypatch.setattr(audio, "_transcript_fingerprint", lambda _audio_id: "transcript")
    monkeypatch.setattr(
        audiobooks_module,
        "build_alignment_report",
        lambda alignment, _chapters: {"summary": {"coverage": 1.0}, "marker": alignment["marker"]},
    )

    def fake_align(*_args, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        if call == 1:
            first_started.set()
            assert release_first.wait(2.0)
        return {
            "schema": "reading-audio-v3",
            "chapters": [],
            "confidence": 0.9 if call == 2 else 0.1,
            "anchor_count": 0,
            "marker": "new" if call == 2 else "old",
        }

    # The alignment itself runs in a child process; the test replaces that step.
    monkeypatch.setattr(audio, "_align_reading_audio", fake_align)

    audio.prepare_alignment(ln_id)
    assert first_started.wait(1.0)
    second = audio.reprocess_alignment(ln_id)
    assert int(second["generation"]) >= 2

    output = audio._alignment_path(ln_id, audio_id)
    _wait_until(lambda: output.is_file())
    assert json.loads(output.read_text(encoding="utf-8"))["marker"] == "new"

    release_first.set()
    _wait_until(
        lambda: not any(name.startswith("reading-audio-align-") for name in audio.active_worker_names())
    )
    assert json.loads(output.read_text(encoding="utf-8"))["marker"] == "new"
    status = audio.alignment_status(ln_id)
    assert status["ready"] is True
    assert float(status["confidence"]) == 0.9


def test_alignment_relink_cancels_old_generation(tmp_path: Path) -> None:
    audio, ln_id, audio_id = _paired_audio(tmp_path)
    old_cancel = threading.Event()
    with audio._lock:
        audio._alignment_generations[ln_id] = 3
        audio._alignment_cancel_events[ln_id] = old_cancel
        audio._alignment_jobs[ln_id] = {
            "status": "aligning",
            "ready": False,
            "audiobook_id": audio_id,
            "generation": 3,
        }

    second_path = tmp_path / "second.m4b"
    second_path.write_bytes(b"second audio")
    second = audio._upsert(
        path=second_path,
        title="Book 2",
        duration=90.0,
        files=[],
        chapters=[],
    )
    second_id = int(second["id"])
    audio.link_light_novel(ln_id, second_id, prepare_alignment=False)

    assert old_cancel.is_set()
    assert not audio._alignment_attempt_current(ln_id, audio_id, 3, old_cancel)
    link = audio.link_for_light_novel(ln_id, include_alignment=False)
    assert link is not None
    assert int(link["book"]["id"]) == second_id


# The VN page in the window: the backend reports the line on screen (current_text), and the
# window asks visual_novel_parse for its words. Parse answers wait until the test releases
# them (window.__answer(text)); window.__line = [id, text] is what the capture shows.
VN_SLOW_PARSE = r"""
window.__line = [1, '猫が好き'];
__api.visual_novel_state = async () => ({running: true, status: 'running', capture_count: 1, ocr_count: 1, generation: 1,
  current_text_id: window.__line[0], current_text: window.__line[1], transcript: []});
const pending = new Map();
__api.visual_novel_parse = text => new Promise(resolve => pending.set(text, resolve));
window.__answer = text => pending.get(text)({paragraphs: [text], tokens: [[{start: 0, end: 1, wordId: 1, readingIndex: 0}]],
  vocabulary: [{wordId: 1, readingIndex: 0, spelling: text[0], reading: 'x', meaningsChunks: [['word']], cardState: ['new']}]});
"""
VN_CURRENT = r"""
const current = () => ({text: __text('#vnCurrent'), words: [...document.querySelectorAll('#vnCurrent [data-pudge-study-token]')].map(w => w.textContent)});
const parses = () => __apiCalls.filter(call => call.name === 'visual_novel_parse').map(call => call.args[0]);
"""


def test_vn_keeps_polling_while_a_line_is_parsed_and_shows_only_the_newest_line(page) -> None:
    # A slow parse used to hold up the capture poll (the next line appeared only after it),
    # and a late answer for an older line replaced the newer line's words.
    result = page.run(
        VN_CURRENT
        + r"""
        setPage('visualnovels', true); await __tick(50);
        const out = {first: current(), parses1: parses()};
        window.__line = [2, '犬も好き'];
        await __tick(600);                                 // the poll goes on while 猫が好き is parsed
        out.second = current(); out.parses2 = parses();
        window.__answer('犬も好き'); await __tick(10);
        out.newParsed = current();
        window.__answer('猫が好き'); await __tick(10);     // the late answer of the older line
        out.afterLate = current();
        return out;
        """,
        before=VN_SLOW_PARSE,
    )
    out = result.value
    assert out["first"] == {"text": "猫が好き", "words": []}  # plain text until parsed
    assert out["parses1"] == ["猫が好き"]
    assert out["second"] == {"text": "犬も好き", "words": []}
    assert out["parses2"] == ["猫が好き", "犬も好き"]
    assert out["newParsed"] == {"text": "犬も好き", "words": ["犬"]}
    assert out["afterLate"] == out["newParsed"]


def test_vn_dialogue_roi_beats_non_dialogue_full_frame(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []

    def recognize(image: Image.Image) -> str:
        calls.append(image.size)
        if image.size == (800, 600):
            return "MENU"
        return "これは日本語の台詞です"

    monkeypatch.setattr(vn_module, "_vision_recognize", recognize)
    service = VisualNovelService()
    frame = Image.new("RGB", (800, 600), "white")
    try:
        assert service._recognize_frame(frame) == "これは日本語の台詞です"
    finally:
        frame.close()
    assert calls
    assert calls[0][1] < 600


def test_vn_change_detection_ignores_animation_outside_dialogue_roi() -> None:
    service = VisualNovelService()
    first = Image.new("RGB", (400, 300), "white")
    second = first.copy()
    changed_dialogue = first.copy()
    try:
        ImageDraw.Draw(first).rectangle((0, 0, 399, 50), fill="red")
        ImageDraw.Draw(second).rectangle((0, 0, 399, 50), fill="blue")
        ImageDraw.Draw(changed_dialogue).rectangle((20, 220, 200, 260), fill="black")
        assert service._frame_roi_fingerprint(first) == service._frame_roi_fingerprint(second)
        assert service._frame_roi_fingerprint(first) != service._frame_roi_fingerprint(changed_dialogue)
    finally:
        first.close()
        second.close()
        changed_dialogue.close()


def test_vn_blank_speaker_roi_skips_extra_vision_call(monkeypatch) -> None:
    calls = 0

    def recognize(_image: Image.Image) -> str:
        nonlocal calls
        calls += 1
        return "日本語です"

    monkeypatch.setattr(vn_module, "_vision_recognize", recognize)
    service = VisualNovelService()
    frame = Image.new("RGB", (320, 180), "white")
    try:
        assert service._recognize_frame(frame) == "日本語です"
        assert service._recognize_speaker(frame) == ""
    finally:
        frame.close()
    assert calls == 1
