from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _audio_helper_source() -> str:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    start = html.index("function lnAudioKana")
    end = html.index("function renderLnParagraph", start)
    return html[start:end]


def test_kanji_reading_weight_uses_mora_count() -> None:
    source = _audio_helper_source()
    script = source + "\nconsole.log(JSON.stringify(lnAudioReadingWeights('承る',{start:0,reading:'うけたまわる'},{})));"
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(result.stdout) == [5, 1]


def test_ruby_range_weight_has_priority() -> None:
    source = _audio_helper_source()
    script = source + "\nconsole.log(JSON.stringify(lnAudioReadingWeights('承る',{start:0,rubies:[{start:0,end:1,text:'うけたまわ'}]},{})));"
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(result.stdout) == [5, 1]


def test_weighted_highlight_finishes_word_before_switch() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    block = html[
        html.index("function renderLnPairedPosition"):
        html.index("function pollLnPaired")
    ]
    assert "previous.style.setProperty('--ln-paired-word-progress','100%')" in block
    assert "until:performance.now()+34" not in block
    assert "lnPairedOffsetAtTime(state,estimatedTime)" in block
    assert "lnPairedOffsetAtTime(state,Number(state.position))" in block
    assert "clamp_backward" in block


def test_ln_audio_trace_exports_to_internal_debug_dir_and_reveals_file() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    backend = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")

    assert "light_novel_export_audio_sync_trace(payload)" in html
    assert "event.code!=='KeyL'" in html
    assert "ui.lnPairedTrace.length>2400" in html
    assert 'output_dir = debug_log_dir()' in backend
    assert 'Pudge-patch-logs' not in backend
    assert 'subprocess.Popen(["open", "-R", str(output)])' in backend
    assert "raw_events[-3000:]" in backend
