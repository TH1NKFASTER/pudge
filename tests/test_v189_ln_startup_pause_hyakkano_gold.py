from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from pudge.reading_audio_alignment import _inject_punctuation_pause_anchors

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
now=1040;
const steady=lnPairedTransportClockReconcile({{...state,position:33.973208}},{{settled:true}});
console.log(JSON.stringify({{first,second,started,steady}}));
"""
    result = _run_node(script)
    assert result["first"]["startupWaiting"] is True
    assert abs(float(result["first"]["lead"])) < 0.01
    assert result["second"]["startupWaiting"] is True
    assert abs(float(result["second"]["lead"])) < 0.01
    assert result["started"]["startupRebase"] is True
    assert abs(float(result["started"]["lead"])) < 0.01
    assert abs(float(result["steady"]["lead"])) < 0.05


def test_running_mpv_seek_exits_startup_watch_immediately() -> None:
    source = HTML.read_text(encoding="utf-8")
    functions = "\n".join(
        _function(source, name)
        for name in (
            "lnPairedTransportClockDesired",
            "lnPairedTransportClockNow",
            "lnPairedTransportClockReconcile",
            "lnPairedTransportClockReset",
        )
    )
    script = f"""
let now=0;
global.performance={{now:()=>now}};
global.ui={{lnPairedTransportDesired:null,lnPairedState:null}};
{functions}
const state={{position:14054.186847,speed:1,playing:true}};
lnPairedTransportClockReset(state);
now=122;
const result=lnPairedTransportClockReconcile({{...state,position:14054.309}},{{settled:true}});
console.log(JSON.stringify(result));
"""
    result = _run_node(script)
    assert result["startupWaiting"] is False
    assert abs(float(result["lead"])) < 0.03


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
    assert not any(
        abs(float(row["offset"]) - 133.0) < 1e-6
        and float(row["time"]) < 14086.700 - 1e-6
        for row in refined
    )
    assert {"offset": 132.999, "time": 14085.96} in refined
    assert {"offset": 132.999, "time": 14086.699} in refined
    assert {"offset": 133, "time": 14086.7} in refined



def test_no_episode_specific_subtitle_timing_override_is_kept() -> None:
    source = (ROOT / "pudge/syncing.py").read_text(encoding="utf-8")
    assert "_apply_verified_candidate_timing_override" not in source
    assert "_HYAKKANO_S03E09_PREOPENING_OFFSET" not in source
