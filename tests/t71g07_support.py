"""Shared run-body helpers for the manga reader tests of audit 71Б workstream g07 (word
cards from the page, selections, the OCR debug log). This module has no tests of its own.

They build on tests/t71g06_support.py: `reader(page, CLICKS + code, ...)` opens volume 3
on its first page with page images of 1000 x 1500 px at (0, 0), so an OCR point (x, y)
(image fractions, y from the BOTTOM as the backend sends it) is the client point
(x * 1000, (1 - y) * 1500).

  card()                   the open word card's term, or null
  closeCard()              closes the word card
  clickImage(x, y, dx, dy, {press, keepSelection})
                           a click on the page image itself (the text overlay lets clicks
                           through to the picture) at OCR point (x, y), moved by (dx, dy) px;
                           like WebKit, the mouse press first clears a selection (keepSelection:
                           it stays; press: false = a click without a pointerdown)
  clickText(x, y)          a click on the transparent OCR text copy of the bubble at (x, y)
  liveTextSelect(text, r)  WebKit Live Text selects `text` on the page image; r = client rect
                           {left, top, width, height}; the selection has no DOM ancestry
  dropSelection()          the selection collapses without a selectionchange (as on mousedown)
  debugLog()               the payload of the OCR debug export (Cmd+Shift+L)

CLICKS also gives document.elementFromPoint a layout (dom_lite has none): over a bubble it
is the bubble's OCR text copy, elsewhere on the page the page image.
"""

from __future__ import annotations

CLICKS = r"""
const W = 1000, H = 1500;
const card = () => document.getElementById('pudgeStudyCard')?.classList.contains('open')
  ? document.querySelector('#pudgeStudyCard .pudge-study-term')?.textContent : null;
const closeCard = async () => { PudgeReadingTools.study.closeIfOpen(); await __tick(10); };
const pagePoint = (x, y, dx = 0, dy = 0) => ({clientX: x * W + dx, clientY: (1 - y) * H + dy});
// Layout for document.elementFromPoint (dom_lite has none): the OCR text copy over a bubble
// takes the pointer, elsewhere the page image does.
const regionAt = (clientX, clientY) => [...document.querySelectorAll('#mangaV2Pages .manga-v2-text-region')].find(node => {
  const left = Number(node.dataset.rawX) * W, width = Number(node.dataset.rawWidth) * W;
  const top = (1 - Number(node.dataset.rawY) - Number(node.dataset.rawHeight)) * H, height = Number(node.dataset.rawHeight) * H;
  return clientX >= left && clientX <= left + width && clientY >= top && clientY <= top + height;
});
window.__elementFromPoint = (x, y) => {
  const region = regionAt(x, y);
  if (region) return region.querySelector('.manga-v2-selection-content');
  return x >= 0 && x <= W && y >= 0 && y <= H ? document.querySelector('#mangaV2Pages .manga-v2-page-frame img') : document.body;
};
const mouse = (target, type, init) => target.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, detail: 1, ...init}));
const clickImage = async (x, y, dx = 0, dy = 0, {press = true, keepSelection = false} = {}) => {
  const image = document.querySelector('#mangaV2Pages .manga-v2-page-frame img');
  const point = pagePoint(x, y, dx, dy);
  if (press) image.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true, cancelable: true, ...point}));
  if (!keepSelection) dropSelection();        // WebKit clears a Live Text selection on mouse down
  mouse(image, 'click', point);
  await __tick(50);
  return card();
};
const clickText = async (x, y) => {
  const point = pagePoint(x, y);
  mouse(regionAt(point.clientX, point.clientY).querySelector('.manga-v2-selection-content'), 'click', point);
  await __tick(50);
  return card();
};
const liveTextSelect = (text, rect) => {
  const holder = document.createElement('span');
  holder.textContent = text;
  document.body.appendChild(holder);
  const range = document.createRange();
  range.setStart(holder.firstChild, 0); range.setEnd(holder.firstChild, text.length);
  range.__rect = {x: rect.left, y: rect.top, width: rect.width, height: rect.height};
  const selection = window.getSelection();
  selection.removeAllRanges(); selection.addRange(range);
  document.dispatchEvent(new Event('selectionchange'));
};
const dropSelection = () => window.getSelection().removeAllRanges();
const debugLog = async () => {
  const before = __apiCalls.filter(call => call.name === 'manga_export_ocr_debug').length;
  await __key('L', {code: 'KeyL', metaKey: true, shiftKey: true}); await __tick(20);
  return __apiCalls.filter(call => call.name === 'manga_export_ocr_debug')[before].args[0];
};
"""

# before= code: the OCR debug export answers with a path (it writes nothing in tests).
DEBUG_EXPORT = "__api.manga_export_ocr_debug = async () => ({path: '/logs/manga-ocr-debug.zip'});\n"
