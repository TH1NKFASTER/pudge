"""Public replacements for retired private donor, art-retry and acceptance tests."""

from copy import deepcopy

import pytest
from PIL import Image
from synthetic_ocr import Readings, glyphs, region

from pudge import manga_ocr_worker as worker

TEXT = "空には星がある"


def _donor_scene(kind="suffix"):
    image = Image.new("RGB", (760, 1200), "white")
    glyphs(image, (350, 140, 379, 336), len(TEXT))
    old = TEXT + "青空" if kind == "suffix" else "青空" + TEXT[:-1]
    donor = region(
        old,
        (350, 140, 379, 336),
        detector="wide-vertical-text-donor-v1",
        confidence=0.3,
        provenance={"wide_vertical_text_donor": True, "detector_bbox_px": [350, 140, 379, 336]},
    )
    peer = region(
        "青空を見た" if kind == "suffix" else "青空",
        (320, 140, 343, 336) if kind == "suffix" else (392, 140, 414, 336),
    )
    return image, donor, peer


@pytest.mark.parametrize(
    "kind,relation",
    [
        ("suffix", "exact-crop-drop-adjacent-trailing-two"),
        ("prefix", "exact-crop-drop-adjacent-leading-two-and-recover-tail"),
    ],
)
def test_foreign_glyphs_belong_to_separate_physical_lane(kind, relation):
    image, donor, peer = _donor_scene(kind)
    with image:
        rows = [donor, peer]
        original = deepcopy(rows)
        model = Readings([TEXT] * 4)
        result = worker._reread_wide_vertical_layout_donors_exact_crop(model, image, rows)
        assert result[0]["text"] == TEXT and result[1]["text"] == peer["text"]
        assert result[0]["provenance"]["wide_vertical_exact_crop_relation"] == relation
        assert len(model.calls) == 4 and len(set(model.calls)) == 4
        assert "".join(seg["text"] for seg in result[0]["segments"]) == TEXT
        assert all(seg["source"].startswith("layout-line-ink-v2") for seg in result[0]["segments"])
        assert all(result[0][key] == donor[key] for key in ("x", "y", "width", "height"))
        assert rows == original


@pytest.mark.parametrize("bad_view", [1, 2, 3])
@pytest.mark.parametrize("kind", ["suffix", "prefix"])
def test_any_disagreeing_detector_view_rejects_foreign_lane_repair(kind, bad_view):
    image, donor, peer = _donor_scene(kind)
    with image:
        answers = [TEXT] * 4
        answers[bad_view] = "空には花がある"
        result = worker._reread_wide_vertical_layout_donors_exact_crop(
            Readings(answers), image, [donor, peer]
        )
        assert result[0]["text"] == donor["text"]


@pytest.mark.parametrize(
    "guard",
    ["no_peer", "wrong_side", "far_peer", "no_overlap", "wrong_text", "no_ink", "unrelated_candidate"],
)
def test_foreign_lane_repair_needs_correct_peer_and_physical_ink(guard):
    image, donor, peer = _donor_scene()
    with image:
        peers = [peer]
        candidate = TEXT
        if guard == "no_peer":
            peers = []
        elif guard == "wrong_side":
            peer["x"] = 392 / 760
        elif guard == "far_peer":
            peer["x"] = 0.1
        elif guard == "no_overlap":
            peer["y"] = 0.1
        elif guard == "wrong_text":
            peer["text"] = "星を見る"
        elif guard == "no_ink":
            image.paste("white", (0, 0, 760, 1200))
        else:
            candidate = "今日は雨が降る"
        result = worker._reread_wide_vertical_layout_donors_exact_crop(
            Readings([candidate] * 4), image, [donor, *peers]
        )
        assert result[0]["text"] == donor["text"]


def _art_retry(branch):
    fields = {
        "confidence": 0.5,
        "selected_hypothesis_id": "manga-ocr-square-retry",
        "hypotheses": [{"id": "manga-ocr", "text": "あ"}],
        "provenance": {
            "component_count": 2,
            "component_coverage": 1.1,
            "support_kind": "observed-gap",
            "black_ratio": 0.08,
        },
    }
    if branch == "gap":
        return region("カナ", (200, 300, 220, 330), detector="manga-raw-components-v1", **fields)
    fields["hypotheses"] = [{"id": "manga-ocr", "text": "…"}]
    if branch == "strokes":
        fields["provenance"]["component_count"] = 3
        return region("なな", (200, 300, 220, 342), **fields)
    fields["selected_hypothesis_id"] = "manga-ocr-xy-context-retry"
    fields["provenance"]["component_coverage"] = 0.5
    return region("ここ", (200, 1140, 220, 1200), **fields)


@pytest.mark.parametrize("branch", ["gap", "strokes", "edge"])
def test_unsupported_art_retry_is_rejected_at_serialization_boundary(branch):
    row = _art_retry(branch)
    before = deepcopy(row)
    assert worker._v96p24_unsupported_art_retry(row)
    verified = region("空には星がある", (400, 200, 435, 430), confidence=0.99)
    assert worker._suppress_v96p24_unsupported_art_retries([row, verified]) == [verified]
    assert worker._finalize_worker_output_regions([row, verified]) == [verified]
    assert row == before


@pytest.mark.parametrize("branch", ["gap", "strokes", "edge"])
@pytest.mark.parametrize(
    "guard", ["source", "orientation", "selection", "confidence", "count", "coverage", "first_read"]
)
def test_art_retry_rejection_requires_all_observation_and_hypothesis_guards(branch, guard):
    row = _art_retry(branch)
    if guard == "source":
        row["source"] = "other"
    elif guard == "orientation":
        row["orientation"] = "horizontal"
    elif guard == "selection":
        row["selected_hypothesis_id"] = "manga-ocr"
    elif guard == "confidence":
        row["confidence"] = 0.99
    elif guard == "count":
        row["provenance"]["component_count"] = 5
    elif guard == "coverage":
        row["provenance"]["component_coverage"] = 0.8 if branch == "edge" else 0.5
    else:
        row["hypotheses"][0]["text"] = row["text"]
    assert not worker._v96p24_unsupported_art_retry(row)
    assert worker._suppress_v96p24_unsupported_art_retries([row]) == [row]
