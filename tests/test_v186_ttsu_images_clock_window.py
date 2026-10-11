"""Light novels: blurred illustrations as ッツ (ttsu) shows them, and the reader's clock
between two answers of the backend (behaviour; audit 71Б rewrite).

The reader runs in node (tests/page_harness.py). This file also covers the illustration
rules of v1.83 and v1.84 (the same image element, loaded at once, revealed in place).
Turning blurring on and off in the reader settings, and the click-to-reveal contract
with pointer events: test_v137_pair_blur_images.py."""

from __future__ import annotations

import pytest

from t71g21_support import computed_style

# Light novel 6 with an illustration between two paragraphs; the reader blurs images.
ILLUSTRATED = r"""
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, tokens: [[], [], []],
  paragraphs: ['前の本文です。', '[[PUDGE_LN_IMAGE_URL:/covers/pic.jpg]]', '後ろの本文です。']});
"""
BLURRING = {
    "light_novel_state": {
        "version": 1,
        "settings": {"blur_images": True},
        "books": [{"id": 6, "title": "Novel", "volume": 1}],
    }
}


def test_blurred_illustration_is_the_real_image_revealed_in_place_then_zoomed(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            computed_style(
                "filter", "cursor", "overflow", "contain", "transform", "backface-visibility", "will-change"
            )
            + r"""
            await __settle(openLightNovel(6)); await __tick(300);
            const figure = document.querySelector('#lnReader figure.ln-inline-image'), img = figure.querySelector('img');
            const frame = figure.querySelector('.ln-inline-image-frame');
            const look = () => {
              const shown = figure.querySelector('img');
              return {same: shown === img, inFrame: shown.parentElement === frame, src: shown.getAttribute('src'), loading: shown.getAttribute('loading'),
                      revealed: figure.classList.contains('revealed'), label: __visible('#lnReader .ln-inline-image-spoiler-label'),
                      filter: __css(shown, 'filter'), cursor: __css(shown, 'cursor'), preview: PudgeCoverPreview.isOpen(),
                      parts: [...figure.querySelectorAll('*')].map(n => n.className || n.localName), canvases: document.querySelectorAll('#lnReader canvas').length};
            };
            const out = {label: __text('#lnReader .ln-inline-image-spoiler-label'), expected: t('ui.showIllustration'),
              frame: Object.fromEntries(['overflow', 'contain', 'transform', 'backface-visibility', 'will-change'].map(p => [p, __css(frame, p)]))};
            out.blurred = look();
            await __click('#lnReader .ln-inline-image img'); await __tick(50);
            out.revealed = look();
            await __click('#lnReader .ln-inline-image img'); await __tick(50);
            out.zoomed = {...look(), previewSrc: document.querySelector('.pudge-cover-preview img')?.getAttribute('src')};
            PudgeCoverPreview.close(); await __tick(50);
            // An image that lost its source (an older reader page) is loaded again by a click.
            img.removeAttribute('src');
            await __click('#lnReader .ln-inline-image img'); await __tick(50);
            out.recovered = {src: img.getAttribute('src'), preview: PudgeCoverPreview.isOpen()};
            return out;
            """,
            lang=lang,
            before=ILLUSTRATED,
            api=BLURRING,
            state={"settings": {"review_gate_ln_enabled": False}},
        )
        out = result.value
        assert out["label"] == out["expected"]
        # The blur is drawn over the very <img> of the page, already loading (not a canvas
        # copy or a placeholder that loads the picture only when it is revealed).
        assert out["blurred"] == {
            "same": True,
            "inFrame": True,
            "src": "/covers/pic.jpg",
            "loading": "eager",
            "revealed": False,
            "label": True,
            "filter": "blur(44px)",
            "cursor": "pointer",
            "preview": False,
            "parts": ["ln-inline-image-frame", "ln-inline-image-spoiler-label", "img"],
            "canvases": 0,
        }
        # The first click only removes the blur, from the same image.
        assert out["revealed"] == {
            **out["blurred"],
            "revealed": True,
            "label": False,
            "filter": None,
            "cursor": "zoom-in",
        }
        # The next one zooms it.
        assert out["zoomed"]["preview"] is True and out["zoomed"]["previewSrc"] == "/covers/pic.jpg"
        assert out["recovered"] == {"src": "/covers/pic.jpg", "preview": False}
        # The frame clips the blur to the picture, as ッツ does; none of the compositing
        # hints (contain, 3D transform, backface) of the removed canvas pipeline.
        assert out["frame"] == {
            "overflow": "hidden",
            "contain": None,
            "transform": None,
            "backface-visibility": None,
            "will-change": None,
        }


# Anchor window of a poll answer (real trace, Spice and Wolf II 第三幕): seven anchors well
# past the poll interval; speech activity covers it all.
WINDOW = r"""
const lookahead = {chapter_char_offset_exact: 125, anchor_window: {
  left_time: 14084.195, left_offset: 125, right_time: 14084.415, right_offset: 126, activity_clock: true,
  activity: [{start: 14084.195, end: 14085.935}],
  path: [{time: 14084.195, offset: 125}, {time: 14084.415, offset: 126}, {time: 14084.635, offset: 127}, {time: 14084.895, offset: 128},
         {time: 14085.455, offset: 130}, {time: 14085.635, offset: 131}, {time: 14085.935, offset: 133}]}};
// The same window with a real pause in speech (14085.2-14085.4).
const paused = {...lookahead, anchor_window: {...lookahead.anchor_window, activity: [{start: 14084.195, end: 14085.2}, {start: 14085.4, end: 14085.935}]}};
const at = (state, times) => times.map(time => lnPairedOffsetAtTime(state, time));
"""


def test_reader_clock_runs_along_the_lookahead_window_and_holds_only_in_real_pauses(page) -> None:
    result = page.run(
        WINDOW
        + r"""
        return {smooth: at(lookahead, [14084.30, 14084.50, 14084.60, 14084.75, 14085.80, 14086.5]),
                paused: at(paused, [14085.0, 14085.2, 14085.3, 14085.4, 14085.5])};
        """
    )
    smooth, paused = result.value["smooth"], result.value["paused"]
    # Beyond the window's first segment (offset 126) the text keeps moving, smoothly.
    assert 125 < smooth[0] < 126
    assert 126 < smooth[1] < smooth[2] < 127
    assert 127 < smooth[3] < 128
    assert 131 < smooth[4] < 133 and smooth[5] == 133
    # Inside the pause the clock holds; it moves on when speech resumes.
    assert paused[0] < paused[1] == paused[2] == paused[3] < paused[4]


def test_stale_poll_behind_the_reader_clock_does_not_move_the_highlight_back(page) -> None:
    # mpv reports positions up to 1.5 s behind the reader's clock right after a start or a
    # seek: the clock keeps running (no cap on its lead); a bigger difference is a real
    # jump back (a seek elsewhere) and is followed at once.
    result = page.run(
        r"""
        const state = {position: 100, speed: 1, playing: true};
        ui.lnPairedState = {...state};
        lnPairedTransportClockReset(state);
        await __tick(250);
        lnPairedTransportClockReconcile({...state, position: 100.25}, {settled: true});     // mpv is playing
        await __tick(1000);
        const stale = lnPairedTransportClockReconcile({...state, position: 100.05}, {settled: true});
        await __tick(250);
        const running = lnPairedTransportClockNow(state);
        const jump = lnPairedTransportClockReconcile({...state, position: 99.9}, {settled: true});
        return {stale, running, jump, after: lnPairedTransportClockNow(state)};
        """
    )
    out = result.value
    assert out["stale"]["position"] == pytest.approx(101.25) and out["stale"]["snap"] is False
    assert out["running"] == pytest.approx(101.5)
    assert out["jump"]["position"] == 99.9 and out["jump"]["snap"] is True
    assert out["after"] == 99.9
