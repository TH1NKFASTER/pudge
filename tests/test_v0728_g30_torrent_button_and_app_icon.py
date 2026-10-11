"""The torrent switch keeps its height in every state; the installed app uses the padded
app icon (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from PIL import Image

from tests.css_support import css_rules
from tests.i18n_support import en

ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / "pudge" / "assets" / "app-icon.png"
STATES = """
const lines = () => [...document.querySelectorAll('#torrentToggleButton > *')].map(node => node.tagName);
const out = {};
for (const [name, enabled, traffic] of [
  ['offConfirmed', false, {enabled: false, transition: 'off_confirmed'}],
  ['offUnconfirmed', false, {enabled: false, transition: 'unconfirmed'}],
  ['onRates', true, {enabled: true, download_speed: 2048, upload_speed: 0, updated_at: Date.now() / 1000}],
  ['onStale', true, {enabled: true, stale: true}],
]) {
  ui.state.settings.torrents_enabled = enabled; ui.torrentTraffic = traffic; refreshTorrentToggle();
  out[name] = {lines: lines(), classes: $('torrentToggleButton').className};
}
return out;
"""


def test_torrent_toggle_keeps_original_two_line_height_in_every_state(page) -> None:
    assert en("ui.shutdownNotConfirmed") == "Shutdown not confirmed"
    out = page.run(STATES).value
    # One or two lines depending on the state ...
    assert out["offConfirmed"]["lines"] == ["SPAN"]
    assert (
        out["offUnconfirmed"]["lines"]
        == out["onRates"]["lines"]
        == out["onStale"]["lines"]
        == ["SPAN", "SMALL"]
    )
    # ... in a button of fixed height: the sidebar does not jump when the second line goes.
    rules = css_rules("index.html")
    assert (
        rules.get("#torrentToggleButton", "height")
        == rules.get("#torrentToggleButton", "min-height")
        == "48px"
    )
    for state in {state["classes"] for state in out.values() if isinstance(state, dict)}:
        for name in state.split():
            assert not {"height", "min-height", "max-height"} & set(
                rules.declarations(f"#torrentToggleButton.{name}")
            ), name


def test_runtime_app_icon_has_safe_padding_and_install_uses_same_asset(tmp_path: Path) -> None:
    with Image.open(ICON) as image:
        rgba = image.convert("RGBA")
        assert rgba.size == (1024, 1024)
        bbox = rgba.getbbox()
    assert bbox is not None
    left, top, right, bottom = bbox
    assert min(left, top, 1024 - right, 1024 - bottom) >= 48

    # The installer's icon step (macOS tools stubbed) builds AppIcon.icns from that same file.
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "sips").write_text(
        '#!/bin/sh\necho "$@" >> "$CALLS"\nwhile [ $# -gt 0 ]; do [ "$1" = --out ] && : > "$2"; shift; done\n'
    )
    (stubs / "iconutil").write_text(
        '#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done\n'
    )
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    resources = tmp_path / "Resources"
    resources.mkdir()
    env = {
        **os.environ,
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "CALLS": str(tmp_path / "calls"),
        "PROJECT_DIR": str(ROOT),
        "BUILD_DIR": str(tmp_path / "build"),
        "NEW_RESOURCES": str(resources),
        "PLIST": str(tmp_path / "Info.plist"),
    }
    subprocess.run(
        # install.sh is a zsh script; eval (not `source <(...)`, which macOS's bash 3.2 cannot read).
        [
            "zsh",
            "-c",
            'setopt errexit nounset pipefail; eval "$(sed -n "/^ICON_SOURCE=/,/^fi$/p" "$PROJECT_DIR/install.sh")"',
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    sources = {line.split()[3] for line in (tmp_path / "calls").read_text().splitlines()}
    assert sources == {str(ICON)}
    assert (resources / "AppIcon.icns").is_file()
