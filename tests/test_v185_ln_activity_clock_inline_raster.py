"""Listen together: the clock holds through pauses inside speech (VAD), in the backend
and in the reader's highlight (behaviour; audit 71Б rewrite).

A small step back of the clock is ignored (clock_rebase_backward):
test_v0723_ln_audio_weighted_highlight.py. The word card of unparsed text:
test_v184_ln_unparsed_mora_overlay.py."""

from __future__ import annotations

import pytest

from pudge.reading_audio_alignment import (
    audio_position_for_light_novel_offset,
    light_novel_position_for_audio,
)
from t71g19_support import LN_PAIRED, OPEN


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


# The reader with light novel 9 read together with audiobook 31 (tests/t71g19_support.py).
# One poll answer carries an anchor window of four segments with speech activity; then mpv
# stops answering, so everything after the first segment is look-ahead.
#   10 s -> 0 · 12 s -> 1.5 · 14 s -> 4 · 16 s -> 6 · 18 s -> 8;  speech 10-11.5, 12-15, 16-18
ACTIVITY_WINDOW = r"""
Object.assign(AUDIO, {activity: [{start: 10, end: 11.5}, {start: 12, end: 15}, {start: 16, end: 18}], activityClock: true,
  path: [{time: 10, offset: 0}, {time: 12, offset: 1.5}, {time: 14, offset: 4}, {time: 16, offset: 6}, {time: 18, offset: 8}]});
"""


def test_reader_highlight_follows_speech_activity_in_every_lookahead_segment(page) -> None:
    result = page.run(
        ACTIVITY_WINDOW
        + OPEN
        + r"""
        const shown = () => { const w = wordNodes().find(n => n.classList.contains('ln-paired-word-current')); if (!w) return null;
          const s = Number(w.dataset.lnAudioStart), e = Number(w.dataset.lnAudioEnd);
          return s + (e - s) * parseFloat(w.style.getPropertyValue('--ln-paired-word-progress') || '0') / 100; };
        await __tick(100); AUDIO.hold = true;
        const samples = [];
        for (const time of [11.2, 11.8, 14.6, 15.6]) { while (audioAt() < time) await __tick(10); samples.push(shown()); }
        return {samples, polls: trace('poll').length};
        """,
        before=LN_PAIRED,
    )
    samples = result.value["samples"]
    assert result.value["polls"] == 1
    # Text moves only while there is speech: 1.5 characters in 1.5 s of speech, not over
    # the whole 2 s segment (1.2 at 11.2 s; linear would be 0.9), held in the pause...
    assert samples[0] == pytest.approx(1.2, abs=0.15)
    assert samples[1] == 1.5
    # ... and the same in a later segment of the window (5.2 at 14.6 s; linear: 4.6).
    assert samples[2] == pytest.approx(5.2, abs=0.2)
    assert samples[3] == 6
