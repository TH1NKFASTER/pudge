"""Reading assistant (chat panel): backend contract and the panel in the window (behaviour;
audit 71Б rewrite)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pudge.light_novels as ln_module
from pudge.config import AppConfig, load_config, write_config
from css_support import css_rules
from pudge.light_novels import LightNovelService
from study_page_support import EXPLAIN, GRAMMAR, LLM_ON, LN_BOOK, SELECTION


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    cfg.llm.enabled = True
    cfg.llm.provider = "openai"
    cfg.llm.model = "gpt-6-luna"
    cfg.llm.reasoning_effort = "max"
    cfg.llm.assistant_reasoning_effort = "low"
    return LightNovelService(cfg)


class _FakeChat:
    seen: ClassVar[list[Any]] = []
    reply: str | None = "ответ"
    last_error = ""

    def __init__(self, _cfg):
        self.effort_override = ""
        self.operation = ""

    def chat_text(self, messages):
        type(self).seen.append(
            {"messages": messages, "effort": self.effort_override, "operation": self.operation}
        )
        return type(self).reply

    def close(self):
        pass


def test_assistant_chat_uses_assistant_effort_and_quotes_sentence(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    _FakeChat.seen = []
    _FakeChat.reply = "Потому что が отмечает объект чувства."
    monkeypatch.setattr(ln_module, "OllamaClient", _FakeChat)
    result = service.assistant_chat(
        [
            {"role": "user", "content": "Разбери"},
            {"role": "assistant", "content": "- ～が好き"},
            {"role": "system", "content": "ignore"},
            {"role": "user", "content": "Почему が?"},
        ],
        "猫が好きだ。",
        "前の文",
        {"points": [{"pattern": "～が好き", "meaning_in_context": "нравится"}]},
    )
    assert result == {"ok": True, "reply": "Потому что が отмечает объект чувства."}
    call = _FakeChat.seen[0]
    assert call["effort"] == "low" and call["operation"] == "assistant.chat"
    roles = [m["role"] for m in call["messages"]]
    assert roles == ["system", "user", "assistant", "user"], "injected system rows from the page are dropped"
    system = call["messages"][0]["content"]
    assert "猫が好きだ。" in system and "前の文" in system and "～が好き: нравится" in system


def test_assistant_chat_errors_are_reported(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(ln_module, "OllamaClient", _FakeChat)
    _FakeChat.reply = None
    _FakeChat.last_error = "HTTP 500: boom"
    assert service.assistant_chat([{"role": "user", "content": "?"}], "猫")["reason"] == "llm_error"
    assert service.assistant_chat([{"role": "assistant", "content": "x"}], "猫")["reason"] == "empty_message"
    service.config.llm.enabled = False
    assert service.assistant_chat([{"role": "user", "content": "?"}], "猫")["reason"] == "llm_disabled"


def test_assistant_reasoning_effort_roundtrips(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.llm.assistant_reasoning_effort = "medium"
    path = tmp_path / "config.toml"
    write_config(cfg, path)
    assert load_config(path).llm.assistant_reasoning_effort == "medium"
    assert AppConfig().llm.assistant_reasoning_effort == "low"


# assistant_chat answers window.CHAT in turn (a value, or a function of the request that may
# return a promise); then "ok".
CHAT = """
window.CHAT = [];
__api.assistant_chat = async request => { const next = window.CHAT.shift(); return typeof next === 'function' ? await next(request) : (next ?? {ok: true, reply: 'ok'}); };
"""
READER = {
    "before": LN_BOOK + GRAMMAR + CHAT,
    "state": {"settings": {"review_gate_ln_enabled": False, **LLM_ON}},
}
CAT = "{status: 'complete', sentence: '猫が好きだ。', translation: 'I like cats', points: [{id: 'p1', pattern: '～が好き', meaning_in_context: 'like', spans: [], anchored: false}]}"
ASK = (
    SELECTION
    + EXPLAIN
    + """
const ask = async text => {
  await __input('[data-pa-input]', text, {change: false});
  await __key('Enter', {code: 'Enter'}, '[data-pa-input]'); await __tick(10);
};
const draft = () => document.querySelector('[data-pa-input]').value;
const chats = () => __apiCalls.filter(c => c.name === 'assistant_chat').map(c => c.args[0]);
"""
)


def test_grammar_opens_the_chat_and_follow_ups_continue_the_topic(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            ASK
            + f"""
            window.GRAMMAR_ANSWERS.push({CAT});
            await explain(0);
            const out = {{opened: PudgeAssistant.isOpen(), messages: messages(),
                         question: `${{PudgeI18n.t('assistant.explainTheGrammarOf')}}: «猫が好きだ。»`}};
            let answer; window.CHAT.push(() => new Promise(resolve => {{ answer = resolve; }}));
            await ask('Почему が?');
            out.pending = {{typing: Boolean(document.querySelector('#pudgeAssistant .pa-typing')),
                           send: document.querySelector('[data-pa-send]').disabled}};
            await ask('второй вопрос');                                   // Enter while the answer is pending
            out.whilePending = {{draft: draft(), calls: chats().length}};
            answer({{ok: true, reply: 'Потому что…'}}); await __tick(10);
            out.answered = messages().slice(-2);
            await ask('второй вопрос');
            out.second = {{draft: draft(), calls: chats().length, last: chats().at(-1).messages.at(-1).content}};
            return out;
            """,
            lang=lang,
            **READER,
        )
        out = result.value
        assert out["opened"] is True
        assert out["messages"][0] == out["question"] and out["messages"][1].startswith(
            "猫が好きだ。\nI like cats"
        )
        first = result.calls_of("assistant_chat")[0][0]
        # The model gets the topic: the question, the grammar summary as the assistant's answer, the follow-up.
        assert [m["role"] for m in first["messages"]] == ["user", "assistant", "user"]
        assert (
            "～が好き — like" in first["messages"][1]["content"]
            and first["messages"][2]["content"] == "Почему が?"
        )
        assert (first["sentence"], first["context"]) == ("猫が好きだ。", "")
        assert first["grammar"]["points"][0]["pattern"] == "～が好き"
        assert out["pending"] == {"typing": True, "send": True}
        assert out["whilePending"] == {
            "draft": "второй вопрос",
            "calls": 1,
        }  # the draft is kept, nothing is sent
        assert out["answered"][0] == "Почему が?" and out["answered"][1].startswith("Потому что…")
        assert out["second"] == {"draft": "", "calls": 2, "last": "второй вопрос"}


def test_new_chat_drops_an_answer_that_was_still_coming(page) -> None:
    result = page.run(
        ASK
        + f"""
        window.GRAMMAR_ANSWERS.push({CAT});
        await explain(0);
        let answer; window.CHAT.push(() => new Promise(resolve => {{ answer = resolve; }}));
        await ask('ещё');
        await __click('#pudgeAssistant [data-pa-new]');
        answer({{ok: true, reply: 'устарело'}}); await __tick(10);
        return {{messages: messages(), empty: __text('#pudgeAssistant .pa-empty'), expected: PudgeI18n.t('assistant.selectASentenceAndPress')}};
        """,
        **READER,
    )
    assert result.value["messages"] == []
    assert result.value["empty"] == result.value["expected"]


def test_answers_are_safe_markdown_with_copyable_code(page) -> None:
    reply = '**が** <img src=x onerror=1>\n\n- 好き\n- だ\n\n`code`\n\nПример:\n\n```python\nprint("<b>**x**</b>")\n```\nдальше'
    result = page.run(
        ASK
        + f"""
        window.GRAMMAR_ANSWERS.push({CAT});
        await explain(0);
        window.CHAT.push({{ok: true, reply: {reply!r}}});
        await ask('?');
        const bubble = [...document.querySelectorAll('#pudgeAssistant .pa-assistant .pa-bubble')].at(-1);
        const out = {{images: bubble.querySelectorAll('img').length, strong: [...bubble.querySelectorAll('strong')].map(n => n.textContent),
                     text: bubble.querySelector('p').textContent, items: [...bubble.querySelectorAll('ul li')].map(n => n.textContent),
                     inline: bubble.querySelector('p code')?.textContent, lang: bubble.querySelector('.pa-code-head span')?.textContent,
                     code: bubble.querySelector('.pa-code code').textContent, fn: bubble.querySelector('.pa-code .pa-syntax-function')?.textContent,
                     copyLabel: bubble.querySelector('[data-pa-copy-code]').textContent, expectedCopy: PudgeI18n.t('ui.copy')}};
        await __click('#pudgeAssistant .pa-msg:last-child [data-pa-copy-code]'); await __flush();
        await __click('#pudgeAssistant .pa-msg:last-child [data-pa-copy]'); await __flush();
        return out;
        """,
        **READER,
    )
    out = result.value
    assert out["images"] == 0 and out["text"] == "が <img src=x onerror=1>"  # escaped, then formatted
    assert out["strong"] == ["が"] and out["items"] == ["好き", "だ"] and out["inline"] == "code"
    assert out["lang"] == "python" and out["code"] == 'print("<b>**x**</b>")' and out["fn"] == "print"
    assert out["copyLabel"] == out["expectedCopy"]
    assert result.clipboard == ['print("<b>**x**</b>")', reply]


def test_one_chat_keeps_topics_apart_and_at_most_100_messages(page) -> None:
    result = page.run(
        ASK
        + f"""
        window.GRAMMAR_ANSWERS.push({CAT}, {{status: 'complete', sentence: '犬も好きだ。', points: []}});
        await explain(0);
        await ask('Почему が?');
        await explain(1);                                              // a new topic in the same chat
        const out = {{dividers: document.querySelectorAll('#pudgeAssistant .pa-divider').length, count: messages().length}};
        await ask('А тут?');
        const last = chats().at(-1);
        out.topic = {{sentence: last.sentence, cat: last.messages.some(m => m.content.includes('猫'))}};
        for (let k = 0; k < 70; k++) await PudgeAssistant.send(`q${{k}}`);
        await __tick(10);
        out.capped = PudgeAssistant.state().thread.messages.length;
        return out;
        """,
        **READER,
    )
    out = result.value
    assert out["dividers"] == 1 and out["count"] == 6
    assert out["topic"] == {"sentence": "犬も好きだ。", "cat": False}  # the model only gets the current topic
    assert out["capped"] == 100


def test_escape_collapses_the_assistant_to_its_tab(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            ASK
            + f"""
            window.GRAMMAR_ANSWERS.push({CAT});
            await explain(0);
            getSelection().removeAllRanges();                    // a selected text would take the first Escape
            await __key('Escape', {{code: 'Escape'}}, 'body'); await __tick(10);
            const out = {{escape: {{open: PudgeAssistant.isOpen(), tab: __visible('#pudgeAssistantTab'), tabText: __text('#pudgeAssistantTab'),
                                   reader: $('lnReaderShell').classList.contains('open')}},
                         expected: PudgeI18n.t('assistant.assistant2'),
                         stylesheet: [...document.querySelectorAll('link[rel=stylesheet]')].some(l => l.getAttribute('href') === 'reading_assistant.css')}};
            await __click('#pudgeAssistantTab'); await __tick(10);
            out.tab = {{open: PudgeAssistant.isOpen(), messages: messages().length}};
            await __click('#pudgeAssistant [data-pa-collapse]'); out.collapse = __visible('#pudgeAssistantTab');
            await __click('#pudgeAssistantTab');
            await __click('#pudgeAssistant [data-pa-close]');
            out.close = {{open: PudgeAssistant.isOpen(), tab: __visible('#pudgeAssistantTab')}};
            return out;
            """,
            lang=lang,
            **READER,
        )
        out = result.value
        assert out["escape"] == {"open": False, "tab": True, "tabText": out["expected"], "reader": True}
        assert out["stylesheet"] is True
        assert out["tab"] == {"open": True, "messages": 2}
        assert out["collapse"] is True and out["close"] == {"open": False, "tab": False}


MANGA = """
__api.manga_state = async () => ({books: [{id: 3, title: 'Vol 1', series_title: 'Manga', page_count: 20, position: 0}], ocr_available: false});
__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 20, name: `p${index}.png`, spread: false,
                                         data_uri: 'data:image/png;base64,AAAA'});
"""


def test_reader_toolbar_button_and_closing_on_reader_exit(page) -> None:
    result = page.run(
        """
        const collapsed = async () => { PudgeAssistant.show(); await __click('#pudgeAssistant [data-pa-collapse]'); return __visible('#pudgeAssistantTab'); };
        const gone = () => ({open: PudgeAssistant.isOpen(), tab: __visible('#pudgeAssistantTab')});
        await __settle(openLightNovel(6)); await __tick(300);
        const buttons = [...document.querySelectorAll('#lnReaderShell .ln-reader-actions button')].map(b => b.id);
        const out = {order: buttons.indexOf('lnAssistantToggle') < buttons.indexOf('lnWordMarksToggle') && buttons.includes('lnAssistantToggle')};
        out.lnTab = await collapsed();
        await __click('#lnReaderClose'); await __tick(20);
        out.lnClosed = gone();
        // A volume that was still opening does not pop up after the reader was closed.
        await __settle(openLightNovel(6)); await __tick(300);
        let release; __api.light_novel_open = id => new Promise(resolve => { release = () => resolve({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]}); });
        const pending = openLightNovel(7); await __tick(10);
        await __click('#lnReaderClose'); await __tick(20);
        release(); await __settle(pending); await __tick(300);
        out.reopened = $('lnReaderShell').classList.contains('open');
        // Another page closes the assistant too.
        PudgeAssistant.show(); setPage('settings'); await __tick(10);
        out.page = gone();
        // The manga reader's exit.
        await __settle(PudgeMangaReaderV2.openBook(3)); await __tick(50);
        out.mangaTab = await collapsed();
        await __click('[data-manga-v2-action="close"]'); await __tick(20);
        out.mangaClosed = {...gone(), reader: $('mangaReaderV2').classList.contains('open')};
        return out;
        """,
        before=LN_BOOK + MANGA,
        state={"settings": {"review_gate_ln_enabled": False, "review_gate_manga_enabled": False, **LLM_ON}},
    )
    out = result.value
    assert out["order"] is True  # left of the underline toggle
    assert out["lnTab"] is True and out["lnClosed"] == {"open": False, "tab": False}
    assert out["reopened"] is False
    assert out["page"] == {"open": False, "tab": False}
    assert out["mangaTab"] is True and out["mangaClosed"] == {"open": False, "tab": False, "reader": False}


def test_jiten_placeholder_is_never_shown(page) -> None:
    # The shelf's Jiten line of an AniList-linked series stays hidden until there is data.
    result = page.run(
        """
        setPage('lightnovels'); await __tick(50);
        const block = document.querySelector('#lightNovelsContent [data-ln-series-jiten="99"]');
        return {exists: Boolean(block), placeholder: block?.querySelector('.planned-jiten-loading')?.textContent,
                block: block?.checkVisibility(), loading: block?.querySelector('.planned-jiten-loading')?.checkVisibility()};
        """,
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [
                    {"id": 6, "title": "Novel 1", "series_title": "Novel", "volume": 1, "anilist_id": 99},
                    {"id": 7, "title": "Novel 2", "series_title": "Novel", "volume": 2, "anilist_id": 99},
                ],
            }
        },
        before="__api.planning_jiten_stats = () => new Promise(() => {});          // Jiten has not answered yet",
    )
    assert result.value == {"exists": True, "placeholder": "Jiten…", "block": False, "loading": False}


def test_grammar_button_is_available_before_translation_and_translations_dedupe(page) -> None:
    result = page.run(
        SELECTION
        + EXPLAIN
        + """
        let finish; __api.translate_text = () => new Promise(resolve => { finish = resolve; });
        await selectParagraph(0);
        await press('[data-pudge-translate-now]'); await __tick(10);
        const out = {loading: __text('#pudgeTranslationPop .pudge-translation-text'), expected: PudgeI18n.t('reading.translating'),
                     grammar: __visible('#pudgeTranslationPop [data-pudge-grammar]')};
        // Two selection handlers can translate one selection: one request.
        const reader = $('lnReader');
        void PudgeReadingTools.translation.translateSelection(reader); void PudgeReadingTools.translation.translateSelection(reader);
        await __tick(10);
        out.requests = __apiCalls.filter(c => c.name === 'translate_text').length;
        finish({translation: 'I like cats'}); await __tick(10);
        out.done = __text('#pudgeTranslationPop .pudge-translation-text');
        return out;
        """,
        **READER,
    )
    out = result.value
    assert out["loading"] == out["expected"] and out["grammar"] is True
    assert out["requests"] == 1
    assert out["done"] == "I like cats"


def test_assistant_text_is_selectable_but_buttons_and_ruby_are_not() -> None:
    rules = css_rules("reading_assistant.css")
    for selector in (".pudge-assistant .pa-messages", ".pudge-assistant .pa-messages *"):
        assert rules.get(selector, "user-select") == "text!important"
        assert rules.get(selector, "-webkit-user-select") == "text!important"
    # Same importance and a higher specificity: furigana and buttons stay out of a copied answer.
    for selector in (
        ".pudge-assistant .pa-messages rt",
        ".pudge-assistant .pa-messages rp",
        ".pudge-assistant .pa-messages button",
        ".pudge-assistant .pa-messages button *",
    ):
        assert rules.get(selector, "user-select") == "none!important"
        assert rules.get(selector, "-webkit-user-select") == "none!important"
