from __future__ import annotations

from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.reading_audio_alignment import _inject_punctuation_pause_anchors

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"
WEB_APP = ROOT / "pudge" / "web_app.py"


def _cfg(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _paired_services(tmp_path: Path) -> tuple[AudiobookService, int, int]:
    cfg = _cfg(tmp_path)
    ln = LightNovelService(cfg)
    source = tmp_path / "book.txt"
    source.write_text("第一章\n狼と商人の物語です。\n\n第二章\n市場へ向かいます。", encoding="utf-8")
    book = ln.import_file(source)
    audio_file = tmp_path / "audio.m4b"
    audio_file.write_bytes(b"audio")
    audio = AudiobookService(
        Database(cfg.library.database_path),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=cfg.paths.cache_dir,
    )
    audiobook = audio._upsert(
        path=audio_file,
        title="Book 1",
        duration=100.0,
        files=[{"index": 0, "path": str(audio_file), "title": "audio", "duration": 100.0, "start": 0.0, "end": 100.0}],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": 100.0}],
    )
    ln_id, audio_id = int(book["id"]), int(audiobook["id"])
    audio.link_light_novel(ln_id, audio_id, prepare_alignment=False)
    return audio, ln_id, audio_id


def test_blur_is_clipped_to_plain_image_frame_without_forced_compositor_layer() -> None:
    html = INDEX.read_text(encoding="utf-8")
    frame = html.split(".ln-inline-image-frame{", 1)[1].split("}", 1)[0]
    assert "overflow:hidden" in frame
    assert "contain:" not in frame
    assert "translateZ" not in frame
    assert "backface-visibility" not in frame
    assert 'loading="eager"' in html
    assert 'decoding="async"' in html
    assert ".ln-reader.blur-images .ln-inline-image:not(.revealed) img{filter:blur(44px);cursor:pointer}" in html
    assert "canvas.className='ln-inline-image-raster'" not in html


def test_parsed_chapter_rerender_reuses_already_loaded_image_nodes() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert "function lnReplaceReaderHtmlPreservingImages" in html
    assert "existing.set(key,figure)" in html
    assert "if(previous)figure.replaceWith(previous)" in html
    assert "root.replaceChildren(template.content)" in html
    assert "lnReplaceReaderHtmlPreservingImages(html)" in html


def test_reader_shell_opens_before_python_book_metadata_returns() -> None:
    html = INDEX.read_text(encoding="utf-8")
    function = html.split("async function openLightNovel(bookId)", 1)[1].split("async function loadLightNovelChapter", 1)[0]
    show_at = function.index("$('lnReaderShell').classList.add('open')")
    await_at = function.index("await pywebview.api.light_novel_open")
    assert show_at < await_at
    backend = WEB_APP.read_text(encoding="utf-8")
    open_fn = backend.split("def light_novel_open", 1)[1].split("def light_novel_chapter", 1)[0]
    assert "include_alignment=False" in open_fn
    assert "include_transcription=False" in open_fn


def test_lightweight_book_does_not_touch_transcript_fingerprint(tmp_path: Path) -> None:
    audio, _ln_id, audio_id = _paired_services(tmp_path)
    audio.transcription_status = lambda _book_id: (_ for _ in ()).throw(AssertionError("transcription should be lazy"))  # type: ignore[method-assign]
    book = audio.book(audio_id, include_transcription=False)
    assert book["transcription"] == {"status": "unknown", "ready": False}


def test_paired_state_loads_ready_alignment_once_per_poll(tmp_path: Path) -> None:
    audio, ln_id, _audio_id = _paired_services(tmp_path)
    calls = 0
    alignment = {
        "schema": "reading-audio-v3",
        "model": "test",
        "confidence": 0.9,
        "anchor_count": 0,
        "chapters": [],
        "quality": {"coverage": 1.0, "warning_count": 0},
    }

    def load(_ln: int, _audio: int):
        nonlocal calls
        calls += 1
        return alignment

    audio._load_alignment = load  # type: ignore[method-assign]
    state = audio.paired_state(ln_id)
    assert state["alignment"]["ready"] is True
    assert calls == 1


def test_uncached_audiobook_activity_uses_stt_without_full_audio_fft(tmp_path: Path, monkeypatch) -> None:
    audio, _ln_id, audio_id = _paired_services(tmp_path)
    monkeypatch.setattr("pudge.audiobooks.analyze_audio_activity", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("FFT must not block first alignment")))
    regions, source = audio._load_or_analyze_activity(
        audio_id,
        [{"start": 1.0, "end": 2.0, "words": [{"start": 1.0, "end": 1.4}, {"start": 1.5, "end": 2.0}]}],
    )
    assert source == "stt"
    assert regions


def test_punctuation_pause_clock_uses_indexed_ranges_and_keeps_expected_clock() -> None:
    anchors = [{"offset": i * 2, "time": i * 0.1} for i in range(101)]
    boundaries = [{"offset": 40, "strength": 3}, {"offset": 80, "strength": 3}, {"offset": 120, "strength": 3}]
    regions = [{"start": 0.0, "end": 1.8}, {"start": 2.2, "end": 3.8}, {"start": 4.2, "end": 10.0}]
    output, count = _inject_punctuation_pause_anchors(anchors, boundaries, regions)
    assert count == 2
    assert output == sorted(output, key=lambda row: (float(row["time"]), float(row["offset"])))
    assert any(abs(float(row["time"]) - 1.8) < 1e-6 for row in output)
    source = (ROOT / "pudge" / "reading_audio_alignment.py").read_text(encoding="utf-8")
    fn = source.split("def _inject_punctuation_pause_anchors", 1)[1].split("def _transcript_clock", 1)[0]
    assert "bisect.bisect_right(anchor_times" in fn
    assert "for row in anchors if" not in fn


def test_reader_wheel_uses_native_webkit_scrolling() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert "function lnSmoothCoarseWheel" not in html
    wheel = "$('lnReaderScroll').addEventListener('wheel',()=>markLnPairedManualNavigation('scroll',2000),{passive:true});"
    assert wheel in html
    assert "{passive:false}" not in html.split("$('lnReaderScroll').addEventListener('wheel'", 1)[1][:220]
