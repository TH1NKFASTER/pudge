from pathlib import Path

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


def test_reader_removes_duplicate_furigana_setting_and_has_context_menus() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert 'id="lnrFurigana"' not in html
    assert "LN_JITEN_RAW_STATES=['new','learning','young','mature','due','failed','known','mastered','never-forget','blacklisted']" in html
    assert "showLnStudyStateMenu(button.id==='lnUnknownFuriganaToggle'?'furigana':'underline'" in html
    assert "lnStudyStateMenu" in html
    assert "lnCardMatchesStates(card,ui.lnFuriganaStates" in html
    assert "ln-study-mark-enabled" in html


def test_underline_menu_only_opens_in_underline_mode() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "button.id==='lnWordMarksToggle'&&String(ui.lnWordMarkStyle" in html
    assert "!=='underline')return" in html
