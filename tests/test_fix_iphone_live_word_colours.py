"""The phone colours words like the Mac reader: texts sent to the iPhone carry the live Jiten
states (with deck membership: "not in deck"), due first, within a short time budget."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import pudge.iphone_media as iphone_media
from pudge.config import AppConfig
from pudge.database import Database
from pudge.iphone_media import word_state
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.word_states import WORD_STATES

PARSED = {
    "paragraphs": ["猫犬鳥"],
    # The parse snapshot: no deck membership, states of the parse time.
    "vocabulary": [
        {"wordId": 1, "readingIndex": 0, "knownState": ["new"]},
        {"wordId": 2, "readingIndex": 0, "knownState": ["mature"]},
        {"wordId": 3, "readingIndex": 0, "knownState": ["new"]},
    ],
    "tokens": [
        [
            {"wordId": 1, "readingIndex": 0, "start": 0, "end": 1},
            {"wordId": 2, "readingIndex": 0, "start": 1, "end": 2},
            {"wordId": 3, "readingIndex": 0, "start": 2, "end": 3},
        ]
    ],
}
# Jiten now: 1 is new in no study deck, 2 is mature and due, 3 is new in deck 7.
LIVE = {"result": [[0], [2, 4], [0]], "decks": [[], [5], [7]]}
AS_PARSED = ["new", "mature", "new"]
AS_ON_THE_MAC = ["not_in_deck", "due", "new"]


@pytest.fixture
def reader(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "pudge.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    service = LightNovelService(config)
    service.save_settings({"jiten_api_key": "key", "word_mark_style": "color"})
    return service


@pytest.fixture
def phone(tmp_path: Path, reader: LightNovelService):
    from pudge.iphone_service import IPhoneService

    service = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")

    def chapter(book: int, index: int) -> dict:
        # What the Mac reader gets on opening the chapter (states from the parse/stale cache).
        tokens, vocabulary = reader._provider_scoped_study_payload(PARSED, "jiten")
        return {
            "chapter_index": index,
            "title": "One",
            "parsing": False,
            "paragraphs": PARSED["paragraphs"],
            "tokens": tokens,
            "vocabulary": vocabulary,
        }

    def parse_text(text: str) -> dict:
        tokens, vocabulary = reader._provider_scoped_study_payload(PARSED, "jiten")
        return {"paragraphs": PARSED["paragraphs"], "tokens": tokens, "vocabulary": vocabulary}

    service.api = SimpleNamespace(
        light_novels=reader, light_novel_chapter=chapter, study_parse_text=parse_text
    )
    return service


def _requests(reader: LightNovelService, monkeypatch, answer) -> list:
    calls: list = []

    def request(action, payload=None):
        calls.append((action, payload))
        return answer(action, payload) if callable(answer) else answer

    monkeypatch.setattr(reader, "_jiten_request", request)
    return calls


def _chapter_states(phone) -> list[str]:
    view = phone.media.handle("GET", "/api/v2/media/ln/3/chapters/0", {}, dict)
    return [WORD_STATES[((token[5] >> 7) & 15) - 1] for token in view["paragraphs"][0]["tokens"]]


def test_chapter_for_the_phone_carries_the_live_states_with_deck_membership(phone, reader, monkeypatch):
    calls = _requests(reader, monkeypatch, LIVE)
    view = phone.media.handle("GET", "/api/v2/media/ln/3/chapters/0", {}, dict)
    tokens = view["paragraphs"][0]["tokens"]
    # Bits 7-10: the Mac reader's word state; the due word wins over mature (knowledge code 2).
    assert [WORD_STATES[((token[5] >> 7) & 15) - 1] for token in tokens] == AS_ON_THE_MAC
    assert [token[4] for token in tokens] == [0, 2, 0]
    assert calls == [("reader/lookup-vocabulary", {"words": [[1, 0], [2, 0], [3, 0]]})]
    # Opened again within the cache time: no new question to Jiten.
    assert _chapter_states(phone) == AS_ON_THE_MAC and len(calls) == 1


def test_parsed_text_and_subtitle_lines_carry_the_live_states(phone, reader, monkeypatch):
    _requests(reader, monkeypatch, LIVE)
    [paragraph] = phone.media.study_parse({"text": "猫犬鳥"})["paragraphs"]
    assert [WORD_STATES[((t[5] >> 7) & 15) - 1] for t in paragraph["tokens"]] == AS_ON_THE_MAC
    monkeypatch.setattr(reader, "parse_study_text", lambda text: phone.api.study_parse_text(text))
    [line] = phone.media.parse_lines(["猫犬鳥"])
    assert [WORD_STATES[((t[5] >> 7) & 15) - 1] for t in line["tokens"]] == AS_ON_THE_MAC


def test_a_slow_jiten_never_holds_the_phone_and_its_late_answer_is_kept(phone, reader, monkeypatch):
    monkeypatch.setattr(iphone_media, "_LIVE_STATE_SECONDS", 0.3)
    release = threading.Event()

    def slow(action, payload):
        release.wait(10)
        return LIVE

    calls = _requests(reader, monkeypatch, slow)
    started = time.monotonic()
    assert _chapter_states(phone) == AS_PARSED  # the states it was parsed with
    assert time.monotonic() - started < 2
    release.set()
    deadline = time.monotonic() + 5
    while reader._cached_jiten_live_state(3, 0) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    # The answer that came late fills the Mac's cache: the next open has it at once.
    assert _chapter_states(phone) == AS_ON_THE_MAC and len(calls) == 1


def test_jiten_unavailable_keeps_the_parsed_states(phone, reader, monkeypatch):
    def down(action, payload):
        raise LightNovelError("Jiten is unavailable")

    _requests(reader, monkeypatch, down)
    assert _chapter_states(phone) == AS_PARSED


def test_states_of_a_jiten_account_switched_meanwhile_are_not_used(phone, reader, monkeypatch):
    def switch(action, payload):
        reader.save_settings({"jiten_api_key": "other"})
        return LIVE

    _requests(reader, monkeypatch, switch)
    assert _chapter_states(phone) == AS_PARSED


def test_jiten_is_not_asked_with_jpdb_as_the_study_provider(phone, reader, monkeypatch):
    calls = _requests(reader, monkeypatch, LIVE)
    reader.save_settings({"study_backend": "jpdb"})
    _chapter_states(phone)
    assert calls == []


CARDS = [
    {"normalizedState": "not_in_deck", "states": ["new"]},
    {"normalizedState": "due", "states": ["mature", "due"]},
    {"states": ["mature", "due"]},
    {"states": ["blacklisted", "due"]},
    {"states": ["new"], "studyDeckIds": []},
    {"states": ["new"], "studyDeckIds": [3]},
    {"states": ["new"]},
    # An older cache's bucket names: the raw states decide, else the name.
    {"normalizedState": "learning", "states": ["mature"]},
    {"normalizedState": "known"},
    {"normalizedState": "learning"},
    {"cardState": ["young"], "states": ["mature"]},
]


def test_phone_word_state_is_the_mac_readers_choice(page):
    mac = page.run(f"return {json.dumps(CARDS)}.map(card => PudgeReadingTools.study.normalizeState(card));")
    assert [word_state(card) for card in CARDS] == mac.value
