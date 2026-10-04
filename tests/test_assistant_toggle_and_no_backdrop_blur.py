from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "pudge" / "web"


def test_ln_click_router_includes_assistant_toggle():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    start = html.index("document.addEventListener('click',async e=>{\n  const target=e.target.closest(")
    selector_line = html[start:html.index("\n", html.index("closest(", start))]
    assert "#lnAssistantToggle" in selector_line
    assert "target.id==='lnAssistantToggle'" in html


def test_no_backdrop_blur_in_desktop_overlays():
    for name in ("cover_preview.css", "pudge_confirm.js", "index.html"):
        text = (WEB / name).read_text(encoding="utf-8")
        assert "backdrop-filter:blur(" not in text.replace(" ", ""), name
    css = (WEB / "cover_preview.css").read_text(encoding="utf-8")
    assert "drop-shadow" not in css
