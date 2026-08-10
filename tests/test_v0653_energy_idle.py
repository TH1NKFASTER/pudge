from pathlib import Path


def _html() -> str:
    return (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")


def test_idle_polychrome_has_no_infinite_animation() -> None:
    html = _html()
    assert ".cover-shell.polychrome::before" in html
    assert ".cover-shell.polychrome::after" in html
    assert "animation:none;" in html
    assert "polychrome-flow 5.2s linear infinite" not in html
    assert "polychrome-glint 3.7s ease-in-out infinite" not in html


def test_polychrome_motion_is_short_and_user_triggered() -> None:
    html = _html()
    assert ".cover-shell.polychrome.polychrome-wake::before," in html
    assert ".cover-shell.polychrome.polychrome-hover-wake::before" in html
    assert "animation:polychrome-flow 1.875s ease-out 1 both" in html
    assert "animation:polychrome-glint 1.425s ease-out 1 both" in html


def test_idle_has_no_periodic_full_state_rebuild() -> None:
    html = _html()
    assert "function pollBackgroundState()" not in html
    assert "backgroundStatePollDelay" not in html
    assert "backgroundStatePollTimer" not in html
    assert "await pywebview.api.ui_state_versions()" in html
