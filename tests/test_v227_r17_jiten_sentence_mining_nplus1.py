from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from pudge.web_app import WebAppApi
from css_support import css_rules
from tests.i18n_support import en, ru


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    service = LightNovelService(cfg)
    service.save_settings({"jiten_api_key": "jiten-token", "study_backend": "jiten"})
    return service


def test_review_auto_adds_unassigned_word_and_enriches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    mutations: list[tuple[str, dict]] = []
    sentences: list[tuple[int, int, str]] = []
    images: list[tuple[int, int, bytes, str]] = []

    monkeypatch.setattr(
        service,
        "jiten_live_states",
        lambda pairs, *, force=False: [{"studyDeckIds": [], "states": ["new"]}],
    )
    monkeypatch.setattr(
        service,
        "_jiten_mutation",
        lambda action, payload=None: mutations.append((action, payload or {})) or {},
    )
    monkeypatch.setattr(
        service,
        "_jiten_add_custom_sentence",
        lambda word_id, reading_index, sentence: sentences.append((word_id, reading_index, sentence)),
    )
    monkeypatch.setattr(
        service,
        "_jiten_upload_card_image",
        lambda word_id, reading_index, image, *, filename="": images.append(
            (word_id, reading_index, image, filename)
        ),
    )
    monkeypatch.setattr(
        "pudge.light_novels.JitenReviewProvider.submit_review",
        lambda *_args, **_kwargs: {"ok": True, "outcome": "confirmed", "provider": "jiten"},
    )

    sentence = "前一 前二 前三 前四 前五 対象 後一 後二 後三 後四 後五。次の文。"
    result = service.study_action(
        "jiten",
        "review",
        120,
        2,
        grade="good",
        sentence=sentence,
        deck_id=77,
        attempt_id="r17-review-1",
        id_namespace="jiten",
        media_image=b"jpeg-data",
        media_filename="page.jpg",
        surface="対象",
    )

    assert mutations[0][0] == "srs/study-decks/77/words"
    assert mutations[0][1]["wordId"] == 120
    assert mutations[0][1]["readingIndex"] == 2
    assert mutations[0][1]["sentence"] == sentence
    assert sentences == [(120, 2, sentence.replace("対象", "**対象**"))]
    assert images == [(120, 2, b"jpeg-data", "page.jpg")]
    assert result["auto_added"] is True


def test_review_does_not_duplicate_word_already_in_any_jiten_deck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    mutations: list[tuple[str, dict]] = []
    enriched: list[str] = []
    monkeypatch.setattr(
        service,
        "jiten_live_states",
        lambda pairs, *, force=False: [{"studyDeckIds": [12], "states": ["new"]}],
    )
    monkeypatch.setattr(
        service,
        "_jiten_mutation",
        lambda action, payload=None: mutations.append((action, payload or {})) or {},
    )
    monkeypatch.setattr(service, "_jiten_add_custom_sentence", lambda *_args: enriched.append("sentence"))
    monkeypatch.setattr(
        service, "_jiten_upload_card_image", lambda *_args, **_kwargs: enriched.append("image")
    )
    monkeypatch.setattr(
        "pudge.light_novels.JitenReviewProvider.submit_review",
        lambda *_args, **_kwargs: {"ok": True, "outcome": "confirmed", "provider": "jiten"},
    )

    result = service.study_action(
        "jiten", "review", 120, 2, deck_id=77, sentence="context", id_namespace="jiten"
    )

    assert mutations == []
    assert enriched == []
    assert result["auto_added"] is False


def test_deck_add_metadata_does_not_exceed_jiten_150_char_sentence_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    mutations: list[tuple[str, dict]] = []
    custom_sentences: list[str] = []
    long_context = "文" * 200 + "猫" + "文" * 149
    monkeypatch.setattr(
        service,
        "jiten_live_states",
        lambda pairs, *, force=False: [{"studyDeckIds": [], "states": ["new"]}],
    )
    monkeypatch.setattr(
        service,
        "_jiten_mutation",
        lambda action, payload=None: mutations.append((action, payload or {})) or {},
    )
    monkeypatch.setattr(
        service, "_jiten_add_custom_sentence", lambda _w, _r, sentence: custom_sentences.append(sentence)
    )
    monkeypatch.setattr(
        "pudge.light_novels.JitenReviewProvider.submit_review",
        lambda *_args, **_kwargs: {"ok": True, "outcome": "confirmed", "provider": "jiten"},
    )

    service.study_action(
        "jiten", "review", 120, 2, deck_id=77, sentence=long_context, id_namespace="jiten", surface="猫"
    )

    assert len(mutations[0][1]["sentence"]) <= 150
    # Jiten keeps a custom sentence only with the word marked and within 150 characters.
    assert custom_sentences == ["文" * 72 + "**猫**" + "文" * 73]


def test_web_api_only_compresses_manga_page_for_word_missing_from_all_decks() -> None:
    class Settings:
        study_backend = "jiten"

    class LightNovels:
        def __init__(self, deck_ids):
            self.deck_ids = deck_ids
            self.last_kwargs = None

        def settings(self):
            return Settings()

        def jiten_live_states(self, pairs, *, force=False):
            return [{"studyDeckIds": list(self.deck_ids), "states": ["new"]}]

        def study_action(self, *args, **kwargs):
            self.last_kwargs = kwargs
            return {"ok": True}

    payload = {
        "backend": "jiten",
        "action": "review",
        "word_id": 120,
        "reading_index": 2,
        "deck_id": 77,
        "media_context": {"kind": "manga", "book_id": 5, "page_index": 3},
    }

    existing = WebAppApi.__new__(WebAppApi)
    existing.light_novels = LightNovels([12])
    existing._study_manga_card_image = lambda _ctx: pytest.fail(
        "existing deck word must not compress manga page"
    )
    assert existing.study_action(payload)["ok"] is True
    assert existing.light_novels.last_kwargs["media_image"] is None

    missing = WebAppApi.__new__(WebAppApi)
    missing.light_novels = LightNovels([])
    missing._study_manga_card_image = lambda _ctx: (b"jpeg", "page.jpg")
    assert missing.study_action(payload)["ok"] is True
    assert missing.light_novels.last_kwargs["media_image"] == b"jpeg"
    assert missing.light_novels.last_kwargs["media_filename"] == "page.jpg"


# Words of a reader paragraph; study_states answers from window.__live (held while window.__hold).
DECK_GUARD = r"""
window.__live = {}; window.__held = []; window.__hold = false;
__api.study_provider_capabilities = async () => ({account_key: 'jiten:me', configured: true});
__api.study_action = async payload => ({ok: true, outcome: 'confirmed'});
__api.study_states = payload => {
  const answer = () => ({ok: true, account_scope: 'jiten:me', states: payload.words.map(([w, r]) =>
    ({ok: true, wordId: w, readingIndex: r, ...window.__live[`${w}:${r}`]}))});
  return window.__hold ? new Promise(resolve => window.__held.push(() => resolve(answer()))) : Promise.resolve(answer());
};
"""


def test_reader_blocks_new_unassigned_review_until_a_jiten_deck_is_selected(page) -> None:
    assert en("reading.newWordChooseADeck") == "New word: choose a deck to add it to"
    assert ru("reading.newWordChooseADeck").startswith("Новое слово: выберите колоду")
    result = page.run(
        DECK_GUARD
        + """
        window.__live = {'1:0': {states: ['young'], normalizedState: 'learning', studyDeckIds: []},
                         '2:0': {states: ['new'], normalizedState: 'new', studyDeckIds: [12]},
                         '3:0': {states: ['new'], normalizedState: 'new', studyDeckIds: []}};
        // 猫's card comes from a cached parse that says "new, in no deck" (not batch-refreshed).
        const words = [['少し', 1, {states: ['young'], studyDeckIds: []}], ['猫', 2, null], ['犬', 3, {states: ['new'], studyDeckIds: []}]];
        let pos = 0;
        const tokens = words.map(([text, wordId]) => { const start = pos; pos += text.length;
          return {wordId, readingIndex: 0, start, end: pos, ...(wordId === 2 ? {card: {spelling: text, states: ['new'], studyDeckIds: []}} : {})}; });
        const vocabulary = words.filter(w => w[2]).map(([text, wordId, card]) => ({wordId, readingIndex: 0, spelling: text, ...card}));
        const box = document.createElement('div');
        box.innerHTML = PudgeReadingTools.study.renderParsedText({paragraphs: [words.map(w => w[0]).join('')], tokens: [tokens], vocabulary}, {backend: 'jiten'});
        document.body.appendChild(box);
        await __tick(100);
        const open = async text => {
          const node = [...box.querySelectorAll('[data-pudge-study-token]')].find(n => n.textContent === text);
          await __click(`[data-pudge-study-token="${node.dataset.pudgeStudyToken}"]`); await __tick(50);
        };
        const good = async () => { await __click('#pudgeStudyCard [data-pudge-study-review="good"]'); await __tick(50); };
        const button = () => { const b = document.querySelector('#pudgeStudyCard [data-pudge-study-review="good"]');
          return {disabled: b.getAttribute('aria-disabled'), unavailable: b.classList.contains('is-unavailable'), title: b.title}; };
        const reviews = () => __apiCalls.filter(c => c.name === 'study_action').map(c => [c.args[0].word_id, c.args[0].deck_id]);
        const out = {reason: t('reading.newWordChooseADeck')};
        await open('少し'); out.known = button(); await good();
        out.afterKnown = reviews();

        window.__hold = true;                  // the live refresh of 猫 is still on its way
        await open('猫'); await good();
        out.waiting = {reviews: reviews(), toasts: __toasts()};
        window.__hold = false; window.__held.splice(0).forEach(answer => answer()); await __tick(50);
        out.afterRefresh = reviews();

        await open('犬'); out.newWord = button(); await good();
        out.blocked = {reviews: reviews(), toasts: __toasts(), open: __visible('#pudgeStudyCard')};
        await __input('#pudgeStudyDeck', '12'); out.withDeck = button(); await good();
        out.afterDeck = reviews();
        return out;
        """,
        api={"study_decks": [{"id": 12, "name": "Deck A"}]},
    )
    out = result.value
    # A word already being learned is reviewed without a deck (Jiten reviews are shared across decks).
    assert out["known"]["disabled"] == "false" and out["known"]["unavailable"] is False
    assert out["afterKnown"] == [[1, ""]]
    # A cached "in no deck" is not trusted: the grade waits for the live refresh, which finds the word in a deck.
    assert out["waiting"] == {"reviews": [[1, ""]], "toasts": []}
    assert out["afterRefresh"] == [[1, ""], [2, ""]]
    # A new word in no deck: the grades are grey with the reason, a click explains and sends nothing...
    assert out["newWord"] == {"disabled": "true", "unavailable": True, "title": out["reason"]}
    assert out["blocked"] == {"reviews": out["afterRefresh"], "toasts": [out["reason"]], "open": True}
    # ...until a deck is chosen; the review then goes to that deck.
    assert out["withDeck"]["disabled"] == "false" and out["withDeck"]["unavailable"] is False
    assert out["afterDeck"][-1] == [3, "12"]


def test_card_media_upload_uses_jiten_api_key_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    captured: dict = {}

    class Response:
        status_code = 200
        content = b"{}"

        def json(self):
            return {}

    def fake_post(url, *, headers, files, timeout):
        captured.update(url=url, headers=headers, files=files, timeout=timeout)
        return Response()

    monkeypatch.setattr("pudge.light_novels.httpx.post", fake_post)
    service._jiten_upload_card_image(120, 2, b"jpeg", filename="page.jpg")

    assert captured["headers"]["Authorization"] == "ApiKey jiten-token"
    assert "Content-Type" not in captured["headers"]
    assert captured["files"]["file"][0] == "page.jpg"


def test_context_mining_keeps_at_least_five_words_on_each_side_and_nplus1_is_strict() -> None:
    tools_path = str(WEB / "reading_tools.js")
    script = f"""
    global.window=global;
    global.document={{addEventListener(){{}},querySelectorAll(){{return []}}}};
    require({json.dumps(tools_path)});
    const study=PudgeReadingTools.study;
    const words=['前1','前2','前3','前4','前5','対象','後1','後2','後3','後4','後5'];
    const text=words.join(' ')+'。';
    const tokens=[];
    let cursor=0;
    for(let i=0;i<words.length;i++){{
      const p=text.indexOf(words[i],cursor);cursor=p+words[i].length;
      tokens.push({{wordId:i+1,readingIndex:0,surface:words[i],sentence:text,contextStart:p,contextEnd:cursor,card:{{states:i===5?['new']:['mature'],frequencyRank:i===5?900:100,studyDeckIds:[]}}}});
    }}
    const mined=study.contextForToken(text,tokens[5],tokens);
    const classes=tokens.map(()=>new Set());
    const entries=tokens.map((token,i)=>({{token,node:{{classList:{{add(x){{classes[i].add(x)}},remove(x){{classes[i].delete(x)}}}}}}}}));
    const highlighted=study.applyOptimalHighlights(entries,{{enabled:true,frequencyLimit:1000}});
    process.stdout.write(JSON.stringify({{mined,highlighted,classes:classes.map(x=>[...x])}}));
    """
    result = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert "前1" in result["mined"] and "後5" in result["mined"]
    assert result["highlighted"] == 1
    assert "pudge-optimal-word" in result["classes"][5]
    assert all("pudge-optimal-word" not in row for i, row in enumerate(result["classes"]) if i != 5)


# A manga volume with OCR: two speech bubbles on page 1 (read right to left), parsed by Jiten.
# The second bubble is ten words: nine known and one new, frequent word (an N+1 sentence).
MANGA = r"""
const SENTENCE = '一二三四五六七八九好';
__api.manga_state = async () => ({books: [{id: 3, title: 'Vol 1', series_title: 'Manga', page_count: 2, position: 0, ocr_complete: true}], ocr_available: true});
__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 2, name: `p${index}.png`, spread: false, data_uri: 'data:image/png;base64,AAAA'});
__api.manga_text_regions = async (id, index) => ({book_id: id, page_index: index, available: true, cached: true,
  regions: index === 0 ? [{text: '前の吹き出し。', x: .1, y: .1, width: .2, height: .2}, {text: SENTENCE, x: .5, y: .5, width: .2, height: .2}] : []});
window.__parsed = text => text === SENTENCE
  ? {paragraphs: [text], tokens: [[...text].map((ch, i) => ({wordId: i + 1, readingIndex: 0, start: i, end: i + 1}))],
     vocabulary: [...text].map((ch, i) => i < 9
       ? {wordId: i + 1, readingIndex: 0, spelling: ch, states: ['mature'], frequencyRank: 100, studyDeckIds: [1]}
       : {wordId: i + 1, readingIndex: 0, spelling: ch, states: ['new'], frequencyRank: 300, studyDeckIds: []})}
  : {paragraphs: [text], tokens: [[]], vocabulary: []};
__api.study_parse_text = async text => window.__parsed(text);
__api.study_action = async () => ({ok: true, outcome: 'confirmed'});
"""


def test_manga_words_are_mined_with_their_page_and_never_get_the_n_plus_one_mark(page) -> None:
    result = page.run(
        """
        await __tick(100);
        await __settle(PudgeMangaReaderV2.openBook(3));
        await __tick(500);
        const reader = document.getElementById('mangaReaderV2');
        const out = {
          words: [...reader.querySelectorAll('[data-pudge-study-token]')].map(n => n.textContent),
          marked: reader.querySelectorAll('.pudge-optimal-word').length,
          settings: [...reader.querySelectorAll('[data-manga-setting]')].map(n => n.dataset.mangaSetting),
        };
        // The same sentence in a novel: 好 is the one unknown word, so it gets the N+1 mark.
        const novel = document.createElement('div');
        novel.innerHTML = PudgeReadingTools.study.renderParsedText(window.__parsed('一二三四五六七八九好'), {backend: 'jiten'});
        document.body.appendChild(novel);
        PudgeReadingTools.study.refreshOptimalHighlights();
        out.novelMarked = [...novel.querySelectorAll('.pudge-optimal-word')].map(n => n.textContent);
        const cat = reader.querySelector('[data-pudge-study-token]');
        await __click(`[data-pudge-study-token="${cat.dataset.pudgeStudyToken}"]`);
        await __tick(50);
        await __click('#pudgeStudyCard [data-pudge-study-review="good"]');
        await __tick(50);
        return out;
        """,
        state={"settings": {"review_gate_manga_enabled": False}},
        before=MANGA,
    )
    out = result.value
    assert "".join(out["words"]) == "一二三四五六七八九好"
    assert out["novelMarked"] == ["好"]
    assert out["marked"] == 0
    # The manga settings have no switch for the N+1 mark.
    assert out["settings"] and "highlightOptimalWords" not in out["settings"]
    # A review from a manga page carries the page (for the card image) and the page's text as context.
    [[review]] = result.calls_of("study_action")
    assert (review["word_id"], review["grade"]) == (1, "good")
    assert review["media_context"] == {"kind": "manga", "book_id": 3, "page_index": 0}
    assert "一二三四五六七八九好" in review["sentence"] and "前の吹き出し" in review["sentence"]


def test_nplus1_highlight_is_static_gold_without_animation() -> None:
    rules = css_rules("reading_tools.css")
    assert rules.get(".pudge-optimal-word", "background") == "rgba(255,210,76,.22)"
    for name in ("reading_tools.css", "index.html", "manga_reader_v2.css"):
        for _media, selector, decls in css_rules(name).rules:
            if "pudge-optimal-word" in selector:
                assert not [prop for prop in decls if prop.startswith("animation")], (name, selector)
