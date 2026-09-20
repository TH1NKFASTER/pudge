from PIL import Image

from pudge import manga_ocr_worker as worker


def _proposal(*, x, y, width, height, black, count, coverage=0.95, detector=None):
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "detector": detector or worker._LAYOUT_DETECTOR,
        "source": worker._LAYOUT_LINE_SOURCE,
        "provenance": {
            "component_count": count,
            "component_coverage": coverage,
            "black_ratio": black,
        },
    }


def test_v94_ruby_guard_uses_raw_main_peer_before_blocker_removes_that_peer(monkeypatch):
    ruby = _proposal(
        x=0.098500,
        y=0.814833,
        width=0.016158,
        height=0.065333,
        black=0.0574,
        count=5,
        coverage=0.7105,
    )
    main = _proposal(
        x=0.126316,
        y=0.842500,
        width=0.043421,
        height=0.056667,
        black=0.1698,
        count=4,
        detector=worker._RAW_LAYOUT_DETECTOR,
    )
    blocker = {"id": "vision-main-owner"}

    monkeypatch.setattr(worker, "_layout_vertical_lines", lambda image: [ruby])
    monkeypatch.setattr(
        worker,
        "_attach_partial_weak_raw_component_support",
        lambda image, regions, primary: list(primary),
    )
    monkeypatch.setattr(worker, "_layout_recovery_enabled", lambda regions, primary: True)
    monkeypatch.setattr(
        worker,
        "_supplemental_raw_layout_lines",
        lambda image, regions, primary: [main],
    )
    monkeypatch.setattr(
        worker,
        "_contextual_missing_layout_lines",
        lambda image, regions, proposals: [],
    )
    monkeypatch.setattr(
        worker,
        "_horizontal_observation_blocks_layout",
        lambda existing, proposal: existing is blocker and proposal is main,
    )
    monkeypatch.setattr(worker, "_region_geometry_trust", lambda region: "weak")
    monkeypatch.setattr(worker, "_observed_geometry_blocks_layout", lambda existing, proposal: False)

    proposals = worker._manga_layout_line_proposals(Image.new("RGB", (760, 1200), "white"), [blocker])

    assert proposals == []
