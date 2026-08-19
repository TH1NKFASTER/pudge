from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"


def test_resume_after_silence_skips_unvoiced_anchor_gap() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "resumedAfterPause=speechActive&&ui.lnPairedSpeechWasActive===false" in source
    assert "resumeSnap=resumedAfterPause&&Number.isFinite(lastDisplay)" in source
    assert "if(resumeSnap){ui.lnPairedResumeTarget=null;ui.lnPairedCatchupHoldUntil=0;}" in source
    assert "resumeSnap" in source
    assert "lnPairedTrace('resume_snap'" in source
    assert "ui.lnPairedSpeechWasActive=speechActive" in source


def test_ln_toolbar_is_strictly_one_line() -> None:
    source = HTML.read_text(encoding="utf-8")
    css = source[source.index("/* LN reader responsive toolbar */"):source.index(".credential-guide")]

    assert "display:flex;" in css
    assert "flex-wrap:nowrap;" in css
    assert "overflow:hidden;" in css
    assert "grid-template-areas" not in css
    assert ".ln-paired-time,.ln-paired-autoscroll{display:none}" in css
    assert "#lnWordMarksToggle,#lnUnknownFuriganaToggle{display:none}" in css


def test_inflected_reading_and_trace_surface_are_corrected() -> None:
    source = HTML.read_text(encoding="utf-8")

    start = source.index("function lnAudioInflectedNotation(")
    end = source.index("function lnAudioReadingWeights(", start)
    helper = source[start:end]
    script = (
        helper
        + "\nconsole.log(lnAudioInflectedNotation('戻せば','戻[もど]す'));"
        + "\nconsole.log(lnAudioInflectedNotation('目に入った','目[め]に入[はい]る'));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    assert result.stdout.splitlines() == ["戻[もど]せば", "目[め]に入[はい]った"]

    assert "token=ui.lnTokenMap?.get?.(key)" in source
    assert "if(token?.surface)return String(token.surface)" in source


def test_inline_javascript_still_parses() -> None:
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
        path = ROOT / f".pudge-v0734-inline-{index}.js"
        path.write_text(script, encoding="utf-8")
        try:
            subprocess.run(["node", "--check", str(path)], cwd=ROOT, check=True)
        finally:
            path.unlink(missing_ok=True)
