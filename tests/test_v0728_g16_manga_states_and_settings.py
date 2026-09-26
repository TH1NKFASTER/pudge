from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge import web_app

ROOT = Path(__file__).resolve().parents[1]


class _LightNovelsStub:
    def settings(self):
        return SimpleNamespace(study_backend="jiten")

    def study_provider_capabilities(self, backend: str):
        assert backend == "jiten"
        return {"configured": True, "account_key": "jiten:scope-test"}

    def jiten_live_state(self, word_id: int, reading_index: int, *, force: bool = False):
        return {
            "ok": True,
            "provider": "jiten",
            "wordId": word_id,
            "readingIndex": reading_index,
            "states": ["mature"],
            "normalizedState": "learning",
            "stale": False,
        }

    def jiten_live_states(self, pairs, *, force: bool = False):
        return [
            {
                "ok": True,
                "provider": "jiten",
                "wordId": int(word_id),
                "readingIndex": int(reading_index),
                "states": ["mature"],
                "normalizedState": "learning",
                "stale": False,
            }
            for word_id, reading_index in pairs
        ]

    def jiten_optimal_word_stats(self):
        return {"matureWords": 2000, "optimalWordLimit": 4000}


def test_study_state_responses_are_account_scoped() -> None:
    api = object.__new__(web_app.WebAppApi)
    api.light_novels = _LightNovelsStub()
    single = api.study_state({"backend": "jiten", "word_id": 12, "reading_index": 1})
    assert single["account_scope"] == "jiten:scope-test"
    batch = api.study_states({"backend": "jiten", "words": [[12, 1], [13, 0]]})
    assert batch["account_scope"] == "jiten:scope-test"
    assert len(batch["states"]) == 2


def test_frontend_accepts_capability_scope_for_mixed_version_window() -> None:
    js = (ROOT / "pudge/web/reading_tools.js").read_text(encoding="utf-8")
    assert "if (!accountScope) accountScope = String(caps?.account_key || '')" in js
    assert "currentAccountScope() { return studyStateScope; }" in js


def test_manga_settings_have_click_away_and_escape_owner() -> None:
    manga = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    index = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    assert "function closeReaderSettings()" in manga
    assert "!event.target?.closest?.('#mangaV2Settings')" in manga
    assert "closeSettingsIfOpen: closeReaderSettings" in manga
    assert "if(window.PudgeMangaReaderV2?.closeSettingsIfOpen?.())return;" in index


def test_select_enhancement_is_atomic_and_idempotent() -> None:
    js = (ROOT / "pudge/web/pudge_select.js").read_text(encoding="utf-8")
    assert "function hideNativeSelect(select)" in js
    assert "select.dataset.pudgeSelectEnhanced === '1'" in js
    assert "select.style.setProperty(name, value, 'important')" in js


def test_manga_debug_reports_status_layer_scope_and_box_count() -> None:
    js = (ROOT / "pudge/web/manga_reader_v2.js").read_text(encoding="utf-8")
    assert "mangaDebugRecord('status_layer'" in js
    assert "account_scope:String(window.PudgeReadingTools?.study?.currentAccountScope?.() || '')" in js
