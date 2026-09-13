from __future__ import annotations

from PIL import Image

import pudge.manga_ocr_worker as worker


def test_detector_selected_kanji_surface_cannot_be_undone_by_local_consensus(monkeypatch) -> None:
    image = Image.new("RGB", (240, 100), "white")
    piece = {
        "orientation": "horizontal",
        "selected_hypothesis_id": "detector-recognition",
        "text": "田栄一郎",
        "raw_text": "田栄一郎",
        "hypotheses": [{"source": "apple-vision", "text": "田栄一郎"}],
        "segments": [
            {
                "text": ch,
                "x": 0.10 + index * 0.08,
                "y": 0.40,
                "width": 0.07,
                "height": 0.20,
                "source": "vision-accurate-range-v2",
            }
            for index, ch in enumerate("田栄一郎")
        ],
    }
    monkeypatch.setattr(worker, "_horizontal_overlap_ratio", lambda *_: 1.0)
    monkeypatch.setattr(worker, "_recognize_wide_horizontal_segment", lambda *_: "宋一")
    monkeypatch.setattr(worker, "_recognize_horizontal_lower_band", lambda *_a, **_k: "宋一")
    monkeypatch.setattr(worker, "_recognize_horizontal_baseline_masked", lambda *_: "宋一")

    repaired = worker._repair_short_kanji_pairs_with_mangaocr(object(), image, piece)

    assert repaired["text"] == "田栄一郎"
    assert [segment["text"] for segment in repaired.get("segments", piece["segments"])] == list("田栄一郎")
    assert not repaired.get("short_kanji_pair_repairs")


def test_detector_supported_candidate_can_still_repair_agreeing_pair(monkeypatch) -> None:
    image = Image.new("RGB", (240, 100), "white")
    piece = {
        "orientation": "horizontal",
        "selected_hypothesis_id": "detector-recognition",
        "text": "大左手",
        "raw_text": "大左手",
        "hypotheses": [{"source": "apple-vision", "text": "海軍大佐斧手"}],
        "segments": [
            {
                "text": ch,
                "x": 0.10 + index * 0.08,
                "y": 0.40,
                "width": 0.07,
                "height": 0.20,
                "source": "vision-accurate-range-v2",
            }
            for index, ch in enumerate("大左手")
        ],
    }
    monkeypatch.setattr(worker, "_horizontal_overlap_ratio", lambda *_: 1.0)
    monkeypatch.setattr(worker, "_recognize_wide_horizontal_segment", lambda *_: "大佐")
    monkeypatch.setattr(worker, "_recognize_horizontal_lower_band", lambda *_a, **_k: "")
    monkeypatch.setattr(worker, "_recognize_horizontal_baseline_masked", lambda *_: "")

    repaired = worker._repair_short_kanji_pairs_with_mangaocr(object(), image, piece)
    assert repaired["text"] == "大佐手"


def test_redundant_physically_impossible_wide_vertical_donor_is_dropped() -> None:
    donor = {
        "text": "誰だと思ってるナメたマネする",
        "orientation": "vertical",
        "source": "manga-layout-line-v1",
        "detector": "wide-vertical-text-donor-v1",
        "x": 0.889474,
        "y": 0.826667,
        "width": 0.025,
        "height": 0.099167,
        "provenance": {
            "component_count": 10,
            "component_coverage": 0.9829,
            "wide_vertical_text_donor": True,
        },
    }
    peer = {
        "text": "と思ってる",
        "orientation": "vertical",
        "source": "manga-layout-line-v1",
        "detector": "manga-ink-components-v1",
        "x": 0.847184,
        "y": 0.854833,
        "width": 0.033263,
        "height": 0.0845,
        "provenance": {"component_count": 6, "component_coverage": 0.9899},
    }

    assert worker._layout_retry_acceptable(donor, donor["text"]) is False
    assert worker._layout_retry_acceptable(peer, peer["text"]) is True
    out = worker._suppress_redundant_implausible_wide_vertical_donors([peer, donor])
    assert [item["text"] for item in out] == ["と思ってる"]


def test_isolated_implausible_wide_vertical_donor_is_preserved() -> None:
    donor = {
        "text": "しれえ！！",
        "orientation": "vertical",
        "source": "manga-layout-line-v1",
        "detector": "wide-vertical-text-donor-v1",
        "x": 0.50,
        "y": 0.70,
        "width": 0.025,
        "height": 0.045,
        "provenance": {
            "component_count": 3,
            "component_coverage": 0.95,
            "wide_vertical_text_donor": True,
        },
    }
    unrelated = {
        "text": "別の列",
        "orientation": "vertical",
        "source": "manga-layout-line-v1",
        "detector": "manga-ink-components-v1",
        "x": 0.25,
        "y": 0.70,
        "width": 0.03,
        "height": 0.12,
        "provenance": {"component_count": 3, "component_coverage": 0.9},
    }

    out = worker._suppress_redundant_implausible_wide_vertical_donors([donor, unrelated])
    assert any(item["text"] == "しれえ！！" for item in out)


def test_page18_collapsed_text_still_splits_into_two_final_lanes() -> None:
    image = Image.new("RGB", (760, 1200), "white")
    region = {
        "orientation": "vertical",
        "x": 0.103211,
        "y": 0.8265,
        "width": 0.061999,
        "height": 0.0545,
        "text": "まだ栓もあけてない",
        "raw_text": "まだ栓もあけてない",
    }
    proposals = [
        {
            "orientation": "vertical",
            "source": "manga-layout-line-v1",
            "x": 0.104,
            "y": 0.821,
            "width": 0.025,
            "height": 0.061,
            "provenance": {"component_count": 5, "component_coverage": 1.0},
        },
        {
            "orientation": "vertical",
            "source": "manga-layout-line-v1",
            "x": 0.134,
            "y": 0.824,
            "width": 0.025,
            "height": 0.055,
            "provenance": {"component_count": 4, "component_coverage": 1.0},
        },
    ]

    out = worker._promote_wide_vertical_text_to_layout_lanes(image, [region], proposals)
    assert [item["text"] for item in out] == ["まだ栓も", "あけてない"]
