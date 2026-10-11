"""Leftovers: the old accent #6f8cff and ignored shortcut keys in the example config (behaviour).

Borders, progress bars and buttons used the old accent #6f8cff instead of the theme's
--accent (white text on #6f8cff has a contrast of only 3:1). The example configs listed
app_* shortcuts that config.toml no longer reads (app navigation is not configurable).
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest
from css_support import WEB, css_rules

from pudge import config as config_module

ROOT = Path(__file__).resolve().parents[1]
STYLESHEETS = ["index.html", *sorted(path.name for path in WEB.glob("*.css"))]


def _luminance(color: str) -> float:
    channels = [int(color.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(a: str, b: str) -> float:
    light, dark = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


ACCENT = css_rules("index.html").get(":root", "--accent")


@pytest.mark.parametrize(
    ("sheet", "selector", "prop"),
    [
        ("visual_novels.css", ".vn-line.active", "border-color"),
        ("review_gate.css", ".pudge-review-gate-progress>span", "background"),
        ("review_gate.css", ".pudge-review-gate-grades .easy", "border-color"),
        ("reading_tools.css", ".pudge-grammar-seg", "border-bottom"),
        ("reading_assistant.css", ".pa-composer:focus-within", "border-color"),
        ("reading_assistant.css", ".pa-avatar", "background"),
        ("ln_chapter_swipe.css", ".ln-chapter-swipe-chip", "background"),
        ("ln_chapter_swipe.css", ".ln-chapter-swipe.ready .ln-chapter-swipe-arrow", "background"),
        ("index.html", ".ln-native-reader-toggle.active", "border-color"),
        ("index.html", ".ln-study-trigger-capture.active", "border-color"),
    ],
)
def test_accent_coloured_parts_use_the_theme_accent(sheet, selector, prop) -> None:
    value = css_rules(sheet).get(selector, prop) or ""
    assert "var(--accent" in value and "6f8cff" not in value.lower()


def test_no_stylesheet_rule_uses_the_old_accent() -> None:
    for sheet in STYLESHEETS:
        for _media, selector, declarations in css_rules(sheet).rules:
            for prop, value in declarations.items():
                assert "6f8cff" not in value.lower(), (sheet, selector, prop)


def test_white_text_on_accent_buttons_is_readable() -> None:
    assert ACCENT and _contrast("#ffffff", ACCENT) >= 4.5
    assert css_rules("index.html").get("button.primary", "background") == "var(--accent)"
    # The assistant's avatar (white letter on a gradient) and the chapter swipe arrow.
    avatar = css_rules("reading_assistant.css").get(".pa-avatar", "background") or ""
    stops = [
        ACCENT if "var(--accent" in part else part
        for part in re.findall(r"var\(--accent[^)]*\)|#[0-9a-fA-F]{6}", avatar)
    ]
    assert len(stops) == 2 and all(_contrast("#ffffff", stop) >= 4.5 for stop in stops)
    # Pages without the window's theme (the mpv subtitle assistant) fall back to the same accent.
    assert re.findall(r"var\(--accent,\s*(#[0-9a-fA-F]{6})\)", avatar) == [ACCENT]


@pytest.mark.parametrize("example", [ROOT / "pudge" / "config.example.toml", ROOT / "config.example.toml"])
def test_example_shortcuts_are_all_read_by_the_config(example, monkeypatch) -> None:
    sections: dict[str, dict] = {}
    section = config_module._section

    def recorded(raw, name):
        sections[name] = section(raw, name)
        return sections[name]

    monkeypatch.setattr(config_module, "_section", recorded)
    loaded = config_module.load_config(example, resolve_secrets=False, apply_language=False)
    known = {field.name for field in dataclasses.fields(config_module.ShortcutsConfig)}
    assert sections["shortcuts"] and set(sections["shortcuts"]) <= known
    assert loaded.shortcuts.mpv_mark_watched == sections["shortcuts"]["mpv_mark_watched"]
