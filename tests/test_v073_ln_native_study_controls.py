from __future__ import annotations

from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService

ROOT = Path(__file__).resolve().parents[1]

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
    saved = service.save_settings({
        "word_mark_style": "underline",
        "furigana_unknown_only": True,
        "show_furigana": True,
        "study_card_mode": "button",
        "study_card_triggers": ["ShiftLeft", "ShiftRight", "MouseRight", "ShiftLeft"],
    })
    assert saved["word_mark_style"] == "underline"
    assert saved["furigana_unknown_only"] is True
    assert saved["study_card_mode"] == "button"
    assert saved["study_card_triggers"] == ["ShiftLeft", "ShiftRight", "MouseRight"]
    assert LightNovelService(service.config).settings_payload()["study_card_triggers"] == saved["study_card_triggers"]

def test_invalid_trigger_and_modes_fall_back_safely(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.save_settings({
        "word_mark_style": "rainbow",
        "study_card_mode": "telepathy",
        "study_card_triggers": ["", "<script>", "MouseLeft"],
    })
    assert saved["word_mark_style"] == "underline"
    assert saved["study_card_mode"] == "button"
    assert saved["study_card_triggers"] == ["MouseLeft"]

@pytest.mark.parametrize("css", [
    ".ln-reader { letter-spacing: .03em; }",
    "#lnReader p { margin-bottom: 1.4em; }",
    ".ln-reader .state-new { text-decoration-thickness: 3px; }",
    ":root { --ln-reader-bg: #111111; --pudge-study-new: #abcdef; }",
])
def test_reader_css_validator_accepts_reader_only_css(css: str) -> None:
    assert LightNovelService._validate_reader_css(css) == css

@pytest.mark.parametrize("css", [
    "@import url('https://example.com/a.css');",
    ".ln-reader { background-image:url(https://example.com/x); }",
    "body { color:red; }",
    ".ln-reader-toolbar button { color:red; }",
    ".ln-reader { pointer-events:none; }",
    ".ln-reader { display:none; }",
])
def test_reader_css_validator_rejects_unsafe_or_out_of_scope_css(css: str) -> None:
    with pytest.raises(LightNovelError):
        LightNovelService._validate_reader_css(css)

def test_frontend_has_native_controls_multi_triggers_and_persistent_deck() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    tools = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    assert 'id="lnWordMarksToggle"' in html
    assert 'id="lnUnknownFuriganaToggle"' in html
    assert "word-marks-underline" in html
    assert "furigana_unknown_only" in html
    assert "study_card_mode" in html and "study_card_triggers" in html
    assert "lnStudyTriggerEnabled(event.code)" in html
    assert "ShiftLeft" in html and "ShiftRight" in html
    assert "MouseLeft" in html and "MouseRight" in html
    assert 'id="lnrGenerateCss"' in html
    assert "light_novel_generate_reader_css" in html
    assert "rememberedStudyDeck" in tools and "rememberStudyDeck" in tools
    assert "pudge.studyDeck." in tools

def test_backend_exposes_llm_css_with_strict_rules() -> None:
    service_source = (ROOT / "pudge/light_novels.py").read_text(encoding="utf-8")
    api_source = (ROOT / "pudge/web_app.py").read_text(encoding="utf-8")
    assert "def generate_reader_css(" in service_source
    assert "Return strict JSON with exactly one string field named css" in service_source
    assert "Never break word clicking" in service_source
    assert "def light_novel_generate_reader_css(" in api_source
