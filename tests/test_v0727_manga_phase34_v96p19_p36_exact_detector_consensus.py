from __future__ import annotations

from PIL import Image, ImageDraw

import pudge.manga as manga
import pudge.manga_ocr_worker as worker


class _Model:
    def __init__(self, values: list[str]):
        self.values = list(values)
        self.calls = 0

    def __call__(self, _image: Image.Image) -> str:
        value = self.values[self.calls]
        self.calls += 1
        return value


def _head_item(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "text": "これらぶっかけ",
        "raw_text": "これらぶっかけ",
        "source": worker._LAYOUT_LINE_SOURCE,
        "orientation": "vertical",
        "detector": worker._LAYOUT_DETECTOR,
        "confidence": 0.877,
        "selected_hypothesis_id": "manga-ocr",
        "hypotheses": [
            {"id": "manga-ocr", "text": "これらぶっかけ", "source": "manga-ocr", "selected": True}
        ],
        "provenance": {
            "component_count": 6,
            "component_coverage": 0.9009,
            "detector_bbox_px": [475.86, 510.8, 502.14, 624.2],
        },
    }
    item.update(overrides)
    return item


def test_v96p20_exact_detector_pair_repairs_p36_like_head_prefix() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    model = _Model(["頭からぶっかけ", "頭からぶっかけ"])
    result = worker._recover_vertical_exact_detector_leading_pair_consensus(
        model, image, _head_item()
    )
    assert result["text"] == "頭からぶっかけ"
    assert result["recognizer_retry"] == "vertical-exact-detector-leading-pair-v1"
    assert result["selected_hypothesis_id"] == "manga-ocr-exact-detector-leading-pair-v1"
    info = result["provenance"]["exact_detector_leading_pair_consensus"]
    assert info["current"] == "これらぶっかけ"
    assert info["candidate"] == "頭からぶっかけ"
    assert info["views"] == ["頭からぶっかけ", "頭からぶっかけ"]
    assert model.calls == 2


def test_v96p20_exact_detector_pair_rejects_suffix_change_or_weak_evidence() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    original = _head_item()
    assert worker._recover_vertical_exact_detector_leading_pair_consensus(
        _Model(["頭からぶっかる", "頭からぶっかる"]), image, original
    ) == original
    assert worker._recover_vertical_exact_detector_leading_pair_consensus(
        _Model(["頭からぶっかけ", "頭からぶっかけ"]),
        image,
        _head_item(confidence=0.70),
    ) == _head_item(confidence=0.70)
    assert worker._recover_vertical_exact_detector_leading_pair_consensus(
        _Model(["頭からぶっかけ", "頭からぶっかけ"]),
        image,
        _head_item(
            provenance={
                "component_count": 5,
                "component_coverage": 0.9009,
                "detector_bbox_px": [475.86, 510.8, 502.14, 624.2],
            }
        ),
    ) == _head_item(
        provenance={
            "component_count": 5,
            "component_coverage": 0.9009,
            "detector_bbox_px": [475.86, 510.8, 502.14, 624.2],
        }
    )


def _draw_slots(image: Image.Image, *, left: int, top: int, count: int) -> tuple[int, int, int, int]:
    draw = ImageDraw.Draw(image)
    width = 16
    gap = 5
    glyph_h = 14
    y = top
    for _ in range(count):
        draw.rectangle((left, y, left + width, y + glyph_h), fill="black")
        y += glyph_h + gap
    return left - 2, top - 2, left + width + 2, y - gap + 2


def _donor_for_bbox(text: str, bbox: tuple[int, int, int, int], image: Image.Image) -> dict[str, object]:
    left, top, right, bottom = bbox
    width, height = image.size
    return {
        "text": text,
        "raw_text": text,
        "orientation": "vertical",
        "x": left / width,
        "y": 1.0 - bottom / height,
        "width": (right - left) / width,
        "height": (bottom - top) / height,
        "confidence": 0.25,
        "detector": "wide-vertical-text-donor-v1",
        "source": worker._LAYOUT_LINE_SOURCE,
        "recognition_selection": "wide-vertical-layout-donor-v1",
        "selected_hypothesis_id": "wide-vertical-layout-donor-v1",
        "hypotheses": [
            {
                "id": "wide-vertical-layout-donor-v1",
                "text": text,
                "source": "recognized-wide-region",
                "selected": True,
            }
        ],
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": max(1, len(text)),
            "component_coverage": 0.92,
            "cluster_context_donor": True,
            "wide_vertical_text_donor": True,
            "detector_bbox_px": [float(left), float(top), float(right), float(bottom)],
        },
    }


def test_v96p20_wide_exact_consensus_allows_four_glyph_tail_extension() -> None:
    image = Image.new("RGB", (400, 600), "white")
    candidate = "おれは酒や食い物を"
    bbox = _draw_slots(image, left=90, top=90, count=len(candidate))
    donor = _donor_for_bbox("おれは酒や", bbox, image)
    model = _Model([candidate, candidate])
    result = worker._reread_wide_vertical_layout_donors_exact_crop(model, image, [donor])[0]
    assert result["text"] == candidate
    assert result["provenance"]["wide_vertical_exact_crop_relation"] == "prefix-extension-plus4"
    assert result["provenance"]["wide_vertical_exact_crop_consensus_views"] == [candidate, candidate]
    assert model.calls == 2


def test_v96p20_wide_exact_consensus_drops_one_false_leader_and_restores_tail() -> None:
    image = Image.new("RGB", (400, 600), "white")
    candidate = "られようが"
    bbox = _draw_slots(image, left=90, top=90, count=len(candidate))
    donor = _donor_for_bbox("けられよ", bbox, image)
    model = _Model([candidate, candidate])
    result = worker._reread_wide_vertical_layout_donors_exact_crop(model, image, [donor])[0]
    assert result["text"] == candidate
    assert result["provenance"]["wide_vertical_exact_crop_relation"] == "drop-one-leading-plus-trailing"
    assert result["provenance"]["wide_vertical_exact_crop_consensus_views"] == [candidate, candidate]
    assert model.calls == 2


def test_v96p20_generalized_wide_cases_require_detector_consensus() -> None:
    image = Image.new("RGB", (400, 600), "white")
    candidate = "おれは酒や食い物を"
    bbox = _draw_slots(image, left=90, top=90, count=len(candidate))
    donor = _donor_for_bbox("おれは酒や", bbox, image)
    original = dict(donor)
    result = worker._reread_wide_vertical_layout_donors_exact_crop(
        _Model([candidate, "おれは酒や食べ物を"]), image, [donor]
    )[0]
    assert result["text"] == original["text"]
    assert "wide_vertical_exact_crop_reread" not in result["provenance"]


def test_v96p20_generation_markers_advance() -> None:
    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
    assert manga._REGION_CACHE_KEY == "pudge-manga-regions-v96p27-orphan-vertical-ink"
