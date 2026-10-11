"""Furigana hand-off between words, global search of audiobooks, EPUB illustrations, long
STT jobs and their memory limit, light audiobook actions (behaviour; audit 71Б rewrite).
Horizontal furigana as an overlay without a reserved row: tests/test_v128_*."""

from __future__ import annotations

import json
import sys
import threading
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import i18n
from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService, _epub_metadata
from pudge.subtitles import stt_worker
from pudge.web_app import _global_search_score
from t71g21_support import AUDIOBOOKS, READER, computed_style, paired
from test_web_app import make_api
from tests.i18n_support import en, ru


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


def test_previous_reading_lingers_briefly_even_where_it_overlaps_the_next(page) -> None:
    # 今日 and 明日 stand so close that their readings overlap. When the highlight moves on,
    # the old reading still lingers its 0.1 s (it used to be suppressed at once as
    # "overlapping"), then goes.
    result = page.run(
        READER
        + computed_style("display")
        + r"""
        await openReader();
        wordEls().forEach((w, i) => { w.querySelector('.ln-furigana-ruby').__rect = {x: 12 * i, y: 40, width: 20, height: 20}; });
        const overlay = i => __css(wordEls()[i].querySelector('.ln-furigana-ruby'), 'display', 'after');
        await untilAudio(102.05);
        const handoff = {word: current(), shown: [overlay(0), overlay(1)], classes: words()[0].classes};
        await untilAudio(102.2);
        return {handoff, after: [overlay(0), overlay(1)]};
        """,
        **paired(playing=True),
    )
    handoff = result.value["handoff"]
    assert handoff["word"] == 1 and handoff["shown"] == ["block", "block"]
    assert (
        "ln-paired-furigana-linger" in handoff["classes"]
        and "ln-paired-furigana-suppressed" not in handoff["classes"]
    )
    assert result.value["after"] == ["none", "block"]


def _audiobook(service: AudiobookService, path: Path, title: str, duration: float = 10.0) -> dict:
    path.write_bytes(b"audio")
    return service._upsert(
        path=path,
        title=title,
        duration=duration,
        files=[
            {
                "index": 0,
                "path": str(path),
                "title": title,
                "duration": duration,
                "start": 0.0,
                "end": duration,
            }
        ],
        chapters=[{"index": 0, "title": title, "start": 0.0, "end": duration}],
    )


def test_global_search_finds_audiobooks_by_romaji_and_opens_them(page, tmp_path: Path) -> None:
    score, matched = _global_search_score("mata", ["また、同じ夢を見ていた"])
    assert score >= 93
    assert matched == "また、同じ夢を見ていた"
    api = make_api(tmp_path)
    try:
        book = _audiobook(api.audiobooks, tmp_path / "mata.m4b", "また、同じ夢を見ていた")
        rows = api.global_media_search("mata")
        library = json.loads(json.dumps(api.audiobooks.state(), default=str))
    finally:
        api.close()
    assert [(row["kind"], row["local_id"], row["page"]) for row in rows] == [
        ("audiobook", int(book["id"]), "audiobooks")
    ]
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            let scrolled = null;
            let proto = document.createElement('article');
            while (proto && !Object.prototype.hasOwnProperty.call(proto, 'scrollIntoView')) proto = Object.getPrototypeOf(proto);
            const original = proto.scrollIntoView;
            proto.scrollIntoView = function (...args) { if (this.matches('[data-audiobook-id]')) scrolled = this.dataset.audiobookId; return original?.apply(this, args); };
            openGlobalSearch(); await __tick(20);
            await __input('#globalSearchInput', 'mata'); await __tick(300);
            const row = document.querySelector('#globalSearchResults [data-global-open-index]');
            const shown = row.innerText;
            await __click('#globalSearchResults [data-global-open-index]'); await __tick(100);
            return {shown, kind: t('ui.audiobook2'), page: ui.page, overlay: __visible('#globalSearchOverlay'), scrolled};
            """,
            lang=lang,
            api={"global_media_search": rows, "planning_search_anilist": [], "audiobook_state": library},
        )
        out = result.value
        assert "また、同じ夢を見ていた" in out["shown"] and out["kind"] in out["shown"]
        assert (out["page"], out["overlay"], out["scrolled"]) == ("audiobooks", False, str(book["id"]))
        assert result.calls_of("global_media_search") == [["mata", 40]]


def _image_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            """<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Illustrated</dc:title></metadata><manifest><item id="body" href="Text/body.xhtml" media-type="application/xhtml+xml"/><item id="pic" href="Images/pic.jpg" media-type="image/jpeg"/></manifest><spine><itemref idref="body"/></spine></package>""",
        )
        zf.writestr(
            "OEBPS/Text/body.xhtml",
            '<html><body><p>前の本文です。</p><img src="../Images/pic.jpg"/><p>後ろの本文です。</p></body></html>',
        )
        zf.writestr("OEBPS/Images/pic.jpg", b"JPEGDATA")


def _import_illustrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[LightNovelService, int]:
    """An EPUB with an inline illustration: imported, stored and parsed without sending the
    image to Jiten."""
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
    monkeypatch.setattr(
        service,
        "_jiten_request",
        lambda _path, payload: (
            seen.append(list(payload["text"])) or {"tokens": [[] for _ in payload["text"]], "vocabulary": []}
        ),
    )
    parsed = service._parse_text(str(row["text"]), str(row["text_hash"]))
    assert all("PUDGE_LN_IMAGE" not in paragraph for batch in seen for paragraph in batch)
    image_index = parsed["paragraphs"].index("[[PUDGE_LN_IMAGE_URL:/covers/inline.jpg]]")
    assert parsed["tokens"][image_index] == []
    return service, int(book["id"])


def test_epub_inline_images_survive_import_and_are_not_sent_to_jiten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _import_illustrated(tmp_path, monkeypatch)


def test_reader_shows_epub_illustrations_between_the_text(
    page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, book_id = _import_illustrated(tmp_path, monkeypatch)
    chapter = json.loads(json.dumps(service.chapter(book_id, 0), default=str))
    opened = json.loads(json.dumps(service.open_book(book_id), default=str))
    result = page.run(
        r"""
        await __settle(openLightNovel(BOOK)); await __tick(100);
        const blocks = [...document.querySelectorAll('#lnReader [data-ln-paragraph]')];
        return {blocks: blocks.map(node => ({tag: node.tagName.toLowerCase(), image: node.querySelector('img')?.getAttribute('src') || null,
                                             visible: node.checkVisibility(), words: node.querySelectorAll('.ln-word').length})),
                text: [...document.querySelectorAll('#lnReader .ln-word')].map(w => w.textContent).join('')};
        """.replace("BOOK", str(book_id)),
        state={"settings": {"review_gate_ln_enabled": False}},
        api={"light_novel_open": opened, "light_novel_chapter": chapter},
    )
    blocks = result.value["blocks"]
    assert [(block["tag"], block["image"]) for block in blocks] == [
        ("p", None),
        ("figure", "/covers/inline.jpg"),
        ("p", None),
    ]
    assert blocks[1]["visible"] is True and blocks[1]["words"] == 0
    assert "PUDGE" not in result.value["text"] and "前の本文" in result.value["text"]


def test_long_stt_is_chunked_with_offsets_and_resumes_completed_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _audio_service(tmp_path)
    source = tmp_path / "long.m4b"
    source.write_bytes(b"audio")
    book = service._upsert(
        path=source,
        title="Long",
        duration=650.0,
        files=[
            {"index": 0, "path": str(source), "title": "Long", "duration": 650.0, "start": 0.0, "end": 650.0}
        ],
        chapters=[{"index": 0, "title": "Long", "start": 0.0, "end": 650.0}],
    )
    plan = service._transcription_chunk_plan(service._file_rows(book["id"]))
    assert [round(row["duration"]) for row in plan] == [300, 300, 50]
    assert [row["global_offset"] for row in plan] == [0.0, 300.0, 600.0]
    assert all(not row["direct"] for row in plan)

    extracted: list[tuple[float, float, Path]] = []

    def fake_extract(
        _source: Path, destination: Path, *, start: float, duration: float, cancel_event
    ) -> None:
        destination.write_bytes(b"chunk")
        extracted.append((start, duration, destination))

    monkeypatch.setattr(service, "_extract_stt_chunk", fake_extract)
    monkeypatch.setattr(service, "_resolved_ffmpeg", lambda: "/usr/bin/false")

    # One worker serves every chunk of the book (the model loads once).
    calls: list[dict] = []
    starts: list[dict] = []

    class FakeWorker:
        def __init__(self):
            self.process = SimpleNamespace(poll=lambda: None)
            self.answers: list[dict] = []

        def submit(self, request):
            calls.append(dict(request))
            Path(request["output"]).write_text(
                json.dumps(
                    {
                        "segments": [
                            {"start": 1.0, "end": 2.0, "words": [{"start": 1.1, "end": 1.4, "word": "x"}]}
                        ]
                    }
                ),
                encoding="utf-8",
            )
            Path(request["progress"]).write_text(
                json.dumps({"percent": 100, "memory": {"peak_bytes": 1234}}), encoding="utf-8"
            )
            self.answers.append({"id": request["id"], "ok": True})
            return True

        def answer(self, timeout):
            return self.answers.pop(0) if self.answers else False

        def stderr_tail(self):
            return ""

        def stop(self, timeout=15.0):
            pass

        def kill(self):
            pass

    monkeypatch.setattr(
        service,
        "_start_stt_worker",
        lambda environment, stderr_path: starts.append(environment) or FakeWorker(),
    )

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
    assert len(calls) == 3 and len(starts) == 1
    assert all(request["words"] for request in calls)
    assert all(not destination.exists() for _start, _duration, destination in extracted)

    # Final-output loss/restart reuses the per-chunk JSON checkpoints and does
    # not run MLX a second time.
    output.unlink()
    event2 = threading.Event()
    service._transcription_cancel_events[book_id] = threading.Event()
    service._transcribe_worker(book_id, output, event2)
    assert event2.is_set() and output.is_file()
    assert len(calls) == 3 and len(starts) == 1


def test_short_stt_keeps_original_source_and_cancel_is_controlled(tmp_path: Path) -> None:
    service = _audio_service(tmp_path)
    source = tmp_path / "short.m4b"
    source.write_bytes(b"audio")
    rows = [{"file_index": 0, "path": str(source), "duration": 120.0, "start": 0.0, "end": 120.0}]
    plan = service._transcription_chunk_plan(rows)
    assert len(plan) == 1 and plan[0]["direct"] is True and plan[0]["source"] == str(source)

    book = service._upsert(
        path=source,
        title="Short",
        duration=120.0,
        files=[
            {"index": 0, "path": str(source), "title": "Short", "duration": 120.0, "start": 0.0, "end": 120.0}
        ],
        chapters=[{"index": 0, "title": "Short", "start": 0.0, "end": 120.0}],
    )
    book_id = int(book["id"])
    cancel = threading.Event()
    cancel.set()
    service._transcription_cancel_events[book_id] = cancel
    done = threading.Event()
    service._transcribe_worker(book_id, service._transcript_path(book_id), done)
    assert done.is_set()
    assert service.transcription_status(book_id)["status"] == "cancelled"


def _fake_mlx(monkeypatch: pytest.MonkeyPatch, calls: list, *, fail: bool = False) -> None:
    core = types.ModuleType("mlx.core")
    core.set_memory_limit = lambda value: calls.append(("memory_limit", value))
    core.set_cache_limit = lambda value: calls.append(("cache_limit", value))
    core.reset_peak_memory = lambda: calls.append(("reset_peak",))
    core.clear_cache = lambda: calls.append(("clear_cache",))
    core.get_active_memory, core.get_cache_memory, core.get_peak_memory = (
        (lambda: 100),
        (lambda: 50),
        (lambda: 1234),
    )
    package = types.ModuleType("mlx")
    package.core = core

    def transcribe(audio: str, **options):
        calls.append(("transcribe", options["word_timestamps"]))
        if fail:
            raise MemoryError("out of memory")
        return {"segments": [{"start": 0.0, "end": 1.0, "text": "x"}]}

    whisper = types.ModuleType("mlx_whisper")
    whisper.transcribe = transcribe
    progress = types.ModuleType("tqdm")
    progress.tqdm = type(
        "tqdm",
        (),
        {"total": 0, "n": 0, "__init__": lambda self, *a, **k: None, "update": lambda self, n=1: None},
    )
    for name, module in (("mlx", package), ("mlx.core", core), ("mlx_whisper", whisper), ("tqdm", progress)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delenv("PUDGE_BACKGROUND_PRIORITY", raising=False)
    monkeypatch.setenv("PUDGE_MLX_MEMORY_LIMIT_BYTES", str(6 * 1024**3))
    monkeypatch.setenv("PUDGE_MLX_CACHE_LIMIT_BYTES", str(1024**3))


def test_mlx_worker_applies_memory_limits_and_reports_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    calls: list = []
    _fake_mlx(monkeypatch, calls)
    output, progress = tmp_path / "out.json", tmp_path / "progress.json"
    assert stt_worker.main(["--words", str(tmp_path / "a.wav"), str(output), "model", str(progress)]) == 0
    # The limits are set before the model runs; the buffer cache is released after it.
    assert calls[:2] == [("cache_limit", 1024**3), ("memory_limit", 6 * 1024**3)]
    assert calls.index(("reset_peak",)) < calls.index(("transcribe", True)) < calls.index(("clear_cache",))
    assert json.loads(output.read_text(encoding="utf-8"))["segments"][0]["text"] == "x"
    assert json.loads(progress.read_text(encoding="utf-8"))["memory"] == {
        "active_bytes": 100,
        "cache_bytes": 50,
        "peak_bytes": 1234,
    }

    calls.clear()
    _fake_mlx(monkeypatch, calls, fail=True)
    assert stt_worker.main([str(tmp_path / "a.wav"), str(output), "model"]) == 4
    assert calls[-1] == ("clear_cache",)
    assert "peak_bytes" in capsys.readouterr().err


def test_transcription_stops_when_mlx_exceeds_its_memory_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _audio_service(tmp_path)
    book_id = int(_audiobook(service, tmp_path / "long.m4b", "Long", 120.0)["id"])
    environments: list[dict] = []
    killed: list[bool] = []

    class GreedyWorker:
        # Reports 7 GiB of peak MLX memory and never finishes.
        def __init__(self):
            self.process = SimpleNamespace(poll=lambda: None)

        def submit(self, request):
            Path(request["progress"]).write_text(
                json.dumps({"percent": 10, "memory": {"peak_bytes": 7 * 1024**3}}), encoding="utf-8"
            )
            Path(request["output"]).unlink(missing_ok=True)
            return True

        def answer(self, timeout):
            return False

        def stderr_tail(self):
            return ""

        def stop(self, timeout=15.0):
            pass

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(service, "_resolved_ffmpeg", lambda: "/usr/bin/false")
    monkeypatch.setattr(
        service,
        "_start_stt_worker",
        lambda environment, stderr_path: environments.append(dict(environment)) or GreedyWorker(),
    )
    service._transcription_cancel_events[book_id] = threading.Event()
    done = threading.Event()
    service._transcribe_worker(book_id, service._transcript_path(book_id), done)
    assert done.is_set() and killed
    assert environments[0]["PUDGE_MLX_MEMORY_LIMIT_BYTES"] == str(6 * 1024**3)
    status = service.transcription_status(book_id)
    assert status["status"] == "error"
    assert (
        status["error"]
        == en("error.audiobook.sttMemoryLimit", gib="7.0")
        == "MLX STT memory safety limit exceeded: 7.0 GiB"
    )
    assert i18n.message(status["error"], "ru") == ru("error.audiobook.sttMemoryLimit", gib="7.0")


def test_transcript_cache_path_is_single_json_file(tmp_path: Path) -> None:
    service = _audio_service(tmp_path)
    source = tmp_path / "book.m4b"
    source.write_bytes(b"audio")
    book = service._upsert(
        path=source,
        title="Book",
        duration=12.0,
        files=[
            {"index": 0, "path": str(source), "title": "Book", "duration": 12.0, "start": 0.0, "end": 12.0}
        ],
        chapters=[{"index": 0, "title": "Book", "start": 0.0, "end": 12.0}],
    )
    path = service._transcript_path(int(book["id"]))
    assert path.parent.name == "audiobook-transcripts"
    assert path.suffix == ".json"
    assert path.name.count(".json") == 1


def test_search_catalog_does_not_load_whole_books(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _audio_service(tmp_path)
    source = tmp_path / "mata.m4b"
    source.write_bytes(b"audio")
    service._upsert(
        path=source,
        title="また、同じ夢を見ていた",
        duration=10.0,
        files=[
            {"index": 0, "path": str(source), "title": "Mata", "duration": 10.0, "start": 0.0, "end": 10.0}
        ],
        chapters=[{"index": 0, "title": "Mata", "start": 0.0, "end": 10.0}],
    )
    monkeypatch.setattr(
        service,
        "book",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("search must not hydrate books")),
    )
    rows = service.search_catalog()
    assert len(rows) == 1 and rows[0]["title"] == "また、同じ夢を見ていた"


def test_audiobook_point_actions_return_the_changed_book_only(tmp_path: Path) -> None:
    # Rebuilding a 200-book library after every click made the controls stall.
    api = make_api(tmp_path)
    try:
        book_id = int(_audiobook(api.audiobooks, tmp_path / "book.m4b", "Book", 100.0)["id"])
        api.audiobooks.state = lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("the whole library was rebuilt")
        )
        results = [
            api.audiobook_set_speed(book_id, 1.5),
            api.audiobook_seek(book_id, 5),
            api.audiobook_seek_to(book_id, 30),
            api.audiobook_add_bookmark(book_id, "here"),
            api.audiobook_sleep_timer(book_id, "off"),
            api.audiobook_stop(book_id),
        ]
        mark = results[3]["bookmark_id"]
        results += [
            api.audiobook_rename_bookmark(mark, "there"),
            api.audiobook_reorder_bookmarks(book_id, [mark]),
            api.audiobook_delete_bookmark(mark),
            api.audiobook_restore_bookmark(book_id, 30.0, "there", 0, 0, mark),
        ]
    finally:
        api.close()
    assert all(result["book"]["id"] == book_id and "state" not in result for result in results)
    assert results[0]["book"]["speed"] == 1.5 and results[2]["book"]["position"] == 30.0
    assert [mark["title"] for mark in results[-1]["book"]["bookmarks"]] == ["there"]


def test_audiobook_page_applies_a_point_action_without_reloading_the_library(page) -> None:
    result = page.run(
        r"""
        setPage('audiobooks', true); await __tick(100);
        const loads = () => __apiCalls.filter(c => c.name === 'audiobook_state').length;
        const before = loads();
        await __click('#audiobooksContent [data-audiobook-id="3"] [data-media-action="bookmark-audio"]'); await __tick(50);
        return {reloads: loads() - before,
                bookmarks: [...document.querySelectorAll('#audiobooksContent [data-audiobook-id="3"] .audiobook-bookmark-label')].map(n => n.textContent)};
        """,
        before=AUDIOBOOKS,
    )
    assert result.value == {"reloads": 0, "bookmarks": ["Here", "2:00"]}
