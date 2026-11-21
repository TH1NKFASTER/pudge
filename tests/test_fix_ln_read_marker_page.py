"""Light novel reader (Mac window): the "read up to here" marker, through the page harness.

"Read up to here" puts a small red marker exactly before the chosen word: a ribbon in the
margin by the line and a thin caret before the character, drawn over the text so its layout
never moves, in horizontal and vertical text and with furigana. The dwell save sends the start
of the first line wholly on screen and the Mac snaps automatic markers to their sentence
(tests/test_fix_ln_read_marker.py); the paired audiobook moves it forward only, once it is
300 characters ahead. Hovering the ribbon for a second says who set it and when (en/ru); a
"back to bookmark" button appears when it is off screen.

dom_lite has no layout: the test lays the text out on a grid (20 characters a line, 40 px
lines; vertical: 40 px columns from the right) through getClientRects/getBoundingClientRect.
"""

from __future__ import annotations

import json

from pudge import ln_read_marker
from test_fix_ln_read_marker import CHAPTER0, CHAPTER1, P1, P3, at

PARAGRAPHS0 = [line.strip() for line in CHAPTER0.split("\n") if line.strip()]
PARAGRAPHS1 = [line.strip() for line in CHAPTER1.split("\n") if line.strip()]
TOKENS0 = [
    [],
    [
        {
            "start": 0,
            "end": 1,
            "wordId": 1,
            "readingIndex": 0,
            "rubies": [{"start": 0, "end": 1, "text": "かれ"}],
        },
        {"start": 2, "end": 4, "wordId": 2, "readingIndex": 0},
        {"start": 7, "end": 9, "wordId": 3, "readingIndex": 0},
        {"start": 13, "end": 15, "wordId": 4, "readingIndex": 0},
    ],
    [],
    [],
    [
        {"start": 0, "end": 4, "wordId": 5, "readingIndex": 0},
        {"start": 5, "end": 6, "wordId": 6, "readingIndex": 0},
    ],
]
assert P1[7:9] == "待っ" and P3.encode("utf-16-le")[10:12].decode("utf-16-le") == "前"

WHEN = 1790856000.0  # the harness clock at the start: 2026-10-01 12:00 UTC

BOOK = r"""
window.MARKER = null;
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, read_marker: window.MARKER,
  chapters: [{chapter_index: 0, title: 'One'}, {chapter_index: 1, title: 'Two'}]});
const CHAPTERS = __CHAPTERS__;
__api.light_novel_chapter = async (id, index) => JSON.parse(JSON.stringify({book_id: id, chapter_index: index, ...CHAPTERS[index], vocabulary: []}));
window.SAVED = null;  // what the Mac answers to the next save
__api.light_novel_bookmark = async (id, chapter, offset, source, char) => ({ok: true, current_chapter: chapter, current_offset: offset,
  read_marker: window.SAVED ?? {chapter_index: chapter, char_offset: char, source, updated_at: __WHEN__, device_id: 'mac', device_name: '', auto_min_chars: 300},
  marker_moved: true});
__api.light_novel_move_read_marker = async (id, chapter, char, source) => ({ok: true, marker_moved: true,
  read_marker: {chapter_index: chapter, char_offset: char, source, updated_at: __WHEN__ + 60, device_id: 'mac', device_name: '', auto_min_chars: 300}});

// The grid layout. Horizontal: 20 characters a line, 40 px lines, paragraphs 20 px apart, the view
// 800 x 600 scrolled by LAYOUT.scroll. Vertical: 40 px columns from x = 760 leftwards, moved by LAYOUT.shift.
window.LAYOUT = {scroll: 0, shift: 0};
const vertical = () => document.getElementById('lnReader').classList.contains('vertical');
const lines = i => Math.max(1, Math.ceil(String(ui.lnChapter.paragraphs[i] || '').length / 20));
const before = i => { let n = 0; for (let j = 0; j < i; j++) n += lines(j); return n; };
const box = (x, y, width, height) => ({x, y, width, height, left: x, top: y, right: x + width, bottom: y + height});
window.charBox = (p, i) => vertical()
  ? box(760 - (before(p) + p + Math.floor(i / 20) + 1) * 40 + LAYOUT.shift, 50 + (i % 20) * 20, 30, 20)
  : box(100 + (i % 20) * 20, 50 + before(p) * 40 + p * 20 + Math.floor(i / 20) * 40 - LAYOUT.scroll, 20, 30);
const localOf = (p, node, offset) => {
  let n = 0, found = null;
  const walk = el => { for (const c of el.childNodes) { if (found !== null) return; if (c === node) { found = n + offset; return; }
    if (c.nodeType === 3) n += c.data.length; else if (c.nodeType === 1 && !/^(rt|rp)$/i.test(c.localName) && !c.classList.contains('ln-read-marker')) walk(c); } };
  walk(p); return found;
};
Range.prototype.getClientRects = function () {
  const p = this.startContainer.parentNode?.closest?.('p[data-ln-paragraph]');
  if (!p) return [];
  const i = localOf(p, this.startContainer, this.startOffset);
  return i === null ? [] : [charBox(Number(p.dataset.lnParagraph), i)];
};
const elementRect = Element.prototype.getBoundingClientRect;
Element.prototype.getBoundingClientRect = function () {
  if (this.id === 'lnReaderScroll') return box(0, 0, 800, 600);
  if (this.id === 'lnReader') return vertical() ? box(LAYOUT.shift, 0, 800, 600) : box(0, -LAYOUT.scroll, 800, 4000);
  if (this.matches?.('p[data-ln-paragraph]')) {
    const p = Number(this.dataset.lnParagraph);
    return vertical() ? box(760 - (before(p) + p + lines(p)) * 40 + LAYOUT.shift, 50, lines(p) * 40, 400)
                      : box(100, 50 + before(p) * 40 + p * 20 - LAYOUT.scroll, 400, lines(p) * 40);
  }
  return elementRect.call(this);
};
""".replace(
    "__CHAPTERS__",
    json.dumps(
        [
            {"text": CHAPTER0, "paragraphs": PARAGRAPHS0, "tokens": TOKENS0},
            {"text": CHAPTER1, "paragraphs": PARAGRAPHS1, "tokens": [[] for _ in PARAGRAPHS1]},
        ],
        ensure_ascii=False,
    ),
).replace("__WHEN__", str(WHEN))

VIEW = r"""
const open = async () => { await __settle(openLightNovel(6)); await __tick(400); };
const layer = () => document.querySelector('#lnReader .ln-read-marker');
const style = sel => { const s = layer().querySelector(sel).style; return [s.left, s.top, s.width, s.height]; };
const marker = () => { const l = layer(); return l && !l.hidden ? {char: Number(l.dataset.char), paragraph: Number(l.dataset.paragraph),
  local: Number(l.dataset.local), kind: l.dataset.kind, placed: l.dataset.placed, flash: l.classList.contains('flash')} : null; };
const word = (p, start) => [...document.querySelectorAll(`p[data-ln-paragraph="${p}"] [data-ln-token]`)].find(el => ui.lnTokenMap.get(el.dataset.lnToken)?.start === start);
const button = () => { const b = document.getElementById('lnReadMarkerJump'); return b && !b.hidden ? {side: b.dataset.side, text: b.textContent} : null; };
"""

VERTICAL = {"light_novel_state": {"version": 1, "settings": {"reader_vertical": True}, "books": []}}


def test_read_up_to_here_marks_exactly_the_word_without_moving_the_text(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open();
        const text = document.getElementById('lnReader').textContent;
        const target = word(1, 7);                                   // 待っ
        ui.lnPopupTarget = target;
        const pop = document.getElementById('lnStudyPop');
        pop.innerHTML = '<button data-ln-bookmark-here>Read up to here</button>';
        pop.classList.add('open');
        await __click('#lnStudyPop [data-ln-bookmark-here]');
        await __tick(50);
        const first = {marker: marker(), caret: style('.ln-read-marker-caret'), ribbon: style('.ln-read-marker-ribbon'),
                       sameText: document.getElementById('lnReader').textContent === text, toast: __toasts()};
        // A word after a two-unit kanji (𠮷): the character count and the drawn place agree.
        await __settle(saveLnBookmark('manual', word(4, 5)));        // 前
        return {first, second: marker(), caret: style('.ln-read-marker-caret')};
        """,
        before=BOOK,
    )
    out = result.value
    sent = result.calls_of("light_novel_bookmark")
    assert sent[0][3] == "manual" and sent[0][4] == at(CHAPTER0, "待")
    first = out["first"]
    assert first["marker"] == {
        "char": at(CHAPTER0, "待"),
        "paragraph": 1,
        "local": 7,
        "kind": "manual",
        "placed": "1",
        "flash": True,
    }
    # The caret right before 待 (line 0, column 7: x = 100 + 7 * 20); the ribbon in the margin by
    # that line, left of the paragraph (x = 100).
    assert first["caret"] == ["239.0px", "110.0px", "2.0px", "30.0px"]
    assert first["ribbon"] == ["81.0px", "114.5px", "11.0px", "21.0px"]
    assert first["sameText"] is True and first["toast"] == ["Bookmark and progress updated"]
    assert sent[1][4] == at(CHAPTER0, "前")
    assert (
        out["second"]["paragraph"] == 4
        and out["second"]["local"] == 5
        and out["second"]["char"] == at(CHAPTER0, "前")
    )
    assert out["caret"][0] == f"{100 + 5 * 20 - 1:.1f}px"


def test_the_dwell_save_sends_the_first_line_on_screen_and_draws_the_snapped_marker(page) -> None:
    long = at(CHAPTER0, "あ")  # paragraph 2 starts on line 2 of the grid (y = 50 + 2*40 + 2*20 = 170)
    sent = long + 5 * 20  # scrolled so its sixth line (y = 370) is the first wholly on screen
    snapped = ln_read_marker.sentence_start(CHAPTER0, sent)
    assert snapped == sent  # 100 characters into a 400-character sentence: the line start stays
    result = page.run(
        VIEW
        + r"""
        await open();
        window.MARKER = null;
        LAYOUT.scroll = 360;                       // line 5 of paragraph 2 (y = 330..360) is just above
        await __settle(saveLnBookmark('auto'));
        const drawn = marker();
        // The Mac keeps the marker (not far enough): no pulse, nothing moves.
        window.SAVED = ui.lnBook.read_marker;
        await __tick(2000);
        LAYOUT.scroll = 400;
        await __settle(saveLnBookmark('auto'));
        return {drawn, again: marker(), toast: __toasts()};
        """,
        before=BOOK,
    )
    out = result.value
    calls = result.calls_of("light_novel_bookmark")
    assert calls[0][3] == "auto" and calls[0][4] == sent
    assert (
        out["drawn"]["char"] == snapped and out["drawn"]["kind"] == "auto" and out["drawn"]["flash"] is True
    )
    assert out["drawn"]["paragraph"] == 2 and out["drawn"]["local"] == 100
    assert calls[1][4] == long + 6 * 20
    assert out["again"]["char"] == snapped and out["again"]["flash"] is False
    assert out["toast"] == []  # automatic saves are silent


def test_the_audio_moves_the_marker_forward_only_once_far_enough(page) -> None:
    start = at(CHAPTER0, "あ")
    result = page.run(
        VIEW
        + r"""
        window.MARKER = {chapter_index: 0, char_offset: %(start)d, source: 'manual', updated_at: %(when)s, device_id: 'mac', auto_min_chars: 300};
        await open();
        const reader = document.getElementById('lnReader');
        const paragraph = i => reader.querySelector(`p[data-ln-paragraph="${i}"]`);
        const asked = () => __apiCalls.filter(c => c.name === 'light_novel_move_read_marker').length;
        PudgeLnReadMarker.followAudio(word(1, 7));       // behind the marker
        PudgeLnReadMarker.followAudio(paragraph(2));     // at it
        await __tick(2500);
        const before = asked();
        // A word 300+ characters on (paragraph 4, after the long one): its line start goes to the Mac.
        PudgeLnReadMarker.followAudio(word(4, 5));
        await __flush(); await __tick(50);
        const moved = marker();
        PudgeLnReadMarker.followAudio(word(4, 5));       // within 2 s and not 300 past the new marker
        await __tick(50);
        return {before, after: asked(), moved};
        """
        % {"start": start, "when": WHEN},
        before=BOOK,
    )
    out = result.value
    assert out["before"] == 0
    calls = result.calls_of("light_novel_move_read_marker")
    assert out["after"] == 1 and calls[0][1:] == [
        0,
        at(CHAPTER0, "𠮷"),
        "auto",
    ]  # 前 is on the line that 𠮷 starts
    assert (
        out["moved"]["char"] == at(CHAPTER0, "𠮷")
        and out["moved"]["kind"] == "auto"
        and out["moved"]["flash"] is True
    )


def _tooltip(page, lang, marker):
    return page.run(
        VIEW
        + r"""
        window.MARKER = %(marker)s;
        await open();
        const ribbon = layer().querySelector('.ln-read-marker-ribbon');
        const tip = () => { const t = document.getElementById('lnReadMarkerTip'); return t && !t.hidden ? t.textContent : null; };
        await __fire('#lnReader .ln-read-marker-ribbon', 'pointerenter', {}, 'PointerEvent');
        await __tick(900);
        const early = tip();
        await __tick(150);
        const shown = tip();
        await __fire('#lnReader .ln-read-marker-ribbon', 'pointerleave', {}, 'PointerEvent');
        const time = new Date(MARKER.updated_at * 1000).toLocaleTimeString(PudgeI18n.lang(), {hour: '2-digit', minute: '2-digit'});
        return {early, shown, after: tip(), time, label: ribbon.getAttribute('aria-label')};
        """
        % {"marker": json.dumps(marker)},
        before=BOOK,
        lang=lang,
    )


def test_hovering_the_marker_for_a_second_says_who_set_it_and_when(page) -> None:
    manual = {
        "chapter_index": 0,
        "char_offset": at(CHAPTER0, "待"),
        "source": "manual",
        "updated_at": WHEN,
        "device_id": "mac",
    }
    en = _tooltip(page, "en", manual).value
    assert en["early"] is None and en["after"] is None
    assert en["shown"] == f"Read up to here · set by you · {en['time']}" == en["label"]
    phone = {**manual, "source": "auto", "device_id": "AB-12", "device_name": "Maksim iPhone"}
    ru = _tooltip(page, "ru", phone).value
    assert ru["shown"] == f"Прочитано досюда · автоматически · {ru['time']} · Maksim iPhone"
    assert ":" in ru["time"]


def test_vertical_text_draws_the_marker_above_the_character_and_its_column(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open();
        await __settle(saveLnBookmark('manual', word(1, 0)));   // 彼, with furigana (<ruby><rt>)
        const ruby = !!document.querySelector('p[data-ln-paragraph="1"] ruby rt');
        return {ruby, marker: marker(), vertical: layer().classList.contains('vertical'),
                caret: style('.ln-read-marker-caret'), ribbon: style('.ln-read-marker-ribbon')};
        """,
        before=BOOK,
        api=VERTICAL,
    )
    out = result.value
    assert out["ruby"] is True and out["vertical"] is True
    # The reading (rt) is not counted: 彼 is character 0 of paragraph 1.
    assert result.calls_of("light_novel_bookmark")[0][4] == at(CHAPTER0, "彼")
    assert out["marker"]["local"] == 0 and out["marker"]["placed"] == "1"
    # Paragraph 1 is column 2 from the right (x = 760 - 3 * 40 = 640): a 2 px bar over the
    # character (y = 50), the ribbon above the column (its top is y = 50).
    assert out["caret"] == ["640.0px", "49.0px", "30.0px", "2.0px"]
    assert out["ribbon"] == ["646.0px", "33.0px", "18.0px", "12.0px"]


def test_back_to_bookmark_when_the_marker_is_off_screen(page) -> None:
    result = page.run(
        VIEW
        + r"""
        window.MARKER = {chapter_index: 0, char_offset: %(word)d, source: 'manual', updated_at: %(when)s, device_id: 'mac'};
        await open();
        PudgeLnReadMarker.syncJump();
        const onScreen = button();
        LAYOUT.scroll = 900;                                       // read on: the marker is above
        document.getElementById('lnReaderScroll').dispatchEvent(new Event('scroll'));
        await __tick(200);
        const above = button();
        await __click('#lnReadMarkerJump');
        await __tick(50);
        const scrolled = [...(layer().querySelector('.ln-read-marker-caret').__scrolls || [])];
        // In another chapter: the button opens the marker's chapter.
        await __settle(loadLightNovelChapter(1, 0)); await __tick(400);
        const elsewhere = [button(), marker()];
        await __click('#lnReadMarkerJump');
        await __tick(600);
        return {onScreen, above, scrolled, elsewhere, chapter: ui.lnChapter.chapter_index, back: marker()};
        """
        % {"word": at(CHAPTER0, "待"), "when": WHEN},
        before=BOOK,
    )
    out = result.value
    assert out["onScreen"] is None
    assert out["above"] == {"side": "before", "text": "↑Back to bookmark"}
    assert out["scrolled"] == [{"block": "center", "inline": "center", "behavior": "smooth"}]
    assert (
        out["elsewhere"][0] == {"side": "before", "text": "↑Back to bookmark"} and out["elsewhere"][1] is None
    )
    assert out["chapter"] == 0 and out["back"]["char"] == at(CHAPTER0, "待")


def test_a_marker_moved_on_the_phone_shows_when_the_window_is_back(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open();
        const none = marker();
        window.FROM_PHONE = {chapter_index: 0, char_offset: %(word)d, source: 'manual', updated_at: %(when)s, device_id: 'AB', device_name: 'iPad'};
        __api.light_novel_read_marker = async id => ({ok: true, read_marker: window.FROM_PHONE});
        window.dispatchEvent(new Event('focus'));
        await __flush(); await __tick(20);
        const shown = marker();
        // The chapter is drawn again (its words arrived): the marker is drawn with it.
        renderLnChapterPayload(ui.lnChapter);
        const again = marker();
        return {none, shown, again};
        """
        % {"word": at(CHAPTER0, "叫"), "when": WHEN},
        before=BOOK,
    )
    out = result.value
    assert out["none"] is None
    assert out["shown"]["char"] == at(CHAPTER0, "叫") and out["shown"]["flash"] is True
    assert out["again"]["char"] == at(CHAPTER0, "叫") and out["again"]["placed"] == "1"
    assert result.calls_of("light_novel_read_marker") == [[6]]


def test_the_dwell_save_in_vertical_text_sends_the_rightmost_whole_column(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open();
        LAYOUT.shift = 400;   // read leftwards: the long paragraph's first five columns are past the right edge
        await __settle(saveLnBookmark('auto'));
        return marker();
        """,
        before=BOOK,
        api=VERTICAL,
    )
    assert result.calls_of("light_novel_bookmark")[0][4] == at(CHAPTER0, "あ") + 5 * 20
    assert result.value["paragraph"] == 2 and result.value["local"] == 100
