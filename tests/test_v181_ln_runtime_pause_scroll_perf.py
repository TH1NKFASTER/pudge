from __future__ import annotations

from pathlib import Path

from pudge.reading_audio_alignment import (
    _punctuation_boundaries,
    audio_position_for_light_novel_offset,
    chapter_audio_text,
    light_novel_position_for_audio,
)


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
AUDIOBOOKS = ROOT / "pudge" / "audiobooks.py"


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
    assert abs(
        float(audio_position_for_light_novel_offset(alignment, 1, 11) or 0.0)
        - 6859.3
    ) < 1e-6


def test_runtime_alignment_loader_attaches_current_ln_punctuation_context() -> None:
    source = AUDIOBOOKS.read_text(encoding="utf-8")
    block = source.split("def _attach_runtime_alignment_context", 1)[1].split(
        "def _load_alignment", 1
    )[0]
    assert "SELECT chapter_index,text FROM ln_chapters" in block
    assert "chapter_audio_text" in block
    assert "_punctuation_boundaries" in block
    assert '"_runtime_punctuation_boundaries"' in block


def test_scroll_trace_captures_frame_cadence_images_and_text_compositor_state() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "perf_revision:'ln-scroll-v181'" in source
    assert "function lnScrollPerfStart()" in source
    assert "max_raf_gap_ms" in source
    assert "slow_frames_gt50" in source
    assert "max_scroll_handler_ms" in source
    assert "function lnScrollPerfImageSnapshot" in source
    assert "natural_width" in source
    assert "natural_height" in source
    assert "img_filter" in source
    assert "frame_transform" in source
    assert "frame_contain" in source
    assert "function lnScrollPerfTextStyle" in source
    assert "webkit_text_fill_color" in source
    assert "mix_blend_mode" in source
    assert "new IntersectionObserver" in source
    assert "performance_entry_types" in source
    assert "'longtask'" in source
    assert "'layout-shift'" in source


def test_scroll_trace_is_exported_with_audio_sync_trace() -> None:
    source = HTML.read_text(encoding="utf-8")
    export = source.split("async function exportLnPairedTrace()", 1)[1].split(
        "function renderLnTokenBody", 1
    )[0]
    assert "scroll_perf_revision:'ln-scroll-v181'" in export
    assert "scroll_perf:{" in export
    assert "ui.lnScrollPerfTrace" in export
    assert "ui.lnScrollPerfVisibleImages" in export


def test_manual_scroll_handler_starts_and_finishes_perf_session_without_layout_loop() -> None:
    source = HTML.read_text(encoding="utf-8")
    handler = source.split(
        "$('lnReaderScroll').addEventListener('scroll',()=>{", 1
    )[1].split("},{passive:true});", 1)[0]
    assert "lnScrollPerfOnEvent" in handler
    assert "lnScrollPerfFinish()" in handler

    start = source.split("function lnScrollPerfStart()", 1)[1].split(
        "function lnScrollPerfOnEvent", 1
    )[0]
    # rAF timing can sample scrollTop, but visibility geometry is supplied by
    # IntersectionObserver rather than querying every image each frame.
    assert "requestAnimationFrame(tick)" in start
    assert "getBoundingClientRect" not in start
