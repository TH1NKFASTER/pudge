"""Paragraphs Jiten could not parse are asked about again now and then; a chapter whose
paragraph parses now is parsed again the next time it is opened.

User (a Re:Zero light novel, one long line of dialogue): Jiten returned 7 words for a 63-letter
sentence, the same on a fresh request, so the reader showed it plain. The test sentences are
invented. "Keep the texts Jiten did
not parse and check now and then (rarely, not to spend Jiten's API) whether they changed; if
they improved, lazily invalidate the old results and parse again."
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from pudge import light_novels as ln_module
from pudge.light_novels import LightNovelService
from tests.test_v0661_light_novels import cfg

GOOD = "猫が好きです。"
BROKEN = "「ねえ、あの古い時計台のことなんだけど、カイって昔からあそこに登っていたんでしょう？ 鐘の音とか覚えているんじゃない？」"
SHORT_CRY = "「わあああああああ──ッ!!」"
DAY = 86400.0


def _wait() -> None:
    for thread in list(threading.enumerate()):
        if thread.name.startswith("ln-jiten") and thread is not threading.current_thread():
            thread.join(10)


@pytest.fixture
def reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(ln_module.time, "time", lambda: clock["now"])
    service = LightNovelService(cfg(tmp_path))
    service.save_settings({"jiten_api_key": "token", "parse_ahead": "current"})
    monkeypatch.setattr(service, "_reserve_parse_request_start", lambda: None)
    txt = tmp_path / "novel.txt"
    txt.write_text(f"{GOOD}\n{BROKEN}\n{SHORT_CRY}\n", encoding="utf-8")
    book = service.import_file(txt)
    jiten = {"fixed": False, "requests": []}

    def fake(action, payload=None):
        assert action == "reader/parse"
        jiten["requests"].append(list(payload["text"]))
        tokens = []
        for text in payload["text"]:
            if text == BROKEN and not jiten["fixed"]:
                tokens.append([{"wordId": 1, "readingIndex": 0, "start": 1, "end": 3, "length": 2}])
            elif text == SHORT_CRY:
                tokens.append([])
            else:
                tokens.append(
                    [{"wordId": 2, "readingIndex": 0, "start": 0, "end": len(text), "length": len(text)}]
                )
        return {"tokens": tokens, "vocabulary": [], "parseVersion": 3 if jiten["fixed"] else 2}

    monkeypatch.setattr(service, "_jiten_request", fake)
    return service, book["id"], jiten, clock


def _suspects(service) -> list[tuple[str, float]]:
    with service._connection() as conn:
        return [
            (str(row["text"]), float(row["coverage"]))
            for row in conn.execute("SELECT text,coverage FROM ln_parse_suspects")
        ]


def _broken_tokens(payload) -> list:
    index = payload["paragraphs"].index(BROKEN)
    return payload["tokens"][index]


def test_a_paragraph_jiten_failed_is_kept_and_rechecked_weekly_at_most(reader) -> None:
    service, book_id, jiten, clock = reader
    service.chapter(book_id, 0)
    _wait()
    [(text, coverage)] = _suspects(service)  # not the short cry, not the good line
    assert text == BROKEN and coverage < 0.2
    assert len(jiten["requests"]) == 1

    # Opening the chapter again within the week asks Jiten nothing.
    clock["now"] += 6 * DAY
    service.chapter(book_id, 0)
    _wait()
    assert len(jiten["requests"]) == 1

    # A week later: one request with only the suspect; Jiten still fails, nothing changes.
    clock["now"] += 1.5 * DAY
    service.chapter(book_id, 0)
    _wait()
    assert jiten["requests"][1:] == [[BROKEN]]
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ln_parse_stale").fetchone()[0] == 0
    # The same day again: no second request.
    service._suspect_probe_not_before = 0.0
    assert service._maybe_probe_parse_suspects() is False


def test_once_jiten_parses_it_the_chapter_is_parsed_again_on_the_next_opening(reader) -> None:
    service, book_id, jiten, clock = reader
    first = service.chapter(book_id, 0)
    _wait()
    assert len(_broken_tokens(first)) == 1

    jiten["fixed"] = True
    clock["now"] += 8 * DAY
    service.chapter(book_id, 0)  # starts the weekly check: improved -> chapter marked stale
    _wait()
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ln_parse_stale").fetchone()[0] == 1

    # Lazily: this opening still shows the old parse and re-parses the chapter behind it.
    shown = service.chapter_fast(book_id, 0)
    assert len(_broken_tokens(shown)) == 1
    _wait()
    after = service.chapter_fast(book_id, 0)
    assert _broken_tokens(after)[0]["end"] == len(BROKEN)
    assert _suspects(service) == []
    with service._connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ln_parse_stale").fetchone()[0] == 0
    # probe + whole chapter once each, nothing more
    assert len(jiten["requests"]) == 3 and jiten["requests"][1] == [BROKEN]


def test_study_texts_are_not_tracked(reader) -> None:
    service, _book_id, jiten, _clock = reader
    service.parse_study_text(BROKEN)
    assert _suspects(service) == []
    assert len(jiten["requests"]) == 1


def test_a_chapter_parsed_before_this_is_looked_at_when_opened(reader) -> None:
    service, book_id, jiten, _clock = reader
    service.chapter(book_id, 0)
    _wait()
    with service._connection() as conn:
        conn.execute("DELETE FROM ln_parse_suspects")  # as before suspects were kept
    fresh_service = LightNovelService(service.config)
    fresh_service._jiten_request = lambda *_a, **_k: pytest.fail("the cached parse is enough")
    fresh_service.chapter_fast(book_id, 0)
    assert [text for text, _coverage in _suspects(fresh_service)] == [BROKEN]
