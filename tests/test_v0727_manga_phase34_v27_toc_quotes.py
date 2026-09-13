from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _seg(text: str, x: float) -> dict[str, object]:
    return {
        "text": text,
        "x": x,
        "y": 0.5,
        "width": 0.03,
        "height": 0.03,
        "source": "vision-accurate-ink-v4",
    }


def _row(text: str, *, gaps: dict[int, float] | None = None) -> dict[str, object]:
    core = worker._chapter_quote_core_characters(text)
    x = 0.08
    segs: list[dict[str, object]] = []
    for index, char in enumerate(core):
        segs.append(_seg(char, x))
        x += 0.036
        if gaps and (index + 1) in gaps:
            x += gaps[index + 1]
    return {
        "text": text,
        "raw_text": text,
        "orientation": "horizontal",
        "segments": segs,
    }


def test_v27_candidate_boundaries_include_visible_large_gap_and_trailing_punctuation() -> None:
    row = _row(
        "第4話海軍大佐斧手のモーガン。",
        gaps={7: 0.05},
    )
    candidates = worker._chapter_quote_candidate_boundaries(row)
    assert 7 in candidates
    assert 14 in candidates


def test_v27_page_style_recovers_three_missing_quote_pairs(monkeypatch) -> None:
    chapter2 = _row("第2話その男・麦わらのルフィ、", gaps={6: 0.02})
    chapter3 = _row("第3話、海賊狩りのゾロ、登場", gaps={3: 0.04, 10: 0.02})
    chapter4 = _row("第4話海軍大佐斧手のモーガン。", gaps={7: 0.05})
    stable = [
        _row("第5話〝海賊王と大剣豪〟"),
        _row("第6話〝1人目〟"),
        _row("第7話〝友達〟"),
    ]

    openings = {
        "第2話その男・麦わらのルフィ、": 6,
        "第3話、海賊狩りのゾロ、登場": 3,
        "第4話海軍大佐斧手のモーガン。": 7,
    }

    def fake_signal(_model, _image, piece, boundary):
        current = worker._normalize_line_surface(piece.get("text"))
        if openings.get(current) == boundary:
            return True, False, f"local〝{boundary}"
        return False, False, "local"

    monkeypatch.setattr(worker, "_chapter_local_quote_signal", fake_signal)
    image = Image.new("RGB", (1000, 1000), "white")
    try:
        result = worker._repair_page_chapter_quote_style(
            object(), image, [chapter2, chapter3, chapter4, *stable]
        )
    finally:
        image.close()

    assert result[0]["text"] == "第2話その男〝麦わらのルフィ〟"
    assert result[1]["text"] == "第3話〝海賊狩りのゾロ〟登場"
    assert result[2]["text"] == "第4話海軍大佐〝斧手のモーガン〟"


def test_v27_page_style_requires_three_balanced_reference_rows(monkeypatch) -> None:
    target = _row("第3話、海賊狩りのゾロ、登場", gaps={3: 0.04})
    references = [
        _row("第5話〝海賊王と大剣豪〟"),
        _row("第6話〝1人目〟"),
    ]
    calls = 0

    def fake_signal(*_args):
        nonlocal calls
        calls += 1
        return True, False, "〝"

    monkeypatch.setattr(worker, "_chapter_local_quote_signal", fake_signal)
    image = Image.new("RGB", (100, 100), "white")
    try:
        result = worker._repair_page_chapter_quote_style(
            object(), image, [target, *references]
        )
    finally:
        image.close()

    assert result[0]["text"] == target["text"]
    assert calls == 0


def test_v27_does_not_guess_closing_quote_with_multiple_later_punctuation(monkeypatch) -> None:
    target = _row("第9話、甲、乙、丙", gaps={3: 0.04})
    references = [
        _row("第5話〝海賊王と大剣豪〟"),
        _row("第6話〝1人目〟"),
        _row("第7話〝友達〟"),
    ]

    def fake_signal(_model, _image, piece, boundary):
        if piece is target and boundary == 3:
            return True, False, "第9話〝甲"
        return False, False, ""

    monkeypatch.setattr(worker, "_chapter_local_quote_signal", fake_signal)
    image = Image.new("RGB", (100, 100), "white")
    try:
        result = worker._repair_page_chapter_quote_style(
            object(), image, [target, *references]
        )
    finally:
        image.close()

    assert result[0]["text"] == target["text"]


def test_v27_segment_sync_updates_clickable_kanji_despite_missing_punctuation_segments() -> None:
    target_text = "第4話海軍大佐〝斧手のモーガン〟"
    segment_text = "第4話海軍大佐洋手のモーガン"
    piece = {
        "text": target_text,
        "raw_text": target_text,
        "orientation": "horizontal",
        "segments": [_seg(char, 0.05 + i * 0.04) for i, char in enumerate(segment_text)],
    }

    repaired = worker._sync_chapter_core_segments_to_text(piece)

    stream = "".join(str(item.get("text") or "") for item in repaired["segments"])
    assert stream == "第4話海軍大佐斧手のモーガン"
    assert repaired["chapter_segment_sync"] == [
        {
            "study_index": 7,
            "segment_index": 7,
            "from": "洋",
            "to": "斧",
        }
    ]
    assert repaired["segments"][7]["recognition_correction"] == "chapter-final-core-sync-v1"


def test_v27_segment_sync_rejects_low_similarity_stream() -> None:
    piece = {
        "text": "第4話海軍大佐〝斧手のモーガン〟",
        "raw_text": "第4話海軍大佐〝斧手のモーガン〟",
        "orientation": "horizontal",
        "segments": [_seg(char, 0.05 + i * 0.04) for i, char in enumerate("第4話xxxxxxxxxxx")],
    }
    assert worker._sync_chapter_core_segments_to_text(piece) is piece
