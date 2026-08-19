from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


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


def test_optimistic_transport_clock_accumulates_only_playing_intervals() -> None:
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
const state={{position:336.663454,speed:1,playing:true}};
ui.lnPairedState={{...state}};
lnPairedTransportClockReset(state);
const events=[
  [0,false],[352,true],[692,false],[1181,true],[1521,false],[1984,true],
  [2353,false],[2739,true],[3106,false],[3466,true],[3874,false],[4239,true],
  [4699,false],[5084,true],[5559,false],[5977,true],[6512,false],[6937,true],
  [7461,false],[7917,true],[8378,false],[8991,true]
];
for(const [time,desired] of events){{now=time;lnPairedTransportClockSetDesired(desired,state);ui.lnPairedTransportDesired=desired;}}
now=9612;
const local=lnPairedTransportClockNow(state);
const reconciled=lnPairedTransportClockReconcile({{...state,position:341.886417,playing:true}},{{settled:true}});
console.log(JSON.stringify({{local,reconciled}}));
"""
    result = _run_node(script)
    assert abs(float(result["local"]) - 341.563454) < 0.01
    reconciled = result["reconciled"]
    assert reconciled["snap"] is False
    assert 0.30 < float(reconciled["drift"]) < 0.35
    assert float(reconciled["position"]) > float(result["local"])


def test_pending_playing_state_does_not_roll_visual_clock_back_to_stale_backend() -> None:
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
const state={{position:100,speed:1,playing:true}};
ui.lnPairedState={{...state}};
lnPairedTransportClockReset(state);
lnPairedTransportClockSetDesired(true,state);ui.lnPairedTransportDesired=true;
now=1200;
const before=lnPairedTransportClockNow(state);
const result=lnPairedTransportClockReconcile({{...state,position:100.25}},{{settled:false}});
console.log(JSON.stringify({{before,result}}));
"""
    result = _run_node(script)
    assert float(result["before"]) > 101.1
    assert float(result["result"]["position"]) >= float(result["before"])
    assert result["result"]["snap"] is False


def test_transport_loop_keeps_polling_and_only_snaps_on_large_settled_drift() -> None:
    source = HTML.read_text(encoding="utf-8")
    toggle = _function(source, "toggleLnPairedPlayback")
    poll = _function(source, "pollLnPaired")
    assert "if(ui.lnPairedState?.player_running)pollLnPaired();else invalidateLnPairedPoll();" in toggle
    assert "lnPairedTransportClockReconcile(state,{settled:!pending})" in poll
    assert "if(clock.snap&&desiredPlaying)ui.lnPairedResumeNeedsSnap=true;" in poll
    assert "ui.lnPairedResumeNeedsSnap=false;" in toggle
    assert "startLnPairedInterpolation({...ui.lnPairedState,playing:true,paused:false,position:optimisticClock})" in toggle
