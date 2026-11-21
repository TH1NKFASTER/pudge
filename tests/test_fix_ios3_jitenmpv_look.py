"""IOS3 item 6: the phone's subtitles take the JitenMPV look from its real config.json.

JitenMPV writes snake_case keys (theme, custom_theme_colors, font_family, font_size,
border_size, subtitle_margin_y, subtitle_alignment); Pudge read PascalCase ones, so the phone
always showed the Default theme. PascalCase stays as a fallback. mpv/Windows font names map to
a Japanese gothic the phone has.
"""

from __future__ import annotations

import json

from pudge.iphone_media import ios_subtitle_font, subtitle_look

STATES = ("New", "Young", "Mature", "Blacklisted", "Due", "Mastered", "Redundant", "Suspended")


def _custom_config() -> dict:
    rows = {}
    for name in STATES:
        rows[name] = {
            "text_color": "#FFFFFF",
            "outline_color": "#36449B" if name == "New" else "#70C000",
            "outline_size": 2,
            "bold": False,
        }
    return {
        "theme": "Custom",
        "custom_theme_colors": rows,
        "font_family": "Gothic",
        "font_size": 38,
        "border_size": 1,
        "subtitle_margin_y": 90,
        "subtitle_alignment": 2,
    }


def test_snake_case_config_gives_the_custom_theme(tmp_path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps(_custom_config()), encoding="utf-8")
    look = subtitle_look(config)
    assert look["source"] == "jiten-mpv"
    assert look["theme"] == "Custom"
    assert look["fontFamily"] == "Hiragino Sans"
    assert look["fontSize"] == 38
    assert look["borderSize"] == 1
    assert look["marginY"] == 90 and look["alignment"] == 2
    assert look["states"][0] == {
        "text": "#FFFFFF",
        "outline": "#36449B",
        "outlineSize": 2.0,
        "bold": False,
        "italic": False,
        "underline": False,
    }
    for state in look["states"][1:]:
        assert state["outline"] == "#70C000" and state["text"] == "#FFFFFF" and state["outlineSize"] == 2.0
    # The text that is not a word takes mpv's border size.
    assert look["unparsed"]["outlineSize"] == 1


def test_snake_case_preset_theme_and_sizes(tmp_path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"theme": "Underline", "font_size": 52, "border_size": 2}), encoding="utf-8")
    look = subtitle_look(config)
    assert look["theme"] == "Underline" and look["fontSize"] == 52 and look["borderSize"] == 2
    assert look["states"][0]["underlineColor"] == "#a566ef"


def test_pascal_case_still_read_as_a_fallback(tmp_path) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "Theme": "Custom",
                "CustomThemeColors": {
                    "Young": {"text_color": "#123456", "outline_color": "#000000", "outline_size": 2}
                },
                "FontFamily": "Hiragino Mincho ProN",
                "FontSize": 44,
                "BorderSize": 2,
            }
        ),
        encoding="utf-8",
    )
    look = subtitle_look(config)
    assert look["theme"] == "Custom" and look["fontSize"] == 44 and look["borderSize"] == 2
    assert look["fontFamily"] == "Hiragino Mincho ProN"
    assert look["states"][1]["text"] == "#123456"
    assert look["states"][0]["text"] == "#a566ef"  # missing custom states keep the Default theme


def test_missing_or_broken_config_is_the_default_look(tmp_path) -> None:
    assert subtitle_look(tmp_path / "missing.json")["source"] == "default"
    broken = tmp_path / "config.json"
    broken.write_text("[1, 2]", encoding="utf-8")
    look = subtitle_look(broken)
    assert look["source"] == "default" and look["theme"] == "Default" and look["fontSize"] == 48


def test_font_names_map_to_an_ios_japanese_gothic() -> None:
    for name in (
        "Gothic",
        "MS Gothic",
        "ms  gothic",
        "Noto Sans JP",
        "Noto Sans CJK JP",
        "Meiryo",
        "",
        None,
        "Comic Font X",
    ):
        assert ios_subtitle_font(name) == "Hiragino Sans"
    assert ios_subtitle_font("Hiragino Kaku Gothic ProN") == "Hiragino Kaku Gothic ProN"
    # Hiragino Sans GB is Chinese: the Japanese gothic instead.
    assert ios_subtitle_font("Hiragino Sans GB") == "Hiragino Sans"
