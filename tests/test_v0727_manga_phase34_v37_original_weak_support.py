from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _primary() -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.206,
        "y": 0.376,
        "width": 0.027,
        "height": 0.101,
        "source": "manga-layout-line-v1",
        "detector": "manga-ink-components-v1",
        "provenance": {
            "component_count": 3,
            "component_coverage": 0.94,
            "black_ratio": 0.14,
            "midtone_ratio": 0.12,
        },
    }


def _raw() -> dict[str, object]:
    return {
        "orientation": "vertical",
        "x": 0.208,
        "y": 0.377,
        "width": 0.024,
        "height": 0.099,
        "source": "manga-layout-line-v1",
        "detector": "manga-raw-components-v1",
        "provenance": {
            "component_count": 9,
            "component_coverage": 1.03,
        },
    }


def _weak() -> dict[str, object]:
    return {
        "text": "れは",
        "orientation": "vertical",
        "x": 0.203,
        "y": 0.372,
        "width": 0.032,
        "height": 0.024,
        "confidence": 0.25,
        "source": "",
        "segments": [
            {
                "text": "",
                "orientation": "horizontal",
                "x": 0.209,
                "y": 0.378,
                "width": 0.020,
                "height": 0.012,
                "source": "",
            }
        ],
    }


def test_original_weak_region_recorroborates_already_built_layout_proposal(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda _image: [_raw()])
    try:
        [supported] = worker._attach_partial_weak_raw_component_support(
            image, [_weak()], [_primary()]
        )
    finally:
        image.close()

    assert supported["provenance"]["raw_component_count_support"] == 9
    assert worker._layout_text_geometry_plausible(supported, "ちゃんとおれは") is True


def test_recognize_regions_recorroborates_after_prepare_without_changing_proposal_api(monkeypatch) -> None:
    image = Image.new("RGB", (100, 100), "white")
    original = [_weak()]
    prepared: list[dict[str, object]] = []
    proposal = _primary()
    calls: list[tuple[list[dict[str, object]], list[dict[str, object]]]] = []

    monkeypatch.setattr(worker, "_prepare_regions_for_ocr", lambda _regions, _image: prepared)
    monkeypatch.setattr(worker, "_manga_layout_line_proposals", lambda _image, rows: [proposal])
    monkeypatch.setattr(worker, "_wide_vertical_geometry_candidates", lambda _image: [])

    def attach(_image, support_regions, proposals):
        calls.append((support_regions, proposals))
        return []

    monkeypatch.setattr(worker, "_attach_partial_weak_raw_component_support", attach)
    try:
        assert worker._recognize_regions(lambda _image: "", image, original) == []
    finally:
        image.close()

    # The second call is the v37 post-proposal corroboration and must use the
    # untouched detector input, not the prepared/expanded list.
    assert calls[-1][0] is original
    assert calls[-1][1] == [proposal]
