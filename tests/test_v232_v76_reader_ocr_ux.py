"""Manga OCR waiting for playback, no N+1 marks in manga, a local LN cover rebuilt from its
EPUB, optimistic word-card reviews and the Jiten link in the card header (behaviour;
audit 71Б rewrite)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from pudge.web_app import WebAppApi
from t71g03_support import MANGA, READER
from t71g06_support import STUDY
from t71g08_support import OCR_STATUS, OCR_UI
from tests.css_support import css_rules
from tests.i18n_support import en, ru


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (48, 72), (60, 90, 120)).save(out, format="PNG")
    return out.getvalue()


def _epub_with_cover(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>表紙テスト</dc:title><meta name="cover" content="cover"/>'
            "</metadata><manifest>"
            '<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>'
            '<item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="c1"/></spine></package>',
        )
        zf.writestr("OEBPS/cover.png", _png())
        zf.writestr("OEBPS/c1.xhtml", "<html><body><p>これは日本語の本文です。</p></body></html>")


def _ln_config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_manga_status_exposes_foreground_wait_reason_while_queued() -> None:
    class Scheduler:
        def background_wait_reason(self):
            return "foreground"

    cache = {
        "book_id": 1,
        "total_pages": 10,
        "completed_pages": 0,
        "failed_pages": 0,
        "cached_pages": 0,
        "complete": False,
    }
    api = WebAppApi.__new__(WebAppApi)
    api.manga = SimpleNamespace(
        ocr_cache_status=lambda _book_id: dict(cache),
        work_scheduler=Scheduler(),
    )
    api._manga_book_ocr_lock = __import__("threading").Lock()
    api._manga_book_ocr_state = {1: {"state": "queued", "running": True, "errors": []}}
    api._manga_book_ocr_threads = {1: SimpleNamespace(is_alive=lambda: True)}

    status = api.manga_ocr_book_status(1)

    assert status["wait_reason"] == "foreground"
    assert status["running"] is True
    assert status["processed_pages"] == 0


def test_manga_status_exposes_foreground_wait_during_initial_detecting_phase() -> None:
    class Scheduler:
        def background_wait_reason(self):
            return "foreground"

    cache = {
        "book_id": 1,
        "total_pages": 10,
        "completed_pages": 0,
        "failed_pages": 0,
        "cached_pages": 0,
        "complete": False,
    }
    api = WebAppApi.__new__(WebAppApi)
    api.manga = SimpleNamespace(ocr_cache_status=lambda _book_id: dict(cache), work_scheduler=Scheduler())
    api._manga_book_ocr_lock = __import__("threading").Lock()
    api._manga_book_ocr_state = {
        1: {
            "state": "preparing",
            "phase": "detecting",
            "running": True,
            "processed_pages": 0,
            "prepared_pages": 0,
            "page_index": None,
            "errors": [],
        }
    }
    api._manga_book_ocr_threads = {1: SimpleNamespace(is_alive=lambda: True)}

    status = api.manga_ocr_book_status(1)

    assert status["wait_reason"] == "foreground"
    assert status["current_page"] is None


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_manga_reader_says_ocr_waits_for_playback_and_keeps_polling_while_it_waits(page, lang) -> None:
    assert ru("manga.ocrWaitingForPlaybackTo") == "OCR ждёт окончания просмотра аниме"
    assert en("manga.ocrWaitingForPlaybackTo") == "OCR waiting for playback to finish"
    result = page.run(
        READER
        + OCR_UI
        + r"""
        for (const book of window.MANGA.books) book.ocr_complete = false;
        window.STATUS = {book_id: 3, running: true, state: 'queued', wait_reason: 'foreground', complete: false, total_pages: 6, completed: 2, processed_pages: 2};
        await open(3);
        const out = {waiting: ocrUi().progress};
        const asked = () => __apiCalls.filter(call => call.name === 'manga_ocr_book_status').map(call => call.at);
        const opened = asked().length;
        await __tick(3000);
        // The reader keeps asking every 750 ms while the job waits.
        const times = asked().slice(opened - 1);
        out.gaps = times.slice(1).map((at, k) => Math.round(at - times[k]));
        out.stillWaiting = ocrUi().progress;
        await __click('[data-manga-v2-action="close"]'); await __tick(50);
        const closed = statusCalls();
        await __tick(3000);
        out.afterClose = statusCalls() - closed;
        out.expected = `${t('manga.ocrWaitingForPlaybackTo')} · 2/6`;
        return out;
        """,
        lang=lang,
        before=MANGA + OCR_STATUS,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    out = result.value
    assert out["waiting"] == out["stillWaiting"] == out["expected"]
    assert out["gaps"][1:] == [750, 750, 750] and out["gaps"][0] <= 750
    assert out["afterClose"] == 0


def test_manga_reader_settings_have_no_n_plus_one_switch(page) -> None:
    assert en("ui.highlightOptimalWordsN1") == "Highlight optimal words (N+1)"
    assert ru("ui.highlightOptimalWordsN1") == "Подсвечивать оптимальные слова (N+1)"
    for lang in ("en", "ru"):
        result = page.run(
            READER
            + r"""
            await open(3);
            await __click('.manga-v2-toolbar [data-manga-v2-action="settings"]'); await __tick(20);
            return {shown: __visible('#mangaV2Settings'), text: __text('#mangaV2Settings'), option: t('ui.highlightOptimalWordsN1')};
            """,
            lang=lang,
            before=MANGA,
            state={"settings": {"review_gate_manga_enabled": False}},
        )
        out = result.value
        assert out["shown"] is True and out["text"]
        assert out["option"] not in out["text"] and "N+1" not in out["text"]
    # Words on manga pages never get the N+1 mark: tests/test_v227_r17_jiten_sentence_mining_nplus1.py.


def test_missing_local_ln_cover_is_rebuilt_from_epub_before_books_payload(tmp_path: Path) -> None:
    service = LightNovelService(_ln_config(tmp_path))
    source = tmp_path / "book.epub"
    _epub_with_cover(source)
    book = service.import_file(source)
    cover_url = str(book["cover_url"])
    assert cover_url.startswith("covers/ln-")
    stored = service.cover_cache_dir / Path(cover_url).name
    assert stored.is_file()
    stored.unlink()

    refreshed = next(row for row in service.books() if int(row["id"]) == int(book["id"]))

    # The library payload shows the 320x480 copy of the restored cover.
    assert refreshed["cover_url"] in {cover_url, f"{cover_url}.list.jpg"}
    assert stored.is_file()
    assert stored.read_bytes() == _png()


# Two N+1 sentences: nine mature words and the same new, frequent word (好, id 10).
# A third sentence with the new word 猫 (id 20). Live Jiten states: window.__live.
# Reviews and adds wait for window.finish(answer).
WORD_CARD = (
    STUDY
    + r"""
for (let id = 1; id <= 9; id++) window.__live[`${id}:0`] = {states: ['mature'], normalizedState: 'learning'};
window.__live['10:0'] = {states: ['new'], normalizedState: 'new'};
window.__live['20:0'] = {states: ['new'], normalizedState: 'new'};
__api.study_decks = async () => [{id: '12', name: 'Mining'}];
window.__pending = [];
__api.study_action = payload => new Promise(resolve => window.__pending.push(resolve));
window.finish = answer => window.__pending.shift()(answer);
"""
)

SENTENCES = r"""
const sentence = (text, newWord) => {
  const box = document.createElement('div');
  box.className = 'sentence';
  box.innerHTML = PudgeReadingTools.study.renderParsedText({paragraphs: [text], tokens: [[...text].map((ch, i) => ({wordId: i < 9 ? i + 1 : newWord, readingIndex: 0, start: i, end: i + 1}))],
    vocabulary: [...text].map((ch, i) => i < 9
      ? {wordId: i + 1, readingIndex: 0, spelling: ch, states: ['mature'], frequencyRank: 100, studyDeckIds: [1]}
      : {wordId: newWord, readingIndex: 0, spelling: ch, states: ['new'], frequencyRank: 300, studyDeckIds: [1]})}, {backend: 'jiten'});
  document.body.appendChild(box);
};
sentence('一二三四五六七八九好', 10); sentence('九八七六五四三二一好', 10); sentence('一二三四五六七八九猫', 20);
PudgeReadingTools.study.refreshOptimalHighlights();
const gold = () => [...document.querySelectorAll('.sentence .pudge-optimal-word')].map(n => n.textContent);
const word = (text, k = 0) => [...document.querySelectorAll('.sentence [data-pudge-study-token]')].filter(n => n.textContent === text)[k];
const openCard = async (text, k = 0) => { await __click(`[data-pudge-study-token="${word(text, k).dataset.pudgeStudyToken}"]`); await __tick(50); };
const cardOpen = () => document.getElementById('pudgeStudyCard').classList.contains('open');
"""


def test_reviews_and_adds_close_the_card_and_drop_the_n_plus_one_mark_at_once(page) -> None:
    assert en("reading.openInJiten") == "Open in Jiten"
    result = page.run(
        SENTENCES
        + r"""
        const out = {start: gold()};
        await openCard('好');
        const link = document.querySelector('#pudgeStudyCard .pudge-study-head [data-pudge-study-jiten]');
        out.link = {text: link?.textContent, title: link?.title, label: link?.getAttribute('aria-label'), expected: t('reading.openInJiten')};
        // Review: the card closes and both copies of the word lose the mark before Jiten answers.
        await __click('#pudgeStudyCard [data-pudge-study-review="good"]');
        out.reviewSent = {open: cardOpen(), gold: gold(), calls: __apiCalls.filter(call => call.name === 'study_action').length};
        window.finish({ok: false, message: 'Jiten is down'}); await __tick(50);
        out.reviewFailed = {gold: gold(), toasts: __toasts()};
        // Add (a deck chosen): the same, and the mark stays gone when Jiten confirms.
        await openCard('好', 1);
        await __input('#pudgeStudyDeck', '12');
        await __click('#pudgeStudyCard [data-pudge-study-add]');
        out.addSent = {open: cardOpen(), gold: gold()};
        window.finish({ok: true, outcome: 'confirmed'}); await __tick(50);
        PudgeReadingTools.study.refreshOptimalHighlights();          // e.g. more text rendered later
        out.added = gold();
        await __click('#pudgeStudyCard [data-pudge-study-close]').catch(() => {}); await __tick(10);
        // Meanwhile 猫 became mature on Jiten: opening its card shows that and removes its mark.
        window.__live['20:0'] = {states: ['mature'], normalizedState: 'learning'};
        await openCard('猫');
        out.mature = gold();
        return out;
        """,
        before=WORD_CARD,
    )
    out = result.value
    assert out["start"] == ["好", "好", "猫"]
    assert out["link"] == {
        "text": "↗",
        "title": out["link"]["expected"],
        "label": out["link"]["expected"],
        "expected": out["link"]["expected"],
    }
    assert out["reviewSent"] == {"open": False, "gold": ["猫"], "calls": 1}
    # A failed review gives the mark back and says why.
    assert (
        out["reviewFailed"]["gold"] == ["好", "好", "猫"] and "Jiten is down" in out["reviewFailed"]["toasts"]
    )
    assert out["addSent"] == {"open": False, "gold": ["猫"]}
    assert out["added"] == ["猫"]
    assert out["mature"] == []
    [review, add] = result.calls_of("study_action")
    assert (review[0]["action"], review[0]["grade"], review[0]["word_id"]) == ("review", "good", 10)
    assert (add[0]["action"], add[0]["deck_id"], add[0]["word_id"]) == ("add", "12", 10)


def test_jiten_link_in_the_card_header_is_a_small_square_button() -> None:
    link = css_rules("reading_tools.css").declarations(".pudge-study-jiten-link")
    assert (link["width"], link["height"], link["cursor"]) == ("26px", "26px", "pointer")
