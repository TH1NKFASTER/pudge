"""A clock half a second off is tuned when the precise pass cannot place it (too few end votes).

The window fine-tuning left such a move to the precise pass, which needs line ends near the
reference's: with a reference whose ends never sit near the file's (a CC track on another
cut's English subtitles), neither pass moved the clock and every line stayed 0.5 s off.
"""

from __future__ import annotations

from pudge.subtitles import timeline_alignment as ta


def _episode(*, ends_match: bool):
    source_onsets: list[float] = []
    reference_onsets: list[float] = []
    source_cues: list[tuple[float, float, str]] = []
    reference_ends: list[float] = []
    for index in range(300):
        start = 10.0 + 4.0 * index
        source_onsets.append(start)
        source_cues.append((start, start + 1.0, f"line {index}"))
        if index % 30 == 7:
            # A line whose only reference neighbour is a wrong one, near the old clock only.
            reference_onsets.append(start - 1.2)
            reference_ends.append(start - 1.2 + (1.0 if ends_match else 2.2))
            continue
        reference_onsets.append(start + 0.5)
        reference_ends.append(start + 0.5 + (1.0 if ends_match else 2.2))
    return source_onsets, sorted(reference_onsets), source_cues, sorted(reference_ends)


def _tune(*, ends_match: bool, with_ends: bool = True):
    source_onsets, reference_onsets, source_cues, reference_ends = _episode(ends_match=ends_match)
    kwargs = {"source_cues": source_cues, "reference_ends": reference_ends} if with_ends else {}
    return ta._fine_tune_segment_offsets(
        [{"offset_seconds": 0.0}], [], source_onsets, reference_onsets, **kwargs
    )


def test_move_is_made_when_the_precise_pass_has_no_end_votes() -> None:
    tuned, rows = _tune(ends_match=False)
    assert rows[0]["tuned_offset_seconds"] == 0.5
    assert rows[0]["applied"] is True
    assert tuned[0]["offset_seconds"] == 0.5


def test_move_is_left_to_the_precise_pass_when_ends_vote() -> None:
    tuned, rows = _tune(ends_match=True)
    assert rows[0]["applied"] is False
    assert tuned[0]["offset_seconds"] == 0.0


def test_without_end_information_the_old_rule_holds() -> None:
    tuned, rows = _tune(ends_match=False, with_ends=False)
    assert rows[0]["applied"] is False
    assert tuned[0]["offset_seconds"] == 0.0
