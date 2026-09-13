from __future__ import annotations

from PIL import Image, ImageDraw

from pudge.manga_ocr_worker import (
    _LAYOUT_LINE_SOURCE,
    _accept_vertical_trailing_context_text,
    _layout_cluster_proposals,
    _merge_layout_cluster_donors,
    _prefer_detector_recognition,
    _recover_vertical_trailing_context,
    _vertical_leading_ink_geometry,
)


def test_exact_latin_vision_masthead_beats_mangaocr_japanese_suffix() -> None:
    item = {
        "orientation": "horizontal",
        "raw_text": "JUMP COMICS：",
        "confidence": 1.0,
        "segments": [
            {
                "text": char,
                "orientation": "horizontal",
                "x": 0.05 + index * 0.04,
                "y": 0.5,
                "width": 0.035,
                "height": 0.05,
                "source": "vision-accurate-range-v2",
            }
            for index, char in enumerate("JUMPCOMICS：")
        ],
    }
    assert _prefer_detector_recognition(item, "ＪＵＭＰＣＯＭＩＣＳで") is True


def test_short_dense_kanji_one_can_extend_leading_vertical_lane() -> None:
    image = Image.new("RGB", (160, 240), "white")
    draw = ImageDraw.Draw(image)
    # A short dense 一 above the observed lane. It does not continue sideways
    # like a bubble frame.
    draw.rectangle((70, 48, 88, 51), fill="black")
    for top, bottom in ((61, 80), (83, 101), (104, 122), (125, 143), (146, 164), (167, 185)):
        draw.rectangle((68, top, 90, bottom), fill="black")
    region = {
        "text": "番身にしみて",
        "raw_text": "番身にしみて",
        "orientation": "vertical",
        "x": 67 / 160,
        "y": 1.0 - 190 / 240,
        "width": 25 / 160,
        "height": (190 - 60) / 240,
        "source": _LAYOUT_LINE_SOURCE,
    }
    recovered = _vertical_leading_ink_geometry(image, region)
    assert recovered is not None
    info = recovered["provenance"]["leading_ink_geometry"]
    assert int(info["new_top_px"]) <= 51


def test_vertical_trailing_context_can_recover_missing_emphatic_suffix() -> None:
    image = Image.new("RGB", (160, 260), "white")
    draw = ImageDraw.Draw(image)
    for top, bottom in ((40, 58), (62, 80), (84, 102), (106, 124)):
        draw.rectangle((70, top, 90, bottom), fill="black")
    # Missing trailing glyph directly below the detector bbox.
    draw.rectangle((70, 139, 90, 157), fill="black")
    region = {
        "text": "邪魔する",
        "raw_text": "邪魔する",
        "orientation": "vertical",
        "x": 68 / 160,
        "y": 1.0 - 128 / 260,
        "width": 24 / 160,
        "height": (128 - 38) / 260,
        "source": _LAYOUT_LINE_SOURCE,
        "hypotheses": [{"id": "manga-ocr", "text": "邪魔する", "selected": True}],
    }
    assert _accept_vertical_trailing_context_text("邪魔する", "邪魔するぜ") is True
    repaired = _recover_vertical_trailing_context(lambda _crop: "邪魔するぜ", image, region)
    assert repaired["text"] == "邪魔するぜ"
    assert float(repaired["y"]) < float(region["y"])
    assert repaired["selected_hypothesis_id"] == "manga-ocr-trailing-ink"


def test_layout_cluster_groups_adjacent_columns_but_not_distant_bubble() -> None:
    def lane(x: float, y: float = 0.80, height: float = 0.08) -> dict[str, object]:
        return {
            "text": "",
            "orientation": "vertical",
            "x": x,
            "y": y,
            "width": 0.025,
            "height": height,
            "source": _LAYOUT_LINE_SOURCE,
        }

    image = Image.new("RGB", (200, 200), "white")
    clusters = _layout_cluster_proposals(image, [
        lane(0.10), lane(0.132),  # same bubble
        lane(0.25), lane(0.282),  # another bubble
        lane(0.70),               # isolated
    ])
    assert len(clusters) == 2
    assert sorted(int((cluster["provenance"])["member_count"]) for cluster in clusters) == [2, 2]


def test_cluster_context_donor_never_rewrites_existing_lane() -> None:
    existing = {
        "text": "番身にしみて",
        "raw_text": "番身にしみて",
        "orientation": "vertical",
        "x": 0.25,
        "y": 0.50,
        "width": 0.03,
        "height": 0.11,
        "source": _LAYOUT_LINE_SOURCE,
    }
    donor = {
        "text": "一番身にしみて",
        "raw_text": "一番身にしみて",
        "orientation": "vertical",
        "x": 0.249,
        "y": 0.50,
        "width": 0.031,
        "height": 0.13,
        "source": _LAYOUT_LINE_SOURCE,
        "provenance": {"cluster_context_donor": True},
    }
    merged = _merge_layout_cluster_donors([existing, donor])
    assert len(merged) == 1
    assert merged[0]["text"] == "番身にしみて"


def test_short_kanji_pair_can_use_one_following_glyph_as_context() -> None:
    from pudge.manga_ocr_worker import _repair_short_kanji_pairs_with_mangaocr

    image = Image.new("RGB", (400, 100), "white")
    piece = {
        "text": "DAWNー皆険の夜明けー",
        "raw_text": "DAWNー皆険の夜明けー",
        "orientation": "horizontal",
        "selected_hypothesis_id": "detector-recognition",
        "segments": [
            {"text": "ー", "x": 0.10, "y": 0.40, "width": 0.05, "height": 0.20, "source": "vision-accurate-range-v2"},
            {"text": "管", "x": 0.15, "y": 0.40, "width": 0.08, "height": 0.20, "source": "vision-accurate-range-v2"},
            {"text": "険", "x": 0.23, "y": 0.40, "width": 0.08, "height": 0.20, "source": "vision-accurate-range-v2"},
            {"text": "の", "x": 0.31, "y": 0.40, "width": 0.05, "height": 0.20, "source": "vision-accurate-range-v2"},
        ],
    }
    calls = 0

    def model(_crop: Image.Image) -> str:
        nonlocal calls
        calls += 1
        return "" if calls == 1 else "冒険の"

    repaired = _repair_short_kanji_pairs_with_mangaocr(model, image, piece)
    assert repaired["text"] == "DAWNー冒険の夜明けー"
    assert calls >= 2
