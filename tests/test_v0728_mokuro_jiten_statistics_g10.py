from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mokuro_block_lookup_uses_real_geometry_without_fabricating_word_boxes() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/mokuro_jiten_g10.cjs"), str(ROOT / "pudge/web/manga_reader_v2.js")],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "mokuro block hit: PASS" in result.stdout


def test_statistics_exhausted_pagination_stops_querying() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/statistics_async_g7.cjs"), str(ROOT / "pudge/web/statistics.js")],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pagination exhaustion: PASS" in result.stdout
