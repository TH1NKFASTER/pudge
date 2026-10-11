"""The foil (polychrome) covers keep WKWebView idle (behaviour + stylesheet; audit 71Б rewrite).

The foil look stays, but its motion is a short one-shot when Watching becomes active
or the user hovers a cover; never an endless animation. "No periodic full state rebuild
while idle" is covered by test_ready_notification_ui_refresh_v0603.py
(test_open_window_checks_the_tiny_versions_and_fetches_state_only_when_they_change)."""

from __future__ import annotations

from css_support import css_rules

READY = {
    "media_id": 7,
    "title": "Show",
    "episodes": 12,
    "next_episode": 3,
    "site_url": "https://anilist.co/anime/7",
    "local": {"episode": 3, "state": "ready", "video_path": "/lib/Show - 03.mkv"},
}
IDLE = {"startup_maintenance": {"running": False}, "ui_state_versions": {"ready": "1", "ui": "1"}}


def test_idle_foil_has_no_endless_animation() -> None:
    rules = css_rules("index.html")
    assert rules.get(".cover-shell.polychrome::before", "animation") == "none"
    assert rules.get(".cover-shell.polychrome::after", "animation") == "none"
    for _media, selector, declarations in rules.rules:
        if "polychrome" in selector:
            assert "infinite" not in declarations.get("animation", ""), selector


def test_foil_motion_plays_once_when_triggered() -> None:
    rules = css_rules("index.html")
    for wake in ("polychrome-wake", "polychrome-hover-wake"):
        assert (
            rules.get(f".cover-shell.polychrome.{wake}::before", "animation")
            == "polychrome-flow 1.875s ease-out 1 both"
        )
        assert (
            rules.get(f".cover-shell.polychrome.{wake}::after", "animation")
            == "polychrome-glint 1.425s ease-out 1 both"
        )


# What the first foil cover on Home wakes with ('' when still).
COVER = """
const wake = () => [...document.querySelector('#current .cover-shell.polychrome').classList].filter(name => name.endsWith('wake')).join(' ');
const refocus = async () => { window.dispatchEvent(new Event('blur')); window.dispatchEvent(new Event('focus')); await __tick(40); };
const hover = async () => {
  await __fire('#current .cover-shell.polychrome', 'pointerout', {}, 'PointerEvent');
  await __fire('#current .cover-shell.polychrome', 'pointerover', {}, 'PointerEvent');
};
await __tick(3000);
"""


def test_foil_wakes_briefly_on_focus_and_hover_of_watching(page) -> None:
    result = page.run(
        COVER
        + """
        const out = {idle: wake()};
        await refocus(); out.focus = wake();
        await __tick(2100); out.after = wake();                     // a one-shot of about 2 s
        await hover(); out.hover = wake();
        await __tick(2100); out.hoverAfter = wake();
        window.dispatchEvent(new Event('blur'));
        await hover(); out.inactiveHover = wake();                  // a window in the background stays still
        window.dispatchEvent(new Event('focus')); await __tick(2100);
        setPage('settings'); await refocus(); out.otherPage = wake();
        setPage('current'); await __tick(2100);
        window.dispatchEvent(new Event('blur')); window.dispatchEvent(new Event('focus'));
        setPage('settings'); await __tick(40); out.leftAtOnce = wake();   // left Watching before the wake frame
        return out;
        """,
        state={"home": {"new_ready": [READY]}},
        api=IDLE,
    )
    assert result.value == {
        "idle": "",
        "focus": "polychrome-wake",
        "after": "",
        "hover": "polychrome-hover-wake",
        "hoverAfter": "",
        "inactiveHover": "",
        "otherPage": "",
        "leftAtOnce": "",
    }


def test_foil_stays_still_in_energy_saving(page) -> None:
    result = page.run(
        COVER
        + """
        const out = {saving: document.documentElement.classList.contains('energy-saving')};
        await refocus(); out.focus = wake();
        await hover(); out.hover = wake();
        return out;
        """,
        state={"home": {"new_ready": [READY]}, "settings": {"power_policy": {"mode": "energy_saving"}}},
        api={**IDLE, "power_status": {"mode": "energy_saving", "reasons": ["manual"]}},
    )
    assert result.value == {"saving": True, "focus": "", "hover": ""}
    rules = css_rules("index.html")
    assert rules.get("html.energy-saving .cover-shell.polychrome::before", "display") == "none !important"
