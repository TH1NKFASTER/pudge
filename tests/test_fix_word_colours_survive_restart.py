"""Word colours chosen in the reader settings survive a restart. An older Pudge left an
unmarked ":root { --pudge-study-new: …; --pudge-study-learning: … }" line in the custom CSS;
its new/due/blacklisted variables share names with today's and put the old colours back
into the pickers on every start (the user's settings from the debug archive)."""

from __future__ import annotations

from pathlib import Path

from tests.test_fix_word_states_jiten import JITEN_DEFAULTS, _legacy_rows, _service

STALE = (
    ":root { --pudge-study-new: #f3f6fb; --pudge-study-learning: #f4bd63; --pudge-study-due: #ff7d8c; "
    "--pudge-study-known: #57d38c; --pudge-study-blacklisted: #7d8795; --pudge-pitch-color: #9ec5ff; }"
)
MANAGED = (
    "/* Pudge colors */\n:root {\n  --pudge-study-new: #ffffff;\n  --pudge-study-not_in_deck: #be38f3;\n"
    "  --pudge-study-young: #ffffff;\n  --pudge-study-mature: #ffffff;\n  --pudge-study-mastered: #ffffff;\n"
    "  --pudge-study-due: #ff7d8c;\n  --pudge-study-blacklisted: #ffffff;\n  --pudge-study-suspended: #ffffff;\n"
    "  --pudge-study-redundant: #ffffff;\n}\n/* /Pudge colors */\n"
)
USER_ROWS = {
    "word_color_theme": "custom",
    "word_color_new": "#ffffff",
    "word_color_learning": "#f4bd63",
    "word_color_due": "#ff7d8c",
    "word_color_known": "#57d38c",
    "word_color_blacklisted": "#ffffff",
    "word_color_not_in_deck": "#be38f3",
    "word_color_young": "#ffffff",
    "word_color_mature": "#ffffff",
    "word_color_mastered": "#ffffff",
    "word_color_suspended": "#ffffff",
    "word_color_redundant": "#ffffff",
    "custom_css": STALE + "\n\n" + MANAGED,
}


def test_the_old_colour_line_is_dropped_and_chosen_colours_stay(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _legacy_rows(service, USER_ROWS)
    payload = service.settings_payload()
    assert payload["word_color_new"] == "#ffffff" and payload["word_color_blacklisted"] == "#ffffff"
    # Due only held the old default because the old line had pushed it back: Jiten's again.
    assert payload["word_color_due"] == JITEN_DEFAULTS["due"]
    assert payload["word_color_not_in_deck"] == "#be38f3"
    assert "--pudge-study-learning" not in payload["custom_css"]
    assert payload["custom_css"].startswith("/* Pudge colors */")


def test_a_user_rule_with_those_names_is_left_alone(tmp_path: Path) -> None:
    service = _service(tmp_path)
    mine = ".ln-reader { --pudge-study-new: #010203; }"
    _legacy_rows(service, {**USER_ROWS, "custom_css": mine + "\n\n" + MANAGED})
    payload = service.settings_payload()
    assert payload["custom_css"].startswith(mine)
    assert payload["word_color_due"] == "#ff7d8c"  # nothing to repair without the old line


READER = """
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['本文です。'], tokens: [[]]});
__api.light_novel_save_settings = async values => ({settings: values});
"""


def test_the_pickers_take_the_last_declaration_of_a_colour(page) -> None:
    # Custom CSS that still holds an earlier declaration of a colour: the later one (the
    # Pudge colour block at the end) is what the reader shows and what the picker shows.
    css = ":root { --pudge-study-new: #f3f6fb; --pudge-study-blacklisted: #7d8795; }\n\n" + MANAGED
    result = page.run(
        """
        await __settle(openLightNovel(6));
        await __click('#lnReaderAppearanceToggle'); await __tick(20);
        return {new: $('lnrWordNew').value, blacklisted: $('lnrWordBlacklisted').value, due: $('lnrWordDue').value};
        """,
        before=READER,
        state={"settings": {"review_gate_ln_enabled": False}},
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {
                    "jiten_api_key": "key",
                    "word_color_theme": "custom",
                    "word_color_new": "#ffffff",
                    "word_color_blacklisted": "#ffffff",
                    "word_color_due": "#ff7d8c",
                    "custom_css": css,
                },
                "books": [{"id": 6, "title": "Novel", "volume": 1}],
            }
        },
    )
    assert result.value == {"new": "#ffffff", "blacklisted": "#ffffff", "due": "#ff7d8c"}
