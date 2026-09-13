from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _extended() -> dict[str, object]:
    return {
        "orientation": "vertical",
        "x": 0.711658,
        "y": 0.219,
        "width": 0.030632,
        "height": 0.103666,
        "leading_ink_geometry": True,
        "source": worker._LAYOUT_LINE_SOURCE,
        "provenance": {
            "leading_ink_geometry": {
                "old_top_px": 835,
                "new_top_px": 813,
                "extension_px": 22,
                "scan_x_px": [538, 567],
            }
        },
    }


def _segments(text: str, first_width: float, first_source: str) -> list[dict[str, object]]:
    compact = worker._compact_surface(text)
    out: list[dict[str, object]] = []
    for index, ch in enumerate(compact):
        out.append(
            {
                "text": ch,
                "orientation": "vertical",
                "x": 0.711658,
                "y": 0.30 - index * 0.01,
                "width": first_width if index == 0 else 0.025,
                "height": 0.01,
                "source": first_source if index == 0 else "vertical-leading-ink-v2+tight-v1",
            }
        )
    return out


def test_p009_spurious_wide_leading_kana_is_rejected(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    monkeypatch.setattr(
        worker,
        "_vertical_leading_ink_character_segments",
        lambda _image, _region, text: _segments(str(text), 0.030632, "vertical-leading-ink-v2"),
    )
    monkeypatch.setattr(
        worker,
        "_tighten_vertical_slot_ink_segments",
        lambda _image, _segments_value: _segments(
            "どうそつけ！！",
            0.064474,
            "vertical-leading-ink-v2+x-context-v1+tight-v1",
        ),
    )
    try:
        assert worker._leading_prefix_width_supported(
            image, _extended(), "うそつけ！！", "どうそつけ！！"
        ) is False
    finally:
        image.close()


def test_genuine_leading_prefix_with_bounded_x_context_is_allowed(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    monkeypatch.setattr(
        worker,
        "_vertical_leading_ink_character_segments",
        lambda _image, _region, text: _segments(str(text), 0.030632, "vertical-leading-ink-v2"),
    )
    monkeypatch.setattr(
        worker,
        "_tighten_vertical_slot_ink_segments",
        lambda _image, _segments_value: _segments(
            "じゃねェ！！",
            0.0237,
            "vertical-leading-ink-v2+x-context-v1+tight-v1",
        ),
    )
    try:
        assert worker._leading_prefix_width_supported(
            image, _extended(), "ねェ！！", "じゃねェ！！"
        ) is True
    finally:
        image.close()
