from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_jiten_nplus1_card_star_shares_highlight_eligibility() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/jiten_optimal_card_g11.cjs"), str(ROOT / "pudge/web/reading_tools.js")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Jiten N+1 study-card star: PASS" in result.stdout
