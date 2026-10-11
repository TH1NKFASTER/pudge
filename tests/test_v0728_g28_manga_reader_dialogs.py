"""Dialogs opened from the manga reader (the volume's word list) appear above it
(behaviour, page harness; audit 71Б rewrite)."""

from __future__ import annotations

from tests.css_support import css_rules
from tests.t71g06_support import reader


def _z(rules, selector: str) -> int:
    value = rules.get(selector, "z-index")
    assert value is not None, f"no z-index for {selector}"
    return int(value)


def test_dialogs_stack_above_the_reader_only_while_it_is_open() -> None:
    page_css, reader_css = css_rules("index.html"), css_rules("manga_reader_v2.css")
    # An ordinary dialog sits below the full-window reader; while the reader is open the
    # body is marked and an open dialog is raised above it.
    assert (
        _z(page_css, ".modal-backdrop")
        < _z(reader_css, ".manga-v2-reader")
        < _z(page_css, "body.manga-v2-reading .modal-backdrop.open")
    )


EMPTY_LIST = "__api.manga_volume_words = async () => ({ok: false, reason: 'no_ocr'});"


def test_volume_words_open_the_shared_dialog_over_the_open_reader(page) -> None:
    result = reader(
        page,
        r"""
        const out = {reading: document.body.classList.contains('manga-v2-reading'), visible: __visible('#modalBackdrop')};
        await __click('[data-manga-v2-action="settings"]');
        await __click('#mangaV2Settings [data-manga-v2-action="volume-words"]'); await __tick(20);
        out.dialog = {visible: __visible('#modalBackdrop'), title: __text('#modalTitle'), body: __text('#modalBody'),
                      readerOpen: $('mangaReaderV2').classList.contains('open')};
        await __click('#modalClose'); await __tick(20);
        out.dialogClosed = {visible: __visible('#modalBackdrop'), readerOpen: $('mangaReaderV2').classList.contains('open')};
        await __click('#mangaReaderV2 [data-manga-v2-action="close"]'); await __tick(20);
        out.closed = document.body.classList.contains('manga-v2-reading');
        out.labels = {title: t('manga.volumeWords2'), none: t('manga.thisVolumeHasNoOcr')};
        return out;
        """,
        before=EMPTY_LIST,
    )
    out = result.value
    assert out["reading"] is True and out["visible"] is False
    assert out["dialog"] == {
        "visible": True,
        "title": out["labels"]["title"],
        "body": out["labels"]["none"],
        "readerOpen": True,
    }
    assert result.calls_of("manga_volume_words") == [[3, 40]]
    assert out["dialogClosed"] == {"visible": False, "readerOpen": True}
    assert out["closed"] is False


def test_one_escape_closes_the_words_dialog_it_covers_the_settings(page) -> None:
    result = reader(
        page,
        r"""
        await __click('[data-manga-v2-action="settings"]');
        await __click('#mangaV2Settings [data-manga-v2-action="volume-words"]'); await __tick(20);
        await __key('Escape'); await __tick(20);
        return {dialog: __visible('#modalBackdrop'), reader: $('mangaReaderV2').classList.contains('open')};
        """,
        before=EMPTY_LIST,
    )
    assert result.value == {"dialog": False, "reader": True}
