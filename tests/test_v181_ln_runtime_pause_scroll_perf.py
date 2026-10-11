"""v1.81: the narrator's comma pause in an old cached alignment, the scroll performance trace
(behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pudge.reading_audio_alignment import (
    _punctuation_boundaries,
    audio_position_for_light_novel_offset,
    chapter_audio_text,
    light_novel_position_for_audio,
)
from t71g19_support import LN_PAIRED, OPEN
from t71g22_support import USER_SCROLL, ln_audio_pair


def _old_cached_second_act_alignment() -> dict:
    text = (
        "緩やかに下る坂も終わり、しばらくは土地の起伏といえば"
        "申し訳程度の小さい丘、という実に進みやすい道だった。"
    )
    boundaries = _punctuation_boundaries(chapter_audio_text(text))
    assert {"offset": 11, "strength": 1} in boundaries
    return {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 1,
                "title": "第二幕",
                "normalized_length": 100,
                "start": 6852.494,
                "end": 6900.0,
                # Shape from the user's v180 trace: old cache has no pause
                # anchors inside the first prose span.
                "anchors": [
                    {"offset": 34.999, "time": 6861.82},
                    {"offset": 49, "time": 6864.7},
                ],
                "speech_regions": [
                    {"start": 6852.68, "end": 6853.62},
                    {"start": 6856.2, "end": 6858.66},
                    {"start": 6859.3, "end": 6861.82},
                    {"start": 6862.62, "end": 6864.7},
                ],
                "leading_prefix_debug": {
                    "attempted": True,
                    "recovered": True,
                    "chapter_marker": True,
                    "marker_start": 6852.494,
                    "marker_end": 6853.594,
                    "story_start": 6856.134,
                },
                "punctuation_pause_count": 0,
                "_runtime_punctuation_boundaries": boundaries,
            }
        ],
    }


def test_old_cached_second_act_recovers_comma_pause_before_shibaraku() -> None:
    alignment = _old_cached_second_act_alignment()

    # v180 used to reach offset 11 / しばらく around 6857.96 because it
    # linearly interpolated 0 -> 34.999 across the whole 5.686-second span.
    early = light_novel_position_for_audio(alignment, 6857.957)
    assert early is not None
    assert float(early["chapter_char_offset_exact"]) < 9.0

    # The VAD gap is 6858.66 -> 6859.30 and the comma is exactly offset 11.
    # Hold at 10.999 throughout the gap, then begin しばらく at 6859.30.
    hold = light_novel_position_for_audio(alignment, 6858.9)
    assert hold is not None
    assert 10.998 <= float(hold["chapter_char_offset_exact"]) <= 10.9991
    assert int(hold["anchor_window"]["runtime_punctuation_pause_count"]) >= 1

    resumed = light_novel_position_for_audio(alignment, 6859.3)
    assert resumed is not None
    assert abs(float(resumed["chapter_char_offset_exact"]) - 11.0) < 1e-6

    # "Play from here" must use the same recovered runtime clock.
    assert abs(float(audio_position_for_light_novel_offset(alignment, 1, 11) or 0.0) - 6859.3) < 1e-6


def test_old_alignment_cache_gets_the_books_punctuation_pauses(tmp_path: Path) -> None:
    # An alignment cached before pause anchors existed: when it is loaded for the reader,
    # the book's own text supplies the punctuation, so the highlight waits at the comma
    # during the narrator's pause (6858.66-6859.30) instead of crawling through it.
    text = (
        "緩やかに下る坂も終わり、しばらくは土地の起伏といえば"
        "申し訳程度の小さい丘、という実に進みやすい道だった。"
    )
    _ln, audio, ln_id, audio_id = ln_audio_pair(tmp_path, ["小高い丘が延々と続く。", text], duration=7000.0)
    cached = _old_cached_second_act_alignment()
    del cached["chapters"][0]["_runtime_punctuation_boundaries"]
    alignment_file = audio._alignment_path(ln_id, audio_id)
    alignment_file.parent.mkdir(parents=True, exist_ok=True)
    alignment_file.write_text(json.dumps(cached), encoding="utf-8")
    offsets = {}
    for position in (6857.957, 6858.9, 6859.3):
        audio.set_position(audio_id, position)
        state = audio.paired_state(ln_id)
        assert (state["ln_chapter_index"], state["alignment_mode"]) == (1, "stt")
        offsets[position] = state["chapter_char_offset_exact"]
    assert offsets[6857.957] < 9.0
    assert 10.998 <= offsets[6858.9] <= 10.9991
    assert offsets[6859.3] == pytest.approx(11.0)


PERF_BOOK = (
    LN_PAIRED
    + r"""
// The window's performance observers (WebKit supports long tasks and layout shifts) and its
// intersection observers, reachable by the test; the chapter ends with an illustration.
window.PERF_OBSERVERS = [];
window.PerformanceObserver = class { constructor(callback) { this.callback = callback; PERF_OBSERVERS.push(this); } observe(options) { this.type = options.type; } disconnect() {}
  static get supportedEntryTypes() { return ['longtask', 'layout-shift', 'paint']; } };
const PlainIntersectionObserver = window.IntersectionObserver;
window.INTERSECTION_OBSERVERS = [];
window.IntersectionObserver = class extends PlainIntersectionObserver { constructor(...args) { super(...args); INTERSECTION_OBSERVERS.push(this); } };
const plainChapter = __api.light_novel_chapter;
__api.light_novel_chapter = async (id, index) => { const chapter = await plainChapter(id, index); chapter.paragraphs.push('[[PUDGE_LN_IMAGE_URL:/covers/pic.jpg]]'); chapter.tokens.push([]); return chapter; };
__api.light_novel_state = async () => ({version: 1, settings: {blur_images: true}, books: []});
"""
)


def test_scroll_performance_trace_is_exported_with_the_sync_trace(page) -> None:
    # Scrolling a chapter with an illustration on a slow machine (60 ms frames, a long task,
    # a layout shift); ⌘⇧L exports the scroll measurements next to the listen-together
    # trace. The frame sampling never measures the pictures itself: what is visible comes
    # from the intersection observer.
    result = page.run(
        OPEN
        + USER_SCROLL
        + r"""
        await __tick(600);
        const figure = document.querySelector('#lnReader .ln-inline-image'), img = figure.querySelector('img');
        img.__naturalWidth = 800; img.__naturalHeight = 600;
        INTERSECTION_OBSERVERS.at(-1).__fire([{target: figure, intersectionRatio: 0.5}]);
        let measured = 0;
        const rect = Element.prototype.getBoundingClientRect, frame = window.requestAnimationFrame;
        Element.prototype.getBoundingClientRect = function () { if (this.closest?.('.ln-inline-image')) measured++; return rect.call(this); };
        window.requestAnimationFrame = callback => setTimeout(() => callback(performance.now()), 60);
        await userScroll(600);
        PERF_OBSERVERS.find(o => o.type === 'longtask').callback({getEntries: () => [{startTime: 1700, duration: 120}]});
        PERF_OBSERVERS.find(o => o.type === 'layout-shift').callback({getEntries: () => [{startTime: 1710, duration: 0, value: 0.05, hadRecentInput: false}]});
        window.requestAnimationFrame = frame;
        await __tick(300);
        Element.prototype.getBoundingClientRect = rect;
        await __key('l', {code: 'KeyL', metaKey: true, shiftKey: true}, 'body'); await __tick(10);
        return {measured};
        """,
        before=PERF_BOOK,
    )
    assert result.value["measured"] == 0
    [[payload]] = result.calls_of("light_novel_export_audio_sync_trace")
    perf = payload["scroll_perf"]
    assert perf["capabilities"] == {
        "intersection_observer": True,
        "performance_observer": True,
        "performance_entry_types": ["longtask", "layout-shift", "paint"],
    }
    events = [row for row in perf["events"] if row["event"] != "image_load"]
    assert [row["event"] for row in events] == [
        "scroll_start",
        "scroll_frame_sample",
        "scroll_frame_sample",
        "long_task",
        "layout_shift",
        "scroll_end",
    ]
    end = events[-1]
    assert end["event_count"] == 15 and end["distance_px"] == end["net_scroll_px"] == 420
    assert (
        end["frames"] >= 10
        and end["max_raf_gap_ms"] == 60
        and end["slow_frames_gt50"] >= 10
        and end["slow_frames_gt100"] == 0
    )
    assert isinstance(end["max_scroll_handler_ms"], (int, float)) and end["paired_deferred_render"] is True
    assert (events[3]["duration_ms"], events[4]["layout_shift_value"]) == (120, 0.05)
    assert all(isinstance(row["perf_revision"], str) and row["perf_revision"] for row in events)
    # What the text and the visible pictures looked like (compositor-relevant styles).
    assert {"color", "webkit_text_fill_color", "opacity", "filter", "transform", "mix_blend_mode"} <= set(
        end["current_word_style"]
    )
    [image] = perf["visible_images"]
    assert {
        key: image[key]
        for key in (
            "paragraph",
            "source",
            "intersection_ratio",
            "natural_width",
            "natural_height",
            "revealed",
            "spoiler_label_visible",
        )
    } == {
        "paragraph": 3,
        "source": "pic.jpg",
        "intersection_ratio": 0.5,
        "natural_width": 800,
        "natural_height": 600,
        "revealed": False,
        "spoiler_label_visible": True,
    }
    assert {"img_filter", "img_transform", "frame_transform", "frame_contain", "frame_backface"} <= set(image)
