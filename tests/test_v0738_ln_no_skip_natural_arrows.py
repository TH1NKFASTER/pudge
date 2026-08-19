from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"
OLD_MAP = "{ArrowLeft:-5,ArrowRight:5,ArrowUp:15,ArrowDown:-15}"
NEW_MAP = "{ArrowLeft:-5,ArrowRight:5,ArrowUp:-15,ArrowDown:15}"


def test_resume_catchup_does_not_skip_short_words() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "lnPairedTrace('catchup_word_switch'" in source
    assert "lnPairedTrace('catchup_word_hold'" in source
    assert "ui.lnPairedCatchupHoldUntil=now+90" in source

    silence = source.split("if(!speechActive){", 1)[1].split(
        "if(seekJump)ui.lnPairedPendingWord=null;", 1
    )[0]
    assert "ui.lnPairedPendingWord=null" not in silence


def test_vertical_arrows_use_natural_reading_direction() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert NEW_MAP in source
    assert OLD_MAP not in source


def test_fast_seek_and_legacy_contracts_remain() -> None:
    source = HTML.read_text(encoding="utf-8")

    required = [
        "if(!speechActive)",
        "if(!speechActive&&!seekJump)",
        "if(!seekJump)",
        "ui.lnPairedLastAudioPosition=NaN",
        "behavior:seekJump?'auto':'smooth'",
        "resumeSnap=resumedAfterPause&&Number.isFinite(lastDisplay)",
        "paintActive=speechActive||seekJump",
        "if(active&&Number.isFinite(offset)&&!frozen&&speechActive)",
        "else if(active&&Number.isFinite(offset)&&!frozen&&seekJump)",
        "maxAdvance=Math.max(.16,Math.min(.65",
    ]
    missing = [value for value in required if value not in source]
    assert not missing, missing


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
        path = ROOT / f".pudge-v0738-inline-{index}.js"
        path.write_text(script, encoding="utf-8")
        try:
            subprocess.run(["node", "--check", str(path)], cwd=ROOT, check=True)
        finally:
            path.unlink(missing_ok=True)
