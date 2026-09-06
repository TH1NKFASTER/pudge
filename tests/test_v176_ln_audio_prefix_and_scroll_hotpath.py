from __future__ import annotations

from pathlib import Path

import pytest

from pudge.audiobooks import AudiobookService
from pudge.reading_audio_alignment import _recover_leading_prefix_clock

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "pudge" / "web" / "index.html"
AUDIOBOOKS = ROOT / "pudge" / "audiobooks.py"


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
    assert any(abs(float(row["time"]) - 6865.06) < 0.05 and 34 <= float(row["offset"]) <= 36 for row in repaired)


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


def test_paired_scroll_hotpath_is_logarithmic_and_throttled() -> None:
    html = INDEX.read_text(encoding="utf-8")
    weight = html.split("function lnPairedWeightMap()", 1)[1].split("function lnPairedSpeechRatio", 1)[0]
    assert "nodes=domIndex.words||[]" in weight
    assert "querySelectorAll('[data-ln-audio-start][data-ln-audio-end].ln-word')" not in weight
    assert "function lnPairedLastSegmentAt(rows,value,key)" in weight
    weighted = weight.split("function lnPairedWeightedPosition", 1)[1].split("function lnPairedSourcePosition", 1)[0]
    source = weight.split("function lnPairedSourcePosition", 1)[1]
    assert "lnPairedLastSegmentAt" in weighted
    assert "for(const segment of map.segments)" not in weighted
    assert "lnPairedLastSegmentAt" in source
    assert "for(const segment of map.segments)" not in source

    interpolation = html.split("function startLnPairedInterpolation", 1)[1].split("async function applyLnPairedPosition", 1)[0]
    assert "minPaintMs=manualScrolling?250:33" in interpolation
    assert "previewOffset=manualScrolling?NaN" in interpolation

    render = html.split("function renderLnPairedPosition", 1)[1].split("function startLnPairedInterpolation", 1)[0]
    assert "if(manualScrolling&&options.scrollCatchup!==true)" in render
    assert "if(!manualScrolling)" in render
    assert "lnPairedFuriganaPaintOverlaps" not in render
    assert "perf_revision:'ln-paired-v178'" in html


def test_scroll_does_not_recreate_bookmark_and_settle_timers_per_event() -> None:
    html = INDEX.read_text(encoding="utf-8")
    bookmark = html.split("function scheduleLnAutoBookmark()", 1)[1].split("function restoreLnReaderOffset", 1)[0]
    assert "ui.lnAutoBookmarkDueAt=performance.now()+20000" in bookmark
    assert "if(ui.lnAutoBookmarkTimer)return" in bookmark
    assert "cancelLnAutoBookmark();" not in bookmark

    handler = html.split("$('lnReaderScroll').addEventListener('scroll',()=>{", 1)[1].split("},{passive:true});", 1)[0]
    assert "if(!lnReaderScrollSettleTimer)" in handler
    assert "clearTimeout(lnReaderScrollSettleTimer)" not in handler


def test_alignment_revision_bumped_to_v17() -> None:
    source = AUDIOBOOKS.read_text(encoding="utf-8")
    assert 'reading-audio-v3-leading-prefix-v18' in source
