"""Listen together: the backend sends an anchor window reaching past the next poll, and
the reader's highlight runs along it (behaviour; audit 71Б rewrite). How the shared clock
maps a window to offsets: tests/test_v0744_ln_linear_mora_clock.py."""

from __future__ import annotations

import pytest

from pudge.reading_audio_alignment import light_novel_position_for_audio
from t71g21_support import READER, paired


def _alignment() -> dict[str, object]:
    return {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 1,
                "normalized_length": 100,
                "start": 10.0,
                "end": 14.0,
                "anchors": [
                    {"offset": 0, "time": 10.0},
                    {"offset": 10, "time": 10.2},
                    {"offset": 20, "time": 10.4},
                    {"offset": 30, "time": 10.6},
                    {"offset": 45, "time": 11.0},
                    {"offset": 60, "time": 11.6},
                    {"offset": 75, "time": 12.2},
                    {"offset": 100, "time": 14.0},
                ],
                "speech_regions": [],
            }
        ],
    }


def test_backend_returns_future_anchor_path_beyond_poll_interval() -> None:
    result = light_novel_position_for_audio(_alignment(), 10.1)

    assert result is not None
    assert result["chapter_char_offset_exact"] == pytest.approx(5.0)
    path = result["anchor_window"]["path"]

    assert path[0] == {"time": 10.0, "offset": 0.0}
    assert path[1] == {"time": 10.2, "offset": 10.0}
    assert path[-1]["time"] >= 11.6
    assert len(path) >= 6


def test_reader_runs_along_the_window_until_its_last_anchor(page) -> None:
    # One poll answer carries several anchors (slow, fast, slow); no newer answer comes
    # (the backend stalls). The highlight follows every stretch of the window and stops at
    # its last anchor instead of guessing beyond it.
    result = page.run(
        READER
        + r"""
        AUDIO.path = [{time: 100, offset: 0}, {time: 101, offset: 1}, {time: 101.2, offset: 3}, {time: 102, offset: 3.5}];
        await openReader();
        AUDIO.hold = true;
        const samples = [];
        for (const t of [100.6, 101.5, 101.9, 103, 104]) { await untilAudio(t); samples.push({word: current(), offset: shown()}); }
        return samples;
        """,
        **paired(playing=True),
    )
    assert [sample["word"] for sample in result.value] == [0, 1, 1, 1, 1]
    assert [sample["offset"] for sample in result.value] == pytest.approx(
        [0.6, 3.19, 3.44, 3.5, 3.5], abs=0.05
    )


def test_highlight_is_not_held_back_behind_the_clock(page) -> None:
    # Forward steps of the clock show up in full on the next paint (there used to be an
    # LN-only forward clamp that let the highlight trail the audio).
    result = page.run(
        READER
        + r"""
        await openReader();
        return [paintAt(0.5, 100.5), paintAt(1.5, 100.53), paintAt(3.3, 100.56), paintAt(5.6, 100.59)];
        """,
        **paired(playing=False),
    )
    assert result.value == [
        {"word": 0, "offset": pytest.approx(0.5)},
        {"word": 0, "offset": pytest.approx(1.5)},
        {"word": 1, "offset": pytest.approx(3.3)},
        {"word": 2, "offset": pytest.approx(5.6)},
    ]
