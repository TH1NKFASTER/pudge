from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_index_inline_javascript_parses_with_node() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")

    output = ["\n" if char == "\n" else " " for char in html]
    for match in re.finditer(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        start, end = match.span(1)
        output[start:end] = html[start:end]

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".js",
        encoding="utf-8",
        delete=False,
    ) as handle:
        handle.write("".join(output))
        js_path = Path(handle.name)

    try:
        result = subprocess.run(
            [node, "--check", str(js_path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        js_path.unlink(missing_ok=True)

    assert result.returncode == 0, result.stderr or result.stdout
