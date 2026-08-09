from pathlib import Path


def _html() -> str:
    return (
        Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html"
    ).read_text(encoding="utf-8")


def test_polychrome_uses_one_shot_wake_animation() -> None:
    html = _html()

    assert ".cover-shell.polychrome.polychrome-wake::before" in html
    assert ".cover-shell.polychrome.polychrome-wake::after" in html
    assert "animation:polychrome-flow 1.875s ease-out 1 both" in html
    assert "animation:polychrome-glint 1.425s ease-out 1 both" in html
    assert "polychrome-flow 5.2s linear infinite" not in html
    assert "polychrome-glint 3.7s ease-in-out infinite" not in html


def test_polychrome_is_woken_on_focus_and_when_returning_home() -> None:
    html = _html()

    assert "function queuePolychromeWake()" in html
    assert "if(page==='current')queuePolychromeWake();" in html
    assert "if(next){queuePolychromeWake();" in html
    assert "ui.polychromeWakeTimer=requestAnimationFrame" in html
    assert "wakePolychromeAnimations();" in html
    assert "if(!ui.windowActive||ui.page!=='current')return;" in html
