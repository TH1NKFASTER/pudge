"""Light-novel reader: the background Jiten/JPDB parse of the open chapter (review FE-2).

Before: when the parse failed (offline, a Jiten outage, a token change) the backend kept
answering {ready: false, parsing: false} and the window polled it every 0.7 s for as long
as the chapter stayed open (about 86 bridge calls and log lines a minute), with no word
lookup and no message. One failed status call, on the other hand, ended the poll for good.
Now the poll ends on "not parsing any more" with a line that offers Retry (which starts the
parse again), and a failed status call is retried with a growing pause.
Pages run in the node harness (tests/page_harness.py)."""

from __future__ import annotations

from tests.i18n_support import en, ru

SETUP = r"""
window.parses = [];
document.getElementById('lnReaderShell').classList.add('open');   // the reader is open
__api.light_novel_chapter = async (book, index) => ({book_id: book, chapter_index: index, title: 'C', text: 'テキスト',
  paragraphs: ['テキスト'], tokens: [], vocabulary: [], settings: {}, parsing: true});
"""

FAILED_PARSE = r"""
__api.light_novel_chapter_parse_status = async () => window.parses.length ? window.parses.shift() : {ready: false, parsing: false};
renderLnChapterPayload = payload => { window.rendered = payload; };
ui.lnBook = {id: 7, chapters: [{}, {}]};
const token = ++ui.lnChapterLoadToken;
const calls = name => __apiCalls.filter(call => call.name === name).length;
pollLnParse(7, 0, token);
await __tick(60000); await __flush();
const out = {statusCalls: calls('light_novel_chapter_parse_status'), timer: ui.lnParsePollTimer,
             notice: __visible('#lnParseNotice'), text: __text('#lnParseNotice span'), button: __text('#lnParseRetry'),
             expected: PudgeI18n.t('uiB.ln.wordLookupUnavailable'), retryLabel: PudgeI18n.t('media.retry')};
// Retry starts the parse again (a fresh chapter request) and polls until it is there.
window.parses.push({ready: false, parsing: true},
                   {ready: true, payload: {book_id: 7, chapter_index: 0, paragraphs: ['テキスト'], tokens: [[]], vocabulary: []}});
await __click('#lnParseRetry'); await __flush();
out.chapterCalls = calls('light_novel_chapter');
out.noticeAfterRetry = __visible('#lnParseNotice');
await __tick(3000); await __flush();
out.rendered = !!window.rendered;
out.statusCallsAfter = calls('light_novel_chapter_parse_status') - out.statusCalls;
out.noticeAtEnd = __visible('#lnParseNotice');
return out;
"""


def test_a_failed_parse_stops_polling_and_offers_retry(page) -> None:
    for lang in ("en", "ru"):
        out = page.run(FAILED_PARSE, lang=lang, before=SETUP).value
        # One status answer says nothing is coming: no more polls for the rest of the minute.
        assert out["statusCalls"] == 1 and out["timer"] is None
        assert out["notice"] is True
        assert out["text"] == out["expected"] and out["button"] == out["retryLabel"]
        assert out["chapterCalls"] == 1 and out["noticeAfterRetry"] is False
        assert out["rendered"] is True and out["statusCallsAfter"] == 2
        assert out["noticeAtEnd"] is False
    assert en("uiB.ln.wordLookupUnavailable") == "Word lookup is not available for this chapter right now."
    assert ru("uiB.ln.wordLookupUnavailable") == "Поиск слов для этой главы сейчас недоступен."


def test_failed_status_calls_are_retried_with_a_growing_pause(page) -> None:
    out = page.run(
        r"""
        let failures = 3;
        __api.light_novel_chapter_parse_status = async () => {
          if (failures-- > 0) throw new Error('bridge busy');
          return {ready: true, payload: {book_id: 7, chapter_index: 0, paragraphs: ['テキスト'], tokens: [[]], vocabulary: []}};
        };
        renderLnChapterPayload = payload => { window.rendered = payload; };
        ui.lnBook = {id: 7, chapters: [{}]};
        const token = ++ui.lnChapterLoadToken;
        const calls = () => __apiCalls.filter(call => call.name === 'light_novel_chapter_parse_status').length;
        pollLnParse(7, 0, token);
        const seen = [];
        for (const step of [500, 1400, 2800, 5600]) { await __tick(step); await __flush(); seen.push(calls()); }
        return {seen, rendered: !!window.rendered, notice: __visible('#lnParseNotice')};
        """,
        before=SETUP,
    ).value
    # 0.5 s, then pauses of 1.4 s, 2.8 s and 5.6 s after each failure; the fourth answer is the parse.
    assert out == {"seen": [1, 2, 3, 4], "rendered": True, "notice": False}


def test_another_chapter_drops_the_retry_line(page) -> None:
    out = page.run(
        r"""
        __api.light_novel_chapter_parse_status = async () => ({ready: false, parsing: false});
        ui.lnBook = {id: 7, chapters: [{}, {}]};
        pollLnParse(7, 0, ++ui.lnChapterLoadToken);
        await __tick(600); await __flush();
        const shown = __visible('#lnParseNotice');
        stopLnParsePoll();                                   // what opening another chapter does first
        return {shown, after: __visible('#lnParseNotice'), retry: ui.lnParseRetry};
        """,
        before=SETUP,
    ).value
    assert out == {"shown": True, "after": False, "retry": None}
