from __future__ import annotations

from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService
from study_page_support import LN_BOOK
from test_web_app import make_api


def _service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "pudge.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    return LightNovelService(config)


def test_native_reader_study_settings_persist(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.save_settings(
        {
            "word_mark_style": "underline",
            "furigana_unknown_only": True,
            "show_furigana": True,
            "study_card_mode": "button",
            "study_card_triggers": ["ShiftLeft", "ShiftRight", "MouseRight", "ShiftLeft"],
        }
    )
    assert saved["word_mark_style"] == "underline"
    assert saved["furigana_unknown_only"] is True
    assert saved["study_card_mode"] == "button"
    assert saved["study_card_triggers"] == ["ShiftLeft", "ShiftRight", "MouseRight"]
    assert (
        LightNovelService(service.config).settings_payload()["study_card_triggers"]
        == saved["study_card_triggers"]
    )


def test_invalid_trigger_and_modes_fall_back_safely(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.save_settings(
        {
            "word_mark_style": "rainbow",
            "study_card_mode": "telepathy",
            "study_card_triggers": ["", "<script>", "MouseLeft"],
        }
    )
    assert saved["word_mark_style"] == "underline"
    assert saved["study_card_mode"] == "button"
    assert saved["study_card_triggers"] == ["MouseLeft"]


@pytest.mark.parametrize(
    "css",
    [
        ".ln-reader { letter-spacing: .03em; }",
        "#lnReader p { margin-bottom: 1.4em; }",
        ".ln-reader .state-new { text-decoration-thickness: 3px; }",
        ":root { --ln-reader-bg: #111111; --pudge-study-new: #abcdef; }",
    ],
)
def test_reader_css_validator_accepts_reader_only_css(css: str) -> None:
    assert LightNovelService._validate_reader_css(css) == css


@pytest.mark.parametrize(
    "css",
    [
        "@import url('https://example.com/a.css');",
        ".ln-reader { background-image:url(https://example.com/x); }",
        "body { color:red; }",
        ".ln-reader-toolbar button { color:red; }",
        ".ln-reader { pointer-events:none; }",
        ".ln-reader { display:none; }",
    ],
)
def test_reader_css_validator_rejects_unsafe_or_out_of_scope_css(css: str) -> None:
    with pytest.raises(LightNovelError):
        LightNovelService._validate_reader_css(css)


# Light novel 6 (study_page_support.LN_BOOK) with a Jiten key and the card on triggers.
def _settings(**extra) -> dict:
    settings = {
        "jiten_api_key": "key",
        "study_card_mode": "button",
        "study_card_triggers": ["ShiftLeft", "MouseRight"],
        **extra,
    }
    return {"light_novel_state": {"version": 1, "settings": settings, "books": []}}


READER = r"""
await __settle(openLightNovel(6)); await __tick(300);
const cat = '#lnReader [data-ln-token="0:0"]';
const card = () => document.getElementById('pudgeStudyCard')?.classList.contains('open')
  ? document.querySelector('#pudgeStudyCard .pudge-study-term').innerText : null;
const closeCard = async () => { PudgeReadingTools.study.close(); await __tick(50); };
const saved = () => __apiCalls.filter(c => c.name === 'light_novel_save_settings').map(c => c.args[0]);
"""


def test_jiten_card_opens_only_with_the_configured_triggers(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            READER
            + r"""
            const out = {};
            await __click(cat); await __tick(100); out.leftClick = card(); await closeCard();
            await __fire(cat, 'pointerover', {}, 'PointerEvent');
            await __key('Shift', {code: 'ShiftRight'}, 'body'); await __tick(100); out.rightShift = card();
            await __key('Shift', {code: 'ShiftLeft'}, 'body'); await __tick(100); out.leftShift = card(); await closeCard();
            await __fire(cat, 'contextmenu', {button: 2}, 'MouseEvent'); await __tick(100); out.rightClick = card(); await closeCard();
            out.triggers = [...document.querySelectorAll('#lnrStudyTriggerList .ln-study-trigger-chip')].map(chip => chip.firstChild.textContent);
            // Add Right Shift in the reader settings: the next key press is captured.
            await __click('#lnrAddStudyTrigger');
            out.capturing = __text('#lnrAddStudyTrigger');
            await __key('Shift', {code: 'ShiftRight'}, 'body'); await __tick(300);
            out.added = [...document.querySelectorAll('#lnrStudyTriggerList .ln-study-trigger-chip')].map(chip => chip.firstChild.textContent);
            await __fire(cat, 'pointerover', {}, 'PointerEvent');
            await __key('Shift', {code: 'ShiftRight'}, 'body'); await __tick(100); out.rightShiftNow = card(); await closeCard();
            // Remove the right click: it no longer opens the card.
            await __click('[data-ln-study-trigger-remove="MouseRight"]'); await __tick(300);
            await __fire(cat, 'contextmenu', {button: 2}, 'MouseEvent'); await __tick(100); out.rightClickRemoved = card();
            out.labels = ['uiB.keys.leftShift', 'ui.rightClick', 'uiB.keys.rightShift', 'ui.pressAKeyButton'].map(key => t(key));
            out.saved = saved();
            return out;
            """,
            lang=lang,
            before=LN_BOOK,
            api=_settings(),
        )
        out = result.value
        assert out["leftClick"] is None and out["rightShift"] is None
        assert out["leftShift"] == "猫ねこ" and out["rightClick"] == "猫ねこ"
        left_shift, right_click, right_shift, press_key = out["labels"]
        assert out["triggers"] == [left_shift, right_click]
        assert out["capturing"] == press_key
        assert out["added"] == [left_shift, right_click, right_shift]
        assert out["rightShiftNow"] == "猫ねこ" and out["rightClickRemoved"] is None
        assert [patch["study_card_triggers"] for patch in out["saved"]] == [
            ["ShiftLeft", "MouseRight", "ShiftRight"],
            ["ShiftLeft", "ShiftRight"],
        ]
        assert out["saved"][-1]["study_card_mode"] == "button"


def test_card_on_hover_mode_opens_the_card_after_a_short_hover(page) -> None:
    result = page.run(
        READER
        + r"""
        await __fire(cat, 'pointerover', {}, 'PointerEvent');
        await __tick(100);
        const early = card();
        await __tick(150);
        return {early, later: card()};
        """,
        before=LN_BOOK,
        api=_settings(study_card_mode="hover"),
    )
    assert result.value == {"early": None, "later": "猫ねこ"}


def test_local_llm_writes_reader_css_from_a_description(page) -> None:
    result = page.run(
        READER
        + r"""
        await __click('#lnReaderAppearanceToggle');
        await __input('#lnrCustomCss', 'wider letter spacing');  // a description instead of the CSS
        await __click('#lnrGenerateCss'); await __tick(400);
        const out = {css: $('lnrCustomCss').value, toasts: __toasts(), done: t('ui.cssGeneratedByLocalLlm'), saved: saved()};
        await __input('#lnrCustomCss', '');
        await __click('#lnrGenerateCss'); await __tick(400);     // nothing to describe: no request
        return out;
        """,
        before=LN_BOOK
        + "__api.light_novel_generate_reader_css = async () => ({css: '.ln-reader { letter-spacing: .05em; }'});",
        api=_settings(custom_css="#lnReader p { margin-bottom: 1em; }"),
    )
    out = result.value
    assert result.calls_of("light_novel_generate_reader_css") == [
        ["wider letter spacing", "#lnReader p { margin-bottom: 1em; }"]
    ]
    assert out["css"] == ".ln-reader { letter-spacing: .05em; }"
    assert out["done"] in out["toasts"]
    assert out["saved"][-1]["custom_css"] == ".ln-reader { letter-spacing: .05em; }"


DECKS = {"study_decks": [{"id": 12, "name": "Deck A"}, {"id": 13, "name": "Deck B"}]}


def test_chosen_study_deck_is_remembered_for_the_next_card(page) -> None:
    pick = page.run(
        READER
        + r"""
        await __fire(cat, 'pointerover', {}, 'PointerEvent');
        await __key('Shift', {code: 'ShiftLeft'}, 'body'); await __tick(100);
        await __input('#pudgeStudyDeck', '13');
        return localStorage.getItem('pudge.studyDeck.jiten');
        """,
        before=LN_BOOK,
        api={**_settings(), **DECKS},
    )
    assert pick.value == "13"
    assert pick.calls_of("set_study_deck_preference") == [["jiten", "13"]]
    reopen = (
        READER
        + r"""
    await __fire(cat, 'pointerover', {}, 'PointerEvent');
    await __key('Shift', {code: 'ShiftLeft'}, 'body'); await __tick(100);
    return $('pudgeStudyDeck').value;
    """
    )
    # The same window session (local storage) and after a relaunch (the app's database).
    assert (
        page.run(
            reopen, before=LN_BOOK, api={**_settings(), **DECKS}, localStorage={"pudge.studyDeck.jiten": "13"}
        ).value
        == "13"
    )
    assert (
        page.run(
            reopen, before=LN_BOOK, api={**_settings(), **DECKS, "study_deck_preference": {"deck_id": "13"}}
        ).value
        == "13"
    )


class _FakeLlm:
    requests: list[tuple[str, str]] = []
    answer: object = None

    def __init__(self, _config) -> None:
        pass

    def json_chat(self, system: str, user: str):
        type(self).requests.append((system, user))
        return type(self).answer

    def close(self) -> None:
        pass


def test_reader_css_generation_asks_the_local_llm_for_safe_reader_css(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("pudge.light_novels.OllamaClient", _FakeLlm)
    _FakeLlm.requests = []
    api = make_api(tmp_path)
    with pytest.raises(LightNovelError):  # no local LLM configured
        api.light_novel_generate_reader_css("bigger text")
    api.light_novels.config.llm.enabled = True
    api.light_novels.config.llm.model = "test-model"
    _FakeLlm.answer = {"css": "```css\n.ln-reader { letter-spacing: .05em; }\n```"}
    assert api.light_novel_generate_reader_css("wider letters", "#lnReader p { margin: 0; }") == {
        "css": ".ln-reader { letter-spacing: .05em; }"
    }
    [(system, user)] = _FakeLlm.requests
    assert "Return strict JSON with exactly one string field named css" in system
    assert "Never break word clicking" in system
    assert "#lnReader p { margin: 0; }" in user and "wider letters" in user
    # CSS outside the reader (or that hides it) is refused, whatever the model says.
    _FakeLlm.answer = {"css": "body { display:none; }"}
    with pytest.raises(LightNovelError):
        api.light_novel_generate_reader_css("hide everything")
    with pytest.raises(LightNovelError):
        api.light_novel_generate_reader_css("   ")
