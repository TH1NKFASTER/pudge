"""Shared window set-ups for the light-novel reader tests of audit 71Б (listen together,
seeking, word states and marks). This module has no tests of its own.

Each constant is JavaScript for `page.run(before=...)` or the start of a `page.run` body
(see tests/page_harness.py)."""

from __future__ import annotations

# Light novel 6 "Novel" linked to audiobook 9 whose player is running (paused at 0 s).
# One chapter of four paragraphs; every paragraph is the words ああ|いいい|う|ええええ (10
# characters), so character offsets run 0..40. The audio says one character per second
# (anchor path 0 s -> 0 ... 40 s -> 40), so "position 12.5 s" is the middle of いいい in the
# second paragraph. The fake player (window.PLAYER) moves with the page clock while it
# plays; seeks, pause and resume change it the way mpv would (pause and resume take
# window.MPV_DELAY ms). before= code.
LN_PAIRED = r"""
window.WORDS = ['ああ', 'いいい', 'う', 'ええええ'];
window.PLAYER = {playing: false, base: 0, at: 0, speed: 1};
window.playerPosition = () => Math.max(0, Math.min(40, PLAYER.base + (PLAYER.playing ? (performance.now() - PLAYER.at) / 1000 * PLAYER.speed : 0)));
window.movePlayer = changes => { PLAYER.base = playerPosition(); PLAYER.at = performance.now(); Object.assign(PLAYER, changes); };
window.pairedState = () => {
  const position = playerPosition();
  return {linked: true, audiobook_id: 9, alignment_mode: 'stt', alignment: {ready: true, status: 'ready'},
    player_running: true, playing: PLAYER.playing, paused: !PLAYER.playing, position, speed: PLAYER.speed, duration: 40,
    ln_chapter_index: 0, chapter_char_offset_exact: position, chapter_progress: position / 40,
    anchor_window: {points: [{time: 0, offset: 0}, {time: 40, offset: 40}]}};
};
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0,
  chapters: [{chapter_index: 0, title: 'One'}],
  paired_audio: {ln_book_id: id, alignment_mode: 'stt', alignment: {ready: true, status: 'ready'},
                 book: {id: 9, title: 'Audio', duration: 40, player_running: true, paused: true}}});
__api.light_novel_chapter = async (id, index) => {
  const paragraphs = [0, 1, 2, 3].map(() => WORDS.join(''));
  const tokens = paragraphs.map(() => { let at = 0; return WORDS.map(word => ({start: at, end: (at += word.length)})); });
  return {book_id: id, chapter_index: index, paragraphs, tokens, vocabulary: []};
};
__api.light_novel_paired_state = async () => pairedState();
window.MPV_DELAY = 0;   // ms mpv takes to pause / resume
__api.audiobook_set_paused = async (id, paused) => {
  if (MPV_DELAY) await new Promise(resolve => setTimeout(resolve, MPV_DELAY));
  movePlayer({playing: !paused}); return {ok: true};
};
__api.audiobook_seek = async (id, seconds) => { movePlayer({}); PLAYER.base = Math.max(0, Math.min(40, PLAYER.base + Number(seconds))); return {ok: true}; };
__api.audiobook_seek_to = async (id, position) => { movePlayer({}); PLAYER.base = Number(position); return {ok: true}; };
__api.audiobook_set_speed = async (id, speed) => { movePlayer({speed: Number(speed)}); return {ok: true}; };
__api.light_novel_play_paired = async () => { movePlayer({playing: true}); return pairedState(); };
"""

# Run-body helpers for LN_PAIRED: open the book, describe the highlight.
PAIRED_VIEW = r"""
const openBook = async () => { await __settle(openLightNovel(6)); await __tick(400); };
const wordAt = offset => [...document.querySelectorAll('#lnReader .ln-word')].find(w => Number(w.dataset.lnAudioStart) <= offset && offset < Number(w.dataset.lnAudioEnd));
const label = node => node ? `${node.closest('[data-ln-paragraph]').dataset.lnParagraph}:${node.textContent}` : null;
const highlight = () => {
  const current = [...document.querySelectorAll('#lnReader .ln-paired-word-current')];
  return {words: current.map(label), progress: current.map(w => w.style.getPropertyValue('--ln-paired-word-progress')),
          paragraphs: [...document.querySelectorAll('#lnReader .ln-paired-current')].map(p => p.dataset.lnParagraph)};
};
"""

# A reader paragraph of three words of 10 characters each (offsets 0..30), drawn directly
# for calls of renderLnPairedPosition(state, offset, options) with crafted timings. Run-body code.
READER = r"""
const reader = document.getElementById('lnReader');
reader.innerHTML = '<p data-ln-paragraph="0" data-ln-audio-start="0" data-ln-audio-end="30">'
  + ['一', '二', '三'].map((w, i) => `<span class="ln-word" data-ln-token="0:${i}" data-ln-audio-start="${i * 10}" data-ln-audio-end="${i * 10 + 10}">${w}</span>`).join('')
  + '</p>';
ui.lnChapter = {chapter_index: 0};
const at = (position, extra = {}) => ({alignment_mode: 'stt', playing: true, ln_chapter_index: 0, position, ...extra});
const words = () => [...reader.querySelectorAll('.ln-word')].map(w => ({
  text: w.textContent, current: w.classList.contains('ln-paired-word-current'), done: w.classList.contains('ln-paired-word-done'),
  progress: w.style.getPropertyValue('--ln-paired-word-progress')}));
const current = () => words().filter(w => w.current).map(w => w.text);
"""

# Light novel 6 for word states, marks and furigana: two paragraphs 猫を戻せば / それが目に入った
# with Jiten cards 猫 (new), 戻す (young; the text has the inflected 戻せば), それ (known) and
# 目に入る (due; the text has 目に入った, with token furigana at chapter positions). before= code.
STUDY_BOOK = r"""
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index,
  paragraphs: ['猫を戻せば', 'それが目に入った'],
  tokens: [[{start: 0, end: 1, wordId: 1, readingIndex: 0}, {start: 2, end: 5, wordId: 2, readingIndex: 0}],
           [{start: 0, end: 2, wordId: 3, readingIndex: 0},
            {start: 3, end: 8, wordId: 4, readingIndex: 0, rubies: [{start: 3, end: 4, text: 'め'}, {start: 5, end: 6, text: 'はい'}]}]],
  vocabulary: [
    {wordId: 1, readingIndex: 0, spelling: '猫', reading: 'ねこ', cardState: ['new']},
    {wordId: 2, readingIndex: 0, spelling: '戻す', reading: 'もどす', cardState: ['young']},
    {wordId: 3, readingIndex: 0, spelling: 'それ', reading: 'それ', cardState: ['known']},
    {wordId: 4, readingIndex: 0, spelling: '目に入る', reading: 'めにはいる', cardState: ['due']},
  ]});
"""

# Run-body helpers for STUDY_BOOK: open it; per word (by its text): the furigana shown over
# it, whether it carries the state mark (underline / colour), its state class.
STUDY_VIEW = r"""
await __settle(openLightNovel(6)); await __tick(300);
const studyWords = () => [...document.querySelectorAll('#lnReader .ln-word:not(.ln-word-fallback)')];
const plain = node => { const copy = node.cloneNode(true); copy.querySelectorAll('rt,rp').forEach(n => n.remove()); return copy.textContent; };
const furigana = () => Object.fromEntries(studyWords().map(w => [plain(w),
  [...w.querySelectorAll(':scope > .ln-furigana-ruby, :scope > ruby')].map(r => [r.firstChild.textContent, r.dataset.lnReading])]));
const marked = () => studyWords().filter(w => w.classList.contains('ln-study-mark-enabled')).map(plain);
const marksMode = () => ['word-marks-underline', 'word-marks-color', 'word-marks-none'].filter(c => $('lnReader').classList.contains(c));
"""
