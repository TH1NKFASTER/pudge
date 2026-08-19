from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"


def test_silence_branch_preserves_both_contract_shapes() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "if(!speechActive)" in source
    assert "if(!speechActive&&!seekJump)" in source
    assert "if(!seekJump)" in source

    assert "paintActive=speechActive||seekJump" in source
    assert "if(active&&Number.isFinite(offset)&&!frozen&&speechActive)" in source
    assert "else if(active&&Number.isFinite(offset)&&!frozen&&seekJump)" in source


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
        path = ROOT / f".pudge-v0737-inline-{index}.js"
        path.write_text(script, encoding="utf-8")
        try:
            subprocess.run(["node", "--check", str(path)], cwd=ROOT, check=True)
        finally:
            path.unlink(missing_ok=True)
