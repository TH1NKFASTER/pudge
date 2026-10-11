"""Window set-ups for the light-novel reader entry tests (audit 71Б, workstream g10). No tests here.

ENTRY is `before=` code: light novels 178 ("Book 178", four chapters) and others, whose
audiobook link (the shape light_novel_open sends: {ln_book_id, book: {id, playing,
player_running, paused, position, ...}}) is window.LINKS[id]. light_novel_paired_state
answers window.PAIRED. An API name in window.HOLD waits for `release(name)`.

READER_VIEW is the start of a run body: `view()` (what the reader shows), `space()` (one
Space press on the page; returns whether the page took it), `transport()` (playback calls
made so far), `entered()` (the reader contexts the window announced)."""

from __future__ import annotations

TRANSPORT = [
    "audiobook_seek",
    "audiobook_set_paused",
    "audiobook_stop",
    "audiobook_play",
    "light_novel_play_paired",
    "light_novel_play_paired_at_offset",
    "audiobook_play_from",
    "audiobook_toggle",
]

ENTRY = r"""
window.LINKS = {};
window.PAIRED = {};
window.HOLD = {};
window.CHAPTER_FAILS = {};
const waiting = {};
window.release = name => { const answer = (waiting[name] || []).shift(); if (answer) answer(); };
const later = (name, value) => window.HOLD[name]
  ? new Promise((resolve, reject) => { (waiting[name] ||= []).push(() => Promise.resolve().then(value).then(resolve, reject)); })
  : Promise.resolve().then(value);
window.link = (flags = {}, extra = {}) => ({ln_book_id: 178, alignment_mode: 'stt', alignment: {ready: true, status: 'ready'},
  book: {id: 281, title: 'Audio', duration: 900, position: 120, speed: 1, ...flags}, ...extra});
window.bookmark = {};
__api.light_novel_open = id => later('light_novel_open', () => ({id, title: `Book ${id}`, current_chapter: window.bookmark[id]?.[0] ?? 0,
  current_offset: window.bookmark[id]?.[1] ?? 0, chapters: [0, 1, 2, 3].map(i => ({chapter_index: i, title: `Chapter ${i}`})),
  paired_audio: window.LINKS[id] ?? null}));
__api.light_novel_chapter = (id, index) => later('light_novel_chapter', () => {
  if (window.CHAPTER_FAILS[index]) throw new Error(window.CHAPTER_FAILS[index]);
  return {book_id: id, chapter_index: index, paragraphs: [`第${index}章の本文です。`], tokens: [[]], vocabulary: []};
});
__api.light_novel_paired_state = async () => ({...window.PAIRED});
__api.audiobook_review_context = (id, consumer) => later('audiobook_review_context', () => ({ok: true}));
__api.audiobook_reader_context = async () => ({ok: true});
"""

READER_VIEW = r"""
const TRANSPORT_CALLS = TRANSPORT_NAMES;
const view = () => ({reader: $('lnReaderShell').classList.contains('open'), tray: __visible('#lnPairedTray'),
  controls: __visible('#lnPairedTime') && __visible('#lnPairedTray [data-ln-audio-seek="-15"]'), button: __text('#lnPairedAudio'),
  disabled: Boolean($('lnPairedAudio')?.disabled), autoScroll: __visible('#lnPairedAutoScrollLabel')});
const space = async (init = {}, target = document.body) => {
  const event = new KeyboardEvent('keydown', {key: ' ', code: 'Space', bubbles: true, cancelable: true, ...init});
  target.dispatchEvent(event); await __flush();
  return event.defaultPrevented;
};
const transport = () => __apiCalls.filter(c => TRANSPORT_CALLS.includes(c.name)).map(c => [c.name, ...c.args]);
const entered = () => __apiCalls.filter(c => c.name === 'audiobook_reader_context').map(c => c.args[0]);
const gates = () => __apiCalls.filter(c => c.name === 'content_review_begin').map(c => c.args);
""".replace("TRANSPORT_NAMES", str(TRANSPORT).replace("'", '"'))
