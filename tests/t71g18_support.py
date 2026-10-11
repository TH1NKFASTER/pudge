"""Shared window set-ups for the light-novel tests (audit 71Б, workstream g18). No tests here.

Each constant is JavaScript for `page.run(before=...)` or the start of a `page.run` body
(see tests/page_harness.py)."""

from __future__ import annotations

import json

# Light novel 6 "Novel" read together with audiobook 75 (alignment ready). window.__state is
# what the backend reports (`paired_ln(playing=...)` sets whether the narration plays);
# pause/resume answers wait until the test calls window.__release().
PAIRED_LN = """
window.__state = {audiobook_id: 75, ln_book_id: 6, ln_chapter_index: 0, alignment: {ready: true, status: 'ready'}, alignment_mode: 'stt', linked: true,
  playing: PLAYING, player_running: PLAYING, paused: false, position: 100, speed: 1, duration: 3600, chapter_char_offset_exact: 0, chapter_char_offset: 0,
  anchor_window: {left_time: 100, left_offset: 0, right_time: 112, right_offset: 12, path: [{time: 100, offset: 0}, {time: 112, offset: 12}]}};
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}],
  paired_audio: {ln_book_id: id, alignment_mode: 'stt', alignment: {ready: true}, book: {id: 75, playing: PLAYING, player_running: PLAYING, paused: false}}});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['一二三四五六七八九十百千'],
  tokens: [[...'一二三四五六七八九十百千'].map((surface, i) => ({start: i, end: i + 1, surface}))]});
__api.light_novel_paired_state = () => Promise.resolve({...window.__state});
window.__release = () => {};
const held = playing => () => new Promise(resolve => { window.__release = () => { window.__state = {...window.__state, playing, player_running: true}; resolve({...window.__state}); }; });
__api.light_novel_play_paired = held(true);
__api.audiobook_set_paused = (_id, paused) => held(!paused)();
__api.audiobook_stop = async () => ({ok: true});
"""
NO_LN_GATE = {"settings": {"review_gate_ln_enabled": False}}


def paired_ln(playing: bool) -> str:
    return PAIRED_LN.replace("PLAYING", "true" if playing else "false")


# The book card of light novel 6 on the shelf (its audiobook link, as light_novel_state sends it).
PAIRED_BOOK = {
    "id": 6,
    "title": "Novel",
    "volume": 1,
    "paired_audio": {"ln_book_id": 6, "alignment": {"ready": True}, "book": {"id": 75}},
}
PAIRED_SHELF = {
    "light_novel_state": {"version": 1, "settings": {}, "books": [PAIRED_BOOK]},
    "audiobook_state": {"books": [{"id": 75, "title": "Audio", "duration": 3600}]},
}

# Run-body helpers for an open reader: the highlighted word of listen-together and the
# visual clock of the transport (seconds of narration the reader shows).
READER_VIEW = r"""
const spoken = () => document.querySelector('#lnReader .ln-paired-word-current')?.textContent ?? null;
const clock = () => lnPairedTransportClockNow(ui.lnPairedState || {});
const transport = () => ({button: __visible('#lnPairedAudio'), label: __text('#lnPairedAudio'), classes: $('lnPairedAudio')?.className ?? '',
  controls: [...document.querySelectorAll('#lnPairedTray [data-ln-paired-control]')].filter(n => n.checkVisibility()).length});
"""


def ln_book(book_id: int = 6, paragraphs: list[str] | None = None, settings: dict | None = None) -> str:
    """before= code: book `book_id` with one chapter of plain paragraphs and the given reader settings."""
    paragraphs = paragraphs or ["猫が好きです。"]
    return (
        (
            "__api.light_novel_state = async () => ({version: 1, settings: SETTINGS, books: [{id: ID, title: 'Novel', volume: 1}]});\n"
            "__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});\n"
            "__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: PARAGRAPHS,\n"
            "  tokens: PARAGRAPHS.map(() => []), vocabulary: []});\n"
        )
        .replace("SETTINGS", json.dumps(settings or {}))
        .replace("ID", str(book_id))
        .replace("PARAGRAPHS", json.dumps(paragraphs, ensure_ascii=False))
    )


# The reading assistant panel has a box that follows its inline position and size
# (dom_lite has no layout). Start of a run body or before= code.
ASSISTANT_LAYOUT = r"""
Object.defineProperty(Element.prototype, '__rect', {
  configurable: true,
  get() {
    if (this.id !== 'pudgeAssistant') return undefined;
    const px = value => parseFloat(value) || 0;
    return {x: px(this.style.left), y: px(this.style.top), width: px(this.style.width), height: px(this.style.height)};
  },
  set(value) { Object.defineProperty(this, '__rect', {value, writable: true, configurable: true}); },
});
"""
LLM = {
    "llm_enabled": True,
    "llm_model": "model",
    "llm_url": "http://127.0.0.1:11434",
    "llm_provider": "ollama",
}
