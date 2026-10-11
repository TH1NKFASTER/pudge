"""Shared window set-ups for the light-novel "listen together" tests (audit 71Б).
This module has no tests of its own.

Light novel 9 "Novel" is linked to audiobook 31; one chapter, three paragraphs:

    承る言い方、大人。      承る 0-2 · 言い方 2-5 · 大人 5-7     (audio character offsets)
    洒落る話した。きゃく    洒落る 7-10 · 話した 10-13 · きゃく 13-16 (きゃく: no dictionary token)
    承る。                  承る 16-18 (the book's own ruby うけたまわ on 承; dictionary says しょう)

`window.AUDIO` is what mpv reports to light_novel_paired_state: the position runs with the
page clock while `playing`; the anchor path maps audio seconds to character offsets
(default: offset = seconds - 10). The tests change AUDIO / call `seekAudio()`; `AUDIO.lag`
makes mpv report a position that many seconds late, `AUDIO.hold` makes it stop answering.
"""

from __future__ import annotations

# before= code.
LN_PAIRED = r"""
window.LN_VOCAB = [
  {wordId: 1, readingIndex: 0, spelling: '承る', reading: 'うけたまわる', cardState: ['new'], normalizedState: 'new'},
  {wordId: 2, readingIndex: 0, spelling: '言い方', reading: '言[い]い方[かた]', cardState: ['new'], normalizedState: 'new'},
  {wordId: 3, readingIndex: 0, spelling: '大人', reading: '大人[おとな]', cardState: ['known'], normalizedState: 'known'},
  {wordId: 4, readingIndex: 0, spelling: '洒落る', reading: '洒落[しゃれ]る', cardState: ['new'], normalizedState: 'new'},
  {wordId: 5, readingIndex: 0, spelling: '話す', reading: '話[はな]す', cardState: ['learning'], normalizedState: 'learning'},
  {wordId: 6, readingIndex: 0, spelling: '承', reading: 'しょう', cardState: ['new'], normalizedState: 'new'},
];
window.AUDIO = {playing: true, position: 10, at: null, speed: 1, path: [{time: 10, offset: 0}, {time: 28, offset: 18}], activity: null, activityClock: false, hold: false, lag: 0};
window.audioAt = () => {
  const a = window.AUDIO;
  return a.playing && a.at !== null ? a.position + (performance.now() - a.at) / 1000 * a.speed : a.position;
};
window.seekAudio = position => { window.AUDIO.position = position; window.AUDIO.at = performance.now(); };
window.pairedState = () => {
  const a = window.AUDIO, position = window.audioAt() - a.lag;
  const anchor = {path: a.path, ...(a.activity ? {activity: a.activity, activity_clock: a.activityClock} : {})};
  return {linked: true, audiobook_id: 31, alignment_mode: 'stt', alignment: {ready: true, status: 'ready'},
    playing: a.playing, paused: !a.playing, player_running: true, position, speed: a.speed, duration: 600,
    ln_chapter_index: 0, chapter_char_offset_exact: PudgePairedAudioClock.offsetAtTime({anchor_window: anchor}, position),
    anchor_window: anchor};
};
__api.light_novel_paired_state = async () => {
  const a = window.AUDIO;
  if (a.hold) return new Promise(() => {});            // mpv stopped answering
  if (a.playing && a.at === null) a.at = performance.now();
  return window.pairedState();
};
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0,
  chapters: [{chapter_index: 0, title: 'One'}],
  paired_audio: {ln_book_id: id, alignment_mode: 'stt', alignment: {ready: true, status: 'ready'},
    book: {id: 31, title: 'Audio', duration: 600, position: window.AUDIO.position, speed: 1,
           playing: window.AUDIO.playing, paused: !window.AUDIO.playing, player_running: true}}});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index,
  paragraphs: ['承る言い方、大人。', '洒落る話した。きゃく', '承る。'],
  tokens: [
    [{start: 0, end: 2, wordId: 1, readingIndex: 0}, {start: 2, end: 5, wordId: 2, readingIndex: 0}, {start: 6, end: 8, wordId: 3, readingIndex: 0}],
    [{start: 0, end: 3, wordId: 4, readingIndex: 0}, {start: 3, end: 6, wordId: 5, readingIndex: 0}],
    [{start: 0, end: 2, wordId: 6, readingIndex: 0, rubies: [{start: 0, end: 1, text: 'うけたまわ'}]}],
  ],
  vocabulary: window.LN_VOCAB});
__api.audiobook_seek = async (id, delta) => { window.seekAudio(Math.max(0, window.audioAt() + Number(delta))); return {ok: true}; };
__api.light_novel_export_audio_sync_trace = async payload => ({ok: true, path: '/logs/pudge-ln-audio-sync.json', event_count: payload.events.length});
"""

# Start of a run body: open the book (the reader polls the audiobook from now on) and helpers.
#   words()      every word of the chapter: text (without furigana), current, done, preview, progress
#   current()    index of the highlighted word (-1: none)
#   render(position, offset, options)   the reader draws audio second `position` at character `offset`
#   trace(event) the reader's sync-trace records of one kind
OPEN = r"""
await __settle(openLightNovel(9));
const wordNodes = () => [...document.querySelectorAll('#lnReader .ln-word')];
const surface = node => { const copy = node.cloneNode(true); copy.querySelectorAll('rt,rp').forEach(n => n.remove()); return copy.textContent; };
const words = () => wordNodes().map(node => ({
  text: surface(node),
  current: node.classList.contains('ln-paired-word-current'),
  done: node.classList.contains('ln-paired-word-done'),
  preview: node.classList.contains('ln-paired-furigana-preview'),
  finishing: node.classList.contains('ln-paired-word-finishing'),
  progress: node.style.getPropertyValue('--ln-paired-word-progress'),
}));
const current = () => words().findIndex(word => word.current);
const progressOf = index => words()[index].progress;
const render = (position, offset, options) => renderLnPairedPosition(
  {alignment_mode: 'stt', playing: true, ln_chapter_index: 0, position}, offset, options);
const trace = event => (ui.lnPairedTrace || []).filter(row => row.event === event);
"""
