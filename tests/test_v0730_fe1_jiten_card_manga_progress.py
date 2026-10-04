from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("script", "marker"),
    [
        ("tests/js/jiten_live_card_s5.cjs", "jiten live card S5: PASS"),
        ("tests/js/jiten_card_header_s10.cjs", "jiten card header S10: PASS"),
        ("tests/js/manga_progress_x01_x03.cjs", "manga progress X01/X03: PASS"),
    ],
)
def test_frontend_behaviour(script: str, marker: str) -> None:
    result = subprocess.run(
        ["node", str(ROOT / script), str(ROOT / "pudge/web")],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker in result.stdout
