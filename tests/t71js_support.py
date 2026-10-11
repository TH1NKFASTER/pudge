"""Window set-ups shared by the manga progress tests of audit 71Б (workstream t71js). No
tests here. Build on tests/t71g03_support.py (MANGA, READER).

SAVES (before= code, after MANGA): manga progress saves (manga_set_position, manga_mark_read)
answer at once, fail with 'bridge down' while SAVES.failing, or wait while SAVES.hold until
SAVES.settle(how, filter): how 'resolve' | 'reject' ('offline'), filter(row) picks held saves
({kind: 'position' | 'read', id, index}); the others keep waiting.

STRIP (run body, after READER): openStrip(id) opens a volume in the vertical strip
(scrolled to its top, far from its end); see(i) scrolls page i into view (the page seen
before leaves it); again() goes up to page 2 and down to page 3 at once; saves() lists the
saves sent so far ({position: [index], read: [index], toasts: count}), marks() the read-through
saves as [id, index];
toasts are taken off the screen as they come (the same message would otherwise only extend
the toast already shown) and counted."""

from __future__ import annotations

import json

SAVES = r"""
window.SAVES = {failing: false, hold: false, held: []};
SAVES.settle = (how, filter = () => true) => {
  const now = SAVES.held.filter(filter); SAVES.held = SAVES.held.filter(row => !now.includes(row));
  now.forEach(row => row[how]());
};
const save = (kind, id, index, value) => SAVES.hold
  ? new Promise((resolve, reject) => SAVES.held.push({kind, id, index, resolve: () => resolve(value), reject: () => reject(new Error('offline'))}))
  : (SAVES.failing ? Promise.reject(new Error('bridge down')) : Promise.resolve(value));
__api.manga_set_position = (id, index) => save('position', id, index, {ok: true, id, position: index});
__api.manga_mark_read = (id, index) => save('read', id, index, {id, read_pages: index + 1});
"""

STRIP = r"""
const openStrip = async id => {
  seen = null;
  await open(id);
  Object.assign(document.getElementById('mangaV2Viewport'), {scrollHeight: 20000, clientHeight: 500, scrollTop: 0});
};
const frame = i => document.querySelector(`#mangaV2Pages [data-page-index="${i}"]`);
const visibility = () => __observers.intersection.find(o => !o.disconnected && o.thresholds.length > 1);
let seen = null;
const see = async (index, wait = 300) => {
  const entries = seen === null || seen === index ? [] : [{target: frame(seen), isIntersecting: false, intersectionRatio: 0}];
  visibility().__fire([...entries, {target: frame(index), intersectionRatio: 1}]);
  seen = index; await __tick(wait);
};
const again = async () => { await see(2, 0); await see(3); };
const shownToasts = [];
const saves = () => {
  shownToasts.push(...__toasts()); $('toast').replaceChildren();
  return {position: callsOf('manga_set_position').map(c => c[1]), read: callsOf('manga_mark_read').map(c => c[1]), toasts: shownToasts.length};
};
const marks = () => callsOf('manga_mark_read');
"""

VERTICAL = {
    "localStorage": {"pudge.manga.reader.v2": json.dumps({"mode": "vertical"})},
    "state": {"settings": {"review_gate_manga_enabled": False}},
}


def pages(count: int, ocr: bool = True) -> str:
    """before= code (after MANGA): both volumes have `count` pages; OCR available or not."""
    return f"""
window.MANGA.ocr_available = {json.dumps(ocr)};
window.MANGA.books = window.MANGA.books.map(book => ({{...book, page_count: {count}}}));
"""
