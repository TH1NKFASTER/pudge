from __future__ import annotations

from pathlib import Path

import pytest

from pudge.reading_audio_alignment import (
    audio_position_for_light_novel_offset,
    light_novel_position_for_audio,
)

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def _trace_shape_alignment() -> dict[str, object]:
    """Real 第三幕 shape around 通行証をもらって from the v184 trace."""
    return {
        "chapters": [
            {
                "chapter_index": 2,
                "start": 14051.515,
                "end": 14110.0,
                "normalized_length": 220,
                "punctuation_pause_count": 869,
                "anchors": [
                    {"time": 14085.935, "offset": 136.0},
                    {"time": 14090.36, "offset": 154.999},
                    {"time": 14090.899, "offset": 154.999},
                    {"time": 14090.9, "offset": 155.0},
                    {"time": 14094.755, "offset": 162.0},
                ],
                "speech_regions": [
                    {"start": 14085.935, "end": 14085.96},
                    {"start": 14086.70, "end": 14087.14},
                    {"start": 14087.58, "end": 14090.36},
                ],
            }
        ]
    }


def test_live_clock_holds_offset_during_internal_vad_pauses() -> None:
    alignment = _trace_shape_alignment()

    before_pause = light_novel_position_for_audio(alignment, 14085.96)
    during_first_pause = light_novel_position_for_audio(alignment, 14086.30)
    at_second_phrase_end = light_novel_position_for_audio(alignment, 14087.14)
    during_second_pause = light_novel_position_for_audio(alignment, 14087.35)
    resumed = light_novel_position_for_audio(alignment, 14087.58)

    assert before_pause is not None
    assert during_first_pause is not None
    assert at_second_phrase_end is not None
    assert during_second_pause is not None
    assert resumed is not None

    # The highlight must not keep consuming characters while narration is quiet.
    assert during_first_pause["chapter_char_offset_exact"] == pytest.approx(
        before_pause["chapter_char_offset_exact"], abs=0.01
    )
    assert during_second_pause["chapter_char_offset_exact"] == pytest.approx(
        at_second_phrase_end["chapter_char_offset_exact"], abs=0.01
    )
    assert resumed["chapter_char_offset_exact"] == pytest.approx(
        at_second_phrase_end["chapter_char_offset_exact"], abs=0.01
    )


def test_seek_inverse_uses_same_activity_clock() -> None:
    alignment = _trace_shape_alignment()
    # Offset 147 is around 通行証. Seeking must land in active speech, never in
    # the long quiet interval before the next activity region.
    position = audio_position_for_light_novel_offset(alignment, 2, 147)
    assert position is not None
    assert 14087.58 <= position <= 14090.36


def test_frontend_activity_clock_uses_vad_for_every_lookahead_segment() -> None:
    html = HTML.read_text(encoding="utf-8")
    fn = html.split("function lnPairedOffsetAtTime", 1)[1].split(
        "function lnPairedResetWordProgress", 1
    )[0]
    assert "lnPairedSpeechRatioRange(anchor,left.time,right.time,value)" in fn
    assert "value>=windowRight" not in fn
    assert "value<=windowLeft" not in fn

    render = html.split("function renderLnPairedPosition", 1)[1].split(
        "function cancelLnPairedInterpolation", 1
    )[0]
    assert "clock_rebase_backward" in render


def test_fallback_popup_has_actions_without_jiten_explanation() -> None:
    html = HTML.read_text(encoding="utf-8")
    popup = html.split("async function showLnPopup", 1)[1].split(
        "async function showLnNyaa", 1
    )[0]
    assert "if(token.fallback)" in popup
    assert "No Jiten entry" not in popup
    assert "Нет записи Jiten" not in popup
    assert "data-ln-bookmark-here" in popup
    assert 'id="lnPlayFromWord"' in popup
