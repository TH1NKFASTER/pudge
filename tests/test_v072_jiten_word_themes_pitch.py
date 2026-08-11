from __future__ import annotations

from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService


ROOT = Path(__file__).resolve().parents[1]


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
            "word_color_learning": "#e8a020",
            "word_color_due": "#ff5940",
            "word_color_known": "#70c000",
            "word_color_blacklisted": "#969696",
        }
    )

    assert saved["word_color_theme"] == "jiten"
    assert saved["word_color_new"] == "#a566ef"
    assert LightNovelService(service.config).settings_payload()["word_color_due"] == "#ff5940"

    rejected = service.save_settings(
        {"word_color_theme": "script", "word_color_new": "url(javascript:bad)"}
    )
    assert rejected["word_color_theme"] == "balanced"
    assert rejected["word_color_new"] == "#a566ef"


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
    assert result["settings"]["word_color_theme"] == "balanced"


def test_shared_reader_renders_pitch_diagrams_and_status_themes() -> None:
    reading_tools = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    reading_css = (ROOT / "pudge/web/reading_tools.css").read_text(encoding="utf-8")
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")

    assert "renderPitchAccent(card)" in reading_tools
    assert "card.pitchAccents" in reading_tools
    assert "pitchMorae" in reading_tools and "pitchPattern" in reading_tools
    assert "applyAppearance: applyStudyAppearance" in reading_tools
    assert ".pudge-study-pitch" in reading_css
    assert 'data-pudge-study-theme="underline"' in reading_css
    assert 'data-pudge-study-theme="none"' in reading_css
    assert "LN_WORD_COLOR_PRESETS" in html
    assert "word_color_blacklisted" in html
    assert 'data-pudge-study-theme="underline"' in html
