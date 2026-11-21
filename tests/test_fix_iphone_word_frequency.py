"""The phone's word card shows Jiten's frequency rank like the Mac's popup ("Frequency #1234").

The Mac sent `frequencyRank` only from the card's top level; Jiten's vocabulary info may keep
it on the word's reading (mainReading / alternativeReadings) instead, so both are read. The phone formats and places it (WordFrequency in PudgeCore, WordColorsTests.swift)."""

from __future__ import annotations

from types import SimpleNamespace

from pudge.iphone_media import IPhoneMedia, frequency_rank


def _view(detail: dict, reading: int = 0) -> dict:
    return IPhoneMedia._word_view(7, reading, detail, {}, "猫")


def test_the_rank_comes_from_the_card_or_its_reading():
    info = {
        "wordId": 7,
        "mainReading": {"text": "猫[ねこ]", "readingIndex": 0, "frequencyRank": 1234},
        "alternativeReadings": [{"text": "ネコ", "readingIndex": 1, "frequencyRank": 25000}],
    }
    assert _view(info)["frequencyRank"] == 1234
    assert _view(info, reading=1)["frequencyRank"] == 25000
    # Another reading's rank is never taken: a reading without its own has none.
    assert _view(info, reading=2)["frequencyRank"] is None
    # A parsed word's own rank comes first.
    assert _view({**info, "frequencyRank": 300})["frequencyRank"] == 300
    # The word cache keeps readings as rows (mainReading first).
    assert frequency_rank({"readings": [{"readingIndex": 3, "frequencyRank": "4567"}]}, 3) == 4567


def test_an_unknown_rank_is_left_out():
    for detail in ({}, {"frequencyRank": 0}, {"mainReading": {"readingIndex": 0}}, {"frequencyRank": "x"}):
        assert _view(detail)["frequencyRank"] is None
    assert frequency_rank({"mainReading": {"frequencyRank": True}}, 0) is None


def test_looked_up_and_offline_words_carry_it(tmp_path):
    info = {"wordId": 7, "mainReading": {"text": "猫", "readingIndex": 0, "frequencyRank": 1234}}
    api = SimpleNamespace(
        study_word=lambda card: dict(info),
        study_state=lambda card: {},
        study_words=lambda cards: [{**info, "dictionary_complete": True} for _ in cards],
    )
    media = IPhoneMedia(SimpleNamespace(api=api, root=tmp_path))
    assert media.study_word({"wordId": 7, "readingIndex": 0})["frequencyRank"] == 1234
    assert media.study_words({"pairs": [[7, 0]]})["items"][0]["frequencyRank"] == 1234


def test_jitens_real_word_shape_without_a_rank_has_none():
    # The shape of tests/test_v27_ui_and_workers.py's powder_dto (vocabulary info, no rank).
    dto = {
        "wordId": 1,
        "mainReading": {"text": "粉[こな]", "readingIndex": 0, "readingType": 0},
        "alternativeReadings": [{"text": "こな", "readingIndex": 1, "readingType": 1}],
    }
    assert _view(dto)["frequencyRank"] is None
