"""iPhone without the Mac: dictionary entries of downloaded subtitles are sent in advance.

The phone keeps them so a tapped word still shows its meanings offline. The Mac answers
from its word cache at once and fetches only a few missing words in the background, so a
large batch never holds up the Mac reader's own lookups.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from pudge.database import Database
from pudge.jiten_words import JitenWordRepository


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _api(**values):
    defaults = dict(
        light_novels=SimpleNamespace(
            settings=lambda: SimpleNamespace(study_backend="jiten", review_mode="binary")
        )
    )
    return SimpleNamespace(**{**defaults, **values})


def test_many_words_come_with_meanings_and_only_complete_entries(service):
    asked = []

    def study_words(cards):
        asked.append(cards)
        return [
            {
                "wordId": 5,
                "readingIndex": 0,
                "spelling": "猫",
                "reading": "猫[ねこ]",
                "meanings": [["cat", "feline"]],
                "readings": [{"reading": "ねこ"}],
                "frequencyRank": 300,
                "dictionary_complete": True,
            },
            {"wordId": 6, "readingIndex": 1},  # not cached yet: comes on a later request
            {"wordId": 7, "readingIndex": 0, "dictionary_complete": True, "not_found": True},
        ]

    service.api = _api(study_words=study_words)
    answer = service.media.handle(
        "POST",
        "/api/v2/media/study/words",
        {},
        lambda: {"pairs": [[5, 0], [6, 1], ["x"], [7, 0]]},
    )
    assert asked == [
        [{"wordId": 5, "readingIndex": 0}, {"wordId": 6, "readingIndex": 1}, {"wordId": 7, "readingIndex": 0}]
    ]
    first, missing = answer["items"]
    assert first == {
        "wordId": 5,
        "readingIndex": 0,
        "spelling": "猫",
        "reading": "猫",
        "readings": ["ねこ"],
        "meanings": ["cat", "feline"],
        "state": 0,
        "stateLabel": "",
        "frequencyRank": 300,
        "inDeck": None,
        "complete": True,
    }
    assert missing["wordId"] == 7 and missing["meanings"] == [] and missing["complete"] is True


def test_one_request_carries_at_most_200_words(service):
    seen = []
    service.api = _api(study_words=lambda cards: seen.extend(cards) or [])
    assert service.media.study_words({"pairs": [[n, 0] for n in range(1, 400)]}) == {"items": []}
    assert len(seen) == 200
    assert service.media.study_words({"pairs": []}) == {"items": []}
    assert len(seen) == 200


def test_single_word_lookup_keeps_its_live_state(service):
    service.api = _api(
        study_word=lambda card: {"spelling": "犬", "meanings": ["dog"], "dictionary_complete": True},
        study_state=lambda payload: {"states": ["young"], "studyDeckIds": [3], "rawStateLabel": "Young"},
    )
    word = service.media.study_word({"wordId": 9, "readingIndex": 0, "surface": "いぬ"})
    assert word["state"] == 1 and word["stateLabel"] == "Young" and word["inDeck"] is True
    assert word["spelling"] == "犬" and word["meanings"] == ["dog"]


def _light_novels(tmp_path, fetch):
    from pudge.light_novels import LightNovelService

    repository = JitenWordRepository(tmp_path / "words.sqlite")
    novels = LightNovelService.__new__(LightNovelService)
    novels.study_provider_capabilities = lambda backend: {"account_key": "account"}
    novels._word_repository = lambda: repository
    novels._jiten_get = fetch
    return novels, repository


def test_cached_words_answer_at_once_and_only_a_few_missing_ones_are_fetched(tmp_path):
    calls = []

    def fetch(route):
        calls.append(route)
        return {"spelling": "語", "readings": [{"reading": "ご"}], "meanings": ["word"]}

    novels, repository = _light_novels(tmp_path, fetch)
    repository.put(
        "account",
        {"wordId": 1, "readingIndex": 0, "spelling": "猫", "meanings": ["cat"], "dictionary_complete": True},
    )
    cards = [{"wordId": n, "readingIndex": 0} for n in range(1, 6)]
    result = novels.jiten_words_cached(cards, fetch_limit=2)
    repository.pool.shutdown(wait=True)
    assert result[0]["meanings"] == ["cat"] and result[0]["dictionary_complete"]
    assert [card["wordId"] for card in result] == [1, 2, 3, 4, 5]
    assert not any(card.get("dictionary_complete") for card in result[1:])
    # The cached word is not fetched again; of the four missing words only two are fetched.
    assert sorted(calls) == ["vocabulary/2/0/info", "vocabulary/3/0/info"]
    reopened = JitenWordRepository(repository.path)
    try:
        assert reopened.get("account", {"wordId": 2, "readingIndex": 0})[0]["dictionary_complete"]
        assert reopened.get("account", {"wordId": 4, "readingIndex": 0}) == (None, 0)
    finally:
        reopened.pool.shutdown()


def test_mac_api_answers_many_words_from_the_cache(tmp_path):
    from pudge.web_app import WebAppApi

    novels, repository = _light_novels(tmp_path, lambda route: None)
    repository.put(
        "account",
        {"wordId": 1, "readingIndex": 0, "spelling": "猫", "meanings": ["cat"], "dictionary_complete": True},
    )
    api = SimpleNamespace(light_novels=novels, logger=logging.getLogger("test"))
    try:
        cards = WebAppApi.study_words(api, [{"wordId": 1, "readingIndex": 0}])
    finally:
        repository.pool.shutdown()
    assert cards[0]["meanings"] == ["cat"] and cards[0]["dictionary_complete"]
