"""Shared window set-ups for the light-novel reader tests: reading together with an
audiobook (the clock, the highlight, furigana timing), furigana layout and selection
(audit 71Б). This module has no tests of its own.

Each JavaScript constant is code for `page.run(before=...)` or the start of a
`page.run` body (see tests/page_harness.py)."""

from __future__ import annotations

import json
import re
from functools import lru_cache

from css_support import WEB, css_rules

# Light novel 6 "Novel" read together with audiobook 75. Its one chapter is one paragraph
# of five two-character words 今日明日昨日話し先日, word i covering text offsets [2i, 2i+2)
# (the reading clock counts characters; 話し has uneven morae, 話 = はな); even words are
# new (furigana for unknown words), odd ones known.
#
# window.AUDIO is the audiobook as the backend reports it: while `playing`, its position
# runs with the page clock (performance.now) at `speed`; `path` is the anchor window
# (audio time -> text offset) sent with every state. AUDIO.hold = true keeps further
# state requests unanswered (a stalled backend). before= code.
PAIRED = r"""
window.WORDS = ['今日', '明日', '昨日', '話し', '先日'];
window.READINGS = ['きょう', 'あした', 'きのう', 'はなし', 'せんじつ'];
window.RUBIES = [[0, 2, 'きょう'], [2, 4, 'あした'], [4, 6, 'きのう'], [6, 7, 'はな'], [8, 10, 'せんじつ']];
window.AUDIO = {playing: false, running: false, position: 100, at: 0, speed: 1, hold: false,
  path: [{time: 100, offset: 0}, {time: 110, offset: 10}], activity: null};
AUDIO.at = performance.now();
window.audioPosition = () => AUDIO.playing ? AUDIO.position + (performance.now() - AUDIO.at) / 1000 * AUDIO.speed : AUDIO.position;
window.setAudio = patch => { AUDIO.position = audioPosition(); AUDIO.at = performance.now(); Object.assign(AUDIO, patch); };
window.pairedState = () => {
  const position = audioPosition(), path = AUDIO.path;
  return {audiobook_id: 75, ln_book_id: 6, ln_chapter_index: 0, linked: true, alignment: {ready: true, status: 'ready'}, alignment_mode: 'stt',
    playing: AUDIO.playing, player_running: AUDIO.running, paused: !AUDIO.playing, position, speed: AUDIO.speed, duration: 3600,
    chapter_char_offset_exact: PudgePairedAudioClock.offsetAtTime({anchor_window: {path}}, position),
    anchor_window: {path, left_time: path[0].time, right_time: path[path.length - 1].time, ...(AUDIO.activity ? {activity: AUDIO.activity} : {})}};
};
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}],
  paired_audio: {ln_book_id: id, alignment_mode: 'stt', alignment: {ready: true, status: 'ready'},
                 book: {id: 75, playing: AUDIO.playing, player_running: AUDIO.running, paused: !AUDIO.playing}}});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: [WORDS.join('')],
  tokens: [WORDS.map((w, i) => ({start: 2 * i, end: 2 * i + 2, wordId: 200 + i, readingIndex: 0, rubies: [{start: RUBIES[i][0], end: RUBIES[i][1], text: RUBIES[i][2]}]}))],
  vocabulary: WORDS.map((w, i) => ({wordId: 200 + i, readingIndex: 0, spelling: w, reading: READINGS[i], meaningsChunks: [['day']], cardState: [i % 2 ? 'known' : 'new']}))});
__api.light_novel_paired_state = () => AUDIO.hold ? new Promise(() => {}) : Promise.resolve(pairedState());
__api.audiobook_set_paused = async (_id, paused) => { setAudio({playing: !paused, running: true}); return {ok: true}; };
__api.light_novel_play_paired = async (_book, _chapter, progress) => { setAudio({position: 100 + 10 * progress, playing: true, running: true}); return pairedState(); };
__api.audiobook_stop = async () => { setAudio({playing: false, running: false}); return {ok: true}; };
__api.audiobook_review_context = async () => ({ok: true});
"""

# The reader's light-novel settings; reviews before reading are off.
LN_STATE = {"version": 1, "settings": {}, "books": []}
LN = {"state": {"settings": {"review_gate_ln_enabled": False}}}


def paired(playing: bool = False, settings: dict | None = None, extra: str = "") -> dict:
    """page.run options: the paired book above, the audiobook playing (and its player
    running) from audio time 100 or stopped, the reader `settings`."""
    start = f"Object.assign(AUDIO, {{playing: {str(playing).lower()}, running: {str(playing).lower()}}});"
    state = {**LN_STATE, "settings": dict(settings or {})}
    return {**LN, "before": PAIRED + start + extra, "api": {"light_novel_state": state}}


# Run-body helpers: the reader's words and what the highlight shows.
#   openReader()   open book 6 in the reader (it starts following the audiobook)
#   wordEls()      the reader's word elements; words(): [{current, progress, classes}] per
#                  word (classes: the ln-paired-* ones)
#   current()      index of the highlighted word (-1: none)
#   shown()        the text offset the highlight shows (current word + its progress)
#   untilAudio(t)  advance the page clock until the audiobook is at audio time t
#   paintAt(o, t)  one paint of the highlight (see below)
READER = r"""
const wordEls = () => [...document.querySelectorAll('#lnReader .ln-word')];
const words = () => wordEls().map(w => ({current: w.classList.contains('ln-paired-word-current'),
  progress: w.style.getPropertyValue('--ln-paired-word-progress'),
  classes: [...w.classList].filter(c => c.startsWith('ln-paired')).sort()}));
const current = () => wordEls().findIndex(w => w.classList.contains('ln-paired-word-current'));
const shown = () => {
  const w = wordEls()[current()]; if (!w) return null;
  const start = Number(w.dataset.lnAudioStart), end = Number(w.dataset.lnAudioEnd);
  return start + (end - start) * parseFloat(w.style.getPropertyValue('--ln-paired-word-progress') || '0') / 100;
};
const untilAudio = async time => { await __tick(Math.max(0, Math.round((time - audioPosition()) * 1000 / AUDIO.speed))); };
const openReader = async () => { await __settle(openLightNovel(6)); await __tick(150); };
// One paint of the highlight at text `offset` (audio time `position`), as the frame loop
// paints while playing; with the audiobook stopped nothing else paints in between.
const paintAt = (offset, position) => {
  renderLnPairedPosition({...pairedState(), playing: true, paused: false, position}, offset, {speechActive: true});
  return {word: current(), offset: shown()};
};
"""


# ---- computed style of page elements (the harness models only `display` of elements)

_SHEET_FILES = sorted(path.name for path in WEB.glob("*.css"))


def _media_applies(media: str, width: int = 1300) -> bool:
    # As tests/js/page.cjs decides for the `display` cascade.
    query = media.lower()
    if re.search(r"print", query) and not re.search(r"screen", query):
        return False
    if re.search(
        r"prefers-reduced-motion\s*:\s*reduce|prefers-color-scheme\s*:\s*light|hover\s*:\s*none|pointer\s*:\s*coarse",
        query,
    ):
        return False
    if any(width > int(n) for n in re.findall(r"max-width\s*:\s*(\d+)px", query)):
        return False
    return not any(width < int(n) for n in re.findall(r"min-width\s*:\s*(\d+)px", query))


def _split(selector_list: str) -> list[str]:
    out, depth, start = [], 0, 0
    for k, c in enumerate(selector_list):
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == "," and depth == 0:
            out.append(selector_list[start:k])
            start = k + 1
    out.append(selector_list[start:])
    return [s.strip() for s in out if s.strip()]


def _specificity(selector: str) -> tuple[int, int, int]:
    a = b = c = 0
    k = 0
    while k < len(selector):
        ch = selector[k]
        if ch == "#":
            m = re.match(r"#[\w-]+", selector[k:])
            a += 1
            k += len(m.group(0)) if m else 1
        elif ch == ".":
            m = re.match(r"\.[\w-]+", selector[k:])
            b += 1
            k += len(m.group(0)) if m else 1
        elif ch == "[":
            b += 1
            k = selector.index("]", k) + 1
        elif ch == ":":
            m = re.match(r"::?([\w-]+)(\()?", selector[k:])
            if not m:
                k += 1
                continue
            name, is_element = m.group(1), selector[k : k + 2] == "::" or m.group(1) in {"before", "after"}
            k += len(m.group(0))
            arg = ""
            if m.group(2):
                depth, start = 1, k
                while depth:
                    depth += {"(": 1, ")": -1}.get(selector[k], 0)
                    k += 1
                arg = selector[start : k - 1]
            if is_element:
                c += 1
            elif name in {"not", "is", "has", "matches"}:
                parts = [_specificity(part) for part in _split(arg)] or [(0, 0, 0)]
                best = max(parts)
                a, b, c = a + best[0], b + best[1], c + best[2]
            elif name != "where":
                b += 1
        elif re.match(r"[a-zA-Z]", ch):
            m = re.match(r"[\w-]+", selector[k:])
            c += 1
            k += len(m.group(0))
        else:
            k += 1
    return a, b, c


_PSEUDO_ELEMENT = re.compile(r"(::?(?:after|before)|::[\w-]+)$")


@lru_cache(maxsize=None)
def _rules(props: tuple[str, ...]) -> str:
    rows = []
    for sheet in [*_SHEET_FILES, "index.html"]:
        for order, (media, selector_list, declarations) in enumerate(css_rules(sheet).rules):
            if media and not _media_applies(media):
                continue
            for prop in props:
                if prop not in declarations:
                    continue
                raw = declarations[prop]
                important = bool(re.search(r"!\s*important\s*$", raw))
                value = re.sub(r"\s*!\s*important\s*$", "", raw).strip()
                for selector in _split(selector_list):
                    pseudo = ""
                    found = _PSEUDO_ELEMENT.search(selector)
                    if found:
                        pseudo = found.group(1).lstrip(":")
                        selector = selector[: found.start()].strip() or "*"
                    rows.append(
                        {
                            "sheet": sheet,
                            "order": order,
                            "sel": selector.replace(":hover", "[data-t71-hover]"),
                            "pseudo": pseudo,
                            "prop": prop,
                            "value": value,
                            "important": important,
                            "spec": list(_specificity(selector + (f"::{pseudo}" if pseudo else ""))),
                        }
                    )
    return json.dumps(rows, ensure_ascii=False)


INHERITED = ("visibility", "cursor", "line-height", "color", "font-size", "white-space")


def computed_style(*props: str) -> str:
    """Run-body code defining `__css(element, property, pseudo = '')`: the value the window's
    stylesheets give the element (or its ::after / ::before), as a browser cascades it:
    !important, specificity, then order (link order of the document, its <style> blocks
    last), inherited properties from the parent, inline styles; var(--x) resolved from the
    inline custom properties of the element and its ancestors. Null when nothing sets it.
    Hover: `__hover(element)` marks the element and its ancestors as hovered."""
    return (
        f"const __cssRules = {_rules(tuple(sorted(set(props))))};\nconst __cssInherited = {json.dumps(INHERITED)};\n"
        + r"""
const __sheetRank = (() => {
  const links = [...document.querySelectorAll('link[rel="stylesheet"]')].map(l => l.getAttribute('href'));
  return sheet => sheet === 'index.html' ? links.length : links.indexOf(sheet);
})();
const __hover = element => { document.querySelectorAll('[data-t71-hover]').forEach(n => n.removeAttribute('data-t71-hover'));
  for (let n = element; n && n.nodeType === 1; n = n.parentElement) n.setAttribute('data-t71-hover', ''); };
const __resolveVars = (value, element) => String(value).replace(/var\((--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\))?[^()]*))?\)/g, (_m, name, fallback) => {
  for (let n = element; n && n.nodeType === 1; n = n.parentElement) { const v = n.style?.getPropertyValue?.(name); if (v) return v.trim(); }
  return fallback === undefined ? '' : fallback.trim();
});
const __cssWins = (key, other) => { for (let k = 0; k < key.length; k++) if (key[k] !== other[k]) return key[k] > other[k]; return false; };
const __css = (element, prop, pseudo = '') => {
  if (!element) return null;
  if (!pseudo) { const inline = element.style?.getPropertyValue?.(prop); if (inline) return __resolveVars(inline, element); }
  let best = null;
  for (const rule of __cssRules) {
    if (rule.prop !== prop || rule.pseudo !== pseudo) continue;
    const rank = __sheetRank(rule.sheet); if (rank < 0) continue;
    let hit = false; try { hit = element.matches(rule.sel); } catch (_) { hit = false; }
    if (!hit) continue;
    const key = [rule.important ? 1 : 0, ...rule.spec, rank, rule.order];
    if (!best || __cssWins(key, best.key)) best = {key, value: rule.value};
  }
  if (best) return __resolveVars(best.value, element);
  if (pseudo) return __cssInherited.includes(prop) ? __css(element, prop) : null;
  return __cssInherited.includes(prop) && element.parentElement ? __css(element.parentElement, prop) : null;
};
"""
    )


# The audiobooks page: Wolf volumes 1 (id 3, with a cover and a bookmark) and 2 (id 4)
# form a series card; "Other" (id 5) stands alone. Point actions answer with the changed
# book only, as the backend does. before= code.
AUDIOBOOKS = r"""
window.BOOKS = [
  {id: 3, title: 'Book 1', series_key: 'wolf', series_title: 'Wolf', volume: 1, duration: 600, position: 120, playing: false, player_running: false, speed: 1,
   cover_url: '/covers/a.jpg', bookmarks: [{id: 9, position: 60, title: 'Here'}]},
  {id: 4, title: 'Book 2', series_key: 'wolf', series_title: 'Wolf', volume: 2, duration: 600, position: 0, playing: false, player_running: false, speed: 1},
  {id: 5, title: 'Other', duration: 300, position: 0, playing: false, player_running: false, speed: 1},
];
__api.audiobook_state = async () => ({books: window.BOOKS});
__api.audiobook_add_bookmark = async (id, title) => ({book: {...BOOKS.find(b => b.id === id), bookmarks: [{id: 9, position: 60, title: 'Here'}, {id: 10, position: 120, title}]}});
__api.audiobook_seek_to = async (id, position) => ({book: {...BOOKS.find(b => b.id === id), position}});
__api.audiobook_play = async id => ({ok: true, book: {...BOOKS.find(b => b.id === id), playing: true, player_running: true}});
"""
