"""iPhone/iPad word colours: every Jiten state the Mac sends decodes to the state the Mac means
and takes the Mac's own colour (the phone changes none of them).

The Mac's side is checked here: tests/fixtures/iphone_word_states.json is what it sends now
(tokens of every state, the reader's colours, the JitenMPV themes' word styles). The phone
decodes the same file in ios/PudgeCore/Tests/PudgeCoreTests/WordColorsTests.swift (`swift test`
on the Mac): the state names, the reader colour and the JitenMPV style of each."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pudge.iphone_media import (
    JITEN_STATES,
    JITEN_THEMES,
    IPhoneMedia,
    jiten_state,
    paragraph_view,
    state_code,
    subtitle_look,
    word_state,
)

FIXTURE = Path(__file__).with_name("fixtures") / "iphone_word_states.json"
CARDS = {
    "new": {"cardState": ["new"]},
    "not_in_deck": {"cardState": ["new"], "studyDeckIds": []},
    "young": {"cardState": ["young"]},
    "learning": {"cardState": ["learning"]},
    "mature": {"cardState": ["mature"]},
    "known": {"cardState": ["known"]},
    "mastered": {"cardState": ["mastered"]},
    "never-forget": {"cardState": ["never-forget"]},
    "never_forget": {"cardState": ["never_forget"]},
    "named neverforget": {"normalizedState": "neverforget"},
    "named not-in-deck": {"normalizedState": "not-in-deck"},
    "due": {"cardState": ["due"]},
    "failed": {"cardState": ["failed"]},
    "blacklisted": {"cardState": ["blacklisted"]},
    "suspended": {"cardState": ["suspended"]},
    "redundant": {"cardState": ["redundant"]},
    "mature and due": {"cardState": ["mature", "due"]},
    "mature and blacklisted": {"cardState": ["mature", "blacklisted"]},
    "mastered and due": {"cardState": ["mastered", "due"]},
    "blacklisted and due": {"cardState": ["blacklisted", "due"]},
    "redundant and due": {"cardState": ["redundant", "due"]},
    "suspended and due": {"cardState": ["suspended", "due"]},
    "named mature": {"normalizedState": "mature"},
    "named known": {"normalizedState": "known"},
    "named blacklisted": {"normalizedState": "blacklisted"},
    "no card": None,
}


def _table(tmp_path: Path) -> dict:
    cases = []
    for name, card in CARDS.items():
        token = {"start": 0, "end": 1, "wordId": 7, "readingIndex": 0}
        if card is not None:
            token["card"] = card
        view = paragraph_view("猫", [token], [], None)
        cases.append(
            {
                "name": name,
                "token": view["tokens"][0],
                "state": state_code(card),
                "wordState": word_state(card) or None,
                "jitenState": JITEN_STATES[jiten_state(card)],
            }
        )
    media = IPhoneMedia(SimpleNamespace(api=None, root=tmp_path))
    return {
        "_comment": "What the Mac sends the phone for each word state (pudge/iphone_media.py); "
        "tests/test_fix_iphone_word_state_colours.py keeps it equal to the Mac's output, "
        "WordColorsTests.swift decodes it on the phone.",
        "cases": cases,
        "readerColors": media.reader_settings()["colors"],
        "jitenStates": list(JITEN_STATES),
        "themes": {
            name: [{"text": row["text"], "opacity": row.get("opacity", 255)} for row in rows]
            for name, rows in JITEN_THEMES.items()
        },
        "defaultLook": [
            {"text": row["text"], "opacity": row.get("opacity", 255)}
            for row in subtitle_look(tmp_path / "missing.json")["states"]
        ],
    }


def test_the_phone_gets_the_states_and_colours_its_tests_decode(tmp_path):
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == _table(tmp_path)


def test_each_state_is_the_one_the_mac_reader_shows(tmp_path):
    cases = {case["name"]: case for case in _table(tmp_path)["cases"]}
    # The reader's state and the JitenMPV look agree (a "known" word was Mastered in the look,
    # dimmed by its themes, but mature and green on the Mac).
    for name in ("mature", "known", "named mature", "named known"):
        assert cases[name]["wordState"] == "mature" and cases[name]["jitenState"] == "mature", name
    for name in ("blacklisted", "mature and blacklisted", "named blacklisted"):
        assert cases[name]["wordState"] == "blacklisted" and cases[name]["jitenState"] == "blacklisted", name
        assert cases[name]["state"] == 4
    assert cases["mature and due"]["wordState"] == "due" and cases["mature and due"]["jitenState"] == "due"
    # Every spelling the Mac reader knows (word_states.STATE_ALIASES) decodes as it does there.
    for name in ("mastered", "never-forget", "never_forget", "named neverforget"):
        assert cases[name]["wordState"] == "mastered" and cases[name]["jitenState"] == "mastered", name
    assert cases["named not-in-deck"]["wordState"] == "not_in_deck"
    assert cases["named not-in-deck"]["jitenState"] == "new"
    for case in cases.values():
        flags = case["token"][5]
        # Bits 3-6: the JitenMPV state + 1; bits 7-10: the reader's state + 1 (0: not known).
        assert JITEN_STATES[((flags >> 3) & 15) - 1] == case["jitenState"]
        assert case["token"][4] == case["state"]
