from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"
FOLLOWUP = ROOT / "tests/test_v072_followup_ln_credentials_progress.py"


def test_previous_word_contract_is_explicit() -> None:
    source = HTML.read_text(encoding="utf-8")
    assert "const previousWord=previous;" in source


def test_toolbar_contract_matches_responsive_structure() -> None:
    source = HTML.read_text(encoding="utf-8")
    test_source = FOLLOWUP.read_text(encoding="utf-8")

    expected = "toolbar.insertBefore(tray,toolbar.querySelector('.ln-reader-actions')||$('lnReaderAppearanceToggle'))"
    assert expected in source
    assert expected in test_source
    assert "toolbar.insertBefore(tray,$('lnReaderAppearanceToggle'))" not in test_source
