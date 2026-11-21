"""Synthetic timeline-alignment suite: seeded episodes with a known true clock.

Each case builds a ~24 min episode with tests/synthetic_timeline.py (cold open / OP /
parts / eyecatch / ED / preview, master edits, framerate, segmentation, jitter, signs,
one-sided song cues), runs align_subtitle_timelines and compares every source cue with
its true position on the reference clock. Cases come from a 3000-case fuzz run
(tests/fixtures/timeline_synthetic_cases.json).

Contract:
  pass    accepted, <=1% of cues >1.0 s off, p95 error <= 0.8 s
  reject  not accepted (25 vs 23.976 fps cannot be expressed as clocks; ALASS takes over)
  xfail   one minimal repro per known failure class (silent wrong sync or an easy case
          rejected); strict, so a fix flips it and it has to be promoted to a pass case.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.synthetic_timeline import evaluate, sample_params

CASES = json.loads((Path(__file__).parent / "fixtures" / "timeline_synthetic_cases.json").read_text())


def _run(case: dict, tmp_path: Path) -> dict:
    return evaluate(case["params"], int(case["seed"]), tmp_path)


@pytest.mark.parametrize("case", CASES["pass"], ids=lambda c: f"{c['family']}-{c['seed']}")
def test_synthetic_alignment_is_accurate(case: dict, tmp_path: Path) -> None:
    row = _run(case, tmp_path)
    assert row["accepted"] is True, row.get("reason")
    assert row["frac_bad"] <= 0.01, (row["frac_bad"], row["segments"], row["meta"]["true_clocks"])
    assert row["p95"] <= 0.8, (row["p95"], row["segments"])


@pytest.mark.parametrize("case", CASES["reject"], ids=lambda c: f"{c['family']}-{c['seed']}")
def test_synthetic_unrepresentable_mapping_is_rejected(case: dict, tmp_path: Path) -> None:
    row = _run(case, tmp_path)
    assert row["accepted"] is False, (row.get("frac_bad"), row.get("segments"))


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, marks=pytest.mark.xfail(strict=True, reason=c["class"])) for c in CASES["xfail"]],
    ids=lambda c: f"{c['family']}-{c['seed']}",
)
def test_synthetic_known_failure(case: dict, tmp_path: Path) -> None:
    row = _run(case, tmp_path)
    if case["class"].startswith("E:"):
        # an easy constant-offset case must be accepted and accurate
        assert row["accepted"] is True, row.get("reason")
    if row["accepted"]:
        assert row["frac_bad"] <= 0.03, (row["frac_bad"], row["segments"], row["meta"]["true_clocks"])


def test_a_late_clock_the_windows_missed_is_sent_to_the_speech_check(tmp_path: Path) -> None:
    # A 17 s scene only in the video 72 s before the end: no window found the new clock.
    seed = 236
    row = evaluate(sample_params(seed), seed, tmp_path)
    assert row["accepted"] is True, row.get("reason")
    assert row["frac_bad"] <= 0.01 or "late_clock_disagreement" in (row["risk_reasons"] or []), row
