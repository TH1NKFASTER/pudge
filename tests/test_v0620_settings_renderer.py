from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def test_settings_helpers_are_declared_before_renderer() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert source.index("function input(") < source.index("function renderSettings(")
    assert source.index("function checkbox(") < source.index("function renderSettings(")


def test_render_settings_executes_in_javascript() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")

    source = HTML.read_text(encoding="utf-8")
    start = source.index("function input(")
    end = source.index("function fillSettings(", start)
    renderer_source = source[start:end]

    script = f"""
const elements = new Map();
function element(id) {{
  if (!elements.has(id)) {{
    elements.set(id, {{
      id,
      innerHTML: '',
      value: '',
      checked: false,
      disabled: false,
      type: 'text',
      textContent: '',
      classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }},
      removeAttribute() {{}},
    }});
  }}
  return elements.get(id);
}}
const $ = element;
const t = key => key;
const escapeHtml = value => String(value ?? '');
const ui = {{ state: {{ settings: {{ version: '0.6.29' }}, storage: null }} }};
function fillSettings() {{}}
function syncConditionalSettings() {{}}
function updateAniListSyncUi() {{}}
{renderer_source}
renderSettings();
const html = element('settingsContent').innerHTML;
if (!html.includes('id="s_escape_fullscreen"')) throw new Error('checkbox helper did not render');
if (!html.includes('id="s_jimaku"')) throw new Error('input helper did not render');
if (!html.includes('0.6.29')) throw new Error('version was not rendered');
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
