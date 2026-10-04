from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from pudge.reading_audio_alignment import light_novel_position_for_audio


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"


def _shared_offsets(state: dict, times: list[float]) -> list[float]:
    """Evaluate the shared PudgePairedAudioClock (LN and sidebar use the same math)."""
    import json
    script = (
        "const vm=require('vm'),fs=require('fs');const c={performance:{now:()=>0}};c.globalThis=c;vm.createContext(c);"
        f"vm.runInContext(fs.readFileSync({json.dumps(str(ROOT / 'pudge/web/paired_audio_clock.js'))},'utf8'),c);"
        f"const s={json.dumps(state)};console.log(JSON.stringify({json.dumps(times)}.map(t=>c.PudgePairedAudioClock.offsetAtTime(s,t))));"
    )
    out = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def _alignment() -> dict[str, object]:
    return {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 1,
                "normalized_length": 100,
                "start": 10.0,
                "end": 14.0,
                "anchors": [
                    {"offset": 0, "time": 10.0},
                    {"offset": 10, "time": 10.2},
                    {"offset": 20, "time": 10.4},
                    {"offset": 30, "time": 10.6},
                    {"offset": 45, "time": 11.0},
                    {"offset": 60, "time": 11.6},
                    {"offset": 75, "time": 12.2},
                    {"offset": 100, "time": 14.0},
                ],
                "speech_regions": [],
            }
        ],
    }


def test_backend_returns_future_anchor_path_beyond_poll_interval() -> None:
    result = light_novel_position_for_audio(_alignment(), 10.1)

    assert result is not None
    assert result["chapter_char_offset_exact"] == pytest.approx(5.0)
    path = result["anchor_window"]["path"]

    assert path[0] == {"time": 10.0, "offset": 0.0}
    assert path[1] == {"time": 10.2, "offset": 10.0}
    assert path[-1]["time"] >= 11.6
    assert len(path) >= 6


def test_frontend_uses_piecewise_anchor_path() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "function lnPairedAnchorPath(anchor)" in source
    # The anchor path is parsed by the shared clock module (plan §7).
    assert "PudgePairedAudioClock?.pathFor?.({anchor_window:anchor||{}})" in source
    assert "function lnPairedOffsetAtTime(state,time)" in source
    state = {"anchor_window": {"path": [{"time": 0, "offset": 0}, {"time": 1, "offset": 10}, {"time": 3, "offset": 12}]}}
    assert _shared_offsets(state, [0.5, 1, 2, 3]) == pytest.approx([5, 10, 11, 12])
    assert "path[path.length-1]?.time" in source


def test_forward_clamp_remains_only_as_a_safety_net() -> None:
    source = HTML.read_text(encoding="utf-8")

    # Plan §7 removed the LN-only forward clamp from the timing path.
    assert "function lnPairedSmoothOffset(" not in source
    assert "clamp_forward" not in source
    assert "function lnPairedAnchorPath(anchor)" in source


def test_inline_javascript_parses() -> None:
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
        path = ROOT / f".pudge-v0745-anchor-path-inline-{index}.js"
        path.write_text(script, encoding="utf-8")
        try:
            subprocess.run(
                ["node", "--check", str(path)],
                cwd=ROOT,
                check=True,
            )
        finally:
            path.unlink(missing_ok=True)
