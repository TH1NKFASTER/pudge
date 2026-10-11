"""Pudge's source text, for codebase-wide policy tests only (audit 71Б).

Tests check behaviour: the window through tests/page_harness.py, Python by calling
it, mpv scripts through tests/lua_harness.py, stylesheets through css_support.
Reading source text is kept for rules about the code base as a whole, where there
is no behaviour to run: "no inline language pairs anywhere", "every catalog key
used in code exists", "the package ships the catalogs". Such tests read sources
through this module, and tests/test_source_policy_guard.py lists who may.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source(relative: str) -> str:
    """Text of a file under the repository root, e.g. "pudge/web/index.html"."""
    return (ROOT / relative).read_text(encoding="utf-8")


def sources(pattern: str) -> dict[str, str]:
    """{relative path: text} for a glob under the repository root, e.g. "pudge/**/*.py"."""
    return {
        str(path.relative_to(ROOT)): path.read_text(encoding="utf-8")
        for path in sorted(ROOT.glob(pattern))
        if path.is_file() and "__pycache__" not in path.parts
    }
