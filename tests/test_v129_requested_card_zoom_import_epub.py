"""The shared word card, zooming an open cover preview, importing an audiobook collection
folder, EPUB chapters split at Contents fragments (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
import math
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from css_support import css_rules
from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from study_page_support import LN_BOOK
from t71g21_support import AUDIOBOOKS, computed_style
from test_web_app import make_api


def _ln_service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "pudge.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(config)


# Novel 6 of tests/study_page_support.py: 猫 (new, pitch accent 1) が 好き (learning; Jiten
# writes its reading as 好[す]き, pitch accent 2) だ。 Run body: open the card of word `TOKEN`.
CARD = (
    computed_style("user-select", "border-left", "border-color")
    + r"""
await __settle(openLightNovel(6)); await __tick(300);
await __click('#lnReader [data-ln-token="TOKEN"]'); await __tick(20);
const card = document.getElementById('pudgeStudyCard');
const term = card.querySelector('.pudge-study-term'), pitch = card.querySelector('.pudge-study-pitch');
return {
  rubies: [...term.querySelectorAll('ruby')].map(r => [r.querySelector('span').textContent, r.querySelector('rt').textContent]),
  termText: term.textContent, readingLine: Boolean(card.querySelector('.pudge-study-reading')),
  pitch: [...pitch.querySelectorAll('.pudge-pitch-mora')].map(m => m.textContent + (m.classList.contains('high') ? '↑' : '↓')),
  pitchText: pitch.textContent,
  header: [...card.querySelectorAll('.pudge-study-head .pudge-study-header-action')].map(b => b.textContent),
  footer: Boolean(card.querySelector('.pudge-study-secondary-actions')),
  grades: [...card.querySelectorAll('.pudge-study-grade')].map(b => [/grade-(\w+)/.exec(b.className)[1], __css(b, 'border-color')]),
  addBorder: __css(card.querySelector('.pudge-study-add-wrap'), 'border-left'),
  selectable: {term: __css(term, 'user-select'), meaning: __css(card.querySelector('.pudge-study-meanings li'), 'user-select'),
               grade: __css(card.querySelector('.pudge-study-grade'), 'user-select'), close: __css(card.querySelector('.pudge-study-close'), 'user-select')},
  readUpToHere: t('ui.readUpToHere'),
};
"""
)
CARD_BOOK = (
    LN_BOOK
    + "LN_VOCAB[0].pitchAccents = [1]; LN_VOCAB[2].reading = '好[す]き'; LN_VOCAB[2].pitchAccents = [2];"
)
GRADE_COLORS = {
    "again": "#ef4444",
    "something": "#f97316",
    "hard": "#f59e0b",
    "good": "#22c55e",
    "easy": "#3b82f6",
}


def _card(page, token: str, backend: str = "jiten") -> dict:
    return page.run(
        CARD.replace("TOKEN", token),
        before=CARD_BOOK,
        state={"settings": {"review_gate_ln_enabled": False}},
        api={"light_novel_state": {"version": 1, "settings": {"study_backend": backend}, "books": []}},
    ).value


def test_word_card_shows_the_term_once_with_ruby_and_a_bare_pitch_row(page) -> None:
    cat = _card(page, "0:0")
    # One term with its reading as ruby; the reading is not repeated in another line.
    assert cat["rubies"] == [["猫", "ねこ"]] and cat["readingLine"] is False
    # The pitch row is the morae alone (high-low for accent 1, then the particle), no label.
    assert cat["pitch"] == ["ね↑", "こ↓", "・↓"] and cat["pitchText"] == "1ねこ・"
    # Jiten's bracket reading: ruby on the kanji only, the pitch row from the plain kana.
    like = _card(page, "0:2")
    assert like["rubies"] == [["好", "す"]] and like["termText"] == "好すき"
    assert like["pitch"] == ["す↓", "き↑", "・↓"] and like["readingLine"] is False


def test_word_card_actions_grades_and_selectable_text(page) -> None:
    for backend, tones in (
        ("jiten", ["again", "hard", "good", "easy"]),
        ("jpdb", ["again", "something", "hard", "good", "easy"]),
    ):
        out = _card(page, "0:0", backend)
        # Reader actions sit in the header next to the term.
        assert out["header"] == [out["readUpToHere"]] and out["footer"] is False
        assert out["grades"] == [[tone, GRADE_COLORS[tone]] for tone in tones]
        # Add sits apart from the grades (a five-grade row moves it to its own line instead).
        assert out["addBorder"] == ("1px solid #334862" if backend == "jiten" else "0")
        # Card text can be selected and copied; buttons cannot.
        assert out["selectable"] == {"term": "text", "meaning": "text", "grade": "none", "close": "none"}


def _reviewed_sentence(page, paragraphs: list[str], target: int) -> str:
    # A chapter whose only parsed word is 猫 at the start of paragraph `target`; the
    # sentence sent with the review is the card's context.
    chapter = {
        "book_id": 6,
        "chapter_index": 0,
        "paragraphs": paragraphs,
        "tokens": [
            [{"start": 0, "end": 1, "wordId": 101, "readingIndex": 0}] if index == target else []
            for index in range(len(paragraphs))
        ],
        "vocabulary": [
            {
                "wordId": 101,
                "readingIndex": 0,
                "spelling": "猫",
                "reading": "ねこ",
                "meaningsChunks": [["cat"]],
                "cardState": ["learning"],
            }
        ],
    }
    result = page.run(
        r"""
        await __settle(openLightNovel(6)); await __tick(300);
        await __click('#lnReader [data-ln-token="TARGET:0"]'); await __tick(20);
        await __click('#pudgeStudyCard .pudge-study-grade'); await __tick(50);
        """.replace("TARGET", str(target)),
        before=LN_BOOK
        + f"__api.light_novel_chapter = async () => ({json.dumps(chapter, ensure_ascii=False)});",
        state={"settings": {"review_gate_ln_enabled": False}},
    )
    [[review]] = result.calls_of("study_action")
    return review["sentence"]


def test_word_card_context_takes_neighbour_sentences_around_a_short_one(page) -> None:
    short = ["前の文です。", "猫が好きだ。", "次の文です。"]
    assert _reviewed_sentence(page, short, 1) == "前の文です。 猫が好きだ。 次の文です。"
    long = [
        "前の文です。",
        "猫がとても長い文章の中で読み手の集中力を最後まで試し続けている。",
        "次の文です。",
    ]
    assert len(long[1]) >= 28
    assert _reviewed_sentence(page, long, 1) == long[1]


def test_visual_novel_word_card_context_includes_the_neighbour_lines(page) -> None:
    result = page.run(
        r"""
        await window.PudgeVisualNovels.load(); await __tick(50);
        await __click('[data-vn-line="2"]'); await __tick(50);
        await __click('#vnCurrent [data-pudge-study-token]'); await __tick(50);
        await __click('#pudgeStudyCard .pudge-study-grade'); await __tick(50);
        """,
        before=r"""
        __api.visual_novel_state = async () => ({running: false, transcript: [{id: 1, text: '前の行です。'}, {id: 2, text: '猫だ。'}, {id: 3, text: '次の行です。'}]});
        __api.visual_novel_parse = async text => ({paragraphs: [text], tokens: [text === '猫だ。' ? [{start: 0, end: 1, wordId: 101, readingIndex: 0}] : []],
          vocabulary: [{wordId: 101, readingIndex: 0, spelling: '猫', reading: 'ねこ', meaningsChunks: [['cat']], cardState: ['learning']}]});
        """,
    )
    [[review]] = result.calls_of("study_action")
    assert review["sentence"] == "前の行です。 猫だ。 次の行です。"


def test_open_cover_preview_zooms_with_pinch_and_ctrl_wheel(page) -> None:
    # The preview opens from a cover with a trackpad pinch; once open, pinching keeps
    # zooming it (0.5×–6×), so does Ctrl+scroll; a double click goes back to 1×.
    result = page.run(
        r"""
        setPage('audiobooks', true); await __tick(100);
        const gesture = (target, type, scale) => { const e = new Event(type, {bubbles: true, cancelable: true});
          Object.assign(e, {scale, clientX: 650, clientY: 400}); target.dispatchEvent(e); };
        const pinch = (target, scale) => { gesture(target, 'gesturestart', 1); gesture(target, 'gesturechange', scale); gesture(target, 'gestureend', scale); };
        const img = () => document.querySelector('.pudge-cover-preview img');
        const zoom = () => Number(/scale\(([\d.]+)\)/.exec(img().style.transform)[1]);
        pinch(document.querySelector('[data-audiobook-id="3"] .audiobook-cover img'), 1.5); await __tick(50);
        const out = {src: img()?.getAttribute('src')};
        pinch(img(), 2); out.pinched = zoom();
        pinch(img(), 10); out.most = zoom();
        await __fire('.pudge-cover-preview img', 'dblclick', {}, 'MouseEvent'); out.reset = zoom();
        pinch(img(), .05); out.least = zoom();
        await __fire('.pudge-cover-preview img', 'wheel', {ctrlKey: true, deltaY: -100, clientX: 650, clientY: 400}, 'WheelEvent'); out.wheel = zoom();
        out.open = Boolean(img());
        return out;
        """,
        before=AUDIOBOOKS,
    )
    out = result.value
    assert out["src"] == "/covers/a.jpg" and out["open"] is True
    assert (out["pinched"], out["most"], out["reset"], out["least"]) == (2, 6, 1, 0.5)
    assert out["wheel"] == pytest.approx(0.5 * math.e)
    assert (
        css_rules("cover_preview.css").get(".pudge-cover-preview img", "transform-origin") == "center center"
    )


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
            """<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Anchor Chapters</dc:title></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/><item id="body" href="body.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="body"/></spine></package>""",
        )
        zf.writestr(
            "OEBPS/nav.xhtml",
            """<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol><li><a href="body.xhtml#chapter-1">第一章</a></li><li><a href="body.xhtml#chapter-2">第二章</a></li><li><a href="body.xhtml#chapter-3">第三章</a></li></ol></nav><nav epub:type="page-list"><a href="body.xhtml#page-2">2</a></nav></body></html>""",
        )
        zf.writestr(
            "OEBPS/body.xhtml",
            """<html><body><section id="chapter-1"><h1>第一章</h1><p>最初の本文です。猫がいます。</p><span id="page-2"></span></section><section id="chapter-2"><h1>第二章</h1><p>次の本文です。犬がいます。</p></section><section id="chapter-3"><h1>第三章</h1><p>最後の本文です。鳥がいます。</p></section></body></html>""",
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


def test_folder_dialog_imports_each_child_book_of_a_collection(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    try:
        parent = tmp_path / "audiobooks"
        for name in ("Book A", "Book B"):
            (parent / name).mkdir(parents=True)
            (parent / name / "01.mp3").write_bytes(b"audio")
        monkeypatch.setattr(api.audiobooks, "_probe", lambda _path: (10.0, []))
        monkeypatch.setattr(api.audiobooks, "auto_link_audiobook", lambda *_a, **_k: None)
        monkeypatch.setattr(api.audiobooks, "prepare_transcription", lambda *_a, **_k: {})
        api.window = SimpleNamespace(create_file_dialog=lambda *_a, **_k: [str(parent)])
        result = api.choose_audiobook_folder()
        [job] = [job for job in api.job_center.jobs() if job.get("kind") == "import"]
    finally:
        api.close()
    assert [book["title"] for book in result["books"]] == ["Book A", "Book B"] and result["errors"] == []
    assert job["state"] == "succeeded"
    assert job["result"] == {"book_ids": [int(book["id"]) for book in result["books"]], "errors": []}
