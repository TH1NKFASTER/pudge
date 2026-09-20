"""v96p27: physical candidate discovery and strict OCR-consensus guards."""
from __future__ import annotations

import json
import os
import pytest
from pathlib import Path

from PIL import Image, ImageDraw

from pudge import manga_ocr_worker as worker

_FIXTURES = Path(os.environ.get("PUDGE_V96P27_FIXTURES", Path(__file__).resolve().parents[2] / "v96p27-work"))


def _baseline(page: int):
    if not (_FIXTURES / "pages" / f"page_{page:03d}.png").is_file():
        pytest.skip("v96p27 real-image fixtures are only supplied by installer")
    image = Image.open(_FIXTURES / "pages" / f"page_{page:03d}.png").convert("RGB")
    result = json.loads((_FIXTURES / "fresh" / f"page_{page:02d}.json").read_text())
    return image, result["data"].get("regions", [])


def test_physical_candidates_only_on_three_verified_pages():
    leading = {}
    merged = {}
    for page in range(40):
        image, rows = _baseline(page)
        try:
            a = worker._orphan_bold_leading_lanes(image, rows)
            b = worker._orphan_merged_vertical_lanes(image, rows)
            if a:
                leading[page] = [tuple(round(n) for n in worker._pixel_bbox(r, *image.size)) for r in a]
            if b:
                merged[page] = [tuple(round(n) for n in worker._pixel_bbox(r, *image.size)) for r in b]
        finally:
            image.close()
    assert leading == {24: [(307, 112, 338, 202)]}
    assert merged == {39: [(107, 46, 140, 244)]}


def test_real_p24_leading_recovery_preserves_existing_tail():
    image, old = _baseline(24)
    try:
        candidate = worker._recover_orphan_vertical_lanes(lambda crop: "食えば", image, old)
        assert len(candidate) == len(old) + 1
        assert any(row.get("text") == "食えば" and row["recognizer_retry"] == "orphan-vertical-consensus-v1" for row in candidate)
        assert [r.get("text") for r in candidate[:len(old)]] == [r.get("text") for r in old]
        assert worker._recover_orphan_vertical_lanes(lambda crop: "食えば", image, candidate) == candidate
    finally:
        image.close()


def test_real_p39_complete_sentence_not_short_suffix():
    image, old = _baseline(39)
    try:
        repaired = worker._recover_orphan_vertical_lanes(lambda crop: "うぬぼれるなよ", image, old)
        assert [r.get("text") for r in repaired[len(old):]] == ["うぬぼれるなよ"]
        assert not any(r.get("text") == "なよ" for r in repaired)
        bad = iter(("うぬぼれるなよ", "うぬぼれるな", "うぬほれるなよ"))
        assert worker._recover_orphan_vertical_lanes(lambda crop: next(bad), image, old) == old
    finally:
        image.close()


def test_real_p25_only_low_confidence_clipped_donor_is_reexamined():
    image, old = _baseline(25)
    try:
        repaired = worker._recover_low_confidence_clipped_vertical_donors(
            lambda crop: "一生海から", image, old,
        )
        changed = [(i, row) for i, row in enumerate(repaired) if row.get("text") != old[i].get("text")]
        assert len(changed) == 1
        i, row = changed[0]
        assert old[i]["text"] == "ら落ちな"
        assert row["text"] == "一生海から"
        assert row["recognizer_retry"] == "clipped-low-confidence-donor-consensus-v1"
        assert row["provenance"]["observed_leading_dark_pixels"] >= 20
        assert len(row["segments"]) >= 1
        wrong = iter(("一生海から", "一生海まら", "一生梅から"))
        assert worker._recover_low_confidence_clipped_vertical_donors(
            lambda crop: next(wrong), image, old,
        ) == old
    finally:
        image.close()


def test_orphan_recognizer_rejects_art_english_and_failed_consensus():
    for page in (24, 39):
        image, old = _baseline(page)
        try:
            assert worker._recover_orphan_vertical_lanes(lambda crop: "ARTWORK", image, old) == old
            assert worker._recover_orphan_vertical_lanes(lambda crop: "", image, old) == old
        finally:
            image.close()


def test_no_leading_proposal_without_three_independent_ink_components():
    image = Image.new("RGB", (760, 1200), "white")
    tail = {"text": "ゴム人間！！！", "source": worker._LAYOUT_LINE_SOURCE,
            "detector": worker._LAYOUT_DETECTOR, "x":306/760,"y":1-403/1200,
            "width":41/760,"height":144/1200}
    try:
        assert worker._orphan_bold_leading_lanes(image, [tail]) == []
        draw = ImageDraw.Draw(image)
        draw.rectangle((310,115,335,198), fill="black")
        assert worker._orphan_bold_leading_lanes(image, [tail]) == []
    finally:
        image.close()


def test_real_p39_asymmetric_context_recovers_when_standard_views_disagree():
    """The actual detected merged lane extends past a symmetric crop's edge."""
    image, old = _baseline(39)
    try:
        calls = []
        def read(crop):
            calls.append(crop.size)
            # Existing symmetric views have widths 33, 39, 47 on this image.
            # Independent full/tight context views recover the same 7 glyphs.
            return "うぬぼれるなよ" if crop.size in ((36, 208), (32, 200)) else f"異読{len(calls)}"
        repaired = worker._recover_orphan_vertical_lanes(read, image, old)
        assert any(r.get("text") == "うぬぼれるなよ" and
                   r.get("recognizer_retry") == "orphan-vertical-consensus-v1"
                   for r in repaired[len(old):])
        assert len(calls) == 6
        assert calls[-3:] == [(36, 208), (32, 200), (39, 212)]
    finally:
        image.close()


def test_real_p39_contextual_retry_still_rejects_inconsistent_text():
    image, old = _baseline(39)
    try:
        reads = iter(("うぬぼれ", "うぬほれ", "うぬぼれる", "うぬぼれるなよ",
                      "うぬほれるなよ", "うぬぼれるな"))
        assert worker._recover_orphan_vertical_lanes(lambda crop: next(reads), image, old) == old
    finally:
        image.close()


def test_real_p39_cleanup_rechecks_merged_orphan_after_transient_overlap(monkeypatch):
    """A raw-only peer may block a genuine orphan until output suppression."""
    image, old = _baseline(39)
    try:
        blocked = dict(old[0])
        blocked.update({"text": "", "x": 105/760, "width": 38/760,
                        "y": 1-249/1200, "height": 205/1200})
        raw = [*old, blocked]
        assert not worker._orphan_merged_vertical_lanes(image, raw)
        original = worker._finalize_worker_output_regions
        monkeypatch.setattr(worker, "_finalize_worker_output_regions",
                            lambda rows: original([r for r in rows if r is not blocked]))
        calls = []
        def model(crop):
            calls.append(crop.size)
            return "うぬぼれるなよ"
        repaired = worker._recover_orphan_after_worker_cleanup(model, image, raw)
        added = repaired[len(raw):]
        assert len(added) == 1
        assert added[0]["text"] == "うぬぼれるなよ"
        assert added[0]["recognizer_retry"] == "orphan-vertical-consensus-v1"
        assert len(added[0]["segments"]) >= 5
        assert repaired[:len(raw)] == raw
        assert calls == [(33, 198), (39, 204), (47, 212)]
    finally:
        image.close()


def test_real_p39_cleanup_never_invents_missing_consensus(monkeypatch):
    image, old = _baseline(39)
    try:
        blocker = {"text": "", "x": 105/760, "width": 38/760,
                   "y": 1-249/1200, "height": 205/1200}
        raw = [*old, blocker]
        monkeypatch.setattr(worker, "_finalize_worker_output_regions",
                            lambda rows: [r for r in rows if r is not blocker])
        readings = iter(("うぬぼれ", "うぬほれ", "うぬぼれる", "うぬぼれるなよ",
                         "うぬほれるなよ", "うぬぼれるな"))
        assert worker._recover_orphan_after_worker_cleanup(
            lambda crop: next(readings), image, raw) == raw
    finally:
        image.close()


def test_real_p39_cleanup_has_no_extra_model_calls_without_removed_peers():
    image, old = _baseline(39)
    try:
        def forbidden(_):
            raise AssertionError("no OCR on unchanged output")
        assert worker._recover_orphan_after_worker_cleanup(forbidden, image, old) == old
    finally:
        image.close()
