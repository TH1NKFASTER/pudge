from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"


def test_speech_activity_uses_edge_hysteresis() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "function lnPairedSpeechActive(anchor,time)" in source
    assert "value=Number(time),grace=.7," in source
    assert "start:Math.max(left,Number(row.start)-grace)" in source
    assert "end:Math.min(right,Number(row.end)+grace)" in source


def test_renderer_is_not_replaced_by_another_pending_queue() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "LN_PAIRED_CATCHUP_QUEUE_LIMIT" not in source
    assert "lnPairedCatchupQueue.shift()" not in source
    assert "catchupPending=Boolean(pending?.catchup)" not in source


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
        path = ROOT / f".pudge-v0743-hysteresis-inline-{index}.js"
        path.write_text(script, encoding="utf-8")
        try:
            subprocess.run(
                ["node", "--check", str(path)],
                cwd=ROOT,
                check=True,
            )
        finally:
            path.unlink(missing_ok=True)
