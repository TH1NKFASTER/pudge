"""Jiten keeps a card's custom sentence only with the word marked as **word** and within 150
characters (it answered "HTTP 400" before). The window sends the word as written in the sentence."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pudge.light_novels import LightNovelError, LightNovelService, jiten_marked_sentence
from tests.test_fix_vn_card_picture import ADD, PAGE_API


def test_the_word_is_marked_and_the_sentence_cut_around_it() -> None:
    assert (
        jiten_marked_sentence("明日から袖を通すはずだったそれを眺め、", "眺め")
        == "明日から袖を通すはずだったそれを**眺め**、"
    )
    long = "前" * 200 + "猫" + "後" * 200
    marked = jiten_marked_sentence(long, "猫")
    assert len(marked) == 150 and marked == "前" * 72 + "**猫**" + "後" * 73
    near_start = jiten_marked_sentence("猫" + "後" * 300, "猫")
    assert near_start == "**猫**" + "後" * 145
    assert jiten_marked_sentence("  猫が\n好き  ", "猫") == "**猫**が 好き"
    # Without the word in the sentence Jiten would refuse it: nothing is sent.
    assert jiten_marked_sentence("犬が好き", "猫") == ""
    assert jiten_marked_sentence("犬が好き", "") == ""
    assert jiten_marked_sentence("", "猫") == ""


def test_no_custom_sentence_is_sent_when_the_word_is_not_in_it() -> None:
    service = LightNovelService.__new__(LightNovelService)
    calls: list[tuple] = []
    service._jiten_mutation = lambda path, body: calls.append(("add", path)) or {}
    service._jiten_add_custom_sentence = lambda w, r, sentence: calls.append(("sentence", sentence))
    service.invalidate_jiten_live_state = lambda w, r: None
    result = service.study_action(
        "jiten", "add", 5, 0, deck_id="12", sentence="犬が好き", media_source="vn", surface="猫"
    )
    assert result.get("enrichment_warnings", []) == [] and [call[0] for call in calls] == ["add"]


def test_jiten_error_text_is_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    from pudge import light_novels as ln_module

    service = LightNovelService.__new__(LightNovelService)
    service.settings = lambda: SimpleNamespace(jiten_api_key="key")
    answer = SimpleNamespace(
        status_code=400,
        content=b'"Text must contain at least one **word** marker."',
        text='"Text must contain at least one **word** marker."',
        json=lambda: "Text must contain at least one **word** marker.",
    )
    monkeypatch.setattr(ln_module.httpx, "post", lambda *a, **k: answer)
    with pytest.raises(LightNovelError, match="word\\*\\* marker"):
        service._jiten_add_custom_sentence(1, 0, "**猫**")


def test_the_card_sends_the_word_as_written(page) -> None:
    result = page.run(
        "setPage('visualnovels'); await __tick(100);" + ADD.replace("#ROOT", "#vnCurrent"),
        before=PAGE_API
        + "__api.visual_novel_state = async () => ({running: true, status: 'reading', capture_count: 1, ocr_count: 1,"
        " transcript: [{id: 1, text: '二階堂ヒロ', frame_id: 3}], current_text: '二階堂ヒロ', current_frame_id: 3});",
    )
    [[added]] = result.calls_of("study_action")
    assert added["surface"] == "二階堂"
