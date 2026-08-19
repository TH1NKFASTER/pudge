from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"


def _audio_helper_source() -> str:
    html = HTML.read_text(encoding="utf-8")
    start = html.index("function lnAudioKana")
    end = html.index("function renderLnParagraph", start)
    return html[start:end]


def _weights(surface: str, reading: str) -> list[float]:
    script = (
        _audio_helper_source()
        + f"\nconsole.log(JSON.stringify(lnAudioReadingWeights({json.dumps(surface)},"
        + f"{{start:0,reading:{json.dumps(reading)}}},{{}})));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def test_ruby_notation_weights_the_correct_segments() -> None:
    assert _weights("承る", "うけたまわる") == [5, 1]
    assert _weights("言い方", "言[い]い方[かた]") == [1, 1, 2]
    assert _weights("洒落る", "洒落[しゃれ]る") == [1, 1, 1]
    assert _weights("大人", "大人[おとな]") == [1.5, 1.5]


def test_seek_resets_stale_word_progress() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "function lnPairedResetWordProgress(reader)" in source
    assert "lnPairedResetWordProgress(reader);" in source
    assert "if(word&&word!==old)word.style.removeProperty('--ln-paired-word-progress')" in source
    assert "ui.lnPairedHighlightKey=''" in source


def test_anchor_changes_are_smoothed_without_slowing_explicit_seek() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "function lnPairedSmoothOffset(" in source
    assert "maxAdvance=Math.max(.16,Math.min(.65" in source
    assert "clamp_forward" in source
    assert "seekJump||!speechActive" in source
    assert "function lnPairedTransportClockReconcile(" in source
    assert "function lnPairedTransportClockNow(" in source
    assert "path[path.length-1]?.time" in source
    assert "const behind=speechActive&&" not in source


def test_trace_is_readable_and_deduplicates_speech_pauses() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "now-Number(ui.lnPairedTracePauseAt||0)<750" in source
    assert "function lnPairedSurface(node)" in source
    assert "surface:lnPairedSurface(current)" in source


def test_ln_toolbar_is_responsive_and_uses_short_labels() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert 'class="ln-reader-actions"' in source
    assert 'data-wide-label="Light Novels"' in source
    assert 'data-wide-label="Finish volume"' in source
    assert "#lnReaderClose::after" in source
    assert "#lnFinishVolume::after{content:attr(data-short-label)}" in source
    assert "@media(max-width:680px)" in source
    assert ".ln-reader-toolbar{" in source
    assert "display:flex;" in source
    assert "flex-wrap:nowrap;" in source
    assert ".ln-paired-tray{display:flex;align-items:center" in source
    assert ".ln-reader-heading{flex-basis:104px;max-width:104px}" in source
