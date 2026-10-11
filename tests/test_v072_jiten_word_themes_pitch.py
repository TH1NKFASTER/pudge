from __future__ import annotations

import json
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from tests.css_support import css_rules


def _service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "pudge.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    return LightNovelService(config)


def test_word_color_settings_are_validated_and_persisted(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.save_settings(
        {
            "word_color_theme": "jiten",
            "word_color_new": "#A566EF",
            "word_color_young": "#e8a020",
            "word_color_due": "#ff5940",
            "word_color_mature": "",
            "word_color_blacklisted": "#969696",
        }
    )

    assert saved["word_color_theme"] == "jiten"
    assert saved["word_color_new"] == "#a566ef"
    assert LightNovelService(service.config).settings_payload()["word_color_due"] == "#ff5940"
    # "" (an older Pudge's ordinary text colour) is stored as the state's default.
    assert saved["word_color_mature"] == "#4ade80" and saved["word_color_young"] == "#e8a020"

    rejected = service.save_settings(
        {"word_color_theme": "script", "word_color_new": "url(javascript:bad)", "word_color_due": "red"}
    )
    assert rejected["word_color_theme"] == "jiten"
    assert rejected["word_color_new"] == "#a566ef" and rejected["word_color_due"] == "#ff5940"


def test_jiten_pitch_accents_survive_shared_study_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)

    def fake_parse(action: str, payload: dict | None = None) -> dict:
        assert action == "reader/parse"
        assert payload == {"text": ["日本語"]}
        return {
            "tokens": [[{"wordId": 42, "readingIndex": 0, "start": 0, "end": 3}]],
            "vocabulary": [
                {
                    "wordId": 42,
                    "readingIndex": 0,
                    "spelling": "日本語",
                    "reading": "にほんご",
                    "pitchAccents": [0, 3],
                    "knownState": ["young"],
                }
            ],
        }

    monkeypatch.setattr(service, "_jiten_request", fake_parse)
    result = service.parse_study_text("日本語")
    assert result["vocabulary"][0]["pitchAccents"] == [0, 3]
    assert result["settings"]["word_color_theme"] == "jiten"


def _jiten_parse(action: str, payload: dict | None = None) -> dict:
    assert action == "reader/parse"
    return {
        "tokens": [[{"wordId": 42, "readingIndex": 0, "start": 0, "end": 3}]],
        "vocabulary": [
            {
                "wordId": 42,
                "readingIndex": 0,
                "spelling": "日本語",
                "reading": "にほんご",
                "pitchAccents": [0, 3],
                "knownState": ["young"],
            }
        ],
    }


def test_word_card_draws_pitch_diagrams_and_applies_the_word_theme(
    page, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The backend's own study payload (pitch accents + the reader's word colors) drives the card.
    service = _service(tmp_path)
    service.save_settings(
        {"word_color_theme": "underline", "word_color_due": "#ff5940", "pitch_accent_color": "#12abef"}
    )
    monkeypatch.setattr(service, "_jiten_request", _jiten_parse)
    payload = json.loads(json.dumps(service.parse_study_text("日本語")))
    result = page.run(
        r"""
        const opened = await __settle(PudgeReadingTools.study.openText('日本語', {left: 10, top: 10, right: 40, bottom: 30}));
        const rows = [...document.querySelectorAll('#pudgeStudyCard .pudge-study-pitch .pudge-pitch-row')].map(row => ({
          accent: row.querySelector('.pudge-pitch-number').innerText,
          morae: [...row.querySelectorAll('.pudge-pitch-mora')].map(m => m.innerText + (m.classList.contains('high') ? '↑' : '↓'))}));
        const root = document.documentElement;
        return {opened, open: PudgeReadingTools.study.isOpen(), rows, theme: root.dataset.pudgeStudyTheme,
                due: root.style.getPropertyValue('--pudge-study-due'), pitch: root.style.getPropertyValue('--pudge-pitch-color')};
        """,
        api={"study_parse_text": payload},
    )
    out = result.value
    assert out["opened"] is True and out["open"] is True
    # にほんご: heiban (0) rises after the first mora and stays high on the particle;
    # nakadaka (3) drops after the third mora.
    assert out["rows"] == [
        {"accent": "0", "morae": ["に↓", "ほ↑", "ん↑", "ご↑", "・↑"]},
        {"accent": "3", "morae": ["に↓", "ほ↑", "ん↑", "ご↓", "・↓"]},
    ]
    assert (out["theme"], out["due"], out["pitch"]) == ("underline", "#ff5940", "#12abef")


def test_reader_word_theme_preset_saves_all_nine_status_colors(page, tmp_path: Path) -> None:
    result = page.run(
        r"""
        populateLnReaderAppearance(ui.lnState.settings);
        await __input('#lnrWordTheme', 'jiten');
        await __tick(300);
        return {theme: document.documentElement.dataset.pudgeStudyTheme, blacklisted: $('lnrWordBlacklisted').value};
        """,
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {"jiten_api_key": "key", "word_color_theme": "balanced"},
                "books": [],
            }
        },
    )
    [[sent]] = result.calls_of("light_novel_save_settings")
    colors = {key: sent[key] for key in sent if key.startswith("word_color_")}
    # Jiten's own colours, with mature green and mastered teal.
    assert colors == {
        "word_color_theme": "jiten",
        "word_color_new": "#f43f5e",
        "word_color_not_in_deck": "#f43f5e",
        "word_color_young": "#f59e0b",
        "word_color_mature": "#4ade80",
        "word_color_mastered": "#2dd4bf",
        "word_color_due": "#f97316",
        "word_color_blacklisted": "#9ca3af",
        "word_color_suspended": "#9ca3af",
        "word_color_redundant": "#0ea5e9",
    }
    assert result.value == {"theme": "jiten", "blacklisted": "#9ca3af"}
    # The backend keeps exactly what the window sends.
    saved = _service(tmp_path).save_settings(colors)
    assert {key: saved[key] for key in colors} == colors


@pytest.mark.parametrize(
    "sheet, word", [("reading_tools.css", ".pudge-study-word"), ("index.html", ".ln-word")]
)
def test_underline_theme_marks_only_words_to_study_and_none_theme_marks_nothing(
    sheet: str, word: str
) -> None:
    rules = css_rules(sheet)
    underline = ':root[data-pudge-study-theme="underline"] '
    assert rules.get(underline + word, "color") == "inherit"
    for state in ("new", "not_in_deck", "young", "due"):
        assert rules.get(f"{underline}{word}.state-{state}", "text-decoration-line") == "underline"
        assert (
            rules.get(f"{underline}{word}.state-{state}", "text-decoration-color")
            == f"var(--pudge-study-{state},{STUDY_FALLBACKS[state]})"
        )
    for state in ("mature", "mastered", "blacklisted", "suspended", "redundant"):
        assert rules.declarations(f"{underline}{word}.state-{state}") == {}
    if sheet == "reading_tools.css":
        none = rules.declarations(f':root[data-pudge-study-theme="none"] {word}')
        assert (none.get("color"), none.get("text-decoration")) == ("inherit", "none")
        # The pitch diagram in the card cannot be selected along with the word.
        assert rules.get(".pudge-study-pitch", "user-select") == "none"


STUDY_FALLBACKS = {"new": "#f43f5e", "not_in_deck": "#f43f5e", "young": "#f59e0b", "due": "#f97316"}
