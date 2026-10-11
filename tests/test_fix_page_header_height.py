"""Page header: the same fixed height (bottom line at the same y) on every page."""

from __future__ import annotations

from css_support import css_rules

PAGES = ["current", "lightnovels", "manga", "audiobooks", "visualnovels", "planned", "statistics", "settings"]


def test_header_has_one_fixed_height_and_centres_its_contents() -> None:
    rules = css_rules("index.html")
    header = rules.declarations(".toolbar")
    assert header.get("box-sizing") == "border-box"
    assert header.get("height") == "61px"
    assert header.get("align-items") == "center"
    assert header.get("flex", "").startswith("0 0")  # never stretched or squeezed by the page
    # Title line box fits the content box (61 - 18 - 12 - 1px line).
    assert rules.get(".toolbar h1", "line-height") == "30px"
    assert rules.get(".toolbar button", "white-space") == "nowrap"  # a button cannot grow it by wrapping


def test_every_page_uses_the_same_header_and_its_buttons_live_inside_it(page) -> None:
    result = page.run(
        r"""
        const out = {};
        for (const name of %s) {
          setPage(name, true);
          await __tick(50);
          const headers = [...document.querySelectorAll('.main > .toolbar')].filter(node => __visible('.main > .toolbar'));
          const buttons = [...document.querySelectorAll('.main > .toolbar button')]
            .filter(node => !node.hidden && !node.closest('[hidden]')).map(node => node.id || node.className);
          out[name] = {headers: headers.length, title: __text('#pageTitle'), buttons};
        }
        return out;
        """
        % PAGES,
    )
    out = result.value
    for name in PAGES:
        assert out[name]["headers"] == 1, name
        assert out[name]["title"], name
    for name in ("lightnovels", "manga", "audiobooks"):
        assert out[name]["buttons"] == ["pageImportButton"], name
    for name in ("current", "visualnovels", "planned", "statistics", "settings"):
        assert out[name]["buttons"] == [], name
