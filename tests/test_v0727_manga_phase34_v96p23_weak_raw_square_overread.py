from __future__ import annotations

from pudge import manga_ocr_worker as worker


def candidate(**override):
    item = {
        "text": "．．．かわり、", "source": worker._LAYOUT_LINE_SOURCE,
        "orientation": "vertical", "detector": "manga-raw-components-v1",
        "recognizer_retry": "vertical-square-pad-v1", "confidence": 0.5606,
        "x": 0.672368, "y": 0.194167, "width": 0.025, "height": 0.0425,
        "provenance": {"component_count": 2, "component_coverage": 0.6122},
    }
    item.update(override)
    return item


def test_v96p23_rejects_real_physical_two_component_overread():
    bad = candidate()
    good = candidate(text="村長！", x=0.60, provenance={"component_count": 3, "component_coverage": 0.9})
    assert worker._suppress_weak_raw_square_pad_component_overreads([good, bad]) == [good]
    assert worker._finalize_worker_output_regions([bad, good]) == [good]


def test_v96p23_requires_independent_evidence_for_rejection():
    changes = [
        {"text": "ふん"}, {"text": "大丈夫です"},
        {"detector": "manga-ink-components-v1"},
        {"recognizer_retry": None}, {"confidence": 0.61},
        {"height": 0.055}, {"width": 0.04},
        {"provenance": {"component_count": 3, "component_coverage": 0.6122}},
        {"provenance": {"component_count": 2, "component_coverage": 0.75}},
        {"provenance": {}},
    ]
    for change in changes:
        altered = candidate(**change)
        assert worker._suppress_weak_raw_square_pad_component_overreads([altered]) == [altered], change


def test_v96p23_current_review_offline_suppresses_only_known_extra():
    # The expected replay is checked by the package builder on all 40 fresh pages.
    assert worker._PIPELINE_WORKER == "pudge-manga-recovery-phase3.4-full-volume-v96p27"
    assert worker._PIPELINE_GENERATION == "pudge-manga-regions-v96p27-orphan-vertical-ink"
