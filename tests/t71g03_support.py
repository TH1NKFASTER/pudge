"""Shared window set-ups for the manga reader tests (audit 71Б, workstream g03). No tests here.

Each constant is JavaScript for `page.run(before=...)` or the start of a `page.run` body
(see tests/page_harness.py)."""

from __future__ import annotations

# A manga library the backend reports: series "Manga", volumes 1 and 2 (ids 3 and 4),
# six pages each. window.MANGA is the backend's state; tests change it in before= code
# or in the run body (the fake backend reads it on every call):
#   books              manga_state books
#   ocr_available      whether Mokuro (the OCR engine) is installed
#   regions(id, index) OCR regions of a page (normalised, y from the bottom as the backend sends)
#   status(id)         manga_ocr_book_status answer
#   cachedOnly(id, index) answer of manga_text_regions(..., cachedOnly=true) when it differs
#   hold.page          page index whose manga_page answers wait for window.release('page')
#   hold.regions       "id:index" whose manga_text_regions answers wait for window.release('regions')
# Every page is a 400x600 image (data URI names the book and page: "data:image/png;base64,3-0").
MANGA = r"""
window.MANGA = {
  ocr_available: true, hold: {},
  books: [
    {id: 3, title: 'Manga 1', series_title: 'Manga', volume: 1, page_count: 6, position: 0, read_pages: 0, ocr_complete: true, anilist_id: 77},
    {id: 4, title: 'Manga 2', series_title: 'Manga', volume: 2, page_count: 6, position: 0, read_pages: 0, ocr_complete: true, anilist_id: 77},
  ],
  regions: (id, index) => [{text: '猫', x: .5, y: .5, width: .2, height: .2, orientation: 'vertical'}],
  status: id => ({book_id: id, complete: true, running: false, total_pages: 6, cached_pages: 6, completed_pages: 6}),
};
// Held answers wait until the test calls window.release('page') / window.release('regions').
window.waiting = {page: [], regions: []};
window.held = kind => new Promise(resolve => window.waiting[kind].push(resolve));
window.release = kind => { const list = window.waiting[kind]; window.waiting[kind] = []; list.forEach(resolve => resolve()); };
__api.manga_state = async () => ({books: JSON.parse(JSON.stringify(window.MANGA.books)), ocr_available: window.MANGA.ocr_available});
__api.manga_page = async (id, index) => {
  if (window.MANGA.hold.page === index) await window.held('page');
  return {book_id: id, page_index: index, page_count: 6, name: `p${index}.png`, spread: false, data_uri: `data:image/png;base64,${id}-${index}`};
};
__api.manga_text_regions = async (id, index, refresh, cachedOnly) => {
  if (window.MANGA.hold.regions === `${id}:${index}`) await window.held('regions');
  return (cachedOnly && window.MANGA.cachedOnly?.(id, index)) ||
    {book_id: id, page_index: index, available: true, cached: true, regions: window.MANGA.regions(id, index)};
};
__api.manga_ocr_book_status = async id => window.MANGA.status(id);
__api.start_manga_ocr_book = async id => window.MANGA.status(id);
__api.stop_manga_ocr_book = async () => ({ok: true});
__api.manga_nearby_mokuro = async () => ({available: false});
__api.manga_set_position = async (id, index) => ({ok: true, id, position: index});
__api.manga_mark_read = async (id, index) => ({id, read_pages: index + 1});
__api.toggle_fullscreen = async () => ({ok: true});
// Jiten words of the test texts: 猫 (new), 犬 (known).
window.WORDS = {'猫': {wordId: 101, reading: 'ねこ', meaning: 'cat', state: 'new'}, '犬': {wordId: 102, reading: 'いぬ', meaning: 'dog', state: 'known'}};
__api.study_parse_text = async text => {
  const tokens = [], vocabulary = [];
  [...text].forEach((ch, i) => {
    const word = window.WORDS[ch];
    if (!word) return;
    tokens.push({start: i, end: i + 1, wordId: word.wordId, readingIndex: 0});
    vocabulary.push({wordId: word.wordId, readingIndex: 0, spelling: ch, reading: word.reading, meaningsChunks: [[word.meaning]], cardState: [word.state]});
  });
  return {paragraphs: [text], tokens: [tokens], vocabulary};
};
__api.study_decks = async () => [{id: '12', name: 'Mining'}];
__api.study_action = async payload => ({ok: true, outcome: 'confirmed', ...payload});
"""

# Run-body helpers for the open reader.
#   open(id)      opens a volume as the library does (a click on its card) and waits for it
#   pages()       what the page frames show: [{index, src, regions: [OCR text of each region]}]
#   layout()      gives the page images a size and position (dom_lite has no layout)
READER = r"""
const reader = () => document.getElementById('mangaReaderV2');
const open = async id => {
  setPage('manga'); await __tick(50);
  await __click(`#mangaContent [data-manga-v2-action="read"][data-id="${id}"]`);
  await __tick(100);
};
const pages = () => [...document.querySelectorAll('#mangaV2Pages .manga-v2-page-frame')].map(frame => ({
  index: Number(frame.dataset.pageIndex),
  src: frame.querySelector('img')?.getAttribute('src') ?? null,
  // The OCR text of each region (the transparent, selectable copy over the bubble).
  regions: [...frame.querySelectorAll('.manga-v2-text-region')].map(r => r.querySelector('.manga-v2-selection-content').textContent),
}));
const layout = () => {
  for (const img of document.querySelectorAll('#mangaV2Pages img')) img.__rect = {x: 0, y: 0, width: 400, height: 600};
};
const callsOf = name => __apiCalls.filter(call => call.name === name).map(call => call.args);
"""
