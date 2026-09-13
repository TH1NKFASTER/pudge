
from __future__ import annotations

from pudge.manga_ocr_worker import _strip_dark_block_decorative_suffix


def test_dark_block_separator_suffix_is_removed_when_segments_prove_it_is_not_text() -> None:
    item = {
        "source": "dark-block-proposal",
        "text": "大海賊時代を迎えるー",
        "segments": [
            {
                "text": char,
                "source": "dark-columns-v1",
                "x": 0.2 if index < 6 else 0.1,
                "y": 0.2 - index * 0.01,
                "width": 0.03,
                "height": 0.02,
            }
            for index, char in enumerate("大海賊時代を迎える")
        ],
    }

    assert _strip_dark_block_decorative_suffix(item) is True
    assert item["text"] == "大海賊時代を迎える"
    assert item["decorative_suffix_removed"] == "ー"
    assert item["decorative_suffix_reason"] == "dark-block-long-rule-v1"


def test_real_dash_is_preserved_when_segment_stream_contains_it() -> None:
    item = {
        "source": "dark-block-proposal",
        "text": "テストー",
        "segments": [
            {"text": char, "source": "dark-columns-v1"}
            for char in "テストー"
        ],
    }

    assert _strip_dark_block_decorative_suffix(item) is False
    assert item["text"] == "テストー"
