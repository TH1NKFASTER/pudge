"""Reading tools shared by light novels and manga, used from the manga reader: study cards for
OCR words, selection translation, non-blocking parsing, zoom and the hidden toolbar
(behaviour; the reader runs in the page harness, audit 71Б rewrite)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from css_support import css_rules
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.manga import MangaService
from pudge.web_app import WebAppApi
from study_page_support import SELECTION
from t71g03_support import MANGA, READER

NO_GATE = {"settings": {"review_gate_manga_enabled": False}}


def test_a_manga_word_opens_the_shared_study_card_and_a_selection_offers_translation(page) -> None:
    result = page.run(
        READER
        + SELECTION
        + r"""
        await open(3);
        layout();
        const word = document.querySelector('.manga-v2-region-content [data-pudge-study-token]');
        const out = {word: word && {text: word.textContent, state: word.className}};
        // The bubble (page x 50-70 %, y 30-50 %) is clicked where the word is.
        await __click('.manga-v2-text-region .manga-v2-selection-content', {clientX: 240, clientY: 240, detail: 1}); await __tick(50);
        const card = document.getElementById('pudgeStudyCard');
        out.card = card && {open: card.classList.contains('open'), term: card.querySelector('.pudge-study-term')?.textContent,
                            meanings: [...card.querySelectorAll('.pudge-study-meanings li')].map(li => li.textContent)};
        await __input('#pudgeStudyDeck', '12'); await __tick(10);
        await __click('#pudgeStudyCard [data-pudge-study-add]'); await __tick(50);
        // Selecting bubble text offers a translation; T / the button translates it.
        select(document.querySelector('.manga-v2-text-region .manga-v2-selection-content'));
        await __fire('.manga-v2-text-region .manga-v2-selection-content', 'mouseup', {button: 0}, 'MouseEvent'); await __tick(10);
        await press('[data-pudge-translate-now]'); await __tick(20);
        out.translation = __text('#pudgeTranslationPop .pudge-translation-text');
        return out;
        """,
        before=MANGA + "__api.translate_text = async () => ({translation: 'Cat.'});",
        state=NO_GATE,
    )
    out = result.value
    # The OCR text of the bubble is parsed and shown as study words with their Jiten state.
    assert result.calls_of("study_parse_text") == [["猫"]]
    assert out["word"] == {"text": "猫", "state": "pudge-study-word state-new"}
    assert (
        out["card"]
        and out["card"]["open"] is True
        and out["card"]["meanings"] == ["cat"]
        and "猫" in out["card"]["term"]
    )
    assert result.calls_of("study_decks") == [["jiten"]]
    [[added]] = result.calls_of("study_action")
    assert (added["action"], added["word_id"], added["deck_id"]) == ("add", 101, "12")
    assert added["media_context"] == {"kind": "manga", "book_id": 3, "page_index": 0}
    # The translation is asked in the interface language, with the series for its name glossary.
    assert result.calls_of("translate_text") == [["猫", "猫", "en", 77]]
    assert out["translation"] == "Cat."


def test_bubble_text_shows_at_once_and_bubbles_are_parsed_one_at_a_time(page) -> None:
    result = page.run(
        READER
        + r"""
        window.MANGA.regions = () => [{text: '猫だ', x: .6, y: .5, width: .1, height: .3, orientation: 'vertical'},
                                      {text: '犬だ', x: .2, y: .5, width: .1, height: .3, orientation: 'vertical'}];
        const parse = __api.study_parse_text;
        window.parses = [];
        __api.study_parse_text = text => new Promise(resolve => window.parses.push(() => resolve(parse(text))));
        await open(3);
        const words = () => [...document.querySelectorAll('.manga-v2-text-region')].map(r =>
          [...r.querySelectorAll('.manga-v2-region-content [data-pudge-study-token]')].map(w => w.textContent));
        const out = {text: pages()[0].regions, words: words(), asked: callsOf('study_parse_text').length};
        window.parses.shift()(); await __tick(20);
        out.first = {words: words(), asked: callsOf('study_parse_text').length};
        window.parses.shift()(); await __tick(20);
        out.second = words();
        return out;
        """,
        before=MANGA,
        state=NO_GATE,
    )
    out = result.value
    # The plain OCR text is there before any parse answer, and one bubble is parsed at a time.
    assert out["text"] == ["猫だ", "犬だ"] and out["words"] == [[], []] and out["asked"] == 1
    assert out["first"] == {"words": [["猫"], []], "asked": 2}
    assert out["second"] == [["猫"], ["犬"]]


def test_zoom_by_keys_ctrl_wheel_and_pinch_and_a_hidden_toolbar_can_be_brought_back(page) -> None:
    result = page.run(
        READER
        + r"""
        await open(3);
        const zoom = () => __text('#mangaV2ZoomValue');
        const out = {start: zoom()};
        await __key('+', {}, 'body'); out.plus = zoom();
        await __key('0', {}, 'body'); out.reset = zoom();
        await __fire('#mangaV2Viewport', 'wheel', {deltaY: -100}, 'WheelEvent'); out.plainWheel = zoom();
        await __fire('#mangaV2Viewport', 'wheel', {deltaY: -100, ctrlKey: true}, 'WheelEvent'); out.ctrlWheel = zoom();
        await __key('0', {}, 'body');
        await __fire('#mangaReaderV2', 'gesturestart', {});
        const change = new Event('gesturechange', {bubbles: true, cancelable: true}); change.scale = 1.5;
        reader().dispatchEvent(change); await __tick(20);
        await __fire('#mangaReaderV2', 'gestureend', {});
        out.pinch = zoom();
        out.saved = JSON.parse(localStorage.getItem('pudge.manga.reader.v2')).zoom;
        // T hides the toolbar; a small handle and the top edge bring it back.
        await __key('t', {}, 'body'); await __tick(20);
        const bar = () => ({hidden: reader().classList.contains('toolbar-hidden'), peek: reader().classList.contains('toolbar-peek'),
                            handle: __visible('#mangaV2ToolbarReveal')});
        out.hidden = bar();
        await __fire('#mangaReaderV2', 'pointermove', {clientX: 300, clientY: 8}, 'PointerEvent'); await __tick(20);
        out.peek = bar();
        await __tick(2000);
        out.peekOver = bar();
        await __click('#mangaV2ToolbarReveal'); await __tick(20);
        out.shown = bar();
        return out;
        """,
        before=MANGA,
        state=NO_GATE,
    )
    out = result.value
    assert (out["start"], out["plus"], out["reset"]) == ("100%", "110%", "100%")
    # A plain wheel scrolls; Ctrl (pinch on a trackpad), Cmd or Alt with the wheel zooms.
    assert (out["plainWheel"], out["ctrlWheel"]) == ("100%", "110%")
    assert out["pinch"] == "150%" and out["saved"] == 150
    assert out["hidden"] == {"hidden": True, "peek": False, "handle": True}
    assert out["peek"] == {"hidden": True, "peek": True, "handle": False}
    assert out["peekOver"] == {"hidden": True, "peek": False, "handle": True}
    assert out["shown"] == {"hidden": False, "peek": False, "handle": False}
    rules = css_rules("manga_reader_v2.css")
    # The hidden toolbar takes no clicks; while it peeks it does.
    assert rules.get(".manga-v2-reader.toolbar-hidden .manga-v2-toolbar", "pointer-events") == "none"
    assert (
        rules.get(".manga-v2-reader.toolbar-hidden.toolbar-peek .manga-v2-toolbar", "pointer-events")
        == "auto"
    )


def test_manga_service_purges_legacy_whole_page_ocr(tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,position,reading_direction,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(tmp_path / "book.cbz"), "book", 3, 0, "rtl", 1.0, 1.0),
        )
        book_id = int(conn.execute("SELECT id FROM manga_books").fetchone()[0])
        conn.execute(
            "INSERT INTO manga_ocr_cache(book_id,page_index,region_key,text,updated_at) VALUES(?,1,'full',?,?)",
            (book_id, "obsolete whole page", 1.0),
        )

    service = MangaService(db, cache_dir=tmp_path / "cache")

    with db.connect() as conn:
        legacy_count = int(
            conn.execute("SELECT COUNT(*) FROM manga_ocr_cache WHERE region_key='full'").fetchone()[0]
        )
    assert legacy_count == 0
    assert not hasattr(service, "cached_ocr_page")
    assert not hasattr(service, "ocr_page")


class _ReadingService:
    """The light-novel service as the generic reading API sees it."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def settings(self):
        return SimpleNamespace(study_backend="jiten")

    def parse_study_text(self, text):
        self.calls.append(("parse", text))
        return {"text": text, "vocabulary": [{"wordId": 1, "readingIndex": 0}]}

    def cache_jiten_words(self, words):
        self.calls.append(("cache", len(words)))

    def decks(self, backend):
        self.calls.append(("decks", backend))
        return [{"id": "12", "name": "Mining"}]

    def study_action(self, backend, action, word_id, reading_index, **kwargs):
        self.calls.append(
            ("action", backend, action, word_id, reading_index, kwargs["deck_id"], kwargs["sentence"])
        )
        return {"ok": True}


def test_generic_reading_api_serves_manga_through_the_light_novel_service() -> None:
    api = WebAppApi.__new__(WebAppApi)
    api.light_novels = service = _ReadingService()
    assert api.study_parse_text("猫")["text"] == "猫"
    assert api.study_decks("jiten") == [{"id": "12", "name": "Mining"}]
    assert api.study_action(
        {
            "backend": "jiten",
            "action": "add",
            "word_id": 5,
            "reading_index": 0,
            "deck_id": "12",
            "sentence": "猫だ",
        }
    ) == {"ok": True}
    assert service.calls == [
        ("parse", "猫"),
        ("cache", 1),
        ("decks", "jiten"),
        ("action", "jiten", "add", 5, 0, "12", "猫だ"),
    ]


def test_translation_of_a_manga_selection_uses_the_reading_prompt_and_the_series_glossary(
    tmp_path: Path, monkeypatch
) -> None:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "db.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    config.llm.enabled, config.llm.base_url, config.llm.model = True, "http://127.0.0.1:11434", "local-model"
    config.ui.language = "ru"  # the app language decides the target language
    api = WebAppApi.__new__(WebAppApi)
    api.light_novels = LightNovelService(config)
    api.light_novels.save_character_glossary_override(77, "猫田", "Nekota")
    sent: list[dict] = []

    def post(url, **kwargs):
        sent.append(kwargs["json"])
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"message": {"content": json.dumps({"translation": "Nekota is a cat."})}},
        )

    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    monkeypatch.setattr(httpx, "post", post)

    result = api.translate_text("猫田は猫だ。", "前の吹き出し", "ru", 77)

    assert result["translation"] == "Nekota is a cat."
    system, user = (message["content"] for message in sent[0]["messages"])
    assert "light novels and manga" in system and "into Russian" in system
    assert "猫田 = Nekota" in user and user.endswith("SELECTED TEXT:\n猫田は猫だ。")
