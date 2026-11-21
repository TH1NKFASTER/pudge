"""Timeline clocks to 0.01 s instead of the 0.25-1 s window grid (sync benchmark, 2026-10).

The window search scores offsets on a 0.25 s / 1 s grid and the old tuning moved a
clock only when its lines were 0.3 s or more off, so whole episodes stayed 0.1-0.5 s
late or early (Sayonara Lara on the Crunchyroll track, Dungeon Meshi E20 AT-X captions,
SAKAMOTO DAYS E01 Amazon, Ansatsu Kyoushitsu E20 +0.58 s). Synthetic data only.
"""

from __future__ import annotations

import json
import random
import tempfile
from pathlib import Path

import pytest

from pudge.subtitles.timeline_alignment import _fine_tune_segment_offsets, _precise_segment_offsets
from tests.synthetic_timeline import evaluate

CASES = {
    int(case["seed"]): case
    for case in json.loads(
        (Path(__file__).parent / "fixtures" / "timeline_synthetic_cases.json").read_text()
    )["pass"]
}


@pytest.mark.parametrize("seed", [2099, 410, 836, 1123, 80])
def test_accepted_clock_sits_on_its_lines_not_on_the_window_grid(seed: int, tmp_path: Path) -> None:
    # Before: every line 0.3-0.5 s off (p95 >= 0.3), still a "pass" under the 1 s contract.
    row = evaluate(CASES[seed]["params"], seed, tmp_path)
    assert row["accepted"] is True, row.get("reason")
    assert row["p95"] <= 0.25, (row["p95"], row["segments"], row["meta"]["true_clocks"])


def _onsets(count: int = 300, seed: int = 7) -> list[float]:
    rng = random.Random(seed)
    values, time = [], 20.0
    for _ in range(count):
        time += rng.uniform(1.5, 5.0)
        values.append(round(time, 3))
    return values


def test_coarse_tuning_is_not_refused_for_one_line_lost_far_away() -> None:
    # Ansatsu Kyoushitsu E20: windows said +1.0 s, the lines +0.45 s (median error 0.54 ->
    # 0.06 s), refused because 2 of 401 lines lost their neighbour within 1.5 s.
    lines = _onsets()
    strays = [lines[-1] + 20.0, lines[-1] + 40.0]  # lines without a counterpart ...
    source = lines + strays
    # ... that only the wrong clock puts within 1.5 s of some reference line
    reference = sorted([value + 0.45 for value in lines] + [value + 2.3 for value in strays])
    tuned, rows = _fine_tune_segment_offsets([{"offset_seconds": 1.0, "support": 20}], [], source, reference)
    assert rows and rows[0]["applied"] is True, rows
    assert abs(float(tuned[0]["offset_seconds"]) - 0.45) <= 0.05


def _precise(segments, boundaries, source, reference, *, length=2.0, reference_ends=None):
    cues = [(value, value + length, f"l{i}") for i, value in enumerate(source)]
    ends = sorted(reference_ends if reference_ends is not None else [value + length for value in reference])
    return _precise_segment_offsets(
        segments, boundaries, source, sorted(reference), [], source_cues=cues, reference_ends=ends
    )


def test_small_residual_clock_is_put_on_its_lines() -> None:
    rng = random.Random(3)
    source = _onsets()
    reference = sorted(value + 2.17 + rng.uniform(-0.05, 0.05) for value in source if rng.random() > 0.1)
    tuned, rows = _precise([{"offset_seconds": 2.0, "support": 20}], [], source, reference)
    assert rows and rows[0]["applied"] is True, rows
    assert abs(float(tuned[0]["offset_seconds"]) - 2.17) <= 0.03


def test_clock_within_a_tenth_of_a_second_is_left_alone() -> None:
    source = _onsets()
    tuned, rows = _precise([{"offset_seconds": 1.0, "support": 20}], [], source, [v + 1.06 for v in source])
    assert tuned[0]["offset_seconds"] == 1.0 and rows == []


def test_drifting_clock_is_not_moved_by_one_half() -> None:
    # A clock whose lines drift 0.4 s from one end to the other (a framerate drift the
    # cutting left in one piece): the halves want 0.25 and 0.45 s, one shift fits neither.
    source = _onsets()
    tuned, rows = _precise(
        [{"offset_seconds": 0.0, "support": 20}], [], source, [v * 1.0004 + 0.15 for v in source]
    )
    assert tuned[0]["offset_seconds"] == 0.0
    assert rows and rows[0]["applied"] is False, rows


def test_votes_from_one_half_alone_cannot_move_the_clock() -> None:
    # Only the first half has reference lines near it (60+ votes there, none in the other
    # half): a half without votes is no agreement.
    source = _onsets(400)
    middle = source[len(source) // 2]
    reference = [v + 0.2 for v in source if v < middle]
    tuned, rows = _precise([{"offset_seconds": 0.0, "support": 20}], [], source, reference)
    assert tuned[0]["offset_seconds"] == 0.0
    assert rows and rows[0]["applied"] is False and len(rows[0]["half_shifts_seconds"]) == 1, rows


def test_reference_padding_does_not_move_a_right_clock() -> None:
    # The reference shows every line 0.2 s early and keeps it 0.2 s longer: its starts say
    # -0.2 s, its ends +0.2 s; the clock is right and must stay.
    source = _onsets()
    tuned, rows = _precise(
        [{"offset_seconds": 0.0, "support": 20}],
        [],
        source,
        [v - 0.2 for v in source],
        reference_ends=[v + 2.0 + 0.2 for v in source],
    )
    assert tuned[0]["offset_seconds"] == 0.0
    assert rows and rows[0]["applied"] is False, rows


def test_a_large_move_with_loosely_agreeing_halves_is_capped() -> None:
    source = _onsets(400)
    middle = source[len(source) // 2]
    reference = [v + (0.40 if v < middle else 0.50) for v in source]
    tuned, rows = _precise([{"offset_seconds": 0.0, "support": 20}], [], source, reference)
    assert rows and rows[0]["capped"] is True and rows[0]["applied"] is True, rows
    assert tuned[0]["offset_seconds"] == 0.3


def test_every_segment_gets_its_own_precise_clock() -> None:
    source = _onsets()
    cut = source[150]
    reference = [v + 5.12 for v in source if v < cut] + [v - 3.13 for v in source if v >= cut]
    tuned, _rows = _precise(
        [{"offset_seconds": 5.0, "support": 10}, {"offset_seconds": -3.0, "support": 10}],
        [cut - 0.5],
        source,
        reference,
    )
    assert [round(float(segment["offset_seconds"]), 2) for segment in tuned] == [5.12, -3.13]


def test_a_refined_clock_that_reorders_lines_falls_back_to_the_window_clocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pudge.subtitles import timeline_alignment

    def reordering(segments, boundaries, *args, **kwargs):
        moved = [dict(segment) for segment in segments]
        moved[0]["offset_seconds"] = float(moved[0]["offset_seconds"]) + 40.0
        return moved, [{"pass": "precise", "applied": True, "segment": 0}]

    monkeypatch.setattr(timeline_alignment, "_precise_segment_offsets", reordering)
    row = evaluate(CASES[836]["params"], 836, tmp_path)  # two clocks: the first runs into the second
    assert row["accepted"] is True, row.get("reason")
    assert row["frac_bad"] <= 0.01


def test_synthetic_suite_precision_does_not_drop(tmp_path: Path) -> None:
    # A few fixture families in one go: no accepted case may get fewer lines within 0.3 s.
    for seed in (410, 2395, 2227):
        row = evaluate(CASES[seed]["params"], seed, Path(tempfile.mkdtemp(dir=tmp_path)))
        assert row["accepted"] is True, (seed, row.get("reason"))
        assert row["frac_bad"] <= 0.01, (seed, row["segments"])


def test_a_refined_clock_that_fits_fewer_lines_falls_back_to_the_window_clocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pudge.subtitles import timeline_alignment
    from tests.synthetic_timeline import build, write_srt

    def worse(segments, boundaries, *args, **kwargs):
        moved = [
            {**segment, "offset_seconds": float(segment["offset_seconds"]) - 0.45} for segment in segments
        ]
        return moved, [{"pass": "precise", "applied": True, "segment": 0}]

    monkeypatch.setattr(timeline_alignment, "_precise_segment_offsets", worse)
    source, reference, _meta, _params = build(CASES[565]["params"], 565)
    write_srt(tmp_path / "s.srt", [(s, e, "ja") for s, e, _t, _o in source], "ja")
    write_srt(tmp_path / "r.srt", [(s, e) for s, e, _k in reference], "en")
    _out, result = timeline_alignment.align_subtitle_timelines(
        tmp_path / "s.srt", tmp_path / "r.srt", tmp_path / "cache", force=True
    )
    assert result["accepted"] is True
    assert result["timeline_precise_clock_fallback"] == "refined_mapping_matches_fewer_lines"
    assert result["timeline_segments"][0]["offset_seconds"] == 13.0  # the window clock
