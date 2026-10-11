"""Listen together: a clock segment the backend stretched over a pause runs on the wall
clock, in the backend and in the reader (behaviour; audit 71Б rewrite).

The torrent switch of v2.00 (one HTTP request per click, owned by one handler):
test_torrent_toggle_ui.py."""

from __future__ import annotations

import pytest

from pudge.reading_audio_alignment import (
    audio_position_for_light_novel_offset,
    light_novel_position_for_audio,
)


def _wall_clock_bridge_alignment() -> dict:
    return {
        "duration": 20_000.0,
        "chapters": [
            {
                "chapter_index": 2,
                "start": 14054.365,
                "end": 14110.0,
                "normalized_length": 200,
                "activity_clock": True,
                "punctuation_pause_count": 1,
                "speech_regions": [
                    {"start": 14089.015, "end": 14090.360},
                    {"start": 14090.900, "end": 14092.460},
                ],
                "anchors": [
                    {"time": 14089.015, "offset": 142},
                    {
                        "time": 14090.900,
                        "offset": 155,
                        "wall_clock_from_previous": True,
                    },
                    {"time": 14094.755, "offset": 162},
                ],
            }
        ],
    }


def test_pause_stretched_bridge_is_not_recompressed_by_backend_activity_clock() -> None:
    alignment = _wall_clock_bridge_alignment()
    at_speech_end = light_novel_position_for_audio(alignment, 14090.360)
    assert at_speech_end is not None
    # Linear wall-time interpolation: 1.345 / 1.885 of the 13-char bridge.
    expected = 142 + 13 * ((14090.360 - 14089.015) / (14090.900 - 14089.015))
    assert at_speech_end["chapter_char_offset_exact"] == pytest.approx(expected, abs=0.01)
    assert at_speech_end["chapter_char_offset_exact"] < 152.0
    assert at_speech_end["anchor_window"]["path"][1]["wall_clock_from_previous"] is True


def test_pause_stretched_bridge_inverse_seek_uses_same_wall_clock() -> None:
    alignment = _wall_clock_bridge_alignment()
    position = audio_position_for_light_novel_offset(alignment, 2, 151)
    assert position is not None
    expected = 14089.015 + ((151 - 142) / 13) * (14090.900 - 14089.015)
    assert position == pytest.approx(expected, abs=0.01)
    # Activity-clock compression would incorrectly land before the speech end.
    assert position > 14090.30


def test_reader_does_not_squeeze_a_wall_clock_bridge_into_speech_activity(page) -> None:
    # Speech only in the first second of a 4 s segment of 8 characters: an ordinary
    # segment runs its text during that second (activity clock); a segment the backend
    # marked as a wall-clock bridge (stretched over a pause) runs linearly over all 4 s.
    result = page.run(
        r"""
        const activity = [{start: 0, end: 1}];
        const segment = mark => ({anchor_window: {activity_clock: true, activity,
          path: [{time: 0, offset: 0}, {time: 4, offset: 8, ...(mark ? {wall_clock_from_previous: true} : {})}]}});
        const at = state => [0.5, 1, 2, 3].map(time => lnPairedOffsetAtTime(state, time));
        return {bridge: at(segment(true)), speech: at(segment(false))};
        """
    )
    assert result.value == {"bridge": pytest.approx([1, 2, 4, 6]), "speech": pytest.approx([4, 8, 8, 8])}
