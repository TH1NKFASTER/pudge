from pathlib import Path


def test_inactive_window_pauses_all_css_animations() -> None:
    html = (
        Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html"
    ).read_text(encoding="utf-8")

    assert "html.ui-inactive *" in html
    assert "html.ui-inactive *::before" in html
    assert "html.ui-inactive *::after" in html
    assert "animation-play-state:paused !important" in html


def test_inactive_window_defers_render_and_slows_polling() -> None:
    html = (
        Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html"
    ).read_text(encoding="utf-8")

    assert "windowActive:!document.hidden&&document.hasFocus()" in html
    assert "pendingDataRender:false" in html
    assert "if(!force&&!ui.windowActive){ui.pendingDataRender=true;return;}" in html
    assert "if(next&&ui.pendingDataRender)renderDataPages(true)" in html
    assert "if(!downloads.length)return document.hidden||!ui.windowActive?120000:15000" in html
    assert "Math.min(60000,delay)" in html
    assert "window.addEventListener('blur'" in html
    assert "setWindowActivity(false)" in html
    assert "window.addEventListener('focus',()=>setWindowActivity(true))" in html
