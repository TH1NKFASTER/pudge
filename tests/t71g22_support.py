"""Shared set-ups for the light-novel reader tests of audit 71Б workstream g22 (audio
start-up and alignment, scrolling while listening, illustrations, diagnostics). This
module has no tests of its own.

JavaScript constants are code for `page.run(before=...)` or the start of a `page.run`
body (see tests/page_harness.py); the Python helper builds a light novel linked to an
audiobook in the real backend services."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService


def ln_audio_pair(
    tmp_path: Path, chapters: list[str], *, duration: float = 100.0
) -> tuple[LightNovelService, AudiobookService, int, int]:
    """A light novel with the given chapter texts linked to a one-file audiobook of
    `duration` seconds (no alignment prepared). Returns (ln, audio, ln_book_id, audiobook_id)."""
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    ln = LightNovelService(cfg)
    now = time.time()
    with ln._connect() as conn:
        conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,current_chapter,current_offset,finished,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            ("Book", str(tmp_path / "book.txt"), "txt", 0, 0.0, 0, now, now),
        )
        ln_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for index, text in enumerate(chapters):
            conn.execute(
                "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
                (
                    ln_id,
                    index,
                    f"Chapter {index + 1}",
                    text,
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                ),
            )
    audio_file = tmp_path / "audio.m4b"
    audio_file.write_bytes(b"audio")
    audio = AudiobookService(
        Database(cfg.library.database_path),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=cfg.paths.cache_dir,
    )
    book = audio._upsert(
        path=audio_file,
        title="Audio",
        duration=duration,
        files=[
            {
                "index": 0,
                "path": str(audio_file),
                "title": "audio",
                "duration": duration,
                "start": 0.0,
                "end": duration,
            }
        ],
        chapters=[{"index": 0, "title": "Chapter 1", "start": 0.0, "end": duration}],
    )
    audio_id = int(book["id"])
    audio.link_light_novel(ln_id, audio_id, prepare_alignment=False)
    return ln, audio, ln_id, audio_id


# Light novel 6 "Novel" (not read with an audiobook): one chapter, an illustration between two
# paragraphs. The chapter arrives first unparsed (Jiten still working: plain text, no
# dictionary words); the parse-status poll answers with the parsed chapter once the test
# sets window.PARSED = true. Reader settings: window.LN_SETTINGS (blur_images ...). before= code.
ILLUSTRATED = r"""
window.PARAS = ['猫が好きです。', '[[PUDGE_LN_IMAGE_URL:/covers/pic.jpg]]', '犬も好きです。'];
window.PARSED = false;
window.LN_SETTINGS = window.LN_SETTINGS || {};
__api.light_novel_state = async () => ({version: 1, settings: LN_SETTINGS, books: [{id: 6, title: 'Novel', volume: 1}]});
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, parsing: true, paragraphs: PARAS, tokens: [[], [], []], vocabulary: []});
__api.light_novel_chapter_parse_status = async (id, index) => PARSED ? {ready: true, payload: {book_id: id, chapter_index: index, paragraphs: PARAS,
  tokens: [[{start: 0, end: 1, wordId: 1, readingIndex: 0}], [], [{start: 0, end: 1, wordId: 2, readingIndex: 0}]],
  vocabulary: [{wordId: 1, readingIndex: 0, spelling: '猫', reading: 'ねこ', cardState: ['new']},
               {wordId: 2, readingIndex: 0, spelling: '犬', reading: 'いぬ', cardState: ['new']}]}} : {ready: false};
"""
NO_LN_GATE = {"settings": {"review_gate_ln_enabled": False}}

# Run-body helpers: the user scrolls the reader with the trackpad (a wheel event starts it,
# then the WebKit scroll events come every `step` ms; the page itself never moves
# scrollTop); `idle(ms)` lets time pass without scrolling.
USER_SCROLL = r"""
const scroller = $('lnReaderScroll');
const wheel = () => { const event = new WheelEvent('wheel', {deltaY: 40, bubbles: true, cancelable: true}); scroller.dispatchEvent(event); return event; };
const userScroll = async (ms, step = 40) => {
  wheel();
  for (let t = 0; t < ms; t += step) { scroller.scrollTop += 30; scroller.dispatchEvent(new Event('scroll')); await __tick(step); }
};
const scrolling = () => scroller.classList.contains('ln-reader-scrolling');
"""

# Run-body helpers: layout reads of the reader's viewport (getBoundingClientRect of
# #lnReaderScroll forces a WebKit layout) and DOM searches over the whole chapter
# (querySelectorAll on #lnReader), counted while `costs.on`.
COSTS = r"""
const costs = {on: false, viewport: 0, chapterQueries: 0};
const rectOf = Element.prototype.getBoundingClientRect, queryAll = Element.prototype.querySelectorAll;
Element.prototype.getBoundingClientRect = function () { if (costs.on && this.id === 'lnReaderScroll') costs.viewport++; return rectOf.call(this); };
Element.prototype.querySelectorAll = function (selector) { if (costs.on && this.id === 'lnReader') costs.chapterQueries++; return queryAll.call(this, selector); };
const measure = async ms => { costs.viewport = 0; costs.chapterQueries = 0; costs.on = true; await __tick(ms); costs.on = false; return {viewport: costs.viewport, chapterQueries: costs.chapterQueries}; };
"""
