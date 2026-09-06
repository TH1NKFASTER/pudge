from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"
WEB_APP = ROOT / "pudge" / "web_app.py"


def test_torrent_button_has_one_early_capture_owner() -> None:
    html = INDEX.read_text(encoding="utf-8")
    button = html.split('id="torrentToggleButton"', 1)[1].split('</button>', 1)[0]
    # The capture listener is the proven working owner. The old inline fallback
    # could never run after stopImmediatePropagation and was only debug residue.
    assert 'onclick=' not in button

    ui_pos = html.index("const ui={")
    capture_pos = html.index("async function torrentToggleHttpCapture(){")
    ordinary_handlers = html.index("document.addEventListener('contextmenu'")
    assert ui_pos < capture_pos < ordinary_handlers

    handler = html.split("async function torrentToggleHttpCapture(){", 1)[1].split(
        "document.addEventListener('click',event=>{", 1
    )[0]
    assert "torrentHttpJson('/api/torrents/enabled'" in handler
    assert "Torrent toggle HTTP timeout" in handler
    assert "torrentToggleCaptureInFlight" in handler
    assert "ui.state.settings.torrents_enabled=Boolean(result.enabled)" in handler

    capture = html.split("document.addEventListener('click',event=>{", 1)[1].split("},true);", 1)[0]
    assert "#torrentToggleButton" in capture
    assert "event.stopImmediatePropagation()" in capture
    assert "torrentToggleGeneration" not in html


def test_torrent_http_status_is_plain_authoritative_endpoint() -> None:
    source = WEB_APP.read_text(encoding="utf-8")
    section = source.split('if path == "/api/torrents/status":', 1)[1].split('super().do_GET()', 1)[0]
    assert "torrent_traffic_status()" in section
    assert "_torrent_http_status_seen" not in section
    assert "torrent.http_status_first" not in section
