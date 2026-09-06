from __future__ import annotations

from pathlib import Path

from pudge.reading_audio_alignment import (
    _punctuation_boundaries,
    audio_position_for_light_novel_offset,
    chapter_audio_text,
    light_novel_position_for_audio,
)


ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def _real_v181_second_act_shape(*, stored_pause_count: int) -> dict:
    text = (
        "緩やかに下る坂も終わり、しばらくは土地の起伏といえば"
        "申し訳程度の小さい丘、という実に進みやすい道だった。"
    )
    boundaries = _punctuation_boundaries(chapter_audio_text(text))
    assert {"offset": 11, "strength": 1} in boundaries
    return {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 1,
                "title": "第二幕",
                "normalized_length": 100,
                "start": 6852.494,
                "end": 6900.0,
                # Exact live shape from the user's v181 trace after spoken-title
                # cleanup: the first surviving positive anchor is already 34.999.
                "anchors": [
                    {"offset": 34.999, "time": 6861.82},
                    {"offset": 49, "time": 6864.7},
                ],
                "speech_regions": [
                    {"start": 6852.68, "end": 6853.62},
                    {"start": 6856.2, "end": 6858.66},
                    {"start": 6859.3, "end": 6861.82},
                    {"start": 6862.62, "end": 6864.7},
                ],
                "leading_prefix_debug": {
                    "attempted": True,
                    "recovered": True,
                    "chapter_marker": True,
                    "marker_start": 6852.494,
                    "marker_end": 6853.594,
                    "story_start": 6856.134,
                },
                # Real v181 cache already had 1172 stored pause anchors elsewhere
                # in the chapter. That must not suppress prefix reconstruction.
                "punctuation_pause_count": stored_pause_count,
                "_runtime_punctuation_boundaries": boundaries,
            }
        ],
    }


def test_recovered_prefix_pause_is_restored_even_when_cache_has_other_pause_anchors() -> None:
    alignment = _real_v181_second_act_shape(stored_pause_count=1172)

    early = light_novel_position_for_audio(alignment, 6857.957)
    assert early is not None
    assert float(early["chapter_char_offset_exact"]) < 9.0

    hold = light_novel_position_for_audio(alignment, 6858.9)
    assert hold is not None
    assert 10.998 <= float(hold["chapter_char_offset_exact"]) <= 10.9991
    assert int(hold["anchor_window"]["stored_punctuation_pause_count"]) == 1172
    assert int(hold["anchor_window"]["runtime_punctuation_pause_count"]) >= 1

    resumed = light_novel_position_for_audio(alignment, 6859.3)
    assert resumed is not None
    assert abs(float(resumed["chapter_char_offset_exact"]) - 11.0) < 1e-6

    assert abs(
        float(audio_position_for_light_novel_offset(alignment, 1, 11) or 0.0)
        - 6859.3
    ) < 1e-6


def test_spoiler_blur_uses_ttsu_style_same_img_and_plain_css_filter() -> None:
    html = HTML.read_text(encoding="utf-8")

    assert ".ln-reader.blur-images .ln-inline-image:not(.revealed) img{filter:blur(44px);cursor:pointer}" in html
    assert "ln-inline-image-spoiler-label" in html
    assert "function lnBuildInlineBlurPreview(img)" not in html
    assert "canvas.className='ln-inline-image-blur-preview'" not in html
    assert "canvas.className='ln-inline-image-raster'" not in html
    assert "contain:paint" not in html.split(".ln-inline-image-frame{", 1)[1].split("}", 1)[0]
    assert "translateZ(0)" not in html.split(".ln-inline-image-frame{", 1)[1].split("}", 1)[0]

    assert "const figure=event.target.closest?.('#lnReader .ln-inline-image');if(!figure)return;" in html
    assert "const image=figure.querySelector('img');if(!image)return;" in html
