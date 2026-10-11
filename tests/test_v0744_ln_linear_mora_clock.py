"""Listen together: the text clock is linear between alignment anchors, in the backend and
in the reader's highlight (behaviour; audit 71Б rewrite).

The reader runs in node (tests/page_harness.py); tests/t71g21_support.py has the book read
together with its audiobook. The reader side of the anchor windows (v0.7.45) and of
punctuation pauses (v0.7.50) is covered here as well."""

from __future__ import annotations

import pytest

from pudge.reading_audio_alignment import (
    audio_position_for_light_novel,
    audio_position_for_light_novel_offset,
    light_novel_position_for_audio,
)
from t71g21_support import READER, paired


def _alignment() -> dict[str, object]:
    return {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 1,
                "normalized_length": 100,
                "start": 10.0,
                "end": 20.0,
                "anchors": [
                    {"offset": 0, "time": 10.0},
                    {"offset": 40, "time": 14.0},
                    {"offset": 100, "time": 20.0},
                ],
                "speech_regions": [
                    {"start": 10.0, "end": 10.5},
                    {"start": 13.5, "end": 14.0},
                    {"start": 19.0, "end": 20.0},
                ],
            }
        ],
    }


@pytest.mark.parametrize(
    ("position", "expected"),
    [
        (10.0, 0.0),
        (11.0, 10.0),
        (12.0, 20.0),
        (13.0, 30.0),
        (14.0, 40.0),
        (17.0, 70.0),
        (20.0, 100.0),
    ],
)
def test_audio_to_text_is_linear_between_anchors(
    position: float,
    expected: float,
) -> None:
    result = light_novel_position_for_audio(_alignment(), position)

    assert result is not None
    assert result["chapter_char_offset_exact"] == expected


@pytest.mark.parametrize("offset", [0, 10, 20, 30, 40, 55, 70, 100])
def test_text_to_audio_is_inverse_of_linear_anchor_clock(offset: int) -> None:
    alignment = _alignment()
    position = audio_position_for_light_novel_offset(
        alignment,
        1,
        offset,
    )
    assert position is not None

    restored = light_novel_position_for_audio(alignment, position)
    assert restored is not None
    assert restored["chapter_char_offset_exact"] == pytest.approx(
        float(offset),
        abs=0.0001,
    )


def test_progress_seek_uses_the_same_linear_clock() -> None:
    assert audio_position_for_light_novel(_alignment(), 1, 0.2) == 12.0
    assert audio_position_for_light_novel(_alignment(), 1, 0.7) == 17.0


def test_shared_clock_maps_audio_time_to_text_offsets(page) -> None:
    # The reader and the sidebar use PudgePairedAudioClock for the anchor window.
    result = page.run(
        r"""
        const offsets = (path, times, state = {}) => times.map(t => PudgePairedAudioClock.offsetAtTime({...state, anchor_window: {path}}, t));
        return {
          linear: offsets([{time: 10, offset: 0}, {time: 14, offset: 40}, {time: 20, offset: 100}], [10, 11, 13, 14, 17, 20]),
          // Several anchors ahead of the audio position (the backend sends more than one poll's worth).
          lookahead: offsets([{time: 0, offset: 0}, {time: 1, offset: 10}, {time: 3, offset: 12}], [0.5, 1, 2, 3]),
          // A punctuation pause: equal offsets hold the clock until speech resumes.
          pause: offsets([{time: 0, offset: 0}, {time: 1, offset: 5}, {time: 3, offset: 5}, {time: 4, offset: 9}], [1.5, 2.9, 3.5]),
          // A path that runs backwards is not used: the backend's exact offset is.
          backwards: offsets([{time: 0, offset: 9}, {time: 2, offset: 3}], [1], {chapter_char_offset_exact: 42}),
        };
        """
    )
    assert result.value["linear"] == pytest.approx([0, 10, 30, 40, 70, 100])
    assert result.value["lookahead"] == pytest.approx([5, 10, 11, 12])
    assert result.value["pause"] == pytest.approx([5, 5, 7])
    assert result.value["backwards"] == pytest.approx([42])


def test_reader_highlight_follows_the_anchor_clock(page) -> None:
    """While the audiobook plays, the highlight shows the anchor clock's text offset: one
    character per second, then a fast stretch, a punctuation pause that holds it, and a
    slower stretch. Inside a word the progress is linear in time even when its morae are
    uneven (話し, 話 = はな: the reader used to re-time words by their morae)."""
    result = page.run(
        READER
        + r"""
        AUDIO.path = [{time: 100, offset: 0}, {time: 102, offset: 2}, {time: 102.5, offset: 5.9},
                      {time: 104, offset: 5.9}, {time: 106, offset: 10}];
        await openReader();
        const samples = [];
        for (const t of [101, 102.25, 103, 103.9, 104.29, 105]) { await untilAudio(t); samples.push({word: current(), offset: shown()}); }
        return {samples, weights: wordEls()[3].dataset.lnAudioWeights};
        """,
        **paired(playing=True),
    )
    samples = result.value["samples"]
    assert result.value["weights"] == "2,1"
    assert [sample["word"] for sample in samples] == [0, 1, 2, 2, 3, 3]
    # Paints follow the clock every ~33 ms, so a sample may lag that much behind it.
    assert samples[0]["offset"] == pytest.approx(1.0, abs=0.06)
    assert samples[1]["offset"] == pytest.approx(3.95, abs=0.35)
    assert [samples[2]["offset"], samples[3]["offset"]] == pytest.approx([5.9, 5.9])
    assert samples[4]["offset"] == pytest.approx(6.5, abs=0.1)
    assert samples[5]["offset"] == pytest.approx(7.95, abs=0.1)


def test_highlight_moves_to_the_next_word_at_once(page) -> None:
    # The paint that crosses into the next word shows it; the previous word is complete.
    # (A switch used to be held for 34 ms with the old word at 100 %.)
    result = page.run(
        READER
        + r"""
        await openReader();
        const before = paintAt(1.9, 101.9);
        const after = paintAt(2.05, 101.93);
        return {before, after, words: words().slice(0, 2)};
        """,
        **paired(playing=False),
    )
    assert result.value["before"]["word"] == 0
    assert result.value["after"] == {"word": 1, "offset": pytest.approx(2.05)}
    first, second = result.value["words"]
    assert (
        first["current"] is False
        and first["progress"] == "100%"
        and "ln-paired-word-done" in first["classes"]
    )
    assert second["current"] is True


def test_highlight_stays_with_the_voice_after_a_speed_change_elsewhere(page) -> None:
    result = page.run(
        READER
        + r"""
        await openReader();
        await untilAudio(101.1);                           // between two polls
        setAudio({speed: 2});
        const samples = [];
        for (const t of [102.6, 103.4, 104.6]) { await untilAudio(t); samples.push([shown(), t - 100]); }
        return samples;
        """,
        **paired(playing=True),
    )
    # The highlight may trail the voice by one paint (~0.07 s of audio at 2×), never lead it.
    assert all(expected - 0.08 <= shown <= expected + 0.01 for shown, expected in result.value)
