from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_companion_http_accepts_study_parser_callback() -> None:
    source = (ROOT / "pudge" / "mobile_sync_http.py").read_text(encoding="utf-8")
    assert "study_parser:" in source
    assert 'request.path == "/api/v1/study/parse"' in source
    assert 'parser(text)' in source
    assert '"study": result' in source


def test_web_app_wires_light_novel_jiten_parser_to_companion() -> None:
    source = (ROOT / "pudge" / "web_app.py").read_text(encoding="utf-8")
    assert "study_parser=api.light_novels.parse_study_text" in source


def test_interactive_subtitle_ui_contract() -> None:
    root = ROOT / "pudge" / "web" / "companion"
    app = (root / "app.js").read_text(encoding="utf-8")
    html = (root / "index.html").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")
    sw = (root / "sw.js").read_text(encoding="utf-8")

    for contract in (
        "PUDGE_COMPANION_INTERACTIVE_SUBTITLES_V13",
        "parseSubtitleStudy",
        "studySegments",
        "fallbackSubtitleSegments",
        "subtitleOffsetSeconds",
        "showSubtitleStudySheet",
        "/api/v1/study/parse",
        "Intl.Segmenter",
    ):
        assert contract in app

    for element_id in (
        "animeSubtitleOffsetMinus",
        "animeSubtitleOffsetValue",
        "animeSubtitleOffsetPlus",
        "animeSubtitleSizeMinus",
        "animeSubtitleSizePlus",
        "animeSubtitleStudySheet",
        "animeStudyWord",
        "animeStudyMeanings",
        "animeStudyReplayLine",
        "animeStudyResume",
        "animeSubtitleDiagnostic",
    ):
        assert f'id="{element_id}"' in html

    assert "app.js?v=13" in html
    assert "styles.css?v=13" in html
    assert ".subtitle-token.state-learning" in css
    assert ".anime-study-sheet" in css
    assert "pudge-companion-shell-v13" in sw


def test_subtitle_overlay_is_not_a_button_anymore() -> None:
    html = (ROOT / "pudge" / "web" / "companion" / "index.html").read_text(encoding="utf-8")
    assert '<div id="animeSubtitleOverlay"' in html
    assert '<button type="button" id="animeSubtitleOverlay"' not in html
