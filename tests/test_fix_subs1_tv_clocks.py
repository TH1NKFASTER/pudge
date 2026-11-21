"""SUBS1: TV captures with minutes of pre-roll, a cut ED and kept commercials (invented timings).

* text clock plateaus: lines the first clock puts before the file start take their own text clock
  (a cold open) or, when they would pile up at 0 s, are dropped (commercials, the previous
  programme) instead of making the output guard reject an otherwise right map;
* a short excursion clock between two clocks of one level is folded when it alone made the
  fit refuse the whole map;
* the output guard counts clock changes between stable runs, not each step of the lines a cut
  squeezes between two clocks;
* the subtitle timeline moves a trailing block mapped past the reference's end (a preview after
  an ED the release cut) onto the reference's own lines.
"""

from __future__ import annotations

import random
from pathlib import Path

from pudge import alignment_guard, syncing
from pudge.subtitle_formats import write_srt
from pudge.subtitles import text_clock_plateaus as tcp
from pudge.subtitles import timeline_alignment as ta

# ----------------------------------------------------------------- excursion fold ---


def _clock_cues(spans: list[tuple[int, float]], *, step: float = 2.0, start: float = 30.0):
    """Back-to-back lines (no dialogue gap anywhere) on the given clocks."""
    starts, ends, shifts = [], [], []
    time = start
    for count, shift in spans:
        for _ in range(count):
            starts.append(time)
            ends.append(time + 2.0)
            shifts.append(shift)
            time += step
    return starts, ends, shifts


def test_a_short_excursion_without_a_dialogue_gap_is_read_as_the_clock_around_it() -> None:
    starts, ends, shifts = _clock_cues([(110, -91.0), (13, -79.0), (100, -91.0)])
    fit = tcp.fit_text_clock_plateaus(starts, ends, shifts)
    assert fit["accepted"] is True and fit["reason"] == "ok"
    assert fit["folded_excursions"][0]["cues"] == 13
    assert {round(value, 1) for value in fit["shifts"]} == {-91.0}


def test_a_long_or_one_sided_clock_change_is_not_folded() -> None:
    # 60 lines on another clock without a gap: refused as before (not an excursion).
    starts, ends, shifts = _clock_cues([(110, -91.0), (60, -79.0), (100, -91.0)])
    assert tcp.fit_text_clock_plateaus(starts, ends, shifts)["reason"] == "forward_jump_without_dialogue_gap"
    # a real forward change at a gap stays a clock change
    starts, ends, shifts = _clock_cues([(110, -91.0), (100, -60.0)], step=4.0)
    starts = starts[:110] + [value + 40.0 for value in starts[110:]]
    ends = ends[:110] + [value + 40.0 for value in ends[110:]]
    fit = tcp.fit_text_clock_plateaus(starts, ends, shifts)
    assert fit["accepted"] is True and "folded_excursions" not in fit
    assert fit["jumps"][0]["kind"] == "inserted_time"


# ------------------------------------------------------------- leading cues < 0 s ---


def _retime(cues):
    def retime(values):
        return [
            (max(0.0, start + (value or 0.0)), max(0.05, end + (value or 0.0)), text)
            for (start, end, text), value in zip(cues, values)
        ]

    return retime


def _supported(monkeypatch, supported: bool) -> None:
    monkeypatch.setattr(
        syncing,
        "_plateau_text_support",
        lambda cues, reference, low, high: {
            "supported": supported,
            "score": 0.6,
            "compared_cues": high - low + 1,
        },
    )


def test_a_cold_open_before_the_cut_pre_roll_takes_its_own_text_clock(monkeypatch) -> None:
    _supported(monkeypatch, True)
    cues = [(33.0 + 3 * k, 35.0 + 3 * k, f"cold {k}") for k in range(6)]
    cues += [(216.0 + 3 * k, 218.0 + 3 * k, f"line {k}") for k in range(40)]
    own = [-32.6, -32.4, -32.9, -31.8, -30.5, -30.9]
    shifts = own + [-91.3] * 40
    repaired = [-91.3] * 46
    payload = syncing._plateau_leading_cues_before_start(cues, shifts, repaired, _retime(cues), [])
    assert payload["cues"] == 6 and payload["own_clock"] == -32.1
    assert repaired[:6] == [-32.1] * 6 and repaired[6] == -91.3
    assert "dropped" not in payload


def test_commercials_before_the_programme_are_dropped_not_piled_at_the_start(monkeypatch) -> None:
    _supported(monkeypatch, True)
    cues = [(32.0 + 4 * k, 34.0 + 4 * k, f"ad {k}") for k in range(10)]
    cues += [(187.0 + 3 * k, 189.0 + 3 * k, f"line {k}") for k in range(40)]
    scattered = [-4.1, 2.9, 10.1, 10.4, -18.9, -14.3, -12.4, -8.0, -4.8, 1.3]
    repaired = [-90.5] * 50
    payload = syncing._plateau_leading_cues_before_start(
        cues, scattered + [-90.5] * 40, repaired, _retime(cues), []
    )
    assert payload["dropped"] == list(range(10)) and payload["own_clock"] is None
    assert repaired == [-90.5] * 50


def test_a_few_lines_at_the_start_stay_as_before(monkeypatch) -> None:
    _supported(monkeypatch, False)
    cues = [(30.0 + 3 * k, 32.0 + 3 * k, f"x {k}") for k in range(4)] + [
        (200.0 + 3 * k, 202.0 + 3 * k, f"line {k}") for k in range(40)
    ]
    payload = syncing._plateau_leading_cues_before_start(
        cues, [None, -50.0, 3.0, -20.0] + [-92.0] * 40, [-92.0] * 44, _retime(cues), []
    )
    assert payload["kept_at_start"] == 4 and "dropped" not in payload


def test_nothing_changes_when_the_first_clock_keeps_every_line_in_the_file(monkeypatch) -> None:
    _supported(monkeypatch, True)
    cues = [(100.0 + 3 * k, 102.0 + 3 * k, f"line {k}") for k in range(20)]
    repaired = [-90.0] * 20
    assert syncing._plateau_leading_cues_before_start(cues, list(repaired), repaired, _retime(cues), []) == {
        "cues": 0
    }


# ------------------------------------------------------------------ output guard ---


def _write(path: Path, cues) -> Path:
    write_srt(cues, path, preserve_order=True)
    return path


def test_lines_squeezed_by_a_cut_do_not_make_a_three_clock_map_scattered(tmp_path: Path) -> None:
    raw, final = [], []
    time = 10.0
    for k in range(120):  # clock +139.7
        raw.append((time, time + 2, f"first part line {k}"))
        final.append((time + 139.7, time + 141.7, f"first part line {k}"))
        time += 5.0
    gap_start = final[-1][0] + 3.0
    for k in range(22):  # squeezed: their content is cut, they ramp across 47 s of source
        raw.append((time, time + 2, f"squeezed line {k}"))
        final.append((gap_start + 0.6 * k, gap_start + 0.6 * k + 0.5, f"squeezed line {k}"))
        time += 3.0
    for k in range(110):  # clock +92.7
        raw.append((time, time + 2, f"middle line {k}"))
        final.append((time + 92.7, time + 94.7, f"middle line {k}"))
        time += 5.0
    time += 20.0
    for k in range(100):  # commercials kept in the video: clock +252.7
        raw.append((time, time + 2, f"last part line {k}"))
        final.append((time + 252.7, time + 254.7, f"last part line {k}"))
        time += 5.0
    result = alignment_guard.validate_alignment_output(
        _write(tmp_path / "raw.srt", raw), _write(tmp_path / "out.srt", final)
    )
    assert result["status"] == "accepted", result
    assert result["diagnostics"]["shift_jumps"] > 10 and result["diagnostics"]["stable_shift_jumps"] <= 3


def test_a_destroyed_map_is_still_rejected(tmp_path: Path) -> None:
    rng = random.Random(7)
    raw = [(10.0 + 5 * k, 12.0 + 5 * k, f"line {k}") for k in range(300)]
    final = []
    for start, end, text in raw:
        shift = rng.choice([0.0, 40.0, -35.0, 80.0]) if rng.random() < 0.5 else 0.0
        final.append((max(0.0, start + shift), max(0.05, end + shift), text))
    final.sort()
    result = alignment_guard.validate_alignment_output(
        _write(tmp_path / "raw.srt", raw), _write(tmp_path / "out.srt", final)
    )
    assert result["status"] == "rejected"


# ---------------------------------------------------------------- overshooting tail ---


def test_a_preview_mapped_past_the_reference_end_moves_onto_its_lines() -> None:
    source = [(100.0 + 4 * k, 102.0 + 4 * k, f"line {k}") for k in range(300)]  # ends 1298
    source.append((1420.0, 1480.0, "♬～"))  # the ED
    preview = [(1500.0 + 2.5 * k, 1502.0 + 2.5 * k, f"preview {k}") for k in range(16)]
    source += preview
    mapped = [(start - 150.0, end - 150.0, text) for start, end, text in source]  # tail ends ~1390
    # the release cut the ED: its preview speech sits 90 s earlier than the clock puts it
    reference = [(start - 150.0, end - 150.0, "x") for start, end, _t in source[:300]]
    reference += [(start - 240.0 + 0.1, end - 240.0, "y") for start, end, _t in preview]
    moved, payload = ta._reanchor_overshooting_tail(source, mapped, reference)
    assert payload["applied"] is True and payload["move_seconds"] == -90.0
    assert payload["block_first_cue"] == 300  # the ED mark moves with its preview
    assert moved[:300] == mapped[:300]
    assert abs(moved[-1][0] - (preview[-1][0] - 240.0)) < 1e-6


def test_a_tail_inside_the_reference_or_without_its_lines_stays() -> None:
    source = [(100.0 + 4 * k, 102.0 + 4 * k, f"line {k}") for k in range(300)]
    mapped = [(start - 50.0, end - 50.0, text) for start, end, text in source]
    reference = [(start, end, "x") for start, end, _t in mapped]
    assert ta._reanchor_overshooting_tail(source, mapped, reference)[1]["reason"] == "no_overshoot"
    # overshooting, but the reference has nothing of the block's rhythm before its end
    source2 = source + [(1500.0 + 2.5 * k, 1502.0 + 2.5 * k, f"p {k}") for k in range(16)]
    mapped2 = [(start - 50.0, end - 50.0, text) for start, end, text in source2]
    moved, payload = ta._reanchor_overshooting_tail(source2, mapped2, reference[:250])
    assert payload["applied"] is False and moved == mapped2
