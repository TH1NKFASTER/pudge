"""Generated main-print/furigana scenes, including transient overlap blockers."""

from copy import deepcopy

import pytest
from synthetic_ocr import Readings, forbidden, ruby_scene

from pudge import manga
from pudge import manga_ocr_worker as worker

READINGS = ["空には星がある", "空には星がある", "空には", "星がある"]


def test_ruby_main_requires_four_observed_bands_and_four_consistent_reads():
    image, rows = ruby_scene()
    with image:
        original = deepcopy(rows)
        proposals = worker._ruby_anchored_full_main_candidates(image, rows)
        assert len(proposals) == 1
        assert min(proposals[0][0]["provenance"]["physical_main_ink_ratios"]) >= 0.28
        model = Readings(READINGS)
        result = worker._recover_ruby_anchored_full_main_lanes(model, image, rows)
        assert result[: len(rows)] == original == rows
        added = result[-1]
        assert added["text"] == "空には星がある"
        assert added["recognizer_retry"] == "ruby-main-full-and-split-consensus-v1"
        assert len(added["segments"]) == len(added["text"])
        assert len(model.calls) == 4 and len(set(model.calls)) == 4
        persisted = manga._finalize_recognized_regions(worker._finalize_worker_output_regions(result))
        assert any(row["text"] == added["text"] for row in persisted)
        assert worker._ruby_anchored_full_main_candidates(image, result) == []
        assert worker._recover_ruby_anchored_full_main_lanes(forbidden, image, result) == result


@pytest.mark.parametrize("bad_view", range(4))
def test_one_disagreeing_view_prevents_text_invention(bad_view):
    image, rows = ruby_scene()
    with image:
        readings = list(READINGS)
        readings[bad_view] = "別の文字"
        assert worker._recover_ruby_anchored_full_main_lanes(Readings(readings), image, rows) == rows


@pytest.mark.parametrize("omitted_band", range(4))
def test_each_missing_physical_band_prevents_any_ocr(omitted_band):
    image, rows = ruby_scene(omitted_band=omitted_band)
    with image:
        assert worker._ruby_anchored_full_main_candidates(image, rows) == []
        assert worker._recover_ruby_anchored_full_main_lanes(forbidden, image, rows) == rows


@pytest.mark.parametrize("surviving_peer", [False, True])
def test_cleanup_only_retries_after_transient_blocker_disappears(monkeypatch, surviving_peer):
    image, rows = ruby_scene()
    with image:
        proposal = worker._ruby_anchored_full_main_candidates(image, rows)[0][0]
        transient = {**proposal, "text": "", "source": "layout-cluster-v2"}
        peer = {**proposal, "text": "空には星がある"}
        raw = [*rows, *([peer] if surviving_peer else []), transient]
        assert worker._ruby_anchored_full_main_candidates(image, raw) == []
        monkeypatch.setattr(
            worker,
            "_finalize_worker_output_regions",
            lambda output: [r for r in output if r is not transient],
        )
        monkeypatch.setattr(manga, "_finalize_recognized_regions", lambda output: output)
        model = Readings(READINGS) if not surviving_peer else forbidden
        result = worker._recover_ruby_main_after_output_cleanup(model, image, raw)
        assert result[: len(raw)] == raw
        assert len(result) == len(raw) + int(not surviving_peer)
        if not surviving_peer:
            assert len(model.calls) == 4 and result[-1]["text"] == "空には星がある"
            assert worker._recover_ruby_main_after_output_cleanup(forbidden, image, result) == result


def test_unchanged_output_does_not_trigger_ruby_second_pass():
    image, rows = ruby_scene()
    with image:
        assert worker._recover_ruby_main_after_output_cleanup(forbidden, image, rows) == rows
