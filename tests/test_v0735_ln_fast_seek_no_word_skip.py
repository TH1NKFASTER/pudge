from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"


def test_seek_renders_immediately_even_inside_silence() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "if(!speechActive&&!seekJump)" in source
    assert "paintActive=speechActive||seekJump" in source
    assert "behavior:seekJump?'auto':'smooth'" in source
    assert "ui.lnPairedLastAudioPosition=NaN" in source
    assert "let state=ui.lnPairedState?.audiobook_id?ui.lnPairedState:null" in source


def test_resume_catchup_visits_word_boundaries() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "function lnPairedResumeCatchupOffset(" not in source
    assert "resumeSnap=resumedAfterPause&&Number.isFinite(lastDisplay)" in source
    assert "resumeCatchup=false" in source
    assert "word_safe:true" in source
    assert "lnPairedSmoothOffset(offset,state,{sameChapter,seekJump,speechActive,resumeSnap})" in source


def test_inline_javascript_parses_after_seek_and_word_skip_fix() -> None:
    source = HTML.read_text(encoding="utf-8")
    scripts = [
        match.group(2)
        for match in re.finditer(
            r"<script([^>]*)>(.*?)</script>",
            source,
            flags=re.I | re.S,
        )
        if not re.search(r"\bsrc\s*=", match.group(1), flags=re.I)
    ]
    assert scripts

    for index, script in enumerate(scripts):
        path = ROOT / f".pudge-v0735-inline-{index}.js"
        path.write_text(script, encoding="utf-8")
        try:
            subprocess.run(["node", "--check", str(path)], cwd=ROOT, check=True)
        finally:
            path.unlink(missing_ok=True)

def test_rapid_pause_resume_snaps_visual_clock_and_does_not_replay_stale_anchor() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "previousRaw===null||previousRaw===undefined?NaN:Number(previousRaw)" in source
    assert "lastDisplayRaw===null||lastDisplayRaw===undefined?NaN:Number(lastDisplayRaw)" in source
    assert "if(value-previous>4)return value;" in source
    assert "ui.lnPairedResumeNeedsSnap=false;" in source
    assert "if(!desired)cancelLnPairedInterpolation();" not in source
    assert "function lnPairedTransportClockNow(state={})" in source
    assert "lnPairedTransportClockSetDesired(true,optimistic)" in source
    assert "if(resumeSnap){ui.lnPairedResumeTarget=null;ui.lnPairedCatchupHoldUntil=0;}" in source
    assert "startLnPairedInterpolation(optimistic)" not in source
    assert "ui.lnPairedLastAudioPosition=Number(settled.position||0);" not in source
