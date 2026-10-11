"""Shared window set-ups for the study tests (review gate, word card, reader tools,
reading assistant); audit 71Б. This module has no tests of its own.

Each constant is JavaScript for `page.run(before=...)` or the start of a `page.run`
body (see tests/page_harness.py)."""

from __future__ import annotations

import re

from css_support import CssRules, css_rules

# An LLM that the window considers usable (llmAvailable): grammar and the assistant.
LLM_ON = {"llm_enabled": True, "llm_model": "test-model", "llm_url": "http://127.0.0.1:11434"}

# Light novel 6 "Novel", one chapter of two paragraphs with Jiten tokens
# (猫 が 好き だ。 / 犬 も 好き だ。). before= code.
LN_BOOK = r"""
window.LN_VOCAB = [
  {wordId: 101, readingIndex: 0, spelling: '猫', reading: 'ねこ', meaningsChunks: [['cat']], cardState: ['new']},
  {wordId: 102, readingIndex: 0, spelling: 'が', reading: 'が', meaningsChunks: [['subject marker']], cardState: ['known']},
  {wordId: 103, readingIndex: 0, spelling: '好き', reading: 'すき', meaningsChunks: [['liked']], cardState: ['learning']},
  {wordId: 104, readingIndex: 0, spelling: '犬', reading: 'いぬ', meaningsChunks: [['dog']], cardState: ['new']},
];
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0,
  chapters: [{chapter_index: 0, title: 'One'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index,
  paragraphs: ['猫が好きだ。', '犬も好きだ。'],
  tokens: [[{start: 0, end: 1, wordId: 101, readingIndex: 0}, {start: 1, end: 2, wordId: 102, readingIndex: 0},
            {start: 2, end: 4, wordId: 103, readingIndex: 0}],
           [{start: 0, end: 1, wordId: 104, readingIndex: 0}, {start: 2, end: 4, wordId: 103, readingIndex: 0}]],
  vocabulary: window.LN_VOCAB});
"""

# Text selection in the page. dom_lite has no Range.cloneContents (the reader and the
# translation popup read the selected text through it): a small version for the test.
# select(node) selects the node's contents; press(sel) is a real mouse press
# (pointerdown, mousedown, mouseup, click). Start of a run body.
SELECTION = r"""
const RangeProto = Object.getPrototypeOf(document.createRange());
RangeProto.cloneContents = function () {
  const path = node => { const out = []; for (let n = node; n.parentNode; n = n.parentNode) out.unshift(n.parentNode.childNodes.indexOf(n)); return out; };
  const cmp = (a, b) => { for (let k = 0; k < Math.min(a.length, b.length); k++) if (a[k] !== b[k]) return a[k] - b[k]; return a.length - b.length; };
  const start = [...path(this.startContainer), this.startOffset], end = [...path(this.endContainer), this.endOffset];
  const clip = node => {
    if (node.nodeType === 3) {
      const at = path(node), len = node.data.length;
      const s = node === this.startContainer ? this.startOffset : (cmp([...at, 0], start) >= 0 ? 0 : len);
      const e = node === this.endContainer ? this.endOffset : (cmp([...at, len], end) <= 0 ? len : 0);
      return e > s ? document.createTextNode(node.data.slice(s, e)) : null;
    }
    const copy = node.cloneNode(false);
    for (const child of node.childNodes) { const part = clip(child); if (part) copy.appendChild(part); }
    return copy.childNodes.length ? copy : null;
  };
  const box = document.createElement('div');
  const root = this.commonAncestorContainer;
  for (const child of (root.nodeType === 3 ? [root] : root.childNodes)) { const part = clip(child); if (part) box.appendChild(part); }
  return box;
};
const select = node => { const range = document.createRange(); range.selectNodeContents(node); const s = getSelection(); s.removeAllRanges(); s.addRange(range); };
const press = async selector => {
  await __fire(selector, 'pointerdown', {button: 0}, 'PointerEvent');
  for (const type of ['mousedown', 'mouseup']) await __fire(selector, type, {button: 0}, 'MouseEvent');
  await __click(selector);
};
// Select a reader paragraph with the mouse: the reader offers a translation.
const selectParagraph = async index => {
  const selector = `#lnReader [data-ln-paragraph="${index}"]`;
  select(document.querySelector(selector));
  await __fire(selector, 'mouseup', {button: 0}, 'MouseEvent');
  await __tick(10);
};
const translationPop = () => document.getElementById('pudgeTranslationPop');
"""

# Jiten review cards as the backend passes them on to the review gate (reading rows:
# kana text + bracket ruby, as in tests/test_v227_r5_review_gate_card_ux.py).
CARD = r"""
const reviewCard = (id, word, reading, extra = {}) => ({
  wordId: id, readingIndex: 0, wordText: word, wordTextPlain: word,
  readings: [{text: reading || word, rubyText: reading ? `${word}[${reading}]` : word, readingIndex: 0}],
  definitions: [{meanings: [`meaning ${id}`]}],
  intervalPreview: {againSeconds: 60, hardSeconds: 600, goodSeconds: 86400, easySeconds: 345600},
  ...extra,
});
"""

# Watching "/lib/Show - 03.mkv" needs `window.GATE_CARDS.length` reviews first. The fake
# backend counts confirmed reviews; window.HOLD_REVIEW = true keeps the next review answer
# pending until window.releaseReview() (a slow network). before= code.
GATE = (
    CARD
    + r"""
window.EPISODE = '/lib/Show - 03.mkv';
window.GATE_CARDS = [reviewCard(1, '猫', 'ねこ'), reviewCard(2, '犬', 'いぬ')];
window.GATE_DONE = 0;
const gateStatus = () => ({supported: true, blocking: window.GATE_DONE < window.GATE_CARDS.length, granted: window.GATE_DONE >= window.GATE_CARDS.length,
  required: window.GATE_CARDS.length, completed: window.GATE_DONE, remaining: window.GATE_CARDS.length - window.GATE_DONE, episode: 3});
__api.review_gate_status = async () => gateStatus();
__api.review_gate_begin = async () => ({...gateStatus(), cards: window.GATE_CARDS.slice(window.GATE_DONE)});
__api.review_gate_review = async () => {
  if (window.HOLD_REVIEW) await new Promise(resolve => { window.releaseReview = resolve; });
  window.GATE_DONE += 1;
  return {ok: true, outcome: 'confirmed', ...gateStatus()};
};
__api.review_gate_undo = async () => { window.GATE_DONE -= 1; return {ok: true, outcome: 'undone', ...gateStatus()}; };
__api.play = async () => ({ok: true, status: 'starting'});
"""
)

# Run-body helpers for the open gate.
GATE_VIEW = r"""
const gate = () => document.getElementById('pudgeReviewGate');
const shown = selector => { const el = gate()?.querySelector(selector); return Boolean(el && el.checkVisibility()); };
const frontWord = () => gate().querySelector('.pudge-review-gate-front .pudge-review-gate-word');
const space = () => __key(' ', {code: 'Space'}, 'body');
const gradeKey = digit => __key(String(digit), {code: `Digit${digit}`}, 'body');
"""


# ---- stylesheet values as the window computes them (for sizes the page harness has no layout for)


def _media_applies(media: str, width: int) -> bool:
    query = media.lower()
    if re.search(r"print|prefers-|hover|pointer", query):
        return False
    if any(width > int(n) for n in re.findall(r"max-width\s*:\s*(\d+)px", query)):
        return False
    return not any(width < int(n) for n in re.findall(r"min-width\s*:\s*(\d+)px", query))


def cascade(sheet: str, selector: str, width: int = 1300) -> dict[str, str]:
    """Declarations for `selector` in `sheet` at a window `width`: rules in source order,
    @media by width, `!important` wins (the value is returned without it)."""
    out: dict[str, str] = {}
    strong: set[str] = set()
    for media, selectors, declarations in css_rules(sheet).rules:
        if media and not _media_applies(media, width):
            continue
        if not CssRules([("", selectors, {"matches": "1"})]).declarations(selector):
            continue
        for prop, value in declarations.items():
            important = value.replace(" ", "").endswith("!important")
            if prop in strong and not important:
                continue
            out[prop] = re.sub(r"\s*!\s*important$", "", value).strip()
            if important:
                strong.add(prop)
    return out


def css_length(value: str, variables: dict[str, str], *, width: int, height: int) -> float:
    """A length in px: var(), calc(), clamp(), min(), max() with px / vh / vw."""
    expr = value
    for _ in range(10):
        expr = re.sub(r"var\((--[\w-]+)\)", lambda m: f"({variables[m.group(1)]})", expr)
    expr = expr.replace("calc(", "(")
    expr = re.sub(r"(\d+(?:\.\d+)?)vh", lambda m: f"({m.group(1)}*{height}/100)", expr)
    expr = re.sub(r"(\d+(?:\.\d+)?)vw", lambda m: f"({m.group(1)}*{width}/100)", expr)
    expr = re.sub(r"(\d+(?:\.\d+)?)px", r"\1", expr)
    if not re.fullmatch(r"[\d\s.+\-*/(),a-z]*", expr):
        raise ValueError(f"not a length: {value!r}")
    return float(
        eval(expr, {"__builtins__": {}}, {"clamp": lambda a, b, c: max(a, min(b, c)), "min": min, "max": max})
    )


# Grammar through the reader: window.GRAMMAR_ANSWERS answers analyze_grammar in turn (a value,
# or a function of the text returning a value or a promise). before= code, with LN_BOOK.
GRAMMAR = r"""
window.GRAMMAR_ANSWERS = [];
__api.analyze_grammar = async (text, context, id) => {
  const next = window.GRAMMAR_ANSWERS.shift();
  const value = typeof next === 'function' ? await next(text) : next;
  return {...(value || {}), request_id: id};
};
__api.translate_text = async () => ({translation: 'I like cats', provider: 'llm'});
"""
# Run-body helpers (after SELECTION): open the reader, select a paragraph, Translate, then
# Explain grammar; the assistant shows the answer.
EXPLAIN = r"""
await __settle(openLightNovel(6)); await __tick(300);
const explain = async index => {
  await selectParagraph(index);
  await press('[data-pudge-translate-now]'); await __tick(10);
  await press('[data-pudge-grammar]'); await __tick(10);
};
const grammarBoxes = () => [...document.querySelectorAll('#pudgeAssistant .pudge-grammar')];
const messages = () => [...document.querySelectorAll('#pudgeAssistant .pa-msg')].map(m => m.innerText);
"""
