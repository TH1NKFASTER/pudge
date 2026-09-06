from __future__ import annotations

import zipfile
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService

ROOT = Path(__file__).resolve().parents[1]


def _ln_service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "pudge.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(config)


def test_shared_study_card_is_single_ruby_copyable_and_structured() -> None:
    js = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/reading_tools.css").read_text(encoding="utf-8")
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    vn = (ROOT / "pudge/web/visual_novels.js").read_text(encoding="utf-8")

    assert "function plainStudyReading(value)" in js
    assert 'class="pudge-study-term-ruby"' in js
    assert '<div class="pudge-study-reading">' not in js
    assert 'return `<div class="pudge-study-pitch">${rows}</div>`;' in js
    assert "pudge-study-pitch-label" not in css
    assert 'class="pudge-study-header-action"' in js
    assert "action.placement === 'header'" in js
    assert "placement:'header'" in html
    assert 'class="pudge-study-grade grade-again"' in js
    assert 'class="pudge-study-grade grade-hard"' in js
    assert 'class="pudge-study-grade grade-good"' in js
    assert 'class="pudge-study-grade grade-easy"' in js
    assert ".grade-again{border-color:#ef4444" in css
    assert ".grade-hard{border-color:#f59e0b" in css
    assert ".grade-good{border-color:#22c55e" in css
    assert ".grade-easy{border-color:#3b82f6" in css
    assert "pudge-study-add-wrap" in js and "border-left:1px solid #334862" in css
    assert "-webkit-user-select:text;user-select:text" in css
    assert ".pudge-study-card button *{-webkit-user-select:none;user-select:none}" in css
    assert "function studyContext(text, token = {})" in js
    assert "current < 28" in js
    assert "contextStart" in js
    assert "studyContextOptions" in vn and "transcriptRows" in vn


def test_open_cover_preview_keeps_native_pinch_zoom_after_open() -> None:
    js = (ROOT / "pudge/web/cover_preview.js").read_text(encoding="utf-8")
    css = (ROOT / "pudge/web/cover_preview.css").read_text(encoding="utf-8")

    assert "let previewPinch = null;" in js
    assert "function setPreviewZoom(value)" in js
    assert "Math.min(6" in js
    assert "event.target.closest?.('.pudge-cover-preview')" in js
    assert "previewPinch.base*Math.max(.2,Number(event.scale||1))" in js
    assert "document.addEventListener('wheel'" in js
    assert "event.ctrlKey" in js
    assert "setPreviewZoom(1)" in js
    assert "transform-origin:center center" in css


def test_parent_audiobook_folder_imports_child_books_separately(tmp_path: Path, monkeypatch) -> None:
    db = Database(tmp_path / "db.sqlite3")
    service = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    parent = tmp_path / "audiobooks"
    first = parent / "Book A"
    second = parent / "Book B"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "01.mp3").write_bytes(b"audio")
    (second / "01.mp3").write_bytes(b"audio")
    monkeypatch.setattr(service, "_probe", lambda _path: (10.0, []))
    monkeypatch.setattr(service, "auto_link_audiobook", lambda *_a, **_k: None)
    monkeypatch.setattr(service, "prepare_transcription", lambda *_a, **_k: {})

    targets = service.folder_import_targets(parent)
    assert targets == [first.resolve(), second.resolve()]
    books = service.import_folder_collection(parent)
    assert [book["title"] for book in books] == ["Book A", "Book B"]
    assert all(book["file_count"] == 1 for book in books)


def test_disc_subfolders_stay_one_audiobook(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    service = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    parent = tmp_path / "One Book"
    for name in ("Disc 1", "Disc 2"):
        folder = parent / name
        folder.mkdir(parents=True)
        (folder / "01.mp3").write_bytes(b"audio")
    assert service.folder_import_targets(parent) == [parent.resolve()]


def _anchor_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            '''<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Anchor Chapters</dc:title></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="body" href="body.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="body"/></spine></package>''',
        )
        zf.writestr(
            "OEBPS/nav.xhtml",
            '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol><li><a href="body.xhtml#chapter-1">第一章</a></li><li><a href="body.xhtml#chapter-2">第二章</a></li><li><a href="body.xhtml#chapter-3">第三章</a></li></ol></nav><nav epub:type="page-list"><a href="body.xhtml#page-2">2</a></nav></body></html>''',
        )
        zf.writestr(
            "OEBPS/body.xhtml",
            '''<html><body><section id="chapter-1"><h1>第一章</h1><p>最初の本文です。猫がいます。</p><span id="page-2"></span></section><section id="chapter-2"><h1>第二章</h1><p>次の本文です。犬がいます。</p></section><section id="chapter-3"><h1>第三章</h1><p>最後の本文です。鳥がいます。</p></section></body></html>''',
        )


def test_epub_toc_fragments_split_one_spine_document_into_real_chapters(tmp_path: Path) -> None:
    epub = tmp_path / "anchor.epub"
    _anchor_epub(epub)
    service = _ln_service(tmp_path)
    book = service.import_file(epub)

    assert [row["title"] for row in book["chapters"]] == ["第一章", "第二章", "第三章"]
    with service._connect() as conn:
        rows = conn.execute(
            "SELECT title,text FROM ln_chapters WHERE book_id=? ORDER BY chapter_index",
            (book["id"],),
        ).fetchall()
    assert "最初の本文" in str(rows[0]["text"])
    assert "次の本文" in str(rows[1]["text"])
    assert "最後の本文" in str(rows[2]["text"])
    assert all(str(row["title"]) != "2" for row in rows)


def test_folder_dialog_uses_collection_detection() -> None:
    source = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    assert "targets = self.audiobooks.folder_import_targets(selected_folder)" in source
    assert 'result={"book_ids": [int(book["id"]) for book in books], "errors": errors}' in source
