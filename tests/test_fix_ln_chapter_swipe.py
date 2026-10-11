"""Light novel reader: scrolling on past the end of a chapter opens the next one (and past
the start the previous one, at its end), like Firefox's two-finger back/forward swipe: short
travel, a quick flick turns the chapter at once, a slow pull when the gesture ends full, and
short of that the indicator springs straight back. Vertical text: chapters open at their start (the right
edge) and only sideways scrolling swipes. Reader settings apply to every chapter opened later.
Behaviour through the page harness: wheel events with deltas on the fake clock."""

from __future__ import annotations

from css_support import css_rules

BOOK = r"""
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0,
  chapters: [{chapter_index: 0, title: 'One'}, {chapter_index: 1, title: 'Two'}, {chapter_index: 2, title: 'Three'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index,
  paragraphs: [`章${index}`], tokens: [[]], vocabulary: []});
"""

VERTICAL = {"light_novel_state": {"version": 1, "settings": {"reader_vertical": True}, "books": []}}

VIEW = r"""
const el = document.getElementById('lnReaderScroll'), reader = document.getElementById('lnReader');
const open = async (chapter = 0) => {
  await __settle(openLightNovel(6)); await __tick(400);
  if (chapter) { await __settle(loadLightNovelChapter(chapter, 0)); await __tick(400); }
};
// Horizontal text: 3000 px of text in a 600 px window.
const tall = top => { el.clientHeight = 600; el.clientWidth = 800; el.scrollHeight = 3000; el.scrollWidth = 800; el.scrollTop = top; el.scrollLeft = 0; };
// Vertical text 4000 px wide in an 800 px window. The reader's box follows scrollLeft. Engine A
// counts scrollLeft 0..3200 with the start (right edge) at 3200; engine B 0..-3200, start at 0.
window.ENGINE = 'A';
const place = () => { reader.__rect = {x: (ENGINE === 'A' ? 0 : -3200) - el.scrollLeft, y: 0, width: 4000, height: 600}; };
const wide = left => {
  el.__rect = {x: 0, y: 0, width: 800, height: 600};
  el.clientHeight = 600; el.clientWidth = 800; el.scrollHeight = 600; el.scrollWidth = 4000; el.scrollTop = 0; el.scrollLeft = left; place();
};
const wheel = async (deltaY, deltaX = 0, gap = 16) => {
  const event = new WheelEvent('wheel', {deltaX, deltaY, bubbles: true, cancelable: true});
  el.dispatchEvent(event); await __tick(gap); return event.defaultPrevented;
};
const stream = async (count, deltaY, deltaX = 0) => { for (let i = 0; i < count; i++) await wheel(deltaY, deltaX); };
const chip = () => {
  const node = document.getElementById('lnChapterSwipe');
  if (!node || node.hidden) return null;
  return {side: node.dataset.side, label: node.querySelector('.ln-chapter-swipe-label').textContent,
          ready: node.classList.contains('ready'), shift: parseFloat(node.style.getPropertyValue('--ln-swipe-shift'))};
};
const chapter = () => Number(ui.lnChapter?.chapter_index);
const text = () => reader.innerText.trim();
"""


def test_a_slow_pull_turns_the_chapter_as_soon_as_the_gesture_ends_full(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open();
        tall(2400);                      // at the bottom of chapter One
        await __tick(400);               // a fresh gesture: the finger rests first
        const consumed = await wheel(10);
        await stream(2, 10);
        const partial = chip();          // 30 px of 72
        await stream(5, 10);             // 80 px: full
        const ready = chip();
        await __tick(90);                // the fingers may still move: nothing decided yet
        const waiting = [chapter(), reader.classList.contains('ln-chapter-swipe-out')];
        await __tick(30);                // 120 ms without movement: the gesture is over
        const sliding = reader.classList.contains('ln-chapter-swipe-out');
        await __tick(600);
        return {consumed, partial, ready, waiting, sliding, chapter: chapter(), text: text(), top: el.scrollTop,
                select: document.getElementById('lnChapterSelect').value, after: chip(), classes: reader.className};
        """,
        before=BOOK,
    )
    out = result.value
    assert out["consumed"] is True
    assert out["partial"]["side"] == "bottom" and out["partial"]["label"] == "Next chapter: Two"
    assert out["partial"]["ready"] is False and 0 < out["partial"]["shift"] < out["ready"]["shift"]
    # The chip follows one to one: 30 of 72 px moved it 30/72 of its 88 px travel.
    assert abs(out["partial"]["shift"] - 88 * 30 / 72) < 0.2
    assert out["ready"]["ready"] is True
    assert out["waiting"] == [0, False]
    assert out["sliding"] is True
    assert (out["chapter"], out["text"], out["top"], out["select"]) == (1, "章1", 0, "1")
    assert out["after"] is None
    assert "ln-chapter-swipe" not in out["classes"]
    # Chapter Two came from the look-ahead cache; Three is prefetched in turn.
    assert [args[1] for args in result.calls_of("light_novel_chapter")] == [0, 1, 2]


def test_a_quick_flick_turns_the_chapter_at_once_and_its_fling_stays_with_the_swipe(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open();
        tall(2400); await __tick(400);
        await wheel(30, 0, 0); await __tick(16);
        await wheel(40, 0, 0);           // 70 px in 16 ms: a flick (short of 72 px)
        const sliding = reader.classList.contains('ln-chapter-swipe-out');
        await __tick(16);
        const fling = [];
        for (const delta of [30, 20, 12, 6, 3]) fling.push(await wheel(delta, 0, 40));
        const during = chapter();
        await __tick(600);
        const later = await wheel(20);   // a new scroll in chapter Two is an ordinary scroll
        return {sliding, fling, during, chapter: chapter(), later};
        """,
        before=BOOK,
    )
    out = result.value
    assert out["sliding"] is True
    # The rest of the fling neither scrolls the next chapter nor swipes again.
    assert out["fling"] == [True, True, True, True, True]
    assert out["chapter"] == 1 and out["later"] is False


def test_one_mouse_wheel_notch_does_not_turn_the_chapter(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open();
        tall(2400); await __tick(400);
        await wheel(120);
        const shown = chip();
        await __tick(600);
        return {shown, after: chip(), chapter: chapter()};
        """,
        before=BOOK,
    )
    out = result.value
    assert out["shown"]["ready"] is False
    assert out["after"] is None and out["chapter"] == 0


def test_short_of_the_threshold_it_springs_back_right_after_the_gesture(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open();
        tall(2400); await __tick(400);
        await stream(4, 10);             // 40 px of 72, slowly
        await __tick(70);
        const holding = chip() && !document.getElementById('lnChapterSwipe').classList.contains('springing');
        await __tick(60);                // the gesture ended: springs back now
        const springing = document.getElementById('lnChapterSwipe').classList.contains('springing');
        await __tick(300);
        const after = chip();
        await __tick(400);
        // Pulling back into the text is an ordinary scroll again.
        await wheel(10); await wheel(-5); const pulled = chip(); const back = await wheel(-30);
        await __tick(600);
        return {holding, springing, after, pulled, back, gone: chip(), chapter: chapter()};
        """,
        before=BOOK,
    )
    out = result.value
    assert out["holding"] is True and out["springing"] is True and out["after"] is None
    assert out["pulled"]["ready"] is False and out["back"] is False and out["gone"] is None
    assert out["chapter"] == 0


def test_momentum_that_reached_the_end_does_not_swipe(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open();
        tall(2000);                      // the scroll starts inside the text ...
        await __tick(400);
        await wheel(40);
        tall(2400);                      // ... and its inertia carries on at the end
        await stream(10, 40);
        const momentum = chip();
        await __tick(400);               // a pause, then a fresh swipe
        await stream(5, 40);
        await __tick(1000);
        return {momentum, chapter: chapter()};
        """,
        before=BOOK,
    )
    assert result.value == {"momentum": None, "chapter": 1}


def test_vertical_text_swipes_with_sideways_scrolling_only(page) -> None:
    for engine, end in (("A", 0), ("B", -3200)):
        result = page.run(
            VIEW
            + f"window.ENGINE = '{engine}';"
            + r"""
            await open();
            wide(END);                       // the left end of the text: the end of chapter One
            await __tick(400);
            const right = await wheel(0, 50);   // rightwards: back into the text
            await __tick(400);
            const down = await wheel(40); await stream(6, 40);   // the wheel down does not swipe
            await __tick(1500);
            const afterWheel = [chip(), chapter()];
            await stream(4, 0, -40);         // trackpad leftwards = forward
            const shown = chip();
            await __tick(1000);
            return {vertical: reader.classList.contains('vertical'), right, down, afterWheel, shown, chapter: chapter()};
            """.replace("END", str(end)),
            before=BOOK,
            api=VERTICAL,
        )
        out = result.value
        assert out["vertical"] is True and out["right"] is False, engine
        assert out["down"] is False and out["afterWheel"] == [None, 0], engine
        assert out["shown"]["side"] == "left" and out["shown"]["label"] == "Next chapter: Two", engine
        assert out["chapter"] == 1, engine


def test_vertical_chip_sits_inside_the_visible_reader_box(page) -> None:
    # The reader's scroll box is larger than the window here: the chip's layer is the part of
    # the box inside the window, and the chip sits at its left / right edge, vertically centred.
    result = page.run(
        VIEW
        + r"""
        await open(1);
        wide(0); el.__rect = {x: -40, y: 60, width: 1600, height: 900}; await __tick(400);
        await stream(2, 0, -10);
        const node = document.getElementById('lnChapterSwipe');
        const next = {side: node.dataset.side, box: ['left', 'top', 'width', 'height'].map(k => node.style[k])};
        await __tick(600);
        wide(3200); el.__rect = {x: 0, y: 60, width: 800, height: 600}; await __tick(400);
        await stream(2, 0, 10);
        const previous = {side: node.dataset.side, box: ['left', 'top', 'width', 'height'].map(k => node.style[k])};
        return {next, previous};
        """,
        before=BOOK,
        api=VERTICAL,
        viewport={"width": 1000, "height": 700},
    )
    assert result.value["next"] == {"side": "left", "box": ["0px", "60px", "1000px", "640px"]}
    assert result.value["previous"] == {"side": "right", "box": ["0px", "60px", "800px", "600px"]}
    rules = css_rules("ln_chapter_swipe.css")
    for side in ("left", "right"):
        chip = rules.declarations(f'.ln-chapter-swipe[data-side="{side}"] .ln-chapter-swipe-indicator')
        # Anchored inside the box at its own edge (not outside it), centred on the height.
        assert chip[side] == "16px" and chip["top"] == "50%", side
        assert {"left", "right"} - {side} & chip.keys() == set(), side
        # It moves in at most 16 px back towards that edge: never past it.
        assert "16px" in chip["transform"] and "var(--ln-swipe-progress)" in chip["transform"], side
    indicator = rules.declarations(".ln-chapter-swipe-indicator")
    assert "calc(100% - 32px)" in indicator["max-width"] and indicator["white-space"] == "nowrap"
    assert rules.get(".ln-chapter-swipe-label", "text-overflow") == "ellipsis"
    assert rules.get(".ln-chapter-swipe", "overflow") == "hidden"


def test_vertical_text_away_from_the_left_end_scrolls_normally(page) -> None:
    for engine, middle in (("A", 1200), ("B", -1200), ("A", 3200), ("B", 0)):
        result = page.run(
            VIEW
            + f"window.ENGINE = '{engine}';"
            + r"""
            await open();
            wide(MIDDLE); await __tick(400);
            const consumed = await wheel(0, -40);
            await stream(6, 0, -40);
            await __tick(1000);
            return {consumed, chip: chip(), chapter: chapter()};
            """.replace("MIDDLE", str(middle)),
            before=BOOK,
            api=VERTICAL,
        )
        assert result.value == {"consumed": False, "chip": None, "chapter": 0}, (engine, middle)


def test_vertical_scroll_mode_opens_chapters_at_their_start_and_restores_positions(page) -> None:
    for engine, start, end in (("A", 3200, 0), ("B", 0, -3200)):
        result = page.run(
            VIEW
            + f"window.ENGINE = '{engine}';"
            + r"""
            wide(END);                       // the window was left at the end of something
            await open();
            const opened = el.scrollLeft;
            el.scrollLeft = (START + END) / 2; place();
            const middle = lnReaderOffset();
            await __settle(loadLightNovelChapter(1, 0)); await __tick(100); place();
            const next = el.scrollLeft;
            await __settle(loadLightNovelChapter(0, 1)); await __tick(100); place();
            const previousAtEnd = el.scrollLeft;
            restoreLnReaderOffset(0.25); await __tick(100);
            return {opened, middle, next, previousAtEnd, quarter: el.scrollLeft, top: el.scrollTop};
            """.replace("START", str(start)).replace("END", str(end)),
            before=BOOK,
            api=VERTICAL,
        )
        out = result.value
        assert out["opened"] == start and out["next"] == start, engine
        assert out["middle"] == 0.5, engine
        assert out["previousAtEnd"] == end, engine
        assert out["quarter"] == start - 800 and out["top"] == 0, engine


def test_vertical_swipe_back_opens_the_previous_chapter_at_its_end(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open(1);
        wide(3200); await __tick(400);       // the start (right edge) of chapter Two
        await stream(4, 0, 40);              // trackpad rightwards = backwards
        const shown = chip();
        await __tick(1000); place();
        return {shown, chapter: chapter(), left: el.scrollLeft};
        """,
        before=BOOK,
        api=VERTICAL,
    )
    out = result.value
    assert out["shown"]["side"] == "right" and out["shown"]["label"] == "Previous chapter: One"
    assert out["chapter"] == 0 and out["left"] == 0


def test_layout_settings_apply_to_every_chapter_opened_later(page) -> None:
    # The backend's chapter payload carries the settings of the moment it was fetched.
    result = page.run(
        VIEW
        + r"""
        __api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index,
          paragraphs: [`章${index}`], tokens: [[]], vocabulary: [], settings: {reader_vertical: false}});
        await open(1);                       // chapters 1 and 2 are cached, 0 was read before
        await __click('#lnrVertical');       // vertical text switched on in chapter Two
        await __tick(50);
        const here = reader.classList.contains('vertical');
        await __settle(loadLightNovelChapter(0, 0)); await __tick(100);
        const previous = reader.classList.contains('vertical');
        await __settle(loadLightNovelChapter(2, 0)); await __tick(100);
        const prefetched = reader.classList.contains('vertical');
        return {here, previous, prefetched};
        """,
        before=BOOK,
    )
    assert result.value == {"here": True, "previous": True, "prefetched": True}


def test_last_chapter_shows_a_notice_instead(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open(2);
        tall(2400); await __tick(400);
        await stream(8, 40);
        const during = chip();
        await __tick(600);
        const held = chip();
        await __tick(1200);
        return {during, held, after: chip(), chapter: chapter()};
        """,
        before=BOOK,
    )
    out = result.value
    assert out["during"]["label"] == "Last chapter" and out["during"]["ready"] is False
    assert out["held"]["label"] == "Last chapter"
    assert out["after"] is None and out["chapter"] == 2


def test_scrolling_up_past_the_start_opens_the_previous_chapter_at_its_end(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open(1);
        tall(0); await __tick(400);
        await stream(5, -40);
        const shown = chip();
        await __tick(1000);
        return {shown, chapter: chapter(), top: el.scrollTop};
        """,
        before=BOOK,
    )
    out = result.value
    assert out["shown"]["side"] == "top" and out["shown"]["label"] == "Previous chapter: One"
    assert out["chapter"] == 0 and out["top"] == 2400


def test_no_swipe_while_selecting_text(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open();
        tall(2400); await __tick(400);
        await __fire('#lnReaderScroll', 'pointerdown', {}, 'PointerEvent');   // mouse held: selecting
        await stream(6, 40);
        await __tick(1000);
        return {chip: chip(), chapter: chapter()};
        """,
        before=BOOK,
    )
    assert result.value == {"chip": None, "chapter": 0}


def test_reduced_motion_opens_without_sliding(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open();
        tall(2400); await __tick(400);
        await stream(5, 40);
        await __tick(320);
        const sliding = reader.classList.contains('ln-chapter-swipe-out');
        await __tick(300);
        return {sliding, chapter: chapter()};
        """,
        before=BOOK,
        media={"(prefers-reduced-motion: reduce)": True},
    )
    assert result.value == {"sliding": False, "chapter": 1}


def test_swiping_back_right_after_a_swipe_needs_no_pause_or_click(page) -> None:
    # The trackpad's momentum from the first swipe runs straight into the swipe back.
    result = page.run(
        VIEW
        + r"""
        await open();
        tall(2400); await __tick(400);
        await wheel(30, 0, 0); await __tick(16);
        await wheel(40, 0, 0);                    // flick: chapter Two
        for (let i = 0; i < 20; i++) await wheel(Math.max(1, 12 - i * 0.5), 0, 40);  // its fling
        const opened = chapter();
        tall(0);
        const back = [];
        for (let i = 0; i < 5; i++) back.push(await wheel(-40));  // no pause: back
        const shown = chip();
        await __tick(1000);
        return {opened, back, shown, chapter: chapter()};
        """,
        before=BOOK,
    )
    out = result.value
    assert out["opened"] == 1
    assert out["back"] == [True] * 5
    assert out["shown"]["label"] == "Previous chapter: One"
    assert out["chapter"] == 0


def test_a_new_faster_scroll_after_the_fling_scrolls_the_new_chapter(page) -> None:
    result = page.run(
        VIEW
        + r"""
        await open();
        tall(2400); await __tick(400);
        await wheel(30, 0, 0); await __tick(16);
        await wheel(40, 0, 0);
        for (let i = 0; i < 20; i++) await wheel(Math.max(1, 6 - i * 0.25), 0, 40);
        tall(0);
        const slow = await wheel(2, 0, 40);       // still the fling: kept from the new chapter
        const fast = await wheel(30);             // fingers again: an ordinary scroll
        return {slow, fast, chapter: chapter()};
        """,
        before=BOOK,
    )
    assert result.value == {"slow": True, "fast": False, "chapter": 1}
