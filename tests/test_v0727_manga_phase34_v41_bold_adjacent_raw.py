from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def _primary_dense() -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": 0.110342,
        "y": 0.751500,
        "width": 0.056947,
        "height": 0.192833,
        "confidence": 0.75,
        "detector": worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "geometry_source": worker._LAYOUT_DETECTOR,
        "geometry_status": "observed",
        "provenance": {
            "proposal_kind": "vertical_text_line",
            "component_count": 1,
            "component_coverage": 1.0,
            "black_ratio": 0.4574,
            "white_ratio": 0.4519,
            "midtone_ratio": 0.0907,
            "single_merged_component": False,
        },
    }


def _raw(
    *,
    x: float = 0.055263,
    y: float = 0.823333,
    width: float = 0.051316,
    height: float = 0.088333,
    components: int = 6,
    coverage: float = 1.2885,
    black: float = 0.4514,
    white: float = 0.4674,
    mid: float = 0.0812,
) -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "confidence": 0.70,
        "detector": worker._RAW_LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "geometry_source": worker._RAW_LAYOUT_DETECTOR,
        "geometry_status": "observed",
        "provenance": {
            "proposal_kind": "vertical_text_line_raw",
            "component_count": components,
            "component_coverage": coverage,
            "black_ratio": black,
            "white_ratio": white,
            "midtone_ratio": mid,
            "single_merged_component": False,
        },
    }


def test_v41_real_p008_bold_raw_companion_is_supported() -> None:
    support = worker._raw_layout_support(
        _raw(),
        [],
        [_primary_dense()],
        760,
        1200,
    )
    assert support is not None
    assert support["support_kind"] == "bold-adjacent-raw-line-v1"
    assert support["support_component_count"] == 6
    assert support["support_spacing_px"] == 44.0
    assert support["support_vertical_overlap"] == 1.0


def test_v41_same_high_black_raw_lane_is_not_standalone_without_dense_peer() -> None:
    assert worker._raw_layout_support(_raw(), [], [], 760, 1200) is None


def test_v41_sparse_one_component_sfx_does_not_corroborate_bold_raw_lane() -> None:
    sparse_peer = _primary_dense()
    sparse_peer["width"] = 0.035895
    sparse_peer["height"] = 0.145333
    sparse_peer["x"] = 0.110342
    sparse_peer["provenance"] = {
        **sparse_peer["provenance"],
        "black_ratio": 0.1206,
        "white_ratio": 0.8218,
        "midtone_ratio": 0.0576,
    }
    assert worker._tall_dense_merged_layout_candidate(sparse_peer) is False
    assert worker._raw_layout_support(_raw(), [], [sparse_peer], 760, 1200) is None


def test_v41_unrelated_high_black_raw_lane_stays_blocked_at_wrong_pitch() -> None:
    candidate = _raw(x=0.20)
    assert worker._raw_layout_support(candidate, [], [_primary_dense()], 760, 1200) is None


def test_v41_unrelated_high_black_raw_lane_stays_blocked_with_low_overlap() -> None:
    candidate = _raw(y=0.52)
    assert worker._raw_layout_support(candidate, [], [_primary_dense()], 760, 1200) is None


def test_v41_supplemental_raw_line_keeps_pixel_geometry_and_support_provenance(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    raw = _raw()
    primary = _primary_dense()
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda _image: [dict(raw)])

    out = worker._supplemental_raw_layout_lines(image, [], [primary])

    assert len(out) == 1
    assert out[0]["x"] == raw["x"]
    assert out[0]["y"] == raw["y"]
    assert out[0]["width"] == raw["width"]
    assert out[0]["height"] == raw["height"]
    assert out[0]["provenance"]["support_kind"] == "bold-adjacent-raw-line-v1"
    image.close()


def test_v41_proposal_pipeline_promotes_supported_bold_companion(monkeypatch) -> None:
    image = Image.new("RGB", (760, 1200), "white")
    primary = _primary_dense()
    raw = _raw()

    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda _image: [dict(primary)])
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda _image: [dict(raw)])
    monkeypatch.setattr(worker, "_layout_recovery_enabled", lambda _regions, _primary: True)
    monkeypatch.setattr(worker, "_contextual_missing_layout_lines", lambda _image, _regions, _peers: [])

    out = worker._manga_layout_line_proposals(image, [])

    matches = [
        item
        for item in out
        if item.get("detector") == worker._RAW_LAYOUT_DETECTOR
        and (item.get("provenance") or {}).get("support_kind") == "bold-adjacent-raw-line-v1"
    ]
    assert len(matches) == 1
    assert matches[0]["x"] == raw["x"]
    assert matches[0]["y"] == raw["y"]
    image.close()
