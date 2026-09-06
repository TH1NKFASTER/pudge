from __future__ import annotations

from pathlib import Path

from pudge.reading_audio_alignment import (
    _punctuation_boundaries,
    chapter_audio_text,
    light_novel_position_for_audio,
)

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
AUDIOBOOKS = ROOT / "pudge" / "audiobooks.py"


def _second_act_with_precision_words() -> dict:
    text = (
        "緩やかに下る坂も終わり、しばらくは土地の起伏といえば"
        "申し訳程度の小さい丘、という実に進みやすい道だった。"
    )
    hints = [
        {"offset_start": 0, "offset_end": 3, "surface": "緩やか", "reading": "ゆるやか"},
        {"offset_start": 8, "offset_end": 11, "surface": "終わり", "reading": "おわり"},
        {"offset_start": 11, "offset_end": 15, "surface": "しばらく", "reading": "しばらく"},
        {"offset_start": 15, "offset_end": 16, "surface": "は", "reading": "は"},
        {"offset_start": 16, "offset_end": 18, "surface": "土地", "reading": "とち"},
        {"offset_start": 18, "offset_end": 19, "surface": "の", "reading": "の"},
        {"offset_start": 19, "offset_end": 21, "surface": "起伏", "reading": "きふく"},
    ]
    # This shape mimics the cached chapter-start --words STT: short words have
    # their own timestamps, including the real pause before しばらく.
    precision_segments = [
        {
            "start": 6856.20,
            "end": 6860.80,
            "text": "緩やかに下る坂も終わりしばらくは土地の起伏",
            "words": [
                {"start": 6856.20, "end": 6856.80, "word": "ゆるやか"},
                {"start": 6856.82, "end": 6857.10, "word": "に"},
                {"start": 6857.12, "end": 6857.45, "word": "くだる"},
                {"start": 6857.47, "end": 6857.72, "word": "さか"},
                {"start": 6857.74, "end": 6857.86, "word": "も"},
                {"start": 6858.18, "end": 6858.66, "word": "おわり"},
                {"start": 6859.30, "end": 6860.04, "word": "しばらく"},
                {"start": 6860.05, "end": 6860.16, "word": "は"},
                {"start": 6860.17, "end": 6860.47, "word": "とち"},
                {"start": 6860.48, "end": 6860.57, "word": "の"},
                {"start": 6860.58, "end": 6860.80, "word": "きふく"},
            ],
        }
    ]
    return {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 1,
                "title": "第二幕",
                "normalized_length": 100,
                "start": 6852.494,
                "end": 6900.0,
                "anchors": [
                    {"offset": 34.999, "time": 6861.82},
                    {"offset": 49, "time": 6864.70},
                ],
                "speech_regions": [
                    {"start": 6852.68, "end": 6853.62},
                    {"start": 6856.20, "end": 6858.66},
                    {"start": 6859.30, "end": 6861.82},
                ],
                "leading_prefix_debug": {
                    "attempted": True,
                    "recovered": True,
                    "chapter_marker": True,
                    "marker_start": 6852.494,
                    "marker_end": 6853.594,
                    "story_start": 6856.134,
                    "precision_window_start": 6831.714,
                    "precision_window_end": 6903.714,
                },
                "punctuation_pause_count": 1286,
                "_runtime_punctuation_boundaries": _punctuation_boundaries(chapter_audio_text(text)),
                "_runtime_reading_hints": hints,
                "_runtime_precision_segments": precision_segments,
            }
        ],
    }


def test_precision_word_clock_keeps_shibaraku_current_longer_than_linear_prefix() -> None:
    alignment = _second_act_with_precision_words()

    # v182's linear 11 -> 34.999 interval already switched to は around 6859.66.
    # Word-level precision timing should still be inside しばらく (offset < 15).
    mid = light_novel_position_for_audio(alignment, 6859.66)
    assert mid is not None
    assert 11.0 <= float(mid["chapter_char_offset_exact"]) < 15.0
    assert int(mid["anchor_window"]["runtime_reading_hint_anchor_count"]) >= 3
    assert mid["anchor_window"]["runtime_reading_hint_mode"] == "precision_word_reading_prefix"

    after = light_novel_position_for_audio(alignment, 6860.05)
    assert after is not None
    assert float(after["chapter_char_offset_exact"]) >= 15.0


def test_runtime_loader_attaches_cached_precision_words_and_reader_hints() -> None:
    source = AUDIOBOOKS.read_text(encoding="utf-8")
    block = source.split("def _attach_runtime_alignment_context", 1)[1].split(
        "def _load_alignment", 1
    )[0]
    assert "_cached_chapter_start_reading_hints" in block
    assert "_chapter_start_precision_cache_path" in block
    assert '"_runtime_reading_hints"' in block
    assert '"_runtime_precision_segments"' in block
    assert "audiobook-chapter-start-stt-v1" in block


def test_hidden_spoiler_images_keep_same_eager_img_in_normal_flow() -> None:
    html = HTML.read_text(encoding="utf-8")

    assert "ln-inline-image-spoiler-label" in html
    assert 'src="${src}" data-ln-src="${src}"' in html
    assert 'loading="eager"' in html
    assert "function lnLoadInlineImage(img" in html
    assert "const deferHidden=root.classList.contains('blur-images')" not in html
    assert "if(deferHidden){img.removeAttribute('src')" not in html
    assert "canvas.className='ln-inline-image-raster'" not in html
    assert "canvas.className='ln-inline-image-blur-preview'" not in html
    assert "lnLoadInlineImage(image,figure)" in html  # legacy recovery path remains
