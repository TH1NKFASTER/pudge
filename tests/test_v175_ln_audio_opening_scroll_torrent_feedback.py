"""Listen together v1.75: the first sentence of a chapter is not skipped, a spoken chapter
title does not advance the text, following the audio stays cheap (behaviour; audit 71Б rewrite).

Covered elsewhere: blurred pictures stay blurred and visible while scrolling,
test_v141_ln_blur_scroll_open_alignment_perf.py::
test_blurred_illustration_is_the_eager_image_itself_clipped_by_its_frame; the
torrent switch turns green only after the backend confirmed it, is disabled meanwhile and gives
up after 3 s, test_v195_torrent_authoritative_state.py and test_v197_torrent_http_bridge.py."""

from __future__ import annotations

import pytest

from pudge.reading_audio_alignment import _recover_leading_prefix_clock
from t71g19_support import LN_PAIRED, OPEN
from t71g22_support import COSTS, USER_SCROLL


def _first_chapter_story() -> str:
    return (
        "小高い丘が延々と続く。岩ばかりが目立ち、草も木も少ない。"
        "道は丘と丘の間を縫って作られているために、狭いところでは荷馬車が一台通ればふさがってしまう。"
    ) * 6


def _second_chapter_story() -> str:
    return (
        "緩やかに下る坂も終わり、しばらくは土地の起伏といえば申し訳程度の小さい丘、という実に進みやすい道だった。"
        "昨晩の酒の余韻がまだ抜けきらないロレンスには、ちょうどよい道といえた。"
    ) * 6


def test_first_real_sentence_is_reconstructed_instead_of_treated_as_technical_prefix() -> None:
    story = _first_chapter_story()
    # Whisper missed the first sentence and first matched the prose at 岩..., while
    # the old clock encoded an impossible same-time 0 -> 10 jump.
    segments = [
        {"start": 35.84, "end": 38.20, "text": story[10:34]},
        {"start": 38.40, "end": 41.10, "text": story[34:62]},
        {"start": 41.30, "end": 44.20, "text": story[62:92]},
    ]
    clock = [
        {"offset": 0, "time": 35.84},
        {"offset": 10, "time": 35.84},
        {"offset": 34, "time": 38.20},
        {"offset": 62, "time": 41.10},
        {"offset": 92, "time": 44.20},
    ]

    repaired, story_start, debug = _recover_leading_prefix_clock(
        story,
        segments,
        clock,
        chapter_title="第一幕",
        chapter_index=0,
        force_verify=True,
        search_start=20.0,
        search_end=70.0,
    )

    assert debug.get("skipped_ln_prefix") is not True
    assert debug.get("reconstructed_prose_prefix") is True
    assert story_start is not None and story_start < 35.84
    assert repaired[0]["offset"] == 0
    assert float(repaired[0]["time"]) < 35.84
    # There must be no instantaneous jump that skips the first sentence.
    same_time = [row for row in repaired if abs(float(row["time"]) - 35.84) < 0.0005]
    assert len(same_time) <= 1
    assert float(same_time[0]["offset"]) == pytest.approx(10.0, abs=0.2)


def test_short_title_burst_plateau_becomes_zero_width_until_real_prose() -> None:
    story = _second_chapter_story()
    # Current v174 shape: chapter-title audio incorrectly borrowed the first 11
    # prose chars, sat flat during the pause, and only later found a semantic
    # prose chain around offset 35.
    segments = [
        {"start": 6865.06, "end": 6867.20, "text": story[35:58]},
        {"start": 6867.42, "end": 6869.80, "text": story[58:84]},
        {"start": 6870.00, "end": 6872.70, "text": story[84:112]},
    ]
    clock = [
        {"offset": 0, "time": 6852.68},
        {"offset": 11, "time": 6853.62},
        {"offset": 11, "time": 6856.199},
        {"offset": 23, "time": 6861.60},
        {"offset": 35, "time": 6865.06},
        {"offset": 58, "time": 6867.20},
        {"offset": 84, "time": 6869.80},
    ]

    repaired, story_start, debug = _recover_leading_prefix_clock(
        story,
        segments,
        clock,
        chapter_title="第二幕",
        chapter_index=1,
        force_verify=True,
        search_start=6845.0,
        search_end=6880.0,
    )

    assert debug.get("skipped_ln_prefix") is not True
    assert debug.get("marker_false_advance_chars") == pytest.approx(11.0, abs=0.2)
    assert (
        debug.get("inferred_marker_plateau") is True or debug.get("marker_source") == "acoustic+prefix-rebase"
    )
    assert story_start == pytest.approx(6856.20, abs=0.08)
    assert all(float(row["offset"]) == 0.0 for row in repaired if float(row["time"]) < story_start - 0.01)
    # The later semantic chain is retained, so the repair naturally converges.
    assert any(
        abs(float(row["time"]) - 6865.06) < 0.05 and 34 <= float(row["offset"]) <= 36 for row in repaired
    )


def test_following_the_audio_reuses_its_word_index_and_reads_layout_rarely(page) -> None:
    # Following the narration paints ~30 frames a second. It finds the spoken word in an
    # index built once per chapter (searching the chapter's DOM each frame was slow in long
    # chapters) and checks whether the word is still in view at most every 250 ms (that
    # check forces a WebKit layout); right after the user scrolled it does not check at all.
    result = page.run(
        OPEN
        + USER_SCROLL
        + COSTS
        + r"""
        await __tick(500);
        const playing = await measure(1000);
        wheel();
        const afterWheel = await measure(1500);
        await __tick(600);
        return {playing, afterWheel, following: await measure(1000)};
        """,
        before=LN_PAIRED,
    )
    out = result.value
    assert out["playing"]["chapterQueries"] == 0 and 2 <= out["playing"]["viewport"] <= 4
    assert out["afterWheel"] == {"viewport": 0, "chapterQueries": 0}
    assert out["following"]["chapterQueries"] == 0 and 2 <= out["following"]["viewport"] <= 4
