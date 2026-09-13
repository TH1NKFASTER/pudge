from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _row(text: str, *, complete_segments: bool) -> dict[str, object]:
    core = text.replace("〝", "").replace("〟", "")
    segment_text = text if complete_segments else core
    return {
        "text": text,
        "raw_text": text,
        "orientation": "horizontal",
        "x": 0.05,
        "y": 0.70,
        "width": 0.55,
        "height": 0.05,
        "segments": [
            {
                "text": char,
                "orientation": "horizontal",
                "x": 0.05 + index * 0.02,
                "y": 0.70,
                "width": 0.018,
                "height": 0.03,
                "source": "synthetic",
            }
            for index, char in enumerate(segment_text)
        ],
    }


def test_v51_existing_quote_text_still_repairs_missing_segment_geometry(monkeypatch) -> None:
    rows = [
        _row("第1話〝友達〟", complete_segments=True),
        _row("第2話〝友達〟", complete_segments=True),
        _row("第3話〝友達〟", complete_segments=True),
        _row("第4話〝友達〟", complete_segments=True),
        _row("第5話〝友達〟", complete_segments=False),
    ]
    calls: list[str] = []

    def fake_repair(
        image: Image.Image,
        piece: dict[str, object],
        target_core: str,
        *,
        style_rows: int,
    ) -> dict[str, object]:
        calls.append(str(piece["text"]))
        repaired = dict(piece)
        repaired["v51_repaired"] = True
        return repaired

    monkeypatch.setattr(worker, "_repair_chapter_quotes_from_page_ink", fake_repair)

    image = Image.new("RGB", (760, 1200), "white")
    try:
        output = worker._repair_page_chapter_stability_consensus(image, rows)
    finally:
        image.close()

    assert calls == ["第5話〝友達〟"]
    assert not output[0].get("v51_repaired")
    assert output[4]["v51_repaired"] is True


def test_v51_complete_existing_quote_segment_stream_keeps_fast_path(monkeypatch) -> None:
    rows = [
        _row(f"第{index}話〝友達〟", complete_segments=True)
        for index in range(1, 6)
    ]

    def fail_repair(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("complete quote geometry should keep the fast path")

    monkeypatch.setattr(worker, "_repair_chapter_quotes_from_page_ink", fail_repair)

    image = Image.new("RGB", (760, 1200), "white")
    try:
        output = worker._repair_page_chapter_stability_consensus(image, rows)
    finally:
        image.close()

    assert [item["text"] for item in output] == [item["text"] for item in rows]
