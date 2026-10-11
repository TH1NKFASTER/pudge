"""Shared set-up for the manga OCR behaviour tests (audit 71Б, workstream g05).

Window side: `reader_before(regions, words)` answers the manga reader's API for a
4-page volume whose OCR is complete; `READER` is JavaScript the test body starts
with (open the volume, read the OCR debug overlay's word boxes, click a point of
the page and see which word card opens). Geometry is OCR geometry: normalized to
the page image, x from the left, y from the BOTTOM (as the backend sends it).

Backend side: `manga_service()` imports a small volume into a real MangaService;
`page_ocr()` lets it read pages with a stand-in for Mokuro.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image

READER_SETTINGS = {"settings": {"review_gate_manga_enabled": False}}


def reader_before(
    regions: dict[int, list[dict[str, Any]]], words: dict[str, list[str]] | None = None, extra: str = ""
) -> str:
    """API answers: pages' OCR regions (cached) and a parser that splits each
    region text into the given words (unknown texts are one word)."""
    data = json.dumps(
        {"regions": {str(k): v for k, v in regions.items()}, "words": words or {}}, ensure_ascii=False
    )
    return r"""
window.OCR = %s;
__api.manga_state = async () => ({books: [{id: 3, title: 'Vol 1', series_title: 'Manga', page_count: 4, position: 0, ocr_complete: true}], ocr_available: true});
__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 4, name: `p${index}.png`, spread: false, data_uri: 'data:image/png;base64,AAAA'});
__api.manga_ocr_book_status = async () => ({running: false, state: 'ready', complete: true, total_pages: 4, completed: 4, cached_pages: 4});
__api.manga_text_regions = async (id, index) => ({book_id: id, page_index: index, available: true, cached: true, regions: OCR.regions[index] || []});
__api.study_parse_text = async text => {
  const surfaces = OCR.words[text] || [text];
  const tokens = [], vocabulary = [];
  let at = 0;
  surfaces.forEach((surface, k) => {
    const start = text.indexOf(surface, at);
    if (start < 0) return;
    at = start + surface.length;
    tokens.push({wordId: k + 1, readingIndex: 0, start, end: at});
    vocabulary.push({wordId: k + 1, readingIndex: 0, spelling: surface, states: ['new']});
  });
  return {paragraphs: [text], tokens: [tokens], vocabulary};
};
%s
""" % (data, extra)


# The page image is 200x300 px on screen at (0, 0) (natural size the same) as soon as
# it is created, like a decoded image in the browser.
READER = r"""
const W = 200, H = 300;
const reader = () => document.getElementById('mangaReaderV2');
{
  const proto = Object.getPrototypeOf(document.createElement('img'));
  const rectOf = proto.getBoundingClientRect;
  const size = (name, value) => {
    const base = Object.getOwnPropertyDescriptor(proto, name);
    Object.defineProperty(proto, name, {configurable: true, get() { return this.localName === 'img' ? value : base.get.call(this); }});
  };
  size('naturalWidth', W); size('naturalHeight', H);
  proto.getBoundingClientRect = function () {
    return this.localName === 'img' && this.closest('.manga-v2-page-frame')
      ? {x: 0, y: 0, left: 0, top: 0, right: W, bottom: H, width: W, height: H} : rectOf.call(this);
  };
}
const openReader = async () => { await __settle(PudgeMangaReaderV2.openBook(3)); await __tick(300); };
const debugMode = () => reader().dataset.debugOverlay;
const setDebug = async mode => {
  for (let k = 0; k < 3 && debugMode() !== mode; k++) { await __key('D', {shiftKey: true}); await __tick(20); }
};
const pct = value => Math.round(parseFloat(value) * 10) / 1000;
// The OCR debug overlay's word boxes ("words" mode): what a click can hit.
const wordBoxes = async () => {
  await setDebug('words');
  return [...document.querySelectorAll('.manga-v2-debug-token-core')].map(n => {
    const w = pct(n.style.width), h = pct(n.style.height);
    return {word: n.dataset.word, source: n.dataset.source, x: pct(n.style.left), y: Math.round((1 - pct(n.style.top) - h) * 1000) / 1000, w, h};
  });
};
const slopBoxes = () => [...document.querySelectorAll('.manga-v2-debug-token-slop')].map(n => ({
  word: n.dataset.word, left: parseFloat(n.style.left) * W / 100, top: parseFloat(n.style.top) * H / 100,
  width: parseFloat(n.style.width) * W / 100, height: parseFloat(n.style.height) * H / 100}));
// Clicks the page at OCR point (x, y) — on the text overlay when the point is inside a
// region, else on the page image — and returns the word card's term (null: no card).
const cardTerm = () => document.getElementById('pudgeStudyCard')?.classList.contains('open')
  ? document.querySelector('#pudgeStudyCard .pudge-study-term')?.textContent : null;
const clickAt = async (x, y, dx = 0, dy = 0) => {
  const clientX = x * W + dx, clientY = (1 - y) * H + dy;
  const region = [...document.querySelectorAll('.manga-v2-text-region')].find(node => {
    const rx = Number(node.dataset.rawX) * W, rw = Number(node.dataset.rawWidth) * W;
    const ry = (1 - Number(node.dataset.rawY) - Number(node.dataset.rawHeight)) * H, rh = Number(node.dataset.rawHeight) * H;
    return clientX >= rx && clientX <= rx + rw && clientY >= ry && clientY <= ry + rh;
  });
  const target = region ? region.querySelector('.manga-v2-selection-content') : document.querySelector('.manga-v2-page-frame img');
  target.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, clientX, clientY, detail: 1}));
  await __tick(50);
  const term = cardTerm();
  if (term !== null) { PudgeReadingTools.study.closeIfOpen(); await __tick(10); }
  return term;
};
"""

# Fake page pixels for the canvas the reader samples (dom_lite has no raster):
# window.PIXELS = {base: luma, rects: [{x, y, w, h, luma}]} in normalized TOP-DOWN page coordinates.
PIXELS = r"""
const canvasProto = Object.getPrototypeOf(document.createElement('canvas'));
const plainContext = canvasProto.getContext;
canvasProto.getContext = function (kind, options) {
  if (this.localName !== 'canvas' || !window.PIXELS) return plainContext.call(this, kind, options);
  return {
    canvas: this,
    drawImage() {},
    getImageData(left, top, width, height) {
      const data = new Uint8ClampedArray(width * height * 4);
      for (let row = 0; row < height; row++) for (let col = 0; col < width; col++) {
        const px = (left + col + .5) / W, py = (top + row + .5) / H;
        const rect = [...PIXELS.rects].reverse().find(r => px >= r.x && px < r.x + r.w && py >= r.y && py < r.y + r.h);
        const luma = rect ? rect.luma : PIXELS.base, k = (row * width + col) * 4;
        data[k] = data[k + 1] = data[k + 2] = luma; data[k + 3] = 255;
      }
      return {data, width, height};
    },
  };
};
"""


def vertical_segments(
    text: str, x: float, top: float, width: float, height: float, source: str = "vision-character-box"
) -> list[dict[str, Any]]:
    """One segment per character of a vertical column, read top to bottom from `top`
    (OCR y of the column's upper edge)."""
    return [
        {
            "text": ch,
            "x": x,
            "y": round(top - height * (k + 1), 6),
            "width": width,
            "height": height,
            "source": source,
        }
        for k, ch in enumerate(text)
    ]


def horizontal_segments(
    text: str, x: float, y: float, width: float, height: float, source: str = "vision-accurate-range-v2"
) -> list[dict[str, Any]]:
    return [
        {"text": ch, "x": round(x + width * k, 6), "y": y, "width": width, "height": height, "source": source}
        for k, ch in enumerate(text)
    ]


# Backend


def write_volume(path: Path, pages: int = 2) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(pages):
            payload = io.BytesIO()
            Image.new("RGB", (40, 60), (20 + index * 100, 0, 0)).save(payload, format="PNG")
            archive.writestr(f"{index + 1:03}.png", payload.getvalue())
    return path


def manga_service(tmp_path: Path, pages: int = 2):
    """A MangaService with one imported volume: (service, book_id)."""
    from pudge.database import Database
    from pudge.manga import MangaService

    service = MangaService(Database(tmp_path / "db.sqlite3"), cache_dir=tmp_path / "cache")
    book = service.import_file(write_volume(tmp_path / "Volume 1.cbz", pages))
    return service, int(book["id"])


def page_ocr(service, monkeypatch, text: str = "迎える") -> list[str]:
    """Lets `service.text_regions(book, page)` read a page with Mokuro: a stand-in for the
    Mokuro worker answers one vertical text block reading `text` (Mokuro is not installed in
    tests). Returns the names of the pages it read."""
    runs: list[str] = []
    monkeypatch.setattr(service, "_mokuro_python", lambda: Path("/opt/mokuro/bin/python"))

    def recognize(
        archive: Path, name: str, *, python: Any = None, background: bool = False
    ) -> dict[str, Any]:
        runs.append(str(name))
        with zipfile.ZipFile(archive) as volume:
            with Image.open(io.BytesIO(volume.read(name))) as image:
                width, height = image.size
        box = [width * 0.3, height * 0.1, width * 0.4, height * 0.4]
        corners = [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]
        block = {"box": box, "vertical": True, "font_size": 4, "lines": [text], "lines_coords": [corners]}
        return {"img_width": width, "img_height": height, "blocks": [block]}

    monkeypatch.setattr(service, "_mokuro_recognize", recognize)
    return runs
