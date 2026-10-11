from pathlib import Path

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from t71g20_support import STUDY_BOOK, STUDY_VIEW
from tests.i18n_support import en, ru


def _service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "pudge.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    return LightNovelService(config)


def test_jiten_state_filters_round_trip(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.save_settings(
        {
            "furigana_states": ["new", "mature", "due"],
            "underline_states": ["new", "young", "mastered"],
        }
    )
    assert saved["furigana_states"] == ["new", "mature", "due"]
    assert saved["underline_states"] == ["new", "young", "mastered"]


def test_state_filters_are_canonicalized(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.save_settings(
        {
            "furigana_states": ["mature", "MATURE", "bogus"],
            "underline_states": [],
        }
    )
    assert saved["furigana_states"] == ["mature"]
    assert "bogus" not in saved["furigana_states"]
    assert saved["underline_states"]


# Jiten states in the order the menus list them, with their catalog keys.
STATES = [
    "new",
    "learning",
    "young",
    "mature",
    "due",
    "failed",
    "known",
    "mastered",
    "never-forget",
    "blacklisted",
]
LABEL_KEYS = {
    state: "uiB.jitenState." + ("neverForget" if state == "never-forget" else state) for state in STATES
}


def test_state_menus_choose_which_words_get_furigana_and_underlines(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            STUDY_VIEW
            + r"""
            const open = async selector => {
              document.querySelector(selector).dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true, button: 2}));
              await __tick(0);
              const menu = $('lnStudyStateMenu');
              return {title: menu.querySelector('.ln-study-state-menu-title').textContent,
                      options: [...menu.querySelectorAll('[data-ln-study-state]')].map(i => [i.dataset.lnStudyState, i.checked, i.nextElementSibling.textContent])};
            };
            const out = {before: {furigana: furigana(), marked: marked()}, oldSetting: $('lnrFurigana')};
            out.furiganaMenu = await open('#lnUnknownFuriganaToggle');
            await __input('#lnStudyStateMenu [data-ln-study-state="young"]', false);    // 戻す is young
            out.afterFurigana = furigana();
            out.underlineMenu = await open('#lnWordMarksToggle');
            await __input('#lnStudyStateMenu [data-ln-study-state="known"]', false);    // それ is known
            out.afterUnderline = marked();
            await __tick(300);
            out.titles = [t('ui.furiganaFor'), t('ui.underline')];
            return out;
            """,
            lang=lang,
            before=STUDY_BOOK,
        )
        out = result.value
        labels = [(en if lang == "en" else ru)(LABEL_KEYS[state]) for state in STATES]
        # Furigana by default for words still being learned; underlines for every state.
        assert out["before"]["furigana"] == {
            "猫": [["猫", "ねこ"]],
            "戻せば": [["戻", "もど"]],
            "それ": [],
            "目に入った": [["目", "め"], ["入", "はい"]],
        }
        assert out["before"]["marked"] == ["猫", "戻せば", "それ", "目に入った"]
        assert out["oldSetting"] is None  # the reader settings no longer have a second furigana switch
        assert out["furiganaMenu"]["title"] == out["titles"][0]
        assert out["furiganaMenu"]["options"] == [
            [state, state in STATES[:6], label] for state, label in zip(STATES, labels)
        ]
        assert out["afterFurigana"]["戻せば"] == [] and out["afterFurigana"]["猫"] == [["猫", "ねこ"]]
        assert out["underlineMenu"]["title"] == out["titles"][1]
        assert out["underlineMenu"]["options"] == [
            [state, True, label] for state, label in zip(STATES, labels)
        ]
        assert out["afterUnderline"] == ["猫", "戻せば", "目に入った"]
        saved = {
            key: value
            for [patch] in result.calls_of("light_novel_save_settings")
            for key, value in patch.items()
        }
        assert saved["furigana_states"] == ["new", "learning", "mature", "due", "failed"]
        assert saved["underline_states"] == [state for state in STATES if state != "known"]


def test_saved_state_filters_apply_when_the_reader_opens(page) -> None:
    result = page.run(
        STUDY_VIEW + "return {furigana: furigana(), marked: marked()};",
        before=STUDY_BOOK,
        api={
            "light_novel_state": {
                "version": 1,
                "books": [],
                "settings": {"furigana_states": ["due"], "underline_states": ["new", "known"]},
            }
        },
    )
    assert result.value["furigana"] == {
        "猫": [],
        "戻せば": [],
        "それ": [],
        "目に入った": [["目", "め"], ["入", "はい"]],
    }
    assert result.value["marked"] == ["猫", "それ"]
