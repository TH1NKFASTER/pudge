from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
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
            index += 1
            continue
        if char == "{":
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


def test_preview_owner_is_removed_when_preview_becomes_current_word() -> None:
    source = HTML.read_text(encoding="utf-8")
    render = source.split("function renderLnPairedPosition", 1)[1].split(
        "function startLnPairedInterpolation", 1
    )[0]
    assert "if(oldPreview)oldPreview.classList.remove('ln-paired-furigana-preview')" in render
    assert "word.classList.remove('ln-paired-furigana-linger','ln-paired-furigana-preview')" in render
    assert "oldPreview&&oldPreview!==previewCandidate" not in render


def test_linger_expiry_removes_stale_preview_and_hard_expires_furigana() -> None:
    source = HTML.read_text(encoding="utf-8")
    linger = _function(source, "lingerLnPairedFurigana")
    script = f"""
const names=new Set(['ln-paired-furigana-preview']);
const word={{isConnected:true,classList:{{add(name){{names.add(name);}},remove(...items){{for(const item of items)names.delete(item);}}}}}};
global.ui={{lnPairedPreviewWord:word,lnPairedFuriganaPreviewKey:'preview'}};
{linger}
(async()=>{{
  lingerLnPairedFurigana(word);
  await new Promise(resolve=>setTimeout(resolve,130));
  console.log(JSON.stringify({{
    preview:names.has('ln-paired-furigana-preview'),
    linger:names.has('ln-paired-furigana-linger'),
    expired:names.has('ln-paired-furigana-expired'),
    previewOwner:ui.lnPairedPreviewWord===word,
  }}));
}})();
"""
    assert _run_node(script) == {
        "preview": False,
        "linger": False,
        "expired": True,
        "previewOwner": False,
    }
    assert (
        "#lnReader:not(.vertical).furigana-reading .ln-word.ln-paired-furigana-expired "
        ".ln-furigana-ruby::after{display:none!important}"
    ) in source


def test_settled_transport_clock_ignores_transient_backward_backend_poll() -> None:
    source = HTML.read_text(encoding="utf-8")
    desired = _function(source, "lnPairedTransportClockDesired")
    now_fn = _function(source, "lnPairedTransportClockNow")
    reconcile = _function(source, "lnPairedTransportClockReconcile")
    script = f"""
let tick=1000;
global.performance={{now:()=>tick}};
global.ui={{lnPairedTransportClockPosition:10.5,lnPairedTransportClockAt:1000,lnPairedTransportClockPlaying:true}};
{desired}
{now_fn}
{reconcile}
const result=lnPairedTransportClockReconcile({{position:10.0,playing:true,speed:1}},{{settled:true}});
console.log(JSON.stringify(result));
"""
    result = _run_node(script)
    assert float(result["position"]) == 10.5
    assert float(result["lead"]) == 0.5
    assert float(result["drift"]) == -0.5
    assert result["staleBackward"] is True


def test_paired_scroll_stops_animation_and_throttles_forced_layout_reads() -> None:
    source = HTML.read_text(encoding="utf-8")
    interpolation = source.split("function startLnPairedInterpolation", 1)[1].split(
        "async function applyLnPairedPosition", 1
    )[0]
    assert "if(manualScrolling){ui.lnPairedAnimationFrame=null;return;}" in interpolation

    render = source.split("function renderLnPairedPosition", 1)[1].split(
        "function startLnPairedInterpolation", 1
    )[0]
    assert "now-Number(ui.lnPairedAutoScrollProbeAt||0)>=250" in render
    assert render.count("target.getBoundingClientRect()") == 1
    assert "ln-inline-image" not in source.split(".ln-reader-scroll.ln-reader-scrolling", 1)[1].split(".ln-word.state-new", 1)[0]

    handler = source.split("$('lnReaderScroll').addEventListener('scroll',()=>{", 1)[1].split(
        "},{passive:true});", 1
    )[0]
    assert "startLnPairedInterpolation(deferred.state)" in handler


def test_new_trace_captures_backend_clock_and_transport_drift() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "clock_revision:'paired-clock-v180'" in source
    assert "backend_audio_position:Number(state._backend_audio_position??state.position??0)" in source
    assert "transport_drift_ms:" in source
    assert "transport_lead_ms:" in source
    poll = source.split("function pollLnPaired()", 1)[1].split(
        "async function seekLnPairedRelative", 1
    )[0]
    assert "_backend_audio_position:backendPosition" in poll
    assert "_transport_drift:clock.drift" in poll
    assert "_transport_lead:clock.lead" in poll
