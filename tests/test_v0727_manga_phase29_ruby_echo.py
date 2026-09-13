
from __future__ import annotations

from pudge.manga_ocr_worker import _suppress_nested_ruby_echo_layout_regions


def _layout_region(
    text: str,
    *,
    x: float,
    y: float,
    width: float,
    height: float,
    confidence: float,
    component_coverage: float,
    black_ratio: float,
) -> dict[str, object]:
    return {
        "text": text,
        "raw_text": text,
        "source": "manga-layout-line-v1",
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "confidence": confidence,
        "provenance": {
            "component_coverage": component_coverage,
            "black_ratio": black_ratio,
        },
    }


def test_nested_ruby_echo_region_inside_main_column_is_suppressed() -> None:
    main = _layout_region(
        "財宝か？",
        x=0.790605,
        y=0.535667,
        width=0.049053,
        height=0.0845,
        confidence=0.7708,
        component_coverage=0.9394,
        black_ratio=0.1563,
    )
    echo = _layout_region(
        "才臣ハ",
        x=0.814289,
        y=0.564,
        width=0.024053,
        height=0.057,
        confidence=0.7070,
        component_coverage=0.6212,
        black_ratio=0.1404,
    )

    kept = _suppress_nested_ruby_echo_layout_regions([main, echo])
    assert [item["text"] for item in kept] == ["財宝か？"]


def test_real_adjacent_columns_are_preserved() -> None:
    a = _layout_region(
        "欲しけりゃ",
        x=0.739289,
        y=0.5165,
        width=0.049053,
        height=0.103667,
        confidence=0.8486,
        component_coverage=0.9016,
        black_ratio=0.1101,
    )
    b = _layout_region(
        "財宝か？",
        x=0.790605,
        y=0.535667,
        width=0.049053,
        height=0.0845,
        confidence=0.7708,
        component_coverage=0.9394,
        black_ratio=0.1563,
    )

    kept = _suppress_nested_ruby_echo_layout_regions([a, b])
    assert [item["text"] for item in kept] == ["欲しけりゃ", "財宝か？"]
