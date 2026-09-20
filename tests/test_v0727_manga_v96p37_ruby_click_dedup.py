"""Reader ruby hitboxes: synthetic cases only; no real manga OCR corpus."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "pudge/web/manga_reader_v2.js"
SCRIPT = ROOT / "tests/fixtures/v96p37_ruby_click_synthetic.cjs"


def test_ruby_click_dedup_affects_only_the_interactive_layer():
    source = JS.read_text(encoding="utf-8")
    assert "function mangaRubyDuplicateRegionIndices(regions)" in source
    assert "if (rubyDuplicates.has(regionIndex)) return '';" in source
    assert "if (rubyDuplicates.has(regionIndex)) continue;" in source
    assert "if (rubyDuplicates.has(regionIndex)) return;" in source
    assert "textRegionCache.set(key, regions)" in source


def test_synthetic_ruby_clicks_keep_regular_dialogue_clickable():
    node = shutil.which("node")
    if not node:
        pytest.skip("node unavailable; static integration contract still tested")
    result = subprocess.run(
        [node, str(SCRIPT), str(JS)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"syntheticCases": 13, "passed": True}
