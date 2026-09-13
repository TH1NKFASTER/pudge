from __future__ import annotations

import json
import subprocess
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

ROOT = Path(__file__).resolve().parents[1]


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

    monkeypatch.setattr(audiobooks_module, "align_light_novel_to_transcript", fake_align)

    audio.prepare_alignment(ln_id)
    assert first_started.wait(1.0)
    second = audio.reprocess_alignment(ln_id)
    assert int(second["generation"]) >= 2

    output = audio._alignment_path(ln_id, audio_id)
    _wait_until(lambda: output.is_file())
    assert json.loads(output.read_text(encoding="utf-8"))["marker"] == "new"

    release_first.set()
    _wait_until(lambda: not any(name.startswith("reading-audio-align-") for name in audio.active_worker_names()))
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


def test_vn_render_ignores_out_of_order_parse_response() -> None:
    source = (ROOT / "pudge/web/visual_novels.js").read_text(encoding="utf-8")
    export_marker = "  window.PudgeVisualNovels={"
    assert export_marker in source
    source = source.replace(
        export_marker,
        "  window.__vnTest={renderCurrent,setState:(g,line,key)=>{pollGeneration=g;lastLineId=line;lastCurrentKey=key;}};\n"
        + export_marker,
        1,
    )
    script = f"""
const vm=require('vm');
const source={json.dumps(source)};
const root={{textContent:'',innerHTML:'',dataset:{{}}}};
let resolves=[];
global.window={{ui:{{lang:'en'}},PudgeReadingTools:{{study:{{renderParsedText:(payload)=>payload.value}}}},addEventListener(){{}}}};
global.document={{documentElement:{{lang:'en'}},getElementById:(id)=>id==='vnCurrent'?root:null,addEventListener(){{}}}};
global.pywebview={{api:{{visual_novel_parse:(text)=>new Promise(resolve=>resolves.push({{text,resolve}}))}}}};
vm.runInThisContext(source);
window.__vnTest.setState(1,0,'1:1:OLD');
const oldPromise=window.__vnTest.renderCurrent('OLD','current:1:1:OLD',1);
window.__vnTest.setState(1,0,'1:2:NEW');
const newPromise=window.__vnTest.renderCurrent('NEW','current:1:2:NEW',1);
resolves.find(row=>row.text==='NEW').resolve({{value:'NEW_PARSED'}});
setImmediate(()=>{{
  resolves.find(row=>row.text==='OLD').resolve({{value:'OLD_PARSED'}});
  Promise.all([oldPromise,newPromise]).then(()=>process.stdout.write(JSON.stringify({{html:root.innerHTML,text:root.textContent}})));
}});
"""
    payload = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert payload["html"] == "NEW_PARSED"


def test_vn_poll_does_not_await_slow_parse() -> None:
    source = (ROOT / "pudge/web/visual_novels.js").read_text(encoding="utf-8")
    update = source.split("  async function update", 1)[1].split("  async function start", 1)[0]
    assert "await renderCurrent" not in update
    assert "void renderCurrent" in update
    assert "requestId!==renderRequestId" in source
    assert "String(lineId)===currentKey" in source


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
