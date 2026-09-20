from __future__ import annotations

from pudge import manga_ocr_worker as worker


def _segment(text: str, y: float) -> dict[str, object]:
    return {
        "text": text,
        "orientation": "vertical",
        "x": 0.429,
        "y": y,
        "width": 0.022,
        "height": 0.009,
        "source": "layout-line-ink-v2+tight-v1",
        "geometry_status": "approximate",
    }


def test_v96p12_trims_bidirectional_peer_bleed_from_p26_wide_donor() -> None:
    repair = getattr(worker, "_trim_wide_donor_bidirectional_peer_bleed", None)
    assert repair is not None

    right_peer = {
        "text": "そうかしら",
        "raw_text": "そうかしら",
        "orientation": "vertical",
        "x": 0.464289,
        "y": 0.5215,
        "width": 0.026684,
        "height": 0.0745,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": "wide-vertical-text-donor-v1",
        "provenance": {"wide_vertical_text_donor": True},
    }
    donor_text = "しら私はあんな事さ"
    donor = {
        "text": donor_text,
        "raw_text": donor_text,
        "orientation": "vertical",
        "x": 0.427632,
        "y": 0.5075,
        "width": 0.025,
        "height": 0.0875,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": "wide-vertical-text-donor-v1",
        "segments": [
            _segment("し", 0.5825),
            _segment("ら", 0.5750),
            _segment("私", 0.566667),
            _segment("は", 0.553333),
            _segment("あ", 0.546667),
            _segment("ん", 0.5375),
            _segment("な", 0.5275),
            _segment("事", 0.5175),
            _segment("さ", 0.508333),
        ],
        "provenance": {
            "wide_vertical_text_donor": True,
            "component_count": 9,
            "component_coverage": 1.2524,
        },
    }
    left_peer = {
        "text": "されても",
        "raw_text": "されても",
        "orientation": "vertical",
        "x": 0.399816,
        "y": 0.5365,
        "width": 0.026684,
        "height": 0.0595,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {"component_count": 4, "component_coverage": 1.0},
    }

    repaired = repair([right_peer, donor, left_peer])

    assert [piece["text"] for piece in repaired] == [
        "そうかしら",
        "私はあんな事",
        "されても",
    ]
    middle = repaired[1]
    assert [segment["text"] for segment in middle["segments"]] == list("私はあんな事")
    assert middle["recognition_selection"] == "wide-vertical-layout-donor-bidirectional-trim-v1"
    provenance = middle["provenance"]
    assert provenance["bidirectional_peer_bleed_trim"] is True
    assert provenance["bidirectional_peer_bleed_trim_original_text"] == donor_text
    assert provenance["bidirectional_peer_bleed_trim_right_suffix"] == "しら"
    assert provenance["bidirectional_peer_bleed_trim_left_prefix"] == "さ"


def test_v96p12_requires_both_sides_so_p13_legit_overlap_is_unchanged() -> None:
    repair = getattr(worker, "_trim_wide_donor_bidirectional_peer_bleed", None)
    assert repair is not None

    donor_text = "今日は顔に大ケ"
    donor = {
        "text": donor_text,
        "raw_text": donor_text,
        "orientation": "vertical",
        "x": 0.630263,
        "y": 0.870833,
        "width": 0.026316,
        "height": 0.071667,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": "wide-vertical-text-donor-v1",
        "segments": [_segment(ch, 0.87 - i * 0.009) for i, ch in enumerate(donor_text)],
        "provenance": {
            "wide_vertical_text_donor": True,
            "component_count": 7,
            "component_coverage": 1.0714,
        },
    }
    left_peer = {
        "text": "大ケガまで",
        "raw_text": "大ケガまで",
        "orientation": "vertical",
        "x": 0.593237,
        "y": 0.869833,
        "width": 0.038526,
        "height": 0.0745,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "provenance": {"component_count": 4, "component_coverage": 0.954},
    }

    repaired = repair([donor, left_peer])
    assert [piece["text"] for piece in repaired] == [donor_text, "大ケガまで"]
