"""Word status on the iPhone/iPad after grading, and Due before every other state.

* A word graded in the phone's word card kept its old colour in the subtitles, the reader and
  looked-up texts: nothing applied the new state. The Mac's grade answer now carries the word's
  fresh state row and the phone recolours every occurrence (ios/PudgeCore GradedWords).
* The Mac shows a due word as Due whatever else it is (word_states.WORD_STATE_PRIORITY); the
  phone's subtitle look (JitenMPV state, bits 3-6) put Blacklisted/Mastered/Redundant/Suspended
  first and the word card labelled a mature-but-due word "Mature": the status seemed to jump
  from Due to Mature/Known.
* A state request sent to Jiten before a grade and answered after it was cached as fresh, so the
  pre-review state came back for up to 15 minutes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.config import AppConfig
from pudge.database import Database
from pudge.iphone_media import JITEN_STATES, IPhoneMedia, jiten_state, token_flags
from pudge.light_novels import LightNovelService
from pudge.word_states import normalized_word_state

KEY = "jiten-test-key"


@pytest.mark.parametrize("other", ["blacklisted", "mastered", "redundant", "suspended", "mature", "young"])
def test_a_due_word_is_due_in_the_phones_subtitle_look_like_on_the_mac(other):
    card = {"states": [other, "due"]}
    assert normalized_word_state(card["states"]) == "due"
    assert JITEN_STATES[jiten_state(card)] == "due"
    assert (token_flags(card, set(), set()) >> 3) & 15 == JITEN_STATES.index("due") + 1


def test_the_word_card_labels_a_mature_but_due_word_due():
    detail = {"dictionary_complete": True, "spelling": "猫"}
    live = {"states": ["mature", "due"], "rawStateLabel": "Mature"}
    view = IPhoneMedia._word_view(7, 0, detail, live, "猫")
    assert view["state"] == 2 and view["stateLabel"] == "Due"
    live = {"states": ["mastered"], "rawStateLabel": "Mastered"}
    assert IPhoneMedia._word_view(7, 0, detail, live, "猫")["stateLabel"] == "Mastered"


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _api(**values):
    settings = SimpleNamespace(study_backend="jiten", review_mode="binary")
    return SimpleNamespace(light_novels=SimpleNamespace(settings=lambda: settings), **values)


def test_the_grade_answer_carries_the_words_new_state(service):
    asked = []

    def states(payload):
        asked.append(payload)
        return {"ok": True, "states": [{"wordId": 4, "readingIndex": 1, "states": ["young"], "stale": False}]}

    service.api = _api(study_action=lambda payload: {"ok": True}, study_states=states)
    result = service.media.study_action(
        {"action": "review", "wordId": 4, "readingIndex": 1, "grade": "good", "attemptId": "a" * 32}
    )
    assert result["ok"] is True and result["outcome"] == "saved"
    assert asked == [{"pairs": [[4, 1]]}]
    word, reading, state, flags = result["row"]
    assert (word, reading, state) == (4, 1, 1)
    assert JITEN_STATES[((flags >> 3) & 15) - 1] == "young"


def test_no_state_row_when_the_state_is_stale_or_unavailable(service):
    stale = {"ok": True, "states": [{"wordId": 4, "readingIndex": 1, "states": ["due"], "stale": True}]}
    service.api = _api(study_action=lambda payload: {"ok": True}, study_states=lambda payload: stale)
    value = {"action": "review", "wordId": 4, "readingIndex": 1, "grade": "good", "attemptId": "a" * 32}
    assert "row" not in service.media.study_action(value)

    def broken(payload):
        raise RuntimeError("Jiten is down")

    service.api = _api(study_action=lambda payload: {"ok": True}, study_states=broken)
    result = service.media.study_action(value)
    assert result["ok"] is True and "row" not in result
    # A failed grade asks nothing.
    service.api = _api(study_action=lambda payload: {"ok": False}, study_states=broken)
    assert "row" not in service.media.study_action(value)


def _ln(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "db.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    service = LightNovelService(config)
    service.save_settings({"jiten_api_key": KEY, "parse_ahead": "current"})
    service._schedule_overview_check = lambda *args: None
    return service


def test_a_state_answered_after_a_grade_is_not_cached_as_fresh(tmp_path):
    service = _ln(tmp_path)
    calls = []

    def request(path, payload):
        calls.append(path)
        if len(calls) == 1:
            # The word is graded while Jiten answers with the state from before.
            service.invalidate_jiten_live_state(7, 0)
            return {"result": [["mature", "due"]], "decks": [[1]]}
        return {"result": [["mature"]], "decks": [[1]]}

    service._jiten_request = request
    first = service.jiten_live_states([(7, 0)])
    assert first[0]["stale"] is True  # not trusted as the word's state now
    second = service.jiten_live_states([(7, 0)])
    assert len(calls) == 2, "the pre-review answer was cached and Jiten was not asked again"
    assert second[0]["states"] == ["mature"] and second[0]["stale"] is False
    # Kept in SQLite too: a new launch reads the graded state, not the one from before.
    fresh = LightNovelService(service.config)
    fresh._schedule_overview_check = lambda *args: None
    fresh._jiten_request = lambda path, payload: pytest.fail("asked Jiten again")
    assert fresh.jiten_live_states([(7, 0)])[0]["states"] == ["mature"]


def test_a_single_state_answered_after_a_grade_is_not_cached(tmp_path):
    service = _ln(tmp_path)
    calls = []

    def get(path, *args, **kwargs):
        calls.append(path)
        if len(calls) == 1:
            service.invalidate_jiten_live_state(7, 0)
            return ["mature", "due"]
        return ["mature"]

    service._jiten_get = get
    assert service.jiten_live_state(7, 0)["stale"] is True
    assert service.jiten_live_state(7, 0)["states"] == ["mature"]
    assert len(calls) == 2
