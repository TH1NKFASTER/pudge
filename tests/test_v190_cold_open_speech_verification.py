from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from pudge.reading_audio_alignment import _inject_punctuation_pause_anchors
from pudge.subtitle_formats import parse_srt, write_srt
from pudge.subtitles.timeline_alignment import (
    _WindowMatch,
    _cold_start_refinement,
    _early_edit_audio_verification_risk,
)
from pudge.syncing import _restore_embedded_opening_clock_scaffold

ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"


def _function(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    assert match, name
    opening = source.find("){", match.end())
    assert opening >= 0
    opening += 1
    depth = 0
    quote: str | None = None
    escaped = False
    index = opening
    while index < len(source):
        char = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
        index += 1
    raise AssertionError(name)


def _run_node(script: str) -> dict[str, object]:
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _row(center: float, offset: float) -> _WindowMatch:
    return _WindowMatch(
        center=center,
        offset=offset,
        score=2.6,
        matched=6,
        source_count=7,
        reference_count=8,
        onset_coverage=0.85,
        onset_f1=0.75,
        activity_f1=0.9,
        mean_error=0.4,
        rank_delta=0.01,
        gap_fingerprint=0.7,
        edge_hint_distance=None,
    )


def test_overlapping_cold_start_keeps_gap_evidence_for_risk_escalation() -> None:
    cues = [
        (10.0, 11.0, "a"),
        (20.0, 21.0, "b"),
        (130.0, 131.0, "c"),
        (140.0, 141.0, "d"),
    ]
    segments = [
        {
            "first_center": 38.0,
            "last_center": 38.0,
            "offset_seconds": 3.5,
            "support": 1,
            "mean_score": 2.43,
            "mean_coverage": 0.857,
            "windows": [],
        },
        {
            "first_center": 131.0,
            "last_center": 1200.0,
            "offset_seconds": 0.0,
            "support": 23,
            "mean_score": 3.09,
            "mean_coverage": 0.824,
            "windows": [],
        },
    ]
    _, _, _, diagnostics = _cold_start_refinement(
        cues,
        [row[0] for row in cues],
        [17.988, 27.988, 130.0, 140.0],
        segments,
        [80.0],
        [],
        (7.988, 0.0),
    )
    assert diagnostics["applied"] is False
    assert diagnostics["reason"] == "cold_start_overlaps_main_boundary"
    assert float(diagnostics["gap_seconds"]) >= 100.0
    assert 70.0 < float(diagnostics["boundary_source_time"]) < 80.0

    risk = _early_edit_audio_verification_risk(
        [_row(38.0, 3.5), _row(110.0, 0.0), _row(146.0, 0.0)],
        diagnostics,
        [],
    )
    assert risk["required"] is True
    assert "opening_gap_clock_ambiguity" in risk["reasons"]


def test_ambiguous_single_window_uses_local_japanese_speech_residual(tmp_path: Path) -> None:
    aligned = tmp_path / "aligned.srt"
    speech = tmp_path / "japanese-speech.srt"
    pre = [10.0, 16.0, 22.0, 28.0, 34.0, 40.0]
    post = [142.0, 150.0, 158.0, 166.0, 174.0, 182.0, 190.0]
    write_srt(
        [(value, value + 1.0, f"pre-{index}") for index, value in enumerate(pre)]
        + [(value, value + 1.0, f"post-{index}") for index, value in enumerate(post)],
        aligned,
    )
    # The subtitle-only timeline supplies the coarse +3.5s early clock.  The
    # independent Japanese speech reference shows that the remaining residual
    # is +2.1s.  The generic algorithm must discover 5.6 = 3.5 + 2.1; nothing in
    # production code knows any media id, episode, or target value.
    write_srt(
        [(value + 5.6, value + 6.4, f"speech-{index}") for index, value in enumerate(pre)]
        + [(value, value + 0.8, f"main-{index}") for index, value in enumerate(post)],
        speech,
    )
    embedded = {
        "timeline_segments": [
            {
                "offset_seconds": 3.5,
                "support": 1,
                "mean_score": 2.4294,
                "mean_coverage": 0.8571,
                "kind": "stable",
            },
            {
                "offset_seconds": 0.0,
                "support": 23,
                "mean_score": 3.0926,
                "mean_coverage": 0.8244,
                "kind": "stable",
            },
        ],
        "timeline_edge_hints_seconds": [7.988, -1.413],
        "timeline_cold_start": {
            "applied": False,
            "reason": "cold_start_overlaps_main_boundary",
            "base_offset_seconds": 3.5,
            "hint_offset_seconds": 7.988,
            "delta_seconds": 4.488,
            "gap_seconds": 102.0,
            "boundary_source_time": 80.0,
        },
        "timeline_early_edit_audio_verification": {
            "required": True,
            "reasons": ["opening_gap_clock_ambiguity"],
            "early_offset_span_seconds": 3.5,
            "early_max_jump_seconds": 3.5,
        },
        "timeline_validation": {
            "after": {"f1": 0.7517},
            "activity_f1": 0.9133,
            "holdout": {
                "p90_abs_residual_seconds": 0.75,
                "mean_coverage": 0.8772,
            },
        },
    }
    output, result = _restore_embedded_opening_clock_scaffold(
        aligned,
        embedded,
        {"offset_seconds": 0.0, "timing_reference": str(speech)},
        tmp_path / "cache",
    )
    assert result["applied"] is True
    assert result["single_window_evidence"]["evidence_mode"] == "speech_verified_cold_ambiguity"
    assert result["residual_speech_refinement"]["accepted"] is True
    assert abs(float(result["base_correction_seconds"]) - 3.5) < 0.01
    assert abs(float(result["residual_speech_shift_seconds"]) - 2.1) < 0.01
    assert abs(float(result["correction_seconds"]) - 5.6) < 0.01
    cues = parse_srt(output)
    assert abs(cues[0][0] - 15.6) < 0.01
    assert abs(cues[len(pre)][0] - post[0]) < 0.01


def test_syncing_has_no_episode_specific_hyakkano_timing_override() -> None:
    source = (ROOT / "pudge/syncing.py").read_text(encoding="utf-8")
    assert "_HYAKKANO_S03E09_PREOPENING_OFFSET" not in source
    assert "_apply_verified_candidate_timing_override" not in source
    assert "verified-user-timing-v1" not in source


def test_new_player_startup_does_not_leave_browser_clock_700ms_ahead() -> None:
    source = HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _function(source, name)
        for name in (
            "lnPairedTransportClockDesired",
            "lnPairedTransportClockNow",
            "lnPairedTransportClockSetDesired",
            "lnPairedTransportClockReconcile",
            "lnPairedTransportClockReset",
        )
    )
    script = f"""
let now=0;
global.performance={{now:()=>now}};
global.ui={{lnPairedTransportDesired:null,lnPairedState:null}};
{functions}
const state={{position:33.646,speed:1,playing:true}};
lnPairedTransportClockReset(state);
now=148;
const first=lnPairedTransportClockReconcile({{...state,position:33.646}},{{settled:true}});
now=527;
const second=lnPairedTransportClockReconcile({{...state,position:33.646}},{{settled:true}});
now=782;
const started=lnPairedTransportClockReconcile({{...state,position:33.71679}},{{settled:true}});
console.log(JSON.stringify({{first,second,started}}));
"""
    result = _run_node(script)
    assert result["first"]["startupWaiting"] is True
    assert result["second"]["startupWaiting"] is True
    assert result["started"]["startupRebase"] is True
    assert abs(float(result["started"]["lead"])) < 0.01


def test_long_vad_gap_moves_impossible_next_word_onset_to_resume() -> None:
    anchors = [
        {"offset": 131, "time": 14085.635},
        {"offset": 133, "time": 14085.935},
        {"offset": 135, "time": 14087.395},
    ]
    boundaries = [{"offset": 133, "strength": 3}]
    speech_regions = [
        {"start": 14085.455, "end": 14085.960},
        {"start": 14086.700, "end": 14087.140},
        {"start": 14087.580, "end": 14088.075},
    ]
    refined, count = _inject_punctuation_pause_anchors(
        anchors, boundaries, speech_regions
    )
    assert count == 1
    assert {"offset": 132.999, "time": 14085.96} in refined
    assert {"offset": 132.999, "time": 14086.699} in refined
    assert {"offset": 133, "time": 14086.7} in refined
