from __future__ import annotations

import json
import threading
import zipfile
from pathlib import Path

import pytest

import pudge.audiobooks as audiobook_module
from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService, _epub_metadata
from pudge.web_app import _global_search_score

ROOT = Path(__file__).resolve().parents[1]


def _audio_service(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "audio.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )


def _ln_service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "ln.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(cfg)


def test_furigana_is_overlay_only_and_hands_off_with_bounded_linger() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "pudge-v133-ln-furigana-dom-overlay-v1" in html
    assert "document.createElement('span')" in html
    assert "ruby.replaceWith(span)" in html
    assert "content:attr(data-ln-reading)" in html
    assert "width:max-content" in html
    assert "ln-furigana-reserved-row-v1" not in html
    assert "lnPairedFuriganaPaintOverlaps(old,word)" not in html.split("function renderLnPairedPosition", 1)[1].split("function startLnPairedInterpolation", 1)[0]
    assert "lingerLnPairedFurigana(old,false)" in html
    assert "},100)" in html


def test_global_search_matches_romaji_and_has_audiobook_route() -> None:
    score, matched = _global_search_score("mata", ["また、同じ夢を見ていた"])
    assert score >= 93
    assert matched == "また、同じ夢を見ていた"
    backend = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    frontend = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert '"kind": "audiobook"' in backend
    assert "self.audiobooks.search_catalog()" in backend
    assert "def search_catalog(self)" in (ROOT / "pudge/audiobooks.py").read_text(encoding="utf-8")
    assert "if(item.kind==='audiobook')" in frontend


def _image_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            '''<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Illustrated</dc:title></metadata><manifest><item id="body" href="Text/body.xhtml" media-type="application/xhtml+xml"/><item id="pic" href="Images/pic.jpg" media-type="image/jpeg"/></manifest><spine><itemref idref="body"/></spine></package>''',
        )
        zf.writestr(
            "OEBPS/Text/body.xhtml",
            '<html><body><p>前の本文です。</p><img src="../Images/pic.jpg"/><p>後ろの本文です。</p></body></html>',
        )
        zf.writestr("OEBPS/Images/pic.jpg", b"JPEGDATA")


def test_epub_inline_images_survive_import_and_are_not_sent_to_jiten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    epub = tmp_path / "illustrated.epub"
    _image_epub(epub)
    _title, chapters, _cover = _epub_metadata(epub)
    assert "PUDGE_EPUB_IMAGE" in chapters[0][1]

    service = _ln_service(tmp_path)
    monkeypatch.setattr(service, "_store_cover_bytes", lambda raw, suffix: "/covers/inline.jpg")
    monkeypatch.setattr(service, "queue_auto_bind_anilist", lambda *_a, **_k: None)
    monkeypatch.setattr(service, "_inherit_series_anilist", lambda *_a, **_k: None)
    book = service.import_file(epub)
    with service._connect() as conn:
        row = conn.execute("SELECT text,text_hash FROM ln_chapters WHERE book_id=?", (book["id"],)).fetchone()
    assert row is not None
    assert "[[PUDGE_LN_IMAGE_URL:/covers/inline.jpg]]" in row["text"]

    seen: list[list[str]] = []
    monkeypatch.setattr(service, "_jiten_request", lambda _path, payload: seen.append(list(payload["text"])) or {"tokens": [[] for _ in payload["text"]], "vocabulary": []})
    parsed = service._parse_text(str(row["text"]), str(row["text_hash"]))
    assert all("PUDGE_LN_IMAGE" not in paragraph for batch in seen for paragraph in batch)
    image_index = parsed["paragraphs"].index("[[PUDGE_LN_IMAGE_URL:/covers/inline.jpg]]")
    assert parsed["tokens"][image_index] == []
    frontend = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "ln-inline-image" in frontend


def test_long_stt_is_chunked_with_offsets_and_resumes_completed_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _audio_service(tmp_path)
    source = tmp_path / "long.m4b"
    source.write_bytes(b"audio")
    book = service._upsert(
        path=source,
        title="Long",
        duration=650.0,
        files=[{"index":0,"path":str(source),"title":"Long","duration":650.0,"start":0.0,"end":650.0}],
        chapters=[{"index":0,"title":"Long","start":0.0,"end":650.0}],
    )
    plan = service._transcription_chunk_plan(service._file_rows(book["id"]))
    assert [round(row["duration"]) for row in plan] == [300, 300, 50]
    assert [row["global_offset"] for row in plan] == [0.0, 300.0, 600.0]
    assert all(not row["direct"] for row in plan)

    extracted: list[tuple[float, float, Path]] = []
    def fake_extract(_source: Path, destination: Path, *, start: float, duration: float, cancel_event) -> None:
        destination.write_bytes(b"chunk")
        extracted.append((start, duration, destination))
    monkeypatch.setattr(service, "_extract_stt_chunk", fake_extract)
    monkeypatch.setattr(service, "_resolved_ffmpeg", lambda: "/usr/bin/false")

    calls: list[list[str]] = []
    class FakeProcess:
        returncode = 0
        def __init__(self, command, **_kwargs):
            self.command = list(command); calls.append(self.command)
            result = Path(self.command[5]); progress = Path(self.command[7])
            result.write_text(json.dumps({"segments":[{"start":1.0,"end":2.0,"words":[{"start":1.1,"end":1.4,"word":"x"}]}]}), encoding="utf-8")
            progress.write_text(json.dumps({"percent":100,"memory":{"peak_bytes":1234}}), encoding="utf-8")
        def poll(self): return 0
        def terminate(self): self.returncode = -15
        def kill(self): self.returncode = -9
        def wait(self, timeout=None): return self.returncode
    monkeypatch.setattr(audiobook_module.subprocess, "Popen", FakeProcess)

    book_id = int(book["id"])
    event = threading.Event()
    service._transcription_cancel_events[book_id] = threading.Event()
    output = service._transcript_path(book_id)
    service._transcribe_worker(book_id, output, event)
    assert event.is_set() and output.is_file()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == "audiobook-stt-v3"
    assert [row["start"] for row in payload["segments"]] == [1.0, 301.0, 601.0]
    assert [row["words"][0]["start"] for row in payload["segments"]] == [1.1, 301.1, 601.1]
    assert len(calls) == 3
    assert all(not destination.exists() for _start, _duration, destination in extracted)

    # Final-output loss/restart reuses the per-chunk JSON checkpoints and does
    # not run MLX a second time.
    output.unlink()
    event2 = threading.Event()
    service._transcription_cancel_events[book_id] = threading.Event()
    service._transcribe_worker(book_id, output, event2)
    assert event2.is_set() and output.is_file()
    assert len(calls) == 3


def test_short_stt_keeps_original_source_and_cancel_is_controlled(tmp_path: Path) -> None:
    service = _audio_service(tmp_path)
    source = tmp_path / "short.m4b"; source.write_bytes(b"audio")
    rows=[{"file_index":0,"path":str(source),"duration":120.0,"start":0.0,"end":120.0}]
    plan=service._transcription_chunk_plan(rows)
    assert len(plan)==1 and plan[0]["direct"] is True and plan[0]["source"]==str(source)

    book=service._upsert(path=source,title="Short",duration=120.0,files=[{"index":0,"path":str(source),"title":"Short","duration":120.0,"start":0.0,"end":120.0}],chapters=[{"index":0,"title":"Short","start":0.0,"end":120.0}])
    book_id=int(book["id"]); cancel=threading.Event(); cancel.set(); service._transcription_cancel_events[book_id]=cancel
    done=threading.Event(); service._transcribe_worker(book_id, service._transcript_path(book_id), done)
    assert done.is_set()
    assert service.transcription_status(book_id)["status"] == "cancelled"


def test_mlx_worker_has_hard_memory_controls_and_reports_memory() -> None:
    worker=(ROOT/"pudge/subtitles/stt_worker.py").read_text(encoding="utf-8")
    audio=(ROOT/"pudge/audiobooks.py").read_text(encoding="utf-8")
    assert "set_memory_limit" in worker and "set_cache_limit" in worker and "clear_cache" in worker
    assert "get_peak_memory" in worker and '"memory": _mlx_memory_snapshot()' in worker
    assert "_STT_MLX_MEMORY_LIMIT_BYTES = 6 * 1024 * 1024 * 1024" in audio
    assert "MLX STT memory safety limit exceeded" in audio


def test_transcript_cache_path_is_single_json_file(tmp_path: Path) -> None:
    service = _audio_service(tmp_path)
    source = tmp_path / "book.m4b"
    source.write_bytes(b"audio")
    book = service._upsert(
        path=source,
        title="Book",
        duration=12.0,
        files=[{"index": 0, "path": str(source), "title": "Book", "duration": 12.0, "start": 0.0, "end": 12.0}],
        chapters=[{"index": 0, "title": "Book", "start": 0.0, "end": 12.0}],
    )
    path = service._transcript_path(int(book["id"]))
    assert path.parent.name == "audiobook-transcripts"
    assert path.suffix == ".json"
    assert path.name.count(".json") == 1


def test_search_catalog_is_lightweight_and_point_actions_do_not_rebuild_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _audio_service(tmp_path)
    source = tmp_path / "mata.m4b"
    source.write_bytes(b"audio")
    service._upsert(
        path=source,
        title="また、同じ夢を見ていた",
        duration=10.0,
        files=[{"index": 0, "path": str(source), "title": "Mata", "duration": 10.0, "start": 0.0, "end": 10.0}],
        chapters=[{"index": 0, "title": "Mata", "start": 0.0, "end": 10.0}],
    )
    monkeypatch.setattr(service, "book", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("search must not hydrate books")))
    rows = service.search_catalog()
    assert len(rows) == 1 and rows[0]["title"] == "また、同じ夢を見ていた"

    backend = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    frontend = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    start = backend.index("    def audiobook_set_speed")
    end = backend.index("    def audiobook_reveal_source", start)
    action_block = backend[start:end]
    assert "self.audiobooks.state()" not in action_block
    assert "const applyAudioActionResult = result =>" in frontend
    assert "result.state||await pywebview.api.audiobook_state()" not in frontend
