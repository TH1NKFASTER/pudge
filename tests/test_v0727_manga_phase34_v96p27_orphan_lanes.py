"""Generated regressions for missed leading/fused lanes and clipped donors."""

from copy import deepcopy

import pytest
from PIL import Image, ImageDraw
from synthetic_ocr import Readings, forbidden, glyphs, leading_scene, merged_scene, region

from pudge import manga
from pudge import manga_ocr_worker as worker


@pytest.mark.parametrize("offset", [-120, 0, 160])
def test_three_independent_components_recover_leading_text_and_keep_tail(offset):
    image, rows = leading_scene(offset=offset)
    with image:
        before = deepcopy(rows)
        proposals = worker._orphan_bold_leading_lanes(image, rows)
        assert len(proposals) == 1
        assert len(proposals[0]["provenance"]["component_bboxes_px"]) == 3
        model = Readings(["星を見る"] * 3)
        result = worker._recover_orphan_vertical_lanes(model, image, rows)
        assert result[: len(rows)] == before == rows
        assert result[-1]["text"] == "星を見る"
        assert result[-1]["recognizer_retry"] == "orphan-vertical-consensus-v1"
        assert result[-1]["segments"] and len(model.calls) == 3
        assert worker._recover_orphan_vertical_lanes(forbidden, image, result) == result


@pytest.mark.parametrize("count", [0, 1, 2, 4])
def test_leading_lane_requires_exactly_three_separate_components(count):
    image, rows = leading_scene(count=count)
    with image:
        assert worker._orphan_bold_leading_lanes(image, rows) == []
        assert worker._recover_orphan_vertical_lanes(forbidden, image, rows) == rows


def test_leading_lane_rejects_joined_ink_or_existing_coverage():
    image, rows = leading_scene()
    with image:
        proposal = worker._orphan_bold_leading_lanes(image, rows)[0]
        assert worker._orphan_bold_leading_lanes(image, [*rows, proposal]) == []
        ImageDraw.Draw(image).rectangle((320, 148, 321, 226), fill="black")
        assert worker._orphan_bold_leading_lanes(image, rows) == []


@pytest.mark.parametrize("offset", [-100, 0, 200])
def test_fused_lane_is_detected_from_generated_pixels(offset):
    image, rows = merged_scene(offset=offset)
    with image:
        proposals = worker._orphan_merged_vertical_lanes(image, rows)
        assert len(proposals) == 1
        assert proposals[0]["provenance"]["component_count"] == 1
        result = worker._recover_orphan_vertical_lanes(Readings(["空には星がある"] * 3), image, rows)
        assert len(result) == 1 and result[0]["text"] == "空には星がある"
        assert result[0]["segments"]
        assert worker._recover_orphan_vertical_lanes(forbidden, image, result) == result


@pytest.mark.parametrize("bad_read", ["", "ARTWORK", "星"])
def test_unsupported_readings_do_not_create_text(bad_read):
    for make_scene in (leading_scene, merged_scene):
        image, rows = make_scene()
        with image:
            assert worker._recover_orphan_vertical_lanes(lambda _crop: bad_read, image, rows) == rows


def test_merged_lane_uses_asymmetric_context_only_with_independent_agreement():
    image, rows = merged_scene()
    with image:
        model = Readings(["空に星", "空の星", "空と星", "空には星がある", "空には星がある", "空には花がある"])
        result = worker._recover_orphan_vertical_lanes(model, image, rows)
        assert result[-1]["text"] == "空には星がある"
        assert len(model.calls) == 6 and len(set(model.calls)) == 6
        disagree = Readings(
            ["空に星", "空の星", "空と星", "空には星がある", "空には花がある", "空にも星がある"]
        )
        assert worker._recover_orphan_vertical_lanes(disagree, image, rows) == rows


@pytest.mark.parametrize("obstruction", ["upstream", "both_sides"])
def test_fused_lane_rejects_context_art(obstruction):
    image, rows = merged_scene()
    with image:
        draw = ImageDraw.Draw(image)
        if obstruction == "upstream":
            draw.rectangle((243, 122, 266, 131), fill="black")
        else:
            draw.rectangle((225, 150, 233, 320), fill="black")
            draw.rectangle((276, 150, 283, 320), fill="black")
        assert worker._orphan_merged_vertical_lanes(image, rows) == []


@pytest.mark.parametrize("agree", [True, False])
def test_cleanup_retries_newly_unblocked_lane_without_dropping_input(monkeypatch, agree):
    image, _ = merged_scene()
    with image:
        blocker = {**worker._orphan_merged_vertical_lanes(image, [])[0], "text": ""}
        raw = [blocker]
        assert worker._orphan_merged_vertical_lanes(image, raw) == []
        monkeypatch.setattr(worker, "_finalize_worker_output_regions", lambda _rows: [])
        monkeypatch.setattr(manga, "_finalize_recognized_regions", lambda rows: rows)
        answers = (
            ["空には星がある"] * 3
            if agree
            else ["空に星", "空の星", "空と星", "空には星がある", "空には花がある", "空にも星がある"]
        )
        model = Readings(answers)
        result = worker._recover_orphan_after_worker_cleanup(model, image, raw)
        assert result[: len(raw)] == raw
        assert len(result) == 1 + int(agree)
        assert len(model.calls) == (3 if agree else 6)


def test_unchanged_cleanup_does_not_repeat_ocr():
    image, rows = merged_scene()
    with image:
        assert worker._recover_orphan_after_worker_cleanup(forbidden, image, rows) == rows


def _clipped_scene():
    image = Image.new("RGB", (760, 1200), "white")
    glyphs(image, (510, 203, 530, 290), 5)
    row = region("には星が", (510, 220, 530, 290), confidence=0.20)
    row.update(
        detector="wide-vertical-text-donor-v1",
        provenance={
            "proposal_kind": "vertical_text_line_raw",
            "component_count": 4,
            "component_coverage": 0.95,
        },
    )
    return image, row


def test_clipped_donor_recovers_only_observed_upper_ink_and_consensus():
    image, row = _clipped_scene()
    with image:
        original = deepcopy(row)
        result = worker._recover_low_confidence_clipped_vertical_donors(
            Readings(["空には星が"] * 3), image, [row]
        )[0]
        assert row == original and result["text"] == "空には星が"
        assert result["recognizer_retry"] == "clipped-low-confidence-donor-consensus-v1"
        assert result["provenance"]["observed_leading_dark_pixels"] >= 20
        assert result["segments"] and result["y"] == pytest.approx(row["y"], abs=1e-6)
        assert result["height"] > row["height"]
        assert worker._recover_low_confidence_clipped_vertical_donors(
            Readings(["空には星が", "空には花が", "空にも星が"]), image, [row]
        ) == [row]


@pytest.mark.parametrize(
    "guard", ["confidence", "detector", "count", "coverage", "prior_trim", "no_upper_ink"]
)
def test_clipped_donor_guards_prevent_extra_model_calls(guard):
    image, row = _clipped_scene()
    with image:
        if guard == "confidence":
            row["confidence"] = 0.9
        elif guard == "detector":
            row["detector"] = "other"
        elif guard == "count":
            row["provenance"]["component_count"] = 3
        elif guard == "coverage":
            row["provenance"]["component_coverage"] = 0.4
        elif guard == "prior_trim":
            row["provenance"]["bidirectional_peer_bleed_trim"] = True
        else:
            ImageDraw.Draw(image).rectangle((508, 200, 533, 219), fill="white")
        assert worker._recover_low_confidence_clipped_vertical_donors(forbidden, image, [row]) == [row]
