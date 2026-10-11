"""Word-state colours have no "Text color" (no colour) option any more: every state always
has a real colour. Empty colours saved by an earlier Pudge, and Jiten's null colours, become
Pudge's default for that state."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from pudge.word_states import jiten_profile_colors

DEFAULTS = {
    "new": "#f43f5e",
    "not_in_deck": "#f43f5e",
    "young": "#f59e0b",
    "mature": "#4ade80",
    "mastered": "#2dd4bf",
    "due": "#f97316",
    "blacklisted": "#9ca3af",
    "suspended": "#9ca3af",
    "redundant": "#0ea5e9",
}
STATES = list(DEFAULTS)


def _service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "pudge.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    return LightNovelService(config)


def _rows(service: LightNovelService, rows: dict[str, str]) -> None:
    with service._connection() as conn:
        for key, value in rows.items():
            conn.execute("INSERT OR REPLACE INTO ln_settings(key,value) VALUES(?,?)", (key, value))


def test_new_settings_give_every_state_a_colour(tmp_path: Path) -> None:
    payload = _service(tmp_path).settings_payload()
    assert {state: payload[f"word_color_{state}"] for state in STATES} == DEFAULTS


def test_empty_colours_from_an_earlier_save_load_as_the_default(tmp_path: Path) -> None:
    service = _service(tmp_path)
    block = (
        "/* Pudge colors */\n:root {\n  --pudge-study-young: #123456;\n"
        "  --pudge-study-mature: currentColor;\n  --pudge-study-mastered: currentColor;\n}\n/* /Pudge colors */"
    )
    _rows(
        service,
        {
            "word_color_young": "#123456",
            "word_color_mature": "",
            "word_color_mastered": "",
            "word_color_due": "text",
            "custom_css": block,
        },
    )
    payload = service.settings_payload()
    assert {state: payload[f"word_color_{state}"] for state in STATES} == {
        **DEFAULTS,
        "young": "#123456",
    }
    # The colour block an earlier Pudge wrote no longer paints them in the text colour.
    assert "currentColor" not in payload["custom_css"]
    assert "--pudge-study-mature: #4ade80;" in payload["custom_css"]
    assert "--pudge-study-mastered: #2dd4bf;" in payload["custom_css"]
    assert "--pudge-study-young: #123456;" in payload["custom_css"]


def test_saving_an_empty_colour_stores_the_default(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save_settings({"word_color_mature": "#010203", "word_color_mastered": "#040506"})
    saved = service.save_settings({"word_color_mature": "", "word_color_mastered": None})
    assert (saved["word_color_mature"], saved["word_color_mastered"]) == ("#4ade80", "#2dd4bf")
    again = LightNovelService(service.config).settings_payload()
    assert (again["word_color_mature"], again["word_color_mastered"]) == ("#4ade80", "#2dd4bf")


def test_jiten_null_colour_becomes_pudges_default() -> None:
    payload = {
        "profiles": [
            {
                "name": "Desk",
                "values": {"stateColours": {"new": "#111111", "mature": None, "due": None, "ignored": ""}},
            }
        ]
    }
    colors = jiten_profile_colors(payload)["colors"]
    assert colors == {
        **DEFAULTS,
        "new": "#111111",
        "not_in_deck": "#111111",
    }
    assert all(value.startswith("#") for value in colors.values())


def test_iphone_gets_a_real_colour_for_an_empty_saved_one() -> None:
    from pudge.iphone_media import IPhoneMedia

    settings = SimpleNamespace(**{f"word_color_{state}": "" for state in STATES})
    media = IPhoneMedia.__new__(IPhoneMedia)
    media._reader_settings = lambda: settings
    colors = media.reader_settings()["colors"]
    assert {state: colors[state] for state in STATES} == DEFAULTS
    assert (colors["learning"], colors["known"]) == ("#f59e0b", "#2dd4bf")


SETTINGS = {
    "version": 1,
    "settings": {
        "jiten_api_key": "key",
        "word_color_theme": "custom",
        **{f"word_color_{s}": c for s, c in DEFAULTS.items()},
        "word_color_mature": "",
        "word_color_mastered": "",
    },
    "books": [],
}


@pytest.mark.parametrize(("lang", "text"), [("en", "Text color"), ("ru", "Цвет текста")])
def test_word_colour_rows_have_no_text_colour_option(page, lang, text) -> None:
    result = page.run(
        r"""
        populateLnReaderAppearance(ui.lnState.settings);
        const ids = LN_WORD_COLOR_STATES.map(([, id]) => id);
        const rows = ids.map(id => {
          const picker = $(id).closest('.ln-word-color-picker');
          return {inputs: [...picker.querySelectorAll('input')].map(i => i.type), label: picker.textContent.trim(),
                  textBox: !!document.getElementById(`${id}Text`), disabled: $(id).disabled, value: lnWordColorValue(id)};
        });
        const checks = [...document.querySelectorAll('#lnReaderAppearance .ln-word-color-picker input[type=checkbox]')].length;
        return {rows, checks};
        """,
        lang=lang,
        api={"light_novel_state": SETTINGS},
    )
    out = result.value
    assert out["checks"] == 0
    for row in out["rows"]:
        assert row["inputs"] == ["color"]
        assert row["textBox"] is False and row["disabled"] is False
        assert text not in row["label"]
    # Empty colours from an earlier save show the state's default.
    assert [row["value"] for row in out["rows"]] == list(DEFAULTS.values())


def test_window_saves_real_colours_after_loading_empty_ones(page) -> None:
    result = page.run(
        r"""
        populateLnReaderAppearance(ui.lnState.settings);
        await __input('#lnrWordYoung', '#00ff00'); await __tick(300);
        const saved = __apiCalls.filter(c => c.name === 'light_novel_save_settings').map(c => c.args[0]).at(-1);
        const style = document.documentElement.style;
        return {saved, css: $('lnrCustomCss').value,
                mature: style.getPropertyValue('--pudge-study-mature'), mastered: style.getPropertyValue('--pudge-study-mastered')};
        """,
        api={"light_novel_state": SETTINGS},
    )
    out = result.value
    assert {s: out["saved"][f"word_color_{s}"] for s in STATES} == {**DEFAULTS, "young": "#00ff00"}
    assert "currentColor" not in out["css"]
    assert "--pudge-study-mature: #4ade80;" in out["css"]
    assert (out["mature"], out["mastered"]) == ("#4ade80", "#2dd4bf")


def test_jiten_profile_null_colour_fills_the_default_in_the_window(page) -> None:
    profile = {
        "ok": True,
        "profileName": "Desk",
        "profileCount": 1,
        "colors": {**DEFAULTS, "new": "#111111", "mature": "", "mastered": None},
    }
    result = page.run(
        r"""
        populateLnReaderAppearance(ui.lnState.settings);
        await __click('#lnrWordJitenProfile'); await __tick(300);
        return LN_WORD_COLOR_STATES.map(([, id]) => lnWordColorValue(id));
        """,
        api={"light_novel_state": SETTINGS, "light_novel_jiten_profile_colors": profile},
    )
    assert result.value == list({**DEFAULTS, "new": "#111111"}.values())
