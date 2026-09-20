from __future__ import annotations

from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker


def _vertical_lane_image(*, lanes: list[int], rows: int = 7, weak_first: bool = False) -> Image.Image:
    image = Image.new("RGB", (240, 320), "white")
    draw = ImageDraw.Draw(image)
    for x in lanes:
        for row in range(rows):
            top = 42 + row * 24
            if weak_first and row == 0:
                draw.rectangle((x + 3, top + 2, x + 9, top + 11), outline="black", width=2)
            else:
                draw.rectangle((x, top, x + 13, top + 15), outline="black", width=3)
    return image


def _anchor_region(x_px: int = 42, *, text: str = "してたのか？") -> dict[str, object]:
    return {
        "text": text,
        "raw_text": "",
        "orientation": "vertical",
        "x": x_px / 240,
        "y": 1.0 - (42 + 7 * 24) / 320,
        "width": 14 / 240,
        "height": (7 * 24 - 8) / 320,
        "confidence": 0.92,
        "detector": "manga-ink-components-v1",
        "source": "manga-layout-line-v1",
        "geometry_source": "layout-line-proportional-v1",
        "geometry_status": "observed",
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 7,
            "component_coverage": 0.95,
            "black_ratio": 0.15,
            "white_ratio": 0.80,
            "midtone_ratio": 0.05,
        },
    }


def test_v78_finds_two_missing_companion_columns_next_to_observed_bubble_lane() -> None:
    image = _vertical_lane_image(lanes=[42, 72, 103])
    proposals = worker._speech_bubble_companion_lane_proposals(image, [_anchor_region()])

    assert len(proposals) == 2
    centers = sorted(
        round((float(item["x"]) + float(item["width"]) / 2.0) * image.width)
        for item in proposals
    )
    assert centers == [79, 110]
    assert all(
        (item.get("provenance") or {}).get("support_kind")
        == "speech-bubble-companion-chain-v1"
        for item in proposals
    )


def test_v78_does_not_promote_a_single_art_like_neighbor() -> None:
    image = _vertical_lane_image(lanes=[42, 78])
    proposals = worker._speech_bubble_companion_lane_proposals(image, [_anchor_region()])

    assert proposals == []


def test_v78_trims_one_ghost_prefix_when_physical_ink_has_one_fewer_main_row() -> None:
    image = _vertical_lane_image(lanes=[84], rows=9, weak_first=True)
    item = {
        "text": "金じゃなかったのか？",
        "raw_text": "",
        "orientation": "vertical",
        "x": 82 / 240,
        "y": 1.0 - (42 + 9 * 24) / 320,
        "width": 18 / 240,
        "height": (9 * 24 - 8) / 320,
        "confidence": 0.92,
        "detector": "manga-ink-components-v1",
        "source": "manga-layout-line-v1",
        "geometry_status": "observed",
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 9,
            "component_coverage": 0.95,
            "black_ratio": 0.15,
            "white_ratio": 0.80,
            "midtone_ratio": 0.05,
        },
    }

    repaired = worker._trim_vertical_ghost_prefix_from_main_ink(image, item)

    assert repaired["text"] == "じゃなかったのか？"
    assert repaired["ghost_prefix_trimmed"] == "金"
    assert "ghost-prefix-main-ink-v1" in str(repaired.get("geometry_source") or "")


def test_v78_keeps_prefix_when_row_count_is_short_but_first_glyph_is_full_size() -> None:
    image = _vertical_lane_image(lanes=[84], rows=9)
    item = {
        "text": "金じゃなかったのか？",
        "raw_text": "",
        "orientation": "vertical",
        "x": 82 / 240,
        "y": 1.0 - (42 + 9 * 24) / 320,
        "width": 18 / 240,
        "height": (9 * 24 - 8) / 320,
        "confidence": 0.92,
        "detector": "manga-ink-components-v1",
        "source": "manga-layout-line-v1",
        "geometry_status": "observed",
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 9,
            "component_coverage": 0.95,
            "black_ratio": 0.15,
            "white_ratio": 0.80,
            "midtone_ratio": 0.05,
        },
    }

    assert worker._trim_vertical_ghost_prefix_from_main_ink(image, item)["text"] == item["text"]


def test_v78_keeps_prefix_when_every_character_has_a_physical_row() -> None:
    image = _vertical_lane_image(lanes=[84], rows=10)
    item = {
        "text": "金じゃなかったのか？",
        "raw_text": "",
        "orientation": "vertical",
        "x": 82 / 240,
        "y": 1.0 - (42 + 10 * 24) / 320,
        "width": 18 / 240,
        "height": (10 * 24 - 8) / 320,
        "confidence": 0.92,
        "detector": "manga-ink-components-v1",
        "source": "manga-layout-line-v1",
        "geometry_status": "observed",
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 10,
            "component_coverage": 0.95,
            "black_ratio": 0.15,
            "white_ratio": 0.80,
            "midtone_ratio": 0.05,
        },
    }

    assert worker._trim_vertical_ghost_prefix_from_main_ink(image, item)["text"] == item["text"]


class _SequenceModel:
    def __init__(self, values: list[str]) -> None:
        self.values = list(values)

    def __call__(self, _image: Image.Image) -> str:
        assert self.values
        return self.values.pop(0)


def test_v78_recovers_two_missing_bubble_columns_only_with_crop_consensus() -> None:
    image = _vertical_lane_image(lanes=[42, 72, 103])
    regions = [_anchor_region()]
    model = _SequenceModel(
        [
            "拭き掃除でも",
            "拭き掃除でも",
            "拭き掃除でも",
            "ずっと村の",
            "ずっと村の",
            "ずっと村の",
        ]
    )

    recovered = worker._recover_missing_speech_bubble_columns(model, image, regions)

    assert [item["text"] for item in recovered] == ["してたのか？", "拭き掃除でも", "ずっと村の"]
    assert all(item.get("segments") for item in recovered[1:])
    assert all(
        item.get("recognition_selection") == "speech-bubble-companion-consensus-v1"
        for item in recovered[1:]
    )


def test_v78_rejects_missing_bubble_column_when_three_crops_disagree() -> None:
    image = _vertical_lane_image(lanes=[42, 72, 103])
    regions = [_anchor_region()]
    model = _SequenceModel(
        [
            "拭き掃除でも",
            "掃除でも",
            "なにか別",
            "ずっと村の",
            "村の",
            "別の文字",
        ]
    )

    assert worker._recover_missing_speech_bubble_columns(model, image, regions) == regions


def test_v78_uses_a_fresh_region_artifact_filename() -> None:
    from pathlib import Path

    from pudge import manga

    source = Path(manga.__file__).read_text(encoding="utf-8")
    assert "-regions-v96p27.json" in source
    assert "-regions-v77.json" not in source
