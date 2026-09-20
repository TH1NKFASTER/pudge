"""Ruby-surviving, full-size print lane recovery: independently measured ink and OCR."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pudge import manga, manga_ocr_worker as worker

FIXTURES = Path(os.environ.get("PUDGE_V96P28_FIXTURES", Path(__file__).resolve().parent / "fixtures/v96p28"))


def _page(index: int):
    image_path = FIXTURES / "pages" / f"page_{index:03d}.png"
    data_path = FIXTURES / "fresh" / f"page_{index:02d}.json"
    if not image_path.is_file() or not data_path.is_file():
        pytest.skip("actual page and normalized baseline supplied by v96p28 installer")
    image = Image.open(image_path).convert("RGB")
    rows = json.loads(data_path.read_text(encoding="utf-8"))["data"]["regions"]
    return image, rows


def _fake_real_crop(crop):
    """Use verified Mac views; an unmeasured crop must not gain a target reading."""
    readings = {
        (58, 310): "おれは友達を", (62, 336): "おれは友達を",
        (57, 154): "おれは", (57, 156): "友達を",
    }
    return readings.get(crop.size, "")


def test_real_p36_only_physically_qualified_candidate_in_all_40_pages():
    found = {}
    for page in range(40):
        image, rows = _page(page)
        try:
            candidates = worker._ruby_anchored_full_main_candidates(image, rows)
            if candidates:
                found[page] = [(tuple(round(v) for v in worker._pixel_bbox(p, *image.size)),
                                anchor.get("text")) for p, anchor in candidates]
        finally:
            image.close()
    assert found == {36: [((155, 760, 213, 1070), "ともだち")]}


def test_real_p36_ruby_main_recovers_independently_confirmed_complete_print():
    image, rows = _page(36)
    try:
        calls = []

        def model(crop):
            calls.append(crop.size)
            return _fake_real_crop(crop)

        repaired = worker._recover_ruby_anchored_full_main_lanes(model, image, rows)
        assert [r.get("text") for r in repaired[len(rows):]] == ["おれは友達を"]
        assert len(calls) == 4
        added = repaired[-1]
        assert added["recognizer_retry"] == "ruby-main-full-and-split-consensus-v1"
        assert added["provenance"]["ruby_anchor_bbox_px"] == [185.86, 923.8, 199.14, 997.2]
        assert len(added["segments"]) == len(added["text"])
        assert repaired[:len(rows)] == rows
        assert [r.get("text") for r in manga._finalize_recognized_regions(
            worker._finalize_worker_output_regions(repaired))
            if r.get("recognizer_retry") == "ruby-main-full-and-split-consensus-v1"] == ["おれは友達を"]
        assert worker._recover_ruby_anchored_full_main_lanes(model, image, repaired) == repaired
    finally:
        image.close()


@pytest.mark.parametrize("bad_view", [0, 1, 2, 3])
def test_real_p36_one_disagreeing_read_prevents_text_invention(bad_view):
    image, rows = _page(36)
    try:
        views = ["おれは友達を", "おれは友達を", "おれは", "友達を"]
        views[bad_view] = "おれよ友達を" if bad_view < 2 else "ともだち"
        it = iter(views)
        assert worker._recover_ruby_anchored_full_main_lanes(
            lambda _crop: next(it), image, rows) == rows
    finally:
        image.close()


def test_fake_ruby_without_main_ink_cannot_trigger_ocr():
    image = Image.new("RGB", (760, 1200), "white")
    anchor = {"text": "ともだち", "source": worker._LAYOUT_LINE_SOURCE,
              "detector": worker._LAYOUT_DETECTOR, "orientation": "vertical",
              "x": 186/760, "y": 1-997/1200, "width": 13/760, "height": 73/1200}
    try:
        ImageDraw.Draw(image).rectangle((186, 924, 199, 997), fill="black")
        assert worker._ruby_anchored_full_main_candidates(image, [anchor]) == []
        assert worker._recover_ruby_anchored_full_main_lanes(
            lambda _crop: pytest.fail("should not call model"), image, [anchor]) == [anchor]
    finally:
        image.close()


def test_real_p36_existing_main_peer_blocks_duplicate_recovery():
    image, rows = _page(36)
    try:
        already = worker._recover_ruby_anchored_full_main_lanes(_fake_real_crop, image, rows)
        assert worker._ruby_anchored_full_main_candidates(image, already) == []
        assert worker._recover_ruby_anchored_full_main_lanes(
            lambda _crop: pytest.fail("already repaired"), image, already) == already
    finally:
        image.close()


def test_real_p36_ruby_main_retried_only_after_blocker_is_removed(monkeypatch):
    """A transient detector row blocks the initial pass, but is absent from output."""
    image, rows = _page(36)
    try:
        physical, _ = worker._ruby_anchored_full_main_candidates(image, rows)[0]
        # This post-cluster rectangle is a temporary read, not a second main
        # lane. A real surviving peer remains a hard blocker in the test below.
        transient = {**physical, "text": "", "source": "layout-cluster-v2",
                     "detector": "post-cluster-temporary"}
        raw = [*rows, transient]
        assert not worker._ruby_anchored_full_main_candidates(image, raw)
        assert worker._recover_ruby_anchored_full_main_lanes(
            lambda _crop: pytest.fail("blocked before cleanup"), image, raw) == raw
        unpatched_finalize = worker._finalize_worker_output_regions

        def cleanup(output):
            return unpatched_finalize([row for row in output if row is not transient])

        monkeypatch.setattr(worker, "_finalize_worker_output_regions", cleanup)
        calls = []

        def model(crop):
            calls.append(crop.size)
            return _fake_real_crop(crop)

        recovered = worker._recover_ruby_main_after_output_cleanup(model, image, raw)
        assert recovered[:len(raw)] == raw
        assert len(calls) == 4
        assert [(r.get("text"), r.get("recognizer_retry")) for r in recovered[len(raw):]] == [
            ("おれは友達を", "ruby-main-full-and-split-consensus-v1")]
        persisted = manga._finalize_recognized_regions(
            unpatched_finalize([r for r in recovered if r is not transient]))
        assert [r.get("text") for r in persisted if r.get("recognizer_retry") ==
                "ruby-main-full-and-split-consensus-v1"] == ["おれは友達を"]
        # A second pass must neither re-read the same column nor duplicate it.
        assert worker._recover_ruby_main_after_output_cleanup(
            lambda _crop: pytest.fail("existing main must block"), image, recovered) == recovered
    finally:
        image.close()


def test_real_p36_surviving_main_peer_still_blocks_late_recovery(monkeypatch):
    image, rows = _page(36)
    try:
        proposal, _ = worker._ruby_anchored_full_main_candidates(image, rows)[0]
        peer = {**proposal, "text": "おれは友達を", "source": worker._LAYOUT_LINE_SOURCE}
        transient = {**proposal, "text": "", "source": "layout-cluster-v2"}
        raw = [*rows, peer, transient]
        original_finalize = worker._finalize_worker_output_regions
        monkeypatch.setattr(worker, "_finalize_worker_output_regions", lambda out:
                            original_finalize([r for r in out if r is not transient]))
        assert worker._recover_ruby_main_after_output_cleanup(
            lambda _crop: pytest.fail("surviving peer must block"), image, raw) == raw
    finally:
        image.close()


def test_saved_40_pages_do_not_trigger_ruby_main_second_pass():
    for page in range(40):
        image, rows = _page(page)
        try:
            assert worker._recover_ruby_main_after_output_cleanup(
                lambda _crop: pytest.fail(f"unexpected OCR second pass on page {page}"),
                image, rows) == rows
        finally:
            image.close()
