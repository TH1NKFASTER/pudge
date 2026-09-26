from __future__ import annotations

from pathlib import Path


def test_confirmed_torrent_off_button_has_no_redundant_second_line() -> None:
    root = Path(__file__).resolve().parents[1]
    index = (root / "pudge/web/index.html").read_text(encoding="utf-8")

    assert '<button id="torrentToggleButton" class="torrent-off"><span>Torrents: off</span></button>' in index
    assert "live.transition==='off_confirmed'?'':" in index
    assert "button.innerHTML=`<span>${title}</span>${detail?`<small>${detail}</small>`:''}`" in index
    assert "live.transition==='off_confirmed'?(ui.lang==='ru'?'Торренты выключены':'Torrents off')" not in index
