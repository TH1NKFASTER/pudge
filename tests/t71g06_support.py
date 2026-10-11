"""Shared set-ups for the manga reader / manga OCR tests of audit 71Б (workstream g06).
This module has no tests of its own.

`reader(page, code, ...)` opens manga volume 3 in the page harness with OCR regions you
give per page; `code` runs with the reader open on page 1 and these helpers:

  regions()            the clickable OCR regions on screen: [{index, text, page}]
  regionNode(i)        the element of OCR region i;  calls(name): argument lists of an API method
  rightClick(i)        right-click OCR region i (raw region index) -> menu actions
  menu()               data-manga-region-action of the open context menu
  choose(action)       click a context-menu action
  modal()              {title, text, open} of the shared modal
  clickModal(sel)      click a control inside the modal
  setRegions(page, rows)  what the backend answers for a page from now on
  rects()              status-layer marks: [{state, due, approximate, id, x, y, w, h}]
  marks()              the same with their region text and word
  debugBoxes()         word-overlay hitboxes: [{word, source, status, region, token}]
  exportDebug()        Cmd+Shift+L: the OCR debug payload the reader sends
  takeToast()          the newest toast (the toast area is then emptied)

In `before`: window.__ocrAvailable = false (no OCR engine), __api.manga_ocr_book_status = ...

Images are 1000 x 1500 px laid out at (0, 0), so image fractions map to pixels.
`parse` maps region text to study tokens: {text: [[surface, wordId, readingIndex, card]]}.
STUDY (for `before`) answers Jiten word states from window.__live; `prefs` are the reader
preferences the backend keeps ({"configured": True, "statusHighlight": True} paints states).

Python side: bridge(service) is the window API around a MangaService; vertical(),
horizontal() and column() build OCR regions (column: one segment per character).
"""

from __future__ import annotations

import json
from typing import Any

from pudge.manga import MangaService

BOOK = {
    "id": 3,
    "title": "Vol 1",
    "series_title": "Manga",
    "page_count": 4,
    "position": 0,
    "read_pages": 0,
    "ocr_complete": True,
}

BEFORE = r"""
window.__book = %(book)s;
window.__regions = %(regions)s;
window.__parse = %(parse)s;
window.__prefs = %(prefs)s;
__api.manga_state = async () => ({books: [window.__book], ocr_available: window.__ocrAvailable ?? true,
                                  reader_preferences: window.__prefs});
__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: window.__book.page_count,
                                          name: `p${index + 1}.png`, spread: false, width: 1000, height: 1500,
                                          data_uri: 'data:image/png;base64,AAAA'});
__api.manga_text_regions = async (id, index) => ({book_id: id, page_index: index, available: true, cached: true,
                                                  regions: JSON.parse(JSON.stringify(window.__regions[index] || []))});
__api.manga_ocr_book_status = async () => ({running: false, state: 'ready', complete: true, total_pages: 4, cached_pages: 4, completed: 4});
__api.manga_nearby_mokuro = async () => ({available: false, filename: ''});
// A parse of a region: the words listed for its text, else one unknown word per region.
__api.study_parse_text = async text => {
  const words = window.__parse[text];
  if (!words) return null;
  let pos = 0;
  const tokens = words.map(([surface, wordId, readingIndex]) => {
    const start = text.indexOf(surface, pos); pos = start + surface.length;
    return {wordId, readingIndex, start, end: pos};
  });
  const vocabulary = words.map(([surface, wordId, readingIndex, card]) => ({wordId, readingIndex, spelling: surface, ...(card || {})}));
  return {paragraphs: [text], tokens: [tokens], vocabulary, settings: {study_backend: 'jiten'}};
};
"""

HELPERS = r"""
const __sizeImages = () => {
  for (const image of document.querySelectorAll('.manga-v2-page-frame img')) {
    if (image.__naturalWidth) continue;
    image.__naturalWidth = 1000; image.__naturalHeight = 1500;
    image.__rect = {x: 0, y: 0, width: 1000, height: 1500};
  }
};
new MutationObserver(__sizeImages).observe(document.body, {childList: true, subtree: true});
const openReader = async () => {
  await __settle(PudgeMangaReaderV2.openBook(3));
  __sizeImages();
  await __tick(200);
};
const regions = () => [...document.querySelectorAll('#mangaV2Pages .manga-v2-text-region')].map(node => ({
  index: Number(node.dataset.regionIndex), page: Number(node.dataset.pageIndex), text: node.getAttribute('aria-label')}));
const regionNode = index => document.querySelector(`#mangaV2Pages .manga-v2-text-region[data-region-index="${index}"]`);
const menu = () => document.querySelector('#contextMenu.open')
  ? [...document.querySelectorAll('#contextMenu [data-manga-region-action]')].map(b => b.dataset.mangaRegionAction) : [];
const rightClick = async index => {
  await __fire(`#mangaV2Pages .manga-v2-text-region[data-region-index="${index}"]`, 'contextmenu', {clientX: 300, clientY: 300}, 'MouseEvent');
  await __tick(10);
  return menu();
};
const choose = async action => { await __click(`#contextMenu [data-manga-region-action="${action}"]`); await __tick(50); };
const modal = () => ({open: $('modalBackdrop').classList.contains('open'), title: __text('#modalTitle'), text: __text('#modalBody')});
const clickModal = async selector => { await __click(`#modalBody ${selector}`); await __tick(50); };
const setRegions = (pageIndex, rows) => { window.__regions[pageIndex] = rows; };
// The newest toast message; the toast area is emptied so the next check sees only new ones.
const takeToast = () => { const items = __toasts(); $('toast').replaceChildren(); return items.pop() ?? null; };
const calls = name => __apiCalls.filter(call => call.name === name).map(call => call.args);
const rects = () => [...document.querySelectorAll('#mangaV2Pages .manga-v2-status-layer [data-occurrence-id]')].map(node => ({
  state: [...node.classList].find(c => c.startsWith('state-')), due: node.classList.contains('is-due'),
  approximate: node.classList.contains('is-approximate'), outline: node.classList.contains('outline-only'),
  id: node.dataset.occurrenceId,
  x: Math.round(Number(node.getAttribute('x'))), y: Math.round(Number(node.getAttribute('y'))),
  w: Math.round(Number(node.getAttribute('width'))), h: Math.round(Number(node.getAttribute('height')))}));
// The marks with their words: [{region, word, state, due, approximate, outline}].
const marks = () => rects().map(mark => {
  const [_page, region, _revision, token] = mark.id.split(':');
  const node = regionNode(region);
  return {region: node?.getAttribute('aria-label'), word: node?.querySelectorAll('[data-pudge-study-token]')[Number(token)]?.textContent,
          state: mark.state?.slice(6), due: mark.due, approximate: mark.approximate, outline: mark.outline};
});
const debugBoxes = () => [...document.querySelectorAll('#mangaV2Pages .manga-v2-debug-token-core')].map(node => ({
  word: node.dataset.word, source: node.dataset.source, status: node.dataset.geometryStatus,
  region: Number(node.dataset.regionIndex), token: Number(node.dataset.tokenIndex)}));
const exportDebug = async () => {
  const before = calls('manga_export_ocr_debug').length;
  await __key('L', {code: 'KeyL', metaKey: true, shiftKey: true}); await __tick(20);
  return calls('manga_export_ocr_debug')[before]?.[0] ?? null;
};
"""

# Reading-state answers for the words of a page: window.__live['wordId:readingIndex'] = {...}.
STUDY = r"""
window.__live = window.__live || {};
window.__account = 'jiten:me';
__api.study_provider_capabilities = async () => ({configured: true, account_key: window.__account});
// window.__scope: the account the answer claims (default: the current one; '' = none);
// window.__onlyListed: answer only for the words in window.__live.
__api.study_states = async payload => ({ok: true, account_scope: window.__scope ?? window.__account,
  states: payload.words.filter(([w, r]) => !window.__onlyListed || window.__live[`${w}:${r}`]).map(([w, r]) => ({ok: true, wordId: w, readingIndex: r, stale: false,
                                          ...(window.__live[`${w}:${r}`] || {states: [], normalizedState: 'unknown'})}))});
"""


def reader(
    page,
    code: str,
    *,
    regions: dict[int, list[dict[str, Any]]] | None = None,
    parse: dict[str, list[list[Any]]] | None = None,
    book: dict[str, Any] | None = None,
    prefs: dict[str, Any] | None = None,
    before: str = "",
    state: dict[str, Any] | None = None,
    **options: Any,
):
    """Run `code` (async body) with manga volume 3 open on its first page."""
    prelude = BEFORE % {
        "book": json.dumps({**BOOK, **(book or {})}),
        "regions": json.dumps({str(k): v for k, v in (regions or {}).items()}, ensure_ascii=False),
        "parse": json.dumps(parse or {}, ensure_ascii=False),
        "prefs": json.dumps(prefs or {"configured": False}),
    }
    merged_state = {"settings": {"review_gate_manga_enabled": False, "review_gate_ln_enabled": False}}
    if state:
        merged_state = {**state, "settings": {**merged_state["settings"], **(state.get("settings") or {})}}
    return page.run(
        HELPERS + "\nawait openReader();\n" + code, before=prelude + before, state=merged_state, **options
    )


# ---------------------------------------------------------------- Python side


def bridge(service: MangaService):
    """The window's pywebview API object around `service` (only the manga part is set up)."""
    from pudge.web_app import WebAppApi

    api = WebAppApi.__new__(WebAppApi)
    api.manga = service
    return api


def vertical(text: str, x: float, y: float, width: float, height: float, **extra: Any) -> dict[str, Any]:
    """A vertical OCR region as the backend sends it."""
    return {
        "text": text,
        "raw_text": text,
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        **extra,
    }


def horizontal(text: str, x: float, y: float, width: float, height: float, **extra: Any) -> dict[str, Any]:
    """A horizontal OCR region as the backend sends it."""
    return {
        **vertical(text, x, y, width, height, **extra),
        "orientation": extra.get("orientation", "horizontal"),
    }


def column(
    text: str,
    x: float,
    top: float,
    *,
    source: str = "ink-components-v2",
    status: str | None = "approximate",
    width: float = 0.05,
    step: float = 0.075,
    mapped: bool = True,
    segment_text: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A vertical OCR region of `text` whose top edge is at `top` (image fractions, y up):
    one segment per character of `segment_text` (default: the text), `step` high each."""
    chars = text if segment_text is None else segment_text
    segments = [
        {
            "text": ch,
            "x": x,
            "y": round(top - step * (k + 1), 4),
            "width": width,
            "height": step,
            "source": source,
        }
        for k, ch in enumerate(chars)
    ]
    region: dict[str, Any] = {
        "text": text,
        "raw_text": text,
        "orientation": "vertical",
        "x": x,
        "y": round(top - step * len(text), 4),
        "width": width,
        "height": round(step * len(text), 4),
        "segments": segments,
    }
    if status:
        region["geometry_status"] = status
    if mapped:
        region["word_geometry"] = "mapped_segments"
    region.update(extra)
    return region
