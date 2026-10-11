"""Listen together v1.76: the real opening of a chapter is never skipped, a spoken chapter
title is a zero-width plateau, scrolling stays cheap (behaviour; audit 71Б rewrite).

Covered elsewhere: following the audio finds the spoken word in a per-chapter index and
probes layout at most every 250 ms, test_v175_ln_audio_opening_scroll_torrent_feedback.py::
test_following_the_audio_reuses_its_word_index_and_reads_layout_rarely; while the user
scrolls the highlight is frozen (no frames, no furigana preview) and catches up afterwards,
test_v178_ln_audio_state_clock_torrent.py::test_manual_scroll_freezes_the_highlight_and_catches_up_after;
a previous word's reading lingers even where it overlaps the next one,
test_v131_furigana_search_images_stt.py::test_previous_reading_lingers_briefly_even_where_it_overlaps_the_next.
Dropped: the removed DOM mora-weight helpers and the alignment cache revision string (no
behaviour of their own)."""

from __future__ import annotations

import pytest

from pudge.audiobooks import AudiobookService
from pudge.reading_audio_alignment import _recover_leading_prefix_clock
from t71g18_support import NO_LN_GATE, ln_book
from t71g22_support import USER_SCROLL


def test_unpunctuated_real_opening_prefix_is_never_silently_skipped() -> None:
    story = (
        "小高い丘が延々と続き岩ばかりが目立ち草も木も少ない道を荷馬車が進んでいた。"
        "旅人は遠くの町を目指してゆっくりと坂を下っていった。"
    ) * 8
    # The first trustworthy STT phrase begins at offset 10.  There is no strong
    # punctuation boundary in the omitted prefix, which is the real trace shape
    # that made v175 keep skipped_ln_prefix=true.
    segments = [
        {"start": 35.84, "end": 38.20, "text": story[10:34]},
        {"start": 38.45, "end": 41.10, "text": story[34:62]},
        {"start": 41.35, "end": 44.20, "text": story[62:92]},
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
    assert float(repaired[0]["offset"]) == 0.0
    assert float(repaired[0]["time"]) < 35.84
    at_first_match = [row for row in repaired if abs(float(row["time"]) - 35.84) < 0.0005]
    assert len(at_first_match) <= 1
    assert float(at_first_match[0]["offset"]) == pytest.approx(10.0, abs=0.2)


def test_character_level_spoken_chapter_title_plateau_is_zero_width() -> None:
    story = (
        "緩やかに下る坂も終わりしばらくは土地の起伏といえば申し訳程度の小さい丘という実に進みやすい道だった。"
        "昨晩の酒の余韻がまだ抜けきらないロレンスにはちょうどよい道といえた。"
    ) * 8
    # Real clocks can split the false 第X幕 advance into one-character anchors.
    # v175 inspected only the first seven anchors and therefore never reached
    # the ~11-char plateau.
    clock = [{"offset": 0, "time": 6852.68}]
    for offset in range(1, 12):
        clock.append({"offset": offset, "time": 6852.68 + offset * 0.075})
    clock.extend(
        [
            {"offset": 11, "time": 6856.199},
            {"offset": 23, "time": 6861.60},
            {"offset": 35, "time": 6865.06},
            {"offset": 58, "time": 6867.20},
            {"offset": 84, "time": 6869.80},
        ]
    )
    segments = [
        {"start": 6865.06, "end": 6867.20, "text": story[35:58]},
        {"start": 6867.42, "end": 6869.80, "text": story[58:84]},
        {"start": 6870.00, "end": 6872.70, "text": story[84:112]},
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
    assert debug.get("inferred_marker_plateau") is True
    assert debug.get("marker_false_advance_chars") == pytest.approx(11.0, abs=0.5)
    assert story_start == pytest.approx(6856.199, abs=0.08)
    assert all(float(row["offset"]) == 0.0 for row in repaired if float(row["time"]) < story_start - 0.01)
    assert any(
        abs(float(row["time"]) - 6865.06) < 0.05 and 34 <= float(row["offset"]) <= 36 for row in repaired
    )


def test_reconstructed_nonzero_title_prefix_gets_precision_pass() -> None:
    row = {
        "title": "第一幕",
        "leading_prefix_debug": {
            "recovered": True,
            "degraded": False,
            "reconstructed_prose_prefix": True,
            "original_first_offset": 10,
        },
    }
    assert AudiobookService._alignment_chapter_needs_precision(row) is True


def test_scrolling_keeps_its_timers_and_bookmarks_20_s_after_the_last_scroll(page) -> None:
    # WebKit sends a scroll event per frame. The reader keeps one "scrolling" timer and one
    # auto-bookmark timer for the whole gesture (it used to clear and re-create both on
    # every event); "scrolling" ends ~150 ms after the last event and the position is
    # bookmarked once, 20 s after it.
    result = page.run(
        USER_SCROLL
        + r"""
        await __settle(openLightNovel(6)); await __tick(1000);
        const bookmarks = () => __apiCalls.filter(c => c.name === 'light_novel_bookmark').map(c => c.args);
        wheel(); scroller.dispatchEvent(new Event('scroll'));
        let timers = 0;
        const set = window.setTimeout, clear = window.clearTimeout;
        window.setTimeout = (...args) => { timers++; return set(...args); };
        window.clearTimeout = (...args) => { timers++; return clear(...args); };
        for (let k = 0; k < 50; k++) { scroller.scrollTop += 30; scroller.dispatchEvent(new Event('scroll')); }
        window.setTimeout = set; window.clearTimeout = clear;
        const out = {timers, during: scrolling()};
        await __tick(130); out.at130 = scrolling();
        await __tick(40); out.at170 = scrolling();
        await __tick(19700); out.before20s = bookmarks();
        await __tick(200); out.after20s = bookmarks();
        await __tick(30000); out.later = bookmarks();
        return out;
        """,
        before=ln_book(paragraphs=["猫が好きです。", "犬も好きです。"])
        + "__api.light_novel_bookmark = async (id, chapter, offset) => ({current_chapter: chapter, current_offset: offset});",
        state=NO_LN_GATE,
    )
    out = result.value
    assert out["timers"] == 0
    assert (out["during"], out["at130"], out["at170"]) == (True, True, False)
    assert out["before20s"] == []
    assert [args[0:2] + args[3:] for args in out["after20s"]] == [[6, 0, "auto"]]
    assert out["later"] == out["after20s"]
