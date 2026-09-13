from __future__ import annotations

from pudge import manga


def _member(x: float, text: str) -> dict[str, object]:
    return {
        "x": x,
        "y": 0.73,
        "width": 0.04,
        "height": 0.21,
        "orientation": "vertical",
        "text": text,
    }


def _recalled(member: dict[str, object], text: str, members: list[dict[str, object]], direct: str) -> dict[str, object]:
    return {
        **member,
        "text": text,
        "recognition_selection": "post-recognition-cluster-consensus-v1",
        "provenance": {
            "post_recognition_cluster_donor": True,
            "cluster_direct_text": direct,
            "member_boxes": members,
        },
    }


def test_finalized_service_payload_suppresses_transverse_cluster_donor() -> None:
    members = [
        _member(0.76, "戊己"),
        _member(0.80, "丙丁"),
        _member(0.84, "甲乙"),
    ]
    direct = "甲乙丙丁戊己"
    donors = [
        _recalled(members[2], "甲乙", members, direct),
        _recalled(members[1], "丙丁", members, direct),
        _recalled(members[0], "戊己", members, direct),
    ]
    transverse = {
        "x": 0.759,
        "y": 0.889,
        "width": 0.122,
        "height": 0.055,
        "orientation": "vertical",
        "confidence": 0.25,
        "text": "甲乙丙丁",
        "segments": [
            {"text": "", "x": 0.76, "y": 0.91, "width": 0.12, "height": 0.02, "source": ""},
        ],
    }

    finalized = manga._finalize_recognized_regions([transverse, *donors])

    assert [region["text"] for region in finalized] == ["甲乙", "丙丁", "戊己"]


def test_finalized_service_payload_keeps_narrow_non_transverse_region() -> None:
    members = [
        _member(0.76, "戊己"),
        _member(0.80, "丙丁"),
        _member(0.84, "甲乙"),
    ]
    direct = "甲乙丙丁戊己"
    donors = [
        _recalled(members[2], "甲乙", members, direct),
        _recalled(members[1], "丙丁", members, direct),
        _recalled(members[0], "戊己", members, direct),
    ]
    local = {
        "x": 0.84,
        "y": 0.88,
        "width": 0.04,
        "height": 0.06,
        "orientation": "vertical",
        "confidence": 0.25,
        "text": "甲乙",
        "segments": [{"text": "", "x": 0.84, "y": 0.89, "width": 0.04, "height": 0.02, "source": ""}],
    }

    finalized = manga._finalize_recognized_regions([local, *donors])

    assert finalized[0]["text"] == "甲乙"
    assert len(finalized) == 4
